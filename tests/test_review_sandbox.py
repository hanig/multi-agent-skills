"""Exercise the process sandbox through its real worker and parent audit."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests import review_sandbox_worker as sandbox


class TestReviewSandbox(unittest.TestCase):
    def test_review_module_runs_once_in_supervised_worker(self):
        report = sandbox.review_report()
        self.assertIs(report, sandbox.review_report())
        self.assertTrue(report["collected"])
        skips = report["skipped"]
        print("\nDelegated test_review: %d tests, %.3fs, %d skips" % (
            len(report["collected"]), report["elapsed_seconds"], len(skips)))
        for outcome in skips:
            print("  SKIP %s: %s" % (outcome["id"], outcome["reason"]))
        # Optional validation artifact, written by the parent only after audit.
        destination = os.environ.get("HANIG_REVIEW_SANDBOX_REPORT")
        if destination:
            Path(destination).write_text(json.dumps(report, indent=2))


class TestSandboxRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.module = self.root / "delegated_probe.py"

    def run_module(self, source, timeout=30):
        self.module.write_text(source)
        return sandbox.run_sandbox(self.module, "delegated_probe", timeout)

    def preamble(self):
        return """import importlib.util, os, unittest
from pathlib import Path
spec = importlib.util.spec_from_file_location('journal', %r)
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)
cached_path = journal.review_journal_path()
def write(path=None):
    return journal.append_review_journal(
        cached_path if path is None else path, 'implementation', 1,
        ['offline'], 'REVIEW_PASS', [journal.HONEST_RUN_CLAIM])
""" % str(ROOT / "skills/hanig-review-gate/scripts/review.py")

    def simple_suite(self, body="pass"):
        return "import unittest\nclass Probe(unittest.TestCase):\n    def test_probe(self):\n        " + body + "\n"

    def test_import_and_load_tests_writers_cannot_touch_outer_journal(self):
        outer = self.root / "operator"
        seed = outer / "hanig-review-gate/seed/record.jsonl"
        seed.parent.mkdir(parents=True)
        seed.write_bytes(sandbox.SEED)
        before = sandbox.manifest(outer)
        for source in (
                self.preamble() + "write()\n" + self.simple_suite(),
                self.preamble() + self.simple_suite() +
                "def load_tests(loader, tests, pattern):\n    write()\n    return tests\n"):
            with self.subTest(source=source):
                with patch.dict(os.environ, HOME=str(self.root), XDG_STATE_HOME=str(outer)):
                    with self.assertRaisesRegex(sandbox.SandboxFailure, "manifest changed"):
                        self.run_module(source)
                self.assertEqual(sandbox.manifest(outer), before)
                self.assertEqual(seed.read_bytes(), sandbox.SEED)

    def test_import_cached_journal_path_resolves_inside_fake_root(self):
        source = self.preamble() + self.simple_suite(
            "self.assertTrue(cached_path.is_relative_to(Path(os.environ['HOME']).parent))")
        report = self.run_module(source)
        self.assertEqual(report["outcomes"][0]["status"], "success")

    def test_function_and_foreign_module_cases_remain_sandboxed(self):
        bodies = (
            "    return unittest.TestSuite([unittest.FunctionTestCase(write)])\n",
            "    import foreign_probe\n    foreign_probe.write = write\n"
            "    return loader.loadTestsFromModule(foreign_probe)\n",
        )
        (self.root / "foreign_probe.py").write_text(
            "import unittest\nclass Foreign(unittest.TestCase):\n"
            "    def test_write(self):\n        write()\n")
        for body in bodies:
            with self.subTest(body=body):
                source = self.preamble() + "def load_tests(loader, tests, pattern):\n" + body
                with self.assertRaisesRegex(sandbox.SandboxFailure, "manifest changed"):
                    self.run_module(source)

    def test_escaping_writer_is_rejected_by_parent_comparison(self):
        with self.assertRaisesRegex(sandbox.SandboxFailure, "manifest changed"):
            self.run_module(self.preamble() + self.simple_suite("write()"))

    def test_honest_isolated_write_passes_byte_identical_seed(self):
        source = self.preamble() + "import tempfile\n" + self.simple_suite(
            "write(Path(tempfile.mkdtemp()) / 'isolated-journal')")
        report = self.run_module(source)
        self.assertEqual(report["collected"], ["delegated_probe.Probe.test_probe"])
        self.assertEqual([o["status"] for o in report["outcomes"]], ["success"])

    def test_unjoined_thread_after_suite_is_never_a_silent_pass(self):
        source = self.preamble() + """import threading, time
