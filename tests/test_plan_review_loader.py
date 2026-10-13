"""Fresh-process loader checks through the real review commands and fake transport."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


class TestPlanReviewLoader(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='.hanig-review-gate-tests-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        for arguments in (
                ['init', '-q'], ['config', 'user.name', 'Loader Fixture'],
                ['config', 'user.email', 'loader@example.invalid'],
                ['config', 'commit.gpgsign', 'false'],
                ['remote', 'add', 'origin', 'https://example.invalid/fixture.git']):
            self.git(*arguments)
        (self.repo / 'candidate.py').write_text('value = 1\n')
        self.git('add', 'candidate.py')
        self.git('commit', '-qm', 'fixture')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.root / 'state'),
                        HANIG_REVIEW_GATE_TESTING=str(self.root),
                        OPENAI_API_KEY='test-loader-alpha-key',
                        OPENROUTER_API_KEY='test-loader-beta-key',
                        HANIG_ROUTING_SNAPSHOTS='off',
                        ROUND_CLI_CALLS=str(self.root / 'calls.txt'))

    def git(self, *arguments):
        subprocess.run(['git', *arguments], cwd=self.repo, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run_process(self, code, *arguments):
        result = subprocess.run([sys.executable, '-c', code, str(REPO), *arguments],
                                cwd=self.repo, env=self.env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def run_implementation(self, polluted):
        code = '''
import io, json, runpy, sys, types
from contextlib import redirect_stdout
from pathlib import Path
repo = Path(sys.argv[1])
polluted = sys.argv[2] == 'yes'
private = '_hanig_review_gate_plan_review'
if polluted:
    unrelated = types.ModuleType('plan_review')
    sys.modules['plan_review'] = unrelated
sys.argv = [str(repo / 'tests/review_round_cli.py'),
            str(repo / 'skills/hanig-review-gate/scripts/review.py'),
            '--kind', 'implementation', '--round', '1', '--file', 'candidate.py',
            '--claim', 'This change cannot make an honest run fail.', '--json']
output = io.StringIO()
with redirect_stdout(output):
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    except SystemExit as stopped:
        assert stopped.code == 0, (stopped.code, output.getvalue())
    else:
        raise AssertionError('implementation command did not exit')
assert json.loads(output.getvalue())['state'] == 'REVIEW_PASS', output.getvalue()
assert private not in sys.modules, 'implementation loaded the plan-review module'
assert 'plan_review' not in sys.modules or sys.modules['plan_review'] is unrelated
if polluted:
    sys.path.insert(0, str(repo))
    from tests.test_plan_review import TestPlanExchange
    case = TestPlanExchange()
    case.setUp()
    try:
        code, report, stderr = case.run_cli()
        assert (code, report['state']) == (0, 'PLAN_PASS'), (code, report, stderr)
        assert len(case.requests) == 4, case.requests
        assert sys.modules['plan_review'] is unrelated
        assert Path(sys.modules[private].__file__) == repo / 'skills/hanig-review-gate/scripts/plan_review.py'
    finally:
        case.doCleanups()
print('implementation and requested plan transport completed')
'''
        self.run_process(code, 'yes' if polluted else 'no')
        self.assertEqual((self.root / 'calls.txt').read_text().splitlines(),
                         ['provider', 'provider'])

    def test_cached_unrelated_module_allows_both_review_kinds(self):
        self.run_implementation(True)

    def test_implementation_never_loads_plan_module(self):
        self.run_implementation(False)

    def test_private_cache_reuses_only_the_shipped_path(self):
        self.run_process('''
import importlib.util, sys, types
from pathlib import Path
from unittest import mock
script = Path(sys.argv[1]) / 'skills/hanig-review-gate/scripts/review.py'
private = '_hanig_review_gate_plan_review'
def load(name):
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
subject = load('loader_subject')
assert private not in sys.modules
for wrong_path in (None, str(Path.cwd() / 'plan_review.py')):
    unrelated = types.ModuleType(private)
    if wrong_path is not None:
        unrelated.__file__ = wrong_path
    sys.modules[private] = unrelated
    try:
        subject._load_plan_review()
    except ImportError:
        pass
    else:
        raise AssertionError('accepted unrelated private cache entry')
    assert sys.modules[private] is unrelated
del sys.modules[private]
owner = subject._load_plan_review()
assert Path(owner.__file__) == script.parent / 'plan_review.py'
second = load('second_loader_subject')
with mock.patch.object(importlib.util, 'spec_from_file_location',
                       side_effect=AssertionError('module loaded more than once')):
    assert subject._load_plan_review() is owner
    assert second._load_plan_review() is owner
assert sys.modules[private] is owner
assert 'plan_review' not in sys.modules
''')


if __name__ == '__main__':
    unittest.main()
