"""Plan-rule extraction preserves the API and the owner of patched globals."""

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
import swarm_plan_rules as SPR

DEFINITIONS = (
    "declared_seed", "declared_scope", "_norm_scope", "_scopes_overlap",
    "declared_account", "declared_partition", "_known_partitions",
    "partition_problems", "read_survey", "discover_survey", "_scheduler_facts",
    "surveyed_partition", "limit_state", "_account_names", "declared_memory",
    "memory_flag_problems", "account_problems", "resolve_promote_to",
    "_unquoted_shell_syntax", "_isolation_command_argv", "isolation_problem",
    "_ancestors", "resolve_runtime", "_looks_unresolved", "_runtime_identity",
    "_validate_runtimes", "converge_problem", "_units_with_canary", "plan_digest",
)
CONSTANTS = (
    "_LOOK_IT_UP", "SURVEY_FILE", "LIMIT_SET", "LIMIT_OPEN", "LIMIT_UNKNOWN",
    "MEM_FLAGS", "RESOLUTIONS", "ISOLATION_BACKENDS", "ISOLATION_KEYS",
    "ISOLATION_ROOT_TOKENS", "ISOLATION_SHELL_EXECUTABLES",
    "ISOLATION_MULTICALL_EXECUTABLES", "ISOLATION_LAUNCHER_EXECUTABLES",
    "_NEEDS_RUNTIME", "_PLACEHOLDER_RE", "_PLACEHOLDER_EXACT",
    "_PLACEHOLDER_PREFIX", "CONVERGE_UNIT_KEYS", "COSMETIC_FIELDS",
)


class TestPlanRulesExtraction(unittest.TestCase):
    def test_every_moved_definition_is_reexported_identically(self):
        namespace = {}
        exec("from swarm import " + ", ".join(DEFINITIONS + CONSTANTS), namespace)
        for name in DEFINITIONS + CONSTANTS:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SPR, name))
                self.assertIs(namespace[name], getattr(SPR, name))
                if name in DEFINITIONS:
                    self.assertIs(getattr(S, name).__globals__, SPR.__dict__)

    def test_owner_lookup_patch_reaches_moved_partition_check(self):
        units = [{"id": "work", "sbatch": ["--partition=missing"]}]
        with mock.patch.object(SPR, "_known_partitions", return_value={"present"}) as lookup:
            self.assertEqual(S.partition_problems(units), [("work", "missing")])
        lookup.assert_called_once_with()

    def test_owner_destination_patch_reaches_coordinator_validation(self):
        plan = {"units": [{"id": "work", "kind": "slurm", "command": "true",
                           "runtime": "none", "outputs": ["out"],
                           "promote_to": "/fixture/destination"}]}
        with mock.patch.object(SPR, "resolve_promote_to",
                               return_value=(None, "fixture refusal")) as resolve:
            with self.assertRaisesRegex(S.PlanError, "fixture refusal"):
                S.validate_plan(plan)
        resolve.assert_called_once_with("/fixture/destination")

    def test_owner_runtime_patch_reaches_coordinator_validation(self):
        plan = {"units": [{"id": "work", "kind": "slurm", "command": "true",
                           "runtime": "none", "outputs": ["out"]}]}
        with mock.patch.object(SPR, "_validate_runtimes",
                               side_effect=S.PlanError("fixture runtime refusal")) as validate:
            with self.assertRaisesRegex(S.PlanError, "fixture runtime refusal"):
                S.validate_plan(plan)
        validate.assert_called_once_with(plan, plan["units"])

    def test_owner_syntax_patch_reaches_moved_command_parser(self):
        with mock.patch.object(SPR, "_unquoted_shell_syntax", return_value=";") as syntax:
            argv, problem = S._isolation_command_argv("/bin/true")
        self.assertIsNone(argv)
        self.assertIn("shell syntax ';'", problem)
        syntax.assert_called_once_with("/bin/true")

    def test_coordinator_consumers_read_functions_from_owner(self):
        trees = []
        for module, expected in ((S, "_submit"), (S.SCL, "_capture_code_launch")):
            tree = ast.parse(Path(module.__file__).read_text())
            self.assertIn(expected, {n.name for n in tree.body
                                     if isinstance(n, ast.FunctionDef)})
            trees.append(tree)
        bare = [(n.id, n.lineno) for tree in trees for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in DEFINITIONS]
        consumers = {n.attr for tree in trees for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SPR"}
        self.assertEqual(consumers, {
            "declared_partition", "declared_seed",
            "declared_scope", "resolve_promote_to", "partition_problems",
            "_known_partitions", "memory_flag_problems", "account_problems",
            "_scopes_overlap", "discover_survey", "read_survey",
            "_units_with_canary", "converge_problem", "isolation_problem",
            "_validate_runtimes", "plan_digest", "resolve_runtime",
            "_isolation_command_argv", "_ancestors", "_looks_unresolved",
        })
        self.assertEqual(bare, [], "a cached facade read bypasses owner patches")

    def test_tests_patch_plan_rule_owner(self):
        # Maintenance lint for the suite's patch/object, setattr and assignment
        # spellings, not arbitrary Python alias/dataflow analysis.
        offenders, owner_patches = [], set()
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            aliases = {a.asname or a.name for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names if a.name == "swarm"}
            owners = {a.asname or a.name for n in ast.walk(tree)
                      if isinstance(n, ast.Import) for a in n.names
                      if a.name == "swarm_plan_rules"}
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
                    if target in owners | {"swarm_plan_rules", "S.SPR", "self.S.SPR"}:
                        owner_patches.add(name)
        self.assertTrue({"_known_partitions", "resolve_promote_to",
                         "_validate_runtimes", "_unquoted_shell_syntax"} <= owner_patches)
        self.assertEqual(offenders, [])

    def test_new_module_imports_without_facade_or_network(self):
        tree = ast.parse(Path(SPR.__file__).read_text())
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
        result = subprocess.run([sys.executable, "-S", "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_plan_rules; "
            "assert not set(sys.modules).intersection(" + repr(runtime_forbidden) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
