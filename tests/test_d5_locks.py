"""U3a primitives, real flock/descriptor tests, and named mutation targets.

Ordering uses pipes/events or lock-entry hooks. Timeouts only fail tests and
reap fixture processes: they never release or steal an operational lock.
"""
from contextlib import ExitStack
import errno
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
        for index, (_, flags, mode, dir_fd, _) in enumerate(lock_opens):
            expected = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
            if index < 3:
                expected |= os.O_CREAT | os.O_EXCL
            self.assertEqual(flags, expected)
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

    def test_R1_umask_new_persisted_and_overlapping_creators(self):
        proc = self.peer("permissions")
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stdout, b"permissions repaired\n")

    def test_new_modes_before_flock_under_umasks(self):
        real_flock = locks.fcntl.flock

        def checked(fd, operation):
            self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
            return real_flock(fd, operation)

        for mask in (0o000, 0o022, 0o077, 0o277, 0o777):
            with self.subTest(umask=oct(mask)), locks.pin(self.directory) as record:
                saved = os.umask(mask)
                try:
                    with mock.patch.object(locks.fcntl, "flock", side_effect=checked), \
                            mock.patch.object(os, "fchmod", wraps=os.fchmod) as chmod:
                        with record.drain(), record.start(), record.state():
                            self.assertEqual(chmod.call_count, 3)
                        # Reopening safe existing locks must not chmod them.
                        with record.drain(), record.start(), record.state():
                            self.assertEqual(chmod.call_count, 3)
                finally:
                    os.umask(saved)
                for name in locks._NAMES.values():
                    path = self.directory / name
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    path.unlink()  # fixture reset between masks

    def test_existing_group_or_other_permissions_refused_without_repair(self):
        for rank, name in locks._NAMES.items():
            for mode in (0o644, 0o640, 0o620, 0o610, 0o604, 0o602, 0o601, 0o040):
                with self.subTest(rank=rank, mode=oct(mode)):
                    path = self.directory / name
                    path.write_bytes(b"unchanged")
                    path.chmod(mode)
                    inode = path.stat().st_ino
                    try:
                        with locks.pin(self.directory) as record, \
                                mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("flock")), \
                                mock.patch.object(os, "chmod", side_effect=AssertionError("chmod")), \
                                mock.patch.object(os, "fchmod", side_effect=AssertionError("fchmod")):
                            guard = (record.drain, record.start, record.state)[rank]
                            with self.refused(locks._UNSAFE), guard():
                                self.fail("nonprivate lock entered")
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                        self.assertEqual(path.stat().st_ino, inode)
                    finally:
                        path.chmod(0o600)
                    self.assertEqual(path.read_bytes(), b"unchanged")
                    path.unlink()

    def test_new_nonprivate_mode_repaired_before_check_and_flock(self):
        real_open, real_fchmod = os.open, os.fchmod
        real_flock = locks.fcntl.flock
        created = []

        def opening(path, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if flags & os.O_CREAT:
                self.assertTrue(flags & os.O_EXCL)
                real_fchmod(fd, 0o644)  # synthetic inherited-ACL mode
                created.append(path)
            return fd

        def flock(fd, operation):
            self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
            return real_flock(fd, operation)

        with mock.patch.object(os, "open", side_effect=opening), \
                mock.patch.object(locks.fcntl, "flock", side_effect=flock):
            with locks.pin(self.directory) as record:
                with record.drain(), record.start(), record.state():
                    pass
        self.assertCountEqual(created, locks._NAMES.values())
        for name in created:
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600)

    def test_existing_nonprivate_inherited_drain_uses_reference_refusal(self):
        path = self.directory / "authority.drain.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            for mode in (0o644, 0o640, 0o620, 0o610, 0o604, 0o602, 0o601):
                with self.subTest(mode=oct(mode)):
                    os.fchmod(fd, mode)
                    locks.fcntl.flock(fd, locks.fcntl.LOCK_SH)
                    with locks.pin(self.directory) as record, \
                            mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("flock")), \
                            mock.patch.object(os, "fchmod", side_effect=AssertionError("fchmod")), \
                            self.refused(locks._REFERENCE), record.drain(inherited_fd=fd):
                        self.fail("nonprivate inherited drain entered")
                    self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), mode)
                    self.assertFalse(self.available(path.name))
        finally:
            os.close(fd)

    def test_G6_unrelated_permission_error_does_not_change_mode(self):
        with locks.pin(self.directory) as record:
            with record.state():
                pass
            with mock.patch.object(os, "open", side_effect=PermissionError("fixture")), \
                    mock.patch.object(os, "chmod", side_effect=AssertionError("chmod")), \
                    self.refused(locks._UNSAFE), record.state():
                self.fail("unrelated permission error hidden")

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
                with record.state():
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

                class Gate:
                    def __enter__(self):
                        if threading.current_thread() is not threading.main_thread():
                            attempted.set()
                        guard.acquire()

                    def __exit__(self, *args):
                        guard.release()

                a.entry.guards[rank] = Gate()

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



