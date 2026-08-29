#!/usr/bin/env python3
"""swarm.py: the coordinator: dispatch, bound, detach, advance the DAG.

Step 2 of docs/plan-swarm.md. This is the CENTRE OF GRAVITY of the system, per
the committee's drift guard: "the center of gravity is the coordinator and the
human interface, not the predicate."

WHAT IT DOES. Reads a plan of polymorphic units (slurm | pipeline | code) with
dependencies. Validates that the DAG is acyclic and that no two units can write
the same place. Then, for each unit whose dependencies are DONE: allocates an
exclusive write root via unit.py, submits, records the binding, and DETACHES.

WHY IT DETACHES. A coordinator that babysits is a coordinator that dies with its
terminal. Cluster jobs run for hours or days; an ssh drop must not lose the DAG.
So `run` dispatches and exits, and `advance` -- idempotent, safe to run from a
Paseo schedule or cron -- reads durable state, re-checks units, and moves the
DAG forward. Every state transition is on disk before it is acted on.

WHAT IT REFUSES TO DO. It does not judge units; `unit.py check` does, and its
exit code is the only input. It does not re-run a completed unit. It does not
reuse a write root: a retry mints a NEW attempt, because reuse is exactly what
makes a predicate inconclusive.

Adapted from Shreshth's `start-a-sprint`, whose plan validator already enforced
the two invariants that matter here: acyclic dependencies, and disjoint write
scopes between concurrent workers. His scope-overlap logic is the direct
ancestor of `_scopes_overlap` below.

Python 3.8+, stdlib only, login-node safe.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import unit as U  # noqa: E402  same skill, installed together

STATE_FILE = "swarm-state.json"
KINDS = U.KINDS

# unit.py's exit codes are the ONLY judgement this coordinator consumes.
DONE, RUNNING, FAILED, PREEMPTED, INCOMPLETE, NEEDS_HUMAN = 0, 1, 2, 3, 4, 5
NAME = {0: "DONE", 1: "RUNNING", 2: "FAILED", 3: "PREEMPTED",
        4: "INCOMPLETE", 5: "NEEDS_HUMAN"}

EXIT_OK = 0
EXIT_HALTED = 1          # budget or runaway stopped new dispatch
EXIT_FAILED_UNIT = 2     # at least one unit is terminally failed
EXIT_USAGE = 64


class PlanError(Exception):
    pass


# --- plan validation ------------------------------------------------------
def _norm_scope(scope):
    """A write scope as a comparable path prefix. From start-a-sprint."""
    s = str(scope).strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/") + "/" if s and not s.endswith("/") else s


def _scopes_overlap(a, b):
    """True when either scope contains the other. Two units that can write the
    same place cannot both have an exclusive write root, which is the property
    the whole done-predicate rests on."""
    na, nb = _norm_scope(a), _norm_scope(b)
    if not na or not nb:
        return True                      # an empty scope is everything
    return na == nb or na.startswith(nb) or nb.startswith(na)


def validate_plan(plan):
    """Raise PlanError, or return a summary. Refuses BEFORE anything is
    dispatched: a plan that cannot be run should not half-run."""
    if not isinstance(plan, dict):
        raise PlanError("the plan is not a JSON object")
    units = plan.get("units")
    if not isinstance(units, list) or not units:
        raise PlanError("the plan declares no units; add a 'units' list")

    # A `code` unit needs paseo ON THIS HOST, because that is where the
    # coordinator dispatches it. Refusing here, before anything is dispatched,
    # beats failing at `paseo run` with half a DAG already live.
    #
    # This is normally a signal that the unit is on the wrong machine rather
    # than that paseo is missing: a code unit runs a coding agent as a local
    # process, and a cluster login node is not where that belongs.
    if any(isinstance(u, dict) and u.get("kind") == "code" for u in units):
        if not shutil.which("paseo"):
            ids = ", ".join(sorted(u.get("id", "?") for u in units
                                   if isinstance(u, dict)
                                   and u.get("kind") == "code"))
            raise PlanError(
                f"unit(s) {ids} are kind=code, which runs a coding agent "
                f"through paseo, and paseo is not on PATH on "
                f"{os.uname().nodename}. Either run this plan from a machine "
                f"where you run agents, or declare the work as kind=pipeline "
                f"or kind=slurm and invoke the tool directly. Installing "
                f"paseo on a shared login node is usually the wrong answer: "
                f"it would run the agent processes there.")

    seen, by_id = set(), {}
    for i, u in enumerate(units):
        if not isinstance(u, dict):
            raise PlanError(f"unit {i} is not an object")
        uid = u.get("id")
        if not isinstance(uid, str) or not uid.strip():
            raise PlanError(f"unit {i} has no 'id'; every unit needs a stable id")
        if uid in seen:
            raise PlanError(f"duplicate unit id {uid!r}; ids must be unique "
                            f"because state is keyed on them")
        seen.add(uid)
        by_id[uid] = u
        if u.get("kind") not in KINDS:
            raise PlanError(f"unit {uid!r} has kind {u.get('kind')!r}; use one "
                            f"of {', '.join(KINDS)}")
        if not u.get("outputs"):
            raise PlanError(f"unit {uid!r} declares no outputs, so it can never "
                            f"be judged done. Add 'outputs'.")
        if u["kind"] != "code" and not u.get("command"):
            raise PlanError(f"unit {uid!r} is kind {u['kind']} with no "
                            f"'command' to submit")

    # Dependencies must exist and must not cycle. Cycle detection lifted in
    # shape from start-a-sprint's validator.
    for uid, u in by_id.items():
        for dep in (u.get("needs") or []):
            if dep not in by_id:
                raise PlanError(f"unit {uid!r} needs {dep!r}, which is not in "
                                f"the plan")
            if dep == uid:
                raise PlanError(f"unit {uid!r} depends on itself")
    state = {}

    def visit(uid, path):
        if state.get(uid) == "done":
            return
        if state.get(uid) == "open":
            cut = path.index(uid)
            raise PlanError("unit dependency cycle: "
                            + " -> ".join([*path[cut:], uid]))
        state[uid] = "open"
        for dep in (by_id[uid].get("needs") or []):
            visit(dep, [*path, uid])
        state[uid] = "done"

    for uid in by_id:
        visit(uid, [])

    # No two units may be able to write the same place. Units that are ordered
    # by a dependency are exempt: they cannot run concurrently.
    def ordered(a, b):
        seen_ = set()

        def reaches(x, target):
            if x == target:
                return True
            if x in seen_:
                return False
            seen_.add(x)
            return any(reaches(d, target)
                       for d in (by_id[x].get("needs") or []))
        return reaches(a, b) or reaches(b, a)

    ids = sorted(by_id)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if ordered(a, b):
                continue
            for sa in (by_id[a].get("write_scopes") or []):
                for sb in (by_id[b].get("write_scopes") or []):
                    if _scopes_overlap(sa, sb):
                        raise PlanError(
                            f"units {a!r} and {b!r} can run concurrently and "
                            f"their write scopes overlap ({sa} / {sb}). Two "
                            f"units that can write the same place cannot both "
                            f"have an exclusive write root, which is what makes "
                            f"the done-predicate conclusive. Give them disjoint "
                            f"scopes, or order them with 'needs'.")
    return {"units": len(units),
            "with_deps": sum(1 for u in units if u.get("needs"))}


# --- safety for unattended running ----------------------------------------
# `advance` was called "idempotent and safe to re-enter" before it was audited.
# It was not. Four holes, all of which matter the moment a scheduler runs it
# rather than a human:
#
#   1. Two concurrent advances could both load old state and submit one unit.
#   2. A crash between sbatch and bind left a job running that nothing owned.
#   3. INCOMPLETE could stay live forever, so a lost job never became terminal.
#   4. Nothing detected the plan file changing while units were live.
#
# A human typing `advance` notices all four. A cron job does not.
LEASE = "lease.json"
LEASE_TTL_S = 900
# A breaker is held across a few filesystem calls only, so one
# older than this was abandoned by a controller that died.
BREAKER_STALE_S = 120          # a stale lease must expire, or one crash blocks forever
SETTLE_S = 600             # accounting lag before a missing row becomes terminal


# Fields that cannot change what a dispatch DOES. Everything else is
# digested. Listing the cosmetic fields rather than the meaningful ones is
# deliberate: four reviewers broke the previous inclusion list by naming
# fields it had simply forgotten (inputs, gpu_hours, charge_to, write_scopes,
# max_attempts, timeout_s, prompt, provider). An exclusion list fails in the
# safe direction, because a field added next year is covered until someone
# argues it is cosmetic.
COSMETIC_FIELDS = {"description", "comment", "notes", "title", "owner",
                   "tags", "_comment"}


def plan_digest(plan):
    """Canonical digest of the plan's DISPATCHABLE content.

    Comments, ordering and formatting must not invalidate a live run. Anything
    that reaches `unit.py allocate` or the submitted script must."""
    def unit_payload(u):
        return {k: v for k, v in sorted(u.items())
                if k not in COSMETIC_FIELDS}
    units = sorted((unit_payload(u) for u in (plan.get("units") or [])),
                   key=lambda d: json.dumps(d, sort_keys=True))
    payload = json.dumps(
        {"units": units,
         "budget": plan.get("budget"),
         "root": plan.get("root")}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _lease_body():
    return {"owner": os.environ.get("USER", "?"), "host": os.uname().nodename,
            "pid": os.getpid(), "acquired_at": time.time(),
            "expires_at": time.time() + LEASE_TTL_S}


def acquire_lease(state_dir, force=False):
    """One writer at a time. Returns (ok, holder_description).

    ATOMIC. An earlier version read the lease and then wrote it, which three
    reviewers independently broke: two advances starting together both see no
    lease and both proceed. The fix is the primitive this whole repo rests on
    -- an exclusive create that the OS arbitrates -- rather than a check that
    a scheduler can interleave.

    Taking over a STALE lease is the delicate half: several contenders may
    agree it is stale, and only one may win. They race to create a breaker
    directory instead, because mkdir is likewise atomic."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    path = Path(state_dir) / LEASE
    body = json.dumps(_lease_body(), sort_keys=True)

    def _create_exclusive():
        """Create the lease atomically AND fully populated.

        O_EXCL alone is not enough: it creates a ZERO-LENGTH file, and the
        body is written a moment later. A rival reading in that window sees an
        empty lease, parses nothing, concludes it is stale, and breaks a lease
        that was in fact live -- observed here as two winners out of twelve.
        Writing the body to a private temp file and hard-linking it into place
        closes the window, because link() is atomic and the file it publishes
        is already complete."""
        tmp = path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
        try:
            with open(tmp, "w") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            return f"cannot write the lease: {e}"
        try:
            os.link(str(tmp), str(path))
            return True
        except FileExistsError:
            return False
        except OSError as e:
            return f"cannot publish the lease: {e}"
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    got = _create_exclusive()
    if got is True:
        return True, None
    if isinstance(got, str):
        return False, got

    held, err = U.read_json(path)
    readable = (not err) and isinstance(held, dict)
    held = held if readable else {}
    same_process = (held.get("pid") == os.getpid()
                    and held.get("host") == os.uname().nodename)
    if same_process:
        return True, None                      # re-entrant within one process
    if readable:
        fresh = float(held.get("expires_at") or 0) > time.time()
    else:
        # Unreadable. Judge by the file's own age rather than assuming it is
        # stale, so a corrupt lease still expires instead of wedging the
        # project, but a momentary bad read never breaks a live one.
        try:
            fresh = (time.time() - path.stat().st_mtime) < LEASE_TTL_S
        except OSError:
            fresh = False
    if fresh and not force:
        age = int(time.time() - float(held.get("acquired_at") or 0))
        return False, (f"{held.get('owner')}@{held.get('host')} "
                       f"pid {held.get('pid')}, {age}s ago")

    # Stale, or forced. Exactly one contender may break it.
    breaker = Path(state_dir) / (LEASE + ".break")
    try:
        breaker.mkdir()
    except FileExistsError:
        # A controller SIGKILLed between mkdir and the finally leaves this
        # behind, and every later takeover then fails forever: one crash
        # blocking the project, which is precisely what LEASE_TTL_S exists to
        # prevent. Found by three reviewers, one rating it critical. A breaker
        # is only ever held across a few filesystem calls, so one older than
        # BREAKER_STALE_S was abandoned.
        try:
            age = time.time() - breaker.stat().st_mtime
        except OSError:
            return False, "another controller is taking over the stale lease"
        if age < BREAKER_STALE_S:
            return False, "another controller is taking over the stale lease"
        try:
            breaker.rmdir()
            breaker.mkdir()
        except OSError:
            return False, ("a takeover breaker is abandoned but could not be "
                           f"cleared: remove {breaker} by hand")
    except OSError as e:
        return False, f"cannot arbitrate the stale lease: {e}"
    try:
        # RE-READ inside the critical section. The staleness decision above was
        # made before we held the breaker, and a rival may have installed a
        # fresh lease and released the breaker in between. Acting on the older
        # read let a second controller unlink a live lease and take over: the
        # same read-then-act shape as the original defect, one level down.
        # Found by running the race repeatedly rather than once.
        again, aerr = U.read_json(path)
        if not aerr and isinstance(again, dict):
            still_stale = float(again.get("expires_at") or 0) <= time.time()
            ours = (again.get("pid") == os.getpid()
                    and again.get("host") == os.uname().nodename)
            if ours:
                return True, None
            if not still_stale and not force:
                age = int(time.time() - float(again.get("acquired_at") or 0))
                return False, (f"{again.get('owner')}@{again.get('host')} "
                               f"pid {again.get('pid')}, {age}s ago "
                               f"(took over while we waited)")
        try:
            path.unlink()
        except OSError:
            pass
        got = _create_exclusive()
        if got is not True:
            return False, (got if isinstance(got, str)
                           else "lost the race for the stale lease")
        return True, None
    finally:
        try:
            breaker.rmdir()
        except OSError:
            pass


