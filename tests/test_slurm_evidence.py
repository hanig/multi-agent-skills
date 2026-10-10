"""Controller-first judging, bounded accounting, and batch exit attestations."""
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from scheduler_fixture import isolated_module_state_home as setUpModule  # noqa: E402
from scheduler_fixture import cleanup_module_path as tearDownModule  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "hanig-swarm" / "scripts"))
import swarm as S
import unit as U

from tests.scheduler_fixture import closed_bin


def _bash_major(path):
    """Major version of the bash at path, or 0. A clean environment keeps
    BASH_ENV or other startup output out of the answer, and only the last
    line is read."""
    try:
        done = subprocess.run([path, "--noprofile", "--norc", "-c",
                               'builtin echo "${BASH_VERSINFO[0]}"'],
                              capture_output=True, text=True, timeout=10,
                              env={"PATH": os.defpath})
        lines = done.stdout.strip().splitlines()
        return int(lines[-1]) if lines else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _find_bash(minimum):
    """The first bash at least `minimum` major, by absolute path, or None.

    The Slurm wrapper runs under the cluster's /bin/bash (5.x); its CHLD probe
    needs bash 4+. macOS /bin/bash is 3.2, so tests that execute the wrapper
    look for a newer bash and skip, saying so, when there is none.
    """
    seen = []
    for candidate in (os.environ.get("HANIG_TEST_BASH"), "/bin/bash",
                      "/opt/homebrew/bin/bash", "/usr/local/bin/bash",
                      "/usr/bin/bash", shutil.which("bash")):
        if not candidate:
            continue
        candidate = os.path.abspath(candidate)  # tests run with other cwds
        if candidate not in seen and os.access(candidate, os.X_OK):
            seen.append(candidate)
            if _bash_major(candidate) >= minimum:
                return candidate
    return None


BASH = _find_bash(4)
assert BASH is None or os.path.isabs(BASH)
OLD_BASH = "/bin/bash" if 0 < _bash_major("/bin/bash") < 4 else None


class SlurmEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.attempt = self.root / "attempt with spaces"
        self.attempt.mkdir()
        self.submitted = U.now_iso()
        self.spec = {"kind": "slurm", "job_id": "42", "task_id": "unit",
                     "attempt_id": self.attempt.name,
                     "created_at": self.submitted, "bound_at": self.submitted,
                     "declared_outputs": ["output"]}
        self.state = {"schema_version": 1, "halted": None, "units": {}}
        self.plan_unit = {"id": "unit", "kind": "slurm", "runtime": "none",
                          "outputs": ["output"], "command": "touch output"}
        self.basis = S._capture_artifact_basis(
            self.state, "unit", self.attempt, self.plan_unit, enable_slurm_exit=True)
        (self.attempt / "output").write_text("produced")
        self.responses = {"scontrol": (1, "", "Invalid job id specified"),
                          "sacct": (0, "", ""), "squeue": (0, "", "")}
        self.calls = []
        patch = mock.patch.object(U, "SACCT_UNAVAILABLE", False)
        patch.start()
        self.addCleanup(patch.stop)
        descriptor = mock.patch.object(U, "SACCT_STATUS_FD", None)
        descriptor.start()
        self.addCleanup(descriptor.stop)

    def controller(self, state="COMPLETED", code="0:0", submit=None,
                   multiline=False, name="analysis", user="test", group="test",
                   label="N/A", account="test", qos="normal", command="/run/job"):
        lines = ["JobId=42 JobName=" + name,
                 "UserId=" + user + "(1) GroupId=" + group + "(1) MCS_label=" + label,
                 "Priority=1 Nice=0 Account=" + account + " QOS=" + qos,
                 "JobState=" + state + " Reason=None Dependency=(null)",
                 "Requeue=1 Restarts=0 BatchFlag=1 Reboot=0 ExitCode=" + code,
                 "SubmitTime=" + (submit or self.submitted) + " EligibleTime=Unknown",
                 "StartTime=Unknown EndTime=" + U.now_iso() + " Deadline=N/A",
                 "Command=" + command, "WorkDir=/run"]
        return (0, ("\n   " if multiline else " ").join(lines), "")

    def accounting(self, state="COMPLETED", code="0:0", submit=None):
        return state + "|" + code + "|" + (submit or self.submitted) + "|" + U.now_iso()

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "scontrol" and "-o" not in argv:
            return self.responses.get("scontrol_multiline", (1, "", "refinement unavailable"))
        return self.responses[argv[0]]

    def judge(self, basis=True):
        self.notes = []
        with mock.patch.object(U.shutil, "which", side_effect=lambda name: name), \
                mock.patch.object(U, "run", side_effect=self.runner):
            return U.check_unit(self.attempt, self.spec, self.notes,
                                artifact_basis=self.basis if basis else None)

    def exit_record(self, **changes):
        record = {"job_id": "42", "exit_status": 0, "end_time": U.now_iso()}
        record.update(changes)
        (self.attempt / U.SLURM_EXIT_RECORD).write_text(json.dumps(record))

    def test_controller_free_text_does_not_override_scheduler_fields(self):
        for field in ("name", "user", "group", "label", "account", "qos", "command"):
            with self.subTest(field=field):
                metadata = {field: "study Command=notes JobState=notes ExitCode=9:0 SubmitTime=notes"}
                self.responses["scontrol"] = self.controller(**metadata)
                self.responses["scontrol_multiline"] = self.controller(multiline=True, **metadata)
                self.calls.clear()
                self.assertEqual(self.judge(), "DONE")
                self.assertEqual(self.spec["slurm_evidence_source"], "slurmctld")
                self.assertEqual([call[0] for call in self.calls], [
                    ["scontrol", "show", "job", "-o", "42"],
                    ["scontrol", "show", "job", "42"]])

    def test_controller_success_does_not_query_accounting(self):
        self.responses["scontrol"] = self.controller()
        self.responses["sacct"] = (127, "", "timed out")
        self.assertEqual(self.judge(), "DONE")
        self.assertEqual(self.spec["slurm_evidence_source"], "slurmctld")
        self.assertEqual([call[0][0] for call in self.calls], ["scontrol"])
        self.assertEqual(self.calls[0][0], ["scontrol", "show", "job", "-o", "42"])

    def test_duplicate_controller_identity_does_not_unlock_fallback(self):
        response = self.controller()[1].replace("JobId=42", "JobId=42 JobId=42")
        self.responses["scontrol"] = (0, response, "")
        self.responses["sacct"] = (0, self.accounting(), "")
        self.assertEqual(self.judge(), "INCOMPLETE")
        self.assertEqual([call[0][0] for call in self.calls], ["scontrol", "scontrol"])

    def test_controller_identity_labels_can_contain_whitespace(self):
        for user, group, label in (("alice", "domain users", "N/A"),
                                   ("domain user", "users", "N/A"),
                                   ("alice", "users", "project label"),
                                   ("alice", "users", "research Priority=7 note"),
                                   ("domain (lab) user", "research (1) team", "N/A")):
            with self.subTest(user=user, group=group, label=label):
                self.responses["scontrol"] = self.controller(user=user, group=group, label=label,
                                                            account="project account", qos="normal queue")
                self.calls.clear()
                self.assertEqual(self.judge(), "DONE")
                self.assertEqual(self.spec["slurm_evidence_source"], "slurmctld")
                self.assertEqual([call[0][0] for call in self.calls], ["scontrol"])

    def test_raw_metadata_cannot_supply_scheduler_identity(self):
        self.spec["job_id"] = "99_1"
        for name in ("ArrayJobId=99 ArrayTaskId=1", "JobId=99 ArrayJobId=99 ArrayTaskId=1"):
            with self.subTest(name=name):
                self.responses["scontrol"] = self.controller(name=name)
                self.calls.clear()
                self.assertEqual(self.judge(), "INCOMPLETE")
                self.assertEqual([call[0][0] for call in self.calls], ["scontrol"])

    def test_refinement_uses_only_the_new_controller_snapshot(self):
        self.responses["scontrol"] = self.controller(name="study JobState=notes")
        self.responses["sacct"] = (0, self.accounting(), "")
        for response, expected in ((self.controller("RUNNING", multiline=True), "RUNNING"),
                                   (self.controller(code="7:0", multiline=True), "FAILED"),
                                   (self.controller(submit="2099-01-01T00:00:00Z", multiline=True), "INCOMPLETE"),
                                   ((0, self.controller(multiline=True)[1].replace("JobId=42", "JobId=43"), ""), "INCOMPLETE")):
            with self.subTest(response=response):
                self.responses["scontrol_multiline"] = response
                self.calls.clear()
                self.assertEqual(self.judge(), expected)
                self.assertEqual([call[0][0] for call in self.calls], ["scontrol", "scontrol"])

    def test_unresolved_refinement_never_unlocks_accounting(self):
        self.responses["scontrol"] = self.controller(name="study JobState=notes")
        self.responses["sacct"] = (0, self.accounting(), "")
        self.exit_record()
        for response in ((1, "", "connection refused"), (1, "", "Invalid job id specified"),
                         (0, "", ""), (0, self.controller(multiline=True)[1].replace("ExitCode=0:0", "NoExit=0:0"), "")):
            with self.subTest(response=response):
                self.responses["scontrol_multiline"] = response
                self.calls.clear()
                self.assertEqual(self.judge(), "INCOMPLETE")
                self.assertEqual([call[0][0] for call in self.calls], ["scontrol", "scontrol"])

    def test_only_native_newlines_split_controller_groups(self):
        self.real_tools()
        for separator in ("\r", "\v", "\f", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                command = "/run/job" + separator + "JobState=notes"
                inline = self.controller(command=command)[1]
                grouped = self.controller(multiline=True, command=command)[1]
                self.stub("scontrol", "import sys; sys.stdout.buffer.write(("
                          + repr(inline) + " if '-o' in sys.argv else "
                          + repr(grouped) + ").encode('utf-8'))")
                self.assertEqual(U.check_unit(self.attempt, self.spec, [],
                                             artifact_basis=self.basis), "DONE")
                self.assertEqual(self.spec["slurm_evidence_source"], "slurmctld")

    def test_multiline_injected_evidence_groups_are_not_filtered_away(self):
        self.responses["scontrol"] = self.controller(name="study JobState=notes")
        for injected in ("JobId=42 JobName=other", "JobState=", "JobState=RUNNING",
                         "Requeue=1 ExitCode=0:0", "SubmitTime=Unknown", "StartTime=Unknown EndTime=Unknown"):
            with self.subTest(injected=injected):
                self.responses["scontrol_multiline"] = self.controller(
                    multiline=True, command="/run/job\n   " + injected)
                self.assertEqual(self.judge(), "INCOMPLETE")

    def test_controller_uses_existing_state_and_exit_rules(self):
        cases = [("COMPLETED", "0:0", "DONE"),
                 ("COMPLETED", "1:0", "FAILED"),
                 ("COMPLETED", "0:9", "FAILED"),
                 ("COMPLETED", "", "FAILED"),
                 ("CANCELLED", "0:0", "FAILED"),
                 ("SPECIAL_EXIT", "0:0", "FAILED"),
                 ("RUNNING", "0:0", "RUNNING"),
                 ("STAGE_OUT", "0:0", "RUNNING"),
                 ("REQUEUED", "0:0", "PREEMPTED"),
                 ("PREEMPTED", "0:0", "PREEMPTED")]
        self.exit_record()
        self.responses["sacct"] = (0, self.accounting(), "")
        for state, code, expected in cases:
            with self.subTest(state=state, code=code):
                self.responses["scontrol"] = self.controller(state, code)
                self.assertEqual(self.judge(), expected)
                self.assertEqual(self.spec["slurm_evidence_source"], "slurmctld")

    def test_both_bounds_reject_job_id_reuse(self):
        for submitted in ("2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"):
            with self.subTest(submitted=submitted):
                self.responses["scontrol"] = self.controller(submit=submitted)
                self.responses["sacct"] = (0, self.accounting(submit=submitted), "")
                self.assertEqual(self.judge(), "INCOMPLETE")
                self.assertIsNone(self.spec["slurm_evidence_source"])

    def test_reset_submit_time_cannot_unlock_stale_accounting_success(self):
        self.responses["scontrol"] = self.controller(submit="2099-01-01T00:00:00Z")
        self.responses["sacct"] = (0, self.accounting(), "")
        self.assertEqual(self.judge(), "INCOMPLETE")
        self.assertEqual([call[0][0] for call in self.calls], ["scontrol"])

    def test_controller_purge_falls_back_to_last_owned_accounting_row(self):
        self.responses["sacct"] = (0, "\n".join([
            self.accounting("PREEMPTED"), self.accounting(),
            self.accounting("FAILED", "7:0", "2099-01-01T00:00:00Z")]), "")
        self.assertEqual(self.judge(), "DONE")
        self.assertEqual(self.spec["slurm_evidence_source"], "sacct")

    def test_controller_accepts_array_task_identity_without_widening_ownership(self):
        self.spec["job_id"] = "42_1"
        response = self.controller()[1].replace("JobId=42", "JobId=43 ArrayJobId=42 ArrayTaskId=1")
        self.responses["scontrol"] = (0, response, "")
        self.assertEqual(self.judge(), "DONE")
        self.assertEqual(self.spec["slurm_evidence_source"], "slurmctld")
        for altered in (response.replace("ArrayTaskId=1", "ArrayTaskId=2"),
                        response.replace("ArrayJobId=42", "ArrayJobId=99"),
                        response.replace(self.submitted, "2099-01-01T00:00:00Z"),
                        response.replace(" JobName=", " ArrayTaskId=1 JobName=")):
            with self.subTest(response=altered):
                self.responses["scontrol"] = (0, altered, "")
                self.assertEqual(self.judge(), "INCOMPLETE")

    def test_repeated_checks_replace_audit_fields_with_current_evidence(self):
        self.basis.pop("slurm_exit_record")
        self.responses["scontrol"] = self.controller()
        for _ in range(2):
            self.assertEqual(self.judge(), "DONE")
            self.assertEqual(self.spec["slurm_evidence_source"], "slurmctld")
            self.assertTrue(self.spec["artifact_fingerprints"])
        self.responses["scontrol"] = (1, "", "Invalid job id specified")
        self.responses["sacct"] = (0, self.accounting(), "")
        self.assertEqual(self.judge(), "DONE")
        self.assertEqual(self.spec["slurm_evidence_source"], "sacct")

    def test_empty_controller_message_variants_allow_fallback(self):
        self.responses["sacct"] = (0, self.accounting(), "")
        for message in ("No jobs in the system", "No jobs in the system.", ""):
            with self.subTest(message=message):
                self.responses["scontrol"] = (0, message, "")
                self.assertEqual(self.judge(), "DONE")

    def test_exit_record_is_not_a_declared_payload_output(self):
        self.spec["declared_outputs"] = [U.SLURM_EXIT_RECORD]
        self.basis = S._capture_artifact_basis({}, "unit", self.attempt,
                                               {"kind": "slurm", "outputs": [U.SLURM_EXIT_RECORD]},
                                               enable_slurm_exit=True)
        self.responses["scontrol"] = self.controller()
        self.exit_record()
        self.assertEqual(self.judge(), "INCOMPLETE")

    def test_empty_output_keeps_existing_validation_diagnostic(self):
        for output in ("", "   "):
            with self.subTest(output=output):
                plan_unit = dict(self.plan_unit, outputs=[output], write_scopes=["unit/"])
                with self.assertRaisesRegex(S.PlanError, "empty output"):
                    S.validate_plan({"name": "p", "units": [plan_unit]})

    def test_reserved_paths_are_refused_before_allocation_and_dispatch(self):
        for output in (U.SLURM_EXIT_RECORD, "./" + U.SLURM_EXIT_RECORD,
                       U.SLURM_EXIT_RECORD + ".tmp.42", "."):
            with self.subTest(output=output):
                plan_unit = dict(self.plan_unit, outputs=[output], write_scopes=["unit/"])
                with self.assertRaisesRegex(S.PlanError, "reserved"):
                    S.validate_plan({"name": "p", "units": [plan_unit]})
                allocated = subprocess.run(
                    [sys.executable, str(ROOT / "skills/hanig-swarm/scripts/unit.py"),
                     "allocate", "--root", str(self.root / "runs"), "--task", "unit",
                     "--kind", "slurm", "--output", output],
                    capture_output=True, text=True, timeout=10)
                self.assertNotEqual(allocated.returncode, 0)
                self.assertIn("reserved", allocated.stderr)
                self.assertFalse((self.root / "runs" / "unit").exists())
                with mock.patch.object(U, "run") as runner:
                    job, problem = S._submit(plan_unit, self.attempt, False,
                                             state=self.state, state_dir=self.root / "state")
                self.assertIsNone(job)
                self.assertIn("reserved", problem)
                runner.assert_not_called()

    def test_root_and_symlink_output_cannot_launder_wrapper_evidence(self):
        self.responses["scontrol"] = self.controller()
        (self.attempt / "alias").symlink_to(U.SLURM_EXIT_RECORD)
        for output in ("alias", "."):
            with self.subTest(output=output):
                record = self.attempt / U.SLURM_EXIT_RECORD
                if record.exists():
                    record.unlink()
                self.spec["declared_outputs"] = [output]
                self.basis = S._capture_artifact_basis({}, "unit", self.attempt,
                                                       {"kind": "slurm", "outputs": [output]},
                                                       enable_slurm_exit=True)
                self.exit_record()
                self.assertEqual(self.judge(), "INCOMPLETE")
                self.assertIn("reserved", " ".join(self.notes))

    def test_nested_payload_with_same_basename_is_not_reserved(self):
        output = "results/" + U.SLURM_EXIT_RECORD
        self.spec["declared_outputs"] = [output]
        self.basis = S._capture_artifact_basis({}, "unit", self.attempt,
                                               {"kind": "slurm", "outputs": [output]},
                                               enable_slurm_exit=True)
        (self.attempt / "results").mkdir()
        (self.attempt / output).write_text("payload data")
        self.responses["scontrol"] = self.controller()
        self.assertEqual(self.judge(), "DONE")

    def test_reserved_literal_is_refused_even_when_symlink_target_is_payload(self):
        (self.attempt / U.SLURM_EXIT_RECORD).symlink_to("output")
        plan_unit = dict(self.plan_unit, outputs=[U.SLURM_EXIT_RECORD])
        with mock.patch.object(U, "run") as runner:
            job, problem = S._submit(plan_unit, self.attempt, False,
                                     state=self.state, state_dir=self.root / "state")
        self.assertIsNone(job)
        self.assertIn("reserved", problem)
        runner.assert_not_called()

    def test_unknown_controller_response_is_not_purge(self):
        self.exit_record()
        self.responses["sacct"] = (0, self.accounting(), "")
        for response in ((1, "", "connection refused"),
                         self.controller(submit="Unknown"),
                         (0, self.controller()[1] + " JobState=RUNNING", ""),
                         (0, "JobId=43 JobState=COMPLETED", ""),
                         (0, "JobId=42 SubmitTime=" + self.submitted, "")):
            with self.subTest(response=response):
                self.responses["scontrol"] = response
                self.calls.clear()
                self.assertEqual(self.judge(), "INCOMPLETE")
                self.assertTrue(self.calls)
                self.assertTrue(all(call[0][0] == "scontrol" for call in self.calls))

    def test_accounting_timeout_and_connection_failure_disable_later_queries(self):
        for message in ("timed out after 20s", "persist_init: connection refused"):
            with self.subTest(message=message):
                U.SACCT_UNAVAILABLE = False
                self.calls.clear()
                self.responses["sacct"] = (127, "", message)
                self.assertEqual(self.judge(), "INCOMPLETE")
                self.assertEqual(self.judge(), "INCOMPLETE")
                accounting = [call for call in self.calls if call[0][0] == "sacct"]
                self.assertEqual(len(accounting), 1)
                self.assertEqual(accounting[0][1]["timeout"], 20)

    def test_exit_record_success_and_failure(self):
        for status, expected in ((0, "DONE"), (1, "FAILED"), (137, "FAILED")):
            with self.subTest(status=status):
                self.exit_record(exit_status=status)
                self.assertEqual(self.judge(), expected)
                self.assertEqual(self.spec["slurm_evidence_source"], "exit-record")

    def test_exit_record_can_survive_accounting_outage(self):
        self.responses["sacct"] = (127, "", "timed out after 20s")
        self.exit_record()
        self.assertEqual(self.judge(), "DONE")
        self.assertTrue(U.SACCT_UNAVAILABLE)

    def test_bad_exit_records_are_absent_not_failure(self):
        for changes in ({"job_id": "43"}, {"job_id": 42},
                        {"exit_status": "0"}, {"exit_status": False},
                        {"exit_status": -1}, {"exit_status": 256},
                        {"end_time": "Unknown"},
                        {"end_time": "2000-01-01T00:00:00Z"}):
            with self.subTest(changes=changes):
                self.exit_record(**changes)
                self.assertEqual(self.judge(), "INCOMPLETE")
        for content in ("{", "[]", "null"):
            (self.attempt / U.SLURM_EXIT_RECORD).write_text(content)
            self.assertEqual(self.judge(), "INCOMPLETE")

    def test_exit_record_requires_positive_queue_absence(self):
        self.exit_record()
        for response in ((0, "42", ""), (1, "", "unreachable")):
            with self.subTest(response=response):
                self.responses["squeue"] = response
                self.assertEqual(self.judge(), "INCOMPLETE")

    def test_purged_job_error_from_squeue_is_positive_absence(self):
        self.responses["squeue"] = (1, "", "slurm_load_jobs error: Invalid job id specified")
        self.exit_record()
        self.assertEqual(self.judge(), "DONE")
        self.assertEqual(self.spec["slurm_evidence_source"], "exit-record")
        queue = [call[0] for call in self.calls if call[0][0] == "squeue"]
        self.assertEqual(queue, [["squeue", "-h", "--jobs=42", "--states=all", "-o", "%i"]])
        self.exit_record(exit_status=9)
        self.assertEqual(self.judge(), "FAILED")

    def test_accounting_requeue_or_failure_overrides_old_exit_record(self):
        self.exit_record()
        for state, expected in (("REQUEUED", "PREEMPTED"), ("FAILED", "FAILED"),
                                ("PENDING", "RUNNING")):
            self.responses["sacct"] = (0, self.accounting(state, "1:0"), "")
            self.assertEqual(self.judge(), expected)
            self.assertEqual(self.spec["slurm_evidence_source"], "sacct")

    def test_unowned_accounting_rows_do_not_veto_authorized_exit_records(self):
        self.responses["sacct"] = (0, self.accounting(submit="2000-01-01T00:00:00Z"), "")
        for status, expected in ((0, "DONE"), (7, "FAILED")):
            self.exit_record(exit_status=status)
            self.assertEqual(self.judge(), expected)
            self.assertEqual(self.spec["slurm_evidence_source"], "exit-record")

    def test_owned_malformed_accounting_row_blocks_exit_record(self):
        self.exit_record()
        self.responses["sacct"] = (0, self.accounting(state=""), "")
        self.assertEqual(self.judge(), "INCOMPLETE")
        self.assertNotIn("squeue", [call[0][0] for call in self.calls])

    def test_owned_selection_output_resets_on_absent_accounting(self):
        for initial in ([], [True]):
            for available in (False, True):
                with self.subTest(initial=initial, available=available):
                    selected = list(initial)
                    with mock.patch.object(U.shutil, "which", return_value="sacct" if available else None), \
                            mock.patch.object(U, "run_sacct", return_value=(0, "", "")):
                        self.assertEqual(U.sacct_state("42", owned_out=selected), (None, None, None, None))
                    self.assertEqual(selected, [False])

    def test_legacy_attempt_neither_reserves_nor_interprets_record_name(self):
        self.basis.pop("slurm_exit_record")
        self.spec["slurm_exit_record"] = {"version": 1, "path": U.SLURM_EXIT_RECORD}
        for status in (0, 7):
            self.exit_record(exit_status=status)
            self.assertEqual(self.judge(), "INCOMPLETE")
        self.spec["declared_outputs"] = [U.SLURM_EXIT_RECORD]
        self.basis["declared"] = self.basis["absent"] = [U.SLURM_EXIT_RECORD]
        self.responses["scontrol"] = self.controller()
        self.assertEqual(self.judge(), "DONE")
        self.assertEqual(self.spec["slurm_evidence_source"], "slurmctld")

    def test_invalid_capability_or_basis_cannot_authorize_either_exit_outcome(self):
        policies = (None, True, {"version": True, "path": U.SLURM_EXIT_RECORD},
                    {"version": 2, "path": U.SLURM_EXIT_RECORD},
                    {"version": 1, "path": "other.json"})
        for policy in policies:
            with self.subTest(policy=policy), mock.patch.dict(self.basis, slurm_exit_record=policy):
                for status in (0, 7):
                    self.spec["artifact_fingerprints"] = {"forged": {"sha256": "fake"}}
                    self.exit_record(exit_status=status)
                    self.assertEqual(self.judge(), "INCOMPLETE")
                    self.assertEqual(self.spec["artifact_fingerprints"], {})
        for field in ("unit_id", "attempt_id"):
            with self.subTest(field=field), mock.patch.dict(self.basis, {field: "other"}):
                for status in (0, 7):
                    self.exit_record(exit_status=status)
                    self.assertEqual(self.judge(), "INCOMPLETE")

    def test_capability_is_persisted_but_never_backfilled(self):
        state_dir = self.root / "state"
        S.save_state(str(state_dir), self.state)
        loaded = S.load_state(str(state_dir))
        self.assertEqual(S.raw_artifact_basis(loaded, "unit", self.attempt), self.basis)
        legacy = S._capture_artifact_basis({}, "unit", self.attempt, self.plan_unit)
        self.assertNotIn("slurm_exit_record", legacy)
        prior = {"units": {"unit": {"attempt_artifact_bases": {self.attempt.name: legacy}}}}
        self.assertIs(S._capture_artifact_basis(prior, "unit", self.attempt,
                                               self.plan_unit, enable_slurm_exit=True), legacy)
        self.assertNotIn("slurm_exit_record", legacy)

    def test_rejected_capability_is_not_filtered_to_legacy_before_dispatch(self):
        self.basis["unit_id"] = "wrong"
        self.assertIsNone(S.trusted_artifact_basis(self.state, "unit", self.attempt))
        self.assertIs(S.raw_artifact_basis(self.state, "unit", self.attempt), self.basis)
        with mock.patch.object(U, "run", return_value=(0, "42", "")) as runner:
            job, problem = S._submit(self.plan_unit, self.attempt, False,
                                     state=self.state, state_dir=self.root / "state")
        self.assertIsNone(job)
        self.assertTrue(problem)
        runner.assert_not_called()

    def test_allocation_checks_parent_alias_before_mkdir(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "skills/hanig-swarm/scripts/unit.py"),
             "allocate", "--root", str(self.root / "runs"), "--task", "unit",
             "--kind", "slurm", "--output", "foo/../" + U.SLURM_EXIT_RECORD],
            capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reserved", result.stderr)
        self.assertFalse((self.root / "runs" / "unit").exists())

    def test_accounting_configuration_does_not_retain_mock_descriptors(self):
        U.configure_sacct(mock.Mock())
        self.assertIs(U.SACCT_UNAVAILABLE, False)
        self.assertIsNone(U.SACCT_STATUS_FD)

    def test_success_still_requires_outputs_and_pinned_artifact_basis(self):
        for source in ("slurmctld", "sacct", "exit-record"):
            with self.subTest(source=source):
                self.responses["scontrol"] = (self.controller() if source == "slurmctld"
                                              else (1, "", "Invalid job id specified"))
                self.responses["sacct"] = (0, self.accounting() if source == "sacct" else "", "")
                self.exit_record()
                self.assertEqual(self.judge(basis=False), "INCOMPLETE")
                unchanged = dict(self.basis, absent=[], present=U.fingerprint_outputs(
                    self.attempt, ["output"]))
                with mock.patch.object(self, "basis", unchanged):
                    self.assertEqual(self.judge(), "INCOMPLETE")
                (self.attempt / "output").unlink()
                self.assertEqual(self.judge(), "INCOMPLETE")
                (self.attempt / "output").write_text("produced")

    def stub(self, name, program):
        path = self.bin / name
        if path.is_symlink():
            path.unlink()
        path.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable)
                        + " -c " + shlex.quote(program) + ' "$@"\n')
        path.chmod(0o755)

    def bash(self):
        """The bash 4+ that executes the wrapper, or skip this test."""
        if BASH is None:
            self.skipTest("no bash 4+ on this host; the Slurm wrapper needs it "
                          "(clusters run 5.x); test_old_bash_opts_out covers 3.2")
        return BASH

    def real_tools(self):
        self.bin = self.root / "bin"
        patch = mock.patch.dict(os.environ, {"PATH": closed_bin(self.bin)})
        patch.start()
        self.addCleanup(patch.stop)
        (self.attempt / U.UNIT).write_text(json.dumps(self.spec))
        self.stub("scontrol", "import sys; sys.exit('Invalid job id specified')")
        self.stub("squeue", "pass")

    def cli_advance(self, max_new=0, retry=False):
        state_dir = self.root / "state"
        self.state.setdefault("units", {}).setdefault("unit", {}).update({
            "state": "SUBMITTED", "job_id": "42", "attempt_dir": str(self.attempt),
            "attempts": [str(self.attempt)], "gpu_hours": 0})
        S.save_state(str(state_dir), self.state)
        (self.attempt / U.UNIT).write_text(json.dumps(self.spec))
        plan_unit = dict(self.plan_unit, outputs=self.spec["declared_outputs"], write_scopes=["unit/"])
        plan = {"name": "p", "units": [plan_unit]}
        if retry:
            plan["retry_limits"] = {"items": 0}
            plan_unit.update(max_attempts=2, retry={"mode": "restart", "max_lost": {"items": 0}})
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(plan))
        result = subprocess.run(
            [sys.executable, str(ROOT / "skills/hanig-swarm/scripts/swarm.py"),
             "advance", str(plan_path), "--state-dir", str(state_dir),
             "--root", str(self.root / "runs"), "--max-new-dispatches", str(max_new)],
            cwd=str(self.root), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result, S.load_state(str(state_dir))

    def test_cli_advance_preserves_legacy_payload_namespace(self):
        self.real_tools()
        self.basis.pop("slurm_exit_record")
        self.spec["declared_outputs"] = [U.SLURM_EXIT_RECORD]
        self.basis["declared"] = self.basis["absent"] = [U.SLURM_EXIT_RECORD]
        (self.attempt / U.SLURM_EXIT_RECORD).write_text("legacy payload")
        self.stub("scontrol", "print(%r)" % self.controller()[1])
        _, state = self.cli_advance()
        self.assertEqual(state["units"]["unit"]["state"], "DONE")
        self.assertNotIn("slurm_exit_record", S.raw_artifact_basis(state, "unit", self.attempt))

    def test_cli_legacy_retry_still_refuses_new_reserved_output(self):
        self.real_tools()
        self.basis.pop("slurm_exit_record")
        self.spec["declared_outputs"] = [U.SLURM_EXIT_RECORD]
        self.basis["declared"] = self.basis["absent"] = [U.SLURM_EXIT_RECORD]
        (self.attempt / U.SLURM_EXIT_RECORD).write_text("legacy payload")
        self.stub("scontrol", "print(%r)" % self.controller("PREEMPTED")[1])
        called = self.root / "unexpected-submit"
        self.stub("sbatch", "from pathlib import Path; Path(%r).touch(); print('43')" % str(called))
        result, state = self.cli_advance(max_new=1, retry=True)
        self.assertIn("reserved", result.stdout)
        self.assertIsNone(state["units"]["unit"]["attempt_dir"])
        self.assertFalse((self.root / "runs" / "unit").exists())
        self.assertFalse(called.exists())
        self.assertEqual((self.attempt / U.SLURM_EXIT_RECORD).read_text(), "legacy payload")

    def test_cli_invalid_capability_is_not_downgraded_before_judging(self):
        self.real_tools()
        self.basis["attempt_id"] = "other"
        self.exit_record(exit_status=7)
        self.stub("sacct", "pass")
        _, state = self.cli_advance()
        self.assertEqual(state["units"]["unit"]["state"], "INCOMPLETE")
        receipt = json.loads((self.attempt / U.RECEIPT).read_text())
        self.assertIn("not", " ".join(receipt["notes"]))
        self.assertIsNone(receipt["basis"]["exit_status_attested_by"])

    def test_fresh_dispatch_pins_capability_before_generating_trap(self):
        self.real_tools()
        self.stub("sbatch", "print('42')")
        state_dir = self.root / "fresh-state"
        state_dir.mkdir()
        acquired, problem = S.acquire_lease(str(state_dir))
        self.assertTrue(acquired, problem)
        self.addCleanup(S.release_lease, str(state_dir))
        state = {"schema_version": 1, "halted": None, "units": {}}
        plan = {"name": "p", "units": [dict(self.plan_unit, write_scopes=["unit/"])]}
        report, dispatched, halted = S.advance(plan, state, str(state_dir),
                                               str(self.root / "fresh-runs"), False, max_new=1)
        self.assertEqual(dispatched, 1, report)
        self.assertIsNone(halted)
        attempt = Path(state["units"]["unit"]["attempt_dir"])
        stored = S.load_state(str(state_dir))
        policy = S.raw_artifact_basis(stored, "unit", attempt)["slurm_exit_record"]
        self.assertEqual(policy, {"version": 1, "path": U.SLURM_EXIT_RECORD})
        result = subprocess.run([self.bash(), str(attempt / "job.sbatch")],
                                env=dict(os.environ, SLURM_JOB_ID="42"), timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((attempt / U.SLURM_EXIT_RECORD).is_file())

    def test_legacy_dispatch_does_not_write_or_remove_payload_record_name(self):
        self.real_tools()
        self.basis.pop("slurm_exit_record")
        self.plan_unit.update(command="true", outputs=[U.SLURM_EXIT_RECORD])
        (self.attempt / U.SLURM_EXIT_RECORD).write_text("legacy payload")
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            job, error = S._submit(self.plan_unit, self.attempt, False,
                                   state=self.state, state_dir=self.root / "state")
        self.assertIsNone(error)
        self.assertEqual(job, "42")
        result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                env=dict(os.environ, SLURM_JOB_ID="42"), timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.attempt / U.SLURM_EXIT_RECORD).read_text(), "legacy payload")

    def test_receipts_name_the_actual_source_through_real_checker(self):
        self.real_tools()
        self.exit_record()
        for source in ("slurmctld", "sacct", "exit-record"):
            with self.subTest(source=source):
                self.stub("scontrol", "print(%r)" % self.controller()[1]
                          if source == "slurmctld" else
                          "import sys; sys.exit('Invalid job id specified')")
                self.stub("sacct", "print(%r)" % self.accounting()
                          if source == "sacct" else "pass")
                result = S._check(self.attempt, artifact_basis=self.basis)
                self.assertEqual(result[0], U.STATES["DONE"], result)
                receipt = json.loads((self.attempt / U.RECEIPT).read_text())
                self.assertEqual(receipt["basis"]["exit_status_attested_by"], source)

    def test_outage_crosses_checker_processes_not_agent_receipts(self):
        self.real_tools()
        calls = self.root / "accounting-calls"
        self.stub("sacct", "from pathlib import Path; import sys; "
                  "path=Path(%r); path.write_text(path.read_text()+'x' if path.exists() else 'x'); "
                  "sys.exit('connection refused')" % str(calls))
        for _ in range(2):
            result = S._check(self.attempt, artifact_basis=self.basis)
            self.assertEqual(result[0], U.STATES["INCOMPLETE"], result)
        self.assertEqual(calls.read_text(), "x")
        self.assertTrue(U.SACCT_UNAVAILABLE)
        U.SACCT_UNAVAILABLE = False
        (self.attempt / U.RECEIPT).write_text('{"sacct_unavailable":true}')
        S._check(self.attempt, artifact_basis=self.basis)
        self.assertEqual(calls.read_text(), "xx")

    def test_real_timeout_kills_accounting_and_skips_next_query(self):
        self.real_tools()
        self.stub("sacct", "import time; time.sleep(60)")
        with mock.patch.object(U, "SACCT_TIMEOUT_S", 0.05):
            first = U.run_sacct(["sacct"])
        self.assertIn("timed out", first[2])
        self.assertTrue(U.SACCT_UNAVAILABLE)
        with mock.patch.object(U, "run", side_effect=AssertionError("retried")):
            self.assertNotEqual(U.run_sacct(["sacct"])[0], 0)

    def test_advance_shares_outage_across_units_and_retries_next_pass(self):
        self.real_tools()
        calls = self.root / "accounting-calls"
        self.stub("sacct", "from pathlib import Path; import sys; "
                  "path=Path(%r); path.write_text(path.read_text()+'x' if path.exists() else 'x'); "
                  "sys.exit('connection refused')" % str(calls))
        units = []
        for name in ("unit", "second"):
            attempt = self.attempt if name == "unit" else self.root / "second-attempt"
            attempt.mkdir(exist_ok=True)
            spec = dict(self.spec, task_id=name, attempt_id=attempt.name)
            (attempt / U.UNIT).write_text(json.dumps(spec))
            plan_unit = dict(self.plan_unit, id=name, write_scopes=[name + "/"])
            S._capture_artifact_basis(self.state, name, attempt, plan_unit)
            self.state["units"][name].update({"state": "SUBMITTED", "job_id": "42",
                "attempt_dir": str(attempt), "attempts": [str(attempt)], "gpu_hours": 0})
            units.append(plan_unit)
        state_dir = self.root / "state"
        state_dir.mkdir()
        acquired, problem = S.acquire_lease(str(state_dir))
        self.assertTrue(acquired, problem)
        self.addCleanup(S.release_lease, str(state_dir))
        for expected in ("x", "xx"):
            S.save_state(str(state_dir), self.state)
            S.advance({"name": "p", "units": units}, self.state, str(state_dir),
                      str(self.root / "runs"), False, max_new=0)
            self.assertEqual(calls.read_text(), expected)
            for name in ("unit", "second"):
                self.assertEqual(self.state["units"][name]["state"], "INCOMPLETE")

    def test_reconciliation_obeys_the_same_accounting_breaker(self):
        U.SACCT_UNAVAILABLE = True
        with mock.patch.object(U, "run", return_value=(0, "", "")) as runner:
            job, evidence = S.reconcile_orphan(self.attempt, kind="slurm")
        self.assertIsNone(job)
        self.assertEqual(evidence, "UNKNOWN")
        self.assertEqual([call.args[0][0] for call in runner.call_args_list], ["squeue"])

    def test_reconciliation_stops_after_live_queue_match(self):
        with mock.patch.object(U, "run", return_value=(0, "42\n", "")) as runner, \
                mock.patch.object(U, "run_sacct") as accounting:
            job, evidence = S.reconcile_orphan(self.attempt, kind="slurm")
        self.assertEqual(job, "42")
        self.assertIn("recovered job", evidence)
        self.assertEqual([call.args[0][0] for call in runner.call_args_list], ["squeue"])
        accounting.assert_not_called()
        self.assertEqual(runner.call_args.kwargs["timeout"], 60)
        self.assertFalse(U.SACCT_UNAVAILABLE)

    def test_generated_batch_script_records_exit_and_clears_requeue_record(self):
        self.real_tools()
        for command, status in (("touch output", 0), ("exit 7", 7),
                                ("false; touch should-not-exist", 1),
                                ("test ! -e slurm-exit.json", 0)):
            with self.subTest(command=command):
                self.plan_unit["command"] = command
                with mock.patch.object(U, "run", return_value=(0, "42", "")):
                    job, error = S._submit(self.plan_unit, self.attempt, False,
                                           state=self.state, state_dir=self.root / "state")
                self.assertIsNone(error)
                self.assertEqual(job, "42")
                self.exit_record(exit_status=99)
                result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                        env=dict(os.environ, SLURM_JOB_ID="42"),
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertTrue((self.attempt / U.SLURM_EXIT_RECORD).is_file())
                record = json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())
                self.assertEqual(record["job_id"], "42")
                self.assertEqual(record["exit_status"], status)
                self.assertIsNotNone(U.parse_iso_ts(record["end_time"]))
                self.assertFalse((self.attempt / "should-not-exist").exists())

    def test_batch_shell_signal_reaches_payload_handler(self):
        self.real_tools()
        self.plan_unit.update(command="trap 'touch warned' USR1; touch ready; sleep .3; test -f warned; touch output",
                              sbatch=["--signal=B:USR1@60"])
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            job, problem = S._submit(self.plan_unit, self.attempt, False,
                                    state=self.state, state_dir=self.root / "state")
        self.assertIsNone(problem)
        with subprocess.Popen([self.bash(), str(self.attempt / "job.sbatch")],
                              env=dict(os.environ, SLURM_JOB_ID="42"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            deadline = time.monotonic() + 5
            while not (self.attempt / "ready").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((self.attempt / "ready").exists())
            os.kill(process.pid, signal.SIGUSR1)
            _, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)
        self.assertTrue((self.attempt / "warned").is_file())
        record = json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())
        self.assertEqual(record["exit_status"], 0)

    def test_fatal_batch_signals_cannot_record_zero_after_partial_output(self):
        self.real_tools()
        self.plan_unit["command"] = "touch output ready; sleep .2; touch should-not-exist"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        fatal_signals = [getattr(signal, name) for name in (
            "SIGHUP", "SIGINT", "SIGTERM", "SIGUSR1", "SIGUSR2", "SIGPIPE",
            "SIGALRM", "SIGXCPU", "SIGXFSZ", "SIGABRT", "SIGILL", "SIGSEGV",
            "SIGBUS", "SIGFPE", "SIGTRAP", "SIGPROF", "SIGVTALRM", "SIGIO",
            "SIGPWR", "SIGSYS", "SIGSTKFLT", "SIGRTMIN", "SIGRTMAX")
            if hasattr(signal, name)]
        cases = [(fatal, False) for fatal in fatal_signals] + [(signal.SIGTERM, True)]
        for fatal, posix in cases:
            with self.subTest(signal=fatal, posix=posix):
                ready = self.attempt / "ready"
                if ready.exists():
                    ready.unlink()
                options = ["--posix"] if posix else []
                with subprocess.Popen([self.bash(), *options, str(self.attempt / "job.sbatch")],
                                      env=dict(os.environ, SLURM_JOB_ID="42"),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists())
                    os.kill(process.pid, fatal)
                    _, error = process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0, error)
                record = self.attempt / U.SLURM_EXIT_RECORD
                if record.exists() and record.stat().st_size:
                    self.assertNotEqual(json.loads(record.read_text())["exit_status"], 0)
                self.assertNotEqual(self.judge(), "DONE")
                self.assertFalse((self.attempt / "should-not-exist").exists())

    def test_signal_during_recorder_cannot_leave_zero_record(self):
        self.real_tools()
        (self.bin / "rm").unlink()
        startup = self.root / "startup"
        startup.write_text("readonly IFS=:\n")
        ready = self.attempt / "recording"
        self.plan_unit["command"] = 'hash -r; test "$IFS" = :; touch output'
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        for phase in ("staging", "publication"):
            for recipient in ("batch", "group", "recorder"):
                with self.subTest(phase=phase, recipient=recipient):
                    if ready.exists():
                        ready.unlink()
                    self.stub("date", "print(%r)" % U.now_iso())
                    self.stub("mv", "import os, sys; os.replace(sys.argv[-2], sys.argv[-1])")
                    if phase == "staging":
                        self.stub("date", "from pathlib import Path; import os, time; "
                                  "Path(%r).write_text(str(os.getppid())); time.sleep(.3); print(%r)"
                                  % (str(ready), U.now_iso()))
                    else:
                        self.stub("mv", "from pathlib import Path; import os, sys, time; "
                                  "os.replace(sys.argv[-2], sys.argv[-1]); "
                                  "Path(%r).write_text(str(os.getpid())); time.sleep(.3)" % str(ready))
                    with subprocess.Popen([self.bash(), str(self.attempt / "job.sbatch")],
                                          env=dict(os.environ, SLURM_JOB_ID="42", BASH_ENV=str(startup)),
                                          start_new_session=True, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE) as process:
                        deadline = time.monotonic() + 5
                        while (not ready.exists() or not ready.read_text()) and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue(ready.exists())
                        if recipient == "group":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            target = process.pid if recipient == "batch" else int(ready.read_text())
                            os.kill(target, signal.SIGTERM)
                        _, error = process.communicate(timeout=5)
                    if recipient != "recorder":
                        self.assertNotEqual(process.returncode, 0, error)
                    else:
                        self.assertEqual(process.returncode, 0, error)
                    record = self.attempt / U.SLURM_EXIT_RECORD
                    if record.exists():
                        self.assertNotEqual(json.loads(record.read_text())["exit_status"], 0)
                    self.assertNotEqual(self.judge(), "DONE")

    def test_payload_interrupt_handler_overrides_default_abort(self):
        self.real_tools()
        self.plan_unit["command"] = "trap 'touch output; exit 0' INT; kill -INT $$; exit 7"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                env=dict(os.environ, SLURM_JOB_ID="42"),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())
        self.assertEqual(record["exit_status"], 0)

    def test_ignored_signals_stay_ignored(self):
        self.real_tools()
        self.plan_unit["command"] = "touch ready; sleep .2; touch output"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        for ignored in (signal.SIGQUIT, signal.SIGUSR1):
            with self.subTest(signal=ignored):
                ready = self.attempt / "ready"
                if ready.exists():
                    ready.unlink()
                with subprocess.Popen([self.bash(), "-c", 'trap "" USR1; exec ' + shlex.quote(self.bash()) + ' "$1"',
                                       "launcher", str(self.attempt / "job.sbatch")],
                                      env=dict(os.environ, SLURM_JOB_ID="42"),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists())
                    os.kill(process.pid, ignored)
                    _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                record = json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())
                self.assertEqual(record["exit_status"], 0)

    def test_shell_or_exit_trap_replacement_leaves_no_invented_record(self):
        self.real_tools()
        for command, status in (("exec false", 1), ("trap ':' EXIT; exit 9", 9)):
            with self.subTest(command=command):
                self.plan_unit["command"] = command
                with mock.patch.object(U, "run", return_value=(0, "42", "")):
                    S._submit(self.plan_unit, self.attempt, False,
                              state=self.state, state_dir=self.root / "state")
                self.exit_record()
                result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                        env=dict(os.environ, SLURM_JOB_ID="42"), timeout=5)
                self.assertEqual(result.returncode, status)
                self.assertFalse((self.attempt / U.SLURM_EXIT_RECORD).exists())
                self.assertEqual(self.judge(), "INCOMPLETE")

    def test_generated_array_record_matches_only_its_bound_task(self):
        self.real_tools()
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                env=dict(os.environ, SLURM_JOB_ID="43",
                                         SLURM_ARRAY_JOB_ID="42", SLURM_ARRAY_TASK_ID="1"), timeout=5)
        self.assertEqual(result.returncode, 0)
        record = json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())
        self.assertEqual(record["job_id"], "43")
        self.assertEqual(record["array_job_id"], "42")
        self.assertEqual(record["array_task_id"], "1")
        for identity, expected in (("43", "DONE"), ("42_1", "DONE"),
                                   ("42_2", "INCOMPLETE"), ("42", "INCOMPLETE")):
            with self.subTest(identity=identity):
                self.spec["job_id"] = identity
                self.assertEqual(self.judge(), expected)

    def test_wrapper_setup_handles_posix_mode_and_restricted_path(self):
        self.real_tools()
        empty_path = self.root / "empty-bin"
        empty_path.mkdir()
        self.plan_unit["command"] = "printf produced > output"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        for mode in ("posix-env", "posix-arg", "posix-helper", "restricted-path", "cleanup-failure"):
            with self.subTest(mode=mode):
                self.exit_record(exit_status=99)
                (self.attempt / "output").unlink(missing_ok=True)
                record_path = self.attempt / U.SLURM_EXIT_RECORD
                if mode == "cleanup-failure":
                    record_path.unlink()
                    record_path.mkdir()
                environment = dict(os.environ, SLURM_JOB_ID="42")
                environment.pop("POSIXLY_CORRECT", None)
                if mode == "posix-env":
                    environment["POSIXLY_CORRECT"] = "1"
                if mode == "restricted-path":
                    environment["PATH"] = str(empty_path)
                options = ["--posix"] if mode == "posix-arg" else []
                script = self.attempt / "job.sbatch"
                if mode == "posix-helper":
                    helper_script = self.attempt / "posix-helper.sbatch"
                    helper_script.write_text(script.read_text().replace("/bin/bash -p -c", "/bin/bash --posix -p -c"))
                    script = helper_script
                result = subprocess.run([self.bash(), *options, str(script)],
                                        env=environment, capture_output=True, text=True, timeout=5)
                if mode == "cleanup-failure":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((self.attempt / "output").exists())
                    self.assertTrue(record_path.is_dir())
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.attempt / "output").is_file(), result.stderr)
                self.assertEqual((self.attempt / "output").read_text(), "produced")
                if mode == "restricted-path":
                    self.assertFalse(record_path.exists())
                elif mode in ("posix-env", "posix-arg"):
                    self.assertEqual(record_path.read_text(), "")
                else:
                    self.assertTrue(record_path.is_file(), result.stderr)
                    self.assertEqual(json.loads(record_path.read_text())["exit_status"], 0)

    def test_native_child_handler_preserves_first_background_pid(self):
        self.real_tools()
        startup = self.root / "startup"
        startup.write_text('trap \'builtin printf "%s\\n" "$!"\' CHLD\n')
        for status in (0, 7):
            with self.subTest(status=status):
                self.plan_unit["command"] = (
                    "/bin/sleep .1 & wait; printf produced > output; exit " + str(status))
                with mock.patch.object(U, "run", return_value=(0, "42", "")):
                    S._submit(self.plan_unit, self.attempt, False,
                              state=self.state, state_dir=self.root / "state")
                if (self.attempt / "output").exists():
                    (self.attempt / "output").unlink()
                self.exit_record()
                result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                        env=dict(os.environ, SLURM_JOB_ID="42", BASH_ENV=str(startup)),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertTrue(result.stdout.strip().isdigit(), result.stdout)
                self.assertTrue((self.attempt / "output").is_file())
                self.assertEqual((self.attempt / "output").read_text(), "produced")
                self.assertEqual((self.attempt / U.SLURM_EXIT_RECORD).read_text(), "")
                self.responses["scontrol"] = (1, "", "Invalid job id specified")
                self.assertEqual(self.judge(), "INCOMPLETE")
                self.responses["scontrol"] = self.controller(
                    state="FAILED" if status else "COMPLETED", code=str(status) + ":0")
                self.assertEqual(self.judge(), "FAILED" if status else "DONE")

    def test_ignored_child_handler_invalidates_stale_record_with_noclobber(self):
        self.real_tools()
        startup = self.root / "startup"
        startup.write_text("set -C\ntrap '' CHLD\n")
        self.plan_unit["command"] = "printf produced > output"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        for prior in (None, "", "stale"):
            with self.subTest(prior=prior):
                record = self.attempt / U.SLURM_EXIT_RECORD
                record.unlink(missing_ok=True)
                if prior is not None:
                    record.write_text(prior)
                (self.attempt / "output").unlink(missing_ok=True)
                result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                        env=dict(os.environ, SLURM_JOB_ID="42", BASH_ENV=str(startup)),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.attempt / "output").is_file())
                self.assertEqual((self.attempt / "output").read_text(), "produced")
                if prior is None:
                    self.assertFalse(record.exists())
                else:
                    self.assertEqual(record.read_text(), "")
                self.assertEqual(self.judge(), "INCOMPLETE")

    def test_child_free_invalidation_rejects_unsafe_destinations(self):
        self.real_tools()
        startup = self.root / "startup"
        startup.write_text("trap '' CHLD\n")
        self.plan_unit["command"] = "printf produced > output"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        record = self.attempt / U.SLURM_EXIT_RECORD
        protected = self.root / "protected"
        protected.write_text("protected data")
        modes = ["symlink", "dangling", "directory", "fifo"]
        if os.geteuid() != 0:
            modes.append("readonly")
        for mode in modes:
            with self.subTest(mode=mode):
                protected.write_text("protected data")
                if record.is_dir() and not record.is_symlink():
                    record.rmdir()
                else:
                    record.unlink(missing_ok=True)
                (self.attempt / "output").unlink(missing_ok=True)
                if mode == "symlink":
                    record.symlink_to(protected)
                elif mode == "dangling":
                    record.symlink_to(self.root / "missing")
                elif mode == "directory":
                    record.mkdir()
                elif mode == "fifo":
                    os.mkfifo(str(record))
                else:
                    self.exit_record()
                    record.chmod(0o400)
                try:
                    result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                            env=dict(os.environ, SLURM_JOB_ID="42", BASH_ENV=str(startup)),
                                            capture_output=True, text=True, timeout=5)
                except subprocess.TimeoutExpired:
                    self.fail("invalidation blocked on an unsafe destination")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.attempt / "output").exists())
                self.assertEqual(protected.read_text(), "protected data")
                self.assertFalse((self.root / "missing").exists())

    def test_native_child_handler_sees_initialized_unit_context(self):
        self.real_tools()
        dependency = self.root / "dependency"
        dependency.mkdir()
        self.plan_unit["needs"] = ["input"]
        self.state["units"]["input"] = {"state": "DONE", "attempt_dir": str(dependency)}
        startup = self.root / "startup"
        startup.write_text('trap \'builtin printf "%s|%s|%s|%s\\n" '
                           '"$SWARM_UNIT_ID" "$SWARM_UNIT_DIR" "$SWARM_DEP_INPUT" "$PWD"\' CHLD\n')
        self.plan_unit["command"] = "test ! -s slurm-exit.json; /bin/true; printf produced > output"
        original_hook = S._slurm_exit_trap
        prerequisite = ('test "$SWARM_UNIT_DIR" = "$PWD"\n'
                        'test -n "$SWARM_UNIT_ID"\ntest -n "$SWARM_DEP_INPUT"\n')
        with mock.patch.object(U, "run", return_value=(0, "42", "")), \
                mock.patch.object(S, "_slurm_exit_trap", side_effect=lambda directory: prerequisite + original_hook(directory)):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        (self.attempt / "output").unlink()
        self.exit_record()
        environment = dict(os.environ, SLURM_JOB_ID="42", BASH_ENV=str(startup))
        for name in ("SWARM_UNIT_ID", "SWARM_UNIT_DIR", "SWARM_DEP_INPUT"):
            environment.pop(name, None)
        result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                cwd=str(self.root), env=environment,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()),
                         {"unit|{}|{}|{}".format(self.attempt, dependency, self.attempt)})
        self.assertTrue((self.attempt / "output").is_file())
        self.assertEqual((self.attempt / "output").read_text(), "produced")
        self.assertEqual(self.judge(), "INCOMPLETE")
        self.responses["scontrol"] = self.controller()
        self.assertEqual(self.judge(), "DONE")

    def test_wrapper_preserves_preinstalled_native_signal_handler(self):
        self.real_tools()
        startup = self.root / "startup"
        startup.write_text("trap ':' USR1\n")
        self.plan_unit["command"] = "kill -USR1 $$; printf produced > output"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        (self.attempt / "output").unlink()
        result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                env=dict(os.environ, SLURM_JOB_ID="42", BASH_ENV=str(startup)),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.attempt / "output").read_text(), "produced")
        self.assertEqual(json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())["exit_status"], 0)
        self.assertEqual(self.judge(), "DONE")

    def test_preinstalled_exit_handler_is_preserved_without_fabricating_record(self):
        self.real_tools()
        startup = self.root / "startup"
        startup.write_text("trap 'printf produced > output' EXIT\n")
        self.plan_unit["command"] = "true"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        (self.attempt / "output").unlink()
        self.exit_record(exit_status=99)
        result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                env=dict(os.environ, SLURM_JOB_ID="42", BASH_ENV=str(startup)),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.attempt / "output").is_file())
        self.assertEqual((self.attempt / "output").read_text(), "produced")
        self.assertFalse((self.attempt / U.SLURM_EXIT_RECORD).exists())
        self.assertEqual(self.judge(), "INCOMPLETE")
        self.responses["scontrol"] = self.controller()
        self.assertEqual(self.judge(), "DONE")

    def test_native_debug_tracing_does_not_abort_payload(self):
        self.real_tools()
        startup = self.root / "startup"
        for options in ("set -T", "shopt -s extdebug", "shopt -s extdebug; set +T",
                        "BASH_XTRACEFD=1; set -x", "set -x"):
            for status in (0, 7):
                with self.subTest(options=options, status=status):
                    native_startup = "extdebug" not in options
                    startup.write_text((options + '\n' if native_startup else '')
                                       + 'trap \'builtin printf "%s\\n" WRAPPER_TRACE\' DEBUG\n')
                    self.plan_unit["command"] = "printf produced > output; exit " + str(status)
                    with mock.patch.object(U, "run", return_value=(0, "42", "")):
                        S._submit(self.plan_unit, self.attempt, False,
                                  state=self.state, state_dir=self.root / "state")
                    if (self.attempt / "output").exists():
                        (self.attempt / "output").unlink()
                    self.exit_record()
                    invocation = ([self.bash(), str(self.attempt / "job.sbatch")]
                                  if native_startup else
                                  [self.bash(), "-c", options + '; source "$1"',
                                   "wrapper", str(self.attempt / "job.sbatch")])
                    result = subprocess.run(invocation,
                                            env=dict(os.environ, SLURM_JOB_ID="42", BASH_ENV=str(startup)),
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertIn("WRAPPER_TRACE", result.stdout)
                    self.assertTrue((self.attempt / "output").is_file())
                    self.assertEqual((self.attempt / "output").read_text(), "produced")
                    self.assertEqual((self.attempt / U.SLURM_EXIT_RECORD).read_text(), "")
                    self.responses["scontrol"] = (1, "", "Invalid job id specified")
                    self.assertEqual(self.judge(), "INCOMPLETE")
                    self.responses["scontrol"] = self.controller(
                        state="FAILED" if status else "COMPLETED", code=str(status) + ":0")
                    self.assertEqual(self.judge(), "FAILED" if status else "DONE")

    def test_record_temporary_path_quotes_pid_under_readonly_ifs(self):
        self.real_tools()
        startup = self.root / "startup"
        startup.write_text("readonly IFS=0123456789\n")
        captured = self.attempt / "renamed-source"
        self.stub("mv", "from pathlib import Path; import os, sys; Path(%r).write_text(sys.argv[-2]); "
                  "os.replace(sys.argv[-2], sys.argv[-1])" % str(captured))
        self.plan_unit["command"] = 'printf "%s" "$$" > process-id; printf "%s" "$IFS" > output'
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                env=dict(os.environ, SLURM_JOB_ID="42", BASH_ENV=str(startup)),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.attempt / "output").read_text(), "0123456789")
        self.assertTrue(captured.is_file(), result.stderr)
        self.assertEqual(captured.read_text(), str(self.attempt / (U.SLURM_EXIT_RECORD + ".tmp."))
                         + (self.attempt / "process-id").read_text())
        self.assertEqual(self.judge(), "DONE")

    def test_failed_generator_cannot_continue_into_payload(self):
        self.real_tools()
        self.plan_unit["command"] = "printf produced > output"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        script = self.attempt / "job.sbatch"
        original = script.read_text()
        self.assertEqual(original.count("/bin/bash -p -c"), 1)
        script.write_text(original.replace("/bin/bash -p -c", shlex.quote(str(self.bin / "generator"))))
        for partial in ("", "builtin trap ':' USR1;"):
            with self.subTest(partial=partial):
                self.stub("generator", "import sys; sys.stdout.write(%r); sys.exit(1)" % partial)
                self.exit_record(exit_status=99)
                (self.attempt / "output").unlink(missing_ok=True)
                result = subprocess.run([self.bash(), str(script)],
                                        env=dict(os.environ, SLURM_JOB_ID="42"),
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.attempt / "output").exists())
                self.assertFalse((self.attempt / U.SLURM_EXIT_RECORD).exists())
                self.assertEqual(self.judge(), "INCOMPLETE")

    def test_signal_setup_preserves_inherited_payload_variables(self):
        self.real_tools()
        self.plan_unit["command"] = 'printf "%s:%s:%s" "$swarm_signal" "$swarm_signal_number" "$1" > output'
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch"), "first argument"],
                                env=dict(os.environ, SLURM_JOB_ID="42",
                                         swarm_signal="ready", swarm_signal_number="value"),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.attempt / "output").read_text(), "ready:value:first argument")
        self.assertEqual(json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())["exit_status"], 0)

    def test_record_identity_is_captured_before_payload_changes_environment(self):
        self.real_tools()
        self.plan_unit["command"] = (
            "unset SLURM_JOB_ID SLURM_ARRAY_JOB_ID SLURM_ARRAY_TASK_ID; "
            "SLURM_RESTART_COUNT=99; touch output")
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                env=dict(os.environ, SLURM_JOB_ID="43", SLURM_ARRAY_JOB_ID="42",
                                         SLURM_ARRAY_TASK_ID="1", SLURM_RESTART_COUNT="2"),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.attempt / U.SLURM_EXIT_RECORD).is_file())
        record = json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())
        self.assertEqual([record[field] for field in
                          ("job_id", "array_job_id", "array_task_id", "restart_count")],
                         ["43", "42", "1", "2"])
        self.spec["job_id"] = "42_1"
        self.assertEqual(self.judge(), "DONE")

    def test_recorder_ignores_payload_bookkeeping_names(self):
        self.real_tools()
        for status in (0, 7):
            with self.subTest(status=status):
                self.plan_unit["command"] = (
                    "readonly swarm_status=complete swarm_exit_path=output "
                    "swarm_exit_tmp=output swarm_array_job=wrong swarm_array_task=wrong; "
                    "swarm_record_exit() { return 19; }; readonly -f swarm_record_exit; "
                    "printf 'payload' > output; exit " + str(status))
                with mock.patch.object(U, "run", return_value=(0, "42", "")):
                    S._submit(self.plan_unit, self.attempt, False,
                              state=self.state, state_dir=self.root / "state")
                result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                        env=dict(os.environ, SLURM_JOB_ID="42"), timeout=5)
                self.assertEqual(result.returncode, status)
                self.assertEqual((self.attempt / "output").read_text(), "payload")
                self.assertTrue((self.attempt / U.SLURM_EXIT_RECORD).is_file())
                record = json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())
                self.assertEqual(record["exit_status"], status)

    def test_recorder_failures_preserve_payload_status(self):
        self.real_tools()
        for failure in ("write", "publish", "cleanup"):
            for status in (0, 7):
                with self.subTest(failure=failure, status=status):
                    record_path = self.attempt / U.SLURM_EXIT_RECORD
                    if record_path.is_dir():
                        record_path.rmdir()
                    self.plan_unit["command"] = (
                        ("mkdir slurm-exit.json.tmp.$$; " if failure == "write" else "")
                        + ("mkdir slurm-exit.json; " if failure == "cleanup" else "")
                        + "touch output; exit " + str(status))
                    if failure in ("publish", "cleanup"):
                        self.stub("mv", "import sys; sys.exit(1)")
                    with mock.patch.object(U, "run", return_value=(0, "42", "")):
                        S._submit(self.plan_unit, self.attempt, False,
                                  state=self.state, state_dir=self.root / "state")
                    self.exit_record()
                    result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                            env=dict(os.environ, SLURM_JOB_ID="42"),
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, status, result.stderr)
                    record_path = self.attempt / U.SLURM_EXIT_RECORD
                    if failure == "cleanup":
                        self.assertTrue(record_path.is_dir())
                        record_path.rmdir()
                    else:
                        self.assertFalse(record_path.exists())

    def test_a_bash_4_host_never_skips_the_wrapper_tests(self):
        """Where /bin/bash is 4+, the wrapper tests must run, not skip."""
        if _bash_major("/bin/bash") >= 4:
            self.assertIsNotNone(BASH, "a bash 4+ exists at /bin/bash, yet the "
                                 "probe found none; wrapper tests would skip")
        with mock.patch.dict(os.environ, {"BASH_ENV": str(self.root / "noisy")}):
            (self.root / "noisy").write_text("printf 'site setup\\n'\n")
            for path in filter(None, (BASH, OLD_BASH)):
                self.assertGreater(_bash_major(path), 0, path)

    @unittest.skipUnless(OLD_BASH, "/bin/bash here is 4+; the opt-out applies "
                         "to bash 3.2 such as macOS /bin/bash")
    def test_old_bash_opts_out_instead_of_recording(self):
        """bash 3.2 cannot run the CHLD probe, so the wrapper must not record.

        Its `trap -p CHLD >&-` returns 0 even with a CHLD trap set, so the
        probe would wrongly report a default disposition. The wrapper checks
        BASH_VERSINFO first and takes the opt-out: the payload still runs and
        keeps its status, no record is published, and a stale one is cleared.
        """
        self.real_tools()
        self.plan_unit["command"] = "printf produced > output; exit 7"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        record = self.attempt / U.SLURM_EXIT_RECORD
        record.write_text('{"job_id":"42","exit_status":0}')
        result = subprocess.run([OLD_BASH, str(self.attempt / "job.sbatch")],
                                env=dict(os.environ, SLURM_JOB_ID="42"),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual((self.attempt / "output").read_text(), "produced")
        self.assertEqual(record.read_text(), "")
        self.assertNotIn("invalid signal specification", result.stderr)
        self.assertEqual(self.judge(), "INCOMPLETE")

    def test_invalid_job_id_cannot_publish_a_stale_temporary_record(self):
        self.real_tools()
        stale = json.dumps({"job_id": "42", "exit_status": 0, "end_time": U.now_iso()})
        candidate = shlex.quote(str(self.attempt / (U.SLURM_EXIT_RECORD + ".tmp."))) + "$$"
        launcher = 'printf "%s\\n" ' + shlex.quote(stale) + ' > ' + candidate + '; exec ' + shlex.quote(self.bash()) + ' "$1"'
        for status in (0, 7):
            for job_id in (None, "bad id"):
                with self.subTest(status=status, job_id=job_id):
                    self.plan_unit["command"] = "exit " + str(status)
                    with mock.patch.object(U, "run", return_value=(0, "42", "")):
                        S._submit(self.plan_unit, self.attempt, False,
                                  state=self.state, state_dir=self.root / "state")
                    environment = dict(os.environ)
                    environment.pop("SLURM_JOB_ID", None)
                    if job_id is not None:
                        environment["SLURM_JOB_ID"] = job_id
                    result = subprocess.run([self.bash(), "-c", launcher, "launcher",
                                             str(self.attempt / "job.sbatch")],
                                            env=environment, capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertFalse((self.attempt / U.SLURM_EXIT_RECORD).exists())
                    self.assertEqual(self.judge(), "INCOMPLETE")

    def test_restart_count_is_recorded_and_stale_record_removed_at_start(self):
        self.real_tools()
        self.plan_unit["command"] = "test ! -e slurm-exit.json; touch output"
        with mock.patch.object(U, "run", return_value=(0, "42", "")):
            S._submit(self.plan_unit, self.attempt, False,
                      state=self.state, state_dir=self.root / "state")
        for restart, expected in ((None, "0"), ("1", "1"), ("03", "03"),
                                  ('bad"value', ""), ('$(touch injected)', "")):
            with self.subTest(restart=restart):
                self.exit_record(exit_status=99)
                environment = dict(os.environ, SLURM_JOB_ID="42")
                environment.pop("SLURM_RESTART_COUNT", None)
                if restart is not None:
                    environment["SLURM_RESTART_COUNT"] = restart
                result = subprocess.run([self.bash(), str(self.attempt / "job.sbatch")],
                                        env=environment, timeout=5)
                self.assertEqual(result.returncode, 0)
                record = json.loads((self.attempt / U.SLURM_EXIT_RECORD).read_text())
                self.assertEqual(record["restart_count"], expected)
                self.assertEqual(record["exit_status"], 0)
                self.assertFalse((self.attempt / "injected").exists())

    def test_unobserved_requeue_is_an_explicit_weaker_evidence_limit(self):
        self.exit_record(restart_count="0")
        self.responses["squeue"] = (0, "42", "")
        self.assertEqual(self.judge(), "INCOMPLETE")
        self.responses["squeue"] = (0, "", "")
        self.assertEqual(self.judge(), "DONE")
        self.assertEqual(self.spec["slurm_evidence_source"], "exit-record")
        self.assertTrue(any("unobserved requeue" in note for note in self.notes))


if __name__ == "__main__":
    unittest.main()
