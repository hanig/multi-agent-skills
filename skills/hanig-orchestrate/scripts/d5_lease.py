"""D5 lease management; no production callers, CLI or authorization.

Registration owns placement and identity; R3/R2 serialize active mutations,
and the record store owns replacement rules. Status reads atomic snapshots without creating locks
or persisting invalidation. Its evaluation is diagnostic only.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import uuid

import d5_authority as authority
import d5_authority_store as store
import d5_control
import d5_install
import d5_registry
import d5_readiness as readiness


def _active(state_dir):
    state_dir = readiness.refuse_broken(state_dir)
    if not d5_registry.d5_active(state_dir):
        raise d5_registry.AuthorityError("D5 is not active")


def _registered(checkout, plan_id, state_dir):
    entry = d5_registry.lookup(checkout, plan_id, state_dir)
    if entry is None:
        raise d5_registry.AuthorityError("not registered")
    return entry


@contextmanager
def _grant_locked(directory):
    """Refuse overlapping grant mutations; keep grant.lock permanent.

    This outer lock spans record publication through store-lock release.
    The store still owns the record lock and all replacement decisions.
    """
    try:
        fd = os.open(str(directory / "grant.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise d5_registry.AuthorityError(
                    "grant: concurrent operation already exists") from exc
            yield
        finally:
            os.close(fd)
    except OSError as exc:
        raise d5_registry.AuthorityError(
            "grant: filesystem operation failed; durability is unconfirmed: {}".format(exc)) from exc


def grant(checkout, plan_id, state_dir, *, issued_at, confirmed_at, expires_at,
          mandate_bytes, scope, owner_response, displayed_bounds):
    """Create or replace a validated grant with exact owner confirmation."""
    _active(state_dir)
    ready = readiness.require(state_dir)
    record = {"schema_version": 1, "grant_id": uuid.uuid4().hex,
              "issued_at": issued_at, "confirmed_at": confirmed_at,
              "expires_at": expires_at, "session_cap": 1,
              "mandate_sha256": authority.mandate_digest(mandate_bytes),
              "scope": scope, "confirmation": {"owner_response": owner_response,
                                               "displayed_bounds": displayed_bounds},
              "custody": None, "invalidation": None}
    authority.validate(record)
    for key, value in record["confirmation"].items():
        if not value or value.isspace():
            raise authority.AuthorityError("confirmation." + key + ": fresh confirmation required")
    entry = readiness.bound(ready, _registered(checkout, plan_id, state_dir))
    directory = Path(entry["record_dir"])
    operation = (d5_install.replace_authority
                 if store.present(directory / store.RECORD_NAME, "record")
                 else d5_install.install_initial)
    with _grant_locked(directory):
        if operation is d5_install.replace_authority:
            previous = store.read(directory)
            if confirmed_at <= previous["confirmed_at"]:
                raise authority.AuthorityError("grant: fresh confirmation time required")
        return operation(directory, record)


def revoke(checkout, plan_id, state_dir, now, reason):
    """Keep the inactive refusal; R2 resolves registration and serializes revoke."""
    _active(state_dir)
    return d5_control.revoke(checkout, plan_id, state_dir, now, reason)


def status(checkout, plan_id, state_dir, *, now, mandate_bytes):
    """Read a diagnostic snapshot, even while inactive; create nothing.

    A mandate change is reported but never persisted. This is not the durable
    invalidation check required before relying on a grant for authorization.
    """
    entry = d5_registry.lookup(checkout, plan_id, state_dir)
    record, state = None, "absent"
    if entry is not None:
        path = Path(entry["record_dir"]) / store.RECORD_NAME
        if store.present(path, "record"):
            record = authority.decode(store.read_bytes(path))
            state = authority.evaluate(record, now, mandate_bytes)[0]
    return {"state": "active" if d5_registry.d5_active(readiness.activation_directory(state_dir)) else "inactive",
            "registration": entry, "record": record, "record_state": state}
