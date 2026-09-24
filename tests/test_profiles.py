"""Reviewer profile eligibility, in ONE place.

Sol's fix for "a reviewer with no `profiles` key lands in every profile" was
applied to the normal selection path and not to `escalate()`, which kept the
inclusive spelling `not r.get("profiles") or tier in r["profiles"]`. That is
the path every escalated review takes, and every review in the session that
found this bug ran with --escalate.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
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


class TestChangedPathProfiles(unittest.TestCase):
    """Drive real Git inputs through main and inspect the panel it calls."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name).resolve()
        previous = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Profile Test")
        self.doc = "docs/x.md"
        self.code = "skills/hanig-swarm/scripts/swarm.py"
        self.write(self.doc, "base\n")
        self.write(self.code, "base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")

    def git(self, *args):
        return subprocess.check_output(["git", *args], stderr=subprocess.PIPE)

    def write(self, name, text="changed\n"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def invoke(self, *flags, failing=False, unavailable=False):
        argv = [str(REVIEW), "--kind", "implementation", "--round", "1",
                "--claim", R.HONEST_RUN_CLAIM, "--author", "codex/gpt-6-astra",
                *flags]
        called = []

        def answer(reviewer, *_args):
            called.append(reviewer["name"])
            return {"name": reviewer["name"], "ok": True, "elapsed_s": 0,
                    "verdict": "refuted" if failing else "upheld",
                    "findings": [], "claims": ([{"status": "refuted"}]
                                                if failing else [])}

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(R, "availability", side_effect=lambda r:
                             "offline" if unavailable or not r.get("enabled", True)
                             else None), \
                patch.object(R, "run_one", side_effect=answer), \
                patch.object(R, "arm_watchdog"), \
                patch.object(R, "disarm_watchdog"), \
                patch.object(R, "record_review_round", return_value={}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                R.main()
        return stopped.exception.code, stdout.getvalue(), stderr.getvalue(), called

    def report(self, *flags, **kwargs):
        code, output, err, called = self.invoke(*flags, "--json", **kwargs)
        self.assertEqual(code, 0, output + err)
        return json.loads(output), called

    def test_docs_code_override_and_unknown_acceptance(self):
        self.write(self.doc)
        report, called = self.report("--diff")
        self.assertEqual(report["profile"], "fast")
        self.assertEqual(set(called), {"luna", "kimi-k2.7-code"})
        self.write(self.code)
        report, called = self.report("--diff")
        self.assertEqual(report["profile"], "standard")
        self.assertEqual(set(called), {"luna", "kimi-k2.7-code", "glm-5.3"})
        self.write(self.code, "base\n")
        report, _ = self.report("--diff", "--profile", "standard")
        self.assertEqual(report["profile"], "standard")
        self.assertEqual(report["profile_reason"], "explicit --profile")
        with patch.object(R, "changed_paths", return_value=(None, "lookup failed")):
            report, _ = self.report("--diff")
        self.assertEqual(report["profile"], "standard")
        self.assertIn("lookup failed", report["profile_reason"])

    def test_implicit_diff_and_text_verdict_explain_the_tier(self):
        self.write(self.doc)
        code, output, err, _ = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertIn("tier: fast (all 1 changed paths are documentation)", output)
        self.assertIn("REVIEW_PASS [tier: fast", output)

    def test_staged_input_ignores_unstaged_code(self):
        self.write(self.doc)
        self.git("add", self.doc)
        self.write(self.code)
        report, _ = self.report("--staged")
        self.assertEqual(report["profile"], "fast")
        report, _ = self.report("--diff")
        self.assertEqual(report["profile"], "standard")
        self.git("add", self.code)
        report, _ = self.report("--staged")
        self.assertEqual(report["profile"], "standard")

    def test_range_uses_git_endpoints_and_ignores_working_tree(self):
        self.write(self.doc)
        self.git("commit", "-qam", "docs")
        self.write(self.code)
        for spec in ("HEAD~1..HEAD", "HEAD~1...HEAD", "HEAD^!"):
            with self.subTest(spec=spec):
                report, _ = self.report("--range", spec)
                self.assertEqual(report["profile"], "fast")
        self.git("commit", "-qam", "code")
        report, _ = self.report("--range", "HEAD~1..HEAD")
        self.assertEqual(report["profile"], "standard")

    def test_explicit_files_relative_absolute_and_from_subdirectory(self):
        for name in (self.doc, str(self.repo / self.doc)):
            report, _ = self.report("--file", name)
            self.assertEqual(report["profile"], "fast")
        report, _ = self.report("--file", self.doc, "--file", self.code)
        self.assertEqual(report["profile"], "standard")
        os.chdir(self.repo / "docs")
        report, _ = self.report("--file", "x.md")
        self.assertEqual(report["profile"], "fast")

    def test_relative_git_config_cannot_hide_a_sensitive_directory(self):
        self.write("lib/README.md", "base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "library documentation")
        self.write("lib/README.md")
        self.git("add", "lib/README.md")
        self.git("config", "diff.relative", "true")
        os.chdir(self.repo / "lib")
        for source in ("--diff", "--staged"):
            report, _ = self.report(source)
            self.assertEqual(report["profile"], "standard", source)
        self.git("commit", "-qm", "change library documentation")
        report, _ = self.report("--range", "HEAD~1..HEAD")
        self.assertEqual(report["profile"], "standard")

    def test_file_and_directory_symlinks_outside_root_are_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp).resolve()
            (external / "README.md").write_text("external documentation\n")
            (self.repo / "docs-link").symlink_to(external, target_is_directory=True)
            (self.repo / "docs/link.md").symlink_to(external / "README.md")
            for name in ("docs-link/README.md", "docs/link.md"):
                report, _ = self.report("--file", name)
                self.assertEqual(report["profile"], "standard", name)
                self.assertIn("outside the review root", report["profile_reason"])

    def test_inside_symlinks_keep_both_lexical_and_target_classifications(self):
        for name, target, expected in (("docs/code.md", self.code, "standard"),
                                       ("scripts/doc.md", self.doc, "standard"),
                                       ("docs/alias.md", self.doc, "fast")):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(self.repo / target)
            report, _ = self.report("--file", name)
            self.assertEqual(report["profile"], expected, name)

    def test_file_inputs_are_unioned_with_diff_and_deduplicated(self):
        self.write(self.doc)
        report, _ = self.report("--diff", "--file", self.doc)
        self.assertEqual(report["profile_reason"], "all 1 changed paths are documentation")
        report, _ = self.report("--diff", "--file", self.code)
        self.assertEqual(report["profile"], "standard")

    def test_renamed_and_deleted_code_cannot_hide_behind_docs(self):
        self.git("mv", self.code, "docs/former-code.md")
        report, _ = self.report("--staged")
        self.assertEqual(report["profile"], "standard")
        self.git("reset", "--hard", "HEAD")
        (self.repo / self.code).unlink()
        report, _ = self.report("--diff")
        self.assertEqual(report["profile"], "standard")

    def test_paths_use_nul_boundaries_and_preserve_unicode(self):
        for name in ("docs/line\nbreak.md", "résumé.md", "space name.md"):
            self.write(name)
            self.git("add", name)
        report, _ = self.report("--staged")
        self.assertEqual(report["profile_reason"], "all 3 changed paths are documentation")
        self.write("odd\nname.py")
        self.git("add", "odd\nname.py")
        report, _ = self.report("--staged")
        self.assertEqual(report["profile"], "standard")

    def test_documentation_globs_and_sensitive_locations(self):
        for name in ("README.md", "docs/guide.txt", "examples/config.json",
                     "skills/hanig-review-gate/SKILL.md"):
            self.write(name)
            report, _ = self.report("--file", name)
            self.assertEqual(report["profile"], "fast", name)
        for name in ("scripts/guide.md", "lib/readme.md", "bin/help.md",
                     "tests/guide.md", "skills/demo/scripts/guide.md",
                     "skills/hanig-review-gate/reviewers.json",
                     "examples/reviewers.json", "config.json", "app.py"):
            self.write(name)
            report, _ = self.report("--file", name)
            self.assertEqual(report["profile"], "standard", name)

    def test_unknown_git_paths_do_not_discard_file_input_risk(self):
        args = type("Args", (), {"diff": True, "staged": False, "range": None,
                                 "file": [self.doc]})()
        for result in (subprocess.CompletedProcess([], 1, b"", b"error"),
                       subprocess.CompletedProcess([], 0, b"docs/x.md", b""),
                       subprocess.CompletedProcess([], 0, b"bad\xff\0", b"")):
            with patch.object(R.subprocess, "run", return_value=result):
                paths, reason = R.changed_paths(args)
            self.assertIsNone(paths)
            self.assertEqual(R.profile_for_paths(paths, reason)[0], "standard")
        with patch.object(R.subprocess, "run", side_effect=OSError("no git")):
            self.assertIsNone(R.changed_paths(args)[0])

    def test_empty_paths_and_unresolved_file_paths_are_conservative(self):
        self.assertEqual(R.profile_for_paths(set())[0], "standard")
        report, _ = self.report("--file", "docs/../docs/x.md")
        self.assertEqual(report["profile"], "standard")
        self.assertIn("parent components", report["profile_reason"])

    def test_explicit_profile_skips_detection_and_can_choose_fast_on_code(self):
        self.write(self.code)
        with patch.object(R, "changed_paths", side_effect=AssertionError("lookup")):
            report, called = self.report("--diff", "--profile", "fast")
        self.assertEqual(report["profile"], "fast")
        self.assertEqual(len(called), 2)

    def test_escalation_starts_at_selected_tier_and_adds_only_new_reviewers(self):
        self.write(self.doc)
        for flags, expected in (([], ["fast", "standard", "deep"]),
                                (["--profile", "standard"], ["standard", "deep"]),
                                (["--profile", "deep"], ["deep"])):
            report, called = self.report("--diff", "--escalate", *flags)
            self.assertEqual(report["tiers_run"], expected)
            self.assertEqual(len(called), len(set(called)))
            self.assertEqual(set(called), {"luna", "kimi-k2.7-code", "glm-5.3", "sol"})
        self.write(self.code)
        report, _ = self.report("--diff", "--escalate")
        self.assertEqual(report["tiers_run"], ["standard", "deep"])

    def test_standard_escalation_reviews_full_starting_panel_before_stopping(self):
        self.write(self.code)
        code, output, err, called = self.invoke("--diff", "--escalate", "--json",
                                               failing=True)
        self.assertEqual(code, R.STATES["REVIEW_CLAIMS_REFUTED"], err)
        self.assertEqual(json.loads(output)["tiers_run"], ["standard"])
        self.assertEqual(set(called), {"luna", "kimi-k2.7-code", "glm-5.3"})

    def test_automatic_fast_cannot_relax_author_or_fresh_cycle_floors(self):
        self.write(self.doc)
        code, output, _, called = self.invoke("--diff", "--json", "--author",
                                              "codex/gpt-5.6-luna")
        self.assertEqual(code, R.STATES["REVIEW_UNAVAILABLE"])
        self.assertEqual(json.loads(output)["profile"], "fast")
        self.assertEqual(called, [])
        code, _, _, called = self.invoke("--diff", "--fresh-cycle-from", "standard")
        self.assertEqual(code, R.STATES["REVIEW_ERROR"])
        self.assertEqual(called, [])

    def test_unavailable_panel_still_explains_selection(self):
        self.write(self.doc)
        code, output, _, called = self.invoke("--diff", "--json", unavailable=True)
        self.assertEqual(code, R.STATES["REVIEW_UNAVAILABLE"])
        report = json.loads(output)
        self.assertEqual(report["profile"], "fast")
        self.assertIn("documentation", report["profile_reason"])
        self.assertEqual(called, [])


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
