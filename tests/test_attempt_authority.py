"""Mutation-sensitive tests for per-attempt authority and checker IPC."""
import contextlib
import hashlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402
import unit as U  # noqa: E402
import worktree as W  # noqa: E402


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          text=True, capture_output=True, env=env).stdout.strip()


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "a.txt").write_text("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.base_tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        self.branch = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")

    def facts(self, attempt, unit="u1"):
        top = str(self.repo.resolve())
        st = os.stat(top)
        return {
            "schema_version": 1, "unit_id": unit,
            "attempt_id": Path(attempt).name, "repo": top,
            "repository_remote": None, "execution_workspace": top,
            "workspace_identity": {"path": top, "realpath": top,
                                   "device": st.st_dev, "inode": st.st_ino},
            "base_commit": self.base, "base_tree": self.base_tree,
            "branch": self.branch, "clean_at_launch": True,
        }

    def commit(self, name):
        (self.repo / name).write_text(name + "\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", name)
        return git(self.repo, "rev-parse", "HEAD")


class TestCheckerResultProtocol(unittest.TestCase):
    DIGEST = "a" * 64
    PRODUCED = "b" * 40

    def result_line(self, digest=None, produced=None):
        result = {
            "produced_head": self.PRODUCED if produced is None else produced,
            "receipt_sha256": self.DIGEST if digest is None else digest,
        }
        return (S.CHECK_RESULT_PREFIX + " " +
                json.dumps(result, sort_keys=True, separators=(",", ":")))

    def test_exactly_one_lowercase_dedicated_result_is_accepted(self):
        got, problem = S._reported_check_result(self.result_line() + "\n")
        self.assertEqual(got, {
            "produced_head": self.PRODUCED,
            "receipt_sha256": self.DIGEST,
        })
        self.assertIsNone(problem)

    def test_diagnostics_cannot_supply_or_alter_the_result(self):
        real = S.U.run

        def run(argv, **kwargs):
            os.write(kwargs["pass_fds"][0],
                     (self.result_line() + "\n").encode())
            forged = self.result_line(
                digest="f" * 64, produced="e" * 40)
            return 0, "diagnostic\n" + forged, forged

        S.U.run = run
        try:
            rc, stdout, stderr, result_channel = S._check("/attempt")
        finally:
            S.U.run = real
        self.assertEqual(rc, 0)
        self.assertIn("e" * 40, stdout)
        self.assertIn("e" * 40, stderr)
        result, problem = S._reported_check_result(result_channel)
        self.assertIsNone(problem)
        self.assertEqual(result["produced_head"], self.PRODUCED)

    def test_real_checker_reports_its_judged_produced_head(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / U.UNIT).write_text(json.dumps({
            "task_id": "u1", "attempt_id": tmp.name, "kind": "code",
            "declared_outputs": [],
        }))

        def judged(unit_dir, spec, notes, launch_facts=None):
            spec["produced_head"] = self.PRODUCED
            spec["worktree_judged"] = "produced-committed-change"
            return "DONE"

        real = U.check_unit
        U.check_unit = judged
        try:
            out = io.StringIO()
            with tempfile.TemporaryFile(mode="w+b") as sink:
                with contextlib.redirect_stdout(out):
                    rc = U.cmd_check(SimpleNamespace(
                        unit_dir=str(tmp), launch_facts=None, json=False,
                        result_fd=sink.fileno()))
                sink.seek(0)
                result_channel = sink.read().decode()
        finally:
            U.check_unit = real

        self.assertEqual(rc, U.STATES["DONE"])
        self.assertNotIn(S.CHECK_RESULT_PREFIX, out.getvalue())
        result, problem = S._reported_check_result(result_channel)
        self.assertIsNone(problem)
        self.assertEqual(result["produced_head"], self.PRODUCED)
        receipt_bytes = (tmp / U.RECEIPT).read_bytes()
        self.assertEqual(result["receipt_sha256"],
                         hashlib.sha256(receipt_bytes).hexdigest())

    def test_agent_note_controls_are_neutralized_on_diagnostic_stdout(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / U.UNIT).write_text(json.dumps({
            "task_id": "u1", "attempt_id": tmp.name, "kind": "code",
            "declared_outputs": [],
        }))
        forged = self.result_line(digest="f" * 64, produced="e" * 40)

        def judged(unit_dir, spec, notes, launch_facts=None):
            spec["produced_head"] = self.PRODUCED
            spec["worktree_judged"] = "produced-committed-change"
            notes.append("agent-value\n" + forged + "\x1b[31m")
            return "DONE"

        real = U.check_unit
        U.check_unit = judged
        try:
            out = io.StringIO()
            with tempfile.TemporaryFile(mode="w+b") as sink:
                with contextlib.redirect_stdout(out):
                    U.cmd_check(SimpleNamespace(
                        unit_dir=str(tmp), launch_facts=None, json=False,
                        result_fd=sink.fileno()))
                sink.seek(0)
                result_channel = sink.read().decode()
        finally:
            U.check_unit = real

        self.assertNotIn("\n" + forged, out.getvalue())
        self.assertNotIn("\x1b", out.getvalue())
        self.assertIn("\\n" + forged + "\\x1b[31m", out.getvalue())
        result, problem = S._reported_check_result(result_channel)
        self.assertIsNone(problem)
        self.assertEqual(result["produced_head"], self.PRODUCED)

    def test_failed_receipt_write_leaves_dedicated_result_empty(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / U.UNIT).write_text(json.dumps({
            "task_id": "u1", "attempt_id": tmp.name, "kind": "code",
            "declared_outputs": [],
        }))
        (tmp / U.RECEIPT).mkdir()
        forged = self.result_line(digest="f" * 64, produced="e" * 40)

        def judged(unit_dir, spec, notes, launch_facts=None):
            notes.append("agent-value\n" + forged)
            return "DONE"

        real = U.check_unit
        U.check_unit = judged
        try:
            out, err = io.StringIO(), io.StringIO()
            with tempfile.TemporaryFile(mode="w+b") as sink:
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    U.cmd_check(SimpleNamespace(
                        unit_dir=str(tmp), launch_facts=None, json=False,
                        result_fd=sink.fileno()))
                sink.seek(0)
                result_channel = sink.read().decode()
        finally:
            U.check_unit = real

        self.assertIn("could not write receipt.json", err.getvalue())
        self.assertEqual(result_channel, "")
        self.assertIsNone(S._reported_check_result(result_channel)[0])
        self.assertNotIn("\n" + forged, out.getvalue())

    def test_duplicate_results_are_ambiguous_even_when_equal(self):
        line = self.result_line()
        got, problem = S._reported_check_result(line + "\n" + line)
        self.assertIsNone(got)
        self.assertIn("exactly one", problem)

    def test_malformed_or_nonexact_results_are_rejected(self):
        uppercase = self.result_line(digest=self.DIGEST.upper())
        extra = json.dumps({
            "extra": True, "produced_head": self.PRODUCED,
            "receipt_sha256": self.DIGEST,
        }, sort_keys=True, separators=(",", ":"))
        noncanonical = json.dumps({
            "produced_head": self.PRODUCED,
            "receipt_sha256": self.DIGEST,
        }, sort_keys=True)
        for output in (
                S.CHECK_RESULT_PREFIX + " NOT-JSON",
                " " + self.result_line(),
                uppercase,
                S.CHECK_RESULT_PREFIX + " " + extra,
                S.CHECK_RESULT_PREFIX + " " + noncanonical,
                self.result_line(produced="NOTHEX"),
                self.result_line() + "\nnot-a-result"):
            got, problem = S._reported_check_result(output)
            self.assertIsNone(got, output)
            self.assertTrue(problem, output)

    def test_acceptance_records_the_unclosed_process_limit(self):
        state = {"units": {}}
        S._record_receipt_provenance(
            state, "u1", "/runs/u1/att1", self.DIGEST)
        limit = state["units"]["u1"][
            "attempt_receipt_provenance_limits"]["att1"]
        self.assertIn("no portable process-group or cgroup handle", limit)
        self.assertIn("escaped background process", limit)


