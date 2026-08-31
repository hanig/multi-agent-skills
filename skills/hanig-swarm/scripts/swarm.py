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
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import unit as U  # noqa: E402  same skill, installed together
import worktree as W  # noqa: E402
import verify as V  # noqa: E402

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
# Two receipts claim different tracker refs for one intent, so something was
# filed twice, in two places. Not a usage error and not a clean run, so it
# gets its own code and a script can branch on it.
EXIT_CONFLICT = 3


class OutboxError(Exception):
    """The receipt journal cannot be written or read safely."""


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


def declared_account(u):
    """The account a unit charges, or None. Same five spellings as partition."""
    args = [str(a) for a in (u.get("sbatch") or [])]
    for i, a in enumerate(args):
        if a.startswith("--account="):
            return a.split("=", 1)[1]
        if a.startswith("-A="):
            return a.split("=", 1)[1]
        if a in ("--account", "-A") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("-A") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
    return None


def declared_partition(u):
    """The partition a unit asks for, or None.

    Handles BOTH spellings: `--partition=cpu` and `--partition cpu`. Reading
    only the equals form reported a unit that plainly declares a partition as
    declaring none, which would make the validator's own honesty message a
    false statement."""
    args = [str(a) for a in (u.get("sbatch") or [])]
    for i, a in enumerate(args):
        if a.startswith("--partition="):
            return a.split("=", 1)[1]
        if a.startswith("-p="):
            return a.split("=", 1)[1]
        if a in ("--partition", "-p") and i + 1 < len(args):
            return args[i + 1]
        # `-pcpu`, the attached short form. Missing it made the advisory line
        # claim a unit declared no partition when it plainly did, which turns
        # an honesty message into a false one.
        if a.startswith("-p") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
    return None


def _known_partitions():
    """Partition names this cluster actually has, or None if it cannot be told.

    None is not an empty set. A host without `sinfo`, or a scheduler that does
    not answer, means UNKNOWN, and a validator that refuses on unknown is a
    validator that blocks honest work on the first flaky day."""
    if not shutil.which("sinfo"):
        return None
    rc, out, _ = U.run(["sinfo", "-h", "-o", "%P"], timeout=30)
    if rc != 0 or not (out or "").strip():
        return None
    names = set()
    for line in out.splitlines():
        n = line.strip().rstrip("*")        # the default partition carries a *
        if n:
            names.add(n)
    return names or None


# A sentinel, because None already means something. `known=None` was doing
# double duty -- "could not be determined" AND "go and look it up" -- and the
# two coincide only on a machine WITHOUT sinfo. On a real cluster a caller
# passing None to mean "unknown" triggered a live lookup instead, so a test
# asserting "unknown refuses nothing" passed on a laptop and failed on
# andromeda. Found by running the suite where it will actually run.
_LOOK_IT_UP = object()


def partition_problems(units, known=_LOOK_IT_UP):
    """Which units name a partition this cluster does not have.

    A project runs on ONE server and its plan carries that server's sbatch
    flags, so a plan written for lambda and run on chimera names partitions
    that do not exist here. Caught at validate, it is one clear line; caught
    at submit, it is a half-dispatched DAG and an sbatch error per unit."""
    if known is _LOOK_IT_UP:
        known = _known_partitions()
    # None means UNKNOWN and must refuse nothing. An empty set means the query
    # came back empty, which is also not evidence that a partition is absent.
    if not known:
        return []
    bad = []
    for u in units:
        if not isinstance(u, dict):
            continue
        name = declared_partition(u)
        if name and name not in known:
            bad.append((u.get("id", "?"), name))
    return bad


RESOLUTIONS = ("direct", "path", "conda", "container", "module", "uv",
               "wrapper")

# Kinds whose work runs somewhere else, so the runtime there is not knowable
# from here and must be declared.
_NEEDS_RUNTIME = ("slurm", "pipeline")


def _ancestors(uid, units_by_id, seen=None):
    """Every unit that must close before `uid` starts."""
    seen = seen if seen is not None else set()
    for dep in (units_by_id.get(uid, {}).get("needs") or []):
        if dep in seen:
            continue
        seen.add(dep)
        _ancestors(dep, units_by_id, seen)
    return seen


def resolve_runtime(plan, unit):
    """A unit's runtime profile, whether inline or referenced by id."""
    rt = unit.get("runtime")
    if rt == "none":
        return "none", "none"
    if isinstance(rt, str):
        return (plan.get("runtimes") or {}).get(rt), rt
    return rt, None


# ${VAR} $VAR {{VAR}} {VAR} %(VAR)s %VAR% @VAR@ <VAR>, anywhere in the string.
_PLACEHOLDER_RE = re.compile(
    r"\$\{[^}]*\}"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\{\{[^}]*\}\}"
    r"|\{[A-Za-z_][A-Za-z0-9_]*\}"
    r"|%\([^)]*\)"
    r"|%[A-Za-z_][A-Za-z0-9_]*%"
    r"|@[A-Za-z_][A-Za-z0-9_]*@"
    r"|<[^>]*>")

_PLACEHOLDER_EXACT = ("...", "PATH", "TBD", "TODO", "FIXME", "CHANGEME",
                      "XXX", "N/A", "NA", "-")
_PLACEHOLDER_PREFIX = ("<", "TODO", "FIXME", "CHANGEME", "XXX")


def _looks_unresolved(text):
    """Is this input still a hole somebody meant to fill in?

    The first version checked four prefixes and three exact strings, so
    "${CORPUS}" sailed straight through: not one of the exact values, does not
    begin with "<", and looks like an ordinary relative path to everything
    else. A plan built on it validates, has tickets filed against it, and then
    waits for a value nobody was asked for, which is the exact failure this
    check exists to prevent.

    Shell and template syntax are how these are usually spelled, so they are
    named here. This cannot be exhaustive, and it is not the real defence: the
    interview is. Reaching this refusal means the interview already failed.
    """
    t = text.strip()
    if t in _PLACEHOLDER_EXACT or t.startswith(_PLACEHOLDER_PREFIX):
        return True
    if t.endswith(">"):
        return True
    # EMBEDDED counts. Checking only the string's edges accepted
    # "data/$CORPUS/shard.fastq" and "samples/{sample}.fastq", which are just
    # as unresolved as the bare form and are the way people actually write
    # them. A dispatched command would open that literal path.
    return bool(_PLACEHOLDER_RE.search(t))


def _runtime_identity(rt):
    """What makes two runtimes the same THING to execute in.

    Not the whole profile: a canary's own `verified_by` necessarily differs
    from the unit it vouches for, because it cannot be verified by itself.
    What must match is what actually gets run.
    """
    if rt == "none":
        return ("none",)
    if not isinstance(rt, dict):
        return (None,)
    return (rt.get("id"), rt.get("resolution"), rt.get("entrypoint"))


def _validate_runtimes(plan, units):
    by_id = {u.get("id"): u for u in units if isinstance(u, dict)}
    catalogue = plan.get("runtimes") or {}
    if catalogue and not isinstance(catalogue, dict):
        raise PlanError("'runtimes' must be an object of id -> profile")

    for u in units:
        if not isinstance(u, dict) or u.get("kind") not in _NEEDS_RUNTIME:
            continue
        uid = u.get("id", "?")
        # "none" is a DECLARATION, not an omission: this unit runs only tools
        # the base image guarantees (coreutils, tar, the scheduler itself).
        # Without it a unit running `sha256sum` would have to invent a runtime
        # profile, and a required field that is noise for a third of its uses
        # becomes a rubber stamp: everyone pastes "unverified: n/a" and the
        # declaration stops meaning anything. "none" is a specific claim, and
        # it stays visible at the approval gate and in the report.
        if u.get("runtime") == "none":
            continue

        rt, ref = resolve_runtime(plan, u)

        # A dangling reference must be reported AS a dangling reference. It
        # resolves to None, so an ordering slip here reports "declares no
        # runtime" about a unit that plainly declares one, which is the same
        # false-honesty bug the partition message had.
        if isinstance(u.get("runtime"), str) and not isinstance(rt, dict):
            raise PlanError(
                f"unit {uid!r} references runtime {ref!r}, which is not "
                f"defined in the plan's 'runtimes'.")

        if rt is None:
            raise PlanError(
                f"unit {uid!r} is kind={u.get('kind')!r} and declares no "
                f"'runtime'. Which interpreter or image runs this, and what "
                f"establishes that it works where the job lands? That is not "
                f"discoverable from the command: shell hides it behind "
                f"wrappers, modules, containers and variables. Declare it:\n"
                f'    "runtime": {{"id": "...", "resolution": "direct", '
                f'"entrypoint": "/abs/path", "verified_by": "canary:<unit>"}}'
                f"\n  or reference a profile from the plan's 'runtimes', "
                f'or declare "runtime": "none" if this runs only tools the '
                f"base image guarantees.")
        if not isinstance(rt, dict):
            raise PlanError(f"unit {uid!r} has a 'runtime' that is not an "
                            f"object")

        res = rt.get("resolution")
        if res not in RESOLUTIONS:
            raise PlanError(
                f"unit {uid!r} runtime.resolution={res!r}; use one of: "
                f"{', '.join(RESOLUTIONS)}. This says HOW the runtime is "
                f"reached, which is the part a reader cannot infer.")
        if not str(rt.get("entrypoint") or "").strip():
            raise PlanError(
                f"unit {uid!r} runtime declares no 'entrypoint'. Name the "
                f"interpreter, image or wrapper this actually executes.")

        vb = str(rt.get("verified_by") or "").strip()
        if not vb:
            raise PlanError(
                f"unit {uid!r} runtime declares no 'verified_by'. A declared "
                f"runtime that nothing checks is a hope. Use "
                f"'canary:<unit-id>', 'preflight', or "
                f"'unverified:<why that is acceptable here>'.")

        if vb.startswith("canary:"):
            canary = vb.split(":", 1)[1].strip()
            if canary not in by_id:
                raise PlanError(
                    f"unit {uid!r} says its runtime is verified by canary "
                    f"{canary!r}, which is not a unit in this plan.")
            if canary == uid:
                raise PlanError(
                    f"unit {uid!r} names ITSELF as its runtime canary. The "
                    f"workload must not be its own probe: by the time it "
                    f"fails, the fan-out has already been dispatched.")
            if canary not in _ancestors(uid, by_id):
                raise PlanError(
                    f"unit {uid!r} is verified by canary {canary!r}, but "
                    f"{canary!r} is not an ancestor of it, so nothing stops "
                    f"{uid!r} starting before the probe closes. Add it to "
                    f"'needs' (directly or upstream).")

            # ORDERING IS NOT PROOF. Ancestry alone let a `runtime: "none"`
            # probe on the cpu partition stand as evidence for a python
            # runtime on gpu: it ran first, and it established nothing about
            # the thing it was vouching for. A canary has to exercise the
            # same runtime, in the same place.
            crt, _cref = resolve_runtime(plan, by_id[canary])
            if _runtime_identity(crt) != _runtime_identity(rt):
                raise PlanError(
                    f"unit {uid!r} is verified by canary {canary!r}, but "
                    f"{canary!r} declares a different runtime. A probe that "
                    f"does not run the runtime it vouches for proves nothing "
                    f"about it. Give the canary the same runtime.")
            # A canary whose command is `true` exercises nothing. We cannot
            # tell from arbitrary shell whether a command runs a runtime, and
            # parsing it is the move already rejected here. So compare
            # DECLARED to DECLARED: the profile states its probe, and the
            # canary must be running exactly that.
            probe = str((rt or {}).get("probe") or "").strip()
            if not probe:
                raise PlanError(
                    f"runtime for unit {uid!r} is verified by canary "
                    f"{canary!r} but declares no 'probe'. State the command "
                    f"that establishes this runtime works, so the canary can "
                    f"be checked against it rather than trusted.")
            ccmd = str(by_id[canary].get("command") or "").strip()
            if ccmd != probe:
                raise PlanError(
                    f"canary {canary!r} does not run the runtime's declared "
                    f"probe. Its command is {ccmd!r}; the probe is {probe!r}. "
                    f"A canary that closes without exercising the runtime "
                    f"proves the runtime works exactly as much as `true` "
                    f"does.")

            cacct = declared_account(by_id[canary])
            uacct = declared_account(u)
            if cacct != uacct:
                raise PlanError(
                    f"unit {uid!r} is charged to "
                    f"{uacct or 'the default account'} but its canary "
                    f"{canary!r} runs under "
                    f"{cacct or 'the default account'}. Access to a runtime "
                    f"and its files can differ by account, so a probe under "
                    f"another one does not establish this one works.")

            cpart = declared_partition(by_id[canary])
            upart = declared_partition(u)
            # ABSENCE IS A VALUE. "Declares no partition" means "the cluster
            # default", which is a specific partition, not a wildcard. Writing
            # `if cpart and upart and cpart != upart` let a canary with no
            # partition vouch for a unit on gpu: it ran on the default cpu
            # queue and established nothing. Compare them directly so None
            # only matches None.
            if cpart != upart:
                raise PlanError(
                    f"unit {uid!r} runs on partition "
                    f"{upart or 'the cluster default'} but its canary "
                    f"{canary!r} runs on {cpart or 'the cluster default'}. A "
                    f"runtime that resolves on one partition may not resolve "
                    f"on another, so the probe has to land where the work "
                    f"lands. Declare the same partition on both.")
        elif vb == "preflight":
            pass
        elif vb.startswith("unverified:"):
            why = vb.split(":", 1)[1].strip()
            if len(why) < 12:
                raise PlanError(
                    f"unit {uid!r} declares its runtime unverified but gives "
                    f"no real reason. An unverified runtime is allowed and "
                    f"is sometimes right; it has to be a decision somebody "
                    f"made on purpose and can be held to.")
        else:
            raise PlanError(
                f"unit {uid!r} runtime.verified_by={vb!r}; use "
                f"'canary:<unit-id>', 'preflight', or 'unverified:<why>'.")

    # NOTE what is deliberately absent: no stat of the entrypoint, and no
    # parsing of the command. Both would assert facts about a machine this
    # one cannot see.


