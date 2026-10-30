"""U4.9a named-entry refusals and merge containment; no isolation claim."""
from contextlib import nullcontext
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import builtins
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

from tests.test_d5_u4_0b1_issuance import no_effects

ROOT = Path(__file__).resolve().parents[1]
BASE = 'ce207c41c62da2001b193fe03ae423c717eb0348'
FIXTURES = ROOT / 'tests/fixtures/d5'


def cli_probe(tool, command, active, ready=False, source=ROOT):
    args = json.dumps([str(source), tool, command, active, ready])
    result = subprocess.run([sys.executable, '-B', str(FIXTURES / 'u49a_probe.py'), args],
        capture_output=True, text=True, timeout=90, env={'PATH': os.defpath})
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


class TestDirectRoots(unittest.TestCase):
    def test_direct_clis_both_readiness_states_before_effects(self):
        for ready in (False, True):
            for tool, commands in {'unit': ('allocate', 'bind', 'check'),
                                   'remote_verify': ('worker', 'supervise')}.items():
                for command in commands:
                    with self.subTest(ready=ready, tool=tool, command=command):
                        result = cli_probe(tool, command, True, ready)
                        self.assertIn('D5 active:', str(result['outcome']))
                        self.assertEqual(result['outcome'][0], 'SystemExit')
                        for channel in ('launches', 'writes', 'transports'):
                            self.assertEqual(result[channel], [], result)
                        self.assertTrue(result['protected_unchanged'])

    def test_inactive_matches_recorded_merge_base(self):
        fixture = json.loads((FIXTURES / 'u49a-inactive-base.json').read_text())
        self.assertEqual(fixture['base'], BASE)
        self.assertEqual(set(fixture['captures']), {'unit/allocate', 'unit/bind',
            'unit/check', 'remote_verify/worker', 'remote_verify/supervise', 'merge_unit/audit'})
        for key, expected in fixture['captures'].items():
            tool, command = key.split('/')
            with self.subTest(entry=key):
                self.assertEqual(cli_probe(tool, command, False), expected)


class TestMergeAdmission(unittest.TestCase):
    def test_production_merge_launch_is_not_admitted(self):
        import merge_unit as M
        self.assertIs(M.merge_launch_admitted(), False)

    def test_active_entries_refuse_before_any_effect_without_forced_admission(self):
        from tests.test_d5_registry import TestD5Registry
        from tests.fixtures.d5.ready import ready
        from tests.test_d5_u4_1_active_surface import SurfaceFixture
        from tests.test_d5_u4_2_request_authority import tree_bytes
        import merge_unit as M
        import d5_readiness as DR
        import d5_registry as registry
        for installed in (False, True):
            with self.subTest(installed=installed), ExitStack() as stack:
                case = TestD5Registry()
                case.setUp()
                stack.callback(case.doCleanups)
                if installed:
                    ready(case, case.repo, 'p', case.state)
                tmp = stack.enter_context(tempfile.TemporaryDirectory())
                fixture = SurfaceFixture(tmp)
                fixture.args.state_dir = str(case.state)
                fixture.margv[2] = str(case.state)
                before = tree_bytes(case.state)
                def lease_entry():
                    with M._active_entry_lease(fixture.args):
                        self.fail('entered active lease')
                calls = [lambda: M._d5_merge_entry(fixture.args), lease_entry,
                         lambda: M.reconcile(fixture.args, fixture.plan)]
                probes = []
                for owner, name in ((registry, 'lookup'), (M, 'authority'),
                        (M, 'current_intent'), (M, 'commands'), (M, 'read_object'),
                        (DR, 'require'), (DR, 'prepared_state'), (DR, 'installer_lease'),
                        (urllib.request, 'urlopen')):
                    probes.append(stack.enter_context(mock.patch.object(owner, name,
                        side_effect=AssertionError('unexpected ' + name))))
                stack.enter_context(nullcontext())
                output = io.StringIO()
                with no_effects() as effects, redirect_stderr(output):
                    for call in calls:
                        with self.assertRaisesRegex(M.Refusal, 'executable binding.*U8 cutover'):
                            call()
                    self.assertEqual(M.main(fixture.margv), 1)
                    self.assertIn('U8 cutover', output.getvalue())
                    self.assertIn('executable binding', output.getvalue())
                    self.assertEqual(effects, ([], []))
                for probe in probes:
                    probe.assert_not_called()
                self.assertEqual(tree_bytes(case.state), before)


