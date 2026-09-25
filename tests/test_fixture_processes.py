"""Exercise fixture cleanups and their guard through real unittest outcomes."""
import itertools
import os
from pathlib import Path
import select
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_processes import FixtureProcesses, process_table


def kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class TestFixtureProcessGuard(unittest.TestCase):
    def test_000_non_session_fixture_orphan_exec_is_killed_by_cleanup(self):
        for session_flag in ({}, {'start_new_session': False}):
            with self.subTest(session_flag=session_flag):
                observed = self._orphan_probe(session_flag=session_flag)
                self.assertTrue(observed['result'].wasSuccessful(),
                                observed['result'].failures)
                self.assertTrue(observed['gone'],
                                'root-free orphan survived a clean unittest result')

    def _orphan_probe(self, session_flag=None, leader_only=False):
        observed = {}

        class Probe(unittest.TestCase):
            def runTest(case):
                directory = tempfile.TemporaryDirectory()
                case.addCleanup(directory.cleanup)

                def kill(pgid):
                    if leader_only:
                        # Remove both marker-bearing leaders, leaving only
                        # the orphan and the unreaped session anchor's PID.
                        for pid in {pgid, proc.pid}:
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    else:
                        kill_group(pgid)

                scope = FixtureProcesses(case, directory.name, kill)
                script = Path(directory.name) / 'fixture.py'
                pidfile = Path(directory.name) / 'orphan.pid'
                reaped = Path(directory.name) / 'middle-reaped'
                # The middle child exits before cleanup, severing the PPID
                # chain. The grandchild execs away every root-bearing marker.
                script.write_text(
                    'import os, time\n'
                    'middle = os.fork()\n'
                    'if middle == 0:\n'
                    '    if os.fork(): os._exit(0)\n'
                    '    with open(%r, "w") as f: f.write(str(os.getpid()))\n'
                    '    os.execl("/bin/sleep", "/bin/sleep", "600")\n'
                    'os.waitpid(middle, 0)\n'
                    'open(%r, "w").close()\n'
                    'time.sleep(600)\n' % (str(pidfile), str(reaped)))
                proc = scope.popen([sys.executable, str(script)],
                                   **(session_flag or {}))
                observed['proc'] = proc
                # Independent containment also works when group *recording*
                # is mutated away. Never signal the outer test runner's group.
                pgid = os.getpgid(proc.pid)
                self.assertNotEqual(pgid, os.getpgrp())
                supervisor = scope.children[-1][0]
                observed['anchor'] = supervisor

                def contain():
                    if supervisor.returncode is None or not observed.get('gone', False):
                        kill_group(pgid)
                    supervisor.wait(timeout=5)

                self.addCleanup(contain)
                deadline = time.monotonic() + 20
                while True:
                    if reaped.exists() and pidfile.exists() and pidfile.read_text():
                        pid = int(pidfile.read_text())
                        row = process_table().get(pid)
                        if row and row[0] != proc.pid and row[3] == '/bin/sleep 600':
                            observed['orphan'] = pid
                            case.assertEqual(row[1], pgid)
                            break
                    case.assertLess(time.monotonic(), deadline, 'orphan did not exec')
                    time.sleep(0.01)

        result = unittest.TestResult()
        Probe().run(result)
        observed['result'] = result
        row = process_table().get(observed['orphan'])
        observed['gone'] = row is None or row[2].startswith('Z')
        return observed

    def test_root_free_group_survivor_fails_before_the_leader_is_reaped(self):
        observed = self._orphan_probe(leader_only=True)
        result = observed['result']
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 2)
        self.assertIn('fixture PIDs survived KILL', result.failures[0][1])
        self.assertIn('fixture processes survived cleanup', result.failures[1][1])
        self.assertIsNone(observed['anchor'].returncode,
                          'failed termination released the group ownership anchor')
        self.assertFalse(observed['gone'])

    def test_reaping_non_session_fixture_does_not_release_its_group_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            case = unittest.TestCase()
            scope = FixtureProcesses(case, directory, kill_group)
            script = Path(directory) / 'fixture.py'
            script.write_text(
                'import subprocess\n'
                'child = subprocess.Popen(["/bin/sleep", "600"], '
                'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, '
                'stderr=subprocess.DEVNULL)\n'
                'print(child.pid, flush=True)\n')
            proc = scope.popen([sys.executable, str(script)],
                               stdout=subprocess.PIPE, text=True)
            anchor = scope.children[-1][0]
            cleaned = False
            try:
                out, _err = proc.communicate(timeout=10)
                orphan = int(out.strip())
                reserved = anchor.returncode is None
                pgid = os.getpgid(orphan)
                self.assertEqual(proc.returncode, 0)
                self.assertTrue(case.doCleanups())
                row = process_table().get(orphan)
                cleaned = row is None or row[2].startswith('Z')
                self.assertTrue(cleaned, 'caller-reaped fixture left a live orphan: %r' % (row,))
                self.assertTrue(reserved)
                self.assertNotEqual(proc.pid, anchor.pid)
                self.assertEqual(pgid, anchor.pid)
            finally:
                if not cleaned:
                    kill_group(anchor.pid)
                anchor.wait(timeout=5)
                case.doCleanups()

    def test_non_session_handle_keeps_native_io_timeout_signal_and_launch_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            case = unittest.TestCase()
            scope = FixtureProcesses(case, directory, kill_group)
            try:
                # Preserve the native positional surface (bufsize, executable,
                # stdin, stdout, stderr), as well as keyword-only invocation.
                positional = scope.popen(
                    [sys.executable, '-c', 'print("positional")'],
                    0, None, None, subprocess.PIPE, None)
                self.assertEqual(positional.communicate(timeout=10), (b'positional\n', None))
                self.assertEqual(positional.returncode, 0)
                self.assertEqual(scope.popen(
                    args=[sys.executable, '-c', 'pass'], bufsize=0).wait(timeout=10), 0)
                saved_cwd = Path.cwd()
                relative_case = unittest.TestCase()
                try:
                    os.chdir(directory)
                    Path('relative-scope').mkdir()
                    Path('child-cwd').mkdir()
                    relative_scope = FixtureProcesses(relative_case, 'relative-scope', kill_group)
                    relative = relative_scope.popen(
                        [sys.executable, '-c', 'import os; print(os.getcwd())'],
                        cwd='child-cwd', stdout=subprocess.PIPE, text=True)
                    self.assertEqual(relative.communicate(timeout=10)[0].strip(),
                                     str(Path('child-cwd').resolve()))
                    self.assertEqual(relative.returncode, 0)
                    self.assertEqual(relative_scope.run_python(
                        'import os; os.chdir("/"); result = "cwd changed"'), 'cwd changed')
                finally:
                    try:
                        self.assertTrue(relative_case.doCleanups())
                    finally:
                        os.chdir(saved_cwd)
                proc = scope.popen(
                    [sys.executable, '-c', 'import sys; print(sys.stdin.read().upper())'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                out, _err = proc.communicate(input='fixture', timeout=10)
                self.assertEqual(out, 'FIXTURE\n')
                self.assertEqual(proc.wait(timeout=0), 0)
                proc = scope.popen(['/bin/sleep', '600'], stdout=subprocess.PIPE)
                self.assertIsNone(proc.poll())
                with self.assertRaises(subprocess.TimeoutExpired):
                    proc.communicate(timeout=0.01)
                proc.kill()
                self.assertEqual(proc.communicate(timeout=5), (b'', None))
                self.assertEqual(proc.returncode, -signal.SIGKILL)
                missing = str(Path(directory) / 'missing-command')
                with self.assertRaises(FileNotFoundError) as caught:
                    scope.popen([missing])
                self.assertEqual(caught.exception.filename, missing)
            finally:
                self.assertTrue(case.doCleanups())

    def test_retained_supervisor_contains_a_reaped_non_session_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            case = unittest.TestCase()
            scope = FixtureProcesses(case, directory, kill_group)
            fixture = (
                'import json, os, subprocess\n'
                'child = subprocess.Popen(["/bin/sleep", "600"], '
                'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, '
                'stderr=subprocess.DEVNULL)\n'
                'print(json.dumps({"pid": child.pid, '
                '"pgid": os.getpgid(child.pid), "sid": os.getsid(child.pid)}))\n')
            source = (
                'import json, subprocess, sys\n'
                'completed = subprocess.run([sys.executable, "-c", %r], '
                'capture_output=True, text=True, check=True)\n'
                'result = json.loads(completed.stdout)\n' % fixture)
            try:
                result = scope.run_python(source)
                supervisor = scope.children[0][0]
                self.assertEqual(result['pgid'], supervisor.pid)
                self.assertEqual(result['sid'], supervisor.pid)
                self.assertNotEqual(result['pid'], result['pgid'])
                self.assertIsNone(supervisor.returncode)
                self.assertTrue(case.doCleanups())
                row = process_table().get(result['pid'])
                self.assertTrue(row is None or row[2].startswith('Z'), row)
            finally:
                self.assertTrue(case.doCleanups())

    def test_supervisor_errors_and_timeouts_still_clean_up(self):
        for source, error, timeout in (
                ('import time; time.sleep(2); raise ValueError("injected fixture error")',
                 AssertionError, 60),
                ('import time; time.sleep(600)', subprocess.TimeoutExpired, 1)):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                case = unittest.TestCase()
                scope = FixtureProcesses(case, directory, kill_group)
                try:
                    with self.assertRaises(error):
                        scope.run_python(source, timeout=timeout)
                finally:
                    self.assertTrue(case.doCleanups())
                self.assertIsNotNone(scope.children[0][0].returncode)
                scope.assert_no_survivors()

    def test_reap_timeout_reaches_the_unittest_result(self):
        class Probe(unittest.TestCase):
            def runTest(case):
                directory = tempfile.TemporaryDirectory()
                case.addCleanup(directory.cleanup)
                patcher = None
                case.addCleanup(lambda: patcher.stop() if patcher else None)
                scope = FixtureProcesses(case, directory.name, kill_group)
                proc = scope.popen(['/bin/sleep', '600'])
                anchor = scope.children[-1][0]
                self.addCleanup(anchor.wait, timeout=5)
                patcher = mock.patch.object(
                    anchor, 'wait', side_effect=subprocess.TimeoutExpired(proc.args, 5))
                patcher.start()

        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn('TimeoutExpired', result.errors[0][1])

    def _probe(self, outcome='pass', omit_cleanup=False, escape=False, root_free=False):
        observed = {}

        class Probe(unittest.TestCase):
            def runTest(case):
                directory = tempfile.TemporaryDirectory()
                observed['root'] = directory.name
                case.addCleanup(directory.cleanup)
                scope = FixtureProcesses(case, directory.name, kill_group)
                # The outer test contains its intentional leak even if the
                # inner guard regresses; this runs after result assertions.
                self.addCleanup(scope.cleanup)
                observed['scope'] = scope
                script = Path(directory.name) / 'fixture.py'
                if escape:
                    pidfile = Path(directory.name) / 'escaped.pid'
                    scope.record_pidfile(pidfile)
                    script.write_text(
                        'import os, time\n'
                        'read_fd, write_fd = os.pipe()\n'
                        'pid = os.fork()\n'
                        'if pid:\n'
                        '    os.close(write_fd)\n'
                        '    os.read(read_fd, 1)\n'
                        '    os._exit(0)\n'
                        'os.close(read_fd)\n'
                        'os.setsid()\n'
                        'with open(%r, "w") as f: f.write(str(os.getpid()))\n'
                        'os.write(write_fd, b"R")\n'
                        'os.close(write_fd)\n'
                        'time.sleep(600)\n' % str(pidfile))
                elif root_free:
                    script.write_text(
                        'import os\nos.execl("/bin/sleep", "/bin/sleep", "600")\n')
                else:
                    script.write_text(
                        'import time\nprint("ready", flush=True)\ntime.sleep(600)\n')
                proc = scope.popen(
                    [sys.executable, str(script)], start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
                observed['proc'] = proc
                if root_free:
                    deadline = time.monotonic() + 10
                    while True:
                        row = process_table().get(proc.pid)
                        if row and row[3] == '/bin/sleep 600':
                            break
                        case.assertLess(time.monotonic(), deadline, 'fixture did not exec')
                        time.sleep(0.01)
                if not escape and not root_free:
                    ready, _, _ = select.select([proc.stdout], [], [], 10)
                    case.assertTrue(ready, 'fixture did not reach its sleep')
                    case.assertEqual(proc.stdout.readline(), b'ready\n')
                if escape:
                    case.assertEqual(proc.wait(timeout=10), 0)
                    pid = int(pidfile.read_text())
                    observed['escaped'] = pid
                    case.assertEqual(os.getpgid(pid), pid)
                    os.kill(pid, 0)
                if outcome == 'failure':
                    case.fail('injected assertion failure')
                if outcome == 'timeout':
                    proc.wait(timeout=0.01)

        real_add = Probe.addCleanup

        def register(case, function, *args, **kwargs):
            if omit_cleanup and getattr(function, '__name__', '') == 'cleanup':
                # Remove exactly FixtureProcesses.cleanup, not dir cleanup.
                if isinstance(getattr(function, '__self__', None), FixtureProcesses):
                    return
            real_add(case, function, *args, **kwargs)

        with mock.patch.object(Probe, 'addCleanup', register):
            result = unittest.TestResult()
            Probe().run(result)
        # Inspect what unittest received, not text printed by the guard.
        observed['result'] = result
        scope = observed['scope']
        self.assertEqual(scope._root_rows(process_table()), {})
        self.assertIsNotNone(observed['proc'].returncode)
        self.assertFalse(Path(observed['root']).exists())
        return observed

    def test_cleanup_runs_after_pass_assertion_failure_and_timeout(self):
        for outcome in ('pass', 'failure', 'timeout'):
            with self.subTest(outcome=outcome):
                result = self._probe(outcome)['result']
                self.assertEqual(len(result.failures), int(outcome == 'failure'))
                self.assertEqual(len(result.errors), int(outcome == 'timeout'))
                if outcome == 'failure':
                    self.assertIn('injected assertion failure', result.failures[0][1])
                if outcome == 'timeout':
                    self.assertIn('TimeoutExpired', result.errors[0][1])

    def test_guard_fails_through_unittest_when_cleanup_is_removed(self):
        result = self._probe(omit_cleanup=True)['result']
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn('fixture processes survived cleanup', result.failures[0][1])

    def test_guard_includes_unreaped_children_after_root_free_exec(self):
        self.assertTrue(self._probe(root_free=True)['result'].wasSuccessful())
        result = self._probe(root_free=True, omit_cleanup=True)['result']
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn('fixture processes survived cleanup', result.failures[0][1])

    def test_reaped_pid_records_do_not_target_unrelated_reused_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = FixtureProcesses(unittest.TestCase(), directory, mock.Mock())
            pid = 123456789
            child = SimpleNamespace(pid=pid, returncode=0, stdin=None,
                                    stdout=None, stderr=None, wait=mock.Mock())
            scope.children.append((child, pid))
            scope.pidfiles[0].write_text(str(pid))
            foreign = {pid: (1, pid, 'S', '/unrelated/worker')}
            with mock.patch('fixture_processes.process_table', return_value=foreign), mock.patch(
                    'fixture_processes.os.kill') as kill:
                scope.cleanup()
                scope.assert_no_survivors()
            scope.kill_group.assert_not_called()
            kill.assert_not_called()

    def test_post_kill_observation_does_not_reuse_old_pid_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = FixtureProcesses(unittest.TestCase(), directory, mock.Mock())
            pid = 123456789
            scope.pidfiles[0].write_text(str(pid))
            fixture = {pid: (1, pid, 'S', str(Path(directory) / 'fixture'))}
            foreign = {pid: (1, pid, 'S', '/unrelated/worker')}
            with mock.patch('fixture_processes.process_table', side_effect=itertools.chain([fixture], itertools.repeat(foreign))), mock.patch(
                    'fixture_processes.os.kill') as kill:
                scope.cleanup()
            scope.kill_group.assert_called_once_with(pid)
            # Group KILL is enough; a subsequent positive signal would race reuse.
            kill.assert_not_called()

    def test_slow_snapshot_requires_a_fresh_observation_after_grace(self):
        for survives in (False, True):
            with self.subTest(survives=survives), tempfile.TemporaryDirectory() as directory:
                scope = FixtureProcesses(unittest.TestCase(), directory, mock.Mock())
                pid = 123456789
                scope.pidfiles[0].write_text(str(pid))
                fixture = {pid: (1, pid, 'S', str(Path(directory) / 'fixture'))}
                clock = [0]
                snapshots = []

                def snapshot():
                    snapshots.append(clock[0])
                    if len(snapshots) == 2:
                        clock[0] = 6  # ps delivers an old row after the grace period.
                    if len(snapshots) <= 2 or survives:
                        return fixture
                    return {}

                with mock.patch('fixture_processes.process_table', snapshot), mock.patch(
                        'fixture_processes.time.monotonic', side_effect=lambda: clock[0]), mock.patch(
                        'fixture_processes.time.sleep'), mock.patch('fixture_processes.os.kill'):
                    if survives:
                        with self.assertRaisesRegex(AssertionError, 'fixture PIDs survived KILL'):
                            scope.cleanup()
                    else:
                        scope.cleanup()
                self.assertEqual(snapshots, [0, 0, 6])

    def test_detached_descendant_is_cleaned_after_its_parent_is_reaped(self):
        observed = self._probe(escape=True)
        self.assertTrue(observed['result'].wasSuccessful())
        row = process_table().get(observed['escaped'])
        self.assertTrue(row is None or row[2].startswith('Z'), row)

    def test_group_kill_precedes_wait_and_never_polls(self):
        with tempfile.TemporaryDirectory() as directory:
            case = unittest.TestCase()
            events = []

            def record_kill(pgid):
                self.assertIsNone(proc.returncode, 'group kill requested after reap')
                events.append('kill')
                kill_group(pgid)

            scope = FixtureProcesses(case, directory, record_kill)
            script = Path(directory) / 'fixture.py'
            script.write_text('import time\ntime.sleep(600)\n')
            proc = scope.popen([sys.executable, str(script)], start_new_session=True)
            original_wait = proc.wait

            def record_wait(*args, **kwargs):
                self.assertIn('kill', events)
                events.append('wait')
                return original_wait(*args, **kwargs)

            try:
                with mock.patch.object(proc, 'wait', record_wait), mock.patch.object(
                        proc, 'poll', side_effect=AssertionError('polled before KILL')):
                    self.assertTrue(case.doCleanups())
                self.assertEqual(events[0], 'kill')
                self.assertIn('wait', events)
                self.assertIsNotNone(proc.returncode)
            finally:
                # Independent containment if the ordering assertion regresses.
                if proc.returncode is None:
                    record_kill(proc.pid)
                    original_wait(timeout=5)


    def test_sleep_factory_records_post_exec_identity_and_keeps_the_group(self):
        for invocation in ('bare', 'absolute'):
            with self.subTest(invocation=invocation), tempfile.TemporaryDirectory() as directory:
                case = unittest.TestCase()
                scope = FixtureProcesses(case, directory, kill_group)
                wrapper = scope.sleep_command()
                command = 'sleep' if invocation == 'bare' else shlex.quote(str(wrapper))
                script = Path(directory) / 'parent'
                script.write_text(scope.shell_script(
                    '#!/bin/sh\n' + command + ' 600 &\n' +
                    'printf \'%s\\n\' "$!"\nexit 0\n'))
                script.chmod(0o700)
                proc = scope.popen([str(script)], start_new_session=True,
                                   stdout=subprocess.PIPE, text=True)
                try:
                    ready, _, _ = select.select([proc.stdout], [], [], 10)
                    self.assertTrue(ready, 'parent did not report its child')
                    int(proc.stdout.readline())  # Background job started.
                    child = None
                    deadline = time.monotonic() + 10
                    while True:
                        table = process_table()
                        for word in scope.pidfiles[0].read_text().split():
                            candidate = int(word)
                            row = table.get(candidate)
                            if candidate != proc.pid and row and not row[3].startswith(('/bin/sh ', 'sh ')):
                                child = candidate
                                break
                        if child is not None:
                            break
                        self.assertLess(time.monotonic(), deadline, 'sleep did not exec')
                        time.sleep(0.01)
                    # Check the actual long-lived executable, not its wrapper.
                    self.assertIn(directory + '/', row[3])
                    self.assertEqual(row[1], proc.pid)
                    self.assertIn(str(child), scope.pidfiles[0].read_text().split())
                    self.assertEqual(proc.wait(timeout=5), 0)
                    self.assertTrue(case.doCleanups())
                    row = process_table().get(child)
                    self.assertTrue(row is None or row[2].startswith('Z'), row)
                finally:
                    # Before the identity assertion passes, do not reap the
                    # parent: its reserved PGID contains a root-free mutant.
                    self.assertTrue(case.doCleanups())

    def test_project_consumers_clean_up_after_a_leader_only_supervisor(self):
        import test_project as project

        with tempfile.TemporaryDirectory() as directory:
            doctor = Path(directory) / 'doctor'
            source, count = re.subn(
                r'kill "(TERM|KILL)", -\$(active_pid|pid)',
                r'kill "\1", $\2', project.DOCTOR.read_text())
            self.assertGreater(count, 0)
            # Preserve the real checkout's diagnostics and installed-script
            # lookups; only the supervisor's group signals are mutated.
            repo_assignment = 'REPO=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)'
            self.assertIn(repo_assignment, source)
            source = source.replace(repo_assignment, 'REPO=' + shlex.quote(str(project.ROOT)))
            doctor.write_text(source)
            names = (
                'test_timeout_cleans_up_an_ordinary_login_profile_descendant',
                'test_timed_out_health_output_cannot_report_the_daemon_up',
                'test_a_slow_valid_compiler_is_timeout_not_a_syntax_failure',
            )
            original_read = Path.read_text
            for name in names:
                with self.subTest(consumer=name):
                    observed = {}

                    def capture_records(path, *args, **kwargs):
                        value = original_read(path, *args, **kwargs)
                        if path.name in ('fixture-groups.pid', 'profile-child.pid'):
                            table = process_table()
                            for word in value.split():
                                pid = int(word)
                                row = table.get(pid)
                                if row and not row[2].startswith('Z') and (
                                        str(path.parent) + '/' in row[3] or
                                        (path.name == 'profile-child.pid' and
                                         row[3] == '/bin/sleep 600')):
                                    observed[pid] = (row[1], row[3])
                        return value

                    def matching_rows(records=observed):
                        return {pid: row for pid, row in process_table().items()
                                if pid in records and not row[2].startswith('Z')
                                and (row[1], row[3]) == records[pid]}

                    def contain_mutation(matching=matching_rows):
                        # Preserve the failed observation; contain a regression
                        # in these deliberately failing inner unittest cases.
                        for pgid in {row[1] for row in matching().values()} - {os.getpgrp()}:
                            kill_group(pgid)

                    self.addCleanup(contain_mutation)
                    case = project.TestDoctorSeesThePrerequisitesTheSkillsRefuseWithout(name)
                    result = unittest.TestResult()
                    with mock.patch.object(project, 'DOCTOR', doctor), mock.patch.object(
                            Path, 'read_text', capture_records):
                        case.run(result)
                    survivors = matching_rows()
                    contain_mutation()
                    self.assertEqual(survivors, {})
                    self.assertEqual(result.errors, [])
                    if name == names[0]:
                        self.assertTrue(observed, 'profile child was never observed alive')
                        self.assertEqual(len(result.failures), 1)
                        self.assertIn('login-profile descendant survived timeout',
                                      result.failures[0][1])
                    else:
                        self.assertTrue(result.wasSuccessful(), result.failures)

    def test_project_readiness_failure_still_cleans_isolated_groups(self):
        import test_project as project

        roots = []
        original = Path.read_text

        def fail_before_pid_assignment(path, *args, **kwargs):
            if path.name == 'leader.pid':
                roots.append(str(path.parent))
                raise AssertionError('injected failure before PID assignment')
            return original(path, *args, **kwargs)

        case = project.TestDoctorSeesThePrerequisitesTheSkillsRefuseWithout(
            'test_interrupt_signals_clean_up_the_isolated_probe_group')
        result = unittest.TestResult()
        with mock.patch.object(Path, 'read_text', fail_before_pid_assignment):
            case.run(result)
        self._assert_roots_gone(roots)
        self.assertEqual(len(roots), 3)
        self.assertEqual(len(result.failures), 3)
        self.assertEqual(result.errors, [])
        for _test, failure in result.failures:
            self.assertIn('injected failure before PID assignment', failure)

    def test_project_failure_before_escape_pid_read_still_cleans_escape(self):
        import test_project as project

        roots = []
        cls = project.TestDoctorSeesThePrerequisitesTheSkillsRefuseWithout
        original = cls._doctor

        def fail_after_doctor(case, directory, *args, **kwargs):
            roots.append(directory)
            original(case, directory, *args, **kwargs)
            raise AssertionError('injected failure before escape PID read')

        case = cls('test_timeout_does_not_wait_for_a_descendant_that_calls_setsid')
        result = unittest.TestResult()
        with mock.patch.object(cls, '_doctor', fail_after_doctor):
            case.run(result)
        self._assert_roots_gone(roots)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn('injected failure before escape PID read', result.failures[0][1])

    def _assert_roots_gone(self, roots):
        survivors = {pid: row for pid, row in process_table().items()
                     if not row[2].startswith('Z')
                     and any(root + '/' in row[3] for root in roots)}
        # Preserve a failed observation while containing the base-code mutation.
        for pgid in {row[1] for row in survivors.values()} - {os.getpgrp()}:
            kill_group(pgid)
        self.assertEqual(survivors, {})


if __name__ == '__main__':
    unittest.main()
