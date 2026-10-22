"""Inert U3-R2 controls; no admission, CLI or activation change.

Supported envelope and accepted limits
-------------------------------------
P: an existing same-UID 0700 record directory, stable lock inodes and trusted
ancestry. The merged opener may repair owner permissions in place to 0600;
no directory chmod or lock replacement. Refusals may leave protocol lock files.
Resource/open failures are permitted; no legacy error/descriptor compatibility.
Direct store calls and uncontrolled mandate-file edits are not serialized here.

The mandate source is the registered checkout's working-tree file. This unit
does not compare it with the owner-merged blob on the target branch, so a
checkout that has not fast-forwarded to a newly merged mandate keeps evaluating
its older bytes until it does. The checkout's ancestry is trusted, as in P.
"""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import stat

import d5_authority as authority
import d5_authority_store as store
import d5_host as host
import d5_protocol_locks as protocol
import d5_registry as registry


@contextmanager
def _started(checkout, plan_id, state_dir):
    if not registry.d5_active():
        raise authority.AuthorityError("D5 is inactive")
    entry = registry.lookup(checkout, plan_id, state_dir)
    if entry is None:
        raise authority.AuthorityError("not registered")
    with protocol.pin(entry["record_dir"]) as record:
        with record.start():
            yield entry


def _mandate_bytes(entry):
    path = Path(entry["checkout"]) / "docs/orchestrator-mandate.md"
    # NONBLOCK lets a FIFO reach the regular-file refusal without a writer.
    with ExitStack() as stack:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        stack.callback(os.close, fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise authority.AuthorityError("D5 mandate must be a regular file")
        handle = stack.enter_context(os.fdopen(fd, "rb", closefd=False))
        return handle.read()


def revoke(checkout, plan_id, state_dir, now, reason):
    """Serialize the unchanged store's revocation under START, without DRAIN."""
    with _started(checkout, plan_id, state_dir) as entry:
        return store.revoke(entry["record_dir"], now, reason)


def record_mandate_invalidation(checkout, plan_id, state_dir, *, now):
    """Read exact current registered bytes under START; return store.check state.

    Missing, unreadable, symlinked and non-regular sources refuse before check.
    A matching mandate need not invalidate; store.check owns that decision.
    """
    with _started(checkout, plan_id, state_dir) as entry:
        mandate = _mandate_bytes(entry)
        return store.check(entry["record_dir"], now, mandate)


def relinquish(checkout, plan_id, state_dir, *, now):
    """Inspect the native session anchor and relinquish under START."""
    with _started(checkout, plan_id, state_dir) as entry:
        anchor = host.session_anchor()
        if isinstance(anchor, host.Unknown):
            raise authority.AuthorityError(anchor.reason)
        session_id, identity = anchor
        return store.relinquish(entry["record_dir"], now, session_id, identity)
