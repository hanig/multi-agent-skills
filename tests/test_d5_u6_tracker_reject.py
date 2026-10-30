"""U6 row-75 helper refusal and row-76 observation at the consumer boundary."""
from contextlib import nullcontext
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import test_d5_registry as registry_fixture
from tests import test_linear_audit as audit_fixture
from tests.fixtures.d5.ready import ready, ready_installation, missing_installation
from tests.test_d5_u4_0b1_issuance import no_effects
from tests.test_d5_u4_1_active_surface import SurfaceFixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-swarm/scripts"))
import swarm as S
import swarm_outbox as SO
import d5_activation as activation
import merge_unit as M

FIXTURES = ROOT / "tests/fixtures/d5"
ENTRIES = ("_write_sequence", "_allocate_sequence", "set_sequence_floor",
           "emit_intent", "backfill_tracker_intents", "_append_receipt_atomic",
           "record_receipt")
CLOCK = "2026-10-25T00:00:00+0000"


class OutboxFixture:
    def __init__(self, root, state_dir=None):
        self.root = Path(root)
        self.state_dir = state_dir or self.root / "state"
        self.state_dir.mkdir(exist_ok=True)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.us = {"attempt_dir": "/fixture-runs/u/a1", "job_id": "123"}
        with nullcontext(), \
                mock.patch.object(SO.time, "strftime", return_value=CLOCK):
            self.key = SO.emit_intent(self.state_dir, "fixture", "u", "SUBMITTED", self.us)
            self.receipt = SO.record_receipt(self.state_dir, self.key, "ref", by="Operator")
        (self.state_dir / SO.RECEIPTS).unlink()

    def call(self, name):
        calls = {
            "_write_sequence": lambda: SO._write_sequence(self.state_dir,
                {"schema_version": 1, "high_water": 3}),
            "_allocate_sequence": lambda: SO._allocate_sequence(self.state_dir,
                SO._sequence_history(self.state_dir / SO.OUTBOX)),
            "set_sequence_floor": lambda: SO.set_sequence_floor(
                self.state_dir, 3, "Operator", "fixture recovery"),
            "emit_intent": lambda: SO.emit_intent(self.state_dir, "fixture", "v",
                "SUBMITTED", self.us),
            "backfill_tracker_intents": lambda: SO.backfill_tracker_intents(
                self.state_dir, "fixture", {"u": {"tracker": "ARC-1"}}),
            "_append_receipt_atomic": lambda: SO._append_receipt_atomic(self.state_dir, self.receipt),
            "record_receipt": lambda: SO.record_receipt(self.state_dir, self.key, "ref", by="Operator"),
        }
        return calls[name]()

    def tree(self):
        return {str(p.relative_to(self.state_dir)): p.read_text()
                for p in self.state_dir.rglob("*") if p.is_file()}

    def capture(self, name):
        before, output, launches = self.tree(), io.StringIO(), []

        def launch(*args, **kwargs):
            launches.append([args, kwargs])
            raise AssertionError("unexpected launch")

        with ExitStack() as stack:
            # No executables are needed. Never inherit the host's schedulers.
            stack.enter_context(mock.patch.dict(os.environ, {"PATH": str(self.tools)}))
            stack.enter_context(nullcontext())
            stack.enter_context(mock.patch.object(SO.time, "strftime", return_value=CLOCK))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(output))
            for name_ in ("Popen", "run", "call", "check_call", "check_output",
                          "getoutput", "getstatusoutput"):
                stack.enter_context(mock.patch.object(subprocess, name_, launch))
            for name_ in dir(os):
                if (name_.startswith(("exec", "spawn", "posix_spawn")) or name_ in
                        ("fork", "forkpty", "system", "popen", "startfile")):
                    if callable(getattr(os, name_)):
                        stack.enter_context(mock.patch.object(os, name_, launch))
            result = self.call(name)
        after = self.tree()
        return {"result": result, "output": output.getvalue(), "launches": launches,
                "changed": {key: value for key, value in after.items() if before.get(key) != value},
                "removed": sorted(set(before) - set(after))}


