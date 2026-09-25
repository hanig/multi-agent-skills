"""Exercise the typed fixture contract through real processes and unittest."""
import ast
import contextlib
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading
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

    def _launch(self, scope, source, python_options=()):
        script = scope.root / ('child-%d.py' % len(scope.children))
        script.write_text(source)
        return scope.launch(FixtureSpec((sys.executable, *python_options, str(script))))

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
                    def contain_escape():
                        if pidfile.exists():
                            kill_group(int(pidfile.read_text()))
                    case.addCleanup(contain_escape)
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
        foreign = replace(proc._anchor, sid=proc.supervisor_pid + 1000000)
        with mock.patch.object(fixtures, '_session_rows', return_value={proc.supervisor_pid: foreign}), mock.patch.object(
                fixtures.os, 'kill') as kill:
            result = proc._perform_cleanup()
        self.assertEqual(result.state, CleanupState.CLEAN)
        kill.assert_not_called()

    def test_post_kill_observation_does_not_reuse_old_pid_identity(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass\n')
        self.assertEqual(proc.join(10).returncode, 0)
        foreign = replace(proc._anchor, sid=proc.supervisor_pid + 1000000)
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
                        fixtures, '_pause'), mock.patch.object(proc, '_signal_group'), mock.patch.object(proc, '_signal_owned'), mock.patch.object(
                        proc, '_reap'):
                    result = proc._perform_cleanup()
                if survives:
                    self.assertEqual(result.state, CleanupState.INDETERMINATE)
                else:
                    self.assertEqual(result.state, CleanupState.CLEAN)
                self.assertEqual(snapshots[:3], [0, 0, 9])
                # Synthetic reap did not release the real anchor. The outer
                # scope still has to contain it through the real cleanup path.

    def test_detached_descendant_has_independent_teardown_after_parent_reap(self):
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


