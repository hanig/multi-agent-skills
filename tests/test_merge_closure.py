"""How a code unit closes: a merged PR, attested and BOUND.

Closure authority for `code` is a merged pull request, and the coordinator has
no network imports, so it cannot ask GitHub anything. The evidence arrives as
an attestation from a session that can see the PR.

An attestation alone would be worthless: an attester naming any commit could
close any unit. What makes it admissible is that the head it pins must equal
the head this coordinator judged the attempt to have produced, from an anchor
written before the agent existed. The attester can lie about whether a PR
merged; it cannot make this unit's produced commit be a different commit.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402

HEAD = "a" * 40
OTHER = "b" * 40


def receipt(**over):
    r = {"unit": "u1", "repo": "/r", "pr": "https://x/pr/1", "target": "main",
         "head": HEAD, "merged_as": "c" * 40, "method": "merge",
         "merged": True, "attested": True}
    r.update(over)
    return r


def write(state_dir, *recs):
    os.makedirs(state_dir, exist_ok=True)
    with open(Path(state_dir) / S.MERGE_RECEIPTS, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


class TestTheBinding(unittest.TestCase):
    """The one check a coordinator without a network can make."""

    def test_a_receipt_pinning_the_produced_head_is_admitted(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt())
            got, refusal = S.admit_merge(d, "u1", HEAD)
            self.assertIsNone(refusal)
            self.assertEqual(got["merged_as"], "c" * 40)

    def test_a_receipt_pinning_another_commit_is_refused(self):
        """Otherwise a merge of somebody else's work closes this unit."""
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt(head=OTHER))
            got, refusal = S.admit_merge(d, "u1", HEAD)
            self.assertIsNone(got)
            self.assertIn("does not close this unit", refusal)

    def test_a_receipt_for_another_unit_does_not_count(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt(unit="somebody-else"))
            got, refusal = S.admit_merge(d, "u1", HEAD)
            self.assertIsNone(got)
            self.assertIn("no merge receipt", refusal)

    def test_no_produced_commit_means_nothing_to_bind_to(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt())
            got, refusal = S.admit_merge(d, "u1", None)
            self.assertIsNone(got)
            self.assertIn("nothing a merge could be bound to", refusal)

    def test_the_right_receipt_among_several_is_found(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt(head=OTHER), receipt(unit="other"), receipt())
            got, refusal = S.admit_merge(d, "u1", HEAD)
            self.assertIsNone(refusal)
            self.assertEqual(got["head"], HEAD)


class TestAnUnmergedPRDoesNotClose(unittest.TestCase):

    def test_an_open_pr_is_not_a_merge(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt(merged=False))
            got, refusal = S.admit_merge(d, "u1", HEAD)
            self.assertIsNone(got)
            self.assertIn("no merge receipt", refusal)

    def test_no_receipt_at_all_names_the_command_to_run(self):
        with tempfile.TemporaryDirectory() as d:
            got, refusal = S.admit_merge(d, "u1", HEAD)
            self.assertIsNone(got)
            self.assertIn("swarm.py merge", refusal)


class TestMalformedReceiptsAreNotEvidence(unittest.TestCase):

    def test_every_pinning_field_is_required(self):
        for field in ("unit", "repo", "pr", "target", "head", "merged_as",
                      "method"):
            self.assertIsNotNone(
                S._merge_shape_problem(receipt(**{field: ""})), field)

    def test_an_unknown_merge_method_fails_closed(self):
        """An unrecorded method means `merged_as` cannot be interpreted."""
        bad = S._merge_shape_problem(receipt(method="cherry-pick"))
        self.assertIn("cannot be interpreted", bad)

    def test_every_documented_method_is_accepted(self):
        for m in S.MERGE_METHODS:
            self.assertIsNone(S._merge_shape_problem(receipt(method=m)), m)

    def test_a_malformed_line_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d, exist_ok=True)
            with open(Path(d) / S.MERGE_RECEIPTS, "w") as fh:
                fh.write(json.dumps(receipt()) + "\n")
                fh.write("NOT JSON\n")
            with self.assertRaises(S.OutboxError):
                S.load_merge_receipts(d)

    def test_an_open_pr_record_is_readable_but_not_admissible(self):
        """Shape answers "can this be read", admission answers "does this
        close the unit". Conflating them made one uninteresting record
        poison the whole journal."""
        self.assertIsNone(S._merge_shape_problem(receipt(merged=False)))
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt(merged=False))
            S.load_merge_receipts(d)             # readable
            got, refusal = S.admit_merge(d, "u1", HEAD)
            self.assertIsNone(got)               # not admissible


