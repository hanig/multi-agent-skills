#!/usr/bin/env python3
"""Tests for review.py — the gate's own integrity.

A gate that can produce a false pass is worse than no gate, so these focus on
the ways round 4 showed it could: a non-answer filling a quorum slot, truncated
input passing, enum case defeating the refutation rule, and a config error
reading as a review failure.

Offline: no API calls.

    python3 tests/test_review.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "hanig-review-gate" / "scripts" / "review.py"

spec = importlib.util.spec_from_file_location("review", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class TestVerdictSchema(unittest.TestCase):
    """sol: any parseable object counted as a completed review, so {} could
    fill a quorum slot and the gate passed on one real opinion."""

    def test_empty_object_is_not_a_review(self):
        self.assertIsNotNone(review.verdict_schema_error({}))

    def test_missing_verdict_is_rejected(self):
        self.assertIsNotNone(review.verdict_schema_error({"findings": []}))

    def test_bogus_verdict_value_is_rejected(self):
        self.assertIsNotNone(review.verdict_schema_error({"verdict": "maybe"}))

    def test_wrong_types_rejected(self):
        self.assertIsNotNone(
            review.verdict_schema_error({"verdict": "upheld", "findings": {}}))
        self.assertIsNotNone(
            review.verdict_schema_error({"verdict": "upheld", "claims": "x"}))

    def test_valid_verdict_accepted(self):
        self.assertIsNone(review.verdict_schema_error(
            {"verdict": "upheld", "findings": [], "claims": []}))

    def test_case_insensitive_verdict(self):
        self.assertIsNone(review.verdict_schema_error(
            {"verdict": "Refuted", "findings": [], "claims": []}))

    def test_verdict_word_alone_is_not_a_review(self):
        """gpt-5.6-sol: two responses carrying only {"verdict":"upheld"} met
        quorum and produced REVIEW_PASS without assessing anything."""
        e = review.verdict_schema_error({"verdict": "upheld"})
        self.assertIsNotNone(e)
        self.assertIn("required", e)


class TestEnumCase(unittest.TestCase):
    """kimi-k3: enums were matched case-sensitively, so a reviewer that wrote
    'Refuted' or 'Major' had its finding silently dropped and the gate passed."""

    def test_capitalised_severity_still_confirms(self):
        f = {"severity": "Major", "confidence": "High",
             "failure_scenario": "x happens"}
        self.assertTrue(review.is_confirmed(f))

    def test_uppercase_confidence_still_confirms(self):
        f = {"severity": "CRITICAL", "confidence": "MEDIUM",
             "failure_scenario": "x"}
        self.assertTrue(review.is_confirmed(f))

    def test_no_failure_scenario_is_not_confirmed(self):
        self.assertFalse(review.is_confirmed(
            {"severity": "critical", "confidence": "high",
             "failure_scenario": "  "}))

    def test_minor_is_not_confirmed(self):
        self.assertFalse(review.is_confirmed(
            {"severity": "minor", "confidence": "high",
             "failure_scenario": "x"}))

    def test_norm_handles_none(self):
        self.assertEqual(review.norm(None), "")


class TestRound6Redaction(unittest.TestCase):
    """Four reviewers found four routes to the same leak: redaction ran on the
    serialized report, so JSON escaping defeated it."""

    def setUp(self):
        self.old = os.environ.get("OPENAI_API_KEY")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self.old

    def test_key_with_json_metacharacter_is_scrubbed(self):
        """kimi-k2.7-code: a key containing a quote is escaped by json.dumps
        and no longer matched the raw-key pattern."""
        os.environ["OPENAI_API_KEY"] = 'sk-"secret-value-here'
        finding = {"summary": 'leaked sk-"secret-value-here here'}
        scrubbed = review.deep_redact(finding)
        self.assertNotIn("secret-value-here", json.dumps(scrubbed))

    def test_short_key_is_scrubbed(self):
        """luna: the length guard skipped keys of 8 chars or fewer."""
        os.environ["OPENAI_API_KEY"] = "shortkey"
        self.assertNotIn("shortkey", review.redact("quoting shortkey here"))

    def test_deep_redact_walks_nested_structures(self):
        os.environ["OPENAI_API_KEY"] = "sk-nested-secret-1234"
        obj = {"a": [{"b": "sk-nested-secret-1234"}], "c": ("x",)}
        self.assertNotIn("sk-nested-secret-1234",
                         json.dumps(review.deep_redact(obj), default=str))


class TestClaimAssessment(unittest.TestCase):
    """gpt-5.6-sol: with claims asserted, an EMPTY claims array satisfied the
    schema, so two non-answers met quorum and the gate passed."""

    def test_empty_claims_rejected_when_claims_asserted(self):
        v = {"verdict": "upheld", "findings": [], "claims": []}
        self.assertIsNone(review.verdict_schema_error(v, require_claims=0))
        self.assertIsNotNone(review.verdict_schema_error(v, require_claims=2))

    def test_partial_claim_assessment_rejected(self):
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim": "a", "status": "supported"}]}
        self.assertIsNotNone(review.verdict_schema_error(v, require_claims=3))

    def test_full_claim_assessment_accepted(self):
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim": "a", "status": "supported", "why": "the code at line 40 does exactly this"},
                        {"claim": "b", "status": "unverifiable", "why": "the code at line 40 does exactly this"}]}
        self.assertIsNone(review.verdict_schema_error(v, require_claims=2))


class TestRound7Regressions(unittest.TestCase):
    """Round 7, caught by the `fast` tier alone for about three cents."""

    def setUp(self):
        self.old = os.environ.get("OPENAI_API_KEY")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self.old

    def test_dict_keys_are_redacted(self):
        """luna: deep_redact scrubbed values but left dictionary keys alone."""
        os.environ["OPENAI_API_KEY"] = "sk-key-as-a-dict-key-9999"
        out = review.deep_redact({"sk-key-as-a-dict-key-9999": "x"})
        self.assertNotIn("sk-key-as-a-dict-key-9999", json.dumps(out))

    def test_json_escaped_key_is_redacted(self):
        """deepseek-v4-pro: a key containing a quote is escaped by json.dumps,
        so a literal replace on the serialized report missed it."""
        os.environ["OPENAI_API_KEY"] = 'sk-"quoted"-secret-1234'
        payload = json.dumps({"summary": 'sk-"quoted"-secret-1234'})
        self.assertNotIn("quoted", review.redact(payload))

    def test_unassessed_claim_is_detected(self):
        """Both: the check counted entries without checking correspondence, so
        two copies of claim A satisfied two asserted claims."""
        asserted = ["The parser never raises on malformed input",
                    "Keys are redacted from all output"]
        returned = [{"claim": "The parser never raises on malformed input",
                     "status": "supported"},
                    {"claim": "The parser never raises on malformed input",
                     "status": "supported"}]
        missing = review.unassessed_claims(returned, asserted)
        self.assertEqual(len(missing), 1)
        self.assertIn("redacted", missing[0])

    def test_unrelated_claims_do_not_satisfy(self):
        asserted = ["Budget exhaustion is never reported as convergence"]
        returned = [{"claim": "The sky is blue", "status": "supported"}]
        self.assertEqual(len(review.unassessed_claims(returned, asserted)), 1)

    def test_claim_index_with_unrelated_text_does_not_satisfy(self):
        """CONVENTION REVERSED. Round 7 accepted a claim_index on its own so
        that reworded text still matched. luna showed that is exploitable: a
        reviewer can index any claim and write about something else entirely,
        and the gate counted it as assessed. The index must now agree with the
        text."""
        asserted = ["a fairly long claim about redaction behaviour",
                    "another distinct claim about budget handling"]
        returned = [{"claim_index": 0, "claim": "reworded entirely",
                     "status": "supported",
                     "why": "the code at line 40 does exactly this"},
                    {"claim_index": 1, "claim": "also reworded",
                     "status": "refuted",
                     "why": "the code at line 40 does exactly this"}]
        self.assertEqual(len(review.unassessed_claims(returned, asserted)), 2)

    def test_claim_index_with_agreeing_text_satisfies(self):
        """Rewording is still fine when the text is recognisably the claim."""
        asserted = ["a fairly long claim about redaction behaviour",
                    "another distinct claim about budget handling"]
        returned = [{"claim_index": 0,
                     "claim": "claim regarding redaction behaviour",
                     "status": "supported",
                     "why": "the code at line 40 does exactly this"},
                    {"claim_index": 1,
                     "claim": "claim regarding budget handling",
                     "status": "refuted",
                     "why": "the code at line 40 does exactly this"}]
        self.assertEqual(review.unassessed_claims(returned, asserted), [])

    def test_statusless_claim_stub_is_rejected(self):
        """luna: [{"claim_index":0}] counted as an assessment."""
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim_index": 0}, {"claim_index": 1}]}
        e = review.verdict_schema_error(v, require_claims=2)
        self.assertIsNotNone(e)
        self.assertIn("status", e)

    def test_parse_error_text_is_redacted(self):
        """luna, CRITICAL: a provider echoing the key into a non-JSON response
        put it into the stored error string, which is printed."""
        old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-leaky-secret-abcdefghij"
        try:
            _, err = review.parse_verdict("sk-leaky-secret-abcdefghij oops")
            self.assertNotIn("sk-leaky-secret-abcdefghij", review.redact(err))
        finally:
            if old is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old

    def test_reworded_claim_still_matches(self):
        asserted = ["traincontract never reports CONVERGED when the budget "
                    "was exhausted without meeting the criterion"]
        returned = [{"claim": "CONVERGED is not reported when the budget was "
                              "exhausted without the criterion being met",
                     "status": "supported"}]
        self.assertEqual(review.unassessed_claims(returned, asserted), [])

    def test_claim_matching_is_one_to_one(self):
        """luna, MAJOR: two identical entries for claim A satisfied both A and
        B, so B went unassessed while the count looked correct."""
        asserted = ["API key values are redacted from all printed output",
                    "API key values are redacted from provider error strings"]
        dup = {"claim": "API key values are redacted from all printed output",
               "status": "supported"}
        missing = review.unassessed_claims([dict(dup), dict(dup)], asserted)
        self.assertEqual(len(missing), 1, f"expected one unassessed: {missing}")

    def test_distinct_entries_satisfy_distinct_claims(self):
        asserted = ["API key values are redacted from printed output",
                    "budget exhaustion is never reported as convergence"]
        returned = [{"claim": "API key values are redacted from printed output",
                     "status": "supported"},
                    {"claim": "budget exhaustion is never reported as convergence",
                     "status": "supported"}]
        self.assertEqual(review.unassessed_claims(returned, asserted), [])

    def test_duplicate_indices_do_not_double_count(self):
        """Both entries also fail the text-agreement rule added later ("x" has
        no distinctive words), so both claims come back unassessed -- stricter
        than the original expectation of one, and correct."""
        asserted = ["first distinctive claim regarding redaction",
                    "second distinctive claim regarding budgets"]
        returned = [{"claim_index": 0, "claim": "x", "status": "supported"},
                    {"claim_index": 0, "claim": "x", "status": "supported"}]
        self.assertEqual(len(review.unassessed_claims(returned, asserted)), 2)

    def test_duplicate_indices_with_agreeing_text_cover_only_one(self):
        asserted = ["first distinctive claim regarding redaction",
                    "second distinctive claim regarding budgets"]
        entry = {"claim_index": 0,
                 "claim": "first distinctive claim regarding redaction",
                 "status": "supported"}
        self.assertEqual(
            len(review.unassessed_claims([dict(entry), dict(entry)], asserted)),
            1)

    def test_schema_rejects_verdict_missing_a_claim(self):
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim": "first claim about redaction",
                         "status": "supported", "why": "the code at line 40 does exactly this"}]}
        e = review.verdict_schema_error(
            v, require_claims=1,
            asserted=["first claim about redaction",
                      "second claim about budget exhaustion"])
        self.assertIsNotNone(e)
        self.assertIn("did not assess", e)


class TestProviderRobustness(unittest.TestCase):
    """gpt-5.6-sol: a provider returning HTTP 200 with {"choices": null} raised
    TypeError out of run_one, through ex.map, and killed the whole gate."""

    def test_run_one_never_raises(self):
        def exploding(rev, prompt, timeout, deadline=None):
            raise TypeError("'NoneType' object is not subscriptable")
        review.PROVIDERS["_boom"] = exploding
        try:
            r = review.run_one({"name": "x", "provider": "_boom", "model": "m"},
                               "prompt", 5)
            self.assertFalse(r["ok"])
            self.assertIn("crashed", r["error"])
        finally:
            review.PROVIDERS.pop("_boom", None)

    def test_null_choices_is_an_error_not_a_crash(self):
        import os as _os
        _os.environ.setdefault("OPENROUTER_API_KEY", "x" * 40)
        orig = review._post
        review._post = lambda *a, **k: ({"choices": None}, None)
        try:
            out, err = review.call_openrouter(
                {"name": "k", "model": "m"}, "p", 5)
            self.assertIsNone(out)
            self.assertIn("unexpected response shape", err)
        finally:
            review._post = orig


class TestRedaction(unittest.TestCase):
    """sol refuted the claim that keys never reach output: http.client embeds
    the Authorization header in ValueError when a key contains a newline."""

    def setUp(self):
        self.old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-supersecret-value-1234567890"

    def tearDown(self):
        if self.old is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self.old

    def test_key_is_scrubbed_from_error_text(self):
        msg = "ValueError: Invalid header b'Bearer sk-supersecret-value-1234567890'"
        out = review.redact(msg)
        self.assertNotIn("sk-supersecret-value-1234567890", out)
        self.assertIn("redacted", out)

    def test_redact_handles_empty(self):
        self.assertEqual(review.redact(""), "")
        self.assertIsNone(review.redact(None))

    def test_short_values_are_not_used_as_patterns(self):
        os.environ["OPENAI_API_KEY"] = "abc"
        self.assertEqual(review.redact("abcdef"), "abcdef")

    def test_reviewer_authored_text_is_redacted(self):
        """gpt-5.6-sol: a reviewer quoting a key out of the reviewed source
        leaked it, because only error strings were scrubbed."""
        finding = json.dumps({"summary": "key is sk-supersecret-value-1234567890"})
        self.assertNotIn("sk-supersecret-value-1234567890",
                         review.redact(finding))


class TestParsing(unittest.TestCase):
    def test_truncated_json_is_reported_as_truncation(self):
        v, err = review.parse_verdict('{"verdict": "refuted", "findi')
        self.assertIsNone(v)
        self.assertIn("truncated", err)

    def test_fenced_json_is_recovered(self):
        v, err = review.parse_verdict(
            '```json\n{"verdict": "upheld", "findings": []}\n```')
        self.assertIsNone(err)
        self.assertEqual(v["verdict"], "upheld")

    def test_braces_inside_strings_do_not_break_parsing(self):
        v, err = review.parse_verdict(
            '{"verdict": "upheld", "notes": "a } brace { here"}')
        self.assertIsNone(err, err)
        self.assertEqual(v["notes"], "a } brace { here")

    def test_empty_response(self):
        v, err = review.parse_verdict("")
        self.assertIsNone(v)
        self.assertIn("empty", err)


class TestConfigErrors(unittest.TestCase):
    """deepseek-v4-pro, CRITICAL: a missing reviewers.json exited 1, which this
    scheme means REVIEW_FAIL — CI would block a merge for a review that never
    ran."""

    def run_with_config(self, content):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "scripts").mkdir()
        script = tmp / "scripts" / "review.py"
        script.write_text(SCRIPT.read_text())
        if content is not None:
            (tmp / "reviewers.json").write_text(content)
        return subprocess.run([sys.executable, str(script), "--list"],
                              capture_output=True, text=True)

    def test_missing_config_is_review_error_not_review_fail(self):
        r = self.run_with_config(None)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("REVIEW_ERROR", r.stderr)

    def test_malformed_config_is_review_error(self):
        r = self.run_with_config("{ not json")
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_empty_reviewer_list_is_review_error(self):
        r = self.run_with_config('{"reviewers": []}')
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_reviewer_missing_fields_is_review_error(self):
        r = self.run_with_config('{"reviewers": [{"name": "x"}]}')
        self.assertEqual(r.returncode, 4, r.stderr)


class TestUsageErrors(unittest.TestCase):
    """gpt-5.6-sol: argparse exits 2, which this scheme reserves for
    REVIEW_UNAVAILABLE, so a typo read as "nothing was reviewed"."""

    def test_bad_argument_is_review_error_not_unavailable(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--quorum", "nope"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("REVIEW_ERROR", r.stderr)


class TestRound10Regressions(unittest.TestCase):
    def test_zero_quorum_is_rejected(self):
        """luna: --quorum 0 made the quorum test vacuously true, so a single
        reviewer could carry a REVIEW_PASS."""
        r = subprocess.run([sys.executable, str(SCRIPT), "--quorum", "0",
                            "--diff"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("at least 1", r.stderr)

    def test_stopword_only_claim_is_unassessed_not_satisfied(self):
        """luna: a claim of pure filler normalised to an empty key and was
        silently counted as assessed."""
        missing = review.unassessed_claims(
            [{"claim": "something else entirely", "status": "supported"}],
            ["This must be done"])
        self.assertEqual(len(missing), 1)
        self.assertIn("claim_index", missing[0])

    def test_stopword_only_claim_satisfied_by_explicit_index(self):
        missing = review.unassessed_claims(
            [{"claim_index": 0, "claim": "anything", "status": "supported"}],
            ["This must be done"])
        self.assertEqual(missing, [])


class TestMalformedRetry(unittest.TestCase):
    """Observed in round 11: luna emitted invalid JSON and was dropped, putting
    the run below quorum. A formatting slip must not weaken the gate."""

    def test_malformed_first_reply_is_retried_then_succeeds(self):
        calls = []
        good = json.dumps({"verdict": "upheld", "findings": [], "claims": []})

        def flaky(rev, prompt, timeout, deadline=None):
            calls.append(prompt)
            if len(calls) == 1:
                return {"text": "{bad json,,,", "in_tokens": 1,
                        "out_tokens": 1}, None
            return {"text": good, "in_tokens": 1, "out_tokens": 1}, None

        review.PROVIDERS["_flaky"] = flaky
        try:
            r = review.run_one({"name": "f", "provider": "_flaky", "model": "m"},
                               "review this", 5)
            self.assertTrue(r["ok"], r.get("error"))
            self.assertTrue(r["retried"])
            self.assertEqual(len(calls), 2)
            self.assertIn("could not be used", calls[1])
        finally:
            review.PROVIDERS.pop("_flaky", None)

    def test_twice_malformed_gives_up_and_is_not_counted(self):
        def broken(rev, prompt, timeout, deadline=None):
            return {"text": "still not json", "in_tokens": 1,
                    "out_tokens": 1}, None

        review.PROVIDERS["_broken"] = broken
        try:
            r = review.run_one({"name": "b", "provider": "_broken", "model": "m"},
                               "p", 5)
            self.assertFalse(r["ok"])
            self.assertIn("after a retry", r["error"])
        finally:
            review.PROVIDERS.pop("_broken", None)

    def test_transport_error_is_not_retried_as_malformed(self):
        def dead(rev, prompt, timeout, deadline=None):
            return None, "HTTP 401: bad key"

        review.PROVIDERS["_dead"] = dead
        try:
            r = review.run_one({"name": "d", "provider": "_dead", "model": "m"},
                               "p", 5)
            self.assertFalse(r["ok"])
            self.assertIn("401", r["error"])
        finally:
            review.PROVIDERS.pop("_dead", None)


class TestRound12Regressions(unittest.TestCase):
    def test_near_identical_claims_are_not_covered_by_duplicates(self):
        """luna: two entries for claim A covered both A and a near-identical B,
        because matching took the first entry over a threshold per claim."""
        asserted = ["review.py reports upheld verdict",
                    "review.py reports upheld output"]
        dup = {"claim": "review.py reports upheld verdict",
               "status": "supported"}
        missing = review.unassessed_claims([dict(dup), dict(dup)], asserted)
        self.assertEqual(len(missing), 1, f"expected one unassessed: {missing}")

    def test_best_match_wins_between_similar_claims(self):
        asserted = ["review.py reports upheld verdict",
                    "review.py reports upheld output"]
        returned = [{"claim": "review.py reports upheld output",
                     "status": "supported"},
                    {"claim": "review.py reports upheld verdict",
                     "status": "supported"}]
        self.assertEqual(review.unassessed_claims(returned, asserted), [])

    def test_major_finding_without_a_scenario_is_a_schema_error(self):
        """luna: such a finding was accepted, then silently dropped by
        is_confirmed, so the gate could pass over a reported defect."""
        v = {"verdict": "refuted",
             "findings": [{"severity": "major", "confidence": "high",
                           "summary": "defect"}],
             "claims": []}
        e = review.verdict_schema_error(v)
        self.assertIsNotNone(e)
        self.assertIn("failure_scenario", e)

    def test_bad_finding_severity_is_a_schema_error(self):
        v = {"verdict": "refuted",
             "findings": [{"severity": "catastrophic", "confidence": "high",
                           "failure_scenario": "x"}],
             "claims": []}
        self.assertIsNotNone(review.verdict_schema_error(v))

    def test_minor_finding_without_a_scenario_is_allowed(self):
        v = {"verdict": "upheld",
             "findings": [{"severity": "minor", "confidence": "low",
                           "summary": "nit"}],
             "claims": []}
        self.assertIsNone(review.verdict_schema_error(v))


class TestRound13Regressions(unittest.TestCase):
    def test_duplicate_indexed_entries_cover_only_one_claim(self):
        """luna + deepseek: claim_index bypassed resemblance, so two identical
        entries indexed 0 and 1 marked both claims assessed."""
        asserted = ["redaction covers provider error strings",
                    "budget exhaustion is never called convergence"]
        dup = {"claim": "redaction covers provider error strings",
               "status": "supported"}
        returned = [dict(dup, claim_index=0), dict(dup, claim_index=1)]
        missing = review.unassessed_claims(returned, asserted)
        self.assertEqual(len(missing), 1, f"expected one unassessed: {missing}")

    def test_null_failure_scenario_is_rejected(self):
        """luna: str(None).strip() is "None" -- truthy -- so a null scenario
        was accepted and then counted as a real finding."""
        for bad in (None, False, [], {}, 0):
            with self.subTest(scenario=bad):
                v = {"verdict": "refuted",
                     "findings": [{"severity": "major", "confidence": "high",
                                   "summary": "d", "failure_scenario": bad}],
                     "claims": []}
                e = review.verdict_schema_error(v)
                self.assertIsNotNone(e, f"{bad!r} was accepted")

    def test_escalate_keeps_the_full_roster(self):
        """luna, the important one: main pre-filtered reviewers to the profile
        before escalate saw them, so the ladder could never reach the deep
        tier. Verified by inspecting the source, since running it costs money."""
        src = SCRIPT.read_text()
        self.assertIn("not args.only and not args.escalate", src,
                      "escalate must not receive a profile-filtered roster")

    def test_ladder_covers_every_tier(self):
        cfg = json.loads((SCRIPT.parent.parent / "reviewers.json").read_text())
        tiers = set()
        for r in cfg["reviewers"]:
            tiers.update(r.get("profiles") or [])
        # `plan` is deliberately NOT a ladder tier: a plan review is two
        # contrasting models and is never escalated, so it must not appear in
        # the cheapest-first cascade. Exempted by name rather than by
        # loosening the check, which is what keeps a typo'd tier detectable.
        tiers -= {"plan"}
        self.assertTrue(tiers.issubset(set(review.LADDER)),
                        f"reviewers.json uses tiers outside LADDER: "
                        f"{tiers - set(review.LADDER)}")
        for tier in review.LADDER:
            self.assertTrue(
                any(tier in (r.get("profiles") or []) for r in cfg["reviewers"]),
                f"no reviewer is in tier {tier}")


class TestJointReviewRegressions(unittest.TestCase):
    def test_non_string_provider_is_a_config_error(self):
        """luna: provider:["openai"] reached env.get() as an unhashable list and
        killed the gate before any REVIEW_* verdict."""
        import tempfile as _tf
        for bad in ('["openai"]', "5", "null", '{"a":1}'):
            with self.subTest(provider=bad):
                tmp = Path(_tf.mkdtemp())
                (tmp / "scripts").mkdir()
                (tmp / "scripts" / "review.py").write_text(SCRIPT.read_text())
                (tmp / "reviewers.json").write_text(
                    '{"reviewers":[{"name":"x","provider":' + bad +
                    ',"model":"m"}]}')
                r = subprocess.run(
                    [sys.executable, str(tmp / "scripts" / "review.py"),
                     "--list"], capture_output=True, text=True)
                self.assertEqual(r.returncode, 4, r.stderr)
                self.assertIn("REVIEW_ERROR", r.stderr)

    def test_non_list_profiles_is_a_config_error(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp())
        (tmp / "scripts").mkdir()
        (tmp / "scripts" / "review.py").write_text(SCRIPT.read_text())
        (tmp / "reviewers.json").write_text(
            '{"reviewers":[{"name":"x","provider":"openai","model":"m",'
            '"profiles":"fast"}]}')
        r = subprocess.run([sys.executable, str(tmp / "scripts" / "review.py"),
                            "--list"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 4, r.stderr)


class TestRefutedVerdict(unittest.TestCase):
    """luna: a reviewer's top-level 'refuted' verdict was ignored entirely, so
    two reviewers could both reject the work with empty findings and the gate
    still emitted REVIEW_PASS. A false pass in the gate itself."""

    def test_refuted_verdict_is_schema_valid_but_must_fail_the_gate(self):
        v = {"verdict": "refuted", "findings": [],
             "claims": [{"claim_index": 0, "claim": "x",
                         "status": "supported", "why": "the code at line 40 does exactly this"}]}
        self.assertIsNone(review.verdict_schema_error(v, 1, ["x"]),
                          "a refuted verdict is well-formed")
        self.assertEqual(review.norm(v["verdict"]), "refuted")

    def test_refuted_verdict_with_nothing_confirmed_is_partial_not_pass(self):
        """Three-way, after deepseek showed my previous fix was too blunt.
        Ignoring a refuted verdict allowed a false pass (luna's finding);
        failing on it alone made failure permanent, because both reviewers set
        that field on essentially every round while their confirmed findings
        vary. It is REVIEW_PARTIAL: a rejection a human must read."""
        # Was a source grep for "elif rejecting:"; that asserted the shape of
        # the code rather than its behaviour, and broke the moment the logic
        # moved into decide_state. Assert the verdict instead.
        base = dict(n_completed=2, n_failed=0, confirmed=[], refuted_claims=[],
                    rejecting=[], truncated=False, quorum=2)
        self.assertEqual(review.decide_state(**base), "REVIEW_PASS")
        self.assertEqual(
            review.decide_state(**{**base, "rejecting": ["deepseek-v4-pro"]}),
            "REVIEW_PARTIAL")
        # A rejection alone is not FAIL: both reviewers set that field on
        # essentially every round while their confirmed findings vary.
        self.assertNotEqual(
            review.decide_state(**{**base, "rejecting": ["deepseek-v4-pro"]}),
            "REVIEW_FAIL")
        # PARTIAL is non-zero, so it can never be mistaken for a pass.
        self.assertNotEqual(review.STATES["REVIEW_PARTIAL"], 0)

    def test_confirmed_finding_still_fails_regardless_of_verdict(self):
        f = {"severity": "major", "confidence": "high",
             "failure_scenario": "concrete"}
        self.assertTrue(review.is_confirmed(f))

    def test_upheld_verdict_does_not_reject(self):
        self.assertEqual(review.norm("Upheld"), "upheld")
        self.assertNotEqual(review.norm("Upheld"), "refuted")


class TestBoundedReads(unittest.TestCase):
    """luna: gather() read --file arguments with a plain read_text(), so a FIFO
    there blocked the gate forever and it never printed a verdict."""

    def test_fifo_file_argument_is_a_config_error_not_a_hang(self):
        import tempfile as _tf, os as _os, signal
        tmp = Path(_tf.mkdtemp())
        fifo = tmp / "src.fifo"
        _os.mkfifo(fifo)
        pr = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--file", str(fifo), "--list"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = pr.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pr.kill()
            self.fail("review.py hung on a FIFO --file argument")
        self.assertIsNotNone(pr.returncode)

    def test_bounded_reader_rejects_non_regular_files(self):
        import tempfile as _tf, os as _os
        tmp = Path(_tf.mkdtemp())
        fifo = tmp / "x.fifo"
        _os.mkfifo(fifo)
        text, err = review.read_text_bounded(fifo)
        self.assertTrue(err)
        self.assertIn("not a regular file", err)

    def test_bounded_reader_reads_a_real_file(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp())
        f = tmp / "a.py"
        f.write_text("print(1)\n")
        text, err = review.read_text_bounded(f)
        self.assertIsNone(err)
        self.assertIn("print(1)", text)


class TestConfigFifo(unittest.TestCase):
    """luna, fourth instance of the same class: I hardened predicate, attempt,
    metrics and --file reads but not the config/contract files themselves."""

    def test_fifo_reviewers_config_does_not_hang(self):
        import tempfile as _tf, os as _os
        tmp = Path(_tf.mkdtemp())
        (tmp / "scripts").mkdir()
        (tmp / "scripts" / "review.py").write_text(SCRIPT.read_text())
        _os.mkfifo(tmp / "reviewers.json")
        pr = subprocess.Popen(
            [sys.executable, str(tmp / "scripts" / "review.py"), "--list"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = pr.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pr.kill()
            self.fail("review.py hung reading a FIFO reviewers.json")
        self.assertEqual(pr.returncode, 4, err)


class TestGitDecoding(unittest.TestCase):
    """luna: contract.py's run() has used errors="replace" since round 4, but
    review.py's git_out still used strict decoding, so one non-UTF-8 byte in a
    tracked file crashed the gate before it printed anything."""

    def test_git_out_never_raises_on_undecodable_output(self):
        import tempfile as _tf, subprocess as _sp
        tmp = Path(_tf.mkdtemp())
        _sp.run(["git", "init", "-q", str(tmp)], capture_output=True)
        f = tmp / "bin.txt"
        f.write_bytes(b"ok\n")
        _sp.run(["git", "-C", str(tmp), "add", "-A"], capture_output=True)
        _sp.run(["git", "-C", str(tmp), "-c", "user.email=t@t",
                 "-c", "user.name=t", "commit", "-qm", "x"],
                capture_output=True)
        f.write_bytes(b"\xff\xfe not utf8\n")
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            out = review.git_out("diff", "HEAD")   # must not raise
            self.assertIsInstance(out, str)
        finally:
            os.chdir(cwd)

    def test_git_out_returns_empty_on_failure(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp())
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            self.assertEqual(review.git_out("diff", "HEAD"), "")
        finally:
            os.chdir(cwd)


class TestGitTimeout(unittest.TestCase):
    def test_git_out_has_a_timeout(self):
        """luna: a repository can set diff.external to an arbitrary command, so
        git_out without a timeout could hang the gate indefinitely."""
        src = SCRIPT.read_text()
        self.assertIn("timeout=120", src)
        self.assertIn("TimeoutExpired", src)


class TestTotalWatchdog(unittest.TestCase):
    def test_total_watchdog_exists(self):
        """luna: urlopen's timeout is per-read, so a response trickling one byte
        below the idle timeout never trips it and the executor waits forever."""
        src = SCRIPT.read_text()
        self.assertIn("def arm_watchdog", src)
        self.assertIn("SIGALRM", src)
        self.assertIn("--watchdog", src)

    def test_watchdog_flag_is_accepted(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--watchdog", "60",
                            "--list"], capture_output=True, text=True)
        self.assertIn(r.returncode, (0, 2), r.stderr)


class TestEmptyClaimEntry(unittest.TestCase):
    def test_index_and_status_alone_assess_nothing(self):
        """luna: an entry with claim_index and status but no text was accepted
        and counted as covering the claim, so quorum passed without any
        reviewer actually assessing it."""
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim_index": 0, "status": "supported"}]}
        e = review.verdict_schema_error(v, 1, ["some asserted claim"])
        self.assertIsNotNone(e)
        self.assertIn("substantive", e)

    def test_index_with_a_why_but_no_text_is_rejected(self):
        """luna: unassessed_claims honoured the index without requiring claim
        text, so nothing showed WHICH claim had been assessed."""
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim_index": 0, "status": "supported",
                         "why": "the code does exactly this at line 40"}]}
        e = review.verdict_schema_error(v, 1, ["some asserted claim"])
        self.assertIsNotNone(e)
        self.assertIn("claim TEXT", e)

    def test_index_with_text_and_why_is_accepted(self):
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim_index": 0, "claim": "some asserted claim",
                         "status": "supported",
                         "why": "the code does exactly this at line 40"}]}
        self.assertIsNone(review.verdict_schema_error(
            v, 1, ["some asserted claim"]))

    def test_filler_why_is_rejected(self):
        """luna: fifteen x's passed the length check."""
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim_index": 0, "claim": "some asserted claim",
                         "status": "supported", "why": "xxxxxxxxxxxxxxx"}]}
        self.assertIsNotNone(review.verdict_schema_error(
            v, 1, ["some asserted claim"]))

    def test_text_without_an_index_is_accepted(self):
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim": "some asserted claim",
                         "status": "supported", "why": "the code at line 40 does exactly this"}]}
        self.assertIsNone(review.verdict_schema_error(
            v, 1, ["some asserted claim"]))


