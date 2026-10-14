"""Code-launch extraction preserves exports and patch delivery."""

import ast
import os
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
import swarm_code_launch as SCL

DEFINITIONS = (
    "_capture_code_launch", "_code_launch_intent_problem", "_paseo_workspace_id",
    "_code_launch_record_payload", "_create_code_worktree", "_register_code_workspace",
    "_write_code_launch_record", "_complete_code_launch", "_write_launch_record",
)
CONSTANTS = ("LAUNCH_RECORD",)


class TestCodeLaunchExtraction(unittest.TestCase):
    def test_identical_reexports(self):
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS + CONSTANTS), namespace)
        for name in DEFINITIONS + CONSTANTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SCL, name))
                self.assertIs(namespace[name], getattr(SCL, name))
                if name in DEFINITIONS:
                    self.assertEqual(getattr(SCL, name).__module__, SCL.__name__)
                    self.assertIs(getattr(SCL, name).__globals__, SCL.__dict__)

    def test_payload_patch_reaches_reexported_writer(self):
        with tempfile.TemporaryDirectory(prefix="launch-writer-") as directory:
            attempt = Path(directory) / "unit" / "attempt"
            facts = {"unit_id": "unit"}
            with mock.patch.object(SCL, "_code_launch_record_payload",
                                   return_value=b"fixture payload\n") as build:
                seal, error = S._write_code_launch_record(attempt, facts)
            self.assertIsNone(error)
            self.assertTrue(seal)
            self.assertEqual(SCL.W.launch_record_path(attempt).read_bytes(),
                             b"fixture payload\n")
            build.assert_called_once_with(facts)

    def test_owner_lookups(self):
        tree = ast.parse(Path(S.__file__).read_text())
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS + CONSTANTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SCL"}
        self.assertEqual(consumers, set(DEFINITIONS) - {
            "_code_launch_record_payload", "_write_code_launch_record"})
        self.assertEqual(bare, [], "facade bypasses owner patches")

    def test_patch_owners(self):
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            imports = {(a.name, a.asname or a.name) for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names}
            facades = {a for m, a in imports if m == "swarm"}
            owners = {a for m, a in imports if m == "swarm_code_launch"}
            for node in ast.walk(tree):
                target, name = None, None
                if isinstance(node, ast.Call):
                    args = node.args
                    method = getattr(node.func, "attr", None)
                    function = getattr(node.func, "id", None)
                    if (method == "addCleanup" and args
                            and getattr(args[0], "id", None) == "setattr"):
                        args = args[1:]
                    if (len(args) >= 2 and isinstance(args[1], ast.Constant)
                            and (method in {"object", "addCleanup"}
                                 or function == "setattr")):
                        target, name = ast.unparse(args[0]), args[1].value
                    elif (args and isinstance(args[0], ast.Constant)
                          and isinstance(args[0].value, str)):
                        target, _, name = args[0].value.rpartition(".")
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    target, name = ast.unparse(node.value), node.attr
                if name in DEFINITIONS + CONSTANTS:
                    if target in facades | {"swarm", "self.S"}:
                        offenders.append((path.name, node.lineno, target, name))
                    if target in owners | {"swarm_code_launch", "S.SCL", "self.S.SCL"}:
                        owner_patches.add(name)
        self.assertEqual(owner_patches, {
            "_register_code_workspace", "_create_code_worktree",
            "_code_launch_record_payload"})
        self.assertEqual(offenders, [])

    def test_standalone_offline_import(self):
        tree = ast.parse(Path(SCL.__file__).read_text())
        found = {n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertEqual(found, set(DEFINITIONS))
        forbidden = {"swarm", "review", "committee", "resolve_models", "linear_api",
                     "linear_sync", "merge_unit", "urllib", "http", "socket", "ssl"}
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        self.assertFalse({str(n).split(".")[0] for n in imports} & forbidden)
        # pathlib on Python 3.9 imports urllib.parse for path quoting;
        # urllib.request is the network client, as in the preflight probe.
        blocked = (forbidden - {"urllib"}) | {"urllib.request"}
        # Exclude interpreter startup hooks so the module graph belongs to
        # this probe; sitecustomize may legitimately preload network clients.
        result = subprocess.run([sys.executable, "-S", "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_code_launch; "
            "assert not set(sys.modules).intersection(" + repr(blocked) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_probe_ignores_startup_customization(self):
        with tempfile.TemporaryDirectory(prefix="launch-startup-") as directory:
            Path(directory, "sitecustomize.py").write_text("import ssl\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": directory}):
                self.test_standalone_offline_import()


if __name__ == "__main__":
    unittest.main()
