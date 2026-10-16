"""Worker scope checks use real Git objects and never create merge authority."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

from tests import test_arc809_scope_check as scope_fixture
from tests import test_attempt_worktrees as worktree_fixture
from tests.scheduler_fixture import isolated_module_path
from tests.scheduler_fixture import cleanup_module_path as tearDownModule

S = scope_fixture.S
SCRIPTS = scope_fixture.SCRIPTS


def setUpModule():
    isolated_module_path()


class TestWorkerScopeCheck(unittest.TestCase):
    # Reuse setup helpers without rediscovering the existing suite here.
    setUp = scope_fixture.TestScopeCheck.setUp
    git = scope_fixture.TestScopeCheck.git
    write = scope_fixture.TestScopeCheck.write
    commit = scope_fixture.TestScopeCheck.commit

    def check(self, head=None):
        self.plan_path.write_text(json.dumps(self.plan))
        (self.state_dir / S.STATE_FILE).write_text(json.dumps(self.state))
        before = {p.name: p.read_bytes() for p in self.state_dir.iterdir()}
        command = [sys.executable, str(SCRIPTS / "swarm.py"), "scope-check",
                   str(self.plan_path), "--state-dir", str(self.state_dir),
                   "--unit", "u", "--json"]
        if head is not None:
            command += ["--head", head]
        # Neither the caller's cwd nor its ambient Git variables select repo.
        env = dict(self.env, GIT_DIR=str(self.root / "not-a-repository"))
        result = subprocess.run(command, cwd=self.root, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stderr, "")
        self.assertEqual({p.name: p.read_bytes() for p in self.state_dir.iterdir()}, before)
        return result, json.loads(result.stdout)

    def test_in_scope_before_judgment_resolves_revision_to_full_oid(self):
        self.write("tests/new.py", "new\n")
        head = self.commit("worker change")
        self.git("tag", "-a", "candidate", "-m", "candidate")
        result, report = self.check("candidate")
        self.assertEqual(result.returncode, 0, report)
        self.assertEqual(report["status"], "in_scope")
        self.assertEqual(report["head"], head)
        self.assertEqual(report["base"], self.base)
        self.assertEqual(report["head_source"], "worker")
        self.assertEqual(self.us["attempt_produced_heads"], {})
        # The advisory success must not make the ordinary check pass.
        result, report = self.check()
        self.assertEqual(result.returncode, S.EXIT_SCOPE_UNCHECKED, report)
        self.assertEqual(report["head_source"], "judged")
        self.assertIsNone(report["head"])

    def test_outside_revision_is_checked_instead_of_operator_head(self):
        self.git("checkout", "-qb", "worker")
        self.write("outside.txt", "worker change\n")
        head = self.commit("outside")
        self.git("checkout", "--detach", self.base)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        result, report = self.check("worker")
        self.assertEqual(result.returncode, S.EXIT_SCOPE_OUTSIDE, report)
        self.assertEqual(report["head"], head)
        self.assertEqual(report["head_source"], "worker")
        self.assertEqual(report["out_of_scope"], ["outside.txt"])

    def test_outside_deletion_is_reported(self):
        (self.repo / "skills/x/SKILL.md").unlink()
        head = self.commit("remove outside")
        result, report = self.check(head)
        self.assertEqual(result.returncode, S.EXIT_SCOPE_OUTSIDE, report)
        self.assertEqual(report["out_of_scope"], ["skills/x/SKILL.md"])
        self.assertEqual(report["deletions_out_of_scope"], ["skills/x/SKILL.md"])

    def test_invalid_worker_revision_is_unchecked(self):
        for revision in ("missing-revision", self.base + "^{tree}"):
            with self.subTest(revision=revision):
                result, report = self.check(revision)
                self.assertEqual(result.returncode, S.EXIT_SCOPE_UNCHECKED, report)
                self.assertEqual(report["head_source"], "worker")
                self.assertIn("local Git read failed", report["reason"])

    def test_no_head_report_preserves_complete_legacy_shape_and_output(self):
        self.write("tests/new.py", "new\n")
        head = self.commit("produced")
        baseline = {"unit": "u", "status": "unchecked", "attempt": "a1",
                    "base": self.base, "head": None, "scope": ["tests/**"],
                    "repository": None, "target": "main", "state_epoch": 0,
                    "out_of_scope": [], "deletions_out_of_scope": [],
                    "reason": "no valid judged head for this attempt in coordinator state"}
        for judged in (False, True):
            with self.subTest(judged=judged):
                if judged:
                    self.us["attempt_produced_heads"]["a1"] = head
                    baseline.update(status="in_scope", head=head)
                    baseline.pop("reason")
                result, report = self.check()
                self.assertEqual(result.returncode, 0 if judged else S.EXIT_SCOPE_UNCHECKED)
                self.assertEqual(report.pop("head_source"), "judged")
                self.assertEqual(report, baseline)
                # Existing JSON formatting is unchanged after removing the one key.
                actual = "\n".join(line for line in result.stdout.splitlines()
                                   if '"head_source":' not in line) + "\n"
                self.assertEqual(actual, json.dumps(baseline, indent=2, sort_keys=True) + "\n")

    def test_dispatched_protocol_command_runs_in_attempt_worktree(self):
        fixture = worktree_fixture.TestPerAttemptWorktrees("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        unit = worktree_fixture.code_unit(fixture.repo)
        unit["scope"] = ["tracked.txt"]
        plan_path = fixture.tmp / "plan with ' quotes.json"
        plan_path.write_text(json.dumps({"name": "worker-scope", "units": [unit]}))
        state_dir = fixture.tmp / "state with ' quotes"
        attempt = fixture.attempt("code", "worker-scope")
        state = {"units": {"code": {"attempt_dir": str(attempt)}}}
        with worktree_fixture.paseo_resolvable():
            job, error = S._submit(unit, str(attempt), False, state, state_dir,
                                   plan_path=plan_path)
        self.assertIsNone(error)
        self.assertIsNotNone(job)
        argv = fixture.fake.launches[0]
        worktree = Path(argv[argv.index("--cwd") + 1])
        self.assertNotEqual(worktree, fixture.repo)
        (worktree / "tracked.txt").write_text("worker commit\n")
        worktree_fixture.git(worktree, "commit", "-qam", "worker change")
        head = worktree_fixture.git(worktree, "rev-parse", "HEAD")
        self.assertNotEqual(head, worktree_fixture.git(fixture.repo, "rev-parse", "HEAD"))
        prompt = argv[-1]
        command = next(block for block in re.findall(r"```sh\n(.*?)\n```", prompt, re.S)
                       if " scope-check " in block)
        self.assertLess(prompt.index(command), prompt.index("Before opening a pull request"))
        self.assertIn("fix any out-of-scope path before review", prompt)
        before = (state_dir / S.STATE_FILE).read_bytes()
        result = subprocess.run(["/bin/sh", "-c", command], cwd=worktree,
                                env=dict(os.environ), capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "in_scope")
        self.assertEqual(report["head_source"], "worker")
        self.assertEqual(report["head"], head)
        self.assertEqual(report["out_of_scope"], [])
        self.assertEqual((state_dir / S.STATE_FILE).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
