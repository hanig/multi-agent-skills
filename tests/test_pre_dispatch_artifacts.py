#!/usr/bin/env python3
"""B1: an artifact that was already there is not evidence this attempt made it.

The failure this is written against, from the field report: post-hoc
observation cannot tell an input from an output. A unit that passed its input
path to the receipt with no `--out` recorded a file it never wrote as produced
evidence and read DONE. The done predicate's premise -- "the write root is
exclusive, so an artifact found there was produced here" -- needs the write
root to have been empty of that artifact when the attempt was dispatched, and
nothing ever checked it.

So the bar for these tests is not "the comparator works". It is: a unit whose
declared output existed before dispatch and did not change must not read DONE,
anywhere a reader looks -- coordinator state OR the receipt, because
report.py deliberately lets an attested receipt outrank state.

Python 3.8+, stdlib only.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402
import unit as U  # noqa: E402
import worktree as W  # noqa: E402


class Base(unittest.TestCase):
    """One external run root, one coordinator state directory, no scheduler.

    Every attempt here is fabricated at the point a real dispatch would have
    reached: the write root exists, `unit.json` is written, and the basis is
    whatever the test wants coordinator state to hold. That is the only way to
    reach the hole at all -- through the dispatch loop the write root is
    always brand new, so the artifact is always absent and the gate always
    passes. The hole opens when something puts a file there first: a sibling
    unit writing into a neighbour's root, an operator staging an input, the
    code-launch recovery path re-entering `_submit`, or a continuation running
    a second turn in the same directory.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.root = self.tmp / "runs"

    def attempt(self, outputs=("o.txt",), task="u", attempt_id="att1",
                job="4242", kind="slurm"):
        d = self.root / task / attempt_id
        d.mkdir(parents=True)
        (d / U.UNIT).write_text(json.dumps({
            "schema_version": 1, "task_id": task, "attempt_id": attempt_id,
            "kind": kind, "job_id": job, "bound_at": U.now_iso(),
            "declared_outputs": list(outputs),
            "created_at": U.now_iso(), "created_at_epoch": time.time()}))
        return d

    def sacct_on_path(self):
        """A COMPLETED row for any job, owned by an attempt declared just now.

        Submit and End come from `date` inside the stub, which is the
        ownership window `sacct_row_is_ours` enforces.
        """
        b = self.tmp / "bin"
        b.mkdir(exist_ok=True)
        (b / "sacct").write_text(
            "#!/bin/sh\nnow=$(date +%Y-%m-%dT%H:%M:%S)\n"
            'echo "COMPLETED|0:0|$now|$now"\n')
        (b / "sacct").chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{b}{os.pathsep}{old}"
        self.addCleanup(os.environ.__setitem__, "PATH", old)

    def plan(self, outputs=("o.txt",), task="u"):
        return {"name": "p", "units": [
            {"id": task, "kind": "slurm", "runtime": "none",
             "command": "true", "outputs": list(outputs),
             "write_scopes": [f"{task}/"]}]}

    def state(self, attempt, basis=None, task="u", job="4242"):
        us = {"state": "SUBMITTED", "job_id": job,
              "attempt_dir": str(attempt), "attempts": [str(attempt)],
              "gpu_hours": 0}
        if basis is not None:
            us["attempt_artifact_bases"] = {Path(attempt).name: basis}
        return {"schema_version": 1, "halted": None, "units": {task: us}}

    def advance(self, plan, state):
        self.sacct_on_path()
        ok, why = S.acquire_lease(str(self.state_dir))
        self.assertTrue(ok, why)
        self.addCleanup(S.release_lease, str(self.state_dir))
        S.save_state(str(self.state_dir), state)
        report, _dispatched, _halted = S.advance(
            plan, S.load_state(str(self.state_dir)), str(self.state_dir),
            str(self.root), False, max_new=0)
        return report, S.load_state(str(self.state_dir))

    def verdict(self, attempt, state, task="u"):
        """(coordinator state, receipt state). Both, because report.py lets an
        attested receipt outrank coordinator state, so a gate that only
        corrected state would still be reported DONE."""
        receipt = json.loads((Path(attempt) / U.RECEIPT).read_text())
        return (state["units"][task]["state"], receipt["state"],
                receipt["notes"])


