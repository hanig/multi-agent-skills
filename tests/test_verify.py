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



class TestRoundOneFindings(Base):
    """All four attacked the authorization chain, which is where I asked
    them to look."""

    def test_replacement_refs_cannot_swap_the_anchored_policy(self):
        """`git show A:f` follows refs/replace/A, and the agent can write
        one. The anchor is only an anchor if the object it names cannot be
        swapped underneath it."""
        # A second commit carrying a policy the agent would prefer.
        with open(os.path.join(self.repo, V.POLICY_FILE), "w") as fh:
            json.dump({"schema_version": 1, "verifiers": [
                {"name": "mine", "sha256": self.digest,
                 "claims": ["tests-pass"]}]}, fh)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "agent policy")
        evil = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "replace", self.base, evil)

        policy, _d, err = V.read_policy(U.run, self.repo, self.base)
        self.assertIsNone(err, err)
        names = [v["name"] for v in policy["verifiers"]]
        self.assertNotIn("mine", names,
                         "a replacement ref swapped the anchored policy")
        self.assertIn("tests", names)

    def test_a_string_claims_field_does_not_grant_substrings(self):
        """`claim not in "tests-pass-and-more"` is a substring test."""
        policy = {"verifiers": [{"name": "tests", "sha256": self.digest,
                                 "claims": "tests-pass-and-more"}]}
        entry, refusal = V.authorized(policy, "tests", self.digest,
                                      "tests-pass")
        self.assertIsNone(entry)
        self.assertIn("not a list", refusal)

    def test_the_verifier_must_run_at_the_produced_commit(self):
        """Test failing commit A, move to passing commit B, verify from B,
        and the receipt used to say A passed."""
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        ok, why = V.head_matches(U.run, self.repo, head)
        self.assertTrue(ok, why)
        ok, why = V.head_matches(U.run, self.repo, "b" * 40)
        self.assertFalse(ok)
        self.assertIn("says nothing about this one", why)

    def test_a_dirty_tree_is_not_the_commit_being_claimed_about(self):
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        with open(os.path.join(self.repo, "scratch.txt"), "w") as fh:
            fh.write("uncommitted\n")
        ok, why = V.head_matches(U.run, self.repo, head)
        self.assertFalse(ok)
        self.assertIn("uncommitted", why)


class TestAnUnreadablePolicyRefuses(unittest.TestCase):
    """Leaving the digest None skipped the comparison, so a receipt under any
    rules at all was admitted the moment `git show` failed. An unreadable
    authorization source is the strongest reason to refuse."""

    def test_advance_refuses_when_the_policy_cannot_be_read(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        self.assertIn("the authorizing policy cannot be read", src)

    def test_the_refusal_precedes_admission(self):
        src = (SCRIPTS / "swarm.py").read_text()
        i = src.index("the authorizing policy cannot be read")
        j = src.index("admit_verification(", i)
        self.assertLess(i, j, "admission runs before the policy-read check")


class TestTheBaseComesFromCoordinatorState(unittest.TestCase):
    """base_commit is the linchpin of the chain, and the launch record sits
    where the agent's Unix user can write."""

    def test_verify_reads_the_base_from_state(self):
        src = (SCRIPTS / "swarm.py").read_text()
        self.assertIn('.get(\n        "base_commit")', src.replace("\r", ""))

    def test_a_disagreement_is_refused_not_reconciled(self):
        # Each fragment must sit inside ONE string literal. The message is
        # built from adjacent f-strings, so a phrase spanning two of them is
        # separated by `" f"` in the source and no amount of whitespace
        # normalising will join it. Fourth time this shape has caught me.
        src = (SCRIPTS / "swarm.py").read_text()
        self.assertIn("changed after it was written", src)
        self.assertIn("authorization policy from a base", src)

    def test_the_base_is_recorded_at_dispatch_not_copied_back(self):
        """Copying the base out of the agent-writable launch record and then
        storing it as trusted state launders the very value the trust was
        meant to protect: state would agree with the record because it came
        FROM the record."""
        src = (SCRIPTS / "swarm.py").read_text()
        self.assertIn('.setdefault("base_commit", anchored_base)', src)
        self.assertNotIn('us.setdefault("base_commit", anchor_rec', src)

    def test_the_writer_returns_the_base_it_observed(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_write_launch_record")
        returns = [ast.unparse(n.value) for n in ast.walk(fn)
                   if isinstance(n, ast.Return) and n.value is not None]
        self.assertTrue(returns)
        for r in returns:
            self.assertIn(",", r,
                          "every return must carry (error, base); a bare "
                          "error leaves the caller with no base to record")


class TestAdmissionRechecksTheVerifier(unittest.TestCase):
    """Checking authorization only when the receipt was WRITTEN meant the
    receipt's own claim about which verifier ran was then taken on trust."""

    def _write(self, d, rec):
        os.makedirs(d, exist_ok=True)
        with open(Path(d) / S.VERIFY_RECEIPTS, "w") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _rec(self, **over):
        r = {"unit": "u1", "claim": "tests-pass", "verifier": "tests",
             "verifier_sha256": "v" * 64, "policy_sha256": "p" * 64,
             "subject_head": "a" * 40, "result": "pass"}
        r.update(over)
        return r

    POLICY = {"verifiers": [{"name": "tests", "sha256": "v" * 64,
                             "claims": ["tests-pass"]}]}

    def test_a_receipt_naming_an_unauthorized_verifier_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, self._rec(verifier="mine", verifier_sha256="z" * 64))
            got, refusal = S.admit_verification(
                d, "u1", "tests-pass", "a" * 40, "p" * 64, policy=self.POLICY)
            self.assertIsNone(got)
            self.assertIn("not authorized by that policy", refusal)

    def test_a_receipt_with_the_wrong_digest_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, self._rec(verifier_sha256="z" * 64))
            got, refusal = S.admit_verification(
                d, "u1", "tests-pass", "a" * 40, "p" * 64, policy=self.POLICY)
            self.assertIsNone(got)
            self.assertIn("not authorized", refusal)

    def test_an_authorized_receipt_is_admitted(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, self._rec())
            got, refusal = S.admit_verification(
                d, "u1", "tests-pass", "a" * 40, "p" * 64, policy=self.POLICY)
            self.assertIsNone(refusal)
            self.assertIsNotNone(got)


class TestTheBoundaryIsStated(unittest.TestCase):
    """Four findings in one round were 'the agent can write that file'. That
    is true of every file, so it is scoped rather than chased."""

    def test_verify_states_what_it_does_not_establish(self):
        doc = (SCRIPTS / "verify.py").read_text()
        self.assertIn("WHAT THIS DOES NOT ESTABLISH", doc)
        # The phrase wraps across a line in the module docstring, so
        # normalise whitespace before matching prose.
        self.assertIn("A HOSTILE agent is out of scope",
                      " ".join(doc.split()))

    def test_a_failed_status_is_not_a_clean_tree(self):
        src = (SCRIPTS / "verify.py").read_text()
        self.assertIn('"could not look" is not "nothing there"',
                      src.replace("\n", " ").replace("    ", " "))

if __name__ == "__main__":
    unittest.main()
