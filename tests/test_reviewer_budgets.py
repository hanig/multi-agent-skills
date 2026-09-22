"""An enabled reviewer may not carry an output budget nothing has tried.

Three times now a reviewer has spent its entire output budget on reasoning
and returned no content. Each time the round still reached quorum, because a
reviewer that returns nothing is counted as present: the log presents three
reviewers and two opinions decide. glm-5.3 did it first, then kimi-k2.7-code
did it in two consecutive rounds of one unit (ARC-689).

Each time the answer was to raise a number, and twice the raise was not swept
to the siblings. The gate on the third raise objected, correctly, that a
larger number is asserted rather than validated: a provider whose ceiling is
lower rejects the request, and the reviewer contributes nothing for a
different reason.

So this file enforces two things about the config, neither of which is about
the size of the number:

  1. an ENABLED reviewer declares its budget explicitly, because the shared
     default is the value that starved two reviewers, and
  2. the declared value carries a dated record of a real request that
     completed at it.

What it deliberately does NOT do is call a provider. That would make the
suite depend on network and on credentials. The record is the evidence, and
it is only as honest as whoever wrote it -- which is why it must name the
request, not merely assert a date.
"""

import json
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO_ROOT, "skills", "hanig-review-gate", "reviewers.json")

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REQUIRED_RECORD_FIELDS = (
    "date", "max_output_tokens", "provider", "model", "outcome", "output_tokens")


def load():
    with open(CONFIG) as handle:
        return json.load(handle)


class ReviewerOutputBudgets(unittest.TestCase):
    def setUp(self):
        self.config = load()
        self.reviewers = self.config["reviewers"]
        self.enabled = [r for r in self.reviewers if r.get("enabled")]

    def test_there_is_at_least_one_enabled_reviewer(self):
        # Otherwise every assertion below is vacuously true.
        self.assertTrue(self.enabled, "no reviewer is enabled")

    def test_every_enabled_reviewer_declares_its_own_output_budget(self):
        """Inheriting DEFAULT_MAX_OUTPUT_TOKENS is not permitted once enabled.

        64000 is not a neutral default. It is the value two reasoning
        reviewers measurably starved at, and starvation is counted as
        participation, so the failure is silent.
        """
        for reviewer in self.enabled:
            with self.subTest(reviewer=reviewer["name"]):
                self.assertIsInstance(
                    reviewer.get("max_output_tokens"), int,
                    "%s is enabled but inherits the shared default; measure it "
                    "and declare max_output_tokens before enabling it"
                    % reviewer["name"],
                )

    def test_every_declared_budget_names_a_request_that_completed_at_it(self):
        """The record is structured data, compared field by field.

        It was free text matched by substring for one round, and the gate
        refused that correctly: luna's counterexample, "completed a
        1280000-token request via openrouterx for z-ai/glm-5.3-variant",
        satisfied `"128000" in record`, `"openrouter" in record` and
        `"z-ai/glm-5.3" in record` while documenting a different value, a
        different provider and a different model. Exact-token matching would
        have closed that hole and kept the shape, which is a human sentence
        a test parses. So the record carries typed fields and English
        commentary stays in _max_output_tokens.
        """
        for reviewer in self.reviewers:
            if reviewer.get("max_output_tokens") is None:
                continue
            with self.subTest(reviewer=reviewer["name"]):
                record = reviewer.get("_max_output_tokens_accepted")
                self.assertIsInstance(
                    record, dict,
                    "%s declares max_output_tokens with no structured "
                    "_max_output_tokens_accepted record" % reviewer["name"],
                )
                for field in REQUIRED_RECORD_FIELDS:
                    self.assertIn(field, record,
                                  "%s's acceptance record is missing %s"
                                  % (reviewer["name"], field))
                self.assertRegex(
                    str(record["date"]), ISO_DATE,
                    "%s's acceptance record needs an ISO date"
                    % reviewer["name"])
                # Exact equality against the reviewer's own configuration.
                # A record for a neighbouring value, provider or model is
                # evidence about a request that was never made here.
                self.assertEqual(
                    record["max_output_tokens"], reviewer["max_output_tokens"],
                    "%s's acceptance record documents %r, but the reviewer is "
                    "configured for %r"
                    % (reviewer["name"], record["max_output_tokens"],
                       reviewer["max_output_tokens"]))
                self.assertEqual(record["provider"], reviewer["provider"],
                                 reviewer["name"])
                self.assertEqual(record["model"], reviewer["model"],
                                 reviewer["name"])
                self.assertEqual(
                    record["outcome"], "completed",
                    "%s's acceptance record must record a completed request"
                    % reviewer["name"])
                # A provider ACCEPTING a budget is not the same fact as a
                # reviewer RETURNING usable output, and it was the second
                # fact that motivated raising these budgets at all.
                self.assertIsInstance(record["output_tokens"], int,
                                      reviewer["name"])
                self.assertGreater(
                    record["output_tokens"], 0,
                    "%s's acceptance record shows no output; an accepted "
                    "request that returns nothing is the defect being fixed, "
                    "not evidence against it" % reviewer["name"])

    def test_a_declared_budget_explains_itself(self):
        """Measured or pre-emptive, the file has to say which.

        A raise recorded without its reason is how the same symptomatic fix
        gets applied a fourth time.
        """
        for reviewer in self.reviewers:
            if reviewer.get("max_output_tokens") is None:
                continue
            with self.subTest(reviewer=reviewer["name"]):
                note = reviewer.get("_max_output_tokens")
                self.assertIsInstance(note, str, reviewer["name"])
                self.assertTrue(
                    note.startswith("MEASURED") or note.startswith("PRE-EMPTIVE"),
                    "%s's _max_output_tokens note must open with MEASURED or "
                    "PRE-EMPTIVE: %r" % (reviewer["name"], note[:60]),
                )

    def test_the_rule_is_stated_in_the_file_it_governs(self):
        rule = self.config.get("_output_budget_rule")
        self.assertIsInstance(
            rule, str,
            "the config must carry the rule, so someone enabling a reviewer "
            "reads it there rather than discovering it from a test failure",
        )
        self.assertIn("test_reviewer_budgets.py", rule,
                      "the rule must name its enforcer")


if __name__ == "__main__":
    unittest.main()
