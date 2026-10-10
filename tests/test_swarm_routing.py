"""The types/routing extraction keeps the public coordinator API live."""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S
import swarm_routing as SR
import swarm_types as ST

ROUTING_FUNCTIONS = (
    "_routing_token", "_routing_model", "load_agent_routing", "_model_family",
    "apply_agent_resolution", "default_thinking_for",
)
ROUTING_DATA = (
    "AGENTS_FILE", "DEFAULT_AGENT_PROVIDER", "DEFAULT_AGENT_THINKING",
    "THINKING_BY_MODEL", "PINNED_AGENT_PROVIDER", "PINNED_THINKING_BY_MODEL",
)
TYPES = (
    "PlanError", "OutboxError", "DONE", "RUNNING", "FAILED", "PREEMPTED",
    "INCOMPLETE", "NEEDS_HUMAN", "NAME", "EXIT_OK", "EXIT_HALTED",
    "EXIT_FAILED_UNIT", "EXIT_USAGE", "EXIT_CONFLICT", "EXIT_SCOPE_OUTSIDE",
    "EXIT_SCOPE_UNCHECKED",
)


class TestRoutingExtraction(unittest.TestCase):
    def test_every_moved_definition_is_reexported_identically(self):
        for module, names in ((SR, ROUTING_FUNCTIONS + ROUTING_DATA), (ST, TYPES)):
            for name in names:
                with self.subTest(name=name):
                    self.assertIs(getattr(S, name), getattr(module, name))
        for name in ROUTING_FUNCTIONS:
            self.assertIs(getattr(S, name).__globals__, SR.__dict__)
        with self.assertRaises(AttributeError):
            getattr(S, "no_such_routing_export")

    def test_wildcard_import_includes_live_routing_and_existing_exports(self):
        from unittest import mock
        for provider in (SR.PINNED_AGENT_PROVIDER, "codex/future-987"):
            with mock.patch.object(SR, "DEFAULT_AGENT_PROVIDER", provider):
                namespace = {}
                exec("from swarm import *", namespace)
                for name in ROUTING_DATA + TYPES + ROUTING_FUNCTIONS:
                    if not name.startswith("_"):
                        with self.subTest(name=name, provider=provider):
                            self.assertIs(namespace[name], getattr(S, name))
                self.assertEqual(namespace["DEFAULT_AGENT_PROVIDER"], provider)
                # Defining __all__ too early must not hide later coordinator API.
                self.assertIs(namespace["cmd_run"], S.cmd_run)
                self.assertIs(namespace["main"], S.main)

    def test_dynamic_exports_follow_resolution_and_restore(self):
        # Exercise the live facade across resolution and the next-plan reset.
        from unittest import mock
        class Family:
            __file__ = str(ROOT / "skills/hanig-review-gate/scripts/model_family.py")
            @staticmethod
            def config_digests(*args):
                return {}
            @staticmethod
            def resolved_agent_default(*args, **kwargs):
                return "codex/future-987", "fixture resolution"
            @staticmethod
            def agent_thinking(*args):
                return "xhigh"
        with mock.patch.object(SR, "_model_family", return_value=Family):
            try:
                self.assertEqual(S.apply_agent_resolution(ROOT / "plan.json"), "codex/future-987")
                self.assertEqual(S.DEFAULT_AGENT_PROVIDER, "codex/future-987")
                self.assertEqual(S.default_thinking_for({}), "xhigh")
            finally:
                with mock.patch.object(SR, "_model_family", return_value=None):
                    S.apply_agent_resolution(ROOT / "plan.json")
        self.assertEqual(S.DEFAULT_AGENT_PROVIDER, S.PINNED_AGENT_PROVIDER)
        self.assertEqual(S.THINKING_BY_MODEL, S.PINNED_THINKING_BY_MODEL)

    def test_new_modules_import_without_the_facade_or_network(self):
        forbidden = {"swarm", "review", "committee", "resolve_models", "linear_api",
                     "linear_sync", "merge_unit", "urllib", "http", "socket", "ssl"}
        for module, expected in ((SR, set(ROUTING_FUNCTIONS)),
                                 (ST, {"PlanError", "OutboxError"})):
            tree = ast.parse(Path(module.__file__).read_text())
            found = {n.name for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            self.assertEqual(found, expected)
            imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            self.assertFalse({str(n).split('.')[0] for n in imports} & forbidden)
        # Python 3.12 pathlib imports urllib.parse for path/URI conversion;
        # the package alone is not network I/O. Block its network client.
        runtime_forbidden = (forbidden - {"urllib"}) | {"urllib.request"}
        proc = subprocess.run([sys.executable, "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import swarm_types, swarm_routing; swarm_routing._model_family(); "
            "assert not set(sys.modules).intersection(" + repr(runtime_forbidden) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_tests_patch_the_routing_owner(self):
        # Direct mock.patch/object and setattr cleanup spellings used by this
        # suite. This is a maintenance lint, not arbitrary Python dataflow.
        moved = set(ROUTING_FUNCTIONS + ROUTING_DATA + TYPES)
        offenders, retargeted = [], []
        for path in sorted((ROOT / "tests").glob("test*.py")):
            tree = ast.parse(path.read_text())
            aliases = {a.asname or a.name for n in ast.walk(tree)
                       if isinstance(n, ast.Import) for a in n.names if a.name == "swarm"}
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                args = n.args
                if (isinstance(n.func, ast.Attribute) and n.func.attr == "addCleanup"
                        and args and isinstance(args[0], ast.Name) and args[0].id == "setattr"):
                    args = args[1:]
                elif not ((isinstance(n.func, ast.Attribute) and n.func.attr == "object")
                          or (isinstance(n.func, ast.Name) and n.func.id == "setattr")):
                    if isinstance(n.func, ast.Attribute) and n.func.attr == "patch" and args:
                        target = getattr(args[0], "value", None)
                        if isinstance(target, str) and target.startswith("swarm.") and target[6:] in moved:
                            offenders.append((path.name, n.lineno, target))
                    continue
                if len(args) < 2 or not isinstance(args[0], ast.Name):
                    continue
                name = getattr(args[1], "value", None)
                if name not in moved:
                    continue
                if args[0].id in aliases:
                    offenders.append((path.name, n.lineno, name))
                if args[0].id == "SR":
                    retargeted.append((path.name, name))
        self.assertIn(("test_model_resolution.py", "AGENTS_FILE"), retargeted)
        self.assertIn(("test_model_resolution.py", "_model_family"), retargeted)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