class TestMergeChild(unittest.TestCase):
    def setUp(self):
        from tests.test_d5_u4_3_merge_provider import TestMergeProvider
        self.fixture = TestMergeProvider()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_actual_child_environment_and_no_descriptors_or_provider_tracker(self):
        import merge_unit as M
        import d5_process
        self.assertEqual(d5_process.DESIGNATED_BOOTSTRAPS, frozenset())
        names = {'D5_AUTHORITY_FD', 'HANIG_D5_COMPLETION', 'CLAUDE_CODE_SESSION_ID',
                 'CLAUDE_PID', 'LINEAR_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY',
                 'ANTHROPIC_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'SSH_AUTH_SOCK'}
        auth = {'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'}
        self.assertEqual(set(M.FORGE_AUTH_ENV), auth)
        gh = Path(shutil.which('gh'))
        self.assertEqual(gh.parent, Path(os.environ['PATH']))
        self.assertEqual(gh.parent.name, 'tools')
        received = self.fixture.c.root / 'received.json'
        prefix = ('import json, os, sys\nfrom pathlib import Path\n'
                  'received=[]\nfor fd in range(3,256):\n'
                  '    try:\n        st=os.fstat(fd); received.append([st.st_dev,st.st_ino])\n'
                  '    except OSError: pass\n'
                  "if sys.argv[1:3] == ['pr', 'merge']: Path(%r).write_text(json.dumps([sorted(set(os.environ) & %r),received]))\n"\
                  % (str(received), names | auth))
        original = gh.read_text().split('\n', 1)
        gh.write_text(original[0] + '\n' + prefix + original[1])
        popen = subprocess.Popen
        observed = []
        def launch(argv, *args, **kwargs):
            if argv[:3] == ['gh', 'pr', 'merge']:
                self.assertIs(kwargs.get('shell'), False)
                self.assertIs(kwargs.get('close_fds'), True)
                self.assertNotIn('pass_fds', kwargs)
                protected = [self.fixture.c.state_dir / 'lease.lock',
                             self.fixture.c.directory / 'authority.drain.lock']
                for path in protected:
                    st = path.stat()
                    observed.append([st.st_dev, st.st_ino])
                    fds = []
                    for fd in range(3, 256):
                        try:
                            if os.path.samestat(os.fstat(fd), st):
                                fds.append(fd)
                                self.assertFalse(os.get_inheritable(fd))
                        except OSError:
                            pass
                    self.assertTrue(fds, 'protected description was not live at launch')
            return popen(argv, *args, **kwargs)
        with mock.patch.dict(os.environ, {name: 'synthetic' for name in names | auth}), \
                mock.patch.object(subprocess, 'Popen', launch), \
                mock.patch.object(urllib.request, 'urlopen', side_effect=AssertionError('provider/tracker')) as wire:
            self.fixture.c.submit()
            wire.assert_not_called()
        child_names, descriptors = json.loads(received.read_text())
        self.assertFalse(set(child_names) & names)
        self.assertTrue(auth.issubset(child_names))
        self.assertTrue(observed)
        self.assertFalse(any(item in descriptors for item in observed))

    def test_arbitrary_executable_or_extra_switch_refuses_before_launch(self):
        import merge_unit as M
        for command in (['sh', '-c', 'exit 0'], self.fixture.cmd['merge'] + ['--admin']):
            with self.subTest(command=command), \
                    mock.patch.dict(self.fixture.boundary.cmd, {'merge': command}), \
                    no_effects() as (launches, writes):
                with self.assertRaisesRegex(M.Refusal, 'not designated'):
                    self.fixture.boundary.start(self.fixture.c.operation)
                self.assertEqual(launches, [])
                self.assertEqual(writes, [])

    def test_exit_zero_without_merge_is_not_completion(self):
        import merge_unit as M
        self.fixture.boundary.process = mock.Mock(returncode=0)
        self.fixture.boundary.process.communicate.return_value = ('', '')
        with self.assertRaisesRegex(M.Refusal, 'merge not observed'):
            self.fixture.boundary.finish()
        self.assertFalse(self.fixture.receipts.exists())


class TestMergeAudit(unittest.TestCase):
    def test_active_audit_unavailable_without_dependencies_or_effects(self):
        import merge_unit as M
        from types import SimpleNamespace
        # The inactive fixture reaches an actual audit write, so removing the
        # guard cannot pass merely because no binding/key is available.
        result = cli_probe('merge_unit', 'audit', True)
        self.assertEqual(result['output'],
            'Tracker: UNAVAILABLE (D5 active: audit is not admitted)\n')
        for channel in ('launches', 'writes', 'transports'):
            self.assertEqual(result[channel], [])
        imported = []
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name in ('linear_sync', 'linear_api', 'tracker_audit'):
                imported.append(name)
                raise AssertionError('unexpected tracker import')
            return original(name, *args, **kwargs)
        with nullcontext(), \
                mock.patch.object(builtins, '__import__', importing), \
                mock.patch.object(M.skill_paths, 'sibling_skill_root') as lookup, \
                mock.patch.object(M.tempfile, 'TemporaryDirectory') as temporary, \
                redirect_stdout(io.StringIO()) as output, no_effects() as effects:
            M.print_tracker_audit(SimpleNamespace())
            self.assertIn('Tracker: UNAVAILABLE (D5 active:', output.getvalue())
            self.assertEqual(effects, ([], []))
            self.assertEqual(imported, [])
            lookup.assert_not_called()
            temporary.assert_not_called()


if __name__ == '__main__':
    unittest.main()
