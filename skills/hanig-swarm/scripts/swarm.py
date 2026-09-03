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
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import unit as U  # noqa: E402  same skill, installed together
import child_environment as CE  # noqa: E402
import paseo_io as PIO
import worktree as W  # noqa: E402
import verify as V  # noqa: E402
import converge as CV  # noqa: E402  the declared-convergence gate
import coordinator_paths as CP  # noqa: E402

STATE_FILE = "swarm-state.json"
KINDS = U.KINDS

# What a `code` unit runs unless it says otherwise.
#
# `codex/gpt-5.6-sol` at `high` is the strongest agent available here: `bus
# models` puts it top of the local roster on measured intelligence, ahead of
# claude/opus, and the pairing is the one already in use by hand. Provider,
# model and thinking id were all read off live agents rather than guessed,
# because paseo answers an unknown thinking id with an ERRORED agent, and a
# default that fails at dispatch is worse than no default.
#
# A unit overrides any of it with `provider`, `model` or `thinking`. Setting
# `thinking` to null or "" turns the flag off entirely for a provider that has
# no such option.
DEFAULT_AGENT_PROVIDER = "codex/gpt-5.6-sol"
DEFAULT_AGENT_THINKING = "high"

# This marker is deliberately stable: validation checks the exact prompt the
# coordinator would dispatch, not prose copied into a plan. Plans cannot omit
# the protocol, and attempts cannot invent its source branch, target, or base.
CODE_COMPLETION_PROTOCOL_MARKER = "SWARM CODE COMPLETION PROTOCOL"

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


def _code_completion_protocol(intent):
    """Instructions that make a code attempt capable of closing its unit.

    C11 creates the branch and worktree. The agent's job is to leave durable
    evidence on that branch and open the pull request that the unit's closing
    predicate requires; asking it to choose either launch fact would put
    authority back in prompt prose.

    Precedence is explicit rather than inferred from task text. Contradictions
    in arbitrary prose are not statically recognizable: matching phrases such
    as "do not commit" would also reject legitimate tasks that merely discuss
    them. A visible stop-and-report rule is bounded; a prose detector is not.
    """
    repo = str(intent["repo"])
    branch = str(intent["branch"])
    base = str(intent["base_commit"])
    target = str(intent["target_branch"])
    remote = intent.get("repository_remote")
    if remote:
        remote_instruction = (
            f"Use Git remote 'origin', recorded by the coordinator as "
            f"{remote!r}. Push {branch!r} to that remote. Open a pull request "
            f"from {branch!r} into target branch {target!r}; only a "
            f"pull request merged into {target!r} in that recorded repository "
            f"closes this code unit.")
    else:
        remote_instruction = (
            "The coordinator recorded Git remote 'origin' as None. STOP AND "
            "REPORT that no merge-evidence repository was recorded; do not "
            "guess another remote.")
    return f"""{CODE_COMPLETION_PROTOCOL_MARKER} (coordinator-required)
This protocol overrides any contrary instruction in the task text above it. If the task appears to forbid committing, pushing, or opening a pull request, STOP AND REPORT that conflict instead of choosing either instruction.
You are already in a dedicated worktree for repository {repo!r}, on branch {branch!r}, cut from recorded base commit {base}.
The required pull-request target is {target!r}.
Do not create or switch branches, and do not choose a different base.
Commit all intended work on {branch!r}. Uncommitted work is invisible to the transition predicate and will be judged as producing nothing.
{remote_instruction}
If you cannot finish cleanly, STOP AND REPORT the problem instead of working around it.
Leave the worktree clean. Do not force-push or rewrite history. The final commit must descend from recorded base {base}; rewritten history makes honest work unjudgeable."""


def _dispatch_prompt(u, intent=None):
    """Return the single prompt argv element for an agent dispatch."""
    prompt = str(u.get("prompt") or u.get("command") or u.get("id") or "")
    if u.get("kind") != "code":
        return prompt
    if not isinstance(intent, dict):
        raise PlanError(
            f"unit {u.get('id','?')!r} is kind=code but has no trusted "
            f"launch intent from which to build its completion protocol")
    return prompt.rstrip() + "\n\n" + _code_completion_protocol(intent)


def _code_protocol_problem(prompt, intent):
    """Return why an assembled code prompt is structurally unclosable."""
    if not prompt.endswith("\n\n" + _code_completion_protocol(intent)):
        return "the coordinator-generated protocol is not the final prompt block"
    required = {
        "protocol marker": CODE_COMPLETION_PROTOCOL_MARKER,
        "repository": repr(str(intent["repo"])),
        "attempt branch": repr(str(intent["branch"])),
        "pull-request target": repr(str(intent["target_branch"])),
        "recorded remote": repr(intent.get("repository_remote")),
        "recorded base": str(intent["base_commit"]),
        "protocol precedence": "overrides any contrary instruction",
        "contradiction instruction": "STOP AND REPORT that conflict",
        "commit instruction": "Commit all intended work",
        "remote action": ("Open a pull request" if intent.get(
            "repository_remote") else "no merge-evidence repository was recorded"),
        "clean failure instruction": "STOP AND REPORT",
        "history instruction": "Do not force-push or rewrite history",
    }
    missing = [name for name, text in required.items() if text not in prompt]
    if missing:
        return "missing " + ", ".join(missing)
    return None


# --- the declared convergence gate ---------------------------------------
# `unit.py` answers existence and terminal state. For a training run that is
# not enough: a job that executed its whole step budget, exited 0 and wrote a
# checkpoint is DONE under that predicate even if the loss was flat for the
# last three quarters of it. `converge.py` scores a declared criterion over
# the metrics SERIES and tells "it converged" apart from "it stopped".
#
# It is OPT-IN per unit and it gates DONE, in the same shape as
# `requires_verification`: undeclared, nothing changes, because a gate
# everybody must satisfy is one everybody learns to satisfy trivially.
#
# THE CRITERION COMES FROM THE PLAN, whose digest is frozen, and never from
# the attempt directory. That is not a detail. The unit spec lives inside the
# write root the job itself writes to, so reading the criterion from there
# would let a run rewrite the standard it is judged against -- the same
# laundering the launch record and the receipt were demoted to audit-only for.
CONVERGE_UNIT_KEYS = frozenset({"metrics", "criterion", "diverge", "budget",
                                "sparse_metric"})


def converge_problem(u):
    """Why a unit's declared convergence block cannot be evaluated, or None.

    Checked at PLAN time as well as at judge time. `converge.py` says it
    plainly -- "declaring the criterion BEFORE the run is the whole point" --
    and a criterion accepted here but rejected forty thousand steps later has
    cost a real job for a typo.
    """
    spec = u.get("converge")
    if spec is None:
        return None
    uid = u.get("id", "?")
    if u.get("kind") == "code":
        return (f"unit {uid!r} is kind=code and declares 'converge'. A code "
                f"unit has no metrics series and is closed by a merged pull "
                f"request, so the gate would never be reached. Drop the "
                f"block, or declare the work as kind=slurm or kind=pipeline.")
    if not isinstance(spec, dict):
        return (f"unit {uid!r} has converge={spec!r}, a "
                f"{type(spec).__name__}; it must be an object like "
                f'{{"metrics": "metrics.jsonl", "criterion": '
                f'{{"metric": "val_loss", "mode": "min", "threshold": 0.5}}}}.')
    unknown = sorted(set(spec) - CONVERGE_UNIT_KEYS)
    if unknown:
        return (f"unit {uid!r} converge has unrecognised key(s) "
                f"{', '.join(unknown)}; it reads only "
                f"{', '.join(sorted(CONVERGE_UNIT_KEYS))}. A typo here would "
                f"drop the gate silently, which is the one failure mode this "
                f"whole family of checks exists to prevent.")
    metrics = spec.get("metrics")
    if not isinstance(metrics, str) or not metrics.strip():
        return (f"unit {uid!r} converge declares metrics={metrics!r}; name "
                f"the JSONL file the run appends its evaluations to, RELATIVE "
                f"to the attempt write root.")
    if metrics not in (u.get("outputs") or []):
        return (f"unit {uid!r} judges convergence over {metrics!r}, which is "
                f"not one of its declared outputs. The gate is conclusive "
                f"only over a file inside the exclusive write root, and only "
                f"a declared output is checked to be there at all -- "
                f"otherwise a missing metrics file reads as 'cannot judge' "
                f"instead of 'the run produced nothing'. Add {metrics!r} to "
                f"'outputs'.")
    problem = CV.criterion_problem(spec.get("criterion"))
    if problem:
        return f"unit {uid!r} converge criterion: {problem}"
    if not isinstance(spec.get("sparse_metric", False), bool):
        return (f"unit {uid!r} has converge.sparse_metric="
                f"{spec['sparse_metric']!r}; it must be true or false.")
    rules = spec.get("diverge")
    if rules is not None and not isinstance(rules, list):
        return (f"unit {uid!r} has converge.diverge={rules!r}, a "
                f"{type(rules).__name__}, but it must be a LIST of rule "
                f"objects. A single object here is iterated over its KEYS, so "
                f"the bound is silently lost and nothing is checked.")
    for i, rule in enumerate(rules or []):
        if not isinstance(rule, dict):
            return (f"unit {uid!r} converge.diverge[{i}]={rule!r} is not an "
                    f"object; each rule looks like "
                    f'{{"metric": "train_loss", "above": 1e9}}.')
        if not isinstance(rule.get("metric"), str) or not rule["metric"].strip():
            return (f"unit {uid!r} converge.diverge[{i}] names no metric; "
                    f"give the metric's name exactly as the run writes it.")
        if not any(k in rule for k in ("above", "below")):
            return (f"unit {uid!r} converge.diverge[{i}] declares no bound; "
                    f"add 'above' or 'below', or remove the rule. A rule with "
                    f"no bound cannot show the run stayed inside it.")
        problem = CV.unread_key_problem(rule, CV.DIVERGE_KEYS,
                                        f"converge.diverge[{i}]")
        if problem:
            return f"unit {uid!r} {problem}"
    if spec.get("budget") is not None:
        budget, err = CV.finite_number(spec["budget"])
        if err or budget < 0:
            return (f"unit {uid!r} has converge.budget={spec['budget']!r}, "
                    f"which {err or 'must not be negative'}. The budget is "
                    f"the step count whose exhaustion is NOT convergence.")
    return None


