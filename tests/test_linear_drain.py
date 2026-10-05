"""PR 2 acceptance through the real CLI, fake transport and offline receipt child."""
import ast
import contextlib
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tests.test_linear_audit import AuditCase, FakeLinear, KEY, ROOT, connection

PROJECT = ROOT / 'skills/hanig-project/scripts'
sys.path.insert(0, str(PROJECT))
import linear_api as API
import linear_sync as LS
import drain_contract as DC
import merge_unit as MU


class DrainLinear(FakeLinear):
    def __init__(self):
        super().__init__()
        self.comments = {}
        self.mutations = []
        self.comment_paging = None
        self.hide_comments = False
        self.omit_listing = False
        self.lag_state = False
        self.state_before = {}
        self.mutation_failure = None
        self.reject_after_create = False
        # API order deliberately puts each type's higher-position state first,
        # so choosing by list order picks '-second' and fails the tests.
        first = [{'id': kind + '-first', 'type': kind, 'position': 1.0}
                 for kind in ('started', 'completed', 'unstarted')]
        self.states = [dict(s, id=s['type'] + '-second', position=2.0) for s in first] + first

    def __call__(self, body, headers, timeout=None):
        request = json.loads(body)
        q, v = request['query'], request['variables']
        assert headers['Authorization'] == KEY
        self.calls.append((q, v))
        if self.fail and self.fail in q:
            raise OSError('failed ' + KEY)
        if q.startswith('mutation'):
            self.mutations.append((q, v))
            if self.mutation_failure and self.mutation_failure in q:
                raise OSError('mutation failed ' + KEY)
            if 'IntentCommentCreate' in q:
                value = v['input']
                if value['id'] in self.comments:
                    return 200, json.dumps({'errors': [{'message': 'duplicate'}]}).encode()
                self.comments[value['id']] = {'id': value['id'], 'body': value['body'],
                                             'issue': {'id': value['issueId']}}
                if self.reject_after_create:
                    return 200, json.dumps({'errors': [{'message': 'ambiguous'}]}).encode()
                data = {'commentCreate': {'success': True}}
            else:
                assert 'DrainState' in q
                self.state_before[v['id']] = copy.deepcopy(self.issues[v['id']]['state'])
                state = next(s for s in self.states if s['id'] == v['state'])
                self.issues[v['id']]['state'] = {'type': state['type'], 'name': state['id']}
                data = {'issueUpdate': {'success': True}}
        else:
            data = self.dispatch(q, v)
        return 200, json.dumps({'data': data}).encode()

    def dispatch(self, q, v):
        if 'query IntentComments' in q:
            nodes = [copy.deepcopy(c) for c in self.comments.values() if c['issue']['id'] == v['id']]
            if self.omit_listing:
                nodes = []
            mode = self.comment_paging
            if mode == 'repeat':
                self.comment_fetches = 1 if v['after'] is None else self.comment_fetches + 1
                if self.comment_fetches > 2:
                    raise OSError('continued past a repeated cursor')
                return {'issue': {'comments': connection([], True, 'same')}}
            if mode == 'unfinished':
                return {'issue': {'comments': connection(nodes, True, None)}}
            if mode == 'invalid':
                return {'issue': {'comments': connection(nodes, 1, 'next')}}
            if mode == 'duplicate':
                nodes += copy.deepcopy(nodes)
            if mode in ('pages', 'error'):
                if v['after'] and mode == 'error':
                    raise OSError('page failed')
                conn = connection(nodes[1:] if v['after'] else nodes[:1], not v['after'], 'next')
            else:
                conn = connection(nodes)
            return {'issue': {'comments': conn}}
        if 'query IntentComment(' in q:
            return {'comment': None if self.hide_comments else copy.deepcopy(self.comments.get(v['id']))}
        if 'query DrainIssue' in q:
            issue = next((copy.deepcopy(i) for i in self.issues.values() if v['id'] in (i['id'], i['identifier'])), None)
            if issue and self.lag_state and issue['id'] in self.state_before:
                issue['state'] = self.state_before[issue['id']]
            return {'issue': issue}
        if 'query DrainStates' in q:
            return {'team': {'states': connection(self.states)}}
        return super().dispatch(q, v)


