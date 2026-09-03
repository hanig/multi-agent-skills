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
            "target_branch": "main", "prompt": "work",
            "mode": "full-access"}


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

    def submit(self, unit, attempt, dry_run, state):
        return S._submit(unit, str(attempt), dry_run, state,
                         str(self.tmp / "state"))

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
        job, err = self.submit(code_unit(self.repo), attempt, False, state)
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

    def test_dispatched_prompt_names_source_target_base_and_survives_newlines(self):
        attempt = self.attempt("code", "prompt-facts")
        original = "fix the parser\nthen run the focused tests"
        unit = code_unit(self.repo)
        unit["prompt"] = original
        unit["target_branch"] = "release/next"
        state = {"units": {}}

        job, err = self.submit(unit, attempt, False, state)

        self.assertIsNone(err)
        self.assertEqual(job, "agent-prompt-facts")
        argv = self.fake.launches[0]
        prompt = argv[-1]
        intent = state["units"]["code"]["attempt_launch_intents"][
            "prompt-facts"]
        self.assertTrue(prompt.startswith(original + "\n\n"))
        self.assertIn(S.CODE_COMPLETION_PROTOCOL_MARKER, prompt)
        self.assertIn(repr(intent["repo"]), prompt)
        self.assertIn(repr(intent["branch"]), prompt)
        self.assertIn(repr(intent["target_branch"]), prompt)
        self.assertNotEqual(intent["branch"], intent["target_branch"])
        self.assertIn("into target branch", prompt)
        self.assertIn(intent["base_commit"], prompt)
        self.assertIn("Commit all intended work", prompt)
        self.assertIn("Open a pull request", prompt)
        self.assertIn("STOP AND REPORT", prompt)
        # FakePaseo captures the argv list handed to U.run. Both original
        # lines and the protocol arriving in this one final element pins the
        # no-shell, no-requoting delivery property rather than merely testing
        # the string before dispatch.
        self.assertEqual(argv.count(prompt), 1)

    def test_same_and_different_units_receive_distinct_worktrees(self):
        state = {"units": {}}
        cases = (("u1", "a1"), ("u1", "a2"), ("u2", "a3"))
        paths = []
        for uid, attempt_id in cases:
            job, err = self.submit(code_unit(self.repo, uid),
                                   self.attempt(uid, attempt_id), False, state)
            self.assertIsNone(err)
            self.assertTrue(job)
            paths.append(state["units"][uid]["attempt_launch_facts"]
                         [attempt_id]["execution_workspace"])
        self.assertEqual(len(set(paths)), 3)

    def test_dirty_shared_checkout_does_not_block_a_code_attempt(self):
        (self.repo / "human-edit.txt").write_text("uncommitted\n")
        state = {"units": {}}
        job, err = self.submit(code_unit(self.repo),
                               self.attempt("code", "dirty-shared"),
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
        _job, err = self.submit(code_unit(self.repo), attempt, False, state)
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
        job, err = self.submit(code_unit(self.repo),
                               self.attempt("code", "dry"), True, state)
        self.assertIsNone(err)
        self.assertTrue(job.startswith("dry-"))
        self.assertEqual(self.fake.launches, [])
        self.assertFalse((self.tmp / "managed").exists())
        self.assertNotIn("attempt_launch_facts", state["units"]["code"])

    def test_non_dry_dispatch_without_state_dir_is_refused(self):
        state = {"units": {}}
        job, error = S._submit(
            code_unit(self.repo), str(self.attempt("code", "no-state-dir")),
            False, state)
        self.assertIsNone(job)
        self.assertIn("without coordinator state and state_dir", error)
        self.assertEqual(self.fake.launches, [])

    def test_dry_run_archives_nothing_and_preserves_durable_state(self):
        attempt = self.attempt("code", "real-failed")
        unit = code_unit(self.repo)
        plan = {"name": "p", "units": [unit]}
        state_dir = self.tmp / "dry-state"
        state = {"schema_version": 1, "halted": None,
                 "plan_digest": S.plan_digest(plan), "units": {"code": {
                     "state": "FAILED", "attempt_dir": str(attempt),
                     "attempts": [str(attempt)], "gpu_hours": 0,
                     "job_id": "agent-real",
                     "launch_recovery_problem": "fixture stops judgment",
                     "attempt_workspaces": {attempt.name: {
                         "path": str(self.tmp / "managed" / attempt.name),
                         "workspace_id": "wks_real", "archived": False,
                         "cleanup_pending": True},
                         "older": {
                             "path": str(self.tmp / "managed" / "older"),
                             "workspace_id": "wks_older", "archived": False,
                             "cleanup_gave_up": True,
                             "cleanup_attempts":
                                 S.WORKTREE_ARCHIVE_MAX_ATTEMPTS}}}}}
        S.save_state(str(state_dir), state)
        before = (state_dir / S.STATE_FILE).read_bytes()
        archives = []
        real = self.real_run

        def spy(argv, **kwargs):
            if argv[:3] == ["paseo", "workspace", "archive"]:
                archives.append(list(argv))
                return 0, "{}", ""
            return real(argv, **kwargs)

        S.U.run = spy
        report, _dispatched, _halted = S.advance(
            plan, state, str(state_dir), str(self.tmp / "runs"),
            True, max_new=0)
        self.assertEqual(archives, [])
        self.assertEqual((state_dir / S.STATE_FILE).read_bytes(), before)
        self.assertFalse((state_dir / S.OUTBOX).exists())
        joined = "\n".join(report)
        self.assertIn("DRY RUN -- would archive", joined)
        self.assertIn(S.WORKTREE_CLEANUP_SUMMARY_PREFIX, joined)
        self.assertIn("would re-emit tracker", joined)

    def test_bind_never_precedes_its_durable_job_record(self):
        attempt = self.attempt("u", "bind-order")
        state_dir = self.tmp / "bind-state"
        state = {"schema_version": 1, "halted": None, "units": {}}
        plan = {"name": "p", "units": [{
            "id": "u", "kind": "slurm", "command": "true",
            "outputs": [], "write_scopes": ["u/"]}]}
        real_allocate, real_submit, real_bind = (
            S._allocate, S._submit, S._bind)

        def allocate(*_args, **_kwargs):
            return str(attempt), None

        def submit(*_args, **_kwargs):
            return "job-1", None

        def bind(unit_dir, job_id):
            durable = S.load_state(str(state_dir))["units"]["u"]
            self.assertEqual(durable["job_id"], job_id)
            self.assertTrue(durable["bind_pending"])
            self.assertEqual(durable["state"], "SUBMITTED")
            return None

        S._allocate, S._submit, S._bind = allocate, submit, bind
        self.addCleanup(setattr, S, "_allocate", real_allocate)
        self.addCleanup(setattr, S, "_submit", real_submit)
        self.addCleanup(setattr, S, "_bind", real_bind)
        ok, why = S.acquire_lease(str(state_dir))
        self.assertTrue(ok, why)
        self.addCleanup(S.release_lease, str(state_dir))
        report, dispatched, _halted = S.advance(
            plan, state, str(state_dir), str(self.tmp / "runs"),
            False, max_new=1)
        self.assertEqual(dispatched, 1, report)

    def test_terminal_cleanup_archives_workspace_but_keeps_branch(self):
        attempt = self.attempt("code", "cleanup")
        state = {"units": {}}
        _job, err = self.submit(code_unit(self.repo), attempt, False, state)
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

    def test_crash_after_archive_keeps_durable_produced_head(self):
        class Crash(BaseException):
            pass

        attempt = self.attempt("code", "archive-crash")
        unit = code_unit(self.repo)
        state = {"schema_version": 1, "halted": None, "units": {}}
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        facts = state["units"]["code"]["attempt_launch_facts"][attempt.name]
        workspace = Path(facts["execution_workspace"])
        (workspace / "made.txt").write_text("made\n")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-qm", "attempt work")
        produced = git(workspace, "rev-parse", "HEAD")
        us = state["units"]["code"]
        us.update({"state": "SUBMITTED", "attempt_dir": str(attempt),
                   "attempts": [str(attempt)], "gpu_hours": 0,
                   "job_id": "agent-archive-crash"})
        state_dir = self.tmp / "state"
        S.save_state(str(state_dir), state)
        plan = {"name": "p", "units": [dict(
            unit, outputs=[], write_scopes=["code/"])]}

        result_line = S.CHECK_RESULT_PREFIX + " " + json.dumps({
            "produced_head": produced, "receipt_sha256": "f" * 64,
        }, sort_keys=True, separators=(",", ":"))
        real_check = S._check
        real_archive = S._archive_code_worktree

        def check(*_args, **_kwargs):
            return S.DONE, "DONE", "", result_line

        def archive_then_crash(current, _unit, _attempt, _report,
                               current_state_dir=None):
            durable = S.load_state(current_state_dir)
            recorded = durable["units"]["code"]["attempt_produced_heads"]
            self.assertEqual(recorded[attempt.name], produced)
            current["units"]["code"]["attempt_workspaces"][
                attempt.name]["archived"] = True
            git(self.repo, "worktree", "remove", "--force", str(workspace))
            raise Crash()

        S._check = check
        S._archive_code_worktree = archive_then_crash
        self.addCleanup(setattr, S, "_check", real_check)
        self.addCleanup(setattr, S, "_archive_code_worktree", real_archive)
        ok, why = S.acquire_lease(str(state_dir))
        self.assertTrue(ok, why)
        self.addCleanup(S.release_lease, str(state_dir))
        with self.assertRaises(Crash):
            S.advance(plan, state, str(state_dir), str(self.tmp / "runs"),
                      False, max_new=0)
        recovered = S.load_state(str(state_dir))
        self.assertEqual(recovered["units"]["code"][
            "attempt_produced_heads"][attempt.name], produced)
        recovered_facts = S.trusted_launch_facts(
            recovered, "code", str(attempt))
        self.assertIsNone(W.validate_pinned_head(
            self.real_run, recovered_facts, produced))

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

    def test_symlinked_plan_repo_cannot_make_source_checkout_a_worktree(self):
        attempt = self.attempt("code", "symlink-source")
        link = self.tmp / "repo-link"
        link.symlink_to(self.repo, target_is_directory=True)
        unit = code_unit(link)
        real = S._plan_workspace
        S._plan_workspace = lambda _unit: (str(link), None)
        self.addCleanup(setattr, S, "_plan_workspace", real)
        error, anchored = S._capture_code_launch(str(attempt), unit)
        self.assertIsNone(error)
        intent = anchored["intent"]
        self.assertEqual(intent["repo"], str(self.repo.resolve()))
        state = {"units": {"code": {
            "attempt_launch_intents": {attempt.name: intent}}}}
        error = S._complete_code_launch(
            state, unit, str(attempt), str(link))
        self.assertIn("shared source checkout", error)
        self.assertNotIn("attempt_launch_facts", state["units"]["code"])

    def test_renamed_and_replaced_worktree_fails_judgment_on_inode(self):
        attempt = self.attempt("code", "replaced")
        state = {"units": {}}
        unit = code_unit(self.repo)
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        facts = state["units"]["code"]["attempt_launch_facts"][attempt.name]
        workspace = Path(facts["execution_workspace"])
        moved = workspace.with_name("moved-original")
        workspace.rename(moved)
        git(self.repo, "worktree", "prune", "--expire", "now")
        git(self.repo, "worktree", "add", "-q", str(workspace),
            facts["branch"])
        produced, why = W.judge(S.U.run, str(attempt), unit, facts)
        self.assertFalse(produced)
        self.assertIn("device/inode changed", why)

    def test_swapped_git_metadata_is_refused_at_judgment(self):
        attempt = self.attempt("code", "swapped-git")
        state = {"units": {}}
        unit = code_unit(self.repo)
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        facts = state["units"]["code"]["attempt_launch_facts"][attempt.name]
        workspace = Path(facts["execution_workspace"])
        (workspace / "made.txt").write_text("made\n")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-qm", "attempt work")
        produced_head = git(workspace, "rev-parse", "HEAD")

        donor = self.tmp / "donor-worktree"
        git(self.repo, "worktree", "add", "-q", "--detach", str(donor),
            facts["base_commit"])
        donor_git = Path(git(donor, "rev-parse", "--git-dir"))
        if not donor_git.is_absolute():
            donor_git = (donor / donor_git).resolve()
        (donor_git / "HEAD").write_text(f"ref: refs/heads/{facts['branch']}\n")
        git(donor, "reset", "--hard", produced_head)

        launched_git = Path(facts["workspace_identity"]["git_dir"])
        before_root = os.stat(workspace)
        before_git = os.stat(launched_git)
        saved_git = launched_git.with_name(launched_git.name + "-original")
        launched_git.rename(saved_git)
        shutil.copytree(donor_git, launched_git)
        after_root = os.stat(workspace)
        after_git = os.stat(launched_git)
        self.assertEqual((before_root.st_dev, before_root.st_ino),
                         (after_root.st_dev, after_root.st_ino))
        self.assertNotEqual((before_git.st_dev, before_git.st_ino),
                            (after_git.st_dev, after_git.st_ino))
        produced, why = W.judge(S.U.run, str(attempt), unit, facts)
        self.assertFalse(produced)
        self.assertIn("metadata identity", why)

    def test_pre_migration_workspace_identity_still_judges(self):
        attempt = self.attempt("code", "old-identity")
        state = {"units": {}}
        unit = code_unit(self.repo)
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        facts = state["units"]["code"]["attempt_launch_facts"][attempt.name]
        workspace = Path(facts["execution_workspace"])
        (workspace / "made.txt").write_text("made\n")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-qm", "attempt work")
        for key in ("git_common_device", "git_common_inode",
                    "git_dir_device", "git_dir_inode"):
            facts["workspace_identity"].pop(key)

        produced, why = W.judge(S.U.run, str(attempt), unit, facts)
        self.assertTrue(produced, why)

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
        workspace = self.tmp / "recovery-managed" / intent["worktree_slug"]
        workspace.parent.mkdir()
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
                                       "cwd": str(workspace),
                                       "name": intent["branch"],
                                       "isolation": "worktree"}]), ""
            if argv[:2] == ["paseo", "run"]:
                self.fail("re-dispatch tried to recreate an existing worktree")
            return real(argv, **kwargs)

        S.U.run = recovery_spy
        job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        self.assertEqual(job, "agent-recover")
        self.assertFalse(any(call[:2] == ["paseo", "run"] for call in calls))
        self.assertIsNotNone(S.trusted_launch_facts(
            state, "code", str(attempt)))

    def test_recovery_succeeds_when_agent_committed_during_downtime(self):
        attempt = self.attempt("code", "recover-ahead")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "ahead-managed" / intent["worktree_slug"]
        workspace.parent.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(workspace), intent["base_commit"])
        (workspace / "during-downtime.txt").write_text("done\n")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-qm", "work during downtime")
        real = self.real_run

        def recovery(argv, **kwargs):
            if argv[:2] == ["paseo", "inspect"]:
                return 0, json.dumps({"Cwd": str(workspace)}), ""
            if argv[:3] == ["paseo", "workspace", "ls"]:
                return 0, json.dumps([{
                    "workspaceId": "wks_ahead", "cwd": str(workspace),
                    "name": intent["branch"],
                    "isolation": "worktree"}]), ""
            return real(argv, **kwargs)

        S.U.run = recovery
        error = S._recover_code_launch(
            state, unit, str(attempt), "agent-ahead")
        self.assertIsNone(error)
        facts = state["units"]["code"]["attempt_launch_facts"][attempt.name]
        self.assertEqual(facts["base_commit"], intent["base_commit"])

    def test_recovery_refuses_when_agent_replaced_history(self):
        attempt = self.attempt("code", "recover-replaced")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "replaced-managed" / intent["worktree_slug"]
        workspace.parent.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(workspace), intent["base_commit"])
        tree = git(workspace, "write-tree")
        replaced = subprocess.run(
            ["git", "-C", str(workspace), "commit-tree", tree, "-m", "root"],
            check=True, env=ENV, capture_output=True, text=True).stdout.strip()
        git(workspace, "reset", "--hard", replaced)
        real = self.real_run

        def recovery(argv, **kwargs):
            if argv[:2] == ["paseo", "inspect"]:
                return 0, json.dumps({"Cwd": str(workspace)}), ""
            if argv[:3] == ["paseo", "workspace", "ls"]:
                return 0, json.dumps([{
                    "workspaceId": "wks_replaced", "cwd": str(workspace),
                    "name": intent["branch"],
                    "isolation": "worktree"}]), ""
            return real(argv, **kwargs)

        S.U.run = recovery
        error = S._recover_code_launch(
            state, unit, str(attempt), "agent-replaced")
        self.assertIn("does not descend from trusted base", error)
        self.assertNotIn("attempt_launch_facts", state["units"]["code"])

    def test_ambiguous_registry_does_not_refuse_legitimate_recovery(self):
        attempt = self.attempt("code", "ambiguous-registry")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "ambiguous-managed" / intent["worktree_slug"]
        workspace.parent.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(workspace), intent["base_commit"])
        real = self.real_run

        def recovery(argv, **kwargs):
            if argv[:2] == ["paseo", "inspect"]:
                return 0, json.dumps({"Cwd": str(workspace)}), ""
            if argv[:3] == ["paseo", "workspace", "ls"]:
                record = {"cwd": str(workspace), "name": intent["branch"],
                          "isolation": "worktree"}
                return 0, json.dumps([
                    dict(record, workspaceId="wks_one"),
                    dict(record, workspaceId="wks_duplicate")]), ""
            return real(argv, **kwargs)

        S.U.run = recovery
        error = S._recover_code_launch(
            state, unit, str(attempt), "agent-legitimate")
        self.assertIsNone(error)
        self.assertIsNotNone(S.trusted_launch_facts(
            state, "code", str(attempt)))

    def test_matching_title_agent_is_not_bound_to_another_workspace(self):
        attempt = self.attempt("code", "wrong-owner")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        expected = self.tmp / "expected-managed" / intent["worktree_slug"]
        unrelated = self.tmp / "unrelated-worktree"
        expected.parent.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(expected), intent["base_commit"])
        git(self.repo, "worktree", "add", "-q", "--detach", str(unrelated),
            intent["base_commit"])
        real = self.real_run

        def impostor(argv, **kwargs):
            if argv[:3] == ["paseo", "ls", "--json"]:
                return 0, json.dumps([{
                    "name": f"[swarm] code {attempt.name}",
                    "id": "agent-unrelated"}]), ""
            if argv[:2] == ["paseo", "inspect"]:
                return 0, json.dumps({"Cwd": str(unrelated)}), ""
            if argv[:3] == ["paseo", "workspace", "ls"]:
                return 0, json.dumps([{
                    "workspaceId": "wks_expected", "cwd": str(expected),
                    "name": intent["branch"],
                    "isolation": "worktree"}]), ""
            return real(argv, **kwargs)

        S.U.run = impostor
        job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(job)
        self.assertIn("device/inode does not match", error)
        self.assertNotIn("attempt_launch_facts", state["units"]["code"])

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
        job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(job)
        self.assertEqual(launched, [])
        self.assertIn("no registered agent", error)
        self.assertIn("git worktree list", error)

    def test_exact_base_worktree_without_agent_is_reused(self):
        attempt = self.attempt("code", "reuse-unowned")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "unowned" / attempt.name
        workspace.parent.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(workspace), intent["base_commit"])
        real = self.real_run
        launches = []

        def reuse(argv, **kwargs):
            if argv[:3] == ["paseo", "ls", "--json"]:
                return 0, "[]", ""
            if argv[:3] == ["paseo", "workspace", "ls"]:
                return 0, "[]", ""
            if argv[:2] == ["paseo", "run"]:
                launches.append(list(argv))
                return 0, json.dumps({
                    "agentId": "agent-reused", "cwd": str(workspace),
                    "workspaceId": "wks_unowned"}), ""
            return real(argv, **kwargs)

        S.U.run = reuse
        state["schema_version"] = 1
        state["halted"] = None
        state["plan_digest"] = S.plan_digest(
            {"name": "p", "units": [unit]})
        state["units"]["code"].update({
            "state": "ALLOCATED", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "gpu_hours": 0,
            "allocated_at": 1,
        })
        state_dir = self.tmp / "reuse-state"
        S.save_state(str(state_dir), state)
        real_bind = S._bind
        S._bind = lambda unit_dir, job_id: None
        self.addCleanup(setattr, S, "_bind", real_bind)

        report, _dispatched, _halted = S.advance(
            {"name": "p", "units": [unit]}, state, str(state_dir),
            str(self.tmp / "runs"), False, max_new=0)
        self.assertEqual(state["units"]["code"]["job_id"], "agent-reused")
        self.assertEqual(state["units"]["code"]["state"], "SUBMITTED")
        self.assertNotIn("NEEDS_HUMAN", "\n".join(report))
        self.assertEqual(len(launches), 1)
        self.assertNotIn("--new-workspace", launches[0])
        self.assertEqual(launches[0][launches[0].index("--cwd") + 1],
                         str(workspace.resolve()))
        self.assertIsNotNone(S.trusted_launch_facts(
            state, "code", str(attempt)))

    def test_reuse_refuses_a_path_paseo_already_owns(self):
        attempt = self.attempt("code", "foreign-owner")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "foreign" / attempt.name
        workspace.parent.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(workspace), intent["base_commit"])
        real = self.real_run
        launches = []

        def owned(argv, **kwargs):
            if argv[:3] == ["paseo", "ls", "--json"]:
                return 0, "[]", ""
            if argv[:3] == ["paseo", "workspace", "ls"]:
                return 0, json.dumps([{
                    "workspaceId": "wks_foreign", "cwd": str(workspace),
                    "name": "someone-else", "isolation": "worktree"}]), ""
            if argv[:2] == ["paseo", "run"]:
                launches.append(list(argv))
                return 0, "{}", ""
            return real(argv, **kwargs)

        S.U.run = owned
        job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(job)
        self.assertIn("already owns that path", error)
        self.assertIn("wks_foreign", error)
        self.assertEqual(launches, [])

    def test_reuse_refuses_an_existing_agent_at_the_path(self):
        attempt = self.attempt("code", "foreign-agent")
        unit, state = self.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        workspace = self.tmp / "foreign-agent" / attempt.name
        workspace.parent.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", intent["branch"],
            str(workspace), intent["base_commit"])
        real = self.real_run
        launches = []

        def owned(argv, **kwargs):
            if argv[:3] == ["paseo", "workspace", "ls"]:
                return 0, "[]", ""
            if argv[:3] == ["paseo", "ls", "--json"]:
                return 0, json.dumps([{
                    "id": "agent-foreign", "name": "unrelated",
                    "cwd": str(workspace)}]), ""
            if argv[:2] == ["paseo", "run"]:
                launches.append(list(argv))
                return 0, "{}", ""
            return real(argv, **kwargs)

        S.U.run = owned
        job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(job)
        self.assertIn("already has agent agent-foreign", error)
        self.assertEqual(launches, [])

    def test_no_cwd_response_still_registers_created_workspace(self):
        attempt = self.attempt("code", "nocwd")
        state = {"units": {}}
        state_dir = self.tmp / "no-cwd-state"
        fake = self.fake

        def omit_cwd(argv, **kwargs):
            rc, out, err = fake(argv, **kwargs)
            if argv[:2] == ["paseo", "run"]:
                out = (f"Created workspace wks_{attempt.name} - fixture\n" +
                       json.dumps({"agentId": f"agent-{attempt.name}"}))
            return rc, out, err

        S.U.run = omit_cwd
        job, error = S._submit(
            code_unit(self.repo), str(attempt), False, state, str(state_dir))
        self.assertIsNone(job)
        self.assertIn("no worktree cwd", error)
        meta = S.load_state(str(state_dir))["units"]["code"][
            "attempt_workspaces"][attempt.name]
        self.assertEqual(meta["workspace_id"], f"wks_{attempt.name}")
        self.assertIsNone(meta["path"])
        self.assertTrue(meta["cleanup_pending"])

    def test_failed_verification_registers_worktree_for_retained_report(self):
        attempt = self.attempt("code", "verify-fails")
        unit = code_unit(self.repo)
        state = {"units": {}}
        state_dir = self.tmp / "state"
        fake = self.fake

        def wrong_branch(argv, **kwargs):
            result = fake(argv, **kwargs)
            if argv[:2] == ["paseo", "run"]:
                workspace = self.tmp / "managed" / attempt.name
                git(workspace, "switch", "-q", "-c", "wrong-branch")
            return result

        S.U.run = wrong_branch
        job, error = S._submit(
            unit, str(attempt), False, state, str(state_dir))
        self.assertIsNone(job)
        self.assertIn("not trusted attempt branch", error)
        durable = S.load_state(str(state_dir))
        meta = durable["units"]["code"]["attempt_workspaces"][attempt.name]
        self.assertEqual(meta["verification"], "refused")
        self.assertTrue(meta["cleanup_pending"])
        self.assertEqual(meta["path"],
                         str((self.tmp / "managed" / attempt.name).resolve()))

        def failing_archive(argv, **kwargs):
            if argv[:3] == ["paseo", "workspace", "archive"]:
                return 1, "", "archive unavailable"
            if argv[:3] == ["paseo", "ls", "--json"]:
                return 0, "[]", ""
            return self.real_run(argv, **kwargs)

        S.U.run = failing_archive
        durable["schema_version"] = 1
        durable["halted"] = None
        durable["units"]["code"].update({
            # Crash after _submit persisted the rejected workspace but before
            # advance classified the submit error as FAILED.
            "state": "ALLOCATED", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "gpu_hours": 0,
            "job_id": None,
            "launch_recovery_problem": "verification was refused",
        })
        plan = {"name": "p", "units": [unit]}
        S.save_state(str(state_dir), durable)
        report = []
        for _ in range(S.WORKTREE_ARCHIVE_MAX_ATTEMPTS):
            durable = S.load_state(str(state_dir))
            report, _dispatched, _halted = S.advance(
                plan, durable, str(state_dir), str(self.tmp / "runs"),
                False, max_new=0)
        retained = [line for line in report if line.startswith(
            S.WORKTREE_CLEANUP_SUMMARY_PREFIX)]
        self.assertEqual(len(retained), 1)
        self.assertIn(meta["path"], retained[0])

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

    def test_crash_mid_cleanup_does_not_reset_retry_counter(self):
        class Crash(BaseException):
            pass

        attempt = self.attempt("code", "cleanup-crash")
        unit = code_unit(self.repo)
        state_dir = self.tmp / "state"
        state = {"schema_version": 1, "halted": None, "units": {
            "code": {"attempt_workspaces": {attempt.name: {
                "path": str(self.tmp / "managed" / attempt.name),
                "workspace_id": "wks_crash", "archived": False}}}}}
        S.save_state(str(state_dir), state)

        def crash_during_archive(argv, **_kwargs):
            if argv[:3] == ["paseo", "workspace", "archive"]:
                raise Crash()
            return self.real_run(argv, **_kwargs)

        S.U.run = crash_during_archive
        for expected in range(1, S.WORKTREE_ARCHIVE_MAX_ATTEMPTS + 1):
            current = S.load_state(str(state_dir))
            with self.assertRaises(Crash):
                S._archive_code_worktree(
                    current, unit, str(attempt), [], str(state_dir))
            durable = S.load_state(str(state_dir))
            meta = durable["units"]["code"]["attempt_workspaces"][attempt.name]
            self.assertEqual(meta["cleanup_attempts"], expected)
        report = []
        durable = S.load_state(str(state_dir))
        S._archive_code_worktree(
            durable, unit, str(attempt), report, str(state_dir))
        final = S.load_state(str(state_dir))["units"]["code"][
            "attempt_workspaces"][attempt.name]
        self.assertTrue(final["cleanup_gave_up"])
        self.assertEqual(final["cleanup_attempts"],
                         S.WORKTREE_ARCHIVE_MAX_ATTEMPTS)

    def test_archive_exhaustion_is_one_aggregate_report(self):
        unit1 = code_unit(self.repo, "u1")
        unit2 = code_unit(self.repo, "u2")
        attempts = [self.attempt("u1", "a1"), self.attempt("u2", "a2")]
        paths = [str(self.tmp / "managed" / p.name) for p in attempts]
        state = {"units": {
            "u1": {"attempt_workspaces": {"a1": {
                "path": paths[0], "workspace_id": "wks_a1",
                "archived": False}}},
            "u2": {"attempt_workspaces": {"a2": {
                "path": paths[1], "workspace_id": "wks_a2",
                "archived": False}}},
        }}

        def failing_archive(argv, **_kwargs):
            if argv[:3] == ["paseo", "workspace", "archive"]:
                return 1, "", "archive unavailable"
            return self.real_run(argv, **_kwargs)

        S.U.run = failing_archive
        report = []
        for unit, attempt in ((unit1, attempts[0]), (unit2, attempts[1])):
            for _ in range(S.WORKTREE_ARCHIVE_MAX_ATTEMPTS):
                S._archive_code_worktree(
                    state, unit, str(attempt), report)
        aggregate = [line for line in report if line.startswith(
            S.WORKTREE_CLEANUP_SUMMARY_PREFIX)]
        self.assertEqual(len(aggregate), 1)
        self.assertIn("2 cleanup(s)", aggregate[0])
        self.assertIn(paths[0], aggregate[0])
        self.assertIn(paths[1], aggregate[0])



