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
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
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
import recovery as R  # noqa: E402  audit-only worktree preservation

STATE_FILE = "swarm-state.json"
STATE_EPOCH_FILE = "state-epoch.json"
KINDS = U.KINDS

# What a `code` unit runs unless it says otherwise.
#
# `codex/gpt-6-astra` at `high`, by owner decision on 2026-09-24, replacing
# `codex/gpt-5.6-sol`. Provider, model and thinking id were read off a live
# agent before this default changed, not guessed: a canary launched with
# `paseo run --provider codex --model gpt-6-astra --thinking high` inspected as
# Provider codex, Model gpt-6-astra, Thinking high, and answered. That check is
# the bar, because paseo answers an unknown thinking id with an ERRORED agent,
# and a default that fails at dispatch is worse than no default.
#
# There is deliberately no fallback to another model. A silent fallback would
# dispatch a model the plan never declared, and a host that cannot serve this
# one fails loudly instead: paseo returns an ERRORED agent at dispatch. The
# live check above ran on the coordinator's host; a host that has not run it
# should, and on 2026-09-24 chimera could not run ANY codex model, sol included,
# because its codex login token had expired.
#
# A unit overrides any of it with `provider`, `model` or `thinking`. Setting
# `thinking` to null or "" turns the flag off entirely for a provider that has
# no such option.
DEFAULT_AGENT_PROVIDER = "codex/gpt-6-astra"
DEFAULT_AGENT_THINKING = "high"

# Reasoning effort belongs to the MODEL, not to the project. One project-wide
# value was wrong the moment the roster held more than one model: on `bus
# models`' measured intelligence luna sits well below sol and opus, so it is
# asked for xhigh to compensate while the two leaders run at high. Asking the
# leaders for xhigh buys latency, not quality.
#
# Every id here was read off a live agent on 2026-09-04, not guessed, because
# paseo answers an unknown thinking id with an ERRORED agent. Two results are
# worth keeping: `high` resolves on claude, whose bare default is `auto`, so
# the value that looks like a no-op is the one that silently changes the run;
# and `claude/opus` is an alias paseo expands to `claude-opus-5`, which is why
# both spellings are keys.
THINKING_BY_MODEL = {
    "codex/gpt-6-astra": "high",
    "codex/gpt-5.6-sol": "high",
    "codex/gpt-5.6-luna": "xhigh",
    "claude/opus": "high",
    "claude/claude-opus-5": "high",
}


def default_thinking_for(u):
    """The reasoning effort one unit gets when it declares none.

    Keyed on the model that will actually run, which is the provider string
    unless the unit names a `model` separately -- both spellings reach paseo
    the same way, so both have to resolve here or the mapping would apply to
    one plan and not its equivalent. An unrecognised model falls back rather
    than refusing: a new model on the roster should dispatch at a sane effort,
    and the fallback is the level the two strongest models use.
    """
    provider = str(u.get("provider") or DEFAULT_AGENT_PROVIDER)
    model = u.get("model")
    key = "%s/%s" % (provider.split("/", 1)[0], model) if model else provider
    return THINKING_BY_MODEL.get(key, DEFAULT_AGENT_THINKING)

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

# Advisory scope-check outcomes; neither nonzero value is merge permission.
EXIT_SCOPE_OUTSIDE = 1
EXIT_SCOPE_UNCHECKED = 2


class OutboxError(Exception):
    """The receipt journal cannot be written or read safely."""


class PlanError(Exception):
    pass


# --- plan validation ------------------------------------------------------
def declared_seed(u):
    """Validate optional code provenance without rewriting any deciding byte."""
    if "seed" not in u:
        return None
    seed = u["seed"]
    prefix = f"unit {u.get('id', '?')!r}: seed"
    if not isinstance(seed, dict):
        raise PlanError(prefix + " must be a JSON object")
    if u.get("kind") != "code":
        raise PlanError(prefix + " is supported only for kind=code")
    if set(seed) - {"ref", "base", "head", "evidence"}:
        raise PlanError(prefix + " has unknown fields; use ref, base, head, evidence")
    ref = seed.get("ref")
    if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
        raise PlanError(prefix + ".ref must be a full refs/heads/... branch ref")
    rc, _, _ = U.run(["git", "check-ref-format", ref])
    if rc != 0:
        raise PlanError(prefix + ".ref is not a valid refs/heads/... branch ref")
    for field in ("base", "head"):
        value = seed.get(field)
        if (not isinstance(value, str)
                or re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value) is None):
            raise PlanError(prefix + "." + field + " must be a full 40/64-hex commit id")
    if "evidence" in seed:
        value = seed["evidence"]
        if not isinstance(value, str) or not value or "\0" in value:
            raise PlanError(prefix + ".evidence must be a non-empty path string without NUL")
    return seed


def declared_scope(u):
    """Return optional fnmatch patterns without normalizing deciding bytes."""
    if "scope" not in u:
        return None
    patterns = u["scope"]
    if (not isinstance(patterns, list)
            or any(not isinstance(p, str) for p in patterns)):
        raise PlanError(f"unit {u.get('id', '?')!r}: scope must be a JSON "
                        "list of strings")
    for pattern in patterns:
        if (not pattern or "\0" in pattern
                or pattern.startswith(("/", "\\"))
                or re.match(r"^[A-Za-z]:", pattern)
                or ".." in re.split(r"[/\\]", pattern)):
            raise PlanError(f"unit {u.get('id', '?')!r}: scope pattern "
                            f"{pattern!r} must be repo-relative, non-empty, "
                            "and contain no '..' segments")
    return patterns


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


# --- what the survey already knows ----------------------------------------
#
# `survey.py` records this cluster BEFORE the plan is written: whether the
# scheduler has a default memory, and who each partition will accept. Three
# parts of the system agreed those facts matter -- the survey computes them,
# the skill tells the planner to plan around them, the report prints them --
# and the one component that can refuse a plan never looked. A unit charging
# an account its partition denies validated clean and then spent hours in
# QOSGrpCpuLimit beside 202 idle CPUs.
#
# THE RULE FOR `unknown` IS THE WHOLE DESIGN. Every allowance is recorded as
# {"state": "set"|"unrestricted"|"unknown", "value": ...}, and unknown means
# the query did not answer -- scontrol was missing, sacctmgr failed, the
# survey predates the field. A refusal built on that is a guess wearing a
# number: it blocks honest work on the first flaky day, and a validator that
# cries wolf gets switched off. So unknown refuses NOTHING here. It is not
# reported as satisfied either: silence from these checks means "not
# checked", which is why `validate` names the survey it read, or says it read
# none.
SURVEY_FILE = os.path.join(".swarm", "survey.json")

# The one artifact hanig-project's report renders as the project's own
# claims. Named here because validate now has an opinion about it; see the
# findings block in validate_plan for what that opinion is and is not.
FINDINGS_FILE = "findings.json"

# survey.py's vocabulary, imported as words rather than as a habit.
LIMIT_SET, LIMIT_OPEN, LIMIT_UNKNOWN = "set", "unrestricted", "unknown"

# The caveat that must ride along with every one of these judgements. Said
# in the refusal itself, because that is the message someone reads while
# choosing the partition they will move the unit to.
QOS_CAVEAT = (
    "Note that `qos_grptres` in the survey resolves the PARTITION QOS only: "
    "an account or association QOS can impose a GrpTRES nothing here can "
    "see, so a partition that accepts this account is still not a promise "
    "the job will run.")


def read_survey(path):
    """One survey file as a mapping. Returns (survey, error)."""
    data, err = U.read_json(path)
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "it is not a JSON object"
    return data, None


def discover_survey(plan_path=None, cwd=None):
    """The survey recorded beside a plan. Returns (survey, note).

    `None` is the answer when there is nothing to read, and the note says
    which file was looked for or why the one found was not used. A validator
    with no survey knows nothing about this cluster's allowances, and that is
    `unknown`, which refuses nothing.

    Two candidates, in order: the plan's own directory, then the working
    directory, both at `.swarm/survey.json` -- the path hanig-project's very
    first command writes.

    A survey is an observation of ONE host. Applying another machine's
    allowances here would refuse a unit on evidence about a different
    cluster, so a hostname mismatch is unknown rather than authority. A
    survey named explicitly on the command line is used as given: that is the
    operator asserting it applies.
    """
    here = os.uname().nodename
    tried = []
    for base in (os.path.dirname(os.path.abspath(plan_path)) if plan_path
                 else None, cwd or os.getcwd()):
        if not base:
            continue
        cand = os.path.join(base, SURVEY_FILE)
        if cand in tried:
            continue
        tried.append(cand)
        if not os.path.isfile(cand):
            continue
        data, err = read_survey(cand)
        if err:
            return None, (f"the survey at {cand} could not be read ({err}), "
                          f"so nothing in it was applied")
        machine = data.get("machine") or {}
        host = str(machine.get("hostname") or "").strip()
        if host and host != here:
            return None, (f"the survey at {cand} was taken on {host}, not "
                          f"{here}. A survey is evidence about the host it "
                          f"ran on, so nothing in it was applied here; pass "
                          f"--survey to use it deliberately")
        return data, cand
    return None, (f"no survey was read (looked for {' and '.join(tried)}). "
                  f"Record one with hanig-project's "
                  f"`survey.py --repo . --out {SURVEY_FILE}`")


def _scheduler_facts(survey):
    sched = (survey or {}).get("scheduler")
    return sched if isinstance(sched, dict) else {}


def surveyed_partition(survey, name):
    """The surveyed record for ONE partition, or None if the survey is silent
    about it. Silence is unknown: `partitions_unavailable` and a partition
    the query never listed are both "nothing was established here"."""
    for part in (_scheduler_facts(survey).get("partitions") or []):
        if isinstance(part, dict) and part.get("partition") == name:
            return part
    return None


def limit_state(part, field):
    """(state, value) for one allowance field, in the survey's vocabulary.

    An ABSENT field is unknown, not unrestricted. schema_version 1 predates
    the allowance block entirely, so a survey that never asked the question
    must not read as one that asked and found no restriction."""
    lim = (part or {}).get(field)
    if not isinstance(lim, dict) or lim.get("state") not in (
            LIMIT_SET, LIMIT_OPEN, LIMIT_UNKNOWN):
        return LIMIT_UNKNOWN, None
    return lim["state"], lim.get("value")


def _account_names(value):
    """An allowance's value as a list of account names, or None.

    A bare string here would make `account in value` a SUBSTRING test, so
    `lab` would pass an allow-list of `goodarzilab` and a deny-list naming
    `goodarzilab` would refuse `lab`. Anything that is not a list is not an
    account list, and not-a-list is unknown."""
    if isinstance(value, (list, tuple)):
        return [str(a) for a in value]
    return None


# Slurm's three ways to state a job's memory. `--mem` is MUTUALLY EXCLUSIVE
# with `--mem-per-cpu` at submission, so insisting on `--mem` by name would
# refuse a unit that has already answered the question in the only other
# spelling it is allowed to use.
MEM_FLAGS = ("--mem", "--mem-per-cpu", "--mem-per-gpu")


def declared_memory(u):
    """The memory request a unit makes, or None. Both spellings, as with
    partition and account: `--mem=64G` and `--mem 64G`.

    A flag with no value (`--mem=` or a trailing `--mem`) is not a request --
    sbatch rejects it -- but it does not mask a later, well-formed one."""
    args = [str(a) for a in (u.get("sbatch") or [])]
    for i, a in enumerate(args):
        name, sep, value = a.partition("=")
        if name not in MEM_FLAGS:
            continue
        if sep:
            if value.strip():
                return a
        elif i + 1 < len(args) and not args[i + 1].startswith("-"):
            return f"{a} {args[i + 1]}"
    return None


def memory_flag_problems(units, survey):
    """Which slurm units state no memory on a cluster that has no default.

    `mem_flag_required` is the survey's reading of `DefMemPerNode`. Absent,
    it is UNKNOWN -- `scontrol show config` did not answer, or the survey
    predates the field -- and unknown refuses nothing and satisfies nothing.
    """
    sched = _scheduler_facts(survey)
    if not sched.get("present") or "mem_flag_required" not in sched:
        return []
    if not sched.get("mem_flag_required"):
        return []                       # answered: this cluster has a default
    return [u.get("id", "?") for u in units
            if isinstance(u, dict) and u.get("kind") == "slurm"
            and not declared_memory(u)]


def account_problems(units, survey):
    """Which units charge an account their declared partition will not take.

    BOTH HALVES of the rule, because Slurm prints `DenyAccounts` INSTEAD of
    `AllowAccounts`: a partition that denies this account reads as wide open
    to anyone who consults only the allowance. Checked only where the survey
    ANSWERED -- `unknown` on one field still lets the other decide, and
    `unknown` on both decides nothing.

    Returns [(unit id, account, partition, what the survey says)].
    """
    bad = []
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "slurm":
            continue
        account, name = declared_account(u), declared_partition(u)
        if not account or not name:
            # An undeclared account is the association default and an
            # undeclared partition is the cluster default. Neither is in the
            # plan, so neither can be checked against the survey.
            continue
        part = surveyed_partition(survey, name)
        if part is None:
            continue
        state, value = limit_state(part, "deny_accounts")
        denied = _account_names(value)
        if state == LIMIT_SET and denied is not None and account in denied:
            bad.append((u.get("id", "?"), account, name,
                        f"the survey records deny_accounts="
                        f"{', '.join(denied)} on that partition, so the job "
                        f"is refused there however long it queues"))
            continue
        state, value = limit_state(part, "allow_accounts")
        allowed = _account_names(value)
        if (state == LIMIT_SET and allowed is not None
                and account not in allowed):
            bad.append((u.get("id", "?"), account, name,
                        f"that partition's allow_accounts is set to "
                        f"{', '.join(allowed)}, which does not include it"))
    return bad


RESOLUTIONS = ("direct", "path", "conda", "container", "module", "uv",
               "wrapper")

# An isolation profile is deliberately narrower than a general container
# launcher.  These two backends expose the same bind/contain interface on the
# clusters this skill targets.  Docker being installed on a submit host is not
# enough: its daemon, user mapping, environment forwarding, and mount host are
# separate facts, so silently translating this contract to `docker run` would
# claim an enforcement path the plan did not declare.
ISOLATION_BACKENDS = ("apptainer", "singularity")
ISOLATION_KEYS = frozenset(
    {"kind", "backend", "image", "writable", "read_only"})
ISOLATION_ROOT_TOKENS = ("$SWARM_UNIT_DIR", "${SWARM_UNIT_DIR}")
ISOLATION_SHELL_EXECUTABLES = frozenset({
    "sh", "ash", "bash", "dash", "zsh", "ksh", "mksh", "posh", "yash",
    "csh", "tcsh", "fish",
})
ISOLATION_MULTICALL_EXECUTABLES = frozenset({"busybox", "toybox"})
ISOLATION_LAUNCHER_EXECUTABLES = frozenset({
    "env", "nice", "nohup", "setsid", "stdbuf", "timeout",
})
ISOLATION_MARKER_NAME = ".swarm-isolation-applied-v1"


def _unquoted_shell_syntax(command):
    """Return shell syntax outside single quotes (and expansions in double)."""
    quote = None
    escaped = False
    word_start = True
    syntax = ";&|<>(){}\n\r\x00$`*?["
    for char in command:
        if escaped:
            escaped = False
            word_start = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char == "\\":
                escaped = True
            elif char in "$`":
                return char
            continue
        if char in "'\"":
            quote = char
            word_start = False
        elif char == "\\":
            escaped = True
        elif char.isspace():
            word_start = True
        elif char in "#~" and word_start:
            return char
        elif char in syntax:
            return char
        else:
            word_start = False
    return None


def _isolation_command_argv(command):
    """Parse the deliberately small command language an isolated unit uses.

    A container runtime accepts an argv, while an ordinary unit command is a
    shell program.  Treating the latter as the former silently changes shell
    expansion, and putting another shell inside the image both assumes that
    image contains one and detaches background work from the pipeline
    wrapper's process tree.  Refuse shell programs and preserve simple argv
    commands exactly through shlex instead.
    """
    if not isinstance(command, str) or not command.strip():
        return None, "its command must be a non-empty string"
    # This is intentionally conservative.  Every spelling below has shell
    # semantics that direct exec cannot preserve without assuming an
    # interpreter exists inside the declared image.
    found = _unquoted_shell_syntax(command)
    if found is not None:
        return None, (
            f"its command contains shell syntax {found!r}; isolated commands "
            f"must be one executable plus literal arguments")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as e:
        return None, f"its command cannot be parsed as a direct argv: {e}"
    if not argv:
        return None, "its command has no executable"
    executable = os.path.basename(argv[0])
    if executable in ISOLATION_SHELL_EXECUTABLES:
        return None, (
            f"its executable {argv[0]!r} is a shell; isolated commands must "
            f"run the workload directly, not an image-side interpreter")
    if executable in ISOLATION_LAUNCHER_EXECUTABLES:
        return None, (
            f"its executable {argv[0]!r} is a process launcher that can hide "
            f"a shell; isolated commands must run the workload directly")
    if (executable in ISOLATION_MULTICALL_EXECUTABLES and len(argv) > 1
            and os.path.basename(argv[1]) in ISOLATION_SHELL_EXECUTABLES):
        return None, (
            f"its executable sequence {argv[:2]!r} selects a shell; "
            f"isolated commands must run the workload directly")
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        return None, (
            "its command starts with a shell environment assignment; put "
            "configuration in the workload or image instead")
    return argv, None


def isolation_problem(u):
    """Why a unit's optional host-write isolation cannot be enforced.

    This validates declarations only.  It does not choose a backend from PATH,
    infer one from the image spelling, or claim that a submit-host executable
    exists on a compute node.  Runtime availability remains a runtime/canary
    fact; the shape of the writable host surface is knowable here.
    """
    profile = u.get("isolation")
    if profile is None:
        return None
    uid = u.get("id", "?")
    if u.get("kind") not in _NEEDS_RUNTIME:
        return (f"unit {uid!r} is kind={u.get('kind')!r} and declares "
                f"'isolation'. Only slurm and pipeline workloads are launched "
                f"through a container backend; code units run in Paseo "
                f"worktrees and cannot enforce this profile.")
    if not isinstance(profile, dict):
        return (f"unit {uid!r} has isolation={profile!r}, a "
                f"{type(profile).__name__}; it must be an object")
    unknown = sorted(set(profile) - ISOLATION_KEYS)
    if unknown:
        return (f"unit {uid!r} isolation has unrecognised key(s) "
                f"{', '.join(unknown)}; it reads only "
                f"{', '.join(sorted(ISOLATION_KEYS))}. A misspelled boundary "
                f"must be refused rather than silently dropped.")
    if profile.get("kind") != "container":
        return (f"unit {uid!r} isolation.kind={profile.get('kind')!r}; the "
                f"only enforced kind is 'container'.")
    backend = profile.get("backend")
    if backend not in ISOLATION_BACKENDS:
        return (f"unit {uid!r} isolation.backend={backend!r}; use one of "
                f"{', '.join(ISOLATION_BACKENDS)}. The coordinator never "
                f"selects a backend from PATH or translates a profile to a "
                f"different runtime.")
    image = profile.get("image")
    if not isinstance(image, str) or not image.strip():
        return f"unit {uid!r} isolation declares no container image."
    is_uri = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", image) is not None
    if (image.lstrip().startswith("-") or "\n" in image or "\r" in image
            or _looks_unresolved(image)
            or not (os.path.isabs(image) or is_uri)):
        return (f"unit {uid!r} isolation.image={image!r} is not a concrete "
                f"absolute path or container URI, or is parsed as a runtime "
                f"option.")

    for output in (u.get("outputs") or []):
        if isinstance(output, str) and os.path.normpath(output) == (
                ISOLATION_MARKER_NAME):
            return (f"unit {uid!r} output {output!r} is reserved for the "
                    f"coordinator's isolation application evidence. Choose "
                    f"a different declared output path.")

    writable = profile.get("writable")
    if not isinstance(writable, list):
        return (f"unit {uid!r} isolation.writable={writable!r}; it must be a "
                f"list containing exactly '$SWARM_UNIT_DIR'.")
    if len(writable) != 1 or writable[0] not in ISOLATION_ROOT_TOKENS:
        return (f"unit {uid!r} isolation.writable must resolve to the attempt "
                f"root and NOTHING ELSE. Declare exactly "
                f'["$SWARM_UNIT_DIR"]; got {writable!r}.')

    read_only = profile.get("read_only")
    if not isinstance(read_only, list):
        return (f"unit {uid!r} isolation.read_only={read_only!r}; it must be "
                f"a list naming every declared input.")
    inputs = u.get("inputs") or []
    normalized = []
    for path in read_only:
        if not isinstance(path, str) or not path.strip():
            return (f"unit {uid!r} isolation.read_only contains {path!r}; "
                    f"every bind source must be a non-empty absolute path.")
        if (not os.path.isabs(path) or _looks_unresolved(path)
                or any(ch in path for ch in "*?[\n\r,:")):
            return (f"unit {uid!r} isolation.read_only path {path!r} cannot "
                    f"be enforced as one unambiguous read-only bind. Use a "
                    f"concrete absolute path without glob, template, comma, "
                    f"or colon syntax.")
        norm = os.path.normpath(path)
        if norm == os.path.sep:
            return (f"unit {uid!r} isolation.read_only may not bind the host "
                    f"filesystem root into the container.")
        normalized.append(norm)
    if len(set(normalized)) != len(normalized):
        return f"unit {uid!r} isolation.read_only contains duplicate binds."

    declared = []
    for path in inputs:
        if not isinstance(path, str):
            return (f"unit {uid!r} input {path!r} cannot be matched to an "
                    f"isolation.read_only bind.")
        declared.append(os.path.normpath(path))
    if sorted(normalized) != sorted(declared):
        return (f"unit {uid!r} isolation.read_only must name exactly its "
                f"declared inputs. Declared inputs are {inputs!r}; read-only "
                f"binds are {read_only!r}.")
    _argv, command_error = _isolation_command_argv(u.get("command"))
    if command_error:
        return (f"unit {uid!r} isolation cannot preserve {command_error}. "
                f"Refusing the profile avoids relying on /bin/sh or another "
                f"undeclared interpreter inside the image.")
    return None

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


def _seed_carry_forward(seed, repo, remote):
    """Render recorded provenance as instructions, never as closure evidence."""
    if seed is None:
        return ""
    # Exactly-once routing, as in _git_push_destination: origin may fetch
    # from somewhere other than its push destination. Quote every shell arg.
    alias = "hanig-swarm-seed-" + hashlib.sha256(remote.encode()).hexdigest() + ":"
    route = shlex.quote(f"url.{remote}.insteadOf={alias}")
    fetch = (f"git -c {route} fetch --no-tags --recurse-submodules=no "
             f"{shlex.quote(alias)} {shlex.quote(seed['ref'])}")
    evidence = ""
    if "evidence" in seed:
        evidence = (f"\nRead the previous evidence file {seed['evidence']!r} before "
                    f"editing; relative paths are relative to the source repository "
                    f"{repo!r}, not the new worktree. Treat it as prior "
                    "context, not a review pass or completion evidence.")
    return f"""\n\nCARRY FORWARD (recorded seed; provenance only)
Stay on the new attempt branch and its recorded launch base. Fetch the seed ref {seed['ref']!r} from the recorded origin push destination:
```sh
{fetch} &&
swarm_seed_commits=$(git rev-list --max-count=1 {seed['base']}..{seed['head']}) &&
if test -n "$swarm_seed_commits"; then
    git cherry-pick -x {seed['base']}..{seed['head']}
fi
```
An empty range carries no commits and is a successful no-op. Skip empty commits: when cherry-pick stops because a commit is empty or already applied, confirm that it is empty and run `git cherry-pick --skip`, repeating as needed. Resolve real conflicts explicitly; never skip a non-empty conflicting change just to finish. Never port by whole-file checkout.
If fetching or replay cannot be completed, STOP AND REPORT; do not substitute another ref or range.{evidence}
The coordinator checked reachability at launch, not whether replay will be conflict-free. It does not cherry-pick or alter the worktree for you. Seed history does not change judging, scope-check, review, or merged-PR closure; the produced head is judged against this fresh attempt's recorded base."""


