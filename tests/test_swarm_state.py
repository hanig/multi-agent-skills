"""State extraction keeps coordinator callers and the owner in agreement."""

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
import swarm_state as SS

DEFINITIONS = (
    "acquire_lease", "_holds_the_path", "renew_lease", "release_lease",
    "_StateSnapshot", "_read_state_epoch", "load_state",
    "save_state", "_unit_state", "_set_unit_state", "_normalise_state_clock",
    "_deadline_host_evaluation", "_deadline_exceeded", "_mark_deadline_exceeded",
    "_clear_deadline_breach", "_deadline_attempt_is_unresolved",
    "_occupies_live_resources", "trusted_launch_facts", "trusted_launch_host_anchor",
)
DATA = (
    "STATE_FILE", "STATE_EPOCH_FILE", "LEASE", "LOCK",
    "_LOCK_FDS", "_LOCK_EPOCHS", "_LOCK_OWNER_PID", "LIVE_STATES",
)


class TestStateExtraction(unittest.TestCase):
    def test_every_moved_definition_is_reexported_identically(self):
        for name in DEFINITIONS + DATA:
            with self.subTest(name=name):
                self.assertIs(getattr(S, name), getattr(SS, name))
        for name in DEFINITIONS:
            if name != "_StateSnapshot":
                self.assertIs(getattr(S, name).__globals__, SS.__dict__)
        self.assertIs(S._StateSnapshot.copy.__globals__, SS.__dict__)
        namespace = {}
        exec("from swarm import *", namespace)
        for name in DEFINITIONS + DATA:
            if not name.startswith("_"):
                self.assertIs(namespace[name], getattr(SS, name))
        self.assertIs(namespace["cmd_run"], S.cmd_run)

    def test_facade_and_owner_share_real_lease_snapshot_and_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            self.addCleanup(SS._LOCK_EPOCHS.pop, str(Path(directory).resolve()), None)
            try:
                self.assertEqual(S.acquire_lease(directory), (True, None))
                self.assertEqual(S._LOCK_OWNER_PID, os.getpid())
                self.assertIs(S._LOCK_OWNER_PID, SS._LOCK_OWNER_PID)
                self.assertTrue(SS.renew_lease(directory))
                state = S.load_state(directory)
                self.assertIsInstance(state, SS._StateSnapshot)
                state["preserved"] = "state from facade"
                S.save_state(directory, state)
                SS.release_lease(directory)
                self.assertFalse(S.renew_lease(directory))
                self.assertEqual(SS.acquire_lease(directory), (True, None))
                with self.assertRaisesRegex(SystemExit, "snapshot belongs"):
                    S.save_state(directory, state)
                current = SS.load_state(directory)
                self.assertEqual(current["preserved"], "state from facade")
                S.save_state(directory, current)
                self.assertEqual(S._read_state_epoch(directory), (2, None))
            finally:
                S.release_lease(directory)

    def test_owner_patch_reaches_moved_snapshot_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(SS, "_read_state_epoch", return_value=(19, None)) as read:
                state = S.load_state(directory)
            read.assert_called_once_with(directory)
            self.assertEqual(state._snapshot_epoch,
                             (str(Path(directory).resolve()), os.getpid(), 19))

    def test_tests_patch_state_owner_not_facade(self):
        # Scan executable patch targets and manual assignment targets. This is
        # a spelling guard, not a claim to resolve arbitrary Python aliases.
        offenders, owner_patches = [], set()
        facade_names = {"S", "MU.S", "self.first"}
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "object" and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in DEFINITIONS + DATA):
                    target = ast.unparse(node.args[0])
                    if target in facade_names:
                        offenders.append((path.name, node.lineno, target, node.args[1].value))
                    if target == "SS" or target.endswith(".SS"):
                        owner_patches.add(node.args[1].value)
                if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                        and node.attr in DEFINITIONS + DATA
                        and ast.unparse(node.value) in facade_names):
                    offenders.append((path.name, node.lineno, ast.unparse(node)))
        self.assertTrue({"load_state", "renew_lease", "acquire_lease",
                         "release_lease", "_read_state_epoch", "save_state"} <= owner_patches)
        self.assertEqual(offenders, [])

    def test_coordinator_consumers_read_state_helpers_from_owner(self):
        trees = []
        for module, expected in ((S, "advance"), (S.SD, "_submit")):
            part = ast.parse(Path(module.__file__).read_text())
            self.assertIn(expected, {n.name for n in part.body
                                     if isinstance(n, ast.FunctionDef)})
            trees.extend(part.body)
        tree = ast.Module(body=trees, type_ignores=[])
        moved = set(DEFINITIONS) - {
            "_holds_the_path", "_StateSnapshot", "_read_state_epoch",
        }
        bare = [(n.id, n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and n.id in moved]
        consumers = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "SS"}
        self.assertTrue({"save_state", "_unit_state", "_set_unit_state",
                         "_normalise_state_clock", "_deadline_host_evaluation",
                         "_deadline_exceeded", "_mark_deadline_exceeded",
                         "_clear_deadline_breach", "_occupies_live_resources",
                         "trusted_launch_facts", "trusted_launch_host_anchor"}
                        <= consumers)
        self.assertEqual(bare, [], "a cached facade read bypasses owner patches")

    def test_state_imports_without_facade_or_network(self):
        tree = ast.parse(Path(SS.__file__).read_text())
        found = {n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertEqual(found, set(DEFINITIONS))
        forbidden = {"swarm", "review", "committee", "resolve_models", "linear_api",
                     "linear_sync", "merge_unit", "urllib", "http", "socket", "ssl"}
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        self.assertFalse({str(n).split(".")[0] for n in imports} & forbidden)
        # pathlib can load urllib.parse on newer Python; the network client
        # remains forbidden throughout the owner's transitive import graph.
        runtime_forbidden = (forbidden - {"urllib"}) | {"urllib.request"}
        result = subprocess.run([sys.executable, "-S", "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import swarm_state; "
            "assert not set(sys.modules).intersection(" + repr(runtime_forbidden) + ")"
        ), str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
