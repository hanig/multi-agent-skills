"""Code launch capture, worktree creation, and audit record writers.

The coordinator imports this module; dependencies never point back to swarm.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

import recovery as R
import swarm_authors as SA
import swarm_plan_rules as SPR
import swarm_preflight as SP
import swarm_routing as SR
import unit as U
import worktree as W
from swarm_types import PlanError


LAUNCH_RECORD = "launch.json"


def _capture_code_launch(unit_dir, u, dispatch_source=None, unit_state=None):
    """Record immutable input to coordinator worktree creation.

    The shared checkout is a SOURCE, not the execution tree. Its dirty index
    and working files cannot enter a worktree made from an object id, so
    checking them would both block unrelated human work and prove nothing
    about the tree the agent receives. The clean-at-launch guarantee comes
    from constructing a new branch-off worktree from ``base_commit``.

    The stash stack is the exception, and `_stash_refusal` says why.
    """
    try:
        seed = SPR.declared_seed(u)
    except PlanError as exc:
        return str(exc), None
    repo, err = SP._plan_workspace(u)
    if err:
        return err, None
    stash_problem = SP._stash_preflight(u.get("id"), repo)
    if stash_problem:
        return stash_problem, None
    target = str(u.get("target_branch") or "").strip()
    if not target:
        return (f"unit {u.get('id')!r}: no target_branch was declared; "
                f"refusing to create a source branch for a pull request with "
                f"no named destination"), None
    slug, branch = SP._code_worktree_names(unit_dir)
    # The generated source name depends on the attempt id, so plan validation
    # cannot know this collision. Intent construction is the first point that
    # can, and it is still before the coordinator creates a worktree or agent.
    if target == branch:
        return (f"unit {u.get('id')!r}: target_branch {target!r} is the same "
                f"as generated attempt branch {branch!r}; a pull request "
                f"cannot merge a branch into itself"), None
    # Store the source identity in the same canonical form used for the
    # worktree cwd. This is an authority boundary, not a display path: a
    # relative spelling or symlink must not make the source checkout compare
    # unequal to itself later.
    repo = str(Path(repo).resolve())
    source = dispatch_source
    if source is None:
        resolved, problem = SP._resolve_dispatch_target(u)
        if problem:
            return problem, None
        source, problem = SP._dispatch_source_identity(u, resolved)
        if problem:
            return problem, None
    if source.get("repo") != repo or source.get("target_branch") != target:
        return SP._dispatch_base_refusal(
            u.get("id"), repo, "cached dispatch source names another target"), None
    seed_problem = SP._seed_reachability_problem(
        seed, repo, source["repository_remote_raw"], source["repository_remote"])
    if seed_problem:
        return seed_problem, None
    head, tree = source["base_commit"], source["base_tree"]
    remote_raw = source["repository_remote_raw"]
    remote = source["repository_remote"]
    judgment_ref = f"refs/heads/{branch}"
    # Query the PUSH destination, not the raw fetch spelling: with
    # `url.*.pushInsteadOf` configured they are different repositories, and
    # the attempt will push to the former. Checking the latter for collisions
    # asks the wrong repository and later judges the wrong one too.
    remote_rc, _remote_head, remote_err = SP._git_push_destination(
        repo, remote_raw, remote, 2, "ls-remote", "--exit-code", remote_raw,
        f"refs/heads/{branch}")
    if remote_rc == 0:
        return (f"unit {u.get('id')!r}: generated attempt branch {branch!r} "
                f"already exists on origin. Allocate a new attempt rather "
                f"than asking an agent to overwrite unrelated remote "
                f"history"), None
    if remote_rc != 2:
        detail = (remote_err or "git ls-remote returned %s" % remote_rc).strip()
        return (f"unit {u.get('id')!r}: cannot establish that generated "
                f"attempt branch {branch!r} is absent on origin: "
                f"{detail[:200]}. Refusing before agent creation"), None
    # The remote branch and the local branch are separate collision domains.
    # The coordinator creates the latter, while the former is the durable ref
    # the checker will query. A remote-tracking ref is deliberately irrelevant:
    # whether Git writes one after push is controlled by remote.origin.fetch.
    local_ref = f"refs/heads/{branch}"
    ref_rc, _out, _err = SP._git(
        repo, "show-ref", "--verify", "--quiet", local_ref)
    if ref_rc == 0:
        return (f"unit {u.get('id')!r}: generated local attempt branch "
                f"{branch!r} already exists. Allocate a new attempt rather "
                f"than asking Paseo to reuse its history"), None
    intent = {
        "schema_version": 6,
        "unit_id": u.get("id"),
        "attempt_id": Path(unit_dir).name,
        "launch_host": os.uname().nodename,
        "repo": repo,
        "repository_remote": remote,
        "repository_remote_raw": remote_raw,
        "base_commit": head,
        "target_commit": source["target_commit"],
        "base_tree": tree,
        "worktree_slug": slug,
        "branch": branch,
        # The exact durable observation the checker will make after the
        # worktree may already be gone. This is the remote ref NAME;
        # the agent controls its VALUE by pushing, and the checker derives and
        # validates that value rather than accepting an agent assertion.
        "judgment_ref": judgment_ref,
        "target_branch": target,
        # Makes the audit payload reproducible after a crash. Recovery can
        # compare exact expected bytes and restore the original seal without
        # trusting or laundering fields out of the file.
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        # Audit-only, captured before Paseo runs and persisted with the intent
        # so recovery retains this observation instead of sampling a new one.
        "installed_skills": SP._installed_skill_snapshot(repo, head),
        # Prompt metadata only, using the same defaults/overrides as _submit.
        # Do not copy into launch facts or use for admission/closure. Missing
        # fields in legacy intents remain unknown; never backfill them.
        "provider": u.get("provider") or SR.DEFAULT_AGENT_PROVIDER,
        "model": u.get("model"),
        "author_history": SA.collect_author_history(u, unit_state, Path(unit_dir).name),
    }
    if seed is not None:
        intent["seed"] = dict(seed)
    return None, {"base": head, "intent": intent}


def _code_launch_intent_problem(intent, u, attempt):
    if not isinstance(intent, dict):
        return (f"unit {u.get('id')!r}: coordinator state has no worktree "
                f"launch intent for attempt {attempt!r}")
    if intent.get("unit_id") != u.get("id") or intent.get("attempt_id") != attempt:
        return (f"unit {u.get('id')!r}: worktree launch intent belongs to "
                f"unit {intent.get('unit_id')!r}, attempt "
                f"{intent.get('attempt_id')!r}")
    schema = intent.get("schema_version", 1)
    if (not isinstance(schema, int) or isinstance(schema, bool)
            or schema < 1):
        return (f"unit {u.get('id')!r}: worktree launch intent has invalid "
                f"schema_version {schema!r}")
    for key in ("repo", "base_commit", "base_tree", "worktree_slug", "branch",
                "target_branch", "captured_at"):
        if not intent.get(key):
            return (f"unit {u.get('id')!r}: worktree launch intent is "
                    f"incomplete (missing {key})")
    if schema >= 2:
        for key in ("repository_remote", "judgment_ref"):
            if not intent.get(key):
                return (f"unit {u.get('id')!r}: worktree launch intent is "
                        f"incomplete (missing {key})")
    if schema >= 4 and not intent.get("repository_remote_raw"):
        return (f"unit {u.get('id')!r}: worktree launch intent is "
                "incomplete (missing repository_remote_raw)")
    if (schema >= 5
            and (not isinstance(intent.get("launch_host"), str)
                 or not intent["launch_host"])):
        return (f"unit {u.get('id')!r}: worktree launch intent is "
                "incomplete (missing launch_host)")
    for key in ("base_commit", "base_tree"):
        value = intent[key]
        if (not isinstance(value, str) or len(value) not in (40, 64)
                or any(c not in "0123456789abcdef" for c in value.lower())):
            return f"unit {u.get('id')!r}: invalid trusted {key}"
    if intent["target_branch"] == intent["branch"]:
        return (f"unit {u.get('id')!r}: trusted pull-request target equals "
                f"its generated attempt branch {intent['branch']!r}")
    expected_ref = (f"refs/heads/{intent['branch']}" if schema >= 3 else
                    f"refs/remotes/origin/{intent['branch']}")
    if (intent.get("judgment_ref") is not None
            and intent["judgment_ref"] != expected_ref):
        return (f"unit {u.get('id')!r}: trusted judgment ref "
                f"{intent['judgment_ref']!r} is not the schema-{schema} "
                "judgment ref "
                f"for attempt branch {intent['branch']!r}")
    target = str(u.get("target_branch") or "").strip()
    if intent["target_branch"] != target:
        return (f"unit {u.get('id')!r}: trusted pull-request target "
                f"{intent['target_branch']!r} disagrees with plan target "
                f"{target!r}")
    return None


def _paseo_workspace_id(out):
    """Read the workspace id from Paseo's creation notice, if present.

    KNOWN WEAKNESS: this fallback parses free text. It is used for cleanup
    bookkeeping, never to authenticate the returned cwd, branch, base, or Git
    identity. A same-UID process can also falsify Paseo registry evidence, so
    hardening this spelling alone would not create an ownership boundary.
    """
    match = re.search(r"(?m)^Created workspace (wks_[A-Za-z0-9]+)\b", out or "")
    return match.group(1) if match else None


def _code_launch_record_payload(facts):
    """Deterministic audit bytes reconstructed only from trusted facts."""
    captured_at = facts["captured_at"]
    rec = dict(facts)
    rec.update({
        "captured_at": captured_at,
        "preflight": {
            "status": "passed",
            "checked_at": captured_at,
            "predicate": "paseo-branch-off-from-immutable-commit",
            "workspace": facts["execution_workspace"],
            "dirty_path_count": 0,
        },
        "dirty_paths_at_launch": 0,
        "dirty_paths": [],
    })
    return (json.dumps(rec, indent=1, sort_keys=True) + "\n").encode()


def _create_code_worktree(state_dir, intent):
    """Create the linked worktree the coordinator, not Paseo, will own."""
    source = Path(intent["repo"]).resolve()
    root = (Path(state_dir).resolve() / "code-worktrees")
    workspace = root / intent["worktree_slug"]
    try:
        if os.path.commonpath((str(source), str(root))) == str(source):
            return None, None, (
                f"refusing coordinator worktree root {root}: it is "
                f"inside operated repository {source}")
    except ValueError:
        pass
    if workspace.parent != root or workspace.exists() or workspace.is_symlink():
        return (None, None,
                f"coordinator worktree path {workspace} is not fresh")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (None, None,
                f"cannot create coordinator worktree root {root}: {exc}")
    rc, _out, err = SP._git(
        source, "worktree", "add", "-q", "-b", intent["branch"],
        str(workspace), intent["base_commit"], timeout=120)
    if rc != 0:
        return None, None, (f"cannot create coordinator-owned worktree "
                            f"{workspace}: {err[:200]}")
    resolved = str(workspace.resolve())
    pointer_digest, pointer_error = R.git_pointer_digest(
        Path(resolved) / ".git")
    if pointer_error:
        return resolved, None, pointer_error
    return resolved, pointer_digest, None


def _register_code_workspace(state, u, unit_dir, workspace, workspace_id=None,
                             workspace_owner=None):
    """Record a workspace before deciding whether to trust it.

    This is cleanup bookkeeping, not launch verification. In particular, a
    path that later fails branch, base, or Git-identity checks must remain
    visible and recoverable rather than becoming an unowned resource.
    """
    attempt = Path(unit_dir).name
    us = state.setdefault("units", {}).setdefault(u["id"], {})
    intent = (us.get("attempt_launch_intents") or {}).get(attempt) or {}
    path = str(Path(workspace).resolve()) if workspace else None
    prior = (us.get("attempt_workspaces") or {}).get(attempt) or {}
    meta = {
        "path": path,
        "branch": intent.get("branch"),
        "slug": intent.get("worktree_slug"),
        "archived": False,
        "verification": "pending",
    }
    owner = workspace_owner or prior.get("workspace_owner")
    if owner:
        meta["workspace_owner"] = owner
    if workspace_id:
        meta["workspace_id"] = workspace_id
    elif prior.get("workspace_id"):
        meta["workspace_id"] = prior["workspace_id"]
    us.setdefault("attempt_workspaces", {})[attempt] = meta
    return meta


def _write_code_launch_record(unit_dir, facts):
    """Write the human-readable audit copy after Paseo reports its cwd."""
    path = W.launch_record_path(unit_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _code_launch_record_payload(facts)
        seal = hashlib.sha256(payload).hexdigest()
        with open(path, "xb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        return seal, None
    except FileExistsError:
        # Crash after fsync but before save_state. Do not bless whatever is on
        # disk: compare it byte-for-byte with the deterministic payload
        # reconstructed from trusted intent + verified Paseo cwd. Exact bytes
        # restore the same seal; any difference refuses recovery.
        try:
            existing = path.read_bytes()
        except OSError as exc:
            return None, f"cannot read existing launch record {path}: {exc}"
        if existing != payload:
            return None, (
                f"existing launch record {path} does not exactly match the "
                f"trusted worktree launch facts; move it aside and retry "
                f"recovery rather than sealing changed evidence")
        return seal, None
    except OSError as exc:
        return None, f"cannot write the launch record for {facts['unit_id']!r}: {exc}"


def _complete_code_launch(state, u, unit_dir, workspace, workspace_id=None,
                          recovery=False, workspace_owner=None,
                          git_pointer_sha256=None):
    """Complete trusted launch facts from the verified worktree cwd.

    A new launch must still be exactly at its pinned base. Crash recovery is
    different: the already-running agent may honestly have committed while
    the coordinator was down, so recovery accepts an extension of that base
    but never a replacement history.
    """
    attempt = Path(unit_dir).name
    us = state.setdefault("units", {}).setdefault(u["id"], {})
    intent = (us.get("attempt_launch_intents") or {}).get(attempt)
    problem = _code_launch_intent_problem(intent, u, attempt)
    if problem:
        return problem
    problem = W.launch_host_problem(intent)
    if problem:
        return problem
    prior_facts = (us.get("attempt_launch_facts") or {}).get(attempt) or {}
    prior_meta = (us.get("attempt_workspaces") or {}).get(attempt) or {}
    prior_identity = (prior_facts.get("workspace_identity")
                      or prior_meta.get("workspace_identity") or {})
    if git_pointer_sha256 is None:
        # Carry forward only a digest the coordinator already stored. Never
        # manufacture a launch baseline by observing the agent's live file.
        git_pointer_sha256 = prior_identity.get("git_pointer_sha256")
    workspace = str(Path(workspace).resolve())
    if workspace == intent["repo"]:
        return (f"the shared source checkout {workspace!r} was supplied "
                f"instead of a per-attempt worktree")
    if not os.path.isdir(workspace):
        return f"attempt worktree {workspace!r} is missing"
    rc, top, _ = SP._git(workspace, "rev-parse", "--show-toplevel")
    if rc != 0 or str(Path(top).resolve()) != workspace:
        return f"attempt cwd {workspace!r} is not a Git worktree root"
    rc, source_common, _ = SP._git(intent["repo"], "rev-parse", "--git-common-dir")
    rc2, worktree_common, _ = SP._git(workspace, "rev-parse", "--git-common-dir")
    if rc != 0 or rc2 != 0:
        return f"cannot identify Git common directory for {workspace!r}"
    source_common = str((Path(intent["repo"]) / source_common).resolve())
    worktree_common = str((Path(workspace) / worktree_common).resolve())
    if source_common != worktree_common:
        return (f"attempt cwd {workspace!r} is not a worktree of trusted source "
                f"repository {intent['repo']!r}")
    rc, worktree_git_dir, _ = SP._git(workspace, "rev-parse", "--git-dir")
    if rc != 0:
        return f"cannot identify Git metadata directory for {workspace!r}"
    worktree_git_dir = str((Path(workspace) / worktree_git_dir).resolve())
    if worktree_git_dir == worktree_common:
        return (f"attempt cwd {workspace!r} is the repository's main checkout, "
                f"not a linked per-attempt worktree")
    # Initial creation requires the exact base before Paseo starts. Recovery
    # may see honest commits made by the already-running agent and therefore
    # accepts only descendant history.
    rc, branch, _ = SP._git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or branch != intent["branch"]:
        return (f"attempt worktree {workspace!r} is on branch {branch!r}, not "
                f"trusted attempt branch {intent['branch']!r}")
    rc, head, _ = SP._git(workspace, "rev-parse", "HEAD")
    if rc != 0:
        return f"attempt worktree {workspace!r} has no readable HEAD"
    if recovery:
        rc, _, _ = SP._git(workspace, "merge-base", "--is-ancestor",
                         intent["base_commit"], head)
        if rc != 0:
            return (f"recovered attempt worktree {workspace!r} at {head} does "
                    f"not descend from trusted base {intent['base_commit']}; "
                    f"its history was replaced rather than extended")
    elif head != intent["base_commit"]:
        return (f"attempt worktree {workspace!r} is at {head}, not trusted "
                f"base {intent['base_commit']}")
    try:
        st = os.stat(workspace)
        common_st = os.stat(worktree_common)
        git_st = os.stat(worktree_git_dir)
    except OSError as exc:
        return f"cannot identify attempt worktree {workspace!r}: {exc}"
    identity = {"path": workspace, "realpath": workspace,
                "device": st.st_dev, "inode": st.st_ino,
                "git_common_dir": worktree_common,
                "git_common_device": common_st.st_dev,
                "git_common_inode": common_st.st_ino,
                "git_dir": worktree_git_dir,
                "git_dir_device": git_st.st_dev,
                "git_dir_inode": git_st.st_ino}
    if git_pointer_sha256 is not None:
        identity["git_pointer_sha256"] = git_pointer_sha256
    intent_schema = intent.get("schema_version", 1)
    judgment_ref = intent.get("judgment_ref")
    direct_remote_judgment = intent_schema >= 3
    facts = {
        # Schema 3 facts were emitted by the preserved first attempt and name
        # a local remote-tracking ref. Schema 4 facts are the first direct-ref
        # generation but predate the raw URL anchor. Keep both migrations
        # byte-compatible; schema 5 records the raw and once-expanded route,
        # and schema 6 binds the host-scoped identity to its launch host.
        "schema_version": (6 if intent_schema >= 5 else
                           5 if intent_schema >= 4 else
                           4 if direct_remote_judgment else
                           3 if judgment_ref else 2),
        "unit_id": u.get("id"),
        "attempt_id": attempt,
        "launch_host": intent.get("launch_host"),
        "repo": intent["repo"],
        "repository_remote": intent.get("repository_remote"),
        "workspace_identity": identity,
        "execution_workspace": workspace,
        "base_commit": intent["base_commit"],
        "base_tree": intent["base_tree"],
        "branch": intent["branch"],
        "worktree_slug": intent["worktree_slug"],
        "captured_at": intent["captured_at"],
        "clean_at_launch": True,
    }
    if intent.get("repository_remote_raw") is not None:
        facts["repository_remote_raw"] = intent["repository_remote_raw"]
    if judgment_ref:
        facts["judgment_ref"] = judgment_ref
    # Old intents have no observation to recover. Do not backfill them from
    # today's installs or change their deterministic audit bytes.
    if "installed_skills" in intent:
        facts["installed_skills"] = intent["installed_skills"]
    seal, error = _write_code_launch_record(unit_dir, facts)
    if error or not seal:
        return error or "worktree launch record has no recoverable seal"
    us.setdefault("attempt_launch_facts", {})[attempt] = facts
    prior = (us.get("attempt_workspaces") or {}).get(attempt) or {}
    meta = {"path": workspace, "branch": intent["branch"],
            "slug": intent["worktree_slug"], "workspace_identity": identity,
            "archived": False,
            "workspace_owner": (workspace_owner
                                or prior.get("workspace_owner")
                                or ("paseo" if workspace_id else "unknown"))}
    if workspace_id:
        meta["workspace_id"] = workspace_id
    us.setdefault("attempt_workspaces", {})[attempt] = meta
    us.setdefault("attempt_record_seals", {})[attempt] = seal
    return None


def _write_launch_record(unit_dir, u):
    """Capture the repository state BEFORE the agent exists.

    Returns an error string, or None. A unit whose policy requires clean Git
    must declare an execution workspace. Other units may explicitly record
    that no repository transition applies.

    The record lives beside the attempt, NOT inside the agent's working
    directory: a worker that can rewrite its own baseline can manufacture a
    transition, and the whole point of the anchor is that it cannot.
    """
    # PER ATTEMPT, and outside the attempt directory.
    #
    # It was `<unit>/launch.json`, shared by every attempt of the unit, and
    # written with "x" so a retry left the first attempt's baseline in place.
    # A retry then inherited an anchor from before the previous attempt's
    # commits, so THOSE commits satisfied the new attempt's transition and a
    # retry that produced nothing passed. One anchor per attempt.
    #
    # Still not inside `unit_dir`: attempt artifacts and coordinator launch
    # facts are different records and should not share a filename namespace.
    path = W.launch_record_path(unit_dir)
    repo = SP._execution_workspace(u)
    dirty = []
    rec = {"schema_version": 2, "unit": u.get("id"),
           "attempt": Path(unit_dir).name,
           "launch_host": os.uname().nodename,
           "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    if not repo:
        # validate_plan already rejects this shape. Direct _submit callers are
        # reachable in tests and recovery helpers, so the launch chokepoint
        # must still fail closed rather than create an unchecked code agent.
        if SP._requires_clean_workspace(u):
            return (f"unit {u.get('id')!r}: launch preflight requires a "
                    f"declared Git execution workspace"), None
        rec["repo"] = None
        rec["execution_workspace"] = None
        rec["preflight"] = {"status": "not-required"}
        rec["note"] = ("this unit declared no Git execution workspace, so no "
                       "git transition can be judged for it")
    else:
        repo = str(repo)
        if not os.path.isdir(repo):
            return (f"unit {u['id']!r} declares repo {repo!r}, which is "
                    f"not a directory"), None
        rc, top, _ = SP._git(repo, "rev-parse", "--show-toplevel")
        if rc != 0:
            return (f"unit {u['id']!r} declares repo {repo!r}, which is "
                    f"not a git repository"), None
        rc, head, _ = SP._git(repo, "rev-parse", "HEAD")
        if rc != 0:
            return (f"unit {u['id']!r}: {repo!r} has no HEAD to anchor to. "
                    f"An empty repository gives nothing to transition FROM."), None
        rc, tree, _ = SP._git(repo, "rev-parse", "HEAD^{tree}")
        rc2, branch, _ = SP._git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        rc3, dirty = W.repo_status(U.run, repo)
        if rc3 != 0:
            return (f"unit {u['id']!r}: cannot read git status in "
                    f"{repo!r}"), None
        # The remote, so a merge receipt naming a DIFFERENT repository can be
        # refused. The receipt says "owner/name"; the anchor knows what this
        # working copy actually pushes to.
        rc4, remote, _ = SP._git(repo, "remote", "get-url", "origin")
        rec["remote"] = remote if rc4 == 0 else None
        resolved_top = str(Path(top).resolve())
        rc5, common_dir, _ = SP._git(repo, "rev-parse", "--git-common-dir")
        rc6, git_dir, _ = SP._git(repo, "rev-parse", "--git-dir")
        if rc5 != 0 or rc6 != 0:
            return (f"unit {u['id']!r}: cannot identify Git metadata for "
                    f"{resolved_top!r}"), None
        common_dir = str((Path(resolved_top) / common_dir).resolve())
        git_dir = str((Path(resolved_top) / git_dir).resolve())
        try:
            st = os.stat(resolved_top)
            common_st = os.stat(common_dir)
            git_st = os.stat(git_dir)
            identity = {"path": resolved_top, "realpath": resolved_top,
                        "device": st.st_dev, "inode": st.st_ino,
                        "git_common_dir": common_dir,
                        "git_common_device": common_st.st_dev,
                        "git_common_inode": common_st.st_ino,
                        "git_dir": git_dir,
                        "git_dir_device": git_st.st_dev,
                        "git_dir_inode": git_st.st_ino}
        except OSError as exc:
            return (f"unit {u['id']!r}: cannot identify execution workspace "
                    f"{resolved_top!r}: {exc}"), None
        status = "passed" if not dirty else "refused"
        rec.update({"repo": resolved_top,
                    "execution_workspace": resolved_top,
                    "workspace_identity": identity,
                    "branch": branch if rc2 == 0 else None,
                    "base_commit": head, "base_tree": tree if rc == 0 else None,
                    "clean_at_launch": not dirty,
                    "dirty_paths_at_launch": len(dirty),
                    "dirty_paths": dirty,
                    "preflight": {
                        "status": status,
                        "checked_at": rec["captured_at"],
                        "predicate": "git-status-porcelain-v1-z",
                        "workspace": resolved_top,
                        "dirty_path_count": len(dirty),
                    }})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize ONCE and seal those exact bytes. Re-serializing to compute
        # the digest would seal a second rendering, and any difference in
        # separators or key order between the two makes the seal fail on a
        # record nobody touched.
        payload = json.dumps(rec, indent=1, sort_keys=True) + "\n"
        seal = hashlib.sha256(payload.encode()).hexdigest()
        with open(path, "x") as fh:      # x: an anchor is written ONCE
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except FileExistsError:
        # Already anchored, so this is a re-dispatch of the SAME attempt.
        # I warned explicitly that retry and recovery must not bypass the
        # initial preflight, then made exactly that mistake here. Re-run the
        # dirty predicate, but never move or re-trust the anchored base.
        #
        # Do NOT read the base back out of the file. I removed that laundering
        # from the main path and left it right here, which two reviewers found
        # independently: reading an agent-writable value and returning it to
        # be stored as trusted coordinator state is the exact move the trust
        # was meant to prevent, and it does not stop being that because it
        # happens on the second dispatch.
        #
        # The coordinator observed the base the FIRST time and wrote it to
        # state then. If state has it, it stands. If it does not, this attempt
        # cannot be verified, and `verify` says so rather than inventing a
        # base from a file the agent can edit.
        # The CURRENT predicate decides, and only it. A previous refusal
        # recorded in the file used to refuse here too, which was wrong twice
        # over: a workspace cleaned since then is legitimately dispatchable,
        # and the stale message was built by reading `dirty_paths` back out of
        # a record the launched agent can write. Both reviewers landed on this
        # function from opposite directions, one calling it a false refusal
        # and one calling it a manufactured one. It has no work left to do.
        current_refusal, _workspace = SP._repeat_launch_preflight(u)
        if current_refusal:
            return current_refusal, None
        # No new bytes were written, so there is no new seal to report. The
        # seal from the FIRST dispatch stands in state; re-sealing here would
        # bless whatever the file says now, which is the laundering this
        # whole mechanism exists to stop.
        return None, None
    except OSError as exc:
        return f"cannot write the launch record for {u['id']!r}: {exc}", None
    if dirty:
        return SP._dirty_refusal(u.get("id"), resolved_top, dirty), None
    facts = {
        "schema_version": 1,
        "unit_id": rec.get("unit"),
        "attempt_id": rec.get("attempt"),
        "launch_host": rec.get("launch_host"),
        "repo": rec.get("repo"),
        "repository_remote": rec.get("remote"),
        "workspace_identity": rec.get("workspace_identity"),
        "execution_workspace": rec.get("execution_workspace"),
        "base_commit": rec.get("base_commit"),
        "base_tree": rec.get("base_tree"),
        "branch": rec.get("branch"),
        "clean_at_launch": rec.get("clean_at_launch"),
    }
    return None, {"base": rec.get("base_commit"), "seal": seal,
                  "facts": facts}
