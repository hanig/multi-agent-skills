"""ARC-725: watcher exits must reach status and durable coordinator state."""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S


class TestTerminalWatcherOutcome(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "state"
        self.attempt = self.root / "runs" / "code" / "attempt1"
        self.attempt.mkdir(parents=True)
        self.plan = {"name": "watch-test", "units": [{
            "id": "code", "kind": "code", "repo": str(self.root / "repo"),
            "mode": "full-access", "target_branch": "main",
            "outputs": ["out.txt"], "prompt": "fixture"}]}
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan))
        self.state = {"units": {"code": {
            "state": "SUBMITTED", "attempt_dir": str(self.attempt),
            "attempts": [str(self.attempt)], "job_id": "agent1",
            "gpu_hours": 0.0}}, "halted": None}
        self.args = Namespace(
            plan=str(self.plan_path), state_dir=str(self.state_dir),
            root=str(self.root / "runs"), unit="code",
            attempt=self.attempt.name, agent="agent1")
        S.save_state(self.state_dir, self.state)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        ps = shutil.which("ps")
        self.assertIsNotNone(ps)
        (self.bin / "ps").symlink_to(ps)
        self.env = dict(os.environ, PATH=str(self.bin))
        self.paseo = self.bin / "paseo"
        self.paseo.write_text(
            "#!" + sys.executable + "\n"
            "import os, sys, time\nfrom pathlib import Path\n"
            "root = Path(" + repr(str(self.root)) + ")\n"
            "assert sys.argv[1:] == ['wait', 'agent1', '--json']\n"
            "(root / 'wait-pid').write_text(str(os.getpid()))\n"
            "while not (root / 'release').exists(): time.sleep(0.02)\n"
            "rc = int((root / 'release').read_text())\n"
            "print('idle' if rc == 0 else 'fixture wait failed', flush=True)\n"
            "sys.exit(rc)\n")
        self.paseo.chmod(0o755)
        self.proc = None
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=10)
        pid_path = self.root / "wait-pid"
        if (pid_path.exists() and self.proc is not None
                and self.proc.returncode == -signal.SIGKILL):
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        S.release_lease(self.state_dir)

    def until(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("watcher fixture did not reach the expected state")

    def start(self):
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            self.proc = real_popen(*args, **kwargs)
            return self.proc

        ok, why = S.acquire_lease(self.state_dir)
        self.assertTrue(ok, why)
        try:
            with mock.patch.dict(os.environ, self.env, clear=True), \
                    mock.patch.object(S.subprocess, "Popen", side_effect=spawn):
                self.assertTrue(S._start_code_terminal_watchers(
                    self.plan, self.state, self.args, []))
        finally:
            S.release_lease(self.state_dir)
        self.until(lambda: (self.root / "wait-pid").exists())
        return self.proc

    def status(self, as_json=True):
        argv = [sys.executable, str(SCRIPTS / "swarm.py"), "status",
                str(self.plan_path), "--state-dir", str(self.state_dir)]
        if as_json:
            argv.append("--json")
        result = subprocess.run(argv, env=self.env, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, S.EXIT_OK, result.stderr)
        return (json.loads(result.stdout) if as_json else result.stdout)

    def saved_watch(self, attempt="attempt1"):
        return S.load_state(self.state_dir)["units"]["code"][
            "code_terminal_watches"][attempt]

    def seed_watch(self, **updates):
        watch = {"agent_id": "agent1", "host": os.uname().nodename,
                 "status": "waiting", "pid": os.getpid(),
                 "log": str(self.attempt / S.CODE_TERMINAL_WATCH_LOG)}
        watch.update(updates)
        self.state["units"]["code"]["code_terminal_watches"] = {
            self.attempt.name: watch}
        S.save_state(self.state_dir, self.state)
        return watch

    def test_killed_watcher_is_consumed_by_status_and_persisted(self):
        proc = self.start()
        proc.kill()
        proc.wait(timeout=10)
        report = self.status()
        row = report["units"][0]
        self.assertEqual(row["state"], "SUBMITTED")
        self.assertEqual(report["needs_attention"], [])
        self.assertIsNone(report["halted"])
        self.assertEqual(row["terminal_watch"]["status"], "exited_without_idle")
        watch = self.saved_watch()
        self.assertEqual(watch["status"], "exited_without_idle")
        self.assertIsNone(watch["wait_exit_code"])
        self.assertIn("no idle observation", watch["reason"])
        self.assertTrue(watch["observed_at"])
        rendered = self.status(False)
        self.assertIn("terminal watcher: exited_without_idle", rendered)
        self.assertIn("Scheduled advance remains the fallback", rendered)

    def test_zombie_watcher_is_not_reported_as_present(self):
        proc = self.start()
        proc.kill()
        # Keep it unreaped while the real status CLI consumes ps's Z state.
        self.until(lambda: subprocess.run(
            [str(self.bin / "ps"), "-p", str(proc.pid), "-o", "stat="],
            capture_output=True, text=True).stdout.strip().startswith("Z"))
        self.assertEqual(self.status()["units"][0]["terminal_watch"]["status"],
                         "exited_without_idle")
        self.assertIn("zombie", self.saved_watch()["reason"])

    def test_nonzero_wait_records_exact_exit_and_tail_without_status_poll(self):
        proc = self.start()
        (self.root / "release").write_text("17")
        self.assertEqual(proc.wait(timeout=10), S.EXIT_HALTED)
        watch = self.saved_watch()
        self.assertEqual(watch["status"], "exited_without_idle")
        self.assertEqual(watch["wait_exit_code"], 17)
        self.assertIn("fixture wait failed", watch["log_tail"])
        self.assertIn("exit 17", self.status(False))

    def test_killed_wait_child_records_signal_exit(self):
        proc = self.start()
        os.kill(int((self.root / "wait-pid").read_text()), signal.SIGKILL)
        self.assertEqual(proc.wait(timeout=10), S.EXIT_HALTED)
        self.assertEqual(self.saved_watch()["wait_exit_code"], -signal.SIGKILL)

    def test_live_watcher_remains_waiting_without_claiming_work_progress(self):
        self.start()
        watch = self.status()["units"][0]["terminal_watch"]
        self.assertEqual(watch["status"], "waiting")
        self.assertEqual(watch["process_state"], "present")
        self.assertIn("not work progress", watch["process_reason"])

    def set_watch_identity(self, attempt, agent):
        self.attempt = self.attempt.with_name(attempt)
        self.attempt.mkdir(exist_ok=True)
        self.args.attempt, self.args.agent = attempt, agent
        self.state["units"]["code"].update({
            "attempt_dir": str(self.attempt), "attempts": [str(self.attempt)],
            "job_id": agent})
        S.save_state(self.state_dir, self.state)
        self.paseo.write_text(self.paseo.read_text().replace(
            "['wait', 'agent1', '--json']", repr(["wait", agent, "--json"])))

    def test_live_watch_with_whitespace_attempt_is_unknown(self):
        self.set_watch_identity("attempt 1", "agent1")
        proc = self.start()
        watch = self.status()["units"][0]["terminal_watch"]
        self.assertIsNone(proc.poll())
        self.assertEqual(watch["status"], "waiting")
        self.assertEqual(watch["process_state"], "unknown")
        self.assertEqual(self.saved_watch("attempt 1")["status"], "waiting")

    def test_live_watch_with_whitespace_agent_is_unknown(self):
        self.set_watch_identity("attempt1", "agent 1")
        proc = self.start()
        watch = self.status()["units"][0]["terminal_watch"]
        self.assertIsNone(proc.poll())
        self.assertEqual(watch["status"], "waiting")
        self.assertEqual(watch["process_state"], "unknown")

    def test_killed_watch_with_whitespace_is_still_observed_as_gone(self):
        self.set_watch_identity("attempt 1", "agent 1")
        proc = self.start()
        proc.kill()
        proc.wait(timeout=10)
        watch = self.status()["units"][0]["terminal_watch"]
        self.assertEqual(watch["status"], "exited_without_idle")

    def test_legacy_waiting_record_is_repaired_and_log_tail_is_bounded(self):
        # This pid names the test runner, not the recorded watcher.
        watch = self.seed_watch()
        Path(watch["log"]).write_bytes(b"x" * 5000 + b"legacy crash\xff\n")
        self.status()
        saved = self.saved_watch()
        self.assertEqual(saved["status"], "exited_without_idle")
        self.assertIn("different command", saved["reason"])
        self.assertIn("legacy crash", saved["log_tail"])
        self.assertLessEqual(len(saved["log_tail"]), 4000)

    def test_foreign_host_and_missing_pid_are_unknown_not_dead(self):
        for fields in ({"host": "other-host"}, {"pid": None}):
            with self.subTest(fields=fields):
                self.seed_watch(**fields)
                watch = self.status()["units"][0]["terminal_watch"]
                self.assertEqual(watch["status"], "waiting")
                self.assertEqual(watch["process_state"], "unknown")
                self.assertIn("process unknown", self.status(False))

    def test_wait_exception_is_recorded(self):
        self.seed_watch()
        with mock.patch.object(S.U, "run", side_effect=OverflowError("fixture")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.cmd_watch_code_terminal(self.args), S.EXIT_HALTED)
        watch = self.saved_watch()
        self.assertEqual(watch["status"], "exited_without_idle")
        self.assertIn("OverflowError", watch["reason"])
        self.assertIsNone(watch["wait_exit_code"])

    def test_idle_is_recorded_before_the_normal_checker_runs(self):
        self.seed_watch()

        def check(*args, **kwargs):
            watch = self.saved_watch()
            self.assertEqual(watch["status"], "idle_observed")
            self.assertEqual(watch["wait_exit_code"], 0)
            self.assertEqual(kwargs, {"max_new": 0})
            return [], 0, None

        with mock.patch.object(S.U, "run", return_value=(0, "idle", "")), \
                mock.patch.object(S, "_load_plan", return_value=self.plan), \
                mock.patch.object(S, "advance", side_effect=check) as advance:
            self.assertEqual(S.cmd_watch_code_terminal(self.args), S.EXIT_OK)
        advance.assert_called_once()
        self.assertEqual(self.saved_watch()["status"], "checked")
        self.assertIn("terminal watcher: checked", self.status(False))

    def test_observation_survives_lock_contention_and_status_consumes_it(self):
        self.seed_watch()
        with mock.patch.object(S.U, "run", return_value=(0, "idle", "")), \
                mock.patch.object(S, "acquire_lease", return_value=(False, "busy")), \
                mock.patch.object(S.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.cmd_watch_code_terminal(self.args), S.EXIT_HALTED)
        self.assertEqual(self.saved_watch()["status"], "waiting")
        row = self.status()["units"][0]
        self.assertEqual(row["terminal_watch"]["status"], "idle_observed")
        self.assertIn("lock remained busy", row["terminal_watch"]["reason"])
        self.assertEqual(self.saved_watch()["status"], "idle_observed")

    def test_idle_for_old_attempt_does_not_check_the_new_attempt(self):
        self.seed_watch()
        self.state["units"]["code"]["attempt_dir"] = str(self.attempt.parent / "new")
        self.state["units"]["code"]["job_id"] = "new-agent"
        S.save_state(self.state_dir, self.state)
        with mock.patch.object(S.U, "run", return_value=(0, "idle", "")), \
                mock.patch.object(S, "advance") as advance, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.cmd_watch_code_terminal(self.args), S.EXIT_OK)
        advance.assert_not_called()
        self.assertEqual(self.saved_watch()["status"], "idle_observed")
        self.assertIn("obsolete", self.saved_watch()["reason"])
        self.assertIsNone(self.status()["units"][0]["terminal_watch"])

    def test_start_failures_are_visible_without_halting_the_unit(self):
        with mock.patch.object(S.subprocess, "Popen", side_effect=OSError("fixture")):
            self.assertFalse(S._start_code_terminal_watchers(
                self.plan, self.state, self.args, []))
        self.assertEqual(self.saved_watch()["status"], "start_failed")
        self.assertIn("fixture", self.status(False))

    def test_replaced_log_fifo_cannot_hang_status(self):
        watch = self.seed_watch()
        os.mkfifo(watch["log"])
        self.status()
        self.assertIn("not a regular file", self.saved_watch()["log_tail"])

    def test_scheduled_advance_still_runs_with_a_failed_watcher(self):
        self.seed_watch(status="exited_without_idle", reason="fixture")
        self.args.dry_run = False
        self.args.max_new_dispatches = 0
        real_popen = subprocess.Popen

        def no_watcher(argv, *args, **kwargs):
            self.assertNotIn("watch-code-terminal", argv)
            return real_popen(argv, *args, **kwargs)

        with mock.patch.object(S, "_load_plan", return_value=self.plan), \
                mock.patch.object(S, "advance", return_value=([], 0, None)) as advance, \
                mock.patch.object(S.subprocess, "Popen", side_effect=no_watcher), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.cmd_advance(self.args), S.EXIT_OK)
        advance.assert_called_once()
        self.assertEqual(S.load_state(self.state_dir)["units"]["code"]["state"],
                         "SUBMITTED")

    def test_unavailable_ps_reports_unknown_without_halting(self):
        self.start()
        (self.bin / "ps").unlink()
        watch = self.status()["units"][0]["terminal_watch"]
        self.assertEqual(watch["status"], "waiting")
        self.assertEqual(watch["process_state"], "unknown")

    def test_log_open_failure_is_persisted(self):
        (self.attempt / S.CODE_TERMINAL_WATCH_LOG).mkdir()
        self.assertFalse(S._start_code_terminal_watchers(
            self.plan, self.state, self.args, []))
        self.assertEqual(self.saved_watch()["status"], "start_failed")
        self.assertIn("terminal watcher: start_failed", self.status(False))

    def test_idle_survives_an_exception_in_the_checker(self):
        self.seed_watch()
        with mock.patch.object(S.U, "run", return_value=(0, "idle", "")), \
                mock.patch.object(S, "_load_plan", return_value=self.plan), \
                mock.patch.object(S, "advance", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                S.cmd_watch_code_terminal(self.args)
        self.assertEqual(self.saved_watch()["status"], "idle_observed")
        self.assertIn("RuntimeError", self.status(False))

    def test_status_under_contention_observes_without_clobbering_state(self):
        proc = self.start()
        proc.kill()
        proc.wait(timeout=10)
        ok, why = S.acquire_lease(self.state_dir)
        self.assertTrue(ok, why)
        try:
            before = (self.state_dir / S.STATE_FILE).read_bytes()
            self.assertEqual(self.status()["units"][0]["terminal_watch"]["status"],
                             "exited_without_idle")
            self.assertEqual((self.state_dir / S.STATE_FILE).read_bytes(), before)
        finally:
            S.release_lease(self.state_dir)
        self.status()
        self.assertEqual(self.saved_watch()["status"], "exited_without_idle")

    def test_checker_exception_does_not_persist_uncommitted_unit_state(self):
        self.seed_watch()

        def partial_check(_plan, current, *_args, **_kwargs):
            current["units"]["code"]["state"] = "FAILED"
            raise RuntimeError("uncommitted checker update")

        with mock.patch.object(S.U, "run", return_value=(0, "idle", "")), \
                mock.patch.object(S, "_load_plan", return_value=self.plan), \
                mock.patch.object(S, "advance", side_effect=partial_check):
            with self.assertRaises(RuntimeError):
                S.cmd_watch_code_terminal(self.args)
        self.assertEqual(S.load_state(self.state_dir)["units"]["code"]["state"],
                         "SUBMITTED")
        self.assertEqual(self.saved_watch()["status"], "idle_observed")

    def test_outcome_arriving_during_process_probe_is_not_lost(self):
        self.seed_watch()
        outcome = {"status": "idle_observed", "wait_exit_code": 0,
                   "reason": "idle before exit"}
        with mock.patch.object(S, "_read_code_terminal_outcome",
                               side_effect=[{}, outcome]), \
                mock.patch.object(S, "_code_terminal_process",
                                  return_value=("absent", "process gone")):
            with contextlib.redirect_stdout(io.StringIO()):
                args = Namespace(plan=str(self.plan_path),
                                 state_dir=str(self.state_dir), json=True)
                self.assertEqual(S.cmd_status(args), S.EXIT_OK)
        self.assertEqual(self.saved_watch()["status"], "idle_observed")


if __name__ == "__main__":
    unittest.main()