def validate_plan(plan):
    """Raise PlanError, or return a summary. Refuses BEFORE anything is
    dispatched: a plan that cannot be run should not half-run."""
    if not isinstance(plan, dict):
        raise PlanError("the plan is not a JSON object")
    units = plan.get("units")
    if not isinstance(units, list) or not units:
        raise PlanError("the plan declares no units; add a 'units' list")

    # --- list fields must BE lists ---------------------------------------
    #
    # `sbatch` is read as [str(a) for a in (u.get("sbatch") or [])]. Hand it
    # the string "--partition=cpu_batch" and Python iterates it CHARACTER by
    # character, so declared_partition() finds nothing, the unit silently
    # lands on the cluster default, and validate prints "declares no
    # partition" about a unit that plainly declares one. That turns the
    # validator's own honesty message into a false statement, which is the
    # third time that exact failure has been paid for in this function.
    #
    # A string is the natural thing to write here, so it must be refused
    # loudly rather than misread quietly. Same for the other iterated fields.
    for u in units:
        if not isinstance(u, dict):
            continue
        for field in ("needs", "inputs", "outputs", "sbatch",
                      "requires_verification"):
            val = u.get(field)
            if val is None or isinstance(val, list):
                continue
            raise PlanError(
                f"unit {u.get('id','?')!r} has {field}={val!r}, a "
                f"{type(val).__name__}, but {field} must be a list. A string "
                f"here is not rejected by the code that reads it: it is "
                f"iterated one character at a time, so the value is silently "
                f"lost and the unit runs with a default instead. Write "
                f'["--partition=cpu_batch"], not "--partition=cpu_batch".')

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

    for u in units:
        if isinstance(u, dict) and u.get("promote_to"):
            _, derr = resolve_promote_to(u.get("promote_to"))
            if derr:
                raise PlanError(
                    f"unit {u.get('id', '?')!r} declares promote_to "
                    f"{u['promote_to']!r}, but {derr}")

    # --- retry exposure ---------------------------------------------------
    # Enforce only DECLARED facts. Exposure is NOT inferred from output count,
    # command text, gpu_hours, a partition named "preemptible", walltime or
    # fan-out: none of those establishes how much work is lost, and a warning
    # built on them cries wolf until it is switched off.
    limits = (plan.get("retry_limits") or {})
    for k, v in limits.items():
        # A string limit reached the `value > cap` comparison and raised
        # TypeError, crashing the validator rather than refusing the plan.
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or v != v or v in (float("inf"), float("-inf")) or v < 0:
            raise PlanError(f"retry_limits[{k!r}]={v!r}; a limit must be a "
                            f"non-negative, finite number.")
        if k not in CHARGE_METRICS:
            raise PlanError(
                f"retry_limits names {k!r}, which is not a known metric. "
                f"Use one of: {', '.join(CHARGE_METRICS)}.")
    for u in units:
        if not isinstance(u, dict):
            continue
        uid = u.get("id", "?")
        attempts = u.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
        # 2.0 is 2. JSON has one number type, so refusing a float here
        # rejected plans that were previously fine and said nothing useful
        # about their retry exposure -- a refusal earning nothing.
        if isinstance(attempts, bool) or not isinstance(attempts, (int, float)) \
                or attempts != attempts or attempts in (float("inf"),
                                                        float("-inf")) \
                or attempts != int(attempts) or int(attempts) < 0:
            raise PlanError(f"unit {uid!r} has max_attempts={attempts!r}; it "
                            f"must be a whole number of at least 0.")
        attempts = max(1, int(attempts))     # 0 and 1 both mean "run once"
        retry = u.get("retry")
        if attempts == 1:
            continue
        if not isinstance(retry, dict):
            raise PlanError(
                f"unit {uid!r} asks for {attempts} attempts but declares no "
                f"'retry' contract. A retry starts in a FRESH EMPTY attempt "
                f"directory, so it redoes the whole unit unless a tested "
                f"resume path says otherwise. Declare what an interruption "
                f"costs:\n"
                f'      "retry": {{"mode": "restart", '
                f'"max_lost": {{"read_bytes": <n>}}}}\n'
                f"    or set max_attempts to 1.")
        mode = retry.get("mode")
        if mode not in RETRY_MODES:
            raise PlanError(f"unit {uid!r} has retry.mode={mode!r}; use "
                            f"{' or '.join(repr(m) for m in RETRY_MODES)}.")
        if mode == "resume":
            raise PlanError(
                f"unit {uid!r} declares retry.mode='resume', which is NOT "
                f"SUPPORTED yet. Cross-attempt handoff is not built and has "
                f"not passed a forced-preemption test, and shipping the claim "
                f"before the mechanism is how a false pass gets made. Use "
                f"'restart' with a max_lost you can afford, or split the unit.")
        lost = retry.get("max_lost")
        if not isinstance(lost, dict) or not lost:
            raise PlanError(
                f"unit {uid!r} declares retry.mode='restart' without "
                f"'max_lost'. State what ONE interruption costs, in a metric "
                f"the project also limits: {', '.join(CHARGE_METRICS)}.")
        for metric, value in lost.items():
            if metric not in CHARGE_METRICS:
                raise PlanError(f"unit {uid!r} max_lost names {metric!r}, "
                                f"which is not a known metric. Use one of: "
                                f"{', '.join(CHARGE_METRICS)}.")
            # NaN slipped through every comparison: NaN < 0 is False and
            # NaN > cap is False, so a unit could declare an unbounded loss
            # and pass the check that exists to bound it.
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or value != value \
                    or value in (float("inf"), float("-inf")) or value < 0:
                raise PlanError(f"unit {uid!r} max_lost[{metric!r}]="
                                f"{value!r}; it must be a non-negative number.")
            cap = limits.get(metric)
            if cap is None:
                raise PlanError(
                    f"unit {uid!r} declares it can lose {value} {metric} per "
                    f"interruption, but the plan sets no retry_limits for "
                    f"{metric!r}, so nothing says whether that is acceptable. "
                    f"Add: \"retry_limits\": {{\"{metric}\": <n>}}")
            if value > cap:
                raise PlanError(
                    f"unit {uid!r} can lose {value} {metric} per interruption, "
                    f"over the project limit of {cap}. Split it into smaller "
                    f"units, or raise the limit deliberately.")

    lim = plan.get("limits") or {}
    if not isinstance(lim, dict):
        raise PlanError("'limits' must be an object")
    mr = lim.get("max_running")
    if mr is not None and (not isinstance(mr, int) or isinstance(mr, bool)
                           or mr < 1):
        raise PlanError(f"limits.max_running={mr!r}; it must be an integer of "
                        f"at least 1.")
    pools = lim.get("pools") or {}
    if not isinstance(pools, dict):
        raise PlanError("limits.pools must be an object of name -> integer")
    for name, cap in pools.items():
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            raise PlanError(f"limits.pools[{name!r}]={cap!r}; it must be an "
                            f"integer of at least 1.")
    for u in units:
        pool = isinstance(u, dict) and u.get("pool")
        if pool and pool not in pools:
            raise PlanError(
                f"unit {u.get('id','?')!r} joins pool {pool!r}, which is not "
                f"declared in limits.pools. A pool with no cap bounds nothing.")

    for u in units:
        if not isinstance(u, dict):
            continue
        cont = u.get("continuation")
        if cont is not None:
            if not isinstance(cont, dict):
                raise PlanError(
                    f"unit {u.get('id','?')!r} has continuation={cont!r}; it "
                    f"must be an object with a 'max'.")
            mx = cont.get("max")
            if not isinstance(mx, int) or isinstance(mx, bool) or mx < 1:
                raise PlanError(
                    f"unit {u.get('id','?')!r} has continuation.max={mx!r}; "
                    f"it must be a whole number of at least 1. A bound that "
                    f"is not a number is not a bound.")
            if u.get("kind") != "code":
                raise PlanError(
                    f"unit {u.get('id','?')!r} is kind={u.get('kind')!r} and "
                    f"declares 'continuation'. A continuation resumes a "
                    f"conversational turn, which only a code agent has. A "
                    f"Slurm job that exits is retried, not prodded.")
        for claim in (u.get("requires_verification") or []):
            if not str(claim).strip():
                raise PlanError(
                    f"unit {u.get('id','?')!r} declares an empty entry in "
                    f"'requires_verification'. Name the claim a verifier must "
                    f"establish, e.g. \"tests-pass\".")
        if u.get("requires_verification") and u.get("kind") != "code":
            raise PlanError(
                f"unit {u.get('id','?')!r} is kind={u.get('kind')!r} and "
                f"declares 'requires_verification'. Verification binds to a "
                f"produced commit, which only a code unit has. A slurm or "
                f"pipeline unit is judged by its declared outputs.")

    # --- an output must land where the predicate will look -----------------
    #
    # THE most load-bearing constraint in the model, and it was learned from a
    # runtime INCOMPLETE receipt after a full dispatch cycle. The done
    # predicate looks inside the attempt's exclusive write root and nowhere
    # else, so an output declared as an absolute path somewhere else is
    # unfindable by construction: the work can succeed completely and the unit
    # can never close.
    #
    # Refused here because it is knowable here, and because a whole
    # dispatch-and-diagnose cycle is an expensive way to learn a path is
    # wrong.
    for u in units:
        if not isinstance(u, dict):
            continue
        for out in (u.get("outputs") or []):
            text = str(out).strip()
            if not text:
                raise PlanError(
                    f"unit {u.get('id','?')!r} declares an empty output. Name "
                    f"the artifact, relative to the attempt's write root.")
            if os.path.isabs(text):
                raise PlanError(
                    f"unit {u.get('id','?')!r} declares the output {text!r} as "
                    f"an ABSOLUTE path. Declared outputs are looked for inside "
                    f"the attempt's exclusive write root and nowhere else, so "
                    f"this one can never be found and the unit can never "
                    f"close, however well the work goes. Declare it relative "
                    f"to the write root; use $SWARM_UNIT_DIR in the command if "
                    f"the tool needs an absolute path. To publish somewhere "
                    f"shared, declare 'promote_to' instead.")
            if text.startswith("..") or "/../" in text:
                raise PlanError(
                    f"unit {u.get('id','?')!r} declares the output {text!r}, "
                    f"which climbs out of the attempt's write root. The write "
                    f"root is exclusive so that finding an artifact there is "
                    f"conclusive; an output above it is neither exclusive nor "
                    f"findable.")

    # --- the runtime a unit will actually execute in -----------------------
    #
    # Nothing validated this, and it is the likeliest reason a scientific unit
    # dies on first contact. In a real run the plan artifact could not answer
    # "which python runs this?"; it took interrogating the planner.
    #
    # My first design was to refuse a bare `python` and stat absolute
    # interpreter paths. Sol rejected both, correctly:
    #
    #   There is no reliable static shell-text chokepoint. Shell allows
    #   variables, aliases, functions, nested shells, here-documents,
    #   wrappers, and container namespace changes. `srun python`,
    #   `conda run -n e python`, `apptainer exec img python`, `uv run` and
    #   `bash -lc` with modules are all legitimate and unparseable.
    #
    #   And a submit-host stat is an observed SUBMIT-HOST fact. Concluding
    #   from it that the path exists inside a compute node, container or
    #   module shell would be inferring an undeclared fact, which is the one
    #   thing this validator must never do. It has both failure modes: the
    #   path can exist on the login node and not the compute node, or exist
    #   only inside the container.
    #
    # So: DECLARE the runtime, uniformly, and prove it where it actually
    # runs. Refusal never depends on spotting the word "python".
    _validate_runtimes(plan, units)

    # --- can this plan actually RUN? -------------------------------------
    #
    # A plan was built, validated, and had five tracker issues filed for it,
    # and only then did anyone discover that the one value it needed -- the
    # corpus path -- had never been asked for. The planner had even written
    # "I'll need the subpath and the glob" in an earlier answer and then never
    # came back for it. The interview stopped when the planner ran out of
    # prepared questions, not when the plan could run.
    #
    # So: a declared input must either exist, or be produced by an upstream
    # unit. An unresolved placeholder is a plan that cannot run, and that is a
    # fact available NOW rather than at dispatch.
    # An output only resolves an input if its producer actually runs FIRST.
    # This used to be a flat set of every output in the plan, so a consumer
    # declaring input "generated.txt" validated even with no dependency on the
    # unit that generates it, and the coordinator was then free to start it
    # before that file existed. "Something in this plan makes it" is not the
    # same claim as "it will exist when I run".
    _by_id = {u.get("id"): u for u in units if isinstance(u, dict)}
    produced_by = {}
    for u in units:
        if not isinstance(u, dict):
            continue
        for out in (u.get("outputs") or []):
            produced_by.setdefault(out, set()).add(u.get("id"))
    unresolved = []
    for u in units:
        if not isinstance(u, dict):
            continue
        for raw in (u.get("inputs") or []):
            text = str(raw).strip()
            if not text:
                unresolved.append((u.get("id", "?"), "<empty>"))
                continue
            # A placeholder somebody meant to fill in.
            if _looks_unresolved(text):
                unresolved.append((u.get("id", "?"), text))
                continue
            makers = produced_by.get(text)
            if makers:
                uid_here = u.get("id")
                if makers & _ancestors(uid_here, _by_id):
                    continue                # an UPSTREAM unit makes it
                named = ", ".join(sorted(m for m in makers if m))
                unresolved.append((
                    uid_here,
                    f"{text} (produced by {named}, which is not upstream of "
                    f"it, so nothing orders them)"))
                continue
            # A glob that matches nothing, or a path that is not there, is
            # only knowable locally; skip silently when it is neither, since
            # refusing on an unreadable mount would block honest work.
            if any(ch in text for ch in "*?[") :
                import glob as _glob
                if os.path.isabs(text) and not _glob.glob(text):
                    unresolved.append((u.get("id", "?"),
                                       f"{text} (matches nothing)"))
            elif os.path.isabs(text) and not os.path.exists(text):
                unresolved.append((u.get("id", "?"), f"{text} (does not exist)"))
    if unresolved:
        listed = "; ".join(f"{uid} needs {what}" for uid, what in unresolved)
        raise PlanError(
            f"this plan cannot run: {listed}.\n"
            f"    An input that is missing, empty or still a placeholder is "
            f"not a detail to settle later: the plan will be built, issues "
            f"will be filed for it, and it will then sit waiting for a value "
            f"nobody was asked for. Settle it before dispatching.")

    bad_parts = partition_problems(units)
    if bad_parts:
        known = sorted(_known_partitions() or [])
        listed = "; ".join(f"{uid} wants {name!r}" for uid, name in bad_parts)
        raise PlanError(
            f"this plan names partition(s) that {os.uname().nodename} does "
            f"not have: {listed}. This cluster offers: {', '.join(known)}. A "
            f"project runs on ONE server and its plan carries that server's "
            f"sbatch flags, so this is usually a plan written for a different "
            f"cluster. Refusing here beats a half-dispatched DAG and an "
            f"sbatch error per unit.")

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
    # --- a slurm command is the WORK, not a submission --------------------
    #
    # `sbatch --wrap='...'` as a unit command: the coordinator wrapped it in
    # its own sbatch, the outer job submitted an inner job it was not bound
    # to, and exited in 00:00:00. Slurm reported COMPLETED, ExitCode 0:0. Only
    # the missing declared output caught it, one dispatch cycle later.
    #
    # This is the exact confusion the whole system exists to prevent -- a
    # scheduler reporting success for work that never ran -- so it is refused
    # at the one place it is cheap to refuse.
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "slurm":
            continue
        cmd = str(u.get("command") or "").strip()
        first = (cmd.split() or [""])[0]
        if os.path.basename(first) in ("sbatch", "srun", "salloc"):
            raise PlanError(
                f"unit {u.get('id','?')!r} has a command starting with "
                f"{os.path.basename(first)!r}. The coordinator submits this "
                f"command ITSELF, so a submission here nests one job inside "
                f"another: the outer job exits in seconds having queued an "
                f"inner job nothing is bound to, and Slurm reports COMPLETED "
                f"with ExitCode 0:0 for work that never ran. Give the command "
                f"that does the work, and put scheduler flags in 'sbatch'.")

    # --- a code unit needs a branch of its own ---------------------------
    #
    # `write_scopes` names FILES, and it does isolate the attempt directory.
    # It does not isolate a repository: agents share a checkout, so nine code
    # units on one repo would run against one working tree, one branch and one
    # index, and the scopes would say nothing about it.
    #
    # And a `code` unit closes on a MERGED PR. A unit with no branch has
    # nothing to open a PR from, so it is structurally unclosable: the plan can
    # be built, dispatched and judged, and the unit can never reach DONE. That
    # is worth refusing at the one point it is cheap.
    by_repo = {}
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "code":
            continue
        if not u.get("repo"):
            continue
        uid = u.get("id", "?")
        branch = str(u.get("branch") or "").strip()
        if not branch:
            raise PlanError(
                f"unit {uid!r} is kind=code on repo {u['repo']!r} and declares "
                f"no 'branch'. A code unit is closed by a MERGED PULL REQUEST, "
                f"so with no branch there is nothing to open one from and the "
                f"unit can never close however good the work is. Agents also "
                f"share a checkout: write scopes name files and isolate the "
                f"attempt directory, not a working tree, so two units on one "
                f"repo without distinct branches run against one index.")
        by_repo.setdefault(str(u["repo"]), {}).setdefault(branch, []).append(uid)

    for repo, branches in by_repo.items():
        for branch, ids in branches.items():
            if len(ids) > 1:
                raise PlanError(
                    f"units {', '.join(sorted(ids))} all target branch "
                    f"{branch!r} of {repo!r}. One branch cannot carry two "
                    f"units' work as separable changes: their commits "
                    f"interleave, one PR merges both, and neither unit's "
                    f"produced tree means what its receipt says. Give each "
                    f"unit its own branch.")

    return {"units": len(units),
            "with_deps": sum(1 for u in units if u.get("needs")),
            # Which slurm units say nothing about where they run. The
            # partition check reads u["sbatch"], so an empty one means it
            # examined nothing -- and a validator silent about what it skipped
            # is indistinguishable from one that checked and approved.
            # Units that will make exactly ONE attempt because they said
            # nothing. Reported as a FACT about the policy being applied, not
            # as a guess about whether that is wise: inferring "you probably
            # wanted retries" from a partition name is precisely what this
            # code refuses to do elsewhere.
            "default_attempts": sorted(
                u["id"] for u in units
                if isinstance(u, dict) and "max_attempts" not in u),
            "without_partition": sorted(
                u["id"] for u in units
                if u.get("kind", "slurm") == "slurm"
                and not declared_partition(u))}


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
# older than this was abandoned by a controller that died.
DRY_PREFIX = "dry-attempt-"
SETTLE_S = 600             # accounting lag before a missing row becomes terminal

