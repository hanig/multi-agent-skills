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


SECRET_NAME = "OPENAI_API_KEY"
DEP_SHAPED_SECRET = "SWARM_DEP_OPENAI_API_KEY"
UNIT_SHAPED_SECRET = "SWARM_UNIT_OPENAI_API_KEY"
EXPECTED_DENIED_NAMES = {
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "SENTRY_DSN_NXTRAY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HUGGINGFACE_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "SSH_AUTH_SOCK",
    "SBATCH_GET_USER_ENV",
}

RUNTIME_NAMES = (
    "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES", "SLURM_JOB_ID",
    "MODULEPATH", "MODULESHOME", "LOADEDMODULES", "LMOD_SYSTEM_NAME",
    "SRUN_CPU_BIND", "SALLOC_ACCOUNT", "SLURM_CPU_BIND", "http_proxy",
    "https_proxy", "no_proxy", "NCCL_DEBUG", "OMPI_MCA_btl",
)


SPAWN_NAMES = {"run", "Popen", "call", "check_call", "check_output"}

# BEST-EFFORT LINT, NOT A PROOF. This catches ordinary direct subprocess calls
# whose env= expression is not syntactically a direct child_env(...) call, and
# recursively checks today's scripts. Python permits assigned subprocess
# aliases, getattr access, dynamic imports and rebinding CE.child_env; those can
# evade this scan. Adding spellings in pursuit of airtight static provenance
# would repeat the unbounded-pattern mistake this module exists to avoid.


def _child_env_call(node, module_aliases, direct_aliases):
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in direct_aliases
    return (isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_aliases
            and node.func.attr == "child_env")


def _subprocess_calls(path):
    """Yield subprocess spawn calls and containment-module import aliases."""
    tree = ast.parse(path.read_text())
    module_aliases = {n.asname or n.name for n in ast.walk(tree)
                      if isinstance(n, ast.Import) for n in n.names
                      if n.name == "subprocess"}
    child_module_aliases = {n.asname or n.name for n in ast.walk(tree)
                            if isinstance(n, ast.Import) for n in n.names
                            if n.name == "child_environment"}
    child_direct_aliases = {
        n.asname or n.name for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and n.module == "child_environment" for n in n.names
        if n.name == "child_env"}
    rebound = {n.id for n in ast.walk(tree)
               if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    rebound.update(n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)))
    rebound.update(n.arg for n in ast.walk(tree) if isinstance(n, ast.arg))
    child_module_aliases -= rebound
    child_direct_aliases -= rebound
    direct_names = {n.asname or n.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom)
                    and n.module == "subprocess" for n in n.names
                    if n.name in SPAWN_NAMES}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            is_spawn = (isinstance(node.func.value, ast.Name)
                        and node.func.value.id in module_aliases
                        and node.func.attr in SPAWN_NAMES)
        else:
            is_spawn = (isinstance(node.func, ast.Name)
                        and node.func.id in direct_names)
        if not is_spawn:
            continue
        yield node, child_module_aliases, child_direct_aliases


def _env_is_direct_child_env_call(call, module_aliases, direct_aliases):
    keyword = next((kw for kw in call.keywords if kw.arg == "env"), None)
    return (keyword is not None
            and _child_env_call(keyword.value, module_aliases,
                                direct_aliases))


def _spawn_offenders(root):
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for call, module_aliases, direct_aliases in _subprocess_calls(path):
            if not _env_is_direct_child_env_call(
                    call, module_aliases, direct_aliases):
                offenders.append(f"{path.relative_to(root)}:{call.lineno}")
    return offenders


