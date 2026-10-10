"""Read-only audit through the one fake Linear transport; no network."""
import ast
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'skills/hanig-project/scripts'
SWARM = ROOT / 'skills/hanig-swarm/scripts'
ORCHESTRATE = ROOT / 'skills/hanig-orchestrate/scripts'
for path in (SWARM, ORCHESTRATE, PROJECT):
    sys.path.insert(0, str(path))
import linear_api as API
import linear_sync as LS
import tracker_audit as TA
import report
import merge_unit as MU
import child_environment as CE

KEY = 'linear-test-secret-0123456789'


def connection(nodes, more=False, cursor=None):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}


def selected_fields(query, data):
    """Project fake responses onto these queries' actual GraphQL selections."""
    # Our requests use named fields and arguments, without aliases or fragments.
    while '(' in query:
        query, count = re.subn(r'\([^()]*\)', '', query)
        assert count, 'unbalanced query arguments'
    tokens = iter(re.findall(r'[A-Za-z_][A-Za-z_0-9]*|[{}]', query[query.index('{'):]))
    assert next(tokens) == '{'

    def selection():
        fields = {}
        previous = None
        for token in tokens:
            if token == '}':
                return fields
            if token == '{':
                fields[previous] = selection()
            else:
                previous = token
                fields[token] = None
        raise AssertionError('unclosed query selection')

    def project(value, fields):
        if value is None or fields is None:
            return value
        if isinstance(value, list):
            return [project(item, fields) for item in value]
        return {name: project(value[name], nested) for name, nested in fields.items()}

    return project(data, selection())


class FakeLinear:
    """A read-only variation of f92398a's transport, with paging faults."""
    def __init__(self):
        self.org = {'id': 'workspace', 'name': 'Workspace'}
        self.team = {'id': 'team', 'key': 'ARC', 'name': 'Arc'}
        self.project = {'id': 'project', 'name': 'Project'}
        self.issues = {}
        self.calls = []
        self.fail = None
        self.moved = set()
        self.paging = None
        self.nested = None
        self.marker_paging = None
        self.rate_limit = False

    def add(self, iid, state='unstarted', description='swarm-independent: test', project='project'):
        issue = {'id': iid, 'identifier': 'ARC-' + iid, 'title': 'original', 'trashed': False,
                 'updatedAt': '2026-10-05T00:00:00Z',
                 'description': description, 'archivedAt': None, 'state': {'type': state, 'name': state},
                 'project': {'id': project} if project else None, 'team': {'id': 'team'},
                 'relations': connection([]), 'inverseRelations': connection([])}
        self.issues[iid] = issue
        return issue

    def edge(self, a, b, kind='blocks'):
        rel = {'id': 'r-' + a + '-' + b + '-' + kind, 'type': kind,
               'issue': {k: self.issues[a][k] for k in ('id', 'identifier')},
               'relatedIssue': {k: self.issues[b][k] for k in ('id', 'identifier')}}
        self.issues[a]['relations']['nodes'].append(rel)
        self.issues[b]['inverseRelations']['nodes'].append(rel)

    def __call__(self, body, headers, timeout=None):
        request = json.loads(body)
        q, v = request['query'], request['variables']
        assert not q.lstrip().startswith('mutation'), 'mutation reached transport'
        assert headers['Authorization'] == KEY
        self.calls.append((q, v))
        if self.fail and self.fail in q:
            raise OSError('transport failed ' + KEY)
        if self.rate_limit and 'query ProjectIssues' in q:
            return 429, json.dumps({'errors': [{'message': 'rate limit ' + KEY}]}).encode()
        data = selected_fields(q, self.dispatch(q, v))
        return 200, json.dumps({'data': data}).encode()

    def dispatch(self, q, v):
        if any('query ' + name in q for name in ('IssueBatch', 'StabilityBatch', 'CommentBatch', 'OperationIdentifiers')):
            if 'OperationIdentifiers' in q:
                filters = v['filter']
                if 'or' in filters:
                    # Measured 2026-10-10: Linear flattens these multi-field
                    # entries, returning the whole team, not the named issues.
                    assert all(set(f) == {'team', 'number'} for f in filters['or'])
                    teams = {f['team']['key']['eq'] for f in filters['or']}
                    nodes = [copy.deepcopy(i) for i in self.issues.values()
                             if i['identifier'].rsplit('-', 1)[0] in teams]
                else:
                    assert set(filters) == {'team', 'number'}
                    names = {'%s-%d' % (filters['team']['key']['eq'], number)
                             for number in filters['number']['in']}
                    nodes = [copy.deepcopy(i) for i in self.issues.values() if i['identifier'] in names]
                nodes.sort(key=lambda i: int(i['identifier'].rsplit('-', 1)[1]), reverse=True)
            else:
                nodes = [copy.deepcopy(i) for i in self.issues.values() if i['id'] in v['ids']]
            if 'StabilityBatch' in q:
                for node in nodes:
                    if node['id'] in self.moved:
                        node['updatedAt'] = '2026-10-05T01:00:00Z'
                nodes = [{k: node[k] for k in ('id', 'updatedAt')} for node in nodes]
            elif 'CommentBatch' in q:
                nodes = [{'id': node['id'], 'comments': self.dispatch(
                    'query IntentComments', {'id': node['id'], 'after': None})['issue']['comments']} for node in nodes]
                for node in nodes:
                    if not node['comments']['pageInfo']['hasNextPage']:
                        node['comments'] = budget_page(node['comments']['nodes'], None, 20)
            return {'issues': connection(nodes)}
        if 'query IntentComments' in q:
            return {'issue': {'comments': connection([])}}
        if 'query Binding' in q:
            project = dict(self.project, teams=connection([self.team])) if self.project else None
            return {'viewer': {'organization': self.org}, 'project': project}
        if 'query ProjectIssues' in q:
            nodes = [copy.deepcopy(i) for i in self.issues.values() if (i['project'] or {}).get('id') == 'project']
            if 'nodes { id updatedAt }' in q:
                nodes = [{'id': n['id'], 'updatedAt': 'moved' if n['id'] in self.moved else n['updatedAt']} for n in nodes]
            if self.paging == 'duplicate':
                nodes += copy.deepcopy(nodes)
            conn = connection(nodes)
            if self.paging == 'repeat':
                conn = connection([], True, 'same')
            if self.paging == 'pages':
                conn = connection(nodes[1:] if v['after'] else nodes[:1], not v['after'], 'next')
            if self.nested and nodes and 'relations(' in q:
                nodes[0]['relations'] = connection([], True, 'nested')
            return {'project': {'issues': conn}}
        if 'query Relations' in q:
            if self.nested == 'fail':
                raise OSError('unfinished relation page')
            field = 'inverseRelations' if 'inverseRelations(' in q else 'relations'
            return {'issue': {field: connection([])}}
        if 'query Markers' in q:
            assert 'team:' not in q, 'marker search narrowed to a team'
            nodes = [copy.deepcopy(i) for i in self.issues.values() if v['marker'] in i['description']]
            if 'nodes { id updatedAt }' in q:
                nodes = [{'id': n['id'], 'updatedAt': 'moved' if n['id'] in self.moved else n['updatedAt']} for n in nodes]
            if self.marker_paging == 'repeat':
                return {'issues': connection([], True, 'again')}
            if self.marker_paging == 'duplicate':
                nodes += copy.deepcopy(nodes)
            if self.marker_paging == 'pages':
                return {'issues': connection(nodes[1:] if v['after'] else nodes[:1], not v['after'], 'next')}
            return {'issues': connection(nodes)}
        if 'query Issue' in q or 'query Stability' in q:
            issue = next((copy.deepcopy(i) for i in self.issues.values() if v['id'] in (i['id'], i['identifier'])), None)
            if issue and 'query Stability' in q and issue['id'] in self.moved:
                issue['updatedAt'] = '2026-10-05T01:00:00Z'
            return {'issue': issue}
        raise AssertionError('unexpected query: ' + q)


