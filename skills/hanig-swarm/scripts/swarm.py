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
import codex_home as CH  # noqa: E402  local filesystem only

KINDS = U.KINDS

import swarm_routing as SR
import swarm_state as SS
import swarm_journal as SJ
import swarm_evidence as SE
import swarm_outbox as SO
import swarm_plan_rules as SPR
import swarm_preflight as SP
import swarm_code_protocol as SCP
import swarm_code_launch as SCL
import swarm_workspaces as SW
import swarm_dispatch as SD
from swarm_dispatch import (
    _allocate, _dep_env, _isolation_submission, _record_isolation_facts,
    _clear_isolation_marker, _slurm_exit_trap, _bind,
    _CONTAINER_BIND_ENV,
)
from swarm_workspaces import (
    _paseo_json,
    _paseo_error,
    _paseo_workspace_records,
    _paseo_workspace_for_path,
    _paseo_path_ownership_problem,
    _registered_attempt_workspace,
    _recover_code_launch,
    _report_retained_worktrees,
    _report_would_archive_code_worktree,
    _worktree_cleanup_failed,
    _cleanup_codex_home,
    _archive_code_worktree,
    _preserve_and_archive_code_worktree,
    reconcile_orphan,
    WORKTREE_ARCHIVE_MAX_ATTEMPTS,
    WORKTREE_CLEANUP_SUMMARY_PREFIX,
)
from swarm_code_launch import (
    LAUNCH_RECORD, _capture_code_launch, _code_launch_intent_problem,
    _paseo_workspace_id, _code_launch_record_payload, _create_code_worktree,
    _register_code_workspace, _write_code_launch_record, _complete_code_launch,
    _write_launch_record,
)
from swarm_code_protocol import (
    _seed_carry_forward, _code_completion_protocol, _dispatch_prompt,
    _code_protocol_problem, CODE_COMPLETION_PROTOCOL_MARKER,
)
from swarm_preflight import (
    _git, _unattended_mode_example, _workspace_policy, _requires_clean_workspace,
    _execution_workspace, PreflightRefusal, _plan_workspace, _dirty_refusal,
    _repeat_launch_preflight, _inside_dir, _output_destinations, _claim_dir,
    _claim_liveness, _claim_refusal, _claim_record, _own_claim, _take_output_claims,
    _drop_claims, _release_output_claims, _note_claimed_attempt,
    OUTPUT_CLAIMS_DIRNAME, CLAIM_FILE, CLAIM_LIVE, CLAIM_FREE, CLAIM_UNKNOWN,
    DRY_PREFIX,
    _code_worktree_names,
    _git_worktrees_on_branch,
    _stash_refusal,
    _stash_preflight,
    _repeat_stash_preflight,
    _git_push_destination,
    _exact_remote_ref_head,
    _dispatch_base_refusal,
    _resolve_dispatch_target,
    _dispatch_source_identity,
    _dispatch_target_for_advance,
    _installed_skill_source_version,
    _installed_skill_snapshot,
    _warn_installed_skill_drift,
    _seed_reachability_problem,
)
from swarm_plan_rules import (
    _unquoted_shell_syntax,
    _isolation_command_argv,
    isolation_problem,
    _ancestors,
    resolve_runtime,
    _looks_unresolved,
    _runtime_identity,
    _validate_runtimes,
    converge_problem,
    _units_with_canary,
    plan_digest,
    RESOLUTIONS,
    ISOLATION_BACKENDS,
    ISOLATION_KEYS,
    ISOLATION_ROOT_TOKENS,
    ISOLATION_SHELL_EXECUTABLES,
    ISOLATION_MULTICALL_EXECUTABLES,
    ISOLATION_LAUNCHER_EXECUTABLES,
    _NEEDS_RUNTIME,
    _PLACEHOLDER_RE,
    _PLACEHOLDER_EXACT,
    _PLACEHOLDER_PREFIX,
    CONVERGE_UNIT_KEYS,
    COSMETIC_FIELDS,
    declared_seed,
    declared_scope,
    _norm_scope,
    _scopes_overlap,
    declared_account,
    declared_partition,
    _known_partitions,
    partition_problems,
    read_survey,
    discover_survey,
    _scheduler_facts,
    surveyed_partition,
    limit_state,
    _account_names,
    declared_memory,
    memory_flag_problems,
    account_problems,
    resolve_promote_to,
    _LOOK_IT_UP,
    SURVEY_FILE,
    LIMIT_SET,
    LIMIT_OPEN,
    LIMIT_UNKNOWN,
    MEM_FLAGS,
)
from swarm_outbox import (
    emit_intent,
    backfill_tracker_intents,
    _parse_receipt_text,
    _read_receipts_raw,
    _receipt_shape_problem,
    load_acknowledgments,
    _receipt_grade,
    _append_receipt_atomic,
    record_receipt,
    acknowledgment_status,
    read_outbox,
    load_outbox_contract,
    OUTBOX,
    RECEIPT_SCHEMA_VERSION,
    TRACKER_EVENTS,
    RECEIPTS,
    RECEIPT_CONFIRMED,
    RECEIPT_CONFIRMING_SOURCES,
    UNACKNOWLEDGED,
    ATTESTED_UNSPECIFIED,
    ATTESTED_CONFIRMED,
    ACKNOWLEDGED,
    CONFLICT,
    closing_evidence_for,
    outbox_key,
    _intent_evidence_digest,
    _intent_attempt_identity,
    _intent_envelope,
    normalize_intent,
    _nonblank_text,
    _schema_is,
    validate_intent,
    validate_observation,
    require_valid_intent,
    _receipt_admissible,
    reconcile_observation,
    _has_bound_merge_evidence,
    _intent_key,
    _with_intent_envelope,
    INTENT_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    RECONCILIATION_SCHEMA_VERSION,
    INTENT_CONNECTOR_CAPABILITY,
    INTENT_OPERATIONS,
    OPERATION_ACCEPTED,
    ASYNC_COMPLETED,
    CONFIRMED_BY_READBACK,
    UNKNOWN,
    OBSERVATION_OUTCOMES,
    MUTATION_RESPONSE,
    A2A_LIFECYCLE,
    RECEIVER_READBACK,
    RECEIVER_DEDUPLICATION,
    CONFIRMING_SOURCES,
    OBSERVATION_SOURCES,
    CLOSING_EVIDENCE,
)
from swarm_evidence import (
    trusted_base,
    trusted_produced_head,
    _capture_artifact_basis,
    raw_artifact_basis,
    trusted_artifact_basis,
    trusted_isolation_facts,
    _reported_check_result,
    _record_receipt_provenance,
    trusted_record_seal,
    admit_merge,
    _authority_result_sink,
    _check,
    CHECK_RESULT_PREFIX,
    RECEIPT_PROVENANCE_LIMIT,
    ISOLATION_MARKER_NAME,
    _merge_shape_problem,
    _read_merge_raw,
    load_merge_receipts,
    _same_repo,
    _verify_shape_problem,
    _read_verify_raw,
    load_verifications,
    admit_verification,
    _receipt_reason,
    attested_receipt,
    MERGE_RECEIPTS,
    MERGE_METHODS,
    _MERGE_REQUIRED,
    VERIFY_RECEIPTS,
    _VERIFY_REQUIRED,
)
from swarm_journal import (
    _heal_jsonl_tail, _fsync_append, _RawJournal, fatal_problems, _fsync_directory,
)
from swarm_state import (
    _holds_the_path,
    _StateSnapshot,
    _read_state_epoch,
    STATE_FILE,
    STATE_EPOCH_FILE,
    LEASE,
    LOCK,
    _LOCK_FDS,
    _LOCK_EPOCHS,
)
from swarm_routing import (
    _routing_token, _routing_model, load_agent_routing, _model_family,
    apply_agent_resolution, default_thinking_for,
)
from swarm_types import (
    PlanError, OutboxError, DONE, RUNNING, FAILED, PREEMPTED, INCOMPLETE,
    NEEDS_HUMAN, NAME, EXIT_OK, EXIT_HALTED, EXIT_FAILED_UNIT, EXIT_USAGE,
    EXIT_CONFLICT, EXIT_SCOPE_OUTSIDE, EXIT_SCOPE_UNCHECKED,
)


