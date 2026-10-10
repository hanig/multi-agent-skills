"""Offline size preflight exercises the same input gathering as a review."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "hanig-review-gate" / "scripts" / "review.py"
spec = importlib.util.spec_from_file_location("review_size_subject", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class TestSizePreflight(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        original = Path.cwd()
        os.chdir(self.temporary.name)
        self.addCleanup(os.chdir, original)
        self.git("init", "-q")
        self.git("config", "user.name", "Size Fixture")
        self.git("config", "user.email", "size@example.invalid")
        Path("subject.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "subject.txt")
        self.git("commit", "-qm", "base")
        Path("subject.txt").write_text("committed café\n", encoding="utf-8")
        self.git("commit", "-qam", "change")
        Path("subject.txt").write_text("staged café\n", encoding="utf-8")
        self.git("add", "subject.txt")
        Path("subject.txt").write_text("working café\n", encoding="utf-8")
        Path("notes.txt").write_text(
            review.PRIOR_RERUN_ANNOTATION + "\nContext café ☃\n", encoding="utf-8")
        Path("extra.txt").write_text("More context\n", encoding="utf-8")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], stderr=subprocess.STDOUT).decode("utf-8")

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            # An offline preflight must not even load provider configuration,
            # arm a review watchdog, or write a review receipt.
            guards = [stack.enter_context(patch.object(
                review, name, side_effect=AssertionError(name + " called")))
                for name in ("load_effective_reviewers", "arm_watchdog",
                             "record_review_round")]
            transport = stack.enter_context(patch.object(
                review.urllib.request, "urlopen",
                side_effect=AssertionError("network called")))
            stack.enter_context(patch.object(sys, "argv", [str(SCRIPT), *args]))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            with self.assertRaises(SystemExit) as exited:
                review.main()
            transport.assert_not_called()
            for guard in guards:
                guard.assert_not_called()
        return exited.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_size_equals_gather_for_every_source_and_combinations(self):
        cases = [
            ([], True, False, None, []),
            (["--diff"], True, False, None, []),
            (["--staged"], False, True, None, []),
            (["--range", "HEAD~1..HEAD"], False, False, "HEAD~1..HEAD", []),
            (["--file", "notes.txt", "--file", "extra.txt"],
             False, False, None, ["notes.txt", "extra.txt"]),
        ]
        for selector in ("--diff", "--staged", "--range"):
            flags = [selector] + (["HEAD~1..HEAD"] if selector == "--range" else [])
            cases.append((flags + ["--file", "notes.txt", "--file", "extra.txt"],
                          selector == "--diff", selector == "--staged",
                          "HEAD~1..HEAD" if selector == "--range" else None,
                          ["notes.txt", "extra.txt"]))
        for flags, diff, staged, range_spec, files in cases:
            with self.subTest(flags=flags):
                gathered, _ = review.gather(SimpleNamespace(
                    diff=diff, staged=staged, range=range_spec, file=files))
                code, output, _ = self.invoke("--size", "--json", *flags)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output), {
                    "characters": len(gathered), "planning_budget": 100000,
                    "truncation_limit": 180000})
                if files:
                    self.assertNotIn(review.PRIOR_RERUN_ANNOTATION, gathered)
                    self.assertIn("--- FILE: notes.txt ---", gathered)
                    self.assertNotEqual(len(gathered), len(self.git("diff", "HEAD")))
                    self.assertNotEqual(len(gathered), len(gathered.encode("utf-8")))

    def test_exit_codes_at_both_boundaries_count_before_truncation(self):
        header = "--- FILE: large.txt ---\n"
        for size, expected in ((99999, 0), (100000, 1), (100001, 1),
                               (179999, 1), (180000, 2), (180001, 2), (220000, 2)):
            with self.subTest(size=size):
                Path("large.txt").write_text("é" * (size - len(header)), encoding="utf-8")
                code, output, _ = self.invoke("--size", "--json", "--file", "large.txt")
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(output)["characters"], size)

    def test_text_output_names_all_three_counts(self):
        body, _ = review.gather(SimpleNamespace(
            diff=True, staged=False, range=None, file=[]))
        code, output, _ = self.invoke("--size", "--diff")
        self.assertEqual(code, 0)
        self.assertIn("{} characters".format(len(body)), output)
        self.assertIn("planning budget: 100000", output)
        self.assertIn("truncation limit: 180000", output)

    def test_empty_diff_is_zero_size_without_a_review_verdict(self):
        code, output, _ = self.invoke("--size", "--json", "--range", "HEAD..HEAD")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["characters"], 0)
        self.assertNotIn("REVIEW_PASS", output)

    def test_invalid_sources_and_conflicting_commands_stay_offline(self):
        for flags in (["--file", "missing.txt"], ["--range", "absent..HEAD"],
                      ["--diff", "--staged"], ["--list"], ["--open-findings"],
                      ["--adjudicate", "f" * 64]):
            with self.subTest(flags=flags):
                code, _output, _error = self.invoke("--size", *flags)
                self.assertEqual(code, 4)


if __name__ == "__main__":
    unittest.main()
