"""ARC-763: code dispatch is pinned to one exact target observation.

Commit-identity cases ported selectively from ARC-680 attempt 8. No canary
qualification, intent migration, capacity, or dry-run repair is imported.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "hanig-swarm" / "scripts"))
import swarm as S
from tests.test_attempt_worktrees import FakePaseo

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="test",
               GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="test",
               GIT_COMMITTER_EMAIL="test@example.invalid")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env=GIT_ENV).stdout.strip()


class CommitIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.remote = self.tmp / "origin.git"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       env=GIT_ENV)
        (self.repo / "tracked.txt").write_text("base\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "branch", "-M", "main")
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)],
                       check=True, env=GIT_ENV)
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-qu", "origin", "main")
        self.target = git(self.repo, "rev-parse", "HEAD")
        self.plan = {"name": "commit-identity", "units": [{
            "id": "code", "kind": "code", "repo": str(self.repo),
            "target_branch": "main", "mode": "full-access",
            "prompt": "work", "outputs": ["evidence.md"]}]}
        self.fake = FakePaseo(self, self.tmp / "managed", S.U.run)

        def fake_run(argv, **kwargs):
            rc, out, err = self.fake(argv, **kwargs)
            if argv[:2] == ["paseo", "run"]:
                notice, payload = out.rsplit("\n", 1)
                data = json.loads(payload)
                data["agentId"] = "%032x" % len(self.fake.launches)
                out = notice + "\n" + json.dumps(data)
            return rc, out, err

        patcher = mock.patch.object(S.U, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_detached_head_at_target_commit_is_admitted(self):
        git(self.repo, "checkout", "-q", "--detach", self.target)

        result, state = self.run_advance()

        self.assertEqual(result[1], 1, result[0])
        self.assertIsNone(result[2], result[0])
        intent = state["units"]["code"]["attempt_launch_intents"][
            "attempt-1"]
        self.assertEqual(intent["base_commit"], self.target)
        self.assertEqual(intent["target_commit"], self.target)

    def test_checkout_ahead_on_another_branch_is_refused(self):
        git(self.repo, "checkout", "-qb", "topic")
        (self.repo / "tracked.txt").write_text("topic\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "topic")
        topic = git(self.repo, "rev-parse", "HEAD")

        result, state = self.run_advance()

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNotNone(result[2], result[0])
        self.assertIn(topic, " ".join(result[0]))
        self.assertIn(self.target, " ".join(result[0]))
        self.assertFalse(state["units"]["code"]["attempts"])

    def test_launch_is_bound_to_checked_commit_after_checkout_moves(self):
        moved = {"head": None}

        def allocate(_plan, _unit, _root):
            git(self.repo, "checkout", "-qb", "moved-after-check")
            (self.repo / "tracked.txt").write_text("moved\n")
            git(self.repo, "add", "tracked.txt")
            git(self.repo, "commit", "-qm", "moved")
            moved["head"] = git(self.repo, "rev-parse", "HEAD")
            path = self.tmp / "runs" / "code" / "attempt-1"
            path.mkdir(parents=True)
            return str(path), None

        result, state = self.run_advance(allocate=allocate)

        self.assertEqual(result[1], 1, result[0])
        intent = state["units"]["code"]["attempt_launch_intents"][
            "attempt-1"]
        self.assertNotEqual(moved["head"], self.target)
        self.assertEqual(intent["base_commit"], self.target)
        self.assertEqual(intent["target_commit"], self.target)
        argv = self.fake.launches[0]
        workspace = Path(argv[argv.index("--cwd") + 1])
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.target)
        self.assertEqual(str(workspace), state["units"]["code"][
            "attempt_launch_facts"]["attempt-1"]["execution_workspace"])

    def test_target_is_resolved_once_per_advance_and_bound_to_each_launch(self):
        second = dict(self.plan["units"][0])
        second["id"] = "code-two"
        second["target_branch"] = " main "
        subdir = self.repo / "nested-workspace"
        subdir.mkdir()
        second["execution_workspace"] = str(subdir)
        plan = {"name": "one-resolution", "units": [
            dict(self.plan["units"][0]), second]}
        state = {"units": {}}
        counter = {"n": 0}

        def allocate(_plan, unit, root):
            counter["n"] += 1
            if counter["n"] == 1:
                # Move only the remote target after admission. The second
                # launch in this advance must reuse the first observation.
                newer = git(self.repo, "commit-tree", "HEAD^{tree}",
                            "-p", self.target, "-m", "target moved")
                git(self.repo, "push", "-q", "origin",
                    newer + ":refs/heads/main")
            path = Path(root) / unit["id"] / ("a%d" % counter["n"])
            path.mkdir(parents=True)
            return str(path), None

        with mock.patch.object(S, "_allocate", side_effect=allocate), \
                mock.patch.object(
                    S, "_resolve_dispatch_target",
                    wraps=S._resolve_dispatch_target) as resolve, \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(S, "_bind", return_value=None), \
                mock.patch.object(
                    S, "_git_push_destination",
                    wraps=S._git_push_destination) as remote_query:
            result = S.advance(
                plan, state, str(self.tmp / "once-state"),
                str(self.tmp / "once-runs"), False)

        self.assertEqual(result[1], 2, result[0])
        self.assertEqual(resolve.call_count, 1)
        target_ref = "refs/heads/main"
        target_reads = [call for call in remote_query.call_args_list
                        if call.args and call.args[-1] == target_ref]
        self.assertEqual(len(target_reads), 1, remote_query.call_args_list)
        for uid in ("code", "code-two"):
            intents = state["units"][uid]["attempt_launch_intents"]
            intent = next(iter(intents.values()))
            self.assertEqual(intent["base_commit"], self.target)
            self.assertEqual(intent["target_commit"], self.target)

    def test_other_branch_at_target_is_admitted(self):
        git(self.repo, "checkout", "-qb", "same-commit")
        result, _state = self.run_advance()
        self.assertEqual(result[1], 1, result[0])

    def test_behind_and_diverged_checkouts_are_refused(self):
        (self.repo / "tracked.txt").write_text("target advanced\n")
        git(self.repo, "commit", "-qam", "advance target")
        newer = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", "--detach", self.target)
        for diverge in (False, True):
            with self.subTest(diverge=diverge):
                if diverge:
                    (self.repo / "tracked.txt").write_text("diverged\n")
                    git(self.repo, "commit", "-qam", "diverge")
                checkout = git(self.repo, "rev-parse", "HEAD")
                result, state = self.run_advance()
                self.assertEqual(result[1], 0, result[0])
                self.assertIn(checkout, " ".join(result[0]))
                self.assertIn(newer, " ".join(result[0]))
                self.assertEqual(state["units"]["code"]["attempts"], [])
        self.assertEqual(self.fake.launches, [])

    def test_direct_submit_refuses_non_target_checkout(self):
        git(self.repo, "checkout", "-qb", "topic")
        (self.repo / "tracked.txt").write_text("topic\n")
        git(self.repo, "commit", "-qam", "topic")
        attempt = self.tmp / "direct-attempt"
        attempt.mkdir()
        state = {"units": {}}
        job, error = S._submit(
            self.plan["units"][0], str(attempt), False, state,
            str(self.tmp / "state"))
        self.assertIsNone(job)
        self.assertIn(self.target, error)
        self.assertEqual(self.fake.launches, [])
        self.assertEqual(state, {"units": {}})

    def test_missing_target_does_not_fall_back_to_local_branch(self):
        git(self.remote, "update-ref", "-d", "refs/heads/main")
        result, state = self.run_advance()
        self.assertEqual(result[1], 0, result[0])
        self.assertIn("cannot resolve origin/main", " ".join(result[0]))
        self.assertEqual(state["units"]["code"]["attempts"], [])
        self.assertEqual(self.fake.launches, [])

    def test_dry_submit_does_not_write_persisted_state(self):
        attempt = self.tmp / "preview"
        attempt.mkdir()
        state_dir = self.tmp / "preview-state"
        original = {"schema_version": 1, "units": {}, "halted": None}
        S.save_state(str(state_dir), original)
        before = (state_dir / S.STATE_FILE).read_bytes()
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                if mismatch:
                    (self.repo / "tracked.txt").write_text("ahead\n")
                    git(self.repo, "commit", "-qam", "ahead")
                state = S.load_state(str(state_dir))
                _job, error = S._submit(
                    self.plan["units"][0], str(attempt), True,
                    state, str(state_dir))
                self.assertEqual(bool(error), mismatch)
                self.assertEqual((state_dir / S.STATE_FILE).read_bytes(), before)
                self.assertEqual(list(attempt.iterdir()), [])
        self.assertEqual(self.fake.launches, [])

    def test_legacy_inflight_attempt_is_judged_after_target_moves(self):
        # Launch via the real allocator and bind path; only Paseo itself is
        # replaced. The subsequent advance invokes the real unit.py checker.
        state_dir = self.tmp / "legacy-state"
        state = {"schema_version": 1, "units": {}, "halted": None}
        with mock.patch.object(S, "renew_lease", return_value=True):
            result = S.advance(self.plan, state, str(state_dir),
                               str(self.tmp / "legacy-runs"), False)
        self.assertEqual(result[1], 1, result[0])
        us = state["units"]["code"]
        attempt = Path(us["attempt_dir"])
        intent = us["attempt_launch_intents"][attempt.name]
        # The base shipped schema 5 without target_commit. No new required
        # field or migration may be imposed on these already-launched bytes.
        intent.pop("target_commit")
        legacy_intent = dict(intent)
        facts = us["attempt_launch_facts"][attempt.name]
        workspace = Path(facts["execution_workspace"])
        (workspace / "made.txt").write_text("made\n")
        git(workspace, "add", "made.txt")
        git(workspace, "commit", "-qm", "attempt work")
        produced = git(workspace, "rev-parse", "HEAD")
        git(workspace, "push", "-q", "origin",
            "HEAD:refs/heads/" + intent["branch"])
        (attempt / "evidence.md").write_text("done\n")
        (self.repo / "tracked.txt").write_text("target moved\n")
        git(self.repo, "commit", "-qam", "target moved")
        git(self.repo, "push", "-q", "origin", "main")
        self.assertNotEqual(intent["base_commit"],
                            git(self.repo, "rev-parse", "HEAD"))
        us["state"] = "RUNNING"
        S.save_state(str(state_dir), state)
        state = S.load_state(str(state_dir))
        before_preview = (state_dir / S.STATE_FILE).read_bytes()
        preview = S.advance(self.plan, state, str(state_dir),
                            str(self.tmp / "legacy-runs"), True)
        self.assertEqual(preview[1], 0, preview[0])
        self.assertEqual((state_dir / S.STATE_FILE).read_bytes(), before_preview)
        fakebin = self.tmp / "bin"
        fakebin.mkdir()
        paseo = fakebin / "paseo"
        paseo.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"status":"closed"}\'\n')
        paseo.chmod(0o755)
        with mock.patch.dict(os.environ, {
                "PATH": str(fakebin) + os.pathsep + os.environ["PATH"]}), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(S, "_check", wraps=S._check) as check, \
                mock.patch.object(S, "_resolve_dispatch_target",
                                  side_effect=AssertionError("rechecked target")):
            result = S.advance(self.plan, state, str(state_dir),
                               str(self.tmp / "legacy-runs"), False)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        durable = S.load_state(str(state_dir))["units"]["code"]
        self.assertEqual(durable["state"], "READY_FOR_PR",
                         (result[0], (attempt / "receipt.json").read_text()))
        self.assertEqual(durable["attempt_produced_heads"][attempt.name], produced)
        self.assertEqual(durable["attempt_launch_intents"][attempt.name],
                         legacy_intent)

    def test_stale_persisted_intent_is_not_rebased_for_new_dispatch(self):
        unit = self.plan["units"][0]
        attempt = self.tmp / "unsubmitted"
        attempt.mkdir()
        error, anchor = S._capture_code_launch(str(attempt), unit)
        self.assertIsNone(error)
        anchor["intent"].pop("target_commit")
        state_dir = self.tmp / "stale-state"
        state = {"units": {"code": {"attempt_launch_intents": {
            attempt.name: anchor["intent"]}}}}
        S.save_state(str(state_dir), state)
        before = (state_dir / S.STATE_FILE).read_bytes()
        (self.repo / "tracked.txt").write_text("target moved\n")
        git(self.repo, "commit", "-qam", "target moved")
        git(self.repo, "push", "-q", "origin", "main")
        state = S.load_state(str(state_dir))
        job, error = S._submit(unit, str(attempt), False, state, str(state_dir))
        self.assertIsNone(job)
        self.assertIn("recorded launch base " + self.target, error)
        self.assertEqual((state_dir / S.STATE_FILE).read_bytes(), before)
        self.assertEqual(self.fake.launches, [])

    def run_advance(self, allocate=None):
        state = {"schema_version": 1, "units": {}, "halted": None}
        allocated = self.tmp / "runs" / "code" / "attempt-1"

        def default_allocate(_plan, _unit, _root):
            allocated.mkdir(parents=True, exist_ok=True)
            return str(allocated), None

        with mock.patch.object(
                S, "_allocate", side_effect=allocate or default_allocate), \
                mock.patch.object(S, "_bind", return_value=None), \
                mock.patch.object(S, "renew_lease", return_value=True):
            result = S.advance(
                self.plan, state, str(self.tmp / "state"),
                str(self.tmp / "runs"), False)
        return result, state


if __name__ == "__main__":
    unittest.main()
