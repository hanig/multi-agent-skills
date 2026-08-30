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
        # DERIVE both sides. This list used to be hand-written, so adding a
        # state that the coordinator genuinely produces failed the guard until
        # somebody remembered to edit it here too -- a guard that cries wolf
        # about correct code is one that gets edited away rather than heeded.
        # Every literal assigned to us["state"] counts as produced.
        import ast
        src = SWARM.read_text()
        produced = set(S.NAME.values())
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "us"
                        and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == "state"
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    produced.add(node.value.value)
        self.assertIn("READY_FOR_PR", produced,
                      "the deriver missed a state the coordinator sets")
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
# Contender run as a SEPARATE PROCESS. Real processes, real kernel locking,
# no in-process synchronisation that could mask the race being tested.
_CONTENDER = """
import sys, time
sys.path.insert(0, %r)
import swarm as S
state_dir, start = sys.argv[1], float(sys.argv[2])
while time.time() < start - 0.005:
    time.sleep(0.001)
while time.time() < start:
    pass
ok, _ = S.acquire_lease(state_dir)
print("WON" if ok else "lost")
if ok:
    time.sleep(1.5)          # hold it while the rest try
"""


class TestOnlyOneControllerRuns(unittest.TestCase):
    """The property five hand-rolled lease versions were trying to provide,
    now stated against the OS lock. These are unchanged in intent from the
    tests that guarded the old scheme; only the mechanism beneath them moved."""

    TRIALS = 3

    def _contend(self, state_dir, n):
        import tempfile
        src = Path(tempfile.mkdtemp()) / "contend.py"
        src.write_text(_CONTENDER % str(SWARM.parent))
        start = time.time() + 2.0
        procs = [subprocess.Popen(
            [sys.executable, str(src), str(state_dir), str(start)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(n)]
        return [p.communicate(timeout=90)[0].strip() for p in procs]

    def test_many_concurrent_acquirers_yield_exactly_one_winner(self):
        import tempfile
        for i in range(self.TRIALS):
            with tempfile.TemporaryDirectory() as d:
                out = self._contend(d, 12)
                self.assertEqual(out.count("WON"), 1,
                                 f"trial {i}: expected exactly one controller "
                                 f"to win, got {out.count('WON')}: {out}")

    def test_a_held_lock_is_never_taken(self):
        """The counter-claim: exclusion must actually exclude."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ok, _ = S.acquire_lease(d)
            self.assertTrue(ok)
            try:
                out = self._contend(d, 6)
                self.assertEqual(out.count("WON"), 0,
                                 f"contenders took a held lock: {out}")
            finally:
                S.release_lease(d)

    def test_releasing_frees_it_for_the_next_controller(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ok, _ = S.acquire_lease(d)
            self.assertTrue(ok)
            S.release_lease(d)
            out = self._contend(d, 4)
            self.assertEqual(out.count("WON"), 1, out)

    def test_two_projects_in_one_process_lock_independently(self):
        """A single global fd made acquire return True for ANY state directory
        once one had been acquired, so a second project was never locked at
        all. Found by the new tests on the first run."""
        import tempfile
        with tempfile.TemporaryDirectory() as a, \
                tempfile.TemporaryDirectory() as b:
            self.assertTrue(S.acquire_lease(a)[0])
            try:
                out = self._contend(b, 3)
                self.assertEqual(out.count("WON"), 1,
                                 f"holding project A's lock wrongly blocked "
                                 f"or granted project B: {out}")
            finally:
                S.release_lease(a)


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


class TestRound2ReviewFindings(unittest.TestCase):
    """A four-reviewer implementation review returned REVIEW_FAIL with a
    CRITICAL. These pin each confirmed finding against the failure described."""

    def test_cannot_tell_is_not_treated_as_lease_loss(self):
        """A transient NFS read returned False, the caller treated it as loss,
        and a STICKY halted flag stopped a healthy project until a human
        edited durable state. The discipline elsewhere is "cannot tell"."""
        src = SWARM.read_text()
        self.assertIn("renew_lease(state_dir) is False", src,
                      "only an explicit False may stop the advance; None "
                      "means cannot tell")

    def test_accept_weak_evidence_still_compares_size_and_mtime(self):
        """Two reviewers: the weak branch returned success without comparing
        ANYTHING, so a changed output was published while the record asserted
        a size-and-mtime match that never happened -- a false statement in the
        audit trail of the one outward-facing surface."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            att = root / "att"
            att.mkdir()
            (att / "o.txt").write_text("original\n")
            st = (att / "o.txt").stat()
            rec = {"size": st.st_size, "mtime": int(st.st_mtime),
                   "method": "size-mtime (WEAK: over the digest limit)"}
            good, _ = S._redigest(str(att), "o.txt", rec, True)
            self.assertTrue(good, "an unchanged output must still pass")
            (att / "o.txt").write_text("completely different content here\n")
            good, why = S._redigest(str(att), "o.txt", rec, True)
            self.assertFalse(good, "a changed output was accepted under "
                                   "--accept-weak-evidence")
            self.assertIn("does not waive", why)

    def test_the_copy_is_re_verified_before_the_pointer_moves(self):
        """Fingerprints are checked before copying, and an ordinary concurrent
        writer can change the source in between, so what landed in staging is
        the only thing worth trusting."""
        src = SWARM.read_text()
        seg = src[src.index("def promote("):]
        seg = seg[:seg.index("\ndef ")]
        i = seg.index("_redigest(staging")
        j = seg.index("os.replace(staging, version)")
        self.assertLess(i, j, "the copy must be verified BEFORE it takes the "
                              "canonical name")

    def test_a_brace_in_paseos_preamble_does_not_break_the_id_parse(self):
        """'Tip: reuse with --workspace {id}' made the parser start at that
        brace, fail, and leave a launched agent unbound."""
        out = ('Created workspace wks_x - p\n'
               'Tip: reuse with --workspace {id}\n'
               '{"agentId": "the-real-one"}')
        self.assertEqual((S._paseo_json(out) or {}).get("agentId"),
                         "the-real-one")

    def test_unit_py_parses_paseo_output_as_robustly_as_swarm_py(self):
        """The tolerant extractor existed and this call site used raw
        json.loads, so a completed agent with every output present would be
        reported INCOMPLETE and stall its dependents."""
        sys.path.insert(0, str(SWARM.parent))
        import unit as U2
        out = 'Some notice {with a brace}\n{"Status": "idle"}'
        self.assertEqual((U2._json_object_in(out) or {}).get("Status"), "idle")
        seg = (SWARM.parent / "unit.py").read_text()
        seg = seg[seg.index("def _code_state("):]
        seg = seg[:seg.index("\ndef ")]
        self.assertNotIn("json.loads(out)", seg)

    def test_one_bad_line_does_not_discard_the_promotions_log(self):
        """A crash during append leaves a partial record; discarding the whole
        file showed promoted units as NOT promoted, inviting a second publish."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / S.PROMOTIONS).write_text(
                json.dumps({"unit": "a", "promoted_to": "/x", "approver": "h"})
                + "\n{ partial-write-from-a-crash\n"
                + json.dumps({"unit": "b", "promoted_to": "/y",
                              "approver": "h"}) + "\n")
            plan = {"name": "p", "units": [
                {"id": "a", "kind": "slurm", "command": "true",
                 "outputs": ["o"], "promote_to": "/x"},
                {"id": "b", "kind": "slurm", "command": "true",
                 "outputs": ["o"], "promote_to": "/y"}]}
            state = {"schema_version": 1, "halted": None, "units": {}}
            rows = {r["id"]: r for r in S._status_rows(plan, state, t)}
            self.assertEqual(rows["a"]["promoted"], "/x")
            self.assertEqual(rows["b"]["promoted"], "/y",
                             "a record after the bad line was lost")

    def test_orphan_recovery_matches_an_agent_exactly(self):
        """`attempt_id in name` also matched an attempt whose id is a prefix
        of another's, binding a unit to somebody else's agent."""
        src = SWARM.read_text()
        seg = src[src.index('if kind == "code":'):]
        seg = seg[:seg.index("name = f\"swarm-")]
        self.assertNotIn("attempt_id in str(", seg)
        self.assertIn("[attempt_id]", seg)

    def test_exec_in_a_pipeline_command_was_a_false_finding(self):
        """One reviewer said `exec true` leaves engine.rc absent. Measured
        instead of accepted: exec replaces the SUBSHELL, so the outer status
        line still runs. Kept as a test so the refutation stays checked."""
        import subprocess as sp
        import tempfile
        for cmd, want in (("exec true", "0"), ("exec false", "1")):
            with tempfile.TemporaryDirectory() as d:
                wrapped = f'(\n{cmd}\n)\nprintf %s "$?" > engine.rc\n'
                sp.run(["sh", "-c", wrapped], cwd=d,
                       stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                self.assertEqual((Path(d) / "engine.rc").read_text().strip(),
                                 want, f"for {cmd!r}")


class TestTheLockIsArbitratedByTheOS(unittest.TestCase):
    """The sixth lease. Five hand-rolled versions preceded it and two
    independent reviews found a CRITICAL in each of the last two. These test
    the properties that motivated the change, not the implementation."""

    def test_the_filesystem_under_the_test_really_excludes(self):
        """NFS can silently degrade advisory locking to local-only, which
        would be WORSE than the old scheme because it would look like it
        worked. Verified here on whatever filesystem the suite runs on, and
        measured separately on lambda (nfs), chimera (nfs4) and andromeda
        (weka): 10 contenders, three trials, one winner every time."""
        import subprocess as sp
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            probe = Path(d) / "probe.py"
            probe.write_text(
                "import fcntl, os, sys, time\n"
                "path, start = sys.argv[1], float(sys.argv[2])\n"
                "while time.time() < start - 0.005: time.sleep(0.001)\n"
                "while time.time() < start: pass\n"
                "fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)\n"
                "try:\n"
                "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "except OSError:\n"
                "    print('lost'); sys.exit(0)\n"
                "print('WON', flush=True)\n"
                "time.sleep(1.5)\n")
            start = time.time() + 1.5
            procs = [sp.Popen([sys.executable, str(probe),
                               str(Path(d) / "L"), str(start)],
                              stdout=sp.PIPE, text=True) for _ in range(10)]
            out = [p.communicate(timeout=60)[0].strip() for p in procs]
            self.assertEqual(out.count("WON"), 1,
                             f"advisory locking does not exclude across "
                             f"processes on this filesystem: {out}")

    def test_a_dead_holder_frees_the_project_with_no_cleanup(self):
        """THE reason for the change. Every defect in five rewrites lived in
        machinery that existed because a plain file cannot tell you its owner
        died: the TTL, the breaker, the token, the mtime heuristic. A killed
        holder used to wedge the project until a human deleted a directory."""
        import subprocess as sp
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            holder_src = Path(d) / "holder.py"
            holder_src.write_text(
                "import sys, time\n"
                f"sys.path.insert(0, {str(SWARM.parent)!r})\n"
                "import swarm as S\n"
                "ok, _ = S.acquire_lease(sys.argv[1])\n"
                "print('HELD' if ok else 'no', flush=True)\n"
                "time.sleep(120)\n")
            sd = str(Path(d) / "st")
            holder = sp.Popen([sys.executable, str(holder_src), sd],
                              stdout=sp.PIPE, text=True)
            self.assertEqual(holder.stdout.readline().strip(), "HELD")
            ok, why = S.acquire_lease(sd)
            self.assertFalse(ok, "acquired a lock a live process holds")
            holder.kill()
            holder.wait(timeout=15)
            ok, why = S.acquire_lease(sd)
            self.assertTrue(ok, f"a killed holder still blocks the project: "
                                f"{why}")
            S.release_lease(sd)

    def test_a_slow_holder_is_never_deposed(self):
        """The CRITICAL from round 2: a holder merely PAUSED past the TTL had
        its breaker stolen and then clobbered its successor. There is no TTL
        now, so being slow cannot cost the lock."""
        import subprocess as sp
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            holder_src = Path(d) / "holder.py"
            holder_src.write_text(
                "import sys, time\n"
                f"sys.path.insert(0, {str(SWARM.parent)!r})\n"
                "import swarm as S\n"
                "ok, _ = S.acquire_lease(sys.argv[1])\n"
                "print('HELD' if ok else 'no', flush=True)\n"
                "time.sleep(6)\n")
            sd = str(Path(d) / "st")
            holder = sp.Popen([sys.executable, str(holder_src), sd],
                              stdout=sp.PIPE, text=True)
            self.assertEqual(holder.stdout.readline().strip(), "HELD")
            stolen = 0
            deadline = time.time() + 3
            while time.time() < deadline:
                ok, _ = S.acquire_lease(sd)
                if ok:
                    stolen += 1
                    S.release_lease(sd)
            holder.wait(timeout=20)
            self.assertEqual(stolen, 0,
                             f"{stolen} contender(s) took the lock from a live "
                             f"holder that was simply slow")

    def test_renewal_is_not_a_clock_any_more(self):
        """There is nothing to renew: the lock is held until the process
        releases it or exits. A timestamp that had to be refreshed is what let
        a paused controller be deposed."""
        import ast
        src = SWARM.read_text()
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef) and n.name == "renew_lease")
        code = "\n".join(ast.unparse(x) for x in fn.body)
        for gone in ("expires_at", "LEASE_TTL_S", "os.replace", "unlink"):
            self.assertNotIn(gone, code,
                             f"renewal still manipulates {gone}; the lock "
                             f"should need no refreshing at all")

    def test_the_hand_rolled_machinery_is_gone(self):
        """Closure by exclusion: if any of it survives, some path still
        depends on the reasoning that failed five times."""
        src = SWARM.read_text()
        for gone in ("_Breaker", "_HELD_TOKEN", "_publish_lease",
                     "lease.json.break", "LEASE + \".break\""):
            self.assertNotIn(gone, src, f"{gone} still present")

    def test_the_lease_file_decides_nothing(self):
        """It exists so a human blocked by the lock can see who holds it. If
        a decision were taken from it, the file would be load-bearing again."""
        import ast
        src = SWARM.read_text()
        for name in ("acquire_lease", "renew_lease", "release_lease"):
            fn = next(n for n in ast.parse(src).body
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            code = "\n".join(ast.unparse(x) for x in fn.body)
            self.assertNotIn("acquired_at') >", code)
            self.assertNotIn("expires_at", code,
                             f"{name} still decides something from the "
                             f"descriptive lease file")


class TestPlanIsRefusedForTheWrongCluster(unittest.TestCase):
    """Plan criterion 5(f). A project runs on ONE server and its plan carries
    that server's sbatch flags, so a plan written for lambda and run on
    chimera names partitions that do not exist there."""

    def test_a_partition_this_cluster_lacks_is_refused(self):
        bad = S.partition_problems(
            [{"id": "prep", "sbatch": ["--partition=labinloop", "--time=5"]},
             {"id": "ok", "sbatch": ["--partition=cpu"]}],
            known={"cpu", "gpu"})
        self.assertEqual(bad, [("prep", "labinloop")])

    def test_a_partition_this_cluster_has_is_accepted(self):
        self.assertEqual(
            S.partition_problems([{"id": "a", "sbatch": ["--partition=cpu"]}],
                                 known={"cpu", "gpu"}), [])

    def test_the_default_partition_star_is_stripped(self):
        """`sinfo -o %P` marks the default with a trailing *, and comparing
        the raw string would reject the one partition most plans use."""
        self.assertEqual(
            S.partition_problems([{"id": "a", "sbatch": ["--partition=cpu"]}],
                                 known={"cpu", "gpu"}), [],
            "the default partition must match after the * is stripped")
        rc, out, _ = (0, "cpu*\ngpu\n", "")
        names = {l.strip().rstrip("*") for l in out.splitlines() if l.strip()}
        self.assertIn("cpu", names)

    def test_an_unknown_partition_list_refuses_NOTHING(self):
        """The cries-wolf guard. A host without sinfo, or a scheduler that did
        not answer, is UNKNOWN, not empty. Refusing on unknown would block
        every plan on the first flaky day, and this repo weights that as
        seriously as a false pass."""
        self.assertEqual(
            S.partition_problems(
                [{"id": "a", "sbatch": ["--partition=anything-at-all"]}],
                known=None), [],
            "an unknown partition list must not refuse a plan")
        self.assertEqual(
            S.partition_problems(
                [{"id": "a", "sbatch": ["--partition=x"]}], known=set()), [],
            "an empty partition list means the query failed, not that the "
            "cluster has no partitions")

    def test_a_unit_with_no_sbatch_flags_is_not_refused(self):
        self.assertEqual(S.partition_problems([{"id": "a"}], known={"cpu"}), [])


class TestLockErrorsAreNotAllContention(unittest.TestCase):
    """Found by following the instruction I gave the reviewers rather than
    waiting for their answer. A bare `except OSError` reported every failure
    as contention, including one that means the opposite."""

    def _fail_with(self, err):
        import errno as E
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("fcntl.flock",
                            side_effect=OSError(err, os.strerror(err))):
                return S.acquire_lease(d)

    def test_a_filesystem_that_cannot_lock_is_not_reported_as_contention(self):
        """ENOLCK means nothing can guarantee single-controller operation. On
        such a mount every advance would refuse forever, blaming a controller
        that does not exist: a verifier crying wolf, which this repo weights
        equally with a false pass."""
        import errno as E
        ok, why = self._fail_with(E.ENOLCK)
        self.assertFalse(ok)
        self.assertIn("cannot lock", why)
        self.assertIn("NOT contention", why)
        self.assertNotIn("another controller holds it", why)

    def test_real_contention_still_reads_as_contention(self):
        import errno as E
        ok, why = self._fail_with(E.EAGAIN)
        self.assertFalse(ok)
        self.assertIn("another controller", why)

    def test_the_refusal_names_an_action(self):
        import errno as E
        _, why = self._fail_with(E.ENOLCK)
        self.assertTrue(
            any(w in why for w in ("Put the state directory", "run the "
                                   "coordinator")),
            f"a refusal must name what the operator can do: {why}")

    def test_an_interrupted_lock_is_retried_not_judged(self):
        """EINTR is a signal, not a verdict."""
        import ast
        src = SWARM.read_text()
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "acquire_lease")
        code = "\n".join(ast.unparse(x) for x in fn.body)
        self.assertIn("EINTR", code)

    def test_the_lock_fd_is_not_inherited_by_spawned_children(self):
        """The coordinator spawns sbatch, unit.py and paseo. If any inherited
        the lock fd, a long-lived child would hold the project after the
        coordinator exited. Python 3.4+ makes fds non-inheritable by default;
        asserted rather than assumed, because it is load-bearing here."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ok, _ = S.acquire_lease(d)
            self.assertTrue(ok)
            try:
                fd = S._LOCK_FDS[str(Path(d).resolve())]
                self.assertFalse(os.get_inheritable(fd),
                                 "the lock fd would be inherited by sbatch, "
                                 "unit.py and paseo children")
            finally:
                S.release_lease(d)


class TestRound3ReviewFindings(unittest.TestCase):
    """Round 3 returned REVIEW_FAIL. Its largest finding (every flock error
    read as contention) was already fixed before the verdict landed, which is
    corroboration rather than news. These pin the rest."""

    def test_a_forked_child_is_not_granted_the_parents_lock(self):
        """CRITICAL. flock is held per OPEN FILE DESCRIPTION, which a fork
        SHARES, so a child inherited _LOCK_FDS and was told it already held a
        lock it never took.

        Uses a REAL fork. The earlier version simulated one by mutating the
        owner pid, which stopped being a valid simulation once the guard began
        closing inherited descriptors: in one process, closing the only fd
        genuinely releases the lock, so the test failed while the code was
        right. A property about forking has to fork."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ok, _ = S.acquire_lease(d)
            self.assertTrue(ok)
            try:
                r, w = os.pipe()
                pid = os.fork()
                if pid == 0:                       # child
                    try:
                        os.close(r)
                        got, _why = S.acquire_lease(d)
                        os.write(w, b"GRANTED" if got else b"refused")
                        os.close(w)
                    finally:
                        os._exit(0)
                os.close(w)
                verdict = os.read(r, 32).decode()
                os.close(r)
                os.waitpid(pid, 0)
                self.assertEqual(
                    verdict, "refused",
                    "a forked child was granted its parent's lock, so two "
                    "controllers could advance the same DAG")
            finally:
                S.release_lease(d)

    def test_the_parent_keeps_its_lock_after_the_child_exits(self):
        """The counter-claim to the fix. Closing inherited descriptors in the
        child must NOT release the parent's lock: they share one open file
        description, and the parent still references it."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(S.acquire_lease(d)[0])
            try:
                pid = os.fork()
                if pid == 0:
                    try:
                        S.acquire_lease(d)          # triggers the fd close
                    finally:
                        os._exit(0)
                os.waitpid(pid, 0)
                self.assertTrue(
                    S.renew_lease(d),
                    "the parent lost its lock because a child closed an "
                    "inherited descriptor")
            finally:
                S.release_lease(d)

    def test_a_replaced_lock_file_is_detected(self):
        """CRITICAL. A flock is on an INODE, not a name. If lease.lock is
        deleted, a second controller creates a new inode at the same path and
        locks it successfully, and both then believe they are alone."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ok, _ = S.acquire_lease(d)
            self.assertTrue(ok)
            try:
                self.assertTrue(S.renew_lease(d))
                # Somebody replaces the lock file underneath us.
                (Path(d) / S.LOCK).unlink()
                (Path(d) / S.LOCK).write_text("")
                self.assertFalse(
                    S.renew_lease(d),
                    "the coordinator still believed it held the lock after "
                    "the file it locked was replaced")
            finally:
                S.release_lease(d)

    def test_a_deleted_lock_file_is_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(S.acquire_lease(d)[0])
            try:
                (Path(d) / S.LOCK).unlink()
                self.assertFalse(S.renew_lease(d))
            finally:
                S.release_lease(d)

    def test_the_lock_survives_a_child_process_but_not_the_holder(self):
        """Refuted by measurement, kept so the refutation stays checked: one
        reviewer said the fd lacks O_CLOEXEC and so survives exec. Python has
        made fds non-inheritable by default since 3.4."""
        import subprocess as sp
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(S.acquire_lease(d)[0])
            code = ("import fcntl,os,sys\n"
                    "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600)\n"
                    "try:\n"
                    "    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                    "    print('took')\n"
                    "except OSError: print('blocked')\n")
            lock = str(Path(d) / S.LOCK)
            r = sp.run([sys.executable, "-c", code, lock],
                       capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "blocked")
            S.release_lease(d)
            r = sp.run([sys.executable, "-c", code, lock],
                       capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "took")

    def test_the_docstring_does_not_claim_the_suite_checks_cross_client(self):
        """A reviewer showed every contender in the filesystem test runs on
        ONE host, and local-only locking still excludes same-host processes
        perfectly, so the test cannot tell the two apart. The docstring said
        it re-checks that property. Claiming a check that cannot exist is the
        over-claiming this project keeps having to walk back."""
        import ast
        src = SWARM.read_text()
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "acquire_lease")
        doc = ast.get_docstring(fn) or ""
        self.assertIn("does not show", doc.lower().replace("not show",
                                                           "not show"))
        self.assertIn("ONE host", doc)
        self.assertIn("KNOWN LIMIT", doc,
                      "the NFS lock-recovery limit must be recorded, not "
                      "papered over")


class TestUnknownIsNotTheSameAsLookItUp(unittest.TestCase):
    """Found by running the suite on andromeda, where it failed while passing
    on a laptop. `known=None` meant BOTH "could not be determined" and "go
    look it up", and those coincide only on a machine without sinfo."""

    UNITS = [{"id": "a", "sbatch": ["--partition=anything-at-all"]}]

    def test_explicit_unknown_refuses_nothing_even_on_a_slurm_host(self):
        """This is the assertion that was accidentally host-dependent: on a
        cluster it triggered a live lookup and reported a problem."""
        self.assertEqual(S.partition_problems(self.UNITS, known=None), [])

    def test_an_empty_result_also_refuses_nothing(self):
        """An empty query result is not evidence that a partition is absent."""
        self.assertEqual(S.partition_problems(self.UNITS, known=set()), [])

    def test_a_real_list_still_refuses_a_foreign_partition(self):
        self.assertEqual(S.partition_problems(self.UNITS, known={"cpu"}),
                         [("a", "anything-at-all")])

    def test_the_lookup_default_is_a_distinct_sentinel(self):
        """If the default were None again, the ambiguity returns and the bug
        with it."""
        import inspect
        default = inspect.signature(S.partition_problems).parameters["known"].default
        self.assertIsNot(default, None,
                         "None must not mean 'look it up' as well as 'unknown'")


class TestAUnitCanFindWhatItConsumes(unittest.TestCase):
    """Found by running a DAG whose downstream unit actually READS its
    upstream's output, which none of the earlier tests did. Nothing told a
    unit where its dependency's outputs were: the script cds into the unit's
    own exclusive directory, so an author had to glob `../../dep/*/file`,
    which matches two directories the moment that dep retries."""

    def test_each_dependency_is_exported_by_name(self):
        state = {"units": {
            "align-reads": {"attempt_dir": "/runs/align-reads/att1"},
            "index": {"attempt_dir": "/runs/index/att9"}}}
        got = dict(S._dep_env({"id": "call", "needs": ["align-reads", "index"]},
                              state))
        self.assertEqual(got["SWARM_DEP_ALIGN_READS"], "/runs/align-reads/att1")
        self.assertEqual(got["SWARM_DEP_INDEX"], "/runs/index/att9")

    def test_it_names_the_CURRENT_attempt_not_a_glob(self):
        """The whole point: after a retry there are two attempt directories,
        and only the coordinator knows which one is live."""
        state = {"units": {"prep": {"attempt_dir": "/runs/prep/attempt-2"}}}
        got = dict(S._dep_env({"id": "x", "needs": ["prep"]}, state))
        self.assertEqual(got["SWARM_DEP_PREP"], "/runs/prep/attempt-2")

    def test_a_dependency_with_no_attempt_yet_is_omitted(self):
        """Exporting an empty path would let a command silently read from the
        wrong place instead of failing."""
        self.assertEqual(
            S._dep_env({"id": "x", "needs": ["nothing-yet"]},
                       {"units": {"nothing-yet": {"attempt_dir": None}}}), [])

    def test_paths_are_quoted_in_the_generated_script(self):
        """We generate a shell script. A directory with a space in its name
        must stay one word."""
        src = SWARM.read_text()
        self.assertIn("shlex.quote(v)", src)
        self.assertIn("shlex.quote(str(unit_dir))", src)

    def test_the_unit_learns_its_own_id_and_directory_too(self):
        src = SWARM.read_text()
        self.assertIn("SWARM_UNIT_ID", src)
        self.assertIn("SWARM_UNIT_DIR", src)


class TestADryRunCannotWedgeARealProject(unittest.TestCase):
    """A reviewer flagged this weeks ago and I recorded it without fixing it.
    It then bit the first real user on their first run: a dry run against the
    live state directory recorded a placeholder job id, the reconcile net
    skips anything holding a job id, and the unit could never be judged or
    re-dispatched. Their only way out was to discard all state."""

    PLAN = {"name": "dw", "units": [
        {"id": "a", "kind": "slurm", "command": "true", "outputs": ["o"],
         "write_scopes": ["d/a/"]}]}

    def _run(self, tmp, *argv):
        (tmp / "plan.json").write_text(json.dumps(self.PLAN))
        return subprocess.run(
            [sys.executable, str(SWARM), *argv, str(tmp / "plan.json"),
             "--state-dir", str(tmp / "st"), "--root", str(tmp / "rn")],
            capture_output=True, text=True, cwd=tmp)

    def test_a_dry_placeholder_is_cleared_not_treated_as_bound(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._run(tmp, "run", "--dry-run")
            st = json.loads((tmp / "st" / "swarm-state.json").read_text())
            self.assertTrue(str(st["units"]["a"]["job_id"]).startswith("dry-"))
            r = self._run(tmp, "advance")
            self.assertIn("clearing a dry-run placeholder", r.stdout,
                          f"the unit stayed wedged:\n{r.stdout}")

    def test_a_dry_run_refuses_a_state_dir_holding_real_attempts(self):
        """The prevention, not just the cure: a dry run must not contaminate
        a project that is genuinely running."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._run(tmp, "run", "--dry-run")
            st_path = tmp / "st" / "swarm-state.json"
            st = json.loads(st_path.read_text())
            st["units"]["a"] = {"state": "SUBMITTED", "job_id": "2868624",
                                "attempt_dir": "/x", "attempts": ["/x"],
                                "gpu_hours": 0}
            st_path.write_text(json.dumps(st))
            r = self._run(tmp, "run", "--dry-run")
            self.assertIn("REFUSING to dry-run", r.stdout)
            self.assertIn("--state-dir", r.stdout,
                          "the refusal must name the way through")
            after = json.loads(st_path.read_text())
            self.assertEqual(after["units"]["a"]["job_id"], "2868624",
                             "the dry run modified live state anyway")

    def test_a_dry_run_on_a_clean_directory_still_works(self):
        """The counter-claim. Refusing every dry run would remove the only way
        to check a DAG's shape without a scheduler."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            r = self._run(tmp, "run", "--dry-run")
            self.assertIn("submitted dry-", r.stdout, r.stdout)


class TestRetryExposureIsDeclared(unittest.TestCase):
    """Plan step 8. A unit is the retry boundary and nothing said so, which
    produced a five-unit plan whose hash stage risked ~4 TiB of re-reading.
    Exposure is now DECLARED and checked arithmetically; it is never inferred
    from a partition name or a walltime, because none of those establishes how
    much work is lost and a warning built on them cries wolf."""

    def _plan(self, **unit):
        u = {"id": "hash", "kind": "slurm", "command": "true",
             "outputs": ["o"], "write_scopes": ["h/"]}
        u.update(unit)
        p = {"name": "p", "units": [u]}
        return p

    def test_the_default_is_one_attempt(self):
        """It was 3, so every unit silently carried three times its stated
        exposure and nobody had asked for that."""
        self.assertEqual(S.DEFAULT_MAX_ATTEMPTS, 1)
        S.validate_plan(self._plan())          # no contract needed at 1

    def test_repetition_without_a_contract_is_refused(self):
        with self.assertRaises(S.PlanError) as cm:
            S.validate_plan(self._plan(max_attempts=3))
        self.assertIn("retry", str(cm.exception))
        self.assertIn("FRESH EMPTY", str(cm.exception))

    def test_exposure_needs_a_project_limit_to_be_judged_against(self):
        with self.assertRaises(S.PlanError) as cm:
            S.validate_plan(self._plan(
                max_attempts=3,
                retry={"mode": "restart", "max_lost": {"read_bytes": 10**12}}))
        self.assertIn("no retry_limits", str(cm.exception))

    def test_exposure_over_the_limit_is_refused(self):
        p = self._plan(max_attempts=3,
                       retry={"mode": "restart",
                              "max_lost": {"read_bytes": 10**12}})
        p["retry_limits"] = {"read_bytes": 10**11}
        with self.assertRaises(S.PlanError) as cm:
            S.validate_plan(p)
        self.assertIn("over the project limit", str(cm.exception))

    def test_exposure_within_the_limit_passes(self):
        """The counter-claim: the sharded plan this rule exists to produce
        must actually validate."""
        p = self._plan(max_attempts=3,
                       retry={"mode": "restart",
                              "max_lost": {"read_bytes": 9 * 10**10}})
        p["retry_limits"] = {"read_bytes": 10**11}
        S.validate_plan(p)

    def test_resume_is_refused_as_unsupported(self):
        """Cross-attempt handoff is not built. Accepting the declaration would
        ship a claim ahead of the mechanism."""
        p = self._plan(max_attempts=3,
                       retry={"mode": "resume", "max_lost": {"items": 1}})
        p["retry_limits"] = {"items": 1}
        with self.assertRaises(S.PlanError) as cm:
            S.validate_plan(p)
        self.assertIn("NOT SUPPORTED", str(cm.exception))

    def test_a_typo_in_a_metric_is_caught(self):
        p = self._plan(max_attempts=2,
                       retry={"mode": "restart",
                              "max_lost": {"read_byte": 1}})
        p["retry_limits"] = {"read_bytes": 10}
        with self.assertRaises(S.PlanError):
            S.validate_plan(p)

    def test_exposure_is_never_inferred_from_a_proxy(self):
        """Sol's correction to my first draft, and the mistake this repo keeps
        making: a partition name and a walltime do not establish how much work
        is lost."""
        # EXECUTABLE code only. Grepping the source matched the comment that
        # explains these are not used -- the third time in this project a
        # guard has matched the prose describing its own absence. ast.unparse
        # drops comments, so only real code is examined.
        import ast
        src = SWARM.read_text()
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "validate_plan")
        code = "\n".join(ast.unparse(x) for x in fn.body)
        i = code.index("retry_limits")
        j = code.index("partition_problems")
        seg = code[i:j]
        for proxy in ("preemptible", "--time", "gpu_hours", "sbatch"):
            self.assertNotIn(proxy, seg,
                             f"exposure is being inferred from {proxy!r}")


class TestLiveConcurrencyIsBounded(unittest.TestCase):
    """Splitting a unit for retry safety turns one reader into sixteen. Sol
    caught that my own advice, taken alone, traded one problem for a worse one
    on a filesystem shared by ~18 people."""

    def _sharded(self, n=16, **limits):
        units = [{"id": f"hash-{i:02d}", "kind": "slurm", "command": "true",
                  "outputs": ["o"], "pool": "shared-fs-read",
                  "write_scopes": [f"h/{i}/"]} for i in range(n)]
        return {"name": "p", "limits": limits, "units": units}

    def test_a_pool_caps_simultaneous_dispatch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "plan.json").write_text(json.dumps(
                self._sharded(max_running=8, pools={"shared-fs-read": 2})))
            r = subprocess.run(
                [sys.executable, str(SWARM), "run", str(tmp / "plan.json"),
                 "--dry-run", "--state-dir", str(tmp / "st"),
                 "--root", str(tmp / "rn")],
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(r.stdout.count("submitted dry-"), 2,
                             f"pool cap not enforced:\n{r.stdout}")
            self.assertIn("pool 'shared-fs-read' full", r.stdout)

    def test_max_running_caps_the_whole_project(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            plan = self._sharded(max_running=3)
            for u in plan["units"]:
                u.pop("pool")
            (tmp / "plan.json").write_text(json.dumps(plan))
            r = subprocess.run(
                [sys.executable, str(SWARM), "run", str(tmp / "plan.json"),
                 "--dry-run", "--state-dir", str(tmp / "st"),
                 "--root", str(tmp / "rn")],
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(r.stdout.count("submitted dry-"), 3, r.stdout)
            self.assertIn("slots in use", r.stdout)

    def test_an_undeclared_pool_is_refused(self):
        """A pool with no cap bounds nothing, so a typo would silently remove
        the limit rather than apply it."""
        plan = self._sharded(max_running=8, pools={"shared-fs-read": 2})
        plan["units"][0]["pool"] = "typo"
        with self.assertRaises(S.PlanError) as cm:
            S.validate_plan(plan)
        self.assertIn("not declared", str(cm.exception))

    def test_no_limits_means_no_cap(self):
        """The counter-claim: a plan that declares no limits must not suddenly
        stop dispatching."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            plan = self._sharded(n=5)
            for u in plan["units"]:
                u.pop("pool")
            plan.pop("limits")
            (tmp / "plan.json").write_text(json.dumps(plan))
            r = subprocess.run(
                [sys.executable, str(SWARM), "run", str(tmp / "plan.json"),
                 "--dry-run", "--state-dir", str(tmp / "st"),
                 "--root", str(tmp / "rn")],
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(r.stdout.count("submitted dry-"), 5, r.stdout)


class TestTheNewRefusalsDoNotCryWolf(unittest.TestCase):
    """This range added four ways to reject a plan, the largest single
    increase in refusals the validator has had. A gate that blocks honest work
    is weighted as seriously here as a false pass, so these are the plans that
    must still run."""

    LEGITIMATE = {
        "plain unit, no retries, no limits":
            {"units": [{"id": "a", "kind": "slurm", "command": "true",
                        "outputs": ["o"]}]},
        "explicit single attempt":
            {"units": [{"id": "a", "kind": "slurm", "command": "true",
                        "outputs": ["o"], "max_attempts": 1}]},
        "retry_limits declared but unused":
            {"retry_limits": {"read_bytes": 100},
             "units": [{"id": "a", "kind": "slurm", "command": "true",
                        "outputs": ["o"]}]},
        "limits block with no pools used":
            {"limits": {"max_running": 4},
             "units": [{"id": "a", "kind": "slurm", "command": "true",
                        "outputs": ["o"]}]},
        "pool declared but unused":
            {"limits": {"max_running": 4, "pools": {"io": 2}},
             "units": [{"id": "a", "kind": "slurm", "command": "true",
                        "outputs": ["o"]}]},
        "float max_lost":
            {"retry_limits": {"cpu_hours": 10},
             "units": [{"id": "a", "kind": "slurm", "command": "true",
                        "outputs": ["o"], "max_attempts": 2,
                        "retry": {"mode": "restart",
                                  "max_lost": {"cpu_hours": 1.5}}}]},
        "zero max_lost":
            {"retry_limits": {"items": 5},
             "units": [{"id": "a", "kind": "slurm", "command": "true",
                        "outputs": ["o"], "max_attempts": 3,
                        "retry": {"mode": "restart",
                                  "max_lost": {"items": 0}}}]},
        "max_lost exactly at the limit":
            {"retry_limits": {"items": 5},
             "units": [{"id": "a", "kind": "slurm", "command": "true",
                        "outputs": ["o"], "max_attempts": 3,
                        "retry": {"mode": "restart",
                                  "max_lost": {"items": 5}}}]},
    }

    def test_every_legitimate_shape_still_validates(self):
        for name, plan in self.LEGITIMATE.items():
            plan = dict(plan, name="p")
            try:
                S.validate_plan(plan)
            except S.PlanError as e:
                self.fail(f"refused a legitimate plan ({name}): {e}")


class TestConcurrencyDoesNotDeadlock(unittest.TestCase):
    def _run(self, tmp, plan, *argv):
        (tmp / "plan.json").write_text(json.dumps(plan))
        return subprocess.run(
            [sys.executable, str(SWARM), *argv, str(tmp / "plan.json"),
             "--dry-run", "--state-dir", str(tmp / "st"),
             "--root", str(tmp / "rn")],
            capture_output=True, text=True, cwd=tmp)

    PLAN = {"name": "p", "limits": {"max_running": 1}, "units": [
        {"id": "a", "kind": "slurm", "command": "true", "outputs": ["o"],
         "write_scopes": ["a/"]},
        {"id": "b", "kind": "slurm", "command": "true", "outputs": ["o"],
         "write_scopes": ["b/"]}]}

    def test_a_finished_unit_frees_its_slot(self):
        """A cap that never releases is a deadlock, not a limit."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            r = self._run(tmp, self.PLAN, "run")
            self.assertEqual(r.stdout.count("submitted dry-"), 1, r.stdout)
            self.assertIn("slots in use", r.stdout)
            st_path = tmp / "st" / "swarm-state.json"
            st = json.loads(st_path.read_text())
            st["units"]["a"]["state"] = "DONE"
            st_path.write_text(json.dumps(st))
            r = self._run(tmp, self.PLAN, "advance")
            self.assertIn("b: submitted dry-", r.stdout,
                          f"the slot was never released:\n{r.stdout}")


class TestPreemptionUnderTheNewDefault(unittest.TestCase):
    """max_attempts now defaults to 1, so a SINGLE preemption ends a unit.
    That is the intended policy, but the old message said "preempted 1 times,
    giving up", which reads as a bug rather than a decision."""

    def test_the_message_explains_the_policy_and_names_the_way_out(self):
        import ast
        src = SWARM.read_text()
        i = src.index("if rc in RETRYABLE:")
        seg = src[i:src.index("save_state(state_dir, state)", i)]
        self.assertIn("max_attempts defaults to 1", seg)
        self.assertIn("opt-in", seg)
        self.assertNotIn('preempted {policy} times, giving up"\n'
                         '                    )', seg)
        # And the plain repeated-preemption message survives for plans that
        # genuinely asked for several attempts.
        self.assertIn("preempted {policy} times, giving up", seg)


class TestRetryBudgetBehaviour(unittest.TestCase):
    """BEHAVIOURAL, not constant-checking. A reviewer showed my earlier test
    asserted only that DEFAULT_MAX_ATTEMPTS == 1, so reverting the line in
    advance() that actually consumes it left the suite green -- the flagship
    fix's runtime half was covered by nothing."""

    def _drive(self, tmp, plan, verdict, prior_attempts=None):
        """One advance over a unit that already has a REAL attempt, with the
        predicate forced to a given verdict.

        The attempt is constructed directly rather than produced by a dry run:
        a dry placeholder is now cleared at the top of advance (correctly), so
        driving through one would never reach the check at all. My first
        version of this harness did exactly that and tested nothing."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("sw_drive", SWARM)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        (tmp / "plan.json").write_text(json.dumps(plan))
        st = tmp / "st"
        st.mkdir(parents=True, exist_ok=True)
        att = tmp / "rn" / "h" / "attempt-1"
        att.mkdir(parents=True, exist_ok=True)
        attempts = list(prior_attempts or []) + [str(att)]
        (st / "swarm-state.json").write_text(json.dumps(
            {"schema_version": 1, "halted": None, "units": {
                "h": {"state": "SUBMITTED", "job_id": "2868624",
                      "attempt_dir": str(att), "attempts": attempts,
                      "gpu_hours": 0}}}))
        m._check = lambda d: (verdict, "forced")
        # A real advance renews the lease between units, so the harness has to
        # hold it exactly as a controller would. Without this it stopped at
        # "no longer holds the lease" and measured nothing.
        ok, why = m.acquire_lease(str(st))
        self.assertTrue(ok, f"harness could not take the lock: {why}")
        self.addCleanup(m.release_lease, str(st))
        # dry_run=False, because the dry-run refusal (correctly) blocks a dry
        # advance over a live attempt -- my first harness tripped exactly that
        # and measured the refusal instead of the retry. max_new=0 stops it
        # dispatching afterwards, which would need a real sbatch.
        rep, _, _ = m.advance(plan, m.load_state(str(st)), str(st),
                              str(tmp / "rn"), False, max_new=0)
        return rep, m.load_state(str(st))

    def _plan(self, **over):
        u = {"id": "h", "kind": "slurm", "command": "true", "outputs": ["o"],
             "write_scopes": ["h/"]}
        u.update(over)
        return {"name": "p", "units": [u]}

    def test_a_preemption_does_not_retry_by_default(self):
        """THE flagship behaviour: without an explicit max_attempts, one
        preemption ends the unit rather than silently costing three runs."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rep, state = self._drive(tmp, self._plan(), 3)   # 3 = PREEMPTED
            self.assertEqual(state["units"]["h"]["state"], "FAILED",
                             f"it retried without being asked:\n{rep}")
            self.assertTrue(any("opt-in" in l for l in rep), rep)

    def test_a_plan_that_asks_for_retries_gets_them(self):
        """The counter-claim. A default of 1 must not remove retries from
        plans that declared what an interruption costs."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            plan = self._plan(max_attempts=3,
                              retry={"mode": "restart",
                                     "max_lost": {"items": 1}})
            plan["retry_limits"] = {"items": 5}
            rep, state = self._drive(tmp, plan, 3)
            self.assertIsNone(state["units"]["h"]["attempt_dir"],
                              "the attempt was not released for a re-run")
            self.assertNotEqual(state["units"]["h"]["state"], "FAILED")
            self.assertTrue(any("will re-attempt" in l for l in rep), rep)

    def test_a_dry_run_does_not_consume_a_declared_retry(self):
        """Found by a reviewer as an interaction between two changes that
        were each correct alone: a dry attempt is appended to us['attempts'],
        and that list IS the retry budget, so every dry run stole a retry."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            plan = self._plan(max_attempts=2,
                              retry={"mode": "restart",
                                     "max_lost": {"items": 1}})
            plan["retry_limits"] = {"items": 5}
            # One prior DRY attempt on the record, exactly as a permitted
            # dry run leaves behind, plus the real attempt being judged.
            rep, state = self._drive(
                tmp, plan, 3, prior_attempts=[f"{S.DRY_PREFIX}/somewhere"])
            self.assertNotEqual(
                state["units"]["h"]["state"], "FAILED",
                f"a dry run consumed one of the two declared attempts:\n{rep}")
            self.assertTrue(any("will re-attempt (1/2)" in l for l in rep),
                            f"the dry attempt was counted as real:\n{rep}")

    def test_a_terminal_unit_does_not_block_a_dry_run(self):
        """The refusal counted any non-placeholder job id as live, so a DONE
        unit blocked every future dry run for the life of the project."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            plan = self._plan()
            (tmp / "plan.json").write_text(json.dumps(plan))
            sd = tmp / "st"
            sd.mkdir(parents=True)
            (sd / "swarm-state.json").write_text(json.dumps(
                {"schema_version": 1, "halted": None, "units": {
                    "h": {"state": "DONE", "job_id": "2868624",
                          "attempt_dir": "/x", "attempts": ["/x"],
                          "gpu_hours": 0}}}))
            r = subprocess.run(
                [sys.executable, str(SWARM), "run", str(tmp / "plan.json"),
                 "--dry-run", "--state-dir", str(sd), "--root", str(tmp / "rn")],
                capture_output=True, text=True, cwd=tmp)
            self.assertNotIn("REFUSING to dry-run", r.stdout, r.stdout)

    def test_a_LIVE_unit_still_blocks_a_dry_run(self):
        """The property the refusal exists for must survive the narrowing."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "plan.json").write_text(json.dumps(self._plan()))
            sd = tmp / "st"
            sd.mkdir(parents=True)
            (sd / "swarm-state.json").write_text(json.dumps(
                {"schema_version": 1, "halted": None, "units": {
                    "h": {"state": "SUBMITTED", "job_id": "2868624",
                          "attempt_dir": "/x", "attempts": ["/x"],
                          "gpu_hours": 0}}}))
            r = subprocess.run(
                [sys.executable, str(SWARM), "run", str(tmp / "plan.json"),
                 "--dry-run", "--state-dir", str(sd), "--root", str(tmp / "rn")],
                capture_output=True, text=True, cwd=tmp)
            self.assertIn("REFUSING to dry-run", r.stdout, r.stdout)


class TestClosureAuthorityIsFixedByKind(unittest.TestCase):
    """Plan step 9, stage 1. A merged PR is the right evidence for CODE and
    the wrong evidence for a 1.42 TiB hash. If GitHub closes an issue the
    swarm also closes on a receipt, the tracker starts lying about what was
    verified."""

    def _emit(self, kind, state="DONE"):
        import tempfile
        d = tempfile.mkdtemp()
        S.emit_intent(d, "p", "u", state, {"attempt_dir": None},
                      evidence={"receipt": {"state": state}}, kind=kind)
        return S.read_outbox(d)[0]

    def test_a_code_unit_cannot_close_its_own_issue(self):
        """Its receipt establishes that an agent went idle and files exist.
        The accepted form of that work is a merged PR, and nothing here has
        seen one."""
        i = self._emit("code")
        self.assertEqual(i["verb"], "open_pr")
        self.assertIn("not done", i["why"])

    def test_compute_units_still_close_on_a_receipt(self):
        """The counter-claim: the path that worked must keep working."""
        for kind in ("slurm", "pipeline"):
            i = self._emit(kind)
            self.assertEqual(i["verb"], "close", kind)
            self.assertEqual(i["closing_evidence"], "predicate_receipt")

    def test_every_intent_names_its_authority(self):
        """So a drainer never has to infer which evidence would justify it."""
        for kind, want in (("code", "merged_pr"),
                           ("slurm", "predicate_receipt"),
                           ("pipeline", "predicate_receipt")):
            self.assertEqual(self._emit(kind)["closing_evidence"], want)

    def test_the_matrix_has_no_plan_level_override(self):
        """Configurable means configurable wrong, and the failure is silent."""
        import ast
        src = SWARM.read_text()
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "closing_evidence_for")
        code = "\n".join(ast.unparse(x) for x in fn.body)
        for override in ("plan", "u.get", "retry", "limits"):
            self.assertNotIn(override, code,
                             f"closure authority can be overridden via "
                             f"{override!r}")

    def test_an_unknown_kind_defaults_to_the_stricter_evidence(self):
        """A kind nobody has thought about must not inherit the weaker rule."""
        self.assertEqual(S.closing_evidence_for("something-new"),
                         "predicate_receipt")

    def test_the_skill_tells_a_drainer_what_to_do_with_a_violation(self):
        doc = (ROOT / "skills" / "hanig-project" / "SKILL.md").read_text()
        self.assertIn("integrity", doc.lower())
        self.assertIn("Never mark the intent applied", doc)


class TestADefaultChangeIsMadeVisible(unittest.TestCase):
    """max_attempts went from 3 to 1, so a plan that still VALIDATES may
    behave differently than it used to. Validating is not the same as
    behaving identically, and a silent behaviour change discovered by a
    preemption is the worst way to learn about one."""

    def _validate(self, plan):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.json"
            f.write_text(json.dumps(plan))
            return subprocess.run(
                [sys.executable, str(SWARM), "validate", str(f)],
                capture_output=True, text=True).stdout

    def test_units_relying_on_the_default_are_named(self):
        out = self._validate({"name": "p", "units": [
            {"id": "a", "kind": "slurm", "command": "true", "outputs": ["o"]}]})
        self.assertIn("retry policy", out)
        self.assertIn("ONE attempt", out)

    def test_a_plan_that_declares_its_intent_is_not_lectured(self):
        """The counter-claim: output that always appears is output nobody
        reads."""
        out = self._validate({
            "name": "p", "retry_limits": {"items": 9},
            "units": [{"id": "a", "kind": "slurm", "command": "true",
                       "outputs": ["o"], "max_attempts": 3,
                       "retry": {"mode": "restart",
                                 "max_lost": {"items": 1}}}]})
        self.assertNotIn("retry policy", out)

    def test_it_reports_policy_and_does_not_guess_at_intent(self):
        """Saying "you probably wanted retries" would be inferring from a
        proxy, which this code refuses to do everywhere else."""
        import ast
        src = SWARM.read_text()
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef) and n.name == "cmd_validate")
        code = "\n".join(ast.unparse(x) for x in fn.body)
        for proxy in ("preemptible", "gpu_hours", "--time"):
            self.assertNotIn(proxy, code)


