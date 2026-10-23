"""R8 consumer tests with real execs, pipe barriers and nonblocking flock."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/hanig-orchestrate/scripts'))
import d5_process as process
import d5_protocol_locks as protocol
import d5_registry as registry
from tests.test_d5_locks import isolate_pool

BOOTSTRAP = str(ROOT / 'tests/fixtures/d5/r7_child.py')
PEER = str(ROOT / 'tests/fixtures/d5/r8_peer.py')


def identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def descriptors():
    result = {}
    for name in os.listdir('/dev/fd'):
        try:
            result[int(name)] = identity(int(name))
        except OSError:
            pass  # The directory iterator's own descriptor has already closed.
    return result


class TestProcess(unittest.TestCase):
    def setUp(self):
        isolate_pool(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / 'record'
        self.directory.mkdir(mode=0o700)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def own(self, proc):
        def cleanup():
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
                proc.stdin = None
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=20)
        self.addCleanup(cleanup)
        return proc

    def event(self, proc, name):
        ready, _, _ = select.select([proc.stdout], [], [], 20)
        self.assertTrue(ready, 'child watchdog: ' + name)
        raw = proc.stdout.readline()
        self.assertTrue(raw, 'child exited before ' + name)
        event = json.loads(raw)
        self.assertEqual(event['event'], name, event)
        return event

    def send(self, proc, word):
        proc.stdin.write((word + '\n').encode())
        proc.stdin.flush()

    def finish(self, proc):
        self.send(proc, 'exit')
        stdout, stderr = proc.communicate(timeout=20)
        self.assertEqual((proc.returncode, stdout, stderr), (0, b'', b''))

    def setup_effect(self):
        completed = subprocess.run([sys.executable, PEER, 'setup', str(self.root)],
            capture_output=True, check=True, timeout=30)
        self.assertEqual(completed.stderr, b'')
        self.data = json.loads(completed.stdout)['data']
        self.directory = Path(self.data['directory'])
        self.assertFalse(registry.d5_active())

    def holding(self):
        record = self.stack.enter_context(protocol.pin(self.directory))
        fd = self.stack.enter_context(record.drain())
        return record, fd

    def opened(self, name):
        fd = os.open(self.directory / name, os.O_RDWR | os.O_CREAT, 0o600)
        self.stack.callback(os.close, fd)
        return fd

    def probe(self):
        fd = os.open(self.directory / 'authority.drain.lock', os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'blocked'
            return 'available'
        finally:
            os.close(fd)

    def launch(self, fd, bootstrap=BOOTSTRAP, **kwargs):
        with mock.patch.object(process, 'DESIGNATED_BOOTSTRAPS', frozenset([bootstrap])):
            return self.own(process.protected_spawn(self.directory, fd, bootstrap,
                ['child', json.dumps(self.data), str(fd), 'submitted'], **kwargs))

    def complete_effect(self, proc):
        self.send(proc, 'request')
        self.event(proc, 'waiting')
        self.send(proc, 'finish')
        result = self.event(proc, 'returned')
        self.assertIsNone(result['error'])
        self.assertEqual(result['calls'], ['start', 'finish'])
        self.assertFalse(result['retained'])

    def test_protected_spawn_descriptor_allowlist(self):
        # R8-PASS-START / R8-PASS-STATE / R8-CLOSE-FDS: observe actual child fds.
        self.setup_effect()
        record, fd = self.holding()
        start = self.opened('authority.start.lock')
        state = self.opened('authority.lock')
        unrelated = self.opened('unrelated')
        read, write = os.pipe()
        self.stack.callback(os.close, read)
        self.stack.callback(os.close, write)
        forbidden = (start, state, unrelated, record._record.fd, write)
        for extra in forbidden:
            os.set_inheritable(extra, True)
        before = {str(n): identity(n) for n in (fd, read) + forbidden}
        self.data['inspect_fds'] = list(map(int, before))
        proc = self.launch(fd, pass_fds=(read,))
        adopted = self.event(proc, 'adopted')
        self.assertFalse(adopted['inheritable'])
        for n in (fd, read):
            self.assertEqual(adopted['observed'][str(n)], before[str(n)])
        for n in forbidden:
            self.assertNotEqual(adopted['observed'][str(n)], before[str(n)])
        self.complete_effect(proc)
        self.finish(proc)

    def test_parent_cleanup_preserves_child_drain(self):
        # R8-PARENT-UNLOCK; also exercise the designated -m module form.
        self.setup_effect()
        _, fd = self.holding()
        proc = self.launch(fd, bootstrap='tests.fixtures.d5.r7_child')
        self.event(proc, 'adopted')
        self.stack.close()
        self.assertEqual(self.probe(), 'blocked')
        self.complete_effect(proc)
        self.assertEqual(self.probe(), 'available')
        self.finish(proc)

    def test_live_worker_does_not_hold_drain(self):
        # U3-M7 / R8-PASS-DRAIN-TO-WORKER: ready worker stays at pipe barrier.
        _, fd = self.holding()
        alias = os.dup(fd)
        self.stack.callback(os.close, alias)
        os.set_inheritable(fd, True)
        os.set_inheritable(alias, True)
        before = {str(n): identity(n) for n in (fd, alias)}
        proc = self.own(process.worker_spawn([sys.executable, PEER, 'worker',
                                             json.dumps([fd, alias])]))
        ready = self.event(proc, 'ready')
        for n in before:
            self.assertNotEqual(ready['observed'][n], before[n])
        self.stack.close()
        self.assertIsNone(proc.poll())
        self.assertEqual(self.probe(), 'available')
        self.finish(proc)

    def test_failed_spawn_releases_parent_reference(self):
        # R8-LEAK-FAILED-SPAWN: real Popen failure after R1 imports its duplicate.
        _, fd = self.holding()
        before = descriptors()
        imported = []
        duplicate = os.dup
        local_os = mock.Mock(wraps=os)
        def observe(original):
            result = duplicate(original)
            imported.append(result)
            return result
        local_os.dup.side_effect = observe
        with mock.patch.object(process, 'DESIGNATED_BOOTSTRAPS', frozenset([BOOTSTRAP])), \
                mock.patch.object(protocol.locks, 'os', local_os), \
                mock.patch.object(process.sys, 'executable', str(self.root / 'absent')):
            with self.assertRaises(FileNotFoundError):
                process.protected_spawn(self.directory, fd, BOOTSTRAP)
        self.assertEqual(len(imported), 1)
        self.assertEqual(descriptors(), before)
        self.assertEqual(self.probe(), 'blocked')
        self.stack.close()
        self.assertEqual(self.probe(), 'available')

    def test_protected_spawn_inside_start_refused(self):
        # R8-SPAWN-UNDER-START: with and without a preexisting shared holder.
        with protocol.pin(self.directory) as record:
            for held in (False, True):
                with ExitStack() as stack:
                    fd = stack.enter_context(record.drain()) if held else -1
                    stack.enter_context(record.start())
                    before = descriptors()
                    with mock.patch.object(process, 'DESIGNATED_BOOTSTRAPS', frozenset([BOOTSTRAP])), \
                            mock.patch.object(process.subprocess, 'Popen') as popen:
                        with self.assertRaisesRegex(process.AuthorityError,
                                '^D5 protected launch inside START is forbidden$'):
                            process.protected_spawn(self.directory, fd, BOOTSTRAP)
                        popen.assert_not_called()
                    self.assertEqual(descriptors(), before)

    def test_unlisted_bootstrap_refused(self):
        # R8-ACCEPT-UNLISTED-BOOTSTRAP: no process, no extra reference.
        self.assertEqual(process.DESIGNATED_BOOTSTRAPS, frozenset())
        self.assertIsInstance(process.DESIGNATED_BOOTSTRAPS, frozenset)
        _, fd = self.holding()
        before = descriptors()
        with mock.patch.object(process.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(process.AuthorityError, 'not designated'):
                process.protected_spawn(self.directory, fd, BOOTSTRAP)
            popen.assert_not_called()
        self.assertEqual(descriptors(), before)

    def test_protocol_locks_cannot_be_communication(self):
        # R8-ACCEPT-LOCK-AS-COMM: direct and dup aliases, explicit/stdio paths.
        _, drain = self.holding()
        for fd in (drain, self.opened('authority.start.lock'), self.opened('authority.lock')):
            alias = os.dup(fd)
            self.stack.callback(os.close, alias)
            for candidate in (fd, alias):
                for kw in ({'pass_fds': (candidate,)}, {'stdin': candidate},
                           {'stdout': candidate}, {'stderr': candidate}):
                    with self.subTest(fd=candidate, kw=kw), \
                            mock.patch.object(process, 'DESIGNATED_BOOTSTRAPS', frozenset([BOOTSTRAP])), \
                            mock.patch.object(process.subprocess, 'Popen') as popen:
                        before = descriptors()
                        with self.assertRaisesRegex(process.AuthorityError, 'communication descriptor'):
                            process.protected_spawn(self.directory, drain, BOOTSTRAP, **kw)
                        popen.assert_not_called()
                        self.assertEqual(descriptors(), before)

    def test_M5_protected_child_survives_ancestor_until_accounted(self):
        self.setup_effect()
        ancestor = self.own(process.worker_spawn([sys.executable, PEER, 'ancestor',
                                                 json.dumps(self.data)]))
        self.event(ancestor, 'adopted')
        self.send(ancestor, 'request')
        self.event(ancestor, 'waiting')
        ancestor.kill()
        ancestor.wait(timeout=20)
        self.assertLess(ancestor.returncode, 0)
        installer = self.own(process.worker_spawn([sys.executable, PEER, 'installer',
                                                  json.dumps(self.data)]))
        self.assertTrue(self.event(installer, 'exclusive-probe')['blocked'])
        self.send(ancestor, 'finish')
        self.event(ancestor, 'closing')
        self.assertEqual(self.probe(), 'blocked')
        self.send(ancestor, 'close')
        self.assertIsNone(self.event(ancestor, 'returned')['error'])
        self.event(installer, 'installed')
        stdout, stderr = installer.communicate(timeout=20)
        self.assertEqual((installer.returncode, stdout, stderr), (0, b'', b''))
        self.send(ancestor, 'exit')
        stdout, stderr = ancestor.communicate(timeout=20)
        self.assertEqual((stdout, stderr), (b'', b''))
        self.assertEqual(json.loads((self.directory / 'authority.json').read_bytes())['grant_id'], 'b' * 32)


if __name__ == '__main__':
    unittest.main()
