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

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}: \S")


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
        for reviewer in self.reviewers:
            if reviewer.get("max_output_tokens") is None:
                continue
            with self.subTest(reviewer=reviewer["name"]):
                record = reviewer.get("_max_output_tokens_accepted")
                self.assertIsInstance(
                    record, str,
                    "%s declares max_output_tokens with no "
                    "_max_output_tokens_accepted record" % reviewer["name"],
                )
                self.assertRegex(
                    record, ISO_DATE,
                    "%s's acceptance record must start with an ISO date and "
                    "then say what completed: %r" % (reviewer["name"], record),
                )
                self.assertIn(
                    str(reviewer["max_output_tokens"]), record,
                    "%s's acceptance record does not name the value it "
                    "accepts (%s)" % (reviewer["name"], reviewer["max_output_tokens"]),
                )
                self.assertIn(
                    reviewer["provider"], record,
                    "%s's acceptance record does not name the provider the "
                    "request went to" % reviewer["name"],
                )
                # Acceptance is per model, not per value or per provider.
                # 128000 completing for gpt-6-astra says nothing about
                # gpt-5.6-sol, and a record that names neither the model nor
                # a completion is a date with a number after it.
                self.assertIn(
                    reviewer["model"], record,
                    "%s's acceptance record does not name the model it was "
                    "measured on (%s); a value accepted by one model on a "
                    "provider is not evidence for another"
                    % (reviewer["name"], reviewer["model"]),
                )
                self.assertIn(
                    "completed", record,
                    "%s's acceptance record must say what completed, not "
                    "merely assert a value: %r" % (reviewer["name"], record),
                )

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
