"""Reviewer profile eligibility, in ONE place.

Sol's fix for "a reviewer with no `profiles` key lands in every profile" was
applied to the normal selection path and not to `escalate()`, which kept the
inclusive spelling `not r.get("profiles") or tier in r["profiles"]`. That is
the path every escalated review takes, and every review in the session that
found this bug ran with --escalate.
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "skills" / "hanig-review-gate" / "scripts" / "review.py"
sys.path.insert(0, str(REVIEW.parent))
import review as R  # noqa: E402


class TestOneEligibilityRule(unittest.TestCase):

    def test_a_missing_profiles_key_is_in_no_profile(self):
        self.assertFalse(R.in_profile({"name": "x"}, "fast"))
        for tier in R.LADDER:
            self.assertFalse(R.in_profile({"name": "x"}, tier), tier)

    def test_an_empty_profiles_list_is_also_in_no_profile(self):
        """Declaring membership in nothing is not membership in everything."""
        self.assertFalse(R.in_profile({"name": "x", "profiles": []}, "fast"))

    def test_explicit_membership_is_included(self):
        self.assertTrue(R.in_profile({"name": "x", "profiles": ["fast"]},
                                     "fast"))

    def test_explicit_non_membership_is_excluded(self):
        self.assertFalse(R.in_profile({"name": "x", "profiles": ["deep"]},
                                      "fast"))

    def test_no_function_tests_profile_membership_on_its_own(self):
        """The bug was one fix applied to one of two call sites.

        Targets MEMBERSHIP TESTING specifically, not any mention of the field:
        `load_reviewers` reads `profiles` to type-check it, which is
        legitimate and must not be forced through the eligibility rule. What
        must not recur is a second place deciding for itself whether a
        reviewer belongs to a profile.
        """
        import ast
        offenders = []
        for node in ast.walk(ast.parse(REVIEW.read_text())):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name == "in_profile":
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Compare):
                    continue
                if not any(isinstance(o, ast.In) for o in sub.ops):
                    continue
                if "profiles" in ast.unparse(sub):
                    offenders.append("%s:%d" % (node.name, sub.lineno))
        self.assertEqual(offenders, [],
                         "these decide profile membership themselves instead "
                         "of calling in_profile: %s" % sorted(offenders))


class TestEscalationOverAnEmptyLadderRefuses(unittest.TestCase):
    """Zero reviewers returning zero findings is indistinguishable from a
    clean review, which is the worst possible way to fail."""

    class _Args:
        quorum = 2
        json = False
        timeout = 60
        watchdog = None

    def test_a_roster_with_no_tiers_is_refused(self):
        roster = [{"name": "a", "provider": "openai", "model": "m",
                   "enabled": True}]
        with self.assertRaises(SystemExit):
            R.escalate(roster, "prompt", self._Args(), False, "label", 10)

    def test_an_empty_profiles_roster_is_refused(self):
        roster = [{"name": "a", "provider": "openai", "model": "m",
                   "enabled": True, "profiles": []}]
        with self.assertRaises(SystemExit):
            R.escalate(roster, "prompt", self._Args(), False, "label", 10)

    def test_the_shipped_roster_has_at_least_one_tier(self):
        import json
        cfg = json.loads((REVIEW.parent.parent / "reviewers.json").read_text())
        self.assertTrue(
            any(R.in_profile(r, t) for t in R.LADDER
                for r in cfg["reviewers"]),
            "the shipped roster must be able to escalate")


class TestShippedLadderAddsReviewers(unittest.TestCase):
    def test_each_enabled_tier_is_a_strict_superset_of_the_previous_tier(self):
        import json
        cfg = json.loads((REVIEW.parent.parent / "reviewers.json").read_text())

        def enabled_members(profile):
            return {
                reviewer["name"]
                for reviewer in cfg["reviewers"]
                if reviewer.get("enabled", True)
                and R.in_profile(reviewer, profile)
            }

        for lower, upper in zip(R.LADDER, R.LADDER[1:]):
            with self.subTest(lower=lower, upper=upper):
                self.assertLess(
                    enabled_members(lower), enabled_members(upper),
                    "%s must add at least one enabled reviewer beyond %s"
                    % (upper, lower))


class TestAuthorExclusion(unittest.TestCase):
    """Exercise argparse and selection through the reviewers actually called."""

    def invoke(self, *flags, roster=None, kind="implementation"):
        argv = [str(REVIEW), "--kind", kind, "--file", str(REVIEW)]
        if kind == "implementation":
            argv += ["--round", "1", "--claim", R.HONEST_RUN_CLAIM]
        argv += list(flags)
        called = []

        def answer(reviewer, *_args):
            called.append(reviewer["name"])
            return {"name": reviewer["name"], "ok": True, "verdict": "upheld",
                    "findings": [], "claims": [], "elapsed_s": 0}

        roster = R.load_reviewers() if roster is None else roster
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(R, "load_reviewers", return_value=roster), \
                patch.object(R, "DEFAULT_PROFILE", "standard"), \
                patch.object(R, "availability", side_effect=lambda r:
                             None if r.get("enabled", True) else "disabled"), \
                patch.object(R, "run_one", side_effect=answer), \
                patch.object(R, "gather", return_value=("diff", "offline diff")), \
                patch.object(R, "arm_watchdog"), \
                patch.object(R, "disarm_watchdog"), \
                patch.object(R, "record_review_round", return_value={}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                R.main()
        return stopped.exception.code, stdout.getvalue(), stderr.getvalue(), called

    def test_deep_excludes_sol_author_and_names_removal(self):
        code, output, _err, called = self.invoke(
            "--profile", "deep", "--quorum", "3",
            "--author", "codex/gpt-5.6-sol")
        self.assertNotIn("sol", called)
        self.assertEqual(set(called), {"luna", "kimi-k2.7-code", "glm-5.3"})
        self.assertEqual(code, R.STATES["REVIEW_PASS"], output)
        self.assertIn("sol excluded: authored this change", output)

    def test_exclusion_below_quorum_is_unavailable_before_any_call(self):
        for flags in (["--profile", "deep"], ["--escalate"],
                      ["--only", "sol,luna,kimi-k2.7-code,glm-5.3"]):
            with self.subTest(flags=flags):
                code, output, _err, called = self.invoke(
                    *flags, "--quorum", "4", "--author", "codex/gpt-5.6-sol",
                    "--json")
                self.assertEqual(code, R.STATES["REVIEW_UNAVAILABLE"], output)
                report = json.loads(output)
                self.assertEqual(report["quorum"], 4)
                self.assertIn("author exclusion leaves 3", report["reason"])
                self.assertEqual(report["excluded"][0]["name"], "sol")
                self.assertEqual(called, [])

    def test_escalation_does_not_reintroduce_the_author(self):
        code, output, _err, called = self.invoke(
            "--escalate", "--quorum", "3", "--author", "codex/gpt-5.6-sol",
            "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(set(called), {"luna", "kimi-k2.7-code", "glm-5.3"})
        self.assertEqual(json.loads(output)["excluded"][0]["name"], "sol")

    def test_repeatable_authors_preserve_nested_model_ids(self):
        code, output, _err, called = self.invoke(
            "--profile", "deep", "--author", "codex/gpt-5.6-sol",
            "--author", "openrouter/moonshotai/kimi-k2.7-code", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(set(called), {"luna", "glm-5.3"})
        self.assertEqual({r["name"] for r in json.loads(output)["excluded"]},
                         {"sol", "kimi-k2.7-code"})

    def test_model_equality_does_not_match_substrings_case_or_seat_names(self):
        for author in ("codex/my-gpt-5.6-sol-helper", "codex/GPT-5.6-SOL",
                       "codex/sol", "codex/gpt-5.6-sol-extra"):
            with self.subTest(author=author):
                code, output, _err, called = self.invoke(
                    "--profile", "deep", "--author", author, "--json")
                self.assertEqual(code, 0, output)
                self.assertIn("sol", called)
                self.assertEqual(json.loads(output)["excluded"], [])

    def test_different_effort_seats_of_same_model_are_both_excluded(self):
        code, output, _err, called = self.invoke(
            "--only", "astra,astra-xhigh,luna,kimi-k2.7-code",
            "--author", "codex/gpt-6-astra", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(set(called), {"luna", "kimi-k2.7-code"})
        self.assertEqual({r["name"] for r in json.loads(output)["excluded"]},
                         {"astra", "astra-xhigh"})

    def test_all_selected_authors_cannot_run_even_with_singleton_override(self):
        code, output, _err, called = self.invoke(
            "--only", "sol", "--quorum", "1", "--allow-single-reviewer",
            "diagnostic", "--author", "codex/gpt-5.6-sol")
        self.assertEqual(code, R.STATES["REVIEW_UNAVAILABLE"], output)
        self.assertEqual(called, [])

    def test_fresh_cycle_floor_survives_author_exclusion(self):
        code, output, _err, called = self.invoke(
            "--profile", "deep", "--fresh-cycle-from", "deep",
            "--author", "codex/gpt-5.6-sol", "--json")
        self.assertEqual(code, R.STATES["REVIEW_UNAVAILABLE"], output)
        self.assertEqual(json.loads(output)["quorum"], 4)
        self.assertEqual(called, [])

    def test_plan_cannot_drop_its_author_and_pass_with_one(self):
        code, output, _err, called = self.invoke(
            "--author", "codex/gpt-5.6-luna", kind="plan")
        self.assertEqual(code, R.STATES["REVIEW_UNAVAILABLE"], output)
        self.assertIn("required quorum is 2", output)
        self.assertEqual(called, [])

    def test_no_author_keeps_the_deep_panel(self):
        code, output, _err, called = self.invoke("--profile", "deep")
        self.assertEqual(code, 0, output)
        self.assertIn("sol", called)
        self.assertNotIn("excluded", output)

    def test_malformed_author_is_a_usage_error(self):
        for author in ("sol", "/gpt-5.6-sol", "codex/", "codex/ gpt-5.6-sol"):
            with self.subTest(author=author):
                code, _out, err, called = self.invoke("--author", author)
                self.assertEqual(code, R.STATES["REVIEW_ERROR"])
                self.assertIn("PROVIDER/MODEL", err)
                self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