class TestRecordingRefusesABadJournal(unittest.TestCase):

    class _Args:
        unit = "u1"
        pr = "https://x/pr/1"
        head = HEAD
        target = "main"
        merged_as = "c" * 40
        method = "merge"
        repo = "/r"

    def test_it_will_not_extend_a_corrupt_journal(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.MERGE_RECEIPTS, "w") as fh:
                fh.write("NOT JSON\n")
            a = self._Args()
            a.state_dir = d
            self.assertEqual(S.cmd_merge(a), S.EXIT_CONFLICT)

    def test_a_good_record_is_written_and_admissible(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._Args()
            a.state_dir = d
            self.assertEqual(S.cmd_merge(a), S.EXIT_OK)
            got, refusal = S.admit_merge(d, "u1", HEAD)
            self.assertIsNone(refusal)
            self.assertTrue(got["attested"])


class TestClosureAuthorityIsUnchanged(unittest.TestCase):

    def test_code_still_closes_on_a_merged_pr(self):
        self.assertEqual(S.closing_evidence_for("code"), "merged_pr")

    def test_slurm_does_not_need_one(self):
        self.assertEqual(S.closing_evidence_for("slurm"), "predicate_receipt")

    def test_the_merge_path_is_never_consulted_for_slurm(self):
        """A merge receipt must not become a way to close a Slurm unit."""
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "admit_merge")
        self.assertNotIn("slurm", ast.unparse(fn))



class TestTheCommandCanActuallyBeRun(unittest.TestCase):
    """--repo was optional in argparse while the shape check required it, so
    the command could only ever fail. Every unit test passed a repo, so only
    running it found this."""

    def test_every_field_the_shape_requires_is_required_by_the_parser(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        tree = ast.parse(src)
        required = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "add_argument":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            flag = node.args[0].value
            if not isinstance(flag, str) or not flag.startswith("--"):
                continue
            opt = flag[2:].replace("-", "_")
            for kw in node.keywords:
                if (kw.arg == "required"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value):
                    required.add(opt)
        for field in S._MERGE_REQUIRED:
            if field == "merged":
                continue
            self.assertIn(field, required,
                          f"--{field.replace('_', '-')} is optional in the "
                          f"parser but mandatory in the receipt, so the "
                          f"command can only fail")


class TestRoundOneFindings(unittest.TestCase):
    """Three MAJOR findings, all attacking the binding, all real."""

    def test_the_receipts_repository_must_match_the_anchored_remote(self):
        """The attester reports what it SAW. It does not get to decide which
        repository this unit belongs to."""
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt(repo="someone-else/unrelated"))
            got, refusal = S.admit_merge(d, "u1", HEAD,
                                         expect_repo="hanig/demo")
            self.assertIsNone(got)
            self.assertIn("not to decide which repository", refusal)

    def test_equivalent_spellings_of_the_same_repo_match(self):
        """Same host, different transport, still the same repository."""
        for spelling in ("git@github.com:hanig/demo.git",
                         "https://github.com/hanig/demo",
                         "https://github.com/hanig/demo.git",
                         "ssh://git@github.com/hanig/demo",
                         "HTTPS://GITHUB.COM/HANIG/DEMO"):
            self.assertTrue(
                S._same_repo(spelling, "https://github.com/hanig/demo"),
                spelling)

    def test_unrelated_names_do_not_match(self):
        for other in ("hanig/other", "someone/demo", "", None):
            self.assertFalse(S._same_repo(other, "hanig/demo"), repr(other))

    def test_a_repo_that_cannot_be_reduced_compares_literally(self):
        self.assertTrue(S._same_repo("weirdname", "weirdname"))
        self.assertFalse(S._same_repo("weirdname", "otherthing"))

    def test_no_expected_repo_means_the_check_does_not_apply(self):
        """An anchor with no remote must not block closure outright."""
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt(repo="anything/at-all"))
            got, refusal = S.admit_merge(d, "u1", HEAD, expect_repo=None)
            self.assertIsNone(refusal)
            self.assertIsNotNone(got)


