"""PR 3 acceptance through the CLI and a stateful fake transport; no network."""
import ast
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import sys
import re
import subprocess
import unittest
from unittest import mock

from tests.test_linear_audit import AuditCase, FakeLinear, KEY, ROOT, connection, BudgetPaging, budget_project, selected_fields

sys.path.insert(0, str(ROOT / 'skills/hanig-project/scripts'))
import linear_api as API
import linear_issue as LI
import linear_sync as LS
import tracker_audit as TA


class Crash(BaseException):
    pass


class IssueLinear(FakeLinear):
    # Independent request fixtures for the issue command's complete query
    # inventory, checked against linear/linear's packages/sdk/src/schema.graphql.
    # Do not import the production query fragments: that would bless regressions.
    page = 'pageInfo { hasNextPage endCursor }'
    relation = 'id type issue { id identifier } relatedIssue { id identifier }'
    fields = ('trashed id identifier title updatedAt description archivedAt state { type name } '
              'project { id } team { id } relations(first: 20) { nodes { %s } %s } '
              'inverseRelations(first: 20) { nodes { %s } %s }' % (relation, page, relation, page))
    shapes = {
        'OperationIssue': [
            'query OperationIssue($id: ID!) { issues(filter: {id: {eq: $id}}, first: 1, '
            'includeArchived: true) { nodes { %s } %s } }' % (fields, page),
            'query OperationIssue($id: ID!) { issues(filter: {id: {eq: $id}}, first: 1) { nodes { id } } }'],
        'OperationIdentifier': [
            'query OperationIdentifier($filter: IssueFilter!) { issues(filter: $filter, first: 1, '
            'includeArchived: true) { nodes { %s } %s } }' % (fields, page)],
        'Binding': ['query Binding($id: String!) { viewer { organization { id name } } '
                    'project(id: $id) { id name teams(first: 100) { nodes { id key name } %s } } }' % page],
        'Teams': ['query Teams($id: String!, $after: String) { project(id: $id) { '
                  'teams(first: 100, after: $after) { nodes { id key name } %s } } }' % page],
        'ProjectIssues': ['query ProjectIssues($id: String!, $after: String) { project(id: $id) { '
                          'issues(first: 50, after: $after, includeArchived: true) { nodes { id updatedAt } %s } } }' % page,
                          'query ProjectIssues($id: String!, $after: String) { project(id: $id) { '
                          'issues(first: 50, after: $after, includeArchived: true) { nodes { %s } %s } } }' % (fields, page)],
        'Relations': [
            'query Relations($id: String!, $after: String) { issue(id: $id) { relations(first: 100, '
            'after: $after) { nodes { %s } %s } } }' % (relation, page),
            'query Relations($id: String!, $after: String) { issue(id: $id) { inverseRelations(first: 100, '
            'after: $after) { nodes { %s } %s } } }' % (relation, page)],
        'IssueBatch': ['query IssueBatch($ids: [ID!]!, $after: String) { issues(filter: {id: {in: $ids}}, '
                       'first: 50, after: $after, includeArchived: true) { nodes { %s } %s } }' % (fields, page)],
        'StabilityBatch': ['query StabilityBatch($ids: [ID!]!, $after: String) { issues(filter: {id: {in: $ids}}, '
                           'first: 50, after: $after, includeArchived: true) { nodes { id updatedAt } %s } }' % page],
        'OperationIdentifiers': ['query OperationIdentifiers($filter: IssueFilter!, $after: String) { '
                                 'issues(filter: $filter, first: 50, after: $after, includeArchived: true) '
                                 '{ nodes { %s } %s } }' % (fields, page)],
        'Stability': ['query Stability($id: String!) { issue(id: $id) { id updatedAt } }'],
        'OperationCreate': ['mutation OperationCreate($input: IssueCreateInput!) { '
                            'issueCreate(input: $input) { success } }'],
        'OperationUpdate': ['mutation OperationUpdate($id: String!, $input: IssueUpdateInput!) { '
                            'issueUpdate(id: $id, input: $input) { success } }'],
        'OperationRelationCreate': ['mutation OperationRelationCreate($input: IssueRelationCreateInput!) { '
                                    'issueRelationCreate(input: $input) { success } }'],
        'OperationRelationDelete': ['mutation OperationRelationDelete($id: String!) { '
                                    'issueRelationDelete(id: $id) { success } }'],
    }

    def __init__(self):
        super().__init__()
        self.mutations = []
        self.reject = None
        self.collision = None
        self.after_mutation = None
        self.ignore_update = False
        self.ignore_delete = False
        self.read_override = None
        self.relation_page_size = None
        self.team_paging = False
        self.tick = 0

    def add(self, *args, **kwargs):
        issue = super().add(*args, **kwargs)
        issue['title'] = 'original'
        issue['trashed'] = False
        return issue

    def dispatch(self, q, v):
        if any('query ' + name in q for name in ('IssueBatch', 'OperationIdentifiers')):
            data = super().dispatch(q, v)
            if self.relation_page_size:
                for node in data['issues']['nodes']:
                    for field in ('relations', 'inverseRelations'):
                        node[field] = self.relation_page(node[field]['nodes'])
            if self.read_override:
                self.read_override(data)
            return data
        if 'query Teams' in q:
            return {'project': {'teams': connection([self.team])}}
        if 'query Relations' in q and self.relation_page_size:
            field = 'inverseRelations' if 'inverseRelations(' in q else 'relations'
            start = int(v['after'])
            return {'issue': {field: self.relation_page(self.issues[v['id']][field]['nodes'], start)}}
        data = super().dispatch(q, v)
        if 'query Binding' in q and self.team_paging:
            data['project']['teams'] = connection([], True, 'teams-next')
        if 'query ProjectIssues' in q:
            if self.relation_page_size and 'relations(' in q:
                for node in data['project']['issues']['nodes']:
                    for field in ('relations', 'inverseRelations'):
                        node[field] = self.relation_page(node[field]['nodes'])
            data['project']['issues']['nodes'] = [i for i in data['project']['issues']['nodes']
                                                 if not self.issues[i['id']].get('trashed')]
        return data

    def relation_page(self, nodes, start=0):
        stop = start + self.relation_page_size
        more = stop < len(nodes)
        return connection(copy.deepcopy(nodes[start:stop]), more, str(stop) if more else None)

    def __call__(self, body, headers, timeout=None):
        q, v = (json.loads(body)[k] for k in ('query', 'variables'))
        assert headers['Authorization'] == KEY
        self.calls.append((q, v))
        if re.search(r'\bissueRelations\s*\([^)]*\bfilter\s*:', q):
            return 200, json.dumps({'errors': [{'message':
                'Unknown argument "filter" on field "Query.issueRelations". Did you mean "after"?'}]}).encode()
        sizes = re.findall(r'issues\(first: (\d+)', q)
        nested = re.findall(r'(?:inverseRelations|relations)\(first: (\d+)', q)
        if sizes and nested and (max(map(int, sizes)) > 50 or max(map(int, nested)) > 20):
            return 200, json.dumps({'errors': [{'message': 'Query too complex'}]}).encode()
        name = re.match(r'(?:query|mutation) (\w+)', q)[1]
        if name in self.shapes:
            assert ' '.join(q.split()) in self.shapes[name], 'unsupported request shape: ' + q
        if self.fail and self.fail in q:
            raise OSError('transport failed ' + KEY)
        if ('issue(id:' in q and not any(v.get('id') in (i['id'], i['identifier']) for i in self.issues.values())):
            return 200, json.dumps({'errors': [{'message': 'Entity not found: Issue'}]}).encode()
        if 'issueRelation(id:' in q and not any(r['id'] == v.get('id') for r in self.relations()):
            return 200, json.dumps({'errors': [{'message': 'Entity not found: IssueRelation'}]}).encode()
        if q.startswith('mutation'):
            self.mutations.append((q, copy.deepcopy(v)))
            if self.collision:
                self.collision(q, v)
            self.mutate(q, v)
            self.tick += 1
            for issue in self.issues.values():
                issue['updatedAt'] = 'tick-%d' % self.tick
            if self.after_mutation:
                self.after_mutation(q, v)
            if self.reject and self.reject in q:
                return 200, json.dumps({'errors': [{'message': 'rejected ' + KEY}]}).encode()
            data = {'success': True}
        elif 'query OperationIssue' in q:
            assert 'issues(filter: {id: {eq: $id}}' in q
            issue = self.issues.get(v['id'])
            if issue and (issue.get('trashed') or issue.get('archivedAt')) and 'includeArchived: true' not in q:
                issue = None
            nodes = [copy.deepcopy(issue)] if issue else []
            for node in nodes:
                if self.relation_page_size:
                    for field in ('relations', 'inverseRelations'):
                        node[field] = self.relation_page(node[field]['nodes'])
                if not re.search(r'\btrashed\b', q):
                    node.pop('trashed', None)
                if not re.search(r'\barchivedAt\b', q):
                    node.pop('archivedAt', None)
            data = {'issues': connection(nodes)}
            if self.read_override:
                self.read_override(data)
        elif 'query OperationIdentifier(' in q:
            assert 'issues(filter: $filter' in q
            assert 'includeArchived: true' in q
            identifier = '%s-%d' % (v['filter']['team']['key']['eq'], v['filter']['number']['eq'])
            data = {'issues': connection([copy.deepcopy(i) for i in self.issues.values()
                                          if i['identifier'] == identifier])}
        elif 'issueRelation(id:' in q:
            data = {'issueRelation': next(copy.deepcopy(r) for r in self.relations() if r['id'] == v['id'])}
        else:
            data = self.dispatch(q, v)
        if not q.startswith('mutation'):
            data = selected_fields(q, data)
        return 200, json.dumps({'data': data}).encode()

    def relations(self):
        return [r for i in self.issues.values() for r in i['relations']['nodes']]

    def mutate(self, q, v):
        if 'OperationRelationCreate' in q:
            p = v['input']
            if any(r['id'] == p['id'] for r in self.relations()):
                return
            self.edge(p['issueId'], p['relatedIssueId'], p['type'])
            self.issues[p['issueId']]['relations']['nodes'][-1]['id'] = p['id']
        elif 'OperationRelationDelete' in q:
            if not self.ignore_delete:
                for i in self.issues.values():
                    for field in ('relations', 'inverseRelations'):
                        i[field]['nodes'] = [r for r in i[field]['nodes'] if r['id'] != v['id']]
        elif 'OperationCreate' in q:
            p = v['input']
            if p['id'] in self.issues:
                return
            i = self.add(p['id'], description=p['description'].rstrip(), project=p['projectId'])
            i['identifier'] = 'ARC-' + str(100 + len(self.issues))
            i['team'] = {'id': p['teamId']}
            i['title'] = p['title']
        elif 'OperationUpdate' in q:
            if not self.ignore_update:
                self.issues[v['id']].update({k: value.rstrip() if k == 'description' else value
                                           for k, value in v['input'].items()})
        else:
            raise AssertionError(q)


class BudgetIssueLinear(BudgetPaging, IssueLinear):
    pass


