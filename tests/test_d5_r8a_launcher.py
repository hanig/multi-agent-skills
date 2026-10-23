"""R8a component evidence, not callers' later M6 obligations."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import fcntl
import types
import json
import os
import select
import signal
from pathlib import Path
import subprocess as sp
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

    def done(self, h):
        self.eq(h.wait(5), 0)

    def argv(self, action, *args):
        return [sys.executable, str(CHILD), action, *map(str, args)]

    def launch(self, action, *args, **options):
        h = P.worker_launch(self.argv(action, *args), options=L.Options(**options))
        self.addCleanup(h.close)
        return h

    def await_file(self, path):
        deadline = time.monotonic() + 4
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(path.exists(), 'child barrier watchdog')

    def barrier(self, **options):
        ready, release = self.root / 'ready', self.root / 'release'
        h = self.launch('barrier', ready, release, **options)
        self.await_file(ready)
        return h, release

    def test_options_isolation(self):
        def inspect(i):
            cwd = self.root / str(i)
            cwd.mkdir()
            h = self.launch('inspect', 'literal;$(not-shell)', cwd=cwd,
                        env={'R8A': str(i)}, new_session=True)
            self.done(h)
            data = json.loads(h.read('stdout'))
            self.eq(data['cwd'], str(cwd))
            self.eq(data['env'].get('R8A'), str(i))
            self.assertNotIn('R8A_PARENT', data['env'])
            self.eq(data['session'], data['pid'])
            self.eq(data['args'], ['literal;$(not-shell)'])
            self.eq(h.read('stderr'), b'error\n')
        before = os.getcwd()
        with mock.patch.dict(os.environ, {'R8A_PARENT': 'inherited'}):
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(inspect, (1, 2)))
            for env, want in ((None, 'inherited'), ({}, None)):
                h = self.launch('inspect', env=env)
                self.done(h)
                data = json.loads(h.read('stdout'))
                self.eq(data['env'].get('R8A_PARENT'), want)
                self.eq(data['session'], os.getsid(0))
        self.eq(os.getcwd(), before)

    def test_async_timeout(self):
        h, release = self.barrier()
        self.assertIsNone(h.poll(), 'launch waited for exit')
        with self.assertRaises(sp.TimeoutExpired):
            h.wait(.01)
        self.assertIsNone(h.poll())
        release.touch()
        self.done(h)
        self.eq(h.wait(.01), 0)
        self.eq(h.read('stdout'), b'released\n')
        h.close()
        h.close()
        with self.assertRaises(ValueError):
            h.read('stdout')

    def test_capture_before_wait(self):
        done = self.root / 'done'
        h = self.launch('flood', done)
        self.await_file(done)
        self.done(h)
        for stream, byte in (('stdout', b'o'), ('stderr', b'e')):
            self.eq(h.size(stream), 2 * 1024 * 1024)
            for offset in range(0, h.size(stream), L.MAX_READ):
                self.eq(h.read(stream, offset=offset, limit=L.MAX_READ),
                        byte * L.MAX_READ)
            self.eq(h.read(stream, offset=h.size(stream)), b'')
            for bad in (-1, 0, L.MAX_READ + 1):
                with self.assertRaises(ValueError):
                    h.read(stream, limit=bad)

    def test_logs_stdin(self):
        path = self.root / 'output'
        path.write_bytes(b'prior\n')
        for mode, want in (('ab', b'prior\ninputerror'), ('wb', b'inputerror')):
            h = self.launch('echo', stdin=b'input', stdout=L.Log(path, mode),
                            stderr=sp.STDOUT)
            self.done(h)
            self.eq(path.read_bytes(), want)
            with self.assertRaises(ValueError):
                h.read('stdout')
        with self.assertRaises(FileExistsError):
            self.launch('echo', stdout=L.Log(path, 'xb'))
        fresh = self.root / 'fresh'
        h = self.launch('echo', stdout=L.Log(fresh, 'xb'), stdin=sp.PIPE,
                        stderr=sp.DEVNULL)
        h.stdin.write(b'pipe input')
        h.close_stdin()
        self.assertTrue(h.stdin.closed)
        self.done(h)
        self.eq(fresh.read_bytes(), b'pipe input')
        h = self.launch('echo', stdout=sp.DEVNULL)
        self.done(h)

    def test_cleanup(self):
        h, _ = self.barrier(new_session=True)
        caps = list(h._files.values())
        h.cancel()
        self.assertLess(h.wait(5), 0)
        self.eq(h.wait(), h.poll())
        h.close()
        self.assertTrue(all(f.closed for f in caps))
        files = []
        real = tempfile.TemporaryFile
        def opened(*args, **kwargs):
            f = real(*args, **kwargs)
            files.append(f)
            return f
        with mock.patch.object(L.tempfile, 'TemporaryFile', side_effect=opened):
            with self.assertRaises(FileNotFoundError):
                L.launch([str(self.root / 'absent')])
        self.eq(len(files), 2)
        self.assertTrue(all(f.closed for f in files))
        h = self.launch('barrier', self.root / 'r2', self.root / 'never', stdin=sp.PIPE)
        h.close()
        self.assertLess(h.poll(), 0)
        self.assertTrue(h.stdin.closed)

    def test_cancel_group(self):
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
                    if ready.exists() and os.getpgid(int(ready.read_text())) == h.pid:
                        os.killpg(h.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_worker_fds(self):
        d = self.root / 'record'
        d.mkdir(mode=0o700)
        with protocol.pin(d) as pinned, pinned.drain() as fd:
            os.set_inheritable(fd, True)
            h = self.launch('fd', fd)
            self.done(h)
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

    def cancel_lock(self, state):
        def until(check):
            deadline = time.monotonic() + 4
            while not check():
                self.assertLess(time.monotonic(), deadline, 'lock/exit watchdog')
                time.sleep(.01)
        lock, ready = self.root / 'lock', self.root / 'ready'
        h = self.launch('lock', lock, ready, state, new_session=True)
        def cleanup():
            if ready.exists():
                try:
                    if os.getpgid(int(ready.read_text())) == h.pid:
                        os.killpg(h.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup)
        self.await_file(ready)
        self.eq(os.getpgid(int(ready.read_text())), h.pid)
        with lock.open('r+') as probe:
            def unlocked():
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return True
            self.assertFalse(unlocked())
            if state != 'live':
                until(lambda: sp.check_output(['ps', '-o', 'stat=', '-p',
                                     str(h.pid)]).strip().startswith(b'Z'))
                if state == 'reaped':
                    self.done(h)
            with mock.patch.object(L.os, 'killpg', wraps=os.killpg) as sent:
                h.cancel()
            if state == 'live':
                self.assertLess(h.wait(5), 0)
                until(unlocked)
            else:
                sent.assert_not_called()
                self.assertFalse(unlocked())

    def test_cancel_live_lock(self): self.cancel_lock('live')

    def test_cancel_unreaped_lock(self): self.cancel_lock('exited')

    def test_cancel_reaped_lock(self): self.cancel_lock('reaped')

    def test_log_fd_alias(self):
        d = self.root / 'record'
        d.mkdir(mode=0o700)
        with protocol.pin(d) as p, p.drain() as fd:
            alias = self.root / 'alias'
            alias.symlink_to('/dev/fd/' + str(fd))
            for path in ('/dev/fd/' + str(fd), alias):
                with self.assertRaises((ValueError, OSError)):
                    self.launch('echo', stdout=L.Log(path))

    def test_parent_custody(self):
        d = self.root / 'record'
        d.mkdir(mode=0o700)
        with protocol.pin(d) as pinned, pinned.drain() as fd:
            h = L._launch(self.argv('barrier', self.root / 'r', self.root / 'x'),
                        _protected_fd=fd)
            self.addCleanup(h.close)
            with self.assertRaises(sp.TimeoutExpired):
                h.wait(.01)
            h.close()
            probe = os.open(d / 'authority.drain.lock', os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            os.fstat(fd)

    def test_protected_refusal(self):
        self.assertFalse(registry.d5_active())
        self.eq(P.DESIGNATED_BOOTSTRAPS, frozenset())
        d = self.root / 'record'
        d.mkdir(mode=0o700)
        seen = []
        popen = sp.Popen
        def real_spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            seen.append(child)
            child.communicate(timeout=5)
            return child
        with protocol.pin(d) as pinned, pinned.drain() as fd:
            with mock.patch.object(P.subprocess, 'Popen', side_effect=real_spawn):
                with self.assertRaisesRegex(P.AuthorityError, 'not designated'):
                    P.protected_spawn(d, fd, str(CHILD), ('mark', str(self.root / 'effect')))
                with mock.patch.object(P, 'DESIGNATED_BOOTSTRAPS', frozenset([str(CHILD)])):
                    with pinned.start(), self.assertRaisesRegex(P.AuthorityError, 'inside START'):
                        P.protected_spawn(d, fd, str(CHILD), ('mark', str(self.root / 'effect')))
        self.eq(seen, [])
        self.assertFalse((self.root / 'effect').exists())

    def test_legacy_defaults(self):
        source = sp.check_output(['git', 'show', BASE + ':skills/hanig-orchestrate/scripts/d5_process.py'], cwd=ROOT)
        base = types.ModuleType('base')
        exec(compile(source, 'base', 'exec'), base.__dict__)
        observed = []
        for spawn in (base.worker_spawn, P.worker_spawn):
            with spawn(self.argv('echo')) as child:
                output = child.communicate(b'literal input', timeout=5)
                observed.append((child.args, child.returncode, output))
        self.eq(observed[0], observed[1])
        h = self.launch('echo', stdin=b'literal input')
        self.eq(h.wait(5), observed[0][1])
        self.eq((h.read('stdout'), h.read('stderr')), observed[0][2])

    def test_shared_truncating_log(self):
        path = self.root / 'log'
        path.touch()
        alias = self.root / 'alias'
        os.link(path, alias)
        for target in (path, alias):
            for out, err, prefix in (('wb', 'wb', b''), ('wb', 'ab', b''),
                            ('ab', 'wb', b''), ('ab', 'ab', b'prior')):
                path.write_bytes(b'prior')
                h = self.launch('echo', stdin=b'abcde', stdout=L.Log(path, out),
                                stderr=L.Log(target, err))
                self.done(h)
                self.eq(path.read_bytes(), prefix + b'abcdeerror')

    @unittest.skipIf(os.geteuid() == 0, 'requires ordinary directory permissions')
    def test_search_only_log_directory(self):
        d = self.root / 'drop'
        d.mkdir()
        path = d / 'log'
        path.write_bytes(b'control')
        d.chmod(0o300)
        self.addCleanup(d.chmod, 0o700)
        with path.open('ab') as f:
            f.write(b'append')
        h = self.launch('echo', stdin=b'child', stdout=L.Log(path))
        self.done(h)
        self.eq(path.read_bytes(), b'controlappendchild')

    def test_missing_descriptor_namespace(self):
        real_stat = os.stat
        def stat(path, *args, **kwargs):
            if path == '/dev/fd':
                raise FileNotFoundError(path)
            return real_stat(path, *args, **kwargs)
        path = self.root / 'log'
        with mock.patch.object(L.os, 'stat', side_effect=stat):
            with path.open('ab') as f:
                f.write(b'control')
            h = self.launch('echo', stdin=b'child', stdout=L.Log(path))
            self.done(h)
        self.eq(path.read_bytes(), b'controlchild')

    def test_invalid_options(self):
        for options in (L.Options(stdout=L.Log(self.root / 'log', 'r')),
                        L.Options(env={'bad': 3}), L.Options(new_session=1)):
            with self.assertRaises(ValueError):
                L.launch(self.argv('mark', self.root / 'effect'), options=options)
        with self.assertRaises(TypeError):
            L.launch('echo shell text')
        self.eq(list(self.root.iterdir()), [])

if __name__ == '__main__':
    unittest.main()
