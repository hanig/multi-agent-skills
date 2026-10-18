"""Triage real Git/unittest fixtures."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess as SP
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/hanig-orchestrate/scripts/flake_triage.py'
sys.path.insert(0, str(SCRIPT.parent))
import flake_triage as F
ID = 'tests.test_x.C.test_x'
SOURCE = '''import os, time, unittest, subprocess, sys
from pathlib import Path
from mode import MODE
class C(unittest.TestCase):
 def test_x(self):
  if MODE == 'fail': self.fail('fixture failure')
  if MODE == 'error': raise RuntimeError('fixture error')
  if MODE == 'flaky':
   p = Path('counter')
   n = int(p.read_text()) if p.exists() else 0
   p.write_text(str(n+1))
   self.assertEqual(n % 2, 1)
  if MODE == 'timeout': time.sleep(30)
  if MODE == 'permissions':
   d = Path('locked')
   d.mkdir()
   (d / 'artifact').write_text('fixture')
   (d / 'outside').symlink_to(os.environ['OUTSIDE'])
   d.chmod(0)
   Path(os.environ['READY']).write_text(str(Path.cwd()))
   Path.cwd().chmod(0)
  if MODE == 'detached':
   child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
   Path(os.environ['CHILD_PID']).write_text(str(child.pid))
   Path(os.environ['READY']).write_text(str(Path.cwd()))
   time.sleep(60)
  if MODE == 'interrupt':
   Path(os.environ['READY']).write_text(str(Path.cwd()))
   time.sleep(30)
'''

class TestFlakeTriage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'repo space'
        self.repo.mkdir()
        self.env = F.child_env()
        for args in [('init', '-q'), ('config', 'user.email', 'a@invalid'),
                     ('config', 'user.name', 'Fixture'), ('config', 'commit.gpgsign', 'false')]:
            self.git(*args)
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/__init__.py').touch()
        self.testfile = self.repo / 'tests/test_x.py'
        self.testfile.write_text(SOURCE)
        self.base = self.head = self.save('pass')

    def git(self, *args):
        return SP.check_output(['git', '-C', str(self.repo), *args], env=F.child_env(),
                               stderr=SP.STDOUT, text=True).strip()

    def save(self, mode):
        (self.repo / 'mode.py').write_text('MODE = %r\n' % mode)
        self.git('add', '.')
        self.git('commit', '-qm', mode, '--allow-empty')
        return self.git('rev-parse', 'HEAD')

    def argv(self, *extra):
        return ['--repo', str(self.repo), '--head', self.head[:12], '--base', self.base[:12],
                '--test', ID, '--runs', '2', '--timeout', '5', '--python', sys.executable, *extra]

    def cli(self, *extra):
        return SP.run([sys.executable, str(SCRIPT), *self.argv(*extra)], env=self.env,
                      capture_output=True, text=True, timeout=30)

    def trees(self):
        return self.git('worktree', 'list', '--porcelain')

    def git_state(self):
        return (self.git('for-each-ref'), self.trees(),
                hashlib.sha256((self.repo / '.git/config').read_bytes()).hexdigest(),
                self.git('count-objects', '-v'))

    def git_fixture(self, body):
        self.testfile.write_text('import os, time, subprocess, sys, unittest\n'
                                 'from pathlib import Path\n'
                                 "common = Path(subprocess.check_output(['git', 'rev-parse', '--git-common-dir'], text=True).strip()).resolve()\n"
                                 'class C(unittest.TestCase):\n'
                                 ' def test_x(self):\n' + body)
        self.head = self.save('pass')
        self.git('branch', 'disposable')
        remote = self.repo.parent / 'remote.git'
        SP.check_call(['git', 'init', '--bare', '-q', str(remote)], env=self.env)
        self.git('remote', 'add', 'fixture', str(remote))

    def test_scratch_git_identity_and_environment(self):
        self.git_fixture('''  self.assertEqual(common, Path.cwd() / '.git')
  self.assertNotEqual(str(common), os.environ['OPERATOR_COMMON'])
  self.assertEqual(subprocess.check_output(['git', 'remote']), b'')
  self.assertFalse((common / 'objects/info/alternates').exists())
  self.assertFalse(any(k.startswith('GIT_') for k in os.environ))
''')
        self.env['OPERATOR_COMMON'] = str(self.repo / '.git')
        before = self.git_state()
        for overrides in ({}, dict(GIT_DIR=str(self.repo / '.git'), GIT_WORK_TREE=str(self.repo),
                                   GIT_INDEX_FILE=str(self.repo / '.git/index'), GIT_CANARY='set')):
            self.env.update(overrides)
            self.check('INCONCLUSIVE', [['pass'], ['pass']], '--runs', '1')
            self.assertEqual(self.git_state(), before)

    def test_escaped_git_helper_preserves_operator(self):
        helper = '''import os, subprocess, sys, time
from pathlib import Path
time.sleep(3)
os.chdir('/')
git = ['git', '--git-dir=' + sys.argv[1]]
subprocess.run(git + ['update-ref', 'refs/heads/canary', 'HEAD'])
r = subprocess.run(git + ['remote'], capture_output=True, text=True)
for remote in r.stdout.splitlines():
 subprocess.run(git + ['push', remote, '--all'])
Path(sys.argv[2]).write_text('attempted')
time.sleep(60)
'''
        self.git_fixture('''  p = subprocess.Popen([sys.executable, '-c', %r, str(common), os.environ['ATTEMPTED']], start_new_session=True)
  Path(os.environ['CHILD_PID']).write_text(str(p.pid))
  time.sleep(60)
''' % helper)
        pid, attempted = self.repo.parent / 'pid', self.repo.parent / 'attempted'
        self.env.update(CHILD_PID=str(pid), ATTEMPTED=str(attempted))
        before = self.git_state()
        try:
            self.check('CONSISTENT_FAILURE', [['timeout'], ['pass']], '--runs', '1', '--timeout', '1')
            self.assertTrue(pid.exists())
            time.sleep(5)
            self.assertEqual(attempted.read_text(), 'attempted')
            self.assertEqual(self.git_state(), before)
        finally:
            if pid.exists():
                try: os.kill(int(pid.read_text()), signal.SIGKILL)
                except ProcessLookupError: pass

    def test_synchronous_git_mutator_preserves_operator(self):
        self.git_fixture('''  for args in (['branch', '-D', 'disposable'], ['update-ref', 'refs/heads/canary', 'HEAD'], ['worktree', 'prune']):
   subprocess.run(['git'] + args)
''')
        before = self.git_state()
        self.check('INCONCLUSIVE', [['pass'], ['pass']], '--runs', '1')
        self.assertEqual(self.git_state(), before)

    def test_unreachable_sha(self):
        before = self.git_state()
        r = self.cli('--head', '0' * 40, '--json')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)['classification'], 'SETUP_ERROR')
        with self.assertRaises(F.SetupError):
            with F.scratch_repository(self.repo, '0' * 40):
                self.fail('unreachable commit yielded a checkout')
        self.assertEqual(self.git_state(), before)

    def test_limit_wording(self):
        wording = F.__doc__.split('\n\n', 1)[1].strip()
        self.assertEqual(hashlib.sha256(wording.encode()).hexdigest(),
                         'f350dc82db6fb149a924aa2002c87e7de4a79530d4644632cfc69676f73bc7a9')
        self.assertIn(wording, self.cli('--runs', '1').stdout)
        self.env['PYTHONOPTIMIZE'] = '2'
        r = self.cli('--json', '--runs', '1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(wording, json.loads(r.stdout)['notice'])

    def check(self, label, outcomes, *extra, code=0, index=0):
        before = self.trees()
        r = self.cli('--json', *extra)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual((report['head'], report['base']), (self.head, self.base))
        self.assertEqual(self.trees(), before)
        d = report['tests'][index]
        self.assertEqual(d['class'], label)
        self.assertEqual([[r['outcome'] for r in d[s]] for s in ('head', 'base')], outcomes)
        return d

    def test_setup_error(self):
        self.testfile.unlink()
        self.head = self.save('fail')
        d = self.check('SETUP_ERROR', [['absent']*2, ['pass']*2], code=2)
        self.assertTrue(d['touched'])

    def test_fails_on_base(self):
        self.base = self.save('fail')
        self.head = self.save('flaky')
        self.check('FAILS_ON_BASE', [['fail', 'pass'], ['fail']*2])

    def test_flaky_on_head(self):
        self.head = self.save('flaky')
        d = self.check('FLAKY_ON_HEAD', [['fail', 'pass'], ['pass']*2])
        self.assertFalse(d['touched'])
        self.assertIn('complete rerun', d['suggested_rerun'])

    def test_consistent_failure(self):
        for mode in ('fail', 'error', 'timeout'):
            self.head = self.save(mode)
            d = self.check('CONSISTENT_FAILURE', [[mode]*2, ['pass']*2], '--timeout', '0.5')
            self.assertFalse(d['touched'])
            self.assertEqual(d['file'], 'tests/test_x.py')

    def test_inconclusive(self):
        self.check('INCONCLUSIVE', [['pass']*2]*2)

    def test_absent_base(self):
        (self.repo / 'tests/test_new.py').write_text(SOURCE)
        self.head = self.save('flaky')
        d = self.check('FLAKY_ON_HEAD', [['fail', 'pass'], ['absent']*2],
                       '--test', 'tests.test_new.C.test_x', index=1)
        self.assertTrue(d['touched'])
        self.assertIn('unavailable comparison evidence', d['issue_body'])

    def test_three_dot_and_module_class_ids(self):
        self.head = self.save('pass')
        self.git('checkout', '-q', '--detach', self.base)
        self.testfile.write_text(SOURCE + '\n# target-only edit\n')
        self.base = self.save('pass')
        for name in ('tests.test_x', 'tests.test_x.C'):
            d = self.check('INCONCLUSIVE', [['pass']*2]*2, '--test', name, index=1)
            self.assertFalse(d['touched'])

    def test_scratch_creation_failure_cleanup(self):
        git, roots = F.git, []
        def failing_checkout(repo, *args, **kwargs):
            if args[0] == 'checkout':
                roots.append(repo.parent)
                hook = repo / '.git/hooks/post-checkout'
                hook.parent.mkdir(exist_ok=True)
                hook.write_text('#!/bin/sh\nexit 1\n')
                hook.chmod(0o755)
            return git(repo, *args, **kwargs)
        before = self.git_state()
        with patch.object(F, 'git', failing_checkout), patch('sys.stdout') as output:
            self.assertEqual(F.main(self.argv('--json')), 2)
        report = json.loads(output.write.call_args_list[0].args[0])
        self.assertEqual(report['tests'][0]['class'], 'SETUP_ERROR')
        self.assertEqual([report['tests'][0][s] for s in ('head', 'base')], [[], []])
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(not root.exists() for root in roots))
        self.assertEqual(self.git_state(), before)

    def test_cleanup_restores_directory_access_without_following_symlinks(self):
        self.head = self.save('permissions')
        outside, ready = self.repo.parent / 'outside', self.repo.parent / 'ready'
        outside.mkdir(mode=0o500)
        self.env.update(OUTSIDE=str(outside), READY=str(ready))
        try:
            self.check('INCONCLUSIVE', [['pass'], ['pass']], '--runs', '1')
            self.assertFalse(Path(ready.read_text()).parent.exists())
            self.assertEqual(outside.stat().st_mode & 0o777, 0o500)
        finally:
            outside.chmod(0o700)
            if ready.exists():
                tree = Path(ready.read_text())
                if tree.exists():
                    tree.chmod(0o700)
                    (tree / 'locked').chmod(0o700)
                    shutil.rmtree(str(tree.parent))

    def test_load_errors_on_both_sides(self):
        good = self.base
        for bad in ("raise ImportError('broken')", 'import missing_dependency', 'invalid syntax !',
                    'import os\nos.removed_helper',
                    "try:\n import os\n os.removed_helper\nexcept AttributeError as exc:\n raise ImportError('broken API') from exc"):
            self.testfile.write_text(bad)
            self.head = self.save('pass')
            self.base = good
            self.check('CONSISTENT_FAILURE', [['error']*2, ['pass']*2])
            self.base, self.head = self.head, good
            self.check('FAILS_ON_BASE', [['pass']*2, ['error']*2])

    def test_missing_attributes_and_empty_class(self):
        for before, after in [('class C', 'class D'), ('def test_x', 'def test_other')]:
            self.testfile.write_text(SOURCE.replace(before, after))
            self.head = self.save('pass')
            self.check('SETUP_ERROR', [['absent']*2, ['pass']*2], code=2)
        self.testfile.write_text(SOURCE + '\nclass Empty(unittest.TestCase): pass\n')
        self.head = self.save('pass')
        result = SP.run([sys.executable, '-m', 'unittest', 'tests.test_x.Empty'],
                        cwd=str(self.repo), env=self.env, capture_output=True)
        label, outcome = ('CONSISTENT_FAILURE', 'error') if result.returncode else ('INCONCLUSIVE', 'pass')
        self.check(label, [[outcome]*2, ['absent']*2], '--test', 'tests.test_x.Empty', index=1)

    def test_detached_output_timeout_and_interrupt(self):
        self.head = self.save('detached')
        before = self.trees()
        for interrupt in (False, True):
            pidfile, ready = self.repo.parent / 'child', self.repo.parent / 'ready'
            for path in (pidfile, ready):
                if path.exists(): path.unlink()
            p = SP.Popen([sys.executable, str(SCRIPT), *self.argv('--json', '--runs', '1', '--timeout', '1')],
                         env=dict(self.env, CHILD_PID=str(pidfile), READY=str(ready)),
                         stdout=SP.PIPE, stderr=SP.PIPE)
            try:
                end = time.monotonic() + 10
                while not ready.exists() and p.poll() is None and time.monotonic() < end:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                scratch = Path(ready.read_text())
                if interrupt: p.send_signal(signal.SIGINT)
                out, err = p.communicate(timeout=10)
                self.assertEqual(p.returncode, 2 if interrupt else 0, (out, err))
                if not interrupt:
                    self.assertEqual(json.loads(out)['tests'][0]['head'][0]['outcome'], 'timeout')
                self.assertFalse(scratch.parent.exists())
                self.assertEqual(self.trees(), before)
            finally:
                if pidfile.exists():
                    try: os.kill(int(pidfile.read_text()), signal.SIGKILL)
                    except ProcessLookupError: pass
                try: p.communicate(timeout=10)
                except SP.TimeoutExpired:
                    p.kill()
                    p.communicate()

    def test_interrupt_cleanup(self):
        self.head = self.save('interrupt')
        before = self.trees()
        for sig in (signal.SIGINT, signal.SIGTERM):
            ready = self.repo.parent / str(sig)
            p = SP.Popen([sys.executable, str(SCRIPT), *self.argv('--json')],
                         env=dict(self.env, READY=str(ready)), stdout=SP.PIPE, stderr=SP.PIPE)
            try:
                end = time.monotonic() + 10
                while not ready.exists() and p.poll() is None and time.monotonic() < end:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                scratch = Path(ready.read_text())
                p.send_signal(sig)
                out, err = p.communicate(timeout=10)
                self.assertEqual(p.returncode, 2, (out, err))
                self.assertIn(b'triage incomplete', err)
                self.assertFalse(scratch.parent.exists())
                self.assertEqual(self.trees(), before)
            finally:
                if p.poll() is None:
                    p.kill()
                p.communicate()

    def test_shell_quoting(self):
        title = '"flake" \'quote\' $HOME $(touch INJECTED)'
        cmd = F.issue_command(title, 'body file', 'a binding', 'A "Name"')
        expected = ['linear_sync.py', 'issue', 'new', '--binding', 'a binding',
                    '--title', title, '--body-file', 'body file', '--approver', 'A "Name"']
        self.assertEqual(shlex.split(cmd), expected)
        exe = self.repo / 'linear_sync.py'
        exe.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@"\n')
        exe.chmod(0o755)
        r = SP.run(['/bin/sh', '-c', cmd], cwd=str(self.repo), capture_output=True, text=True,
                   env=dict(self.env, PATH=str(self.repo) + os.pathsep + os.defpath), check=True)
        self.assertEqual(r.stdout.splitlines(), expected[1:])
        self.assertFalse((self.repo / 'INJECTED').exists())

if __name__ == '__main__':
    unittest.main()
