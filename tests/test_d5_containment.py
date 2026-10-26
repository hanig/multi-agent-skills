"""U4.7a enumerated CLI refusals, gate admission, and merge-child containment."""
import ast
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import importlib.util
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

ROOT = Path(__file__).resolve().parents[1]
BASE = '9f9cbaf74d2154262a1f1664c26573f1ef0c9d80'
FIXTURES = ROOT / 'tests/fixtures/d5'
GATE = ROOT / 'skills/hanig-review-gate/scripts'
SWARM = ROOT / 'skills/hanig-swarm/scripts'
sys.path.insert(0, str(GATE))
import review as R
import committee as C
from tests.test_d5_u4_0b1_issuance import no_effects
from tests.watchdog_guard import guarded_main

REFUSED = {
    'unit': ('allocate', 'bind', 'check'),
    'linear_sync': ('bind', 'drain', 'drain-dry', 'file', 'replay',
                    'issue-new', 'issue-edit', 'issue-replay', 'audit-state', 'audit-ops'),
    'remote_verify': ('worker', 'supervise'),
}


def cli_probe(tool, command, active, ready=False, source=ROOT, env=None, allow_gate=False):
    args = json.dumps([str(source), tool, command, active, ready, allow_gate])
    result = subprocess.run([sys.executable, '-B', str(FIXTURES / 'containment_probe.py'), args],
        capture_output=True, text=True, timeout=90,
        env=env or {'PATH': os.environ.get('PATH', os.defpath)})
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def pin_copies(root):
    skills = Path(root) / 'skills'
    canonical = skills / 'hanig-swarm/scripts/d5_activation.py'
    copies = set(skills.glob('hanig-*/scripts/d5_activation.py'))
    required = {canonical, skills / 'hanig-review-gate/scripts/d5_activation.py'}
    if not required.issubset(copies):
        raise AssertionError('missing activation copy')
    for copy in copies:
        if copy.read_bytes() != canonical.read_bytes():
            raise AssertionError('activation copy diverges: ' + str(copy))


