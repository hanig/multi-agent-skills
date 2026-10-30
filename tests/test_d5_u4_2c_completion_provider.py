"""U4.2-c provider dispatch, retain-on-unknown and unit-local M6 captures."""
from contextlib import nullcontext, ExitStack
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import unittest
from unittest import mock
import uuid

from tests import test_d5_submission as t
from tests.test_d5_u4_2_request_authority import tree_bytes

S, C, E = t.submission, t.completion, t.ledger
BASE = "17b40983a6993681d95fd9f7029eb21dbc82c5de"
FIXTURES = Path(__file__).parent / "fixtures/d5"


def ledger_capture():
    """Capture complete ledger row digests, commands, journal bytes and outcomes.

    Random identities and inode bindings are fixed at their source on both
    revisions. No row fields are removed, and no external command is launched.
    """
    observations = {}
    for mode in ("inactive", "submitted", "failed", "not_submitted", "unknown", "finish_error"):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(uuid, "uuid4", return_value=uuid.UUID(int=1)))
            c = t.TestSubmission()
            c.setUp()
            stack.callback(c.doCleanups)
            stack.enter_context(mock.patch.object(t.registry, "lookup", return_value=c.entry))
            stack.enter_context(mock.patch.object(E, "_key", return_value=[1, 2]))
            stack.enter_context(mock.patch.object(E.secrets, "token_hex", return_value="a" * 32))
            stack.enter_context(mock.patch.object(E.secrets, "token_bytes", return_value=b"s" * 32))
            if mode == "inactive":
                c.active.return_value = False
            elif mode == "not_submitted":
                c.boundary.state = S.Initiation.NOT_SUBMITTED
            elif mode == "unknown":
                c.boundary.state = S.Initiation.UNCERTAIN
            elif mode == "failed":
                c.boundary.on_finish = lambda: dict(status="failed", detail="fixture failure")
            elif mode == "finish_error":
                def failed():
                    raise RuntimeError("fixture interrupted")
                c.boundary.on_finish = failed
            before = tree_bytes(c.repo)
            launched = stack.enter_context(mock.patch.object(subprocess, "Popen",
                side_effect=AssertionError("unexpected launch")))
            network = stack.enter_context(mock.patch.object(socket, "create_connection",
                side_effect=AssertionError("unexpected external request")))
            try:
                result = c.submit()
                visible = [result.effect_id, result.initiation.value, result.outcome]
            except (ValueError, RuntimeError) as exc:
                visible = [type(exc).__name__, str(exc)]
            observations[mode] = dict(
                rows=[hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in sorted((c.directory / E.DIRECTORY).glob("*.json"))],
                visible=visible, calls=[call[0] for call in c.boundary.calls],
                launches=[str(call) for call in launched.call_args_list],
                external=[str(call) for call in network.call_args_list],
                journals={str(p.relative_to(c.root)): p.read_bytes().hex()
                          for p in c.root.rglob("*merge*json*")},
                repository_changed=tree_bytes(c.repo) != before,
                holders=[S.os.dup.call_count, S.os.close.call_count], drain=c.probe("drain"))
    return observations


@dataclass(frozen=True)
class Handle:
    effect_id: str
    binding: str
    submitted: bool = False


@dataclass(frozen=True)
class JournalCapability:
    provider: object
    handle: Handle

    def uncertain(self):
        p = self.provider
        p.event("uncertain")
        row = p.read(self.handle)
        row["state"] = "unknown"
        p.write(row)

    def complete(self, outcome):
        p = self.provider
        p.event("complete")
        row = p.read(self.handle)
        if not self.handle.submitted or row["state"] != "submitted":
            raise ValueError("fixture not submitted")
        p.validate_outcome(outcome)
        receipt = dict(binding=self.handle.binding, outcome=outcome)
        p.write_receipt(receipt)
        # The fixture models durable readback; no real merge provider is shipped.
        loaded = json.loads(p.receipt.read_text())
        if loaded != receipt:
            raise ValueError("fixture receipt mismatch")
        return loaded["outcome"]