class TestReadyForPRIsNotADeadEnd(unittest.TestCase):
    """Guarding admission on DONE alone meant the first advance moved a
    produced unit to READY_FOR_PR and never looked again, so a receipt
    recorded afterwards did nothing. Recording after the fact is the NORMAL
    order: the PR is merged after the unit produced it."""

    def test_advance_reconsiders_a_unit_already_in_ready_for_pr(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            text = ast.unparse(node)
            if "admit" in text:
                continue
            if "READY_FOR_PR" in text and "DONE" in text and " in " in text:
                found = True
        self.assertTrue(found,
                        "admission is reachable only from DONE, so a unit "
                        "parked in READY_FOR_PR can never close")


class TestTheBindingUsesTheJudgedHead(unittest.TestCase):
    """produced_head re-read HEAD after judging, so an agent moving HEAD
    between the two closed the unit for work nothing had validated."""

    def test_produced_head_comes_from_the_judgment_itself(self):
        import ast
        src = (SCRIPTS / "worktree.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "produced_head")
        body = ast.unparse(fn)
        self.assertIn("judge_detail", body)
        self.assertNotIn("rev-parse", body,
                         "the head is re-read after judging, which is the "
                         "time-of-check/time-of-use gap this closed")

    def test_judge_detail_returns_the_head_it_validated(self):
        sys.path.insert(0, str(SCRIPTS))
        import worktree as W
        import inspect
        src = inspect.getsource(W.judge_detail)
        self.assertIn("return True, head,", src)


class TestRoundTwoFindings(unittest.TestCase):

    def test_a_lookalike_host_is_refused(self):
        """github.com/acme/app and evil.example:acme/app both reduce to
        acme/app, so a PR in a lookalike repository could close a unit."""
        self.assertFalse(S._same_repo("https://github.com/acme/app.git",
                                      "git@evil.example:acme/app.git"))

    def test_the_same_host_in_different_spellings_still_matches(self):
        self.assertTrue(S._same_repo("https://github.com/acme/app.git",
                                     "git@github.com:acme/app.git"))
        self.assertTrue(S._same_repo("ssh://git@github.com/acme/app",
                                     "https://github.com/acme/app"))

    def test_a_bare_owner_name_no_longer_matches_a_full_url(self):
        """Shorthand WAS accepted, and that permissiveness was the hole: a
        hostless anchor matched a host-bearing lookalike. Identity needs both
        parts, including both being absent."""
        self.assertFalse(S._same_repo("acme/app",
                                      "https://github.com/acme/app.git"))
        self.assertTrue(S._same_repo("acme/app", "acme/app"))

    def test_a_hostless_anchor_does_not_match_a_lookalike(self):
        self.assertFalse(S._same_repo("https://evil.example/acme/app",
                                      "acme/app"))

    def test_a_different_owner_never_matches(self):
        self.assertFalse(S._same_repo("https://github.com/other/app",
                                      "https://github.com/acme/app"))


class TestClosedIsClosed(unittest.TestCase):
    """Adding READY_FOR_PR to the admission guard, to fix a unit that could
    never leave it, created the opposite defect: a unit that HAD closed was
    re-judged every advance, so the agent dirtying its repository afterwards
    drove a DONE unit back to READY_FOR_PR."""

    def test_an_admitted_merge_is_recorded_on_the_unit(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        self.assertIn('us["merged_as"] = receipt.get("merged_as")', src)

    def test_the_guard_skips_a_unit_that_already_closed(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        tree = ast.parse(src)
        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            text = ast.unparse(node.test)
            if "READY_FOR_PR" in text and "DONE" in text:
                self.assertIn("merged_as", text,
                              "a closed unit is re-judged, so its repository "
                              "changing later can reopen it")
                guarded = True
        self.assertTrue(guarded, "the admission guard was not found")


class TestTheTreeBelongsToThePinnedCommit(unittest.TestCase):
    """Reading HEAD^{tree} was a second look at a moving target: the agent
    could leave an empty descendant at HEAD for the first read and a
    content-changing one for the second, so the tree that satisfied the check
    belonged to a commit other than the one pinned."""

    def test_the_tree_is_read_from_the_captured_head(self):
        import ast
        src = (SCRIPTS / "worktree.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "judge_detail")
        body = ast.unparse(fn)
        self.assertNotIn("'HEAD^{tree}'", body)
        self.assertIn("head + '^{tree}'", body.replace('"', "'"))


class TestRoundThreeFindings(unittest.TestCase):

    def test_a_lookalike_path_prefix_is_not_the_host(self):
        """https://evil.example/github.com/acme/app presented github.com as
        its host when the host was read as parts[-3]."""
        self.assertFalse(S._same_repo(
            "https://evil.example/github.com/acme/app.git",
            "https://github.com/acme/app.git"))

    def test_the_genuine_host_still_matches_itself(self):
        self.assertTrue(S._same_repo("https://github.com/acme/app.git",
                                     "git@github.com:acme/app.git"))

    def test_a_wrong_repository_receipt_does_not_mask_a_later_correct_one(self):
        """Returning on the first mismatch parked the unit forever on one
        attester slip."""
        with tempfile.TemporaryDirectory() as d:
            write(d,
                  receipt(repo="git@evil.example:acme/app.git"),
                  receipt(repo="https://github.com/acme/app.git"))
            got, refusal = S.admit_merge(
                d, "u1", HEAD, expect_repo="https://github.com/acme/app.git")
            self.assertIsNone(refusal)
            self.assertIsNotNone(got)

    def test_only_wrong_repositories_reports_them_all(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, receipt(repo="git@evil.example:acme/app.git"))
            got, refusal = S.admit_merge(
                d, "u1", HEAD, expect_repo="https://github.com/acme/app.git")
            self.assertIsNone(got)
            self.assertIn("does not block it", refusal)


class TestTheProducedHeadIsRecordedNotRederived(unittest.TestCase):
    """The root cause behind most findings in three rounds: the coordinator
    kept asking a repository the agent owns what it had produced, and the
    answer changed between askings."""

    def test_advance_persists_the_produced_head(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        self.assertIn('us["produced_head"] = produced', src)

    def test_a_recorded_head_is_preferred_over_rejudging(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        i = src.index("attempt = us.get(\"attempt_dir\") or \"\"")
        seg = src[i:i + 900]
        self.assertIn('us.get("produced_head")', seg)
        j = seg.index('us.get("produced_head")')
        k = seg.index("W.produced_head(")
        self.assertLess(j, k,
                        "the repository is re-judged before the recorded "
                        "head is consulted, so a moved branch still wins")


class TestTheJudgedHeadIsInTheReceipt(unittest.TestCase):
    """Recording it during merge admission was still a second question to a
    repository the agent owns. The receipt is written at the moment the
    judgment is true."""

    def test_the_receipt_carries_the_judged_head(self):
        import ast
        src = (SCRIPTS / "worktree.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "code_basis")
        self.assertIn("produced_head", ast.unparse(fn))

    def test_admission_prefers_the_receipt_over_rejudging(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        i = src.index('produced = us.get("produced_head")')
        seg = src[i:i + 300]
        self.assertIn("_head_from_receipt", seg)
        self.assertLess(seg.index("_head_from_receipt"),
                        seg.index("W.produced_head("),
                        "the repository is re-judged before the receipt is "
                        "consulted")

    def test_head_from_receipt_reads_the_basis(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / "receipt.json", "w") as fh:
                json.dump({"basis": {"produced_head": HEAD}}, fh)
            self.assertEqual(S._head_from_receipt(d), HEAD)

    def test_a_missing_or_unreadable_receipt_is_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(S._head_from_receipt(d))
            with open(Path(d) / "receipt.json", "w") as fh:
                fh.write("NOT JSON")
            self.assertIsNone(S._head_from_receipt(d))
        self.assertIsNone(S._head_from_receipt(""))

if __name__ == "__main__":
    unittest.main()
