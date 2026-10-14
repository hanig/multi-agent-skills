"""Attempt allocation, submission and binding for the offline coordinator.

Python 3.8+, standard library only. Dependencies point to supporting modules,
never back to the swarm facade.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys

import unit as U
import swarm_evidence as SE
import swarm_plan_rules as SPR
import swarm_preflight as SP

_HERE = Path(__file__).resolve().parent


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
    problem = SPR.isolation_problem(u)
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
                    or SP._inside_dir(root_path, source_path)
                    or SP._inside_dir(source_path, root_path)):
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
    marker = str(Path(root) / SE.ISOLATION_MARKER_NAME)
    reusable = (existing_facts if isinstance(existing_facts, dict)
                and existing_facts.get("profile_sha256") == hashlib.sha256(
                    canonical_profile.encode()).hexdigest()
                and existing_facts.get("writable_host_binds") == [root]
                else None)
    marker_token = ((reusable or {}).get("application_token_sha256")
                    or os.urandom(32).hex())

    command_argv, command_error = SPR._isolation_command_argv(u.get("command"))
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


def _slurm_exit_trap(unit_dir):
    """Record explicit exits, normal returns and default fatal-signal exits.

    Payload handlers can replace defaults; no payload supervisor is introduced.
    Clear old records when initialized, including requeues. Shell replacement,
    a replaced EXIT trap, SIGKILL or node loss can leave absent evidence.
    The recorder uses isolated positional data, not payload-mutable globals;
    failure to publish a record must not change the payload's original status.

    GNU Bash's trap-output write status permits a child-free CHLD probe:
    default non-POSIX disposition prints nothing; configured/ignored traps
    fail on closed stdout. POSIX default output conservatively opts out too.
    Opt-outs invalidate existing JSON with builtins, without emitting CHLD.

    The CHLD probe needs GNU Bash 4 or later: bash 3.2 (macOS /bin/bash)
    ignores the closed stdout and returns 0 even when a CHLD trap is set, so
    it cannot tell a payload handler is present. Before 4 the wrapper takes
    the conservative opt-out (no record, so the unit judges INCOMPLETE, never
    DONE); BASH_VERSINFO is a builtin variable, so the check forks nothing.

    The JSON braces are written as printf escapes (\\173, \\175), never as
    literal `{...,...}`: bash 3.2 (macOS /bin/bash) brace-expands that text
    inside the quoted command substitution that builds the EXIT trap, which
    splits the trap into several words and records one field per line.
    """
    record = shlex.quote(str(Path(unit_dir) / U.SLURM_EXIT_RECORD))
    temporary = shlex.quote(str(Path(unit_dir) / (U.SLURM_EXIT_RECORD + ".tmp."))) + '"$$"'
    clear = f"(builtin command -p rm -f -- {record})"
    signal_exit = shlex.quote(clear + "; builtin exit ")
    generator = f'''set -euo pipefail
    builtin set -- $(builtin kill -l)
    while (($#)); do
        case "$1" in
            *')'|KILL|SIGKILL|QUIT|SIGQUIT|CHLD|SIGCHLD|CONT|SIGCONT|STOP|SIGSTOP|TSTP|SIGTSTP|TTIN|SIGTTIN|TTOU|SIGTTOU|URG|SIGURG|WINCH|SIGWINCH) ;;
            *) builtin printf 'case "$(builtin trap -p %q)" in ""|"trap -- - "*) builtin trap %q %q ;; esac\\n' "$1" {signal_exit}"$((128 + $(builtin kill -l "$1")))" "$1" ;;
        esac
        builtin shift
    done'''
    defaults = f'''builtin eval "$(/bin/bash -p -c {shlex.quote(generator)} || builtin printf '\\n%s\\n' 'builtin exit 1')"'''
    prefix = '(\n    builtin set -- "$?"'
    identity = '''builtin printf ' %q' "${SLURM_JOB_ID:-}" "${SLURM_ARRAY_JOB_ID:-}" "${SLURM_ARRAY_TASK_ID:-}" "${SLURM_RESTART_COUNT:-0}"'''
    recorder = f'''
    [[ $2 =~ ^[0-9]+$ ]] || exit 1
    [[ $3:$4 =~ ^[0-9]+:[0-9]+$ ]] || builtin set -- "$1" "$2" "" "" "$5"
    [[ $5 =~ ^[0-9]+$ ]] || builtin set -- "$1" "$2" "$3" "$4" ""
    builtin printf '\\173"job_id":"%s","exit_status":%s,"end_time":"%s","array_job_id":"%s","array_task_id":"%s","restart_count":"%s"\\175\\n' \\
        "$2" "$1" "$(command date -u +%Y-%m-%dT%H:%M:%SZ)" "$3" "$4" "$5" \\
        > {temporary}
) && command mv -f -- {temporary} {record} || {clear} || :'''
    return f'''if (( ${{BASH_VERSINFO[0]:-0}} >= 4 )) && [[ ! -o functrace && ! -o xtrace ]] && ! builtin shopt -q extdebug && builtin trap -p CHLD >&- 2>/dev/null; then
{clear}
{defaults}
case "$(builtin trap -p EXIT)" in
    ""|"trap -- - "*)
        builtin trap "$(builtin printf '%s' {shlex.quote(prefix)}; {identity}; builtin printf '%s' {shlex.quote(recorder)})" EXIT ;;
esac
else
    [[ ! -L {record} && ( ! -e {record} || -f {record} ) ]] || builtin exit 1
    if [[ -s {record} ]]; then builtin printf '' >| {record}; fi
fi'''


def _bind(unit_dir, job_id):
    rc, out, err = U.run([sys.executable, str(_HERE / "unit.py"), "bind",
                          str(unit_dir), "--job-id", str(job_id)], timeout=120)
    return None if rc == 0 else f"bind failed: {(err or out).strip()[:200]}"