def delayed():
    time.sleep(0.5)
    write()
""" + self.simple_suite("threading.Thread(target=delayed).start()")
        with self.assertRaisesRegex(sandbox.SandboxFailure, "surviving worker threads"):
            self.run_module(source)

    def test_delayed_subprocess_is_failed_and_group_quiescent(self):
        group_file = self.root / "group"
        source = self.preamble() + "import subprocess, sys\n" + self.simple_suite(
            "Path(%r).write_text(str(os.getpgrp())); "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"
            % str(group_file))
        with self.assertRaisesRegex(sandbox.SandboxFailure, "surviving worker descendants"):
            self.run_module(source)
        self.assertEqual(sandbox.session_members(int(group_file.read_text())), [])

    def test_import_failure_requires_completion_handshake(self):
        with self.assertRaisesRegex(sandbox.SandboxFailure, "ImportError: delegated import failed"):
            self.run_module("raise ImportError('delegated import failed')\n")

    def test_setup_module_failure_forwards_fixture_traceback(self):
        source = self.simple_suite() + "def setUpModule():\n    raise RuntimeError('setup failed')\n"
        with self.assertRaisesRegex(sandbox.SandboxFailure, "setUpModule.*delegated_probe") as raised:
            self.run_module(source)
        self.assertIn("RuntimeError: setup failed", str(raised.exception))
        self.assertIn("delegated_probe.Probe.test_probe: not_run", str(raised.exception))

    def test_whole_module_skip_is_failure_but_individual_skips_are_reported(self):
        for source in (
                "import unittest\nraise unittest.SkipTest('module skipped')\n",
                self.simple_suite() + "def setUpModule():\n    raise unittest.SkipTest('module skipped')\n",
                self.simple_suite("self.skipTest('module skipped')")):
            with self.subTest(source=source):
                with self.assertRaises(sandbox.SandboxFailure):
                    self.run_module(source)
        report = self.run_module(self.simple_suite() +
            "    def test_skipped(self):\n        self.skipTest('individual skip')\n")
        self.assertEqual([o["status"] for o in report["outcomes"]], ["success", "skip"])
        self.assertEqual(report["outcomes"][1]["traceback"], "individual skip")

    def test_partial_fixture_and_subtest_skips_do_not_reject_honest_suite(self):
        source = self.simple_suite() + """    def test_subtest(self):
        with self.subTest(value=1):
            self.skipTest('one subtest')
        with self.subTest(value=2):
            self.assertTrue(True)
