"""Workspace preflight and output claims. Python 3.8+, stdlib only."""

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
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
            agents = json.loads(out)
        except (ValueError, TypeError):
            return CLAIM_UNKNOWN, "`paseo ls --json` did not return JSON"
        if (not isinstance(agents, list)
                or any(not isinstance(a, dict) or not isinstance(a.get("name"), str)
                       for a in agents)):
            return CLAIM_UNKNOWN, "`paseo ls --json` did not return an agent list"
        for a in agents:
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