def __getattr__(name):
    # Read state helpers through their owner so patches reach every consumer;
    # the lock owner PID is rebound there after acquisition and fork detection.
    if name in {
        "acquire_lease", "renew_lease", "release_lease", "load_state",
        "_LOCK_OWNER_PID",
        "save_state",
        "_unit_state",
        "_set_unit_state",
        "_normalise_state_clock",
        "_deadline_host_evaluation",
        "_deadline_exceeded",
        "_mark_deadline_exceeded",
        "_clear_deadline_breach",
        "_deadline_attempt_is_unresolved",
        "_occupies_live_resources",
        "trusted_launch_facts",
        "trusted_launch_host_anchor",
        "LIVE_STATES",
    }:
        return getattr(SS, name)
    # Routing resolution rebinds defaults. Read through to their owner so
    # legacy swarm.<name> callers see the current plan's routing, not a copy.
    if name in {
        "AGENTS_FILE", "DEFAULT_AGENT_PROVIDER", "DEFAULT_AGENT_THINKING",
        "THINKING_BY_MODEL", "PINNED_AGENT_PROVIDER", "PINNED_THINKING_BY_MODEL",
    }:
        return getattr(SR, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


# --- plan validation ------------------------------------------------------
# The one artifact hanig-project's report renders as the project's own
# claims. Named here because validate now has an opinion about it; see the
# findings block in validate_plan for what that opinion is and is not.
FINDINGS_FILE = "findings.json"

# The caveat that must ride along with every one of these judgements. Said
# in the refusal itself, because that is the message someone reads while
# choosing the partition they will move the unit to.
QOS_CAVEAT = (
    "Note that `qos_grptres` in the survey resolves the PARTITION QOS only: "
    "an account or association QOS can impose a GrpTRES nothing here can "
    "see, so a partition that accepts this account is still not a promise "
    "the job will run.")


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


def validate_plan(plan, survey=None, new_attempts=True):
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
        SPR.declared_seed(u)
        SPR.declared_scope(u)
        if "tracker" in u and not SO._nonblank_text(u["tracker"]):
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
        units = SPR._units_with_canary(plan)

    # A convergence criterion is refused HERE, before anything is dispatched,
    # for the reason converge.py gives for requiring one at all: it has to be
    # declared before the run. Discovering the typo when the job is finished
    # means the GPU-hours are already spent and there is no criterion to judge
    # them against.
    for u in units:
        if not isinstance(u, dict):
            continue
        problem = SPR.converge_problem(u)
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
            _, derr = SPR.resolve_promote_to(u.get("promote_to"))
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
        problem = U.slurm_output_problem(u.get("outputs") or []) if new_attempts and u.get("kind") == "slurm" else None
        if problem:
            raise PlanError(f"unit {u.get('id', '?')!r}: {problem}")

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
    SPR._validate_runtimes(plan, units)

    # Optional OS-backed containment is a declaration, never an inference
    # from runtime.resolution, an image suffix, a partition, or whichever
    # executable happens to be first on PATH.  Refuse the whole plan before
    # dispatch when its writable host surface cannot be rendered exactly.
    for u in units:
        if not isinstance(u, dict):
            continue
        problem = SPR.isolation_problem(u)
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
            if SPR._looks_unresolved(text):
                unresolved.append((u.get("id", "?"), text))
                continue
            makers = produced_by.get(text)
            if makers:
                uid_here = u.get("id")
                if makers & SPR._ancestors(uid_here, _by_id):
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

    bad_parts = SPR.partition_problems(units)
    if bad_parts:
        known = sorted(SPR._known_partitions() or [])
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
    starved = SPR.memory_flag_problems(units, survey)
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

    refused = SPR.account_problems(units, survey)
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
                    if SPR._scopes_overlap(sa, sb):
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
        assembled = SCP._dispatch_prompt(u, unit_intent)
        problem = SCP._code_protocol_problem(assembled, unit_intent)
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
        unattended = SP._unattended_mode_example(u.get("provider"))
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
                f"{u.get('provider') or SR.DEFAULT_AGENT_PROVIDER}, or "
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
        rt, _ref = SPR.resolve_runtime(plan, u)
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
                and not SPR.declared_partition(u))}


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
# older than this was abandoned by a controller that died.
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

