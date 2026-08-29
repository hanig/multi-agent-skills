"""The tracker outbox.

The coordinator cannot reach a tracker and must never depend on one. These
tests pin the three properties that make that separation safe rather than
merely convenient.
"""
import json
import os
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


class TestThePipelinePredicate(unittest.TestCase):
    """The pipeline path had NEVER run for real. Its first live run sat
    INCOMPLETE with its declared output on disk, because check_unit demanded a
    scheduler binding that a pipeline unit never has. These pin the predicate
    written to replace that, and the negative cases that give it meaning."""

    UNIT = SWARM.parent / "unit.py"

    def _attempt(self, tmp, rc=None, outputs=(), engine=True, pid=None):
        import os as _os
        d = Path(tmp) / "attempt"
        d.mkdir(parents=True, exist_ok=True)
        (d / "unit.json").write_text(json.dumps({
            "schema_version": 1, "attempt_id": "attempt", "task_id": "u",
            "kind": "pipeline", "declared_outputs": list(outputs),
            "created_at": "2026-08-28T00:00:00+0000"}))
        if engine:
            (d / "engine.json").write_text(json.dumps({
                "pid": pid if pid is not None else _os.getpid(),
                "host": _os.uname().nodename, "launched_at": time.time(),
                "command": "x", "log": "engine.log"}))
        if rc is not None:
            (d / "engine.rc").write_text(str(rc))
        return d

    def _check(self, d):
        r = subprocess.run([sys.executable, str(self.UNIT), "check", str(d)],
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def test_exit_zero_with_every_output_is_done(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = self._attempt(t, rc=0, outputs=["o.txt"])
            (d / "o.txt").write_text("real\n")
            rc, out = self._check(d)
            self.assertEqual(rc, 0, out)

    def test_exit_zero_with_a_missing_output_is_not_done(self):
        """The failure a scheduler structurally cannot see, and the reason a
        clean exit is never sufficient on its own."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = self._attempt(t, rc=0, outputs=["o.txt"])
            rc, out = self._check(d)
            self.assertNotEqual(rc, 0, f"exit 0 with no output must not be "
                                       f"DONE:\n{out}")
            self.assertIn("absent", out)

    def test_a_nonzero_exit_is_failed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = self._attempt(t, rc=3, outputs=["o.txt"])
            (d / "o.txt").write_text("written anyway\n")
            rc, out = self._check(d)
            self.assertNotEqual(rc, 0, f"a non-zero exit must not be DONE even "
                                       f"when the output exists:\n{out}")
            self.assertIn("exited 3", out)

    def test_an_attempt_with_no_engine_record_is_not_judged_done(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = self._attempt(t, rc=None, outputs=["o.txt"], engine=False)
            (d / "o.txt").write_text("appeared from nowhere\n")
            rc, out = self._check(d)
            self.assertNotEqual(rc, 0, f"an output with no launched engine "
                                       f"must not be DONE:\n{out}")

    def test_a_live_engine_reads_as_running_not_failed(self):
        """The honest-work case. Reporting a running engine as failed holds
        its dependents and ends work that was fine.

        Spawns a REAL child. The fixture used to point at the test runner's
        own pid while claiming it was launched a moment ago, which the
        stricter identity check correctly rejects: a process alive for minutes
        cannot be one launched seconds back. It passed alone and failed in the
        full suite, purely because the runner had been alive longer by then.
        The fixture was lying, not the code."""
        import subprocess as sp
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            child = sp.Popen(["sleep", "20"])
            time.sleep(0.3)
            try:
                d = self._attempt(t, rc=None, outputs=["o.txt"],
                                  pid=child.pid)
                rc, out = self._check(d)
            finally:
                child.kill()
                child.wait()
            self.assertIn("still running", out, out)

    def test_the_wrapper_survives_an_explicit_exit_in_the_command(self):
        """`exit N` is ordinary in pipeline wrappers and it terminated the
        outer shell before the status line ran, so engine.rc was never written
        and the check reported 'killed, or the node rebooted': false, and it
        pointed the operator at the wrong thing entirely."""
        src = SWARM.read_text()
        seg = src[src.index("wrapped = "):]
        seg = seg[:seg.index("proc = subprocess.Popen")]
        self.assertIn("(\\n", seg,
                      "the command must run in a subshell so an `exit` inside "
                      "it cannot skip the status line")

    def test_the_receipt_does_not_borrow_the_schedulers_authority(self):
        """A pipeline unit has no third party behind it. Saying so is the
        whole discipline."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = self._attempt(t, rc=0, outputs=["o.txt"])
            (d / "o.txt").write_text("real\n")
            self._check(d)
            basis = json.loads((d / "receipt.json").read_text())["basis"]
            self.assertEqual(basis["exit_status_attested_by"],
                             "launcher wrapper (no scheduler)")
            self.assertFalse(basis["os_enforced_isolation"])


FAKE_PASEO = """#!/bin/sh
# Fake paseo. $PASEO_FIXTURE holds the JSON `inspect` should return.
case "$1" in
  inspect) cat "$PASEO_FIXTURE" ;;
  ls)      cat "${PASEO_LS:-/dev/null}" ;;
  run)     echo "Created workspace wks_deadbeef - fixture"
           echo "Tip: pass --workspace <id> to run in an existing workspace."
           cat "$PASEO_FIXTURE" ;;
esac
exit 0
"""


class TestTheCodePredicate(unittest.TestCase):
    """The `code` path had never run either. Running it for real found four
    separate breaks in one dispatch: the agent ran in the coordinator's
    directory instead of its write root, the agent id was read from the wrong
    JSON key, `bind` rejected a UUID outright, and the coordinator skipped
    binding any non-numeric id. Each alone made the kind unusable."""

    UNIT = SWARM.parent / "unit.py"

    def _env(self, tmp, fixture):
        import os as _os
        bindir = Path(tmp) / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        fake = bindir / "paseo"
        fake.write_text(FAKE_PASEO)
        fake.chmod(0o755)
        fx = Path(tmp) / "fixture.json"
        fx.write_text(json.dumps(fixture))
        env = dict(_os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["PASEO_FIXTURE"] = str(fx)
        return env

    def _attempt(self, tmp, agent="a1b2c3d4-0000-1111-2222-333344445555",
                 outputs=("o.txt",)):
        d = Path(tmp) / "attempt"
        d.mkdir(parents=True, exist_ok=True)
        (d / "unit.json").write_text(json.dumps({
            "schema_version": 1, "attempt_id": "attempt", "task_id": "u",
            "kind": "code", "job_id": agent, "declared_outputs": list(outputs),
            "created_at": "2026-08-28T00:00:00+0000"}))
        return d

    def _check(self, d, env):
        r = subprocess.run([sys.executable, str(self.UNIT), "check", str(d)],
                           capture_output=True, text=True, env=env)
        return r.returncode, r.stdout + r.stderr

    def test_idle_without_the_output_is_not_done(self):
        """THE point. `idle` is a lifecycle state exactly as `COMPLETED` is
        for Slurm, and an agent finishing its turn is not the work being
        done. Verified against a real agent too: it went idle having written
        nothing, and the predicate said INCOMPLETE."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            env = self._env(t, {"Status": "idle", "PendingPermissions": []})
            rc, out = self._check(self._attempt(t), env)
            self.assertNotEqual(rc, 0, f"an idle agent with no output must "
                                       f"not be DONE:\n{out}")
            self.assertIn("not the same as the work being done", out)

    def test_idle_with_every_output_is_done(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            env = self._env(t, {"Status": "idle", "PendingPermissions": []})
            d = self._attempt(t)
            (d / "o.txt").write_text("real\n")
            rc, out = self._check(d, env)
            self.assertEqual(rc, 0, out)

    def test_a_pending_permission_is_its_own_state(self):
        """Found live: an agent under default permissions stopped at its first
        Write and sat `running` forever. Reporting that as RUNNING hides it
        until the settle window turns it into a failure; reporting it FAILED
        is untrue and discards the agent's context."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            env = self._env(t, {"Status": "running",
                                "PendingPermissions": [{"tool": "Write"}]})
            d = self._attempt(t)
            (d / "o.txt").write_text("even with the output present\n")
            rc, out = self._check(d, env)
            self.assertEqual(rc, 5, f"expected NEEDS_HUMAN:\n{out}")
            self.assertIn("until a person answers", out)

    def test_needs_human_does_not_decay_into_a_failure(self):
        """Waiting for a person must not become FAILED_EVIDENCE because
        nobody was at the keyboard for ten minutes."""
        src = SWARM.read_text()
        i = src.index("if rc == NEEDS_HUMAN:")
        j = src.index("if rc == INCOMPLETE:")
        self.assertLess(i, j, "NEEDS_HUMAN must be handled before the settle "
                              "window logic")
        self.assertIn('us.pop("incomplete_since", None)', src[i:j])

    def test_the_agent_runs_in_its_exclusive_write_root(self):
        """Without --cwd the agent runs in the coordinator's directory, so it
        writes nowhere near its write root and its declared outputs can never
        be found: the isolation premise silently void for this one kind."""
        src = SWARM.read_text()
        seg = src[src.index('if kind == "code":'):]
        seg = seg[:seg.index("return str(agent), None")]
        self.assertIn('"--cwd", str(unit_dir)', seg)

    def test_bind_accepts_an_agent_id_for_a_code_unit(self):
        """`bind` demanded a numeric scheduler id, so a code unit could never
        be bound and its predicate reported 'no agent bound' forever."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = self._attempt(t, agent=None)
            spec = json.loads((d / "unit.json").read_text())
            spec.pop("job_id")
            (d / "unit.json").write_text(json.dumps(spec))
            r = subprocess.run(
                [sys.executable, str(self.UNIT), "bind", str(d), "--job-id",
                 "a1b2c3d4-0000-1111-2222-333344445555"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("bound agent", r.stdout)

    def test_a_slurm_unit_still_refuses_a_uuid(self):
        """The counter-claim: relaxing the shape for code must not relax it
        for slurm, where a numeric id is what sacct can be asked about."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "s"
            d.mkdir()
            (d / "unit.json").write_text(json.dumps({
                "schema_version": 1, "attempt_id": "s", "task_id": "u",
                "kind": "slurm", "declared_outputs": [],
                "created_at": "2026-08-28T00:00:00+0000"}))
            r = subprocess.run(
                [sys.executable, str(self.UNIT), "bind", str(d), "--job-id",
                 "a1b2c3d4-0000-1111-2222-333344445555"],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)

    def test_paseo_notices_do_not_break_the_id_parse(self):
        """paseo prints "Created workspace ..." and "Tip: ..." to stdout
        BEFORE its JSON, so json.loads on the whole stream fails and a live
        agent is left running, unbound."""
        sys.path.insert(0, str(SWARM.parent))
        import swarm as S2
        out = ('Created workspace wks_abc - foo\nTip: pass --workspace <id>.\n'
               '{\n "agentId": "the-id",\n "cwd": "/x/{nested}"\n}\n')
        self.assertEqual((S2._paseo_json(out) or {}).get("agentId"), "the-id")

    def test_paseos_own_diagnostic_survives(self):
        """An invalid mode comes back naming every mode the provider accepts.
        Truncating the combined stream threw that away and left the operator
        reading a workspace notice."""
        sys.path.insert(0, str(SWARM.parent))
        import swarm as S2
        out = ("Created workspace wks_abc - foo\nTip: something.\n"
               '{"error": {"code": "AGENT_CREATE_FAILED", "message": '
               '"Invalid mode. Available modes: plan, default, acceptEdits, '
               'auto, bypassPermissions"}}')
        msg = S2._paseo_error(out, "")
        self.assertIn("Available modes", msg)
        self.assertNotIn("Created workspace", msg)


class TestGatedPromotion(unittest.TestCase):
    """Plan step 3, criteria d-g. Outputs live in the exclusive write root;
    reaching a shared canonical tree is a separate, approved, recorded step,
    because a swarm that silently writes a shared path gets switched off."""

    def _project(self, tmp, promote_to=True, state="DONE", body="real\n"):
        import os as _os
        root = Path(tmp)
        attempt = root / "runs" / "w" / "att1"
        attempt.mkdir(parents=True)
        (attempt / "o.txt").write_text(body)
        st = (attempt / "o.txt").stat()
        digest = __import__("hashlib").sha256(body.encode()).hexdigest()
        (attempt / "receipt.json").write_text(json.dumps({
            "state": "DONE", "attempt_id": "att1",
            "outputs": {"o.txt": {"size": st.st_size,
                                  "mtime": int(st.st_mtime),
                                  "sha256": digest,
                                  "method": "content-digest"}}}))
        unit = {"id": "w", "kind": "slurm", "command": "true",
                "outputs": ["o.txt"], "write_scopes": ["w/"]}
        if promote_to:
            unit["promote_to"] = str(root / "shared")
        plan = {"name": "p", "units": [unit]}
        state_obj = {"schema_version": 1, "halted": None, "units": {
            "w": {"state": state, "attempt_dir": str(attempt),
                  "attempts": [str(attempt)], "gpu_hours": 0}}}
        sd = root / "state"
        sd.mkdir()
        return plan, state_obj, str(sd), attempt, root / "shared"

    def test_a_unit_with_no_destination_never_touches_a_shared_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st, sd, _, shared = self._project(t, promote_to=False)
            lines, ok = S.promote(plan, st, sd, "w", "hani", True)
            self.assertFalse(ok)
            self.assertFalse(shared.exists(),
                             "a unit declaring no promote_to created a shared "
                             "path anyway")

    def test_a_dry_run_copies_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st, sd, _, shared = self._project(t)
            lines, ok = S.promote(plan, st, sd, "w", None, False)
            self.assertTrue(ok)
            self.assertIn("DRY RUN", "\n".join(lines))
            self.assertFalse(shared.exists())

    def test_approval_requires_a_named_approver(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st, sd, _, shared = self._project(t)
            lines, ok = S.promote(plan, st, sd, "w", None, True)
            self.assertFalse(ok)
            self.assertFalse(shared.exists())

    def test_only_a_DONE_unit_may_be_promoted(self):
        """Promoting on any weaker basis is the false pass this repo exists to
        prevent."""
        import tempfile
        for bad in ("RUNNING", "FAILED", "INCOMPLETE", "FAILED_EVIDENCE",
                    "NEEDS_HUMAN"):
            with tempfile.TemporaryDirectory() as t:
                plan, st, sd, _, shared = self._project(t, state=bad)
                lines, ok = S.promote(plan, st, sd, "w", "hani", True)
                self.assertFalse(ok, f"{bad} was promotable")
                self.assertFalse(shared.exists())

    def test_a_changed_output_is_refused_and_the_pointer_does_not_move(self):
        """Criterion f. The receipt is evidence about a MOMENT; promotion
        happens later."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st, sd, attempt, shared = self._project(t)
            lines, ok = S.promote(plan, st, sd, "w", "hani", True)
            self.assertTrue(ok, "\n".join(lines))
            first = os.readlink(shared / "w" / "current")

            (attempt / "o.txt").write_text("tampered after the verdict\n")
            lines, ok = S.promote(plan, st, sd, "w", "hani", True)
            self.assertFalse(ok, "a changed output was promoted")
            self.assertIn("CONTENT CHANGED", "\n".join(lines))
            self.assertEqual(os.readlink(shared / "w" / "current"), first,
                             "the canonical pointer moved on a refused "
                             "promotion")

    def test_promotion_is_recorded_with_who_approved_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st, sd, _, _ = self._project(t)
            S.promote(plan, st, sd, "w", "hani", True)
            rec = json.loads((Path(sd) / S.PROMOTIONS).read_text().strip())
            for field in ("unit", "attempt", "approver", "at", "promoted_to",
                          "digest_basis"):
                self.assertIn(field, rec)
            self.assertEqual(rec["approver"], "hani")

    def test_the_canonical_name_is_a_pointer_not_a_copy_target(self):
        """Renaming into place is NOT atomic across filesystems, and a shared
        tree is usually a different mount from the run root, so a half-copied
        output would appear under the canonical name."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st, sd, _, shared = self._project(t)
            S.promote(plan, st, sd, "w", "hani", True)
            self.assertTrue((shared / "w" / "current").is_symlink())
            self.assertTrue((shared / "w" / "att1" / "o.txt").is_file())

    def test_a_weak_fingerprint_is_REFUSED_by_default(self):
        """Promotion is the one place this tool writes where other people
        read, so it does not publish on evidence that cannot establish
        unchanged content. A size+mtime match cannot see a file edited in
        place within the same mtime second."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st, sd, attempt, shared = self._project(t)
            r = json.loads((attempt / "receipt.json").read_text())
            r["outputs"]["o.txt"].pop("sha256")
            r["outputs"]["o.txt"]["method"] = "size-mtime (WEAK: over limit)"
            (attempt / "receipt.json").write_text(json.dumps(r))
            lines, ok = S.promote(plan, st, sd, "w", "hani", True)
            self.assertFalse(ok, "published on evidence that cannot establish "
                                 "unchanged content")
            self.assertFalse(shared.exists())
            self.assertIn("--accept-weak-evidence", "\n".join(lines),
                          "the refusal must name the way through; refusing "
                          "outright would make any output over the digest "
                          "limit permanently unpromotable, and a 40GB "
                          "checkpoint is exactly what is worth publishing")

    def test_weak_evidence_can_be_accepted_explicitly_and_is_recorded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st, sd, attempt, shared = self._project(t)
            r = json.loads((attempt / "receipt.json").read_text())
            r["outputs"]["o.txt"].pop("sha256")
            r["outputs"]["o.txt"]["method"] = "size-mtime (WEAK: over limit)"
            (attempt / "receipt.json").write_text(json.dumps(r))
            lines, ok = S.promote(plan, st, sd, "w", "hani", True,
                                  accept_weak=True)
            self.assertTrue(ok, "\n".join(lines))
            self.assertIn("does NOT establish", "\n".join(lines))
            rec = json.loads((Path(sd) / S.PROMOTIONS).read_text().strip())
            self.assertIn("size-mtime", rec["digest_basis"],
                          "the record must say the evidence was weak, or it "
                          "reads as stronger than it is")

class TestStatusIsTheNotificationChannel(unittest.TestCase):
    def _st(self, state, halted=None):
        plan = {"name": "p", "units": [
            {"id": "a", "kind": "slurm", "command": "true", "outputs": ["o"]},
            {"id": "b", "kind": "slurm", "command": "true", "outputs": ["o"],
             "needs": ["a"]}]}
        st = {"schema_version": 1, "halted": halted, "units": {
            "a": {"state": state, "attempt_dir": "/x", "attempts": ["/x"],
                  "gpu_hours": 2},
            "b": {"state": "HELD", "attempt_dir": None, "attempts": [],
                  "gpu_hours": 0}}}
        return plan, st

    def test_a_unit_needing_a_person_is_surfaced(self):
        import tempfile
        for s in ("NEEDS_HUMAN", "FAILED", "FAILED_EVIDENCE"):
            with tempfile.TemporaryDirectory() as t:
                plan, st = self._st(s)
                rep = S.status_report(plan, st, t)
                self.assertIn("a", rep["needs_attention"], f"{s} not surfaced")

    def test_a_healthy_swarm_asks_for_nothing(self):
        """The counter-claim. A monitor that always says "look at me" is a
        monitor that gets ignored."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st = self._st("RUNNING")
            self.assertEqual(S.status_report(plan, st, t)["needs_attention"],
                             [])

    def test_a_held_unit_says_what_is_holding_it(self):
        """"Waiting" and "will never run" need different actions from whoever
        reads the status."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st = self._st("FAILED")
            rows = {r["id"]: r for r in S.status_report(plan, st, t)["units"]}
            self.assertEqual(rows["b"]["held_by"], ["a"])

    def test_budget_is_spent_of_declared(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st = self._st("RUNNING")
            plan["budget"] = {"gpu_hours": 10}
            b = S.status_report(plan, st, t)["budget"]
            self.assertEqual((b["spent_gpu_hours"], b["remaining_gpu_hours"]),
                             (2.0, 8.0))

    def test_a_halted_swarm_says_so_in_the_report(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            plan, st = self._st("RUNNING", halted="budget exceeded")
            self.assertEqual(S.status_report(plan, st, t)["halted"],
                             "budget exceeded")

    def test_status_reads_state_only_and_launches_nothing(self):
        """It must render with the coordinator stopped, which is exactly when
        someone wants to look."""
        import ast
        src = SWARM.read_text()
        for name in ("_status_rows", "status_report"):
            fn = next(n for n in ast.parse(src).body
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            code = "\n".join(ast.unparse(x) for x in fn.body)
            for forbidden in ("U.run(", "subprocess", "sbatch", "squeue",
                              "sacct", "paseo", "_submit", "_check"):
                self.assertNotIn(forbidden, code,
                                 f"{name} calls {forbidden}: status must read "
                                 f"durable state only")


class TestTheThreeWeakPointsIFlagged(unittest.TestCase):
    """Written by attacking my own least-confident code before the review came
    back. Two of the three were real."""

    def test_re_promoting_does_not_destroy_published_data(self):
        """REAL. It did `rmtree(version)` then recopied, which destroys data
        other people may already be reading and leaves `current` pointing at
        nothing during the gap. A shared canonical tree is the one place this
        tool leaves its own sandbox."""
        src = SWARM.read_text()
        seg = src[src.index("def promote("):]
        seg = seg[:seg.index("\ndef ")]
        self.assertNotIn("shutil.rmtree(version)", seg,
                         "promotion deletes an already-published version")
        self.assertIn("already published", seg,
                      "re-promoting the same attempt must be a no-op, not a "
                      "destroy-and-recopy")

    def test_pid_identity_degrades_to_cannot_tell_not_to_yes(self):
        """REAL. `_proc_alive` read /proc only. macOS has no /proc, so the
        except branch returned True and EVERY live pid read as our engine: a
        reused pid would hold a unit at RUNNING forever."""
        sys.path.insert(0, str(SWARM.parent))
        import unit as U2
        import subprocess as sp
        proc = sp.Popen(["sleep", "8"])
        time.sleep(0.3)
        try:
            self.assertIs(U2._proc_alive(proc.pid, time.time() - 1), True)
            # The hazard, on any platform: alive, but it predates our launch,
            # so it is somebody else's process wearing a recycled pid.
            self.assertIs(U2._proc_alive(proc.pid, time.time() + 3600), False,
                          "a process that started before we launched ours was "
                          "accepted as ours")
        finally:
            proc.kill()
            proc.wait()
        self.assertIs(U2._proc_alive(proc.pid, time.time()), False)

    def test_etime_parsing_covers_the_formats_ps_actually_emits(self):
        sys.path.insert(0, str(SWARM.parent))
        import unit as U2
        for text, want in (("00:05", 5), ("01:00", 60), ("12:34:56", 45296),
                           ("2-03:04:05", 183845), ("", None), ("junk", None)):
            self.assertEqual(U2._parse_etime(text), want, f"for {text!r}")

    def test_an_unjudgeable_engine_is_not_reported_running(self):
        """The caller must treat "cannot tell" as unjudgeable. Reporting it as
        RUNNING is how a unit sits live forever."""
        src = (SWARM.parent / "unit.py").read_text()
        seg = src[src.index("def _pipeline_state("):]
        seg = seg[:seg.index("\ndef ")]
        self.assertIn("if alive is None:", seg)
        i, j = seg.index("if alive is None:"), seg.index("if alive:")
        self.assertLess(i, j, "the None case must be handled before the "
                              "truthy case, or None falls through as running")

    def test_the_engine_wrapper_records_a_status_for_awkward_commands(self):
        """NOT a defect, checked because it looked like one: `exit`, `set -e`,
        a trap, `exec`, a backgrounded child and an unbalanced paren in the
        command all still produce an exit status, because the subshell
        contains them."""
        import subprocess as sp
        import tempfile
        cases = {
            "printf ok > out.txt": "0",
            "printf ok > out.txt; exit 0": "0",
            "exit 7": "7",
            "set -e; false; printf never > out.txt": "1",
            'trap "echo t" EXIT; printf ok > out.txt': "0",
            "sleep 3 & printf ok > out.txt": "0",
            'printf "a)b" > out.txt': "0",
        }
        for cmd, want in cases.items():
            with tempfile.TemporaryDirectory() as d:
                wrapped = f'(\n{cmd}\n)\nprintf %s "$?" > engine.rc\n'
                sp.run(["sh", "-c", wrapped], cwd=d,
                       stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                got = (Path(d) / "engine.rc")
                self.assertTrue(got.is_file(),
                                f"no exit status recorded for {cmd!r}")
                self.assertEqual(got.read_text().strip(), want, f"for {cmd!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestThePromotionDestination(unittest.TestCase):
    """promote_to is a path taken from the plan and written to, on the one
    surface where this tool writes where other people read. Three ways it went
    wrong, all found by trying them rather than by reading."""

    def test_a_relative_destination_is_refused(self):
        """It resolves against the coordinator's cwd, and cron runs from a
        different directory than a shell, so one plan published to two
        different places depending on how it was invoked."""
        dest, err = S.resolve_promote_to("shared/canonical")
        self.assertIsNone(dest)
        self.assertIn("relative", err)
        self.assertIn("absolute", err, "the refusal must name the fix")

    def test_a_tilde_is_expanded_not_taken_literally(self):
        """"~/canonical" created a directory literally named "~" and put the
        results somewhere nobody would ever look."""
        dest, err = S.resolve_promote_to("~/canonical")
        self.assertIsNone(err, err)
        self.assertFalse(str(dest).startswith("~"))
        self.assertTrue(str(dest).startswith(str(Path.home())))

    def test_a_destination_inside_the_run_root_is_refused(self):
        """Promotion publishes OUT of the exclusive write area; a destination
        inside it defeats the isolation everything else rests on."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "runs"
            root.mkdir()
            dest, err = S.resolve_promote_to(str(root / "canonical"), str(root))
            self.assertIsNone(dest)
            self.assertIn("isolation", err)

    def test_an_absolute_destination_outside_the_root_is_accepted(self):
        """The counter-claim: the ordinary case must still work."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            dest, err = S.resolve_promote_to(str(Path(t) / "canonical"),
                                             str(Path(t) / "runs"))
            self.assertIsNone(err, err)
            self.assertIsNotNone(dest)

    def test_validate_refuses_a_bad_destination_before_anything_dispatches(self):
        """A plan that cannot publish should not half-run first."""
        with self.assertRaises(S.PlanError) as cm:
            S.validate_plan({"units": [
                {"id": "w", "kind": "slurm", "command": "true",
                 "outputs": ["o"], "promote_to": "relative/path"}]})
        self.assertIn("relative", str(cm.exception))


class TestDirectoryOutputs(unittest.TestCase):
    def test_a_nested_edit_is_caught_where_size_and_mtime_are_blind(self):
        """A directory's own size and mtime say NOTHING about a nested file:
        editing results/sub/n.txt changes neither, demonstrated below. Without
        a tree digest, promotion would have published a tampered tree while
        reporting that the outputs matched the receipt."""
        import tempfile
        sys.path.insert(0, str(SWARM.parent))
        import unit as U2
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            att = root / "runs" / "w" / "att1"
            att.mkdir(parents=True)
            res = att / "results"
            (res / "sub").mkdir(parents=True)
            (res / "table.csv").write_text("a,b\n1,2\n")
            (res / "sub" / "n.txt").write_text("nested\n")

            before = res.stat()
            fp = U2.fingerprint_outputs(str(att), ["results"])
            self.assertIn("tree-digest", fp["results"]["method"])
            (att / "receipt.json").write_text(json.dumps(
                {"state": "DONE", "attempt_id": "att1", "outputs": fp}))
            plan = {"name": "p", "units": [
                {"id": "w", "kind": "slurm", "command": "true",
                 "outputs": ["results"],
                 "promote_to": str(root / "canonical")}]}
            state = {"schema_version": 1, "halted": None, "units": {
                "w": {"state": "DONE", "attempt_dir": str(att),
                      "attempts": [str(att)], "gpu_hours": 0}}}
            sd = root / "state"
            sd.mkdir()
            _, ok = S.promote(plan, state, str(sd), "w", "hani", True)
            self.assertTrue(ok)

            (res / "sub" / "n.txt").write_text("TAMPERED\n")
            after = res.stat()
            # The premise of the whole branch, asserted rather than assumed.
            self.assertEqual((before.st_size, int(before.st_mtime)),
                             (after.st_size, int(after.st_mtime)),
                             "this test is vacuous unless the directory's own "
                             "size and mtime really are unchanged")
            lines, ok = S.promote(plan, state, str(sd), "w", "hani", True)
            self.assertFalse(ok, "a tampered directory tree was published")
            self.assertIn("tree changed", "\n".join(lines))
            self.assertEqual(
                (root / "canonical" / "w" / "att1" / "results" / "sub"
                 / "n.txt").read_text().strip(), "nested",
                "the published tree was overwritten by the tampered one")


class TestTheTwoWaysAUnitCanBeUnjudgeable(unittest.TestCase):
    """A clean exit with no output, and no accounting row at all, are
    different failures needing opposite actions from an operator. Calling both
    "the evidence never arrived" sent someone to `sacct` for a job whose row
    says COMPLETED 0:0, which is the one place that hides the problem."""

    def test_the_reason_is_machine_readable_not_grepped_prose(self):
        sys.path.insert(0, str(SWARM.parent))
        import unit as U2
        self.assertTrue(hasattr(U2, "REASON_NO_OUTPUTS"))
        self.assertTrue(hasattr(U2, "REASON_NO_EVIDENCE"))
        self.assertNotEqual(U2.REASON_NO_OUTPUTS, U2.REASON_NO_EVIDENCE)
        src = SWARM.read_text()
        self.assertIn("U.REASON_NO_OUTPUTS", src,
                      "the coordinator must branch on the reason code, not on "
                      "the wording of a note")

    def test_a_clean_exit_with_no_output_becomes_FAILED_not_FAILED_EVIDENCE(self):
        """Verified live on lambda: a job that exited 0 and wrote nothing."""
        src = SWARM.read_text()
        seg = src[src.index("elif time.time() - float(first) > SETTLE_S:"):]
        seg = seg[:seg.index("else:\n            us.pop")]
        self.assertIn('us["state"] = "FAILED"', seg)
        self.assertIn('us["state"] = "FAILED_EVIDENCE"', seg)
        i = seg.index("REASON_NO_OUTPUTS")
        j = seg.index('us["state"] = "FAILED"')
        self.assertLess(i, j, "FAILED must be chosen BECAUSE of the reason "
                              "code, not by falling through to it")

    def test_the_message_does_not_send_the_operator_to_sacct_for_a_clean_job(self):
        src = SWARM.read_text()
        seg = src[src.index("if reason == U.REASON_NO_OUTPUTS:"):]
        seg = seg[:seg.index("else:")]
        self.assertNotIn("sacct", seg,
                         "sacct will say this job succeeded; pointing there "
                         "is the exact confusion this tool exists to prevent")
        self.assertIn("log", seg, "it must name where the real answer is")


class TestNoStateChangeEscapesTheOutbox(unittest.TestCase):
    """Found by running a real failing DAG on lambda and then reading the
    outbox. A unit that exited 0 and wrote nothing reached FAILED through the
    settle branch and told the tracker NOTHING, because emission happened at
    three specific sites and that transition was made at a fourth. Its issue
    would have sat on "work started" forever."""

    def test_intents_come_from_the_final_state_not_from_call_sites(self):
        src = SWARM.read_text()
        self.assertLessEqual(src.count("emit_intent("), 2,
                             "emitting at each place a state is set will miss "
                             "whichever path is added next; emit once from the "
                             "final state")
        i = src.index("before_states = {")
        j = src.index("for uid in sorted(units):\n        us = _unit_state")
        self.assertLess(i, j, "the before-snapshot must precede the diff")

    def test_a_terminal_state_reached_late_in_an_advance_still_emits(self):
        """The exact escape: state set in the settle branch, long after the
        point where emission used to happen."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "plan.json").write_text(json.dumps(PLAN))
            sd = tmp / "st"
            # Dispatch, then force both units terminal the way the settle
            # branch does: by writing state directly, not via _check.
            subprocess.run([sys.executable, str(SWARM), "run",
                            str(tmp / "plan.json"), "--dry-run",
                            "--state-dir", str(sd)],
                           capture_output=True, text=True, cwd=tmp)
            st = json.loads((sd / "swarm-state.json").read_text())
            # attempt_dir must be cleared too, or the advance RE-CHECKS prep
            # and the verdict overwrites FAILED before the DAG is walked. My
            # first version of this test missed that and asserted against a
            # scenario that never happened.
            st["units"]["prep"]["state"] = "FAILED"
            st["units"]["prep"]["attempt_dir"] = None
            (sd / "swarm-state.json").write_text(json.dumps(st))
            (sd / "outbox.jsonl").unlink(missing_ok=True)
            subprocess.run([sys.executable, str(SWARM), "advance",
                            str(tmp / "plan.json"), "--dry-run",
                            "--state-dir", str(sd)],
                           capture_output=True, text=True, cwd=tmp)
            emitted = {(i["unit"], i["unit_state"])
                       for i in S.read_outbox(sd)}
            self.assertIn(("train", "HELD"), emitted,
                          f"a state reached during the advance did not emit: "
                          f"{emitted}")

    def test_an_unchanged_unit_does_not_re_emit_every_advance(self):
        """The counter-claim. A diff against the previous state must not turn
        a quiet DAG into a stream of duplicate tracker updates."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "plan.json").write_text(json.dumps(PLAN))
            sd = tmp / "st"
            for _ in range(3):
                subprocess.run([sys.executable, str(SWARM), "advance",
                                str(tmp / "plan.json"), "--dry-run",
                                "--state-dir", str(sd)],
                               capture_output=True, text=True, cwd=tmp)
            keys = [i["key"] for i in S.read_outbox(sd)]
            self.assertEqual(len(keys), len(set(keys)),
                             "an unchanged unit emitted twice")
