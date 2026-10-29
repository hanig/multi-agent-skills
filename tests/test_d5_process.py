"""R8 exec, barrier and nonblocking-flock tests."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import fcntl
import errno
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
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
LOW_CONTROL = 'D5 control descriptors must be above 2; duplicate before constructing child argv'


def designated(bootstrap=BOOTSTRAP):
    return mock.patch.object(process, 'DESIGNATED_BOOTSTRAPS', frozenset([bootstrap]))


def spawn_spy():
    return mock.patch.object(process.subprocess, 'Popen')


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
    eq = unittest.TestCase.assertEqual

    def setUp(self):
        from tests.d5_test_support import for_case
        for_case(self)
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

    def worker(self, *args):
        return self.own(process.worker_spawn([sys.executable, PEER, *args]))

    def event(self, proc, name):
        ready, _, _ = select.select([proc.stdout], [], [], 20)
        self.assertTrue(ready, 'child watchdog: ' + name)
        raw = proc.stdout.readline()
        self.assertTrue(raw, 'child exited before ' + name)
        event = json.loads(raw)
        self.eq(event['event'], name, event)
        return event

    def send(self, proc, word):
        proc.stdin.write((word + '\n').encode())
        proc.stdin.flush()

    def finish(self, proc, error_stream=b''):
        self.send(proc, 'exit')
        stdout, stderr = proc.communicate(timeout=20)
        self.eq((proc.returncode, stdout, stderr), (0, b'', error_stream))

    def setup_effect(self):
        completed = subprocess.run([sys.executable, PEER, 'setup', str(self.root)],
            capture_output=True, check=True, timeout=30)
        self.eq(completed.stderr, b'')
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
        with designated(bootstrap):
            return self.own(process.protected_spawn(self.directory, fd, bootstrap,
                ['child', json.dumps(self.data), str(fd), 'submitted'], **kwargs))

    def complete_effect(self, proc):
        self.send(proc, 'request')
        result = self.event(proc, 'returned')
        self.eq(result['error'], 'D5 readiness: caller-held coordinator lease required')
        self.eq(result['calls'], [])
        self.eq(list((self.directory / 'authority.effects').glob('*.json')), [])
        self.assertTrue(result['retained'])

    def test_protected_spawn_descriptor_allowlist(self):
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
        with mock.patch.object(process.subprocess, 'Popen', wraps=subprocess.Popen) as launched:
            proc = self.launch(fd, pass_fds=(read,))
        adopted = self.event(proc, 'adopted')
        self.assertFalse(adopted['inheritable'])
        for n in (fd, read):
            self.eq(adopted['observed'][str(n)], before[str(n)])
        for n in forbidden:
            self.assertNotEqual(adopted['observed'][str(n)], before[str(n)])
        self.assertIs(launched.call_args.kwargs['close_fds'], True)
        self.eq(launched.call_args.kwargs['pass_fds'], (fd, read))
        self.complete_effect(proc)
        self.finish(proc)

    def test_parent_cleanup_preserves_child_drain(self):
        self.setup_effect()
        _, fd = self.holding()
        proc = self.launch(fd, bootstrap='tests.fixtures.d5.r7_child')
        self.event(proc, 'adopted')
        self.stack.close()
        self.eq(self.probe(), 'blocked')
        self.complete_effect(proc)
        self.eq(self.probe(), 'blocked')
        self.finish(proc)
        self.eq(self.probe(), 'available')

    def test_live_worker_does_not_hold_drain(self):
        _, fd = self.holding()
        alias = os.dup(fd)
        self.stack.callback(os.close, alias)
        os.set_inheritable(fd, True)
        os.set_inheritable(alias, True)
        before = {str(n): identity(n) for n in (fd, alias)}
        proc = self.worker('worker', json.dumps([fd, alias]))
        ready = self.event(proc, 'ready')
        for n in before:
            self.assertNotEqual(ready['observed'][n], before[n])
        self.stack.close()
        self.assertIsNone(proc.poll())
        self.eq(self.probe(), 'available')
        self.finish(proc)

    def test_failed_spawn_releases_parent_reference(self):
        _, fd = self.holding()
        before = descriptors()
        imported = []
        duplicate = os.dup
        def observe(original):
            result = duplicate(original)
            imported.append(result)
            return result
        with designated(), \
                mock.patch.object(os, 'dup', side_effect=observe), \
                mock.patch.object(process.sys, 'executable', str(self.root / 'absent')):
            with self.assertRaises(FileNotFoundError):
                process.protected_spawn(self.directory, fd, BOOTSTRAP)
        self.eq(len(imported), 1)
        self.eq(descriptors(), before)
        self.eq(self.probe(), 'blocked')
        self.stack.close()
        self.eq(self.probe(), 'available')

    def test_protected_spawn_inside_start_refused(self):
        with protocol.pin(self.directory) as record:
            for held in (True, False):
                with ExitStack() as stack:
                    fd = stack.enter_context(record.drain()) if held else -1
                    stack.enter_context(record.start())
                    before = descriptors()
                    with designated(), spawn_spy() as popen:
                        with self.assertRaisesRegex(process.AuthorityError,
                                '^D5 protected launch inside START is forbidden$'):
                            process.protected_spawn(self.directory, fd, BOOTSTRAP)
                        popen.assert_not_called()
                    self.eq(descriptors(), before)

    def test_unlisted_bootstrap_refused(self):
        self.eq(process.DESIGNATED_BOOTSTRAPS, frozenset())
        self.assertIsInstance(process.DESIGNATED_BOOTSTRAPS, frozenset)
        _, fd = self.holding()
        before = descriptors()
        with spawn_spy() as popen:
            with self.assertRaisesRegex(process.AuthorityError, 'not designated'):
                process.protected_spawn(self.directory, fd, BOOTSTRAP)
            popen.assert_not_called()
        self.eq(descriptors(), before)

    def test_standard_descriptor_collision_refused(self):
        proc = self.worker('standard-slots', str(self.directory))
        for standard in (0, 1, 2):
            event = self.event(proc, 'standard-slot')
            self.eq(event['standard'], standard)
            self.eq(event['error'], LOW_CONTROL)
            self.assertFalse(event['spawned'])
            self.assertTrue(event['unchanged'])
            self.eq(event['implicit'], 'D5 communication descriptor is a protocol lock')
            self.assertTrue(self.event(proc, 'control-slot')['refused'])
        stdout, stderr = proc.communicate(timeout=20)
        self.eq((proc.returncode, stdout, stderr), (0, b'', b''))

    def test_duplicated_control_with_aliased_stderr(self):
        self.setup_effect()
        _, fd = self.holding()
        control = fcntl.fcntl(2, fcntl.F_DUPFD_CLOEXEC, 3)
        self.stack.callback(os.close, control)
        alias = self.stack.enter_context(os.fdopen(os.dup(2), 'wb'))
        self.data['inspect_fds'] = [control, 2]
        proc = self.launch(fd, pass_fds=(control,), stderr=alias)
        observed = self.event(proc, 'adopted')['observed']
        self.eq(observed[str(control)], identity(control))
        self.eq(observed['2'], identity(alias.fileno()))
        self.complete_effect(proc)
        self.finish(proc, None)

    def test_peer_start_allows_launch_but_blocks_child_admission(self):
        self.setup_effect()
        ready, release = threading.Event(), threading.Event()
        def hold():
            with protocol.pin(self.directory) as record, record.start():
                ready.set()
                self.assertTrue(release.wait(20), 'release watchdog')
        with ThreadPoolExecutor(1) as pool:
            peer = pool.submit(hold)
            try:
                self.assertTrue(ready.wait(20))
                _, fd = self.holding()
                self.data['probe_start'] = True
                proc = self.launch(fd)
                self.event(proc, 'adopted')
                self.send(proc, 'request')
                result = self.event(proc, 'returned')
                self.eq(result['error'], 'D5 readiness: caller-held coordinator lease required')
                self.eq(result['calls'], [])
                self.assertTrue(result['retained'])
                self.eq(list((self.directory / 'authority.effects').glob('*.json')), [])
                self.assertFalse(release.is_set())
            finally:
                release.set()
            self.assertIsNone(peer.result(timeout=20))
        self.finish(proc)

    def test_closed_inherited_streams_allow_valid_effect(self):
        for mode in ('closed-streams', 'closed-streams-low'):
            result = subprocess.run([sys.executable, PEER, mode, str(self.directory)],
                                    capture_output=True, timeout=60)
            self.eq(result.returncode, 0, result.stderr.decode())
            self.eq(json.loads(result.stdout)['event'], 'closed-streams-completed')
            self.eq(result.stderr, b'')

    def test_descriptor_inspection_errors_are_not_hidden(self):
        _, fd = self.holding()
        real_fstat = os.fstat
        for number, error, kwargs in ((1000000, errno.EBADF, {'pass_fds': (1000000,)}),
                                     (2, errno.EIO, {'stderr': None})):
            def stat(n):
                if n == number:
                    raise OSError(error, 'fixture inspection')
                return real_fstat(n)
            with designated(), \
                    mock.patch.object(os, 'fstat', side_effect=stat), \
                    spawn_spy() as popen:
                with self.assertRaises(OSError) as caught:
                    process.protected_spawn(self.directory, fd, BOOTSTRAP, **kwargs)
                self.eq(caught.exception.errno, error)
                popen.assert_not_called()

    def test_protocol_locks_cannot_be_communication(self):
        _, drain = self.holding()
        for fd in (drain, self.opened('authority.start.lock'), self.opened('authority.lock')):
            alias = os.dup(fd)
            self.stack.callback(os.close, alias)
            for candidate in (fd, alias):
                for kw in ({'pass_fds': (candidate,)}, {'stdin': candidate},
                           {'stdout': candidate}, {'stderr': candidate}):
                    with self.subTest(fd=candidate, kw=kw), \
                            designated(), \
                            spawn_spy() as popen:
                        before = descriptors()
                        with self.assertRaisesRegex(process.AuthorityError, 'communication descriptor'):
                            process.protected_spawn(self.directory, drain, BOOTSTRAP, **kw)
                        popen.assert_not_called()
                        self.eq(descriptors(), before)

    def test_M5_protected_child_survives_ancestor_until_accounted(self):
        self.setup_effect()
        ancestor = self.worker('ancestor', json.dumps(self.data))
        self.event(ancestor, 'adopted')
        ancestor.kill()
        ancestor.wait(timeout=20)
        self.assertLess(ancestor.returncode, 0)
        self.send(ancestor, 'request')
        result = self.event(ancestor, 'returned')
        self.eq(result['error'], 'D5 readiness: caller-held coordinator lease required')
        self.eq(result['calls'], [])
        self.assertTrue(result['retained'])
        self.eq(list((self.directory / 'authority.effects').glob('*.json')), [])
        installer = self.worker('installer', json.dumps(self.data))
        self.assertTrue(self.event(installer, 'exclusive-probe')['blocked'])
        self.eq(self.probe(), 'blocked')
        self.send(ancestor, 'exit')
        self.event(installer, 'installed')
        stdout, stderr = installer.communicate(timeout=20)
        self.eq((installer.returncode, stdout, stderr), (0, b'', b''))
        stdout, stderr = ancestor.communicate(timeout=20)
        self.eq((stdout, stderr), (b'', b''))
        self.eq(json.loads((self.directory / 'authority.json').read_bytes())['grant_id'], 'b' * 32)


if __name__ == '__main__':
    unittest.main()
