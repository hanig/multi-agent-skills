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
BASE = 'eb90c2c50081291c43e7177bcd3edfc2197fe61e'
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


if __name__ == '__main__':
    unittest.main()
