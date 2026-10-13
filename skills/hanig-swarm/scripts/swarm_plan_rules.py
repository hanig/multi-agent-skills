"""Plan declarations, scheduler survey rules and promotion destinations.

Python 3.8+, standard library only; dependencies never point to the facade.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex

import unit as U
import converge as CV
import swarm_evidence as SE
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
                SE.ISOLATION_MARKER_NAME):
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
