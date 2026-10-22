"""U3c consumer and mutation tests; barriers/hooks provide ordering."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
import copy
import fcntl
import json
import os
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
from tests.test_d5_authority import grant


class TestEffectLog(unittest.TestCase):
    eq = unittest.TestCase.assertEqual
    raises = unittest.TestCase.assertRaisesRegex
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.d = Path(temp.name)
        self.reg = {"registration_id": "a" * 32, "record_dir": str(self.d)}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(E.registry, "d5_active", return_value=True))
        self.lookup = self.stack.enter_context(mock.patch.object(
            E.registry, "lookup", return_value=self.reg))
        self.args = ("checkout", "plan", "state")
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

    def test_A0_inactive_precedes_filesystem_and_process_work(self):
        with mock.patch.object(E.registry, "d5_active", return_value=False), ExitStack() as stack:
            for target, name in ((E.registry, "lookup"), (E.locks, "pin"),
                                 (os, "open"), (os, "mkdir"), (os, "lstat"),
                                 (E.registry.subprocess, "Popen")):
                stack.enter_context(mock.patch.object(target, name,
                    side_effect=AssertionError("inactive performed work")))
            for call in (self.prepare, lambda: E._classify(None, "submitted"),
                         lambda: E.complete(None, self.ok), lambda: E.unresolved(*self.args)):
                with self.subTest(call=call), self.reject("D5 inactive"):
                    call()

    def test_C1_destination_secret_and_immutable_request(self):
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

    def test_C1_forged_cross_operation_and_binding_variants(self):
        receipt, other = self.receipt(), self.receipt()
        path = self.path(receipt)
        forged = [replace(receipt, _secret=b"x" * 32), replace(receipt, effect_id=other.effect_id),
                  replace(receipt, effect_id="../outside"), replace(receipt, _secret=b""), object()]
        saved = self.entry(receipt)
        for key in saved["binding"]:
            binding = copy.deepcopy(saved["binding"])
            value = binding[key]
            if isinstance(value, str):
                binding[key] = "c" * 32
            elif isinstance(value, list):
                binding[key] = [1, 2]
            elif key == "custody":
                value["generation"] += 1
            elif key == "scope":
                value["plans"] = ["other"]
            else:
                value["request_id"] = "other"
            forged.append(replace(receipt, _binding=E._encode(binding)))
            tampered = copy.deepcopy(saved)
            tampered["binding"] = binding
            path.write_bytes(E._encode(tampered))
            with self.subTest(binding=key), self.reject():
                E.complete(receipt, self.ok)
            with self.subTest(verifier=key), self.reject():
                E.complete(forged[-1], self.ok)
            path.write_bytes(E._encode(saved))
        for handle in forged:
            with self.subTest(handle=handle), self.reject():
                E.complete(handle, self.ok)
        self.eq(self.entry(receipt), saved)

    def test_C2_intent_and_unsubmitted_states_never_complete(self):
        admitted = self.prepare()
        E._classify(admitted, "submitted")
        with self.reject(E.NOT_SUBMITTED):
            E.complete(admitted, self.ok)
        for state in ("attempting", "unknown", "not_submitted"):
            intent = self.prepare()
            if state != "attempting":
                E._classify(intent, state)
            with self.reject(E.NOT_SUBMITTED):
                E.complete(intent, self.ok)
            forged = E.Receipt(intent.effect_id, intent._directory, intent._binding, intent._secret)
            with self.subTest(state=state), self.reject(E.NOT_SUBMITTED):
                E.complete(forged, self.ok)
            self.eq(self.entry(intent)["state"], state)

    def test_C3_terminal_idempotence_and_conflict(self):
        receipt = self.receipt()
        path = self.path(receipt)
        self.eq(E.complete(receipt, self.ok), self.ok)
        before = path.stat(), path.read_bytes()
        with mock.patch.object(E, "_write", side_effect=AssertionError("rewritten")):
            self.eq(E.complete(receipt, dict(reversed(list(self.ok.items())))), self.ok)
            with self.reject(E.CONFLICT):
                E.complete(receipt, self.bad)
        after = path.stat(), path.read_bytes()
        self.eq((before[0].st_ino, before[0].st_mtime_ns, before[1]),
                         (after[0].st_ino, after[0].st_mtime_ns, after[1]))

    def test_C4_unknown_exception_and_abandoned_intent_are_exposed(self):
        intent = self.prepare()
        for error in (RuntimeError("unclassified"), KeyboardInterrupt()):
            attempt = self.prepare()
            self.assertIsNone(E._classify(attempt, error))
            self.eq(self.entry(attempt)["state"], "unknown")
        submitted = self.prepare()
        receipt = E._classify(submitted, "submitted")
        E._classify(submitted, RuntimeError("response lost"))
        with self.reject(E.NOT_SUBMITTED):
            E.complete(receipt, self.ok)
        E._classify(self.prepare(), "not_submitted")
        E.complete(self.receipt(), self.ok)
        rows = E.unresolved(*self.args)
        self.eq(len(rows), 4)
        self.assertTrue(all(r["reconciliation_required"] for r in rows))
        self.assertIn(intent.effect_id, [r["effect_id"] for r in rows])
        self.assertTrue(all("receipt_verifier" not in r for r in rows))

    def test_C5_completion_survives_authority_changes_and_replacement(self):
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

    def test_C1_invalid_state_schema_outcome_and_duplicate_keys(self):
        receipt = self.receipt()
        path = self.path(receipt)
        saved = path.read_bytes()
        for key, value in (("state", "bogus"), ("state", []), ("outcome", self.ok),
                ("schema_version", True), ("receipt_verifier", "x"), ("effect_id", "c" * 32)):
            row = json.loads(saved)
            row[key] = value
            path.write_bytes(E._encode(row))
            with self.subTest(field=key, value=value), self.reject():
                E.complete(receipt, self.ok)
        for raw in (b'{', b'[]', b'\xff', saved.replace(b'"state":', b'"state":"unknown","state":')):
            path.write_bytes(raw)
            with self.reject():
                E.complete(receipt, self.ok)
        path.write_bytes(saved)
        for bad in ({}, None, {"status": "unknown", "detail": "x"},
                    {"status": "succeeded", "detail": 1}, dict(self.ok, path="elsewhere")):
            with self.subTest(outcome=bad), self.reject():
                E.complete(receipt, bad)
        self.eq(path.read_bytes(), saved)

    def test_C1_unsafe_files_and_directory_rebinding(self):
        receipt = self.receipt()
        path = self.path(receipt)
        raw = path.read_bytes()
        outside = self.d / "outside"
        outside.write_bytes(raw)
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
            with self.subTest(variant=variant), self.reject():
                E.complete(receipt, self.ok)
        path.unlink()
        path.write_bytes(raw)
        self.eq(outside.read_bytes(), raw)
        directory = path.parent
        moved = self.d / "moved"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.reject():
            E.complete(receipt, self.ok)
        directory.unlink()
        directory.mkdir()
        path.write_bytes(raw)
        with self.reject():
            E.complete(receipt, self.ok)
        other = self.d / "different-record"
        other.mkdir()
        (other / E.DIRECTORY).mkdir()
        (other / E.DIRECTORY / path.name).write_bytes(raw)
        with self.reject():
            E.complete(replace(receipt, _directory=str(other)), self.ok)

    def test_E1_state_serializes_consumers_and_concurrent_terminal_results(self):
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

    def test_E2_exclusive_creation_durability_and_retry(self):
        intent = self.prepare()
        saved = self.path(intent).read_bytes()
        with mock.patch.object(E.secrets, "token_hex", return_value=intent.effect_id):
            with self.reject(E.DURABILITY):
                self.prepare()
        self.eq(self.path(intent).read_bytes(), saved)
        receipt = E._classify(intent, "submitted")
        path = self.path(receipt)
        prior = path.read_bytes()
        with mock.patch.object(os, "replace", side_effect=OSError("pre-publication")):
            with self.reject(E.DURABILITY):
                E.complete(receipt, self.ok)
        self.eq(path.read_bytes(), prior)
        real_sync = os.fsync
        def fail_directory(fd):
            if E._key(fd) == E._key(dirfd):
                raise OSError("post-publication")
            real_sync(fd)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            with mock.patch.object(os, "fsync", side_effect=fail_directory):
                with self.reject(E.DURABILITY):
                    E.complete(receipt, self.ok)
                self.eq(self.entry(receipt)["state"], "completed")
                with self.reject(E.DURABILITY):
                    E.complete(receipt, self.ok)
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
                        with self.reject(E.CONFLICT):
                            E._classify(intent, target)
        for invalid in (None, True, [], "completed", "attempting"):
            with self.reject():
                E._classify(self.prepare(), invalid)

    def test_E4_binding_schema_and_restrictive_umask(self):
        for field, value in (("record_id", "wrong"), ("kind", ""), ("custody", {}),
                             ("scope", "all"), ("correlation", {})):
            with self.subTest(field=field), self.reject():
                self.prepare(**{field: value})
        saved = os.umask(0o777)
        try:
            receipt = self.receipt()
            E.complete(receipt, self.ok)
        finally:
            os.umask(saved)
        self.eq(self.path(receipt).stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