# Repetition is OPT-IN. This defaulted to 3, so every unit silently carried
# three times its stated exposure: a 1.42 TiB read on a preemptible partition
# was really a 4 TiB worst case, and nobody had asked for that. A retry starts
# in a FRESH EMPTY attempt directory, so a retry is a redo unless a tested
# resume contract says otherwise.
DEFAULT_MAX_ATTEMPTS = 1

# Metrics a plan may budget or declare as retry exposure. A small fixed
# vocabulary, so "read_bytes" means one thing everywhere and a typo is caught
# rather than silently ignored.
CHARGE_METRICS = ("gpu_hours", "cpu_hours", "read_bytes", "wall_seconds",
                  "items")
RETRY_MODES = ("restart", "resume")

# States in which a unit is occupying a slot on the cluster right now.
LIVE_STATES = ("ALLOCATED", "SUBMITTED", "RUNNING")


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


# Locks this process holds, KEYED BY PROJECT. A single global fd made
# acquire_lease return True for any state directory once one had been
# acquired, so a second project in the same process was never actually
# locked. Caught by the new tests, which is what they are for.
_LOCK_FDS = {}
# The pid that owns the entries above. flock is held per OPEN FILE
# DESCRIPTION, which a fork shares, so a forked child would inherit this dict
# and be told it already holds a lock it never took. Reviewers found that; the
# guard is cheaper than reasoning about who forks.
_LOCK_OWNER_PID = None
LOCK = "lease.lock"