class TestTheHoleThatReachedDone(Base):
    """The exact reported case, and the control that gives it meaning."""

    def test_an_output_that_predates_dispatch_and_is_untouched_is_not_done(self):
        attempt = self.attempt()
        (attempt / "o.txt").write_text("i am an input\n")
        # The coordinator looked BEFORE dispatch and found it already there.
        basis = S._capture_artifact_basis(
            {}, "u", str(attempt), {"outputs": ["o.txt"]})
        self.assertIn("o.txt", basis["present"])
        _report, state = self.advance(self.plan(), self.state(attempt, basis))
        stored, claimed, notes = self.verdict(attempt, state)
        self.assertNotEqual(stored, "DONE", notes)
        self.assertNotEqual(claimed, "DONE", notes)
        self.assertIn("identical to the artifact that was already there",
                      " ".join(notes))
        self.assertIn(f"REASON={W.REASON_ARTIFACT_UNCHANGED}", notes)

    def test_the_same_unit_that_actually_wrote_its_output_is_done(self):
        """The control. Without it the test above passes on a gate that
        refuses everything, which establishes nothing."""
        attempt = self.attempt()
        basis = S._capture_artifact_basis(
            {}, "u", str(attempt), {"outputs": ["o.txt"]})
        self.assertEqual(basis["absent"], ["o.txt"])
        (attempt / "o.txt").write_text("produced here\n")
        _report, state = self.advance(self.plan(), self.state(attempt, basis))
        stored, claimed, notes = self.verdict(attempt, state)
        self.assertEqual(stored, "DONE", notes)
        self.assertEqual(claimed, "DONE", notes)

    def test_an_output_that_existed_and_then_changed_is_production(self):
        """Present beforehand is not disqualifying on its own. An artifact a
        unit legitimately rewrites is still produced by it."""
        attempt = self.attempt()
        (attempt / "o.txt").write_text("first\n")
        basis = S._capture_artifact_basis(
            {}, "u", str(attempt), {"outputs": ["o.txt"]})
        (attempt / "o.txt").write_text("rewritten by the run\n")
        _report, state = self.advance(self.plan(), self.state(attempt, basis))
        stored, claimed, notes = self.verdict(attempt, state)
        self.assertEqual(stored, "DONE", notes)
        self.assertEqual(claimed, "DONE", notes)

    def test_the_report_cannot_show_done_from_the_receipt_either(self):
        """report.py prefers an ATTESTED receipt over coordinator state, on
        purpose: the receipt is what judged the artifacts. So the refusal has
        to reach the receipt, not just state, or the unit still reads DONE
        everywhere a human looks."""
        attempt = self.attempt()
        (attempt / "o.txt").write_text("i am an input\n")
        basis = S._capture_artifact_basis(
            {}, "u", str(attempt), {"outputs": ["o.txt"]})
        _report, state = self.advance(self.plan(), self.state(attempt, basis))
        receipt = json.loads((attempt / U.RECEIPT).read_text())
        self.assertEqual(receipt["state"], "INCOMPLETE", receipt["notes"])
        self.assertEqual(receipt["exit_code"], U.STATES["INCOMPLETE"])
        # And the receipt IS the one the coordinator caused, so report.py will
        # show it rather than falling back to state. The refusal has to be
        # visible on the winning side.
        rp, why = S.attested_receipt(state, "u", str(attempt))
        self.assertIsNone(why)
        self.assertEqual(rp["state"], "INCOMPLETE")


