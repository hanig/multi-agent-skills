"""Offline audit input capture, validation and the shared tracker renderer."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re

CHECKS = {'binding', 'coverage', 'misplaced', 'relationless',
          'prose_dependency', 'cycle', 'blocked_in_progress', 'intent_order',
          'declared_edges', 'op_incomplete'}
VERDICTS = {'CLEAN', 'DRIFT', 'UNKNOWN'}
STATE_SOURCES = ('swarm-state.json', 'outbox.jsonl', 'outbox-receipts.jsonl')


def operation_directory(workspace, project):
    if any(not isinstance(v, str) or not re.fullmatch('[A-Za-z0-9-]+', v)
           for v in (workspace, project)):
        raise ValueError('invalid operation workspace or project id')
    root = Path(os.environ.get('HANIG_LINEAR_OPS_DIR',
                               str(Path.home() / '.local/state/hanig-swarm/linear-ops')))
    return root / workspace / project


def source_paths(binding=None, draft=None, plan=None, state_dir=None, operation_scope=None):
    paths = [str(Path(p).absolute()) for p in (binding, draft, plan) if p]
    if state_dir:
        paths.extend(str((Path(state_dir) / name).absolute()) for name in STATE_SOURCES)
    if operation_scope is not None:
        directory = operation_directory(operation_scope['workspace'], operation_scope['project'])
        paths.extend(str(p.absolute()) for p in sorted(directory.glob('*.json*')))
    elif binding or draft:
        try:
            config = json.loads(Path(binding or draft).read_bytes())
            workspace = config['workspace']['id']
            project = config['project']['id' if binding else 'linear_id']
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            pass  # Legacy drafts may resolve workspace only from the API.
        else:
            directory = operation_directory(workspace, project)
            paths.extend(str(p.absolute()) for p in sorted(directory.glob('*.json*')))
    return paths


def capture(paths):
    """Digest exactly the bytes used by the reader, including fixed absences."""
    raw, digests = {}, {}
    for path in paths:
        try:
            raw[path] = Path(path).read_bytes()
        except FileNotFoundError:
            raw[path] = None
        digests[path] = (hashlib.sha256(raw[path]).hexdigest()
                         if raw[path] is not None else None)
    return raw, digests


def current_inputs(**kwargs):
    return capture(source_paths(**kwargs))[1]


def verdict(checks):
    values = {c['verdict'] for c in checks}
    return 'UNKNOWN' if 'UNKNOWN' in values else 'DRIFT' if 'DRIFT' in values else 'CLEAN'


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() != dt.timedelta(0):
        raise ValueError('timestamp must be UTC')
    return result


def validate(record):
    if not isinstance(record, dict) or type(record.get('schema_version')) is not int or record['schema_version'] != 1:
        raise ValueError('schema_version')
    start, finish = timestamp(record['read_started']), timestamp(record['read_finished'])
    if start > finish:
        raise ValueError('read interval')
    scope = record['scope']
    for key in ('workspace', 'team', 'project'):
        if not isinstance(scope[key], str) or not scope[key]:
            raise ValueError('scope ' + key)
    for key in ('repository', 'plan'):
        if scope[key] is not None and (not isinstance(scope[key], str) or not scope[key]):
            raise ValueError('scope ' + key)
    inputs = record['inputs']
    if not isinstance(inputs, dict) or not inputs:
        raise ValueError('inputs')
    for path, digest in inputs.items():
        if not isinstance(path, str) or not path or (digest is not None and
                (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest))):
            raise ValueError('input digest')
    coverage = record['coverage']
    if type(coverage['complete']) is not bool:
        raise ValueError('coverage complete')
    for key in ('pages', 'issues'):
        if type(coverage[key]) is not int or coverage[key] < 0:
            raise ValueError('coverage ' + key)
    checks = record['checks']
    if not isinstance(checks, list):
        raise ValueError('checks')
    ids = []
    for check in checks:
        if not isinstance(check, dict) or check['verdict'] not in VERDICTS | {'ADVISORY'}:
            raise ValueError('check verdict')
        if not isinstance(check['id'], str) or not isinstance(check['evidence'], list):
            raise ValueError('check shape')
        if check['verdict'] == 'ADVISORY' and check['id'] != 'prose_dependency':
            raise ValueError('advisory check')
        ids.append(check['id'])
    required = CHECKS | ({'plan_edges'} if scope['plan'] is not None else set())
    if len(ids) != len(set(ids)) or not required.issubset(ids):
        raise ValueError('missing or duplicate checks')
    if record['verdict'] != verdict(checks):
        raise ValueError('verdict precedence')
    return finish


def render(record, current_inputs, now=None, max_age=900):
    if record is None:
        return 'Tracker: NO AUDIT'
    try:
        finish = validate(record)
    except (ValueError, TypeError, KeyError, AttributeError):
        return 'Tracker: INVALID'
    if set(record['inputs']) != set(current_inputs):
        return 'Tracker: STALE: input sources changed'
    for path, digest in record['inputs'].items():
        if current_inputs[path] != digest:
            return 'Tracker: STALE: %s changed' % path
    now = now or dt.datetime.now(dt.timezone.utc)
    age = (now - finish).total_seconds()
    if age < 0:
        return 'Tracker: INVALID (read is in the future)'
    if age > max_age:
        return 'Tracker: STALE: read %ds ago' % age
    if not record['coverage']['complete']:
        return 'Tracker: INCOMPLETE'
    if record['verdict'] == 'UNKNOWN':
        return 'Tracker: UNKNOWN: ' + ', '.join(c['id'] for c in record['checks'] if c['verdict'] == 'UNKNOWN')
    if record['verdict'] == 'DRIFT':
        count = sum(max(1, len(c['evidence'])) for c in record['checks'] if c['verdict'] == 'DRIFT')
        return 'Tracker: DRIFT: %d finding(s)' % count
    scope = ', '.join('%s=%s' % (k, v) for k, v in record['scope'].items() if v is not None)
    return ('Linear consistent with %s as read %s to %s. External writes after '
            'read_finished are outside this claim.' % (scope, record['read_started'], record['read_finished']))


def section(path, **sources):
    try:
        record = json.loads(Path(path).read_bytes()) if path else None
    except FileNotFoundError:
        record = None
    except (OSError, ValueError):
        return 'Tracker: INVALID'
    if record is not None:
        try:
            validate(record)
        except (ValueError, TypeError, KeyError, AttributeError):
            return 'Tracker: INVALID'
    try:
        return render(record, current_inputs(**sources, operation_scope=record.get('scope') if record else None))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 'Tracker: STALE: inputs unreadable'
