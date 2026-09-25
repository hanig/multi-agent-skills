"""Exercise the typed fixture contract through real processes and unittest."""
import ast
from dataclasses import replace
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixture_processes as fixtures
from fixture_processes import (CleanupState, FixtureProcesses, FixtureProcess,
                               FixtureSpec, FixtureSignal, JoinState, SignalState,
                               process_table, wait_readable)


def kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('fixture did not become ready')
        time.sleep(0.01)


class TestFixtureProcessGuard(unittest.TestCase):
    def _scope(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        scope = FixtureProcesses(self, directory.name)
        return scope

    def _launch(self, scope, source):
        script = scope.root / ('child-%d.py' % len(scope.children))
        script.write_text(source)
        return scope.launch(FixtureSpec((sys.executable, str(script))))

    def _probe(self, outcome='pass', omit_cleanup=False, escape=False, root_free=False):
        observed = {}

        class Probe(unittest.TestCase):
            def runTest(case):
                directory = tempfile.TemporaryDirectory()
                observed['root'] = directory.name
                case.addCleanup(directory.cleanup)
                scope = FixtureProcesses(case, directory.name)
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
                    script.write_text('import os\nos.execl("/bin/sleep", "/bin/sleep", "600")\n')
                else:
                    script.write_text('import time\nprint("ready", flush=True)\ntime.sleep(600)\n')
                proc = scope.launch(FixtureSpec((sys.executable, str(script))))
                observed['proc'] = proc
                # Independent outer containment is also used by mutations.
                self.addCleanup(proc._perform_cleanup)
                if root_free:
                    wait_for(lambda: process_table().get(proc.child_pid, (None, None, None, ''))[3]
                             == '/bin/sleep 600')
                if not escape and not root_free:
                    wait_for(lambda: proc.stdout_path.read_bytes() == b'ready\n')
                    case.assertEqual(proc.stdout_path.read_bytes(), b'ready\n')
                if escape:
                    result = proc.join(10)
                    case.assertEqual(result.state, JoinState.EXITED)
                    case.assertEqual(result.returncode, 0)
                    pid = int(pidfile.read_text())
                    observed['escaped'] = pid
                    case.assertEqual(os.getpgid(pid), pid)
                    os.kill(pid, 0)
                if outcome == 'failure':
                    case.fail('injected assertion failure')
                if outcome == 'timeout':
                    result = proc.join(0.01)
                    case.assertEqual(result.state, JoinState.TIMED_OUT)
                    raise TimeoutError('typed fixture join timed out')

        real_add = Probe.addCleanup

        def register(case, function, *args, **kwargs):
            if omit_cleanup and getattr(function, '__name__', '') == 'cleanup':
                if isinstance(getattr(function, '__self__', None), FixtureProcesses):
                    return
            real_add(case, function, *args, **kwargs)

        with mock.patch.object(Probe, 'addCleanup', register):
            result = unittest.TestResult()
            Probe().run(result)
        observed['result'] = result
        self.assertEqual(observed['scope']._root_rows(process_table()), {})
        self.assertIn(observed['proc'].join(0).state,
                      (JoinState.EXITED, JoinState.STATUS_UNAVAILABLE))
        self.assertFalse(Path(observed['root']).exists())
        return observed

    def test_cleanup_runs_after_pass_assertion_failure_and_timeout(self):
        for outcome in ('pass', 'failure', 'timeout'):
            with self.subTest(outcome=outcome):
                result = self._probe(outcome)['result']
                self.assertEqual(len(result.failures), int(outcome == 'failure'), result.failures)
                self.assertEqual(len(result.errors), int(outcome == 'timeout'), result.errors)
                if outcome == 'failure':
                    self.assertIn('injected assertion failure', result.failures[0][1])
                if outcome == 'timeout':
                    self.assertIn('typed fixture join timed out', result.errors[0][1])

    def test_guard_fails_through_unittest_when_cleanup_is_removed(self):
        result = self._probe(omit_cleanup=True)['result']
        self.assertEqual(len(result.failures), 1, result.failures)
        self.assertEqual(result.errors, [])
        self.assertIn('fixture processes survived cleanup', result.failures[0][1])

    def test_guard_includes_unreaped_children_after_root_free_exec(self):
        self.assertTrue(self._probe(root_free=True)['result'].wasSuccessful())
        result = self._probe(root_free=True, omit_cleanup=True)['result']
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn('fixture processes survived cleanup', result.failures[0][1])

    def test_reaped_pid_records_do_not_target_unrelated_reused_ids(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass\n')
        self.assertEqual(proc.join(10).returncode, 0)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        foreign = replace(proc._anchor, birth=(9999999999, 0))
        with mock.patch.object(fixtures, '_identity', return_value=foreign), mock.patch.object(
                fixtures.os, 'kill') as kill:
            result = proc._perform_cleanup()
        self.assertEqual(result.state, CleanupState.INDETERMINATE)
        kill.assert_not_called()

    def test_post_kill_observation_does_not_reuse_old_pid_identity(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass\n')
        self.assertEqual(proc.join(10).returncode, 0)
        foreign = replace(proc._anchor, birth=(9999999999, 0))
        with mock.patch.object(fixtures, '_identity', return_value=foreign), mock.patch.object(
                fixtures.os, 'kill') as kill:
            with self.assertRaisesRegex(fixtures._Indeterminate, 'PID changed'):
                proc._signal_owned(proc.supervisor_pid, proc._anchor, signal.SIGKILL)
        kill.assert_not_called()

    def test_slow_snapshot_requires_a_fresh_observation_after_grace(self):
        for survives in (False, True):
            with self.subTest(survives=survives):
                scope = self._scope()
                proc = self._launch(scope, 'pass\n')
                self.assertEqual(proc.join(10).returncode, 0)
                clock, snapshots = [0], []
                target = {proc.child_pid: proc._anchor}

                def snapshot():
                    snapshots.append(clock[0])
                    if len(snapshots) == 2:
                        clock[0] = 9
                    return (target if len(snapshots) <= 2 or survives else {}), True

                with mock.patch.object(proc, '_survivor_scan', snapshot), mock.patch.object(
                        fixtures.time, 'monotonic', side_effect=lambda: clock[0]), mock.patch.object(
                        fixtures.time, 'sleep'), mock.patch.object(proc, '_signal_owned'), mock.patch.object(
                        proc, '_reap'):
                    result = proc._perform_cleanup()
                if survives:
                    self.assertEqual(result.state, CleanupState.INDETERMINATE)
                else:
                    self.assertEqual(result.state, CleanupState.CLEAN)
                self.assertEqual(snapshots[:3], [0, 0, 9])
                # Synthetic reap did not release the real anchor. The outer
                # scope still has to contain it through the real cleanup path.

    def test_detached_descendant_is_cleaned_after_its_parent_is_reaped(self):
        observed = self._probe(escape=True)
        self.assertTrue(observed['result'].wasSuccessful(), observed['result'].failures)
        self.assertNotIn(observed['escaped'], process_table())

    def test_group_kill_precedes_wait_and_never_polls(self):
        scope = self._scope()
        proc = self._launch(scope, 'import time\ntime.sleep(600)\n')
        events = []
        original_signal, original_reap = proc._signal_owned, proc._reap

        def record_signal(pid, identity, signum):
            self.assertIsNone(proc._supervisor.returncode, 'signal requested after reap')
            events.append('kill')
            return original_signal(pid, identity, signum)

        def record_reap():
            self.assertIn('kill', events)
            events.append('wait')
            return original_reap()

        with mock.patch.object(proc, '_signal_owned', record_signal), mock.patch.object(
                proc, '_reap', record_reap), mock.patch.object(
                proc._supervisor, 'poll', side_effect=AssertionError('polled before KILL')):
            self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        self.assertEqual(events[0], 'kill')
        self.assertIn('wait', events)
        self.assertIsNotNone(proc._supervisor.returncode)

    def test_sleep_factory_records_post_exec_identity_and_keeps_the_group(self):
        for invocation in ('bare', 'absolute'):
            with self.subTest(invocation=invocation):
                scope = self._scope()
                wrapper = scope.sleep_command()
                command = 'sleep' if invocation == 'bare' else shlex.quote(str(wrapper))
                script = scope.root / 'parent'
                script.write_text(scope.shell_script(
                    '#!/bin/sh\n' + command + ' 600 &\n' +
                    'printf \'%s\\n\' "$!"\nexit 0\n'))
                script.chmod(0o700)
                proc = scope.launch(FixtureSpec((str(script),)))
                wait_for(lambda: bool(proc.stdout_path.read_text().strip()))
                int(proc.stdout_path.read_text())  # Background job started.
                observed = {}
                def find_sleep():
                    table = process_table()
                    for word in scope.pidfiles[0].read_text().split():
                        candidate = int(word)
                        row = table.get(candidate)
                        if row and row[3].startswith(str(scope.root / 'fixture-sleep-executable')):
                            observed['child'], observed['row'] = candidate, row
                            return True
                    return False
                wait_for(find_sleep)
                child, row = observed['child'], observed['row']
                self.assertIn(str(scope.root) + '/', row[3])
                self.assertEqual(row[1], proc.child_pid)
                self.assertIn(str(child), scope.pidfiles[0].read_text().split())
                self.assertEqual(proc.join(5).returncode, 0)
                self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
                self.assertNotIn(child, process_table())

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


class TestTypedFixtureContract(unittest.TestCase):
    _scope = TestFixtureProcessGuard._scope
    _launch = TestFixtureProcessGuard._launch

    def test_every_unsupported_launch_field_is_refused_before_side_effects(self):
        import inspect
        scope = self._scope()
        side_effect = scope.root / 'SHOULD-NOT-EXIST'
        command = (sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(side_effect))
        unsupported = set(inspect.signature(subprocess.Popen).parameters) | {
            'process_group', 'pass_fds', 'preexec_fn', 'user', 'group',
            'extra_groups', 'umask', 'start_new_session', 'unknown_field'}
        for field in sorted(unsupported):
            with self.subTest(field=field), mock.patch.object(fixtures.subprocess, 'Popen') as launch:
                with self.assertRaises((TypeError, ValueError)):
                    scope.launch(FixtureSpec(command, **{field: 0}))
                launch.assert_not_called()
                self.assertFalse(side_effect.exists())
        # Validation also runs at launch, before a forged/mutated spec reaches
        # the supervisor. This is the executable refusal mutation target.
        forged = FixtureSpec(command)
        object.__setattr__(forged, 'pass_fds', ())
        with mock.patch.object(fixtures.subprocess, 'Popen', wraps=subprocess.Popen) as launch:
            with self.assertRaisesRegex(TypeError, 'FixtureSpec fields'):
                scope.launch(forged)
            launch.assert_not_called()
        self.assertFalse(side_effect.exists())
        self.assertEqual(list(scope.root.iterdir()), [])

    def test_invalid_values_and_non_specs_are_refused_before_launch(self):
        scope = self._scope()
        bad = (
            {'command': 'echo hello'}, {'command': ()},
            {'command': ('x\0y',)}, {'command': ('x',), 'directory': '.'},
            {'command': ('x',), 'environment': {'PATH': '/bin'}},
            {'command': ('x',), 'environment': (('X', '1'), ('X', '2'))},
            {'command': ('x',), 'environment': ((fixtures._MARKER, 'wrong'),)},
        )
        with mock.patch.object(fixtures.subprocess, 'Popen') as launch:
            for value in bad:
                with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                    scope.launch(FixtureSpec(**value))
            for value in (None, {}, ('echo',), object()):
                with self.assertRaises(TypeError):
                    scope.launch(value)
            launch.assert_not_called()

    def _orphan(self, scope, mode):
        ready = scope.root / ('orphan-' + mode)
        extra = {
            'ordinary': '',
            'group': 'os.setpgid(0, 0)\n',
            'double-fork-session': 'os.setsid()\nif os.fork(): os._exit(0)\n',
        }[mode]
        source = (
            'import os\n'
            'r,w=os.pipe()\n'
            'pid=os.fork()\n'
            'if pid:\n'
            '    os.close(w); os.read(r,1); os._exit(0)\n'
            'os.close(r)\n' + extra +
            'with open(%r,"w") as f: f.write(str(os.getpid()))\n'
            'os.write(w,b"R"); os.close(w)\n'
            'os.execl("/bin/sleep", "/bin/sleep", "600")\n') % str(ready)
        proc = self._launch(scope, source)
        joined = proc.join(10)
        self.assertEqual(joined.state, JoinState.EXITED, joined)
        self.assertEqual(joined.returncode, 0, joined)
        pid = int(ready.read_text())
        wait_for(lambda: process_table().get(pid, (None, None, None, ''))[3] == '/bin/sleep 600')
        # Exact PID plus birth only for outer emergency containment; not a
        # cleanup oracle. Assertions below use a fresh process enumeration.
        identity = fixtures._identity(pid)
        self.addCleanup(proc._signal_owned, pid, identity, signal.SIGKILL)
        return proc, pid

    def test_non_session_parent_exit_does_not_release_its_descendants(self):
        scope = self._scope()
        proc, pid = self._orphan(scope, 'ordinary')
        self.assertNotEqual(proc.child_pid, proc.supervisor_pid)
        self.assertEqual(os.getsid(pid), proc.supervisor_pid)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        self.assertNotIn(pid, process_table())
        self.assertEqual(proc.join(0).returncode, 0)

    def test_additional_groups_and_rapid_double_fork_sessions_are_found(self):
        for mode in ('group', 'double-fork-session'):
            with self.subTest(mode=mode):
                scope = self._scope()
                proc, pid = self._orphan(scope, mode)
                pgid = os.getpgid(pid)
                self.assertNotEqual(pgid, proc.child_pid)
                if mode == 'double-fork-session':
                    self.assertNotEqual(os.getsid(pid), proc.supervisor_pid)
                targets, _stable = proc._survivor_scan()
                self.assertIn(pid, targets)
                self.assertIn(str(pgid), scope.pidfiles[0].read_text().split())
                self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
                self.assertNotIn(pid, process_table())

    def test_external_supervisor_or_group_kill_never_fabricates_child_status(self):
        for group in (False, True):
            with self.subTest(group=group):
                scope = self._scope()
                proc = self._launch(scope, 'import time\nprint("ready",flush=True)\ntime.sleep(600)\n')
                wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
                if group:
                    os.killpg(proc.supervisor_pid, signal.SIGKILL)
                else:
                    os.kill(proc.supervisor_pid, signal.SIGKILL)
                joined = proc.join(10)
                self.assertEqual(joined.state, JoinState.STATUS_UNAVAILABLE)
                self.assertIsNone(joined.returncode)
                self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
                self.assertNotIn(proc.child_pid, process_table())
                self.assertIs(proc.join(0), joined)
                self.assertEqual(proc.terminate().state, SignalState.STATUS_UNAVAILABLE)

    def test_injected_scan_signal_reap_and_marker_failures_prevent_clean(self):
        for failure in ('scan', 'signal', 'reap', 'marker'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                observed = {}

                class Probe(unittest.TestCase):
                    def runTest(case):
                        scope = FixtureProcesses(case, root)
                        proc = scope.launch(FixtureSpec((sys.executable, '-c', 'import time; time.sleep(600)')))
                        observed['proc'] = proc
                        target, name = {
                            'scan': (fixtures, '_enumerate_pids'),
                            'signal': (proc, '_signal_owned'),
                            'reap': (proc, '_reap'),
                            'marker': (fixtures, '_environment'),
                        }[failure]
                        with mock.patch.object(target, name, side_effect=OSError('injected ' + failure)):
                            case.doCleanups()

                result = unittest.TestResult()
                try:
                    Probe().run(result)
                    proc = observed['proc']
                    self.assertFalse(result.wasSuccessful())
                    self.assertEqual(result.errors, [])
                    self.assertGreaterEqual(len(result.failures), 1)
                    self.assertIn('injected ' + failure, result.failures[0][1])
                    self.assertEqual(proc.cleanup().state, CleanupState.ERROR)
                    self.assertIs(proc.cleanup(), proc.cleanup())
                finally:
                    if 'proc' in observed:
                        self.assertEqual(observed['proc']._perform_cleanup().state, CleanupState.CLEAN)

    def test_ambiguous_identity_prevents_clean_without_a_signal(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass\n')
        self.assertEqual(proc.join(10).returncode, 0)
        with mock.patch.object(proc, '_snapshot', side_effect=fixtures._Indeterminate('ambiguous')):
            result = proc._perform_cleanup()
        self.assertEqual(result.state, CleanupState.INDETERMINATE)

    def test_repeated_join_terminate_and_cleanup_preserve_reported_status(self):
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                scope = self._scope()
                proc = self._launch(scope, 'raise SystemExit(%d)\n' % exit_code)
                status = proc.join(10)
                self.assertEqual(status.state, JoinState.EXITED)
                self.assertEqual(status.returncode, exit_code)
                self.assertIs(proc.join(0), status)
                self.assertEqual(proc.terminate().state, SignalState.ALREADY_EXITED)
                cleaned = proc.cleanup()
                self.assertEqual(cleaned.state, CleanupState.CLEAN)
                self.assertIs(proc.cleanup(), cleaned)
                self.assertIs(proc.join(0), status)
                self.assertEqual(proc.terminate().state, SignalState.ALREADY_EXITED)
        scope = self._scope()
        proc = self._launch(scope, 'import time\nprint("ready",flush=True)\ntime.sleep(600)\n')
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        self.assertEqual(proc.join(0).state, JoinState.TIMED_OUT)
        requested = proc.terminate()
        self.assertEqual(requested.state, SignalState.SENT)
        status = proc.join(10)
        self.assertEqual(status.state, JoinState.EXITED)
        self.assertEqual(status.returncode, -signal.SIGTERM)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        self.assertIs(proc.join(0), status)

    def test_closed_caller_stdio_is_supported_without_descriptor_passing(self):
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / 'report.json'
            driver = (
                'import json, os, sys, tempfile, unittest\n'
                'sys.path.insert(0,%r)\n'
                'from fixture_processes import *\n'
                'answers=[]\n'
                'for mask in range(1,8):\n'
                '    saved={fd:os.dup(fd) for fd in (0,1,2)}\n'
                '    try:\n'
                '        for fd in (0,1,2):\n'
                '            if mask & (1<<fd): os.close(fd)\n'
                '        with tempfile.TemporaryDirectory() as d:\n'
                '            scope=FixtureProcesses(unittest.TestCase(),d)\n'
                '            proc=scope.launch(FixtureSpec((sys.executable,"-c","print(42)")))\n'
                '            joined=proc.join(10)\n'
                '            cleaned=proc.cleanup()\n'
                '            answers.append([mask,joined.state.value,joined.returncode,joined.stdout,cleaned.state.value,cleaned.detail])\n'
                '    finally:\n'
                '        for fd,backup in saved.items(): os.dup2(backup,fd); os.close(backup)\n'
                'with open(%r,"w") as f: json.dump(answers,f)\n'
            ) % (str(Path(fixtures.__file__).parent), str(report))
            result = subprocess.run([sys.executable, '-c', driver], capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(report.read_text()),
                             [[mask, 'EXITED', 0, '42\n', 'CLEAN', ''] for mask in range(1, 8)])

    def test_unsupported_platform_and_missing_marker_capability_refuse_before_launch(self):
        scope = self._scope()
        spec = FixtureSpec((sys.executable, '-c', 'pass'))
        with mock.patch.object(fixtures.sys, 'platform', 'unsupported'), mock.patch.object(
                fixtures.subprocess, 'Popen') as launch:
            with self.assertRaises(fixtures.FixtureRefused):
                scope.launch(spec)
            launch.assert_not_called()
        with mock.patch.object(fixtures, '_environment', side_effect=OSError('marker unavailable')), mock.patch.object(
                fixtures.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(OSError, 'marker unavailable'):
                scope.launch(spec)
            launch.assert_not_called()

    def test_external_directory_and_second_launch_are_refused(self):
        scope = self._scope()
        with tempfile.TemporaryDirectory() as elsewhere, mock.patch.object(
                fixtures.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(fixtures.FixtureRefused, 'cwd marker'):
                scope.launch(FixtureSpec((sys.executable, '-c', 'pass'), directory=elsewhere))
            launch.assert_not_called()
        proc = self._launch(scope, 'pass\n')
        self.assertEqual(proc.join(10).state, JoinState.EXITED)
        with mock.patch.object(fixtures.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(fixtures.FixtureRefused, 'one launch'):
                self._launch(scope, 'pass\n')
            launch.assert_not_called()

    def test_omitted_supervisor_is_indeterminate_not_an_empty_scan(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass\n')
        self.assertEqual(proc.join(10).state, JoinState.EXITED)
        with mock.patch.object(fixtures, '_enumerate_pids', return_value=set()):
            self.assertEqual(proc._perform_cleanup().state, CleanupState.INDETERMINATE)

    def test_injected_cwd_marker_failure_prevents_clean(self):
        scope = self._scope()
        proc, pid = self._orphan(scope, 'double-fork-session')
        with mock.patch.object(fixtures, '_has_marker', return_value=False), mock.patch.object(
                fixtures, '_cwd_marker', side_effect=OSError('injected cwd inspection')):
            result = proc._perform_cleanup()
        self.assertEqual(result.state, CleanupState.ERROR)
        self.assertIn('injected cwd inspection', result.detail)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        self.assertNotIn(pid, process_table())

    def test_high_descriptors_use_selectors_for_report_and_control(self):
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard < 1200:
            self.skipTest('host descriptor limit cannot exercise select ceiling')
        held = []
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 1200), hard))
            while not held or held[-1] < 1050:
                held.append(os.open(os.devnull, os.O_RDONLY))
            scope = self._scope()
            proc = self._launch(scope, 'import time\nprint("ready",flush=True)\ntime.sleep(600)\n')
            self.assertGreater(proc._report, 1024)
            self.assertGreater(proc._control, 1024)
            wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
            self.assertEqual(proc.join(0).state, JoinState.TIMED_OUT)
            self.assertEqual(proc.terminate().state, SignalState.SENT)
            joined = proc.join(10)
            self.assertEqual(joined.state, JoinState.EXITED)
            self.assertEqual(joined.returncode, -signal.SIGTERM)
            self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        finally:
            for fd in held:
                os.close(fd)
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


if __name__ == '__main__':
    unittest.main()
