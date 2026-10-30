"""U4.9c named activation routes and advisory; no interpreter isolation claim."""
from contextlib import nullcontext
import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/hanig-orchestrate/scripts'))
sys.path.insert(0, str(ROOT / 'skills/hanig-swarm/scripts'))
import d5_activation as activation
import swarm as S
import merge_unit as M
from tests.fixtures.d5.u49c_capture import inactive_captures, MERGE_ARGV
from tests.fixtures.d5.ready import ready_installation, missing_installation, classification_only

BASE = '4a97999d999347cdc52a6496bbfa5392f0b22d88'
FIXTURES = ROOT / 'tests/fixtures/d5'


class InstallationFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        old = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        patch = mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'xdg')})
        patch.start()
        self.addCleanup(patch.stop)
        self.state = S.CP.pre_git_default_state()
        self.entry = ready_installation(self.root, state_dir=self.state)

    def activation_state(self, active):
        return nullcontext() if active else missing_installation(self.entry)


class TestRoutes(InstallationFixture):
    def test_cmd_run(self):
        for active in (False, True):
            with self.subTest(active=active), \
                    self.activation_state(active), \
                    mock.patch.object(sys, 'dont_write_bytecode', True), \
                    mock.patch.object(S.SR, 'apply_agent_resolution',
                                      side_effect=SystemExit('legacy preparation')) as legacy:
                with self.assertRaisesRegex(SystemExit, 'D5 active:.*cmd_run' if active else 'legacy preparation'):
                    S.cmd_run(argparse.Namespace(plan='fixture-plan.json'))
                self.assertEqual(legacy.call_count, 0 if active else 1)

    def test_cmd_advance(self):
        for active in (False, True):
            with self.subTest(active=active), \
                    self.activation_state(active), \
                    mock.patch.object(sys, 'dont_write_bytecode', True), \
                    mock.patch.object(S.SR, 'apply_agent_resolution',
                                      side_effect=SystemExit('legacy preparation')) as legacy, \
                    mock.patch.object(S, 'cmd_run', wraps=S.cmd_run) as run:
                args = argparse.Namespace(plan='fixture-plan.json')
                with self.assertRaisesRegex(SystemExit, 'D5 active:.*cmd_run' if active else 'legacy preparation'):
                    S.cmd_advance(args)
                run.assert_called_once_with(args)
                self.assertEqual(legacy.call_count, 0 if active else 1)
        with mock.patch.object(S, 'cmd_run', return_value=19) as run:
            self.assertEqual(S.cmd_advance(args), 19)
            run.assert_called_once_with(args)

    def test_swarm_main(self):
        for active in (False, True):
            with self.subTest(active=active), \
                    self.activation_state(active), \
                    mock.patch.object(sys, 'dont_write_bytecode', True), \
                    mock.patch.object(sys, 'argv', ['swarm.py', 'run', 'fixture-plan.json']), \
                    mock.patch.object(S, 'cmd_run', return_value=19) as run:
                with self.assertRaises(SystemExit) as stopped:
                    S.main()
                if active:
                    self.assertIn('D5 active:', str(stopped.exception))
                    run.assert_not_called()
                else:
                    self.assertEqual(stopped.exception.code, 19)
                    run.assert_called_once()

    def test_merge_main(self):
        # Isolate main's active branch from the redundant legacy-path guard.
        # The active lease's actual merge_launch_admitted refusal stays real.
        for active in (False, True):
            with self.subTest(active=active), ExitStack() as stack:
                stack.enter_context(self.activation_state(active))
                guard = stack.enter_context(mock.patch.object(M, '_d5_merge_entry'))
                read = stack.enter_context(mock.patch.object(M, 'read_object',
                    side_effect=M.Refusal('legacy preparation')))
                out = stack.enter_context(redirect_stderr(io.StringIO()))
                self.assertEqual(M.main(MERGE_ARGV[:2] + [str(self.state)] + MERGE_ARGV[3:]), 1)
                self.assertIn('merge launch is not admitted' if active else 'legacy preparation', out.getvalue())
                self.assertEqual(read.call_count, 0 if active else 1)
                self.assertEqual(guard.call_count, 0 if active else 1)

    def test_manifest_routes_and_shared_activation_owner(self):
        routes = json.loads((FIXTURES / 'integration-manifest.json').read_text())['u49c_entrypoints']
        self.assertEqual(routes['entries'], {
            'swarm.py': ['cmd_run', 'cmd_advance', 'main'], 'merge_unit.py': ['main']})
        self.assertEqual(routes['activation'], 'skills/hanig-swarm/scripts/d5_activation.py')
        self.assertEqual(routes['advisory_entries'], {'swarm.py': ['cmd_run', 'cmd_advance', 'main']})
        self.assertIs(S.D5, activation)
        self.assertIs(M.D5, activation)
        self.assertEqual(Path(activation.__file__).resolve(), ROOT / routes['activation'])
        self.assertIs(activation.d5_active(self.state), True)
        self.assertIs(M.merge_launch_admitted(), False)


