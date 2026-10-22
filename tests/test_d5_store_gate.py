"""U3b store integration: real locks, pipe ordering and S1-S3 mutations.

The simulated admitted start is local test code, not a submission API.
Watchdog deadlines fail and reap peers; no sleep establishes test ordering.
"""
from contextlib import ExitStack
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
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_authority_store as store
import d5_locks as locks
from d5_host import ProcessIdentity
from tests.test_d5_authority import MANDATE, grant, held

START = "authority.start.lock"
STATE = "authority.lock"
DRAIN = "authority.drain.lock"
WRITERS = ("create", "check", "revoke", "relinquish", "claim", "replace")
ANCHOR = ProcessIdentity(42, 9000001)


def operation(directory, name):
    if name == "create":
        return store.create(directory, held())
    if name == "check":
        return store.check(directory, 102, b"changed")
    if name == "revoke":
        return store.revoke(directory, 102, "stop")
    if name == "relinquish":
        return store.relinquish(directory, 102, "native", ANCHOR)
    if name == "claim":
        return store.claim(directory, 102, MANDATE, "successor",
                           ProcessIdentity(43, 9000002), lambda _: "dead")
    if name == "replace":
        return store.replace(directory, grant("b" * 32))
    if name == "read":
        return store.read(directory)
    raise AssertionError(name)


def lock_name(directory, fd):
    info = os.fstat(fd)
    for name in (START, STATE, DRAIN):
        path = directory / name
        if path.exists():
            other = path.stat()
            if (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino):
                return name
    raise AssertionError("unexpected lock descriptor")


def peer_main():
    directory, name, watched = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    real = locks.fcntl.flock

    def flock(fd, mode):
        if lock_name(directory, fd) == watched:
            try:
                real(fd, mode | locks.fcntl.LOCK_NB)
            except BlockingIOError:
                print("blocked:" + watched, flush=True)
                real(fd, mode)
            else:
                print("acquired:" + watched, flush=True)
        else:
            real(fd, mode)

    with mock.patch.object(locks.fcntl, "flock", side_effect=flock):
        operation(directory, name)
    print("done", flush=True)


