#!/usr/bin/env python3
"""ARC-680: transition-independent dispatch admission specification."""

import contextlib
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
SPEC = importlib.util.spec_from_file_location(
    "arc680_spec_swarm", SCRIPTS / "swarm.py")
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


class AdmissionInvariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def unit(uid, needs=None):
        return {"id": uid, "kind": "pipeline", "command": "true",
                "runtime": "none", "outputs": [uid + ".txt"],
                "needs": list(needs or [])}

    def plan(self, width):
        return {
            "name": "arc-680-invariant",
            "dispatch_canary": {"unit": "z-canary", "width": width},
            "units": [self.unit("a-independent"),
                      self.unit("b-dependent", ["z-canary"]),
                      self.unit("z-canary")],
        }

    def advance(self, plan, state, *, dry_run=True, check=S.RUNNING,
                allocation_error=False, preflight_refusal=False,
                reconcile=None, accept_plan_change=False, max_new=None):
        allocated = []
        submitted = []

        def allocate(_plan, unit, root):
            allocated.append(unit["id"])
            if allocation_error and unit["id"] == "z-canary":
                return None, "allocation exploded"
            path = Path(root) / unit["id"] / ("attempt-%d" % len(allocated))
            path.mkdir(parents=True, exist_ok=True)
            return str(path), None

        def submit(unit, _attempt, _dry, *_args, **_kwargs):
            submitted.append(unit["id"])
            if preflight_refusal and unit["id"] == "z-canary":
                refusal = S.PreflightRefusal("retryable preflight refusal")
                refusal.workspace = str(self.tmp)
                return None, refusal
            return "dry-" + unit["id"], None

        check_result = (check if isinstance(check, tuple) else
                        (check, "", "", ""))
        patches = [
            mock.patch.object(S, "_allocate", side_effect=allocate),
            mock.patch.object(S, "_submit", side_effect=submit),
            mock.patch.object(S, "_bind", return_value=None),
            mock.patch.object(S, "renew_lease", return_value=True),
            mock.patch.object(
                S, "_check", return_value=check_result),
        ]
        if reconcile is not None:
            patches.append(mock.patch.object(
                S, "reconcile_orphan", return_value=reconcile))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = S.advance(
                plan, state, str(self.tmp / "state"),
                str(self.tmp / "runs"), dry_run, max_new=max_new,
                accept_plan_change=accept_plan_change)
        return result, allocated, submitted

    def assert_only_canary_admitted(self, allocated, submitted):
        self.assertNotIn("a-independent", allocated)
        self.assertNotIn("b-dependent", allocated)
        self.assertNotIn("a-independent", submitted)
        self.assertNotIn("b-dependent", submitted)

    def test_authorization_is_width_independent_before_qualification(self):
        for width in (1, 2, 4):
            with self.subTest(width=width):
                result, allocated, submitted = self.advance(
                    self.plan(width), {"units": {}})
                report, dispatched, halted = result
                self.assertEqual(dispatched, 1, report)
                self.assertIsNone(halted, report)
                self.assertEqual(allocated, ["z-canary"])
                self.assertEqual(submitted, ["z-canary"])
                self.assert_only_canary_admitted(allocated, submitted)

    def test_every_admission_clearing_transition_preserves_the_invariant(self):
        transitions = ("already-failed", "fails-during-advance",
                       "preflight-refusal", "allocation-error",
                       "cleared-attempt")
        for width in (1, 2, 4):
            for transition in transitions:
                with self.subTest(width=width, transition=transition):
                    plan = self.plan(width)
                    kwargs = {}
                    if transition == "already-failed":
                        state = {"units": {"z-canary": {
                            "state": "FAILED", "attempt_dir": None,
                            "attempts": ["old"], "job_id": None,
                            "gpu_hours": 0.0}}}
                    elif transition == "fails-during-advance":
                        attempt = self.tmp / ("live-%s" % width)
                        attempt.mkdir(exist_ok=True)
                        state = {"units": {"z-canary": {
                            "state": "RUNNING", "attempt_dir": str(attempt),
                            "attempts": [str(attempt)], "job_id": "dry-old",
                            "gpu_hours": 0.0}}}
                        kwargs["check"] = S.FAILED
                    elif transition == "preflight-refusal":
                        state = {"units": {}}
                        kwargs["preflight_refusal"] = True
                    elif transition == "allocation-error":
                        state = {"units": {}}
                        kwargs["allocation_error"] = True
                    else:
                        attempt = self.tmp / ("cleared-%s" % width)
                        attempt.mkdir(exist_ok=True)
                        state = {"units": {"z-canary": {
                            "state": "ALLOCATED",
                            "attempt_dir": str(attempt),
                            "attempts": [str(attempt)], "job_id": None,
                            "allocated_at": 1.0, "gpu_hours": 0.0}}}
                        kwargs.update(dry_run=False,
                                      reconcile=(None, "ABSENT"))
                    result, allocated, submitted = self.advance(
                        plan, state, **kwargs)
                    report, _dispatched, halted = result
                    self.assert_only_canary_admitted(allocated, submitted)
                    if transition in ("already-failed",
                                      "fails-during-advance",
                                      "preflight-refusal",
                                      "allocation-error"):
                        self.assertIsNotNone(halted, report)
                    else:
                        self.assertIsNone(halted, report)

    def test_done_label_without_scoped_qualification_authorizes_nothing(self):
        plan = self.plan(4)
        state = {"units": {"z-canary": {
            "state": "DONE", "attempt_dir": None, "attempts": [],
            "job_id": None, "gpu_hours": 0.0}}}

        result, allocated, submitted = self.advance(plan, state)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNotNone(result[2], result[0])
        self.assert_only_canary_admitted(allocated, submitted)

    def test_running_state_without_attempt_refuses_before_any_dispatch(self):
        plan = self.plan(4)
        state = {"units": {"a-independent": {
            "state": "RUNNING", "attempt_dir": None,
            "attempts": ["lost-attempt"], "job_id": "job-still-unknown",
            "gpu_hours": 0.0}}}

        result, allocated, submitted = self.advance(plan, state)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNotNone(result[2], result[0])
        self.assertEqual(allocated, [])
        self.assertEqual(submitted, [])
        self.assertIn("no attempt identity", " ".join(result[0]))

    def test_matching_qualification_cannot_outlive_failed_canary_state(self):
        plan = self.plan(4)
        state = {"units": {"z-canary": {
            "state": "FAILED", "attempt_dir": None,
            "attempts": ["failed"], "job_id": None,
            "gpu_hours": 0.0}},
            "dispatch_canary_qualification": {
                "unit": "z-canary", "plan_digest": S.plan_digest(plan),
                "target_commit": None, "attempt": "old-success"}}

        result, allocated, submitted = self.advance(plan, state)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNotNone(result[2], result[0])
        self.assert_only_canary_admitted(allocated, submitted)

    def test_held_canary_is_not_re_admitted(self):
        plan = self.plan(2)
        state = {"units": {"z-canary": {
            "state": "HELD", "attempt_dir": None,
            "attempts": ["failed-canary"], "job_id": None,
            "gpu_hours": 0.0}}}

        result, allocated, submitted = self.advance(plan, state)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNotNone(result[2], result[0])
        self.assert_only_canary_admitted(allocated, submitted)

    def test_ratification_preserves_unscoped_completion_as_stale(self):
        old_plan = self.plan(2)
        old_plan.pop("dispatch_canary")
        plan = self.plan(2)
        attempt = self.tmp / "pre-canary-attempt"
        state = {"plan_digest": S.plan_digest(old_plan), "units": {
            "z-canary": {
                "state": "DONE", "attempt_dir": None,
                "attempts": [str(attempt)], "job_id": None,
                "gpu_hours": 0.0,
                "attempt_receipt_seals": {attempt.name: "a" * 64}}}}

        result, allocated, submitted = self.advance(
            plan, state, dry_run=False, accept_plan_change=True)

        self.assertEqual(result[1], 1, result[0])
        self.assertIsNone(result[2], result[0])
        self.assertEqual(allocated, ["z-canary"])
        self.assertEqual(submitted, ["z-canary"])
        stale = state["units"]["z-canary"][
            "dispatch_canary_stale_completions"]
        self.assertEqual(stale[0]["attempt"], attempt.name)
        prior_scope = state["units"]["z-canary"][
            "attempt_dispatch_scopes"][attempt.name]
        self.assertEqual(prior_scope["plan_digest"], S.plan_digest(old_plan))
        self.assertNotEqual(prior_scope["plan_digest"], S.plan_digest(plan))

    def test_current_scoped_qualification_authorizes_fanout(self):
        plan = self.plan(4)
        state = {"units": {"z-canary": {
            "state": "DONE", "attempt_dir": None, "attempts": [],
            "job_id": None, "gpu_hours": 0.0}},
            "dispatch_canary_qualification": {
                "unit": "z-canary", "plan_digest": S.plan_digest(plan),
                "target_commit": None}}

        result, allocated, submitted = self.advance(plan, state)

        self.assertEqual(result[1], 2, result[0])
        self.assertEqual(set(allocated), {"a-independent", "b-dependent"})
        self.assertEqual(set(submitted), set(allocated))

    def test_max_new_dispatches_still_limits_qualified_fanout(self):
        plan = self.plan(4)
        state = {"units": {"z-canary": {
            "state": "DONE", "attempt_dir": None, "attempts": [],
            "job_id": None, "gpu_hours": 0.0}},
            "dispatch_canary_qualification": {
                "unit": "z-canary", "plan_digest": S.plan_digest(plan),
                "target_commit": None}}

        result, allocated, submitted = self.advance(
            plan, state, max_new=1)

        self.assertEqual(result[1], 1, result[0])
        self.assertEqual(len(allocated), 1)
        self.assertEqual(submitted, allocated)

    def test_authoritative_done_creates_first_class_qualification(self):
        plan = self.plan(4)
        attempt = self.tmp / "qualifying-canary"
        attempt.mkdir()
        state = {"units": {"z-canary": {
            "state": "RUNNING", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "job_id": "engine-1",
            "gpu_hours": 0.0,
            "attempt_receipt_seals": {attempt.name: "a" * 64},
            "attempt_dispatch_scopes": {attempt.name: {
                "unit": "z-canary",
                "plan_digest": S.plan_digest(plan),
                "target_commit": None}}}}}
        result_payload = json.dumps(
            {"produced_head": None, "receipt_sha256": "a" * 64},
            sort_keys=True, separators=(",", ":"))
        check = (S.DONE, "DONE", "",
                 S.CHECK_RESULT_PREFIX + " " + result_payload)

        result, allocated, submitted = self.advance(
            plan, state, dry_run=False, check=check)

        self.assertEqual(result[1], 2, result[0])
        self.assertEqual(set(allocated), {"a-independent", "b-dependent"})
        self.assertEqual(set(submitted), set(allocated))
        qualification = state["dispatch_canary_qualification"]
        self.assertEqual(qualification["unit"], "z-canary")
        self.assertEqual(qualification["plan_digest"], S.plan_digest(plan))
        self.assertIsNone(qualification["target_commit"])
        self.assertEqual(qualification["attempt"], attempt.name)

    def test_persisted_authoritative_done_repairs_missing_qualification(self):
        plan = self.plan(4)
        attempt = self.tmp / "persisted-canary"
        attempt.mkdir()
        state = {"units": {"z-canary": {
            "state": "DONE", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "job_id": "engine-finished",
            "gpu_hours": 0.0,
            "attempt_receipt_seals": {attempt.name: "b" * 64},
            "attempt_dispatch_scopes": {attempt.name: {
                "unit": "z-canary",
                "plan_digest": S.plan_digest(plan),
                "target_commit": None}}}}}

        result, allocated, submitted = self.advance(
            plan, state, dry_run=False)

        self.assertEqual(result[1], 2, result[0])
        self.assertEqual(set(allocated), {"a-independent", "b-dependent"})
        self.assertEqual(set(submitted), set(allocated))
        qualification = state["dispatch_canary_qualification"]
        self.assertEqual(qualification["attempt"], attempt.name)

    def test_cleared_repair_chooses_newest_sealed_attempt_after_stale_cycle(self):
        plan = self.plan(4)
        a1 = self.tmp / "old-stale-attempt"
        a2 = self.tmp / "new-completed-attempt"
        state = {"units": {"z-canary": {
            "state": "DONE", "attempt_dir": None,
            "attempts": [str(a1), str(a2)], "job_id": None,
            "gpu_hours": 0.0,
            "attempt_receipt_seals": {
                a1.name: "1" * 64, a2.name: "2" * 64},
            "attempt_dispatch_scopes": {
                a1.name: {"unit": "z-canary",
                          "plan_digest": "0" * 64,
                          "target_commit": None},
                a2.name: {"unit": "z-canary",
                          "plan_digest": S.plan_digest(plan),
                          "target_commit": None}}}}}

        result, allocated, submitted = self.advance(
            plan, state, dry_run=False)

        self.assertEqual(result[1], 2, result[0])
        self.assertEqual(set(allocated), {"a-independent", "b-dependent"})
        self.assertEqual(set(submitted), set(allocated))
        self.assertEqual(
            state["dispatch_canary_qualification"]["attempt"], a2.name)

    def test_plan_digest_change_invalidates_prior_qualification(self):
        plan = self.plan(4)
        state = {"units": {"z-canary": {
            "state": "DONE", "attempt_dir": None, "attempts": [],
            "job_id": None, "gpu_hours": 0.0}},
            "dispatch_canary_qualification": {
                "unit": "z-canary", "plan_digest": S.plan_digest(plan),
                "target_commit": None}}
        plan["units"][0]["command"] = "printf changed"

        result, allocated, submitted = self.advance(
            plan, state, dry_run=True)

        self.assertEqual(result[1], 1, result[0])
        self.assertIsNone(result[2], result[0])
        self.assertEqual(allocated, ["z-canary"])
        self.assertEqual(submitted, ["z-canary"])
        self.assert_only_canary_admitted(allocated, submitted)

    def test_cleared_stale_completion_opens_only_fresh_canary_admission(self):
        old_plan = self.plan(2)
        plan = self.plan(2)
        plan["units"][0]["command"] = "printf new-scope"
        state = {"units": {"z-canary": {
            "state": "DONE", "attempt_dir": None,
            "attempts": ["old-canary-attempt"], "job_id": None,
            "gpu_hours": 0.0}},
            "dispatch_canary_qualification": {
                "unit": "z-canary",
                "plan_digest": S.plan_digest(old_plan),
                "target_commit": None,
                "attempt": "old-canary-attempt"}}

        result, allocated, submitted = self.advance(
            plan, state, dry_run=False)

        self.assertEqual(result[1], 1, result[0])
        self.assertEqual(allocated, ["z-canary"])
        self.assertEqual(submitted, ["z-canary"])
        self.assert_only_canary_admitted(allocated, submitted)
        stale = state["units"]["z-canary"][
            "dispatch_canary_stale_completions"]
        self.assertEqual(stale[0]["attempt"], "old-canary-attempt")

    def test_normal_waiting_is_a_successful_noop(self):
        plan = self.plan(1)
        attempt = self.tmp / "running-canary"
        attempt.mkdir()
        state = {"units": {"z-canary": {
            "state": "RUNNING", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "job_id": "dry-running",
            "gpu_hours": 0.0}}}

        result, allocated, submitted = self.advance(plan, state)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        self.assert_only_canary_admitted(allocated, submitted)

    def test_occupancy_is_one_to_one_with_capacity_consuming_admissions(self):
        unit = self.unit("u")
        for state_name in S.LIVE_STATES:
            with self.subTest(state=state_name, attempt=True):
                self.assertTrue(S._occupies_live_resources(
                    unit, {"state": state_name, "attempt_dir": "/attempt"}))
            with self.subTest(state=state_name, attempt=False):
                self.assertFalse(S._occupies_live_resources(
                    unit, {"state": state_name, "attempt_dir": None}))
        for state_name in ("FAILED", "FAILED_EVIDENCE",
                           "PREFLIGHT_REFUSED", "DONE", None):
            with self.subTest(state=state_name, terminal=True):
                self.assertFalse(S._occupies_live_resources(
                    unit, {"state": state_name, "attempt_dir": None,
                           "attempts": ["historical"]}))

    def test_blocking_failure_with_no_allocation_exits_nonzero(self):
        plan = self.plan(2)
        plan["units"] = [self.unit("z-canary"),
                         self.unit("b-dependent", ["z-canary"])]
        plan_path = self.tmp / "plan.json"
        plan_path.write_text(json.dumps(plan))
        state_dir = self.tmp / "command-state"
        S.save_state(str(state_dir), {
            "schema_version": 1, "plan_digest": S.plan_digest(plan),
            "halted": None, "units": {"z-canary": {
                "state": "FAILED", "attempt_dir": None,
                "attempts": ["old"], "job_id": None,
                "gpu_hours": 0.0}}})
        args = Namespace(
            plan=str(plan_path), state_dir=str(state_dir),
            root=str(self.tmp / "command-runs"), dry_run=True,
            max_new_dispatches=None, accept_plan_change=False)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = S.cmd_run(args)

        self.assertEqual(rc, S.EXIT_HALTED, output.getvalue())
        self.assertIn("authoritative DONE qualification is absent",
                      output.getvalue())

    def test_dry_plan_preview_does_not_persist_stale_requeue(self):
        original_plan = self.plan(2)
        preview_plan = self.plan(2)
        preview_plan["units"][0]["command"] = "printf preview"
        plan_path = self.tmp / "preview-plan.json"
        plan_path.write_text(json.dumps(preview_plan))
        state_dir = self.tmp / "preview-state"
        S.save_state(str(state_dir), {
            "schema_version": 1,
            "plan_digest": S.plan_digest(original_plan),
            "halted": None,
            "units": {"z-canary": {
                "state": "DONE", "attempt_dir": None,
                "attempts": ["completed-canary"], "job_id": None,
                "gpu_hours": 0.0}},
            "dispatch_canary_qualification": {
                "unit": "z-canary",
                "plan_digest": S.plan_digest(original_plan),
                "target_commit": None,
                "attempt": "completed-canary"}})
        before = (state_dir / S.STATE_FILE).read_bytes()
        args = Namespace(
            plan=str(plan_path), state_dir=str(state_dir),
            root=str(self.tmp / "preview-runs"), dry_run=True,
            max_new_dispatches=None, accept_plan_change=True)

        with contextlib.redirect_stdout(io.StringIO()):
            rc = S.cmd_run(args)

        self.assertEqual(rc, S.EXIT_OK)
        self.assertEqual((state_dir / S.STATE_FILE).read_bytes(), before)


