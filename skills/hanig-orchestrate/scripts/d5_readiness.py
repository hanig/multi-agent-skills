"""Explicit local installation infrastructure for the restricted synchronous merge profile.

Same-host trusted writers and stable lock inodes only. The reader requires the
caller-held coordinator lease. Installation uses that lease,
then existing exclusive DRAIN/START; no deployed durability claim (U8).
Installation generations are separate from prospective authority grants.
"""
from contextlib import contextmanager
import hashlib
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
import swarm_state as coordinator_state
import d5_installation as installation_state

MARKER = "d5-readiness.json"
INSTALLATION = "installation.json"
SENTINEL = "d5-installed.json"
NOT_INSTALLED, READY, BROKEN = "NOT_INSTALLED", "READY", "BROKEN"
PROFILE = "synchronous-merge-v1"
BINDING = ("registration_id", "plan_id", "state_dir", "record_dir",
           "repo_key", "state_key", "record_key")
# First activation accepts only quiescent schema-1 snapshots. DONE remains
# historical state, never a D5 admission. An absent snapshot is installation
# scaffolding only; the merge operator still requires recorded authority.
PREPARED_UNIT_STATES = (None, "READY_FOR_PR", "DONE", "ABANDONED", "SUPERSEDED")


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


def _state_directory(directory):
    _require(directory is not None, "state_dir is required and must exist")
    try:
        return Path(directory).expanduser().resolve()
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        raise registry.AuthorityError("D5 readiness: invalid state_dir") from exc


def lease_required(directory):
    directory = _state_directory(directory)
    _require(coordinator_state.holds_lease(directory), "caller-held coordinator lease required")
    return directory


def _read(path):
    try:
        # No registry lookup or subprocess. Refuse special files before reading.
        _require(stat.S_ISREG(path.lstat().st_mode), "nonregular installation state")
        return json.loads(path.read_bytes(), object_pairs_hook=registry._pairs)
    except (OSError, ValueError, RecursionError) as exc:
        raise registry.AuthorityError("D5 readiness: missing or malformed installation state") from exc


def prepared_state(state_dir):
    """Read eligibility without normalization, migration, subprocesses or writes.

    Prepare legacy state while inactive before installation: retire an inline
    epoch by the ordinary save path, settle live attempts, and clear dry-run
    placeholders by ordinary advancement. Never rewrite these here. Missing
    clocks and historical receipts remain unchanged audit data; they confer
    no admission. Read-only status may still inspect ineligible legacy state.
    """
    directory = _state_directory(state_dir)
    _require(directory.is_dir(), "prepared state directory must exist")
    path = directory / coordinator_state.STATE_FILE
    if not os.path.lexists(path):
        return None
    value = _read(path)
    _require(type(value) is dict and type(value.get("schema_version")) is int
             and value["schema_version"] == 1 and type(value.get("units")) is dict
             and "epoch" not in value, "incompatible prepared state schema or inline epoch")
    for uid, unit in value["units"].items():
        _require(type(uid) is str and bool(uid) and type(unit) is dict
                 and "state" in unit and unit["state"] in PREPARED_UNIT_STATES,
                 "incompatible prepared state unit; settle while inactive")
        job = unit.get("job_id")
        _require(job is None or (type(job) is str and not job.startswith("dry-")),
                 "incompatible prepared state job; clear placeholders while inactive")
        _require(unit["state"] is not None or
                 (unit.get("attempt_dir") is None and job is None),
                 "incompatible prepared state unjudged attempt")
    return value


def _validated(value):
    try:
        return installation_state._validated(value)
    except ValueError as exc:
        raise registry.AuthorityError(str(exc)) from exc


def installation(directory):
    try:
        return installation_state.installation(directory)
    except ValueError as exc:
        raise registry.AuthorityError(str(exc)) from exc


def state(state_dir):
    return installation_state.state(state_dir)


def refuse_broken(state_dir):
    """Refuse a damaged installation before a consumer can invoke Git."""
    _require(state(state_dir) != BROKEN,
             "installation is BROKEN; repair with d5_install promote")


def require(state_dir):
    """The same validator, under the live lease; never publish readiness."""
    directory = lease_required(state_dir)
    classification, value = installation_state._snapshot(directory)
    _require(classification == READY,
             "installation is " + classification + "; repair with d5_install promote")
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
    directory = _state_directory(state_dir)
    if _INSTALLER_PID != os.getpid():
        _reset_installers()
    with _INSTALLER_GUARD:
        owner = _INSTALLERS.setdefault(directory, dict(
            pid=os.getpid(), count=0, acquired=False, body=threading.RLock()))
        if owner["count"] == 0:
            borrowed = coordinator_state.holds_lease(directory)
            if not borrowed:
                _require(not protocol.locks._held(), "cannot acquire lease under DRAIN or START")
                _require(directory.is_dir(), "state directory must exist")
                ok, reason = coordinator_state.acquire_lease(directory, bump_epoch=False)
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
                            coordinator_state.release_lease(directory)
                    finally:
                        del _INSTALLERS[directory]