def renew_lease(state_dir):
    """Push the expiry out. Returns False if we no longer hold it.

    Publishes by the same hard-link route as acquisition, so renewal cannot
    overwrite a successor's lease: an earlier version read, checked ownership,
    then wrote, and three reviewers pointed out that a takeover completing in
    that window let the deposed controller clobber the new holder. Here the
    old lease is removed and the new one linked into place only while we still
    own it, and the link fails outright if someone else published first."""
    path = Path(state_dir) / LEASE
    held, err = U.read_json(path)
    if err or not isinstance(held, dict):
        return False
    if held.get("pid") != os.getpid() or held.get("host") != os.uname().nodename:
        return False
    body = json.dumps({**held, "expires_at": time.time() + LEASE_TTL_S},
                      sort_keys=True)
    tmp = path.with_suffix(f".renew.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(tmp, "w") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        # Re-check ownership as late as possible, then swap.
        again, aerr = U.read_json(path)
        if (aerr or not isinstance(again, dict)
                or again.get("pid") != os.getpid()
                or again.get("host") != os.uname().nodename):
            return False
        os.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def release_lease(state_dir):
    """Only ever release OUR OWN lease. Blindly unlinking would let a
    controller that had already been taken over delete its successor's lease
    on the way out, leaving the project unprotected."""
    path = Path(state_dir) / LEASE
    held, err = U.read_json(path)
    if err or not isinstance(held, dict):
        return
    if held.get("pid") != os.getpid() or held.get("host") != os.uname().nodename:
        return
    try:
        path.unlink()
    except OSError:
        pass


# --- durable state --------------------------------------------------------
def load_state(state_dir):
    """Every transition is on disk BEFORE it is acted on, so a coordinator
    killed mid-dispatch resumes rather than double-submits."""
    obj, err = U.read_json(Path(state_dir) / STATE_FILE)
    if err == "missing":
        return {"schema_version": 1, "units": {}, "halted": None}
    if err:
        sys.exit(f"error: state at {Path(state_dir) / STATE_FILE} is unreadable "
                 f"({err}). Fix or remove it; removing it will re-dispatch "
                 f"units whose attempts are not recorded elsewhere.")
    return obj


def save_state(state_dir, state):
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    err = U.write_json(Path(state_dir) / STATE_FILE, state)
    if err:
        sys.exit(f"error: cannot persist coordinator state: {err}. Refusing to "
                 f"continue: acting on state that is not on disk is how a "
                 f"crash becomes a double submission.")


def _unit_state(state, uid):
    return state["units"].setdefault(
        uid, {"state": None, "attempt_dir": None, "attempts": [],
              "gpu_hours": 0.0})


# --- dispatch -------------------------------------------------------------
def _allocate(plan, u, root):
    argv = [sys.executable, str(_HERE / "unit.py"), "allocate",
            "--root", str(root), "--task", u["id"], "--kind", u["kind"]]
    if u.get("command"):
        argv += ["--command", u["command"]]
    for o in (u.get("outputs") or []):
        argv += ["--output", o]
    for i in (u.get("inputs") or []):
        argv += ["--input", i]
    if u.get("gpu_hours") is not None:
        argv += ["--gpu-hours", str(u["gpu_hours"])]
    if plan.get("charge_to"):
        argv += ["--charge-to", plan["charge_to"]]
    rc, out, err = U.run(argv, timeout=120)
    if rc != 0:
        return None, f"allocate failed: {(err or out).strip()[:200]}"
    return out.strip().splitlines()[-1], None


def _submit(u, unit_dir, dry_run):
    """Submit, and return (job_id, error). Dispatch differs per kind; judging
    does not.

    --dry-run records what WOULD be submitted, so the DAG logic is testable
    without a scheduler. A coordinator that can only be tested on a live
    cluster does not get tested."""
    kind = u["kind"]
    if dry_run:
        return f"dry-{os.urandom(3).hex()}", None
    if kind == "slurm":
        script = Path(unit_dir) / "job.sbatch"
        # The job is NAMED for the attempt. This is what closes the
        # crash-before-bind window: if we die between sbatch and bind, the job
        # is still running and nothing records its id -- but the scheduler
        # knows it by this name, so `reconcile` can find it instead of
        # submitting a second one.
        attempt_id = Path(unit_dir).name
        body = ["#!/bin/bash", f"#SBATCH --job-name=swarm-{attempt_id}",
                "set -euo pipefail", f"cd {unit_dir}", u["command"], ""]
        for extra in (u.get("sbatch") or []):
            body.insert(1, f"#SBATCH {extra}")
        werr = U.write_json(Path(unit_dir) / "submitted.json",
                            {"script": "job.sbatch"})
        if werr:
            return None, werr
        try:
            script.write_text("\n".join(body))
        except OSError as e:
            return None, f"cannot write {script}: {e}"
        rc, out, err = U.run(["sbatch", "--parsable", str(script)],
                             cwd=str(unit_dir), timeout=120)
        if rc != 0:
            return None, f"sbatch refused it: {(err or out).strip()[:200]}"
        return out.split(";")[0].strip(), None
    if kind == "pipeline":
        # The engine owns its interior. Give it a fresh work dir inside the
        # exclusive root, then stay out of the way.
        #
        # LAUNCHED DETACHED, never run to completion here. An earlier version
        # ran it synchronously under a 120s default, so an honest `nextflow
        # run` was SIGKILLed by the coordinator at two minutes and recorded
        # FAILED. Raising the timeout only moves the damage: advance would then
        # block for hours, holding the lease and checking nothing else. The
        # coordinator dispatches and detaches, exactly as it does for sbatch,
        # and unit.py judges the result later from the artifacts.
        log = Path(unit_dir) / "engine.log"
        try:
            fh = open(log, "ab")
        except OSError as e:
            return None, f"cannot open {log}: {e}"
        try:
            # The wrapper records the exit status, because nothing else
            # will: a detached child is reparented to init and its code can
            # never be reaped. Written by OUR wrapper into the exclusive root,
            # not by the engine, and the receipt says exactly that.
            # The command runs in a SUBSHELL. An `exit N` inside it is very
            # common in pipeline wrappers, and it terminated the outer shell
            # before the status line ran: engine.rc was never written and the
            # check then reported "killed, or the node rebooted", which was
            # false. The subshell contains the exit so the status is always
            # recorded. Newlines, not semicolons, so a trailing comment in the
            # command cannot swallow the closing paren.
            # `wait` before recording the status: a command ending in
            # `... &` returns 0 immediately while a child keeps writing, so
            # the unit could reach DONE and the declared output change
            # afterwards. Waiting makes the recorded status cover the whole
            # job the command started.
            wrapped = (f'(\n{u["command"]}\nrc=$?\nwait\nexit $rc\n)\n'
                       f'printf %s "$?" > {U.ENGINE_RC}\n')
            proc = subprocess.Popen(
                ["sh", "-c", wrapped], cwd=str(unit_dir),
                stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True)
        except OSError as e:
            fh.close()
            return None, f"engine launch failed: {e}"
        fh.close()
        werr = U.write_json(Path(unit_dir) / "engine.json",
                            {"pid": proc.pid, "command": u["command"],
                             "host": os.uname().nodename,
                             "launched_at": time.time(),
                             "log": "engine.log"})
        if werr:
            return None, f"launched pid {proc.pid} but {werr}"
        return f"engine-{proc.pid}", None
    if kind == "code":
        # Paseo owns the agent lifecycle; we own the write root.
        #
        # --cwd is REQUIRED, not optional. Without it the agent runs in the
        # coordinator's directory, so it writes nowhere near its exclusive
        # root and the declared outputs can never be found there: the whole
        # isolation premise, silently void for this one kind.
        argv = ["paseo", "run", "--background", "--json",
                "--cwd", str(unit_dir),
                "--provider", u.get("provider", "claude"),
                # The title carries the ATTEMPT id, mirroring the Slurm job
                # name, so an agent created just before a crash can be found
                # again and is never confused with a later attempt of the same
                # unit.
                "--title", f"[swarm] {u['id']} {Path(unit_dir).name}"]
        # An agent under default permissions stops at the first Write and
        # waits for a person, which is correct behaviour and fatal to an
        # unattended DAG. The plan must therefore say what it wants, and say
        # it EXPLICITLY: a coordinator that silently bypassed permissions on
        # the user's behalf would be a worse bug than a stalled unit.
        if u.get("mode"):
            argv += ["--mode", str(u["mode"])]
        if u.get("model"):
            argv += ["--model", str(u["model"])]
        for kv in (u.get("env") or []):
            argv += ["--env", str(kv)]
        argv.append(u.get("prompt") or u.get("command") or u["id"])
        rc, out, err = U.run(argv, timeout=180)
        if rc != 0:
            return None, f"paseo run failed: {_paseo_error(out, err)}"
        # Read the id from JSON. Scanning output tokens for "something long
        # with a dash in it" would happily return a branch name or a path.
        # paseo names this field `agentId`. Reading `id` found nothing and
        # left a live agent running, unbound and unjudgeable: the orphan class
        # the Slurm path has a reconcile net for.
        rec = _paseo_json(out) or {}
        agent = (rec.get("agentId") or rec.get("AgentId")
                 or rec.get("id") or rec.get("Id"))
        if not agent:
            return None, (f"paseo run returned no agent id: "
                          f"{_paseo_error(out, err)}")
        return str(agent), None
    return None, f"unknown kind {kind!r}"


def _bind(unit_dir, job_id):
    rc, out, err = U.run([sys.executable, str(_HERE / "unit.py"), "bind",
                          str(unit_dir), "--job-id", str(job_id)], timeout=120)
    return None if rc == 0 else f"bind failed: {(err or out).strip()[:200]}"


def _check(unit_dir):
    rc, out, err = U.run([sys.executable, str(_HERE / "unit.py"), "check",
                          str(unit_dir)], timeout=300)
    return rc, (out or "") + (err or "")


# --- tracker outbox -------------------------------------------------------
# The coordinator runs on a cluster LOGIN NODE. An MCP connector lives in the
# Claude client on a laptop, so `swarm.py` cannot call Linear or Asana, and it
# has no network code at all -- deliberately. Putting a tracker API token on a
# shared login node would be the alternative, and it is worse.
#
# So the coordinator writes INTENTS and nothing else. Something that can reach
# the tracker drains them later. Three properties fall out of that separation,
# and each is a real defect avoided:
#
#   - a tracker outage NEVER alters swarm state; the swarm is authoritative and
#     the tracker is a view of it
#   - every intent carries an idempotency key, so a re-run of the drain cannot
#     create a second issue for one unit
#   - a CLOSE intent is emitted only from a predicate verdict, never from a
#     unit's own report. An agent saying "done" on a ticket is exactly the
#     self-assertion this whole family refuses.
OUTBOX = "outbox.jsonl"

# Unit states that justify a tracker mutation, and what each means to a reader.
TRACKER_EVENTS = {
    "SUBMITTED": ("start", "work started"),
    "DONE": ("close", "the unit's predicate returned DONE"),
    "FAILED": ("reopen", "the command failed"),
    "FAILED_EVIDENCE": ("reopen", "no verdict arrived; evidence never landed"),
    "PREEMPTED": ("note", "preempted; a new attempt will be minted"),
    "HELD": ("block", "an upstream unit will not complete"),
    "NEEDS_HUMAN": ("block", "blocked on a person, not on compute"),
}


def outbox_key(project, uid, state, attempt_dir):
    """Idempotency key. Same project, unit, state and attempt yields the same
    key, so draining twice is a no-op rather than a duplicate issue."""
    basis = f"{project}\x00{uid}\x00{state}\x00{attempt_dir or ''}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def emit_intent(state_dir, project, uid, unit_state, us, evidence=None):
    """Append one tracker intent. Returns the key, or None if already emitted.

    Deterministic from state: replaying the same transitions produces the same
    keys, which is what makes the drain safe to retry."""
    action = TRACKER_EVENTS.get(unit_state)
    if not action:
        return None
    verb, why = action
    if verb == "close" and not evidence:
        # Three reviewers found this: the caller built evidence as
        # `{"receipt": rp} if rp else None`, so an NFS blip on the read
        # microseconds after the verdict produced a close intent with
        # evidence null. A drain following the rule must then refuse it and
        # the issue never closes; a lax drain closes on nothing. Refusing HERE
        # keeps the rule in the one place that cannot be forgotten, and the
        # next advance re-reads the receipt and emits it properly.
        print(f"WARNING: not recording a close intent for {uid}: the "
              f"predicate's receipt could not be read, and nothing closes on "
              f"a self-report. It will be retried on the next advance.",
              file=sys.stderr)
        return None
    key = outbox_key(project, uid, unit_state, us.get("attempt_dir"))
    path = Path(state_dir) / OUTBOX
    try:
        if path.is_file():
            for line in path.read_text().splitlines():
                if line.strip() and json.loads(line).get("key") == key:
                    return None          # already emitted; do not duplicate
    except (OSError, ValueError):
        pass
    intent = {
        "key": key, "project": project, "unit": uid, "verb": verb,
        "unit_state": unit_state, "why": why,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "job_id": us.get("job_id"), "attempt_dir": us.get("attempt_dir"),
        # A close intent MUST carry the verdict that justifies it. A drain that
        # cannot see the evidence must refuse to close.
        "evidence": evidence,
        "applied": False,
    }
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(intent, sort_keys=True) + "\n")
    except OSError as e:
        print(f"WARNING: could not append a tracker intent: {e}",
              file=sys.stderr)
        return None
    return key


