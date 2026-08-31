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

if __name__ == "__main__":
    unittest.main()
