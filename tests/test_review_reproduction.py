"""Reproduction records are validated before transport and delivered as evidence."""

from tests.watchdog_guard import guarded_main
import copy
import importlib.util
import io
import json
import os
import shlex
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills/hanig-review-gate/scripts/review.py"
SPEC = importlib.util.spec_from_file_location("reproduction_review", SCRIPT)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)
from tests.review_round_fixture import isolated_reservation
original_reservation = review.reserve_review_round
FIELDS = ("setup", "command", "expected", "observed", "revision")
STATUSES = ("reproduced", "not-reproduced")
COUNTEREXAMPLE = {"setup": " fixture state\nsecond line ", "input": "run probe",
                  "expected": "all records retained", "observed": "one record lost"}
REASON = "The failure needs a manual observation."
OPTIONAL_FIELDS = ({}, {"counterexample": COUNTEREXAMPLE},
                   {"non_executable_reason": REASON},
                   {"counterexample": COUNTEREXAMPLE, "non_executable_reason": REASON})
EXAMPLE_PLACEHOLDERS = (None, {}, {"setup": ""},
                        dict.fromkeys(COUNTEREXAMPLE, None),
                        dict.fromkeys(COUNTEREXAMPLE, " \t\n"),
                        {"setup": None, "input": "", "expected": " \t", "observed": "\n"})
REASON_PLACEHOLDERS = (None, "", " \t\n")
PLACEHOLDER_FIELDS = tuple(
    {"counterexample": example, "non_executable_reason": reason}
    for example in EXAMPLE_PLACEHOLDERS for reason in REASON_PLACEHOLDERS
) + tuple({"counterexample": example, "non_executable_reason": REASON}
          for example in EXAMPLE_PLACEHOLDERS
) + tuple({"counterexample": COUNTEREXAMPLE, "non_executable_reason": reason}
          for reason in REASON_PLACEHOLDERS)


class TestCounterexampleSchema(unittest.TestCase):
    def verdict(self, fields):
        return {"verdict": "refuted", "claims": [], "findings": [dict(
            severity="major", confidence="high", failure_scenario="input loses a record",
            **fields)]}

    def test_either_both_or_neither_is_valid_without_mutating_fields(self):
        for fields in OPTIONAL_FIELDS:
            with self.subTest(fields=fields):
                verdict = self.verdict(copy.deepcopy(fields))
                before = copy.deepcopy(verdict)
                self.assertIsNone(review.verdict_schema_error(verdict))
                self.assertEqual(verdict, before)

    def test_neither_is_valid_and_still_confirmed(self):
        verdict = self.verdict({})
        self.assertIsNone(review.verdict_schema_error(verdict))
        self.assertTrue(review.is_confirmed(verdict["findings"][0]))

    def test_placeholders_validate_in_both_kinds_without_mutating_fields(self):
        plan = review._load_plan_review()
        for fields in PLACEHOLDER_FIELDS:
            for severity in ("critical", "major", "minor"):
                for kind, validate in (("implementation", review.verdict_schema_error),
                                       ("plan", lambda v: plan._plan_schema_error(v, []))):
                    with self.subTest(fields=fields, severity=severity, kind=kind):
                        verdict = self.verdict(copy.deepcopy(fields))
                        verdict["findings"][0]["severity"] = severity
                        before = copy.deepcopy(verdict)
                        self.assertIsNone(validate(verdict))
                        self.assertEqual(verdict, before)

    def test_malformed_counterexample_is_refused(self):
        malformed = [[], "text", "", " \t\n", 7, False,
                     {"extra": "non-empty"}, {"setup": []}, {"setup": False}]
        for field in COUNTEREXAMPLE:
            missing = dict(COUNTEREXAMPLE)
            del missing[field]
            malformed.append(missing)
            malformed.append({field: "one supplied value"})
            malformed.append(dict.fromkeys(COUNTEREXAMPLE, "") | {field: "supplied"})
            for value in ("", " \t\n", None, [], {}, 7, False):
                malformed.append(dict(COUNTEREXAMPLE, **{field: value}))
        for example in malformed:
            for severity in ("critical", "major", "minor"):
                with self.subTest(example=example, severity=severity):
                    verdict = self.verdict({"counterexample": example,
                                            "non_executable_reason": REASON})
                    verdict["findings"][0]["severity"] = severity
                    self.assertIn("counterexample", review.verdict_schema_error(verdict))
                    self.assertIn("counterexample", review._load_plan_review()._plan_schema_error(
                        verdict, []))

    def test_malformed_non_executable_reason_is_refused(self):
        for reason in ([], {}, 7, False):
            for severity in ("critical", "major", "minor"):
                with self.subTest(reason=reason, severity=severity):
                    verdict = self.verdict({"non_executable_reason": reason,
                                            "counterexample": COUNTEREXAMPLE})
                    verdict["findings"][0]["severity"] = severity
                    self.assertIn("non_executable_reason", review.verdict_schema_error(verdict))
                    self.assertIn("non_executable_reason", review._load_plan_review()._plan_schema_error(
                        verdict, []))

    def test_confirmation_filter_ignores_optional_fields(self):
        for fields in OPTIONAL_FIELDS + PLACEHOLDER_FIELDS:
            for severity in ("critical", "major", "minor"):
                for confidence in ("high", "medium", "low"):
                    for in_scope in (True, False):
                        finding = dict(self.verdict({})["findings"][0],
                                       severity=severity, confidence=confidence,
                                       in_scope=in_scope)
                        self.assertEqual(review.is_confirmed(dict(finding, **fields)),
                                         review.is_confirmed(finding))


class TestReproductionEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.code = self.root / "candidate.py"
        self.code.write_text("value = 1\n")
        self.marker = self.root / "COMMAND_WAS_EXECUTED"
        self.entry = {
            "location": "candidate.py:1",
            "summary": "a concrete prior finding",
            "reason": "recorded observation for adjudication",
            "disposition": "not-reproduced",
            "reproduction": {
                "setup": "isolated fixture\nwith a second setup line",
                "command": "touch " + shlex.quote(str(self.marker)),
                "expected": "probe fails with the alleged symptom",
                "observed": "probe succeeds\nwithout that symptom",
                "revision": "a" * 40,
            },
        }
        self.digest = review.finding_digest(self.entry["location"], self.entry["summary"])

    def invoke(self, entry, findings=(), extra=(), json_output=True):
        path = self.root / "dispositions.json"
        path.write_text(json.dumps({self.digest: entry}))
        argv = [str(SCRIPT), "--kind", "implementation", "--round", "2",
                "--author", "codex/gpt-6-astra", "--claim", review.HONEST_RUN_CLAIM,
                "--dispositions", str(path), "--file", str(self.code)]
        if json_output:
            argv.append("--json")
        argv.extend(extra)
        # Exercise both real provider adapters, parser and verdict aggregation.
        roster = [{"name": provider, "provider": provider, "model": "fixture",
                   "profiles": ["standard"], "enabled": True}
                  for provider in ("openai", "openrouter")]
        captured = []
        self.system_prompts = []
        verdict = json.dumps({
            "verdict": "refuted" if findings else "upheld",
            "findings": list(findings),
            "claims": [{"claim_index": 0, "status": "supported",
                        "claim": review.HONEST_RUN_CLAIM,
                        "why": "The offline candidate fixture preserves its declared output."}],
            "notes": "offline fixture",
        })

        def transport(_url, payload, *_args, **_kwargs):
            if "input" in payload:
                captured.append(payload["input"])
                self.system_prompts.append(payload["input"].split("\n\n---\n\n", 1)[0])
                return {"output": [{"type": "message", "content": [
                    {"type": "output_text", "text": verdict}]}]}, None
            captured.append(payload["messages"][1]["content"])
            self.system_prompts.append(payload["messages"][0]["content"])
            return {"choices": [{"message": {"content": verdict},
                                 "finish_reason": "stop"}]}, None

        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", argv))
            stack.enter_context(patch.dict(os.environ, {
                "OPENAI_API_KEY": "fixture-key", "OPENROUTER_API_KEY": "fixture-key"}))
            stack.enter_context(patch.object(review, "load_effective_reviewers", return_value=roster))
            stack.enter_context(patch.object(review, "_post", side_effect=transport))
            stack.enter_context(patch.object(review, "record_review_round", return_value={}))
            # A new transport path must fail instead of contacting the network.
            stack.enter_context(patch.object(review.urllib.request, "urlopen",
                                             side_effect=AssertionError("unexpected network")))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            with self.assertRaises(SystemExit) as raised:
                with patch.object(review, "reserve_review_round",
                        side_effect=lambda args: isolated_reservation(
                            review, original_reservation, args)):
                    guarded_main(review.main)
        return raised.exception.code, stderr.getvalue(), captured, stdout.getvalue()

    def assert_refused(self, entry, field, extra=()):
        code, error, prompts, _ = self.invoke(entry, extra=extra)
        self.assertEqual(code, 4, error)
        self.assertIn(self.digest, error)
        self.assertIn(field, error)
        self.assertEqual(prompts, [], "invalid evidence reached a provider")
        self.assertFalse(self.marker.exists())

    def test_each_missing_field_refuses_before_transport(self):
        for status in STATUSES:
            for field in FIELDS:
                with self.subTest(status=status, field=field):
                    entry = copy.deepcopy(self.entry)
                    entry["disposition"] = status
                    del entry["reproduction"][field]
                    path = self.root / "missing-field.json"
                    path.write_text(json.dumps({self.digest: entry}))
                    error = io.StringIO()
                    with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                        review.load_dispositions(path)
                    self.assertEqual(raised.exception.code, 4)
                    self.assertIn(self.digest, error.getvalue())
                    self.assertIn("reproduction." + field, error.getvalue())
                    self.assert_refused(entry, "reproduction." + field)

    def test_each_malformed_field_refuses_before_transport(self):
        for status in STATUSES:
            for field in FIELDS:
                for value in (None, False, 2, [], {}, "", " \t\n", "\ud800"):
                    with self.subTest(status=status, field=field, value=repr(value)):
                        entry = copy.deepcopy(self.entry)
                        entry["disposition"] = status
                        entry["reproduction"][field] = value
                        self.assert_refused(entry, "reproduction." + field)

    def test_missing_or_nonobject_reproduction_refuses(self):
        for status in STATUSES:
            entry = copy.deepcopy(self.entry)
            entry["disposition"] = status
            del entry["reproduction"]
            self.assert_refused(entry, "reproduction")
            for value in (None, [], "text", 1, False):
                with self.subTest(status=status, value=value):
                    entry["reproduction"] = value
                    self.assert_refused(entry, "reproduction")

    def test_revision_must_be_a_full_commit_id(self):
        for value in ("HEAD", "abc1234", "a" * 39, "a" * 41, "a" * 63,
                      "a" * 65, "g" * 40, "A" * 40, " " + "a" * 40,
                      "a" * 40 + "\n"):
            with self.subTest(value=value):
                entry = copy.deepcopy(self.entry)
                entry["reproduction"]["revision"] = value
                self.assert_refused(entry, "reproduction.revision")

    def test_list_cannot_probe_before_evidence_validation(self):
        entry = copy.deepcopy(self.entry)
        del entry["reproduction"]["setup"]
        self.assert_refused(entry, "reproduction.setup", extra=("--list",))

    def test_complete_records_reach_both_transports_without_execution(self):
        for status in STATUSES:
            for revision in ("a" * 40, "b" * 64):
                with self.subTest(status=status, revision=revision):
                    entry = copy.deepcopy(self.entry)
                    entry["disposition"] = status
                    entry["reproduction"]["revision"] = revision
                    code, error, prompts, _ = self.invoke(entry)
                    self.assertEqual(code, 0, error)
                    self.assertEqual(len(prompts), 2)
                    for prompt in prompts:
                        for field in ("location", "summary", "reason", "disposition"):
                            self.assertIn(entry[field], prompt)
                        for field, value in entry["reproduction"].items():
                            self.assertIn(field + ": " + value, prompt)
                        instruction = "Re-evaluate this disagreement; do not silently filter it."
                        if status == "not-reproduced":
                            self.assertIn("disposition: not-reproduced\n    " + instruction,
                                          prompt)
                        else:
                            self.assertNotIn(instruction, prompt)
                    self.assertFalse(self.marker.exists(), "evidence command was executed")

    def test_deferred_needs_no_reproduction(self):
        entry = copy.deepcopy(self.entry)
        entry["disposition"] = "deferred"
        del entry["reproduction"]
        code, error, prompts, _ = self.invoke(entry)
        self.assertEqual(code, 0, error)
        self.assertEqual(len(prompts), 2)

    def test_not_reproduced_does_not_downgrade_a_confirmed_finding(self):
        finding = {"severity": "major", "confidence": "high",
                   "location": self.entry["location"], "summary": self.entry["summary"],
                   "failure_scenario": "an ordinary input loses its declared output"}
        code, error, prompts, output = self.invoke(self.entry, findings=[finding])
        self.assertEqual(code, 1, error)
        self.assertEqual(len(prompts), 2)
        report = json.loads(output)
        self.assertEqual(report["state"], "REVIEW_FAIL")
        for result in report["results"]:
            self.assertEqual(result["findings"], [finding])
        self.assertFalse(self.marker.exists())

    def test_counterexamples_render_and_do_not_change_verdict_or_execute(self):
        for severity in ("critical", "major", "minor"):
            for fields in OPTIONAL_FIELDS + PLACEHOLDER_FIELDS:
                with self.subTest(severity=severity, fields=fields):
                    fields = copy.deepcopy(fields)
                    has_example = fields.get("counterexample") == COUNTEREXAMPLE
                    has_reason = fields.get("non_executable_reason") == REASON
                    confirmed = severity != "minor"
                    if has_example:
                        fields["counterexample"]["input"] = (
                            "touch " + shlex.quote(str(self.marker)))
                    finding = dict(severity=severity, confidence="high", file="candidate.py",
                                   line=1, summary="record lost",
                                   failure_scenario="an ordinary input loses a record", **fields)
                    for json_output in (True, False):
                        code, error, prompts, output = self.invoke(
                            self.entry, findings=[finding], json_output=json_output)
                        self.assertEqual(code, 1 if confirmed else 3, error)
                        self.assertEqual(len(prompts), 2)
                        self.assertFalse(self.marker.exists(), "counterexample executed")
                        if json_output:
                            report = json.loads(output)
                            self.assertEqual(report["state"],
                                             "REVIEW_FAIL" if confirmed else "REVIEW_PARTIAL")
                            for result in report["results"]:
                                self.assertEqual(result["findings"], [finding])
                                self.assertFalse(result["retried"])
                            self.assertEqual(len(report["confirmed_findings"]), 2 if confirmed else 0)
                            for confirmed in report["confirmed_findings"]:
                                self.assertEqual({k: v for k, v in confirmed.items()
                                                  if k != "reviewer"}, finding)
                        else:
                            self.assertEqual(output.count("no counterexample"),
                                             2 if confirmed and not (has_example or has_reason) else 0)
                            self.assertEqual("      counterexample:" in output,
                                             confirmed and has_example)
                            self.assertEqual("      non_executable_reason:" in output,
                                             confirmed and has_reason)
                            if confirmed and has_example:
                                for field, value in fields["counterexample"].items():
                                    self.assertIn(field + ": " + value, output)
                            if confirmed and has_reason:
                                self.assertIn("non_executable_reason: " + REASON, output)

    def test_delivered_prompt_asks_to_omit_unused_field_without_dropping_findings(self):
        code, error, _, _ = self.invoke(self.entry)
        self.assertEqual(code, 0, error)
        self.assertEqual(len(self.system_prompts), 2)
        for prompt in self.system_prompts:
            self.assertIn("omit the field you do\nnot use rather than send an empty value", prompt)
            self.assertIn("optional in the schema; do not drop or soften a finding that lacks them", prompt)

    def test_new_evidence_fields_keep_existing_contamination_removal(self):
        offender = ("(re-run to capture full finding text; the prior round returned "
                    "REVIEW_PASS with one sub-threshold finding whose text was suppressed)")
        for status in STATUSES:
            for field in ("setup", "command", "expected", "observed"):
                with self.subTest(status=status, field=field):
                    entry = copy.deepcopy(self.entry)
                    entry["disposition"] = status
                    entry["reproduction"][field] = offender + "RETAINED_EVIDENCE"
                    code, error, prompts, output = self.invoke(entry)
                    self.assertEqual(code, 0, error)
                    self.assertEqual(len(prompts), 2)
                    for prompt in prompts:
                        self.assertNotIn(offender, prompt)
                        self.assertIn("RETAINED_EVIDENCE", prompt)
                    events = json.loads(output)["input_redactions"]
                    self.assertEqual(len(events), 1)
                    self.assertTrue(events[0]["source"].endswith("reproduction." + field))


if __name__ == "__main__":
    unittest.main()
