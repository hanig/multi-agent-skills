"""ARC-691: status time telemetry and conservative per-unit deadlines."""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402


def unit(**overrides):
    value = {"id": "u", "kind": "slurm", "runtime": "none",
             "command": "true", "outputs": ["out.txt"]}
    value.update(overrides)
    return value


class TestDeadlineDeclaration(unittest.TestCase):
    def test_positive_finite_seconds_are_accepted(self):
        S.validate_plan({"name": "p", "units": [unit(deadline_s=0.5)]})

    def test_non_positive_or_non_finite_deadlines_are_refused(self):
        for value in (0, -1, True, "60", math.nan, math.inf, -math.inf,
                      10 ** 309):
            with self.subTest(value=value):
                with self.assertRaises(S.PlanError) as raised:
                    S.validate_plan(
                        {"name": "p", "units": [unit(deadline_s=value)]})
                self.assertIn("deadline_s", str(raised.exception))

    def test_schema_names_the_reference_and_conservative_breach_action(self):
        note = next(note for field, _kinds, _required, note in S.SCHEMA_FIELDS
                    if field == "deadline_s")
        self.assertIn("allocated_at", note)
        self.assertIn("NEEDS_HUMAN", note)
        self.assertIn("does not cancel", note)
        self.assertIn("release an output claim", note)
        self.assertIn("mint a retry", note)


class TestStatusCarriesTime(unittest.TestCase):
    def test_one_observation_clock_drives_unit_and_state_age(self):
        plan = {"name": "p", "units": [unit()]}
        state = {"schema_version": 1, "halted": None, "units": {"u": {
            "state": "RUNNING", "attempt_dir": "/runs/u/a1",
            "attempts": ["/runs/u/a1"], "gpu_hours": 0,
            "allocated_at": 100.0, "state_changed_at": 150.0}}}
        with tempfile.TemporaryDirectory() as state_dir:
            report = S.status_report(plan, state, state_dir,
                                     observed_at=200.0)
        row = report["units"][0]
        self.assertEqual(row["age_s"], 100)
        self.assertEqual(row["state_age_s"], 50)
        self.assertEqual(report["observed_at"], report["generated_at"])

    def test_missing_historical_timestamps_are_reported_as_unknown(self):
        plan = {"name": "p", "units": [unit()]}
        state = {"schema_version": 1, "halted": None, "units": {"u": {
            "state": "RUNNING", "attempt_dir": "/runs/u/a1",
            "attempts": ["/runs/u/a1"], "gpu_hours": 0}}}
        with tempfile.TemporaryDirectory() as state_dir:
            row = S._status_rows(plan, state, state_dir,
                                 observed_at=200.0)[0]
        self.assertIsNone(row["age_s"])
        self.assertIsNone(row["state_age_s"])

    def test_a_real_transition_resets_only_the_state_clock(self):
        state = {"state": "ALLOCATED", "allocated_at": 100.0,
                 "state_changed_at": 100.0}
        S._set_unit_state(state, "SUBMITTED", changed_at=120.0)
        self.assertEqual(state["allocated_at"], 100.0)
        self.assertEqual(state["state_changed_at"], 120.0)
        S._set_unit_state(state, "SUBMITTED", changed_at=140.0)
        self.assertEqual(state["state_changed_at"], 120.0)