def converge_verdict(u, attempt_dir):
    """Judge a unit's declared criterion over its attempt's metrics.

    Returns (state_name, [reasons]) straight from `converge.py`. It reads the
    metrics file the PLAN named, inside this attempt's exclusive write root,
    and nothing else: it writes nothing, consults neither the launch record
    nor the receipt, and re-derives no pinned value. The metrics series is
    primary evidence observed where it lies, not a pinned fact asked again.
    """
    spec = u.get("converge") or {}
    return CV.judge(str(Path(attempt_dir) / str(spec.get("metrics") or "")),
                    spec.get("criterion") or {},
                    spec.get("diverge") or [],
                    spec.get("budget"),
                    bool(spec.get("sparse_metric")))


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

        policy = u.get("workspace_policy")
        if policy is not None and not isinstance(policy, dict):
            raise PlanError(
                f"unit {u.get('id','?')!r} has workspace_policy={policy!r}; "
                f"it must be an object")
        if policy and (policy.get("requires_clean_git")
                       or policy.get("clean_git")):
            workspace = (u.get("execution_workspace") or policy.get("path")
                         or u.get("repo"))
            if not workspace:
                raise PlanError(
                    f"unit {u.get('id','?')!r} requires a clean Git workspace "
                    f"but declares no workspace_policy.path, "
                    f"execution_workspace, or repo")

    # A convergence criterion is refused HERE, before anything is dispatched,
    # for the reason converge.py gives for requiring one at all: it has to be
    # declared before the run. Discovering the typo when the job is finished
    # means the GPU-hours are already spent and there is no criterion to judge
    # them against.
    for u in units:
        if not isinstance(u, dict):
            continue
        problem = converge_problem(u)
        if problem:
            raise PlanError(problem)

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

    # --- a code unit's prompt is a PROMPT, not a command line -------------
    #
    # `argv.append(u.get("prompt") or u.get("command"))` puts this string in
    # the final positional slot of `paseo run`, so anything flag-shaped in it
    # is handed to the AGENT as instruction text. Carefully-added paseo flags
    # became a sentence the model was asked to read.
    #
    # The configuration lives in FIELDS: provider, mode, model, env. Nothing
    # in the prompt reaches paseo, and nothing in paseo's flags reaches the
    # prompt, and neither fact is visible from the plan file.
    # `thinking` belongs here and was missing: I added the field and forgot
    # the ban, so `--thinking low` in a prompt was silently read aloud to the
    # agent while it ran at the default effort.
    _AS_FIELDS = ("provider", "mode", "model", "thinking", "env", "cwd",
                  "title", "background", "json")
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "code":
            continue
        text = str(u.get("prompt") or u.get("command") or "")
        # ONLY AT THE START. Checking every token refused
        # "Implement the application's --mode strict option", which is an
        # ordinary thing to ask an agent to do and has nothing to do with
        # paseo. The mistake this catches has a shape: flags pasted at the
        # front, where a command line would have them. Mid-sentence, a flag is
        # the subject of the request rather than an attempt to configure the
        # runner, and refusing it teaches people the validator is noise.
        head = text.split()[:1]
        if not head or not head[0].startswith("--"):
            continue
        flag = head[0][2:].split("=")[0].replace("-", "_")
        if flag in _AS_FIELDS:
            raise PlanError(
                f"unit {u.get('id','?')!r} is kind=code and its prompt BEGINS "
                f"with {head[0]!r}. A code unit's prompt is the LAST "
                f"positional argument to the agent runner, so a flag written "
                f"here is not configuration: it is a sentence the agent is "
                f"asked to read. Set {flag!r} as a field on the unit instead, "
                f"alongside 'prompt'.")

    # --- every dispatched code prompt carries its closing protocol --------
    #
    # Plans once described sixteen code units that could only close on a
    # merged PR, while telling no agent to commit or open one. Appending here
    # would still leave dispatch free to drop plan-level settings, so the
    # coordinator's dispatch builder owns the protocol. Validate exercises
    # that SAME builder with unmistakable launch facts and refuses if a later
    # edit makes it produce an unclosable prompt. _submit repeats the check
    # with the real, trusted attempt facts immediately before Paseo is called.
    validation_intent = {
        "repo": "/__swarm_protocol_validation_repo__",
        "branch": "swarm-protocol-validation-attempt",
        "target_branch": "main",
        "repository_remote": "ssh://git@example.invalid/project.git",
        "base_commit": "0" * 40,
    }
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "code":
            continue
        assembled = _dispatch_prompt(u, validation_intent)
        problem = _code_protocol_problem(assembled, validation_intent)
        if problem:
            raise PlanError(
                f"unit {u.get('id','?')!r} is kind=code but the coordinator "
                f"would dispatch it without the required completion protocol "
                f"({problem}). Refusing a code unit that could finish its "
                f"edits yet never produce the merged-PR evidence that closes "
                f"it.")

    # --- a code unit declares where its pull request must merge -----------
    #
    # C11 made the old `branch` field vestigial: every attempt now gets a
    # coordinator-created `swarm-<attempt>` source branch in its own worktree.
    # Do not silently reinterpret old plans' working-branch names as merge
    # targets. `target_branch` is a new, explicit plan decision; existing
    # plans fail here until migrated instead of opening a PR against a branch
    # they never meant as the destination.
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "code":
            continue
        uid = u.get("id", "?")

        # A code unit is closed by a MERGED PR, so one with no repository has
        # nowhere to open a PR from and cannot reach DONE by any route. The
        # check used to skip these, which made "no repo" a way to bypass the
        # pull-request target rule and land in a state nothing can leave.
        if not u.get("repo"):
            raise PlanError(
                f"unit {uid!r} is kind=code and declares no 'repo'. A code "
                f"unit is closed by a merged pull request, so with no "
                f"repository there is nothing to open one from and the unit "
                f"can never reach DONE. Declare the repository it changes, or "
                f"make it kind=pipeline if it is not changing code.")

        # `mode` has NO default on purpose, and that makes its absence a
        # decision nobody made. An agent under default permissions stops at
        # its first write and waits for a person: unattended, that is a unit
        # that runs forever doing nothing, which is the exact symptom this
        # costs a session to diagnose. The coordinator must not choose for the
        # human, so it insists the human chose.
        # Presence is not a value. `"mode": null` satisfied `"mode" in u` and
        # then `if u.get("mode")` omitted the flag, so the unit dispatched on
        # default permissions and stalled: the exact failure this rule exists
        # to prevent, through the rule's own hole.
        #
        # The distinction that decides what validate should catch: mode fails
        # SILENTLY, as an agent waiting forever, so it is worth refusing here.
        # An unknown `thinking` id fails LOUDLY, as an errored agent and a
        # FAILED unit, and validate cannot know a provider's valid set without
        # introspecting it. Hard-coding one would refuse ids that become valid
        # as the provider changes, which is the worse trade.
        # PROVIDER-SPECIFIC, and the examples have to follow the provider or
        # they teach a value that gets rejected. `bypass` is claude's word;
        # codex answers it with `auto, auto-review, full-access`, and codex is
        # the default provider now, so the old advice was wrong for every unit
        # that did not name a provider. Still no hard-coded valid SET here,
        # for the reason given above about `thinking`: naming one example the
        # provider accepts is guidance, enumerating them all is a stale list
        # waiting to refuse a value that became valid.
        unattended = _unattended_mode_example(u.get("provider"))
        if "mode" in u and not str(u.get("mode") or "").strip():
            raise PlanError(
                f"unit {uid!r} declares mode={u.get('mode')!r}, which is not a "
                f"value. An absent or empty mode omits the flag entirely, so "
                f"the agent runs on DEFAULT permissions, stops at its first "
                f"write and waits for a person. Write the mode you want, e.g. "
                f"\"{unattended}\" or \"default\". Modes are "
                f"provider-specific; `paseo run --help` and the provider name "
                f"the set it accepts.")
        if "mode" not in u:
            raise PlanError(
                f"unit {uid!r} is kind=code and declares no 'mode'. An agent "
                f"under default permissions stops at its first write and "
                f"waits for a person, so unattended it runs forever doing "
                f"nothing. The coordinator will not pick permissions on your "
                f"behalf: say what this unit needs, e.g. "
                f"\"mode\": \"{unattended}\" for unattended work on "
                f"{u.get('provider') or DEFAULT_AGENT_PROVIDER}, or "
                f"\"mode\": \"default\" to accept the stall deliberately. "
                f"Modes are provider-specific.")

        # "null" and "none" as STRINGS are a JSON slip, not a value. paseo
        # answers an unknown thinking id with an errored agent, so this would
        # fail at dispatch for every code unit in the DAG. JSON null and ""
        # already suppress the flag correctly; these do not.
        think = u.get("thinking")
        if isinstance(think, str) and think.strip().lower() in (
                "null", "none", "nil", "false"):
            raise PlanError(
                f"unit {uid!r} has thinking={think!r} as a STRING. paseo would "
                f"receive that as a thinking id, not find it, and return an "
                f"errored agent. To suppress the flag write JSON null or an "
                f"empty string; to set a level write the id, e.g. \"high\".")
        target = str(u.get("target_branch") or "").strip()
        if not target:
            legacy = (" The legacy 'branch' field is not used as a fallback."
                      if u.get("branch") else "")
            raise PlanError(
                f"unit {uid!r} is kind=code on repo {u['repo']!r} and declares "
                f"no 'target_branch'. The coordinator creates the source "
                f"branch, but it cannot open a mergeable pull request without "
                f"the plan naming its destination.{legacy}")

    # --- paths the COMMAND names, not just the ones it declares -----------
    #
    # "validate refuses a plan whose declared inputs are empty, still
    # placeholders, or match nothing" is true, and it gave false confidence: a
    # unit declared one input that existed, and dispatched straight into
    # FileNotFoundError on a DIFFERENT path in its own command line.
    #
    # So the command is read too. An absolute path in it must be one of: an
    # existing file or directory, a declared input, or something an upstream
    # unit produces. Anything else is a path nobody has established will be
    # there, and finding that out costs a dispatch.
    #
    # Deliberately narrow, because a false refusal here is expensive: only
    # ABSOLUTE paths (a relative one is resolved against a working directory
    # this cannot know), only tokens that look like filesystem paths, and
    # anything under the attempt's own root is skipped since that is created
    # at dispatch. A glob that matches nothing is reported, a glob that
    # matches is fine.
    produced_anywhere = {str(o) for u in units if isinstance(u, dict)
                         for o in (u.get("outputs") or [])}
    for u in units:
        if not isinstance(u, dict) or u.get("kind") == "code":
            continue
        cmd = str(u.get("command") or "")
        declared = {str(i) for i in (u.get("inputs") or [])}
        # SKIP THE PROGRAM. The first token is the interpreter or executable,
        # and sol's ruling on the runtime applies to it: an absolute path there
        # need only resolve on the COMPUTE node, so stat-ing it here would
        # assert a fact about a machine this one cannot see, and would refuse
        # a container or module path that is correct. The runtime declaration
        # and its canary cover the program. What is left, the ARGUMENTS, are
        # data paths on shared storage, and those are exactly what dispatches
        # into FileNotFoundError.
        tokens = cmd.split()
        rest = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        rt, _ref = resolve_runtime(plan, u)
        entry = str((rt or {}).get("entrypoint") or "") if isinstance(
            rt, dict) else ""
        for raw in re.findall(r"[^\s'\"=,;:()]+", rest):
            tok = raw.strip().rstrip(",;")
            if not tok.startswith("/") or len(tok) < 4:
                continue
            if "$" in tok or "{" in tok:
                continue                       # expanded at run time
            if tok in declared or tok in produced_anywhere:
                continue
            # EXACT, not a prefix. `startswith` exempted
            # /opt/tool/bin/python_extra/missing.tsv because the entrypoint is
            # /opt/tool/bin/python, so an unrelated missing file rode in on the
            # runtime's name.
            if entry and tok == entry:
                continue               # the declared runtime, not a data path
            # ONE visibility rule, applied to globs too. I wrote the
            # parent-directory test for plain paths and left the glob branch
            # raising unconditionally, so a compute-node-only glob was still
            # refused: the same false refusal, surviving in the branch I did
            # not revisit.
            #
            # Trailing slashes are stripped first. `/tmp/missing/` gave
            # dirname `/tmp/missing`, which is not a directory, so the check
            # skipped a path it should have caught.
            probe = tok.rstrip("/") or tok
            parent = os.path.dirname(probe)
            visible = bool(parent) and os.path.isdir(parent)

            if any(ch in tok for ch in "*?["):
                import glob as _glob
                if _glob.glob(tok):
                    continue
                if not visible:
                    continue           # a mount this host cannot see
                raise PlanError(
                    f"unit {u.get('id','?')!r} names the pattern {tok!r} in "
                    f"its command and nothing in {parent!r} matches it. This "
                    f"host can see that directory, so this is not a mount "
                    f"that differs on the compute node.")
            if os.path.exists(tok):
                continue
            # ONLY WHEN WE CAN SEE THE DIRECTORY. The submit host is not the
            # compute node, so a path under a mount that exists only there is
            # legitimate and unknowable from here: refusing it would reject a
            # working plan, which is the more expensive mistake.
            #
            # The line that separates the two: if the PARENT DIRECTORY is
            # visible and the file is not in it, this host can see the place
            # the path claims to be and the thing is not there. That is the
            # shared-storage typo the check exists for. If the parent is also
            # absent, this is a different mount and nothing can be said.
            if not visible:
                continue
            raise PlanError(
                f"unit {u.get('id','?')!r} names {tok!r} in its command. "
                f"{parent!r} exists on this host and does not contain it, so "
                f"this is not a mount that differs on the compute node: it is "
                f"the FileNotFoundError you would meet after dispatching. "
                f"Declare it in 'inputs' if something else creates it, or fix "
                f"the path.")

    # --- an array fans out into ONE attempt directory ---------------------
    #
    # Every task of `--array` shares the unit's single attempt directory, so
    # the first task to finish writes the artifacts record over a partially
    # complete result and the unit reads DONE on 1/20th of the work. Invisible
    # in a dry run, because a dry run does not fan out.
    #
    # The exclusive write root makes a cheap predicate conclusive precisely
    # because ONE writer owns it. An array is N writers by construction, so
    # the premise is gone and the predicate is answering about whichever task
    # happened to finish first.
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "slurm":
            continue
        arr = None
        args = [str(a) for a in (u.get("sbatch") or [])]
        for i, a in enumerate(args):
            if a.startswith("--array="):
                arr = a.split("=", 1)[1]
            # A separated value must not be the next flag. `--array
            # --partition=cpu` read "--partition=cpu" as the range, which is a
            # malformed plan Slurm would reject at dispatch, and which
            # produced a misleading array-and-outputs refusal when outputs
            # existed.
            elif a in ("--array", "-a") and i + 1 < len(args):
                nxt = args[i + 1]
                if nxt.startswith("-"):
                    raise PlanError(
                        f"unit {u.get('id','?')!r} has {a} followed by "
                        f"{nxt!r}, which is another flag rather than a task "
                        f"range. Slurm would reject this at submission.")
                arr = nxt
            elif a.startswith("-a") and len(a) > 2 and not a.startswith("--"):
                arr = a[2:]
        if arr and (u.get("outputs") or []):
            raise PlanError(
                f"unit {u.get('id','?')!r} declares --array {arr!r} AND "
                f"outputs {', '.join(u['outputs'])}. Every array task shares "
                f"this unit's single attempt directory, so the first task to "
                f"finish writes the artifacts record over a partially "
                f"complete result and the unit reads DONE on a fraction of "
                f"the work. A dry run will not show it, because a dry run "
                f"does not fan out. Make each shard its own unit, or have the "
                f"array write into per-task paths and declare a separate "
                f"merge unit that produces the outputs.")

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
    """Load the durable side of the coordinator's lifecycle invariant.

    PERSIST BEFORE YOU ACT applies at both ends: record creation authority
    before starting an agent, and record conclusions plus cleanup charges
    before destroying their worktree. A crash may repeat observation, never
    creation or destruction whose governing state existed only in memory.
    """
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


