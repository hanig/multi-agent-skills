"""Authorized, content-pinned verifiers.

A production receipt says a change was produced and explicitly denies that it
is any good. A verifier is what can say more. Three properties make its word
admissible, and any one missing makes the rest theatre:

  AUTHORIZED  the policy comes from the anchored BASE commit, not the agent's
              branch. Otherwise the candidate change authorizes its own
              verifier, which is the whole game.
  PINNED      the bytes that run are the bytes that hashed. A path is not an
              identity.
  BOUND       the receipt names the head it verified. A pass for another
              commit is not a pass for this one.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import verify as V  # noqa: E402
import unit as U  # noqa: E402
import swarm as S  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
HEAD = "a" * 40


def git(repo, *a):
    return subprocess.run(["git", "-C", repo] + list(a), check=True, env=ENV,
                          capture_output=True, text=True)


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "r")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True, env=ENV)
        self.script = os.path.join(self.tmp, "check.sh")
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.script, 0o755)
        self.digest = V.digest_file(self.script)[0]
        self.commit_policy({"schema_version": 1, "verifiers": [
            {"name": "tests", "sha256": self.digest,
             "claims": ["tests-pass"]}]})

    def commit_policy(self, policy):
        with open(os.path.join(self.repo, V.POLICY_FILE), "w") as fh:
            json.dump(policy, fh)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "policy")
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()


class TestAuthorizationComesFromTheBase(Base):

    def test_the_policy_is_read_from_the_anchored_base(self):
        policy, digest, err = V.read_policy(U.run, self.repo, self.base)
        self.assertIsNone(err)
        self.assertTrue(digest)
        self.assertEqual(policy["verifiers"][0]["name"], "tests")

    def test_a_verifier_added_on_the_branch_does_not_authorize_itself(self):
        """The agent owns HEAD. It must not own the authorization."""
        self.commit_policy({"schema_version": 1, "verifiers": [
            {"name": "mine", "sha256": self.digest, "claims": ["tests-pass"]}]})
        newer_base = self.base
        # Read against the ORIGINAL base: the new entry must not be visible.
        first = git(self.repo, "rev-list", "--max-parents=0", "HEAD"
                    ).stdout.strip()
        policy, _d, err = V.read_policy(U.run, self.repo, first)
        self.assertIsNone(err)
        names = [v["name"] for v in policy["verifiers"]]
        self.assertNotIn("mine", names)
        self.assertNotEqual(first, newer_base)

    def test_no_policy_at_the_base_authorizes_nothing(self):
        empty = os.path.join(self.tmp, "e")
        os.makedirs(empty)
        subprocess.run(["git", "init", "-q", empty], check=True, env=ENV)
        with open(os.path.join(empty, "x"), "w") as fh:
            fh.write("x")
        git(empty, "add", "-A")
        git(empty, "commit", "-qm", "c")
        base = git(empty, "rev-parse", "HEAD").stdout.strip()
        _p, _d, err = V.read_policy(U.run, empty, base)
        self.assertIn("nothing authorizes any verifier", err)

    def test_no_base_commit_means_no_authorization_source(self):
        _p, _d, err = V.read_policy(U.run, self.repo, None)
        self.assertIn("no authorization source", err)

    def test_a_future_schema_fails_closed(self):
        self.commit_policy({"schema_version": 99, "verifiers": []})
        _p, _d, err = V.read_policy(U.run, self.repo, self.base)
        self.assertIn("Refusing rather than guessing", err)

    def test_a_policy_that_does_not_parse_is_refused(self):
        with open(os.path.join(self.repo, V.POLICY_FILE), "w") as fh:
            fh.write("{ not json")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "bad")
        base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _p, _d, err = V.read_policy(U.run, self.repo, base)
        self.assertIn("does not parse", err)


class TestPinningIsByContent(Base):

    def test_the_authorized_digest_is_required(self):
        entry, refusal = V.authorized(
            {"verifiers": [{"name": "tests", "sha256": "0" * 64,
                            "claims": ["tests-pass"]}]},
            "tests", self.digest, "tests-pass")
        self.assertIsNone(entry)
        self.assertIn("not the file that was approved", refusal)

    def test_an_unnamed_verifier_is_refused(self):
        entry, refusal = V.authorized({"verifiers": []}, "tests",
                                      self.digest, "tests-pass")
        self.assertIsNone(entry)
        self.assertIn("authorizes no verifier", refusal)

    def test_a_verifier_may_not_make_a_claim_it_was_not_given(self):
        """A verifier that can assert anything asserts nothing."""
        policy = {"verifiers": [{"name": "tests", "sha256": self.digest,
                                 "claims": ["lint-clean"]}]}
        entry, refusal = V.authorized(policy, "tests", self.digest,
                                      "tests-pass")
        self.assertIsNone(entry)
        self.assertIn("not to claim", refusal)

    def test_the_bytes_that_run_are_the_bytes_that_hashed(self):
        out, err = V.run_pinned(U.run, self.script, self.digest)
        self.assertIsNone(err)
        self.assertEqual(out["exit_code"], 0)

    def test_a_changed_file_is_refused_at_run_time(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n# changed\n")
        out, err = V.run_pinned(U.run, self.script, self.digest)
        self.assertIsNone(out)
        self.assertIn("hashes to", err)

    def test_a_failing_verifier_reports_its_exit_code(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\nexit 3\n")
        d = V.digest_file(self.script)[0]
        out, err = V.run_pinned(U.run, self.script, d)
        self.assertIsNone(err)
        self.assertEqual(out["exit_code"], 3)

    def test_an_oversized_file_is_not_read(self):
        big = os.path.join(self.tmp, "big")
        with open(big, "wb") as fh:
            fh.write(b"0" * (V.MAX_VERIFIER_BYTES + 1))
        _d, _s, err = V.digest_file(big)
        self.assertIn("over the", err)

    def test_a_directory_is_not_a_verifier(self):
        _d, _s, err = V.digest_file(self.tmp)
        self.assertIn("not a regular file", err)


class TestAdmissionIsBound(unittest.TestCase):

    def _write(self, d, *recs):
        os.makedirs(d, exist_ok=True)
        with open(Path(d) / S.VERIFY_RECEIPTS, "w") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def _rec(self, **over):
        r = {"unit": "u1", "claim": "tests-pass", "verifier": "tests",
             "verifier_sha256": "v" * 64, "policy_sha256": "p" * 64,
             "subject_head": HEAD, "result": "pass"}
        r.update(over)
        return r

    def test_a_bound_passing_receipt_is_admitted(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, self._rec())
            got, refusal = S.admit_verification(d, "u1", "tests-pass", HEAD,
                                                "p" * 64)
            self.assertIsNone(refusal)
            self.assertIsNotNone(got)

    def test_a_pass_for_another_commit_is_not_a_pass_for_this_one(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, self._rec(subject_head="b" * 40))
            got, refusal = S.admit_verification(d, "u1", "tests-pass", HEAD,
                                                "p" * 64)
            self.assertIsNone(got)
            self.assertIn("not a pass for this one", refusal)

    def test_a_receipt_under_different_rules_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, self._rec(policy_sha256="q" * 64))
            got, refusal = S.admit_verification(d, "u1", "tests-pass", HEAD,
                                                "p" * 64)
            self.assertIsNone(got)
            self.assertIn("rules changed after the check", refusal)

    def test_a_fail_is_a_result_not_a_missing_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, self._rec(result="fail"))
            got, refusal = S.admit_verification(d, "u1", "tests-pass", HEAD,
                                                "p" * 64)
            self.assertIsNone(got)
            self.assertIn("fix the work rather than re-running", refusal)

    def test_a_receipt_for_another_claim_does_not_count(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, self._rec(claim="lint-clean"))
            got, refusal = S.admit_verification(d, "u1", "tests-pass", HEAD,
                                                "p" * 64)
            self.assertIsNone(got)
            self.assertIn("no verification receipt", refusal)

    def test_a_malformed_journal_raises_rather_than_admitting(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d, exist_ok=True)
            with open(Path(d) / S.VERIFY_RECEIPTS, "w") as fh:
                fh.write("NOT JSON\n")
            with self.assertRaises(S.OutboxError):
                S.load_verifications(d)

    def test_a_wrong_receipt_does_not_mask_a_correct_one(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, self._rec(subject_head="b" * 40), self._rec())
            got, refusal = S.admit_verification(d, "u1", "tests-pass", HEAD,
                                                "p" * 64)
            self.assertIsNone(refusal)
            self.assertIsNotNone(got)


class TestTheRequirementIsDeclared(unittest.TestCase):

    def _plan(self, **over):
        u = {"id": "u1", "kind": "code", "outputs": ["o"],
             "runtime": "none"}
        u.update(over)
        return {"project": "p", "units": [u]}

    def test_an_empty_claim_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._plan(requires_verification=[""]))
        self.assertIn("Name the claim", str(c.exception))

    def test_a_string_instead_of_a_list_is_refused(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan(self._plan(requires_verification="tests-pass"))

    def test_only_a_code_unit_can_require_verification(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan({"project": "p", "units": [
                {"id": "u1", "kind": "slurm", "outputs": ["o"],
                 "runtime": "none", "command": "true",
                 "requires_verification": ["tests-pass"]}]})
        self.assertIn("only a code unit has", str(c.exception))

    def test_a_declared_claim_validates(self):
        S.validate_plan(self._plan(requires_verification=["tests-pass"]))

    def test_declaring_none_is_unchanged(self):
        S.validate_plan(self._plan())


if __name__ == "__main__":
    unittest.main()
