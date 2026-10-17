"""Provider-error resumes through advance, using real attempt worktrees."""
import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import test_attempt_worktrees as F
from tests.test_continuation import _paseo_stub_on_path
from tests.scheduler_fixture import isolated_module_path as setUpModule
from tests.scheduler_fixture import cleanup_module_path as tearDownModule

S = F.S
P = S.SPRS


class Crash(BaseException):
    pass


class ProviderResumeTests(unittest.TestCase):
    def setUp(self):
        F.TestPerAttemptWorktrees.setUp(self)
        context = F.paseo_resolvable()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.attempt = F.TestPerAttemptWorktrees.attempt(self, "code", "one")
        self.unit = dict(F.code_unit(self.repo), provider_resume={"max": 2})
        self.plan = {"name": "resume-test", "units": [self.unit]}
        self.state_dir = self.tmp / "state"
        self.state = {"schema_version": 1, "units": {}, "halted": None,
                      "plan_digest": S.plan_digest(self.plan)}
        job, error = S._submit(self.unit, str(self.attempt), False,
                               self.state, str(self.state_dir))
        self.assertIsNone(error)
        self.us = self.state["units"]["code"]
        self.us.update(state="RUNNING", attempt_dir=str(self.attempt),
                       attempts=[str(self.attempt)], job_id=job, gpu_hours=0,
                       state_changed_at=1000)
        facts = S.SS.trusted_launch_facts(self.state, "code", self.attempt.name)
        self.workspace = Path(facts["execution_workspace"])
        self.clock = 1000
        self.agent_status = "error"
        self.error_number = 1
        self.message = "Selected model is at capacity. Please try a different model."
        self.sends = []
        self.send_rc = 0
        self.crash_send = False
        self.prefix = "[System Error] "
        S.U.run = self.run_fake
        self.patch(S.time, "time", side_effect=lambda: self.clock)
        self.patch(S.SE, "_check", side_effect=self.check)
        self.assertTrue(S.acquire_lease(self.state_dir)[0])
        self.addCleanup(S.release_lease, self.state_dir)
        S.save_state(self.state_dir, self.state)

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def run_fake(self, argv, **kwargs):
        if argv[:2] == ["paseo", "inspect"]:
            return 0, json.dumps({"Status": self.agent_status,
                                  "UpdatedAt": str(self.error_number)}), ""
        if argv[:2] == ["paseo", "logs"]:
            self.assertEqual(argv[-2:], ["--tail", "1"])
            return 0, self.prefix + json.dumps({"error": {"message": self.message}}), ""
        if argv[:2] == ["paseo", "send"]:
            saved = S.load_state(self.state_dir)["units"]["code"]
            self.assertEqual(saved["provider_resumes"][-1]["status"], "pending")
            self.assertIn("--no-wait", argv)
            if self.crash_send:
                raise Crash()
            if self.send_rc:
                return self.send_rc, "", "daemon down"
            self.sends.append(argv)
            self.agent_status = "running"
            return 0, '{"status":"sent"}', ""
        return self.fake(argv, **kwargs)

    def check(self, *_args, **_kwargs):
        notes = []
        status = S.U._code_state(str(self.attempt), {"job_id": self.us["job_id"]},
                                 [], ["evidence.md"], notes)
        result = S.CHECK_RESULT_PREFIX + " " + json.dumps({
            "produced_head": None, "receipt_sha256": "f" * 64,
        }, sort_keys=True, separators=(",", ":"))
        return S.U.STATES[status], "\n".join(notes), "", result

    def advance(self):
        report, dispatched, _ = S.advance(
            self.plan, self.state, str(self.state_dir), str(self.tmp / "runs"),
            False, max_new=0, accept_plan_change=True)
        self.assertEqual(dispatched, 0)
        return "\n".join(report)

    def reload(self):
        self.state = S.load_state(self.state_dir)
        self.us = self.state["units"]["code"]

    def test_capacity_backoff_same_attempt_and_ledger(self):
        ledger = self.tmp / "state-home/hanig-review-gate/rounds" / ("a" * 64)
        ledger.mkdir(parents=True)
        (ledger / "cycle-1-round-2.json").write_text('{"round":2}')
        dispositions = self.attempt / "dispositions.json"
        dispositions.write_text('{"fixture":"reproduced"}')
        before = {p: p.read_bytes() for p in [*ledger.iterdir(), dispositions]}
        original = copy.deepcopy(self.us)
        self.assertIn("waiting for backoff", self.advance())
        self.assertNotIn(self.attempt.name, self.us.get("attempt_receipt_seals", {}))
        project = F.ROOT / "skills/hanig-project/scripts"
        sys.path.insert(0, str(project))
        spec = importlib.util.spec_from_file_location("resume_report", project / "report.py")
        reporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reporter)
        row = reporter.unit_rows({"plan": self.plan, "state": self.state,
                                  "receipts": {"code": {"state": "FAILED"}},
                                  "receipt_provenance": {"code": (self.attempt.name, "f" * 64)}})[0]
        self.assertEqual(row["state"], "RUNNING")
        self.clock = 1299
        self.reload()
        self.assertIn("waiting for backoff", self.advance())
        self.assertEqual(self.sends, [])
        self.assertEqual(self.us["state"], "RUNNING")
        self.clock = 1300
        self.assertIn("resumed 1/2", self.advance())
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.us["attempt_dir"], original["attempt_dir"])
        self.assertEqual(self.us["attempts"], original["attempts"])
        self.assertEqual(self.us["job_id"], original["job_id"])
        self.assertEqual(self.us["attempt_launch_facts"], original["attempt_launch_facts"])
        self.assertEqual(len(self.fake.launches), 1)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertIn("current review round number", self.sends[0][-1])
        self.assertIn("dispositions", self.sends[0][-1])
        self.assertTrue(self.workspace.is_dir())

    def test_second_resume_then_third_error_fails_with_cleanup(self):
        self.advance()
        self.clock += 300
        self.advance()
        self.error_number += 1
        self.agent_status = "error"
        self.advance()
        self.clock += 899
        self.advance()
        self.assertEqual(len(self.sends), 1)
        self.clock += 1
        self.assertIn("resumed 2/2", self.advance())
        self.assertEqual(len(self.sends), 2)
        self.error_number += 1
        self.agent_status = "error"
        self.assertIn("spent 2/2", self.advance())
        self.assertEqual(self.us["state"], "FAILED")
        self.assertFalse(self.workspace.exists())
        self.assertEqual(len(self.us["attempts"]), 1)

    def test_content_filter_never_resumes(self):
        self.message = "flagged for possible cybersecurity risk (503)"
        self.advance()
        self.assertEqual(self.us["state"], "FAILED")
        self.assertNotIn("provider_resumes", self.us)
        self.assertEqual(self.sends, [])
        self.assertFalse(self.workspace.exists())

    def test_failed_lifecycle_is_not_error(self):
        self.agent_status = "failed"
        self.advance()
        self.assertEqual(self.us["state"], "FAILED")
        self.assertNotIn("provider_resumes", self.us)

    def test_unknown_error_never_resumes(self):
        self.message = "invalid model configuration"
        self.advance()
        self.assertEqual(self.us["state"], "FAILED")
        self.assertNotIn("provider_resumes", self.us)

    def test_conversation_text_is_not_a_provider_message(self):
        self.prefix = "[User] "
        self.advance()
        self.assertEqual(self.us["state"], "FAILED")

    def test_zero_and_absent_preserve_legacy_state_bytes(self):
        initial = copy.deepcopy(self.state)
        results = []
        with mock.patch.object(S.SW, "_archive_code_worktree"), \
                mock.patch.object(P, "maybe_resume", side_effect=AssertionError("off")):
            for config in [None, {"max": 0}]:
                self.state = copy.deepcopy(initial)
                self.us = self.state["units"]["code"]
                self.unit.pop("provider_resume", None)
                if config is not None:
                    self.unit["provider_resume"] = config
                self.state["plan_digest"] = S.plan_digest(self.plan)
                self.advance()
                self.assertEqual(self.us["state"], "FAILED")
                results.append(json.dumps(self.us, sort_keys=True))
        self.assertEqual(results[0], results[1])

    def test_crash_before_send_retries_same_pending_entry(self):
        self.advance()
        self.clock += 300
        self.crash_send = True
        with self.assertRaises(Crash):
            self.advance()
        self.reload()
        entry = copy.deepcopy(self.us["provider_resumes"][0])
        self.crash_send = False
        self.advance()
        self.assertEqual(len(self.us["provider_resumes"]), 1)
        self.assertEqual(self.us["provider_resumes"][0]["at"], entry["at"])
        self.assertEqual(len(self.sends), 1)

    def test_send_failure_retries_same_slot(self):
        self.advance()
        self.clock += 300
        self.send_rc = 1
        self.assertIn("send failed", self.advance())
        self.assertEqual(self.us["state"], "RUNNING")
        self.reload()
        self.send_rc = 0
        self.advance()
        self.assertEqual(len(self.us["provider_resumes"]), 1)
        self.assertEqual(len(self.sends), 1)

    def test_crash_after_send_reconciles_running_agent(self):
        self.advance()
        self.clock += 300
        save = S.SS.save_state
        def crash_after_send(directory, state):
            if state["units"]["code"]["provider_resumes"][-1]["status"] == "sent":
                raise Crash()
            save(directory, state)
        with mock.patch.object(S.SS, "save_state", side_effect=crash_after_send):
            with self.assertRaises(Crash):
                self.advance()
        self.reload()
        self.assertEqual(self.us["provider_resumes"][0]["status"], "pending")
        self.advance()
        self.assertEqual(self.us["provider_resumes"][0]["status"], "sent")
        self.assertEqual(len(self.sends), 1)

    def test_old_error_after_send_does_not_spend_second_slot(self):
        self.advance()
        self.clock += 300
        self.advance()
        self.agent_status = "error"
        self.clock += 900
        self.assertIn("waiting for a new observation", self.advance())
        self.assertEqual(len(self.us["provider_resumes"]), 1)
        self.assertEqual(len(self.sends), 1)

    def test_dirty_work_is_preserved_before_send(self):
        (self.workspace / "untracked.txt").write_text("uncommitted work")
        (self.workspace / "tracked.txt").write_text("changed tracked work")
        self.advance()
        self.clock += 300
        self.advance()
        snapshot = self.us["provider_resumes"][0]["recovery_snapshot"]
        self.assertIsNone(S.R.validate_snapshot(snapshot))
        content = Path(snapshot["snapshot_path"]) / "content"
        self.assertEqual((content / "untracked.txt").read_text(), "uncommitted work")
        self.assertEqual((content / "tracked.txt").read_text(), "changed tracked work")
        self.assertTrue(self.workspace.is_dir())
        self.assertEqual(len(self.sends), 1)

    def test_preservation_failure_retains_attempt_without_sending(self):
        (self.workspace / "untracked.txt").write_text("keep")
        self.advance()
        self.clock += 300
        with mock.patch.object(S.R, "preserve_worktree", return_value=(None, "disk full")):
            self.assertIn("disk full", self.advance())
        self.assertEqual(self.sends, [])
        self.assertEqual(self.us["state"], "RUNNING")
        self.assertTrue(self.workspace.is_dir())

    def test_status_is_dated_delivery_observation(self):
        self.advance()
        self.clock += 300
        self.advance()
        before = (self.state_dir / S.STATE_FILE).read_bytes()
        self.clock += 3600
        row = S.status_report(self.plan, self.state, self.state_dir)["units"][0]
        self.assertEqual(row["provider_resume"], {
            "resumed": 1, "max": 2, "observed_at": "1970-01-01T00:21:40Z", "pending": False})
        self.assertEqual((self.state_dir / S.STATE_FILE).read_bytes(), before)


class ProviderResumeValidationTests(unittest.TestCase):
    def setUp(self):
        _paseo_stub_on_path(self)

    def test_bounds(self):
        for config in [None, [], 2, {}, {"max": True}, {"max": -1},
                       {"max": 4}, {"max": "2"}, {"max": 1.5}]:
            with self.subTest(config=config), self.assertRaisesRegex(S.PlanError, "provider_resume"):
                S.validate_plan({"units": [dict(F.code_unit("/fixture"), outputs=["o"], provider_resume=config)]})
        for value in range(4):
            S.validate_plan({"units": [dict(F.code_unit("/fixture"), outputs=["o"], provider_resume={"max": value})]})


if __name__ == "__main__":
    unittest.main()