class Skipped(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise unittest.SkipTest('one class fixture')
    def test_unrun(self):
        self.fail('skipped class ran')
"""
        report = self.run_module(source)
        self.assertEqual([o["status"] for o in report["outcomes"]],
                         ["success", "success", "skip"])
        self.assertEqual({o["reason"] for o in report["skipped"]},
                         {'one subtest', 'one class fixture'})

    def test_nested_journal_timeout_is_not_a_pass(self):
        with self.assertRaisesRegex(sandbox.SandboxFailure, "worker timeout"):
            self.run_module("import time\ntime.sleep(60)\n", timeout=0.3)

    def test_worker_signal_death_is_not_a_pass(self):
        with self.assertRaisesRegex(sandbox.SandboxFailure, "worker exit -%d" % signal.SIGTERM):
            self.run_module("import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n")

    def test_missing_handshake_with_zero_exit_is_failure(self):
        with self.assertRaisesRegex(sandbox.SandboxFailure, "completion handshake failed"):
            self.run_module("import os\nos._exit(0)\n")

    def test_worker_failures_errors_and_subtests_forward_ids_and_tracebacks(self):
        source = self.simple_suite("self.fail('first failure')") + """    def test_error(self):
        raise ValueError('second failure')
    def test_subtest(self):
        with self.subTest(value=3):
            self.fail('subtest failure')
"""
        with self.assertRaises(sandbox.SandboxFailure) as raised:
            self.run_module(source)
        for method, detail in (("test_probe", "AssertionError: first failure"),
                               ("test_error", "ValueError: second failure"),
                               ("test_subtest", "AssertionError: subtest failure")):
            self.assertIn("delegated_probe.Probe." + method, str(raised.exception))
            self.assertIn(detail, str(raised.exception))
        self.assertIn("Traceback", str(raised.exception))

    def test_recursive_manifest_refuses_missing_extra_unreadable_and_symlink_entries(self):
        for action, message in (
                ("seed.unlink()", "manifest changed"),
                ("seed.write_bytes(b'changed')", "manifest changed"),
                ("(seed.parent / 'extra').write_bytes(b'extra')", "manifest changed"),
                ("(seed.parent / 'empty').mkdir()", "manifest changed"),
                ("seed.chmod(0)", "unreadable file"),
                ("seed.parent.chmod(0)", "unreadable directory"),
                ("seed.unlink(); seed.symlink_to('/nonexistent')", "unexpected entry type"),
                ("seed.unlink(); os.mkfifo(seed)", "unexpected entry type")):
            with self.subTest(action=action):
                source = self.preamble() + self.simple_suite(
                    "seed = Path(os.environ['XDG_STATE_HOME']) / 'hanig-review-gate/seed/record.jsonl'; "
                    + action)
                with self.assertRaisesRegex(sandbox.SandboxFailure, message):
                    self.run_module(source)

    def test_bad_protocol_and_incomplete_outcomes_fail(self):
        for data in ("not json", json.dumps({"version": 99}),
                     json.dumps({"version": 1, "module": "delegated_probe",
                                 "collected": ["missing"], "outcomes": []})):
            with self.subTest(data=data):
                source = "import atexit, sys\nfrom pathlib import Path\n" + self.simple_suite() + (
                    "atexit.register(lambda: Path(sys.argv[3]).write_text(%r))\n" % data)
                with self.assertRaisesRegex(sandbox.SandboxFailure, "completion handshake failed"):
                    self.run_module(source)

    def test_normal_discovery_delegates_without_running_review_tests(self):
        env = dict(os.environ)
        env.pop(sandbox.WORKER_MARKER, None)
        code = """import unittest
suite = unittest.TestLoader().discover('tests', pattern='test_review.py')
assert suite.countTestCases() == 0, suite.countTestCases()
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_new_process_group_in_worker_session_is_failed_and_killed(self):
        identity_file = self.root / "escaped-group.json"
        child = """import json, os, signal, sys, time
from pathlib import Path
os.setpgid(0, os.getpid())
signal.signal(signal.SIGTERM, signal.SIG_IGN)
identity = Path(sys.argv[1])
ready = identity.with_suffix('.pending')
ready.write_text(json.dumps([os.getpid(), os.getpgrp(), os.getsid(0)]))
ready.replace(identity)
time.sleep(60)
"""
        source = "import json, os, subprocess, sys, time\nfrom pathlib import Path\n" + self.simple_suite(
            "child = subprocess.Popen([sys.executable, '-c', %r, %r]); "
            "deadline = time.monotonic() + 10\n"
            "        while not Path(%r).exists() and time.monotonic() < deadline:\n"
            "            time.sleep(0.01)\n"
            "        self.assertTrue(Path(%r).exists(), 'child did not become ready')\n"
            "        pid, pgid, sid = json.loads(Path(%r).read_text())\n"
            "        self.assertEqual(pid, pgid)\n"
            "        self.assertNotEqual(pgid, os.getpgrp())\n"
            "        self.assertEqual(sid, os.getsid(0))" % (
                child, str(identity_file), str(identity_file),
                str(identity_file), str(identity_file)))
        try:
            with self.assertRaisesRegex(sandbox.SandboxFailure, "surviving worker descendants"):
                self.run_module(source)
            pid, pgid, sid = json.loads(identity_file.read_text())
            self.assertEqual(sandbox.session_members(sid), [])
            # Check the particular child independently of the supervisor's
            # session filter, so reverting that filter cannot hide a survivor.
            result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                    capture_output=True, text=True, timeout=10)
            self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith("Z"),
                            "new-group child is still alive: " + result.stdout)
        finally:
            # Also contain the intentionally broken implementation during the
            # mutation run, where the supervisor misses this group entirely.
            if identity_file.exists():
                pid, pgid, sid = json.loads(identity_file.read_text())
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_unrelated_same_uid_process_churn_does_not_fail_a_worker(self):
        ready = self.root / "churn"
        churn = """import os, subprocess, sys, time
from pathlib import Path
counter = Path(sys.argv[1])
for count in range(10000):
    subprocess.run([sys.executable, '-c', 'pass'], check=True)
    temporary = counter.with_suffix('.pending')
    temporary.write_text(str(count))
    temporary.replace(counter)
    time.sleep(0.005)
"""
        process = subprocess.Popen([sys.executable, "-c", churn, str(ready)],
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "churn did not start")
            before = int(ready.read_text())
            report = self.run_module("import time\n" + self.simple_suite("time.sleep(0.5)"))
            self.assertEqual(report["outcomes"][0]["status"], "success")
            self.assertGreater(int(ready.read_text()), before)
            self.assertIsNone(process.poll(), "unrelated process was killed")
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)

    def discovery_probe(self):
        # Exercise TestSuite.run, not just its count: a bare-marker mutation
        # dispatches real test_review TestCases to this spy in the parent.
        return """import json, unittest
ran = []
original = unittest.TestCase.run
def traced(case, result=None):
    if type(case).__module__ == 'test_review':
        ran.append(case.id())
        result.startTest(case)
        result.addSuccess(case)
        result.stopTest(case)
        return result
    return original(case, result)
unittest.TestCase.run = traced
suite = unittest.TestLoader().discover('tests', pattern='test_review.py')
result = unittest.TestResult()
suite.run(result)
print(json.dumps({'ran': ran, 'count': result.testsRun, 'errors': result.errors}))
"""

    def test_inherited_or_stale_marker_never_runs_review_tests_in_parent(self):
        for marker in ("1", "stale-token", "0" * 64):
            with self.subTest(marker=marker):
                env = dict(os.environ, HANIG_REVIEW_SANDBOX_WORKER=marker,
                           HANIG_REVIEW_SANDBOX_ROOT=str(self.root / "missing"))
                result = subprocess.run([sys.executable, "-c", self.discovery_probe()],
                                        cwd=ROOT, env=env, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = json.loads(result.stdout)
                self.assertEqual(observed["ran"], [], "review tests ran in the parent")
                self.assertEqual(observed["count"], 0)
                self.assertEqual(observed["errors"], [])

    def test_live_worker_token_inherited_by_subprocess_still_delegates(self):
        source = "import json, subprocess, sys\n" + self.simple_suite(
            "result = subprocess.run([sys.executable, '-c', %r], "
            "capture_output=True, text=True, timeout=30); "
            "self.assertEqual(result.returncode, 0, result.stdout + result.stderr); "
            "self.assertEqual(json.loads(result.stdout)['ran'], [])" % self.discovery_probe())
        report = self.run_module(source)
        self.assertEqual(report["outcomes"][0]["status"], "success")

    def test_worker_load_tests_requires_matching_token_and_contained_homes(self):
        source = """import os, unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_review
from tests import review_sandbox_worker as worker
class Probe(unittest.TestCase):
    def test_context(self):
        sentinel = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        loader = unittest.TestLoader()
        self.assertIs(test_review.load_tests(loader, sentinel, None), sentinel)
        for field, value in ((worker.WORKER_MARKER, 'stale'),
                             (worker.WORKER_ROOT, '/nonexistent'),
                             ('HOME', '/'), ('XDG_STATE_HOME', '/')):
            with self.subTest(field=field), patch.dict(os.environ, {field: value}):
                self.assertEqual(test_review.load_tests(loader, sentinel, None).countTestCases(), 0)
        token_file = Path(os.environ[worker.WORKER_ROOT]) / 'worker-token'
        original = token_file.read_bytes()
        try:
            token_file.write_text('wrong token')
            self.assertEqual(test_review.load_tests(loader, sentinel, None).countTestCases(), 0)
        finally:
            token_file.write_bytes(original)
"""
        report = self.run_module(source)
        self.assertEqual(report["outcomes"][0]["status"], "success")

    def test_cached_failure_does_not_rerun_worker(self):
        with patch.object(sandbox, "_REVIEW_RUN", None), patch.object(
                sandbox, "run_sandbox", side_effect=sandbox.SandboxFailure("failed once")) as run:
            for _ in range(2):
                with self.assertRaisesRegex(sandbox.SandboxFailure, "failed once"):
                    sandbox.review_report()
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
