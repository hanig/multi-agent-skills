"""ARC-750: present outputs cannot be reported as absent after a Git refusal."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S
import unit as U


class TestNoProducedChange(unittest.TestCase):
    def git(self, repo, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="t@x",
                   GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="t@x")
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, env=env,
            capture_output=True, text=True).stdout.strip()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q")
        (self.repo / "tracked.txt").write_text("base\n")
        self.git(self.repo, "add", "tracked.txt")
        self.git(self.repo, "commit", "-qm", "base")
        self.git(self.repo, "branch", "-M", "main")
        self.remote = self.root / "origin.git"
        self.git(self.repo, "init", "-q", "--bare", str(self.remote))
        self.git(self.repo, "remote", "add", "origin", str(self.remote))
        self.git(self.repo, "push", "-q", "origin", "main")

        # The only external service is a settled Paseo lifecycle. Git, the
        # checker subprocess, receipt attestation and advance are real.
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        paseo = bin_dir / "paseo"
        paseo.write_text(
            '#!/bin/sh\nif [ "$1" = inspect ]; then\n'
            '  echo \'{"status":"idle"}\'\nelse\n  exit 127\nfi\n')
        paseo.chmod(0o755)
        patch = mock.patch.dict(os.environ, {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")})
        patch.start()
        self.addCleanup(patch.stop)

        self.attempt = self.root / "runs" / "u" / "attempt"
        self.attempt.mkdir(parents=True)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.unit = {"id": "u", "kind": "code", "repo": str(self.repo),
                     "target_branch": "main", "runtime": "none",
                     "prompt": "make a change", "mode": "full-access",
                     "outputs": ["evidence.md", "test-output.txt"],
                     "continuation": {"max": 2}}
        error, launch = S._capture_code_launch(str(self.attempt), self.unit)
        self.assertIsNone(error)
        self.us = {"state": "SUBMITTED", "job_id": "agent-test",
                   "attempt_dir": str(self.attempt),
                   "attempts": [str(self.attempt)], "gpu_hours": 0,
                   "incomplete_since": time.time() - S.SETTLE_S - 60,
                   "attempt_launch_intents": {"attempt": launch["intent"]}}
        self.state = {"schema_version": 1, "halted": None,
                      "units": {"u": self.us}}
        self.workspace = self.root / "workspace"
        self.git(self.repo, "worktree", "add", "-q", "-b",
                 launch["intent"]["branch"], str(self.workspace), launch["base"])
        self.assertIsNone(S._complete_code_launch(
            self.state, self.unit, str(self.attempt), str(self.workspace)))
        self.facts = self.us["attempt_launch_facts"]["attempt"]
        S._capture_artifact_basis(self.state, "u", str(self.attempt), self.unit)
        U.write_json(self.attempt / U.UNIT, {
            "schema_version": 1, "task_id": "u", "attempt_id": "attempt",
            "kind": "code", "repo": str(self.repo), "job_id": "agent-test",
            "declared_outputs": self.unit["outputs"]})
        for name in self.unit["outputs"]:
            (self.attempt / name).write_text("present\n")
        acquired, why = S.acquire_lease(str(self.state_dir))
        self.assertTrue(acquired, why)
        self.addCleanup(S.release_lease, str(self.state_dir))

    def push(self):
        self.git(self.workspace, "push", "-q", "origin",
                 "HEAD:refs/heads/" + self.facts["branch"])

    def advance(self):
        # Cleanup is a separate unit's region. Do not exercise teardown here.
        with mock.patch.object(S, "_archive_code_worktree"), \
             mock.patch.object(S, "maybe_continue", wraps=S.maybe_continue) as cont:
            report, dispatched, halted = S.advance(
                {"name": "arc750", "units": [self.unit]}, self.state,
                str(self.state_dir), str(self.root / "runs"), False, max_new=0)
        self.assertEqual(dispatched, 0)
        self.assertIsNone(halted)
        receipt, why = S.attested_receipt(self.state, "u", str(self.attempt))
        self.assertIsNone(why)
        saved = S.load_state(str(self.state_dir))["units"]["u"]
        self.assertEqual(saved["state"], self.us["state"])
        return "\n".join(report), receipt, cont

    def assert_no_change(self, production_state, detail):
        report, receipt, cont = self.advance()
        self.assertEqual(receipt["state"], "INCOMPLETE")
        self.assertIn("REASON=no-produced-change", receipt["notes"])
        self.assertNotIn("REASON=outputs-absent", receipt["notes"])
        self.assertEqual(receipt["basis"]["production_state"], production_state)
        self.assertEqual(set(receipt["outputs"]), set(self.unit["outputs"]))
        self.assertEqual(self.us["state"], "FAILED")
        self.assertFalse(self.us.get("attempt_produced_heads"))
        self.assertIn("no produced repository change", report)
        self.assertIn(detail, report)
        self.assertNotIn("outputs never appeared", report)
        self.assertNotIn("sacct", report)
        for note in receipt["notes"]:
            if not note.startswith("REASON="):
                self.assertIn(note, report)
        cont.assert_not_called()
        self.assertFalse(S.maybe_continue(
            str(self.state_dir), "u", self.unit, self.us, [], self.state))
        self.assertFalse(self.us.get("continuations"))

    def test_outputs_present_but_pushed_base_has_no_produced_change(self):
        self.push()
        self.assert_no_change("pushed-ref-no-tree-change", "still names the launch base")

    def test_empty_commit_is_not_produced_change(self):
        self.git(self.workspace, "commit", "--allow-empty", "-qm", "empty")
        self.push()
        self.assert_no_change("pushed-ref-no-tree-change", "tree is identical")

    def test_reverted_change_is_not_produced_change(self):
        (self.workspace / "tracked.txt").write_text("changed\n")
        self.git(self.workspace, "commit", "-qam", "change")
        self.git(self.workspace, "revert", "--no-edit", "HEAD")
        self.push()
        self.assert_no_change("pushed-ref-no-tree-change", "tree is identical")

    def test_unpushed_unchanged_worktree_uses_the_same_reason(self):
        self.assert_no_change("no-produced-change", "no committed change")

    def test_genuinely_absent_outputs_keep_outputs_absent(self):
        self.push()
        (self.attempt / "evidence.md").unlink()
        self.unit.pop("continuation")
        report, receipt, _cont = self.advance()
        self.assertIn("REASON=outputs-absent", receipt["notes"])
        self.assertNotIn("REASON=no-produced-change", receipt["notes"])
        self.assertIn("outputs never appeared", report)
        self.assertEqual(self.us["state"], "FAILED")

    def test_new_check_replaces_persisted_misclassification(self):
        self.push()
        digest = []
        U.write_json(self.attempt / U.RECEIPT, {
            "task_id": "u", "attempt_id": "attempt", "state": "INCOMPLETE",
            "notes": ["REASON=outputs-absent", "old incorrect reason"]},
            digest_out=digest)
        S._record_receipt_provenance(self.state, "u", str(self.attempt), digest[0])
        self.us["state"] = "FAILED"
        S.save_state(str(self.state_dir), self.state)
        self.state = S.load_state(str(self.state_dir))
        self.us = self.state["units"]["u"]
        self.assert_no_change("pushed-ref-no-tree-change", "still names the launch base")
        self.assertNotEqual(self.us["attempt_receipt_seals"]["attempt"], digest[0])

    def test_real_produced_change_still_reaches_ready_for_pr(self):
        (self.workspace / "tracked.txt").write_text("changed\n")
        self.git(self.workspace, "commit", "-qam", "change")
        self.push()
        _report, receipt, cont = self.advance()
        self.assertEqual(receipt["state"], "DONE")
        self.assertEqual(self.us["state"], "READY_FOR_PR")
        self.assertEqual(self.us["attempt_produced_heads"]["attempt"],
                         self.git(self.workspace, "rev-parse", "HEAD"))
        cont.assert_not_called()

    def test_specific_missing_push_reason_is_preserved(self):
        (self.workspace / "tracked.txt").write_text("changed\n")
        self.git(self.workspace, "commit", "-qam", "change")
        _report, receipt, cont = self.advance()
        self.assertIn("REASON=no-pushed-ref", receipt["notes"])
        self.assertNotIn("REASON=no-produced-change", receipt["notes"])
        self.assertEqual(self.us["state"], "FAILED_EVIDENCE")
        cont.assert_not_called()


if __name__ == "__main__":
    unittest.main()
