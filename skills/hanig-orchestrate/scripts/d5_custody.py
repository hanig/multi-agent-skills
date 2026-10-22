"""Inert D5 custody API; no CLI, production callers or authorization.

Status reads atomic snapshots without locks, evaluation or writes. Neither
its holder match nor its liveness observation authorizes an operation.
"""
from pathlib import Path

import d5_authority as authority
import d5_authority_store as store
import d5_host
import d5_registry


def _claimant(checkout, plan_id, state_dir):
    if not d5_registry.d5_active():
        raise authority.AuthorityError("D5 is not active")
    entry = d5_registry.lookup(checkout, plan_id, state_dir)
    if entry is None:
        raise authority.AuthorityError("not registered")
    anchor = d5_host.session_anchor()
    if isinstance(anchor, d5_host.Unknown):
        raise authority.AuthorityError(anchor.reason)
    session_id, identity = anchor
    return entry["record_dir"], session_id, identity


def claim(checkout, plan_id, state_dir, *, now, mandate_bytes):
    """Require activation, registration and the native session anchor."""
    directory, session_id, identity = _claimant(checkout, plan_id, state_dir)
    return store.claim(directory, now, mandate_bytes, session_id, identity,
                       d5_host.liveness)


def relinquish(checkout, plan_id, state_dir, *, now):
    """Relinquishment disables this holder; it is not a live handoff."""
    directory, session_id, identity = _claimant(checkout, plan_id, state_dir)
    return store.relinquish(directory, now, session_id, identity)


def status(checkout, plan_id, state_dir, *, now):
    """Diagnostic snapshot even while inactive; now confers no time-based rights."""
    entry = d5_registry.lookup(checkout, plan_id, state_dir)
    custody, holder, is_holder = None, "unknown", False
    if entry is not None:
        path = Path(entry["record_dir"]) / store.RECORD_NAME
        if store.present(path, "record"):
            custody = authority.decode(store.read_bytes(path))["custody"]
    if custody is not None:
        identity = d5_host.ProcessIdentity(custody["anchor_pid"], custody["anchor_start_us"])
        holder = store.observe_liveness(identity, d5_host.liveness)
        is_holder = d5_host.session_anchor() == (custody["session_id"], identity)
    return {"state": "active" if d5_registry.d5_active() else "inactive",
            "registration": entry, "custody": custody, "liveness": holder,
            "is_holder": is_holder}
