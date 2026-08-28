#!/usr/bin/env python3
"""test_swarm.py — the unit contract and the coordinator.

Acceptance criteria from docs/plan-swarm.md steps 1 and 2, written by the
committee that planned them. Python 3.8+, stdlib only.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "hanig-swarm" / "scripts"
UNIT, SWARM = SCRIPTS / "unit.py", SCRIPTS / "swarm.py"

_s = importlib.util.spec_from_file_location("unit", UNIT)
unit = importlib.util.module_from_spec(_s); _s.loader.exec_module(unit)
_s2 = importlib.util.spec_from_file_location("swarm", SWARM)
swarm = importlib.util.module_from_spec(_s2); _s2.loader.exec_module(swarm)


def run(script, *argv, cwd=None):
    return subprocess.run([sys.executable, str(script), *argv],
                          capture_output=True, text=True, cwd=cwd, timeout=300)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def plan(self, obj):
        p = self.tmp / "plan.json"
        p.write_text(json.dumps(obj))
        return str(p)

    def swarm(self, *argv):
        return run(SWARM, *argv, cwd=str(self.tmp))


class TestIsolationIsTheMechanism(Base):
    """The whole design rests on one property: the write root is exclusive.
    Three plan versions died trying to prove attribution instead."""

    def alloc(self, task="t", kind="slurm", *extra):
        r = run(UNIT, "allocate", "--root", ".swarm/runs", "--task", task,
                "--kind", kind, "--output", "out.txt", *extra,
                cwd=str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()[-1]

    def test_two_attempts_never_share_a_write_root(self):
        """1(a). mkdir(exist_ok=False) IS the enforcement."""
        a, b = self.alloc(), self.alloc()
        self.assertNotEqual(a, b)
        self.assertTrue(Path(a).is_dir() and Path(b).is_dir())

    def test_allocate_succeeds_with_exit_zero(self):
        """A successful allocation returned a unit STATE (exit 1) at first,
        conflating "the command worked" with "the unit is running"."""
        r = run(UNIT, "allocate", "--root", ".swarm/runs", "--task", "z",
                "--kind", "slurm", "--output", "o", cwd=str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_output_escaping_the_root_is_refused(self):
        d = self.alloc("esc", "slurm", "--output", "../../../etc/passwd")
        r = run(UNIT, "check", d, cwd=str(self.tmp))
        self.assertEqual(r.returncode, unit.STATES["INCOMPLETE"])
        self.assertIn("outside the exclusive write root", r.stdout)

    def test_no_attribution_check_exists_anywhere(self):
        """The drift guard, on the MECHANISM not the phrase: my first version
        grepped for "wrote this" and matched a comment explaining its absence."""
        src = UNIT.read_text()
        for banned in ("production_window", "written_in_window", "foreign_write",
                       "owned_before", "owned_after", "appeared_in_window"):
            self.assertNotIn(banned, src,
                             f"{banned} is attribution machinery; isolation "
                             f"replaced it")

    def test_the_predicate_stays_under_the_size_guard(self):
        """Committee, verbatim: "if the surviving module grows past ~300 lines
        or reacquires any 'the command wrote this' check, stop"."""
        src = UNIT.read_text()
        i = src.index("# The unit contract. Everything above")
        new = [l for l in src[i:].splitlines()
               if l.strip() and not l.strip().startswith("#")]
        self.assertLess(len(new), 340, f"the predicate has grown to {len(new)} "
                                       f"executable lines; the guard is ~300")

    def test_a_pipeline_receipt_admits_its_interior_is_unjudged(self):
        d = self.alloc("pipe", "pipeline")
        run(UNIT, "bind", d, "--job-id", "42", cwd=str(self.tmp))
        run(UNIT, "check", d, cwd=str(self.tmp))
        basis = json.loads((Path(d) / "receipt.json").read_text())["basis"]
        self.assertFalse(basis["interior_judged"])
        self.assertFalse(basis["attribution_by_observation"])

    def test_a_code_unit_delegates_rather_than_duplicating(self):
        d = self.alloc("code", "code")
        r = run(UNIT, "check", d, cwd=str(self.tmp))
        self.assertIn("bus await", r.stdout)

    def test_an_unbound_attempt_is_incomplete_not_done(self):
        d = self.alloc()
        r = run(UNIT, "check", d, cwd=str(self.tmp))
        self.assertEqual(r.returncode, unit.STATES["INCOMPLETE"])

    def test_rebinding_an_attempt_is_refused(self):
        d = self.alloc()
        self.assertEqual(
            run(UNIT, "bind", d, "--job-id", "1", cwd=str(self.tmp)).returncode, 0)
        r = run(UNIT, "bind", d, "--job-id", "2", cwd=str(self.tmp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already bound", r.stderr)

    def test_the_lifted_slurm_knowledge_still_works(self):
        """1(b), (e), (f). Bought against a real scheduler; must survive the lift."""
        self.assertFalse(unit.exit_code_is_clean("0:0:0"))
        self.assertTrue(unit.exit_code_is_clean("0:0"))
        self.assertIsNone(unit.parse_iso_ts("Unknown"))
        self.assertIn("CANCELLED", unit.SLURM_FAILED)
        self.assertIn("REQUEUED", unit.SLURM_PREEMPTED)
        # one instant, three renderings, one epoch
        base = unit.parse_iso_ts("2026-08-27T16:22:32+0000")
        self.assertEqual(base, unit.parse_iso_ts("2026-08-27T09:22:32-0700"))
        self.assertEqual(base, unit.parse_iso_ts("2026-08-27T18:22:32+0200"))


class TestPlanValidation(Base):
    """Refuse before dispatching: a plan that cannot run should not half-run."""

    def bad(self, obj, expect):
        r = self.swarm("validate", self.plan(obj))
        self.assertNotEqual(r.returncode, 0, f"accepted: {obj}")
        self.assertIn(expect, r.stderr)

    def test_a_cycle_is_refused(self):
        self.bad({"units": [
            {"id": "A", "kind": "slurm", "command": "x", "outputs": ["o"],
             "needs": ["B"]},
            {"id": "B", "kind": "slurm", "command": "x", "outputs": ["o"],
             "needs": ["A"]}]}, "cycle")

    def test_concurrent_units_may_not_share_a_write_scope(self):
        self.bad({"units": [
            {"id": "A", "kind": "slurm", "command": "x", "outputs": ["o"],
             "write_scopes": ["r/"]},
            {"id": "B", "kind": "slurm", "command": "x", "outputs": ["o"],
             "write_scopes": ["r/sub/"]}]}, "overlap")

    def test_ordered_units_MAY_share_a_write_scope(self):
        """They cannot run concurrently, so exclusivity is not threatened.
        Refusing this would block honest plans."""
        r = self.swarm("validate", self.plan({"units": [
            {"id": "A", "kind": "slurm", "command": "x", "outputs": ["o"],
             "write_scopes": ["r/"]},
            {"id": "B", "kind": "slurm", "command": "x", "outputs": ["o"],
             "needs": ["A"], "write_scopes": ["r/sub/"]}]}))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_unit_with_no_outputs_is_refused(self):
        self.bad({"units": [{"id": "A", "kind": "slurm", "command": "x",
                             "outputs": []}]}, "no outputs")

    def test_duplicate_ids_are_refused(self):
        self.bad({"units": [
            {"id": "A", "kind": "slurm", "command": "x", "outputs": ["o"]},
            {"id": "A", "kind": "slurm", "command": "x", "outputs": ["o"]}]},
            "duplicate")

    def test_a_missing_dependency_is_refused(self):
        self.bad({"units": [{"id": "A", "kind": "slurm", "command": "x",
                             "outputs": ["o"], "needs": ["Z"]}]}, "not in the plan")


class TestCoordinator(Base):
    """Step 2's acceptance criteria."""

    THREE = {"name": "t", "budget": {"gpu_hours": 100}, "units": [
        {"id": "A", "kind": "slurm", "command": "true", "outputs": ["o"],
         "gpu_hours": 1, "write_scopes": ["r/A/"]},
        {"id": "B", "kind": "slurm", "command": "true", "outputs": ["o"],
         "needs": ["A"], "gpu_hours": 1, "write_scopes": ["r/B/"]},
        {"id": "C", "kind": "slurm", "command": "true", "outputs": ["o"],
         "needs": ["A"], "gpu_hours": 1, "write_scopes": ["r/C/"]}]}

    def state(self):
        return json.loads(
            (self.tmp / ".swarm/state/swarm-state.json").read_text())

    def mark(self, uid, st):
        """Force a terminal state, and CLEAR the live attempt.

        `advance` re-checks any unit with a live attempt_dir and the re-check is
        authoritative -- correctly, since the coordinator must never trust its
        own cached opinion over the predicate. Off-cluster there is no sacct
        row, so the re-check returns INCOMPLETE and overwrites an injected
        FAILED. Clearing attempt_dir models a unit whose attempt has been
        judged and reaped, which is what the DAG logic under test cares about.
        The alternative would be faking sacct, which tests the fake."""
        p = self.tmp / ".swarm/state/swarm-state.json"
        s = json.loads(p.read_text())
        rec = s["units"].setdefault(uid, {"attempts": [], "gpu_hours": 0})
        rec["state"] = st
        if st in ("DONE", "FAILED"):
            rec["attempt_dir"] = None
        p.write_text(json.dumps(s))

    def test_downstream_units_wait_for_their_dependency(self):
        """2(a)."""
        p = self.plan(self.THREE)
        self.swarm("run", p, "--dry-run")
        st = self.state()["units"]
        self.assertEqual(st["A"]["state"], "SUBMITTED")
        self.assertIsNone(st.get("B", {}).get("attempt_dir"))
        self.assertIsNone(st.get("C", {}).get("attempt_dir"))

    def test_rerunning_does_not_resubmit_or_duplicate_attempts(self):
        """2(b). The coordinator is expected to be killed and restarted."""
        p = self.plan(self.THREE)
        self.swarm("run", p, "--dry-run")
        first = self.state()["units"]["A"]["attempt_dir"]
        self.swarm("run", p, "--dry-run")
        self.assertEqual(self.state()["units"]["A"]["attempt_dir"], first)
        self.assertEqual(len(self.state()["units"]["A"]["attempts"]), 1)

    def test_a_done_dependency_releases_its_dependents(self):
        p = self.plan(self.THREE)
        self.swarm("run", p, "--dry-run")
        self.mark("A", "DONE")
        self.swarm("advance", p, "--dry-run")
        st = self.state()["units"]
        self.assertEqual(st["B"]["state"], "SUBMITTED")
        self.assertEqual(st["C"]["state"], "SUBMITTED")

    def test_exceeding_the_budget_halts_and_names_the_skipped_unit(self):
        """2(c). Charged on DISPATCH: a budget counting only finished work
        cannot stop a runaway."""
        p = self.plan({"name": "b", "budget": {"gpu_hours": 5}, "units": [
            {"id": "X", "kind": "slurm", "command": "true", "outputs": ["o"],
             "gpu_hours": 4, "write_scopes": ["r/X/"]},
            {"id": "Y", "kind": "slurm", "command": "true", "outputs": ["o"],
             "gpu_hours": 4, "write_scopes": ["r/Y/"]}]})
        r = self.swarm("run", p, "--dry-run")
        self.assertEqual(r.returncode, swarm.EXIT_HALTED)
        self.assertIn("Y", r.stdout)
        self.assertIn("budget", r.stdout)

    def test_a_failed_upstream_holds_its_dependents_transitively(self):
        """The HELD branch was DEAD CODE: a FAILED dep is also not DONE, so
        `unmet` was non-empty and the loop skipped past HELD every time.
        "Waiting" and "will never run" need different actions from a reader."""
        p = self.plan({"name": "h", "units": [
            {"id": "P", "kind": "slurm", "command": "true", "outputs": ["o"],
             "write_scopes": ["r/P/"]},
            {"id": "Q", "kind": "slurm", "command": "true", "outputs": ["o"],
             "needs": ["P"], "write_scopes": ["r/Q/"]},
            {"id": "R", "kind": "slurm", "command": "true", "outputs": ["o"],
             "needs": ["Q"], "write_scopes": ["r/R/"]}]})
        self.swarm("run", p, "--dry-run")
        self.mark("P", "FAILED")
        self.swarm("advance", p, "--dry-run")
        st = self.state()["units"]
        self.assertEqual(st["Q"]["state"], "HELD")
        self.assertEqual(st["R"]["state"], "HELD",
                         "HELD did not propagate past one hop")

    def test_status_works_with_the_coordinator_stopped(self):
        """3(d) in advance: the human view reads durable state only."""
        p = self.plan(self.THREE)
        self.swarm("run", p, "--dry-run")
        r = self.swarm("status", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        for uid in ("A", "B", "C"):
            self.assertIn(uid, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