class TestAMissingBasisFailsClosed(Base):
    """No fallback, and no fresh look. Both halves, because the second is the
    one that quietly reintroduces the bug."""

    def test_no_basis_at_all_means_not_done(self):
        attempt = self.attempt()
        (attempt / "o.txt").write_text("whatever\n")
        _report, state = self.advance(self.plan(), self.state(attempt))
        stored, claimed, notes = self.verdict(attempt, state)
        self.assertNotEqual(stored, "DONE", notes)
        self.assertNotEqual(claimed, "DONE", notes)
        self.assertIn("no pre-dispatch digest", " ".join(notes))
        self.assertIn("never re-observed after the fact", " ".join(notes))

    def test_the_refusal_never_writes_the_basis_it_was_missing(self):
        """THE trap. Recording what is on disk now, so the next advance has a
        basis, reads as a repair and is the original defect: the baseline
        would be a digest of the run's own output."""
        attempt = self.attempt()
        (attempt / "o.txt").write_text("whatever\n")
        _report, state = self.advance(self.plan(), self.state(attempt))
        self.assertEqual(
            (state["units"]["u"].get("attempt_artifact_bases") or {}), {},
            "the refusal re-observed the artifacts and stored the result as a "
            "baseline, which is the bug this change exists to prevent")

    def test_a_malformed_basis_is_refused_rather_than_ignored(self):
        attempt = self.attempt()
        (attempt / "o.txt").write_text("whatever\n")
        _report, state = self.advance(
            self.plan(), self.state(attempt, {"schema_version": 1}))
        stored, claimed, _notes = self.verdict(attempt, state)
        self.assertNotEqual(stored, "DONE")
        self.assertNotEqual(claimed, "DONE")

    def test_a_future_schema_is_refused_rather_than_guessed(self):
        problem = W.artifact_basis_problem(
            {"schema_version": 99, "attempt_id": "att1", "declared": [],
             "absent": [], "escaped": [], "present": {}}, "/runs/u/att1")
        self.assertIn("schema_version", problem)


class TestPinnedPerAttempt(Base):
    """`produced_head` was a unit-level scalar that was never cleared, so a
    retry inherited the previous attempt's commit. Twice: once on disk, once
    in state. This is the third place it could have happened."""

    def test_a_second_capture_does_not_re_observe(self):
        """`_submit` is re-entered on the code-launch recovery path, and a
        continuation runs another turn in the SAME write root. Either one, if
        it re-digested, would adopt the previous turn's output as the baseline
        it is about to be judged against."""
        attempt = self.attempt()
        state = {}
        first = S._capture_artifact_basis(
            state, "u", str(attempt), {"outputs": ["o.txt"]})
        self.assertEqual(first["absent"], ["o.txt"])
        (attempt / "o.txt").write_text("written by the first turn\n")
        second = S._capture_artifact_basis(
            state, "u", str(attempt), {"outputs": ["o.txt"]})
        self.assertEqual(second, first,
                         "the basis was re-taken after the run had written, "
                         "so the artifact became its own baseline")
        self.assertEqual(second["present"], {})

    def test_a_retry_does_not_inherit_the_previous_attempts_basis(self):
        one = self.attempt(attempt_id="att1")
        (one / "o.txt").write_text("first attempt's output\n")
        state = {}
        S._capture_artifact_basis(state, "u", str(one), {"outputs": ["o.txt"]})
        two = self.attempt(attempt_id="att2")
        # Nothing was captured for att2. The previous attempt's entry must not
        # answer for it, however convenient a unit-level fallback would be.
        self.assertIsNone(S.trusted_artifact_basis(state, "u", str(two)))
        self.assertIsNotNone(S.trusted_artifact_basis(state, "u", str(one)))
        S._capture_artifact_basis(state, "u", str(two), {"outputs": ["o.txt"]})
        bases = state["units"]["u"]["attempt_artifact_bases"]
        self.assertEqual(set(bases["att1"]["present"]), {"o.txt"})
        self.assertEqual(bases["att2"]["absent"], ["o.txt"])
        self.assertEqual(bases["att2"]["present"], {})

    def test_a_basis_from_another_unit_is_not_accepted(self):
        """Both identities are restated inside the basis, so a value copied
        between unit entries in state is useless. `launch_facts_problem`
        checks the same pair for the same reason."""
        attempt = self.attempt(task="u")
        basis = S._capture_artifact_basis(
            {}, "other-unit", str(attempt), {"outputs": ["o.txt"]})
        self.assertEqual(basis["unit_id"], "other-unit")
        state = {"units": {"u": {"attempt_artifact_bases": {"att1": basis}}}}
        self.assertIsNone(S.trusted_artifact_basis(state, "u", str(attempt)))
        self.assertIn(
            "belongs to unit",
            W.artifact_basis_problem(basis, str(attempt), {"task_id": "u"}))

    def test_a_cross_wired_basis_is_not_accepted_for_another_attempt(self):
        """Keyed by attempt AND restating its own attempt id. The second is
        what makes a copied entry useless rather than merely unlikely."""
        one = self.attempt(attempt_id="att1")
        two = self.attempt(attempt_id="att2")
        state = {}
        basis = S._capture_artifact_basis(
            state, "u", str(one), {"outputs": ["o.txt"]})
        state["units"]["u"]["attempt_artifact_bases"]["att2"] = basis
        self.assertIsNone(S.trusted_artifact_basis(state, "u", str(two)))

    def test_a_retry_with_an_inherited_basis_still_cannot_read_done(self):
        """End to end, because the accessor returning None only matters if the
        checker then refuses."""
        one = self.attempt(attempt_id="att1")
        state_map = {}
        basis = S._capture_artifact_basis(
            state_map, "u", str(one), {"outputs": ["o.txt"]})
        two = self.attempt(attempt_id="att2")
        (two / "o.txt").write_text("output\n")
        st = self.state(two)
        st["units"]["u"]["attempt_artifact_bases"] = {"att1": basis}
        _report, state = self.advance(self.plan(), st)
        stored, claimed, notes = self.verdict(two, state)
        self.assertNotEqual(stored, "DONE", notes)
        self.assertNotEqual(claimed, "DONE", notes)


