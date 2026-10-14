"""Dispatch extraction preserves exports and patch delivery."""

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
import swarm_dispatch as SD

DEFINITIONS = (
    "_allocate", "_dep_env", "_isolation_submission", "_record_isolation_facts",
    "_clear_isolation_marker", "_slurm_exit_trap", "_submit", "_bind",
)
CONSTANTS = ("_CONTAINER_BIND_ENV",)


class TestDispatchExtraction(unittest.TestCase):
    def test_identical_reexports(self):
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS + CONSTANTS), namespace)
        for name in DEFINITIONS + CONSTANTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SD, name))
                self.assertIs(namespace[name], getattr(SD, name))
                if name in DEFINITIONS:
                    self.assertEqual(getattr(SD, name).__module__, SD.__name__)
                    self.assertIs(getattr(SD, name).__globals__, SD.__dict__)

    def test_dependency_patch_reaches_reexported_submit(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-env-") as directory:
            unit = {"id": "unit", "kind": "slurm", "command": "true"}
            with mock.patch.object(SD.U, "slurm_record_policy",
                                   return_value=(False, None)), \
                    mock.patch.object(SD.U, "run", return_value=(0, "42", "")), \
                    mock.patch.object(SD, "_dep_env", return_value=[
                        ("SWARM_DEP_INPUT", "/fixture-input")]) as dependencies:
                job, error = S._submit(unit, directory, False, {}, directory)
            self.assertIsNone(error)
            self.assertEqual(job, "42")
            dependencies.assert_called_once_with(unit, {})
            self.assertIn("export SWARM_DEP_INPUT=/fixture-input",
                          Path(directory, "job.sbatch").read_text())

    def test_owner_lookups(self):
        tree = ast.parse(Path(S.__file__).read_text())
        self.assertIn("advance", {n.name for n in tree.body
                                  if isinstance(n, ast.FunctionDef)})
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS + CONSTANTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SD"}
        self.assertEqual(consumers, {"_allocate", "_submit", "_bind"})
        self.assertEqual(bare, [], "facade bypasses owner patches")

    def test_patch_owners(self):
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            imports = {(a.name, a.asname or a.name) for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names}
            facades = {a for m, a in imports if m == "swarm"}
            owners = {a for m, a in imports if m == "swarm_dispatch"}
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
                    if target in owners | {"swarm_dispatch", "S.SD", "self.S.SD"}:
                        owner_patches.add(name)
        self.assertEqual(owner_patches, {
            "_allocate", "_submit", "_bind", "_slurm_exit_trap", "_dep_env"})
        self.assertEqual(offenders, [])

    def test_standalone_offline_import(self):
        tree = ast.parse(Path(SD.__file__).read_text())
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
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_dispatch; "
            "assert not set(sys.modules).intersection(" + repr(blocked) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_probe_ignores_startup_customization(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-startup-") as directory:
            Path(directory, "sitecustomize.py").write_text("import ssl\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": directory}):
                self.test_standalone_offline_import()


if __name__ == "__main__":
    unittest.main()
