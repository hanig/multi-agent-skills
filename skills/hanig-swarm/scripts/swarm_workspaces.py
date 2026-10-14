"""Workspace recovery, preservation, and archive operations.

The coordinator imports this module; dependencies never point back to swarm.
"""

import json
import os
import time
from pathlib import Path

import codex_home as CH
import paseo_io as PIO
import recovery as R
import swarm_code_launch as SCL
import swarm_state as SS
import unit as U
import worktree as W


WORKTREE_ARCHIVE_MAX_ATTEMPTS = 3


def _paseo_json(out):
    """One implementation, in paseo_io. See that module for why."""
    return PIO.first_json_object(out)


def _paseo_error(out, err):
    """paseo's own diagnostics are good and specific: an invalid mode comes
    back naming every mode the provider accepts. Truncating the combined
    stream at 200 chars threw that away and left the workspace notice, which
    tells the operator nothing."""
    rec = _paseo_json(out) or _paseo_json(err)
    if isinstance(rec, dict) and isinstance(rec.get("error"), dict):
        msg = rec["error"].get("message") or rec["error"].get("code")
        if msg:
            return str(msg)[:400]
    text = "\n".join(l for l in ((err or "") + "\n" + (out or "")).splitlines()
                      if l.strip() and not l.startswith(("Created workspace",
                                                         "Tip:")))
    return text.strip()[:400] or "no diagnostic"


def _paseo_workspace_records():
    """Return Paseo's workspace registry, or ``None`` when unreadable."""
    rc, out, _err = U.run(["paseo", "workspace", "ls", "--json"], timeout=60)
    if rc != 0:
        return None
    try:
        records = json.loads(out or "[]")
        return records if isinstance(records, list) else None
    except (ValueError, TypeError):
        return None


def _paseo_workspace_for_path(path):
    """Return Paseo's workspace id for an exact cwd, if it can be listed."""
    records = _paseo_workspace_records()
    if records is None:
        return None
    try:
        wanted = str(Path(path).resolve())
        for rec in records:
            cwd = rec.get("cwd") or rec.get("Cwd")
            if cwd and str(Path(cwd).resolve()) == wanted:
                return rec.get("workspaceId") or rec.get("WorkspaceId")
    except (TypeError, AttributeError):
        return None
    return None


def _paseo_path_ownership_problem(path):
    """Refuse attaching a new agent to a path Paseo already owns.

    Git identity establishes what checkout is at the path; it does not
    establish that an existing Paseo workspace or agent belongs to this
    attempt. Registry unavailability is therefore uncertainty, not absence.

    KNOWN LIMIT: Paseo has no conditional reservation primitive for an
    already-existing checkout. These list calls reduce accidental adoption;
    they do not close the list-then-run race against a same-UID process that
    registers this path after both observations. Re-checking would only move
    the gap. Closing it requires Paseo to atomically reserve-and-launch, or a
    different OS identity/isolation boundary.
    """
    wanted = str(Path(path).resolve())
    workspaces = _paseo_workspace_records()
    if workspaces is None:
        return ("Paseo's workspace registry is unavailable, so ownership of "
                "the path cannot be established; retry when it is readable")
    for rec in workspaces:
        if not isinstance(rec, dict):
            continue
        cwd = rec.get("cwd") or rec.get("Cwd")
        try:
            matches = bool(cwd and str(Path(cwd).resolve()) == wanted)
        except (OSError, TypeError):
            matches = False
        if matches:
            workspace_id = (rec.get("workspaceId")
                            or rec.get("WorkspaceId") or "unknown")
            return (f"Paseo already owns that path as workspace "
                    f"{workspace_id}; refusing to adopt it")

    rc, out, _err = U.run(["paseo", "ls", "--json"], timeout=60)
    if rc != 0:
        return ("Paseo's agent registry is unavailable, so ownership of the "
                "path cannot be established; retry when it is readable")
    try:
        agents = json.loads(out or "[]")
    except (ValueError, TypeError):
        agents = None
    if not isinstance(agents, list):
        return ("Paseo's agent registry is unreadable, so ownership of the "
                "path cannot be established; retry when it is readable")
    for rec in agents:
        if not isinstance(rec, dict):
            continue
        cwd = rec.get("cwd") or rec.get("Cwd")
        try:
            matches = bool(cwd and str(Path(cwd).resolve()) == wanted)
        except (OSError, TypeError):
            matches = False
        if matches:
            agent_id = rec.get("id") or rec.get("agentId") or "unknown"
            return (f"Paseo already has agent {agent_id} at that path; "
                    f"refusing to adopt it")
    return None


