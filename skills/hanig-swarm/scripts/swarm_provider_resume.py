"""Bounded provider resumes."""
import os
import time
from pathlib import Path

import paseo_io as PIO
import recovery as R
import swarm_evidence as SE
import swarm_state as SS
import unit as U
import worktree as W


RETRYABLE_PROVIDER_MESSAGES = (
    "is at capacity", "rate limit", "overloaded",
    "502", "503", "504", "temporarily unavailable",
)
RESUME_PROMPT = (
    "Provider interruption. Continue in SAME attempt/worktree. Committed/pushed "
    "work is intact; preserve uncommitted work, current review round number, "
    "dispositions and interrupted reservations/results. Resume is NOT a review "
    "round: do not reset ledger, replay seed or start an attempt. Keep completion protocol."
)


def _utc(now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _save(state_dir, state, uid, us):
    SE._record_receipt_provenance(state, uid, us["attempt_dir"], None)
    SS.save_state(state_dir, state)


def _eligible(agent):
    """Read the current error."""
    rc, out, _ = U.run(["paseo", "inspect", str(agent), "--json"], timeout=60)
    rec = PIO.first_json_object(out) if rc == 0 else None
    if (not rec or str(rec.get("Status") or rec.get("status")).lower() != "error"
            or rec.get("PendingPermissions") or rec.get("pendingPermissions")):
        return None
    rc, out, _ = U.run(["paseo", "logs", str(agent), "--tail", "1"], timeout=60)
    prefix = "[System Error] "
    if rc or not out.startswith(prefix):
        return None
    message = out[len(prefix):].strip()
    payload = PIO.first_json_object(message)
    if payload:
        error = payload.get("error", payload)
        message = error.get("message", "") if isinstance(error, dict) else error
    if not isinstance(message, str):
        return None
    message = message.lower()
    if "flagged for possible cybersecurity risk" in message:
        return None
    return next((s for s in RETRYABLE_PROVIDER_MESSAGES if s in message), None)


def _preserve(state, uid, us, state_dir):
    attempt = Path(us["attempt_dir"]).name
    facts = SS.trusted_launch_facts(state, uid, attempt)
    if not facts:
        return None, "missing launch facts"
    problem = W.workspace_identity_problem(U.run, facts)
    if problem:
        return None, problem
    identity = facts["workspace_identity"]
    return R.preserve_worktree(facts["execution_workspace"], Path(state_dir) / "recovery-snapshots",
        uid, attempt, facts["base_commit"], facts["base_tree"], expected_git_dir=identity.get("git_dir"),
        expected_git_pointer_sha256=identity.get("git_pointer_sha256"))


def maybe_resume(state_dir, uid, u, us, report, state):
    limit = (u.get("provider_resume") or {}).get("max", 0)
    if not limit or u.get("kind") != "code" or not us.get("job_id"):
        return False
    if SS._deadline_exceeded(u, us, state, uid):
        return False
    matched = _eligible(us["job_id"])
    if not matched:
        return False
    now = time.time()
    attempt = Path(us["attempt_dir"]).name
    records = us.setdefault("provider_resumes", {})
    entry = records.get(attempt)
    if entry is None:
        entry = {"fingerprint": {"message_class": matched, "observed_at": _utc(now)},
                 "resumes_attempted": 0, "next_not_before": now + 300, "snapshot_paths": []}
        records[attempt] = entry
    if entry["resumes_attempted"] >= limit:
        report.append(f"{uid}: spent {entry['resumes_attempted']}/{limit} provider resumes")
        return False
    if entry["next_not_before"] is None:
        report.append(f"{uid}: resume backoff unknown; slot retained")
        return False
    if now < entry["next_not_before"]:
        _save(state_dir, state, uid, us)
        if SS._deadline_exceeded(u, us, state, uid):
            return False
        report.append(f"{uid}: waiting for backoff; provider error observed "
                      f"{entry['fingerprint']['observed_at']}")
        return True
    snapshot, problem = _preserve(state, uid, us, state_dir)
    if problem:
        report.append(f"{uid}: snapshot failed: {problem}")
        return False
    entry["snapshot_paths"].append(snapshot["snapshot_path"])
    entry["resumes_attempted"] += 1
    # Unknown backoff fails closed.
    entry["next_not_before"] = None
    _save(state_dir, state, uid, us)
    # Sync charge.
    with (Path(state_dir) / SS.STATE_FILE).open("rb") as handle:
        os.fsync(handle.fileno())
    descriptor = os.open(str(state_dir), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    matched = _eligible(us["job_id"])
    if not matched or SS._deadline_exceeded(u, us, state, uid):
        return False
    entry["fingerprint"] = {"message_class": matched, "observed_at": _utc(time.time())}
    rc, out, err = U.run(["paseo", "send", str(us["job_id"]), "--no-wait", RESUME_PROMPT], timeout=120)
    entry["next_not_before"] = time.time() + 900
    report.append(f"{uid}: resumed {entry['resumes_attempted']}/{limit} after provider error; "
                  f"observed {entry['fingerprint']['observed_at']} "
                  "(delivery unconfirmed, not progress)")
    if rc:
        report.append(f"{uid}: provider resume send failed: {err or out}; slot remains charged")
        return False
    SS._set_unit_state(us, "RUNNING")
    us.pop("incomplete_since", None)
    _save(state_dir, state, uid, us)
    return True


def status(us, u):
    entry = (us.get("provider_resumes") or {}).get(Path(us.get("attempt_dir") or "").name)
    if not entry:
        return None
    return {"resumed": entry["resumes_attempted"], "max": (u.get("provider_resume") or {}).get("max", 0),
            "observed_at": entry["fingerprint"]["observed_at"]}