class TestActivationCopies(unittest.TestCase):
    def test_activation_resolution_refuses_without_writing_bytecode(self):
        import importlib.machinery
        project = ROOT / 'skills/hanig-project/scripts'
        with mock.patch.object(sys, 'path', [str(project), str(SWARM)] + sys.path):
            import linear_sync
            import unit
            import remote_verify
        original = importlib.machinery.SourceFileLoader.exec_module
        for tool in ('linear', 'unit', 'remote', 'review'):
            for disabled in (False, True):
                with self.subTest(tool=tool, disabled=disabled), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    scripts = root / 'scripts'
                    scripts.mkdir()
                    source = scripts / 'd5_activation.py'
                    source.write_bytes((SWARM / 'd5_activation.py').read_bytes())
                    loaded = []
                    def activated(loader, module):
                        original(loader, module)
                        if module.__name__ in ('_linear_d5_activation', '_gate_d5_activation', 'd5_activation'):
                            loaded.append(Path(loader.path))
                            module.d5_active = lambda: True
                    with ExitStack() as stack:
                        stack.enter_context(mock.patch.dict(sys.modules))
                        sys.modules.pop('d5_activation', None)
                        stack.enter_context(mock.patch.object(sys, 'path', [str(scripts)] + sys.path))
                        stack.enter_context(mock.patch.object(sys, 'dont_write_bytecode', disabled))
                        stack.enter_context(mock.patch.object(importlib.machinery.SourceFileLoader, 'exec_module', activated))
                        stack.enter_context(mock.patch.object(linear_sync.skill_paths, 'sibling_skill_root', return_value=root))
                        stack.enter_context(mock.patch.object(R, 'HERE', scripts))
                        stack.enter_context(mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(root / 'protected')}))
                        stack.enter_context(mock.patch.object(sys, 'argv', ['remote_verify.py', '--worker', str(root / 'stage')]))
                        stack.enter_context(redirect_stderr(io.StringIO()))
                        call = {
                            'linear': lambda: linear_sync._d5_entry(type('Args', (), {'command': 'drain'})()),
                            'unit': lambda: unit._d5_dispatch(type('Args', (), {'cmd': 'check'})()),
                            'remote': remote_verify.main,
                            'review': R.d5_gate_entry,
                        }[tool]
                        with self.assertRaises((ValueError, SystemExit)):
                            call()
                        self.assertEqual(sys.dont_write_bytecode, disabled)
                    self.assertEqual(loaded, [source])
                    self.assertEqual(list(root.rglob('*.pyc')), [])
                    self.assertFalse((root / 'stage').exists())

    def test_expected_copies_match_canonical_bytes(self):
        pin_copies(ROOT)

    def test_activation_copy_mutations(self):
        for mutation in ('activation-copy-diverges', 'activation-copy-missing'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                paths = [Path(tmp) / 'skills' / skill / 'scripts/d5_activation.py'
                         for skill in ('hanig-swarm', 'hanig-review-gate')]
                for path in paths:
                    path.parent.mkdir(parents=True)
                    path.write_bytes((SWARM / 'd5_activation.py').read_bytes())
                pin_copies(tmp)
                if mutation.endswith('missing'):
                    paths[1].unlink()
                else:
                    paths[1].write_text('def d5_active(): return True\n')
                with self.assertRaisesRegex(AssertionError, 'activation copy'):
                    pin_copies(tmp)

    def test_gate_only_install_and_missing_activation(self):
        for absent in (False, True):
            with self.subTest(absent=absent), tempfile.TemporaryDirectory() as tmp:
                gate = Path(tmp) / 'gate'
                shutil.copytree(GATE.parent, gate, ignore=shutil.ignore_patterns('__pycache__'))
                if absent:
                    (gate / 'scripts/d5_activation.py').unlink()
                for program, args in (('review', ['--list', '--no-probe']),
                                      ('committee', ['show', 'absent'])):
                    result = subprocess.run([sys.executable, '-I', '-S', '-B',
                        str(gate / 'scripts' / (program + '.py'))] + args,
                        capture_output=True, text=True, timeout=30, env={'PATH': ''}, cwd=tmp)
                    text = result.stdout + result.stderr
                    self.assertEqual('D5 activation unavailable' in text, absent, text)
                    if absent:
                        self.assertNotEqual(result.returncode, 0)


class TestEnumeratedRefusals(unittest.TestCase):
    def test_direct_clis_both_readiness_states_before_all_effects(self):
        for ready in (False, True):
            for tool, commands in REFUSED.items():
                for command in commands:
                    with self.subTest(ready=ready, tool=tool, command=command):
                        result = cli_probe(tool, command, True, ready)
                        self.assertIn('D5 active:', str(result))
                        for channel in ('launches', 'writes', 'transports'):
                            self.assertEqual(result[channel], [], result)
                        self.assertTrue(result['protected_unchanged'])

    def test_inactive_matches_embedded_attempt_base_and_hermetic_path(self):
        fixture = json.loads((FIXTURES / 'u47-inactive-base.json').read_text())
        self.assertEqual(fixture['base'], BASE, 'stale-m6-base')
        with tempfile.TemporaryDirectory() as tmp:
            sinfo = Path(tmp) / 'sinfo'
            sinfo.write_text('#!/bin/sh\nexit 97\n')
            sinfo.chmod(0o755)
            env = {'PATH': tmp + os.pathsep + os.environ.get('PATH', os.defpath)}
            for key, expected in fixture['captures'].items():
                tool, command = key.split('/')
                with self.subTest(entry=key):
                    self.assertEqual(cli_probe(tool, command, False, env=env), expected)


class TestGateGuards(unittest.TestCase):
    def test_dash_prefixed_git_directory_is_a_value_not_an_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(['git', '-C', '--output=probe.txt', 'status'],
                cwd=tmp, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot change to '--output=probe.txt'", result.stderr)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_committee_provider_posts_and_real_writes_stay_gate_local(self):
        for command in ('open', 'ask', 'review', 'synthesize', 'tiebreak', 'show'):
            with self.subTest(command=command):
                result = cli_probe('committee', command, True, allow_gate=True)
                self.assertNotEqual(result['outcome'][0:1] if isinstance(result['outcome'], list) else [], ['AssertionError'], result)
                self.assertTrue(result['protected_unchanged'])
                self.assertEqual(set(result['transports']) - {'provider'}, set())
                self.assertEqual(result['launches'], [])
                if command in ('open', 'ask', 'review'):
                    self.assertTrue(result['transports'])
                    self.assertTrue(result['writes'])
        result = cli_probe('review', 'list', True)
        self.assertTrue(result['transports'])
        self.assertTrue(result['protected_unchanged'])
        self.assertEqual(result['writes'], [])

    def test_root_overrides_refuse_before_effects(self):
        import d5_activation
        for name in ('HANIG_COMMITTEE_DIR', 'XDG_STATE_HOME', R.JOURNAL_TEST_MARKER, 'HOME'):
            for tool, argv in ((R, ['review.py', '--open-findings', '--head', 'a' * 40]),
                               (C, ['committee.py', 'open', 'swarm-state', '--force', '--problem', 'fixture'])):
                with self.subTest(name=name, tool=tool.__name__), \
                        mock.patch.object(R, 'd5_gate_active', return_value=True), \
                        mock.patch.dict(os.environ, {name: '/synthetic-protected'}), \
                        mock.patch.object(sys, 'argv', argv), \
                        redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()), \
                        no_effects() as (launches, writes):
                    with self.assertRaises(SystemExit):
                        guarded_main(tool.main)
                    self.assertEqual(launches, [])
                    self.assertEqual(writes, [])

    def test_default_gate_roots(self):
        import d5_activation
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        with mock.patch.dict(os.environ, {'HOME': str(home)}, clear=True), \
                mock.patch.object(R, 'd5_gate_active', return_value=True), \
                no_effects() as (launches, writes):
            R.d5_gate_entry()
            self.assertEqual(R.round_ledger_root(), (home / '.local/state/hanig-review-gate/rounds').resolve())
            self.assertEqual(launches, [])
            self.assertEqual(writes, [])

    def test_range_option_injection_refuses_before_any_git_or_write(self):
        for value in ('--output=protected', 'HEAD..--output=protected', '-C', 'HEAD...--help'):
            for active in (False, True):
                for tool, argv in ((R, ['review.py', '--size', '--range=' + value]),
                                   (C, ['committee.py', 'review', 'fixture', '--range=' + value])):
                    import d5_activation
                    with self.subTest(value=value, active=active, tool=tool.__name__), \
                            mock.patch.object(R, 'd5_gate_active', return_value=active), \
                            mock.patch.object(R, 'd5_gate_entry'), \
                            mock.patch.object(sys, 'argv', argv), redirect_stderr(io.StringIO()), \
                            no_effects() as (launches, writes):
                        with self.assertRaises(SystemExit):
                            guarded_main(tool.main)
                        self.assertEqual(launches, [])
                        self.assertEqual(writes, [])

    def test_active_git_disables_external_diff(self):
        import d5_activation
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            marker = repo / 'effect'
            helper = repo / 'helper'
            helper.write_text('#!/bin/sh\nprintf effect > "$1"\n')
            helper.chmod(0o755)
            git = shutil.which('git')
            env = {'PATH': str(repo / 'tools'), 'HOME': tmp}
            (repo / 'tools').mkdir()
            (repo / 'tools/git').symlink_to(git)
            def command(*args):
                subprocess.run([git, '-C', tmp, *args], check=True, capture_output=True,
                               env=env, timeout=30)
            command('init')
            (repo / 'file').write_text('old')
            command('add', '--', 'file')
            command('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-m', 'fixture')
            (repo / 'file').write_text('new')
            import shlex
            command('config', 'diff.external', shlex.join([str(helper), str(marker)]))
            old = Path.cwd()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, env, clear=True):
                    with mock.patch.object(R, 'd5_gate_active', return_value=False):
                        R.git_out('diff', 'HEAD')
                    self.assertEqual(marker.read_text(), 'effect')
                    marker.unlink()
                    with mock.patch.object(R, 'd5_gate_active', return_value=True):
                        self.assertIn('-old', R.git_out('diff', 'HEAD'))
                    self.assertFalse(marker.exists())
            finally:
                os.chdir(old)


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
                        (R.urllib.request, 'urlopen')):
                    probes.append(stack.enter_context(mock.patch.object(owner, name,
                        side_effect=AssertionError('unexpected ' + name))))
                stack.enter_context(mock.patch.object(M.D5, 'd5_active', return_value=True))
                output = io.StringIO()
                with no_effects() as effects, redirect_stderr(output):
                    for call in calls:
                        with self.assertRaisesRegex(M.Refusal, 'U4.9.*executable binding'):
                            call()
                    self.assertEqual(M.main(fixture.margv), 1)
                    self.assertIn('U4.9', output.getvalue())
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
        received = self.fixture.c.root / 'received.json'
        prefix = ('import json, os\nfrom pathlib import Path\n'
                  'received=[]\nfor fd in range(3,256):\n'
                  '    try:\n        st=os.fstat(fd); received.append([st.st_dev,st.st_ino])\n'
                  '    except OSError: pass\n'
                  'Path(%r).write_text(json.dumps([sorted(os.environ),received]))\n' % str(received))
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
                mock.patch.object(R.urllib.request, 'urlopen', side_effect=AssertionError('provider/tracker')) as wire:
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


