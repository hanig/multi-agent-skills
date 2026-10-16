"""Durable allocation through the coordinator CLI and the offline drain fixture."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tests.test_linear_drain import DrainCase, LS
import swarm as S
import swarm_outbox as SO
import swarm_state as SS


AT = '2026-10-16T10:00:00+0000'
SWARM = Path(S.__file__)


class TestDurableSequence(DrainCase):
    def setUp(self):
        super().setUp()
        self.plan.write_text(json.dumps({'name': 'project', 'units': [
            {'id': 'u', 'kind': 'slurm', 'runtime': 'none', 'command': 'true',
             'outputs': ['out'], 'write_scopes': ['u/'], 'tracker': 'ARC-1'}]}))

    def swarm_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', [str(SWARM)] + list(args)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as done:
                S.main()
        return done.exception.code, out.getvalue(), err.getvalue()

    def floor(self, value, **kwargs):
        return self.swarm_cli('seq-floor', '--state-dir', str(self.state),
                              '--floor', str(value), '--approver', kwargs.get('approver', 'operator'),
                              '--reason', kwargs.get('reason', 'highest drained trailer'))

    def counter(self):
        return json.loads((self.state / SO.OUTBOX_SEQ).read_text())

    def advance(self, unit_state):
        # Seed a durable transition; max-new=0 prevents scheduler dispatch.
        # The actual CLI acquires the lease and repairs the missing intent.
        (self.state / SS.STATE_FILE).write_text(json.dumps({
            'schema_version': 1, 'units': {'u': {
                'state': unit_state, 'attempt_dir': None, 'attempts': [],
                'gpu_hours': 0, 'job_id': None}}, 'halted': None}))
        with mock.patch.object(SO.time, 'strftime', return_value=AT):
            return self.swarm_cli('advance', str(self.plan),
                '--state-dir', str(self.state), '--root', str(self.root / 'runs'),
                '--max-new-dispatches', '0')

    def emit(self, uid='new'):
        return SO.emit_intent(self.state, 'project', uid, 'SUBMITTED', {})

    def test_luna_lost_outbox_keeps_same_second_order_through_cli_and_drain(self):
        self.intent(key='seed', seq=6, at=AT)
        result = self.advance('SUBMITTED')
        self.assertEqual(result[0], 0, result)
        old = SO.read_outbox(self.state)[-1]
        self.assertEqual(old['seq'], 7)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.receipts()[0]['key'], old['key'])
        (self.state / SO.OUTBOX).unlink()
        result = self.advance('FAILED')
        self.assertEqual(result[0], 0, result)
        new = SO.read_outbox(self.state)[0]
        self.assertEqual(new['at'], old['at'])
        self.assertGreaterEqual(new['seq'], 8)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        parsed = [LS.genuine_comment(c, 'workspace', 'project', '1')
                  for c in self.fake.comments.values()]
        self.assertEqual(LS.latest_state(parsed)['key'], new['key'])
        self.assertEqual(self.fake.issues['1']['state']['type'], 'unstarted')
        self.assertEqual(len(self.writes('IntentCommentCreate')), 3)

    def test_complete_migration_persists_maximum_then_allocates_five(self):
        old = self.intent(seq=4)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        before = (self.state / SO.OUTBOX).read_bytes()
        self.assertIsNotNone(self.emit())
        self.assertEqual(SO.read_outbox(self.state)[-1]['seq'], 5)
        self.assertEqual(self.counter()['high_water'], 5)
        self.assertTrue((self.state / SO.OUTBOX).read_bytes().startswith(before))
        self.assertEqual(self.receipts()[0]['key'], old['key'])

    def test_missing_and_incomplete_history_refuse_until_recorded_floor(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.fake.comments.clear()
                self.intent(key='drained', seq=7)
                self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
                path = self.state / SO.OUTBOX
                if missing:
                    path.unlink()
                else:
                    path.write_text(json.dumps({'key': 'other', 'seq': 4}) + '\n')
                before = path.read_bytes() if path.exists() else None
                receipts = (self.state / SO.RECEIPTS).read_bytes()
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    self.assertIsNone(self.emit())
                self.assertIn('WARNING', errors.getvalue())
                self.assertIn(str(self.state), errors.getvalue())
                self.assertIn('swarm.py seq-floor', errors.getvalue())
                self.assertEqual(path.read_bytes() if path.exists() else None, before)
                self.assertEqual((self.state / SO.RECEIPTS).read_bytes(), receipts)
                self.assertFalse((self.state / SO.OUTBOX_SEQ).exists())
                result = self.swarm_cli('status', str(self.plan), '--state-dir', str(self.state), '--json')
                self.assertEqual(result[0], 0, result)
                self.assertEqual(self.floor(20)[0], 0)
                self.assertIsNotNone(self.emit())
                self.assertEqual(SO.read_outbox(self.state)[-1]['seq'], 21)
                for filename in (SO.OUTBOX, SO.OUTBOX_SEQ, SO.RECEIPTS):
                    (self.state / filename).unlink()

    def test_floor_never_lowers_and_preserves_operator_record(self):
        self.assertEqual(self.floor(20)[0], 0)
        record = self.counter()
        self.assertEqual(record['approver'], 'operator')
        self.assertEqual(record['reason'], 'highest drained trailer')
        self.assertRegex(record['at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        path = self.state / SO.OUTBOX_SEQ
        before = path.read_bytes()
        for value in (19, 20):
            result = self.floor(value)
            self.assertNotEqual(result[0], 0, result)
            self.assertIn('exceed', result[2])
            self.assertEqual(path.read_bytes(), before)
        self.assertIsNotNone(self.emit())
        self.assertEqual(self.counter(), dict(record, high_water=21))
        self.assertEqual(self.floor(22)[0], 0)
        self.assertEqual(self.counter()['high_water'], 22)

    def test_crash_after_counter_directory_fsync_leaves_gap_without_reuse(self):
        self.assertEqual(self.floor(4)[0], 0)
        script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import swarm_outbox as SO
import swarm_journal as SJ
import swarm_state as SS
real = SJ._fsync_directory
def crash(path):
    real(path)
    os._exit(99)
SJ._fsync_directory = crash
assert SS.acquire_lease(sys.argv[2])[0]
SO.emit_intent(sys.argv[2], 'project', 'crashed', 'SUBMITTED', {})
"""
        result = subprocess.run([sys.executable, '-c', script, str(SWARM.parent), str(self.state)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 99, result.stderr)
        self.assertEqual(self.counter()['high_water'], 5)
        self.assertEqual(SO.read_outbox(self.state), [])
        self.assertEqual(self.advance('SUBMITTED')[0], 0)
        self.assertEqual(SO.read_outbox(self.state)[0]['seq'], 6)
        self.assertEqual(self.counter()['high_water'], 6)

    def test_append_failure_consumes_reservation_and_duplicate_consumes_nothing(self):
        with mock.patch.object(SO.SJ, '_fsync_append', side_effect=OSError('append failed')):
            self.assertIsNone(self.emit())
        self.assertEqual(self.counter()['high_water'], 1)
        self.assertIsNotNone(self.emit())
        self.assertEqual(SO.read_outbox(self.state)[0]['seq'], 2)
        self.assertIsNone(self.emit())
        self.assertEqual(self.counter()['high_water'], 2)

    def test_corrupt_counter_refuses_without_rewriting_journals(self):
        path = self.state / SO.OUTBOX_SEQ
        for raw in ('{', '[]', '{"schema_version":true,"high_water":3}',
                    '{"schema_version":1,"high_water":true}',
                    '{"schema_version":1,"high_water":-1}'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assertIsNone(self.emit())
                self.assertEqual(path.read_text(), raw)
                self.assertFalse((self.state / SO.OUTBOX).exists())
                self.assertNotEqual(self.floor(20)[0], 0)

    def test_migration_refuses_corrupt_history_and_empty_receipts_without_outbox(self):
        path = self.state / SO.OUTBOX
        for raw in ('{}\n', '{"key":"bad","seq":true}\n',
                    'garbage\n', '{"key":"old","seq":4}\ngarbage\n'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assertIsNone(self.emit())
                self.assertEqual(path.read_text(), raw)
                self.assertFalse((self.state / SO.OUTBOX_SEQ).exists())
        path.unlink()
        (self.state / SO.RECEIPTS).touch()
        self.assertIsNone(self.emit())
        self.assertFalse((self.state / SO.OUTBOX_SEQ).exists())

    def test_floor_requires_approver_reason_and_external_path_before_creation(self):
        for kwargs in ({'approver': '  '}, {'reason': '  '}):
            self.assertNotEqual(self.floor(20, **kwargs)[0], 0)
            self.assertFalse((self.state / SO.OUTBOX_SEQ).exists())
        self.assertNotEqual(self.floor(-1)[0], 0)
        inside = SWARM.parents[3] / 'forbidden-seq-state'
        self.assertFalse(inside.exists())
        result = self.swarm_cli('seq-floor', '--state-dir', str(inside),
                               '--floor', '20', '--approver', 'operator', '--reason', 'test')
        self.assertNotEqual(result[0], 0)
        self.assertFalse(inside.exists())

    def test_floor_obeys_coordinator_lease(self):
        self.assertEqual(self.floor(20)[0], 0)
        self.assertTrue(SS.acquire_lease(self.state)[0])
        try:
            result = subprocess.run([sys.executable, str(SWARM), 'seq-floor',
                '--state-dir', str(self.state), '--floor', '21',
                '--approver', 'operator', '--reason', 'test'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('lock', result.stderr)
            self.assertEqual(self.counter()['high_water'], 20)
        finally:
            SS.release_lease(self.state)


if __name__ == '__main__':
    unittest.main()
