"""Inert U3-R4 completion capabilities; no production callers or activation.

Supported envelope and accepted limits
-------------------------------------
L: access to the existing ledger and its restricted receipt mechanism, with
shared DRAIN during accounting. Current authority validity is not required.
Uses the merged receipt and duplicate-completion semantics. No new recovery
daemon or receipt-reconstruction mechanism. Trusted local execution and stable
record/lock identities are prerequisites; this is not a same-UID sandbox.

bind() accepts an existing Attempt (uncertainty accounting) or Receipt
(completion or uncertainty accounting). The returned immutable capability is
neither a ledger Attempt nor a Receipt and exposes no submission transition.
The caller keeps its submission DRAIN through the effect/response lifetime.
Each accounting call acquires a shared R1 reference, duplicating the caller's
reference when nested, and closes it after the ledger returns or raises.
No START acquisition, authority lookup, or authority mutation occurs here.

Non-claims: no authority to retry, poll, reconcile remotely, or install custody.
Completion and uncertainty accounting do not survive deactivation; they are
deferred until D5 is active again, with the ledger entry retained.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field

import d5_effect_log as ledger
import d5_protocol_locks as protocol


MISMATCH = "D5 completion receipt mismatch"
TRANSITION = "D5 completion transition is not authorized"


@contextmanager
def _refusals():
    # Preserve durability, conflict and inactive errors, including their causes.
    try:
        yield
    except ledger.EffectError as exc:
        message = {ledger.INVALID: MISMATCH, ledger.NOT_SUBMITTED: TRANSITION}.get(str(exc))
        if message is None:
            raise
        raise ledger.EffectError(message) from exc


@dataclass(frozen=True)
class _Completion:
    _handle: ledger.Attempt = field(repr=False)

    def complete(self, outcome):
        """Finalize this submitted operation; an identical duplicate is allowed."""
        with _refusals(), protocol.pin(self._handle._directory) as record:
            with record.drain():
                return ledger.complete(self._handle, outcome)

    def uncertain(self):
        """Record unknown for this operation; never assert submission."""
        handle = self._handle
        attempt = ledger.Attempt(handle.effect_id, handle._directory,
                                 handle._binding, handle._secret)
        with _refusals(), protocol.pin(handle._directory) as record:
            with record.drain():
                return ledger._classify(attempt, "unknown")


def bind(handle, *, journal=None):
    """Bind an existing capability while active, without current authorization.

    The merged ledger reauthenticates the stored binding on subsequent calls.
    Construction reads the existing entry; it never prepares or classifies it.
    """
    ledger._active()
    if type(handle) not in (ledger.Attempt, ledger.Receipt) or type(handle._directory) is not str:
        raise ledger.EffectError(MISMATCH)
    with _refusals(), protocol.pin(handle._directory) as record:
        with record.drain(), ledger._checked(handle):
            if journal is not None:
                from d5_journal_completion import BoundJournal
                if type(journal) is not BoundJournal:
                    raise ledger.EffectError(MISMATCH)
                return journal.attach(handle)
            return _Completion(handle)
