"""Shared-guard admission through the real operator with a replaced forge PATH."""
import hashlib
import json
from pathlib import Path
import shutil
import unittest

from tests import test_arc683_merge_precondition as fixtures

S, V = fixtures.S, fixtures.V
CLAIM = "changed-tests-stable"
PROGRAM = "verifiers/changed_tests_stable.py"
ROOT = Path(__file__).resolve().parents[1]


class TestSharedGuard(unittest.TestCase):
    def setUp(self):
        self.precondition = fixtures.TestMergePrecondition()
        self.precondition.setUp()
        self.addCleanup(self.precondition.doCleanups)
        self.f = self.precondition.f

    def install_policy(self, repetitions=None, program=None):
        program = program or (ROOT / PROGRAM).read_text()
        entry = {"name": CLAIM, "claims": [CLAIM],
                 "sha256": hashlib.sha256(program.encode()).hexdigest()}
        if repetitions is not None:
            entry["repetitions"] = repetitions
        self.policy = dict(self.f.policy, verifiers=self.f.policy["verifiers"] + [entry])
        self.target = self.precondition.target_commit({
            PROGRAM: program, V.POLICY_FILE: json.dumps(self.policy)})
        return self.target

    def candidate(self, files):
        f = self.f
        for name, content in files.items():
            path = f.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                path.unlink()
            else:
                path.write_text(content)
        f.git("add", "-A")
        f.git("commit", "-qm", "candidate test changes")
        f.head = f.git("rev-parse", "HEAD")
        f.us["attempt_produced_heads"]["a1"] = f.head
        f.forge["pr"]["headRefOid"] = f.head
        f.unit["scope"] = ["**"]
        f.state["plan_digest"] = S.plan_digest(f.plan)
        f.save()

    def counter_test(self, fail_at=0):
        # External per-fixture counter observes subprocess repetitions; each
        # trial gets its own directory, including when this module is repeated.
        counter = self.f.directory / "repetitions"
        self.counter = counter
        return ("import unittest\nfrom pathlib import Path\n"
                "class Guard(unittest.TestCase):\n"
                "    def test_guard(self):\n"
                "        p = Path(%r)\n"
                "        n = int(p.read_text()) + 1 if p.exists() else 1\n"
                "        p.write_text(str(n))\n"
                "        self.assertNotEqual(n, %r, 'FAIL_ON_NTH_RUN')\n"
                % (str(counter), fail_at))

    def verify(self):
        return self.f.invoke("--verify-integration")

    def shared_receipt(self):
        return [r for r in S.load_verifications(self.f.state_dir)[0]
                if r["claim"] == CLAIM][-1]

    def test_000_fifth_run_failure_refuses_before_any_merge_call(self):
        self.install_policy()
        self.candidate({"tests/test_guard.py": self.counter_test(fail_at=5)})
        verified = self.verify()
        merged = self.f.invoke()
        # Under the once-only mutation this real consumer reaches stub gh merge.
        self.f.assert_refused(merged)
        self.assertNotEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(self.counter.read_text(), "5")
        self.assertEqual(self.shared_receipt()["result"], "fail")
        self.assertIn("FAIL_ON_NTH_RUN", self.shared_receipt()["stderr_tail"])

    def test_no_changed_test_modules_passes_trivially(self):
        self.install_policy()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = self.shared_receipt()
        self.assertEqual(receipt["result"], "pass")
        self.assertIn("no changed test modules", receipt["stdout_tail"])
        self.assertEqual(self.f.calls(["pr", "merge"]), [])
        result = self.f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.f.calls(["pr", "merge"])), 1)

    def test_default_five_runs_and_same_candidate_binding(self):
        self.install_policy()
        self.candidate({"tests/test_guard.py": self.counter_test()})
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.counter.read_text(), "5")
        integration, shared = S.load_verifications(self.f.state_dir)[0][-2:]
        for field in ("subject_head", "produced_head", "target_commit", "merge_base",
                      "candidate_tree", "authorization_commit", "policy_sha256"):
            self.assertEqual(integration[field], shared[field], field)
        self.assertEqual(shared["repetitions"], 5)
        self.assertIn("test_guard.py", shared["stdout_tail"])

    def test_repetitions_come_only_from_target_policy(self):
        self.install_policy(repetitions=3)
        weaker = dict(self.policy, verifiers=[dict(v) for v in self.policy["verifiers"]])
        weaker["verifiers"][-1]["repetitions"] = 1
        # Add candidate policy from its old base: target policy changed too, so
        # make a nonconflicting policy edit after inheriting the target commit.
        self.f.git("merge", "--no-edit", self.target)
        self.candidate({"tests/test_guard.py": self.counter_test(fail_at=3),
                        V.POLICY_FILE: json.dumps(weaker),
                        PROGRAM: "#!/usr/bin/env python3\nraise SystemExit(0)\n"})
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.counter.read_text(), "3")
        self.assertEqual(self.shared_receipt()["repetitions"], 3)
        self.f.assert_refused(self.f.invoke())

    def test_policy_without_new_verifier_keeps_single_claim_rule(self):
        self.candidate({"tests/test_guard.py": self.counter_test(fail_at=1)})
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.counter.exists())
        self.assertTrue(all(r["claim"] == V.INTEGRATION_CLAIM
                            for r in S.load_verifications(self.f.state_dir)[0]))
        result = self.f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_shared_receipt_cannot_use_integration_alone(self):
        self.install_policy()
        self.assertEqual(self.verify().returncode, 0)
        path = self.f.state_dir / S.VERIFY_RECEIPTS
        rows = S.load_verifications(self.f.state_dir)[0]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows if r["claim"] != CLAIM))
        result = self.f.invoke("--allow-unchecked-scope", "cannot waive stability")
        self.f.assert_refused(result)
        self.assertIn(CLAIM, result.stderr)

    def test_shared_binding_tampering_is_refused(self):
        self.install_policy()
        self.assertEqual(self.verify().returncode, 0)
        path = self.f.state_dir / S.VERIFY_RECEIPTS
        rows = S.load_verifications(self.f.state_dir)[0]
        for field in ("subject_head", "produced_head", "target_commit", "merge_base",
                      "candidate_tree", "authorization_commit", "policy_sha256",
                      "verifier_sha256", "repetitions"):
            with self.subTest(field=field):
                changed = dict(rows[-1], **{field: "f" * 40})
                path.write_text("".join(json.dumps(r) + "\n" for r in rows[:-1] + [changed]))
                self.f.assert_refused(self.f.invoke())

    def test_shared_evidence_refused_after_target_move_with_fresh_integration(self):
        self.install_policy()
        self.assertEqual(self.verify().returncode, 0)
        shared = self.shared_receipt()
        self.target = self.precondition.target_commit({"unrelated.txt": "moved\n"})
        self.f.record_integration(self.target)
        S._fsync_append(self.f.state_dir / S.VERIFY_RECEIPTS, shared)
        self.f.assert_refused(self.f.invoke())

    def test_shared_evidence_refused_after_head_move_with_fresh_integration(self):
        self.install_policy()
        self.assertEqual(self.verify().returncode, 0)
        shared = self.shared_receipt()
        self.candidate({"change.txt": "repaired\n"})
        self.f.record_integration(self.target)
        S._fsync_append(self.f.state_dir / S.VERIFY_RECEIPTS, shared)
        self.f.assert_refused(self.f.invoke())

    def test_pass_cannot_hide_shared_failure_for_same_binding(self):
        self.install_policy()
        self.assertEqual(self.verify().returncode, 0)
        failed = dict(self.shared_receipt(), result="fail", exit_code=1)
        S._fsync_append(self.f.state_dir / S.VERIFY_RECEIPTS, failed)
        result = self.f.invoke()
        self.f.assert_refused(result)
        self.assertIn("FAIL", result.stderr)

    def test_declared_but_malformed_or_unpinned_policy_refuses(self):
        self.install_policy()
        for repetition in (0, -1, True, "5"):
            with self.subTest(repetition=repetition):
                self.policy["verifiers"][-1]["repetitions"] = repetition
                self.target = self.precondition.target_commit({V.POLICY_FILE: json.dumps(self.policy)})
                self.f.assert_refused(self.verify())
        self.policy["verifiers"][-1]["repetitions"] = 5
        self.target = self.precondition.target_commit({V.POLICY_FILE: json.dumps(self.policy),
                                                       PROGRAM: "# wrong bytes\n"})
        self.f.assert_refused(self.verify())

    def test_shipped_policy_pins_shared_verifier(self):
        policy = json.loads((ROOT / V.POLICY_FILE).read_text())
        digest = hashlib.sha256((ROOT / PROGRAM).read_bytes()).hexdigest()
        entry, error = V.authorized(policy, CLAIM, digest, CLAIM)
        self.assertIsNone(error, error)
        self.assertEqual(entry.get("repetitions", 5), 5)


if __name__ == "__main__":
    unittest.main()