class TestTheAgentCannotReachTheBaseline(Base):
    """A digest the judged party can rewrite is not a baseline. Authority
    lives in coordinator state, which C13 put outside every operated
    worktree, and travels to the separate judge by value."""

    def test_nothing_in_the_attempt_directory_holds_the_baseline(self):
        """The BEFORE digest must appear nowhere the judged party can write.
        Asserted on that digest rather than on a field name, because the file
        it names is still there and still digested -- the receipt records what
        the artifact looks like NOW, which is a different number."""
        attempt = self.attempt()
        (attempt / "o.txt").write_text("before\n")
        basis = S._capture_artifact_basis(
            {}, "u", str(attempt), {"outputs": ["o.txt"]})
        baseline_digest = basis["present"]["o.txt"]["sha256"]
        (attempt / "o.txt").write_text("after, written by the run\n")
        _report, _state = self.advance(
            self.plan(), self.state(attempt, basis))
        looked_at = 0
        for path in sorted(Path(attempt).rglob("*")):
            if not path.is_file():
                continue
            looked_at += 1
            body = path.read_text(errors="replace")
            self.assertNotIn(baseline_digest, body, str(path))
            self.assertNotIn("attempt_artifact_bases", body, str(path))
        self.assertGreater(looked_at, 1, "the walk found nothing to check")

    def test_the_launch_record_never_carries_it_either(self):
        """The launch record is audit-only. Putting the baseline there would
        make an audit copy decide admission, which is the defect six review
        rounds were spent removing."""
        src = (SCRIPTS / "swarm.py").read_text()
        i = src.index("def _write_launch_record")
        j = src.index("\ndef ", i + 10)
        self.assertNotIn("artifact_basis", src[i:j])
        payload = src.index("def _code_launch_record_payload")
        end = src.index("\ndef ", payload + 10)
        self.assertNotIn("artifact_basis", src[payload:end])

    def test_the_basis_reaches_the_judge_from_state_and_nowhere_else(self):
        seen = {}
        real = S.U.run

        def run(argv, **kwargs):
            if "check" in argv:
                seen["argv"] = list(argv)
            return real(argv, **kwargs)

        attempt = self.attempt()
        basis = S._capture_artifact_basis(
            {}, "u", str(attempt), {"outputs": ["o.txt"]})
        (attempt / "o.txt").write_text("produced\n")
        S.U.run = run
        try:
            self.advance(self.plan(), self.state(attempt, basis))
        finally:
            S.U.run = real
        self.assertIn("--artifact-basis", seen["argv"])
        handed = json.loads(seen["argv"][seen["argv"].index(
            "--artifact-basis") + 1])
        self.assertEqual(handed["attempt_id"], "att1")

    def test_rewriting_the_specs_declared_outputs_is_refused(self):
        """`unit.json` lives inside the attempt directory, so the judged party
        can edit it. Emptying `declared_outputs` made every output "present"
        vacuously; the coordinator's own list is what the gate compares."""
        attempt = self.attempt()
        basis = S._capture_artifact_basis(
            {}, "u", str(attempt), {"outputs": ["o.txt"]})
        (attempt / "o.txt").write_text("produced\n")
        spec = json.loads((attempt / U.UNIT).read_text())
        spec["declared_outputs"] = []
        (attempt / U.UNIT).write_text(json.dumps(spec))
        _report, state = self.advance(
            self.plan(), self.state(attempt, basis))
        stored, claimed, notes = self.verdict(attempt, state)
        self.assertNotEqual(stored, "DONE", notes)
        self.assertNotEqual(claimed, "DONE", notes)
        self.assertIn("declaration changed after the baseline",
                      " ".join(notes))

    def test_the_captured_list_comes_from_the_plan_not_the_spec(self):
        attempt = self.attempt(outputs=["spec-said-this"])
        basis = S._capture_artifact_basis(
            {}, "u", str(attempt), {"outputs": ["plan-said-this"]})
        self.assertEqual(basis["declared"], ["plan-said-this"])


