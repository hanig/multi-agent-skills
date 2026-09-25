#!/usr/bin/env python3
"""Tests for review.py — the gate's own integrity.

A gate that can produce a false pass is worse than no gate, so these focus on
the ways round 4 showed it could: a non-answer filling a quorum slot, truncated
input passing, enum case defeating the refutation rule, and a config error
reading as a review failure.

Offline: no API calls.

    python3 tests/test_review.py
"""

import ast
import errno
import importlib.util
import hashlib
import io
import json
import os
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "hanig-review-gate" / "scripts" / "review.py"

spec = importlib.util.spec_from_file_location("review", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


_MODULE_STATE_HOME = None
_SAVED_MODULE_ENV = None
_ORIGINAL_APPEND_REVIEW_JOURNAL = review.append_review_journal
_ORIGINAL_RECORD_REVIEW_ROUND = review.record_review_round


def _module_home():
    return Path.home()


def _new_module_state_home(worktrees):
    """Create an outside-worktree fixture only when journal I/O needs one."""
    failures = []

    def candidate_parents():
        seen = set()
        candidates = (
            ("repository parent", lambda: REPO.parent),
            ("home directory", _module_home),
            ("default temporary directory",
             lambda: Path(tempfile.gettempdir())),
        )
        for label, get_candidate in candidates:
            try:
                candidate = get_candidate().resolve()
            except (OSError, RuntimeError) as exc:
                failures.append("%s: %s" % (label, exc))
                continue
            if candidate not in seen:
                seen.add(candidate)
                yield label, candidate

    for label, parent in candidate_parents():
        if any(review._inside(parent, worktree) for worktree in worktrees):
            failures.append("%s (%s): inside an operated worktree" %
                            (label, parent))
            continue
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix=review.JOURNAL_TEST_ROOT_PREFIX, dir=parent)
        except OSError as exc:
            failures.append("%s: %s" % (parent, exc))
            continue
        fixture_root = Path(temporary.name).resolve()
        try:
            review._require_git_free_test_root(fixture_root)
        except OSError as exc:
            failures.append("%s: %s" % (fixture_root, exc))
            temporary.cleanup()
            continue
        return temporary
    raise RuntimeError("no outside-worktree directory is available for "
                       "review-test state: %s" % "; ".join(failures))


def _ensure_module_state_home():
    """Lazily isolate the first in-process journal persistence attempt."""
    global _MODULE_STATE_HOME, _SAVED_MODULE_ENV
    if _MODULE_STATE_HOME is None:
        temporary = _new_module_state_home(review.review_worktrees([SCRIPT]))
        _SAVED_MODULE_ENV = {
            "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME"),
            review.JOURNAL_TEST_MARKER:
                os.environ.get(review.JOURNAL_TEST_MARKER),
        }
        fixture_root = Path(temporary.name).resolve()
        os.environ["XDG_STATE_HOME"] = str(fixture_root / "state")
        os.environ[review.JOURNAL_TEST_MARKER] = str(fixture_root)
        _MODULE_STATE_HOME = temporary
    return Path(_MODULE_STATE_HOME.name).resolve()


def _isolated_append_review_journal(*args, **kwargs):
    _ensure_module_state_home()
    return _ORIGINAL_APPEND_REVIEW_JOURNAL(*args, **kwargs)


def _isolated_record_review_round(*args, **kwargs):
    _ensure_module_state_home()
    return _ORIGINAL_RECORD_REVIEW_ROUND(*args, **kwargs)


def _hard_link_or_skip(test_case, source, destination):
    """Construct the hard-link attack, or skip when the host forbids it."""
    unsupported = {errno.EACCES, errno.EXDEV, errno.EPERM}
    for name in ("ENOTSUP", "EOPNOTSUPP"):
        value = getattr(errno, name, None)
        if value is not None:
            unsupported.add(value)
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno in unsupported:
            test_case.skipTest(
                "hard-link defence requires a filesystem that permits the "
                "same-filesystem link: %s" % exc)
        raise


def setUpModule():
    """Install I/O-free wrappers for exactly one unittest module lifecycle."""
    review.append_review_journal = _isolated_append_review_journal
    review.record_review_round = _isolated_record_review_round


