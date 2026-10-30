"""R5 through a locked consumer, real registry/store, and explicit fixture boundary."""
from contextlib import nullcontext
from contextlib import ExitStack
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_admission as admission
import d5_authority as authority
import d5_authority_store as store
import d5_control as control
import d5_effect_log as ledger
import d5_host as host
import d5_lease as lease
import d5_protocol_locks as protocol
import d5_registry as registry
from tests.fixtures.d5.ready import ready, ready_installation, missing_installation, classification_only
from tests.test_d5_locks import isolate_pool

MANDATE = b"fixture mandate\r\n\xff"
ANCHOR = host.ProcessIdentity(42, 9000001)


class TestAdmission(unittest.TestCase):
    def setUp(self):
        isolate_pool(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo, self.state = self.root / "repo", self.root / "state"
        self.git("init", str(self.repo))
        self.state.mkdir(mode=0o700)
        (self.repo / "docs").mkdir()
        self.mandate = self.repo / "docs/orchestrator-mandate.md"
        self.mandate.write_bytes(MANDATE)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(registry.CP, "_external_base",
                                                   return_value=self.root / "external"))
        self.anchor = self.stack.enter_context(mock.patch.object(host, "session_anchor",
                                             return_value=("native", ANCHOR)))
        self.clock = self.stack.enter_context(mock.patch.object(admission.time, "time", return_value=150))
        self.args = self.repo, "p", self.state
        self.entry = ready(self, *self.args)
        self.directory = Path(self.entry["record_dir"])
        self.scope = dict(plans=["p"], repositories=["r"], actions=["write"], trackers=["t"])
        lease.grant(*self.args, issued_at=100, confirmed_at=100, expires_at=200,
                    mandate_bytes=MANDATE, scope=self.scope,
                    owner_response="fixture", displayed_bounds="fixture")
        self.original = store.claim(self.directory, 101, MANDATE, "native", ANCHOR, lambda _: "alive")
        self.path = self.directory / store.RECORD_NAME
        self.request = dict(grant_id=self.original["grant_id"], session_id="native",
                            anchor=ANCHOR, generation=1, scope=json.dumps(self.scope).encode())
        self.boundaries = []

    def git(self, *args):
        return subprocess.run(["git", *args], capture_output=True, check=True, timeout=30)

    def validate(self, pinned, args=None):
        return admission._validate_locked(pinned, *(args or self.args), **self.request)

    def consume(self, before=None, args=None, directory=None):
        # Fixture submission critical section. R6 owns the production boundary
        # and ledger integration; this sentinel records arrival after validation.
        with protocol.pin(directory or self.directory) as pinned, pinned.drain(), pinned.start():
            if before is not None:
                before()
            result = self.validate(pinned, args)
            self.boundaries.append("started")
            return result

    def refuse(self, message, call=None):
        before = list(self.boundaries)
        with self.assertRaisesRegex(authority.AuthorityError, message):
            (call or self.consume)()
        self.assertEqual(self.boundaries, before)

    def write_record(self, record):
        self.path.write_bytes(authority.encode(record))

    def probe(self):
        fd = os.open(self.directory / "authority.start.lock", os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "blocked"
            return "available"
        finally:
            os.close(fd)

    def test_fresh_registration_and_registry_gates(self):
        # R5-CACHE-REGISTRATION: a prior successful call is not a registry cache.
        self.consume()
        (self.directory / ".git").mkdir()
        with mock.patch.object(store, "read", side_effect=AssertionError("authority read")):
            self.refuse("Git worktree")
        (self.directory / ".git").rmdir()
        self.state.rename(self.root / "old-state")
        self.state.mkdir()
        for artifact in (self.root / "old-state").iterdir():
            artifact.rename(self.state / artifact.name)
        self.refuse("state_key mismatch")

    def test_absent_registration(self):
        # R5-ABSENT-REGISTRATION: no authority read on absent registration.
        state = self.root / "other-state"
        state.mkdir()
        ready_installation(self.root, state_dir=state)
        with mock.patch.object(store, "read", side_effect=AssertionError("authority read")):
            self.refuse("^not registered$", lambda: self.consume(args=(self.repo, "other", state)))

    def test_identity_refresh_matches_pinned_directory_and_grant(self):
        # R5-SKIP-IDENTITY, both filesystem identity and current grant ID.
        self.consume()
        other = self.root / "other"
        other.mkdir(mode=0o700)
        self.refuse("^D5 authority identity mismatch$", lambda: self.consume(directory=other))
        replacement = copy.deepcopy(self.original)
        replacement["grant_id"] = "b" * 32
        with protocol.pin(self.directory) as pinned, pinned.drain(shared=False), pinned.start():
            store.replace(self.directory, replacement)
        self.refuse("^D5 authority identity mismatch$")
        self.request["grant_id"] = replacement["grant_id"]
        self.consume()

    def test_authority_refresh_and_invalidated_precedence(self):
        # R5-CACHE-AUTHORITY / R5-ACCEPT-INVALIDATED.
        self.consume()
        control.revoke(*self.args, 151, "fixture stop")
        self.refuse("^D5 authority is invalidated$")
        self.anchor.return_value = host.Unknown("unknown")
        self.request["generation"] = 2
        self.request["scope"] = b"{}"
        self.clock.return_value = 200
        self.mandate.write_bytes(b"changed")
        self.refuse("^D5 authority is invalidated$")

    def test_current_mandate_exact_bytes_and_terminal_persistence(self):
        # R5-CACHE-MANDATE: CRLF and arbitrary bytes are significant.
        self.consume()
        for changed in (MANDATE.replace(b"\r\n", b"\n"), MANDATE + b" ", b"\xff"):
            self.write_record(self.original)
            self.mandate.write_bytes(changed)
            self.refuse("^D5 mandate changed$")
            recorded = store.read(self.directory)["invalidation"]
            self.assertEqual(recorded, dict(at=150, kind="mandate_changed",
                                           reason="mandate byte digest changed"))
            self.mandate.write_bytes(MANDATE)
            self.refuse("^D5 authority is invalidated$")

    def test_registered_source_from_linked_caller(self):
        # R5-WRONG-SOURCE: caller checkout bytes cannot override registration.
        self.git("-C", str(self.repo), "-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture")
        linked = self.root / "linked"
        self.git("-C", str(self.repo), "worktree", "add", "-b", "linked", str(linked), "HEAD")
        (linked / "docs").mkdir()
        (linked / "docs/orchestrator-mandate.md").write_bytes(MANDATE)
        self.mandate.write_bytes(b"registered changed")
        self.refuse("^D5 mandate changed$", lambda: self.consume(args=(linked, "p", self.state)))

    def test_mandate_source_failure_before_store_entry(self):
        # R5-READ-BEFORE-MANDATE / R5-IGNORE-MANDATE-ERROR. Reader itself is R2.
        other = self.root / "saved.md"
        other.write_bytes(MANDATE)
        for kind in ("missing", "symlink", "directory", "fifo", "unreadable"):
            self.mandate.unlink()
            if kind == "symlink":
                self.mandate.symlink_to(other)
            elif kind == "directory":
                self.mandate.mkdir()
            elif kind == "fifo":
                os.mkfifo(self.mandate)
            elif kind == "unreadable":
                self.mandate.write_bytes(MANDATE)
            opening = os.open

            def opened(path, flags, *args, **kwargs):
                if Path(path) == self.mandate:
                    self.assertTrue(flags & os.O_NONBLOCK)
                    if kind == "unreadable":
                        raise PermissionError("fixture denied")
                return opening(path, flags, *args, **kwargs)

            try:
                with self.subTest(kind=kind), mock.patch.object(os, "open", side_effect=opened), \
                        mock.patch.object(store, "read", side_effect=AssertionError("store read")), \
                        mock.patch.object(store, "check", side_effect=AssertionError("store check")):
                    with self.assertRaises((OSError, authority.AuthorityError)):
                        self.consume()
                self.assertEqual(self.boundaries, [])
            finally:
                if kind == "directory":
                    self.mandate.rmdir()
                elif kind != "missing":
                    self.mandate.unlink()
                self.mandate.write_bytes(MANDATE)

    def test_renamed_checkout_retains_registered_mandate_locator(self):
        # Round 1 counterexample: OD18 requires the now-missing registered
        # source to refuse. Rebinding to the caller would violate that rule.
        before = self.path.read_bytes()
        moved = self.root / "repo-moved"
        self.repo.rename(moved)
        self.assertEqual(registry.lookup(moved, "p", self.state), self.entry)
        self.assertEqual((moved / "docs/orchestrator-mandate.md").read_bytes(), MANDATE)
        with mock.patch.object(store, "read", side_effect=AssertionError("store read")), \
                mock.patch.object(store, "check", side_effect=AssertionError("store check")):
            with self.assertRaises(FileNotFoundError) as caught:
                self.consume(args=(moved, "p", self.state))
        self.assertEqual(caught.exception.filename, str(self.mandate))
        self.assertEqual(self.boundaries, [])
        self.assertEqual(self.path.read_bytes(), before)
        # Measured operator recovery: restore the registered locator intact.
        moved.rename(self.repo)
        self.consume()
        self.assertEqual(self.boundaries, ["started"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_identity_verification_is_fresh_and_unknown_refused(self):
        # R5-SKIP-HOST / R5-UNKNOWN-ANCHOR-ACCEPTED. Real verify_anchor consumes
        # changing native observations, not a supplied Boolean authorization.
        self.consume()
        for observed in (host.Unknown("unmeasured"), host.Unknown("unreadable"),
                         ("different", ANCHOR), ("native", host.ProcessIdentity(42, 9000002))):
            self.anchor.return_value = observed
            self.refuse("^D5 anchor is not verifiable$")

    def test_custody_refresh(self):
        # R5-SKIP-CUSTODY, including generation, missing and relinquished state.
        self.consume()
        variants = [None]
        for field, value in (("generation", 2), ("session_id", "other"), ("anchor_pid", 43),
                             ("anchor_start_us", 9000002), ("relinquished_at", 149)):
            variants.append(dict(self.original["custody"], **{field: value}))
        for custody in variants:
            self.write_record(dict(self.original, custody=custody))
            self.refuse("^D5 custody is not current$")
        self.write_record(self.original)
        # R5-BOOL-GENERATION: Python equality must not turn True into generation 1.
        self.request["generation"] = True
        self.refuse("^D5 custody is not current$")

    def test_scope_refresh_each_dimension_and_exact_strings(self):
        # R5-SKIP-SCOPE: test changed requested scope AND changed current grant.
        self.consume()
        raw = self.request["scope"]
        for key, items in self.scope.items():
            for denied in (items[0].upper(), items[0] + " ", "*", "other"):
                candidate = dict(self.scope, **{key: [denied]})
                self.request["scope"] = json.dumps(candidate).encode()
                self.refuse("^D5 authority scope denied$")
            self.request["scope"] = raw
            self.write_record(dict(self.original, scope=dict(self.scope, **{key: []})))
            self.refuse("^D5 authority scope denied$")
            self.write_record(self.original)
        self.consume()

    def test_scope_requires_immutable_complete_schema(self):
        # R5-MUTABLE-SCOPE / R5-SCOPE-SCHEMA / R5-SCOPE-DUPLICATES.
        raw = self.request["scope"]
        invalid = [bytearray(raw), json.loads(raw), b"{}", b"[]", b"\xff", b"{",
                   raw[:-1] + b', "plans": ["p"]}']
        for field in self.scope:
            for value in ("p", None, [1], ["\ud800"], self.scope[field] * 2):
                invalid.append(json.dumps(dict(self.scope, **{field: value})).encode())
        for scope in invalid:
            self.request["scope"] = scope
            self.refuse("^D5 authority scope denied$")

    def test_expiry_equality_refused(self):
        # U3-M8 changes the reused evaluator's >= to >; equality must not start.
        self.clock.return_value = 199
        self.consume()
        for now in (200, 201):
            self.clock.return_value = now
            self.refuse("^D5 authority expired$")

    def test_fresh_time_after_preparation_and_local_checks(self):
        # R5-CACHE-TIME / R5-EARLY-TIME: preparation and local verification can
        # consume the remaining lease. The clock is sampled after both.
        self.consume()
        self.refuse("^D5 authority expired$", lambda: self.consume(
            before=lambda: setattr(self.clock, "return_value", 200)))
        self.clock.return_value = 150
        verify = host.verify_anchor

        def delayed(*args):
            self.clock.return_value = 200
            return verify(*args)

        with mock.patch.object(host, "verify_anchor", side_effect=delayed):
            self.refuse("^D5 authority expired$")

    def test_admission_invalidation_is_serialized(self):
        # R5-SKIP-PERSISTENCE: independent flock at store entry and write proves
        # the caller still owns START. R5 must neither release nor reacquire it.
        self.mandate.write_bytes(b"changed")
        write, check = store._write, store.check
        trace = []

        def checked(*args):
            trace.append(("check", self.probe()))
            return check(*args)

        def writing(*args):
            trace.append(("write", self.probe()))
            return write(*args)

        with protocol.pin(self.directory) as pinned, pinned.drain(), pinned.start():
            with mock.patch.object(store, "check", side_effect=checked), \
                    mock.patch.object(store, "_write", side_effect=writing), \
                    mock.patch.object(protocol._Protocol, "start", side_effect=AssertionError("START reacquired")), \
                    mock.patch.object(protocol._Protocol, "drain", side_effect=AssertionError("DRAIN acquired")):
                self.refuse("^D5 mandate changed$", lambda: self.validate(pinned))
            self.assertEqual(self.probe(), "blocked")
        self.assertEqual(trace, [("check", "blocked"), ("write", "blocked")])
        self.assertEqual(self.probe(), "available")
        self.assertEqual(store.read(self.directory)["invalidation"]["kind"], "mandate_changed")

    def test_recorded_control_precedes_final_admission(self):
        # U3-M2 component and M1: request pauses OUTSIDE final START admission.
        for changed in (True, False):
            self.write_record(self.original)
            self.mandate.write_bytes(MANDATE)
            ready, resume = threading.Event(), threading.Event()
            results = []

            def request():
                ready.set()
                if not resume.wait(10):
                    results.append(AssertionError("request watchdog"))
                    return
                try:
                    results.append(self.consume())
                except BaseException as exc:
                    results.append(exc)

            thread = threading.Thread(target=request, daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(10), "request watchdog")
                if changed:
                    self.mandate.write_bytes(b"changed")
                    self.assertEqual(control.record_mandate_invalidation(*self.args, now=150), "mandate_changed")
                    self.mandate.write_bytes(MANDATE)  # persisted invalidation is terminal
                else:
                    control.revoke(*self.args, 150, "stop")
            finally:
                resume.set()
                thread.join(15)
            self.assertFalse(thread.is_alive(), "thread watchdog")
            self.assertEqual(len(results), 1)
            self.assertIsInstance(results[0], authority.AuthorityError)
            self.assertEqual(str(results[0]), "D5 authority is invalidated")
            self.assertEqual(self.boundaries, [])

    def test_inactive_and_unlocked_consumers_refuse_before_io(self):
        # R5-BYPASS-INACTIVE / R5-NO-START / R5-CLOSED-PIN.
        with protocol.pin(self.directory) as pinned:
            with mock.patch.object(registry, "lookup", side_effect=AssertionError("lookup")):
                self.refuse("^D5 admission requires START$", lambda: self.validate(pinned))
                with pinned.start():
                    empty = self.root / "empty"
                    empty.mkdir()
                    with classification_only(empty):
                        self.refuse("^D5 is inactive$", lambda: self.validate(pinned, (self.repo, "p", empty)))
        self.refuse("^D5 admission requires START$", lambda: self.validate(pinned))
        with protocol.pin(self.directory) as live, live.start():
            self.refuse("^D5 admission requires START$", lambda: self.validate(pinned))

    def test_invalidation_persistence_failure_propagates(self):
        # R5-HIDE-PERSISTENCE-FAILURE: no successful decision or generic mandate
        # refusal can conceal an unconfirmed invalidation write.
        self.mandate.write_bytes(b"changed")
        with mock.patch.object(store, "_write", side_effect=OSError("fixture durability")):
            with self.assertRaisesRegex(authority.AuthorityError, "durability is unconfirmed"):
                self.consume()
        self.assertEqual(self.boundaries, [])
        self.assertIsNone(store.read(self.directory)["invalidation"])
        self.refuse("^D5 mandate changed$")

    def test_validation_result_cannot_submit(self):
        # R5-PERMISSION-TICKET: no returned capability or effect from checking.
        with protocol.pin(self.directory) as pinned, pinned.drain(), pinned.start():
            result = self.validate(pinned)
        self.assertIsNone(result)
        self.assertEqual(self.boundaries, [])
        with self.assertRaises(ledger.EffectError):
            ledger._classify(result, "submitted")
        control.revoke(*self.args, 151, "after check")
        self.refuse("^D5 authority is invalidated$")


if __name__ == "__main__":
    unittest.main()