def acquire_lease(state_dir):
    """One controller at a time, arbitrated by the OS. Returns (ok, holder).

    THE SIXTH VERSION, and the first that does not invent its own mutual
    exclusion. The five before it were hand-rolled from atomic file
    primitives, and two independent reviews found a CRITICAL in each of the
    last two: a deposed controller could overwrite its successor's lease, and
    the breaker that was meant to prevent that was itself reclaimable by mtime
    alone, so a holder merely PAUSED past the TTL had it stolen and then
    clobbered the successor on resume. One reviewer also showed the
    reclamation was unreachable from acquire_lease, so a killed holder wedged
    the project permanently.

    Every one of those defects lived in machinery that existed for a single
    reason: a plain file cannot tell you its owner died. An advisory lock can,
    because the kernel drops it when the process exits, however it exits. That
    removes the stale-lease TTL, the breaker directory, the ownership token,
    the mtime heuristic and the renewal loop, and with them the entire class
    of bug that five rewrites could not close.

    MEASURED, not assumed: 10 concurrent processes, three trials, on lambda
    (nfs), chimera (nfs4) and andromeda (weka) -- exactly one winner every
    time, then 8 concurrent real advances against one live DAG, three trials
    per cluster, one dispatcher every time.

    WHAT THAT MEASUREMENT DOES NOT SHOW, corrected after a reviewer pointed
    it out: every contender ran on ONE host. A mount whose locking has
    degraded to local-only still excludes same-host processes perfectly, so
    neither the manual runs nor the test suite can tell real cross-client
    locking from the local-only case. An earlier version of this docstring
    claimed the suite re-checks that property. It does not, and cannot from a
    single host.

    This is adequate rather than airtight, and it is adequate for a stated
    reason: a project runs entirely on ONE server, so same-host exclusion is
    the property that has to hold. Two controllers on two different nodes
    sharing the filesystem is outside the topology, and would need a
    cross-node test to certify.

    KNOWN LIMIT, not fixed: NFS lock recovery. If the server reboots, or
    evicts this client's lock state after a partition, the lock can be dropped
    while this process is still alive and another controller can then acquire
    it. Nothing in local state can notice, because the kernel does not tell us.
    A lock cannot be made stronger than the lock manager underneath it, so
    this is recorded rather than papered over."""
    global _LOCK_OWNER_PID
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    key = str(Path(state_dir).resolve())
    if _LOCK_OWNER_PID is not None and _LOCK_OWNER_PID != os.getpid():
        # Inherited across a fork. Forgetting the fds is not enough: the child
        # holds real descriptors on the same open file description, so a
        # long-lived child would keep the project locked after the parent
        # exited and block honest work. Closing them here is safe -- the lock
        # lives on the OFD, and the parent's own descriptor still references
        # it -- and it is what actually releases the child's grip.
        for fd in list(_LOCK_FDS.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        _LOCK_FDS.clear()
        _LOCK_OWNER_PID = None
    if key in _LOCK_FDS and _holds_the_path(_LOCK_FDS[key], Path(state_dir)):
        return True, None                     # already ours, in this process
    path = Path(state_dir) / LOCK
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        return False, f"cannot open the lock at {path}: {e}"
    # CONTENTION IS ONE ERRNO. A bare `except OSError` here reported every
    # failure as "another controller holds it", including ENOLCK, which means
    # this filesystem cannot lock AT ALL. On such a mount every advance would
    # refuse forever, blaming a controller that does not exist -- a verifier
    # crying wolf, which is the failure this repo weights equally with a false
    # pass. EINTR is a signal, not a verdict, so it is retried.
    for _ in range(3):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            os.close(fd)
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                held, rerr = U.read_json(Path(state_dir) / LEASE)
                if not rerr and isinstance(held, dict):
                    age = int(time.time()
                              - float(held.get("acquired_at") or 0))
                    return False, (f"{held.get('owner')}@{held.get('host')} "
                                   f"pid {held.get('pid')}, {age}s ago")
                return False, "another controller holds it"
            return False, (
                f"this filesystem cannot lock {path} ({e.strerror}, errno "
                f"{e.errno}), so nothing can guarantee that only one "
                f"controller runs. This is NOT contention. Put the state "
                f"directory on a filesystem that supports advisory locking, "
                f"or run the coordinator somewhere that can reach one.")
    else:
        os.close(fd)
        return False, f"repeatedly interrupted while locking {path}"
    # The lock is on an INODE, not on a name. If the path was replaced
    # between our open and now, we are holding a lock on a file nobody else
    # will ever contend for, and a second controller can lock the new one.
    # Reviewers found this; it is cheap to detect and refuse.
    if not _holds_the_path(fd, Path(state_dir)):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        return False, (f"{path} was replaced while it was being locked, so "
                       f"this lock guards a file no other controller will "
                       f"contend for. Nothing dispatched. Re-run; if it "
                       f"repeats, something else is deleting the state "
                       f"directory underneath the coordinator.")
    _LOCK_FDS[key] = fd
    _LOCK_OWNER_PID = os.getpid()
    # Descriptive only. Nothing decides anything from this file; it exists so a
    # human blocked by the lock can see who has it.
    U.write_json(Path(state_dir) / LEASE,
                 {"owner": os.environ.get("USER", "?"),
                  "host": os.uname().nodename, "pid": os.getpid(),
                  "acquired_at": time.time()})
    return True, None


def _holds_the_path(fd, state_dir):
    """Is the fd we locked still the file at the lock's path?

    A flock is on an inode. If lease.lock is deleted or replaced, another
    process creates a NEW inode at that name and locks it successfully, and
    both controllers then believe they are alone. Comparing the fd's
    (device, inode) with the path's is the only way to notice.

    SCOPE, after a reviewer called the claim too strong: this is a check, and
    a check cannot be atomic with the work that follows it. Replacement in the
    instant after it returns is undetectable until the next call. What it
    genuinely buys is that a lock file deleted or replaced BETWEEN advances,
    which is the way this actually happens, is caught at the next renewal
    rather than never. It narrows the window; it does not close it. Nothing
    short of not deleting the lock file closes it."""
    try:
        a = os.fstat(fd)
        b = os.stat(str(Path(state_dir) / LOCK))
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def renew_lease(state_dir):
    """Returns True while this process still holds the lock.

    There is nothing to renew. The old lease expired on a clock because a file
    cannot notice its owner died, and that expiry is exactly what let a paused
    controller be deposed and then clobber its successor. An advisory lock is
    held until the process releases it or exits, so a slow advance keeps it by
    construction rather than by refreshing a timestamp."""
    key = str(Path(state_dir).resolve())
    if _LOCK_OWNER_PID != os.getpid():
        return False
    fd = _LOCK_FDS.get(key)
    if fd is None:
        return False
    # Not merely "we have an fd". Re-checking that the fd is still the file at
    # the path catches the replaced-inode case mid-advance, which is the one
    # way a live holder can silently stop being the only holder ON THIS HOST.
    return _holds_the_path(fd, Path(state_dir))


def release_lease(state_dir):
    """Release ours. The kernel does this anyway if we die, which is the whole
    point; doing it explicitly just frees the project sooner."""
    key = str(Path(state_dir).resolve())
    fd = _LOCK_FDS.pop(key, None)
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
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
    if u.get("repo"):
        argv += ["--repo", str(u["repo"])]
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


def _dep_env(u, state):
    """Where each upstream unit's outputs actually are, as environment.

    Without this a downstream unit has no way to find what it consumes: the
    submitted script cds into the unit's OWN exclusive directory, so authors
    were reduced to globbing `../../dep/*/file`. That is wrong the moment a
    unit retries, because a second attempt directory appears and the glob
    matches both. The coordinator knows which attempt is current, so it says
    so rather than leaving it to be guessed.

    Ids become env names: `align-reads` -> SWARM_DEP_ALIGN_READS."""
    out = []
    for dep in (u.get("needs") or []):
        d = (state.get("units", {}).get(dep) or {}).get("attempt_dir")
        if d:
            name = re.sub(r"[^A-Za-z0-9]", "_", dep).upper()
            out.append((f"SWARM_DEP_{name}", d))
    return out


def _submit(u, unit_dir, dry_run, state=None):
    """Submit, and return (job_id, error). Dispatch differs per kind; judging
    does not.

    --dry-run records what WOULD be submitted, so the DAG logic is testable
    without a scheduler. A coordinator that can only be tested on a live
    cluster does not get tested."""
    kind = u["kind"]
    if dry_run:
        return f"dry-{os.urandom(3).hex()}", None
    if kind == "slurm":
        deps = _dep_env(u, state or {})
        script = Path(unit_dir) / "job.sbatch"
        # The job is NAMED for the attempt. This is what closes the
        # crash-before-bind window: if we die between sbatch and bind, the job
        # is still running and nothing records its id -- but the scheduler
        # knows it by this name, so `reconcile` can find it instead of
        # submitting a second one.
        attempt_id = Path(unit_dir).name
        body = ["#!/bin/bash", f"#SBATCH --job-name=swarm-{attempt_id}",
                "set -euo pipefail", f"cd {unit_dir}",
                f"export SWARM_UNIT_ID={shlex.quote(u['id'])}",
                f"export SWARM_UNIT_DIR={shlex.quote(str(unit_dir))}"]
        # Quoted: a path is data. A directory with a space in its name must
        # not become two words in a shell script we generate.
        body += [f"export {n}={shlex.quote(v)}" for n, v in deps]
        body += [u["command"], ""]
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
        penv = dict(os.environ)
        penv["SWARM_UNIT_ID"] = str(u["id"])
        penv["SWARM_UNIT_DIR"] = str(unit_dir)
        penv.update(dict(_dep_env(u, state or {})))
        log = Path(unit_dir) / "engine.log"
        try:
            fh = open(log, "ab")
        except OSError as e:
            return None, f"cannot open {log}: {e}"
        try:
            # The wrapper records the exit status, because nothing else
            # will: a detached child is reparented to init and its code can
            # never be reaped. Written by OUR wrapper into the exclusive root,
            # not by the engine.
            #
            # SCOPE: this is the FOREGROUND command's status. A command that
            # backgrounds its real work ("engine.sh &") returns 0 immediately
            # and the background failure is invisible here; `wait` cannot
            # recover it portably, since POSIX `wait` with no operands returns
            # 0 regardless. Reviewers were right that the earlier comment
            # claimed to cover "the whole job". It does not, and the receipt
            # now says so. The declared-outputs check still applies, so a
            # false DONE additionally requires the background work to fail
            # AFTER writing every declared output.
            wrapped = (f'(\n{u["command"]}\nrc=$?\nwait\nexit $rc\n)\n'
                       f'printf %s "$?" > {U.ENGINE_RC}\n')
            proc = subprocess.Popen(
                ["sh", "-c", wrapped], cwd=str(unit_dir), env=penv,
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

        # ANCHOR BEFORE DISPATCH. Everything that later judges whether this
        # agent produced anything compares against this record, so it must be
        # written before the agent can run and must not be caller-supplied.
        #
        # `bus await --base HEAD --require-clean` is what unit.py used to
        # delegate this to, and it is not production evidence: the caller
        # picks the base, so HEAD may already have advanced past the work, and
        # a clean tree is clean precisely when nobody touched it. Anchoring to
        # the repository state observed here, before the agent exists, is what
        # makes a later transition mean something.
        anchor_err, anchored_base = _write_launch_record(unit_dir, u)
        if anchor_err:
            return None, anchor_err
        if state is not None and anchored_base:
            # Into COORDINATOR state, from the coordinator's OWN observation,
            # at the moment it made it, keyed BY ATTEMPT. A per-unit key kept
            # the first attempt's base forever, so a retry anchored at a
            # different commit was checked against a stale one and a valid
            # verification for the retry was refused.
            us = state.setdefault("units", {}).setdefault(u["id"], {})
            us.setdefault("attempt_bases", {})[Path(unit_dir).name] = (
                anchored_base)

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


LAUNCH_RECORD = "launch.json"


def _git(repo, *args, timeout=60):
    rc, out, err = U.run(["git", "-C", str(repo)] + list(args), timeout=timeout)
    return rc, (out or "").strip(), (err or "").strip()


def _write_launch_record(unit_dir, u):
    """Capture the repository state BEFORE the agent exists.

    Returns an error string, or None. A code unit that declares a `repo` gets
    anchored; one that does not is recorded as having no repository to judge,
    which is a declaration rather than a silence.

    The record lives beside the attempt, NOT inside the agent's working
    directory: a worker that can rewrite its own baseline can manufacture a
    transition, and the whole point of the anchor is that it cannot.
    """
    # PER ATTEMPT, and outside the attempt directory.
    #
    # It was `<unit>/launch.json`, shared by every attempt of the unit, and
    # written with "x" so a retry left the first attempt's baseline in place.
    # A retry then inherited an anchor from before the previous attempt's
    # commits, so THOSE commits satisfied the new attempt's transition and a
    # retry that produced nothing passed. One anchor per attempt.
    #
    # Still not inside `unit_dir`: that is the agent's --cwd, so a worker
    # could rewrite its own baseline there.
    path = Path(unit_dir).parent / f"launch-{Path(unit_dir).name}.json"
    repo = u.get("repo")
    rec = {"schema_version": 1, "unit": u.get("id"),
           "attempt": Path(unit_dir).name,
           "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    if not repo:
        rec["repo"] = None
        rec["note"] = ("this unit declared no 'repo', so no git transition "
                       "can be judged for it")
    else:
        repo = str(repo)
        if not os.path.isdir(repo):
            return (f"unit {u['id']!r} declares repo {repo!r}, which is "
                    f"not a directory"), None
        rc, top, _ = _git(repo, "rev-parse", "--show-toplevel")
        if rc != 0:
            return (f"unit {u['id']!r} declares repo {repo!r}, which is "
                    f"not a git repository"), None
        rc, head, _ = _git(repo, "rev-parse", "HEAD")
        if rc != 0:
            return (f"unit {u['id']!r}: {repo!r} has no HEAD to anchor to. "
                    f"An empty repository gives nothing to transition FROM."), None
        rc, tree, _ = _git(repo, "rev-parse", "HEAD^{tree}")
        rc2, branch, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        # Exclude the coordinator's own state tree: allocate has already
        # written unit.json under <root>/<unit>/<attempt> by the time we get
        # here, and if the swarm runs inside the repository it works on, that
        # would make every launch look dirty.
        rc3, dirty = W.repo_status(U.run, repo,
                                   exclude=W.swarm_root_of(unit_dir))
        if rc3 != 0:
            return (f"unit {u['id']!r}: cannot read git status in "
                    f"{repo!r}"), None
        # The remote, so a merge receipt naming a DIFFERENT repository can be
        # refused. The receipt says "owner/name"; the anchor knows what this
        # working copy actually pushes to.
        rc4, remote, _ = _git(repo, "remote", "get-url", "origin")
        rec["remote"] = remote if rc4 == 0 else None
        rec.update({"repo": top, "branch": branch if rc2 == 0 else None,
                    "base_commit": head, "base_tree": tree if rc == 0 else None,
                    "clean_at_launch": not dirty,
                    "dirty_paths_at_launch": len(dirty)})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x") as fh:      # x: an anchor is written ONCE
            json.dump(rec, fh, indent=1, sort_keys=True)
            fh.write("\n")
    except FileExistsError:
        # Already anchored, so this is a re-dispatch of the SAME attempt.
        #
        # Do NOT read the base back out of the file. I removed that laundering
        # from the main path and left it right here, which two reviewers found
        # independently: reading an agent-writable value and returning it to
        # be stored as trusted coordinator state is the exact move the trust
        # was meant to prevent, and it does not stop being that because it
        # happens on the second dispatch.
        #
        # The coordinator observed the base the FIRST time and wrote it to
        # state then. If state has it, it stands. If it does not, this attempt
        # cannot be verified, and `verify` says so rather than inventing a
        # base from a file the agent can edit.
        return None, None
    except OSError as exc:
        return f"cannot write the launch record for {u['id']!r}: {exc}", None
    return None, rec.get("base_commit")


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
    "READY_FOR_PR": ("open_pr", "the agent finished and its declared outputs "
                                "exist. That is not done: a code unit is "
                                "closed by a merged pull request."),
}

# WHICH EVIDENCE MAY CLOSE WHICH KIND. Hard-coded and deliberately not
# configurable per plan: a configurable authority is one somebody can
# configure wrong, and the failure is silent -- the tracker would report work
# as verified on evidence that never established it.
#
# A merged PR is the right evidence for CODE and the wrong evidence for a
# 1.42 TiB hash. A predicate receipt is the reverse.
# READY_FOR_PR is deliberately NOT a unit state yet. The closure-by-exclusion
# guard caught me declaring one that nothing produces, which is the same error
# as accepting retry.mode "resume" before cross-attempt handoff exists: a name
# for a mechanism that is not built. Stage 1 needs no new state, because the
# rewrite below turns a code unit's close into open_pr at the point of
# emission. The state arrives with stage 3, alongside the branch and PR flow
# that can actually reach it.
CLOSING_EVIDENCE = {
    "code": "merged_pr",
    "slurm": "predicate_receipt",
    "pipeline": "predicate_receipt",
}


def closing_evidence_for(kind):
    return CLOSING_EVIDENCE.get(kind or "slurm", "predicate_receipt")


def outbox_key(project, uid, state, attempt_dir):
    """Idempotency key. Same project, unit, state and attempt yields the same
    key, so draining twice is a no-op rather than a duplicate issue."""
    basis = f"{project}\x00{uid}\x00{state}\x00{attempt_dir or ''}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def emit_intent(state_dir, project, uid, unit_state, us, evidence=None,
                kind=None):
    """Append one tracker intent. Returns the key, or None if already emitted.

    Deterministic from state: replaying the same transitions produces the same
    keys, which is what makes the drain safe to retry."""
    action = TRACKER_EVENTS.get(unit_state)
    if not action:
        return None
    verb, why = action

    # BELT AND BRACES. The state machine now yields READY_FOR_PR for a code
    # unit rather than DONE, so this should be unreachable; it stays because
    # a close intent for a kind that cannot be closed by a receipt must never
    # exist, however it was reached.
    if verb == "close" and closing_evidence_for(kind) != "predicate_receipt":
        verb = "open_pr"
        why = ("the agent finished and its declared outputs exist, which "
               "makes this READY FOR A PR. It is not done: a code unit is "
               "closed by a merged pull request, and no merge has been seen.")
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
        # Named on every intent so a drainer never has to infer which kind of
        # evidence would justify acting on it.
        "closing_evidence": closing_evidence_for(kind),
        "kind": kind,
        "unit_state": unit_state, "why": why,
        # `applied` is deliberately NOT written any more. It was always
        # false, nothing ever set it true, and a permanently false field reads
        # as "this was not filed" when the truth is "this machine does not
        # know". Status is derived from outbox-receipts.jsonl instead.
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "job_id": us.get("job_id"), "attempt_dir": us.get("attempt_dir"),
        # A close intent MUST carry the verdict that justifies it. A drain that
        # cannot see the evidence must refuse to close.
        "evidence": evidence,
    }
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(intent, sort_keys=True) + "\n")
    except OSError as e:
        print(f"WARNING: could not append a tracker intent: {e}",
              file=sys.stderr)
        return None
    return key


# --- acknowledgment: did the drain actually land? -------------------------
#
# Every intent was written {"applied": false} and NOTHING ever set it true, so
# after a clean run all eight intents still read pending. Re-draining is a
# no-op (idempotent by key), so this was never a correctness bug. It was worse
# in a quieter way: the outbox could not answer the one question it exists to
# answer, and a record that never advances is not a record.
#
# Sol's three corrections to my first design, each of which I had wrong:
#
# 1. Append-only JSONL is NOT automatically crash-safe. A process can die
#    having written half a line. So: flock, fsync, and a DEFINED rule for a
#    truncated tail (drop it, say so) versus a malformed record in the middle
#    (fail closed; that is corruption, not an interrupted write).
#
# 2. Do not store an authoritative `applied: true`. Store a success receipt
#    carrying the tracker's own reference and DERIVE status. An issue id by
#    itself proves nothing: an update or close intent already contains the
#    target id, and holding an id does not establish that the mutation ran.
#    The receipt means "the drainer observed THIS operation succeed and got
#    reference X".
#
# 3. Absence of a receipt is `unacknowledged`, never "not applied". The
#    operation may well have happened and the acknowledgment been lost.
#    Saying "not applied" claims knowledge this machine does not have.
RECEIPTS = "outbox-receipts.jsonl"

# The WIRE VALUES matter as much as the printed ones. Round 2 caught me
# relabelling only the text output: --json still emitted "acknowledged", so a
# machine consumer read an attestation as verified tracker success. The value
# itself now carries the weakness, so both paths say the same thing.
UNACKNOWLEDGED = "unacknowledged"
ACKNOWLEDGED = "attested"
CONFLICT = "conflict"


