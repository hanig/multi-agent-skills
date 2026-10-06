#!/usr/bin/env python3
"""Linear binding, audit, ad-hoc issue, outbox drain and tracker section commands.

All remote requests use linear_api.transport through Client. Local input bytes
are captured once; no draft, state or receipt is rewritten by the audit.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import linear_api as API
import linear_issue as LI
import linear_file as LF
import tracker_audit as TA
import skill_paths

PAGE = 'pageInfo { hasNextPage endCursor }'
# Linear rejects 100 issues x 100 nested relations as too complex. Measured
# live on 2026-10-05: 50 x 20 (both relation lists) is accepted, 50 x 50 and
# 25 x 50 are refused. Overflow relations are paged by a follow-up query.
ISSUE_PAGE = 50
NESTED_RELATION_PAGE = 20
RELATION = 'id type issue { id identifier } relatedIssue { id identifier }'
FIELDS = '''trashed id identifier title updatedAt description archivedAt
 state { type name } project { id } team { id }
 relations(first: %d) { nodes { %s } %s }
 inverseRelations(first: %d) { nodes { %s } %s }''' % (
    NESTED_RELATION_PAGE, RELATION, PAGE, NESTED_RELATION_PAGE, RELATION, PAGE)
BINDING_QUERY = '''query Binding($id: String!) {
 viewer { organization { id name } }
 project(id: $id) { id name teams(first: 100) { nodes { id key name } %s } }
}''' % PAGE


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def require_id(value, name):
    if not isinstance(value, str) or not value or value.isspace():
        raise ValueError('missing ' + name)
    return value


class Reader:
    def __init__(self, client):
        self.client = client
        self.seen = {}
        self.memberships = []
        self.pages = 0
        self.problems = []

    def pages_of(self, fetch):
        cursor, cursors, ids, nodes = None, set(), set(), []
        while True:
            conn = fetch(cursor)
            self.pages += 1
            if not isinstance(conn, dict) or not isinstance(conn.get('nodes'), list):
                raise ValueError('unreadable connection')
            for node in conn['nodes']:
                iid = require_id(node.get('id'), 'node id')
                if iid in ids:
                    raise ValueError('duplicate id in connection: ' + iid)
                ids.add(iid)
                nodes.append(node)
            info = conn['pageInfo']
            if type(info['hasNextPage']) is not bool:
                raise ValueError('invalid pageInfo')
            if not info['hasNextPage']:
                return nodes
            cursor = require_id(info['endCursor'], 'next cursor')
            if cursor in cursors:
                raise ValueError('repeated cursor')
            cursors.add(cursor)

    def remember(self, issue):
        iid = require_id(issue.get('id'), 'issue id')
        updated = require_id(issue.get('updatedAt'), 'issue updatedAt')
        if iid in self.seen and self.seen[iid]['updatedAt'] != updated:
            self.problems.append('issue changed during read: ' + iid)
        if iid not in self.seen:
            self.seen[iid] = issue
        for field in ('relations', 'inverseRelations'):
            initial = issue[field]
            def fetch(cursor, field=field, initial=initial):
                if cursor is None:
                    return initial
                q = ('query Relations($id: String!, $after: String) { issue(id: $id) { '
                     '%s(first: 100, after: $after) { nodes { %s } %s } } }' % (field, RELATION, PAGE))
                return self.client.query(q, {'id': iid, 'after': cursor})['issue'][field]
            issue[field] = {'nodes': self.pages_of(fetch)}
        return issue

    def issue(self, ref):
        q = 'query Issue($id: String!) { issue(id: $id) { %s } }' % FIELDS
        issue = self.client.query(q, {'id': ref})['issue']
        if not issue:
            raise ValueError('unreadable issue: ' + ref)
        return self.remember(issue)

    def project_issues(self, project):
        def fetch(cursor, stamps=False):
            q = ('query ProjectIssues($id: String!, $after: String) { project(id: $id) { '
                 'issues(first: %d, after: $after, includeArchived: true) { nodes { %s } %s } } }' %
                 (ISSUE_PAGE, 'id updatedAt' if stamps else FIELDS, PAGE))
            return self.client.query(q, {'id': project, 'after': cursor})['project']['issues']
        return self.collection('project issues', fetch)

    def markers(self, plan):
        def fetch(cursor, stamps=False):
            q = ('query Markers($marker: String!, $after: String) { issues(first: %d, after: $after, '
                 'includeArchived: true, filter: {description: {contains: $marker}}) { nodes { %s } %s } }' %
                 (ISSUE_PAGE, 'id updatedAt' if stamps else FIELDS, PAGE))
            return self.client.query(q, {'marker': 'swarm-unit: ' + plan + '/', 'after': cursor})['issues']
        return self.collection('marker search', fetch)

    def collection(self, label, fetch):
        nodes = self.pages_of(fetch)
        # Each collection owns its first stamps, even when another collection
        # also reads an issue. Never recover these from the shared issue cache.
        self.memberships.append((label, fetch, {i['id']: i['updatedAt'] for i in nodes}))
        return [self.remember(i) for i in nodes]

    def batches(self, ids, fields=None, name='IssueBatch'):
        """Cover issue and nested relation pages; missing ids remain absences."""
        ids = sorted(set(ids))
        for offset in range(0, len(ids), ISSUE_PAGE):
            batch = ids[offset:offset + ISSUE_PAGE]
            q = ('query %s($ids: [ID!]!, $after: String) { issues(filter: {id: {in: $ids}}, '
                 'first: %d, after: $after, includeArchived: true) { nodes { %s } %s } }' %
                 (name, ISSUE_PAGE, fields if fields is not None else FIELDS, PAGE))
            nodes = self.pages_of(lambda after: self.client.query(q, {'ids': batch, 'after': after})['issues'])
            if {n['id'] for n in nodes} - set(batch):
                raise ValueError('issue id disagrees on read-back')
            yield [self.remember(node) for node in nodes] if fields is None else nodes

    def resolve(self, refs, fields=None):
        """Reuse the snapshot, batching only references it does not contain."""
        by_ref = {ref: i for i in self.seen.values() for ref in (i['id'], i['identifier'])}
        missing = sorted(set(refs) - by_ref.keys())
        ids = [ref for ref in missing if not re.fullmatch(LI.IDENTIFIER, ref)]
        for nodes in self.batches(ids, fields):
            for issue in nodes:
                by_ref[issue['id']] = issue
        names = [ref for ref in missing if re.fullmatch(LI.IDENTIFIER, ref)]
        for offset in range(0, len(names), ISSUE_PAGE):
            batch = names[offset:offset + ISSUE_PAGE]
            filters = [{'team': {'key': {'eq': ref.rsplit('-', 1)[0]}},
                        'number': {'eq': int(ref.rsplit('-', 1)[1])}} for ref in batch]
            q = ('query OperationIdentifiers($filter: IssueFilter!, $after: String) { '
                 'issues(filter: $filter, first: %d, after: $after, includeArchived: true) '
                 '{ nodes { %s } %s } }' % (ISSUE_PAGE, FIELDS if fields is None else fields, PAGE))
            nodes = self.pages_of(lambda after: self.client.query(q, {'filter': {'or': filters}, 'after': after})['issues'])
            found = set()
            for node in nodes:
                ref = node['identifier']
                if ref not in batch or ref in found:
                    raise ValueError('issue identifier disagrees on read-back: ' + ref)
                found.add(ref)
                by_ref[ref] = self.remember(node) if fields is None else node
        return {ref: by_ref.get(ref) for ref in sorted(set(refs))}

    def stable(self):
        covered = set()
        for label, fetch, first_stamps in self.memberships:
            covered.update(first_stamps)
            try:
                last = self.pages_of(lambda after: fetch(after, stamps=True))
                if {i['id'] for i in last} != set(first_stamps):
                    self.problems.append('snapshot moved: ' + label)
                self.compare_stamps(last, first_stamps)
            except (API.LinearError, ValueError, TypeError, KeyError) as exc:
                self.problems.append(str(exc))
        remaining = set(self.seen) - covered
        try:
            last = [i for nodes in self.batches(remaining, 'id updatedAt', 'StabilityBatch') for i in nodes]
            self.compare_stamps(last, {iid: self.seen[iid]['updatedAt'] for iid in remaining})
        except (API.LinearError, ValueError, TypeError, KeyError) as exc:
            self.problems.append(str(exc))

    def compare_stamps(self, last, expected):
        by_id = {i['id']: i for i in last}
        for iid, stamp in expected.items():
            if iid not in by_id or by_id[iid].get('updatedAt') != stamp:
                self.problems.append('snapshot moved: ' + iid)


def binding_read(reader, project):
    data = reader.client.query(BINDING_QUERY, {'id': project})
    remote = data['project']
    if not remote:
        return data['viewer']['organization'], None, []
    initial = remote['teams']
    def fetch(cursor):
        if cursor is None:
            return initial
        q = ('query Teams($id: String!, $after: String) { project(id: $id) { '
             'teams(first: 100, after: $after) { nodes { id key name } %s } } }' % PAGE)
        return reader.client.query(q, {'id': project, 'after': cursor})['project']['teams']
    return data['viewer']['organization'], remote, reader.pages_of(fetch)


def completion(issue):
    if issue.get('archivedAt') or issue['state']['type'] in ('canceled', 'duplicate'):
        return 'UNKNOWN'
    return 'completed' if issue['state']['type'] == 'completed' else 'open'


def relationless(issue, has_relation, unit_ids=()):
    """Whether an open project issue needs a relation or independence reason."""
    body = issue.get('description') or ''
    return (completion(issue) == 'open' and issue['id'] not in unit_ids and
            not re.search(r'(?m)^\s*`?swarm-unit: \S+/\S+`?\s*$', body) and
            not has_relation and
            not re.search(r'(?m)^[ \t]*swarm-independent:[ \t]*\S[^\n]*$', body))


def edges_of(issues):
    edges, relations = set(), {}
    for issue in issues:
        neighbors = relations.setdefault(issue['id'], set())
        for field in ('relations', 'inverseRelations'):
            for rel in issue[field]['nodes']:
                a, b = rel['issue']['id'], rel['relatedIssue']['id']
                neighbors.add(b if a == issue['id'] else a)
                if rel['type'] == 'blocks':
                    edges.add((a, b))
    return edges, relations


def cyclic(nodes, edges):
    graph = {n: set() for n in nodes}
    degree = {n: 0 for n in nodes}
    for a, b in edges:
        if a in graph and b in graph and b not in graph[a]:
            graph[a].add(b)
            degree[b] += 1
    ready = [n for n in nodes if degree[n] == 0]
    count = 0
    while ready:
        a = ready.pop()
        count += 1
        for b in graph[a]:
            degree[b] -= 1
            if degree[b] == 0:
                ready.append(b)
    return count != len(nodes)


def prose_candidates(issue, neighbors, identifiers):
    text = re.sub(r'```[\s\S]*?```|`[^`]*`|"[^"\n]*"|“[^”]*”|\'[^\'\n]*\'', '', issue.get('description') or '')
    lines = []
    for line in text.splitlines():
        if re.match(r'^\s*(?:>|not\b|no\b|never\b|does\s+not\b|do\s+not\b|is\s+not\b|historically\b)', line, re.I):
            # End the candidate sentence rather than connecting text across
            # an excluded line. Ordinary soft line wraps stay in the sentence.
            lines.append('.')
        else:
            lines.append(line)
    out = []
    for sentence in re.split(r'(?<=[.!?])\s+', '\n'.join(lines)):
        for match in re.finditer(r'\b(blocked by|depends on|must land before)\b([^.!?]*)', sentence, re.I):
            for ident in re.findall(r'\b[A-Z][A-Z0-9]*-\d+\b', match[2]):
                if identifiers.get(ident, ident) in neighbors:
                    continue
                edge = ([issue['identifier'], ident] if match[1].lower() == 'must land before'
                        else [ident, issue['identifier']])
                out.append({'issue': issue['identifier'], 'sentence': sentence, 'proposed_blocks': edge})
    return out


def read_object(raw, path):
    value = json.loads(raw[str(Path(path).absolute())])
    if not isinstance(value, dict):
        raise ValueError('expected object: ' + str(path))
    return value


def audit(args, client, filing=None):
    started = utc()
    requests_started = client.requests
    paths = TA.source_paths(**sources(args)) if filing is None else []
    raw, inputs = TA.capture(paths)
    plan = (filing['plan'] if filing else read_object(raw, args.plan) if args.plan else None)
    draft = (filing['draft'] if filing else read_object(raw, args.draft) if args.draft else None)
    binding = (filing['binding'] if filing else read_object(raw, args.binding) if args.binding else None)
    if plan is not None:
        require_id(plan.get('name'), 'plan name')
        if not isinstance(plan.get('units'), list):
            raise ValueError('units must be a list')
        for unit in plan['units']:
            if not isinstance(unit.get('needs', []), list):
                raise ValueError('needs must be a list')
    if binding:
        if binding.get('schema_version') != 1:
            raise ValueError('binding schema_version')
        workspace = require_id(binding['workspace']['id'], 'workspace id')
        team = require_id(binding['team']['id'], 'team id')
        project = require_id(binding['project']['id'], 'project id')
    else:
        project = require_id(draft['project']['linear_id'], 'draft project.linear_id')
        # Legacy drafts name a team by exact key or name, not UUID. Resolve
        # once from the bound project's teams; use its id thereafter.
        team_value = draft['project']['team']
        team = team_value.get('id') if isinstance(team_value, dict) else team_value
        workspace = (draft.get('workspace') or {}).get('id')
    scope = {'workspace': workspace or 'unresolved', 'team': team, 'project': project,
             'repository': binding.get('repository') if binding else (plan or {}).get('repository'),
             'plan': plan['name'] if plan else None}
    checks = {cid: {'id': cid, 'verdict': 'CLEAN', 'evidence': []} for cid in sorted(TA.CHECKS)}
    if plan:
        checks['plan_edges'] = {'id': 'plan_edges', 'verdict': 'CLEAN', 'evidence': []}
    if args.state_dir:
        checks['swarm_state'] = {'id': 'swarm_state', 'verdict': 'CLEAN', 'evidence': []}

    def finding(cid, value, evidence):
        check = checks[cid]
        if value == 'UNKNOWN' or check['verdict'] != 'UNKNOWN':
            check['verdict'] = value
        check['evidence'].append(API.redact(evidence, client._key) if isinstance(evidence, str) and value == 'UNKNOWN' else evidence)

    reader = Reader(client)
    try:
        org, remote, teams = binding_read(reader, project)
        if not binding:
            if workspace is None:
                workspace = org['id']
            if not isinstance(team_value, dict):
                matches = [t for t in teams if team_value in (t['id'], t['key'], t.get('name'))]
                if len(matches) == 1:
                    team = matches[0]['id']
            scope.update(workspace=workspace, team=team)
        if org['id'] != workspace or not remote or remote['id'] != project or team not in [t['id'] for t in teams]:
            finding('binding', 'DRIFT', 'workspace, team or project ids disagree')
    except (API.LinearError, ValueError, KeyError, TypeError) as exc:
        finding('binding', 'UNKNOWN', str(exc))
        reader.problems.append(str(exc))
    issues = []
    mapped = {}
    known = []
    if checks['binding']['verdict'] == 'CLEAN':
        try:
            issues = reader.project_issues(project)
        except (API.LinearError, ValueError, KeyError, TypeError) as exc:
            reader.problems.append(str(exc))
        refs = [(i.get('unit'), i['linear_id'] if filing else i['identifier'])
                for i in (draft or {}).get('issues', []) if i.get('linear_id' if filing else 'identifier')]
        if args.state_dir:
            try:
                payload = raw[str((Path(args.state_dir) / 'outbox-receipts.jsonl').absolute())]
                for line in (payload or b'').splitlines():
                    receipt = json.loads(line)
                    refs.append((None, require_id(receipt.get('ref'), 'receipt ref')))
            except (ValueError, TypeError, KeyError) as exc:
                finding('misplaced', 'UNKNOWN', str(exc))
        try:
            resolved = reader.resolve([ref for _, ref in refs])
        except (API.LinearError, ValueError, KeyError, TypeError) as exc:
            resolved = {}
            reader.problems.append(str(exc))
        if filing:
            # A lagging project listing cannot hide a freshly created unit.
            listed = {i['id'] for i in issues}
            issues.extend(i for i in resolved.values() if i is not None and i['id'] not in listed)
        for unit, ref in refs:
            issue = resolved.get(ref)
            if issue is not None:
                known.append(issue)
                if unit:
                    mapped[unit] = issue
            else:
                error = 'issue not found or unreadable: ' + ref
                finding('misplaced', 'UNKNOWN', error)
                reader.problems.append(error)
                if unit and plan:
                    finding('plan_edges', 'UNKNOWN', {'unit': unit, 'error': error})
        marker_plan = plan['name'] if plan else (draft or {}).get('project', {}).get('slug')
        if marker_plan:
            try:
                seen_before = set(reader.seen)
                found = reader.markers(marker_plan)
                if filing:
                    found = [i for i in found if any(LF.has_markers(i.get('description'),
                             {'swarm-unit': marker_plan + '/' + unit['id'],
                              'swarm-repo': scope['repository']}) for unit in plan['units'])]
                    keep = {i['id'] for i in found} | seen_before
                    reader.seen = {k: v for k, v in reader.seen.items() if k in keep}
                known.extend(found)
                for issue in found:
                    for unit in (plan or {}).get('units', []):
                        marker = 'swarm-unit: ' + marker_plan + '/' + unit['id']
                        matches = (LF.markers(issue.get('description'), 'swarm-unit') ==
                                   [marker_plan + '/' + unit['id']] if filing else
                                   re.search(r'(?m)^\s*`?' + re.escape(marker) + r'`?\s*$', issue.get('description') or ''))
                        if matches:
                            if unit['id'] in mapped and mapped[unit['id']]['id'] != issue['id']:
                                finding('plan_edges', 'UNKNOWN', {'unit': unit['id'], 'error': 'multiple issue mappings'})
                            else:
                                mapped[unit['id']] = issue
            except (API.LinearError, ValueError, KeyError, TypeError) as exc:
                reader.problems.append(str(exc))
                finding('misplaced', 'UNKNOWN', str(exc))
        for issue in known:
            if (issue.get('project') or {}).get('id') != project:
                finding('misplaced', 'DRIFT', issue['identifier'])
        try:
            if filing:
                LI.expand_scope(sys.modules[__name__], reader)
            edges, relations = edges_of(list(reader.seen.values()))
            project_ids = {i['id'] for i in issues}
            blockers = {a for a, b in edges if b in project_ids and a not in reader.seen}
            try:
                resolved = reader.resolve(blockers)
                absent = sorted(ref for ref, issue in resolved.items() if issue is None)
                if absent:
                    raise ValueError('unreadable blockers: ' + ', '.join(absent))
            except (API.LinearError, ValueError, KeyError, TypeError) as exc:
                reader.problems.append(str(exc))
                if any(i['state']['type'] == 'started' and any(b == i['id'] and a in blockers for a, b in edges)
                       for i in issues):
                    finding('blocked_in_progress', 'UNKNOWN', str(exc))
            identifiers = {i['identifier']: i['id'] for i in reader.seen.values()}
            for issue in reader.seen.values():
                for field in ('relations', 'inverseRelations'):
                    for relation in issue[field]['nodes']:
                        for endpoint in ('issue', 'relatedIssue'):
                            peer = relation[endpoint]
                            identifiers[peer['identifier']] = peer['id']
            unit_ids = {i['id'] for i in mapped.values()}
            # Draft mappings exempt units even when no plan was supplied.
            checked_issues = ([i for i in reader.seen.values() if i['id'] in filing['checked']]
                              if filing else issues)
            for issue in checked_issues:
                iid = issue['id']
                problem = LI.declared_edges(issue)
                if problem:
                    finding('declared_edges', 'DRIFT', problem)
                if relationless(issue, relations.get(iid), unit_ids):
                    finding('relationless', 'DRIFT', issue['identifier'])
                candidates = prose_candidates(issue, relations.get(iid, set()), identifiers)
                if candidates:
                    finding('prose_dependency', 'ADVISORY', candidates)
                if issue['state']['type'] == 'started':
                    for a, b in edges:
                        if b != iid or a not in reader.seen:
                            continue
                        status = completion(reader.seen[a])
                        if status != 'completed':
                            finding('blocked_in_progress', 'UNKNOWN' if status == 'UNKNOWN' else 'DRIFT',
                                    {'issue': issue['identifier'], 'blocker': reader.seen[a]['identifier']})
            if cyclic(set(reader.seen) if filing else {i['id'] for i in issues}, edges):
                finding('cycle', 'DRIFT', 'blocks cycle in project')
            if plan:
                # A filed reference that could not be read is UNKNOWN, not
                # evidence that the unit has no filed issue. Markers count too.
                filed_units = set(mapped) | {unit for unit, ref in refs if unit}
                expected = set()
                for unit in plan['units']:
                    uid = unit['id']
                    if uid not in filed_units:
                        finding('plan_edges', 'DRIFT', {'unit': uid, 'error': 'no filed issue'})
                    for dep in unit.get('needs', []):
                        unmapped = sorted({dep, uid} - filed_units)
                        if unmapped:
                            finding('plan_edges', 'DRIFT', {'edge': [dep, uid],
                                    'unmapped': unmapped, 'error': 'no filed issue'})
                        elif dep in mapped and uid in mapped:
                            expected.add((mapped[dep]['id'], mapped[uid]['id']))
                actual = {(a, b) for a, b in edges if a in unit_ids and b in unit_ids}
                for edge in sorted(expected - actual):
                    finding('plan_edges', 'DRIFT', {'missing': list(edge)})
                for edge in sorted(actual - expected):
                    finding('plan_edges', 'DRIFT', {'extra': list(edge)})
        except (ValueError, TypeError, KeyError) as exc:
            reader.problems.append(str(exc))
    else:
        # A failed binding is itself conclusive drift/unknown; no other
        # remote data is trusted. Coverage explicitly records the skipped read.
        reader.problems.append('issue read withheld because binding did not match')
    if filing is None:
        # Legacy drafts learn the workspace from the reader. Capture their local
        # operation inputs too, and let section reuse that resolved scope.
        op_paths = TA.source_paths(operation_scope=scope)
        for path in op_paths:
            if path not in paths:
                paths.append(path)
                added_raw, added_inputs = TA.capture([path])
                raw.update(added_raw)
                inputs.update(added_inputs)
        try:
            comments = project_comments(reader, workspace, project, [i['id'] for i in issues])
            for issue in issues:
                latest = latest_state(comments[issue['id']])
                if latest and issue['state']['type'] != STATE_TYPE[latest['op']]:
                    finding('intent_order', 'DRIFT', {'issue': issue['identifier'],
                            'latest': latest['key'], 'expected': STATE_TYPE[latest['op']],
                            'actual': issue['state']['type']})
        except (API.LinearError, ValueError, TypeError, KeyError) as exc:
            finding('intent_order', 'UNKNOWN', str(exc))
            reader.problems.append(str(exc))
        if args.state_dir:
            try:
                if raw[str((Path(args.state_dir) / 'swarm-state.json').absolute())] is None:
                    raise ValueError('coordinator state absent')
                project_dir = os.environ.get('HANIG_PROJECT_DIR') or Path(__file__).parents[1]
                swarm_dir = skill_paths.sibling_skill_root(project_dir, 'hanig-project', 'hanig-swarm')
                env = {k: v for k, v in os.environ.items() if k != API.KEY_ENV}
                result = subprocess.run([sys.executable, str(swarm_dir / 'scripts' / 'swarm.py'),
                                         'status', args.plan, '--state-dir', args.state_dir, '--json'],
                                        env=env, capture_output=True, text=True, timeout=60)
                if result.returncode:
                    raise ValueError('coordinator status failed: ' + result.stderr)
                for row in json.loads(result.stdout)['units']:
                    if row['id'] not in mapped:
                        continue
                    issue_state = mapped[row['id']]['state']['type']
                    state = row['state']
                    mismatch = ((state == 'DONE' and issue_state != 'completed') or
                                (state in ('RUNNING', 'SUBMITTED') and issue_state != 'started') or
                                (state in ('FAILED', 'FAILED_EVIDENCE', 'HELD', 'NEEDS_HUMAN') and issue_state == 'completed'))
                    if mismatch:
                        finding('swarm_state', 'DRIFT', {'unit': row['id'], 'coordinator': state, 'issue': issue_state})
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                finding('swarm_state', 'UNKNOWN', str(exc))
                reader.problems.append(str(exc))
        try:
            for problem in LI.incomplete_operations(workspace, project):
                finding('op_incomplete', 'DRIFT', problem)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            finding('op_incomplete', 'UNKNOWN', str(exc))
    reader.stable()
    # Catch local writes during the read, including status-side observations.
    try:
        if filing is None and TA.current_inputs(**sources(args), operation_scope=scope) != inputs:
            reader.problems.append('local inputs changed during read')
    except OSError as exc:
        reader.problems.append(str(exc))
    if reader.problems:
        finding('coverage', 'UNKNOWN', [API.redact(p, client._key) for p in reader.problems])
        # A check that saw an incomplete read has not established CLEAN.
        for cid, check in checks.items():
            if cid != 'coverage' and check['verdict'] == 'CLEAN':
                finding(cid, 'UNKNOWN', 'not evaluated over a complete read')
    records = list(checks.values())
    result = {'schema_version': 1, 'verdict': TA.verdict(records), 'read_started': started,
            'read_finished': utc(), 'scope': scope, 'inputs': inputs,
            'coverage': {'complete': not reader.problems, 'pages': reader.pages, 'issues': len(issues),
                         'requests': client.requests - requests_started},
            'checks': records}
    return (result, reader) if filing else result


# State is reconciled once per issue. Comments, including those from other
# hosts, are the eventually visible ordering journal; there is no CAS or loop.
STATE_TYPE = {'start': 'started', 'close': 'completed', 'reopen': 'unstarted'}
OPERATIONS = set(STATE_TYPE) | {'note', 'block', 'open_pr'}
COMMENT_FIELDS = 'id body issue { id }'
DRAIN_ISSUE_FIELDS = 'id identifier state { type } project { id } team { id }'


def intent_order(at, key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9._:-]+', key):
        raise ValueError('key not drainable')
    require_id(at, 'intent at')
    try:
        instant = dt.datetime.strptime(at, '%Y-%m-%dT%H:%M:%S%z')
    except ValueError:
        instant = dt.datetime.fromisoformat(at.replace('Z', '+00:00'))
    if instant.utcoffset() is None:
        raise ValueError('intent at requires a timezone')
    return instant, key


def comment_id(workspace, project, issue, key):
    return API.derived_id('comment:%s/%s/%s/%s' % (workspace, project, issue, key))


def intent_markers(intent):
    return {'intent': intent['key'], 'evidence': intent['envelope']['evidence_digest'],
            'order': '%s %s %s' % (intent['at'], intent['key'], intent['verb'])}


def comment_body(intent):
    lines = ['swarm `%s` for unit `%s`: %s' %
             (intent['verb'], intent['unit'], intent.get('why') or intent['verb']),
             '', '- unit state: `%s`' % intent.get('unit_state'),
             '- attempt: `%s`' % intent['envelope']['attempt']['id'],
             '- evidence digest: `%s`' % intent['envelope']['evidence_digest'], '']
    lines.extend('`swarm-%s: %s`' % pair for pair in intent_markers(intent).items())
    return '\n'.join(lines)


def comment_markers(comment):
    """Read only the fixed trailer; marker-like prose above it is free text."""
    body = comment.get('body') or ''
    trailer = [line for line in body.splitlines() if line.strip()][-3:]
    if len(trailer) != 3:
        return None
    values = {}
    for name, line in zip(('intent', 'evidence', 'order'), trailer):
        match = re.fullmatch(r'`swarm-' + name + r': ([^`\r\n]+)`', line)
        if not match:
            return None
        values[name] = match[1]
    return values


def genuine_comment(comment, workspace, project, issue):
    """Ignore copies and malformed/edited markers; compare ids exactly."""
    markers = comment_markers(comment)
    if not markers or (comment.get('issue') or {}).get('id') != issue:
        return None
    key = markers['intent']
    if comment.get('id') != comment_id(workspace, project, issue, key):
        return None
    if not re.fullmatch('[0-9a-f]{64}', markers['evidence']):
        return None
    try:
        at, order_key, op = markers['order'].rsplit(' ', 2)
        if order_key != key or op not in OPERATIONS:
            return None
        order = intent_order(at, key)
    except (ValueError, TypeError):
        return None
    return {'key': key, 'op': op, 'order': order, 'markers': markers}


def issue_comments(reader, workspace, project, issue, initial=None):
    q = ('query IntentComments($id: String!, $after: String) { issue(id: $id) { '
         'comments(first: 100, after: $after) { nodes { %s } %s } } }' % (COMMENT_FIELDS, PAGE))
    nodes = reader.pages_of(lambda after: initial if after is None and initial is not None else
                            reader.client.query(q, {'id': issue, 'after': after})['issue']['comments'])
    return [parsed for c in nodes
            for parsed in [genuine_comment(c, workspace, project, issue)] if parsed]


def project_comments(reader, workspace, project, ids):
    fields = 'id comments(first: 20) { nodes { %s } %s }' % (COMMENT_FIELDS, PAGE)
    result = {}
    for nodes in reader.batches(ids, fields, 'CommentBatch'):
        for node in nodes:
            result[node['id']] = issue_comments(reader, workspace, project, node['id'], node['comments'])
    if set(result) != set(ids):
        raise ValueError('incomplete issue comment listing')
    return result


def latest_state(comments):
    return max((c for c in comments if c['op'] in STATE_TYPE),
               key=lambda c: c['order'], default=None)


def read_drain_issue(client, ref):
    q = 'query DrainIssue($id: String!) { issue(id: $id) { %s } }' % DRAIN_ISSUE_FIELDS
    issue = client.query(q, {'id': ref})['issue']
    if not issue:
        raise ValueError('unreadable issue: ' + ref)
    require_id(issue.get('id'), 'issue id')
    require_id(issue.get('identifier'), 'issue identifier')
    return issue


def check_membership(issue, project, team):
    if ((issue.get('project') or {}).get('id') != project or
            (issue.get('team') or {}).get('id') != team):
        raise ValueError('issue outside the bound project')


def read_comment(client, cid):
    # `comment(id:)` answers an absent id with an "Entity not found" error,
    # measured live on 2026-10-05; the filtered list answers it with no nodes.
    nodes = client.query('query IntentComment($id: ID!) { comments(filter: {id: {eq: $id}}, first: 1) '
                         '{ nodes { %s } } }' % COMMENT_FIELDS, {'id': cid})['comments']['nodes']
    return nodes[0] if nodes else None


def confirm_comment(client, intent, workspace, project, issue, create):
    cid = comment_id(workspace, project, issue, intent['key'])
    comment = read_comment(client, cid)
    create_error = None
    if comment is None and create:
        try:
            client.query('mutation IntentCommentCreate($input: CommentCreateInput!) { '
                         'commentCreate(input: $input) { success } }',
                         {'input': {'id': cid, 'issueId': issue, 'body': comment_body(intent)}})
        except API.LinearError as exc:
            # A rejection or ambiguous response counts only if the exact
            # comment is subsequently read back on the intended issue.
            create_error = exc
        try:
            comment = read_comment(client, cid)
        except (API.LinearError, OSError, ValueError, KeyError, TypeError):
            if create_error is not None:
                raise create_error from None
            raise
    if comment is None and not create:
        return None
    parsed = genuine_comment(comment, workspace, project, issue) if comment else None
    if not parsed or parsed['markers'] != intent_markers(intent):
        if create_error is not None:
            raise create_error
        raise ValueError('comment missing or markers disagree on read-back')
    return parsed


def drain_identity(args, reader):
    path = Path(args.binding or args.draft)
    raw = path.read_bytes()
    config = json.loads(raw)
    if args.binding:
        if type(config.get('schema_version')) is not int or config['schema_version'] != 1:
            raise ValueError('binding schema_version')
        workspace = require_id(config['workspace']['id'], 'workspace id')
        team = require_id(config['team']['id'], 'team id')
        project = require_id(config['project']['id'], 'project id')
    else:
        project = require_id(config['project']['linear_id'], 'draft project.linear_id')
        require_id(config['project'].get('slug'), 'draft project.slug')
        team_value = config['project']['team']
        team = team_value.get('id') if isinstance(team_value, dict) else team_value
        workspace = (config.get('workspace') or {}).get('id')
    org, remote, teams = binding_read(reader, project)
    if not args.binding:
        workspace = org['id'] if workspace is None else workspace
        if not isinstance(team_value, dict):
            matches = [t for t in teams if team_value in (t['id'], t['key'], t.get('name'))]
            if len(matches) == 1:
                team = matches[0]['id']
    if org['id'] != workspace or not remote or remote['id'] != project or team not in [t['id'] for t in teams]:
        raise ValueError('workspace, team or project ids disagree')
    return path, raw, config, workspace, project, team


def drain_lock_path(workspace, project):
    # UUIDs in production; restrict path components without rewriting ids.
    if any(not re.fullmatch('[A-Za-z0-9-]+', v) for v in (workspace, project)):
        raise ValueError('invalid workspace or project lock id')
    return Path.home() / '.local/state/hanig-swarm' / ('linear-drain-%s-%s.lock' % (workspace, project))


@contextmanager
def project_lock(workspace, project):
    """The same per-project, per-host flock for every Linear writer."""
    path = drain_lock_path(workspace, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('another writer holds the project lock; nothing was applied')
        yield


def collect_intents(directories, contract, report):
    entries = []
    for directory in dict.fromkeys(directories):
        try:
            status, problems = contract.contract.acknowledgment_status(directory)
            if problems:
                raise ValueError('receipt journal incomplete: %s' % problems)
            path = Path(directory) / contract.OUTBOX
            lines = path.read_text().splitlines() if path.exists() else []
        except Exception as exc:
            report('%s: %s' % (directory, exc))
            continue
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            label = '%s line %d' % (directory, number)
            try:
                intent = contract.normalize_intent(json.loads(line))
                order = intent_order(intent.get('at'), intent.get('key'))
                problems = contract.validate_intent(intent)
                if problems:
                    raise ValueError('; '.join(problems))
                label += ' key=' + intent['key']
                if intent['verb'] == 'close':
                    evidence = intent.get('evidence')
                    receipt = evidence.get('receipt') if isinstance(evidence, dict) else None
                    if not isinstance(receipt, dict) or not receipt:
                        raise ValueError('close evidence carries no receipt')
                    if intent.get('closing_evidence') == 'merged_pr':
                        problem = contract.contract._merge_shape_problem(receipt)
                        if problem or receipt.get('unit') != intent['unit']:
                            raise ValueError('merge receipt: ' + (problem or 'names another unit'))
                grade = status.get(intent['key'], (contract.contract.UNACKNOWLEDGED, []))[0]
                if grade == contract.contract.CONFLICT:
                    raise ValueError('conflicting acknowledgments')
                entries.append({'intent': intent, 'directory': directory, 'label': label, 'order': order,
                                'receipted': grade != contract.contract.UNACKNOWLEDGED})
            except Exception as exc:
                report('%s: %s' % (label, exc))
    return sorted(entries, key=lambda e: e['order'])


def record_drain_receipt(entry, issue, contract):
    intent = entry['intent']
    envelope = intent['envelope']
    observation = {k: envelope[k] for k in ('project', 'unit', 'attempt', 'idempotency_key',
                                           'requested_operation', 'evidence_digest')}
    observation.update(schema_version=contract.OBSERVATION_SCHEMA_VERSION,
                       connector_capability=envelope['required_connector_capability'],
                       outcome=contract.CONFIRMED_BY_READBACK, source=contract.RECEIVER_READBACK,
                       matched=True, reference=issue['identifier'], by='linear_sync.py drain', at=utc())
    with tempfile.TemporaryDirectory(prefix='linear-drain-') as tmp:
        ipath, opath = Path(tmp) / 'intent.json', Path(tmp) / 'observation.json'
        ipath.write_text(json.dumps(intent))
        opath.write_text(json.dumps(observation))
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('drain_contract.py')),
                                 'reconcile', '--state-dir', entry['directory'], '--intent', str(ipath),
                                 '--observation', str(opath)],
                                env={k: v for k, v in os.environ.items() if k != API.KEY_ENV},
                                capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('receipt refused: ' + result.stderr)


def reconcile_issue(reader, workspace, project, team, issue, entries, dry_run):
    comments = issue_comments(reader, workspace, project, issue['id'])
    expected = {e['intent']['key']: intent_markers(e['intent']) for e in entries}
    for comment in comments:
        if comment['key'] in expected and comment['markers'] != expected[comment['key']]:
            raise ValueError('listed comment disagrees with collected intent')
    comments += [e['confirmed'] for e in entries if 'confirmed' in e]
    if dry_run:
        comments += [{'key': e['intent']['key'], 'op': e['intent']['verb'], 'order': e['order']}
                     for e in entries if not e['receipted'] and not e.get('error')]
    latest = latest_state(comments)
    before = read_drain_issue(reader.client, issue['id'])
    if before['id'] != issue['id']:
        raise ValueError('issue identity changed')
    check_membership(before, project, team)
    if latest and before['state']['type'] != STATE_TYPE[latest['op']]:
        target_type = STATE_TYPE[latest['op']]
        q = ('query DrainStates($id: String!, $after: String) { team(id: $id) { '
             'states(first: 100, after: $after) { nodes { id type position } %s } } }' % PAGE)
        states = reader.pages_of(lambda after: reader.client.query(q, {'id': team, 'after': after})['team']['states'])
        # "First" is the lowest position, which is the order Linear shows a
        # team's workflow; API page order is not guaranteed to match it.
        target = min((s for s in states if s['type'] == target_type),
                     key=lambda s: s['position'], default=None)
        if target is None:
            raise ValueError('team has no ' + target_type + ' state')
        if not dry_run:
            reader.client.query('mutation DrainState($id: String!, $state: String!) { '
                                'issueUpdate(id: $id, input: {stateId: $state}) { success } }',
                                {'id': issue['id'], 'state': target['id']})
    after = read_drain_issue(reader.client, issue['id'])
    check_membership(after, project, team)
    if after['id'] != issue['id'] or (latest and not dry_run and after['state']['type'] != STATE_TYPE[latest['op']]):
        raise ValueError('state read-back does not show latest target')
    return latest, after


def drain(args, client):
    import drain_contract as contract

    reader = Reader(client)
    path, raw, config, workspace, project, team = drain_identity(args, reader)
    with project_lock(workspace, project):
        # Identity must be read to locate the lock. All actual input collection
        # follows acquisition, with the routing bytes checked again here.
        if path.read_bytes() != raw:
            raise ValueError('binding or draft changed while acquiring lock')
        return drain_locked(args, reader, config, workspace, project, team, contract)


def drain_locked(args, reader, config, workspace, project, team, contract):
    failures, acknowledged, superseded = [], [], []

    def say(message):
        print(API.redact(message, reader.client._key))

    def fail(message):
        failures.append(message)
        say('unacknowledged/UNKNOWN ' + message)

    entries = collect_intents(args.state_dir, contract, fail)
    identifiers = {}
    if args.draft:
        for item in config.get('issues', []):
            if item.get('identifier'):
                if item['unit'] in identifiers:
                    raise ValueError('duplicate unit in draft')
                identifiers[item['unit']] = item['identifier']
    references = {}
    for entry in entries:
        intent = entry['intent']
        try:
            if args.draft:
                if intent['project'] == 'swarm' or intent['project'] != config['project']['slug']:
                    raise ValueError('intent project does not match draft (nameless plans require binding)')
                ref = identifiers.get(intent['unit'])
                if 'tracker' in intent and intent['tracker'] != ref:
                    raise ValueError('tracker disagrees with draft identifier')
            else:
                ref = intent.get('tracker')
            if not ref:
                raise ValueError('no tracker issue')
            entry['ref'] = ref
            references[ref] = None
        except Exception as exc:
            entry['error'] = str(exc)
            fail(entry['label'] + ': ' + str(exc))
    try:
        references = reader.resolve(references, fields=DRAIN_ISSUE_FIELDS)
    except Exception as exc:
        fail('issue references: ' + str(exc))
    groups = {}
    for entry in entries:
        if entry.get('error'):
            continue
        intent = entry['intent']
        try:
            issue = references[entry['ref']]
            if issue is None:
                raise ValueError('unreadable issue: ' + entry['ref'])
            check_membership(issue, project, team)
            group = groups.setdefault(issue['id'], (issue, []))
            group[1].append(entry)
            try:
                confirmed = confirm_comment(reader.client, intent, workspace, project, issue['id'],
                                            not entry['receipted'] and not args.dry_run)
                if confirmed:
                    entry['confirmed'] = confirmed
                elif entry['receipted']:
                    say('%s: uncovered history (comment missing); reconciling from existing comments' % issue['identifier'])
                elif args.dry_run:
                    say('would post comment for ' + entry['label'])
            except Exception as exc:
                if entry['receipted']:
                    if isinstance(exc, ValueError):
                        say('%s: uncovered history (%s); reconciling from existing comments' % (issue['identifier'], exc))
                    else:
                        fail(entry['label'] + ': comment read UNKNOWN: ' + str(exc))
                else:
                    raise
        except Exception as exc:
            entry['error'] = str(exc)
            fail(entry['label'] + ': ' + str(exc))
    for issue, group in groups.values():
        try:
            latest, after = reconcile_issue(reader, workspace, project, team, issue, group, args.dry_run)
            say('%s: %s%s' % (issue['identifier'], 'would reconcile to ' if args.dry_run else 'reconciled to ',
                             STATE_TYPE[latest['op']] if latest else 'existing state (no state intent)'))
        except Exception as exc:
            fail('%s reconciliation: %s' % (issue['identifier'], exc))
            latest, after = None, None
        # A pending state intent that failed leaves this issue's intended
        # state unresolved, so nothing on the issue is acknowledged this run.
        unresolved = next((e['label'] for e in group if e.get('error') and not e['receipted']
                           and e['intent'].get('verb') in STATE_TYPE), None)
        for entry in group:
            if entry['receipted'] or entry.get('error'):
                continue
            intent = entry['intent']
            if after is None:
                fail(entry['label'] + ': reconciliation UNKNOWN')
                continue
            if unresolved:
                fail('%s: receipt withheld; state intent %s on this issue failed' % (entry['label'], unresolved))
                continue
            if intent['verb'] in STATE_TYPE:
                if latest and latest['key'] != intent['key']:
                    superseded.append(entry['label'])
                    say('%s: superseded by %s' % (entry['label'], latest['key']))
                    continue
            if args.dry_run:
                fail(entry['label'] + ': dry-run; no receipt recorded')
                continue
            if 'confirmed' not in entry:
                fail(entry['label'] + ': comment not confirmed')
                continue
            try:
                record_drain_receipt(entry, after or issue, contract)
                acknowledged.append(entry['label'])
                say('acknowledged ' + entry['label'])
            except Exception as exc:
                fail(entry['label'] + ': ' + str(exc))
    say('%d acknowledged, %d superseded, %d failures' % (len(acknowledged), len(superseded), len(failures)))
    return 3 if failures else 0


def sources(args):
    return {k: getattr(args, k, None) for k in ('binding', 'draft', 'plan', 'state_dir')}


def parse_args(argv=None):
    """Parse and validate command syntax without credentials, I/O or dispatch."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    bind = sub.add_parser('bind')
    bind.add_argument('--project', required=True)
    bind.add_argument('--repository', required=True)
    for command in ('audit', 'section'):
        p = sub.add_parser(command)
        group = p.add_mutually_exclusive_group(required=command == 'audit')
        group.add_argument('--binding')
        group.add_argument('--draft')
        p.add_argument('--plan')
        p.add_argument('--state-dir')
        p.add_argument('--out' if command == 'audit' else '--audit', required=command == 'section')
    p = sub.add_parser('drain')
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--binding')
    group.add_argument('--draft')
    p.add_argument('--state-dir', action='append', required=True)
    p.add_argument('--dry-run', action='store_true')
    LI.add_parser(sub)
    LF.add_parser(sub)
    args = parser.parse_args(argv)
    if getattr(args, 'plan', None) and not args.draft:
        parser.error('--plan requires --draft')
    if args.command != 'drain' and getattr(args, 'state_dir', None) and not args.plan:
        parser.error('--state-dir requires --plan and --draft')
    return args


