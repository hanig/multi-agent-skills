"""ARC-1170: exercise the real test fixtures under hostile ambient Slurm."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

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