def budget_project(fake):
    """150 project issues, 60 relations, 10 external blockers, 3 overflows."""
    fake.issues.clear()
    for number in range(1, 161):
        fake.add(str(number), project='project' if number <= 150 else 'external',
                 state='unstarted' if number <= 150 else 'completed')
    fake.edge('1', '3')
    fake.edge('1', '3', 'related')
    fake.edge('2', '3')
    for number in range(4, 23):
        fake.edge('1', str(number))
    for number in range(23, 43):
        fake.edge('2', str(number))
    for number in list(range(43, 51)) + list(range(151, 161)):
        fake.edge(str(number), '3')
    assert sum(len(i['relations']['nodes']) for i in fake.issues.values()) == 60
    assert sum(any(len(i[f]['nodes']) > 20 for f in ('relations', 'inverseRelations'))
               for i in fake.issues.values()) == 3


def budget_page(nodes, after, size):
    start = int(after or 0)
    stop = start + size
    return connection(copy.deepcopy(nodes[start:stop]), stop < len(nodes),
                      str(stop) if stop < len(nodes) else None)


class BudgetPaging:
    """Enforce real page limits at the fake transport, never in the reader."""
    def __call__(self, body, headers, timeout=None):
        q = json.loads(body)['query']
        sizes = re.findall(r'issues\([^()]*?\bfirst: (\d+)', q)
        nested = re.findall(r'(?:inverseRelations|relations|comments)\(first: (\d+)', q)
        if sizes:
            assert max(map(int, sizes)) <= 50, q
            if nested:
                assert max(map(int, nested)) <= 20, q
        return super().__call__(body, headers, timeout)

    def dispatch(self, q, v):
        if 'query Relations' in q and str(v.get('after', '')).isdigit():
            field = 'inverseRelations' if 'inverseRelations(' in q else 'relations'
            return {'issue': {field: budget_page(self.issues[v['id']][field]['nodes'], v['after'], 100)}}
        data = super().dispatch(q, v)
        issue = data.get('issue')
        if issue and 'id' in issue and 'relations(' in q:
            for field in ('relations', 'inverseRelations'):
                issue[field] = budget_page(issue[field]['nodes'], None, 20)
        conn = (data.get('project') or {}).get('issues') or data.get('issues')
        if conn is not None:
            paged = budget_page(conn['nodes'], v.get('after'), 50)
            conn.update(paged)
            for node in conn['nodes']:
                for field in ('relations', 'inverseRelations'):
                    if field in node and not node[field]['pageInfo']['hasNextPage']:
                        node[field] = budget_page(node[field]['nodes'], None, 20)
        return data


