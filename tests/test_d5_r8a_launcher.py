"""R8a component evidence, not callers' later M6 obligations."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import fcntl
import importlib.util
import json
import os
import select
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/hanig-orchestrate/scripts'))
import d5_process as P
import d5_process_launcher as L
import d5_protocol_locks as protocol
import d5_registry as registry
from tests.test_d5_locks import isolate_pool

CHILD = ROOT / 'tests/fixtures/d5/r8a_child.py'
BASE = '276e76af05a83846ea9c45ae7d030cf11a2e4e94'


class TestLauncher(unittest.TestCase):
    eq = unittest.TestCase.assertEqual
    def setUp(self):
        isolate_pool(self)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()

    def argv(self, action, *args):
        return [sys.executable, str(CHILD), action, *map(str, args)]

    def launch(self, action, *args, **options):
        handle = P.worker_launch(self.argv(action, *args), options=L.Options(**options))
        self.addCleanup(handle.close)
        return handle

    def await_file(self, path):
        deadline = time.monotonic() + 4
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(path.exists(), 'child barrier watchdog')

    def barrier(self, **options):
        ready, release = self.root / 'ready', self.root / 'release'
        handle = self.launch('barrier', ready, release, **options)
        self.await_file(ready)
        return handle, release

    def test_options_and_concurrent_isolation(self):
        def inspect(index):
            cwd = self.root / str(index)
            cwd.mkdir()
            handle = self.launch('inspect', 'literal;$(not-shell)', cwd=cwd,
                                 env={'R8A': str(index)}, new_session=True)
            self.eq(handle.wait(5), 0)
            data = json.loads(handle.read('stdout'))
            self.eq(data['cwd'], str(cwd))
            self.eq(data['env'].get('R8A'), str(index))
            self.assertNotIn('R8A_PARENT', data['env'])
            self.eq(data['session'], data['pid'])
            self.eq(data['args'], ['literal;$(not-shell)'])
            self.eq(handle.read('stderr'), b'error\n')
        before = os.getcwd()
        with mock.patch.dict(os.environ, {'R8A_PARENT': 'inherited'}):
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(inspect, (1, 2)))
            for env, expected in ((None, 'inherited'), ({}, None)):
                h = self.launch('inspect', env=env)
                self.eq(h.wait(5), 0)
                data = json.loads(h.read('stdout'))
                self.eq(data['env'].get('R8A_PARENT'), expected)
                self.eq(data['session'], os.getsid(0))
        self.eq(os.getcwd(), before)

    def test_launch_returns_at_barrier_and_timeout_is_not_completion(self):
        handle, release = self.barrier()
        self.assertIsNone(handle.poll(), 'launch waited for exit')
        with self.assertRaises(subprocess.TimeoutExpired):
            handle.wait(.01)
        self.assertIsNone(handle.poll())
        release.touch()
        self.eq(handle.wait(5), 0)
        self.eq(handle.wait(.01), 0)
        self.eq(handle.read('stdout'), b'released\n')
        handle.close()
        handle.close()
        with self.assertRaises(ValueError):
            handle.read('stdout')

    def test_both_streams_drain_before_wait_without_truncation(self):
        done = self.root / 'done'
        handle = self.launch('flood', done)
        self.await_file(done)  # Written AFTER > pipe capacity on both streams.
        self.eq(handle.wait(5), 0)
        for stream, byte in (('stdout', b'o'), ('stderr', b'e')):
            self.eq(handle.size(stream), 2 * 1024 * 1024)
            for offset in range(0, handle.size(stream), L.MAX_READ):
                self.eq(handle.read(stream, offset=offset, limit=L.MAX_READ),
                                 byte * L.MAX_READ)
            self.eq(handle.read(stream, offset=handle.size(stream)), b'')
            for bad in (-1, 0, L.MAX_READ + 1):
                with self.assertRaises(ValueError):
                    handle.read(stream, limit=bad)

    def test_logs_modes_stdin_and_ownership(self):
        path = self.root / 'output'
        path.write_bytes(b'prior\n')
        for mode, expected in (('ab', b'prior\ninputerror'), ('wb', b'inputerror')):
            h = self.launch('echo', stdin=b'input', stdout=L.Log(path, mode),
                            stderr=subprocess.STDOUT)
            self.eq(h.wait(5), 0)
            self.eq(path.read_bytes(), expected)
            with self.assertRaises(ValueError):
                h.read('stdout')
        with self.assertRaises(FileExistsError):
            self.launch('echo', stdout=L.Log(path, 'xb'))
        fresh = self.root / 'fresh'
        h = self.launch('echo', stdout=L.Log(fresh, 'xb'), stdin=subprocess.PIPE,
                        stderr=subprocess.DEVNULL)
        h.stdin.write(b'pipe input')
        h.close_stdin()
        self.assertTrue(h.stdin.closed)
        self.eq(h.wait(5), 0)
        self.eq(fresh.read_bytes(), b'pipe input')
        h = self.launch('echo', stdout=subprocess.DEVNULL)
        self.eq(h.wait(5), 0)  # DEVNULL stdin delivers EOF.

    def test_cancel_close_and_launch_failure_cleanup(self):
        h, _ = self.barrier(new_session=True)
        captures = list(h._captures.values())
        h.cancel()
        self.assertLess(h.wait(5), 0)
        self.eq(h.wait(), h.poll())
        h.close()
        self.assertTrue(all(f.closed for f in captures))
        files = []
        original = tempfile.TemporaryFile
        def opened(*args, **kwargs):
            f = original(*args, **kwargs)
            files.append(f)
            return f
        with mock.patch.object(L.tempfile, 'TemporaryFile', side_effect=opened):
            with self.assertRaises(FileNotFoundError):
                L.launch([str(self.root / 'absent')])
        self.eq(len(files), 2)
        self.assertTrue(all(f.closed for f in files))
        h = self.launch('barrier', self.root / 'r2', self.root / 'never', stdin=subprocess.PIPE)
        h.close()
        self.assertLess(h.poll(), 0)
        self.assertTrue(h.stdin.closed)

    def test_session_cancel_closes_descendant_writer(self):
        ready, fifo = self.root / 'ready', self.root / 'fifo'
        os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.stack.callback(os.close, fd)
        h = self.launch('group', ready, fifo, new_session=True)
        drained = False
        try:
            self.await_file(ready)
            self.eq(os.read(fd, 1), b'r')
            h.cancel()
            self.assertLess(h.wait(5), 0)
            readable, _, _ = select.select([fd], [], [], 3)
            self.eq(readable, [fd], 'descendant writer survived cancellation')
            self.eq(os.read(fd, 1), b'')
            drained = True
        finally:
            if not drained:
                try:
                    os.killpg(h.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_worker_options_refuse_descriptor_exports_before_logs(self):
        directory = self.root / 'record'
        directory.mkdir(mode=0o700)
        with protocol.pin(directory) as pinned, pinned.drain() as fd:
            os.set_inheritable(fd, True)
            h = self.launch('fd', fd)
            self.eq(h.wait(5), 0)
            self.assertNotEqual(json.loads(h.read('stdout')), [os.fstat(fd).st_dev, os.fstat(fd).st_ino])
            for name in ('stdin', 'stdout', 'stderr'):
                for value in (fd, None, os.fdopen(os.dup(fd), 'rb')):
                    if hasattr(value, 'close'):
                        self.stack.callback(value.close)
                    options = {'stdout': L.Log(self.root / 'must-not-open'), name: value}
                    with self.assertRaises(ValueError):
                        self.launch('mark', self.root / 'effect', **options)
            with self.assertRaises(TypeError):
                L.launch(self.argv('echo'), pass_fds=(fd,))
            self.assertFalse((self.root / 'must-not-open').exists())
            self.assertFalse((self.root / 'effect').exists())

    def test_private_handoff_cleanup_does_not_release_parent_drain(self):
        directory = self.root / 'record'
        directory.mkdir(mode=0o700)
        with protocol.pin(directory) as pinned, pinned.drain() as fd:
            h = L._launch(self.argv('barrier', self.root / 'r', self.root / 'x'),
                                 _protected_fd=fd)
            self.addCleanup(h.close)
            with self.assertRaises(subprocess.TimeoutExpired):
                h.wait(.01)
            h.close()
            probe = os.open(directory / 'authority.drain.lock', os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            os.fstat(fd)

    def test_protected_refusal_before_effects(self):
        self.assertFalse(registry.d5_active())
        self.eq(P.DESIGNATED_BOOTSTRAPS, frozenset())
        directory = self.root / 'record'
        directory.mkdir(mode=0o700)
        seen = []
        popen = subprocess.Popen
        def real_spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            seen.append(child)
            child.communicate(timeout=5)
            return child
        with protocol.pin(directory) as pinned, pinned.drain() as fd:
            with mock.patch.object(P.subprocess, 'Popen', side_effect=real_spawn):
                with self.assertRaisesRegex(P.AuthorityError, 'not designated'):
                    P.protected_spawn(directory, fd, str(CHILD), ('mark', str(self.root / 'effect')))
                with mock.patch.object(P, 'DESIGNATED_BOOTSTRAPS', frozenset([str(CHILD)])):
                    with pinned.start(), self.assertRaisesRegex(P.AuthorityError, 'inside START'):
                        P.protected_spawn(directory, fd, str(CHILD), ('mark', str(self.root / 'effect')))
        self.eq(seen, [])
        self.assertFalse((self.root / 'effect').exists())

    def test_legacy_defaults_equivalent_to_pinned_base(self):
        source = subprocess.check_output(['git', 'show', BASE + ':skills/hanig-orchestrate/scripts/d5_process.py'], cwd=ROOT)
        path = self.root / 'base_P.py'
        path.write_bytes(source)
        spec = importlib.util.spec_from_file_location('r8a_base', path)
        base = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(base)
        observed = []
        for spawn in (base.worker_spawn, P.worker_spawn):
            with spawn(self.argv('echo')) as child:
                output = child.communicate(b'literal input', timeout=5)
                observed.append((child.args, child.returncode, output))
        self.eq(observed[0], observed[1])
        h = self.launch('echo', stdin=b'literal input')
        self.eq(h.wait(5), observed[0][1])
        self.eq((h.read('stdout'), h.read('stderr')), observed[0][2])

    def test_invalid_options_no_files_or_processes(self):
        for options in (L.Options(stdout=L.Log(self.root / 'log', 'r')),
                        L.Options(env={'bad': 3}), L.Options(new_session=1)):
            with self.assertRaises(ValueError):
                L.launch(self.argv('mark', self.root / 'effect'), options=options)
        with self.assertRaises(TypeError):
            L.launch('echo shell text')
        self.eq(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
