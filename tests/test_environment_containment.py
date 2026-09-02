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


class TestEnvironmentContainment(unittest.TestCase):
    def test_unit_run_does_not_pass_an_ambient_secret(self):
        probe = (
            "import json,os; print(json.dumps({"
            f"'secret': {SECRET_NAME!r} in os.environ, "
            "'path': bool(os.environ.get('PATH'))}))"
        )
        with mock.patch.dict(os.environ, {SECRET_NAME: "live-secret"}):
            rc, out, err = U.run([sys.executable, "-c", probe])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"secret": False, "path": True})

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
        with mock.patch.dict(os.environ, {SECRET_NAME: "live-secret"}):
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
                + " in os.environ, 'path': bool(os.environ.get('PATH'))}, "
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
            }
            with mock.patch.dict(os.environ, env):
                job, err = S._submit(unit, attempt, False, state=state)
            self.assertIsNone(err)
            self.assertEqual(job, "12345")
            got = json.loads(
                (attempt / "sbatch-environment.json").read_text())
            self.assertEqual(got, {"secret": False, "path": True})
            script = (attempt / "job.sbatch").read_text()
            self.assertIn("export SWARM_DEP_UPSTREAM=", script)


if __name__ == "__main__":
    unittest.main()
