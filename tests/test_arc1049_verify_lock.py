"""Verification releases the real plan lock and fences its journal publication."""

import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests import test_arc683_merge_precondition as preconditions

S, V = preconditions.S, preconditions.V


class TestVerificationLock(unittest.TestCase):
    # One budget covers startup, concurrent work and publication; scheduling
    # delays must not race several independent, much tighter fixture timers.
    WATCHDOG_SECONDS = 300

    def setUp(self):
        self.precondition = preconditions.TestMergePrecondition()
        self.precondition.setUp()
        self.addCleanup(self.precondition.doCleanups)
        self.f = self.precondition.f
        self.started = self.f.directory / "verifier-started"
        self.finish = self.f.directory / "verifier-finish"
        self.f.env.update(ARC1049_STARTED=str(self.started), ARC1049_FINISH=str(self.finish))
        program = ("#!" + sys.executable + "\n" + '''
import os, time
from pathlib import Path
Path(os.environ['ARC1049_STARTED']).write_text(str(Path.cwd()))
# The parent normally releases this barrier within its 300s budget. Keep a
# longer independent backstop if that supervisor dies before cleanup.
deadline = time.monotonic() + 600
while not Path(os.environ['ARC1049_FINISH']).exists():
    if time.monotonic() >= deadline:
        raise SystemExit('verifier barrier watchdog expired')
    time.sleep(0.02)
raise SystemExit(int(Path('target.fail').exists()))
''')
        policy = {"schema_version": 1, "verifiers": [{
            "name": V.MERGE_VERIFIER, "claims": [V.INTEGRATION_CLAIM],
            "sha256": hashlib.sha256(program.encode()).hexdigest()}]}
        self.target = self.precondition.target_commit({
            V.MERGE_VERIFIER_PATH: program, V.POLICY_FILE: json.dumps(policy)})
        self.before = (self.f.state_dir / S.VERIFY_RECEIPTS).read_bytes()

    def start(self):
        f = self.f
        command = [sys.executable, str(f.operator), str(f.plan_path),
                   "--state-dir", str(f.state_dir), "--unit", "u", "--pr", "7",
                   "--approver", "Operator", "--verify-integration"]
        self.deadline = time.monotonic() + self.WATCHDOG_SECONDS
        self.stdout = f.directory / "operator.stdout"
        self.stderr = f.directory / "operator.stderr"
        # Files let us diagnose an exited operator without waiting for an
        # inherited pipe in a stuck descendant to close.
        with self.stdout.open("w") as stdout, self.stderr.open("w") as stderr:
            self.process = subprocess.Popen(command, cwd=f.repo, env=f.env,
                                            stdout=stdout, stderr=stderr,
                                            start_new_session=True)
        self.addCleanup(self.stop)
        while not self.started.exists():
            if self.process.poll() is not None:
                self.fail("operator exited before verifier barrier (exit %s): %r" %
                          (self.process.returncode, self.output()))
            if not self.remaining():
                self.fail("verifier did not reach barrier within %ss watchdog: %r" %
                          (self.WATCHDOG_SECONDS, self.output()))
            time.sleep(0.02)
        self.assertNotEqual(Path(self.started.read_text()), f.repo)

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def output(self):
        return self.stdout.read_text(), self.stderr.read_text()

    def stop(self):
        if getattr(self, "_stopped", False):
            return
        self.finish.touch()
        try:
            self.process.wait(timeout=self.remaining())
        except subprocess.TimeoutExpired:
            # wait timed out without reaping: the child still owns this PID.
            # Never signal a group after a successful wait/poll freed its ID.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=10)
        self._stopped = True

    def complete(self):
        self.finish.touch()
        try:
            self.process.wait(timeout=self.remaining())
        except subprocess.TimeoutExpired:
            self.fail("operator did not complete within %ss watchdog: %r" %
                      (self.WATCHDOG_SECONDS, self.output()))
        stdout, stderr = self.output()
        self.assertEqual(self.f.calls(["pr", "merge"]), [])
        self.assertEqual(self.f.receipts(), [])
        return subprocess.CompletedProcess(self.process.args, self.process.returncode, stdout, stderr)

    def locked_change(self, action="pass"):
        """Another process takes the real flock without advancing the epoch.

        Direct writes isolate the binding guards from the epoch guard. The
        separate coordinator-acquisition test exercises the real epoch writer.
        """
        code = '''
import fcntl, json, os, pathlib
state = pathlib.Path(os.environ['COORDINATOR_STATE'])
forge_path = pathlib.Path(os.environ['FORGE_STATE'])
with (state / %r).open('r+') as lock:
    # The parent's shared watchdog bounds a genuinely held lock.
    fcntl.flock(lock, fcntl.LOCK_EX)
    exec(%r)
''' % (S.LOCK, action)
        result = subprocess.run([sys.executable, "-c", code], env=self.f.env,
                                capture_output=True, text=True, timeout=self.remaining())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(self.process.poll(), "verifier ended before concurrent lock attempt")

    def assert_stale(self, expected):
        result = self.complete()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(expected, result.stderr)
        self.assertIn("Rerun verification", result.stderr)
        self.assertIn("--verify-integration", result.stderr)
        self.assertEqual((self.f.state_dir / S.VERIFY_RECEIPTS).read_bytes(), self.before)

    def test_000_concurrent_lock_succeeds_and_honest_evidence_appends_once(self):
        # A nonzero starting epoch catches comparison against an absolute 1/2.
        (self.f.state_dir / S.STATE_EPOCH_FILE).write_text('{"epoch": 20}')
        self.start()
        self.locked_change()
        result = self.complete()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipts = S.load_verifications(self.f.state_dir)[0]
        self.assertEqual(len(receipts), 2)
        receipt = receipts[-1]
        for field, value in (("unit", "u"), ("subject_head", self.f.head),
                             ("produced_head", self.f.head), ("target_commit", self.target),
                             ("authorization_commit", self.target), ("result", "pass")):
            self.assertEqual(receipt[field], value)
        self.assertTrue((self.f.state_dir / S.VERIFY_RECEIPTS).read_bytes().startswith(self.before))
        self.assertEqual(S._read_state_epoch(self.f.state_dir), (22, None))
        self.assertEqual(self.f.calls(["api"]), [self.f.forge["ref_command"]] * 2)

    def test_operator_exit_before_barrier_is_diagnosed(self):
        operator = self.f.directory / "early-exit.py"
        operator.write_text("import sys; sys.stderr.write('fixture exit diagnostic'); sys.exit(7)\n")
        self.f.operator = operator
        with self.assertRaisesRegex(AssertionError,
                                    "operator exited before verifier barrier.*7.*fixture exit diagnostic"):
            self.start()
        with mock.patch.object(os, "killpg") as signal_group:
            self.stop()
        signal_group.assert_not_called()

    def test_watchdog_bounds_a_stuck_operator(self):
        operator = self.f.directory / "stuck.py"
        operator.write_text("import time; time.sleep(3600)\n")
        self.f.operator = operator
        self.WATCHDOG_SECONDS = 0.2
        with self.assertRaisesRegex(AssertionError, "verifier did not reach barrier.*watchdog"):
            self.start()
        self.stop()
        self.assertIsNotNone(self.process.poll(), "watchdog cleanup left the operator running")

    def test_verifier_has_an_independent_watchdog(self):
        program = self.f.git("show", self.target + ":" + V.MERGE_VERIFIER_PATH)
        # Execute the actual committed fixture without an operator supervising
        # it. Advance its clock beyond the deadline without waiting ten minutes.
        wrapper = '''
import time
ticks = iter((0.0, 601.0))
time.monotonic = lambda: next(ticks)
def unexpected_sleep(seconds):
    raise AssertionError('expired verifier polled again')
time.sleep = unexpected_sleep
exec(%r)
''' % program
        result = subprocess.run([sys.executable, "-c", wrapper], cwd=self.f.repo,
                                env=self.f.env, capture_output=True, text=True,
                                timeout=self.WATCHDOG_SECONDS)
        self.assertTrue(self.started.exists())
        self.assertFalse(self.finish.exists())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verifier barrier watchdog expired", result.stderr)

    def test_epoch_change_refuses_stale_evidence(self):
        self.start()
        self.locked_change('''
path = state / 'state-epoch.json'
record = json.loads(path.read_text())
record['epoch'] += 1
path.write_text(json.dumps(record))
''')
        self.assert_stale("state_epoch")

    def test_real_coordinator_acquisition_can_run_and_invalidates_observation(self):
        self.start()
        code = ("import sys; sys.path.insert(0, %r); import swarm as S; "
                "ok, reason = S.acquire_lease(%r); assert ok, reason; S.release_lease(%r)" %
                (str(preconditions.fixtures.SCRIPTS), str(self.f.state_dir), str(self.f.state_dir)))
        result = subprocess.run([sys.executable, "-c", code], env=self.f.env,
                                capture_output=True, text=True, timeout=self.remaining())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_stale("state_epoch")

    def test_judged_head_change_refuses_without_epoch_change(self):
        self.start()
        self.locked_change('''
path = state / 'swarm-state.json'
record = json.loads(path.read_text())
record['units']['u']['attempt_produced_heads']['a1'] = 'f' * 40
path.write_text(json.dumps(record))
forge = json.loads(forge_path.read_text())
forge['pr']['headRefOid'] = 'f' * 40
forge_path.write_text(json.dumps(forge))
''')
        self.assert_stale("binding changed during verification: head")

    def test_attempt_change_refuses_without_epoch_change(self):
        self.start()
        self.locked_change('''
path = state / 'swarm-state.json'
record = json.loads(path.read_text())
unit = record['units']['u']
unit['attempt_dir'] = str(pathlib.Path(unit['attempt_dir']).with_name('a2'))
for field in ('attempt_launch_intents', 'attempt_launch_facts'):
    unit[field]['a2'] = dict(unit[field]['a1'], attempt_id='a2', branch='swarm-a2',
                             judgment_ref='refs/heads/swarm-a2', worktree_slug='a2')
unit['attempt_produced_heads']['a2'] = unit['attempt_produced_heads']['a1']
path.write_text(json.dumps(record))
''')
        self.assert_stale("binding changed during verification: attempt")

    def test_target_tip_change_refuses_even_with_stale_pr_base(self):
        self.start()
        self.locked_change('''
forge = json.loads(forge_path.read_text())
forge['ref']['object']['sha'] = 'f' * 40
forge_path.write_text(json.dumps(forge))
''')
        self.assert_stale("target moved during verification")

    def test_forge_head_change_refuses(self):
        self.start()
        self.locked_change('''
forge = json.loads(forge_path.read_text())
forge['pr']['headRefOid'] = 'f' * 40
forge_path.write_text(json.dumps(forge))
''')
        self.assert_stale("PR head does not equal coordinator-judged head")

    def test_forge_merged_pr_refuses(self):
        self.start()
        self.locked_change('''
forge = json.loads(forge_path.read_text())
forge['pr']['state'] = 'MERGED'
forge_path.write_text(json.dumps(forge))
''')
        self.assert_stale("PR is no longer OPEN")

    def test_changed_target_ref_read_failure_refuses(self):
        self.start()
        self.locked_change('''
forge = json.loads(forge_path.read_text())
forge['ref_exit'] = 1
forge_path.write_text(json.dumps(forge))
''')
        self.assert_stale("target ref unavailable")

    def test_unresolved_intent_created_during_verification_refuses(self):
        self.start()
        binding = {"unit": "u", "attempt": "a1", "head": self.f.head,
                   "repo": self.f.remote, "target": "main", "pr": 7}
        operation = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        intent = {"binding": binding, "root": str(self.f.root), "phase": "merge_requested"}
        self.locked_change("(state / %r).write_text(%r)" %
                           ("merge-unit-" + operation + ".json", json.dumps(intent)))
        self.assert_stale("merge intent changed during verification")

    def test_plan_change_during_verification_refuses(self):
        self.start()
        self.f.unit["scope"] = ["**"]
        self.f.state["plan_digest"] = S.plan_digest(self.f.plan)
        self.locked_change("pathlib.Path(%r).write_text(%r)\n"
                           "(state / %r).write_text(%r)" %
                           (str(self.f.plan_path), json.dumps(self.f.plan),
                            S.STATE_FILE, json.dumps(self.f.state)))
        self.assert_stale("plan changed during verification")

    def test_corrupt_journal_during_verification_is_not_appended_to(self):
        self.start()
        damaged = self.before + b'{broken}\n'
        self.locked_change("(state / %r).write_bytes(%r)" % (S.VERIFY_RECEIPTS, damaged))
        self.before = damaged
        self.assert_stale("verification journal cannot be read in full")


if __name__ == "__main__":
    unittest.main()
