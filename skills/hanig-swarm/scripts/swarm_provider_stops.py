"""Coordinator-owned provider-stop retention, independent of resume policy.

These records prevent cleanup; they confer no completion or resume authority.
Publication and release are separate operator work, not implemented here.
"""
import time
from pathlib import Path

import swarm_state as SS
import unit as U
import worktree as W


CONTENT_FILTER = "flagged for possible cybersecurity risk"


def retained(us, attempt_dir):
    if not attempt_dir:
        return None
    record = (us.get("attempt_provider_stops") or {}).get(Path(attempt_dir).name)
    return record if record and record.get("phase") == "retained" else None


def report_retained(state, report, uid=None, attempt=None):
    """Include old attempts and operator-terminal units, once per report."""
    for unit, us in sorted((state.get("units") or {}).items()):
        if uid is not None and unit != uid:
            continue
        for aid in sorted(us.get("attempt_provider_stops") or {}):
            record = retained(us, aid)
            if not record or (attempt is not None and aid != attempt):
                continue
            line = (f"{unit}: retained provider stop {aid}; worktree "
                    f"{record['worktree_path']}; required recovery ref "
                    f"{record['recovery_ref']}")
            if record.get("retention_problem"):
                line += f"; reconciliation needed: {record['retention_problem']}"
            if line not in report:
                report.append(line)


def record_stop(state_dir, state, uid, us, reason, message_class, report):
    """Pin the binding from coordinator state even if the checkout is lost."""
    attempt = Path(us["attempt_dir"]).name
    if retained(us, attempt):
        report_retained(state, report, uid, attempt)
        return
    intent = (us.get("attempt_launch_intents") or {}).get(attempt) or {}
    meta = (us.get("attempt_workspaces") or {}).get(attempt) or {}
    facts = SS.trusted_launch_facts(state, uid, attempt)
    problem = (W.workspace_identity_problem(U.run, facts) if facts
               else "missing or inconsistent coordinator launch facts")
    head = None
    if not problem:
        if (meta.get("path") != facts["execution_workspace"]
                or intent.get("branch") != facts["branch"]):
            problem = "coordinator worktree binding disagrees with launch facts"
        else:
            rc, out, _ = U.run(["git", "-C", meta["path"], "rev-parse",
                                "--verify", "HEAD^{commit}"], timeout=30)
            if rc:
                problem = "cannot read retained worktree HEAD"
            else:
                head = out.strip()
    record = {"schema_version": 1, "reason": reason,
              "message_class": message_class,
              "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
              "branch": intent.get("branch"), "worktree_path": meta.get("path"),
              "recovery_ref": f"refs/heads/recovery/{uid}-{attempt}",
              "head_at_retention": head, "phase": "retained"}
    if problem:
        record["retention_problem"] = problem
    us.setdefault("attempt_provider_stops", {})[attempt] = record
    # Persist the failed outcome with the obligation, so a restart cannot
    # re-enter recovery or classify the observation again.
    SS._set_unit_state(us, "FAILED")
    SS.save_state(state_dir, state)
    report_retained(state, report, uid, attempt)
