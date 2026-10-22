"""U3b store integration with real locks and deterministic pipe ordering."""
from contextlib import ExitStack, contextmanager
import ast
import errno
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import types
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
FS_PREFIX = "record: filesystem operation failed; durability is unconfirmed:"


def legacy_store():
    source = subprocess.check_output(["git", "show",
        "bfed06136d9291d8e7727c58b7642c65c4d1c103:"
        "skills/hanig-orchestrate/scripts/d5_authority_store.py"], cwd=str(ROOT))
    old = types.ModuleType("pre_u3b_store")
    exec(compile(source, "pre_u3b_store.py", "exec"), old.__dict__)
    return old


def files(directory):
    # Exclude exactly the two root-level permanent U3a lock files (C3(ii)).
    result = {}
    for root, _, names, fd in os.fwalk(directory):
        for name in names:
            key = str(Path(os.path.relpath(root, directory)) / name)
            if key not in (START, DRAIN):
                with os.fdopen(os.open(name, os.O_RDONLY, dir_fd=fd), "rb") as handle:
                    result[key] = handle.read()
    return result


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

    def test_read_only_liveness_succeeds(self):
        d = self.seed("claim")
        observed = []
        def probe(identity):
            observed.append((identity, store.read(d)))
            return "dead"
        result = store.claim(d, 103, MANDATE, "outer",
                             ProcessIdentity(44, 9000003), probe)
        self.assertEqual(observed, [(ANCHOR, held())])
        self.assertEqual(result["custody"]["generation"], 2)
        self.assertEqual(store.read(d), result)

    def test_host_liveness_has_no_store_mutator_calls(self):
        # Inspect the whole adapter, including liveness's local callees.
        source = (ROOT / "skills/hanig-orchestrate/scripts/d5_host.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [n.name for n in node.names] + [getattr(node, "module", "") or ""]
                self.assertFalse(any("d5_authority_store" in name for name in names))
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                self.assertNotIn(name, WRITERS)

    def test_fake_lock_refusals_and_filesystem_causes(self):
        messages = ("D5 lock refused: unsafe lock file",
                    "D5 lock refused: safe locking unavailable",
                    "D5 lock refused: acquisition order",
                    "D5 lock refused: cross-record nesting",
                    "D5 custody change refused: caller holds shared drain",
                    "D5 lock refused: closed record",
                    "D5 lock refused: drain mode change",
                    "D5 lock refused: invalid shared drain reference")
        for name in WRITERS + ("read",):
            d = self.seed(name)
            stages = ["pin", "state"]
            if name != "read":
                stages.append("start")
            if name in ("claim", "replace"):
                stages.append("drain")
            if name == "create":
                stages.append("_open")
            for stage in stages:
                for message in messages:
                    for caused in (False, True):
                        with self.subTest(name=name, stage=stage, message=message, caused=caused):
                            fault = OSError(errno.EACCES, "synthetic lock open")
                            refusal = locks.LockError(message)
                            def fail():
                                if caused:
                                    raise refusal from fault
                                raise refusal
                            @contextmanager
                            def guard(*args, **kwargs):
                                fail()
                                yield
                            fake = types.SimpleNamespace(LockError=locks.LockError, pin=guard)
                            if stage == "pin":
                                patch = mock.patch.object(store, "locks", fake)
                            else:
                                patch = mock.patch.object(locks._Record, stage,
                                                          lambda *a, **k: fail()) if stage == "_open" else \
                                    mock.patch.object(locks._Record, stage, guard)
                            with patch, self.assertRaises(store.AuthorityError) as caught:
                                operation(d, name)
                            error = caught.exception
                            if caused:
                                self.assertEqual(str(error), FS_PREFIX + " " + str(fault))
                                self.assertIs(error.__cause__, fault)
                            else:
                                self.assertEqual(str(error), message)
                                self.assertIs(error.__cause__, refusal)

    def test_lock_refusal_survives_descriptor_close_failure(self):
        for name in WRITERS + ("read",):
            d = self.seed(name)
            primary = locks.LockError("D5 lock refused: unsafe lock file")
            secondary = OSError(errno.EIO, "synthetic close failure")
            close = os.close
            def closing(fd):
                close(fd)
                raise secondary
            with self.subTest(name=name), \
                    mock.patch.object(locks._Record, "_open", side_effect=primary), \
                    mock.patch.object(os, "close", side_effect=closing), \
                    self.assertRaises(store.AuthorityError) as caught:
                operation(d, name)
            self.assertEqual(str(caught.exception), str(primary))
            self.assertIs(caught.exception.__cause__, primary)
            self.assertEqual(primary.cleanup_errors, (secondary,))

    def test_symlinked_record_directory_is_refused(self):
        for name in WRITERS + ("read",):
            d = self.seed(name)
            alias = self.d / (name + "-alias")
            alias.symlink_to(d, target_is_directory=True)
            before = {p.name: p.read_bytes() for p in d.iterdir()}
            with self.subTest(name=name), self.assertRaises(store.AuthorityError) as caught:
                operation(alias, name)
            self.assertTrue(str(caught.exception).startswith(FS_PREFIX))
            self.assertIsInstance(caught.exception.__cause__, OSError)
            self.assertEqual({p.name: p.read_bytes() for p in d.iterdir()}, before)

    def test_post_open_error_survives_close(self):
        d = self.seed("read")
        close = os.close
        def closing(fd):
            close(fd)
            raise OSError(errno.ENOSPC, "close")
        for primary in (locks.LockError("D5 lock refused: unsafe lock file"),
                        OSError(errno.EIO, "verify")):
            with self.subTest(primary=primary):
                with mock.patch.object(locks, "_safe", side_effect=[True, primary]), \
                        mock.patch.object(os, "close", closing), \
                        self.assertRaises(store.AuthorityError) as caught:
                    store.read(d)
                self.assertIs(caught.exception.__cause__, primary)

    def test_pre_u3b_differential_records_results_and_errors(self):
        old = legacy_store()
        def transcript(module):
            with tempfile.TemporaryDirectory() as directory:
                d = Path(directory)
                fresh = grant("c" * 32)
                fresh["confirmation"]["owner_response"] = "fresh consent"
                successor = ProcessIdentity(43, 9000002)
                calls = [("create", (held(),)), ("create", (held(),)),
                         ("read", ()), ("check", (102, MANDATE)),
                         ("check", (True, MANDATE)), ("revoke", (102, "")),
                         ("claim", (102, MANDATE, "next", successor, lambda _: "alive")),
                         ("relinquish", (102, "native", ANCHOR)),
                         ("claim", (103, MANDATE, "next", successor, lambda _: "dead")),
                         ("claim", (104, MANDATE, "next", successor, lambda _: "dead")),
                         ("replace", (grant("b" * 32),)),
                         ("revoke", (105, "stop")), ("revoke", (106, "again")),
                         ("check", (107, MANDATE)), ("replace", (fresh,)),
                         ("check", (fresh["expires_at"], MANDATE)),
                         ("check", (108, b"changed")), ("check", (109, MANDATE)),
                         ("claim", (110, MANDATE, "next", successor, lambda _: "dead"))]
                result = []
                for name, args in calls:
                    try:
                        value = getattr(module, name)(d, *args)
                        outcome = ("return", module.encode(value) if isinstance(value, dict) else value)
                    except store.AuthorityError as exc:
                        self.assertNotIsInstance(exc.__cause__, OSError)
                        outcome = (type(exc),)
                    records = (files(d) if outcome[0] != "return" else
                               {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*.json")})
                    result.append((name, outcome, records))
                return result
        self.assertEqual(transcript(old), transcript(store))
        # Same missing D: both refuse, irrespective of error text.
        for name in WRITERS + ("read",):
            errors = []
            for module in (old, store):
                with mock.patch.dict(operation.__globals__, store=module), \
                        self.assertRaises(store.AuthorityError) as caught:
                    operation(self.d / "missing", name)
                error = caught.exception
                self.assertIsInstance(error.__cause__, OSError)
                errors.append((type(error), error.__cause__.errno))
            self.assertEqual(errors[0], errors[1], name)
            self.assertEqual(errors[0][1], errno.ENOENT)
        # C3 declared delta: 0400 locks are excluded from identity comparison.
        # U3a repairs owner read/write permissions before the store operation.
        d = self.seed("permissions")
        (d / STATE).chmod(0o400)
        self.legacy_permissions(d, old)

    def legacy_permissions(self, d, old):
        if os.geteuid() != 0:
            with self.assertRaises(store.AuthorityError) as caught:
                old.revoke(d, 102, "stop")
            self.assertEqual(caught.exception.__cause__.errno, errno.EACCES)
        else:
            # Root may open a 0400 lock: legacy revoke legitimately succeeds.
            result = old.revoke(d, 102, "stop")
            self.assertEqual(result["invalidation"]["kind"], "revoked")
            self.assertEqual(old.read(d), result)
        self.assertEqual((d / STATE).stat().st_mode & 0o777, 0o400)

    def test_C3_refused_files_match_legacy(self):
        old = legacy_store()
        for trigger in ("reason", "missing", "mandate", "sync", "archive"):
            snapshots = []
            for module in (old, store):
                with self.subTest(trigger=trigger, module=module.__name__), \
                        tempfile.TemporaryDirectory() as tmp:
                    d = Path(tmp)
                    if trigger != "missing":
                        old.create(d, held())
                    with ExitStack() as stack:
                        if trigger in ("sync", "archive"):
                            real = module._sync_directory
                            def sync(path):
                                if trigger == "sync" or path.name == "superseded":
                                    raise OSError(errno.EIO, "synthetic directory sync")
                                real(path)
                            stack.enter_context(mock.patch.object(module, "_sync_directory", sync))
                        with self.assertRaises(store.AuthorityError):
                            if trigger == "mandate":
                                module.claim(d, 103, b"changed", "next",
                                             ProcessIdentity(43, 9000002), lambda _: "dead")
                            elif trigger == "archive":
                                module.replace(d, grant("b" * 32))
                            else:
                                module.revoke(d, 103, "" if trigger == "reason" else "stop")
                    snapshots.append(files(d))
            self.assertEqual(snapshots[0], snapshots[1], trigger)

    def test_C3_declared_delta_0400_lock_is_repaired_before_revoke(self):
        d = self.seed("permissions")
        (d / STATE).chmod(0o400)
        result = store.revoke(d, 102, "stop")
        self.assertEqual(result["invalidation"]["kind"], "revoked")
        self.assertEqual(store.read(d), result)
        self.assertEqual((d / STATE).stat().st_mode & 0o777, 0o600)

    def test_review_callback_restore_is_refused(self):
        d = self.seed("claim")
        raw = (d / store.RECORD_NAME).read_bytes()
        def probe(_):
            changed = store.decode(raw)
            changed["confirmation"]["owner_response"] += " transient"
            store.write_bytes(d / store.RECORD_NAME, store.encode(changed))
            store.write_bytes(d / store.RECORD_NAME, raw)
            return "dead"
        with self.assertRaisesRegex(store.AuthorityError,
                "^record: changed during liveness observation$"):
            store.claim(d, 103, MANDATE, "outer", ProcessIdentity(44, 9000003), probe)

    def test_callback_bytes_and_removal_are_refused(self):
        for remove in (False, True):
            d = self.seed("bytes-" + str(remove))
            path = d / store.RECORD_NAME
            raw = path.read_bytes()
            def probe(_):
                if remove:
                    path.unlink()
                else:
                    path.write_bytes(raw + b" \n")  # Same inode and decoded record.
                return "dead"
            with self.subTest(remove=remove), self.assertRaisesRegex(store.AuthorityError,
                    "^record: changed during liveness observation$"):
                store.claim(d, 103, MANDATE, "outer", ProcessIdentity(44, 9000003), probe)

    def test_review_privileged_open_keeps_differential_valid(self):
        # Emulate privileged access without elevation. STATE fds serve flock,
        # not writes, so a read-only descriptor supplies the required access.
        real = os.open
        def privileged(file, flags, *args, **kwargs):
            try:
                return real(file, flags, *args, **kwargs)
            except PermissionError:
                if (not kwargs.get("dir_fd") and str(file).endswith("/authority.lock")
                        and os.stat(file).st_mode & 0o777 == 0o400):
                    return real(file, os.O_RDONLY | os.O_CLOEXEC)
                raise
        d = self.seed("privileged")
        (d / STATE).chmod(0o400)
        with mock.patch.object(os, "open", side_effect=privileged), \
                mock.patch.object(os, "geteuid", return_value=0):
            self.legacy_permissions(d, legacy_store())

    def test_review_long_path_refusal_matches_legacy(self):
        old = legacy_store()
        d = self.d.resolve()
        target = os.pathconf(d, "PC_PATH_MAX") - 6
        while target - len(str(d)) > 202:
            d = d / ("x" * 200)
            d.mkdir()
        d = d / ("x" * (target - len(str(d)) - 1))
        d.mkdir()
        errors = []
        for module in (old, store):
            with self.assertRaises(store.AuthorityError) as caught:
                module.create(d, held())
            error = caught.exception
            errors.append((type(error), error.__cause__.errno, files(d)))
        self.assertEqual(errors[0], errors[1])

    def test_C0_permission_matrix(self):
        message = ("record: directory must be owned by the caller and grant its "
                   "owner read, write and search permission")
        old = legacy_store()
        for mode in (0o700, 0o500, 0o300):
            for name in WRITERS + ("read",):
                snapshots = []
                for module in ((old, store) if mode == 0o700 else (store,)):
                    with self.subTest(mode=oct(mode), name=name, module=module.__name__), \
                            tempfile.TemporaryDirectory() as tmp:
                        d = Path(tmp)
                        path = d / store.RECORD_NAME
                        raw = store.encode(held())
                        path.write_bytes(raw)
                        d.chmod(mode)
                        try:
                            with mock.patch.dict(operation.__globals__, store=module):
                                if mode == 0o700:
                                    try:
                                        value = operation(d, name)
                                        outcome = ("return", module.encode(value)
                                                   if isinstance(value, dict) else value)
                                    except store.AuthorityError as exc:
                                        outcome = (type(exc),)
                                    snapshots.append((outcome, files(d)))
                                else:
                                    with mock.patch.object(locks, "pin") as pin, \
                                            self.assertRaises(store.AuthorityError) as caught:
                                        operation(d, name)
                                    self.assertEqual(str(caught.exception), message)
                                    pin.assert_not_called()
                                    self.assertEqual(path.read_bytes(), raw)
                        finally:
                            d.chmod(0o700)
                        if mode != 0o700:
                            self.assertEqual(list(d.iterdir()), [path])
                if mode == 0o700:
                    self.assertEqual(snapshots[0], snapshots[1])

    def test_C0_foreign_owner_and_nondirectory(self):
        real = os.lstat
        for name in WRITERS + ("read",):
            for kind in ("owner", "file"):
                with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as tmp:
                    d = Path(tmp) / "record"
                    if kind == "file":
                        d.write_bytes(b"unchanged")
                    else:
                        d.mkdir(mode=0o700)
                        (d / store.RECORD_NAME).write_bytes(store.encode(held()))
                    before = d.read_bytes() if kind == "file" else files(d)
                    def lookup(path, *args, **kwargs):
                        info = real(path, *args, **kwargs)
                        if Path(path) == d and kind == "owner":
                            return types.SimpleNamespace(st_mode=info.st_mode,
                                                         st_uid=os.geteuid() + 1)
                        return info
                    with mock.patch.object(os, "lstat", side_effect=lookup), \
                            mock.patch.object(locks, "pin") as pin, \
                            self.assertRaises(store.AuthorityError) as caught:
                        operation(d, name)
                    self.assertEqual(str(caught.exception),
                        "record: directory must be owned by the caller and grant its "
                        "owner read, write and search permission")
                    pin.assert_not_called()
                    self.assertEqual(d.read_bytes() if kind == "file" else files(d), before)

    def test_C0_precedes_create_replace_argument_validation(self):
        self.d.chmod(0o500)
        try:
            for name in ("create", "replace"):
                with self.subTest(name=name), self.assertRaises(store.AuthorityError) as caught:
                    getattr(store, name)(self.d, {})
                self.assertEqual(str(caught.exception),
                    "record: directory must be owned by the caller and grant its "
                    "owner read, write and search permission")
        finally:
            self.d.chmod(0o700)
        self.assertEqual(list(self.d.iterdir()), [])

    def test_C2FS_denied_lock_creation_preserves_original_error(self):
        opening = os.open
        for name in WRITERS + ("read",):
            stages = [STATE] if name == "read" else [START, STATE]
            if name in ("create", "claim", "replace"):
                stages.append(DRAIN)
            for lock in stages:
                with self.subTest(name=name, lock=lock), tempfile.TemporaryDirectory() as tmp:
                    d = Path(tmp)
                    d.chmod(0o700)
                    raw = store.encode(held())
                    (d / store.RECORD_NAME).write_bytes(raw)
                    denied = PermissionError(errno.EACCES, "synthetic lock creation denial")
                    def fail(path, *args, **kwargs):
                        if path == lock:
                            raise denied
                        return opening(path, *args, **kwargs)
                    with mock.patch.object(os, "open", side_effect=fail), \
                            self.assertRaises(store.AuthorityError) as caught:
                        operation(d, name)
                    self.assertEqual((d / store.RECORD_NAME).read_bytes(), raw)
                    self.assertIs(caught.exception.__cause__, denied)
                    self.assertEqual(str(caught.exception), FS_PREFIX + " " + str(denied))

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
