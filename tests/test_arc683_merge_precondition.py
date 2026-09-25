"""Merge admission through the real operator and a forge on a replaced PATH."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_merge_unit as fixtures

S, V = fixtures.S, fixtures.V


class TestMergePrecondition(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TestMergeUnit()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def target_commit(self, files):
        f = self.f
        f.git("checkout", "-q", "main")
        for name, content in files.items():
            path = f.repo / name
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        f.git("add", "-A")
        f.git("commit", "-qm", "target update")
        target = f.git("rev-parse", "HEAD")
        f.git("checkout", "-q", "swarm-a1")
        f.forge["pr"]["baseRefOid"] = target
        f.forge["ref"]["object"]["sha"] = target
        f.forge["commit"]["parents"] = [{"sha": target}]
        f.save()
        return target

    def stale_pr_base(self):
        target = self.target_commit({"unrelated.txt": "advanced target\n"})
        self.f.forge["pr"]["baseRefOid"] = self.f.base
        self.f.save()
        return target

    def test_stale_pr_base_verifies_current_branch_ref(self):
        f = self.f
        target = self.stale_pr_base()
        previous = (f.state_dir / S.VERIFY_RECEIPTS).read_bytes()
        result = f.invoke("--verify-integration")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipts = S.load_verifications(f.state_dir)[0]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[-1]["target_commit"], target)
        self.assertEqual(receipts[-1]["authorization_commit"], target)
        self.assertEqual(receipts[-1]["subject_head"], f.head)
        self.assertEqual(receipts[-1]["result"], "pass")
        self.assertTrue((f.state_dir / S.VERIFY_RECEIPTS).read_bytes().startswith(previous))
        self.assertEqual(f.calls(["pr", "merge"]), [])
        self.assertEqual(f.calls(["api"]), [f.forge["ref_command"]] * 2)

    def test_stale_pr_base_evidence_refuses_before_merge(self):
        f = self.f
        self.stale_pr_base()
        previous = (f.state_dir / S.VERIFY_RECEIPTS).read_bytes()
        result = f.invoke()
        f.assert_refused(result)
        self.assertIn("target moved", result.stderr)
        self.assertIn("--verify-integration", result.stderr)
        self.assertEqual(list(f.state_dir.glob("merge-unit-*.json")), [])
        self.assertEqual((f.state_dir / S.VERIFY_RECEIPTS).read_bytes(), previous)

    def test_ref_move_during_preflight_refuses_before_merge(self):
        f = self.f
        target = self.stale_pr_base()
        f.forge["ref"]["object"]["sha"] = f.base
        f.forge["ref_after_read"] = {
            "ref": "refs/heads/main", "object": {"type": "commit", "sha": target}}
        f.save()
        result = f.invoke()
        f.assert_refused(result)
        self.assertIn("target moved during preflight", result.stderr)
        self.assertIn("--verify-integration", result.stderr)
        self.assertEqual(f.calls(["api"]), [f.forge["ref_command"]] * 2)
        self.assertEqual(list(f.state_dir.glob("merge-unit-*.json")), [])
        # No request was made, so fresh verification can proceed without abandonment.
        verified = f.invoke("--verify-integration")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(S.load_verifications(f.state_dir)[0][-1]["target_commit"], target)

    def test_stale_pr_base_with_current_evidence_merges_once(self):
        f = self.f
        target = self.stale_pr_base()
        receipt = f.record_integration(target)
        result = f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = f.calls()
        merge_index = next(i for i, call in enumerate(calls) if call[:2] == ["pr", "merge"])
        self.assertEqual(calls[merge_index - 1], f.forge["ref_command"])
        self.assertEqual(f.intent()["target_before_request"], target)
        self.assertEqual(f.intent()["preconditions"]["integration"], receipt)
        self.assertEqual(f.receipts()[0]["target_commit"], target)
        self.assertEqual(f.receipts()[0]["integration_status"], "candidate-verified")
        self.assertEqual(f.invoke().returncode, 0)
        self.assertEqual(len(f.calls(["pr", "merge"])), 1)

    def move_after_publication(self, fail_resolution=False):
        f = self.f
        target = self.stale_pr_base()
        moved_ref = dict(f.forge["ref"], object=dict(f.forge["ref"]["object"]))
        f.forge["ref"]["object"]["sha"] = f.base
        f.save()
        log = f.intercept_intent_publication({"ref": moved_ref}, fail_resolution)
        return target, log

    def assert_cancelled(self, result, log, observed_target):
        f = self.f
        f.assert_refused(result)
        self.assertIn("--verify-integration", result.stderr)
        intent = f.intent()
        self.assertEqual(intent["phase"], "cancelled_before_request")
        self.assertEqual(intent["target_before_request"], f.base)
        self.assertEqual(intent["cancellation"]["observed_target"], observed_target)
        self.assertTrue(intent["cancellation"]["reason"])
        # A complete record was visible after the real directory fsync.
        self.assertEqual(json.loads(log.read_text().splitlines()[-1]), intent)
        state = json.loads((f.state_dir / S.STATE_FILE).read_text())
        self.assertEqual(state["units"]["u"]["state"], "READY_FOR_PR")
        return intent

    def test_ref_move_after_intent_publication_cancels_before_merge(self):
        f = self.f
        target, log = self.move_after_publication()
        cancelled = self.assert_cancelled(f.invoke(), log, target)
        self.assertEqual(f.calls(["api"]), [f.forge["ref_command"]] * 3)
        del f.env["PYTHONPATH"]
        # Persisted cancellation is consumed by the real rerun. Stale evidence
        # still refuses, then fresh verification permits one distinct operation.
        stale = f.invoke()
        f.assert_refused(stale)
        self.assertIn("target moved", stale.stderr)
        self.assertNotIn("unresolved outcome", stale.stderr)
        verified = f.invoke("--verify-integration")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(S.load_verifications(f.state_dir)[0][-1]["target_commit"], target)
        self.assertEqual(f.intent(), cancelled)
        result = f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(f.invoke().returncode, 0)
        self.assertEqual(len(f.calls(["pr", "merge"])), 1)
        intents = [json.loads(p.read_text()) for p in f.state_dir.glob("merge-unit-*.json")]
        self.assertIn(cancelled, intents)
        self.assertEqual(len(intents), 2)
        successor, = [i for i in intents if i["phase"] == "receipt_recorded"]
        self.assertNotEqual(successor["operation_id"], cancelled["operation_id"])
        self.assertEqual(successor["target_before_request"], target)

    def test_ref_read_failure_after_intent_publication_cancels_before_merge(self):
        f = self.f
        log = f.intercept_intent_publication({"ref_exit": 1})
        cancelled = self.assert_cancelled(f.invoke(), log, None)
        self.assertIn("target ref unavailable", cancelled["cancellation"]["reason"])
        del f.env["PYTHONPATH"]
        f.save()  # restore the readable ref, leaving the persisted intent alone
        result = f.invoke("--verify-integration")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(f.intent(), cancelled)
        self.assertEqual(f.calls(["pr", "merge"]), [])

    def test_cancellation_write_failure_leaves_unresolved_intent(self):
        f = self.f
        self.move_after_publication(fail_resolution=True)
        result = f.invoke()
        f.assert_refused(result)
        self.assertIn("injected cancellation publication failure", result.stderr)
        unresolved = f.intent()
        self.assertEqual(unresolved["phase"], "merge_requested")
        del f.env["PYTHONPATH"]
        for extra in ((), ("--verify-integration",)):
            result = f.invoke(*extra)
            f.assert_refused(result)
            self.assertIn("unresolved", result.stderr)
            self.assertEqual(f.intent(), unresolved)

    def test_cancellation_directory_fsync_failure_remains_unresolved(self):
        f = self.f
        self.move_after_publication(fail_resolution="directory-sync")
        result = f.invoke()
        f.assert_refused(result)
        self.assertIn("injected cancellation directory fsync failure", result.stderr)
        unresolved = f.intent()
        self.assertEqual(unresolved["phase"], "merge_requested")
        del f.env["PYTHONPATH"]
        result = f.invoke()
        f.assert_refused(result)
        self.assertIn("unresolved outcome", result.stderr)
        self.assertEqual(f.intent(), unresolved)

    def test_pending_cancellation_restores_unresolved_after_failed_rollback(self):
        f = self.f
        self.move_after_publication(fail_resolution="rollback")
        result = f.invoke()
        f.assert_refused(result)
        self.assertIn("injected cancellation rollback failure", result.stderr)
        # The rename was visible, but its failed fsync is not a resolution.
        self.assertEqual(f.intent()["phase"], "cancelled_before_request")
        pending, = f.state_dir.glob("merge-unit-*.cancellation-pending")
        unresolved = json.loads(pending.read_text())
        self.assertEqual(unresolved["phase"], "merge_requested")
        del f.env["PYTHONPATH"]
        result = f.invoke("--verify-integration")
        f.assert_refused(result)
        self.assertIn("unresolved", result.stderr)
        self.assertEqual(f.intent(), unresolved)
        self.assertFalse(pending.exists())
        # The ordinary investigated-abandonment route remains available.
        abandoned = f.abandon_intent(unresolved)
        self.assertEqual(abandoned.returncode, 0, abandoned.stdout + abandoned.stderr)
        self.assertEqual(f.intent()["phase"], "resolved_by_abandonment")
        self.assertEqual(f.calls(["pr", "merge"]), [])

    def test_cancelled_intent_cannot_resolve_a_later_queued_request(self):
        f = self.f
        target, log = self.move_after_publication()
        self.assert_cancelled(f.invoke(), log, target)
        del f.env["PYTHONPATH"]
        self.assertEqual(f.invoke("--verify-integration").returncode, 0)
        path = Path(f.env["FORGE_STATE"])
        forge = json.loads(path.read_text())
        forge["queued"] = True
        path.write_text(json.dumps(forge))
        self.assertNotEqual(f.invoke().returncode, 0)
        result = f.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unresolved outcome", result.stderr)
        self.assertEqual(len(f.calls(["pr", "merge"])), 1)
        self.assertEqual(f.receipts(), [])

    def test_malformed_ref_after_publication_is_cancelled_without_normalizing(self):
        f = self.f
        malformed = {"ref": "refs/heads/main", "object": {
            "type": "commit", "sha": f.base + "\n"}}
        log = f.intercept_intent_publication({"ref": malformed})
        cancelled = self.assert_cancelled(f.invoke(), log, None)
        self.assertIn("invalid exact Git object id", cancelled["cancellation"]["reason"])

    def test_stable_ref_is_read_after_durable_intent_and_merges_once(self):
        f = self.f
        log = f.intercept_intent_publication()
        result = f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(log.read_text().splitlines()[0])["phase"], "merge_requested")
        calls = f.calls()
        merge_index = next(i for i, call in enumerate(calls) if call[:2] == ["pr", "merge"])
        self.assertEqual(calls[merge_index - 1], f.forge["ref_command"])
        self.assertEqual(calls[:merge_index].count(f.forge["ref_command"]), 3)
        self.assertEqual(f.invoke().returncode, 0)
        self.assertEqual(len(f.calls(["pr", "merge"])), 1)

    def test_invalid_target_ref_never_falls_back_to_pr_base(self):
        f = self.f
        payloads = [None, [], {},
                    {"ref": "refs/heads/main", "object": {}},
                    {"ref": "refs/heads/main", "object": {"type": "tag", "sha": f.base}}]
        for name in ("refs/heads/Main", "refs/heads/main/", "refs/heads/main\n"):
            payloads.append({"ref": name, "object": {"type": "commit", "sha": f.base}})
        for sha in (None, f.base.upper(), f.base + "\n", f.base + "0", f.base[:12]):
            payloads.append({"ref": "refs/heads/main", "object": {"type": "commit", "sha": sha}})
        original = (f.state_dir / S.VERIFY_RECEIPTS).read_bytes()
        for payload in payloads:
            for extra in ((), ("--verify-integration",)):
                with self.subTest(payload=payload, extra=extra):
                    f.forge["ref"] = payload
                    f.save()
                    f.assert_refused(f.invoke(*extra))
                    self.assertEqual((f.state_dir / S.VERIFY_RECEIPTS).read_bytes(), original)
                    self.assertEqual(list(f.state_dir.glob("merge-unit-*.json")), [])

    def test_unavailable_target_ref_never_falls_back_to_pr_base(self):
        f = self.f
        f.forge["ref_exit"] = 1
        f.save()
        for extra in ((), ("--verify-integration",)):
            with self.subTest(extra=extra):
                result = f.invoke(*extra)
                f.assert_refused(result)
                self.assertIn("target ref unavailable", result.stderr)
                self.assertEqual(list(f.state_dir.glob("merge-unit-*.json")), [])

    def test_ref_route_uses_anchored_remote_and_exact_encoded_branch(self):
        f = self.f
        branch = "release/next#%+é"
        f.git("branch", "-m", "main", branch)
        f.unit["target_branch"] = branch
        remote = "git@forge.example:another/repository.git"
        for anchor in (f.launch, f.us["attempt_launch_facts"]["a1"]):
            anchor.update(target_branch=branch, repository_remote=remote,
                          repository_remote_raw=remote)
        f.state["plan_digest"] = S.plan_digest(f.plan)
        f.forge["pr"].update(baseRefName=branch,
                             url="https://forge.example/another/repository/pull/7")
        f.forge["ref"]["ref"] = "refs/heads/" + branch
        f.forge["ref_command"] = [
            "api", "--hostname", "forge.example",
            "repos/another/repository/git/ref/heads/release/next%23%25%2B%C3%A9"]
        f.save()
        verified = f.invoke("--verify-integration")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        result = f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(f.calls(["api"])[:3], [f.forge["ref_command"]] * 3)
        self.assertEqual(f.receipts()[0]["target"], branch)
        self.assertEqual(f.receipts()[0]["repo"], remote)

    def test_000_failed_candidate_cannot_be_waived(self):
        f = self.f
        target = self.target_commit({"target.fail": "1\n"})
        (f.state_dir / S.VERIFY_RECEIPTS).unlink()
        verified = f.invoke("--verify-integration")
        self.assertNotEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        evidence, = S.load_verifications(f.state_dir)[0]
        self.assertEqual(evidence["result"], "fail")
        self.assertEqual(evidence["target_commit"], target)
        self.assertEqual(evidence["subject_head"], f.head)
        self.assertTrue(evidence["candidate_tree"])
        # Both branches pass individually; their combination fails.
        verifier = f.repo / V.MERGE_VERIFIER_PATH
        for head in (target, f.head):
            result, error = V.run_in_checkout(
                f.verifier_runner, f.repo, head, verifier,
                f.policy["verifiers"][0]["sha256"])
            self.assertIsNone(error)
            self.assertEqual(result["exit_code"], 0)
        for extra in ((), ("--allow-unchecked-scope", "follow-up will fix it"),
                      ("--reason", "follow-up will fix it"),
                      ("--allow-integration-failure", "follow-up will fix it")):
            with self.subTest(extra=extra):
                result = f.invoke(*extra)
                f.assert_refused(result)
        self.assertEqual(list(f.state_dir.glob("merge-unit-*.json")), [])

    def test_evidence_for_T1_refuses_after_target_moves_to_T2(self):
        f = self.f
        target = self.target_commit({"unrelated.txt": "new target\n"})
        result = f.invoke("--allow-unchecked-scope", "follow-up")
        f.assert_refused(result)
        self.assertIn("target moved", result.stderr)
        self.assertNotEqual(target, f.base)

    def test_evidence_for_H1_refuses_repaired_H2(self):
        f = self.f
        (f.repo / "change.txt").write_text("repaired\n")
        f.git("commit", "-qam", "repair")
        f.head = f.git("rev-parse", "HEAD")
        f.us["attempt_produced_heads"]["a1"] = f.head
        f.forge["pr"]["headRefOid"] = f.head
        f.save()
        result = f.invoke()
        f.assert_refused(result)
        self.assertIn("pass for another commit", result.stderr)

    def test_missing_target_policy_refuses_even_if_candidate_has_policy(self):
        f = self.f
        self.target_commit({V.POLICY_FILE: None})
        result = f.invoke()
        f.assert_refused(result)
        self.assertIn("target policy", result.stderr)
        self.assertIn("--verify-integration", result.stderr)
        f.assert_refused(f.invoke("--verify-integration"))

    def test_target_verifier_digest_mismatch_refuses(self):
        f = self.f
        self.target_commit({V.MERGE_VERIFIER_PATH: f.verifier_bytes.decode() + "# drift\n"})
        result = f.invoke()
        f.assert_refused(result)
        self.assertIn("not the file that was approved", result.stderr)
        self.assertIn("--verify-integration", result.stderr)
        f.assert_refused(f.invoke("--verify-integration"))

    def test_policy_on_target_bootstraps_an_attempt_anchored_before_policy(self):
        f = self.f
        early_base = self.target_commit({V.POLICY_FILE: None, V.MERGE_VERIFIER_PATH: None})
        early_tree = f.git("rev-parse", early_base + "^{tree}")
        f.git("checkout", "-q", "--detach", early_base)
        (f.repo / "change.txt").write_text("produced\n")
        f.git("commit", "-qam", "in-flight work")
        f.head = f.git("rev-parse", "HEAD")
        f.git("update-ref", "refs/heads/swarm-a1", f.head)
        f.git("checkout", "-q", "swarm-a1")
        for anchor in (f.launch, f.us["attempt_launch_facts"]["a1"]):
            anchor.update(base_commit=early_base, base_tree=early_tree)
        f.us["attempt_produced_heads"]["a1"] = f.head
        f.forge["pr"]["headRefOid"] = f.head
        target = self.target_commit({V.POLICY_FILE: json.dumps(f.policy),
                                     V.MERGE_VERIFIER_PATH: f.verifier_bytes.decode()})
        self.assertNotEqual(target, early_base)
        missing = subprocess.run([str(f.bin / "git"), "-C", str(f.repo), "show",
                                  early_base + ":" + V.POLICY_FILE], env=f.env,
                                 capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0)
        result = f.invoke("--verify-integration")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = S.load_verifications(f.state_dir)[0][-1]
        self.assertEqual(receipt["authorization_commit"], target)
        result = f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(f.calls(["pr", "merge"])), 1)

    def test_earlier_pass_cannot_hide_a_red_run_for_the_same_candidate(self):
        f = self.f
        receipt = S.load_verifications(f.state_dir)[0][0]
        receipt.update(result="fail", exit_code=1)
        S._fsync_append(f.state_dir / S.VERIFY_RECEIPTS, receipt)
        result = f.invoke("--allow-unchecked-scope", "follow-up")
        f.assert_refused(result)
        self.assertIn("FAIL", result.stderr)

    def test_candidate_cannot_authorize_a_replacement_verifier(self):
        f = self.f
        verifier = f.repo / V.MERGE_VERIFIER_PATH
        verifier.write_text("#!" + sys.executable + "\nraise SystemExit(0)\n")
        policy = {"schema_version": 1, "verifiers": [{
            "name": V.MERGE_VERIFIER, "claims": [V.INTEGRATION_CLAIM],
            "sha256": hashlib.sha256(verifier.read_bytes()).hexdigest()}]}
        (f.repo / V.POLICY_FILE).write_text(json.dumps(policy))
        f.git("add", "-A")
        f.git("commit", "-qm", "candidate tries to authorize itself")
        f.head = f.git("rev-parse", "HEAD")
        f.us["attempt_produced_heads"]["a1"] = f.head
        f.forge["pr"]["headRefOid"] = f.head
        f.unit["scope"] += ["verifiers/*", "verifiers.json"]
        f.state["plan_digest"] = S.plan_digest(f.plan)
        f.save()
        self.target_commit({"target.fail": "1\n"})
        result = f.invoke("--verify-integration")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = S.load_verifications(f.state_dir)[0][-1]
        self.assertEqual(receipt["result"], "fail")
        self.assertEqual(receipt["verifier_sha256"], f.policy["verifiers"][0]["sha256"])
        f.assert_refused(f.invoke())

    def test_missing_evidence_in_coordinator_is_not_supplied_by_worker_files(self):
        f = self.f
        data = (f.state_dir / S.VERIFY_RECEIPTS).read_bytes()
        (f.state_dir / S.VERIFY_RECEIPTS).unlink()
        (f.attempt / S.VERIFY_RECEIPTS).write_bytes(data)
        (f.attempt / "receipt.json").write_bytes(data)
        result = f.invoke("--allow-unchecked-scope", "follow-up")
        f.assert_refused(result)
        self.assertIn("no verification receipt", result.stderr)

    def test_candidate_basis_tampering_refuses(self):
        f = self.f
        path = f.state_dir / S.VERIFY_RECEIPTS
        original = json.loads(path.read_text())
        for field in ("merge_base", "candidate_tree", "produced_head"):
            with self.subTest(field=field):
                path.write_text(json.dumps(dict(original, **{field: "f" * 40})) + "\n")
                f.assert_refused(f.invoke())

    def test_honest_verification_records_then_merges_exactly_once(self):
        f = self.f
        (f.state_dir / S.VERIFY_RECEIPTS).unlink()
        result = f.invoke("--verify-integration")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(f.calls(["pr", "merge"]), [])
        receipt, = S.load_verifications(f.state_dir)[0]
        self.assertEqual(receipt["authorization_commit"], f.base)
        result = f.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(f.intent()["preconditions"]["integration"], receipt)
        self.assertEqual(f.intent()["integration_status"], "candidate-verified")
        self.assertEqual(f.receipts()[0]["integration_status"], "candidate-verified")
        self.assertEqual(f.invoke().returncode, 0)
        self.assertEqual(len(f.calls(["pr", "merge"])), 1)

    def test_local_repo_anchor_works_when_operator_cwd_is_elsewhere(self):
        f = self.f
        self.assertTrue(Path(f.launch["repo"]).is_absolute())
        self.assertNotEqual(f.launch["repo"], "example/project")
        (f.state_dir / S.VERIFY_RECEIPTS).unlink()
        result = f.invoke("--verify-integration", cwd=f.directory)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = f.invoke(cwd=f.directory)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(f.calls(["pr", "merge"])), 1)

    def test_shipped_policy_pins_program_that_runs_the_discovered_suite(self):
        f = self.f
        root = Path(__file__).resolve().parents[1]
        verifier = root / V.MERGE_VERIFIER_PATH
        digest = hashlib.sha256(verifier.read_bytes()).hexdigest()
        policy = json.loads((root / V.POLICY_FILE).read_text())
        entry, error = V.authorized(policy, V.MERGE_VERIFIER, digest, V.INTEGRATION_CLAIM)
        self.assertIsNone(error, error)
        self.assertEqual(entry["sha256"], digest)
        suite = f.directory / "small-candidate"
        tests = suite / "tests"
        tests.mkdir(parents=True)
        test = tests / "test_candidate.py"
        test.write_text("import unittest\nclass Candidate(unittest.TestCase):\n"
                        "    def test_candidate(self):\n"
                        "        self.fail('INTENTIONAL_BROKEN_CANDIDATE')\n")
        result = subprocess.run([sys.executable, str(verifier)], cwd=suite, env=f.env,
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INTENTIONAL_BROKEN_CANDIDATE", result.stderr)
        self.assertIn("Ran 1 test", result.stderr)
        test.write_text("import unittest\nclass Candidate(unittest.TestCase):\n"
                        "    def test_candidate(self):\n"
                        "        self.assertEqual(2 + 2, 4)\n")
        result = subprocess.run([sys.executable, str(verifier)], cwd=suite, env=f.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_target_race_is_recorded_unverified_and_never_advanced(self):
        f = self.f
        # The target changes only during the forge mutation, after admission.
        f.forge["commit"]["parents"] = [{"sha": "f" * 40}]
        f.save()
        result = f.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INTEGRATION-UNVERIFIED", result.stderr)
        self.assertIn("target moved", result.stderr)
        self.assertEqual(len(f.calls(["pr", "merge"])), 1)
        self.assertEqual(f.intent()["integration_status"], "integration-unverified")
        self.assertEqual(f.receipts()[0]["integration_status"], "integration-unverified")
        state = json.loads((f.state_dir / S.STATE_FILE).read_text())
        self.assertEqual(state["units"]["u"]["state"], "READY_FOR_PR")
        result = f.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(f.calls(["pr", "merge"])), 1)

    def test_scope_exit1_with_binding_but_missing_status_fields_refuses_override(self):
        f = self.f
        f.unit["scope"] = []
        f.state["plan_digest"] = S.plan_digest(f.plan)
        f.save()
        f.intercept_scope_binding(remove=("status", "scope", "out_of_scope",
                                         "deletions_out_of_scope"))
        f.assert_binding_refused(f.invoke("--allow-unchecked-scope", "follow-up"))

    def test_scope_every_exit_requires_complete_schema(self):
        f = self.f
        for declared in (["change.txt"], [], None):
            if declared is None:
                f.unit.pop("scope", None)
            else:
                f.unit["scope"] = declared
            f.state["plan_digest"] = S.plan_digest(f.plan)
            f.save()
            for field in ("status", "out_of_scope", "deletions_out_of_scope"):
                with self.subTest(declared=declared, field=field):
                    f.intercept_scope_binding({field: None})
                    f.assert_binding_refused(f.invoke("--allow-unchecked-scope", "follow-up"))

    def test_old_bad_integration_label_is_corrected_during_reconciliation(self):
        f = self.f
        f.forge["queued"] = True
        f.save()
        self.assertNotEqual(f.invoke().returncode, 0)
        old = f.intent()
        old["integration_status"] = "candidate-verified"
        path = f.state_dir / ("merge-unit-" + old["operation_id"] + ".json")
        path.write_text(json.dumps(old))
        target = self.stale_pr_base()
        f.forge["pr"].update(state="MERGED", mergeCommit={"oid": f.merged})
        f.us["merge_receipt"] = {
            "unit": "u", "repo": f.remote, "pr": f.remote + "/pull/7",
            "target": "main", "head": f.head, "merged_as": f.merged,
            "target_commit": f.base, "integration_status": "candidate-verified"}
        f.save()
        result = f.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(f.intent()["integration_status"], "integration-unverified")
        self.assertEqual(f.receipts()[-1]["integration_status"], "integration-unverified")
        saved = json.loads((f.state_dir / S.STATE_FILE).read_text())
        self.assertEqual(saved["units"]["u"]["merge_receipt"]["integration_status"],
                         "integration-unverified")
        self.assertEqual(saved["units"]["u"]["merge_receipt"]["target_commit"], target)


if __name__ == "__main__":
    unittest.main()