def read_outbox(state_dir):
    out = []
    p = Path(state_dir) / OUTBOX
    if not p.is_file():
        return out
    try:
        for line in p.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


# --- the DAG --------------------------------------------------------------
RETRYABLE = {PREEMPTED}          # a preemption is not a failure
TERMINAL_BAD = {FAILED}


def _paseo_json(out):
    """paseo prints human notices ("Created workspace ...", "Tip: ...") to
    stdout BEFORE its JSON, so json.loads on the whole stream fails. Take the
    first balanced object instead."""
    if not out:
        return None
    i = out.find("{")
    if i < 0:
        return None
    depth, instr, esc = 0, False, False
    for j, ch in enumerate(out[i:], i):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(out[i:j + 1])
                except ValueError:
                    return None
    return None


def _paseo_error(out, err):
    """paseo's own diagnostics are good and specific: an invalid mode comes
    back naming every mode the provider accepts. Truncating the combined
    stream at 200 chars threw that away and left the workspace notice, which
    tells the operator nothing."""
    rec = _paseo_json(out) or _paseo_json(err)
    if isinstance(rec, dict) and isinstance(rec.get("error"), dict):
        msg = rec["error"].get("message") or rec["error"].get("code")
        if msg:
            return str(msg)[:400]
    text = "\n".join(l for l in ((err or "") + "\n" + (out or "")).splitlines()
                      if l.strip() and not l.startswith(("Created workspace",
                                                         "Tip:")))
    return text.strip()[:400] or "no diagnostic"


