"""R6 consumer tests: real registration/admission/ledger, ordered lock probes."""
from contextlib import contextmanager, ExitStack
from dataclasses import FrozenInstanceError
import fcntl
import json
import inspect
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_submission as submission
import d5_admission as admission
import d5_authority as authority
import d5_authority_store as store
import d5_completion as completion
import d5_control as control
import d5_effect_log as ledger
import d5_host as host
import d5_lease as lease
import d5_protocol_locks as protocol
import d5_registry as registry
from tests.test_d5_locks import isolate_pool

MANDATE = b"fixture mandate\r\n\xff"
ANCHOR = host.ProcessIdentity(42, 9000001)
OK = {"status": "succeeded", "detail": "fixture response"}


class DeterministicBoundary(submission.Boundary):
    """Explicit in-process initiation; Finish cannot initiate another effect."""
    def __init__(self):
        self.calls = []
        self.state = submission.Initiation.SUBMITTED
        self.on_start = lambda: None
        self.on_finish = lambda: dict(OK)

    def start(self, operation):
        self.calls.append(("start", operation))
        self.on_start()
        return self.state

    def finish(self):
        self.calls.append(("finish",))
        return self.on_finish()


class TestSubmission(unittest.TestCase):
    def setUp(self):
        isolate_pool(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo, self.state_dir = self.root / "repo", self.root / "state"
        subprocess.run(["git", "init", str(self.repo)], capture_output=True,
                       check=True, timeout=30)
        self.state_dir.mkdir(mode=0o700)
        (self.repo / "docs").mkdir()
        self.mandate = self.repo / "docs/orchestrator-mandate.md"
        self.mandate.write_bytes(MANDATE)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(registry.CP, "_external_base",
                                                   return_value=self.root / "external"))
        self.active = self.stack.enter_context(mock.patch.object(registry, "d5_active", return_value=True))
        self.stack.enter_context(mock.patch.object(host, "session_anchor", return_value=("native", ANCHOR)))
        self.clock = self.stack.enter_context(mock.patch.object(admission.time, "time", return_value=150))
        self.args = self.repo, "p", self.state_dir
        self.entry = registry.register(*self.args)
        self.directory = Path(self.entry["record_dir"])
        self.scope = dict(plans=["p"], repositories=["r"], actions=["write"], trackers=["t"])
        lease.grant(*self.args, issued_at=100, confirmed_at=100, expires_at=200,
                    mandate_bytes=MANDATE, scope=self.scope,
                    owner_response="fixture", displayed_bounds="fixture")
        self.original = store.claim(self.directory, 101, MANDATE, "native", ANCHOR, lambda _: "alive")
        self.request = dict(grant_id=self.original["grant_id"], session_id="native",
                            anchor=ANCHOR, generation=1)
        self.operation = submission.Operation("A", "fixture", json.dumps(self.scope).encode(), b"exact\x00\xff")
        self.boundary = DeterministicBoundary()

    def submit(self, **kwargs):
        return submission.submit(*self.args, **dict(self.request,
            operation=self.operation, boundary=self.boundary, **kwargs))

    def rows(self):
        return [json.loads(p.read_bytes()) for p in sorted((self.directory / ledger.DIRECTORY).glob("*.json"))]

    def probe(self, name):
        fd = os.open(self.directory / ("authority." + name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "blocked"
            return "available"
        finally:
            os.close(fd)

    def launch(self, call):
        results = []

        def worker():
            try:
                results.append(call())
            except BaseException as exc:
                results.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread, results

    def join(self, thread, results):
        thread.join(15)
        self.assertFalse(thread.is_alive(), "thread watchdog")
        self.assertEqual(len(results), 1)
        return results[0]

    def test_boundary_after_fresh_validation_under_start(self):
        events = []
        validate, prepare = admission._validate_locked, ledger.prepare

        def validated(*args, **kwargs):
            result = validate(*args, **kwargs)
            events.append("validate")
            return result

        def prepared(*args, **kwargs):
            result = prepare(*args, **kwargs)
            events.append("prepare")
            return result

        def boundary():
            self.assertEqual(events, ["validate", "prepare", "validate"])
            self.assertEqual(self.probe("start"), "blocked")
            self.assertEqual(self.rows()[0]["state"], "attempting")

        self.boundary.on_start = boundary
        with mock.patch.object(admission, "_validate_locked", side_effect=validated), \
                mock.patch.object(ledger, "prepare", side_effect=prepared):
            result = self.submit()
        self.assertEqual(result.initiation, submission.Initiation.SUBMITTED)
        self.assertEqual(result.outcome, OK)
        row, = self.rows()
        self.assertEqual(row["effect_id"], result.effect_id)
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["binding"]["correlation"], {"request_id": "A", "intent": self.operation.intent.hex()})
        self.assertEqual(row["binding"]["scope"], self.scope)
        self.assertEqual(row["binding"]["record_id"], self.request["grant_id"])
        self.assertEqual(row["binding"]["custody"]["generation"], 1)

    def test_revocation_wins_before_boundary(self):
        for invalidation in (False, True):
            (self.directory / store.RECORD_NAME).write_bytes(authority.encode(self.original))
            ready, resume = threading.Event(), threading.Event()
            start = protocol._Protocol.start

            @contextmanager
            def paused(pinned):
                if threading.current_thread() is not threading.main_thread():
                    ready.set()
                    if not resume.wait(10):
                        raise AssertionError("admission watchdog")
                with start(pinned):
                    yield

            with mock.patch.object(protocol._Protocol, "start", paused):
                thread, results = self.launch(self.submit)
                try:
                    self.assertTrue(ready.wait(10), "request watchdog")
                    if invalidation:
                        self.mandate.write_bytes(b"changed")
                        self.assertEqual(control.record_mandate_invalidation(*self.args, now=150), "mandate_changed")
                        self.mandate.write_bytes(MANDATE)
                    else:
                        control.revoke(*self.args, 150, "stop")
                finally:
                    resume.set()
                    result = self.join(thread, results)
            self.assertIsInstance(result, authority.AuthorityError)
            self.assertEqual(str(result), "D5 authority is invalidated")
            self.assertEqual(self.boundary.calls, [])

    def test_prepare_failure_has_no_effect(self):
        for published in (False, True):
            write = ledger._write

            def failing(*args, **kwargs):
                if published:
                    write(*args, **kwargs)
                raise ledger.EffectError(ledger.DURABILITY)

            with self.subTest(published=published), mock.patch.object(ledger, "_write", side_effect=failing):
                with self.assertRaisesRegex(ledger.EffectError, ledger.DURABILITY):
                    self.submit()
            self.assertEqual(self.boundary.calls, [])
        self.assertEqual([r["state"] for r in self.rows()], ["attempting"])

    def test_final_time_after_prepare_including_equality(self):
        prepare = ledger.prepare

        def prepared(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.clock.return_value = 200
            return result

        with mock.patch.object(ledger, "prepare", side_effect=prepared):
            with self.assertRaisesRegex(authority.AuthorityError, "^D5 authority expired$"):
                self.submit()
        self.assertEqual(self.boundary.calls, [])
        self.assertEqual([r["state"] for r in self.rows()], ["not_submitted"])

    def test_response_wait_does_not_hold_start(self):
        waiting, release, persisted = threading.Event(), threading.Event(), threading.Event()

        def finish():
            waiting.set()
            if not release.wait(10):
                raise AssertionError("response watchdog")
            return dict(OK)

        self.boundary.on_finish = finish
        thread, results = self.launch(self.submit)
        try:
            self.assertTrue(waiting.wait(10), "response watchdog")
            self.assertEqual(self.probe("start"), "available")
            control.revoke(*self.args, 151, "stop")
            self.assertEqual(store.read(self.directory)["invalidation"]["kind"], "revoked")
            persisted.set()
            self.assertFalse(release.is_set())
            self.assertEqual(self.rows()[0]["state"], "submitted")
        finally:
            release.set()
            result = self.join(thread, results)
        self.assertTrue(persisted.is_set())
        self.assertIsInstance(result, submission.Result)
        self.assertEqual(result.outcome, OK)
        self.assertEqual(self.rows()[0]["state"], "completed")
        self.boundary = DeterministicBoundary()
        self.operation = submission.Operation("B", "fixture", self.operation.scope, b"next")
        with self.assertRaisesRegex(authority.AuthorityError, "^D5 authority is invalidated$"):
            self.submit()
        self.assertEqual(self.boundary.calls, [])

    def test_submission_lifetime_retains_drain(self):
        seen = []
        write = ledger._write

        def observe(phase):
            seen.append(phase)
            self.assertEqual(self.probe("drain"), "blocked")

        def writing(*args, **kwargs):
            observe(args[2]["state"])
            return write(*args, **kwargs)

        self.boundary.on_start = lambda: observe("boundary")
        self.boundary.on_finish = lambda: (observe("response"), dict(OK))[1]
        with mock.patch.object(ledger, "_write", side_effect=writing):
            self.submit()
        self.assertEqual(seen, ["attempting", "boundary", "submitted", "response", "completed"])
        self.assertEqual(self.probe("drain"), "available")

    def test_ambiguous_start_is_not_not_submitted(self):
        for exception in (RuntimeError("ambiguous"), KeyboardInterrupt("interrupted")):
            self.boundary = DeterministicBoundary()

            def fail():
                raise exception

            self.boundary.on_start = fail
            with self.assertRaises(BaseException) as caught:
                self.submit()
            self.assertIs(caught.exception, exception)
            self.assertEqual([c[0] for c in self.boundary.calls], ["start", "finish"])
        self.assertEqual([r["state"] for r in self.rows()], ["unknown", "unknown"])

    def test_explicit_initiation_states(self):
        for state in submission.Initiation:
            self.boundary = DeterministicBoundary()
            self.boundary.state = state
            result = self.submit()
            row = next(r for r in self.rows() if r["effect_id"] == result.effect_id)
            self.assertEqual(result.initiation, state)
            submitted = state is submission.Initiation.SUBMITTED
            self.assertEqual(result.outcome, OK if submitted else None)
            self.assertEqual(row["state"], "completed" if submitted else state.value)
            self.assertEqual([c[0] for c in self.boundary.calls], ["start"] if
                             state is submission.Initiation.NOT_SUBMITTED else ["start", "finish"])

    def test_opaque_helper_refused(self):
        class Opaque:
            start = mock.Mock(return_value=submission.Initiation.SUBMITTED)
            finish = mock.Mock(return_value=dict(OK))

        for boundary in (Opaque(), "fixture command", ["fixture"], None, lambda: None):
            self.boundary = boundary
            with self.assertRaisesRegex(authority.AuthorityError,
                    "^D5 submission requires an explicit effect boundary$"):
                self.submit()
        Opaque.start.assert_not_called()
        Opaque.finish.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_next_effect_requires_fresh_admission(self):
        self.boundary.on_finish = lambda: dict(OK, detail="next effect B requested")
        self.submit()
        self.assertEqual([c[0] for c in self.boundary.calls], ["start", "finish"])
        self.assertEqual(len(self.rows()), 1)
        control.revoke(*self.args, 151, "stop")
        self.boundary = DeterministicBoundary()
        with self.assertRaisesRegex(authority.AuthorityError, "invalidated"):
            self.submit()
        self.assertEqual(self.boundary.calls, [])

    def test_finish_failure_accounts_uncertainty(self):
        error = RuntimeError("response lost")

        def finish():
            self.assertEqual(self.probe("start"), "available")
            self.assertEqual(self.probe("drain"), "blocked")
            raise error

        self.boundary.on_finish = finish
        with self.assertRaises(RuntimeError) as caught:
            self.submit()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.rows()[0]["state"], "unknown")
        self.assertEqual(self.probe("drain"), "available")

    def test_accounting_failure_still_finishes_and_never_reports_success(self):
        for phase in ("submitted", "completed", "bind"):
            for published in (False, True):
                self.boundary = DeterministicBoundary()
                error = ledger.EffectError(ledger.DURABILITY)
                write = ledger._write

                def writing(*args, **kwargs):
                    if args[2]["state"] == phase:
                        self.assertEqual(self.probe("drain"), "blocked")
                        if published:
                            write(*args, **kwargs)
                        raise error
                    return write(*args, **kwargs)

                fault = (mock.patch.object(completion, "bind", side_effect=error) if phase == "bind" else
                         mock.patch.object(ledger, "_write", side_effect=writing))
                with self.subTest(phase=phase, published=published), fault:
                    with self.assertRaises(ledger.EffectError) as caught:
                        self.submit()
                self.assertIs(caught.exception, error)
                self.assertEqual([c[0] for c in self.boundary.calls], ["start", "finish"])
                self.assertEqual(self.probe("drain"), "available")
        self.assertEqual(len(self.rows()), 6)

    def test_inactive_completion_refusal_is_unchanged(self):
        for uncertain in (False, True):
            self.active.return_value = True
            self.boundary = DeterministicBoundary()

            def finish():
                self.active.return_value = False
                if uncertain:
                    raise RuntimeError("lost response")
                return dict(OK)

            self.boundary.on_finish = finish
            with self.assertRaisesRegex(ledger.EffectError, "^D5 inactive$"):
                self.submit()
            self.assertTrue(all(r["state"] == "submitted" for r in self.rows()))
        self.active.return_value = True

    def test_inactive_and_absent_registration_refuse_before_work(self):
        self.active.return_value = False
        with mock.patch.object(registry, "lookup", side_effect=AssertionError("inactive lookup")):
            with self.assertRaisesRegex(authority.AuthorityError, "^D5 is inactive$"):
                self.submit()
        self.active.return_value = True
        with mock.patch.object(registry, "lookup", return_value=None), \
                mock.patch.object(protocol, "pin", side_effect=AssertionError("unregistered pin")):
            with self.assertRaisesRegex(authority.AuthorityError, "^not registered$"):
                self.submit()
        self.assertEqual(self.boundary.calls, [])

    def test_ambient_start_refused_before_effect(self):
        with protocol.pin(self.directory) as record, record.drain(), record.start():
            with self.assertRaisesRegex(authority.AuthorityError, "^D5 submission inside START is forbidden$"):
                self.submit()
            self.assertEqual(self.probe("start"), "blocked")
        self.assertEqual(self.boundary.calls, [])
        self.assertEqual(self.rows(), [])

    def test_post_start_interrupt_finishes_under_drain(self):
        lines, first = inspect.getsourcelines(submission.submit)
        target = first + next(i for i, line in enumerate(lines)
                              if line.strip() == "if type(state) is not Initiation:")
        fired = []
        def interrupt(frame, event, arg):
            if event == "line" and frame.f_code is submission.submit.__code__ and frame.f_lineno == target:
                sys.settrace(None)
                fired.append(True)
                raise KeyboardInterrupt("post-start")
            return interrupt
        def finish():
            self.assertEqual(self.probe("drain"), "blocked")
            self.assertEqual(self.probe("start"), "available")
            return dict(OK)
        self.boundary.on_finish = finish
        previous = sys.gettrace()
        try:
            sys.settrace(interrupt)
            with self.assertRaisesRegex(KeyboardInterrupt, "post-start"):
                self.submit()
        finally:
            sys.settrace(previous)
        self.assertEqual(fired, [True])
        self.assertEqual([c[0] for c in self.boundary.calls], ["start", "finish"])
        self.assertEqual(self.rows()[0]["state"], "unknown")
        self.assertEqual(self.probe("drain"), "available")

    def test_immutable_operation_subclass(self):
        class MyOp(submission.Operation): pass
        class MyBytes(bytes):
            def hex(self): return "display"
        self.operation = MyOp("A", "fixture", self.operation.scope, MyBytes(self.operation.intent))
        with self.assertRaises(FrozenInstanceError):
            self.operation.intent = b"changed"
        self.assertEqual(self.submit().initiation, submission.Initiation.SUBMITTED)
        self.assertIs(self.boundary.calls[0][1], self.operation)
        self.assertEqual(self.rows()[0]["binding"]["correlation"]["intent"], bytes.hex(self.operation.intent))
        self.assertEqual(self.rows()[0]["state"], "completed")

    def test_immutable_operation_and_invalid_initiation(self):
        with self.assertRaises(FrozenInstanceError):
            self.operation.intent = b"changed"
        fields = dict(request_id="A", kind="fixture", scope=self.operation.scope, intent=b"effect")
        for field, bad in (("scope", bytearray(fields["scope"])), ("intent", bytearray(b"effect")),
                           ("request_id", []), ("kind", [])):
            with self.assertRaisesRegex(ValueError, "immutable fields"):
                submission.Operation(**dict(fields, **{field: bad}))
        self.operation = fields
        with self.assertRaisesRegex(ValueError, "immutable fields"):
            self.submit()
        self.operation = submission.Operation(**fields)
        for invalid in (None, "submitted", True):
            self.boundary = DeterministicBoundary()
            self.boundary.state = invalid
            with self.assertRaisesRegex(ValueError, "invalid initiation"):
                self.submit()
            self.assertEqual([c[0] for c in self.boundary.calls], ["start", "finish"])
        self.assertEqual([r["state"] for r in self.rows()], ["unknown"] * 3)

    def test_registered_source_limit_and_recovery(self):
        before = (self.directory / store.RECORD_NAME).read_bytes()
        moved = self.root / "moved"
        self.repo.rename(moved)
        self.args = moved, "p", self.state_dir
        with mock.patch.object(store, "read", side_effect=AssertionError("store")), \
                mock.patch.object(ledger, "prepare", side_effect=AssertionError("prepare")):
            with self.assertRaises(FileNotFoundError) as caught:
                self.submit()
        self.assertEqual(caught.exception.filename, str(self.mandate))
        self.assertEqual((self.directory / store.RECORD_NAME).read_bytes(), before)
        self.assertEqual(self.boundary.calls, [])
        moved.rename(self.repo)
        self.args = self.repo, "p", self.state_dir
        self.assertEqual(self.submit().outcome, OK)

    def test_nested_response_call_has_distinct_admission_and_accounting(self):
        inner = DeterministicBoundary()
        outer = self.boundary

        def finish():
            self.assertEqual(self.probe("start"), "available")
            self.boundary = inner
            self.operation = submission.Operation("B", "fixture", self.operation.scope, b"inner")
            result = self.submit()
            self.assertEqual(result.outcome, OK)
            self.assertEqual(self.probe("drain"), "blocked")
            return dict(OK)

        outer.on_finish = finish
        self.assertEqual(self.submit().outcome, OK)
        self.assertEqual({r["binding"]["correlation"]["request_id"] for r in self.rows()}, {"A", "B"})
        self.assertEqual([r["state"] for r in self.rows()], ["completed", "completed"])


if __name__ == "__main__":
    unittest.main()