class TestLinearObservations(unittest.TestCase):
    def test_actual_query_transport_and_section_preserve_state(self):
        from tests.test_linear_audit import AuditCase, LS
        import importlib.machinery
        case = AuditCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        sentinel = case.state / 'swarm-state.json'
        sentinel.write_text('{"units":{}}')
        loader = importlib.machinery.SourceFileLoader.exec_module
        def active(self, module):
            loader(self, module)
            if module.__name__ == '_linear_d5_activation':
                module.d5_active = lambda: True
        before = sentinel.read_bytes()
        with mock.patch.object(importlib.machinery.SourceFileLoader, 'exec_module', active):
            self.assertEqual(case.cli('audit', '--binding', str(case.binding), '--out', str(case.out)), 0)
            self.assertTrue(case.fake.calls)
            self.assertTrue(all(q.lstrip().startswith('query') for q, _ in case.fake.calls))
            count = len(case.fake.calls)
            self.assertEqual(case.cli('section', '--audit', str(case.out), '--binding', str(case.binding)), 0)
            self.assertEqual(json.loads(case.out.read_text())['verdict'], 'CLEAN')
            self.assertIn('Linear consistent', case.stdout)
            self.assertEqual(len(case.fake.calls), count)
        self.assertEqual(sentinel.read_bytes(), before)


