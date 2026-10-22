"""U3a primitives, real flock/descriptor tests, and named mutation targets.

Ordering uses pipes/events or lock-entry hooks. Timeouts only fail tests and
reap fixture processes: they never release or steal an operational lock.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_locks as locks

PEER = ROOT / "tests/fixtures/d5/lock_peer.py"


class TestD5Locks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.other = self.directory / "other"
        self.other.mkdir()

    def refused(self, text):
        return self.assertRaisesRegex(locks.LockError, "^" + text + "$")

    def peer(self, action, *args, pass_fds=()):
        proc = subprocess.Popen([sys.executable, str(PEER), action,
                                 str(self.directory), *map(str, args)],
                                pass_fds=pass_fds, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)

        self.addCleanup(cleanup)
        return proc

    def line(self, proc):
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        self.assertTrue(ready, "peer watchdog: no pipe message")
        return proc.stdout.readline().decode().rstrip("\n")

    def release(self, proc):
        proc.stdin.write(b"release\n")
        proc.stdin.flush()
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, (stdout, stderr))

    def available(self, name):
        # A fresh description is deliberately used only by this external probe.
        fd = os.open(self.directory / name, os.O_RDWR)
        try:
            try:
                locks.fcntl.flock(fd, locks.fcntl.LOCK_EX | locks.fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True
        finally:
            os.close(fd)

    def test_paths_flags_modes_permanence_and_exception_release(self):
        opened, modes = [], []
        real_open, real_flock = os.open, locks.fcntl.flock

        def opening(path, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            opened.append((path, flags, mode, dir_fd, fd))
            self.assertFalse(os.get_inheritable(fd))
            return fd

        def flock(fd, mode):
            modes.append(mode)
            return real_flock(fd, mode)

        with mock.patch.object(os, "open", side_effect=opening), \
                mock.patch.object(locks.fcntl, "flock", side_effect=flock):
            with locks.pin(self.directory) as record:
                with record.drain(), record.start(), record.state():
                    for name in locks._NAMES.values():
                        self.assertFalse(self.available(name))
                with record.drain(shared=False):
                    pass
        pin_open = opened[0]
        self.assertEqual(pin_open[:4], (self.directory, os.O_RDONLY | os.O_DIRECTORY |
                                       os.O_CLOEXEC | os.O_NOFOLLOW, 0o777, None))
        lock_opens = [row for row in opened if row[3] is not None]
        self.assertEqual([row[0] for row in lock_opens],
                         ["authority.drain.lock", "authority.start.lock",
                          "authority.lock", "authority.drain.lock"])
        for _, flags, mode, dir_fd, _ in lock_opens:
            self.assertEqual(flags, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW)
            self.assertEqual(mode, 0o600)
            self.assertEqual(dir_fd, pin_open[4])
        self.assertEqual([m for m in modes if not m & locks.fcntl.LOCK_NB],
                         [locks.fcntl.LOCK_SH] + [locks.fcntl.LOCK_EX] * 3)
        identities = {}
        for name in locks._NAMES.values():
            path = self.directory / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.write_bytes(b"permanent")
            identities[name] = path.stat().st_ino
        with locks.pin(self.directory) as record:
            with self.assertRaisesRegex(ValueError, "fixture"):
                with record.drain(), record.start(), record.state():
                    raise ValueError("fixture")
        for name, inode in identities.items():
            self.assertTrue(self.available(name))
            self.assertEqual((self.directory / name).stat().st_ino, inode)
            self.assertEqual((self.directory / name).read_bytes(), b"permanent")

    def test_L1_symlink_nonregular_and_unsafe_aliases_refused(self):
        target = self.directory / "target"
        target.write_bytes(b"untouched")
        for rank, name in locks._NAMES.items():
            for kind in ("symlink", "fifo", "directory", "hardlink", "writable"):
                with self.subTest(rank=rank, kind=kind):
                    path = self.directory / name
                    if kind == "symlink":
                        path.symlink_to(target)
                    elif kind == "fifo":
                        os.mkfifo(path)
                    elif kind == "directory":
                        path.mkdir()
                    elif kind == "hardlink":
                        os.link(target, path)
                    else:
                        path.write_bytes(b"")
                        path.chmod(0o666)
                    try:
                        with locks.pin(self.directory) as record:
                            guard = (record.drain, record.start, record.state)[rank]
                            with self.refused(locks._UNSAFE), guard():
                                self.fail("unsafe acquisition entered")
                    finally:
                        path.rmdir() if kind == "directory" else path.unlink()
        self.assertEqual(target.read_bytes(), b"untouched")
        alias = self.directory / "alias"
        alias.symlink_to(self.other, target_is_directory=True)
        with self.refused(locks._UNSAFE), locks.pin(alias):
            self.fail("symlink directory entered")

    def test_L1_post_open_check_and_ownership(self):
        real_fstat = os.fstat
        with locks.pin(self.directory) as record:
            for change in ("owner", "type"):
                def changed(fd):
                    fields = list(real_fstat(fd))
                    fields[4 if change == "owner" else 0] = (
                        os.geteuid() + 1 if change == "owner" else stat.S_IFIFO | 0o600)
                    return os.stat_result(fields)

                with self.subTest(change=change), \
                        mock.patch.object(os, "fstat", side_effect=changed), \
                        self.refused(locks._UNSAFE), record.state():
                    self.fail("unsafe opened descriptor entered")

    def test_L2_upgrade_refused_without_flock(self):
        with locks.pin(self.directory) as record:
            with record.drain() as fd:
                with mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("flock")):
                    with self.refused(locks._UPGRADE), record.drain(shared=False):
                        self.fail("upgrade entered")
                    with self.refused(locks._UPGRADE), record.drain(shared=False, inherited_fd=fd):
                        self.fail("import converted")

    def test_L3_cross_record_refused_before_open(self):
        with locks.pin(self.directory) as a, locks.pin(self.other) as b:
            for outer in (a.drain, a.start, a.state):
                with outer():
                    for inner in (b.drain, b.start, b.state):
                        with mock.patch.object(os, "open", side_effect=AssertionError("open")), \
                                self.refused("D5 lock refused: cross-record nesting"), inner():
                            self.fail("cross-record entered")

    def test_L4_U3_M6_same_record_reentrancy(self):
        # A fresh exclusive open mutation hangs only this disposable peer.
        proc = self.peer("nested")
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stdout.decode().strip(), str([
            locks.fcntl.LOCK_SH, locks.fcntl.LOCK_EX, locks.fcntl.LOCK_EX]))

    def test_L5_inherited_survivor_retains_drain_after_parent_close(self):
        with locks.pin(self.directory) as record:
            with record.drain() as fd:
                proc = self.peer("inherited", fd, pass_fds=(fd,))
                self.assertEqual(self.line(proc), "held")
            self.assertFalse(self.available("authority.drain.lock"))
            self.release(proc)
            self.assertTrue(self.available("authority.drain.lock"))

    def test_L5_child_close_keeps_parent_protected(self):
        with locks.pin(self.directory) as record:
            with record.drain() as fd:
                proc = self.peer("inherited", fd, pass_fds=(fd,))
                self.assertEqual(self.line(proc), "held")
                self.release(proc)
                self.assertFalse(self.available("authority.drain.lock"))
            self.assertTrue(self.available("authority.drain.lock"))

    def test_L6_nested_and_imported_references_dup_without_flock(self):
        with locks.pin(self.directory) as record:
            with record.drain() as first:
                with mock.patch.object(os, "dup", wraps=os.dup) as dup, \
                        mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("fresh flock")):
                    with record.drain() as second:
                        self.assertNotEqual(first, second)
                        self.assertFalse(os.get_inheritable(second))
                        with record.drain(inherited_fd=second) as third:
                            self.assertNotEqual(second, third)
                    self.assertEqual([c.args for c in dup.call_args_list], [(first,), (second,)])
                with self.assertRaises(OSError):
                    os.fstat(second)
            # Import a still-owned shared description outside this thread's stack.
            with record.drain() as original:
                delegated = os.dup(original)
            try:
                with mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("conversion")):
                    with record.drain(inherited_fd=delegated) as imported:
                        self.assertNotEqual(imported, delegated)
                        with self.refused(locks._UPGRADE), record.drain(shared=False):
                            self.fail("import upgrade")
            finally:
                os.close(delegated)

    def test_waiting_successor_leaves_start_and_state_available(self):
        with locks.pin(self.directory) as record:
            with record.drain():
                proc = self.peer("waiter")
                self.assertEqual(self.line(proc), "waiting")
                with record.state():  # preparation
                    pass
                with record.start(), record.state():  # control/accounting can progress
                    pass
                with record.drain():  # never opens a new reader behind the waiter
                    pass
            self.assertEqual(self.line(proc), "acquired")
            self.release(proc)

    def test_G1_missing_facilities_fail_before_open(self):
        for target, name, value in ((locks, "fcntl", None), (locks, "_DIR_FD", False),
                                    (locks, "_NOFOLLOW_STAT", False)):
            with mock.patch.object(target, name, value), \
                    mock.patch.object(os, "open", side_effect=AssertionError("open")), \
                    self.refused("D5 lock refused: safe locking unavailable"), \
                    locks.pin(self.directory):
                self.fail("unavailable lock entered")
        for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY"):
            value = getattr(os, name)
            try:
                delattr(os, name)
                with self.refused("D5 lock refused: safe locking unavailable"), locks.pin(self.directory):
                    self.fail("missing flag entered")
            finally:
                setattr(os, name, value)

    def test_G2_order_and_supported_sequences(self):
        with locks.pin(self.directory) as record:
            for outer, inners in ((record.state, (record.start, record.drain)),
                                  (record.start, (record.drain,))):
                with outer():
                    for inner in inners:
                        with self.refused(locks._ORDER), inner():
                            self.fail("reverse order")
            for shared in (True, False):
                with record.drain(shared=shared):
                    with record.state():
                        pass
                    with record.start(), record.state():
                        pass
                    with record.state():
                        pass
            with record.start(), record.state():
                pass

    def test_G3_closed_pin_refuses_all_guards(self):
        with locks.pin(self.directory) as record:
            pass
        for guard in (record.start, record.state, record.drain):
            with mock.patch.object(os, "open", side_effect=AssertionError("open")), \
                    self.refused("D5 lock refused: closed record"), guard():
                self.fail("closed pin entered")

    def test_G4_invalid_import_refused(self):
        with locks.pin(self.directory) as a, locks.pin(self.other) as b:
            with a.drain() as source:
                wrong = os.dup(source)
            try:
                with b.drain():
                    pass
                for fd in (wrong, -1):
                    with self.refused(locks._REFERENCE), b.drain(inherited_fd=fd):
                        self.fail("wrong import")
            finally:
                os.close(wrong)

    def test_G5_exclusive_drain_never_converts(self):
        with locks.pin(self.directory) as record:
            with record.drain(shared=False):
                with mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("conversion")):
                    with record.drain(shared=False):
                        pass
                    with self.refused("D5 lock refused: drain mode change"), record.drain():
                        self.fail("conversion")

    def test_threads_serialize_start_and_state_across_pins(self):
        for rank in (1, 2):
            with self.subTest(rank=rank), locks.pin(self.directory) as a, locks.pin(self.directory) as b:
                self.assertIs(a.entry, b.entry)
                attempted, release = threading.Event(), threading.Event()
                guard = a.entry.guards[rank]
                results = []

                @contextmanager
                def instrumented():
                    if threading.current_thread() is not threading.main_thread():
                        attempted.set()
                    with guard:
                        yield

                class Probe:
                    def __enter__(self):
                        self.context = instrumented()
                        return self.context.__enter__()

                    def __exit__(self, *args):
                        return self.context.__exit__(*args)

                # Use a fresh context per thread; the real RLock is shared.
                class Factory:
                    def __init__(self):
                        self.local = threading.local()

                    def __enter__(self):
                        self.local.probe = Probe()
                        return self.local.probe.__enter__()

                    def __exit__(self, *args):
                        return self.local.probe.__exit__(*args)

                a.entry.guards[rank] = Factory()

                def contender():
                    try:
                        with b._exclusive(rank):
                            results.append(release.is_set())
                    except BaseException as exc:
                        results.append(exc)

                with a._exclusive(rank):
                    thread = threading.Thread(target=contender, daemon=True)
                    thread.start()
                    self.assertTrue(attempted.wait(10), "thread watchdog")
                    release.set()
                thread.join(10)
                self.assertFalse(thread.is_alive(), "thread watchdog")
                self.assertEqual(results, [True])


if __name__ == "__main__":
    unittest.main()
