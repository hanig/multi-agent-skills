"""ARC-1786: approver dispositions through CLI, outbox, fake drain and audit."""
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_linear_drain import DrainCase

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / 'skills/hanig-swarm/scripts'
PROJECT = ROOT / 'skills/hanig-project/scripts'
sys.path.insert(0, str(SWARM))
sys.path.insert(0, str(PROJECT))
import swarm as S
import swarm_state as SS
import swarm_outbox as SO
import report as R


class StopFixture:
    def setup_stop(self):
        self.plan = self.root / 'plan.json'
        self.state = self.root / 'state'
        self.state.mkdir(exist_ok=True)
        self.runs = self.root / 'runs'
        self.attempt = self.runs / 'old' / 'attempt1'
        self.attempt.mkdir(parents=True, exist_ok=True)
        for path in (self.state / SO.OUTBOX, self.state / SS.STATE_EPOCH_FILE,
                     self.attempt / "receipt.json"):
            path.unlink(missing_ok=True)
        (self.attempt / 'preserved.patch').write_bytes(b'preserved implementation\x00\xff')
        self.receipt = {'unit': 'successor', 'repo': 'owner/repo',
                        'pr': 'https://github.com/owner/repo/pull/42', 'target': 'main',
                        'head': 'a' * 40, 'merged_as': 'b' * 40, 'method': 'merge',
                        'merged': True, 'attested': True}
        units = [{'id': uid, 'kind': 'slurm', 'command': 'true', 'runtime': 'none',
                  'outputs': ['result'], 'needs': [], 'tracker': 'ARC-' + str(n)}
                 for n, uid in enumerate(('old', 'successor'), 1)]
        self.plan.write_text(json.dumps({'name': 'stop-test', 'units': units}))
        state = {'schema_version': 1, 'halted': None, 'units': {
            'old': {'state': 'FAILED', 'attempt_dir': str(self.attempt),
                    'attempts': [str(self.attempt)], 'gpu_hours': 0,
                    'attempt_workspaces': {'attempt1': {'cleanup_pending': True}},
                    'preserved_branch': 'recovery/old'},
            'successor': {'state': 'DONE', 'attempt_dir': str(self.runs / 'successor' / 'attempt2'),
                          'attempts': [], 'gpu_hours': 0, 'job_id': 'finished',
                          'attempt_produced_heads': {'attempt2': self.receipt['head']},
                          'merge_receipt': self.receipt, 'merge_pr': self.receipt['pr'],
                          'merged_as': self.receipt['merged_as']}}}
        self.save(state)

    def load(self):
        return json.loads((self.state / SS.STATE_FILE).read_text())

    def save(self, state):
        (self.state / SS.STATE_FILE).write_text(json.dumps(state))

    def swarm(self, command, *args):
        return subprocess.run([sys.executable, str(SWARM / 'swarm.py'), command,
                               str(self.plan), '--state-dir', str(self.state), *args],
                              cwd=str(self.root), capture_output=True, text=True, timeout=60)

    def stop(self, command='abandon', unit='old', approver='owner', reason='No longer needed', by='successor'):
        args = ['--unit', unit, '--approver', approver, '--reason', reason]
        if command == 'supersede':
            args += ['--by', by]
        return self.swarm(command, *args)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_refused(self, result, reason):
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn(reason, result.stderr)

    def intents(self):
        return SO.read_outbox(self.state)


