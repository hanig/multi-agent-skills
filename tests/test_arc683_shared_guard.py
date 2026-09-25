"""Shared-guard admission through the real operator with a replaced forge PATH."""
import hashlib
import json
from pathlib import Path
import shutil
import unittest

from tests import test_arc683_merge_precondition as fixtures
from tests import test_arc1049_verify_lock as lock_fixtures

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

    def use_real_integration(self, root_discovery=False):
        self.install_policy()
        program = (ROOT / V.MERGE_VERIFIER_PATH).read_text()
        if root_discovery:
            program = program.replace(', "-s", "tests"', '')
        self.policy["verifiers"][0]["sha256"] = hashlib.sha256(program.encode()).hexdigest()
        self.target = self.precondition.target_commit({
            V.MERGE_VERIFIER_PATH: program, V.POLICY_FILE: json.dumps(self.policy)})

    def assert_import_verification_passes(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        integration, shared = S.load_verifications(self.f.state_dir)[0][-2:]
        self.assertEqual((integration["result"], shared["result"]), ("pass", "pass"))
        self.assertIn("repetition 5/5", shared["stdout_tail"])
        self.assertEqual(self.f.calls(["pr", "merge"]), [])

    def test_nested_package_relative_imports_match_integration_discovery(self):
        self.use_real_integration()
        self.candidate({
            "tests/nested/__init__.py": "", "tests/nested/helpers.py": "VALUE=7\n",
            "tests/nested/test_guard.py": "import unittest\nfrom .helpers import VALUE\n"
            "class Guard(unittest.TestCase):\n"
            "    def test_guard(self): self.assertEqual(VALUE, 7)\n"})
        self.assert_import_verification_passes()

    def test_root_package_relative_imports_with_target_package_runner(self):
        self.use_real_integration(root_discovery=True)
        self.candidate({
            "tests/__init__.py": "", "tests/helpers.py": "VALUE=7\n",
            "tests/test_guard.py": "import unittest\nfrom .helpers import VALUE\n"
            "class Guard(unittest.TestCase):\n"
            "    def test_guard(self): self.assertEqual(VALUE, 7)\n"})
        self.assert_import_verification_passes()

    def test_root_package_keeps_legacy_absolute_helper_imports(self):
        self.use_real_integration()
        self.candidate({
            "tests/__init__.py": "", "tests/helpers.py": "VALUE=7\n",
            "tests/test_guard.py": "import unittest\nfrom helpers import VALUE\n"
            "class Guard(unittest.TestCase):\n"
            "    def test_guard(self): self.assertEqual(VALUE, 7)\n"})
        self.assert_import_verification_passes()

    def test_tests_helper_keeps_precedence_over_same_named_root_helper(self):
        self.use_real_integration()
        self.candidate({
            "helpers.py": "VALUE=0\n", "tests/__init__.py": "",
            "tests/helpers.py": "VALUE=7\n",
            "tests/test_guard.py": "import unittest\nfrom helpers import VALUE\n"
            "class Guard(unittest.TestCase):\n"
            "    def test_guard(self): self.assertEqual(VALUE, 7)\n"})
        self.assert_import_verification_passes()

    def test_import_time_chdir_keeps_selected_file_identity(self):
        self.use_real_integration()
        self.candidate({"tests/test_guard.py":
                        "import os\nfrom pathlib import Path\n"
                        "os.chdir(Path(__file__).parent)\n" + self.counter_test()})
        self.assert_import_verification_passes()
        self.assertEqual(self.counter.read_text(), "6")  # integration plus five repetitions
        merged = self.f.invoke()
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertEqual(len(self.f.calls(["pr", "merge"])), 1)

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

    def assert_module_refused(self, diagnostic):
        verified = self.verify()
        # Check the consumer first: package discovery mutations reach stub gh.
        self.f.assert_refused(self.f.invoke())
        self.assertNotEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(self.shared_receipt()["result"], "fail")
        self.assertIn(diagnostic, self.shared_receipt()["stderr_tail"])

    def package_tests(self, hook=False):
        package = ("import unittest\nclass PackageGuard(unittest.TestCase):\n"
                   "    def test_package(self): pass\n")
        if hook:
            package += "def load_tests(loader, tests, pattern): return tests\n"
        return package

    def test_empty_module_cannot_borrow_package_tests(self):
        self.install_policy()
        self.candidate({"tests/__init__.py": self.package_tests(),
                        "tests/test_guard.py": "# selected module has no cases\n"})
        self.assert_module_refused("no tests")

    def test_package_hook_cannot_hide_failing_changed_module(self):
        self.install_policy()
        self.candidate({"tests/__init__.py": self.package_tests(hook=True),
                        "tests/test_guard.py": self.counter_test(fail_at=1)})
        self.assert_module_refused("FAIL_ON_NTH_RUN")
        self.assertEqual(self.counter.read_text(), "1")

    def test_nested_package_hook_cannot_hide_failing_changed_module(self):
        self.install_policy()
        self.candidate({"tests/__init__.py": "",
                        "tests/nested/__init__.py": self.package_tests(hook=True),
                        "tests/nested/test_guard.py": self.counter_test(fail_at=1)})
        self.assert_module_refused("FAIL_ON_NTH_RUN")
        self.assertEqual(self.counter.read_text(), "1")

    def test_other_changed_module_cannot_supply_empty_modules_count(self):
        self.install_policy(repetitions=3)
        self.candidate({"tests/test_a_passing.py": self.counter_test(),
                        "tests/test_z_empty.py": "# zero cases after a passing module\n"})
        self.assert_module_refused("no tests")
        self.assertEqual(self.counter.read_text(), "3")

    def test_same_named_package_cannot_replace_selected_file(self):
        self.install_policy()
        self.candidate({"tests/__init__.py": "",
                        "tests/test_guard.py": "# selected file has no cases\n",
                        "tests/test_guard/__init__.py": self.package_tests()})
        self.assert_module_refused("imported from a different file")

    def test_module_own_hook_delegates_and_counts_each_worker_run(self):
        self.install_policy(repetitions=3)
        worker = self.counter_test() + "\nif __name__ == '__main__':\n    unittest.main()\n"
        module = ("import subprocess, sys, unittest\nfrom pathlib import Path\n"
                  "class DefaultCase(unittest.TestCase):\n"
                  "    def test_default(self): self.fail('HOOK_MUST_REPLACE_DEFAULT')\n"
                  "def load_tests(loader, tests, pattern):\n"
                  "    def delegate():\n"
                  "        subprocess.run([sys.executable, str(Path(__file__).with_name("
                  "'review_worker.py'))], check=True)\n"
                  "    return unittest.TestSuite([unittest.FunctionTestCase(delegate)])\n")
        self.candidate({"tests/__init__.py": self.package_tests(hook=True),
                        "tests/test_review.py": module, "tests/review_worker.py": worker})
        verified = self.verify()
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(self.counter.read_text(), "3")
        self.assertEqual(self.shared_receipt()["result"], "pass")
        self.assertIn("test_review.py: repetition 3/3", self.shared_receipt()["stdout_tail"])
        self.assertEqual(self.f.calls(["pr", "merge"]), [])
        merged = self.f.invoke()
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertEqual(len(self.f.calls(["pr", "merge"])), 1)

    def test_module_own_empty_hook_cannot_borrow_default_or_package_tests(self):
        self.install_policy()
        module = ("import unittest\nclass DefaultCase(unittest.TestCase):\n"
                  "    def test_default(self): pass\n"
                  "def load_tests(loader, tests, pattern): return unittest.TestSuite()\n")
        self.candidate({"tests/__init__.py": self.package_tests(),
                        "tests/test_guard.py": module})
        self.assert_module_refused("no tests")

    def test_module_hook_early_zero_exit_cannot_admit_a_merge(self):
        self.use_real_integration()
        self.candidate({"tests/test_guard.py":
                        "def load_tests(loader, tests, pattern):\n"
                        "    raise SystemExit(0)\n"})
        verified = self.verify()
        self.f.assert_refused(self.f.invoke())
        self.assertNotEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(self.shared_receipt()["result"], "fail")

    def test_suite_declared_count_cannot_replace_executed_count(self):
        self.install_policy()
        module = ("import unittest\nclass EmptySuite(unittest.TestSuite):\n"
                  "    def countTestCases(self): return 1\n"
                  "def load_tests(loader, tests, pattern): return EmptySuite()\n")
        self.candidate({"tests/test_guard.py": module})
        self.assert_module_refused("no tests")

    def test_changed_module_error_refuses_merge(self):
        self.install_policy()
        self.candidate({"tests/test_guard.py":
                        "import unittest\nclass Broken(unittest.TestCase):\n"
                        "    def test_error(self): raise RuntimeError('MODULE_ERROR')\n"})
        self.assert_module_refused("MODULE_ERROR")

    def test_changed_module_unexpected_success_refuses_merge(self):
        self.install_policy()
        self.candidate({"tests/test_guard.py":
                        "import unittest\nclass Broken(unittest.TestCase):\n"
                        "    @unittest.expectedFailure\n"
                        "    def test_unexpected_success(self): pass\n"})
        self.assert_module_refused("unexpected success")

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

    def test_selected_module_with_no_tests_is_not_a_pass(self):
        self.install_policy()
        self.candidate({"tests/test_empty.py": "# no actual guard\n"})
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.shared_receipt()["result"], "fail")
        self.assertIn("executed no tests", self.shared_receipt()["stderr_tail"])
        self.f.assert_refused(self.f.invoke())

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

    def test_both_verifiers_run_in_the_same_disposable_checkout(self):
        self.install_policy()
        integration = ("#!/usr/bin/env python3\nfrom pathlib import Path\n"
                       "Path('integration-marker').write_text(str(Path.cwd()))\n")
        self.policy["verifiers"][0] = dict(
            self.policy["verifiers"][0], sha256=hashlib.sha256(integration.encode()).hexdigest())
        self.target = self.precondition.target_commit({
            V.MERGE_VERIFIER_PATH: integration, V.POLICY_FILE: json.dumps(self.policy)})
        test = ("import unittest\nfrom pathlib import Path\n"
                "class SameCheckout(unittest.TestCase):\n"
                "    def test_checkout(self):\n"
                "        self.assertEqual(Path('integration-marker').read_text(), str(Path.cwd()))\n"
                "        self.assertNotEqual(str(Path.cwd()), %r)\n" % str(self.f.repo))
        self.candidate({"tests/test_same_checkout.py": test})
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.f.repo / "integration-marker").exists())

    def test_modified_and_renamed_modules_run_but_target_only_module_does_not(self):
        f = self.f
        passing = "import unittest\nclass Old(unittest.TestCase):\n    def test_old(self): pass\n"
        # Put old tests into both branches' common history, then install policy
        # only on the target so the PR diff is still measured from merge_base.
        base = self.precondition.target_commit({"tests/test_modified.py": passing,
                                                "tests/test_old.py": passing})
        f.git("merge", "--no-edit", base)
        self.install_policy()
        self.target = self.precondition.target_commit({"tests/test_target_only.py":
                                                       "raise RuntimeError('NOT_PR_CHANGED')\n"})
        self.candidate({"tests/test_modified.py": self.counter_test(),
                        "tests/test_old.py": None, "tests/test_renamed.py": passing})
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = self.shared_receipt()
        self.assertIn("test_modified.py: repetition 5/5", receipt["stdout_tail"])
        self.assertIn("test_renamed.py: repetition 5/5", receipt["stdout_tail"])
        self.assertNotIn("test_target_only.py", receipt["stdout_tail"])
        self.assertEqual(self.counter.read_text(), "5")

    def test_retained_both_claims_survive_git_cleanup_after_lost_merge_response(self):
        self.install_policy()
        self.assertEqual(self.verify().returncode, 0)
        self.f.forge["fail_view_once"] = True
        self.f.save()
        self.assertNotEqual(self.f.invoke().returncode, 0)
        shutil.rmtree(self.f.repo / ".git/objects")
        # DONE avoids asking advancement to redo production judgment after the
        # deliberately simulated Git-object cleanup; reconciliation still runs.
        state_path = self.f.state_dir / S.STATE_FILE
        state = json.loads(state_path.read_text())
        state["units"]["u"]["state"] = "DONE"
        state_path.write_text(json.dumps(state))
        result = self.f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.f.intent()["integration_status"], "candidate-verified")
        self.assertEqual(len(self.f.calls(["pr", "merge"])), 1)

    def test_legacy_single_claim_intent_is_relabelled_under_shared_policy(self):
        self.install_policy()
        self.assertEqual(self.verify().returncode, 0)
        self.f.forge["queued"] = True
        self.f.save()
        self.assertNotEqual(self.f.invoke().returncode, 0)
        intent = self.f.intent()
        intent["preconditions"].pop("required_merge_claims", None)
        intent["preconditions"]["integration"].pop(CLAIM, None)
        intent["integration_status"] = "candidate-verified"
        path = self.f.state_dir / ("merge-unit-" + intent["operation_id"] + ".json")
        path.write_text(json.dumps(intent))
        rows = [r for r in S.load_verifications(self.f.state_dir)[0] if r["claim"] != CLAIM]
        (self.f.state_dir / S.VERIFY_RECEIPTS).write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
        self.f.forge["pr"].update(state="MERGED", mergeCommit={"oid": self.f.merged})
        self.f.us["merge_receipt"] = {
            "unit": "u", "repo": self.f.remote, "pr": self.f.remote + "/pull/7",
            "target": "main", "head": self.f.head, "merged_as": self.f.merged,
            "target_commit": self.target, "integration_status": "candidate-verified"}
        self.f.save()
        result = self.f.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.f.intent()["integration_status"], "integration-unverified")
        self.assertEqual(self.f.receipts()[-1]["integration_status"], "integration-unverified")
        state = json.loads((self.f.state_dir / S.STATE_FILE).read_text())
        self.assertEqual(state["units"]["u"]["merge_receipt"]["integration_status"],
                         "integration-unverified")
        self.assertEqual(len(self.f.calls(["pr", "merge"])), 1)
        # Losing only the declared verifier blob must not erase the target's
        # still-readable requirement or resurrect the incorrect persisted label.
        blob = self.f.git("rev-parse", self.target + ":" + PROGRAM)
        (self.f.repo / ".git/objects" / blob[:2] / blob[2:]).unlink()
        result = self.f.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.f.intent()["integration_status"], "integration-unverified")
        self.assertEqual(self.f.receipts()[-1]["integration_status"], "integration-unverified")


