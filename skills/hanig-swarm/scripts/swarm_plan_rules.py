"""Plan declarations, scheduler survey rules and promotion destinations.

Python 3.8+, standard library only; dependencies never point to the facade.
"""

import os
from pathlib import Path
import re
import shutil

import unit as U
from swarm_types import PlanError


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

# survey.py's vocabulary, imported as words rather than as a habit.
LIMIT_SET, LIMIT_OPEN, LIMIT_UNKNOWN = "set", "unrestricted", "unknown"

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
