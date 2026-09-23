"""ARC-682: restored audit snapshots must precede worktree cleanup."""
import ast
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests import test_attempt_worktrees as fixtures

S = fixtures.S
W = fixtures.W
R = S.R
SCRIPTS = fixtures.SCRIPTS
code_unit = fixtures.code_unit
git = fixtures.git


class TestARC682Recovery(unittest.TestCase):
    setUp = fixtures.TestPerAttemptWorktrees.setUp
    attempt = fixtures.TestPerAttemptWorktrees.attempt
    submit = fixtures.TestPerAttemptWorktrees.submit
    intent_state = fixtures.TestPerAttemptWorktrees.intent_state

    def test_000_review_failed_tracked_and_untracked_bytes_survive_cleanup(self):
        """The real Git teardown cannot run until restored bytes match."""
        attempt = self.attempt("code", "review-failed")
        unit = code_unit(self.repo)
        state = {"units": {}}
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        facts = state["units"]["code"]["attempt_launch_facts"][attempt.name]
        workspace = Path(facts["execution_workspace"])
        (workspace / "tracked.txt").write_bytes(b"uncommitted\x00change\n")
        (workspace / "untracked.bin").write_bytes(bytes(range(256)))
        before, problem = R.tree_digest(
            workspace, facts["workspace_identity"]["git_dir"],
            facts["workspace_identity"]["git_pointer_sha256"])
        self.assertIsNone(problem)
        us = state["units"]["code"]
        us.update({"state": "FAILED", "terminal_reason": "REVIEW_FAIL"})
        state_dir = self.tmp / "state"
        S.save_state(str(state_dir), state)

        report = []
        S._archive_code_worktree(
            state, unit, str(attempt), report, str(state_dir))

        self.assertFalse(workspace.exists(), report)
        durable = S.load_state(str(state_dir))
        self.assertEqual(durable["units"]["code"]["state"], "FAILED")
        self.assertNotIn("attempt_produced_heads", durable["units"]["code"])
        record = durable["units"]["code"]["attempt_recovery_snapshots"][
            attempt.name]
        self.assertEqual(record["purpose"], R.PURPOSE)
        self.assertEqual(record["base"]["commit"], facts["base_commit"])
        self.assertEqual(record["base"]["tree"], facts["base_tree"])
        recovered = self.tmp / "recovered"
        self.assertIsNone(R.restore_audit_copy(record, recovered))
        after, problem = R.tree_digest(recovered)
        self.assertIsNone(problem)
        self.assertEqual(after, before)


    def test_launch_records_the_exact_git_pointer_digest(self):
        attempt = self.attempt("code", "pointer-digest")
        unit = code_unit(self.repo)
        state = {"units": {}}
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        facts = state["units"]["code"]["attempt_launch_facts"][attempt.name]
        workspace = Path(facts["execution_workspace"])
        observed, problem = R.git_pointer_digest(workspace / ".git")
        self.assertIsNone(problem)
        self.assertEqual(
            facts["workspace_identity"]["git_pointer_sha256"], observed)
        audit = json.loads(W.launch_record_path(str(attempt)).read_text())
        self.assertEqual(
            audit["workspace_identity"]["git_pointer_sha256"], observed)


    def test_preservation_failure_leaves_worktree_and_skips_teardown(self):
        attempt = self.attempt("code", "preserve-fails")
        unit = code_unit(self.repo)
        state = {"units": {}}
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        workspace = Path(state["units"]["code"]["attempt_launch_facts"]
                         [attempt.name]["execution_workspace"])
        (workspace / "untracked.txt").write_text("must survive\n")
        destructive_calls = []
        real = S.U.run

        def spy(argv, **kwargs):
            if (argv[:2] == ["git", "-C"]
                    and "worktree" in argv and "remove" in argv):
                destructive_calls.append(list(argv))
            return real(argv, **kwargs)

        S.U.run = spy
        state_dir = self.tmp / "state"
        with mock.patch.object(
                S.R, "preserve_worktree",
                return_value=(None, "simulated preservation failure")):
            S._archive_code_worktree(
                state, unit, str(attempt), [], str(state_dir))

        self.assertEqual(destructive_calls, [])
        self.assertTrue(workspace.is_dir())
        self.assertEqual((workspace / "untracked.txt").read_text(),
                         "must survive\n")
        meta = state["units"]["code"]["attempt_workspaces"][attempt.name]
        self.assertFalse(meta["archived"])
        self.assertTrue(meta["cleanup_pending"])
        self.assertNotIn(attempt.name, state["units"]["code"].get(
            "attempt_recovery_snapshots", {}))


    def test_write_after_snapshot_publication_refuses_cleanup(self):
        attempt = self.attempt("code", "late-write")
        unit = code_unit(self.repo)
        state = {"units": {}}
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        workspace = Path(state["units"]["code"]["attempt_launch_facts"]
                         [attempt.name]["execution_workspace"])
        state_dir = self.tmp / "state"
        real_save = S.save_state
        mutated = []

        def mutate_after_snapshot(directory, current):
            real_save(directory, current)
            snapshots = current["units"]["code"].get(
                "attempt_recovery_snapshots", {})
            if attempt.name in snapshots and not mutated:
                (workspace / "late.bin").write_bytes(b"arrived after snapshot\x00")
                mutated.append(True)

        with mock.patch.object(S, "save_state", side_effect=mutate_after_snapshot):
            report = []
            S._archive_code_worktree(
                state, unit, str(attempt), report, str(state_dir))

        self.assertEqual(mutated, [True])
        self.assertTrue(workspace.is_dir(), report)
        self.assertEqual((workspace / "late.bin").read_bytes(),
                         b"arrived after snapshot\x00")
        meta = state["units"]["code"]["attempt_workspaces"][attempt.name]
        self.assertFalse(meta["archived"])
        self.assertIn("changed after its recovery snapshot", "\n".join(report))


    def test_recovery_restores_symlink_object_without_copying_its_target(self):
        source = self.tmp / "symlink-source"
        source.mkdir()
        external = self.tmp / "external.bin"
        external.write_bytes(b"outside the disposable worktree\x00")
        link = source / "external-link"
        link.symlink_to(external)
        recovery_root = self.tmp / "recovery"

        record, error = R.preserve_worktree(
            source, recovery_root, "code", "symlink-attempt",
            "a" * 40, "b" * 40)
        self.assertIsNone(error)
        shutil.rmtree(source)
        external.write_bytes(b"target changed independently\n")
        restored = self.tmp / "restored-symlink"
        self.assertIsNone(R.restore_audit_copy(record, restored))

        restored_link = restored / "external-link"
        self.assertTrue(restored_link.is_symlink())
        self.assertEqual(os.readlink(restored_link), str(external))
        self.assertEqual(external.read_bytes(), b"target changed independently\n")


    def test_replaced_git_pointer_is_preserved_as_worktree_content(self):
        attempt = self.attempt("code", "replaced-git")
        unit = code_unit(self.repo)
        state = {"units": {}}
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        workspace = Path(state["units"]["code"]["attempt_launch_facts"]
                         [attempt.name]["execution_workspace"])
        replacement = b"untracked bytes that replaced git metadata\x00\n"
        (workspace / ".git").write_bytes(replacement)
        state_dir = self.tmp / "state"

        S._archive_code_worktree(
            state, unit, str(attempt), [], str(state_dir))

        record = state["units"]["code"]["attempt_recovery_snapshots"][
            attempt.name]
        restored = self.tmp / "restored-replaced-git"
        self.assertIsNone(R.restore_audit_copy(record, restored))
        self.assertEqual((restored / ".git").read_bytes(), replacement)


    def test_dot_equivalent_replaced_git_pointer_is_preserved(self):
        attempt = self.attempt("code", "dot-equivalent-git")
        unit = code_unit(self.repo)
        state = {"units": {}}
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        facts = state["units"]["code"]["attempt_launch_facts"][attempt.name]
        workspace = Path(facts["execution_workspace"])
        git_dir = Path(facts["workspace_identity"]["git_dir"])
        target = str(git_dir.parent) + "/./" + git_dir.name
        replacement = b"gitdir: " + os.fsencode(target) + b"\n"
        self.assertIn(b"/./", replacement)
        self.assertNotEqual((workspace / ".git").read_bytes(), replacement)
        (workspace / ".git").write_bytes(replacement)
        state_dir = self.tmp / "state"

        S._archive_code_worktree(
            state, unit, str(attempt), [], str(state_dir))

        record = state["units"]["code"]["attempt_recovery_snapshots"][
            attempt.name]
        restored = self.tmp / "restored-dot-equivalent-git"
        self.assertIsNone(R.restore_audit_copy(record, restored))
        self.assertEqual((restored / ".git").read_bytes(), replacement)


    def test_missing_pointer_digest_fails_closed_and_preserves_git_file(self):
        attempt = self.attempt("code", "missing-pointer-digest")
        unit = code_unit(self.repo)
        state = {"units": {}}
        _job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        us = state["units"]["code"]
        facts = us["attempt_launch_facts"][attempt.name]
        workspace = Path(facts["execution_workspace"])
        pointer_bytes = (workspace / ".git").read_bytes()
        facts["workspace_identity"].pop("git_pointer_sha256")
        us["attempt_workspaces"][attempt.name]["workspace_identity"].pop(
            "git_pointer_sha256", None)
        state_dir = self.tmp / "state"

        S._archive_code_worktree(
            state, unit, str(attempt), [], str(state_dir))

        record = us["attempt_recovery_snapshots"][attempt.name]
        restored = self.tmp / "restored-missing-pointer-digest"
        self.assertIsNone(R.restore_audit_copy(record, restored))
        self.assertEqual((restored / ".git").read_bytes(), pointer_bytes)


    def test_missing_legacy_checkout_records_unrecoverable_migration(self):
        attempt = self.attempt("code", "legacy-already-gone")
        unit, state = self.intent_state(attempt)
        missing = self.tmp / "old-paseo-worktree"
        state["units"]["code"]["attempt_workspaces"] = {attempt.name: {
            "path": str(missing), "workspace_id": "wks_old",
            "workspace_owner": "paseo", "archived": False}}
        state_dir = self.tmp / "state"
        report = []

        S._archive_code_worktree(
            state, unit, str(attempt), report, str(state_dir))

        meta = state["units"]["code"]["attempt_workspaces"][attempt.name]
        self.assertTrue(meta["archived"])
        self.assertEqual(meta["recovery_snapshot"],
                         "unavailable-before-preservation-enforcement")
        self.assertIn("already absent before preservation enforcement",
                      "\n".join(report))


    def test_crash_after_git_worktree_add_is_adopted_on_retry(self):
        class Crash(BaseException):
            pass

        attempt = self.attempt("code", "create-crash")
        unit = code_unit(self.repo)
        state = {"units": {}}
        state_dir = self.tmp / "create-crash-state"
        with mock.patch.object(
                S, "_register_code_workspace", side_effect=Crash()):
            with self.assertRaises(Crash):
                S._submit(unit, str(attempt), False, state, str(state_dir))

        workspace = state_dir / "code-worktrees" / attempt.name
        self.assertTrue(workspace.is_dir())
        self.assertNotIn("attempt_workspaces", state["units"]["code"])
        fake = self.fake

        def no_foreign_owner(argv, **kwargs):
            if argv[:3] in (["paseo", "workspace", "ls"],
                            ["paseo", "ls", "--json"]):
                return 0, "[]", ""
            return fake(argv, **kwargs)

        S.U.run = no_foreign_owner
        job, error = S._submit(
            unit, str(attempt), False, state, str(state_dir))

        self.assertIsNone(error)
        self.assertEqual(job, "agent-create-crash")
        self.assertEqual(
            state["units"]["code"]["attempt_workspaces"][attempt.name]
            ["path"], str(workspace.resolve()))
        self.assertEqual(
            state["units"]["code"]["attempt_workspaces"][attempt.name]
            ["workspace_owner"], "coordinator")


    def test_wrong_returned_cwd_cannot_redirect_preservation_or_cleanup(self):
        attempt = self.attempt("code", "wrong-cwd")
        state = {"units": {}}
        state_dir = self.tmp / "wrong-cwd-state"
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        (foreign / "do-not-touch.txt").write_text("foreign\n")
        fake = self.fake

        def wrong_cwd(argv, **kwargs):
            rc, out, err = fake(argv, **kwargs)
            if argv[:2] == ["paseo", "run"]:
                out = json.dumps({"agentId": "agent-wrong-cwd",
                                  "cwd": str(foreign)})
            return rc, out, err

        S.U.run = wrong_cwd
        job, error = S._submit(
            code_unit(self.repo), str(attempt), False, state, str(state_dir))

        self.assertIsNone(job)
        self.assertIn("not authenticated worktree", error)
        meta = S.load_state(str(state_dir))["units"]["code"][
            "attempt_workspaces"][attempt.name]
        expected = state_dir / "code-worktrees" / attempt.name
        self.assertEqual(meta["path"], str(expected.resolve()))
        self.assertTrue(expected.is_dir())
        self.assertEqual((foreign / "do-not-touch.txt").read_text(),
                         "foreign\n")


class TestRecoveryAuthority(unittest.TestCase):
    def test_recovery_material_has_no_judgment_or_resume_consumer(self):
        uses = []
        for path in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(path.read_text())
            stack = []

            class Visitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    stack.append(node.name)
                    self.generic_visit(node)
                    stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Constant(self, node):
                    if node.value == "attempt_recovery_snapshots":
                        uses.append((path.name,
                                     stack[-1] if stack else "<module>"))

            Visitor().visit(tree)
        self.assertEqual(
            uses, [("swarm.py", "_archive_code_worktree")],
            "Recovery material may gate cleanup only. Judgment, completion, "
            "retry, and resume paths must have no reader for it.")


if __name__ == "__main__":
    unittest.main()