def tearDownModule():
    global _MODULE_STATE_HOME, _SAVED_MODULE_ENV
    review.append_review_journal = _ORIGINAL_APPEND_REVIEW_JOURNAL
    review.record_review_round = _ORIGINAL_RECORD_REVIEW_ROUND
    try:
        if _SAVED_MODULE_ENV is not None:
            for name, value in _SAVED_MODULE_ENV.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    finally:
        try:
            if _MODULE_STATE_HOME is not None:
                _MODULE_STATE_HOME.cleanup()
        finally:
            _MODULE_STATE_HOME = None
            _SAVED_MODULE_ENV = None


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
        # --kind is required before the quorum check is reached, so this
        # names one: the test is about quorum 0, not about the new flag.
        r = subprocess.run([sys.executable, str(SCRIPT), "--quorum", "0",
                            "--kind", "implementation", "--round", "1",
                            "--claim", review.HONEST_RUN_CLAIM,
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
        #
        # `committee` is exempt for a different reason: it is not a review
        # panel at all. It names committee.py's members, who plan and are
        # challenged across turns. Keeping it out of the ladder is what lets a
        # model sit on the committee while being barred from the gate --
        # deepseek plans well and over-claims as a refuter, and that split is
        # only expressible because these two lists are separate.
        # tiebreak rules on a split; it must never enter the gate ladder.
        tiers -= {"plan", "committee", "tiebreak"}
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
        """A FIFO handed to --file is refused, not waited on.

        The test used `--list`, which prints the reviewer roster and
        exits WITHOUT OPENING --file, so it passed because `--list`
        returns -- not because a FIFO was rejected. The path it is
        named for had never run. `--list` also contacts every provider
        first (measured 13.3s against a 15s deadline), so it raced a
        network round trip it did not need.

        A real reviewing invocation reads the file, refuses a
        non-regular one in `read_text_bounded`, and exits before any
        provider is contacted: measured rc=4 in 0.14s.

        The asserted contract lives in review.py: `config_error` exits
        4 (REVIEW_ERROR, a configuration problem) and prints
        "cannot read {path}: {reason}". Both are asserted here because
        a refusal that names neither is not a usable diagnostic.

        The `communicate` deadline is inherited from this test as it
        stood and widened from 15s to 60s, against an operation that
        went from 13.3s to 0.14s. It is the only way to notice the hang
        the test exists for, and every honest run reaches it with four
        hundred times the margin it had before.
        """
        import tempfile as _tf, os as _os
        tmp = Path(_tf.mkdtemp())
        fifo = tmp / "src.fifo"
        _os.mkfifo(fifo)
        pr = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--kind", "implementation",
             "--file", str(fifo), "--round", "1",
             "--claim", "This change cannot make an honest run fail."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = pr.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            pr.kill()
            pr.communicate()
            self.fail("review.py hung on a FIFO --file argument")
        both = out + err
        self.assertEqual(
            pr.returncode, 4,
            "a FIFO must be the gate's configuration-error exit, not %r: %r"
            % (pr.returncode, both[:400]))
        self.assertIn("cannot read", both)
        self.assertIn(str(fifo), both,
                      "the refusal did not name the path it refused")

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


class TestRangeDivergence(unittest.TestCase):
    """Exercise the actual diff collector against local Git history, offline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Range Test")
        self.git("config", "user.email", "range@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.commit_file("shared.txt", "shared\n")
        self.git("branch", "base")
        self.git("checkout", "-qb", "branch")
        self.commit_file("branch.txt", "branch change\n")
        self.git("checkout", "-q", "base")
        self.commit_file("base-one.txt", "first base addition\n")
        self.commit_file("base-two.txt", "second base addition\n")
        self.git("checkout", "-q", "branch")
        previous = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True).stdout

    def commit_file(self, name, content):
        (self.repo / name).write_text(content)
        self.git("add", name)
        self.git("commit", "-qm", name)

    def gather(self, range_spec):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            body, label = review.gather(SimpleNamespace(
                diff=False, staged=False, range=range_spec, file=[]))
        return body, label, stderr.getvalue()

    def range_cli(self, range_spec):
        # A nonempty diff reaches journal persistence in the child, where the
        # in-process wrappers cannot initialize the lazy module fixture.
        _ensure_module_state_home()
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENROUTER_API_KEY", None)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--range", range_spec,
             "--kind", "implementation", "--round", "1",
             "--claim", review.HONEST_RUN_CLAIM],
            capture_output=True, text=True, env=env, timeout=30)

    def test_unresolvable_three_dot_base_names_ref_instead_of_empty(self):
        result = self.range_cli("deadbeef...HEAD")
        self.assertIn("unresolvable ref 'deadbeef'", result.stderr)
        self.assertEqual(result.returncode, review.STATES["REVIEW_ERROR"])
        self.assertIn("REVIEW_ERROR", result.stderr)
        git_error = subprocess.run(
            ["git", "rev-parse", "--verify", "deadbeef^{commit}"],
            capture_output=True, text=True, check=False)
        self.assertNotEqual(git_error.returncode, 0)
        self.assertTrue(git_error.stderr.strip())
        self.assertIn(git_error.stderr.strip(), result.stderr)
        self.assertNotIn("empty", result.stdout + result.stderr)

    def test_unresolvable_endpoint_is_rejected_in_every_range_form(self):
        for range_spec in ("deadbeef..HEAD", "HEAD..deadbeef",
                           "HEAD...deadbeef", "deadbeef",
                           "deadbeef..", "..deadbeef",
                           "deadbeef...", "...deadbeef"):
            with self.subTest(range_spec=range_spec):
                result = self.range_cli(range_spec)
                self.assertIn("unresolvable ref 'deadbeef'", result.stderr)
                self.assertIn("REVIEW_ERROR", result.stderr)
                self.assertEqual(result.returncode, review.STATES["REVIEW_ERROR"])
                self.assertNotIn("empty", result.stdout + result.stderr)

    def test_resolvable_empty_ranges_still_report_empty(self):
        for range_spec in ("HEAD..HEAD", "HEAD...HEAD", "HEAD",
                           "HEAD..", "..HEAD", "HEAD...", "...HEAD"):
            with self.subTest(range_spec=range_spec):
                result = self.range_cli(range_spec)
                self.assertEqual(result.returncode, review.STATES["REVIEW_ERROR"])
                self.assertIn(
                    "nothing to review (commit range " + range_spec + " is empty)",
                    result.stdout)
                self.assertEqual(result.stderr, "")

    def test_single_commit_keeps_working_tree_diff(self):
        (self.repo / "branch.txt").write_text("uncommitted change\n")
        body, label, warning = self.gather("HEAD")
        self.assertEqual(body, self.git("diff", "HEAD"))
        self.assertIn("+uncommitted change", body)
        self.assertEqual(label, "commit range HEAD")
        self.assertEqual(warning, "")

    def test_single_commit_range_shorthands_keep_git_diff_semantics(self):
        for range_spec in ("HEAD^!", "HEAD^@", "HEAD^-", "HEAD^-1"):
            with self.subTest(range_spec=range_spec):
                body, _label, warning = self.gather(range_spec)
                self.assertTrue(body)
                self.assertEqual(body, self.git("diff", range_spec))
                self.assertEqual(warning, "")

    def test_commit_search_expressions_are_resolved_before_splitting(self):
        self.git("commit", "--allow-empty", "-qm", "needle..dots needle...dots")
        self.commit_file("later.txt", "after the matching commit\n")
        for expression in ("HEAD^{/needle..dots}", "HEAD^{/needle...dots}",
                           ":/needle..dots", ":/needle...dots", ":/needle"):
            with self.subTest(expression=expression):
                body, label, warning = self.gather(expression)
                self.assertTrue(body)
                self.assertEqual(body, self.git("diff", expression))
                self.assertEqual(label, "commit range " + expression)
                self.assertEqual(warning, "")

    def test_dotted_search_in_left_endpoint_cli_reports_empty(self):
        self.git("commit", "--allow-empty", "-qm", "needle..dots needle...dots")
        for operator in ("..", "..."):
            for search in ("needle..dots", "needle...dots"):
                left = "HEAD^{/" + search + "}"
                expression = left + operator + "HEAD"
                with self.subTest(expression=expression):
                    self.assertEqual(self.git("rev-parse", "--verify", left),
                                     self.git("rev-parse", "HEAD"))
                    result = self.range_cli(expression)
                    self.assertEqual(result.returncode,
                                     review.STATES["REVIEW_ERROR"])
                    self.assertIn("nothing to review (commit range " +
                                  expression + " is empty)", result.stdout)
                    self.assertEqual(result.stderr, "")

    def test_dotted_left_search_accepts_omitted_and_braced_right_endpoints(self):
        self.git("commit", "--allow-empty", "-qm", "needle{..dots needle{...dots")
        for dots in ("..", "..."):
            # A bracket expression makes { literal in both GNU and BSD regex.
            # It still must not nest the revision suffix's closing brace.
            left = "HEAD^{/needle[{]" + dots + "dots}"
            for operator in ("..", "..."):
                for right in ("", left):
                    expression = left + operator + right
                    with self.subTest(expression=expression):
                        self.assertEqual(self.git("rev-parse", "--verify", left),
                                         self.git("rev-parse", "HEAD"))
                        result = self.range_cli(expression)
                        self.assertIn("nothing to review (commit range " +
                                      expression + " is empty)", result.stdout)
                        self.assertEqual(result.returncode,
                                         review.STATES["REVIEW_ERROR"])
                        self.assertEqual(result.stderr, "")

    def test_range_cli_isolates_journal_before_first_in_process_write(self):
        # A fresh interpreter prevents earlier journal tests from masking a
        # missing initialization in range_cli. Use a real nonempty diff and
        # the real journal child; no provider credentials reach the CLI.
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve() / "operator-state"
            seed = state / review.JOURNAL_DIR / "seed" / "record.jsonl"
            seed.parent.mkdir(parents=True)
            seed.write_bytes(b"operator history\n")
            env = dict(os.environ, XDG_STATE_HOME=str(state))
            env.pop(review.JOURNAL_TEST_MARKER, None)
            program = "\n".join((
                "import json, os",
                "from pathlib import Path",
                "import tests.test_review as module",
                "assert module._MODULE_STATE_HOME is None",
                "module.setUpModule()",
                "case = module.TestRangeDivergence()",
                "try:",
                "    case.setUp()",
                "    result = case.range_cli('HEAD~1..HEAD')",
                "    state = Path(os.environ['XDG_STATE_HOME'])",
                "    records = [json.loads(p.read_text()) for p in state.glob('hanig-review-gate/review-rounds/*/record.jsonl')]",
                "    print(json.dumps({'code': result.returncode, 'stderr': result.stderr, 'records': records}))",
                "finally:",
                "    case.doCleanups()",
                "    module.tearDownModule()",
            ))
            result = subprocess.run(
                [sys.executable, "-c", program], cwd=REPO, env=env,
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["code"], review.STATES["REVIEW_UNAVAILABLE"])
            self.assertEqual(report["stderr"], "")
            self.assertEqual([r["verdict"] for r in report["records"]],
                             ["REVIEW_UNAVAILABLE"])
            self.assertEqual(sorted(p for p in state.rglob("*") if p.is_file()),
                             [seed])
            self.assertEqual(seed.read_bytes(), b"operator history\n")

    def test_dotted_left_search_reports_the_complete_bad_endpoint(self):
        self.git("commit", "--allow-empty", "-qm", "needle..dots")
        left = "HEAD^{/needle..dots}"
        missing = "HEAD^{/missing..search}"
        unclosed = "HEAD^{/needle..dots"
        for operator in ("..", "..."):
            for expression, bad_ref in (
                    (left + operator + "deadbeef", "deadbeef"),
                    (missing + operator + "HEAD", missing),
                    (unclosed + operator + "HEAD", unclosed + operator + "HEAD")):
                with self.subTest(expression=expression):
                    result = self.range_cli(expression)
                    self.assertEqual(result.returncode,
                                     review.STATES["REVIEW_ERROR"])
                    self.assertIn("unresolvable ref " + repr(bad_ref),
                                  result.stderr)
                    self.assertNotIn("empty", result.stdout + result.stderr)

    def test_braced_suffixes_and_literal_ref_braces_keep_git_diff(self):
        self.git("branch", "brace{base", "base")
        self.git("branch", "brace}base", "base")
        for left in ("base@{0}", "base^{}", "base^{commit}",
                     "base^{/base-one}", "brace{base", "brace}base"):
            for operator in ("..", "..."):
                expression = left + operator + "HEAD"
                with self.subTest(expression=expression):
                    body, _label, warning = self.gather(expression)
                    self.assertTrue(body)
                    self.assertEqual(body, self.git("diff", expression))
                    if operator == "..":
                        self.assertIn("NOT in HEAD", warning)
                    else:
                        self.assertEqual(warning, "")

    def test_dotted_search_in_right_endpoint_preserves_range_operator(self):
        self.git("commit", "--allow-empty", "-qm", "needle..dots needle...dots")
        for operator in ("..", "..."):
            for search in ("needle..dots", "needle...dots"):
                expression = "base" + operator + "HEAD^{/" + search + "}"
                with self.subTest(expression=expression):
                    body, _label, warning = self.gather(expression)
                    self.assertTrue(body)
                    self.assertEqual(body, self.git("diff", expression))
                    if operator == "..":
                        self.assertIn("2 commits on base are NOT in", warning)
                    else:
                        self.assertEqual(warning, "")

    def test_single_commit_shorthands_still_reject_unresolvable_refs(self):
        for range_spec, bad_ref in (("deadbeef^!", "deadbeef"),
                                    ("deadbeef^@", "deadbeef"),
                                    ("deadbeef^-", "deadbeef"),
                                    ("HEAD^-2", "HEAD^2")):
            with self.subTest(range_spec=range_spec):
                result = self.range_cli(range_spec)
                self.assertEqual(result.returncode, review.STATES["REVIEW_ERROR"])
                self.assertIn("unresolvable ref " + repr(bad_ref), result.stderr)
                self.assertNotIn("empty", result.stdout + result.stderr)

    def test_commit_tag_and_three_dot_omitted_endpoints_keep_diff(self):
        self.git("tag", "-a", "start", "-m", "start", "HEAD~1")
        for range_spec in ("start..HEAD", "start...HEAD", "start...",
                           "...base", "HEAD~1", "start"):
            with self.subTest(range_spec=range_spec):
                body, _label, warning = self.gather(range_spec)
                self.assertTrue(body)
                self.assertEqual(body, self.git("diff", range_spec))
                self.assertEqual(warning, "")

    def test_existing_tree_object_is_not_a_commit_endpoint(self):
        result = self.range_cli("HEAD^{tree}")
        self.assertEqual(result.returncode, review.STATES["REVIEW_ERROR"])
        self.assertIn("unresolvable ref 'HEAD^{tree}'", result.stderr)
        self.assertNotIn("empty", result.stdout + result.stderr)

    def test_bad_endpoint_is_rejected_before_collecting_any_diff(self):
        with patch.object(review, "git_out") as git_out:
            with self.assertRaises(SystemExit) as stopped:
                self.gather("HEAD..deadbeef")
        self.assertEqual(stopped.exception.code, review.STATES["REVIEW_ERROR"])
        git_out.assert_not_called()

    def test_diverged_two_dot_warns_with_base_only_count(self):
        body, label, warning = self.gather("base..branch")
        self.assertIn("WARNING", warning)
        self.assertIn("2 commits on base are NOT in branch", warning)
        self.assertIn("2 commits on base are NOT in branch", label)
        self.assertIn("deletions", warning)
        self.assertIn("base...branch", warning)
        self.assertIn("merge-base", warning)
        self.assertEqual(body, self.git("diff", "base..branch"))
        self.assertIn("deleted file mode", body)
        self.assertNotEqual(body, self.git("diff", "base...branch"))

    def test_three_dot_has_no_warning(self):
        body, label, warning = self.gather("base...branch")
        self.assertEqual(warning, "")
        self.assertEqual(label, "commit range base...branch")
        self.assertEqual(body, self.git("diff", "base...branch"))
        self.assertNotIn("deleted file mode", body)

    def test_up_to_date_two_dot_has_no_warning(self):
        self.git("merge", "-q", "--no-edit", "base")
        body, label, warning = self.gather("base..branch")
        self.assertEqual(warning, "")
        self.assertEqual(label, "commit range base..branch")
        self.assertEqual(body, self.git("diff", "base..branch"))
        self.assertIn("branch change", body)

    def test_merge_base_failure_is_unknown_and_keeps_diff(self):
        git_out = review.git_out

        def fail_merge_base(*args):
            if args[0] == "merge-base":
                return git_out("merge-base", "missing-ref", "branch")
            return git_out(*args)

        with patch.object(review, "git_out", side_effect=fail_merge_base):
            body, label, warning = self.gather("base..branch")
        self.assertIn("WARNING", warning)
        self.assertIn("divergence is UNKNOWN", warning)
        self.assertIn("divergence is UNKNOWN", label)
        self.assertNotIn("2 commits", warning)
        self.assertEqual(body, self.git("diff", "base..branch"))

    def test_failed_resolution_or_count_is_unknown(self):
        git_out = review.git_out
        for command in ("rev-parse", "rev-list"):
            with self.subTest(command=command):
                def fail_check(*args):
                    return "" if args[0] == command else git_out(*args)
                with patch.object(review, "git_out", side_effect=fail_check):
                    body, label, warning = self.gather("base..branch")
                self.assertIn("divergence is UNKNOWN", warning)
                self.assertIn("divergence is UNKNOWN", label)
                self.assertEqual(body, self.git("diff", "base..branch"))

    def test_omitted_endpoint_defaults_to_head(self):
        for range_spec, head in (("base..", "branch"), ("..branch", "base")):
            with self.subTest(range_spec=range_spec):
                self.git("checkout", "-q", head)
                body, _label, warning = self.gather(range_spec)
                self.assertIn("2 commits on", warning)
                self.assertIn("HEAD", warning)
                self.assertEqual(body, self.git("diff", range_spec))

    def test_header_displays_divergence_offline(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        reviewer = {"name": "offline", "profiles": ["standard"]}
        result = {"name": "offline", "ok": True, "verdict": "upheld",
                  "findings": [], "claims": [], "elapsed_s": 0}
        argv = [str(SCRIPT), "--range", "base..branch", "--kind",
                "implementation", "--round", "1", "--quorum", "1",
                "--allow-single-reviewer", "offline singleton fixture",
                "--claim", review.HONEST_RUN_CLAIM]
        with patch.object(sys, "argv", argv), \
                patch.object(review, "load_reviewers", return_value=[reviewer]), \
                patch.object(review, "availability", return_value=None), \
                patch.object(review, "run_one", return_value=result), \
                patch.object(review, "arm_watchdog"), \
                patch.object(review, "disarm_watchdog"), \
                patch.object(review, "record_review_round", return_value={}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                review.main()
        self.assertEqual(stopped.exception.code, review.STATES["REVIEW_PASS"])
        header = next(line for line in stdout.getvalue().splitlines()
                      if line.startswith("reviewing commit range"))
        self.assertIn("base..branch", header)
        self.assertIn("2 commits on base are NOT in branch", header)


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
        allowed = {"argparse", "concurrent", "datetime", "errno", "hashlib", "json", "os", "re", "signal", "stat",
                   "subprocess", "sys", "tempfile", "time", "urllib", "pathlib"}
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
                   "REVIEW_ERROR", "REVIEW_INCOMPLETE", "REVIEW_CLAIMS_REFUTED"):
            self.assertNotEqual(review.STATES[st], 0, st)


class TestClaimsRefuted(unittest.TestCase):
    BASE = dict(n_completed=3, n_failed=0, confirmed=[], refuted_claims=[],
                rejecting=[], truncated=False, quorum=3)

    def test_000_claim_refutation_is_distinct_from_a_confirmed_defect(self):
        claim = {"claim": "every input is accepted", "status": "refuted"}
        finding = {"severity": "major", "confidence": "high",
                   "failure_scenario": "valid input is rejected"}
        self.assertEqual(review.decide_state(
            **{**self.BASE, "refuted_claims": [claim]}),
            "REVIEW_CLAIMS_REFUTED")
        for claims in ([], [claim]):
            with self.subTest(claims=claims):
                self.assertEqual(review.decide_state(
                    **{**self.BASE, "confirmed": [finding],
                       "refuted_claims": claims}), "REVIEW_FAIL")
        code = review.STATES["REVIEW_CLAIMS_REFUTED"]
        self.assertEqual(code, 7)
        self.assertNotIn(code, (0, 1, 2, 3, 4, 5, 6))

    def test_quorum_and_other_nonpass_precedence_are_preserved(self):
        for overrides, expected in (
                ({}, "REVIEW_PASS"),
                ({"n_completed": 2, "refuted_claims": ["c"]}, "REVIEW_PARTIAL"),
                ({"n_completed": 2, "refuted_claims": ["c"],
                  "n_incomplete": 1}, "REVIEW_INCOMPLETE"),
                ({"refuted_claims": ["c"], "n_failed": 1,
                  "n_incomplete": 1, "truncated": True,
                  "n_out_of_scope_critical": 1}, "REVIEW_CLAIMS_REFUTED"),
                ({"refuted_claims": ["c"], "confirmed": ["f"],
                  "n_incomplete": 1}, "REVIEW_FAIL")):
            with self.subTest(overrides=overrides):
                self.assertEqual(review.decide_state(**{**self.BASE, **overrides}),
                                 expected)

    def test_cli_exit_text_json_escalation_and_persisted_verdict(self):
        claim = "The reader preserves café rows:\n" + "row detail; " * 50
        reason = "The reader drops the last row:\n" + "missing café row; " * 50
        roster = [{"name": name, "model": name, "provider": "offline",
                   "profiles": profiles} for name, profiles in (
                       ("a", ["fast", "standard", "deep"]),
                       ("b", ["fast", "standard", "deep"]),
                       ("c", ["standard", "deep"]),
                       ("d", ["deep"]))]
        with tempfile.TemporaryDirectory(dir=_ensure_module_state_home()) as tmp:
            for rendering in ([], ["--json"]):
                for ladder in ([], ["--escalate", "--profile", "fast"]):
                    with self.subTest(rendering=rendering, ladder=ladder):
                        called = []

                        def answer(reviewer, _prompt, _timeout, _count, asserted):
                            called.append(reviewer["name"])
                            claims = [{"claim_index": i, "claim": text,
                                       "status": "supported",
                                       "why": "the fixture supports this exact assertion"}
                                      for i, text in enumerate(asserted)]
                            if reviewer["name"] == "a":
                                claims[1].update(status="refuted", why=reason)
                            return {"name": reviewer["name"], "ok": True,
                                    "elapsed_s": 0, "findings": [], "claims": claims,
                                    "verdict": ("refuted" if reviewer["name"] == "a"
                                                else "upheld")}

                        argv = [str(SCRIPT), "--diff", "--kind", "implementation",
                                "--round", "1", "--profile", "standard",
                                "--quorum", "3", "--author", "codex/gpt-6-astra",
                                "--claim", review.HONEST_RUN_CLAIM,
                                "--claim", claim, *rendering, *ladder]
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with patch.object(sys, "argv", argv), \
                                patch.object(review, "load_reviewers", return_value=roster), \
                                patch.object(review, "availability", return_value=None), \
                                patch.object(review, "run_one", side_effect=answer), \
                                patch.object(review, "gather", return_value=("diff", "fixture")), \
                                patch.object(review, "arm_watchdog"), \
                                patch.dict(os.environ, {"XDG_STATE_HOME": tmp}), \
                                redirect_stdout(stdout), redirect_stderr(stderr):
                            with self.assertRaises(SystemExit) as stopped:
                                review.main()
                        self.assertEqual(stopped.exception.code, 7)
                        self.assertEqual(stderr.getvalue(), "")
                        self.assertEqual(set(called), {"a", "b", "c"})
                        self.assertEqual(len(called), 3)
                        records = sorted((Path(tmp) / review.JOURNAL_DIR /
                                          review.JOURNAL_NAME).glob("*/record.jsonl"))
                        persisted = json.loads(records[-1].read_text())
                        self.assertEqual(persisted["schema_version"], 2)
                        self.assertEqual(persisted["verdict"], "REVIEW_CLAIMS_REFUTED")
                        self.assertEqual(persisted["refuted_claims"][0]["claim"], claim)
                        self.assertEqual(persisted["refuted_claims"][0]["why"], reason)
                        if rendering:
                            report = json.loads(stdout.getvalue())
                            self.assertEqual(report["state"], "REVIEW_CLAIMS_REFUTED")
                            self.assertEqual(report["confirmed_findings"], [])
                            self.assertEqual(report["refuted_claims"], persisted["refuted_claims"])
                            self.assertEqual(report["tiers_run"],
                                             ["fast", "standard"] if ladder else ["standard"])
                            action = report["next_action"]
                        else:
                            action = stdout.getvalue()
                            self.assertIn("REVIEW_CLAIMS_REFUTED", action)
                            self.assertIn(claim, action)
                            self.assertIn(reason, action)
                        self.assertIn("Correct the claim (or the code)", action)
                        self.assertIn("Do not argue", action)
                        self.assertIn("not a pass", action)


class TestReviewIncomplete(unittest.TestCase):
    BASE = dict(n_completed=0, n_failed=1, confirmed=[], refuted_claims=[],
                rejecting=[], truncated=False, quorum=1,
                n_out_of_scope_critical=0)

    def test_silent_reviewer_is_incomplete_not_clean_or_a_finding(self):
        state = review.decide_state(**self.BASE, n_incomplete=1)
        self.assertEqual(state, "REVIEW_INCOMPLETE")
        self.assertNotEqual(state, "REVIEW_PASS")
        self.assertNotEqual(state, "REVIEW_FAIL")
        self.assertNotEqual(review.STATES[state], 0)

    def test_silent_reviewer_does_not_satisfy_required_coverage(self):
        state = review.decide_state(**self.BASE, n_incomplete=1)
        self.assertNotEqual(state, "REVIEW_PASS",
                            "zero content filled a one-reviewer quorum")

    def test_clean_single_reviewer_still_passes(self):
        state = review.decide_state(
            **{**self.BASE, "n_completed": 1, "n_failed": 0},
            n_incomplete=0)
        self.assertEqual(state, "REVIEW_PASS")

    def test_confirmed_finding_at_quorum_still_fails(self):
        state = review.decide_state(
            **{**self.BASE, "n_completed": 1,
               "confirmed": [{"severity": "major"}]},
            n_incomplete=1)
        self.assertEqual(state, "REVIEW_FAIL")


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


class TestHonestRunCounterClaimSemantics(unittest.TestCase):
    """The prompt's decision boundary, exercised without paid model calls.

    The three real refutations that exposed the ambiguity are supplemented by
    two constructed statements supplying only one required fact each. The
    fixtures identify the evidence each statement actually supplies, while the
    classification itself is evaluated from SYSTEM's decision rule. This does
    not build a second natural-language reviewer in the test suite.
    """

    RULE_FACTS = frozenset((
        "DEFECT_FREE_WORK_NAMED",
        "WRONGFUL_REJECTION_EXPLAINED",
    ))

    CASES = (
        {
            "reviewer": "luna",
            "statement": (
                "The new rule can reject an otherwise honest green run whose "
                "selected panel returns REVIEW_PASS when the required full-tier "
                "rerun finds a defect."
            ),
            "evidence": {},
            "refutes": False,
        },
        {
            "reviewer": "kimi",
            "statement": (
                "the declaration withholds merge authorization from any change "
                "that lacks a declared-tier pass"
            ),
            "evidence": {},
            "refutes": False,
        },
        {
            "reviewer": "glm-5.3",
            "statement": (
                "a false tier finding recorded 'unresolved' continues to block "
                "the merge; the merge stays blocked indefinitely and the "
                "correct work never ships."
            ),
            "evidence": {
                "DEFECT_FREE_WORK_NAMED": "correct work",
                "WRONGFUL_REJECTION_EXPLAINED": (
                    "false tier finding recorded 'unresolved' continues to "
                    "block the merge"
                ),
            },
            "refutes": True,
        },
        {
            "reviewer": "fixture: defect-free work only",
            "statement": (
                "The two-row UTC CSV export meets every acceptance criterion "
                "and is defect-free."
            ),
            "evidence": {
                "DEFECT_FREE_WORK_NAMED": (
                    "two-row UTC CSV export meets every acceptance criterion "
                    "and is defect-free"
                ),
            },
            "refutes": False,
        },
        {
            "reviewer": "fixture: wrongful rejection only",
            "statement": (
                "The new timestamp check wrongly rejects a submission solely "
                "for using UTC, although the acceptance criteria permit UTC. "
                "Other acceptance criteria remain unchecked."
            ),
            "evidence": {
                "WRONGFUL_REJECTION_EXPLAINED": (
                    "wrongly rejects a submission solely for using UTC, "
                    "although the acceptance criteria permit UTC"
                ),
            },
            "refutes": False,
        },
    )

    def honest_run_policy(self):
        claim = review.HONEST_RUN_CLAIM
        start = review.SYSTEM.rfind("\n", 0, review.SYSTEM.index(claim)) + 1
        end = review.SYSTEM.index("\n\nReply with ONLY", start)
        return review.SYSTEM[start:end]

    def assert_policy_concepts(self, policy):
        # Deliberately pin the owner-approved policy vocabulary, including its
        # anti-circularity terms; this is not a general equivalence checker.
        normalized = policy.casefold().replace("-", " ")
        for concept in ("admissible", "defect", "free", "independent",
                        "wrong", "reject", "refut", "nam", "circular",
                        "false finding"):
            self.assertIn(concept, normalized,
                          f"honest-run policy lost the {concept!r} concept")

    def honest_run_rule(self):
        marker = "HONEST_RUN_REFUTED :="
        lines = [line for line in self.honest_run_policy().splitlines()
                 if line.startswith(marker)]
        self.assertEqual(len(lines), 1,
                         "SYSTEM must contain one honest-run decision rule")
        return lines[0][len(marker):].strip()

    def evaluate_rule(self, expression, facts):
        """Evaluate SYSTEM's small boolean grammar, rejecting other syntax."""
        tree = ast.parse(expression, mode="eval")

        def evaluate(node):
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Name):
                self.assertIn(node.id, self.RULE_FACTS,
                              f"unknown honest-run fact {node.id!r}")
                return facts[node.id]
            if isinstance(node, ast.BoolOp):
                values = [evaluate(value) for value in node.values]
                if isinstance(node.op, ast.And):
                    return all(values)
                if isinstance(node.op, ast.Or):
                    return any(values)
            if (isinstance(node, ast.UnaryOp)
                    and isinstance(node.op, ast.Not)):
                return not evaluate(node.operand)
            self.fail(f"unsupported honest-run rule syntax: {ast.dump(node)}")

        return evaluate(tree)

    def counter_claim_refuted(self, case):
        policy = self.honest_run_policy()
        self.assert_policy_concepts(policy)
        self.assertEqual(set(case["evidence"]),
                         set(case["evidence"]) & self.RULE_FACTS)
        statement = case["statement"].casefold()
        for excerpt in case["evidence"].values():
            self.assertIn(excerpt.casefold(), statement)
        facts = {name: name in case["evidence"] for name in self.RULE_FACTS}
        return self.evaluate_rule(self.honest_run_rule(), facts)

    def test_system_defines_a_falsifiable_non_circular_boundary(self):
        self.assert_policy_concepts(self.honest_run_policy())

    def test_specific_equivalent_rewording_preserves_policy_concepts(self):
        policy = self.honest_run_policy().replace(
            "otherwise admissible, defect-free work",
            "work that is otherwise admissible and free of defects")
        self.assertNotEqual(policy, self.honest_run_policy())
        self.assert_policy_concepts(policy)

        facts = {"DEFECT_FREE_WORK_NAMED": True,
                 "WRONGFUL_REJECTION_EXPLAINED": True}
        reordered = ("WRONGFUL_REJECTION_EXPLAINED and "
                     "DEFECT_FREE_WORK_NAMED")
        self.assertEqual(self.evaluate_rule(reordered, facts),
                         self.evaluate_rule(self.honest_run_rule(), facts))

    def test_claim_quote_style_does_not_change_classification(self):
        original = review.SYSTEM
        self.addCleanup(setattr, review, "SYSTEM", original)
        policy = self.honest_run_policy()
        unquoted = policy.replace('"', "").replace("'", "")
        for quote in ("'", "", '"'):
            with self.subTest(quote=quote):
                restyled = unquoted.replace(
                    review.HONEST_RUN_CLAIM,
                    quote + review.HONEST_RUN_CLAIM + quote)
                review.SYSTEM = original.replace(policy, restyled)
                for case in self.CASES:
                    self.assertEqual(self.counter_claim_refuted(case),
                                     case["refutes"])

    def test_refutations_discriminate_in_both_directions(self):
        results = []
        for case in self.CASES:
            with self.subTest(reviewer=case["reviewer"],
                              statement=case["statement"]):
                actual = self.counter_claim_refuted(case)
                self.assertEqual(actual, case["refutes"])
                results.append(actual)

        self.assertIn(False, results, "correct rejection became a refutation")
        self.assertIn(True, results, "wrongful rejection became unrefutable")


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

    def test_empty_openai_reply_is_classified_as_no_content(self):
        old_post = review._post
        old_key = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["OPENAI_API_KEY"] = "sk-test"
            review._post = lambda *_args, **_kwargs: ({
                "status": "incomplete", "output": [],
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {
                    "output_tokens": 69000,
                    "output_tokens_details": {"reasoning_tokens": 69000},
                },
            }, None)
            result, error = review.call_openai(
                {"name": "luna", "model": "m"}, "prompt", 1)
        finally:
            review._post = old_post
            if old_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_key
        self.assertIsNone(result)
        self.assertIn("no content", error)
        self.assertIn("69000", error)
        self.assertIn("whole output budget", error)
        self.assertIn("max_output_tokens", error)

    def test_both_providers_call_the_shared_empty_classifier(self):
        import ast
        tree = ast.parse(SCRIPT.read_text())
        calls = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in (
                    "call_openai", "call_openrouter"):
                calls[node.name] = [
                    child for child in ast.walk(node)
                    if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "no_content_error"]
        self.assertEqual(len(calls.get("call_openai", [])), 1)
        self.assertEqual(len(calls.get("call_openrouter", [])), 1)

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
    """The protocol rules, tested by INVOKING THE CLI and asserting it refuses.

    The first version of this class asserted `review.MAX_ROUNDS == 3` and
    grepped the source for an error-message substring. sol pointed out that
    deleting the runtime guard while leaving the constant, or leaving the
    string in dead code, passes both. That is the tenth instance in this repo
    of a test built from the same assumption as the code, and I wrote it while
    fixing that very class.

    So: run the real command line, assert the exit code is REVIEW_ERROR, and
    assert no reviewer was ever contacted (a refusal at configuration time
    prints no 'reviewing ...' banner)."""

    def run_cli(self, *argv):
        import subprocess
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENROUTER_API_KEY", None)
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--file", str(SCRIPT), *argv],
            capture_output=True, text=True, timeout=120, env=env)
        return r

    def assert_refused(self, r, *, because):
        self.assertEqual(
            r.returncode, review.STATES["REVIEW_ERROR"],
            f"expected refusal ({because}); got rc={r.returncode}\n"
            f"{r.stdout[:300]}{r.stderr[:300]}")
        self.assertNotIn(
            "reviewing", r.stdout,
            f"a reviewer was contacted despite {because}")

    def test_a_review_must_declare_its_kind(self):
        self.assert_refused(self.run_cli(),
                            because="--kind was omitted")

    def test_an_implementation_review_must_declare_its_round(self):
        self.assert_refused(self.run_cli(
            "--kind", "implementation", "--claim", review.HONEST_RUN_CLAIM),
                            because="--round was omitted")

    def test_the_round_bound_is_enforced_at_runtime(self):
        self.assert_refused(
            self.run_cli("--kind", "implementation", "--round",
                         str(review.MAX_ROUNDS + 1), "--claim",
                         review.HONEST_RUN_CLAIM),
            because="the round exceeds MAX_ROUNDS")

    def test_implementation_requires_the_honest_run_counter_claim(self):
        r = self.run_cli("--kind", "implementation", "--round", "1",
                         "--claim", "malformed input is rejected")
        self.assert_refused(r, because="the honest-run counter-claim is absent")
        self.assertIn(review.HONEST_RUN_CLAIM, r.stderr)

    def test_implementation_with_no_claims_exits_review_error(self):
        r = self.run_cli("--kind", "implementation", "--round", "1")
        self.assert_refused(r, because="the claim list is empty")
        self.assertIn(review.HONEST_RUN_CLAIM, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_counter_claim_does_not_accept_multiple_terminal_periods(self):
        r = self.run_cli("--kind", "implementation", "--round", "1",
                         "--claim", review.HONEST_RUN_CLAIM + ".")
        self.assert_refused(r, because="the counter-claim has extra punctuation")
        self.assertIn(review.HONEST_RUN_CLAIM, r.stderr)

    def test_round_two_requires_dispositions(self):
        r = self.run_cli("--kind", "implementation", "--round", "2",
                         "--claim", review.HONEST_RUN_CLAIM)
        self.assert_refused(r, because="--dispositions was omitted")
        self.assertIn("--dispositions FILE", r.stderr)

    def test_a_plan_review_cannot_be_escalated(self):
        self.assert_refused(self.run_cli("--kind", "plan", "--escalate"),
                            because="--escalate was passed to a plan review")

    def test_a_plan_panel_cannot_be_widened_past_two(self):
        """sol: `--plan --only a,b,c` ran three reviewers, because the panel
        was checked before --only was applied rather than after."""
        names = [r["name"] for r in review.load_reviewers()
                 if r.get("enabled", True)][:3]
        self.assertEqual(len(names), 3, "need three enabled reviewers to test")
        self.assert_refused(
            self.run_cli("--kind", "plan", "--only", ",".join(names),
                         "--quorum", "2"),
            because="three reviewers were selected for a plan review")

    def test_a_plan_panel_cannot_be_narrowed_to_one(self):
        one = next(r["name"] for r in review.load_reviewers()
                   if r.get("enabled", True))
        self.assert_refused(
            self.run_cli("--kind", "plan", "--only", one, "--quorum", "1"),
            because="one reviewer is not a committee")

    def cycle_cli(self, *flags, failing=False, unavailable=(), roster=None):
        """Drive argparse, selection, verdict and real persistence offline."""
        if not hasattr(self, "cycle_root"):
            self.cycle_root = Path(tempfile.mkdtemp(
                dir=_ensure_module_state_home())).resolve()
            self.addCleanup(shutil.rmtree, self.cycle_root)
            (self.cycle_root / "dispositions.json").write_text("{}")
        roster = roster if roster is not None else [
            {"name": "a", "profiles": ["fast", "standard", "deep"]},
            {"name": "b", "profiles": ["fast", "standard", "deep"]},
            {"name": "c", "profiles": ["standard", "deep"]},
            {"name": "d", "profiles": ["deep"]},
        ]
        called = []

        def answer(reviewer, *_args):
            called.append(reviewer["name"])
            return {"name": reviewer["name"], "ok": True, "elapsed_s": 0,
                    "verdict": "refuted" if failing else "upheld",
                    "findings": [], "claims": [{
                        "claim": review.HONEST_RUN_CLAIM,
                        "status": "refuted" if failing else "supported",
                        "why": "offline assessment for the panel regression"}]}

        argv = [str(SCRIPT), "--file", str(SCRIPT), "--kind", "implementation",
                "--round", "1", "--claim", review.HONEST_RUN_CLAIM,
                "--dispositions", str(self.cycle_root / "dispositions.json"),
                *flags]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(review, "load_reviewers", return_value=roster), \
                patch.object(review, "DEFAULT_PROFILE", "standard"), \
                patch.object(review, "availability", side_effect=lambda r:
                             "offline" if r["name"] in unavailable else None), \
                patch.object(review, "run_one", side_effect=answer), \
                patch.object(review, "arm_watchdog"), \
                patch.dict(os.environ, {"XDG_STATE_HOME": str(self.cycle_root)}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                review.main()
        return SimpleNamespace(returncode=stopped.exception.code,
                               stdout=stdout.getvalue(),
                               stderr=stderr.getvalue(), called=called)

    def cycle_records(self):
        return [json.loads(path.read_text()) for path in sorted(
            (self.cycle_root / review.JOURNAL_DIR / review.JOURNAL_NAME)
            .glob("*/record.jsonl"))]

    def test_three_failed_rounds_cannot_restart_with_one_reviewer(self):
        for round_no in (1, 2, 3):
            result = self.cycle_cli("--round", str(round_no), "--quorum", "3",
                                    failing=True)
            self.assertEqual(result.returncode, review.STATES["REVIEW_CLAIMS_REFUTED"])
        history = self.cycle_records()
        self.assertEqual([r["round"] for r in history], [1, 2, 3])
        self.assertEqual([r["verdict"] for r in history], ["REVIEW_CLAIMS_REFUTED"] * 3)
        # Even the ordinary singleton exception cannot relax a fresh cycle.
        # Removing the fresh-cycle floor makes the override case PASS.
        for override in ([], ["--allow-single-reviewer", "diagnostic exception"]):
            with self.subTest(override=override):
                result = self.cycle_cli(
                    "--fresh-cycle-from", "standard", "--only", "d",
                    "--quorum", "1", *override)
                self.assert_refused(result, because="fresh cycle shrank to one")
                self.assertIn("fresh cycle from standard", result.stderr)
                self.assertEqual(result.called, [])
        self.assertEqual(self.cycle_records(), history)

    def test_implementation_quorum_one_needs_explicit_override(self):
        for flags in ([], ["--only", "a"], ["--escalate"]):
            with self.subTest(flags=flags):
                result = self.cycle_cli("--quorum", "1", *flags)
                self.assert_refused(result, because="quorum one has no override")
                self.assertEqual(result.called, [])
                self.assertIn("--allow-single-reviewer", result.stderr)

    def test_single_reviewer_override_is_visible_and_persisted(self):
        for rendering in ([], ["--json"]):
            with self.subTest(rendering=rendering):
                result = self.cycle_cli(
                    "--only", "a", "--quorum", "1",
                    "--allow-single-reviewer", "owner requested diagnostic",
                    *rendering)
                self.assertEqual(result.returncode, review.STATES["REVIEW_PASS"])
                policy = self.cycle_records()[-1]["panel_policy"]
                self.assertEqual(policy["single_reviewer_override"],
                                 "owner requested diagnostic")
                if rendering:
                    self.assertEqual(json.loads(result.stdout)["panel_policy"],
                                     policy)
                else:
                    self.assertIn("REVIEW_PASS — SINGLE_REVIEWER_OVERRIDE: "
                                  "owner requested diagnostic", result.stdout)

    def test_override_requires_a_reason_and_quorum_one(self):
        for reason, quorum in (("", "1"), ("  ", "1"), ("a\nb", "1"),
                               ("a\u2028b", "1"), ("diagnostic", "2")):
            with self.subTest(reason=reason, quorum=quorum):
                self.assert_refused(self.cycle_cli(
                    "--quorum", quorum, "--allow-single-reviewer", reason),
                    because="override is not an explicit singleton reason")

    def test_fresh_cycle_checks_selected_panel_against_replaced_profile(self):
        for flags in (["--profile", "fast"], ["--only", "a,b"],
                      ["--only", "a,a,b"]):
            with self.subTest(flags=flags):
                result = self.cycle_cli("--fresh-cycle-from", "standard", *flags)
                self.assert_refused(result, because="replacement shrank the panel")
                self.assertEqual(result.called, [])
        result = self.cycle_cli("--fresh-cycle-from", "deep")
        self.assert_refused(result, because="standard is smaller than deep")
        self.assertIn("at least 4", result.stderr)

    def test_fresh_cycle_cannot_count_disabled_selected_reviewers(self):
        roster = [{"name": n, "profiles": ["standard"]} for n in ("a", "b", "c")]
        roster.append({"name": "d", "profiles": [], "enabled": False})
        result = self.cycle_cli("--fresh-cycle-from", "standard",
                                "--only", "a,b,d", roster=roster)
        self.assert_refused(result, because="disabled reviewer cannot fill floor")
        self.assertEqual(result.called, [])

    def test_fresh_cycle_cannot_count_duplicate_routing_entries(self):
        roster = [{"name": n, "profiles": ["fast", "standard"]}
                  for n in ("a", "a", "b", "c")]
        for flags in ([], ["--escalate"]):
            with self.subTest(flags=flags):
                result = self.cycle_cli(
                    "--fresh-cycle-from", "standard", "--json", *flags,
                    roster=roster, unavailable=("c",))
                self.assert_refused(result, because="two names would fill quorum 3")
                self.assertIn("duplicate reviewer names", result.stderr)
                self.assertEqual(result.called, [])

    def test_fresh_cycle_raises_quorum_for_fixed_panel_and_ladder(self):
        for flags, absent in (([], ("c",)), (["--escalate"], ("c", "d"))):
            with self.subTest(flags=flags):
                result = self.cycle_cli("--fresh-cycle-from", "standard",
                                        "--json", *flags, unavailable=absent)
                self.assertEqual(result.returncode, review.STATES["REVIEW_PARTIAL"])
                report = json.loads(result.stdout)
                self.assertEqual(report["quorum"], 3)
                self.assertEqual(report["completed"], 2)

    def test_fresh_cycle_ladder_does_not_stop_below_replacement_floor(self):
        result = self.cycle_cli("--fresh-cycle-from", "standard", "--escalate",
                                "--json", failing=True)
        self.assertEqual(result.returncode, review.STATES["REVIEW_CLAIMS_REFUTED"])
        self.assertEqual(set(result.called), {"a", "b", "c"})
        self.assertEqual(json.loads(result.stdout)["quorum"], 3)

    def test_fresh_cycle_provenance_reaches_verdict_and_journal(self):
        for rendering in ([], ["--json"]):
            with self.subTest(rendering=rendering):
                result = self.cycle_cli("--fresh-cycle-from", "standard", *rendering)
                self.assertEqual(result.returncode, review.STATES["REVIEW_PASS"])
                fresh = self.cycle_records()[-1]["panel_policy"]["fresh_cycle"]
                self.assertEqual(fresh["replaces_profile"], "standard")
                self.assertEqual(fresh["minimum_reviewers"], 3)
                self.assertEqual(fresh["profile_reviewers"], ["a", "b", "c"])
                self.assertIn("caller-declared", fresh["provenance"])
                if rendering:
                    self.assertEqual(json.loads(result.stdout)["panel_policy"]
                                     ["fresh_cycle"], fresh)
                else:
                    self.assertIn("REVIEW_PASS — FRESH_CYCLE from standard; "
                                  "minimum 3 reviewers", result.stdout)

    def test_unavailable_fresh_cycle_keeps_provenance(self):
        result = self.cycle_cli("--fresh-cycle-from", "standard",
                                unavailable=("a", "b", "c"))
        self.assertEqual(result.returncode, review.STATES["REVIEW_UNAVAILABLE"])
        self.assertIn("REVIEW_UNAVAILABLE — FRESH_CYCLE from standard", result.stdout)

    def test_implementation_flags_do_not_relax_a_plan_panel(self):
        for flags in (["--fresh-cycle-from", "standard"],
                      ["--quorum", "1", "--allow-single-reviewer", "diagnostic"]):
            self.assert_refused(self.run_cli("--kind", "plan", *flags),
                                because="implementation exception used for a plan")

    def test_the_two_plan_reviewers_cannot_share_a_provider(self):
        by_prov = {}
        for r in review.load_reviewers():
            if r.get("enabled", True):
                by_prov.setdefault(r["provider"], []).append(r["name"])
        pair = next((v[:2] for v in by_prov.values() if len(v) >= 2), None)
        if not pair:
            self.skipTest("no two enabled reviewers share a provider")
        self.assert_refused(
            self.run_cli("--kind", "plan", "--only", ",".join(pair),
                         "--quorum", "2"),
            because="both plan reviewers use the same provider")

    def test_an_undeclared_reviewer_joins_no_profile(self):
        """A reviewer with no `profiles` key was in EVERY profile, so adding
        one silently put a third model on the two-model plan panel."""
        src = SCRIPT.read_text()
        self.assertNotIn('not r.get("profiles") or profile in r["profiles"]',
                         src,
                         "undeclared reviewers are still in every profile")

    def test_the_configured_plan_panel_is_two_contrasting_models(self):
        panel = [r for r in review.load_reviewers()
                 if "plan" in (r.get("profiles") or [])
                 and r.get("enabled", True)]
        self.assertEqual(len(panel), 2, f"plan panel is {len(panel)}, not 2")
        self.assertEqual(len({r["provider"] for r in panel}), 2,
                         "the plan panel shares a provider")

    def test_every_config_error_names_an_action(self):
        import ast
        tree = ast.parse(SCRIPT.read_text())
        actionable = ("Drop ", "drop ", "Pass ", "pass ", "remove ", "Step",
                      "Override", "must be", "Available:", "Name one",
                      "name exactly")
        checked = 0
        for node in ast.walk(tree):
            f = getattr(node, "func", None)
            if not (isinstance(node, ast.Call) and isinstance(f, ast.Name)
                    and f.id == "config_error"):
                continue
            text = " ".join(n.value for n in ast.walk(node)
                            if isinstance(n, ast.Constant)
                            and isinstance(n.value, str))
            if len(text.strip()) < 30:
                continue
            checked += 1
            self.assertTrue(any(a in text for a in actionable),
                            f"a config_error names no action: {text[:130]}")
        self.assertGreater(checked, 6, "too few config_error messages "
                                       "recovered to be measuring anything")


class TestFindingDispositions(unittest.TestCase):
    def write_dispositions(self, data):
        path = Path(tempfile.mkdtemp()) / "dispositions.json"
        path.write_text(json.dumps(data))
        return path

    def entry(self, disposition="not-reproduced"):
        location = "skills/hanig-review-gate/scripts/review.py:1170"
        summary = "the original finding text survives into the next round"
        digest = review.finding_digest(location, summary)
        return digest, {
            "location": location,
            "summary": summary,
            "disposition": disposition,
            "reason": "the named branch rejects this input before dispatch",
        }

    def test_not_reproduced_finding_is_injected_verbatim(self):
        digest, entry = self.entry()
        dispositions = review.load_dispositions(
            self.write_dispositions({digest: entry}))
        prompt = review.build_prompt(
            "code", [review.HONEST_RUN_CLAIM], False, "context",
            dispositions=dispositions)
        self.assertIn(entry["summary"], prompt)
        self.assertIn(entry["reason"], prompt)

    def test_cli_wires_not_reproduced_finding_into_reviewer_prompt(self):
        digest, entry = self.entry()
        path = self.write_dispositions({digest: entry})
        captured = []
        original = {
            "argv": sys.argv,
            "load_reviewers": review.load_reviewers,
            "availability": review.availability,
            "run_one": review.run_one,
            "arm_watchdog": review.arm_watchdog,
        }
        reviewer = {"name": "offline", "provider": "offline", "model": "m",
                    "profiles": ["standard"], "enabled": True}
        try:
            sys.argv = [str(SCRIPT), "--file", str(SCRIPT),
                        "--kind", "implementation", "--round", "2",
                        "--claim", review.HONEST_RUN_CLAIM,
                        "--dispositions", str(path), "--quorum", "1",
                        "--allow-single-reviewer", "offline singleton fixture",
                        "--json"]
            review.load_reviewers = lambda: [reviewer]
            review.availability = lambda _reviewer: None
            review.arm_watchdog = lambda _seconds: None

            def run_one(_reviewer, prompt, *_args, **_kwargs):
                captured.append(prompt)
                return {"name": "offline", "ok": True, "elapsed_s": 0,
                        "model": "m", "effort": None, "in_tokens": 0,
                        "out_tokens": 0, "verdict": "upheld",
                        "findings": [], "claims": [], "notes": ""}

            review.run_one = run_one
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    review.main()
            self.assertEqual(raised.exception.code,
                             review.STATES["REVIEW_PASS"])
        finally:
            sys.argv = original["argv"]
            review.load_reviewers = original["load_reviewers"]
            review.availability = original["availability"]
            review.run_one = original["run_one"]
            review.arm_watchdog = original["arm_watchdog"]
        self.assertEqual(len(captured), 1)
        self.assertIn(entry["summary"], captured[0])

    def test_reproduced_finding_is_not_injected_as_a_disagreement(self):
        digest, entry = self.entry("reproduced")
        dispositions = review.load_dispositions(
            self.write_dispositions({digest: entry}))
        prompt = review.build_prompt(
            "code", [review.HONEST_RUN_CLAIM], False, "context",
            dispositions=dispositions)
        self.assertNotIn(entry["summary"], prompt)

    def test_digest_must_match_location_and_summary(self):
        _digest, entry = self.entry()
        with self.assertRaises(SystemExit) as raised:
            review.load_dispositions(self.write_dispositions({"0" * 64: entry}))
        self.assertEqual(raised.exception.code, review.STATES["REVIEW_ERROR"])

    def test_duplicate_digest_keys_are_rejected(self):
        digest, entry = self.entry()
        first = json.dumps({digest: entry})[1:-1]
        entry["disposition"] = "reproduced"
        second = json.dumps({digest: entry})[1:-1]
        path = Path(tempfile.mkdtemp()) / "duplicate-dispositions.json"
        path.write_text("{" + first + "," + second + "}")
        with self.assertRaises(SystemExit) as raised:
            review.load_dispositions(path)
        self.assertEqual(raised.exception.code, review.STATES["REVIEW_ERROR"])

    def test_lone_surrogate_is_review_error_not_a_traceback(self):
        _digest, entry = self.entry()
        entry["summary"] = "invalid lone surrogate: \ud800"
        path = self.write_dispositions({"0" * 64: entry})
        with self.assertRaises(SystemExit) as raised:
            review.load_dispositions(path)
        self.assertEqual(raised.exception.code, review.STATES["REVIEW_ERROR"])

    def test_lone_surrogate_in_reason_is_review_error(self):
        digest, entry = self.entry()
        entry["reason"] = "invalid lone surrogate: \ud800"
        path = self.write_dispositions({digest: entry})
        with self.assertRaises(SystemExit) as raised:
            review.load_dispositions(path)
        self.assertEqual(raised.exception.code, review.STATES["REVIEW_ERROR"])

    def test_reason_must_be_one_line(self):
        digest, entry = self.entry()
        entry["reason"] = "first line\nsecond line"
        with self.assertRaises(SystemExit) as raised:
            review.load_dispositions(self.write_dispositions({digest: entry}))
        self.assertEqual(raised.exception.code, review.STATES["REVIEW_ERROR"])

    def test_reason_rejects_unicode_line_separators(self):
        for separator in ("\v", "\f", "\x1c", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                digest, entry = self.entry()
                entry["reason"] = "first line" + separator + "second line"
                with self.assertRaises(SystemExit) as raised:
                    review.load_dispositions(
                        self.write_dispositions({digest: entry}))
                self.assertEqual(raised.exception.code,
                                 review.STATES["REVIEW_ERROR"])


class TestReviewSuiteJournalIsolation(unittest.TestCase):
    """Journal-capable tests cannot leak audit records into operator state."""

    # Exercise the lazy record wrapper first, before tests that explicitly
    # acquire the module fixture. These cover review.main -> journal child,
    # repeated child writes, and direct append, with no live providers.
    JOURNAL_WRITING_TESTS = (
        "tests.test_review.TestFindingDispositions."
        "test_cli_wires_not_reproduced_finding_into_reviewer_prompt",
        "tests.test_review.TestReviewJournal."
        "test_two_invocations_append_two_records_with_monotonic_timestamps",
        "tests.test_review.TestReviewSuiteJournalIsolation."
        "test_fixture_allows_a_real_isolated_append",
    )

    def test_unrelated_selected_test_needs_no_same_device_journal_root(self):
        program = "\n".join((
            "import importlib, os, sys, unittest",
            "before = (os.environ.get('XDG_STATE_HOME'), os.environ.get('HANIG_REVIEW_GATE_TESTING'))",
            "module = importlib.import_module('tests.test_review')",
            "if module._MODULE_STATE_HOME is not None:",
            "    raise SystemExit('import acquired journal storage')",
            "if before != (os.environ.get('XDG_STATE_HOME'), os.environ.get('HANIG_REVIEW_GATE_TESTING')):",
            "    raise SystemExit('import changed journal environment')",
            "def cross_device(_worktrees):",
            "    raise RuntimeError('repository and temporary roots are on different devices')",
            "module._new_module_state_home = cross_device",
            "suite = unittest.defaultTestLoader.loadTestsFromName(",
            "    'tests.test_review.TestVerdictSchema.test_empty_object_is_not_a_review')",
            "result = unittest.TextTestRunner(verbosity=0).run(suite)",
            "sys.exit(0 if result.wasSuccessful() else 1)",
        ))
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=REPO,
            capture_output=True, text=True, timeout=60)
        self.assertEqual(
            result.returncode, 0,
            "an unrelated selected test acquired the simulated cross-device "
            "journal fixture at module setup:\n%s%s" %
            (result.stdout, result.stderr))

    def test_module_fixture_can_be_reacquired_in_same_interpreter(self):
        selected = (
            "tests.test_review.TestReviewSuiteJournalIsolation."
            "test_fixture_allows_a_real_isolated_append")
        program = "\n".join((
            "import os, sys, unittest",
            "import tests.test_review as module",
            "before = (os.environ.get('XDG_STATE_HOME'), os.environ.get(module.review.JOURNAL_TEST_MARKER))",
            "original_new = module._new_module_state_home",
            "created = []",
            "def traced(worktrees):",
            "    temporary = original_new(worktrees)",
            "    created.append(temporary.name)",
            "    return temporary",
            "module._new_module_state_home = traced",
            "ok = True",
            "for _attempt in (1, 2):",
            "    suite = unittest.defaultTestLoader.loadTestsFromName(%r)" %
            selected,
            "    result = unittest.TextTestRunner(verbosity=0).run(suite)",
            "    ok = ok and result.wasSuccessful()",
            "    ok = ok and module.review.append_review_journal is module._ORIGINAL_APPEND_REVIEW_JOURNAL",
            "    ok = ok and module.review.record_review_round is module._ORIGINAL_RECORD_REVIEW_ROUND",
            "    ok = ok and module._MODULE_STATE_HOME is None and module._SAVED_MODULE_ENV is None",
            "    ok = ok and before == (os.environ.get('XDG_STATE_HOME'), os.environ.get(module.review.JOURNAL_TEST_MARKER))",
            "    ok = ok and all(not os.path.exists(path) for path in created)",
            "ok = ok and len(created) == 2 and created[0] != created[1]",
            "sys.exit(0 if ok else 1)",
        ))
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=REPO,
            capture_output=True, text=True, timeout=60)
        self.assertEqual(
            result.returncode, 0,
            "a second suite run reused the cleaned module fixture:\n%s%s" %
            (result.stdout, result.stderr))

    def test_fixture_does_not_read_tempdir_after_first_candidate_succeeds(self):
        fixture_root = _ensure_module_state_home()
        original_gettempdir = tempfile.gettempdir
        original_temporary_directory = tempfile.TemporaryDirectory
        calls = []

        def unavailable():
            raise OSError("default temp directory unavailable")

        def successful_first_candidate(*args, **kwargs):
            calls.append(Path(kwargs["dir"]))
            kwargs["dir"] = str(fixture_root)
            return original_temporary_directory(*args, **kwargs)

        tempfile.gettempdir = unavailable
        tempfile.TemporaryDirectory = successful_first_candidate
        temporary = None
        try:
            # Inject eligibility for this simulated topology; attached host
            # worktrees must not change which candidates the test exercises.
            temporary = _new_module_state_home([REPO])
            root = Path(temporary.name).resolve()
            self.assertEqual(calls, [REPO.parent])
            self.assertTrue(review._inside(root, fixture_root))
        finally:
            tempfile.gettempdir = original_gettempdir
            tempfile.TemporaryDirectory = original_temporary_directory
            if temporary is not None:
                temporary.cleanup()

    def test_fixture_uses_home_when_parent_unwritable_and_temp_is_worktree(self):
        fixture_root = _ensure_module_state_home()
        original_gettempdir = tempfile.gettempdir
        original_temporary_directory = tempfile.TemporaryDirectory
        saved_home = os.environ.get("HOME")
        home_candidate = fixture_root
        calls = []

        def temp_inside_worktree():
            return str(REPO)

        def simulated_topology(*args, **kwargs):
            candidate = Path(kwargs["dir"]).resolve()
            calls.append(candidate)
            if candidate == REPO.parent:
                raise PermissionError("repository parent is not writable")
            if candidate == home_candidate:
                kwargs["dir"] = str(fixture_root)
            return original_temporary_directory(*args, **kwargs)

        os.environ["HOME"] = str(home_candidate)
        tempfile.gettempdir = temp_inside_worktree
        tempfile.TemporaryDirectory = simulated_topology
        temporary = None
        try:
            # Inject eligibility for this simulated topology; attached host
            # worktrees must not change which candidates the test exercises.
            temporary = _new_module_state_home([REPO])
            root = Path(temporary.name).resolve()
            self.assertEqual(calls, [REPO.parent, home_candidate])
            self.assertTrue(review._inside(root, fixture_root))
        finally:
            tempfile.gettempdir = original_gettempdir
            tempfile.TemporaryDirectory = original_temporary_directory
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
            if temporary is not None:
                temporary.cleanup()

    def test_home_lookup_failure_falls_through_to_default_temp(self):
        fixture_root = _ensure_module_state_home()
        original_home = globals()["_module_home"]
        original_gettempdir = tempfile.gettempdir
        original_temporary_directory = tempfile.TemporaryDirectory
        calls = []

        def unavailable_home():
            raise RuntimeError("home lookup unavailable")

        def simulated_topology(*args, **kwargs):
            candidate = Path(kwargs["dir"]).resolve()
            calls.append(candidate)
            if candidate == REPO.parent:
                raise PermissionError("repository parent is not writable")
            kwargs["dir"] = str(fixture_root)
            return original_temporary_directory(*args, **kwargs)

        globals()["_module_home"] = unavailable_home
        tempfile.gettempdir = lambda: str(fixture_root)
        tempfile.TemporaryDirectory = simulated_topology
        temporary = None
        try:
            # Inject eligibility for this simulated topology; attached host
            # worktrees must not change which candidates the test exercises.
            temporary = _new_module_state_home([REPO])
            self.assertEqual(calls, [REPO.parent, fixture_root])
        finally:
            globals()["_module_home"] = original_home
            tempfile.gettempdir = original_gettempdir
            tempfile.TemporaryDirectory = original_temporary_directory
            if temporary is not None:
                temporary.cleanup()

    def test_git_free_root_check_rejects_git_entries_and_probe_errors(self):
        fixture_root = _ensure_module_state_home()
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                parent = fixture_root / ("unrelated-" + kind)
                candidate = parent / "candidate"
                candidate.mkdir(parents=True)
                marker = parent / ".git"
                if kind == "directory":
                    marker.mkdir()
                elif kind == "file":
                    marker.write_text("gitdir: elsewhere\n")
                else:
                    marker.symlink_to(parent / "missing")
                with self.assertRaisesRegex(OSError, "Git worktree marker"):
                    review._require_git_free_test_root(candidate)

        original_lstat = review.Path.lstat

        def broken_lstat(path):
            if path.name == ".git":
                raise OSError(errno.EIO, "injected marker probe failure")
            return original_lstat(path)

        review.Path.lstat = broken_lstat
        try:
            with self.assertRaisesRegex(OSError, "cannot establish"):
                review._require_git_free_test_root(fixture_root / "candidate")
        finally:
            review.Path.lstat = original_lstat

    def test_fixture_skips_candidate_below_unrelated_git_marker(self):
        fixture_root = _ensure_module_state_home()
        unrelated = fixture_root / "unrelated-repository"
        unrelated.mkdir()
        (unrelated / ".git").mkdir()
        original_home = globals()["_module_home"]
        original_gettempdir = tempfile.gettempdir
        original_temporary_directory = tempfile.TemporaryDirectory
        calls = []

        def simulated_topology(*args, **kwargs):
            candidate = Path(kwargs["dir"]).resolve()
            calls.append(candidate)
            if candidate == REPO.parent:
                raise PermissionError("repository parent is not writable")
            return original_temporary_directory(*args, **kwargs)

        globals()["_module_home"] = lambda: unrelated
        tempfile.gettempdir = lambda: str(fixture_root)
        tempfile.TemporaryDirectory = simulated_topology
        temporary = None
        try:
            # Inject eligibility for this simulated topology; attached host
            # worktrees must not change which candidates the test exercises.
            temporary = _new_module_state_home([REPO])
            root = Path(temporary.name).resolve()
            self.assertEqual(calls, [REPO.parent, unrelated, fixture_root])
            self.assertTrue(review._inside(root, fixture_root))
            self.assertFalse(review._inside(root, unrelated))
        finally:
            globals()["_module_home"] = original_home
            tempfile.gettempdir = original_gettempdir
            tempfile.TemporaryDirectory = original_temporary_directory
            if temporary is not None:
                temporary.cleanup()

    def test_module_suite_leaves_the_user_journal_untouched(self):
        fixture_root = _ensure_module_state_home()
        for mode in ("default", "custom-xdg", "fallback"):
            with self.subTest(mode=mode):
                root = Path(tempfile.mkdtemp(dir=fixture_root)).resolve()
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                home = root / "home"
                temp_root = root / "tmp"
                home.mkdir()
                temp_root.mkdir()
                custom_state = root / "custom-state"
                candidates = [
                    home / ".local" / "state" / review.JOURNAL_DIR,
                    custom_state / review.JOURNAL_DIR,
                    temp_root / "hanig-review-gate-state" /
                    review.JOURNAL_DIR,
                ]
                env = dict(os.environ)
                env["TMPDIR"] = str(temp_root)
                if mode == "fallback":
                    env["HOME"] = str(REPO)
                    env["XDG_STATE_HOME"] = str(REPO / ".guard-state")
                    candidates = [candidates[-1]]
                elif mode == "custom-xdg":
                    env["HOME"] = str(home)
                    env["XDG_STATE_HOME"] = str(custom_state)
                else:
                    env["HOME"] = str(home)
                    env.pop("XDG_STATE_HOME", None)
                env.pop(review.JOURNAL_TEST_MARKER, None)
                env.pop("OPENAI_API_KEY", None)
                env.pop("OPENROUTER_API_KEY", None)

                for index, candidate in enumerate(candidates):
                    seed = candidate / ("seed-%d" % index) / "record.jsonl"
                    seed.parent.mkdir(parents=True)
                    seed.write_bytes(("operator history %s %d\n" %
                                      (mode, index)).encode("utf-8"))

                def snapshot(path):
                    entries = []
                    for item in sorted(path.rglob("*"), key=str):
                        relative = str(item.relative_to(path))
                        entries.append((relative,
                                        None if item.is_dir()
                                        else item.read_bytes()))
                    return entries

                before = {path: snapshot(path) for path in candidates}

                result = subprocess.run(
                    [sys.executable, "-m", "unittest",
                     *self.JOURNAL_WRITING_TESTS],
                    cwd=REPO, env=env, capture_output=True, text=True,
                    # A hang bound for three fixed writers, independent of
                    # the growing module's runtime. TimeoutExpired is an
                    # error, never evidence that the journal stayed intact.
                    timeout=600)

                self.assertEqual(
                    {path: snapshot(path) for path in candidates}, before,
                    "the review test suite changed seeded journal state "
                    "outside its module fixture")
                self.assertEqual(
                    result.returncode, 0,
                    result.stdout[-2000:] + result.stderr[-2000:])

    def test_nested_journal_timeout_is_not_a_pass(self):
        _ensure_module_state_home()
        case = type(self)("test_module_suite_leaves_the_user_journal_untouched")
        result = unittest.TestResult()
        with patch.object(subprocess, "run", side_effect=
                          subprocess.TimeoutExpired("journal writers", 600)):
            case.run(result)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 3)
        for _case, error in result.errors:
            self.assertIn("TimeoutExpired", error)

    def test_fixture_allows_a_real_isolated_append(self):
        fixture_root = _ensure_module_state_home()
        path = (Path(os.environ["XDG_STATE_HOME"]) / review.JOURNAL_DIR /
                review.JOURNAL_NAME)
        _record, record_path = review.append_review_journal(
            path, "implementation", 1, ["offline"], "REVIEW_PASS",
            [review.HONEST_RUN_CLAIM])
        self.assertTrue(record_path.is_file())
        self.assertTrue(review._inside(record_path, fixture_root))

    def test_marker_absent_keeps_production_append_behavior(self):
        fixture_root = _ensure_module_state_home()
        path = fixture_root / "marker-absent" / review.JOURNAL_NAME
        saved = os.environ.pop(review.JOURNAL_TEST_MARKER)
        try:
            _record, record_path = _ORIGINAL_APPEND_REVIEW_JOURNAL(
                path, "implementation", 1, ["offline"], "REVIEW_PASS",
                [review.HONEST_RUN_CLAIM])
            self.assertTrue(record_path.is_file())
            self.assertNotIn(review.JOURNAL_TEST_MARKER, os.environ)
        finally:
            os.environ[review.JOURNAL_TEST_MARKER] = saved

    def test_test_marker_refuses_state_homes_outside_the_fixture(self):
        fixture_root = _ensure_module_state_home()
        root = Path(tempfile.mkdtemp(dir=fixture_root.parent)).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        saved_home = os.environ.get("HOME")
        saved_xdg = os.environ.get("XDG_STATE_HOME")
        try:
            os.environ["HOME"] = str(root / "home")
            os.environ["XDG_STATE_HOME"] = str(root / "custom-state")
            paths = [
                Path.home() / ".local" / "state" / review.JOURNAL_DIR /
                review.JOURNAL_NAME,
                Path(os.environ["XDG_STATE_HOME"]) / review.JOURNAL_DIR /
                review.JOURNAL_NAME,
            ]
            for path in paths:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(
                            OSError, "test-marked review journal"):
                        review.append_review_journal(
                            path, "implementation", 1, ["offline"],
                            "REVIEW_PASS", [review.HONEST_RUN_CLAIM])
                    self.assertFalse(path.exists())
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
            if saved_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = saved_xdg

    def test_test_marker_refuses_unconstrained_roots(self):
        fixture_root = _ensure_module_state_home()
        regular_file = fixture_root / "not-a-directory"
        regular_file.write_text("not a directory\n")
        symlink = fixture_root.parent / (fixture_root.name + "-link")
        self.addCleanup(symlink.unlink, missing_ok=True)
        symlink.symlink_to(fixture_root, target_is_directory=True)
        saved = os.environ[review.JOURNAL_TEST_MARKER]
        try:
            for marker in ("", "relative", "/", str(regular_file),
                           str(symlink), str(fixture_root / "missing"),
                           str(fixture_root / ".." / fixture_root.name),
                           str(fixture_root) + "/."):
                with self.subTest(marker=marker):
                    os.environ[review.JOURNAL_TEST_MARKER] = marker
                    with self.assertRaises(OSError):
                        review.append_review_journal(
                            fixture_root / "state" / review.JOURNAL_DIR /
                            review.JOURNAL_NAME,
                            "implementation", 1, ["offline"],
                            "REVIEW_PASS", [review.HONEST_RUN_CLAIM])
        finally:
            os.environ[review.JOURNAL_TEST_MARKER] = saved

    def test_test_marker_refuses_fixture_root_inside_worktree(self):
        fixture_root = _ensure_module_state_home()
        repository = fixture_root / "marker-refusal-repository"
        self.addCleanup(shutil.rmtree, repository, ignore_errors=True)
        subprocess.run(["git", "init", "-q", str(repository)],
                       check=True, capture_output=True)
        inside = repository / (review.JOURNAL_TEST_ROOT_PREFIX + fixture_root.name)
        inside.mkdir()
        path = inside / "state" / review.JOURNAL_DIR / review.JOURNAL_NAME
        saved = os.environ[review.JOURNAL_TEST_MARKER]
        try:
            os.environ[review.JOURNAL_TEST_MARKER] = str(inside)
            with self.assertRaisesRegex(OSError, "Git worktree marker"):
                review.append_review_journal(
                    path, "implementation", 1, ["offline"],
                    "REVIEW_PASS", [review.HONEST_RUN_CLAIM])
            self.assertFalse(path.exists())
        finally:
            os.environ[review.JOURNAL_TEST_MARKER] = saved

    def test_test_marker_refuses_tilde_relative_fixture_root(self):
        fixture_root = _ensure_module_state_home()
        path = (fixture_root / "state" / review.JOURNAL_DIR /
                review.JOURNAL_NAME)
        saved_marker = os.environ[review.JOURNAL_TEST_MARKER]
        saved_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(fixture_root.parent)
            os.environ[review.JOURNAL_TEST_MARKER] = "~/" + fixture_root.name
            with self.assertRaisesRegex(OSError, "must name the absolute"):
                review.append_review_journal(
                    path, "implementation", 1, ["offline"],
                    "REVIEW_PASS", [review.HONEST_RUN_CLAIM])
        finally:
            os.environ[review.JOURNAL_TEST_MARKER] = saved_marker
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home

    def test_hard_link_fixture_skips_when_filesystem_policy_forbids_link(self):
        original_link = os.link
        try:
            for error in (errno.EACCES, errno.EPERM):
                with self.subTest(error=error):
                    def denied(*_args, **_kwargs):
                        raise OSError(
                            error, "hard links disabled by filesystem policy")

                    os.link = denied
                    with self.assertRaises(unittest.SkipTest):
                        _hard_link_or_skip(self, "source", "destination")
        finally:
            os.link = original_link

    def test_hard_link_fixture_does_not_hide_unexpected_io_errors(self):
        original_link = os.link

        def broken(*_args, **_kwargs):
            raise OSError(errno.EIO, "injected I/O failure")

        os.link = broken
        try:
            with self.assertRaisesRegex(OSError, "injected I/O failure"):
                _hard_link_or_skip(self, "source", "destination")
        finally:
            os.link = original_link