def _fsync_append(path, record):
    """Append one JSON line durably, serialised against other writers.

    Everything happens under ONE lock on ONE handle. Healing the tail used to
    run before the lock was taken, so two writers could interleave: A reads a
    partial tail, B truncates it and appends its receipt, then A truncates
    using its stale view and deletes B's record before appending its own. The
    repair for one crash silently ate a good receipt.

    The lock is advisory and process-scoped: the kernel drops it when this
    process exits, including when it is killed, so a crash cannot wedge the
    journal.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode()

    # r+b keeps existing content and allows truncate; a+b cannot truncate
    # portably. Create it first if absent.
    if not path.exists():
        path.touch()
    with open(path, "r+b") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise OutboxError(
                f"cannot take an exclusive lock on {path}: {exc}. Receipt "
                f"writes must be serialised, and this filesystem will not "
                f"serialise them, so two drainers could interleave and "
                f"corrupt the journal. Record receipts from a filesystem "
                f"that supports flock.")

        # HEAL, now that nobody else can be writing. A crash can leave the
        # journal ending mid-record with no newline; appending onto that fuses
        # the new receipt to the broken one, turning a recoverable interrupted
        # write into corruption that takes the next record with it. An
        # incomplete record has no meaning, so dropping it loses nothing.
        fh.seek(0, os.SEEK_END)
        if fh.tell():
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.seek(0)
                data = fh.read()
                cut = data.rfind(b"\n")
                fh.truncate(0 if cut < 0 else cut + 1)

        fh.seek(0, os.SEEK_END)
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


class _RawJournal:
    """What the raw reader returns: records you cannot reach by accident.

    Six review findings chased name-level bypasses of a static test (nested
    functions, lambdas, aliases, same-named methods). That arms race is not
    winnable: no static check over a shared module namespace can stop
    deliberate indirection, and a test claiming otherwise is the overclaim
    this repo keeps catching.

    So the barrier moved from the NAME to the DATA. The raw reader hands back
    this box. The records sit behind a private attribute that only
    `load_acknowledgments` opens, and it opens it only after refusing a bad
    journal. A caller who wants them without the check must reach into
    `_records` deliberately, in a line that says exactly what it is doing.

    The threat model is an honest maintainer in a hurry, not an adversary.
    Against a slip this is airtight; against smuggling, nothing here would be.
    """

    __slots__ = ("_records", "problems")

    def __init__(self, records, problems):
        self._records = records
        self.problems = problems


def _read_receipts_raw(state_dir):
    """Parse the journal. PRIVATE: everything goes through the chokepoint.

    Do not call this to decide anything. It reports what it found, including
    what it could not read, and reporting is not refusing. Three review rounds
    found the same bug in four places because each caller decided for itself
    whether a problem mattered, and each new caller forgot again. Use
    `load_acknowledgments`, which cannot be bypassed by forgetting.

    Returns (receipts, problems), where a problem is STRUCTURED.

    Problems used to be prose, and the caller decided whether to fail closed
    by looking for the word "corruption" in it. That is the guard-matching-
    its-own-message bug this repo has now paid for four times: an OSError
    produced "cannot read ...", matched nothing, and the command cheerfully
    reported every intent as unacknowledged and exited zero.

    So the KIND is data:
      truncated_tail  an interrupted write; recoverable by re-draining
      corrupt         a complete but unreadable record; fail closed
      unreadable      the journal itself cannot be read; fail closed
      malformed       parsed as JSON but not a receipt; fail closed
    """
    p = Path(state_dir) / RECEIPTS
    # is_file() answers False both for "not there" and for "cannot look",
    # which are opposite facts: absent means nothing was ever attested,
    # unreadable means we do not know. It can also raise outright on some
    # platforms. Ask to open it and read the errno instead. report.py's copy
    # of this reader was fixed first and this one was left behind, which is
    # the same copy-drift that put the removed `applied` field in two states.
    try:
        with open(p) as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return _RawJournal([], [])
    except IsADirectoryError as exc:
        return _RawJournal(
            [], [{"kind": "unreadable", "detail": f"{p} is not a file: {exc}"}])
    except OSError as exc:
        return _RawJournal(
            [], [{"kind": "unreadable", "detail": f"cannot read {p}: {exc}"}])

    lines = raw_text.splitlines()
    # A final line WITHOUT a trailing newline is an interrupted write. One
    # WITH a trailing newline was written in full, so if it does not parse it
    # is corruption, not a crash. Reviewer round 3 caught this: splitlines()
    # cannot tell the two apart, and every malformed last line was being
    # forgiven.
    complete_tail = raw_text.endswith("\n")

    out, problems = [], []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        is_last = idx == len(lines) - 1
        try:
            rec = json.loads(line)
        except ValueError:
            if is_last and not complete_tail:
                problems.append({
                    "kind": "truncated_tail",
                    "detail": "the last receipt line is truncated, which is "
                              "what an interrupted write looks like. It was "
                              "dropped; re-drain to re-record that key."})
            else:
                problems.append({
                    "kind": "corrupt",
                    "detail": f"receipt line {idx + 1} was written in full "
                              f"and does not parse, so this is corruption "
                              f"rather than an interrupted write."})
            continue
        bad = _receipt_shape_problem(rec)
        if bad:
            problems.append({"kind": "malformed",
                             "detail": f"receipt line {idx + 1}: {bad}"})
            continue
        out.append(rec)
    return _RawJournal(out, problems)


def _receipt_shape_problem(rec):
    """Valid JSON is not a valid receipt.

    Round 3: {"key": "k1", "ref": "ARC-1", "attested": false} parsed fine and
    produced an ATTESTED status, because nothing looked past json.loads."""
    if not isinstance(rec, dict):
        return "not an object"
    if not str(rec.get("key") or "").strip():
        return "no key"
    if not str(rec.get("ref") or "").strip():
        return "no tracker ref"
    if rec.get("attested") is not True:
        return ("attested is not true, so this record does not assert that "
                "anything succeeded")
    return None


def fatal_problems(problems):
    """Everything except an interrupted tail. Structured, never grepped."""
    return [p for p in problems if p.get("kind") != "truncated_tail"]


def load_acknowledgments(state_dir):
    """THE ONLY WAY to read the receipt journal. Raises OutboxError.

    Three rounds of review found the same defect in four different places: a
    complete-but-malformed final line, `--record-receipt` appending to a
    corrupt journal, a record that parsed but asserted nothing, and a
    fail-closed test that grepped its own prose. None of those were four bugs.
    They were one bug wearing four hats: detection lived here and the decision
    to refuse lived in each caller, so every caller got to be wrong
    separately, and every NEW caller got a fresh chance to be wrong.

    The fix is not another check. It is that reading this journal and refusing
    a bad one are now the same operation, so a future caller cannot obtain the
    records without also accepting the refusal. `test_ack` asserts
    structurally that no other function touches RECEIPTS.
    """
    journal = _read_receipts_raw(state_dir)
    problems = journal.problems
    fatal = fatal_problems(problems)
    if fatal:
        raise OutboxError(
            "the receipt journal cannot be read in full, so no acknowledgment "
            "status derived from it can be trusted:\n" +
            "\n".join(f"  [{f['kind']}] {f['detail']}" for f in fatal) +
            "\n  Repair or remove it, then re-record: intents are keyed, so "
            "re-recording is safe.")
    # Opened ONLY here, and only after the refusal above.
    return journal._records, problems


def record_receipt(state_dir, key, ref, op=None, by=None, at=None):
    """Record the drainer's ATTESTATION that this operation succeeded.

    Read that word carefully, because a reviewer caught me overclaiming here.
    This is NOT verified evidence and cannot be. The coordinator has no
    network imports, so it cannot ask the tracker whether ARC-171 really
    closed; anyone who can run this command can write any reference they like.
    What the record establishes is only: this identified writer, at this time,
    said this operation succeeded and returned this reference.

    That is the same class of claim as an agent reporting "done", and this
    repo exists to refuse exactly that when it is dressed up as proof. So the
    mechanism stays (there is no other one available across the network gap)
    and the LABEL carries the weakness: every display says attested, never
    verified. A reader can then go and check the reference by hand, which is
    the only thing that would settle it.

    Written only after the tracker confirms to the drainer. A false
    attestation is strictly worse than a missing one: re-draining is safe,
    un-filing is not."""
    load_acknowledgments(state_dir)      # refuse to extend a broken journal
    # Validated HERE, not only in the CLI. A direct caller passing ref=None
    # used to store the literal string "None", and a whitespace-only ref
    # passed a truthiness check and poisoned the journal later.
    key = str(key or "").strip()
    ref = str(ref if ref is not None else "").strip()
    if not key:
        raise OutboxError("a receipt needs the intent key it acknowledges")
    if not ref:
        raise OutboxError(
            "a receipt needs the tracker's own reference. Without one there "
            "is nothing to check by hand later, and checking by hand is the "
            "only thing that ever settles an attestation.")
    rec = {"key": key, "ref": ref, "op": op, "attested": True,
           "by": by or os.environ.get("USER") or "?",
           "at": at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "schema_version": 1}
    _fsync_append(Path(state_dir) / RECEIPTS, rec)
    return rec


def acknowledgment_status(state_dir):
    """Map key -> (status, receipts), plus problems. Derived, never stored.

    CALLERS MUST FAIL CLOSED ON `problems`. Detecting corruption and then
    deriving status from whatever survived is not a safeguard: a journal that
    lost a line reports the intents it can still see as attested, and the one
    it lost as unacknowledged, which is precisely the false-negative-turned-
    false-positive this check exists to prevent."""
    receipts, problems = load_acknowledgments(state_dir)
    by_key = {}
    for r in receipts:
        by_key.setdefault(r.get("key"), []).append(r)
    status = {}
    for key, rs in by_key.items():
        refs = {r.get("ref") for r in rs}
        status[key] = (CONFLICT if len(refs) > 1 else ACKNOWLEDGED, rs)
    return status, problems


# --- merge attestation: how a code unit closes -----------------------------
#
# Closure authority for `code` is a merged PR, and the coordinator has no
# network imports, so it cannot ask GitHub anything. The evidence therefore
# arrives the way tracker acknowledgment does: a session that CAN reach the
# network records what it observed.
#
# That would be worthless on its own, because an attester naming any commit
# could close any unit. What makes it admissible is the BINDING: the head the
# attestation pins must equal the head this coordinator independently judged
# as produced, from an anchor written before the agent existed. The attester
# can lie about whether a PR merged. It cannot make this unit's produced
# commit be some other commit.
MERGE_RECEIPTS = "merge-receipts.jsonl"

# Every method changes the resulting commit differently, and an unrecorded
# method means `merged_as` cannot be interpreted at all. Fail closed rather
# than approximate head identity.
MERGE_METHODS = ("merge", "squash", "rebase")

_MERGE_REQUIRED = ("unit", "repo", "pr", "target", "head", "merged_as",
                   "method")


def _merge_shape_problem(rec):
    if not isinstance(rec, dict):
        return "not an object"
    for field in _MERGE_REQUIRED:
        if not str(rec.get(field) or "").strip():
            return f"no {field}"
    # NOT checked here: whether `merged` is true. A record saying a PR is
    # still open is a legitimate observation somebody may want on file, and
    # treating it as corruption would make the whole journal unreadable over
    # a record that is merely uninteresting. Shape answers "can this be
    # read"; admission answers "does this close the unit".
    if rec.get("method") not in MERGE_METHODS:
        return (f"method {rec.get('method')!r} is not one of "
                f"{', '.join(MERGE_METHODS)}; an unrecognised method means "
                f"`merged_as` cannot be interpreted")
    return None


def _read_merge_raw(state_dir):
    p = Path(state_dir) / MERGE_RECEIPTS
    try:
        with open(p) as fh:
            raw = fh.read()
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [{"kind": "unreadable", "detail": f"cannot read {p}: {exc}"}]
    lines = raw.splitlines()
    complete_tail = raw.endswith("\n")
    out, problems = [], []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        last = idx == len(lines) - 1
        try:
            rec = json.loads(line)
        except ValueError:
            if last and not complete_tail:
                problems.append({"kind": "truncated_tail",
                                 "detail": "the last merge receipt is a "
                                           "half-written line; re-record it"})
            else:
                problems.append({"kind": "corrupt",
                                 "detail": f"merge receipt line {idx + 1} was "
                                           f"written in full and does not "
                                           f"parse"})
            continue
        bad = _merge_shape_problem(rec)
        if bad:
            problems.append({"kind": "malformed",
                             "detail": f"merge receipt line {idx + 1}: {bad}"})
            continue
        out.append(rec)
    return out, problems


def load_merge_receipts(state_dir):
    """THE ONLY way to read merge receipts. Raises OutboxError.

    Same chokepoint discipline as the acknowledgment journal, for the same
    reason: detection in the reader and the decision to refuse in each caller
    is how one bug appeared in four places.
    """
    recs, problems = _read_merge_raw(state_dir)
    fatal = fatal_problems(problems)
    if fatal:
        raise OutboxError(
            "the merge receipt journal cannot be read in full, so no closure "
            "derived from it can be trusted:\n" +
            "\n".join(f"  [{f['kind']}] {f['detail']}" for f in fatal))
    return recs, problems


def _same_repo(a, b):
    """Do two repository names refer to the same place?

    Compares the last two path segments, so `git@github.com:o/r.git`,
    `https://github.com/o/r` and `o/r` all match. Anything that cannot be
    reduced to owner/name compares literally rather than being waved through.
    """
    def split(x):
        s = str(x or "").strip().rstrip("/")
        if s.endswith(".git"):
            s = s[:-4]
        for scheme in ("https://", "http://", "ssh://", "git://"):
            if s.lower().startswith(scheme):
                s = s[len(scheme):]
                break
        if "@" in s.split("/")[0]:
            s = s.split("@", 1)[1]
        parts = [p for p in s.replace(":", "/").split("/") if p]
        if len(parts) < 2:
            return None, s.lower()
        name = "/".join(parts[-2:]).lower()
        # The host is the FIRST segment, never parts[-3]. Counting back from
        # the end let https://evil.example/github.com/acme/app present
        # "github.com" as its host, which is precisely the lookalike this
        # check exists to catch. A bare "owner/name" has no host at all.
        host = parts[0].lower() if len(parts) > 2 else None
        return host, name

    ha, na = split(a)
    hb, nb = split(b)
    if not a or not b or na != nb:
        return False
    # The HOST is part of a repository's identity. Reducing to owner/name
    # alone let github.com/acme/app and evil.example:acme/app compare equal,
    # so a PR in a lookalike repository could close a unit. Compared only
    # when both sides carry one: a bare "owner/name" receipt is a legitimate
    # shorthand, not a mismatch.
    # BOTH parts, including both being absent. Comparing hosts only when
    # each side had one let a hostless anchor (`acme/app`) match a
    # host-bearing lookalike (`https://evil.example/acme/app`). Accepting
    # shorthand is what the hole was made of, so it is not accepted: two names
    # for the same repository must agree on where it lives.
    return ha == hb


VERIFY_RECEIPTS = "verify-receipts.jsonl"

_VERIFY_REQUIRED = ("unit", "claim", "verifier", "verifier_sha256",
                    "policy_sha256", "subject_head", "result")


def _verify_shape_problem(rec):
    if not isinstance(rec, dict):
        return "not an object"
    for f in _VERIFY_REQUIRED:
        if not str(rec.get(f) or "").strip():
            return f"no {f}"
    if rec.get("result") not in ("pass", "fail"):
        return f"result {rec.get('result')!r} is not 'pass' or 'fail'"
    return None


def _read_verify_raw(state_dir):
    p = Path(state_dir) / VERIFY_RECEIPTS
    try:
        with open(p) as fh:
            raw = fh.read()
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [{"kind": "unreadable", "detail": f"cannot read {p}: {exc}"}]
    lines = raw.splitlines()
    complete = raw.endswith("\n")
    out, problems = [], []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        last = idx == len(lines) - 1
        try:
            rec = json.loads(line)
        except ValueError:
            problems.append({"kind": "truncated_tail" if (last and not complete)
                             else "corrupt",
                             "detail": f"verification receipt line {idx + 1}"})
            continue
        bad = _verify_shape_problem(rec)
        if bad:
            problems.append({"kind": "malformed",
                             "detail": f"verification receipt line "
                                       f"{idx + 1}: {bad}"})
            continue
        out.append(rec)
    return out, problems


def load_verifications(state_dir):
    """THE ONLY way to read verification receipts. Raises OutboxError."""
    recs, problems = _read_verify_raw(state_dir)
    fatal = fatal_problems(problems)
    if fatal:
        raise OutboxError(
            "the verification journal cannot be read in full, so no claim "
            "derived from it can be trusted:\n" +
            "\n".join(f"  [{f['kind']}] {f['detail']}" for f in fatal))
    return recs, problems


def admit_verification(state_dir, unit, claim, produced, policy_digest,
                       policy):
    """(receipt, refusal) for one required claim.

    Four bindings, and all of them must hold. Any one missing turns the
    verifier's word into a self-report with extra steps:

      subject   the head it verified is the head this attempt produced
      policy    the authorization it ran under is the one at the anchored base
      claim     it is the claim the unit requires
      result    it passed
    """
    if not produced:
        return None, ("this attempt has no produced commit, so there is "
                      "nothing a verification could be about")
    if not isinstance(policy, dict):
        return None, ("no anchored policy was supplied to admission, so the "
                      "verifier a receipt names cannot be checked against "
                      "anything. Refusing rather than taking the receipt's "
                      "word for which verifier ran.")
    recs, _p = load_verifications(state_dir)
    mine = [r for r in recs if r.get("unit") == unit
            and r.get("claim") == claim]
    if not mine:
        return None, (f"no verification receipt for {unit!r} claiming "
                      f"{claim!r}, which the unit declares it requires. Run "
                      f"it:\n  swarm.py verify --unit {unit} --claim {claim} "
                      f"--verifier NAME --path PATH")
    stale, wrong_policy, failed, unauthorized = [], [], [], []
    for r in mine:
        if str(r.get("subject_head")) != str(produced):
            stale.append(str(r.get("subject_head"))[:12])
            continue
        if policy_digest and r.get("policy_sha256") != policy_digest:
            wrong_policy.append(str(r.get("policy_sha256"))[:12])
            continue
        if r.get("result") != "pass":
            failed.append(r)
            continue
        # RE-CHECK the verifier against the policy, here, at admission, with
        # NO way to skip it. `if policy is not None` made the whole check
        # optional: a caller that passed nothing got the receipt's own word
        # about which verifier ran, which is the self-authorization this
        # exists to refuse. The argument is required and None is refused.
        entry, refusal = V.authorized(policy, r.get("verifier"),
                                      r.get("verifier_sha256"), claim)
        if refusal:
            unauthorized.append(refusal)
            continue
        return r, None
    if failed:
        return None, (f"the verifier ran against the produced commit and "
                      f"returned FAIL for {claim!r}. That is a result, not a "
                      f"missing receipt: fix the work rather than re-running "
                      f"until it passes.")
    if unauthorized:
        return None, (f"a verification receipt for {unit!r} is bound to the "
                      f"right head and policy, but the verifier it names is "
                      f"not authorized by that policy: {unauthorized[0]}")
    if wrong_policy:
        return None, (f"verification for {unit!r} ran under policy "
                      f"{', '.join(sorted(set(wrong_policy)))}, but this "
                      f"attempt was anchored to {str(policy_digest)[:12]}. "
                      f"The rules changed after the check.")
    return None, (f"verification for {unit!r} names head(s) "
                  f"{', '.join(sorted(set(stale)))}, but this attempt "
                  f"produced {produced[:12]}. A pass for another commit is "
                  f"not a pass for this one.")


def _receipt_reason(attempt_dir):
    """The machine-readable REASON a check came back INCOMPLETE."""
    rp, _ = U.read_json(Path(attempt_dir) / "receipt.json")
    for note in ((rp or {}).get("notes") or []):
        if str(note).startswith("REASON="):
            return str(note).split("=", 1)[1]
    return ""


def maybe_continue(state_dir, uid, u, us, report):
    """Send a bounded continuation to a code agent that settled empty-handed.

    NOT a unit state, NOT a retry mode, and there is deliberately no Slurm
    analogue. A conversational turn is not a retry boundary: a planning-only
    turn can settle while the session, worktree, launch identity, attempt root
    and budget all remain valid, so minting a fresh attempt would discard the
    context and spend a retry on what is really a provider liveness defect.

    So this stays INSIDE the attempt: same attempt id, same write root, same
    anchor. What it must never become is a correction loop. It answers exactly
    one condition, "settled and produced nothing", and the bound is declared
    in the plan rather than passed by whoever happens to run advance.

    Returns True if a continuation was sent.
    """
    cfg = u.get("continuation") or {}
    limit = cfg.get("max") or 0
    if not limit:
        return False
    if u.get("kind") != "code":
        return False
    # ONLY this reason. A permission block is NEEDS_HUMAN and a person must
    # answer it; a failure is FAILED and prodding it repeats the failure; a
    # verifier rejection is a correction loop, which is a different thing
    # wearing the same clothes.
    if _receipt_reason(us.get("attempt_dir") or "") != U.REASON_NO_OUTPUTS:
        return False
    used = len(us.get("continuations") or [])
    if used >= limit:
        return False
    agent = us.get("job_id")
    if not agent:
        return False

    prompt = str(cfg.get("prompt") or
                 "Your turn ended without producing the declared outputs. "
                 "Continue the work you planned.")
    rc, out, err = U.run(["paseo", "send", str(agent), prompt], timeout=120)
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "n": used + 1, "of": limit,
             "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()[:16],
             "sent": rc == 0}
    if rc != 0:
        entry["error"] = (err or out or "").strip()[:200]
    us.setdefault("continuations", []).append(entry)
    if rc != 0:
        report.append(f"{uid}: continuation {used + 1}/{limit} could not be "
                      f"sent: {entry['error']}")
        return False
    us["state"] = "RUNNING"
    report.append(
        f"{uid}: settled without producing; sent continuation "
        f"{used + 1}/{limit} to agent {agent} in the SAME attempt. After "
        f"{limit} the unit fails for missing production evidence.")
    return True


def trusted_base(state, unit, attempt_dir):
    """THE ONLY way to obtain the base commit an attempt was anchored to.

    Three rounds of review found the same defect in three places, each time
    because some caller reached for the base wherever it was handy. The launch
    record is the handiest place and the wrong one: it sits on a filesystem
    the agent's Unix user can write, so reading it and then treating the value
    as trusted launders exactly what the trust was for.

    So there is one function, it reads ONLY coordinator state, and it has no
    fallback. Returning None means "this attempt cannot be verified", which is
    an honest answer; inventing a base from a file the agent can edit is not.
    A test asserts nothing else reads base_commit out of a launch record.
    """
    if not attempt_dir:
        return None
    us = (state.get("units") or {}).get(unit) or {}
    return (us.get("attempt_bases") or {}).get(Path(attempt_dir).name)


def _head_from_receipt(attempt_dir):
    """The head recorded when the attempt was JUDGED.

    Preferred over asking the repository again: the receipt was written at the
    moment the judgment was true, and the repository has been under the
    agent's control ever since.
    """
    if not attempt_dir:
        return None
    try:
        with open(Path(attempt_dir) / U.RECEIPT) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    return (rec.get("basis") or {}).get("produced_head")


def admit_merge(state_dir, unit, produced, expect_repo=None):
    """(receipt, refusal). A merged-PR attestation, admitted or refused.

    `produced` is the commit this coordinator judged the attempt to have
    produced. An attestation that pins a different head is refused: that is
    the whole binding, and without it any attester closes any unit.
    """
    if not produced:
        return None, ("this attempt has no produced commit, so there is "
                      "nothing a merge could be bound to. A unit reaches "
                      "READY_FOR_PR only after producing one.")
    recs, _problems = load_merge_receipts(state_dir)
    mine = [r for r in recs if r.get("unit") == unit
            and r.get("merged") is True]
    if not mine:
        return None, (f"no merge receipt for {unit!r}. Record one from a "
                      f"machine that can see the PR:\n  swarm.py merge "
                      f"--unit {unit} --pr URL --head {produced[:12]} "
                      f"--target BRANCH --merged-as SHA --method merge")
    wrong_repo = []
    for r in mine:
        if str(r.get("head")) != str(produced):
            continue
        if expect_repo and not _same_repo(r.get("repo"), expect_repo):
            # Collect and keep looking. Returning on the first mismatch let a
            # single wrong-repository receipt mask every correct one appended
            # after it, so one attester slip parked the unit forever.
            wrong_repo.append(str(r.get("repo")))
            continue
        return r, None
    if wrong_repo:
        return None, (
            f"{len(wrong_repo)} merge receipt(s) for {unit!r} pin the right "
            f"head but name repositor(y/ies) {', '.join(sorted(set(wrong_repo)))}, "
            f"and this attempt was anchored to {expect_repo!r}. The attester "
            f"is trusted to report what it saw, not to decide which "
            f"repository this unit belongs to. Record a corrected receipt; "
            f"the wrong one does not block it.")
    heads = ", ".join(sorted({str(r.get("head"))[:12] for r in mine}))
    return None, (
        f"{len(mine)} merge receipt(s) for {unit!r} pin head(s) {heads}, but "
        f"this attempt produced {produced[:12]}. A merge of something else "
        f"does not close this unit, and re-pointing the receipt would defeat "
        f"the only check the coordinator can make without a network.")


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
    # Scan EVERY candidate brace, not just the first. paseo's preamble can
    # contain one ("Tip: reuse with --workspace {id}"), and starting there
    # parsed "{id}", failed, and left a launched agent unbound.
    for i, ch in enumerate(out):
        if ch == "{":
            got = _balanced_from(out, i)
            if got is not None:
                return got
    return None


def _balanced_from(out, i):
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
            # EXACT trailing match. `attempt_id in name` also matched an
            # attempt whose id is a prefix of another's, binding a unit to
            # somebody else's agent.
            for a in json.loads(out or "[]"):
                name = str(a.get("name") or "")
                if name.split()[-1:] == [attempt_id]:
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

    # A DRY RUN MUST NOT CONTAMINATE A REAL PROJECT. Recording a fake
    # `dry-...` job id into a live state directory wedges that unit forever:
    # the reconcile net skips anything holding a job id, so the attempt can
    # never be judged and never re-dispatched. A reviewer found this weeks
    # ago; I recorded it and did not fix it, and it then bit the first real
    # user on their first run, whose only way out was to discard all state.
    if dry_run:
        # LIVE attempts only. A terminal unit cannot be contaminated, and
        # refusing because a DONE unit still records the job id that finished
        # it would block every dry run for the rest of a project's life.
        real = sorted(uid for uid in units
                      if (_unit_state(state, uid).get("job_id")
                          and not str(_unit_state(state, uid)["job_id"])
                          .startswith("dry-")
                          and _unit_state(state, uid).get("state")
                          in LIVE_STATES))
        if real:
            return ([f"REFUSING to dry-run against a state directory that "
                     f"holds REAL attempts ({', '.join(real)}).",
                     f"A dry run records placeholder job ids, and a unit that "
                     f"has one can never be judged or re-dispatched, so this "
                     f"would wedge work that is genuinely running.",
                     f"Use a throwaway state directory instead:",
                     f"    swarm.py run <plan> --dry-run --state-dir "
                     f"$(mktemp -d)/state --root $(mktemp -d)/runs"], 0, None)
    before_states = {uid: (_unit_state(state, uid) or {}).get("state")
                     for uid in units}

    # NORMALISE PERSISTED STATE FIRST. Converting DONE to READY_FOR_PR only in
    # the fresh-check path left every ALREADY-persisted DONE untouched: a
    # state file written before this rule existed, or a unit whose kind
    # changed from slurm to code, kept a DONE that the re-check loop skips by
    # design. Its dependents then dispatched on evidence this system says
    # cannot close a code unit. Three reviewers found it independently, and it
    # is the third time in a row I have fixed a forward path and left the
    # stored state alone.
    for uid, u in sorted(units.items()):
        us = _unit_state(state, uid)
        if (us.get("state") == "DONE"
                and closing_evidence_for(u.get("kind")) != "predicate_receipt"):
            us["state"] = "READY_FOR_PR"
            report.append(
                f"{uid}: recorded DONE, but a {u.get('kind')} unit is closed "
                f"by a merged pull request, not by its own receipt. Corrected "
                f"to READY_FOR_PR; anything depending on it waits.")
            save_state(state_dir, state)

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
        if str(us.get("job_id") or "").startswith("dry-"):
            # A placeholder from an earlier dry run. Nothing was ever
            # submitted, so there is nothing to recover and nothing to lose:
            # release it so the unit can dispatch for real. Previously this
            # was indistinguishable from a bound job and wedged the unit.
            report.append(f"{uid}: clearing a dry-run placeholder "
                          f"({us['job_id']}); nothing was ever submitted for "
                          f"it, so the unit will dispatch normally.")
            us["job_id"] = None
            us["attempt_dir"] = None
            us["state"] = None
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
        # None is "cannot tell" and must NOT be treated as loss: one
        # transient NFS read used to halt a healthy project until a human
        # edited durable state. Only an explicit False stops us.
        if not dry_run and renew_lease(state_dir) is False:
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
        # A CODE UNIT IS NOT DONE WHEN ITS PREDICATE PASSES. The receipt says
        # an agent went idle and files exist; the accepted form of that work
        # is a merged PR. Rewriting only the tracker intent left the unit
        # DONE in durable state, so dependents dispatched before any merge and
        # the DAG contradicted the tracker -- the fix was cosmetic. Found by a
        # reviewer.
        #
        # KNOWN LIMIT, stated rather than hidden: nothing records a merge yet,
        # so a code unit stays READY_FOR_PR and anything depending on it
        # waits. That is honest until stage 3 exists.
        # READY_FOR_PR is included deliberately. Guarding on DONE alone made
        # the state a dead end: the first advance moved a produced unit to
        # READY_FOR_PR, and a receipt recorded afterwards was never looked at
        # again, so the unit could never close no matter what the attester
        # did. Recording evidence after the fact is the NORMAL order here.
        # CLOSED IS CLOSED. Adding READY_FOR_PR to this guard, to fix a unit
        # that could never leave it, created the opposite defect: a unit that
        # HAD closed on an admitted merge was re-judged on every advance, so
        # the agent dirtying its repository afterwards drove a DONE unit back
        # to READY_FOR_PR. Closure is not a property of the repository's
        # current state; it is a fact about evidence already admitted.
        if (us["state"] in ("DONE", "READY_FOR_PR")
                and not us.get("merged_as")
                and closing_evidence_for(u.get("kind")) != "predicate_receipt"):
            # STAGE 3. A produced tree is not a closed unit: `code` is closed
            # by a merged PR. The attestation is admitted only when the head
            # it pins is the one this attempt produced, which is the single
            # check available to a coordinator with no network.
            attempt = us.get("attempt_dir") or ""
            # RECORD IT ONCE. Re-deriving the produced head on every advance
            # asks a repository the agent owns what it produced, and the
            # answer changes: validate A, park in READY_FOR_PR, let the agent
            # move the branch to C, and a receipt for the genuinely produced A
            # is then refused because the question was asked again. Every
            # finding across three review rounds has been a version of this,
            # so the fact is captured when it is true and read thereafter.
            produced = us.get("produced_head") or _head_from_receipt(attempt)
            if not produced:
                produced = W.produced_head(U.run, attempt, u)
            if produced:
                us["produced_head"] = produced
            anchor_rec, _ = W.read_launch_record(attempt) if attempt else (None,
                                                                           None)
            # DELIBERATELY NOT copied from the launch record here. Reading
            # the base out of an agent-writable file and then storing it as
            # trusted state launders exactly the value the trust was meant to
            # protect: state would agree with the record because it was copied
            # FROM the record. The base is written into state at dispatch, by
            # the coordinator, from its own observation of the repository. If
            # it is absent here, the attempt predates that and cannot be
            # verified, which `verify` reports rather than papering over.

            # A DECLARED verification requirement gates closure. Undeclared,
            # nothing changes: a unit that never asked for a verifier is not
            # improved by demanding one, and a requirement everybody must
            # satisfy is one everybody learns to satisfy trivially.
            vrefusal = None
            required = u.get("requires_verification") or []
            policy_digest, perr, _pol = None, None, None
            if required:
                base = trusted_base(state, uid, attempt)
                if anchor_rec and anchor_rec.get("repo"):
                    _pol, policy_digest, perr = V.read_policy(
                        U.run, anchor_rec["repo"], base)
                else:
                    perr = "this attempt anchored no repository"
            for claim in required:
                # A policy that cannot be READ must refuse. Leaving the digest
                # None skipped the comparison entirely, so a receipt recorded
                # under any rules at all was admitted the moment `git show`
                # failed. An unreadable authorization source is the strongest
                # reason to refuse, not a reason to stop checking.
                if perr:
                    vrefusal = (f"{uid} requires verification of {claim!r}, "
                                f"and the authorizing policy cannot be read: "
                                f"{perr}")
                    break
                _vr, vrefusal = admit_verification(
                    state_dir, uid, claim, produced, policy_digest,
                    policy=_pol)
                if vrefusal:
                    break

            receipt, refusal = (None, vrefusal) if vrefusal else admit_merge(
                state_dir, uid, produced,
                expect_repo=(anchor_rec or {}).get("remote"))
            if receipt:
                us["state"] = "DONE"
                us["merged_as"] = receipt.get("merged_as")
                us["merge_pr"] = receipt.get("pr")
                report.append(
                    f"{uid}: DONE on a merged PR ({receipt.get('pr')}, "
                    f"{receipt.get('method')} as "
                    f"{str(receipt.get('merged_as'))[:12]}). The merge itself "
                    f"is attested, not verified; the head it pins was "
                    f"produced by this attempt.")
            else:
                us["state"] = "READY_FOR_PR"
                us["merge_refusal"] = refusal
                report.append(f"{uid}: READY_FOR_PR. {refusal}")
        report.append(f"{uid}: {us['state']}")

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
                waited = int(time.time() - float(first))
                # TWO DIFFERENT FAILURES, and they need opposite actions.
                # Calling both "the evidence never arrived" sent an operator
                # to `sacct` for a job whose sacct row says COMPLETED 0:0 --
                # the one place that hides the problem, and precisely the
                # confusion this whole tool exists to prevent.
                rp, _ = U.read_json(Path(us["attempt_dir"]) / "receipt.json")
                reason = ""
                for note in ((rp or {}).get("notes") or []):
                    if str(note).startswith("REASON="):
                        reason = str(note).split("=", 1)[1]
                if (reason == U.REASON_NO_OUTPUTS
                        and maybe_continue(state_dir, uid, u, us, report)):
                    save_state(state_dir, state)
                    continue
                if reason == U.REASON_NO_OUTPUTS:
                    used = len(us.get("continuations") or [])
                    if used:
                        report.append(
                            f"{uid}: {used} continuation(s) sent and it still "
                            f"produced nothing. The bound is the point: this "
                            f"fails for missing production evidence rather "
                            f"than being prodded again.")
                    us["state"] = "FAILED"
                    report.append(
                        f"{uid}: the job finished cleanly and its declared "
                        f"outputs never appeared, {waited}s on. This is a "
                        f"FAILED unit, not missing evidence: the scheduler "
                        f"will tell you it succeeded. Read the job's own log "
                        f"in {us['attempt_dir']}.")
                else:
                    us["state"] = "FAILED_EVIDENCE"
                    report.append(
                        f"{uid}: no verdict {waited}s after the first "
                        f"INCOMPLETE, past the {SETTLE_S}s accounting settle "
                        f"window. Treating as terminal: the evidence never "
                        f"arrived. Check `sacct -j {us.get('job_id')}` by "
                        f"hand.")
        else:
            us.pop("incomplete_since", None)
        if rc in RETRYABLE:
            policy = u.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
            # Count REAL attempts only. A dry run appends to this list, and
            # this list is the retry budget, so every dry run silently stole
            # one of the retries the plan had declared: a unit promised two
            # attempts got one, and two prior dry runs left a three-attempt
            # unit with none. Found by a reviewer as an interaction between
            # two changes that were each correct alone.
            real_attempts = [a for a in us["attempts"]
                             if not str(a).startswith(DRY_PREFIX)]
            if len(real_attempts) < policy:
                # A retry mints a NEW write root. Reusing one is precisely what
                # makes the predicate inconclusive.
                us["attempt_dir"] = None
                report.append(f"{uid}: preempted, will re-attempt "
                              f"({len(real_attempts)}/{policy})")
            else:
                us["state"] = "FAILED"
                if policy == 1 and "max_attempts" not in u:
                    # The default is now 1, so a SINGLE preemption ends the
                    # unit. "preempted 1 times, giving up" reads like a bug
                    # rather than a policy, so say which policy and how to
                    # change it deliberately. Retrying costs a full redo, and
                    # that is the decision the plan has to make explicitly.
                    report.append(
                        f"{uid}: preempted once and not retried, because "
                        f"max_attempts defaults to 1. A retry starts in a "
                        f"FRESH EMPTY directory and redoes the whole unit, so "
                        f"repetition is opt-in: set max_attempts with a "
                        f"'retry' contract stating what one interruption "
                        f"costs, or split the unit smaller.")
                elif policy == 1:
                    report.append(
                        f"{uid}: preempted, and the plan allows one attempt, "
                        f"so it is not retried. Raise max_attempts with a "
                        f"'retry' contract, or split the unit smaller.")
                else:
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

        # BOUND LIVE CONCURRENCY, counted across every attempt currently on
        # the cluster rather than per invocation. --max-new-dispatches limits
        # ONE run, and cron adds another batch on its next pass, so it never
        # bounded the total. This matters most immediately after splitting a
        # unit for retry safety: sixteen shards that are individually
        # recoverable are also sixteen simultaneous readers of a filesystem
        # shared by everyone else.
        live = [x for x in units
                if _unit_state(state, x).get("state") in LIVE_STATES]
        cap_all = (plan.get("limits") or {}).get("max_running")
        if cap_all is not None and len(live) >= cap_all:
            report.append(f"{uid}: waiting, {len(live)} of {cap_all} slots in "
                          f"use")
            continue
        pool = u.get("pool")
        if pool:
            caps = ((plan.get("limits") or {}).get("pools") or {})
            in_pool = [x for x in live if units[x].get("pool") == pool]
            if len(in_pool) >= caps.get(pool, 10**9):
                report.append(f"{uid}: waiting, pool {pool!r} full "
                              f"({len(in_pool)} of {caps[pool]})")
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
        # A dry attempt is TAGGED, so it can never be mistaken for real work
        # when counting the retry budget.
        us["attempts"].append(f"{DRY_PREFIX}{unit_dir}" if dry_run
                              else unit_dir)
        us["state"] = "ALLOCATED"
        us["allocated_at"] = time.time()
        # ACCUMULATE. Overwriting meant a unit preempted twice was charged
        # once, so retries could walk straight through a ceiling: budget 8,
        # three 4-hour attempts, 12 committed.
        us["gpu_hours"] = float(us.get("gpu_hours") or 0) + want
        spent += want
        save_state(state_dir, state)

        job_id, err = _submit(u, unit_dir, dry_run, state)
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
        dispatched += 1
        report.append(f"{uid}: submitted {job_id} -> {unit_dir}")
        save_state(state_dir, state)

    state["halted"] = halted
    save_state(state_dir, state)

    # Emit tracker intents from the FINAL state of the advance, not at each
    # place a state happens to be set. Three sites used to emit, and they
    # missed every transition made later in the same pass: a unit that exited
    # 0 and wrote nothing reached FAILED through the settle branch and told
    # the tracker NOTHING, so its issue would have sat on "work started"
    # forever. Comparing before-and-after cannot miss a path, including paths
    # added later.
    project = plan.get("name") or "swarm"
    for uid in sorted(units):
        us = _unit_state(state, uid)
        now = us.get("state")
        if not now or now == before_states.get(uid):
            continue
        evidence = None
        if now == "DONE" and us.get("attempt_dir"):
            # The verdict itself, so a drain never closes on a self-report.
            rp, _ = U.read_json(Path(us["attempt_dir"]) / "receipt.json")
            evidence = {"receipt": rp} if rp else None
        emit_intent(state_dir, project, uid, now, us, evidence,
                    kind=(units.get(uid) or {}).get("kind"))
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
    # NAME WHAT WAS NOT VERIFIED, so silence is never read as approval. A real
    # run declared cpu_preemptible with 32 CPUs in its prose, ran on the
    # default partition with 2, and this printed "plan is valid" -- because
    # the partition check reads u["sbatch"], which was empty, so it examined
    # nothing and said nothing.
    # A DEFAULT CHANGED UNDER EXISTING PLANS. max_attempts was 3 and is now
    # 1, so a plan that still validates may behave differently than it used
    # to. Say which units that applies to rather than letting it be
    # discovered by a preemption.
    once = summary.get("default_attempts") or []
    if once:
        print(f"  retry policy: {len(once)} unit(s) declare no max_attempts "
              f"and will make ONE attempt each. A retry starts in a fresh "
              f"empty directory, so repetition is opt-in: declare "
              f"max_attempts with a 'retry' contract if a redo is acceptable.")

    missing = summary.get("without_partition") or []
    if missing:
        known = sorted(_known_partitions() or [])
        print(f"  NOT CHECKED: {len(missing)} slurm unit(s) declare no "
              f"partition, so nothing here verified where they run: "
              f"{', '.join(missing)}")
        print(f"  They will use this cluster's DEFAULT partition"
              + (f", of {len(known)} available" if known else "")
              + ". If the plan's prose names a partition, it is describing "
                "something this file does not request.")
    return EXIT_OK


def cmd_run(args):
    """Dispatch what is ready, then EXIT. Does not babysit."""
    plan = _load_plan(args.plan)
    # ONE WRITER. Two schedulers firing at once, or a human running `advance`
    # while cron does, would both read old state and submit the same unit.
    ok, holder = acquire_lease(args.state_dir)
    if not ok:
        print(f"cannot take this project's lock: {holder}")
        # The old text here said "pass --force if you are certain it is dead"
        # and "a stale lease expires on its own after 900s". Both became false
        # when the lock moved to the kernel: --force was accepted and silently
        # ignored, and there is no expiry to wait out. Telling an operator to
        # type a flag that does nothing is worse than offering nothing.
        print("  A live controller cannot be forced off, and a dead one frees "
              "the project immediately, so there is nothing to wait out and "
              "nothing to override.")
        print("  If nothing is really running, the lock is already free: "
              "check with `swarm.py status`.")
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


def resolve_promote_to(raw, root=None):
    """Return (path, error) for a declared promotion destination.

    Three ways this went wrong, all found by trying them, all on the ONE path
    where this tool writes where other people read:

      - a RELATIVE path resolves against the coordinator's cwd, and cron runs
        from a different directory than an interactive shell, so the same plan
        published to two different places depending on how it was invoked
      - "~/canonical" is not expanded by the filesystem, so it created a
        directory literally named "~" and quietly put the results somewhere
        nobody would ever look
      - a destination INSIDE the run root published back into the exclusive
        write area, muddling the isolation everything else rests on
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "it is empty"
    text = raw.strip()
    if text.startswith("~"):
        text = os.path.expanduser(text)
        if text.startswith("~"):
            return None, (f"{raw!r} starts with ~ but no home directory could "
                          f"be resolved. Write the absolute path.")
    if not os.path.isabs(text):
        return None, (f"{raw!r} is a relative path. It would resolve against "
                      f"whatever directory the coordinator happens to run in, "
                      f"which differs between cron and a shell, so the same "
                      f"plan would publish to two different places. Write an "
                      f"absolute path.")
    dest = Path(text).resolve()
    if root:
        try:
            dest.relative_to(Path(root).resolve())
            return None, (f"{raw!r} is inside the run root {root}. Promotion "
                          f"publishes OUT of the exclusive write area; a "
                          f"destination inside it defeats the isolation the "
                          f"predicate depends on. Choose a path outside it.")
        except ValueError:
            pass
    return dest, None


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
        # Two reviewers found that this branch returned success without
        # comparing ANYTHING: st was bound from src.stat() and discarded, so a
        # file replaced with different content and a different size was
        # published while the record asserted a "size and mtime" match that
        # was never performed. A false statement in the audit trail of the one
        # outward-facing surface is worse than no record at all.
        if st.st_size != recorded.get("size"):
            return False, (f"{rel}: size changed since the verdict "
                           f"({recorded.get('size')} -> {st.st_size}). "
                           f"--accept-weak-evidence lowers the standard to "
                           f"size and mtime; it does not waive them.")
        if int(st.st_mtime) != recorded.get("mtime"):
            return False, (f"{rel}: mtime changed since the verdict. "
                           f"--accept-weak-evidence lowers the standard to "
                           f"size and mtime; it does not waive them.")
        return True, (f"{rel}: size and mtime match ({shown}). This does NOT "
                      f"establish the content is unchanged; accepted because "
                      f"--accept-weak-evidence was passed.")
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
    dest_root, derr = resolve_promote_to(u.get("promote_to"),
                                         state.get("root"))
    if u.get("promote_to") and derr:
        return [f"REFUSING: unit {uid} declares promote_to but {derr}"], False
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
            # RE-VERIFY THE COPY. Fingerprints were checked before copying,
            # and an ordinary concurrent writer on a shared filesystem can
            # change a source file in between, so what landed in staging is
            # the only thing worth trusting. Reviewers found this window; it
            # is closed by checking the copy rather than by hoping.
            for rel, rec in sorted(recorded.items()):
                good, why = _redigest(staging, rel, rec, accept_weak)
                if not good:
                    shutil.rmtree(staging, ignore_errors=True)
                    return lines + ["", f"REFUSING: the copy does not match "
                                        f"the receipt, so the source changed "
                                        f"while it was being read. {why}",
                                    "Nothing was published and the canonical "
                                    "pointer did not move."], False
            # Fully populated AND verified before it takes the canonical name,
            # so a reader never sees a partial or altered version directory.
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