class TestWhatCountsAsAChange(unittest.TestCase):
    """The comparator, in isolation. It reads no file and runs no command, so
    there is no second observation of a moving target inside it."""

    def basis(self, present=None, absent=(), escaped=(), declared=None):
        present = present or {}
        declared = (list(declared) if declared is not None
                    else sorted(set(present) | set(absent) | set(escaped)))
        return {"schema_version": W.ARTIFACT_BASIS_SCHEMA,
                "attempt_id": "att1", "captured_at": "now",
                "declared": declared, "absent": list(absent),
                "escaped": list(escaped), "present": present}

    def problem(self, basis, observed, declared=None):
        spec = {"declared_outputs": (list(declared) if declared is not None
                                     else list(basis["declared"]))}
        return W.artifact_transition_problem(
            basis, "/runs/u/att1", spec, observed)

    def test_a_changed_content_digest_is_production(self):
        problem, weak = self.problem(
            self.basis({"o": {"sha256": "a" * 64, "size": 1,
                              "method": "content-digest"}}),
            {"o": {"sha256": "b" * 64, "size": 2,
                   "method": "content-digest"}})
        self.assertIsNone(problem)
        self.assertEqual(weak, [])

    def test_an_unchanged_content_digest_is_refused(self):
        problem, _weak = self.problem(
            self.basis({"o": {"sha256": "a" * 64, "size": 1,
                              "method": "content-digest"}}),
            {"o": {"sha256": "a" * 64, "size": 1,
                   "method": "content-digest"}})
        self.assertIn("already there", problem)

    def test_a_directory_whose_nested_file_changed_is_production(self):
        """A directory's own size and mtime say nothing about a nested file.
        The tree digest is what carries the change, and both sides use the
        same function so the comparison means something."""
        problem, _weak = self.problem(
            self.basis({"d": {"sha256": "a" * 64, "entries": 2,
                              "method": "tree-digest"}}),
            {"d": {"sha256": "c" * 64, "entries": 3,
                   "method": "tree-digest"}})
        self.assertIsNone(problem)

    def test_a_directory_with_an_identical_tree_is_refused(self):
        problem, _weak = self.problem(
            self.basis({"d": {"sha256": "a" * 64, "entries": 2,
                              "method": "tree-digest"}}),
            {"d": {"sha256": "a" * 64, "entries": 2,
                   "method": "tree-digest"}})
        self.assertIn("already there", problem)

    def test_two_methods_that_cannot_be_compared_fail_closed(self):
        """A basis taken by content against an observation recorded by
        size+mtime describes the artifact with different strength. Calling
        that pair "changed" would admit the unit on the weaker of the two
        without saying so."""
        problem, _weak = self.problem(
            self.basis({"o": {"sha256": "a" * 64, "size": 1,
                              "method": "content-digest"}}),
            {"o": {"size": 2, "mtime": 5,
                   "method": "size-mtime (WEAK: over the digest limit)"}})
        self.assertIn("cannot be compared", problem)

    def test_an_over_limit_artifact_is_admitted_weakly_and_says_so(self):
        problem, weak = self.problem(
            self.basis({"o": {"size": 1, "mtime": 5, "method": "size-mtime"}}),
            {"o": {"size": 9, "mtime": 7, "method": "size-mtime"}})
        self.assertIsNone(problem)
        self.assertEqual(weak, ["o"])

    def test_an_over_limit_artifact_with_the_same_size_and_mtime_is_refused(self):
        problem, _weak = self.problem(
            self.basis({"o": {"size": 1, "mtime": 5, "method": "size-mtime"}}),
            {"o": {"size": 1, "mtime": 5, "method": "size-mtime"}})
        self.assertIn("already there", problem)

    def test_a_stat_error_on_either_side_fails_closed(self):
        for was, now in (({"error": "boom"}, {"sha256": "a" * 64}),
                         ({"sha256": "a" * 64}, {"error": "boom"})):
            problem, _weak = self.problem(self.basis({"o": was}), {"o": now})
            self.assertIn("cannot be compared", problem)

    def test_an_artifact_the_basis_never_covered_is_refused(self):
        problem, _weak = self.problem(
            self.basis(declared=["o"]), {"o": {"sha256": "a" * 64}})
        self.assertIn("nothing was digested for it before dispatch",
                      problem)

    def test_an_output_that_escaped_the_write_root_is_refused(self):
        problem, _weak = self.problem(
            self.basis(escaped=["../outside"]),
            {"../outside": {"sha256": "a" * 64}})
        self.assertIn("never isolated", problem)

    def test_an_absent_artifact_that_appeared_is_production(self):
        problem, weak = self.problem(
            self.basis(absent=["o"]), {"o": {"sha256": "a" * 64}})
        self.assertIsNone(problem)
        self.assertEqual(weak, [])

    def test_a_unit_with_nothing_declared_has_no_transition_to_judge(self):
        """`validate_plan` already refuses a unit with no outputs, for the
        same reason. Refusing it a second time here would be this gate
        reaching past its own question."""
        problem, _weak = self.problem(self.basis(declared=[]), {})
        self.assertIsNone(problem)