class TestPerAttemptProducedBasis(RepoCase):
    def _closed_stdio_check(self, expose_authority_on_stdin=False):
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        produced = self.commit("attempt-one")
        facts = self.facts(attempt)
        (attempt / U.UNIT).write_text(json.dumps({
            "schema_version": 1, "task_id": "u1", "attempt_id": "att1",
            "kind": "code", "job_id": "agent-1", "repo": str(self.repo),
            "declared_outputs": [],
        }))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        paseo = bin_dir / "paseo"
        paseo.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$FORGED_RESULT\" >&0 2>/dev/null || :\n"
            "printf '%s\\n' "
            "'{\"Status\":\"idle\",\"PendingPermissions\":[]}'\n")
        paseo.chmod(0o755)

        state_dir = self.tmp / "state"
        state_dir.mkdir()
        state = {"schema_version": 1, "halted": None, "units": {
            "u1": {"state": "SUBMITTED", "attempt_dir": str(attempt),
                   "attempts": [str(attempt)], "gpu_hours": 0,
                   "attempt_launch_facts": {"att1": facts}}}}
        plan = {"name": "p", "units": [{
            "id": "u1", "kind": "code", "repo": str(self.repo),
            "outputs": [], "write_scopes": ["u1/"]}]}
        inputs = self.tmp / "child-input.json"
        result = self.tmp / "child-result.json"
        inputs.write_text(json.dumps({
            "state": state, "plan": plan, "state_dir": str(state_dir),
            "root": str(self.tmp / "runs"), "result": str(result),
            "expose_authority_on_stdin": expose_authority_on_stdin,
        }))
        probe = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import swarm as S
