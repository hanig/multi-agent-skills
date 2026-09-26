"""Merge evidence results and reconciliation through real CLIs and stub gh."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_arc683_shared_guard as fixtures

S, V = fixtures.S, fixtures.V


class TestEvidenceSemantics(unittest.TestCase):
    def setUp(self):
        self.shared = fixtures.TestSharedGuard()
        self.shared.setUp()
        self.addCleanup(self.shared.doCleanups)
        self.f = self.shared.f
        self.started = self.f.directory / "started"
        self.release = self.f.directory / "release"

    def rows(self):
        return S.load_verifications(self.f.state_dir)[0]

    def integration_program(self, body):
        program = "#!" + sys.executable + "\n" + body
        policy = dict(self.f.policy, verifiers=[dict(
            self.f.policy["verifiers"][0],
            sha256=hashlib.sha256(program.encode()).hexdigest())])
        self.target = self.shared.precondition.target_commit({
            V.MERGE_VERIFIER_PATH: program, V.POLICY_FILE: json.dumps(policy)})
        return program

    def barrier(self):
        return ("import os, time\nfrom pathlib import Path\n"
                "Path(%r).write_text(str(os.getpid()))\n"
                "while Path(%r).parent.exists() and not Path(%r).exists(): time.sleep(0.02)\n"
                % (str(self.started), str(self.release), str(self.release)))

    def fault_after_barrier(self, stability=False, transport=False, intent=False,
                            wait_for_exit=False, exit_before_kill=False):
        """Fault after the real child starts; load cannot race a short deadline.

        The real communicate timeout then kills the running process group.
        This also works against the base runner for the mutation check.
        """
        site = self.f.directory / "fault-site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import json, os, pathlib, subprocess, time\n"
            "original = subprocess.Popen.communicate\n"
            "active = None\n"
            "original_killpg = os.killpg\n"
            "def killpg(pid, sig):\n"
            "    if %r and active is not None and active.pid == pid:\n"
            "        pathlib.Path(%r).touch()\n"
            "        active.wait(timeout=45)\n"
            "    return original_killpg(pid, sig)\n"
            "os.killpg = killpg\n"
            "def communicate(self, *args, **kwargs):\n"
            "    global active\n"
            "    command = self.args\n"
            "    if (isinstance(command, list) and command\n"
            "            and pathlib.Path(command[0]).name == 'verifier'\n"
            "            and ('--merge-base' in command) == %r\n"
            "            and not getattr(self, '_faulted', False)):\n"
            "        self._faulted = True\n"
            "        active = self\n"
            "        deadline = time.monotonic() + 45\n"
            "        while not pathlib.Path(%r).exists():\n"
            "            if (self.poll() is not None and not %r) or time.monotonic() >= deadline:\n"
            "                raise RuntimeError('fixture child never reached barrier')\n"
            "            time.sleep(0.02)\n"
            "        if %r: self.wait(timeout=45)\n"
            "        if %r:\n"
            "            pathlib.Path(%r).write_text(json.dumps({'binding': {'unit': 'u'}}))\n"
            "            pathlib.Path(%r).touch()\n"
            "        elif %r:\n"
            "            raise OSError('injected coordinator capture failure')\n"
            "        else:\n"
            "            kwargs['timeout'] = 0\n"
            "    return original(self, *args, **kwargs)\n"
            "subprocess.Popen.communicate = communicate\n"
            % (exit_before_kill, str(self.release), stability, str(self.started),
               wait_for_exit, wait_for_exit, intent,
               str(self.f.state_dir / 'merge-unit-during-run.json'),
               str(self.release), transport))
        self.f.env["PYTHONPATH"] = str(site)

    def verify(self):
        return self.f.invoke("--verify-integration")

    def swarm_verify(self):
        return subprocess.run([
            sys.executable, S.__file__,
            "verify", "--state-dir", str(self.f.state_dir), "--unit", "u",
            "--attempt", str(self.f.attempt), "--claim", V.INTEGRATION_CLAIM,
            "--target-commit", self.f.base, "--verifier", V.MERGE_VERIFIER,
            "--path", str(self.f.repo / V.MERGE_VERIFIER_PATH)],
            env=self.f.env, cwd=self.f.repo, capture_output=True, text=True, timeout=60)

    def anchor_integration_program(self):
        self.integration_program(self.barrier())
        f = self.f
        f.git("merge", "--no-edit", self.target)
        f.base = self.target
        f.head = f.git("rev-parse", "HEAD")
        for anchor in (f.launch, f.us["attempt_launch_facts"]["a1"]):
            anchor.update(base_commit=f.base, base_tree=f.git("rev-parse", f.base + "^{tree}"))
        f.us["attempt_produced_heads"]["a1"] = f.head
        f.forge["pr"]["headRefOid"] = f.head
        f.save()

    def assert_timeout_rerun(self, claim):
        result = self.verify()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        first = self.rows()[-1]
        self.assertEqual(first["claim"], claim)
        self.f.assert_refused(self.f.invoke())  # incomplete alone cannot admit
        self.release.touch()
        self.f.env.pop("PYTHONPATH")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        passed = self.rows()[-1]
        self.assertEqual(passed["result"], "pass")
        for field in V.MERGE_BASIS_FIELDS + ("verifier_sha256", "policy_sha256"):
            self.assertEqual(first[field], passed[field], field)
        merged = self.f.invoke()
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertEqual(self.f.intent()["integration_status"], "candidate-verified")
        self.assertEqual(first["result"], "incomplete")
        self.assertIn("coordinator timed out", first["incomplete_reason"])

    def test_integration_timeout_then_same_binding_pass_is_admitted(self):
        self.integration_program(self.barrier())
        self.fault_after_barrier()
        self.assert_timeout_rerun(V.INTEGRATION_CLAIM)

    def test_stability_timeout_kills_repetition_without_poisoning_binding(self):
        self.shared.install_policy(repetitions=2)
        self.shared.candidate({"tests/test_wait.py": self.barrier() +
                               "import unittest\nclass Guard(unittest.TestCase):\n"
                               "    def test_pass(self): pass\n"})
        self.fault_after_barrier(stability=True)
        self.assert_timeout_rerun(V.STABILITY_CLAIM)

    def test_launch_failure_then_same_binding_pass_is_admitted(self):
        interpreter = self.f.directory / "initially-unavailable-python"
        program = "#!" + str(interpreter) + "\nraise SystemExit(0)\n"
        policy = dict(self.f.policy, verifiers=[dict(
            self.f.policy["verifiers"][0], sha256=hashlib.sha256(program.encode()).hexdigest())])
        self.shared.precondition.target_commit({
            V.MERGE_VERIFIER_PATH: program, V.POLICY_FILE: json.dumps(policy)})
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        receipt = self.rows()[-1]
        self.assertEqual(receipt["result"], "incomplete")
        self.assertIn("could not launch", receipt["incomplete_reason"])
        interpreter.symlink_to(sys.executable)
        self.assertEqual(self.verify().returncode, 0)
        merged = self.f.invoke()
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)

    def test_transport_failure_is_incomplete(self):
        self.integration_program(self.barrier())
        self.fault_after_barrier(transport=True)
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.rows()[-1]["result"], "incomplete")
        self.assertIn("capture failure", self.rows()[-1]["incomplete_reason"])
        self.release.touch()
        self.f.env.pop("PYTHONPATH")
        self.assertEqual(self.verify().returncode, 0)
        self.assertEqual(self.f.invoke().returncode, 0)

    def test_exit_127_and_timeout_text_are_completed_failure(self):
        self.integration_program("import sys\nsys.stderr.write('timed out after 5400s')\n"
                                 "raise SystemExit(127)\n")
        self.assertNotEqual(self.verify().returncode, 0)
        row = self.rows()[-1]
        self.assertEqual((row["result"], row["exit_code"]), ("fail", 127))
        self.assertNotIn("incomplete_reason", row)
        self.f.assert_refused(self.f.invoke())

    def test_completed_failure_survives_capture_timeout_from_descendant(self):
        self.integration_program(
            "import subprocess, sys\nfrom pathlib import Path\n"
            "if not Path(%r).exists():\n"
            "    subprocess.Popen([sys.executable, '-c', %r])\n"
            "    raise SystemExit(1)\n" % (str(self.release), self.barrier()))
        self.fault_after_barrier(wait_for_exit=True)
        self.assertNotEqual(self.verify().returncode, 0)
        first = self.rows()[-1]
        self.release.touch()
        self.f.env.pop("PYTHONPATH")
        self.assertEqual(self.verify().returncode, 0)
        result = self.f.invoke()
        self.f.assert_refused(result)
        self.assertIn("FAIL", result.stderr)
        self.assertEqual((first["result"], first["exit_code"]), ("fail", 1))

    def test_candidate_system_exit_without_handshake_is_fail(self):
        self.shared.install_policy(repetitions=2)
        self.shared.candidate({"tests/test_exit.py": "raise SystemExit(0)\n"})
        self.assertNotEqual(self.verify().returncode, 0)
        row = self.rows()[-1]
        self.assertEqual((row["claim"], row["result"]), (V.STABILITY_CLAIM, "fail"))
        self.assertIn("missing or malformed", row["stderr_tail"])
        self.f.assert_refused(self.f.invoke())

    def test_completed_failure_in_poll_to_kill_window_remains_fail(self):
        fail = self.f.directory / "fail-first"
        fail.touch()
        self.integration_program(self.barrier() +
                                 "raise SystemExit(int(Path(%r).exists()))\n" % str(fail))
        self.fault_after_barrier(exit_before_kill=True)
        self.assertNotEqual(self.verify().returncode, 0)
        first = self.rows()[-1]
        fail.unlink()
        self.f.env.pop("PYTHONPATH")
        self.assertEqual(self.verify().returncode, 0)
        result = self.f.invoke()
        self.f.assert_refused(result)
        self.assertIn("FAIL", result.stderr)
        self.assertEqual((first["result"], first["exit_code"]), ("fail", 1))

    def assert_fail_then_pass(self, claim, legacy=False):
        self.shared.install_policy()
        self.assertEqual(self.verify().returncode, 0)
        rows = self.rows()[-2:]
        failed = dict(next(r for r in rows if r["claim"] == claim), result="fail", exit_code=1)
        if legacy:
            failed.update(exit_code=127, stderr_tail="timed out after 5400s")
        journal = self.f.state_dir / S.VERIFY_RECEIPTS
        journal.write_text(json.dumps(failed) + "\n")
        for row in rows:
            S._fsync_append(journal, row)
        before = journal.read_bytes()
        result = self.f.invoke()
        self.f.assert_refused(result)
        self.assertIn("FAIL", result.stderr)
        self.assertEqual(journal.read_bytes(), before)

    def test_completed_integration_fail_then_pass_still_refuses(self):
        self.assert_fail_then_pass(V.INTEGRATION_CLAIM)

    def test_completed_stability_fail_then_pass_still_refuses(self):
        self.assert_fail_then_pass(V.STABILITY_CLAIM)

    def test_legacy_timeout_fail_is_not_reinterpreted(self):
        self.assert_fail_then_pass(V.INTEGRATION_CLAIM, legacy=True)

    def queue_merge(self):
        self.shared.install_policy()
        self.assertEqual(self.verify().returncode, 0)
        self.f.forge["queued"] = True
        self.f.save()
        self.assertNotEqual(self.f.invoke().returncode, 0)
        self.assertEqual(self.f.intent()["phase"], "merge_requested")
        self.f.forge["pr"].update(state="MERGED", mergeCommit={"oid": self.f.merged})
        self.f.us["merge_receipt"] = {
            "unit": "u", "repo": self.f.remote, "pr": self.f.remote + "/pull/7",
            "target": "main", "head": self.f.head, "merged_as": self.f.merged,
            "target_commit": self.shared.target, "integration_status": "candidate-verified"}
        self.f.save()

    def assert_late_fail(self, claim):
        self.queue_merge()
        failed = dict(next(r for r in self.rows()[-2:] if r["claim"] == claim),
                      result="fail", exit_code=1)
        S._fsync_append(self.f.state_dir / S.VERIFY_RECEIPTS, failed)
        for _ in range(2):
            result = self.f.invoke()
            self.assertEqual(self.f.intent()["integration_status"], "integration-unverified")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("FAIL", self.f.intent()["integration_problem"])
            self.assertEqual(self.f.receipts()[-1]["integration_status"], "integration-unverified")
            state = json.loads((self.f.state_dir / S.STATE_FILE).read_text())
            self.assertEqual(state["units"]["u"]["state"], "READY_FOR_PR")
            self.assertEqual(state["units"]["u"]["merge_receipt"]["integration_status"],
                             "integration-unverified")
            self.assertNotIn(" advance ", result.stdout)
            self.assertFalse(any(r["verb"] == "close" for r in S.read_outbox(self.f.state_dir)))
        self.assertEqual(len(self.f.calls(["pr", "merge"])), 1)

    def test_later_integration_fail_invalidates_retained_evidence(self):
        self.assert_late_fail(V.INTEGRATION_CLAIM)

    def test_later_stability_fail_invalidates_retained_evidence(self):
        self.assert_late_fail(V.STABILITY_CLAIM)

    def test_unrelated_failure_and_incomplete_do_not_invalidate_retained_evidence(self):
        self.queue_merge()
        for row in self.rows()[-2:]:
            S._fsync_append(self.f.state_dir / S.VERIFY_RECEIPTS,
                            dict(row, result="incomplete", incomplete_reason="coordinator timeout"))
            for field in V.MERGE_BASIS_FIELDS + ("policy_sha256", "verifier_sha256", "unit"):
                changed = {field: "e" * 40}
                if field == "produced_head":
                    changed["subject_head"] = changed[field]
                S._fsync_append(self.f.state_dir / S.VERIFY_RECEIPTS,
                                dict(row, result="fail", exit_code=1, **changed))
        result = self.f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.f.intent()["integration_status"], "candidate-verified")

    def test_unreadable_journal_corrects_retained_label_and_withholds_advance(self):
        self.queue_merge()
        with (self.f.state_dir / S.VERIFY_RECEIPTS).open("a") as handle:
            handle.write("broken complete record\n")
        result = self.f.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.f.intent()["integration_status"], "integration-unverified")
        self.assertIn("journal is unreadable", self.f.intent()["integration_problem"])
        self.assertNotIn(" advance ", result.stdout)

    def test_swarm_integration_verify_refuses_existing_merge_intent(self):
        self.f.forge["queued"] = True
        self.f.save()
        self.assertNotEqual(self.f.invoke().returncode, 0)
        before = (self.f.state_dir / S.VERIFY_RECEIPTS).read_bytes()
        result = self.swarm_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("merge intent exists", result.stderr)
        self.assertEqual((self.f.state_dir / S.VERIFY_RECEIPTS).read_bytes(), before)
        self.assertEqual(len(self.f.calls(["pr", "merge"])), 1)

    def test_swarm_integration_verify_without_intent_is_allowed(self):
        result = self.swarm_verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.rows()[-1]["result"], "pass")

    def test_swarm_timeout_is_incomplete_and_same_binding_can_pass(self):
        self.anchor_integration_program()
        self.fault_after_barrier()
        self.assertNotEqual(self.swarm_verify().returncode, 0)
        first = self.rows()[-1]
        self.assertEqual(first["result"], "incomplete")
        self.release.touch()
        self.f.env.pop("PYTHONPATH")
        result = self.swarm_verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for field in V.MERGE_BASIS_FIELDS:
            self.assertEqual(first[field], self.rows()[-1][field])
        result = self.f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_swarm_rechecks_intent_before_publication(self):
        self.anchor_integration_program()
        self.fault_after_barrier(intent=True)
        before = (self.f.state_dir / S.VERIFY_RECEIPTS).read_bytes()
        result = self.swarm_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("merge intent exists", result.stderr)
        self.assertEqual((self.f.state_dir / S.VERIFY_RECEIPTS).read_bytes(), before)

    def test_swarm_unrelated_intent_does_not_block_verification(self):
        (self.f.state_dir / "merge-unit-other.json").write_text(
            json.dumps({"binding": {"unit": "other"}}))
        result = self.swarm_verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_swarm_unreadable_intent_refuses_before_verification(self):
        (self.f.state_dir / "merge-unit-broken.json").write_text("not json")
        before = (self.f.state_dir / S.VERIFY_RECEIPTS).read_bytes()
        result = self.swarm_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot read merge intent", result.stderr)
        self.assertEqual((self.f.state_dir / S.VERIFY_RECEIPTS).read_bytes(), before)

    def test_differently_named_stability_declaration_is_authorized_and_retained(self):
        self.shared.install_policy(repetitions=2)
        policy = self.shared.policy
        policy["verifiers"][-1]["name"] = "stability-checker"
        self.shared.precondition.target_commit({V.POLICY_FILE: json.dumps(policy)})
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.rows()[-1]["verifier"], "stability-checker")
        result = self.f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.f.intent()["integration_status"], "candidate-verified")

    def test_ambiguous_or_malformed_alternative_declarations_refuse(self):
        self.shared.install_policy()
        original = self.shared.policy
        entry = original["verifiers"][-1]
        for additions in ([dict(entry, name="another")],
                          [dict(entry, claims=["other-claim"])],
                          []):
            with self.subTest(additions=additions):
                policy = dict(original, verifiers=original["verifiers"] + additions)
                if not additions:
                    policy["verifiers"] = [original["verifiers"][0],
                                           dict(entry, name="another", claims=V.STABILITY_CLAIM)]
                self.shared.precondition.target_commit({V.POLICY_FILE: json.dumps(policy)})
                result = self.verify()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.f.assert_refused(self.f.invoke())


if __name__ == "__main__":
    unittest.main()
