"""Bounded continuation, inside a code attempt.

Both committee members were emphatic that this must NOT become a unit state or
a retry mode, and that no Slurm analogue should exist. A conversational turn is
not a retry boundary: a planning-only turn can settle while the session,
worktree, launch identity, attempt root and budget all remain valid, so minting
a fresh attempt would discard the context and spend a retry on what is really a
provider liveness defect.

The danger is that it becomes a correction loop. It answers exactly one
condition, and the bound is declared in the plan rather than passed by whoever
runs advance.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402
import unit as U  # noqa: E402


class TestOnlyOneConditionTriggersIt(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []
        self._real = S.U.run
        S.U.run = lambda argv, timeout=None: (
            self.sent.append(argv) or (0, "", ""))
        self._reason = S._receipt_reason
        S._receipt_reason = lambda st, uid, d: U.REASON_NO_OUTPUTS

    def tearDown(self):
        S.U.run = self._real
        S._receipt_reason = self._reason
        self.tmp.cleanup()

    def _u(self, **over):
        u = {"id": "u1", "kind": "code", "repo": "/tmp/fixture-repo", "target_branch": "main", "mode": "bypass", "continuation": {"max": 2}}
        u.update(over)
        return u

    def _us(self, **over):
        us = {"job_id": "agent-1", "attempt_dir": "/tmp/att", "state": "INCOMPLETE"}
        us.update(over)
        return us

    def _continue(self, us, report=None, u=None):
        state = {"schema_version": 1, "units": {"u1": us}}
        return S.maybe_continue(
            self.tmp.name, "u1", u or self._u(), us,
            report if report is not None else [], state)

    def test_settled_without_producing_sends_one(self):
        us = self._us()
        report = []
        self.assertTrue(self._continue(us, report))
        self.assertEqual(len(us["continuations"]), 1)
        self.assertEqual(us["state"], "RUNNING")
        self.assertIn("SAME attempt", " ".join(report))

    def test_a_permission_block_is_never_prodded(self):
        """NEEDS_HUMAN means a person must answer. Sending a message at it
        does not make the person appear."""
        S._receipt_reason = lambda st, uid, d: U.REASON_NO_EVIDENCE
        self.assertFalse(
            self._continue(self._us()))
        self.assertEqual(self.sent, [])

    def test_an_undeclared_bound_means_no_continuation(self):
        self.assertFalse(self._continue(
            self._us(), u=self._u(continuation=None)))

    def test_the_bound_is_honoured(self):
        us = self._us(continuations=[{"n": 1}, {"n": 2}])
        self.assertFalse(self._continue(us))

    def test_one_below_the_bound_still_sends(self):
        us = self._us(continuations=[{"n": 1}])
        self.assertTrue(self._continue(us))

    def test_a_unit_with_no_agent_is_not_prodded(self):
        self.assertFalse(self._continue(self._us(job_id=None)))

    def test_only_a_code_unit(self):
        self.assertFalse(self._continue(
            self._us(), u=self._u(kind="slurm")))

    def test_every_continuation_is_recorded(self):
        us = self._us()
        self._continue(us)
        entry = us["continuations"][0]
        for field in ("at", "n", "of", "prompt_sha256", "sent"):
            self.assertIn(field, entry)

    def test_the_prompt_comes_from_the_plan_not_the_caller(self):
        us = self._us()
        self._continue(
            us, u=self._u(continuation={"max": 1, "prompt": "GO ON"}))
        self.assertIn("GO ON", self.sent[-1])

    def test_a_send_failure_is_recorded_and_does_not_resume(self):
        S.U.run = lambda argv, timeout=None: (1, "", "daemon down")
        us = self._us()
        self.assertFalse(self._continue(us))
        self.assertFalse(us["continuations"][0]["sent"])
        self.assertNotEqual(us.get("state"), "RUNNING")

    def test_continuation_is_charged_durably_before_send(self):
        us = self._us()
        state = {"schema_version": 1, "units": {"u1": us}}

        def observe(argv, timeout=None):
            durable = S.load_state(self.tmp.name)
            entry = durable["units"]["u1"]["continuations"][0]
            self.assertEqual(entry["status"], "pending")
            self.assertIsNone(entry["sent"])
            return 0, "", ""

        S.U.run = observe
        self.assertTrue(S.maybe_continue(
            self.tmp.name, "u1", self._u(), us, [], state))

    def test_crash_after_pending_save_leaves_the_slot_retryable(self):
        class Crash(BaseException):
            pass

        us = self._us()
        state = {"schema_version": 1, "units": {"u1": us}}
        S.U.run = lambda argv, timeout=None: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            S.maybe_continue(
                self.tmp.name, "u1", self._u(), us, [], state)

        recovered = S.load_state(self.tmp.name)
        recovered_us = recovered["units"]["u1"]
        self.assertEqual(len(recovered_us["continuations"]), 1)
        self.assertEqual(recovered_us["continuations"][0]["status"],
                         "pending")
        S.U.run = lambda argv, timeout=None: (0, "", "")
        self.assertTrue(S.maybe_continue(
            self.tmp.name, "u1", self._u(), recovered_us, [], recovered))
        self.assertEqual(len(recovered_us["continuations"]), 1)
        self.assertTrue(recovered_us["continuations"][0]["sent"])


def _paseo_stub_on_path(case):
    """Put a stub `paseo` on PATH for the duration of one test.

    Every unit in the class below is kind=code, and `validate_plan` refuses
    those unless `paseo` resolves, so on a host without it these tests never
    reached their own assertion: three failed, and the ones asserting only
    "some PlanError" passed on the WRONG refusal, which is worse than red.

    Same PATH seam as test_swarm.py::_fake_scheduler; the reasoning, and the
    test that pins the refusal itself, are in tests/test_plan_shape.py. The
    stub is never executed -- validate only asks shutil.which -- and exits
    127 so a test that starts needing a real paseo fails loudly."""
    d = tempfile.mkdtemp(prefix="fake-paseo-")
    stub = Path(d) / "paseo"
    stub.write_text("#!/bin/sh\necho 'test stub, not a real paseo' >&2\n"
                    "exit 127\n")
    stub.chmod(0o755)
    old = os.environ.get("PATH", "")
    case.addCleanup(shutil.rmtree, d, True)
    case.addCleanup(os.environ.__setitem__, "PATH", old)
    os.environ["PATH"] = d + os.pathsep + old


class TestTheBoundIsDeclaredAndChecked(unittest.TestCase):

    def setUp(self):
        _paseo_stub_on_path(self)

    def _plan(self, **over):
        u = {"id": "u1", "kind": "code", "repo": "/tmp/fixture-repo", "target_branch": "main", "mode": "bypass", "outputs": ["o"], "runtime": "none"}
        u.update(over)
        return {"project": "p", "units": [u]}

    def test_a_non_numeric_bound_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._plan(continuation={"max": "lots"}))
        self.assertIn("not a bound", str(c.exception))

    def test_zero_is_refused(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan(self._plan(continuation={"max": 0}))

    def test_a_bare_value_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._plan(continuation=3))
        self.assertIn("must be an object", str(c.exception))

    def test_a_slurm_unit_may_not_declare_one(self):
        """A Slurm job that exits is retried, not prodded."""
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan({"project": "p", "units": [
                {"id": "u1", "kind": "slurm", "outputs": ["o"],
                 "runtime": "none", "command": "true",
                 "continuation": {"max": 2}}]})
        self.assertIn("not prodded", str(c.exception))

    def test_a_declared_bound_validates(self):
        S.validate_plan(self._plan(continuation={"max": 2}))


class TestItIsNotAUnitState(unittest.TestCase):

    def test_no_continuing_state_was_invented(self):
        self.assertNotIn("CONTINUING", U.STATES)
        self.assertNotIn("CONTINUING", (SCRIPTS / "swarm.py").read_text())

    def test_there_is_no_slurm_analogue(self):
        import ast
        src = (SCRIPTS / "swarm.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "maybe_continue")
        body = ast.unparse(fn)
        self.assertIn("'code'", body.replace('"', "'"))
        self.assertNotIn("sbatch", body)
        self.assertNotIn("scontrol", body)

    def test_exhaustion_fails_rather_than_looping(self):
        src = (SCRIPTS / "swarm.py").read_text()
        self.assertIn("fails for missing production evidence", src)


if __name__ == "__main__":
    unittest.main()