class TestEveryArchiveSiteIsDryRunGuarded(unittest.TestCase):
    """Best-effort spelling lint, because one behaviour test covers one site.

    Removing the dry-run guard from the SECOND archive site broke nothing in
    the whole suite: the guard was correct and untested, so a later edit could
    delete it silently. A dry run that archives a real Paseo workspace is not
    a dry run, and the property worth pinning is "every archive site is
    guarded", not "this one is".

    This catches direct calls spelled `_archive_code_worktree` under a lexical
    `dry_run` condition. An alias, wrapper, computed call, or rebinding can
    evade it; proving that arbitrary Python reaches no archive operation is
    not statically decidable. The behavioural dry-run test remains the actual
    lifecycle check. Do not read this lint as proof.
    """

    def test_no_archive_call_escapes_a_dry_run_guard(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        tree = ast.parse(src)

        guarded = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            if not any(isinstance(n, ast.Name) and n.id == "dry_run"
                       for n in ast.walk(node.test)):
                continue
            for stmt in node.body:
                guarded.append((stmt.lineno,
                                getattr(stmt, "end_lineno", stmt.lineno)))

        unguarded = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None)
                    == "_archive_code_worktree"):
                if not any(lo <= node.lineno <= hi for lo, hi in guarded):
                    unguarded.append(node.lineno)

        self.assertEqual(
            unguarded, [],
            "archive call(s) at line(s) %s are not inside a dry_run guard; a "
            "dry run must never archive a real workspace" % unguarded)

if __name__ == "__main__":
    unittest.main()
