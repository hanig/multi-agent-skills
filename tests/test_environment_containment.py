"""E1: coordinator execution context is not coordinator authority."""

import ast
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402
import unit as U  # noqa: E402
import coordinator_paths as CP  # noqa: E402
import child_environment as CE  # noqa: E402


SECRET_NAME = "E1_PLANTED_COORDINATOR_SECRET"
DEP_SHAPED_SECRET = "SWARM_DEP_OPENAI_API_KEY"
UNIT_SHAPED_SECRET = "SWARM_UNIT_OPENAI_API_KEY"

RUNTIME_NAMES = (
    "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES", "SLURM_JOB_ID",
    "MODULEPATH", "MODULESHOME", "LOADEDMODULES", "LMOD_SYSTEM_NAME",
    "SRUN_CPU_BIND", "SALLOC_ACCOUNT", "SLURM_CPU_BIND", "http_proxy",
    "https_proxy", "no_proxy", "NCCL_DEBUG", "OMPI_MCA_btl",
)


def _child_env_call(node, module_aliases):
    if not isinstance(node, ast.Call):
        return False
    return (isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_aliases
            and node.func.attr == "child_env")


def _subprocess_calls(path):
    """Yield subprocess spawn calls and their containing function."""
    tree = ast.parse(path.read_text())
    module_aliases = {n.asname or n.name for n in ast.walk(tree)
                      if isinstance(n, ast.Import) for n in n.names
                      if n.name == "subprocess"}
    child_module_aliases = {n.asname or n.name for n in ast.walk(tree)
                            if isinstance(n, ast.Import) for n in n.names
                            if n.name == "child_environment"}
    direct_names = {n.asname or n.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom)
                    and n.module == "subprocess" for n in n.names
                    if n.name in {"run", "Popen", "call", "check_call",
                                  "check_output"}}
    spawn_names = {"run", "Popen", "call", "check_call", "check_output"}

    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            is_spawn = (isinstance(node.func.value, ast.Name)
                        and node.func.value.id in module_aliases
                        and node.func.attr in spawn_names)
        else:
            is_spawn = (isinstance(node.func, ast.Name)
                        and node.func.id in direct_names)
        if not is_spawn:
            continue
        owner = node
        while owner in parents and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents[owner]
        yield (node, owner if isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef)) else tree,
               child_module_aliases)


def _env_is_from_child_module(call, owner, child_module_aliases):
    keyword = next((kw for kw in call.keywords if kw.arg == "env"), None)
    if keyword is None:
        return False
    if _child_env_call(keyword.value, child_module_aliases):
        return True
    if not isinstance(keyword.value, ast.Name):
        return False
    assignments = []
    for node in ast.walk(owner):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name)
                   and target.id == keyword.value.id for target in targets):
                assignments.append(node.value)
    return (len(assignments) == 1
            and _child_env_call(assignments[0], child_module_aliases))


