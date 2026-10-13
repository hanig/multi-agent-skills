"""Journal extraction retains the facade API and patches its actual owner."""

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
import swarm_journal as SJ

DEFINITIONS = (
    "_heal_jsonl_tail", "_fsync_append", "_RawJournal", "fatal_problems",
    "_fsync_directory",
)


class TestJournalExtraction(unittest.TestCase):
    def test_every_moved_definition_is_reexported_identically(self):
        for name in DEFINITIONS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SJ, name))
                definition = getattr(SJ, name)
                if name == "_RawJournal":
                    definition = definition.__init__
                self.assertIs(definition.__globals__, SJ.__dict__)
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS), namespace)
        for name in DEFINITIONS:
            self.assertIs(namespace[name], getattr(SJ, name))

    def test_owner_healer_patch_reaches_facade_append(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            path.write_bytes(b'{"kept": true}\n{"torn":')
            with mock.patch.object(SJ, "_heal_jsonl_tail", wraps=SJ._heal_jsonl_tail) as heal:
                S._fsync_append(path, {"new": True})
            heal.assert_called_once()
            self.assertEqual(path.read_bytes(), b'{"kept": true}\n{"new": true}\n')

    def test_tests_patch_journal_owner_not_facade(self):
        # A spelling guard over executable targets, not general alias analysis.
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "object" and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in DEFINITIONS):
                    target = ast.unparse(node.args[0])
                    if target in {"S", "swarm"}:
                        offenders.append((path.name, node.lineno, target))
                    if target == "SJ":
                        owner_patches.add(node.args[1].value)
                if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                        and node.attr in DEFINITIONS):
                    target = ast.unparse(node.value)
                    if target in {"S", "swarm"}:
                        offenders.append((path.name, node.lineno, target))
                    if target == "SJ":
                        owner_patches.add(node.attr)
        self.assertTrue({"_heal_jsonl_tail", "_fsync_directory"} <= owner_patches)
        self.assertEqual(offenders, [])

    def test_coordinator_consumers_read_journal_helpers_from_owner(self):
        bare, consumers = [], set()
        for module, expected in ((S, "advance"), (S.SO, "_append_receipt_atomic")):
            tree = ast.parse(Path(module.__file__).read_text())
            self.assertIn(expected, {n.name for n in tree.body
                                     if isinstance(n, ast.FunctionDef)})
            bare.extend((module.__name__, n.id, n.lineno) for n in ast.walk(tree)
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                        and n.id in DEFINITIONS)
            consumers.update(n.attr for n in ast.walk(tree)
                             if isinstance(n, ast.Attribute)
                             and isinstance(n.value, ast.Name) and n.value.id == "SJ")
        self.assertEqual(consumers, set(DEFINITIONS))
        self.assertEqual(bare, [], "a cached facade read bypasses owner patches")

    def test_journal_imports_without_facade_or_network(self):
        tree = ast.parse(Path(SJ.__file__).read_text())
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
        result = subprocess.run([sys.executable, "-S", "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_journal; "
            "assert not set(sys.modules).intersection(" + repr(runtime_forbidden) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
