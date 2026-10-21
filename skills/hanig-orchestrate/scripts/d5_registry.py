"""Inert D5 registration library; no production callers or CLI.

Local filesystem, trusted same-UID writers. Filesystem objects identify a
repository and state directory; paths are locators, never identity. Atomic
registry snapshots permit read-only lookup without creating a lock.
The permanent registry lock serializes registration writes.
No custody, enforcement, migration or protection from outside-lock writers.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager

import skill_paths
import d5_authority as authority
import d5_authority_store as store

_SWARM_DIR = skill_paths.sibling_skill_root(
    Path(__file__).absolute().parents[1], "hanig-orchestrate", "hanig-swarm")
sys.path.insert(0, str(_SWARM_DIR / "scripts"))
import coordinator_paths as CP
import child_environment as CE

AuthorityError = authority.AuthorityError
_ENTRY_FIELDS = {"registration_id", "repo_key", "plan_id", "state_key",
                 "checkout", "git_common_dir", "state_dir", "record_dir"}


def d5_active():
    """U8 owns activation. There is no runtime override."""
    return False


def _require(condition, message):
    if not condition:
        raise AuthorityError("registry: " + message)


def _active():
    _require(d5_active(), "D5 is not active")


def _plan_id(value):
    _require(type(value) is str and
             re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is not None,
             "invalid plan_id")


def _key(info):
    return (info.st_dev, info.st_ino)


def _git(checkout, *args):
    return subprocess.run(
        ["git", "-C", str(checkout), *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        env=CE.child_env(), timeout=30, check=True).stdout


def _git_path(checkout, option):
    """Read one Git path without stripping path bytes."""
    raw = _git(checkout, "rev-parse", "--path-format=absolute", option)
    _require(raw.endswith(b"\n"), "invalid Git path")
    path = Path(os.fsdecode(raw[:-1]))
    _require(path.is_absolute(), "Git path must be absolute")
    return path


def _worktrees(checkout):
    """Include the checkout omitted by separate-Git-directory listings."""
    raw = _git(checkout, "worktree", "list", "--porcelain", "-z")
    return [_git_path(checkout, "--show-toplevel")] + [
        Path(os.fsdecode(field[9:])) for field in raw.split(b"\0")
        if field.startswith(b"worktree ")]


def _outside(path, protected):
    path = path.resolve()
    for parent in (path, *path.parents):
        info = store.present(parent, "containment")
        _require(not info or _key(info) not in protected,
                 "authority root/state path is inside operated Git storage")


@contextmanager
def _identity(checkout, plan_id, state_dir):
    _plan_id(plan_id)
    _require(state_dir is not None, "state_dir is required and must exist")
    try:
        checkout = Path(checkout).absolute()
        state, _, _ = CP.resolve_paths(state_dir=state_dir, cwd=checkout)
        worktrees = _worktrees(checkout)
        root = CP._external_base(worktrees) / "hanig-authority" / "v1"
        common = _git_path(checkout, "--git-common-dir")
        repo_fd = os.open(str(common), os.O_RDONLY | os.O_DIRECTORY)
        with store.cleanup(os.close, repo_fd):
            repo_key = _key(os.fstat(repo_fd))
            protected = {repo_key} | {_key(wt.stat()) for wt in worktrees if wt.is_dir()}
            for path in (root, state, root / "plans"):
                _outside(path, protected)
            state_fd = os.open(str(state), os.O_RDONLY | os.O_DIRECTORY)
            with store.cleanup(os.close, state_fd):
                repo_info, state_info = os.fstat(repo_fd), os.fstat(state_fd)
                yield root, {
                    "repo_key": [repo_info.st_dev, repo_info.st_ino],
                    "plan_id": plan_id,
                    "state_key": [state_info.st_dev, state_info.st_ino],
                    "checkout": str(checkout), "git_common_dir": str(common),
                    "state_dir": str(state)}, state_fd, protected
    except (OSError, subprocess.SubprocessError, CP.PathPolicyError) as exc:
        raise AuthorityError("registry: filesystem/git operation failed; "
                             "durability is unconfirmed: {}".format(exc)) from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON field " + key)
        result[key] = value
    return result


def _entries(root):
    path = root / "registry.json"
    if not store.present(path, "registry"):
        return []
    try:
        value = json.loads(store.read_bytes(path).decode("utf-8"),
                           object_pairs_hook=_pairs)
    except (ValueError, RecursionError) as exc:
        raise AuthorityError("registry: invalid JSON: {}".format(exc)) from exc
    _require(type(value) is dict and set(value) == {"schema_version", "entries"},
             "invalid registry fields")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1,
             "invalid schema_version")
    entries = value["entries"]
    _require(type(entries) is list, "entries must be a list")
    ids, plans, states = set(), set(), set()
    for entry in entries:
        _require(type(entry) is dict and set(entry) == _ENTRY_FIELDS,
                 "invalid entry fields")
        rid = entry["registration_id"]
        _require(type(rid) is str and re.fullmatch(r"[0-9a-f]{32}", rid) is not None,
                 "invalid registration_id")
        _plan_id(entry["plan_id"])
        for field in ("repo_key", "state_key"):
            key = entry[field]
            _require(type(key) is list and len(key) == 2 and
                     all(type(n) is int and n >= 0 for n in key), "invalid " + field)
        for field in ("checkout", "git_common_dir", "state_dir", "record_dir"):
            locator = entry[field]
            _require(type(locator) is str and "\0" not in locator and
                     Path(locator).is_absolute(), "invalid " + field)
        _require(entry["record_dir"] == str(root / "plans" / rid),
                 "record_dir is not the registered authority directory")
        plan, state = (tuple(entry["repo_key"]), entry["plan_id"]), tuple(entry["state_key"])
        _require(rid not in ids and plan not in plans and state not in states,
                 "duplicate registration identity")
        ids.add(rid)
        plans.add(plan)
        states.add(state)
    return entries


def _find(entries, identity, state_fd):
    for entry in entries:
        if (entry["repo_key"], entry["plan_id"]) == (identity["repo_key"], identity["plan_id"]):
            # Re-verify the open object, never the caller's or stored path spelling.
            info = os.fstat(state_fd)
            _require(entry["state_key"] == [info.st_dev, info.st_ino],
                     "state_key mismatch: state directory replaced or conflicting")
            return entry
    _require(not any(e["state_key"] == identity["state_key"] for e in entries),
             "state_key already registered to another plan")
    return None


def _mkdir(path, base, protected):
    """Confirm owned links on every attempt, stopping at the external base."""
    _outside(path, protected)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in path.parents:
        store.sync_directory(parent)
        if parent == base:
            break


@contextmanager
def _locked(root):
    fd = os.open(str(root / "registry.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with store.cleanup(os.close, fd):
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield


def register(checkout, plan_id, state_dir):
    """Register one (repo object, plan) with one state object, or return it intact."""
    _active()
    with _identity(checkout, plan_id, state_dir) as (root, identity, state_fd, protected):
        base = root.parent.parent
        _mkdir(root, base, protected)
        with _locked(root):
            entries = _entries(root)
            existing = _find(entries, identity, state_fd)
            if existing is not None:
                # A visible entry may follow a failed publication fsync. Confirm
                # its bytes and every directory link without rewriting anything.
                fd = os.open(str(root / "registry.json"), os.O_RDONLY | os.O_NOFOLLOW)
                with store.cleanup(os.close, fd):
                    os.fsync(fd)
                directory = Path(existing["record_dir"])
                for path in (directory, root / "plans", root, root.parent, base):
                    store.sync_directory(path)
                return existing
            rid = uuid.uuid4().hex
            directory = root / "plans" / rid
            _mkdir(directory.parent, base, protected)
            _outside(directory, protected)
            directory.mkdir(mode=0o700)  # Never reuse a pre-existing record root.
            store.sync_directory(directory.parent)
            entry = dict(identity, registration_id=rid, record_dir=str(directory))
            raw = (json.dumps({"schema_version": 1, "entries": entries + [entry]},
                              sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
            store.write_bytes(root / "registry.json", raw)
            return entry


def lookup(checkout, plan_id, state_dir):
    """Read an atomic registry snapshot; absent registration returns None."""
    with _identity(checkout, plan_id, state_dir) as (root, identity, state_fd, _):
        return _find(_entries(root), identity, state_fd)
