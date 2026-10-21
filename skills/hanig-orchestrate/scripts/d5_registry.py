"""Inert D5 registration and management library; no production callers or CLI.

Local filesystem, trusted same-UID writers. Filesystem objects identify a
repository and state directory; paths are locators, never identity. Atomic
registry snapshots permit read-only lookup/status without creating a lock.
The permanent registry lock serializes registration and management writes.
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


def _inside(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


@contextmanager
def _identity(checkout, plan_id, state_dir):
    _plan_id(plan_id)
    _require(state_dir is not None, "state_dir is required and must exist")
    try:
        checkout = Path(checkout).absolute()
        state, _, worktrees = CP.resolve_paths(state_dir=state_dir, cwd=checkout)
        root = (CP._external_base(worktrees) / "hanig-authority" / "v1").resolve()
        _require(not any(_inside(root, wt) for wt in worktrees),
                 "authority root resolves inside an operated worktree")
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute",
             "--git-common-dir"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=CE.child_env(), timeout=30, check=True)
        _require(result.stdout.endswith(b"\n"), "invalid git common directory")
        common = Path(os.fsdecode(result.stdout[:-1]))
        _require(common.is_absolute(), "git common directory must be absolute")
        _require(not _inside(root, common.resolve()),
                 "authority root resolves inside the Git common directory")
        repo_fd = os.open(str(common), os.O_RDONLY | os.O_DIRECTORY)
        with store.cleanup(os.close, repo_fd):
            state_fd = os.open(str(state), os.O_RDONLY | os.O_DIRECTORY)
            with store.cleanup(os.close, state_fd):
                repo_info, state_info = os.fstat(repo_fd), os.fstat(state_fd)
                yield root, {
                    "repo_key": [repo_info.st_dev, repo_info.st_ino],
                    "plan_id": plan_id,
                    "state_key": [state_info.st_dev, state_info.st_ino],
                    "checkout": str(checkout), "git_common_dir": str(common),
                    "state_dir": str(state)}, state_fd
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


def _mkdir(path):
    """Persist each new directory link before publishing any entry using it."""
    if store.present(path, "directory"):
        _require(path.is_dir(), "expected directory: " + str(path))
        return
    _mkdir(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _require(path.is_dir(), "expected directory: " + str(path))
    store.sync_directory(path.parent)


@contextmanager
def _locked(root):
    fd = os.open(str(root / "registry.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with store.cleanup(os.close, fd):
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield


def register(checkout, plan_id, state_dir):
    """Register one (repo object, plan) with one state object, or return it intact."""
    _active()
    with _identity(checkout, plan_id, state_dir) as (root, identity, state_fd):
        _mkdir(root)
        with _locked(root):
            entries = _entries(root)
            existing = _find(entries, identity, state_fd)
            if existing is not None:
                return existing
            rid = uuid.uuid4().hex
            directory = root / "plans" / rid
            _mkdir(directory.parent)
            directory.mkdir(mode=0o700)  # Never reuse a pre-existing record root.
            store.sync_directory(directory.parent)
            entry = dict(identity, registration_id=rid, record_dir=str(directory))
            raw = (json.dumps({"schema_version": 1, "entries": entries + [entry]},
                              sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
            store.write_bytes(root / "registry.json", raw)
            return entry


def lookup(checkout, plan_id, state_dir):
    """Read an atomic registry snapshot; absent registration returns None."""
    with _identity(checkout, plan_id, state_dir) as (root, identity, state_fd):
        return _find(_entries(root), identity, state_fd)


@contextmanager
def _registered(checkout, plan_id, state_dir):
    with _identity(checkout, plan_id, state_dir) as (root, identity, state_fd):
        _require(store.present(root / "registry.json", "registry"), "not registered")
        with _locked(root):
            entry = _find(_entries(root), identity, state_fd)
            _require(entry is not None, "not registered")
            yield entry


def grant(checkout, plan_id, state_dir, *, issued_at, confirmed_at, expires_at,
          mandate_bytes, scope, owner_response, displayed_bounds):
    """Create or replace a grant, preserving exact confirmation and store rules."""
    _active()
    record = {"schema_version": 1, "grant_id": uuid.uuid4().hex,
              "issued_at": issued_at, "confirmed_at": confirmed_at,
              "expires_at": expires_at, "session_cap": 1,
              "mandate_sha256": authority.mandate_digest(mandate_bytes),
              "scope": scope, "confirmation": {"owner_response": owner_response,
                                               "displayed_bounds": displayed_bounds},
              "custody": None, "invalidation": None}
    authority.validate(record)
    with _registered(checkout, plan_id, state_dir) as entry:
        directory = Path(entry["record_dir"])
        operation = store.replace if store.present(directory / store.RECORD_NAME, "record") else store.create
        return operation(directory, record)


def revoke(checkout, plan_id, state_dir, now, reason):
    """Persist revocation using the record store's terminal transition."""
    _active()
    with _registered(checkout, plan_id, state_dir) as entry:
        return store.revoke(entry["record_dir"], now, reason)


def status(checkout, plan_id, state_dir, *, now, mandate_bytes):
    """Read-only snapshot. Evaluation never persists a mandate invalidation.

    The result is diagnostic, never an authorization decision. In particular,
    mandate_changed here is not the durable check performed by the store.
    """
    with _identity(checkout, plan_id, state_dir) as (root, identity, state_fd):
        entry = _find(_entries(root), identity, state_fd)
        record, state = None, "absent"
        if entry is not None:
            path = Path(entry["record_dir"]) / store.RECORD_NAME
            if store.present(path, "record"):
                record = authority.decode(store.read_bytes(path))
                state = authority.evaluate(record, now, mandate_bytes)[0]
        return {"state": "active" if d5_active() else "inactive",
                "registration": entry, "record": record, "record_state": state}