class BudgetLinear(BudgetPaging, FakeLinear):
    pass


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeLinear()
        self.patch = mock.patch.object(API, 'transport', self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        env = mock.patch.dict(os.environ, {API.KEY_ENV: KEY})
        env.start()
        self.addCleanup(env.stop)
        self.binding = self.root / 'binding.json'
        self.binding.write_text(json.dumps({'schema_version': 1, 'repository': 'owner/fork',
                                           'workspace': self.fake.org, 'team': self.fake.team,
                                           'project': self.fake.project}))
        self.out = self.root / 'audit.json'
        self.plan = self.root / 'plan.json'
        self.draft = self.root / 'tickets.json'
        self.state = self.root / 'state'
        self.state.mkdir()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = LS.main(list(args))
        self.stdout, self.stderr = out.getvalue(), err.getvalue()
        self.assertNotIn(KEY, self.stdout + self.stderr)
        return rc

    def audit(self, plan=False, state=False):
        args = ['--draft', str(self.draft), '--plan', str(self.plan)] if plan else ['--binding', str(self.binding)]
        if state:
            args += ['--state-dir', str(self.state)]
        rc = self.cli('audit', *args, '--out', str(self.out))
        self.assertIn(rc, (0, 1, 3), self.stderr)
        self.record = json.loads(self.out.read_text())
        self.checks = {c['id']: c['verdict'] for c in self.record['checks']}
        self.assertNotIn(KEY, self.out.read_text())
        return rc

    def make_plan(self, needs=None):
        self.plan.write_text(json.dumps({'name': 'sample', 'units': [
            {'id': 'one', 'needs': []}, {'id': 'two', 'needs': ['one'] if needs is None else needs}]}))
        self.draft.write_text(json.dumps({'project': {'linear_id': 'project', 'team': 'Arc'},
                                        'issues': [{'unit': 'one', 'identifier': 'ARC-1'},
                                                   {'unit': 'two', 'identifier': 'ARC-2'}]}))
        self.fake.add('1', description='')
        self.fake.add('2', description='')


class TestAudit(AuditCase):
    def test_identifier_filter_excludes_unrequested_team_issues(self):
        for number in (1396, 1175, 1171, 1464, 1463, 2, 10):
            self.fake.add(str(number))
        self.fake.add('other')['identifier'] = 'OTHER-1396'
        refs = ['ARC-1396', 'ARC-1175', 'ARC-1171', 'ARC-2', 'ARC-10']
        found = LS.Reader(API.Client(KEY)).resolve(refs)
        self.assertEqual({ref: i['identifier'] for ref, i in found.items()},
                         {ref: ref for ref in refs})
        self.assertEqual(len(self.fake.calls), 1)
        query, variables = self.fake.calls[0]
        self.assertEqual(variables, {'filter': {'team': {'key': {'eq': 'ARC'}},
                                              'number': {'in': [2, 10, 1171, 1175, 1396]}}, 'after': None})
        # Exercise the old shape through the same transport: it must overread.
        old_filter = {'or': [{'team': {'key': {'eq': 'ARC'}}, 'number': {'eq': n}}
                             for n in (1396, 1175, 1171)]}
        nodes = API.Client(KEY).query(query, {'filter': old_filter, 'after': None})['issues']['nodes']
        self.assertEqual([i['identifier'] for i in nodes],
                         ['ARC-1464', 'ARC-1463', 'ARC-1396', 'ARC-1175', 'ARC-1171', 'ARC-10', 'ARC-2'])

    def test_missing_requested_identifiers_refuse_instead_of_returning_none(self):
        self.fake.add('1')
        for fields in (None, 'id identifier'):
            for refs in (['ARC-999'], ['ARC-1', 'ARC-999']):
                with self.subTest(fields=fields, refs=refs):
                    reader = LS.Reader(API.Client(KEY))
                    for attempt in range(2):
                        with self.assertRaisesRegex(ValueError,
                                '^issue identifier disagrees on read-back: ARC-999$'):
                            reader.resolve(refs, fields=fields)
        # Derived UUIDs are absence probes before creation, unlike identifiers.
        self.assertEqual(LS.Reader(API.Client(KEY)).resolve(['absent-uuid']), {'absent-uuid': None})

    def test_identifier_batches_group_teams_and_preserve_coverage(self):
        self.fake = BudgetLinear()
        refs = []
        for team in ('ARC', 'OTHER'):
            for number in range(1, 53):
                ref = '%s-%d' % (team, number)
                self.fake.add(ref)['identifier'] = ref
                if number <= 51:
                    refs.append(ref)
        self.fake.add('uuid-only')['identifier'] = 'THIRD-1'
        with mock.patch.object(API, 'transport', self.fake):
            for fields in (None, 'id identifier'):
                with self.subTest(fields=fields):
                    self.fake.calls.clear()
                    reader = LS.Reader(API.Client(KEY))
                    requested = list(reversed(refs)) + refs[:2] + ['uuid-only']
                    found = reader.resolve(requested, fields=fields)
                    self.assertEqual(set(found), set(requested))
                    self.assertEqual({i['id'] for i in found.values()}, set(requested))
                    lookups = [v['filter'] for q, v in self.fake.calls if 'OperationIdentifiers' in q]
                    self.assertEqual(len(self.fake.calls), 5)  # Four team chunks and one UUID batch.
                    self.assertEqual(len(lookups), 4)
                    for team in ('ARC', 'OTHER'):
                        batches = [f['number']['in'] for f in lookups if f['team']['key']['eq'] == team]
                        self.assertEqual(list(map(len, batches)), [50, 1])
                        self.assertEqual(sorted(n for batch in batches for n in batch), list(range(1, 52)))
                    self.assertTrue(all(set(f) == {'team', 'number'} for f in lookups))
                    if fields is None:
                        reader.resolve(requested)
                        self.assertEqual(len(self.fake.calls), 5)

    def test_duplicate_identifiers_with_distinct_ids_are_refused(self):
        self.fake.add('1')
        self.fake.add('duplicate')['identifier'] = 'ARC-1'
        with self.assertRaisesRegex(ValueError, 'issue identifier disagrees on read-back: ARC-1'):
            LS.Reader(API.Client(KEY)).resolve(['ARC-1'])

    def test_overlapping_collections_keep_their_original_stamps(self):
        self.make_plan()
        self.fake.add('1', description='swarm-unit: sample/one')
        self.fake.add('2', description='swarm-unit: sample/two')
        self.fake.edge('1', '2')
        dispatch = self.fake.dispatch
        for final_stamp in ('t0', 't1'):
            with self.subTest(final_stamp=final_stamp):
                def interleaved(q, v):
                    data = dispatch(q, v)
                    if 'query ProjectIssues' in q or 'query Markers' in q:
                        conn = data['issues'] if 'query Markers' in q else data['project']['issues']
                        for issue in conn['nodes']:
                            issue['updatedAt'] = (final_stamp if 'nodes { id updatedAt }' in q else
                                                  't1' if 'query Markers' in q else 't0')
                    return data
                with mock.patch.object(self.fake, 'dispatch', interleaved):
                    self.assertEqual(self.audit(plan=True), 3, self.stdout + self.stderr)
                self.assertFalse(self.record['coverage']['complete'])
                coverage = next(c for c in self.record['checks'] if c['id'] == 'coverage')
                self.assertIn('snapshot moved: 1', json.dumps(coverage))
                self.assertIn('issue changed during read: 1', json.dumps(coverage))

    def test_request_budget(self):
        self.fake = BudgetLinear()
        budget_project(self.fake)
        self.fake.issues['100']['description'] += '\nswarm-unit: sample/extra'
        # Exercise mappings and the workspace-wide marker search too.
        self.plan.write_text(json.dumps({'name': 'sample', 'units': []}))
        self.draft.write_text(json.dumps({'project': {'linear_id': 'project', 'team': 'Arc'},
            'issues': [{'identifier': 'ARC-' + str(n)} for n in range(1, 161)]}))
        with mock.patch.object(API, 'transport', self.fake), mock.patch.object(Path, 'home', return_value=self.root):
            self.assertEqual(self.audit(plan=True), 1)
        self.assertEqual({k for k, v in self.checks.items() if v != 'CLEAN'}, {'misplaced'})
        self.assertTrue(self.record['coverage']['complete'], self.record)
        counts = len(self.fake.calls)
        self.assertEqual(self.record['coverage']['requests'], counts)
        self.assertEqual(sum('query Relations' in q for q, _ in self.fake.calls), 3)
        print('audit budget: %d requests' % counts)
        self.assertLessEqual(counts, 20)

    def test_batches_chunk_ids_identifiers_and_external_stability(self):
        self.fake = BudgetLinear()
        for n in range(120):
            self.fake.add(str(n), project='external')
        for identifiers in (False, True):
            with self.subTest(identifiers=identifiers), mock.patch.object(API, 'transport', self.fake):
                self.fake.calls.clear()
                reader = LS.Reader(API.Client(KEY))
                refs = [i['identifier' if identifiers else 'id'] for i in self.fake.issues.values()]
                found = reader.resolve(refs)
                self.assertEqual({i['id'] for i in found.values()}, set(self.fake.issues))
                self.assertEqual(len(self.fake.calls), 3)
                reader.stable()
                self.assertEqual(reader.problems, [])
                self.assertEqual(len(self.fake.calls), 6)
                reader.resolve(refs)
                self.assertEqual(len(self.fake.calls), 6)
                self.assertTrue(all(len(v.get('ids', v.get('filter', {}).get('number', {}).get('in', []))) <= 50
                                    for _, v in self.fake.calls))

    def test_batch_coverage_faults_are_unknown(self):
        self.fake.add('1', state='started')
        self.fake.add('2', state='completed', project='external')
        self.fake.edge('2', '1')
        dispatch = self.fake.dispatch
        for query in ('IssueBatch', 'StabilityBatch', 'CommentBatch', 'OperationIdentifiers'):
            for fault in ('repeat', 'duplicate', 'unfinished', 'invalid', 'page-error', 'missing', 'wrong-id', 'moved'):
                if fault == 'moved' and query != 'StabilityBatch':
                    continue
                with self.subTest(query=query, fault=fault):
                    self.draft.write_text(json.dumps({'project': {'linear_id': 'project', 'team': 'Arc'},
                        'issues': [{'identifier': 'ARC-2'}] if query == 'OperationIdentifiers' else []}))
                    def broken(q, v):
                        data = dispatch(q, v)
                        if 'query ' + query not in q:
                            return data
                        conn = data['issues']
                        if fault == 'repeat':
                            data['issues'] = connection([], True, 'again')
                        elif fault == 'duplicate':
                            conn['nodes'] += copy.deepcopy(conn['nodes'])
                        elif fault in ('unfinished', 'invalid'):
                            conn['pageInfo'] = {'hasNextPage': True if fault == 'unfinished' else 1, 'endCursor': None}
                        elif fault == 'page-error':
                            if v['after']:
                                raise OSError('batch page failed')
                            data['issues'] = connection([], True, 'next')
                        elif fault == 'missing':
                            conn['nodes'] = []
                        elif fault == 'wrong-id':
                            conn['nodes'][0]['id'] = 'unexpected'
                            conn['nodes'][0]['identifier'] = 'ARC-999'
                        elif fault == 'moved':
                            conn['nodes'][0]['updatedAt'] += '-changed'
                        return data
                    with mock.patch.object(self.fake, 'dispatch', broken):
                        self.assertEqual(self.cli('audit', '--draft', str(self.draft), '--out', str(self.out)), 3)
                    record = json.loads(self.out.read_text())
                    self.assertFalse(record['coverage']['complete'])
                    self.assertNotIn('CLEAN', [c['verdict'] for c in record['checks']])
                    if fault == 'page-error':
                        self.assertIn('batch page failed', json.dumps(record))

    def test_clean_all_checks_and_advisory_precedence(self):
        self.fake.add('1', description='swarm-independent: test\nDepends on ARC-9.')
        self.assertEqual(self.audit(), 0)
        self.assertEqual(self.checks['prose_dependency'], 'ADVISORY')
        self.assertEqual(set(self.checks.values()), {'CLEAN', 'ADVISORY'})
        self.fake.issues['1']['description'] = 'Depends on ARC-9.'
        self.assertEqual(self.audit(), 1)
        self.assertEqual(self.checks['relationless'], 'DRIFT')
        self.fake.moved.add('1')
        self.assertEqual(self.audit(), 3)

    def test_binding_exact_and_fork_never_reads_remote(self):
        with mock.patch.object(LS.subprocess, 'run', side_effect=AssertionError('git remote read')):
            self.audit()
            self.assertEqual(self.checks['binding'], 'CLEAN')
            for field in ('workspace', 'team', 'project'):
                original = json.loads(self.binding.read_text())
                for bad in (original[field]['id'].upper(), original[field]['id'] + '-suffix'):
                    altered = copy.deepcopy(original)
                    altered[field]['id'] = bad
                    self.binding.write_text(json.dumps(altered))
                    self.audit()
                    self.assertEqual(self.checks['binding'], 'DRIFT')
                self.binding.write_text(json.dumps(original))
        self.fake.fail = 'Binding'
        self.audit()
        self.assertEqual(self.checks['binding'], 'UNKNOWN')

    def test_pagination_guards_and_transport_errors(self):
        self.fake.add('1')
        for fault in ('repeat', 'duplicate'):
            self.fake.paging = fault
            self.assertEqual(self.audit(), 3)
            self.assertEqual(self.checks['coverage'], 'UNKNOWN')
        self.fake.paging = None
        self.fake.nested = 'fail'
        self.assertEqual(self.audit(), 3)
        self.fake.nested = 'complete'
        self.assertEqual(self.audit(), 0)
        self.fake.nested = None
        self.fake.fail = 'ProjectIssues'
        self.assertEqual(self.audit(), 3)
        self.fake.fail = None
        self.fake.rate_limit = True
        self.assertEqual(self.audit(), 3)
        self.fake.rate_limit = False
        self.fake.moved.add('1')
        self.assertEqual(self.audit(), 3)
        self.fake.moved.clear()
        self.fake.add('2')
        self.fake.paging = 'pages'
        self.assertEqual(self.audit(), 0)
        self.assertEqual(self.record['coverage']['issues'], 2)

    def test_incomplete_read_leaves_no_check_clean(self):
        self.fake.add('1')
        self.fake.fail = 'ProjectIssues'
        self.assertEqual(self.audit(), 3)
        self.assertNotIn('CLEAN', set(self.checks.values()) - {'ADVISORY'})
        self.fake.fail = None

    def membership_change(self, scope, change):
        self.fake.issues.clear()
        self.fake.calls.clear()
        query = 'query ProjectIssues' if scope == 'project' else 'query Markers'
        description = 'swarm-unit: sample/extra'
        project = 'project' if scope == 'project' else 'elsewhere'
        if scope == 'marker':
            self.draft.write_text(json.dumps({'project': {
                'linear_id': 'project', 'team': 'Arc', 'slug': 'sample'}, 'issues': []}))
        if change != 'empty_add':
            self.fake.add('1', description=description, project=project)
            self.fake.add('2', description=description, project=project)
        dispatch = self.fake.dispatch
        changed = []

        def move(q, variables):
            data = dispatch(q, variables)
            # Change membership only after the entire first collection was
            # returned. Existing issues keep their ids and updatedAt, so an
            # individual issue reread cannot expose removal from the set.
            conn = data['project']['issues'] if query in q and scope == 'project' else data.get('issues')
            if query in q and not conn['pageInfo']['hasNextPage'] and not changed:
                changed.append(True)
                if change in ('add', 'empty_add', 'replace'):
                    self.fake.add('3', description=description, project=project)
                if change in ('remove', 'replace'):
                    if scope == 'project':
                        self.fake.issues['2']['project'] = {'id': 'elsewhere'}
                    else:
                        self.fake.issues['2']['description'] = 'swarm-independent: test'
            return data

        with mock.patch.object(self.fake, 'dispatch', side_effect=move):
            args = ['--binding', str(self.binding)] if scope == 'project' else ['--draft', str(self.draft)]
            rc = self.cli('audit', *args, '--out', str(self.out))
        record = json.loads(self.out.read_text())
        checks = {c['id']: c['verdict'] for c in record['checks']}
        self.assertTrue(changed)
        self.assertEqual(checks['coverage'], 'UNKNOWN')
        self.assertFalse(record['coverage']['complete'])
        self.assertNotIn('CLEAN', checks.values())
        self.assertEqual(rc, 3)
        self.assertIn('snapshot moved: ' + ('project issues' if scope == 'project' else 'marker search'),
                      json.dumps(record['checks']))

    def test_project_membership_changes_during_read(self):
        self.fake.paging = 'pages'
        for change in ('add', 'remove', 'replace', 'empty_add'):
            with self.subTest(change=change):
                self.membership_change('project', change)

    def test_marker_membership_changes_during_read(self):
        self.fake.marker_paging = 'pages'
        for change in ('add', 'remove', 'replace', 'empty_add'):
            with self.subTest(change=change):
                self.membership_change('marker', change)

    def test_membership_rereads_compare_sets_not_order(self):
        self.make_plan(needs=[])
        for issue in self.fake.issues.values():
            issue['description'] = 'swarm-unit: sample/extra'
        self.fake.paging = self.fake.marker_paging = 'pages'
        dispatch = self.fake.dispatch
        reordered = []

        def reorder(q, variables):
            if 'nodes { id updatedAt }' in q and not reordered:
                self.fake.issues = dict(reversed(list(self.fake.issues.items())))
                reordered.append(True)
            return dispatch(q, variables)

        with mock.patch.object(self.fake, 'dispatch', side_effect=reorder):
            self.assertEqual(self.audit(plan=True), 0)
        self.assertEqual(self.checks['coverage'], 'CLEAN')
        self.assertTrue(reordered)
        for query in ('query ProjectIssues', 'query Markers'):
            self.assertEqual(sum(query in q and v['after'] is None for q, v in self.fake.calls), 2)

    def test_membership_reread_also_checks_updated_at(self):
        self.fake.add('1')
        dispatch = self.fake.dispatch

        def change(q, variables):
            data = dispatch(q, variables)
            if 'query ProjectIssues' in q and 'nodes { id updatedAt }' not in q:
                self.fake.issues['1']['updatedAt'] = '2026-10-05T02:00:00Z'
            return data

        with mock.patch.object(self.fake, 'dispatch', side_effect=change):
            self.assertEqual(self.audit(), 3)
        self.assertEqual(self.checks['coverage'], 'UNKNOWN')
        self.assertNotIn('CLEAN', self.checks.values())

    def test_membership_reread_failure_leaves_no_check_clean(self):
        self.make_plan(needs=[])
        dispatch = self.fake.dispatch
        for query in ('query ProjectIssues', 'query Markers'):
            reads = []

            def fail(q, variables):
                if query in q and variables['after'] is None:
                    reads.append(True)
                    if len(reads) == 2:
                        raise OSError('membership reread failed ' + KEY)
                return dispatch(q, variables)

            with self.subTest(query=query), mock.patch.object(self.fake, 'dispatch', side_effect=fail):
                self.assertEqual(self.audit(plan=True), 3)
                self.assertEqual(self.checks['coverage'], 'UNKNOWN')
                self.assertNotIn('CLEAN', self.checks.values())

    def test_page_sizes_stay_within_linear_complexity_measured_2026_10_05(self):
        # 50 issues x 20 nested relations was accepted live; 50 x 50 and
        # 25 x 50 were refused as too complex.
        self.assertLessEqual(LS.ISSUE_PAGE, 50)
        self.assertLessEqual(LS.NESTED_RELATION_PAGE, 20)
        nested = re.findall(r"elations\(first: (\d+)\)", LS.FIELDS)
        self.assertEqual(nested, [str(LS.NESTED_RELATION_PAGE)] * 2)
        seen = []
        real = API.transport

        def spy(body, headers, timeout=None):
            q = json.loads(body.decode())["query"]
            for size in re.findall(r"issues\(first: (\d+)", q):
                seen.append(int(size))
            return real(body, headers, timeout)
        self.fake.add('1')
        with mock.patch.object(API, "transport", spy):
            self.audit()
        self.assertTrue(seen)
        self.assertLessEqual(max(seen), LS.ISSUE_PAGE)

    def test_misplaced_identifiers_markers_receipts_and_unreadable(self):
        self.make_plan(needs=[])
        self.fake.issues['1']['project'] = None
        self.fake.add('3', description='swarm-unit: sample/extra\nswarm-repo: owner/repo', project='elsewhere')['team'] = {'id': 'other'}
        self.assertEqual(self.audit(plan=True), 1)
        self.assertEqual(self.checks['misplaced'], 'DRIFT')
        reads = [v['filter'] for q, v in self.fake.calls if 'query OperationIdentifiers' in q]
        self.assertIn({'team': {'key': {'eq': 'ARC'}}, 'number': {'in': [1]}}, reads)
        self.fake.moved.add('3')
        self.assertEqual(self.audit(plan=True), 3)
        self.fake.moved.clear()
        self.fake.issues.pop('1')
        self.audit(plan=True)
        self.assertEqual(self.checks['misplaced'], 'UNKNOWN')
        self.assertEqual(self.checks['plan_edges'], 'UNKNOWN')
        self.fake.add('1')
        self.fake.add('4', project='elsewhere')
        (self.state / 'outbox-receipts.jsonl').write_text(json.dumps({'ref': 'ARC-4'}) + '\n')
        self.audit(plan=True, state=True)
        self.assertTrue(any(v['filter']['team']['key']['eq'] == 'ARC' and 4 in v['filter']['number']['in']
                            for q, v in self.fake.calls if 'query OperationIdentifiers' in q))

    def test_marker_pagination_and_explicit_draft_workspace(self):
        self.make_plan(needs=[])
        self.fake.issues['1']['description'] = 'swarm-unit: sample/one'
        for fault in ('repeat', 'duplicate'):
            self.fake.marker_paging = fault
            self.assertEqual(self.audit(plan=True), 3)
            self.assertEqual(self.checks['coverage'], 'UNKNOWN')
        self.fake.marker_paging = None
        draft = json.loads(self.draft.read_text())
        draft['workspace'] = {'id': 'WRONG'}
        self.draft.write_text(json.dumps(draft))
        self.audit(plan=True)
        self.assertEqual(self.checks['binding'], 'DRIFT')

    def test_draft_only_marker_search_uses_slug_without_claiming_plan_edges(self):
        self.make_plan(needs=[])
        draft = json.loads(self.draft.read_text())
        draft['project']['slug'] = 'sample'
        self.draft.write_text(json.dumps(draft))
        self.fake.add('3', description='swarm-unit: sample/extra\nswarm-repo: owner/repo', project='other')
        self.assertEqual(self.cli('audit', '--draft', str(self.draft), '--out', str(self.out)), 1)
        record = json.loads(self.out.read_text())
        self.assertIsNone(record['scope']['plan'])
        self.assertEqual(next(c['verdict'] for c in record['checks'] if c['id'] == 'misplaced'), 'DRIFT')

    def test_independence_requires_a_reason_on_its_own_line(self):
        issue = self.fake.add('1', description='swarm-independent:\nDepends on ARC-2.')
        self.audit()
        self.assertEqual(self.checks['relationless'], 'DRIFT')
        issue['description'] = 'swarm-independent: reason'
        self.assertEqual(self.audit(), 0)
        issue['description'] = 'swarm-unit: another-plan/unit'
        self.assertEqual(self.audit(), 0)

    def test_plan_edges_without_state_and_empty_needs(self):
        self.make_plan(needs=[])
        self.assertEqual(self.audit(plan=True), 0)
        self.assertEqual(self.checks['relationless'], 'CLEAN')
        self.assertEqual(self.checks['plan_edges'], 'CLEAN')
        self.fake.edge('1', '2')
        self.assertEqual(self.audit(plan=True), 1)
        self.assertEqual(self.checks['plan_edges'], 'DRIFT')
        self.make_plan()
        self.fake.edge('1', '2', 'related')
        self.assertEqual(self.audit(plan=True), 1)
        self.fake.edge('1', '2')
        self.assertEqual(self.audit(plan=True), 0)

    def test_plan_unit_without_filed_issue_is_drift_in_empty_project(self):
        self.plan.write_text(json.dumps({'name': 'sample', 'units': [{'id': 'u'}]}))
        for issues in ([], [{'unit': 'u', 'identifier': None}]):
            with self.subTest(issues=issues):
                self.draft.write_text(json.dumps({
                    'project': {'linear_id': 'project', 'team': 'Arc'}, 'issues': issues}))
                self.assertEqual(self.audit(plan=True), 1)
                check = next(c for c in self.record['checks'] if c['id'] == 'plan_edges')
                self.assertEqual(check['verdict'], 'DRIFT')
                self.assertIn({'unit': 'u', 'error': 'no filed issue'}, check['evidence'])
                self.assertTrue(self.record['coverage']['complete'])

    def test_plan_edge_with_unmapped_endpoint_is_drift(self):
        for missing in ('one', 'two', 'undeclared'):
            with self.subTest(missing=missing):
                self.make_plan(needs=[missing] if missing == 'undeclared' else None)
                draft = json.loads(self.draft.read_text())
                draft['issues'] = [i for i in draft['issues'] if i['unit'] != missing]
                self.draft.write_text(json.dumps(draft))
                if missing != 'undeclared':
                    self.fake.issues.pop('1' if missing == 'one' else '2')
                self.assertEqual(self.audit(plan=True), 1)
                check = next(c for c in self.record['checks'] if c['id'] == 'plan_edges')
                self.assertEqual(check['verdict'], 'DRIFT')
                self.assertIn({'edge': ['undeclared' if missing == 'undeclared' else 'one', 'two'],
                               'unmapped': [missing], 'error': 'no filed issue'}, check['evidence'])

    def test_plan_unreadable_mapped_endpoint_stays_unknown(self):
        for missing in ('1', '2'):
            with self.subTest(missing=missing):
                self.make_plan()
                self.fake.issues.pop(missing)
                self.assertEqual(self.audit(plan=True), 3)
                check = next(c for c in self.record['checks'] if c['id'] == 'plan_edges')
                self.assertEqual(check['verdict'], 'UNKNOWN')
                self.assertNotIn('no filed issue', json.dumps(check['evidence']))

    def test_plan_marker_mapping_satisfies_missing_draft_mapping(self):
        self.make_plan()
        draft = json.loads(self.draft.read_text())
        draft['issues'] = []
        self.draft.write_text(json.dumps(draft))
        for iid, unit in (('1', 'one'), ('2', 'two')):
            self.fake.issues[iid]['description'] = 'swarm-unit: sample/' + unit + '\nswarm-repo: owner/repo'
        self.fake.edge('1', '2')
        self.assertEqual(self.audit(plan=True), 0)
        self.assertEqual(self.checks['plan_edges'], 'CLEAN')

    def test_audit_identity_requires_complete_terminal_block(self):
        self.make_plan(needs=[])
        draft = json.loads(self.draft.read_text())
        draft['project']['slug'] = 'sample'
        self.draft.write_text(json.dumps(draft))
        for description in ('swarm-unit: sample/extra',
                            'swarm-unit: sample/extra\nswarm-repo: owner/repo\nEnd example'):
            self.fake.add('3', description=description, project='other')
            self.assertEqual(self.cli('audit', '--draft', str(self.draft), '--out', str(self.out)), 0)
            record = json.loads(self.out.read_text())
            self.assertEqual(next(c['verdict'] for c in record['checks'] if c['id'] == 'misplaced'), 'CLEAN')

    def test_cycle_and_blocker_completion_policy(self):
        self.fake.add('1')
        self.fake.add('2', state='started')
        self.fake.edge('1', '2')
        self.assertEqual(self.audit(), 1)
        self.assertEqual(self.checks['blocked_in_progress'], 'DRIFT')
        self.fake.edge('2', '1')
        self.audit()
        self.assertEqual(self.checks['cycle'], 'DRIFT')
        self.fake.issues['1']['relations'] = connection([])
        self.fake.issues['1']['inverseRelations'] = connection([])
        self.fake.issues['2']['relations'] = connection([])
        self.fake.issues['2']['inverseRelations'] = connection([])
        self.fake.edge('1', '2')
        for state, archived, expected in [('completed', None, 'CLEAN'), ('canceled', None, 'UNKNOWN'),
                                          ('duplicate', None, 'UNKNOWN'), ('completed', 'date', 'UNKNOWN')]:
            self.fake.issues['1']['state']['type'] = state
            self.fake.issues['1']['archivedAt'] = archived
            self.audit()
            self.assertEqual(self.checks['blocked_in_progress'], expected)
        self.fake.issues['1']['project'] = {'id': 'outside'}
        self.fake.issues['1']['archivedAt'] = None
        self.fake.moved.add('1')
        self.assertEqual(self.audit(), 3)
        self.assertEqual(self.checks['coverage'], 'UNKNOWN')
        self.fake.moved.clear()
        self.fake.issues.pop('1')
        self.audit()
        self.assertEqual(self.checks['blocked_in_progress'], 'UNKNOWN')

    def test_prose_exclusions_directions_and_relation(self):
        self.fake.add('1', description='\n'.join([
            'swarm-independent: reason', 'Not blocked by ARC-2.', 'Historically depends on ARC-2.',
            '> blocked by ARC-2', '`depends on ARC-2`', '"blocked by ARC-2"',
            '```\ndepends on ARC-2\n```', 'related to ARC-2']))
        self.fake.add('2')
        self.audit()
        self.assertEqual(self.checks['prose_dependency'], 'CLEAN')

        self.fake.issues['1']['description'] += '\nMust land before ARC-2.'
        self.audit()
        evidence = next(c['evidence'] for c in self.record['checks'] if c['id'] == 'prose_dependency')
        self.assertEqual(evidence[0][0]['proposed_blocks'], ['ARC-1', 'ARC-2'])
        self.fake.edge('1', '2', 'related')
        self.audit()
        self.assertEqual(self.checks['prose_dependency'], 'CLEAN')

    def test_prose_relation_to_external_issue_is_not_a_candidate(self):
        self.fake.add('1', description='Depends on ARC-2.')
        self.fake.add('2', project='outside')
        self.fake.edge('1', '2', 'related')
        self.assertEqual(self.audit(), 0)
        self.assertEqual(self.checks['prose_dependency'], 'CLEAN')

    def test_prose_sentence_can_wrap_but_not_cross_an_excluded_line(self):
        issue = self.fake.add('1', description='swarm-independent: reason\nThis depends on\nARC-2.')
        self.audit()
        self.assertEqual(self.checks['prose_dependency'], 'ADVISORY')
        evidence = next(c['evidence'] for c in self.record['checks'] if c['id'] == 'prose_dependency')
        self.assertEqual(evidence[0][0]['proposed_blocks'], ['ARC-2', 'ARC-1'])
        for excluded in ('Not blocked by ARC-2.', 'Historically depends on ARC-2.', '> blocked by ARC-2.'):
            issue['description'] = 'swarm-independent: reason\nDepends on\n' + excluded
            self.audit()
            self.assertEqual(self.checks['prose_dependency'], 'CLEAN')

    def test_swarm_state_and_subprocess_key_boundary(self):
        self.make_plan(needs=[])
        (self.state / 'swarm-state.json').write_text('{}')
        def status(command, **kwargs):
            self.assertEqual(command[2], 'status')
            self.assertNotIn(API.KEY_ENV, kwargs['env'])
            code = 2 if self.coordinator in ('FAILED', 'FAILED_EVIDENCE', 'NEEDS_HUMAN') else 0
            return subprocess.CompletedProcess(command, code, json.dumps({'units': [{'id': 'one', 'state': self.coordinator}]}), '')
        with mock.patch.object(LS.subprocess, 'run', side_effect=status):
            for coord, remote, expected in [('DONE', 'completed', 'CLEAN'), ('DONE', 'started', 'DRIFT'),
                                            ('RUNNING', 'started', 'CLEAN'), ('SUBMITTED', 'unstarted', 'DRIFT'),
                                            ('FAILED', 'completed', 'DRIFT'), ('FAILED_EVIDENCE', 'completed', 'DRIFT'),
                                            ('HELD', 'completed', 'DRIFT'), ('NEEDS_HUMAN', 'completed', 'DRIFT'),
                                            ('PENDING', 'completed', 'CLEAN')]:
                self.coordinator = coord
                self.fake.issues['1']['state']['type'] = remote
                self.audit(plan=True, state=True)
                self.assertEqual(self.checks['swarm_state'], expected)
                self.assertTrue(self.record['coverage']['complete'])
        (self.state / 'swarm-state.json').write_text('not JSON')
        self.audit(plan=True, state=True)
        self.assertEqual(self.checks['swarm_state'], 'UNKNOWN')

    def test_attention_status_keeps_coverage_complete(self):
        self.make_plan(needs=[])
        (self.state / 'swarm-state.json').write_text('{}')
        for code, state, halted in ((0, 'PENDING', None), (1, 'PENDING', 'budget'),
                                    (2, 'FAILED', None)):
            with self.subTest(code=code):
                status = {'units': [{'id': 'one', 'state': state}], 'halted': halted,
                          'needs_attention': ['one'] if code == 2 else []}
                result = subprocess.CompletedProcess([], code, json.dumps(status), '')
                with mock.patch.object(LS.subprocess, 'run', return_value=result):
                    self.assertEqual(self.audit(plan=True, state=True), 0)
                self.assertEqual(self.checks['swarm_state'], 'CLEAN')
                self.assertEqual(self.checks['coverage'], 'CLEAN')
                self.assertTrue(self.record['coverage']['complete'])
                evidence = next(c['evidence'] for c in self.record['checks'] if c['id'] == 'swarm_state')
                self.assertIn({'exit_code': code}, evidence)
                self.assertIn('Linear consistent', TA.render(self.record, self.record['inputs']))

    def test_absent_sources_fixed_and_d2_no_data_redaction(self):
        self.make_plan(needs=[])
        before = self.draft.read_bytes()
        self.audit(plan=True, state=True)
        paths = TA.source_paths(draft=self.draft, plan=self.plan, state_dir=self.state)
        self.assertEqual(set(self.record['inputs']), set(paths))
        absent = str(self.state / 'outbox.jsonl')
        self.assertIsNone(self.record['inputs'][absent])
        (self.state / 'outbox.jsonl').write_text(KEY)
        self.assertIn('STALE', TA.render(self.record, TA.capture(paths)[1]))
        self.assertEqual(self.draft.read_bytes(), before)
        self.assertEqual((self.state / 'outbox.jsonl').read_text(), KEY)

    def test_swarm_state_read_failures_invalidate_all_clean_checks(self):
        self.make_plan(needs=[])
        for fault, code, stdout in (
                ('absent', 0, '{}'), ('oserror', 0, '{}'),
                ('nonzero', 64, '{"units": []}'),
                ('failed_json', 2, 'bad JSON'), ('halted_json', 1, 'bad JSON'),
                ('json', 0, 'bad JSON'), ('shape', 0, '{}'),
                ('units_shape', 2, '{"units": {}}'), ('timeout', 0, '{}')):
            with self.subTest(fault=fault):
                if fault != 'absent':
                    (self.state / 'swarm-state.json').write_text('{}')
                result = subprocess.CompletedProcess([], code, stdout, 'status failure ' + KEY)
                error = (OSError('status unavailable ' + KEY) if fault == 'oserror' else
                         subprocess.TimeoutExpired('status', 60) if fault == 'timeout' else None)
                with mock.patch.object(LS.subprocess, 'run', return_value=result, side_effect=error) as run:
                    self.assertEqual(self.audit(plan=True, state=True), 3)
                    if fault == 'absent':
                        run.assert_not_called()
                self.assertEqual(self.checks['swarm_state'], 'UNKNOWN')
                self.assertEqual(self.checks['coverage'], 'UNKNOWN')
                self.assertFalse(self.record['coverage']['complete'])
                self.assertNotIn('CLEAN', self.checks.values())
                evidence = {c['id']: c['evidence'] for c in self.record['checks']}
                self.assertIn(evidence['swarm_state'][-1], evidence['coverage'][0])
                if fault not in ('absent', 'oserror', 'timeout'):
                    self.assertIn({'exit_code': code}, evidence['swarm_state'])

    def test_bind_and_key_file_shell_words(self):
        old = os.getcwd()
        try:
            os.chdir(str(self.root))
            (self.root / '.git').mkdir()
            self.assertEqual(self.cli('bind', '--project', 'project', '--repository', 'owner/repo'), 0)
            bound = json.loads((self.root / '.hanig/linear-binding.json').read_text())
            self.assertEqual(bound['team']['id'], 'team')
        finally:
            os.chdir(old)
        path = self.root / 'linear.env'
        for line in ['export LINEAR_API_KEY="a b" # comment', "LINEAR_API_KEY='a b' # c"]:
            path.write_text(line)
            path.chmod(0o600)
            self.assertEqual(API.load_key({}, path), 'a b')
        self.assertIn(API.KEY_ENV, CE.DENIED_ENV_NAMES)
        self.assertNotIn(API.KEY_ENV, CE.child_env())

    def test_key_file_mode_600_loads_and_environment_bypasses_file(self):
        path = self.root / 'linear.env'
        path.write_text('LINEAR_API_KEY=' + KEY)
        path.chmod(0o600)
        self.assertEqual(API.load_key({}, path), KEY)
        path.chmod(0o644)
        with mock.patch.object(API.os, 'stat', side_effect=AssertionError('file inspected')):
            self.assertEqual(API.load_key({API.KEY_ENV: ' environment-key '}, path), 'environment-key')
        self.assertIsNone(API.load_key({}, self.root / 'absent.env'))

    def test_cli_key_file_refusal_and_offline_section(self):
        self.assertEqual(self.audit(), 0)
        path = self.root / '.config/hanig/linear.env'
        path.parent.mkdir(parents=True)
        path.write_text('LINEAR_API_KEY=' + KEY)
        path.chmod(0o644)
        env = dict(os.environ, HOME=str(self.root))
        env.pop(API.KEY_ENV, None)
        commands = [
            ('audit', '--binding', str(self.binding)),
            ('bind', '--project', 'project', '--repository', 'owner/repo'),
            ('section', '--audit', str(self.out), '--binding', str(self.binding)),
        ]
        for args in commands:
            with self.subTest(command=args[0]):
                result = subprocess.run([sys.executable, str(PROJECT / 'linear_sync.py'), *args],
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn(KEY, result.stdout + result.stderr)
                if args[0] == 'section':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('Linear consistent', result.stdout)
                    self.assertEqual(result.stderr, '')
                else:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(str(path), result.stderr)
                    self.assertIn('chmod 600', result.stderr)
                    self.assertEqual(result.stdout, '')
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(self.cli(*args), result.returncode)
                self.assertEqual(self.stdout, result.stdout)
                self.assertEqual(self.stderr, result.stderr)
        self.assertFalse((self.root / '.hanig/linear-binding.json').exists())
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(API, 'load_key') as load:
            self.assertEqual(self.cli(*commands[-1]), 0)
            self.assertIn('Linear consistent', self.stdout)
            load.assert_not_called()

    def test_key_file_group_or_world_access_refused_before_read(self):
        path = self.root / 'linear.env'
        path.write_text('LINEAR_API_KEY=' + KEY)
        for mode in (0o640, 0o604, 0o644, 0o620, 0o602, 0o610, 0o601):
            with self.subTest(mode=oct(mode)):
                path.chmod(mode)
                with mock.patch.object(Path, 'read_text') as read:
                    with self.assertRaises(API.LinearError) as caught:
                        API.load_key({}, path)
                    read.assert_not_called()
                self.assertIn(str(path), str(caught.exception))
                self.assertIn('chmod 600 ' + str(path), str(caught.exception))
                self.assertNotIn(KEY, str(caught.exception))

    def test_key_file_foreign_owner_refused_before_read(self):
        path = self.root / 'linear.env'
        path.write_text('LINEAR_API_KEY=' + KEY)
        path.chmod(0o600)
        fields = list(path.stat())
        fields[4] = os.getuid() + 1
        with mock.patch.object(API.os, 'stat', return_value=os.stat_result(fields)), \
                mock.patch.object(Path, 'read_text') as read:
            with self.assertRaises(API.LinearError) as caught:
                API.load_key({}, path)
            read.assert_not_called()
        self.assertIn(str(path), str(caught.exception))
        self.assertIn('chmod 600 ' + str(path), str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))

    def test_output_cannot_overwrite_inputs_and_plan_requires_draft(self):
        before = self.binding.read_bytes()
        for target in (self.binding, self.root / 'alias'):
            if target != self.binding:
                target.symlink_to(self.binding)
            self.assertEqual(self.cli('audit', '--binding', str(self.binding), '--out', str(target)), 2)
            self.assertEqual(self.binding.read_bytes(), before)
        self.assertEqual(self.fake.calls, [])
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            LS.main(['audit', '--binding', str(self.binding), '--plan', str(self.plan)])
        self.assertEqual(caught.exception.code, 2)

    def test_ast_read_only_network_boundaries(self):
        tree = ast.parse((PROJECT / 'linear_sync.py').read_text())
        for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            if function.name in {'confirm_comment', 'reconcile_issue'}:
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertFalse(node.value.lstrip().startswith('mutation'))
        for path in (SWARM / 'swarm.py', PROJECT / 'tickets.py', PROJECT / 'drain_contract.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or '']
                else:
                    continue
                self.assertFalse(any(n.split('.')[0] in {'linear_api', 'linear_sync', 'urllib', 'http', 'socket', 'ssl', 'review', 'committee'} for n in names))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            else:
                continue
            self.assertFalse(any(n.split('.')[0] in {'urllib', 'http', 'socket', 'ssl'} for n in names))
        api_tree = ast.parse((PROJECT / 'linear_api.py').read_text())
        users = [f.name for f in ast.walk(api_tree) if isinstance(f, ast.FunctionDef)
                 and any(isinstance(n, ast.Attribute) and n.attr == 'urlopen' for n in ast.walk(f))]
        self.assertEqual(users, ['transport'])
        self.assertEqual(self.audit(), 0)
        with self.assertRaises(AssertionError):
            self.fake(json.dumps({'query': 'mutation X {}', 'variables': {}}).encode(), {'Authorization': KEY})

    def test_redaction_and_api_failures(self):
        self.fake.fail = 'Binding'
        self.assertEqual(self.audit(), 3)
        self.assertIn('[REDACTED]', self.out.read_text())
        self.assertEqual(self.cli('bind', '--project', 'project', '--repository', 'owner/repo'), 2)
        self.assertIn('[REDACTED]', self.stderr)
        self.fake.fail = None
        self.fake.rate_limit = True
        self.assertEqual(self.audit(), 3)
        self.assertIn('[REDACTED]', self.out.read_text())
        key = 'lin"odd\\key'
        self.assertNotIn(json.dumps(key)[1:-1], API.redact(json.dumps({'message': key}), key))
        self.assertNotIn(KEY, repr(API.Client(KEY)))

    def test_prose_dependency_output_redacts_key_and_preserves_inputs(self):
        for key in (KEY, 'linear-test-odd\\key-\u2603'):
            with self.subTest(key=key), mock.patch(__name__ + '.KEY', key), mock.patch.dict(os.environ, {API.KEY_ENV: key}):
                self.make_plan(needs=[])
                self.fake.issues['1']['description'] = 'Depends on ARC-9 using ' + key + '.'
                draft = json.loads(self.draft.read_text())
                draft['project']['name'] = key
                self.draft.write_text(json.dumps(draft))
                before = self.draft.read_bytes()
                self.assertEqual(self.cli('audit', '--draft', str(self.draft),
                                          '--plan', str(self.plan), '--out', str(self.out)), 0)
                for stream, payload in (('file', self.out.read_text()), ('stdout', self.stdout)):
                    with self.subTest(stream=stream):
                        self.assertNotIn(key, payload)
                        self.assertNotIn(json.dumps(key)[1:-1], payload)
                        record = json.loads(payload)
                        prose = next(c for c in record['checks'] if c['id'] == 'prose_dependency')
                        self.assertEqual(prose['verdict'], 'ADVISORY')
                        self.assertIn('Depends on ARC-9 using [REDACTED].',
                                      prose['evidence'][0][0]['sentence'])
                        self.assertEqual(record['inputs'][str(self.draft)],
                                         hashlib.sha256(before).hexdigest())
                self.assertEqual(self.out.read_text(), self.stdout)
                self.assertEqual(self.draft.read_bytes(), before)

    def test_local_input_write_during_read_invalidates_coverage(self):
        dispatch = self.fake.dispatch
        def change(q, variables):
            if 'query Binding' in q:
                self.binding.write_text(self.binding.read_text() + ' ')
            return dispatch(q, variables)
        self.fake.dispatch = change
        self.assertEqual(self.audit(), 3)
        self.assertEqual(self.checks['coverage'], 'UNKNOWN')

    def test_overlapping_reads_cannot_hide_a_transient_change(self):
        self.make_plan(needs=[])
        self.fake.issues['1']['description'] = 'swarm-unit: sample/one'
        dispatch = self.fake.dispatch
        def change(q, variables):
            data = dispatch(q, variables)
            if 'query Markers' in q and 'relations(' in q:
                data['issues']['nodes'][0]['updatedAt'] = '2026-10-05T02:00:00Z'
            return data
        self.fake.dispatch = change
        self.assertEqual(self.audit(plan=True), 3)
        self.assertEqual(self.checks['coverage'], 'UNKNOWN')

    def test_argument_configuration_guards(self):
        self.make_plan(needs=[])
        plan = json.loads(self.plan.read_text())
        plan['units'][0]['needs'] = 'two'
        self.plan.write_text(json.dumps(plan))
        self.assertEqual(self.cli('audit', '--draft', str(self.draft), '--plan', str(self.plan)), 2)
        self.assertIn('needs must be a list', self.stderr)
        self.assertEqual(self.fake.calls, [])
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            LS.main(['audit', '--binding', str(self.binding), '--state-dir', str(self.state)])
        self.assertEqual(caught.exception.code, 2)

    def test_closed_adhoc_issues_need_no_independence_reason(self):
        for state, archived in [('completed', None), ('canceled', None), ('duplicate', None), ('started', 'date')]:
            with self.subTest(state=state):
                self.fake.add('1', state=state, description='')['archivedAt'] = archived
                self.assertEqual(self.audit(), 0)
                self.assertEqual(self.checks['relationless'], 'CLEAN')


class TestConsumers(AuditCase):
    def variants(self):
        self.audit()
        valid = copy.deepcopy(self.record)
        yield 'clean', valid, 'Linear consistent'
        yield 'absent', None, 'NO AUDIT'
        yield 'invalid', {}, 'INVALID'
        rec = copy.deepcopy(valid); rec['inputs'][str(self.binding)] = '0' * 64
        yield 'input', rec, 'STALE'
        rec = copy.deepcopy(valid); rec['read_started'] = rec['read_finished'] = '2000-01-01T00:00:00Z'
        yield 'age', rec, 'STALE'
        rec = copy.deepcopy(valid); rec['coverage']['complete'] = False
        yield 'incomplete', rec, 'INCOMPLETE'
        for verdict in ('DRIFT', 'UNKNOWN'):
            rec = copy.deepcopy(valid); rec['verdict'] = verdict
            rec['checks'][0]['verdict'] = verdict
            rec['checks'][0]['evidence'] = ['finding']
            yield verdict, rec, verdict
        rec = copy.deepcopy(valid); rec['inputs'][str(self.root / 'other')] = None
        yield 'sources', rec, 'STALE'
        rec = copy.deepcopy(valid); rec['scope']['plan'] = 'plan'
        yield 'missing_edges', rec, 'INVALID'
        rec = copy.deepcopy(valid); rec['read_finished'] = '2999-01-01T00:00:00Z'
        yield 'future', rec, 'INVALID'
        rec = copy.deepcopy(valid); rec['checks'][0]['verdict'] = 'UNKNOWN'
        yield 'precedence', rec, 'INVALID'

    def test_all_three_consumers_require_every_clean_condition(self):
        binding_dir = self.root / '.hanig'
        binding_dir.mkdir()
        binding = binding_dir / 'linear-binding.json'
        binding.write_bytes(self.binding.read_bytes())
        self.binding = binding
        self.plan.write_text('{"name":"sample","units":[]}')
        args = mock.Mock(plan=str(self.plan), state_dir=str(self.state))
        for name, record, expected in self.variants():
            with self.subTest(variant=name):
                if record is None:
                    self.out.unlink(missing_ok=True)
                else:
                    self.out.write_text(json.dumps(record))
                self.assertEqual(self.cli('section', '--audit', str(self.out), '--binding', str(self.binding)), 0)
                self.assertIn(expected, self.stdout)
                out, err = io.StringIO(), io.StringIO()
                data = {'project': str(self.root), 'state_dir': str(self.state), 'runs_root': '', 'plan': {'units': []},
                        'state': {}, 'receipts': {}, 'evidence': {}, 'tickets': {}, 'outbox': [], 'survey': {},
                        'findings': None, 'brief': {}}
                with mock.patch.object(report, 'collect', return_value=data), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(report.main([str(self.root), '--tracker-audit', str(self.out), '--json']), 0)
                self.assertIn(expected, json.loads(out.getvalue())['tracker_section'])
                with mock.patch.object(report, 'collect', return_value=data), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    html = self.root / 'report.html'
                    self.assertEqual(report.main([str(self.root), '--tracker-audit', str(self.out), '--out', str(html)]), 0)
                self.assertIn(expected, html.read_text())
                commands = []
                def child(command, **kwargs):
                    self.assertEqual(kwargs['env'][API.KEY_ENV], KEY)
                    self.assertEqual(Path(command[1]).name, 'linear_sync.py')
                    commands.append(command[2])
                    if command[2] == 'audit':
                        Path(command[-1]).write_text(json.dumps(record) if record is not None else 'null')
                    return subprocess.CompletedProcess(command, 0, '', '')
                out = io.StringIO()
                with mock.patch.object(MU.subprocess, 'run', side_effect=child), contextlib.redirect_stdout(out):
                    MU.print_tracker_audit(args)
                self.assertIn(expected, out.getvalue())
                self.assertEqual(commands, ['audit', 'section'])
                if name != 'clean':
                    self.assertNotIn('Linear consistent', out.getvalue())

    def test_schema_validation_guards(self):
        self.audit()
        original = copy.deepcopy(self.record)
        changes = [
            ('schema_version', True), ('schema_version', 2), ('verdict', 'OK'),
            ('read_started', '2999-01-01T00:00:00Z'), ('read_finished', 'bad'),
            ('read_finished', '2026-10-05T00:00:00'),
            ('read_finished', TA.timestamp(original['read_finished']).astimezone(dt.timezone(dt.timedelta(hours=1))).isoformat()),
            ('scope', dict(original['scope'], workspace='')),
            ('scope', dict(original['scope'], team=3)),
            ('scope', dict(original['scope'], project=None)),
            ('scope', dict(original['scope'], plan=3)),
            ('scope', dict(original['scope'], repository=[])),
            ('inputs', {}), ('inputs', {'': None}), ('inputs', {'x': 'bad'}),
            ('coverage', dict(original['coverage'], complete=1)),
            ('coverage', dict(original['coverage'], pages=-1)),
            ('coverage', dict(original['coverage'], issues=True)),
            ('checks', {}), ('checks', original['checks'] * 2),
            ('checks', original['checks'][1:]),
            ('checks', [dict(c, evidence=None) for c in original['checks']]),
            ('checks', [dict(c, verdict='BOGUS') for c in original['checks']]),
            ('checks', [dict(c, verdict='ADVISORY') for c in original['checks']]),
        ]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                record = copy.deepcopy(original)
                record[field] = value
                self.assertIn('INVALID', TA.render(record, original['inputs']))
        self.assertIn('Linear consistent', TA.render(original, original['inputs'],
                      now=TA.timestamp(original['read_finished']) + dt.timedelta(seconds=900)))
        self.assertIn('STALE', TA.render(original, dict(original['inputs'], extra=None)))

    def test_no_audit_report_html_is_explicit(self):
        data = {'project': str(self.root), 'state_dir': str(self.state), 'plan': {'units': []},
                'state': {}, 'receipts': {}, 'evidence': {}, 'tickets': {}, 'outbox': [], 'survey': {},
                'findings': None, 'brief': {}}
        self.assertIn('Tracker: UNKNOWN (no audit)', report.render(data))

    def test_merge_unavailability_preserves_exit_and_order(self):
        self.plan.write_text('{}')
        arguments = [str(self.plan), '--state-dir', str(self.state), '--unit', 'u', '--pr', '1', '--approver', 'owner']
        for mode in ('no_binding', 'no_key', 'failed_audit'):
            with self.subTest(mode=mode):
                root = self.root / '.hanig'; root.mkdir(exist_ok=True)
                target = root / 'linear-binding.json'
                if mode != 'no_binding':
                    target.write_bytes(self.binding.read_bytes())
                events = []
                def child(command, **kwargs):
                    events.append('audit')
                    return subprocess.CompletedProcess(command, 2, '', 'failure ' + KEY)
                out = io.StringIO()
                with mock.patch.object(MU, 'authority'), mock.patch.object(MU.S, 'acquire_lease', return_value=(True, None)), mock.patch.object(MU.S, 'release_lease'), mock.patch.object(MU, 'reconcile', return_value=['advance']), mock.patch.object(MU, 'run', side_effect=lambda c: (events.append('advance') or subprocess.CompletedProcess(c, 0, '', ''))), mock.patch.object(MU, 'print_pending_close', side_effect=lambda *a: events.append('pending')), mock.patch.object(API, 'load_key', return_value=None if mode == 'no_key' else KEY), mock.patch.object(MU.subprocess, 'run', side_effect=child), contextlib.redirect_stdout(out):
                    self.assertEqual(MU.main(arguments), 0)
                self.assertIn('Tracker: UNAVAILABLE', out.getvalue())
                self.assertNotIn(KEY, out.getvalue())
                self.assertEqual(events[:2], ['advance', 'pending'])
        def child(command, **kwargs):
            self.assertNotIn(API.KEY_ENV, kwargs['env'])
            return subprocess.CompletedProcess(command, 0, '', '')
        with mock.patch.object(MU.subprocess, 'run', side_effect=child), contextlib.redirect_stdout(io.StringIO()):
            MU.run(['gh', 'pr', 'view'])
            MU.run([sys.executable, MU.SWARM, 'status'])


if __name__ == '__main__':
    unittest.main()
