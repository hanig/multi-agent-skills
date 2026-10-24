"""Inert D5 registration library; no production callers or CLI.

Local filesystem, trusted same-UID writers. Filesystem objects identify a
repository and state directory; paths are locators, never identity. Atomic
registry snapshots permit nonmutating lookup without creating a lock.
The permanent registry lock serializes registration writes.
No custody, enforcement, migration or protection from outside-lock writers.

Registration checks containment by object identity. Both operations require
Git 2.36 or later and a standard repository layout. Lookup and idempotent
registration check owned directories for Git entries and record identity;
this module remains inert until activation.

Registry schema 2 includes record_key. Fact: register has refused while D5
is inactive since U1b-1 merged (PR 267), and D5 has never been active.
Existing schema-1 files were written by tests or by hand; none can hold a grant.
Non-claim: a schema-1 registry is not migrated.

Non-claims: path-string equivalence; equivalence across clones or copies;
persistence across inode recreation; protection against malicious same-UID
replacement; TOCTOU against a writer outside the lock.
- **Bind mounts or other aliases** of an operated worktree's descendants. The supported host is macOS, without bind mounts.
- **Recreated directories whose `(st_dev, st_ino)` the filesystem reuses.**
- **A race against a concurrent same-UID writer.**
- Substitution between the directory walk and a later use of the record root.
- **A separate Git directory named `.git`.** Git reports its parent as the main worktree and records the original checkout nowhere, so it is indistinguishable from a standard repository.
- **An unlisted alias.** Containment does not cover a checkout that reaches the common directory through a `.git` file but is absent from Git's worktree list.

Non-claim: if an earlier failed call created B itself, a later retry does not re-confirm B's link in its parent. The external base is coordinator infrastructure (`~/.local/state`) that normally pre-exists.

Tamper-walk non-claims:
- The envelope gate (U1b-1c) may stat D before the walk.
- A concurrent same-UID substitution between two `lstat` calls (TOCTOU).
- Components above R are not checked by the walk.
- Identity equality does not detect a recreated directory that the filesystem gives the same `(st_dev, st_ino)` (inode reuse).
- Registration needs read permission on R and `plans` for the OD4 directory fsyncs.
- The new-registration path, which creates D and then takes `record_key`, is outside C6 and C7.
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
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
import d5_activation

AuthorityError = authority.AuthorityError
_ENTRY_FIELDS = {"registration_id", "repo_key", "plan_id", "state_key",
                 "checkout", "git_common_dir", "state_dir", "record_dir", "record_key"}


def d5_active():
    """U8 owns activation. There is no runtime override."""
    return d5_activation.d5_active()


def _require(condition, message):
    if not condition:
        raise AuthorityError("registry: " + message)


def _active():
    _require(d5_active(), "D5 is not active")


def _plan_id(value):
    _require(type(value) is str and
             re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is not None,
             "invalid plan_id")


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


def _key(info):
    return (info.st_dev, info.st_ino)


def _worktrees(checkout):
    raw = _git(checkout, "worktree", "list", "--porcelain", "-z")
    first_record = raw.split(b"\0\0", 1)[0].split(b"\0")
    _require(b"bare" not in first_record, "unsupported repository layout")
    fields = raw.split(b"\0")
    paths = [Path(os.fsdecode(field[9:])) for field in fields
             if field.startswith(b"worktree ")]
    _require(paths and all(p.is_absolute() for p in paths),
             "unsupported repository layout")
    return paths


def _worktree_keys(paths):
    """Follow listed targets; absent targets have no identity to protect."""
    for path in paths:
        try:
            info = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue  # ENOENT/ENOTDIR: no target; this does not hide access errors.
        yield _key(info)  # Unknown lookup errors propagate, never count as absent.


def _envelope(checkout):
    """Refuse unsupported Git/layouts before touching authority or state."""
    try:
        version = re.match(rb"git version ([0-9]+)\.([0-9]+)(?:\D|$)",
                           _git(checkout, "--version"))
        _require(version and tuple(map(int, version.groups())) >= (2, 36),
                 "unsupported Git version")
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthorityError("registry: unsupported Git version") from exc
    try:
        common = _git_path(checkout, "--git-common-dir")
        info = store.present(common, "unsupported repository layout")
        _require(common.name == ".git" and info and stat.S_ISDIR(info.st_mode),
                 "unsupported repository layout")
        worktrees = _worktrees(checkout)
        _require(_key(worktrees[0].stat()) == _key(common.parent.stat()),
                 "unsupported repository layout")
        checkout_key = _key(_git_path(checkout, "--show-toplevel").stat())
        _require(checkout_key in _worktree_keys(worktrees),
                 "unsupported repository layout")
        return common, worktrees
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthorityError("registry: unsupported repository layout") from exc


def _outside(path, protected, label="authority"):
    """Walk actual ancestors; only their filesystem identities decide."""
    path = path.resolve()
    for parent in (path, *path.parents):
        info = store.present(parent, "containment")
        _require(not info or _key(info) not in protected,
                 "authority path is inside operated Git storage: " + label +
                 " is inside protected storage")


@contextmanager
def _identity(checkout, plan_id, state_dir, containment=False):
    _plan_id(plan_id)
    try:
        checkout = Path(checkout).absolute()
        common, worktrees = _envelope(checkout)
        _require(state_dir is not None, "state_dir is required and must exist")
        root = CP._external_base(worktrees) / "hanig-authority" / "v1"
        _directory(root)
        _directory(root / "plans")
        protected = set()
        if containment:
            protected = set(_worktree_keys([common] + worktrees))
            for path in (root, root / "plans"):
                _outside(path, protected)
            _outside(Path(state_dir).expanduser(), protected, "state-dir")
        state, _, _ = CP.resolve_paths(state_dir=state_dir, cwd=checkout)
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
    version = value["schema_version"]
    _require(type(version) is int, "invalid schema_version")
    _require(version != 1, "unsupported registry schema 1, written before tamper checks; "
             "D5 was never active, so it holds no grants: remove registry.json "
             "and register again")
    _require(version == 2, "invalid schema_version")
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
        for field in ("repo_key", "state_key", "record_key"):
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


def _find(root, entries, identity, state_fd):
    for entry in entries:
        if (entry["repo_key"], entry["plan_id"]) == (identity["repo_key"], identity["plan_id"]):
            # Re-verify the open object, never the caller's or stored path spelling.
            info = os.fstat(state_fd)
            _require(entry["state_key"] == [info.st_dev, info.st_ino],
                     "state_key mismatch: state directory replaced or conflicting")
            _check_record(root, entry)
            return entry
    _require(not any(e["state_key"] == identity["state_key"] for e in entries),
             "state_key already registered to another plan")
    return None


def _record_key(root, registration_id):
    """Check R/P/D top-down by no-follow lstat; open and create nothing."""
    for path in (root, root / "plans", root / "plans" / registration_id):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(path))
        try:
            os.lstat(path / ".git")
        except FileNotFoundError:
            pass
        else:
            raise AuthorityError("authority storage became a Git worktree")
    return list(_key(info))


def _check_record(root, entry):
    _require(_record_key(root, entry["registration_id"]) == entry["record_key"],
             "record_key mismatch: record directory replaced")


def _directory(path):
    info = store.present(path, str(path))
    _require(not info or not stat.S_ISLNK(info.st_mode), str(path) + ": symlink")
    return info


def _mkdir_base(path):
    """Confirm parents only for external infrastructure created by this call."""
    if store.present(path, str(path)):
        return
    _mkdir_base(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        return  # Another trusted coordinator created it; this call did not.
    store.sync_directory(path.parent)


@contextmanager
def _locked(root):
    fd = os.open(str(root / "registry.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with store.cleanup(os.close, fd):
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield


def register(checkout, plan_id, state_dir):
    """Confirm an installed registration; ordinary entry cannot provision."""
    _active()
    _plan_id(plan_id)
    import d5_readiness as readiness
    ready = readiness.require(state_dir)
    readiness.bound(ready, lookup(checkout, plan_id, state_dir))
    # The existing idempotent branch confirms the exact directory/file chain.
    return stage(checkout, plan_id, state_dir)


def stage(checkout, plan_id, state_dir):
    """Installer scaffolding only; never retarget an existing registration."""
    import d5_readiness as readiness
    with _identity(checkout, plan_id, state_dir, containment=True) as (
            root, identity, state_fd, protected):
        # OD6: R/P checks precede the registry read and this registration's D
        # check. Preflight D before even the infrastructure's first mkdir.
        existing = _find(root, _entries(root), identity, state_fd)
        rid = existing["registration_id"] if existing else uuid.uuid4().hex
        directory = root / "plans" / rid
        info = _directory(directory) if existing is None else None
        if existing is not None or info:
            _outside(directory, protected)
        _require(not info, str(directory) + ": already exists")
        with readiness.installer_lease(identity["state_dir"]):
            base = root.parent.parent
            _mkdir_base(base)
            root.parent.mkdir(mode=0o700, exist_ok=True)
            root.mkdir(mode=0o700, exist_ok=True)
            with _locked(root):
                # Another registration may have published during preflight.
                entries = _entries(root)
                existing = _find(root, entries, identity, state_fd)
                plans = root / "plans"
                if existing is None:
                    plans.mkdir(mode=0o700, exist_ok=True)
                    _require(not _directory(directory), str(directory) + ": already exists")
                    directory.mkdir(mode=0o700)  # Never reuse a record root.
                    record_key = _record_key(root, rid)
                # OD4: confirm the fixed chain on every call. R is synced once below:
                # by the atomic registry write, or explicitly on idempotent retry.
                for path in (base, root.parent, plans):
                    store.sync_directory(path)
                if existing is not None:
                    fd = os.open(str(root / "registry.json"), os.O_RDONLY | os.O_NOFOLLOW)
                    with store.cleanup(os.close, fd):
                        os.fsync(fd)
                    store.sync_directory(root)
                    return existing
                entry = dict(identity, registration_id=rid, record_dir=str(directory),
                             record_key=record_key)
                raw = (json.dumps({"schema_version": 2, "entries": entries + [entry]},
                                  sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
                store.write_bytes(root / "registry.json", raw)
                return entry


def lookup(checkout, plan_id, state_dir):
    """Read an atomic registry snapshot; absent registration returns None."""
    with _identity(checkout, plan_id, state_dir) as (root, identity, state_fd, _):
        return _find(root, _entries(root), identity, state_fd)
