"""Inert D5 lease management; no production callers, CLI or authorization.

Registration owns placement and identity; the record store owns write locking
and replacement rules. Status reads atomic snapshots without creating locks
or persisting invalidation. Its evaluation is diagnostic only.
"""
from pathlib import Path
import uuid

import d5_authority as authority
import d5_authority_store as store
import d5_registry


def _active():
    if not d5_registry.d5_active():
        raise d5_registry.AuthorityError("D5 is not active")


def _registered(checkout, plan_id, state_dir):
    entry = d5_registry.lookup(checkout, plan_id, state_dir)
    if entry is None:
        raise d5_registry.AuthorityError("not registered")
    return entry


def grant(checkout, plan_id, state_dir, *, issued_at, confirmed_at, expires_at,
          mandate_bytes, scope, owner_response, displayed_bounds):
    """Create or replace a validated grant with exact owner confirmation."""
    _active()
    record = {"schema_version": 1, "grant_id": uuid.uuid4().hex,
              "issued_at": issued_at, "confirmed_at": confirmed_at,
              "expires_at": expires_at, "session_cap": 1,
              "mandate_sha256": authority.mandate_digest(mandate_bytes),
              "scope": scope, "confirmation": {"owner_response": owner_response,
                                               "displayed_bounds": displayed_bounds},
              "custody": None, "invalidation": None}
    authority.validate(record)
    entry = _registered(checkout, plan_id, state_dir)
    directory = Path(entry["record_dir"])
    operation = (store.replace if store.present(directory / store.RECORD_NAME, "record")
                 else store.create)
    return operation(directory, record)


def revoke(checkout, plan_id, state_dir, now, reason):
    """Require activation and registration before the store's revocation."""
    _active()
    entry = _registered(checkout, plan_id, state_dir)
    return store.revoke(entry["record_dir"], now, reason)


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
    return {"state": "active" if d5_registry.d5_active() else "inactive",
            "registration": entry, "record": record, "record_state": state}