class TestD5StoreGate(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def seed(self, name):
        directory = self.directory / name
        directory.mkdir()
        if name != "create":
            store.create(directory, held())
        return directory

    def available(self, directory, name, shared=False):
        fd = os.open(directory / name, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                locks.fcntl.flock(fd, locks.fcntl.LOCK_NB |
                                  (locks.fcntl.LOCK_SH if shared else locks.fcntl.LOCK_EX))
            except BlockingIOError:
                return False
            return True
        finally:
            os.close(fd)

    def peer(self, directory, name, watched=START):
        proc = subprocess.Popen([
            sys.executable, "-c", "from tests.test_d5_store_gate import peer_main; peer_main()",
            str(directory), name, watched], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)

        self.addCleanup(cleanup)
        return proc

    def line(self, proc):
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        self.assertTrue(ready, "peer watchdog: missing lock observation")
        return proc.stdout.readline().decode().strip()

    def finish(self, proc):
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, (stdout, stderr))
        self.assertEqual(stdout, b"done\n")

    def start_blocks(self, name):
        directory = self.seed(name)
        with locks.pin(directory) as record, record.start():
            before = ((directory / store.RECORD_NAME).read_bytes()
                      if name != "create" else None)
            proc = self.peer(directory, name)
            self.assertEqual(self.line(proc), "blocked:" + START)
            self.assertTrue(self.available(directory, STATE))
            after = ((directory / store.RECORD_NAME).read_bytes()
                     if name != "create" else None)
            self.assertEqual(after, before)
        self.finish(proc)
        return directory

    def test_S1_U3_M1_revoke_waits_for_admitted_start(self):
        directory = self.start_blocks("revoke")
        with locks.pin(directory) as record, record.drain(), record.start():
            self.assertEqual(store.check(directory, 103, MANDATE), "invalidated")
        self.assertEqual(store.read(directory)["invalidation"]["kind"], "revoked")

    def test_S2_U3_M2_check_waits_for_admitted_start(self):
        directory = self.start_blocks("check")
        with locks.pin(directory) as record, record.drain(), record.start():
            self.assertEqual(store.check(directory, 103, MANDATE), "invalidated")
        self.assertEqual(store.read(directory)["invalidation"]["kind"], "mandate_changed")

    def test_S3_other_named_writers_wait_for_start(self):
        for name in ("create", "relinquish", "claim", "replace"):
            with self.subTest(name=name):
                directory = self.start_blocks(name)
                record = store.read(directory)
                if name == "relinquish":
                    self.assertEqual(record["custody"]["relinquished_at"], 102)
                elif name == "claim":
                    self.assertEqual(record["custody"]["generation"], 2)
                else:
                    self.assertEqual(record["grant_id"], ("b" if name == "replace" else "a") * 32)

    def test_named_acquisition_order_and_read_state_only(self):
        real = locks.fcntl.flock
        for name in WRITERS + ("read",):
            with self.subTest(name=name):
                directory = self.seed(name)
                calls = []

                def flock(fd, mode):
                    calls.append((lock_name(directory, fd), mode))
                    return real(fd, mode)

                with mock.patch.object(locks.fcntl, "flock", side_effect=flock):
                    operation(directory, name)
                expected = [STATE] if name == "read" else [START, STATE]
                if name in ("claim", "replace"):
                    expected.insert(0, DRAIN)
                self.assertEqual(calls, [(lock, locks.fcntl.LOCK_EX) for lock in expected])
                for lock in expected:
                    self.assertTrue(self.available(directory, lock))

    def test_check_retains_start_through_invalidation_persistence(self):
        directory = self.seed("check")
        stages = []
        real_write, real_sync = store._write, store._sync_directory

        def writing(path, raw):
            self.assertFalse(self.available(directory, START))
            self.assertFalse(self.available(directory, STATE))
            stages.append("write")
            return real_write(path, raw)

        def syncing(path):
            self.assertFalse(self.available(directory, START))
            self.assertFalse(self.available(directory, STATE))
            stages.append("fsync")
            return real_sync(path)

        with mock.patch.object(store, "_write", side_effect=writing), \
                mock.patch.object(store, "_sync_directory", side_effect=syncing):
            self.assertEqual(store.check(directory, 102, b"changed"), "mandate_changed")
        self.assertEqual(stages, ["write", "fsync"])
        self.assertEqual(store.check(directory, 103, MANDATE), "invalidated")
        self.assertTrue(self.available(directory, START))

    def test_read_progresses_while_start_is_held(self):
        directory = self.seed("read")
        with locks.pin(directory) as record, record.start():
            proc = self.peer(directory, "read", STATE)
            self.assertEqual(self.line(proc), "acquired:" + STATE)
            self.finish(proc)
        self.assertEqual(store.read(directory), held())

    def test_claim_replace_wait_for_drain_without_start_or_state(self):
        for name in ("claim", "replace"):
            with self.subTest(name=name):
                directory = self.seed(name)
                with locks.pin(directory) as record, record.drain():
                    proc = self.peer(directory, name, DRAIN)
                    self.assertEqual(self.line(proc), "blocked:" + DRAIN)
                    self.assertTrue(self.available(directory, START))
                    self.assertTrue(self.available(directory, STATE))
                    self.assertEqual(store.read(directory), held())
                self.finish(proc)

    def test_revoke_and_check_do_not_wait_for_drain(self):
        for name in ("revoke", "check"):
            with self.subTest(name=name):
                directory = self.seed(name)
                with locks.pin(directory) as record, record.drain():
                    proc = self.peer(directory, name)
                    self.assertEqual(self.line(proc), "acquired:" + START)
                    self.finish(proc)
                    self.assertIsNotNone(store.read(directory)["invalidation"])

    def test_nested_named_operations_reuse_start_and_state(self):
        for name in WRITERS + ("read",):
            with self.subTest(name=name):
                directory = self.seed(name)
                with locks.pin(directory) as record, ExitStack() as stack:
                    stack.enter_context(record.drain(shared=name not in ("claim", "replace")))
                    stack.enter_context(record.start())
                    stack.enter_context(record.state())
                    # No new flock description is permitted under these outer locks.
                    with mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("reflock")):
                        operation(directory, name)
                for lock in (DRAIN, START, STATE):
                    self.assertTrue(self.available(directory, lock))

    def test_store_preserves_upgrade_and_cross_record_refusals(self):
        directory = self.seed("held")
        other = self.seed("other")
        with locks.pin(directory) as record, record.drain():
            for name in ("claim", "replace"):
                with self.subTest(name=name), self.assertRaisesRegex(
                        locks.LockError, "^D5 custody change refused: caller holds shared drain$"):
                    operation(directory, name)
            for name in WRITERS + ("read",):
                with self.subTest(name=name), self.assertRaisesRegex(
                        locks.LockError, "^D5 lock refused: cross-record nesting$"):
                    operation(other, name)

    def test_state_only_same_record_read_is_reentrant(self):
        directory = self.seed("read")
        with locks.pin(directory) as record, record.state(), \
                mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("reflock")):
            self.assertEqual(store.read(directory), held())

    def test_threads_share_store_start_guard(self):
        directory = self.seed("revoke")
        started, finished = threading.Event(), threading.Event()
        errors = []
        real = locks._Record.start

        def entering(record):
            started.set()
            return real(record)

        def worker():
            try:
                store.revoke(directory, 102, "stop")
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        with locks.pin(directory) as record:
            with record.start(), mock.patch.object(locks._Record, "start", entering):
                thread = threading.Thread(target=worker, daemon=True)
                thread.start()
                self.assertTrue(started.wait(10), "worker watchdog")
                self.assertFalse(finished.is_set())
                self.assertEqual(store.read(directory), held())
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(store.read(directory)["invalidation"]["kind"], "revoked")


if __name__ == "__main__":
    unittest.main()