class CommitIdentityTests(unittest.TestCase):
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
        self.target = git(self.repo, "rev-parse", "HEAD")
        self.plan = {"name": "commit-identity", "units": [{
            "id": "code", "kind": "code", "repo": str(self.repo),
            "target_branch": "main", "mode": "full-access",
            "prompt": "work", "outputs": ["evidence.md"]}]}

    def run_advance(self, allocate=None):
        state = {"units": {}}
        allocated = self.tmp / "runs" / "code" / "attempt-1"

        def default_allocate(_plan, _unit, _root):
            allocated.mkdir(parents=True, exist_ok=True)
            return str(allocated), None

        with mock.patch.object(
                S, "_allocate", side_effect=allocate or default_allocate), \
                mock.patch.object(S, "renew_lease", return_value=True):
            result = S.advance(
                self.plan, state, str(self.tmp / "state"),
                str(self.tmp / "runs"), True)
        return result, state

    def test_detached_head_at_target_commit_is_admitted(self):
        git(self.repo, "checkout", "-q", "--detach", self.target)

        result, state = self.run_advance()

        self.assertEqual(result[1], 1, result[0])
        self.assertIsNone(result[2], result[0])
        intent = state["units"]["code"]["attempt_launch_intents"][
            "attempt-1"]
        self.assertEqual(intent["base_commit"], self.target)
        self.assertEqual(intent["target_commit"], self.target)
        self.assertEqual(intent["base_branch"], "(detached HEAD)")

    def test_checkout_ahead_on_another_branch_is_refused(self):
        git(self.repo, "checkout", "-qb", "topic")
        (self.repo / "tracked.txt").write_text("topic\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "topic")
        topic = git(self.repo, "rev-parse", "HEAD")

        result, state = self.run_advance()

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNotNone(result[2], result[0])
        self.assertIn(topic, " ".join(result[0]))
        self.assertIn(self.target, " ".join(result[0]))
        self.assertFalse(state["units"]["code"]["attempts"])

    def test_running_canary_wait_does_not_inspect_blocked_code_source(self):
        git(self.repo, "checkout", "-qb", "ahead-while-blocked")
        (self.repo / "tracked.txt").write_text("ahead but ineligible\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "ahead but ineligible")
        canary_attempt = self.tmp / "running-pipeline-canary"
        canary_attempt.mkdir()
        plan = {"name": "blocked-source", "dispatch_canary": {
            "unit": "canary", "width": 2}, "units": [{
                "id": "canary", "kind": "pipeline", "command": "true",
                "runtime": "none", "outputs": ["canary.txt"]},
                dict(self.plan["units"][0])]}
        state = {"units": {"canary": {
            "state": "RUNNING", "attempt_dir": str(canary_attempt),
            "attempts": [str(canary_attempt)], "job_id": "dry-canary",
            "gpu_hours": 0.0}}}

        with mock.patch.object(
                S, "_dispatch_source_identity",
                wraps=S._dispatch_source_identity) as source_identity:
            result = S.advance(
                plan, state, str(self.tmp / "blocked-state"),
                str(self.tmp / "blocked-runs"), True)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        source_identity.assert_not_called()

    def test_running_code_canary_wait_does_not_resolve_remote_target(self):
        attempt = self.tmp / "running-code-canary"
        attempt.mkdir()
        plan = dict(self.plan)
        plan["dispatch_canary"] = {"unit": "code", "width": 2}
        plan["units"] = [dict(self.plan["units"][0]), {
            "id": "fanout", "kind": "pipeline", "command": "true",
            "runtime": "none", "outputs": ["fanout.txt"]}]
        state = {"units": {"code": {
            "state": "RUNNING", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "job_id": "dry-code",
            "gpu_hours": 0.0}}}

        with mock.patch.object(
                S, "_resolve_dispatch_target",
                side_effect=AssertionError("ordinary wait resolved target")) \
                as resolve:
            result = S.advance(
                plan, state, str(self.tmp / "code-wait-state"),
                str(self.tmp / "code-wait-runs"), True)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        resolve.assert_not_called()

    def test_other_completion_does_not_resolve_running_code_canary_target(self):
        canary_attempt = self.tmp / "running-code"
        other_attempt = self.tmp / "finishing-other"
        canary_attempt.mkdir()
        other_attempt.mkdir()
        plan = {"name": "completion-while-canary-runs",
                "dispatch_canary": {"unit": "code", "width": 2},
                "units": [dict(self.plan["units"][0]), {
                    "id": "other", "kind": "pipeline", "command": "true",
                    "runtime": "none", "outputs": ["other.txt"]}]}
        state = {"units": {
            "code": {"state": "RUNNING",
                     "attempt_dir": str(canary_attempt),
                     "attempts": [str(canary_attempt)],
                     "job_id": "agent-code", "gpu_hours": 0.0},
            "other": {"state": "RUNNING",
                      "attempt_dir": str(other_attempt),
                      "attempts": [str(other_attempt)],
                      "job_id": "engine-other", "gpu_hours": 0.0}}}
        done_payload = json.dumps(
            {"produced_head": None, "receipt_sha256": "f" * 64},
            sort_keys=True, separators=(",", ":"))

        def check(attempt, *_args, **_kwargs):
            if Path(attempt).name == other_attempt.name:
                return (S.DONE, "DONE", "",
                        S.CHECK_RESULT_PREFIX + " " + done_payload)
            return S.RUNNING, "RUNNING", "", ""

        with mock.patch.object(S, "_check", side_effect=check), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(
                    S, "trusted_launch_facts",
                    return_value={"launch_host": "test-host"}), \
                mock.patch.object(S.W, "launch_host_problem",
                                  return_value=None), \
                mock.patch.object(
                    S, "_resolve_dispatch_target",
                    side_effect=AssertionError(
                        "other completion resolved running canary target")) \
                as resolve:
            result = S.advance(
                plan, state, str(self.tmp / "other-done-state"),
                str(self.tmp / "other-done-runs"), False)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        self.assertEqual(state["units"]["other"]["state"], "DONE")
        resolve.assert_not_called()

    def test_full_width_wait_does_not_resolve_completed_canary_target(self):
        other_attempt = self.tmp / "full-width-other"
        other_attempt.mkdir()
        plan = {"name": "full-width-code-canary",
                "dispatch_canary": {"unit": "code", "width": 1},
                "units": [dict(self.plan["units"][0]), {
                    "id": "other", "kind": "pipeline", "command": "true",
                    "runtime": "none", "outputs": ["other.txt"]}, {
                    "id": "waiting", "kind": "pipeline", "command": "true",
                    "runtime": "none", "outputs": ["waiting.txt"]}]}
        state = {"units": {
            "code": {"state": "DONE", "attempt_dir": None,
                     "attempts": [], "job_id": None, "gpu_hours": 0.0,
                     "merged_as": self.target},
            "other": {"state": "RUNNING",
                      "attempt_dir": str(other_attempt),
                      "attempts": [str(other_attempt)],
                      "job_id": "engine-other", "gpu_hours": 0.0}},
            "dispatch_canary_qualification": {
                "unit": "code", "plan_digest": S.plan_digest(plan),
                "target_commit": self.target}}

        with mock.patch.object(
                S, "_check", return_value=(S.RUNNING, "RUNNING", "", "")), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(
                    S, "_resolve_dispatch_target",
                    side_effect=AssertionError(
                        "full width must not resolve the target")) as resolve:
            result = S.advance(
                plan, state, str(self.tmp / "full-width-state"),
                str(self.tmp / "full-width-runs"), False)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        resolve.assert_not_called()

    def test_full_width_done_repair_does_not_resolve_canary_target(self):
        canary_attempt = self.tmp / "done-code-canary"
        other_attempt = self.tmp / "full-width-repair-other"
        canary_attempt.mkdir()
        other_attempt.mkdir()
        produced = "c" * 40
        receipt = {
            "unit": "code", "repo": str(self.remote), "pr": "PR-9",
            "target": "main", "head": produced,
            "merged_as": self.target, "method": "merge",
            "merged": True, "attested": True}
        plan = {"name": "full-width-done-repair",
                "dispatch_canary": {"unit": "code", "width": 1},
                "units": [dict(self.plan["units"][0]), {
                    "id": "other", "kind": "pipeline", "command": "true",
                    "runtime": "none", "outputs": ["other.txt"]}]}
        state = {"units": {
            "code": {"state": "DONE",
                     "attempt_dir": str(canary_attempt),
                     "attempts": [str(canary_attempt)],
                     "job_id": "agent-done", "gpu_hours": 0.0,
                     "merged_as": self.target, "merge_pr": "PR-9",
                     "merge_receipt": receipt,
                     "attempt_produced_heads": {
                         canary_attempt.name: produced}},
            "other": {"state": "RUNNING",
                      "attempt_dir": str(other_attempt),
                      "attempts": [str(other_attempt)],
                      "job_id": "engine-other", "gpu_hours": 0.0}}}

        with mock.patch.object(S, "_archive_code_worktree"), \
                mock.patch.object(
                    S, "trusted_launch_facts",
                    return_value={"launch_host": "test-host"}), \
                mock.patch.object(S.W, "launch_host_problem",
                                  return_value=None), \
                mock.patch.object(
                    S, "_check", return_value=(S.RUNNING, "RUNNING", "", "")), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(
                    S, "_resolve_dispatch_target",
                    side_effect=AssertionError(
                        "full width DONE repair must not resolve target")) \
                as resolve:
            result = S.advance(
                plan, state, str(self.tmp / "full-repair-state"),
                str(self.tmp / "full-repair-runs"), False)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        resolve.assert_not_called()

    def test_dependency_blocked_wait_does_not_resolve_canary_target(self):
        canary_attempt = self.tmp / "done-dependency-canary"
        running_attempt = self.tmp / "running-dependency"
        canary_attempt.mkdir()
        running_attempt.mkdir()
        produced = "d" * 40
        receipt = {
            "unit": "code", "repo": str(self.remote), "pr": "PR-10",
            "target": "main", "head": produced,
            "merged_as": self.target, "method": "merge",
            "merged": True, "attested": True}
        plan = {"name": "dependency-blocked-code-canary",
                "dispatch_canary": {"unit": "code", "width": 3},
                "units": [dict(self.plan["units"][0]), {
                    "id": "running", "kind": "pipeline", "command": "true",
                    "runtime": "none", "outputs": ["running.txt"]}, {
                    "id": "blocked", "kind": "pipeline", "command": "true",
                    "runtime": "none", "outputs": ["blocked.txt"],
                    "needs": ["running"]}]}
        state = {"units": {
            "code": {"state": "DONE",
                     "attempt_dir": str(canary_attempt),
                     "attempts": [str(canary_attempt)],
                     "job_id": "agent-done", "gpu_hours": 0.0,
                     "merged_as": self.target, "merge_pr": "PR-10",
                     "merge_receipt": receipt,
                     "attempt_produced_heads": {
                         canary_attempt.name: produced}},
            "running": {"state": "RUNNING",
                        "attempt_dir": str(running_attempt),
                        "attempts": [str(running_attempt)],
                        "job_id": "engine-running", "gpu_hours": 0.0}},
            "dispatch_canary_qualification": {
                "unit": "code", "plan_digest": S.plan_digest(plan),
                "target_commit": self.target}}

        with mock.patch.object(S, "_archive_code_worktree"), \
                mock.patch.object(
                    S, "trusted_launch_facts",
                    return_value={"launch_host": "test-host"}), \
                mock.patch.object(S.W, "launch_host_problem",
                                  return_value=None), \
                mock.patch.object(
                S, "_check", return_value=(S.RUNNING, "RUNNING", "", "")), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(
                    S, "_resolve_dispatch_target",
                    side_effect=AssertionError(
                        "dependency wait must not resolve target")) as resolve:
            result = S.advance(
                plan, state, str(self.tmp / "dependency-wait-state"),
                str(self.tmp / "dependency-wait-runs"), False)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        resolve.assert_not_called()

    def test_max_new_zero_does_not_resolve_unqualified_canary_target(self):
        plan = {"name": "max-new-zero-code-canary",
                "dispatch_canary": {"unit": "code", "width": 2},
                "units": [dict(self.plan["units"][0]), {
                    "id": "ready", "kind": "pipeline", "command": "true",
                    "runtime": "none", "outputs": ["ready.txt"]}]}
        state = {"units": {"code": {
            "state": "DONE", "attempt_dir": None, "attempts": [],
            "job_id": None, "gpu_hours": 0.0, "merged_as": self.target}}}

        with mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(
                S, "_resolve_dispatch_target",
                side_effect=AssertionError(
                    "max-new zero must not resolve target")) as resolve:
            result = S.advance(
                plan, state, str(self.tmp / "max-zero-state"),
                str(self.tmp / "max-zero-runs"), False, max_new=0)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        resolve.assert_not_called()

    def test_dry_target_drift_preview_preserves_qualified_state_bytes(self):
        code = dict(self.plan["units"][0])
        plan = {"name": "dry-target-drift",
                "dispatch_canary": {"unit": "code", "width": 2},
                "units": [code, {
                    "id": "fanout", "kind": "pipeline", "command": "true",
                    "runtime": "none", "outputs": ["fanout.txt"]}]}
        state_dir = self.tmp / "dry-drift-state"
        state = {"schema_version": 1, "halted": None,
                 "plan_digest": S.plan_digest(plan), "units": {"code": {
                     "state": "DONE", "attempt_dir": None,
                     "attempts": ["settled-code"], "job_id": None,
                     "gpu_hours": 0.0, "merged_as": self.target}},
                 "dispatch_canary_qualification": {
                     "unit": "code", "plan_digest": S.plan_digest(plan),
                     "target_commit": self.target,
                     "attempt": "settled-code"}}
        S.save_state(str(state_dir), state)
        before = (state_dir / S.STATE_FILE).read_bytes()
        (self.repo / "tracked.txt").write_text("target drift\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "target drift")
        git(self.repo, "push", "-q", "origin", "main")
        plan_path = self.tmp / "dry-drift-plan.json"
        plan_path.write_text(json.dumps(plan))
        args = Namespace(
            plan=str(plan_path), state_dir=str(state_dir),
            root=str(self.tmp / "dry-drift-runs"), dry_run=True,
            max_new_dispatches=None, accept_plan_change=False)

        def allocate(_plan, unit, root):
            path = Path(root) / unit["id"] / "fresh"
            path.mkdir(parents=True, exist_ok=True)
            return str(path), None

        with mock.patch.object(S, "_allocate", side_effect=allocate), \
                mock.patch.object(S, "_submit", return_value=(
                    "dry-code", None)), \
                mock.patch.object(S, "_bind", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = S.cmd_run(args)

        self.assertEqual(rc, S.EXIT_OK)
        self.assertEqual((state_dir / S.STATE_FILE).read_bytes(), before)

    def test_launch_is_bound_to_checked_commit_after_checkout_moves(self):
        moved = {"head": None}

        def allocate(_plan, _unit, _root):
            git(self.repo, "checkout", "-qb", "moved-after-check")
            (self.repo / "tracked.txt").write_text("moved\n")
            git(self.repo, "add", "tracked.txt")
            git(self.repo, "commit", "-qm", "moved")
            moved["head"] = git(self.repo, "rev-parse", "HEAD")
            path = self.tmp / "runs" / "code" / "attempt-1"
            path.mkdir(parents=True)
            return str(path), None

        result, state = self.run_advance(allocate=allocate)

        self.assertEqual(result[1], 1, result[0])
        intent = state["units"]["code"]["attempt_launch_intents"][
            "attempt-1"]
        self.assertNotEqual(moved["head"], self.target)
        self.assertEqual(intent["base_commit"], self.target)
        self.assertEqual(intent["target_commit"], self.target)

    def test_target_is_resolved_once_per_advance_and_bound_to_each_launch(self):
        second = dict(self.plan["units"][0])
        second["id"] = "code-two"
        second["target_branch"] = " main "
        plan = {"name": "one-resolution", "units": [
            dict(self.plan["units"][0]), second]}
        state = {"units": {}}
        counter = {"n": 0}

        def allocate(_plan, unit, root):
            counter["n"] += 1
            path = Path(root) / unit["id"] / ("a%d" % counter["n"])
            path.mkdir(parents=True)
            return str(path), None

        with mock.patch.object(S, "_allocate", side_effect=allocate), \
                mock.patch.object(
                    S, "_resolve_dispatch_target",
                    wraps=S._resolve_dispatch_target) as resolve, \
                mock.patch.object(
                    S, "_git_push_destination",
                    wraps=S._git_push_destination) as remote_query:
            result = S.advance(
                plan, state, str(self.tmp / "once-state"),
                str(self.tmp / "once-runs"), True)

        self.assertEqual(result[1], 2, result[0])
        self.assertEqual(resolve.call_count, 1)
        target_ref = "refs/heads/main"
        target_reads = [call for call in remote_query.call_args_list
                        if call.args and call.args[-1] == target_ref]
        self.assertEqual(len(target_reads), 1, remote_query.call_args_list)
        for uid in ("code", "code-two"):
            intents = state["units"][uid]["attempt_launch_intents"]
            intent = next(iter(intents.values()))
            self.assertEqual(intent["base_commit"], self.target)
            self.assertEqual(intent["target_commit"], self.target)

    def test_merged_code_canary_repairs_cleared_qualification(self):
        code = dict(self.plan["units"][0])
        plan = {"name": "repair-code-canary", "dispatch_canary": {
            "unit": "code", "width": 2}, "units": [code, {
                "id": "fanout", "kind": "pipeline", "command": "true",
                "runtime": "none", "outputs": ["fanout.txt"]}]}
        attempt = "settled-code-attempt"
        produced = "e" * 40
        (self.repo / "tracked.txt").write_text("merged canary result\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "merged canary result")
        merged_as = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main")
        receipt = {
            "unit": "code", "repo": str(self.remote), "pr": "PR-1",
            "target": "main", "head": produced,
            "merged_as": merged_as, "method": "merge",
            "merged": True, "attested": True}
        state = {"units": {"code": {
            "state": "DONE", "attempt_dir": None,
            "attempts": [str(self.tmp / attempt)], "job_id": None,
            "gpu_hours": 0.0, "merged_as": merged_as,
            "merge_pr": "PR-1", "merge_receipt": dict(receipt),
            "attempt_produced_heads": {attempt: produced},
            "attempt_receipt_seals": {attempt: "a" * 64},
            "attempt_dispatch_scopes": {attempt: {
                "unit": "code", "plan_digest": S.plan_digest(plan),
                "target_commit": self.target}}}}}
        allocated = []

        def allocate(_plan, unit, root):
            allocated.append(unit["id"])
            path = Path(root) / unit["id"] / "fresh"
            path.mkdir(parents=True)
            return str(path), None

        with mock.patch.object(S, "_allocate", side_effect=allocate), \
                mock.patch.object(S, "_submit", return_value=(
                    "engine-fanout", None)), \
                mock.patch.object(S, "_bind", return_value=None), \
                mock.patch.object(S, "renew_lease", return_value=True):
            result = S.advance(
                plan, state, str(self.tmp / "repair-state"),
                str(self.tmp / "repair-runs"), False)

        self.assertEqual(result[1], 1, result[0])
        self.assertEqual(allocated, ["fanout"])
        qualification = state["dispatch_canary_qualification"]
        self.assertEqual(qualification["attempt"], attempt)
        self.assertEqual(qualification["target_commit"], merged_as)
        self.assertEqual(
            qualification["admission_target_commit"], self.target)
        self.assertEqual(state["units"]["code"]["state"], "DONE")

    def test_legacy_launch_intent_requires_exact_target_upgrade(self):
        attempt = self.tmp / "legacy-intent-attempt"
        attempt.mkdir()
        problem, anchored = S._capture_code_launch(
            str(attempt), self.plan["units"][0])
        self.assertIsNone(problem)
        intent = anchored["intent"]
        intent["schema_version"] = 5
        intent.pop("target_commit", None)
        (self.repo / "tracked.txt").write_text("target advanced\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "target advanced")
        git(self.repo, "push", "-q", "origin", "main")

        refusal = S._upgrade_legacy_code_launch_intent(
            intent, self.plan["units"][0])

        self.assertIsInstance(refusal, S.PreflightRefusal)
        self.assertEqual(refusal.reason, "dispatch-base")
        self.assertIn("cannot be upgraded", refusal)
        self.assertIn("schema 5", S._code_launch_intent_problem(
            intent, self.plan["units"][0], attempt.name))
        self.assertIn(
            "no valid worktree launch intent object",
            S._upgrade_legacy_code_launch_intent(
                None, self.plan["units"][0]))
        malformed = dict(intent, schema_version="5")
        self.assertIn(
            "invalid schema_version",
            S._upgrade_legacy_code_launch_intent(
                malformed, self.plan["units"][0]))
        malformed7 = dict(
            intent, schema_version=7, target_commit="not-a-hash")
        self.assertIn(
            "does not bind base_commit",
            S._upgrade_legacy_code_launch_intent(
                malformed7, self.plan["units"][0]))

    def test_submit_does_not_migrate_host_only_intent_stub(self):
        attempt = self.tmp / "host-only-stub"
        attempt.mkdir()
        state = {"units": {"code": {"attempt_launch_intents": {
            attempt.name: {"launch_host": "test-host"}}}}}

        with mock.patch.object(
                S, "_upgrade_legacy_code_launch_intent",
                side_effect=AssertionError("host-only stub was migrated")) \
                as migrate:
            job, problem = S._submit(
                self.plan["units"][0], str(attempt), True, state,
                str(self.tmp / "host-only-state"))

        self.assertIsNone(job)
        self.assertIn("launch intent", str(problem))
        migrate.assert_not_called()

    def test_submit_refuses_stale_schema8_intent_at_new_target(self):
        attempt = self.tmp / "stale-schema8-intent"
        attempt.mkdir()
        problem, anchored = S._capture_code_launch(
            str(attempt), self.plan["units"][0])
        self.assertIsNone(problem)
        intent = anchored["intent"]
        self.assertEqual(intent["schema_version"], 8)
        (self.repo / "tracked.txt").write_text("new dispatch target\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "new dispatch target")
        new_target = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main")
        source, problem = S._resolve_dispatch_target(self.plan["units"][0])
        self.assertIsNone(problem)
        state = {"units": {"code": {"attempt_launch_intents": {
            attempt.name: intent}}}}

        job, refusal = S._submit(
            self.plan["units"][0], str(attempt), True, state,
            str(self.tmp / "stale-schema8-state"),
            dispatch_source=source)

        self.assertIsNone(job)
        self.assertIsInstance(refusal, S.PreflightRefusal)
        self.assertEqual(refusal.reason, "dispatch-base")
        self.assertEqual(source["target_commit"], new_target)
        self.assertNotEqual(intent["target_commit"], new_target)
        self.assertIn("refusing to reuse stale schema-8", refusal)

        job, implicit_refusal = S._submit(
            self.plan["units"][0], str(attempt), True, state,
            str(self.tmp / "stale-schema8-implicit-state"))
        self.assertIsNone(job)
        self.assertIsInstance(implicit_refusal, S.PreflightRefusal)
        self.assertEqual(implicit_refusal.reason, "dispatch-base")

    def test_legacy_upgrade_reuses_bound_dispatch_target(self):
        attempt = self.tmp / "bound-legacy-intent"
        attempt.mkdir()
        problem, anchored = S._capture_code_launch(
            str(attempt), self.plan["units"][0])
        self.assertIsNone(problem)
        intent = anchored["intent"]
        intent["schema_version"] = 5
        intent.pop("target_commit", None)
        source, problem = S._resolve_dispatch_target(self.plan["units"][0])
        self.assertIsNone(problem)

        with mock.patch.object(
                S, "_resolve_dispatch_target",
                side_effect=AssertionError("target resolved twice")) \
                as resolve:
            problem = S._upgrade_legacy_code_launch_intent(
                intent, self.plan["units"][0], dispatch_source=source)

        self.assertIsNone(problem)
        self.assertEqual(intent["schema_version"], 8)
        self.assertEqual(intent["target_commit"], self.target)
        resolve.assert_not_called()

    def test_running_live_legacy_attempt_waits_without_target_lookup(self):
        attempt = self.tmp / "live-legacy-attempt"
        attempt.mkdir()
        problem, anchored = S._capture_code_launch(
            str(attempt), self.plan["units"][0])
        self.assertIsNone(problem)
        intent = anchored["intent"]
        intent["schema_version"] = 5
        intent.pop("target_commit", None)
        plan = dict(self.plan)
        plan["dispatch_canary"] = {"unit": "code", "width": 1}
        state = {"units": {"code": {
            "state": "RUNNING", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "job_id": "agent-live",
            "gpu_hours": 0.0,
            "attempt_launch_intents": {attempt.name: intent}}}}

        with mock.patch.object(
                S, "_check", return_value=(S.RUNNING, "RUNNING", "", "")), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(
                    S, "trusted_launch_facts",
                    return_value={"launch_host": intent["launch_host"]}), \
                mock.patch.object(S.W, "launch_host_problem",
                                  return_value=None), \
                mock.patch.object(
                    S, "_resolve_dispatch_target",
                    side_effect=AssertionError(
                        "ordinary legacy RUNNING wait resolved target")) \
                as resolve:
            result = S.advance(
                plan, state, str(self.tmp / "legacy-live-state"),
                str(self.tmp / "legacy-live-runs"), False)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        self.assertEqual(intent["schema_version"], 5)
        self.assertNotIn("target_commit", intent)
        self.assertEqual(intent["base_commit"], self.target)
        self.assertEqual(state["units"]["code"]["state"], "RUNNING")
        self.assertNotIn(
            "attempt_dispatch_scopes", state["units"]["code"])
        resolve.assert_not_called()

    def test_completed_live_legacy_attempt_upgrades_before_judgment(self):
        attempt = self.tmp / "completed-legacy-attempt"
        attempt.mkdir()
        problem, anchored = S._capture_code_launch(
            str(attempt), self.plan["units"][0])
        self.assertIsNone(problem)
        intent = anchored["intent"]
        intent["schema_version"] = 5
        intent.pop("target_commit", None)
        plan = dict(self.plan)
        plan["dispatch_canary"] = {"unit": "code", "width": 1}
        state = {"units": {"code": {
            "state": "RUNNING", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "job_id": "agent-live",
            "gpu_hours": 0.0,
            "attempt_launch_intents": {attempt.name: intent}}}}
        payload = json.dumps({
            "produced_head": self.target, "receipt_sha256": "a" * 64,
        }, sort_keys=True, separators=(",", ":"))

        with mock.patch.object(
                S, "_check", return_value=(
                    S.DONE, "DONE", "",
                    S.CHECK_RESULT_PREFIX + " " + payload)), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(
                    S, "trusted_launch_facts",
                    return_value={"launch_host": intent["launch_host"]}), \
                mock.patch.object(S.W, "launch_host_problem",
                                  return_value=None), \
                mock.patch.object(S.W, "validate_pinned_head",
                                  return_value=None):
            result = S.advance(
                plan, state, str(self.tmp / "legacy-complete-state"),
                str(self.tmp / "legacy-complete-runs"), False)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNone(result[2], result[0])
        self.assertEqual(intent["schema_version"], 8)
        self.assertEqual(intent["target_commit"], self.target)
        scope = state["units"]["code"]["attempt_dispatch_scopes"][
            attempt.name]
        self.assertEqual(scope["unit"], "code")
        self.assertEqual(scope["plan_digest"], S.plan_digest(plan))
        self.assertEqual(scope["target_commit"], self.target)
        self.assertIn(
            "upgraded completed legacy launch intent", " ".join(result[0]))

    def test_legacy_canary_migration_keeps_pre_ratification_plan_scope(self):
        attempt = self.tmp / "legacy-plan-change-attempt"
        attempt.mkdir()
        old_plan = dict(self.plan)
        old_plan["dispatch_canary"] = {"unit": "code", "width": 1}
        plan = json.loads(json.dumps(old_plan))
        plan["units"][0]["prompt"] = "ratified new prompt"
        problem, anchored = S._capture_code_launch(
            str(attempt), plan["units"][0])
        self.assertIsNone(problem)
        intent = anchored["intent"]
        intent["schema_version"] = 5
        intent.pop("target_commit", None)
        state = {"plan_digest": S.plan_digest(old_plan), "units": {"code": {
            "state": "RUNNING", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "job_id": "agent-live",
            "gpu_hours": 0.0,
            "attempt_launch_intents": {attempt.name: intent}}}}

        with mock.patch.object(
                S, "_check", return_value=(
                    S.DONE, "DONE", "", S.CHECK_RESULT_PREFIX + " " +
                    json.dumps({
                        "produced_head": self.target,
                        "receipt_sha256": "b" * 64,
                    }, sort_keys=True, separators=(",", ":")))), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(
                    S, "trusted_launch_facts",
                    return_value={"launch_host": intent["launch_host"]}), \
                mock.patch.object(S.W, "launch_host_problem",
                                  return_value=None), \
                mock.patch.object(S.W, "validate_pinned_head",
                                  return_value=None):
            result = S.advance(
                plan, state, str(self.tmp / "legacy-change-state"),
                str(self.tmp / "legacy-change-runs"), False,
                accept_plan_change=True)

        self.assertIsNone(result[2], result[0])
        self.assertEqual(state["plan_digest"], S.plan_digest(plan))
        scope = state["units"]["code"]["attempt_dispatch_scopes"][
            attempt.name]
        self.assertEqual(scope["plan_digest"], S.plan_digest(old_plan))
        self.assertNotEqual(scope["plan_digest"], state["plan_digest"])

    def test_merged_canary_target_lookup_failure_is_retriable(self):
        code = dict(self.plan["units"][0])
        plan = {"name": "transient-code-canary", "dispatch_canary": {
            "unit": "code", "width": 2}, "units": [code, {
                "id": "fanout", "kind": "pipeline", "command": "true",
                "runtime": "none", "outputs": ["fanout.txt"]}]}
        attempt = "merged-code-attempt"
        produced = "c" * 40
        merged_as = "d" * 40
        receipt = {
            "unit": "code", "repo": str(self.remote), "pr": "PR-2",
            "target": "main", "head": produced,
            "merged_as": merged_as, "method": "merge",
            "merged": True, "attested": True}
        state = {"units": {"code": {
            "state": "DONE", "attempt_dir": None,
            "attempts": [str(self.tmp / attempt)], "job_id": None,
            "gpu_hours": 0.0, "merged_as": merged_as,
            "merge_pr": "PR-2", "merge_receipt": dict(receipt),
            "attempt_produced_heads": {attempt: produced},
            "attempt_receipt_seals": {attempt: "b" * 64},
            "attempt_dispatch_scopes": {attempt: {
                "unit": "code", "plan_digest": S.plan_digest(plan),
                "target_commit": self.target}}}}}
        refusal = S._dispatch_base_refusal(
            "code", self.repo, "temporary target lookup failure")

        with mock.patch.object(
                S, "_resolve_dispatch_target",
                return_value=(None, refusal)):
            result = S.advance(
                plan, state, str(self.tmp / "transient-state"),
                str(self.tmp / "transient-runs"), False)

        self.assertEqual(result[1], 0, result[0])
        self.assertIsNotNone(result[2], result[0])
        self.assertEqual(state["units"]["code"]["state"], "DONE")
        self.assertNotEqual(
            state["units"]["code"]["state"], "FAILED_EVIDENCE")

    def test_target_commit_change_invalidates_qualification(self):
        plan = dict(self.plan)
        plan["dispatch_canary"] = {"unit": "code", "width": 2}
        plan["units"] = [dict(self.plan["units"][0]), {
            "id": "fanout", "kind": "pipeline", "command": "true",
            "runtime": "none", "outputs": ["fanout.txt"]}]
        state = {"units": {"code": {
            "state": "DONE", "attempt_dir": None, "attempts": [],
            "job_id": None, "gpu_hours": 0.0}},
            "dispatch_canary_qualification": {
                "unit": "code", "plan_digest": S.plan_digest(plan),
                "target_commit": self.target}}
        (self.repo / "tracked.txt").write_text("new target\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "new target")
        new_target = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main")
        allocations = []

        def allocate(_plan, unit, _root):
            allocations.append(unit["id"])
            return None, "must not allocate"

        with mock.patch.object(S, "_allocate", side_effect=allocate):
            result = S.advance(
                plan, state, str(self.tmp / "scope-state"),
                str(self.tmp / "scope-runs"), True)

        self.assertNotEqual(new_target, self.target)
        self.assertEqual(result[1], 0, result[0])
        self.assertEqual(allocations, ["code"])
        self.assertNotIn("fanout", allocations)
        self.assertIn("authoritative DONE qualification is absent",
                      " ".join(result[0]))

    def test_target_move_during_canary_run_cannot_qualify_new_scope(self):
        plan = dict(self.plan)
        plan["dispatch_canary"] = {"unit": "code", "width": 1}
        attempt = self.tmp / "code-canary-attempt"
        attempt.mkdir()
        state = {"units": {"code": {
            "state": "DONE", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "job_id": "agent-old",
            "gpu_hours": 0.0,
            "attempt_receipt_seals": {attempt.name: "c" * 64},
            "attempt_dispatch_scopes": {attempt.name: {
                "unit": "code", "plan_digest": S.plan_digest(plan),
                "target_commit": self.target}}}}}
        (self.repo / "tracked.txt").write_text("moved while running\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "moved while running")
        new_target = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main")

        problem = S._record_dispatch_canary_qualification(
            plan, state, {"code": plan["units"][0]}, "code", {})

        self.assertNotEqual(new_target, self.target)
        self.assertIsInstance(problem, S.DispatchCanaryScopeStale)
        self.assertIn("not current", problem)
        self.assertNotIn("dispatch_canary_qualification", state)

    def test_target_move_requeues_canary_and_still_blocks_fanout(self):
        plan = dict(self.plan)
        plan["dispatch_canary"] = {"unit": "code", "width": 2}
        plan["units"] = [dict(self.plan["units"][0]), {
            "id": "fanout", "kind": "pipeline", "command": "true",
            "runtime": "none", "outputs": ["fanout.txt"]}]
        attempt = self.tmp / "stale-code-canary"
        attempt.mkdir()
        state = {"units": {"code": {
            "state": "DONE", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "job_id": "agent-old",
            "gpu_hours": 0.0,
            "attempt_receipt_seals": {attempt.name: "d" * 64},
            "attempt_dispatch_scopes": {attempt.name: {
                "unit": "code", "plan_digest": S.plan_digest(plan),
                "target_commit": self.target}}}}}
        (self.repo / "tracked.txt").write_text("new scope\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "new scope")
        new_target = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main")
        units = {unit["id"]: unit for unit in plan["units"]}
        problem = S._record_dispatch_canary_qualification(
            plan, state, units, "code", {})
        report = []

        S._requeue_stale_dispatch_canary(
            state, units["code"], problem, report,
            str(self.tmp / "requeue-state"), dry_run=False)
        allocated = []

        def allocate(_plan, unit, root):
            allocated.append(unit["id"])
            path = Path(root) / unit["id"] / "fresh"
            path.mkdir(parents=True)
            return str(path), None

        with mock.patch.object(S, "_allocate", side_effect=allocate), \
                mock.patch.object(S, "renew_lease", return_value=True):
            result = S.advance(
                plan, state, str(self.tmp / "requeue-state"),
                str(self.tmp / "requeue-runs"), True)

        self.assertEqual(result[1], 1, result[0])
        self.assertEqual(allocated, ["code"])
        self.assertNotIn("fanout", allocated)
        self.assertEqual(
            state["units"]["code"]["dispatch_canary_stale_completions"]
            [0]["attempt"], attempt.name)
        fresh_scope = state["units"]["code"]["attempt_dispatch_scopes"][
            "fresh"]
        self.assertEqual(fresh_scope["target_commit"], new_target)


if __name__ == "__main__":
    unittest.main()
