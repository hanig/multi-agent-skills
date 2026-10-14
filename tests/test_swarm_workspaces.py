"""Workspace extraction preserves exports and patch delivery."""

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
import swarm_workspaces as SW

DEFINITIONS = (
    "_paseo_json", "_paseo_error", "_paseo_workspace_records",
    "_paseo_workspace_for_path", "_paseo_path_ownership_problem",
    "_registered_attempt_workspace", "_recover_code_launch",
    "_report_retained_worktrees", "_report_would_archive_code_worktree",
    "_worktree_cleanup_failed", "_cleanup_codex_home", "_archive_code_worktree",
    "_preserve_and_archive_code_worktree", "reconcile_orphan",
)
CONSTANTS = ("WORKTREE_ARCHIVE_MAX_ATTEMPTS", "WORKTREE_CLEANUP_SUMMARY_PREFIX")


class TestWorkspacesExtraction(unittest.TestCase):
    def test_identical_reexports(self):
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS + CONSTANTS), namespace)
        for name in DEFINITIONS + CONSTANTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SW, name))
                self.assertIs(namespace[name], getattr(SW, name))
                if name in DEFINITIONS:
                    self.assertEqual(getattr(SW, name).__module__, SW.__name__)
                    self.assertIs(getattr(SW, name).__globals__, SW.__dict__)

    def test_registry_patch_reaches_reexported_lookup(self):
        with tempfile.TemporaryDirectory(prefix="workspace-lookup-") as directory:
            records = [{"cwd": directory, "workspaceId": "fixture-workspace"}]
            with mock.patch.object(SW, "_paseo_workspace_records",
                                   return_value=records) as registry:
                workspace = S._paseo_workspace_for_path(directory)
            self.assertEqual(workspace, "fixture-workspace")
            registry.assert_called_once_with()

    def test_owner_lookups(self):
        trees = []
        for module, expected in ((S, "advance"), (S.SD, "_submit")):
            part = ast.parse(Path(module.__file__).read_text())
            self.assertIn(expected, {n.name for n in part.body
                                     if isinstance(n, ast.FunctionDef)})
            trees.extend(part.body)
        tree = ast.Module(body=trees, type_ignores=[])
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS + CONSTANTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SW"}
        self.assertEqual(consumers, {
            "_paseo_json", "_paseo_error", "_paseo_path_ownership_problem",
            "_recover_code_launch", "_report_retained_worktrees",
            "_report_would_archive_code_worktree", "_cleanup_codex_home",
            "_archive_code_worktree", "reconcile_orphan"})
        self.assertEqual(bare, [], "facade bypasses owner patches")

    def test_patch_owners(self):
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            imports = {(a.name, a.asname or a.name) for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names}
            facades = {a for m, a in imports if m == "swarm"}
            owners = {a for m, a in imports if m == "swarm_workspaces"}
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
                    if target in owners | {"swarm_workspaces", "S.SW", "self.S.SW"}:
                        owner_patches.add(name)
        self.assertEqual(owner_patches, {
            "_paseo_workspace_records", "_archive_code_worktree",
            "_preserve_and_archive_code_worktree", "_recover_code_launch",
            "reconcile_orphan"})
        self.assertEqual(offenders, [])

    def test_standalone_offline_import(self):
        tree = ast.parse(Path(SW.__file__).read_text())
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
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_workspaces; "
            "assert not set(sys.modules).intersection(" + repr(blocked) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_probe_ignores_startup_customization(self):
        with tempfile.TemporaryDirectory(prefix="workspace-startup-") as directory:
            Path(directory, "sitecustomize.py").write_text("import ssl\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": directory}):
                self.test_standalone_offline_import()


if __name__ == "__main__":
    unittest.main()