def successor_gate(entry):
    """Read both unresolved sources under lease, exclusive DRAIN and START.

    Reuse R4's strict ledger decoder and merge_unit's read-only resolution
    traversal. No intent repair, ledger creation, or readiness substitution.
    """
    import d5_effect_log as ledger
    import merge_unit
    directory = lease_required(entry["state_dir"])
    prepared_state(directory)
    record_dir = Path(entry["record_dir"])
    if os.path.lexists(record_dir / ledger.DIRECTORY):
        with ledger._access(record_dir) as (record, fd):
            for name in sorted(os.listdir(fd)):
                if name.endswith(".json"):
                    row = ledger._load(fd, name, record)
                    _require(row["state"] not in ("attempting", "unknown", "submitted"),
                             "unresolved effect ledger")
    try:
        # Pending cancellation may be the only surviving intent copy.
        # glob silently skips an unreadable directory; an inventory error
        # must refuse before withdrawal, not masquerade as no pending work.
        names = os.listdir(directory)
        paths = set(directory / name for name in names
                    if name.startswith("merge-unit-") and name.endswith(".json"))
        paths.update((directory / name).with_suffix(".json") for name in names
                     if name.startswith("merge-unit-") and name.endswith(".cancellation-pending"))
        paths.update(directory / ("merge-unit-" + name[len("merge-abandonment-"):])
                     for name in names
                     if name.startswith("merge-abandonment-") and name.endswith(".json"))
        for path in sorted(paths):
            source = path if path.exists() else path.with_suffix(".cancellation-pending")
            if not source.exists():
                source = directory / ("merge-abandonment-" + path.name[len("merge-unit-"):])
                value = merge_unit.read_object(source)["intent"]
            else:
                value = merge_unit.read_object(source)
            _require(path.name == "merge-unit-" + value["operation_id"] + ".json",
                     "merge intent filename mismatch")
            _, current_path, current = merge_unit.current_intent(
                directory, value["binding"], value["root"], repair=False)
            # Follow the already validated resolution chain, including the
            # pending rollback view, to bind every inventoried source to it.
            operation = hashlib.sha256(json.dumps(value["binding"], sort_keys=True).encode()).hexdigest()
            for _ in range(len(paths) + 1):
                member = directory / ("merge-unit-" + operation + ".json")
                if member == path:
                    break
                _require(member != current_path, "unreachable merge intent")
                prior = member.with_suffix(".cancellation-pending")
                if not prior.exists() or member.with_suffix(".cancellation-committed").exists():
                    prior = member
                phase = merge_unit.read_object(prior).get("phase")
                suffix = ":after-cancellation" if phase == "cancelled_before_request" else ":after-abandonment"
                operation = hashlib.sha256((operation + suffix).encode()).hexdigest()
            else:
                _require(False, "unreachable merge intent")
            _require(current is None or current.get("phase") == "receipt_recorded",
                     "unresolved merge intent")
    except registry.AuthorityError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise registry.AuthorityError("D5 readiness: unresolved or malformed merge intent") from exc


def publish(path, value):
    store.write_bytes(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def withdraw(directory):
    """Withdraw before any installation/authority commit; caller owns all locks."""
    try:
        (directory / MARKER).unlink()
    except FileNotFoundError:
        pass
    store.sync_directory(directory)

def publish_installation(entry):
    """Commit a fresh generation and binding, then publish readiness last."""
    value = dict((key, entry[key]) for key in BINDING)
    value.update(schema_version=1, generation=uuid.uuid4().hex, profile=PROFILE)
    _validated(value)
    publish(Path(entry["state_dir"]) / SENTINEL, value)
    publish(Path(entry["record_dir"]) / INSTALLATION, value)
    publish(Path(entry["state_dir"]) / MARKER, value)
    return value


def promote(checkout, plan_id, state_dir):
    """Installer-only publication; never issue a grant, claim custody or dispatch."""
    # Refuse even before creating a coordinator lock. Recheck under the lease
    # in successor_gate before withdrawing an existing installation.
    prepared_state(state_dir)
    with installer_lease(state_dir) as directory:
        entry = registry.lookup(checkout, plan_id, directory)
        _require(entry is not None, "not registered; stage first")
        with protocol.pin(entry["record_dir"]) as record:
            with record.drain(shared=False), record.start():
                successor_gate(entry)
                withdraw(directory)
                return publish_installation(entry)
