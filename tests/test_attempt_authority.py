"""Mutation-sensitive tests for per-attempt authority and checker IPC."""
import contextlib
import hashlib
import ast
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
        self.remote = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)],
                       check=True)
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "push", "-qu", "origin", "main")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.base_tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        self.branch = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")

    def basis(self, attempt, outputs=()):
        """The pre-dispatch artifact digest a real dispatch would have pinned.

        Every attempt these fixtures fabricate is one the coordinator would
        have dispatched, and a dispatched attempt has a basis in coordinator
        state. Without one the check fails closed, which is B1 working rather
        than anything these tests are about.
        """
        return S._capture_artifact_basis(
            {}, "u1", str(attempt), {"outputs": list(outputs)})

    def facts(self, attempt, unit="u1"):
        top = str(self.repo.resolve())
        st = os.stat(top)
        common = str((Path(top) / git(
            self.repo, "rev-parse", "--git-common-dir")).resolve())
        git_dir = str((Path(top) / git(
            self.repo, "rev-parse", "--git-dir")).resolve())
        common_st, git_st = os.stat(common), os.stat(git_dir)
        return {
            "schema_version": 1, "unit_id": unit,
            "attempt_id": Path(attempt).name, "repo": top,
            "repository_remote": None, "execution_workspace": top,
            "workspace_identity": {"path": top, "realpath": top,
                                   "device": st.st_dev, "inode": st.st_ino,
                                   "git_common_dir": common,
                                   "git_common_device": common_st.st_dev,
                                   "git_common_inode": common_st.st_ino,
                                   "git_dir": git_dir,
                                   "git_dir_device": git_st.st_dev,
                                   "git_dir_inode": git_st.st_ino},
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

        def judged(unit_dir, spec, notes, launch_facts=None,
                   artifact_basis=None):
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
                        unit_dir=str(tmp), launch_facts=None,
                        artifact_basis=None, json=False,
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

        def judged(unit_dir, spec, notes, launch_facts=None,
                   artifact_basis=None):
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
                        unit_dir=str(tmp), launch_facts=None,
                        artifact_basis=None, json=False,
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

        def judged(unit_dir, spec, notes, launch_facts=None,
                   artifact_basis=None):
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
                        unit_dir=str(tmp), launch_facts=None,
                        artifact_basis=None, json=False,
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
                   "attempt_artifact_bases": {
                       Path(attempt).name: self.basis(attempt)},
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

        def check(unit_dir, launch_facts=None, artifact_basis=None):
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
                   "attempt_artifact_bases": {
                       Path(attempt).name: self.basis(attempt)},
                   "attempt_launch_facts": {"att1": facts}}}}
        plan = {"name": "p", "units": [{
            "id": "u1", "kind": "code", "repo": str(self.repo),
            "outputs": ["o"], "write_scopes": ["u1/"]}]}

        def check(unit_dir, launch_facts=None, artifact_basis=None):
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
                   "attempt_artifact_bases": {
                       Path(attempt).name: self.basis(attempt)},
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


def _stderr_tail(text):
    """The distinctive tail of a stderr line, as the refusal renders it."""
    return " ".join(text.split())[-30:]


# The functions whose refusals are fully rendered today. The module has
# more, counted and filed rather than swept here; adding a name to this
# tuple is the way to bring one in, and the test then enforces it.
def _is_rendered(node, allowed_bare):
    """True when this interpolated expression went through a renderer.

    A call to `render_for_record` / `render_git_diagnostic`, a call to
    `len` (an int cannot carry text), or one of the named bare values.
    Anything else -- an arithmetic expression, a concatenation, an
    attribute, a subscript -- is raw, whatever its source text mentions.
    """
    import ast as _ast
    if isinstance(node, _ast.Call):
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        return name in ("render_for_record", "render_git_diagnostic", "len")
    if isinstance(node, _ast.Name):
        return node.id in allowed_bare
    return False


RENDERED_REFUSAL_FUNCTIONS = (
    "judge_detail", "validate_pinned_head", "workspace_identity_problem")


