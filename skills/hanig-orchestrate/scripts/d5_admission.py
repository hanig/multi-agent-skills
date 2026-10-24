"""Inert U3-R5 admission consumer; no submission API or production callers.

Supported envelope and accepted limits
-------------------------------------
P/H. The function is an internal part of the submission critical section, not
a standalone authorization API. Trusted local execution, stable record/lock
inodes and the registry's existing measured envelope are prerequisites. The
merged opener may repair owner permissions in place to 0600; no directory chmod
or lock-file replacement. Uncontrolled mandate edits and direct store mutations
are not serialized by this consumer. Unknown host information cannot admit.

The registered checkout stays at the path recorded at registration for the registration's lifetime. A renamed or moved registered checkout makes admission refuse, before any store or boundary call, with the missing registered mandate path in the error; the grant, custody and mandate bytes are unchanged. Operator recovery: restore the checkout to its registered absolute path; recovery remains subject to ordinary expiry and the other admission checks.

Non-claims: checking alone starts nothing and provides no later permission;
no claim about checks performed by unrelated callers.

The mandate source is the registered checkout's working-tree file. This unit
does not compare it with the owner-merged blob on the target branch, so a
checkout that has not fast-forwarded to a newly merged mandate keeps evaluating
its older bytes until it does.
"""
import json
import time
from types import MappingProxyType

import d5_authority as authority
import d5_authority_store as store
from d5_control import _mandate_bytes
import d5_host as host
import d5_locks as locks
import d5_registry as registry


# Closed trusted classification. The existing synthetic adapters are ledger
# effects; production merge admission awaits U4.3's journal binding. A scope,
# intent, serialized preference or receipt cannot override this table.
RECORDING_AUTHORITIES = MappingProxyType({
    "fixture": "ledger", "test-effect": "ledger", "synthetic": "ledger",
    "merge": "journal",
})


def recording_authority(kind):
    if type(kind) is not str or kind not in RECORDING_AUTHORITIES:
        raise authority.AuthorityError("D5 operation kind is not classified")
    return RECORDING_AUTHORITIES[kind]


def require_ledger(kind):
    if recording_authority(kind) != "ledger":
        raise authority.AuthorityError("D5 journal request requires journal binding")


def _scope_allowed(raw, allowed):
    # The operation's scope is immutable UTF-8 JSON bytes. Exact scalar strings
    # and set inclusion match authority.validate/replacement_allowed semantics.
    if type(raw) is not bytes:
        return False
    try:
        requested = json.loads(raw.decode("utf-8"), object_pairs_hook=authority._pairs)
        authority._keys(requested, authority._SCOPES, "scope")
        for key, items in requested.items():
            if type(items) is not list:
                return False
            for item in items:
                authority._string(item, "scope." + key)
            if len(items) != len(set(items)) or not set(items) <= set(allowed[key]):
                return False
    except (ValueError, RecursionError):
        return False
    return True


def _validate_locked(pinned, checkout, plan_id, state_dir, *, grant_id,
                     session_id, anchor, generation, scope):
    """Refuse or return None, within the caller's existing START ownership.

    The submission caller supplies an authority reference (grant ID and custody
    generation), its session/anchor, and immutable scope bytes from its operation
    intent. It owns DRAIN and START and prepares durable intent before this call;
    after return it must invoke the explicit boundary without intervening work
    or releasing START. No permission object, effect or callback is returned.
    Accurate effect/scope declaration and the boundary belong to that caller.

    R1's private pinned record/ownership view is reused to check directory inode
    and this thread's START. STATE is never pre-acquired here; the store owns it.
    """
    if not registry.d5_active():
        raise authority.AuthorityError("D5 is inactive")
    record = pinned._record
    if record.fd is None or not any(key == record.key and rank == 1
                                   for key, rank, _, _ in locks._held()):
        raise authority.AuthorityError("D5 admission requires START")
    entry = registry.lookup(checkout, plan_id, state_dir)
    if entry is None:
        raise authority.AuthorityError("not registered")
    if tuple(entry["record_key"]) != record.key:
        raise authority.AuthorityError("D5 authority identity mismatch")
    # Source failures must precede authority-store entry points, including read.
    mandate = _mandate_bytes(entry)
    current = store.read(entry["record_dir"])
    if current["grant_id"] != grant_id:
        raise authority.AuthorityError("D5 authority identity mismatch")
    verified = host.verify_anchor(session_id, anchor)
    custody = current["custody"]
    held = (custody is not None and custody["relinquished_at"] is None and
            type(generation) is int and custody["generation"] == generation and
            (custody["session_id"], host.ProcessIdentity(custody["anchor_pid"],
             custody["anchor_start_us"])) == (session_id, anchor))
    allowed = _scope_allowed(scope, current["scope"])
    # Sample after the local preparation/identity/scope work, not at pin time.
    now = int(time.time())
    state, _ = authority.evaluate(current, now, mandate)
    if state == "mandate_changed":
        state = store.check(entry["record_dir"], now, mandate)
    if state != "valid":
        raise authority.AuthorityError({
            "invalidated": "D5 authority is invalidated",
            "mandate_changed": "D5 mandate changed",
            "expired": "D5 authority expired",
        }[state])
    if not verified:
        raise authority.AuthorityError("D5 anchor is not verifiable")
    if not held:
        raise authority.AuthorityError("D5 custody is not current")
    if not allowed:
        raise authority.AuthorityError("D5 authority scope denied")
