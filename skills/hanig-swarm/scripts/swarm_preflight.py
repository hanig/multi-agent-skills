"""Workspace preflight and output claims. Python 3.8+, stdlib only."""

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import unit as U
import worktree as W
import swarm_routing as SR
import swarm_plan_rules as SPR

# Shared with coordinator dry-attempt bookkeeping.
DRY_PREFIX = "dry-attempt-"


def _git(repo, *args, timeout=60):
    rc, out, err = U.run(["git", "-C", str(repo)] + list(args), timeout=timeout)
    return rc, (out or "").strip(), (err or "").strip()


def _unattended_mode_example(provider):
    """An unattended mode this provider actually accepts.

    Guidance only. The coordinator does not police the valid set: a provider
    adds modes faster than a hard-coded list is updated, and refusing a value
    that became valid is worse than passing one through to a loud rejection.
    """
    name = str(provider or SR.DEFAULT_AGENT_PROVIDER).split("/", 1)[0].lower()
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
        dest, derr = SPR.resolve_promote_to(promote, root)
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
