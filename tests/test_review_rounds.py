"""Stable identities and enforcing reservations, through Git, CLI and races."""

import concurrent.futures
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
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

    @contextmanager
    def held_lock(self):
        self.reserve(1)
        lock = self.markers()[0].parent / "lock"
        code = """
import fcntl, sys
with open(sys.argv[1], 'r+') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    print('locked', flush=True)
    sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(lock)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline(), "locked\n")
            yield process
        finally:
            process.communicate("release\n", timeout=10)
            self.assertEqual(process.returncode, 0)

    def test_valid_next_round_waits_for_concurrent_holder(self):
        with self.held_lock() as holder:
            original = review.fcntl.flock
            timers = []

            def release():
                holder.stdin.write("release\n")
                holder.stdin.flush()

            def contended(*args):
                if not timers:
                    # Release only after the consumer actually reaches flock.
                    timer = threading.Timer(0.2, release)
                    timers.append(timer)
                    timer.start()
                return original(*args)

            try:
                with patch.object(review.fcntl, "flock", side_effect=contended):
                    result = self.invoke(2)
            finally:
                for timer in timers:
                    timer.join()
        self.assertEqual(result[0], 0, result[2])
        self.assertEqual(len(result[3]), 2)
        self.assertEqual([json.loads(p.read_text())["round"] for p in self.markers()], [1, 2])
        repeated = self.invoke(2)
        self.assertEqual(repeated[0], 4, repeated[2])
        self.assertEqual((repeated[3], repeated[4]), ([], []))

    def test_lock_timeout_is_a_bounded_exit_4_refusal(self):
        with self.held_lock(), patch.object(review, "ROUND_LOCK_WAIT_SECONDS", 0.05):
            result = self.invoke(2)
        self.assertEqual(result[0], 4, result[2])
        self.assertIn("lock wait", result[2])
        self.assertEqual((result[3], result[4]), ([], []))
        self.assertEqual(len(self.markers()), 1)

    def test_lone_last_round_is_not_exhaustion(self):
        for number in range(1, review.MAX_ROUNDS + 1):
            self.reserve(number)
        for marker in self.markers()[:-1]:
            marker.unlink()
        before = {p: p.read_bytes() for p in self.markers()}
        result = self.invoke(extra=("--fresh-cycle-from", "standard"))
        self.assertEqual(result[0], 4, result[2])
        self.assertIn("missing rounds: 1, 2", result[2])
        self.assertEqual((result[3], result[4]), ([], []))
        self.assertEqual(before, {p: p.read_bytes() for p in self.markers()})

    def test_exhaustion_requires_each_round_of_the_declared_profile(self):
        self.reserve(1, profile="fast")
        with self.assertRaisesRegex(ValueError, "profile"):
            self.reserve(2)
        # Build the formerly admitted persisted shape by hand: subsequent
        # readers must refuse it too, before either transport or audit writes.
        one = self.markers()[0]
        record = json.loads(one.read_text())
        for number in (2, 3):
            marker = one.with_name("cycle-1-round-{}.json".format(number))
            marker.write_text(json.dumps(dict(record, round=number, profile="standard")))
        result = self.invoke(extra=("--fresh-cycle-from", "standard"))
        self.assertEqual(result[0], 4, result[2])
        self.assertIn("profiles differ", result[2])
        self.assertEqual((result[3], result[4]), ([], []))

    def test_existing_successor_with_incomplete_predecessor_is_refused(self):
        for number in range(1, review.MAX_ROUNDS + 1):
            self.reserve(number)
        self.reserve(1, fresh="standard")
        self.markers()[0].unlink()
        result = self.invoke(2, ("--fresh-cycle-from", "standard"))
        self.assertEqual(result[0], 4, result[2])
        self.assertIn("missing rounds: 1", result[2])
        self.assertEqual((result[3], result[4]), ([], []))

    def test_next_round_requires_immediately_previous_reservation(self):
        for number, missing in ((2, 1), (3, 2)):
            result = self.invoke(number)
            self.assertEqual(result[0], 4, result[2])
            self.assertIn("missing round {}".format(missing), result[2])
            self.assertEqual((result[3], result[4]), ([], []))
        self.reserve(1)
        result = self.invoke(3)
        self.assertEqual(result[0], 4, result[2])
        self.assertIn("missing round 2", result[2])
        self.assertEqual(len(self.markers()), 1)

    def test_unresolvable_home_is_exit_4_with_environment_guidance(self):
        with patch.dict(os.environ):
            os.environ.pop("HOME", None)
            os.environ.pop("XDG_STATE_HOME", None)
            with patch.object(review.Path, "home", side_effect=RuntimeError("no home")):
                result = self.invoke()
        self.assertEqual(result[0], 4, result[2])
        self.assertIn("XDG_STATE_HOME", result[2])
        self.assertIn("HOME", result[2])
        self.assertNotIn("Traceback", result[2])
        self.assertEqual((result[3], result[4]), ([], []))

    def test_unusable_state_home_and_home_are_exit_4(self):
        unavailable = self.root / "file-not-directory"
        unavailable.write_text("unusable home")
        for state_home in ("", "relative-state", str(unavailable)):
            with self.subTest(state_home=state_home), patch.dict(
                    os.environ, {"XDG_STATE_HOME": state_home, "HOME": str(unavailable)}):
                result = self.invoke()
            self.assertEqual(result[0], 4, result[2])
            self.assertIn("XDG_STATE_HOME", result[2])
            self.assertIn("HOME", result[2])
            self.assertNotIn("Traceback", result[2])
            self.assertEqual((result[3], result[4]), ([], []))

    def test_fresh_cycle_on_unseen_identity_records_no_predecessor(self):
        result = self.invoke(extra=("--fresh-cycle-from", "standard"))
        self.assertEqual(result[0], 0, result[2])
        record = json.loads(self.markers()[0].read_text())
        self.assertEqual(record["cycle"], 1)
        self.assertEqual(record["predecessor"],
                         {"cycle": "none declared", "profile": "standard"})
        self.assertEqual(self.invoke(2)[0], 4, "the declared panel floor must persist")
        self.assertEqual(self.invoke(2, ("--fresh-cycle-from", "standard"))[0], 0)
        self.assertEqual(self.invoke(3, ("--fresh-cycle-from", "standard"))[0], 0)
        following = self.reserve(1, fresh="standard")
        self.assertEqual(following["cycle"], 2)
        self.assertEqual(following["predecessor"], {"cycle": 1, "profile": "standard"})

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
        self.reserve(1)
        for number in (1, 2):
            with self.assertRaises(ValueError):
                self.reserve(number, fresh="standard")
        self.reserve(2)
        self.reserve(3)
        with self.assertRaises(ValueError):
            self.reserve(1, fresh="fast")

    def test_lower_round_is_refused_even_without_its_marker(self):
        for number in range(1, review.MAX_ROUNDS + 1):
            self.reserve(number)
        for marker in self.markers()[:-1]:
            marker.unlink()
        result = self.invoke(2)
        self.assertEqual(result[0], 4, result[2])
        self.assertEqual((result[3], result[4]), ([], []))
        self.assertEqual(len(self.markers()), 1)

    def test_state_home_may_use_a_system_or_user_directory_alias(self):
        actual = self.root / "actual-state"
        actual.mkdir()
        alias = self.root / "alias-state"
        alias.symlink_to(actual, target_is_directory=True)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(alias)}):
            self.reserve(1)
            with self.assertRaises(ValueError):
                self.reserve(1)
        self.assertEqual(len(list(actual.glob("hanig-review-gate/rounds/*/*.json"))), 1)

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

    def test_directory_fsync_failure_removes_marker_and_allows_retry(self):
        ledger = review.round_ledger_root() / review.change_identity()
        marker = ledger / "cycle-1-round-1.json"
        original = review.os.fsync
        error = OSError("injected unsupported directory fsync")

        def reject_directory_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise error
            return original(fd)

        with patch.object(review.os, "fsync", side_effect=reject_directory_fsync):
            result = self.invoke(1)
        self.assertEqual(result[0], 4, result[2])
        self.assertEqual((result[3], result[4]), ([], []))
        with self.subTest("marker removed before retry"):
            self.assertFalse(marker.exists())
        retry = self.invoke(1)
        self.assertEqual(retry[0], 0, "immediate round-1 retry failed: " + retry[2])
        self.assertIn(str(ledger), result[2])
        self.assertIn(str(error), result[2])
        self.assertIn("reservation was not recorded", result[2])

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

    def test_malformed_predecessor_is_refused_before_dispatch(self):
        self.reserve(1)
        marker = self.markers()[0]
        record = json.loads(marker.read_text())
        record["predecessor"] = {"cycle": 0, "profile": "standard"}
        marker.write_text(json.dumps(record))
        result = self.invoke(2, ("--fresh-cycle-from", "standard"))
        self.assertEqual(result[0], 4, result[2])
        self.assertEqual((result[3], result[4]), ([], []))
        self.assertEqual(len(self.markers()), 1)

    def test_stored_history_checks_actual_predecessor_and_exact_field_types(self):
        for number in range(1, review.MAX_ROUNDS + 1):
            self.reserve(number)
        self.reserve(1, fresh="standard")
        markers = self.markers()
        saved = {p: p.read_bytes() for p in markers}
        for value in (None, {"cycle": True, "profile": "standard"},
                      {"cycle": 1, "profile": "fast"},
                      {"cycle": 99, "profile": "standard"}):
            with self.subTest(predecessor=value):
                record = json.loads(saved[markers[-1]])
                record["predecessor"] = value
                markers[-1].write_text(json.dumps(record))
                self.assertEqual(self.invoke(2, ("--fresh-cycle-from", "standard"))[0], 4)
        markers[-1].write_bytes(saved[markers[-1]])
        # Removing the actual predecessor is different from malformed metadata.
        markers[0].unlink()
        self.assertEqual(self.invoke(2, ("--fresh-cycle-from", "standard"))[0], 4)

    def test_noninteger_and_oversized_marker_fields_fail_closed(self):
        self.reserve(1)
        marker = self.markers()[0]
        saved = json.loads(marker.read_text())
        for key in ("cycle", "round"):
            record = dict(saved, **{key: True})
            marker.write_text(json.dumps(record))
            self.assertEqual(self.invoke(2)[0], 4)
        marker.write_text(json.dumps(saved) + " " * 4096 + "broken trailing data")
        self.assertEqual(self.invoke(2)[0], 4)
        marker.write_text('{"predecessor": {"cycle": 0},' + json.dumps(saved)[1:])
        self.assertEqual(self.invoke(2)[0], 4)

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
            self.reserve(2)
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



class TestClosedRoundGrammar(unittest.TestCase):
    setUp = TestRoundReservations.setUp
    git = TestRoundReservations.git
    markers = TestRoundReservations.markers
    reserve = TestRoundReservations.reserve
    held_lock = TestRoundReservations.held_lock

    def invoke(self, number=1, profile="standard", fresh=None):
        calls = self.root / "calls.txt"
        env = dict(os.environ, XDG_STATE_HOME=str(self.state),
                   OPENAI_API_KEY="fixture-key", OPENROUTER_API_KEY="fixture-key",
                   HANIG_ROUTING_SNAPSHOTS="off", ROUND_CLI_CALLS=str(calls))
        env["ROUND_CLI_LOCK_READY"] = str(self.root / "lock-ready")
        # A mutation runner can supply an isolated source copy without changing
        # the checkout. The production CLI and reservation path remain intact.
        script = os.environ.get("ROUND_CLI_SOURCE", str(SCRIPT))
        argv = [sys.executable, str(Path(__file__).with_name("review_round_cli.py")),
                script, "--kind", "implementation", "--round", str(number),
                "--author", "codex/gpt-6-astra", "--file", "code.py", "--json",
                "--claim", review.HONEST_RUN_CLAIM, "--profile", profile,
                "--dispositions", str(self.dispositions)]
        if fresh is not None:
            argv += ["--fresh-cycle-from", fresh]
        return subprocess.run(argv, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=25)

    def marker(self, cycle, number, profile="standard", predecessor=None):
        identity = review.change_identity()
        directory = self.state / "hanig-review-gate/rounds" / identity
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "cycle-{}-round-{}.json".format(cycle, number)
        path.write_text(json.dumps(dict(identity=identity, cycle=cycle,
                                       round=number, profile=profile,
                                       predecessor=predecessor)))
        return path

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and "state" in p.parts
                and p.name != "lock"}

    def assert_refused(self, number, names, profile="standard", fresh=None):
        before = self.snapshot()
        calls = self.root / "calls.txt"
        before_calls = calls.read_bytes() if calls.exists() else b""
        result = self.invoke(number, profile, fresh)
        self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
        for name in names:
            self.assertIn(name, result.stderr)
        self.assertEqual(before, self.snapshot(), "refusal changed ledger or journal")
        self.assertEqual(before_calls, calls.read_bytes() if calls.exists() else b"",
                         "refusal reached a provider")
        return result

    def assert_admitted(self, number, profile="standard", fresh=None):
        result = self.invoke(number, profile, fresh)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertTrue(report["journal"]["written"], report)
        return result

    def test_lone_round_two_refuses_round_three(self):
        path = self.marker(1, 2)
        self.assert_refused(3, [path.name, "cycle-1-round-1.json"])

    def test_profile_change_refuses_immediately(self):
        self.assert_admitted(1)
        self.assert_refused(2, ["cycle-1-round-1.json"], profile="deep")

    def test_hand_created_mixed_cycle_refuses_next(self):
        one = self.marker(1, 1)
        two = self.marker(1, 2, profile="deep")
        self.assert_refused(3, [one.name, two.name], profile="deep")

    def test_incomplete_predecessor_refuses_existing_successor(self):
        self.marker(1, 1)
        self.marker(1, 2)
        two = self.marker(2, 1, predecessor={"cycle": 1, "profile": "standard"})
        self.assert_refused(2, [two.name, "cycle-1-round-3.json"], fresh="standard")

    def test_incomplete_cycle_refuses_new_successor(self):
        self.assert_admitted(1)
        self.assert_refused(1, ["cycle-1-round-2.json", "cycle-1-round-3.json"],
                            fresh="standard")

    def test_stray_entry_refuses(self):
        one = self.marker(1, 1)
        stray = one.with_name("notes.txt")
        stray.write_text("unregistered history")
        self.assert_refused(2, [stray.name])

    def test_valid_rounds_and_successor(self):
        for number in (1, 2, 3):
            self.assert_admitted(number)
        self.assert_admitted(1, profile="deep", fresh="standard")
        self.assert_admitted(2, profile="deep", fresh="standard")
        last = json.loads(self.markers()[-1].read_text())
        self.assertEqual(last["cycle"], 2)
        self.assertEqual(last["predecessor"], {"cycle": 1, "profile": "standard"})

    def test_repeated_lower_and_skipped_rounds_refuse(self):
        self.assert_refused(2, ["cycle-1-round-1.json"])
        self.assert_admitted(1)
        self.assert_refused(1, ["cycle-1-round-1.json"])
        self.assert_refused(3, ["cycle-1-round-2.json"])
        self.assert_admitted(2)
        self.assert_refused(1, ["cycle-1-round-2.json"])

    def test_missing_cycle_and_wrong_predecessors_refuse(self):
        path = self.marker(2, 1, predecessor={"cycle": 1, "profile": "standard"})
        self.assert_refused(2, [path.name, "cycle-1-round-1.json"], fresh="standard")
        path.unlink()
        for number in (1, 2, 3):
            self.marker(1, number)
        for predecessor in (None, {"cycle": True, "profile": "standard"},
                            {"cycle": 1, "profile": "deep"},
                            {"cycle": 0, "profile": "standard"}):
            with self.subTest(predecessor=predecessor):
                path = self.marker(2, 1, predecessor=predecessor)
                self.assert_refused(2, [path.name], fresh="standard")

    def test_malformed_markers_name_the_file(self):
        path = self.marker(1, 1)
        good = json.loads(path.read_text())
        malformed = [dict(good, extra=1), dict(good, identity="0" * 64),
                     dict(good, round=True), dict(good, cycle=True),
                     dict(good, round=2), dict(good, profile=[]),
                     dict(good, predecessor={"cycle": "none declared"})]
        payloads = [json.dumps(value) for value in malformed]
        payloads += ["{}", "[]", "null", "{", " " * 4097,
                     '{"round": 2,' + json.dumps(good)[1:]]
        for payload in payloads:
            with self.subTest(payload=payload[:80]):
                path.write_text(payload)
                self.assert_refused(2, [path.name])
        path.write_bytes(b"\xff")
        self.assert_refused(2, [path.name])

    def test_nonregular_entries_refuse_without_blocking(self):
        path = self.marker(1, 1)
        saved = path.read_bytes()
        path.unlink()
        for kind in ("directory", "symlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    path.mkdir()
                elif kind == "symlink":
                    target = self.root / "target.json"
                    target.write_bytes(saved)
                    path.symlink_to(target)
                else:
                    os.mkfifo(path)
                self.assert_refused(2, [path.name])
                if kind == "directory":
                    path.rmdir()
                else:
                    path.unlink()

    def test_brief_lock_contention_delays_valid_cli(self):
        with self.held_lock() as holder:
            # The harness records a real BlockingIOError from flock. Waiting
            # for that event distinguishes contention from slow CLI startup.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.invoke, 2)
                deadline = time.monotonic() + 10
                ready = self.root / "lock-ready"
                while not ready.exists() and not pending.done() and time.monotonic() < deadline:
                    time.sleep(0.01)
                contended = ready.exists()
                time.sleep(0.2)
                still_waiting = not pending.done()
                holder.stdin.write("release\n")
                holder.stdin.flush()
                result = pending.result(timeout=25)
        self.assertTrue(contended, result.stderr)
        self.assertTrue(still_waiting, result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.markers()), 2)

    def test_lock_holder_past_bound_refuses_cli(self):
        with self.held_lock():
            started = time.monotonic()
            self.assert_refused(2, ["lock"])
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, review.ROUND_LOCK_WAIT_SECONDS)
        self.assertLess(elapsed, 20)


if __name__ == "__main__":
    unittest.main()
