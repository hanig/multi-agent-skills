"""Exercise the pinned verifier against disposable candidate trees only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / 'verifiers' / 'integration_tests.py'
spec = importlib.util.spec_from_file_location('parallel_verifier', str(VERIFIER))
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
PASS = '''\
import unittest
class TestExample(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(2 + 2, 4)
'''


class TestParallelVerifier(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests' / '__init__.py').write_text(
            "raise AssertionError('tests package must not be imported')\n")

    def module(self, name, source=PASS):
        (self.root / 'tests' / name).write_text(textwrap.dedent(source))

    def run_verifier(self, workers='4', patch=''):
        env = dict(os.environ)
        env.pop('HANIG_VERIFIER_WORKERS', None)
        if workers is not None:
            env['HANIG_VERIFIER_WORKERS'] = workers
        if patch:
            code = ('import importlib.util, sys\n'
                    'spec = importlib.util.spec_from_file_location("v", sys.argv[1])\n'
                    'v = importlib.util.module_from_spec(spec)\n'
                    'spec.loader.exec_module(v)\n' + patch +
                    '\nraise SystemExit(v.main())\n')
            argv = [sys.executable, '-c', code, str(VERIFIER)]
        else:
            argv = [sys.executable, str(VERIFIER)]
        return subprocess.run(argv, cwd=str(self.root), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=30)

    def summary(self, result):
        lines = result.stdout.splitlines()
        self.assertTrue(lines[-1].startswith('SUMMARY '), result.stdout)
        return {key: int(value) for key, value in
                re.findall(r'(\w+)=(\d+)(?: |$)', lines[-1])}

    def test_pass_counts_match_single_discovery(self):
        self.module('test_z.py')
        self.module('test_alpha.py', '''\
            import unittest
            class TestExample(unittest.TestCase):
                def test_pass(self): pass
                @unittest.skip('fixture')
                def test_skip(self): pass
                @unittest.expectedFailure
                def test_expected_failure(self): self.fail('expected')
        ''')
        self.module('testplain.py')
        self.module('helper.py', "raise AssertionError('not a test module')\n")
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)
        counts = self.summary(result)
        self.assertEqual(counts, dict(tests=5, failures=0, errors=0, skips=1,
                                     modules=3, failed_modules=0))
        per_module = [int(n) for n in re.findall(r'^Ran (\d+) tests? in ',
                                                result.stdout, re.MULTILINE)]
        self.assertEqual(sum(per_module), counts['tests'])
        serial = subprocess.run([sys.executable, '-m', 'unittest', 'discover',
                                 '-s', 'tests'], cwd=str(self.root),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(serial.returncode, 0, serial.stderr)
        self.assertIn('Ran {} tests'.format(counts['tests']), serial.stderr)
        self.assertIn('skipped=1', serial.stderr)

    def test_failure_and_error_output_and_counts(self):
        self.module('test_ok.py')
        self.module('test_bad.py', '''\
            import unittest
            class TestBad(unittest.TestCase):
                def test_fail(self): self.fail('distinct failure message')
                def test_error(self): raise ValueError('distinct error message')
                @unittest.skip('fixture')
                def test_skip(self): pass
        ''')
        result = self.run_verifier()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('distinct failure message', result.stdout)
        self.assertIn('distinct error message', result.stdout)
        self.assertIn('test_bad.py: FAIL:', result.stdout)
        self.assertEqual(self.summary(result), dict(tests=4, failures=1, errors=1,
                                                   skips=1, modules=2, failed_modules=1))

    def test_signal_after_success_report_still_fails(self):
        self.module('test_signal.py', PASS + '''\
import atexit, os, signal
atexit.register(lambda: os.kill(os.getpid(), signal.SIGKILL))
''')
        result = self.run_verifier()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('Ran 1 test', result.stdout)
        self.assertIn('test_signal.py: FAIL: child exited -', result.stdout)

    def test_crash_and_early_success_without_report_fail(self):
        for code in (0, 7):
            with self.subTest(code=code):
                self.module('test_exit.py', 'import os\nos._exit({})\n'.format(code))
                result = self.run_verifier()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('test_exit.py: FAIL:', result.stdout)

    def test_nested_success_report_cannot_hide_early_exit(self):
        self.module('test_exit.py', """\
            import os, unittest
            class TestExit(unittest.TestCase):
                def test_exit(self):
                    unittest.TextTestRunner().run(unittest.TestSuite([
                        unittest.FunctionTestCase(lambda: None)]))
                    os._exit(0)
        """)
        result = self.run_verifier()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('Ran 1 test', result.stdout)
        self.assertIn('missing or invalid unittest completion record', result.stdout)

    def test_import_time_nested_program_cannot_complete_outer_program(self):
        self.module('test_exit.py', PASS + """\
