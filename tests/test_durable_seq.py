"""Durable sequences through the CLI and fake drain."""
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
        self.box = self.state / SO.OUTBOX
        self.seq = self.state / SO.OUTBOX_SEQ
        self.acks = self.state / SO.RECEIPTS
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

    def floor_args(self, n, directory=None, approver='operator', reason='drained trailer'):
        return ['seq-floor', '--state-dir', str(directory or self.state),
                '--floor', str(n), '--approver', approver, '--reason', reason]

    def floor(self, value, **kwargs):
        return self.swarm_cli(*self.floor_args(value, **kwargs))

    def intents(self):
        return SO.read_outbox(self.state)

    def counter(self):
        return json.loads(self.seq.read_text())

    def assert_water(self, n):
        self.assertEqual(self.counter()['high_water'], n)

    def drain_ok(self):
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)

    def advance(self, unit_state):
        (self.state / SS.STATE_FILE).write_text(json.dumps({
            'schema_version': 1, 'units': {'u': {
                'state': unit_state, 'attempt_dir': None, 'attempts': [],
                'gpu_hours': 0, 'job_id': None}}, 'halted': None}))
        with mock.patch.object(SO.time, 'strftime', return_value=AT):
            result = self.swarm_cli('advance', str(self.plan),
                '--state-dir', str(self.state), '--root', str(self.root / 'runs'),
                '--max-new-dispatches', '0')
        self.assertEqual(result[0], 0, result)
        return result

    def emit(self, uid='new'):
        return SO.emit_intent(self.state, 'project', uid, 'SUBMITTED', {})

    def assert_blocked(self):
        self.assertIsNone(self.emit())
        self.assertFalse(self.seq.exists())

    def check_malformed(self, raw):
        self.advance('SUBMITTED')
        row = self.box.read_bytes()
        self.seq.unlink()
        before = row + raw + b'\n' + row
        self.box.write_bytes(before)
        result = self.advance('FAILED')
        self.assertIn(str(self.box) + ' line 2:', result[2])
        self.assertIn('swarm.py seq-floor', result[2])
        self.assertFalse(self.seq.exists())
        self.assertEqual(self.box.read_bytes(), before)
        self.assertEqual(self.floor(20)[0], 0)
        self.advance('FAILED')
        self.assertEqual(json.loads(self.box.read_bytes()[len(before):])['seq'], 21)
        self.advance('HELD')
        self.assert_water(22)

    def test_counter_refusal_and_recovery(self):
        self.advance('SUBMITTED')
        before = self.box.read_bytes()
        for raw in (b'{', b'\xff', b'[' * 10000 + b'0' + b']' * 10000):
            with self.subTest(size=len(raw)):
                self.seq.write_bytes(raw)
                result = self.advance('FAILED')
                self.assertIn(str(self.seq), result[2])
                self.assertIn('swarm.py seq-floor', result[2])
                self.assertEqual(self.seq.read_bytes(), raw)
                self.assertEqual(self.box.read_bytes(), before)
                self.assertEqual(self.floor(20)[0], 0)
                self.assert_water(20)
        self.advance('FAILED')
        self.assertEqual(self.intents()[-1]['seq'], 21)

    def test_invalid_utf8_middle(self):
        self.check_malformed(b'\xff')

    def test_dangling_counter_requires_floor(self):
        self.intent(key='old', seq=5, project='project')
        missing = self.state / 'missing-counter'
        self.seq.symlink_to(missing.name)
        before = self.box.read_bytes()
        result = self.advance('FAILED')
        self.assertIn('unreadable counter', result[2], self.intents())
        self.assertIn(str(self.seq), result[2])
        self.assertIn('swarm.py seq-floor', result[2])
        self.assertTrue(self.seq.is_symlink())
        self.assertEqual(SO.os.readlink(self.seq), missing.name)
        self.assertEqual(self.box.read_bytes(), before)
        self.assertFalse(missing.exists())
        self.assertEqual(self.floor(20)[0], 0)
        self.assertFalse(self.seq.is_symlink())
        self.assertIs(self.counter()['replaced_malformed_counter'], True)
        self.assert_water(20)
        self.assertFalse(missing.exists())
        self.advance('FAILED')
        self.assertEqual(self.intents()[-1]['seq'], 21)
        self.advance('HELD')
        self.assert_water(22)

    def test_malformed_middle(self):
        self.check_malformed(b'{"key":"interrupted","seq":')

    def test_fifo_counter_requires_floor(self):
        fifo = self.state / 'fifo'
        SO.os.mkfifo(fifo)
        for link in (False, True):
            with self.subTest(symlink=link):
                if self.seq.exists():
                    self.seq.unlink()
                if link:
                    self.seq.symlink_to(fifo.name)
                else:
                    SO.os.mkfifo(self.seq)
                result = subprocess.run([sys.executable, str(SWARM)] + self.floor_args(20),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.seq.is_file())
                self.assertFalse(self.seq.is_symlink())
                self.assert_water(20)
                self.assertIs(self.counter()['replaced_malformed_counter'], True)
                self.assertTrue(SO.stat.S_ISFIFO(fifo.stat().st_mode))

    def test_valid_counter_allocation_is_independent_of_history(self):
        self.assertEqual(self.floor(20)[0], 0)
        self.box.write_bytes(b'\xff\n{"key":"damaged","seq":\n')
        allocate = SO._allocate_sequence
        open_path = Path.open

        def no_history(path, *args, **kwargs):
            self.assertNotEqual(path.name, SO.OUTBOX, 'allocation read outbox history')
            return open_path(path, *args, **kwargs)

        def independent(directory, history):
            # Allocation cannot reuse even predecoded dedup history.
            unusable = mock.MagicMock()
            unusable.__iter__.side_effect = AssertionError('allocation used history')
            with mock.patch.object(Path, 'open', no_history):
                return allocate(directory, unusable)

        with mock.patch.object(SO, '_allocate_sequence', independent):
            self.advance('SUBMITTED')
            self.advance('FAILED')
        self.assertEqual([r['seq'] for r in self.intents()], [21, 22])
        self.assert_water(22)

    def test_reemitted_receipted_key_does_not_change_drain_order(self):
        self.advance('SUBMITTED')
        old = self.intents()[0]
        self.drain_ok()
        self.advance('FAILED')
        latest = self.intents()[-1]
        self.drain_ok()
        receipts = self.acks.read_bytes()
        comments = dict(self.fake.comments)
        mutations = list(self.fake.mutations)
        self.box.write_bytes(b'{broken}\n' + self.box.read_bytes().splitlines(True)[1])
        self.advance('SUBMITTED')
        duplicate = self.intents()[-1]
        self.assertEqual(duplicate['key'], old['key'])
        self.assertEqual(duplicate['seq'], 3)
        self.assertEqual(LS.comment_id('workspace', 'project', '1', duplicate['key']),
                         LS.comment_id('workspace', 'project', '1', old['key']))
        # Drain reports the corrupt line but still processes collectable rows.
        self.assertEqual(self.drain(), 3, self.stdout + self.stderr)
        self.assertIn('line 1', self.stdout + self.stderr)
        self.assertEqual(self.acks.read_bytes(), receipts)
        self.assertEqual(self.fake.comments, comments)
        self.assertEqual(self.fake.mutations, mutations)
        self.assertEqual(LS.latest_state(self.comments())['key'], latest['key'])
        self.assertEqual(self.fake.issues['1']['state']['type'], 'unstarted')

    def test_killed_final_append_migrates_automatically(self):
        self.advance('SUBMITTED')
        prefix = self.box.read_bytes()
        script = """
import fcntl, json, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import swarm as S
import swarm_journal as SJ
import swarm_state as SS
directory = Path(sys.argv[2])
state = directory / SS.STATE_FILE
record = json.loads(state.read_text())
record['units']['u']['state'] = 'FAILED'
state.write_text(json.dumps(record))
def interrupted(path, intent):
    with path.open('ab') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(intent).encode()[:-1])
        handle.flush()
        os.fsync(handle.fileno())
        os.kill(os.getpid(), signal.SIGKILL)
SJ._fsync_append = interrupted
sys.argv = ['swarm.py', 'advance', sys.argv[3], '--state-dir', str(directory),
            '--root', sys.argv[4], '--max-new-dispatches', '0']
S.main()
"""
        result = subprocess.run([sys.executable, '-c', script, str(SWARM.parent),
            str(self.state), str(self.plan), str(self.root / 'runs')],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, -9, result.stderr)
        self.assert_water(2)
        torn = self.box.read_bytes()[len(prefix):]
        self.assertTrue(torn)
        self.assertFalse(torn.endswith(b'\n'))
        with self.assertRaises(json.JSONDecodeError):
            json.loads(torn)
        self.seq.unlink()  # Exercise migration of this legacy-shaped journal.
        self.advance('HELD')
        self.assertTrue(self.box.read_bytes().startswith(prefix))
        self.assertEqual([r['seq'] for r in self.intents()], [1, 2])
        self.assertEqual(self.intents()[-1]['verb'], 'block')
        self.drain_ok()
        self.assertEqual(len(self.fake.comments), 2)

    def test_deep_json(self):
        self.check_malformed(b'[' * 10000 + b'0' + b']' * 10000)

    def test_duplicate_members(self):
        self.check_malformed(b'{"key":"higher","seq":20,"seq":1}')

    def test_parser_limit(self):
        # 3.9 lacks the integer-conversion limit; exercise the same ValueError
        # at the parser boundary there, and the real runtime limit on 3.12.
        raw = b'{"key":"huge","seq":' + b'1' * 5000 + b'}'
        loads = json.loads
        def limited(data, *args, **kwargs):
            if not hasattr(sys, 'set_int_max_str_digits') and data.strip() == raw.decode():
                raise ValueError('integer string conversion limit')
            return loads(data, *args, **kwargs)
        with mock.patch.object(SO.json, 'loads', limited):
            self.check_malformed(raw)

    def test_loss_order(self):
        self.intent(key='seed', seq=6, at=AT)
        self.advance('SUBMITTED')
        old = self.intents()[-1]
        self.assertEqual(old['seq'], 7)
        self.drain_ok()
        self.assertEqual(self.receipts()[0]['key'], old['key'])
        self.box.unlink()
        self.advance('FAILED')
        new = self.intents()[0]
        self.assertEqual(new['at'], old['at'])
        self.assertGreaterEqual(new['seq'], 8)
        self.drain_ok()
        parsed = self.comments()
        self.assertEqual(LS.latest_state(parsed)['key'], new['key'])
        self.assertEqual(self.fake.issues['1']['state']['type'], 'unstarted')
        self.assertEqual(len(self.writes('IntentCommentCreate')), 3)

    def test_migration(self):
        old = self.intent(seq=4)
        self.drain_ok()
        before = self.box.read_bytes()
        self.assertIsNotNone(self.emit())
        self.assertEqual(self.intents()[-1]['seq'], 5)
        self.assert_water(5)
        self.assertTrue(self.box.read_bytes().startswith(before))
        self.assertEqual(self.receipts()[0]['key'], old['key'])

    def test_missing_history(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.fake.comments.clear()
                self.intent(key='drained', seq=7)
                self.drain_ok()
                path = self.box
                if missing:
                    path.unlink()
                else:
                    path.write_text(json.dumps({'key': 'other', 'seq': 4}) + '\n')
                before = path.read_bytes() if path.exists() else None
                receipts = self.acks.read_bytes()
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    self.assertIsNone(self.emit())
                self.assertIn('WARNING', errors.getvalue())
                self.assertIn(str(self.state), errors.getvalue())
                self.assertIn('swarm.py seq-floor', errors.getvalue())
                self.assertEqual(path.read_bytes() if path.exists() else None, before)
                self.assertEqual(self.acks.read_bytes(), receipts)
                self.assertFalse(self.seq.exists())
                result = self.swarm_cli('status', str(self.plan), '--state-dir', str(self.state), '--json')
                self.assertEqual(result[0], 0, result)
                self.assertEqual(self.floor(20)[0], 0)
                self.assertIsNotNone(self.emit())
                self.assertEqual(self.intents()[-1]['seq'], 21)
                for filename in (SO.OUTBOX, SO.OUTBOX_SEQ, SO.RECEIPTS):
                    (self.state / filename).unlink()

    def test_floor(self):
        self.assertEqual(self.floor(20)[0], 0)
        record = self.counter()
        self.assertEqual(record['approver'], 'operator')
        self.assertEqual(record['reason'], 'drained trailer')
        self.assertRegex(record['at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        path = self.seq
        before = path.read_bytes()
        for value in (19, 20):
            result = self.floor(value)
            self.assertNotEqual(result[0], 0, result)
            self.assertIn('exceed', result[2])
            self.assertEqual(path.read_bytes(), before)
        self.assertIsNotNone(self.emit())
        self.assertEqual(self.counter(), dict(record, high_water=21))
        self.assertEqual(self.floor(22)[0], 0)
        self.assert_water(22)

    def test_crash(self):
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
        self.assert_water(5)
        self.assertEqual(self.intents(), [])
        self.advance('SUBMITTED')
        self.assertEqual(self.intents()[0]['seq'], 6)
        self.assert_water(6)

    def test_append_failure(self):
        with mock.patch.object(SO.SJ, '_fsync_append', side_effect=OSError('append failed')):
            self.assertIsNone(self.emit())
        self.assert_water(1)
        self.assertIsNotNone(self.emit())
        self.assertEqual(self.intents()[0]['seq'], 2)
        self.assertIsNone(self.emit())
        self.assert_water(2)

    def comments(self):
        return [LS.genuine_comment(c, 'workspace', 'project', '1')
                for c in self.fake.comments.values()]

    def check_remigration(self, cut):
        self.advance('SUBMITTED')
        self.drain_ok()
        before = self.box.read_bytes()

        def interrupted(path, record):
            path.write_bytes(before + json.dumps(record).encode()[:cut])
            raise OSError('interrupted append')

        with mock.patch.object(SO.SJ, '_fsync_append', interrupted):
            self.advance('FAILED')
        self.assert_water(2)
        seqs = [row['seq'] for row in self.intents()]
        self.assertEqual(seqs, [1, 2] if cut is None else [1])
        self.seq.unlink()
        self.advance('HELD')
        rows = self.intents()
        new = rows[-1]['seq']
        self.assertEqual(new, 3 if cut is None else 2)
        self.assertNotIn(new, seqs)
        self.assertNotIn(new, [c['order'][2] for c in self.comments()])
        self.assertEqual([r['seq'] for r in rows], seqs + [new])
        self.drain_ok()
        self.assertEqual(len(self.fake.comments), len(rows))

    def test_unpublished_reuse(self):
        self.check_remigration(0)

    def test_incomplete_tail(self):
        self.check_remigration(-1)

    def test_complete_tail(self):
        self.check_remigration(None)

    def test_corrupt_counter(self):
        path = self.seq
        for raw in ('{', '[]', '{"schema_version":true,"high_water":3}',
                    '{"schema_version":1,"high_water":true}',
                    '{"schema_version":1,"high_water":-1}'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assertIsNone(self.emit())
                self.assertEqual(path.read_text(), raw)
                self.assertFalse(self.box.exists())
                write_sequence = SO._write_sequence
                replace = SO.os.replace
                def atomic(directory, record):
                    with mock.patch.object(SO.os, 'fsync', wraps=SO.os.fsync) as fsync:
                        def publish(source, target):
                            self.assertEqual(Path(source).parent, Path(directory))
                            self.assertNotEqual(Path(source), Path(target))
                            self.assertEqual(Path(target), Path(directory) / SO.OUTBOX_SEQ)
                            self.assertEqual(path.read_text(), raw)
                            self.assertEqual(json.loads(Path(source).read_text()), record)
                            self.assertEqual(fsync.call_count, 1)  # Temp file before rename.
                            replace(source, target)
                        with mock.patch.object(SO.os, 'replace', side_effect=publish) as rename, \
                                mock.patch.object(SO.SJ, '_fsync_directory',
                                                  wraps=SO.SJ._fsync_directory) as sync_dir:
                            write_sequence(directory, record)
                        rename.assert_called_once()
                        sync_dir.assert_called_once_with(Path(directory))
                        self.assertEqual(fsync.call_count, 2)  # Directory after rename.
                with mock.patch.object(SO, '_write_sequence', side_effect=atomic) as write:
                    self.assertEqual(self.floor(20)[0], 0)
                write.assert_called_once()
                self.assert_water(20)
                record = self.counter()
                self.assertEqual(record['approver'], 'operator')
                self.assertEqual(record['reason'], 'drained trailer')
                self.assertRegex(record['at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
                self.assertIs(record['replaced_malformed_counter'], True)
                self.assertEqual(list(self.state.glob('.outbox-seq-*')), [])

    def test_corruption(self):
        path = self.box
        old = SO.outbox_key('project', 'old', 'SUBMITTED', None)
        row = (json.dumps({'key': old, 'seq': 4}) + '\n').encode()
        tails = (b"[" * 10000 + b"0" + b"]" * 10000, b"1" * 5000)
        for corrupt in (b'garbage\n', b'{"key":"bad","seq":0}\n', b'[]\n',
                        b'\xff\n') + tuple(t + b"\n" for t in tails):
            with self.subTest(bytes=len(corrupt)):
                before = corrupt + row
                tail = corrupt[:-1] if corrupt[:-1] in tails else b''
                if tail:
                    path.write_bytes(row + tail)
                    self.assertIsNone(self.emit())
                path.write_bytes(before)
                self.assertIsNone(self.emit())
                self.assertEqual(self.floor(20)[0], 0)
                self.assertIsNone(self.emit('old'))
                self.assert_water(20)
                before += tail
                path.write_bytes(before)
                self.assertIsNotNone(self.emit())
                after = path.read_bytes()
                self.assertTrue(after.startswith(before))
                self.assertEqual(json.loads(after[len(before):])['seq'], 21)
                self.seq.unlink()

    def test_bad_history(self):
        path = self.box
        path.write_text('{"key":"old","seq":4}\n')
        receipt = self.acks
        for key in ({'bad': 'shape'}, ['bad']):
            receipt.write_text(json.dumps({'key': key, 'ref': 'fixture', 'attested': True}) + '\n')
            self.assert_blocked()
        receipt.unlink()
        for raw in ('{}\n', '{"key":"bad","seq":true}\n',
                    'garbage\n', '{"key":"old","seq":4}\ngarbage\n'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assert_blocked()
                self.assertEqual(path.read_text(), raw)
        path.unlink()
        self.acks.touch()
        self.assert_blocked()

    def test_floor_inputs(self):
        for kwargs in ({'approver': '  '}, {'reason': '  '}):
            self.assertNotEqual(self.floor(20, **kwargs)[0], 0)
            self.assertFalse(self.seq.exists())
        self.assertNotEqual(self.floor(-1)[0], 0)
        inside = SWARM.parents[3] / 'forbidden-seq-state'
        self.assertFalse(inside.exists())
        result = self.floor(20, directory=inside)
        self.assertNotEqual(result[0], 0)
        self.assertFalse(inside.exists())

    def test_floor_lease(self):
        self.assertEqual(self.floor(20)[0], 0)
        self.assertTrue(SS.acquire_lease(self.state)[0])
        try:
            result = subprocess.run([sys.executable, str(SWARM)] + self.floor_args(21), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('lock', result.stderr)
            self.assert_water(20)
        finally:
            SS.release_lease(self.state)


if __name__ == '__main__':
    unittest.main()