def reconcile_orphan(unit_dir, allocated_at=None, kind=None):
    """Did a job for this attempt reach the scheduler even though we never
    recorded its id? Returns (job_id, note).

    Asks the SCHEDULER, which is the only party that knows. Blindly
    resubmitting after a crash is how one unit becomes two jobs writing the
    same directory -- the exact thing the exclusive write root is for."""
    attempt_id = Path(unit_dir).name
    if kind == "code":
        # Same question, different registry: paseo knows whether an agent was
        # created for this attempt.
        rc, out, _ = U.run(["paseo", "ls", "--json"], timeout=60)
        if rc != 0:
            return None, "UNKNOWN"
        try:
            for a in json.loads(out or "[]"):
                if attempt_id in str(a.get("name") or ""):
                    aid = a.get("id") or a.get("agentId")
                    if aid:
                        return str(aid), (
                            f"recovered agent {aid} for attempt {attempt_id}: "
                            f"it had been created but was never bound. Not "
                            f"re-run.")
        except (ValueError, AttributeError):
            return None, "UNKNOWN"
        return None, None
    name = f"swarm-{attempt_id}"
    # `sacct` WITHOUT -S defaults to jobs that started today. A crash at 23:50
    # whose job finished at 23:55 is invisible to a 00:10 reconcile, and the
    # unit decays to FAILED_EVIDENCE though it succeeded. Anchor the window to
    # when the attempt was allocated, an hour early for clock skew.
    since = (allocated_at - 3600) if allocated_at else (time.time() - 7 * 86400)
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since))
    asked = 0
    for argv in (["squeue", "-h", "-n", name, "-o", "%i"],
                 ["sacct", "-n", "-P", "-X", "--name", name,
                  "-S", start, "-o", "JobID"]):
        rc, out, _ = U.run(argv, timeout=60)
        if rc != 0:
            continue                       # the tool failed; it proved nothing
        asked += 1
        if (out or "").strip():
            job = out.strip().splitlines()[0].split("|")[0].strip()
            if job:
                return job, (f"recovered job {job} for attempt {attempt_id}: it "
                             f"had reached the scheduler but was never bound. "
                             f"Not resubmitted.")
    # "Both tools answered and neither knows it" is proof of absence.
    # "squeue was down" is not, and treating the two alike would release a
    # live attempt and dispatch a second job for one unit.
    return None, (None if asked == 2 else "UNKNOWN")


