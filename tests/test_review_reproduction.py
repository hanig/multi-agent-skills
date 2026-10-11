"""Reproduction records are validated before transport and delivered as evidence."""

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
FIELDS = ("setup", "command", "expected", "observed", "revision")
STATUSES = ("reproduced", "not-reproduced")


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

    def invoke(self, entry, findings=(), extra=()):
        path = self.root / "dispositions.json"
        path.write_text(json.dumps({self.digest: entry}))
        argv = [str(SCRIPT), "--kind", "implementation", "--round", "2",
                "--author", "codex/gpt-6-astra", "--claim", review.HONEST_RUN_CLAIM,
                "--dispositions", str(path), "--file", str(self.code), "--json"]
        argv.extend(extra)
        # Exercise both real provider adapters, parser and verdict aggregation.
        roster = [{"name": provider, "provider": provider, "model": "fixture",
                   "profiles": ["standard"], "enabled": True}
                  for provider in ("openai", "openrouter")]
        captured = []
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
                return {"output": [{"type": "message", "content": [
                    {"type": "output_text", "text": verdict}]}]}, None
            captured.append(payload["messages"][1]["content"])
            return {"choices": [{"message": {"content": verdict},
                                 "finish_reason": "stop"}]}, None

        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", argv))
            stack.enter_context(patch.dict(os.environ, {
                "OPENAI_API_KEY": "fixture-key", "OPENROUTER_API_KEY": "fixture-key"}))
            stack.enter_context(patch.object(review, "load_effective_reviewers", return_value=roster))
            stack.enter_context(patch.object(review, "arm_watchdog"))
            stack.enter_context(patch.object(review, "disarm_watchdog"))
            stack.enter_context(patch.object(review, "_post", side_effect=transport))
            stack.enter_context(patch.object(review, "record_review_round", return_value={}))
            # A new transport path must fail instead of contacting the network.
            stack.enter_context(patch.object(review.urllib.request, "urlopen",
                                             side_effect=AssertionError("unexpected network")))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            with self.assertRaises(SystemExit) as raised:
                review.main()
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
