"""ARC-1170: exercise the real test fixtures under hostile ambient Slurm."""
import importlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scheduler_fixture import closed_bin

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    'tests.test_outbox.TestListFieldsMustBeLists',
    'tests.test_plan_shape.TestArrayAndOneWriteRoot',
    'tests.test_plan_shape.TestRoundOneOfThisBatch',
    'tests.test_runtime.TestACanaryMustExerciseWhatItVouchesFor',
    'tests.test_portable_paths.InstalledSnapshot.test_contract_handoff_survey_and_validation_use_copy_not_checkout',
    'tests.test_project.TestTheSurveySaysWhoMayUseAPartition',
)


def hostile_scheduler(directory, calls):
    directory.mkdir()
    body = ('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> ' + shlex.quote(str(calls)) + '\n' + '''
case "$0" in
  */sinfo) case "$*" in *'%P|%a|%l|%D'*) echo 'fixture_other|up|1:00:00|1';; *) echo fixture_other;; esac;;
  */scontrol) case "$*" in *config*) echo 'DefMemPerNode = UNLIMITED';; *) echo 'PartitionName=fixture_other AllowAccounts=ALL DefMemPerNode=UNLIMITED';; esac;;
  */squeue) exit 0;;
  */sbatch) echo 'fixture forbids submission' >&2; exit 97;;
esac
''')
    for name in ('sinfo', 'scontrol', 'squeue', 'sbatch'):
        path = directory / name
        path.write_text(body)
        path.chmod(0o755)
    return str(directory)


class OrdinaryTools(unittest.TestCase):
    def test_module_path_is_restored_by_the_unittest_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            program = (
                "import os, pathlib, sys, unittest\n"
                "before = os.environ.get('PATH')\n"
                "suite = unittest.defaultTestLoader.loadTestsFromName("
                "'tests.test_runtime.TestRuntimeMustBeDeclared')\n"
                "result = unittest.TextTestRunner().run(suite)\n"
                "assert result.wasSuccessful()\n"
                "assert os.environ.get('PATH') == before, 'module PATH leaked'\n"
                "assert not list(pathlib.Path(sys.argv[1]).glob('scheduler-free-*')), 'module fixture retained'\n")
            result = subprocess.run([sys.executable, "-c", program, directory],
                                    cwd=ROOT, env=dict(os.environ, TMPDIR=directory),
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_empty_path_still_resolves_named_system_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"PATH": ""}):
                path = closed_bin(Path(directory) / "closed", tools=("sh",))
            result = subprocess.run(["sh", "-c", "printf fixture-ok"],
                                    env=dict(os.environ, PATH=path),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "fixture-ok")

    def test_relative_path_tool_still_runs_from_another_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "relative-tools"
            tools.mkdir()
            (tools / "sh").symlink_to("/bin/sh")
            previous = Path.cwd()
            try:
                os.chdir(root)
                with mock.patch.dict(os.environ, {"PATH": "relative-tools"}):
                    path = closed_bin(root / "closed", tools=("sh",))
            finally:
                os.chdir(previous)
            result = subprocess.run(["sh", "-c", "printf fixture-ok"],
                                    cwd=previous, env=dict(os.environ, PATH=path),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "fixture-ok")

    def test_paseo_helpers_keep_ordinary_tools_but_drop_host_scheduler(self):
        for module in ("tests.test_continuation", "tests.test_verify"):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls = root / "calls"
                ambient = hostile_scheduler(root / "hostile", calls) + os.pathsep + closed_bin(root / "tools")
                case = unittest.TestCase()
                with mock.patch.dict(os.environ, {"PATH": ambient}):
                    try:
                        importlib.import_module(module)._paseo_stub_on_path(case)
                        result = subprocess.run(
                            ["sh", "-c", "command -v paseo && ! command -v sinfo"],
                            capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("paseo", result.stdout)
                        python = subprocess.run(["python3", "-c", "print('fixture-ok')"],
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(python.returncode, 0, python.stderr)
                        self.assertEqual(python.stdout.strip(), "fixture-ok")
                    finally:
                        case.doCleanups()
                self.assertFalse(calls.exists())


class HostileScheduler(unittest.TestCase):
    def test_validate_and_survey_test_fixtures_ignore_ambient_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / 'calls'
            hostile = hostile_scheduler(root / 'hostile', calls)
            # Replace the actual host PATH, then prepend the simulated host's
            # scheduler to a closed set of ordinary executables.
            ambient = hostile + os.pathsep + closed_bin(root / 'tools')
            env = dict(os.environ, PATH=ambient)
            probe = subprocess.run(['sinfo', '-h', '-o', '%P'], env=env,
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(probe.stdout.strip(), 'fixture_other')
            self.assertTrue(calls.exists())
            calls.unlink()
            # Model scheduler tools in the platform default directories too.
            # That is the separate os.defpath leak in InstalledSnapshot.
            program = ('import os, sys, unittest\n'
                       'os.defpath = os.environ["PATH"]\n'
                       'suite = unittest.defaultTestLoader.loadTestsFromNames(%r)\n'
                       'result = unittest.TextTestRunner(verbosity=2).run(suite)\n'
                       'sys.exit(not result.wasSuccessful())\n') % (CASES,)
            result = subprocess.run([sys.executable, '-c', program], cwd=ROOT,
                                    env=env, capture_output=True, text=True,
                                    timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(calls.exists(), calls.read_text() if calls.exists() else '')


if __name__ == '__main__':
    unittest.main()