import os
unittest.main(module=__name__, argv=['nested'], exit=False)
os._exit(0)
""")
        result = self.run_verifier()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('missing or invalid unittest completion record', result.stdout)

    def test_no_modules_matches_single_discovery(self):
        result = self.run_verifier()
        serial = subprocess.run([sys.executable, '-m', 'unittest', 'discover',
                                 '-s', 'tests'], cwd=str(self.root),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, serial.returncode, result.stdout)
        self.assertEqual(self.summary(result)['modules'], 0)

    def test_discovery_can_import_local_json_helper(self):
        self.module('json.py', 'SENTINEL = True\n')
        self.module('test_helper.py', PASS + 'import json\nassert json.SENTINEL\n')
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.summary(result)['tests'], 1)

    def test_temporary_startup_is_not_exposed(self):
        probe = ("import importlib.util; s = importlib.util.find_spec('sitecustomize'); "
                 "print(repr(s.origin if s else None))")
        expected = subprocess.check_output([sys.executable, '-c', probe],
                                           cwd=str(self.root), text=True).splitlines()[-1]
        self.module('test_startup.py', PASS + """\
import importlib.util
spec = importlib.util.find_spec('sitecustomize')
assert (spec.origin if spec else None) == EXPECTED
""".replace('EXPECTED', expected))
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_load_hook_may_return_genuinely_empty_suite(self):
        self.module('test_optional.py', PASS + """\
def load_tests(loader, tests, pattern):
    return unittest.TestSuite()
""")
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('test_optional.py: discovered zero tests', result.stdout)
        self.assertEqual(self.summary(result)['tests'], 0)

    def test_module_set_comparison(self):
        self.module('test_a.py')
        self.module('test_b.py')
        complete = self.run_verifier()
        self.assertEqual(complete.returncode, 0, complete.stdout)
        self.assertEqual(self.summary(complete)['modules'], 2)
        omitted = self.run_verifier(patch='''\
original = v.run_modules
def omit(root, modules, workers):
    return original(root, modules[:-1], workers)
v.run_modules = omit
''')
        self.assertEqual(omitted.returncode, 1, omitted.stdout)
        self.assertIn("module set mismatch: missing=['test_b.py']", omitted.stdout)

    def test_invalid_workers(self):
        self.module('test_never.py', "raise AssertionError('must not import')\n")
        for value in ('0', 'x', '-1', '1.5', ''):
            with self.subTest(value=value):
                result = self.run_verifier(workers=value)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('HANIG_VERIFIER_WORKERS', result.stdout)
                self.assertNotIn('must not import', result.stdout)

    def test_default_workers_and_override(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for cpu, expected in ((None, 1), (0, 1), (2, 2), (40, 8)):
                with mock.patch.object(verifier.os, 'cpu_count', return_value=cpu):
                    self.assertEqual(verifier.worker_count(), expected)
            with mock.patch.dict(os.environ, {'HANIG_VERIFIER_WORKERS': '12'}):
                self.assertEqual(verifier.worker_count(), 12)

    def test_nested_package_refused_before_import(self):
        package = self.root / 'tests' / 'fixtures' / 'nested'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        self.module('test_never.py', "raise AssertionError('must not import')\n")
        result = self.run_verifier()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('tests/fixtures/nested', result.stdout)
        self.assertNotIn('must not import', result.stdout)

    def test_symlinked_package_refused(self):
        package = self.root / 'external_package'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (self.root / 'tests' / 'linked').symlink_to(package, target_is_directory=True)
        result = self.run_verifier()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('tests/linked', result.stdout)

    def test_package_below_linked_plain_directory_and_cycle(self):
        external = self.root / 'external'
        (external / 'pkg').mkdir(parents=True)
        marker = external / 'pkg' / '__init__.py'
        marker.write_text('')
        (external / 'cycle').symlink_to(external, target_is_directory=True)
        (self.root / 'tests' / 'linked').symlink_to(external, target_is_directory=True)
        self.module('test_ok.py')
        result = self.run_verifier()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('tests/linked/pkg', result.stdout)
        marker.unlink()
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_non_module_filename_does_not_expand_pattern(self):
        self.module('test_a.py')
        self.module('test_[ab].py', "raise AssertionError('not an importable module')\n")
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.summary(result)['tests'], 1)
        self.assertEqual(self.summary(result)['modules'], 1)

    def test_existing_startup_and_environment_preserved(self):
        startup = self.root / 'startup'
        startup.mkdir()
        (startup / 'sitecustomize.py').write_text(
            "import os\nos.environ['E1_STARTUP'] = os.environ.get('E1_STARTUP', '') + 'ran'\n")
        self.module('test_startup.py', PASS + """\
