"""Provider-stop retention through the real advance, status and stop CLIs."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tests import test_provider_resume as H
from tests.scheduler_fixture import isolated_module_path as setUpModule
from tests.scheduler_fixture import cleanup_module_path as tearDownModule

S = H.S
P = S.SPS


def stop_record(h):
    return h.us()["attempt_provider_stops"][h.attempt.name]


def stop(h, command):
    successor = {"id": "successor", "kind": "slurm", "command": "true",
                 "runtime": "none", "outputs": ["result"]}
    h.plan["units"].append(successor)
    h.change_plan()
    state = S.load_state(h.sd)
    receipt = {"unit": "successor", "repo": "owner/repo", "pr": "https://github.com/owner/repo/pull/42",
               "target": "main", "head": "a" * 40, "merged_as": "b" * 40,
               "method": "merge", "merged": True, "attested": True}
    state["units"]["successor"] = {"state": "DONE", "attempt_dir": "finished",
        "attempts": [], "gpu_hours": 0, "job_id": "finished",
        "attempt_produced_heads": {"finished": receipt["head"]},
        "merge_receipt": receipt, "merge_pr": receipt["pr"], "merged_as": receipt["merged_as"]}
    S.save_state(h.sd, state)
    argv = [sys.executable, str(h.driver), command, str(h.planfile),
            "--state-dir", str(h.sd), "--unit", "code", "--approver", "owner", "--reason", "stopped"]
    if command == "supersede":
        argv += ["--by", "successor"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    h.eq(proc.returncode, 0, proc.stdout + proc.stderr)
    return proc.stdout


class ProviderStopTests(unittest.TestCase):
    def assert_report(self, output, h, attempt=None):
        attempt = attempt or h.attempt.name
        lines = [line for line in output.splitlines() if "retained provider stop " + attempt in line]
        self.assertEqual(len(lines), 1, output)
        for value in ("code", attempt, str(h.work), "refs/heads/recovery/code-" + attempt):
            self.assertIn(value, lines[0])

    def test_content_filter(self):
        with H.scenario(message=P.CONTENT_FILTER) as h:
            head = H.F.git(h.work, "rev-parse", "HEAD")
            output = h.cli()
            h.failed()
            self.assertTrue(h.work.exists())
            self.assertNotIn("provider_resumes", h.us())
            record = stop_record(h)
            self.assertEqual(record, {
                "schema_version": 1, "reason": "content_filter", "message_class": P.CONTENT_FILTER,
                "observed_at": "1970-01-01T00:16:40Z",
                "branch": h.original["attempt_launch_intents"][h.attempt.name]["branch"],
                "worktree_path": str(h.work), "recovery_ref": "refs/heads/recovery/code-" + h.attempt.name,
                "head_at_retention": head, "phase": "retained"})
            self.assert_report(output, h)
            self.assert_report(h.cli("status", expected=2), h)

    def test_exhaustion_and_other_false_returns(self):
        with H.scenario(persistent=True) as h:
            h.ready()
            h.cli()
            h.cli(now=2200)
            before = h.record()
            h.cli()
            h.expect("FAILED")
            h.sent(2)
            self.assertEqual(stop_record(h)["reason"], "provider_resume_exhausted")
            self.assertEqual(stop_record(h)["message_class"], "is at capacity")
            self.assertEqual(h.record(), before)
            h.assert_snapshot(h.record())
            self.assertTrue(h.work.exists())
        for failure in ("send_rc", "snapshot_failure", "unknown_backoff", "unmatched"):
            with self.subTest(failure=failure), H.scenario() as h:
                h.ready()
                cfg = {failure: 1}
                if failure == "unknown_backoff":
                    state = S.load_state(h.sd)
                    state["units"]["code"]["provider_resumes"][h.attempt.name]["next_not_before"] = None
                    S.save_state(h.sd, state)
                    cfg = {}
                if failure == "unmatched":
                    cfg = {"message": "invalid model (429, 500, 501, 505)"}
                h.cli(**cfg)
                h.expect("FAILED")
                self.assertNotIn("attempt_provider_stops", h.us())

    def test_repeated_advance_does_not_reenter(self):
        with H.scenario(message=P.CONTENT_FILTER) as h:
            h.cli()
            before = copy.deepcopy(h.us())
            # An unrelated later observation must not revive or reclassify it.
            for status in ("running", "error"):
                self.assert_report(h.cli(now=3000, status=status, message="503"), h)
                self.assertEqual((h.tmp / "observations").read_text(), "0")
                h.expect("FAILED")
                h.sent(0)
                self.assertEqual(h.us()["attempt_provider_stops"], before["attempt_provider_stops"])
                for key in ("attempts", "attempt_dir", "job_id", "attempt_workspaces"):
                    self.assertEqual(h.us()[key], before[key])
                self.assertTrue(h.work.exists())

    def test_stop_commands_and_reporting(self):
        for command in ("supersede", "abandon"):
            with self.subTest(command=command), H.scenario(message=P.CONTENT_FILTER) as h:
                h.cli()
                before = stop_record(h)
                occupied = S.SS._occupies_live_resources(h.unit, h.us())
                self.assertFalse(occupied)
                self.assert_report(stop(h, command), h)
                self.assertEqual(h.us()["terminal_decision"]["retains_live_resources"], occupied)
                self.assertFalse(S.SS._occupies_live_resources(h.unit, h.us()))
                self.assert_report(h.cli(), h)
                self.assert_report(h.cli("status"), h)
                # A cleanup request even after disposition must hit the guard.
                state = S.load_state(h.sd)
                S.SW._archive_code_worktree(state, h.unit, h.attempt, [], h.sd)
                self.assertEqual(stop_record(h), before)
                self.assertTrue(h.work.exists())
                self.assertNotIn("archived", h.us()["attempt_workspaces"][h.attempt.name])

    def test_plan_removal_and_cleanup_pending(self):
        for historical in (False, True):
            with self.subTest(historical=historical), H.scenario(message=P.CONTENT_FILTER) as h:
                h.cli()
                record = stop_record(h)
                h.unit.pop("provider_resume")
                h.change_plan()
                state = S.load_state(h.sd)
                us = state["units"]["code"]
                us["attempt_workspaces"][h.attempt.name]["cleanup_pending"] = True
                if historical:
                    us["attempt_dir"] = None
                S.save_state(h.sd, state)
                meta = copy.deepcopy(us["attempt_workspaces"])
                self.assert_report(h.cli(), h)
                self.assertTrue(h.work.exists())
                self.assertEqual(h.us()["attempt_workspaces"], meta)
                self.assertEqual(stop_record(h), record)
                before = (h.sd / S.STATE_FILE).read_bytes()
                report = []
                S.SW._report_would_archive_code_worktree(state, h.unit, h.attempt, report)
                self.assert_report("\n".join(report), h)
                self.assertNotIn("would preserve and clean", "\n".join(report))
                self.assertEqual((h.sd / S.STATE_FILE).read_bytes(), before)

    def test_disabled_units_have_no_stop_record(self):
        for policy in (None, {"max": 0}):
            with self.subTest(policy=policy), H.scenario(message=P.CONTENT_FILTER) as h:
                h.unit.pop("provider_resume")
                if policy is not None:
                    h.unit["provider_resume"] = policy
                h.change_plan()
                h.cli()
                h.failed()
                self.assertNotIn("provider_resumes", h.us())
                self.assertNotIn("attempt_provider_stops", h.us())

    def test_missing_or_inconsistent_worktree_is_retained(self):
        for condition in ("missing", "branch"):
            with self.subTest(condition=condition), H.scenario() as h:
                state = S.load_state(h.sd)
                us = state["units"]["code"]
                if condition == "missing":
                    h.work.rename(h.work.with_name(h.work.name + "-moved"))
                else:
                    H.F.git(h.work, "checkout", "--detach")
                report = []
                P.record_stop(h.sd, state, "code", us, "content_filter", P.CONTENT_FILTER, report)
                record = stop_record(h)
                self.assertEqual(record["phase"], "retained")
                self.assertIsNone(record["head_at_retention"])
                self.assertTrue(record["retention_problem"])
                self.assertIn("reconciliation needed", "\n".join(report))
                self.assert_report(h.cli(), h)
                self.assert_report(h.cli("status", expected=2), h)
                self.assertNotIn("archived", h.us()["attempt_workspaces"][h.attempt.name])

    def test_retention_git_uses_stripped_environment(self):
        with H.scenario() as h:
            state = S.load_state(h.sd)
            popen = S.U.subprocess.Popen
            calls = []
            def observe(argv, **kw):
                if argv[0] == "git":
                    calls.append(argv)
                    self.assertNotIn("OPENAI_API_KEY", kw["env"])
                    self.assertNotIn("SSH_AUTH_SOCK", kw["env"])
                return popen(argv, **kw)
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic", "SSH_AUTH_SOCK": "synthetic"}), \
                    mock.patch.object(S.U.subprocess, "Popen", side_effect=observe):
                P.record_stop(h.sd, state, "code", state["units"]["code"], "content_filter", P.CONTENT_FILTER, [])
            self.assertTrue(any("HEAD^{commit}" in argv for argv in calls))
            self.assertEqual(stop_record(h)["head_at_retention"], H.F.git(h.work, "rev-parse", "HEAD"))

    def test_gate_refuses_protocol_and_permission_problems(self):
        for cfg in ({"permissions": ["pending"]}, {"prefix": "[User] "}):
            with self.subTest(cfg=cfg), H.scenario(message=P.CONTENT_FILTER, **cfg) as h:
                h.cli()
                h.sent(0)
                self.assertNotIn("attempt_provider_stops", h.us())
        with H.scenario() as h:
            state = S.load_state(h.sd)
            with mock.patch.object(S.SE, "_check", return_value=(S.FAILED, "", "", "invalid")), \
                    mock.patch.object(S.SPRS, "maybe_resume", side_effect=AssertionError("protocol bypass")):
                S.advance(h.plan, state, str(h.sd), str(h.tmp / "runs"), False, max_new=0)
            self.assertNotIn("attempt_provider_stops", state["units"]["code"])


if __name__ == "__main__":
    unittest.main()