def _registered_attempt_workspace(intent):
    """Return all registry entries that claim this exact attempt."""
    records = _paseo_workspace_records()
    if records is None:
        return None
    matches = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        name = rec.get("name") or rec.get("Name")
        isolation = rec.get("isolation") or rec.get("Isolation")
        cwd = rec.get("cwd") or rec.get("Cwd")
        # Paseo records the branch as the workspace name and uses the slug as
        # the managed cwd's final component. Bind both independently-recorded
        # values to the launch intent before considering a title-matched agent.
        if (name == intent["branch"] and isolation == "worktree" and cwd
                and Path(cwd).name == intent["worktree_slug"]):
            matches.append(rec)
    return matches


def _recover_code_launch(state, u, unit_dir, agent):
    """Finish a launch snapshot after a crash between Paseo run and bind."""
    rc, out, err = U.run(["paseo", "inspect", str(agent), "--json"], timeout=60)
    if rc != 0:
        return f"cannot inspect recovered agent {agent}: {_paseo_error(out, err)}"
    rec = _paseo_json(out) or {}
    workspace = rec.get("Cwd") or rec.get("cwd")
    if not workspace:
        return f"recovered agent {agent} reports no cwd"
    attempt = Path(unit_dir).name
    intent = (((state.get("units") or {}).get(u["id"]) or {})
              .get("attempt_launch_intents") or {}).get(attempt)
    problem = SCL._code_launch_intent_problem(intent, u, attempt)
    if problem:
        return problem
    problem = W.launch_host_problem(intent)
    if problem:
        return problem
    try:
        agent_path = str(Path(workspace).resolve())
        agent_st = os.stat(agent_path)
    except OSError as exc:
        return f"cannot identify recovered attempt workspace: {exc}"
    agent_identity = (agent_st.st_dev, agent_st.st_ino)
    registered = _registered_attempt_workspace(intent)
    matching_registry = []
    if registered:
        for candidate in registered:
            registered_path = candidate.get("cwd") or candidate.get("Cwd")
            if not registered_path:
                continue
            try:
                registry_path = str(Path(registered_path).resolve())
                registry_st = os.stat(registry_path)
            except OSError:
                continue
            if (registry_st.st_dev, registry_st.st_ino) == agent_identity:
                matching_registry.append(candidate)
        # One registry claim is unambiguous evidence. If it identifies a
        # different inode, the title-matched agent is the wrong owner. Zero,
        # duplicate, or unreadable claims are merely unavailable evidence:
        # the trusted inode (when present) and Git intent checks below decide.
        if len(registered) == 1 and not matching_registry:
            candidate = registered[0]
            registered_path = candidate.get("cwd") or candidate.get("Cwd")
            return (f"recovered agent {agent} is attached to worktree "
                    f"{agent_path!r}, whose device/inode does not match "
                    f"Paseo's registered workspace for attempt {attempt!r} "
                    f"at {registered_path!r}")
    us = ((state.get("units") or {}).get(u["id"]) or {})
    known_facts = ((us.get("attempt_launch_facts") or {}).get(attempt) or {})
    known_meta = ((us.get("attempt_workspaces") or {}).get(attempt) or {})
    known_identity = (known_facts.get("workspace_identity")
                      or known_meta.get("workspace_identity"))
    if isinstance(known_identity, dict):
        expected = (known_identity.get("device"), known_identity.get("inode"))
        if expected != agent_identity:
            return (f"recovered agent {agent} does not own the recorded "
                    f"device/inode for attempt {attempt!r}")
    workspace_id = None
    if len(matching_registry) == 1:
        workspace_id = (matching_registry[0].get("workspaceId")
                        or matching_registry[0].get("WorkspaceId"))
    return SCL._complete_code_launch(
        state, u, unit_dir, agent_path, workspace_id=workspace_id,
        recovery=True)