def _code_completion_protocol(intent):
    """Instructions that make a code attempt capable of closing its unit.

    C11 creates the branch and worktree. The agent's job is to leave durable
    evidence on that branch and open the pull request that the unit's closing
    predicate requires; asking it to choose either launch fact would put
    authority back in prompt prose. The worker stops after opening the pull
    request; merge decisions belong to the orchestrator.

    Precedence is explicit rather than inferred from task text. Contradictions
    in arbitrary prose are not statically recognizable: matching phrases such
    as "do not commit" would also reject legitimate tasks that merely discuss
    them. A visible stop-and-report rule is bounded; a prose detector is not.

    ARC-243, and why the stash rule is here rather than in the skill's prose.
    Three agents in three worktrees of one repository each ran `git stash -u`
    inside the same window. The stash stack is a SINGLE ref in the shared
    common Git directory, so it is not per-worktree: every pop took somebody
    else's entry. The work was recovered from dangling commits and it cost
    real time. All three were doing the same reasonable thing -- checking
    whether a red test pre-existed their change -- which is what makes it a
    defect in the protocol rather than three mistakes, and which is why the
    prohibition names its SUBSTITUTES in the same breath. A prohibition with
    no alternative is a prohibition that gets worked around.
    """
    repo = str(intent["repo"])
    branch = str(intent["branch"])
    base = str(intent["base_commit"])
    target = str(intent["target_branch"])
    remote = intent.get("repository_remote")
    judgment_ref = intent.get("judgment_ref")
    # Use only coordinator-supplied routing facts. Older intents contain
    # neither field; the current default is not evidence of their author.
    provider_spec = intent.get("provider")
    provider, embedded_model = "", ""
    if isinstance(provider_spec, str):
        provider, _, embedded_model = provider_spec.partition("/")
    model = intent.get("model") or embedded_model
    if provider and isinstance(model, str) and model:
        author_instruction = (
            f"Pass --author {shlex.quote(provider + '/' + model)} on every "
            "review.py or committee.py run so the gate excludes this unit's own model.")
    else:
        author_instruction = (
            "The coordinator-recorded provider/model for this unit is "
            "UNKNOWN. Report the missing identity to the coordinator before "
            "review; do not invent an author or omit --author.")
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
    if judgment_ref:
        ref_kind = ("remote branch" if str(judgment_ref).startswith(
                    "refs/heads/") else "legacy remote-tracking")
        judgment_instruction = (
            f"The coordinator judges only the pre-anchored {ref_kind} "
            f"ref {judgment_ref!r}; a commit left only in the worktree, or "
            "pushed under another ref, cannot close the attempt.")
    else:
        judgment_instruction = (
            "This persisted legacy launch intent predates durable pushed-ref "
            "judgment. No pushed ref will be treated as though it had been "
            "anchored before the agent existed; its managed worktree must "
            "remain available for legacy transition judgment.")
    return f"""{CODE_COMPLETION_PROTOCOL_MARKER} (coordinator-required)
This protocol overrides any contrary instruction in the task text above it. If the task appears to forbid committing, pushing, or opening a pull request, STOP AND REPORT that conflict instead of choosing either instruction.
You are already in a dedicated worktree for repository {repo!r}, on branch {branch!r}, cut from recorded base commit {base}.
The required pull-request target is {target!r}.
Do not create or switch branches, and do not choose a different base.
Commit all intended work on {branch!r}. Uncommitted work is invisible to the transition predicate and will be judged as producing nothing.
{remote_instruction}
After opening the pull request, STOP. NEVER merge, approve, or enable auto-merge. Merge decisions belong to the orchestrator. The merged-PR closure criterion describes how the orchestrator's merge is judged, not an instruction to the worker.
{judgment_instruction}
Push after every meaningful edit. Commit and push the actual changed content to the anchored attempt ref for normal progress; use the recovery ref below when preserving a failed review. An empty or marker commit preserves no implementation; a tree identical to the recorded base is refused.
Before opening a pull request, run this repository's review gate. Use hanig-review-gate's review.py from the loaded skill directory with --kind implementation, the actual --round number, and --range {base}..HEAD to review the COMPLETE delta from the recorded base. Commit all intended edits first and leave the worktree clean so that range includes all intended work. Declare the change's claims and the standalone counter-claim "This change cannot make an honest run fail." {author_instruction} Only exit 0 (REVIEW_PASS) permits opening a pull request; unavailable, partial, incomplete, and failing reviews are not a pass. Reproduce each confirmed finding before acting on it. If a reproduced failure remains, do NOT open a pull request: follow the recovery procedure below without moving the anchored attempt ref, then STOP AND REPORT where it was preserved. REVIEW_PASS means the panel failed to refute the claims, not proof.
Recovery procedure: first inspect status as required below and stage only your own uncommitted work. Set and export SWARM_RECOVERY_MESSAGE to a message beginning RECOVERY, NOT READY: that says the work has not passed review, must not be merged, and lists every open reproduced finding. Run the following in the current attempt worktree. It commits UNCOMMITTED work if present; on an already clean tree it pushes existing content first, then adds an empty recovery label and pushes it. A label commit alone preserves nothing and must never be the only thing pushed. If any step fails, retain the worktree and report the failure; do not clean up or claim preservation succeeded.
```sh
(
set -eu
: "${{SWARM_RECOVERY_MESSAGE:?set the recovery message with every open reproduced finding}}"
case "$SWARM_RECOVERY_MESSAGE" in
    'RECOVERY, NOT READY:'?*) ;;
    *) echo 'Recovery message must begin RECOVERY, NOT READY:' >&2; exit 1 ;;
esac
swarm_recovery_dirty=$(git status --porcelain --untracked-files=all)
if test -n "$swarm_recovery_dirty"; then
    git commit -m "$SWARM_RECOVERY_MESSAGE"
fi
test -z "$(git status --porcelain --untracked-files=all)" || exit 1
swarm_recovery_head=$(git rev-parse HEAD)
swarm_recovery_ref={shlex.quote('refs/heads/recovery/' + branch)}
swarm_recovery_diff=0
git diff --quiet {shlex.quote(base)} "$swarm_recovery_head" -- || swarm_recovery_diff=$?
if test "$swarm_recovery_diff" -ne 1; then
    echo 'Recovery requires real content different from the recorded base' >&2
    exit 1
fi
git push origin "$swarm_recovery_head:$swarm_recovery_ref"
if test -z "$swarm_recovery_dirty"; then
    git status --porcelain
    git commit --allow-empty -m "$SWARM_RECOVERY_MESSAGE"
    swarm_recovery_head=$(git rev-parse HEAD)
    git push origin "$swarm_recovery_head:$swarm_recovery_ref"
fi
)
```
NEVER run `git stash`, in any form. The stash stack is a SINGLE ref in the shared common Git directory, so every worktree of {repo!r} shares one stack and a pop takes whatever another agent parked. Do these instead: to read a file as it was at base, `git show {base}:<path>`; to set work aside, `git diff > /tmp/wip.patch` then `git checkout -- <path>`; and to answer "was this test already failing", add a separate worktree at {base} and run it there, rather than moving anything in this one. Note what such a comparison does and does not show: green at {base} and green here is a claim about your change alone, not about {target!r} after a merge.
Before every commit, run `git status --porcelain` and read it. Stage only paths you changed yourself; if it lists a path you did not touch, STOP AND REPORT instead of committing it. The observed failure is a commit that carried another agent's files.
If you cannot finish cleanly, STOP AND REPORT the problem instead of working around it.
Leave the worktree clean. Do not force-push or rewrite history. The final commit must descend from recorded base {base}; rewritten history makes honest work unjudgeable.{_seed_carry_forward(intent.get('seed'), repo, remote)}"""


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
    judgment_ref = intent.get("judgment_ref")
    required = {
        "protocol marker": CODE_COMPLETION_PROTOCOL_MARKER,
        "repository": repr(str(intent["repo"])),
        "attempt branch": repr(str(intent["branch"])),
        "pull-request target": repr(str(intent["target_branch"])),
        "recorded remote": repr(intent.get("repository_remote")),
        "recorded base": str(intent["base_commit"]),
        "judgment basis": (repr(judgment_ref) if judgment_ref else
                           "persisted legacy launch intent predates durable pushed-ref judgment"),
        "protocol precedence": "overrides any contrary instruction",
        "contradiction instruction": "STOP AND REPORT that conflict",
        "commit instruction": "Commit all intended work",
        "remote action": ("Open a pull request" if intent.get(
            "repository_remote") else "no merge-evidence repository was recorded"),
        "worker stop instruction": "After opening the pull request, STOP.",
        "worker merge prohibition": "NEVER merge, approve, or enable auto-merge.",
        "merge authority": "Merge decisions belong to the orchestrator.",
        "closure interpretation": (
            "The merged-PR closure criterion describes how the orchestrator's "
            "merge is judged, not an instruction to the worker."),
        "clean failure instruction": "STOP AND REPORT",
        "history instruction": "Do not force-push or rewrite history",
        "review gate": "Before opening a pull request, run this repository's review gate.",
        "push discipline": "Push after every meaningful edit.",
        "complete review range": f"--range {intent['base_commit']}..HEAD",
        "author exclusion": "--author",
        "finding reproduction": "Reproduce each confirmed finding before acting on it.",
        "failed review preservation": "RECOVERY, NOT READY:",
        "clean-tree recovery label": "git commit --allow-empty",
        "review interpretation": "REVIEW_PASS means the panel failed to refute the claims, not proof.",
        # ARC-243. The prohibition and each substitute are required
        # SEPARATELY and by exact text, so an edit cannot leave the ban
        # standing with nothing to do instead -- which is the state in which
        # it gets worked around.
        "stash prohibition": "NEVER run `git stash`",
        "read-at-base substitute": f"git show {str(intent['base_commit'])}:",
        "set-aside substitute": "git diff > /tmp/wip.patch",
        "base-comparison substitute": "add a separate worktree at",
        "foreign path check": "git status --porcelain",
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


def _units_with_canary(plan):
    """Effective dependencies, without editing the plan or launch intents.

    Use the ordinary needs readers for validation, dispatch and status. The
    raw plan is still the digest input; plans without canary keep their bytes.
    """
    units = plan.get("units") or []
    if "canary" not in plan:
        return units
    canary = plan["canary"]
    return [dict(u, needs=[*(u.get("needs") or []), canary])
            if isinstance(u, dict) and u.get("id") != canary
            and canary not in (u.get("needs") or []) else u for u in units]


def validate_plan(plan, survey=None):
    """Raise PlanError, or return a summary. Refuses BEFORE anything is
    dispatched: a plan that cannot be run should not half-run.

    `survey` is hanig-project's recorded observation of this cluster, or None
    when there is none to read. None is UNKNOWN, not "no restrictions": every
    check that consults it stays silent rather than guessing."""
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
        declared_seed(u)
        declared_scope(u)
        if "tracker" in u and not _nonblank_text(u["tracker"]):
            raise PlanError(
                f"unit {u.get('id', '?')!r} has tracker={u['tracker']!r}; "
                "tracker must be a non-empty issue string")
        deadline = u.get("deadline_s")
        try:
            finite_deadline = (float(deadline) if deadline is not None
                               and not isinstance(deadline, bool)
                               and isinstance(deadline, (int, float))
                               else None)
        except (OverflowError, ValueError):
            finite_deadline = None
        if (deadline is not None
                and (finite_deadline is None
                     or finite_deadline != finite_deadline
                     or finite_deadline in (float("inf"), float("-inf"))
                     or finite_deadline <= 0)):
            raise PlanError(
                f"unit {u.get('id','?')!r} has deadline_s={deadline!r}; "
                f"it must be a positive, finite number of seconds measured "
                f"from the coordinator-written allocated_at timestamp.")
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

    if "canary" in plan:
        canary = plan["canary"]
        if not isinstance(canary, str):
            raise PlanError("plan 'canary' must be a unit id string")
        probe = next((u for u in units if isinstance(u, dict)
                      and u.get("id") == canary), None)
        if probe is None:
            raise PlanError(f"plan 'canary' {canary!r} names no unit")
        if probe.get("needs"):
            raise PlanError(f"plan 'canary' {canary!r} must be a root "
                            "with no 'needs'")
        units = _units_with_canary(plan)

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

    # --- findings.json must be able to REACH its reader -------------------
    #
    # ARC-247 asked whether every plan's terminal unit should be forced to
    # declare `findings.json`. It should not, and the reason is not only that
    # a training run has no findings to write: a "terminal unit declares it"
    # check would not deliver the artifact either. `report.py` reads
    # findings.json from the PROJECT DIRECTORY, while declared outputs live
    # inside the attempt's exclusive write root, which nothing outside the
    # attempt reads. A plan could satisfy that rule in full and the report
    # would still render no findings section -- a passing check standing in
    # for a working system, which is the shape of failure this validator
    # exists to refuse.
    #
    # So the rule is narrowed to the part that is genuinely required and
    # currently enforced nowhere: a unit that declares findings.json must
    # also declare where it is published. Promotion is the only route out of
    # the write root, and it is an explicit, approved, recorded step. Without
    # one, the file is written, digested, and read by nobody.
    for u in units:
        if not isinstance(u, dict):
            continue
        for out in (u.get("outputs") or []):
            if os.path.basename(str(out).strip()) != FINDINGS_FILE:
                continue
            if u.get("promote_to"):
                break
            raise PlanError(
                f"unit {u.get('id','?')!r} declares the output {str(out)!r} "
                f"and no 'promote_to'. Declared outputs stay inside the "
                f"attempt's exclusive write root, and the only reader of "
                f"{FINDINGS_FILE} -- report.py's \"Findings reported by this "
                f"project\" section -- reads it from the PROJECT directory. "
                f"Written there and never promoted, it is an artifact nobody "
                f"sees, which is indistinguishable from not writing it. "
                f"Declare \"promote_to\": \"/abs/path/to/the/project\", or "
                f"drop the output if these findings are not being published.")

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

    # Optional OS-backed containment is a declaration, never an inference
    # from runtime.resolution, an image suffix, a partition, or whichever
    # executable happens to be first on PATH.  Refuse the whole plan before
    # dispatch when its writable host surface cannot be rendered exactly.
    for u in units:
        if not isinstance(u, dict):
            continue
        problem = isolation_problem(u)
        if problem:
            raise PlanError(problem)

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

    # --- the cluster facts the SURVEY already recorded --------------------
    #
    # Both of these were audited as "three parts of the system agree this
    # matters and the one component that could refuse never looks". They read
    # the survey and nothing else: no live sacctmgr call, because a validator
    # that queries the controller per unit is a validator that hangs on a
    # busy login node, and because the survey is the artifact the plan was
    # written against. No survey is `unknown`, and unknown refuses nothing.
    starved = memory_flag_problems(units, survey)
    if starved:
        raise PlanError(
            f"unit(s) {', '.join(sorted(starved))} are kind=slurm and request "
            f"no memory, but the survey of this cluster reports "
            f"mem_flag_required: DefMemPerNode is UNLIMITED, so there is no "
            f"default per-node memory to fall back on. A unit that says "
            f"nothing here is not accepting a sensible default -- there is "
            f"none -- and what it gets instead is a site-dependent fallback "
            f"nothing in the plan records. Add a memory request to the unit's "
            f"'sbatch' list: \"--mem=64G\". `--mem-per-cpu` or `--mem-per-gpu` "
            f"count too, since sbatch refuses `--mem` alongside either.")

    refused = account_problems(units, survey)
    if refused:
        listed = "; ".join(
            f"{uid} charges {acct!r} on partition {part!r}, but {why}"
            for uid, acct, part, why in refused)
        raise PlanError(
            f"this plan charges an account its partition will not accept: "
            f"{listed}.\n"
            f"    This is the failure the per-partition survey was added "
            f"for: hours went into QOSGrpCpuLimit while a 736-CPU partition "
            f"sat 202 CPUs idle, because nothing joined the account to the "
            f"partition. Move the unit to a partition this account is allowed "
            f"in, or charge an account that partition takes. {QOS_CAVEAT}")

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
        "judgment_ref": "refs/heads/swarm-protocol-validation-attempt",
        "base_commit": "0" * 40,
    }
    for u in units:
        if not isinstance(u, dict) or u.get("kind") != "code":
            continue
        unit_intent = dict(validation_intent)
        if "seed" in u:
            unit_intent["seed"] = u["seed"]
        assembled = _dispatch_prompt(u, unit_intent)
        problem = _code_protocol_problem(assembled, unit_intent)
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
    content = {"units": units,
               "budget": plan.get("budget"),
               "root": plan.get("root")}
    if "canary" in plan:
        content["canary"] = plan["canary"]
    payload = json.dumps(content, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


# Locks this process holds, KEYED BY PROJECT. A single global fd made
# acquire_lease return True for any state directory once one had been
# acquired, so a second project in the same process was never actually
# locked. Caught by the new tests, which is what they are for.
_LOCK_FDS = {}
# Pinned on actual acquisition, not refreshed from a later state read. Keep
# the pin after release so a stale process cannot save over a successor.
# Legacy unleased helpers pin on their first save instead (see save_state).
_LOCK_EPOCHS = {}
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
    it. The kernel does not tell us; the epoch fence can detect a successor's
    published acquisition on the next save, but is not an atomic compare-and-
    swap and cannot certify cross-node exclusion."""
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
    # Detection only: no heartbeat, TTL, expiry, mtime aging or timer. Nothing
    # reads this epoch to grant, steal or release custody; flock still decides
    # acquisition. The separate file keeps a no-op acquisition from rewriting
    # swarm-state.json. Concurrent reads/replaces and stale filesystem reads
    # remain outside this guarantee; this is not a distributed lock or a CAS.
    try:
        epoch, err = _read_state_epoch(state_dir)
        if err:
            sys.exit(f"HALTED: {err}")
        err = U.write_json(Path(state_dir) / STATE_EPOCH_FILE,
                           {"epoch": epoch + 1})
        if err:
            sys.exit(f"HALTED: cannot persist coordinator state epoch: {err}")
        _LOCK_EPOCHS[key] = (os.getpid(), epoch + 1)
    except BaseException:
        release_lease(state_dir)
        raise
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
class _StateSnapshot(dict):
    """Loaded state with an in-memory directory/process/epoch binding.

    Attributes are not JSON fields. Keep ordinary copies bound to the same
    load; neither a save nor a later acquisition may refresh this binding.
    This is a trusted-caller guard, not protection against deliberately
    rebuilding a snapshot as an untagged dict or editing private attributes.
    """

    def copy(self):
        snapshot = type(self)(self)
        snapshot._snapshot_epoch = self._snapshot_epoch
        return snapshot


def _read_state_epoch(state_dir):
    """Read only the fence file; absent legacy directories start at zero."""
    try:
        record = json.loads((Path(state_dir) / STATE_EPOCH_FILE).read_text())
    except FileNotFoundError:
        return 0, None
    except (OSError, ValueError) as exc:
        return None, f"unreadable coordinator state epoch: {exc}"
    if (not isinstance(record, dict) or type(record.get("epoch")) is not int
            or record["epoch"] < 0):
        return None, "invalid coordinator state epoch"
    return record["epoch"], None


def load_state(state_dir):
    """Load the durable side of the coordinator's lifecycle invariant.

    PERSIST BEFORE YOU ACT applies at both ends: record creation authority
    before starting an agent, and record conclusions plus cleanup charges
    before destroying their worktree. A crash may repeat observation, never
    creation or destruction whose governing state existed only in memory.

    Capture the binding BEFORE reading state. Unleased readers observe the
    fence without pinning the process or writing anything; an unreadable
    fence still allows status but cannot authorize a later save.
    """
    key = str(Path(state_dir).resolve())
    pin = _LOCK_EPOCHS.get(key)
    if pin is None:
        epoch, _error = _read_state_epoch(state_dir)
        pin = (os.getpid(), epoch)
    obj, err = U.read_json(Path(state_dir) / STATE_FILE)
    if err == "missing":
        obj = {"schema_version": 1, "units": {}, "halted": None}
    elif err:
        sys.exit(f"error: state at {Path(state_dir) / STATE_FILE} is unreadable "
                 f"({err}). Fix or remove it; removing it will re-dispatch "
                 f"units whose attempts are not recorded elsewhere.")
    if isinstance(obj, dict):
        obj = _StateSnapshot(obj)
        obj._snapshot_epoch = (key, *pin)
    return obj


def save_state(state_dir, state):
    key = str(Path(state_dir).resolve())
    epoch, err = _read_state_epoch(state_dir)
    # A lease holder always uses its process pin, even after reloading state.
    # Legacy direct helpers (including promotion) do not acquire a lease:
    # their first save pins the observed epoch without bumping it or granting
    # custody. Loaded snapshots also carry their own earlier observation.
    owner, expected = _LOCK_EPOCHS.get(key, (os.getpid(), epoch))
    if err or owner != os.getpid() or epoch != expected:
        # Do not persist the halt into the successor's state.
        reason = err or "state epoch changed or process pin was inherited"
        sys.exit("HALTED: another coordinator has written state under us "
                 f"({reason}); refusing to save")
    # Re-acquisition moves the process pin, never the snapshot's load epoch.
    # Check before changing the snapshot, pinning a helper or touching disk.
    # Plain, caller-built dictionaries retain the legacy direct-helper API;
    # every dictionary returned by load_state is a tagged snapshot.
    if (isinstance(state, _StateSnapshot)
            and getattr(state, "_snapshot_epoch", None)
            != (key, owner, expected)):
        sys.exit("HALTED: another coordinator has written state under us "
                 "(snapshot belongs to a different lease epoch or state "
                 "directory); reload state before saving")
    _LOCK_EPOCHS.setdefault(key, (owner, expected))
    # Retire the old inline field on a normal save, never on acquisition.
    state.pop("epoch", None)
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


def _set_unit_state(us, value, changed_at=None):
    """Set a unit state and durably date a real transition.

    The timestamp is coordinator state, not worker evidence. Historical state
    has no trustworthy transition time, so advance marks it from first
    observation rather than inventing an earlier event.
    """
    previous = us.get("state")
    if previous != value:
        us["state_changed_at"] = (time.time() if changed_at is None
                                  else float(changed_at))
        us.pop("state_changed_at_basis", None)
        if previous == "NEEDS_HUMAN" and value != "NEEDS_HUMAN":
            us.pop("reason", None)
            us.pop("deadline_previous_reason", None)
    us["state"] = value


def _normalise_state_clock(us, observed_at):
    """Give historical state an honest clock from its first observation."""
    if us.get("state") is not None and us.get("state_changed_at") is None:
        us["state_changed_at"] = float(observed_at)
        us["state_changed_at_basis"] = "first_observed"


def _deadline_host_evaluation(state, uid, us):
    """Whether this coordinator may evaluate the attempt's local clock."""
    anchor = trusted_launch_host_anchor(state, uid, us.get("attempt_dir"))
    belongs = W.attempt_belongs_to_host(anchor)
    if belongs is True:
        return True, None
    if belongs is False:
        return False, W.launch_host_problem(anchor)
    return None, (
        "UNJUDGEABLE HERE: coordinator state records no launch_host for this "
        "attempt, so its allocated_at deadline is not evaluable on this "
        "host. No deadline escalation is emitted")


def _deadline_exceeded(u, us, state, uid, observed_at=None):
    """Whether this host-owned attempt crossed its allocation deadline."""
    deadline = u.get("deadline_s")
    allocated = us.get("allocated_at")
    if deadline is None or allocated is None or not us.get("attempt_dir"):
        return False
    belongs, _problem = _deadline_host_evaluation(state, uid, us)
    if belongs is not True:
        return False
    terminal = {"DONE", "FAILED", "FAILED_EVIDENCE", "READY_FOR_PR",
                "PREFLIGHT_REFUSED", "HELD"}
    if us.get("state") in terminal:
        return False
    now = time.time() if observed_at is None else float(observed_at)
    return now - float(allocated) >= float(deadline)


def _mark_deadline_exceeded(u, us, observed_at=None):
    """Persist the conservative escalation; return True on first breach."""
    now = time.time() if observed_at is None else float(observed_at)
    active = (us.get("deadline_breach_active") is True
              or us.get("reason") == "deadline_exceeded")
    first_breach = not active
    sequence = us.get("deadline_breach_seq")
    if type(sequence) is not int or sequence < 1:
        sequence = 0
    if first_breach:
        sequence += 1
    elif sequence == 0:
        # State written by the first deadline implementation has no sequence.
        # Adopt its active breach as event one and retain that implementation's
        # key until the breach ends rather than emitting a duplicate.
        sequence = 1
        us["deadline_breach_legacy_key"] = True
    prior_reason = us.get("reason")
    if first_breach and prior_reason:
        us["deadline_previous_reason"] = prior_reason
    us["deadline_breach_active"] = True
    us["deadline_breach_seq"] = sequence
    us["reason"] = "deadline_exceeded"
    us["deadline_s"] = float(u["deadline_s"])
    us["deadline_at"] = float(us["allocated_at"]) + float(u["deadline_s"])
    # Marking the breach includes the operator-visible state transition. Every
    # caller, including the post-check path, persists NEEDS_HUMAN through here.
    _set_unit_state(us, "NEEDS_HUMAN", changed_at=now)
    return first_breach


def _clear_deadline_breach(us):
    """Clear one ended breach while retaining its monotonic event counter."""
    was_active = (us.pop("deadline_breach_active", None) is True
                  or us.get("reason") == "deadline_exceeded")
    if not was_active:
        return False
    if us.get("reason") == "deadline_exceeded":
        us.pop("reason", None)
        prior_reason = us.pop("deadline_previous_reason", None)
        if prior_reason:
            us["reason"] = prior_reason
    else:
        us.pop("deadline_previous_reason", None)
    us.pop("deadline_s", None)
    us.pop("deadline_at", None)
    us.pop("deadline_breach_legacy_key", None)
    return True


def _deadline_attempt_is_unresolved(u, us):
    """Whether a declared deadline still protects this attempt's resources."""
    terminal = {"DONE", "FAILED", "FAILED_EVIDENCE", "READY_FOR_PR",
                "PREFLIGHT_REFUSED", "HELD"}
    return (u.get("deadline_s") is not None
            and bool(us.get("attempt_dir"))
            and us.get("state") not in terminal)


def _occupies_live_resources(u, us):
    """Whether an attempt may still execute and must retain its claim/slot.

    Deadline protection depends on the declared attempt, never on whether a
    later phase has already rewritten the operator-facing state/reason.
    """
    return (us.get("state") in LIVE_STATES
            or _deadline_attempt_is_unresolved(u, us))


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


_CONTAINER_BIND_ENV = (
    "APPTAINER_BIND", "APPTAINER_BINDPATH",
    "APPTAINER_MOUNT",
    "SINGULARITY_BIND", "SINGULARITY_BINDPATH",
    "SINGULARITY_MOUNT",
)


def _isolation_submission(u, unit_dir, existing_facts=None):
    """Return (rendered command, trusted facts, error) for one profile.

    The runtime receives no implicit administrator/user bind paths, no host
    home, no hostfs mount, and no automatic cwd mount.  The attempt root is
    added once as rw and every declared input once as ro.  The root filesystem
    and container-private scratch may still be writable without creating a
    writable HOST bind; this contract is specifically the host write surface.

    Apptainer/Singularity still run as the invoking Unix user.  This confines
    the dispatched workload's host writes; it does not stop another process
    already running as that user from writing the attempt root, and it makes
    no PID or network isolation claim.
    """
    problem = isolation_problem(u)
    if problem:
        return None, None, problem
    profile = u.get("isolation")
    if profile is None:
        return u.get("command"), None, None

    root = str(Path(unit_dir).resolve())
    if any(ch in root for ch in "\n\r,:"):
        return None, None, (
            f"unit {u.get('id')!r}: attempt root {root!r} cannot be encoded "
            f"as one unambiguous container bind; refusing rather than "
            f"dropping or splitting the writable boundary")
    read_only = [os.path.normpath(str(p))
                 for p in (profile.get("read_only") or [])]
    for source in read_only:
        try:
            source_path = Path(source)
            root_path = Path(root)
            if (source_path == root_path
                    or _inside_dir(root_path, source_path)
                    or _inside_dir(source_path, root_path)):
                return None, None, (
                    f"unit {u.get('id')!r}: read-only input {source!r} "
                    f"overlaps attempt root {root!r}, so the rw and ro bind "
                    f"surfaces cannot both be enforced")
        except (OSError, ValueError):
            return None, None, (
                f"unit {u.get('id')!r}: cannot resolve isolation bind "
                f"relationship for {source!r}")

    canonical_profile = json.dumps(
        profile, sort_keys=True, separators=(",", ":"))
    marker = str(Path(root) / ISOLATION_MARKER_NAME)
    reusable = (existing_facts if isinstance(existing_facts, dict)
                and existing_facts.get("profile_sha256") == hashlib.sha256(
                    canonical_profile.encode()).hexdigest()
                and existing_facts.get("writable_host_binds") == [root]
                else None)
    marker_token = ((reusable or {}).get("application_token_sha256")
                    or os.urandom(32).hex())

    command_argv, command_error = _isolation_command_argv(u.get("command"))
    if command_error:
        return None, None, command_error
    argv = ["env"]
    for name in _CONTAINER_BIND_ENV:
        argv += ["-u", name]
    argv += [
        profile["backend"], "exec",
        "--contain", "--no-home", "--writable-tmpfs",
        "--no-mount", "bind-paths",
        "--no-mount", "hostfs",
        "--no-mount", "cwd",
        "--bind", f"{root}:{root}:rw",
    ]
    for source in read_only:
        argv += ["--bind", f"{source}:{source}:ro"]
    argv += ["--pwd", root, profile["image"]] + command_argv
    runtime = " ".join(shlex.quote(str(part)) for part in argv)
    # A zero exit from the directly executed workload proves the backend
    # entered the image and ran it under these flags.  Record application only
    # then.  A workload that failed after entry remains conservatively false:
    # backend errors and workload errors cannot be distinguished portably.
    # The marker is written by the existing host job shell after the runtime
    # returns, so no interpreter is assumed inside the image and no nested
    # shell can detach work from the pipeline wrapper's `wait`.
    applied = ("umask 077 && printf '%s\\n' "
               f"{shlex.quote(marker_token)} > {shlex.quote(marker)}")
    # Evidence failure must not turn an otherwise successful honest workload
    # into a failed unit.  Preserve the runtime status independently; a full
    # filesystem or unwritable marker merely leaves the receipt's isolation
    # bit false.  The final subshell restores the workload's exact status even
    # under the Slurm script's `set -e`.
    clear = f"rm -f -- {shlex.quote(marker)}"
    rendered = (f"isolation_rc=0; {clear} || isolation_rc=$?; "
                f"if [ \"$isolation_rc\" -eq 0 ]; then "
                f"{runtime} || isolation_rc=$?; fi; "
                f"if [ \"$isolation_rc\" -eq 0 ]; then {applied} || :; fi; "
                f"(exit \"$isolation_rc\")")
    facts = {
        "schema_version": 1,
        "unit_id": u.get("id"),
        "attempt_id": Path(root).name,
        "applied_to_submission": True,
        "mechanism": "container-host-bind-write-scope",
        "backend": profile["backend"],
        "image": profile["image"],
        "profile_sha256": hashlib.sha256(
            canonical_profile.encode()).hexdigest(),
        "writable_host_binds": [root],
        "read_only_host_binds": read_only,
        "application_marker": marker,
        "application_token_sha256": marker_token,
        "does_not_isolate": [
            "other processes running as the same Unix user",
            "networking",
        ],
    }
    return rendered, facts, None


def _record_isolation_facts(state, u, unit_dir, facts):
    """Pin the exact wrapper facts once per attempt in coordinator state."""
    us = state.setdefault("units", {}).setdefault(u["id"], {})
    attempt = Path(unit_dir).name
    stored = us.setdefault("attempt_isolation_facts", {})
    existing = stored.get(attempt)
    if existing is not None and existing != facts:
        return (f"unit {u['id']!r}: attempt {attempt!r} already has a "
                f"different isolation wrapper pinned in coordinator state; "
                f"refusing to replace the boundary after allocation")
    stored[attempt] = facts
    return None


def _clear_isolation_marker(facts):
    """Remove evidence from an earlier launch before submitting this one."""
    marker = Path(facts["application_marker"])
    try:
        marker.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return (f"cannot clear stale isolation application marker {marker}: "
                f"{exc}. Refusing to submit: an old marker must never attest "
                f"a new backend invocation")
    return None


def _submit(u, unit_dir, dry_run, state=None, state_dir=None,
            dispatch_source=None):
    """Submit, and return (job_id, error). Dispatch differs per kind; judging
    does not.

    --dry-run records what WOULD be submitted, so the DAG logic is testable
    without a scheduler. A coordinator that can only be tested on a live
    cluster does not get tested."""
    kind = u["kind"]
    anchored_base = None
    if kind == "code":
        # Capture only facts the coordinator can know before the agent exists.
        # The immutable base and generated path are durable before the
        # coordinator creates the worktree or asks Paseo to run in it.
        attempt = Path(unit_dir).name
        existing_intent = ((((state or {}).get("units") or {}).get(u["id"]) or {})
                           .get("attempt_launch_intents") or {}).get(attempt)
        if existing_intent:
            anchor_err = _code_launch_intent_problem(
                existing_intent, u, attempt)
            # Never manufacture provenance for a historical attempt from a
            # changed plan. A repair is a fresh attempt, including after a
            # crash before agent creation. This is admission only, not judging.
            if not anchor_err and (("seed" in u) != ("seed" in existing_intent)
                                   or u.get("seed") != existing_intent.get("seed")):
                anchor_err = (f"unit {u['id']!r}: seed differs from recorded "
                              "launch intent; allocate a fresh attempt")
            if not anchor_err:
                try:
                    seed = declared_seed(u)
                except PlanError as exc:
                    anchor_err = str(exc)
                else:
                    anchor_err = (_seed_reachability_problem(
                        seed, existing_intent["repo"],
                        existing_intent.get("repository_remote_raw"),
                        existing_intent.get("repository_remote")))
            # This path may create an agent after an interrupted submission.
            # Recheck admission, but never migrate an existing intent or ask
            # this question while judging an already-launched attempt.
            if not anchor_err and dispatch_source is None:
                target, anchor_err = _resolve_dispatch_target(u)
                if not anchor_err:
                    dispatch_source, anchor_err = _dispatch_source_identity(
                        u, target)
            if (not anchor_err and existing_intent["base_commit"] !=
                    dispatch_source["target_commit"]):
                anchor_err = _dispatch_base_refusal(
                    u.get("id"), dispatch_source["repo"],
                    f"recorded launch base {existing_intent['base_commit']} "
                    f"differs from resolved target "
                    f"{dispatch_source['target_commit']}; allocate a fresh "
                    f"attempt rather than replacing its launch authority")
            # Re-run the stash preflight on a re-dispatch of the SAME
            # attempt, for the reason `_write_launch_record` re-runs the
            # dirty predicate on its own already-anchored path: retry and
            # recovery must not be the way past a launch check. The anchored
            # base is never recaptured or re-trusted here, only the CURRENT
            # condition re-asked.
            if not anchor_err:
                anchor_err = _repeat_stash_preflight(u)
            anchored_base = (None if anchor_err else {
                "base": existing_intent["base_commit"],
                "intent": existing_intent,
            })
        else:
            anchor_err, anchored_base = _capture_code_launch(
                unit_dir, u, dispatch_source=dispatch_source)
        if anchor_err:
            return None, anchor_err
        # One warning point for cached/uncached sources and restored intents.
        # Keep it outside their branches so every code snapshot is reported.
        _warn_installed_skill_drift(
            anchored_base["intent"].get("installed_skills"),
            anchored_base["base"], u.get("id"))
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
        # before a worktree or agent can exist; crash recovery may identify an
        # existing checkout, but must never discover a replacement base from
        # a ref that could have moved in the meantime.
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
    submission_command = u.get("command")
    # The explicit guard is redundant with the return above by design: keep
    # the new side-effecting isolation path visibly unreachable to dry runs.
    if (not dry_run and kind in _NEEDS_RUNTIME
            and u.get("isolation") is not None):
        prior = (((state.get("units") or {}).get(u["id"]) or {})
                 .get("attempt_isolation_facts") or {}).get(
                     Path(unit_dir).name)
        submission_command, isolation_facts, isolation_error = (
            _isolation_submission(u, unit_dir, prior))
        if isolation_error:
            return None, isolation_error
        isolation_error = _clear_isolation_marker(isolation_facts)
        if isolation_error:
            return None, isolation_error
        isolation_error = _record_isolation_facts(
            state, u, unit_dir, isolation_facts)
        if isolation_error:
            return None, isolation_error
        # This is the fact the later receipt may report. Persist it before the
        # external launcher sees the wrapper; a crash may repeat submission
        # recovery, but it may never reconstruct or replace this boundary from
        # the agent-writable job script or attempt spec.
        save_state(state_dir, state)
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
        body += [submission_command, ""]
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
            wrapped = (f'(\n{submission_command}\nrc=$?\nwait\nexit $rc\n)\n'
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
        # The coordinator owns the per-attempt worktree. Paseo receives it as
        # an ordinary cwd and therefore cannot delete it when an agent closes.
        # This is the lifecycle boundary that lets cleanup fail closed on a
        # restore-checked snapshot instead of racing Paseo's workspace GC.
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
                state, u, unit_dir, reuse_workspace,
                workspace_owner="coordinator")
            if reuse_error:
                return None, (
                    f"attempt {attempt!r} has an unowned worktree, but it "
                    f"does not match the trusted launch intent: {reuse_error}")
            # The exact-base worktree is now authenticated and durable. It is
            # safe to create the missing agent in it without recreating Git
            # resources or resolving a live ref.
            save_state(state_dir, state)
        else:
            reuse_workspace, git_pointer_sha256, create_error = (
                _create_code_worktree(state_dir, intent))
            if reuse_workspace:
                workspace_meta = _register_code_workspace(
                    state, u, unit_dir, reuse_workspace,
                    workspace_owner="coordinator")
                # Registration precedes verification so even a rejected
                # checkout remains visible to preservation and cleanup.
                save_state(state_dir, state)
            if create_error:
                return None, create_error
            complete_error = _complete_code_launch(
                state, u, unit_dir, reuse_workspace,
                workspace_owner="coordinator",
                git_pointer_sha256=git_pointer_sha256)
            if complete_error:
                workspace_meta["verification"] = "refused"
                workspace_meta["cleanup_pending"] = True
                workspace_meta["verification_problem"] = complete_error
                save_state(state_dir, state)
                return None, (f"coordinator-created worktree could not be "
                              f"verified: {complete_error}")
            save_state(state_dir, state)
        # U.run contains the short-lived Paseo client's environment. The
        # long-lived Paseo daemon is a separate process boundary: a live probe
        # showed its provider credentials can still reach an agent even when
        # absent from this client's environment. Paseo's --env cannot replace
        # those provider variables. Do not mistake this boundary for daemon
        # isolation; the daemon must itself be launched without ambient keys.
        argv = ["paseo", "run", "--background", "--json",
                "--cwd", str(reuse_workspace)]
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
        thinking = u.get("thinking", default_thinking_for(u))
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
            workspace_meta = _register_code_workspace(
                state, u, unit_dir, reuse_workspace,
                workspace_owner="coordinator")
            workspace_meta["verification"] = "refused"
            workspace_meta["cleanup_pending"] = True
            workspace_meta["verification_problem"] = "paseo run failed"
            save_state(state_dir, state)
            return None, f"paseo run failed: {_paseo_error(out, err)}"
        # Read the id from JSON. Scanning output tokens for "something long
        # with a dash in it" would happily return a branch name or a path.
        # paseo names this field `agentId`. Reading `id` found nothing and
        # left a live agent running, unbound and unjudgeable: the orphan class
        # the Slurm path has a reconcile net for.
        rec = _paseo_json(out) or {}
        agent = (rec.get("agentId") or rec.get("AgentId")
                 or rec.get("id") or rec.get("Id"))
        reported_workspace = rec.get("cwd") or rec.get("Cwd")
        workspace = reuse_workspace
        workspace_id = (_paseo_workspace_id(out)
                        or rec.get("workspaceId") or rec.get("WorkspaceId"))
        # Paseo has already acted. Persist ownership of the resource now,
        # before checks that may reject it; registration makes it cleanable
        # and does not endorse its contents. Cleanup remains bound to the
        # coordinator-created path even if Paseo omits or misreports cwd.
        workspace_meta = _register_code_workspace(
            state if state is not None else {}, u, unit_dir, workspace,
            workspace_id=workspace_id, workspace_owner="coordinator")
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
        if reuse_workspace:
            facts = trusted_launch_facts(state, u["id"], unit_dir)
            identity_error = W.workspace_identity_problem(U.run, facts)
            if (reported_workspace is not None
                    and str(Path(reported_workspace).resolve())
                    != reuse_workspace):
                identity_error = (f"Paseo attached the recovered agent to "
                                  f"{reported_workspace!r}, not authenticated worktree "
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
            workspace_id=workspace_id, recovery=True,
            workspace_owner="coordinator")
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
    # WHICH preflight refused. There are two of them now, and the counts
    # cannot tell them apart: a shared-stash-stack refusal has zero dirty
    # paths, and the run report rendered a zero count as "uncommitted
    # changes", which misstates why nothing ran.
    reason = "dirty-worktree"


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


# --- B6: one live claim per declared output destination -------------------
#
# NOT validate_plan's duplicate-id check, which can only see one plan. The
# skill encourages ad-hoc sub-plans; a reporter made three, and one unit was
# dispatched twice from two of them into identical output paths. Both
# coordinators were correct about everything they could see: `acquire_lease`
# excludes two CONTROLLERS over one state directory, and says nothing at all
# about two state directories over one output namespace.
#
# WHY A CLAIM DIRECTORY AND NOT AN flock. `acquire_lease`'s docstring is
# emphatic that an advisory lock beats every hand-rolled lease it replaced,
# because the kernel drops it when the holder dies and so there is no TTL to
# steal. That property is simply unavailable here: `advance` dispatches and
# EXITS, while the job it dispatched runs for hours or days. A lock held by
# the coordinator process would be released seconds after the thing it is
# meant to protect started, and holding it longer would mean not detaching,
# which is the design. So the claim is durable, and "is it stale" is answered
# by asking the party that actually knows -- the scheduler -- instead of by
# inventing the timeout `acquire_lease` was rewritten to delete.
#
# `mkdir(exist_ok=False)` IS the exclusivity, the same primitive and the same
# reasoning as unit.py's exclusive write root: two coordinators cannot both
# create one directory, and unlike O_EXCL on a plain file, directory creation
# is atomic on the network filesystems these paths usually sit on. Taking
# over a claim PROVEN dead is one `rename` of the whole directory, so exactly
# one racer wins it and the loser refuses.
#
# WHAT THIS DOES NOT GIVE YOU, stated in the spirit of ARC-248's note on the
# lease rather than left for someone to assume: the registry lives under the
# RUN ROOT, so it is shared by exactly those coordinators that share a run
# root. That is the reported case -- the default root is derived from the
# project checkout, so ad-hoc sub-plans of one project all land on it -- but
# two coordinators pointed at different roots do not see each other's claims,
# and no claim of any kind is taken by a dry run.
OUTPUT_CLAIMS_DIRNAME = ".output-claims"
CLAIM_FILE = "claim.json"

# survey.py's vocabulary, imported as a habit rather than as a word: "the
# scheduler does not list it" and "the scheduler could not be asked" are
# different facts, and written the same way the second reads as the first.
# Here that difference decides whether a second job is dispatched into a
# namespace a live one owns, so they are never collapsed.
CLAIM_LIVE, CLAIM_FREE, CLAIM_UNKNOWN = "live", "free", "unknown"


def _inside_dir(path, directory):
    try:
        Path(path).relative_to(Path(directory))
        return True
    except ValueError:
        return False


def _output_destinations(u, root):
    """Every place a unit's DECLARED OUTPUTS land, as (label, abs path).

    The unit's own namespace `<root>/<id>` comes first. Every attempt of that
    id lives under it, `SWARM_DEP_<ID>` points into it, and it is precisely
    what two plans naming one unit id share -- which is the reported failure.

    Then each declared output that ESCAPES that namespace, and each declared
    output under `promote_to`. Those are the keys that catch two DIFFERENT
    ids: a plain relative `metrics.jsonl` resolves inside its own namespace
    and can collide with nobody, so claiming it would be a false refusal
    against the commonest output name in the repo, while `../shared/model.pt`
    or an absolute path or a shared promotion tree resolve to the SAME string
    for both units. "The same outputs" only means anything as a destination.
    """
    uid = str(u.get("id") or "")
    base = Path(root).resolve()
    namespace = base / uid if uid else base
    out = [("output namespace", str(namespace))]
    seen = {str(namespace)}
    anchors = [namespace]
    promote = u.get("promote_to")
    if isinstance(promote, str) and promote.strip():
        # A malformed promote_to is `promote`'s refusal to make, with its own
        # message; claiming nothing for it here neither hides that nor
        # pretends to have checked a destination that does not resolve.
        dest, derr = resolve_promote_to(promote, root)
        if dest and not derr:
            anchors.append(Path(dest))
    for anchor in anchors:
        for o in (u.get("outputs") or []):
            resolved = os.path.normpath(os.path.join(str(anchor), str(o)))
            if anchor == namespace and _inside_dir(resolved, namespace):
                continue                  # covered by the namespace claim
            if resolved not in seen:
                seen.add(resolved)
                out.append(("declared output destination", resolved))
    return out


def _claim_dir(root, destination):
    """Where the claim on one destination lives. Content-addressed, because a
    destination is an arbitrary absolute path and cannot be a filename."""
    digest = hashlib.sha256(str(destination).encode()).hexdigest()[:24]
    return Path(root).resolve() / OUTPUT_CLAIMS_DIRNAME / digest


def _claim_liveness(claim):
    """Is the attempt a foreign claim records still live? live/free/unknown.

    FREE is a positive observation: a registry that answered and does not
    list the attempt. UNKNOWN is a registry that is absent or that failed,
    and it is NOT free. Silence is not evidence of absence, and reading it as
    absence here dispatches a second writer into a namespace a live one owns,
    which is the whole of B6.
    """
    attempt_id = str(claim.get("attempt") or "")
    kind = str(claim.get("kind") or "")
    job = str(claim.get("job") or "")
    if not attempt_id:
        return CLAIM_UNKNOWN, ("the claim names no attempt, so nothing can "
                               "be asked about it")
    if job.startswith("dry-") or job.startswith(DRY_PREFIX):
        return CLAIM_FREE, "the recorded attempt is a dry run and has no job"
    scheduler_job = f"swarm-{attempt_id}"
    if kind == "code":
        if not shutil.which("paseo"):
            return CLAIM_UNKNOWN, ("paseo is not on PATH, so the agent "
                                   "registry cannot be asked whether the "
                                   "agent for this attempt still exists")
        rc, out, _ = U.run(["paseo", "ls", "--json"], timeout=60)
        if rc != 0:
            return CLAIM_UNKNOWN, "`paseo ls --json` failed, which proves nothing"
        try:
            agents = json.loads(out or "[]")
        except (ValueError, TypeError):
            return CLAIM_UNKNOWN, "`paseo ls --json` did not return JSON"
        for a in (agents if isinstance(agents, list) else []):
            # EXACT trailing match, for reconcile_orphan's reason: `in`
            # also matched an attempt whose id is another's prefix.
            if str((a or {}).get("name") or "").split()[-1:] == [attempt_id]:
                return CLAIM_LIVE, (f"paseo still lists an agent for attempt "
                                    f"{attempt_id}")
        return CLAIM_FREE, f"paseo answered and lists no agent for {attempt_id}"
    if kind != "slurm":
        # A detached pipeline is a pid on the host that launched it. Asking
        # about a pid from another host is unanswerable, and asking about one
        # on THIS host would still confuse pid reuse with liveness, so this
        # says unknown rather than guessing in either direction.
        return CLAIM_UNKNOWN, (f"a {kind or 'kind-less'} attempt records no "
                               f"registry this host can ask about liveness")
    if not shutil.which("squeue"):
        return CLAIM_UNKNOWN, ("squeue is not on PATH, so the scheduler "
                               "cannot be asked whether a job is still "
                               "queued or running for this attempt")
    # squeue ONLY, deliberately. `sacct` keeps a row long after a job ends,
    # so an sacct row answers "did it ever reach the scheduler" -- which is
    # reconcile_orphan's question -- and not "is it live", which is this one.
    rc, out, _ = U.run(["squeue", "-h", "-n", scheduler_job, "-o", "%i"],
                       timeout=60)
    if rc != 0:
        return CLAIM_UNKNOWN, (f"`squeue -n {scheduler_job}` failed, so "
                               f"the scheduler said nothing about this "
                               f"attempt")
    listed = (out or "").strip()
    if listed:
        return CLAIM_LIVE, (f"squeue lists job "
                            f"{listed.splitlines()[0].strip()} under job name "
                            f"{scheduler_job}")
    return CLAIM_FREE, (f"squeue answered and lists no job named "
                        f"{scheduler_job}, so no queued or running job "
                        f"holds it")


def _claim_refusal(uid, label, destination, claim, verdict, why, claim_dir):
    """The refusal, which is where the `squeue` half of B6 earns its keep: the
    lock decides, and the scheduler supplies the sentence a human can act on."""
    owner = str(claim.get("state_dir") or "an unrecorded state directory")
    other = str(claim.get("unit") or "?")
    head = ("is still live" if verdict == CLAIM_LIVE
            else "cannot be shown to be finished")
    lines = [
        f"unit {uid!r}: launch preflight refused the {label} "
        f"{str(destination)!r}; unit {other!r}, dispatched from state "
        f"directory {owner!r} as attempt "
        f"{str(claim.get('attempt') or '?')!r}, holds a claim on it and "
        f"{head}.",
        f"  scheduler: {why}",
    ]
    if verdict == CLAIM_UNKNOWN:
        lines.append(
            "  UNKNOWN IS NOT FREE. Nothing here shows the other attempt "
            "finished, and dispatching on that silence is how one unit "
            "becomes two writers of one output path.")
    lines.append(
        f"  If that attempt is genuinely gone, remove the claim and re-run: "
        f"rm -r {claim_dir}")
    refusal = PreflightRefusal("\n".join(lines))
    refusal.workspace = str(destination)
    refusal.dirty_count = 0
    refusal.reason = "output-claim-held"
    return refusal


def _claim_record(u, state_dir, attempt, label, destination):
    return {"schema_version": 1,
            "unit": u.get("id"),
            "kind": u.get("kind"),
            "state_dir": str(Path(state_dir).resolve()),
            "attempt": attempt,
            "job": None,
            "label": label,
            "destination": str(destination),
            "host": os.uname().nodename,
            "pid": os.getpid(),
            "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def _own_claim(claim, u, state_dir):
    """Is an existing claim this state directory's own claim for this unit?

    Adjudicated from OUR state file, which is authority for us and which the
    lease already serializes. This is what stops a crashed coordinator
    wedging its own project: it restarts, its state says the unit is not
    live, and it retakes a claim it never released -- with no timeout, and
    without any claim of knowing anything about a FOREIGN coordinator.
    """
    return (str(claim.get("state_dir") or "")
            == str(Path(state_dir).resolve())
            and claim.get("unit") == u.get("id"))


def _take_output_claims(u, root, state_dir, attempt):
    """Claim every destination this unit's declared outputs occupy.

    Returns (refusal, held). Nothing is left held on refusal: a partial claim
    would block the very unit that was not allowed to start.
    """
    held = []
    for label, destination in _output_destinations(u, root):
        directory = _claim_dir(root, destination)
        record = _claim_record(u, state_dir, attempt, label, destination)
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            existing, read_err = U.read_json(directory / CLAIM_FILE)
            existing = existing if isinstance(existing, dict) else {}
            if read_err or not existing:
                # A claim directory with no readable claim is a coordinator
                # that died between the mkdir and the write. It names no
                # owner, so it cannot be adjudicated and must not be assumed
                # empty; the refusal says how to clear it.
                refusal = _claim_refusal(
                    u.get("id"), label, destination, existing, CLAIM_UNKNOWN,
                    f"the claim itself is unreadable ({read_err or 'empty'})",
                    directory)
                _drop_claims(held)
                return refusal, []
            if not _own_claim(existing, u, state_dir):
                verdict, why = _claim_liveness(existing)
                if verdict != CLAIM_FREE:
                    refusal = _claim_refusal(u.get("id"), label, destination,
                                             existing, verdict, why, directory)
                    _drop_claims(held)
                    return refusal, []
                # PROVEN dead. One rename decides the takeover, so two
                # coordinators cannot both conclude they won it.
                aside = directory.with_name(
                    f"{directory.name}.superseded-{os.getpid()}-"
                    f"{int(time.time())}")
                try:
                    os.rename(str(directory), str(aside))
                    directory.mkdir(parents=True, exist_ok=False)
                except OSError as exc:
                    refusal = _claim_refusal(
                        u.get("id"), label, destination, existing,
                        CLAIM_UNKNOWN,
                        f"{why}, but the dead claim could not be taken over "
                        f"({exc}); another coordinator may have taken it first",
                        directory)
                    _drop_claims(held)
                    return refusal, []
        except OSError as exc:
            _drop_claims(held)
            return (f"unit {u.get('id')!r}: cannot claim the {label} "
                    f"{str(destination)!r}: {exc}"), []
        write_err = U.write_json(directory / CLAIM_FILE, record)
        if write_err:
            _drop_claims(held + [directory])
            return (f"unit {u.get('id')!r}: cannot record the claim on "
                    f"{str(destination)!r}: {write_err}"), []
        held.append(directory)
    return None, held


def _drop_claims(directories):
    for directory in directories:
        shutil.rmtree(str(directory), ignore_errors=True)


def _release_output_claims(u, root, state_dir):
    """Drop the claims THIS state directory holds for this unit.

    Recomputed from the plan rather than by scanning the registry, so a
    coordinator can only ever release its own, and only for units it owns.
    """
    released = []
    for _label, destination in _output_destinations(u, root):
        directory = _claim_dir(root, destination)
        claim, err = U.read_json(directory / CLAIM_FILE)
        if err or not isinstance(claim, dict):
            continue
        if _own_claim(claim, u, state_dir):
            shutil.rmtree(str(directory), ignore_errors=True)
            released.append(str(destination))
    return released


def _note_claimed_attempt(u, root, state_dir, attempt, job):
    """Record the job id on claims already held, so a later coordinator can
    ask the scheduler about it rather than only about the attempt name."""
    for _label, destination in _output_destinations(u, root):
        directory = _claim_dir(root, destination)
        claim, err = U.read_json(directory / CLAIM_FILE)
        if err or not isinstance(claim, dict):
            continue
        if _own_claim(claim, u, state_dir) and claim.get("attempt") == attempt:
            claim["job"] = str(job) if job is not None else None
            U.write_json(directory / CLAIM_FILE, claim)


def _code_worktree_names(unit_dir):
    """Name coordinator/Git resources from the random attempt id."""
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


def _stash_refusal(uid, repo, entries):
    """ARC-243, at second zero, in the same shape as C10's dirty refusal.

    The dirty index of the source checkout is deliberately NOT checked here,
    for the reason the docstring below gives: it cannot reach a worktree made
    from an object id. THE STASH STACK IS DIFFERENT, and that is the whole
    reason this refusal exists. It is one ref in the shared common Git
    directory, so it is visible and mutable from the fresh attempt worktree
    too -- it crosses the isolation boundary every other part of the
    code-unit design rests on. If the stack is non-empty at second zero, the
    agent's worktree is not isolated with respect to it before the agent has
    run a single command.

    This does NOT stop an agent creating a stash mid-run; nothing at dispatch
    can. What it stops is a run STARTING on top of somebody else's parked
    work, which is the case where recovery is hardest because nobody knows
    the entry is there to look for.
    """
    lines = [
        f"unit {uid!r}: launch preflight refused source checkout "
        f"{str(repo)!r}; its `git stash` stack holds {len(entries)} "
        f"entry/entries:"]
    lines += [f"  {line}" for line in entries[:10]]
    if len(entries) > 10:
        lines.append(f"  ... and {len(entries) - 10} more")
    lines.append(
        "The stack is a SINGLE ref in the shared common Git directory, so it "
        "is shared with every worktree of this repository, including the one "
        "this attempt would be given. Three agents lost work to that in one "
        "window: each popped an entry another had pushed.")
    lines.append(
        "Deal with the parked work first -- `git stash list` to see it, then "
        "apply or drop each entry -- and the next advance dispatches this "
        "unit. No retry was charged.")
    refusal = PreflightRefusal("\n".join(lines))
    refusal.workspace = str(repo)
    refusal.dirty_count = 0
    refusal.reason = "shared-stash-stack"
    return refusal


def _stash_preflight(uid, repo):
    """The refusal for a non-empty shared stash stack, or None."""
    rc, stashes, _ = _git(repo, "stash", "list")
    if rc != 0:
        # Not being able to ask is not an answer, the same rule the output
        # claims are built on. A checkout whose HEAD reads fine but whose
        # stash list errors is not a checkout we can call free of parked work.
        return (f"unit {uid!r}: cannot read the `git stash` stack in "
                f"{str(repo)!r}, so it cannot be shown to be empty. The stack "
                f"is shared with every worktree of this repository; refusing "
                f"rather than assuming it is empty.")
    entries = [line for line in (stashes or "").splitlines() if line.strip()]
    return _stash_refusal(uid, repo, entries) if entries else None


def _repeat_stash_preflight(u):
    """Re-ask the stash question without recapturing or trusting an anchor."""
    repo, err = _plan_workspace(u)
    if err:
        return err
    return _stash_preflight(u.get("id"), repo)


def _git_push_destination(repo, raw, resolved, remote_index, *args,
                          timeout=60):
    """Run a remote command against origin's once-expanded push route."""
    routed_args = list(args)
    if (remote_index < 0 or remote_index >= len(routed_args)
            or routed_args[remote_index] != raw):
        return 2, "", "internal error: push-route argument is not anchored"
    alias = "hanig-swarm-route:" + os.urandom(16).hex()
    routed_args[remote_index] = alias
    handle = tempfile.NamedTemporaryFile(
        prefix="hanig-swarm-push-route-", delete=False)
    config_path = handle.name
    handle.close()
    try:
        rc, out, err = U.run(
            ["git", "config", "--file", config_path, "--add",
             f"url.{resolved}.insteadOf", alias], timeout=timeout)
        if rc != 0:
            return rc, (out or "").strip(), (err or "").strip()
        return _git(repo, "-c", f"include.path={config_path}", *routed_args,
                    timeout=timeout)
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass


def _exact_remote_ref_head(listing, ref):
    """Return the sole valid object id reported for exactly ``ref``."""
    matches = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref:
            matches.append(fields[0])
    if len(matches) != 1:
        return None
    head = matches[0]
    if (len(head) not in (40, 64)
            or any(c not in "0123456789abcdef" for c in head.lower())):
        return None
    return head


def _dispatch_base_refusal(uid, repo, message):
    """Carry a source-identity refusal to the command exit contract."""
    refusal = PreflightRefusal(f"unit {uid!r}: {message}")
    refusal.workspace = str(repo)
    refusal.dirty_count = 0
    refusal.reason = "dispatch-base"
    return refusal


def _resolve_dispatch_target(u):
    """Resolve one code unit's target commit without deciding from a branch.

    The caller caches this value for an entire advance. Every launch uses
    the same immutable commit, even if the remote target moves afterwards.
    """
    repo, err = _plan_workspace(u)
    if err:
        return None, _dispatch_base_refusal(u.get("id"), u.get("repo"), err)
    repo = str(Path(repo).resolve())
    target = str(u.get("target_branch") or "").strip()
    if not target:
        return None, _dispatch_base_refusal(
            u.get("id"), repo, "no target_branch was declared")
    remote_raw, remote, problem = W.remote_push_transport(U.run, repo)
    if problem:
        return None, _dispatch_base_refusal(
            u.get("id"), repo,
            f"repository has no readable single origin push destination "
            f"({problem}). Code attempts must push their generated branch "
            f"to origin")
    target_ref = f"refs/heads/{target}"
    rc, listing, remote_err = _git_push_destination(
        repo, remote_raw, remote, 2, "ls-remote", "--exit-code", remote_raw,
        target_ref)
    target_commit = _exact_remote_ref_head(listing, target_ref)
    if rc != 0 or target_commit is None:
        detail = remote_err or "no single valid exact-ref answer"
        return None, _dispatch_base_refusal(
            u.get("id"), repo,
            f"cannot resolve origin/{target} before dispatch: {detail[:200]}")
    return {
        "repo": repo,
        "repository_remote": remote,
        "repository_remote_raw": remote_raw,
        "target_branch": target,
        "target_ref": target_ref,
        "target_commit": target_commit,
    }, None


def _dispatch_source_identity(u, target):
    """Bind actual launch source to the target commit, irrespective of ref."""
    repo = target["repo"]
    rc, head, head_err = _git(repo, "rev-parse", "HEAD")
    if rc != 0:
        return None, _dispatch_base_refusal(
            u.get("id"), repo,
            f"checkout has no readable HEAD: {head_err[:200]}")
    rc, base_ref, _ = _git(repo, "symbolic-ref", "--quiet", "HEAD")
    if rc != 0:
        base_ref = None
        base_branch = "(detached HEAD)"
    elif base_ref.startswith("refs/heads/"):
        base_branch = base_ref[len("refs/heads/"):]
    else:
        base_branch = base_ref
    if head != target["target_commit"]:
        return None, _dispatch_base_refusal(
            u.get("id"), repo,
            f"checkout {base_branch!r} at {head} differs from "
            f"origin/{target['target_branch']} at "
            f"{target['target_commit']}; launch source must equal the "
            f"pinned target commit")
    rc, tree, tree_err = _git(repo, "rev-parse", head + "^{tree}")
    if rc != 0:
        return None, _dispatch_base_refusal(
            u.get("id"), repo,
            f"cannot read the tree of target {head}: {tree_err[:200]}")
    source = dict(target)
    source.update({"base_commit": target["target_commit"],
                   "base_tree": tree})
    return source, None


def _dispatch_target_for_advance(u, cache):
    """Resolve a target once per advance and return the cached observation."""
    # A plan may spell its workspace as a subdirectory of the repository.
    # Key by the same resolved Git root that target resolution and launch use.
    repo, problem = _plan_workspace(u)
    if problem:
        return None, _dispatch_base_refusal(
            u.get("id"), _execution_workspace(u), problem)
    key = (str(Path(repo).resolve()),
           str(u.get("target_branch") or "").strip())
    if key not in cache:
        cache[key] = _resolve_dispatch_target(u)
    return cache[key]


def _installed_skill_source_version(marker):
    """Read a marker's claim, never installation or execution authority."""
    fd = None
    try:
        fd = os.open(str(marker), os.O_RDONLY | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "marker is not a regular file"
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(65537)
        if len(raw) > 65536:
            return None, "marker exceeds 65536 bytes"
        versions = [line.partition("=")[2]
                    for line in raw.decode("utf-8").splitlines()
                    if line.startswith("source_version=")]
        if len(versions) != 1 or not versions[0]:
            return None, "marker needs one non-empty source_version"
        return versions[0], None
    except (OSError, UnicodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if fd is not None:
            os.close(fd)


def _installed_skill_snapshot(repo, base):
    """Best-effort audit of ~/.agents/skills and ~/.claude/skills only.

    These are available candidates, not evidence of which skill a worker
    loads. Loader precedence, project/custom stores and later edits are not
    observed. Keep each path and raw marker claim, including duplicate names.
    A symlinked skill store holding a link install can yield the wrong sidecar
    path (for example after relocation); record an advisory error instead of
    a version and report it at dispatch. This does not certify installed bytes.
    """
    snapshot = {"skills": [], "errors": []}
    resolved_versions = {}
    try:
        home = Path.home()
    except (OSError, RuntimeError) as exc:
        snapshot["errors"].append({"path": "~", "error": str(exc)})
        return snapshot
    for root in (home / ".agents" / "skills", home / ".claude" / "skills"):
        try:
            candidates = sorted(root.iterdir())
        except FileNotFoundError as exc:
            # An absent optional store is quiet. A dangling store symlink
            # still exists as an entry and needs a visible audit error.
            error = exc
            try:
                root.lstat()
            except FileNotFoundError:
                continue
            except OSError as probe_error:
                error = probe_error
            snapshot["errors"].append({"path": str(root), "error": str(error)})
            continue
        except OSError as exc:
            snapshot["errors"].append({"path": str(root), "error": str(exc)})
            continue
        for skill in candidates:
            if not skill.name.startswith("hanig-"):
                continue
            try:
                if not (skill / "SKILL.md").is_file():
                    snapshot["errors"].append({
                        "path": str(skill), "error": "no loadable SKILL.md"})
                    continue
                marker = skill / ".installed-by-multi-agent-skills"
                # Link installs keep provenance beside the destination. This
                # mirrors the installer's default sidecar naming only; the
                # recorded claim does not certify ownership or current bytes.
                # A marker in the linked source describes that source copy,
                # not this link installation; the sidecar takes precedence.
                if skill.is_symlink():
                    destination = skill.parent.resolve() / skill.name
                    digest = hashlib.sha256(os.fsencode(str(destination)))
                    marker = (destination.parent / ".multi-agent-skills-provenance"
                              / f"{skill.name}-{digest.hexdigest()[:24]}.provenance")
                version, error = _installed_skill_source_version(marker)
            except (OSError, RuntimeError) as exc:
                snapshot["errors"].append({"path": str(skill), "error": str(exc)})
                continue
            entry = {"skill": skill.name, "path": str(skill),
                     "marker": str(marker), "source_version": version}
            if error:
                entry["error"] = error
            # Keep the marker's spelling. Search the object namespace only:
            # revision expressions can instead resolve a hash-named ref.
            # Equality needs exactly one object, the full pinned base; an
            # ambiguous or unavailable claim remains advisory uncertainty.
            # Several skills commonly share one installed revision, so only
            # resolve each distinct claim once in this dispatch snapshot.
            if (version and version != base
                    and re.fullmatch(r"[0-9a-fA-F]{4,64}", version)):
                if version not in resolved_versions:
                    rc, resolved, _ = _git(repo, "rev-parse",
                                           "--disambiguate=" + version, timeout=5)
                    resolved_versions[version] = (
                        base if rc == 0 and resolved == base else None)
                if resolved_versions[version] is not None:
                    entry["resolved_commit"] = resolved_versions[version]
            snapshot["skills"].append(entry)
    return snapshot


def _warn_installed_skill_drift(snapshot, base, uid):
    """Report audit observations; never return an admission decision."""
    observations = []
    for entry in (snapshot or {}).get("skills", []):
        version = entry["source_version"]
        if entry.get("error"):
            observations.append(
                f"installed skill audit incomplete at {entry['path']!r}: "
                f"marker={entry['marker']!r}; {entry['error']!r}.")
        elif (version is not None and version != base
                and entry.get("resolved_commit") != base):
            observations.append(
                f"installed skill drift check at {entry['path']!r}: "
                f"source_version={version!r}; attempt base={base!r}. "
                "Equality was not established.")
    for error in (snapshot or {}).get("errors", []):
        observations.append(
            f"installed skill audit incomplete at {error['path']!r}: "
            f"{error['error']!r}.")
    for observation in observations:
        try:
            print(f"WARNING: unit {uid!r}: {observation} Only ~/.agents/skills "
                  "and ~/.claude/skills are scanned; project/custom stores "
                  "are not scanned; symlinked skill stores may hide "
                  "link-install sidecars. Advisory only; dispatch continues.",
                  file=sys.stderr, flush=True)
        except (OSError, ValueError):
            # A closed/broken diagnostic stream must not turn this audit
            # warning into a new dispatch refusal. The snapshot persists.
            pass


def _seed_reachability_problem(seed, repo, raw, remote):
    """Fetch an exact seed ref and check base <= head <= fetched tip.

    The private temporary ref avoids stale tracking refs and shared FETCH_HEAD
    as a deciding value. Fetch writes Git metadata only; no index, worktree or
    attempt branch is changed. Reachability is a point-in-time admission fact,
    not proof that a worker replayed the seed or that replay has no conflicts.
    """
    if seed is None:
        return None
    cache_ref = "refs/hanig-swarm-seeds/" + os.urandom(16).hex()
    problem = None
    try:
        rc, _, err = _git_push_destination(
            repo, raw, remote, 4, "fetch", "--no-tags", "--force",
            "--recurse-submodules=no", raw, seed["ref"] + ":" + cache_ref,
            timeout=120)
        if rc != 0:
            problem = f"seed.ref {seed['ref']!r} cannot be fetched: {err[:200]}"
        else:
            for field in ("base", "head"):
                rc, kind, _ = _git(repo, "cat-file", "-t", seed[field])
                if rc != 0 or kind != "commit":
                    problem = f"seed.{field} {seed[field]!r} is not an available commit"
                    break
                # Git accepts a unique 40-hex abbreviation in a SHA-256
                # repository. Syntax alone therefore does not establish a
                # full object ID. Preserve the input spelling; compare widths.
                rc, full_id, _ = _git(repo, "rev-parse", "--verify", seed[field])
                if rc != 0 or len(full_id) != len(seed[field]):
                    problem = f"seed.{field} {seed[field]!r} is not a full commit id in this repository"
                    break
            if not problem:
                for ancestor, descendant in ((seed["base"], seed["head"]),
                                             (seed["head"], cache_ref)):
                    rc, _, _ = _git(repo, "merge-base", "--is-ancestor",
                                    ancestor, descendant)
                    if rc != 0:
                        problem = (f"seed range {seed['base']}..{seed['head']} "
                                   f"is not reachable in order from seed.ref {seed['ref']!r}")
                        break
    finally:
        cleanup_rc, _, _ = _git(repo, "update-ref", "-d", cache_ref)
    if cleanup_rc != 0:
        # The random cache ref is never reused. A leftover metadata ref is
        # housekeeping, not reachability evidence or a reason to reject work.
        print(f"WARNING: seed preflight could not remove temporary Git ref "
              f"{cache_ref!r}; remove it after the competing Git operation "
              "finishes. The reachability result is unchanged.", file=sys.stderr)
    return problem


def _capture_code_launch(unit_dir, u, dispatch_source=None):
    """Record immutable input to coordinator worktree creation.

    The shared checkout is a SOURCE, not the execution tree. Its dirty index
    and working files cannot enter a worktree made from an object id, so
    checking them would both block unrelated human work and prove nothing
    about the tree the agent receives. The clean-at-launch guarantee comes
    from constructing a new branch-off worktree from ``base_commit``.

    The stash stack is the exception, and `_stash_refusal` says why.
    """
    try:
        seed = declared_seed(u)
    except PlanError as exc:
        return str(exc), None
    repo, err = _plan_workspace(u)
    if err:
        return err, None
    stash_problem = _stash_preflight(u.get("id"), repo)
    if stash_problem:
        return stash_problem, None
    target = str(u.get("target_branch") or "").strip()
    if not target:
        return (f"unit {u.get('id')!r}: no target_branch was declared; "
                f"refusing to create a source branch for a pull request with "
                f"no named destination"), None
    slug, branch = _code_worktree_names(unit_dir)
    # The generated source name depends on the attempt id, so plan validation
    # cannot know this collision. Intent construction is the first point that
    # can, and it is still before the coordinator creates a worktree or agent.
    if target == branch:
        return (f"unit {u.get('id')!r}: target_branch {target!r} is the same "
                f"as generated attempt branch {branch!r}; a pull request "
                f"cannot merge a branch into itself"), None
    # Store the source identity in the same canonical form used for the
    # worktree cwd. This is an authority boundary, not a display path: a
    # relative spelling or symlink must not make the source checkout compare
    # unequal to itself later.
    repo = str(Path(repo).resolve())
    source = dispatch_source
    if source is None:
        resolved, problem = _resolve_dispatch_target(u)
        if problem:
            return problem, None
        source, problem = _dispatch_source_identity(u, resolved)
        if problem:
            return problem, None
    if source.get("repo") != repo or source.get("target_branch") != target:
        return _dispatch_base_refusal(
            u.get("id"), repo, "cached dispatch source names another target"), None
    seed_problem = _seed_reachability_problem(
        seed, repo, source["repository_remote_raw"], source["repository_remote"])
    if seed_problem:
        return seed_problem, None
    head, tree = source["base_commit"], source["base_tree"]
    remote_raw = source["repository_remote_raw"]
    remote = source["repository_remote"]
    judgment_ref = f"refs/heads/{branch}"
    # Query the PUSH destination, not the raw fetch spelling: with
    # `url.*.pushInsteadOf` configured they are different repositories, and
    # the attempt will push to the former. Checking the latter for collisions
    # asks the wrong repository and later judges the wrong one too.
    remote_rc, _remote_head, remote_err = _git_push_destination(
        repo, remote_raw, remote, 2, "ls-remote", "--exit-code", remote_raw,
        f"refs/heads/{branch}")
    if remote_rc == 0:
        return (f"unit {u.get('id')!r}: generated attempt branch {branch!r} "
                f"already exists on origin. Allocate a new attempt rather "
                f"than asking an agent to overwrite unrelated remote "
                f"history"), None
    if remote_rc != 2:
        detail = (remote_err or "git ls-remote returned %s" % remote_rc).strip()
        return (f"unit {u.get('id')!r}: cannot establish that generated "
                f"attempt branch {branch!r} is absent on origin: "
                f"{detail[:200]}. Refusing before agent creation"), None
    # The remote branch and the local branch are separate collision domains.
    # The coordinator creates the latter, while the former is the durable ref
    # the checker will query. A remote-tracking ref is deliberately irrelevant:
    # whether Git writes one after push is controlled by remote.origin.fetch.
    local_ref = f"refs/heads/{branch}"
    ref_rc, _out, _err = _git(
        repo, "show-ref", "--verify", "--quiet", local_ref)
    if ref_rc == 0:
        return (f"unit {u.get('id')!r}: generated local attempt branch "
                f"{branch!r} already exists. Allocate a new attempt rather "
                f"than asking Paseo to reuse its history"), None
    intent = {
        "schema_version": 5,
        "unit_id": u.get("id"),
        "attempt_id": Path(unit_dir).name,
        "launch_host": os.uname().nodename,
        "repo": repo,
        "repository_remote": remote,
        "repository_remote_raw": remote_raw,
        "base_commit": head,
        "target_commit": source["target_commit"],
        "base_tree": tree,
        "worktree_slug": slug,
        "branch": branch,
        # The exact durable observation the checker will make after the
        # worktree may already be gone. This is the remote ref NAME;
        # the agent controls its VALUE by pushing, and the checker derives and
        # validates that value rather than accepting an agent assertion.
        "judgment_ref": judgment_ref,
        "target_branch": target,
        # Makes the audit payload reproducible after a crash. Recovery can
        # compare exact expected bytes and restore the original seal without
        # trusting or laundering fields out of the file.
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        # Audit-only, captured before Paseo runs and persisted with the intent
        # so recovery retains this observation instead of sampling a new one.
        "installed_skills": _installed_skill_snapshot(repo, head),
        # Prompt metadata only, using the same defaults/overrides as _submit.
        # Do not copy into launch facts or use for admission/closure. Missing
        # fields in legacy intents remain unknown; never backfill them.
        "provider": u.get("provider") or DEFAULT_AGENT_PROVIDER,
        "model": u.get("model"),
    }
    if seed is not None:
        intent["seed"] = dict(seed)
    return None, {"base": head, "intent": intent}


def _code_launch_intent_problem(intent, u, attempt):
    if not isinstance(intent, dict):
        return (f"unit {u.get('id')!r}: coordinator state has no worktree "
                f"launch intent for attempt {attempt!r}")
    if intent.get("unit_id") != u.get("id") or intent.get("attempt_id") != attempt:
        return (f"unit {u.get('id')!r}: worktree launch intent belongs to "
                f"unit {intent.get('unit_id')!r}, attempt "
                f"{intent.get('attempt_id')!r}")
    schema = intent.get("schema_version", 1)
    if (not isinstance(schema, int) or isinstance(schema, bool)
            or schema < 1):
        return (f"unit {u.get('id')!r}: worktree launch intent has invalid "
                f"schema_version {schema!r}")
    for key in ("repo", "base_commit", "base_tree", "worktree_slug", "branch",
                "target_branch", "captured_at"):
        if not intent.get(key):
            return (f"unit {u.get('id')!r}: worktree launch intent is "
                    f"incomplete (missing {key})")
    if schema >= 2:
        for key in ("repository_remote", "judgment_ref"):
            if not intent.get(key):
                return (f"unit {u.get('id')!r}: worktree launch intent is "
                        f"incomplete (missing {key})")
    if schema >= 4 and not intent.get("repository_remote_raw"):
        return (f"unit {u.get('id')!r}: worktree launch intent is "
                "incomplete (missing repository_remote_raw)")
    if (schema >= 5
            and (not isinstance(intent.get("launch_host"), str)
                 or not intent["launch_host"])):
        return (f"unit {u.get('id')!r}: worktree launch intent is "
                "incomplete (missing launch_host)")
    for key in ("base_commit", "base_tree"):
        value = intent[key]
        if (not isinstance(value, str) or len(value) not in (40, 64)
                or any(c not in "0123456789abcdef" for c in value.lower())):
            return f"unit {u.get('id')!r}: invalid trusted {key}"
    if intent["target_branch"] == intent["branch"]:
        return (f"unit {u.get('id')!r}: trusted pull-request target equals "
                f"its generated attempt branch {intent['branch']!r}")
    expected_ref = (f"refs/heads/{intent['branch']}" if schema >= 3 else
                    f"refs/remotes/origin/{intent['branch']}")
    if (intent.get("judgment_ref") is not None
            and intent["judgment_ref"] != expected_ref):
        return (f"unit {u.get('id')!r}: trusted judgment ref "
                f"{intent['judgment_ref']!r} is not the schema-{schema} "
                "judgment ref "
                f"for attempt branch {intent['branch']!r}")
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


def _create_code_worktree(state_dir, intent):
    """Create the linked worktree the coordinator, not Paseo, will own."""
    source = Path(intent["repo"]).resolve()
    root = (Path(state_dir).resolve() / "code-worktrees")
    workspace = root / intent["worktree_slug"]
    try:
        if os.path.commonpath((str(source), str(root))) == str(source):
            return None, None, (
                f"refusing coordinator worktree root {root}: it is "
                f"inside operated repository {source}")
    except ValueError:
        pass
    if workspace.parent != root or workspace.exists() or workspace.is_symlink():
        return (None, None,
                f"coordinator worktree path {workspace} is not fresh")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (None, None,
                f"cannot create coordinator worktree root {root}: {exc}")
    rc, _out, err = _git(
        source, "worktree", "add", "-q", "-b", intent["branch"],
        str(workspace), intent["base_commit"], timeout=120)
    if rc != 0:
        return None, None, (f"cannot create coordinator-owned worktree "
                            f"{workspace}: {err[:200]}")
    resolved = str(workspace.resolve())
    pointer_digest, pointer_error = R.git_pointer_digest(
        Path(resolved) / ".git")
    if pointer_error:
        return resolved, None, pointer_error
    return resolved, pointer_digest, None


def _register_code_workspace(state, u, unit_dir, workspace, workspace_id=None,
                             workspace_owner=None):
    """Record a workspace before deciding whether to trust it.

    This is cleanup bookkeeping, not launch verification. In particular, a
    path that later fails branch, base, or Git-identity checks must remain
    visible and recoverable rather than becoming an unowned resource.
    """
    attempt = Path(unit_dir).name
    us = state.setdefault("units", {}).setdefault(u["id"], {})
    intent = (us.get("attempt_launch_intents") or {}).get(attempt) or {}
    path = str(Path(workspace).resolve()) if workspace else None
    prior = (us.get("attempt_workspaces") or {}).get(attempt) or {}
    meta = {
        "path": path,
        "branch": intent.get("branch"),
        "slug": intent.get("worktree_slug"),
        "archived": False,
        "verification": "pending",
    }
    owner = workspace_owner or prior.get("workspace_owner")
    if owner:
        meta["workspace_owner"] = owner
    if workspace_id:
        meta["workspace_id"] = workspace_id
    elif prior.get("workspace_id"):
        meta["workspace_id"] = prior["workspace_id"]
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
                          recovery=False, workspace_owner=None,
                          git_pointer_sha256=None):
    """Complete trusted launch facts from the verified worktree cwd.

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
    problem = W.launch_host_problem(intent)
    if problem:
        return problem
    prior_facts = (us.get("attempt_launch_facts") or {}).get(attempt) or {}
    prior_meta = (us.get("attempt_workspaces") or {}).get(attempt) or {}
    prior_identity = (prior_facts.get("workspace_identity")
                      or prior_meta.get("workspace_identity") or {})
    if git_pointer_sha256 is None:
        # Carry forward only a digest the coordinator already stored. Never
        # manufacture a launch baseline by observing the agent's live file.
        git_pointer_sha256 = prior_identity.get("git_pointer_sha256")
    workspace = str(Path(workspace).resolve())
    if workspace == intent["repo"]:
        return (f"the shared source checkout {workspace!r} was supplied "
                f"instead of a per-attempt worktree")
    if not os.path.isdir(workspace):
        return f"attempt worktree {workspace!r} is missing"
    rc, top, _ = _git(workspace, "rev-parse", "--show-toplevel")
    if rc != 0 or str(Path(top).resolve()) != workspace:
        return f"attempt cwd {workspace!r} is not a Git worktree root"
    rc, source_common, _ = _git(intent["repo"], "rev-parse", "--git-common-dir")
    rc2, worktree_common, _ = _git(workspace, "rev-parse", "--git-common-dir")
    if rc != 0 or rc2 != 0:
        return f"cannot identify Git common directory for {workspace!r}"
    source_common = str((Path(intent["repo"]) / source_common).resolve())
    worktree_common = str((Path(workspace) / worktree_common).resolve())
    if source_common != worktree_common:
        return (f"attempt cwd {workspace!r} is not a worktree of trusted source "
                f"repository {intent['repo']!r}")
    rc, worktree_git_dir, _ = _git(workspace, "rev-parse", "--git-dir")
    if rc != 0:
        return f"cannot identify Git metadata directory for {workspace!r}"
    worktree_git_dir = str((Path(workspace) / worktree_git_dir).resolve())
    if worktree_git_dir == worktree_common:
        return (f"attempt cwd {workspace!r} is the repository's main checkout, "
                f"not a linked per-attempt worktree")
    # Initial creation requires the exact base before Paseo starts. Recovery
    # may see honest commits made by the already-running agent and therefore
    # accepts only descendant history.
    rc, branch, _ = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or branch != intent["branch"]:
        return (f"attempt worktree {workspace!r} is on branch {branch!r}, not "
                f"trusted attempt branch {intent['branch']!r}")
    rc, head, _ = _git(workspace, "rev-parse", "HEAD")
    if rc != 0:
        return f"attempt worktree {workspace!r} has no readable HEAD"
    if recovery:
        rc, _, _ = _git(workspace, "merge-base", "--is-ancestor",
                         intent["base_commit"], head)
        if rc != 0:
            return (f"recovered attempt worktree {workspace!r} at {head} does "
                    f"not descend from trusted base {intent['base_commit']}; "
                    f"its history was replaced rather than extended")
    elif head != intent["base_commit"]:
        return (f"attempt worktree {workspace!r} is at {head}, not trusted "
                f"base {intent['base_commit']}")
    try:
        st = os.stat(workspace)
        common_st = os.stat(worktree_common)
        git_st = os.stat(worktree_git_dir)
    except OSError as exc:
        return f"cannot identify attempt worktree {workspace!r}: {exc}"
    identity = {"path": workspace, "realpath": workspace,
                "device": st.st_dev, "inode": st.st_ino,
                "git_common_dir": worktree_common,
                "git_common_device": common_st.st_dev,
                "git_common_inode": common_st.st_ino,
                "git_dir": worktree_git_dir,
                "git_dir_device": git_st.st_dev,
                "git_dir_inode": git_st.st_ino}
    if git_pointer_sha256 is not None:
        identity["git_pointer_sha256"] = git_pointer_sha256
    intent_schema = intent.get("schema_version", 1)
    judgment_ref = intent.get("judgment_ref")
    direct_remote_judgment = intent_schema >= 3
    facts = {
        # Schema 3 facts were emitted by the preserved first attempt and name
        # a local remote-tracking ref. Schema 4 facts are the first direct-ref
        # generation but predate the raw URL anchor. Keep both migrations
        # byte-compatible; schema 5 records the raw and once-expanded route,
        # and schema 6 binds the host-scoped identity to its launch host.
        "schema_version": (6 if intent_schema >= 5 else
                           5 if intent_schema >= 4 else
                           4 if direct_remote_judgment else
                           3 if judgment_ref else 2),
        "unit_id": u.get("id"),
        "attempt_id": attempt,
        "launch_host": intent.get("launch_host"),
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
    if intent.get("repository_remote_raw") is not None:
        facts["repository_remote_raw"] = intent["repository_remote_raw"]
    if judgment_ref:
        facts["judgment_ref"] = judgment_ref
    # Old intents have no observation to recover. Do not backfill them from
    # today's installs or change their deterministic audit bytes.
    if "installed_skills" in intent:
        facts["installed_skills"] = intent["installed_skills"]
    seal, error = _write_code_launch_record(unit_dir, facts)
    if error or not seal:
        return error or "worktree launch record has no recoverable seal"
    us.setdefault("attempt_launch_facts", {})[attempt] = facts
    prior = (us.get("attempt_workspaces") or {}).get(attempt) or {}
    meta = {"path": workspace, "branch": intent["branch"],
            "slug": intent["worktree_slug"], "workspace_identity": identity,
            "archived": False,
            "workspace_owner": (workspace_owner
                                or prior.get("workspace_owner")
                                or ("paseo" if workspace_id else "unknown"))}
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
           "launch_host": os.uname().nodename,
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
        "launch_host": rec.get("launch_host"),
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


def _check(unit_dir, launch_facts=None, artifact_basis=None,
           isolation_facts=None, isolation_required=False):
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
    if isolation_facts:
        argv += ["--isolation-facts", json.dumps(
            isolation_facts, sort_keys=True, separators=(",", ":"))]
    if isolation_required:
        argv.append("--isolation-required")
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
#   - every intent carries an idempotency key that a capable receiver can use
#     for deduplication; the key alone does not make a blind replay safe
#   - a CLOSE intent is emitted only from a predicate verdict, never from a
#     unit's own report. An agent saying "done" on a ticket is exactly the
#     self-assertion this whole family refuses.
OUTBOX = "outbox.jsonl"
INTENT_SCHEMA_VERSION = 1
OBSERVATION_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 2
RECONCILIATION_SCHEMA_VERSION = 1
INTENT_CONNECTOR_CAPABILITY = (
    "tracker.intent.idempotent-mutation-readback.v1")
INTENT_OPERATIONS = frozenset(
    ("start", "close", "reopen", "note", "block", "open_pr"))
OPERATION_ACCEPTED = "operation_accepted"
ASYNC_COMPLETED = "asynchronously_completed"
CONFIRMED_BY_READBACK = "confirmed_by_readback"
UNKNOWN = "unknown"
OBSERVATION_OUTCOMES = frozenset(
    (OPERATION_ACCEPTED, ASYNC_COMPLETED, CONFIRMED_BY_READBACK, UNKNOWN))
MUTATION_RESPONSE = "mutation_response"
A2A_LIFECYCLE = "a2a_lifecycle"
RECEIVER_READBACK = "receiver_readback"
RECEIVER_DEDUPLICATION = "receiver_deduplication"
CONFIRMING_SOURCES = frozenset(
    (RECEIVER_READBACK, RECEIVER_DEDUPLICATION))
OBSERVATION_SOURCES = frozenset(
    (MUTATION_RESPONSE, A2A_LIFECYCLE,
     RECEIVER_READBACK, RECEIVER_DEDUPLICATION))

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
    key, allowing receiver-side deduplication after an ambiguous drain."""
    basis = f"{project}\x00{uid}\x00{state}\x00{attempt_dir or ''}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def _intent_evidence_digest(evidence):
    """Digest the exact evidence value carried by the intent envelope."""
    # Keep json.dumps' ASCII escaping. The existing outbox writer accepts
    # surrogateescaped filesystem names by persisting them as ``\udxxx``;
    # ensure_ascii=False would make the following UTF-8 encode crash.
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _intent_attempt_identity(attempt_dir):
    directory = str(attempt_dir) if attempt_dir else None
    return {"id": Path(directory).name if directory else None,
            "directory": directory}


def _intent_envelope(project, uid, attempt_dir, key, verb, evidence):
    """The connector-independent, versioned routing and binding contract."""
    return {
        "schema_version": INTENT_SCHEMA_VERSION,
        "project": project,
        "unit": uid,
        "attempt": _intent_attempt_identity(attempt_dir),
        "idempotency_key": key,
        "requested_operation": verb,
        "evidence_digest": _intent_evidence_digest(evidence),
        "required_connector_capability": INTENT_CONNECTOR_CAPABILITY,
    }


def normalize_intent(intent):
    """Give persisted pre-envelope intents the same read contract as new ones.

    Envelope migration leaves JSONL audit history untouched. Normalizing
    at the sole reader keeps a restart from exposing
    the obsolete connector shape while preserving their original bytes.
    """
    if not isinstance(intent, dict) or "envelope" in intent:
        return intent
    normalized = dict(intent)
    normalized["envelope"] = _intent_envelope(
        intent.get("project"), intent.get("unit"), intent.get("attempt_dir"),
        intent.get("key"), intent.get("verb"), intent.get("evidence"))
    return normalized


# Compatibility name for existing internal callers. The contract owner is
# this module; the project-side drain CLI delegates here rather than carrying
# another implementation.
_with_intent_envelope = normalize_intent


def _nonblank_text(value):
    return isinstance(value, str) and bool(value.strip())


def _schema_is(value, expected):
    """JSON booleans are not integer schema versions (`True == 1` in Python)."""
    return type(value) is int and value == expected


def validate_intent(intent, connector_capabilities=None):
    """Return envelope problems after explicit legacy normalization."""
    if not isinstance(intent, dict):
        return ["intent is not an object"]
    intent = normalize_intent(intent)
    envelope = intent.get("envelope")
    if not isinstance(envelope, dict):
        return ["intent has no versioned `envelope` object"]
    problems = []
    if not _schema_is(envelope.get("schema_version"), INTENT_SCHEMA_VERSION):
        problems.append("unsupported envelope schema_version %r "
                        "(understands %d)" %
                        (envelope.get("schema_version"),
                         INTENT_SCHEMA_VERSION))
    for field in ("project", "unit", "idempotency_key",
                  "requested_operation", "evidence_digest",
                  "required_connector_capability"):
        if not _nonblank_text(envelope.get(field)):
            problems.append("envelope.%s is missing or blank" % field)
    if envelope.get("requested_operation") not in INTENT_OPERATIONS:
        problems.append("envelope.requested_operation is not one of %s" %
                        ", ".join(sorted(INTENT_OPERATIONS)))
    if (envelope.get("required_connector_capability") !=
            INTENT_CONNECTOR_CAPABILITY):
        problems.append("envelope requires unsupported connector capability "
                        "%r" % envelope.get(
                            "required_connector_capability"))
    digest = envelope.get("evidence_digest")
    if (_nonblank_text(digest) and
            (len(digest) != 64 or
             any(c not in "0123456789abcdef" for c in digest))):
        problems.append("envelope.evidence_digest is not a lowercase SHA-256")
    attempt = envelope.get("attempt")
    if not isinstance(attempt, dict):
        problems.append("envelope.attempt is not an object")
    else:
        aid, directory = attempt.get("id"), attempt.get("directory")
        if (aid is None) != (directory is None):
            problems.append("envelope.attempt id and directory must both be "
                            "null or both be present")
        elif directory is not None:
            if not _nonblank_text(aid) or not _nonblank_text(directory):
                problems.append("envelope.attempt id and directory must be "
                                "non-blank strings")
            elif Path(directory).name != aid:
                problems.append("envelope.attempt.id does not name its "
                                "directory")
    aliases = (("project", "project"), ("unit", "unit"),
               ("idempotency_key", "key"),
               ("requested_operation", "verb"))
    for envelope_field, payload_field in aliases:
        if envelope.get(envelope_field) != intent.get(payload_field):
            problems.append("envelope.%s does not match intent.%s" %
                            (envelope_field, payload_field))
    if (isinstance(attempt, dict) and
            attempt.get("directory") != intent.get("attempt_dir")):
        problems.append("envelope.attempt.directory does not match "
                        "intent.attempt_dir")
    if envelope.get("evidence_digest") != _intent_evidence_digest(
            intent.get("evidence")):
        problems.append("envelope.evidence_digest does not match the "
                        "intent evidence")
    if intent.get("verb") == "close" and intent.get("evidence") is None:
        problems.append("a close intent has no evidence")
    if connector_capabilities is not None:
        capability = envelope.get("required_connector_capability")
        if capability not in set(connector_capabilities):
            problems.append("connector does not declare required capability "
                            "%r" % capability)
    return problems


def validate_observation(intent, observation):
    """Return problems binding one connected-session report to an intent."""
    problems = []
    if not isinstance(observation, dict):
        return ["observation is not an object"]
    if not _schema_is(observation.get("schema_version"),
                      OBSERVATION_SCHEMA_VERSION):
        problems.append("unsupported observation schema_version %r" %
                        observation.get("schema_version"))
    outcome = observation.get("outcome")
    if outcome not in OBSERVATION_OUTCOMES:
        problems.append("observation.outcome is not one of %s" %
                        ", ".join(sorted(OBSERVATION_OUTCOMES)))
    source = observation.get("source")
    if source not in OBSERVATION_SOURCES:
        problems.append("observation.source is not one of %s" %
                        ", ".join(sorted(OBSERVATION_SOURCES)))
    envelope = intent.get("envelope") or {}
    bindings = (("project", "project"), ("unit", "unit"),
                ("idempotency_key", "idempotency_key"),
                ("requested_operation", "requested_operation"),
                ("evidence_digest", "evidence_digest"),
                ("connector_capability",
                 "required_connector_capability"))
    for observed, expected in bindings:
        if observation.get(observed) != envelope.get(expected):
            problems.append("observation.%s does not match envelope.%s" %
                            (observed, expected))
    if observation.get("attempt") != envelope.get("attempt"):
        problems.append("observation.attempt does not match envelope.attempt")
    if outcome == CONFIRMED_BY_READBACK:
        if source not in CONFIRMING_SOURCES:
            problems.append("confirmed_by_readback requires receiver_readback "
                            "or receiver_deduplication, not %r" % source)
        if observation.get("matched") is not True:
            problems.append("confirmed_by_readback requires matched=true")
        if not _nonblank_text(observation.get("reference")):
            problems.append("confirmed_by_readback requires the receiver's "
                            "reference")
    elif outcome == OPERATION_ACCEPTED and source != MUTATION_RESPONSE:
        problems.append("operation_accepted requires mutation_response")
    elif outcome == ASYNC_COMPLETED and source != A2A_LIFECYCLE:
        problems.append("asynchronously_completed requires a2a_lifecycle")
    elif outcome == UNKNOWN and observation.get("matched") is True:
        problems.append("unknown cannot claim matched=true")
    return problems


def require_valid_intent(intent, connector_capabilities=None):
    intent = normalize_intent(intent)
    problems = validate_intent(intent, connector_capabilities)
    if problems:
        raise OutboxError("; ".join(problems))
    return intent


def _receipt_admissible(outcome, source, matched):
    """The single policy boundary between remote hints and receipts."""
    return (outcome == CONFIRMED_BY_READBACK
            and source in CONFIRMING_SOURCES
            and matched is True)


def reconcile_observation(intent, observation):
    """Classify offline data; ambiguity and lifecycle never authorize replay."""
    intent = require_valid_intent(intent)
    problems = validate_observation(intent, observation)
    if problems:
        raise OutboxError("; ".join(problems))
    confirmed = _receipt_admissible(
        observation["outcome"], observation.get("source"),
        observation.get("matched"))
    return {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "idempotency_key": intent["envelope"]["idempotency_key"],
        "outcome": observation["outcome"],
        "source": observation["source"],
        "reference": observation.get("reference"),
        "receipt_admissible": confirmed,
        "replay": False,
        "closing_evidence": False,
    }


def _has_bound_merge_evidence(uid, us, evidence):
    """Does *evidence* carry the merge already admitted for this attempt?"""
    receipt = evidence.get("receipt") if isinstance(evidence, dict) else None
    if not isinstance(receipt, dict) or us.get("merge_receipt") != receipt:
        return False
    attempt = Path(us.get("attempt_dir") or "").name
    produced = (us.get("attempt_produced_heads") or {}).get(attempt)
    return (bool(produced)
            and receipt.get("unit") == uid
            and receipt.get("head") == produced
            and receipt.get("merged_as") == us.get("merged_as")
            and receipt.get("pr") == us.get("merge_pr")
            and receipt.get("merged") is True
            and receipt.get("attested") is True
            and _merge_shape_problem(receipt) is None)


def _intent_key(project, uid, unit_state, us, verb, kind, evidence):
    key = outbox_key(project, uid, unit_state, us.get("attempt_dir"))
    if (verb == "block" and unit_state == "NEEDS_HUMAN"
            and us.get("reason") == "deadline_exceeded"):
        # A prior NEEDS_HUMAN intent for a different reason must not suppress
        # the newly declared deadline breach for the same retained attempt.
        # The coordinator-owned sequence also distinguishes a later breach
        # after an operator ratifies a raised deadline for that attempt.
        if us.get("deadline_breach_legacy_key") is True:
            key += "-deadline_exceeded"
        else:
            sequence = us.get("deadline_breach_seq")
            suffix = (str(sequence) if type(sequence) is int and sequence > 0
                      else "legacy")
            key += "-deadline_exceeded-" + suffix
    if verb == "close" and closing_evidence_for(kind) == "merged_pr":
        # Before merge-aware closure, a DONE code unit emitted open_pr under
        # the base key. Include the admitted receipt only for merge closes so
        # that stale entry cannot suppress the correction; compute keys retain
        # their existing idempotency contract.
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        key += "-" + hashlib.sha256(canonical.encode()).hexdigest()[:8]
    return key


def emit_intent(state_dir, project, uid, unit_state, us, evidence=None,
                kind=None, tracker=None):
    """Append one tracker intent. Returns the key, or None if already emitted.

    Deterministic from state: replaying the same local transition produces the
    same key. A connected drainer must deduplicate or read back before retrying
    a remote mutation; the key alone cannot establish what landed."""
    action = TRACKER_EVENTS.get(unit_state)
    if not action:
        return None
    verb, why = action
    if unit_state == "NEEDS_HUMAN" and us.get("reason") == "deadline_exceeded":
        why = "deadline_exceeded"

    # A code predicate reaches READY_FOR_PR, but an admitted merge advances it
    # to DONE. Only the exact merge receipt persisted by that admission may
    # cross this last boundary into a close intent. Keeping the fallback is
    # still belt-and-braces for malformed or legacy DONE state: it may request
    # PR work, but it cannot manufacture a close without merge evidence.
    if (verb == "close" and closing_evidence_for(kind) == "merged_pr"
            and not _has_bound_merge_evidence(uid, us, evidence)):
        verb = "open_pr"
        why = ("the agent finished and its declared outputs exist, which "
               "makes this READY FOR A PR. It is not done: a code unit is "
               "closed by a bound merged-pull-request receipt, and none is "
               "available.")
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
    key = _intent_key(project, uid, unit_state, us, verb, kind, evidence)
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
    if tracker is not None:
        intent["tracker"] = tracker
    intent["envelope"] = _intent_envelope(
        project, uid, us.get("attempt_dir"), key, verb, evidence)
    try:
        _fsync_append(path, intent)
    except (OSError, OutboxError) as e:
        print(f"WARNING: could not append a tracker intent: {e}",
              file=sys.stderr)
        return None
    return key


def backfill_tracker_intents(state_dir, project, units):
    """Add missing labels to historical intents under the coordinator lease.

    The accepted plan supplies labels, never evidence or acknowledgments.
    Existing labels and all other record values survive unchanged. This is an
    additive migration of pre-tracker records, not a replay or a new intent.
    Invalid journals remain intact; failure warns without halting dispatch.
    """
    labels = {uid: u["tracker"] for uid, u in units.items() if "tracker" in u}
    path = Path(state_dir) / OUTBOX
    if not labels:
        return
    temporary = None
    try:
        if not path.is_file():
            return
        # Refuse a partial/corrupt journal before replacing any bytes.
        load_outbox_contract(state_dir)
        lines = path.read_bytes().splitlines(keepends=True)
        changed = False
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            intent = json.loads(line)
            if (intent.get("project") == project
                    and intent.get("unit") in labels and "tracker" not in intent):
                intent["tracker"] = labels[intent["unit"]]
                lines[index] = (json.dumps(intent, sort_keys=True) + "\n").encode()
                changed = True
        if not changed:
            return
        fd, temporary = tempfile.mkstemp(prefix=".outbox-tracker-", dir=str(path.parent))
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), stat.S_IMODE(path.stat().st_mode))
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except (OSError, ValueError, OutboxError) as exc:
        print(f"WARNING: could not backfill tracker labels: {exc}", file=sys.stderr)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError as exc:
                print(f"WARNING: could not remove tracker migration temporary: {exc}",
                      file=sys.stderr)


# --- acknowledgment: did the drain actually land? -------------------------
#
# Every intent was written {"applied": false} and NOTHING ever set it true, so
# after a clean run all eight intents still read pending. A receiver can use
# the key for deduplication, but a blind re-drain after ambiguity is unsafe.
# Worse, the outbox could not answer the one question it exists to answer, and
# a record that never advances is not a record.
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
RECEIPT_CONFIRMED = CONFIRMED_BY_READBACK
RECEIPT_CONFIRMING_SOURCES = CONFIRMING_SOURCES

# The WIRE VALUES matter as much as the printed ones. Round 2 caught me
# relabelling only the text output: --json still emitted "acknowledged", so a
# machine consumer read an attestation as verified tracker success. The value
# itself now carries the weakness, so both paths say the same thing.
UNACKNOWLEDGED = "unacknowledged"
ATTESTED_UNSPECIFIED = "attested"  # historical wire value: source unspecified
ATTESTED_CONFIRMED = "attested_confirmed"
ACKNOWLEDGED = ATTESTED_CONFIRMED   # compatibility name, stronger grade
CONFLICT = "conflict"


def _heal_jsonl_tail(fh):
    """Under an exclusive lock, preserve complete JSON and drop only a tear."""
    fh.seek(0, os.SEEK_END)
    if not fh.tell():
        return
    fh.seek(-1, os.SEEK_END)
    if fh.read(1) == b"\n":
        return
    fh.seek(0)
    data = fh.read()
    cut = data.rfind(b"\n")
    tail = data[cut + 1:]
    try:
        json.loads(tail.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        fh.truncate(0 if cut < 0 else cut + 1)
    else:
        fh.seek(0, os.SEEK_END)
        fh.write(b"\n")


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
                f"journal writes must be serialised, and this filesystem "
                f"will not "
                f"serialise them, so two drainers could interleave and "
                f"corrupt the journal. Record state on a filesystem "
                f"that supports flock.")

        # HEAL, now that nobody else can be writing. A crash can leave the
        # journal ending mid-record with no newline; appending onto that fuses
        # the new receipt to the broken one, turning a recoverable interrupted
        # write into corruption that takes the next record with it. An
        # incomplete record has no meaning, so dropping it loses nothing.
        _heal_jsonl_tail(fh)

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


def _parse_receipt_text(raw_text):
    """Parse already-read receipt bytes into the guarded journal result."""
    lines = raw_text.splitlines()
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
                              "dropped; repeat offline reconciliation after "
                              "the receiver resolves that key."})
            else:
                problems.append({
                    "kind": "corrupt",
                    "detail": "receipt line %d was written in full and does "
                              "not parse, so this is corruption rather than "
                              "an interrupted write." % (idx + 1)})
            continue
        bad = _receipt_shape_problem(rec)
        if bad:
            problems.append({"kind": "malformed",
                             "detail": "receipt line %d: %s" %
                                       (idx + 1, bad)})
            continue
        out.append(rec)
    return _RawJournal(out, problems)


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
      truncated_tail  interrupted local write; repeat reconciliation
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

    return _parse_receipt_text(raw_text)


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
    schema = rec.get("schema_version")
    if (schema is not None and
            not (_schema_is(schema, 1) or
                 _schema_is(schema, RECEIPT_SCHEMA_VERSION))):
        return "unsupported receipt schema_version %r" % schema
    if not _schema_is(schema, RECEIPT_SCHEMA_VERSION):
        if any(rec.get(field) is not None
               for field in ("outcome", "source", "matched")):
            return ("legacy receipts cannot claim a version-2 outcome or "
                    "source")
    else:
        if rec.get("op") not in INTENT_OPERATIONS:
            return "version-2 receipt has no valid requested operation"
        digest = rec.get("evidence_digest")
        if (not _nonblank_text(digest) or len(digest) != 64 or
                any(c not in "0123456789abcdef" for c in digest)):
            return "version-2 receipt has no lowercase SHA-256 evidence digest"
    outcome, source = rec.get("outcome"), rec.get("source")
    if outcome is None:
        if source is not None or rec.get("matched") is not None:
            return "a legacy receipt with no outcome cannot claim a source"
    elif not _receipt_admissible(outcome, source, rec.get("matched")):
        return ("only matched receiver read-back or receiver-side "
                "deduplication may create a confirmed receipt; accepted "
                "operations and lifecycle reports are hints")
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