class TestEnvironmentContainment(unittest.TestCase):
    def test_unit_run_does_not_pass_an_ambient_secret(self):
        probe = (
            "import json,os; print(json.dumps({"
            f"'secret': {SECRET_NAME!r} in os.environ, "
            f"'dep_shaped': {DEP_SHAPED_SECRET!r} in os.environ, "
            f"'unit_shaped': {UNIT_SHAPED_SECRET!r} in os.environ, "
            "'path': bool(os.environ.get('PATH'))}))"
        )
        planted = {SECRET_NAME: "live-secret",
                   DEP_SHAPED_SECRET: "live-secret",
                   UNIT_SHAPED_SECRET: "live-secret"}
        with mock.patch.dict(os.environ, planted):
            rc, out, err = U.run([sys.executable, "-c", probe])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {
            "secret": False, "dep_shaped": False, "unit_shaped": False,
            "path": True})

    def test_every_denied_name_and_pattern_is_stripped(self):
        planted = {name: "credential" for name in CE.DENIED_ENV_NAMES}
        planted.update({f"PLANTED{suffix}": "credential"
                        for suffix in CE.DENIED_ENV_SUFFIXES})
        planted.update({f"{prefix}PLANTED": "credential"
                        for prefix in CE.DENIED_ENV_PREFIXES})
        planted.update({
            "LC_OPENAI_API_KEY": "credential",
            "lowercase_api_key": "credential",
            "RUNTIME_CONTROL": "present",
        })
        with mock.patch.dict(os.environ, planted, clear=True):
            got = CE.child_env()
        self.assertEqual(got, {"RUNTIME_CONTROL": "present"})

    def test_runtime_environment_reaches_the_actual_child(self):
        planted = {name: f"value-{i}" for i, name in enumerate(RUNTIME_NAMES)}
        probe = ("import json, os; print(json.dumps("
                 + repr(list(RUNTIME_NAMES))
                 + " and {n: os.environ.get(n) for n in "
                 + repr(list(RUNTIME_NAMES)) + "}))")
        with mock.patch.dict(os.environ, planted):
            rc, out, err = U.run([sys.executable, "-c", probe])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), planted)

    def test_only_explicit_constructed_swarm_values_are_passed(self):
        planted = {"SWARM_DEP_RESULT": "ambient-dependency",
                   "SWARM_UNIT_CUSTOM": "ambient-unit"}
        with mock.patch.dict(os.environ, planted, clear=True):
            self.assertEqual(CE.child_env(), {})
            got = CE.child_env({"SWARM_DEP_RESULT": "trusted-dependency",
                                "SWARM_UNIT_CUSTOM": "trusted-unit"})
        self.assertEqual(got, {"SWARM_DEP_RESULT": "trusted-dependency",
                               "SWARM_UNIT_CUSTOM": "trusted-unit"})

    def test_credential_shaped_constructed_name_is_still_a_trusted_path(self):
        planted = {"SWARM_DEP_OPENAI_API_KEY": "ambient-credential"}
        with mock.patch.dict(os.environ, planted, clear=True):
            got = CE.child_env({
                "SWARM_DEP_OPENAI_API_KEY": "/trusted/attempt-1"})
        self.assertEqual(got, {
            "SWARM_DEP_OPENAI_API_KEY": "/trusted/attempt-1"})

    def test_spawn_environments_come_from_the_containment_module(self):
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            for call, owner, aliases in _subprocess_calls(path):
                if not _env_is_from_child_module(call, owner, aliases):
                    offenders.append(f"{path.name}:{call.lineno}")
        self.assertEqual(
            offenders, [],
            "Coordinator subprocesses must pass env=child_env(...) or a "
            "single local assigned from child_env(...):\n  "
            + "\n  ".join(offenders))

    def test_path_policy_git_helper_uses_the_same_denylist(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bindir = tmp / "bin"
            bindir.mkdir()
            result = tmp / "git-environment.json"
            fake = bindir / "git"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, os\n"
                f"json.dump({{'secret': {SECRET_NAME!r} in os.environ}}, "
                f"open({str(result)!r}, 'w'))\n"
                "print('/trusted/worktree')\n"
            )
            fake.chmod(0o755)
            env = {
                "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                SECRET_NAME: "live-secret",
            }
            with mock.patch.dict(os.environ, env):
                self.assertEqual(CP._git("/repo", "rev-parse"),
                                 "/trusted/worktree")
            self.assertEqual(json.loads(result.read_text()), {"secret": False})

    def _pipeline_environment(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        attempt = Path(tmp.name) / "attempt-1"
        attempt.mkdir()
        probe = attempt / "probe.py"
        probe.write_text(
            "import json, os\n"
            "json.dump({\n"
            f"  'secret': {SECRET_NAME!r} in os.environ,\n"
            f"  'dep_shaped': {DEP_SHAPED_SECRET!r} in os.environ,\n"
            f"  'unit_shaped': {UNIT_SHAPED_SECRET!r} in os.environ,\n"
            "  'path': bool(os.environ.get('PATH')),\n"
            "  'unit': os.environ.get('SWARM_UNIT_DIR'),\n"
            "  'dep': os.environ.get('SWARM_DEP_UPSTREAM'),\n"
            "}, open('pipeline-environment.json', 'w'))\n"
        )
        upstream = "/trusted/upstream/attempt-7"
        unit = {
            "id": "pipeline", "kind": "pipeline", "needs": ["upstream"],
            "command": f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}",
        }
        state = {"units": {"upstream": {"attempt_dir": upstream}}}
        planted = {SECRET_NAME: "live-secret",
                   DEP_SHAPED_SECRET: "live-secret",
                   UNIT_SHAPED_SECRET: "live-secret"}
        with mock.patch.dict(os.environ, planted):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                job, err = S._submit(unit, attempt, False, state=state)
        self.assertIsNone(err)
        self.assertTrue(str(job).startswith("engine-"))
        result = attempt / "pipeline-environment.json"
        deadline = time.time() + 10
        while not result.exists() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(result.exists(), "detached pipeline probe did not run")
        os.waitpid(int(str(job).split("-", 1)[1]), 0)
        return json.loads(result.read_text()), str(attempt), upstream

    def test_direct_pipeline_does_not_pass_an_ambient_secret(self):
        got, _attempt, _upstream = self._pipeline_environment()
        self.assertFalse(got["secret"])
        self.assertFalse(got["dep_shaped"])
        self.assertFalse(got["unit_shaped"])

    def test_pipeline_keeps_path_unit_root_and_dependency_map(self):
        got, attempt, upstream = self._pipeline_environment()
        self.assertTrue(got["path"])
        self.assertEqual(got["unit"], attempt)
        self.assertEqual(got["dep"], upstream)

    def test_sbatch_uses_the_shared_denylist(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            attempt = tmp / "attempt-1"
            bindir = tmp / "bin"
            attempt.mkdir()
            bindir.mkdir()
            fake = bindir / "sbatch"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, os\n"
                "json.dump({'secret': " + repr(SECRET_NAME)
                + " in os.environ, 'path': bool(os.environ.get('PATH')), "
                "'get_user_env': 'SBATCH_GET_USER_ENV' in os.environ, "
                "'account': os.environ.get('SBATCH_ACCOUNT'), "
                "'partition': os.environ.get('SBATCH_PARTITION')}, "
                "open('sbatch-environment.json', 'w'))\n"
                "print('12345')\n"
            )
            fake.chmod(0o755)
            unit = {
                "id": "scheduled", "kind": "slurm", "needs": ["upstream"],
                "command": "test -n \"$PATH\" && test -n "
                           "\"$SWARM_DEP_UPSTREAM\"",
            }
            state = {"units": {"upstream": {
                "attempt_dir": "/trusted/upstream/attempt-7"}}}
            env = {
                "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                SECRET_NAME: "live-secret",
                "SBATCH_ACCOUNT": "lab-account",
                "SBATCH_PARTITION": "gpu-batch",
                "SBATCH_GET_USER_ENV": "1",
            }
            with mock.patch.dict(os.environ, env):
                job, err = S._submit(unit, attempt, False, state=state)
            self.assertIsNone(err)
            self.assertEqual(job, "12345")
            got = json.loads(
                (attempt / "sbatch-environment.json").read_text())
            self.assertEqual(got, {
                "secret": False, "path": True,
                "get_user_env": False,
                "account": "lab-account", "partition": "gpu-batch"})
            script = (attempt / "job.sbatch").read_text()
            self.assertIn("export SWARM_DEP_UPSTREAM=", script)


if __name__ == "__main__":
    unittest.main()