WORKTREE_CLEANUP_SUMMARY_PREFIX = "NEEDS_HUMAN -- retained worktrees:"


def _report_retained_worktrees(state, report, dry_run=False):
    """Maintain one aggregate escalation for all exhausted cleanups."""
    report[:] = [line for line in report
                 if WORKTREE_CLEANUP_SUMMARY_PREFIX not in line]
    retained = []
    for uid, us in sorted((state.get("units") or {}).items()):
        for attempt, meta in sorted((us.get("attempt_workspaces") or {}).items()):
            if isinstance(meta, dict) and meta.get("cleanup_gave_up"):
                retained.append((uid, attempt, meta.get("path"),
                                 meta.get("workspace_id") or "unknown"))
    if not retained:
        return
    paths = "; ".join(
        f"{uid}/{attempt}={path!r} (Paseo {workspace_id})"
        for uid, attempt, path, workspace_id in retained)
    line = (
        f"{WORKTREE_CLEANUP_SUMMARY_PREFIX} {len(retained)} cleanup(s) "
        f"exhausted the {WORKTREE_ARCHIVE_MAX_ATTEMPTS}-attempt bound; "
        f"automatic retries stopped. Archive or deliberately remove: {paths}")
    if dry_run:
        line += " (DRY RUN observation only; no cleanup was attempted)"
    report.append(line)


def _report_would_archive_code_worktree(state, u, unit_dir, report):
    """Describe cleanup without charging a retry or contacting Paseo."""
    if u.get("kind") != "code" or not unit_dir:
        return
    attempt = Path(unit_dir).name
    us = ((state.get("units") or {}).get(u["id"]) or {})
    meta = (us.get("attempt_workspaces") or {}).get(attempt)
    if (not isinstance(meta, dict) or meta.get("archived")
            or meta.get("cleanup_gave_up")):
        return
    target = meta.get("workspace_id") or meta.get("path") or "unknown workspace"
    line = (f"{u['id']}: DRY RUN -- would preserve and clean worktree {target} "
            f"for attempt {attempt}; no cleanup retry was charged")
    if line not in report:
        report.append(line)


def _worktree_cleanup_failed(state, u, meta, report, detail):
    tries = int(meta.get("cleanup_attempts") or 0)
    if tries < WORKTREE_ARCHIVE_MAX_ATTEMPTS:
        report.append(
            f"{u['id']}: {detail}. Cleanup attempt {tries} of "
            f"{WORKTREE_ARCHIVE_MAX_ATTEMPTS} failed")
        return
    meta.pop("cleanup_pending", None)
    meta["cleanup_gave_up"] = True
    meta["cleanup_problem"] = detail
    _report_retained_worktrees(state, report)