def cmd_verify(args):
    """Run an authorized, content-pinned verifier and record what it said.

    Everything that makes this admissible happens HERE, in one place, and the
    receipt records all of it: the policy came from the anchored base commit,
    the verifier's bytes hashed to what the policy authorized, those exact
    bytes ran, and the head it ran against is the one this attempt produced.
    """
    try:
        load_verifications(args.state_dir)
    except OutboxError as exc:
        sys.stderr.write(f"  VERIFICATION JOURNAL: {exc}\n")
        return EXIT_CONFLICT

    anchor_rec, err = W.read_launch_record(args.attempt)
    if err:
        sys.stderr.write(f"error: {err}\n")
        return EXIT_USAGE
    repo = (anchor_rec or {}).get("repo")
    base = (anchor_rec or {}).get("base_commit")

    # THE BASE COMES FROM COORDINATOR STATE, not from the launch record alone.
    # base_commit is the linchpin of the whole authorization chain, and the
    # launch record sits on a filesystem the agent's Unix user can write. If
    # the agent can choose the base, it can point at a commit carrying a
    # policy it wrote, and every check below is then checking its own work.
    # swarm-state.json is coordinator state, which the threat model trusts.
    state = load_state(args.state_dir) or {}
    recorded = trusted_base(state, args.unit, args.attempt)
    if recorded and base and recorded != base:
        sys.stderr.write(
            f"error: the launch record says base {str(base)[:12]} and "
            f"coordinator state says {str(recorded)[:12]}. The record was "
            f"changed after it was written; refusing to read an "
            f"authorization policy from a base this attempt did not "
            f"anchor.\n")
        return EXIT_USAGE
    # NO FALLBACK. `recorded or base` quietly reinstated the whole defect:
    # when coordinator state had nothing, the agent-writable record supplied
    # the value, which is the case an attacker arranges. Found by the
    # structural test written for this root cause, one commit after I claimed
    # the laundering was gone.
    if not recorded:
        sys.stderr.write(
            f"error: coordinator state records no anchored base for attempt "
            f"{Path(args.attempt).name}, so there is no authorization source "
            f"this agent could not have written. Re-dispatch the unit; do not "
            f"verify against a base taken from the launch record.\n")
        return EXIT_USAGE
    base = recorded
    if not repo:
        sys.stderr.write("error: this attempt anchored no repository, so "
                         "there is no base to read a policy from.\n")
        return EXIT_USAGE

    policy, policy_digest, perr = V.read_policy(U.run, repo, base)
    if perr:
        sys.stderr.write(f"error: {perr}\n")
        return EXIT_USAGE

    digest, _size, derr = V.digest_file(args.path)
    if derr:
        sys.stderr.write(f"error: {derr}\n")
        return EXIT_USAGE

    entry, refusal = V.authorized(policy, args.verifier, digest, args.claim)
    if refusal:
        sys.stderr.write(f"error: {refusal}\n")
        return EXIT_USAGE

    produced = _head_from_receipt(args.attempt)
    if not produced:
        sys.stderr.write("error: this attempt has no judged produced commit, "
                         "so a verification would have nothing to bind to.\n")
        return EXIT_USAGE

    outcome, rerr = V.run_in_checkout(U.run, repo, produced, args.path,
                                      digest, args=args.arg,
                                      timeout=args.timeout)
    if rerr:
        sys.stderr.write(f"error: {rerr}\n")
        return EXIT_FAILED_UNIT

    rec = {"unit": args.unit, "claim": args.claim, "verifier": args.verifier,
           "verifier_sha256": digest, "policy_sha256": policy_digest,
           "subject_head": produced,
           "result": "pass" if outcome["exit_code"] == 0 else "fail",
           "exit_code": outcome["exit_code"],
           "stdout_tail": outcome["stdout"], "stderr_tail": outcome["stderr"],
           "by": os.environ.get("USER") or "?",
           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "schema_version": 1}
    bad = _verify_shape_problem(rec)
    if bad:
        sys.stderr.write(f"error: this would not be admissible: {bad}\n")
        return EXIT_USAGE
    _fsync_append(Path(args.state_dir) / VERIFY_RECEIPTS, rec)
    print(f"  {args.claim}: {rec['result'].upper()} (exit "
          f"{outcome['exit_code']}) for {produced[:12]}")
    print(f"  verifier {args.verifier} {digest[:12]}, policy "
          f"{policy_digest[:12]} from base {str(base)[:12]}")
    return EXIT_OK if rec["result"] == "pass" else EXIT_FAILED_UNIT