class TestPinnedCommitIsNotAMovingRef(RepoCase):
    def test_code_state_captures_both_results_from_one_judgment(self):
        source = inspect.getsource(U._code_state)
        self.assertEqual(source.count("W.judge_and_capture("), 1)

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
                "target_branch": "main", "prompt": "work",
                "mode": "bypass"}
        seen = {}
        real = S.U.run

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                durable = json.loads(
                    (state_dir / S.STATE_FILE).read_text())
                seen.update(durable["units"]["u1"][
                    "attempt_launch_intents"]["att1"])
                workspace = Path(argv[argv.index("--cwd") + 1])
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
                    "base_tree", "branch", "target_branch", "worktree_slug",
                    "repository_remote", "judgment_ref"):
            self.assertIn(key, seen)
        facts = state["units"]["u1"]["attempt_launch_facts"]["att1"]
        self.assertEqual(facts["execution_workspace"],
                         str((state_dir / "code-worktrees"
                              / "att1").resolve()))

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

    def test_a_missing_repository_is_not_reported_as_a_missing_commit(self):
        """Unknown is not absent, and nine units paid for the difference.

        After the coordinator moved from chimera to a Mac, every launch
        record still named /home/hani/multi-agent-skills, so `git -C` on a
        path that does not exist failed exactly as `cat-file -e` on a
        deleted object does. Nine judged heads were reported "no longer
        available" while all nine commits sat in the new checkout.
        """
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")
        self.assertIsNone(W.validate_pinned_head(U.run, facts, pinned))

        moved = dict(facts)
        moved["repo"] = str(self.tmp / "no-such-checkout")
        why = W.validate_pinned_head(U.run, moved, pinned)
        self.assertIsNotNone(why, "a missing repository must still refuse")
        # git's own words diagnose it; the code names no cause.
        self.assertIn("No such file or directory", why)
        self.assertNotIn("absent", why)
        self.assertNotIn("no longer available", why)

    def test_the_refusal_never_claims_which_cause_it_was(self):
        """Five rounds of classifying git's English, five leaks.

        A step-back committee agreed unanimously that no caller reads a
        category. These are the cases that were each, at some round, sorted
        into the wrong bucket: a genuinely absent object, an unreadable
        object, a path that is a regular file, and an unrecognised message.
        None of them may now be given a cause by this code.
        """
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")
        cases = {
            "unreadable object": "fatal: unable to read object file",
            "corrupt pack": "error: could not get object info",
            "permission": "fatal: cannot change to '/x': Permission denied",
            "unrecognised": "fatal: something nobody has seen before",
        }
        for label, stderr in cases.items():
            with self.subTest(case=label):
                def runner(argv, _stderr=stderr, **kwargs):
                    if "cat-file" in argv:
                        return 1, "", _stderr
                    return real(argv, **kwargs)
                why = W.validate_pinned_head(runner, facts, pinned)
                self.assertIsNotNone(why)
                self.assertIn(W.PIN_VALIDATION_REFUSAL, why)
                self.assertIn(_stderr_tail(stderr), why)
                for word in ("absent", "is not present on this host",
                             "is not a git repository"):
                    self.assertNotIn(word, why,
                                     "%s was given a cause: %r" % (label, why))

    def test_a_refusal_carries_a_stable_greppable_prefix(self):
        """Discovery without classification, which is what the committee
        asked for: a stable label survives git versions, wording and
        locale; a semantic bucket derived from English does not."""
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        self.commit("A")
        why = W.validate_pinned_head(U.run, facts, "0" * 40)
        self.assertIsNotNone(why)
        # The literal, not the module constant: asserting startswith() on
        # W.PIN_VALIDATION_REFUSAL passes for any string when the constant
        # is emptied, which is a test satisfiable by its own source.
        self.assertTrue(
            why.startswith("pinned commit validation failed"),
            "the refusal lost its greppable prefix: %r" % why)
        self.assertTrue(W.PIN_VALIDATION_REFUSAL.strip(),
                        "the prefix constant must not be empty")

    def test_a_diagnostic_is_rendered_before_it_enters_a_record(self):
        """It goes into a record an operator reads."""
        rendered = W.render_git_diagnostic(
            128, "fatal: line one\nline two\ttabbed\x07bell")
        self.assertIn("git exited 128", rendered)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\x07", rendered)
        # Whitespace is COLLAPSED, not merely made printable. Replacing the
        # split/join with the raw text still passes an isprintable() filter,
        # because that filter turns a newline into "?" -- which is why this
        # asserts the words are rejoined by single spaces instead.
        self.assertIn("fatal: line one line two tabbed", rendered)
        self.assertNotIn("  ", rendered)
        self.assertIn("?", rendered)  # the bell survives only as a marker

        self.assertIn("no diagnostic output",
                      W.render_git_diagnostic(1, "   \n  "))
        # The bound is on what gets rendered, not on the payload inside it.
        for size in (400, 401, 5000):
            with self.subTest(size=size):
                long_one = W.render_git_diagnostic(1, "x" * size)
                self.assertLessEqual(
                    len(long_one), W._DIAGNOSTIC_LIMIT,
                    "the rendered diagnostic is %d characters against a "
                    "limit of %d" % (len(long_one), W._DIAGNOSTIC_LIMIT))
        self.assertIn("[truncated]", W.render_git_diagnostic(1, "x" * 5000))
        # A short one is not padded or mangled.
        self.assertEqual(W.render_git_diagnostic(2, "fatal: nope"),
                         "git exited 2 and said: fatal: nope")

    def test_the_whole_refusal_is_bounded_not_just_the_diagnostic(self):
        """kimi-k2.7-code: the recorded path went in verbatim.

        Bounding git's diagnostic and then interpolating a 10,000-character
        repository path left the refusal unbounded through the other field.
        Every value this message interpolates goes through the renderer,
        which is the sibling the first bound missed.
        """
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = dict(self.facts(attempt))
        pinned = self.commit("A")
        facts["repo"] = "/" + "x" * 10000

        why = W.validate_pinned_head(U.run, facts, pinned)
        self.assertIsNotNone(why)
        self.assertLess(
            len(why), 1200,
            "the refusal is %d characters; a recorded path is interpolated "
            "into a durable record and must be bounded like the diagnostic"
            % len(why))
        self.assertIn("[truncated]", why)

    def test_the_renderer_is_total(self):
        """A function whose job is to produce a refusal must never raise.

        luna: a runner returning bytes made the string join raise
        TypeError, so a validation failure became an exception. `_git`
        stringifies in the real path, but the runner is injected.
        """
        for value in (b"fatal: bytes", None, 7, 7.5, ["a"], {"b": 1}, object()):
            with self.subTest(value=type(value).__name__):
                out = W.render_for_record(value, 200)
                self.assertIsInstance(out, str)
        self.assertEqual(W.render_for_record(b"fatal: unable to read", 200),
                         "fatal: unable to read")
        self.assertEqual(W.render_for_record(None, 200), "")
        # kimi-k2.7-code: a limit below the marker's own length produced a
        # result LONGER than the limit.
        for limit in range(0, 16):
            with self.subTest(limit=limit):
                out = W.render_for_record("x" * 100, limit)
                self.assertLessEqual(len(out), limit)

    def test_no_runner_value_is_touched_before_the_renderer(self):
        """The boundary, asserted as a property rather than per field.

        Three rounds running I fixed the call site a reviewer named instead
        of the boundary. luna, kimi-k2.7-code and glm-5.3 then found the
        same defect one argument over: `(err or "").strip()` dereferenced
        the runner's stderr OUTSIDE the total renderer, so a list -- "a
        natural runner shape" -- raised AttributeError and a validation
        failure became an exception. glm's phrase for it: "the exact defect
        class this change claims to have eliminated".

        So this enumerates value shapes an injected runner could plausibly
        return, for BOTH fields, and requires a refusal every time.
        """
        class Hostile(object):
            def __str__(self):
                raise ValueError("this value refuses to be a string")

        class ExitOnStr(object):
            def __str__(self):
                raise SystemExit(3)      # a BaseException, not an Exception

        class PoisonedStr(str):
            def strip(self, *args):      # a str SUBCLASS with a bad method
                raise RuntimeError("poisoned strip")

        class PoisonedStatus(int):
            def __ne__(self, other):     # raises at `rc != 0`
                raise RuntimeError("poisoned comparison")

        shapes = [["fatal: unreadable"], 123, {"b": 1}, object(), Hostile(),
                  ExitOnStr(), PoisonedStr("fatal: poisoned"),
                  b"fatal: bytes", None, "", "   "]
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")

        for shape in shapes:
            with self.subTest(stderr=type(shape).__name__):
                def runner(argv, _s=shape, **kwargs):
                    if "cat-file" in argv:
                        return 1, "", _s
                    return real(argv, **kwargs)
                why = W.validate_pinned_head(runner, facts, pinned)
                self.assertIsNotNone(why, "no refusal for stderr %r" % (shape,))
                self.assertIsInstance(why, str)

        # A runner whose exit status raises on comparison: luna and
        # kimi-k2.7-code both found `rc != 0` evaluated before any refusal
        # could be built.
        def poisoned_status(argv, **kwargs):
            if "cat-file" in argv:
                return PoisonedStatus(1), "", "fatal: nope"
            return real(argv, **kwargs)
        why = W.validate_pinned_head(poisoned_status, facts, pinned)
        self.assertIsNotNone(why, "a poisoned exit status produced no refusal")
        self.assertIsInstance(why, str)

        for shape in [Hostile(), ExitOnStr(), ["7"], {"rc": 1}, None]:
            with self.subTest(rc=type(shape).__name__):
                # render_git_diagnostic is reached with the runner's rc, so
                # the exit status is the other field with the same exposure.
                out = W.render_git_diagnostic(shape, "boom")
                self.assertIsInstance(out, str)

    def test_a_zero_valued_status_is_success(self):
        """kimi-k2.7-code: I over-corrected and refused 0.0 and False.

        The ORIGINAL `rc != 0` accepted those and refused "0". Replacing a
        working comparison with a coercion broke it in one direction, then
        in the other. The comparison is back; the only addition is that it
        cannot raise.
        """
        for status in (0, 0.0, False):
            with self.subTest(status=repr(status)):
                self.assertEqual(W._as_status(status), 0)
        # A LYING object. luna: one whose __eq__(0) returns True was
        # admitted as success, so a runner could wrap a real exit 128
        # in it and validate_pinned_head would return None instead of
        # refusing. Every earlier version of this function erred
        # towards refusing; that one erred towards admitting.
        class Liar(object):
            def __eq__(self, other):
                return True

        class Raises(object):
            def __eq__(self, other):
                raise SystemExit(1)

        for status in ("0", "00", b"0", 0.5, None, object(),
                       Liar(), Raises(), True, 128.0):
            with self.subTest(status=repr(status)[:20]):
                self.assertIs(W._as_status(status), W.UNESTABLISHED_STATUS)
                self.assertNotEqual(W._as_status(status), 0)

    def test_an_unestablished_status_answers_no_exact_question(self):
        """luna and glm-5.3: collapsing every nonzero to 1 made a fatal
        status answer YES to "is this merge-base's documented exit 1".

        Three branches in this module read an EXACT status -- `rc == 1`
        for not-an-ancestor, `rc == 2` for ls-remote's absent ref,
        `rc in (0, 1)` for config's key-not-found. A runner reporting
        statuses as strings, floats or bools reached all three through a
        value that was never 128, 2 or 1 to begin with. The sentinel is
        not an int on purpose: it equals no exact status, and it is not
        zero.
        """
        for status in ("128", 128.0, True, "2", 2.0, "1", 1.0):
            with self.subTest(status=repr(status)):
                rc = W._as_status(status)
                self.assertIs(rc, W.UNESTABLISHED_STATUS)
                self.assertTrue(rc != 0, "an unreadable status is not zero")
                self.assertFalse(rc == 1, "it is not merge-base's exit 1")
                self.assertFalse(rc == 2, "it is not ls-remote's exit 2")
                self.assertNotIn(rc, (0, 1), "it is not config's key-absent")
        # It is renderable, because every refusal carrying it is durable.
        rendered = W.render_git_diagnostic(W.UNESTABLISHED_STATUS, "fatal: x")
        self.assertIsInstance(rendered, str)
        self.assertIn("not report", rendered)
        self.assertIn("fatal: x", rendered)

    def test_the_rev_parse_branch_names_no_cause_either(self):
        """luna, in the round after the lineage branch was fixed.

        cat-file and merge-base can both exit 0 and rev-parse still fail
        -- the checkout going away between two commands is enough -- and
        "the tree could not be READ" then sends an operator after a tree
        that is fine. Third branch, same claims-more-than-it-knows shape,
        which is why the sibling in the judgment path is swept in the
        same commit rather than waiting for a fourth round.
        """
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")

        for status in (1, 128, "128", 128.0):
            with self.subTest(status=repr(status)):
                def gone(argv, _status=status, **kwargs):
                    if "rev-parse" in argv:
                        return _status, "", "fatal: not a git repository"
                    return real(argv, **kwargs)

                why = W.validate_pinned_head(gone, facts, pinned)
                self.assertIsNotNone(why)
                self.assertIn("could not be validated", why)
                self.assertNotIn("could not be read", why)
                self.assertIn("not a verdict on the tree", why)
                self.assertIn("not a git repository", why)

    def test_the_base_commit_is_not_truncated_into_one_character(self):
        """kimi-k2.7-code read `render_for_record(base, 12)` on a full
        40-character SHA as producing 'a [truncated]'.

        It does not: the marker is exactly 12 characters, so a limit of
        12 takes the `limit <= len(marker)` path and returns the first
        twelve. The finding does not reproduce -- and the code was right
        only by landing exactly on that boundary, which is why it now
        slices first, the way the produced-commit sites already did.
        """
        base = "a" * 40
        self.assertEqual(W.render_for_record(base[:12], 12), "a" * 12)
        # One character over and the coincidence would have been a defect.
        self.assertEqual(W.render_for_record(base, 13), "a [truncated]")

        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")

        def not_an_ancestor(argv, **kwargs):
            if "merge-base" in argv:
                return 1, "", ""
            return real(argv, **kwargs)

        why = W.validate_pinned_head(not_an_ancestor, facts, pinned)
        self.assertIsNotNone(why)
        self.assertIn(facts["base_commit"][:12], why,
                      "the durable record lost the base identifier")
        self.assertNotIn("[truncated]", why)

    def test_a_fatal_merge_base_is_not_a_lineage_verdict(self):
        """glm-5.3: exit 1 means "not an ancestor"; 128 means git failed.

        A checkout holding the produced commit but not the base object --
        a shallow re-clone, a corrupt pack -- made `--is-ancestor` die
        128, and the durable record then said the commit "does not descend
        from trusted base": a false lineage verdict, sending an operator
        after an off-base commit that descends fine. This is the nine-unit
        wound recurring in a sibling branch I had swept for rendering and
        not for the no-cause property.
        """
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")

        def fatal(argv, **kwargs):
            if "merge-base" in argv:
                return 128, "", "fatal: bad object deadbeef"
            return real(argv, **kwargs)

        why = W.validate_pinned_head(fatal, facts, pinned)
        self.assertIsNotNone(why)
        self.assertIn("could not be determined", why)
        self.assertIn("bad object", why)
        self.assertNotIn("does not descend", why)

        def not_an_ancestor(argv, **kwargs):
            if "merge-base" in argv:
                return 1, "", ""
            return real(argv, **kwargs)

        verdict = W.validate_pinned_head(not_an_ancestor, facts, pinned)
        self.assertIsNotNone(verdict)
        self.assertIn("does not descend", verdict)

        # The finding above fed int 128 only, so a runner that reports
        # statuses as strings, floats or bools walked straight past it --
        # luna and glm-5.3 both, in the round after the int case was
        # fixed. Every one of these is a FATAL merge-base, and none of
        # them may produce a lineage verdict.
        for status in ("128", 128.0, True, "1", 1.0):
            with self.subTest(status=repr(status)):
                def not_a_number(argv, _status=status, **kwargs):
                    if "merge-base" in argv:
                        return _status, "", "fatal: bad object deadbeef"
                    return real(argv, **kwargs)

                why = W.validate_pinned_head(not_a_number, facts, pinned)
                self.assertIsNotNone(why)
                self.assertIsInstance(why, str)
                self.assertIn("could not be determined", why)
                self.assertNotIn(
                    "does not descend", why,
                    "a status of %r produced a lineage verdict" % (status,))
                self.assertIn("bad object", why)

    def test_a_textual_zero_status_is_not_success(self):
        """luna and glm-5.3: coercing "0" ADMITTED a refused run.

        `int("0")` is 0, so a runner reporting a failed call with a
        textual status turned a refusal into a pass. The old `rc != 0`
        comparison refused it. Anything that is not already an int is a
        failure now.
        """
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")
        for status in ("0", 0.5, "00", b"0"):
            with self.subTest(status=repr(status)):
                def runner(argv, _s=status, **kwargs):
                    if "cat-file" in argv:
                        return _s, "", "fatal: it failed"
                    return real(argv, **kwargs)
                why = W.validate_pinned_head(runner, facts, pinned)
                self.assertIsNotNone(
                    why, "status %r was read as success" % (status,))

    def test_a_bytes_subclass_with_a_raising_decode_still_refuses(self):
        class BadBytes(bytes):
            def decode(self, *args, **kwargs):
                raise RuntimeError("poisoned decode")

        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")

        def runner(argv, **kwargs):
            if "cat-file" in argv:
                return 1, "", BadBytes(b"fatal: nope")
            return real(argv, **kwargs)

        why = W.validate_pinned_head(runner, facts, pinned)
        self.assertIsNotNone(why, "a poisoned decode produced no refusal")
        self.assertIsInstance(why, str)

    def test_a_bytes_diagnostic_still_produces_a_refusal(self):
        real = U.run

        def runner(argv, **kwargs):
            if "cat-file" in argv:
                return 1, "", b"fatal: unable to read object"
            return real(argv, **kwargs)

        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")
        why = W.validate_pinned_head(runner, facts, pinned)
        self.assertIsNotNone(why, "a bytes diagnostic produced no refusal")
        self.assertIn("unable to read object", why)

    def test_every_refusal_branch_carries_the_stable_prefix(self):
        """glm-5.3, filed out of scope and swept anyway: three sibling
        branches still interpolated raw. "Some refusals are rendered" is
        not a property anyone can rely on."""
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = dict(self.facts(attempt))
        pinned = self.commit("A")

        # not a descendant of the trusted base
        unrelated = dict(facts)
        unrelated["base_commit"] = "0" * 40

        def ancestor_fails(argv, **kwargs):
            if "merge-base" in argv:
                return 1, "", "fatal: not an ancestor"
            return real(argv, **kwargs)

        # the tree cannot be read
        def tree_fails(argv, **kwargs):
            if "rev-parse" in argv and "^{tree}" in " ".join(map(str, argv)):
                return 1, "", b"fatal: unreadable tree"
            return real(argv, **kwargs)

        for label, runner, facts_used in (
                ("not a descendant", ancestor_fails, facts),
                ("unreadable tree", tree_fails, facts),
        ):
            with self.subTest(branch=label):
                why = W.validate_pinned_head(runner, facts_used, pinned)
                self.assertIsNotNone(why, label)
                self.assertTrue(
                    why.startswith("pinned commit validation failed"),
                    "%s refusal lost the stable prefix: %r" % (label, why))
                self.assertNotIn("\n", why)

    def test_a_control_character_in_a_path_does_not_reach_the_record(self):
        # Collapsing mode, for git's prose.
        self.assertEqual(W.render_for_record("/tmp/a\nb\x07c", 200),
                         "/tmp/a b?c")
        # Path mode: a newline still cannot reach the record, and a space
        # is left exactly as recorded because a path may contain one.
        self.assertEqual(
            W.render_for_record("/tmp/two  spaces/x", 200, collapse=False),
            "/tmp/two  spaces/x")
        self.assertEqual(
            W.render_for_record(" /tmp/lead and trail ", 200, collapse=False),
            " /tmp/lead and trail ")
        self.assertEqual(
            W.render_for_record("/tmp/a\nb", 200, collapse=False), "/tmp/a?b")

    def test_a_recorded_path_reaches_the_refusal_unaltered(self):
        """luna and kimi-k2.7-code: the renderer trimmed a path the claim
        promised not to trim."""
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = dict(self.facts(attempt))
        pinned = self.commit("A")
        spaced = str(self.tmp / "two  spaces")
        facts["repo"] = spaced
        why = W.validate_pinned_head(U.run, facts, pinned)
        self.assertIsNotNone(why)
        self.assertIn(spaced, why,
                      "the recorded path was altered on its way into the "
                      "refusal: %r" % why)

    def test_every_interpolated_value_goes_through_the_renderer(self):
        """The claim has been refuted once per field. This asserts the
        property rather than the fields: no value reaches the refusal
        without the renderer, checked by rendering each and finding it."""
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = dict(self.facts(attempt))
        pinned = self.commit("A")
        facts["repo"] = str(self.tmp / "repo with spaces")
        real = U.run

        def runner(argv, **kwargs):
            if "cat-file" in argv:
                return 1, "", "fatal: line one\nline two"
            return real(argv, **kwargs)

        why = W.validate_pinned_head(runner, facts, pinned)
        self.assertIsNotNone(why)
        for value, collapse in ((pinned[:12], True),
                                (facts["repo"], False),
                                ("1", True)):        # the exit code
            self.assertIn(
                W.render_for_record(value, 400, collapse=collapse), why,
                "a value reached the refusal unrendered: %r" % value)
        self.assertIn("fatal: line one line two", why)
        self.assertNotIn("\n", why)

        # A runner is an injected callable; nothing enforces that its exit
        # status is an int, so the status is rendered like everything else.
        def hostile_rc(argv, **kwargs):
            if "cat-file" in argv:
                return "7\nsmuggled", "", "fatal: nope"
            return real(argv, **kwargs)

        # NO try/except. glm-5.3: wrapping this in `except Exception` and
        # then asserting only `if hostile is not None` made the contract
        # unassertable -- deleting the guard in _as_status would raise,
        # the test would swallow it, and the suite would stay green while
        # a validation failure became an exception again. A test that
        # cannot fail is not a test.
        hostile = W.validate_pinned_head(hostile_rc, facts, pinned)
        self.assertIsNotNone(hostile, "a non-int status produced no refusal")
        self.assertNotIn("\n", hostile)
        self.assertNotIn("smuggled\n", hostile)

    def test_an_empty_recorded_repository_refuses_before_running_git(self):
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = dict(self.facts(attempt))
        pinned = self.commit("A")
        facts["repo"] = ""

        def never(argv, **kwargs):
            raise AssertionError("git must not run without a repository")

        why = W.validate_pinned_head(never, facts, pinned)
        self.assertIsNotNone(why)
        self.assertIn("recorded no repository", why)

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


    def test_the_judgment_path_does_not_collapse_lineage_either(self):
        """The SIBLING, swept in the same commit rather than in a fourth
        round.

        `validate_pinned_head` was fixed to distinguish merge-base's
        documented exit 1 from a fatal one. `judge_detail` collapsed the
        same status in the same way one screen up, and it is the same
        wound: nine units read a false cause off a collapsed status. The
        mutation that put `rc != 0` back here left the suite green until
        this test existed.
        """
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        unit = {"id": "u1", "kind": "code", "repo": str(self.repo)}
        err, anchor_facts = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(err)
        self.commit("A")

        for status in (128, "128", 128.0):
            with self.subTest(status=repr(status)):
                def fatal(argv, _status=status, **kwargs):
                    if "merge-base" in argv:
                        return _status, "", "fatal: bad object deadbeef"
                    return real(argv, **kwargs)

                produced, _head, why = W.judge_detail(
                    fatal, str(attempt), unit, anchor_facts["facts"])
                self.assertFalse(produced)
                self.assertIsNotNone(why)
                self.assertIn("could not be determined", why)
                self.assertIn("not a verdict on lineage", why)
                self.assertNotIn("does not descend", why)
                self.assertIn("bad object", why)

        # Exit 1 is still the documented verdict, and still says so.
        def not_an_ancestor(argv, **kwargs):
            if "merge-base" in argv:
                return 1, "", ""
            return real(argv, **kwargs)

        produced, _head, why = W.judge_detail(
            not_an_ancestor, str(attempt), unit, anchor_facts["facts"])
        self.assertFalse(produced)
        self.assertIn("does not descend from the anchored base", why)
        # ...and says only that. luna, one round after the exit STATUS
        # stopped being collapsed: the PROSE still named a cause. A
        # sibling-branch commit, a branch already ahead at launch and a
        # force-pushed replacement all give exit 1, and only one is a
        # replacement.
        self.assertNotIn("history was replaced rather than extended", why)
        self.assertIn("does not distinguish", why)

    def test_an_unreadable_git_identity_names_no_cause(self):
        """kimi-k2.7-code: "(top is unreadable)" for every nonzero exit
        sent an operator to check permissions when git had said
        `fatal: not a git repository`."""
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        unit = {"id": "u1", "kind": "code", "repo": str(self.repo)}
        err, anchor_facts = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(err)
        self.commit("A")

        def not_a_repository(argv, **kwargs):
            if "--show-toplevel" in argv:
                return 128, "", "fatal: not a git repository"
            return real(argv, **kwargs)

        produced, _head, why = W.judge_detail(
            not_a_repository, str(attempt), unit, anchor_facts["facts"])
        self.assertFalse(produced)
        self.assertIn("could not be determined", why)
        self.assertNotIn("unreadable", why)
        self.assertIn("not a git repository", why,
                      "git's own words did not reach the record")

    def test_head_equal_to_base_names_no_history(self):
        """luna: "nothing was committed" from HEAD == base alone.

        A run that commits and then resets leaves exactly this state,
        so the message named a history the check never observed.
        """
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        unit = {"id": "u1", "kind": "code", "repo": str(self.repo)}
        err, anchor_facts = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(err)

        produced, _head, why = W.judge_detail(
            real, str(attempt), unit, anchor_facts["facts"])
        self.assertFalse(produced)
        self.assertIn("produced nothing to judge", why)
        self.assertNotIn("nothing was committed", why)
        self.assertIn("does not distinguish", why)

    def test_a_repository_that_is_not_a_directory_names_no_cause(self):
        """kimi-k2.7-code: `not os.path.isdir(repo)` reported the
        repository as GONE, which is also true of a regular file or a
        path this process cannot stat.

        The branch is reachable only by a race: `workspace_identity_problem`
        stats the workspace and runs git in it first, so by the time
        this check runs the path was a directory a moment ago. The
        window is narrow and that is exactly why the message matters --
        something changed underneath the attempt between two lines, and
        "is gone" picks one explanation out of several the process
        never saw.

        Patched rather than raced, because the message is what the
        finding was about and a timing window is not something to
        reproduce by luck.
        """
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        unit = {"id": "u1", "kind": "code", "repo": str(self.repo)}
        err, anchor_facts = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(err)
        self.commit("A")

        real_isdir = W.os.path.isdir
        workspace = anchor_facts["facts"]["execution_workspace"]

        def vanished(path):
            if str(path) == str(workspace):
                return False
            return real_isdir(path)

        W.os.path.isdir = vanished
        try:
            produced, _head, why = W.judge_detail(
                U.run, str(attempt), unit, anchor_facts["facts"])
        finally:
            W.os.path.isdir = real_isdir

        self.assertFalse(produced)
        self.assertIn("not a directory", why)
        self.assertNotIn("is gone", why)
        self.assertIn("does not distinguish", why)
        self.assertIn(str(workspace), why)

    def test_a_poisoned_str_subclass_cannot_escape_the_boundary(self):
        """luna, one round after the isinstance fix, and the fourth time
        this boundary has been claimed one value short.

        `type(value) is str` sends a subclass to the `str(value)` branch
        -- but `str()` RETURNS THE SUBCLASS when handed one, and so does
        `bytes.decode` when overridden, so the subclass this branch
        exists to defuse walked straight through it and `_git` called
        its poisoned `.strip()` anyway.
        """
        class Poison(str):
            def strip(self, *args):
                raise RuntimeError("poisoned")

            def __str__(self):
                return self

            def __radd__(self, other):
                # The hole glm-5.3 named: when the right operand is a
                # str SUBCLASS, Python gives it first crack at __radd__,
                # so `"" + poison` returns the poison. My first repair
                # cited that rule as the reason it was safe.
                return self

            def encode(self, *args, **kwargs):
                return b"lies"

        class PoisonBytes(bytes):
            def decode(self, *args, **kwargs):
                return Poison("from bytes")

        class NotEvenAStr(object):
            def __radd__(self, other):
                return 42

        class DecodesToNonStr(bytes):
            def decode(self, *args, **kwargs):
                return NotEvenAStr()

        for label, value in (("a str subclass", Poison("hello")),
                             ("a bytes subclass", PoisonBytes(b"x")),
                             ("decode returning a non-str",
                              DecodesToNonStr(b"z"))):
            with self.subTest(value=label):
                out = W._as_text(value)
                self.assertIs(type(out), str,
                              "%s escaped the boundary as %s"
                              % (label, type(out).__name__))
                self.assertEqual(out.strip(), out.strip())

        # And end to end: a runner returning one must still produce a
        # refusal rather than an exception.
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        facts = self.facts(attempt)
        pinned = self.commit("A")

        def poisoned(argv, **kwargs):
            if "cat-file" in argv:
                return 1, Poison(""), Poison("fatal: poisoned")
            return real(argv, **kwargs)

        why = W.validate_pinned_head(poisoned, facts, pinned)
        self.assertIsNotNone(why)
        self.assertIsInstance(why, str)
        self.assertIn("poisoned", why)

    def test_no_refusal_interpolates_a_value_the_renderer_never_saw(self):
        """The one-renderer property, enforced instead of asserted.

        Four rounds running I claimed every interpolated value goes
        through render_for_record, and four rounds running a reviewer
        found a field that did not -- the last one `rec['branch']`,
        where a 10,000-character recorded branch reached a durable
        refusal whole. Fixing the field each reviewer names is the
        sibling-sweep failure, and my "sweep" had been by eye.

        kimi-k2.7-code also showed why a behavioural test cannot finish
        the job: at the merge-base exit-1 sites the values are already
        validated as hex object ids, so rendering is the identity and a
        bypass there is unobservable no matter what a test feeds. The
        property is syntactic, so this checks the syntax: no f-string in
        either function may interpolate anything that has not been
        through a renderer.

        The allowances are named rather than pattern-matched, and each
        one is a value this module produces itself.

        SCOPED to the functions this change owns. An AST sweep of the
        whole module finds 86 raw interpolations across 18 functions,
        which is a rendering audit rather than a fix for the
        missing-repository defect, and is filed separately. Widening
        this tuple is how that audit gets done one function at a time
        without the property silently reverting behind it.
        """
        source = inspect.getsource(W)
        tree = ast.parse(source)
        # Each name here is a local this module rendered itself, one
        # line earlier, and the reason is stated rather than inferred
        # from a pattern. Adding one is a deliberate edit a reviewer
        # can weigh; matching a shape would let any local through.
        allowed_bare = {
            "shown_repo",            # render_for_record of the repo path
            "said",                  # render_for_record of git's stderr
            "PIN_VALIDATION_REFUSAL",  # this module's own constant
        }
        offenders = []
        for node in ast.walk(tree):
            if (not isinstance(node, ast.FunctionDef)
                    or node.name not in RENDERED_REFUSAL_FUNCTIONS):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.JoinedStr):
                    continue
                for value in sub.values:
                    if not isinstance(value, ast.FormattedValue):
                        continue
                    expression = ast.get_source_segment(source, value.value)
                    # By STRUCTURE, not by substring. luna: the first
                    # version skipped any expression CONTAINING a
                    # renderer's name, so `f"{repo + render_for_record('', 1)}"`
                    # passed with `repo` raw, and anything starting
                    # `len(` passed whatever followed. A guard written
                    # to be unfoolable that can be fooled by a substring
                    # is the same defect this repository fixed in the
                    # skill-shape test one branch over.
                    #
                    # The interpolated expression must BE a call to a
                    # renderer, or a bare name this module produces.
                    if _is_rendered(value.value, allowed_bare):
                        continue
                    offenders.append(
                        (node.name, value.lineno, expression or "?"))
        self.assertEqual(
            offenders, [],
            "a refusal interpolates a value the renderer never saw. Every "
            "value either goes through render_for_record, or is named in "
            "allowed_bare here with a reason.")

    def test_a_recorded_branch_cannot_flood_a_durable_refusal(self):
        """luna: `rec['branch']!r` put the whole recorded branch in the
        record, so a 10,000-character branch produced a refusal nobody
        can read and nothing can bound."""
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        unit = {"id": "u1", "kind": "code", "repo": str(self.repo)}
        err, anchor_facts = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(err)
        self.commit("A")
        facts = dict(anchor_facts["facts"])
        facts["branch"] = "b" * 10000 + "\n\t\x00"

        produced, _head, why = W.judge_detail(
            real, str(attempt), unit, facts)
        self.assertFalse(produced)
        self.assertIn("anchored on", why)
        self.assertLess(len(why), 2000,
                        "a recorded branch flooded the durable refusal")
        self.assertTrue(why.isprintable(), repr(why[:200]))

    def test_judge_detail_renders_every_value_it_records(self):
        """luna, kimi-k2.7-code and glm-5.3, all three in one round.

        I rewrote these refusals and left them interpolating `repo!r`,
        `head[:12]` and `str(base)[:12]` raw while claiming one renderer
        at one boundary. render_for_record's own docstring already
        records that claim running ahead of the code three times, once
        per field. This is the fourth, so the property is asserted here
        rather than argued: a runner that returns a head full of control
        characters, against a recorded path far past the bound, must
        still produce a bounded printable refusal.
        """
        real = U.run
        attempt = self.tmp / "runs" / "u1" / "att1"
        attempt.mkdir(parents=True)
        long_repo = str(self.repo)
        unit = {"id": "u1", "kind": "code", "repo": long_repo}
        err, anchor_facts = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(err)
        self.commit("A")
        # base_commit stays VALID. Making it hostile got the attempt
        # refused by launch_facts_problem before any of these branches
        # ran, which is why four renderer mutations survived the first
        # version of this test: it was asserting on a message from a
        # different function.
        facts = anchor_facts["facts"]

        def hostile_head(argv, **kwargs):
            if "merge-base" in argv:
                return 1, "", ""
            if argv[-1] == "HEAD" and "rev-parse" in argv:
                return 0, "d\x01\te" + "f" * 400, ""
            return real(argv, **kwargs)

        # Each branch separately: a mutation that un-renders the
        # could-not-be-determined message is invisible if only the
        # exit-1 message is exercised, which is how my first version of
        # this test let two of its own mutations through.
        for label, status in (("not an ancestor", 1),
                              ("a fatal merge-base", 128)):
            with self.subTest(branch=label):
                def hostile(argv, _status=status, **kwargs):
                    if "merge-base" in argv:
                        return _status, "", "fatal: bad object"
                    if ("rev-parse" in argv and argv[-1] == "HEAD"
                            and "--abbrev-ref" not in argv):
                        return 0, "d\x01\te" + "f" * 400, ""
                    return real(argv, **kwargs)

                produced, _head, why = W.judge_detail(
                    hostile, str(attempt), unit, facts)
                self.assertFalse(produced)
                self.assertIsInstance(why, str)
                self.assertTrue(
                    why.isprintable(),
                    "a refusal reached the record unprintable: %r" % why)
                for forbidden in ("\x00", "\x01", "\n", "\t"):
                    self.assertNotIn(forbidden, why)
                self.assertIn("?", why,
                              "the hostile head reached the record intact, "
                              "so this branch never rendered it")

        # And the repository path goes through the renderer rather than
        # repr(), which is the difference all three reviewers named. A
        # rendered path appears bare; `{repo!r}` wraps it in quotes.
        def head_unreadable(argv, **kwargs):
            if ("rev-parse" in argv and argv[-1] == "HEAD"
                    and "--abbrev-ref" not in argv):
                return 128, "", "fatal: not a git repository"
            return real(argv, **kwargs)

        produced, _head, why = W.judge_detail(
            head_unreadable, str(attempt), unit, anchor_facts["facts"])
        self.assertFalse(produced)
        # Against the path judge_detail actually saw, not the one this
        # test wrote: on macOS /var is a symlink to /private/var, and
        # comparing against the unresolved spelling made both the
        # rendered and the repr() form fail to match, so the assertion
        # pair could not tell them apart and four mutations walked past
        # it.
        recorded = anchor_facts["facts"]["repo"]
        self.assertIn(recorded, why)
        self.assertNotIn("'%s'" % recorded, why,
                         "the path went through repr(), not the renderer")

        # The tree branch is the fourth message and was reached by
        # nothing, so its mutation survived while the other three fell.
        def tree_unreadable(argv, **kwargs):
            if any(a.endswith("^{tree}") for a in argv):
                return 128, "", "fatal: not a git repository"
            return real(argv, **kwargs)

        produced, _head, why = W.judge_detail(
            tree_unreadable, str(attempt), unit, anchor_facts["facts"])
        self.assertFalse(produced)
        self.assertIn("could not be validated", why)
        self.assertIn(recorded, why)
        self.assertNotIn("'%s'" % recorded, why,
                         "the tree branch bypassed the renderer")


class TestEvidenceRecordAuthorityKeys(unittest.TestCase):
    def test_repository_location_is_an_authority_input(self):
        rec = W.EvidenceRecord({"repo": "/agent/chosen/repository"})
        with self.assertRaises(W.AuthorityFromEvidence):
            rec.get("repo")


if __name__ == "__main__":
    unittest.main()