class TestCleanup(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def check_site(self, site, trigger):
        primary = (OSError(errno.EIO, "flock primary") if trigger == "flock" else
                   KeyboardInterrupt("caller primary"))
        cause = ValueError("primary cause")
        primary.__cause__ = cause
        cleanup = OSError(errno.EIO, "close cleanup")
        real_close = os.close

        def close(fd):
            real_close(fd)  # Inject the error after releasing the fixture fd.
            raise cleanup

        with ExitStack() as stack:
            record = stack.enter_context(locks.pin(self.directory)) if site != "pin" else None
            guard = locks.pin(self.directory) if record is None else getattr(record, site)()
            with mock.patch.object(os, "close", side_effect=close) as closing:
                if trigger == "flock":
                    stack.enter_context(mock.patch.object(locks.fcntl, "flock", side_effect=primary))
                with self.assertRaises(BaseException) as caught:
                    # An already handled caller exception is not unwinding this guard.
                    try:
                        raise ValueError("ambient handled exception")
                    except ValueError:
                        with guard:
                            if trigger == "body":
                                raise primary
                closing.assert_called_once()
            expected = cleanup if trigger == "none" else primary
            self.assertIs(caught.exception, expected)
            self.assertEqual(str(caught.exception), str(expected))
            self.assertIs(caught.exception.__cause__, None if trigger == "none" else cause)
        self.assertEqual(locks._held(), [])
        if record is not None:
            self.assertIsNone(record.fd)

    def test_exclusive_cleanup(self):
        for site in ("start", "state"):
            for trigger in ("body", "flock", "none"):
                with self.subTest(site=site, trigger=trigger):
                    self.check_site(site, trigger)

    def test_drain_cleanup(self):
        for trigger in ("body", "flock", "none"):
            with self.subTest(trigger=trigger):
                self.check_site("drain", trigger)

    def test_pin_cleanup(self):
        for trigger in ("body", "none"):
            with self.subTest(trigger=trigger):
                self.check_site("pin", trigger)

    def test_caller_finally_cleanup_keeps_existing_context(self):
        # Explicit non-claim: the guard's own body finishes normally.
        primary = ValueError("primary")
        cause = LookupError("original cause")
        primary.__cause__ = cause
        cleanup = OSError(errno.EIO, "cleanup EIO")
        real_close = os.close

        def close(fd):
            real_close(fd)
            raise cleanup

        with self.assertRaises(BaseException) as caught:
            try:
                raise primary
            finally:
                with mock.patch.object(os, "close", side_effect=close) as closing:
                    with locks.pin(self.directory):
                        pass
        closing.assert_called_once()
        self.assertIs(caught.exception, cleanup)
        self.assertEqual(str(caught.exception), "[Errno 5] cleanup EIO")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIs(caught.exception.__context__, primary)
        self.assertEqual(str(primary), "primary")
        self.assertIs(primary.__cause__, cause)

    def test_post_open_cleanup(self):
        real_close = os.close
        def close(fd):
            real_close(fd)
            raise OSError(errno.EIO, "close cleanup")

        with locks.pin(self.directory) as record:
            # New file: _safe is first consulted after open/fstat.
            with mock.patch.object(locks, "_safe", return_value=False), \
                    mock.patch.object(os, "close", side_effect=close) as closing, \
                    self.assertRaises(locks.LockError) as caught:
                record._open(2)
            closing.assert_called_once()
            self.assertEqual(str(caught.exception), locks._UNSAFE)
            self.assertIsNone(caught.exception.__cause__)

    def test_open_primary_oserror_keeps_original_wrapping(self):
        primary = OSError(errno.EIO, "fstat primary")
        primary.__cause__ = ValueError("source cause")
        real_close = os.close
        def close(fd):
            real_close(fd)
            raise OSError(errno.EIO, "close cleanup")

        with locks.pin(self.directory) as record:
            with mock.patch.object(os, "fstat", side_effect=primary), \
                    mock.patch.object(os, "close", side_effect=close) as closing, \
                    self.assertRaises(locks.LockError) as caught:
                record._open(2)
            closing.assert_called_once()
            self.assertEqual(str(caught.exception), locks._UNSAFE)
            self.assertIs(caught.exception.__cause__, primary)
            self.assertEqual(str(primary.__cause__), "source cause")


class TestCleanupErrors(unittest.TestCase):
    """DISCARD-CLEANUP-ERROR: each consumer must retain its secondary error."""
    def test_readonly_cleanup_errors_preserves_primary(self):
        # ARC-3635-OD2: failed annotation drops the secondary, never the primary.
        class Primary(RuntimeError):
            @property
            def cleanup_errors(self):
                return ()

        self.check_annotation(Primary, "dropped")

    def test_slots_cleanup_errors_preserves_primary(self):
        class Primary(RuntimeError):
            __slots__ = ("code",)

        # BaseException still supplies __dict__; slots alone allow annotation.
        self.check_annotation(Primary, "attached")

    def test_setattr_cleanup_errors_preserves_primary(self):
        class Primary(RuntimeError):
            def __setattr__(self, name, value):
                if name == "cleanup_errors":
                    raise AttributeError("diagnostics are read-only")
                super().__setattr__(name, value)

        self.check_annotation(Primary, "dropped")

    def test_getter_cleanup_errors_preserves_primary(self):
        class Primary(RuntimeError):
            @property
            def cleanup_errors(self):
                raise ValueError("diagnostics are unreadable")

        self.check_annotation(Primary, "unreadable")

    def check_annotation(self, kind, annotation):
        with tempfile.TemporaryDirectory() as directory:
            for site in ("pin", "start", "state", "drain", "_open"):
                with self.subTest(site=site), ExitStack() as stack:
                    record = (None if site == "pin" else
                              stack.enter_context(locks.pin(directory)))
                    primary, secondary = kind("primary"), OSError("secondary")
                    cause = ValueError("cause")
                    primary.__cause__ = cause
                    real_close = os.close

                    def close(fd):
                        real_close(fd)
                        raise secondary

                    with mock.patch.object(os, "close", side_effect=close) as closing:
                        with self.assertRaises(BaseException) as caught:
                            if site == "_open":
                                with mock.patch.object(os, "fstat", side_effect=primary):
                                    record._open(1)
                            else:
                                guard = (locks.pin(directory) if record is None
                                         else getattr(record, site)())
                                with guard:
                                    raise primary
                        closing.assert_called_once()
                    self.assertIs(caught.exception, primary)
                    self.assertIs(type(caught.exception), kind)
                    self.assertEqual(str(caught.exception), "primary")
                    self.assertIs(caught.exception.__cause__, cause)
                    if annotation == "attached":
                        self.assertEqual(caught.exception.cleanup_errors, (secondary,))
                    elif annotation == "dropped":
                        self.assertEqual(getattr(caught.exception, "cleanup_errors", ()), ())
                    else:
                        with self.assertRaisesRegex(ValueError, "diagnostics are unreadable"):
                            _ = caught.exception.cleanup_errors

    def test_cleanup_preserves_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            for site in ("pin", "start", "state", "drain", "_open"):
                for kind in (ValueError, OSError, KeyboardInterrupt):
                    with self.subTest(site=site, kind=kind), ExitStack() as stack:
                        record = (None if site == "pin" else
                                  stack.enter_context(locks.pin(directory)))
                        primary, secondary = kind("primary"), OSError("secondary")
                        cause, previous = ValueError("cause"), OSError("previous")
                        primary.__cause__ = cause
                        primary.cleanup_errors = (previous,)
                        real_close = os.close

                        def close(fd):
                            real_close(fd)
                            raise secondary

                        with mock.patch.object(os, "close", side_effect=close) as closing:
                            with self.assertRaises(BaseException) as caught:
                                if site == "_open":
                                    with mock.patch.object(os, "fstat", side_effect=primary):
                                        record._open(1)
                                else:
                                    guard = (locks.pin(directory) if record is None
                                             else getattr(record, site)())
                                    with guard:
                                        raise primary
                            closing.assert_called_once()
                        observed = caught.exception
                        if site == "_open" and kind is OSError:
                            self.assertIsInstance(observed, locks.LockError)
                            self.assertEqual(str(observed), locks._UNSAFE)
                            observed = observed.__cause__
                        self.assertIs(observed, primary)
                        self.assertEqual(str(observed), "primary")
                        self.assertIs(observed.__cause__, cause)
                        self.assertEqual(observed.cleanup_errors, (previous, secondary))


if __name__ == "__main__":
    unittest.main()
