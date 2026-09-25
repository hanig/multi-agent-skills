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

    def test_explicit_plan_selector_disambiguates_from_numbered_round(self):
        plan = self.seed(round_no=None)
        numbered = self.seed(round_no=1)
        before = {p: p.read_bytes() for p in (plan, numbered)}
        ambiguous = self.adjudicate()
        self.assertEqual(ambiguous.returncode, 4)
        self.assertIn("--round plan", ambiguous.stderr)
        self.assertIn("--round 1", ambiguous.stderr)
        recorded = self.adjudicate("--round", "plan")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertIsNone(self.records()[-1]["round"])
        self.assertIsNone(json.loads(recorded.stdout)["round"])
        query = self.query()
        self.assertEqual(query.returncode, 1, query.stderr)
        self.assertEqual([f["round"] for f in json.loads(query.stdout)["open_findings"]], [1])
        self.assertEqual(self.adjudicate("--round", "1").returncode, 0)
        self.assertEqual(self.query().returncode, 0)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_plan_selector_requires_plan_occurrence_and_adjudication_mode(self):
        self.seed()
        for result in (self.adjudicate("--round", "plan"),
                       self.invoke("--open-findings", "--head", HEAD, "--round", "plan"),
                       self.invoke("--kind", "implementation", "--round", "plan"),
                       self.invoke("--kind", "plan", "--round", "plan")):
            self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(len(self.records()), 1)

    def collision_round_trip(self, secret, round_no=1):
        """Exercise persisted values through the CLI's real query consumer."""
        stamp = "2026-09-24T12:34:56.123456789Z"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}), \
                patch.object(review, "journal_timestamp", return_value=stamp):
            path = self.seed(round_no=round_no)
            record = json.loads(path.read_text())
            expected = {"reviewed_head": HEAD, "round": round_no,
                        "claim_digests": review.claim_digests([review.HONEST_RUN_CLAIM]),
                        "type": "review_round", "schema_version": 2,
                        "date": stamp, "kind": "implementation", "verdict": "REVIEW_FAIL"}
            for field, value in expected.items():
                self.assertEqual(record[field], value, field)
            self.assertEqual(record["results"][0]["findings"][0]["finding_digest"], self.digest)
            self.assertIs(record["results"][0]["findings"][0]["confirmed"], True)
            opened = self.query()
            self.assertEqual(opened.returncode, 1, opened.stderr)
            finding = json.loads(opened.stdout)["open_findings"][0]
            self.assertEqual((finding["reviewed_head"], finding["finding_digest"], finding["round"]),
                             (HEAD, self.digest, round_no))
            recorded = self.adjudicate(reason="Reason containing " + secret)
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            self.assertTrue(json.loads(recorded.stdout)["journal"]["written"])
            adjudication = self.records()[-1]
            expected = {"reviewed_head": HEAD, "finding_digest": self.digest,
                        "round": round_no, "type": "adjudication", "schema_version": 2,
                        "timestamp": stamp, "author": [AUTHOR], "accepted_by": "owner",
                        "decision": "overruled"}
            for field, value in expected.items():
                self.assertEqual(adjudication[field], value, field)
            if len(secret) >= 4:
                self.assertIn("<OPENAI_API_KEY redacted>", adjudication["reason"])
            closed = self.query()
            self.assertEqual(closed.returncode, 0, closed.stderr)
            self.assertEqual(json.loads(closed.stdout)["state"], "NO_OPEN_FINDINGS")
        self.assertEqual(self.query().returncode, 0)

    def test_author_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("codex")

    def test_accepted_by_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("owner")

    def test_head_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip(HEAD[:4])

    def test_finding_digest_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip(self.digest[:4])

    def test_claim_digest_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip(review.claim_digests([review.HONEST_RUN_CLAIM])[0][:4])

    def test_round_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("1234", round_no=1234)

    def test_decision_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("overruled")

    def test_type_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("adjudication")

    def test_review_type_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("review_round")

    def test_schema_version_secret_collision_is_exact_and_queryable(self):
        # The typed integer has no string-redaction path, even for short keys.
        self.collision_round_trip("2")

    def test_schema_label_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("schema_version")

    def test_timestamp_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("123456789")

    def test_review_date_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("2026-09-24")

    def test_kind_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("implementation")

    def test_verdict_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("REVIEW_FAIL")

    def test_damaged_verdict_is_unattributable_even_after_adjudication(self):
        path = self.seed()
        self.assertEqual(self.adjudicate().returncode, 0)
        original = json.loads(path.read_text())
        for head in (HEAD, "2" * 40):
            for verdict in ("<OPENAI_API_KEY redacted>", "unknown", None, True):
                with self.subTest(head=head, verdict=verdict):
                    record = dict(original, verdict=verdict, reviewed_head=head)
                    path.write_text(json.dumps(record) + "\n")
                    before = path.read_bytes()
                    query = self.query()
                    self.assertEqual(query.returncode, 1, query.stderr)
                    output = json.loads(query.stdout)
                    self.assertEqual(output["state"], "UNATTRIBUTABLE")
                    self.assertEqual(output["unattributable_records"][0]["record_path"], str(path))
                    self.assertEqual(path.read_bytes(), before)

    def test_invalid_review_fields_are_refused_before_publication(self):
        for field, value in (("verdict", "not-a-state"), ("kind", "not-a-kind"),
                             ("round_no", True), ("reviewed_head", "not-a-head")):
            kwargs = dict(kind="implementation", round_no=1, effective_panel=[],
                          verdict="REVIEW_PASS", claims=[], reviewed_head=HEAD)
            kwargs[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                review.append_review_journal(self.journal, **kwargs)
        self.assertEqual(self.records(), [])

    def test_confirmed_secret_collision_is_exact_and_queryable(self):
        self.collision_round_trip("true")

    def test_invalid_identity_tokens_are_refused_before_recording(self):
        self.seed()
        for token in ("white space", "newline\n", "nul\x00", "esc\x1b", "del\x7f",
                      "nonascii\u200b", "a" * 257, "<redacted>", "local//model", "local/"):
            with self.subTest(token=token):
                self.assertEqual(self.adjudicate(acceptor=token).returncode, 4)
                author = "codex/" + token
                # argparse rejects the same restricted token before ledger I/O.
                self.assertEqual(self.adjudicate("--author", author).returncode, 4)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.query().returncode, 1)

    def test_invalid_decision_fields_are_refused_at_record_time(self):
        args = SimpleNamespace(adjudicate=self.digest, head=HEAD, decision="overruled",
                               accepted_by="owner", author=[AUTHOR], reason="reason", file=[])
        for field, value in (("head", "bad-head"), ("adjudicate", "bad-digest"),
                             ("decision", "over ruled"), ("accepted_by", "own\x00er"),
                             ("author", ["codex/gpt\x1b"]), ("author", []), ("reason", 1)):
            with self.subTest(field=field), patch.object(args, field, value), \
                    patch.object(review, "_run_journal_append") as writer:
                with self.assertRaises(ValueError):
                    review.record_adjudication(args, 1)
                writer.assert_not_called()
        for round_no in (True, 0, -1, "1", "plan"):
            with self.subTest(round=round_no), self.assertRaises(ValueError):
                review.record_adjudication(args, round_no)
        for stamp in ("2026-02-30T12:34:56.123456789Z", "2026-09-24", "secret\n"):
            with patch.object(review, "journal_timestamp", return_value=stamp), \
                    self.assertRaises(ValueError):
                review.record_adjudication(args, 1)
        self.assertEqual(self.records(), [])

    def test_damaged_decision_fields_cannot_hide_on_another_head(self):
        original = self.seed()
        self.assertEqual(self.adjudicate().returncode, 0)
        adjudication = sorted(self.journal.glob("*/record.jsonl"))[-1]
        baseline = {p: p.read_bytes() for p in (original, adjudication)}
        cases = (("author", ["<OPENAI_API_KEY redacted>/gpt-6-astra"]),
                 ("accepted_by", "<OPENAI_API_KEY redacted>"),
                 ("decision", "<OPENAI_API_KEY redacted>"),
                 ("round", "1"), ("round", True), ("schema_version", True),
                 ("schema_version", 999), ("type", "<OPENAI_API_KEY redacted>"),
                 ("timestamp", "<OPENAI_API_KEY redacted>"), ("reason", None))
        for field, value in cases:
            for head in (HEAD, "2" * 40):
                with self.subTest(field=field, head=head):
                    record = json.loads(baseline[adjudication])
                    record.update({field: value, "reviewed_head": head})
                    adjudication.write_text(json.dumps(record) + "\n")
                    before = adjudication.read_bytes()
                    result = self.query()
                    self.assertEqual(result.returncode, 1, result.stderr)
                    output = json.loads(result.stdout)
                    self.assertEqual(output["state"], "UNATTRIBUTABLE")
                    self.assertEqual(output["unattributable_records"][0]["record_path"], str(adjudication))
                    self.assertEqual(adjudication.read_bytes(), before)
        adjudication.write_bytes(baseline[adjudication])
        for field, value in (("confirmed", "true"), ("confirmed", 1)):
            record = json.loads(baseline[original])
            record["reviewed_head"] = "2" * 40
            record["results"][0]["findings"][0][field] = value
            original.write_text(json.dumps(record) + "\n")
            result = self.query()
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)["state"], "UNATTRIBUTABLE")

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
        queried = self.query()
        self.assertEqual(queried.returncode, 1)
        report = json.loads(queried.stdout)
        self.assertEqual(report["state"], "UNATTRIBUTABLE")
        self.assertEqual(report["unattributable_records"][0]["record_path"], str(original))
        self.assertEqual(self.adjudicate().returncode, 4)
        self.assertEqual(original.read_bytes(), before)

    def test_head_prefix_secret_preserves_identity_and_open_finding(self):
        actual_head = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        for head in (actual_head, "1234abcd" * 8):
            with self.subTest(head=head):
                with patch.dict(os.environ, {"OPENAI_API_KEY": head[:4]}):
                    path = self.seed(head=head)
                    record = json.loads(path.read_text())
                    self.assertEqual(record["reviewed_head"], head)
                    self.assertIs(record["results"][0]["findings"][0]["confirmed"], True)
                    queried = self.query(head)
                    self.assertEqual(queried.returncode, 1, queried.stderr)
                    report = json.loads(queried.stdout)
                    self.assertEqual(report["state"], "OPEN_FINDINGS")
                    self.assertEqual(report["reviewed_head"], head)
                    self.assertEqual(report["open_findings"][0]["reviewed_head"], head)
                self.assertEqual(self.query(head).returncode, 1)

    def test_digest_prefix_secrets_preserve_review_and_adjudication_identities(self):
        claim_digests = review.claim_digests([review.HONEST_RUN_CLAIM])
        secrets = {"OPENAI_API_KEY": self.digest[:4],
                   "OPENROUTER_API_KEY": HEAD[:4],
                   "ANTHROPIC_API_KEY": claim_digests[0][:4]}
        with patch.dict(os.environ, secrets):
            path = self.seed()
            before = path.read_bytes()
            record = json.loads(before)
            self.assertEqual(record["claim_digests"], claim_digests)
            self.assertEqual(record["results"][0]["findings"][0]["finding_digest"], self.digest)
            report = json.loads(self.query().stdout)
            self.assertEqual(report["open_findings"][0]["finding_digest"], self.digest)
            result = self.adjudicate(reason="Reproduction excludes " + self.digest[:4])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["finding_digest"], self.digest)
            adjudication = self.records()[-1]
            self.assertEqual(adjudication["reviewed_head"], HEAD)
            self.assertEqual(adjudication["finding_digest"], self.digest)
            self.assertIn("<OPENAI_API_KEY redacted>", adjudication["reason"])
            self.assertEqual(self.query().returncode, 0)
        self.assertEqual(self.query().returncode, 0)
        self.assertEqual(path.read_bytes(), before)

    def test_pre_corrupted_legacy_record_is_named_and_cannot_be_adjudicated(self):
        path = self.seed()
        record = json.loads(path.read_text())
        record["reviewed_head"] = "<OPENAI_API_KEY redacted>" + HEAD[4:]
        path.write_text(json.dumps(record) + "\n")
        before = path.read_bytes()
        queried = self.query()
        self.assertEqual(queried.returncode, 1, queried.stderr)
        report = json.loads(queried.stdout)
        self.assertEqual(report["state"], "UNATTRIBUTABLE")
        damaged = report["unattributable_records"]
        self.assertEqual(len(damaged), 1)
        self.assertEqual(damaged[0]["record_path"], str(path))
        self.assertEqual(damaged[0]["reviewed_head"], record["reviewed_head"])
        self.assertIn("reviewed_head", " ".join(damaged[0]["problems"]))
        self.assertEqual(self.adjudicate().returncode, 4)
        self.assertEqual(self.adjudicate(head=record["reviewed_head"]).returncode, 4)
        self.assertEqual(path.read_bytes(), before)

    def test_schema_label_secrets_preserve_query_and_adjudication(self):
        labels = ("reviewed_head", "finding_digest", "claim_digests", "results",
                  "findings", "type", "review_round", "round", "confirmed",
                  "decision", "adjudication", "overruled", "author", "accepted_by",
                  "reason", "state", "OPEN_FINDINGS", "NO_OPEN_FINDINGS",
                  "ADJUDICATION_RECORDED", "journal", "written")
        for index, secret in enumerate(labels, 1):
            with self.subTest(secret=secret):
                head = format(index, "040x")
                with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
                    path = self.seed(head=head)
                    before = path.read_bytes()
                    query = self.query(head)
                    self.assertEqual(query.returncode, 1, query.stderr)
                    report = json.loads(query.stdout)
                    self.assertEqual(report["state"], "OPEN_FINDINGS")
                    self.assertEqual(report["open_findings"][0]["finding_digest"], self.digest)
                    recorded = self.adjudicate(head=head)
                    self.assertEqual(recorded.returncode, 0, recorded.stderr)
                    saved = json.loads(recorded.stdout)
                    self.assertEqual(saved["state"], "ADJUDICATION_RECORDED")
                    self.assertIs(saved["journal"]["written"], True)
                    after = self.query(head)
                    self.assertEqual(after.returncode, 0, after.stderr)
                    self.assertEqual(json.loads(after.stdout)["state"], "NO_OPEN_FINDINGS")
                self.assertEqual(self.query(head).returncode, 0)
                self.assertEqual(path.read_bytes(), before)

    def test_bad_digests_cannot_hide_behind_another_head_or_a_disposition(self):
        original = self.seed()
        self.assertEqual(self.adjudicate().returncode, 0)
        original_review = original.read_bytes()
        adjudication = sorted(self.journal.glob("*/record.jsonl"))[-1]
        original_adjudication = adjudication.read_bytes()
        for field in ("claim_digests", "finding_digest", "adjudication"):
            for head in (HEAD, "2" * 40):
                with self.subTest(field=field, head=head):
                    original.write_bytes(original_review)
                    adjudication.write_bytes(original_adjudication)
                    path = adjudication if field == "adjudication" else original
                    record = json.loads(path.read_text())
                    record["reviewed_head"] = head
                    bad_digest = "<OPENAI_API_KEY redacted>" + self.digest[4:]
                    if field == "claim_digests":
                        record[field] = [bad_digest]
                    elif field == "finding_digest":
                        record["results"][0]["findings"][0][field] = bad_digest
                    else:
                        record["finding_digest"] = bad_digest
                    path.write_text(json.dumps(record) + "\n")
                    before = path.read_bytes()
                    queried = self.query()
                    self.assertEqual(queried.returncode, 1, queried.stderr)
                    report = json.loads(queried.stdout)
                    self.assertEqual(report["state"], "UNATTRIBUTABLE")
                    self.assertEqual(report["unattributable_records"][0]["record_path"], str(path))
                    self.assertEqual(path.read_bytes(), before)

    def test_non_hex_head_is_refused_and_hex_free_text_is_redacted(self):
        secret = "secret-test-key"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            with self.assertRaises(ValueError):
                self.seed(head="not-a-head/" + secret)
        self.assertEqual(self.records(), [])
        with patch.dict(os.environ, {"OPENAI_API_KEY": HEAD[:4]}):
            path = self.seed(finding={**self.finding, "summary": HEAD,
                                     "extra": {"reviewed_head": HEAD}})
        record = json.loads(path.read_text())
        self.assertEqual(record["reviewed_head"], HEAD)
        finding = record["results"][0]["findings"][0]
        self.assertIn("<OPENAI_API_KEY redacted>", finding["summary"])
        self.assertIn("<OPENAI_API_KEY redacted>", finding["extra"]["reviewed_head"])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "reviewed_head"}):
            path = self.seed(finding={**self.finding, "extra": {"reviewed_head": HEAD}})
        record = json.loads(path.read_text())
        self.assertEqual(record["reviewed_head"], HEAD)
        self.assertEqual(record["results"][0]["findings"][0]["extra"],
                         {"<OPENAI_API_KEY redacted>": HEAD})

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

    def test_file_cannot_redirect_a_query_away_from_open_history(self):
        project_a, project_b, home = [self.root / name
                                    for name in ("project-a", "project-b", "home")]
        for repo in (project_a, project_b):
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        source = project_b / "input.py"
        source.write_text("pass\n")
        state = project_b / ".state"
        self.journal = state / review.JOURNAL_DIR / review.JOURNAL_NAME
        original = self.seed()
        before = original.read_bytes()
        previous = Path.cwd()
        os.chdir(project_a)
        self.addCleanup(os.chdir, previous)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}), \
                patch.object(review.Path, "home", return_value=home):
            self.assertEqual(self.query().returncode, 1)
            result = self.invoke("--open-findings", "--head", HEAD,
                                 "--file", str(source))
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertIn("Drop review options", result.stderr)
            self.assertEqual(self.adjudicate("--file", str(source)).returncode, 4)
            self.assertEqual(self.query().returncode, 1)
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(home.exists())

    def test_full_head_required_and_ledger_cannot_replace_review(self):
        self.seed()
        for head in ("HEAD", "1" * 7, " " + HEAD, HEAD + "0", "g" * 40,
                     HEAD + "\n"):
            self.assertEqual(self.query(head).returncode, 4)
            self.assertEqual(self.adjudicate(head=head).returncode, 4)
        for digest in ("not-a-digest", self.digest[:40], self.digest + "\n"):
            self.assertEqual(self.adjudicate(digest=digest).returncode, 4)
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