class JournalProvider(C.Provider):
    """Synthetic storage only; implements the provider's bound readback contract."""
    def __init__(self, case):
        self.case = case
        self.events = []
        self.path = case.c.root / "fixture-merge-intent.json"
        self.receipt = case.c.root / "fixture-merge-receipt.json"

    def event(self, name):
        self.events.append(name)
        self.case.assertEqual(self.case.c.probe("drain"), "blocked")

    def persist(self, path, row):
        with path.open("w") as out:
            json.dump(row, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write(self, row):
        self.persist(self.path, row)

    def write_receipt(self, row):
        self.persist(self.receipt, row)

    def read(self, handle):
        row = json.loads(self.path.read_text())
        if row["binding"] != handle.binding or row["effect_id"] != handle.effect_id:
            raise ValueError("fixture binding mismatch")
        return row

    def prepare(self, checkout, plan_id, state_dir, **binding):
        self.event("prepare")
        self.case.assertEqual(self.case.c.probe("start"), "blocked")
        self.case.assertEqual((checkout, plan_id, state_dir), self.case.c.args)
        self.case.assertEqual(binding["kind"], "merge")
        handle = Handle(binding["correlation"]["request_id"], json.dumps(binding, sort_keys=True))
        if self.path.exists():
            raise ValueError("fixture unresolved request cannot replay")
        self.write(dict(effect_id=handle.effect_id, binding=handle.binding, state="attempting"))
        return handle

    def classify(self, attempt, state):
        self.event(state)
        row = self.read(attempt)
        row["state"] = state
        self.write(row)
        return replace(attempt, submitted=True) if state == "submitted" else None

    def bind(self, handle):
        self.event("bind")
        self.read(handle)
        return JournalCapability(self, handle)

    def validate_outcome(self, outcome):
        if (type(outcome) is not dict or outcome.get("status") != "succeeded" or
                type(outcome.get("detail")) is not str or
                set(outcome) - {"status", "detail", "provider", "recording_authority"}):
            raise ValueError("fixture invalid outcome")


class TestCompletionProvider(unittest.TestCase):
    def setUp(self):
        self.c = t.TestSubmission()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.provider = JournalProvider(self)
        self.c.operation = replace(self.c.operation, kind="merge")

    def fixture(self):
        self.c.boundary.observe = lambda operation: None
        self.c.boundary.check_observation = lambda operation, observation: None
        self.c.stack.enter_context(mock.patch.object(C, "_PROVIDERS",
            dict(C._PROVIDERS, journal=self.provider)))
        # A failing fallback must be observed even when R6 catches its exception.
        self.ledger_calls = [self.c.stack.enter_context(mock.patch.object(target, name,
            side_effect=AssertionError("journal reached ledger: " + name)))
            for target, name in ((E, "prepare"), (E, "_classify"), (E, "complete"),
                                 (E, "_outcome"), (C, "bind"))]

    def assert_no_ledger(self):
        for call in self.ledger_calls:
            call.assert_not_called()
        self.assertEqual(self.c.rows(), [])
        self.assertFalse((self.c.directory / E.DIRECTORY).exists())

    def test_journal_dispatch_preparation_binding_and_completion(self):
        self.fixture()
        validated = t.admission._validate_locked
        def validate(*args, **kwargs):
            validated(*args, **kwargs)
            self.provider.event("validate")
        def start():
            self.provider.event("start")
            self.assertEqual(self.c.probe("start"), "blocked")
            row = json.loads(self.provider.path.read_text())
            self.assertEqual(row["state"], "attempting")
            bound = json.loads(row["binding"])
            self.assertEqual(bound["record_id"], self.c.request["grant_id"])
            self.assertEqual(bound["custody"], dict(generation=1, session_id="native",
                anchor_pid=t.ANCHOR.pid, anchor_start_us=t.ANCHOR.start_us))
            self.assertEqual(bound["scope"], self.c.scope)
            self.assertEqual(bound["correlation"], dict(request_id="A", intent=self.c.operation.intent.hex()))
        def finish():
            self.provider.event("finish")
            self.assertEqual(self.c.probe("start"), "available")
            return dict(t.OK)
        self.c.boundary.on_start, self.c.boundary.on_finish = start, finish
        with mock.patch.object(t.admission, "_validate_locked", side_effect=validate):
            result = self.c.submit()
        self.assertEqual(result.effect_id, "A")
        self.assertEqual(result.outcome, t.OK)
        self.assertEqual(self.provider.events,
            ["validate", "prepare", "validate", "start", "submitted", "bind", "finish", "complete"])
        self.assertEqual(json.loads(self.provider.receipt.read_text())["outcome"], t.OK)
        self.assertEqual(self.c.probe("drain"), "available")
        self.assert_no_ledger()

    def test_provider_is_fixed_before_result_and_never_from_result(self):
        self.fixture()
        result = dict(t.OK, provider="ledger", recording_authority="ledger")
        self.c.boundary.on_finish = lambda: result
        with mock.patch.object(C, "provider_for", wraps=C.provider_for) as select:
            self.assertEqual(self.c.submit().outcome, result)
        select.assert_called_once_with("merge")
        self.assertEqual(self.provider.events, ["prepare", "submitted", "bind", "complete"])
        self.assert_no_ledger()

    def test_uncertainty_uses_same_provider_and_never_ledger(self):
        self.fixture()
        self.c.boundary.state = S.Initiation.UNCERTAIN
        result = self.c.submit()
        self.assertIsNone(result.outcome)
        self.assertEqual(self.provider.events, ["prepare", "bind", "uncertain"])
        self.assertEqual(json.loads(self.provider.path.read_text())["state"], "unknown")
        self.assertFalse(self.provider.receipt.exists())
        self.assertEqual(self.c.probe("drain"), "blocked")
        self.assert_no_ledger()

    def test_not_submitted_is_durable_and_does_not_finish(self):
        self.fixture()
        self.c.boundary.state = S.Initiation.NOT_SUBMITTED
        self.assertIsNone(self.c.submit().outcome)
        self.assertEqual(self.provider.events, ["prepare", "not_submitted"])
        self.assertEqual(json.loads(self.provider.path.read_text())["state"], "not_submitted")
        self.assertEqual([c[0] for c in self.c.boundary.calls], ["start"])
        self.assertEqual(self.c.probe("drain"), "available")
        self.assert_no_ledger()

    def test_journal_prepare_failure_never_starts(self):
        self.fixture()
        with mock.patch.object(self.provider, "write", side_effect=OSError("fixture sync failure")):
            with self.assertRaisesRegex(OSError, "sync failure"):
                self.c.submit()
        self.assertEqual(self.c.boundary.calls, [])
        self.assertEqual(self.c.probe("drain"), "available")
        self.assert_no_ledger()

    def test_exit_reap_timeout_missing_and_malformed_are_not_completion(self):
        cases = [0, 1, subprocess.CompletedProcess(["fixture"], 0), None,
                 {"merged": True, "attested": True}, {"status": "succeeded"},
                 TimeoutError("fixture timeout")]
        for outcome in cases:
            with self.subTest(outcome=outcome):
                c = TestCompletionProvider()
                c.setUp()
                try:
                    c.fixture()
                    def finish():
                        if isinstance(outcome, BaseException):
                            raise outcome
                        return outcome
                    c.c.boundary.on_finish = finish
                    with c.assertRaises((ValueError, TimeoutError)):
                        c.c.submit()
                    c.assertEqual(c.provider.events, ["prepare", "submitted", "bind", "bind", "uncertain"])
                    c.assertEqual(json.loads(c.provider.path.read_text())["state"], "unknown")
                    c.assertFalse(c.provider.receipt.exists())
                    c.assertEqual(c.c.probe("drain"), "blocked")
                    c.assert_no_ledger()
                finally:
                    c.doCleanups()

    def test_missing_malformed_or_unbound_readback_retains_holder(self):
        for mode in ("missing", "malformed", "binding", "sync"):
            with self.subTest(mode=mode):
                c = TestCompletionProvider()
                c.setUp()
                try:
                    c.fixture()
                    def write(row):
                        if mode == "missing":
                            return
                        if mode == "malformed":
                            c.provider.receipt.write_text("{")
                        elif mode == "binding":
                            c.provider.persist(c.provider.receipt, dict(row, binding="other"))
                        else:
                            raise OSError("fixture sync failure")
                    with mock.patch.object(c.provider, "write_receipt", side_effect=write):
                        with c.assertRaises((ValueError, OSError)):
                            c.c.submit()
                    c.assertEqual(c.c.probe("drain"), "blocked")
                    c.assertEqual(json.loads(c.provider.path.read_text())["state"], "submitted")
                    c.assert_no_ledger()
                finally:
                    c.doCleanups()

    def test_malformed_capability_return_does_not_establish_C(self):
        self.fixture()
        with mock.patch.object(JournalCapability, "complete", return_value=None):
            with self.assertRaisesRegex(ValueError, "invalid outcome"):
                self.c.submit()
        self.assertFalse(self.provider.receipt.exists())
        self.assertEqual(self.c.probe("drain"), "blocked")
        self.assert_no_ledger()

    def test_binding_loss_never_falls_back(self):
        self.fixture()
        def finish():
            row = json.loads(self.provider.path.read_text())
            self.provider.write(dict(row, binding="other generation"))
            return dict(t.OK)
        self.c.boundary.on_finish = finish
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.c.submit()
        self.assertFalse(self.provider.receipt.exists())
        self.assertEqual(self.c.probe("drain"), "blocked")
        self.assert_no_ledger()

    def test_m6_premature_activation_refuses_before_START_without_effects(self):
        import d5_merge_journal as J
        self.assertIs(C.provider_for("merge"), J.PROVIDER)
        with self.assertRaises(TypeError):
            C._PROVIDERS["journal"] = self.provider
        before = tree_bytes(self.c.root)
        with ExitStack() as stack:
            stack.enter_context(nullcontext())
            probes = [stack.enter_context(mock.patch.object(target, name,
                side_effect=AssertionError("unexpected side effect: " + name)))
                for target, name in ((t.registry, "lookup"), (t.protocol, "pin"),
                    (t.protocol._Protocol, "start"), (E, "prepare"), (E, "complete"),
                    (subprocess, "Popen"), (socket, "create_connection"))]
            with self.assertRaisesRegex(t.authority.AuthorityError, "journal binding"):
                self.c.submit()
            for probe in probes:
                probe.assert_not_called()
        self.assertEqual(tree_bytes(self.c.root), before)
        self.assertEqual(self.c.boundary.calls, [])
        S.os.dup.assert_not_called()
        self.assertFalse(self.provider.path.exists())
        self.assertEqual(self.c.rows(), [])

    def test_release_expression_unchanged(self):
        # Reuse the merged structural guard: all six facts and exactly one close.
        self.c.test_single_terminal_release_gate()

    def test_m6_ledger_before_after_equivalence(self):
        self.c.doCleanups()
        baseline = json.loads((FIXTURES / "u4_2c_baseline.json").read_text())
        self.assertEqual(baseline["base"], BASE)
        self.assertEqual(ledger_capture(), baseline["observations"])


if __name__ == "__main__":
    unittest.main()
