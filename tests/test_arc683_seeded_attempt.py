"""ARC-683: seed provenance reaches the worker without changing judgment."""
import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "hanig-swarm" / "scripts"))
import swarm as S
from tests.test_attempt_worktrees import ENV, FakePaseo, git, repo_at, paseo_resolvable


class SeedValidationTests(unittest.TestCase):
    def unit(self, seed):
        return {"id": "repair", "kind": "code", "repo": "/unused",
                "target_branch": "main", "mode": "full-access",
                "prompt": "Repair", "outputs": ["evidence.md"], "seed": seed}

    def seed(self, **changes):
        return dict({"ref": "refs/heads/previous", "base": "a" * 40,
                     "head": "b" * 40}, **changes)

    def test_wrong_seed_types_name_the_field(self):
        for value in ("branch", [], None, 1, True):
            with self.subTest(value=value), self.assertRaisesRegex(
                    S.PlanError, "seed.*JSON object"):
                S.validate_plan({"units": [self.unit(value)]})

    def test_invalid_commits_and_refs_name_the_subfield(self):
        cases = [(field, value) for field in ("base", "head")
                 for value in (None, 12, "a" * 39, "a" * 41, "g" * 40,
                               "a" * 63, "a" * 65, " " + "a" * 40,
                               "a" * 40 + "\n")]
        cases += [("ref", value) for value in (
            None, 1, "main", "refs/tags/v1", "refs/remotes/origin/a",
            "refs/heads/", "refs/heads/a..b", "refs/heads/a.lock",
            " refs/heads/a", "refs/heads/a\n", "refs/heads/a:b")]
        cases += [("evidence", value) for value in (None, 1, "", "a\0b")]
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    S.PlanError, "seed\\." + field):
                S.validate_plan({"units": [self.unit(self.seed(**{field: value}))]})

    def test_missing_fields_unknown_keys_and_noncode_seed_are_refused(self):
        for field in ("ref", "base", "head"):
            seed = self.seed()
            del seed[field]
            with self.subTest(field=field), self.assertRaisesRegex(
                    S.PlanError, "seed\\." + field):
                S.validate_plan({"units": [self.unit(seed)]})
        with self.assertRaisesRegex(S.PlanError, "seed"):
            S.validate_plan({"units": [self.unit(self.seed(hed="b" * 40))]})
        for kind in ("slurm", "pipeline"):
            unit = dict(self.unit(self.seed()), kind=kind,
                        runtime="none", command="true")
            with self.subTest(kind=kind), self.assertRaisesRegex(S.PlanError, "seed"):
                S.validate_plan({"units": [unit]})

    def test_valid_sha1_sha256_and_raw_values_survive_validation(self):
        with paseo_resolvable():
            for size in (40, 64):
                unit = self.unit(self.seed(base="A" * size, head="B" * size,
                                          ref="refs/heads/repair/\u00a0prior",
                                          evidence="../old/evidence with 'quotes'.md"))
                before = copy.deepcopy(unit)
                S.validate_plan({"units": [unit]})
                self.assertEqual(unit, before)

    def test_cli_validate_names_seed_before_dispatch_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps({"units": [self.unit("branch")]}))
            result = subprocess.run([sys.executable, str(S._HERE / "swarm.py"),
                                     "validate", str(path)], capture_output=True,
                                    text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("seed", result.stderr)


class SeedDispatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = repo_at(self.tmp / "repo")
        git(self.repo, "branch", "-M", "main")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.remote = self.tmp / "origin.git"
        git(self.repo, "init", "-q", "--bare", str(self.remote))
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-qb", "previous")
        (self.repo / "repair.txt").write_text("carried forward\n")
        git(self.repo, "add", "repair.txt")
        git(self.repo, "commit", "-qm", "previous implementation")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/previous")
        git(self.repo, "checkout", "-q", "main")
        self.seed = {"ref": "refs/heads/previous", "base": self.base,
                     "head": self.head, "evidence": "../old/evidence.md"}
        self.unit = {"id": "repair", "kind": "code", "repo": str(self.repo),
                     "target_branch": "main", "mode": "full-access",
                     "prompt": "Repair", "outputs": ["evidence.md"],
                     "seed": self.seed}
        self.attempt = self.tmp / "runs" / "repair" / "attempt-1"
        self.attempt.mkdir(parents=True)
        self.state_dir = str(self.tmp / "state")
        self.state = {"units": {}}
        self.fake = FakePaseo(self, self.tmp / "managed", S.U.run)
        patcher = mock.patch.object(S.U, "run", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(S, "_installed_skill_snapshot",
                                    return_value={"skills": [], "errors": []})
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit(self):
        return S._submit(self.unit, str(self.attempt), False,
                         self.state, self.state_dir)

    def test_000_exact_seed_is_durable_before_worker_receives_prompt(self):
        observed = []
        fake = self.fake

        def inspect_launch(argv, **kwargs):
            if argv[:2] == ["paseo", "run"]:
                observed.append(S.load_state(self.state_dir)["units"]["repair"][
                    "attempt_launch_intents"][self.attempt.name])
            return fake(argv, **kwargs)

        with mock.patch.object(S.U, "run", inspect_launch):
            job, error = self.submit()
        self.assertIsNone(error)
        self.assertTrue(job)
        self.assertEqual(observed[0]["seed"], self.seed)
        durable = S.load_state(self.state_dir)["units"]["repair"]
        self.assertEqual(durable["attempt_launch_intents"][self.attempt.name]["seed"],
                         self.seed)
        prompt = self.fake.launches[0][-1]
        for text in ("CARRY FORWARD", self.seed["ref"],
                     self.base + ".." + self.head, self.seed["evidence"],
                     "cherry-pick -x", "cherry-pick --skip", "whole-file checkout"):
            self.assertIn(text, prompt)
        facts = durable["attempt_launch_facts"][self.attempt.name]
        self.assertNotIn("seed", facts)
        self.assertEqual(facts["base_commit"], self.base)
        self.assertEqual(git(facts["execution_workspace"], "rev-parse", "HEAD"),
                         self.base)
        self.assertFalse((Path(facts["execution_workspace"]) / "repair.txt").exists())
        self.seed["evidence"] = "changed-after-capture"
        self.assertEqual(observed[0]["seed"]["evidence"], "../old/evidence.md")

    def test_unreachable_head_refuses_before_worktree_or_agent(self):
        self.unit["seed"] = dict(self.seed, ref="refs/heads/main")
        with mock.patch.object(S, "_create_code_worktree") as create:
            job, error = self.submit()
        self.assertIsNone(job)
        self.assertIn("seed", error)
        self.assertIn("reachable", error)
        self.assertFalse(self.fake.launches)
        create.assert_not_called()
        self.assertNotIn("attempt_launch_intents", self.state["units"].get("repair", {}))

    def test_unrelated_base_and_noncommit_objects_are_refused(self):
        unrelated = git(self.repo, "commit-tree", "HEAD^{tree}", "-m", "unrelated")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        for field, value in (("base", unrelated), ("base", tree), ("head", tree),
                             ("head", "0" * 40)):
            with self.subTest(field=field):
                self.unit["seed"] = dict(self.seed, **{field: value})
                job, error = self.submit()
                self.assertIsNone(job)
                self.assertIn("seed", error)
        self.assertFalse(self.fake.launches)

    def test_remote_only_commits_and_descendant_ref_are_admitted(self):
        later = git(self.repo, "commit-tree", self.head + "^{tree}",
                    "-p", self.head, "-m", "later")
        git(self.repo, "push", "-q", "origin", later + ":refs/heads/previous")
        fresh = self.tmp / "fresh"
        git(self.repo, "clone", "-q", "--single-branch", "--branch", "main",
            str(self.remote), str(fresh))
        self.unit["repo"] = str(fresh)
        before = git(fresh, "show-ref")
        job, error = self.submit()
        self.assertIsNone(error)
        self.assertTrue(job)
        self.assertEqual(git(fresh, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(fresh, "status", "--porcelain"), "")
        self.assertNotIn("refs/hanig-swarm-seeds/", git(fresh, "show-ref"))
        self.assertIn("refs/heads/main", before)

    def test_prompt_commands_replay_from_push_route_and_skip_empty_commits(self):
        # The fetch URL deliberately cannot serve the seed. Admission and
        # the actual commands delivered to a worker must use the push route.
        git(self.repo, "remote", "set-url", "--push", "origin", str(self.remote))
        git(self.repo, "remote", "set-url", "origin", str(self.tmp / "absent.git"))
        empty_head = git(self.repo, "commit-tree", self.head + "^{tree}",
                         "-p", self.head, "-m", "empty seed commit")
        git(self.repo, "push", "-q", "origin", empty_head + ":refs/heads/previous")
        self.seed["head"] = empty_head
        self.seed["ref"] = "refs/heads/previous"
        job, error = self.submit()
        self.assertIsNone(error)
        self.assertTrue(job)
        argv = self.fake.launches[0]
        workspace = argv[argv.index("--cwd") + 1]
        carry = argv[-1].split("CARRY FORWARD", 1)[1]
        commands = carry.split("```sh\n", 1)[1].split("\n```", 1)[0].splitlines()
        fetched = subprocess.run(shlex.split(commands[0]), cwd=workspace,
                                 env=ENV, capture_output=True, text=True)
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        picked = subprocess.run(shlex.split(commands[1]), cwd=workspace,
                                env=ENV, capture_output=True, text=True)
        self.assertNotEqual(picked.returncode, 0)
        self.assertIn("empty", picked.stderr)
        git(workspace, "cherry-pick", "--skip")
        message = git(workspace, "log", "-1", "--format=%B")
        self.assertIn("cherry picked from commit " + self.head, message)
        self.assertEqual(git(workspace, "status", "--porcelain"), "")
        self.assertEqual((Path(workspace) / "repair.txt").read_text(), "carried forward\n")

    def test_seed_does_not_replace_produced_head_or_exempt_seeded_scope(self):
        self.unit["scope"] = ["tracked.txt"]
        job, error = self.submit()
        self.assertIsNone(error)
        self.assertTrue(job)
        us = self.state["units"]["repair"]
        facts = us["attempt_launch_facts"][self.attempt.name]
        workspace = facts["execution_workspace"]
        git(workspace, "cherry-pick", "-x", self.base + ".." + self.head)
        produced = git(workspace, "rev-parse", "HEAD")
        self.assertNotEqual(produced, self.head)
        git(workspace, "push", "-q", "origin", "HEAD:" + facts["judgment_ref"])
        ok, head, why = S.W.judge_detail(S.U.run, str(self.attempt), self.unit, facts)
        self.assertTrue(ok, why)
        self.assertEqual(head, produced)
        us["attempt_dir"] = str(self.attempt)
        us["attempt_produced_heads"] = {self.attempt.name: head}
        S.save_state(self.state_dir, self.state)
        plan_path = self.tmp / "plan.json"
        plan_path.write_text(json.dumps({"units": [self.unit]}))
        result = subprocess.run([sys.executable, str(S._HERE / "swarm.py"),
                                 "scope-check", str(plan_path), "--state-dir",
                                 self.state_dir, "--unit", "repair", "--json"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, S.EXIT_SCOPE_OUTSIDE, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["base"], self.base)
        self.assertEqual(report["head"], produced)
        self.assertEqual(report["out_of_scope"], ["repair.txt"])

    def test_raw_unicode_ref_and_uppercase_ids_are_recorded_without_normalizing(self):
        ref = "refs/heads/repair/\u00a0prior"
        git(self.repo, "push", "-q", "origin", self.head + ":" + ref)
        self.unit["seed"] = dict(self.seed, ref=ref, base=self.base.upper(),
                                 head=self.head.upper())
        job, error = self.submit()
        self.assertIsNone(error)
        self.assertTrue(job)
        intent = self.state["units"]["repair"]["attempt_launch_intents"][self.attempt.name]
        self.assertEqual(intent["seed"], self.unit["seed"])

    def test_redispatch_of_recorded_seed_succeeds_without_recapturing_it(self):
        error, anchor = S._capture_code_launch(str(self.attempt), self.unit)
        self.assertIsNone(error)
        original = copy.deepcopy(anchor["intent"])
        self.state = {"units": {"repair": {"attempt_launch_intents": {
            self.attempt.name: anchor["intent"]}}}}
        S.save_state(self.state_dir, self.state)
        job, error = self.submit()
        self.assertIsNone(error)
        self.assertTrue(job)
        self.assertEqual(S.load_state(self.state_dir)["units"]["repair"][
            "attempt_launch_intents"][self.attempt.name], original)

    def test_redispatch_rechecks_remote_and_preserves_intent_bytes(self):
        error, anchor = S._capture_code_launch(str(self.attempt), self.unit)
        self.assertIsNone(error)
        self.state = {"units": {"repair": {"attempt_launch_intents": {
            self.attempt.name: anchor["intent"]}}}}
        S.save_state(self.state_dir, self.state)
        before = Path(self.state_dir, S.STATE_FILE).read_bytes()
        git(self.repo, "push", "-q", "origin", ":refs/heads/previous")
        job, error = self.submit()
        self.assertIsNone(job)
        self.assertIn("seed", error)
        self.assertFalse(self.fake.launches)
        self.assertEqual(Path(self.state_dir, S.STATE_FILE).read_bytes(), before)

    def test_redispatch_never_backfills_or_replaces_seed_from_changed_plan(self):
        for prior_seed in (None, dict(self.seed, evidence="old-evidence.md")):
            prior_unit = dict(self.unit)
            if prior_seed is None:
                del prior_unit["seed"]
            else:
                prior_unit["seed"] = prior_seed
            error, anchor = S._capture_code_launch(str(self.attempt), prior_unit)
            self.assertIsNone(error)
            self.state = {"units": {"repair": {"attempt_launch_intents": {
                self.attempt.name: anchor["intent"]}}}}
            before = copy.deepcopy(self.state)
            job, error = self.submit()
            self.assertIsNone(job)
            self.assertIn("seed", error)
            self.assertEqual(self.state, before)
        self.assertFalse(self.fake.launches)


class UnseededCompatibilityTests(unittest.TestCase):
    def test_base_intent_prompt_and_plan_golden_digests(self):
        # Captured by executing base e5a7e018 before implementation, with only
        # nondeterministic observations fixed. These are not source-text checks.
        unit = {"id": "code", "kind": "code", "repo": "/seed-compat/repo",
                "target_branch": "main", "prompt": "Repair the implementation.",
                "outputs": ["evidence.md"], "mode": "full-access"}
        source = {"repo": unit["repo"], "target_branch": "main",
                  "base_commit": "a" * 40, "target_commit": "a" * 40,
                  "base_tree": "b" * 40,
                  "repository_remote_raw": "https://example.invalid/repo.git",
                  "repository_remote": "https://example.invalid/repo.git"}
        with mock.patch.object(S, "_plan_workspace", return_value=(unit["repo"], None)), \
                mock.patch.object(S, "_stash_preflight", return_value=None), \
                mock.patch.object(S, "_git_push_destination", return_value=(2, "", "")), \
                mock.patch.object(S, "_git", return_value=(1, "", "")), \
                mock.patch.object(S, "_installed_skill_snapshot",
                                  return_value={"skills": [], "errors": []}), \
                mock.patch.object(S.os, "uname", return_value=SimpleNamespace(
                    nodename="fixture-host")), \
                mock.patch.object(S.time, "strftime", return_value="2026-09-25T00:00:00+0000"):
            error, anchor = S._capture_code_launch(
                "/seed-compat/runs/code/attempt-1", unit, source)
        self.assertIsNone(error)
        intent = anchor["intent"]
        self.assertNotIn("seed", intent)
        encoded = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),
                         "6d17a37532381f9046afbe0656c139340bbc79b6fb28a8ae736b96eb4af7fbf3")
        self.assertEqual(hashlib.sha256(S._dispatch_prompt(unit, intent).encode()).hexdigest(),
                         "8bf3481207446c8ddee6ce6d9058a97afd874962787dce91bd33f0a88ddac57b")
        self.assertEqual(S.plan_digest({"name": "seed-compat", "units": [unit]}),
                         "55ce911fb07620734d54da3b6ef9cde709df04d099bc7d7ffd66aaf2d24db93a")


if __name__ == "__main__":
    unittest.main()
