"""Durable ad-hoc Linear operations. Transport and coverage come from the operator.

Specifications are immutable; progress is an append-only hint, never a reason
to skip a live read. Same-UID writers and cross-host races remain limitations.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

import linear_api as API
import tracker_audit as TA

IDENTIFIER = r'[A-Z][A-Z0-9]*-[0-9]+'
ISSUE_UUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
INDEPENDENT = re.compile(r'(?m)^[ \t]*swarm-independent:[ \t]*(\S[^\r\n]*)\r?$')
MAX_SCOPE_ISSUES = 2000


class IncompleteGraph(ValueError):
    """Read-back coverage cannot establish the resulting graph."""


def add_parser(sub):
    issue = sub.add_parser('issue')
    commands = issue.add_subparsers(dest='issue_command', required=True)
    for name in ('new', 'edit', 'replay'):
        p = commands.add_parser(name)
        p.add_argument('--binding', required=True)
        p.set_defaults(draft=None)
        if name == 'replay':
            p.add_argument('operation_id')
            continue
        if name == 'edit':
            p.add_argument('identifier')
        p.add_argument('--title', required=name == 'new')
        body = p.add_mutually_exclusive_group(required=name == 'new')
        body.add_argument('--body-file')
        body.add_argument('--body-stdin', action='store_true')
        for flag in (('blocked-by', 'blocks') if name == 'new' else
                     ('add-blocked-by', 'remove-blocked-by', 'add-blocks', 'remove-blocks')):
            p.add_argument('--' + flag, action='append', default=[])
        independence = p.add_mutually_exclusive_group()
        independence.add_argument('--independent')
        if name == 'edit':
            independence.add_argument('--clear-independent', action='store_true')
        p.add_argument('--approver', required=True)
        p.add_argument('--preview', action='store_true')


def line_value(value, name, code_span=False):
    forbidden = '\r\n`' if code_span else '\r\n'
    if not isinstance(value, str) or not value.strip() or any(c in value for c in forbidden):
        raise ValueError(name + ' must be a nonblank single line' + (' without backticks' if code_span else ''))


def validate_request(args):
    if args.issue_command == 'replay':
        operation_id(args.operation_id)
        return
    line_value(args.approver, 'approver', code_span=True)
    if args.independent is not None:
        line_value(args.independent, 'independence reason')
    if args.title is not None and not args.title.strip():
        raise ValueError('title must not be blank')
    flags = ('blocked_by', 'blocks') if args.issue_command == 'new' else (
        'add_blocked_by', 'remove_blocked_by', 'add_blocks', 'remove_blocks')
    refs = [ref for flag in flags for ref in getattr(args, flag)]
    if args.issue_command == 'edit':
        refs.append(args.identifier)
    for ref in refs:
        validate_reference(ref)
    if args.issue_command == 'new':
        dependencies = bool(args.blocked_by or args.blocks)
        if dependencies == (args.independent is not None):
            raise ValueError('new requires dependencies or an independence reason, exclusively')


def validate_reference(ref):
    if not re.fullmatch(IDENTIFIER, ref) and not re.fullmatch(ISSUE_UUID, ref):
        raise ValueError('invalid issue reference; identifiers are written as TEAM-123, or use a UUID')


def reject_key(value, key):
    if isinstance(value, str) and key and key in value:
        raise ValueError('content contains the loaded API key')
    if isinstance(value, dict):
        for item in list(value) + list(value.values()):
            reject_key(item, key)
    if isinstance(value, list):
        for item in value:
            reject_key(item, key)


def trailer(body):
    """Only the final three nonempty lines are the authoritative trailer."""
    lines = list(re.finditer(r'[^\r\n]+', body or ''))
    lines = [m for m in lines if m[0].strip()]
    if len(lines) < 3:
        return None
    last = lines[-3:]
    values = {}
    for name, match in zip(('deps', 'op', 'approver'), last):
        parsed = re.fullmatch(r'`swarm-' + name + r': ([^`\r\n]+)`', match[0])
        if not parsed:
            return None
        values[name] = parsed[1]
    values['prefix'] = body[:last[0].start()]
    by = re.search(r'`swarm-deps-by: ([^`\r\n]+)`\s*$', values['prefix'])
    values['by'] = by[1] if by else None
    if by:
        values['prefix'] = values['prefix'][:by.start()]
    return values


def dependency_names(issue):
    incoming, outgoing = set(), set()
    for field in ('relations', 'inverseRelations'):
        for relation in issue[field]['nodes']:
            if relation['type'] != 'blocks':
                continue
            a, b = relation['issue'], relation['relatedIssue']
            if b['id'] == issue['id']:
                incoming.add(a['identifier'])
            if a['id'] == issue['id']:
                outgoing.add(b['identifier'])
    return incoming, outgoing


def deps_text(incoming, outgoing):
    return 'blocked-by=%s blocks=%s' % (','.join(sorted(incoming)) or '-',
                                      ','.join(sorted(outgoing)) or '-')


def declared_edges(issue):
    mark = trailer(issue.get('description'))
    body = issue.get('description') or ''
    if mark is None:
        if re.search(r'(?m)^`swarm-deps:', body):
            return {'issue': issue['identifier'], 'error': 'malformed dependency trailer'}
        return None
    parsed = re.fullmatch(r'blocked-by=([^ ]+) blocks=([^ ]+)', mark['deps'])
    declared = tuple(set(v.split(',')) if v != '-' else set() for v in parsed.groups()) if parsed else None
    if declared != dependency_names(issue):
        return {'issue': issue['identifier'], 'declared': mark['deps'],
                'actual': deps_text(*dependency_names(issue))}
    return None


def structured(body):
    """Whole dependency lines only; Markdown code and quotations are excluded."""
    result = {}
    fence = None
    for line in body.splitlines():
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if match:
            token = match[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not match[2].strip():
                fence = None
            continue
        if fence:
            continue
        match = re.fullmatch(r' {0,3}(blocked by|depends on|blocks):[ \t]*(' +
                             IDENTIFIER + r'(?:(?:[ \t]*,[ \t]*|[ \t]+)' +
                             IDENTIFIER + r')*)[ \t]*', line, re.I)
        if match:
            direction = 'blocks' if match[1].lower() == 'blocks' else 'blocked-by'
            # Case insensitivity belongs to the syntax, never to identities.
            result.setdefault(direction, set()).update(re.split(r'[ ,\t]+', match[2]))
    return result


def check_structured(body, incoming, outgoing):
    for direction, names in structured(body).items():
        expected = outgoing if direction == 'blocks' else incoming
        if names != expected:
            raise ValueError('structured ' + direction + ' disagrees with resulting relations')


def components(body):
    body = body or ''
    mark = trailer(body)
    prefix = mark['prefix'] if mark else body
    return {'body': INDEPENDENT.sub('', prefix),
            'independence': INDEPENDENT.findall(prefix),
            'trailer': {k: mark[k] for k in ('deps', 'op', 'approver', 'by')} if mark else None}


def description(prefix, incoming, outgoing, op, approver, by=None):
    if prefix and not prefix.endswith('\n'):
        prefix += '\n'
    if by:
        prefix += '`swarm-deps-by: %s`\n' % by
    return prefix + '`swarm-deps: %s`\n`swarm-op: %s`\n`swarm-approver: %s`' % (
        deps_text(incoming, outgoing), op, approver)


def operation_id(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
        raise ValueError('invalid operation id')
    return value


def digest(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def outside_git(path):
    for parent in (path.resolve(), *path.resolve().parents):
        if (parent / '.git').exists():
            raise ValueError('operation records must be outside every Git worktree')


def record_path(workspace, project, op):
    path = TA.operation_directory(workspace, project) / (operation_id(op) + '.json')
    outside_git(path.parent)
    return path


def progress_path(path):
    return path.with_suffix('.progress.jsonl')


def fsync_directory(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_record(path, spec):
    # Persist new directory entries too, before any remote mutation.
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
        fsync_directory(directory.parent)
    with path.open('x', encoding='utf-8') as handle:
        json.dump({'spec': spec, 'sha256': digest(spec)}, handle, ensure_ascii=False, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


def load_record(path):
    record = json.loads(path.read_text(encoding='utf-8'))
    if record['sha256'] != digest(record['spec']):
        raise ValueError('operation specification digest mismatch')
    return record['spec']


def progress(path):
    try:
        raw = progress_path(path).read_bytes()
    except FileNotFoundError:
        return []
    # A torn last append is incomplete, not confirmation. Retain its bytes.
    entries = []
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b'\n'):
            entries.append({'step': 'TORN'})
            break
        if line.strip():
            try:
                entries.append(json.loads(line))
            except ValueError:
                entries.append({'step': 'TORN'})
    return entries


def log_step(path, step):
    target = progress_path(path)
    with target.open('ab+') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell():
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b'\n':
                handle.write(b'\n')
        handle.write((json.dumps({'step': step}) + '\n').encode('utf-8'))
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


def confirmed(path):
    entries = progress(path)
    return bool(entries and isinstance(entries[-1], dict) and entries[-1].get('step') == 'CONFIRMED')


def incomplete_operations(workspace, project):
    findings = []
    for path in sorted(TA.operation_directory(workspace, project).glob('*.json')):
        try:
            spec = load_record(path)
            if spec['workspace'] != workspace or spec['project'] != project:
                raise ValueError('operation binding mismatch')
            if confirmed(path):
                continue
            reason = 'read-back unconfirmed'
        except (ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
        findings.append({'operation': path.stem, 'reason': reason,
                         'replay': 'linear_sync.py issue replay %s --binding FILE' % path.stem})
    return findings


def scope(sync, client, project, refs):
    reader = sync.Reader(client)
    def check_limit(extra=0):
        if len(reader.seen) + extra > MAX_SCOPE_ISSUES:
            raise ValueError('graph issue limit exceeded: %d' % MAX_SCOPE_ISSUES)
    reader.project_issues(project)
    check_limit()
    for ref in refs:
        if not any(ref in (i['id'], i['identifier']) for i in reader.seen.values()):
            check_limit(1)
            if re.fullmatch(IDENTIFIER, ref):
                team, number = ref.rsplit('-', 1)
                query = ('query OperationIdentifier($filter: IssueFilter!) { '
                         'issues(filter: $filter, first: 1, includeArchived: true) '
                         '{ nodes { %s } %s } }' % (sync.FIELDS, sync.PAGE))
                nodes = reader.pages_of(lambda after: client.query(query, {'filter': {
                    'team': {'key': {'eq': team}}, 'number': {'eq': int(number)}}})['issues'])
                if not nodes:
                    raise ValueError('issue not found: ' + ref)
                if len(nodes) != 1 or nodes[0]['identifier'] != ref:
                    raise ValueError('issue identifier disagrees on read-back: ' + ref)
                reader.remember(nodes[0])
            else:
                validate_reference(ref)
                if read_by_id(sync, client, ref, reader) is None:
                    raise ValueError('issue not found: ' + ref)
    # Read both ends of every reached blocks edge, including each newly read
    # issue's paginated relations. A one-hop boundary can hide a return path.
    pending = list(reader.seen.values())
    for issue in pending:
        edges, _ = sync.edges_of([issue])
        for iid in sorted({iid for edge in edges for iid in edge} - reader.seen.keys()):
            check_limit(1)
            reached = read_by_id(sync, client, iid, reader)
            if reached is None:
                raise ValueError('issue not found: ' + iid)
            pending.append(reached)
    reader.stable()
    if reader.problems:
        raise ValueError('incomplete graph read: ' + '; '.join(reader.problems))
    return reader


def names_for(iid, edges, identities):
    return ({identities[a] for a, b in edges if b == iid},
            {identities[b] for a, b in edges if a == iid})


def prepare(args, client, sync, identity, op):
    _, _, _, workspace, project, team = identity
    target_id = API.derived_id('issue:%s/%s/op/%s' % (workspace, project, op))
    changes = [('blocked_by', True, True), ('blocks', True, False)] if args.issue_command == 'new' else [
        ('add_blocked_by', True, True), ('remove_blocked_by', False, True),
        ('add_blocks', True, False), ('remove_blocks', False, False)]
    refs = [ref for flag, _, _ in changes for ref in getattr(args, flag)]
    if args.issue_command == 'edit':
        refs.append(args.identifier)
    reader = scope(sync, client, project, refs)
    issues = reader.seen
    by_ref = {ref: i for i in issues.values() for ref in (i['id'], i['identifier'])}
    target = by_ref[args.identifier] if args.issue_command == 'edit' else None
    if target:
        sync.check_membership(target, project, team)
        target_id = target['id']
    edges, neighbors = sync.edges_of(list(issues.values()))
    additions, removals = set(), set()
    for flag, adding, incoming in changes:
        for ref in getattr(args, flag):
            peer = by_ref[ref]['id']
            edge = (peer, target_id) if incoming else (target_id, peer)
            (additions if adding else removals).add(edge)
    if additions & removals:
        raise ValueError('the same edge cannot be added and removed')
    desired_edges = (edges - removals) | additions
    if sync.cyclic(set(issues) | {target_id}, desired_edges):
        raise ValueError('resulting blocks cycle')
    identities = {i['id']: i['identifier'] for i in issues.values()}
    for issue in issues.values():
        for field in ('relations', 'inverseRelations'):
            for relation in issue[field]['nodes']:
                for endpoint in ('issue', 'relatedIssue'):
                    peer = relation[endpoint]
                    identities[peer['id']] = peer['identifier']
    # The server assigns a new issue's identifier. This symbolic slot is bound
    # to its derived id, and filled only from that exact issue on read-back.
    identities[target_id] = target['identifier'] if target else None
    old_body = (target.get('description') or '') if target else ''
    mark = trailer(old_body)
    prefix = args.body if args.body is not None else (mark['prefix'] if mark else old_body)
    reason = args.independent
    if reason is None and not getattr(args, 'clear_independent', False):
        reasons = INDEPENDENT.findall(old_body)
        reason = reasons[0] if reasons else None
    if args.body is None or getattr(args, 'clear_independent', False):
        prefix = INDEPENDENT.sub('', prefix)
    if reason:
        prefix += ('\n' if prefix and not prefix.endswith('\n') else '') + 'swarm-independent: ' + reason + '\n'
    touched = {target_id}
    for a, b in additions | removals:
        peer = b if a == target_id else a
        other = issues[peer]
        if not trailer(other.get('description')) and re.search(r'(?m)^`swarm-deps:', other.get('description') or ''):
            raise ValueError('malformed counterpart dependency trailer: ' + other['identifier'])
        if trailer(other.get('description')):
            sync.check_membership(other, project, team)
            touched.add(peer)
    # Removing an edge also binds the unmarked endpoint's relationless rule.
    for iid in touched | {n for edge in removals for n in edge}:
        issue = issues.get(iid)
        local = issue is None or (issue.get('project') or {}).get('id') == project
        has_relation = any(iid in edge for edge in desired_edges)
        if issue:
            has_relation = has_relation or any(r['type'] != 'blocks' for f in ('relations', 'inverseRelations')
                                               for r in issue[f]['nodes'])
        body = prefix if iid == target_id else issue.get('description') or ''
        candidate = dict(issue or {'id': iid, 'state': {'type': 'unstarted'}}, description=body)
        if local and sync.relationless(candidate, has_relation):
            raise ValueError('relationless open issue: ' + (issue['identifier'] if issue else 'new issue'))
    rows = []
    warnings = []
    for iid in sorted(touched, key=lambda value: (value != target_id, value)):
        issue = issues.get(iid)
        before = {'title': issue['title'], 'description': issue.get('description') or ''} if issue else None
        old = trailer(before['description']) if before else None
        body = prefix if iid == target_id else old['prefix']
        incoming, outgoing = names_for(iid, desired_edges, {k: v or '<new issue>' for k, v in identities.items()})
        check_structured(body, incoming, outgoing)
        warnings.extend(sync.prose_candidates({'identifier': identities[iid] or '<new issue>', 'description': body},
                                              {n for edge in desired_edges if iid in edge for n in edge if n != iid},
                                              {v: k for k, v in identities.items() if v}))
        desired = {'title': (args.title if args.title is not None else before['title']) if iid == target_id else before['title'],
                   'prefix': body, 'op': op if iid == target_id else old['op'],
                   'approver': args.approver if iid == target_id else old['approver'],
                   'by': None if iid == target_id else op,
                   'incoming': sorted(a for a, b in desired_edges if b == iid),
                   'outgoing': sorted(b for a, b in desired_edges if a == iid)}
        rows.append({'id': iid, 'before': before, 'desired': desired})
    managed = {edge for edge in edges | desired_edges if any(i in touched for i in edge)} | additions | removals
    spec = {'schema_version': 1, 'operation': op, 'kind': args.issue_command,
            'workspace': workspace, 'project': project, 'team': team,
            'repository': identity[2].get('repository'), 'approver': args.approver,
            'target': target_id, 'identifier': args.identifier if target else None,
            'identities': identities, 'issues': rows,
            'edges': [{'a': a, 'b': b, 'before': (a, b) in edges, 'desired': (a, b) in desired_edges}
                      for a, b in sorted(managed)],
            'additions': [list(e) for e in sorted(additions)], 'removals': [list(e) for e in sorted(removals)]}
    reject_key(spec, client._key)
    return spec, warnings


def read_by_id(sync, client, iid, reader=None):
    query = ('query OperationIssue($id: ID!) { issues(filter: {id: {eq: $id}}, '
             'first: 1, includeArchived: true) { nodes { trashed %s } %s } }' % (sync.FIELDS, sync.PAGE))
    reader = reader if reader is not None else sync.Reader(client)
    nodes = reader.pages_of(lambda after: client.query(query, {'id': iid})['issues'])
    if len(nodes) > 1 or (nodes and nodes[0]['id'] != iid):
        raise ValueError('issue id disagrees on read-back')
    return reader.remember(nodes[0]) if nodes else None


def refuse_deleted_creation(issue, path):
    if issue and (issue['trashed'] or issue['archivedAt']):
        # Preserve this observation before refusing; a later purge must not
        # make the same operation appear never to have created its issue.
        step = 'issue:' + issue['id']
        if path is not None and not any(isinstance(entry, dict) and entry.get('step') in (step, 'CONFIRMED')
                                        for entry in progress(path)):
            log_step(path, step)
        raise ValueError('issue deleted after creation')


def desired_value(row, identities):
    d = row['desired']
    return {'title': d['title'], 'description': description(d['prefix'],
            {identities[i] for i in d['incoming']}, {identities[i] for i in d['outgoing']},
            d['op'], d['approver'], d['by'])}


def read_managed(spec, sync, client, is_confirmed, path):
    identities = dict(spec['identities'])
    target = read_by_id(sync, client, spec['target'])
    if spec['kind'] == 'new':
        refuse_deleted_creation(target, path)
    creation_step = 'issue:' + spec['target']
    created = any(isinstance(entry, dict) and entry.get('step') in (creation_step, 'CONFIRMED')
                  for entry in (progress(path) if path is not None else []))
    if spec['kind'] == 'new' and not target and (created or is_confirmed):
        raise ValueError('issue deleted after creation')
    if spec['kind'] == 'new' and target:
        mark = trailer(target.get('description'))
        if not is_confirmed and (not mark or mark['op'] != spec['operation']):
            raise ValueError('created issue lacks this operation marker')
        identities[spec['target']] = target['identifier']
        # Preserve observed creation even if a later managed-field check fails.
        if not created and path is not None:
            log_step(path, creation_step)
    if is_confirmed and not target:
        raise ValueError('issue deleted after completion')
    live = {spec['target']: target}
    live.update({row['id']: read_by_id(sync, client, row['id'])
                 for row in spec['issues'] if row['id'] != spec['target']})
    for row in spec['issues']:
        issue = live[row['id']]
        if issue is None:
            if row['before'] is not None or is_confirmed:
                raise ValueError('managed issue deleted: ' + row['id'])
            continue
        sync.check_membership(issue, spec['project'], spec['team'])
        if spec['identities'][row['id']] is not None and issue['identifier'] != spec['identities'][row['id']]:
            raise ValueError('managed identifier changed: ' + row['id'])
        # Before creation, the desired counterpart trailer has a symbolic id.
        # Its only permitted live value then is the recorded before trailer.
        resolvable = all(identities[i] for i in row['desired']['incoming'] + row['desired']['outgoing'])
        desired = desired_value(row, identities) if resolvable else row['before']
        before = row['before'] or desired
        for field, value in [('title', issue['title'])] + list(components(issue.get('description')).items()):
            old = before['title'] if field == 'title' else components(before['description'])[field]
            new = desired['title'] if field == 'title' else components(desired['description'])[field]
            if value != new and (is_confirmed or value != old):
                raise ValueError('managed %s changed: %s' % (field, issue['identifier']))
    # Any extra incident edge is outside both recorded snapshots; do not write.
    edges, _ = sync.edges_of([i for i in live.values() if i])
    allowed = {(e['a'], e['b']) for e in spec['edges']}
    if edges - allowed:
        raise ValueError('declared_edges DRIFT: unrecorded blocks edge')
    for edge in spec['edges']:
        value = (edge['a'], edge['b']) in edges
        if value != edge['desired'] and (is_confirmed or value != edge['before']):
            raise ValueError('declared_edges DRIFT: managed edge changed')
    return live, identities


def relation_by_id(client, rid):
    nodes = client.query('query OperationRelation($id: ID!) { issueRelations(filter: {id: {eq: $id}}, '
                         'first: 1) { nodes { id type issue { id } relatedIssue { id } } } }',
                         {'id': rid})['issueRelations']['nodes']
    return nodes[0] if nodes else None


def apply(spec, path, client, sync, initial=None):
    is_confirmed = confirmed(path)
    live, identities = (read_managed(spec, sync, client, is_confirmed, path)
                        if initial is None else initial)
    if is_confirmed:
        verify(spec, client, sync)
        return
    for row in spec['issues']:
        iid = row['id']
        desired = desired_value(row, identities)
        current = live[iid]
        if current is None:
            # Recheck the exact derived id immediately before creating: a
            # prior create may have succeeded without a durable progress line.
            current = read_by_id(sync, client, iid)
            refuse_deleted_creation(current, path)
            if current is None:
                try:
                    client.query('mutation OperationCreate($input: IssueCreateInput!) { issueCreate(input: $input) { success } }',
                                 {'input': dict(desired, id=iid, projectId=spec['project'], teamId=spec['team'])})
                except API.LinearError:
                    pass
                current = read_by_id(sync, client, iid)
                refuse_deleted_creation(current, path)
            mark = trailer(current.get('description')) if current else None
            if not current or not mark or mark['op'] != spec['operation']:
                raise ValueError('rejected create: issue missing or lacks this operation marker')
            sync.check_membership(current, spec['project'], spec['team'])
            identities[iid] = current['identifier']
        elif any(current.get(k) != v for k, v in desired.items()):
            client.query('mutation OperationUpdate($id: String!, $input: IssueUpdateInput!) { '
                         'issueUpdate(id: $id, input: $input) { success } }', {'id': iid, 'input': desired})
        log_step(path, 'issue:' + iid)
    # All touched trailers precede every relation change.
    for adding, changes in ((True, spec['additions']), (False, spec['removals'])):
        for a, b in changes:
            issue = read_by_id(sync, client, a)
            if issue is None:
                raise ValueError('relation endpoint deleted: ' + a)
            matches = [r for r in issue['relations']['nodes'] if r['type'] == 'blocks'
                       and r['issue']['id'] == a and r['relatedIssue']['id'] == b]
            if adding and not matches:
                rid = API.derived_id('relation:%s/%s/%s/%s' % (spec['workspace'], spec['project'], a, b))
                try:
                    client.query('mutation OperationRelationCreate($input: IssueRelationCreateInput!) { '
                                 'issueRelationCreate(input: $input) { success } }',
                                 {'input': {'id': rid, 'type': 'blocks', 'issueId': a, 'relatedIssueId': b}})
                except API.LinearError:
                    pass
                relation = relation_by_id(client, rid)
                if (not relation or relation['id'] != rid or relation['type'] != 'blocks' or
                        relation['issue']['id'] != a or relation['relatedIssue']['id'] != b):
                    raise ValueError('rejected relation create: type or endpoints disagree')
            if not adding:
                for relation in matches:
                    client.query('mutation OperationRelationDelete($id: String!) { issueRelationDelete(id: $id) { success } }',
                                 {'id': relation['id']})
            log_step(path, ('add:' if adding else 'remove:') + a + '/' + b)
    verify(spec, client, sync)
    log_step(path, 'CONFIRMED')


def verify(spec, client, sync):
    try:
        reader = scope(sync, client, spec['project'], [row['id'] for row in spec['issues']])
    except (API.LinearError, OSError, ValueError, KeyError, TypeError) as exc:
        raise IncompleteGraph('incomplete graph read: ' + str(exc)) from exc
    identities = dict(spec['identities'])
    if spec['kind'] == 'new':
        identities[spec['target']] = reader.seen[spec['target']]['identifier']
    edges, _ = sync.edges_of(list(reader.seen.values()))
    for row in spec['issues']:
        issue = reader.seen[row['id']]
        sync.check_membership(issue, spec['project'], spec['team'])
        expected = dict(desired_value(row, identities), identifier=identities[row['id']])
        for field, desired in expected.items():
            if issue.get(field) != desired:
                raise ValueError('issue %s read-back differs: %s' % (field, row['id']))
        if declared_edges(issue):
            raise ValueError('declared_edges DRIFT: marker identifiers differ: ' + issue['identifier'])
        actual = {edge for edge in edges if row['id'] in edge}
        desired = {(e['a'], e['b']) for e in spec['edges'] if e['desired'] and row['id'] in (e['a'], e['b'])}
        if actual != desired:
            raise ValueError('declared_edges DRIFT: ' + issue['identifier'])
    if sync.cyclic(set(reader.seen), edges):
        raise ValueError('blocks cycle DRIFT after write')


def run(args, client, sync):
    replay = args.issue_command == 'replay'
    if not replay:
        args.body = (Path(args.body_file).read_bytes().decode('utf-8') if args.body_file else
                     sys.stdin.read() if args.body_stdin else None)
        reject_key(vars(args), client._key)
        if args.body is not None and re.search(r'(?m)^`swarm-(?:deps|op|approver|deps-by):', args.body):
            raise ValueError('body must not supply managed marker lines')
    identity = sync.drain_identity(args, sync.Reader(client))
    binding_path, binding_bytes, config, workspace, project, team = identity
    op = args.operation_id if replay else str(uuid.uuid4())
    path = record_path(workspace, project, op)
    if replay:
        spec = load_record(path)
        if (spec['operation'] != op or spec['workspace'] != workspace or spec['project'] != project or
                spec['team'] != team or spec['repository'] != config.get('repository')):
            raise ValueError('operation binding mismatch')
        reject_key(spec, client._key)
    else:
        spec, warnings = prepare(args, client, sync, identity, op)
        for warning in warnings:
            print(API.redact('warning: ' + json.dumps(warning), client._key))
        if args.preview:
            print(json.dumps({'spec': spec, 'checks': 'passed', 'warnings': warnings}, indent=2))
            return 0
    with sync.project_lock(workspace, project):
        if binding_path.read_bytes() != binding_bytes:
            raise ValueError('binding changed while acquiring lock')
        initial = None
        if not replay:
            # Preflight before creating the lock avoids files on ordinary
            # refusals; repeat under the shared lock to serialize local writers.
            spec, _ = prepare(args, client, sync, identity, op)
            # Fresh refusals leave no record or progress. Reuse this snapshot
            # in apply; replay still checks and journals against its record.
            initial = read_managed(spec, sync, client, False, None)
            save_record(path, spec)
        print('operation ' + op)
        try:
            apply(spec, path, client, sync, initial=initial)
        except (API.LinearError, OSError, ValueError, KeyError, TypeError) as exc:
            status = 'UNKNOWN/INCOMPLETE' if isinstance(exc, IncompleteGraph) else 'DRIFT/INCOMPLETE'
            print(API.redact('%s: %s; replay %s' % (status, exc, op), client._key))
            return 3
        print('CONFIRMED ' + op)
        return 0