def advance(plan, state, state_dir, root, dry_run, max_new=None,
            accept_plan_change=False):
    """Idempotent. Re-check every live unit, then dispatch whatever is ready.

    Safe to run from a Paseo schedule or cron every few minutes: it never
    re-submits a unit that has an attempt recorded, and it persists before it
    acts."""
    units = {u["id"]: u for u in plan["units"]}
    report, dispatched, halted = [], 0, state.get("halted")

    # The plan must not change under a live run. A mid-flight edit silently
    # redefines what the recorded attempts were for.
    digest = plan_digest(plan)
    if state.get("plan_digest") is None:
        state["plan_digest"] = digest
    elif state["plan_digest"] != digest and accept_plan_change:
        prior = state["plan_digest"]
        state["plan_digest"] = digest
        state.setdefault("ratified_edits", []).append(
            {"from": prior, "to": digest,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "by": os.environ.get("USER", "?")})
        report.append(f"plan change RATIFIED: {prior[:12]} -> {digest[:12]}. "
                      f"Recorded attempts are kept.")
        save_state(state_dir, state)
    elif state["plan_digest"] != digest:
        # The old remedy here was "start a new state directory", which
        # re-dispatches DONE units and duplicates live jobs: a refusal whose
        # named action is worse than the fault. Raising a budget ceiling is a
        # DESIGNED human intervention, so there must be a way to say yes.
        return ([f"REFUSING to advance: the plan file changed while units are "
                 f"live (digest {state['plan_digest'][:12]} -> {digest[:12]}). "
                 f"A mid-flight edit redefines what the recorded attempts were "
                 f"for.",
                 f"If the edit was intended, ratify it explicitly:",
                 f"    python3 swarm.py advance <plan> --accept-plan-change",
                 f"which records the new digest and keeps every recorded "
                 f"attempt. Units already DONE are not re-dispatched."], 0,
                "plan changed mid-flight")

    # An attempt allocated but never bound may still have reached the
    # scheduler. Ask before dispatching anything else.
    for uid, u in sorted(units.items()):
        us = _unit_state(state, uid)
        if dry_run or not us["attempt_dir"]:
            continue
        if us.get("bind_pending") and us.get("job_id"):
            if not _bind(us["attempt_dir"], us["job_id"]):
                us.pop("bind_pending", None)
                us["state"] = "SUBMITTED"
                report.append(f"{uid}: binding to job {us['job_id']} succeeded "
                              f"on retry; the unit can now be judged.")
                save_state(state_dir, state)
            continue
        if us.get("job_id"):
            continue
        job, note = reconcile_orphan(us["attempt_dir"], us.get("allocated_at"),
                                     kind=u.get("kind"))
        if job:
            us["job_id"] = job
            _bind(us["attempt_dir"], job)
            us["state"] = "SUBMITTED"
            report.append(f"{uid}: {note}")
            save_state(state_dir, state)
        elif note == "UNKNOWN":
            report.append(f"{uid}: allocated at {us['attempt_dir']} with no "
                          f"binding, and the scheduler could not be asked "
                          f"(the query itself failed). NOT releasing the "
                          f"attempt: a failed query is not evidence that "
                          f"nothing is running. Retrying next advance.")
        elif us["state"] == "ALLOCATED":
            # Allocated, then the coordinator died before `sbatch`. The
            # scheduler has never heard of it, so nothing is running and the
            # directory is inert. Releasing the attempt lets the unit dispatch
            # into a FRESH write root; keeping it wedged the unit forever,
            # because only a PREEMPTED verdict cleared attempt_dir.
            report.append(f"{uid}: allocated at {us['attempt_dir']} but never "
                          f"reached the scheduler (absent from both squeue and "
                          f"sacct). Releasing the attempt; it will re-dispatch "
                          f"into a new directory. The stale one is left on disk "
                          f"rather than deleted.")
            us["attempt_dir"] = None
            save_state(state_dir, state)

    # 1. Re-check anything with a live attempt. The coordinator does not judge;
    #    unit.py does, and its exit code is the whole input.
    for uid, u in units.items():
        # Each `_check` can take its own timeout, and a synchronous pipeline
        # engine takes far longer. Renewing between units keeps mutual
        # exclusion for as long as we are actually making progress, instead of
        # losing it on a fixed clock while still running.
        if not dry_run and not renew_lease(state_dir):
            # We LOST the lease. Three reviewers found this discarded, which
            # is the worst outcome in the file: a controller deposed mid-run
            # keeps checking, dispatching and writing swarm-state.json
            # alongside its successor, which is the "one unit becomes two
            # jobs" that renewal exists to prevent.
            report.append("STOPPING: this controller no longer holds the "
                          "lease; another has taken it over. Nothing further "
                          "is dispatched from here. Whatever is already "
                          "submitted keeps running and will be judged by "
                          "whoever holds the lease.")
            state["halted"] = "lease lost mid-advance"
            save_state(state_dir, state)
            return report, dispatched, state["halted"]
        us = _unit_state(state, uid)
        if not us["attempt_dir"] or us["state"] == "DONE":
            continue
        rc, text = _check(us["attempt_dir"])
        previous = us.get("state")
        us["state"] = NAME.get(rc, f"rc={rc}")
        report.append(f"{uid}: {us['state']}")
        if us["state"] != previous:
            evidence = None
            if us["state"] == "DONE":
                # The verdict itself, so a drain never closes on a self-report.
                rp, _ = U.read_json(Path(us["attempt_dir"]) / "receipt.json")
                evidence = {"receipt": rp} if rp else None
            emit_intent(state_dir, plan.get("name") or "swarm", uid,
                        us["state"], us, evidence)

        # INCOMPLETE could stay live forever, so a job that vanished was never
        # terminal and the DAG never moved. Once Slurm accounting has had time
        # to settle, an attempt still lacking a verdict IS a failure -- of
        # evidence, which is a different thing from the command failing, so it
        # gets its own name rather than being called FAILED.
        if rc == NEEDS_HUMAN:
            # Nothing is wrong and nothing will progress until a person acts.
            # It must NOT accrue toward the settle window: turning "waiting for
            # you" into FAILED_EVIDENCE after ten minutes would discard a live
            # agent and its context because nobody was at the keyboard.
            us.pop("incomplete_since", None)
        if rc == INCOMPLETE:
            first = us.get("incomplete_since")
            if first is None:
                us["incomplete_since"] = time.time()
            elif time.time() - float(first) > SETTLE_S:
                us["state"] = "FAILED_EVIDENCE"
                report.append(
                    f"{uid}: no verdict {int(time.time() - float(first))}s "
                    f"after the first INCOMPLETE, past the {SETTLE_S}s "
                    f"accounting settle window. Treating as terminal: the "
                    f"evidence never arrived. Check `sacct -j "
                    f"{us.get('job_id')}` by hand.")
        else:
            us.pop("incomplete_since", None)
        if rc in RETRYABLE:
            policy = u.get("max_attempts", 3)
            if len(us["attempts"]) < policy:
                # A retry mints a NEW write root. Reusing one is precisely what
                # makes the predicate inconclusive.
                us["attempt_dir"] = None
                report.append(f"{uid}: preempted, will re-attempt "
                              f"({len(us['attempts'])}/{policy})")
            else:
                us["state"] = "FAILED"
                report.append(f"{uid}: preempted {policy} times, giving up")
    save_state(state_dir, state)

    # 2. Budget. Charged on DISPATCH, not on completion: a budget that only
    #    counts finished work cannot stop a runaway.
    budget = (plan.get("budget") or {}).get("gpu_hours")
    spent = sum(_unit_state(state, uid)["gpu_hours"] for uid in units)

    # 3. Dispatch every unit whose dependencies are DONE.
    for uid, u in sorted(units.items()):
        us = _unit_state(state, uid)
        if us["attempt_dir"] or us["state"] in ("DONE", "FAILED",
                                                "FAILED_EVIDENCE"):
            continue
        # A failed upstream is checked FIRST. Ordered the other way round, the
        # HELD branch was dead code: a FAILED dependency is also not DONE, so
        # `unmet` was non-empty and the loop skipped past HELD every time. The
        # distinction matters -- "waiting" and "will never run" need different
        # actions from whoever reads the status.
        needs = u.get("needs") or []
        failed_upstream = [d for d in needs
                           if _unit_state(state, d)["state"] in
                           ("FAILED", "HELD", "FAILED_EVIDENCE")]
        if failed_upstream:
            us["state"] = "HELD"
            emit_intent(state_dir, plan.get("name") or "swarm", uid, "HELD", us)
            report.append(f"{uid}: held, upstream "
                          f"{', '.join(failed_upstream)} will not complete")
            save_state(state_dir, state)
            continue
        unmet = [d for d in needs
                 if _unit_state(state, d)["state"] != "DONE"]
        if unmet:
            continue
        want = float(u.get("gpu_hours") or 0)
        if budget is not None and spent + want > float(budget):
            halted = (f"budget: {spent + want:.1f} of {budget} GPU-hours would "
                      f"be committed")
            report.append(f"{uid}: SKIPPED -- {halted}")
            continue
        if max_new is not None and dispatched >= max_new:
            report.append(f"{uid}: skipped, --max-new-dispatches reached")
            continue

        unit_dir, err = _allocate(plan, u, root)
        if err:
            us["state"] = "FAILED"
            report.append(f"{uid}: {err}")
            save_state(state_dir, state)
            continue
        # Persist the allocation BEFORE submitting: a crash between the two
        # must leave an orphaned directory, never an unrecorded job.
        us["attempt_dir"] = unit_dir
        us["attempts"].append(unit_dir)
        us["state"] = "ALLOCATED"
        us["allocated_at"] = time.time()
        # ACCUMULATE. Overwriting meant a unit preempted twice was charged
        # once, so retries could walk straight through a ceiling: budget 8,
        # three 4-hour attempts, 12 committed.
        us["gpu_hours"] = float(us.get("gpu_hours") or 0) + want
        spent += want
        save_state(state_dir, state)

        job_id, err = _submit(u, unit_dir, dry_run)
        if err:
            us["state"] = "FAILED"
            report.append(f"{uid}: {err}")
            save_state(state_dir, state)
            continue
        # Bind EVERY kind that has an id. The old guard bound only numeric
        # ids, so a code unit's agent id never reached unit.json and its
        # predicate could never see an agent at all. A dry run still has no
        # real id to bind.
        berr = (_bind(unit_dir, job_id)
                if job_id and not str(job_id).startswith(("dry-", "engine-"))
                else None)
        if berr:
            # The job is REAL and running; only the binding write failed, on an
            # NFS blip say. Recording job_id without this flag was a wedge: the
            # reconcile net skips anything with a job_id, so the unit could
            # never be judged and decayed to FAILED_EVIDENCE while the job
            # succeeded. Retried at the top of the next advance.
            us["bind_pending"] = True
            report.append(f"{uid}: submitted {job_id} but {berr}. The job is "
                          f"running; the binding will be retried next advance.")
        else:
            us.pop("bind_pending", None)
        us["job_id"] = str(job_id)
        us["state"] = "SUBMITTED"
        emit_intent(state_dir, plan.get("name") or "swarm", uid, "SUBMITTED", us)
        dispatched += 1
        report.append(f"{uid}: submitted {job_id} -> {unit_dir}")
        save_state(state_dir, state)

    state["halted"] = halted
    save_state(state_dir, state)
    return report, dispatched, halted


