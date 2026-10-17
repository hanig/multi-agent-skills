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
        self.outbox = self.state / SO.OUTBOX
        self.seqfile = self.state / SO.OUTBOX_SEQ
        self.receipt_file = self.state / SO.RECEIPTS
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

    def floor_args(self, value, **kwargs):
        return ['seq-floor', '--state-dir', str(kwargs.get('directory', self.state)),
                '--floor', str(value), '--approver', kwargs.get('approver', 'operator'),
                '--reason', kwargs.get('reason', 'highest drained trailer')]

    def floor(self, value, **kwargs):
        return self.swarm_cli(*self.floor_args(value, **kwargs))

    def intents(self):
        return SO.read_outbox(self.state)

    def counter(self):
        return json.loads(self.seqfile.read_text())

    def high_water(self):
        return self.counter()['high_water']

    def drain_ok(self):
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)

    def advance(self, unit_state):
        # CLI acquires the lease; max-new=0 prevents dispatch.
        (self.state / SS.STATE_FILE).write_text(json.dumps({
            'schema_version': 1, 'units': {'u': {
                'state': unit_state, 'attempt_dir': None, 'attempts': [],
                'gpu_hours': 0, 'job_id': None}}, 'halted': None}))
        with mock.patch.object(SO.time, 'strftime', return_value=AT):
            result = self.swarm_cli('advance', str(self.plan),
                '--state-dir', str(self.state), '--root', str(self.root / 'runs'),
                '--max-new-dispatches', '0')
        self.assertEqual(result[0], 0, result)

    def emit(self, uid='new'):
        return SO.emit_intent(self.state, 'project', uid, 'SUBMITTED', {})

    def test_outbox_loss_drain_order(self):
        self.intent(key='seed', seq=6, at=AT)
        self.advance('SUBMITTED')
        old = self.intents()[-1]
        self.assertEqual(old['seq'], 7)
        self.drain_ok()
        self.assertEqual(self.receipts()[0]['key'], old['key'])
        self.outbox.unlink()
        self.advance('FAILED')
        new = self.intents()[0]
        self.assertEqual(new['at'], old['at'])
        self.assertGreaterEqual(new['seq'], 8)
        self.drain_ok()
        parsed = self.comments()
        self.assertEqual(LS.latest_state(parsed)['key'], new['key'])
        self.assertEqual(self.fake.issues['1']['state']['type'], 'unstarted')
        self.assertEqual(len(self.writes('IntentCommentCreate')), 3)

    def test_complete_history_migration(self):
        old = self.intent(seq=4)
        self.drain_ok()
        before = self.outbox.read_bytes()
        self.assertIsNotNone(self.emit())
        self.assertEqual(self.intents()[-1]['seq'], 5)
        self.assertEqual(self.high_water(), 5)
        self.assertTrue(self.outbox.read_bytes().startswith(before))
        self.assertEqual(self.receipts()[0]['key'], old['key'])

    def test_incomplete_history_requires_floor(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.fake.comments.clear()
                self.intent(key='drained', seq=7)
                self.drain_ok()
                path = self.outbox
                if missing:
                    path.unlink()
                else:
                    path.write_text(json.dumps({'key': 'other', 'seq': 4}) + '\n')
                before = path.read_bytes() if path.exists() else None
                receipts = self.receipt_file.read_bytes()
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    self.assertIsNone(self.emit())
                self.assertIn('WARNING', errors.getvalue())
                self.assertIn(str(self.state), errors.getvalue())
                self.assertIn('swarm.py seq-floor', errors.getvalue())
                self.assertEqual(path.read_bytes() if path.exists() else None, before)
                self.assertEqual(self.receipt_file.read_bytes(), receipts)
                self.assertFalse(self.seqfile.exists())
                result = self.swarm_cli('status', str(self.plan), '--state-dir', str(self.state), '--json')
                self.assertEqual(result[0], 0, result)
                self.assertEqual(self.floor(20)[0], 0)
                self.assertIsNotNone(self.emit())
                self.assertEqual(self.intents()[-1]['seq'], 21)
                for filename in (SO.OUTBOX, SO.OUTBOX_SEQ, SO.RECEIPTS):
                    (self.state / filename).unlink()

    def test_floor_and_record(self):
        self.assertEqual(self.floor(20)[0], 0)
        record = self.counter()
        self.assertEqual(record['approver'], 'operator')
        self.assertEqual(record['reason'], 'highest drained trailer')
        self.assertRegex(record['at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        path = self.seqfile
        before = path.read_bytes()
        for value in (19, 20):
            result = self.floor(value)
            self.assertNotEqual(result[0], 0, result)
            self.assertIn('exceed', result[2])
            self.assertEqual(path.read_bytes(), before)
        self.assertIsNotNone(self.emit())
        self.assertEqual(self.counter(), dict(record, high_water=21))
        self.assertEqual(self.floor(22)[0], 0)
        self.assertEqual(self.high_water(), 22)

    def test_counter_fsync_crash(self):
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
        self.assertEqual(self.high_water(), 5)
        self.assertEqual(self.intents(), [])
        self.advance('SUBMITTED')
        self.assertEqual(self.intents()[0]['seq'], 6)
        self.assertEqual(self.high_water(), 6)

    def test_append_failure_and_dedup(self):
        with mock.patch.object(SO.SJ, '_fsync_append', side_effect=OSError('append failed')):
            self.assertIsNone(self.emit())
        self.assertEqual(self.high_water(), 1)
        self.assertIsNotNone(self.emit())
        self.assertEqual(self.intents()[0]['seq'], 2)
        self.assertIsNone(self.emit())
        self.assertEqual(self.high_water(), 2)

    def comments(self):
        return [LS.genuine_comment(c, 'workspace', 'project', '1')
                for c in self.fake.comments.values()]

    def check_remigration(self, cut):
        self.advance('SUBMITTED')
        self.drain_ok()
        before = self.outbox.read_bytes()

        def interrupted(path, record):
            path.write_bytes(before + json.dumps(record).encode()[:cut])
            raise OSError('interrupted append')

        with mock.patch.object(SO.SJ, '_fsync_append', interrupted):
            self.advance('FAILED')
        self.assertEqual(self.high_water(), 2)
        seqs = [row['seq'] for row in self.intents()]
        self.assertEqual(seqs, [1, 2] if cut is None else [1])
        self.seqfile.unlink()
        self.advance('HELD')
        rows = self.intents()
        new = rows[-1]['seq']
        self.assertEqual(new, 3 if cut is None else 2)
        self.assertNotIn(new, seqs)
        self.assertNotIn(new, [c['order'][2] for c in self.comments()])
        self.assertEqual([r['seq'] for r in rows], seqs + [new])
        self.drain_ok()
        self.assertEqual(len(self.fake.comments), len(rows))

    def test_unpublished_reservation_reuse(self):
        self.check_remigration(0)

    def test_incomplete_tail_discarded(self):
        self.check_remigration(-1)

    def test_complete_tail_remigration(self):
        self.check_remigration(None)

    def test_corrupt_counter_preserved(self):
        path = self.seqfile
        for raw in ('{', '[]', '{"schema_version":true,"high_water":3}',
                    '{"schema_version":1,"high_water":true}',
                    '{"schema_version":1,"high_water":-1}'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assertIsNone(self.emit())
                self.assertEqual(path.read_text(), raw)
                self.assertFalse(self.outbox.exists())
                self.assertNotEqual(self.floor(20)[0], 0)

    def test_corruption_recovery_and_dedup(self):
        path = self.outbox
        for corrupt in ('garbage\n', '{"key":"bad","seq":0}\n', '[]\n'):
            with self.subTest(corrupt=corrupt):
                old = SO.outbox_key('project', 'old', 'SUBMITTED', None)
                before = (corrupt + json.dumps({'key': old, 'seq': 4}) + '\n').encode()
                path.write_bytes(before)
                self.assertIsNone(self.emit())
                self.assertEqual(self.floor(20)[0], 0)
                self.assertIsNone(self.emit('old'))
                self.assertEqual(self.high_water(), 20)
                self.assertIsNotNone(self.emit())
                after = path.read_bytes()
                self.assertTrue(after.startswith(before))
                self.assertEqual(json.loads(after[len(before):])['seq'], 21)
                self.seqfile.unlink()

    def test_bad_history_refused(self):
        path = self.outbox
        path.write_text('{"key":"old","seq":4}\n')
        receipt = self.receipt_file
        for key in ({'bad': 'shape'}, ['bad']):
            receipt.write_text(json.dumps({'key': key, 'ref': 'fixture', 'attested': True}) + '\n')
            self.assertIsNone(self.emit())
            self.assertFalse(self.seqfile.exists())
        receipt.unlink()
        for raw in ('{}\n', '{"key":"bad","seq":true}\n',
                    'garbage\n', '{"key":"old","seq":4}\ngarbage\n'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assertIsNone(self.emit())
                self.assertEqual(path.read_text(), raw)
                self.assertFalse(self.seqfile.exists())
        path.unlink()
        self.receipt_file.touch()
        self.assertIsNone(self.emit())
        self.assertFalse(self.seqfile.exists())

    def test_floor_attribution_and_path(self):
        for kwargs in ({'approver': '  '}, {'reason': '  '}):
            self.assertNotEqual(self.floor(20, **kwargs)[0], 0)
            self.assertFalse(self.seqfile.exists())
        self.assertNotEqual(self.floor(-1)[0], 0)
        inside = SWARM.parents[3] / 'forbidden-seq-state'
        self.assertFalse(inside.exists())
        result = self.swarm_cli(*self.floor_args(20, directory=inside))
        self.assertNotEqual(result[0], 0)
        self.assertFalse(inside.exists())

    def test_floor_obeys_coordinator_lease(self):
        self.assertEqual(self.floor(20)[0], 0)
        self.assertTrue(SS.acquire_lease(self.state)[0])
        try:
            result = subprocess.run([sys.executable, str(SWARM)] + self.floor_args(21), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('lock', result.stderr)
            self.assertEqual(self.high_water(), 20)
        finally:
            SS.release_lease(self.state)


if __name__ == '__main__':
    unittest.main()
