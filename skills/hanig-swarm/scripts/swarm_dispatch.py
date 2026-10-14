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
import subprocess
import sys
import time

import child_environment as CE
import codex_home as CH
import coordinator_paths as CP
import unit as U
import worktree as W
import swarm_code_launch as SCL
import swarm_code_protocol as SCP
import swarm_evidence as SE
import swarm_plan_rules as SPR
import swarm_preflight as SP
import swarm_routing as SR
import swarm_state as SS
import swarm_workspaces as SW
from swarm_plan_rules import _NEEDS_RUNTIME
from swarm_types import PlanError

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


def _submit(u, unit_dir, dry_run, state=None, state_dir=None,
            dispatch_source=None):
    """Submit, and return (job_id, error). Dispatch differs per kind; judging
    does not.

    --dry-run records what WOULD be submitted, so the DAG logic is testable
    without a scheduler. A coordinator that can only be tested on a live
    cluster does not get tested."""
    kind = u["kind"]
    record_enabled, problem = U.slurm_record_policy(
        unit_dir, u, SE.raw_artifact_basis(state or {}, u["id"], unit_dir))
    if problem:
        return None, problem
    anchored_base = None
    if kind == "code":
        # Capture only facts the coordinator can know before the agent exists.
        # The immutable base and generated path are durable before the
        # coordinator creates the worktree or asks Paseo to run in it.
        attempt = Path(unit_dir).name
        existing_intent = ((((state or {}).get("units") or {}).get(u["id"]) or {})
                           .get("attempt_launch_intents") or {}).get(attempt)
        if existing_intent:
            anchor_err = SCL._code_launch_intent_problem(
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
                    seed = SPR.declared_seed(u)
                except PlanError as exc:
                    anchor_err = str(exc)
                else:
                    anchor_err = (SP._seed_reachability_problem(
                        seed, existing_intent["repo"],
                        existing_intent.get("repository_remote_raw"),
                        existing_intent.get("repository_remote")))
            # This path may create an agent after an interrupted submission.
            # Recheck admission, but never migrate an existing intent or ask
            # this question while judging an already-launched attempt.
            if not anchor_err and dispatch_source is None:
                target, anchor_err = SP._resolve_dispatch_target(u)
                if not anchor_err:
                    dispatch_source, anchor_err = SP._dispatch_source_identity(
                        u, target)
            if (not anchor_err and existing_intent["base_commit"] !=
                    dispatch_source["target_commit"]):
                anchor_err = SP._dispatch_base_refusal(
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
                anchor_err = SP._repeat_stash_preflight(u)
            anchored_base = (None if anchor_err else {
                "base": existing_intent["base_commit"],
                "intent": existing_intent,
            })
        else:
            anchor_err, anchored_base = SCL._capture_code_launch(
                unit_dir, u, dispatch_source=dispatch_source)
        if anchor_err:
            return None, anchor_err
        # One warning point for cached/uncached sources and restored intents.
        # Keep it outside their branches so every code snapshot is reported.
        SP._warn_installed_skill_drift(
            anchored_base["intent"].get("installed_skills"),
            anchored_base["base"], u.get("id"))
    elif SP._requires_clean_workspace(u):
        anchor_err, anchored_base = SCL._write_launch_record(unit_dir, u)
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
        SS.save_state(state_dir, state)
    if SP._requires_clean_workspace(u) and kind != "code" and not dry_run:
        facts = SS.trusted_launch_facts(state or {}, u["id"], unit_dir)
        if not facts:
            return None, (
                f"unit {u['id']!r}: coordinator state has no complete launch "
                f"snapshot for attempt {Path(unit_dir).name!r}. Re-dispatch "
                f"into a fresh attempt; do not reconstruct trusted facts "
                f"from the agent-writable launch record.")
        # The seal now audits the human-readable copy only. A mismatch is
        # retained for inspection and cannot alter the facts used below.
        stored = SE.trusted_record_seal(state or {}, u["id"], unit_dir)
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
            SS.save_state(state_dir, state)
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
        SS.save_state(state_dir, state)
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
                "set -euo pipefail",
                f"cd {shlex.quote(str(unit_dir))}",
                f"export SWARM_UNIT_ID={shlex.quote(u['id'])}",
                f"export SWARM_UNIT_DIR={shlex.quote(str(unit_dir))}"]
        # Quoted: a path is data. A directory with a space in its name must
        # not become two words in a shell script we generate.
        body += [f"export {n}={shlex.quote(v)}" for n, v in deps]
        if record_enabled:
            body.append(_slurm_exit_trap(unit_dir))
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
        codex = (u.get("provider") or SR.DEFAULT_AGENT_PROVIDER).split("/", 1)[0] == "codex"
        if codex:
            try:
                CH.check_effective_uid()
            except CH.HomeError as exc:
                return None, str(exc)
            if any(str(kv).partition("=")[0] == "CODEX_HOME"
                   for kv in (u.get("env") or [])):
                return None, "CODEX_HOME is coordinator-owned; remove it from the unit env"
            try:
                auth = CH.validate_source()
            except CH.HomeError as exc:
                return None, str(exc)
        intent = (((state or {}).get("units", {}).get(u["id"], {})
                   .get("attempt_launch_intents") or {}).get(attempt))
        intent_err = SCL._code_launch_intent_problem(intent, u, attempt)
        if intent_err:
            return None, intent_err
        source_repo = intent["repo"]
        reuse_workspace = None
        # A prior `paseo run` may have created both branch and worktree before
        # this controller crashed. Re-running the creation command can only
        # collide with those resources. Recover the named agent first; if no
        # agent owns the pre-existing branch, fail closed and name the manual
        # cleanup rather than retrying the same impossible creation forever.
        rc, _out, _err = SP._git(
            source_repo, "show-ref", "--verify", "--quiet",
            f"refs/heads/{intent['branch']}")
        if rc == 0:
            existing_agent, note = SW.reconcile_orphan(
                unit_dir, kind="code")
            if existing_agent:
                recovery_error = SW._recover_code_launch(
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
                    SS.save_state(state_dir, state)
                return str(existing_agent), None
            candidates = SP._git_worktrees_on_branch(
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
            ownership_problem = SW._paseo_path_ownership_problem(reuse_workspace)
            if ownership_problem:
                return None, (
                    f"attempt {attempt!r} will not launch an agent in "
                    f"pre-existing worktree {reuse_workspace!r}: "
                    f"{ownership_problem}")
            reuse_error = SCL._complete_code_launch(
                state, u, unit_dir, reuse_workspace,
                workspace_owner="coordinator")
            if reuse_error:
                return None, (
                    f"attempt {attempt!r} has an unowned worktree, but it "
                    f"does not match the trusted launch intent: {reuse_error}")
            # The exact-base worktree is now authenticated and durable. It is
            # safe to create the missing agent in it without recreating Git
            # resources or resolving a live ref.
            SS.save_state(state_dir, state)
        else:
            reuse_workspace, git_pointer_sha256, create_error = (
                SCL._create_code_worktree(state_dir, intent))
            if reuse_workspace:
                workspace_meta = SCL._register_code_workspace(
                    state, u, unit_dir, reuse_workspace,
                    workspace_owner="coordinator")
                # Registration precedes verification so even a rejected
                # checkout remains visible to preservation and cleanup.
                SS.save_state(state_dir, state)
            if create_error:
                return None, create_error
            complete_error = SCL._complete_code_launch(
                state, u, unit_dir, reuse_workspace,
                workspace_owner="coordinator",
                git_pointer_sha256=git_pointer_sha256)
            if complete_error:
                workspace_meta["verification"] = "refused"
                workspace_meta["cleanup_pending"] = True
                workspace_meta["verification_problem"] = complete_error
                SS.save_state(state_dir, state)
                return None, (f"coordinator-created worktree could not be "
                              f"verified: {complete_error}")
            SS.save_state(state_dir, state)
        # U.run contains the short-lived Paseo client's environment. The
        # long-lived Paseo daemon is a separate process boundary: a live probe
        # showed its provider credentials can still reach an agent even when
        # absent from this client's environment. Paseo's --env cannot replace
        # those provider variables. Do not mistake this boundary for daemon
        # isolation; the daemon must itself be launched without ambient keys.
        argv = ["paseo", "run", "--background", "--json",
                "--cwd", str(reuse_workspace)]
        argv += ["--provider", u.get("provider") or SR.DEFAULT_AGENT_PROVIDER,
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
        thinking = u.get("thinking", SR.default_thinking_for(u))
        if thinking:
            argv += ["--thinking", str(thinking)]
        for kv in (u.get("env") or []):
            argv += ["--env", str(kv)]
        prompt = SCP._dispatch_prompt(u, intent)
        protocol_problem = SCP._code_protocol_problem(prompt, intent)
        if protocol_problem:
            return None, (
                f"unit {u['id']!r} code prompt has no valid completion "
                f"protocol ({protocol_problem}); refusing an unclosable "
                f"agent launch")
        if codex:
            us = SS._unit_state(state, u["id"])
            had_homes = "attempt_codex_homes" in us
            previous_homes = us.get("attempt_codex_homes")
            homes = dict(previous_homes) if had_homes else {}
            try:
                home, home_identity = CH.allocate(state_dir, attempt, unit_dir, u)
                try:
                    # Allocation captured identity under its rollback guard.
                    # Cover metadata construction and mutation as well as save.
                    home_meta = {"path": str(home), "removed": False,
                                 "identity": home_identity}
                    homes[attempt] = home_meta
                    us["attempt_codex_homes"] = homes
                    # Cleanup authority is durable before links or an agent exist.
                    SS.save_state(state_dir, state)
                except BaseException:
                    # Metadata may not exist yet. Use allocation's identity,
                    # and restore memory even if a replacement refuses cleanup.
                    try:
                        CH.remove(state_dir, attempt, str(home), home_identity)
                    finally:
                        if had_homes:
                            us["attempt_codex_homes"] = previous_homes
                        else:
                            us.pop("attempt_codex_homes", None)
                    raise
                CH.populate(home, auth)
            except (CH.HomeError, CP.PathPolicyError) as exc:
                return None, str(exc)
            argv += ["--env", f"CODEX_HOME={home}"]
        # One list element, however many lines it contains. U.run does not
        # invoke a shell, and Paseo's launcher forwards its argv with "$@",
        # so newlines reach the runner as prompt content rather than argument
        # separators.
        argv.append(prompt)

        rc, out, err = U.run(argv, timeout=180)
        if rc != 0:
            workspace_meta = SCL._register_code_workspace(
                state, u, unit_dir, reuse_workspace,
                workspace_owner="coordinator")
            workspace_meta["verification"] = "refused"
            workspace_meta["cleanup_pending"] = True
            workspace_meta["verification_problem"] = "paseo run failed"
            SS.save_state(state_dir, state)
            return None, f"paseo run failed: {SW._paseo_error(out, err)}"
        # Read the id from JSON. Scanning output tokens for "something long
        # with a dash in it" would happily return a branch name or a path.
        # paseo names this field `agentId`. Reading `id` found nothing and
        # left a live agent running, unbound and unjudgeable: the orphan class
        # the Slurm path has a reconcile net for.
        rec = SW._paseo_json(out) or {}
        agent = (rec.get("agentId") or rec.get("AgentId")
                 or rec.get("id") or rec.get("Id"))
        reported_workspace = rec.get("cwd") or rec.get("Cwd")
        workspace = reuse_workspace
        workspace_id = (SCL._paseo_workspace_id(out)
                        or rec.get("workspaceId") or rec.get("WorkspaceId"))
        # Paseo has already acted. Persist ownership of the resource now,
        # before checks that may reject it; registration makes it cleanable
        # and does not endorse its contents. Cleanup remains bound to the
        # coordinator-created path even if Paseo omits or misreports cwd.
        workspace_meta = SCL._register_code_workspace(
            state if state is not None else {}, u, unit_dir, workspace,
            workspace_id=workspace_id, workspace_owner="coordinator")
        if state_dir is not None:
            SS.save_state(state_dir, state)
        if not agent:
            workspace_meta["verification"] = "refused"
            workspace_meta["cleanup_pending"] = True
            workspace_meta["verification_problem"] = (
                "Paseo returned no agent id")
            if state_dir is not None:
                SS.save_state(state_dir, state)
            return None, (f"paseo run returned no agent id: "
                          f"{SW._paseo_error(out, err)}")
        if reuse_workspace:
            facts = SS.trusted_launch_facts(state, u["id"], unit_dir)
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
                SS.save_state(state_dir, state)
                return None, (f"paseo run returned agent {agent}, but reused "
                              f"worktree verification failed: "
                              f"{identity_error}")
        complete_err = SCL._complete_code_launch(
            state if state is not None else {}, u, unit_dir, workspace,
            workspace_id=workspace_id, recovery=True,
            workspace_owner="coordinator")
        if complete_err:
            workspace_meta["verification"] = "refused"
            workspace_meta["cleanup_pending"] = True
            workspace_meta["verification_problem"] = complete_err
            if state_dir is not None:
                SS.save_state(state_dir, state)
            return None, (f"paseo run returned agent {agent}, but its "
                          f"worktree could not be recorded: {complete_err}")
        if state_dir is not None:
            SS.save_state(state_dir, state)
        return str(agent), None
    return None, f"unknown kind {kind!r}"


def _bind(unit_dir, job_id):
    rc, out, err = U.run([sys.executable, str(_HERE / "unit.py"), "bind",
                          str(unit_dir), "--job-id", str(job_id)], timeout=120)
    return None if rc == 0 else f"bind failed: {(err or out).strip()[:200]}"