def _load_plan(path):
    plan, err = U.read_json(path)
    if err:
        sys.exit(f"error: no readable plan at {path}: {err}")
    try:
        validate_plan(plan)
    except PlanError as e:
        sys.exit(f"error: invalid plan: {e}")
    except RecursionError:
        sys.exit("error: the dependency graph is too deep to validate; it is "
                 "probably cyclic in a way the checker could not unwind.")
    return plan


def cmd_validate(args):
    plan, err = U.read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    try:
        summary = validate_plan(plan)
    except PlanError as e:
        sys.exit(f"error: invalid plan: {e}")
    print(f"plan is valid: {summary['units']} unit(s), "
          f"{summary['with_deps']} with dependencies")
    return EXIT_OK


def cmd_run(args):
    """Dispatch what is ready, then EXIT. Does not babysit."""
    plan = _load_plan(args.plan)
    # ONE WRITER. Two schedulers firing at once, or a human running `advance`
    # while cron does, would both read old state and submit the same unit.
    ok, holder = acquire_lease(args.state_dir, force=getattr(args, "force", False))
    if not ok:
        print(f"another controller holds this project: {holder}")
        print("  Wait for it, or pass --force if you are certain it is dead. A "
              "stale lease expires on its own after "
              f"{LEASE_TTL_S}s.")
        return EXIT_HALTED
    try:
        state = load_state(args.state_dir)
        report, dispatched, halted = advance(
            plan, state, args.state_dir, args.root, args.dry_run,
            args.max_new_dispatches,
            accept_plan_change=getattr(args, "accept_plan_change", False))
    finally:
        release_lease(args.state_dir)
    for line in report:
        print(f"  {line}")
    print(f"\ndispatched {dispatched} unit(s); coordinator exiting. Advance "
          f"with:\n  swarm.py advance {args.plan} --state-dir "
          f"{args.state_dir} --root {args.root}")
    if halted:
        print(f"HALTED: {halted}")
        return EXIT_HALTED
    return EXIT_OK


def cmd_advance(args):
    return cmd_run(args)


# --- gated promotion ------------------------------------------------------
# Outputs live in the exclusive write root. Getting them into a shared
# canonical tree is a SEPARATE, human-approved, recorded step, because a swarm
# that silently writes a shared path is a swarm that gets switched off.
#
# A unit that declares no `promote_to` never touches a shared path at all.
PROMOTIONS = "promotions.jsonl"