class TestFinalReviewAuditRegressions(unittest.TestCase):
    """Unfixed OD5 final-review counterexamples, retained for recovery."""

    def setUp(self):
        from tests.test_linear_audit import AuditCase, LS
        self.case = AuditCase()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.linear = LS
        self.operations = self.case.root / 'current' / 'operations'

    def active(self):
        import importlib.machinery
        original = importlib.machinery.SourceFileLoader.exec_module

        def load(loader, module):
            original(loader, module)
            if module.__name__ == '_linear_d5_activation':
                module.d5_active = lambda: True

        stack = ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, {
            'HANIG_LINEAR_OPS_DIR': str(self.operations)}))
        stack.enter_context(mock.patch.object(
            importlib.machinery.SourceFileLoader, 'exec_module', load))
        stack.enter_context(mock.patch.object(R.urllib.request, 'urlopen',
            side_effect=AssertionError('unexpected network')))
        return stack

    def test_safe_reports_ignore_unrelated_marker_names(self):
        case = self.case
        for marker in ('lease.lock', 'swarm-state.json'):
            with self.subTest(marker=marker), self.active():
                reports = case.root / ('reports-' + marker)
                reports.mkdir()
                target = reports / 'audit.json'
                argv = ('audit', '--binding', str(case.binding), '--out', str(target))
                self.assertEqual(case.cli(*argv), 0, case.stderr)
                target.unlink()
                unrelated = reports / marker
                unrelated.write_text('Unrelated fixture data, not coordinator state.\n')
                before = unrelated.read_bytes()
                case.fake.calls.clear()
                rc = case.cli(*argv)
                self.assertEqual(unrelated.read_bytes(), before)
                self.assertEqual((rc, target.exists(), bool(case.fake.calls)),
                                 (0, True, True), case.stderr)

    def test_audit_output_cannot_overwrite_another_operation_root(self):
        case = self.case
        old = case.root / 'old' / 'operations'
        with mock.patch.dict(os.environ, {'HANIG_LINEAR_OPS_DIR': str(old)}):
            directory = self.linear.TA.operation_directory('workspace', 'project')
            directory.mkdir(parents=True)
            target = directory / 'existing-operation.json'
            target.write_text('{"protected":"operation-journal"}\n')
        before = target.read_bytes()
        with self.active():
            rc = case.cli('audit', '--binding', str(case.binding), '--out', str(target))
        self.assertEqual((rc != 0, target.read_bytes() == before, not case.fake.calls),
                         (True, True, True), 'unsafe output must refuse before effects')


if __name__ == '__main__':
    unittest.main()
