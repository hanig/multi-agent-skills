"""ARC-1170: exercise the real test fixtures under hostile ambient Slurm."""
import importlib
import json
import os
from pathlib import Path
import pwd
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
    'tests.test_arc692_state_epoch.TestStateEpoch.test_sequential_dry_run_commands_each_use_their_own_epoch',
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
                "keys = ('PATH', 'XDG_STATE_HOME')\n"
                "before = {key: os.environ.get(key) for key in keys}\n"
                "suite = unittest.defaultTestLoader.loadTestsFromName("
                "'tests.test_runtime.TestRuntimeMustBeDeclared')\n"
                "result = unittest.TextTestRunner().run(suite)\n"
                "assert result.wasSuccessful()\n"
                "assert {key: os.environ.get(key) for key in keys} == before, 'module environment leaked'\n"
                "assert not list(pathlib.Path(sys.argv[1]).glob('scheduler-free-*')), 'module fixture retained'\n")
            result = subprocess.run([sys.executable, "-c", program, directory],
                                    cwd=ROOT, env=dict(os.environ, TMPDIR=directory),
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dispatch_and_report_fixtures_leave_real_state_home_unchanged(self):
        # Use the real unittest module lifecycle and the existing CLI fixture.
        # Omitting --root exercises default allocation instead of masking it
        # with an explicit temporary run root. Report collection also reaches
        # coordinator_paths through legacy migration, even though it reads.
        program = '''
import json, os, pathlib, unittest
from tests import test_arc692_state_epoch as fixture
from tests import test_report
import coordinator_paths as paths
before = dict(os.environ)
def observe_project(project):
    state, runs = paths.default_paths(cwd=project)
    print('STATE_PROJECT ' + json.dumps(state.parent.name), flush=True)
collect = test_report.R.collect
def observed_collect(project):
    observe_project(project)
    return collect(project)
test_report.R.collect = observed_collect
class DefaultRootDispatch(fixture.TestStateEpoch):
    __module__ = fixture.__name__
    def runTest(self):
        observe_project(self.root)
        result = self.cli('run', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.read()
        self.assertEqual(state['units']['u']['state'], 'SUBMITTED')
        attempt = pathlib.Path(state['units']['u']['attempt_dir'])
        self.assertTrue((attempt / 'unit.json').is_file())
        attempt.relative_to(pathlib.Path(os.environ['XDG_STATE_HOME']).resolve())
suite = unittest.TestSuite([DefaultRootDispatch(),
    unittest.defaultTestLoader.loadTestsFromName(
        'tests.test_report.TestBuiltFromEvidence.test_digests_come_from_the_receipt')])
result = unittest.TextTestRunner(verbosity=2).run(suite)
assert dict(os.environ) == before, 'module state fixture leaked its environment'
raise SystemExit(not result.wasSuccessful())
'''
        # The caller's state can contain stale or concurrently created names.
        # Give this child its own empty homes so every observed project is a
        # fixture leak, without inspecting or assuming anything about the caller.
        with tempfile.TemporaryDirectory(prefix="state-guard-") as directory:
            root = Path(directory)
            home, state, temporary = (root / name for name in ("home", "state", "tmp"))
            for path in (home, state, temporary):
                path.mkdir()
            env = dict(os.environ, HOME=str(home), XDG_STATE_HOME=str(state),
                       TMPDIR=str(temporary))
            result = subprocess.run([sys.executable, "-c", program], cwd=ROOT,
                                    env=env, capture_output=True, text=True, timeout=60)
            names = {json.loads(line[len("STATE_PROJECT "):])
                     for line in result.stdout.splitlines()
                     if line.startswith("STATE_PROJECT ")}
            self.assertEqual(len(names), 2, result.stdout + result.stderr)
            # Retain the exact fixture-project checks, and reject every other
            # project too: no projects are allowed in these owned fallback homes.
            for base in (state, home / ".local" / "state",
                         temporary / "hanig-swarm-state"):
                projects = base / "hanig-swarm" / "projects"
                watched = {projects / name for name in names}
                self.assertFalse(any(path.exists() for path in watched),
                                 "test fixtures created real state-home projects")
                found = set(projects.iterdir()) if projects.exists() else set()
                self.assertEqual(found, set(),
                                 "test fixtures created unexpected state-home projects")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_state_guard_accepts_a_uid_without_a_passwd_record(self):
        with mock.patch.object(pwd, "getpwuid", side_effect=KeyError("no uid")):
            self.test_dispatch_and_report_fixtures_leave_real_state_home_unchanged()

    def test_state_guard_ignores_stale_and_concurrent_fixture_names_but_catches_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            caller_home = root / "caller"
            caller_state = caller_home / ".local" / "state"
            fixture_tmp = root / "fixtures"
            fixture_tmp.mkdir()
            caller_state.mkdir(parents=True)
            real_run, real_iterdir = subprocess.run, Path.iterdir
            observed = []
            allowed_listings = set()
            mode = ["isolated"]

            def run_with_reused_paths(command, **kwargs):
                # Real exclusive mkdir/cleanup and project-name calculation,
                # with a repeatable sequence of legal temporary path candidates.
                # Override only the child's temporary allocator, not its HOME.
                prefix = ("import tempfile\n"
                          "tempfile.tempdir = %r\n"
                          "tempfile._name_sequence = iter('reused%%08d' %% i for i in range(1000))\n"
                          % str(fixture_tmp))
                env = kwargs["env"]
                self.assertNotEqual(Path(env["HOME"]), caller_home)
                self.assertNotEqual(Path(env["XDG_STATE_HOME"]), caller_state)
                allowed_listings.update(
                    base / "hanig-swarm" / "projects" for base in (
                        Path(env["XDG_STATE_HOME"]),
                        Path(env["HOME"]) / ".local" / "state",
                        Path(env["TMPDIR"]) / "hanig-swarm-state"))
                program = command[2]
                if mode[0] == "leak":
                    # Mutation: restore the report fixture's original shared
                    # state destination, inside the guard-owned surrogate HOME.
                    program = program.replace(
                        "from tests import test_report\n",
                        "from tests import test_report\n"
                        "test_report.setUpModule = lambda: None\n")
                result = real_run([command[0], "-c", prefix + program], **kwargs)
                names = {json.loads(line[len("STATE_PROJECT "):])
                         for line in result.stdout.splitlines()
                         if line.startswith("STATE_PROJECT ")}
                observed.append(names)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if mode[0] == "concurrent":
                    for name in names | {"unrelated"}:
                        (caller_state / "hanig-swarm" / "projects" / name).mkdir()
                return result

            def owned_listing(path):
                self.assertIn(path, allowed_listings,
                              "guard scanned a state home it did not create")
                return real_iterdir(path)

            with mock.patch.dict(os.environ, {
                    "HOME": str(caller_home), "XDG_STATE_HOME": str(caller_state)}), \
                    mock.patch.object(subprocess, "run", side_effect=run_with_reused_paths), \
                    mock.patch.object(Path, "iterdir", owned_listing):
                # Establish the exact names using the actual fixtures, then
                # pre-create stale projects before invoking the guard again.
                self.test_dispatch_and_report_fixtures_leave_real_state_home_unchanged()
                projects = caller_state / "hanig-swarm" / "projects"
                for name in observed[0]:
                    (projects / name).mkdir(parents=True)
                self.test_dispatch_and_report_fixtures_leave_real_state_home_unchanged()
                self.assertEqual(observed[0], observed[1])
                # Remove only this regression's synthetic stale entries, then
                # recreate same-name projects during the next subprocess window.
                for name in observed[0]:
                    (projects / name).rmdir()
                mode[0] = "concurrent"
                self.test_dispatch_and_report_fixtures_leave_real_state_home_unchanged()
                self.assertEqual(observed[0], observed[2])
                mode[0] = "leak"
                with self.assertRaisesRegex(AssertionError, "test fixtures created real state-home projects"):
                    self.test_dispatch_and_report_fixtures_leave_real_state_home_unchanged()

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