class TestQuorumGatesFailAsWellAsPass(unittest.TestCase):
    """Found by using the gate: a plan review returned REVIEW_FAIL on ONE
    verdict, because luna returned an empty response and the finding check sat
    above the quorum check. Quorum was required for a pass and not for a fail.

    The same asymmetry had already been fixed in escalate(); this is the
    sibling site, missed. The finding still prints and REVIEW_PARTIAL is still
    not a pass -- what changes is that one opinion is not a committee."""

    def test_a_finding_below_quorum_is_partial_not_fail(self):
        st = review.decide_state(
            n_completed=1, n_failed=1, confirmed=[{"severity": "major"}],
            refuted_claims=[], rejecting=False, truncated=False, quorum=2)
        self.assertEqual(st, "REVIEW_PARTIAL",
                         "one reviewer's finding was returned as a committee "
                         "verdict")

    def test_a_refuted_claim_below_quorum_is_partial_not_fail(self):
        st = review.decide_state(
            n_completed=1, n_failed=0, confirmed=[],
            refuted_claims=["c"], rejecting=False, truncated=False, quorum=2)
        self.assertEqual(st, "REVIEW_PARTIAL")

    def test_a_finding_at_quorum_still_fails(self):
        """The counter-claim: this must not stop the gate from failing when the
        panel actually did reach quorum."""
        st = review.decide_state(
            n_completed=2, n_failed=0, confirmed=[{"severity": "major"}],
            refuted_claims=[], rejecting=False, truncated=False, quorum=2)
        self.assertEqual(st, "REVIEW_FAIL")

    def test_no_verdicts_is_still_unavailable(self):
        st = review.decide_state(
            n_completed=0, n_failed=2, confirmed=[{"severity": "critical"}],
            refuted_claims=[], rejecting=False, truncated=False, quorum=2)
        self.assertEqual(st, "REVIEW_UNAVAILABLE")