def _redigest(attempt_dir, rel, recorded, accept_weak=False):
    """Re-derive one output's fingerprint NOW and compare with the receipt.

    The receipt is evidence about a moment; promotion happens later. Comparing
    like for like matters: a size-mtime record cannot certify content, so a
    match against one is reported as the weak thing it is."""
    src = Path(attempt_dir) / rel
    try:
        st = src.stat()
    except OSError as e:
        return False, f"{rel}: cannot stat it now ({e})"
    method = str(recorded.get("method") or "")
    # PROMOTION REFUSES WEAK EVIDENCE. Everywhere else a weak fingerprint is
    # reported as weak and allowed through; here it is not, because this is
    # the one place the tool writes to a path other people read. Four
    # reviewers found the old fallback: a size+mtime match cannot see a file
    # edited in place within the same mtime second, and for a DIRECTORY output
    # it cannot see anything at all.
    if "WEAK" in method or not recorded.get("sha256"):
        shown = method or "an unknown fingerprint"
        if not accept_weak:
            return False, (f"{rel}: the receipt holds only {shown}, which "
                           f"cannot establish that the content is unchanged. "
                           f"Promotion will not publish on that basis. "
                           f"Re-check the attempt to mint a stronger receipt, "
                           f"or pass --accept-weak-evidence to publish on "
                           f"size and mtime alone, which is recorded.")
        # Refusing outright would make any output over the digest limit
        # permanently unpromotable, and a 40GB checkpoint is exactly the thing
        # worth publishing. So the refusal names an escape hatch rather than
        # being a dead end, the approver has to type it, and the promotion
        # record says the evidence was weak.
        return True, (f"{rel}: matches on size and mtime only ({shown}). This "
                      f"does NOT establish the content is unchanged; accepted "
                      f"because --accept-weak-evidence was passed.")
    if recorded.get("sha256") and method.startswith("tree-digest"):
        now = U._tree_digest(src)
        if now.get("sha256") != recorded["sha256"]:
            return False, (f"{rel}: the directory tree changed since the "
                           f"verdict ({recorded['sha256'][:12]} -> "
                           f"{now.get('sha256','?')[:12]})")
        return True, f"{rel}: directory tree digest matches"
    if recorded.get("sha256") and method.startswith("content-digest"):
        try:
            digest, truncated = U.sha256_file(src)
        except OSError as e:
            return False, f"{rel}: cannot digest it now ({e})"
        if truncated:
            return False, (f"{rel}: the receipt holds a full content digest "
                           f"but the file now digests as truncated")
        if digest != recorded["sha256"]:
            return False, (f"{rel}: CONTENT CHANGED since the verdict "
                           f"({recorded['sha256'][:12]} -> {digest[:12]})")
        return True, f"{rel}: content digest matches"
    shown = method or "no usable fingerprint"
    return False, (f"{rel}: the receipt records {shown}, which promotion "
                   f"cannot verify against.")


def promote(plan, state, state_dir, uid, approver, approve,
            accept_weak=False):
    """Returns (lines, ok). Refuses loudly; copies only on explicit approval."""
    units = {u["id"]: u for u in plan["units"]}
    u = units.get(uid)
    if not u:
        return [f"no unit {uid!r} in this plan"], False
    dest_root = u.get("promote_to")
    if not dest_root:
        return ([f"unit {uid} declares no 'promote_to', so it has no shared "
                 f"destination and nothing to promote. Its outputs stay in the "
                 f"exclusive write root, which is the safe default."], False)

    us = state.get("units", {}).get(uid) or {}
    if us.get("state") != "DONE":
        return ([f"REFUSING: unit {uid} is {us.get('state') or 'unstarted'}, "
                 f"not DONE. Only a unit whose predicate returned DONE may be "
                 f"promoted; promoting on any weaker basis is the false pass "
                 f"this repo exists to prevent."], False)
    attempt = us.get("attempt_dir")
    receipt, err = (U.read_json(Path(attempt) / "receipt.json") if attempt
                    else (None, "no attempt"))
    if err or not isinstance(receipt, dict):
        return ([f"REFUSING: no readable receipt for unit {uid} ({err}). The "
                 f"receipt is the evidence promotion rests on."], False)
    if receipt.get("state") != "DONE":
        return ([f"REFUSING: the receipt for unit {uid} says "
                 f"{receipt.get('state')!r}, not DONE."], False)

    recorded = receipt.get("outputs") or {}
    if not recorded:
        return ([f"REFUSING: the receipt for unit {uid} records no output "
                 f"fingerprints, so a change since the verdict could not be "
                 f"detected. Re-run `unit.py check` on the attempt to record "
                 f"them, then promote."], False)

    lines, ok = [f"unit {uid}, attempt {Path(attempt).name}", ""], True
    weak = False
    for rel, rec in sorted(recorded.items()):
        good, why = _redigest(attempt, rel, rec, accept_weak)
        lines.append(f"  {'ok ' if good else 'NO '} {why}")
        ok = ok and good
        weak = weak or (good and "does NOT establish" in why)
    if not ok:
        lines += ["", f"REFUSING to promote {uid}: the outputs are not what "
                      f"the receipt describes. Re-run the unit, or re-check "
                      f"the attempt to mint a receipt for what is there now."]
        return lines, False

    # Versioned directory + one pointer swap. Renaming into place is NOT
    # atomic across filesystems, and a shared canonical tree is usually a
    # different mount from the run root, so a half-copied output would appear
    # under the canonical name. Copy into a version, then swap a symlink.
    dest = Path(dest_root) / uid
    version = dest / Path(attempt).name
    current = dest / "current"
    lines += ["", f"  destination : {version}",
              f"  pointer     : {current} -> {Path(attempt).name}"]
    if weak:
        lines.append("  NOTE: at least one output matched only on size and "
                     "mtime, which does not establish unchanged content.")
    if not approve:
        lines += ["", "DRY RUN. Nothing was copied. Approve with:",
                  f"    swarm.py promote <plan> --unit {uid} --approve "
                  f"--approver <name>"]
        return lines, True
    if not approver:
        lines += ["", "REFUSING: --approve requires --approver <name>. The "
                      "record must name who accepted this."]
        return lines, False

    try:
        version.parent.mkdir(parents=True, exist_ok=True)
        if version.exists():
            # ALREADY PUBLISHED. The earlier version of this deleted it and
            # recopied, which destroys data other people may already be
            # reading and leaves `current` pointing at nothing during the gap.
            # A version directory is named by attempt id and its contents were
            # digest-verified when it was written, so re-promoting the same
            # attempt is a no-op. Re-point and stop.
            lines.append(f"  already published at {version}; not rewriting it")
        else:
            staging = dest / f".staging-{Path(attempt).name}-{os.getpid()}"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            for rel in sorted(recorded):
                src, dst = Path(attempt) / rel, staging / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            # Fully populated before it takes the canonical name, so a reader
            # never sees a partial version directory.
            os.replace(staging, version)      # same directory: atomic
        tmp_link = dest / f".current-{os.getpid()}"
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        os.symlink(Path(attempt).name, tmp_link)
        os.replace(tmp_link, current)         # atomic pointer swap
    except OSError as e:
        return lines + ["", f"REFUSING: promotion failed partway ({e}). The "
                            f"canonical pointer was NOT moved, so nothing "
                            f"downstream sees a partial result."], False

    record = {"unit": uid, "attempt": Path(attempt).name,
              "promoted_to": str(version), "approver": approver,
              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "outputs": recorded,
              "digest_basis": ("size-mtime only for at least one output" if weak
                               else "content digest for every output")}
    try:
        with (Path(state_dir) / PROMOTIONS).open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as e:
        lines.append(f"  WARNING: promoted, but the record could not be "
                     f"appended: {e}")
    lines += ["", f"PROMOTED {uid} -> {version}", f"approved by {approver}"]
    return lines, True


def cmd_promote(args):
    plan, err = U.read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    state = load_state(args.state_dir)
    lines, ok = promote(plan, state, args.state_dir, args.unit,
                        args.approver, args.approve,
                        getattr(args, "accept_weak_evidence", False))
    for line in lines:
        print(f"  {line}" if line else "")
    return EXIT_OK if ok else EXIT_FAILED_UNIT