import os, sys
assert os.environ['E1_STARTUP'] == 'ranran'
assert sys.path[0] == os.path.abspath('tests'), sys.path
assert sys.path[1] == os.getcwd(), sys.path
assert sys.path[2] == __EXPECTED_STARTUP__, sys.path
assert os.environ['PYTHONPATH'] == __EXPECTED_STARTUP__
""".replace('__EXPECTED_STARTUP__', repr(str(startup))))
        with mock.patch.dict(os.environ, {'PYTHONPATH': str(startup), 'E1_STARTUP': ''}):
            result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.summary(result)['tests'], 1)

    def test_sorted_complete_output_under_four_workers(self):
        for index, name in enumerate(('test_d.py', 'test_c.py', 'test_b.py', 'test_a.py')):
            self.module(name, '''\
                import sys, time, unittest
                class TestOutput(unittest.TestCase):
                    def test_output(self):
                        print('BEGIN {name}', flush=True)
                        time.sleep({delay})
                        print('END {name}', file=sys.stderr, flush=True)
            '''.format(name=name, delay=index * 0.08))
        result = self.run_verifier(workers='4')
        self.assertEqual(result.returncode, 0, result.stdout)
        names = re.findall(r'^=== (.+) ===$', result.stdout, re.MULTILINE)
        self.assertEqual(names, ['test_a.py', 'test_b.py', 'test_c.py', 'test_d.py'])
        chunks = re.split(r'^=== .+ ===\n', result.stdout, flags=re.MULTILINE)[1:]
        for name, chunk in zip(names, chunks):
            self.assertEqual(re.findall(r'(?:BEGIN|END) (test_\w+.py)', chunk), [name, name])
            self.assertIn('Ran 1 test', chunk)
        self.summary(result)

    def test_empty_modules_listed_and_nonpackage_subdirs_ignored(self):
        self.module('test_empty.py', '# no tests\n')
        ignored = self.root / 'tests' / 'data'
        ignored.mkdir()
        (ignored / 'test_ignored.py').write_text("raise AssertionError('not discovered')\n")
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('test_empty.py: discovered zero tests', result.stdout)
        self.assertEqual(self.summary(result)['tests'], 0)
        self.assertEqual(self.summary(result)['modules'], 1)

    def test_discovered_tests_silently_not_run_fail(self):
        self.module('test_silent.py', PASS + '''\
class Silent(unittest.TestSuite):
    def run(self, result, debug=False):
        return result
def load_tests(loader, tests, pattern):
    return Silent(tests)
''')
        result = self.run_verifier()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('discovery found 1 tests but child ran zero', result.stdout)

    def test_load_tests_hook_and_discovery_import_identity(self):
        self.module('test_hook.py', PASS + '''\
def load_tests(loader, tests, pattern):
    assert __name__ == 'test_hook', __name__
    assert pattern == 'test_hook.py', pattern
    return tests
''')
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.summary(result)['tests'], 1)

    @unittest.skipIf(sys.version_info >= (3, 12), 'exit 5 denotes empty discovery on 3.12+')
    def test_unexpected_exit5_on_older_python(self):
        self.module('test_empty.py',
                    'import atexit, os\natexit.register(lambda: os._exit(5))\n')
        result = self.run_verifier()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('child exited 5', result.stdout)

    def test_timeout_fails_with_output(self):
        self.module('test_timeout.py', "import time\nfrom pathlib import Path\n"
                    "time.sleep(0.3)\nprint('before timeout', flush=True)\n"
                    "Path('ready').touch()\ntime.sleep(10)\n")
        result = self.run_verifier(patch='''\
import time
v.MODULE_TIMEOUT = 0.2
original_wait = v.subprocess.Popen.wait
def after_ready(self, timeout=None):
    if timeout is not None:
        deadline = time.monotonic() + 10
        while not v.Path('ready').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    return original_wait(self, timeout=timeout)
v.subprocess.Popen.wait = after_ready
''')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('before timeout', result.stdout)
        self.assertIn('child timed out', result.stdout)

    def test_nested_runner_report_not_double_counted(self):
        self.module('test_nested.py', '''\
            import sys, unittest
            class TestNested(unittest.TestCase):
                def test_nested(self):
                    print('Ran 17 tests in 0.1s\\n\\nOK (skipped=2)', file=sys.stderr)
        ''')
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.summary(result)['tests'], 1)
        self.assertEqual(self.summary(result)['skips'], 0)

    def test_pinned_digest(self):
        policy = json.loads((ROOT / 'verifiers.json').read_text())
        entry = next(v for v in policy['verifiers'] if v['name'] == 'merge-precondition')
        self.assertEqual(entry['sha256'], hashlib.sha256(VERIFIER.read_bytes()).hexdigest())
        self.assertEqual(entry['claims'], ['integration-tests'])


if __name__ == '__main__':
    unittest.main()
