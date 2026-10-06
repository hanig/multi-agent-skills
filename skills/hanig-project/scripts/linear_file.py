"""File an approved draft using the shared reader, lock and operation engine."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

import linear_api as API
import linear_issue as LI
import tickets as T
import tracker_audit as TA

# Linear refuses 50 projects x 100 nested teams as too complex; 50 x 20 was
# accepted live on 2026-10-06. Team overflow is paged by a follow-up query.
PROJECT_TEAM_PAGE = 20
PROJECT_FIELDS = ('id name description content url teams(first: %d) { nodes { id key name } %%s }'
                  % PROJECT_TEAM_PAGE)


def add_parser(sub):
    p = sub.add_parser('file')
    p.add_argument('--draft', required=True)
    p.add_argument('--adopt-checked', action='store_true')
    p.add_argument('--preview', action='store_true')
    p = sub.add_parser('replay')
    p.add_argument('operation_id')
    p.add_argument('--draft', required=True)


def identity_block(body):
    """Parse one complete terminal identity, above any PR 3 trailer.

    Projects require plan/repo; units require unit/repo with an optional
    body digest for legacy bindings. Whole lines may be plain or backticked,
    in any order, with blank separators. Adjacent deps-by provenance is
    preserved only when a PR 3 trailer is present. Partial blocks are prose.
    """
    body = body or ''
    mark = LI.trailer(body)
    prefix = mark['prefix'] if mark else body
    lines = [m for m in re.finditer(r'[^\r\n]+', prefix) if m[0].strip()]
    end = len(prefix)
    provenance = r'`swarm-deps-by: [^`\r\n]+`'
    while mark and lines and re.fullmatch(provenance, lines[-1][0]):
        end = lines.pop().start()
    for count, names in ((3, {'swarm-unit', 'swarm-repo', 'swarm-body'}),
                         (2, {'swarm-unit', 'swarm-repo'}),
                         (2, {'swarm-plan', 'swarm-repo'})):
        if len(lines) < count:
            continue
        values = {}
        for line in lines[-count:]:
            parsed = re.fullmatch(r'(`?)(swarm-(?:unit|plan|repo|body)): ([^`\r\n]+)\1', line[0])
            if parsed:
                values[parsed[2]] = parsed[3]
        if set(values) == names:
            start = lines[-count].start()
            before = lines[:-count]
            while mark and before and re.fullmatch(provenance, before[-1][0]):
                start = before.pop().start()
            return {'start': start, 'end': end, 'values': values}
    return {'start': end, 'end': end, 'values': {}}


def markers(body, name):
    values = identity_block(body)['values']
    return [values[name]] if name in values else []


def has_markers(body, expected):
    values = identity_block(body)['values']
    return bool(expected) and all(values.get(k) == v for k, v in expected.items())


def identity_lines(body, expected, existing=True):
    """Write terminal identity, preserving project prose and PR 3 bytes."""
    body = body or ''
    block = identity_block(body)
    values = block['values'] if existing else {}
    for name, value in values.items():
        if name not in expected or (name != 'swarm-body' and value != expected[name]):
            raise ValueError('conflicting ' + name + ' marker')
    if existing and has_markers(body, expected):
        return body
    end = block['end']
    prefix = body[:end]
    if values and 'swarm-body' in values:
        start = block['start']
        prefix = prefix[:start] + re.sub(
            r'(?m)^(`?swarm-body: )[^\r\n`]+(`?\r?)$',
            lambda m: m[1] + expected['swarm-body'] + m[2], prefix[start:])
    for name, value in expected.items():
        if name not in values:
            prefix += ('\n' if prefix and not prefix.endswith('\n') else '') + name + ': ' + value + '\n'
    return prefix + body[end:]


def approved_body(body, approved):
    """Replace issue prose after adding identity, keeping identity and provenance bytes."""
    start = identity_block(body)['start']
    return approved + ('\n' if approved and not approved.endswith('\n') else '') + body[start:]


def validate(draft, key):
    LI.reject_key(draft, key)
    project = draft['project']
    for name in ('name', 'slug', 'repository'):
        LI.line_value(project.get(name), 'project.' + name)
    if not re.fullmatch(r'[^/\s]+(?:/[^/\s]+)+', project['repository']):
        raise ValueError('project.repository must be a forge path')
    if '/' in project['slug']:
        raise ValueError('project.slug must not contain /')
    for name in ('summary', 'description'):
        if not isinstance(project.get(name), str):
            raise ValueError('project.' + name + ' must be text')
    approval = draft.get('approval') or {}
    if approval.get('state') not in ('granted', 'autopilot'):
        raise ValueError('draft approval required')
    LI.line_value(approval.get('granted_by'), 'approver', code_span=True)
    if not isinstance(draft['issues'], list):
        raise ValueError('issues must be a list')
    units = set()
    for issue in draft['issues']:
        LI.line_value(issue.get('unit'), 'unit')
        LI.line_value(issue.get('title'), 'title')
        if '/' in issue['unit'] or issue['unit'] in units:
            raise ValueError('duplicate unit or unit containing /')
        units.add(issue['unit'])
        if not isinstance(issue.get('body'), str) or not isinstance(issue.get('blocked_by'), list):
            raise ValueError('body must be text and blocked_by must be a list')
        if any(not isinstance(v, str) for v in issue['blocked_by']):
            raise ValueError('blocked_by must contain unit ids')
    for issue in draft['issues']:
        if set(issue['blocked_by']) - units:
            raise ValueError('blocked_by names a unit outside this draft')
    digest = T.content_digest(draft)
    if (approval.get('state') != 'autopilot' or 'content_digest' in approval) and approval.get('content_digest') != digest:
        raise ValueError('approval content_digest missing or changed; re-approve the draft')
    return digest


def workspace_team(client, sync, value):
    reader = sync.Reader(client)
    org = []
    def fetch(after):
        q = ('query FilingIdentity($after: String) { viewer { organization { id name } } '
             'teams(first: 100, after: $after) { nodes { id key name } %s } }' % sync.PAGE)
        data = client.query(q, {'after': after})
        org.append(data['viewer']['organization'])
        return data['teams']
    teams = reader.pages_of(fetch)
    ref = value.get('id') if isinstance(value, dict) else value
    matches = [t for t in teams if ref in (t['id'], t['key'], t['name'])]
    if len(matches) != 1 or any(o['id'] != org[0]['id'] for o in org):
        raise ValueError('workspace or team ambiguous')
    return org[0], matches[0]


def projects(client, sync, field, value):
    reader = sync.Reader(client)
    query = ('query FilingProjects($filter: ProjectFilter!, $after: String) { '
             'projects(filter: $filter, first: 50, after: $after, includeArchived: true) '
             '{ nodes { %s } %s } }' % (PROJECT_FIELDS % sync.PAGE, sync.PAGE))
    rows = reader.pages_of(lambda after: client.query(query, {
        'filter': {field: {'eq': value}}, 'after': after})['projects'])
    if any(p[field] != value for p in rows):
        raise ValueError('project filter disagrees on read-back')
    for p in rows:
        initial = p['teams']
        def fetch(after):
            if after is None:
                return initial
            q = ('query Teams($id: String!, $after: String) { project(id: $id) { '
                 'teams(first: 100, after: $after) { nodes { id key name } %s } } }' % sync.PAGE)
            return client.query(q, {'id': p['id'], 'after': after})['project']['teams']
        p['teams'] = {'nodes': reader.pages_of(fetch)}
    return rows


def project_at(client, sync, iid):
    rows = projects(client, sync, 'id', iid)
    return rows[0] if rows else None


def project_values(project):
    return {k: project.get(k) or '' for k in ('name', 'description', 'content')}


def identity_values(body):
    values = identity_block(body)['values']
    return {name: [values[name]] if name in values else []
            for name in ('swarm-plan', 'swarm-unit', 'swarm-repo', 'swarm-body')}


def project_identity(project):
    if project is None:
        return None
    return {'name': project['name'], 'description': project.get('description') or '',
            'content': identity_values(project.get('content'))}


def issue_identity(issue):
    """Managed values are plain title and markers, never rendered Markdown."""
    if issue is None:
        return None
    body = issue.get('description') or ''
    return {'title': issue['title'], 'identity': identity_values(body),
            'trailer': LI.components(body)['trailer']}


def prepare(draft, client, sync, org, team, pid, derived, adopt, op):
    p = draft['project']
    expected_project = {'swarm-plan': p['slug'], 'swarm-repo': p['repository']}
    remote = project_at(client, sync, pid)
    if draft['project'].get('linear_id') and remote is None:
        raise ValueError('recorded project not found')
    if remote:
        if team['id'] not in [t['id'] for t in remote['teams']['nodes']]:
            raise ValueError('project team disagrees')
        if not has_markers(remote.get('content'), expected_project) and not adopt:
            raise ValueError('foreign or unmarked project; audit then --adopt-checked: ' + pid)
    else:
        conflicts = [r for r in projects(client, sync, 'name', p['name'])
                     if not markers(r.get('content'), 'swarm-repo') or
                     p['repository'] in markers(r.get('content'), 'swarm-repo')]
        if conflicts:
            raise ValueError('same-named project; record project.linear_id: ' +
                             ', '.join(r['id'] for r in conflicts))
    project_before = project_values(remote) if remote else None
    # Adopting preserves existing prose. A created project's text is exactly the approved text.
    project_desired = (dict(project_before, content=identity_lines(project_before['content'], expected_project))
                       if remote else {'name': p['name'], 'description': p['summary'],
                                       'content': identity_lines(p['description'], expected_project, existing=False)})
    # Plain project descriptions round-trip after Linear's terminal whitespace trim.
    for field in ('description', 'content'):
        project_desired[field] = project_desired[field].rstrip()
    reader = sync.Reader(client)
    listed = reader.project_issues(pid) if remote else []
    derived_units = {i['unit']: API.derived_id('issue:%s/%s/%s/%s' %
                     (org['id'], p['repository'], p['slug'], i['unit'])) for i in draft['issues']}
    refs = set(derived_units.values())
    for issue in draft['issues']:
        refs.update(issue[k] for k in ('linear_id', 'identifier') if issue.get(k))
    resolved = reader.resolve(refs)
    LI.expand_scope(sync, reader)
    selected, mapping, used = {}, {}, {}
    for issue in draft['issues']:
        unit = issue['unit']
        binding = {'swarm-unit': p['slug'] + '/' + unit, 'swarm-repo': p['repository']}
        expected = dict(binding)
        expected['swarm-body'] = hashlib.sha256(issue['body'].encode('utf-8')).hexdigest()
        derived_hit = resolved[derived_units[unit]]
        ref = issue.get('linear_id') or issue.get('identifier')
        explicit_selection = ref and resolved[ref] is not None and resolved[ref]['id'] == derived_units[unit]
        if derived_hit and not explicit_selection and not (has_markers(derived_hit.get('description'), expected) or
                                has_markers(derived_hit.get('description'), binding)):
            raise ValueError('derived issue id collision: %s (%s)' %
                             (derived_hit['identifier'], derived_hit['id']))
        found = resolved[ref] if ref else resolved[derived_units[unit]]
        if ref and found is None:
            raise ValueError('recorded issue not found: ' + ref)
        if found and issue.get('identifier') and found['identifier'] != issue['identifier']:
            raise ValueError('recorded identifier disagrees')
        if not found:
            candidates = [i for i in listed if i['title'] == issue['title']]
            if len(candidates) > 1:
                raise ValueError('ambiguous title: ' + ', '.join(i['identifier'] for i in candidates))
            found = candidates[0] if candidates else None
        if found:
            sync.check_membership(found, pid, team['id'])
            LI.refuse_deleted_creation(found, None)
            body = found.get('description') or ''
            if LI.malformed_trailer(body):
                raise ValueError('malformed dependency trailer')
            other = [u for u, iid in derived_units.items() if iid == found['id'] and u != unit]
            other += [i['unit'] for i in draft['issues'] if i['unit'] != unit and
                      (i.get('linear_id') == found['id'] or i.get('identifier') == found['identifier'])]
            if (other or found['id'] in used or
                    any(markers(found.get('description'), name) and
                        markers(found.get('description'), name) != [value]
                        for name, value in binding.items())):
                raise ValueError('title candidate bound to another unit: ' + found['identifier'])
            if not (has_markers(body, expected) or has_markers(body, binding)) and not adopt:
                raise ValueError('unmarked issue; audit then --adopt-checked: ' + found['identifier'])
            used[found['id']] = unit
        iid = found['id'] if found else derived_units[unit]
        mapping[unit] = iid
        selected[iid] = (issue, found, expected)
    edges, _ = sync.edges_of(list(reader.seen.values()))
    units = set(mapping.values())
    declared = {(mapping[b], mapping[i['unit']]) for i in draft['issues'] for b in i['blocked_by']}
    internal = {(a, b) for a, b in edges if a in units and b in units}
    additions, removals = declared - edges, internal - declared
    desired_edges = (edges - removals) | additions
    if sync.cyclic(set(reader.seen) | units, desired_edges):
        raise ValueError('resulting blocks cycle')
    touched = units | {iid for edge in additions | removals for iid in edge
                       if iid in reader.seen and LI.trailer(reader.seen[iid].get('description'))}
    identities = {i['id']: i['identifier'] for i in reader.seen.values()}
    identities.update({iid: found['identifier'] if found else None for iid, (_, found, _) in selected.items()})
    rows = []
    for iid in sorted(touched, key=lambda i: (selected.get(i, (None, True))[1] is not None, i)):
        drafted, found, expected = (selected[iid] if iid in selected else
                                    (None, reader.seen[iid], None))
        before = {'title': found['title'], 'description': found.get('description') or ''} if found else None
        body = before['description'] if before else drafted['body']
        title = before['title'] if before else drafted['title']
        if LI.malformed_trailer(body):
            raise ValueError('malformed dependency trailer')
        replace_body = drafted and before and not has_markers(body, expected)
        body = identity_lines(body, expected, existing=before is not None) if expected else body
        if drafted:
            title = drafted['title']
        if replace_body:
            body = approved_body(body, drafted['body'])
        mark = LI.trailer(body)
        changed_edge = any(iid in edge for edge in additions | removals)
        if mark and changed_edge:
            # Do not discard earlier provenance, including the most recent deps-by line.
            prefix = mark['prefix']
            if mark['by']:
                prefix += '`swarm-deps-by: %s`\n' % mark['by']
            desired = {'title': title, 'prefix': prefix, 'op': mark['op'],
                       'approver': mark['approver'], 'by': op,
                       'incoming': sorted(a for a, b in desired_edges if b == iid),
                       'outgoing': sorted(b for a, b in desired_edges if a == iid)}
        else:
            desired = {'title': title, 'description': body.rstrip()}
        LI.check_structured(body, *LI.names_for(iid, desired_edges, {k: v or '<new issue>' for k, v in identities.items()}))
        rows.append({'id': iid, 'before': before, 'desired': desired, 'markers': expected})
    managed = {e for e in edges | desired_edges if any(i in touched for i in e)}
    spec = {'schema_version': 1, 'kind': 'file', 'operation': op, 'workspace': org['id'],
            'project': pid, 'team': team['id'], 'repository': p['repository'],
            'approval_digest': T.content_digest(draft), 'approver': draft['approval']['granted_by'],
            'project_before': project_before, 'project_desired': project_desired,
            'project_markers': expected_project, 'identities': identities, 'issues': rows,
            'mapping': mapping, 'edges': [{'a': a, 'b': b, 'before': (a, b) in edges,
                                         'desired': (a, b) in desired_edges} for a, b in sorted(managed)],
            'additions': [list(e) for e in sorted(additions)], 'removals': [list(e) for e in sorted(removals)],
            'kept': [list(e) for e in sorted(edges) if e[1] in units and e[0] not in units]}
    LI.reject_key(spec, client._key)
    return spec, reader, remote


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, str(path))
        LI.fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Filing:
    def __init__(self, args, draft, spec, path, client, sync, org, team):
        self.args, self.draft, self.spec, self.path = args, draft, spec, path
        self.client, self.sync, self.org, self.team = client, sync, org, team
        self.binding_path = Path(args.draft).parent / '.hanig/linear-binding.json'
        self.binding = {'schema_version': 1, 'repository': spec['repository'], 'workspace': org,
                        'team': team, 'project': {'id': spec['project'], 'name': spec['project_desired']['name']}}

    def save(self):
        LI.reject_key(self.draft, self.client._key)
        LI.reject_key(self.binding, self.client._key)
        atomic_json(Path(self.args.draft), self.draft)
        atomic_json(self.binding_path, self.binding)

    def checkpoint(self, step, current=None):
        if current:
            LI.reject_key(current['identifier'], self.client._key)
            for issue in self.draft['issues']:
                if self.spec['mapping'][issue['unit']] == current['id']:
                    issue.update(linear_id=current['id'], identifier=current['identifier'])
        self.save()

    def project_check(self, remote, is_confirmed):
        s = self.spec
        if remote is None:
            if s['project_before'] is not None or is_confirmed or any(
                    e.get('step') == 'project' for e in LI.progress(self.path)):
                raise ValueError('managed project deleted')
            return
        if self.team['id'] not in [t['id'] for t in remote['teams']['nodes']]:
            raise ValueError('project team changed')
        live = project_identity(remote)
        if live != project_identity(s['project_desired']) and (
                is_confirmed or live != project_identity(s['project_before'])):
            raise ValueError('managed project changed')

    def read_managed(self, is_confirmed, reader=None):
        s = self.spec
        if reader is None:
            reader = self.sync.Reader(self.client)
            reader.resolve(s['identities'])
            LI.expand_scope(self.sync, reader)
        identities = dict(s['identities'])
        recorded_names = {s['mapping'][i['unit']]: i.get('identifier') for i in self.draft['issues']}
        live = {row['id']: reader.seen.get(row['id']) for row in s['issues']}
        for iid, issue in live.items():
            if issue:
                self.sync.check_membership(issue, s['project'], s['team'])
                LI.refuse_deleted_creation(issue, self.path if self.path.exists() else None)
                if any(value is not None and value != issue['identifier']
                       for value in (identities[iid], recorded_names.get(iid))):
                    raise ValueError('managed identifier changed')
                identities[iid] = issue['identifier']
        steps = {e.get('step') for e in LI.progress(self.path)}
        for row in s['issues']:
            issue = live[row['id']]
            if issue is None:
                if row['before'] is not None or is_confirmed or 'issue:' + row['id'] in steps:
                    raise ValueError('managed issue deleted')
                continue
            resolvable = all(identities[i] for i in row['desired'].get('incoming', []) + row['desired'].get('outgoing', []))
            desired = LI.desired_value(row, identities) if resolvable else row['before']
            actual = issue_identity(issue)
            if actual != issue_identity(desired) and (
                    is_confirmed or actual != issue_identity(row['before'])):
                raise ValueError('managed issue changed: ' + issue['identifier'])
            if row['before'] is None:
                self.validate_created(row, issue)
        edges, _ = self.sync.edges_of([i for i in live.values() if i])
        allowed = {(e['a'], e['b']) for e in s['edges']}
        if edges - allowed or any(((e['a'], e['b']) in edges) != e['desired'] and
                                 (is_confirmed or e['desired'] == e['before']) for e in s['edges']):
            raise ValueError('managed edges changed')
        if self.sync.cyclic(set(reader.seen) | set(live),
                            (self.sync.edges_of(list(reader.seen.values()))[0] -
                             {tuple(e) for e in s['removals']}) | {tuple(e) for e in s['additions']}):
            raise ValueError('resulting blocks cycle')
        return live, identities

    def validate_created(self, row, current):
        if not current or not has_markers(current.get('description'), row['markers']):
            raise ValueError('created issue missing or lacks draft markers')
        LI.reject_key(current, self.client._key)

    def issue_matches(self, current, desired):
        return issue_identity(current) == issue_identity(desired)

    def issue_update(self, current, desired):
        before, after = issue_identity(current), issue_identity(desired)
        changes = {}
        if before['title'] != after['title']:
            changes['title'] = desired['title']
        if any(before[k] != after[k] for k in ('identity', 'trailer')):
            changes['description'] = desired['description']
        return changes

    def project_apply(self, remote):
        s = self.spec
        desired = s['project_desired']
        create_error = None
        if remote is None:
            try:
                self.client.query('mutation FilingProjectCreate($input: ProjectCreateInput!) { '
                                  'projectCreate(input: $input) { success } }',
                                  {'input': dict(desired, id=s['project'], teamIds=[s['team']])})
            except API.LinearError as exc:
                create_error = exc
        elif project_identity(remote) != project_identity(desired):
            self.client.query('mutation FilingProjectUpdate($id: String!, $input: ProjectUpdateInput!) { '
                              'projectUpdate(id: $id, input: $input) { success } }',
                              {'id': s['project'], 'input': desired})
        try:
            remote = project_at(self.client, self.sync, s['project'])
        except (API.LinearError, OSError, ValueError, KeyError, TypeError):
            if create_error is not None:
                raise create_error from None
            raise
        if create_error is not None and (remote is None or not has_markers(
                remote.get('content'), s['project_markers'])):
            raise create_error
        self.project_check(remote, True)
        LI.reject_key(remote, self.client._key)
        self.draft['project'].update(linear_id=remote['id'], url=remote.get('url'))
        self.draft['workspace'] = self.org
        LI.log_step(self.path, 'project')
        self.checkpoint('project')

    def verify(self):
        s = self.spec
        remote = project_at(self.client, self.sync, s['project'])
        self.project_check(remote, True)
        plan = {'name': self.draft['project']['slug'], 'units': [
            {'id': i['unit'], 'needs': i['blocked_by']} for i in self.draft['issues']]}
        args = argparse.Namespace(plan=None, draft=None, binding=None, state_dir=None)
        record, reader = self.sync.audit(args, self.client, filing={
            'plan': plan, 'draft': self.draft, 'binding': self.binding,
            'checked': [row['id'] for row in s['issues']]})
        live, identities = self.read_managed(True, reader)
        # Plan units are judged by plan_edges, not the ad-hoc relationless check.
        required = {'binding', 'coverage', 'plan_edges', 'misplaced', 'declared_edges', 'cycle'}
        bad = [c['id'] + '=' + c['verdict'] for c in record['checks']
               if c['id'] in required and c['verdict'] != 'CLEAN']
        bad += [cid + '=MISSING' for cid in sorted(required - {c['id'] for c in record['checks']})]
        if bad:
            raise LI.IncompleteGraph('scoped audit: ' + ', '.join(bad))
        edges, _ = self.sync.edges_of(list(reader.seen.values()))
        self.draft[T.READBACK] = {'schema_version': 1, 'read_at': record['read_finished'],
                                'source': 'linear_sync.py file',
                                'edges': {identities[iid]: sorted(identities[a] for a, b in edges if b == iid)
                                          for iid in s['mapping'].values()}}
        T.sync_blocked_by(self.draft, self.draft[T.READBACK])
        self.save()
        print('plan checks CLEAN as read %s to %s' % (record['read_started'], record['read_finished']))
        return s['project']


def replay_record(op):
    LI.operation_id(op)
    # The draft may have no recorded ids if the process died during project creation.
    root = TA.operation_directory('workspace', 'project').parent.parent
    matches = list(root.glob('*/*/' + op + '.json'))
    if len(matches) != 1:
        raise ValueError('operation not found or ambiguous')
    LI.outside_git(matches[0].parent)
    return matches[0], LI.load_record(matches[0])


def run(args, client, sync):
    LI.reject_key(vars(args), client._key)
    raw = Path(args.draft).read_bytes()
    draft = json.loads(raw)
    digest = validate(draft, client._key)
    replay = args.command == 'replay'
    if replay:
        path, spec = replay_record(args.operation_id)
        LI.reject_key(spec, client._key)
        if spec['kind'] != 'file' or spec['approval_digest'] != digest:
            raise ValueError('replay approval digest differs')
        approved_hashes = {spec['mapping'][i['unit']]: hashlib.sha256(i['body'].encode('utf-8')).hexdigest()
                           for i in draft['issues']}
        if any((row.get('markers') or {}).get('swarm-body') != approved_hashes[row['id']]
               for row in spec['issues'] if row['id'] in approved_hashes):
            raise ValueError('operation lacks approved body identity; run file with the approved draft')
    org, team = workspace_team(client, sync, draft['project']['team'])
    derived = API.derived_id('project:%s/%s/%s' % (org['id'], draft['project']['repository'], draft['project']['slug']))
    pid = spec['project'] if replay else draft['project'].get('linear_id') or derived
    if replay:
        if (spec['workspace'] != org['id'] or spec['team'] != team['id'] or
                spec['operation'] != args.operation_id or
                draft['project'].get('linear_id') not in (None, pid) or any(
                    i.get('linear_id') not in (None, spec['mapping'][i['unit']]) for i in draft['issues'])):
            raise ValueError('operation binding differs')
    op = args.operation_id if replay else str(uuid.uuid4())
    if not replay:
        path = LI.record_path(org['id'], pid, op)
    if not replay and args.preview:
        spec, _, _ = prepare(draft, client, sync, org, team, pid, derived, args.adopt_checked, op)
        print(json.dumps({'project': 'create' if spec['project_before'] is None else 'adopt',
                          'issues': [{'id': r['id'], 'action': 'create' if r['before'] is None else 'adopt'}
                                     for r in spec['issues']],
                          'link': spec['additions'], 'unlink': spec['removals'], 'kept': spec['kept']}, indent=2))
        return 0
    with sync.project_lock(org['id'], pid):
        if Path(args.draft).read_bytes() != raw:
            raise ValueError('draft changed while acquiring lock')
        if not replay:
            spec, reader, remote = prepare(draft, client, sync, org, team, pid, derived, args.adopt_checked, op)
        else:
            remote = project_at(client, sync, pid)
        handler = Filing(args, draft, spec, path, client, sync, org, team)
        handler.project_check(remote, LI.confirmed(path))
        live, identities = handler.read_managed(LI.confirmed(path), None if replay else reader)
        endpoints = LI.source_endpoints(spec, client, sync, [iid for iid, i in live.items() if i is None])
        if not replay:
            LI.save_record(path, spec)
        print('operation ' + op)
        for a, b in spec['kept']:
            print('kept outside-plan blocker %s -> %s; remove with issue edit if stale' % (a, b))
        try:
            if not LI.confirmed(path):
                handler.project_apply(remote)
            LI.apply(spec, path, client, sync, initial=(live, identities, endpoints), handler=handler)
        except (API.LinearError, OSError, ValueError, KeyError, TypeError) as exc:
            print(API.redact('INCOMPLETE: %s; replay %s' % (exc, op), client._key))
            return 3
        print('CONFIRMED ' + op)
        return 0