class DrainCase(AuditCase):
    def setUp(self):
        super().setUp()
        self.patch.stop()
        self.fake = DrainLinear()
        self.patch = mock.patch.object(API, 'transport', self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        home = mock.patch.object(Path, 'home', return_value=self.root)
        home.start()
        self.addCleanup(home.stop)
        self.fake.add('1')

    def intent(self, key='key', op='start', at='2026-10-05T10:00:00+0000',
               project='swarm', tracker='ARC-1', directory=None, **values):
        intent = {'project': project, 'unit': 'u', 'attempt_dir': '/runs/u/attempt1',
                  'key': key, 'verb': op, 'at': at, 'unit_state': 'RUNNING', 'why': 'testing',
                  'evidence': {'receipt': {'state': 'DONE'}} if op == 'close' else None}
        if tracker is not None:
            intent['tracker'] = tracker
        intent.update(values)
        intent = DC.normalize_intent(intent)
        directory = directory or self.state
        directory.mkdir(exist_ok=True)
        with (directory / DC.OUTBOX).open('a') as f:
            f.write(json.dumps(intent) + '\n')
        return intent

    def seed(self, intent, issue='1', **changes):
        cid = LS.comment_id('workspace', 'project', issue, intent['key'])
        comment = {'id': cid, 'body': LS.comment_body(intent), 'issue': {'id': issue}}
        comment.update(changes)
        self.fake.comments[cid] = comment
        return comment

    def legacy(self, intent, directory=None):
        DC.contract.record_receipt(directory or self.state, intent['key'], intent.get('tracker', 'ARC-1'))

    def drain(self, *directories, draft=False, dry=False):
        args = ['drain', '--draft' if draft else '--binding', str(self.draft if draft else self.binding)]
        for directory in directories or (self.state,):
            args.extend(['--state-dir', str(directory)])
        if dry:
            args.append('--dry-run')
        return self.cli(*args)

    def receipts(self, directory=None):
        return DC.contract.load_acknowledgments(directory or self.state)[0]

    def writes(self, name):
        return [v for q, v in self.fake.mutations if name in q]

    def draft_file(self, slug='sample'):
        self.draft.write_text(json.dumps({'project': {'linear_id': 'project', 'team': 'ARC', 'slug': slug},
                                        'issues': [{'unit': 'u', 'identifier': 'ARC-1'}]}))


class TestDrain(DrainCase):
    def test_failed_state_intent_withholds_sibling_receipts_until_resolved(self):
        self.intent('s1', 'start')
        self.intent('n1', 'note', at='2026-10-05T10:00:05+0000')
        real = LS.confirm_comment

        def start_fails(client, intent, *rest):
            if intent['key'] == 's1':
                raise RuntimeError('comment create failed')
            return real(client, intent, *rest)
        with mock.patch.object(LS, 'confirm_comment', start_fails):
            self.assertEqual(self.drain(), 3)
        self.assertIn('receipt withheld', self.stdout + self.stderr)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual({r['key'] for r in self.receipts()}, {'s1', 'n1'})
        self.assertEqual(self.fake.issues['1']['state']['type'], 'started')

    def test_operations_rerun_and_state_type_preservation(self):
        for op in ('start', 'close', 'reopen', 'note', 'block', 'open_pr'):
            for already in (False, True):
                with self.subTest(op=op, already=already):
                    directory = self.root / (op + str(already))
                    target = LS.STATE_TYPE.get(op)
                    initial = target if already and target else 'backlog'
                    self.fake.issues['1']['state'] = {'type': initial, 'name': 'custom'}
                    self.fake.comments.clear(); self.fake.mutations.clear()
                    self.intent(op + str(already), op, directory=directory)
                    self.assertEqual(self.drain(directory), 0, self.stdout + self.stderr)
                    self.assertEqual(self.fake.issues['1']['state']['type'], target or initial)
                    self.assertEqual(len(self.writes('DrainState')), int(bool(target) and not already))
                    self.assertEqual(len(self.writes('IntentCommentCreate')), 1)
                    self.assertEqual(len(self.receipts(directory)), 1)
                    receipt = self.receipts(directory)[0]
                    self.assertEqual(receipt['outcome'], 'confirmed_by_readback')
                    self.assertEqual(receipt['ref'], 'ARC-1')
                    if target and not already:
                        self.assertEqual(self.writes('DrainState')[0]['state'], target + '-first')
                    before = copy.deepcopy(self.fake.mutations)
                    receipt_bytes = (directory / DC.RECEIPTS).read_bytes()
                    with mock.patch.object(LS.subprocess, 'run', side_effect=AssertionError('receipted intent invoked a child')):
                        self.assertEqual(self.drain(directory), 0, self.stdout)
                    self.assertEqual(self.fake.mutations, before)
                    self.assertEqual((directory / DC.RECEIPTS).read_bytes(), receipt_bytes)

    def test_comment_identity_markers_and_rejected_create(self):
        intent = self.intent()
        self.fake.omit_listing = True
        for change in ('evidence', 'order', 'issue', 'id', 'duplicate_marker',
                       'missing_intent', 'missing_evidence', 'missing_order',
                       'reordered_trailer', 'trailing_prose'):
            with self.subTest(change=change):
                comment = self.seed(intent)
                if change == 'issue':
                    comment['issue']['id'] = 'other'
                elif change == 'id':
                    comment['id'] = comment['id'].upper()
                elif change == 'duplicate_marker':
                    comment['body'] += '\n`swarm-intent: key`'
                elif change.startswith('missing_'):
                    name = change[len('missing_'):]
                    comment['body'] = '\n'.join(
                        line for line in comment['body'].splitlines()
                        if not line.startswith('`swarm-' + name + ':'))
                elif change == 'reordered_trailer':
                    lines = comment['body'].splitlines()
                    lines[-3], lines[-2] = lines[-2], lines[-3]
                    comment['body'] = '\n'.join(lines)
                elif change == 'trailing_prose':
                    comment['body'] += '\nmore text after the markers'
                else:
                    old = intent['envelope']['evidence_digest'] if change == 'evidence' else intent['at']
                    new = 'a' * 64 if change == 'evidence' else '2026-10-06T10:00:00+0000'
                    comment['body'] = comment['body'].replace(old, new)
                if change not in ('evidence', 'order'):
                    self.assertIsNone(LS.genuine_comment(comment, 'workspace', 'project', '1'))
                self.assertEqual(self.drain(), 3)
                self.assertEqual(self.receipts(), [])
                self.assertEqual(self.fake.mutations, [])
        self.fake.comments.clear()
        self.fake.reject_after_create = True
        self.assertEqual(self.drain(), 0, self.stdout)
        self.assertEqual(len(self.fake.comments), 1)

    def test_marker_lines_in_why_drain_and_receipt(self):
        markers = ['`swarm-intent: prose`', '`swarm-evidence: %s`' % ('f' * 64),
                   '`swarm-order: 2099-01-01T00:00:00Z prose close`']
        for number, text in enumerate(markers + ['\n'.join(markers)]):
            with self.subTest(text=text):
                directory = self.root / ('why-' + str(number))
                intent = self.intent('why-' + str(number), 'note', directory=directory,
                                     why='Explanation:\n' + text)
                self.assertEqual(self.drain(directory), 0, self.stdout + self.stderr)
                comment = self.fake.comments[LS.comment_id('workspace', 'project', '1', intent['key'])]
                self.assertIn(text, comment['body'])
                self.assertEqual(comment['body'].splitlines()[-3:], [
                    '`swarm-intent: %s`' % intent['key'],
                    '`swarm-evidence: %s`' % intent['envelope']['evidence_digest'],
                    '`swarm-order: %s %s note`' % (intent['at'], intent['key'])])
                self.assertEqual([r['key'] for r in self.receipts(directory)], [intent['key']])
                before = copy.deepcopy(self.fake.mutations)
                self.assertEqual(self.drain(directory), 0, self.stdout)
                self.assertEqual(self.fake.mutations, before)
                self.assertEqual(len(self.receipts(directory)), 1)

    def test_marker_like_body_before_real_trailer_drains_and_receipts(self):
        intent = self.intent()
        comment = self.seed(intent)
        trailer = comment['body'].splitlines()[-3:]
        comment['body'] = '\n'.join([
            'Arbitrary edited prose with marker-like lines:',
            '`swarm-intent: other`', '`swarm-evidence: %s`' % ('a' * 64),
            '`swarm-order: 2099-01-01T00:00:00Z other close`', '',
            trailer[0], '', trailer[1], '', trailer[2], '', '   '])
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.writes('IntentCommentCreate'), [])
        self.assertEqual(self.fake.issues['1']['state']['type'], 'started')
        self.assertEqual([r['key'] for r in self.receipts()], [intent['key']])
        self.assertEqual(self.audit(), 0)
        self.assertEqual(self.checks['intent_order'], 'CLEAN')

    def test_undrainable_keys_stay_unacknowledged_without_posting(self):
        for number, key in enumerate(('back`tick', 'two words', 'line\nbreak', 'key\n',
                                      'carriage\rreturn', 'tab\tkey', 'slash/key',
                                      'unicode-\u00e9', 'null\x00key', 'bracket[key]', '', ' ')):
            with self.subTest(key=key):
                self.fake.comments.clear(); self.fake.mutations.clear()
                directory = self.root / ('bad-key-' + str(number))
                self.intent(key, 'note', directory=directory)
                for _ in range(2):
                    self.assertEqual(self.drain(directory), 3)
                    self.assertEqual(self.fake.mutations, [])
                    self.assertEqual(self.receipts(directory), [])
                    self.assertIn('unacknowledged', self.stdout)
                    self.assertIn('key not drainable', self.stdout)

    def test_drainable_keys_round_trip_and_receipt(self):
        for number, key in enumerate(('0123456789abcdef-start',
                                      'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-')):
            with self.subTest(key=key):
                directory = self.root / ('good-key-' + str(number))
                self.intent(key, 'note', directory=directory)
                self.assertEqual(self.drain(directory), 0, self.stdout + self.stderr)
                self.assertEqual([r['key'] for r in self.receipts(directory)], [key])
                before = copy.deepcopy(self.fake.mutations)
                self.assertEqual(self.drain(directory), 0, self.stdout)
                self.assertEqual(self.fake.mutations, before)

    def test_unknown_operations_stay_unacknowledged_without_posting(self):
        for number, op in enumerate(('unknown', 'START', 'open-pr', 'note\n', 'note`', '')):
            with self.subTest(op=op):
                directory = self.root / ('bad-operation-' + str(number))
                self.intent('key', op, directory=directory)
                self.assertEqual(self.drain(directory), 3)
                self.assertEqual(self.fake.mutations, [])
                self.assertEqual(self.receipts(directory), [])
                self.assertIn('unacknowledged', self.stdout)

    def test_namespace_and_copied_comment_ignored(self):
        intent = self.intent('same')
        self.fake.add('2')
        second = self.root / 'second'
        self.intent('same', tracker='ARC-2', directory=second)
        copied = dict(intent, key='zzz', verb='close', at='2099-01-01T00:00:00Z')
        c = self.seed(copied)
        self.fake.comments.clear()
        c['id'] = API.derived_id('random-copy')
        self.fake.comments[c['id']] = c
        self.assertEqual(self.drain(self.state, second), 0, self.stdout)
        self.assertEqual(len(self.fake.comments), 3)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'started')
        self.assertNotEqual(LS.comment_id('workspace', 'project', '1', 'same'),
                            LS.comment_id('workspace', 'project', '2', 'same'))
        self.assertNotEqual(LS.comment_id('workspace', 'project', '1', 'same'),
                            LS.comment_id('other', 'project', '1', 'same'))
        self.assertEqual(LS.comment_id('workspace', 'project', '1', 'same'),
                         API.derived_id('comment:workspace/project/1/same'))

    def test_comment_pages_complete_or_no_further_changes(self):
        a = self.intent('a', 'start'); self.seed(a)
        later = dict(a, key='b', verb='close', at='2026-10-05T11:00:00+0000')
        self.seed(later)
        self.fake.comment_paging = 'pages'
        self.assertEqual(self.drain(), 0, self.stdout)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'completed')
        self.assertEqual(self.receipts(), [])  # a is superseded by remote b
        for mode in ('repeat', 'unfinished', 'error', 'invalid', 'duplicate'):
            with self.subTest(mode=mode):
                self.fake.comment_paging = mode
                self.fake.mutations.clear()
                self.fake.issues['1']['state']['type'] = 'unstarted'
                self.assertEqual(self.drain(), 3)
                if mode == 'invalid':
                    self.assertIn('invalid pageInfo', self.stdout)
                if mode == 'repeat':
                    self.assertNotIn('continued past', self.stdout)
                    self.assertIn('repeated cursor', self.stdout)
                self.assertEqual(self.fake.mutations, [])
                self.assertEqual(self.receipts(), [])
                self.assertEqual(self.audit(), 3)
                self.assertEqual(self.checks['intent_order'], 'UNKNOWN')

    def test_binding_nameless_and_draft_exact_routing(self):
        self.intent()
        self.assertEqual(self.drain(), 0)
        for project, slug, tracker in [('swarm', 'swarm', 'ARC-1'),
                                      ('swarm', 'unnamed-swarm-project', 'ARC-1'),
                                      ('SAMPLE', 'sample', 'ARC-1'),
                                      ('sample ', 'sample', 'ARC-1'),
                                      ('sample', 'sample', 'arc-1'),
                                      ('sample', 'sample', 'ARC-1 ')]:
            with self.subTest(project=project, slug=slug, tracker=tracker):
                directory = self.root / ('case' + str(len(list(self.root.iterdir()))))
                self.intent(project=project, tracker=tracker, directory=directory)
                self.draft_file(slug)
                self.fake.mutations.clear()
                self.assertEqual(self.drain(directory, draft=True), 3)
                self.assertEqual(self.fake.mutations, [])
        directory = self.root / 'valid-draft'
        self.intent('draft', project='sample', tracker=None, directory=directory)
        self.assertEqual(self.drain(directory, draft=True), 0, self.stdout)
        directory = self.root / 'no-tracker'
        self.intent(tracker=None, directory=directory)
        self.assertEqual(self.drain(directory), 3)
        self.assertIn('no tracker issue', self.stdout)

    def test_membership_and_identity_exact_before_mutation(self):
        self.intent(project='sample')
        self.draft_file()
        for draft in (False, True):
            for field in ('project', 'team'):
                for value in (None, {'id': field.upper()}, {'id': field + ' '}):
                    with self.subTest(draft=draft, field=field, value=value):
                        original = self.fake.issues['1'][field]
                        self.fake.issues['1'][field] = value
                        self.assertEqual(self.drain(draft=draft), 3)
                        self.assertIn('issue outside the bound project', self.stdout)
                        self.assertEqual(self.fake.mutations, [])
                        self.fake.issues['1'][field] = original
        original = json.loads(self.binding.read_text())
        for field in ('workspace', 'project', 'team'):
            value = copy.deepcopy(original); value[field]['id'] = field.upper()
            self.binding.write_text(json.dumps(value))
            self.assertEqual(self.drain(), 2)
            self.assertEqual(self.fake.mutations, [])
        self.binding.write_text(json.dumps(original))

    def test_lagging_readback_retry_no_duplicate(self):
        self.intent()
        self.fake.hide_comments = True
        self.fake.omit_listing = True
        self.assertEqual(self.drain(), 3)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.writes('DrainState'), [])
        self.fake.hide_comments = False
        self.fake.lag_state = True
        self.assertEqual(self.drain(), 3)
        self.assertEqual(self.receipts(), [])
        self.fake.lag_state = False
        self.assertEqual(self.drain(), 0, self.stdout)
        self.assertEqual(len(self.writes('IntentCommentCreate')), 1)
        self.assertEqual(len(self.writes('DrainState')), 1)
        self.assertEqual(len(self.receipts()), 1)

    def test_order_across_directories_instants_keys_and_lagging_listing(self):
        self.intent('a', 'close', at='2026-10-05T12:00:00+0200')
        bdir = self.root / 'b'
        self.intent('b', 'reopen', at='2026-10-05T10:00:00+0000', directory=bdir)
        cdir = self.root / 'c'
        self.intent('c', 'start', at='2026-10-05T09:00:01-0100', directory=cdir)
        self.fake.omit_listing = True
        self.assertEqual(self.drain(cdir, self.state, bdir), 0, self.stdout)
        self.assertEqual([v['input']['body'].splitlines()[-3] for v in self.writes('IntentCommentCreate')],
                         ['`swarm-intent: a`', '`swarm-intent: b`', '`swarm-intent: c`'])
        self.assertEqual(len(self.writes('DrainState')), 1)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'started')
        self.assertEqual(self.receipts(), []); self.assertEqual(self.receipts(bdir), [])
        self.assertEqual(len(self.receipts(cdir)), 1)
        self.assertIn('superseded by c', self.stdout)
        # Equal instants choose the key even when input directory order differs.
        self.fake.mutations.clear(); self.fake.comments.clear()
        self.assertEqual(self.drain(bdir, self.state), 0, self.stdout)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'unstarted')
        self.assertEqual(len(self.receipts(bdir)), 1)
        self.assertEqual([v['input']['body'].splitlines()[-3] for v in self.writes('IntentCommentCreate')],
                         ['`swarm-intent: a`', '`swarm-intent: b`'])

    def test_invalid_at_and_contract_leave_other_intents_running(self):
        for n, at in enumerate(('bad', '2026-10-05T10:00:00', None, 123)):
            self.intent('bad' + str(n), at=at)
        self.intent('good', 'note')
        self.assertEqual(self.drain(), 3)
        self.assertEqual(len(self.fake.comments), 1)
        self.assertEqual([r['key'] for r in self.receipts()], ['good'])
        other = self.root / 'malformed'
        self.intent('invalid', directory=other, envelope={})
        with (other / DC.OUTBOX).open('a') as f:
            f.write('broken-json\nnull\n')
        last = self.root / 'last'
        self.intent('last', 'note', directory=last)
        self.assertEqual(self.drain(other, last), 3)
        self.assertEqual([r['key'] for r in self.receipts(last)], ['last'])

    def test_close_requires_receipt_merge_shape_and_unit(self):
        merge = {'unit': 'u', 'repo': 'repo', 'pr': 1, 'target': 'main', 'head': 'h',
                 'merged_as': 'm', 'method': 'squash'}
        cases = [(None, None), ({}, None), ({'receipt': {}}, None),
                 ({'receipt': {'unit': 'u'}}, 'merged_pr'),
                 ({'receipt': dict(merge, unit='U')}, 'merged_pr'),
                 ({'receipt': dict(merge, method='bad')}, 'merged_pr')]
        for n, (evidence, closing) in enumerate(cases):
            directory = self.root / str(n)
            self.intent(str(n), 'close', evidence=evidence, closing_evidence=closing, directory=directory)
            self.assertEqual(self.drain(directory), 3)
            self.assertEqual(self.fake.mutations, [])
        self.intent('valid', 'close', evidence={'receipt': merge}, closing_evidence='merged_pr')
        self.assertEqual(self.drain(), 0, self.stdout)

    def test_receipted_history_repair_audit_and_failure(self):
        older = self.intent('old', 'start')
        self.seed(older); self.legacy(older)
        later = dict(older, key='new', verb='close', at='2026-10-05T11:00:00Z')
        self.seed(later)
        self.fake.issues['1']['state']['type'] = 'started'
        before = (self.state / DC.RECEIPTS).read_bytes()
        self.assertEqual(self.audit(), 1)
        self.assertEqual(self.checks['intent_order'], 'DRIFT')
        self.assertEqual(self.drain(), 0, self.stdout)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'completed')
        self.assertEqual(self.audit(), 0)
        self.assertEqual(self.checks['intent_order'], 'CLEAN')
        self.assertEqual(self.writes('IntentCommentCreate'), [])
        self.assertEqual((self.state / DC.RECEIPTS).read_bytes(), before)
        self.fake.issues['1']['state']['type'] = 'started'
        self.fake.mutation_failure = 'DrainState'
        self.assertEqual(self.drain(), 3)
        self.assertIn('reconciliation', self.stdout)
        self.fake.mutation_failure = None
        self.fake.comments.pop(LS.comment_id('workspace', 'project', '1', 'old'))
        self.assertEqual(self.drain(), 0)
        self.assertIn('ARC-1: uncovered history', self.stdout)
        self.assertEqual(self.writes('IntentCommentCreate'), [])

    def test_lock_same_project_different_binding_no_writes(self):
        self.intent()
        lock = LS.drain_lock_path('workspace', 'project')
        lock.parent.mkdir(parents=True)
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = self.root / 'another-binding.json'
            second.write_bytes(self.binding.read_bytes()); self.binding = second
            self.assertEqual(self.drain(), 2)
            self.assertIn('project lock', self.stderr)
            self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.drain(), 0)

    def test_one_remote_failure_does_not_stop_other_issue(self):
        bad = self.intent('bad', tracker='ARC-missing')
        self.intent('good', 'note')
        self.assertEqual(self.drain(), 3)
        self.assertIn(bad['key'], self.stdout)
        self.assertEqual([r['key'] for r in self.receipts()], ['good'])

    def test_dry_run_reads_without_mutations_or_receipts(self):
        intent = self.intent()
        self.assertEqual(self.drain(dry=True), 3)
        self.assertIn('would post comment', self.stdout)
        self.assertIn('would reconcile to started', self.stdout)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.receipts(), [])
        self.seed(intent)
        self.assertEqual(self.drain(dry=True), 3)
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(self.receipts(), [])

    def test_key_containment_real_receipt_child_and_errors(self):
        self.intent()
        actual_run = subprocess.run
        def child(command, **kwargs):
            self.assertNotIn(KEY, repr(command))
            self.assertNotIn(API.KEY_ENV, kwargs['env'])
            for flag in ('--intent', '--observation'):
                self.assertNotIn(KEY, Path(command[command.index(flag) + 1]).read_text())
            probe = actual_run([sys.executable, '-c',
                                'import os; assert "LINEAR_API_KEY" not in os.environ'],
                               env=kwargs['env'], capture_output=True, text=True)
            self.assertEqual(probe.returncode, 0)
            return actual_run(command, **kwargs)
        with mock.patch.object(LS.subprocess, 'run', side_effect=child) as run:
            self.assertEqual(self.drain(), 0, self.stdout)
            self.assertEqual(run.call_count, 1)
        for path in self.state.iterdir():
            self.assertNotIn(KEY, path.read_text())
        self.fake.fail = 'DrainIssue'
        self.assertEqual(self.drain(), 3)
        self.assertNotIn(KEY, self.stdout + self.stderr)
        with mock.patch.object(API, 'load_key', return_value=None):
            self.assertEqual(self.drain(), 2)
        # Only the established seam owns network imports.
        for path in (ROOT / 'skills/hanig-swarm/scripts/swarm.py', PROJECT / 'tickets.py', PROJECT / 'drain_contract.py', PROJECT / 'linear_sync.py'):
            tree = ast.parse(path.read_text())
            names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            forbidden = {'urllib', 'http', 'requests', 'socket', 'linear_api', 'linear_sync'}
            if path.name == 'linear_sync.py':
                forbidden.remove('linear_api')
            self.assertFalse({str(n).split('.')[0] for n in names} & forbidden, path)

    def test_receipt_child_failure_does_not_abort_next(self):
        self.intent('a', 'note'); self.intent('b', 'note')
        actual = subprocess.run
        count = []
        def child(command, **kwargs):
            count.append(command)
            if len(count) == 1:
                return subprocess.CompletedProcess(command, 2, '', 'failure ' + KEY)
            return actual(command, **kwargs)
        with mock.patch.object(LS.subprocess, 'run', side_effect=child):
            self.assertEqual(self.drain(), 3)
        self.assertEqual([r['key'] for r in self.receipts()], ['b'])

    def test_malformed_sibling_contract_limitation_is_reported(self):
        self.intent('good', 'note')
        with (self.state / DC.OUTBOX).open('a') as f:
            f.write('null\n')
        self.assertEqual(self.drain(), 3)
        self.assertEqual(len(self.fake.comments), 1)
        self.assertEqual(self.receipts(), [])
        self.assertIn('receipt refused', self.stdout)