class TestTrackerRefusal(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return OutboxFixture(temporary.name)

    def assert_refused(self, fixture, name):
        if not activation.d5_active(fixture.state_dir):
            ready_installation(fixture.root, state_dir=fixture.state_dir)
        before = fixture.tree()
        with mock.patch.dict(os.environ, {"PATH": str(fixture.tools)}), \
                nullcontext(), \
                no_effects() as effects:
            with self.assertRaisesRegex(SO.OutboxError, "D5 active: tracker " + name + " "):
                fixture.call(name)
            self.assertEqual(effects, ([], []))
        self.assertEqual(fixture.tree(), before)

    def test_premature_activation_helpers_have_zero_effects(self):
        for name in ENTRIES:
            with self.subTest(entry=name):
                self.assert_refused(self.fixture(), name)

    def test_ready_activation_helpers_have_zero_effects(self):
        host = registry_fixture.TestD5Registry()
        self.addCleanup(host.doCleanups)
        host.setUp()
        fixture = OutboxFixture(host.d, host.state)
        with nullcontext():
            entry = ready(self, host.repo, "p", host.state)
            for name in ENTRIES:
                with self.subTest(entry=name):
                    self.assert_refused(fixture, name)

    def test_inactive_observable_effects_equal_embedded_base(self):
        baseline = json.loads((FIXTURES / "u6-inactive-effects.json").read_text())
        self.assertEqual(set(baseline["captures"]), set(ENTRIES))
        for name in ENTRIES:
            with self.subTest(entry=name):
                self.assertEqual(self.fixture().capture(name), baseline["captures"][name])

    def test_row75_containing_entries_refuse_before_helpers(self):
        for tool, names in (("swarm", ("cmd_seq_floor", "cmd_outbox", "advance", "main")),
                            ("operator", ("print_tracker_drain",))):
            for name in names:
                with self.subTest(entry=name), tempfile.TemporaryDirectory() as directory:
                    fixture = SurfaceFixture(directory)
                    with mock.patch.object(SO, "backfill_tracker_intents") as backfill, \
                            mock.patch.object(SO, "emit_intent") as emit:
                        before = copy.deepcopy(fixture.state)
                        observed = fixture.observe(tool, name, True)
                        backfill.assert_not_called()
                        emit.assert_not_called()
                    self.assertEqual(fixture.state, before)
                    self.assertEqual(observed["writes"], [])
                    self.assertEqual(observed["launches"], [])
                    self.assertIn("D5 active:", str(observed["outcome"]))

    def test_inactive_capture_excludes_inherited_sinfo(self):
        with tempfile.TemporaryDirectory() as directory:
            inherited = Path(directory) / "inherited"
            inherited.mkdir()
            sinfo = inherited / "sinfo"
            sinfo.write_text("#!/bin/sh\nexit 99\n")
            sinfo.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": str(inherited)}):
                for name in ENTRIES:
                    with self.subTest(entry=name):
                        baseline = json.loads((FIXTURES / "u6-inactive-effects.json").read_text())
                        self.assertEqual(self.fixture().capture(name), baseline["captures"][name])
                # swarm.main used to discover a host sinfo before its capture.
                surface = Path(directory) / "surface"
                surface.mkdir()
                capture = SurfaceFixture(surface).observe("swarm", "main", False)
                self.assertFalse(any("sinfo" in str(c) for c in capture["commands"]))

    def test_manifest_names_all_rejected_helpers_and_observation(self):
        surface = json.loads((FIXTURES / "integration-manifest.json").read_text())["u6_tracker"]
        self.assertEqual(surface["row75_helpers"], list(ENTRIES))
        self.assertEqual(surface["row76"], "merge_unit.print_tracker_audit: observation")
        self.assertEqual(surface["tracker_permissions_installed"], [])
        self.assertEqual(surface["tracker_bootstraps_installed"], [])


class TestAuditObservation(audit_fixture.AuditCase):
    def setUp(self):
        super().setUp()

    def test_active_audit_invokes_only_observations_at_real_transport(self):
        host = registry_fixture.TestD5Registry()
        self.addCleanup(host.doCleanups)
        host.setUp()
        with nullcontext():
            entry = ready(self, host.repo, "p", host.state)
        binding = self.root / ".hanig/linear-binding.json"
        binding.parent.mkdir()
        binding.write_bytes(self.binding.read_bytes())
        args = SimpleNamespace(plan=str(self.plan), state_dir=str(host.state))
        tools = self.root / "tools"
        tools.mkdir()
        commands = []

        def child(command, **kwargs):
            commands.append(command[2])
            # Dispatch the real audit CLI against the in-memory read-only
            # transport. No network, subprocess or production tracker access.
            self.assertEqual(Path(command[1]).name, "linear_sync.py")
            self.assertIn(command[2], ("audit", "section"))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = audit_fixture.LS.main(command[2:])
            return subprocess.CompletedProcess(command, rc, out.getvalue(), err.getvalue())

        before = host.tree()
        tracker_before = copy.deepcopy(self.fake.issues)
        for active in (False, True):
            with self.subTest(active=active), mock.patch.dict(os.environ, {"PATH": str(tools)}), \
                    (nullcontext() if active else missing_installation(entry)), \
                    mock.patch.object(M.subprocess, "run", side_effect=child), \
                    redirect_stdout(io.StringIO()) as output:
                commands.clear()
                self.fake.calls.clear()
                M.print_tracker_audit(args)
                if active:
                    self.assertEqual(commands, [])
                    self.assertIn("Tracker: UNAVAILABLE", output.getvalue())
                    self.assertNotIn("Linear consistent", output.getvalue())
                    self.assertEqual(self.fake.calls, [])
                else:
                    self.assertEqual(commands, ["audit", "section"])
                    self.assertIn("Linear consistent", output.getvalue())
                    self.assertTrue(self.fake.calls)
                self.assertTrue(all(q.lstrip().startswith("query") for q, _ in self.fake.calls))
                self.assertEqual(self.fake.issues, tracker_before)
            self.assertEqual(host.tree(), before)


if __name__ == "__main__":
    unittest.main()
