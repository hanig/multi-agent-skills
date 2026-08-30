"""Reviewer profile eligibility, in ONE place.

Sol's fix for "a reviewer with no `profiles` key lands in every profile" was
applied to the normal selection path and not to `escalate()`, which kept the
inclusive spelling `not r.get("profiles") or tier in r["profiles"]`. That is
the path every escalated review takes, and every review in the session that
found this bug ran with --escalate.
"""
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