class TestDrainGuards(DrainCase):
    def test_config_and_draft_errors(self):
        self.intent(project='sample')
        original = self.binding.read_bytes()
        config = json.loads(original)
        for value in (None, [], dict(config, schema_version=True), dict(config, schema_version=2),
                      {'schema_version': 1, 'workspace': {'id': ''}}):
            self.binding.write_text(json.dumps(value))
            self.assertEqual(self.drain(), 2, self.stdout + self.stderr)
            self.assertEqual(self.fake.mutations, [])
        self.binding.write_bytes(original)
        self.draft_file()
        draft = json.loads(self.draft.read_text())
        draft['issues'] *= 2
        self.draft.write_text(json.dumps(draft))
        self.assertEqual(self.drain(draft=True), 2)
        self.assertEqual(self.fake.mutations, [])
        self.draft_file('')
        self.assertEqual(self.drain(draft=True), 2)

    def test_changed_binding_and_invalid_lock_component(self):
        self.intent()
        original = Path.read_bytes
        count = []
        def changed(path):
            data = original(path)
            if path == self.binding:
                count.append(True)
                if len(count) > 1:
                    return data + b' '
            return data
        with mock.patch.object(Path, 'read_bytes', changed):
            self.assertEqual(self.drain(), 2)
        self.assertEqual(self.fake.mutations, [])
        self.fake.org['id'] = 'workspace/other'
        config = json.loads(self.binding.read_text())
        config['workspace']['id'] = 'workspace/other'
        self.binding.write_text(json.dumps(config))
        self.assertEqual(self.drain(), 2)
        self.assertEqual(self.fake.mutations, [])
        self.assertFalse((self.root / '.local/state/escape-project.lock').exists())

    def test_receipt_corruption_and_conflict_withhold_mutations(self):
        intent = self.intent()
        self.legacy(intent)
        original = (self.state / DC.RECEIPTS).read_text()
        for suffix in ('bad-json\n', '{', json.dumps(dict(self.receipts()[0], ref='ARC-2')) + '\n'):
            (self.state / DC.RECEIPTS).write_text(original + suffix)
            self.assertEqual(self.drain(), 3)
            self.assertEqual(self.fake.mutations, [])
        (self.state / DC.RECEIPTS).write_text(original)

    def test_envelope_evidence_guard_before_comments(self):
        intent = self.intent()
        intent['evidence'] = {'tampered': True}
        (self.state / DC.OUTBOX).write_text(json.dumps(intent) + '\n')
        self.assertEqual(self.drain(), 3)
        self.assertEqual(self.fake.mutations, [])

    def test_invalid_remote_markers_ignored_in_audit_and_drain(self):
        good = self.intent('good')
        self.seed(good)
        bad = dict(good, key='bad', verb='close', at='2099-01-01T00:00:00Z')
        for mode in ('digest', 'order-key', 'operation', 'at', 'marker', 'duplicate', 'wrong-issue', 'wrong-id'):
            with self.subTest(mode=mode):
                self.fake.comments.clear(); self.seed(good)
                comment = self.seed(bad)
                if mode == 'digest':
                    comment['body'] = comment['body'].replace(bad['envelope']['evidence_digest'], 'invalid')
                elif mode == 'order-key':
                    comment['body'] = comment['body'].replace(' bad close`', ' different close`')
                elif mode == 'operation':
                    comment['body'] = comment['body'].replace(' bad close`', ' bad unknown`')
                elif mode == 'at':
                    comment['body'] = comment['body'].replace(bad['at'], 'not-an-instant')
                elif mode == 'marker':
                    comment['body'] = comment['body'].replace('`swarm-intent: bad`', 'swarm-intent: bad')
                elif mode == 'duplicate':
                    comment['body'] += '\n`swarm-order: 2099-01-01T00:00:00Z bad close`'
                elif mode == 'wrong-issue':
                    comment['issue']['id'] = '2'
                else:
                    comment['id'] = comment['id'].upper()
                parsed = LS.issue_comments(LS.Reader(API.Client(KEY)), 'workspace', 'project', '1')
                self.assertEqual([c['key'] for c in parsed], ['good'])
                self.assertEqual(self.drain(), 0, self.stdout)
                self.assertEqual(self.fake.issues['1']['state']['type'], 'started')
                self.assertEqual(self.audit(), 0)
                self.assertEqual(self.checks['intent_order'], 'CLEAN')

    def test_membership_and_identity_rechecked_and_state_missing(self):
        self.intent()
        actual = self.fake.dispatch
        for when, field in ((2, 'project'), (2, 'id'), (3, 'team'), (3, 'id')):
            with self.subTest(when=when, field=field):
                self.fake.comments.clear(); self.fake.mutations.clear()
                self.fake.issues['1']['state']['type'] = 'unstarted'
                count = []
                def move(q, v):
                    result = actual(q, v)
                    if 'query DrainIssue' in q:
                        count.append(True)
                        if len(count) == when:
                            result['issue'][field] = 'other' if field == 'id' else {'id': 'other'}
                    return result
                with mock.patch.object(self.fake, 'dispatch', side_effect=move):
                    self.assertEqual(self.drain(), 3)
                self.assertEqual(self.receipts(), [])
                self.assertEqual(len(self.writes('DrainState')), 0 if when == 2 else 1)
        self.fake.states = []
        self.fake.issues['1']['state']['type'] = 'unstarted'
        self.fake.mutations.clear()
        self.assertEqual(self.drain(), 3)
        self.assertIn('no started state', self.stdout)
        self.assertEqual(self.writes('DrainState'), [])

    def test_all_receipted_comment_read_failure_is_unknown(self):
        intent = self.intent(); self.seed(intent); self.legacy(intent)
        self.fake.fail = 'query IntentComment('
        self.assertEqual(self.drain(), 3)
        self.assertIn('comment read UNKNOWN', self.stdout)
        self.assertNotIn('uncovered history', self.stdout)
        self.assertEqual(self.writes('IntentCommentCreate'), [])

    def test_incomplete_listing_with_note_withholds_receipt(self):
        self.intent(op='note')
        self.fake.comment_paging = 'unfinished'
        self.assertEqual(self.drain(), 3)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.writes('DrainState'), [])

    def test_listing_disagrees_with_confirmed_comment(self):
        self.intent()
        actual = self.fake.dispatch
        def edited(q, v):
            data = actual(q, v)
            if 'query IntentComments' in q:
                for comment in data['issue']['comments']['nodes']:
                    comment['body'] = comment['body'].replace('2026-10-05', '2026-10-06')
            return data
        with mock.patch.object(self.fake, 'dispatch', side_effect=edited):
            self.assertEqual(self.drain(), 3)
        self.assertEqual(self.writes('DrainState'), [])
        self.assertEqual(self.receipts(), [])
        self.assertIn('listed comment disagrees', self.stdout)

    def test_dry_run_does_not_excuse_conflicting_comment(self):
        intent = self.intent()
        comment = self.seed(intent)
        comment['body'] = comment['body'].replace('2026-10-05', '2026-10-06')
        self.assertEqual(self.drain(dry=True), 3)
        self.assertIn('markers disagree', self.stdout)
        self.assertEqual(self.fake.mutations, [])


