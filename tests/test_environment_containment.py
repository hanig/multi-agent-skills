"""E1: coordinator execution context is not coordinator authority."""

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


SECRET_NAME = "E1_PLANTED_COORDINATOR_SECRET"
DEP_SHAPED_SECRET = "SWARM_DEP_OPENAI_API_KEY"
UNIT_SHAPED_SECRET = "SWARM_UNIT_OPENAI_API_KEY"

EXPECTED_SLURM_INPUT_ENV_NAMES = set("""
SBATCH_ACCOUNT SBATCH_ACCTG_FREQ SBATCH_ARRAY_INX SBATCH_BATCH SBATCH_CLUSTERS
SBATCH_CONSTRAINT SBATCH_CONTAINER SBATCH_CONTAINER_ID SBATCH_CONTAINER_TYPE
SBATCH_CORE_SPEC SBATCH_CPUS_PER_GPU SBATCH_DEBUG SBATCH_DELAY_BOOT
SBATCH_DISTRIBUTION SBATCH_ERROR SBATCH_EXCLUSIVE SBATCH_EXPORT
SBATCH_GET_USER_ENV SBATCH_GPU_BIND SBATCH_GPU_FREQ SBATCH_GPUS
SBATCH_GPUS_PER_NODE SBATCH_GPUS_PER_TASK SBATCH_GRES SBATCH_GRES_FLAGS
SBATCH_HINT SBATCH_IGNORE_PBS SBATCH_INPUT SBATCH_JOB_NAME SBATCH_MEM_BIND
SBATCH_MEM_PER_CPU SBATCH_MEM_PER_GPU SBATCH_MEM_PER_NODE SBATCH_NETWORK
SBATCH_NO_KILL SBATCH_NO_REQUEUE SBATCH_OPEN_MODE SBATCH_OUTPUT
SBATCH_OVERCOMMIT SBATCH_PARTITION SBATCH_POWER SBATCH_PROFILE SBATCH_QOS
SBATCH_REQ_SWITCH SBATCH_REQUEUE SBATCH_RESERVATION SBATCH_SEGMENT_SIZE
SBATCH_SIGNAL SBATCH_SPREAD_JOB SBATCH_THREAD_SPEC SBATCH_THREADS_PER_CORE
SBATCH_TIMELIMIT SBATCH_TRES_BIND SBATCH_TRES_PER_TASK SBATCH_USE_MIN_NODES
SBATCH_WAIT SBATCH_WAIT_ALL_NODES SBATCH_WAIT4SWITCH SBATCH_WCKEY
SLURM_CLUSTERS SLURM_CONF SLURM_DEBUG_FLAGS SLURM_EXIT_ERROR SLURM_HINT
SLURM_STEP_KILLED_MSG_NODE_ID SLURM_UMASK SLURM_CLUSTER_NAME
""".split())


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

    def test_the_slurm_ambient_allowlist_is_an_exact_reviewable_set(self):
        import child_environment as CE
        self.assertEqual(set(CE.SLURM_INPUT_ENV_NAMES),
                         EXPECTED_SLURM_INPUT_ENV_NAMES)

    def test_path_policy_git_helper_uses_the_same_allowlist(self):
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

    def test_sbatch_receives_only_the_allowlisted_submission_environment(self):
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
            }
            with mock.patch.dict(os.environ, env):
                job, err = S._submit(unit, attempt, False, state=state)
            self.assertIsNone(err)
            self.assertEqual(job, "12345")
            got = json.loads(
                (attempt / "sbatch-environment.json").read_text())
            self.assertEqual(got, {
                "secret": False, "path": True,
                "account": "lab-account", "partition": "gpu-batch"})
            script = (attempt / "job.sbatch").read_text()
            self.assertIn("export SWARM_DEP_UPSTREAM=", script)


if __name__ == "__main__":
    unittest.main()
