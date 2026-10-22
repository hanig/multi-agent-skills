"""Inert U3-R1 protocol adapter; no effect admission or activation change.

Supported envelope and accepted limits
-------------------------------------
P: an existing same-UID 0700 record directory with stable ancestry and lock
inodes. The reused opener may repair owner permissions in place to 0600;
it neither chmods directories nor replaces lock files. Resource/open failures
are permitted. No legacy-store behavior-compatibility claim is made.
S: managed subprocesses explicitly inherit a held shared DRAIN via pass_fds.
Raw forks and uninstrumented effect-capable descendants are outside the envelope.

pin(D) yields lexical START and DRAIN contexts backed by d5_locks. START uses
authority.start.lock; DRAIN uses authority.drain.lock. Same-thread nesting
shares ownership, including across pins of the same directory inode. Contexts
must enter and exit in their owning thread, in lexical order. Do not transfer
an exclusive START description to another thread or process.

drain() yields a borrowed fd until exit. A managed child imports that held
shared description with drain(inherited_fd=fd); the importer duplicates it and
owns the duplicate, while the caller owns the original. Final close releases
protection. Import validates identity, not whether the source actually holds
SH; supplying an already-held shared description is the caller's obligation.
The protected bootstrap itself belongs to a later unit.

No new effect admission; no change to legacy store behavior; no protection for
arbitrary raw forks. STATE remains owned by the store/ledger, and is deliberately
absent from this surface. Use DRAIN before START; no lock timeout or theft.
Cleanup preserves exceptions escaping the context, not unrelated exceptions
unwinding through a caller's finally block in which the context finishes normally.
"""
from contextlib import contextmanager, ExitStack

import d5_locks as locks


LockError = locks.LockError
_REFUSALS = {
    locks._UPGRADE: "D5 drain upgrade is unsupported",
    "D5 lock refused: cross-record nesting": "D5 nested authority mismatch",
}


@contextmanager
def _acquire(guard):
    # Translate entry refusals, never exceptions raised by the caller's body.
    with ExitStack() as stack:
        try:
            value = stack.enter_context(guard)
        except locks.LockError as exc:
            message = _REFUSALS.get(str(exc))
            if message is None:
                raise
            raise LockError(message) from exc
        yield value


class _Protocol:
    def __init__(self, record):
        self._record = record

    def start(self):
        """Exclusive START, reentrant in this thread for this directory inode."""
        return _acquire(self._record.start())

    def drain(self, *, shared=True, inherited_fd=None):
        """Close-owned DRAIN; inherited_fd is a caller-owned held shared fd."""
        return _acquire(self._record.drain(shared=shared, inherited_fd=inherited_fd))


@contextmanager
def pin(directory):
    """Pin an already resolved participating D; does not validate authority."""
    with locks.pin(directory) as record:
        yield _Protocol(record)
