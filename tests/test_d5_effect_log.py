"""U3c consumer and mutation tests; barriers/hooks provide ordering."""
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
import copy
import errno
import fcntl
import json
import os
import signal
import stat
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_effect_log as E
import d5_authority as authority
import d5_authority_store as store
import d5_host as host
from tests.fixtures.d5.ready import ready_installation, missing_installation, classification_only
from tests.test_d5_authority import grant


class TestEffectLog(unittest.TestCase):
    eq = unittest.TestCase.assertEqual
    raises = unittest.TestCase.assertRaisesRegex
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.d = Path(temp.name)
        self.reg = {"registration_id": "a" * 32, "record_dir": str(self.d)}
        self.entry = ready_installation(self.d, state_dir=self.d / "state", record_dir=self.d)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.lookup = self.stack.enter_context(mock.patch.object(
            E.registry, "lookup", return_value=self.reg))
        self.args = ("checkout", "plan", self.entry["state_dir"])
        self.req = dict(record_id="b" * 32, custody={"generation": 1,
            "session_id": "session", "anchor_pid": 123, "anchor_start_us": 101},
            kind="test-effect", scope={"plans": ["plan"], "repositories": ["repo"],
            "actions": ["start"], "trackers": []}, correlation={"request_id": "request-1"})
        self.ok = {"status": "succeeded", "detail": "result-1"}
        self.bad = {"status": "failed", "detail": "result-2"}

    def prepare(self, **overrides):
        return E.prepare(*self.args, **dict(self.req, **overrides))

    def receipt(self):
        return E._classify(self.prepare(), "submitted")

    def path(self, handle):
        return self.d / E.DIRECTORY / (handle.effect_id + ".json")

    def entry(self, handle):
        return json.loads(self.path(handle).read_bytes())

    def reject(self, message=E.INVALID):
        return self.raises(E.EffectError, "^" + message + "$")

    def denied(self, call, *args, error=E.INVALID, **kwargs):
        with self.reject(error):
            call(*args, **kwargs)

    def test_A0_inactive_no_io(self):
        handle = self.prepare()
        receipt = E._classify(handle, "submitted")
        with missing_installation(self.entry), classification_only(self.entry["state_dir"]), ExitStack() as stack:
            for target, name in ((E.registry, "lookup"), (E.locks, "pin"),
                                 (E.registry.subprocess, "Popen")):
                stack.enter_context(mock.patch.object(target, name,
                    side_effect=AssertionError("inactive performed work")))
            for call in (self.prepare, lambda: E._classify(handle, "submitted"),
                         lambda: E.complete(receipt, self.ok), lambda: E.unresolved(*self.args)):
                with self.subTest(call=call):
                    self.denied(call, error="D5 inactive")

    def test_C1_destination_and_secrets(self):
        intent = self.prepare()
        self.lookup.assert_called_once_with(*self.args)
        self.assertRegex(intent.effect_id, r"^[0-9a-f]{32}$")
        self.eq(len(intent._secret), 32)
        self.eq(self.path(intent).parent, self.d / "authority.effects")
        row = self.entry(intent)
        self.eq(row["state"], "attempting")
        self.assertIsNone(row["outcome"])
        self.eq(row["binding"]["record_id"], self.req["record_id"])
        self.eq(row["binding"]["custody"], self.req["custody"])
        self.assertNotIn(intent._secret.hex(), self.path(intent).read_text())
        self.assertNotIn(repr(intent._secret), repr(intent))
        with self.assertRaises(FrozenInstanceError):
            intent.effect_id = "c" * 32
        self.req["scope"]["plans"].append("changed")
        self.req["custody"]["generation"] = 99
        self.req["correlation"]["request_id"] = "changed"
        receipt = E._classify(intent, "submitted")
        E.complete(receipt, self.ok)
        self.eq(self.entry(receipt)["binding"], row["binding"])

    def test_C1_forgery_and_bindings(self):
        receipt, other = self.receipt(), self.receipt()
        path = self.path(receipt)
        forged = [replace(receipt, _secret=b"x" * 32), replace(receipt, effect_id=other.effect_id),
                  replace(receipt, effect_id="../outside"), replace(receipt, _secret=b""), object()]
        saved = self.entry(receipt)
        changes = dict(record_id="c" * 32, registration_id="c" * 32,
            record_key=[1, 2], effects_key=[1, 2], kind="other",
            custody=dict(saved["binding"]["custody"], generation=2),
            scope=dict(saved["binding"]["scope"], plans=["other"]),
            correlation={"request_id": "other"})
        for key, value in changes.items():
            binding = dict(saved["binding"], **{key: value})
            forged.append(replace(receipt, _binding=E._encode(binding)))
            tampered = copy.deepcopy(saved)
            tampered["binding"] = binding
            path.write_bytes(E._encode(tampered))
            with self.subTest(binding=key):
                self.denied(E.complete, receipt, self.ok)
            with self.subTest(verifier=key):
                self.denied(E.complete, forged[-1], self.ok)
            path.write_bytes(E._encode(saved))
        for handle in forged:
            with self.subTest(handle=handle):
                self.denied(E.complete, handle, self.ok)
        self.eq(self.entry(receipt), saved)

    def test_C2_unsubmitted_refusals(self):
        admitted = self.prepare()
        E._classify(admitted, "submitted")
        self.denied(E.complete, admitted, self.ok, error=E.NOT_SUBMITTED)
        for state in ("attempting", "unknown", "not_submitted"):
            intent = self.prepare()
            if state != "attempting":
                E._classify(intent, state)
            self.denied(E.complete, intent, self.ok, error=E.NOT_SUBMITTED)
            forged = E.Receipt(intent.effect_id, intent._directory, intent._binding, intent._secret, intent._state_dir)
            with self.subTest(state=state):
                self.denied(E.complete, forged, self.ok, error=E.NOT_SUBMITTED)
            self.eq(self.entry(intent)["state"], state)

    def test_C3_terminal_repeats(self):
        receipt = self.receipt()
        path = self.path(receipt)
        self.eq(E.complete(receipt, self.ok), self.ok)
        before = path.stat(), path.read_bytes()
        with mock.patch.object(E, "_write", side_effect=AssertionError("rewritten")):
            self.eq(E.complete(receipt, dict(reversed(list(self.ok.items())))), self.ok)
            self.denied(E.complete, receipt, self.bad, error=E.CONFLICT)
        after = path.stat(), path.read_bytes()
        self.eq((before[0].st_ino, before[0].st_mtime_ns, before[1]),
                         (after[0].st_ino, after[0].st_mtime_ns, after[1]))

    def test_C4_reconciliation(self):
        intent = self.prepare()
        for error in (RuntimeError("unclassified"), KeyboardInterrupt()):
            attempt = self.prepare()
            self.assertIsNone(E._classify(attempt, error))
            self.eq(self.entry(attempt)["state"], "unknown")
        submitted = self.prepare()
        receipt = E._classify(submitted, "submitted")
        E._classify(submitted, RuntimeError("response lost"))
        self.denied(E.complete, receipt, self.ok, error=E.NOT_SUBMITTED)
        E._classify(self.prepare(), "not_submitted")
        E.complete(self.receipt(), self.ok)
        rows = E.unresolved(*self.args)
        self.eq(len(rows), 4)
        self.assertTrue(all(r["reconciliation_required"] for r in rows))
        self.assertIn(intent.effect_id, [r["effect_id"] for r in rows])
        self.assertTrue(all("receipt_verifier" not in r for r in rows))

    def test_C5_authority_changes(self):
        record = grant()
        store.create(self.d, record)
        receipt = self.receipt()
        store.revoke(self.d, record["expires_at"] + 1, "revoked after start")
        successor = copy.deepcopy(record)
        successor["grant_id"] = "e" * 32
        successor["confirmation"]["owner_response"] = "fresh consent"
        store.replace(self.d, successor)
        successor["custody"] = {"generation": 9, "session_id": "successor"}
        successor["expires_at"] = 1
        successor["mandate_sha256"] = "f" * 64
        (self.d / store.RECORD_NAME).write_text(json.dumps(successor))
        with ExitStack() as stack:
            for target, names in ((E.registry, ("lookup",)), (store, ("read", "check", "claim")),
                    (authority, ("validate", "evaluate")), (host, ("session_anchor", "liveness"))):
                for name in names:
                    stack.enter_context(mock.patch.object(target, name,
                        side_effect=AssertionError("live authority consulted")))
            E.complete(receipt, self.ok)
        self.eq(self.entry(receipt)["outcome"], self.ok)

    def test_C1_invalid_entries(self):
        receipt = self.receipt()
        path = self.path(receipt)
        saved = path.read_bytes()
        for key, value in (("state", "bogus"), ("state", []), ("outcome", self.ok),
                ("schema_version", True), ("receipt_verifier", "x"), ("effect_id", "c" * 32)):
            row = json.loads(saved)
            row[key] = value
            path.write_bytes(E._encode(row))
            with self.subTest(field=key, value=value):
                self.denied(E.complete, receipt, self.ok)
        for raw in (b'{', b'[]', b'\xff', saved.replace(b'"state":', b'"state":"unknown","state":')):
            path.write_bytes(raw)
            self.denied(E.complete, receipt, self.ok)
        path.write_bytes(saved)
        for bad in ({}, None, {"status": "unknown", "detail": "x"},
                    {"status": "succeeded", "detail": 1}, dict(self.ok, path="elsewhere")):
            with self.subTest(outcome=bad):
                self.denied(E.complete, receipt, bad)
        self.eq(path.read_bytes(), saved)

    def test_C1_filesystem_binding(self):
        receipt = self.receipt()
        path = self.path(receipt)
        raw = path.read_bytes()
        outside = self.d / "outside"
        outside.write_bytes(raw)
        outside.chmod(0o600)
        for variant in ("symlink", "fifo", "hardlink", "writable"):
            path.unlink()
            if variant == "symlink":
                path.symlink_to(outside)
            elif variant == "fifo":
                os.mkfifo(path)
            elif variant == "hardlink":
                os.link(outside, path)
            else:
                path.write_bytes(raw)
                path.chmod(0o666)
            with self.subTest(variant=variant):
                self.denied(E.complete, receipt, self.ok)
        path.unlink()
        path.write_bytes(raw)
        path.chmod(0o600)
        self.eq(outside.read_bytes(), raw)
        directory = path.parent
        moved = self.d / "moved"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        self.denied(E.complete, receipt, self.ok)
        directory.unlink()
        directory.mkdir(mode=0o700)
        path.write_bytes(raw)
        path.chmod(0o600)
        self.denied(E.complete, receipt, self.ok)
        other = self.d / "different-record"
        other.mkdir(mode=0o700)
        (other / E.DIRECTORY).mkdir(mode=0o700)
        (other / E.DIRECTORY / path.name).write_bytes(raw)
        (other / E.DIRECTORY / path.name).chmod(0o600)
        self.denied(E.complete, replace(receipt, _directory=str(other)), self.ok)

    def test_E1_serialization(self):
        real_write = E._write
        def writing(*args, **kwargs):
            fd = os.open(self.d / "authority.lock", os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            return real_write(*args, **kwargs)
        with mock.patch.object(E, "_write", side_effect=writing):
            receipt = self.receipt()
            barrier = threading.Barrier(2)
            def finish(outcome):
                barrier.wait(timeout=10)
                try:
                    E.complete(receipt, outcome)
                    return "ok"
                except E.EffectError as exc:
                    return str(exc)
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(finish, result) for result in (self.ok, self.bad)]
                results = [f.result(timeout=10) for f in futures]
            self.assertCountEqual(results, ["ok", E.CONFLICT])

    def test_E2_durability(self):
        intent = self.prepare()
        saved = self.path(intent).read_bytes()
        with mock.patch.object(E.secrets, "token_hex", return_value=intent.effect_id):
            self.denied(self.prepare, error=E.DURABILITY)
        self.eq(self.path(intent).read_bytes(), saved)
        receipt = E._classify(intent, "submitted")
        path = self.path(receipt)
        prior = path.read_bytes()
        with mock.patch.object(os, "replace", side_effect=OSError("pre-publication")):
            self.denied(E.complete, receipt, self.ok, error=E.DURABILITY)
        self.eq(path.read_bytes(), prior)
        real_sync = os.fsync
        def fail_directory(fd):
            if E._key(fd) == E._key(dirfd):
                raise OSError("post-publication")
            real_sync(fd)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            with mock.patch.object(os, "fsync", side_effect=fail_directory):
                self.denied(E.complete, receipt, self.ok, error=E.DURABILITY)
                self.eq(self.entry(receipt)["state"], "completed")
                self.denied(E.complete, receipt, self.ok, error=E.DURABILITY)
            E.complete(receipt, self.ok)
        finally:
            os.close(dirfd)
        self.eq(list(path.parent.glob(".effect-*")), [])

    def test_E3_classification_transitions(self):
        for initial in ("submitted", "unknown", "not_submitted", "completed"):
            for target in ("submitted", "unknown", "not_submitted"):
                intent = self.prepare()
                receipt = E._classify(intent, "submitted" if initial == "completed" else initial)
                if initial == "completed":
                    E.complete(receipt, self.ok)
                allowed = target == initial or (initial, target) in (
                    ("completed", "submitted"), ("submitted", "unknown"))
                with self.subTest(initial=initial, target=target):
                    if allowed:
                        E._classify(intent, target)
                    else:
                        self.denied(E._classify, intent, target, error=E.CONFLICT)
        for invalid in (None, True, [], "completed", "attempting"):
            self.denied(E._classify, self.prepare(), invalid)

    def test_E4_schema_and_umask(self):
        for field, value in (("record_id", "wrong"), ("kind", ""), ("custody", {}),
                             ("scope", "all"), ("correlation", {})):
            with self.subTest(field=field):
                self.denied(self.prepare, **{field: value})
        saved = os.umask(0o777)
        try:
            receipt = self.receipt()
            E.complete(receipt, self.ok)
        finally:
            os.umask(saved)
        self.eq(self.path(receipt).stat().st_mode & 0o777, 0o600)

    def test_created_modes_under_umasks(self):
        for mask in (0o000, 0o022, 0o077, 0o277, 0o777):
            with self.subTest(umask=oct(mask)), tempfile.TemporaryDirectory() as directory:
                self.reg["record_dir"] = directory
                saved = os.umask(mask)
                try:
                    intent = self.prepare()
                    effects = Path(directory) / E.DIRECTORY
                    path = effects / (intent.effect_id + ".json")
                    self.eq(stat.S_IMODE(effects.stat().st_mode), 0o700)
                    self.eq(stat.S_IMODE(path.stat().st_mode), 0o600)
                    receipt = E._classify(intent, "submitted")
                    self.eq(stat.S_IMODE(path.stat().st_mode), 0o600)
                    E.complete(receipt, self.ok)
                    self.eq(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.eq(list(effects.glob(".effect-*")), [])
                finally:
                    os.umask(saved)

    def test_new_nonprivate_modes_repaired_before_check(self):
        real_open, real_fchmod, real_safe = os.open, os.fchmod, E._safe
        created, checked = [], []

        def opening(path, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == E.DIRECTORY and not created:
                real_fchmod(fd, 0o755)  # synthetic new-directory ACL mode
                created.append((os.fstat(fd).st_ino, 0o700))
            elif flags & os.O_CREAT and not str(path).endswith(".lock"):
                self.assertTrue(flags & os.O_EXCL)
                real_fchmod(fd, 0o644)
                created.append((os.fstat(fd).st_ino, 0o600))
            return fd

        def checking(fd, directory=False):
            real_safe(fd, directory=directory)
            info = os.fstat(fd)
            checked.append((info.st_ino, stat.S_IMODE(info.st_mode)))

        with mock.patch.object(os, "open", side_effect=opening), \
                mock.patch.object(E, "_safe", side_effect=checking):
            intent = self.prepare()
            self.eq(stat.S_IMODE(self.path(intent).parent.stat().st_mode), 0o700)
            self.eq(stat.S_IMODE(self.path(intent).stat().st_mode), 0o600)
            receipt = E._classify(intent, "submitted")
            E.complete(receipt, self.ok)
        self.eq(len(created), 4)  # directory, intent, two atomic replacements
        for identity_and_mode in created:
            self.assertIn(identity_and_mode, checked)
        self.eq(stat.S_IMODE(self.path(receipt).stat().st_mode), 0o600)

    def test_existing_nonprivate_files_and_directories_refused(self):
        receipt = self.receipt()
        path = self.path(receipt)
        raw = path.read_bytes()
        for target, owner in ((path, 0o600), (path.parent, 0o700), (self.d, 0o700)):
            for bits in (0o044, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001):
                with self.subTest(target=target.name, bits=oct(bits)):
                    mode = owner | bits
                    target.chmod(mode)
                    inode = target.stat().st_ino
                    try:
                        self.denied(E.complete, receipt, self.ok)
                        if target != path:
                            self.denied(self.prepare)
                            self.denied(E.unresolved, *self.args)
                        else:
                            self.eq(E.unresolved(*self.args), [{"file": path.name,
                                "error": E.INVALID, "reconciliation_required": True}])
                        self.eq(stat.S_IMODE(target.stat().st_mode), mode)
                        self.eq(target.stat().st_ino, inode)
                        self.eq(path.read_bytes(), raw)
                    finally:
                        target.chmod(owner)


    def test_E5_torn_intent(self):
        intent = self.prepare()
        E._classify(intent, "unknown")
        pid = os.fork()
        if pid == 0:
            signal.alarm(10)
            encode = E._encode
            def crash(value):
                if isinstance(value, dict) and "schema_version" in value:
                    os._exit(99)
                return encode(value)
            E._encode = crash
            try:
                self.prepare()
            finally:
                os._exit(1)
        self.eq(os.waitpid(pid, 0)[1], 99 << 8)
        torn = next(p for p in (self.d / E.DIRECTORY).glob("*.json") if not p.stat().st_size)
        rows = E.unresolved(*self.args)
        self.eq(next(r for r in rows if r.get("effect_id") == intent.effect_id)["state"], "unknown")
        self.eq(next(r for r in rows if "error" in r),
                {"file": torn.name, "error": E.INVALID, "reconciliation_required": True})
        self.eq(torn.read_bytes(), b"")



class TestEffectCleanup(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def exercise(self, site, primary, cleanup_fails):
        cleanup = OSError(errno.EIO, "cleanup EIO")
        calls = []
        access_fd = None
        real_close, real_fdopen = os.close, os.fdopen

        def close(fd):
            real_close(fd)
            if site == "access" and fd != access_fd:
                return
            calls.append("close")
            if cleanup_fails:
                raise cleanup

        def unlink(*args, **kwargs):
            calls.append("unlink")
            if cleanup_fails:
                raise cleanup

        def fdopen(*args):
            handle = real_fdopen(*args)

            class FileFault:
                # Delegate real I/O without assigning methods on a built-in file.
                def __getattr__(self, name):
                    return getattr(handle, name)

                def close(self):
                    handle.close()
                    calls.append("handle.close")
                    if cleanup_fails:
                        raise cleanup

                def write(self, data):
                    if site == "write" and primary is not None:
                        raise primary
                    return handle.write(data)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self.close()

            return FileFault()

        with E._access(self.directory, create=True) as (record, fd):
            name = "fixture.json"
            E._write(fd, name, {})
            with ExitStack() as stack:
                if site in ("write", "load"):
                    stack.enter_context(mock.patch.object(os, "fdopen", side_effect=fdopen))
                elif site == "unlink":
                    stack.enter_context(mock.patch.object(os, "unlink", side_effect=unlink))
                else:
                    stack.enter_context(mock.patch.object(os, "close", side_effect=close))
                if primary is not None and site not in ("access", "write"):
                    target = {"write": "_encode", "load": "_decode",
                              "confirm": "_sync", "unlink": "_encode"}[site]
                    stack.enter_context(mock.patch.object(E, target, side_effect=primary))
                # _load needs a valid schema only when decoding succeeds.
                if site == "load" and primary is None:
                    stack.enter_context(mock.patch.object(E, "_keys", side_effect=StopIteration("after close")))
                try:
                    try:
                        raise ValueError("ambient handled exception")
                    except ValueError:
                        if site == "access":
                            # Only fail the effects-directory close, not outer locks.
                            with E._access(self.directory) as (_, target_fd):
                                access_fd = target_fd
                                if primary is not None:
                                    raise primary
                        elif site == "write" or site == "unlink":
                            E._write(fd, name, {})
                        elif site == "load":
                            E._load(fd, name, record)
                        else:
                            E._confirm(fd, name)
                except BaseException as exc:
                    result = exc
                else:
                    result = None
            self.assertEqual(len(calls), 1, calls)
        return result, cleanup

    def check_site(self, site):
        # Compare the same operation with successful and failing cleanup.
        # Test both direct propagation and the module's existing OSError wrappers.
        for primary in (KeyboardInterrupt("primary body"), OSError(errno.EIO, "ledger write primary"), None):
            if primary is not None:
                primary.__cause__ = ValueError("original cause")
            with self.subTest(site=site, primary=type(primary).__name__):
                before, _ = self.exercise(site, primary, False)
                after, cleanup = self.exercise(site, primary, True)
                if primary is not None:
                    self.assertIs(type(after), type(before))
                    self.assertEqual(str(after), str(before))
                    self.assertIs(after.__cause__, before.__cause__)
                    if before is primary:
                        self.assertIs(after, primary)
                    else:
                        self.assertIs(after.__cause__, primary)
                    self.assertEqual(str(primary.__cause__), "original cause")
                elif site in ("access", "write"):
                    self.assertIsNone(before)
                    self.assertIsInstance(after, E.EffectError)
                    self.assertEqual(str(after), E.INVALID if site == "access" else E.DURABILITY)
                    self.assertIs(after.__cause__, cleanup)
                else:
                    if site == "load":
                        self.assertIsInstance(before, StopIteration)
                    else:
                        self.assertIsNone(before)
                    self.assertIs(after, cleanup)
                    self.assertIsNone(after.__cause__)

    def test_access_cleanup(self):
        self.check_site("access")

    def test_write_close_cleanup(self):
        self.check_site("write")

    def test_load_close_cleanup(self):
        self.check_site("load")

    def test_temporary_unlink_cleanup(self):
        self.check_site("unlink")

    def test_confirm_cleanup(self):
        self.check_site("confirm")


if __name__ == "__main__":
    unittest.main()