class TestAdvisory(InstallationFixture):
    def schema_output(self, stderr):
        output = io.StringIO()
        with nullcontext(), \
                mock.patch.object(sys, 'dont_write_bytecode', False), \
                mock.patch.object(sys, 'argv', ['swarm.py', 'schema']), \
                redirect_stdout(output), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                S.main()
            self.assertEqual(stopped.exception.code, 0)
        return output.getvalue()

    def test_missing_stderr_preserves_schema_stdout(self):
        expected = self.schema_output(io.StringIO())
        self.assertEqual(self.schema_output(None), expected)

    def test_unexpected_stderr_errors_never_gate_schema(self):
        expected = self.schema_output(io.StringIO())
        for error in (RuntimeError, AttributeError, TypeError):
            with self.subTest(error=error.__name__):
                stream = mock.Mock(write=mock.Mock(side_effect=error('synthetic stream failure')))
                self.assertEqual(self.schema_output(stream), expected)

    def test_warning_truth_table_at_swarm_entries(self):
        for active in (False, True):
            for disabled in (False, True):
                for entry in ('cmd_run', 'cmd_advance', 'main'):
                    with self.subTest(active=active, disabled=disabled, entry=entry), ExitStack() as stack:
                        stack.enter_context(self.activation_state(active))
                        stack.enter_context(mock.patch.object(sys, 'dont_write_bytecode', disabled))
                        stack.enter_context(mock.patch.object(sys, 'argv', ['swarm.py', 'schema']))
                        # Direct effect entries still refuse when active; inactive
                        # calls stop at fixed preparation without touching disk.
                        stack.enter_context(mock.patch.object(S.SR, 'apply_agent_resolution',
                            side_effect=SystemExit(23)))
                        err = stack.enter_context(redirect_stderr(io.StringIO()))
                        stack.enter_context(redirect_stdout(io.StringIO()))
                        with self.assertRaises(SystemExit) as stopped:
                            if entry == 'main':
                                S.main()
                            else:
                                getattr(S, entry)(argparse.Namespace(plan='fixture-plan.json'))
                        if entry == 'main':
                            self.assertEqual(stopped.exception.code, 0)
                        elif active:
                            self.assertIn('D5 active:', str(stopped.exception))
                        else:
                            self.assertEqual(stopped.exception.code, 23)
                        warning = err.getvalue()
                        self.assertEqual(bool(warning), active and not disabled)
                        if warning:
                            self.assertEqual(warning.count('D5 advisory:'), 1)
                            self.assertIn('bytecode writing is enabled', warning)
                            self.assertIn('host temporary-directory', warning)
                            self.assertIn('U8 to enforce or waive', warning)

    def test_read_only_and_diagnostic_failure_never_blocks(self):
        for stream in (io.StringIO(), mock.Mock(write=mock.Mock(side_effect=OSError('unavailable'))),
                       mock.Mock(write=mock.Mock(side_effect=ValueError('closed')))):
            with self.subTest(stream=type(stream).__name__), ExitStack() as stack:
                stack.enter_context(missing_installation(self.entry))
                stack.enter_context(classification_only(self.state))
                stack.enter_context(mock.patch.object(sys, 'dont_write_bytecode', False))
                stack.enter_context(redirect_stderr(stream))
                for owner, name in ((Path, 'iterdir'),
                                    (os, 'scandir'), (os, 'listdir')):
                    stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError('filesystem access')))
                self.assertIsNone(S._d5_bytecode_advisory(self.state))
                self.assertIs(sys.dont_write_bytecode, False)
                # Exercise a real allowed consumer with a failing stderr too.
                stack.enter_context(mock.patch.object(argparse.ArgumentParser, 'parse_args',
                    return_value=argparse.Namespace(fn=S.cmd_schema, state_dir=str(self.state))))
                stack.enter_context(redirect_stdout(io.StringIO()))
                with self.assertRaises(SystemExit) as stopped:
                    S.main()
                self.assertEqual(stopped.exception.code, 0)


class TestInactive(unittest.TestCase):
    def test_m6_bound_to_base(self):
        fixture = json.loads((FIXTURES / 'u49c-inactive-base.json').read_text())
        self.assertEqual(fixture['base'], BASE)
        self.assertEqual(set(fixture['captures']), {
            'swarm.cmd_run', 'swarm.cmd_advance', 'swarm.main', 'merge_unit.main'})
        self.assertEqual(inactive_captures(S, M), fixture['captures'])

    def test_tmpdir_portability(self):
        expected = json.loads((FIXTURES / 'u49c-inactive-base.json').read_text())['captures']
        probe = '''
import json, sys, tempfile
sys.path.insert(0, sys.argv[1])
from tests.test_d5_u49_entrypoints import S, M, inactive_captures
with tempfile.TemporaryDirectory() as scratch:
    print(json.dumps(inactive_captures(S, M), sort_keys=True))
'''
        with tempfile.TemporaryDirectory() as scratch:
            for leaf in ('s', 'long-' + 'x' * 150, 'with space'):
                directory = Path(scratch) / leaf
                directory.mkdir()
                with self.subTest(variant=leaf):
                    result = subprocess.run([sys.executable, '-B', '-c', probe, str(ROOT)],
                        env={'PATH': os.defpath, 'TMPDIR': str(directory)}, cwd=directory,
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(json.loads(result.stdout), expected)


if __name__ == '__main__':
    unittest.main()
