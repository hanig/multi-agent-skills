"""U3-R1 consumers, seam characterization and named mutation targets."""
from contextlib import nullcontext
from contextlib import ExitStack
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import weakref
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_locks as locks
import d5_protocol_locks as protocol
import d5_effect_log as ledger
import d5_authority_store as store
from tests.test_d5_authority import grant
from tests import test_d5_locks as lock_tests

PEER = ROOT / "tests/fixtures/d5/protocol_peer.py"


class TestProtocolLocks(unittest.TestCase):
    def setUp(self):
        lock_tests.isolate_pool(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.other = self.directory / "other"
        self.other.mkdir(mode=0o700)

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
        self.assertTrue(ready, "peer watchdog")
        return proc.stdout.readline().decode().rstrip("\n")

    def send(self, proc, message):
        proc.stdin.write((message + "\n").encode())
        proc.stdin.flush()

    def probe(self, rank):
        proc = self.peer("probe", rank)
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr)
        return stdout.decode().strip()

    def test_lock_domain_matches_merged(self):
        # R1-SPLIT-LOCK-DOMAIN: independent merged consumer probes these inodes.
        self.assertEqual(locks._NAMES, {0: "authority.drain.lock",
                         1: "authority.start.lock", 2: "authority.lock"})
        real_open = os.open
        opened = []

        def opening(path, flags, mode=0o777, **kwargs):
            fd = real_open(path, flags, mode, **kwargs)
            opened.append((path, flags, mode, kwargs.get("dir_fd")))
            self.assertFalse(os.get_inheritable(fd))
            return fd

        with mock.patch.object(os, "open", side_effect=opening):
            with protocol.pin(self.directory) as record:
                with record.drain() as fd, record.start():
                    info = os.fstat(fd)
                    disk = (self.directory / "authority.drain.lock").stat()
                    self.assertEqual((info.st_dev, info.st_ino), (disk.st_dev, disk.st_ino))
                    self.assertEqual(self.probe(0), "blocked")
                    self.assertEqual(self.probe(1), "blocked")
        self.assertEqual(opened[0][1], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        files = [row for row in opened if row[3] is not None]
        self.assertEqual([row[0] for row in files], ["authority.drain.lock", "authority.start.lock"])
        for _, flags, mode, _ in files:
            self.assertEqual(flags, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW)
            self.assertEqual(mode, 0o600)
        identities = {p.name: p.stat().st_ino for p in self.directory.glob("*.lock")}
        with protocol.pin(self.directory) as record:
            with record.drain(shared=False), record.start():
                self.assertEqual(self.probe(0), "blocked")
        for name, inode in identities.items():
            self.assertEqual((self.directory / name).stat().st_ino, inode)
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600)
        self.assertEqual(self.probe(0), "available")
        self.assertEqual(self.probe(1), "available")
        self.assertFalse((self.directory / "authority.lock").exists())

    def ledger_setup(self, stack):
        stack.enter_context(nullcontext())
        stack.enter_context(mock.patch.object(ledger.registry, "lookup", return_value={
            "registration_id": "a" * 32, "record_dir": str(self.directory)}))
        return dict(record_id="b" * 32,
                    custody=dict(generation=1, session_id="fixture", anchor_pid=123, anchor_start_us=101),
                    kind="fixture", scope=dict(plans=["p"], repositories=["r"], actions=["a"], trackers=[]),
                    correlation={"request_id": "outer"})

    def test_nested_start_reentrant(self):
        # U3-M6: nested logical operations get distinct durable accounting.
        with ExitStack() as stack:
            request = self.ledger_setup(stack)
            with protocol.pin(self.directory) as outer:
                with outer.drain(), outer.start():
                    a = ledger.prepare("fixture", "p", "fixture", **request)
                    original = locks._Record._open

                    def opening(record, rank):
                        self.assertNotEqual(rank, 1, "nested START opened another description")
                        return original(record, rank)

                    with mock.patch.object(locks._Record, "_open", opening):
                        with protocol.pin(self.directory / ".") as inner:
                            with inner.drain(), inner.start():
                                request["correlation"] = {"request_id": "inner"}
                                b = ledger.prepare("fixture", "p", "fixture", **request)
                                self.assertEqual(self.probe(1), "blocked")
                    self.assertEqual(self.probe(1), "blocked")
                with outer.drain():
                    for attempt in (a, b):
                        receipt = ledger._classify(attempt, "submitted")
                        ledger.complete(receipt, {"status": "succeeded", "detail": attempt.effect_id})
            self.assertNotEqual(a.effect_id, b.effect_id)
            self.assertEqual(ledger.unresolved("fixture", "p", "fixture"), [])
            self.assertEqual(len(list((self.directory / ledger.DIRECTORY).glob("*.json"))), 2)
        self.assertEqual(self.probe(1), "available")

    def test_start_excludes_peer(self):
        # R1-DROP-THREAD-EXCLUSION. Hook measures failed nonblocking acquisition;
        # no elapsed interval is used as proof of exclusion.
        attempted, release = threading.Event(), threading.Event()
        results = []
        with protocol.pin(self.directory) as a, protocol.pin(self.directory) as b:
            guard = a._record.entry.guards[1]

            class ObservedGuard:
                def __enter__(self):
                    if threading.current_thread() is not threading.main_thread():
                        got = guard.acquire(blocking=False)
                        results.append(("blocked", not got))
                        attempted.set()
                        if got:
                            return
                    guard.acquire()

                def __exit__(self, *args):
                    guard.release()

            def contender():
                try:
                    with b.start():
                        results.append(("entered_after_release", release.is_set()))
                except BaseException as exc:
                    results.append(exc)
                finally:
                    attempted.set()

            self.addCleanup(a._record.entry.guards.__setitem__, 1, guard)
            a._record.entry.guards[1] = ObservedGuard()
            with a.start():
                thread = threading.Thread(target=contender, daemon=True)
                thread.start()
                try:
                    self.assertTrue(attempted.wait(10), "thread watchdog")
                    self.assertEqual(results, [("blocked", True)])
                    self.assertEqual(self.probe(1), "blocked")
                finally:
                    release.set()
            thread.join(10)
            self.assertFalse(thread.is_alive(), "thread watchdog")
            self.assertEqual(results, [("blocked", True), ("entered_after_release", True)])
        self.assertEqual(self.probe(1), "available")

    def test_guard_restoration_with_reused_inode(self):
        # Retain a pooled entry across the original test order and inode reuse.
        entry = locks._Entry()
        originals = dict(entry.guards)
        pool = weakref.WeakValueDictionary({(123, 456): entry})
        real_fstat = os.fstat
        directories = []
        cases = (lock_tests.TestD5Locks("test_threads_serialize_start_and_state_across_pins"),
                 TestProtocolLocks("test_start_excludes_peer"))
        for case in cases:
            prior_pool = locks._pool
            try:
                case.setUp()
                self.assertIsNot(locks._pool, prior_pool)
                directories.append(case.directory)
                disk = case.directory.stat()

                def reused(fd):
                    info = real_fstat(fd)
                    if (info.st_dev, info.st_ino) == (disk.st_dev, disk.st_ino):
                        fields = list(info)
                        fields[stat.ST_DEV], fields[stat.ST_INO] = 123, 456
                        return os.stat_result(fields)
                    return info

                # Share the pool to test restoration independently of isolation.
                with mock.patch.object(locks, "_pool", pool), \
                        mock.patch.object(os, "fstat", side_effect=reused):
                    with locks.pin(case.directory) as record:
                        self.assertIs(record.entry, entry)
                    getattr(case, case._testMethodName)()
            finally:
                self.assertTrue(case.doCleanups())
            self.assertIs(locks._pool, prior_pool)
        self.assertNotEqual(*directories)
        self.assertEqual(entry.guards, originals)

    def test_inherited_drain_survives_owner_close(self):
        # R1-UNLOCK-SHARED: parent closure cannot release the child's description.
        with protocol.pin(self.directory) as record:
            with record.drain() as fd:
                proc = self.peer("inherited", fd, pass_fds=(fd,))
                self.assertEqual(self.line(proc), "held")
            self.assertEqual(self.probe(0), "blocked")
            self.send(proc, "release")
            self.assertEqual(self.line(proc), "released")
            self.assertIsNone(proc.poll())
            self.assertEqual(self.probe(0), "available")
            self.send(proc, "exit")
            stdout, stderr = proc.communicate(timeout=10)
            self.assertEqual((proc.returncode, stdout, stderr), (0, b"", b""))

    def test_upgrade_refused(self):
        with protocol.pin(self.directory) as record:
            with record.drain() as fd:
                with mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("upgrade attempted")):
                    for inherited in (None, fd):
                        with self.subTest(inherited=inherited), self.assertRaisesRegex(
                                protocol.LockError, "^D5 drain upgrade is unsupported$"):
                            with record.drain(shared=False, inherited_fd=inherited):
                                self.fail("upgrade entered")

    def test_cross_authority_nesting_refused(self):
        with protocol.pin(self.directory) as a, protocol.pin(self.other) as b:
            for outer in (a.drain, a.start):
                with outer():
                    for inner in (b.drain, b.start):
                        with self.subTest(outer=outer, inner=inner):
                            with mock.patch.object(os, "open", side_effect=AssertionError("cross open")):
                                with self.assertRaisesRegex(protocol.LockError, "^D5 nested authority mismatch$"):
                                    with inner():
                                        self.fail("cross authority entered")

    def test_cleanup_preserves_primary(self):
        # R1-REPLACE-PRIMARY, including an error whose text matches a refusal.
        for cls in (KeyboardInterrupt, OSError, protocol.LockError):
            primary = cls(locks._UPGRADE)
            cause = ValueError("cause")
            primary.__cause__ = cause
            errors = []
            real_close = os.close

            def close(fd):
                real_close(fd)
                error = OSError("close %d" % len(errors))
                errors.append(error)
                raise error

            with self.subTest(cls=cls), mock.patch.object(os, "close", side_effect=close):
                with self.assertRaises(BaseException) as caught:
                    with protocol.pin(self.directory) as record:
                        with record.drain(), record.start():
                            raise primary
            self.assertIs(caught.exception, primary)
            self.assertEqual(str(primary), locks._UPGRADE)
            self.assertIs(primary.__cause__, cause)
            self.assertEqual(len(errors), 3)
            self.assertEqual(primary.cleanup_errors, tuple(errors))
        self.assertEqual(locks._held(), [])
        self.assertEqual(self.probe(0), "available")
        self.assertEqual(self.probe(1), "available")

    def test_merged_store_and_ledger_seams(self):
        opened = []
        real_open = os.open

        def opening(path, flags, mode=0o777, **kwargs):
            fd = real_open(path, flags, mode, **kwargs)
            if Path(path).name == store.LOCK_NAME:
                opened.append((flags, mode, os.get_inheritable(fd)))
            return fd

        with mock.patch.object(os, "open", side_effect=opening):
            with protocol.pin(self.directory) as record:
                with record.drain(shared=False), record.start():
                    store.create(self.directory, grant())
        self.assertEqual(opened, [(os.O_CREAT | os.O_RDWR, 0o600, False)])
        ranks = []
        original = locks._Record._exclusive

        def exclusive(record, rank):
            ranks.append(rank)
            return original(record, rank)

        with ExitStack() as stack:
            request = self.ledger_setup(stack)
            stack.enter_context(mock.patch.object(locks._Record, "_exclusive", exclusive))
            for name in ("start", "drain"):
                stack.enter_context(mock.patch.object(locks._Record, name, side_effect=AssertionError(name)))
            attempt = ledger.prepare("fixture", "p", "fixture", **request)
            receipt = ledger._classify(attempt, "submitted")
            ledger.complete(receipt, {"status": "succeeded", "detail": "fixture"})
            self.assertEqual(ledger.unresolved("fixture", "p", "fixture"), [])
        self.assertEqual(ranks, [2, 2, 2, 2])


if __name__ == "__main__":
    unittest.main()
