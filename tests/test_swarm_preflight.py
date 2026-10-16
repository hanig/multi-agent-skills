"""Preflight extraction contracts."""

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

DEFS = (
    "_git", "_unattended_mode_example", "_workspace_policy",
    "_requires_clean_workspace", "_execution_workspace", "PreflightRefusal",
    "_plan_workspace", "_dirty_refusal", "_repeat_launch_preflight",
    "_inside_dir", "_output_destinations", "_claim_dir", "_claim_liveness",
    "_claim_refusal", "_claim_record", "_own_claim", "_take_output_claims",
    "_drop_claims", "_release_output_claims", "_note_claimed_attempt",
    "_code_worktree_names",
    "_git_worktrees_on_branch",
    "_stash_refusal",
    "_stash_preflight",
    "_repeat_stash_preflight",
    "_git_push_destination",
    "_exact_remote_ref_head",
    "_dispatch_base_refusal",
    "_resolve_dispatch_target",
    "_dispatch_source_identity",
    "_dispatch_target_for_advance",
    "_installed_skill_source_version",
    "_installed_skill_snapshot",
    "_warn_installed_skill_drift",
    "_seed_reachability_problem",
)
CONSTS = (
    "OUTPUT_CLAIMS_DIRNAME", "CLAIM_FILE", "CLAIM_LIVE", "CLAIM_FREE",
    "CLAIM_UNKNOWN", "DRY_PREFIX",
)


class TestPreflightExtraction(unittest.TestCase):
    def test_identical_reexports(self):
        ns = {}
        exec("from swarm import " + ", ".join(DEFS + CONSTS), ns)
        for name in DEFS + CONSTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SP, name))
                self.assertIs(ns[name], getattr(SP, name))
                if name in DEFS:
                    self.assertEqual(getattr(SP, name).__module__, SP.__name__)
                    if name != "PreflightRefusal":
                        self.assertIs(getattr(SP, name).__globals__, SP.__dict__)

    def test_workspace_patch_delivery(self):
        unit = {"id": "work"}
        with mock.patch.object(SP, "_plan_workspace",
                               return_value=(None, "fixture refusal")) as resolve:
            self.assertEqual(S._repeat_launch_preflight(unit),
                             ("fixture refusal", None))
        resolve.assert_called_once_with(unit)

    def test_git_patch_delivery(self):
        with mock.patch.object(SP, "_git", return_value=(1, "", "fixture failure")) as git:
            refusal = S._stash_preflight("work", "/repo")
        self.assertIn("cannot be shown to be empty", refusal)
        git.assert_called_once_with("/repo", "stash", "list")

    def test_owner_lookups(self):
        trees = []
        for module, expected in ((S, "advance"), (S.SD, "_allocate"),
                                 (S.SCL, "_capture_code_launch")):
            tree = ast.parse(Path(module.__file__).read_text())
            self.assertIn(expected, {n.name for n in tree.body
                                     if isinstance(n, ast.FunctionDef)})
            trees.append(tree)
        bare = [(n.id, n.lineno) for tree in trees for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFS + CONSTS]
        consumers = {n.attr for tree in trees for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SP"}
        self.assertEqual(consumers, {
            "DRY_PREFIX", "CLAIM_FREE", "_claim_liveness",
            "PreflightRefusal",
            "_code_worktree_names",
            "_dirty_refusal",
            "_dispatch_base_refusal",
            "_dispatch_source_identity",
            "_dispatch_target_for_advance",
            "_execution_workspace",
            "_git",
            "_git_push_destination",
            "_git_worktrees_on_branch",
            "_inside_dir",
            "_installed_skill_snapshot",
            "_note_claimed_attempt",
            "_plan_workspace",
            "_release_output_claims",
            "_repeat_launch_preflight",
            "_repeat_stash_preflight",
            "_requires_clean_workspace",
            "_resolve_dispatch_target",
            "_seed_reachability_problem",
            "_stash_preflight",
            "_take_output_claims",
            "_unattended_mode_example",
            "_warn_installed_skill_drift",
        })
        self.assertEqual(bare, [], "facade bypasses patches")

    def test_patch_owners(self):
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            imports = {(a.name, a.asname or a.name) for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names}
            aliases = {a for m, a in imports if m == "swarm"}
            owners = {a for m, a in imports if m == "swarm_preflight"}
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
                            and (method in {"object", "addCleanup"} or function == "setattr")):
                        target, name = ast.unparse(args[0]), args[1].value
                    elif args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                        target, _, name = args[0].value.rpartition(".")
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    target, name = ast.unparse(node.value), node.attr
                if name in DEFS + CONSTS:
                    if target in aliases | {"swarm", "self.S"}:
                        offenders.append((path.name, node.lineno, target, name))
                    if target in owners | {"swarm_preflight", "S.SP", "self.S.SP"}:
                        owner_patches.add(name)
        self.assertTrue({"_git", "_plan_workspace", "_release_output_claims",
                         "_resolve_dispatch_target", "_installed_skill_snapshot",
                         "_stash_preflight", "_git_push_destination"} <= owner_patches)
        self.assertEqual(offenders, [])

    def test_standalone_offline_import(self):
        tree = ast.parse(Path(SP.__file__).read_text())
        found = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertEqual(found, set(DEFS))
        forbidden = {"swarm", "review", "committee", "resolve_models", "linear_api",
                     "linear_sync", "merge_unit", "urllib", "http", "socket", "ssl"}
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        self.assertFalse({str(n).split(".")[0] for n in imports} & forbidden)
        blocked = (forbidden - {"urllib"}) | {"urllib.request"}
        result = subprocess.run([sys.executable, "-S", "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_preflight; "
            "assert not set(sys.modules).intersection(" + repr(blocked) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