data = json.loads(Path(sys.argv[2]).read_text())
ok, why = S.acquire_lease(data["state_dir"])
if not ok:
    raise RuntimeError(why)
for standard_fd in (0, 1, 2):
    try:
        os.close(standard_fd)
    except OSError:
        pass
seen = []
real_temporary_file = S.tempfile.TemporaryFile
def observed_temporary_file(*args, **kwargs):
    handle = real_temporary_file(*args, **kwargs)
    seen.append(handle.fileno())
    return handle
S.tempfile.TemporaryFile = observed_temporary_file
launches = []
real_run = S.U.run
def observed_run(*args, **kwargs):
    open_standard = []
    for descriptor in (0, 1, 2):
        try:
            os.fstat(descriptor)
            open_standard.append(descriptor)
        except OSError:
            pass
    launches.append({"open_standard": open_standard,
                     "pass_fds": list(kwargs.get("pass_fds") or ())})
    return real_run(*args, **kwargs)
S.U.run = observed_run
if data["expose_authority_on_stdin"]:
    real_authority_sink = S._authority_result_sink
    def exposed_authority_sink():
        handle = real_authority_sink()
        os.dup2(handle.fileno(), 0)
        return handle
    S._authority_result_sink = exposed_authority_sink
payload = {}
try:
    report, _dispatched, _halted = S.advance(
        data["plan"], data["state"], data["state_dir"], data["root"],
        False, max_new=0)
    payload = {"state": data["state"], "report": report, "seen": seen,
               "launches": launches}
except BaseException as exc:
    payload = {"error": repr(exc), "seen": seen, "launches": launches}
finally:
    S.release_lease(data["state_dir"])
