"""The tracker outbox.

The coordinator cannot reach a tracker and must never depend on one. These
tests pin the three properties that make that separation safe rather than
merely convenient.
"""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
sys.path.insert(0, str(SWARM.parent))
import swarm as S  # noqa: E402

PLAN = {"name": "rna-bench", "units": [
    {"id": "prep", "kind": "slurm", "command": "true", "outputs": ["o"],
     "write_scopes": ["r/p/"]},
    {"id": "train", "kind": "slurm", "command": "true", "outputs": ["o"],
     "needs": ["prep"], "write_scopes": ["r/t/"]}]}


def run(tmp, *argv):
    (tmp / "plan.json").write_text(json.dumps(PLAN))
    return subprocess.run(
        [sys.executable, str(SWARM), *argv, str(tmp / "plan.json"),
         "--dry-run", "--state-dir", str(tmp / "st")],
        capture_output=True, text=True, cwd=tmp)


class TestIdempotency(unittest.TestCase):
    def test_the_same_transition_never_yields_two_intents(self):
        """The point of the key. A drain that retries after a partial failure
        must not open a second issue for one unit."""
        a = S.outbox_key("p", "train", "DONE", "/x/attempt-1")
        b = S.outbox_key("p", "train", "DONE", "/x/attempt-1")
        self.assertEqual(a, b)

    def test_a_new_attempt_is_a_new_intent(self):
        """A preempted unit that reruns is genuinely new work, and the tracker
        should hear about it. Keying on state alone would swallow it."""
        a = S.outbox_key("p", "train", "DONE", "/x/attempt-1")
        b = S.outbox_key("p", "train", "DONE", "/x/attempt-2")
        self.assertNotEqual(a, b)

    def test_distinct_projects_do_not_collide(self):
        self.assertNotEqual(S.outbox_key("a", "u", "DONE", None),
                            S.outbox_key("b", "u", "DONE", None))

    def test_advancing_twice_appends_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            run(tmp, "run")
            run(tmp, "advance")
            run(tmp, "advance")
            keys = [i["key"] for i in S.read_outbox(tmp / "st")]
            self.assertEqual(len(keys), len(set(keys)),
                             "an intent was appended twice")


class TestNothingClosesOnASelfReport(unittest.TestCase):
    def test_a_close_intent_carries_the_verdict(self):
        """A unit reporting success is exactly what this family refuses to
        believe. A close must carry the receipt that a predicate produced."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            st = Path(d); st.mkdir(exist_ok=True)
            S.emit_intent(st, "p", "u", "DONE", {"attempt_dir": None},
                          evidence={"receipt": {"verdict": "DONE"}})
            i = S.read_outbox(st)[0]
            self.assertEqual(i["verb"], "close")
            self.assertIsNotNone(i["evidence"])

    def test_only_declared_states_produce_intents(self):
        """Enumerate the good set. An unrecognised state emits nothing rather
        than guessing at a tracker mutation."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            st = Path(d)
            self.assertIsNone(
                S.emit_intent(st, "p", "u", "RUNNING", {"attempt_dir": None}))
            self.assertEqual(S.read_outbox(st), [])

    def test_every_declared_state_maps_to_a_real_unit_state(self):
        """Closure by exclusion: a state in TRACKER_EVENTS that swarm.py can
        never produce is dead code that will silently stop firing."""
        produced = set(S.NAME.values()) | {
            "SUBMITTED", "HELD", "PREEMPTED", "FAILED_EVIDENCE"}
        unreachable = set(S.TRACKER_EVENTS) - produced
        self.assertEqual(unreachable, set(),
                         f"TRACKER_EVENTS names states nothing produces: "
                         f"{unreachable}")


