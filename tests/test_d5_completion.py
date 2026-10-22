"""R4 consumer tests: real ledger transitions, deterministic DRAIN exclusion."""
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
import d5_completion as C
import d5_effect_log as ledger
import d5_protocol_locks as protocol
import d5_locks as locks
import d5_authority as authority
import d5_authority_store as store
import d5_host as host
import d5_custody as custody
import d5_lease as lease
from tests.test_d5_authority import grant, MANDATE
from tests.test_d5_locks import isolate_pool


class TestCompletion(unittest.TestCase):
    def setUp(self):
        isolate_pool(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.active = self.stack.enter_context(mock.patch.object(
            ledger.registry, "d5_active", return_value=True))
        self.registration = {"registration_id": "a" * 32,
                             "record_dir": str(self.directory)}
        self.stack.enter_context(mock.patch.object(
            ledger.registry, "lookup", return_value=self.registration))
        self.request = dict(record_id="b" * 32,
            custody=dict(generation=1, session_id="fixture", anchor_pid=123, anchor_start_us=101),
            kind="fixture", scope=dict(plans=["p"], repositories=["r"], actions=["a"], trackers=[]),
            correlation={"request_id": "existing-operation"})
        self.outcome = {"status": "succeeded", "detail": "synthetic response"}

    def prepare(self):
        return ledger.prepare("fixture", "p", "fixture", **self.request)

    def receipt(self):
        return ledger._classify(self.prepare(), "submitted")

    def path(self, receipt):
        return Path(receipt._directory) / ledger.DIRECTORY / (receipt.effect_id + ".json")

    def snapshot(self):
        return {str(p.relative_to(self.directory)): p.read_bytes()
                for p in self.directory.rglob("*.json")}

    def refuse(self, message):
        return self.assertRaisesRegex(ledger.EffectError, "^" + message + "$")

    def probe(self, name="authority.drain.lock"):
        fd = os.open(self.directory / name, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "blocked"
            return "available"
        finally:
            os.close(fd)

    def test_receipt_binding_enforced(self):
        # R4-WRONG-RECEIPT: retained ledger authentication is part of this seam.
        receipt, other = self.receipt(), self.receipt()
        capability = C.bind(receipt)
        binding = json.loads(receipt._binding)
        variants = [replace(receipt, effect_id=other.effect_id),
                    replace(receipt, _secret=b"x" * 32),
                    replace(receipt, _directory=None), object()]
        changes = dict(record_id="c" * 32, registration_id="c" * 32,
            record_key=[0, 0], effects_key=[0, 0], kind="changed",
            custody=dict(binding["custody"], generation=2),
            scope=dict(binding["scope"], actions=["changed"]),
            correlation={"request_id": "changed"})
        for key, value in changes.items():
            changed = copy.deepcopy(binding)
            changed[key] = value
            variants.append(replace(receipt, _binding=ledger._encode(changed)))
        alternate = self.directory / "alternate"
        alternate.mkdir(mode=0o700)
        self.registration["record_dir"] = str(alternate)
        elsewhere = self.receipt()
        self.registration["record_dir"] = str(self.directory)
        variants.append(replace(receipt, _directory=str(alternate), effect_id=elsewhere.effect_id))
        saved = self.snapshot()
        for candidate in variants:
            with self.subTest(candidate=type(candidate).__name__), self.refuse(C.MISMATCH):
                C.bind(candidate).complete(self.outcome)
            self.assertEqual(self.snapshot(), saved)
        # Recheck immutable data at accounting, not merely when binding.
        path = self.path(receipt)
        raw = path.read_bytes()
        for key, value in changes.items():
            row = json.loads(raw)
            row["binding"][key] = value
            path.write_bytes(ledger._encode(row))
            before = self.snapshot()
            for call in (lambda: capability.complete(self.outcome), capability.uncertain):
                with self.subTest(field=key), self.refuse(C.MISMATCH):
                    call()
                self.assertEqual(self.snapshot(), before)
            path.write_bytes(raw)
        self.assertEqual(capability.complete(self.outcome), self.outcome)

    def test_completion_cannot_create_operation(self):
        # R4-NEW-OPERATION: attempts cannot be promoted by this wrapper.
        for state in ("attempting", "unknown", "not_submitted", "submitted"):
            attempt = self.prepare()
            if state != "attempting":
                ledger._classify(attempt, state)
            capability = C.bind(attempt)
            before = self.snapshot()
            with self.subTest(state=state), self.refuse(C.TRANSITION):
                capability.complete(self.outcome)
            self.assertEqual(self.snapshot(), before)
            if state != "submitted":
                # Recasting an intent as a Receipt is no submission evidence.
                forged = ledger.Receipt(attempt.effect_id, attempt._directory,
                                        attempt._binding, attempt._secret)
                with self.refuse(C.TRANSITION):
                    C.bind(forged).complete(self.outcome)
                self.assertEqual(self.snapshot(), before)
        receipt = self.receipt()
        capability = C.bind(receipt)
        self.path(receipt).unlink()
        before = self.snapshot()
        with self.refuse(C.MISMATCH):
            capability.complete(self.outcome)
        self.assertEqual(self.snapshot(), before)

    def test_completion_does_not_reauthorize(self):
        # U3-M3 component / R4-REAUTHORIZE. R4 makes no admission-B claim.
        record = grant()
        store.create(self.directory, record)
        receipt, attempt = self.receipt(), self.prepare()
        capability, uncertainty = C.bind(receipt), C.bind(attempt)
        store.revoke(self.directory, record["expires_at"] + 1, "revoked after submission")
        self.assertEqual(capability.complete(self.outcome), self.outcome)
        receipt = self.receipt()
        capability = C.bind(receipt)
        successor = copy.deepcopy(record)
        successor["grant_id"] = "e" * 32
        successor["confirmation"]["owner_response"] = "fresh consent"
        store.replace(self.directory, successor)
        successor["custody"] = dict(self.request["custody"], generation=9,
                                    session_id="successor", claimed_at=101, relinquished_at=102)
        successor["expires_at"] = 150
        successor["mandate_sha256"] = authority.mandate_digest(b"changed mandate")
        (self.directory / store.RECORD_NAME).write_bytes(authority.encode(successor))
        self.assertEqual(authority.evaluate(successor, 151, b"changed mandate")[0], "expired")
        self.assertEqual(authority.evaluate(successor, 151, MANDATE)[0], "mandate_changed")
        with ExitStack() as stack:
            for target, names in (
                (ledger.registry, ("lookup",)),
                (store, ("read", "check", "claim", "revoke", "replace", "relinquish")),
                (authority, ("evaluate", "validate", "mandate_digest")),
                (host, ("session_anchor", "verify_anchor", "liveness")),
                (custody, ("claim", "status", "relinquish")),
                (lease, ("grant", "revoke", "status")),
                (protocol._Protocol, ("start",))):
                for name in names:
                    stack.enter_context(mock.patch.object(target, name,
                        side_effect=AssertionError("current authority/START consulted")))
            self.assertEqual(capability.complete(self.outcome), self.outcome)
            self.assertIsNone(uncertainty.uncertain())
            # Late binding also uses the historical ledger, not live authority.
            self.assertEqual(C.bind(receipt).complete(self.outcome), self.outcome)
        self.assertEqual(json.loads(self.path(receipt).read_bytes())["outcome"], self.outcome)

    def test_inactive_completion_is_deferred(self):
        # R4-DROP-ON-INACTIVE, including duplicate and uncertainty siblings.
        receipt, attempt = self.receipt(), self.prepare()
        capability, uncertainty = C.bind(receipt), C.bind(attempt)
        for state in ("submitted", "completed"):
            self.assertEqual(json.loads(self.path(receipt).read_bytes())["state"], state)
            before = self.snapshot()
            self.active.return_value = False
            with self.refuse("D5 inactive"):
                capability.complete(self.outcome)
            with self.refuse("D5 inactive"):
                uncertainty.uncertain()
            self.assertEqual(self.snapshot(), before)
            self.active.return_value = True
            self.assertEqual(capability.complete(self.outcome), self.outcome)
        self.assertIsNone(uncertainty.uncertain())
        self.assertEqual(json.loads(self.path(attempt).read_bytes())["state"], "unknown")

    def test_inactive_binding_refused(self):
        # R4-BYPASS-INACTIVE: no capability is issued through inactive binding.
        receipt = self.receipt()
        self.active.return_value = False
        with mock.patch.object(protocol, "pin", side_effect=AssertionError("inactive pin")):
            with self.refuse("D5 inactive"):
                C.bind(receipt)

    def test_completion_is_not_admission(self):
        receipt, other = self.receipt(), self.receipt()
        capability = C.bind(receipt)
        before = self.snapshot()
        with self.refuse(ledger.INVALID):
            ledger._classify(capability, "submitted")
        with self.refuse(ledger.INVALID):
            ledger.complete(capability, self.outcome)
        with self.refuse(C.MISMATCH):
            C.bind(capability)
        # R4-REBIND: the public capability cannot be redirected after issue.
        with self.assertRaises(FrozenInstanceError):
            capability._handle = other
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn(receipt._secret.hex(), repr(capability))
        self.assertNotIn(repr(receipt._secret), repr(capability))

    def test_uncertainty_has_no_submission_transition(self):
        # R4-ASSERT-SUBMITTED: unknown is the fixed classification, not a parameter.
        for submitted in (False, True):
            attempt = self.prepare()
            handle = ledger._classify(attempt, "submitted") if submitted else attempt
            capability = C.bind(handle)
            self.assertIsNone(capability.uncertain())
            self.assertEqual(json.loads(self.path(handle).read_bytes())["state"], "unknown")
            before = self.snapshot()
            self.assertIsNone(capability.uncertain())
            with self.refuse(C.TRANSITION):
                capability.complete(self.outcome)
            self.assertEqual(self.snapshot(), before)
        receipt = self.receipt()
        capability = C.bind(receipt)
        capability.complete(self.outcome)
        before = self.snapshot()
        with self.refuse(ledger.CONFLICT):
            capability.uncertain()
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_and_conflicting_completion(self):
        capability = C.bind(self.receipt())
        self.assertEqual(capability.complete(self.outcome), self.outcome)
        before = self.snapshot()
        self.assertEqual(capability.complete(dict(self.outcome)), self.outcome)
        with self.refuse(ledger.CONFLICT):
            capability.complete(dict(self.outcome, detail="different response"))
        self.assertEqual(self.snapshot(), before)

    def test_accounting_blocks_installation(self):
        # R4-EARLY-DRAIN-CLOSE: real nonblocking flock proves exclusion.
        # Hooks/events order an installer through DRAIN->START->store.replace.
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                if not (self.directory / store.RECORD_NAME).exists():
                    store.create(self.directory, grant())
                capability = C.bind(self.prepare() if uncertain else self.receipt())
                original = store.read(self.directory)
                successor = copy.deepcopy(original)
                successor["grant_id"] = "c" * 32 if not uncertain else "d" * 32
                successor["confirmation"]["owner_response"] = "installation fixture"
                entered, release, probed = threading.Event(), threading.Event(), threading.Event()
                observations, errors = [], []
                real_write, real_flock = ledger._write, fcntl.flock

                def writing(*args, **kwargs):
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("accounting watchdog")
                    return real_write(*args, **kwargs)

                def flock(fd, operation):
                    if threading.current_thread().name == "r4-installer" and operation == fcntl.LOCK_EX:
                        info = os.fstat(fd)
                        drain = (self.directory / "authority.drain.lock").stat()
                        if (info.st_dev, info.st_ino) == (drain.st_dev, drain.st_ino):
                            try:
                                real_flock(fd, operation | fcntl.LOCK_NB)
                            except BlockingIOError:
                                observations.append("blocked")
                            else:
                                observations.append("available")
                            probed.set()
                    return real_flock(fd, operation)

                def account():
                    try:
                        if uncertain:
                            capability.uncertain()
                        else:
                            capability.complete(self.outcome)
                    except BaseException as exc:
                        errors.append(exc)

                def install():
                    try:
                        with protocol.pin(self.directory) as record:
                            with record.drain(shared=False), record.start():
                                observations.append("install")
                                store.replace(self.directory, successor)
                    except BaseException as exc:
                        errors.append(exc)

                with mock.patch.object(ledger, "_write", side_effect=writing), \
                        mock.patch.object(fcntl, "flock", side_effect=flock):
                    worker = threading.Thread(target=account, daemon=True)
                    installer = threading.Thread(target=install, name="r4-installer", daemon=True)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(10), "accounting watchdog")
                        installer.start()
                        self.assertTrue(probed.wait(10), "installer watchdog")
                        self.assertEqual(observations, ["blocked"])
                        self.assertEqual(self.probe("authority.start.lock"), "available")
                    finally:
                        release.set()
                        worker.join(10)
                        if installer.ident is not None:
                            installer.join(10)
                    self.assertFalse(worker.is_alive() or installer.is_alive(), "thread watchdog")
                self.assertEqual(errors, [])
                self.assertEqual(observations, ["blocked", "install"])
                self.assertEqual(store.read(self.directory), successor)
                self.assertEqual(self.probe(), "available")

    def test_completion_write_failure_is_visible(self):
        # R4-SWALLOW-ACCOUNTING-ERROR: before and after publication, both paths.
        for uncertain in (False, True):
            for published in (False, True):
                with self.subTest(uncertain=uncertain, published=published):
                    handle = self.prepare() if uncertain else self.receipt()
                    capability = C.bind(handle)
                    call = capability.uncertain if uncertain else lambda: capability.complete(self.outcome)
                    before = self.snapshot()
                    real_sync = os.fsync
                    effects = self.path(handle).parent.stat()

                    def sync(fd):
                        info = os.fstat(fd)
                        if (info.st_dev, info.st_ino) == (effects.st_dev, effects.st_ino):
                            self.assertEqual(self.probe(), "blocked")
                            raise OSError("post-publication fixture")
                        return real_sync(fd)

                    fault = (mock.patch.object(os, "fsync", side_effect=sync) if published else
                             mock.patch.object(os, "replace", side_effect=OSError("before publication")))
                    with fault:
                        with self.refuse(ledger.DURABILITY):
                            call()
                        if published:
                            # Duplicate confirmation must surface the same fsync failure.
                            with self.refuse(ledger.DURABILITY):
                                call()
                    if not published:
                        self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.probe(), "available")
                    self.assertEqual(call(), None if uncertain else self.outcome)

    def test_nested_completion_retains_callers_drain(self):
        capability = C.bind(self.receipt())
        with protocol.pin(self.directory) as record, record.drain():
            capability.complete(self.outcome)
            self.assertEqual(self.probe(), "blocked")
            with self.refuse(ledger.CONFLICT):
                capability.complete(dict(self.outcome, detail="different"))
            self.assertEqual(self.probe(), "blocked")
        self.assertEqual(self.probe(), "available")


if __name__ == "__main__":
    unittest.main()
