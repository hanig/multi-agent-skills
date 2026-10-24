"""Explicit local installation infrastructure for the restricted synchronous merge profile.

Same-host trusted writers and stable lock inodes only. The reader requires the
caller-held coordinator lease. Installation uses that lease,
then existing exclusive DRAIN/START; no deployed durability claim (U8).
Installation generations are separate from prospective authority grants.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid

import d5_registry as registry
import d5_authority_store as store
import d5_protocol_locks as protocol
import swarm_state as state

MARKER = "d5-readiness.json"
INSTALLATION = "installation.json"
PROFILE = "synchronous-merge-v1"
BINDING = ("registration_id", "plan_id", "state_dir", "record_dir",
           "repo_key", "state_key", "record_key")


def _reset_installers():
    # A fork can inherit a guard held by a thread that no longer exists.
    global _INSTALLER_PID, _INSTALLER_GUARD, _INSTALLERS
    _INSTALLER_PID = os.getpid()
    _INSTALLER_GUARD = threading.Lock()
    _INSTALLERS = {}


_reset_installers()
os.register_at_fork(after_in_child=_reset_installers)


def _require(ok, message):
    if not ok:
        raise registry.AuthorityError("D5 readiness: " + message)


def lease_required(directory):
    directory = Path(directory).resolve()
    _require(state.holds_lease(directory), "caller-held coordinator lease required")
    return directory


def _read(path):
    try:
        # No registry lookup or subprocess. Refuse special files before reading.
        _require(stat.S_ISREG(path.lstat().st_mode), "nonregular installation state")
        return json.loads(path.read_bytes(), object_pairs_hook=registry._pairs)
    except (OSError, ValueError, RecursionError) as exc:
        raise registry.AuthorityError("D5 readiness: missing or malformed installation state") from exc


def _validated(value):
    _require(type(value) is dict and set(value) == set(BINDING) | {
        "schema_version", "generation", "profile"}, "invalid installation fields")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1,
             "invalid installation schema")
    for key in ("generation", "registration_id"):
        _require(type(value[key]) is str and re.fullmatch(r"[0-9a-f]{32}", value[key]),
                 "invalid " + key)
    _require(value["profile"] == PROFILE, "unsupported route profile")
    registry._plan_id(value["plan_id"])
    for key in ("state_dir", "record_dir"):
        _require(type(value[key]) is str and Path(value[key]).is_absolute(), "invalid " + key)
        if key == "state_dir":
            _require(str(Path(value[key]).resolve()) == value[key], "noncanonical " + key)
    for key in ("repo_key", "state_key", "record_key"):
        pair = value[key]
        _require(type(pair) is list and len(pair) == 2 and
                 all(type(n) is int and n >= 0 for n in pair), "invalid " + key)
    return value


def require(state_dir):
    """Pure reader under the live lease; operational enforcement belongs to U4.0b."""
    directory = lease_required(state_dir)
    value = _validated(_read(directory / MARKER))
    _require(value["state_dir"] == str(directory), "state directory mismatch")
    installed = _validated(_read(Path(value["record_dir"]) / INSTALLATION))
    _require(value == installed, "installation generation or binding mismatch")
    for name, key in (("state_dir", "state_key"), ("record_dir", "record_key")):
        _require(list(registry._key(Path(value[name]).stat())) == value[key],
                 "installation object mismatch")
    return value


def bound(value, entry):
    _require(entry is not None, "not registered")
    _require(all(entry.get(k) == value[k] for k in BINDING), "registration binding mismatch")
    return entry


@contextmanager
def installer_lease(state_dir):
    """Count lease users, serialize bodies, and never release an outer lease.

    Count before waiting for the reentrant body lock: the last exiting context
    owns release, including when another installer is queued to enter its body.
    Only acquisition of the existing cross-process lease is nonblocking.
    """
    directory = Path(state_dir).resolve()
    if _INSTALLER_PID != os.getpid():
        _reset_installers()
    with _INSTALLER_GUARD:
        owner = _INSTALLERS.setdefault(directory, dict(
            pid=os.getpid(), count=0, acquired=False, body=threading.RLock()))
        if owner["count"] == 0:
            borrowed = state.holds_lease(directory)
            if not borrowed:
                _require(not protocol.locks._held(), "cannot acquire lease under DRAIN or START")
                _require(directory.is_dir(), "state directory must exist")
                ok, reason = state.acquire_lease(directory, bump_epoch=False)
                _require(ok, "coordinator lease unavailable: " + str(reason))
            owner["acquired"] = not borrowed
        owner["count"] += 1
    try:
        with owner["body"]:
            yield directory
    finally:
        # An inherited lexical context cannot release its parent's descriptor.
        if owner["pid"] == os.getpid():
            with _INSTALLER_GUARD:
                owner["count"] -= 1
                if owner["count"] == 0:
                    try:
                        if owner["acquired"]:
                            state.release_lease(directory)
                    finally:
                        del _INSTALLERS[directory]

def publish(path, value):
    store.write_bytes(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def promote(checkout, plan_id, state_dir):
    """Installer-only publication; never issue a grant, claim custody or dispatch."""
    with installer_lease(state_dir) as directory:
        entry = registry.lookup(checkout, plan_id, directory)
        _require(entry is not None, "not registered; stage first")
        with protocol.pin(entry["record_dir"]) as record:
            with record.drain(shared=False), record.start():
                marker = directory / MARKER
                try:
                    marker.unlink()
                except FileNotFoundError:
                    pass
                store.sync_directory(directory)
                value = dict((key, entry[key]) for key in BINDING)
                value.update(schema_version=1, generation=uuid.uuid4().hex, profile=PROFILE)
                _validated(value)
                publish(Path(entry["record_dir"]) / INSTALLATION, value)
                publish(marker, value)
                return value
