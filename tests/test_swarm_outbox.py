"""Outbox extraction retains the facade API and patches its actual owner."""

import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S
import swarm_outbox as SO

DEFINITIONS = (
    "emit_intent", "backfill_tracker_intents", "_parse_receipt_text",
    "_read_receipts_raw", "_receipt_shape_problem", "load_acknowledgments",
    "_receipt_grade", "_append_receipt_atomic", "record_receipt",
    "acknowledgment_status", "read_outbox", "load_outbox_contract",
    "closing_evidence_for", "outbox_key", "_intent_evidence_digest",
    "_intent_attempt_identity", "_intent_envelope", "normalize_intent",
    "_nonblank_text", "_schema_is", "validate_intent", "validate_observation",
    "require_valid_intent", "_receipt_admissible", "reconcile_observation",
    "_has_bound_merge_evidence", "_intent_key",
)
CONSTANTS = (
    "OUTBOX", "RECEIPT_SCHEMA_VERSION", "TRACKER_EVENTS", "RECEIPTS",
    "RECEIPT_CONFIRMED", "RECEIPT_CONFIRMING_SOURCES", "UNACKNOWLEDGED",
    "ATTESTED_UNSPECIFIED", "ATTESTED_CONFIRMED", "ACKNOWLEDGED", "CONFLICT",
    "INTENT_SCHEMA_VERSION", "OBSERVATION_SCHEMA_VERSION",
    "RECONCILIATION_SCHEMA_VERSION", "INTENT_CONNECTOR_CAPABILITY",
    "INTENT_OPERATIONS", "OPERATION_ACCEPTED", "ASYNC_COMPLETED",
    "CONFIRMED_BY_READBACK", "UNKNOWN", "OBSERVATION_OUTCOMES",
    "MUTATION_RESPONSE", "A2A_LIFECYCLE", "RECEIVER_READBACK",
    "RECEIVER_DEDUPLICATION", "CONFIRMING_SOURCES", "OBSERVATION_SOURCES",
    "CLOSING_EVIDENCE",
)
EXPORTS = DEFINITIONS + CONSTANTS + ("_with_intent_envelope",)


class TestOutboxExtraction(unittest.TestCase):
    def test_every_moved_definition_is_reexported_identically(self):
        for name in EXPORTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SO, name))
                if name in DEFINITIONS:
                    self.assertIs(getattr(SO, name).__globals__, SO.__dict__)
        namespace = {}
        exec("from swarm import " + ", ".join(EXPORTS), namespace)
        for name in EXPORTS:
            self.assertIs(namespace[name], getattr(SO, name))
        self.assertIs(SO._with_intent_envelope, SO.normalize_intent)

    def test_owner_validator_patch_reaches_facade_requirement(self):
        with mock.patch.object(SO, "validate_intent", return_value=["fixture refusal"]) as validate:
            with self.assertRaisesRegex(S.OutboxError, "fixture refusal"):
                S.require_valid_intent({"envelope": {}}, ["fixture capability"])
        validate.assert_called_once_with({"envelope": {}}, ["fixture capability"])

    def test_owner_key_patch_reaches_coordinator_emission(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(SO, "_intent_key", return_value="fixture-key") as key:
                self.assertEqual(S.emit_intent(
                    directory, "project", "unit", "SUBMITTED", {}), "fixture-key")
            key.assert_called_once()
            self.assertEqual(S.read_outbox(directory)[0]["key"], "fixture-key")

    def test_owner_receipt_reader_patch_reaches_facade_status(self):
        with mock.patch.object(SO, "load_acknowledgments", return_value=([], [])) as read:
            self.assertEqual(S.acknowledgment_status("fixture-state"), ({}, []))
        read.assert_called_once_with("fixture-state")

    def test_owner_status_patch_reaches_coordinator_command(self):
        import contextlib
        import io
        from types import SimpleNamespace
        args = SimpleNamespace(record_receipt=None, state_dir="fixture-state",
                               json=True, all=True)
        with mock.patch.object(SO, "acknowledgment_status", return_value=({}, [])) as status:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(S._cmd_outbox_inner(args, []), S.EXIT_OK)
        status.assert_called_once_with("fixture-state")

    def test_tests_patch_outbox_owner_not_facade(self):
        # Pin suite spellings, including direct assignment and string patches.
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                target, name = None, None
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "object" and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)):
                    target, name = ast.unparse(node.args[0]), node.args[1].value
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id == "setattr" and len(node.args) >= 2
                      and isinstance(node.args[1], ast.Constant)):
                    target, name = ast.unparse(node.args[0]), node.args[1].value
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    target, name = ast.unparse(node.value), node.attr
                elif (isinstance(node, ast.Call) and node.args
                      and isinstance(node.args[0], ast.Constant)
                      and isinstance(node.args[0].value, str)
                      and node.args[0].value.startswith("swarm.")):
                    target, _, name = node.args[0].value.rpartition(".")
                if name in EXPORTS:
                    if target in {"S", "swarm", "m", "self.S"}:
                        offenders.append((path.name, node.lineno, target, name))
                    if target in {"SO", "S.SO", "m.SO", "self.S.SO"}:
                        owner_patches.add(name)
        self.assertTrue({"validate_intent", "_intent_key", "load_acknowledgments",
                         "acknowledgment_status"} <= owner_patches)
        self.assertEqual(offenders, [])

    def test_coordinator_consumers_read_outbox_from_owner(self):
        tree = ast.parse(Path(S.__file__).read_text())
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in EXPORTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SO"}
        self.assertTrue({"_intent_key", "_has_bound_merge_evidence", "emit_intent",
                         "backfill_tracker_intents", "read_outbox", "record_receipt",
                         "acknowledgment_status", "_receipt_grade"} <= consumers)
        owner_tree = ast.parse(Path(SO.__file__).read_text())
        owner_consumers = {n.id for n in ast.walk(owner_tree)
                           if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        self.assertTrue({"_intent_envelope", "require_valid_intent",
                         "reconcile_observation"} <= owner_consumers)
        self.assertEqual(bare, [], "a cached facade read bypasses owner patches")

    def test_outbox_imports_without_facade_or_network(self):
        tree = ast.parse(Path(SO.__file__).read_text())
        found = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(found, set(DEFINITIONS))
        forbidden = {"swarm", "review", "committee", "resolve_models", "linear_api",
                     "linear_sync", "merge_unit", "urllib", "http", "socket", "ssl"}
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        self.assertFalse({str(n).split(".")[0] for n in imports} & forbidden)
        # pathlib may import urllib.parse; network clients remain forbidden.
        runtime_forbidden = (forbidden - {"urllib"}) | {"urllib.request"}
        result = subprocess.run([sys.executable, "-S", "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_outbox; "
            "assert not set(sys.modules).intersection(" + repr(runtime_forbidden) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