class TestClaimSubstance(unittest.TestCase):
    """Third iteration on this validator. luna: text + status alone is still
    content-free -- a reviewer can echo the claim back and mark it supported
    without examining anything."""

    def test_echoed_claim_with_no_why_is_rejected(self):
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim_index": 0, "claim": "the asserted claim text",
                         "status": "supported"}]}
        e = review.verdict_schema_error(v, 1, ["the asserted claim text"])
        self.assertIsNotNone(e)
        self.assertIn("why", e)

    def test_trivial_why_is_rejected(self):
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim_index": 0, "claim": "x", "status": "supported",
                         "why": "yes"}]}
        self.assertIsNotNone(review.verdict_schema_error(v, 1, ["x claim"]))

    def test_substantive_why_is_accepted(self):
        v = {"verdict": "upheld", "findings": [],
             "claims": [{"claim_index": 0, "claim": "the asserted claim text",
                         "status": "supported",
                         "why": "cmd_check line 612 enforces exactly this"}]}
        self.assertIsNone(
            review.verdict_schema_error(v, 1, ["the asserted claim text"]))


class TestReviewerDeadline(unittest.TestCase):
    """A round that normally takes 3 minutes ran 73: the per-attempt timeout was
    multiplied by 3 transport retries and 2 schema attempts, giving no
    per-reviewer bound at all."""

    def test_reviewer_has_a_total_budget(self):
        src = SCRIPT.read_text()
        self.assertIn("deadline = t0 + max(30, timeout * 2)", src)
        self.assertIn("deadline=deadline", src)
        self.assertIn("budget of", src)

    def test_post_respects_a_deadline(self):
        import time as _t
        calls = []

        def slow_open(*a, **k):
            calls.append(1)
            raise OSError("simulated transport failure")

        orig = review.urllib.request.urlopen
        review.urllib.request.urlopen = slow_open
        try:
            out, err = review._post("https://example.invalid", {}, {}, 5,
                                    retries=3, deadline=_t.time() - 1)
            self.assertIsNone(out)
            self.assertIn("deadline", err)
            self.assertEqual(calls, [], "no request should be made past the deadline")
        finally:
            review.urllib.request.urlopen = orig


