"""Inert completion capabilities and U4.2-c source-specific provider contract.

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

OD7 amendment for the closed merge-journal class
----------------------------------------------
Preparation, classification, uncertainty binding and terminal completion belong
to ONE provider selected from admission's closed class before START. Results,
receipt tags and caller preferences cannot choose it. There is no journal-to-
ledger fallback. Production has no journal provider until U4.3.

For journals, C means durable completion evidence bound to the admitted operation,
its existing binding and installation/fencing identity. Intent journals carry
request/no-replay/uncertainty state; their merged label is NOT successful-
completion authority. Legacy merged/attested booleans are insufficient. U4.3's
provider must append a D5-bound receipt in process under the coordinator lease
and receipt append flock, sync the file (and directory on first creation), and
validate readback before complete() returns. A missing, malformed, mismatched or
unconfirmed receipt must raise. Process exit/reap/timeout and a not-merged forge
observation never establish C. Unknown remains unknown, with no redispatch.

The operator must pass independent readiness before lookup/execution and hold
the coordinator lease across examination, preparation, actual synchronous merge
and recording. prepare() durably records request/no-replay state at R6's existing
preparation point, before Boundary.start. Unresolved intent fences successor
installation; bound completion-only recovery must remain possible for the
installed generation without permitting a new merge. These are obligations of
U4.3's provider/operator, not implementations supplied by this inert interface.
"""
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType

import d5_admission as admission
from d5_authority import AuthorityError
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
        with _refusals():
            ledger._check_handle(self._handle)
        with _refusals(), protocol.pin(self._handle._directory) as record:
            with record.drain():
                return ledger.complete(self._handle, outcome)

    def uncertain(self):
        """Record unknown for this operation; never assert submission."""
        handle = self._handle
        with _refusals():
            ledger._check_handle(handle)
        attempt = ledger.Attempt(handle.effect_id, handle._directory,
                                 handle._binding, handle._secret)
        with _refusals(), protocol.pin(handle._directory) as record:
            with record.drain():
                return ledger._classify(attempt, "unknown")


def bind(handle):
    """Bind an existing capability while active, without current authorization.

    The merged ledger reauthenticates the stored binding on subsequent calls.
    Construction reads the existing entry; it never prepares or classifies it.
    """
    ledger._active()
    if type(handle) not in (ledger.Attempt, ledger.Receipt) or type(handle._directory) is not str:
        raise ledger.EffectError(MISMATCH)
    with _refusals():
        ledger._check_handle(handle)
    with _refusals(), protocol.pin(handle._directory) as record:
        with record.drain(), ledger._checked(handle):
            return _Completion(handle)


class Provider(ABC):
    """Trusted source adapter; no provider argument is exposed by submit().

    Every handle belongs to the same prepared operation and retains its binding.
    A prepared handle exposes effect_id (the existing operation key, unchanged).
    Accounting requires the caller's DRAIN for the entire operation lifetime;
    it cannot reauthorize, acquire START, dispatch, retry or change the binding.
    Implementations must fail closed on unconfirmed storage. This is a trusted
    Python contract, not a sandbox against a malicious provider implementation.
    """

    @abstractmethod
    def prepare(self, checkout, plan_id, state_dir, **binding):
        """Durably prepare before dispatch; return the bound attempt handle."""

    @abstractmethod
    def classify(self, attempt, state):
        """Persist submitted/not_submitted; submitted returns a bound handle.

        not_submitted is allowed only when Boundary guarantees no initiation.
        Neither a failed command nor an unknown outcome is not_submitted.
        """

    @abstractmethod
    def bind(self, handle):
        """Return a capability with complete(outcome) and uncertain().

        uncertain() records unknown without releasing or asserting completion.
        complete() returns a validated outcome ONLY after authoritative durable
        completion; for journals this includes bound receipt readback. It raises
        for missing/invalid evidence. Neither method initiates an external effect.
        Binding an attempt permits uncertainty, not invented submission.
        """

    @abstractmethod
    def validate_outcome(self, outcome):
        """Raise for invalid Finish/completion results; return establishes no C.

        Shape validation is not completion authority. The bound capability must
        establish authoritative completion independently of result fields.
        """


class _LedgerProvider(Provider):
    def prepare(self, checkout, plan_id, state_dir, **binding):
        return ledger.prepare(checkout, plan_id, state_dir, **binding)

    def classify(self, attempt, state):
        return ledger._classify(attempt, state)

    def bind(self, handle):
        return bind(handle)

    def validate_outcome(self, outcome):
        ledger._outcome(outcome)


def _merge_provider():
    from d5_merge_journal import PROVIDER
    return PROVIDER


_PROVIDERS = MappingProxyType({"ledger": _LedgerProvider(), "journal": _merge_provider})


def provider_for(kind):
    """Fixed pre-START mapping; result fields cannot select a provider."""
    provider = _PROVIDERS[admission.recording_authority(kind)]
    if provider is _merge_provider:
        return _merge_provider()
    if provider is None:
        raise AuthorityError("D5 journal request requires journal binding")
    return provider
