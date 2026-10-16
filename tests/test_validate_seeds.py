"""Opt-in seed admission checks run before allocating any attempt."""
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
SWARM = ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
sys.path.insert(0, str(SWARM.parent))
import swarm_plan_rules as SPR


class ValidateSeedsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        git = shutil.which("git") or shutil.which("git", path=os.defpath)
        self.assertIsNotNone(git)
        (self.bin / "git").symlink_to(git)
        # Validation requires the executable name, but must not launch an agent.
        paseo = self.bin / "paseo"
        paseo.write_text("#!/bin/sh\nexit 127\n")
        paseo.chmod(0o755)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state"
        self.env = dict(os.environ, PATH=str(self.bin), HOME=str(self.home),
                        XDG_STATE_HOME=str(self.state),
                        # Apple Python otherwise writes import caches under HOME.
                        PYTHONDONTWRITEBYTECODE="1",
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="test",
                        GIT_COMMITTER_EMAIL="test@example.invalid")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        (self.repo / "file.txt").write_text("base\n")
        self.git("add", "file.txt")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", str(self.origin))
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-qb", "seed")
        (self.repo / "file.txt").write_text("seed\n")
        self.git("commit", "-qam", "seed")
        self.head = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", "seed")
        self.git("checkout", "-q", "main")
        self.seed = {"ref": "refs/heads/seed", "base": self.base,
                     "head": self.head}
        self.plan = self.tmp / "plan.json"

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              env=self.env, check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()

    def unit(self, uid="repair", **changes):
        return dict({"id": uid, "kind": "code", "repo": str(self.repo),
                     "target_branch": "main", "provider": "codex",
                     "mode": "full-access", "prompt": "Repair",
                     "outputs": ["evidence.md"], "seed": dict(self.seed)},
                    **changes)

    def validate(self, units, check=True):
        self.plan.write_text(json.dumps({"units": units}))
        return subprocess.run(
            [sys.executable, str(SWARM), "validate", str(self.plan)]
            + (["--check-seeds"] if check else []), cwd=str(self.tmp),
            env=self.env, capture_output=True, text=True, timeout=30)

    def assert_seed_failure(self, result, uid, seed):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("plan is valid", result.stdout)
        self.assertIn(
            "unit {!r}: seed range {}..{} is not reachable in order from "
            "seed.ref {!r}".format(uid, seed["base"], seed["head"], seed["ref"]),
            result.stderr.splitlines())

    def test_base_not_ancestor_fails_with_unit_and_reason(self):
        unrelated = self.git("commit-tree", "HEAD^{tree}", "-m", "unrelated")
        seed = dict(self.seed, base=unrelated)
        self.assert_seed_failure(self.validate([self.unit(seed=seed)]),
                                 "repair", seed)

    def test_head_not_reachable_from_ref_fails(self):
        seed = dict(self.seed, ref="refs/heads/main")
        self.assert_seed_failure(self.validate([self.unit(seed=seed)]),
                                 "repair", seed)

    def test_correct_seed_passes_and_temporary_ref_is_removed(self):
        result = self.validate([self.unit()])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plan is valid", result.stdout)
        self.assertEqual(self.git("for-each-ref", "refs/hanig-swarm-seeds/"), "")

    def test_two_failing_seeded_units_are_both_reported(self):
        first = dict(self.seed, base=self.head, head=self.base)
        second = dict(self.seed, ref="refs/heads/main")
        result = self.validate([self.unit("first", seed=first),
                                self.unit("second", seed=second)])
        self.assert_seed_failure(result, "first", first)
        self.assert_seed_failure(result, "second", second)
        self.assertEqual(len([line for line in result.stderr.splitlines()
                              if line.startswith("unit ")]), 2)

    def test_without_flag_broken_seed_validates_without_git(self):
        (self.bin / "git").unlink()
        self.assertIsNone(shutil.which("git", path=self.env["PATH"]))
        result = self.validate([self.unit(seed=dict(self.seed, base="0" * 40))],
                               check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plan is valid", result.stdout)

    def test_without_seed_flag_passes_without_git(self):
        unit = self.unit(repo=str(self.tmp / "nonexistent"))
        del unit["seed"]
        (self.bin / "git").unlink()
        self.assertIsNone(shutil.which("git", path=self.env["PATH"]))
        result = self.validate([unit])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_ref_syntax_matches_git_without_running_it(self):
        suffixes = ["seed", "nested/seed", "-leading-dash", "@", "a@b",
                    "a.locked", "a.LOCK", "a./b", "a]b", "caf\u00e9", "a\u00a0b",
                    "", "/seed", "seed/", "a//b", ".hidden", "a/.hidden/b",
                    "a.lock", "a.lock/b", "a..b", "a.", "a@{b"]
        suffixes += ["a" + chr(code) + "b" for code in range(1, 128)]
        for suffix in suffixes:
            ref = "refs/heads/" + suffix
            expected = subprocess.run(
                ["git", "check-ref-format", ref], env=self.env,
                capture_output=True, timeout=30).returncode == 0
            unit = self.unit(seed=dict(self.seed, ref=ref))
            with self.subTest(ref=ref), mock.patch.object(
                    SPR.U, "run", side_effect=AssertionError("offline Git call")):
                if expected:
                    self.assertIs(SPR.declared_seed(unit), unit["seed"])
                else:
                    with self.assertRaisesRegex(SPR.PlanError, "seed.ref"):
                        SPR.declared_seed(unit)
        with self.assertRaisesRegex(SPR.PlanError, "seed.ref"):
            SPR.declared_seed(self.unit(seed=dict(self.seed, ref="refs/heads/a\0b")))

    def test_source_resolution_failure_does_not_skip_later_seed(self):
        seed = dict(self.seed, ref="refs/heads/main")
        result = self.validate([self.unit("missing", repo=str(self.tmp / "absent")),
                                self.unit("unreachable", seed=seed)])
        self.assertIn("unit 'missing':", result.stderr)
        self.assert_seed_failure(result, "unreachable", seed)

    def test_checkout_must_match_dispatch_target(self):
        self.git("checkout", "-q", "seed")
        result = self.validate([self.unit()])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unit 'repair':", result.stderr)
        self.assertIn("launch source must equal the pinned target commit", result.stderr)

    def test_remote_only_seed_uses_origin_push_destination(self):
        # A fresh main-only clone needs the seed fetch; its fetch URL is unusable.
        fresh = self.tmp / "fresh"
        self.git("clone", "-q", "--single-branch", "--branch", "main",
                 str(self.origin), str(fresh))
        self.repo = fresh
        self.git("remote", "set-url", "--push", "origin", str(self.origin))
        self.git("remote", "set-url", "origin", str(self.tmp / "absent.git"))
        result = self.validate([self.unit()])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def snapshot(self):
        # Git fetch metadata is permitted; all other bytes and directory names
        # (including missing state/attempt roots) must remain identical.
        result = {}
        for path in self.tmp.rglob("*"):
            relative = path.relative_to(self.tmp)
            if ".git" in relative.parts:
                continue
            name = str(relative)
            if path.is_symlink():
                result[name] = ("link", os.readlink(str(path)))
            elif path.is_dir():
                result[name] = ("directory",)
            else:
                result[name] = ("file", path.read_bytes())
        return result

    def test_coordinator_state_and_attempt_worktrees_unchanged(self):
        # Cover both existing state and absence: validation must not initialize it.
        worktrees = self.git("worktree", "list", "--porcelain")
        for seed in (self.seed, dict(self.seed, ref="refs/heads/main")):
            for state_exists in (True, False):
                with self.subTest(seed=seed, state_exists=state_exists):
                    for root in (self.state, self.tmp / ".swarm"):
                        if state_exists:
                            root.mkdir(parents=True, exist_ok=True)
                            (root / "state.json").write_bytes(b'{"units":{}}\n')
                            (root / "events.jsonl").write_bytes(b'untouched\x00\xff\n')
                        else:
                            shutil.rmtree(root, ignore_errors=True)
                    units = [self.unit(seed=seed)]
                    self.plan.write_text(json.dumps({"units": units}))
                    before = self.snapshot()
                    result = self.validate(units)
                    self.assertEqual(result.returncode, 0 if seed == self.seed else 1,
                                     result.stdout + result.stderr)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.git("worktree", "list", "--porcelain"),
                                     worktrees)


if __name__ == "__main__":
    unittest.main()
