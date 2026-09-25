"""ARC-809: exercise scope-check through its CLI against real Git commits."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S


class TestScopeCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("GIT_")}
        self.env.update({"GIT_AUTHOR_NAME": "Scope Test",
                         "GIT_AUTHOR_EMAIL": "scope@example.invalid",
                         "GIT_COMMITTER_NAME": "Scope Test",
                         "GIT_COMMITTER_EMAIL": "scope@example.invalid",
                         "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_CONFIG_GLOBAL": os.devnull})
        self.git("init", "-q")
        self.write("tests/existing.py", "base test\n")
        self.write("skills/x/SKILL.md", "existing skill\n")
        self.base = self.commit("base")
        self.unit = {"id": "u", "kind": "code", "repo": str(self.repo),
                     "target_branch": "main", "scope": ["tests/**"]}
        self.plan = {"name": "scope", "units": [self.unit]}
        self.attempt = self.root / "attempts/a1"
        self.attempt.mkdir(parents=True)
        self.intent = {"schema_version": 1, "unit_id": "u",
                       "attempt_id": "a1", "repo": str(self.repo),
                       "base_commit": self.base,
                       "base_tree": self.git("rev-parse", "HEAD^{tree}"),
                       "worktree_slug": "a1", "branch": "swarm-a1",
                       "target_branch": "main", "captured_at": "test"}
        self.us = {"attempt_dir": str(self.attempt),
                   "attempt_launch_intents": {"a1": self.intent},
                   "attempt_produced_heads": {}}
        self.state = {"units": {"u": self.us}}
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.plan_path = self.root / "plan.json"

    def git(self, *args):
        proc = subprocess.run(["git", "-C", str(self.repo)] + list(args),
                              env=self.env, capture_output=True, check=True)
        return proc.stdout.decode("utf-8", "surrogateescape").strip()

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def pin(self):
        self.us["attempt_produced_heads"]["a1"] = self.commit("produced")

    def check(self, json_output=True, env=None):
        self.plan_path.write_text(json.dumps(self.plan))
        state_file = self.state_dir / S.STATE_FILE
        state_file.write_text(json.dumps(self.state))
        before = {p.name: p.read_bytes() for p in self.state_dir.iterdir()}
        cmd = [sys.executable, str(SCRIPTS / "swarm.py"), "scope-check",
               str(self.plan_path), "--state-dir", str(self.state_dir),
               "--unit", "u"]
        if json_output:
            cmd.append("--json")
        proc = subprocess.run(cmd, cwd=self.repo, env=env or self.env,
                              capture_output=True, text=True, check=False)
        self.assertEqual({p.name: p.read_bytes() for p in self.state_dir.iterdir()}, before)
        self.assertEqual(proc.stderr, "")
        return proc.returncode, (json.loads(proc.stdout) if json_output
                                 else proc.stdout)

    def test_000_outside_deletion_is_a_failure_and_separately_reported(self):
        self.write("tests/new.py", "new test\n")
        (self.repo / "skills/x/SKILL.md").unlink()
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 1, report)
        self.assertEqual(report["status"], "out_of_scope")
        self.assertEqual(report["out_of_scope"], ["skills/x/SKILL.md"])
        self.assertEqual(report["deletions_out_of_scope"],
                         ["skills/x/SKILL.md"])

    def test_report_emits_exact_coordinator_binding_and_epoch(self):
        self.write("tests/new.py", "new test\n")
        self.pin()
        self.intent["repository_remote"] = "https://Example.invalid/Owner/Project.git/"
        self.state["epoch"] = 999  # obsolete inline metadata is not the fence
        (self.state_dir / S.STATE_EPOCH_FILE).write_text(json.dumps({"epoch": 17}))
        code, report = self.check()
        self.assertEqual(code, 0, report)
        expected = {"unit": "u", "attempt": "a1", "base": self.base,
                    "head": self.us["attempt_produced_heads"]["a1"],
                    "repository": self.intent["repository_remote"],
                    "target": self.intent["target_branch"], "state_epoch": 17}
        self.assertEqual({key: report.get(key) for key in expected}, expected)

    def test_absent_scope_still_reports_coordinator_binding(self):
        del self.unit["scope"]
        self.write("tests/new.py", "new test\n")
        self.pin()
        self.intent["repository_remote"] = "git@example.invalid:Owner/Project.git"
        code, report = self.check()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "unchecked")
        self.assertEqual(report["repository"], self.intent["repository_remote"])
        self.assertEqual(report["attempt"], "a1")
        self.assertEqual(report["head"], self.us["attempt_produced_heads"]["a1"])
        self.assertEqual(report["base"], self.base)
        self.assertEqual(report["target"], "main")
        self.assertEqual(report["state_epoch"], 0)

    def test_legacy_absent_epoch_is_observed_as_zero_without_writes(self):
        self.write("tests/new.py", "new test\n")
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["state_epoch"], 0)
        self.assertIsNone(report["repository"])
        self.assertFalse((self.state_dir / S.STATE_EPOCH_FILE).exists())

    def test_malformed_epoch_is_unchecked(self):
        self.write("tests/new.py", "new test\n")
        self.pin()
        (self.state_dir / S.STATE_EPOCH_FILE).write_text('{"epoch": true}')
        code, report = self.check()
        self.assertEqual(code, 2, report)
        self.assertIsNone(report["state_epoch"])
        self.assertIn("epoch", report["reason"])

    def test_in_scope_change_passes_at_multiple_depths(self):
        self.write("tests/new.py", "new test\n")
        self.write("tests/nested/deeper/new.py", "nested test\n")
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "in_scope")
        self.assertEqual(report["out_of_scope"], [])
        self.assertEqual(report["deletions_out_of_scope"], [])

    def test_absent_scope_is_unchecked(self):
        del self.unit["scope"]
        code, text = self.check(json_output=False)
        self.assertEqual(code, 2, text)
        self.assertIn("unchecked", text)
        self.assertIn("no scope", text)

    def test_empty_scope_allows_no_changes(self):
        self.unit["scope"] = []
        self.write("tests/new.py", "new test\n")
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 1, report)
        self.assertEqual(report["out_of_scope"], ["tests/new.py"])

    def test_unjudged_attempt_ignores_forged_files_and_previous_head(self):
        self.write("tests/new.py", "new test\n")
        head = self.commit("worker commit")
        forged = {"repo": str(self.repo), "base_commit": self.base,
                  "produced_head": head, "state": "DONE"}
        (self.attempt / "receipt.json").write_text(json.dumps(forged))
        (self.attempt.parent / "launch-a1.json").write_text(json.dumps(forged))
        self.us["produced_head"] = head
        self.us["attempt_produced_heads"]["older"] = head
        code, report = self.check()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "unchecked")
        self.assertIn("judged head", report["reason"])

    def test_missing_intent_does_not_fall_back_to_worker_record(self):
        self.write("tests/new.py", "new test\n")
        self.pin()
        (self.attempt.parent / "launch-a1.json").write_text(
            json.dumps(self.intent))
        self.us["attempt_launch_intents"] = {}
        code, report = self.check()
        self.assertEqual(code, 2, report)
        self.assertIn("launch intent", report["reason"])

    def test_wrong_attempt_intent_is_unchecked(self):
        self.intent["attempt_id"] = "older"
        code, report = self.check()
        self.assertEqual(code, 2, report)
        self.assertIn("belongs to", report["reason"])

    def test_repository_also_comes_from_coordinator_intent(self):
        self.write("tests/new.py", "new test\n")
        self.pin()
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        self.unit["repo"] = str(unrelated)
        code, report = self.check()
        self.assertEqual(code, 0, report)

    def test_pinned_base_and_head_ignore_current_branch_and_worker_files(self):
        (self.repo / "skills/x/SKILL.md").unlink()
        self.pin()
        head = self.us["attempt_produced_heads"]["a1"]
        # Later work restores the deletion; diffing HEAD would hide it.
        self.write("skills/x/SKILL.md", "existing skill\n")
        self.write("tests/later.py", "later\n")
        later = self.commit("later")
        forged = dict(self.intent, base_commit=head, produced_head=later)
        (self.attempt.parent / "launch-a1.json").write_text(json.dumps(forged))
        (self.attempt / "receipt.json").write_text(json.dumps(forged))
        code, report = self.check()
        self.assertEqual(code, 1, report)
        self.assertEqual(report["base"], self.base)
        self.assertEqual(report["head"], head)
        self.assertEqual(report["out_of_scope"], ["skills/x/SKILL.md"])

    def test_rename_away_checks_old_path(self):
        self.git("mv", "skills/x/SKILL.md", "tests/moved.md")
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 1, report)
        self.assertEqual(report["out_of_scope"], ["skills/x/SKILL.md"])
        self.assertEqual(report["deletions_out_of_scope"],
                         ["skills/x/SKILL.md"])

    def test_rename_destination_is_also_checked(self):
        self.git("mv", "tests/existing.py", "outside.py")
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 1, report)
        self.assertEqual(report["out_of_scope"], ["outside.py"])
        self.assertEqual(report["deletions_out_of_scope"], [])

    def test_in_scope_deletion_and_rename_pass(self):
        self.write("tests/another.py", "another\n")
        self.commit("additional base file")
        self.intent["base_commit"] = self.git("rev-parse", "HEAD")
        self.intent["base_tree"] = self.git("rev-parse", "HEAD^{tree}")
        self.git("mv", "tests/existing.py", "tests/moved.py")
        (self.repo / "tests/another.py").unlink()
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 0, report)

    def test_modification_addition_and_type_change_are_all_checked(self):
        self.write("skills/x/SKILL.md", "modified\n")
        self.write("outside.txt", "added\n")
        (self.repo / "tests/existing.py").unlink()
        (self.repo / "tests/existing.py").symlink_to("../outside.txt")
        self.unit["scope"] = ["tests/new*"]
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 1, report)
        self.assertEqual(report["out_of_scope"],
                         ["outside.txt", "skills/x/SKILL.md", "tests/existing.py"])
        self.assertEqual(report["deletions_out_of_scope"], [])

    def test_case_sensitive_fnmatch_and_literal_whitespace(self):
        paths = ["tests/deep/test_a.py", "tests/ tab\tline\n.py ",
                 "Other.PY"]
        for path in paths:
            self.write(path, path)
        self.unit["scope"] = ["tests/**/test_[ab].p?", paths[1], "*.py"]
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 1, report)
        self.assertEqual(report["out_of_scope"], ["Other.PY"])

    def test_non_utf8_path_bytes_are_not_replaced_before_matching(self):
        raw_path = os.fsencode(self.repo) + b"/outside-\xff"
        try:
            with open(raw_path, "wb") as handle:
                handle.write(b"outside")
        except OSError:
            self.skipTest("filesystem cannot store non-UTF-8 filenames")
        self.unit["scope"] = ["outside-\ufffd"]
        self.pin()
        code, report = self.check()
        self.assertEqual(code, 1, report)
        self.assertEqual(report["out_of_scope"], ["outside-\udcff"])

    def test_git_config_and_environment_cannot_hide_deletions(self):
        (self.repo / "skills/x/SKILL.md").unlink()
        self.pin()
        marker = self.root / "external-diff-ran"
        script = self.root / "external-diff"
        script.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        script.chmod(0o755)
        self.git("config", "diff.external", str(script))
        self.git("config", "diff.relative", "true")
        # Replacing a pinned object must not turn the comparison into no-op.
        self.git("replace", self.us["attempt_produced_heads"]["a1"], self.base)
        env = dict(self.env, GIT_DIR=str(self.root / "nonexistent"),
                   GIT_EXTERNAL_DIFF=str(script))
        code, report = self.check(env=env)
        self.assertEqual(code, 1, report)
        self.assertEqual(report["deletions_out_of_scope"],
                         ["skills/x/SKILL.md"])
        self.assertFalse(marker.exists())

    def test_missing_object_is_unchecked_and_never_uses_branch(self):
        self.us["attempt_produced_heads"]["a1"] = "f" * 40
        code, report = self.check()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "unchecked")
        self.assertIn("local Git read failed", report["reason"])

    def test_missing_promisor_tree_cannot_launch_a_remote_helper(self):
        self.write("tests/new.py", "new test\n")
        self.pin()
        marker = self.root / "remote-contacted"
        helper = self.root / "git-remote-scope-test"
        helper.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\nexit 1\n")
        helper.chmod(0o755)
        self.git("remote", "add", "origin", "scope-test::unavailable")
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.partialclonefilter", "blob:none")
        tree = self.intent["base_tree"]
        (self.repo / ".git/objects" / tree[:2] / tree[2:]).unlink()
        env = dict(self.env, PATH=str(self.root) + os.pathsep + self.env["PATH"])
        code, report = self.check(env=env)
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "unchecked")
        self.assertFalse(marker.exists(), "scope-check contacted a remote")

    def test_text_output_names_the_deletion_separately(self):
        (self.repo / "skills/x/SKILL.md").unlink()
        self.pin()
        code, text = self.check(json_output=False)
        self.assertEqual(code, 1, text)
        self.assertIn('out of scope: "skills/x/SKILL.md"', text)
        self.assertIn('deletion or rename-away out of scope: '
                      '"skills/x/SKILL.md"', text)


class TestScopeValidation(unittest.TestCase):
    def plan(self, value):
        return {"name": "scope-validation", "units": [
            {"id": "u", "kind": "slurm", "runtime": "none",
             "command": "true", "outputs": ["result"], "scope": value}]}

    def test_string_scope_is_refused_by_validate(self):
        with self.assertRaisesRegex(S.PlanError, "scope"):
            S.validate_plan(self.plan("tests/**"))

    def test_non_list_or_non_string_elements_are_refused(self):
        for value in (None, 3, True, {}, [1], ["tests/**", None], [False]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(S.PlanError, "scope"):
                    S.validate_plan(self.plan(value))

    def test_absolute_and_parent_segments_are_refused(self):
        for pattern in ("/tests/**", "../tests/**", "tests/../**",
                        "tests/..", "C:/tests/**", "\\server\\tests",
                        "tests\\..\\outside", "", "tests/\0*"):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(S.PlanError, "scope"):
                    S.validate_plan(self.plan([pattern]))

    def test_valid_patterns_are_not_normalized(self):
        value = ["tests/**", "test_?.[py]", " literal ", "file..name"]
        self.assertEqual(S.validate_plan(self.plan(value))["units"], 1)
        self.assertEqual(S.declared_scope({"scope": value}), value)

    def test_scope_is_documented_in_cli_schema(self):
        proc = subprocess.run([sys.executable, str(SCRIPTS / "swarm.py"),
                               "schema", "--json"], capture_output=True,
                              text=True, check=True)
        fields = {row["field"]: row for row in json.loads(proc.stdout)["fields"]}
        self.assertEqual(fields["scope"]["requirement"], "optional")
        self.assertIn("JSON list", fields["scope"]["notes"])


if __name__ == "__main__":
    unittest.main()
