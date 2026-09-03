"""C11: every code attempt executes in its own Paseo-managed worktree."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402
import worktree as W  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          env=ENV, capture_output=True, text=True).stdout.strip()


def repo_at(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=ENV)
    (path / "tracked.txt").write_text("base\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path


def code_unit(repo, uid="code"):
    return {"id": uid, "kind": "code", "repo": str(repo),
            "prompt": "work", "mode": "full-access"}


class FakePaseo:
    def __init__(self, owner, root, real_run):
        self.owner = owner
        self.root = root
        self.real_run = real_run
        self.launches = []

    def __call__(self, argv, **kwargs):
        if argv[:2] != ["paseo", "run"]:
            return self.real_run(argv, **kwargs)
        self.launches.append(list(argv))
        source = Path(argv[argv.index("--cwd") + 1])
        slug = argv[argv.index("--worktree-slug") + 1]
        branch = argv[argv.index("--new-branch") + 1]
        base = argv[argv.index("--base") + 1]
        workspace = self.root / slug
        subprocess.run(["git", "-C", str(source), "worktree", "add", "-q",
                        "-b", branch, str(workspace), base], check=True,
                       env=ENV, capture_output=True, text=True)
        out = (f"Created workspace wks_{slug} - fixture\n" + json.dumps(
            {"agentId": f"agent-{slug}", "cwd": str(workspace)}))
        return 0, out, ""


class TestPerAttemptWorktrees(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = repo_at(self.tmp / "repo")
        self.real_run = S.U.run
        self.fake = FakePaseo(self, self.tmp / "managed", self.real_run)
        S.U.run = self.fake
        self.addCleanup(setattr, S.U, "run", self.real_run)

    def attempt(self, uid, attempt):
        path = self.tmp / "runs" / uid / attempt
        path.mkdir(parents=True)
        return path

    def intent_state(self, attempt, uid="code"):
        unit = code_unit(self.repo, uid)
        err, anchored = S._capture_code_launch(str(attempt), unit)
        self.assertIsNone(err)
        return unit, {"units": {uid: {
            "attempt_bases": {attempt.name: anchored["base"]},
            "attempt_launch_intents": {attempt.name: anchored["intent"]},
        }}}

    def test_paseo_argv_uses_trusted_base_and_records_returned_worktree(self):
        attempt = self.attempt("code", "a1")
        before = git(self.repo, "rev-parse", "HEAD")
        state = {"units": {}}
        moved = {}
        fake = self.fake

        def move_ref_then_launch(argv, **kwargs):
            if argv[:2] == ["paseo", "run"]:
                (self.repo / "later.txt").write_text("later\n")
                git(self.repo, "add", "-A")
                git(self.repo, "commit", "-qm", "later")
                moved["head"] = git(self.repo, "rev-parse", "HEAD")
            return fake(argv, **kwargs)

        S.U.run = move_ref_then_launch
        job, err = S._submit(code_unit(self.repo), str(attempt), False, state)
        self.assertIsNone(err)
        self.assertEqual(job, "agent-a1")
        argv = fake.launches[0]
        self.assertEqual(argv[argv.index("--new-workspace") + 1], "worktree")
        self.assertEqual(argv[argv.index("--worktree-mode") + 1], "branch-off")
        self.assertEqual(argv[argv.index("--worktree-slug") + 1], "a1")
        self.assertEqual(argv[argv.index("--new-branch") + 1], "swarm-a1")
        self.assertEqual(argv[argv.index("--base") + 1], before)
        self.assertNotEqual(before, moved["head"])
        facts = state["units"]["code"]["attempt_launch_facts"]["a1"]
        self.assertEqual(facts["base_commit"], before)
        self.assertEqual(facts["execution_workspace"],
                         str((self.tmp / "managed" / "a1").resolve()))
        self.assertNotEqual(facts["execution_workspace"], facts["repo"])

    def test_same_and_different_units_receive_distinct_worktrees(self):
        state = {"units": {}}
        cases = (("u1", "a1"), ("u1", "a2"), ("u2", "a3"))
        paths = []
        for uid, attempt_id in cases:
            job, err = S._submit(code_unit(self.repo, uid),
                                 str(self.attempt(uid, attempt_id)), False,
                                 state)
            self.assertIsNone(err)
            self.assertTrue(job)
            paths.append(state["units"][uid]["attempt_launch_facts"]
                         [attempt_id]["execution_workspace"])
        self.assertEqual(len(set(paths)), 3)

    def test_dirty_shared_checkout_does_not_block_a_code_attempt(self):
        (self.repo / "human-edit.txt").write_text("uncommitted\n")
        state = {"units": {}}
        job, err = S._submit(code_unit(self.repo),
                             str(self.attempt("code", "dirty-shared")),
                             False, state)
        self.assertIsNone(err)
        self.assertEqual(job, "agent-dirty-shared")
        workspace = Path(state["units"]["code"]["attempt_launch_facts"]
                         ["dirty-shared"]["execution_workspace"])
        self.assertFalse((workspace / "human-edit.txt").exists())
        self.assertEqual(git(workspace, "status", "--porcelain"), "")

    def test_judging_reads_the_execution_worktree(self):
        attempt = self.attempt("code", "judge")
        state = {"units": {}}
        _job, err = S._submit(code_unit(self.repo), str(attempt), False, state)
        self.assertIsNone(err)
        facts = state["units"]["code"]["attempt_launch_facts"]["judge"]
        workspace = Path(facts["execution_workspace"])
        (workspace / "made.txt").write_text("made\n")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-qm", "attempt work")
        produced, why = W.judge(S.U.run, str(attempt),
                                code_unit(self.repo), facts)
        self.assertTrue(produced, why)
        self.assertFalse((self.repo / "made.txt").exists())

    def test_dry_run_never_asks_paseo_to_create_a_worktree(self):
        state = {"units": {}}
        job, err = S._submit(code_unit(self.repo),
                             str(self.attempt("code", "dry")), True, state)
        self.assertIsNone(err)
        self.assertTrue(job.startswith("dry-"))
        self.assertEqual(self.fake.launches, [])
        self.assertFalse((self.tmp / "managed").exists())
        self.assertNotIn("attempt_launch_facts", state["units"]["code"])

    def test_terminal_cleanup_archives_workspace_but_keeps_branch(self):
        attempt = self.attempt("code", "cleanup")
        state = {"units": {}}
        _job, err = S._submit(code_unit(self.repo), str(attempt), False, state)
        self.assertIsNone(err)
        workspace = self.tmp / "managed" / "cleanup"
        archived = []
        fake = self.fake

        def archive_spy(argv, **kwargs):
            if argv[:3] == ["paseo", "workspace", "archive"]:
                archived.append(list(argv))
                git(self.repo, "worktree", "remove", "--force", str(workspace))
                return 0, "{}", ""
            return fake(argv, **kwargs)

        S.U.run = archive_spy
        report = []
        S._archive_code_worktree(state, code_unit(self.repo), str(attempt),
                                 report)
        self.assertEqual(len(archived), 1)
        self.assertFalse(workspace.exists())
        self.assertEqual(git(self.repo, "rev-parse", "swarm-cleanup"),
                         git(self.repo, "rev-parse", "HEAD"))
        meta = state["units"]["code"]["attempt_workspaces"]["cleanup"]
        self.assertTrue(meta["archived"])

    def test_returned_worktree_on_wrong_branch_is_refused_unrecorded(self):
        attempt = self.attempt("code", "wrong-branch")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "wrong-branch-worktree"
        git(self.repo, "worktree", "add", "-q", "-b", "some-other-branch",
            str(workspace), intent["base_commit"])
        error = S._complete_code_launch(state, unit, str(attempt), workspace)
        self.assertIn("not trusted attempt branch", error)
        self.assertNotIn("attempt_launch_facts", state["units"]["code"])
        self.assertNotIn("attempt_workspaces", state["units"]["code"])
        self.assertFalse(W.launch_record_path(str(attempt)).exists())

    def test_returned_worktree_at_wrong_commit_is_refused_unrecorded(self):
        attempt = self.attempt("code", "wrong-head")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "wrong-head-worktree"
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(workspace), intent["base_commit"])
        (workspace / "later.txt").write_text("later\n")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-qm", "later")
        error = S._complete_code_launch(state, unit, str(attempt), workspace)
        self.assertIn("not trusted base", error)
        self.assertNotIn("attempt_launch_facts", state["units"]["code"])
        self.assertNotIn("attempt_workspaces", state["units"]["code"])
        self.assertFalse(W.launch_record_path(str(attempt)).exists())

    def test_fsync_before_state_save_recovers_the_exact_seal(self):
        attempt = self.attempt("code", "seal-crash")
        unit, first = self.intent_state(attempt)
        intent = first["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "seal-crash-worktree"
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(workspace), intent["base_commit"])
        self.assertIsNone(S._complete_code_launch(
            first, unit, str(attempt), workspace, "wks_seal"))
        first_seal = first["units"]["code"]["attempt_record_seals"][attempt.name]

        # Simulate losing every post-fsync state write while retaining the
        # pre-launch intent and the audit bytes.
        second = {"units": {"code": {
            "attempt_bases": {attempt.name: intent["base_commit"]},
            "attempt_launch_intents": {attempt.name: dict(intent)},
        }}}
        self.assertIsNone(S._complete_code_launch(
            second, unit, str(attempt), workspace, "wks_seal"))
        recovered = second["units"]["code"]
        self.assertEqual(recovered["attempt_record_seals"][attempt.name],
                         first_seal)
        self.assertIsNotNone(S.trusted_launch_facts(
            second, "code", str(attempt)))

    def test_redispatch_recovers_existing_agent_instead_of_recreating(self):
        attempt = self.attempt("code", "recover")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "recover-worktree"
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(workspace), intent["base_commit"])
        real = self.real_run
        calls = []

        def recovery_spy(argv, **kwargs):
            calls.append(list(argv))
            if argv[:3] == ["paseo", "ls", "--json"]:
                return 0, json.dumps([{
                    "name": "[swarm] code recover", "id": "agent-recover"}]), ""
            if argv[:2] == ["paseo", "inspect"]:
                return 0, json.dumps({"Id": "agent-recover",
                                      "Cwd": str(workspace)}), ""
            if argv[:3] == ["paseo", "workspace", "ls"]:
                return 0, json.dumps([{"workspaceId": "wks_recover",
                                       "cwd": str(workspace)}]), ""
            if argv[:2] == ["paseo", "run"]:
                self.fail("re-dispatch tried to recreate an existing worktree")
            return real(argv, **kwargs)

        S.U.run = recovery_spy
        job, error = S._submit(unit, str(attempt), False, state)
        self.assertIsNone(error)
        self.assertEqual(job, "agent-recover")
        self.assertFalse(any(call[:2] == ["paseo", "run"] for call in calls))
        self.assertIsNotNone(S.trusted_launch_facts(
            state, "code", str(attempt)))

    def test_precreated_branch_without_agent_is_actionably_refused(self):
        attempt = self.attempt("code", "precreated")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        git(self.repo, "branch", intent["branch"], intent["base_commit"])
        real = self.real_run
        launched = []

        def no_agent(argv, **kwargs):
            if argv[:3] == ["paseo", "ls", "--json"]:
                return 0, "[]", ""
            if argv[:2] == ["paseo", "run"]:
                launched.append(list(argv))
                return 1, "", "must not launch"
            return real(argv, **kwargs)

        S.U.run = no_agent
        job, error = S._submit(unit, str(attempt), False, state)
        self.assertIsNone(job)
        self.assertEqual(launched, [])
        self.assertIn("already exists but no agent", error)
        self.assertIn("git worktree list", error)

    def test_archive_retries_are_bounded_and_name_the_retained_path(self):
        attempt = self.attempt("code", "archive-fails")
        unit = code_unit(self.repo)
        path = str(self.tmp / "managed" / "archive-fails")
        state = {"units": {"code": {"attempt_workspaces": {
            attempt.name: {"path": path, "workspace_id": "wks_fails",
                           "archived": False}}}}}
        calls = []

        def failing_archive(argv, **_kwargs):
            if argv[:3] == ["paseo", "workspace", "archive"]:
                calls.append(list(argv))
                return 1, "", "archive unavailable"
            return self.real_run(argv, **_kwargs)

        S.U.run = failing_archive
        report = []
        for _ in range(S.WORKTREE_ARCHIVE_MAX_ATTEMPTS + 1):
            S._archive_code_worktree(state, unit, str(attempt), report)
        meta = state["units"]["code"]["attempt_workspaces"][attempt.name]
        self.assertEqual(len(calls), S.WORKTREE_ARCHIVE_MAX_ATTEMPTS)
        self.assertTrue(meta["cleanup_gave_up"])
        self.assertNotIn("cleanup_pending", meta)
        message = "\n".join(report)
        self.assertIn("NEEDS_HUMAN", message)
        self.assertIn(path, message)


if __name__ == "__main__":
    unittest.main()