class IssueCase(AuditCase):
    def setUp(self):
        super().setUp()
        self.patch.stop()
        self.fake = IssueLinear()
        patch = mock.patch.object(API, 'transport', self.fake)
        patch.start()
        self.addCleanup(patch.stop)
        for patch in (mock.patch.object(Path, 'home', return_value=self.root),
                      mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': str(self.root / 'ops')})):
            patch.start()
            self.addCleanup(patch.stop)
        self.body = self.root / 'body.md'
        self.body.write_text('A task')
        self.fake.add('1')
        self.fake.add('2')

    def issue(self, command='new', *args, body=None):
        if body is not None:
            self.body.write_text(body)
        common = ['--binding', str(self.binding)]
        if command != 'replay':
            common += ['--approver', 'owner']
        if command == 'new':
            common += ['--title', 'new task', '--body-file', str(self.body)]
        return self.cli('issue', command, *common, *args)

    def check_rejected_issue_create(self, call):
        before = copy.deepcopy(self.fake.issues)
        self.fake.reject = 'OperationCreate'
        mutate = self.fake.mutate

        def refuse(q, v):
            if 'OperationCreate' not in q:
                mutate(q, v)

        with mock.patch.object(self.fake, 'mutate', refuse):
            self.assertEqual(call(), 3, self.stdout + self.stderr)
        self.assertIn('rejected [REDACTED]', self.stdout)
        self.assertNotIn('CONFIRMED', self.stdout)
        self.assertEqual(set(self.fake.issues), set(before))
        self.assertFalse(any('OperationRelation' in q for q, _ in self.fake.mutations))

    def records(self):
        return sorted((self.root / 'ops').glob('*/*/*.json'))

    def entrypoint(self, *args, scenario='normal'):
        """Execute the script's real __main__ and observe its process exit."""
        fixture = self.root / 'entrypoint.json'
        fixture.write_text(json.dumps(self.fake.issues))
        code = '''
import atexit, json, runpy, sys
from pathlib import Path
from tests.test_linear_issue import API, IssueLinear, ROOT
fixture, scenario, *args = sys.argv[1:]
fake = IssueLinear()
fake.issues = json.loads(Path(fixture).read_text())
project_reads = 0
def transport(body, headers, timeout=None):
    global project_reads
    query = json.loads(body)['query']
    if 'query ProjectIssues' in query:
        project_reads += 1
        # Preparation runs under the real lock; inject before that read.
        if scenario == 'cycle-under-lock' and project_reads == 1:
            fake.edge('2', '1')
    return fake(body, headers, timeout)
API.transport = transport
if scenario == 'postwrite-drift':
    def drift(query, variables):
        if 'OperationCreate' in query:
            fake.issues[variables['input']['id']]['title'] = 'concurrent title'
    fake.after_mutation = drift
def observations():
    Path(fixture).write_text(json.dumps({'calls': fake.calls,
        'mutations': fake.mutations, 'project_reads': project_reads}))
atexit.register(observations)
script = ROOT / 'skills/hanig-project/scripts/linear_sync.py'
sys.argv = [str(script)] + args
runpy.run_path(str(script), run_name='__main__')
'''
        result = subprocess.run([sys.executable, '-c', code, str(fixture), scenario, *args],
                                cwd=str(ROOT), env=dict(os.environ, HOME=str(self.root)),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=30)
        self.stdout, self.stderr = result.stdout, result.stderr
        self.assertNotIn(KEY, self.stdout + self.stderr)
        return result.returncode, json.loads(fixture.read_text())

    def operation(self):
        return self.records()[-1].stem

    def target(self):
        return LI.load_record(self.records()[-1])['target']

    def replay(self):
        return self.issue('replay', self.operation())

    def marked(self, iid, incoming=(), outgoing=(), body='swarm-independent: test\n'):
        self.fake.issues[iid]['description'] = LI.description(body, incoming, outgoing,
                                                            'prior-op', 'prior-owner')

    def check_remote(self, expected='CLEAN'):
        with mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': str(self.root / 'other-host')}):
            self.audit()
            self.assertEqual(self.checks['declared_edges'], expected, self.stdout)
            self.assertEqual(self.checks['op_incomplete'], 'CLEAN')

    def crash_after(self, n, call):
        original = LI.log_step
        count = [0]
        def fail(path, step):
            count[0] += 1
            if count[0] == n:
                raise Crash()
            original(path, step)
        with mock.patch.object(LI, 'log_step', fail), self.assertRaises(Crash):
            call()

    def external_chain(self, middle=True):
        self.fake.add('3', project='external')
        self.fake.add('4', project='external')
        self.fake.edge('2', '3')
        self.fake.edge('4', '1')
        if middle:
            self.fake.edge('3', '4')


class TestIssue(IssueCase):
    def test_archived_observed_sources_and_relation_lookups(self):
        for field, value in (('archivedAt', 'date'), ('trashed', True)):
            for incoming in (True, False):
                with self.subTest(field=field, incoming=incoming):
                    self.fake.issues['1'].update(archivedAt=None, trashed=False)
                    self.fake.issues['1'][field] = value
                    self.assertEqual(self.issue('new', '--blocked-by' if incoming else '--blocks', 'ARC-1'), 0,
                                     self.stdout + self.stderr)
                    op = re.search(r'^operation (\S+)$', self.stdout, re.M)[1]
                    target = LI.load_record(next(p for p in self.records() if p.stem == op))['target']
                    edge = ('1', target) if incoming else (target, '1')
                    self.assertIn(edge, LS.edges_of(list(self.fake.issues.values()))[0])
                    self.assertEqual(self.issue('replay', op), 0, self.stdout + self.stderr)
                    relation = next(r for r in self.fake.relations()
                                    if (r['issue']['id'], r['relatedIssue']['id']) == edge)
                    self.assertEqual(LI.relation_by_id(LS, API.Client(KEY), relation['id'], *edge, endpoint='1'), relation)
                    with self.assertRaises(LI.DeletedIssue):
                        LI.relation_by_id(LS, API.Client(KEY), relation['id'], *edge, endpoint='1', managed=True)

    def test_archived_touched_counterparts_refuse_before_record(self):
        self.marked('1')
        for field, value in (('archivedAt', 'date'), ('trashed', True)):
            self.fake.issues['1'].update(archivedAt=None, trashed=False)
            self.fake.issues['1'][field] = value
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2, self.stdout + self.stderr)
            self.assertIn('issue deleted', self.stderr)
            self.assertEqual(self.records(), [])
            self.assertEqual(self.fake.mutations, [])

    def test_archived_managed_source_after_trailer_write_refuses_relation_write(self):
        self.marked('1')
        def archive(q, v):
            if 'OperationUpdate' in q and v['id'] == '1':
                self.fake.issues['1']['archivedAt'] = 'date'
        self.fake.after_mutation = archive
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3, self.stdout + self.stderr)
        self.assertIn('issue deleted', self.stdout)
        self.assertFalse(any('OperationRelation' in q for q, _ in self.fake.mutations))

    def test_archived_observed_frontier_retains_cycle_edges(self):
        self.external_chain()
        self.fake.issues['3']['archivedAt'] = 'date'
        self.fake.issues['4']['trashed'] = True
        self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocks', 'ARC-2'), 2,
                         self.stdout + self.stderr)
        self.assertIn('resulting blocks cycle', self.stderr)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.fake.mutations, [])

    def test_dependency_examples_are_prose_in_audit_and_prepare(self):
        for prose in ('Example:\n```text\n`swarm-deps: example`\n```',
                      '> `swarm-deps: example`', '`swarm-deps: example`\nEnd of example'):
            with self.subTest(prose=prose):
                self.fake.issues['1']['description'] = prose
                self.assertIsNone(LI.declared_edges(self.fake.issues['1']))
                self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 0,
                                 self.stdout + self.stderr)

    def test_deleted_issue_edit_and_replay_send_no_mutations(self):
        self.fake.ignore_update = True
        self.assertEqual(self.issue('edit', 'ARC-1', '--title', 'Changed'), 3,
                         self.stdout + self.stderr)
        self.fake.ignore_update = False
        op = self.operation()
        writes = copy.deepcopy(self.fake.mutations)
        for field, value in (('trashed', True), ('archivedAt', '2026-10-06T00:00:00Z')):
            with self.subTest(field=field):
                self.fake.issues['1'].update(trashed=False, archivedAt=None)
                self.fake.issues['1'][field] = value
                for args in (('edit', 'ARC-1', '--title', 'Another title'), ('replay', op)):
                    with self.subTest(command=args[0]):
                        self.assertEqual(self.issue(*args), 3 if args[0] == 'replay' else 2, self.stdout + self.stderr)
                        self.assertIn('issue deleted', self.stdout + self.stderr)
                        self.assertEqual(self.fake.mutations, writes)

    def test_new_edit_and_counterpart_descriptions_match_storage(self):
        self.marked('1')
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1',
                                    body='Prose with interior  \nspacing \t\r\n'), 0,
                         self.stdout + self.stderr)
        target = self.target()
        self.assertEqual(self.issue('edit', self.fake.issues[target]['identifier'],
                                    '--body-file', str(self.body), body='Edited \t\r\n'), 0,
                         self.stdout + self.stderr)
        edit_operation = re.search(r'^operation (\S+)$', self.stdout, re.M)[1]
        self.assertTrue(any('OperationCreate' in q for q, _ in self.fake.mutations))
        self.assertTrue(any('OperationUpdate' in q and v['id'] == '1' for q, v in self.fake.mutations))
        for q, v in self.fake.mutations:
            if 'description' in v.get('input', {}):
                body = v['input']['description']
                self.assertEqual(body, body.rstrip(), q)
        self.assertEqual(self.fake.issues[target]['description'],
                         next(v['input']['description'] for q, v in reversed(self.fake.mutations)
                              if 'OperationUpdate' in q and v['id'] == target))
        self.assertEqual(self.issue('replay', edit_operation), 0, self.stdout + self.stderr)

    def test_request_budget(self):
        self.fake = BudgetIssueLinear()
        budget_project(self.fake)
        self.fake.relation_page_size = 20
        with mock.patch.object(API, 'transport', self.fake):
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1', '--blocked-by', 'ARC-2'), 0,
                             self.stdout + self.stderr)
        self.assertEqual(sum('query ProjectIssues' in q and 'relations(' in q and v['after'] is None
                             for q, v in self.fake.calls), 2)
        self.assertGreaterEqual(sum('query Relations' in q for q, _ in self.fake.calls), 6)
        print('issue new budget: %d requests' % len(self.fake.calls))
        self.assertLessEqual(len(self.fake.calls), 42)

    def test_relation_query_rejections_match_linear(self):
        client = API.Client(KEY)
        for arguments in ('filter: {id: {eq: $id}}, first: 1',
                          'first: 1, filter: {id: {eq: $id}}'):
            query = ('query OperationRelation($id: ID!) { issueRelations(%s) '
                     '{ nodes { id } } }' % arguments)
            with self.assertRaises(API.LinearError) as caught:
                client.query(query, {'id': 'absent'})
            self.assertEqual(str(caught.exception),
                'Linear refused the request: Unknown argument "filter" on field "Query.issueRelations". '
                'Did you mean "after"?')
        query = 'query OperationRelation($id: String!) { issueRelation(id: $id) { id } }'
        with self.assertRaisesRegex(API.LinearError, 'Entity not found: IssueRelation'):
            client.query(query, {'id': 'absent'})
        self.fake.edge('1', '2')
        relation = self.fake.relations()[0]
        self.assertEqual(client.query(query, {'id': relation['id']})['issueRelation']['id'], relation['id'])

    def test_relation_creation_confirms_on_first_attempt_from_blocked_pages(self):
        self.fake.relation_page_size = 1
        # The new relation will be on the blocked issue's second inverse page.
        self.fake.edge('1', '2', 'related')
        for command, args in (('new', ('--blocked-by', 'ARC-1', '--blocks', 'ARC-2')),
                              ('edit', ('ARC-1', '--add-blocks', 'ARC-2'))):
            with self.subTest(command=command):
                self.fake.calls.clear()
                self.assertEqual(self.issue(command, *args), 0, self.stdout + self.stderr)
                self.assertTrue(any('query Relations' in q and 'inverseRelations(' in q and
                                    v == {'id': '2', 'after': '1'} for q, v in self.fake.calls))
                self.assertFalse(any(re.search(r'\bissueRelations?\s*\(', q) for q, _ in self.fake.calls))
                self.assertTrue(all(LI.confirmed(path) for path in self.records()))

    def relation_overflow(self):
        self.fake.relation_page_size = 20
        for number in range(3, 24):
            self.fake.add(str(number))
            self.fake.edge(str(number), '2', 'related')
            self.fake.edge('1', str(number), 'related')

    def test_batched_relation_overflow_create(self):
        self.relation_overflow()
        self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocks', 'ARC-2'), 0,
                         self.stdout + self.stderr)
        self.assertTrue(LI.confirmed(self.records()[0]))
        rid = API.derived_id('relation:workspace/project/1/2')
        self.assertEqual(self.fake.issues['2']['inverseRelations']['nodes'][21]['id'], rid)
        # Require overflow reads specifically between the mutation and confirmation.
        created = next(n for n, (q, _) in enumerate(self.fake.calls) if 'OperationRelationCreate' in q)
        self.assertTrue(any('query Relations' in q and 'inverseRelations(' in q and
                            v == {'id': '2', 'after': '20'} for q, v in self.fake.calls[created + 1:]))

    def test_batched_relation_overflow_remove(self):
        self.relation_overflow()
        self.fake.edge('1', '2')
        rid = self.fake.issues['1']['relations']['nodes'][21]['id']
        self.assertEqual(self.issue('edit', 'ARC-1', '--remove-blocks', 'ARC-2'), 0,
                         self.stdout + self.stderr)
        self.assertTrue(any('OperationRelationDelete' in q and v['id'] == rid for q, v in self.fake.calls))
        self.assertFalse(any(r['id'] == rid for r in self.fake.relations()))
        self.assertTrue(all(LI.confirmed(path) for path in self.records()))

    def test_query_inventory_includes_paged_binding_and_external_identifier(self):
        self.fake.team_paging = True
        self.fake.add('3', project='external')
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-3'), 0, self.stdout + self.stderr)
        self.assertTrue(any('query Teams(' in q and v['after'] == 'teams-next' for q, v in self.fake.calls))
        self.assertTrue(any('query OperationIdentifiers(' in q for q, _ in self.fake.calls))

    def test_relation_readback_page_failure_is_incomplete_until_replay(self):
        self.fake.relation_page_size = 1
        self.fake.edge('1', '2', 'related')
        def fail_readback(q, v):
            if 'OperationRelationCreate' in q:
                self.fake.fail = 'query Relations'
        self.fake.after_mutation = fail_readback
        self.assertEqual(self.issue('new', '--blocks', 'ARC-2'), 3, self.stdout + self.stderr)
        self.assertIn('UNKNOWN/INCOMPLETE: incomplete relation read:', self.stdout)
        self.assertFalse(LI.confirmed(self.records()[0]))
        before = copy.deepcopy(self.fake.mutations)
        self.fake.fail = None
        self.assertEqual(self.replay(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.mutations, before)

    def test_relation_lookup_pages_both_sides_and_matches_exact_tuple(self):
        self.fake.relation_page_size = 1
        self.fake.edge('1', '2', 'related')
        self.fake.edge('1', '2')
        relation = self.fake.relations()[-1]
        client = API.Client(KEY)
        for endpoint, field in (('2', 'inverseRelations'), ('1', 'relations')):
            with self.subTest(endpoint=endpoint):
                self.fake.calls.clear()
                self.assertEqual(LI.relation_by_id(LS, client, relation['id'], '1', '2', endpoint), relation)
                self.assertTrue(any('query Relations' in q and field + '(' in q and
                                    v == {'id': endpoint, 'after': '1'} for q, v in self.fake.calls))
                self.assertIsNone(LI.relation_by_id(LS, client, 'absent', '1', '2', endpoint))
                for key in ('id', 'type', 'issue', 'relatedIssue'):
                    old = relation[key]
                    relation[key] = dict(old, id=old['id'] + '-suffix') if isinstance(old, dict) else old.upper()
                    rid = old if key == 'id' else relation['id']
                    self.assertIsNone(LI.relation_by_id(LS, client, rid, '1', '2', endpoint), key)
                    relation[key] = old

    def test_relation_lookup_refuses_incomplete_or_moving_pages(self):
        self.fake.relation_page_size = 1
        self.fake.edge('1', '2', 'related')
        self.fake.edge('1', '2')
        relation = self.fake.relations()[-1]
        client = API.Client(KEY)
        dispatch = self.fake.dispatch
        for endpoint, field in (('2', 'inverseRelations'), ('1', 'relations')):
            for fault in ('error', 'duplicate', 'repeat', 'missing-cursor', 'invalid-info', 'moved', 'deleted'):
                with self.subTest(endpoint=endpoint, fault=fault):
                    def broken(q, v):
                        if 'query StabilityBatch' in q and endpoint in v['ids'] and fault == 'deleted':
                            return {'issues': connection([])}
                        if 'query Relations' not in q or v['id'] != endpoint:
                            return dispatch(q, v)
                        if fault == 'error':
                            raise OSError('relation page failed')
                        if fault == 'duplicate':
                            return {'issue': {field: connection([self.fake.relations()[0]])}}
                        if fault in ('repeat', 'missing-cursor', 'invalid-info'):
                            return {'issue': {field: connection([], 1 if fault == 'invalid-info' else True,
                                                               None if fault == 'missing-cursor' else '1')}}
                        return dispatch(q, v)
                    self.fake.moved = {endpoint} if fault == 'moved' else set()
                    with mock.patch.object(self.fake, 'dispatch', broken), self.assertRaises(LI.IncompleteGraph):
                        LI.relation_by_id(LS, client, relation['id'], '1', '2', endpoint)

    def test_confirmed_line_identifies_target_for_new_edit_and_replay(self):
        self.marked('1')
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 0, self.stdout + self.stderr)
        target = self.target()
        identifier = self.fake.issues[target]['identifier']
        new_op = self.operation()
        self.assertEqual(self.stdout.splitlines(), ['operation ' + new_op, 'CONFIRMED ' + new_op + ' ' + identifier])
        self.assertEqual(self.issue('replay', new_op), 0, self.stdout + self.stderr)
        self.assertEqual(self.stdout.splitlines(), ['operation ' + new_op, 'CONFIRMED ' + new_op + ' ' + identifier])
        # Editing by UUID must still report the server's human-readable identifier.
        self.assertEqual(self.issue('edit', target, '--title', 'edited'), 0, self.stdout + self.stderr)
        edit_op = self.stdout.splitlines()[0].split()[1]
        self.assertEqual(self.stdout.splitlines(), ['operation ' + edit_op, 'CONFIRMED ' + edit_op + ' ' + identifier])
        self.assertEqual(self.issue('replay', edit_op), 0, self.stdout + self.stderr)
        self.assertEqual(self.stdout.splitlines(), ['operation ' + edit_op, 'CONFIRMED ' + edit_op + ' ' + identifier])

    def test_cli_validation_before_key_load_is_one_line_refusal(self):
        cases = [(['--approver', ' ', '--independent', 'reason'],
                  'approver must be a nonblank single line without backticks'),
                 (['--approver', 'owner', '--blocked-by', 'ARC-1', '--independent', 'reason'],
                  'new requires dependencies or an independence reason, exclusively')]
        for flags, message in cases:
            with self.subTest(flags=flags):
                rc, observed = self.entrypoint('issue', 'new', '--binding', str(self.binding),
                    '--title', 'new', '--body-file', str(self.body), *flags)
                self.assertEqual(rc, 2, self.stdout + self.stderr)
                self.assertEqual(self.stderr.splitlines(), ['error: ' + message])
                self.assertEqual(self.stdout, '')
                self.assertEqual(observed['calls'], [])
                self.assertEqual(self.records(), [])

    def test_cli_busy_project_lock_is_one_line_refusal(self):
        with LS.project_lock('workspace', 'project'):
            rc, observed = self.entrypoint('issue', 'new', '--binding', str(self.binding),
                '--approver', 'owner', '--title', 'new', '--body-file', str(self.body),
                '--independent', 'reason')
        self.assertEqual(rc, 2, self.stdout + self.stderr)
        self.assertEqual(self.stderr.splitlines(),
                         ['error: another writer holds the project lock; nothing was applied'])
        self.assertEqual(self.stdout, '')
        self.assertEqual(observed['mutations'], [])
        self.assertEqual(self.records(), [])

    def test_cli_cycle_discovered_under_lock_is_one_line_refusal(self):
        rc, observed = self.entrypoint('issue', 'edit', '--binding', str(self.binding),
            '--approver', 'owner', 'ARC-1', '--add-blocks', 'ARC-2',
            scenario='cycle-under-lock')
        self.assertEqual(rc, 2, self.stdout + self.stderr)
        self.assertEqual(self.stderr.splitlines(), ['error: resulting blocks cycle'])
        self.assertEqual(self.stdout, '')
        self.assertEqual(observed['project_reads'], 2)
        self.assertEqual(observed['mutations'], [])
        self.assertEqual(self.records(), [])

    def test_preflight_multiline_error_is_one_line_and_redacted(self):
        with mock.patch.object(API, 'transport', side_effect=API.LinearError('read refused\n' + KEY)):
            self.assertEqual(self.issue('new', '--independent', 'reason'), 2)
        self.assertEqual(self.stderr.splitlines(), ['error: Linear request failed: read refused [REDACTED]'])
        self.assertEqual(self.stdout, '')
        self.assertEqual(self.records(), [])

    def test_cli_postwrite_drift_keeps_exit_three_and_record(self):
        rc, observed = self.entrypoint('issue', 'new', '--binding', str(self.binding),
            '--approver', 'owner', '--title', 'new', '--body-file', str(self.body),
            '--independent', 'reason', scenario='postwrite-drift')
        self.assertEqual(rc, 3, self.stdout + self.stderr)
        self.assertIn('DRIFT/INCOMPLETE:', self.stdout)
        self.assertIn('read-back differs', self.stdout)
        self.assertEqual(self.stderr, '')
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(observed['mutations']), 1)

    def test_fresh_live_value_refusal_leaves_no_record(self):
        self.marked('1')
        dispatch = self.fake.dispatch
        def changed(q, v):
            data = dispatch(q, v)
            if 'query ProjectIssues' in q and 'relations(' in q:
                self.fake.issues['1']['title'] = 'concurrent title'
                self.fake.issues['1']['updatedAt'] += '-changed'
            return data
        self.fake.dispatch = changed
        for command, args in (('new', ('--blocked-by', 'ARC-1')),
                              ('edit', ('ARC-1', '--title', 'desired'))):
            with self.subTest(command=command):
                rc = self.issue(command, *args)
                self.assertEqual(self.records(), [])
                self.assertEqual(rc, 2, self.stdout + self.stderr)
                self.assertEqual(self.stderr.splitlines(), ['error: incomplete graph read: snapshot moved: 1'])
                self.assertEqual(self.stdout, '')
                self.assertEqual(list((self.root / 'ops').rglob('*.progress.jsonl')), [])
                self.assertEqual(self.fake.mutations, [])

    def test_missing_uuid_and_malformed_references_refuse_without_single_read(self):
        missing = 'b1db2d3e-1234-4567-890a-123456789abc'
        for ref in (missing, 'arc-1', 'ARC-nope', 'not-an-issue'):
            # A single-entity fallback really would yield a GraphQL error.
            with self.assertRaisesRegex(API.LinearError, 'Entity not found'):
                LS.Reader(API.Client(KEY)).issue(ref)
            for command, args in (('new', ('--blocked-by', ref)),
                                  ('edit', (ref, '--title', 'desired'))):
                with self.subTest(ref=ref, command=command):
                    self.fake.calls.clear()
                    self.assertEqual(self.issue(command, *args), 2, self.stdout + self.stderr)
                    self.assertIn('issue not found: ' + ref if ref == missing else
                                  'identifiers are written as TEAM-123', self.stderr)
                    self.assertEqual(len(self.stderr.splitlines()), 1)
                    self.assertNotIn('Entity not found', self.stderr)
                    self.assertFalse(any('issue(id:' in q and v.get('id') == ref
                                         for q, v in self.fake.calls))
                    if ref == missing:
                        self.assertEqual([v for q, v in self.fake.calls if 'query IssueBatch' in q],
                                         [{'ids': [missing], 'after': None}])
                    self.assertEqual(self.records(), [])
                    self.assertEqual(self.fake.mutations, [])

    def test_external_uuid_uses_filtered_read_and_keeps_exact_identity(self):
        iid = 'b1db2d3e-1234-4567-890a-123456789abc'
        peer = self.fake.add(iid, project='foreign', description='external blocker')
        peer['identifier'] = 'ARC-3'
        self.assertEqual(self.issue('new', '--blocked-by', iid), 0, self.stdout + self.stderr)
        self.assertTrue(any('query IssueBatch' in q and v == {'ids': [iid], 'after': None}
                            for q, v in self.fake.calls))
        self.assertFalse(any('query Issue(' in q and v.get('id') == iid for q, v in self.fake.calls))
        self.assertIn((iid, self.target()),
                      {(r['issue']['id'], r['relatedIssue']['id']) for r in self.fake.relations()})

    def test_absent_external_blocker_uses_filtered_read(self):
        iid = 'b1db2d3e-1234-4567-890a-123456789abc'
        self.fake.add(iid, project='foreign')['identifier'] = 'ARC-3'
        self.fake.edge(iid, '1')
        del self.fake.issues[iid]
        self.assertEqual(self.issue('edit', 'ARC-1', '--title', 'desired'), 2)
        self.assertIn('issue not found: ' + iid, self.stderr)
        self.assertFalse(any('issue(id:' in q and v.get('id') == iid for q, v in self.fake.calls))
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])

    def test_malformed_identifier_is_refused_even_if_returned_by_project(self):
        self.fake.issues['1']['identifier'] = 'arc-1'
        self.assertEqual(self.issue('edit', 'arc-1', '--title', 'desired'), 2)
        self.assertIn('identifiers are written as TEAM-123', self.stderr)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.records(), [])

    def test_1_authority_and_dependency_exclusivity_before_network(self):
        for args in ([], ['--independent', 'reason', '--blocked-by', 'ARC-1'],
                     ['--independent', 'reason', '--approver', '   '], ['--independent', '  ']):
            self.assertEqual(self.issue('new', *args), 2, self.stdout)
            self.assertEqual(self.fake.calls, [])
            self.assertEqual(self.records(), [])
        with self.assertRaises(SystemExit) as caught:
            self.cli('issue', 'new', '--binding', str(self.binding), '--title', 't', '--body-stdin', '--independent', 'reason')
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.fake.calls, [])

    def test_2_derived_ids_bidirectional_and_confirmed_replay(self):
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1', '--blocks', 'ARC-2'), 0, self.stdout + self.stderr)
        path = self.records()[0]
        spec = LI.load_record(path)
        target = spec['target']
        self.assertEqual(target, API.derived_id('issue:workspace/project/op/' + path.stem))
        for r in self.fake.relations():
            self.assertEqual(r['id'], API.derived_id('relation:workspace/project/%s/%s' % (r['issue']['id'], r['relatedIssue']['id'])))
        before = copy.deepcopy(self.fake.mutations)
        journal = LI.progress_path(path).read_bytes()
        self.assertEqual(self.replay(), 0, self.stdout)
        self.assertEqual(before, self.fake.mutations)
        self.assertEqual(journal, LI.progress_path(path).read_bytes())
        self.check_remote()

    def test_rejected_issue_create_preserves_original_error(self):
        self.check_rejected_issue_create(lambda: self.issue('new', '--blocked-by', 'ARC-1'))

    def test_rejected_relation_create_preserves_original_error(self):
        self.fake.reject = 'OperationRelationCreate'
        mutate = self.fake.mutate

        def refuse(q, v):
            if 'OperationRelationCreate' not in q:
                mutate(q, v)

        with mock.patch.object(self.fake, 'mutate', refuse):
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3, self.stdout + self.stderr)
        self.assertIn('rejected [REDACTED]', self.stdout)
        self.assertNotIn('CONFIRMED', self.stdout)
        self.assertEqual(self.fake.relations(), [])

    def test_2_rejected_create_requires_operation_marker(self):
        def collision(q, v):
            if 'OperationCreate' in q:
                self.fake.add(v['input']['id'], description='owned by someone else')
        self.fake.collision = collision
        self.fake.reject = 'OperationCreate'
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3)
        self.assertIn('rejected [REDACTED]', self.stdout)
        self.assertFalse(self.fake.relations())
        self.assertEqual(self.replay(), 3)
        self.assertIn('lacks this operation marker', self.stdout)
        self.assertEqual(len(self.fake.mutations), 1)

    def test_2_ambiguous_creates_and_relation_collision(self):
        self.fake.reject = 'Create'
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 0, self.stdout)
        self.fake.reject = 'OperationRelationCreate'
        def collision(q, v):
            if 'OperationRelationCreate' in q:
                self.fake.edge('1', '2', 'related')
                self.fake.issues['1']['relations']['nodes'][-1]['id'] = v['input']['id']
        self.fake.collision = collision
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-2'), 3)
        self.assertIn('rejected [REDACTED]', self.stdout)

    def test_add_collision_preserves_removal_until_replay(self):
        for collision in ('type', 'endpoints'):
            with self.subTest(collision=collision):
                self.fake = IssueLinear()
                for iid in ('1', '2', '3', '4'):
                    self.fake.add(iid)
                self.fake.edge('1', '2')
                original = copy.deepcopy(self.fake.relations()[0])
                rid = API.derived_id('relation:workspace/project/1/3')
                self.fake.edge('1', '3' if collision == 'type' else '4',
                               'related' if collision == 'type' else 'blocks')
                self.fake.relations()[-1]['id'] = rid
                self.fake.reject = 'OperationRelationCreate'
                folder = self.root / collision
                with mock.patch.object(API, 'transport', self.fake), mock.patch.dict(
                        os.environ, {'HANIG_LINEAR_OPS_DIR': str(folder)}):
                    self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocks', 'ARC-3',
                                                '--remove-blocks', 'ARC-2'), 3, self.stdout)
                    self.assertIn('rejected [REDACTED]', self.stdout)
                    self.assertIn(original, self.fake.relations())
                    self.assertFalse(any('OperationRelationDelete' in q for q, _ in self.fake.mutations))
                    path = next(folder.glob('*/*/*.json'))
                    self.assertFalse(LI.confirmed(path))
                    immutable = path.read_bytes()
                    # Repair the colliding tuple in place, preserving any
                    # pre-existing blocks edge recorded by this operation.
                    self.fake.relations()[-1]['id'] = 'repaired-collision'
                    self.fake.reject = None
                    self.assertEqual(self.issue('replay', path.stem), 0, self.stdout)
                    self.assertEqual(path.read_bytes(), immutable)
                    edges, _ = LS.edges_of(list(self.fake.issues.values()))
                    self.assertIn(('1', '3'), edges)
                    self.assertNotIn(('1', '2'), edges)
                    self.assertTrue(LI.confirmed(path))

    def test_additions_confirm_in_one_batch_before_removals(self):
        for fault in (None, 'missing', 'read-failure', 'moving', 'existing-disappears'):
            with self.subTest(fault=fault):
                self.fake = IssueLinear()
                for iid in ('1', '2', '3', '4'):
                    self.fake.add(iid)
                self.fake.edge('1', '2')
                if fault == 'existing-disappears':
                    self.fake.edge('4', '1')
                def after_add(q, v):
                    if 'OperationRelationCreate' not in q:
                        return
                    if fault == 'read-failure':
                        self.fake.fail = 'query IssueBatch'
                    elif fault == 'moving':
                        self.fake.moved.add('3')
                    elif fault == 'existing-disappears' or (fault == 'missing' and v['input']['issueId'] == '4'):
                        self.fake.issues['4']['relations']['nodes'].clear()
                        self.fake.issues['1']['inverseRelations']['nodes'].clear()
                self.fake.after_mutation = after_add
                folder = self.root / str(fault)
                with mock.patch.object(API, 'transport', self.fake), mock.patch.dict(
                        os.environ, {'HANIG_LINEAR_OPS_DIR': str(folder)}):
                    self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocks', 'ARC-3',
                        '--add-blocked-by', 'ARC-4', '--remove-blocks', 'ARC-2'),
                        3 if fault else 0, self.stdout)
                    calls = self.fake.calls
                    last_add = max(i for i, (q, _) in enumerate(calls) if 'OperationRelationCreate' in q)
                    removals = [i for i, (q, _) in enumerate(calls) if 'OperationRelationDelete' in q]
                    stop = removals[0] if removals else len(calls)
                    batches = [v['ids'] for q, v in calls[last_add + 1:stop] if 'query IssueBatch' in q]
                    self.assertEqual(batches, [['1', '3', '4']])
                    path = next(folder.glob('*/*/*.json'))
                    if fault:
                        self.assertEqual(removals, [])
                        self.assertIn(('1', '2'), LS.edges_of(list(self.fake.issues.values()))[0])
                        self.assertFalse(LI.confirmed(path))
                        if fault in ('read-failure', 'moving'):
                            self.assertIn('UNKNOWN/INCOMPLETE:', self.stdout)
                    else:
                        self.assertEqual(len(removals), 1)
                        self.assertTrue(LI.confirmed(path))

    def test_rejected_external_relation_type_remains_drift(self):
        self.fake.add('3', project='external')
        def wrong_type(q, v):
            if 'OperationRelationCreate' in q:
                value = v['input']
                self.fake.edge(value['issueId'], value['relatedIssueId'], 'related')
                self.fake.issues[value['issueId']]['relations']['nodes'][-1]['id'] = value['id']
        self.fake.collision = wrong_type
        self.assertEqual(self.issue('new', '--blocks', 'ARC-3'), 3, self.stdout + self.stderr)
        self.assertIn('DRIFT/INCOMPLETE: rejected relation create: type or endpoints disagree', self.stdout)
        self.assertFalse(LI.confirmed(self.records()[0]))

    def test_3_crash_each_new_step_remote_local_and_replay(self):
        for step in range(1, 6):
            with self.subTest(step=step):
                self.fake = IssueLinear()
                self.fake.add('1'); self.fake.add('2')
                self.marked('1')
                folder = self.root / ('step-' + str(step))
                with mock.patch.object(API, 'transport', self.fake), mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': str(folder)}):
                    self.crash_after(step, lambda: self.issue('new', '--blocked-by', 'ARC-1', '--blocks', 'ARC-2'))
                    path = next(folder.glob('*/*/*.json'))
                    self.check_remote('CLEAN' if step >= 4 else 'DRIFT')
                    self.audit()
                    self.assertEqual(self.checks['op_incomplete'], 'DRIFT')
                    immutable = path.read_bytes()
                    journal = LI.progress_path(path)
                    before_progress = journal.read_bytes() if journal.exists() else b''
                    self.assertEqual(self.issue('replay', path.stem), 0, self.stdout)
                    self.assertEqual(path.read_bytes(), immutable)
                    self.assertTrue(journal.read_bytes().startswith(before_progress))
                    self.assertEqual(len(self.fake.issues), 3)
                    self.assertEqual(len(self.fake.relations()), 2)
                    self.audit()
                    self.assertEqual(self.checks['op_incomplete'], 'CLEAN')

    def test_3_crash_after_durable_record_before_create(self):
        with mock.patch.object(LI, 'apply', side_effect=Crash()), self.assertRaises(Crash):
            self.issue('new', '--blocked-by', 'ARC-1')
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(len(self.records()), 1)
        self.check_remote()
        self.audit()
        self.assertEqual(self.checks['op_incomplete'], 'DRIFT')
        self.assertEqual(self.replay(), 0, self.stdout)
        self.assertEqual(len(self.fake.issues), 3)
        self.assertEqual(sum('OperationCreate' in q for q, _ in self.fake.mutations), 1)

    def test_replay_deleted_after_recorded_creation_never_recreates(self):
        self.crash_after(2, lambda: self.issue('new', '--independent', 'reason'))
        path = self.records()[0]
        target = self.target()
        self.assertFalse(LI.confirmed(path))
        self.assertIn({'step': 'issue:' + target}, LI.progress(path))
        del self.fake.issues[target]
        journal = LI.progress_path(path).read_bytes()
        for recorded in (journal, journal + b'{"step":', b'{"step":"CONFIRMED"}\n{"step":'):
            with self.subTest(progress=recorded):
                self.fake.issues.pop(target, None)
                before = copy.deepcopy(self.fake.mutations)
                LI.progress_path(path).write_bytes(recorded)
                self.assertFalse(LI.confirmed(path))
                self.assertEqual(self.replay(), 3, self.stdout)
                self.assertIn('issue deleted after creation', self.stdout)
                self.assertEqual(self.fake.mutations, before)
                self.assertNotIn(target, self.fake.issues)

    def test_replay_deleted_before_creation_progress_never_recreates(self):
        self.crash_after(1, lambda: self.issue('new', '--independent', 'reason'))
        path = self.records()[0]
        target = self.target()
        immutable = path.read_bytes()
        before = copy.deepcopy(self.fake.mutations)
        created_issue = copy.deepcopy(self.fake.issues[target])
        self.assertEqual(LI.progress(path), [])
        for state in ('trashed', 'archived'):
            with self.subTest(state=state):
                self.fake.issues[target] = copy.deepcopy(created_issue)
                if LI.progress_path(path).exists():
                    LI.progress_path(path).unlink()
                self.fake.issues[target].update(trashed=state == 'trashed',
                    archivedAt='2026-10-05T01:00:00Z' if state == 'archived' else None)
                # The ordinary filtered lookup hides the server's retained issue.
                client = API.Client(KEY)
                query = 'query OperationIssue($id: ID!) { issues(filter: {id: {eq: $id}}, first: 1) { nodes { id } } }'
                self.assertEqual(client.query(query, {'id': target})['issues']['nodes'], [])
                self.assertEqual(self.replay(), 3, self.stdout + self.stderr)
                self.assertIn('issue deleted after creation', self.stdout)
                self.assertEqual(self.fake.mutations, before)
                self.assertEqual(path.read_bytes(), immutable)
                self.assertIn({'step': 'issue:' + target}, LI.progress(path))
                self.assertFalse(LI.confirmed(path))
                # Retain observed creation even if the trash is later purged.
                del self.fake.issues[target]
                self.assertEqual(self.replay(), 3, self.stdout + self.stderr)
                self.assertIn('issue deleted after creation', self.stdout)
                self.assertEqual(self.fake.mutations, before)

    def test_create_readback_preserves_deleted_id_race_after_record(self):
        save = LI.save_record
        def race(path, spec):
            save(path, spec)
            self.fake.add(spec['target'])['trashed'] = True
        with mock.patch.object(LI, 'save_record', race):
            self.assertEqual(self.issue('new', '--independent', 'reason'), 3,
                             self.stdout + self.stderr)
        self.assertIn('issue deleted after creation', self.stdout)
        # The pre-create read precedes the record. A collision after that
        # boundary cannot overwrite the existing id and is caught on read-back.
        self.assertEqual([q.split('(')[0] for q, _ in self.fake.mutations], ['mutation OperationCreate'])
        self.assertTrue(self.fake.issues[self.target()]['trashed'])
        self.assertEqual(self.fake.issues[self.target()]['title'], 'original')
        self.assertEqual(LI.progress(self.records()[0]), [{'step': 'issue:' + self.target()}])


    def test_replay_remembers_creation_read_back_before_later_refusal(self):
        self.crash_after(1, lambda: self.issue('new', '--independent', 'reason'))
        target = self.target()
        self.assertEqual(LI.progress(self.records()[0]), [])
        # Ownership can be read back even when a later component conflicts.
        self.fake.issues[target]['title'] = 'concurrent title'
        before = copy.deepcopy(self.fake.mutations)
        self.assertEqual(self.replay(), 3, self.stdout)
        self.assertIn('managed title changed', self.stdout)
        del self.fake.issues[target]
        self.assertEqual(self.replay(), 3, self.stdout)
        self.assertIn('issue deleted after creation', self.stdout)
        self.assertEqual(self.fake.mutations, before)

    def test_replay_ignores_unrelated_deleted_snapshot_issue(self):
        self.crash_after(2, lambda: self.issue('new', '--independent', 'reason'))
        self.assertIn('2', LI.load_record(self.records()[0])['identities'])
        del self.fake.issues['2']
        before = copy.deepcopy(self.fake.mutations)
        self.assertEqual(self.replay(), 0, self.stdout)
        self.assertEqual(self.replay(), 0, self.stdout)
        self.assertEqual(self.fake.mutations, before)

    def test_creation_read_back_survives_later_counterpart_read_failure(self):
        self.marked('1')
        self.crash_after(1, lambda: self.issue('new', '--blocked-by', 'ARC-1'))
        target = self.target()
        dispatch = self.fake.dispatch
        def fail_counterpart(q, v):
            if 'query ProjectIssues' in q:
                raise OSError('counterpart unavailable')
            return dispatch(q, v)
        self.fake.dispatch = fail_counterpart
        before = copy.deepcopy(self.fake.mutations)
        self.assertEqual(self.replay(), 3, self.stdout)
        self.assertIn('counterpart unavailable', self.stdout)
        self.fake.dispatch = dispatch
        del self.fake.issues[target]
        self.assertEqual(self.replay(), 3, self.stdout)
        self.assertIn('issue deleted after creation', self.stdout)
        self.assertEqual(self.fake.mutations, before)

    def test_replay_reads_current_graph_cycles_and_coverage(self):
        self.assertEqual(self.issue('new', '--independent', 'reason'), 0)
        before = copy.deepcopy(self.fake.mutations)
        del self.fake.issues['2']
        self.fake.add('3'); self.fake.add('4')
        self.fake.edge('3', '4'); self.fake.edge('4', '3')
        self.fake.paging = 'pages'
        self.assertEqual(self.replay(), 3, self.stdout)
        self.assertIn('blocks cycle', self.stdout)
        self.fake.issues['4']['relations']['nodes'].clear()
        self.fake.issues['3']['inverseRelations']['nodes'].clear()
        self.fake.moved.add('3')
        self.assertEqual(self.replay(), 3, self.stdout)
        self.assertIn('incomplete graph read', self.stdout)
        self.assertIn('UNKNOWN/INCOMPLETE:', self.stdout)
        self.fake.moved.clear()
        self.fake.nested = 'fail'
        self.assertEqual(self.replay(), 3, self.stdout)
        self.assertIn('unfinished relation page', self.stdout)
        self.assertIn('UNKNOWN/INCOMPLETE:', self.stdout)
        self.fake.nested = None
        self.assertEqual(self.replay(), 0, self.stdout)
        self.assertEqual(self.fake.mutations, before)

    def test_3_crash_edit_steps_replay_and_external_satisfaction(self):
        self.fake.edge('1', '2')
        self.marked('1', outgoing=['ARC-2'])
        self.marked('2', incoming=['ARC-1'])
        self.crash_after(1, lambda: self.issue('edit', 'ARC-1', '--remove-blocks', 'ARC-2', '--add-blocked-by', 'ARC-2'))
        self.check_remote('DRIFT')
        # Another writer satisfies the desired edge, without using its derived id.
        self.fake.edge('2', '1')
        before = len(self.fake.mutations)
        self.assertEqual(self.replay(), 0, self.stdout)
        self.assertFalse(any('RelationCreate' in q for q, _ in self.fake.mutations[before:]))
        self.assertEqual(len(self.fake.relations()), 1)
        self.check_remote()

    def test_3_crash_after_every_edit_step(self):
        for step in range(1, 6):
            with self.subTest(step=step):
                self.fake = IssueLinear()
                self.fake.add('1'); self.fake.add('2')
                self.fake.edge('1', '2')
                self.marked('1', outgoing=['ARC-2'])
                self.marked('2', incoming=['ARC-1'])
                folder = self.root / ('edit-' + str(step))
                with mock.patch.object(API, 'transport', self.fake), mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': str(folder)}):
                    self.crash_after(step, lambda: self.issue('edit', 'ARC-1', '--remove-blocks', 'ARC-2', '--add-blocked-by', 'ARC-2'))
                    path = next(folder.glob('*/*/*.json'))
                    self.check_remote('CLEAN' if step >= 4 else 'DRIFT')
                    self.audit()
                    self.assertEqual(self.checks['op_incomplete'], 'DRIFT')
                    self.assertEqual(self.issue('replay', path.stem), 0, self.stdout)
                    self.assertEqual(len(self.fake.relations()), 1)
                    self.check_remote()

    def test_3_digest_deletion_confirmed_drift_and_incomplete_conflicts(self):
        self.assertEqual(self.issue('new', '--independent', 'reason'), 0)
        path = self.records()[0]
        raw = path.read_bytes()
        record = json.loads(raw)
        record['spec']['approver'] = 'tampered'
        path.write_text(json.dumps(record))
        count = len(self.fake.mutations)
        self.assertEqual(self.replay(), 3)
        self.assertIn('digest', self.stderr)
        path.write_bytes(raw)
        target = self.target()
        self.fake.issues[target]['title'] = 'newer work'
        self.assertEqual(self.replay(), 3)
        self.assertIn('title', self.stdout)
        self.assertEqual(len(self.fake.mutations), count)
        del self.fake.issues[target]
        self.assertEqual(self.replay(), 3)
        self.assertIn('issue deleted after creation', self.stdout)
        self.assertEqual(len(self.fake.mutations), count)

    def test_3_incomplete_every_managed_value_conflicts_without_writes(self):
        self.crash_after(1, lambda: self.issue('new', '--blocked-by', 'ARC-1'))
        target = self.target()
        original = copy.deepcopy(self.fake.issues[target])
        alterations = [dict(title='newer title'), dict(description='newer body'),
                       dict(project={'id': 'elsewhere'}), dict(team={'id': 'elsewhere'})]
        for change in alterations:
            self.fake.issues[target] = dict(copy.deepcopy(original), **change)
            before = len(self.fake.mutations)
            self.assertEqual(self.replay(), 3, self.stdout)
            self.assertEqual(len(self.fake.mutations), before)
        self.fake.issues[target] = original
        self.assertEqual(self.replay(), 0, self.stdout)

    def test_replay_managed_values_use_newer_covered_snapshot(self):
        self.fake.issues['1']['title'] = 'Old'
        # Leave the immutable edit specification durable but entirely unapplied.
        self.fake.ignore_update = True
        self.crash_after(1, lambda: self.issue('edit', 'ARC-1', '--title', 'Planned'))
        self.fake.ignore_update = False
        path = self.records()[0]
        immutable = path.read_bytes()
        before = copy.deepcopy(self.fake.mutations)
        original = copy.deepcopy(self.fake.issues['1'])
        changes = {
            'title': {'title': 'External'},
            'body': {'description': 'External\nswarm-independent: test'},
            'independence': {'description': 'swarm-independent: External'},
            'trailer': {'description': LI.description('swarm-independent: test', (), (),
                                                     'external-op', 'external-owner')},
            'identifier': {'identifier': 'ARC-999'},
            'project': {'project': {'id': 'external'}},
            'team': {'team': {'id': 'external'}},
            'edges': {},
        }
        for field, change in changes.items():
            with self.subTest(field=field):
                LI.progress_path(path).write_bytes(b'')
                self.fake.mutations = copy.deepcopy(before)
                self.fake.issues['1'] = copy.deepcopy(original)
                self.fake.issues['2']['inverseRelations']['nodes'].clear()
                observed = []
                def interleave(data):
                    # read_override runs after the exact-id response was copied,
                    # before the replay's project/scope read starts.
                    if not observed:
                        observed.append(copy.deepcopy(data['issues']['nodes'][0]))
                        self.fake.issues['1'].update(change, updatedAt='external-change')
                        if field == 'edges':
                            self.fake.edge('1', '2')
                with mock.patch.object(self.fake, 'read_override', interleave):
                    self.assertEqual(self.replay(), 3, self.stdout)
                self.assertEqual(observed[0]['title'], 'Old')
                self.assertIn('DRIFT/INCOMPLETE:', self.stdout)
                expected = ('unrecorded blocks edge' if field == 'edges' else
                            'outside the bound project' if field in ('project', 'team') else field)
                self.assertIn(expected, self.stdout)
                self.assertEqual(self.fake.mutations, before)
                for key, value in change.items():
                    self.assertEqual(self.fake.issues['1'][key], value)
                self.assertEqual(path.read_bytes(), immutable)
                self.assertFalse(LI.confirmed(path))

    def test_3_unchanged_managed_edge_removed_refuses_replay(self):
        self.fake.edge('1', '2')
        self.crash_after(1, lambda: self.issue('edit', 'ARC-1', '--title', 'changed'))
        self.fake.issues['1']['relations']['nodes'].clear()
        self.fake.issues['2']['inverseRelations']['nodes'].clear()
        before = len(self.fake.mutations)
        self.assertEqual(self.replay(), 3)
        self.assertIn('managed edge', self.stdout)
        self.assertEqual(len(self.fake.mutations), before)

    def test_3a_counterpart_prechecks_and_readback(self):
        self.marked('1')
        self.fake.issues['1']['project'] = {'id': 'foreign'}
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])
        self.fake.issues['1']['description'] = 'unmarked external blocker'
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 0, self.stdout)

    def test_3a_counterpart_secret_structure_conflict_and_concurrent_change(self):
        self.marked('1', body='swarm-independent: why\nBlocks: ARC-2\n')
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2)
        self.assertIn('structured', self.stderr)
        self.marked('1', body=KEY)
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2)
        self.assertEqual(self.records(), [])
        self.marked('1')
        def change(q, v):
            if 'OperationRelationCreate' in q:
                self.fake.issues['1']['title'] = 'concurrent counterpart title'
        self.fake.after_mutation = change
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3)
        self.assertIn('title', self.stdout)

    def test_4_structured_grammar_and_warning_only_prose(self):
        for body in ('Blocked by: ARC-2', 'DEPENDS ON: ARC-2', 'blocks: ARC-2', 'blocked by: arc-1'):
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1', body=body), 2, body)
            self.assertEqual(self.records(), [])
        for body in ('Blocked By: ARC-1', 'Depends on: ARC-1\n',
                     '```\nblocked by: ARC-2\n```', '~~~txt\nblocks: ARC-2\n~~~',
                     '> blocked by: ARC-2', '    blocked by: ARC-2', '\tblocks: ARC-2',
                     'blocked by: ARC-2 after release'):
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1', body=body), 0, body + self.stdout + self.stderr)
        self.assertIn('warning:', self.stdout)
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1', '--blocked-by', 'ARC-2',
                                    body='depends on: ARC-1, ARC-2'), 0, self.stdout)

    def test_4a_counterpart_preserves_prose_markers_and_both_removal_directions(self):
        body = 'Prose bytes.\n\n  More bytes.  \n\nswarm-independent: why\n'
        self.marked('1', body=body)
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 0, self.stdout)
        target = self.target()
        mark = LI.trailer(self.fake.issues['1']['description'])
        self.assertEqual(mark['prefix'], body)
        self.assertEqual((mark['op'], mark['approver'], mark['by']), ('prior-op', 'prior-owner', self.operation()))
        self.check_remote()
        self.assertEqual(self.issue('edit', self.fake.issues[target]['identifier'], '--remove-blocked-by', 'ARC-1', '--independent', 'done'), 0, self.stdout)
        self.check_remote()
        self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocks', self.fake.issues[target]['identifier']), 0, self.stdout)
        self.assertEqual(self.issue('edit', 'ARC-1', '--remove-blocks', self.fake.issues[target]['identifier']), 0, self.stdout)
        self.check_remote()

    def test_5_cycles_reversal_and_postwrite_race(self):
        self.fake.edge('1', '2')
        self.assertEqual(self.issue('edit', 'ARC-2', '--add-blocks', 'ARC-1'), 2)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.issue('edit', 'ARC-2', '--add-blocks', 'ARC-1', '--remove-blocked-by', 'ARC-1'), 0, self.stdout)
        self.fake.add('3')
        self.fake.add('4')
        def race(q, v):
            if 'OperationCreate' in q:
                self.fake.edge('3', '4'); self.fake.edge('4', '3')
        self.fake.after_mutation = race
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3)
        self.assertIn('cycle', self.stdout)

    def test_external_three_hop_cycle_refuses_before_write(self):
        self.external_chain()
        self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocks', 'ARC-2'), 2,
                         self.stdout + self.stderr)
        self.assertEqual(self.stderr.splitlines(), ['error: resulting blocks cycle'])
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])

    def test_external_three_hop_cycle_detected_after_write(self):
        self.external_chain(middle=False)
        def race(q, v):
            if 'OperationRelationCreate' in q:
                self.fake.edge('3', '4')
        self.fake.after_mutation = race
        self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocks', 'ARC-2'), 3,
                         self.stdout + self.stderr)
        self.assertIn('blocks cycle DRIFT after write', self.stdout)
        self.assertTrue(self.fake.mutations)
        self.assertFalse(LI.confirmed(self.records()[0]))

    def test_external_relation_page_is_read_before_cycle_check(self):
        self.external_chain()
        def read(data):
            for issue in data['issues']['nodes']:
                if issue['id'] == '3':
                    issue['relations'] = connection([], True, 'external-next')
        dispatch = self.fake.dispatch
        def page(q, v):
            if 'query Relations' in q and v['id'] == '3':
                self.assertEqual(v['after'], 'external-next')
                return {'issue': {'relations': copy.deepcopy(self.fake.issues['3']['relations'])}}
            return dispatch(q, v)
        self.fake.read_override = read
        with mock.patch.object(self.fake, 'dispatch', page):
            self.assertEqual(self.issue('edit', 'ARC-1', '--title', 'changed'), 0,
                             self.stdout + self.stderr)
            self.fake.mutations.clear()
            self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocks', 'ARC-2'), 2,
                             self.stdout + self.stderr)
        self.assertIn('resulting blocks cycle', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertTrue(any('query Relations' in q and v['id'] == '3' for q, v in self.fake.calls))

    def external_coverage_faults(self, after_write):
        self.external_chain()
        original = copy.deepcopy(self.fake.issues)
        for fault, message in (('repeat', 'repeated cursor'), ('page-error', 'external page failed'),
                               ('unreadable', 'issue not found: 3'), ('moved', 'snapshot moved: 3')):
            with self.subTest(fault=fault):
                self.fake.issues = copy.deepcopy(original)
                self.fake.mutations.clear()
                self.fake.calls.clear()
                self.fake.moved.clear()
                records = set(self.records())
                def active():
                    return not after_write or bool(self.fake.mutations)
                def read(data):
                    if not active():
                        return
                    for issue in data['issues']['nodes']:
                        if issue['id'] == '3':
                            if fault == 'unreadable':
                                data['issues']['nodes'] = []
                            elif fault == 'moved':
                                self.fake.moved.add('3')
                            else:
                                issue['relations'] = connection([], True, 'again')
                dispatch = self.fake.dispatch
                def page(q, v):
                    if active() and 'query Relations' in q and v['id'] == '3':
                        if fault == 'page-error':
                            raise OSError('external page failed')
                        return {'issue': {'relations': connection([], True, 'again')}}
                    return dispatch(q, v)
                self.fake.read_override = read
                with mock.patch.object(self.fake, 'dispatch', page):
                    self.assertEqual(self.issue('edit', 'ARC-1', '--title', 'changed'),
                                     3 if after_write else 2, self.stdout + self.stderr)
                if after_write:
                    self.assertIn('UNKNOWN/INCOMPLETE:', self.stdout)
                    self.assertIn(message, self.stdout)
                    paths = set(self.records()) - records
                    self.assertEqual(len(paths), 1)
                    self.assertFalse(LI.confirmed(paths.pop()))
                    self.assertEqual(len(self.fake.mutations), 1)
                else:
                    self.assertIn(message, self.stderr)
                    self.assertEqual(self.fake.mutations, [])
                    self.assertEqual(set(self.records()), records)

    def test_external_coverage_faults_refuse_before_write(self):
        self.external_coverage_faults(after_write=False)

    def test_external_coverage_faults_unknown_after_write(self):
        self.external_coverage_faults(after_write=True)

    def test_external_scope_cap_refuses_before_write(self):
        self.external_chain()
        with mock.patch.object(LI, 'MAX_SCOPE_ISSUES', 3):
            self.assertEqual(self.issue('edit', 'ARC-1', '--title', 'changed'), 2,
                             self.stdout + self.stderr)
        self.assertIn('graph issue limit exceeded', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])

    def test_scope_limit_counts_uuid_and_identifier_as_one_issue(self):
        iid = '12345678-1234-1234-1234-123456789abc'
        peer = self.fake.add(iid, project='external')
        peer['identifier'] = 'ARC-3'
        with mock.patch.object(LI, 'MAX_SCOPE_ISSUES', 3):
            self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocked-by', iid,
                                        '--add-blocked-by', 'ARC-3'), 0, self.stdout + self.stderr)
        self.assertEqual(sum('OperationRelationCreate' in q for q, _ in self.fake.mutations), 1)
        self.assertTrue(LI.confirmed(self.records()[0]))

    def test_external_scope_cap_unknown_after_write(self):
        def race(q, v):
            if 'OperationUpdate' in q:
                self.external_chain()
        self.fake.after_mutation = race
        with mock.patch.object(LI, 'MAX_SCOPE_ISSUES', 3):
            self.assertEqual(self.issue('edit', 'ARC-1', '--title', 'changed'), 3,
                             self.stdout + self.stderr)
        self.assertIn('UNKNOWN/INCOMPLETE:', self.stdout)
        self.assertIn('graph issue limit exceeded', self.stdout)
        self.assertEqual(len(self.fake.mutations), 1)
        self.assertFalse(LI.confirmed(self.records()[0]))

    def test_5_incomplete_coverage_refuses_before_record(self):
        for mode in ('duplicate', 'repeat'):
            self.fake.paging = mode
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2)
            self.assertEqual(self.records(), [])
        self.fake.paging = None
        self.fake.moved.add('1')
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2)
        self.assertEqual(self.fake.mutations, [])
        self.fake.moved.clear()
        self.fake.nested = 'fail'
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2)
        self.assertEqual(self.records(), [])

    def test_6_edit_fields_independence_existing_edges_and_foreign_target(self):
        self.fake.edge('1', '2')
        self.body.write_text('blocks: ARC-2')
        self.assertEqual(self.issue('edit', 'ARC-1', '--title', 'changed', '--body-file', str(self.body), '--clear-independent'), 0, self.stdout)
        self.assertEqual(self.fake.issues['1']['title'], 'changed')
        self.assertNotIn('swarm-independent:', self.fake.issues['1']['description'])
        self.assertEqual(self.issue('edit', 'ARC-1', '--remove-blocks', 'ARC-2'), 2)
        self.body.write_text('new prose')
        self.assertEqual(self.issue('edit', 'ARC-1', '--remove-blocks', 'ARC-2', '--independent', 'standalone', '--body-file', str(self.body)), 0, self.stdout)
        self.assertIn('\nswarm-independent: standalone\n', self.fake.issues['1']['description'])
        self.fake.issues['1']['project'] = {'id': 'foreign'}
        self.assertEqual(self.issue('edit', 'ARC-1', '--title', 'no'), 2)

    def test_6_supplied_body_bytes_and_plain_independence_reason(self):
        body = 'Body\r\n\r\nswarm-independent: reason in the given body\r\n'
        self.body.write_bytes(body.encode('utf-8'))
        self.assertEqual(self.issue('new', '--independent', 'uses `code`'), 0, self.stdout)
        written = self.fake.issues[self.target()]['description']
        self.assertTrue(written.startswith(body), repr(written))
        self.assertIn('\nswarm-independent: uses `code`\n', written)
        self.assertEqual(self.audit(), 0, self.stdout)

    def test_6_removed_unmarked_endpoint_relationless_refused(self):
        self.fake.issues['2']['description'] = ''
        self.fake.edge('1', '2')
        self.assertEqual(self.issue('edit', 'ARC-1', '--remove-blocks', 'ARC-2'), 2)
        self.assertIn('ARC-2', self.stderr)
        self.assertEqual(self.records(), [])
        self.fake.issues['2']['state']['type'] = 'completed'
        self.assertEqual(self.issue('edit', 'ARC-1', '--remove-blocks', 'ARC-2'), 0, self.stdout)

    def test_removed_plan_unit_endpoint_matches_audit_exemption(self):
        for marker in ('swarm-unit: sample/two', '`swarm-unit: sample/two`'):
            with self.subTest(marker=marker):
                self.fake.issues['1']['relations']['nodes'].clear()
                self.fake.issues['2']['inverseRelations']['nodes'].clear()
                self.fake.issues['2']['description'] = marker
                self.fake.edge('1', '2')
                self.assertEqual(self.issue('edit', 'ARC-1', '--remove-blocks', 'ARC-2'), 0,
                                 self.stdout + self.stderr)
                self.assertFalse(self.fake.relations())
                self.assertEqual(self.audit(), 0, self.stdout)
                self.assertEqual(self.checks['relationless'], 'CLEAN')
                # The edited endpoint uses its resulting body for the same rule.
                self.assertEqual(self.issue('edit', 'ARC-2', '--title', 'unit'), 0,
                                 self.stdout + self.stderr)
                self.body.write_text('unit marker removed')
                self.assertEqual(self.issue('edit', 'ARC-2', '--body-file', str(self.body)), 2)
                self.assertIn('relationless open issue: ARC-2', self.stderr)

    def test_missing_identifiers_use_filtered_list_and_refuse_cleanly(self):
        # This fake returns Linear's real GraphQL error for single-entity misses.
        with self.assertRaisesRegex(API.LinearError, 'Entity not found'):
            LS.Reader(API.Client(KEY)).issue('ARC-999')
        self.fake.calls.clear()
        for command, args in (('new', ('--blocked-by', 'ARC-999')),
                              ('edit', ('ARC-999', '--title', 'missing'))):
            with self.subTest(command=command):
                self.assertEqual(self.issue(command, *args), 2, self.stdout + self.stderr)
                self.assertIn('issue not found: ARC-999', self.stderr)
                self.assertNotIn('Entity not found', self.stderr)
        lookups = [v for q, v in self.fake.calls if 'query OperationIdentifier' in q]
        self.assertEqual(lookups, [{'filter': {'or': [{'team': {'key': {'eq': 'ARC'}},
                                                     'number': {'eq': 999}}]}, 'after': None}] * 2)
        self.assertFalse(any('issue(id:' in q and v.get('id') == 'ARC-999'
                             for q, v in self.fake.calls))
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])

    def test_7_body_file_stdin_no_argv_and_secret_never_persisted(self):
        for flag in ('--title', '--independent', '--approver'):
            self.assertEqual(self.issue('new', '--independent', 'why', flag, KEY), 2)
            self.assertEqual(self.fake.mutations, [])
            self.assertEqual(self.records(), [])
        self.assertEqual(self.issue('new', '--independent', 'why', body=KEY), 2)
        self.assertEqual(self.fake.calls, [])
        with mock.patch.object(sys, 'stdin', io.StringIO('from stdin')), mock.patch.object(LS.subprocess, 'run', side_effect=AssertionError('child')):
            self.assertEqual(self.cli('issue', 'new', '--binding', str(self.binding), '--title', 'stdin', '--body-stdin', '--independent', 'why', '--approver', 'owner'), 0, self.stdout)
        self.assertTrue(self.fake.issues[self.target()]['description'].startswith('from stdin'))
        for path in (self.root / 'ops').rglob('*'):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text())
        with self.assertRaises(SystemExit):
            self.issue('new', '--independent', 'why', '--body', 'argv body')

    def test_preview_all_checks_no_files_or_mutations(self):
        before = sorted(str(p) for p in self.root.rglob('*'))
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1', '--preview'), 0, self.stdout)
        self.assertIn('"checks": "passed"', self.stdout)
        self.assertEqual(sorted(str(p) for p in self.root.rglob('*')), before)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1', '--preview', body=KEY), 2)
        with mock.patch.object(API, 'load_key', return_value=None):
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1', '--preview'), 2)
        self.assertEqual(self.records(), [])

    def test_binding_exact_missing_reference_and_shared_lock(self):
        original = self.binding.read_bytes()
        for field in ('workspace', 'project', 'team'):
            binding = json.loads(original)
            binding[field]['id'] = binding[field]['id'].upper()
            self.binding.write_text(json.dumps(binding))
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2)
            self.assertEqual(self.records(), [])
        self.binding.write_bytes(original)
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-99'), 2)
        self.assertIn('issue not found: ARC-99', self.stderr)
        lock = LS.drain_lock_path('workspace', 'project')
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])

    def test_readback_extra_edge_and_unapplied_update_or_delete(self):
        def race(q, v):
            if 'OperationRelationCreate' in q:
                self.fake.edge(v['input']['relatedIssueId'], '2')
        self.fake.after_mutation = race
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3)
        self.assertIn('declared_edges', self.stdout)
        self.check_remote('DRIFT')
        self.fake.after_mutation = None
        self.fake.ignore_update = True
        self.assertEqual(self.issue('edit', 'ARC-1', '--title', 'ignored'), 3)
        self.fake.ignore_update = False
        self.fake.edge('1', '2')
        self.fake.ignore_delete = True
        self.assertEqual(self.issue('edit', 'ARC-1', '--remove-blocks', 'ARC-2'), 3)

    def test_readback_compares_marker_names_and_exact_endpoint_ids(self):
        def rename(q, v):
            if 'OperationRelationCreate' in q:
                self.fake.issues['1']['identifier'] = 'ARC-999'
                for relation in self.fake.relations():
                    for endpoint in ('issue', 'relatedIssue'):
                        if relation[endpoint]['id'] == '1':
                            relation[endpoint]['identifier'] = 'ARC-999'
        self.fake.after_mutation = rename
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3)
        self.assertIn('marker identifiers differ', self.stdout)
        self.check_remote('DRIFT')

    def test_readback_same_display_identifier_different_id_is_drift(self):
        def race(q, v):
            if 'OperationRelationCreate' not in q:
                return
            foreign = self.fake.add('foreign')
            foreign['identifier'] = 'ARC-1'
            relation = self.fake.issues['1']['relations']['nodes'].pop()
            relation['issue']['id'] = 'foreign'
            foreign['relations']['nodes'].append(relation)
        self.fake.after_mutation = race
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3)
        self.assertIn('rejected relation create: type or endpoints disagree', self.stdout)

    def test_operation_storage_fsync_and_outside_git(self):
        real = os.fsync
        with mock.patch.object(LI.os, 'fsync', wraps=real) as fsync:
            self.assertEqual(self.issue('new', '--independent', 'why'), 0)
            self.assertGreaterEqual(fsync.call_count, 5)
        directory = self.root / 'checkout'
        directory.mkdir(); (directory / '.git').write_text('gitdir: elsewhere')
        before = len(self.fake.mutations)
        with mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': str(directory / 'state')}):
            self.assertEqual(self.issue('new', '--independent', 'why'), 2)
        self.assertEqual(len(self.fake.mutations), before)
        self.assertFalse((directory / 'state').exists())

    def test_operation_id_collision_never_overwrites_existing_record(self):
        self.assertEqual(self.issue('new', '--independent', 'why'), 0)
        path = self.records()[0]
        immutable = path.read_bytes()
        before = len(self.fake.mutations)
        with mock.patch.object(LI.uuid, 'uuid4', return_value=LI.uuid.UUID(path.stem)):
            self.assertEqual(self.issue('new', '--independent', 'why', '--title', 'different operation'), 2)
        self.assertEqual(path.read_bytes(), immutable)
        self.assertEqual(len(self.fake.mutations), before)

    def test_record_is_durable_before_first_mutation_and_fsync_failure(self):
        fsynced = []
        observations = []
        real = os.fsync
        def fsync(fd):
            stat = os.fstat(fd)
            fsynced.append((stat.st_ino, stat.st_size))
            real(fd)
        def inspect(q, v):
            path = self.records()[0]
            for p in (path, path.parent):
                stat = p.stat()
                observations.append((str(p), (stat.st_ino, stat.st_size) in fsynced))
            if 'OperationCreate' in q:
                observations.append(('record before create', LI.load_record(path)['target'] == v['input']['id']))
            else:
                stat = LI.progress_path(path).stat()
                observations.append(('progress before next mutation', (stat.st_ino, stat.st_size) in fsynced))
        self.fake.after_mutation = inspect
        self.marked('1')
        with mock.patch.object(LI.os, 'fsync', fsync):
            self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1', '--blocks', 'ARC-2'), 0, self.stdout)
            self.assertTrue(observations)
            self.assertEqual([label for label, durable in observations if not durable], [])
            stat = LI.progress_path(self.records()[0]).stat()
            self.assertIn((stat.st_ino, stat.st_size), fsynced)
        self.fake.after_mutation = None
        before = len(self.fake.mutations)
        with mock.patch.object(LI.os, 'fsync', side_effect=OSError('disk failed')):
            self.assertEqual(self.issue('new', '--independent', 'why'), 2)
        self.assertEqual(len(self.fake.mutations), before)

    def test_invalid_local_inputs_and_replay_binding(self):
        for value in ('', '   '):
            self.assertEqual(self.issue('new', '--title', value, '--independent', 'reason'), 2)
        for value in ('owner\nforged', '`owner`'):
            self.assertEqual(self.issue('new', '--independent', 'reason', '--approver', value), 2)
        self.assertEqual(self.issue('new', '--independent', 'reason', body='`swarm-op: forged`'), 2)
        self.assertEqual(self.issue('replay', '../escape'), 3)
        self.assertEqual(self.fake.calls, [])
        self.body.write_text('body')
        self.assertEqual(self.issue('edit', 'ARC-1', '--add-blocks', 'ARC-2', '--remove-blocks', 'ARC-2'), 2)
        self.assertEqual(self.issue('new', '--independent', 'reason'), 0)
        data = json.loads(self.binding.read_text())
        data['repository'] = 'owner/different'
        self.binding.write_text(json.dumps(data))
        before = len(self.fake.mutations)
        self.assertEqual(self.replay(), 3)
        self.assertIn('binding mismatch', self.stderr)
        self.assertEqual(len(self.fake.mutations), before)

    def test_operation_directory_rejects_invalid_identity_without_normalizing(self):
        self.fake.org['id'] = '@workspace'
        data = json.loads(self.binding.read_text())
        data['workspace'] = self.fake.org
        self.binding.write_text(json.dumps(data))
        self.assertEqual(self.issue('new', '--independent', 'why', '--preview'), 2)
        self.assertIn('invalid operation workspace', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])

    def test_replay_managed_counterpart_and_target_identifier_conflicts(self):
        self.marked('1')
        self.crash_after(1, lambda: self.issue('new', '--blocked-by', 'ARC-1'))
        original = copy.deepcopy(self.fake.issues['1'])
        for change in (dict(description='newer counterpart body'), dict(identifier='arc-1')):
            self.fake.issues['1'] = dict(original, **change)
            before = len(self.fake.mutations)
            self.assertEqual(self.replay(), 3)
            self.assertEqual(len(self.fake.mutations), before)
        self.fake.issues['1'] = original
        del self.fake.issues['1']
        before = len(self.fake.mutations)
        self.assertEqual(self.replay(), 3)
        self.assertEqual(len(self.fake.mutations), before)

    def test_incomplete_extra_edge_refuses_before_any_replay_write(self):
        self.crash_after(1, lambda: self.issue('new', '--blocked-by', 'ARC-1'))
        self.fake.edge(self.target(), '2')
        before = len(self.fake.mutations)
        self.assertEqual(self.replay(), 3)
        self.assertIn('unrecorded blocks edge', self.stdout)
        self.assertEqual(len(self.fake.mutations), before)

    def test_issue_existence_read_compares_returned_id_exactly(self):
        self.assertEqual(self.issue('new', '--independent', 'why'), 0)
        def wrong_id(data):
            data['issues']['nodes'][0]['id'] = data['issues']['nodes'][0]['id'].upper()
        self.fake.read_override = wrong_id
        self.assertEqual(self.replay(), 3)
        self.assertIn('issue id disagrees', self.stdout)

    def test_endpoint_deleted_between_trailer_and_relation_step(self):
        def deleted(q, v):
            if 'OperationCreate' in q:
                del self.fake.issues['1']
        self.fake.after_mutation = deleted
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3)
        self.assertIn('relation endpoint deleted', self.stdout)
        self.assertEqual(len(self.fake.mutations), 1)

    def test_source_resolution_failure_leaves_no_operation_record(self):
        self.fake.fail = 'query IssueBatch'
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2,
                         self.stdout + self.stderr)
        self.assertIn('incomplete relation read:', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])
        self.assertEqual(list((self.root / 'ops').rglob('*.progress.jsonl')), [])

    def test_creation_resolution_failure_leaves_no_operation_record(self):
        self.fake.fail = 'query OperationIssue'
        self.assertEqual(self.issue('new', '--independent', 'reason'), 2,
                         self.stdout + self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])

    def test_record_is_followed_by_mutation_before_any_readback(self):
        save = LI.save_record
        pending = []
        def saved(path, spec):
            save(path, spec)
            pending.append(path)
        def transport(body, headers, timeout=None):
            query = json.loads(body)['query']
            if pending:
                self.assertTrue(query.startswith('mutation'), query)
                self.assertTrue(pending.pop().is_file())
            return self.fake(body, headers, timeout)
        with mock.patch.object(LI, 'save_record', saved), mock.patch.object(API, 'transport', transport):
            for command, args in (('new', ('--blocked-by', 'ARC-1', '--blocks', 'ARC-2')),
                                  ('edit', ('ARC-1', '--add-blocks', 'ARC-2'))):
                with self.subTest(command=command):
                    pending.clear()
                    self.assertEqual(self.issue(command, *args), 0, self.stdout + self.stderr)
                    self.assertEqual(pending, [])

    def test_source_endpoint_read_failure_is_unknown_and_resumable(self):
        def fail_sources(q, v):
            if 'OperationCreate' in q:
                self.fake.fail = 'query IssueBatch'
        self.fake.after_mutation = fail_sources
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 3, self.stdout)
        self.assertIn('UNKNOWN/INCOMPLETE: incomplete relation read:', self.stdout)
        self.assertNotIn('DRIFT/INCOMPLETE:', self.stdout)
        self.assertEqual(len(self.fake.mutations), 1)
        path = self.records()[0]
        self.assertFalse(LI.confirmed(path))
        immutable = path.read_bytes()
        self.fake.fail = None
        self.fake.after_mutation = None
        self.assertEqual(self.replay(), 0, self.stdout)
        self.assertEqual(path.read_bytes(), immutable)
        self.assertTrue(LI.confirmed(path))
        self.assertEqual(sum('OperationCreate(' in q for q, _ in self.fake.mutations), 1)

    def test_audit_operation_digest_binding_and_bad_progress(self):
        self.assertEqual(self.issue('new', '--independent', 'why'), 0)
        path = self.records()[0]
        for invalid in ([], None, 3, 'CONFIRMED'):
            LI.progress_path(path).write_text(json.dumps(invalid) + '\n')
            self.audit()
            self.assertEqual(self.checks['op_incomplete'], 'DRIFT')
        LI.progress_path(path).write_text('{"step":"CONFIRMED"}\n')
        record = json.loads(path.read_text())
        record['spec']['workspace'] = 'wrong-workspace'
        record['sha256'] = LI.digest(record['spec'])
        path.write_text(json.dumps(record))
        self.audit()
        self.assertEqual(self.checks['op_incomplete'], 'DRIFT')
        self.assertIn('operation binding mismatch', self.out.read_text())

    def test_legacy_draft_operation_inputs_invalidate_real_section(self):
        self.draft.write_text(json.dumps({'project': {'linear_id': 'project', 'team': 'ARC'}, 'issues': []}))
        self.assertEqual(self.cli('audit', '--draft', str(self.draft), '--out', str(self.out)), 0)
        self.assertIn('consistent', TA.section(self.out, draft=self.draft))
        self.assertEqual(self.issue('new', '--independent', 'why'), 0)
        self.assertIn('STALE', TA.section(self.out, draft=self.draft))
        self.assertEqual(self.cli('audit', '--draft', str(self.draft), '--out', str(self.out)), 0)
        self.assertIn('consistent', TA.section(self.out, draft=self.draft))
        with LI.progress_path(self.records()[0]).open('ab') as f:
            f.write(b'{"step":"TORN"}\n')
        self.assertIn('STALE', TA.section(self.out, draft=self.draft))

    def test_replay_accepts_mixed_before_desired_components(self):
        self.fake.edge('1', '2')
        self.crash_after(1, lambda: self.issue('edit', 'ARC-1', '--title', 'desired', '--remove-blocks', 'ARC-2'))
        spec = LI.load_record(self.records()[0])
        self.fake.issues['1']['description'] = spec['issues'][0]['before']['description']
        # Title is already desired; the body/trailer is still before.
        self.fake.issues['1']['relations']['nodes'].clear()
        self.fake.issues['2']['inverseRelations']['nodes'].clear()
        before = len(self.fake.mutations)
        self.assertEqual(self.replay(), 0, self.stdout)
        self.assertFalse(any('RelationDelete' in q for q, _ in self.fake.mutations[before:]))

    def test_binding_race_refuses_before_operation_record(self):
        real = LS.project_lock
        from contextlib import contextmanager
        @contextmanager
        def changed(workspace, project):
            with real(workspace, project):
                self.binding.write_text(self.binding.read_text() + '\n')
                yield
        with mock.patch.object(LS, 'project_lock', changed):
            self.assertEqual(self.issue('new', '--independent', 'why'), 2)
        self.assertIn('binding changed', self.stderr)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.fake.mutations, [])

    def test_audit_edge_sets_malformed_marker_and_torn_progress_tail(self):
        self.fake.add('3')
        self.fake.edge('1', '2'); self.fake.edge('1', '3')
        self.marked('1', outgoing=['ARC-2', 'ARC-3'])
        self.fake.issues['1']['description'] = self.fake.issues['1']['description'].replace('ARC-2,ARC-3', 'ARC-3,ARC-2')
        self.audit()
        self.assertEqual(self.checks['declared_edges'], 'CLEAN')
        self.fake.issues['1']['description'] = '`swarm-deps: invalid`'
        self.audit()
        self.assertEqual(self.checks['declared_edges'], 'DRIFT')
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 2)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.issue('new', '--independent', 'why'), 0)
        with LI.progress_path(self.records()[0]).open('ab') as handle:
            handle.write(b'{"step":')
        self.audit()
        self.assertEqual(self.checks['op_incomplete'], 'DRIFT')
        self.assertEqual(self.replay(), 0, self.stdout)
        self.audit()
        self.assertEqual(self.checks['op_incomplete'], 'CLEAN')

    def test_audit_added_removed_edges_and_local_staleness(self):
        self.marked('1', outgoing=['ARC-2'])
        self.audit()
        self.assertEqual(self.checks['declared_edges'], 'DRIFT')
        self.fake.edge('1', '2')
        self.audit()
        self.assertEqual(self.checks['declared_edges'], 'CLEAN')
        self.assertEqual(self.issue('new', '--independent', 'why'), 0)
        self.assertIn('STALE', TA.section(self.out, binding=self.binding))
        self.audit()
        self.assertIn('consistent', TA.section(self.out, binding=self.binding))
        LI.progress_path(self.records()[0]).write_bytes(b'{"step":"CONFIRMED"}')
        self.audit()
        self.assertEqual(self.checks['op_incomplete'], 'DRIFT')
        self.assertEqual(self.replay(), 0)
        self.audit()
        self.assertEqual(self.checks['op_incomplete'], 'CLEAN')

    def test_8_network_boundary(self):
        for path in (ROOT / 'skills/hanig-swarm/scripts/swarm.py', ROOT / 'skills/hanig-project/scripts/tickets.py', ROOT / 'skills/hanig-project/scripts/drain_contract.py'):
            tree = ast.parse(path.read_text())
            imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            self.assertTrue(imports.isdisjoint({'linear_api', 'linear_sync', 'linear_issue', 'urllib.request', 'http.client'}), str(path))
        self.assertEqual(self.issue('new', '--blocked-by', 'ARC-1'), 0, self.stdout)
        self.assertTrue(any(q.startswith('mutation') for q, v in self.fake.calls))


if __name__ == '__main__':
    unittest.main()