class TestFailClosed(unittest.TestCase):
    def test_no_keys_means_unavailable_not_pass(self):
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENROUTER_API_KEY", None)
        r = subprocess.run([sys.executable, str(SCRIPT), "--list"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("not a pass", r.stdout.lower())

    def test_states_are_distinct(self):
        self.assertEqual(len(set(review.STATES.values())), len(review.STATES))
        self.assertEqual(review.STATES["REVIEW_PASS"], 0)
        self.assertNotEqual(review.STATES["REVIEW_UNAVAILABLE"], 0)
        self.assertNotEqual(review.STATES["REVIEW_PARTIAL"], 0)


class TestPortability(unittest.TestCase):
    def test_stdlib_only(self):
        allowed = {"argparse", "concurrent", "json", "os", "re", "signal", "stat",
                   "subprocess", "sys", "time", "urllib", "pathlib"}
        for line in SCRIPT.read_text().splitlines():
            s = line.strip()
            if s.startswith("import ") and not s.startswith("import ("):
                self.assertIn(s.split()[1].split(".")[0], allowed, s)
            elif s.startswith("from ") and " import " in s:
                self.assertIn(s.split()[1].split(".")[0], allowed, s)


class TestFailedReviewerIsNotAPass(unittest.TestCase):
    """luna: the final state checked only whether len(completed) reached quorum,
    so a reviewer that errored, timed out, or returned unparseable output left
    `failed` non-empty while the gate still reported REVIEW_PASS. The gate's own
    premise is that absent evidence is not a pass."""

    BASE = dict(n_completed=2, n_failed=0, confirmed=[], refuted_claims=[],
                rejecting=[], truncated=False, quorum=2)

    def test_quorum_met_with_a_failed_reviewer_is_not_a_pass(self):
        st = review.decide_state(**{**self.BASE, "n_failed": 1})
        self.assertEqual(st, "REVIEW_PARTIAL")
        self.assertNotEqual(review.STATES[st], 0)

    def test_extra_completed_reviewers_do_not_absorb_a_failure(self):
        st = review.decide_state(**{**self.BASE, "n_completed": 5,
                                    "n_failed": 1})
        self.assertEqual(st, "REVIEW_PARTIAL")

    def test_clean_run_still_passes(self):
        self.assertEqual(review.decide_state(**self.BASE), "REVIEW_PASS")

    def test_a_confirmed_finding_outranks_a_failed_reviewer(self):
        # FAIL is actionable; PARTIAL is not. A real defect must not be
        # softened into "a human should look" by an unrelated reviewer error.
        st = review.decide_state(**{**self.BASE, "n_failed": 1,
                                    "confirmed": [{"severity": "major"}]})
        self.assertEqual(st, "REVIEW_FAIL")

    def test_no_completed_reviewer_is_unavailable_not_partial(self):
        st = review.decide_state(**{**self.BASE, "n_completed": 0,
                                    "n_failed": 2})
        self.assertEqual(st, "REVIEW_UNAVAILABLE")
        self.assertNotEqual(review.STATES[st], 0)

    def test_every_non_pass_state_is_non_zero(self):
        for st in ("REVIEW_FAIL", "REVIEW_PARTIAL", "REVIEW_UNAVAILABLE",
                   "REVIEW_ERROR"):
            self.assertNotEqual(review.STATES[st], 0, st)


class TestScopeDiscipline(unittest.TestCase):
    """Rounds stopped converging because the gate was pointed at an adversary
    the tool never claimed to defend against: SKILL.md says contract.json is
    trusted input, yet six of fourteen recent findings required hand-editing
    it. A finding outside the threat model is real but does not gate."""

    def f(self, **over):
        base = {"severity": "critical", "confidence": "high",
                "failure_scenario": "edit contract.json -> false pass"}
        base.update(over)
        return base

    def test_in_scope_defaults_to_true_when_unstated(self):
        """Fail closed: an unstated scope is not evidence of irrelevance, and
        reviewers that ignore the new field must still be able to gate."""
        self.assertTrue(review.is_confirmed(self.f()))

    def test_explicit_false_takes_a_finding_out_of_the_verdict(self):
        self.assertFalse(review.is_confirmed(self.f(in_scope=False)))

    def test_explicit_true_still_gates(self):
        self.assertTrue(review.is_confirmed(self.f(in_scope=True)))

    def test_scope_does_not_rescue_a_finding_that_fails_the_other_bars(self):
        self.assertFalse(review.is_confirmed(self.f(in_scope=True,
                                                    severity="minor")))
        self.assertFalse(review.is_confirmed(self.f(in_scope=True,
                                                    confidence="low")))
        self.assertFalse(review.is_confirmed(self.f(in_scope=True,
                                                    failure_scenario="")))

    def test_a_truthy_non_boolean_does_not_silently_exclude(self):
        """Only an explicit False excludes. "false", 0 and None must not."""
        for v in ("false", "no", 0, None, ""):
            self.assertTrue(review.is_confirmed(self.f(in_scope=v)), repr(v))

    def test_threat_model_reaches_the_prompt(self):
        p = review.build_prompt("code", ["a claim"], False, "ctx",
                                "contract.json is trusted input")
        self.assertIn("THREAT MODEL", p)
        self.assertIn("contract.json is trusted input", p)

    def test_prompt_is_unchanged_when_no_threat_model_is_given(self):
        p = review.build_prompt("code", ["a claim"], False, "ctx")
        self.assertNotIn("THREAT MODEL", p)

    def test_the_system_prompt_states_the_scope_rule(self):
        self.assertIn("in_scope", review.SYSTEM)
        self.assertIn("preconditions", review.SYSTEM)


class TestThreatModelActuallyReachesReviewers(unittest.TestCase):
    """luna: --threat-model was parsed and then never passed to build_prompt,
    so a whole round ran with the feature silently inert while appearing to
    work. Unit-testing build_prompt was not enough -- the break was in the
    wiring between argparse and the call."""

    def test_the_flag_is_accepted(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--threat-model", "x", "--list"],
            capture_output=True, text=True)
        self.assertNotIn("unrecognized arguments", r.stderr)

    def test_the_parsed_value_is_passed_to_the_prompt_builder(self):
        """Reads the call site, because the defect was that argparse held a
        value nothing consumed. Asserting on build_prompt alone passed while
        the flag did nothing."""
        src = SCRIPT.read_text()
        i = src.index("prompt = build_prompt(")
        call = src[i:src.index(")", src.index("args.context", i))]
        self.assertIn("args.threat_model", call,
                      "build_prompt is called without the parsed threat model")

    def test_dest_matches_what_the_call_site_reads(self):
        import argparse as _a
        ap = _a.ArgumentParser()
        ap.add_argument("--threat-model", default=None)
        self.assertEqual(ap.parse_args(["--threat-model", "v"]).threat_model,
                         "v")


class TestOutOfScopeCannotBuyAPass(unittest.TestCase):
    """deepseek: in_scope is a reviewer's judgment about MY threat model, and a
    reviewer that misreads it could dismiss a real defect with one word. It may
    demote a finding out of the verdict; it may not produce a clean pass."""

    BASE = dict(n_completed=2, n_failed=0, confirmed=[], refuted_claims=[],
                rejecting=[], truncated=False, quorum=2)

    def test_an_out_of_scope_critical_forces_partial(self):
        st = review.decide_state(**self.BASE, n_out_of_scope_critical=1)
        self.assertEqual(st, "REVIEW_PARTIAL")
        self.assertNotEqual(review.STATES[st], 0)

    def test_out_of_scope_minor_findings_do_not_block(self):
        self.assertEqual(review.decide_state(**self.BASE,
                                             n_out_of_scope_critical=0),
                         "REVIEW_PASS")

    def test_a_real_finding_still_outranks_it(self):
        st = review.decide_state(**{**self.BASE,
                                    "confirmed": [{"severity": "major"}]},
                                 n_out_of_scope_critical=1)
        self.assertEqual(st, "REVIEW_FAIL")

class TestListMeasuresAvailability(unittest.TestCase):
    """`--list` printed "ready" for reviewers whose account had no credits, and
    the real call then failed with HTTP 429. SKILL.md claimed availability was
    "resolved live by review.py --list, never asserted"; it was asserted, from
    nothing more than whether an environment variable was set. Found by using
    the gate, not by reviewing it."""

    def test_availability_only_checks_the_key_variable(self):
        """Kept as the low bar it is, and named so, because probe_liveness is
        the one that answers the question."""
        rev = {"name": "x", "provider": "openai", "model": "m",
               "enabled": True}
        old = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["OPENAI_API_KEY"] = "sk-not-a-real-key"
            self.assertIsNone(review.availability(rev))
            os.environ.pop("OPENAI_API_KEY")
            self.assertIn("OPENAI_API_KEY", review.availability(rev))
        finally:
            if old is not None:
                os.environ["OPENAI_API_KEY"] = old
            else:
                os.environ.pop("OPENAI_API_KEY", None)

    def test_probe_reports_a_provider_error_instead_of_ready(self):
        """No network: substitute a provider that fails the way a spent
        account does."""
        rev = {"name": "x", "provider": "openai", "model": "m",
               "enabled": True}
        old_p = review.PROVIDERS.get("openai")
        old_k = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["OPENAI_API_KEY"] = "sk-present-but-spent"
            review.PROVIDERS["openai"] = lambda r, p, t, **kw: (
                None, 'HTTP 429: {"error": {"message": "You have no credits '
                      'remaining."}}')
            why = review.probe_liveness(rev)
            self.assertIsNotNone(why, "a 429 must not read as ready")
            self.assertIn("no credits", why)
        finally:
            if old_p is not None:
                review.PROVIDERS["openai"] = old_p
            if old_k is not None:
                os.environ["OPENAI_API_KEY"] = old_k
            else:
                os.environ.pop("OPENAI_API_KEY", None)

    def test_probe_reports_ready_when_the_provider_answers(self):
        rev = {"name": "x", "provider": "openai", "model": "m",
               "enabled": True}
        old_p = review.PROVIDERS.get("openai")
        old_k = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["OPENAI_API_KEY"] = "sk-works"
            review.PROVIDERS["openai"] = lambda r, p, t, **kw: ("x", None)
            self.assertIsNone(review.probe_liveness(rev))
        finally:
            if old_p is not None:
                review.PROVIDERS["openai"] = old_p
            if old_k is not None:
                os.environ["OPENAI_API_KEY"] = old_k
            else:
                os.environ.pop("OPENAI_API_KEY", None)

    def test_a_crashing_provider_does_not_traceback_out_of_list(self):
        rev = {"name": "x", "provider": "openai", "model": "m",
               "enabled": True}
        old_p = review.PROVIDERS.get("openai")
        old_k = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["OPENAI_API_KEY"] = "sk-works"
            def boom(*a, **k):
                raise RuntimeError("socket exploded")
            review.PROVIDERS["openai"] = boom
            why = review.probe_liveness(rev)
            self.assertIn("RuntimeError", why)
        finally:
            if old_p is not None:
                review.PROVIDERS["openai"] = old_p
            if old_k is not None:
                os.environ["OPENAI_API_KEY"] = old_k
            else:
                os.environ.pop("OPENAI_API_KEY", None)

    def test_short_error_pulls_the_message_out_of_a_json_body(self):
        """It reported "HTTP 429: {" -- the first line, and useless."""
        got = review._short_error(
            'HTTP 429: {"error": {"message": "You have no credits remaining."}}')
        self.assertIn("no credits remaining", got)
        self.assertNotEqual(got.strip().endswith("{"), True)

    def test_short_error_survives_a_non_json_body(self):
        self.assertIn("gateway", review._short_error("HTTP 502: bad gateway"))

    def test_no_probe_labels_its_answer_as_unverified(self):
        src = SCRIPT.read_text()
        self.assertIn("UNVERIFIED", src)
        self.assertIn("--no-probe", src)

class TestEmptyContentNamesItsCause(unittest.TestCase):
    """kimi-k2.7-code was written off as an unavailable reviewer for a whole
    session on the strength of "unparseable verdict: empty response". It was
    answering fine: as a heavy reasoner it spent its ENTIRE 16000-token output
    budget on reasoning tokens and emitted no content, with
    finish_reason=length. The message named the symptom and hid the cause, so
    the obvious next step -- raise the budget -- was never taken."""

    def _reply(self, content, finish_reason, reasoning=None, completion=None):
        return {"choices": [{"message": {"content": content},
                             "finish_reason": finish_reason}],
                "usage": {"completion_tokens": completion,
                          "completion_tokens_details":
                              {"reasoning_tokens": reasoning}}}

    def call(self, data):
        """Drive call_openrouter with a substituted transport."""
        old_post = review._post
        old_key = os.environ.get("OPENROUTER_API_KEY")
        try:
            os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
            review._post = lambda *a, **k: (data, None)
            return review.call_openrouter(
                {"name": "kimi-k2.7-code", "model": "m"}, "prompt", 30)
        finally:
            review._post = old_post
            if old_key is not None:
                os.environ["OPENROUTER_API_KEY"] = old_key
            else:
                os.environ.pop("OPENROUTER_API_KEY", None)

    def test_budget_exhaustion_says_to_raise_the_budget(self):
        res, err = self.call(self._reply("", "length", reasoning=17240,
                                         completion=16000))
        self.assertIsNone(res)
        self.assertIn("whole output budget", err)
        self.assertIn("reasoning", err)
        self.assertIn("max_output_tokens", err)

    def test_empty_for_another_reason_reports_that_reason(self):
        res, err = self.call(self._reply("", "content_filter"))
        self.assertIsNone(res)
        self.assertIn("content_filter", err)
        self.assertNotIn("whole output budget", err)

    def test_whitespace_only_content_counts_as_empty(self):
        res, err = self.call(self._reply("   \n  ", "length", completion=16000))
        self.assertIsNone(res)
        self.assertIn("output budget", err)

    def test_real_content_still_comes_through(self):
        res, err = self.call(self._reply('{"verdict": "upheld"}', "stop"))
        self.assertIsNone(err)
        self.assertEqual(res["text"], '{"verdict": "upheld"}')

    def test_every_reviewer_has_room_to_reason_and_then_answer(self):
        """Was asserted per-reviewer for kimi; deepseek then hit the same wall
        at a larger input, so the DEFAULT is what needs to be right. Reasoning
        tokens come out of the answer's budget."""
        import json as _json
        cfg = _json.loads((REPO / "skills" / "hanig-review-gate"
                           / "reviewers.json").read_text())
        self.assertGreaterEqual(review.DEFAULT_MAX_OUTPUT_TOKENS, 32_000)
        for r in cfg["reviewers"]:
            effective = r.get("max_output_tokens",
                              review.DEFAULT_MAX_OUTPUT_TOKENS)
            self.assertGreaterEqual(effective, 32_000, r["name"])


class TestEscalateFormsACommittee(unittest.TestCase):
    """The ladder checked `bad` BEFORE quorum, so one reviewer's refuted claim
    ended it on its own. Four of five reviews in one session were decided by a
    single model and no committee ever formed -- and the one round that did
    reach quorum is the round where the second and third readers found what the
    first had upheld. A finding from one reviewer is a hypothesis; adjudicating
    it is what the panel is for.

    Nothing in this file covered the stopping rule, so changing it broke no
    test. That is why these exist."""

    class Args:
        def __init__(self, quorum):
            self.quorum = quorum
            self.json = True
            self.timeout = 1
            self.claim = []

    def setUp(self):
        self.roster = [
            {"name": "cheap1", "profiles": ["fast"]},
            {"name": "cheap2", "profiles": ["fast"]},
            {"name": "mid1", "profiles": ["standard"]},
            {"name": "mid2", "profiles": ["standard"]},
            {"name": "dear1", "profiles": ["deep"]},
        ]
        self._avail = review.availability
        self._run = review.run_one
        self.addCleanup(setattr, review, "availability", self._avail)
        self.addCleanup(setattr, review, "run_one", self._run)

    def stub(self, available, verdicts):
        """available: names that answer. verdicts: name -> refuted bool."""
        review.availability = lambda r: (
            None if r["name"] in available else "no credits")
        def run_one(rev, *a, **k):
            refuted = verdicts.get(rev["name"], False)
            return {"ok": True, "name": rev["name"],
                    "verdict": "refuted" if refuted else "upheld",
                    "findings": [],
                    "claims": ([{"status": "refuted", "claim": "c"}]
                               if refuted else [{"status": "upheld",
                                                 "claim": "c"}]),
                    "elapsed_s": 0.1}
        review.run_one = run_one

    def test_a_lone_refuted_verdict_does_not_end_the_ladder(self):
        # Only one fast reviewer answers, and it refutes.
        self.stub({"cheap1", "mid1", "mid2"}, {"cheap1": True})
        completed, _failed, _un, tiers = review.escalate(
            self.roster, "p", self.Args(quorum=2), False, "l", 10)
        self.assertIn("standard", tiers,
                      "the ladder stopped on one reviewer's refuted claim; "
                      "no committee formed")
        self.assertGreaterEqual(
            len(completed), 2,
            "a verdict was reached below quorum on a single opinion")

    def test_the_ladder_stops_once_quorum_has_adjudicated(self):
        # Both fast reviewers answer; one refutes. Quorum 2 is met, so the
        # dearer tiers must not run: cheapest-first still holds.
        self.stub({"cheap1", "cheap2", "mid1", "dear1"}, {"cheap1": True})
        _c, _f, _u, tiers = review.escalate(
            self.roster, "p", self.Args(quorum=2), False, "l", 10)
        self.assertEqual(tiers, ["fast"],
                         "quorum was met with a finding, so the ladder should "
                         "have stopped before the dearer tiers")

    def test_a_clean_tier_below_quorum_still_escalates(self):
        # Unchanged behaviour, asserted so it cannot regress.
        self.stub({"cheap1", "mid1"}, {})
        _c, _f, _u, tiers = review.escalate(
            self.roster, "p", self.Args(quorum=2), False, "l", 10)
        self.assertIn("standard", tiers)


class TestSolIsBothDeepReviewerAndTieBreaker(unittest.TestCase):
    """Sol has two roles, not one. A first attempt gated it behind
    `tiebreak_only`, which excluded it from the deep tier altogether -- it must
    review at deep AND arbitrate when the cheaper tiers cannot fill quorum.

    The ladder already gives both: deep is reached when the cheaper tiers came
    up clean, and when they could not reach quorum. It is skipped only when a
    finding already has quorum behind it, which is the case where the right
    move is to fix the finding rather than pay for another opinion."""

    class Args:
        def __init__(self, quorum):
            self.quorum = quorum
            self.json = True
            self.timeout = 1
            self.claim = []

    def setUp(self):
        self.roster = [
            {"name": "cheap1", "profiles": ["fast"]},
            {"name": "cheap2", "profiles": ["fast"]},
            {"name": "mid1", "profiles": ["standard"]},
            {"name": "sol", "profiles": ["deep"]},
        ]
        self._avail, self._run = review.availability, review.run_one
        self.addCleanup(setattr, review, "availability", self._avail)
        self.addCleanup(setattr, review, "run_one", self._run)

    def stub(self, available, refuting=()):
        review.availability = lambda r: (
            None if r["name"] in available else "no credits")
        def run_one(rev, *a, **k):
            bad = rev["name"] in refuting
            return {"ok": True, "name": rev["name"],
                    "verdict": "refuted" if bad else "upheld", "findings": [],
                    "claims": [{"status": "refuted" if bad else "upheld",
                                "claim": "c"}],
                    "elapsed_s": 0.1}
        review.run_one = run_one

    def ran(self, completed):
        return [r["name"] for r in completed]

    def test_sol_reviews_at_deep_when_the_cheaper_tiers_are_clean(self):
        self.stub({"cheap1", "cheap2", "mid1", "sol"})
        completed, _f, _u, tiers = review.escalate(
            self.roster, "p", self.Args(quorum=3), False, "l", 10)
        self.assertIn("deep", tiers)
        self.assertIn("sol", self.ran(completed),
                      "sol must review at deep, not only arbitrate")

    def test_sol_is_recruited_when_the_panel_cannot_fill_quorum(self):
        # Only one cheap reviewer answers, so quorum 2 needs sol.
        self.stub({"cheap1", "sol"})
        completed, _f, _u, _t = review.escalate(
            self.roster, "p", self.Args(quorum=2), False, "l", 10)
        self.assertIn("sol", self.ran(completed),
                      "the panel was below quorum and sol was not recruited")

    def test_a_finding_from_an_earlier_tier_is_not_forgotten(self):
        """glm-5.1, MAJOR: `bad` was computed from the CURRENT tier only, so a
        finding in fast was forgotten when standard came back clean with quorum
        met, and the ladder paid for the deep tier with a defect already on the
        table.

        glm-5.1 also noted that test_a_lone_refuted_verdict_does_not_end_the_
        ladder has this exact cross-tier shape but never exercises the gap,
        because its deep reviewer is unavailable. Here sol IS available, which
        is the whole point."""
        self.stub({"cheap1", "mid1", "sol"}, refuting={"cheap1"})
        completed, _f, _u, tiers = review.escalate(
            self.roster, "p", self.Args(quorum=2), False, "l", 10)
        self.assertNotIn(
            "deep", tiers,
            "a finding in the fast tier was forgotten once standard came back "
            "clean, so the ladder paid for the deep tier anyway")
        self.assertNotIn("sol", [r["name"] for r in completed])

    def test_sol_is_not_paid_for_a_finding_that_already_has_quorum(self):
        self.stub({"cheap1", "cheap2", "mid1", "sol"}, refuting={"cheap1"})
        completed, _f, _u, tiers = review.escalate(
            self.roster, "p", self.Args(quorum=2), False, "l", 10)
        self.assertNotIn("deep", tiers)
        self.assertNotIn("sol", self.ran(completed),
                         "a finding with quorum behind it should be fixed, not "
                         "escalated to the dearest reviewer")


class TestProtocolIsEnforcedNotRemembered(unittest.TestCase):
    """Every rule here was already written down in a memory file and drifted
    from anyway, in the same session that wrote it. Prose is not a constraint.
    See skills/hanig-review-gate/PROTOCOL.md."""

    def test_the_plan_panel_is_exactly_two_contrasting_models(self):
        revs = review.load_reviewers()
        panel = [r for r in revs if "plan" in (r.get("profiles") or [])
                 and r.get("enabled", True)]
        self.assertEqual(
            len(panel), 2,
            f"a plan review is two contrasting models; found {len(panel)}. "
            f"A third adds agreement, not insight.")
        self.assertEqual(
            len({r["provider"] for r in panel}), 2,
            "the two plan reviewers share a provider, so they can share a "
            "failure mode; contrasting means different provider and family")

    def test_the_round_bound_exists_and_is_three(self):
        self.assertEqual(review.MAX_ROUNDS, 3)

    def test_plan_and_escalate_are_refused_together(self):
        src = (REPO / "skills" / "hanig-review-gate" / "scripts"
               / "review.py").read_text()
        self.assertIn("--plan and --escalate are contradictory", src)

    def test_every_protocol_refusal_names_an_action(self):
        """The rule the gate itself was exempt from for five rounds."""
        import ast
        src = (REPO / "skills" / "hanig-review-gate" / "scripts"
               / "review.py").read_text()
        tree = ast.parse(src)
        actionable = ("Drop ", "drop ", "Pass ", "pass ", "remove ", "Step "
                      "back", "Override", "must be", "Available:")
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Name) and f.id == "config_error"):
                continue
            text = " ".join(
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str))
            if len(text.strip()) < 30:
                continue
            checked += 1
            self.assertTrue(
                any(a in text for a in actionable),
                f"a config_error names no action: {text[:130]}")
        self.assertGreater(checked, 3,
                           "recovered too few config_error messages to be "
                           "measuring anything")


if __name__ == "__main__":
    unittest.main(verbosity=2)