class TestOnlyADoneIsGated(unittest.TestCase):

    def basis(self):
        return {"schema_version": W.ARTIFACT_BASIS_SCHEMA,
                "attempt_id": "att1", "captured_at": "now",
                "declared": ["o"], "absent": [], "escaped": [],
                "present": {"o": {"sha256": "a" * 64, "size": 1}}}

    def test_a_running_verdict_is_not_turned_into_a_refusal(self):
        """An artifact that has not changed yet is the NORMAL condition of a
        live unit. Gating anything but DONE would report every running
        attempt as broken."""
        notes = []
        spec = {"declared_outputs": ["o"]}
        for state in ("RUNNING", "FAILED", "PREEMPTED", "NEEDS_HUMAN",
                      "INCOMPLETE"):
            self.assertEqual(
                W.judge_artifacts(state, self.basis(), "/runs/u/att1", spec,
                                  {"o": {"sha256": "a" * 64, "size": 1}},
                                  notes),
                state)
        self.assertEqual(notes, [])

    def test_a_done_with_no_basis_becomes_incomplete(self):
        notes = []
        got = W.judge_artifacts("DONE", None, "/runs/u/att1",
                                {"declared_outputs": ["o"]},
                                {"o": {"sha256": "a" * 64}}, notes)
        self.assertEqual(got, "INCOMPLETE")
        self.assertIn(f"REASON={W.REASON_ARTIFACT_UNCHANGED}", notes)

    def test_the_reason_is_not_the_one_that_asks_for_another_turn(self):
        """`maybe_continue` answers exactly one reason, "settled and produced
        nothing", by sending the agent another turn. "What is there is what
        was already there" is an evidence failure; prodding the agent would
        answer a question nobody asked."""
        self.assertNotEqual(W.REASON_ARTIFACT_UNCHANGED, U.REASON_NO_OUTPUTS)


if __name__ == "__main__":
    unittest.main()
