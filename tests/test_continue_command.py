"""Exercise continuation seed authority and atomic plan publication via the CLI."""
import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from tests import test_validate_seeds as seed_fixture
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "skills/hanig-swarm/scripts/swarm.py"


class ContinueCommandTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                          if not k.startswith("GIT_")}, clear=True):
            seed_fixture.ValidateSeedsTests.setUp(self)
        self.tmp = self.tmp.resolve()
        self.repo, self.origin = self.repo.resolve(), self.origin.resolve()
        self.git("remote", "set-url", "origin", str(self.origin))
        self.root = self.base
        self.base = self.commit("recorded base")
        self.git("checkout", "-qb", "swarm-a1")
        self.head = self.commit("judged head")
        self.tip = self.commit("later branch tip")
        self.git("push", "-q", "origin", "swarm-a1")
        self.git("checkout", "-qb", "override", self.base)
        self.override = self.commit("explicit override")
        self.git("push", "-q", "origin", "override")
        self.git("checkout", "-qb", "unrelated", self.root)
        self.unrelated = self.commit("does not descend from recorded base")
        self.git("push", "-q", "origin", "unrelated")
        self.git("checkout", "-q", "main")
        self.target = self.commit("advanced target")
        self.git("push", "-q", "origin", "main")
        self.unit = {"id": "old", "kind": "code", "repo": str(self.repo),
                     "target_branch": "main", "provider": "codex",
                     "mode": "full-access", "prompt": "old prompt",
                     "scope": ["tests/*"], "inputs": [], "outputs": ["evidence.md"],
                     "max_attempts": 1, "tracker": "ARC-2189"}
        self.plan = {"name": "continuations", "units": [self.unit]}
        self.plan_path = self.tmp / "plan.json"
        self.attempt = self.tmp / "attempts/a1"
        self.attempt.mkdir(parents=True)
        self.intent = {"schema_version": 5, "unit_id": "old", "attempt_id": "a1",
                       "repo": str(self.repo), "base_commit": self.base,
                       "base_tree": self.git("rev-parse", self.base + "^{tree}"),
                       "repository_remote": str(self.origin),
                       "repository_remote_raw": str(self.origin),
                       "branch": "swarm-a1", "judgment_ref": "refs/heads/swarm-a1",
                       "target_branch": "main", "worktree_slug": "a1",
                       "launch_host": "fixture", "captured_at": "fixture"}
        self.us = {"attempt_dir": str(self.attempt),
                   "attempt_launch_intents": {"a1": self.intent},
                   "attempt_produced_heads": {"a1": self.head}}
        self.state = {"units": {"old": self.us}}
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.prompt = self.tmp / "prompt.txt"
        self.prompt.write_bytes("New prompt\r\n\n  caf\u00e9\t\r\n".encode("utf-8"))
        self.save()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              check=True, capture_output=True, text=True,
                              timeout=30).stdout.strip()

    def commit(self, message):
        (self.repo / "file.txt").write_text(message + "\n")
        self.git("add", "file.txt")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def save(self):
        self.plan_path.write_text(json.dumps(self.plan, indent=4) + "\n")
        (self.state_dir / "swarm-state.json").write_text(json.dumps(self.state))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SWARM), *args], cwd=str(self.tmp),
                              env=self.env, capture_output=True, text=True, timeout=60)

    def continuation(self, *args):
        return self.run_cli("continue", str(self.plan_path), "--state-dir",
                            str(self.state_dir), "--unit", "old", "--new-id", "new",
                            "--prompt-file", str(self.prompt), *args)

    def result_unit(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def snapshot(self):
        # The shared seed check may write fetch metadata, as validate does.
        # Include checkout/index/ref identities alongside all non-Git bytes.
        return (seed_fixture.ValidateSeedsTests.snapshot(self),
                self.git("rev-parse", "HEAD"),
                self.git("ls-files", "--stage"), self.git("show-ref"),
                self.git("worktree", "list", "--porcelain"))

    def test_preview_matches_hand_computation_without_publishing(self):
        before = self.snapshot()
        unit = self.result_unit(self.continuation())
        expected_base = self.git("merge-base", "origin/main", self.head)
        self.git("merge-base", "--is-ancestor", self.base, self.head)
        self.git("merge-base", "--is-ancestor", expected_base, self.target)
        self.assertEqual(unit["seed"], {"ref": "refs/heads/swarm-a1",
                         "head": self.head, "base": expected_base,
                         "evidence": str(self.attempt / "evidence.md")})
        self.assertEqual(unit["prompt"].encode("utf-8"), self.prompt.read_bytes())
        expected = dict(self.unit, id="new", seed=unit["seed"], prompt=unit["prompt"],
                        description="Continuation of old",
                        author_history=[{"unit": "old", "attempt": "a1", "identity": None}])
        self.assertEqual(unit, expected)
        self.assertEqual(self.snapshot(), before)

    def test_multihop_carries_captured_history_and_unknowns_without_mirrors(self):
        sys.path.insert(0, str(SWARM.parent))
        import swarm_continue as SC
        import swarm_code_launch as SCL
        ancestor = {"unit": "ancestor", "attempt": "z0", "identity": "p/ancestor"}
        # The plan and latest intent carry different lineage: reading just
        # either one must lose a real record. A's legacy a1 is unknown.
        self.unit["author_history"] = [ancestor]
        captured_only = {"unit": "older", "attempt": "z1", "identity": None}
        self.us["attempt_launch_intents"]["a2"] = dict(
            self.intent, schema_version=6, attempt_id="a2", provider="p/second",
            author_history=[captured_only])
        args = SimpleNamespace(unit="old", new_id="middle", state_dir=str(self.state_dir),
                               head_ref=None, prompt_file=str(self.prompt))
        state_before = copy.deepcopy(self.state)
        middle = SC._continuation(self.plan, self.state, args)
        expected = [ancestor, {"unit": "old", "attempt": "a1", "identity": None},
                    captured_only, {"unit": "old", "attempt": "a2", "identity": "p/second"}]
        self.assertEqual(middle["author_history"], expected)
        self.assertEqual(self.state, state_before)
        self.plan["units"].append(middle)
        middle.update(provider="p/third")
        # Freeze B's history via the real capture path. Its seed keeps the
        # old head; the cached source here is the coordinator's base observation.
        source = dict(self.intent, target_commit=self.base)
        error, anchor = SCL._capture_code_launch(
            self.tmp / "b1", middle, dispatch_source=source)
        self.assertIsNone(error)
        self.assertEqual(anchor["intent"]["author_history"], expected)
        self.git("push", "-q", "origin", self.head + ":refs/heads/swarm-b1")
        self.state["units"]["middle"] = {
            "attempt_dir": str(self.tmp / "b1"),
            "attempt_launch_intents": {"b1": anchor["intent"]},
            "attempt_produced_heads": {"b1": self.head}}
        # Routing edits and removal of B's plan history cannot erase the
        # captured history or change either predecessor's frozen identity.
        middle.update(provider="changed/provider", model="changed-model", author_history=[])
        self.unit.update(provider="changed/old", model="changed-old-model")
        args.unit, args.new_id = "middle", "last"
        state_before = copy.deepcopy(self.state)
        last = SC._continuation(self.plan, self.state, args)
        self.assertEqual(last["author_history"], expected + [
            {"unit": "middle", "attempt": "b1", "identity": "p/third"}])
        self.assertEqual(self.state, state_before)
        for us in self.state["units"].values():
            self.assertFalse([key for key in us if "author" in key])

    def test_write_appends_exactly_one_valid_unit(self):
        preview = self.result_unit(self.continuation())
        self.plan_path.chmod(0o640)
        written = self.result_unit(self.continuation("--write"))
        self.assertEqual(written, preview)
        self.assertEqual(json.loads(self.plan_path.read_text()),
                         dict(self.plan, units=[self.unit, preview]))
        self.assertEqual(self.plan_path.stat().st_mode & 0o777, 0o640)
        result = self.run_cli("validate", str(self.plan_path), "--check-seeds")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_head_precedence(self):
        unit = self.result_unit(self.continuation("--head-ref", "refs/heads/override"))
        self.assertEqual(unit["seed"]["head"], self.override)
        self.assertEqual(unit["seed"]["ref"], "refs/heads/override")
        self.assertEqual(self.result_unit(self.continuation())["seed"]["head"], self.head)
        self.us["attempt_produced_heads"] = {"older-attempt": self.override}
        self.save()
        self.assertEqual(self.result_unit(self.continuation())["seed"]["head"], self.tip)

    def assert_refusal(self, message, *args):
        before = self.snapshot()
        result = self.continuation("--write", *args)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_id_refused(self):
        self.plan["units"].append(dict(self.unit, id="new"))
        self.save()
        self.assert_refusal("already exists")

    def test_old_missing_refused(self):
        self.plan["units"] = []
        self.save()
        self.assert_refusal("OLD unit")

    def test_no_recorded_attempt_refused(self):
        del self.us["attempt_dir"]
        self.save()
        self.assert_refusal("no recorded attempt")

    def test_non_descendant_refused(self):
        # A merge-base exists, but the recorded launch base is not an ancestor.
        self.assert_refusal("recorded base/head ancestry", "--head-ref", "refs/heads/unrelated")

    def test_unresolved_ref_refused_without_fallback(self):
        self.assert_refusal("seed.ref", "--head-ref", "")
        self.assert_refusal("does not resolve", "--head-ref", "refs/heads/missing")
        self.us["attempt_produced_heads"] = {}
        self.git("push", "-q", "origin", ":refs/heads/swarm-a1")
        self.save()
        self.assert_refusal("does not resolve")

    def test_evidence_head_has_no_effect(self):
        (self.attempt / "evidence.md").write_text(json.dumps({"head": self.tip}))
        (self.attempt / "receipt.json").write_text(json.dumps({"head": self.override}))
        before = self.snapshot()
        self.assertEqual(self.result_unit(self.continuation())["seed"]["head"], self.head)
        self.assertEqual(self.snapshot(), before)

    def test_remote_only_objects_use_shared_fetch_without_publishing(self):
        fresh = self.tmp / "fresh"
        self.git("clone", "-q", "--no-local", "--single-branch", "--branch", "main",
                 str(self.origin), str(fresh))
        self.repo = fresh
        self.unit["repo"] = self.intent["repo"] = str(fresh)
        self.save()
        before = self.snapshot()
        absent = subprocess.run(["git", "-C", str(fresh), "cat-file", "-e", self.head],
                                env=self.env, capture_output=True)
        self.assertNotEqual(absent.returncode, 0)
        self.assertEqual(self.result_unit(self.continuation())["seed"]["head"], self.head)
        self.assertEqual(self.git("cat-file", "-t", self.head), "commit")
        self.assertEqual(self.git("for-each-ref", "refs/hanig-swarm-seeds/"), "")
        self.assertEqual(self.snapshot(), before)

    def test_validation_failure_does_not_publish(self):
        self.unit["inputs"] = "not a list"
        self.save()
        self.assert_refusal("inputs")


from tests.d5_test_support import inactive_class
inactive_class(ContinueCommandTests)


if __name__ == "__main__":
    unittest.main()