def cmd_merge(args):
    """Record that a PR for this unit was observed merged.

    Run from a machine that can see the PR. This is an ATTESTATION, like the
    tracker acknowledgment: nothing here checked GitHub, and nothing can. What
    the coordinator checks is that the head you pin is the head it judged this
    attempt to have produced.
    """
    try:
        load_merge_receipts(args.state_dir)     # refuse to extend a bad journal
    except OutboxError as exc:
        sys.stderr.write(f"  MERGE JOURNAL: {exc}\n")
        return EXIT_CONFLICT

    rec = {"unit": args.unit, "repo": args.repo or "", "pr": args.pr,
           "target": args.target, "head": args.head,
           "merged_as": args.merged_as, "method": args.method,
           "merged": True, "attested": True,
           "by": os.environ.get("USER") or "?",
           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "schema_version": 1}
    bad = _merge_shape_problem(rec)
    if bad:
        sys.stderr.write(f"error: this would not be admissible: {bad}\n")
        return EXIT_USAGE
    _fsync_append(Path(args.state_dir) / MERGE_RECEIPTS, rec)
    print(f"  recorded: {args.unit} merged as {args.merged_as[:12]} "
          f"(head {args.head[:12]}, {args.method})")
    print("  This is the attester's word. The coordinator will admit it only "
          "if\n  that head is the one this attempt produced.")
    return EXIT_OK


