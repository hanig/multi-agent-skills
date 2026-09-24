"""Offline ledger tests through the CLI, with real isolated journal writes."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/hanig-review-gate/scripts/review.py"
SPEC = importlib.util.spec_from_file_location("arc709_review", SCRIPT)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)
AUTHOR = "codex/gpt-6-astra"
HEAD = "1" * 40


class TestAdjudication(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=review.JOURNAL_TEST_ROOT_PREFIX)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        review._require_git_free_test_root(self.root)
        self.state = self.root / "state"
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.state),
            review.JOURNAL_TEST_MARKER: str(self.root),
            "OPENAI_API_KEY": "", "OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.journal = self.state / review.JOURNAL_DIR / review.JOURNAL_NAME
        self.finding = {
            "file": "sample.py", "line": 7, "severity": "major",
            "confidence": "high", "summary": "missing input is lost",
            "failure_scenario": "a missing input produces an empty result",
        }
        self.digest = review.finding_digest("sample.py:7", self.finding["summary"])

    def seed(self, head=HEAD, round_no=1, finding=None):
        result = {"name": "independent", "verdict": "refuted", "claims": [],
                  "findings": [self.finding if finding is None else finding]}
        _record, path = review.append_review_journal(
            self.journal, "plan" if round_no is None else "implementation",
            round_no, ["independent"],
            "REVIEW_FAIL", [review.HONEST_RUN_CLAIM], results=[result],
            reviewed_head=head)
        return path

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", [str(SCRIPT), "--author", AUTHOR, *args]), \
                patch.object(review, "load_reviewers", side_effect=AssertionError("offline ledger")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                review.main()
        return SimpleNamespace(returncode=stopped.exception.code,
                               stdout=stdout.getvalue(), stderr=stderr.getvalue())

    def query(self, head=HEAD):
        return self.invoke("--open-findings", "--head", head)

    def adjudicate(self, *extra, digest=None, head=HEAD, acceptor="owner", reason="Reproduction rules it out."):
        return self.invoke("--adjudicate", self.digest if digest is None else digest,
                           "--head", head, "--accepted-by", acceptor,
                           "--reason", reason, *extra)

    def records(self):
        return [json.loads(p.read_text()) for p in sorted(self.journal.glob("*/record.jsonl"))]

    def test_record_query_round_trip_preserves_failed_review_bytes(self):
        original = self.seed()
        before = original.read_bytes()
        opened = self.query()
        self.assertEqual(opened.returncode, 1, opened.stderr)
        finding = json.loads(opened.stdout)["open_findings"][0]
        self.assertEqual((finding["finding_digest"], finding["round"], finding["reviewed_head"]),
                         (self.digest, 1, HEAD))
        result = self.adjudicate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "ADJUDICATION_RECORDED")
        self.assertEqual(self.query().returncode, 0)
        self.assertEqual(original.read_bytes(), before)
        record = self.records()[-1]
        self.assertEqual(record["type"], "adjudication")
        self.assertEqual(record["finding_digest"], self.digest)
        self.assertEqual(record["reviewed_head"], HEAD)
        self.assertEqual(record["round"], 1)
        self.assertEqual(record["decision"], "overruled")
        self.assertEqual(record["author"], [AUTHOR])
        self.assertEqual(record["accepted_by"], "owner")
        self.assertEqual(record["reason"], "Reproduction rules it out.")
        self.assertRegex(record["timestamp"], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertEqual(self.records()[0]["verdict"], "REVIEW_FAIL")
        self.assertNotIn("verdict", record)

    def test_unnumbered_plan_finding_can_be_adjudicated(self):
        self.seed(round_no=None)
        self.assertEqual(self.query().returncode, 1)
        result = self.adjudicate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.records()[-1]["round"])
        self.assertEqual(self.query().returncode, 0)

    def test_author_self_acceptance_refused(self):
        self.seed()
        for acceptor in (AUTHOR, "openai/gpt-6-astra", "gpt-6-astra"):
            with self.subTest(acceptor=acceptor):
                result = self.adjudicate(acceptor=acceptor)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("author cannot accept", result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.query().returncode, 1)

    def test_coauthor_and_nested_model_identity_are_checked(self):
        self.seed()
        for acceptor in ("local/moonshotai/kimi-k2.7-code", "openrouter/moonshotai/kimi-k2.7-code",
                         "moonshotai/kimi-k2.7-code"):
            result = self.adjudicate("--author", "openrouter/moonshotai/kimi-k2.7-code", acceptor=acceptor)
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        for acceptor in ("owner-gpt-6-astra", "codex/gpt-6-astra-variant", "codex/GPT-6-ASTRA"):
            result = self.adjudicate(acceptor=acceptor)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_digest_wrong_head_unconfirmed_and_empty_reason_refused(self):
        self.seed()
        self.seed(head="2" * 40, finding={**self.finding, "severity": "minor"})
        for kwargs in ({"digest": "0" * 64}, {"head": "3" * 40},
                       {"head": "2" * 40}, {"reason": ""}, {"reason": " \n\t"}):
            with self.subTest(kwargs=kwargs):
                result = self.adjudicate(**kwargs)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 2)

    def test_missing_author_and_blank_acceptor_refused(self):
        self.seed()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--adjudicate", self.digest,
             "--head", HEAD, "--accepted-by", "owner", "--reason", "reason"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        for acceptor in ("", " ", " owner", "owner\nother", "local/ gpt-6-astra"):
            self.assertEqual(self.adjudicate(acceptor=acceptor).returncode, 4)
        self.assertEqual(len(self.records()), 1)

    def test_each_decision_is_a_disposition_never_a_pass(self):
        for round_no, decision in enumerate(review.ADJUDICATION_DECISIONS, 1):
            self.seed(round_no=round_no)
            result = self.adjudicate("--decision", decision, "--round", str(round_no))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["review_verdict_changed"])
            self.assertEqual(self.records()[-1]["decision"], decision)
            self.assertEqual(self.query().returncode, 0)
        self.assertEqual([r["verdict"] for r in self.records() if r["type"] == "review_round"],
                         ["REVIEW_FAIL"] * 3)

    def test_same_digest_other_head_or_round_stays_open(self):
        self.seed()
        self.seed(round_no=2)
        self.seed(head="2" * 40)
        ambiguous = self.adjudicate()
        self.assertEqual(ambiguous.returncode, 4)
        self.assertIn("--round", ambiguous.stderr)
        self.assertEqual(self.adjudicate("--round", "1").returncode, 0)
        opened = json.loads(self.query().stdout)["open_findings"]
        self.assertEqual([f["round"] for f in opened], [2])
        self.assertEqual(self.query("2" * 40).returncode, 1)

    def test_query_is_read_only_and_pending_records_are_not_history(self):
        self.assertEqual(self.query().returncode, 0)
        self.assertFalse(self.state.exists())
        original = self.seed()
        pending = self.journal / "interrupted" / "record.pending"
        pending.parent.mkdir()
        pending.write_text('{"partial":')
        before = {p: p.read_bytes() for p in self.journal.rglob("*") if p.is_file()}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--open-findings", "--head", HEAD,
             "--author", AUTHOR], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(before, {p: p.read_bytes() for p in self.journal.rglob("*") if p.is_file()})
        self.assertIn(original, before)

    def test_legacy_headless_records_are_preserved_not_rebound(self):
        original = self.seed(head=None)
        record = json.loads(original.read_text())
        record.pop("reviewed_head")
        record["results"][0]["findings"][0].pop("finding_digest")
        original.write_text(json.dumps(record) + "\n")
        before = original.read_bytes()
        self.assertEqual(self.query().returncode, 0)
        self.assertEqual(self.adjudicate().returncode, 4)
        self.assertEqual(original.read_bytes(), before)

    def test_redaction_and_one_line_atomic_records(self):
        self.seed()
        secret = 'sk-"arc709secret\\value\nsecond'
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            result = self.adjudicate(reason="Fixture " + secret + "\nExplained separately.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("arc709secret", result.stdout + result.stderr)
        for path in self.journal.glob("*/record.jsonl"):
            raw = path.read_bytes()
            self.assertEqual(raw.count(b"\n"), 1)
            self.assertTrue(raw.endswith(b"\n"))
            self.assertNotIn(b"arc709secret", raw)
            json.loads(raw)
        self.assertIn("<OPENAI_API_KEY redacted>", self.records()[-1]["reason"])
        self.assertFalse(list(self.journal.glob("*/record.pending")))

    def test_storage_failure_does_not_change_command_outcome_or_close_finding(self):
        self.seed()
        self.journal.chmod(0o500)
        try:
            result = self.adjudicate()
        finally:
            self.journal.chmod(0o700)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["state"], "ADJUDICATION_UNCONFIRMED")
        self.assertFalse(report["journal"]["written"])
        self.assertIn("JOURNAL_WRITE_FAILED", result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.query().returncode, 1)

    def test_serialization_and_timeout_failures_are_non_gating(self):
        self.seed()
        for failure in (ValueError("serialize"), TimeoutError("stalled writer")):
            with patch.object(review, "_run_journal_append", side_effect=failure):
                result = self.adjudicate()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["journal"]["written"])
            self.assertEqual(self.query().returncode, 1)

    def test_corrupt_or_unreadable_canonical_history_cannot_report_no_open_findings(self):
        path = self.seed()
        for raw in ('{broken\n', '{}\n{}\n', '[]\n', '{"a":1,"a":2}\n'):
            path.write_text(raw)
            self.assertEqual(self.query().returncode, 4)
        path.unlink()
        os.mkfifo(path)
        result = self.query()
        self.assertEqual(result.returncode, 4)
        self.assertIn("not a regular file", result.stderr)

    def test_full_head_required_and_ledger_cannot_replace_review(self):
        self.seed()
        for head in ("HEAD", "1" * 7, " " + HEAD, HEAD + "0"):
            self.assertEqual(self.query(head).returncode, 4)
        for extra in (("--kind", "implementation"), ("--diff",), ("--range", "HEAD~1..HEAD"),
                      ("--list",), ("--claim", review.HONEST_RUN_CLAIM)):
            self.assertEqual(self.adjudicate(*extra).returncode, 4)
        self.assertEqual(self.invoke("--head", HEAD).returncode, 4)

    def test_review_binds_range_head_and_adjudication_never_changes_next_verdict(self):
        repo = self.root / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "commit.gpgsign", "false")
        (repo / "sample.py").write_text("before\n")
        git("add", "sample.py")
        git("commit", "-qm", "base")
        (repo / "sample.py").write_text("after\n")
        git("commit", "-qam", "reviewed")
        reviewed_head = git("rev-parse", "HEAD")
        git("branch", "candidate")
        (repo / "sample.py").write_text("later unreviewed content\n")
        git("commit", "-qam", "unreviewed later commit")
        roster = [{"name": n, "model": n, "provider": "stub", "profiles": ["standard"]}
                  for n in ("first", "second")]
        original_git_out = review.git_out
        def moving_diff(*args):
            if args[0] == "diff":
                git("branch", "-f", "candidate", "HEAD")
            return original_git_out(*args)
        def answer(reviewer, prompt, *_args):
            self.assertIn("+after", prompt)
            self.assertNotIn("+later unreviewed content", prompt)
            return {"ok": True, "name": reviewer["name"], "verdict": "refuted",
                    "findings": [self.finding], "claims": [], "elapsed_s": 0}
        previous = Path.cwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, previous)
        argv = [str(SCRIPT), "--range", reviewed_head + "~1..candidate", "--kind", "implementation",
                "--round", "1", "--author", AUTHOR, "--claim", review.HONEST_RUN_CLAIM, "--json"]
        for iteration in range(2):
            git("branch", "-f", "candidate", reviewed_head)
            stdout = io.StringIO()
            with patch.object(sys, "argv", argv), patch.object(review, "load_reviewers", return_value=roster), \
                    patch.object(review, "availability", return_value=None), patch.object(review, "run_one", side_effect=answer), \
                    patch.object(review, "git_out", side_effect=moving_diff), \
                    patch.object(review, "arm_watchdog"), patch.object(review, "disarm_watchdog"), redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as stopped:
                    review.main()
            self.assertEqual(stopped.exception.code, 1)
            self.assertEqual(json.loads(stdout.getvalue())["state"], "REVIEW_FAIL")
            self.assertEqual(self.records()[-1]["reviewed_head"], reviewed_head)
            if iteration == 0:
                self.assertEqual(self.adjudicate(head=reviewed_head).returncode, 0)
                self.assertEqual(self.query(reviewed_head).returncode, 0)


if __name__ == "__main__":
    unittest.main()
