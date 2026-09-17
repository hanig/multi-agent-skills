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
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_BEARER_TOKEN_BEDROCK",
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
    def test_other_model_credentials_are_documented_as_coordinator_side(self):
        docs = (
            ROOT / "README.md",
            ROOT / "skills" / "hanig-swarm" / "SKILL.md",
        )
        for path in docs:
            text = path.read_text()
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn(
                    "`models.json` is routing metadata, not a credential grant",
                    text)
                self.assertIn("`OPENAI_API_KEY`", text)
                self.assertIn("`OPENROUTER_API_KEY`", text)
                self.assertIn("coordinator-side", text)

    def test_huggingface_token_is_not_denied_under_either_name(self):
        """HF_TOKEN and HUGGINGFACE_TOKEN are one credential, two spellings,
        and the same library reads both. Denying either fails a gated model
        download -- the regression that made us abandon suffix matching. This
        pins BOTH names, because the list was briefly wrong on one of them."""
        import child_environment as CE
        planted = {"HF_TOKEN": "hf_a", "HUGGINGFACE_TOKEN": "hf_b",
                   "WANDB_API_KEY": "w"}
        original = dict(os.environ)
        os.environ.update(planted)
        try:
            got = CE.child_env()
        finally:
            os.environ.clear()
            os.environ.update(original)
        for name, value in planted.items():
            self.assertEqual(got.get(name), value,
                             "%s must reach the child; units need it" % name)

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
                job, err = S._submit(
                    unit, attempt, False, state=state,
                    state_dir=str(Path(tmp.name) / "state"))
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
                job, err = S._submit(
                    unit, attempt, False, state=state,
                    state_dir=str(tmp / "state"))
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

    def _isolated_unit(self, input_path, backend="apptainer"):
        return {
            "id": "isolated", "kind": "slurm", "runtime": "none",
            "command": "cp /shared/input.tsv result.tsv",
            "inputs": [str(input_path)], "outputs": ["result.tsv"],
            "isolation": {
                "kind": "container", "backend": backend,
                "image": "/images/tool.sif",
                "writable": ["$SWARM_UNIT_DIR"],
                "read_only": [str(input_path)],
            },
        }

    def _write_isolation_receipt(
            self, attempt, isolation_facts=None, expected="DONE",
            isolation_required=False):
        U.write_json(attempt / U.UNIT, {
            "schema_version": 1, "task_id": "isolated",
            "attempt_id": attempt.name, "kind": "slurm",
            "job_id": "12345", "declared_outputs": ["result.tsv"],
        })
        args = mock.Mock(
            unit_dir=str(attempt), launch_facts=None, artifact_basis=None,
            isolation_facts=(json.dumps(isolation_facts)
                             if isolation_facts else None),
            isolation_required=isolation_required,
            result_fd=None, json=False)
        with mock.patch.object(U, "check_unit", return_value="DONE"):
            self.assertEqual(U.cmd_check(args), U.STATES[expected])
        return json.loads((attempt / U.RECEIPT).read_text())

    def test_declared_container_isolation_renders_one_writable_bind_and_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            input_path = tmp / "input.tsv"
            input_path.write_text("pinned\n")
            attempt = tmp / "attempt-1"
            attempt.mkdir()
            unit = self._isolated_unit(input_path)

            self.assertEqual(S.validate_plan({"units": [unit]})["units"], 1)
            state = {"units": {"isolated": {}}}
            marker = attempt / ".swarm-isolation-applied-v1"
            marker.write_text("stale-from-an-earlier-launch\n")
            with mock.patch.object(
                    S.U, "run", return_value=(0, "12345", "")):
                job, err = S._submit(
                    unit, str(attempt), False, state=state,
                    state_dir=str(tmp / "state"))
            self.assertIsNone(err)
            self.assertEqual(job, "12345")
            self.assertFalse(marker.exists(),
                             "submission must clear stale application proof")
            script = (attempt / "job.sbatch").read_text()
            root_bind = f"{attempt.resolve()}:{attempt.resolve()}:rw"
            input_bind = f"{input_path}:{input_path}:ro"
            self.assertEqual(script.count(":rw"), 1, script)
            self.assertIn(shlex.quote(root_bind), script)
            self.assertIn(shlex.quote(input_bind), script)
            self.assertIn("--no-mount bind-paths", script)
            self.assertIn("--writable-tmpfs", script)
            self.assertNotIn("docker run", script)
            self.assertNotIn("/bin/sh -c", script)
            self.assertIn("cp /shared/input.tsv result.tsv", script)
            self.assertIn("isolation_rc=0", script)
            self.assertIn("|| :", script)
            self.assertIn("-u APPTAINER_MOUNT", script)
            self.assertIn("-u SINGULARITY_MOUNT", script)

            facts = S.trusted_isolation_facts(state, unit, str(attempt))
            self.assertIsNotNone(facts)
            rendered_only = self._write_isolation_receipt(
                attempt, facts, expected="INCOMPLETE")
            self.assertIs(
                rendered_only["basis"]["os_enforced_isolation"], False)
            self.assertEqual(rendered_only["state"], "INCOMPLETE")
            missing_facts = self._write_isolation_receipt(
                attempt, expected="INCOMPLETE", isolation_required=True)
            self.assertEqual(missing_facts["state"], "INCOMPLETE")
            Path(facts["application_marker"]).write_text(
                facts["application_token_sha256"] + "\n")
            receipt = self._write_isolation_receipt(attempt, facts)
            self.assertIs(receipt["basis"]["os_enforced_isolation"], True)
            self.assertEqual(
                receipt["basis"]["isolation_profile"][
                    "writable_host_binds"], [str(attempt.resolve())])

            # Re-entering submission for the same attempt retains the pinned
            # random token but removes the old marker before the backend is
            # contacted. A failed second invocation cannot inherit proof from
            # the first one.
            with mock.patch.object(
                    S.U, "run", return_value=(1, "", "backend unavailable")):
                _job, second_err = S._submit(
                    unit, str(attempt), False, state=state,
                    state_dir=str(tmp / "state"))
            self.assertIn("sbatch refused", second_err)
            self.assertFalse(marker.exists())
            self.assertEqual(
                S.trusted_isolation_facts(state, unit, str(attempt))[
                    "application_token_sha256"],
                facts["application_token_sha256"])

            # Mutation 1: a second writable host path is a plan refusal.
            outside = json.loads(json.dumps(unit))
            outside["isolation"]["writable"].append("/tmp/outside")
            with self.assertRaisesRegex(S.PlanError, "NOTHING ELSE"):
                S.validate_plan({"units": [outside]})

            # Mutation 2: without a declaration/applied fact the historical
            # basis remains byte-for-byte explicit about the weaker boundary.
            plain = self._write_isolation_receipt(attempt)
            self.assertIs(plain["basis"]["os_enforced_isolation"], False)
            self.assertEqual(
                plain["basis"]["conclusive_because"],
                "exclusive by coordinator allocation under a trusted-writer "
                "convention")
            self.assertEqual(
                plain["basis"]["note"],
                "not isolated from other processes running as the same Unix "
                "user. OS-enforced isolation would need a container or mount "
                "namespace with this directory as the only writable bind "
                "mount.")

    def test_isolation_backend_is_declared_not_selected_from_path(self):
        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.tsv"
            input_path.write_text("pinned\n")
            unit = self._isolated_unit(input_path, backend="docker")
            with mock.patch.object(S.shutil, "which", return_value="/bin/docker"):
                with self.assertRaisesRegex(S.PlanError, "never selects"):
                    S.validate_plan({"units": [unit]})

            unit = self._isolated_unit(input_path)
            unit["isolation"]["image"] = "--help"
            with self.assertRaisesRegex(S.PlanError, "runtime option"):
                S.validate_plan({"units": [unit]})
            unit["isolation"]["image"] = "tool.sif"
            with self.assertRaisesRegex(S.PlanError, "absolute path"):
                S.validate_plan({"units": [unit]})

            unit = self._isolated_unit(input_path)
            unit["outputs"] = ["./.swarm-isolation-applied-v1"]
            with self.assertRaisesRegex(S.PlanError, "reserved"):
                S.validate_plan({"units": [unit]})

    def test_isolation_dry_run_does_not_clear_marker_or_persist_facts(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            input_path = tmp / "input.tsv"
            input_path.write_text("pinned\n")
            attempt = tmp / "attempt-1"
            attempt.mkdir()
            marker = attempt / ".swarm-isolation-applied-v1"
            marker.write_text("existing\n")
            state = {"units": {"isolated": {}}}
            job, err = S._submit(
                self._isolated_unit(input_path), attempt, True, state=state,
                state_dir=str(tmp / "state"))
            self.assertIsNone(err)
            self.assertTrue(job.startswith("dry-"))
            self.assertEqual(marker.read_text(), "existing\n")
            self.assertNotIn(
                "attempt_isolation_facts", state["units"]["isolated"])
            self.assertFalse((tmp / "state" / "swarm-state.json").exists())

    def test_isolation_wrapper_clears_proof_on_scheduler_reexecution(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            input_path = tmp / "input.tsv"
            input_path.write_text("pinned\n")
            attempt = tmp / "attempt-1"
            attempt.mkdir()
            bindir = tmp / "bin"
            bindir.mkdir()
            backend = bindir / "apptainer"
            backend.write_text("#!/bin/sh\nexit 0\n")
            backend.chmod(0o755)
            rendered, facts, err = S._isolation_submission(
                self._isolated_unit(input_path), attempt)
            self.assertIsNone(err)
            env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
            with mock.patch.dict(os.environ, env):
                rc, _out, _err = U.run(
                    ["sh", "-c", rendered], cwd=str(attempt))
                self.assertEqual(rc, 0)
                marker = Path(facts["application_marker"])
                self.assertEqual(
                    marker.read_text().strip(),
                    facts["application_token_sha256"])
                backend.write_text("#!/bin/sh\nexit 19\n")
                rc, _out, _err = U.run(
                    ["sh", "-c", rendered], cwd=str(attempt))
            self.assertEqual(rc, 19)
            self.assertFalse(marker.exists())

    def test_isolation_read_only_binds_must_match_declared_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.tsv"
            input_path.write_text("pinned\n")
            unit = self._isolated_unit(input_path)
            unit["isolation"]["read_only"] = []
            with self.assertRaisesRegex(S.PlanError, "exactly its declared"):
                S.validate_plan({"units": [unit]})

    def test_isolation_refuses_shell_program_instead_of_assuming_image_shell(self):
        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.tsv"
            input_path.write_text("pinned\n")
            unit = self._isolated_unit(input_path)
            unit["command"] = "tool input.tsv &"
            with self.assertRaisesRegex(S.PlanError, "shell syntax"):
                S.validate_plan({"units": [unit]})
            unit["command"] = "sh -c 'sleep 10 & exit 0'"
            with self.assertRaisesRegex(S.PlanError, "is a shell"):
                S.validate_plan({"units": [unit]})
            unit["command"] = "ash -c 'sleep 10 & exit 0'"
            with self.assertRaisesRegex(S.PlanError, "is a shell"):
                S.validate_plan({"units": [unit]})
            unit["command"] = "busybox ash -c 'sleep 10 & exit 0'"
            with self.assertRaisesRegex(S.PlanError, "selects a shell"):
                S.validate_plan({"units": [unit]})
            unit["command"] = "/usr/bin/env sh -c 'sleep 10 & exit 0'"
            with self.assertRaisesRegex(S.PlanError, "process launcher"):
                S.validate_plan({"units": [unit]})

    def test_isolation_allows_shell_characters_when_they_are_literal_arguments(self):
        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.tsv"
            input_path.write_text("pinned\n")
            unit = self._isolated_unit(input_path)
            unit["command"] = "cp 'input#1.tsv' 'result[1].tsv'"
            self.assertEqual(S.validate_plan({"units": [unit]})["units"], 1)
            unit["command"] = "cp input#1.tsv result~1.tsv"
            self.assertEqual(S.validate_plan({"units": [unit]})["units"], 1)


if __name__ == "__main__":
    unittest.main()
