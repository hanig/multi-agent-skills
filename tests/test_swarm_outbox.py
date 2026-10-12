"""Outbox extraction retains the facade API and patches its actual owner."""

import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.patch_scan import patch_targets

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S
import swarm_outbox as SO

DEFINITIONS = (
    "closing_evidence_for", "outbox_key", "_intent_evidence_digest",
    "_intent_attempt_identity", "_intent_envelope", "normalize_intent",
    "_nonblank_text", "_schema_is", "validate_intent", "validate_observation",
    "require_valid_intent", "_receipt_admissible", "reconcile_observation",
    "_has_bound_merge_evidence", "_intent_key",
)
CONSTANTS = (
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

    def test_tests_patch_outbox_owner_not_facade(self):
        """Classify the enumerated import forms documented by patch_targets."""
        offenders, unresolved, owner_patches = [], [], set()
        paths = sorted((ROOT / "tests").glob("test_*.py"))
        self.assertIn(Path(__file__).name, {p.name for p in paths})
        for path in paths:
            tree = ast.parse(path.read_text())
            for line, target, name, module in patch_targets(tree, EXPORTS):
                if module is None:
                    unresolved.append((path.name, line, target, name))
                elif module == "swarm":
                    offenders.append((path.name, line, target, name))
                elif module == "swarm_outbox":
                    owner_patches.add(name)
        self.assertEqual(unresolved, [], "unresolved patch targets: %r" % unresolved)
        self.assertTrue({"validate_intent", "_intent_key"} <= owner_patches)
        self.assertEqual(offenders, [], "facade patch targets: %r" % offenders)

    def test_coordinator_consumers_read_outbox_from_owner(self):
        tree = ast.parse(Path(S.__file__).read_text())
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in EXPORTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SO"}
        self.assertTrue({"_intent_key", "_intent_envelope", "_has_bound_merge_evidence",
                         "require_valid_intent", "reconcile_observation"} <= consumers)
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
        result = subprocess.run([sys.executable, "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_outbox; "
            "assert not set(sys.modules).intersection(" + repr(runtime_forbidden) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