class TestBothProviderPathsShareTheirLimits(unittest.TestCase):
    """call_openai kept a hardcoded 16000 output budget after call_openrouter
    was raised to 64000, and reviewers.json's comment already claimed both used
    the shared default. luna then burned its whole budget on reasoning for a
    53KB document and returned an empty response twice, costing a plan review
    its quorum. One rule, two call paths, applied to one."""

    def test_neither_path_hardcodes_an_output_budget(self):
        import ast
        src = SCRIPT.read_text()
        tree = ast.parse(src)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if not fn.name.startswith("call_"):
                continue
            for node in ast.walk(fn):
                # rev.get("max_output_tokens", <literal>) must fall back to the
                # shared constant, never to a number written in place.
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not (isinstance(f, ast.Attribute) and f.attr == "get"):
                    continue
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                if node.args[0].value != "max_output_tokens":
                    continue
                self.assertGreater(len(node.args), 1,
                                   f"{fn.name}: no default given")
                dflt = node.args[1]
                self.assertNotIsInstance(
                    dflt, ast.Constant,
                    f"{fn.name} hardcodes an output budget; use "
                    f"DEFAULT_MAX_OUTPUT_TOKENS so both paths move together")

    def test_the_documented_default_matches_the_code(self):
        """reviewers.json's comment states the number; a comment that lies
        about the code is how this survived."""
        import json
        cfg = json.loads(
            (REPO / "skills" / "hanig-review-gate" / "reviewers.json").read_text())
        comment = cfg.get("_comment", "")
        self.assertIn(str(review.DEFAULT_MAX_OUTPUT_TOKENS), comment,
                     "reviewers.json documents a different default than the "
                     "code uses")


