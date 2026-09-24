"""Host boundaries for coordinator-owned launch facts."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402
import worktree as W  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, env=ENV,
        capture_output=True, text=True).stdout.strip()


class TestLaunchHostIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def legacy_facts(self, attempt, host="host-a"):
        workspace = self.tmp / "foreign-worktree"
        workspace.mkdir(exist_ok=True)
        st = os.stat(workspace)
        facts = {
            "schema_version": 1,
            "unit_id": "code",
            "attempt_id": attempt.name,
            "launch_host": host,
            "repo": str(workspace),
            "repository_remote": None,
            "execution_workspace": str(workspace),
            "workspace_identity": {
                "path": str(workspace),
                "realpath": str(workspace),
                "device": st.st_dev + 1,
                "inode": st.st_ino,
                "git_common_dir": str(workspace / ".git"),
                "git_dir": str(workspace / ".git"),
            },
            "base_commit": "a" * 40,
            "base_tree": "b" * 40,
            "branch": "swarm-attempt",
            "clean_at_launch": True,
        }
        return facts

    def test_host_ownership_predicate_is_tri_state(self):
        self.assertIsNone(W.attempt_belongs_to_host({}, "host-b"))
        facts = {"launch_host": "host-a"}
        self.assertIs(W.attempt_belongs_to_host(facts, "host-a"), True)
        self.assertIs(W.attempt_belongs_to_host(facts, "host-b"), False)

    def test_foreign_state_names_both_hosts_before_identity_comparison(self):
        attempt = self.tmp / "attempt-a"
        attempt.mkdir()
        facts = self.legacy_facts(attempt)
        state = {"units": {"code": {"attempt_launch_facts": {
            attempt.name: facts,
        }}}}
        trusted = S.trusted_launch_facts(state, "code", str(attempt))
        self.assertIs(trusted, facts)

        def no_git(_argv, **_kwargs):
            self.fail("a foreign-host judgment must not inspect the workspace")

        with mock.patch.object(
                W.os, "uname", return_value=SimpleNamespace(nodename="host-b")):
            judgment = {}
            produced, head, why = W.judge_detail(
                no_git, str(attempt), {"id": "code", "repo": "/declared"},
                trusted, judgment)

        self.assertFalse(produced)
        self.assertIsNone(head)
        self.assertIn("UNJUDGEABLE HERE", why)
        self.assertIn("host-a", why)
        self.assertIn("host-b", why)
        self.assertNotIn("cannot identify", why)
        self.assertEqual(judgment["production_state"], "unjudgeable-here")
        self.assertEqual(W.code_failure_reason(judgment["production_state"]),
                         "unjudgeable-here")

        # The pre-host schema keeps its old answer. No host is inferred from a
        # path or inode, because doing that would manufacture launch authority.
        del facts["launch_host"]
        with mock.patch.object(
                W.os, "uname", return_value=SimpleNamespace(nodename="host-b")):
            produced, head, why = W.judge_detail(
                no_git, str(attempt), {"id": "code", "repo": "/declared"},
                trusted)
        self.assertFalse(produced)
        self.assertIsNone(head)
        self.assertIn("device differs", why)
        self.assertNotIn("UNJUDGEABLE HERE", why)

    def test_code_launch_persists_pre_agent_host_in_trusted_facts(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "tracked.txt").write_text("base\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        remote = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)],
                       check=True, env=ENV)
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "branch", "-M", "main")
        git(repo, "push", "-qu", "origin", "main")
        attempt = self.tmp / "attempt-b"
        attempt.mkdir()
        unit = {"id": "code", "kind": "code", "repo": str(repo),
                "target_branch": "main"}
        with mock.patch.object(
                S.os, "uname", return_value=SimpleNamespace(nodename="host-a")):
            problem, anchored = S._capture_code_launch(str(attempt), unit)
        self.assertIsNone(problem)
        self.assertEqual(anchored["intent"]["launch_host"], "host-a")

        workspace = self.tmp / anchored["intent"]["worktree_slug"]
        git(repo, "worktree", "add", "-q", "-b",
            anchored["intent"]["branch"], str(workspace),
            anchored["intent"]["base_commit"])
        state = {"units": {"code": {"attempt_launch_intents": {
            attempt.name: anchored["intent"],
        }}}}
        with mock.patch.object(
                S.os, "uname", return_value=SimpleNamespace(nodename="host-a")):
            problem = S._complete_code_launch(
                state, unit, str(attempt), workspace)
        self.assertIsNone(problem)
        facts = state["units"]["code"]["attempt_launch_facts"][attempt.name]
        self.assertEqual(facts["launch_host"], "host-a")
        self.assertEqual(facts["schema_version"], 6)
        self.assertIsNone(W.launch_facts_problem(
            facts, str(attempt), {"id": "code"}))
        audit = json.loads(W.launch_record_path(attempt).read_text())
        self.assertEqual(audit["launch_host"], "host-a")

    def test_non_code_launch_facts_record_the_observing_host_too(self):
        repo = self.tmp / "shared-repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "tracked.txt").write_text("base\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        attempt = self.tmp / "attempt-non-code"
        attempt.mkdir()
        unit = {"id": "job", "kind": "slurm", "repo": str(repo)}

        with mock.patch.object(
                S.os, "uname", return_value=SimpleNamespace(nodename="host-a")):
            problem, anchored = S._write_launch_record(str(attempt), unit)

        self.assertIsNone(problem)
        self.assertEqual(anchored["facts"]["launch_host"], "host-a")

    def test_host_predicate_precedes_pinned_commit_validation(self):
        attempt = self.tmp / "attempt-c"
        attempt.mkdir()
        facts = self.legacy_facts(attempt)

        def no_git(_argv, **_kwargs):
            self.fail("foreign-host pin validation must not run Git")

        problem = W.validate_pinned_head(
            no_git, facts, "c" * 40)
        self.assertIn("UNJUDGEABLE HERE", problem)

    def test_advance_marks_done_foreign_attempt_without_rejudging_it(self):
        attempt = self.tmp / "runs" / "code" / "attempt-d"
        attempt.mkdir(parents=True)
        state_dir = self.tmp / "state"
        state_dir.mkdir()
        state = {"schema_version": 1, "halted": None, "units": {
            "code": {
                "state": "DONE",
                "attempt_dir": str(attempt),
                "attempts": [str(attempt)],
                "job_id": "agent-foreign",
                "gpu_hours": 0,
                "incomplete_since": 1,
                "launch_recovery_problem": "an earlier recovery error",
                "attempt_launch_intents": {attempt.name: {
                    "launch_host": "host-a",
                }},
            },
        }}
        plan = {"name": "p", "units": [{
            "id": "code", "kind": "code", "repo": "/declared",
            "outputs": [], "write_scopes": ["code/"],
        }]}
        real_check = S._check

        def no_check(*_args, **_kwargs):
            self.fail("the checker must not run for a foreign-host attempt")

        S._check = no_check
        self.addCleanup(setattr, S, "_check", real_check)
        ok, why = S.acquire_lease(str(state_dir))
        self.assertTrue(ok, why)
        self.addCleanup(S.release_lease, str(state_dir))
        with mock.patch.object(
                S.os, "uname", return_value=SimpleNamespace(nodename="host-b")):
            report, dispatched, halted = S.advance(
                plan, state, str(state_dir), str(self.tmp / "runs"),
                False, max_new=0)

        self.assertEqual(dispatched, 0)
        self.assertIsNone(halted)
        self.assertNotEqual(state["units"]["code"]["state"],
                            "UNJUDGEABLE_HERE")
        self.assertEqual(state["units"]["code"]["host_judgment"],
                         "UNJUDGEABLE_HERE")
        self.assertNotIn("incomplete_since", state["units"]["code"])
        self.assertNotIn("launch_recovery_problem", state["units"]["code"])
        self.assertIn("host-a", "\n".join(report))
        self.assertIn("host-b", "\n".join(report))
        persisted = S.load_state(str(state_dir))
        self.assertEqual(persisted["units"]["code"]["host_judgment"],
                         "UNJUDGEABLE_HERE")


if __name__ == "__main__":
    unittest.main()
