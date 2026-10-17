"""Bounded provider-error turns inside an existing code attempt (no judging)."""
import time
from pathlib import Path

import paseo_io as PIO
import recovery as R
import swarm_evidence as SE
import swarm_preflight as SP
import swarm_state as SS
import unit as U
import worktree as W


RETRYABLE_PROVIDER_MESSAGES = (
    "is at capacity", "rate limit", "Rate limit", "overloaded", "Overloaded",
    "502", "503", "504", "temporarily unavailable",
)
RESUME_PROMPT = (
    "Your session was interrupted by a transient provider error. Your committed "
    "and pushed work is intact in the SAME attempt and worktree. Continue from "
    "where you stopped, including the current review round number and its "
    "dispositions in your session and gate ledger. This resume is NOT a review "
    "round: do not reset the round counter, replay the seed, or start a new "
    "attempt. Preserve any uncommitted work. If a review was interrupted, inspect "
    "its existing reservation and results before proceeding; never erase or "
    "recreate the ledger to rerun round 1. Keep the original completion protocol."
)


def _utc(now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _save(state_dir, state, uid, us):
    # A past error receipt must not outrank the retained/resumed attempt.
    SE._record_receipt_provenance(state, uid, us["attempt_dir"], None)
    SS.save_state(state_dir, state)


def pending(us):
    return next((e for e in us.get("provider_resumes", [])
                 if e["status"] == "pending"), None)


def _observation(agent):
    """Read status and only the last System Error, never conversation text."""
    rc, out, err = U.run(["paseo", "inspect", str(agent), "--json"], timeout=60)
    rec = PIO.first_json_object(out) if rc == 0 else None
    if not rec:
        return None, err or out or "Paseo observation unavailable"
    status = rec.get("Status", rec.get("status"))
    observation = {"status": status,
                   "updated_at": rec.get("UpdatedAt", rec.get("updatedAt")),
                   "message": ""}
    if status != "error" or rec.get("PendingPermissions", rec.get("pendingPermissions")):
        return observation, None
    rc, out, err = U.run(["paseo", "logs", str(agent), "--tail", "1"], timeout=60)
    if rc:
        return None, err or out or "Paseo error log unavailable"
    prefix = "[System Error] "
    if out.startswith(prefix):
        message = out[len(prefix):].strip()
        payload = PIO.first_json_object(message)
        if payload:
            error = payload.get("error", payload)
            message = error.get("message", "") if isinstance(error, dict) else error
        observation["message"] = message if isinstance(message, str) else ""
    return observation, None


def _preserve(state, uid, us, state_dir, entry):
    """Require the anchored checkout; snapshot dirty bytes before sending."""
    attempt = Path(us["attempt_dir"]).name
    facts = SS.trusted_launch_facts(state, uid, attempt)
    if not facts:
        return "coordinator has no launch facts for this attempt"
    problem = W.workspace_identity_problem(U.run, facts)
    if problem:
        return problem
    workspace = facts["execution_workspace"]
    rc, dirty, err = SP._git(workspace, "status", "--porcelain", "--untracked-files=all", "--ignored")
    if rc:
        return err or "cannot inspect attempt worktree"
    if dirty:
        identity = facts["workspace_identity"]
        snapshot, problem = R.preserve_worktree(
            workspace, Path(state_dir) / "recovery-snapshots", uid, attempt,
            facts["base_commit"], facts["base_tree"],
            expected_git_dir=identity.get("git_dir"),
            expected_git_pointer_sha256=identity.get("git_pointer_sha256"))
        if problem:
            return problem
        entry["recovery_snapshot"] = snapshot
        _save(state_dir, state, uid, us)
    return None


def maybe_resume(state_dir, uid, u, us, report, state):
    """Retain waiting/resumed attempts. Pending sends survive crashes.

    Paseo has no idempotency key: a crash after send may duplicate delivery.
    """
    limit = (u.get("provider_resume") or {}).get("max", 0)
    if not limit or u.get("kind") != "code" or not us.get("job_id"):
        return False
    entry = pending(us)
    observed, problem = _observation(us["job_id"])
    if problem:
        _save(state_dir, state, uid, us)
        report.append(f"{uid}: provider resume observation unavailable: {problem}; retry next advance")
        return True
    if observed["status"] != "error":
        if observed["status"] not in ("running", "idle", "done", "complete", "completed"):
            return False
        # Reconcile an accepted send after a crash.
        if entry and entry.get("sending_at"):
            entry.update(status="sent", sent_at=entry["sending_at"])
            _save(state_dir, state, uid, us)
        elif entry:
            us["provider_resumes"].remove(entry)
            _save(state_dir, state, uid, us)
        # Let the next check judge this newer lifecycle.
        _save(state_dir, state, uid, us)
        return True
    message = observed["message"]
    matched = next((s for s in RETRYABLE_PROVIDER_MESSAGES if s in message), None)
    if "flagged for possible cybersecurity risk" in message or not matched:
        return False
    now = time.time()
    if (entry and entry.get("sending_at") and observed["updated_at"]
            and observed["updated_at"] != entry["observation"]["updated_at"]):
        entry.update(status="sent", sent_at=entry["sending_at"])
        _save(state_dir, state, uid, us)
        entry = None
    entries = us.get("provider_resumes", [])
    if entry is None:
        # Do not spend another slot on an old asynchronous observation.
        if entries and entries[-1]["observation"] == observed:
            _save(state_dir, state, uid, us)
            report.append(f"{uid}: waiting for a new observation after provider resume")
            return True
        if len(entries) >= limit:
            report.append(f"{uid}: spent {len(entries)}/{limit} provider resumes; next error fails")
            return False
        entry = {"n": len(entries) + 1, "of": limit, "agent_id": us["job_id"],
                 "attempt": Path(us["attempt_dir"]).name,
                 "message": message, "matched": matched, "observation": observed,
                 "at": _utc(now), "observed_epoch": now, "status": "pending"}
        us.setdefault("provider_resumes", []).append(entry)
        _save(state_dir, state, uid, us)
    _save(state_dir, state, uid, us)
    delay = 300 if entry["n"] == 1 else 900
    if now < entry["observed_epoch"] + delay:
        report.append(f"{uid}: waiting for backoff for provider resume {entry['n']}/{entry['of']}; "
                      f"error observed {entry['at']}")
        return True
    problem = _preserve(state, uid, us, state_dir, entry)
    if problem:
        report.append(f"{uid}: provider resume retained without send: {problem}; "
                      "restore the anchored worktree or repair preservation, then advance")
        return True
    entry["sending_at"] = _utc(now)
    _save(state_dir, state, uid, us)
    rc, out, err = U.run(["paseo", "send", str(us["job_id"]), "--no-wait", RESUME_PROMPT], timeout=120)
    if rc:
        entry["send_error"] = err or out
        report.append(f"{uid}: provider resume send failed: {err or out}; retry same entry next advance")
    else:
        entry.update(status="sent", sent_at=_utc(time.time()))
        entry.pop("send_error", None)
        SS._set_unit_state(us, "RUNNING")
        us.pop("incomplete_since", None)
        report.append(f"{uid}: resumed {entry['n']}/{entry['of']} after provider error; "
                      f"observed {entry['sent_at']} (delivery, not progress)")
    _save(state_dir, state, uid, us)
    return True


def status(us):
    entries = us.get("provider_resumes") or []
    if not entries:
        return None
    sent = [e for e in entries if e["status"] == "sent"]
    last = entries[-1]
    return {"resumed": len(sent), "max": last["of"],
            "observed_at": sent[-1]["sent_at"] if sent else last["at"],
            "pending": last["status"] == "pending"}