class TestTheTrackerIsNeverAuthoritative(unittest.TestCase):
    def test_swarm_has_no_network_code(self):
        """Structural. The coordinator runs on a shared login node; it holds no
        tracker token because it makes no calls. Checked by AST, not by grep,
        so a string in a docstring cannot trip or satisfy it."""
        import ast
        tree = ast.parse(SWARM.read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        banned = mods & {"urllib", "http", "requests", "socket", "httpx"}
        self.assertEqual(banned, set(),
                         f"the coordinator imports network code: {banned}")

    def test_an_unwritable_outbox_does_not_stop_the_swarm(self):
        """A full or read-only filesystem must degrade to a warning. Losing a
        ticket update is an inconvenience; stalling the DAG is an outage."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            st = Path(d) / "nope" / "deeper"   # parent does not exist
            self.assertIsNone(
                S.emit_intent(st, "p", "u", "DONE", {"attempt_dir": None}))




# Contender run as a SEPARATE PROCESS. Real processes, real O_EXCL, no
# in-process synchronisation that could mask the race being tested. Each
# spins until a shared wall-clock start so they collide as tightly as the
# scheduler allows.
_CONTENDER = """
import sys, time, json
sys.path.insert(0, %r)
import swarm as S
state_dir, start = sys.argv[1], float(sys.argv[2])
while time.time() < start:
    pass
ok, _ = S.acquire_lease(state_dir)
print("WON" if ok else "lost")
"""


class TestTheLeaseIsAtomic(unittest.TestCase):
    """Three reviewers independently broke the read-then-write lease. These
    pin the fix against the actual failure they described."""

    def _contend(self, state_dir, n):
        import tempfile
        src = Path(tempfile.mkdtemp()) / "contend.py"
        src.write_text(_CONTENDER % str(SWARM.parent))
        start = time.time() + 2.0
        procs = [subprocess.Popen(
            [sys.executable, str(src), str(state_dir), str(start)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(n)]
        return [p.communicate(timeout=60)[0].strip() for p in procs]

    # REPEATED. A single trial of this passed against an implementation that
    # was still racy: with 8 contenders it happened to produce one winner.
    # Running it repeatedly is what exposed the second defect, a re-read
    # missing inside the takeover's own critical section.
    TRIALS = 5

    def test_many_concurrent_acquirers_yield_exactly_one_winner(self):
        """The real race, run for real. A structural claim that the code calls
        O_EXCL would pass even if the surrounding logic still let two through;
        counting winners across real processes cannot."""
        import tempfile
        for i in range(self.TRIALS):
            with tempfile.TemporaryDirectory() as d:
                out = self._contend(d, 12)
                self.assertEqual(out.count("WON"), 1,
                                 f"trial {i}: expected exactly one controller "
                                 f"to win the lease, got {out.count('WON')}: "
                                 f"{out}")

    def test_a_stale_lease_can_be_taken_but_only_by_one(self):
        """A controller killed mid-run must not block the project forever. The
        takeover is itself a race, and it is the one that was still broken
        after the first fix."""
        import tempfile
        for i in range(self.TRIALS):
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / S.LEASE).write_text(json.dumps(
                    {"owner": "ghost", "host": "elsewhere", "pid": 999999,
                     "acquired_at": 0, "expires_at": 0}))
                out = self._contend(d, 12)
                self.assertEqual(out.count("WON"), 1,
                                 f"trial {i}: a stale lease must be taken by "
                                 f"exactly one contender, got "
                                 f"{out.count('WON')}: {out}")

    def test_a_fresh_lease_is_never_taken(self):
        """The counter-claim: exclusion must actually exclude."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / S.LEASE).write_text(json.dumps(
                {"owner": "live", "host": "elsewhere", "pid": 999999,
                 "acquired_at": time.time(),
                 "expires_at": time.time() + 900}))
            out = self._contend(d, 4)
            self.assertEqual(out.count("WON"), 0, f"took a live lease: {out}")

    def test_release_does_not_delete_another_controllers_lease(self):
        """The exit-path defect: a controller already taken over would unlink
        its successor's lease on the way out, leaving nobody protected."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / S.LEASE).write_text(json.dumps(
                {"owner": "someone", "host": "otherhost", "pid": 4242,
                 "acquired_at": time.time(),
                 "expires_at": time.time() + 900}))
            S.release_lease(d)
            self.assertTrue((Path(d) / S.LEASE).is_file(),
                            "released a lease this process did not hold")

    def test_renewal_refuses_when_we_no_longer_hold_it(self):
        """If we were taken over, the right response is to stop, not to steal
        it back and have two controllers believe they hold it."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            S.acquire_lease(d)
            self.assertTrue(S.renew_lease(d))
            (Path(d) / S.LEASE).write_text(json.dumps(
                {"owner": "x", "host": "otherhost", "pid": 4242,
                 "acquired_at": time.time(),
                 "expires_at": time.time() + 900}))
            self.assertFalse(S.renew_lease(d))