class TestOperatorTerminal(StopFixture, unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.setup_stop()

    def test_cli_records_decision_and_preserves_evidence(self):
        for command, expected in (('abandon', 'ABANDONED'), ('supersede', 'SUPERSEDED')):
            with self.subTest(command=command):
                self.setup_stop()
                before = self.load()['units']['old']
                self.assert_ok(self.stop(command))
                us = self.load()['units']['old']
                decision = us['terminal_decision']
                self.assertEqual(us['state'], expected)
                self.assertEqual(decision['state'], expected)
                self.assertEqual(decision['prior_state'], 'FAILED')
                self.assertEqual(decision['approver'], 'owner')
                self.assertEqual(decision['reason'], 'No longer needed')
                self.assertEqual(dt.datetime.fromisoformat(decision['at']).utcoffset(), dt.timedelta(0))
                for key in ('attempt_dir', 'attempts', 'attempt_workspaces', 'preserved_branch'):
                    self.assertEqual(us[key], before[key])
                self.assertEqual((self.attempt / 'preserved.patch').read_bytes(), b'preserved implementation\x00\xff')
                self.assertGreater(json.loads((self.state / SS.STATE_EPOCH_FILE).read_text())['epoch'], 0)
                self.assertEqual(decision['successor'], 'successor' if command == 'supersede' else None)
                if command == 'supersede':
                    self.assertEqual(decision['merge_receipt'], self.receipt)
                status = self.swarm('status', '--json')
                self.assert_ok(status)
                row = next(r for r in json.loads(status.stdout)['units'] if r['id'] == 'old')
                self.assertEqual(row['state'], expected)
                self.assertEqual(row['reason'], decision['reason'])
                self.assertEqual(row['successor'], decision['successor'])
                text = self.swarm('status').stdout
                self.assertIn(expected, text)
                self.assertIn(decision['reason'], text)
                if command == 'supersede':
                    self.assertIn('successor: successor', text)

    def test_cli_refusals_are_usage_errors_and_leave_state_unchanged(self):
        original = (self.state / SS.STATE_FILE).read_bytes()
        for command in ('abandon', 'supersede'):
            cases = [({'unit': 'absent'}, 'unknown unit'),
                     ({'approver': ''}, 'approver'), ({'approver': '  \t'}, 'approver'),
                     ({'reason': ''}, 'reason'), ({'reason': '  \t'}, 'reason'),
                     ({'unit': 'successor'}, 'DONE')]
            if command == 'supersede':
                cases += [({'by': 'elsewhere'}, 'unknown successor'),
                          ({'by': 'old'}, 'not DONE')]
            for values, reason in cases:
                with self.subTest(command=command, values=values):
                    self.assert_refused(self.stop(command, **values), reason)
                    self.assertEqual((self.state / SS.STATE_FILE).read_bytes(), original)
        self.assertFalse((self.state / SO.OUTBOX).exists())
        self.assertFalse((self.state / SS.STATE_EPOCH_FILE).exists())

    def test_supersede_requires_successor_done_and_bound_merge_receipt(self):
        original = self.load()
        variants = [({'state': 'READY_FOR_PR'}, 'not DONE'),
                    ({'merge_receipt': None}, 'bound merge receipt'),
                    ({'attempt_produced_heads': {}}, 'bound merge receipt'),
                    ({'merged_as': 'c' * 40}, 'bound merge receipt'),
                    ({'merge_pr': 'another'}, 'bound merge receipt')]
        for field, value in (('head', 'c' * 40), ('unit', 'old'), ('merged', False),
                             ('attested', False), ('method', 'unknown')):
            variants.append(({'merge_receipt': dict(self.receipt, **{field: value})}, 'bound merge receipt'))
        for change, reason in variants:
            with self.subTest(change=change):
                state = copy.deepcopy(original)
                state['units']['successor'].update(change)
                self.save(state)
                self.assert_refused(self.stop('supersede'), reason)
                self.assertEqual(self.load(), state)
        self.assertFalse((self.state / SO.OUTBOX).exists())

    def test_idempotency_and_conflicting_second_calls(self):
        for command in ('abandon', 'supersede'):
            with self.subTest(command=command):
                self.setup_stop()
                self.assert_ok(self.stop(command))
                paths = [self.state / name for name in (SS.STATE_FILE, SS.STATE_EPOCH_FILE, SO.OUTBOX)]
                before = {p: p.read_bytes() for p in paths}
                self.assert_ok(self.stop(command))
                self.assertEqual({p: p.read_bytes() for p in paths}, before)
                for change in ({'reason': 'changed'}, {'approver': 'other'}):
                    self.assert_refused(self.stop(command, **change), 'conflicting')
                other = 'supersede' if command == 'abandon' else 'abandon'
                self.assert_refused(self.stop(other), 'conflicting')
                if command == 'supersede':
                    self.assert_refused(self.stop(command, by='old'), 'conflicting')
                self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_dependent_held_and_no_redispatch_or_cleanup_through_run_and_advance(self):
        for command in ('abandon', 'supersede'):
            for retained_attempt in (True, False):
                with self.subTest(command=command, retained_attempt=retained_attempt):
                    self.setup_stop()
                    state = self.load()
                    if not retained_attempt:
                        state['units']['old']['attempt_dir'] = None
                        self.save(state)
                    plan = json.loads(self.plan.read_text())
                    plan['units'].append({'id': 'dependent', 'kind': 'slurm', 'command': 'true',
                                          'runtime': 'none', 'needs': ['old'], 'outputs': ['result']})
                    self.plan.write_text(json.dumps(plan))
                    self.assert_ok(self.stop(command))
                    saved = self.load()['units']['old']
                    for entry in ('run', 'advance', 'advance'):
                        result = self.swarm(entry, '--root', str(self.runs))
                        self.assert_ok(result)
                        current = self.load()
                        self.assertEqual(current['units']['old'], saved)
                        self.assertEqual(current['units']['dependent']['state'], 'HELD')
                        self.assertFalse(current['units']['dependent']['attempts'])
                        self.assertIn('dispatched 0 unit(s)', result.stdout)
                        self.assertEqual((self.attempt / 'preserved.patch').read_bytes(), b'preserved implementation\x00\xff')
                    status = json.loads(self.swarm('status', '--json').stdout)
                    self.assertEqual(next(r for r in status['units'] if r['id'] == 'dependent')['held_by'], ['old'])

    def test_outbox_uses_successor_evidence_and_repairs_missing_append(self):
        for command, verb in (('abandon', 'cancel'), ('supersede', 'close')):
            with self.subTest(command=command):
                self.setup_stop()
                (self.state / SO.OUTBOX).unlink(missing_ok=True)
                self.assert_ok(self.stop(command))
                intents = self.intents()
                self.assertEqual(len(intents), 1)
                intent = intents[0]
                self.assertEqual(intent['verb'], verb)
                self.assertEqual(intent['why'], 'No longer needed')
                self.assertEqual(SO.validate_intent(intent), [])
                if command == 'supersede':
                    self.assertEqual(intent['closing_evidence'], 'merged_pr')
                    self.assertEqual(intent['evidence'], {'successor': 'successor', 'receipt': self.receipt})
                (self.state / SO.OUTBOX).unlink()
                self.assert_ok(self.swarm('advance', '--root', str(self.runs)))
                repaired = next(i for i in self.intents() if i['unit'] == 'old')
                self.assertEqual(repaired, intent)

    def test_live_stop_preserves_claim_occupancy_and_deadline_cannot_rewrite_it(self):
        plan = json.loads(self.plan.read_text())
        plan['units'][0]['deadline_s'] = 1
        self.plan.write_text(json.dumps(plan))
        state = self.load()
        state['units']['old'].update(state='RUNNING', allocated_at=1, reason='deadline_exceeded',
                                     deadline_breach_active=True)
        self.save(state)
        self.assert_ok(self.stop())
        stopped = self.load()['units']['old']
        self.assertTrue(SS._occupies_live_resources(plan['units'][0], stopped))
        self.assert_ok(self.swarm('advance', '--root', str(self.runs)))
        self.assertEqual(self.load()['units']['old'], stopped)

    def test_report_dispositions_override_preserved_attempt_verdict(self):
        for command, expected in (('abandon', 'ABANDONED'), ('supersede', 'SUPERSEDED')):
            with self.subTest(command=command):
                self.setup_stop()
                self.assert_ok(self.stop(command))
                # The retained receipt judged the earlier attempt; its missing
                # declared outputs do not invalidate an approver disposition.
                receipt = {'state': 'FAILED', 'outputs': {'other': {}}, 'kind': 'slurm'}
                data = {'plan': {'units': [json.loads(self.plan.read_text())['units'][0]]},
                        'state': {'units': {'old': self.load()['units']['old']}},
                        'receipts': {'old': receipt}, 'evidence': {}}
                rows = R.unit_rows(data)
                self.assertEqual(rows[0]['state'], expected)
                self.assertIsNone(rows[0]['state_conflict'])
                self.assertFalse(rows[0]['missing_outputs'])
                self.assertFalse(rows[0]['undeclared_outputs'])
                self.assertEqual(R.verdict(rows)[0], 'STOPPED')
                (self.attempt / 'receipt.json').write_text(json.dumps(receipt))
                result = subprocess.run([sys.executable, str(PROJECT / 'report.py'), str(self.root),
                                         '--state-dir', str(self.state), '--root', str(self.runs)],
                                        capture_output=True, text=True, timeout=60)
                self.assert_ok(result)
                rendered = (self.root / 'report.html').read_text()
                self.assertIn(expected, rendered)
                self.assertIn('No longer needed', rendered)
                if command == 'supersede':
                    self.assertIn('successor: successor', rendered)


class TestStoppedDrain(StopFixture, DrainCase):
    def setUp(self):
        super().setUp()
        self.setup_stop()
        self.fake.states += [{'id': 'canceled-second', 'type': 'canceled', 'position': 2},
                             {'id': 'canceled-first', 'type': 'canceled', 'position': 1}]

    def history(self):
        self.intent('old-start', 'start', unit='old')
        self.intent('old-pr', 'open_pr', unit='old', at='2026-10-05T11:00:00+0000')
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'started')

    def test_two_drains_then_audit_have_no_intent_order_drift(self):
        self.history()
        self.assert_ok(self.stop())
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'canceled')
        self.assertEqual(self.writes('DrainState')[-1]['state'], 'canceled-first')
        writes = copy.deepcopy(self.fake.mutations)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.mutations, writes)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'canceled')
        self.assertEqual(self.audit(), 0, self.stdout + self.stderr)
        self.assertEqual(self.checks['intent_order'], 'CLEAN')
        self.assertTrue(any('No longer needed' in c['body'] for c in self.fake.comments.values()))

    def test_superseded_drains_completed_and_audits_clean_with_successor_url(self):
        self.history()
        self.assert_ok(self.stop('supersede'))
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'completed')
        self.assertEqual(self.writes('DrainState')[-1]['state'], 'completed-first')
        writes = copy.deepcopy(self.fake.mutations)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.mutations, writes)
        self.assertEqual(self.audit(), 0, self.stdout + self.stderr)
        self.assertEqual(self.checks['intent_order'], 'CLEAN')
        body = '\n'.join(c['body'] for c in self.fake.comments.values())
        for text in ('No longer needed', 'successor', self.receipt['pr'], self.receipt['head']):
            self.assertIn(text, body)

    def test_section_lists_stopped_units_separately_from_running(self):
        self.assert_ok(self.stop())
        state = self.load()
        state['units']['replaced'] = {'state': 'SUPERSEDED', 'terminal_decision': {
            'reason': 'Replaced implementation', 'successor': 'successor'}}
        state['units']['working'] = {'state': 'RUNNING'}
        self.save(state)
        self.draft_file()
        self.assertEqual(self.cli('section', '--audit', str(self.out), '--draft', str(self.draft),
                                  '--plan', str(self.plan), '--state-dir', str(self.state)), 0)
        self.assertIn('ABANDONED: old: No longer needed', self.stdout)
        self.assertIn('SUPERSEDED: replaced: Replaced implementation (successor: successor)', self.stdout)
        self.assertIn('RUNNING: working\n', self.stdout)


if __name__ == '__main__':
    unittest.main()