class TestEnvironmentContainment(unittest.TestCase):
    def test_every_exact_denied_name_and_ambient_swarm_name_is_absent(self):
        self.assertEqual(set(CE.DENIED_ENV_NAMES), EXPECTED_DENIED_NAMES)
        probe = (
            "import json,os; print(json.dumps({"
            f"'denied': [n for n in {sorted(EXPECTED_DENIED_NAMES)!r} "
            "if n in os.environ], "
            f"'dep_shaped': {DEP_SHAPED_SECRET!r} in os.environ, "
            f"'unit_shaped': {UNIT_SHAPED_SECRET!r} in os.environ, "
            "'path': bool(os.environ.get('PATH'))}))"
        )
        planted = {name: "live-secret" for name in EXPECTED_DENIED_NAMES}
        planted.update({DEP_SHAPED_SECRET: "live-secret",
                        UNIT_SHAPED_SECRET: "live-secret"})
        with mock.patch.dict(os.environ, planted):
            rc, out, err = U.run([sys.executable, "-c", probe])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {
            "denied": [], "dep_shaped": False, "unit_shaped": False,
            "path": True})

    def test_runtime_environment_reaches_the_actual_child(self):
        planted = {name: f"value-{i}" for i, name in enumerate(RUNTIME_NAMES)}
        planted["http_proxy"] = "http://build-user:build-pass@proxy.corp:8080"
        planted["https_proxy"] = "https://user:pass@proxy.corp:8443"
        probe = ("import json, os; print(json.dumps("
                 + repr(list(RUNTIME_NAMES))
                 + " and {n: os.environ.get(n) for n in "
                 + repr(list(RUNTIME_NAMES)) + "}))")
        with mock.patch.dict(os.environ, planted):
            rc, out, err = U.run([sys.executable, "-c", probe])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), planted)

    def test_path_containing_sk_tool_reaches_child_intact(self):
        planted = "/opt/sk-tool/bin:/usr/bin"
        probe = "import os; print(os.environ.get('PATH', ''))"
        with mock.patch.dict(os.environ, {"PATH": planted}):
            rc, out, err = U.run([sys.executable, "-c", probe])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, planted)

    def test_aws_runtime_configuration_reaches_child_intact(self):
        planted = {
            "AWS_REGION": "us-west-2",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL": "https://s3.internal",
            "AWS_PROFILE": "research",
        }
        names = list(planted)
        probe = ("import json, os; print(json.dumps({n: os.environ.get(n) "
                 "for n in " + repr(names) + "}))")
        with mock.patch.dict(os.environ, planted, clear=True):
            rc, out, err = U.run([sys.executable, "-c", probe])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), planted)

    def test_ml_credentials_and_database_dsn_reach_child_untouched(self):
        planted = {
            "HF_TOKEN": "hf_gated-model-token",
            "WANDB_API_KEY": "sk-wandb-training-key",
            "DATABASE_DSN": "postgres://alice:hunter2@db.internal/app",
        }
        names = list(planted)
        probe = ("import json, os; print(json.dumps({n: os.environ.get(n) "
                 "for n in " + repr(names) + "}))")
        with mock.patch.dict(os.environ, planted, clear=True):
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
        offenders = _spawn_offenders(SCRIPTS)
        self.assertEqual(
            offenders, [],
            "Best-effort lint: ordinary coordinator subprocesses must pass "
            "env=child_env(...) directly. Dynamic Python can evade this "
            "scan; see its declared limit above. Offenders:\n  "
            + "\n  ".join(offenders))

    def test_direct_import_alias_is_recognized(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "launcher.py").write_text(
                "from child_environment import child_env as contained\n"
                "from subprocess import Popen as launch\n"
                "launch(['true'], env=contained())\n")
            self.assertEqual(_spawn_offenders(root), [])

    def test_shadowed_direct_import_alias_is_an_offender(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "launcher.py").write_text(
                "from child_environment import child_env as contained\n"
                "import subprocess\n"
                "def contained():\n"
                "    return {'OPENAI_API_KEY': 'credential'}\n"
                "subprocess.Popen(['true'], env=contained())\n")
            self.assertEqual(_spawn_offenders(root), ["launcher.py:5"])

    def test_mutating_environment_wrapper_is_an_offender(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "launcher.py").write_text(
                "import child_environment as CE\n"
                "import subprocess\n"
                "env = CE.child_env()\n"
                "env['OPENAI_API_KEY'] = 'credential'\n"
                "subprocess.Popen(['true'], env=env)\n")
            self.assertEqual(_spawn_offenders(root), ["launcher.py:5"])

    def test_bare_spawn_in_nested_module_is_an_offender(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            nested = root / "nested"
            nested.mkdir()
            (nested / "launcher.py").write_text(
                "import subprocess\nsubprocess.Popen(['true'])\n")
            self.assertEqual(_spawn_offenders(root), ["nested/launcher.py:2"])

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