def _cleanup_codex_home(state, u, attempt, report, state_dir):
    """Clean a terminal attempt's home using only coordinator state.

    Callers establish terminality (or retry a pending terminal cleanup).
    A missing workspace record needs no preservation; a recorded workspace
    must have passed preserve-then-archive before its home can be discarded.
    """
    us = SS._unit_state(state, u["id"])
    workspaces = us.get("attempt_workspaces") or {}
    if u.get("kind") == "code" and attempt in workspaces:
        workspace = workspaces[attempt]
        if not isinstance(workspace, dict) or not workspace.get("archived"):
            return
    meta = (us.get("attempt_codex_homes") or {}).get(attempt)
    if not isinstance(meta, dict) or meta.get("removed"):
        return
    meta["cleanup_pending"] = True
    meta["cleanup_attempts"] = int(meta.get("cleanup_attempts") or 0) + 1
    if state_dir is None:
        meta["cleanup_problem"] = "Codex home cleanup requires coordinator state_dir"
        report.append(f"{u['id']}: {meta['cleanup_problem']}")
        return
    SS.save_state(state_dir, state)
    try:
        CH.remove(state_dir, attempt, meta.get("path"), meta.get("identity"))
    except Exception as exc:
        # Cleanup failures must not block independent dispatch. Unexpected
        # exceptions get a bounded diagnostic without arbitrary file bytes.
        meta["cleanup_problem"] = (str(exc) if isinstance(exc, CH.HomeError)
                                   else "Codex home removal failed "
                                   f"({type(exc).__name__}); will retry next advance")
        report.append(f"{u['id']}: {meta['cleanup_problem']}")
    else:
        meta["removed"] = True
        meta.pop("cleanup_pending", None)
        meta.pop("cleanup_problem", None)
        report.append(f"{u['id']}: removed Codex home for attempt {attempt}")
    SS.save_state(state_dir, state)


def _archive_code_worktree(state, u, unit_dir, report, state_dir=None):
    """Extend preserve-then-clean to the attempt's recorded Codex home.

    Non-code outputs remain in their exclusive attempt directory; only the
    Codex runtime home is disposable at the existing terminal transition.
    """
    if not unit_dir:
        return
    _preserve_and_archive_code_worktree(state, u, unit_dir, report, state_dir)
    attempt = Path(unit_dir).name
    _cleanup_codex_home(state, u, attempt, report, state_dir)