# --- dispatch -------------------------------------------------------------
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
            SD._isolation_submission(u, unit_dir, prior))
        if isolation_error:
            return None, isolation_error
        isolation_error = SD._clear_isolation_marker(isolation_facts)
        if isolation_error:
            return None, isolation_error
        isolation_error = SD._record_isolation_facts(
            state, u, unit_dir, isolation_facts)
        if isolation_error:
            return None, isolation_error
        # This is the fact the later receipt may report. Persist it before the
        # external launcher sees the wrapper; a crash may repeat submission
        # recovery, but it may never reconstruct or replace this boundary from
        # the agent-writable job script or attempt spec.
        SS.save_state(state_dir, state)
    if kind == "slurm":
        deps = SD._dep_env(u, state or {})
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
            body.append(SD._slurm_exit_trap(unit_dir))
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
                    **dict(SD._dep_env(u, state or {})),
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
    if (SE._receipt_reason(state or {}, uid, us.get("attempt_dir") or "")
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
        SS.save_state(state_dir, state)
    rc, out, err = U.run(["paseo", "send", str(agent), prompt], timeout=120)
    entry["sent"] = rc == 0
    entry["status"] = "sent" if rc == 0 else "failed"
    if rc != 0:
        entry["error"] = (err or out or "").strip()[:200]
    else:
        SS._set_unit_state(us, "RUNNING")
    SS.save_state(state_dir, state)
    if rc != 0:
        report.append(f"{uid}: continuation {used + 1}/{limit} could not be "
                      f"sent: {entry['error']}")
        return False
    report.append(
        f"{uid}: settled without producing; sent continuation "
        f"{used + 1}/{limit} to agent {agent} in the SAME attempt. After "
        f"{limit} the unit fails for missing production evidence.")
    return True


# --- the DAG --------------------------------------------------------------
RETRYABLE = {PREEMPTED}          # a preemption is not a failure
TERMINAL_BAD = {FAILED}
WORKTREE_CLEANUP_STATES = frozenset(
    ("READY_FOR_PR", "DONE", "FAILED", "FAILED_EVIDENCE", "PREEMPTED"))


def advance(plan, state, state_dir, root, dry_run, max_new=None,
            accept_plan_change=False):
    """Idempotent. Re-check every live unit, then dispatch whatever is ready.

    Safe to run from a Paseo schedule or cron every few minutes: it never
    re-submits a unit that has an attempt recorded, and it persists before it
    acts."""
    U.SACCT_UNAVAILABLE = False
    units = {u["id"]: u for u in SPR._units_with_canary(plan)}
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
                      if (SS._unit_state(state, uid).get("attempt_dir")
                          and not str(
                              SS._unit_state(state, uid).get("job_id") or "")
                          .startswith("dry-")
                          and SS._occupies_live_resources(
                              units[uid], SS._unit_state(state, uid))))
        if real:
            return ([f"REFUSING to dry-run against a state directory that "
                     f"holds REAL attempts ({', '.join(real)}).",
                     f"A dry run records placeholder job ids, and a unit that "
                     f"has one can never be judged or re-dispatched, so this "
                     f"would wedge work that is genuinely running.",
                     f"Use a throwaway state directory instead:",
                     f"    swarm.py run <plan> --dry-run --state-dir "
                     f"$(mktemp -d)/state --root $(mktemp -d)/runs"], 0, None)
    before_states = {uid: (SS._unit_state(state, uid) or {}).get("state")
                     for uid in units}

    # Persist a state clock for snapshots created before state_changed_at
    # existed. The honest reference is when this coordinator first observed
    # the old state, not allocated_at: a unit may have changed states long
    # after allocation, and pretending otherwise would overstate its stall.
    clock_observed_at = advance_observed_at
    if not dry_run:
        for uid in units:
            SS._normalise_state_clock(SS._unit_state(state, uid), clock_observed_at)

    # NORMALISE PERSISTED STATE FIRST. Converting DONE to READY_FOR_PR only in
    # the fresh-check path left every ALREADY-persisted DONE untouched: a
    # state file written before this rule existed, or a unit whose kind
    # changed from slurm to code, kept a DONE that the re-check loop skips by
    # design. Its dependents then dispatched on evidence this system says
    # cannot close a code unit. Three reviewers found it independently, and it
    # is the third time in a row I have fixed a forward path and left the
    # stored state alone.
    for uid, u in sorted(units.items()):
        us = SS._unit_state(state, uid)
        if (us.get("state") == "DONE"
                and SO.closing_evidence_for(u.get("kind")) != "predicate_receipt"):
            SS._set_unit_state(us, "READY_FOR_PR")
            report.append(
                f"{uid}: recorded DONE, but a {u.get('kind')} unit is closed "
                f"by a merged pull request, not by its own receipt. Corrected "
                f"to READY_FOR_PR; anything depending on it waits.")
            SS.save_state(state_dir, state)

    # Worktrees are needed while an agent can still be continued. Once an
    # attempt has a terminal judgment (including READY_FOR_PR), its committed
    # branch is the durable handoff and the checkout is operational debris.
    # Retry failed cleanups on every advance; this bounds managed worktrees to
    # live/interactive attempts plus cleanup failures that remain visible.
    for uid, u in sorted(units.items()):
        us = SS._unit_state(state, uid)
        current_attempt = (Path(us["attempt_dir"]).name
                           if us.get("attempt_dir") else None)
        current_meta = ((us.get("attempt_workspaces") or {}).get(
            current_attempt) if current_attempt else None)
        if (us.get("state") in WORKTREE_CLEANUP_STATES
                or (isinstance(current_meta, dict)
                    and current_meta.get("cleanup_pending"))):
            if not dry_run:
                SW._archive_code_worktree(
                    state, u, us.get("attempt_dir"), report, state_dir)
            else:
                SW._report_would_archive_code_worktree(
                    state, u, us.get("attempt_dir"), report)
        for attempt, meta in (us.get("attempt_workspaces") or {}).items():
            if (attempt != current_attempt and isinstance(meta, dict)
                    and meta.get("cleanup_pending")):
                if not dry_run:
                    SW._archive_code_worktree(
                        state, u, attempt, report, state_dir)
                else:
                    SW._report_would_archive_code_worktree(
                        state, u, attempt, report)
        # A home removal can fail after its worktree was archived, or after
        # a non-code attempt was released for retry. Neither should require
        # another worktree cleanup or prevent independent units dispatching.
        for attempt, meta in (us.get("attempt_codex_homes") or {}).items():
            if (attempt != current_attempt and isinstance(meta, dict)
                    and meta.get("cleanup_pending") and not dry_run):
                SW._cleanup_codex_home(state, u, attempt, report, state_dir)
    SW._report_retained_worktrees(state, report, dry_run=dry_run)
    if not dry_run:
        SS.save_state(state_dir, state)

    # The plan must not change under a live run. A mid-flight edit silently
    # redefines what the recorded attempts were for.
    digest = SPR.plan_digest(plan)
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
        SS.save_state(state_dir, state)
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
        us = SS._unit_state(state, uid)
        host_local, host_problem = SS._deadline_host_evaluation(state, uid, us)
        if (u.get("deadline_s") is not None and us.get("attempt_dir")
                and host_local is not True):
            report.append(
                f"{uid}: DEADLINE_NOT_EVALUABLE_HERE -- {host_problem}")
            continue
        if (not dry_run
                and not SS._deadline_exceeded(
                    u, us, state, uid, deadline_observed_at)):
            SS._clear_deadline_breach(us)
    if not dry_run:
        SS.save_state(state_dir, state)

    # An attempt allocated but never bound may still have reached the
    # scheduler. Ask before dispatching anything else.
    for uid, u in sorted(units.items()):
        us = SS._unit_state(state, uid)
        if dry_run or not us["attempt_dir"]:
            continue
        if (u.get("kind") == "code" and us.get("job_id")
                and not SS.trusted_launch_facts(
                    state, uid, us["attempt_dir"])):
            host_problem = W.launch_host_problem(
                SS.trusted_launch_host_anchor(
                    state, uid, us["attempt_dir"]))
            if host_problem:
                us["launch_recovery_pending"] = True
                us["host_judgment"] = "UNJUDGEABLE_HERE"
                us.pop("launch_recovery_problem", None)
                report.append(f"{uid}: UNJUDGEABLE_HERE -- {host_problem}. "
                              f"The agent is retained for a coordinator on "
                              f"its launch host.")
                SS.save_state(state_dir, state)
                continue
            recovery_error = SW._recover_code_launch(
                state, u, us["attempt_dir"], us["job_id"])
            if recovery_error:
                us["launch_recovery_problem"] = recovery_error
                SS._set_unit_state(us, "NEEDS_HUMAN")
                # No trusted launch facts means the checker cannot run. A
                # deadline crossed during that failed recovery may still
                # supersede the recovery reason without inventing a verdict.
                if SS._deadline_exceeded(u, us, state, uid):
                    first_breach = SS._mark_deadline_exceeded(u, us)
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
                SS.save_state(state_dir, state)
                continue
            us.pop("launch_recovery_problem", None)
            SS._set_unit_state(us, "SUBMITTED")
            report.append(f"{uid}: recovered the agent's attempt worktree and "
                          f"completed its trusted launch snapshot")
            SS.save_state(state_dir, state)
        if us.get("bind_pending") and us.get("job_id"):
            if not SD._bind(us["attempt_dir"], us["job_id"]):
                us.pop("bind_pending", None)
                SS._set_unit_state(us, "SUBMITTED")
                report.append(f"{uid}: binding to job {us['job_id']} succeeded "
                              f"on retry; the unit can now be judged.")
                SS.save_state(state_dir, state)
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
            SS._set_unit_state(us, None)
            SS.save_state(state_dir, state)
            continue
        if us.get("job_id"):
            continue
        job, note = SW.reconcile_orphan(us["attempt_dir"], us.get("allocated_at"),
                                     kind=u.get("kind"))
        if job:
            if u.get("kind") == "code":
                host_problem = W.launch_host_problem(
                    SS.trusted_launch_host_anchor(
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
                    SS.save_state(state_dir, state)
                    continue
                recovery_error = SW._recover_code_launch(
                    state, u, us["attempt_dir"], job)
                if recovery_error:
                    # Bind the identity durably so the next advance asks this
                    # exact agent again instead of repeating title discovery.
                    us["job_id"] = job
                    us["launch_recovery_pending"] = True
                    us["launch_recovery_problem"] = recovery_error
                    SS._set_unit_state(us, "NEEDS_HUMAN")
                    if SS._deadline_exceeded(u, us, state, uid):
                        first_breach = SS._mark_deadline_exceeded(u, us)
                        if first_breach:
                            report.append(
                                f"{uid}: NEEDS_HUMAN -- deadline_exceeded "
                                f"during attempt recovery. The attempt "
                                f"remains bound; no cancellation, claim "
                                f"release, or retry was requested.")
                    report.append(f"{uid}: {note}; NEEDS_HUMAN -- "
                                  f"{recovery_error}. The agent is retained, "
                                  f"but no launch facts were admitted.")
                    SS.save_state(state_dir, state)
                    continue
                us.pop("launch_recovery_pending", None)
                us.pop("launch_recovery_problem", None)
            # The bind marker is an external durable write. Record which job
            # it is allowed to name before creating it; a crash then retries
            # an idempotent bind instead of leaving unauthorised evidence.
            us["job_id"] = job
            us["bind_pending"] = True
            SS._set_unit_state(us, "SUBMITTED")
            SS.save_state(state_dir, state)
            bind_error = SD._bind(us["attempt_dir"], job)
            if bind_error:
                report.append(f"{uid}: recovered {job} but {bind_error}; "
                              f"binding will retry next advance")
            else:
                us.pop("bind_pending", None)
            report.append(f"{uid}: {note}")
            SS.save_state(state_dir, state)
        elif SS._deadline_exceeded(u, us, state, uid):
            SS._mark_deadline_exceeded(u, us)
            report.append(
                f"{uid}: NEEDS_HUMAN -- deadline_exceeded while recovering "
                f"the unbound attempt. It remains allocated; no submission, "
                f"release, or retry was performed.")
            SS.save_state(state_dir, state)
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
                    branch_rc, _out, _err = SP._git(
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
                        target, problem = SP._dispatch_target_for_advance(
                            u, dispatch_targets)
                        if not problem:
                            source, problem = SP._dispatch_source_identity(u, target)
                        if not problem:
                            recovered_job, problem = _submit(
                                u, us["attempt_dir"], False, state, state_dir,
                                dispatch_source=source)
                        if problem:
                            SS._set_unit_state(us, "NEEDS_HUMAN")
                            us["launch_recovery_problem"] = problem
                            report.append(f"{uid}: NEEDS_HUMAN -- {problem}")
                            SS.save_state(state_dir, state)
                            continue
                        us["job_id"] = str(recovered_job)
                        SS._set_unit_state(us, "SUBMITTED")
                        us["bind_pending"] = True
                        SS.save_state(state_dir, state)
                        bind_error = SD._bind(us["attempt_dir"], recovered_job)
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
                        SS.save_state(state_dir, state)
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
            SS.save_state(state_dir, state)

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
        if not dry_run and SS.renew_lease(state_dir) is False:
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
            SS.save_state(state_dir, state)
            return report, dispatched, state["halted"]
        us = SS._unit_state(state, uid)
        if not us["attempt_dir"]:
            continue
        attempt = us["attempt_dir"]
        launch_facts = SS.trusted_launch_facts(state, uid, attempt)
        host_problem = W.launch_host_problem(
            SS.trusted_launch_host_anchor(state, uid, attempt))
        if host_problem:
            us["host_judgment"] = "UNJUDGEABLE_HERE"
            us.pop("incomplete_since", None)
            us.pop("launch_recovery_problem", None)
            report.append(f"{uid}: UNJUDGEABLE_HERE -- {host_problem}")
            SS.save_state(state_dir, state)
            continue
        if us.get("launch_recovery_problem"):
            report.append(f"{uid}: NEEDS_HUMAN -- "
                          f"{us['launch_recovery_problem']}")
            continue
        us.pop("host_judgment", None)
        if us["state"] == "DONE":
            continue
        pinned_before = SE.trusted_produced_head(state, uid, attempt)
        ran_check = not (u.get("kind") == "code" and pinned_before)
        protocol_problem = None
        if ran_check:
            isolation_facts = SE.trusted_isolation_facts(state, u, attempt)
            check_args = (
                attempt, launch_facts,
                SE.raw_artifact_basis(state, uid, attempt))
            # Preserve the historical call shape for undeclared units and old
            # embedders. A declared profile takes the fourth, trusted-by-value
            # argument; there is no attempt-directory fallback.
            check_result = (SE._check(
                *check_args, isolation_facts, isolation_required=True)
                if u.get("isolation") is not None else SE._check(*check_args))
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
            check_report, protocol_problem = SE._reported_check_result(
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
            SE._record_receipt_provenance(state, uid, attempt, digest)

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
                and SS._deadline_exceeded(u, us, state, uid)):
            if dry_run:
                report.append(
                    f"{uid}: DRY RUN -- deadline_exceeded after check; would "
                    f"become NEEDS_HUMAN without cancellation, claim "
                    f"release, or retry")
                continue
            first_breach = SS._mark_deadline_exceeded(u, us)
            if first_breach:
                report.append(
                    f"{uid}: NEEDS_HUMAN -- deadline_exceeded while the "
                    f"attempt was being checked. It remains bound and its "
                    f"output claim is retained; no cancellation or retry "
                    f"was requested.")
            SS.save_state(state_dir, state)
            continue
        SS._set_unit_state(us, checked_state)
        if checked_state in terminal:
            SS._clear_deadline_breach(us)

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
                SS._set_unit_state(us, "NEEDS_HUMAN")
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
                and SO.closing_evidence_for(u.get("kind")) != "predicate_receipt"):
            # STAGE 3. A produced tree is not a closed unit: `code` is closed
            # by a merged PR. The attestation is admitted only when the head
            # it pins is the one this attempt produced, which is the single
            # check available to a coordinator with no network.
            attempt = us.get("attempt_dir") or ""
            produced = SE.trusted_produced_head(state, uid, attempt)
            launch_facts = SS.trusted_launch_facts(state, uid, attempt)
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
            merge_receipt, merge_refusal = SE.admit_merge(
                state_dir, uid, produced, **merge_args)
            vrefusal = None
            required = u.get("requires_verification") or []
            policy_digest, perr, _pol = None, None, None
            if required:
                base = SE.trusted_base(state, uid, attempt)
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
                _vr, vrefusal = SE.admit_verification(
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
                SS._set_unit_state(us, "DONE")
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
                SS._set_unit_state(us, "READY_FOR_PR")
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
                reason = SE._receipt_reason(state, uid, us["attempt_dir"])
                if (reason == U.REASON_NO_OUTPUTS
                        and maybe_continue(state_dir, uid, u, us, report,
                                           state)):
                    SS.save_state(state_dir, state)
                    continue
                if reason == U.REASON_NO_OUTPUTS:
                    used = len(us.get("continuations") or [])
                    if used:
                        report.append(
                            f"{uid}: {used} continuation(s) sent and it still "
                            f"produced nothing. The bound is the point: this "
                            f"fails for missing production evidence rather "
                            f"than being prodded again.")
                    SS._set_unit_state(us, "FAILED")
                    report.append(
                        f"{uid}: the job finished cleanly and its declared "
                        f"outputs never appeared, {waited}s on. This is a "
                        f"FAILED unit, not missing evidence: the scheduler "
                        f"will tell you it succeeded. Read the job's own log "
                        f"in {us['attempt_dir']}.")
                elif reason == U.REASON_NO_PRODUCED_CHANGE:
                    # A refused repository transition is not a planning-only
                    # turn: do not turn continuation into a correction loop.
                    SS._set_unit_state(us, "FAILED")
                    receipt, why = SE.attested_receipt(
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
                    SS._set_unit_state(us, "FAILED_EVIDENCE")
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
                             if not str(a).startswith(SP.DRY_PREFIX)]
            if len(real_attempts) < policy:
                # A retry mints a NEW write root. Reusing one is precisely what
                # makes the predicate inconclusive.
                us["attempt_dir"] = None
                report.append(f"{uid}: preempted, will re-attempt "
                              f"({len(real_attempts)}/{policy})")
            else:
                SS._set_unit_state(us, "FAILED")
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
                SS.save_state(state_dir, state)
                SW._archive_code_worktree(
                    state, u, attempt, report, state_dir)
            else:
                SW._report_would_archive_code_worktree(
                    state, u, attempt, report)
    SS.save_state(state_dir, state)

    # 1b. Release the output claims of units that are no longer live (B6).
    #     Recomputed from the plan and released only when the claim names
    #     THIS state directory, so a coordinator can free its own claims --
    #     including ones it left behind by dying -- and can never free
    #     anybody else's. This is what keeps a crash from wedging a project
    #     without a TTL: our own state file is authority for our own units,
    #     and `acquire_lease` already serialises access to it.
    if not dry_run:
        for uid, u in sorted(units.items()):
            if not SS._occupies_live_resources(u, SS._unit_state(state, uid)):
                SP._release_output_claims(u, root, state_dir)

    # 2. Budget. Charged on DISPATCH, not on completion: a budget that only
    #    counts finished work cannot stop a runaway.
    budget = (plan.get("budget") or {}).get("gpu_hours")
    spent = sum(SS._unit_state(state, uid)["gpu_hours"] for uid in units)

    # 3. Dispatch every unit whose dependencies are DONE.
    for uid, u in sorted(units.items()):
        us = SS._unit_state(state, uid)
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
                           if SS._unit_state(state, d)["state"] in
                           ("FAILED", "HELD", "FAILED_EVIDENCE")]
        if failed_upstream:
            SS._set_unit_state(us, "HELD")
            report.append(f"{uid}: held, upstream "
                          f"{', '.join(failed_upstream)} will not complete")
            SS.save_state(state_dir, state)
            continue
        unmet = [d for d in needs
                 if SS._unit_state(state, d)["state"] != "DONE"]
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
                if SS._occupies_live_resources(units[x], SS._unit_state(state, x))]
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
            target, problem = SP._dispatch_target_for_advance(u, dispatch_targets)
            if not problem:
                dispatch_source, problem = SP._dispatch_source_identity(u, target)
            if problem:
                report.append(f"{uid}: REFUSING dispatch -- {problem}")
                dispatch_refusal = str(problem)
                continue

        unit_dir, err = SD._allocate(plan, u, root)
        if err:
            SS._set_unit_state(us, "FAILED")
            report.append(f"{uid}: {err}")
            SS.save_state(state_dir, state)
            continue
        # Persist the allocation BEFORE submitting: a crash between the two
        # must leave an orphaned directory, never an unrecorded job.
        us["attempt_dir"] = unit_dir
        # A dry attempt is TAGGED, so it can never be mistaken for real work
        # when counting the retry budget.
        us["attempts"].append(f"{SP.DRY_PREFIX}{unit_dir}" if dry_run
                              else unit_dir)
        allocated_at = time.time()
        us["allocated_at"] = allocated_at
        SS._set_unit_state(us, "ALLOCATED", allocated_at)
        # ACCUMULATE. Overwriting meant a unit preempted twice was charged
        # once, so retries could walk straight through a ceiling: budget 8,
        # three 4-hour attempts, 12 committed.
        us["gpu_hours"] = float(us.get("gpu_hours") or 0) + want
        spent += want
        # BEFORE `_submit`, and persisted by the save below it, because the
        # basis is only a basis if it predates everything that could write
        # into the write root. A digest taken after dispatch is a digest of
        # the run's own output.
        SE._capture_artifact_basis(state, uid, unit_dir, u,
                                enable_slurm_exit=u.get("kind") == "slurm")
        SS.save_state(state_dir, state)

        # B6, and the reason it sits HERE. The lease excludes a second
        # controller over this state directory; it says nothing about a
        # second state directory over this unit's output namespace, which is
        # exactly what three ad-hoc sub-plans of one project produce. A dry
        # run takes no claim, because it creates no writer for one to protect
        # against and a placeholder claim in a shared registry would refuse
        # honest work.
        claim_refusal, held_claims = (None, [])
        if not dry_run:
            claim_refusal, held_claims = SP._take_output_claims(
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
                SP._release_output_claims(u, root, state_dir)
            # Classified from what `_submit` OBSERVED, not from re-reading
            # the launch record. On a re-dispatch the record was written while
            # the workspace was clean and still says "passed", so reading it
            # back turned a refusal into a generic failure and charged a
            # retry for an attempt that never started.
            if isinstance(err, SP.PreflightRefusal):
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
                    if last in (str(unit_dir), f"{SP.DRY_PREFIX}{unit_dir}"):
                        us["attempts"].pop()
                us["attempt_dir"] = None
                SS._set_unit_state(us, "PREFLIGHT_REFUSED")
                us.pop("allocated_at", None)
                us["gpu_hours"] = max(
                    0.0, float(us.get("gpu_hours") or 0) - want)
                spent -= want
                cited = us["preflight_refusals"][-1]["receipt"]
                report.append(f"{uid}: {err}" +
                              (f"\n  receipt: {cited}" if cited else ""))
            else:
                SS._set_unit_state(us, "FAILED")
                report.append(f"{uid}: {err}")
            SS.save_state(state_dir, state)
            continue
        # Bind EVERY kind that has an id. The old guard bound only numeric
        # ids, so a code unit's agent id never reached unit.json and its
        # predicate could never see an agent at all. A dry run still has no
        # real id to bind.
        needs_bind = bool(
            job_id and not str(job_id).startswith(("dry-", "engine-")))
        us["job_id"] = str(job_id)
        SS._set_unit_state(us, "SUBMITTED")
        if needs_bind:
            us["bind_pending"] = True
        # Persist the binding authority before unit.py writes its marker.
        SS.save_state(state_dir, state)
        # Now the claim can name the job as well as the attempt. Best effort
        # and non-authoritative: the attempt id already names the scheduler
        # job (`swarm-<attempt>`), so a claim that never gets this update is
        # still adjudicable. It is written for the human reading a refusal.
        if held_claims:
            SP._note_claimed_attempt(u, root, state_dir, Path(unit_dir).name,
                                  job_id)
        berr = SD._bind(unit_dir, job_id) if needs_bind else None
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
        SS.save_state(state_dir, state)

    state["halted"] = halted
    SS.save_state(state_dir, state)

    # Emit tracker intents from the FINAL state of the advance, not at each
    # place a state happens to be set. Three sites used to emit, and they
    # missed every transition made later in the same pass: a unit that exited
    # 0 and wrote nothing reached FAILED through the settle branch and told
    # the tracker NOTHING, so its issue would have sat on "work started"
    # forever. Comparing before-and-after cannot miss a path, including paths
    # added later.
    project = plan.get("name") or "swarm"
    if not dry_run:
        SO.backfill_tracker_intents(state_dir, project, units)
    existing_outbox_keys = {
        rec.get("key") for rec in SO.read_outbox(state_dir)
        if isinstance(rec, dict)} if dry_run else set()
    for uid in sorted(units):
        us = SS._unit_state(state, uid)
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
            if SO.closing_evidence_for(kind) == "merged_pr":
                rp = us.get("merge_receipt")
                # Repair persisted state written by the old coordinator,
                # which recorded the merge commit and PR but discarded the
                # admitted receipt before the outbox could carry it.
                if not SO._has_bound_merge_evidence(
                        uid, us, {"receipt": rp} if rp else None) and not dry_run:
                    produced = SE.trusted_produced_head(
                        state, uid, us["attempt_dir"])
                    facts = SS.trusted_launch_facts(
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
                        recovered, _why = SE.admit_merge(
                            state_dir, uid, produced, **recover_args)
                    except OutboxError:
                        recovered = None
                    if (recovered
                            and recovered.get("merged_as") == us.get(
                                "merged_as")
                            and recovered.get("pr") == us.get("merge_pr")):
                        us["merge_receipt"] = dict(recovered)
                        SS.save_state(state_dir, state)
                        rp = recovered
            else:
                rp, _why = SE.attested_receipt(
                    state, uid, us["attempt_dir"])
            evidence = {"receipt": rp} if rp else None
        if dry_run:
            action = SO.TRACKER_EVENTS.get(now)
            if action:
                verb = action[0]
                if (verb == "close"
                        and SO.closing_evidence_for(kind) == "merged_pr"
                        and not SO._has_bound_merge_evidence(
                            uid, us, evidence)):
                    verb = "open_pr"
                key = SO._intent_key(
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
        SO.emit_intent(state_dir, project, uid, now, us, evidence, kind=kind,
                    tracker=units[uid].get("tracker"))
    return report, dispatched, halted or dispatch_refusal


def _load_plan(path):
    plan, err = U.read_json(path)
    if err:
        sys.exit(f"error: no readable plan at {path}: {err}")
    # Dispatch validates against the survey too, not just `validate`. A
    # refusal that only fires when someone runs the optional command is a
    # refusal that fires after the DAG is live.
    survey, _note = SPR.discover_survey(path)
    try:
        validate_plan(plan, survey, new_attempts=False)
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
        us = SS._unit_state(state, uid)
        attempt_dir = us.get("attempt_dir")
        job_id = us.get("job_id")
        if (not attempt_dir or not job_id or us.get("state") not in SS.LIVE_STATES
                or SE.trusted_produced_head(state, uid, attempt_dir)):
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
        SS.save_state(args.state_dir, state)
        try:
            log = open(log_path, "ab")
        except OSError as exc:
            watch.update({"status": "start_failed", "reason": str(exc),
                          "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            SS.save_state(args.state_dir, state)
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
            SS.save_state(args.state_dir, state)
            report.append(f"{uid}: could not start terminal watcher: {exc}. "
                          "Scheduled advance remains the fallback.")
            continue
        log.close()
        watch["pid"] = proc.pid
        watch["status"] = "waiting"
        SS.save_state(args.state_dir, state)
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
    SR.apply_agent_resolution(args.plan)
    plan, err = U.read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    explicit = getattr(args, "survey", None)
    if explicit:
        # NAMED, so unreadable is an error rather than a shrug. Degrading a
        # survey the operator pointed at into "unknown" would silently drop
        # every check they asked for.
        survey, serr = SPR.read_survey(explicit)
        if serr:
            sys.exit(f"error: no readable survey at {explicit}: {serr}")
        note = explicit
    else:
        survey, note = SPR.discover_survey(args.plan)
    try:
        summary = validate_plan(plan, survey)
    except PlanError as e:
        sys.exit(f"error: invalid plan: {e}")
    if getattr(args, "check_seeds", False):
        problems = []
        for u in plan["units"]:
            if "seed" not in u:
                continue
            target, problem = SP._resolve_dispatch_target(u)
            if not problem:
                source, problem = SP._dispatch_source_identity(u, target)
            if not problem:
                problem = SP._seed_reachability_problem(
                    u["seed"], source["repo"], source["repository_remote_raw"],
                    source["repository_remote"])
            if problem:
                # Render one diagnostic line per unit, after the decision.
                detail = " ".join(problem.splitlines())
                problems.append(f"unit {u['id']!r}: {detail}")
        if problems:
            sys.exit("error: invalid plan:\n" + "\n".join(problems))
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
        known = sorted(SPR._known_partitions() or [])
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
    SR.apply_agent_resolution(args.plan)
    plan = _load_plan(args.plan)
    _prepare_command_paths(args, plan=plan, need_root=True)
    # ONE WRITER. Two schedulers firing at once, or a human running `advance`
    # while cron does, would both read old state and submit the same unit.
    ok, holder = SS.acquire_lease(args.state_dir)
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
        state = SS.load_state(args.state_dir)
        if (getattr(args, "terminal_watch", False)
                and _current_code_terminal_watch(
                    state, args.unit, args.attempt, args.agent) is None):
            print("obsolete terminal watcher: attempt/agent no longer current; no check run")
            return EXIT_OK
        watch_report = []
        if (not args.dry_run
                and _refresh_code_terminal_watches(state, args.state_dir, watch_report)):
            SS.save_state(args.state_dir, state)
        report, dispatched, halted = advance(
            plan, state, args.state_dir, args.root, args.dry_run,
            args.max_new_dispatches,
            accept_plan_change=getattr(args, "accept_plan_change", False))
        report[:0] = watch_report
        if (not args.dry_run
                and _start_code_terminal_watchers(plan, state, args, report)):
            SS.save_state(args.state_dir, state)
    finally:
        SS.release_lease(args.state_dir)
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
                    SJ._fsync_directory(path.parent)
                    return None
        created = not path.exists()
        with path.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if created:
            # fsync(contents) does not make a newly-created filename durable.
            # Completion may be saved only after the parent records the entry.
            SJ._fsync_directory(path.parent)
        return None
    except OSError as exc:
        return str(exc)


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
    dest_root, derr = SPR.resolve_promote_to(u.get("promote_to"),
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
    receipt, err = SE.attested_receipt(state, uid, attempt)
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
    SS.save_state(state_dir, state)

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
            SJ._fsync_directory(created.parent)
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
        SJ._fsync_directory(version.parent)
        tmp_link = dest / f".current-{os.getpid()}"
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        os.symlink(Path(attempt).name, tmp_link)
        os.replace(tmp_link, current)         # atomic pointer swap
        SJ._fsync_directory(current.parent)
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
    SS.save_state(state_dir, state)
    lines += ["", f"PROMOTED {uid} -> {version}", f"approved by {approver}"]
    return lines, True


def cmd_promote(args):
    plan, err = U.read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    _prepare_command_paths(args, plan=plan)
    state = SS.load_state(args.state_dir)
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
     "default is agents.json default.provider"),
    ("model", "code", "optional", "overrides the provider's default"),
    ("thinking", "code", "optional",
     "default from agents.json. JSON null or \"\" suppresses the flag; the STRING "
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
        patterns = SPR.declared_scope(u)
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
        problem = SCL._code_launch_intent_problem(intent, u, attempt)
        if problem:
            raise PlanError(problem)
        base, repo = intent["base_commit"], intent["repo"]
        head = SE.trusted_produced_head(state, args.unit, attempt_dir)
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


def _integration_verification_fence(state_dir, unit, receipt=None):
    """Check coordinator merge intents under the lease, optionally publishing.

    The generic verifier does not interpret resolution chains. Once this unit
    has a merge intent, use the connected operator to resolve it and verify.
    Check both before execution and at publication, never holding the lease
    across the verifier. No connected/network module enters the coordinator.
    """
    ok, holder = SS.acquire_lease(state_dir)
    if not ok:
        return "integration verification cannot acquire coordinator lock: " + str(holder)
    try:
        for path in sorted(Path(state_dir).glob("merge-unit-*.json")):
            intent, error = U.read_json(path)
            if (error or not isinstance(intent, dict)
                    or not isinstance(intent.get("binding"), dict)
                    or not isinstance(intent["binding"].get("unit"), str)):
                return "integration verification cannot read merge intent: " + str(path)
            if intent["binding"]["unit"] == unit:
                return "integration verification refused: merge intent exists for unit " + unit
        if receipt is not None:
            SE.load_verifications(state_dir)
            SJ._fsync_append(Path(state_dir) / SE.VERIFY_RECEIPTS, receipt)
    except (OSError, OutboxError) as exc:
        return "integration verification publication refused: " + str(exc)
    finally:
        SS.release_lease(state_dir)
    return None


def cmd_verify(args):
    """Run an authorized, content-pinned verifier and record what it said.

    Everything that makes this admissible happens HERE, in one place, and the
    receipt records all of it: the policy came from the anchored base commit,
    the verifier's bytes hashed to what the policy authorized, those exact
    bytes ran, and the head it ran against is the one this attempt produced.
    """
    _prepare_command_paths(args)
    if args.claim == V.INTEGRATION_CLAIM:
        problem = _integration_verification_fence(args.state_dir, args.unit)
        if problem:
            sys.stderr.write(f"error: {problem}\n")
            return EXIT_CONFLICT
    state = SS.load_state(args.state_dir) or {}
    launch_facts = SS.trusted_launch_facts(
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
        SE.load_verifications(args.state_dir)
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
    produced = SE.trusted_produced_head(state, args.unit, args.attempt)
    if not produced:
        sys.stderr.write(
            f"error: coordinator state records no judged produced commit for "
            f"attempt {Path(args.attempt).name!r}. Re-run that attempt; do "
            f"not recover a basis from another attempt, its receipt, or the "
            f"current branch.\n")
        return EXIT_USAGE
    runner = U.run
    executables = None
    if args.claim == V.INTEGRATION_CLAIM:
        try:
            execution_policy, execution_digest = V.RV.read_policy(args.state_dir)
            if execution_policy.get("verification_host"):
                sys.stderr.write("error: remote merge verification requires merge_unit.py --verify-integration\n")
                return EXIT_USAGE
            for executable in execution_policy.get("local", {}).values():
                if CP._inside(executable, repo):
                    sys.stderr.write("error: verification executable is inside the operated repository\n")
                    return EXIT_USAGE
            executables = V.RV.resolve_executables(execution_policy.get("local"), excluded_roots=(repo,))
            runner = V.RV.GitRunner(U.run, executables)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            sys.stderr.write("error: verification executables unavailable: {}\n".format(exc))
            return EXIT_USAGE
    basis_problem = W.validate_pinned_head(runner, launch_facts, produced)
    if basis_problem:
        sys.stderr.write(f"error: {basis_problem}\n")
        return EXIT_USAGE

    policy, policy_digest, perr = V.read_policy(runner, repo, base)
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
        runner, repo, base, produced, entry)
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
            runner, repo, produced, target_commit, args.path, digest,
            args=args.arg, timeout=args.timeout, executables=executables,
            state_dir=args.state_dir, unit=args.unit,
            journal_entries=SE.load_verifications(args.state_dir)[0])
        if V.RV.read_policy(args.state_dir)[1] != execution_digest:
            sys.stderr.write("error: execution policy changed during verification\n")
            return EXIT_CONFLICT
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
           **V.outcome_result(outcome),
           "exit_code": outcome["exit_code"],
           "stdout_tail": outcome["stdout"], "stderr_tail": outcome["stderr"],
           "by": os.environ.get("USER") or "?",
           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "schema_version": 1}
    rec.update(corpus_evidence)
    rec.update(merge_evidence)
    if outcome.get("execution") is not None:
        rec.update(execution=outcome["execution"], schema_version=2)
    bad = SE._verify_shape_problem(rec)
    if bad:
        sys.stderr.write(f"error: this would not be admissible: {bad}\n")
        return EXIT_USAGE
    if args.claim == V.INTEGRATION_CLAIM:
        problem = _integration_verification_fence(args.state_dir, args.unit, rec)
        if problem:
            sys.stderr.write(f"error: {problem}\n")
            return EXIT_CONFLICT
    else:
        SJ._fsync_append(Path(args.state_dir) / SE.VERIFY_RECEIPTS, rec)
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
        SE.load_merge_receipts(args.state_dir)     # refuse to extend a bad journal
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
    bad = SE._merge_shape_problem(rec)
    if bad:
        sys.stderr.write(f"error: this would not be admissible: {bad}\n")
        return EXIT_USAGE
    SJ._fsync_append(Path(args.state_dir) / SE.MERGE_RECEIPTS, rec)
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
    intents = SO.read_outbox(args.state_dir)

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
        rec = SO.record_receipt(args.state_dir, args.record_receipt, args.ref,
                             op=args.op)
        print(f"  recorded: {rec['key']} -> {rec['ref']}")
        if SO._receipt_grade(rec) == SO.ATTESTED_UNSPECIFIED:
            print("  grade: attested (compatible legacy form; "
                  "no read-back basis was supplied)")
        return EXIT_OK

    status, problems = SO.acknowledgment_status(args.state_dir)

    # FAIL CLOSED. Corruption is not the same as an interrupted tail: a
    # truncated last line is a local write that did not finish, and repeating
    # reconciliation fixes it. A bad line in the middle means the journal
    # cannot be read in
    # full, so no status derived from it can be trusted, including the
    # comfortable ones.
    for i in intents:
        st, rs = status.get(i.get("key"), (SO.UNACKNOWLEDGED, []))
        i["ack_status"] = st
        i["ack_refs"] = sorted({r.get("ref") for r in rs})

    unack = [i for i in intents if i["ack_status"] == SO.UNACKNOWLEDGED]
    conflicts = [i for i in intents if i["ack_status"] == SO.CONFLICT]

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
        label = {SO.ATTESTED_UNSPECIFIED: "attested",
                 SO.ATTESTED_CONFIRMED: "confirmed",
                 SO.CONFLICT: "CONFLICT",
                 SO.UNACKNOWLEDGED: "unack"}[i["ack_status"]]
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
    units = {u["id"]: u for u in SPR._units_with_canary(plan)}
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
                SS._deadline_host_evaluation(state, uid, us)
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
    state = SS.load_state(args.state_dir)
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
    v.add_argument("--check-seeds", action="store_true",
                   help="check every declared seed against its dispatch "
                        "source and origin push destination using Git; "
                        "does not allocate attempts or write state")
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
    m.add_argument("--method", required=True, choices=SE.MERGE_METHODS)
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
    o.add_argument("--source", choices=sorted(SO.RECEIPT_CONFIRMING_SOURCES),
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


# Wildcard imports enumerate __all__, not names served only by __getattr__.
# Build this after every definition to retain the original public namespace.
__all__ = [name for name in globals() if not name.startswith("_")] + [
    "AGENTS_FILE", "DEFAULT_AGENT_PROVIDER", "DEFAULT_AGENT_THINKING",
    "THINKING_BY_MODEL", "PINNED_AGENT_PROVIDER", "PINNED_THINKING_BY_MODEL",
    "acquire_lease", "renew_lease", "release_lease", "load_state",
    "save_state",
    "trusted_launch_facts",
    "trusted_launch_host_anchor",
    "LIVE_STATES",
]


if __name__ == "__main__":
    main()
