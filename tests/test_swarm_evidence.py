"""Evidence extraction preserves callers and patches the actual owner."""

import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S
import swarm_evidence as SE

DEFINITIONS = (
    "_merge_shape_problem", "_read_merge_raw", "load_merge_receipts",
    "_same_repo", "_verify_shape_problem", "_read_verify_raw",
    "load_verifications", "admit_verification", "_receipt_reason",
    "attested_receipt",
)
CONSTANTS = (
    "MERGE_RECEIPTS", "MERGE_METHODS", "_MERGE_REQUIRED",
    "VERIFY_RECEIPTS", "_VERIFY_REQUIRED",
)


class TestEvidenceExtraction(unittest.TestCase):
    def test_every_moved_definition_is_reexported_identically(self):
        for name in DEFINITIONS + CONSTANTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SE, name))
                if name in DEFINITIONS:
                    self.assertIs(getattr(SE, name).__globals__, SE.__dict__)
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS + CONSTANTS), namespace)
        for name in DEFINITIONS + CONSTANTS:
            self.assertIs(namespace[name], getattr(SE, name))

    def test_owner_reader_patch_reaches_admission_through_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(SE, "load_verifications", return_value=([], [])) as read:
                receipt, refusal = S.admit_verification(
                    directory, "unit", "tests-pass", "a" * 40, "digest", {})
            read.assert_called_once_with(directory)
            self.assertIsNone(receipt)
            self.assertIn("no verification receipt", refusal)

    def test_owner_accessor_patch_reaches_reason_through_facade(self):
        with mock.patch.object(SE, "attested_receipt", return_value=(
                {"notes": ["REASON=fixture-reason"]}, None)) as read:
            self.assertEqual(S._receipt_reason({}, "unit", "attempt"), "fixture-reason")
        read.assert_called_once_with({}, "unit", "attempt")

    def test_tests_patch_evidence_owner_not_facade(self):
        # Pin the spellings used by this suite; this is not general alias analysis.
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                target, name = None, None
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "object" and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)):
                    target, name = ast.unparse(node.args[0]), node.args[1].value
                elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)):
                    target, name = ast.unparse(node.value), node.attr
                elif (isinstance(node, ast.Call) and node.args
                      and isinstance(node.args[0], ast.Constant)
                      and isinstance(node.args[0].value, str)
                      and node.args[0].value.startswith("swarm.")):
                    target, _, name = node.args[0].value.rpartition(".")
                if name in DEFINITIONS:
                    if target in {"S", "swarm"}:
                        offenders.append((path.name, node.lineno, target, name))
                    if target == "SE":
                        owner_patches.add(name)
        self.assertTrue({"_receipt_reason", "load_verifications", "attested_receipt"}
                        <= owner_patches)
        self.assertEqual(offenders, [])

    def test_coordinator_consumers_read_evidence_from_owner(self):
        tree = ast.parse(Path(S.__file__).read_text())
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS + CONSTANTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SE"}
        self.assertEqual(consumers, {
            "_merge_shape_problem", "load_merge_receipts", "_same_repo",
            "_verify_shape_problem", "load_verifications", "admit_verification",
            "_receipt_reason", "attested_receipt", "VERIFY_RECEIPTS",
            "MERGE_RECEIPTS", "MERGE_METHODS",
        })
        self.assertEqual(bare, [], "a cached facade read bypasses owner patches")

    def test_evidence_imports_without_facade_or_network(self):
        tree = ast.parse(Path(SE.__file__).read_text())
        found = {n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
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
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_evidence; "
            "assert not set(sys.modules).intersection(" + repr(runtime_forbidden) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
