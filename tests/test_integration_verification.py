"""Verification of the candidate merge, not either branch in isolation."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402
import unit as U  # noqa: E402
import verify as V  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                          env=ENV, capture_output=True, text=True)


class IntegrationRepo(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       env=ENV)
        (self.repo / "left.txt").write_text("0\n")
        (self.repo / "right.txt").write_text("0\n")
        (self.repo / "compatible.txt").write_text("0\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        self.verifier = Path(self.tmp.name) / "integration-check.sh"
        self.verifier.write_text(
            "#!/bin/sh\n"
            "test \"$(cat left.txt):$(cat right.txt)\" != \"1:1\"\n")
        self.verifier.chmod(0o755)
        self.digest = V.digest_file(self.verifier)[0]

    def commit_from_base(self, branch, path, value):
        git(self.repo, "checkout", "-q", "--detach", self.base)
        git(self.repo, "checkout", "-q", "-b", branch)
        (self.repo / path).write_text(value + "\n")
        git(self.repo, "add", path)
        git(self.repo, "commit", "-qm", branch)
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()


class TestCandidateMergeIsTheSubject(IntegrationRepo):

    def test_separate_passes_do_not_certify_a_broken_combination(self):
        target = self.commit_from_base("target-change", "left.txt", "1")
        produced = self.commit_from_base("produced-change", "right.txt", "1")

        target_only, error = V.run_in_checkout(
            U.run, self.repo, target, self.verifier, self.digest)
        self.assertIsNone(error, error)
        self.assertEqual(target_only["exit_code"], 0)
        produced_only, error = V.run_in_checkout(
            U.run, self.repo, produced, self.verifier, self.digest)
        self.assertIsNone(error, error)
        self.assertEqual(produced_only["exit_code"], 0)

        combined, basis, error = V.run_in_candidate_merge(
            U.run, self.repo, produced, target, self.verifier, self.digest)
        self.assertIsNone(error, error)
        self.assertEqual(basis["produced_head"], produced)
        self.assertEqual(basis["target_commit"], target)
        self.assertEqual(basis["merge_base"], self.base)
        self.assertNotEqual(combined["exit_code"], 0)

        compatible = self.commit_from_base(
            "compatible-change", "compatible.txt", "1")
        honest, honest_basis, error = V.run_in_candidate_merge(
            U.run, self.repo, compatible, target, self.verifier, self.digest)
        self.assertIsNone(error, error)
        self.assertEqual(honest["exit_code"], 0)
        self.assertEqual(honest_basis["merge_base"], self.base)

    def test_missing_objects_are_unavailable_without_a_fetch(self):
        target = self.commit_from_base("target", "left.txt", "1")
        outcome, basis, error = V.run_in_candidate_merge(
            U.run, self.repo, "f" * 40, target, self.verifier, self.digest)
        self.assertIsNone(outcome)
        self.assertIsNone(basis)
        self.assertIn("connected session must supply", error)
        self.assertIn("never contacts a forge", error)

    def test_the_same_commit_cannot_pose_as_a_candidate_merge(self):
        produced = self.commit_from_base(
            "produced", "compatible.txt", "1")
        outcome, basis, error = V.run_in_candidate_merge(
            U.run, self.repo, produced, produced, self.verifier, self.digest)
        self.assertIsNone(outcome)
        self.assertIsNone(basis)
        self.assertIn("same commit", error)

    def test_a_target_that_already_contains_the_head_is_not_a_candidate(self):
        produced = self.commit_from_base(
            "produced", "compatible.txt", "1")
        git(self.repo, "checkout", "-q", "-b", "target", produced)
        (self.repo / "left.txt").write_text("1\n")
        git(self.repo, "add", "left.txt")
        git(self.repo, "commit", "-qm", "target advanced past produced")
        target = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        outcome, basis, error = V.run_in_candidate_merge(
            U.run, self.repo, produced, target, self.verifier, self.digest)
        self.assertIsNone(outcome)
        self.assertIsNone(basis)
        self.assertIn("already contains", error)

    def test_distinct_heads_with_no_candidate_tree_delta_are_refused(self):
        target = self.commit_from_base("target", "left.txt", "1")
        produced = self.commit_from_base("produced", "left.txt", "1")
        self.assertNotEqual(target, produced)

        outcome, basis, error = V.run_in_candidate_merge(
            U.run, self.repo, produced, target, self.verifier, self.digest)
        self.assertIsNone(outcome)
        self.assertIsNotNone(basis)
        self.assertIn("no produced tree change", error)

    def test_a_textual_conflict_is_unavailable_not_a_branch_run(self):
        target = self.commit_from_base("target", "left.txt", "target")
        produced = self.commit_from_base("produced", "left.txt", "produced")
        outcome, basis, error = V.run_in_candidate_merge(
            U.run, self.repo, produced, target, self.verifier, self.digest)
        self.assertIsNone(outcome)
        self.assertEqual(basis["merge_base"], self.base)
        self.assertIn("candidate merge", error)
        self.assertIn("unavailable", error)

    def test_candidate_checkout_does_not_run_repository_hooks(self):
        target = self.commit_from_base("target", "left.txt", "1")
        produced = self.commit_from_base(
            "produced", "compatible.txt", "1")
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\nprintf hook > hook-ran\n")
        hook.chmod(0o755)
        self.verifier.write_text("#!/bin/sh\ntest ! -e hook-ran\n")
        digest = V.digest_file(self.verifier)[0]

        outcome, _basis, error = V.run_in_candidate_merge(
            U.run, self.repo, produced, target, self.verifier, digest)
        self.assertIsNone(error, error)
        self.assertEqual(outcome["exit_code"], 0)

    def test_repository_merge_driver_cannot_resolve_a_real_conflict(self):
        git(self.repo, "checkout", "-q", "--detach", self.base)
        (self.repo / ".gitattributes").write_text("left.txt merge=keep\n")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-qm", "declare untrusted merge driver")
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        target = self.commit_from_base("target", "left.txt", "target")
        produced = self.commit_from_base("produced", "left.txt", "produced")
        git(self.repo, "config", "merge.keep.driver", "true")

        outcome, basis, error = V.run_in_candidate_merge(
            U.run, self.repo, produced, target, self.verifier, self.digest)
        self.assertIsNone(outcome)
        self.assertIsNotNone(basis)
        self.assertIn("candidate merge", error)

    def test_environment_cannot_inject_a_merge_driver(self):
        git(self.repo, "checkout", "-q", "--detach", self.base)
        (self.repo / ".gitattributes").write_text("left.txt merge=keep\n")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-qm", "declare merge attribute")
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        target = self.commit_from_base("target", "left.txt", "target")
        produced = self.commit_from_base("produced", "left.txt", "produced")
        injected = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "merge.keep.driver",
            "GIT_CONFIG_VALUE_0": "true",
        }

        with mock.patch.dict(os.environ, injected):
            outcome, basis, error = V.run_in_candidate_merge(
                U.run, self.repo, produced, target, self.verifier,
                self.digest)
        self.assertIsNone(outcome)
        self.assertIsNotNone(basis)
        self.assertIn("candidate merge", error)

    def test_global_attributes_cannot_inject_a_union_merge(self):
        target = self.commit_from_base("target", "left.txt", "target")
        produced = self.commit_from_base("produced", "left.txt", "produced")
        fake_home = Path(self.tmp.name) / "home"
        attributes = fake_home / ".config" / "git" / "attributes"
        attributes.parent.mkdir(parents=True)
        attributes.write_text("left.txt merge=union\n")

        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            outcome, basis, error = V.run_in_candidate_merge(
                U.run, self.repo, produced, target, self.verifier,
                self.digest)
        self.assertIsNone(outcome)
        self.assertIsNotNone(basis)
        self.assertIn("candidate merge", error)

    def test_path_cannot_substitute_the_git_that_builds_the_candidate(self):
        target = self.commit_from_base("target", "left.txt", "target")
        produced = self.commit_from_base("produced", "left.txt", "produced")
        fake_bin = Path(self.tmp.name) / "fake-bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text(
            "#!/bin/sh\n"
            "for arg do\n"
            "  test \"$arg\" = merge && exit 0\n"
            "done\n"
            "exec /usr/bin/git \"$@\"\n")
        fake_git.chmod(0o755)
        path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")

        with mock.patch.dict(os.environ, {"PATH": path}):
            outcome, basis, error = V.run_in_candidate_merge(
                U.run, self.repo, produced, target, self.verifier,
                self.digest)
        self.assertIsNone(outcome)
        self.assertIsNotNone(basis)
        self.assertIn("candidate merge", error)

    def test_template_directory_cannot_seed_candidate_hooks(self):
        target = self.commit_from_base("target", "left.txt", "1")
        produced = self.commit_from_base(
            "produced", "compatible.txt", "1")
        template = Path(self.tmp.name) / "template"
        hooks = template / "hooks"
        hooks.mkdir(parents=True)
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\nprintf hook > hook-ran\n")
        hook.chmod(0o755)
        self.verifier.write_text(
            "#!/bin/sh\n"
            "git checkout --detach HEAD >/dev/null 2>&1 || exit 2\n"
            "test ! -e hook-ran\n")
        digest = V.digest_file(self.verifier)[0]

        with mock.patch.dict(os.environ, {"GIT_TEMPLATE_DIR": str(template)}):
            outcome, _basis, error = V.run_in_candidate_merge(
                U.run, self.repo, produced, target, self.verifier, digest)
        self.assertIsNone(error, error)
        self.assertEqual(outcome["exit_code"], 0)

    def test_promisor_repo_does_not_lazy_fetch_a_missing_commit(self):
        target = self.commit_from_base("target", "left.txt", "1")
        produced = self.commit_from_base(
            "produced", "compatible.txt", "1")
        git(self.repo, "config", "uploadpack.allowFilter", "true")
        git(self.repo, "config", "uploadpack.allowAnySHA1InWant", "true")
        partial = Path(self.tmp.name) / "partial"
        subprocess.run(
            ["git", "clone", "-q", "--filter=blob:none", "--single-branch",
             "--branch", "target", "file://" + str(self.repo), str(partial)],
            check=True, env=ENV)

        basis, error = V.integration_basis(
            U.run, partial, produced, target)
        self.assertIsNone(basis)
        self.assertIn("not available in the local repository", error)

    def test_local_objects_reachable_through_alternates_are_available(self):
        target = self.commit_from_base("target", "left.txt", "1")
        produced = self.commit_from_base(
            "produced", "compatible.txt", "1")
        referenced = Path(self.tmp.name) / "referenced"
        subprocess.run(
            ["git", "clone", "-q", "--reference", str(self.repo),
             "--no-local", "file://" + str(self.repo), str(referenced)],
            check=True, env=ENV)

        outcome, basis, error = V.run_in_candidate_merge(
            U.run, referenced, produced, target, self.verifier, self.digest)
        self.assertIsNone(error, error)
        self.assertEqual(outcome["exit_code"], 0)
        self.assertEqual(basis["produced_head"], produced)


class TestMergedTargetDerivation(IntegrationRepo):

    def test_squash_parent_establishes_the_target(self):
        target = self.commit_from_base("target", "left.txt", "1")
        produced = self.commit_from_base(
            "produced", "compatible.txt", "1")
        git(self.repo, "checkout", "-q", "--detach", target)
        git(self.repo, "merge", "-q", "--squash", produced)
        git(self.repo, "commit", "-qm", "squash result")
        merged_as = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        actual, error = V.target_before_merge(
            U.run, self.repo, produced, merged_as, "squash", target)
        self.assertIsNone(error, error)
        self.assertEqual(actual, target)

    def test_rebase_is_unavailable_because_it_has_no_encoded_boundary(self):
        target = self.commit_from_base("target", "left.txt", "1")
        produced = self.commit_from_base(
            "produced", "compatible.txt", "1")
        git(self.repo, "checkout", "-q", "--detach", target)
        git(self.repo, "cherry-pick", produced)
        merged_as = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        actual, error = V.target_before_merge(
            U.run, self.repo, produced, merged_as, "rebase", target)
        self.assertIsNone(actual)
        self.assertIn("does not encode", error)

    def test_patch_equivalent_target_cannot_be_mistaken_for_a_replay(self):
        git(self.repo, "checkout", "-q", "-b", "produced", self.base)
        (self.repo / "left.txt").write_text("1\n")
        git(self.repo, "add", "left.txt")
        git(self.repo, "commit", "-qm", "produced patch one")
        (self.repo / "right.txt").write_text("1\n")
        git(self.repo, "add", "right.txt")
        git(self.repo, "commit", "-qm", "produced patch two")
        produced = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        patch_two = produced

        target = self.commit_from_base("target", "left.txt", "1")
        git(self.repo, "checkout", "-q", "--detach", target)
        git(self.repo, "cherry-pick", patch_two)
        merged_as = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        actual, error = V.target_before_merge(
            U.run, self.repo, produced, merged_as, "rebase", target)
        self.assertIsNone(actual)
        self.assertIn("does not encode", error)

    def test_no_op_rebase_cannot_make_a_stale_target_self_confirming(self):
        actual_target = self.commit_from_base("target", "left.txt", "1")
        git(self.repo, "checkout", "-q", "-b", "produced", actual_target)
        (self.repo / "compatible.txt").write_text("1\n")
        git(self.repo, "add", "compatible.txt")
        git(self.repo, "commit", "-qm", "produced")
        produced = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        actual, error = V.target_before_merge(
            U.run, self.repo, produced, produced, "rebase", self.base)
        self.assertIsNone(actual)
        self.assertIn("does not encode", error)


class TestIntegrationReceiptBinding(IntegrationRepo):

    def setUp(self):
        super().setUp()
        self.target = self.commit_from_base("target", "left.txt", "1")
        self.produced = self.commit_from_base(
            "produced", "compatible.txt", "1")
        self.basis, error = V.candidate_merge_basis(
            U.run, self.repo, self.produced, self.target)
        self.assertIsNone(error, error)
        self.policy_digest = "p" * 64
        self.policy = {"verifiers": [{
            "name": "tests", "sha256": self.digest,
            "claims": [V.INTEGRATION_CLAIM]}]}

    def write_receipt(self, **changes):
        receipt = {
            "unit": "u1", "claim": V.INTEGRATION_CLAIM,
            "verifier": "tests", "verifier_sha256": self.digest,
            "policy_sha256": self.policy_digest,
            "subject_head": self.produced, "result": "pass",
        }
        receipt.update(self.basis)
        receipt.update(changes)
        with open(Path(self.tmp.name) / S.VERIFY_RECEIPTS, "w") as fh:
            fh.write(json.dumps(receipt) + "\n")
        return receipt

    def admit(self, target=None):
        return S.admit_verification(
            self.tmp.name, "u1", V.INTEGRATION_CLAIM, self.produced,
            self.policy_digest, self.policy, repo=self.repo,
            base_commit=self.base, target_commit=target or self.target)

    def test_a_branch_local_receipt_cannot_pose_as_integration_evidence(self):
        for branch_head in (self.target, self.produced):
            branch_local = {
                "unit": "u1", "claim": V.INTEGRATION_CLAIM,
                "verifier": "tests", "verifier_sha256": self.digest,
                "policy_sha256": self.policy_digest,
                "subject_head": branch_head, "result": "pass",
            }
            self.assertIn("integration receipt has no produced_head",
                          S._verify_shape_problem(branch_local))

    def test_an_honest_compatible_pair_is_admitted(self):
        expected = self.write_receipt()
        admitted, refusal = self.admit()
        self.assertIsNone(refusal)
        self.assertEqual(admitted, expected)

    def test_target_movement_invalidates_the_old_receipt(self):
        self.write_receipt()
        moved = self.commit_from_base("moved-target", "right.txt", "2")
        admitted, refusal = self.admit(target=moved)
        self.assertIsNone(admitted)
        self.assertIn("target moved after the check", refusal)
        self.assertIn("re-run integration-tests", refusal)

    def test_a_caller_cannot_hide_target_movement_in_merge_attestation(self):
        self.write_receipt()
        git(self.repo, "checkout", "-q", "--detach", self.target)
        (self.repo / "right.txt").write_text("2\n")
        git(self.repo, "add", "right.txt")
        git(self.repo, "commit", "-qm", "target moved")
        actual_target = git(
            self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "merge", "-q", "--no-ff", self.produced,
            "-m", "actual merge")
        merged_as = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(actual_target, self.target)

        merge_receipt = {
            "unit": "u1", "repo": str(self.repo), "pr": "pr/1",
            "target": "main", "head": self.produced,
            "target_commit": self.target, "merged_as": merged_as,
            "method": "merge", "merged": True, "attested": True,
        }
        with open(Path(self.tmp.name) / S.MERGE_RECEIPTS, "w") as fh:
            fh.write(json.dumps(merge_receipt) + "\n")

        admitted, refusal = S.admit_merge(
            self.tmp.name, "u1", self.produced, repo=self.repo,
            require_target_binding=True)
        self.assertIsNone(
            admitted,
            "a stale caller-supplied target was accepted for a merge whose "
            "first parent proves the target had moved")
        self.assertIn("target moved", refusal)

    def test_a_forged_merge_base_is_rederived_and_refused(self):
        self.write_receipt(merge_base="e" * 40)
        admitted, refusal = self.admit()
        self.assertIsNone(admitted)
        self.assertIn("target moved after the check", refusal)


if __name__ == "__main__":
    unittest.main()
