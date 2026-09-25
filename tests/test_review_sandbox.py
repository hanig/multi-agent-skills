"""Exercise the process sandbox through its real worker and parent audit."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
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

    def test_worker_pid_is_reserved_until_last_session_scan(self):
        # No OS identities or signals: reaping releases this fake PID to an
        # unrelated session leader. The old parent then counts and signals it.
        events = []
        self.addCleanup(self.assertNotIn, "counted stranger", events)
        self.addCleanup(self.assertNotIn, "signalled stranger", events)
        class Worker:
            pid = 43210
            returncode = None

            def wait(self, timeout=None):
                events.append("reap")
                self.returncode = 0
                return 0

            def poll(self):
                return self.wait()

        worker = Worker()
        stranger = [(worker.pid, worker.pid)]

        def launch(argv, **kwargs):
            Path(argv[-1]).write_text(json.dumps({
                "version": 1, "module": "delegated_probe",
                "collected": ["delegated_probe.Probe.test_probe"],
                "outcomes": [{"id": "delegated_probe.Probe.test_probe",
                              "status": "success", "traceback": ""}],
                "problems": [], "threads": [], "skipped": []}))
            return worker

        def members(sid):
            self.assertEqual(sid, worker.pid)
            events.append("scan")
            if worker.returncode is not None and stranger:
                events.append("counted stranger")
            return list(stranger) if worker.returncode is not None else []

        def signal_stranger(*args):
            events.append("signalled stranger")
            stranger.clear()

        def observe(pid):
            self.assertEqual(pid, worker.pid)
            events.append("observe exit without reap")
            return True

        with patch.object(sandbox.subprocess, "Popen", side_effect=launch), \
                patch.object(sandbox, "nonreaping_waiter", create=True,
                             return_value=observe), \
                patch.object(sandbox, "session_members", side_effect=members), \
                patch.object(sandbox.os, "killpg", side_effect=signal_stranger) as groups, \
                patch.object(sandbox.os, "kill", side_effect=signal_stranger) as pids, \
                patch.object(sandbox.os, "getsid", return_value=worker.pid):
            report = self.run_module(self.simple_suite())
        self.assertEqual(report["outcomes"][0]["status"], "success")
        self.assertEqual(events.count("reap"), 1, events)
        self.assertEqual(events[-1], "reap", events)
        self.assertGreaterEqual(events.count("scan"), 2, events)
        self.assertIn("observe exit without reap", events)
        groups.assert_not_called()
        pids.assert_not_called()

    def test_native_exit_observation_keeps_worker_waitable(self):
        observe = sandbox.nonreaping_waiter()
        worker = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"],
                                  start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not observe(worker.pid):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            # A second waitid must still return the same child's exit; no
            # poll/wait call is allowed to consume it until after the scan.
            self.assertTrue(observe(worker.pid))
            self.assertIsNone(worker.returncode)
            self.assertEqual(sandbox.session_members(worker.pid), [])
            self.assertEqual(worker.wait(timeout=10), 7)
            with self.assertRaises(ChildProcessError):
                observe(worker.pid)
        finally:
            if worker.returncode is None:
                worker.kill()
                worker.wait(timeout=10)

    def test_missing_waitid_refuses_before_launch(self):
        with patch.object(sandbox.os, "waitid", None, create=True), \
                patch.object(sandbox.sys, "platform", "unsupported"), \
                patch.object(sandbox.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(sandbox.SandboxFailure, "waitid.*WNOWAIT"):
                self.run_module(self.simple_suite())
        launch.assert_not_called()

    def test_os_waitid_uses_nowait_for_running_and_exited_worker(self):
        with patch.object(sandbox.os, "waitid", create=True,
                          side_effect=[None, SimpleNamespace(si_pid=43210)]) as waitid:
            observe = sandbox.nonreaping_waiter()
            self.assertFalse(observe(43210))
            self.assertTrue(observe(43210))
        self.assertEqual(waitid.call_count, 2)
        for args, kwargs in waitid.call_args_list:
            self.assertEqual(args, (os.P_PID, 43210,
                                   os.WEXITED | os.WNOWAIT | os.WNOHANG))
            self.assertEqual(kwargs, {})

    def test_containment_does_not_signal_stale_or_foreign_groups(self):
        for current_sid in (43210, 54321):
            with self.subTest(current_sid=current_sid):
                worker = SimpleNamespace(pid=43210, returncode=None)
                events = []

                def reap(timeout=None):
                    events.append("reap")
                    worker.returncode = 0

                def signal_member(pid, sig):
                    self.assertIsNone(worker.returncode)
                    events.append((pid, sig))

                worker.wait = reap
                # A ps snapshot's group number can already belong elsewhere.
                # Recheck the member's session and never use that group number.
                with patch.object(sandbox, "session_members", side_effect=[
                        [(43211, 54321)], []]), \
                        patch.object(sandbox.os, "getsid", return_value=current_sid), \
                        patch.object(sandbox.os, "kill", side_effect=signal_member) as kill, \
                        patch.object(sandbox.os, "killpg") as killpg:
                    sandbox.stop_session(worker, lambda pid: True)
                killpg.assert_not_called()
                if current_sid == worker.pid:
                    kill.assert_called_once_with(43211, signal.SIGTERM)
                else:
                    kill.assert_not_called()
                self.assertEqual(events[-1], "reap")

    def test_lost_child_reservation_refuses_without_scan_or_signal(self):
        worker = SimpleNamespace(pid=43210, returncode=None)
        with patch.object(sandbox, "session_members") as members, \
                patch.object(sandbox.os, "kill") as kill, \
                patch.object(sandbox.os, "killpg") as killpg:
            with self.assertRaises(ChildProcessError):
                sandbox.stop_session(worker, lambda pid: (_ for _ in ()).throw(
                    ChildProcessError("child was reaped outside supervisor")))
        members.assert_not_called()
        kill.assert_not_called()
        killpg.assert_not_called()

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
        churn = """import itertools, sys, time
from pathlib import Path
counter = Path(sys.argv[1])
# One unrelated process makes observable progress; PID reuse is injected in
# test_worker_pid_is_reserved_until_last_session_scan, never forced by churn.
for count in itertools.count():
    temporary = counter.with_suffix('.pending')
    temporary.write_text(str(count))
    temporary.replace(counter)
    time.sleep(0.02)
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
