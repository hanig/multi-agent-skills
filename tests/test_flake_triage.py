"""Triage real Git/unittest fixtures."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess as SP
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/hanig-orchestrate/scripts/flake_triage.py'
sys.path.insert(0, str(SCRIPT.parent))
import flake_triage as F
ID = 'tests.test_x.C.test_x'
SOURCE = '''import os, time, unittest
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
