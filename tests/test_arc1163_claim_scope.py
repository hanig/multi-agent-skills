"""ARC-1163: an explicit honest-run refutation cannot be scoped into a pass.

The wrongful-refusal positive control runs first through real main, parsing,
aggregation and audit serialization. Only transports and external audit writes
are replaced. Fixtures are self-contained; no historical receipt is required.
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills/hanig-review-gate/scripts/review.py"
SPEC = importlib.util.spec_from_file_location("arc1163_review", SCRIPT)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)

COUNTER_CLAIM = "This change cannot make an honest run fail."
EXIT_ASSERTION = "[arc1163-counter-claim-exit]"
POLICY = "Independent acceptance criteria admit both UTC and local."
BOUNDARY = "All zone refusals are deliberately outside this review's scope."
SOURCE = '''"""Deliberately refuse UTC despite the independent acceptance criteria."""

def accept_zone(zone):
    if zone != "local":
        raise ValueError("zone refused")
    return zone
'''
REFUTATION = "accept_zone wrongly refuses UTC, which the independent acceptance criteria explicitly admit."


class TestCounterClaimScope(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "candidate.py"
        self.source.write_text(SOURCE)

    def invoke(self, escalated=False, json_output=True, claim=COUNTER_CLAIM):
        roster = [
            {"name": "refuter", "provider": "openai", "model": "offline-refuter",
             "profiles": ["fast", "standard", "deep"], "enabled": True},
            {"name": "supporter", "provider": "openrouter", "model": "offline-supporter",
             "profiles": ["fast", "standard", "deep"], "enabled": True},
            {"name": "later", "provider": "openai", "model": "offline-later",
             "profiles": ["standard", "deep"], "enabled": True},
        ]
        calls, audits = [], []

        def transport(seat, prompt, _timeout, **_kwargs):
            self.assertIn(POLICY, prompt)
            self.assertIn(BOUNDARY, prompt)
            self.assertIn(SOURCE, prompt)
            self.assertIn(COUNTER_CLAIM, prompt)
            calls.append(seat["name"])
            assessment = {"claim_index": 0, "claim": COUNTER_CLAIM,
                          "status": "supported",
                          "why": "The documented local admission path returns the expected value."}
            if seat["name"] == "refuter":
                assessment.update(status="refuted", why=REFUTATION, in_scope=False,
                                  scope_reason="The caller's boundary excludes all zone refusals, including UTC.")
            response = {"verdict": "upheld", "findings": [], "notes": "",
                        "claims": [assessment]}
            return {"text": json.dumps(response)}, None

        def audit(_files, line):
            audits.append(json.loads(line))
            return {"path": None, "written": False, "status": "offline test sink"}

        argv = [str(SCRIPT), "--kind", "implementation", "--round", "1",
                "--file", str(self.source), "--claim", claim, "--context", POLICY,
                "--threat-model", BOUNDARY, "--quorum", "2"]
        if json_output:
            argv.append("--json")
        if escalated:
            argv.extend(["--escalate", "--profile", "fast"])
        else:
            argv.extend(["--only", "refuter,supporter"])
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ))
            for credential in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
                os.environ.pop(credential, None)
            stack.enter_context(patch("sys.argv", argv))
            stack.enter_context(patch.object(review, "load_reviewers", return_value=roster))
            stack.enter_context(patch.object(review, "availability", return_value=None))
            stack.enter_context(patch.dict(review.PROVIDERS, {"openai": transport, "openrouter": transport}))
            stack.enter_context(patch.object(review, "arm_watchdog"))
            stack.enter_context(patch.object(review, "disarm_watchdog"))
            stack.enter_context(patch.object(review, "_run_journal_append", side_effect=audit))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            with self.assertRaises(SystemExit) as stopped:
                review.main()
        return stopped.exception.code, stdout.getvalue(), stderr.getvalue(), calls, audits

    def assert_blocked(self, result):
        exit_code, output, errors, calls, audits = result
        self.assertEqual(exit_code, 7, EXIT_ASSERTION)
        report = json.loads(output)
        self.assertEqual(errors, "")
        self.assertEqual(report["state"], "REVIEW_CLAIMS_REFUTED")
        self.assertEqual(report["completed"], 2)
        self.assertEqual(report["quorum"], 2)
        self.assertCountEqual(calls, ["refuter", "supporter"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["unavailable"], [])
        self.assertFalse(report["truncated"])
        self.assertEqual(report["confirmed_findings"], [])
        self.assertEqual(report["rejecting_reviewers"], [])
        self.assertEqual(len(report["refuted_claims"]), 1)
        assessment = report["refuted_claims"][0]
        self.assertEqual(assessment["claim"], COUNTER_CLAIM)
        self.assertEqual(assessment["why"], REFUTATION)
        self.assertIs(assessment["in_scope"], False)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["verdict"], report["state"])
        self.assertEqual(audits[0]["refuted_claims"], report["refuted_claims"])
        return report

    def test_000_wrongful_refusal_with_false_scope_still_blocks(self):
        namespace = {}
        exec(compile(self.source.read_text(), str(self.source), "exec"), namespace)
        self.assertEqual(namespace["accept_zone"]("local"), "local")
        with self.assertRaisesRegex(ValueError, "zone refused"):
            namespace["accept_zone"]("UTC")
        self.assert_blocked(self.invoke())

    def test_010_escalation_stops_on_the_same_refutation(self):
        report = self.assert_blocked(self.invoke(escalated=True))
        self.assertEqual(report["tiers_run"], ["fast"])

    def test_020_text_retains_the_refutation(self):
        exit_code, output, errors, calls, audits = self.invoke(json_output=False)
        self.assertEqual(exit_code, 7, EXIT_ASSERTION)
        self.assertEqual(errors, "")
        self.assertIn("REFUTED CLAIMS:", output)
        self.assertIn(COUNTER_CLAIM, output)
        self.assertIn(REFUTATION, output)
        self.assertIn("REVIEW_CLAIMS_REFUTED", output)
        self.assertCountEqual(calls, ["refuter", "supporter"])
        self.assertEqual(audits[0]["verdict"], "REVIEW_CLAIMS_REFUTED")

    def test_030_caller_cannot_qualify_the_counter_claim(self):
        result = self.invoke(claim=COUNTER_CLAIM + " Except documented refusals.")
        exit_code, _output, errors, calls, audits = result
        self.assertEqual(exit_code, 4)
        self.assertIn("required counter-claim", errors)
        self.assertEqual(calls, [])
        self.assertEqual(audits, [])


if __name__ == "__main__":
    unittest.main()
