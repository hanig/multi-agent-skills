"""Issue labels survive tracker intents, restarts and legacy outbox repair."""

import copy
import io
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / 'skills/hanig-swarm/scripts/swarm.py'
sys.path.insert(0, str(SWARM.parent))
import swarm as S

PLAN = {'name': 'tracker-test', 'units': [
    {'id': 'u', 'kind': 'slurm', 'runtime': 'none',
     'command': 'true', 'outputs': ['o']}]}


class TestTrackerField(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.state_dir = self.directory / 'state'
        self.state_dir.mkdir()
        self.plan = copy.deepcopy(PLAN)
        self.unit = self.plan['units'][0]
        self.unit['tracker'] = 'ARC-1'

    def outbox(self, *extra):
        result = subprocess.run(
            [sys.executable, str(SWARM), 'outbox', '--state-dir', str(self.state_dir),
             *extra], capture_output=True, text=True, cwd=str(self.directory))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def emit(self, state, tracker=True):
        args = {'tracker': self.unit['tracker']} if tracker else {}
        return S.emit_intent(
            self.state_dir, self.plan['name'], self.unit['id'], state,
            {'attempt_dir': '/runs/u/a1'},
            evidence={'receipt': {'state': 'DONE'}} if state == 'DONE' else None,
            kind=self.unit['kind'], **args)

    def test_start_open_pr_close_carry_tracker_and_cli_shows_it(self):
        S.validate_plan(self.plan)
        for state in ('SUBMITTED', 'READY_FOR_PR', 'DONE'):
            self.emit(state)
        # Read persisted bytes independently of the normalizing reader.
        rows = [json.loads(line) for line in
                (self.state_dir / S.OUTBOX).read_text().splitlines()]
        self.assertEqual([row['verb'] for row in rows], ['start', 'open_pr', 'close'])
        self.assertEqual([row.get('tracker') for row in rows], ['ARC-1'] * 3)
        shown = json.loads(self.outbox('--json'))['intents']
        self.assertEqual([row['tracker'] for row in shown], ['ARC-1'] * 3)
        text = self.outbox()
        self.assertEqual(text.count("tracker: 'ARC-1'"), 3)
        for row in rows:
            self.assertIn(row['key'], text)

    def test_all_event_siblings_retain_the_exact_label(self):
        self.unit['tracker'] = ' ARC-1 '
        for state in S.TRACKER_EVENTS:
            self.emit(state)
        rows = S.load_outbox_contract(self.state_dir)
        self.assertEqual(len(rows), len(S.TRACKER_EVENTS))
        self.assertTrue(all(row['tracker'] == ' ARC-1 ' for row in rows))

    def test_validate_refuses_empty_and_non_string_tracker(self):
        for bad in ('', ' \t\n', None, False, 1, [], {}, ['ARC-1']):
            with self.subTest(tracker=bad):
                self.unit['tracker'] = bad
                with self.assertRaisesRegex(S.PlanError, 'tracker'):
                    S.validate_plan(self.plan)

    def test_cli_validate_names_tracker_and_schema_documents_it(self):
        self.unit['tracker'] = ''
        plan_path = self.directory / 'plan.json'
        plan_path.write_text(json.dumps(self.plan))
        result = subprocess.run([sys.executable, str(SWARM), 'validate', str(plan_path)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('tracker', result.stdout + result.stderr)
        schema = subprocess.run([sys.executable, str(SWARM), 'schema', '--json'],
                                capture_output=True, text=True, check=True)
        fields = {row['field']: row for row in json.loads(schema.stdout)['fields']}
        self.assertEqual(fields['tracker']['requirement'], 'optional')
        self.assertIn('non-empty', fields['tracker']['notes'])

    def test_omission_preserves_base_digest_and_intent_keys(self):
        # Values measured on recorded base 33bedaa before this change.
        self.assertEqual(S.plan_digest(PLAN),
                         '6b3c347182387ba796092cd6137d4642a12d21d31b6d8cab57951b3343759cff')
        expected = ['93cb7a47362a7328', 'fe472adc238adecb', 'ffdb52d3166d42ef']
        keys = [self.emit(state, tracker=False)
                for state in ('SUBMITTED', 'READY_FOR_PR', 'DONE')]
        self.assertEqual(keys, expected)
        self.assertTrue(all('tracker' not in row for row in S.read_outbox(self.state_dir)))
        for state in ('SUBMITTED', 'READY_FOR_PR', 'DONE'):
            self.assertIsNone(self.emit(state), 'adding a label must not create a new key')
        self.assertEqual([row['key'] for row in S.read_outbox(self.state_dir)], expected)

    def advance(self, dry_run=False):
        state = {'schema_version': 1, 'halted': None,
                 'plan_digest': S.plan_digest(self.plan), 'units': {'u': {
                     'state': 'FAILED', 'attempt_dir': None,
                     'attempts': [], 'gpu_hours': 0}}}
        S.save_state(self.state_dir, state)
        ok, why = S.acquire_lease(self.state_dir)
        self.assertTrue(ok, why)
        try:
            result = S.advance(self.plan, state, self.state_dir,
                               self.directory / 'runs', dry_run, max_new=0)
        finally:
            S.release_lease(self.state_dir)
        self.assertIsNone(result[2], result)
        self.assertEqual(state['units']['u']['state'], 'FAILED')
        return result

    def test_advance_forwards_label_and_repairs_historical_intents_without_rekeying(self):
        old = [self.emit(state, tracker=False)
               for state in ('SUBMITTED', 'READY_FOR_PR', 'DONE')]
        before = S.read_outbox(self.state_dir)
        (self.state_dir / S.OUTBOX).chmod(0o640)
        S.record_receipt(self.state_dir, old[0], 'ARC-1')
        receipt_bytes = (self.state_dir / S.RECEIPTS).read_bytes()
        self.advance()
        after = S.read_outbox(self.state_dir)
        self.assertEqual(len(after), 4)
        self.assertEqual(stat.S_IMODE((self.state_dir / S.OUTBOX).stat().st_mode), 0o640)
        self.assertEqual([row['tracker'] for row in after], ['ARC-1'] * 4)
        for previous, current in zip(before, after):
            self.assertEqual(current, dict(previous, tracker='ARC-1'))
        self.assertEqual((self.state_dir / S.RECEIPTS).read_bytes(), receipt_bytes)
        S.load_outbox_contract(self.state_dir)
        saved = (self.state_dir / S.OUTBOX).read_bytes()
        self.advance()
        self.assertEqual((self.state_dir / S.OUTBOX).read_bytes(), saved)

    def test_backfill_preserves_other_projects_units_and_existing_labels(self):
        for project, unit, label in [('other', 'u', None), ('tracker-test', 'other', None),
                                      ('tracker-test', 'u', 'ARC-old')]:
            S.emit_intent(self.state_dir, project, unit, 'FAILED',
                          {'attempt_dir': '/runs/u/a1'}, tracker=label)
        before = (self.state_dir / S.OUTBOX).read_bytes()
        self.advance()
        self.assertTrue((self.state_dir / S.OUTBOX).read_bytes().startswith(before))

    def test_unaccepted_plan_change_cannot_backfill(self):
        self.emit('SUBMITTED', tracker=False)
        before = (self.state_dir / S.OUTBOX).read_bytes()
        state = {'schema_version': 1, 'halted': None,
                 'plan_digest': S.plan_digest(PLAN), 'units': {'u': {
                     'state': 'FAILED', 'attempt_dir': None,
                     'attempts': [], 'gpu_hours': 0}}}
        S.save_state(self.state_dir, state)
        ok, why = S.acquire_lease(self.state_dir)
        self.assertTrue(ok, why)
        try:
            result = S.advance(self.plan, state, self.state_dir,
                               self.directory / 'runs', False, max_new=0)
        finally:
            S.release_lease(self.state_dir)
        self.assertEqual(result[2], 'plan changed mid-flight')
        self.assertEqual((self.state_dir / S.OUTBOX).read_bytes(), before)

    def test_dry_run_does_not_backfill(self):
        self.emit('SUBMITTED', tracker=False)
        before = (self.state_dir / S.OUTBOX).read_bytes()
        self.advance(dry_run=True)
        self.assertEqual((self.state_dir / S.OUTBOX).read_bytes(), before)

    def test_failed_backfill_retains_bytes_and_does_not_halt(self):
        self.emit('SUBMITTED', tracker=False)
        before = (self.state_dir / S.OUTBOX).read_bytes()
        real_replace = os.replace

        def fail_label_replace(src, dst):
            if str(src).split('/')[-1].startswith('.outbox-tracker-'):
                raise OSError('injected migration failure')
            return real_replace(src, dst)

        err = io.StringIO()
        with mock.patch.object(S.os, 'replace', side_effect=fail_label_replace), redirect_stderr(err):
            self.advance()
        self.assertIn('could not backfill tracker labels', err.getvalue())
        self.assertTrue((self.state_dir / S.OUTBOX).read_bytes().startswith(before))
        self.assertEqual(list(self.state_dir.glob('.outbox-tracker-*')), [])

    def test_outbox_stat_failure_does_not_halt_advance(self):
        original = Path.is_file

        def failed_stat(path):
            if path == self.state_dir / S.OUTBOX:
                raise OSError('injected outbox stat I/O error')
            return original(path)

        err = io.StringIO()
        with mock.patch.object(Path, 'is_file', failed_stat), redirect_stderr(err):
            self.advance()
        self.assertIn('could not backfill tracker labels', err.getvalue())


if __name__ == '__main__':
    unittest.main()
