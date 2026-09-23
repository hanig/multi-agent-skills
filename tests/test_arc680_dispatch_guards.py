#!/usr/bin/env python3
"""ARC-680 dispatch base and end-to-end canary guards."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("arc680_swarm", SCRIPTS / "swarm.py")
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="ARC-680",
               GIT_AUTHOR_EMAIL="arc680@example.invalid",
               GIT_COMMITTER_NAME="ARC-680",
               GIT_COMMITTER_EMAIL="arc680@example.invalid")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env=GIT_ENV).stdout.strip()


class DispatchBaseGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.remote = self.tmp / "origin.git"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       env=GIT_ENV)
        (self.repo / "tracked.txt").write_text("base\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "branch", "-M", "main")
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)],
                       check=True, env=GIT_ENV)
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-qu", "origin", "main")

        self.fake_bin = self.tmp / "bin"
        self.fake_bin.mkdir()
        paseo = self.fake_bin / "paseo"
        paseo.write_text("#!/bin/sh\nexit 99\n")
        paseo.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.fake_bin) + os.pathsep + old_path
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)

    def commit(self, name):
        (self.repo / "tracked.txt").write_text(name + "\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", name)
        return git(self.repo, "rev-parse", "HEAD")

    def run_dispatch(self, suffix, target_branch="main"):
        plan = self.tmp / ("plan-" + suffix + ".json")
        plan.write_text(json.dumps({
            "name": "arc-680",
            "units": [{
                "id": "code", "kind": "code", "repo": str(self.repo),
                "target_branch": target_branch, "mode": "full-access",
                "prompt": "work", "outputs": ["evidence.md"],
            }],
        }))
        args = Namespace(
            plan=str(plan), state_dir=str(self.tmp / ("state-" + suffix)),
            root=str(self.tmp / ("runs-" + suffix)), dry_run=True,
            max_new_dispatches=None, accept_plan_change=False)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            rc = S.cmd_run(args)
        return rc, stream.getvalue()

    def test_head_behind_origin_names_both_commits_refuses_and_recovers(self):
        local_head = git(self.repo, "rev-parse", "HEAD")
        remote_head = self.commit("remote-ahead")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "reset", "--hard", local_head)

        rc, output = self.run_dispatch("behind")

        self.assertNotEqual(rc, 0, output)
        self.assertIn(local_head, output)
        self.assertIn(remote_head, output)
        self.assertIn("behind origin/main", output)
        self.assertNotIn("submitted", output)

        git(self.repo, "reset", "--hard", remote_head)
        rc, output = self.run_dispatch("current")
        self.assertEqual(rc, 0, output)
        self.assertIn(remote_head, output)
        self.assertIn("from branch 'main'", output)

    def test_ahead_topic_branch_refuses_even_though_it_is_not_behind(self):
        main_head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-qb", "unrelated-topic")
        topic_head = self.commit("topic-ahead")
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "merge-base", "--is-ancestor",
                 main_head, topic_head], env=GIT_ENV).returncode,
            0, "the fixture must be ahead, so a behind-only guard misses it")

        rc, output = self.run_dispatch("wrong-branch")

        self.assertNotEqual(rc, 0, output)
        self.assertIn("unrelated-topic", output)
        self.assertIn("main", output)
        self.assertIn(topic_head, output)
        self.assertIn(main_head, output)
        self.assertNotIn("submitted", output)

    def test_diverged_target_branch_is_neither_current_nor_ahead(self):
        common = git(self.repo, "rev-parse", "HEAD")
        remote_head = self.commit("remote-side")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "reset", "--hard", common)
        local_head = self.commit("local-side")

        rc, output = self.run_dispatch("diverged")

        self.assertNotEqual(rc, 0, output)
        self.assertIn("diverged", output)
        self.assertIn(local_head, output)
        self.assertIn(remote_head, output)
        self.assertNotIn("submitted", output)

    def test_detached_head_at_target_refuses_as_not_on_target_branch(self):
        target = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "--detach", target)

        rc, output = self.run_dispatch("detached")

        self.assertNotEqual(rc, 0, output)
        self.assertIn("(detached HEAD)", output)
        self.assertIn("target branch 'main'", output)
        self.assertNotIn("submitted", output)

    def test_same_named_tag_does_not_change_attached_branch_identity(self):
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "tag", "main")

        rc, output = self.run_dispatch("same-named-tag")

        self.assertEqual(rc, 0, output)
        self.assertIn(head, output)
        self.assertIn("from branch 'main'", output)

    def test_equals_in_push_destination_does_not_corrupt_route(self):
        push_remote = self.tmp / "push=origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(push_remote)],
                       check=True, env=GIT_ENV)
        git(self.repo, "push", "-q", str(push_remote),
            "HEAD:refs/heads/main")
        git(self.repo, "config", f"url.{push_remote}.pushInsteadOf",
            str(self.remote))

        rc, output = self.run_dispatch("equals-push-url")

        self.assertEqual(rc, 0, output)
        self.assertIn("from branch 'main'", output)

    def test_exact_target_wins_over_ls_remote_suffix_match(self):
        git(self.repo, "branch", "-m", "zzz")
        local_head = git(self.repo, "rev-parse", "HEAD")
        target_head = self.commit("exact-target-newer")
        git(self.repo, "push", "-q", "origin", "zzz")
        git(self.repo, "push", "-q", "origin",
            local_head + ":refs/heads/refs/heads/zzz")
        git(self.repo, "reset", "--hard", local_head)

        rc, output = self.run_dispatch("suffix-match", target_branch="zzz")

        self.assertNotEqual(rc, 0, output)
        self.assertIn(local_head, output)
        self.assertIn(target_head, output)
        self.assertIn("behind origin/zzz", output)
        self.assertNotIn("submitted", output)

    def test_equal_length_fetch_rewrite_cannot_override_push_route(self):
        read_remote = self.tmp / "read.git"
        write_remote = self.tmp / "write.git"
        for remote in (read_remote, write_remote):
            subprocess.run(["git", "init", "-q", "--bare", str(remote)],
                           check=True, env=GIT_ENV)
        push_head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", str(write_remote),
            "HEAD:refs/heads/main")
        read_head = self.commit("read-only-newer")
        git(self.repo, "push", "-q", str(read_remote),
            "HEAD:refs/heads/main")
        git(self.repo, "reset", "--hard", push_head)
        git(self.repo, "config", "remote.origin.url", "route:repo")
        git(self.repo, "config", f"url.{read_remote}.insteadOf", "route:repo")
        git(self.repo, "config", f"url.{write_remote}.pushInsteadOf",
            "route:repo")

        rc, output = self.run_dispatch("equal-rewrite")

        self.assertEqual(rc, 0, output)
        self.assertIn(push_head, output)
        self.assertNotIn(read_head, output)
        self.assertIn("from branch 'main'", output)

    def test_identity_push_rewrite_still_bypasses_fetch_rewrite(self):
        read_remote = self.tmp / "identity-read.git"
        write_remote = self.tmp / "identity-write.git"
        for remote in (read_remote, write_remote):
            subprocess.run(["git", "init", "-q", "--bare", str(remote)],
                           check=True, env=GIT_ENV)
        push_head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", str(write_remote),
            "HEAD:refs/heads/main")
        read_head = self.commit("identity-read-newer")
        git(self.repo, "push", "-q", str(read_remote),
            "HEAD:refs/heads/main")
        git(self.repo, "reset", "--hard", push_head)
        git(self.repo, "config", "remote.origin.url", str(write_remote))
        git(self.repo, "config", f"url.{read_remote}.insteadOf",
            str(write_remote))
        git(self.repo, "config", f"url.{write_remote}.pushInsteadOf",
            str(write_remote))

        rc, output = self.run_dispatch("identity-rewrite")

        self.assertEqual(rc, 0, output)
        self.assertIn(push_head, output)
        self.assertNotIn(read_head, output)

    def test_shallow_checkout_deepens_without_same_named_fetch_branch(self):
        ancestor = git(self.repo, "rev-parse", "HEAD")
        descendant = self.commit("shallow-tip")
        git(self.repo, "push", "-q", "origin", "main")
        write_remote = self.tmp / "shallow-write.git"
        subprocess.run(["git", "init", "-q", "--bare", str(write_remote)],
                       check=True, env=GIT_ENV)
        git(self.repo, "push", "-q", str(write_remote),
            ancestor + ":refs/heads/main")
        shallow = self.tmp / "shallow"
        subprocess.run([
            "git", "clone", "-q", "--depth=1", "--branch", "main",
            "file://" + str(self.remote), str(shallow),
        ], check=True, env=GIT_ENV)
        # The fetch repository carries the checkout's history, but not under
        # the push target's branch name. The ancestry fact is still available;
        # requiring an adjacent naming fact would make an honest run fail.
        git(self.repo, "push", "-q", "origin", "main:refs/heads/source")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/source")
        git(self.repo, "push", "-q", "origin", ":refs/heads/main")
        git(shallow, "config", "--replace-all", "remote.origin.fetch",
            "+refs/heads/source:refs/remotes/origin/source")
        git(shallow, "config", "remote.origin.pushurl", str(write_remote))
        self.repo = shallow

        rc, output = self.run_dispatch("shallow-ahead")

        self.assertEqual(rc, 0, output)
        self.assertIn(descendant, output)
        self.assertIn(ancestor, output)
        self.assertFalse((shallow / ".git" / "shallow").exists())

    def test_shallow_checkout_can_deepen_from_anchored_push_route(self):
        ancestor = git(self.repo, "rev-parse", "HEAD")
        descendant = self.commit("push-route-shallow-tip")
        git(self.repo, "push", "-q", "origin", "main")
        push_remote = self.tmp / "push-route.git"
        subprocess.run(["git", "init", "-q", "--bare", str(push_remote)],
                       check=True, env=GIT_ENV)
        git(self.repo, "push", "-q", str(push_remote),
            ancestor + ":refs/heads/main")
        git(self.repo, "push", "-q", str(push_remote),
            descendant + ":refs/heads/source")
        git(push_remote, "symbolic-ref", "HEAD", "refs/heads/source")
        shallow = self.tmp / "push-route-shallow"
        subprocess.run([
            "git", "clone", "-q", "--depth=1", "--branch", "main",
            "file://" + str(self.remote), str(shallow),
        ], check=True, env=GIT_ENV)
        unavailable_fetch = self.tmp / "unavailable-fetch.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(unavailable_fetch)],
            check=True, env=GIT_ENV)
        git(shallow, "remote", "set-url", "origin", str(unavailable_fetch))
        git(shallow, "remote", "set-url", "--push", "origin",
            str(push_remote))
        self.repo = shallow

        rc, output = self.run_dispatch("push-route-shallow-ahead")

        self.assertEqual(rc, 0, output)
        self.assertIn(descendant, output)
        self.assertIn(ancestor, output)
        self.assertFalse((shallow / ".git" / "shallow").exists())


class DispatchCanaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def unit(uid):
        return {"id": uid, "kind": "pipeline", "command": "true",
                "runtime": "none", "outputs": [uid + ".txt"]}

    def plan(self):
        return {
            "name": "qualified-fanout",
            "dispatch_canary": {"unit": "z-canary", "width": 1},
            "units": [self.unit("a-rest"), self.unit("b-rest"),
                      self.unit("z-canary")],
        }

    def advance(self, plan, state, dry_run=True, accept_plan_change=False):
        counter = {"n": 0}

        def allocate(_plan, unit, root):
            counter["n"] += 1
            path = Path(root) / unit["id"] / ("attempt-%d" % counter["n"])
            path.mkdir(parents=True)
            return str(path), None

        with mock.patch.object(S, "_allocate", side_effect=allocate), \
                mock.patch.object(S, "_submit",
                                  side_effect=lambda u, d, dry, *rest:
                                  ("dry-" + u["id"], None)), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(S, "_check",
                                  return_value=(S.RUNNING, "", "", "")):
            return S.advance(
                plan, state, str(self.tmp / "state"),
                str(self.tmp / "runs"), dry_run,
                accept_plan_change=accept_plan_change)

    def test_canary_reaches_done_before_fanout(self):
        plan = self.plan()
        S.validate_plan(plan)
        state = {"units": {}}

        report, dispatched, _halted = self.advance(plan, state)

        self.assertEqual(dispatched, 1, report)
        self.assertEqual(state["units"]["z-canary"]["state"], "SUBMITTED")
        self.assertNotEqual(state["units"]["a-rest"]["state"], "SUBMITTED")
        self.assertIn("canary", " ".join(report).lower())

        state["units"]["z-canary"]["state"] = "READY_FOR_PR"
        report, dispatched, _halted = self.advance(plan, state)
        self.assertEqual(dispatched, 0, report)

        state["units"]["z-canary"]["state"] = "DONE"
        report, dispatched, _halted = self.advance(plan, state)
        self.assertEqual(dispatched, 2, report)

    def test_new_canary_cannot_bypass_full_width_after_plan_change(self):
        plan = self.plan()
        old_plan = dict(plan)
        old_plan.pop("dispatch_canary")
        running = self.tmp / "existing-attempt"
        running.mkdir()
        state = {
            "plan_digest": S.plan_digest(old_plan),
            "units": {
                "a-rest": {
                    "attempt_dir": str(running),
                    "attempts": [str(running)],
                    "job_id": "existing-job",
                    "state": "RUNNING",
                    "gpu_hours": 0.0,
                },
            },
        }

        report, dispatched, halted = self.advance(
            plan, state, dry_run=False, accept_plan_change=True)

        self.assertEqual(dispatched, 0, report)
        self.assertIn("plan change RATIFIED", " ".join(report))
        self.assertIn("past declared width 1", " ".join(report))
        self.assertIn("canary", halted)
        self.assertFalse(state["units"]["z-canary"]["attempts"])

        state["units"]["a-rest"]["state"] = "DONE"
        report, dispatched, halted = self.advance(plan, state, dry_run=False)

        self.assertEqual(dispatched, 1, report)
        self.assertIn("has not reached DONE", halted)
        self.assertEqual(state["units"]["z-canary"]["state"], "SUBMITTED")

    def test_canary_must_exist_be_rooted_and_have_a_positive_width(self):
        plan = self.plan()
        plan["dispatch_canary"]["unit"] = "absent"
        with self.assertRaisesRegex(S.PlanError, "absent"):
            S.validate_plan(plan)

        plan = self.plan()
        plan["dispatch_canary"]["width"] = 0
        with self.assertRaisesRegex(S.PlanError, "width"):
            S.validate_plan(plan)

        plan = self.plan()
        plan["units"][-1]["needs"] = ["a-rest"]
        with self.assertRaisesRegex(S.PlanError, "no dependencies"):
            S.validate_plan(plan)

    def test_absent_canary_keeps_the_legacy_plan_digest(self):
        plan = {"units": [self.unit("only")], "budget": None, "root": None}
        units = sorted(plan["units"],
                       key=lambda d: json.dumps(d, sort_keys=True))
        legacy = json.dumps(
            {"units": units, "budget": None, "root": None},
            sort_keys=True, default=str)
        expected = hashlib.sha256(legacy.encode()).hexdigest()

        self.assertEqual(S.plan_digest(plan), expected)


if __name__ == "__main__":
    unittest.main()