def cmd_outbox(args):
    """Show pending tracker intents. Draining happens elsewhere, on a machine
    that can reach the tracker -- this command exists so a human on the cluster
    can see exactly what WOULD be sent, before anything is."""
    intents = read_outbox(args.state_dir)
    pending = [i for i in intents if not i.get("applied")]
    if args.json:
        print(json.dumps(pending if not args.all else intents, indent=2,
                         sort_keys=True))
        return EXIT_OK
    if not intents:
        print("  no tracker intents recorded")
        print("  Intents appear as units change state. Nothing is ever sent "
              "from here:\n  the coordinator runs on a login node and cannot "
              "reach a tracker.")
        return EXIT_OK
    show = intents if args.all else pending
    print(f"  {len(pending)} pending of {len(intents)} total\n")
    for i in show:
        ev = "with evidence" if i.get("evidence") else "no evidence"
        print(f"  [{'applied' if i.get('applied') else 'PENDING'}] "
              f"{i['verb']:6} {i['unit']:12} {i['unit_state']:16} {ev}")
        print(f"      {i['why']}  key={i['key']}")
    print("\n  Drain these from a machine that can reach the tracker. A close "
          "intent\n  WITHOUT evidence must be refused: nothing closes on a "
          "self-report.")
    return EXIT_OK


def _status_rows(plan, state, state_dir):
    """Everything an operator needs, derived from DURABLE STATE ONLY.

    Reads no scheduler and launches nothing, so it renders correctly with the
    coordinator stopped, which is exactly when someone wants to look."""
    units = {u["id"]: u for u in plan.get("units") or []}
    promoted = {}
    try:
        for line in (Path(state_dir) / PROMOTIONS).read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                promoted[r["unit"]] = r
    except (OSError, ValueError):
        pass
    rows = []
    for uid, u in units.items():
        us = state.get("units", {}).get(uid) or {}
        st = us.get("state") or "-"
        held_by = []
        if st == "HELD":
            held_by = [d for d in (u.get("needs") or [])
                       if (state.get("units", {}).get(d) or {}).get("state")
                       in ("FAILED", "HELD", "FAILED_EVIDENCE")]
        rows.append({
            "id": uid, "kind": u.get("kind", "?"), "state": st,
            "job_id": us.get("job_id"), "attempt_dir": us.get("attempt_dir"),
            "attempts": len(us.get("attempts") or []),
            "gpu_hours": float(us.get("gpu_hours") or 0),
            "needs": u.get("needs") or [],
            "held_by": held_by,
            "promotable": bool(u.get("promote_to")),
            "promoted": promoted.get(uid, {}).get("promoted_to"),
            "promoted_by": promoted.get(uid, {}).get("approver"),
        })
    rows.sort(key=lambda r: r["id"])
    return rows


def status_report(plan, state, state_dir):
    rows = _status_rows(plan, state, state_dir)
    declared = (plan.get("budget") or {}).get("gpu_hours")
    spent = sum(r["gpu_hours"] for r in rows)
    attention = [r for r in rows
                 if r["state"] in ("NEEDS_HUMAN", "FAILED", "FAILED_EVIDENCE")]
    return {
        "project": plan.get("name"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "halted": state.get("halted"),
        "budget": {"declared_gpu_hours": declared, "spent_gpu_hours": spent,
                   "remaining_gpu_hours": (None if declared is None
                                           else float(declared) - spent)},
        "counts": {s: sum(1 for r in rows if r["state"] == s)
                   for s in sorted({r["state"] for r in rows})},
        "needs_attention": [r["id"] for r in attention],
        "units": rows,
    }


def cmd_status(args):
    plan, err = U.read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    state = load_state(args.state_dir)
    rep = status_report(plan, state, args.state_dir)

    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        rows = rep["units"]
        w = max([len(r["id"]) for r in rows] + [4])
        print(f"  {'unit'.ljust(w)}  {'kind':9} {'state':13} {'job':14} "
              f"{'gpuh':>5} att")
        for r in rows:
            job = str(r["job_id"] or "-")
            print(f"  {r['id'].ljust(w)}  {r['kind']:9} {r['state']:13} "
                  f"{job[:14]:14} {r['gpu_hours']:>5g} {r['attempts']}")
            # A held unit must say WHY, or the operator reads it as "waiting".
            if r["held_by"]:
                print(f"  {'':{w}}    held by {', '.join(r['held_by'])}, which "
                      f"will not complete")
            if r["promoted"]:
                print(f"  {'':{w}}    promoted to {r['promoted']} "
                      f"(approved by {r['promoted_by']})")
            elif r["promotable"] and r["state"] == "DONE":
                print(f"  {'':{w}}    NOT promoted; approve with `swarm.py "
                      f"promote <plan> --unit {r['id']} --approve`")
        b = rep["budget"]
        if b["declared_gpu_hours"] is not None:
            print(f"\n  budget: {b['spent_gpu_hours']:g} of "
                  f"{b['declared_gpu_hours']:g} GPU-hours committed, "
                  f"{b['remaining_gpu_hours']:g} left")
        if rep["halted"]:
            # On the page, not only in a log.
            print(f"\n  HALTED: {rep['halted']}")
        if rep["needs_attention"]:
            print(f"\n  NEEDS YOU: {', '.join(rep['needs_attention'])}")

    # Exit code is the notification channel. The coordinator has no network by
    # design, so a cron wrapper reads this and decides whether to wake anyone.
    if rep["needs_attention"]:
        return EXIT_FAILED_UNIT
    return EXIT_HALTED if rep["halted"] else EXIT_OK


def main():
    ap = argparse.ArgumentParser(
        prog="swarm.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("plan")
        p.add_argument("--force", action="store_true",
                       help="take the lease even if another controller holds "
                            "it. Only when you are certain that one is dead.")
        p.add_argument("--state-dir", default=".swarm/state")
        p.add_argument("--root", default=".swarm/runs")
        p.add_argument("--dry-run", action="store_true",
                       help="allocate and record, but do not submit. The DAG "
                            "logic is testable without a scheduler.")
        p.add_argument("--max-new-dispatches", type=int, default=None)
        p.add_argument("--accept-plan-change", action="store_true",
                       help="ratify an intentional mid-flight plan edit. "
                            "Records the new digest and KEEPS every recorded "
                            "attempt, so nothing already DONE is re-dispatched. "
                            "Use when raising a budget ceiling or correcting a "
                            "pending unit; not to paper over an accidental "
                            "edit to a unit that is already running.")

    v = sub.add_parser("validate", help="acyclic deps, disjoint write scopes")
    v.add_argument("plan")
    v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("run", help="dispatch what is ready, then exit")
    common(r)
    r.set_defaults(fn=cmd_run)

    a = sub.add_parser("advance", help="idempotent; for a schedule or cron")
    common(a)
    a.set_defaults(fn=cmd_advance)

    pr = sub.add_parser("promote",
                        help="copy a DONE unit's outputs to its declared "
                             "shared path, on explicit approval")
    pr.add_argument("plan")
    pr.add_argument("--unit", required=True)
    pr.add_argument("--state-dir", default=".swarm/state")
    pr.add_argument("--approve", action="store_true",
                    help="actually copy. Without it this is a dry run.")
    pr.add_argument("--accept-weak-evidence", action="store_true",
                    help="publish an output whose receipt holds only size and "
                         "mtime, which cannot establish unchanged content. "
                         "Needed for outputs too large to digest. Recorded in "
                         "the promotion record as weak.")
    pr.add_argument("--approver", default=None,
                    help="who accepted this result; recorded permanently")
    pr.set_defaults(fn=cmd_promote)

    o = sub.add_parser("outbox", help="tracker intents waiting to be drained")
    o.add_argument("--state-dir", default=".swarm/state")
    o.add_argument("--all", action="store_true", help="include applied ones")
    o.add_argument("--json", action="store_true")
    o.set_defaults(fn=cmd_outbox)

    s = sub.add_parser("status", help="what every unit is doing")
    s.add_argument("--json", action="store_true",
                   help="machine-readable; exit 2 if any unit needs a person")
    s.add_argument("plan")
    s.add_argument("--state-dir", default=".swarm/state")
    s.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
