"""U4.9b named gate admission, option refusal and activation-copy consumers."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_d5_u4_0b1_issuance import no_effects
from tests.watchdog_guard import guarded_main

ROOT = Path(__file__).resolve().parents[1]
BASE = '4a97999d999347cdc52a6496bbfa5392f0b22d88'
GATE = ROOT / 'skills/hanig-review-gate/scripts'
FIXTURES = ROOT / 'tests/fixtures/d5'
sys.path.insert(0, str(GATE))
import review as R
import committee as C


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


class TestGateAdmission(unittest.TestCase):
    def assert_root_refusal(self, module, argv):
        for name in ('HANIG_COMMITTEE_DIR', 'XDG_STATE_HOME', R.JOURNAL_TEST_MARKER):
            for value in ('', '/synthetic-protected'):
                with self.subTest(name=name, value=value), ExitStack() as stack:
                    stack.enter_context(mock.patch.object(R, 'd5_gate_active', return_value=True))
                    stack.enter_context(mock.patch.dict(os.environ, {name: value}, clear=True))
                    stack.enter_context(mock.patch.object(sys, 'argv', argv))
                    output = stack.enter_context(redirect_stderr(io.StringIO()))
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    provider = stack.enter_context(mock.patch.object(R.urllib.request, 'urlopen'))
                    with no_effects() as effects, self.assertRaises(SystemExit) as stopped:
                        guarded_main(module.main)
                    self.assertEqual(stopped.exception.code, 4)
                    self.assertIn('gate-local root overrides refused: ' + name, output.getvalue())
                    self.assertEqual(effects, ([], []))
                    provider.assert_not_called()

    def test_review_rejects_unreviewed_options(self):
        self.assert_root_refusal(R, ['review.py', '--open-findings', '--head', 'a' * 40])

    def test_committee_rejects_unreviewed_options(self):
        self.assert_root_refusal(C, ['committee.py', 'open', 'fixture', '--problem', 'fixture'])

    def test_default_entry_is_admitted_and_inactive_overrides_are_unchanged(self):
        for active in (False, True):
            env = {} if active else {name: 'fixture' for name in
                ('HANIG_COMMITTEE_DIR', 'XDG_STATE_HOME', R.JOURNAL_TEST_MARKER)}
            with mock.patch.object(R, 'd5_gate_active', return_value=active), \
                    mock.patch.dict(os.environ, env, clear=True), no_effects() as effects:
                R.d5_gate_entry()
                self.assertEqual(effects, ([], []))

    def test_option_injection_refuses_before_git_or_session_effects(self):
        for value in ('--output=protected', 'HEAD..--output=protected', '-C',
                      'HEAD...--help', '--output=protected..HEAD'):
            for module, argv in ((R, ['review.py', '--size', '--range=' + value]),
                                 (R, ['review.py', '--size', '--target=' + value]),
                                 (C, ['committee.py', 'review', 'fixture', '--range=' + value])):
                with self.subTest(value=value, module=module.__name__), \
                        mock.patch.object(R, 'd5_gate_active', return_value=True), \
                        mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch.object(sys, 'argv', argv), \
                        redirect_stderr(io.StringIO()) as output, no_effects() as effects:
                    with self.assertRaises(SystemExit) as stopped:
                        guarded_main(module.main)
                    self.assertEqual(stopped.exception.code, 4)
                    self.assertIn("must not begin with '-'", output.getvalue())
                    self.assertEqual(effects, ([], []))

    def test_revision_consumers_and_inactive_passthrough(self):
        for active in (False, True):
            with mock.patch.object(R, 'd5_gate_active', return_value=active):
                for value in (None, 'HEAD', 'HEAD~2..HEAD', 'HEAD...', ':/-fixture'):
                    R.require_revision(value)
                if not active:
                    R.require_revision('--output=fixture')
                    continue
                args = type('Args', (), {'range': 'HEAD..--output=fixture'})()
                for call in (lambda: R.gather(args), lambda: R.changed_paths(args),
                             lambda: C.cmd_review(args),
                             lambda: R.resolve_range_ref('--help'),
                             lambda: R.range_divergence_warning(args.range)):
                    with redirect_stderr(io.StringIO()), no_effects() as effects:
                        with self.assertRaises(SystemExit):
                            call()
                        self.assertEqual(effects, ([], []))

    def test_active_git_disables_external_diff_and_textconv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker, helper = root / 'effect', root / 'helper'
            helper.write_text('#!/bin/sh\nprintf effect > "' + str(marker) + '"\n')
            helper.chmod(0o755)
            git = shutil.which('git')
            env = {'PATH': os.defpath, 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': os.devnull}
            def command(*args):
                subprocess.run([git, '-C', tmp, *args], check=True, capture_output=True,
                               env=env, timeout=30)
            command('init')
            (root / 'file').write_text('old\n')
            (root / '.gitattributes').write_text('file diff=fixture\n')
            command('add', '.')
            command('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-m', 'fixture')
            (root / 'file').write_text('new\n')
            old = Path.cwd()
            try:
                os.chdir(root)
                with mock.patch.dict(os.environ, env, clear=True):
                    for config in ('diff.external', 'diff.fixture.textconv'):
                        command('config', config, shlex.quote(str(helper)))
                        with mock.patch.object(R, 'd5_gate_active', return_value=False):
                            R.git_out('diff', 'HEAD')
                        self.assertEqual(marker.read_text(), 'effect')
                        marker.unlink()
                        with mock.patch.object(R, 'd5_gate_active', return_value=True):
                            self.assertIn('-old', R.git_out('diff', 'HEAD'))
                        self.assertFalse(marker.exists())
                        command('config', '--unset', config)
            finally:
                os.chdir(old)

    def test_external_diff_fixture_with_space_in_tmpdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            spaced = Path(tmp) / 'tmp with spaces'
            spaced.mkdir()
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest', '-v',
                'tests.test_d5_gate_admission.TestGateAdmission.'
                'test_active_git_disables_external_diff_and_textconv'],
                cwd=ROOT, env={'PATH': os.defpath, 'TMPDIR': str(spaced)},
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inactive_matches_recorded_merge_base(self):
        fixture = json.loads((FIXTURES / 'u49b-inactive-base.json').read_text())
        self.assertEqual(fixture['base'], BASE, 'stale M6 base')
        self.assertEqual(set(fixture['captures']), {
            'review/size', 'review/list', 'review/ledger', 'review/option',
            'committee/open', 'committee/ask', 'committee/review',
            'committee/synthesize', 'committee/tiebreak', 'committee/show'})
        env = {'PATH': ''}
        if 'TMPDIR' in os.environ:
            env['TMPDIR'] = os.environ['TMPDIR']
        for key, expected in fixture['captures'].items():
            with self.subTest(route=key):
                result = subprocess.run([sys.executable, '-B', str(FIXTURES / 'u49b_probe.py'),
                    json.dumps([str(ROOT)] + key.split('/'))], capture_output=True,
                    text=True, timeout=60, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)


class TestActivationCopies(unittest.TestCase):
    def test_expected_copies_match_canonical_bytes(self):
        pin_copies(ROOT)
        self.assertIs(R.d5_gate_active(), False)

    def test_gate_only_install_missing_or_active_copy(self):
        for state in ('inactive', 'missing', 'active'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                gate = Path(tmp) / 'gate'
                shutil.copytree(GATE.parent, gate, ignore=shutil.ignore_patterns('__pycache__'))
                activation = gate / 'scripts/d5_activation.py'
                if state == 'missing':
                    activation.unlink()
                elif state == 'active':
                    activation.write_text(activation.read_text().replace('return False', 'return True'))
                for program, argv in (('review', ['--list', '--no-probe']),
                                      ('committee', ['--help']),
                                      ('review', [R.JOURNAL_CHILD_ARG])):
                    result = subprocess.run([sys.executable, '-I', '-S', '-B',
                        str(gate / 'scripts' / (program + '.py'))] + argv,
                        input='{}', capture_output=True, text=True, timeout=30, cwd=tmp,
                        env={'PATH': '', 'XDG_STATE_HOME': str(Path(tmp) / 'state'),
                             'OPENAI_API_KEY': 'synthetic', 'OPENROUTER_API_KEY': 'synthetic',
                             'ANTHROPIC_API_KEY': 'synthetic'})
                    output = result.stdout + result.stderr
                    if state == 'missing':
                        self.assertIn('D5 activation unavailable', output)
                        self.assertNotEqual(result.returncode, 0)
                    elif state == 'active':
                        self.assertIn('gate-local root overrides refused', output)
                        self.assertNotEqual(result.returncode, 0)
                    else:
                        self.assertNotIn('D5 activation unavailable', output)
                        self.assertNotIn('gate-local root overrides refused', output)
                        if argv != [R.JOURNAL_CHILD_ARG]:
                            self.assertEqual(result.returncode, 0, output)
                self.assertFalse((Path(tmp) / 'state').exists())


if __name__ == '__main__':
    unittest.main()