def _submit(u, unit_dir, dry_run, state=None, state_dir=None):
    """Submit, and return (job_id, error). Dispatch differs per kind; judging
    does not.

    --dry-run records what WOULD be submitted, so the DAG logic is testable
    without a scheduler. A coordinator that can only be tested on a live
    cluster does not get tested."""
    kind = u["kind"]
    anchored_base = None
    if kind == "code":
        # Capture only facts the coordinator can know before the agent exists.
        # Paseo chooses the managed worktree path while servicing `run`, so
        # the final launch snapshot is completed from that command's trusted
        # response below. The immutable base is durable before the command:
        # recovery may ask Paseo for the path, but never re-resolves a ref.
        attempt = Path(unit_dir).name
        existing_intent = ((((state or {}).get("units") or {}).get(u["id"]) or {})
                           .get("attempt_launch_intents") or {}).get(attempt)
        if existing_intent:
            anchor_err = _code_launch_intent_problem(
                existing_intent, u, attempt)
            anchored_base = (None if anchor_err else {
                "base": existing_intent["base_commit"],
                "intent": existing_intent,
            })
        else:
            anchor_err, anchored_base = _capture_code_launch(unit_dir, u)
        if anchor_err:
            return None, anchor_err
    elif _requires_clean_workspace(u):
        anchor_err, anchored_base = _write_launch_record(unit_dir, u)
        if anchor_err:
            return None, anchor_err
    if state is not None and anchored_base:
        # Trusted coordinator observation, keyed by attempt. Historical
        # attempts retain the base they actually launched from.
        #
        # The SEAL is stored the same way and for the same reason. It is the
        # digest of the launch record as written, before any agent existed, so
        # a later reader can tell whether the record it is holding is still
        # the one the coordinator wrote. Keeping it here rather than beside
        # the record is the whole point: a seal stored next to what it seals
        # protects nothing.
        us = state.setdefault("units", {}).setdefault(u["id"], {})
        # Two facts, stored independently, because they are not the same fact.
        # A unit that declared no repository still gets a sealed record saying
        # so; it has no base. Storing an explicit None base would put a key in
        # state that reads as "we looked and found nothing" when the truth is
        # "there was nothing to look for".
        if anchored_base.get("base"):
            us.setdefault("attempt_bases", {})[Path(unit_dir).name] = (
                anchored_base["base"])
        if anchored_base.get("intent"):
            us.setdefault("attempt_launch_intents", {})[
                Path(unit_dir).name] = anchored_base["intent"]
        if anchored_base.get("facts"):
            us.setdefault("attempt_launch_facts", {})[
                Path(unit_dir).name] = anchored_base["facts"]
        if anchored_base.get("seal"):
            us.setdefault("attempt_record_seals", {})[Path(unit_dir).name] = (
                anchored_base["seal"])
    if kind == "code" and not dry_run and state_dir is not None:
        # The immutable base and desired branch are authority. Persist them
        # before Paseo can create an agent; a crash recovery may discover the
        # cwd from Paseo, but it must never discover a replacement base from a
        # ref that could have moved in the meantime.
        save_state(state_dir, state)
    if _requires_clean_workspace(u) and kind != "code" and not dry_run:
        facts = trusted_launch_facts(state or {}, u["id"], unit_dir)
        if not facts:
            return None, (
                f"unit {u['id']!r}: coordinator state has no complete launch "
                f"snapshot for attempt {Path(unit_dir).name!r}. Re-dispatch "
                f"into a fresh attempt; do not reconstruct trusted facts "
                f"from the agent-writable launch record.")
        # The seal now audits the human-readable copy only. A mismatch is
        # retained for inspection and cannot alter the facts used below.
        stored = trusted_record_seal(state or {}, u["id"], unit_dir)
        if stored:
            _audit, audit_err = W.read_sealed_launch_record(unit_dir, stored)
            audit = (state.setdefault("units", {}).setdefault(u["id"], {})
                     .setdefault("attempt_record_audit", {}))
            if audit_err:
                audit[Path(unit_dir).name] = audit_err
            else:
                audit.pop(Path(unit_dir).name, None)
        # Apply the lifecycle durability invariant documented by load_state:
        # this snapshot must exist on disk before the agent does.
        if state_dir is not None:
            save_state(state_dir, state)
    if dry_run:
        return f"dry-{os.urandom(3).hex()}", None
    if state is None or state_dir is None:
        return None, (f"refusing non-dry dispatch for unit {u['id']!r} "
                      f"without coordinator state and state_dir: launch "
                      f"authority must be durable before an external job or "
                      f"agent is created")
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
        # Same policy as every U.run child. Pipelines bypass U.run because
        # they detach, so they must construct their environment through the
        # shared credential denylist rather than inheriting coordinator
        # authority.
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
                ["sh", "-c", wrapped], cwd=str(unit_dir), env=CE.child_env({
                    "SWARM_UNIT_ID": str(u["id"]),
                    "SWARM_UNIT_DIR": str(unit_dir),
                    **dict(_dep_env(u, state or {})),
                }),
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
        # Paseo owns both the agent and its per-attempt worktree. `--cwd`
        # names the trusted SOURCE repository only so Paseo knows which
        # project to branch from; the agent's cwd is the newly-created path
        # returned by this command and recorded below.
        attempt = Path(unit_dir).name
        intent = (((state or {}).get("units", {}).get(u["id"], {})
                   .get("attempt_launch_intents") or {}).get(attempt))
        intent_err = _code_launch_intent_problem(intent, u, attempt)
        if intent_err:
            return None, intent_err
        source_repo = intent["repo"]
        reuse_workspace = None
        # A prior `paseo run` may have created both branch and worktree before
        # this controller crashed. Re-running the creation command can only
        # collide with those resources. Recover the named agent first; if no
        # agent owns the pre-existing branch, fail closed and name the manual
        # cleanup rather than retrying the same impossible creation forever.
        rc, _out, _err = _git(
            source_repo, "show-ref", "--verify", "--quiet",
            f"refs/heads/{intent['branch']}")
        if rc == 0:
            existing_agent, note = reconcile_orphan(
                unit_dir, kind="code")
            if existing_agent:
                recovery_error = _recover_code_launch(
                    state if state is not None else {}, u, unit_dir,
                    existing_agent)
                if recovery_error:
                    return None, (
                        f"attempt {attempt!r} already has agent "
                        f"{existing_agent}, branch {intent['branch']!r}, and "
                        f"a worktree, but recovery refused it: "
                        f"{recovery_error}. Inspect the named agent/worktree; "
                        f"do not re-run this attempt until they agree with "
                        f"the recorded launch intent")
                if state_dir is not None:
                    save_state(state_dir, state)
                return str(existing_agent), None
            candidates = _git_worktrees_on_branch(
                source_repo, intent["branch"])
            if not candidates or len(candidates) != 1:
                return None, (
                    f"attempt {attempt!r} has branch {intent['branch']!r} "
                    f"but no registered agent and {len(candidates or [])} "
                    f"matching Git worktrees. Inspect `git worktree list` "
                    f"and `paseo workspace ls`; the coordinator reuses only "
                    f"one unambiguous worktree that still matches its trusted "
                    f"launch intent")
            reuse_workspace = candidates[0]
            ownership_problem = _paseo_path_ownership_problem(reuse_workspace)
            if ownership_problem:
                return None, (
                    f"attempt {attempt!r} will not launch an agent in "
                    f"pre-existing worktree {reuse_workspace!r}: "
                    f"{ownership_problem}")
            reuse_error = _complete_code_launch(
                state, u, unit_dir, reuse_workspace)
            if reuse_error:
                return None, (
                    f"attempt {attempt!r} has an unowned worktree, but it "
                    f"does not match the trusted launch intent: {reuse_error}")
            # The exact-base worktree is now authenticated and durable. It is
            # safe to create the missing agent in it without recreating Git
            # resources or resolving a live ref.
            save_state(state_dir, state)
        # U.run contains the short-lived Paseo client's environment. The
        # long-lived Paseo daemon is a separate process boundary: a live probe
        # showed its provider credentials can still reach an agent even when
        # absent from this client's environment. Paseo's --env cannot replace
        # those provider variables. Do not mistake this boundary for daemon
        # isolation; the daemon must itself be launched without ambient keys.
        argv = ["paseo", "run", "--background", "--json",
                "--cwd", str(source_repo)]
        if reuse_workspace:
            argv[argv.index("--cwd") + 1] = str(reuse_workspace)
        else:
            argv += ["--new-workspace", "worktree",
                     "--worktree-mode", "branch-off",
                     "--worktree-slug", intent["worktree_slug"],
                     "--new-branch", intent["branch"],
                     "--base", intent["base_commit"]]
        argv += ["--provider", u.get("provider") or DEFAULT_AGENT_PROVIDER,
                # The title carries the ATTEMPT id, mirroring the Slurm job
                # name, so an agent created just before a crash can be found
                # again and is never confused with a later attempt of the same
                # unit.
                 "--title", f"[swarm] {u['id']} {Path(unit_dir).name}"]
        # Artifacts remain in the external attempt root even though source
        # work runs in the checkout. Passing these automatically makes the
        # declared output location reachable without relying on prompt prose.
        argv += ["--env", f"SWARM_UNIT_ID={u['id']}",
                 "--env", f"SWARM_UNIT_DIR={unit_dir}"]
        # An agent under default permissions stops at the first Write and
        # waits for a person, which is correct behaviour and fatal to an
        # unattended DAG. The plan must therefore say what it wants, and say
        # it EXPLICITLY: a coordinator that silently bypassed permissions on
        # the user's behalf would be a worse bug than a stalled unit.
        if u.get("mode"):
            argv += ["--mode", str(u["mode"])]
        if u.get("model"):
            argv += ["--model", str(u["model"])]
        # Reasoning effort. Declared per unit, else the project default. Passed
        # for whatever provider is in use: an unknown thinking id makes paseo
        # return an errored agent rather than quietly ignoring it, so a wrong
        # value fails loudly at dispatch instead of silently downgrading the
        # work.
        thinking = u.get("thinking", DEFAULT_AGENT_THINKING)
        if thinking:
            argv += ["--thinking", str(thinking)]
        for kv in (u.get("env") or []):
            argv += ["--env", str(kv)]
        prompt = _dispatch_prompt(u, intent)
        protocol_problem = _code_protocol_problem(prompt, intent)
        if protocol_problem:
            return None, (
                f"unit {u['id']!r} code prompt has no valid completion "
                f"protocol ({protocol_problem}); refusing an unclosable "
                f"agent launch")
        # One list element, however many lines it contains. U.run does not
        # invoke a shell, and Paseo's launcher forwards its argv with "$@",
        # so newlines reach the runner as prompt content rather than argument
        # separators.
        argv.append(prompt)

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
        workspace = rec.get("cwd") or rec.get("Cwd")
        workspace_id = (_paseo_workspace_id(out)
                        or rec.get("workspaceId") or rec.get("WorkspaceId"))
        # Paseo has already acted. Persist ownership of the resource now,
        # before checks that may reject it; registration makes it cleanable
        # and does not endorse its contents. Even a malformed response with
        # no cwd gets an id/slug record rather than becoming invisible.
        workspace_meta = _register_code_workspace(
            state if state is not None else {}, u, unit_dir, workspace,
            workspace_id=workspace_id)
        if state_dir is not None:
            save_state(state_dir, state)
        if not agent:
            workspace_meta["verification"] = "refused"
            workspace_meta["cleanup_pending"] = True
            workspace_meta["verification_problem"] = (
                "Paseo returned no agent id")
            if state_dir is not None:
                save_state(state_dir, state)
            return None, (f"paseo run returned no agent id: "
                          f"{_paseo_error(out, err)}")
        if not workspace:
            workspace_meta["verification"] = "refused"
            workspace_meta["cleanup_pending"] = True
            workspace_meta["verification_problem"] = (
                f"Paseo returned agent {agent} but no worktree cwd")
            if state_dir is not None:
                save_state(state_dir, state)
            return None, (f"paseo run returned agent {agent} but no worktree "
                          f"cwd: {_paseo_error(out, err)}")
        if reuse_workspace:
            facts = trusted_launch_facts(state, u["id"], unit_dir)
            identity_error = W.workspace_identity_problem(U.run, facts)
            if str(Path(workspace).resolve()) != reuse_workspace:
                identity_error = (f"Paseo attached the recovered agent to "
                                  f"{workspace!r}, not authenticated worktree "
                                  f"{reuse_workspace!r}")
            if identity_error:
                workspace_meta["verification"] = "refused"
                workspace_meta["cleanup_pending"] = True
                workspace_meta["verification_problem"] = identity_error
                save_state(state_dir, state)
                return None, (f"paseo run returned agent {agent}, but reused "
                              f"worktree verification failed: "
                              f"{identity_error}")
        complete_err = _complete_code_launch(
            state if state is not None else {}, u, unit_dir, workspace,
            workspace_id=workspace_id, recovery=bool(reuse_workspace))
        if complete_err:
            workspace_meta["verification"] = "refused"
            workspace_meta["cleanup_pending"] = True
            workspace_meta["verification_problem"] = complete_err
            if state_dir is not None:
                save_state(state_dir, state)
            return None, (f"paseo run returned agent {agent}, but its "
                          f"worktree could not be recorded: {complete_err}")
        if state_dir is not None:
            save_state(state_dir, state)
        return str(agent), None
    return None, f"unknown kind {kind!r}"


LAUNCH_RECORD = "launch.json"


def _git(repo, *args, timeout=60):
    rc, out, err = U.run(["git", "-C", str(repo)] + list(args), timeout=timeout)
    return rc, (out or "").strip(), (err or "").strip()


def _unattended_mode_example(provider):
    """An unattended mode this provider actually accepts.

    Guidance only. The coordinator does not police the valid set: a provider
    adds modes faster than a hard-coded list is updated, and refusing a value
    that became valid is worse than passing one through to a loud rejection.
    """
    name = str(provider or DEFAULT_AGENT_PROVIDER).split("/", 1)[0].lower()
    return {"codex": "full-access", "claude": "bypass"}.get(name, "full-access")


def _workspace_policy(u):
    policy = u.get("workspace_policy") or {}
    return policy if isinstance(policy, dict) else {}


def _requires_clean_workspace(u):
    if u.get("kind") == "code":
        return True
    policy = _workspace_policy(u)
    return bool(policy.get("requires_clean_git") or policy.get("clean_git"))


def _execution_workspace(u):
    policy = _workspace_policy(u)
    return (u.get("execution_workspace") or policy.get("path")
            or u.get("repo"))


class PreflightRefusal(str):
    """A refusal the coordinator OBSERVED, carried rather than re-derived.

    `advance` used to decide whether a failed `_submit` was a preflight
    refusal by reading the launch record back off disk and looking at
    `preflight.status`. That is re-deriving a fact instead of recording it
    when it was true, and it is wrong on the re-dispatch path: the record was
    written when the workspace was CLEAN and still says "passed", so a
    workspace that went dirty afterwards produced a refusal that `advance`
    read as a generic failure and charged a retry for.

    A str subclass so every existing consumer that formats or matches the
    message keeps working; the class itself is the signal.
    """

    workspace = None
    dirty_count = 0


def _plan_workspace(u):
    """Resolve the execution workspace FROM THE PLAN, never from a record.

    The plan is trusted input; the launch record is not. It sits at
    `<unit>/launch-<attempt>.json`, one level above the attempt directory
    whose path is handed to the agent as SWARM_UNIT_DIR, and a code agent runs
    with write permissions. Reading `execution_workspace` back out of it and
    passing it to `paseo --cwd` let the agent choose its own working tree
    while `preflight.status` still said "passed" for the tree we checked.
    """
    declared = _execution_workspace(u)
    if not declared:
        return None, (f"unit {u.get('id')!r}: launch preflight requires a "
                      f"declared Git execution workspace")
    declared = str(declared)
    if not os.path.isdir(declared):
        return None, (f"unit {u.get('id')!r} declares repo {declared!r}, "
                      f"which is not a directory")
    rc, top, _ = _git(declared, "rev-parse", "--show-toplevel")
    if rc != 0:
        return None, (f"unit {u.get('id')!r} declares repo {declared!r}, "
                      f"which is not a git repository")
    return str(Path(top).resolve()), None


def _dirty_refusal(uid, workspace, dirty):
    lines = [
        f"unit {uid!r}: launch preflight refused execution workspace "
        f"{str(workspace)!r}; Git reports {len(dirty)} dirty path(s):"]
    for entry in dirty:
        status = entry.get("status", "??")
        path = json.dumps(entry.get("path", ""), ensure_ascii=True)
        if "original_path" in entry:
            old = json.dumps(entry["original_path"], ensure_ascii=True)
            lines.append(f"  [{status}] {old} -> {path}")
        else:
            lines.append(f"  [{status}] {path}")
    refusal = PreflightRefusal("\n".join(lines))
    refusal.workspace = str(workspace) if workspace is not None else None
    refusal.dirty_count = len(dirty)
    return refusal


def _repeat_launch_preflight(u):
    """Recheck cleanliness without recapturing or trusting the anchor base."""
    resolved_top, err = _plan_workspace(u)
    if err:
        return err, None
    rc, dirty = W.repo_status(U.run, resolved_top)
    if rc != 0:
        return (f"unit {u.get('id')!r}: cannot read git status in "
                f"{resolved_top!r}"), None
    if dirty:
        return _dirty_refusal(u.get("id"), resolved_top, dirty), resolved_top
    return None, resolved_top


def _code_worktree_names(unit_dir):
    """Names Paseo/Git resources from the already-random attempt id."""
    attempt = Path(unit_dir).name
    return attempt, f"swarm-{attempt}"


def _git_worktrees_on_branch(repo, branch):
    """Return worktree roots Git associates with one exact local branch."""
    rc, out, _ = _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return None
    wanted = f"refs/heads/{branch}"
    found = []
    current = None
    for line in (out + "\n").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == f"branch {wanted}" and current:
            found.append(str(Path(current).resolve()))
        elif not line:
            current = None
    return found