class TestFixtureQuiescence(unittest.TestCase):
    _scope = TestFixtureProcessGuard._scope
    _launch = TestFixtureProcessGuard._launch

    @contextlib.contextmanager
    def _no_fixture_signals(self, proc):
        real_kill = os.kill

        def checked_kill(pid, signum):
            # Census timeout cleanup may kill its own positive-PID ps child.
            # No group/broadcast signal is needed by that helper, and every
            # fixture member (including later descendants) has the private SID.
            self.assertGreater(pid, 0, 'wait signalled a process group')
            self.assertNotIn(pid, (proc.supervisor_pid, proc.child_pid),
                             'wait signalled a fixture PID')
            try:
                sid = os.getsid(pid)
            except ProcessLookupError:
                return real_kill(pid, signum)
            self.assertNotEqual(sid, proc.supervisor_pid,
                                'wait signalled a fixture session member')
            return real_kill(pid, signum)

        with mock.patch.object(fixtures.os, 'kill', side_effect=checked_kill) as sent, mock.patch.object(
                fixtures.os, 'killpg', side_effect=AssertionError('wait signalled a process group')):
            yield sent

    def _delayed_descendant(self, scope, new_group=False):
        release, effect = scope.root / 'release', scope.root / 'effect'
        proc = self._launch(scope,
            'import os, time\nfrom pathlib import Path\n'
            'if os.fork(): os._exit(23)\n' +
            ('os.setpgid(0, 0)\n' if new_group else '') +
            'while not Path(%r).exists(): time.sleep(0.01)\n'
            'time.sleep(0.15)\n'
            'Path(%r).write_text("finished")\n'
            'os.write(1, b"late stdout\\n")\n'
            'os.write(2, b"late stderr\\n")\n' % (str(release), str(effect)),
            # Python 3.12 can warn here when its runtime has started threads.
            # Keep the captured bytes about the delayed write, not warnings.
            python_options=('-W', 'ignore::DeprecationWarning'))
        return proc, release, effect

    def test_join_precedes_descendant_effect_but_quiescence_observes_it(self):
        for new_group in (False, True):
            with self.subTest(new_group=new_group):
                scope = self._scope()
                proc, release, effect = self._delayed_descendant(scope, new_group)
                joined = proc.join(10)
                self.assertEqual(joined.state, JoinState.EXITED, joined)
                self.assertEqual(joined.returncode, 23)
                self.assertFalse(effect.exists(), 'join alone must not release the barrier')
                self.assertEqual(joined.stdout, '')
                self.assertEqual(joined.stderr, '')
                release.touch()
                with self._no_fixture_signals(proc):
                    quiet = proc.wait_quiescent(10)
                self.assertEqual(quiet.state, fixtures.QuiescenceState.QUIESCENT, quiet)
                self.assertEqual(effect.read_text(), 'finished')
                self.assertEqual(quiet.stdout, 'late stdout\n')
                self.assertEqual(quiet.stderr, 'late stderr\n')
                self.assertIs(proc.join(0), joined, 'quiescence must not rewrite child status')
                self.assertIsNone(proc._cleaned, 'waiting must not perform cleanup')
                self.assertFalse(proc._reaped, 'retain the session anchor for cleanup')

    def test_timeout_is_not_success_and_can_be_retried_without_signalling(self):
        scope = self._scope()
        proc, release, effect = self._delayed_descendant(scope)
        self.assertEqual(proc.join(10).state, JoinState.EXITED)
        with self._no_fixture_signals(proc):
            quiet = proc.wait_quiescent(0.05)
            self.assertEqual(quiet.state, fixtures.QuiescenceState.TIMED_OUT, quiet)
            self.assertFalse(effect.exists())
            release.touch()
            self.assertEqual(proc.wait_quiescent(10).state, fixtures.QuiescenceState.QUIESCENT)
        self.assertEqual(effect.read_text(), 'finished')

    def test_census_timeout_can_kill_its_helper_without_signalling_fixtures(self):
        scope = self._scope()
        proc, release, effect = self._delayed_descendant(scope)
        self.assertEqual(proc.join(10).state, JoinState.EXITED)
        real_popen = subprocess.Popen
        helpers = []

        def slow_census(command, *args, **kwargs):
            self.assertEqual(command, [fixtures._ps_command(), '-U',
                                      str(os.geteuid()), '-o', 'pid=,pgid='])
            # Exercise check_output's real timeout, kill and reap path with
            # a slow helper in the caller's session, just like ps itself.
            helper = real_popen(
                [sys.executable, '-c', 'import time; time.sleep(1)'],
                *args, **kwargs)
            helpers.append(helper)
            return helper

        try:
            with self._no_fixture_signals(proc) as sent:
                with mock.patch.object(fixtures.subprocess, 'Popen', slow_census):
                    quiet = proc.wait_quiescent(0.05)
                self.assertEqual(quiet.state, fixtures.QuiescenceState.TIMED_OUT, quiet)
                self.assertEqual(len(helpers), 1)
                sent.assert_any_call(helpers[0].pid, signal.SIGKILL)
                self.assertEqual(helpers[0].returncode, -signal.SIGKILL)
                self.assertFalse(effect.exists())
                release.touch()
                self.assertEqual(proc.wait_quiescent(10).state,
                                 fixtures.QuiescenceState.QUIESCENT)
            self.assertEqual(effect.read_text(), 'finished')
        finally:
            # Independent containment also runs when the signal guard mutates.
            for helper in helpers:
                if helper.poll() is None:
                    helper.kill()
                helper.wait(timeout=5)

    def test_project_trap_waits_for_delayed_descendant_cleanup(self):
        import test_project as project
        original_read = Path.read_text
        injected = []

        def delayed_trap(path, *args, **kwargs):
            text = original_read(path, *args, **kwargs)
            if path == project.ROOT / 'README.md':
                anchor = '  trap - EXIT HUP INT TERM\n'
                self.assertEqual(text.count(anchor), 1)
                injected.append(1)
                # Delay after saving $? and disabling traps: every documented
                # cleanup operation and return status remains unchanged.
                return text.replace(anchor, anchor + '  sleep 0.3\n')
            return text

        case = project.TestVendoredAgentBusLayoutIsExplicit(
            'test_the_documented_trap_cleans_failure_and_interruption')
        result = unittest.TestResult()
        with mock.patch.object(Path, 'read_text', delayed_trap):
            case.run(result)
        self.assertEqual(injected, [1])
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_quiescence_includes_the_running_direct_child(self):
        scope = self._scope()
        release = scope.root / 'release'
        proc = self._launch(scope,
            'import time\nfrom pathlib import Path\n'
            'print("ready", flush=True)\n'
            'while not Path(%r).exists(): time.sleep(0.01)\n' % str(release))
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        self.assertEqual(proc.wait_quiescent(0.05).state, fixtures.QuiescenceState.TIMED_OUT)
        release.touch()
        self.assertEqual(proc.wait_quiescent(10).state, fixtures.QuiescenceState.QUIESCENT)
        self.assertEqual(proc.join(10).returncode, 0)

    def test_project_interrupt_waits_for_bus_signal_readiness(self):
        import test_project as project
        original_script = fixtures.FixtureProcesses.shell_script
        original_signal = os.killpg
        roots, observed = [], []

        def delay_start(scope, source):
            anchor = "trap 'exit 143' HUP INT TERM\n"
            if anchor in source:
                roots.append(scope.root)
                source = source.replace(anchor, 'sleep 0.2\n' + anchor)
            return original_script(scope, source)

        def check_readiness(pgid, signum):
            if signum == signal.SIGTERM and not observed:
                observed.append(bool(roots) and (roots[0] / 'bus-ready').exists())
                self.assertTrue(observed[0], 'TERM sent before bus signal readiness')
            return original_signal(pgid, signum)

        case = project.TestVendoredAgentBusLayoutIsExplicit(
            'test_the_documented_trap_cleans_failure_and_interruption')
        result = unittest.TestResult()
        with mock.patch.object(fixtures.FixtureProcesses, 'shell_script', delay_start), mock.patch.object(
                project.os, 'killpg', check_readiness):
            case.run(result)
        self.assertEqual(len(roots), 1)
        self.assertEqual(observed, [True], result.failures + result.errors)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_quiescence_inspection_failures_are_typed_and_never_signal(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass')
        self.assertEqual(proc.join(10).state, JoinState.EXITED)
        cases = (
            ('', fixtures.QuiescenceState.INDETERMINATE),
            ('malformed census\n', fixtures.QuiescenceState.INDETERMINATE),
            (PermissionError('census denied'), fixtures.QuiescenceState.ERROR),
            (subprocess.TimeoutExpired('ps', 1), fixtures.QuiescenceState.TIMED_OUT),
        )
        for observation, expected in cases:
            with self.subTest(observation=observation):
                result = ({'side_effect': observation} if isinstance(observation, Exception)
                          else {'return_value': observation})
                with mock.patch.object(fixtures.subprocess, 'check_output', **result), mock.patch.object(
                        fixtures.os, 'kill', side_effect=AssertionError('wait signalled')), mock.patch.object(
                        fixtures.os, 'killpg', side_effect=AssertionError('wait signalled')):
                    quiet = proc.wait_quiescent(1)
                self.assertEqual(quiet.state, expected, quiet)
                self.assertTrue(quiet.detail)
                self.assertIsNone(proc._cleaned)
                self.assertFalse(proc._reaped)
        self.assertEqual(proc.wait_quiescent(10).state, fixtures.QuiescenceState.QUIESCENT)

    def test_censuses_share_one_deadline_and_late_absence_is_not_success(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass')
        self.assertEqual(proc.join(10).state, JoinState.EXITED)
        clock, budgets = [0.0], []
        output = '%d %d\n%d %d\n' % (
            os.getpid(), os.getpgrp(), proc.supervisor_pid, proc.supervisor_pid)

        def census(*args, **kwargs):
            budgets.append(kwargs['timeout'])
            clock[0] += 1.0
            return output

        def pause(seconds):
            clock[0] += seconds

        with mock.patch.object(fixtures.time, 'monotonic', side_effect=lambda: clock[0]), mock.patch.object(
                fixtures.subprocess, 'check_output', census), mock.patch.object(fixtures, '_pause', pause):
            quiet = proc.wait_quiescent(1.5)
        self.assertEqual(quiet.state, fixtures.QuiescenceState.TIMED_OUT, quiet)
        self.assertEqual(len(budgets), 2)
        self.assertAlmostEqual(budgets[0], 1.5)
        self.assertAlmostEqual(budgets[1], 0.48)

    def test_invalid_quiescence_timeouts_do_not_start_an_observation(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass')
        self.assertEqual(proc.join(10).state, JoinState.EXITED)
        with mock.patch.object(proc, '_snapshot') as scan:
            for timeout in (None, True, -1, float('inf'), float('nan'), '1'):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    proc.wait_quiescent(timeout)
            self.assertEqual(proc.wait_quiescent(0).state, fixtures.QuiescenceState.TIMED_OUT)
            scan.assert_not_called()


class TestTypedFixtureContract(unittest.TestCase):
    _scope = TestFixtureProcessGuard._scope
    _launch = TestFixtureProcessGuard._launch

    def test_unencodable_strings_refuse_before_files_pipes_sessions_or_ledger(self):
        def open_descriptors():
            result = set()
            for name in os.listdir('/dev/fd'):
                fd = int(name)
                try:
                    os.fstat(fd)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise
                else:
                    result.add(fd)
            return result

        bad = '\ud800'
        cases = (
            ('command', (bad,)),
            ('command', (sys.executable, '-c', 'pass', bad)),
            ('environment', ((bad, 'value'),)),
            ('environment', (('KEY', bad),)),
            ('directory', '/' + bad),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                scope = self._scope()
                spec = FixtureSpec((sys.executable, '-c', 'pass'))
                # Launch revalidates even a frozen spec changed after creation.
                object.__setattr__(spec, field, value)
                before = open_descriptors()
                with mock.patch.object(fixtures, '_private_pipe', wraps=fixtures._private_pipe) as pipe, mock.patch.object(
                        fixtures.subprocess, 'Popen', wraps=subprocess.Popen) as spawn, mock.patch.object(
                        fixtures, '_identity', wraps=fixtures._identity) as identity:
                    with self.assertRaises(ValueError):
                        scope.launch(spec)
                self.assertEqual(open_descriptors(), before, 'refused launch leaked descriptors')
                self.assertEqual(list(scope.root.iterdir()), [], 'refused launch created files')
                self.assertEqual(scope.children, [])
                self.assertFalse(scope.ledger_path.exists())
                pipe.assert_not_called()
                spawn.assert_not_called()
                identity.assert_not_called()
                with self.assertRaises(UnicodeEncodeError):
                    FixtureSpec(**dict(vars(spec)))

    def test_encodable_unicode_and_surrogateescape_strings_still_launch(self):
        scope = self._scope()
        directory = scope.root / 'caf\u00e9'
        directory.mkdir()
        value = 'caf\u00e9\n' + os.fsdecode(b'\xff')
        proc = scope.launch(FixtureSpec(
            (sys.executable, '-c',
             'import json,os,sys; print(json.dumps([sys.argv[1],os.environ["KEY"]]))', value),
            environment=(('KEY', value),), directory=str(directory)))
        joined = proc.join(10)
        self.assertEqual(joined.state, JoinState.EXITED, joined)
        self.assertEqual(joined.returncode, 0, joined)
        self.assertEqual(json.loads(joined.stdout), [value, value])
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)

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
            {'command': ('x',), 'environment': (('invalid=key', 'wrong'),)},
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
        # This test retains its deliberately escaped PID out of band.
        self.addCleanup(kill_group, os.getpgid(pid))
        return proc, pid

    def test_non_session_parent_exit_does_not_release_its_descendants(self):
        scope = self._scope()
        proc, pid = self._orphan(scope, 'ordinary')
        self.assertNotEqual(proc.child_pid, proc.supervisor_pid)
        self.assertEqual(os.getsid(pid), proc.supervisor_pid)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        self.assertNotIn(pid, process_table())
        self.assertEqual(proc.join(0).returncode, 0)

    def test_additional_groups_are_found_and_double_fork_sessions_are_outside(self):
        for mode in ('group', 'double-fork-session'):
            with self.subTest(mode=mode):
                scope = self._scope()
                proc, pid = self._orphan(scope, mode)
                pgid = os.getpgid(pid)
                self.assertNotEqual(pgid, proc.child_pid)
                targets, _stable = proc._survivor_scan()
                ledger = [json.loads(line) for line in scope.ledger_path.read_text().splitlines()]
                if mode == 'double-fork-session':
                    self.assertNotEqual(os.getsid(pid), proc.supervisor_pid)
                    self.assertNotIn(pid, targets)
                    self.assertNotIn({'sid': proc.supervisor_pid, 'pgid': pgid}, ledger)
                    self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
                    os.kill(pid, 0)  # Deliberate escape really survived CLEAN.
                    self.assertIn(pid, process_table())
                else:
                    self.assertIn(pid, targets)
                    self.assertIn({'sid': proc.supervisor_pid, 'pgid': pgid}, ledger)
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

    def test_injected_scan_signal_and_reap_failures_still_contain_and_retry(self):
        for failure in ('scan', 'signal', 'reap'):
            with self.subTest(failure=failure):
                scope = self._scope()
                proc = self._launch(scope, 'import time; print("ready",flush=True); time.sleep(600)')
                wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
                target, name = {
                    'scan': (proc, '_survivor_scan'),
                    'signal': (proc, '_signal_group'),
                    'reap': (proc, '_reap'),
                }[failure]
                original = getattr(target, name)
                calls = []
                def fail_once(*args, **kwargs):
                    calls.append(1)
                    if len(calls) == 1:
                        raise OSError('injected ' + failure)
                    return original(*args, **kwargs)
                with mock.patch.object(target, name, fail_once):
                    result = proc.cleanup()
                self.assertEqual(result.state, CleanupState.ERROR, result)
                self.assertIn('injected ' + failure, result.detail)
                # Observe before calling cleanup again: emergency containment
                # must already have run despite caching the original ERROR.
                wait_for(lambda: not fixtures._session_rows(proc.supervisor_pid, set()))
                self.assertNotIn(proc.child_pid, process_table())
                self.assertTrue(proc._reaped)
                with mock.patch.object(proc, '_perform_cleanup', wraps=proc._perform_cleanup) as retry:
                    self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
                    retry.assert_called_once_with()
                self.assertIs(proc.cleanup(), proc.cleanup())

    def test_ambiguous_identity_prevents_clean_without_a_signal(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass\n')
        self.assertEqual(proc.join(10).returncode, 0)
        with mock.patch.object(proc, '_snapshot', side_effect=fixtures._Indeterminate('ambiguous')):
            result = proc._perform_cleanup()
        self.assertEqual(result.state, CleanupState.INDETERMINATE)

    def test_captured_output_uses_utf8_replacement_in_a_non_utf8_locale(self):
        driver = (
            'import json,locale,sys,tempfile,unittest\n'
            'sys.path.insert(0,%r)\n'
            'from fixture_processes import FixtureProcesses,FixtureSpec,CleanupState\n'
            'with tempfile.TemporaryDirectory() as root:\n'
            '    scope=FixtureProcesses(unittest.TestCase(),root)\n'
            '    proc=scope.launch(FixtureSpec((sys.executable,"-c",'
            '"import os; os.write(1,bytes([195,169,128])); os.write(2,bytes([195,169,128]))")))\n'
            '    try:\n'
            '        joined=proc.join(10)\n'
            '        print(json.dumps(dict(state=joined.state.value,code=joined.returncode,'
            'stdout=joined.stdout,stderr=joined.stderr,encoding=locale.getpreferredencoding(False),'
            'utf8_mode=sys.flags.utf8_mode)))\n'
            '    finally:\n'
            '        assert proc.cleanup().state is CleanupState.CLEAN\n'
        ) % str(Path(fixtures.__file__).resolve().parent)
        env = dict(os.environ, LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0')
        run = subprocess.run([sys.executable, '-c', driver], env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        answer = json.loads(run.stdout)
        self.assertEqual(answer['utf8_mode'], 0)
        self.assertNotIn(answer['encoding'].lower(), ('utf-8', 'utf8'))
        self.assertEqual(answer['state'], 'EXITED')
        self.assertEqual(answer['code'], 0)
        self.assertEqual(answer['stdout'], 'é�')
        self.assertEqual(answer['stderr'], 'é�')

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

    def test_unsupported_platform_and_missing_membership_capability_refuse_before_launch(self):
        scope = self._scope()
        spec = FixtureSpec((sys.executable, '-c', 'pass'))
        with mock.patch.object(fixtures.sys, 'platform', 'unsupported'), mock.patch.object(
                fixtures.subprocess, 'Popen') as launch:
            with self.assertRaises(fixtures.FixtureRefused):
                scope.launch(spec)
            launch.assert_not_called()
        with mock.patch.object(fixtures, '_identity', side_effect=OSError('membership unavailable')), mock.patch.object(
                fixtures.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(OSError, 'membership unavailable'):
                scope.launch(spec)
            launch.assert_not_called()

    def test_external_directory_and_second_launch_are_refused(self):
        scope = self._scope()
        with tempfile.TemporaryDirectory() as elsewhere, mock.patch.object(
                fixtures.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(fixtures.FixtureRefused, 'beneath its root'):
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
        with mock.patch.object(fixtures, '_session_rows', return_value={}):
            self.assertEqual(proc._perform_cleanup().state, CleanupState.INDETERMINATE)

    def test_injected_membership_permission_failure_prevents_clean(self):
        scope = self._scope()
        proc, pid = self._orphan(scope, 'group')
        with mock.patch.object(fixtures, '_session_rows', side_effect=PermissionError('injected membership inspection')):
            result = proc._perform_cleanup()
        self.assertEqual(result.state, CleanupState.ERROR)
        self.assertIn('injected membership inspection', result.detail)
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


class TestSessionContainment(unittest.TestCase):
    _scope = TestFixtureProcessGuard._scope
    _launch = TestFixtureProcessGuard._launch

    def _diagnostic_output(self, output):
        reader = mock.MagicMock()
        reader.__enter__.return_value = reader
        reader.returncode, reader.pid = 0, 1000000001
        reader.communicate.return_value = (output, '')
        with mock.patch.object(fixtures.subprocess, 'Popen', return_value=reader):
            return process_table()

    def test_multiline_unrelated_command_preserves_real_fixture_member(self):
        scope = self._scope()
        proc = self._launch(scope, 'import time; print("ready",flush=True); time.sleep(600)')
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        caller = '%d %d %d %d S caller\n' % (
            os.getpid(), os.getppid(), os.getpgrp(), os.geteuid())
        foreign = '1000000000 1 1000000000 %d S unrelated\nargument continuation\n' % os.geteuid()
        member = '%d %d %d %d S %s\n' % (
            proc.child_pid, proc.supervisor_pid, proc.child_pid, os.geteuid(),
            ' '.join(proc.spec.command))
        for tail in (foreign + member, member + foreign):
            with self.subTest(tail=tail):
                rows = self._diagnostic_output(caller + tail)
                self.assertEqual(rows[1000000000][3], 'unrelated\nargument continuation')
                self.assertEqual(rows[proc.child_pid][0:2],
                                 (proc.supervisor_pid, proc.child_pid))
                self.assertIn(proc.child_pid, scope._root_rows(rows))
                self.assertIn(proc.child_pid, proc._snapshot())

    def test_multiline_fixture_command_and_blank_continuations_are_retained(self):
        caller = '%d %d %d %d S caller\n' % (
            os.getpid(), os.getppid(), os.getpgrp(), os.geteuid())
        for continuation in ('\ncontinued\rargument\vtext', '-x', '+option', '-', '+'):
            with self.subTest(continuation=continuation):
                command = 'fixture\n' + continuation
                output = caller + '1000000000 1 1000000000 %d S %s\n' % (os.geteuid(), command)
                self.assertEqual(self._diagnostic_output(output)[1000000000][3], command)

    def test_ambiguous_diagnostic_rows_refuse_instead_of_dropping_members(self):
        caller = '%d %d %d %d S caller\n' % (
            os.getpid(), os.getppid(), os.getpgrp(), os.geteuid())
        bad = (
            '123 broken\n', '123x broken\n', '-1 1 1 501 S broken\n',
            '+123 broken\n', '-123 broken\n', '0 1 1 501 S broken\n',
            '123 bad-parent 123 501 S broken\n', caller,
        )
        for row in bad:
            with self.subTest(row=row), self.assertRaises(fixtures._Indeterminate):
                self._diagnostic_output(caller + row)
        for output in ('', 'orphan continuation\n' + caller,
                       '1000000000 1 1000000000 %d S no-caller\n' % os.geteuid()):
            with self.subTest(output=output), self.assertRaises(fixtures._Indeterminate):
                self._diagnostic_output(output)

    def test_path_resolvable_ps_serves_both_readers_when_bin_ps_is_absent(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass')
        self.assertEqual(proc.join(10).returncode, 0)
        real_ps = shutil.which('ps')
        self.assertIsNotNone(real_ps)
        alias = scope.root / 'ps'
        alias.symlink_to(real_ps)
        real_access, real_popen = os.access, subprocess.Popen
        launched = []

        def missing_bin_access(path, mode, *args, **kwargs):
            return False if path == '/bin/ps' else real_access(path, mode, *args, **kwargs)

        def launch(command, *args, **kwargs):
            if command[0] == '/bin/ps':
                raise FileNotFoundError(errno.ENOENT, 'no /bin/ps on this host')
            launched.append(command[0])
            return real_popen(command, *args, **kwargs)

        with mock.patch.dict(os.environ, {'PATH': str(scope.root)}), mock.patch.object(
                fixtures.os, 'access', side_effect=missing_bin_access), mock.patch.object(
                fixtures.subprocess, 'Popen', side_effect=launch):
            with self.subTest(reader='session'):
                self.assertIn(proc.supervisor_pid,
                              fixtures._session_rows(proc.supervisor_pid, set()))
            with self.subTest(reader='diagnostic'):
                self.assertIn(os.getpid(), process_table())
            self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        self.assertTrue(launched)
        self.assertEqual(set(launched), {str(alias)})

    def _foreign(self, cwd=None, new_session=True):
        proc = subprocess.Popen(
            [sys.executable, '-c', 'import time; print("ready",flush=True); time.sleep(600)'],
            cwd=cwd, start_new_session=new_session, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        def contain():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close()
        self.addCleanup(contain)
        self.assertTrue(wait_readable(proc.stdout, 10))
        self.assertEqual(proc.stdout.readline(), b'ready\n')
        return proc

    def test_denied_sid_in_live_callers_group_is_excluded_without_signalling(self):
        scope = self._scope()
        foreign = self._foreign(new_session=False)
        self.assertEqual(os.getpgid(foreign.pid), os.getpgrp())
        proc = self._launch(scope, 'import time; print("ready",flush=True); time.sleep(600)')
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        real_sid = os.getsid

        def denied(pid):
            if pid == foreign.pid:
                raise PermissionError(errno.EPERM, 'unrelated SID denied')
            return real_sid(pid)

        with mock.patch.object(fixtures.os, 'getsid', denied), mock.patch.object(
                fixtures.os, 'kill', wraps=os.kill) as kill, mock.patch.object(
                fixtures.os, 'killpg', wraps=os.killpg) as killpg:
            rows = proc._snapshot()
            self.assertIn(proc.child_pid, rows)
            self.assertNotIn(foreign.pid, rows)
            result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.CLEAN, result)
        self.assertIsNone(foreign.poll())
        self.assertNotIn(foreign.pid, [call.args[0] for call in kill.call_args_list])
        self.assertNotIn(os.getpgrp(), [call.args[0] for call in killpg.call_args_list])

    def test_denied_fixture_anchor_child_and_unrecorded_group_never_disappear(self):
        for member in ('anchor', 'child', 'unrecorded-group'):
            with self.subTest(member=member):
                scope = self._scope()
                ready = scope.root / 'member.pid'
                proc = self._launch(scope,
                    'import os,time\n'
                    'if os.fork():\n'
                    '    while True: time.sleep(600)\n'
                    'os.setpgid(0,0)\n'
                    'with open(%r,"w") as f: f.write(str(os.getpid()))\n'
                    'while True: time.sleep(600)\n' % str(ready))
                wait_for(lambda: ready.exists() and bool(ready.read_text()))
                extra = int(ready.read_text())
                self.assertNotIn((proc.supervisor_pid, extra), proc._groups)
                ledger_before = scope.ledger_path.read_bytes()
                denied_pid = {'anchor': proc.supervisor_pid, 'child': proc.child_pid,
                              'unrecorded-group': extra}[member]
                real_sid = os.getsid

                def denied(pid):
                    if pid == denied_pid:
                        raise PermissionError(errno.EPERM, 'fixture SID denied')
                    return real_sid(pid)

                with mock.patch.object(fixtures.os, 'getsid', denied):
                    with self.assertRaises(PermissionError):
                        proc._snapshot()
                    with self.assertRaises(PermissionError):
                        fixtures._identity(denied_pid)
                    self.assertEqual(proc.wait_quiescent(1).state, fixtures.QuiescenceState.ERROR)
                    # A sustained denied scan must not certify CLEAN; prevent
                    # emergency reap delay here, then exercise real teardown
                    # after restoring permission below.
                    with mock.patch.object(proc, '_emergency_containment') as emergency:
                        result = proc.cleanup()
                    self.assertEqual(result.state, CleanupState.ERROR, result)
                    emergency.assert_called_once_with()
                self.assertEqual(scope.ledger_path.read_bytes(), ledger_before)
                self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
                self.assertNotIn(extra, fixtures._session_rows(proc.supervisor_pid, set()))

    def test_denied_unrelated_group_and_ledger_collision_remain_errors(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass')
        self.assertEqual(proc.join(10).returncode, 0)
        real_sid = os.getsid
        for new_session in (True, False):
            with self.subTest(new_session=new_session):
                foreign = self._foreign(new_session=new_session)
                group = (proc.supervisor_pid, os.getpgid(foreign.pid))
                groups = set() if new_session else {group}

                def denied(pid):
                    if pid == foreign.pid:
                        raise PermissionError(errno.EPERM, 'unproven exclusion')
                    return real_sid(pid)

                with mock.patch.object(fixtures.os, 'getsid', denied):
                    with self.assertRaises(PermissionError):
                        fixtures._session_rows(proc.supervisor_pid, groups)
                self.assertIsNone(foreign.poll())

    def test_denied_row_with_stale_caller_group_cannot_be_excluded(self):
        scope = self._scope()
        proc = self._launch(scope, 'import time; time.sleep(600)')
        real_sid, real_group = os.getsid, os.getpgid
        output = '%d %d\n%d %d\n%d %d\n' % (
            os.getpid(), os.getpgrp(), proc.supervisor_pid, proc.supervisor_pid,
            proc.child_pid, os.getpgrp())

        def denied(pid):
            if pid == proc.child_pid:
                raise PermissionError(errno.EPERM, 'fixture SID denied')
            return real_sid(pid)

        with mock.patch.object(fixtures.subprocess, 'check_output', return_value=output), mock.patch.object(
                fixtures.os, 'getsid', denied):
            with self.assertRaises(PermissionError):
                fixtures._session_rows(proc.supervisor_pid, set())
        self.assertEqual(real_group(proc.child_pid), proc.child_pid)

    def test_unrelated_churn_deleted_cwd_and_denied_metadata_do_not_affect_clean(self):
        scope = self._scope()
        deleted = scope.root / 'unrelated-deleted-cwd'
        deleted.mkdir()
        foreign = self._foreign(cwd=deleted)
        deleted.rmdir()  # A real same-uid neighbour retains a deleted cwd.
        proc = self._launch(scope, 'import time; print("ready",flush=True); time.sleep(600)')
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        stop, running = threading.Event(), threading.Event()
        iterations, failures = [], []
        def churn():
            try:
                while not stop.is_set():
                    subprocess.run([sys.executable, '-c', 'pass'], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    iterations.append(1)
                    if len(iterations) >= 2:
                        running.set()
            except BaseException as error:
                failures.append(error)
                running.set()
        worker = threading.Thread(target=churn)
        worker.start()
        try:
            self.assertTrue(running.wait(10))
            # Runtime tripwires, including the old marker entry points: this
            # test fails if a whole-population inspection is restored.
            import ctypes
            with mock.patch.object(fixtures, '_environment', create=True,
                                   side_effect=PermissionError('unrelated environment denied')) as env, mock.patch.object(
                    fixtures, '_cwd_marker', create=True,
                    side_effect=FileNotFoundError('unrelated cwd deleted')) as cwd, mock.patch.object(
                    ctypes, 'CDLL', side_effect=AssertionError('process metadata API invoked')) as metadata:
                result = proc.cleanup()
                self.assertEqual(result.state, CleanupState.CLEAN, result)
                env.assert_not_called()
                cwd.assert_not_called()
                metadata.assert_not_called()
            self.assertIsNone(foreign.poll())
            self.assertGreaterEqual(len(iterations), 2)
            self.assertEqual(failures, [])
        finally:
            stop.set()
            worker.join(10)
            self.assertFalse(worker.is_alive())

    def test_only_scoped_rows_participate_in_snapshot_stability(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass')
        self.assertEqual(proc.join(10).returncode, 0)
        original = fixtures._session_rows
        serial = [1000000]
        def unrelated_churn(sid, groups):
            rows = original(sid, groups)
            serial[0] += 1
            rows[serial[0]] = fixtures._Identity(serial[0], serial[0])
            return rows
        with mock.patch.object(fixtures, '_session_rows', unrelated_churn):
            result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.CLEAN, result)

    def test_multigeneration_groups_require_term_then_kill_and_disappear(self):
        scope = self._scope()
        ready = scope.root / 'generations.pid'
        proc = self._launch(scope,
            'import os, signal, time\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'for depth in range(4):\n'
            '    os.setpgid(0,0)\n'
            '    with open(%r,"a") as f: f.write(str(os.getpid())+"\\n")\n'
            '    if depth == 3 or os.fork():\n'
            '        while True: time.sleep(600)\n' % str(ready))
        wait_for(lambda: ready.exists() and len(ready.read_text().split()) == 4)
        pids = [int(word) for word in ready.read_text().split()]
        self.assertEqual(len({os.getpgid(pid) for pid in pids}), 4)
        self.assertEqual({os.getsid(pid) for pid in pids}, {proc.supervisor_pid})
        with mock.patch.object(fixtures.os, 'killpg', wraps=os.killpg) as signals:
            result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.CLEAN, result)
        delivered = {call.args[1] for call in signals.call_args_list}
        self.assertIn(signal.SIGTERM, delivered)
        self.assertIn(signal.SIGKILL, delivered)
        self.assertEqual(fixtures._session_rows(proc.supervisor_pid, set()), {})
        self.assertFalse(set(pids) & set(process_table()))
        ledger = [json.loads(line) for line in scope.ledger_path.read_text().splitlines()]
        self.assertTrue(all(row['sid'] == proc.supervisor_pid for row in ledger))
        self.assertTrue(set(pids) <= {row['pgid'] for row in ledger})

    def test_descendant_joining_the_retained_leader_group_is_contained(self):
        scope = self._scope()
        proc = self._launch(scope,
            'import os, signal, time\n'
            'os.setpgid(0,os.getsid(0))\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'print("ready",flush=True)\n'
            'time.sleep(600)\n')
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        self.assertEqual(os.getpgid(proc.child_pid), proc.supervisor_pid)
        result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.CLEAN, result)
        self.assertEqual(fixtures._session_rows(proc.supervisor_pid, set()), {})
        self.assertEqual(proc.join(0).state, JoinState.STATUS_UNAVAILABLE)

    def test_stale_ledger_group_in_another_session_is_never_signalled(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass')
        self.assertEqual(proc.join(10).returncode, 0)
        foreign = self._foreign()
        stale = (proc.supervisor_pid, foreign.pid)
        # Model persisted stale state too, not just a fresh forward-path group.
        proc._record(foreign.pid, fixtures._Identity(foreign.pid, proc.supervisor_pid))
        self.assertIn(stale, proc._groups)
        ledger = [json.loads(line) for line in scope.ledger_path.read_text().splitlines()]
        self.assertIn({'sid': stale[0], 'pgid': stale[1]}, ledger)
        with mock.patch.object(fixtures.os, 'killpg', wraps=os.killpg) as sent:
            proc._signal_group(stale, signal.SIGKILL)
            result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.CLEAN, result)
        self.assertNotIn(foreign.pid, [call.args[0] for call in sent.call_args_list])
        self.assertIsNone(foreign.poll())

    def test_scheduling_delay_before_first_scan_cannot_skip_term(self):
        scope = self._scope()
        proc = self._launch(scope,
            'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            'print("ready",flush=True); time.sleep(600)')
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        clock = time.monotonic
        first = [True]
        def deschedule_after_start():
            observed = clock()
            if first[0]:
                first[0] = False
                time.sleep(0.35)  # Pause after the cleanup start timestamp.
            return observed
        with mock.patch.object(fixtures.time, 'monotonic', deschedule_after_start), mock.patch.object(
                fixtures.os, 'killpg', wraps=os.killpg) as sent:
            result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.CLEAN, result)
        delivered = [call.args[1] for call in sent.call_args_list]
        self.assertTrue(delivered)
        self.assertEqual(delivered[0], signal.SIGTERM)
        self.assertIn(signal.SIGKILL, delivered)
        self.assertEqual(fixtures._session_rows(proc.supervisor_pid, set()), {})

    def test_failed_cache_cannot_skip_emergency_containment_or_retry(self):
        scope = self._scope()
        proc = self._launch(scope, 'import time; print("ready",flush=True); time.sleep(600)')
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        # Populate a prior failed verdict while the fixture really is alive.
        # F08 skipped every future cleanup when this cache entry existed.
        proc._cleaned = fixtures.CleanupResult(CleanupState.ERROR, 'prior failure')
        with mock.patch.object(proc, '_survivor_scan', side_effect=OSError('scan failure')):
            result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.ERROR)
        self.assertIn('scan failure', result.detail)
        wait_for(lambda: not fixtures._session_rows(proc.supervisor_pid, set()))
        self.assertTrue(proc._reaped)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)

    def test_survivor_guard_retries_a_contradicted_clean_cache_through_unittest(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        class Probe(unittest.TestCase):
            def runTest(case):
                pass

        case = Probe()
        scope = FixtureProcesses(case, directory.name)
        proc, pid = TestTypedFixtureContract._orphan(self, scope, 'ordinary')
        # Independent containment if the guard regresses. Keep the directory
        # until it finishes so the test does not erase its own teardown data.
        self.addCleanup(proc._perform_cleanup)
        with mock.patch.object(proc, '_survivor_scan', side_effect=[
                ({proc.supervisor_pid: proc._anchor}, True), ({}, True)]):
            self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        # Simulate a census miss, then consume the guard through unittest's
        # registered cleanups with real scans and the real live descendant.
        self.assertIn(pid, fixtures._session_rows(proc.supervisor_pid, set()))
        result = unittest.TestResult()
        with mock.patch.object(proc, '_perform_cleanup', wraps=proc._perform_cleanup) as retry:
            case.run(result)
        self.assertEqual(len(result.failures), 1, result.failures)
        self.assertEqual(result.errors, [])
        self.assertIn('fixture processes survived cleanup', result.failures[0][1])
        self.assertEqual(fixtures._session_rows(proc.supervisor_pid, set()), {})
        self.assertNotIn(pid, process_table())
        retry.assert_called_once_with()
        self.assertEqual(proc._cleaned.state, CleanupState.CLEAN)

    def test_cwd_validation_precedes_all_launch_side_effects(self):
        scope = self._scope()
        root = scope.root / 'root'
        root.mkdir()
        case = unittest.TestCase()
        scope = FixtureProcesses(case, root)
        outside = root.with_name('root2')
        outside.mkdir()
        link = root / 'outside-link'
        link.symlink_to(outside, target_is_directory=True)
        non_directory = root / 'file'
        non_directory.write_text('not a directory')
        bad = (link, outside, root / '..' / 'root2', root / 'missing', non_directory)
        before = sorted(root.iterdir())
        for requested in bad:
            with self.subTest(cwd=requested), mock.patch.object(fixtures, '_private_pipe') as pipe, mock.patch.object(
                    fixtures.subprocess, 'Popen') as spawn, mock.patch.object(fixtures.os, 'setsid') as session:
                with self.assertRaises((fixtures.FixtureRefused, FileNotFoundError)):
                    scope.launch(FixtureSpec((sys.executable, '-c', 'pass'), directory=str(requested)))
                pipe.assert_not_called()
                spawn.assert_not_called()
                session.assert_not_called()
                self.assertEqual(scope.children, [])
                self.assertFalse(scope.ledger_path.exists())
                self.assertEqual(sorted(root.iterdir()), before)
        canonical = root / 'inside'
        canonical.mkdir()
        alias = root / 'inside-link'
        alias.symlink_to(canonical, target_is_directory=True)
        proc = scope.launch(FixtureSpec(
            (sys.executable, '-c', 'import os; print(os.getcwd())'), directory=str(alias)))
        self.addCleanup(scope.cleanup)
        status = proc.join(10)
        self.assertEqual(status.state, JoinState.EXITED, status)
        self.assertEqual(status.stdout.strip(), str(canonical.resolve()))
        config = json.loads((root / 'fixture-launch-0.json').read_text())
        self.assertEqual(config['directory'], str(canonical.resolve()))
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)

    def test_exit_race_at_group_signal_rescans_and_unavailable_status_is_honest(self):
        scope = self._scope()
        proc = self._launch(scope, 'import time; print("ready",flush=True); time.sleep(600)')
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        original = os.killpg
        calls = []
        def exit_at_signal(pgid, signum):
            calls.append((pgid, signum))
            original(pgid, signal.SIGKILL)
            raise ProcessLookupError(errno.ESRCH, 'exited before signal')
        with mock.patch.object(fixtures.os, 'killpg', exit_at_signal), mock.patch.object(
                proc, '_snapshot', wraps=proc._snapshot) as scan:
            proc._signal_group((proc.supervisor_pid, proc.child_pid), signal.SIGTERM)
            scan.assert_called_once_with()
        self.assertTrue(calls)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        scope = self._scope()
        proc = self._launch(scope, 'import time; time.sleep(600)')
        os.kill(proc.supervisor_pid, signal.SIGKILL)
        os.waitpid(proc.supervisor_pid, 0)  # Status has been consumed elsewhere.
        self.assertEqual(proc.join(10).state, JoinState.STATUS_UNAVAILABLE)
        self.assertIsNone(proc.join(0).returncode)
        result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.CLEAN, result)

    def test_departing_census_rows_are_absent_and_permission_errors_are_errors(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass')
        self.assertEqual(proc.join(10).returncode, 0)
        original_sid = os.getsid
        def lookup(pid):
            if pid == 1000000000:
                raise ProcessLookupError(errno.ESRCH, 'exiting row')
            return original_sid(pid)
        output = '%d %d\n%d %d\n1000000000 1000000000\n' % (
            proc.supervisor_pid, proc.supervisor_pid, os.getpid(), os.getpgrp())
        with mock.patch.object(fixtures.subprocess, 'check_output', return_value=output), mock.patch.object(
                fixtures.os, 'getsid', lookup):
            rows = fixtures._session_rows(proc.supervisor_pid, set())
        self.assertEqual(rows, {proc.supervisor_pid: proc._anchor})
        with mock.patch.object(fixtures.os, 'kill', side_effect=PermissionError(errno.EPERM, 'signal denied')):
            result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.ERROR, result)
        self.assertIn('signal denied', result.detail)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)

    def test_empty_census_after_external_leader_death_cannot_certify_absence(self):
        scope = self._scope()
        proc = self._launch(scope, 'import time; print("ready",flush=True); time.sleep(600)')
        wait_for(lambda: proc.stdout_path.read_text() == 'ready\n')
        os.kill(proc.supervisor_pid, signal.SIGKILL)
        self.assertEqual(proc.join(10).state, JoinState.STATUS_UNAVAILABLE)
        with mock.patch.object(fixtures.subprocess, 'check_output', return_value=''):
            result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.INDETERMINATE, result)
        self.assertIn('omitted its live caller', result.detail)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)
        self.assertNotIn(proc.child_pid, process_table())

    def test_malformed_scoped_census_is_not_absence(self):
        scope = self._scope()
        proc = self._launch(scope, 'pass')
        self.assertEqual(proc.join(10).returncode, 0)
        with mock.patch.object(fixtures.subprocess, 'check_output', return_value='invalid census\n'):
            result = proc.cleanup()
        self.assertEqual(result.state, CleanupState.INDETERMINATE, result)
        self.assertEqual(proc.cleanup().state, CleanupState.CLEAN)


if __name__ == '__main__':
    unittest.main()