class TestACodeUnitIsNotDoneUntilMerged(unittest.TestCase):
    """A reviewer showed my stage-1 fix was COSMETIC: I rewrote the tracker
    intent but left the unit DONE in durable state, so dependents dispatched
    before any merge and the DAG contradicted the tracker."""

    def _advance(self, tmp, plan, verdict):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sw_pr", SWARM)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        st = tmp / "st"
        st.mkdir(parents=True, exist_ok=True)
        att = tmp / "rn" / "c" / "a1"
        att.mkdir(parents=True, exist_ok=True)
        (st / "swarm-state.json").write_text(json.dumps(
            {"schema_version": 1, "halted": None, "units": {
                "c": {"state": "SUBMITTED", "job_id": "agent-1",
                      "attempt_dir": str(att), "attempts": [str(att)],
                      "gpu_hours": 0}}}))
        ok, _ = m.acquire_lease(str(st))
        self.assertTrue(ok)
        self.addCleanup(m.release_lease, str(st))
        m._check = lambda d: (verdict, "forced")
        rep, disp, _ = m.advance(plan, m.load_state(str(st)), str(st),
                                 str(tmp / "rn"), False, max_new=0)
        return rep, disp, m.load_state(str(st)), m

    PLAN = {"name": "p", "units": [
        {"id": "c", "kind": "code", "prompt": "x", "outputs": ["r"],
         "write_scopes": ["c/"]},
        {"id": "after", "kind": "slurm", "command": "true", "outputs": ["o"],
         "needs": ["c"], "write_scopes": ["a/"]}]}

    def test_a_passing_code_predicate_does_not_reach_DONE(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            _, _, state, _ = self._advance(tmp, self.PLAN, 0)   # 0 = DONE
            self.assertEqual(state["units"]["c"]["state"], "READY_FOR_PR")

    def test_a_dependent_does_not_dispatch_before_a_merge(self):
        """The consequence that made the cosmetic fix dangerous."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            _, dispatched, state, _ = self._advance(tmp, self.PLAN, 0)
            self.assertEqual(dispatched, 0)
            self.assertNotEqual(state["units"].get("after", {}).get("state"),
                                "SUBMITTED")

    def test_the_intent_is_open_pr_not_close(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            _, _, _, m = self._advance(tmp, self.PLAN, 0)
            verbs = [i["verb"] for i in m.read_outbox(str(tmp / "st"))]
            self.assertIn("open_pr", verbs)
            self.assertNotIn("close", verbs)

    def test_a_compute_unit_still_reaches_DONE(self):
        """The counter-claim: the path that works must keep working."""
        import tempfile
        plan = json.loads(json.dumps(self.PLAN))
        plan["units"][0]["kind"] = "slurm"
        plan["units"][0]["command"] = "true"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            _, _, state, _ = self._advance(tmp, plan, 0)
            self.assertEqual(state["units"]["c"]["state"], "DONE")


class TestApprovalDoesNotCarryOntoChangedWork(unittest.TestCase):
    """An approval covers the work the human SAW. Carrying it forward
    unconditionally let a changed plan be filed on a yes given for something
    else, including a one-run autopilot."""

    def test_an_unchanged_redraft_keeps_the_approval(self):
        sys.path.insert(0, str((ROOT / "skills" / "hanig-project" / "scripts")))
        import tickets as T2
        plan = {"name": "p", "units": [
            {"id": "a", "kind": "slurm", "command": "true", "outputs": ["o"]}]}
        first = T2.draft(plan)
        first["approval"] = {"state": "granted", "granted_by": "hani",
                             "at": "x"}
        self.assertEqual(T2.draft(plan, existing=first)["approval"]["state"],
                         "granted")

    def test_a_changed_command_re_arms_the_gate(self):
        sys.path.insert(0, str((ROOT / "skills" / "hanig-project" / "scripts")))
        import tickets as T2
        plan = {"name": "p", "units": [
            {"id": "a", "kind": "slurm", "command": "true", "outputs": ["o"]}]}
        first = T2.draft(plan)
        first["approval"] = {"state": "granted", "granted_by": "hani",
                             "at": "x"}
        changed = json.loads(json.dumps(plan))
        changed["units"][0]["command"] = "rm -rf /"
        got = T2.draft(changed, existing=first)["approval"]
        self.assertEqual(got["state"], "required")
        self.assertIn("changed since approval", got["how_to_skip_next_time"])

    def test_an_added_unit_re_arms_the_gate(self):
        sys.path.insert(0, str((ROOT / "skills" / "hanig-project" / "scripts")))
        import tickets as T2
        plan = {"name": "p", "units": [
            {"id": "a", "kind": "slurm", "command": "true", "outputs": ["o"]}]}
        first = T2.draft(plan)
        first["approval"] = {"state": "autopilot", "granted_by": "phrase",
                             "at": None}
        grown = json.loads(json.dumps(plan))
        grown["units"].append({"id": "b", "kind": "slurm", "command": "true",
                               "outputs": ["p"]})
        self.assertEqual(T2.draft(grown, existing=first)["approval"]["state"],
                         "required")


class TestBothPartitionSpellings(unittest.TestCase):
    def test_a_separate_argument_partition_is_recognised(self):
        """Reading only `--partition=cpu` reported a unit that plainly
        declares one as declaring none, making the validator's own honesty
        message a false statement."""
        self.assertEqual(
            S.declared_partition({"sbatch": ["--partition", "cpu", "--time",
                                             "5"]}), "cpu")
        self.assertEqual(
            S.declared_partition({"sbatch": ["--partition=cpu"]}), "cpu")
        self.assertEqual(S.declared_partition({"sbatch": ["-p", "gpu"]}), "gpu")
        self.assertIsNone(S.declared_partition({"sbatch": ["--time", "5"]}))

    def test_both_spellings_are_checked_against_the_cluster(self):
        for sbatch in (["--partition=nope"], ["--partition", "nope"],
                       ["-p", "nope"]):
            self.assertEqual(
                S.partition_problems([{"id": "a", "sbatch": sbatch}],
                                     known={"cpu"}), [("a", "nope")], sbatch)


class TestAStalledDAGIsDiagnosable(unittest.TestCase):
    """I claimed to the reviewers that a READY_FOR_PR unit is diagnosable from
    `status` alone. It was not: the dependent showed a bare "-" and the code
    unit gave no hint that nothing can move it. A DAG that stalls
    undiagnosably is worse than one that fails."""

    PLAN = {"name": "p", "units": [
        {"id": "writer", "kind": "code", "prompt": "x", "outputs": ["r.txt"],
         "write_scopes": ["w/"]},
        {"id": "after", "kind": "slurm", "command": "true", "outputs": ["o"],
         "needs": ["writer"], "write_scopes": ["a/"]}]}

    def _status(self, tmp, extra=None):
        (tmp / "plan.json").write_text(json.dumps(self.PLAN))
        st = tmp / "st"
        st.mkdir(parents=True, exist_ok=True)
        units = {"writer": {"state": "READY_FOR_PR", "job_id": "agent-1",
                            "attempt_dir": "/x", "attempts": ["/x"],
                            "gpu_hours": 0}}
        units.update(extra or {})
        (st / "swarm-state.json").write_text(json.dumps(
            {"schema_version": 1, "halted": None, "units": units}))
        return subprocess.run(
            [sys.executable, str(SWARM), "status", str(tmp / "plan.json"),
             "--state-dir", str(st)], capture_output=True, text=True)

    def test_it_says_why_the_unit_cannot_advance(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = self._status(Path(d)).stdout
            self.assertIn("MERGED PULL REQUEST", out)
            self.assertIn("Nothing records merges yet", out,
                          "a human must not need to know that stage 3 is "
                          "unbuilt to understand why nothing is happening")

    def test_it_names_what_a_waiting_unit_is_waiting_on(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = self._status(Path(d)).stdout
            self.assertIn("waiting on writer", out)

    def test_a_stalled_dag_asks_for_a_person(self):
        """Exit 0 would let a cron wrapper poll a permanently stalled project
        and report it healthy forever."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            r = self._status(Path(d))
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("NEEDS YOU: writer", r.stdout)

    def test_a_healthy_dag_still_exits_zero(self):
        """The counter-claim: a monitor that always says look at me is one
        nobody looks at."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            r = self._status(tmp, extra={
                "writer": {"state": "DONE", "job_id": "a",
                           "attempt_dir": "/x", "attempts": ["/x"],
                           "gpu_hours": 0},
                "after": {"state": "RUNNING", "job_id": "b",
                          "attempt_dir": "/y", "attempts": ["/y"],
                          "gpu_hours": 0}})
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertNotIn("NEEDS YOU", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