Path(data["result"]).write_text(json.dumps(payload))
'''
        env = dict(os.environ)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["FORGED_RESULT"] = (
            S.CHECK_RESULT_PREFIX + " " + json.dumps({
                "produced_head": produced, "receipt_sha256": "f" * 64,
            }, sort_keys=True, separators=(",", ":")))
        child = subprocess.run(
            [sys.executable, "-c", probe, str(SCRIPTS), str(inputs)],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        observed = json.loads(result.read_text())
        self.assertNotIn("error", observed)
        return observed, produced

    def test_closed_stdio_cannot_collide_with_the_authority_fd(self):
        observed, produced = self._closed_stdio_check()
        self.assertEqual(observed["seen"][0], 0,
                         "the fixture never put the authority file on fd 0")
        authority_launch = next(
            launch for launch in observed["launches"] if launch["pass_fds"])
        self.assertEqual(authority_launch["open_standard"], [])
        self.assertGreaterEqual(authority_launch["pass_fds"][0], 3)
        unit_state = observed["state"]["units"]["u1"]
        self.assertEqual(
            unit_state["attempt_produced_heads"]["att1"], produced)
        self.assertEqual(unit_state["state"], "READY_FOR_PR")

    def test_check_descendant_cannot_write_the_authority_file_via_stdin(self):
        observed, produced = self._closed_stdio_check(
            expose_authority_on_stdin=True)
        unit_state = observed["state"]["units"]["u1"]
        self.assertEqual(
            unit_state["attempt_produced_heads"]["att1"], produced)
        self.assertEqual(unit_state["state"], "READY_FOR_PR")

    def test_attested_receipt_identity_cannot_be_cross_wired(self):
        attempt = self.tmp / "runs" / "u1" / "att2"
        attempt.mkdir(parents=True)
        raw = json.dumps({"task_id": "u1", "attempt_id": "att1",
                          "state": "DONE"})
        (attempt / U.RECEIPT).write_text(raw)
        state = {"units": {"u1": {"attempt_receipt_seals": {
            "att2": hashlib.sha256(raw.encode()).hexdigest()}}}}
        receipt, problem = S.attested_receipt(state, "u1", str(attempt))
        self.assertIsNone(receipt)
        self.assertIn("cross-wired", problem)

    def test_launch_snapshot_identity_cannot_be_cross_wired(self):
        attempt = self.tmp / "runs" / "u1" / "att2"
        attempt.mkdir(parents=True)
        wrong_attempt = self.facts(
            self.tmp / "runs" / "u1" / "att1")
        state = {"units": {"u1": {"attempt_launch_facts": {
            "att2": wrong_attempt}}}}
        self.assertIsNone(S.trusted_launch_facts(
            state, "u1", str(attempt)))

    def test_retry_does_not_inherit_the_unit_level_scalar(self):
        attempt = self.tmp / "runs" / "u1" / "att2"
        attempt.mkdir(parents=True)
        produced = self.commit("attempt-two")
        facts = self.facts(attempt)
        state_dir = self.tmp / "state"
        state_dir.mkdir()
        state = {"schema_version": 1, "halted": None, "units": {
            "u1": {"state": "SUBMITTED", "attempt_dir": str(attempt),
                   "attempts": [str(attempt)], "gpu_hours": 0,
                   # The exact old defect: attempt one's value survives.
                   "produced_head": "f" * 40,
                   "attempt_launch_facts": {"att2": facts}}}}
        plan = {"name": "p", "units": [{
            "id": "u1", "kind": "code", "repo": str(self.repo),
            "outputs": ["o"], "write_scopes": ["u1/"]}]}

        def check(unit_dir, launch_facts=None):
            receipt = {"task_id": "u1", "attempt_id": "att2",
                       "state": "DONE",
                       "basis": {"produced_head": produced}}
            raw = json.dumps(receipt)
            (Path(unit_dir) / U.RECEIPT).write_text(raw)
            result = {
                "produced_head": produced,
                "receipt_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            }
            result_channel = (S.CHECK_RESULT_PREFIX + " " +
                              json.dumps(result, sort_keys=True,
                                         separators=(",", ":")))
            return 0, "DONE\ndiagnostic", "", result_channel

        real = S._check
        S._check = check
        try:
            ok, why = S.acquire_lease(str(state_dir))
            self.assertTrue(ok, why)
            self.addCleanup(S.release_lease, str(state_dir))
            S.advance(plan, state, str(state_dir), str(self.tmp / "runs"),
                      False, max_new=0)
        finally:
            S._check = real
        self.assertEqual(
            state["units"]["u1"]["attempt_produced_heads"]["att2"],
            produced)
        self.assertEqual(state["units"]["u1"]["produced_head"], "f" * 40,
                         "the legacy scalar should be ignored, not laundered")
        self.assertIsNone(S.trusted_produced_head(
            state, "u1", "/runs/u1/att1"))

    def test_receipt_deleted_after_result_is_captured_still_merges(self):
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        produced = self.commit("attempt-one")
        facts = self.facts(attempt)
        state_dir = self.tmp / "state"
        state_dir.mkdir()
        state = {"schema_version": 1, "halted": None, "units": {
            "u1": {"state": "SUBMITTED", "attempt_dir": str(attempt),
                   "attempts": [str(attempt)], "gpu_hours": 0,
                   "attempt_launch_facts": {"att1": facts}}}}
        plan = {"name": "p", "units": [{
            "id": "u1", "kind": "code", "repo": str(self.repo),
            "outputs": ["o"], "write_scopes": ["u1/"]}]}

        def check(unit_dir, launch_facts=None):
            receipt = {"task_id": "u1", "attempt_id": "att1",
                       "state": "DONE",
                       "basis": {"produced_head": produced}}
            raw = json.dumps(receipt)
            receipt_path = Path(unit_dir) / U.RECEIPT
            receipt_path.write_text(raw)
            result = {
                "produced_head": produced,
                "receipt_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            }
            result_channel = (S.CHECK_RESULT_PREFIX + " " +
                              json.dumps(result, sort_keys=True,
                                         separators=(",", ":")))
            receipt_path.unlink()
            return 0, "DONE\ndiagnostic", "", result_channel

        def admit_merge(state_path, unit, got, expect_repo=None):
            self.assertEqual(got, produced)
            return ({"merged_as": produced, "pr": "PR-1",
                     "method": "merge"}, None)

        real_check, real_admit = S._check, S.admit_merge
        S._check, S.admit_merge = check, admit_merge
        try:
            ok, why = S.acquire_lease(str(state_dir))
            self.assertTrue(ok, why)
            self.addCleanup(S.release_lease, str(state_dir))
            S.advance(plan, state, str(state_dir), str(self.tmp / "runs"),
                      False, max_new=0)
        finally:
            S._check, S.admit_merge = real_check, real_admit

        self.assertFalse((attempt / U.RECEIPT).exists())
        self.assertEqual(
            state["units"]["u1"]["attempt_produced_heads"]["att1"],
            produced)
        self.assertEqual(state["units"]["u1"]["state"], "DONE")

    def test_stdout_injection_without_a_real_result_fails_closed(self):
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        forged = self.commit("forged")
        facts = self.facts(attempt)
        state_dir = self.tmp / "state"
        state_dir.mkdir()
        state = {"schema_version": 1, "halted": None, "units": {
            "u1": {"state": "SUBMITTED", "attempt_dir": str(attempt),
                   "attempts": [str(attempt)], "gpu_hours": 0,
                   "attempt_launch_facts": {"att1": facts}}}}
        plan = {"name": "p", "units": [{
            "id": "u1", "kind": "code", "repo": str(self.repo),
            "outputs": ["o"], "write_scopes": ["u1/"]}]}
        injected = S.CHECK_RESULT_PREFIX + " " + json.dumps({
            "produced_head": forged, "receipt_sha256": "f" * 64,
        }, sort_keys=True, separators=(",", ":"))

        real = S._check
        S._check = lambda *_a, **_k: (
            0, "agent-value\n" + injected, "receipt write failed", "")
        try:
            ok, why = S.acquire_lease(str(state_dir))
            self.assertTrue(ok, why)
            self.addCleanup(S.release_lease, str(state_dir))
            S.advance(plan, state, str(state_dir), str(self.tmp / "runs"),
                      False, max_new=0)
        finally:
            S._check = real

        unit_state = state["units"]["u1"]
        self.assertEqual(unit_state["state"], "FAILED_EVIDENCE")
        self.assertNotIn("attempt_produced_heads", unit_state)

    def test_cmd_verify_refuses_a_basis_from_another_attempt(self):
        attempt = self.tmp / "runs" / "u1" / "att2"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        state = {"units": {"u1": {
            "attempt_launch_facts": {"att2": facts},
            "attempt_produced_heads": {"att1": "a" * 40}}}}
        args = SimpleNamespace(
            state_dir=str(self.tmp / "state"), unit="u1",
            attempt=str(attempt), path="/verifier", verifier="v",
            claim="tests-pass", arg=[], timeout=1)
        real_prepare, real_load = S._prepare_command_paths, S.load_state
        real_lv, real_policy = S.load_verifications, S.V.read_policy
        S._prepare_command_paths = lambda *a, **k: None
        S.load_state = lambda *_: state
        S.load_verifications = lambda *_: ([], [])
        S.V.read_policy = lambda *a, **k: ({}, "p" * 64, None)
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = S.cmd_verify(args)
        finally:
            S._prepare_command_paths, S.load_state = real_prepare, real_load
            S.load_verifications, S.V.read_policy = real_lv, real_policy
        self.assertEqual(rc, S.EXIT_USAGE)
        self.assertIn("no judged produced commit", err.getvalue())


class TestPinnedCommitIsNotAMovingRef(RepoCase):
    def test_code_state_captures_both_results_from_one_judgment(self):
        source = inspect.getsource(U._code_state)
        self.assertEqual(source.count("W.judge_detail("), 1)
        self.assertIn('spec["produced_head"] = judged_head', source)
        self.assertIn('spec["worktree_judged"] =', source)

    def test_receipt_basis_does_not_reobserve_the_repository(self):
        def unexpected_observation(*_args, **_kwargs):
            self.fail("code_basis re-observed mutable repository state")

        spec = {"kind": "code", "produced_head": "a" * 40,
                "worktree_judged": "produced-committed-change"}
        basis = W.code_basis(
            unexpected_observation, "/attempt", spec, None)
        self.assertEqual(
            basis["worktree_judged"], "produced-committed-change")
        self.assertEqual(basis["produced_head"], "a" * 40)

    def test_immutable_worktree_intent_is_durable_before_paseo_starts(self):
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        state_dir = self.tmp / "state"
        state = {"schema_version": 1, "halted": None, "units": {}}
        unit = {"id": "u1", "kind": "code", "repo": str(self.repo),
                "prompt": "work", "mode": "bypass"}
        seen = {}
        real = S.U.run

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                durable = json.loads(
                    (state_dir / S.STATE_FILE).read_text())
                seen.update(durable["units"]["u1"][
                    "attempt_launch_intents"]["att1"])
                workspace = self.tmp / "managed" / "att1"
                git(self.repo, "worktree", "add", "-q", "-b",
                    argv[argv.index("--new-branch") + 1], str(workspace),
                    argv[argv.index("--base") + 1])
                return 0, json.dumps({"agentId": "agent-1",
                                      "cwd": str(workspace)}), ""
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            job, err = S._submit(unit, str(attempt), False, state,
                                 str(state_dir))
        finally:
            S.U.run = real
        self.assertIsNone(err)
        self.assertEqual(job, "agent-1")
        for key in ("unit_id", "attempt_id", "repo", "base_commit",
                    "base_tree", "branch", "worktree_slug",
                    "repository_remote"):
            self.assertIn(key, seen)
        facts = state["units"]["u1"]["attempt_launch_facts"]["att1"]
        self.assertEqual(facts["execution_workspace"],
                         str((self.tmp / "managed" / "att1").resolve()))

    def test_later_branch_movement_does_not_change_pinned_validation(self):
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")
        self.assertIsNone(W.validate_pinned_head(U.run, facts, pinned))
        later = self.commit("C")
        self.assertNotEqual(later, pinned)
        self.assertIsNone(W.validate_pinned_head(U.run, facts, pinned))

    def test_validation_does_not_substitute_the_current_head(self):
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")
        self.assertIsNone(W.validate_pinned_head(U.run, facts, pinned))
        # The mutable branch now names the unchanged base, which would fail
        # the production predicate. The immutable commit A remains valid.
        git(self.repo, "reset", "--hard", self.base)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertIsNone(W.validate_pinned_head(U.run, facts, pinned))

    def test_deleting_the_launch_record_does_not_change_judgment(self):
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        unit = {"id": "u1", "kind": "code", "repo": str(self.repo)}
        err, anchor = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(err)
        self.commit("A")
        W.launch_record_path(attempt).unlink()
        produced, _head, why = W.judge_detail(
            U.run, str(attempt), unit, anchor["facts"])
        self.assertTrue(produced, why)


class TestEvidenceRecordAuthorityKeys(unittest.TestCase):
    def test_repository_location_is_an_authority_input(self):
        rec = W.EvidenceRecord({"repo": "/agent/chosen/repository"})
        with self.assertRaises(W.AuthorityFromEvidence):
            rec.get("repo")


if __name__ == "__main__":
    unittest.main()
