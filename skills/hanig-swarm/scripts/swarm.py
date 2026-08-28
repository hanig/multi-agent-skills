#!/usr/bin/env python3
"""swarm.py — the coordinator: dispatch, bound, detach, advance the DAG.

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
import json
import os
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
DONE, RUNNING, FAILED, PREEMPTED, INCOMPLETE = 0, 1, 2, 3, 4
NAME = {0: "DONE", 1: "RUNNING", 2: "FAILED", 3: "PREEMPTED", 4: "INCOMPLETE"}

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
        body = ["#!/bin/bash", "set -euo pipefail",
                f"cd {unit_dir}", u["command"], ""]
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
        # The engine owns its interior. Give it a fresh work dir and a fresh
        # publish dir inside the exclusive root, then stay out of the way.
        rc, out, err = U.run(["sh", "-c", u["command"]], cwd=str(unit_dir),
                             timeout=u.get("timeout_s", 120))
        if rc != 0:
            return None, f"engine launch failed: {(err or out).strip()[:200]}"
        return (out.strip().splitlines() or ["engine-local"])[-1], None
    if kind == "code":
        # Shreshth's path, unchanged: Paseo owns the agent lifecycle.
        rc, out, err = U.run(
            ["paseo", "run", "--background", "--provider",
             u.get("provider", "claude"), "--title", f"[swarm] {u['id']}",
             u.get("prompt") or u.get("command") or u["id"]], timeout=180)
        if rc != 0:
            return None, f"paseo run failed: {(err or out).strip()[:200]}"
        for tok in (out or "").split():
            if len(tok) >= 32 and "-" in tok:
                return tok, None
        return None, "could not read an agent id from paseo run output"
    return None, f"unknown kind {kind!r}"


def _bind(unit_dir, job_id):
    rc, out, err = U.run([sys.executable, str(_HERE / "unit.py"), "bind",
                          str(unit_dir), "--job-id", str(job_id)], timeout=120)
    return None if rc == 0 else f"bind failed: {(err or out).strip()[:200]}"


def _check(unit_dir):
    rc, out, err = U.run([sys.executable, str(_HERE / "unit.py"), "check",
                          str(unit_dir)], timeout=300)
    return rc, (out or "") + (err or "")


# --- the DAG --------------------------------------------------------------
RETRYABLE = {PREEMPTED}          # a preemption is not a failure
TERMINAL_BAD = {FAILED}


def advance(plan, state, state_dir, root, dry_run, max_new=None):
    """Idempotent. Re-check every live unit, then dispatch whatever is ready.

    Safe to run from a Paseo schedule or cron every few minutes: it never
    re-submits a unit that has an attempt recorded, and it persists before it
    acts."""
    units = {u["id"]: u for u in plan["units"]}
    report, dispatched, halted = [], 0, state.get("halted")

    # 1. Re-check anything with a live attempt. The coordinator does not judge;
    #    unit.py does, and its exit code is the whole input.
    for uid, u in units.items():
        us = _unit_state(state, uid)
        if not us["attempt_dir"] or us["state"] == "DONE":
            continue
        rc, text = _check(us["attempt_dir"])
        us["state"] = NAME.get(rc, f"rc={rc}")
        report.append(f"{uid}: {us['state']}")
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
        if us["attempt_dir"] or us["state"] in ("DONE", "FAILED"):
            continue
        # A failed upstream is checked FIRST. Ordered the other way round, the
        # HELD branch was dead code: a FAILED dependency is also not DONE, so
        # `unmet` was non-empty and the loop skipped past HELD every time. The
        # distinction matters -- "waiting" and "will never run" need different
        # actions from whoever reads the status.
        needs = u.get("needs") or []
        failed_upstream = [d for d in needs
                           if _unit_state(state, d)["state"] in
                           ("FAILED", "HELD")]
        if failed_upstream:
            us["state"] = "HELD"
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
        us["gpu_hours"] = want
        spent += want
        save_state(state_dir, state)

        job_id, err = _submit(u, unit_dir, dry_run)
        if err:
            us["state"] = "FAILED"
            report.append(f"{uid}: {err}")
            save_state(state_dir, state)
            continue
        berr = _bind(unit_dir, job_id) if str(job_id).replace("_", "").isdigit() \
            else None
        if berr:
            report.append(f"{uid}: submitted {job_id} but {berr}")
        us["job_id"] = str(job_id)
        us["state"] = "SUBMITTED"
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
    state = load_state(args.state_dir)
    report, dispatched, halted = advance(
        plan, state, args.state_dir, args.root, args.dry_run,
        args.max_new_dispatches)
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


def cmd_status(args):
    plan, err = U.read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    state = load_state(args.state_dir)
    rows, bad = [], False
    for u in plan.get("units") or []:
        us = state["units"].get(u["id"]) or {}
        st = us.get("state") or "-"
        if st == "FAILED":
            bad = True
        rows.append((u["id"], u.get("kind", "?"), st,
                     us.get("job_id") or "-",
                     f"{us.get('gpu_hours', 0):g}",
                     len(us.get("attempts") or [])))
    w = max([len(r[0]) for r in rows] + [4])
    print(f"  {'unit'.ljust(w)}  {'kind':9} {'state':10} {'job':14} {'gpuh':>5} att")
    for r in rows:
        print(f"  {r[0].ljust(w)}  {r[1]:9} {r[2]:10} {r[3]:14} {r[4]:>5} {r[5]}")
    if state.get("halted"):
        print(f"\n  HALTED: {state['halted']}")
    return EXIT_FAILED_UNIT if bad else EXIT_OK


def main():
    ap = argparse.ArgumentParser(
        prog="swarm.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("plan")
        p.add_argument("--state-dir", default=".swarm/state")
        p.add_argument("--root", default=".swarm/runs")
        p.add_argument("--dry-run", action="store_true",
                       help="allocate and record, but do not submit. The DAG "
                            "logic is testable without a scheduler.")
        p.add_argument("--max-new-dispatches", type=int, default=None)

    v = sub.add_parser("validate", help="acyclic deps, disjoint write scopes")
    v.add_argument("plan")
    v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("run", help="dispatch what is ready, then exit")
    common(r)
    r.set_defaults(fn=cmd_run)

    a = sub.add_parser("advance", help="idempotent; for a schedule or cron")
    common(a)
    a.set_defaults(fn=cmd_advance)

    s = sub.add_parser("status", help="what every unit is doing")
    s.add_argument("plan")
    s.add_argument("--state-dir", default=".swarm/state")
    s.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
