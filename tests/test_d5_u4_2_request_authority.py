"""U4.2 authority seams, restart refusals and unit-local M6 observations."""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, asdict, replace
import json
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest import mock
import uuid

from tests import test_d5_submission as t
import d5_effect_child as child
import d5_process as process

E, C, S = t.ledger, t.completion, t.submission
Denied = t.authority.AuthorityError

FIXTURES = Path(__file__).parent / "fixtures/d5"
DEFAULT_ACTIVE = lambda: False  # Explicit pre-cutover fixture, not the production default.


def tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def ledger_capture():
    """Same fixture on base and candidate; no real subprocess/external effects.

    Random IDs and ledger inode bindings are fixed at their source. Only the
    new authority metadata and its dependent HMAC are removed for comparison;
    states, outcomes and correlations are compared by their exact row digest;
    commands and journal bytes remain intact. Registration reads are fixture-local.
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
            rows = []
            for path in sorted((c.directory / E.DIRECTORY).glob("*.json")):
                row = json.loads(path.read_bytes())
                if "recording_authority" in row["binding"]:
                    if row["binding"].pop("recording_authority") != "ledger":
                        raise AssertionError("non-ledger row in ledger capture")
                    row["receipt_verifier"] = E._verifier(
                        row["effect_id"], E._encode(row["binding"]), b"s" * 32)
                rows.append(hashlib.sha256(E._encode(row)).hexdigest())
            observations[mode] = dict(rows=rows, visible=visible,
                calls=[call[0] for call in c.boundary.calls],
                launches=[str(call) for call in launched.call_args_list],
                external=[str(call) for call in network.call_args_list],
                journals={str(p.relative_to(c.root)): p.read_text()
                    for p in c.root.rglob("*merge*json*")},
                repository_changed=tree_bytes(c.repo) != before,
                holders=[S.os.dup.call_count, S.os.close.call_count],
                drain=c.probe("drain"))
    return observations


def restart():
    data = json.load(sys.stdin)
    receipt = E.Receipt(data["effect_id"], data["directory"],
        bytes.fromhex(data["binding"]), bytes.fromhex(data["secret"]))
    attempt = E.Attempt(receipt.effect_id, receipt._directory, receipt._binding, receipt._secret)
    results = []
    with mock.patch.object(E.registry, "d5_active", return_value=True):
        for call in (lambda: E.complete(receipt, dict(status="succeeded", detail="fixture response")),
                     lambda: E._classify(attempt, "unknown"),
                     lambda: C._Completion(receipt).uncertain(),
                     lambda: C.bind(receipt)):
            try:
                call()
            except E.EffectError:
                results.append("refused")
            else:
                results.append("accepted")
    print(json.dumps(results))


class TestRequestAuthority(unittest.TestCase):
    eq = unittest.TestCase.assertEqual
    raises = unittest.TestCase.assertRaises

    def setUp(self):
        self.c = t.TestSubmission()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.c.stack.enter_context(mock.patch.object(t.registry, "lookup", return_value=self.c.entry))

    def snapshot(self):
        return tree_bytes(self.c.root)

    def operation(self, kind="merge"):
        return replace(self.c.operation, kind=kind)

    def prepare(self, kind="fixture"):
        c = self.c
        return E.prepare(*c.args, record_id=c.original["grant_id"],
            custody=dict(generation=1, session_id="native", anchor_pid=42, anchor_start_us=9000001),
            kind=kind, scope=c.scope, correlation={"request_id": "A"})

    def legacy(self, kind, tag=None, state="submitted"):
        """Authentic synthetic old receipt: not a wrong-MAC rejection test."""
        receipt = E._classify(self.prepare(), "submitted")
        path = Path(receipt._directory) / E.DIRECTORY / (receipt.effect_id + ".json")
        row = json.loads(path.read_bytes())
        row["binding"].pop("recording_authority", None)
        row["binding"]["kind"] = kind
        if tag is not None:
            row["binding"]["recording_authority"] = tag
        binding = E._encode(row["binding"])
        row["receipt_verifier"] = E._verifier(receipt.effect_id, binding, receipt._secret)
        row["state"] = state
        row["outcome"] = dict(t.OK) if state == "completed" else None
        path.write_bytes(E._encode(row) + b"\n")
        return replace(receipt, _binding=binding), path

    def test_closed_classification_and_immutable_request(self):
        op = self.operation()
        self.eq(op.recording_authority, "journal")
        self.eq(self.c.operation.recording_authority, "ledger")
        with self.raises(FrozenInstanceError):
            op.kind = "fixture"
        with self.raises((FrozenInstanceError, AttributeError)):
            op.recording_authority = "ledger"
        with self.raises(TypeError):
            t.admission.RECORDING_AUTHORITIES["merge"] = "ledger"
        for kind in ("Merge", "merge ", "unlisted", "", None):
            with self.subTest(kind=kind), self.raises(ValueError):
                self.operation(kind)

    def test_caller_and_serialized_authority_cannot_override_kind(self):
        raw = asdict(self.operation())
        raw.update(scope=raw["scope"].hex(), intent=raw["intent"].hex())
        for tag in (None, "ledger", "journal", "", False):
            encoded = dict(raw)
            if tag is not None:
                encoded["recording_authority"] = tag
            decoded = json.loads(json.dumps(encoded))
            decoded.update(scope=bytes.fromhex(decoded["scope"]), intent=bytes.fromhex(decoded["intent"]))
            with self.subTest(tag=tag):
                if tag is not None:
                    with self.raises(TypeError):
                        S.Operation(**decoded)
                else:
                    self.c.operation = S.Operation(**decoded)
                    with self.assertRaisesRegex(Denied, "journal binding"):
                        self.c.submit()
        self.eq(self.c.rows(), [])
        self.eq(self.c.boundary.calls, [])

    def test_m6_premature_activation_zero_effects_and_no_custody(self):
        c = self.c
        c.operation = self.operation()
        self.eq(process.DESIGNATED_BOOTSTRAPS, frozenset())
        c.probe("drain")
        before = tree_bytes(c.root)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(t.registry, "d5_active", DEFAULT_ACTIVE))
            stack.enter_context(mock.patch.object(t.registry.d5_activation, "d5_active", return_value=True))
            probes = [stack.enter_context(mock.patch.object(target, name, wraps=getattr(target, name)))
                for target, name in ((t.protocol._Protocol, "start"), (t.protocol, "pin"),
                    (E, "_write"), (subprocess, "Popen"), (socket, "create_connection"))]
            self.assertTrue(t.registry.d5_active())
            with self.assertRaisesRegex(Denied, "journal binding"):
                c.submit()
            with self.assertRaisesRegex(Denied, "not designated"):
                process.protected_spawn(str(c.directory), -1, "unbound-merge")
            for probe in probes:
                probe.assert_not_called()
        self.eq(tree_bytes(c.root), before)
        self.eq(c.rows(), [])
        self.eq(c.boundary.calls, [])
        self.eq(c.probe("drain"), "available")
        S.os.dup.assert_not_called()

    def test_unknown_legacy_operation_refused_before_start(self):
        operation = object.__new__(S.Operation)
        for key, value in asdict(self.c.operation).items():
            object.__setattr__(operation, key, "unlisted" if key == "kind" else value)
        self.c.operation = operation
        before = self.snapshot()
        with self.assertRaisesRegex(Denied, "not classified"):
            self.c.submit()
        self.eq(self.snapshot(), before)
        self.eq(self.c.boundary.calls, [])
        self.eq(self.c.probe("drain"), "available")

    def test_ledger_prepare_refuses_journal_and_unknown_without_writes(self):
        before = self.snapshot()
        for kind in ("merge", "unlisted"):
            with self.subTest(kind=kind), self.raises(E.EffectError):
                self.prepare(kind)
            self.eq(self.snapshot(), before)
        self.assertFalse((self.c.directory / E.DIRECTORY).exists())

    def test_authority_is_durable_and_legacy_ledger_remains_completable(self):
        receipt = E._classify(self.prepare(), "submitted")
        self.eq(receipt.recording_authority, "ledger")
        self.eq(json.loads(receipt._binding)["recording_authority"], "ledger")
        with self.raises(FrozenInstanceError):
            receipt._binding = b"{}"
        self.eq(C.bind(receipt).complete(t.OK), t.OK)
        for kind in ("fixture", "test-effect", "synthetic"):
            legacy, _ = self.legacy(kind)
            self.eq(C.bind(legacy).complete(t.OK), t.OK)

    def test_legacy_journal_completion_and_uncertainty_never_mutate(self):
        for tag in (None, "journal", "ledger", "garbage"):
            for state in ("attempting", "submitted", "completed"):
                receipt, _ = self.legacy("merge", tag, state)
                attempt = E.Attempt(receipt.effect_id, receipt._directory,
                                           receipt._binding, receipt._secret)
                before = self.snapshot()
                calls = [lambda: E.complete(receipt, t.OK),
                         lambda: E._classify(attempt, "submitted"),
                         lambda: E._classify(attempt, "unknown"),
                         lambda: C.bind(receipt),
                         lambda: C._Completion(receipt).complete(t.OK),
                         lambda: C._Completion(receipt).uncertain()]
                for call in calls:
                    with self.subTest(tag=tag, state=state, call=call), self.raises(E.EffectError):
                        call()
                    self.eq(self.snapshot(), before)
                diagnostics = E.unresolved(*self.c.args)
                self.assertTrue(any(row.get("file") == receipt.effect_id + ".json" and
                    row.get("reconciliation_required") and "state" not in row for row in diagnostics))

    def test_receipt_malformed_authority_is_not_a_default(self):
        for kind, tag in (("unlisted", None), ("fixture", "journal"), ("fixture", False)):
            receipt, _ = self.legacy(kind, tag)
            before = self.snapshot()
            with self.raises(E.EffectError):
                C.bind(receipt).complete(t.OK)
            self.eq(self.snapshot(), before)

    def test_bound_capability_rechecks_persisted_authority(self):
        receipt = E._classify(self.prepare(), "submitted")
        capability = C.bind(receipt)
        path = Path(receipt._directory) / E.DIRECTORY / (receipt.effect_id + ".json")
        row = json.loads(path.read_bytes())
        row["binding"]["recording_authority"] = "journal"
        path.write_bytes(E._encode(row))
        before = self.snapshot()
        for call in (lambda: capability.complete(t.OK), capability.uncertain):
            with self.raises(E.EffectError):
                call()
            self.eq(self.snapshot(), before)

    def test_serialized_receipt_restart_cannot_ledger_complete_merge(self):
        for tag in (None, "journal", "ledger"):
            receipt, _ = self.legacy("merge", tag)
            payload = dict(effect_id=receipt.effect_id, directory=receipt._directory,
                binding=receipt._binding.hex(), secret=receipt._secret.hex())
            before = self.snapshot()
            result = subprocess.run([sys.executable, "-c",
                "from tests.test_d5_u4_2_request_authority import restart; restart()"], cwd=t.ROOT,
                input=json.dumps(payload), text=True, capture_output=True, timeout=30)
            self.eq(result.returncode, 0, result.stderr)
            self.eq(json.loads(result.stdout), ["refused"] * 4)
            self.eq(self.snapshot(), before)

    def test_bootstrap_refuses_journal_before_importing_drain(self):
        c = self.c
        with t.protocol.pin(c.directory) as record, record.drain() as fd:
            original = os.get_inheritable(fd)
            before = tree_bytes(c.root)
            with mock.patch.object(child.protocol, "pin", wraps=child.protocol.pin) as pin:
                with self.assertRaisesRegex(Denied, "journal binding"):
                    child.bootstrap(fd, str(c.directory), *c.args, **c.request,
                        operation=self.operation(), boundary=c.boundary)
                pin.assert_not_called()
            self.eq(os.get_inheritable(fd), original)
            self.eq(tree_bytes(c.root), before)
            self.eq(c.probe("drain"), "blocked")
        self.eq(c.probe("drain"), "available")
        self.eq(c.rows(), [])
        self.eq(c.boundary.calls, [])

    def test_bootstrap_ledger_still_auto_completes(self):
        c = self.c
        with t.protocol.pin(c.directory) as record, record.drain() as fd:
            inherited = os.dup(fd)
            try:
                result = child.bootstrap(inherited, str(c.directory), *c.args,
                    **c.request, operation=c.operation, boundary=c.boundary)
                self.eq(result.outcome, t.OK)
                with self.raises(OSError):
                    os.fstat(inherited)
            finally:
                try:
                    os.close(inherited)
                except OSError:
                    pass
        self.eq(c.rows()[0]["state"], "completed")
        self.eq(c.probe("drain"), "available")

    def test_m6_ledger_observations_match_base(self):
        self.c.doCleanups()
        baseline = json.loads((FIXTURES / "u4_2_baseline.json").read_text())
        self.eq(baseline["base"], "f937e7a0b821196ad04ca1f283e7dcac4fa80c23")
        self.eq(ledger_capture(), baseline["observations"])


if __name__ == "__main__":
    unittest.main()