class TestThePlanDigestCoversDispatch(unittest.TestCase):
    """Four reviewers broke the inclusion list by naming fields it forgot."""

    def _p(self, **over):
        u = {"id": "a", "kind": "slurm", "command": "true", "outputs": ["o"],
             "inputs": ["ref.fa"], "gpu_hours": 4, "charge_to": "acct1",
             "write_scopes": ["r/"], "max_attempts": 3, "timeout_s": 60}
        u.update(over)
        return {"units": [u], "budget": {"gpu_hours": 100}}

    def test_every_field_the_reviewers_named_changes_the_digest(self):
        base = S.plan_digest(self._p())
        for field, value in [("inputs", ["ref2.fa"]), ("gpu_hours", 400),
                             ("charge_to", "acct2"), ("write_scopes", ["z/"]),
                             ("max_attempts", 99), ("timeout_s", 7200),
                             ("command", "false"), ("outputs", ["p"])]:
            self.assertNotEqual(
                base, S.plan_digest(self._p(**{field: value})),
                f"editing {field} mid-flight leaves the digest unchanged, so "
                f"the plan freeze does not refuse it")

    def test_a_field_nobody_thought_of_is_covered_by_default(self):
        """The point of excluding rather than including: a field added next
        year is protected until someone argues it is cosmetic."""
        self.assertNotEqual(S.plan_digest(self._p()),
                            S.plan_digest(self._p(some_future_knob="on")))

    def test_cosmetic_edits_do_not_invalidate_a_live_run(self):
        """The counter-claim. A gate that fires on a comment gets switched
        off, and then prevents nothing."""
        for field in ("description", "comment", "notes", "tags"):
            self.assertEqual(S.plan_digest(self._p()),
                             S.plan_digest(self._p(**{field: "anything"})),
                             f"editing {field} wrongly invalidates the run")

    def test_reordering_units_does_not_change_the_digest(self):
        one = {"units": [{"id": "a", "command": "x"}, {"id": "b", "command": "y"}]}
        two = {"units": [{"id": "b", "command": "y"}, {"id": "a", "command": "x"}]}
        self.assertEqual(S.plan_digest(one), S.plan_digest(two))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheWedges(unittest.TestCase):
    """Two reviewers found states from which a unit could never be dispatched
    or judged again. Each is an honest-work failure: the job is fine, the
    coordinator has simply lost the thread."""

    def test_a_failed_bind_leaves_the_unit_recoverable(self):
        """The job is REAL and running; only the binding write failed. Setting
        job_id without a marker skipped the reconcile net, so the unit decayed
        to FAILED_EVIDENCE while its job succeeded."""
        src = SWARM.read_text()
        self.assertIn('us["bind_pending"] = True', src,
                      "a failed bind must be marked for retry")
        i = src.index("if us.get(\"bind_pending\")")
        j = src.index("if us.get(\"job_id\"):\n            continue")
        self.assertLess(i, j,
                        "the pending-bind retry must be attempted BEFORE the "
                        "reconcile loop skips anything that has a job_id")

    def test_an_allocated_but_unsubmitted_attempt_is_released(self):
        """Crash between the ALLOCATED save and sbatch. The scheduler never
        heard of it, so nothing is running; keeping attempt_dir set wedged the
        unit forever, because only PREEMPTED cleared it."""
        src = SWARM.read_text()
        self.assertIn('elif us["state"] == "ALLOCATED":', src)
        self.assertIn('us["attempt_dir"] = None', src)

    def test_the_released_directory_is_not_deleted(self):
        """Never destroy evidence to tidy up. A new attempt gets a NEW root."""
        src = SWARM.read_text()
        seg = src[src.index('elif us["state"] == "ALLOCATED":'):]
        seg = seg[:seg.index("save_state")]
        for destructive in ("rmtree", "unlink", "rmdir"):
            self.assertNotIn(destructive, seg,
                             f"releasing an attempt must not {destructive} it")

    def test_reconcile_asks_sacct_for_a_window(self):
        """sacct WITHOUT -S defaults to today. A crash at 23:50 whose job
        finished at 23:55 is invisible to a 00:10 reconcile."""
        src = SWARM.read_text()
        seg = src[src.index("def reconcile_orphan"):]
        seg = seg[:seg.index("\ndef ")]
        self.assertIn('"-S", start', seg,
                      "reconcile must bound the sacct window explicitly")

    def test_retry_charges_accumulate(self):
        """Overwriting meant a unit preempted twice was charged once, so
        retries walked straight through the ceiling."""
        src = SWARM.read_text()
        self.assertIn('us["gpu_hours"] = float(us.get("gpu_hours") or 0) + want',
                      src, "each attempt must add to the unit's charge")

    def test_the_pipeline_engine_is_not_run_to_completion(self):
        """An honest `nextflow run` was SIGKILLed at 120s by the coordinator."""
        src = SWARM.read_text()
        seg = src[src.index('if kind == "pipeline":'):]
        seg = seg[:seg.index('if kind == "code":')]
        self.assertIn("start_new_session=True", seg,
                      "the engine must be launched detached")
        self.assertNotIn("U.run(", seg,
                         "the coordinator must not block on the engine")


class TestTheRefusalsNameAnAction(unittest.TestCase):
    def test_the_plan_change_refusal_offers_a_non_destructive_remedy(self):
        """The old remedy was 'start a new state directory', which
        re-dispatches DONE units and duplicates live jobs: a refusal whose
        named action was worse than the fault."""
        src = SWARM.read_text()
        seg = src[src.index("REFUSING to advance"):]
        seg = seg[:seg.index("plan changed mid-flight\")")]
        self.assertIn("--accept-plan-change", seg)
        self.assertNotIn("start a new state directory", seg)