class TestMergeDrain(DrainCase):
    def test_exit_neutral_unavailable_and_sequence(self):
        self.plan.write_text('{}')
        binding = self.root / '.hanig/linear-binding.json'
        binding.parent.mkdir(); binding.write_bytes(self.binding.read_bytes())
        arguments = [str(self.plan), '--state-dir', str(self.state), '--unit', 'u', '--pr', '1', '--approver', 'owner']
        for mode in ('success', 'partial', 'no-key', 'no-binding', 'no-sibling', 'failed', 'timeout'):
            with self.subTest(mode=mode):
                events = []
                def child(command, **kwargs):
                    events.append(command[2])
                    self.assertEqual(Path(command[1]).name, 'linear_sync.py')
                    self.assertEqual(kwargs['env'][API.KEY_ENV], KEY)
                    self.assertNotIn(KEY, repr(command))
                    if mode == 'timeout':
                        raise subprocess.TimeoutExpired(command, 1)
                    return subprocess.CompletedProcess(command, 2 if mode == 'failed' else 3 if mode == 'partial' else 0,
                                                       'summary ' + KEY, 'error ' + KEY)
                if mode == 'no-binding':
                    binding.unlink()
                else:
                    binding.write_bytes(self.binding.read_bytes())
                out = io.StringIO()
                sibling = MU.skill_paths.sibling_skill_root
                def resolve(*args):
                    if mode == 'no-sibling':
                        raise ValueError('missing sibling')
                    return sibling(*args)
                with mock.patch.object(MU, 'authority'), mock.patch.object(MU.S, 'acquire_lease', return_value=(True, None)), mock.patch.object(MU.S, 'release_lease'), mock.patch.object(MU, 'reconcile', return_value=['advance']), mock.patch.object(MU, 'run', side_effect=lambda c: (events.append('advance') or subprocess.CompletedProcess(c, 0, '', ''))), mock.patch.object(MU, 'print_pending_close', side_effect=lambda *a: events.append('pending')), mock.patch.object(MU, 'print_tracker_audit', side_effect=lambda *a: events.append('audit')), mock.patch.object(API, 'load_key', return_value=None if mode == 'no-key' else KEY), mock.patch.object(MU.subprocess, 'run', side_effect=child), mock.patch.object(MU.skill_paths, 'sibling_skill_root', side_effect=resolve), contextlib.redirect_stdout(out):
                    self.assertEqual(MU.main(arguments), 0)
                self.assertEqual(events[:2], ['advance', 'pending'])
                self.assertEqual(events[-1], 'audit')
                self.assertNotIn(KEY, out.getvalue())
                if mode in ('success', 'partial'):
                    self.assertIn('Tracker drain: summary', out.getvalue())
                    self.assertEqual(events, ['advance', 'pending', 'drain', 'audit'])
                else:
                    self.assertIn('Tracker drain: UNAVAILABLE', out.getvalue())
                    if mode in ('no-key', 'no-binding', 'no-sibling'):
                        self.assertEqual(events, ['advance', 'pending', 'audit'])
                    if mode == 'no-key':
                        self.assertIn('UNAVAILABLE (no key)', out.getvalue())
                    if mode == 'no-binding':
                        self.assertIn('UNAVAILABLE (no binding)', out.getvalue())


if __name__ == '__main__':
    unittest.main()
