"""PR 4 acceptance through the CLI and the shared transport, entirely offline."""
import ast
import copy
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_linear_issue import IssueCase, IssueLinear, Crash
from tests.test_linear_audit import BudgetPaging, KEY, ROOT, connection, budget_page

sys.path.insert(0, str(ROOT / 'skills/hanig-project/scripts'))
import linear_api as API
import linear_file as LF
import linear_issue as LI
import linear_sync as LS
import tickets as T


def body_digest(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def render_markdown(body):
    """Observed Linear rendering, not a production canonicalization rule."""
    body = re.sub(r'(?m)^- ', '* ', body).rstrip()
    return re.sub(r'\r?\n(?:[ \t]*\r?\n)+(?=(?:`?swarm-[^\r\n]+(?:\r?\n|$))+$)', '\n', body)


class FileLinear(BudgetPaging, IssueLinear):
    def __init__(self):
        super().__init__()
        self.projects = {}
        self.lag = False
        self.before_mutation = None

    def __call__(self, body, headers, timeout=None):
        q, v = (json.loads(body)[k] for k in ('query', 'variables'))
        if q.startswith('mutation') and self.before_mutation:
            self.before_mutation(q, v)
        if 'project(id:' in q and v['id'] not in self.projects:
            self.calls.append((q, v))
            return 200, json.dumps({'errors': [{'message': 'Entity not found: Project'}]}).encode()
        return super().__call__(body, headers, timeout)

    def dispatch(self, q, v):
        if 'query FilingIdentity' in q:
            return {'viewer': {'organization': self.org}, 'teams': connection([self.team])}
        if 'query FilingProjects' in q:
            self.assert_project_query(q)
            field, test = next(iter(v['filter'].items()))
            nodes = [copy.deepcopy(p) for p in self.projects.values() if p[field] == test['eq']]
            return {'projects': budget_page(nodes, v['after'], 50)}
        if 'query Binding' in q:
            return {'viewer': {'organization': self.org}, 'project': copy.deepcopy(self.projects.get(v['id']))}
        if 'query ProjectIssues' in q:
            nodes = [copy.deepcopy(i) for i in self.issues.values()
                     if (i['project'] or {}).get('id') == v['id'] and not self.lag]
            if 'nodes { id updatedAt }' in q:
                nodes = [{k: n[k] for k in ('id', 'updatedAt')} for n in nodes]
            # BudgetPaging will cover nested and outer pages.
            return {'project': {'issues': connection(nodes)}}
        if 'query Markers' in q and self.lag:
            return {'issues': connection([])}
        return super().dispatch(q, v)

    def assert_project_query(self, q):
        expected = ('query FilingProjects($filter: ProjectFilter!, $after: String) { projects(filter: $filter, '
                    'first: 50, after: $after, includeArchived: true) { nodes { id name description content url '
                    'teams(first: 20) { nodes { id key name } pageInfo { hasNextPage endCursor } } } '
                    'pageInfo { hasNextPage endCursor } } }')
        assert ' '.join(q.split()) == expected, q

    def add_project(self, iid='project', content='', name='Plan'):
        p = {'id': iid, 'name': name, 'description': '', 'content': content,
             'url': 'https://linear.app/project/' + iid, 'teams': connection([self.team])}
        self.projects[iid] = p
        return p

    def mutate(self, q, v):
        if 'FilingProjectCreate' in q:
            p = v['input']
            if p['id'] not in self.projects:
                self.add_project(p['id']).update({k: render_markdown(p[k]) if k == 'content' else
                                                 p[k].rstrip() if k == 'description' else p[k]
                                                 for k in ('name', 'description', 'content')})
        elif 'FilingProjectUpdate' in q:
            if not self.ignore_update:
                self.projects[v['id']].update({k: render_markdown(value) if k == 'content' else
                                              value.rstrip() if k == 'description' else value
                                              for k, value in v['input'].items()})
        else:
            super().mutate(q, v)
            if 'OperationCreate' in q or 'OperationUpdate' in q:
                iid = v['input']['id'] if 'OperationCreate' in q else v['id']
                if iid in self.issues and 'description' in v['input']:
                    self.issues[iid]['description'] = render_markdown(self.issues[iid]['description'])


class FileCase(IssueCase):
    def setUp(self):
        super().setUp()
        self.fake = FileLinear()
        patch = mock.patch.object(API, 'transport', self.fake)
        patch.start()
        self.addCleanup(patch.stop)
        self.draft = self.root / 'tickets.json'
        self.new_draft()

    def new_draft(self, count=3, repository='owner/repo'):
        self.data = {'project': {'name': 'Plan', 'slug': 'plan', 'summary': 'Summary',
                                'description': 'Project prose', 'team': 'Arc', 'repository': repository},
                     'issues': [{'unit': 'u%d' % n, 'title': 'Unit %d' % n, 'body': 'Approved prose %d' % n,
                                 'blocked_by': ['u0'] if n == 1 else []} for n in range(count)]}
        self.approve()

    def approve(self):
        self.data['approval'] = {'state': 'granted', 'granted_by': 'owner',
                                 'content_digest': T.content_digest(self.data)}
        self.save()

    def save(self):
        self.draft.write_text(json.dumps(self.data))

    def read(self):
        self.data = json.loads(self.draft.read_text())
        return self.data

    def file(self, *args):
        return self.cli('file', '--draft', str(self.draft), *args)

    def operation(self):
        return max(self.records(), key=lambda p: p.stat().st_mtime_ns).stem

    def replay_file(self, op=None):
        return self.cli('replay', op or self.operation(), '--draft', str(self.draft))

    def unit_ids(self):
        return {i['unit']: i['linear_id'] for i in self.read()['issues']}

    def body_line(self, n=0):
        return 'swarm-body: ' + body_digest(self.data['issues'][n]['body'])

    def test_absent_draft_identifier_refuses_before_any_mutation(self):
        self.data['issues'][0]['identifier'] = 'ARC-999'
        self.save()
        before = self.draft.read_bytes()
        self.assertEqual(self.file(), 2, self.stdout + self.stderr)
        self.assertIn('issue identifier disagrees on read-back: ARC-999', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])
        self.assertEqual(self.draft.read_bytes(), before)

    def test_bound_identifiers_resolve_when_project_listing_lags(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.read()
        expected = {issue['identifier'] for issue in self.data['issues']}
        self.fake.add('1464', project='elsewhere')
        self.fake.lag = True
        self.fake.calls.clear()
        self.fake.mutations.clear()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        lookups = [v['filter'] for q, v in self.fake.calls if 'OperationIdentifiers' in q]
        self.assertEqual(lookups, [{'team': {'key': {'eq': 'ARC'}},
                                   'number': {'in': sorted(int(ref.split('-')[1]) for ref in expected)}}])
        self.assertEqual(self.fake.mutations, [])

    def test_markdown_rendering_confirms_and_replays_without_writes(self):
        self.new_draft(1)
        self.data['project']['description'] = 'Project\n\n- scope: approved\n\n'
        body = 'Caf\u00e9\r\n\n- kind: code\n- outputs: result\n\n'
        self.data['issues'][0]['body'] = body
        self.approve()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        spec = LI.load_record(self.records()[0])
        row = spec['issues'][0]
        live = self.fake.issues[row['id']]
        wanted = row['desired']['description']
        self.assertIn('- kind: code\n- outputs: result\n\nswarm-unit:', wanted)
        self.assertIn('* kind: code\n* outputs: result\nswarm-unit:', live['description'])
        self.assertNotEqual(wanted, live['description'])
        self.assertIn('\nswarm-body: ' + hashlib.sha256(body.encode('utf-8')).hexdigest(), live['description'])
        project = self.fake.projects[spec['project']]
        self.assertIn('* scope: approved\nswarm-plan:', project['content'])
        self.assertNotEqual(project['content'], spec['project_desired']['content'])
        writes = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)

    def test_human_prose_edits_are_outside_filing_identity(self):
        self.new_draft(1)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        iid = self.unit_ids()['u0']
        live = self.fake.issues[iid]
        live['description'] = live['description'].replace('Approved prose 0', 'Human edited prose\n* note: retained')
        project = self.fake.projects[self.data['project']['linear_id']]
        project['content'] = project['content'].replace('Project prose', 'Human project prose')
        issue_text, project_text = live['description'], project['content']
        writes = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)
        self.data['issues'][0]['title'] += ' revised'
        self.approve()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), writes + 1)
        self.assertEqual(self.fake.mutations[-1][1]['input'], {'title': 'Unit 0 revised'})
        self.assertEqual(live['description'], issue_text)
        self.assertEqual(project['content'], project_text)

    def test_missing_or_different_body_digest_rewrites_approved_body(self):
        self.new_draft(1)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        live = self.fake.issues[self.unit_ids()['u0']]
        for replacement in ('', 'swarm-body: ' + '0' * 64):
            with self.subTest(replacement=replacement):
                live['description'] = live['description'].replace(self.body_line(), replacement)
                live['description'] = live['description'].replace('Approved prose 0', 'Human prose')
                writes = len(self.fake.mutations)
                self.assertEqual(self.replay_file(), 3, self.stdout + self.stderr)
                self.assertIn('managed issue changed', self.stderr)
                self.assertEqual(len(self.fake.mutations), writes)
                self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                self.assertEqual(len(self.fake.mutations), writes + 1)
                self.assertTrue(live['description'].startswith('Approved prose 0\n'))
                self.assertTrue(live['description'].endswith(self.body_line()))

    def test_appended_prose_recovery_through_file_refile_and_replay(self):
        for kind in ('project', 'issue'):
            for pr3 in (False, True):
                with self.subTest(kind=kind, pr3=pr3):
                    self.fake.issues.clear()
                    self.fake.projects.clear()
                    self.new_draft(1)
                    self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                    iid = self.unit_ids()['u0']
                    pid = self.data['project']['linear_id']
                    remote = self.fake.projects[pid] if kind == 'project' else self.fake.issues[iid]
                    field = 'content' if kind == 'project' else 'description'
                    prose = remote[field] + '\nHuman appended note\n'
                    suffix = LI.description('', set(), set(), 'old-op', 'owner') if pr3 else ''
                    remote[field] = prose + suffix
                    writes, records = len(self.fake.mutations), self.records()
                    self.assertEqual(self.file(), 2, self.stdout + self.stderr)
                    self.assertIn('unmarked ' + kind, self.stderr)
                    self.assertIn(pid if kind == 'project' else remote['identifier'], self.stderr)
                    self.assertEqual(self.records(), records)
                    self.assertEqual(len(self.fake.mutations), writes)
                    self.assertEqual(self.replay_file(), 3, self.stdout + self.stderr)
                    # Leave recovery incomplete, then resume its recorded before/desired values.
                    self.fake.ignore_update = True
                    self.assertEqual(self.file('--adopt-checked'), 3, self.stdout + self.stderr)
                    self.fake.ignore_update = False
                    self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
                    identity = ('swarm-plan: plan\nswarm-repo: owner/repo' if kind == 'project' else
                                'swarm-unit: plan/u0\nswarm-repo: owner/repo\n' + self.body_line())
                    prefix = prose if kind == 'project' else 'Approved prose 0\n'
                    self.assertEqual(remote[field], render_markdown(prefix + identity + '\n' + suffix))
                    writes = len(self.fake.mutations)
                    self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                    self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
                    self.assertEqual(len(self.fake.mutations), writes)

    def test_adoption_drops_text_appended_after_pr3_trailer(self):
        self.new_draft(1)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        live = self.fake.issues[self.unit_ids()['u0']]
        live['description'] = LI.description(live['description'], set(), set(), 'old-op', 'owner')
        live['description'] += '\nHuman note after dependency trailer'
        writes, records = len(self.fake.mutations), self.records()
        self.assertEqual(self.file(), 2, self.stdout + self.stderr)
        self.assertIn('unmarked issue', self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)
        self.assertEqual(self.records(), records)
        self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)
        self.assertEqual(live['description'], 'Approved prose 0\nswarm-unit: plan/u0\n'
                         'swarm-repo: owner/repo\n' + self.body_line())

    def test_issue_recovery_with_changed_edges_keeps_pr3_trailer(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        ids = self.unit_ids()
        live = self.fake.issues[ids['u1']]
        live['description'] = LI.description(live['description'] + '\nHuman note\n',
                                             {self.fake.issues[ids['u0']]['identifier']},
                                             set(), 'old-op', 'old-owner')
        self.data['issues'][1]['blocked_by'] = []
        self.approve()
        self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)
        expected = ('Approved prose 1\nswarm-unit: plan/u1\n'
                    'swarm-repo: owner/repo\n' + self.body_line(1) + '\n')
        self.assertEqual(LI.trailer(live['description'])['prefix'], expected)
        self.assertEqual(LI.trailer(live['description'])['op'], 'old-op')
        self.assertEqual(LI.trailer(live['description'])['approver'], 'old-owner')
        self.assertIsNone(LI.declared_edges(live))
        writes = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)

    def test_embedded_identity_requires_exact_contiguous_whole_lines(self):
        self.new_draft(1)
        pid = self.seed()
        live = self.existing_unit(0, pid)
        identity = 'swarm-unit: plan/u0\nswarm-repo: owner/repo\n' + self.body_line()
        for block in (identity, identity.replace('plan/u0', 'plan/other'),
                      identity.replace('owner/repo', 'other/repo'),
                      identity.replace(self.body_line(), 'swarm-body: ' + '0' * 64),
                      identity.replace('\n', '\nInterruption\n', 1),
                      'Example ' + identity, identity + ' suffix',
                      identity.rsplit('\n', 1)[0]):
            with self.subTest(block=block):
                live['description'] = block + '\nEnd of prose'
                self.assertEqual(self.file(), 2, self.stdout + self.stderr)
                self.assertIn('unmarked issue', self.stderr)
                self.assertEqual(self.fake.mutations, [])
        # Other values in prose neither prove a binding nor conflict with adoption.
        live['description'] = identity.replace('plan/u0', 'plan/other') + '\nEnd of prose'
        self.fake.projects[pid]['content'] = 'swarm-plan: other\nswarm-repo: other/repo\nEnd of prose'
        self.assertEqual(self.file(), 2, self.stdout + self.stderr)
        self.assertIn('unmarked project', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)

    def test_exact_embedded_identity_does_not_hide_foreign_trailer(self):
        self.new_draft(1)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        live = self.fake.issues[self.unit_ids()['u0']]
        body = live['description']
        live['description'] += '\nHuman note\nswarm-unit: plan/other\nswarm-repo: other/repo'
        writes = len(self.fake.mutations)
        self.assertEqual(self.file(), 2, self.stdout + self.stderr)
        self.assertIn('bound to another unit', self.stderr)
        self.assertEqual(self.replay_file(), 3, self.stdout + self.stderr)
        self.assertIn('managed issue changed', self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)
        live['description'] = body
        project = self.fake.projects[self.data['project']['linear_id']]
        project['content'] += '\nHuman note\nswarm-plan: other\nswarm-repo: other/repo'
        self.assertEqual(self.file(), 2, self.stdout + self.stderr)
        self.assertIn('unmarked project', self.stderr)
        self.assertEqual(self.file('--adopt-checked'), 2, self.stdout + self.stderr)
        self.assertIn('conflicting swarm-plan marker', self.stderr)
        self.assertEqual(self.replay_file(), 3, self.stdout + self.stderr)
        self.assertIn('managed project changed', self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)

    def test_render_equivalent_reapproval_changes_exact_body_digest(self):
        self.new_draft(1)
        self.data['issues'][0]['body'] = '- kind: code\n\n'
        self.approve()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        live = self.fake.issues[self.unit_ids()['u0']]
        first_digest = self.body_line()
        self.data['issues'][0]['body'] = '* kind: code\n'
        self.approve()
        writes = len(self.fake.mutations)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), writes + 1)
        self.assertNotIn(first_digest, live['description'])
        self.assertIn(self.body_line(), live['description'])
        self.assertTrue(live['description'].startswith('* kind: code\nswarm-unit:'))

    def test_replay_before_values_ignore_prose_but_require_exact_title_and_markers(self):
        self.new_draft(1)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        live = self.fake.issues[self.unit_ids()['u0']]
        self.data['issues'][0].update(title='New title', body='New\n- kind: code\n\n')
        self.approve()
        # Leave an unconfirmed operation with the old values still live.
        self.fake.ignore_update = True
        self.assertEqual(self.file(), 3, self.stdout + self.stderr)
        op = self.operation()
        self.fake.ignore_update = False
        live['description'] = live['description'].replace('Approved prose 0', 'Edited after the interrupted write')
        before = live['title']
        live['title'] = 'Unrelated title'
        writes = len(self.fake.mutations)
        self.assertEqual(self.replay_file(op), 3, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)
        live['title'] = before
        self.assertEqual(self.replay_file(op), 0, self.stdout + self.stderr)
        self.assertEqual(live['title'], 'New title')
        self.assertTrue(live['description'].startswith('New\n* kind: code\nswarm-unit:'))
        self.assertIn(self.body_line(), live['description'])

    def test_project_plain_fields_and_identity_are_exact_on_replay(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        project = self.fake.projects[self.read()['project']['linear_id']]
        for field, value in (('name', 'Changed name'), ('description', 'Changed summary'),
                             ('content', project['content'].replace('swarm-plan: plan', 'swarm-plan: other'))):
            with self.subTest(field=field):
                before = project[field]
                project[field] = value
                writes = len(self.fake.mutations)
                self.assertEqual(self.replay_file(), 3, self.stdout + self.stderr)
                self.assertIn('managed project changed', self.stderr)
                self.assertEqual(len(self.fake.mutations), writes)
                project[field] = before

    def test_legacy_operation_requires_fresh_filing_to_add_body_identity(self):
        self.new_draft(1)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        path = self.records()[0]
        spec = LI.load_record(path)
        row = spec['issues'][0]
        row['markers'].pop('swarm-body')
        row['desired']['description'] = row['desired']['description'].replace('\n' + self.body_line(), '')
        self.fake.issues[row['id']]['description'] = row['desired']['description']
        # Fixture for a durable operation written before body identities existed.
        path.write_text(json.dumps({'spec': spec, 'sha256': LI.digest(spec)}))
        old_record = path.read_bytes()
        self.fake.calls.clear()
        self.assertEqual(self.replay_file(path.stem), 3, self.stdout + self.stderr)
        self.assertIn('run file with the approved draft', self.stderr)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertTrue(self.fake.issues[row['id']]['description'].endswith(self.body_line()))
        self.assertEqual(path.read_bytes(), old_record)

    def seed(self, markers=True, recorded=True, derived=True):
        p = self.data['project']
        pid = API.derived_id('project:workspace/%s/plan' % p['repository']) if derived else 'project'
        content = 'swarm-plan: plan\nswarm-repo: ' + p['repository'] if markers else ''
        self.fake.add_project(pid, content)
        if recorded:
            p['linear_id'] = pid
        self.save()
        return pid

    def existing_unit(self, n, pid, marked=False, derived=False, trailer=False):
        issue = self.data['issues'][n]
        iid = API.derived_id('issue:workspace/%s/plan/%s' % (self.data['project']['repository'], issue['unit'])) if derived else str(n + 1)
        body = 'Existing prose\n'
        if marked:
            body += 'swarm-unit: plan/%s\nswarm-repo: %s\n' % (issue['unit'], self.data['project']['repository'])
        if trailer:
            body = LI.description(body, set(), set(), 'old-op', 'old-owner')
        live = self.fake.add(iid, project=pid, description=body)
        live['identifier'] = 'ARC-' + str(n + 1)
        live['title'] = issue['title']
        return live

    def test_01_approval_refusals_before_network(self):
        base = copy.deepcopy(self.data)
        cases = [lambda d: d.pop('approval'),
                 lambda d: d['approval'].update(granted_by=' '),
                 lambda d: d['approval'].pop('content_digest'),
                 lambda d: d['issues'][0].update(title='changed'),
                 lambda d: d['issues'][1].update(blocked_by=[]),
                 lambda d: d['project'].update(repository='other/repo'),
                 lambda d: d['project'].pop('repository')]
        for edit in cases:
            with self.subTest(edit=cases.index(edit)):
                self.data = copy.deepcopy(base)
                edit(self.data)
                self.save()
                self.fake.calls.clear()
                self.assertEqual(self.file(), 2, self.stdout + self.stderr)
                self.assertEqual(self.fake.calls, [])
        self.data = copy.deepcopy(base)
        self.data['approval'] = {'state': 'autopilot', 'granted_by': 'autopilot phrase in the request'}
        self.save()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)

    def test_autopilot_checks_every_present_digest_before_network(self):
        base = copy.deepcopy(self.data)
        for change in ('content', 'empty', 'null'):
            with self.subTest(change=change):
                self.data = copy.deepcopy(base)
                self.data['approval']['state'] = 'autopilot'
                if change == 'content':
                    self.data['issues'][0]['body'] += ' changed after approval'
                else:
                    self.data['approval']['content_digest'] = '' if change == 'empty' else None
                self.save()
                self.fake.calls.clear()
                before = self.draft.read_bytes()
                self.assertEqual(self.file(), 2, self.stdout + self.stderr)
                self.assertIn('content_digest', self.stderr)
                self.assertEqual(self.fake.calls, [])
                self.assertEqual(self.draft.read_bytes(), before)
        self.data = copy.deepcopy(base)
        self.data['approval']['state'] = 'autopilot'
        self.save()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)

    def test_rejected_project_create_requires_matching_readback(self):
        for outcome in ('missing', 'foreign', 'unreadable', 'marked'):
            with self.subTest(outcome=outcome):
                self.fake.projects.clear()
                self.fake.issues.clear()
                self.fake.mutations.clear()
                self.new_draft(1)
                before = self.draft.read_bytes()
                rejected = []

                def transport(body, headers, timeout=None):
                    q, v = (json.loads(body)[k] for k in ('query', 'variables'))
                    if 'FilingProjectCreate' in q:
                        rejected.append(v['input']['id'])
                        self.fake.calls.append((q, v))
                        self.fake.mutations.append((q, v))
                        if outcome in ('foreign', 'marked'):
                            self.fake.mutate(q, v)
                            if outcome == 'foreign':
                                self.fake.projects[rejected[-1]]['content'] = 'Foreign project'
                        return 200, json.dumps({'errors': [{'message': 'project denied ' + KEY}]}).encode()
                    if rejected and outcome == 'unreadable' and 'FilingProjects' in q:
                        return 200, json.dumps({'errors': [{'message': 'readback unavailable'}]}).encode()
                    return self.fake(body, headers, timeout)

                with mock.patch.object(API, 'transport', transport):
                    result = self.file()
                self.assertEqual(result, 0 if outcome == 'marked' else 3, self.stdout + self.stderr)
                self.assertEqual(len(rejected), 1)
                path = next(p for p in self.records() if p.stem == self.operation())
                self.assertEqual(LI.confirmed(path), outcome == 'marked')
                if outcome != 'marked':
                    self.assertIn('project denied [REDACTED]', self.stdout)
                    self.assertNotIn('managed project deleted', self.stdout)
                    self.assertNotIn('readback unavailable', self.stdout)
                    self.assertEqual(len(self.fake.mutations), 1)
                    self.assertEqual(self.draft.read_bytes(), before)
                    self.assertFalse(any(e.get('step') == 'project' for e in LI.progress(path)))
                else:
                    self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)

    def test_rejected_filing_issue_create_preserves_original_error(self):
        self.check_rejected_issue_create(self.file)

    def test_outgoing_text_matches_linear_storage(self):
        self.data['project']['summary'] = 'Summary with interior  \nspacing \t\r\n'
        self.data['project']['description'] = 'Project with interior  \nspacing \t\r\n'
        self.data['issues'][0]['body'] = 'Issue with interior  \nspacing \t\r\n'
        self.approve()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertIn('CONFIRMED', self.stdout)
        spec = LI.load_record(next(p for p in self.records() if p.stem == self.operation()))
        project = self.fake.projects[spec['project']]
        self.assertEqual(project['description'], 'Summary with interior  \nspacing')
        self.assertEqual(LF.project_values(project), spec['project_desired'])
        for row in spec['issues']:
            live = self.fake.issues[row['id']]
            self.assertEqual({k: live[k] for k in ('title', 'description')}, row['desired'])
        for q, v in self.fake.mutations:
            for field in ('content', 'description'):
                if field in v.get('input', {}):
                    value = v['input'][field]
                    self.assertEqual(value, value.rstrip(), q)
        writes = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)

    def test_partial_file_refiles_with_selected_fields_only(self):
        # Exercise response selection itself, independently of the query fixtures.
        with mock.patch.object(self.fake, 'shapes', {}):
            self.crash_after(2, self.file)
            self.assertEqual(len(self.fake.projects), 1)
            self.assertEqual(len(self.fake.issues), 1)
            live = next(iter(self.fake.issues.values()))
            selected = API.Client(KEY).query(
                'query Issue($id: String!) { issue(id: $id) { id archivedAt } }', {'id': live['id']})
            self.assertEqual(selected, {'issue': {'id': live['id'], 'archivedAt': None}})
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)
            self.assertIn('CONFIRMED', self.stdout)
            writes = len(self.fake.mutations)
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)
            self.assertEqual(len(self.fake.mutations), writes)

    def test_cached_project_issues_refuse_trashed_and_archived(self):
        pid = self.seed()
        live = self.existing_unit(0, pid, marked=True, derived=True)
        for field, value in (('trashed', True), ('archivedAt', '2026-10-06T00:00:00Z')):
            with self.subTest(field=field):
                live.update(trashed=False, archivedAt=None)
                live[field] = value
                self.assertEqual(self.file(), 2, self.stdout + self.stderr)
                self.assertEqual(self.records(), [])
                self.assertFalse(list((self.root / 'ops').glob('*/*/*.progress.jsonl')))
                self.assertIn('issue deleted after creation', self.stderr)
                self.assertEqual(self.fake.mutations, [])

    def test_pre_record_managed_deletion_leaves_no_progress(self):
        self.new_draft(1)
        live = self.existing_unit(0, self.seed(), marked=True)
        prepare = LF.prepare
        def archived(*args, **kwargs):
            spec, reader, project = prepare(*args, **kwargs)
            reader.seen[live['id']]['archivedAt'] = 'date'
            return spec, reader, project
        with mock.patch.object(LF, 'prepare', archived):
            self.assertEqual(self.file(), 2, self.stdout + self.stderr)
        self.assertIn('issue deleted', self.stderr)
        self.assertEqual(self.records(), [])
        self.assertFalse(list((self.root / 'ops').glob('*/*/*.progress.jsonl')))
        self.assertEqual(self.fake.mutations, [])

    def test_deleted_unit_refile_and_replay_send_no_mutations(self):
        self.new_draft(1)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        live = self.fake.issues[self.unit_ids()['u0']]
        confirmed_op = self.operation()
        self.data['issues'][0]['title'] = 'Pending title'
        self.approve()
        self.fake.ignore_update = True
        self.assertEqual(self.file(), 3, self.stdout + self.stderr)
        self.fake.ignore_update = False
        op = self.operation()
        writes = copy.deepcopy(self.fake.mutations)
        for field, value in (('trashed', True), ('archivedAt', '2026-10-06T00:00:00Z')):
            with self.subTest(field=field):
                live.update(trashed=False, archivedAt=None)
                live[field] = value
                for command, call in (('file', self.file), ('replay', lambda: self.replay_file(op))):
                    with self.subTest(command=command):
                        self.assertEqual(call(), 2 if command == 'file' else 3, self.stdout + self.stderr)
                        self.assertIn('issue deleted', self.stdout + self.stderr)
                        self.assertEqual(self.fake.mutations, writes)
        # Completed operations also refuse a retained deleted unit.
        self.data['issues'][0]['title'] = 'Unit 0'
        self.approve()
        live.update(trashed=True, archivedAt=None)
        self.assertEqual(self.replay_file(confirmed_op), 3, self.stdout + self.stderr)
        self.assertIn('issue deleted', self.stdout + self.stderr)
        self.assertEqual(self.fake.mutations, writes)

    def test_01_progress_does_not_change_digest_and_redraft_rearms(self):
        old = T.content_digest(self.data)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(T.content_digest(self.read()), old)
        self.data['issues'][0].update(add_blocked_by=['bogus'], remove_blocked_by=['bogus'], url='url')
        self.assertEqual(T.content_digest(self.data), old)
        plan = {'name': 'p', 'units': [{'id': 'a', 'outputs': ['a']}]}
        first = T.draft(plan, repository='O/R')
        first['approval'] = {'state': 'granted', 'granted_by': 'owner', 'content_digest': T.content_digest(first)}
        self.assertEqual(T.draft(plan, existing=first, repository='O/R')['approval']['state'], 'granted')
        for brief in ({'name': 'changed'}, {'summary': 'changed'}, {'team': 'changed'}, {'description': 'changed'}):
            self.assertEqual(T.draft(plan, brief=brief, existing=first, repository='O/R')['approval']['state'], 'required')
        self.assertEqual(T.draft(plan, existing=first, repository='X/R')['approval']['state'], 'required')
        del first['approval']['content_digest']
        redraft = T.draft(plan, existing=first, repository='O/R')
        self.assertEqual(redraft['approval'], first['approval'])
        self.assertNotIn('content_digest', redraft['approval'])
        with self.assertRaisesRegex(ValueError, 'content_digest missing'):
            LF.validate(redraft, KEY)
        for brief in ({'name': 'changed'}, {'summary': 'changed'}, {'team': 'changed'}, {'description': 'changed'}):
            self.assertEqual(T.draft(plan, brief=brief, existing=first, repository='O/R')['approval']['state'], 'required')

    def check_reapproved_text(self, field):
        self.new_draft(1)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        iid = self.unit_ids()['u0']
        live = self.fake.issues[iid]
        # Preserve mixed identity syntax/order and provenance in the write;
        # the service may render the surrounding Markdown differently.
        old_digest = body_digest(self.data['issues'][0]['body'])
        suffix = ('`swarm-deps-by: earlier-op`\n`swarm-repo: owner/repo`\n'
                  'swarm-unit: plan/u0\n`swarm-body: ' + old_digest + '`\n\n`swarm-deps-by: latest-op`\r\n' +
                  LI.description('', set(), set(), 'old-op', 'old-owner').replace('\n', '\r\n') + '\n\n')
        live['description'] = self.data['issues'][0]['body'] + '\n' + suffix
        before = {k: live[k] for k in ('title', 'description')}
        self.data['issues'][0][field] = 'Re-approved ' + field + '\n\n' if field == 'body' else 'Re-approved title'
        self.approve()
        suffix = suffix.replace(old_digest, body_digest(self.data['issues'][0]['body']))
        desired = {'title': self.data['issues'][0]['title'],
                   'description': (self.data['issues'][0]['body'] + ('' if field == 'body' else '\n') + suffix).rstrip()}

        def check_record(q, v):
            if 'OperationUpdate' in q:
                spec = LI.load_record(next(p for p in self.records() if p.stem == self.operation()))
                row = next(r for r in spec['issues'] if r['id'] == iid)
                self.assertEqual(row['before'], before)
                self.assertEqual(row['desired'], desired)
                self.assertEqual(v['input'], {('description' if field == 'body' else 'title'):
                                              desired['description' if field == 'body' else 'title']})
        self.fake.before_mutation = check_record
        mutations = len(self.fake.mutations)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertIn('CONFIRMED', self.stdout)
        self.assertEqual(len(self.fake.mutations), mutations + 1)
        self.assertEqual(live['title'], desired['title'])
        self.assertEqual(live['description'], render_markdown(desired['description']) if field == 'body' else before['description'])
        last_write = max(n for n, (q, _) in enumerate(self.fake.calls) if q.startswith('mutation'))
        self.assertTrue(any(('IssueBatch' in q and iid in v['ids']) or
                            ('query ProjectIssues' in q and ' description ' in q and
                             v['id'] == self.data['project']['linear_id'])
                            for q, v in self.fake.calls[last_write + 1:]))
        self.fake.before_mutation = None
        mutations = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), mutations)

    def test_reapproved_title_updates_marked_issue(self):
        self.check_reapproved_text('title')

    def test_reapproved_body_updates_marked_issue_preserving_suffix(self):
        self.check_reapproved_text('body')

    def test_marker_prose_in_approved_bodies_and_project_descriptions(self):
        prose = ('Examples, including another unit in this plan:\n'
                 'swarm-plan: example\n`swarm-plan: another-example`\n'
                 'swarm-unit: example\n`swarm-unit: plan/u1`\n'
                 'swarm-deps-by: example\n`swarm-deps-by: example`\n'
                 'swarm-repo: some/docs\n`swarm-repo: other/docs`')
        for trailer in (False, True):
            with self.subTest(trailer=trailer):
                self.fake.issues.clear()
                self.fake.projects.clear()
                self.fake.mutations.clear()
                self.new_draft(2)
                self.data['project']['description'] = prose
                self.data['issues'][0]['body'] = prose
                self.approve()
                self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                ids = self.unit_ids()
                live = self.fake.issues[ids['u0']]
                project = self.fake.projects[self.data['project']['linear_id']]
                self.assertEqual(project['content'], prose + '\nswarm-plan: plan\nswarm-repo: owner/repo')
                identity = 'swarm-unit: plan/u0\nswarm-repo: owner/repo\n' + self.body_line() + '\n'
                self.assertEqual(live['description'], prose + '\n' + identity.rstrip())
                suffix = identity
                if trailer:
                    suffix = ('`swarm-deps-by: earlier-op`\n' + identity +
                              '`swarm-deps-by: previous-op`\r\n`swarm-deps-by: latest-op`\r\n' +
                              LI.description('', set(), {self.fake.issues[ids['u1']]['identifier']},
                                             'old-op', 'old-owner').replace('\n', '\r\n') + '\n\n')
                    live['description'] = prose + '\n' + suffix
                self.data['issues'][0]['body'] = 'Re-approved\n' + prose
                self.approve()
                self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                suffix = suffix.replace(body_digest(prose), body_digest(self.data['issues'][0]['body']))
                self.assertEqual(live['description'], render_markdown('Re-approved\n' + prose + '\n' + suffix))
                self.assertEqual(project['content'], prose + '\nswarm-plan: plan\nswarm-repo: owner/repo')
                mutations = len(self.fake.mutations)
                self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
                self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                self.assertEqual(len(self.fake.mutations), mutations)

    def test_adoption_ignores_marker_examples_above_identity_position(self):
        self.new_draft(1)
        pid = self.seed(markers=False)
        prose = ('swarm-plan: example\n`swarm-plan: other`\n'
                 'swarm-unit: example\n`swarm-unit: plan/other`\n'
                 'swarm-repo: some/docs\n`swarm-repo: other/docs`\n'
                 'swarm-deps-by: example\n`swarm-deps-by: example`\nEnd of prose\n')
        self.fake.projects[pid]['content'] = prose
        live = self.existing_unit(0, pid)
        live['description'] = LI.description(prose, set(), set(), 'old-op', 'old-owner')
        trailer = live['description'][len(prose):]
        self.assertEqual(self.file(), 2, self.stdout + self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.projects[pid]['content'], prose + 'swarm-plan: plan\nswarm-repo: owner/repo')
        self.assertEqual(live['description'], 'Approved prose 0\nswarm-unit: plan/u0\nswarm-repo: owner/repo\n' + self.body_line() + '\n' + trailer)

    def test_reapproved_text_requires_readback_and_replays(self):
        self.new_draft(1)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        iid = self.unit_ids()['u0']
        for field in ('title', 'body'):
            with self.subTest(field=field):
                self.data['issues'][0][field] = 'Changed ' + field
                self.approve()
                self.fake.ignore_update = True
                self.assertEqual(self.file(), 3, self.stdout + self.stderr)
                self.assertNotIn('CONFIRMED', self.stdout)
                self.assertIn('managed issue changed', self.stdout)
                path = next(p for p in self.records() if p.stem == self.operation())
                self.assertFalse(LI.confirmed(path))
                self.fake.ignore_update = False
                self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
                self.assertEqual(self.fake.issues[iid]['title'], self.data['issues'][0]['title'])
                self.assertEqual(self.fake.issues[iid]['description'], self.data['issues'][0]['body'] +
                                 '\nswarm-unit: plan/u0\nswarm-repo: owner/repo\n' + self.body_line())

    def test_reapproved_text_with_changed_edges(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        ids = self.unit_ids()
        live = self.fake.issues[ids['u1']]
        suffix = '`swarm-unit: plan/u1`\nswarm-repo: owner/repo\n`swarm-deps-by: earlier-op`\n'
        live['description'] = LI.description('Approved prose 1\n' + suffix,
                                             {self.fake.issues[ids['u0']]['identifier']}, set(), 'old-op', 'old-owner')
        before = {k: live[k] for k in ('title', 'description')}
        self.data['issues'][1].update(title='New title', body='New body', blocked_by=[])
        self.approve()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(live['title'], 'New title')
        self.assertTrue(live['description'].startswith('New body\n`swarm-unit: plan/u1`\nswarm-repo: owner/repo\n' + self.body_line(1) + '\n`swarm-deps-by: earlier-op`\n'))
        self.assertEqual(LI.trailer(live['description'])['op'], 'old-op')
        self.assertEqual(LI.trailer(live['description'])['approver'], 'old-owner')
        self.assertEqual(LI.trailer(live['description'])['by'], self.operation())
        self.assertIsNone(LI.declared_edges(live))
        spec = LI.load_record(next(p for p in self.records() if p.stem == self.operation()))
        row = next(r for r in spec['issues'] if r['id'] == ids['u1'])
        self.assertEqual(row['before'], before)
        self.assertEqual(LI.desired_value(row, spec['identities']), {k: live[k] for k in before})

    def check_adopted_content(self, selector):
        for suffix in ('', '`swarm-deps-by: prior-op`\r\n\r\n'
                       '`swarm-deps: blocked-by=- blocks=-`\r\n'
                       '`swarm-op: old-op`\r\n`swarm-approver: old-owner`\r\n\n'):
            with self.subTest(selector=selector, trailer=bool(suffix)):
                self.fake.before_mutation = None
                self.fake.issues.clear()
                self.fake.projects.clear()
                self.fake.mutations.clear()
                self.new_draft(1)
                pid = self.seed()
                live = self.existing_unit(0, pid)
                live['description'] = 'Unapproved live prose\n' + suffix
                if selector != 'title':
                    live['title'] = 'Unapproved live title'
                if selector in ('linear_id', 'identifier'):
                    self.data['issues'][0][selector] = live['id' if selector == 'linear_id' else selector]
                    self.save()
                before = {k: live[k] for k in ('title', 'description')}
                desired = {'title': 'Unit 0', 'description': ('Approved prose 0\n'
                           'swarm-unit: plan/u0\nswarm-repo: owner/repo\n' + self.body_line() + '\n' + suffix).rstrip()}

                def check_record(q, v):
                    if 'OperationUpdate' in q:
                        spec = LI.load_record(next(p for p in self.records() if p.stem == self.operation()))
                        row = next(r for r in spec['issues'] if r['id'] == live['id'])
                        self.assertEqual(row['before'], before)
                        self.assertEqual(row['desired'], desired)
                        self.assertEqual(v['input'], {k: value for k, value in desired.items() if value != before[k]})
                self.fake.before_mutation = check_record
                self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)
                self.assertIn('CONFIRMED', self.stdout)
                self.assertEqual(self.unit_ids(), {'u0': live['id']})
                self.assertEqual(len(self.fake.issues), 1)
                rendered = dict(desired, description=render_markdown(desired['description']))
                self.assertEqual({k: live[k] for k in desired}, rendered)
                self.assertEqual(len(self.fake.mutations), 1)
                self.fake.before_mutation = None
                self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                self.assertIn('CONFIRMED', self.stdout)
                self.assertEqual({k: live[k] for k in desired}, rendered)
                self.assertEqual(len(self.fake.mutations), 1)

    def test_derived_id_collision_refuses_even_with_title_candidate_or_adoption(self):
        self.new_draft(1)
        pid = self.seed()
        collision = self.existing_unit(0, pid, derived=True)
        collision['identifier'] = 'ARC-99'
        collision['title'] = 'Foreign title'
        candidate = self.existing_unit(0, pid)
        for body in ('Unmarked', 'swarm-unit: other/u0\nswarm-repo: owner/repo',
                     'swarm-unit: plan/u0\nswarm-repo: other/repo',
                     'swarm-unit: plan/u0\nswarm-repo: owner/repo\nProse after markers'):
            for reference in (None, 'linear_id', 'identifier'):
                for args in ((), ('--adopt-checked',)):
                    if reference and args and LF.identity_block(body)['values'] == {}:
                        continue
                    with self.subTest(body=body, reference=reference, args=args):
                        self.data['issues'][0].pop('linear_id', None)
                        self.data['issues'][0].pop('identifier', None)
                        if reference:
                            self.data['issues'][0][reference] = collision['id' if reference == 'linear_id' else reference]
                        self.save()
                        collision['description'] = body
                        before = copy.deepcopy(self.fake.issues)
                        draft_before = self.draft.read_bytes()
                        self.assertEqual(self.file(*args), 2, self.stdout + self.stderr)
                        error = ('bound to another unit' if LF.identity_block(body)['values'] else 'unmarked issue') if reference else 'derived issue id collision'
                        self.assertIn(error, self.stderr)
                        self.assertIn('ARC-99', self.stderr)
                        self.assertEqual(self.fake.mutations, [])
                        self.assertEqual(self.fake.issues, before)
                        self.assertEqual(self.draft.read_bytes(), draft_before)
        self.data['issues'][0].pop('identifier')
        self.save()
        del self.fake.issues[collision['id']]
        self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)
        self.assertEqual(self.unit_ids(), {'u0': candidate['id']})

    def test_adoption_by_exact_title_applies_approved_content_once(self):
        self.check_adopted_content('title')

    def test_adoption_by_explicit_id_applies_approved_content_once(self):
        self.check_adopted_content('linear_id')

    def test_adoption_by_explicit_identifier_applies_approved_content_once(self):
        self.check_adopted_content('identifier')

    def test_adoption_requires_approved_content_on_readback(self):
        for field in ('title', 'description'):
            with self.subTest(field=field):
                self.fake.issues.clear()
                self.fake.projects.clear()
                self.new_draft(1)
                live = self.existing_unit(0, self.seed())
                live['title'] = 'Unapproved live title'
                self.data['issues'][0]['linear_id'] = live['id']
                self.save()
                before = live[field]

                def retain_unapproved(q, v):
                    if 'OperationUpdate' in q:
                        live[field] = before
                self.fake.after_mutation = retain_unapproved
                self.assertEqual(self.file('--adopt-checked'), 3, self.stdout + self.stderr)
                self.assertNotIn('CONFIRMED', self.stdout)
                self.assertIn('managed issue changed', self.stdout)
                path = next(p for p in self.records() if p.stem == self.operation())
                self.assertFalse(LI.confirmed(path))
                self.fake.after_mutation = None
                self.assertEqual(live[field], before)

    def test_02_idempotent_and_record_before_mutation(self):
        def check(q, v):
            records = self.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(LI.load_record(records[0])['kind'], 'file')
        self.fake.before_mutation = check
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.fake.before_mutation = None
        before = len(self.fake.mutations)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), before)
        self.assertTrue((self.root / '.hanig/linear-binding.json').exists())
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)

    def test_02_crash_after_each_step_and_after_each_mutation(self):
        # Project, three issues, relation, confirmation; also the write-before-log window.
        for mode, count in (('log', 6), ('mutation', 5)):
            for stop in range(1, count + 1):
                with self.subTest(mode=mode, stop=stop), tempfile.TemporaryDirectory() as directory:
                    patch = mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': directory})
                    with patch:
                        self.fake.issues.clear()
                        self.fake.projects.clear()
                        self.new_draft()
                        self.data['project']['description'] = 'Project\n- scope: approved\n\n'
                        for issue in self.data['issues']:
                            issue['body'] += '\n- kind: code\n\n'
                        self.approve()
                        original = LI.log_step
                        seen = []
                        def crash(*args):
                            if mode == 'log':
                                original(*args)
                            seen.append(args)
                            if len(seen) == stop:
                                raise Crash()
                        if mode == 'mutation':
                            self.fake.after_mutation = crash
                        with mock.patch.object(LI, 'log_step', crash if mode == 'log' else original):
                            with self.assertRaises(Crash):
                                self.file()
                        self.fake.after_mutation = None
                        op = next(Path(directory).glob('*/*/*.json')).stem
                        self.assertEqual(self.replay_file(op), 0, self.stdout + self.stderr)
                        self.assertEqual(len(self.fake.projects), 1)
                        self.assertEqual(len(self.fake.issues), 3)
                        self.assertEqual(len(self.fake.relations()), 1)

    def test_replay_created_issue_before_its_uncreated_blocker(self):
        for boundary in ('mutation', 'checkpoint'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                with mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': directory}):
                    self.fake.issues.clear()
                    self.fake.projects.clear()
                    self.fake.mutations.clear()
                    self.new_draft(2)
                    # Filing creates new issues in derived-id order. Make A,
                    # the first one created, depend on the still-absent B.
                    ordered = sorted(self.data['issues'], key=lambda i: API.derived_id(
                        'issue:workspace/owner/repo/plan/' + i['unit']))
                    a, b = ordered
                    a['blocked_by'], b['blocked_by'] = [b['unit']], []
                    self.approve()
                    aid, bid = [API.derived_id('issue:workspace/owner/repo/plan/' + i['unit'])
                                for i in ordered]
                    checkpoint = LF.Filing.checkpoint

                    def crash_mutation(query, variables):
                        if 'mutation OperationCreate(' in query:
                            self.assertEqual(variables['input']['id'], aid)
                            raise Crash()

                    def crash_checkpoint(handler, step, current=None):
                        checkpoint(handler, step, current)
                        if step == 'issue:' + aid:
                            raise Crash()

                    self.fake.after_mutation = crash_mutation if boundary == 'mutation' else None
                    with mock.patch.object(LF.Filing, 'checkpoint',
                                           crash_checkpoint if boundary == 'checkpoint' else checkpoint):
                        with self.assertRaises(Crash):
                            self.file()
                    self.fake.after_mutation = None
                    self.assertEqual(set(self.fake.issues), {aid})
                    self.assertEqual(self.fake.relations(), [])
                    path = next(Path(directory).glob('*/*/*.json'))
                    spec = LI.load_record(path)
                    self.assertIsNone(spec['identities'][bid])
                    self.assertTrue(LF.has_markers(self.fake.issues[aid]['description'],
                                                  next(r for r in spec['issues'] if r['id'] == aid)['markers']))
                    self.assertEqual(self.replay_file(path.stem), 0, self.stdout + self.stderr)
                    self.assertIn('CONFIRMED', self.stdout)
                    self.assertTrue(LI.confirmed(path))
                    self.assertEqual(set(self.fake.issues), {aid, bid})
                    self.assertEqual(LS.edges_of(list(self.fake.issues.values()))[0], {(bid, aid)})
                    creates = [v['input']['id'] for q, v in self.fake.mutations
                               if 'mutation OperationCreate(' in q]
                    self.assertCountEqual(creates, [aid, bid])
                    self.assertEqual(len(self.fake.relations()), 1)
                    writes = len(self.fake.mutations)
                    self.assertEqual(self.replay_file(path.stem), 0, self.stdout + self.stderr)
                    self.assertEqual(len(self.fake.mutations), writes)

    def test_02_replay_edited_draft_and_redirected_ids_refuse(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        old = copy.deepcopy(self.read())
        self.data['issues'][0]['body'] = 'edited after crash'
        self.approve()
        self.fake.calls.clear()
        path = self.records()[0]
        record = path.read_bytes()
        writes = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 3, self.stdout + self.stderr)
        self.assertIn('replay approval digest differs', self.stderr)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(len(self.fake.mutations), writes)
        self.assertEqual(path.read_bytes(), record)
        self.fake.add_project('foreign')
        for target in ('project', 'issue'):
            self.data = copy.deepcopy(old)
            if target == 'project':
                self.data['project']['linear_id'] = 'foreign'
            else:
                live = self.fake.add('foreign', project=old['project']['linear_id'])
                self.data['issues'][0].update(linear_id=live['id'], identifier=live['identifier'])
            self.save()
            before = len(self.fake.mutations)
            self.assertEqual(self.file(), 2, self.stdout + self.stderr)
            self.assertEqual(len(self.fake.mutations), before)

    def test_03_adoption_steps_and_markers_survive_other_host(self):
        pid = self.seed()
        first = self.existing_unit(0, pid, marked=True, derived=True)
        second = self.existing_unit(1, pid)
        self.assertEqual(self.file(), 2)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)
        ids = self.unit_ids()
        self.assertEqual(ids['u0'], first['id'])
        self.assertEqual(ids['u1'], second['id'])
        self.assertEqual(len(self.fake.issues), 3)
        self.assertEqual(second['description'], self.data['issues'][1]['body'] +
                         '\nswarm-unit: plan/u1\nswarm-repo: owner/repo\n' + self.body_line(1))
        with mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': str(self.root / 'elsewhere')}):
            before = len(self.fake.mutations)
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)
            self.assertEqual(len(self.fake.mutations), before)
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)
            self.assertEqual(len(self.fake.mutations), before)

    def test_03_ambiguities_bound_candidates_and_same_name(self):
        pid = self.seed()
        first = self.existing_unit(0, pid)
        other = self.fake.add('99', project=pid)
        other['title'] = first['title']
        self.assertEqual(self.file('--adopt-checked'), 2)
        self.assertIn('ARC-99', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        del self.fake.issues['99']
        for kind in ('marker', 'identifier', 'derived'):
            with self.subTest(kind=kind):
                self.fake.issues.clear()
                a = self.existing_unit(1, pid, marked=kind == 'marker', derived=kind == 'derived')
                a['title'] = self.data['issues'][0]['title']
                if kind == 'identifier':
                    self.data['issues'][1]['identifier'] = a['identifier']
                    self.save()
                self.assertEqual(self.file('--adopt-checked'), 2, self.stdout + self.stderr)
                self.assertEqual(self.fake.mutations, [])
                self.assertIn('bound to another unit', self.stderr)
                self.data['issues'][1].pop('identifier', None)
                self.save()
        self.fake.issues.clear()
        self.fake.projects.clear()
        self.new_draft()
        for content in ('', 'swarm-repo: owner/repo'):
            self.fake.add_project('old', content)
            self.assertEqual(self.file('--adopt-checked'), 2)
            self.assertIn('old', self.stderr)
            self.assertEqual(self.fake.mutations, [])

    def test_matching_unit_without_repo_requires_checked_adoption(self):
        for trailer, newline in ((False, '\n'), (True, '\n'), (False, '\r\n'), (True, '\r\n')):
            with self.subTest(trailer=trailer, newline=newline):
                self.fake.issues.clear()
                self.fake.projects.clear()
                self.fake.mutations.clear()
                self.new_draft(1)
                pid = self.seed()
                live = self.existing_unit(0, pid)
                body = ('Existing prose\n`swarm-unit: plan/u0`\n').replace('\n', newline)
                suffix = LI.description('', set(), set(), 'old-op', 'old-owner') if trailer else ''
                live['description'] = body + suffix
                self.assertEqual(self.file(), 2, self.stdout + self.stderr)
                self.assertEqual(self.fake.mutations, [])
                self.assertEqual(live['description'], body + suffix)
                self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)
                self.assertEqual(self.unit_ids(), {'u0': live['id']})
                self.assertEqual(live['description'], ('Approved prose 0\nswarm-unit: plan/u0\n' +
                                 'swarm-repo: owner/repo\n' + self.body_line() + '\n' + suffix).rstrip())
                mutations = len(self.fake.mutations)
                self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                self.assertEqual(len(self.fake.mutations), mutations)

    def test_checked_adoption_refuses_different_repository(self):
        self.new_draft(1)
        pid = self.seed()
        live = self.existing_unit(0, pid)
        for unit in ('swarm-unit: plan/u0\n',):
            for trailer in (False, True):
                with self.subTest(unit=unit, trailer=trailer):
                    body = 'Existing prose\n' + unit + '`swarm-repo: other/repository`\n'
                    if trailer:
                        body = LI.description(body, set(), set(), 'old-op', 'old-owner')
                    live['description'] = body
                    for args in ((), ('--adopt-checked',)):
                        self.assertEqual(self.file(*args), 2, self.stdout + self.stderr)
                        self.assertIn('bound to another unit', self.stderr)
                        self.assertEqual(self.fake.mutations, [])
                        self.assertEqual(live['description'], body)

    def test_lone_and_prose_only_foreign_markers_do_not_conflict(self):
        for prose in ('`swarm-repo: other/repository`', 'swarm-unit: plan/other',
                      'swarm-unit: plan/other\nswarm-repo: other/repository\nEnd of example'):
            for pr3 in (False, True):
                with self.subTest(prose=prose, pr3=pr3):
                    self.fake.issues.clear()
                    self.fake.projects.clear()
                    self.new_draft(1)
                    pid = self.seed()
                    live = self.existing_unit(0, pid)
                    suffix = LI.description('', set(), set(), 'old-op', 'owner') if pr3 else ''
                    live['description'] = prose + '\n' + suffix
                    self.fake.projects[pid]['content'] = prose
                    self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)
                    self.assertEqual(self.fake.projects[pid]['content'],
                                     prose + '\nswarm-plan: plan\nswarm-repo: owner/repo')
                    self.assertNotIn('other/repository', live['description'])
                    self.assertNotIn('plan/other', live['description'])
                    self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
                    writes = len(self.fake.mutations)
                    self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                    self.assertEqual(len(self.fake.mutations), writes)

    def test_explicit_unmarked_derived_issue_adoption(self):
        for selector in ('linear_id', 'identifier'):
            for pr3 in (False, True):
                with self.subTest(selector=selector, pr3=pr3):
                    self.fake.issues.clear()
                    self.fake.projects.clear()
                    self.new_draft(1)
                    live = self.existing_unit(0, self.seed(), derived=True, trailer=pr3)
                    self.data['issues'][0][selector] = live['id' if selector == 'linear_id' else selector]
                    self.save()
                    writes = len(self.fake.mutations)
                    self.assertEqual(self.file(), 2, self.stdout + self.stderr)
                    self.assertIn('unmarked issue', self.stderr)
                    self.assertEqual(len(self.fake.mutations), writes)
                    self.fake.ignore_update = True
                    self.assertEqual(self.file('--adopt-checked'), 3, self.stdout + self.stderr)
                    self.fake.ignore_update = False
                    self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
                    self.assertTrue(live['description'].startswith('Approved prose 0\nswarm-unit:'))
                    self.assertEqual(bool(LI.trailer(live['description'])), pr3)
                    writes = len(self.fake.mutations)
                    self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                    self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
                    self.assertEqual(len(self.fake.mutations), writes)

    def test_archived_observed_blockers_kept_reported_and_cycle_checked(self):
        for field, value in (('archivedAt', 'date'), ('trashed', True)):
            with self.subTest(field=field):
                self.fake.issues.clear()
                self.fake.projects.clear()
                self.new_draft(2)
                pid = self.seed()
                a = self.existing_unit(0, pid, marked=True)
                b = self.existing_unit(1, pid, marked=True)
                external = self.fake.add('80', project='external')
                external[field] = value
                self.fake.edge('80', a['id'])
                edges = LS.edges_of(list(self.fake.issues.values()))[0]
                self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                self.assertIn('kept outside-plan blocker 80 -> ' + a['id'], self.stdout)
                self.assertEqual(LS.edges_of(list(self.fake.issues.values()))[0], edges | {(a['id'], b['id'])})
                self.assertEqual(self.read()[T.READBACK]['edges'][a['identifier']], ['ARC-80'])
                self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
                self.assertEqual(self.file(), 0, self.stdout + self.stderr)
                self.fake.edge(b['id'], '80')
                writes = len(self.fake.mutations)
                self.assertEqual(self.file(), 2, self.stdout + self.stderr)
                self.assertIn('resulting blocks cycle', self.stderr)
                self.assertEqual(len(self.fake.mutations), writes)

    def test_isolated_plan_unit_filing_audit_relationless_is_clean(self):
        self.new_draft(1)
        records = []
        original = LS.audit
        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertIsNotNone(kwargs.get('filing'))
            records.append(result[0])
            return result
        with mock.patch.object(LS, 'audit', capture):
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)
            self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertEqual(next(c['verdict'] for c in record['checks'] if c['id'] == 'relationless'), 'CLEAN')

    def test_03_adopted_project_refiles_by_markers_without_flag(self):
        pid = self.seed(markers=False, derived=False)
        self.assertEqual(self.file(), 2)
        self.assertEqual(self.fake.mutations, [])
        for n in range(3):
            self.existing_unit(n, pid, marked=True)
        self.assertEqual(self.file(), 2)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)
        self.assertTrue(LF.has_markers(self.fake.projects[pid]['content'], {'swarm-plan': 'plan', 'swarm-repo': 'owner/repo'}))
        self.data = self.read()
        self.data['issues'][1]['blocked_by'] = []
        self.approve()
        before = len(self.fake.mutations)
        with mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': str(self.root / 'other-host')}):
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertGreater(len(self.fake.mutations), before)
        self.assertEqual(self.fake.relations(), [])
        self.assertEqual(len(self.fake.projects), 1)
        self.assertEqual(len(self.fake.issues), 3)
        self.assertEqual(self.read()['project']['linear_id'], pid)

    def test_03_repository_namespace(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.new_draft(repository='other/repo')
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.projects), 2)
        self.assertEqual(len(self.fake.issues), 6)

    def test_04_blocked_by_truth_internal_removal_external_kept(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        ids = self.unit_ids()
        external = '12345678-1234-1234-1234-123456789abc'
        self.fake.add(external, project='external')
        self.fake.add('99', project=self.data['project']['linear_id'])
        self.fake.edge(external, ids['u1'])
        self.fake.edge('99', ids['u1'])
        self.data['issues'][1]['blocked_by'] = []
        for issue in self.data['issues']:
            issue['add_blocked_by'] = ['u2']
            issue['remove_blocked_by'] = [external, 'ARC-99']
        self.approve()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertIn('kept outside-plan blocker', self.stdout)
        self.assertEqual(LS.edges_of(list(self.fake.issues.values()))[0], {(external, ids['u1']), ('99', ids['u1'])})
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.read()
        self.assertEqual(self.data['issues'][1]['remove_blocked_by'], [])

    def test_04a_trailers_crash_before_relation_replay_clean(self):
        pid = self.seed()
        a = self.existing_unit(0, pid, trailer=True)
        b = self.existing_unit(1, pid, trailer=True)
        x = self.fake.add('99', project='external')
        self.fake.edge('99', b['id'])
        b['description'] = LI.description('Existing prose\n`swarm-deps-by: earlier-op`\n',
                                         {'ARC-99'}, set(), 'old-op', 'old-owner')
        def crash(q, v):
            if 'OperationUpdate' in q and v['id'] == b['id']:
                raise Crash()
        self.fake.after_mutation = crash
        with self.assertRaises(Crash):
            self.file('--adopt-checked')
        self.fake.after_mutation = None
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertIn('plan checks CLEAN', self.stdout)
        for n, i in enumerate((a, b)):
            self.assertIsNone(LI.declared_edges(i))
            self.assertTrue(i['description'].startswith(self.data['issues'][n]['body'] + '\n'))
            self.assertEqual(LI.trailer(i['description'])['op'], 'old-op')
        self.assertIn('`swarm-deps-by: earlier-op`', b['description'])
        self.assertEqual(LI.dependency_names(b)[0], {'ARC-1', 'ARC-99'})
        previous_by = LI.trailer(b['description'])['by']
        self.read()
        self.data['issues'][1]['blocked_by'] = []
        self.approve()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertIn('`swarm-deps-by: %s`' % previous_by, b['description'])
        self.assertNotEqual(LI.trailer(b['description'])['by'], previous_by)
        self.assertIsNone(LI.declared_edges(b))

    def test_05_lagging_listing_reads_every_unit_by_id(self):
        self.fake.lag = True
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        ids = set(self.unit_ids().values())
        last_write = max(n for n, (q, _) in enumerate(self.fake.calls) if q.startswith('mutation'))
        reads = {iid for q, v in self.fake.calls[last_write + 1:] if 'IssueBatch' in q for iid in v['ids']}
        self.assertTrue(ids <= reads)
        self.assertEqual(len(self.data[T.READBACK]['edges']), 3)

    def test_05_concurrent_cycle_through_adhoc_is_not_confirmed(self):
        def race(q, v):
            if 'OperationRelationCreate' in q:
                a, b = v['input']['issueId'], v['input']['relatedIssueId']
                self.fake.add('80', project='external')
                self.fake.add('81', project='elsewhere')
                self.fake.edge(b, '80')
                self.fake.edge('80', '81')
                self.fake.edge('81', a)
        self.fake.after_mutation = race
        self.assertEqual(self.file(), 3, self.stdout + self.stderr)
        self.assertNotIn('CONFIRMED', self.stdout)
        self.assertFalse(LI.confirmed(self.records()[0]))

    def test_05_incomplete_pagination_and_managed_drift(self):
        pid = self.seed()
        self.existing_unit(0, pid, marked=True)
        self.fake.paging = 'repeat'
        # Simulate the incomplete nested page through the real Reader consumer.
        self.fake.nested = 'fail'
        self.fake.issues[next(iter(self.fake.issues))]['relations'] = connection([], True, 'nested')
        self.assertEqual(self.file(), 2)
        self.assertEqual(self.fake.mutations, [])
        self.fake.issues.clear()
        self.fake.nested = None
        self.fake.ignore_update = True
        self.existing_unit(0, pid)
        self.assertEqual(self.file('--adopt-checked'), 3)
        self.assertNotIn('CONFIRMED', self.stdout)

    def test_06_thirty_units_forty_edges_request_budget(self):
        self.new_draft(30)
        for n, issue in enumerate(self.data['issues']):
            issue['blocked_by'] = ['u%d' % (n - 1)] if n else []
            if 2 <= n <= 12:
                issue['blocked_by'].append('u%d' % (n - 2))
        self.approve()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.relations()), 40)
        self.assertLessEqual(len(self.fake.calls), 120)
        print('file budget: %d requests (30 units, 40 edges)' % len(self.fake.calls))

    def test_07_secrets_refused_no_output_files_or_children(self):
        base = copy.deepcopy(self.data)
        for where in ('name', 'slug', 'summary', 'description', 'team', 'repository', 'title', 'body', 'blocked_by', 'approver'):
            with self.subTest(field=where):
                self.data = copy.deepcopy(base)
                if where in self.data['project']:
                    self.data['project'][where] = KEY
                elif where == 'approver':
                    self.data['approval']['granted_by'] = KEY
                else:
                    self.data['issues'][0][where] = [KEY] if where == 'blocked_by' else KEY
                self.data['approval']['content_digest'] = T.content_digest(self.data)
                self.save()
                before = self.draft.read_bytes()
                self.fake.calls.clear()
                with mock.patch.object(subprocess, 'run', side_effect=AssertionError('child')):
                    self.assertEqual(self.file(), 2)
                self.assertEqual(self.fake.calls, [])
                self.assertEqual(self.draft.read_bytes(), before)
                self.assertEqual(self.records(), [])
        self.data = base
        self.approve()
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('child')):
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(KEY.encode(), path.read_bytes(), str(path))
        rb = self.read()[T.READBACK]
        self.assertNotIn('Approved prose', json.dumps(rb))
        self.assertNotIn('title', json.dumps(rb))

    def test_preview_writes_nothing_and_runs_refusals(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(self.file('--preview'), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.data['issues'][0]['blocked_by'] = ['u1']
        self.approve()
        self.assertEqual(self.file('--preview'), 2)
        self.assertEqual(self.fake.mutations, [])

    def test_repository_from_origin_and_exact_edge_ids(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'git@github.com:Owner/Repo.git'], check=True)
        self.assertEqual(T.origin_repository(repo), 'Owner/Repo')
        d = {'issues': [{'unit': 'a', 'identifier': 'ARC-1', 'blocked_by': [], 'linear_id': 'uuid-a'},
                        {'unit': 'b', 'identifier': 'ARC-2', 'blocked_by': [], 'linear_id': 'uuid-b'}]}
        for ref in ('arc-1', 'ARC-1 ', ' UUID-A'):
            T.sync_blocked_by(d, {'read_at': 'now', 'edges': {'ARC-2': [ref]}})
            self.assertEqual(d['issues'][1]['remove_blocked_by'], [])
        T.sync_blocked_by(d, {'read_at': 'now', 'edges': {'ARC-2': ['ARC-1']}})
        self.assertEqual(d['issues'][1]['remove_blocked_by'], ['ARC-1'])

    def test_preflight_shape_and_identity_guards(self):
        base = copy.deepcopy(self.data)
        edits = [
            (lambda d: d['approval'].update(state='required'), 'draft approval required'),
            (lambda d: d['project'].update(repository='no-slash'), 'forge path'),
            (lambda d: d['project'].update(slug='a/b'), 'slug must not contain'),
            (lambda d: d['project'].update(summary=7), 'summary must be text'),
            (lambda d: d.update(issues={}), 'issues must be a list'),
            (lambda d: d['issues'][1].update(unit='u0'), 'duplicate unit'),
            (lambda d: d['issues'][1].update(unit='u/1'), 'unit containing /'),
            (lambda d: d['issues'][1].update(body=7), 'body must be text'),
            (lambda d: d['issues'][1].update(blocked_by='u0'), 'blocked_by must be a list'),
            (lambda d: d['issues'][1].update(blocked_by=[7]), 'contain unit ids'),
            (lambda d: d['issues'][1].update(blocked_by=['absent']), 'outside this draft')]
        for edit, error in edits:
            with self.subTest(error=error):
                self.data = copy.deepcopy(base)
                edit(self.data)
                # Valid JSON can be approved even when the filing schema refuses it.
                self.data['approval']['content_digest'] = T.content_digest(self.data)
                self.save()
                self.fake.calls.clear()
                self.assertEqual(self.file(), 2)
                self.assertIn(error, self.stderr)
                self.assertEqual(self.fake.calls, [])
        self.data = base
        self.data['project']['team'] = 'Missing'
        self.approve()
        self.assertEqual(self.file(), 2)
        self.assertIn('team ambiguous', self.stderr)
        self.assertEqual(self.fake.mutations, [])

    def test_named_absences_mismatches_and_conflicting_markers(self):
        self.data['project']['linear_id'] = 'missing'
        self.save()
        self.assertEqual(self.file('--adopt-checked'), 2)
        self.assertIn('recorded project not found', self.stderr)
        pid = self.seed()
        self.fake.projects[pid]['teams'] = connection([dict(self.fake.team, id='wrong')])
        self.assertEqual(self.file('--adopt-checked'), 2)
        self.assertIn('project team disagrees', self.stderr)
        self.fake.projects[pid]['teams'] = connection([self.fake.team])
        self.fake.projects[pid]['content'] = 'swarm-plan: other\nswarm-repo: owner/repo'
        self.assertEqual(self.file('--adopt-checked'), 2)
        self.assertIn('conflicting swarm-plan', self.stderr)
        self.fake.projects[pid]['content'] = 'swarm-plan: plan\nswarm-repo: owner/repo'
        self.data['issues'][0]['identifier'] = 'ARC-999'
        self.save()
        self.assertEqual(self.file('--adopt-checked'), 2)
        self.assertIn('issue identifier disagrees on read-back: ARC-999', self.stderr)
        a = self.existing_unit(0, pid, marked=True)
        self.data['issues'][0].update(linear_id=a['id'], identifier='ARC-999')
        self.save()
        self.assertEqual(self.file('--adopt-checked'), 2)
        self.assertIn('issue identifier disagrees on read-back: ARC-999', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        # A filtered project response must still agree exactly with its requested id.
        original = self.fake.dispatch
        def changed(q, v):
            result = original(q, v)
            if 'query FilingProjects' in q and 'id' in v['filter']:
                result['projects']['nodes'][0]['id'] += ' '
            return result
        with mock.patch.object(self.fake, 'dispatch', changed):
            self.assertEqual(self.file('--adopt-checked'), 2)
        self.assertIn('project filter disagrees', self.stderr)

    def test_replay_deletion_identity_and_field_drift_guards(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        ids = self.unit_ids()
        before = copy.deepcopy(self.fake.issues)
        mutations = len(self.fake.mutations)
        iid = ids['u0']
        edits = [(lambda: self.fake.issues[iid].update(identifier='ARC-999'), 'managed identifier changed'),
                 (lambda: self.fake.issues[iid].update(description='later prose'), 'managed issue changed'),
                 (lambda: self.fake.issues.pop(iid), 'managed issue deleted')]
        # Independent unit deletion avoids a dangling-edge coverage refusal.
        edits[-1] = (lambda: self.fake.issues.pop(ids['u2']), 'managed issue deleted')
        for edit, error in edits:
            self.fake.issues = copy.deepcopy(before)
            edit()
            self.assertEqual(self.replay_file(), 3)
            self.assertIn(error, self.stderr)
            self.assertEqual(len(self.fake.mutations), mutations)
        self.fake.issues = before
        pid = self.data['project']['linear_id']
        self.fake.projects[pid]['teams'] = connection([dict(self.fake.team, id='wrong')])
        self.assertEqual(self.replay_file(), 3)
        self.assertIn('project team changed', self.stderr)
        self.fake.projects[pid]['teams'] = connection([self.fake.team])
        self.fake.projects[pid]['name'] = 'later name'
        self.assertEqual(self.replay_file(), 3)
        self.assertIn('managed project changed', self.stderr)
        self.fake.projects[pid]['name'] = 'Plan'
        del self.fake.projects[pid]
        self.assertEqual(self.replay_file(), 3)
        self.assertIn('managed project deleted', self.stderr)

    def test_replay_refuses_redirected_progress_and_missing_record(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.read()
        self.data['project']['linear_id'] = 'elsewhere'
        self.save()
        before = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 3)
        self.assertIn('operation binding differs', self.stderr)
        self.assertEqual(len(self.fake.mutations), before)
        self.assertEqual(self.replay_file('00000000-0000-0000-0000-000000000000'), 3)
        self.assertIn('operation not found', self.stderr)

    def test_created_identity_collision_stops_before_later_mutations(self):
        def collision(q, v):
            if 'OperationCreate' in q:
                self.fake.issues[v['input']['id']]['description'] = 'unrelated content'
        self.fake.after_mutation = collision
        self.assertEqual(self.file(), 3)
        self.assertIn('created issue missing or lacks draft markers', self.stdout)
        self.assertEqual(len(self.fake.issues), 1)
        self.assertEqual(self.fake.relations(), [])

    def test_replay_preflight_cycle_checks_external_return_path(self):
        pid = self.seed()
        for n in range(3):
            self.existing_unit(n, pid, marked=True)
        self.fake.add('80', project='external')
        self.fake.add('81', project='external')
        self.fake.edge('2', '80')
        self.fake.edge('81', '1')
        original = LI.log_step
        def crash(path, step):
            original(path, step)
            if step == 'project':
                raise Crash()
        with mock.patch.object(LI, 'log_step', crash):
            with self.assertRaises(Crash):
                self.file()
        self.fake.edge('80', '81')
        before = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 3)
        self.assertIn('resulting blocks cycle', self.stderr)
        self.assertEqual(len(self.fake.mutations), before)

    def test_lock_race_and_exclusion(self):
        original = LS.project_lock
        from contextlib import contextmanager
        @contextmanager
        def race(workspace, project):
            self.draft.write_text(self.draft.read_text() + ' ')
            yield
        with mock.patch.object(LS, 'project_lock', race):
            self.assertEqual(self.file(), 2)
        self.assertIn('draft changed while acquiring lock', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        pid = API.derived_id('project:workspace/owner/repo/plan')
        with original('workspace', pid):
            self.assertEqual(self.file(), 2)
        self.assertIn('another writer holds', self.stderr)
        self.assertEqual(self.fake.mutations, [])

    def test_repeated_external_uuid_removal_is_satisfied(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        ids = self.unit_ids()
        iid = '12345678-1234-1234-1234-123456789abc'
        self.fake.add(iid, project='external')['identifier'] = 'ARC-99'
        self.fake.edge(iid, ids['u1'])
        binding = self.root / '.hanig/linear-binding.json'
        identifier = self.fake.issues[ids['u1']]['identifier']
        for _ in range(2):
            self.assertEqual(self.cli('issue', 'edit', identifier, '--binding', str(binding),
                                     '--remove-blocked-by', iid, '--approver', 'owner'), 0,
                             self.stdout + self.stderr)
        self.assertFalse(any(e['issue']['id'] == iid for e in self.fake.relations()))


    def test_malformed_trailer_refuses_before_mutation(self):
        pid = self.seed()
        i = self.existing_unit(0, pid, marked=True)
        i['description'] += '`swarm-deps: broken`\n'
        self.assertEqual(self.file(), 2)
        self.assertIn('malformed dependency trailer', self.stderr)
        self.assertEqual(self.fake.mutations, [])

    def test_final_quoted_dependency_example_refuses_adoption_until_moved(self):
        self.new_draft(1)
        live = self.existing_unit(0, self.seed())
        live['description'] = 'Quoted example:\n`swarm-deps: example`'
        self.assertEqual(self.file('--adopt-checked'), 2, self.stdout + self.stderr)
        self.assertIn('malformed dependency trailer', self.stderr)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.records(), [])
        live['description'] += '\nEnd of example'
        self.assertEqual(self.file('--adopt-checked'), 0, self.stdout + self.stderr)

    def test_fenced_dependency_example_files_refiles_and_replays(self):
        self.new_draft(1)
        self.data['issues'][0]['body'] = 'Example:\n```text\n`swarm-deps: example`\n```\n'
        self.approve()
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        writes = len(self.fake.mutations)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)

    def test_replay_new_edge_refuses_without_overwriting(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        ids = self.unit_ids()
        self.fake.add('99', project='external')
        self.fake.edge('99', ids['u0'])
        before = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 3)
        self.assertIn('managed edges changed', self.stderr)
        self.assertEqual(len(self.fake.mutations), before)

    def test_scoped_audit_gate_consumes_every_required_check(self):
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        original = LS.audit
        for check in ('binding', 'coverage', 'plan_edges', 'misplaced', 'declared_edges', 'cycle'):
            for verdict in ('UNKNOWN', 'DRIFT'):
                with self.subTest(check=check, verdict=verdict):
                    def audit(*args, **kwargs):
                        record, reader = original(*args, **kwargs)
                        for row in record['checks']:
                            if row['id'] == check:
                                row['verdict'] = verdict
                        return record, reader
                    with mock.patch.object(LS, 'audit', audit):
                        self.assertEqual(self.replay_file(), 3)
                        self.assertIn(check + '=' + verdict, self.stdout)
                        self.assertNotIn('CONFIRMED', self.stdout)

    def test_scoped_audit_gate_refuses_each_missing_required_check(self):
        original = LS.audit
        for check in ('binding', 'coverage', 'plan_edges', 'misplaced', 'declared_edges', 'cycle'):
            with self.subTest(check=check), tempfile.TemporaryDirectory() as directory:
                with mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': directory}):
                    self.fake.issues.clear()
                    self.fake.projects.clear()
                    self.new_draft()

                    def audit(*args, **kwargs):
                        record, reader = original(*args, **kwargs)
                        self.assertIn(check, [row['id'] for row in record['checks']])
                        record['checks'] = [row for row in record['checks'] if row['id'] != check]
                        return record, reader

                    with mock.patch.object(LS, 'audit', audit):
                        self.assertEqual(self.file(), 3, self.stdout + self.stderr)
                        self.assertIn('INCOMPLETE', self.stdout)
                        self.assertIn(check + '=MISSING', self.stdout)
                        self.assertNotIn('CONFIRMED', self.stdout)
                        path = next(Path(directory).glob('*/*/*.json'))
                        self.assertFalse(LI.confirmed(path))
                        writes = len(self.fake.mutations)
                        self.assertEqual(self.replay_file(path.stem), 3, self.stdout + self.stderr)
                        self.assertFalse(LI.confirmed(path))
                        self.assertEqual(len(self.fake.mutations), writes)
                    self.assertEqual(self.replay_file(path.stem), 0, self.stdout + self.stderr)
                    self.assertIn('CONFIRMED', self.stdout)
                    self.assertEqual(len(self.fake.mutations), writes)

    def test_scoped_audit_gate_does_not_require_relationless(self):
        original = LS.audit

        def audit(*args, **kwargs):
            record, reader = original(*args, **kwargs)
            record['checks'] = [row for row in record['checks'] if row['id'] != 'relationless']
            return record, reader

        with mock.patch.object(LS, 'audit', audit):
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)
            writes = len(self.fake.mutations)
            self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
            self.assertIn('CONFIRMED', self.stdout)
            self.assertEqual(len(self.fake.mutations), writes)

    def test_filing_audit_ignores_search_hits_without_this_plans_trailer_identity(self):
        self.new_draft(1)
        for n, identity in enumerate(('swarm-unit: other/u0', 'swarm-unit: plan/unknown',
                                       'swarm-plan: plan', 'swarm-unit: plan/u0\nProse')):
            for trailer in (False, True):
                body = ('Example: swarm-unit: plan/u0\nEnd of example\n' + identity +
                        '\nswarm-repo: owner/repo\n')
                if trailer:
                    body = LI.description(body, set(), set(), 'other-op', 'owner')
                self.fake.add('foreign-%d-%s' % (n, trailer), project='other', description=body)
        self.assertEqual(self.file(), 0, self.stdout + self.stderr)
        self.assertIn('plan checks CLEAN', self.stdout)
        writes = len(self.fake.mutations)
        self.assertEqual(self.replay_file(), 0, self.stdout + self.stderr)
        self.assertEqual(len(self.fake.mutations), writes)
        # An actual trailer identity for this plan still counts as misplaced.
        self.fake.add('misplaced', project='other',
                      description='swarm-unit: plan/u0\nswarm-repo: owner/repo')
        self.assertEqual(self.replay_file(), 3, self.stdout + self.stderr)
        self.assertIn('misplaced=DRIFT', self.stdout)

    def test_raced_duplicate_unit_marker_makes_plan_audit_unknown(self):
        def race(q, v):
            if 'OperationRelationCreate' in q:
                a = self.fake.issues[v['input']['issueId']]
                other = self.fake.add('99', project=a['project']['id'], description=a['description'])
                other['title'] = 'different title'
        self.fake.after_mutation = race
        self.assertEqual(self.file(), 3)
        self.assertIn('plan_edges=UNKNOWN', self.stdout)
        self.assertFalse(LI.confirmed(self.records()[0]))

    def test_operation_is_fsynced_before_first_mutation(self):
        import stat
        original = os.fsync
        synced_files = []
        def fsync(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                synced_files.append(fd)
            original(fd)
        def check(q, v):
            self.assertTrue(synced_files, 'no file fsync before mutation')
        self.fake.before_mutation = check
        with mock.patch.object(os, 'fsync', fsync):
            self.assertEqual(self.file(), 0, self.stdout + self.stderr)

    def test_public_entrypoint_and_transport_boundary(self):
        # Real __main__, not just a call to main(); credentials never leave the fake.
        script = ROOT / 'skills/hanig-project/scripts/linear_sync.py'
        source = """
import runpy, sys
from tests.test_linear_file import FileLinear, API
API.transport = FileLinear()
sys.argv = [sys.argv[1], 'file', '--draft', sys.argv[2]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        p = subprocess.run([sys.executable, '-c', source, str(script), str(self.draft)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           env=dict(os.environ, HOME=str(self.root)), timeout=20)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('CONFIRMED', p.stdout)
        self.assertNotIn(KEY, p.stdout + p.stderr)
        for rel in ('skills/hanig-project/scripts/linear_file.py',
                    'skills/hanig-project/scripts/tickets.py',
                    'skills/hanig-swarm/scripts/swarm.py',
                    'skills/hanig-project/scripts/drain_contract.py'):
            tree = ast.parse((ROOT / rel).read_text())
            imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            imported.update(a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names)
            self.assertFalse(imported & {'urllib', 'urllib.request', 'http', 'http.client', 'socket', 'requests'}, rel)
            if not rel.endswith('linear_file.py'):
                self.assertFalse(imported & {'linear_file', 'linear_issue', 'linear_sync', 'linear_api'}, rel)


    def test_digest_covers_every_approved_field(self):
        base = copy.deepcopy(self.data)
        for location, fields in (('project', ('name', 'slug', 'summary', 'description', 'team', 'repository')),
                                 ('issue', ('unit', 'title', 'body', 'blocked_by'))):
            for field in fields:
                self.data = copy.deepcopy(base)
                target = self.data['project'] if location == 'project' else self.data['issues'][2]
                target[field] = ['u0'] if field == 'blocked_by' else target[field] + '-changed'
                self.assertNotEqual(T.content_digest(self.data), base['approval']['content_digest'], field)
                self.save()
                self.fake.calls.clear()
                self.assertEqual(self.file(), 2, field)
                self.assertIn('content_digest', self.stderr)
                self.assertEqual(self.fake.calls, [])


    def test_tickets_cli_records_digest_and_repository_without_key_child(self):
        import argparse
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps({'name': 'p', 'units': [{'id': 'a', 'outputs': ['a']}]}))
        script = ROOT / 'skills/hanig-project/scripts/tickets.py'
        env = {k: v for k, v in os.environ.items() if k != API.KEY_ENV}
        subprocess.run([sys.executable, str(script), 'draft', str(plan), '--out', str(self.draft),
                        '--repository', 'Owner/Exact'], check=True, env=env, stdout=subprocess.PIPE)
        subprocess.run([sys.executable, str(script), 'approve', str(self.draft), '--approver', 'owner'],
                       check=True, env=env, stdout=subprocess.PIPE)
        d = self.read()
        self.assertEqual(d['project']['repository'], 'Owner/Exact')
        self.assertEqual(d['approval']['content_digest'], T.content_digest(d))
        with mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(T.cmd_approve(argparse.Namespace(tickets=str(self.draft), approver='owner')), 0)
        self.assertEqual(self.read()['approval']['content_digest'], T.content_digest(self.data))
        before = self.draft.read_bytes()
        with mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(T.cmd_approve(argparse.Namespace(tickets=str(self.draft), approver=' ')), 2)
        self.assertEqual(self.draft.read_bytes(), before)
        run = mock.Mock(return_value=argparse.Namespace(returncode=0, stdout='git@github.com:Owner/Exact.git\n'))
        with mock.patch.object(T.subprocess, 'run', run):
            self.assertEqual(T.origin_repository(self.root), 'Owner/Exact')
        self.assertNotIn(API.KEY_ENV, run.call_args.kwargs['env'])


    def test_changed_snapshot_refuses_before_mutation(self):
        pid = self.seed()
        self.existing_unit(0, pid, marked=True)
        original = self.fake.dispatch
        def moved(q, v):
            data = original(q, v)
            if 'query ProjectIssues' in q and 'nodes { id updatedAt }' in q:
                for row in data['project']['issues']['nodes']:
                    row['updatedAt'] = 'concurrent-change'
            return data
        with mock.patch.object(self.fake, 'dispatch', moved):
            self.assertEqual(self.file(), 2)
        self.assertIn('snapshot moved', self.stderr)
        self.assertEqual(self.fake.mutations, [])




class TestDocumentedLinearCommands(unittest.TestCase):
    def test_readme_and_project_skill_commands_parse_without_network(self):
        seen = set()
        with mock.patch.object(API, 'load_key', side_effect=AssertionError('loaded credentials')) as key, \
                mock.patch.object(API, 'transport', side_effect=AssertionError('network call')) as transport:
            for relative in ('README.md', 'skills/hanig-project/SKILL.md'):
                commands = []
                for block in re.findall(r'^```[^\n]*\n(.*?)^```', (ROOT / relative).read_text(), re.M | re.S):
                    for line in block.replace('\\\n', ' ').splitlines():
                        if 'linear_sync.py' in line:
                            commands.append(line.strip())
                self.assertTrue(commands, relative + ' has no CLI examples')
                for command in commands:
                    # Brackets denote optional groups in these examples. Test
                    # every combination, including exactly the bare command.
                    groups = re.findall(r'\[([^\[\]]*)\]', command)
                    for enabled in itertools.product((False, True), repeat=len(groups)):
                        choices = iter(enabled)
                        expanded = re.sub(r'\[([^\[\]]*)\]',
                                          lambda m: m[1] if next(choices) else '', command)
                        words = shlex.split(expanded, comments=True)
                        with self.subTest(document=relative, command=expanded):
                            self.assertEqual(words[0], 'python3')
                            self.assertTrue(words[1].endswith('/linear_sync.py'))
                            args = LS.parse_args(words[2:])
                            self.assertEqual(args.command, words[2])
                            seen.add(args.command)
                            if args.command in ('file', 'replay'):
                                self.assertFalse(hasattr(args, 'state_dir'))
            key.assert_not_called()
            transport.assert_not_called()
        self.assertTrue({'file', 'replay', 'drain', 'audit', 'issue'} <= seen)

    def test_state_directory_is_not_a_filing_argument(self):
        for argv in (['file', '--draft', 'tickets.json'],
                     ['replay', 'OPERATION_ID', '--draft', 'tickets.json']):
            with self.subTest(argv=argv):
                self.assertEqual(LS.parse_args(argv).draft, 'tickets.json')
                with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                    LS.parse_args(argv + ['--state-dir', 'STATE'])
                self.assertEqual(caught.exception.code, 2)
        with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
            LS.parse_args(['drain', '--binding', 'binding.json'])
        self.assertEqual(caught.exception.code, 2)


class TestProjectQueryComplexity(unittest.TestCase):
    def test_nested_team_page_stays_within_the_measured_limit(self):
        # Measured live on 2026-10-06: 50 projects x 100 teams is refused as
        # too complex, 50 x 20 is accepted.
        self.assertLessEqual(LF.PROJECT_TEAM_PAGE, 20)
        self.assertIn("teams(first: %d)" % LF.PROJECT_TEAM_PAGE, LF.PROJECT_FIELDS)
        self.assertNotIn("teams(first: 100)", LF.PROJECT_FIELDS)

if __name__ == '__main__':
    unittest.main()