def _capture_code_launch(unit_dir, u):
    """Record the immutable input to Paseo's worktree creation.

    The shared checkout is a SOURCE, not the execution tree. Its dirty index
    and working files cannot enter a worktree made from an object id, so
    checking them would both block unrelated human work and prove nothing
    about the tree the agent receives. The clean-at-launch guarantee comes
    from Paseo constructing a new branch-off worktree from ``base_commit``.
    """
    repo, err = _plan_workspace(u)
    if err:
        return err, None
    target = str(u.get("target_branch") or "").strip()
    if not target:
        return (f"unit {u.get('id')!r}: no target_branch was declared; "
                f"refusing to create a source branch for a pull request with "
                f"no named destination"), None
    slug, branch = _code_worktree_names(unit_dir)
    # The generated source name depends on the attempt id, so plan validation
    # cannot know this collision. Intent construction is the first point that
    # can, and it is still before Paseo creates either an agent or worktree.
    if target == branch:
        return (f"unit {u.get('id')!r}: target_branch {target!r} is the same "
                f"as generated attempt branch {branch!r}; a pull request "
                f"cannot merge a branch into itself"), None
    # Store the source identity in the same canonical form used for Paseo's
    # returned cwd. This is an authority boundary, not a display path: a
    # relative spelling or symlink must not make the source checkout compare
    # unequal to itself later.
    repo = str(Path(repo).resolve())
    rc, head, _ = _git(repo, "rev-parse", "HEAD")
    if rc != 0:
        return (f"unit {u['id']!r}: {repo!r} has no HEAD to anchor to. "
                f"An empty repository gives nothing to transition FROM."), None
    rc, tree, _ = _git(repo, "rev-parse", head + "^{tree}")
    if rc != 0:
        return f"unit {u['id']!r}: cannot read the tree of {head[:12]}", None
    rc, remote, _ = _git(repo, "remote", "get-url", "origin")
    intent = {
        "schema_version": 1,
        "unit_id": u.get("id"),
        "attempt_id": Path(unit_dir).name,
        "repo": repo,
        "repository_remote": remote if rc == 0 else None,
        "base_commit": head,
        "base_tree": tree,
        "worktree_slug": slug,
        "branch": branch,
        "target_branch": target,
        # Makes the audit payload reproducible after a crash. Recovery can
        # compare exact expected bytes and restore the original seal without
        # trusting or laundering fields out of the file.
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return None, {"base": head, "intent": intent}


def _code_launch_intent_problem(intent, u, attempt):
    if not isinstance(intent, dict):
        return (f"unit {u.get('id')!r}: coordinator state has no worktree "
                f"launch intent for attempt {attempt!r}")
    if intent.get("unit_id") != u.get("id") or intent.get("attempt_id") != attempt:
        return (f"unit {u.get('id')!r}: worktree launch intent belongs to "
                f"unit {intent.get('unit_id')!r}, attempt "
                f"{intent.get('attempt_id')!r}")
    for key in ("repo", "base_commit", "base_tree", "worktree_slug", "branch",
                "target_branch", "captured_at"):
        if not intent.get(key):
            return (f"unit {u.get('id')!r}: worktree launch intent is "
                    f"incomplete (missing {key})")
    for key in ("base_commit", "base_tree"):
        value = intent[key]
        if (not isinstance(value, str) or len(value) not in (40, 64)
                or any(c not in "0123456789abcdef" for c in value.lower())):
            return f"unit {u.get('id')!r}: invalid trusted {key}"
    if intent["target_branch"] == intent["branch"]:
        return (f"unit {u.get('id')!r}: trusted pull-request target equals "
                f"its generated attempt branch {intent['branch']!r}")
    target = str(u.get("target_branch") or "").strip()
    if intent["target_branch"] != target:
        return (f"unit {u.get('id')!r}: trusted pull-request target "
                f"{intent['target_branch']!r} disagrees with plan target "
                f"{target!r}")
    return None


def _paseo_workspace_id(out):
    """Read the workspace id from Paseo's creation notice, if present.

    KNOWN WEAKNESS: this fallback parses free text. It is used for cleanup
    bookkeeping, never to authenticate the returned cwd, branch, base, or Git
    identity. A same-UID process can also falsify Paseo registry evidence, so
    hardening this spelling alone would not create an ownership boundary.
    """
    match = re.search(r"(?m)^Created workspace (wks_[A-Za-z0-9]+)\b", out or "")
    return match.group(1) if match else None


def _code_launch_record_payload(facts):
    """Deterministic audit bytes reconstructed only from trusted facts."""
    captured_at = facts["captured_at"]
    rec = dict(facts)
    rec.update({
        "captured_at": captured_at,
        "preflight": {
            "status": "passed",
            "checked_at": captured_at,
            "predicate": "paseo-branch-off-from-immutable-commit",
            "workspace": facts["execution_workspace"],
            "dirty_path_count": 0,
        },
        "dirty_paths_at_launch": 0,
        "dirty_paths": [],
    })
    return (json.dumps(rec, indent=1, sort_keys=True) + "\n").encode()


def _register_code_workspace(state, u, unit_dir, workspace, workspace_id=None):
    """Record a Paseo-created workspace before deciding whether to trust it.

    This is cleanup bookkeeping, not launch verification. In particular, a
    path that later fails branch, base, or Git-identity checks must remain
    visible and archivable rather than becoming an unowned Paseo resource.
    """
    attempt = Path(unit_dir).name
    us = state.setdefault("units", {}).setdefault(u["id"], {})
    intent = (us.get("attempt_launch_intents") or {}).get(attempt) or {}
    path = str(Path(workspace).resolve()) if workspace else None
    meta = {
        "path": path,
        "branch": intent.get("branch"),
        "slug": intent.get("worktree_slug"),
        "archived": False,
        "verification": "pending",
    }
    if workspace_id:
        meta["workspace_id"] = workspace_id
    us.setdefault("attempt_workspaces", {})[attempt] = meta
    return meta


def _write_code_launch_record(unit_dir, facts):
    """Write the human-readable audit copy after Paseo reports its cwd."""
    path = W.launch_record_path(unit_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _code_launch_record_payload(facts)
        seal = hashlib.sha256(payload).hexdigest()
        with open(path, "xb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        return seal, None
    except FileExistsError:
        # Crash after fsync but before save_state. Do not bless whatever is on
        # disk: compare it byte-for-byte with the deterministic payload
        # reconstructed from trusted intent + verified Paseo cwd. Exact bytes
        # restore the same seal; any difference refuses recovery.
        try:
            existing = path.read_bytes()
        except OSError as exc:
            return None, f"cannot read existing launch record {path}: {exc}"
        if existing != payload:
            return None, (
                f"existing launch record {path} does not exactly match the "
                f"trusted worktree launch facts; move it aside and retry "
                f"recovery rather than sealing changed evidence")
        return seal, None
    except OSError as exc:
        return None, f"cannot write the launch record for {facts['unit_id']!r}: {exc}"


def _complete_code_launch(state, u, unit_dir, workspace, workspace_id=None,
                          recovery=False):
    """Complete trusted launch facts from Paseo's returned worktree cwd.

    A new launch must still be exactly at its pinned base. Crash recovery is
    different: the already-running agent may honestly have committed while
    the coordinator was down, so recovery accepts an extension of that base
    but never a replacement history.
    """
    attempt = Path(unit_dir).name
    us = state.setdefault("units", {}).setdefault(u["id"], {})
    intent = (us.get("attempt_launch_intents") or {}).get(attempt)
    problem = _code_launch_intent_problem(intent, u, attempt)
    if problem:
        return problem
    workspace = str(Path(workspace).resolve())
    if workspace == intent["repo"]:
        return (f"Paseo reported the shared source checkout {workspace!r} "
                f"instead of a per-attempt worktree")
    if not os.path.isdir(workspace):
        return f"Paseo reported missing worktree {workspace!r}"
    rc, top, _ = _git(workspace, "rev-parse", "--show-toplevel")
    if rc != 0 or str(Path(top).resolve()) != workspace:
        return f"Paseo reported cwd {workspace!r}, which is not a Git worktree root"
    rc, source_common, _ = _git(intent["repo"], "rev-parse", "--git-common-dir")
    rc2, worktree_common, _ = _git(workspace, "rev-parse", "--git-common-dir")
    if rc != 0 or rc2 != 0:
        return f"cannot identify Git common directory for {workspace!r}"
    source_common = str((Path(intent["repo"]) / source_common).resolve())
    worktree_common = str((Path(workspace) / worktree_common).resolve())
    if source_common != worktree_common:
        return (f"Paseo cwd {workspace!r} is not a worktree of trusted source "
                f"repository {intent['repo']!r}")
    rc, worktree_git_dir, _ = _git(workspace, "rev-parse", "--git-dir")
    if rc != 0:
        return f"cannot identify Git metadata directory for {workspace!r}"
    worktree_git_dir = str((Path(workspace) / worktree_git_dir).resolve())
    if worktree_git_dir == worktree_common:
        return (f"Paseo cwd {workspace!r} is the repository's main checkout, "
                f"not a linked per-attempt worktree")
    # `paseo run --background` starts the agent as it creates the workspace.
    # If an unusually fast honest agent advances HEAD before this response is
    # checked, it is refused: a bounded availability failure is cheaper than
    # admitting an execution tree whose initial state was never corroborated.
    # Removing that race would require separate workspace creation followed
    # by agent creation in the verified workspace.
    rc, branch, _ = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or branch != intent["branch"]:
        return (f"Paseo worktree {workspace!r} is on branch {branch!r}, not "
                f"trusted attempt branch {intent['branch']!r}")
    rc, head, _ = _git(workspace, "rev-parse", "HEAD")
    if rc != 0:
        return f"Paseo worktree {workspace!r} has no readable HEAD"
    if recovery:
        rc, _, _ = _git(workspace, "merge-base", "--is-ancestor",
                         intent["base_commit"], head)
        if rc != 0:
            return (f"recovered Paseo worktree {workspace!r} at {head} does "
                    f"not descend from trusted base {intent['base_commit']}; "
                    f"its history was replaced rather than extended")
    elif head != intent["base_commit"]:
        return (f"Paseo worktree {workspace!r} is at {head}, not trusted "
                f"base {intent['base_commit']}")
    try:
        st = os.stat(workspace)
        common_st = os.stat(worktree_common)
        git_st = os.stat(worktree_git_dir)
    except OSError as exc:
        return f"cannot identify Paseo worktree {workspace!r}: {exc}"
    identity = {"path": workspace, "realpath": workspace,
                "device": st.st_dev, "inode": st.st_ino,
                "git_common_dir": worktree_common,
                "git_common_device": common_st.st_dev,
                "git_common_inode": common_st.st_ino,
                "git_dir": worktree_git_dir,
                "git_dir_device": git_st.st_dev,
                "git_dir_inode": git_st.st_ino}
    facts = {
        "schema_version": 2,
        "unit_id": u.get("id"),
        "attempt_id": attempt,
        "repo": intent["repo"],
        "repository_remote": intent.get("repository_remote"),
        "workspace_identity": identity,
        "execution_workspace": workspace,
        "base_commit": intent["base_commit"],
        "base_tree": intent["base_tree"],
        "branch": intent["branch"],
        "worktree_slug": intent["worktree_slug"],
        "captured_at": intent["captured_at"],
        "clean_at_launch": True,
    }
    seal, error = _write_code_launch_record(unit_dir, facts)
    if error or not seal:
        return error or "worktree launch record has no recoverable seal"
    us.setdefault("attempt_launch_facts", {})[attempt] = facts
    meta = {"path": workspace, "branch": intent["branch"],
            "slug": intent["worktree_slug"], "workspace_identity": identity,
            "archived": False}
    if workspace_id:
        meta["workspace_id"] = workspace_id
    us.setdefault("attempt_workspaces", {})[attempt] = meta
    us.setdefault("attempt_record_seals", {})[attempt] = seal
    return None


def _write_launch_record(unit_dir, u):
    """Capture the repository state BEFORE the agent exists.

    Returns an error string, or None. A unit whose policy requires clean Git
    must declare an execution workspace. Other units may explicitly record
    that no repository transition applies.

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
    # Still not inside `unit_dir`: attempt artifacts and coordinator launch
    # facts are different records and should not share a filename namespace.
    path = W.launch_record_path(unit_dir)
    repo = _execution_workspace(u)
    dirty = []
    rec = {"schema_version": 2, "unit": u.get("id"),
           "attempt": Path(unit_dir).name,
           "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    if not repo:
        # validate_plan already rejects this shape. Direct _submit callers are
        # reachable in tests and recovery helpers, so the launch chokepoint
        # must still fail closed rather than create an unchecked code agent.
        if _requires_clean_workspace(u):
            return (f"unit {u.get('id')!r}: launch preflight requires a "
                    f"declared Git execution workspace"), None
        rec["repo"] = None
        rec["execution_workspace"] = None
        rec["preflight"] = {"status": "not-required"}
        rec["note"] = ("this unit declared no Git execution workspace, so no "
                       "git transition can be judged for it")
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
        rc3, dirty = W.repo_status(U.run, repo)
        if rc3 != 0:
            return (f"unit {u['id']!r}: cannot read git status in "
                    f"{repo!r}"), None
        # The remote, so a merge receipt naming a DIFFERENT repository can be
        # refused. The receipt says "owner/name"; the anchor knows what this
        # working copy actually pushes to.
        rc4, remote, _ = _git(repo, "remote", "get-url", "origin")
        rec["remote"] = remote if rc4 == 0 else None
        resolved_top = str(Path(top).resolve())
        rc5, common_dir, _ = _git(repo, "rev-parse", "--git-common-dir")
        rc6, git_dir, _ = _git(repo, "rev-parse", "--git-dir")
        if rc5 != 0 or rc6 != 0:
            return (f"unit {u['id']!r}: cannot identify Git metadata for "
                    f"{resolved_top!r}"), None
        common_dir = str((Path(resolved_top) / common_dir).resolve())
        git_dir = str((Path(resolved_top) / git_dir).resolve())
        try:
            st = os.stat(resolved_top)
            common_st = os.stat(common_dir)
            git_st = os.stat(git_dir)
            identity = {"path": resolved_top, "realpath": resolved_top,
                        "device": st.st_dev, "inode": st.st_ino,
                        "git_common_dir": common_dir,
                        "git_common_device": common_st.st_dev,
                        "git_common_inode": common_st.st_ino,
                        "git_dir": git_dir,
                        "git_dir_device": git_st.st_dev,
                        "git_dir_inode": git_st.st_ino}
        except OSError as exc:
            return (f"unit {u['id']!r}: cannot identify execution workspace "
                    f"{resolved_top!r}: {exc}"), None
        status = "passed" if not dirty else "refused"
        rec.update({"repo": resolved_top,
                    "execution_workspace": resolved_top,
                    "workspace_identity": identity,
                    "branch": branch if rc2 == 0 else None,
                    "base_commit": head, "base_tree": tree if rc == 0 else None,
                    "clean_at_launch": not dirty,
                    "dirty_paths_at_launch": len(dirty),
                    "dirty_paths": dirty,
                    "preflight": {
                        "status": status,
                        "checked_at": rec["captured_at"],
                        "predicate": "git-status-porcelain-v1-z",
                        "workspace": resolved_top,
                        "dirty_path_count": len(dirty),
                    }})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize ONCE and seal those exact bytes. Re-serializing to compute
        # the digest would seal a second rendering, and any difference in
        # separators or key order between the two makes the seal fail on a
        # record nobody touched.
        payload = json.dumps(rec, indent=1, sort_keys=True) + "\n"
        seal = hashlib.sha256(payload.encode()).hexdigest()
        with open(path, "x") as fh:      # x: an anchor is written ONCE
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except FileExistsError:
        # Already anchored, so this is a re-dispatch of the SAME attempt.
        # I warned explicitly that retry and recovery must not bypass the
        # initial preflight, then made exactly that mistake here. Re-run the
        # dirty predicate, but never move or re-trust the anchored base.
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
        # The CURRENT predicate decides, and only it. A previous refusal
        # recorded in the file used to refuse here too, which was wrong twice
        # over: a workspace cleaned since then is legitimately dispatchable,
        # and the stale message was built by reading `dirty_paths` back out of
        # a record the launched agent can write. Both reviewers landed on this
        # function from opposite directions, one calling it a false refusal
        # and one calling it a manufactured one. It has no work left to do.
        current_refusal, _workspace = _repeat_launch_preflight(u)
        if current_refusal:
            return current_refusal, None
        # No new bytes were written, so there is no new seal to report. The
        # seal from the FIRST dispatch stands in state; re-sealing here would
        # bless whatever the file says now, which is the laundering this
        # whole mechanism exists to stop.
        return None, None
    except OSError as exc:
        return f"cannot write the launch record for {u['id']!r}: {exc}", None
    if dirty:
        return _dirty_refusal(u.get("id"), resolved_top, dirty), None
    facts = {
        "schema_version": 1,
        "unit_id": rec.get("unit"),
        "attempt_id": rec.get("attempt"),
        "repo": rec.get("repo"),
        "repository_remote": rec.get("remote"),
        "workspace_identity": rec.get("workspace_identity"),
        "execution_workspace": rec.get("execution_workspace"),
        "base_commit": rec.get("base_commit"),
        "base_tree": rec.get("base_tree"),
        "branch": rec.get("branch"),
        "clean_at_launch": rec.get("clean_at_launch"),
    }
    return None, {"base": rec.get("base_commit"), "seal": seal,
                  "facts": facts}


def _bind(unit_dir, job_id):
    rc, out, err = U.run([sys.executable, str(_HERE / "unit.py"), "bind",
                          str(unit_dir), "--job-id", str(job_id)], timeout=120)
    return None if rc == 0 else f"bind failed: {(err or out).strip()[:200]}"


def _authority_result_sink():
    """Anonymous result file with no live handle on fd 0, 1, or 2.

    The complete containment property is split deliberately across two
    chokepoints: this function removes every standard-fd alias before launch;
    U.run replaces stdin, captures stdout/stderr, closes every other fd except
    the explicit >=3 result fd, and contains timeout cleanup to the new process
    group. Thus an agent-controlled git/paseo descendant neither inherits nor
    keeps alive a handle to this file.
    """
    sink = tempfile.TemporaryFile(mode="w+b")
    if sink.fileno() >= 3:
        return sink
    duplicates = []
    try:
        promoted_fd = sink.fileno()
        while promoted_fd < 3:
            promoted_fd = os.dup(sink.fileno())
            duplicates.append(promoted_fd)
        # Do NOT pop before fdopen succeeds. fdopen takes ownership of the
        # descriptor only on success; popping first means an exception leaves
        # that one fd owned by nobody and closed by no one.
        promoted = os.fdopen(duplicates[-1], "w+b")
        duplicates.pop()
    except BaseException:
        for duplicate in duplicates:
            os.close(duplicate)
        sink.close()
        raise
    sink.close()
    for duplicate in duplicates:
        os.close(duplicate)
    return promoted


def _check(unit_dir, launch_facts=None, artifact_basis=None):
    argv = [sys.executable, str(_HERE / "unit.py"), "check", str(unit_dir)]
    # The separate judge receives the complete authority snapshot directly
    # from coordinator state. It never opens the launch audit record.
    if launch_facts:
        argv += ["--launch-facts", json.dumps(
            launch_facts, sort_keys=True, separators=(",", ":"))]
    # Same channel, same provenance, for the same reason: the pre-dispatch
    # artifact digest decides admission, so it travels BY VALUE from
    # coordinator state. Passing a path to it, or letting the checker find it
    # beside the attempt, would put the baseline where the judged party can
    # write. What an onlooker could read off this argv is a digest of files it
    # can already read; what it cannot do is change the baseline.
    if artifact_basis:
        argv += ["--artifact-basis", json.dumps(
            artifact_basis, sort_keys=True, separators=(",", ":"))]
    # Anonymous coordinator-owned storage is the authority channel. stdout
    # contains diagnostics derived from agent-writable artifacts and cannot
    # become authority merely by printing a reserved-looking prefix.
    with _authority_result_sink() as result_sink:
        result_fd = result_sink.fileno()
        argv += ["--result-fd", str(result_fd)]
        rc, out, err = U.run(
            argv, timeout=300, pass_fds=(result_fd,))
        result_sink.seek(0)
        result_channel = result_sink.read(4097).decode("ascii", "replace")
    return rc, out or "", err or "", result_channel


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


def _receipt_reason(state, unit, attempt_dir):
    """The machine-readable REASON a check came back INCOMPLETE.

    Attested, because this drives a continuation: an agent that can write its
    own REASON can ask for another turn. Existed in two copies, this one and
    an inline duplicate in `advance`, so a fix to either left the other.
    """
    rp, _why = attested_receipt(state, unit, attempt_dir)
    for note in ((rp or {}).get("notes") or []):
        if str(note).startswith("REASON="):
            return str(note).split("=", 1)[1]
    return ""


def maybe_continue(state_dir, uid, u, us, report, state=None):
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
    if (_receipt_reason(state or {}, uid, us.get("attempt_dir") or "")
            != U.REASON_NO_OUTPUTS):
        return False
    agent = us.get("job_id")
    if not agent:
        return False

    if state is None or state_dir is None:
        report.append(
            f"{uid}: refusing to send an unrecorded continuation: durable "
            f"coordinator state and state_dir are required before Paseo is "
            f"contacted")
        return False

    prompt = str(cfg.get("prompt") or
                 "Your turn ended without producing the declared outputs. "
                 "Continue the work you planned.")
    prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    continuations = us.setdefault("continuations", [])
    pending = next((entry for entry in continuations
                    if isinstance(entry, dict)
                    and entry.get("status") == "pending"
                    and entry.get("sent") is None), None)
    if pending is not None:
        if pending.get("prompt_sha256") != prompt_digest:
            report.append(
                f"{uid}: refusing to retry pending continuation "
                f"{pending.get('n')}: its recorded prompt no longer matches "
                f"the plan")
            return False
        entry = pending
        used = int(entry.get("n") or len(continuations)) - 1
    else:
        used = len(continuations)
        if used >= limit:
            return False
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "n": used + 1, "of": limit,
                 "agent_id": str(agent),
                 "prompt_sha256": prompt_digest,
                 "sent": None, "status": "pending"}
        continuations.append(entry)
        # A continuation is an external, non-idempotent act. Persist the
        # pending entry before sending it. If the coordinator dies before
        # Paseo is contacted, the same entry is retried rather than consuming
        # an unsent slot. If it dies after Paseo accepts the message but
        # before the result is saved, Paseo offers no idempotency key, so a
        # retry can duplicate the prompt; exactly-once delivery is not
        # available across that boundary.
        save_state(state_dir, state)
    rc, out, err = U.run(["paseo", "send", str(agent), prompt], timeout=120)
    entry["sent"] = rc == 0
    entry["status"] = "sent" if rc == 0 else "failed"
    if rc != 0:
        entry["error"] = (err or out or "").strip()[:200]
    else:
        us["state"] = "RUNNING"
    save_state(state_dir, state)
    if rc != 0:
        report.append(f"{uid}: continuation {used + 1}/{limit} could not be "
                      f"sent: {entry['error']}")
        return False
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
    facts = trusted_launch_facts(state, unit, attempt_dir)
    return facts.get("base_commit") if facts else None


def trusted_launch_facts(state, unit, attempt_dir):
    """Complete per-attempt launch authority, from coordinator state only."""
    if not attempt_dir:
        return None
    attempt = Path(attempt_dir).name
    us = (state.get("units") or {}).get(unit) or {}
    facts = (us.get("attempt_launch_facts") or {}).get(attempt)
    if W.launch_facts_problem(facts, attempt_dir,
                              {"id": unit, "task_id": unit}):
        return None
    return facts


def trusted_produced_head(state, unit, attempt_dir):
    """The one judgment basis pinned for this exact attempt; no fallback."""
    if not attempt_dir:
        return None
    us = (state.get("units") or {}).get(unit) or {}
    return (us.get("attempt_produced_heads") or {}).get(
        Path(attempt_dir).name)


def _capture_artifact_basis(state, unit, unit_dir, u):
    """Digest every declared artifact BEFORE anything is dispatched.

    B1, and the strongest thing this function does is happen EARLY. The done
    predicate concludes "the write root is exclusive, so an artifact found
    there was produced here", and that inference needs the write root to have
    been empty of that artifact when the attempt started. Nothing checked it,
    so a unit that declared its input path as its output recorded a file it
    never wrote as produced evidence and read DONE.

    PINNED ONCE PER ATTEMPT, and the `if attempt in bases` guard is the whole
    of it. `_submit` is re-entered on the code-launch recovery path and a
    continuation re-runs the agent inside the SAME write root; either one, if
    it re-digested, would adopt whatever the previous turn left behind as the
    baseline it is about to be judged against. That is `produced_head`'s bug
    -- a value that was never cleared and so was inherited -- and this is
    where it would have been reproduced a third time.

    The outputs come from the PLAN, never from the attempt's `unit.json`: the
    party being judged can write that file, and a baseline over a list it
    chose is a baseline it chose.
    """
    us = state.setdefault("units", {}).setdefault(unit, {})
    attempt = Path(unit_dir).name
    bases = us.setdefault("attempt_artifact_bases", {})
    if attempt in bases:
        return bases[attempt]
    declared = [str(o) for o in (u.get("outputs") or [])]
    present, missing, escaped = W.outputs_present(
        unit_dir, {"declared_outputs": declared})
    bases[attempt] = {
        "schema_version": W.ARTIFACT_BASIS_SCHEMA,
        # Both identities, so a basis lifted from another attempt or another
        # unit is useless rather than merely unlikely to be lifted.
        "attempt_id": attempt,
        "unit_id": unit,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "declared": declared,
        # Absent is the ordinary case and the strongest one: nothing was
        # there, so anything there afterwards can only be new.
        "absent": sorted(missing),
        "escaped": sorted(escaped),
        "present": U.fingerprint_outputs(unit_dir, present),
    }
    return bases[attempt]


def trusted_artifact_basis(state, unit, attempt_dir):
    """The pre-dispatch artifact digest pinned for THIS attempt; no fallback.

    Never derived, never defaulted, and never read from the attempt directory
    or the launch record. Returning None means "this attempt cannot be judged
    for production", which the checker turns into a refusal.
    """
    if not attempt_dir:
        return None
    basis = ((((state.get("units") or {}).get(unit) or {})
              .get("attempt_artifact_bases") or {}).get(Path(attempt_dir).name))
    if W.artifact_basis_problem(basis, attempt_dir,
                                {"id": unit, "task_id": unit}):
        return None
    return basis


CHECK_RESULT_PREFIX = "SWARM_CHECK_RESULT"


def _reported_check_result(result_channel):
    """Parse the sole exact authority result from its dedicated channel.

    The coordinator owns the underlying anonymous file and gives only its fd
    to the checker. stdout and stderr are diagnostics, never inputs here.

    Returns ``(result, problem)``. Exactly one line is accepted; extra text is
    malformed and more than one line is ambiguous even when values agree.
    """
    lines = (result_channel or "").splitlines()
    if not lines:
        return None, None
    if len(lines) != 1:
        return None, (f"checker result channel contained {len(lines)} lines; "
                      "exactly one is required")
    prefix = CHECK_RESULT_PREFIX + " "
    line = lines[0]
    if not line.startswith(prefix):
        return None, (f"malformed checker result {line!r}; expected "
                      f"'{CHECK_RESULT_PREFIX} ' followed by canonical JSON")
    payload = line[len(prefix):]
    try:
        result = json.loads(payload)
    except (TypeError, ValueError) as exc:
        return None, f"malformed checker result {line!r}: {exc}"
    required = {"produced_head", "receipt_sha256"}
    if not isinstance(result, dict) or set(result) != required:
        return None, ("checker result must be an object with exactly "
                      "produced_head and receipt_sha256")
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if payload != canonical:
        return None, "checker result JSON is not in canonical form"
    digest = result.get("receipt_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None, ("checker result receipt_sha256 must be exactly 64 "
                      "lowercase hexadecimal characters")
    produced = result.get("produced_head")
    if produced is not None and (
            not isinstance(produced, str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", produced) is None):
        return None, ("checker result produced_head must be null or a 40/64 "
                      "character lowercase hexadecimal object id")
    return result, None


RECEIPT_PROVENANCE_LIMIT = (
    "The coordinator has no portable process-group or cgroup handle that "
    "proves every same-UID descendant of a Paseo agent or scheduler job is "
    "dead. The digest binds the trusted checker's receipt bytes, but does "
    "not prove that an escaped background process stopped mutating other "
    "artifacts during or after the check. OS-enforced isolation requires a "
    "different principal, container, or scheduler-exposed cgroup barrier."
)


def _record_receipt_provenance(state, unit, attempt_dir, digest):
    """Record that the COORDINATOR caused the receipt now on disk.

    An agent can write `receipt.json` into its own attempt directory, and the
    report deliberately lets a receipt outrank coordinator state because a
    receipt judged artifacts. Both are reasonable and together they are a
    hole: the agent's self-report wins.

    A seal cannot close it, because a seal handed in as an argument can be
    computed by whoever hands it in. What cannot be forged is a fact the
    coordinator wrote to its OWN state, which the threat model trusts and the
    agent is never given the path to. So after a check the coordinator has
    caused, it digests the receipt that check produced and records that here.
    A receipt with no matching entry was not produced by a check this
    coordinator ran, and the report says so instead of trusting it.
    """
    if not attempt_dir:
        return
    us = state.setdefault("units", {}).setdefault(unit, {})
    if not digest:
        # The check wrote no receipt, so the coordinator vouches for NOTHING
        # here any more. Leaving an earlier seal in place let a favourable
        # receipt from a previous check keep validating: restore that file and
        # it still matches the stale seal, outranking the newer FAILED state.
        # Withdrawing the seal is the whole point of holding it.
        (us.get("attempt_receipt_seals") or {}).pop(Path(attempt_dir).name,
                                                    None)
        (us.get("attempt_receipt_provenance_limits") or {}).pop(
            Path(attempt_dir).name, None)
        return
    us.setdefault("attempt_receipt_seals", {})[Path(attempt_dir).name] = digest
    # DECLARED LIMIT AT THE ACCEPTANCE POINT. Slurm terminal accounting and a
    # terminal/idle Paseo lifecycle do not expose one portable handle with
    # which this coordinator can prove every escaped same-UID descendant is
    # gone. Do not silently upgrade this byte binding into process isolation.
    us.setdefault("attempt_receipt_provenance_limits", {})[
        Path(attempt_dir).name] = RECEIPT_PROVENANCE_LIMIT


def attested_receipt(state, unit, attempt_dir):
    """THE ONLY way to read an attempt's receipt for a decision.

    Returns (receipt, why_not). A receipt is admissible when its bytes match
    the digest the coordinator recorded for a check IT caused; otherwise the
    file is a claim by whoever wrote it, and the agent can write there.

    This exists because report.py was taught to check attestation and the FIVE
    readers in this module were not. Every one of them opened receipt.json and
    decided its own trust, which is how `produced_head` -- the commit a merge
    attestation gets pinned to -- came to be taken from an agent-writable file
    and then STORED as coordinator state. Same shape as the base_commit defect
    three review rounds ago, in a different field, found only by looking at
    the neighbours instead of the instance in front of me.

    One accessor, no fallback, and a test asserts nothing else opens the file.
    """
    if not attempt_dir:
        return None, "this unit has no attempt directory"
    recorded = ((((state.get("units") or {}).get(unit) or {})
                 .get("attempt_receipt_seals") or {})
                .get(Path(attempt_dir).name))
    if not recorded:
        return None, ("nothing records this coordinator causing a check for "
                      "attempt %s, so any receipt there is a claim by "
                      "whoever wrote it" % Path(attempt_dir).name)
    path = Path(attempt_dir) / U.RECEIPT
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    if hashlib.sha256(raw).hexdigest() != recorded:
        return None, (f"the receipt at {path} is not the file this "
                      f"coordinator's check wrote; it was replaced afterwards")
    try:
        rec = json.loads(raw)
    except ValueError as exc:
        return None, f"the receipt at {path} is not readable JSON: {exc}"
    if not isinstance(rec, dict):
        return None, f"the receipt at {path} is not a JSON object"
    attempt = Path(attempt_dir).name
    if rec.get("task_id") != unit or rec.get("attempt_id") != attempt:
        return None, (f"the attested receipt identifies unit "
                      f"{rec.get('task_id')!r}, attempt "
                      f"{rec.get('attempt_id')!r}, not unit {unit!r}, "
                      f"attempt {attempt!r}; cross-wired evidence is refused")
    return rec, None


def trusted_record_seal(state, unit, attempt_dir):
    """THE ONLY way to obtain an attempt's launch-record seal.

    Same shape and same reason as `trusted_base`: coordinator state, no
    fallback. None means "this attempt cannot be judged against its anchor",
    which callers must treat as a refusal rather than as permission to read
    the record unsealed.
    """
    if not attempt_dir:
        return None
    us = (state.get("units") or {}).get(unit) or {}
    return (us.get("attempt_record_seals") or {}).get(Path(attempt_dir).name)


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
WORKTREE_CLEANUP_STATES = frozenset(
    ("READY_FOR_PR", "DONE", "FAILED", "FAILED_EVIDENCE", "PREEMPTED"))
WORKTREE_ARCHIVE_MAX_ATTEMPTS = 3


def _paseo_json(out):
    """One implementation, in paseo_io. See that module for why."""
    return PIO.first_json_object(out)


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


def _paseo_workspace_records():
    """Return Paseo's workspace registry, or ``None`` when unreadable."""
    rc, out, _err = U.run(["paseo", "workspace", "ls", "--json"], timeout=60)
    if rc != 0:
        return None
    try:
        records = json.loads(out or "[]")
        return records if isinstance(records, list) else None
    except (ValueError, TypeError):
        return None


def _paseo_workspace_for_path(path):
    """Return Paseo's workspace id for an exact cwd, if it can be listed."""
    records = _paseo_workspace_records()
    if records is None:
        return None
    try:
        wanted = str(Path(path).resolve())
        for rec in records:
            cwd = rec.get("cwd") or rec.get("Cwd")
            if cwd and str(Path(cwd).resolve()) == wanted:
                return rec.get("workspaceId") or rec.get("WorkspaceId")
    except (TypeError, AttributeError):
        return None
    return None


def _paseo_path_ownership_problem(path):
    """Refuse attaching a new agent to a path Paseo already owns.

    Git identity establishes what checkout is at the path; it does not
    establish that an existing Paseo workspace or agent belongs to this
    attempt. Registry unavailability is therefore uncertainty, not absence.

    KNOWN LIMIT: Paseo has no conditional reservation primitive for an
    already-existing checkout. These list calls reduce accidental adoption;
    they do not close the list-then-run race against a same-UID process that
    registers this path after both observations. Re-checking would only move
    the gap. Closing it requires Paseo to atomically reserve-and-launch, or a
    different OS identity/isolation boundary.
    """
    wanted = str(Path(path).resolve())
    workspaces = _paseo_workspace_records()
    if workspaces is None:
        return ("Paseo's workspace registry is unavailable, so ownership of "
                "the path cannot be established; retry when it is readable")
    for rec in workspaces:
        if not isinstance(rec, dict):
            continue
        cwd = rec.get("cwd") or rec.get("Cwd")
        try:
            matches = bool(cwd and str(Path(cwd).resolve()) == wanted)
        except (OSError, TypeError):
            matches = False
        if matches:
            workspace_id = (rec.get("workspaceId")
                            or rec.get("WorkspaceId") or "unknown")
            return (f"Paseo already owns that path as workspace "
                    f"{workspace_id}; refusing to adopt it")

    rc, out, _err = U.run(["paseo", "ls", "--json"], timeout=60)
    if rc != 0:
        return ("Paseo's agent registry is unavailable, so ownership of the "
                "path cannot be established; retry when it is readable")
    try:
        agents = json.loads(out or "[]")
    except (ValueError, TypeError):
        agents = None
    if not isinstance(agents, list):
        return ("Paseo's agent registry is unreadable, so ownership of the "
                "path cannot be established; retry when it is readable")
    for rec in agents:
        if not isinstance(rec, dict):
            continue
        cwd = rec.get("cwd") or rec.get("Cwd")
        try:
            matches = bool(cwd and str(Path(cwd).resolve()) == wanted)
        except (OSError, TypeError):
            matches = False
        if matches:
            agent_id = rec.get("id") or rec.get("agentId") or "unknown"
            return (f"Paseo already has agent {agent_id} at that path; "
                    f"refusing to adopt it")
    return None


def _registered_attempt_workspace(intent):
    """Return all registry entries that claim this exact attempt."""
    records = _paseo_workspace_records()
    if records is None:
        return None
    matches = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        name = rec.get("name") or rec.get("Name")
        isolation = rec.get("isolation") or rec.get("Isolation")
        cwd = rec.get("cwd") or rec.get("Cwd")
        # Paseo records the branch as the workspace name and uses the slug as
        # the managed cwd's final component. Bind both independently-recorded
        # values to the launch intent before considering a title-matched agent.
        if (name == intent["branch"] and isolation == "worktree" and cwd
                and Path(cwd).name == intent["worktree_slug"]):
            matches.append(rec)
    return matches


def _recover_code_launch(state, u, unit_dir, agent):
    """Finish a launch snapshot after a crash between Paseo run and bind."""
    rc, out, err = U.run(["paseo", "inspect", str(agent), "--json"], timeout=60)
    if rc != 0:
        return f"cannot inspect recovered agent {agent}: {_paseo_error(out, err)}"
    rec = _paseo_json(out) or {}
    workspace = rec.get("Cwd") or rec.get("cwd")
    if not workspace:
        return f"recovered agent {agent} reports no cwd"
    attempt = Path(unit_dir).name
    intent = (((state.get("units") or {}).get(u["id"]) or {})
              .get("attempt_launch_intents") or {}).get(attempt)
    problem = _code_launch_intent_problem(intent, u, attempt)
    if problem:
        return problem
    try:
        agent_path = str(Path(workspace).resolve())
        agent_st = os.stat(agent_path)
    except OSError as exc:
        return f"cannot identify recovered attempt workspace: {exc}"
    agent_identity = (agent_st.st_dev, agent_st.st_ino)
    registered = _registered_attempt_workspace(intent)
    matching_registry = []
    if registered:
        for candidate in registered:
            registered_path = candidate.get("cwd") or candidate.get("Cwd")
            if not registered_path:
                continue
            try:
                registry_path = str(Path(registered_path).resolve())
                registry_st = os.stat(registry_path)
            except OSError:
                continue
            if (registry_st.st_dev, registry_st.st_ino) == agent_identity:
                matching_registry.append(candidate)
        # One registry claim is unambiguous evidence. If it identifies a
        # different inode, the title-matched agent is the wrong owner. Zero,
        # duplicate, or unreadable claims are merely unavailable evidence:
        # the trusted inode (when present) and Git intent checks below decide.
        if len(registered) == 1 and not matching_registry:
            candidate = registered[0]
            registered_path = candidate.get("cwd") or candidate.get("Cwd")
            return (f"recovered agent {agent} is attached to worktree "
                    f"{agent_path!r}, whose device/inode does not match "
                    f"Paseo's registered workspace for attempt {attempt!r} "
                    f"at {registered_path!r}")
    us = ((state.get("units") or {}).get(u["id"]) or {})
    known_facts = ((us.get("attempt_launch_facts") or {}).get(attempt) or {})
    known_meta = ((us.get("attempt_workspaces") or {}).get(attempt) or {})
    known_identity = (known_facts.get("workspace_identity")
                      or known_meta.get("workspace_identity"))
    if isinstance(known_identity, dict):
        expected = (known_identity.get("device"), known_identity.get("inode"))
        if expected != agent_identity:
            return (f"recovered agent {agent} does not own the recorded "
                    f"device/inode for attempt {attempt!r}")
    workspace_id = None
    if len(matching_registry) == 1:
        workspace_id = (matching_registry[0].get("workspaceId")
                        or matching_registry[0].get("WorkspaceId"))
    return _complete_code_launch(
        state, u, unit_dir, agent_path, workspace_id=workspace_id,
        recovery=True)


WORKTREE_CLEANUP_SUMMARY_PREFIX = "NEEDS_HUMAN -- retained worktrees:"


def _report_retained_worktrees(state, report, dry_run=False):
    """Maintain one aggregate escalation for all exhausted cleanups."""
    report[:] = [line for line in report
                 if WORKTREE_CLEANUP_SUMMARY_PREFIX not in line]
    retained = []
    for uid, us in sorted((state.get("units") or {}).items()):
        for attempt, meta in sorted((us.get("attempt_workspaces") or {}).items()):
            if isinstance(meta, dict) and meta.get("cleanup_gave_up"):
                retained.append((uid, attempt, meta.get("path"),
                                 meta.get("workspace_id") or "unknown"))
    if not retained:
        return
    paths = "; ".join(
        f"{uid}/{attempt}={path!r} (Paseo {workspace_id})"
        for uid, attempt, path, workspace_id in retained)
    line = (
        f"{WORKTREE_CLEANUP_SUMMARY_PREFIX} {len(retained)} cleanup(s) "
        f"exhausted the {WORKTREE_ARCHIVE_MAX_ATTEMPTS}-attempt bound; "
        f"automatic retries stopped. Archive or deliberately remove: {paths}")
    if dry_run:
        line += " (DRY RUN observation only; no cleanup was attempted)"
    report.append(line)


def _report_would_archive_code_worktree(state, u, unit_dir, report):
    """Describe cleanup without charging a retry or contacting Paseo."""
    if u.get("kind") != "code" or not unit_dir:
        return
    attempt = Path(unit_dir).name
    us = ((state.get("units") or {}).get(u["id"]) or {})
    meta = (us.get("attempt_workspaces") or {}).get(attempt)
    if (not isinstance(meta, dict) or meta.get("archived")
            or meta.get("cleanup_gave_up")):
        return
    target = meta.get("workspace_id") or meta.get("path") or "unknown workspace"
    line = (f"{u['id']}: DRY RUN -- would archive Paseo worktree {target} "
            f"for attempt {attempt}; no cleanup retry was charged")
    if line not in report:
        report.append(line)


def _worktree_cleanup_failed(state, u, meta, report, detail):
    tries = int(meta.get("cleanup_attempts") or 0)
    if tries < WORKTREE_ARCHIVE_MAX_ATTEMPTS:
        report.append(
            f"{u['id']}: {detail}. Cleanup attempt {tries} of "
            f"{WORKTREE_ARCHIVE_MAX_ATTEMPTS} failed")
        return
    meta.pop("cleanup_pending", None)
    meta["cleanup_gave_up"] = True
    meta["cleanup_problem"] = detail
    _report_retained_worktrees(state, report)


def _archive_code_worktree(state, u, unit_dir, report, state_dir=None):
    """Archive a finished attempt's Paseo workspace; keep its Git branch.

    Paseo owns worktree lifecycle. Archiving removes the managed checkout
    after its last active reference, while the branch remains available for
    PR creation and inspection. Failures are tried three times per worktree.
    After that automatic retries stop and one aggregate NEEDS_HUMAN report
    counts and names every retained path, rather than burying the condition
    in one message per attempt.
    """
    if u.get("kind") != "code" or not unit_dir:
        return
    attempt = Path(unit_dir).name
    us = _unit_state(state, u["id"])
    meta = (us.get("attempt_workspaces") or {}).get(attempt)
    if (not isinstance(meta, dict) or meta.get("archived")
            or meta.get("cleanup_gave_up")):
        return
    tries = int(meta.get("cleanup_attempts") or 0)
    if tries >= WORKTREE_ARCHIVE_MAX_ATTEMPTS:
        _worktree_cleanup_failed(
            state, u, meta, report, "previous worktree archive attempts failed")
        if state_dir is not None:
            save_state(state_dir, state)
        return
    meta["cleanup_pending"] = True
    meta["cleanup_attempts"] = tries + 1
    # Charge and persist this attempt before the destructive external call.
    # A crash may consume one retry, but can never reset the bound to zero.
    if state_dir is not None:
        save_state(state_dir, state)
    workspace_id = meta.get("workspace_id")
    if not workspace_id and meta.get("path"):
        workspace_id = _paseo_workspace_for_path(meta.get("path"))
        if workspace_id:
            meta["workspace_id"] = workspace_id
            # Persist newly-discovered cleanup authority before using it.
            if state_dir is not None:
                save_state(state_dir, state)
    if not workspace_id:
        if meta.get("path") and not os.path.exists(meta["path"]):
            meta["archived"] = True
            meta.pop("cleanup_pending", None)
            meta["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            if state_dir is not None:
                save_state(state_dir, state)
            return
        _worktree_cleanup_failed(
            state, u, meta, report,
            f"could not identify Paseo workspace for finished attempt "
            f"{attempt}")
        if state_dir is not None:
            save_state(state_dir, state)
        return
    rc, out, err = U.run(
        ["paseo", "workspace", "archive", str(workspace_id), "--json"],
        timeout=120)
    if rc != 0:
        _worktree_cleanup_failed(
            state, u, meta, report, f"could not archive worktree workspace "
            f"{workspace_id}: {_paseo_error(out, err)}")
        if state_dir is not None:
            save_state(state_dir, state)
        return
    meta["archived"] = True
    meta.pop("cleanup_pending", None)
    meta["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if state_dir is not None:
        save_state(state_dir, state)
    report.append(f"{u['id']}: archived finished worktree {meta.get('path')}; "
                  f"branch {meta.get('branch')} remains")


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

    # Worktrees are needed while an agent can still be continued. Once an
    # attempt has a terminal judgment (including READY_FOR_PR), its committed
    # branch is the durable handoff and the checkout is operational debris.
    # Retry failed archives on every advance; this bounds managed worktrees to
    # live/interactive attempts plus cleanup failures that remain visible.
    for uid, u in sorted(units.items()):
        us = _unit_state(state, uid)
        current_attempt = (Path(us["attempt_dir"]).name
                           if us.get("attempt_dir") else None)
        current_meta = ((us.get("attempt_workspaces") or {}).get(
            current_attempt) if current_attempt else None)
        if (us.get("state") in WORKTREE_CLEANUP_STATES
                or (isinstance(current_meta, dict)
                    and current_meta.get("cleanup_pending"))):
            if not dry_run:
                _archive_code_worktree(
                    state, u, us.get("attempt_dir"), report, state_dir)
            else:
                _report_would_archive_code_worktree(
                    state, u, us.get("attempt_dir"), report)
        for attempt, meta in (us.get("attempt_workspaces") or {}).items():
            if (attempt != current_attempt and isinstance(meta, dict)
                    and meta.get("cleanup_pending")):
                if not dry_run:
                    _archive_code_worktree(
                        state, u, attempt, report, state_dir)
                else:
                    _report_would_archive_code_worktree(
                        state, u, attempt, report)
    _report_retained_worktrees(state, report, dry_run=dry_run)
    if not dry_run:
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
        if (u.get("kind") == "code" and us.get("job_id")
                and not trusted_launch_facts(
                    state, uid, us["attempt_dir"])):
            recovery_error = _recover_code_launch(
                state, u, us["attempt_dir"], us["job_id"])
            if recovery_error:
                us["state"] = "NEEDS_HUMAN"
                us["launch_recovery_problem"] = recovery_error
                report.append(f"{uid}: NEEDS_HUMAN -- {recovery_error}. The "
                              f"agent remains bound and no launch facts were "
                              f"admitted; repair or abandon this exact "
                              f"attempt before it can be judged.")
                save_state(state_dir, state)
                continue
            us.pop("launch_recovery_problem", None)
            us["state"] = "SUBMITTED"
            report.append(f"{uid}: recovered the agent's Paseo worktree and "
                          f"completed its trusted launch snapshot")
            save_state(state_dir, state)
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
            if u.get("kind") == "code":
                recovery_error = _recover_code_launch(
                    state, u, us["attempt_dir"], job)
                if recovery_error:
                    # Bind the identity durably so the next advance asks this
                    # exact agent again instead of repeating title discovery.
                    us["job_id"] = job
                    us["launch_recovery_pending"] = True
                    us["state"] = "NEEDS_HUMAN"
                    us["launch_recovery_problem"] = recovery_error
                    report.append(f"{uid}: {note}; NEEDS_HUMAN -- "
                                  f"{recovery_error}. The agent is retained, "
                                  f"but no launch facts were admitted.")
                    save_state(state_dir, state)
                    continue
                us.pop("launch_recovery_pending", None)
                us.pop("launch_recovery_problem", None)
            # The bind marker is an external durable write. Record which job
            # it is allowed to name before creating it; a crash then retries
            # an idempotent bind instead of leaving unauthorised evidence.
            us["job_id"] = job
            us["bind_pending"] = True
            us["state"] = "SUBMITTED"
            save_state(state_dir, state)
            bind_error = _bind(us["attempt_dir"], job)
            if bind_error:
                report.append(f"{uid}: recovered {job} but {bind_error}; "
                              f"binding will retry next advance")
            else:
                us.pop("bind_pending", None)
            report.append(f"{uid}: {note}")
            save_state(state_dir, state)
        elif note == "UNKNOWN":
            report.append(f"{uid}: allocated at {us['attempt_dir']} with no "
                          f"binding, and the scheduler could not be asked "
                          f"(the query itself failed). NOT releasing the "
                          f"attempt: a failed query is not evidence that "
                          f"nothing is running. Retrying next advance.")
        elif us["state"] == "ALLOCATED":
            if u.get("kind") == "code":
                attempt = Path(us["attempt_dir"]).name
                intent = (us.get("attempt_launch_intents") or {}).get(attempt)
                if isinstance(intent, dict):
                    branch_rc, _out, _err = _git(
                        intent.get("repo"), "show-ref", "--verify", "--quiet",
                        f"refs/heads/{intent.get('branch')}")
                    if branch_rc == 0:
                        # Crash after Paseo created Git resources but before
                        # it registered an agent. _submit reuses exactly one
                        # worktree only after checking the pinned base,
                        # intended branch, and metadata identity. Route that
                        # recovery through the same durable bind protocol as
                        # every other dispatch; do not intercept it merely
                        # because the branch exists.
                        recovered_job, problem = _submit(
                            u, us["attempt_dir"], False, state, state_dir)
                        if problem:
                            us["state"] = "NEEDS_HUMAN"
                            us["launch_recovery_problem"] = problem
                            report.append(f"{uid}: NEEDS_HUMAN -- {problem}")
                            save_state(state_dir, state)
                            continue
                        us["job_id"] = str(recovered_job)
                        us["state"] = "SUBMITTED"
                        us["bind_pending"] = True
                        save_state(state_dir, state)
                        bind_error = _bind(us["attempt_dir"], recovered_job)
                        if bind_error:
                            report.append(
                                f"{uid}: recovered agent {recovered_job} but "
                                f"{bind_error}; binding will retry next advance")
                        else:
                            us.pop("bind_pending", None)
                        us.pop("launch_recovery_problem", None)
                        report.append(
                            f"{uid}: recovered exact-base worktree and "
                            f"created missing agent {recovered_job}")
                        save_state(state_dir, state)
                        continue
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
        if us.get("launch_recovery_problem"):
            report.append(f"{uid}: NEEDS_HUMAN -- "
                          f"{us['launch_recovery_problem']}")
            continue
        attempt = us["attempt_dir"]
        pinned_before = trusted_produced_head(state, uid, attempt)
        ran_check = not (u.get("kind") == "code" and pinned_before)
        protocol_problem = None
        if ran_check:
            check_result = _check(
                attempt, trusted_launch_facts(state, uid, attempt),
                trusted_artifact_basis(state, uid, attempt))
            # Old embedders may omit diagnostic stderr or the new authority
            # channel. Missing authority fails closed; stdout is never used as
            # a compatibility fallback because agent-derived notes reach it.
            if len(check_result) == 4:
                rc, stdout, stderr, result_channel = check_result
            elif len(check_result) == 3:
                rc, stdout, stderr = check_result
                result_channel = ""
            else:
                rc, stdout = check_result
                stderr = ""
                result_channel = ""
            check_report, protocol_problem = _reported_check_result(
                result_channel)
            if rc == DONE and not check_report and not protocol_problem:
                protocol_problem = (
                    "a successful checker emitted no SWARM_CHECK_RESULT "
                    "on stdout, so its receipt cannot be attributed")
            if protocol_problem:
                check_report = None
                report.append(f"{uid}: CHECK RESULT REFUSED -- "
                              f"{protocol_problem}")
            digest = ((check_report or {}).get("receipt_sha256"))
            _record_receipt_provenance(state, uid, attempt, digest)

            # The one permitted production observation happened inside this
            # check. Bind the value reported over the coordinator-controlled
            # stdout pipe directly to this attempt. The receipt is an audit
            # copy, not a transport hop for the merge basis: deleting or
            # replacing it after the checker writes cannot unmake this result.
            if check_report and u.get("kind") == "code" and rc == DONE:
                produced = check_report.get("produced_head")
                basis_problem = W.validate_pinned_head(
                    U.run, trusted_launch_facts(state, uid, attempt), produced)
                if basis_problem:
                    protocol_problem = basis_problem
                    report.append(f"{uid}: CHECK RESULT REFUSED -- "
                                  f"{basis_problem}")
                else:
                    us.setdefault("attempt_produced_heads", {})[
                        Path(attempt).name] = produced
        else:
            # Judgment already crossed the boundary for this attempt. Asking
            # the mutable ref again could replace pinned A with later C.
            rc, stdout, stderr = DONE, "", ""
        previous = us.get("state")
        us["state"] = ("FAILED_EVIDENCE" if protocol_problem
                       else NAME.get(rc, f"rc={rc}"))

        # A DECLARED convergence criterion gates DONE. Undeclared, nothing
        # changes. This is the whole reason converge.py exists: the scheduler
        # and the done predicate BOTH report success for a run that spent its
        # budget without improving, and that checkpoint must not close a
        # ticket, satisfy a dependent, or become promotable.
        #
        # NEEDS_HUMAN, not FAILED: the command did not fail. Extending the
        # budget, changing the recipe, or accepting the checkpoint anyway are
        # decisions with cost, and a coordinator that guessed among them would
        # either burn another full run or quietly accept a bad model. It also
        # keeps the unit out of the retry path and out of the settle window,
        # so nothing is auto-redone on the strength of this verdict.
        if us["state"] == "DONE" and u.get("converge"):
            verdict, why = converge_verdict(u, attempt)
            us["converge_verdict"] = verdict
            us["converge_reasons"] = list(why)
            if verdict != "CONVERGED":
                us["state"] = "NEEDS_HUMAN"
            report.append(f"{uid}: convergence {verdict} -- " +
                          " ".join(str(w) for w in why))

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
            produced = trusted_produced_head(state, uid, attempt)
            launch_facts = trusted_launch_facts(state, uid, attempt)
            immutable_problem = W.validate_pinned_head(
                U.run, launch_facts, produced) if produced else (
                    "coordinator state records no produced commit for this "
                    "attempt. Re-run the attempt; do not recover a basis from "
                    "its receipt or current branch")
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
                if immutable_problem:
                    perr = immutable_problem
                elif launch_facts and launch_facts.get("repo"):
                    _pol, policy_digest, perr = V.read_policy(
                        U.run, launch_facts["repo"], base)
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

            if immutable_problem and not vrefusal:
                vrefusal = immutable_problem
            receipt, refusal = (None, vrefusal) if vrefusal else admit_merge(
                state_dir, uid, produced,
                expect_repo=(launch_facts or {}).get("repository_remote"))
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
                reason = _receipt_reason(state, uid, us["attempt_dir"])
                if (reason == U.REASON_NO_OUTPUTS
                        and maybe_continue(state_dir, uid, u, us, report,
                                           state)):
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
        if us.get("state") in WORKTREE_CLEANUP_STATES:
            # The checker result, produced head, and terminal state are the
            # conclusion that justifies teardown. Persist that conclusion
            # before Paseo can remove the evidence used to reach it.
            if not dry_run:
                save_state(state_dir, state)
                _archive_code_worktree(
                    state, u, attempt, report, state_dir)
            else:
                _report_would_archive_code_worktree(
                    state, u, attempt, report)
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
        # BEFORE `_submit`, and persisted by the save below it, because the
        # basis is only a basis if it predates everything that could write
        # into the write root. A digest taken after dispatch is a digest of
        # the run's own output.
        _capture_artifact_basis(state, uid, unit_dir, u)
        save_state(state_dir, state)

        job_id, err = _submit(u, unit_dir, dry_run, state, state_dir)
        if err:
            # Classified from what `_submit` OBSERVED, not from re-reading
            # the launch record. On a re-dispatch the record was written while
            # the workspace was clean and still says "passed", so reading it
            # back turned a refusal into a generic failure and charged a
            # retry for an attempt that never started.
            if isinstance(err, PreflightRefusal):
                # Allocation is bookkeeping, not a started attempt. Keep the
                # receipt durably, but do not charge retry or resource budgets
                # and do not leave an attempt_dir that recovery could bind.
                launch, _launch_err = W.read_launch_record(unit_dir)
                on_disk = ((launch or {}).get("preflight") or {})
                receipt = str(W.launch_record_path(unit_dir))
                us.setdefault("preflight_refusals", []).append({
                    "attempt_dir": unit_dir,
                    # Only when the file itself records the refusal. On the
                    # re-dispatch path it records the earlier PASS, and
                    # pointing at it as the receipt for this refusal would
                    # cite a document that says the opposite.
                    "receipt": (receipt if on_disk.get("status") == "refused"
                                else None),
                    "workspace": err.workspace,
                    "dirty_path_count": err.dirty_count,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
                if us.get("attempts"):
                    last = str(us["attempts"][-1])
                    if last in (str(unit_dir), f"{DRY_PREFIX}{unit_dir}"):
                        us["attempts"].pop()
                us["attempt_dir"] = None
                us["state"] = "PREFLIGHT_REFUSED"
                us.pop("allocated_at", None)
                us["gpu_hours"] = max(
                    0.0, float(us.get("gpu_hours") or 0) - want)
                spent -= want
                cited = us["preflight_refusals"][-1]["receipt"]
                report.append(f"{uid}: {err}" +
                              (f"\n  receipt: {cited}" if cited else ""))
            else:
                us["state"] = "FAILED"
                report.append(f"{uid}: {err}")
            save_state(state_dir, state)
            continue
        # Bind EVERY kind that has an id. The old guard bound only numeric
        # ids, so a code unit's agent id never reached unit.json and its
        # predicate could never see an agent at all. A dry run still has no
        # real id to bind.
        needs_bind = bool(
            job_id and not str(job_id).startswith(("dry-", "engine-")))
        us["job_id"] = str(job_id)
        us["state"] = "SUBMITTED"
        if needs_bind:
            us["bind_pending"] = True
        # Persist the binding authority before unit.py writes its marker.
        save_state(state_dir, state)
        berr = _bind(unit_dir, job_id) if needs_bind else None
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
    existing_outbox_keys = {
        rec.get("key") for rec in read_outbox(state_dir)
        if isinstance(rec, dict)} if dry_run else set()
    for uid in sorted(units):
        us = _unit_state(state, uid)
        now = us.get("state")
        # Re-emit every durable current event. emit_intent's deterministic key
        # makes this idempotent, and closes the crash window between saving a
        # transition and appending its outbox intent: the next advance repairs
        # a missing append even when state no longer changes.
        if not now:
            continue
        evidence = None
        if now == "DONE" and us.get("attempt_dir"):
            # The verdict itself, so a drain never closes on a self-report.
            # Attested, or it is a self-report by the other party: an
            # unattested receipt shipped into the tracker as "evidence" is
            # exactly the word this field exists to earn.
            rp, _why = attested_receipt(state, uid, us["attempt_dir"])
            evidence = {"receipt": rp} if rp else None
        kind = (units.get(uid) or {}).get("kind")
        if dry_run:
            action = TRACKER_EVENTS.get(now)
            key = outbox_key(project, uid, now, us.get("attempt_dir"))
            if action and key not in existing_outbox_keys:
                verb = action[0]
                if (verb == "close"
                        and closing_evidence_for(kind) != "predicate_receipt"):
                    verb = "open_pr"
                if verb == "close" and not evidence:
                    report.append(
                        f"{uid}: DRY RUN -- would retry outbox emission for "
                        f"{now}, but no attested closing receipt is readable")
                else:
                    report.append(
                        f"{uid}: DRY RUN -- would re-emit tracker {verb} "
                        f"intent from durable state {now}")
            continue
        emit_intent(state_dir, project, uid, now, us, evidence, kind=kind)
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


def _prepare_command_paths(args, plan=None, extra_repos=(), need_root=False):
    """Apply the one external path policy before any command can write."""
    raw_state = getattr(args, "state_dir", None)
    raw_root = getattr(args, "root", None)
    try:
        state, root, _worktrees = CP.resolve_paths(
            raw_state, raw_root, plan=plan, cwd=os.getcwd(),
            extra_repos=extra_repos, need_root=(need_root or raw_state is None))
    except CP.PathPolicyError as exc:
        sys.exit(f"error: {exc}")

    # Only implicit defaults opt into implicit legacy migration. Explicit
    # destinations are never populated from a guessed source.
    if raw_state is None and root is not None:
        try:
            CP.migrate_legacy_defaults(state, root, cwd=os.getcwd())
        except OSError as exc:
            sys.exit(f"error: cannot copy legacy coordinator state to the "
                     f"external default: {exc}. Legacy files were retained.")
    args.state_dir = str(state)
    if need_root:
        args.root = str(root)
    return state, root


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
    _prepare_command_paths(args, plan=plan, need_root=True)
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


def _fsync_directory(path):
    """Persist directory-entry changes before recording their conclusion."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_promotion_receipt(state_dir, record):
    """Durably append one idempotent promotion receipt; return an error."""
    path = Path(state_dir) / PROMOTIONS
    key = record.get("promotion_key")
    try:
        if path.is_file() and key:
            for line in path.read_text().splitlines():
                try:
                    existing = json.loads(line)
                except ValueError:
                    continue
                if existing.get("promotion_key") == key:
                    # This may be recovery from a crash after the file fsync
                    # but before its directory fsync. Anchor the visible entry
                    # before allowing the pending intent to complete.
                    _fsync_directory(path.parent)
                    return None
        created = not path.exists()
        with path.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if created:
            # fsync(contents) does not make a newly-created filename durable.
            # Completion may be saved only after the parent records the entry.
            _fsync_directory(path.parent)
        return None
    except OSError as exc:
        return str(exc)


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
    # ATTESTED. Promotion copies artifacts into a shared canonical tree on a
    # human's approval, and this receipt is both the evidence it rests on and
    # what that human is shown. Read unattested, an agent could write its own
    # DONE receipt and satisfy the gate with a claim about its own work.
    receipt, err = attested_receipt(state, uid, attempt)
    if err or not isinstance(receipt, dict):
        return ([f"REFUSING: no admissible receipt for unit {uid} ({err}). "
                 f"The receipt is the evidence promotion rests on, and it "
                 f"counts only when this coordinator caused the check that "
                 f"wrote it."], False)
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

    promotion_key = hashlib.sha256(
        f"{uid}\x00{Path(attempt).name}\x00{version}".encode()).hexdigest()[:16]
    promotion_intent = {
        "key": promotion_key, "unit": uid,
        "attempt": Path(attempt).name, "destination": str(version),
        "current": str(current), "approver": approver,
        "status": "pending",
    }
    state.setdefault("promotion_intents", {})[promotion_key] = promotion_intent
    # Copying into a shared tree and swapping its public pointer are the one
    # promotion act that cannot be rolled back by coordinator state. Persist
    # the exact approved destination first; a crash can then resume the same
    # idempotent version rather than leaving an unexplained publication.
    save_state(state_dir, state)

    try:
        missing_directories = []
        cursor = version.parent
        while not cursor.exists() and cursor != cursor.parent:
            missing_directories.append(cursor)
            cursor = cursor.parent
        version.parent.mkdir(parents=True, exist_ok=True)
        # mkdir(parents=True) can create both the promotion root and the unit
        # directory. Persist every new directory entry from the first created
        # ancestor down; fsyncing only the leaf does not anchor its own name.
        for created in reversed(missing_directories):
            _fsync_directory(created.parent)
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
            # `staging` and `version` are siblings by construction, making
            # this one same-filesystem atomic rename. If the filesystem
            # refuses that guarantee (for example EXDEV), os.replace fails
            # and the canonical version is not published; there is no
            # cross-filesystem copy fallback that could expose a half-copy.
            os.replace(staging, version)      # same directory: atomic
        # Persist either the new version rename or a version recovered from a
        # crash after rename but before this fsync.
        _fsync_directory(version.parent)
        tmp_link = dest / f".current-{os.getpid()}"
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        os.symlink(Path(attempt).name, tmp_link)
        os.replace(tmp_link, current)         # atomic pointer swap
        _fsync_directory(current.parent)
    except OSError as e:
        return lines + ["", f"REFUSING: promotion failed or could not be "
                            f"made durable ({e}). No completion was recorded; "
                            f"inspect version/current and rerun this exact "
                            f"promotion."], False

    record = {"unit": uid, "attempt": Path(attempt).name,
              "promotion_key": promotion_key,
              "promoted_to": str(version), "approver": approver,
              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "outputs": recorded,
              "digest_basis": ("size-mtime only for at least one output" if weak
                               else "content digest for every output")}
    receipt_error = _append_promotion_receipt(state_dir, record)
    if receipt_error:
        # Publication already happened, so do not claim it did not. Keep the
        # durable intent pending and return failure: a rerun recognizes the
        # version, records this same keyed receipt, then marks it complete.
        lines += [f"  WARNING: promoted, but its receipt could not be "
                  f"persisted: {receipt_error}",
                  "  The promotion intent remains pending; rerun this exact "
                  "promotion to finish its audit record."]
        return lines, False
    promotion_intent["status"] = "complete"
    promotion_intent["completed_at"] = record["at"]
    save_state(state_dir, state)
    lines += ["", f"PROMOTED {uid} -> {version}", f"approved by {approver}"]
    return lines, True


def cmd_promote(args):
    plan, err = U.read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    _prepare_command_paths(args, plan=plan)
    state = load_state(args.state_dir)
    lines, ok = promote(plan, state, args.state_dir, args.unit,
                        args.approver, args.approve,
                        getattr(args, "accept_weak_evidence", False))
    for line in lines:
        print(f"  {line}" if line else "")
    return EXIT_OK if ok else EXIT_FAILED_UNIT


# Every field a unit may carry, what requires it, and what it couples to.
# ONE read instead of five successive refusals.
#
# Getting one moved unit valid took five dispatch attempts: repo, then branch,
# then the account/canary mismatch, then the canary-ancestor rule, then
# overlapping write scopes. Each message was precise about its own rule and
# said nothing about the next one, so the coupling was learned serially, and a
# field that does not exist got invented along the way from guessing at shape.
# Error messages teach one rule at a time by construction; a schema teaches the
# shape at once.
SCHEMA_FIELDS = [
    ("id", "all", "required", "unique; names the attempt directory and the "
     "env var SWARM_DEP_<ID>"),
    ("kind", "all", "required", "slurm | pipeline | code; fixes what closes "
     "the unit and cannot be overridden per plan"),
    ("command", "slurm, pipeline", "required",
     "the WORK. Never sbatch/srun/salloc: the coordinator submits it. An "
     "absolute path or glob in its ARGUMENTS is refused only when its "
     "parent directory is visible on THIS host and does not contain a match; "
     "a path under a mount only the compute node has is not refused, and the "
     "program in the first token is never checked"),
    ("prompt", "code", "required",
     "the agent's instruction, and the runner's last positional argument. A "
     "paseo flag at its start is refused; configuration goes in fields"),
    ("outputs", "all", "required",
     "RELATIVE to the attempt write root. The predicate looks nowhere else. "
     "Refused with --array, which fans N writers into one directory"),
    ("inputs", "all", "optional",
     "checked for existence, placeholders and upstream production"),
    ("needs", "all", "optional",
     "DAG edges; a unit dispatches only after every dependency is DONE"),
    ("repo", "code", "required", "closure is a merged PR; without it DONE is "
     "unreachable"),
    ("target_branch", "code", "required",
     "the destination of the attempt's pull request. The coordinator creates "
     "a separate swarm-<attempt> source branch; legacy branch is not reused"),
    ("mode", "code", "required",
     "no default on purpose. Absent or empty means default permissions, so "
     "the agent stalls at its first write"),
    ("provider", "code", "optional",
     "default codex/gpt-5.6-sol"),
    ("model", "code", "optional", "overrides the provider's default"),
    ("thinking", "code", "optional",
     "default high. JSON null or \"\" suppresses the flag; the STRING "
     "\"null\" is refused"),
    ("env", "code", "optional", "list of KEY=VALUE passed to the agent"),
    ("continuation", "code", "optional",
     '{"max": N, "prompt": "..."}; bounded nudges when it settles without '
     "producing. Exhaustion FAILS the unit"),
    ("requires_verification", "code", "optional",
     "claims an authorized verifier must establish before closing"),
    ("runtime", "slurm, pipeline", "required",
     'inline or a "runtimes" id, or the literal "none". Declares resolution, '
     "entrypoint, probe and verified_by"),
    ("sbatch", "slurm", "optional",
     "a LIST of scheduler flags. A string is iterated character by character"),
    ("write_scopes", "all", "optional",
     "must not overlap between concurrent units. Names FILES; does NOT "
     "isolate a code unit's repository"),
    ("workspace_policy", "slurm, pipeline", "optional",
     '{"requires_clean_git": true, "path": "/checkout"}; opt-in launch '
     "preflight for a non-code unit"),
    ("promote_to", "all", "optional",
     "where verified outputs are published. Needs a named approver"),
    ("max_attempts", "all", "optional",
     "default 1. Above 1 requires a retry contract with max_lost"),
    ("retry", "all", "optional",
     '{"mode": "restart", "max_lost": {...}}. "resume" is REFUSED'),
    ("gpu_hours", "all", "optional", "charged against the plan's budget"),
    ("pool", "all", "optional", "must be declared in limits.pools"),
    ("converge", "slurm, pipeline", "optional",
     '{"metrics": "metrics.jsonl", "criterion": {...}, "diverge": [...], '
     '"budget": N}. Scores a criterion over the metrics SERIES and gates '
     "DONE: a run that spent its budget without meeting it is NEEDS_HUMAN, "
     "not DONE, so it closes no ticket and satisfies no dependent. The "
     "metrics file must also be a declared output, and the criterion is read "
     "from the plan, never from the attempt directory"),
]

# What couples to what, stated once. These are the rules that only announce
# themselves as a refusal.
SCHEMA_COUPLINGS = [
    "A canary must match its unit's runtime identity, partition AND account, "
    "and be a DAG ancestor of it. A plan spanning two partitions needs one "
    "canary per partition.",
    "A canary must run the runtime's declared probe command verbatim; a "
    "canary running `true` establishes nothing.",
    "Concurrent units must have disjoint write_scopes; order them with needs "
    "if they overlap.",
    "max_attempts above 1 needs retry.max_lost in a metric the plan also caps "
    "in retry_limits.",
    "An input is satisfied by an upstream unit's output only when that unit "
    "is an ancestor.",
    "converge.metrics must also appear in outputs, so the predicate checks "
    "the file exists before convergence is judged over it.",
    "converge is refused on kind=code: a code unit is closed by a merged pull "
    "request and has no metrics series.",
]


def cmd_schema(args):
    """Print the unit schema: fields, when required, and what they couple to."""
    if args.json:
        print(json.dumps(
            {"fields": [{"field": f, "kinds": k, "requirement": r,
                         "notes": n} for f, k, r, n in SCHEMA_FIELDS],
             "couplings": SCHEMA_COUPLINGS}, indent=2))
        return EXIT_OK
    width = max(len(f) for f, _k, _r, _n in SCHEMA_FIELDS)
    print("  UNIT FIELDS\n")
    for field, kinds, req, note in SCHEMA_FIELDS:
        print(f"  {field:<{width}}  {req:<8}  {kinds}")
        for line in _wrap(note, 66):
            print(f"  {'':<{width}}  {'':<8}  {line}")
        print()
    print("  COUPLINGS between fields\n")
    for c in SCHEMA_COUPLINGS:
        lines = _wrap(c, 72)
        print(f"  - {lines[0]}")
        for line in lines[1:]:
            print(f"    {line}")
    return EXIT_OK


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def cmd_verify(args):
    """Run an authorized, content-pinned verifier and record what it said.

    Everything that makes this admissible happens HERE, in one place, and the
    receipt records all of it: the policy came from the anchored base commit,
    the verifier's bytes hashed to what the policy authorized, those exact
    bytes ran, and the head it ran against is the one this attempt produced.
    """
    _prepare_command_paths(args)
    state = load_state(args.state_dir) or {}
    launch_facts = trusted_launch_facts(
        state, args.unit, args.attempt)
    if not launch_facts:
        sys.stderr.write(
            f"error: coordinator state records no complete launch snapshot "
            f"for unit {args.unit!r}, attempt {Path(args.attempt).name!r}. "
            f"Re-dispatch it; verification may not reconstruct repository "
            f"identity or a base from the launch record.\n")
        return EXIT_USAGE
    repo = launch_facts["repo"]
    base = launch_facts["base_commit"]
    _prepare_command_paths(args, extra_repos=[repo])
    try:
        load_verifications(args.state_dir)
    except OutboxError as exc:
        sys.stderr.write(f"  VERIFICATION JOURNAL: {exc}\n")
        return EXIT_CONFLICT

    if not repo:
        sys.stderr.write("error: this attempt anchored no repository, so "
                         "there is no base to read a policy from.\n")
        return EXIT_USAGE

    # Attempt identity is part of the lookup. Check it before touching a
    # verifier path so a request for the wrong attempt fails for the actual
    # authority defect and cannot be obscured by an unrelated file error.
    produced = trusted_produced_head(state, args.unit, args.attempt)
    if not produced:
        sys.stderr.write(
            f"error: coordinator state records no judged produced commit for "
            f"attempt {Path(args.attempt).name!r}. Re-run that attempt; do "
            f"not recover a basis from another attempt, its receipt, or the "
            f"current branch.\n")
        return EXIT_USAGE
    basis_problem = W.validate_pinned_head(U.run, launch_facts, produced)
    if basis_problem:
        sys.stderr.write(f"error: {basis_problem}\n")
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
    _prepare_command_paths(args)
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
    _prepare_command_paths(args)
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
                                   "READY_FOR_PR", "PREFLIGHT_REFUSED")]
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
    _prepare_command_paths(args, plan=plan)
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
        p.add_argument("--state-dir", default=None,
                       help="external coordinator state directory. Default: "
                            "a per-project directory under XDG_STATE_HOME or "
                            "~/.local/state")
        p.add_argument("--root", default=None,
                       help="external attempt root beside the default state "
                            "directory")
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
    pr.add_argument("--state-dir", default=None)
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

    sc = sub.add_parser("schema", help="every unit field, when it is "
                                      "required, and what it couples to")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(fn=cmd_schema)

    v = sub.add_parser("verify", help="run an authorized pinned verifier")
    v.add_argument("--state-dir", default=None)
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
    m.add_argument("--state-dir", default=None)
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
    o.add_argument("--state-dir", default=None)
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
    s.add_argument("--state-dir", default=None)
    s.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