def main(argv=None):
    args = parse_args(argv)
    key = None  # Validation can refuse before credentials are loaded.
    try:
        if args.command == 'section':
            print(TA.section(args.audit, **sources(args)))
            return 0
        if args.command == 'issue':
            LI.validate_request(args)
        key = API.load_key()
        client = API.Client(key)
        if args.command in ('file', 'replay'):
            return LF.run(args, client, sys.modules[__name__])
        if args.command == 'issue':
            return LI.run(args, client, sys.modules[__name__])
        if args.command == 'bind':
            root = next((p for p in (Path.cwd(), *Path.cwd().parents) if (p / '.git').exists()), None)
            if root is None:
                raise ValueError('bind requires a Git checkout')
            org, project, teams = binding_read(Reader(client), args.project)
            if not project or project['id'] != args.project or len(teams) != 1:
                raise ValueError('binding requires an exact project id with one team')
            path = root / '.hanig/linear-binding.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            value = {'schema_version': 1, 'repository': args.repository, 'workspace': org,
                     'team': teams[0], 'project': {'id': project['id'], 'name': project['name']}}
            path.write_text(json.dumps(value, indent=2) + '\n')
            print(str(path))
            return 0
        if args.command == 'drain':
            return drain(args, client)
        if args.out:
            for source in TA.source_paths(**sources(args)):
                if (str(Path(args.out).absolute()) == source or
                        (Path(args.out).exists() and Path(source).exists() and
                         os.path.samefile(args.out, source))):
                    raise ValueError('--out cannot overwrite an audit input')
        record = audit(args, client)
        payload = API.redact(json.dumps(record, indent=2) + '\n', key)
        if args.out:
            Path(args.out).write_text(payload)
        print(payload, end='')
        return {'CLEAN': 0, 'DRIFT': 1, 'UNKNOWN': 3}[record['verdict']]
    except (API.LinearError, OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as exc:
        message = API.redact('error: ' + str(exc), key)
        if args.command == 'issue':
            message = ' '.join(message.splitlines())
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
