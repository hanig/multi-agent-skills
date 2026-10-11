"""Stable identities and enforcing reservations, through Git, CLI and races."""

import concurrent.futures
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/hanig-review-gate/scripts/review.py"
SPEC = importlib.util.spec_from_file_location("round_review", SCRIPT)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


class TestRoundReservations(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        original = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, original)
        self.state = self.root / "state"
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        self.git("init", "-q")
        self.git("config", "user.name", "Round Fixture")
        self.git("config", "user.email", "round@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("remote", "add", "origin", "https://example.invalid/repo.git")
        Path("code.py").write_text("value = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("checkout", "-qb", "change")
        self.dispositions = self.root / "dispositions.json"
        self.dispositions.write_text("{}")

    def git(self, *args):
        return subprocess.check_output(["git", *args], stderr=subprocess.STDOUT).decode().strip()

    def fix(self):
        with Path("code.py").open("a") as stream:
            stream.write("# fix\n")
        self.git("commit", "-qam", "fix")

    def reserve(self, number, profile="standard", fresh=None, identity=None):
        return review.reserve_round(identity or review.change_identity(), number, profile, fresh)

    def markers(self):
        return sorted((self.state / "hanig-review-gate/rounds").glob("*/*.json"))

    def invoke(self, number=1, extra=(), fail_journal=False):
        calls, records = [], []
        roster = [{"name": n, "model": n, "provider": "stub",
                   "profiles": ["standard"], "enabled": True} for n in ("a", "b")]

        def answer(reviewer, *_args):
            calls.append(reviewer["name"])
            self.assertTrue(self.markers(), "provider ran before reservation")
            return {"ok": True, "name": reviewer["name"], "verdict": "upheld",
                    "findings": [], "claims": [], "elapsed_s": 0}

        def journal(_files, line):
            records.append(line)
            if fail_journal:
                raise OSError("offline journal failure")
            return {"written": True, "path": "offline audit sink", "error": None}

        argv = [str(SCRIPT), "--kind", "implementation", "--round", str(number),
                "--author", "codex/gpt-6-astra", "--file", "code.py",
                "--dispositions", str(self.dispositions), "--profile", "standard",
                "--claim", review.HONEST_RUN_CLAIM, "--json", *extra]
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            for name, replacement in (("load_effective_reviewers", lambda: roster),
                                      ("availability", lambda _r: None),
                                      ("run_one", answer),
                                      ("arm_watchdog", lambda *_a: None),
                                      ("disarm_watchdog", lambda: None),
                                      ("_run_journal_append", journal)):
                stack.enter_context(patch.object(review, name, replacement))
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            with self.assertRaises(SystemExit) as stopped:
                review.main()
        return stopped.exception.code, out.getvalue(), err.getvalue(), calls, records

    def test_identity_survives_fix_commits_dirty_edits_and_target_advance(self):
        identity = review.change_identity()
        self.fix()
        self.assertEqual(review.change_identity(), identity)
        Path("code.py").write_text("uncommitted fix\n")
        self.assertEqual(review.change_identity(), identity)
        # Advance the target on its own line: the merge-base stays at base.
        target = subprocess.check_output(["git", "commit-tree", self.base + "^{tree}",
                                          "-p", self.base, "-m", "target advance"]).decode().strip()
        self.git("update-ref", "refs/remotes/origin/main", target)
        self.assertEqual(review.change_identity(), identity)

    def test_remote_merge_base_branch_and_plan_unit_are_distinct(self):
        identities = {review.change_identity()}
        for kwargs in ({"plan_name": "plan", "unit": "one"},
                       {"plan_name": "plan", "unit": "two"},
                       {"plan_name": "other", "unit": "one"},
                       {"plan_name": "plan"}, {"unit": "one"}):
            identities.add(review.change_identity(**kwargs))
        self.git("branch", "-m", "different")
        identities.add(review.change_identity())
        self.git("remote", "set-url", "origin", "https://example.invalid/other.git")
        identities.add(review.change_identity())
        self.fix()
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        identities.add(review.change_identity())
        self.assertEqual(len(identities), 9)
        for identity in identities:
            self.reserve(1, identity=identity)
        self.assertEqual(len(self.markers()), 9)

    def test_declared_plan_unit_survives_branch_rename_and_detached_head(self):
        kwargs = {"plan_name": "plan", "unit": "unit"}
        identity = review.change_identity(**kwargs)
        self.git("branch", "-m", "renamed")
        self.assertEqual(review.change_identity(**kwargs), identity)
        self.git("checkout", "--detach", "-q")
        self.assertEqual(review.change_identity(**kwargs), identity)
        with self.assertRaises(ValueError):
            review.change_identity()

    def test_cli_refuses_repeated_and_lower_round_after_fix_commits(self):
        for number in (1, 2, 3):
            result = self.invoke(number)
            self.assertEqual(result[0], 0, result[2])
            self.assertEqual(len(result[3]), 2)
            self.fix()
        before = {p: p.read_bytes() for p in self.markers()}
        for number in (1, 2, 3):
            code, _out, err, calls, records = self.invoke(number)
            self.assertEqual(code, 4, err)
            self.assertIn("already reserved or lower", err)
            self.assertEqual((calls, records), ([], []))
        self.assertEqual(before, {p: p.read_bytes() for p in self.markers()})

    def test_fresh_cycle_records_predecessor_and_preserves_old_markers(self):
        for number in (1, 2, 3):
            self.reserve(number)
        before = {p: p.read_bytes() for p in self.markers()}
        result = self.invoke(extra=("--fresh-cycle-from", "standard"))
        self.assertEqual(result[0], 0, result[2])
        latest = [json.loads(p.read_text()) for p in self.markers()
                  if "cycle-2" in p.name]
        self.assertEqual(latest[0]["predecessor"], {"cycle": 1, "profile": "standard"})
        self.assertEqual(latest[0]["round"], 1)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(self.invoke(2)[0], 4, "omitting predecessor cannot evade its panel floor")
        for number in (2, 3):
            self.assertEqual(self.invoke(number, ("--fresh-cycle-from", "standard"))[0], 0)
        third = self.reserve(1, fresh="standard")
        self.assertEqual(third["cycle"], 3)
        self.assertEqual(third["predecessor"]["cycle"], 2)

    def test_fresh_cycle_requires_exhaustion_and_exact_profile(self):
        with self.assertRaises(ValueError):
            self.reserve(1, fresh="standard")
        self.reserve(1)
        for number in (1, 2):
            with self.assertRaises(ValueError):
                self.reserve(number, fresh="standard")
        self.reserve(3)
        with self.assertRaises(ValueError):
            self.reserve(1, fresh="fast")

    def test_exclusive_marker_rejects_a_precreated_concurrent_winner(self):
        """Inject a winner after the ledger scan, at the O_EXCL boundary.

        Removing O_EXCL overwrites the winner and makes this assertion fail.
        A pre-scan duplicate test alone would not detect that mutation.
        """
        original = review.os.open
        winner = b'{"concurrent winner": true}\n'
        injected = []

        def competing_open(name, flags, *args, **kwargs):
            if name == "cycle-1-round-1.json" and not injected:
                fd = original(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, *args, **kwargs)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(winner)
                injected.append(True)
            return original(name, flags, *args, **kwargs)

        with patch.object(review.os, "open", side_effect=competing_open):
            result = self.invoke()
        self.assertEqual(result[0], 4, result[2])
        self.assertEqual((result[3], result[4]), ([], []))
        self.assertEqual(self.markers()[0].read_bytes(), winner)

    def test_two_processes_get_one_reservation_and_one_refusal(self):
        code = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('subject', sys.argv[1])
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)
try:
    review.reserve_round(review.change_identity(), 1, 'standard')
except (ValueError, OSError):
    sys.exit(4)
"""
        def run():
            return subprocess.run([sys.executable, "-c", code, str(SCRIPT)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=30)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _n: run(), range(2)))
        self.assertEqual(sorted(r.returncode for r in results), [0, 4],
                         [r.stderr for r in results])
        self.assertEqual(len(self.markers()), 1)

    def test_unwritable_or_partial_ledger_refuses_before_providers_and_audit(self):
        self.state.write_text("not a directory")
        result = self.invoke()
        self.assertEqual(result[0], 4)
        self.assertEqual((result[3], result[4]), ([], []))
        self.state.unlink()
        self.reserve(1)
        self.markers()[0].write_text('{"incomplete":')
        result = self.invoke(2)
        self.assertEqual(result[0], 4)
        self.assertEqual((result[3], result[4]), ([], []))

    def test_journal_failure_is_non_gating_and_does_not_release_reservation(self):
        result = self.invoke(fail_journal=True)
        self.assertEqual(result[0], 0, result[2])
        self.assertIn("JOURNAL_WRITE_FAILED", result[2])
        self.assertEqual(self.invoke()[0], 4)

    def test_reservations_never_read_or_modify_journal_bytes(self):
        journal = self.state / "hanig-review-gate" / review.JOURNAL_NAME / "record.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_bytes(b"unparseable audit history\n")
        before = journal.stat()
        with patch.object(review, "read_review_journal", side_effect=AssertionError("audit read")), \
                patch.object(review, "record_review_round", side_effect=AssertionError("audit write")):
            self.reserve(1)
            self.reserve(3)
            self.reserve(1, fresh="standard")
        self.assertEqual(journal.read_bytes(), b"unparseable audit history\n")
        self.assertEqual(journal.stat().st_mtime_ns, before.st_mtime_ns)

    def test_invalid_identity_cli_refuses_without_writes(self):
        for flags in (("--target", "missing"), ("--plan-name", ""), ("--unit", "  ")):
            result = self.invoke(extra=flags)
            self.assertEqual(result[0], 4, result[2])
            self.assertEqual((result[3], result[4]), ([], []))
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