class TestDeadlineBreach(unittest.TestCase):
    def _advance(self, allocated_at, deadline_s=60, observed_at=200.0,
                 check_moves_clock_to=None, check_result=None,
                 historical_state=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        state_dir = root / "state"
        attempt = root / "runs" / "u" / "a1"
        state_dir.mkdir(parents=True)
        attempt.mkdir(parents=True)
        plan = {"name": "p", "units": [unit(
            deadline_s=deadline_s, max_attempts=2,
            retry={"mode": "restart", "max_lost": {"wall_seconds": 60}})],
            "retry_limits": {"wall_seconds": 60}}
        state = {"schema_version": 1, "halted": None, "units": {"u": {
            "state": "SUBMITTED", "job_id": "42",
            "attempt_dir": str(attempt), "attempts": [str(attempt)],
            "gpu_hours": 0, "allocated_at": allocated_at,
            "state_changed_at": allocated_at + 1}}}
        if historical_state:
            state["units"]["u"].pop("state_changed_at")
            state["units"]["u"]["state"] = "RUNNING"
        S.save_state(str(state_dir), state)
        clock = [observed_at]

        def run_check(*_args, **_kwargs):
            if check_moves_clock_to is not None:
                clock[0] = check_moves_clock_to
            return check_result or (S.RUNNING, "", "", "")

        check = mock.Mock(side_effect=run_check)
        release = mock.Mock()
        allocate = mock.Mock(side_effect=AssertionError(
            "a deadline must not mint another attempt"))
        with mock.patch.object(S.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(S, "renew_lease", return_value=True), \
                mock.patch.object(S, "_check", check), \
                mock.patch.object(S, "_release_output_claims", release), \
                mock.patch.object(S, "_allocate", allocate):
            report, dispatched, halted = S.advance(
                plan, S.load_state(str(state_dir)), str(state_dir),
                str(root / "runs"), False)
        return (report, dispatched, halted,
                S.load_state(str(state_dir)), state_dir, check, release)

    def test_breach_blocks_without_check_retry_cancellation_or_claim_release(self):
        report, dispatched, halted, state, state_dir, check, release = \
            self._advance(allocated_at=100.0)
        current = state["units"]["u"]
        self.assertEqual(current["state"], "NEEDS_HUMAN")
        self.assertEqual(current["reason"], "deadline_exceeded")
        self.assertEqual(current["attempts"], [current["attempt_dir"]])
        self.assertEqual(current["job_id"], "42")
        self.assertEqual(current["state_changed_at"], 200.0)
        self.assertEqual((dispatched, halted), (0, None))
        check.assert_not_called()
        release.assert_not_called()
        self.assertIn("no cancellation or retry was requested",
                      "\n".join(report))
        intents = S.read_outbox(str(state_dir))
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["verb"], "block")
        self.assertEqual(intents[0]["why"], "deadline_exceeded")

    def test_an_attempt_inside_its_deadline_is_checked_normally(self):
        _report, _dispatched, _halted, state, _sd, check, release = \
            self._advance(allocated_at=150.0)
        current = state["units"]["u"]
        self.assertEqual(current["state"], "RUNNING")
        self.assertNotIn("reason", current)
        check.assert_called_once()
        release.assert_not_called()

    def test_historical_state_gets_an_honest_first_observed_clock(self):
        _report, _dispatched, _halted, state, _sd, _check, _release = \
            self._advance(allocated_at=150.0, historical_state=True)
        current = state["units"]["u"]
        self.assertEqual(current["state_changed_at"], 200.0)
        self.assertEqual(current["state_changed_at_basis"], "first_observed")

    def test_crossing_the_deadline_during_check_does_not_mint_a_retry(self):
        _report, dispatched, _halted, state, _sd, check, release = \
            self._advance(allocated_at=100.0, observed_at=150.0,
                          check_moves_clock_to=170.0,
                          check_result=(S.PREEMPTED, "", "", ""))
        current = state["units"]["u"]
        self.assertEqual(current["state"], "NEEDS_HUMAN")
        self.assertEqual(current["reason"], "deadline_exceeded")
        self.assertIsNotNone(current["attempt_dir"])
        self.assertEqual(len(current["attempts"]), 1)
        self.assertEqual(dispatched, 0)
        check.assert_called_once()
        release.assert_not_called()

    def test_crossing_during_orphan_recovery_retains_the_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            attempt = root / "runs" / "u" / "a1"
            state_dir.mkdir(parents=True)
            attempt.mkdir(parents=True)
            plan = {"name": "p", "units": [unit(deadline_s=60)]}
            state = {"schema_version": 1, "halted": None,
                     "plan_digest": S.plan_digest(plan), "units": {"u": {
                         "state": "ALLOCATED", "job_id": None,
                         "attempt_dir": str(attempt),
                         "attempts": [str(attempt)], "gpu_hours": 0,
                         "allocated_at": 100.0,
                         "state_changed_at": 100.0}}}
            S.save_state(str(state_dir), state)
            clock = [150.0]

            def reconcile(*_args, **_kwargs):
                clock[0] = 170.0
                return None, None

            allocate = mock.Mock(side_effect=AssertionError(
                "a deadline crossed during recovery must retain its attempt"))
            with mock.patch.object(S.time, "time", side_effect=lambda: clock[0]), \
                    mock.patch.object(S, "renew_lease", return_value=True), \
                    mock.patch.object(S, "reconcile_orphan",
                                      side_effect=reconcile), \
                    mock.patch.object(S, "_allocate", allocate), \
                    mock.patch.object(S, "_release_output_claims"):
                S.advance(plan, S.load_state(str(state_dir)), str(state_dir),
                          str(root / "runs"), False)
            current = S.load_state(str(state_dir))["units"]["u"]
        self.assertEqual(current["state"], "NEEDS_HUMAN")
        self.assertEqual(current["reason"], "deadline_exceeded")
        self.assertEqual(current["attempt_dir"], str(attempt))
        allocate.assert_not_called()

    def test_a_prior_human_block_does_not_hide_the_deadline_block(self):
        with tempfile.TemporaryDirectory() as state_dir:
            current = {"attempt_dir": "/runs/u/a1", "job_id": "42"}
            S.emit_intent(state_dir, "p", "u", "NEEDS_HUMAN", current,
                          kind="slurm")
            current["reason"] = "deadline_exceeded"
            S.emit_intent(state_dir, "p", "u", "NEEDS_HUMAN", current,
                          kind="slurm")
            intents = S.read_outbox(state_dir)
        self.assertEqual([intent["why"] for intent in intents],
                         ["blocked on a person, not on compute",
                          "deadline_exceeded"])

    def test_deadline_supersedes_but_preserves_a_prior_human_reason(self):
        declared = unit(deadline_s=60)
        current = {"state": "NEEDS_HUMAN", "reason": "permission_required",
                   "attempt_dir": "/runs/u/a1", "allocated_at": 100.0,
                   "state_changed_at": 120.0}
        self.assertTrue(S._deadline_exceeded(declared, current, 200.0))
        self.assertTrue(S._mark_deadline_exceeded(
            declared, current, observed_at=200.0))
        self.assertEqual(current["reason"], "deadline_exceeded")
        self.assertEqual(current["deadline_previous_reason"],
                         "permission_required")
        self.assertEqual(current["state_changed_at"], 120.0,
                         "the state remained NEEDS_HUMAN; only its reason "
                         "became more specific")

    def test_dry_run_reports_breach_without_migrating_or_mutating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            attempt = root / "runs" / "u" / "a1"
            state_dir.mkdir(parents=True)
            attempt.mkdir(parents=True)
            plan = {"name": "p", "units": [unit(deadline_s=60)]}
            state = {"schema_version": 1, "halted": None,
                     "plan_digest": S.plan_digest(plan), "units": {"u": {
                         "state": "ALLOCATED", "job_id": None,
                         "attempt_dir": str(attempt),
                         "attempts": [str(attempt)], "gpu_hours": 0,
                         "allocated_at": 100.0}}}
            S.save_state(str(state_dir), state)
            before = (state_dir / S.STATE_FILE).read_bytes()
            with mock.patch.object(S.time, "time", return_value=200.0):
                report, _dispatched, _halted = S.advance(
                    plan, S.load_state(str(state_dir)), str(state_dir),
                    str(root / "runs"), True)
            after = (state_dir / S.STATE_FILE).read_bytes()
        self.assertEqual(after, before)
        self.assertIn("DRY RUN -- deadline_exceeded", "\n".join(report))

    def test_dry_run_refuses_a_real_job_retained_after_deadline(self):
        """The state label changed, but the job is still live and bound."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            attempt = root / "runs" / "u" / "a1"
            state_dir.mkdir(parents=True)
            attempt.mkdir(parents=True)
            plan = {"name": "p", "units": [unit(deadline_s=60)]}
            state = {"schema_version": 1, "halted": None,
                     "plan_digest": S.plan_digest(plan), "units": {"u": {
                         "state": "NEEDS_HUMAN",
                         "reason": "deadline_exceeded", "job_id": "42",
                         "attempt_dir": str(attempt),
                         "attempts": [str(attempt)], "gpu_hours": 0,
                         "allocated_at": 100.0}}}
            S.save_state(str(state_dir), state)
            before = (state_dir / S.STATE_FILE).read_bytes()
            report, _dispatched, _halted = S.advance(
                plan, S.load_state(str(state_dir)), str(state_dir),
                str(root / "runs"), True)
            after = (state_dir / S.STATE_FILE).read_bytes()
        self.assertEqual(after, before)
        self.assertIn("REFUSING to dry-run", report[0])
        self.assertIn("holds REAL attempts", report[0])

    def test_prior_human_reason_cannot_bypass_deadline_resource_protection(self):
        declared = unit(deadline_s=60)
        current = {"state": "NEEDS_HUMAN", "reason": "permission_required",
                   "job_id": "42", "attempt_dir": "/runs/u/a1",
                   "allocated_at": 100.0}
        self.assertTrue(S._occupies_live_resources(declared, current))

    def test_removing_deadline_resumes_checker_and_clears_prior_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            attempt = root / "runs" / "u" / "a1"
            state_dir.mkdir(parents=True)
            attempt.mkdir(parents=True)
            old_plan = {"name": "p", "units": [unit(deadline_s=60)]}
            new_plan = {"name": "p", "units": [unit()]}
            state = {"schema_version": 1, "halted": None,
                     "plan_digest": S.plan_digest(old_plan), "units": {"u": {
                         "state": "NEEDS_HUMAN",
                         "reason": "deadline_exceeded",
                         "deadline_previous_reason": "permission_required",
                         "deadline_s": 60.0, "deadline_at": 160.0,
                         "job_id": "42", "attempt_dir": str(attempt),
                         "attempts": [str(attempt)], "gpu_hours": 0,
                         "allocated_at": 100.0,
                         "state_changed_at": 170.0}}}
            S.save_state(str(state_dir), state)
            check = mock.Mock(return_value=(S.RUNNING, "", "", ""))
            with mock.patch.object(S.time, "time", return_value=180.0), \
                    mock.patch.object(S, "renew_lease", return_value=True), \
                    mock.patch.object(S, "_check", check), \
                    mock.patch.object(S, "_release_output_claims"):
                S.advance(new_plan, S.load_state(str(state_dir)),
                          str(state_dir), str(root / "runs"), False,
                          accept_plan_change=True)
            current = S.load_state(str(state_dir))["units"]["u"]
        self.assertEqual(current["state"], "RUNNING")
        self.assertNotIn("reason", current)
        self.assertNotIn("deadline_previous_reason", current)
        check.assert_called_once()

    def test_poll_returning_to_same_visible_state_keeps_state_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            attempt = root / "runs" / "u" / "a1"
            state_dir.mkdir(parents=True)
            attempt.mkdir(parents=True)
            declared = unit(converge={"metrics": "m.jsonl",
                                      "criterion": {"metric": "loss",
                                                    "mode": "min",
                                                    "threshold": 0.5}})
            plan = {"name": "p", "units": [declared]}
            state = {"schema_version": 1, "halted": None,
                     "plan_digest": S.plan_digest(plan), "units": {"u": {
                         "state": "NEEDS_HUMAN", "job_id": "42",
                         "attempt_dir": str(attempt),
                         "attempts": [str(attempt)], "gpu_hours": 0,
                         "allocated_at": 100.0,
                         "state_changed_at": 120.0}}}
            S.save_state(str(state_dir), state)
            with mock.patch.object(S.time, "time", return_value=200.0), \
                    mock.patch.object(S, "renew_lease", return_value=True), \
                    mock.patch.object(S, "_check",
                                      return_value=(S.DONE, "", "", "result")), \
                    mock.patch.object(
                        S, "_reported_check_result",
                        return_value=({"receipt_sha256": "a" * 64}, None)), \
                    mock.patch.object(S, "converge_verdict",
                                      return_value=("NOT_CONVERGED", ["x"])), \
                    mock.patch.object(S, "_release_output_claims"):
                S.advance(plan, S.load_state(str(state_dir)), str(state_dir),
                          str(root / "runs"), False)
            current = S.load_state(str(state_dir))["units"]["u"]
        self.assertEqual(current["state"], "NEEDS_HUMAN")
        self.assertEqual(current["state_changed_at"], 120.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
