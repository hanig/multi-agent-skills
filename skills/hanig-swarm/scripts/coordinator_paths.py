#!/usr/bin/env python3
"""External coordinator paths and non-destructive legacy migration.

The operated checkout is an input to the coordinator. State kept below that
checkout changes the very Git predicate used to decide whether code may run,
so defaults live in the user's state area and explicit in-repository paths are
refused before their parent directories are created.

Python 3.8+, standard library only.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class PathPolicyError(Exception):
    pass


def _resolved(path):
    return Path(path).expanduser().resolve()


def _inside(path, directory):
    path, directory = _resolved(path), _resolved(directory)
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _git(repo, *args):
    try:
        p = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def git_top(path):
    """The containing worktree, with symlinks resolved, or None."""
    top = _git(path, "rev-parse", "--show-toplevel")
    return _resolved(top) if top else None


def worktrees_of(repo):
    """All ordinary worktrees attached to ``repo``.

    Git 2.23, one of this project's deployment targets, lacks ``-z`` for
    ``git worktree list``. Its porcelain format still gives a dedicated
    ``worktree `` record, which is sufficient for the normal absolute paths
    accepted by that Git version. The declared checkout itself is always
    included independently, so a listing failure never weakens the primary
    containment check.
    """
    top = git_top(repo)
    if top is None:
        return []
    found = {top}
    raw = _git(top, "worktree", "list", "--porcelain")
    if raw:
        for line in raw.splitlines():
            if line.startswith("worktree "):
                found.add(_resolved(line[len("worktree "):]))
    return sorted(found, key=str)


def operated_worktrees(plan=None, cwd=None, extra_repos=()):
    """Resolved worktrees the current command may operate on."""
    repos = list(extra_repos or [])
    for unit in ((plan or {}).get("units") or []):
        if not isinstance(unit, dict):
            continue
        # Keep every workspace source understood by swarm._execution_workspace
        # in this boundary check. Protecting only `repo` leaves policy-only
        # execution checkouts open to coordinator state writes.
        for candidate in (unit.get("repo"), unit.get("execution_workspace")):
            if candidate:
                repos.append(candidate)
        policy = unit.get("workspace_policy") or {}
        if isinstance(policy, dict) and policy.get("path"):
            repos.append(policy["path"])
    context = _resolved(cwd or os.getcwd())
    if git_top(context) is not None:
        repos.append(context)
    found = set()
    for repo in repos:
        found.update(worktrees_of(repo))
    return sorted(found, key=str)


def project_context(cwd=None):
    here = _resolved(cwd or os.getcwd())
    return git_top(here) or here


def _external_base(worktrees):
    candidates = []
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and os.path.isabs(os.path.expanduser(xdg)):
        candidates.append(_resolved(xdg))
    candidates.append(_resolved(Path.home() / ".local" / "state"))
    candidates.append(_resolved(Path(tempfile.gettempdir()) / "hanig-swarm-state"))
    for candidate in candidates:
        if not any(_inside(candidate, wt) for wt in worktrees):
            return candidate
    raise PathPolicyError(
        "no external state location is available outside the operated Git "
        "worktrees")


def default_paths(cwd=None, worktrees=()):
    """Stable per-project defaults shared by run, status, and outbox."""
    context = project_context(cwd)
    digest = hashlib.sha256(str(context).encode()).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", context.name).strip("-.")
    slug = slug or "project"
    project = _external_base(worktrees) / "hanig-swarm" / "projects"
    project = project / f"{slug}-{digest}"
    return project / "state", project / "runs"


def resolve_paths(state_dir=None, root=None, plan=None, cwd=None,
                  extra_repos=(), need_root=False):
    """Resolve and validate paths without creating anything."""
    worktrees = operated_worktrees(plan, cwd, extra_repos)
    default_state, default_root = default_paths(cwd, worktrees)
    state = _resolved(state_dir) if state_dir is not None else default_state
    runs = (_resolved(root) if root is not None else default_root) if need_root else None
    for label, path in (("state-dir", state), ("root", runs)):
        if path is None:
            continue
        for wt in worktrees:
            if _inside(path, wt):
                raise PathPolicyError(
                    f"{label} {str(path)!r} resolves inside operated Git "
                    f"worktree {str(wt)!r}. Coordinator state and run roots "
                    f"must be external; nothing was written.")
    return state, runs, worktrees


def validated_run_root(root, repo=None, cwd=None):
    """Validate unit.py's direct allocation path without creating it."""
    plan = {"units": [{"repo": repo}]} if repo else None
    _state, runs, _worktrees = resolve_paths(
        root=root, plan=plan, cwd=cwd, need_root=True)
    return runs


def required_external_run_root(root, repo=None, cwd=None):
    """CLI view of ``validated_run_root`` with a concise refusal."""
    try:
        return validated_run_root(root, repo, cwd)
    except PathPolicyError as exc:
        raise SystemExit(f"error: {exc}")


def _copy_tree(src, dst):
    """Copy without removing either source or an existing destination."""
    src, dst = Path(src), Path(dst)
    if not src.is_dir():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        # Never let migration turn an external receipt into whichever legacy
        # file happened to be copied last. Equal files and missing files are
        # safe to merge; any different existing file needs a human decision.
        for base, dirs, files in os.walk(str(src)):
            rel = Path(base).relative_to(src)
            target_base = dst / rel
            target_base.mkdir(parents=True, exist_ok=True)
            for name in files:
                source, target = Path(base) / name, target_base / name
                if target.exists():
                    if source.read_bytes() != target.read_bytes():
                        raise OSError(
                            f"migration conflict: {target} already exists "
                            f"with different content; neither copy was "
                            f"deleted or overwritten")
                    continue
                shutil.copy2(str(source), str(target))
        return True
    tmp = dst.parent / f".{dst.name}.migrating-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(str(tmp))
    shutil.copytree(str(src), str(tmp))
    os.rename(str(tmp), str(dst))
    return True


def migrate_legacy_defaults(state_dir, root, cwd=None):
    """Copy active ``.swarm`` data to external defaults, never delete it.

    Attempt paths in copied state intentionally retain their historical
    values. A scheduler JobId may still be writing to that exact directory,
    and changing the pointer would turn a copy into a false launch fact. New
    attempts use the external root passed to the coordinator.
    """
    context = project_context(cwd)
    legacy_state = context / ".swarm" / "state"
    legacy_root = context / ".swarm" / "runs"
    state_dir, root = Path(state_dir), Path(root)
    marker = state_dir / "legacy-migration.json"
    state_file = state_dir / "swarm-state.json"
    if state_file.exists() or not (legacy_state / "swarm-state.json").is_file():
        return None

    copied_runs = _copy_tree(legacy_root, root)
    _copy_tree(legacy_state, state_dir)
    record = {
        "schema_version": 1,
        "copied_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "legacy_state_dir": str(legacy_state.resolve()),
        "legacy_root": str(legacy_root.resolve()),
        "external_state_dir": str(state_dir.resolve()),
        "external_root": str(root.resolve()),
        "runs_copied": copied_runs,
        "source_files_retained": True,
        "attempt_paths_rewritten": False,
    }
    marker.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record