def _receipt_grade(rec):
    if (_schema_is(rec.get("schema_version"), RECEIPT_SCHEMA_VERSION) and
            _receipt_admissible(rec.get("outcome"), rec.get("source"),
                                rec.get("matched"))):
        return ATTESTED_CONFIRMED
    return ATTESTED_UNSPECIFIED


def _append_receipt_atomic(state_dir, rec):
    """Read, decide and append one receipt under the journal's single lock."""
    bad = _receipt_shape_problem(rec)
    if bad:
        raise OutboxError("refusing malformed receipt: %s" % bad)
    path = Path(state_dir) / RECEIPTS
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    with open(path, "r+b") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise OutboxError(
                "cannot serialise receipt admission on %s: %s. The existing "
                "records must be read under the same lock as the append, or "
                "two drainers can admit different references." % (path, exc))
        _heal_jsonl_tail(fh)
        fh.seek(0)
        try:
            raw = fh.read().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OutboxError("receipt journal is not UTF-8: %s" % exc)
        journal = _parse_receipt_text(raw)
        fatal = fatal_problems(journal.problems)
        if fatal:
            raise OutboxError(
                "the receipt journal cannot be read in full, so no receipt "
                "may be admitted:\n" +
                "\n".join("  [%s] %s" % (f["kind"], f["detail"])
                          for f in fatal))
        same_key = [old for old in journal._records
                    if old.get("key") == rec["key"]]
        refs = {old.get("ref") for old in same_key}
        if refs and refs != {rec["ref"]}:
            raise OutboxError(
                "intent %s already has receipt reference(s) %s; refusing "
                "conflicting %s" %
                (rec["key"], ", ".join(sorted(refs)), rec["ref"]))
        # An equivalent repeat adds no bytes. A confirmed observation may
        # still upgrade a legacy attestation naming the same reference.
        if same_key and (_receipt_grade(rec) == ATTESTED_UNSPECIFIED or
                         any(_receipt_grade(old) == ATTESTED_CONFIRMED
                             for old in same_key)):
            return same_key[-1]
        fh.seek(0, os.SEEK_END)
        fh.write((json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    return rec


def record_receipt(state_dir, key, ref, op=None, by=None, at=None,
                   observation=None):
    """Record a legacy attestation or a bound receiver confirmation.

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

    The historical key/ref form remains a weaker attestation whose source is
    unspecified. Passing a complete observation records the stronger
    read-back-confirmed grade. Neither is verified by this offline process."""
    key = str(key or "").strip()
    ref = str(ref if ref is not None else "").strip()
    if not key or not ref:
        raise OutboxError("a receipt needs a non-blank intent key and tracker "
                          "reference")
    matches = [intent for intent in load_outbox_contract(state_dir)
               if intent.get("key") == key]
    if len(matches) != 1:
        raise OutboxError("no unique persisted outbox intent has key %r" % key)
    intent = require_valid_intent(matches[0])
    envelope = intent["envelope"]
    requested = op or envelope["requested_operation"]
    if requested != envelope["requested_operation"]:
        raise OutboxError("receipt operation %r does not match persisted %r" %
                          (requested, envelope["requested_operation"]))
    outcome = None
    source = None
    matched = None
    if observation is not None:
        result = reconcile_observation(intent, observation)
        if not result["receipt_admissible"]:
            raise OutboxError("only confirmed_by_readback may create a "
                              "confirmed receipt")
        if str(observation.get("reference") or "").strip() != ref:
            raise OutboxError("observation reference does not match --ref")
        outcome = observation["outcome"]
        source = observation["source"]
        matched = observation.get("matched")
    rec = {"key": key, "ref": ref, "op": requested,
           "outcome": outcome, "source": source, "matched": matched,
           "evidence_digest": envelope["evidence_digest"],
           "attested": True,
           "by": by or ((observation or {}).get("by")) or
                 os.environ.get("USER") or "?",
           "at": at or ((observation or {}).get("at")) or time.strftime(
               "%Y-%m-%dT%H:%M:%S%z"),
           "schema_version": RECEIPT_SCHEMA_VERSION}
    return _append_receipt_atomic(state_dir, rec)


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
        if len(refs) > 1:
            grade = CONFLICT
        elif any(_receipt_grade(r) == ATTESTED_CONFIRMED for r in rs):
            grade = ATTESTED_CONFIRMED
        else:
            grade = ATTESTED_UNSPECIFIED
        status[key] = (grade, rs)
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
    target_commit = rec.get("target_commit")
    if target_commit is not None and (
            not isinstance(target_commit, str)
            or len(target_commit) not in (40, 64)
            or any(ch not in "0123456789abcdef" for ch in target_commit)):
        return ("target_commit is not an exact lowercase 40- or 64-character "
                "Git object id")
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
    if rec.get("claim") == V.INTEGRATION_CLAIM:
        for field in ("produced_head", "target_commit", "merge_base",
                      "candidate_tree"):
            if not str(rec.get(field) or "").strip():
                return f"integration receipt has no {field}"
        if rec.get("produced_head") != rec.get("subject_head"):
            return ("integration receipt produced_head disagrees with its "
                    "subject_head")
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
                       policy, repo=None, base_commit=None,
                       target_commit=None):
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
    integration_basis = None
    if claim == V.INTEGRATION_CLAIM:
        if not target_commit:
            return None, (
                "integration-tests evidence has no recorded pre-merge target "
                "commit to bind to. Record the target commit in the merge "
                "attestation; branch-local evidence cannot substitute for it.")
        integration_basis, basis_error = V.candidate_merge_basis(
            U.run, repo, produced, target_commit)
        if basis_error:
            return None, basis_error
    recs, _p = load_verifications(state_dir)
    mine = [r for r in recs if r.get("unit") == unit
            and r.get("claim") == claim]
    if not mine:
        return None, (f"no verification receipt for {unit!r} claiming "
                      f"{claim!r}, which the unit declares it requires. Run "
                      f"it:\n  swarm.py verify --unit {unit} --claim {claim} "
                      f"--verifier NAME --path PATH")
    stale, moved_target, wrong_policy, failed, unauthorized = [], [], [], [], []
    corpus_refusals = []
    for r in mine:
        if str(r.get("subject_head")) != str(produced):
            stale.append(str(r.get("subject_head"))[:12])
            continue
        if claim == V.INTEGRATION_CLAIM:
            if str(r.get("target_commit")) != str(target_commit):
                moved_target.append(str(r.get("target_commit"))[:12])
                continue
            if any(r.get(field) != value
                   for field, value in integration_basis.items()):
                moved_target.append(str(r.get("target_commit"))[:12])
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
        evidence, refusal = V.corpus_evidence(
            U.run, repo, base_commit, produced, entry)
        if refusal:
            corpus_refusals.append(refusal)
            continue
        refusal = V.corpus_change_refusal(entry, evidence, claim)
        if refusal:
            corpus_refusals.append(refusal)
            continue
        if evidence and any(r.get(field) != value
                            for field, value in evidence.items()):
            corpus_refusals.append(
                f"receipt corpus evidence does not match the anchored base "
                f"and produced commit for claim {claim!r}")
            continue
        return r, None
    if failed:
        subject = ("the candidate merge" if claim == V.INTEGRATION_CLAIM
                   else "the produced commit")
        return None, (f"the verifier ran against {subject} and "
                      f"returned FAIL for {claim!r}. That is a result, not a "
                      f"missing receipt: fix the work rather than re-running "
                      f"until it passes.")
    if unauthorized:
        return None, (f"a verification receipt for {unit!r} is bound to the "
                      f"right head and policy, but the verifier it names is "
                      f"not authorized by that policy: {unauthorized[0]}")
    if corpus_refusals:
        return None, corpus_refusals[0]
    if moved_target:
        return None, (
            f"integration verification for {unit!r} tested target commit(s) "
            f"{', '.join(sorted(set(moved_target)))}, but the merge was made "
            f"from target {str(target_commit)[:12]}. The target moved after "
            f"the check, so that evidence is invalid; re-run "
            f"integration-tests against the new candidate merge.")
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
        _set_unit_state(us, "RUNNING")
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


def trusted_launch_host_anchor(state, unit, attempt_dir):
    """Coordinator-state host anchor, including an interrupted code launch.

    Completed launch facts are the normal authority. A code launch can crash
    after its pre-agent intent is durable but before Paseo's cwd completes the
    facts; that intent already contains the coordinator-observed launch host.
    Returning neither is unknown, never permission to act on host-local state.
    """
    facts = trusted_launch_facts(state, unit, attempt_dir)
    if facts is not None:
        return facts
    if not attempt_dir:
        return None
    attempt = Path(attempt_dir).name
    us = (state.get("units") or {}).get(unit) or {}
    intent = (us.get("attempt_launch_intents") or {}).get(attempt)
    return intent if isinstance(intent, dict) else None


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


def trusted_isolation_facts(state, u, attempt_dir):
    """Applied submission boundary for this attempt, from state only.

    The job script and unit.json live in the attempt namespace and are
    audit-only.  A declaration removed or changed after dispatch cannot reuse
    stale facts: the canonical profile digest must still match the plan.
    """
    if not attempt_dir or not isinstance(u, dict) or not u.get("isolation"):
        return None
    attempt = Path(attempt_dir).name
    us = (state.get("units") or {}).get(u.get("id")) or {}
    facts = (us.get("attempt_isolation_facts") or {}).get(attempt)
    if not isinstance(facts, dict):
        return None
    canonical = json.dumps(
        u["isolation"], sort_keys=True, separators=(",", ":"))
    expected_digest = hashlib.sha256(canonical.encode()).hexdigest()
    expected_root = str(Path(attempt_dir).resolve())
    expected_marker = str(Path(expected_root) / ISOLATION_MARKER_NAME)
    token = facts.get("application_token_sha256")
    if not (
            facts.get("schema_version") == 1
            and facts.get("unit_id") == u.get("id")
            and facts.get("attempt_id") == attempt
            and facts.get("applied_to_submission") is True
            and facts.get("mechanism") == "container-host-bind-write-scope"
            and facts.get("backend") == u["isolation"].get("backend")
            and facts.get("image") == u["isolation"].get("image")
            and facts.get("profile_sha256") == expected_digest
            and facts.get("writable_host_binds") == [expected_root]
            and facts.get("application_marker") == expected_marker
            and isinstance(token, str)
            and re.fullmatch(r"[0-9a-f]{64}", token) is not None
            and facts.get("read_only_host_binds") == [
                os.path.normpath(str(p))
                for p in (u["isolation"].get("read_only") or [])]):
        return None
    return facts


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


def admit_merge(state_dir, unit, produced, expect_repo=None, repo=None,
                require_target_binding=False, expect_target=None):
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
    wrong_repo, wrong_target, target_refusals = [], [], []
    for r in mine:
        if str(r.get("head")) != str(produced):
            continue
        if expect_repo and not _same_repo(r.get("repo"), expect_repo):
            # Collect and keep looking. Returning on the first mismatch let a
            # single wrong-repository receipt mask every correct one appended
            # after it, so one attester slip parked the unit forever.
            wrong_repo.append(str(r.get("repo")))
            continue
        if expect_target and r.get("target") != expect_target:
            wrong_target.append(str(r.get("target")))
            continue
        claimed_target = r.get("target_commit")
        if require_target_binding:
            if not claimed_target:
                target_refusals.append(
                    "the integration-tests unit has no recorded pre-merge "
                    "target commit")
                continue
            if not repo:
                target_refusals.append(
                    "the receipt names a pre-merge target commit, but no "
                    "coordinator-held repository was supplied to check it")
                continue
            actual_target, target_error = V.target_before_merge(
                U.run, repo, produced, r.get("merged_as"), r.get("method"),
                claimed_target)
            if target_error:
                target_refusals.append(target_error)
                continue
            if actual_target != claimed_target:
                target_refusals.append(
                    f"the merge object establishes pre-merge target "
                    f"{actual_target[:12]}, not claimed target "
                    f"{str(claimed_target)[:12]}; the target moved after the "
                    f"integration check")
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
    if wrong_target:
        return None, (
            f"merge receipt(s) for {unit!r} target "
            f"{', '.join(sorted(set(wrong_target)))}, not the plan's target "
            f"{expect_target!r}. Record a corrected receipt; a wrong target "
            f"does not block a later correct one.")
    if target_refusals:
        return None, (
            f"no merge receipt for {unit!r} has an admissible pre-merge "
            f"target: {target_refusals[0]}. A connected session must supply "
            f"the merge Git objects locally; the coordinator never contacts "
            f"a forge.")
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
                    out.append(_with_intent_envelope(json.loads(line)))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def load_outbox_contract(state_dir):
    """Read every persisted intent or fail closed for drain decisions."""
    path = Path(state_dir) / OUTBOX
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise OutboxError("cannot read persisted outbox: %s" % exc)
    intents = []
    keys = set()
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            intent = normalize_intent(json.loads(line))
        except ValueError as exc:
            raise OutboxError("outbox line %d is not JSON: %s" %
                              (number, exc))
        problems = validate_intent(intent)
        if problems:
            raise OutboxError("outbox line %d: %s" %
                              (number, "; ".join(problems)))
        key = intent["envelope"]["idempotency_key"]
        if key in keys:
            raise OutboxError("outbox has duplicate idempotency key %r" % key)
        keys.add(key)
        intents.append(intent)
    return intents


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
    problem = W.launch_host_problem(intent)
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
    line = (f"{u['id']}: DRY RUN -- would preserve and clean worktree {target} "
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
    """Preserve, restore-check, then remove a finished code worktree.

    Coordinator-owned worktrees are removed directly with Git. Legacy
    Paseo-owned workspaces are archived through Paseo. Neither destructive
    path is reachable until an atomic audit snapshot has preserved tracked and
    untracked bytes, bound them to the coordinator-state base identity, and
    restored an identical copy. Recovery records are cleanup inputs only;
    judging and retry code never consume them.
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
    if state_dir is None:
        _worktree_cleanup_failed(
            state, u, meta, report,
            "refusing worktree cleanup without coordinator state_dir for "
            "a durable recovery snapshot")
        return
    intent = (us.get("attempt_launch_intents") or {}).get(attempt)
    intent_problem = _code_launch_intent_problem(intent, u, attempt)
    if intent_problem:
        _worktree_cleanup_failed(
            state, u, meta, report,
            f"cannot bind recovery snapshot: {intent_problem}")
        save_state(state_dir, state)
        return
    recovery_records = us.setdefault("attempt_recovery_snapshots", {})
    workspace_path = meta.get("path")
    if workspace_path and os.path.isdir(workspace_path):
        workspace_identity = meta.get("workspace_identity") or {}
        expected_git_dir = workspace_identity.get("git_dir")
        expected_git_pointer_sha256 = workspace_identity.get(
            "git_pointer_sha256")
        recovery_record, recovery_error = R.preserve_worktree(
            workspace_path, Path(state_dir) / "recovery-snapshots",
            u["id"], attempt, intent["base_commit"], intent["base_tree"],
            expected_git_dir=expected_git_dir,
            expected_git_pointer_sha256=expected_git_pointer_sha256)
        if recovery_error:
            _worktree_cleanup_failed(
                state, u, meta, report,
                f"worktree preservation failed; checkout retained: "
                f"{recovery_error}")
            save_state(state_dir, state)
            return
        recovery_records[attempt] = recovery_record
        meta["recovery_snapshot"] = "restore-checked"
        # The completed record is durable before either destructive cleanup
        # call. A crash may retry cleanup but cannot lose this prerequisite.
        save_state(state_dir, state)
        current_digest, current_error = R.tree_digest(
            workspace_path, expected_git_dir,
            expected_git_pointer_sha256)
        if (current_error
                or current_digest != recovery_record["content_sha256"]):
            _worktree_cleanup_failed(
                state, u, meta, report,
                "worktree changed after its recovery snapshot was saved; "
                "checkout retained: "
                + (current_error or "content digest differs"))
            save_state(state_dir, state)
            return
    else:
        recovery_record = recovery_records.get(attempt)
        recovery_error = R.validate_snapshot(
            recovery_record, u["id"], attempt,
            intent["base_commit"], intent["base_tree"])
        if recovery_error:
            legacy_paseo = (meta.get("workspace_owner") == "paseo"
                            or (not meta.get("workspace_owner")
                                and bool(meta.get("workspace_id"))))
            if legacy_paseo:
                # Persisted legacy state can describe a Paseo-owned checkout
                # that Paseo deleted before preservation became mandatory.
                # No destructive action remains to gate and the lost bytes
                # cannot be reconstructed. Record that bounded migration fact
                # without inventing a snapshot or completion authority.
                meta["recovery_snapshot"] = (
                    "unavailable-before-preservation-enforcement")
                meta["archived"] = True
                meta.pop("cleanup_pending", None)
                meta["archived_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z")
                meta["cleanup_problem"] = (
                    "legacy checkout was already absent before preservation "
                    "enforcement; no recovery bytes exist")
                save_state(state_dir, state)
                report.append(
                    f"{u['id']}: legacy worktree {workspace_path} was already "
                    "absent before preservation enforcement; recorded the "
                    "unrecoverable migration without running cleanup")
                return
            _worktree_cleanup_failed(
                state, u, meta, report,
                "worktree disappeared without a valid recovery snapshot: "
                + recovery_error)
            save_state(state_dir, state)
            return
        meta["recovery_snapshot"] = "restore-checked"
        meta["archived"] = True
        meta.pop("cleanup_pending", None)
        meta["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_state(state_dir, state)
        return

    if meta.get("workspace_owner") == "coordinator":
        rc, out, err = U.run(
            ["git", "-C", intent["repo"], "worktree", "remove", "--force",
             str(workspace_path)], timeout=120)
        if rc != 0:
            _worktree_cleanup_failed(
                state, u, meta, report,
                f"could not remove coordinator-owned worktree "
                f"{workspace_path}: {(err or out).strip()[:200]}")
            save_state(state_dir, state)
            return
        meta["archived"] = True
        meta.pop("cleanup_pending", None)
        meta["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_state(state_dir, state)
        report.append(
            f"{u['id']}: preserved and removed finished worktree "
            f"{workspace_path}; branch {meta.get('branch')} remains")
        return

    workspace_id = meta.get("workspace_id")
    if not workspace_id and meta.get("path"):
        workspace_id = _paseo_workspace_for_path(meta.get("path"))
        if workspace_id:
            meta["workspace_id"] = workspace_id
            # Persist newly-discovered cleanup authority before using it.
            if state_dir is not None:
                save_state(state_dir, state)
    if not workspace_id:
        _worktree_cleanup_failed(
            state, u, meta, report,
            f"could not identify cleanup owner for preserved attempt "
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
    report.append(f"{u['id']}: preserved and archived legacy worktree "
                  f"{meta.get('path')}; branch {meta.get('branch')} remains")


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
    units = {u["id"]: u for u in _units_with_canary(plan)}
    report, dispatched, halted = [], 0, state.get("halted")
    advance_observed_at = time.time()
    dispatch_targets = {}
    dispatch_refusal = None

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
                      if (_unit_state(state, uid).get("attempt_dir")
                          and not str(
                              _unit_state(state, uid).get("job_id") or "")
                          .startswith("dry-")
                          and _occupies_live_resources(
                              units[uid], _unit_state(state, uid))))
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

    # Persist a state clock for snapshots created before state_changed_at
    # existed. The honest reference is when this coordinator first observed
    # the old state, not allocated_at: a unit may have changed states long
    # after allocation, and pretending otherwise would overstate its stall.
    clock_observed_at = advance_observed_at
    if not dry_run:
        for uid in units:
            _normalise_state_clock(_unit_state(state, uid), clock_observed_at)

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
            _set_unit_state(us, "READY_FOR_PR")
            report.append(
                f"{uid}: recorded DONE, but a {u.get('kind')} unit is closed "
                f"by a merged pull request, not by its own receipt. Corrected "
                f"to READY_FOR_PR; anything depending on it waits.")
            save_state(state_dir, state)

    # Worktrees are needed while an agent can still be continued. Once an
    # attempt has a terminal judgment (including READY_FOR_PR), its committed
    # branch is the durable handoff and the checkout is operational debris.
    # Retry failed cleanups on every advance; this bounds managed worktrees to
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

    # A per-unit deadline is an escalation boundary, not a lease and not a
    # cancellation claim. Keep the attempt, job binding and output claim in
    # place. The boundary is a reason to inspect the attempt, so the checker
    # still runs below; only a checked non-terminal result is escalated.
    # Raising/removing the deadline via the existing plan-ratification path
    # ends the active breach and lets a later crossing become a distinct event.
    deadline_observed_at = advance_observed_at
    for uid, u in sorted(units.items()):
        us = _unit_state(state, uid)
        host_local, host_problem = _deadline_host_evaluation(state, uid, us)
        if (u.get("deadline_s") is not None and us.get("attempt_dir")
                and host_local is not True):
            report.append(
                f"{uid}: DEADLINE_NOT_EVALUABLE_HERE -- {host_problem}")
            continue
        if (not dry_run
                and not _deadline_exceeded(
                    u, us, state, uid, deadline_observed_at)):
            _clear_deadline_breach(us)
    if not dry_run:
        save_state(state_dir, state)

    # An attempt allocated but never bound may still have reached the
    # scheduler. Ask before dispatching anything else.
    for uid, u in sorted(units.items()):
        us = _unit_state(state, uid)
        if dry_run or not us["attempt_dir"]:
            continue
        if (u.get("kind") == "code" and us.get("job_id")
                and not trusted_launch_facts(
                    state, uid, us["attempt_dir"])):
            host_problem = W.launch_host_problem(
                trusted_launch_host_anchor(
                    state, uid, us["attempt_dir"]))
            if host_problem:
                us["launch_recovery_pending"] = True
                us["host_judgment"] = "UNJUDGEABLE_HERE"
                us.pop("launch_recovery_problem", None)
                report.append(f"{uid}: UNJUDGEABLE_HERE -- {host_problem}. "
                              f"The agent is retained for a coordinator on "
                              f"its launch host.")
                save_state(state_dir, state)
                continue
            recovery_error = _recover_code_launch(
                state, u, us["attempt_dir"], us["job_id"])
            if recovery_error:
                us["launch_recovery_problem"] = recovery_error
                _set_unit_state(us, "NEEDS_HUMAN")
                # No trusted launch facts means the checker cannot run. A
                # deadline crossed during that failed recovery may still
                # supersede the recovery reason without inventing a verdict.
                if _deadline_exceeded(u, us, state, uid):
                    first_breach = _mark_deadline_exceeded(u, us)
                    if first_breach:
                        report.append(
                            f"{uid}: NEEDS_HUMAN -- deadline_exceeded during "
                            f"attempt recovery. The attempt remains bound; "
                            f"no cancellation, claim release, or retry was "
                            f"requested.")
                report.append(f"{uid}: NEEDS_HUMAN -- {recovery_error}. The "
                              f"agent remains bound and no launch facts were "
                              f"admitted; repair or abandon this exact "
                              f"attempt before it can be judged.")
                save_state(state_dir, state)
                continue
            us.pop("launch_recovery_problem", None)
            _set_unit_state(us, "SUBMITTED")
            report.append(f"{uid}: recovered the agent's attempt worktree and "
                          f"completed its trusted launch snapshot")
            save_state(state_dir, state)
        if us.get("bind_pending") and us.get("job_id"):
            if not _bind(us["attempt_dir"], us["job_id"]):
                us.pop("bind_pending", None)
                _set_unit_state(us, "SUBMITTED")
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
            _set_unit_state(us, None)
            save_state(state_dir, state)
            continue
        if us.get("job_id"):
            continue
        job, note = reconcile_orphan(us["attempt_dir"], us.get("allocated_at"),
                                     kind=u.get("kind"))
        if job:
            if u.get("kind") == "code":
                host_problem = W.launch_host_problem(
                    trusted_launch_host_anchor(
                        state, uid, us["attempt_dir"]))
                if host_problem:
                    # The discovered agent remains bound in coordinator state
                    # so it is not rediscovered as an orphan, but no path or
                    # inode from its launch host is inspected here.
                    us["job_id"] = job
                    us["launch_recovery_pending"] = True
                    us["host_judgment"] = "UNJUDGEABLE_HERE"
                    us.pop("launch_recovery_problem", None)
                    report.append(f"{uid}: {note}; UNJUDGEABLE_HERE -- "
                                  f"{host_problem}. The agent is retained for "
                                  f"a coordinator on its launch host.")
                    save_state(state_dir, state)
                    continue
                recovery_error = _recover_code_launch(
                    state, u, us["attempt_dir"], job)
                if recovery_error:
                    # Bind the identity durably so the next advance asks this
                    # exact agent again instead of repeating title discovery.
                    us["job_id"] = job
                    us["launch_recovery_pending"] = True
                    us["launch_recovery_problem"] = recovery_error
                    _set_unit_state(us, "NEEDS_HUMAN")
                    if _deadline_exceeded(u, us, state, uid):
                        first_breach = _mark_deadline_exceeded(u, us)
                        if first_breach:
                            report.append(
                                f"{uid}: NEEDS_HUMAN -- deadline_exceeded "
                                f"during attempt recovery. The attempt "
                                f"remains bound; no cancellation, claim "
                                f"release, or retry was requested.")
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
            _set_unit_state(us, "SUBMITTED")
            save_state(state_dir, state)
            bind_error = _bind(us["attempt_dir"], job)
            if bind_error:
                report.append(f"{uid}: recovered {job} but {bind_error}; "
                              f"binding will retry next advance")
            else:
                us.pop("bind_pending", None)
            report.append(f"{uid}: {note}")
            save_state(state_dir, state)
        elif _deadline_exceeded(u, us, state, uid):
            _mark_deadline_exceeded(u, us)
            report.append(
                f"{uid}: NEEDS_HUMAN -- deadline_exceeded while recovering "
                f"the unbound attempt. It remains allocated; no submission, "
                f"release, or retry was performed.")
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
                        target, problem = _dispatch_target_for_advance(
                            u, dispatch_targets)
                        if not problem:
                            source, problem = _dispatch_source_identity(u, target)
                        if not problem:
                            recovered_job, problem = _submit(
                                u, us["attempt_dir"], False, state, state_dir,
                                dispatch_source=source)
                        if problem:
                            _set_unit_state(us, "NEEDS_HUMAN")
                            us["launch_recovery_problem"] = problem
                            report.append(f"{uid}: NEEDS_HUMAN -- {problem}")
                            save_state(state_dir, state)
                            continue
                        us["job_id"] = str(recovered_job)
                        _set_unit_state(us, "SUBMITTED")
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
        if not us["attempt_dir"]:
            continue
        attempt = us["attempt_dir"]
        launch_facts = trusted_launch_facts(state, uid, attempt)
        host_problem = W.launch_host_problem(
            trusted_launch_host_anchor(state, uid, attempt))
        if host_problem:
            us["host_judgment"] = "UNJUDGEABLE_HERE"
            us.pop("incomplete_since", None)
            us.pop("launch_recovery_problem", None)
            report.append(f"{uid}: UNJUDGEABLE_HERE -- {host_problem}")
            save_state(state_dir, state)
            continue
        if us.get("launch_recovery_problem"):
            report.append(f"{uid}: NEEDS_HUMAN -- "
                          f"{us['launch_recovery_problem']}")
            continue
        us.pop("host_judgment", None)
        if us["state"] == "DONE":
            continue
        pinned_before = trusted_produced_head(state, uid, attempt)
        ran_check = not (u.get("kind") == "code" and pinned_before)
        protocol_problem = None
        if ran_check:
            isolation_facts = trusted_isolation_facts(state, u, attempt)
            check_args = (
                attempt, launch_facts,
                trusted_artifact_basis(state, uid, attempt))
            # Preserve the historical call shape for undeclared units and old
            # embedders. A declared profile takes the fourth, trusted-by-value
            # argument; there is no attempt-directory fallback.
            check_result = (_check(
                *check_args, isolation_facts, isolation_required=True)
                if u.get("isolation") is not None else _check(*check_args))
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
            if u.get("isolation") is not None and not isolation_facts:
                protocol_problem = (
                    "the unit declares OS-backed isolation, but coordinator "
                    "state has no matching applied wrapper facts for this "
                    "attempt. Refusing rather than degrading to the trusted-"
                    "writer basis")
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
                    U.run, launch_facts, produced)
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
        previous_state_changed_at = us.get("state_changed_at")
        previous_state_changed_at_basis = us.get("state_changed_at_basis")
        checked_state = ("FAILED_EVIDENCE" if protocol_problem
                         else NAME.get(rc, f"rc={rc}"))

        # A deadline is a reason to inspect, never a reason to skip the only
        # checker. Apply terminal execution evidence first. For a non-terminal
        # result, re-evaluate after the check so PREEMPTED cannot mint a fresh
        # attempt across the boundary. Keeping the active breach state in
        # place also prevents one continuously exceeded deadline from looking
        # like a new event on every poll.
        terminal = {"DONE", "FAILED", "FAILED_EVIDENCE", "READY_FOR_PR",
                    "PREFLIGHT_REFUSED", "HELD"}
        if (checked_state not in terminal
                and _deadline_exceeded(u, us, state, uid)):
            if dry_run:
                report.append(
                    f"{uid}: DRY RUN -- deadline_exceeded after check; would "
                    f"become NEEDS_HUMAN without cancellation, claim "
                    f"release, or retry")
                continue
            first_breach = _mark_deadline_exceeded(u, us)
            if first_breach:
                report.append(
                    f"{uid}: NEEDS_HUMAN -- deadline_exceeded while the "
                    f"attempt was being checked. It remains bound and its "
                    f"output claim is retained; no cancellation or retry "
                    f"was requested.")
            save_state(state_dir, state)
            continue
        _set_unit_state(us, checked_state)
        if checked_state in terminal:
            _clear_deadline_breach(us)

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
                _set_unit_state(us, "NEEDS_HUMAN")
            report.append(f"{uid}: convergence {verdict} -- " +
                          " ".join(str(w) for w in why))

        # A CODE UNIT IS NOT DONE WHEN ITS PREDICATE PASSES. The receipt says
        # an agent went idle and files exist; the accepted form of that work
        # is a merged PR. Rewriting only the tracker intent left the unit
        # DONE in durable state, so dependents dispatched before any merge and
        # the DAG contradicted the tracker -- the fix was cosmetic. Found by a
        # reviewer.
        #
        # A merge IS recorded now -- `us["merged_as"]` below, read a few
        # lines down -- so the older note here claiming otherwise was a limit
        # that outlived its cause. A stale limit is not harmless: it reads as
        # a standing gap and invites a workaround for a solved problem.
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
            merge_args = {
                "expect_repo": (launch_facts or {}).get("repository_remote")}
            if V.INTEGRATION_CLAIM in (u.get("requires_verification") or []):
                merge_args["repo"] = (launch_facts or {}).get("repo")
                merge_args["require_target_binding"] = True
                merge_args["expect_target"] = u.get("target_branch")
            merge_receipt, merge_refusal = admit_merge(
                state_dir, uid, produced, **merge_args)
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
                    policy=_pol, repo=(launch_facts or {}).get("repo"),
                    base_commit=base,
                    target_commit=((merge_receipt or {}).get("target_commit")
                                   if claim == V.INTEGRATION_CLAIM else None))
                if vrefusal:
                    break

            if immutable_problem and not vrefusal:
                vrefusal = immutable_problem
            receipt, refusal = ((None, vrefusal) if vrefusal else
                                (merge_receipt, merge_refusal))
            if receipt:
                _set_unit_state(us, "DONE")
                us["merged_as"] = receipt.get("merged_as")
                us["merge_pr"] = receipt.get("pr")
                # Persist the exact admitted receipt. The outbox is emitted
                # after state is saved and must not re-infer closure from the
                # predicate receipt, which establishes only READY_FOR_PR.
                us["merge_receipt"] = dict(receipt)
                report.append(
                    f"{uid}: DONE on a merged PR ({receipt.get('pr')}, "
                    f"{receipt.get('method')} as "
                    f"{str(receipt.get('merged_as'))[:12]}). The merge itself "
                    f"is attested, not verified; the head it pins was "
                    f"produced by this attempt.")
            else:
                _set_unit_state(us, "READY_FOR_PR")
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
                    _set_unit_state(us, "FAILED")
                    report.append(
                        f"{uid}: the job finished cleanly and its declared "
                        f"outputs never appeared, {waited}s on. This is a "
                        f"FAILED unit, not missing evidence: the scheduler "
                        f"will tell you it succeeded. Read the job's own log "
                        f"in {us['attempt_dir']}.")
                elif reason == U.REASON_NO_PRODUCED_CHANGE:
                    # A refused repository transition is not a planning-only
                    # turn: do not turn continuation into a correction loop.
                    _set_unit_state(us, "FAILED")
                    receipt, why = attested_receipt(
                        state, uid, us["attempt_dir"])
                    details = " ".join(
                        W.render_for_record(note, 2048)
                        for note in ((receipt or {}).get("notes") or [])
                        if not str(note).startswith("REASON="))
                    report.append(
                        f"{uid}: declared outputs are present, but no produced "
                        f"repository change was established, {waited}s on. "
                        f"This is a FAILED unit. "
                        f"{details or why or 'See the check receipt.'}")
                else:
                    _set_unit_state(us, "FAILED_EVIDENCE")
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
                _set_unit_state(us, "FAILED")
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
        if us.get("state") == previous:
            # Policy can pass through internal states and return to the same
            # operator-visible state. Polling is not a transition, so retain
            # the original time-in-state rather than resetting it here.
            us["state_changed_at"] = previous_state_changed_at
            if previous_state_changed_at_basis is None:
                us.pop("state_changed_at_basis", None)
            else:
                us["state_changed_at_basis"] = previous_state_changed_at_basis
        if us.get("state") in WORKTREE_CLEANUP_STATES:
            # The checker result, produced head, and terminal state are the
            # conclusion that justifies teardown. Persist that conclusion
            # before Paseo can remove the evidence used to reach it.
            if not dry_run:
                if u.get("kind") == "code":
                    watch = _current_code_terminal_watch(
                        state, uid, Path(attempt).name, us.get("job_id"))
                    if watch is not None:
                        watch["status"] = "checked"
                        watch["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                save_state(state_dir, state)
                _archive_code_worktree(
                    state, u, attempt, report, state_dir)
            else:
                _report_would_archive_code_worktree(
                    state, u, attempt, report)
    save_state(state_dir, state)

    # 1b. Release the output claims of units that are no longer live (B6).
    #     Recomputed from the plan and released only when the claim names
    #     THIS state directory, so a coordinator can free its own claims --
    #     including ones it left behind by dying -- and can never free
    #     anybody else's. This is what keeps a crash from wedging a project
    #     without a TTL: our own state file is authority for our own units,
    #     and `acquire_lease` already serialises access to it.
    if not dry_run:
        for uid, u in sorted(units.items()):
            if not _occupies_live_resources(u, _unit_state(state, uid)):
                _release_output_claims(u, root, state_dir)

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
        # A root has no dependency that could clear its persisted hold.
        # Keep a held canary terminal for the ordinary upstream path below.
        if uid == plan.get("canary") and us["state"] == "HELD":
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
            _set_unit_state(us, "HELD")
            report.append(f"{uid}: held, upstream "
                          f"{', '.join(failed_upstream)} will not complete")
            save_state(state_dir, state)
            continue
        unmet = [d for d in needs
                 if _unit_state(state, d)["state"] != "DONE"]
        if unmet:
            if plan.get("canary") in unmet:
                report.append(f"{uid}: waiting on canary {plan['canary']}")
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
                if _occupies_live_resources(units[x], _unit_state(state, x))]
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

        dispatch_source = None
        if u.get("kind") == "code":
            target, problem = _dispatch_target_for_advance(u, dispatch_targets)
            if not problem:
                dispatch_source, problem = _dispatch_source_identity(u, target)
            if problem:
                report.append(f"{uid}: REFUSING dispatch -- {problem}")
                dispatch_refusal = str(problem)
                continue

        unit_dir, err = _allocate(plan, u, root)
        if err:
            _set_unit_state(us, "FAILED")
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
        allocated_at = time.time()
        us["allocated_at"] = allocated_at
        _set_unit_state(us, "ALLOCATED", allocated_at)
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

        # B6, and the reason it sits HERE. The lease excludes a second
        # controller over this state directory; it says nothing about a
        # second state directory over this unit's output namespace, which is
        # exactly what three ad-hoc sub-plans of one project produce. A dry
        # run takes no claim, because it creates no writer for one to protect
        # against and a placeholder claim in a shared registry would refuse
        # honest work.
        claim_refusal, held_claims = (None, [])
        if not dry_run:
            claim_refusal, held_claims = _take_output_claims(
                u, root, state_dir, Path(unit_dir).name)
        submit_args = ({"dispatch_source": dispatch_source}
                       if dispatch_source is not None else {})
        job_id, err = ((None, claim_refusal) if claim_refusal
                       else _submit(u, unit_dir, dry_run, state, state_dir,
                                    **submit_args))
        if err:
            # Whatever we claimed for an attempt that never started must not
            # outlive it. `_release_output_claims` recomputes from the plan
            # and only frees claims naming this state directory, so this is
            # the same operation the top of `advance` performs, not a second
            # implementation of it.
            if held_claims:
                _release_output_claims(u, root, state_dir)
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
                    # WHICH preflight. Three of them refuse here now, and a
                    # zero dirty count rendered as "uncommitted changes" is a
                    # plain misstatement of why nothing ran.
                    "reason": err.reason,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
                if us.get("attempts"):
                    last = str(us["attempts"][-1])
                    if last in (str(unit_dir), f"{DRY_PREFIX}{unit_dir}"):
                        us["attempts"].pop()
                us["attempt_dir"] = None
                _set_unit_state(us, "PREFLIGHT_REFUSED")
                us.pop("allocated_at", None)
                us["gpu_hours"] = max(
                    0.0, float(us.get("gpu_hours") or 0) - want)
                spent -= want
                cited = us["preflight_refusals"][-1]["receipt"]
                report.append(f"{uid}: {err}" +
                              (f"\n  receipt: {cited}" if cited else ""))
            else:
                _set_unit_state(us, "FAILED")
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
        _set_unit_state(us, "SUBMITTED")
        if needs_bind:
            us["bind_pending"] = True
        # Persist the binding authority before unit.py writes its marker.
        save_state(state_dir, state)
        # Now the claim can name the job as well as the attempt. Best effort
        # and non-authoritative: the attempt id already names the scheduler
        # job (`swarm-<attempt>`), so a claim that never gets this update is
        # still adjudicable. It is written for the human reading a refusal.
        if held_claims:
            _note_claimed_attempt(u, root, state_dir, Path(unit_dir).name,
                                  job_id)
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
    if not dry_run:
        backfill_tracker_intents(state_dir, project, units)
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
        kind = (units.get(uid) or {}).get("kind")
        if now == "DONE" and us.get("attempt_dir"):
            # The verdict itself, so a drain never closes on a self-report.
            # Attested, or it is a self-report by the other party: an
            # unattested receipt shipped into the tracker as "evidence" is
            # exactly the word this field exists to earn.
            if closing_evidence_for(kind) == "merged_pr":
                rp = us.get("merge_receipt")
                # Repair persisted state written by the old coordinator,
                # which recorded the merge commit and PR but discarded the
                # admitted receipt before the outbox could carry it.
                if not _has_bound_merge_evidence(
                        uid, us, {"receipt": rp} if rp else None) and not dry_run:
                    produced = trusted_produced_head(
                        state, uid, us["attempt_dir"])
                    facts = trusted_launch_facts(
                        state, uid, us["attempt_dir"])
                    try:
                        recover_args = {
                            "expect_repo": (facts or {}).get(
                                "repository_remote")}
                        if V.INTEGRATION_CLAIM in (
                                (units.get(uid) or {}).get(
                                    "requires_verification") or []):
                            recover_args["repo"] = (facts or {}).get("repo")
                            recover_args["require_target_binding"] = True
                            recover_args["expect_target"] = (
                                (units.get(uid) or {}).get("target_branch"))
                        recovered, _why = admit_merge(
                            state_dir, uid, produced, **recover_args)
                    except OutboxError:
                        recovered = None
                    if (recovered
                            and recovered.get("merged_as") == us.get(
                                "merged_as")
                            and recovered.get("pr") == us.get("merge_pr")):
                        us["merge_receipt"] = dict(recovered)
                        save_state(state_dir, state)
                        rp = recovered
            else:
                rp, _why = attested_receipt(
                    state, uid, us["attempt_dir"])
            evidence = {"receipt": rp} if rp else None
        if dry_run:
            action = TRACKER_EVENTS.get(now)
            if action:
                verb = action[0]
                if (verb == "close"
                        and closing_evidence_for(kind) == "merged_pr"
                        and not _has_bound_merge_evidence(
                            uid, us, evidence)):
                    verb = "open_pr"
                key = _intent_key(
                    project, uid, now, us, verb, kind, evidence)
            if action and key not in existing_outbox_keys:
                if verb == "close" and not evidence:
                    report.append(
                        f"{uid}: DRY RUN -- would retry outbox emission for "
                        f"{now}, but no attested closing receipt is readable")
                else:
                    report.append(
                        f"{uid}: DRY RUN -- would re-emit tracker {verb} "
                        f"intent from durable state {now}")
            continue
        emit_intent(state_dir, project, uid, now, us, evidence, kind=kind,
                    tracker=units[uid].get("tracker"))
    return report, dispatched, halted or dispatch_refusal


def _load_plan(path):
    plan, err = U.read_json(path)
    if err:
        sys.exit(f"error: no readable plan at {path}: {err}")
    # Dispatch validates against the survey too, not just `validate`. A
    # refusal that only fires when someone runs the optional command is a
    # refusal that fires after the DAG is live.
    survey, _note = discover_survey(path)
    try:
        validate_plan(plan, survey)
    except PlanError as e:
        sys.exit(f"error: invalid plan: {e}")
    except RecursionError:
        sys.exit("error: the dependency graph is too deep to validate; it is "
                 "probably cyclic in a way the checker could not unwind.")
    return plan


CODE_TERMINAL_WATCH_LOG = "code-terminal-watch.log"


def _code_terminal_outcome_path(state_dir, uid, attempt, agent):
    # The record belongs to the coordinator, outside the worker's write root.
    key = json.dumps([uid, attempt, str(agent)], separators=(",", ":"))
    digest = hashlib.sha256(key.encode()).hexdigest()
    return Path(state_dir) / ("code-terminal-outcome-" + digest + ".json")


def _write_code_terminal_outcome(args, outcome):
    """Retain diagnostics even while another controller holds the lock.

    This per-watcher record is never judgment authority. Only its diagnostic
    fields are imported into swarm-state.json under the project lock.
    """
    path = _code_terminal_outcome_path(
        args.state_dir, args.unit, args.attempt, args.agent)
    record = {"unit": args.unit, "attempt": args.attempt,
              "agent_id": str(args.agent), "outcome": outcome}
    err = U.write_json(path, record)
    if err:
        print(f"cannot record terminal-watch outcome: {err}", flush=True)


def _code_terminal_log_tail(path):
    """A bounded diagnostic tail; a replaced FIFO must not stall status."""
    import stat
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return "[log is not a regular file]"
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4000))
            return fh.read(4000).decode("utf-8", "replace")
    except OSError as exc:
        return f"[log unavailable: {exc}]"


def _code_terminal_process(watch, attempt):
    """Local diagnostic liveness, never a fact used to judge the unit."""
    if watch.get("host") != os.uname().nodename:
        return "unknown", "watcher host is different or unrecorded"
    pid = watch.get("pid")
    if type(pid) is not int or pid <= 0:
        return "unknown", "watcher launch has no recorded pid"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent", "watcher process no longer exists"
    except OSError as exc:
        return "unknown", f"cannot inspect watcher process: {exc}"
    rc, out, _err = U.run(
        ["ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "command="],
        timeout=5)
    fields = out.split(None, 1)
    if rc != 0 or len(fields) != 2:
        return "unknown", "ps did not establish watcher liveness"
    if fields[0].startswith("Z"):
        return "absent", "watcher process is a zombie"
    # ps renders argv without preserving argument boundaries. Never infer a
    # mismatch from a value its display cannot represent unambiguously.
    if any(char.isspace() for value in (attempt, str(watch.get("agent_id")))
           for char in value):
        return "unknown", "ps cannot distinguish watcher arguments containing whitespace"
    # Check the complete ps output, not a shortened display, so an unrelated
    # reused pid does not read as a live watcher just because kill succeeded.
    tokens = fields[1].split()
    pairs = list(zip(tokens, tokens[1:]))
    if ("watch-code-terminal" not in tokens
            or ("--attempt", attempt) not in pairs
            or ("--agent", str(watch.get("agent_id"))) not in pairs):
        return "absent", "recorded pid now names a different command"
    return "present", "watcher process present; this is not work progress"


def _read_code_terminal_outcome(state_dir, uid, attempt, agent):
    record, err = U.read_json(_code_terminal_outcome_path(
        state_dir, uid, attempt, agent))
    if err == "missing":
        return {}, None
    if err or not isinstance(record, dict):
        return {}, "unreadable terminal-watch outcome ignored"
    if (record.get("unit") != uid or record.get("attempt") != attempt
            or record.get("agent_id") != agent):
        return {}, "stale terminal-watch outcome ignored: unit/attempt/agent mismatch"
    if not isinstance(record.get("outcome"), dict):
        return {}, "malformed terminal-watch outcome ignored"
    # Deliberately no produced_head, job id, unit state or other authority.
    return {key: record["outcome"][key] for key in CODE_TERMINAL_DIAGNOSTICS
            if key in record["outcome"]}, None


CODE_TERMINAL_DIAGNOSTICS = (
    "status", "observed_at", "wait_exit_code", "reason", "log_tail", "checked_at")


def _current_code_terminal_watch(state, uid, attempt, agent):
    """Use only the coordinator's current attempt and exact agent binding."""
    us = state.get("units", {}).get(uid) or {}
    current = Path(us["attempt_dir"]).name if us.get("attempt_dir") else None
    watch = (us.get("code_terminal_watches") or {}).get(attempt)
    if (attempt != current or agent != us.get("job_id")
            or not isinstance(watch, dict) or watch.get("agent_id") != agent):
        return None
    return watch


def _observe_code_terminal_watch(state_dir, uid, attempt, watch):
    """Return a diagnostic snapshot without mutating state or taking a lease."""
    result = dict(watch)
    outcome, ignored = _read_code_terminal_outcome(
        state_dir, uid, attempt, watch.get("agent_id"))
    result.update(outcome)
    # The outcome describes wait, while this field records a later locked
    # coordinator check. Re-reading the retained outcome must not undo it.
    if watch.get("status") == "checked":
        result["status"] = "checked"
        result["checked_at"] = watch.get("checked_at")
    if ignored:
        result["outcome_ignored"] = ignored
    if result.get("status") in ("starting", "waiting"):
        liveness, reason = _code_terminal_process(watch, attempt)
        result["process_state"] = liveness
        result["process_reason"] = reason
        result["process_observed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if liveness == "absent":
            # Publication and exit can happen between the read and the probe.
            outcome, ignored = _read_code_terminal_outcome(
                state_dir, uid, attempt, watch.get("agent_id"))
            result.update(outcome)
            if ignored:
                result["outcome_ignored"] = ignored
            if result.get("status") in ("starting", "waiting"):
                result.update({
                    "status": "exited_without_idle", "wait_exit_code": None,
                    "observed_at": result["process_observed_at"],
                    "reason": reason + "; no idle observation or exit code recorded",
                    "log_tail": _code_terminal_log_tail(watch.get("log")),
                })
    return result


def _refresh_code_terminal_watches(state, state_dir, report):
    """Import diagnostics inside cmd_run's lease spanning load through save."""
    changed = False
    for uid, us in state.get("units", {}).items():
        for attempt, watch in (us.get("code_terminal_watches") or {}).items():
            if not isinstance(watch, dict):
                continue
            agent = watch.get("agent_id")
            if _current_code_terminal_watch(state, uid, attempt, agent) is None:
                if _code_terminal_outcome_path(state_dir, uid, attempt, agent).exists():
                    report.append(f"{uid}: stale terminal-watch outcome ignored "
                                  f"for attempt {attempt}; attempt/agent is no longer current")
                continue
            observed = _observe_code_terminal_watch(state_dir, uid, attempt, watch)
            ignored = observed.pop("outcome_ignored", None)
            if ignored:
                report.append(f"{uid}: {ignored}")
            if observed != watch:
                watch.update(observed)
                changed = True
    return changed


def _code_terminal_status(state, state_dir, uid, attempt):
    us = state.get("units", {}).get(uid) or {}
    watch = (us.get("code_terminal_watches") or {}).get(attempt)
    if not isinstance(watch, dict):
        return None
    if _current_code_terminal_watch(state, uid, attempt, watch.get("agent_id")) is None:
        return dict(watch, outcome_ignored="stale terminal-watch record ignored: agent mismatch",
                    import_pending=False)
    observed = _observe_code_terminal_watch(state_dir, uid, attempt, watch)
    observed["import_pending"] = any(
        observed.get(key) != watch.get(key) for key in CODE_TERMINAL_DIAGNOSTICS)
    return observed


def _start_code_terminal_watchers(plan, state, args, report):
    """Start one best-effort event-driven checker for each live code attempt.

    The watcher waits in Paseo, then invokes a separate coordinator process
    for the ordinary advance path under the same project lock. It has no separate judgment authority: unit.py remains
    the only checker and the resulting head still crosses the normal
    coordinator-controlled result channel.
    """
    units = {u["id"]: u for u in plan.get("units") or []}
    started = False
    for uid, u in sorted(units.items()):
        if u.get("kind") != "code":
            continue
        us = _unit_state(state, uid)
        attempt_dir = us.get("attempt_dir")
        job_id = us.get("job_id")
        if (not attempt_dir or not job_id or us.get("state") not in LIVE_STATES
                or trusted_produced_head(state, uid, attempt_dir)):
            continue
        attempt = Path(attempt_dir).name
        watches = us.setdefault("code_terminal_watches", {})
        if isinstance(watches.get(attempt), dict):
            continue
        log_path = Path(attempt_dir) / CODE_TERMINAL_WATCH_LOG
        # Persist the one watcher intent before starting its process. A crash
        # after spawn must not make the next advance launch an unbounded set
        # of duplicate waiters. This record is diagnostic and deduplication
        # state only; it never supplies a judgment fact.
        watch = {
            "agent_id": str(job_id),
            "host": os.uname().nodename,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "log": str(log_path),
            "status": "starting",
        }
        watches[attempt] = watch
        save_state(args.state_dir, state)
        try:
            log = open(log_path, "ab")
        except OSError as exc:
            watch.update({"status": "start_failed", "reason": str(exc),
                          "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            save_state(args.state_dir, state)
            report.append(f"{uid}: could not open terminal-watch log: {exc}. "
                          "Scheduled advance remains the fallback.")
            continue
        argv = [
            sys.executable, str(Path(__file__).resolve()),
            "watch-code-terminal", str(Path(args.plan).resolve()),
            "--state-dir", str(args.state_dir),
            "--root", str(args.root),
            "--unit", uid,
            "--attempt", attempt,
            "--agent", str(job_id),
        ]
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, env=CE.child_env(),
                start_new_session=True)
        except OSError as exc:
            log.close()
            watch.update({"status": "start_failed", "reason": str(exc),
                          "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            save_state(args.state_dir, state)
            report.append(f"{uid}: could not start terminal watcher: {exc}. "
                          "Scheduled advance remains the fallback.")
            continue
        log.close()
        watch["pid"] = proc.pid
        watch["status"] = "waiting"
        save_state(args.state_dir, state)
        report.append(f"{uid}: watching agent {job_id} for an immediate "
                      "terminal judgment")
        started = True
    return started


# This watcher waits with NO DEADLINE, and that is deliberate.
#
# It previously passed `timeout=30 * 24 * 60 * 60`. That reaches
# `selectors.PollSelector.poll()`, whose timeout is milliseconds in a C int,
# so 2592000000 ms exceeded 2147483647 and raised OverflowError before the
# child was ever waited on. The terminal watch therefore never observed an
# agent: a `code` unit was never judged the moment it went idle, and waited
# for the next scheduled advance instead. It failed invisibly because the
# traceback goes to a per-attempt log nothing reads.
#
# A smaller finite value merely moves the deadline. GPT-Astra, asked to
# adjudicate a 20-day replacement: "'this agent has run suspiciously long'
# supports escalation or an explicit workload-lifetime policy. It does not
# naturally support abandoning observation while the agent continues running.
# That is precisely when observation may be most valuable." No duration
# requirement exists to justify any particular ceiling.
#
# `None` is the honest expression of "watch until the agent is idle". It is
# also the only unbounded option: `poll()` blocks indefinitely on None, while
# every finite value is capped at 24.855 days by that same C int. This is the
# one caller that wants to block, and it runs as its own detached watcher
# process rather than inside the coordinator.
#
# The watcher publishes only its own outcome. Status observes without writing;
# a locked advance imports diagnostics, including killed-watcher observations.


def cmd_watch_code_terminal(args):
    """Publish only this watcher's outcome; a separate coordinator checks it."""
    try:
        rc, out, err = U.run(
            ["paseo", "wait", str(args.agent), "--json"],
            timeout=None)
    except Exception as exc:
        rc, out, err = None, "", f"{type(exc).__name__}: {exc}"
    outcome = {
        "status": "idle_observed" if rc == 0 else "exited_without_idle",
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "wait_exit_code": rc,
        "reason": ("paseo wait observed idle" if rc == 0 else
                   f"paseo wait ended without an idle observation (exit {rc}): "
                   f"{(err or out)[-400:]}"),
        "log_tail": (out + "\n" + err)[-4000:].strip(),
    }
    _write_code_terminal_outcome(args, outcome)
    if rc != 0:
        print(outcome["reason"], flush=True)
        return EXIT_HALTED

    # Never load, lock or save coordinator state in the watcher. The ordinary
    # coordinator owns the whole load -> import -> check -> save transaction,
    # and checks this binding again under its lease. Contention leaves the
    # published observation for status and the next scheduled advance.
    try:
        check_rc, out, err = U.run([
            sys.executable, str(Path(__file__).resolve()),
            "advance-code-terminal", str(args.plan),
            "--state-dir", str(args.state_dir), "--root", str(args.root),
            "--unit", args.unit, "--attempt", args.attempt,
            "--agent", str(args.agent)], timeout=None)
    except Exception as exc:
        check_rc, out, err = None, "", f"{type(exc).__name__}: {exc}"
    if check_rc != EXIT_OK:
        outcome["reason"] += (
            f"; immediate coordinator exited {check_rc}: {(err or out)[-400:]}; "
            "scheduled advance remains the fallback")
        _write_code_terminal_outcome(args, outcome)
        print(outcome["reason"], flush=True)
        return EXIT_HALTED
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    return EXIT_OK


def _prepare_command_paths(args, plan=None, extra_repos=(), need_root=False,
                           read_only=False):
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
    if read_only and raw_state is None:
        # Status may inspect an unmigrated legacy snapshot, but only a writer
        # may copy it to the external default. Keep status free of writes even
        # on the first invocation after an upgrade.
        legacy = CP.project_context() / ".swarm" / "state"
        if not (state / STATE_FILE).exists() and (legacy / STATE_FILE).is_file():
            state = legacy
    elif raw_state is None and root is not None:
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
    explicit = getattr(args, "survey", None)
    if explicit:
        # NAMED, so unreadable is an error rather than a shrug. Degrading a
        # survey the operator pointed at into "unknown" would silently drop
        # every check they asked for.
        survey, serr = read_survey(explicit)
        if serr:
            sys.exit(f"error: no readable survey at {explicit}: {serr}")
        note = explicit
    else:
        survey, note = discover_survey(args.plan)
    try:
        summary = validate_plan(plan, survey)
    except PlanError as e:
        sys.exit(f"error: invalid plan: {e}")
    print(f"plan is valid: {summary['units']} unit(s), "
          f"{summary['with_deps']} with dependencies")
    # WHICH CLUSTER FACTS WERE CONSULTED, in both directions. A pass here
    # means "the survey did not contradict this plan", and with no survey it
    # means nothing at all, so neither may be printed as approval.
    if survey is None:
        print(f"  NOT CHECKED: {note}. Nothing here examined this cluster's "
              f"memory policy or per-partition account rules.")
    else:
        print(f"  survey: {note} -- memory policy and per-partition account "
              f"rules were checked against it. {QOS_CAVEAT}")
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
        if (getattr(args, "terminal_watch", False)
                and _current_code_terminal_watch(
                    state, args.unit, args.attempt, args.agent) is None):
            print("obsolete terminal watcher: attempt/agent no longer current; no check run")
            return EXIT_OK
        watch_report = []
        if (not args.dry_run
                and _refresh_code_terminal_watches(state, args.state_dir, watch_report)):
            save_state(args.state_dir, state)
        report, dispatched, halted = advance(
            plan, state, args.state_dir, args.root, args.dry_run,
            args.max_new_dispatches,
            accept_plan_change=getattr(args, "accept_plan_change", False))
        report[:0] = watch_report
        if (not args.dry_run
                and _start_code_terminal_watchers(plan, state, args, report)):
            save_state(args.state_dir, state)
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
SCHEMA_PLAN_FIELDS = [
    ("canary", "all", "optional",
     "string id of one root unit (no needs). Every other unit implicitly "
     "needs it: wait for DONE; FAILED, FAILED_EVIDENCE or HELD holds the "
     "others through ordinary upstream-failure handling. Omit for normal "
     "fan-out. This does not replace runtime verification."),
]

SCHEMA_FIELDS = [
    ("id", "all", "required", "unique; names the attempt directory and the "
     "env var SWARM_DEP_<ID>"),
    ("kind", "all", "required", "slurm | pipeline | code; fixes what closes "
     "the unit and cannot be overridden per plan"),
    ("tracker", "all", "optional",
     "non-empty issue string, e.g. ARC-698. Labels tracker intents only; "
     "never admission, closure or DONE authority. Omission preserves the "
     "plan digest and intent keys"),
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
    ("seed", "code", "optional",
     "JSON object: ref (refs/heads/...), base and head (full 40/64-hex commit "
     "ids), optional evidence path. Dispatch requires base <= head <= the "
     "fetched origin push-destination ref. Raw provenance is recorded in "
     "coordinator launch intent; the worker fetches and cherry-picks -x, "
     "skipping empty commits. No whole-file checkout. The coordinator does "
     "not replay commits; judging, closure and scope-check are unchanged. "
     "Relative evidence paths use the source repository directory"),
    ("mode", "code", "required",
     "no default on purpose. Absent or empty means default permissions, so "
     "the agent stalls at its first write"),
    ("provider", "code", "optional",
     "default codex/gpt-6-astra"),
    ("model", "code", "optional", "overrides the provider's default"),
    ("thinking", "code", "optional",
     "default high. JSON null or \"\" suppresses the flag; the STRING "
     "\"null\" is refused"),
    ("env", "code", "optional", "list of KEY=VALUE passed to the agent"),
    ("continuation", "code", "optional",
     '{"max": N, "prompt": "..."}; bounded nudges when it settles without '
     "producing. Exhaustion FAILS the unit"),
    ("requires_verification", "code", "optional",
     "claims an authorized verifier must establish before closing. The "
     "reserved integration-tests claim runs in a disposable candidate merge "
     "and also binds the pre-merge target commit"),
    ("runtime", "slurm, pipeline", "required",
     'inline or a "runtimes" id, or the literal "none". Declares resolution, '
     "entrypoint, probe and verified_by"),
    ("isolation", "slurm, pipeline", "optional",
     '{"kind": "container", "backend": "apptainer", "image": "...", '
     '"writable": ["$SWARM_UNIT_DIR"], "read_only": [...]}. Restricts the '
     "dispatched workload's writable HOST binds to the attempt root and "
     "binds every declared input read-only. It does not create another Unix "
     "identity or isolate other same-UID host processes, PIDs, or networking"),
    ("sbatch", "slurm", "optional",
     "a LIST of scheduler flags. A string is iterated character by "
     "character. --mem is required when the survey reports "
     "mem_flag_required, and --account is checked against the surveyed "
     "allow/deny lists of the partition --partition names"),
    ("write_scopes", "all", "optional",
     "must not overlap between concurrent units. Names FILES; does NOT "
     "isolate a code unit's repository"),
    ("scope", "all", "optional",
     "JSON list of repo-relative, case-sensitive fnmatch globs; no absolute "
     "paths or '..' segments. Wildcards, including **, span any depth. "
     "An empty list allows no changed paths. scope-check compares the code "
     "attempt's recorded base and judged head locally; it is an advisory "
     "merge precondition and never blocks advance or changes closure"),
    ("workspace_policy", "slurm, pipeline", "optional",
     '{"requires_clean_git": true, "path": "/checkout"}; opt-in launch '
     "preflight for a non-code unit"),
    ("promote_to", "all", "optional",
     "where verified outputs are published. Needs a named approver. "
     "REQUIRED of a unit whose outputs include findings.json, which the "
     "report reads from the project directory rather than the write root"),
    ("max_attempts", "all", "optional",
     "default 1. Above 1 requires a retry contract with max_lost"),
    ("deadline_s", "all", "optional",
     "positive finite seconds measured from allocated_at, the coordinator-"
     "written allocation timestamp. A breach becomes NEEDS_HUMAN with "
     "reason deadline_exceeded; it does not cancel work, release an output "
     "claim, or mint a retry"),
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
    "A runtime canary must match runtime identity, partition AND account, "
    "and be a DAG ancestor of it. A plan spanning two partitions needs one "
    "canary per partition.",
    "A runtime canary must run the runtime's declared probe command verbatim; a "
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
    "A slurm unit must state its memory (--mem, --mem-per-cpu or "
    "--mem-per-gpu) when the survey reports mem_flag_required. With no "
    "survey to read, that fact is unknown and nothing is refused on it.",
    "A slurm unit's --account must not be denied by, nor missing from a set "
    "allow_accounts of, the partition it names, as the survey recorded them. "
    "unknown on a field decides nothing; the other field still decides.",
    "A unit declaring findings.json must declare promote_to. Outputs stay in "
    "the attempt write root and the report reads findings.json from the "
    "project directory, so an unpromoted one is read by nobody.",
]


def cmd_schema(args):
    """Print plan/unit fields, when required, and what they couple to."""
    if args.json:
        print(json.dumps(
            {"fields": [{"field": f, "kinds": k, "requirement": r,
                         "notes": n} for f, k, r, n in SCHEMA_FIELDS],
             "plan_fields": [{"field": f, "kinds": k, "requirement": r,
                              "notes": n} for f, k, r, n in SCHEMA_PLAN_FIELDS],
             "couplings": SCHEMA_COUPLINGS}, indent=2))
        return EXIT_OK
    print("  PLAN FIELDS\n")
    for field, kinds, req, note in SCHEMA_PLAN_FIELDS:
        print(f"  {field}  {req}  {kinds}")
        for line in _wrap(note, 66):
            print(f"    {line}")
        print()
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


def _scope_git(repo, *args):
    """Read local Git objects, preserving filename bytes and denying fetches."""
    env_program = shutil.which("env", path=os.defpath)
    if not env_program:
        raise PlanError("system env executable is unavailable")
    argv = [env_program]
    for key in os.environ:
        if key.startswith("GIT_"):
            argv.extend(("-u", key))
    argv.extend(("GIT_CONFIG_NOSYSTEM=1", f"GIT_CONFIG_GLOBAL={os.devnull}",
                 "GIT_NO_LAZY_FETCH=1", "GIT_ALLOW_PROTOCOL=",
                 "GIT_TERMINAL_PROMPT=0", "git", "--no-replace-objects",
                 "-C", repo))
    try:
        result = subprocess.run(
            argv + list(args),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=CE.child_env(), timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlanError(f"local Git read unavailable: {exc}")
    if result.returncode:
        raise PlanError("local Git read failed: " + W.render_git_diagnostic(
            result.returncode, result.stderr.decode("utf-8", "replace")))
    return result.stdout.decode("utf-8", "surrogateescape")


def _scope_changed_paths(repo, base, head):
    """Read both rename endpoints; no quoting, whitespace folding or filters."""
    for commit in (base, head):
        if _scope_git(repo, "cat-file", "-t", commit).strip() != "commit":
            raise PlanError("scope-check basis is not a commit object")
    raw = _scope_git(repo, "diff", "--name-status", "-z", "-M",
                     "--no-ext-diff", "--no-textconv", "--no-relative",
                     "--ignore-submodules=none", "--no-color",
                     base, head, "--")
    if not raw:
        return [], []
    if not raw.endswith("\0"):
        raise PlanError("incomplete local Git name-status output")
    fields = iter(raw[:-1].split("\0"))
    changed, removed = [], []
    try:
        for status in fields:
            if not re.fullmatch(r"[ADMTUXB]|[RC][0-9]+|M[0-9]+", status):
                raise PlanError(f"unknown local Git change status {status!r}")
            path = next(fields)
            changed.append(path)
            if status == "D" or status.startswith("R"):
                removed.append(path)
            if status.startswith(("R", "C")):
                changed.append(next(fields))
    except StopIteration:
        raise PlanError("incomplete local Git name-status record")
    return changed, removed


def cmd_scope_check(args):
    """Advisory merge precondition; reads state, never judges or mutates it."""
    report = {"unit": args.unit, "status": "unchecked", "attempt": None,
              "base": None, "head": None, "scope": None,
              "repository": None, "target": None, "state_epoch": None,
              "out_of_scope": [], "deletions_out_of_scope": []}
    code = EXIT_SCOPE_UNCHECKED
    try:
        plan, error = U.read_json(args.plan)
        if error or not isinstance(plan, dict):
            raise PlanError(f"no readable plan: {error or 'not an object'}")
        units = plan.get("units")
        if not isinstance(units, list):
            raise PlanError("plan units must be a JSON list")
        matches = [u for u in units if isinstance(u, dict)
                   and u.get("id") == args.unit]
        if len(matches) != 1:
            raise PlanError("scope-check needs one matching plan unit")
        u = matches[0]
        patterns = declared_scope(u)
        report["scope"] = patterns
        if u.get("kind") != "code":
            raise PlanError("unit has no code-attempt judgment")
        # Explicit state-dir: validate containment without default migration.
        state_dir, _root, _trees = CP.resolve_paths(
            args.state_dir, plan=plan, cwd=os.getcwd())
        epoch, error = _read_state_epoch(state_dir)
        if error:
            raise PlanError(error)
        report["state_epoch"] = epoch
        state, error = U.read_json(state_dir / STATE_FILE)
        if error or not isinstance(state, dict):
            raise PlanError(f"no readable coordinator state: "
                            f"{error or 'not an object'}")
        us = (state.get("units") or {}).get(args.unit) or {}
        attempt_dir = us.get("attempt_dir")
        if not isinstance(attempt_dir, str) or not attempt_dir:
            raise PlanError("no current attempt in coordinator state")
        attempt = Path(attempt_dir).name
        report["attempt"] = attempt
        intent = (us.get("attempt_launch_intents") or {}).get(attempt)
        problem = _code_launch_intent_problem(intent, u, attempt)
        if problem:
            raise PlanError(problem)
        base, repo = intent["base_commit"], intent["repo"]
        head = trusted_produced_head(state, args.unit, attempt_dir)
        report.update({"base": base, "head": head,
                       "repository": intent.get("repository_remote"),
                       "target": intent["target_branch"]})
        if patterns is None:
            raise PlanError("unit declares no scope")
        if (not isinstance(head, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head)):
            raise PlanError("no valid judged head for this attempt in "
                            "coordinator state")
        if not isinstance(repo, str) or not repo:
            raise PlanError("no repository in coordinator launch intent")
        CP.resolve_paths(args.state_dir, plan=plan, cwd=os.getcwd(),
                         extra_repos=[repo])
        changed, removed = _scope_changed_paths(repo, base, head)
        outside = lambda path: not any(fnmatch.fnmatchcase(path, pattern)
                                       for pattern in patterns)
        report["out_of_scope"] = sorted(set(filter(outside, changed)))
        report["deletions_out_of_scope"] = sorted(set(filter(outside, removed)))
        report["status"] = ("out_of_scope" if report["out_of_scope"]
                            else "in_scope")
        code = EXIT_SCOPE_OUTSIDE if report["out_of_scope"] else EXIT_OK
    except (PlanError, CP.PathPolicyError, TypeError, ValueError,
            AttributeError) as exc:
        report["reason"] = str(exc)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"scope-check {args.unit!r}: {report['status']}")
        if report.get("reason"):
            print(f"  unchecked: {report['reason']}")
        for path in report["out_of_scope"]:
            print("  out of scope: " + json.dumps(path))
        for path in report["deletions_out_of_scope"]:
            print("  deletion or rename-away out of scope: " + json.dumps(path))
    return code


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

    corpus_evidence, refusal = V.corpus_evidence(
        U.run, repo, base, produced, entry)
    if refusal:
        sys.stderr.write(f"error: {refusal}\n")
        return EXIT_USAGE
    refusal = V.corpus_change_refusal(entry, corpus_evidence, args.claim)
    if refusal:
        sys.stderr.write(f"error: {refusal}\n")
        return EXIT_FAILED_UNIT

    merge_evidence = {}
    if args.claim == V.INTEGRATION_CLAIM:
        target_commit = getattr(args, "target_commit", None)
        if not target_commit:
            sys.stderr.write(
                "error: integration-tests requires --target-commit. Supply "
                "that commit's Git object locally; this command never "
                "contacts a forge.\n")
            return EXIT_USAGE
        outcome, merge_evidence, rerr = V.run_in_candidate_merge(
            U.run, repo, produced, target_commit, args.path, digest,
            args=args.arg, timeout=args.timeout)
    else:
        if getattr(args, "target_commit", None):
            sys.stderr.write(
                "error: --target-commit applies only to the "
                "integration-tests claim. Ordinary verification remains "
                "bound only to the produced head.\n")
            return EXIT_USAGE
        outcome, rerr = V.run_in_checkout(
            U.run, repo, produced, args.path, digest, args=args.arg,
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
    rec.update(corpus_evidence)
    rec.update(merge_evidence)
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
    target_commit = getattr(args, "target_commit", None)
    if target_commit:
        rec["target_commit"] = target_commit
    integration_status = getattr(args, "integration_status", None)
    if integration_status:
        # Audit label supplied by the connected merge operator, never new
        # verification authority. Claim admission still reads verify receipts.
        rec["integration_status"] = integration_status
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
        selected = next((i for i in intents
                         if i.get("key") == args.record_receipt), None)
        if selected is None:
            sys.stderr.write(
                f"error: no intent with key {args.record_receipt!r} in this "
                f"outbox. A receipt for an unknown key would acknowledge "
                f"something that was never intended.\n")
            return EXIT_USAGE
        if not args.ref:
            sys.stderr.write(
                "error: --ref is required. The receipt records the tracker's "
                "own reference for the operation the drainer confirmed by "
                "read-back; without it there is nothing to check later.\n")
            return EXIT_USAGE
        source = getattr(args, "source", None)
        matched = getattr(args, "matched", False)
        if source or matched:
            sys.stderr.write(
                "error: this compatibility command records only the weaker "
                "legacy attestation. Reconcile a complete observation file "
                "with drain_contract.py --state-dir to record the stronger "
                "receiver-confirmed grade.\n")
            return EXIT_USAGE
        rec = record_receipt(args.state_dir, args.record_receipt, args.ref,
                             op=args.op)
        print(f"  recorded: {rec['key']} -> {rec['ref']}")
        if _receipt_grade(rec) == ATTESTED_UNSPECIFIED:
            print("  grade: attested (compatible legacy form; "
                  "no read-back basis was supplied)")
        return EXIT_OK

    status, problems = acknowledgment_status(args.state_dir)

    # FAIL CLOSED. Corruption is not the same as an interrupted tail: a
    # truncated last line is a local write that did not finish, and repeating
    # reconciliation fixes it. A bad line in the middle means the journal
    # cannot be read in
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
            {"note": "ack_status 'attested' is an unspecified legacy claim; "
                     "'attested_confirmed' is bound to receiver read-back or "
                     "receiver deduplication. Neither is independently "
                     "verified tracker state. 'unacknowledged' means no "
                     "receipt either way, NOT that nothing was filed.",
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
        label = {ATTESTED_UNSPECIFIED: "attested",
                 ATTESTED_CONFIRMED: "confirmed",
                 CONFLICT: "CONFLICT",
                 UNACKNOWLEDGED: "unack"}[i["ack_status"]]
        # same string in both modes; see the note on ACKNOWLEDGED
        print(f"  [{label:8}] {i['verb']:6} {i['unit']:12} "
              f"{i['unit_state']:16} {ev}")
        print(f"      {i['why']}  key={i['key']}")
        if "tracker" in i:
            print(f"      tracker: {i['tracker']!r}")
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
          "has\n  no receipt either way. After an ambiguous drain, do not "
          "replay: resolve\n  it with receiver-side deduplication or "
          "read-back.")
    print("  ATTESTED is a compatible legacy claim with no recorded basis; "
          "CONFIRMED\n  is bound to receiver read-back. Neither is "
          "independent verification.")
    return EXIT_CONFLICT if conflicts else EXIT_OK


def _elapsed_seconds(observed_at, started_at):
    """A non-negative whole-second age, or None when history is unknown."""
    if started_at is None or isinstance(started_at, bool):
        return None
    try:
        started = float(started_at)
    except (TypeError, ValueError, OverflowError):
        return None
    if started != started or started in (float("inf"), float("-inf")):
        return None
    return max(0, int(float(observed_at) - started))


def _status_rows(plan, state, state_dir, observed_at=None):
    """Read durable state plus explicitly labelled watcher observations.

    Reads no scheduler and starts no workers; local ps probes are diagnostic
    only. Rendering never imports observations into coordinator state.
    """
    observed_at = time.time() if observed_at is None else float(observed_at)
    units = {u["id"]: u for u in _units_with_canary(plan)}
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
        deadline_host_local = None
        deadline_problem = None
        deadline_evaluation = None
        if u.get("deadline_s") is not None and us.get("attempt_dir"):
            deadline_host_local, deadline_problem = \
                _deadline_host_evaluation(state, uid, us)
            deadline_evaluation = (
                "evaluable_here" if deadline_host_local is True
                else "not_evaluable_here")
        held_by = []
        if st == "HELD":
            held_by = [d for d in (u.get("needs") or [])
                       if (state.get("units", {}).get(d) or {}).get("state")
                       in ("FAILED", "HELD", "FAILED_EVIDENCE")]
        rows.append({
            "id": uid, "kind": u.get("kind", "?"), "state": st,
            "terminal_watch": (_code_terminal_status(
                state, state_dir, uid, Path(us["attempt_dir"]).name)
                if u.get("kind") == "code" and us.get("attempt_dir") else None),
            "job_id": us.get("job_id"), "attempt_dir": us.get("attempt_dir"),
            "allocated_at": us.get("allocated_at"),
            "state_changed_at": us.get("state_changed_at"),
            "state_changed_at_basis": us.get("state_changed_at_basis"),
            "age_s": _elapsed_seconds(observed_at, us.get("allocated_at")),
            "state_age_s": _elapsed_seconds(
                observed_at, us.get("state_changed_at")),
            "reason": us.get("reason"),
            "deadline_evaluation": deadline_evaluation,
            "deadline_host_local": deadline_host_local,
            "deadline_problem": deadline_problem,
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
        if plan.get("canary") in rows[-1]["waiting_on"]:
            rows[-1]["waiting_reason"] = f"waiting on canary {plan['canary']}"
    rows.sort(key=lambda r: r["id"])
    return rows


def status_report(plan, state, state_dir, observed_at=None):
    observed_at = time.time() if observed_at is None else float(observed_at)
    rows = _status_rows(plan, state, state_dir, observed_at=observed_at)
    observation = time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                time.localtime(observed_at))
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
        "observed_at": observation,
        "generated_at": observation,
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
    _prepare_command_paths(args, plan=plan, read_only=True)
    state = load_state(args.state_dir)
    rep = status_report(plan, state, args.state_dir)

    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        rows = rep["units"]
        w = max([len(r["id"]) for r in rows] + [4])
        print(f"  observed at {rep['observed_at']}")
        print(f"  {'unit'.ljust(w)}  {'kind':9} {'state':13} {'job':14} "
              f"{'gpuh':>5} att {'age':>8} {'in-state':>8}")
        for r in rows:
            job = str(r["job_id"] or "-")
            age = "-" if r["age_s"] is None else f"{r['age_s']}s"
            state_age = ("-" if r["state_age_s"] is None
                         else f"{r['state_age_s']}s")
            if (r["state_age_s"] is not None
                    and r["state_changed_at_basis"] == "first_observed"):
                state_age = ">=" + state_age
            print(f"  {r['id'].ljust(w)}  {r['kind']:9} {r['state']:13} "
                  f"{job[:14]:14} {r['gpu_hours']:>5g} {r['attempts']} "
                  f"{age:>8} {state_age:>8}")
            watch = r["terminal_watch"]
            if watch:
                process = (watch.get("process_state") if watch.get("status")
                           in ("starting", "waiting") else None)
                suffix = f" (process {process})" if process else ""
                if watch.get("import_pending"):
                    suffix += " (not yet imported)"
                print(f"  {'':{w}}    terminal watcher: "
                      f"{watch.get('status', 'unknown')}{suffix}; observed "
                      f"{watch.get('observed_at') or watch.get('process_observed_at') or watch.get('started_at') or 'unknown'}")
                if watch.get("outcome_ignored"):
                    print(f"  {'':{w}}      {watch['outcome_ignored']}")
                if watch.get("reason") or watch.get("process_reason"):
                    print(f"  {'':{w}}      "
                          f"{watch.get('reason') or watch.get('process_reason')}")
                if watch.get("log_tail"):
                    print(f"  {'':{w}}      log tail: {watch['log_tail']!r}")
                if watch.get("status") != "checked":
                    print(f"  {'':{w}}      Scheduled advance remains the fallback.")
            if r["reason"]:
                print(f"  {'':{w}}    reason: {r['reason']}")
            if r["deadline_evaluation"] == "not_evaluable_here":
                print(f"  {'':{w}}    deadline: NOT EVALUABLE HERE; "
                      f"no escalation -- {r['deadline_problem']}")
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
                waiting = [f"canary {d}" if d == plan.get("canary") else d
                           for d in r["waiting_on"]]
                print(f"  {'':{w}}    waiting on "
                      f"{', '.join(waiting)}")
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
    v.add_argument("--survey", default=None,
                   help="the survey.py JSON to check this plan's memory "
                        "requests and account/partition pairs against. "
                        "Without it, .swarm/survey.json beside the plan and "
                        "then in the working directory, and if neither is "
                        "there those cluster facts are UNKNOWN and nothing "
                        "is refused on them.")
    v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("run", help="dispatch what is ready, then exit")
    common(r)
    r.set_defaults(fn=cmd_run)

    a = sub.add_parser("advance", help="idempotent; for a schedule or cron")
    common(a)
    a.set_defaults(fn=cmd_advance)

    # Internal detached helper. It carries no independent authority and is
    # intentionally absent from the public CLI reference: `run`/`advance`
    # create it only after persisting the exact attempt and agent binding.
    w = sub.add_parser("watch-code-terminal", help=argparse.SUPPRESS)
    w.add_argument("plan")
    w.add_argument("--state-dir", required=True)
    w.add_argument("--root", required=True)
    w.add_argument("--unit", required=True)
    w.add_argument("--attempt", required=True)
    w.add_argument("--agent", required=True)
    w.set_defaults(fn=cmd_watch_code_terminal)

    # The watcher cannot write coordinator state. This separate invocation
    # uses the normal coordinator lock and verifies the triggering identity.
    tw = sub.add_parser("advance-code-terminal", help=argparse.SUPPRESS)
    tw.add_argument("plan")
    tw.add_argument("--state-dir", required=True)
    tw.add_argument("--root", required=True)
    tw.add_argument("--unit", required=True)
    tw.add_argument("--attempt", required=True)
    tw.add_argument("--agent", required=True)
    tw.set_defaults(fn=cmd_advance, terminal_watch=True, dry_run=False,
                    max_new_dispatches=0)

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

    sc = sub.add_parser("scope-check", help="advisory local merge scope check")
    sc.add_argument("plan")
    sc.add_argument("--state-dir", required=True)
    sc.add_argument("--unit", required=True)
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(fn=cmd_scope_check)

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
    v.add_argument(
        "--target-commit", default=None,
        help="required only for claim integration-tests: the exact target "
             "commit to merge the produced head into. Its object must "
             "already exist locally; verify never fetches it.")
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
    m.add_argument(
        "--target-commit", default=None,
        help="the target branch commit immediately before this merge. "
             "Required for an integration-tests receipt to remain valid.")
    m.add_argument("--merged-as", required=True,
                   help="the resulting commit on the target")
    m.add_argument("--method", required=True, choices=MERGE_METHODS)
    m.add_argument("--integration-status",
                   choices=("candidate-verified", "integration-unverified"),
                   help="connected operator's audit label; not verification authority")
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
                   help="record the compatible legacy attestation for this "
                        "intent. Receiver-confirmed observations go through "
                        "drain_contract.py with their complete bindings; a "
                        "false acknowledgment is worse than a missing one.")
    o.add_argument("--ref", help="the tracker's own reference for the "
                                 "operation that succeeded, e.g. ARC-171")
    o.add_argument("--op", help="optional: which operation was performed")
    o.add_argument("--source", choices=sorted(RECEIPT_CONFIRMING_SOURCES),
                   help="reserved for compatibility; use drain_contract.py "
                        "with a complete observation file")
    o.add_argument("--matched", action="store_true",
                   help="reserved for compatibility; use drain_contract.py "
                        "with a complete observation file")
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
