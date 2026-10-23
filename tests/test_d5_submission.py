"""R6 consumer tests: real registration/admission/ledger, ordered lock probes."""
from contextlib import contextmanager, ExitStack
from dataclasses import FrozenInstanceError
import ast
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
    eq = unittest.TestCase.assertEqual
    refuse = unittest.TestCase.assertRaisesRegex

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
        holders = set()
        local_os = mock.Mock(wraps=os)
        def duplicate(fd):
            fd = os.dup(fd)
            holders.add(fd)
            return fd
        def close(fd):
            os.close(fd)
            holders.remove(fd)
        local_os.dup.side_effect = duplicate
        local_os.close.side_effect = close
        self.stack.enter_context(mock.patch.object(submission, "os", local_os))
        self.addCleanup(lambda: [os.close(fd) for fd in holders])

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
        self.eq(len(results), 1)
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
            self.eq(events, ["validate", "prepare", "validate"])
            self.eq(self.probe("start"), "blocked")
            self.eq(self.rows()[0]["state"], "attempting")

        self.boundary.on_start = boundary
        with mock.patch.object(admission, "_validate_locked", side_effect=validated), \
                mock.patch.object(ledger, "prepare", side_effect=prepared):
            result = self.submit()
        self.eq(result.initiation, submission.Initiation.SUBMITTED)
        self.eq(result.outcome, OK)
        row, = self.rows()
        self.eq(row["effect_id"], result.effect_id)
        self.eq(row["state"], "completed")
        self.eq(row["binding"]["correlation"], {"request_id": "A", "intent": self.operation.intent.hex()})
        self.eq(row["binding"]["scope"], self.scope)
        self.eq(row["binding"]["record_id"], self.request["grant_id"])
        self.eq(row["binding"]["custody"]["generation"], 1)

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
                        self.eq(control.record_mandate_invalidation(*self.args, now=150), "mandate_changed")
                        self.mandate.write_bytes(MANDATE)
                    else:
                        control.revoke(*self.args, 150, "stop")
                finally:
                    resume.set()
                    result = self.join(thread, results)
            self.assertIsInstance(result, authority.AuthorityError)
            self.eq(str(result), "D5 authority is invalidated")
            self.eq(self.boundary.calls, [])

    def test_prepare_failure_has_no_effect(self):
        for published in (False, True):
            write = ledger._write

            def failing(*args, **kwargs):
                if published:
                    write(*args, **kwargs)
                raise ledger.EffectError(ledger.DURABILITY)

            with self.subTest(published=published), mock.patch.object(ledger, "_write", side_effect=failing):
                with self.refuse(ledger.EffectError, ledger.DURABILITY):
                    self.submit()
            self.eq(self.boundary.calls, [])
        self.eq([r["state"] for r in self.rows()], ["attempting"])

    def test_final_time_after_prepare_including_equality(self):
        prepare = ledger.prepare

        def prepared(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.clock.return_value = 200
            return result

        with mock.patch.object(ledger, "prepare", side_effect=prepared):
            with self.refuse(authority.AuthorityError, "^D5 authority expired$"):
                self.submit()
        self.eq(self.boundary.calls, [])
        self.eq([r["state"] for r in self.rows()], ["not_submitted"])

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
            self.eq(self.probe("start"), "available")
            control.revoke(*self.args, 151, "stop")
            self.eq(store.read(self.directory)["invalidation"]["kind"], "revoked")
            persisted.set()
            self.assertFalse(release.is_set())
            self.eq(self.rows()[0]["state"], "submitted")
        finally:
            release.set()
            result = self.join(thread, results)
        self.assertTrue(persisted.is_set())
        self.assertIsInstance(result, submission.Result)
        self.eq(result.outcome, OK)
        self.eq(self.rows()[0]["state"], "completed")
        self.boundary = DeterministicBoundary()
        self.operation = submission.Operation("B", "fixture", self.operation.scope, b"next")
        with self.refuse(authority.AuthorityError, "^D5 authority is invalidated$"):
            self.submit()
        self.eq(self.boundary.calls, [])

    def test_submission_lifetime_retains_drain(self):
        seen = []
        write = ledger._write

        def observe(phase):
            seen.append(phase)
            self.eq(self.probe("drain"), "blocked")

        def writing(*args, **kwargs):
            observe(args[2]["state"])
            return write(*args, **kwargs)

        self.boundary.on_start = lambda: observe("boundary")
        self.boundary.on_finish = lambda: (observe("response"), dict(OK))[1]
        with mock.patch.object(ledger, "_write", side_effect=writing):
            self.submit()
        self.eq(seen, ["attempting", "boundary", "submitted", "response", "completed"])
        self.eq(self.probe("drain"), "available")

    def test_ambiguous_start_is_not_not_submitted(self):
        for exception in (RuntimeError("ambiguous"), KeyboardInterrupt("interrupted")):
            self.boundary = DeterministicBoundary()

            def fail():
                raise exception

            self.boundary.on_start = fail
            with self.assertRaises(BaseException) as caught:
                self.submit()
            self.assertIs(caught.exception, exception)
            self.eq([c[0] for c in self.boundary.calls], ["start", "finish"])
        self.eq([r["state"] for r in self.rows()], ["unknown", "unknown"])

    def test_explicit_initiation_states(self):
        for state in submission.Initiation:
            self.boundary = DeterministicBoundary()
            self.boundary.state = state
            result = self.submit()
            row = next(r for r in self.rows() if r["effect_id"] == result.effect_id)
            self.eq(result.initiation, state)
            submitted = state is submission.Initiation.SUBMITTED
            self.eq(result.outcome, OK if submitted else None)
            self.eq(row["state"], "completed" if submitted else state.value)
            self.eq([c[0] for c in self.boundary.calls], ["start"] if
                             state is submission.Initiation.NOT_SUBMITTED else ["start", "finish"])

    def test_opaque_helper_refused(self):
        class Opaque:
            start = mock.Mock(return_value=submission.Initiation.SUBMITTED)
            finish = mock.Mock(return_value=dict(OK))

        for boundary in (Opaque(), "fixture command", ["fixture"], None, lambda: None):
            self.boundary = boundary
            with self.refuse(authority.AuthorityError,
                    "^D5 submission requires an explicit effect boundary$"):
                self.submit()
        Opaque.start.assert_not_called()
        Opaque.finish.assert_not_called()
        self.eq(self.rows(), [])

    def test_next_effect_requires_fresh_admission(self):
        self.boundary.on_finish = lambda: dict(OK, detail="next effect B requested")
        self.submit()
        self.eq([c[0] for c in self.boundary.calls], ["start", "finish"])
        self.eq(len(self.rows()), 1)
        control.revoke(*self.args, 151, "stop")
        self.boundary = DeterministicBoundary()
        with self.refuse(authority.AuthorityError, "invalidated"):
            self.submit()
        self.eq(self.boundary.calls, [])

    def test_finish_failure_accounts_uncertainty(self):
        error = RuntimeError("response lost")

        def finish():
            self.eq(self.probe("start"), "available")
            self.eq(self.probe("drain"), "blocked")
            raise error

        self.boundary.on_finish = finish
        with self.assertRaises(RuntimeError) as caught:
            self.submit()
        self.assertIs(caught.exception, error)
        self.eq(self.rows()[0]["state"], "unknown")
        self.eq(self.probe("drain"), "blocked")

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
                holder = []
                local_os = mock.Mock(wraps=os)
                local_os.dup.side_effect = lambda fd: holder.append(os.dup(fd)) or holder[-1]
                with self.subTest(phase=phase, published=published), fault, \
                        mock.patch.object(submission, "os", local_os):
                    with self.assertRaises(ledger.EffectError) as caught:
                        self.submit()
                self.assertIs(caught.exception, error)
                self.assertEqual([c[0] for c in self.boundary.calls], ["start", "finish"])
                self.assertEqual(self.probe("drain"), "blocked")
                os.close(holder[0])  # Test teardown AFTER live-process probe.
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
            with self.refuse(ledger.EffectError, "^D5 inactive$"):
                self.submit()
            self.assertTrue(all(r["state"] == "submitted" for r in self.rows()))
        self.active.return_value = True

    def test_inactive_and_absent_registration_refuse_before_work(self):
        self.active.return_value = False
        with mock.patch.object(registry, "lookup", side_effect=AssertionError("inactive lookup")):
            with self.refuse(authority.AuthorityError, "^D5 is inactive$"):
                self.submit()
        self.active.return_value = True
        with mock.patch.object(registry, "lookup", return_value=None), \
                mock.patch.object(protocol, "pin", side_effect=AssertionError("unregistered pin")):
            with self.refuse(authority.AuthorityError, "^not registered$"):
                self.submit()
        self.eq(self.boundary.calls, [])

    def test_ambient_start_refused_before_effect(self):
        with protocol.pin(self.directory) as record, record.drain(), record.start():
            with self.refuse(authority.AuthorityError, "^D5 submission inside START is forbidden$"):
                self.submit()
            self.eq(self.probe("start"), "blocked")
        self.eq(self.boundary.calls, [])
        self.eq(self.rows(), [])

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
            self.eq(self.probe("drain"), "blocked")
            self.eq(self.probe("start"), "available")
            return dict(OK)
        self.boundary.on_finish = finish
        previous = sys.gettrace()
        try:
            sys.settrace(interrupt)
            with self.refuse(KeyboardInterrupt, "post-start"):
                self.submit()
        finally:
            sys.settrace(previous)
        self.eq(fired, [True])
        self.eq([c[0] for c in self.boundary.calls], ["start", "finish"])
        self.eq(self.rows()[0]["state"], "unknown")
        self.eq(self.probe("drain"), "blocked")

    def test_cleanup_interrupt_retains_holder_until_exit(self):
        for mode in ([], ["--finish-entry"]):
            result = subprocess.run([sys.executable,
                str(ROOT / "tests/fixtures/d5/r6_cleanup_interrupt.py")] + mode,
                capture_output=True, text=True, timeout=40)
            self.eq(result.returncode, 0, result.stdout + result.stderr)

    def test_exit_path_matrix(self):
        both = ["start", "finish"]
        E, K = ledger.EffectError, KeyboardInterrupt
        rows = [
            ("no-start", None, "available", [], RuntimeError),
            ("prepare-before", None, "available", [], E),
            ("prepare-after", "attempting", "available", [], E),
            ("revalidate", "not_submitted", "available", [], RuntimeError),
            ("not-submitted", "not_submitted", "available", ["start"], None),
            ("submitted", "completed", "available", both, None),
            ("uncertain-start", "unknown", "blocked", both, None),
            ("start-raises", "unknown", "blocked", both, RuntimeError),
            ("start-close", "attempting", "blocked", ["start"], OSError),
            ("start-exit", "attempting", "blocked", ["start"], RuntimeError),
            ("complete-before", "submitted", "blocked", both, E),
            ("complete-after", "completed", "blocked", both, E),
            ("finish-raises", "unknown", "blocked", both, RuntimeError),
            ("finish-invalid", "unknown", "blocked", both, E),
            ("inactive", "submitted", "blocked", both, E),
            ("uncertainty-before", "attempting", "blocked", both, E),
            ("uncertainty-after", "unknown", "blocked", both, E),
            ("before-A", "not_submitted", "available", [], K),
            ("after-A", "unknown", "blocked", ["finish"], K),
            ("before-S", "attempting", "blocked", ["start"], K),
            ("before-N", "not_submitted", "blocked", ["start"], K),
            ("cleanup-entry", "submitted", "blocked", ["start"], K),
            ("finish-entry", "unknown", "blocked", ["start"], K),
            ("before-F", "unknown", "blocked", both, K),
            ("before-C", "completed", "blocked", both, K),
            ("epilogue", "completed", "blocked", both, K),
        ]
        sites = {"before-A": "A = True", "after-A": "state = boundary.start(operation)",
                 "before-S": "S = True", "before-N": "N = True", "before-F": "F = True",
                 "before-C": "C = True", "finish-entry": "outcome = boundary.finish()",
                 "cleanup-entry": "if S and A and state is not Initiation.NOT_SUBMITTED:",
                 "epilogue": "if H and S and (not A or N or (F and C)):"}
        lines, first = inspect.getsourcelines(submission.submit)
        for name, state, drain, calls, error in rows:
            with self.subTest(path=name):
                c = TestSubmission()
                c.setUp()
                previous = sys.gettrace()
                leaked = []
                try:
                    attempts, fired = [], []
                    prepare, write = ledger.prepare, ledger._write
                    def prepared(*args, **kwargs):
                        attempt = prepare(*args, **kwargs)
                        attempts.append(attempt)
                        return attempt
                    def fail():
                        raise RuntimeError(name)
                    def writing(*args, **kwargs):
                        phase = {"prepare": "attempting", "complete": "completed",
                                 "uncertainty": "unknown"}.get(name.split("-")[0])
                        if args[2]["state"] == phase:
                            if name.endswith("after"):
                                write(*args, **kwargs)
                            raise ledger.EffectError(ledger.DURABILITY)
                        return write(*args, **kwargs)
                    if name in ("not-submitted", "before-N"):
                        c.boundary.state = submission.Initiation.NOT_SUBMITTED
                    if name.startswith("uncertain"):
                        c.boundary.state = submission.Initiation.UNCERTAIN
                    if name == "start-raises":
                        c.boundary.on_start = fail
                    if name == "finish-raises":
                        c.boundary.on_finish = fail
                    if name == "finish-invalid":
                        c.boundary.on_finish = lambda: None
                    if name == "inactive":
                        c.boundary.on_finish = lambda: (setattr(c.active, "return_value", False), dict(OK))[1]
                    with ExitStack() as stack:
                        stack.enter_context(mock.patch.object(ledger, "prepare", side_effect=prepared))
                        stack.enter_context(mock.patch.object(ledger, "_write", side_effect=writing))
                        if name in ("no-start", "revalidate"):
                            validate = admission._validate_locked
                            checks = []
                            def validating(*args, **kwargs):
                                checks.append(True)
                                if name == "no-start" or len(checks) == 2:
                                    fail()
                                return validate(*args, **kwargs)
                            stack.enter_context(mock.patch.object(admission, "_validate_locked", side_effect=validating))
                        if name in ("start-exit", "start-close"):
                            start, close = protocol._Protocol.start, os.close
                            @contextmanager
                            def exiting(pinned):
                                if name == "start-close":
                                    def closing(fd):
                                        if leaked and fd == leaked[0]:
                                            raise OSError("START close")
                                        return close(fd)
                                    stack.enter_context(mock.patch.object(os, "close", side_effect=closing))
                                with start(pinned):
                                    if name == "start-close":
                                        leaked.append(pinned._record._prior(1)[3])
                                    yield
                                fail()
                            stack.enter_context(mock.patch.object(protocol._Protocol, "start", exiting))
                        if name in sites:
                            target = first + next(i for i, line in enumerate(lines) if line.strip() == sites[name])
                            def interrupt(frame, event, arg):
                                if event == "line" and frame.f_code is submission.submit.__code__ and frame.f_lineno == target:
                                    sys.settrace(None)
                                    fired.append(True)
                                    raise KeyboardInterrupt(name)
                                return interrupt
                            sys.settrace(interrupt)
                        if error is None:
                            c.submit()
                        else:
                            with self.assertRaises(error):
                                c.submit()
                    sys.settrace(previous)
                    self.eq(fired, [True] if name in sites else [])
                    self.eq([c[0] for c in c.boundary.calls], calls)
                    self.eq([r["state"] for r in c.rows()], [] if state is None else [state])
                    self.eq(c.probe("drain"), drain)
                    c.active.return_value = True
                    if attempts:
                        if state in ("not_submitted", "completed"):
                            with self.refuse(ledger.EffectError, ledger.CONFLICT):
                                completion.bind(attempts[0]).uncertain()
                        else:
                            completion.bind(attempts[0]).uncertain()
                            state = "unknown"
                        self.eq(c.rows()[0]["state"], state)
                    self.eq(c.probe("drain"), drain)
                    unresolved = ledger.unresolved(*c.args)
                    if state in ("attempting", "submitted", "unknown"):
                        self.eq([r["state"] for r in unresolved], [state])
                        self.assertIs(unresolved[0]["reconciliation_required"], state != "submitted")
                    else:
                        self.eq(unresolved, [])
                finally:
                    sys.settrace(previous)
                    for fd in leaked:
                        os.close(fd)
                    c.doCleanups()

    def test_single_terminal_release_gate(self):
        tree = ast.parse(inspect.getsource(submission.submit))
        for fact in "HANSFC":
            assignment, = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Constant) and n.value.value is True
                and any(isinstance(t, ast.Name) and t.id == fact for t in n.targets)]
            for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
                self.assertNotIn(assignment, list(ast.walk(handler)))
        def closes(node):
            return [n for n in ast.walk(node) if isinstance(n, ast.Call) and
                    isinstance(n.func, ast.Attribute) and n.func.attr == "close"]
        close, = closes(tree)
        self.eq(ast.dump(close), ast.dump(ast.parse("os.close(holder)").body[0].value))
        gate, = [n for n in ast.walk(tree) if isinstance(n, ast.If) and
                 any(isinstance(s, ast.Expr) and s.value is close for s in n.body)]
        self.eq(ast.dump(gate.test), ast.dump(ast.parse("H and S and (not A or N or (F and C))", mode="eval").body))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                self.eq(closes(node), [])
            if isinstance(node, ast.Try):
                for statement in node.finalbody:
                    self.eq(closes(statement), [])

    def test_named_adapter_without_inheritance(self):
        class NamedAdapter:
            def start(self, operation):
                return submission.Initiation.NOT_SUBMITTED
            def finish(self):
                raise AssertionError("not submitted")
        self.assertNotIn(submission.Boundary, NamedAdapter.__mro__)
        submission.Boundary.register(NamedAdapter)
        self.boundary = NamedAdapter()
        self.eq(self.submit().initiation, submission.Initiation.NOT_SUBMITTED)
        self.eq(self.rows()[0]["state"], "not_submitted")
        self.eq(self.probe("drain"), "available")

    def test_immutable_operation_subclass(self):
        class MyOp(submission.Operation): pass
        class MyBytes(bytes):
            def hex(self): return "display"
        self.operation = MyOp("A", "fixture", self.operation.scope, MyBytes(self.operation.intent))
        with self.assertRaises(FrozenInstanceError):
            self.operation.intent = b"changed"
        self.eq(self.submit().initiation, submission.Initiation.SUBMITTED)
        self.assertIs(self.boundary.calls[0][1], self.operation)
        self.eq(self.rows()[0]["binding"]["correlation"]["intent"], bytes.hex(self.operation.intent))
        self.eq(self.rows()[0]["state"], "completed")

    def test_immutable_operation_and_invalid_initiation(self):
        with self.assertRaises(FrozenInstanceError):
            self.operation.intent = b"changed"
        fields = dict(request_id="A", kind="fixture", scope=self.operation.scope, intent=b"effect")
        for field, bad in (("scope", bytearray(fields["scope"])), ("intent", bytearray(b"effect")),
                           ("request_id", []), ("kind", [])):
            with self.refuse(ValueError, "immutable fields"):
                submission.Operation(**dict(fields, **{field: bad}))
        self.operation = fields
        with self.refuse(ValueError, "immutable fields"):
            self.submit()
        self.operation = submission.Operation(**fields)
        for invalid in (None, "submitted", True):
            self.boundary = DeterministicBoundary()
            self.boundary.state = invalid
            with self.refuse(ValueError, "invalid initiation"):
                self.submit()
            self.eq([c[0] for c in self.boundary.calls], ["start", "finish"])
        self.eq([r["state"] for r in self.rows()], ["unknown"] * 3)

    def test_registered_source_limit_and_recovery(self):
        before = (self.directory / store.RECORD_NAME).read_bytes()
        moved = self.root / "moved"
        self.repo.rename(moved)
        self.args = moved, "p", self.state_dir
        with mock.patch.object(store, "read", side_effect=AssertionError("store")), \
                mock.patch.object(ledger, "prepare", side_effect=AssertionError("prepare")):
            with self.assertRaises(FileNotFoundError) as caught:
                self.submit()
        self.eq(caught.exception.filename, str(self.mandate))
        self.eq((self.directory / store.RECORD_NAME).read_bytes(), before)
        self.eq(self.boundary.calls, [])
        moved.rename(self.repo)
        self.args = self.repo, "p", self.state_dir
        self.eq(self.submit().outcome, OK)

    def test_nested_response_call_has_distinct_admission_and_accounting(self):
        inner = DeterministicBoundary()
        outer = self.boundary

        def finish():
            self.eq(self.probe("start"), "available")
            self.boundary = inner
            self.operation = submission.Operation("B", "fixture", self.operation.scope, b"inner")
            result = self.submit()
            self.eq(result.outcome, OK)
            self.eq(self.probe("drain"), "blocked")
            return dict(OK)

        outer.on_finish = finish
        self.eq(self.submit().outcome, OK)
        self.eq({r["binding"]["correlation"]["request_id"] for r in self.rows()}, {"A", "B"})
        self.eq([r["state"] for r in self.rows()], ["completed", "completed"])


if __name__ == "__main__":
    unittest.main()
