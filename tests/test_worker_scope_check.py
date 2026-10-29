"""Worker scope checks use real Git objects and never create merge authority."""

import json
import contextlib
import io
import os
from argparse import Namespace
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest import mock

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
        command = [sys.executable, "-B", str(SCRIPTS / "swarm.py"), "scope-check",
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


class TestDispatchedScopeCommand(unittest.TestCase):
    def setUp(self):
        self.fixture = worktree_fixture.TestPerAttemptWorktrees("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def dispatch(self, spaced=False, recovered=False):
        fixture = self.fixture
        unit = worktree_fixture.code_unit(fixture.repo)
        unit["scope"] = ["tracked.txt"]
        unit["outputs"] = ["evidence.md"]
        suffix = " with ' quotes" if spaced else ""
        plan_path = fixture.tmp / ("plan" + suffix + ".json")
        self.state_dir = fixture.tmp / ("state" + suffix)
        plan_path.write_text(json.dumps({"name": "worker-scope", "units": [unit]}))
        args = Namespace(plan=str(plan_path), state_dir=str(self.state_dir),
                         root=str(fixture.tmp / "runs"), dry_run=False,
                         max_new_dispatches=1)
        if recovered:
            # Crash after worktree creation, before agent creation. The
            # redispatch path must also use this invocation's plan path.
            attempt = fixture.attempt("code", "recover-scope")
            unit, state = fixture.intent_state(attempt)
            us = state["units"]["code"]
            us.update(state="ALLOCATED", attempt_dir=str(attempt),
                      attempts=[str(attempt)], gpu_hours=0.0)
            intent = us["attempt_launch_intents"][attempt.name]
            _path, _pointer, error = S.SCL._create_code_worktree(self.state_dir, intent)
            self.assertIsNone(error)
            S.SS.save_state(self.state_dir, state)
        real = fixture.fake

        def service(argv, **kwargs):
            if argv[:3] == ["paseo", "ls", "--json"]:
                return 0, "[]", ""
            if argv[:3] == ["paseo", "workspace", "ls"]:
                return 0, "[]", ""
            return real(argv, **kwargs)

        # Fake service discovery/creation and watcher scheduling only. The
        # command handler, advance, submit, renderer and Git are exercised.
        with worktree_fixture.paseo_resolvable(), \
                mock.patch.object(S.U, "run", side_effect=service), \
                mock.patch.object(S, "_start_code_terminal_watchers", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = S.cmd_run(args)
        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(len(fixture.fake.launches), 1, output.getvalue())
        argv = fixture.fake.launches[0]
        worktree = Path(argv[argv.index("--cwd") + 1])
        self.assertNotEqual(worktree, fixture.repo)
        prompt = argv[-1]
        command = next(block for block in re.findall(r"```sh\n(.*?)\n```", prompt, re.S)
                       if " scope-check " in block)
        self.assertLess(prompt.index(command), prompt.index("Before opening a pull request"))
        self.assertIn("fix any out-of-scope path before review", prompt)
        self.assertIn("fix the named paths and rerun before review", prompt)
        self.assertIn("merge still requires the coordinator's judged report", prompt)
        state = S.SS.load_state(self.state_dir)
        intent = next(iter(state["units"]["code"]["attempt_launch_intents"].values()))
        self.assertNotIn("scope_plan", intent)
        self.assertNotIn("scope_state_dir", intent)
        return worktree, command

    def run_command(self, path, expected, spaced=False, recovered=False,
                    git_overrides=False):
        worktree, command = self.dispatch(spaced=spaced, recovered=recovered)
        operator_head = worktree_fixture.git(self.fixture.repo, "rev-parse", "HEAD")
        (worktree / path).write_text("worker commit\n")
        worktree_fixture.git(worktree, "add", "-A")
        worktree_fixture.git(worktree, "commit", "-qm", "worker change")
        head = worktree_fixture.git(worktree, "rev-parse", "HEAD")
        self.assertNotEqual(head, operator_head)
        self.assertEqual(worktree_fixture.git(self.fixture.repo, "rev-parse", "HEAD"),
                         operator_head)
        env = dict(os.environ)
        if git_overrides:
            # An inherited operator environment must neither falsely reject
            # honest work nor hide an out-of-scope worker change.
            (self.fixture.repo / "operator-only.txt").write_text("operator change\n")
            worktree_fixture.git(self.fixture.repo, "add", "-A")
            worktree_fixture.git(self.fixture.repo, "commit", "-qm", "operator change")
            env.update(GIT_DIR=str(self.fixture.repo / ".git"),
                       GIT_COMMON_DIR=str(self.fixture.repo / ".git"),
                       GIT_WORK_TREE=str(self.fixture.repo))
        state_file = self.state_dir / S.STATE_FILE
        before = state_file.read_bytes()
        result = subprocess.run(["/bin/sh", "-c", command], cwd=worktree,
                                env=env, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "in_scope" if expected == 0 else "out_of_scope")
        self.assertEqual(report["head_source"], "worker")
        self.assertEqual(report["head"], head)
        self.assertEqual(report["out_of_scope"], [] if expected == 0 else [path])
        self.assertEqual(state_file.read_bytes(), before)

    def test_rendered_command_checks_in_scope_worker_commit(self):
        self.run_command("tracked.txt", 0)

    def test_rendered_command_rejects_outside_worker_commit(self):
        self.run_command("outside.txt", S.EXIT_SCOPE_OUTSIDE)

    def test_rendered_command_quotes_plan_and_state_paths(self):
        self.run_command("tracked.txt", 0, spaced=True)

    def test_recovered_dispatch_supplies_scope_command(self):
        self.run_command("outside.txt", S.EXIT_SCOPE_OUTSIDE, recovered=True)

    def test_rendered_command_ignores_operator_git_overrides(self):
        self.run_command("tracked.txt", 0, git_overrides=True)

    def test_outside_worker_remains_outside_with_operator_git_overrides(self):
        self.run_command("outside.txt", S.EXIT_SCOPE_OUTSIDE, git_overrides=True)

    def test_pathless_protocol_still_requires_resolved_worker_head(self):
        attempt = self.fixture.attempt("code", "pathless")
        _unit, state = self.fixture.intent_state(attempt)
        intent = state["units"]["code"]["attempt_launch_intents"][attempt.name]
        prompt = S._code_completion_protocol(intent)
        self.assertIn("obtain the exact scope-check command", prompt)
        self.assertIn('`--head "$(git rev-parse --verify HEAD^{commit})"`', prompt)
        self.assertIn("Git repository overrides cleared", prompt)
        self.assertIn("fix the named paths and rerun before review", prompt)
        self.assertIn("merge still requires the coordinator's judged report", prompt)
        self.assertLess(prompt.index("--head"), prompt.index("Before opening a pull request"))


if __name__ == "__main__":
    unittest.main()