def cmd_outbox(args):
    """Show tracker intents and whether each was acknowledged.

    Draining happens elsewhere, on a machine that can reach the tracker. This
    command exists so a human on the cluster can see exactly what WOULD be
    sent before anything is, and afterwards what the drainer confirmed landed.
    """
    intents = read_outbox(args.state_dir)

    try:
        return _cmd_outbox_inner(args, intents)
    except OutboxError as exc:
        # One handler for every path into the journal. Previously each branch
        # decided separately whether to care, and one of them always forgot.
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        else:
            sys.stderr.write(f"  RECEIPT JOURNAL: {exc}\n")
        return EXIT_CONFLICT


def _cmd_outbox_inner(args, intents):
    if args.record_receipt:
        keys = {i.get("key") for i in intents}
        if args.record_receipt not in keys:
            sys.stderr.write(
                f"error: no intent with key {args.record_receipt!r} in this "
                f"outbox. A receipt for an unknown key would acknowledge "
                f"something that was never intended.\n")
            return EXIT_USAGE
        if not args.ref:
            sys.stderr.write(
                "error: --ref is required. The receipt records the tracker's "
                "own reference for the operation the drainer watched "
                "succeed; without it there is nothing to check later.\n")
            return EXIT_USAGE
        rec = record_receipt(args.state_dir, args.record_receipt, args.ref,
                             op=args.op)
        print(f"  recorded: {rec['key']} -> {rec['ref']}")
        return EXIT_OK

    status, problems = acknowledgment_status(args.state_dir)

    # FAIL CLOSED. Corruption is not the same as an interrupted tail: a
    # truncated last line is a write that did not finish, and re-draining
    # fixes it. A bad line in the middle means the journal cannot be read in
    # full, so no status derived from it can be trusted, including the
    # comfortable ones.
    for i in intents:
        st, rs = status.get(i.get("key"), (UNACKNOWLEDGED, []))
        i["ack_status"] = st
        i["ack_refs"] = sorted({r.get("ref") for r in rs})

    unack = [i for i in intents if i["ack_status"] == UNACKNOWLEDGED]
    conflicts = [i for i in intents if i["ack_status"] == CONFLICT]

    if args.json:
        print(json.dumps(
            {"note": "ack_status 'attested' is the drainer's claim, not "
                     "verified tracker state: this process cannot reach the "
                     "tracker. 'unacknowledged' means no confirmation either "
                     "way, NOT that nothing was filed.",
             "intents": intents if args.all else unack},
            indent=2, sort_keys=True))
        return EXIT_CONFLICT if conflicts else EXIT_OK

    if not intents:
        print("  no tracker intents recorded")
        print("  Intents appear as units change state. Nothing is ever sent "
              "from here:\n  the coordinator runs on a login node and cannot "
              "reach a tracker.")
        return EXIT_OK

    for note in problems:
        print(f"  RECEIPT JOURNAL [{note['kind']}]: {note['detail']}")
    if problems:
        print()

    show = intents if args.all else unack
    print(f"  {len(unack)} unacknowledged of {len(intents)} intent(s)"
          + (f", {len(conflicts)} in CONFLICT" if conflicts else "") + "\n")
    for i in show:
        ev = "with evidence" if i.get("evidence") else "no evidence"
        label = {ACKNOWLEDGED: "attested", CONFLICT: "CONFLICT",
                 UNACKNOWLEDGED: "unack"}[i["ack_status"]]
        # same string in both modes; see the note on ACKNOWLEDGED
        print(f"  [{label:8}] {i['verb']:6} {i['unit']:12} "
              f"{i['unit_state']:16} {ev}")
        print(f"      {i['why']}  key={i['key']}")
        if i["ack_refs"]:
            print(f"      tracker ref: {', '.join(i['ack_refs'])}")

    if conflicts:
        print("\n  CONFLICT means two receipts claim different tracker refs "
              "for one\n  intent. Something was filed twice, in two places. "
              "Resolve by hand;\n  this tool will not pick a winner.")

    print("\n  Drain from a machine that can reach the tracker, then record "
          "what\n  landed:  swarm.py outbox --state-dir DIR "
          "--record-receipt KEY --ref ID")
    print("  UNACKNOWLEDGED does NOT mean 'not filed'. It means this machine "
          "has\n  no confirmation either way. Re-draining is safe: intents "
          "are keyed.")
    print("  ATTESTED is the drainer's word, not proof. Nothing here can ask "
          "the\n  tracker; check the reference by hand if it matters.")
    return EXIT_CONFLICT if conflicts else EXIT_OK


def _status_rows(plan, state, state_dir):
    """Everything an operator needs, derived from DURABLE STATE ONLY.

    Reads no scheduler and launches nothing, so it renders correctly with the
    coordinator stopped, which is exactly when someone wants to look."""
    units = {u["id"]: u for u in plan.get("units") or []}
    promoted = {}
    try:
        for line in (Path(state_dir) / PROMOTIONS).read_text().splitlines():
            if not line.strip():
                continue
            # Skip a BAD LINE, never the whole file. A crash during append
            # leaves a partial record, and discarding everything showed
            # already-promoted units as "NOT promoted", inviting the operator
            # to publish a second time.
            try:
                r = json.loads(line)
                promoted[r["unit"]] = r
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
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
            # Why a unit that has not started is not starting. A bare "-" in
            # the status table left a human unable to tell a DAG that is
            # waiting from one that has stalled forever.
            "waiting_on": ([d for d in (u.get("needs") or [])
                            if (state.get("units", {}).get(d) or {}).get(
                                "state") != "DONE"]
                           if not us.get("attempt_dir") and st in ("-", None)
                           else []),
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
    # READY_FOR_PR belongs here: no mechanism can leave that state today, so
    # a DAG parked in it is stalled, not progressing. Without this, `status`
    # exits 0 and a cron wrapper polling it reports a healthy project forever.
    attention = [r for r in rows
                 if r["state"] in ("NEEDS_HUMAN", "FAILED", "FAILED_EVIDENCE",
                                   "READY_FOR_PR")]
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
            if r["state"] == "READY_FOR_PR":
                # Say plainly that this cannot advance on its own. A unit
                # parked in a state no mechanism can leave is a stalled DAG,
                # and a human reading `status` must not have to know that.
                print(f"  {'':{w}}    the agent finished and its outputs "
                      f"exist, but a code unit is closed by a MERGED PULL "
                      f"REQUEST.")
                print(f"  {'':{w}}    Nothing records merges yet, so this "
                      f"will not advance on its own and anything below it "
                      f"waits. Open the PR and close it by hand, or make "
                      f"this a kind=slurm or kind=pipeline unit.")
            if r["waiting_on"]:
                print(f"  {'':{w}}    waiting on "
                      f"{', '.join(r['waiting_on'])}")
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

    v = sub.add_parser("verify", help="run an authorized pinned verifier")
    v.add_argument("--state-dir", default=".swarm/state")
    v.add_argument("--unit", required=True)
    v.add_argument("--attempt", required=True,
                   help="the attempt directory whose produced commit is "
                        "being verified")
    v.add_argument("--claim", required=True,
                   help="what this verifier is asserting, e.g. tests-pass. "
                        "It must be a claim the policy allows it to make.")
    v.add_argument("--verifier", required=True,
                   help="the name the policy authorizes")
    v.add_argument("--path", required=True,
                   help="the file to run. Its bytes must hash to what the "
                        "policy recorded.")
    v.add_argument("--arg", action="append", default=[])
    v.add_argument("--timeout", type=int, default=900)
    v.set_defaults(fn=cmd_verify)

    m = sub.add_parser("merge", help="record an observed merged PR for a "
                                     "code unit")
    m.add_argument("--state-dir", default=".swarm/state")
    m.add_argument("--unit", required=True)
    m.add_argument("--pr", required=True, help="the PR's URL or number")
    m.add_argument("--head", required=True,
                   help="the PR head commit. Must be the commit this attempt "
                        "produced, or the receipt is refused.")
    m.add_argument("--target", required=True, help="the branch it merged into")
    m.add_argument("--merged-as", required=True,
                   help="the resulting commit on the target")
    m.add_argument("--method", required=True, choices=MERGE_METHODS)
    # REQUIRED, because _merge_shape_problem requires it: an optional flag
    # feeding a mandatory field is a command that can only fail, and only the
    # smoke test found it. Every unit test passed a repo.
    m.add_argument("--repo", required=True,
                   help="the repository the PR is in")
    m.set_defaults(fn=cmd_merge)

    o = sub.add_parser("outbox", help="tracker intents, and what landed")
    o.add_argument("--state-dir", default=".swarm/state")
    o.add_argument("--all", action="store_true",
                   help="include already-acknowledged intents")
    o.add_argument("--json", action="store_true")
    o.add_argument("--record-receipt", metavar="KEY",
                   help="record that the drainer OBSERVED this intent's "
                        "operation succeed. Only after the tracker confirms: "
                        "a false acknowledgment is worse than a missing one, "
                        "because re-draining is safe and un-filing is not.")
    o.add_argument("--ref", help="the tracker's own reference for the "
                                 "operation that succeeded, e.g. ARC-171")
    o.add_argument("--op", help="optional: which operation was performed")
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