def _preserve_and_archive_code_worktree(state, u, unit_dir, report, state_dir=None):
    """Preserve, restore-check, then remove a finished code worktree.

    Coordinator-owned worktrees are removed directly with Git. Legacy
    Paseo-owned workspaces are archived through Paseo. Neither destructive
    path is reachable until an atomic audit snapshot has preserved tracked and
    untracked bytes, bound them to the coordinator-state base identity, and
    restored an identical copy. Recovery records are cleanup inputs only;
    judging and retry code never consume them.
    """
    if u.get("kind") != "code" or not unit_dir:
        return
    attempt = Path(unit_dir).name
    us = SS._unit_state(state, u["id"])
    meta = (us.get("attempt_workspaces") or {}).get(attempt)
    if (not isinstance(meta, dict) or meta.get("archived")
            or meta.get("cleanup_gave_up")):
        return
    tries = int(meta.get("cleanup_attempts") or 0)
    if tries >= WORKTREE_ARCHIVE_MAX_ATTEMPTS:
        _worktree_cleanup_failed(
            state, u, meta, report, "previous worktree archive attempts failed")
        if state_dir is not None:
            SS.save_state(state_dir, state)
        return
    meta["cleanup_pending"] = True
    meta["cleanup_attempts"] = tries + 1
    # Charge and persist this attempt before the destructive external call.
    # A crash may consume one retry, but can never reset the bound to zero.
    if state_dir is not None:
        SS.save_state(state_dir, state)
    if state_dir is None:
        _worktree_cleanup_failed(
            state, u, meta, report,
            "refusing worktree cleanup without coordinator state_dir for "
            "a durable recovery snapshot")
        return
    intent = (us.get("attempt_launch_intents") or {}).get(attempt)
    intent_problem = SCL._code_launch_intent_problem(intent, u, attempt)
    if intent_problem:
        _worktree_cleanup_failed(
            state, u, meta, report,
            f"cannot bind recovery snapshot: {intent_problem}")
        SS.save_state(state_dir, state)
        return
    recovery_records = us.setdefault("attempt_recovery_snapshots", {})
    workspace_path = meta.get("path")
    if workspace_path and os.path.isdir(workspace_path):
        workspace_identity = meta.get("workspace_identity") or {}
        expected_git_dir = workspace_identity.get("git_dir")
        expected_git_pointer_sha256 = workspace_identity.get(
            "git_pointer_sha256")
        recovery_record, recovery_error = R.preserve_worktree(
            workspace_path, Path(state_dir) / "recovery-snapshots",
            u["id"], attempt, intent["base_commit"], intent["base_tree"],
            expected_git_dir=expected_git_dir,
            expected_git_pointer_sha256=expected_git_pointer_sha256)
        if recovery_error:
            _worktree_cleanup_failed(
                state, u, meta, report,
                f"worktree preservation failed; checkout retained: "
                f"{recovery_error}")
            SS.save_state(state_dir, state)
            return
        recovery_records[attempt] = recovery_record
        meta["recovery_snapshot"] = "restore-checked"
        # The completed record is durable before either destructive cleanup
        # call. A crash may retry cleanup but cannot lose this prerequisite.
        SS.save_state(state_dir, state)
        current_digest, current_error = R.tree_digest(
            workspace_path, expected_git_dir,
            expected_git_pointer_sha256)
        if (current_error
                or current_digest != recovery_record["content_sha256"]):
            _worktree_cleanup_failed(
                state, u, meta, report,
                "worktree changed after its recovery snapshot was saved; "
                "checkout retained: "
                + (current_error or "content digest differs"))
            SS.save_state(state_dir, state)
            return
    else:
        recovery_record = recovery_records.get(attempt)
        recovery_error = R.validate_snapshot(
            recovery_record, u["id"], attempt,
            intent["base_commit"], intent["base_tree"])
        if recovery_error:
            legacy_paseo = (meta.get("workspace_owner") == "paseo"
                            or (not meta.get("workspace_owner")
                                and bool(meta.get("workspace_id"))))
            if legacy_paseo:
                # Persisted legacy state can describe a Paseo-owned checkout
                # that Paseo deleted before preservation became mandatory.
                # No destructive action remains to gate and the lost bytes
                # cannot be reconstructed. Record that bounded migration fact
                # without inventing a snapshot or completion authority.
                meta["recovery_snapshot"] = (
                    "unavailable-before-preservation-enforcement")
                meta["archived"] = True
                meta.pop("cleanup_pending", None)
                meta["archived_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z")
                meta["cleanup_problem"] = (
                    "legacy checkout was already absent before preservation "
                    "enforcement; no recovery bytes exist")
                SS.save_state(state_dir, state)
                report.append(
                    f"{u['id']}: legacy worktree {workspace_path} was already "
                    "absent before preservation enforcement; recorded the "
                    "unrecoverable migration without running cleanup")
                return
            _worktree_cleanup_failed(
                state, u, meta, report,
                "worktree disappeared without a valid recovery snapshot: "
                + recovery_error)
            SS.save_state(state_dir, state)
            return
        meta["recovery_snapshot"] = "restore-checked"
        meta["archived"] = True
        meta.pop("cleanup_pending", None)
        meta["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        SS.save_state(state_dir, state)
        return

    if meta.get("workspace_owner") == "coordinator":
        rc, out, err = U.run(
            ["git", "-C", intent["repo"], "worktree", "remove", "--force",
             str(workspace_path)], timeout=120)
        if rc != 0:
            _worktree_cleanup_failed(
                state, u, meta, report,
                f"could not remove coordinator-owned worktree "
                f"{workspace_path}: {(err or out).strip()[:200]}")
            SS.save_state(state_dir, state)
            return
        meta["archived"] = True
        meta.pop("cleanup_pending", None)
        meta["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        SS.save_state(state_dir, state)
        report.append(
            f"{u['id']}: preserved and removed finished worktree "
            f"{workspace_path}; branch {meta.get('branch')} remains")
        return

    workspace_id = meta.get("workspace_id")
    if not workspace_id and meta.get("path"):
        workspace_id = _paseo_workspace_for_path(meta.get("path"))
        if workspace_id:
            meta["workspace_id"] = workspace_id
            # Persist newly-discovered cleanup authority before using it.
            if state_dir is not None:
                SS.save_state(state_dir, state)
    if not workspace_id:
        _worktree_cleanup_failed(
            state, u, meta, report,
            f"could not identify cleanup owner for preserved attempt "
            f"{attempt}")
        if state_dir is not None:
            SS.save_state(state_dir, state)
        return
    rc, out, err = U.run(
        ["paseo", "workspace", "archive", str(workspace_id), "--json"],
        timeout=120)
    if rc != 0:
        _worktree_cleanup_failed(
            state, u, meta, report, f"could not archive worktree workspace "
            f"{workspace_id}: {_paseo_error(out, err)}")
        if state_dir is not None:
            SS.save_state(state_dir, state)
        return
    meta["archived"] = True
    meta.pop("cleanup_pending", None)
    meta["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if state_dir is not None:
        SS.save_state(state_dir, state)
    report.append(f"{u['id']}: preserved and archived legacy worktree "
                  f"{meta.get('path')}; branch {meta.get('branch')} remains")


def reconcile_orphan(unit_dir, allocated_at=None, kind=None):
    """Did a job for this attempt reach the scheduler even though we never
    recorded its id? Returns (job_id, note).

    Asks the SCHEDULER, which is the only party that knows. Blindly
    resubmitting after a crash is how one unit becomes two jobs writing the
    same directory -- the exact thing the exclusive write root is for."""
    attempt_id = Path(unit_dir).name
    if kind == "code":
        # Same question, different registry: paseo knows whether an agent was
        # created for this attempt.
        rc, out, _ = U.run(["paseo", "ls", "--json"], timeout=60)
        if rc != 0:
            return None, "UNKNOWN"
        try:
            # EXACT trailing match. `attempt_id in name` also matched an
            # attempt whose id is a prefix of another's, binding a unit to
            # somebody else's agent.
            for a in json.loads(out or "[]"):
                name = str(a.get("name") or "")
                if name.split()[-1:] == [attempt_id]:
                    aid = a.get("id") or a.get("agentId")
                    if aid:
                        return str(aid), (
                            f"recovered agent {aid} for attempt {attempt_id}: "
                            f"it had been created but was never bound. Not "
                            f"re-run.")
        except (ValueError, AttributeError):
            return None, "UNKNOWN"
        return None, None
    name = f"swarm-{attempt_id}"
    # `sacct` WITHOUT -S defaults to jobs that started today. A crash at 23:50
    # whose job finished at 23:55 is invisible to a 00:10 reconcile, and the
    # unit decays to FAILED_EVIDENCE though it succeeded. Anchor the window to
    # when the attempt was allocated, an hour early for clock skew.
    since = (allocated_at - 3600) if allocated_at else (time.time() - 7 * 86400)
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since))
    asked = 0
    for argv in (["squeue", "-h", "-n", name, "-o", "%i"],
                 ["sacct", "-n", "-P", "-X", "--name", name,
                  "-S", start, "-o", "JobID"]):
        rc, out, _ = (U.run_sacct(argv) if argv[0] == "sacct"
                      else U.run(argv, timeout=60))
        if rc != 0:
            continue                       # the tool failed; it proved nothing
        asked += 1
        if (out or "").strip():
            job = out.strip().splitlines()[0].split("|")[0].strip()
            if job:
                return job, (f"recovered job {job} for attempt {attempt_id}: it "
                             f"had reached the scheduler but was never bound. "
                             f"Not resubmitted.")
    # "Both tools answered and neither knows it" is proof of absence.
    # "squeue was down" is not, and treating the two alike would release a
    # live attempt and dispatch a second job for one unit.
    return None, (None if asked == 2 else "UNKNOWN")
