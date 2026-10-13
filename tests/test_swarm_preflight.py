"""Preflight extraction keeps public aliases and patch delivery intact."""

import ast
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S
import swarm_preflight as SP

DEFINITIONS = (
    "_git", "_unattended_mode_example", "_workspace_policy",
    "_requires_clean_workspace", "_execution_workspace", "PreflightRefusal",
    "_plan_workspace", "_dirty_refusal", "_repeat_launch_preflight",
    "_inside_dir", "_output_destinations", "_claim_dir", "_claim_liveness",
    "_claim_refusal", "_claim_record", "_own_claim", "_take_output_claims",
    "_drop_claims", "_release_output_claims", "_note_claimed_attempt",
)
CONSTANTS = (
    "OUTPUT_CLAIMS_DIRNAME", "CLAIM_FILE", "CLAIM_LIVE", "CLAIM_FREE",
    "CLAIM_UNKNOWN", "DRY_PREFIX",
)


class TestPreflightExtraction(unittest.TestCase):
    def test_every_moved_definition_is_reexported_identically(self):
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS + CONSTANTS), namespace)
        for name in DEFINITIONS + CONSTANTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SP, name))
                self.assertIs(namespace[name], getattr(SP, name))
                if name in DEFINITIONS:
                    self.assertEqual(getattr(SP, name).__module__, SP.__name__)
                    if name != "PreflightRefusal":
                        self.assertIs(getattr(SP, name).__globals__, SP.__dict__)

    def test_owner_workspace_patch_reaches_moved_preflight(self):
        unit = {"id": "work"}
        with mock.patch.object(SP, "_plan_workspace",
                               return_value=(None, "fixture refusal")) as resolve:
            self.assertEqual(S._repeat_launch_preflight(unit),
                             ("fixture refusal", None))
        resolve.assert_called_once_with(unit)

    def test_owner_git_patch_reaches_coordinator_stash_check(self):
        with mock.patch.object(SP, "_git", return_value=(1, "", "fixture failure")) as git:
            refusal = S._stash_preflight("work", "/fixture/repo")
        self.assertIn("cannot be shown to be empty", refusal)
        git.assert_called_once_with("/fixture/repo", "stash", "list")

    def test_coordinator_consumers_read_functions_from_owner(self):
        tree = ast.parse(Path(S.__file__).read_text())
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS + CONSTANTS]
        consumers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SP"}
        self.assertEqual(consumers, {
            "DRY_PREFIX", "PreflightRefusal", "_dirty_refusal",
            "_execution_workspace", "_git", "_inside_dir",
            "_note_claimed_attempt", "_plan_workspace", "_release_output_claims",
            "_repeat_launch_preflight", "_requires_clean_workspace",
            "_take_output_claims", "_unattended_mode_example",
        })
        self.assertEqual(bare, [], "a cached facade read bypasses owner patches")

    def test_tests_patch_preflight_owner(self):
        # Maintenance lint for the suite's patch/object, setattr and assignment
        # spellings, not arbitrary Python alias/dataflow analysis.
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            aliases = {a.asname or a.name for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names if a.name == "swarm"}
            owners = {a.asname or a.name for n in ast.walk(tree)
                      if isinstance(n, ast.Import) for a in n.names
                      if a.name == "swarm_preflight"}
            for node in ast.walk(tree):
                target, name = None, None
                if isinstance(node, ast.Call):
                    args = node.args
                    if (isinstance(node.func, ast.Attribute) and node.func.attr == "addCleanup"
                            and args and isinstance(args[0], ast.Name)
                            and args[0].id == "setattr"):
                        args = args[1:]
                    if (len(args) >= 2 and isinstance(args[1], ast.Constant)
                            and ((isinstance(node.func, ast.Attribute)
                                  and node.func.attr in {"object", "addCleanup"})
                                 or (isinstance(node.func, ast.Name)
                                     and node.func.id == "setattr"))):
                        target, name = ast.unparse(args[0]), args[1].value
                    elif args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                        target, _, name = args[0].value.rpartition(".")
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    target, name = ast.unparse(node.value), node.attr
                if name in DEFINITIONS + CONSTANTS:
                    if target in aliases | {"swarm", "self.S"}:
                        offenders.append((path.name, node.lineno, target, name))
                    if target in owners | {"swarm_preflight", "S.SP", "self.S.SP"}:
                        owner_patches.add(name)
        self.assertTrue({"_git", "_plan_workspace", "_release_output_claims"} <= owner_patches)
        self.assertEqual(offenders, [])

    def test_new_module_imports_without_facade_or_network(self):
        tree = ast.parse(Path(SP.__file__).read_text())
        found = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertEqual(found, set(DEFINITIONS))
        forbidden = {"swarm", "review", "committee", "resolve_models", "linear_api",
                     "linear_sync", "merge_unit", "urllib", "http", "socket", "ssl"}
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        self.assertFalse({str(n).split(".")[0] for n in imports} & forbidden)
        # pathlib may import urllib.parse; block its network client at runtime.
        runtime_forbidden = (forbidden - {"urllib"}) | {"urllib.request"}
        result = subprocess.run([sys.executable, "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_preflight; "
            "assert not set(sys.modules).intersection(" + repr(runtime_forbidden) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