class TestSharedGuardPublication(unittest.TestCase):
    def setUp(self):
        self.lock = lock_fixtures.TestVerificationLock()
        self.lock.setUp()
        self.addCleanup(self.lock.doCleanups)
        f = self.lock.f
        barrier = f.git("show", self.lock.target + ":" + V.MERGE_VERIFIER_PATH)
        # Restore the original fast integration stub, then put the existing
        # real-process barrier in the second, independently pinned verifier.
        policy = dict(f.policy, verifiers=f.policy["verifiers"] + [{
            "name": CLAIM, "claims": [CLAIM],
            "sha256": hashlib.sha256(barrier.encode()).hexdigest()}])
        self.lock.target = self.lock.precondition.target_commit({
            V.MERGE_VERIFIER_PATH: f.verifier_bytes.decode(), PROGRAM: barrier,
            V.POLICY_FILE: json.dumps(policy)})

    def test_second_verifier_releases_lease_and_publishes_both_claims(self):
        self.lock.start()
        self.lock.locked_change()
        result = self.lock.complete()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = S.load_verifications(self.lock.f.state_dir)[0]
        self.assertEqual([r["claim"] for r in rows[-2:]], [V.INTEGRATION_CLAIM, CLAIM])
        self.assertEqual(len(rows), 3)

    def test_target_move_during_second_verifier_publishes_neither_claim(self):
        self.lock.start()
        self.lock.locked_change("forge = json.loads(forge_path.read_text())\n"
                               "forge['ref']['object']['sha'] = 'f' * 40\n"
                               "forge_path.write_text(json.dumps(forge))\n")
        self.lock.assert_stale("target moved during verification")


if __name__ == "__main__":
    unittest.main()
