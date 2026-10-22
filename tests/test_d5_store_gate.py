"""U3b store integration with real locks and deterministic pipe ordering."""
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


def operation(d, name):
    if name == "create":
        return store.create(d, held())
    if name == "check":
        return store.check(d, 102, b"changed")
    if name == "revoke":
        return store.revoke(d, 102, "stop")
    if name == "relinquish":
        return store.relinquish(d, 102, "native", ANCHOR)
    if name == "claim":
        return store.claim(d, 102, MANDATE, "successor",
                           ProcessIdentity(43, 9000002), lambda _: "dead")
    if name == "replace":
        return store.replace(d, grant("b" * 32))
    if name == "read":
        return store.read(d)
    raise AssertionError(name)


def lock_name(d, fd):
    info = os.fstat(fd)
    for name in (START, STATE, DRAIN):
        path = d / name
        if path.exists():
            other = path.stat()
            if (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino):
                return name
    raise AssertionError("unexpected lock descriptor")


def peer_main():
    d, name, watched = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    real = locks.fcntl.flock

    def flock(fd, mode):
        if lock_name(d, fd) == watched:
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
        operation(d, name)
    print("done", flush=True)


class TestD5StoreGate(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.d = Path(temporary.name)

    def seed(self, name):
        d = self.d / name
        d.mkdir()
        if name != "create":
            store.create(d, held())
        return d

    def available(self, d, name, shared=False):
        fd = os.open(d / name, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                locks.fcntl.flock(fd, locks.fcntl.LOCK_NB |
                                  (locks.fcntl.LOCK_SH if shared else locks.fcntl.LOCK_EX))
            except BlockingIOError:
                return False
            return True
        finally:
            os.close(fd)

    def peer(self, d, name, watched=START):
        proc = subprocess.Popen([
            sys.executable, "-c", "from tests.test_d5_store_gate import peer_main; peer_main()",
            str(d), name, watched], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

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
        d = self.seed(name)
        with locks.pin(d) as record, record.start():
            before = ((d / store.RECORD_NAME).read_bytes()
                      if name != "create" else None)
            proc = self.peer(d, name)
            self.assertEqual(self.line(proc), "blocked:" + START)
            self.assertTrue(self.available(d, STATE))
            after = ((d / store.RECORD_NAME).read_bytes()
                     if name != "create" else None)
            self.assertEqual(after, before)
        self.finish(proc)
        return d

    def test_S1_U3_M1_revoke_waits_for_admitted_start(self):
        d = self.start_blocks("revoke")
        with locks.pin(d) as record, record.drain(), record.start():
            self.assertEqual(store.check(d, 103, MANDATE), "invalidated")
        self.assertEqual(store.read(d)["invalidation"]["kind"], "revoked")

    def test_S2_U3_M2_check_waits_for_admitted_start(self):
        d = self.start_blocks("check")
        with locks.pin(d) as record, record.drain(), record.start():
            self.assertEqual(store.check(d, 103, MANDATE), "invalidated")
        self.assertEqual(store.read(d)["invalidation"]["kind"], "mandate_changed")

    def test_S3_other_named_writers_wait_for_start(self):
        for name in ("create", "relinquish", "claim", "replace"):
            with self.subTest(name=name):
                d = self.start_blocks(name)
                record = store.read(d)
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
                d = self.seed(name)
                calls = []

                def flock(fd, mode):
                    calls.append((lock_name(d, fd), mode))
                    return real(fd, mode)

                with mock.patch.object(locks.fcntl, "flock", side_effect=flock):
                    operation(d, name)
                expected = [STATE] if name == "read" else [START, STATE]
                if name in ("claim", "replace"):
                    expected.insert(0, DRAIN)
                self.assertEqual(calls, [(lock, locks.fcntl.LOCK_EX) for lock in expected])
                for lock in expected:
                    self.assertTrue(self.available(d, lock))

    def test_check_retains_start_through_invalidation_persistence(self):
        d = self.seed("check")
        stages = []
        real_write, real_sync = store._write, store._sync_directory

        def writing(path, raw):
            self.assertFalse(self.available(d, START))
            self.assertFalse(self.available(d, STATE))
            stages.append("write")
            return real_write(path, raw)

        def syncing(path):
            self.assertFalse(self.available(d, START))
            self.assertFalse(self.available(d, STATE))
            stages.append("fsync")
            return real_sync(path)

        with mock.patch.object(store, "_write", side_effect=writing), \
                mock.patch.object(store, "_sync_directory", side_effect=syncing):
            self.assertEqual(store.check(d, 102, b"changed"), "mandate_changed")
        self.assertEqual(stages, ["write", "fsync"])
        self.assertEqual(store.check(d, 103, MANDATE), "invalidated")
        self.assertTrue(self.available(d, START))

    def test_read_progresses_while_start_is_held(self):
        d = self.seed("read")
        with locks.pin(d) as record, record.start():
            proc = self.peer(d, "read", STATE)
            proc.wait(timeout=10)
            self.assertEqual(self.line(proc), "acquired:" + STATE)
            self.finish(proc)
        self.assertEqual(store.read(d), held())

    def test_initial_drain_inode_keeps_refused_replacement_nonmutating(self):
        d = self.seed("replacement")

        def snapshot():
            return {str(p.relative_to(d)):
                    (p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns,
                     p.read_bytes() if p.is_file() else None)
                    for p in [d] + list(d.rglob("*"))}

        before = snapshot()
        replacement = grant("b" * 32)
        replacement["issued_at"] += 1
        replacement["expires_at"] += 1
        with self.assertRaisesRegex(store.AuthorityError, "extension requires fresh confirmation"):
            store.replace(d, replacement)
        self.assertEqual(snapshot(), before)
        self.assertEqual((d / DRAIN).stat().st_mode & 0o777, 0o600)

    def test_claim_replace_wait_for_drain_without_start_or_state(self):
        for name in ("claim", "replace"):
            with self.subTest(name=name):
                d = self.seed(name)
                with locks.pin(d) as record, record.drain():
                    proc = self.peer(d, name, DRAIN)
                    self.assertEqual(self.line(proc), "blocked:" + DRAIN)
                    self.assertTrue(self.available(d, START))
                    self.assertTrue(self.available(d, STATE))
                    self.assertEqual(store.read(d), held())
                self.finish(proc)

    def test_revoke_and_check_do_not_wait_for_drain(self):
        for name in ("revoke", "check"):
            with self.subTest(name=name):
                d = self.seed(name)
                with locks.pin(d) as record, record.drain():
                    proc = self.peer(d, name)
                    self.assertEqual(self.line(proc), "acquired:" + START)
                    self.finish(proc)
                    self.assertIsNotNone(store.read(d)["invalidation"])

    def test_nested_named_operations_reuse_start_and_state(self):
        for name in WRITERS + ("read",):
            with self.subTest(name=name):
                d = self.seed(name)
                with locks.pin(d) as record, ExitStack() as stack:
                    stack.enter_context(record.drain(shared=name not in ("claim", "replace")))
                    stack.enter_context(record.start())
                    stack.enter_context(record.state())
                    with mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("reflock")):
                        operation(d, name)
                for lock in (DRAIN, START, STATE):
                    self.assertTrue(self.available(d, lock))

    def test_store_preserves_upgrade_and_cross_record_refusals(self):
        d = self.seed("held")
        other = self.seed("other")
        with locks.pin(d) as record, record.drain():
            for name in ("claim", "replace"):
                with self.subTest(name=name), self.assertRaisesRegex(
                        store.AuthorityError, "^D5 custody change refused: caller holds shared drain$"):
                    operation(d, name)
            for name in WRITERS + ("read",):
                with self.subTest(name=name), self.assertRaisesRegex(
                        store.AuthorityError, "^D5 lock refused: cross-record nesting$"):
                    operation(other, name)

    def test_state_only_same_record_read_is_reentrant(self):
        d = self.seed("read")
        with locks.pin(d) as record, record.state(), \
                mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("reflock")):
            self.assertEqual(store.read(d), held())

    def test_nested_liveness_mutation_is_retained(self):
        for name in ("check", "revoke", "replace", "relinquish", "claim"):
            with self.subTest(name=name):
                d = self.seed(name)
                observed = []
                def probe(_):
                    operation(d, name)
                    observed.append(store.read(d))
                    return "dead"
                with self.assertRaisesRegex(store.AuthorityError,
                        "^record: changed during liveness observation$"):
                    store.claim(d, 103, MANDATE, "outer",
                                ProcessIdentity(44, 9000003), probe)
                self.assertEqual(store.read(d), observed[0])

    def test_threads_share_store_start_guard(self):
        d = self.seed("revoke")
        started, finished = threading.Event(), threading.Event()
        errors = []
        real = locks._Record.start

        def entering(record):
            started.set()
            return real(record)

        def worker():
            try:
                store.revoke(d, 102, "stop")
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        with locks.pin(d) as record:
            with record.start(), mock.patch.object(locks._Record, "start", entering):
                thread = threading.Thread(target=worker, daemon=True)
                thread.start()
                self.assertTrue(started.wait(10), "worker watchdog")
                self.assertFalse(finished.is_set())
                self.assertEqual(store.read(d), held())
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(store.read(d)["invalidation"]["kind"], "revoked")


if __name__ == "__main__":
    unittest.main()