class TestReviewJournal(unittest.TestCase):
    CLAIM = "This change cannot make an honest run fail."

    def setUp(self):
        fixture_root = _ensure_module_state_home()
        # macOS commonly returns a lexical /var/... temporary path even
        # though /var is a symlink to /private/var.  These success-path tests
        # must not accidentally exercise the deliberate symlink refusal in
        # _open_directory_chain; the dedicated symlink tests below construct
        # the component whose refusal they assert.
        self.tmp = Path(tempfile.mkdtemp(dir=fixture_root)).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.saved = {
            "argv": sys.argv,
            "load_reviewers": review.load_reviewers,
            "availability": review.availability,
            "run_one": review.run_one,
            "gather": review.gather,
            "arm_watchdog": review.arm_watchdog,
            "disarm_watchdog": review.disarm_watchdog,
            "record_review_round": review.record_review_round,
            "time_ns": review.time.time_ns,
            "xdg": os.environ.get("XDG_STATE_HOME"),
        }
        self.addCleanup(self.restore)
        os.environ["XDG_STATE_HOME"] = str(self.tmp / "state")
        review.load_reviewers = lambda: [
            {"name": "answered", "provider": "stub", "model": "stub",
             "profiles": ["standard"], "enabled": True},
            {"name": "errored", "provider": "stub", "model": "stub-2",
             "profiles": ["standard"], "enabled": True},
        ]
        review.availability = lambda _reviewer: None
        review.gather = lambda _args: ("diff body", "test diff")
        review.arm_watchdog = lambda _seconds: None
        review.run_one = self.answer

    def restore(self):
        sys.argv = self.saved["argv"]
        review.load_reviewers = self.saved["load_reviewers"]
        review.availability = self.saved["availability"]
        review.run_one = self.saved["run_one"]
        review.gather = self.saved["gather"]
        review.arm_watchdog = self.saved["arm_watchdog"]
        review.disarm_watchdog = self.saved["disarm_watchdog"]
        review.record_review_round = self.saved["record_review_round"]
        review.time.time_ns = self.saved["time_ns"]
        if self.saved["xdg"] is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.saved["xdg"]

    def answer(self, reviewer, *_args, **_kwargs):
        return {
            "ok": True, "name": reviewer["name"], "verdict": "upheld",
            "findings": [],
            "claims": [{"claim_index": 0, "claim": self.CLAIM,
                        "status": "supported",
                        "why": "the stub supplies a complete claim assessment"}],
            "notes": "", "elapsed_s": 0, "in_tokens": 1,
            "out_tokens": 1,
        }

    def invoke(self, only="answered", extra_args=()):
        sys.argv = [str(SCRIPT), "--kind", "implementation", "--round", "1",
                    "--profile", "standard", "--quorum", "1",
                    "--allow-single-reviewer", "offline singleton fixture",
                    "--claim", self.CLAIM, "--file", str(SCRIPT), "--json"]
        if only is not None:
            sys.argv.extend(["--only", only])
        sys.argv.extend(extra_args)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                review.main()
        return stopped.exception.code, stdout.getvalue(), stderr.getvalue()

    def journal_path(self):
        return (Path(os.environ["XDG_STATE_HOME"]) / review.JOURNAL_DIR /
                review.JOURNAL_NAME)

    def records(self):
        return [json.loads(path.read_text())
                for path in sorted(self.journal_path().glob("*/record.jsonl"))]

    def seed_record(self, name, record, filename="record.jsonl"):
        event = self.journal_path() / name
        event.mkdir(parents=True)
        path = event / filename
        path.write_text(json.dumps(record) + "\n")
        return path

    def test_finding_location_and_summary_are_readable_and_redacted(self):
        secret = 'sk-"journal\\secret\nvalue'
        finding = {
            "file": "src/" + secret + ".py", "line": 17,
            "severity": "minor", "confidence": "high",
            "summary": "reported " + secret + "\nsecond line",
            "failure_scenario": "a diagnostic includes " + secret,
        }
        result = self.answer({"name": "answered"})
        result["findings"] = [finding]
        review.run_one = lambda *_args, **_kwargs: result
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(stderr, "")
        path = Path(json.loads(stdout)["journal"]["path"])
        raw = path.read_bytes()
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertTrue(raw.endswith(b"\n"))
        record = json.loads(raw)
        saved = record["results"][0]["findings"][0]
        self.assertEqual(saved["location"],
                         "src/<OPENAI_API_KEY redacted>.py:17")
        self.assertEqual(saved["summary"],
                         "reported <OPENAI_API_KEY redacted>\nsecond line")
        self.assertEqual(saved["failure_scenario"],
                         "a diagnostic includes <OPENAI_API_KEY redacted>")
        self.assertEqual(saved["severity"], "minor")
        self.assertIs(saved["confirmed"], False)
        self.assertNotIn("journal\\secret", str(record))
        self.assertFalse(list(self.journal_path().glob("*/record.pending")))

    def test_every_reviewer_finding_and_refuted_claim_survives_the_round(self):
        findings = [
            {"file": "sample.py", "line": 10, "severity": "Major",
             "confidence": "High", "summary": "confirmed defect",
             "failure_scenario": "missing input leads to an empty result"},
            {"file": "sample.py", "line": 20, "severity": "minor",
             "confidence": "high", "summary": "minor diagnostic",
             "failure_scenario": "a message omits context"},
            {"file": "sample.py", "line": 30, "severity": "major",
             "confidence": "low", "summary": "uncertain defect",
             "failure_scenario": "an uncertain input could be lost"},
            {"file": "sample.py", "line": 40, "severity": "major",
             "confidence": "high", "summary": "outside the model",
             "failure_scenario": "an excluded writer replaces the file",
             "in_scope": False, "preconditions": "an excluded writer"},
        ]

        def answer(reviewer, *_args, **_kwargs):
            result = self.answer(reviewer)
            if reviewer["name"] == "answered":
                result["verdict"] = "Refuted"
                result["findings"] = findings
                result["claims"][0].update(
                    status="Refuted", why="sample.py loses the missing input")
                result["notes"] = "the input loss explains this rejection"
            return result

        review.run_one = answer
        code, stdout, stderr = self.invoke(only=None)
        self.assertEqual(code, review.STATES["REVIEW_FAIL"])
        self.assertEqual(stderr, "")
        record = self.records()[0]
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual([(r["name"], r["verdict"])
                          for r in record["results"]],
                         [("answered", "Refuted"), ("errored", "upheld")])
        saved = record["results"][0]["findings"]
        self.assertEqual([f["confirmed"] for f in saved],
                         [True, False, False, False])
        self.assertEqual([f["location"] for f in saved],
                         ["sample.py:10", "sample.py:20", "sample.py:30",
                          "sample.py:40"])
        for original, persisted in zip(findings, saved):
            for key, value in original.items():
                self.assertEqual(persisted[key], value)
        self.assertEqual(record["results"][1]["findings"], [])
        self.assertEqual(record["rejecting_reviewers"], ["answered"])
        self.assertEqual(record["refuted_claims"], [{
            "claim_index": 0, "claim": self.CLAIM, "status": "Refuted",
            "why": "sample.py loses the missing input", "reviewer": "answered",
        }])
        self.assertEqual(record["results"][0]["notes"],
                         "the input loss explains this rejection")
        report = json.loads(stdout)
        self.assertEqual(record["refuted_claims"], report["refuted_claims"])
        self.assertEqual(record["rejecting_reviewers"],
                         report["rejecting_reviewers"])

    def test_claim_text_is_redacted_without_changing_original_digests(self):
        secret = 'sk-"claim\\secret\nvalue'
        claim = "The reader preserves this exact claim: " + secret
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            code, _stdout, stderr = self.invoke(extra_args=["--claim", claim])
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(stderr, "")
        record = self.records()[0]
        self.assertEqual(record["claims"], [
            self.CLAIM,
            "The reader preserves this exact claim: <OPENAI_API_KEY redacted>",
        ])
        self.assertEqual(record["claim_digests"], [
            hashlib.sha256(text.encode("utf-8")).hexdigest()
            for text in (self.CLAIM, claim)
        ])
        self.assertNotEqual(record["claim_digests"][1], hashlib.sha256(
            record["claims"][1].encode("utf-8")).hexdigest())

    def test_direct_append_redacts_all_persisted_text(self):
        secrets = {"OPENAI_API_KEY": "sk-direct-openai-secret",
                   "OPENROUTER_API_KEY": 'sk-direct-"router\\secret',
                   "ANTHROPIC_API_KEY": "sk-direct-anthropic-secret"}
        first, second, third = secrets.values()
        result = self.answer({"name": first})
        result.update(verdict="refuted", notes=third)
        result["findings"] = [{
            "file": first, "line": 9, "severity": "major",
            "confidence": "high", "summary": second,
            "failure_scenario": third, "preconditions": first,
            "extra": {second: [third]},
        }]
        result["claims"] = [{"claim": first, "status": "refuted", "why": third}]
        with patch.dict(os.environ, secrets):
            returned, path = review.append_review_journal(
                self.journal_path(), "implementation", 1, [first],
                "REVIEW_FAIL", [second], results=[result],
                panel_policy={third: second})
        persisted = json.loads(path.read_text())
        self.assertEqual(returned, persisted)
        for name, secret in secrets.items():
            self.assertNotIn(secret, str(persisted))
            self.assertIn("<" + name + " redacted>", str(persisted))
        self.assertEqual(persisted["effective_panel"],
                         ["<OPENAI_API_KEY redacted>"])
        self.assertEqual(persisted["panel_policy"], {
            "<ANTHROPIC_API_KEY redacted>": "<OPENROUTER_API_KEY redacted>",
        })
        self.assertEqual(persisted["results"][0]["findings"][0]["extra"], {
            "<OPENROUTER_API_KEY redacted>": ["<ANTHROPIC_API_KEY redacted>"],
        })
        self.assertEqual(persisted["refuted_claims"][0]["why"],
                         "<ANTHROPIC_API_KEY redacted>")

    def test_version_one_history_is_readable_and_unchanged_after_append(self):
        legacy = {
            "type": "review_round", "schema_version": 1,
            "journal_header": review.JOURNAL_HEADER,
            "date": "2026-01-01T00:00:00.000000000Z", "kind": "implementation",
            "round": 1, "effective_panel": ["answered"], "verdict": "REVIEW_PASS",
            "claim_digests": [hashlib.sha256(self.CLAIM.encode()).hexdigest()],
        }
        old_path = self.seed_record("000-legacy", legacy)
        old_bytes = old_path.read_bytes()
        code, _stdout, stderr = self.invoke()
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(stderr, "")
        self.assertEqual(old_path.read_bytes(), old_bytes)
        records = self.records()
        self.assertEqual(records[0], legacy)
        self.assertEqual(records[1]["schema_version"], 2)
        for key in ("type", "kind", "round", "effective_panel", "verdict",
                    "claim_digests"):
            self.assertEqual([record[key] for record in records], [legacy[key]] * 2)

    def test_journal_payload_failure_cannot_change_a_review_verdict(self):
        with patch.object(review, "claim_digests",
                          side_effect=ValueError("injected digest failure")):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertFalse(json.loads(stdout)["journal"]["written"])
        self.assertIn("JOURNAL_WRITE_FAILED", stderr)
        self.assertIn("injected digest failure", stderr)
        self.assertEqual(self.records(), [])

    def test_round_metadata_and_rejection_reasons_are_redacted(self):
        secret = "sk-round-metadata-secret"
        result = self.answer({"name": secret})
        result.update(verdict="refuted", notes="reviewer note: " + secret)
        result["claims"][0].update(
            status="refuted", why="reviewer reason: " + secret)
        review.run_one = lambda *_args, **_kwargs: result
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": secret}):
            code, _stdout, stderr = self.invoke(
                extra_args=["--allow-single-reviewer", "exception: " + secret])
        self.assertEqual(code, review.STATES["REVIEW_CLAIMS_REFUTED"])
        self.assertEqual(stderr, "")
        record = self.records()[0]
        tag = "<OPENROUTER_API_KEY redacted>"
        self.assertEqual(record["effective_panel"], [tag])
        self.assertEqual(record["rejecting_reviewers"], [tag])
        self.assertEqual(record["results"][0]["name"], tag)
        self.assertEqual(record["results"][0]["notes"], "reviewer note: " + tag)
        self.assertEqual(record["refuted_claims"][0]["why"],
                         "reviewer reason: " + tag)
        self.assertEqual(record["panel_policy"]["single_reviewer_override"],
                         "exception: " + tag)
        self.assertNotIn(secret, json.dumps(record))

    def test_redaction_cannot_reclassify_completed_reviewer_results(self):
        result = self.answer({"name": "answered"})
        result["verdict"] = "REFUTED"
        result["claims"][0]["status"] = "REFUTED"
        review.run_one = lambda *_args, **_kwargs: result
        with patch.dict(os.environ, {"OPENAI_API_KEY": "REFUTED"}):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, review.STATES["REVIEW_CLAIMS_REFUTED"])
        self.assertEqual(stderr, "")
        record = self.records()[0]
        report = json.loads(stdout)
        self.assertEqual(record["rejecting_reviewers"], ["answered"])
        self.assertEqual(record["rejecting_reviewers"],
                         report["rejecting_reviewers"])
        self.assertEqual(record["refuted_claims"], report["refuted_claims"])
        self.assertEqual(record["results"][0]["verdict"],
                         "<OPENAI_API_KEY redacted>")

    def test_valid_claim_digest_survives_secret_substring_collision(self):
        digest = hashlib.sha256(b"abc").hexdigest()
        self.assertIn("4141", digest)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "4141"}):
            code, _stdout, stderr = self.invoke(extra_args=["--claim", "abc"])
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(stderr, "")
        record = self.records()[0]
        self.assertEqual(record["claims"], [self.CLAIM, "abc"])
        self.assertEqual(record["claim_digests"][1], digest)

    def test_transport_key_collisions_cannot_drop_or_rehash_record_data(self):
        for secret in ("files", "record_line", "details", "claims", "claim",
                       "claim_digests", "kind", "round", "effective_panel",
                       "verdict", "panel_policy", "results"):
            with self.subTest(secret=secret):
                result = self.answer({"name": "answered"})
                result["verdict"] = "refuted"
                result["claims"][0].update(status="refuted",
                                           why="the input loses a required row")
                result["findings"] = [{
                    "file": "sample.py", "line": 7, "severity": "major",
                    "confidence": "high", "summary": "required row lost",
                    "failure_scenario": "missing input produces an empty row",
                }]
                review.run_one = lambda *_args, **_kwargs: result
                with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
                    code, stdout, stderr = self.invoke(extra_args=["--claim", secret])
                self.assertEqual(code, review.STATES["REVIEW_FAIL"])
                self.assertEqual(stderr, "")
                self.assertTrue(json.loads(stdout)["journal"]["written"])
                record = self.records()[-1]
                tag = "<OPENAI_API_KEY redacted>"
                saved = record["results"][0]
                self.assertEqual(saved["findings"][0]["summary"], "required row lost")
                self.assertEqual(saved["findings"][0]["location"], "sample.py:7")
                self.assertIs(saved["findings"][0]["confirmed"], True)
                self.assertEqual(record["rejecting_reviewers"], ["answered"])
                self.assertEqual(record["refuted_claims"][0]
                                 ["why"], "the input loses a required row")
                self.assertEqual(record["claims"][1], tag)
                self.assertEqual(record["claim_digests"][1],
                                 hashlib.sha256(secret.encode()).hexdigest())
                self.assertNotIn("record_line", record)

    def test_child_refuses_multiple_or_unterminated_prepared_lines(self):
        for line in ("{}\n{}\n", "{}", "", None):
            with self.subTest(line=line):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), review.JOURNAL_CHILD_ARG],
                    input=json.dumps({"files": [str(SCRIPT)], "record_line": line}),
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 1)
                self.assertIn("one newline-terminated JSON line",
                              json.loads(result.stdout)["error"])
                self.assertEqual(self.records(), [])

    def test_two_invocations_append_two_records_with_monotonic_timestamps(self):
        self.assertEqual(self.invoke()[0], review.STATES["REVIEW_PASS"])
        self.assertEqual(self.invoke()[0], review.STATES["REVIEW_PASS"])
        records = self.records()
        self.assertEqual(len(records), 2)
        for path in self.journal_path().glob("*/record.jsonl"):
            self.assertEqual(path.read_bytes().count(b"\n"), 1)
            self.assertTrue(path.read_bytes().endswith(b"\n"))
        self.assertLess(records[0]["date"], records[1]["date"])
        for record in records:
            notice = record["journal_header"].lower()
            self.assertIn("audit-only", notice)
            self.assertIn("mandatory per-change receipt", notice)
            self.assertIn("lock honest authors out", notice)
            self.assertIn("non-gating", notice)
            self.assertIn("cannot decide or block a verdict", notice)
            self.assertEqual(record["kind"], "implementation")
            self.assertEqual(record["round"], 1)
            self.assertEqual(record["effective_panel"], ["answered"])
            self.assertEqual(record["verdict"], "REVIEW_PASS")
            self.assertEqual(
                record["claim_digests"],
                [hashlib.sha256(self.CLAIM.encode("utf-8")).hexdigest()])

    def test_effective_panel_contains_only_reviewers_that_answered(self):
        def one_answer(reviewer, *_args, **_kwargs):
            if reviewer["name"] == "errored":
                return {"ok": False, "name": "errored",
                        "error": "HTTP 402", "elapsed_s": 0}
            return self.answer(reviewer)

        review.run_one = one_answer
        code, stdout, _stderr = self.invoke(only=None)
        self.assertEqual(code, review.STATES["REVIEW_PARTIAL"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PARTIAL")
        self.assertEqual(self.records()[-1]["effective_panel"], ["answered"])

    def test_silent_single_reviewer_is_review_incomplete(self):
        original_post = review._post
        original_key = os.environ.get("OPENAI_API_KEY")
        review.load_reviewers = lambda: [{
            "name": "silent", "provider": "openai", "model": "m",
            "profiles": ["standard"], "enabled": True,
        }]
        review.run_one = self.saved["run_one"]
        try:
            os.environ["OPENAI_API_KEY"] = "sk-test"
            review._post = lambda *_args, **_kwargs: ({
                "status": "incomplete", "output": [],
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {
                    "output_tokens": 69000,
                    "output_tokens_details": {"reasoning_tokens": 69000},
                },
            }, None)
            code, stdout, _stderr = self.invoke(only="silent")
        finally:
            review._post = original_post
            if original_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = original_key
        self.assertEqual(code, review.STATES["REVIEW_INCOMPLETE"])
        report = json.loads(stdout)
        self.assertEqual(report["state"], "REVIEW_INCOMPLETE")
        self.assertEqual(report["completed"], 0)
        self.assertTrue(report["failed"][0]["incomplete"])

    def test_existing_journal_cannot_decide_the_verdict(self):
        self.seed_record("000-seed", {
            "type": "review_round", "verdict": "REVIEW_FAIL",
            "date": "9999-12-31T23:59:59.999999999Z",
        })
        code, stdout, _stderr = self.invoke()
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertEqual([record["verdict"] for record in self.records()],
                         ["REVIEW_FAIL", "REVIEW_PASS"])

    def test_journal_code_runs_only_after_the_verdict_is_decided(self):
        original_decide = review.decide_state
        original_record = review.record_review_round
        events = []

        def decide(*args, **kwargs):
            result = original_decide(*args, **kwargs)
            events.append(("verdict", result))
            return result

        def record(_args, _completed, verdict):
            self.assertEqual(events, [("verdict", verdict)])
            events.append(("journal", verdict))
            return {"path": "test", "written": True, "error": None}

        review.decide_state = decide
        review.record_review_round = record
        try:
            code, stdout, _stderr = self.invoke()
        finally:
            review.decide_state = original_decide
            review.record_review_round = original_record

        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertEqual(events, [("verdict", "REVIEW_PASS"),
                                  ("journal", "REVIEW_PASS")])

    def test_partial_pending_record_cannot_decide_or_block_the_verdict(self):
        pending = self.journal_path() / "000-interrupted" / "record.pending"
        pending.parent.mkdir(parents=True)
        pending.write_text('{"type":"hostile seed","verdict":"REVIEW_FAIL"')
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertNotIn("JOURNAL_WRITE_FAILED", stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(pending.read_text(),
                         '{"type":"hostile seed","verdict":"REVIEW_FAIL"')

    def test_read_only_journal_failure_is_loud_but_non_gating(self):
        path = self.journal_path()
        path.mkdir(parents=True)
        path.chmod(0o500)
        try:
            code, stdout, stderr = self.invoke()
        finally:
            path.chmod(0o700)
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        report = json.loads(stdout)
        self.assertEqual(report["state"], "REVIEW_PASS")
        self.assertFalse(report["journal"]["written"])
        self.assertIn("journal helper failed", report["journal"]["error"])
        self.assertNotIn("invalid success", report["journal"]["error"])
        self.assertIn("JOURNAL_WRITE_FAILED", stderr)

    def test_broken_stderr_cannot_turn_journal_failure_into_review_failure(self):
        class BrokenStderr:
            def write(self, _text):
                raise OSError("stderr is unavailable")

            def flush(self):
                raise OSError("stderr is unavailable")

        sys.argv = [str(SCRIPT), "--kind", "implementation", "--round", "1",
                    "--profile", "standard", "--quorum", "1",
                    "--allow-single-reviewer", "offline singleton fixture",
                    "--claim", self.CLAIM, "--file", str(SCRIPT), "--json",
                    "--only", "answered"]
        stdout = io.StringIO()
        saved_stderr = sys.stderr
        saved_write = review.os.write
        saved_child = review._run_journal_child
        try:
            sys.stderr = BrokenStderr()
            review._run_journal_child = (
                lambda *_args: (_ for _ in ()).throw(
                    OSError("journal storage is unavailable")))
            review.os.write = lambda _fd, _data: (_ for _ in ()).throw(
                OSError("descriptor 2 is unavailable"))
            with redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as stopped:
                    review.main()
        finally:
            sys.stderr = saved_stderr
            review.os.write = saved_write
            review._run_journal_child = saved_child

        self.assertEqual(stopped.exception.code, review.STATES["REVIEW_PASS"])
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["state"], "REVIEW_PASS")
        self.assertFalse(report["journal"]["written"])

    def test_stalled_diagnostic_writer_is_killed_at_its_deadline(self):
        class DescriptorStderr:
            pass

        saved = (sys.stderr, review.os.fork, review.os.waitpid,
                 review.os.kill, review.time.monotonic, review.time.sleep)
        killed = []
        clock = iter((0.0, 0.1, 0.3))
        try:
            sys.stderr = DescriptorStderr()
            review.os.fork = lambda: 4321
            review.os.waitpid = lambda _pid, _flags: (0, 0)
            review.os.kill = lambda pid, sig: killed.append((pid, sig))
            review.time.monotonic = lambda: next(clock)
            review.time.sleep = lambda _seconds: None

            review._emit_journal_failure("diagnostic")
        finally:
            (sys.stderr, review.os.fork, review.os.waitpid,
             review.os.kill, review.time.monotonic,
             review.time.sleep) = saved

        self.assertEqual(killed, [(4321, signal.SIGKILL)])

    def test_journal_state_home_never_resolves_inside_reviewed_worktree(self):
        os.environ["XDG_STATE_HOME"] = str(REPO / ".state-in-repo")
        path = review.review_journal_path([SCRIPT])
        with self.assertRaises(ValueError):
            path.relative_to(REPO.resolve())

    def test_file_symlink_keeps_its_lexical_worktree_in_placement_scope(self):
        target = self.tmp / "outside-input.py"
        target.write_text("pass\n")
        link = REPO / (".journal-input-link-" + self.tmp.name + ".py")
        self.addCleanup(link.unlink, missing_ok=True)
        link.symlink_to(target)
        os.environ["XDG_STATE_HOME"] = str(REPO / ".state-in-repo")
        previous = Path.cwd()
        try:
            os.chdir(self.tmp)
            path = review.review_journal_path([link])
        finally:
            os.chdir(previous)

        with self.assertRaises(ValueError):
            path.relative_to(REPO.resolve())

    def test_symlinked_input_directory_keeps_lexical_worktree_in_scope(self):
        target_dir = self.tmp / "outside-inputs"
        target_dir.mkdir()
        (target_dir / "input.py").write_text("pass\n")
        link_dir = REPO / (".journal-input-dir-" + self.tmp.name)
        self.addCleanup(link_dir.unlink, missing_ok=True)
        link_dir.symlink_to(target_dir, target_is_directory=True)
        os.environ["XDG_STATE_HOME"] = str(REPO / ".state-in-repo")
        previous = Path.cwd()
        try:
            os.chdir(self.tmp)
            path = review.review_journal_path([link_dir / "input.py"])
        finally:
            os.chdir(previous)

        with self.assertRaises(ValueError):
            path.relative_to(REPO.resolve())

    def test_symlinked_journal_directory_cannot_escape_into_worktree(self):
        base = Path(os.environ["XDG_STATE_HOME"])
        base.mkdir(parents=True)
        target = REPO / (".journal-target-" + self.tmp.name)
        (base / review.JOURNAL_DIR).symlink_to(target, target_is_directory=True)
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertIn("JOURNAL_WRITE_FAILED", stderr)
        self.assertIn("operated Git worktree", stderr)
        self.assertFalse(target.exists())

    def test_preexisting_symlink_inside_state_home_is_refused(self):
        base = Path(os.environ["XDG_STATE_HOME"])
        base.mkdir(parents=True)
        target = base / "alternate"
        target.mkdir()
        (base / review.JOURNAL_DIR).symlink_to(target, target_is_directory=True)

        code, stdout, stderr = self.invoke()

        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertIn("JOURNAL_WRITE_FAILED", stderr)
        self.assertFalse((target / review.JOURNAL_NAME).exists())

    def test_symlinked_configured_state_home_is_refused(self):
        target = self.tmp / "actual-state"
        target.mkdir()
        configured = self.tmp / "configured-state"
        configured.symlink_to(target, target_is_directory=True)
        os.environ["XDG_STATE_HOME"] = str(configured)

        code, stdout, stderr = self.invoke()

        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertIn("JOURNAL_WRITE_FAILED", stderr)
        self.assertFalse((target / review.JOURNAL_DIR).exists())

    def test_intermediate_symlink_swap_after_validation_is_refused(self):
        path = review.review_journal_path([SCRIPT])
        path.parent.mkdir(parents=True)
        parked = path.parent.with_name(path.parent.name + "-parked")
        target = REPO / (".journal-swap-target-" + self.tmp.name)
        target.mkdir()
        path.parent.rename(parked)
        path.parent.symlink_to(target, target_is_directory=True)
        saved_marker = os.environ.pop(review.JOURNAL_TEST_MARKER)
        original_open_chain = review._open_directory_chain
        opened = []

        def traced_open_chain(candidate):
            opened.append(Path(candidate))
            return original_open_chain(candidate)

        review._open_directory_chain = traced_open_chain
        try:
            with self.assertRaises(OSError):
                review.append_review_journal(
                    path, "implementation", 1, ["answered"],
                    "REVIEW_PASS", [self.CLAIM])
            self.assertEqual(opened, [path],
                             "the test marker short-circuited the production "
                             "descriptor-anchored no-follow guard")
            self.assertFalse((target / review.JOURNAL_NAME).exists())
        finally:
            review._open_directory_chain = original_open_chain
            os.environ[review.JOURNAL_TEST_MARKER] = saved_marker
            path.parent.unlink(missing_ok=True)
            shutil.rmtree(parked, ignore_errors=True)
            shutil.rmtree(target, ignore_errors=True)

    def test_interrupted_private_write_cannot_corrupt_canonical_history(self):
        path = self.journal_path()
        original_write = review.os.write
        calls = 0

        def interrupted_write(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(fd, data[:17])
            raise OSError("injected interruption")

        review.os.write = interrupted_write
        try:
            with self.assertRaisesRegex(OSError, "injected interruption"):
                review.append_review_journal(
                    path, "implementation", 1, ["answered"],
                    "REVIEW_PASS", [self.CLAIM])
        finally:
            review.os.write = original_write

        pending = list(path.glob("*/record.pending"))
        self.assertEqual(len(pending), 1)
        before = pending[0].read_bytes()
        self.assertEqual(self.records(), [])

        code, stdout, stderr = self.invoke()

        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertNotIn("JOURNAL_WRITE_FAILED", stderr)
        self.assertEqual(pending[0].read_bytes(), before)
        self.assertEqual(len(self.records()), 1)

    def test_stalled_journal_helper_is_killed_without_gating_verdict(self):
        class HungProcess:
            def __init__(self):
                self.returncode = None
                self.calls = []
                self.killed = False

            def communicate(self, input=None, timeout=None):
                self.calls.append((input, timeout))
                raise subprocess.TimeoutExpired("journal helper", timeout)

            def kill(self):
                self.killed = True

        hung = HungProcess()
        original_popen = review.subprocess.Popen
        review.subprocess.Popen = lambda *_args, **_kwargs: hung
        try:
            code, stdout, stderr = self.invoke()
        finally:
            review.subprocess.Popen = original_popen

        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertTrue(hung.killed)
        self.assertEqual(hung.calls[0][1], review.JOURNAL_TIMEOUT_SECONDS)
        self.assertIn("JOURNAL_WRITE_FAILED", stderr)
        self.assertIn("append exceeded", stderr)

    def test_short_writes_are_completed_before_success(self):
        path = self.journal_path()
        original_write = review.os.write
        calls = []

        def short_write(fd, data):
            count = max(1, len(data) // 2)
            calls.append(count)
            return original_write(fd, data[:count])

        review.os.write = short_write
        try:
            review.append_review_journal(
                path, "implementation", 1, ["answered"], "REVIEW_PASS",
                [self.CLAIM])
        finally:
            review.os.write = original_write
        self.assertGreater(len(calls), 1)
        self.assertEqual(len(self.records()), 1)

    def test_private_modes_remain_private_under_common_umask(self):
        previous = os.umask(0o022)
        try:
            _record, record_path = review.append_review_journal(
                self.journal_path(), "implementation", 1, ["answered"],
                "REVIEW_PASS", [self.CLAIM])
        finally:
            os.umask(previous)

        self.assertEqual(stat.S_IMODE(record_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)

    def test_watchdog_is_disarmed_before_journal_persistence(self):
        events = []
        review.disarm_watchdog = lambda: events.append("disarmed")

        def record(_args, _completed, _verdict):
            self.assertEqual(events, ["disarmed"])
            return {"path": "test", "written": True, "error": None}

        review.record_review_round = record
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertEqual(stderr, "")

    def test_hard_linked_journal_cannot_modify_a_worktree_file(self):
        if self.tmp.stat().st_dev != REPO.stat().st_dev:
            self.skipTest("hard-link defence requires a fixture on the "
                          "repository filesystem")
        state = self.tmp / "hardlink-state"
        os.environ["XDG_STATE_HOME"] = str(state)
        base = Path(os.environ["XDG_STATE_HOME"])
        journal = base / review.JOURNAL_DIR / review.JOURNAL_NAME
        journal.parent.mkdir(parents=True)
        target = REPO / (".journal-hardlink-target-" + self.tmp.name)
        self.addCleanup(target.unlink, missing_ok=True)
        target.write_text("repository bytes\n")
        before = target.read_bytes()
        self.assertEqual(
            target.stat().st_dev, journal.parent.stat().st_dev,
            "hard-link journal test fixture and worktree target must share "
            "one filesystem")
        _hard_link_or_skip(self, target, journal)

        code, stdout, stderr = self.invoke()

        self.assertEqual(code, review.STATES["REVIEW_PASS"])
        self.assertEqual(json.loads(stdout)["state"], "REVIEW_PASS")
        self.assertIn("JOURNAL_WRITE_FAILED", stderr)
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
