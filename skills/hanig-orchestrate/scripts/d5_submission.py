"""R6 submission lifecycle and the closed synchronous merge adapter.

Supported envelope and accepted limits
-------------------------------------
P/H plus the explicit adapter contract. Local provider operations may lengthen
START; remote response waits may not. OD2 permits only the closed merge-journal
class to release START after durable Bound preparation, observe its target once
with a hard timeout of at most 60 seconds, then reacquire START and revalidate
before launch. DRAIN spans both sections and the observation. Trusted local execution, stable record
and lock identities, and participating control/install callers are required.
R1 repairs owner permissions in place to 0600, without directory chmod or lock
replacement. Resources and filesystem operations may fail. Raw mandate edits
are not serialized; the registered source is read through R5 at admission.

The registered checkout stays at the path recorded at registration for the registration's lifetime. A renamed or moved registered checkout makes admission refuse, before any store or boundary call, with the missing registered mandate path in the error; the grant, custody and mandate bytes are unchanged. Operator recovery: restore the checkout to its registered absolute path; recovery remains subject to ordinary expiry and the other admission checks.

Non-claims: the helper cannot prove nonblocking behavior of arbitrary injected
code; no exactly-once execution; no implicit retries.

OD5 caller precondition: hold the coordinator lease for the entire submit call.
Do not release it, unlock it or last-close its descriptor from another thread,
a signal handler or a callback during the call. lease_required and holds_lease
detect a lease already lost; they do not fence a later release. U4.3's lease
continuity guarantee depends on this precondition, not a release/submit fence.
flock ownership follows the open file description. An independently opened
descriptor cannot unlock it; a child inheriting the description, or a process
receiving an alias, can. The merge child receives no lease or DRAIN descriptor.

Named, trusted Boundary adapters start one effect without response waits,
deferral, retries or additional effects. The merge adapter observes outside
START and starts without waiting in its second START. A normal Finish return means the
started operation has terminated; children use the protected-child protocol.
Callers supply unique request_id and accurate immutable scope/intent.
Accounting errors, including inactive D5, propagate and retain the entry.
A shared descriptor survives unconfirmed exits until process exit. Uncertainty
accounting never authorizes release. The terminal epilogue is the release gate.

R6 release facts (unchanged): H = this invocation owns its duplicated DRAIN
holder; A = Boundary.start may have run; N = durable not-submitted classification;
S = normal exit from START; F = valid Finish return for the actual operation;
C = authoritative durable completion. Release remains exactly
H and S and (not A or N or (F and C)). No exit/reap/timeout implies C.
Only C's evidence source changes under OD7: ledger completion for ledger classes,
bound journal-provider completion for the closed merge class (see d5_completion).
F alone is not C. OD2 changes START placement only for the closed merge class.
Lifetime ownership and retention on unknown are unchanged.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import json
import os

import d5_admission as admission
from d5_authority import AuthorityError
import d5_completion as completion
import d5_protocol_locks as protocol
import d5_registry as registry
import d5_readiness as readiness


class Initiation(Enum):
    NOT_SUBMITTED = "not_submitted"
    SUBMITTED = "submitted"
    UNCERTAIN = "unknown"


@dataclass(frozen=True)
class Operation:
    request_id: str
    kind: str
    scope: bytes
    intent: bytes

    def __post_init__(self):
        if (type(self.request_id) is not str or type(self.kind) is not str or
                type(self.scope) is not bytes or not isinstance(self.intent, bytes)):
            raise ValueError("D5 operation requires immutable fields")
        admission.recording_authority(self.kind)

    @property
    def recording_authority(self):
        return admission.recording_authority(self.kind)


def _ledger_operation(operation):
    """Pre-START request discrimination, also used at bootstrap entry."""
    if not isinstance(operation, Operation):
        raise ValueError("D5 operation requires immutable fields")
    # Reclassify kind, never trust an instance's authority property/serialized tag.
    admission.require_ledger(operation.kind)


class Boundary(ABC):
    """Explicit adapter: subclass or register a named, trusted implementation."""

    @abstractmethod
    def start(self, operation):
        """Return Initiation."""

    @abstractmethod
    def finish(self):
        """Wait for actual termination, including ambiguity; return provider outcome."""


@dataclass(frozen=True)
class Result:
    effect_id: str
    initiation: Initiation
    outcome: object


def submit(checkout, plan_id, state_dir, *, grant_id, session_id, anchor,
           generation, operation, boundary):
    """Admit one operation; refuse ambient START."""
    if not registry.d5_active():
        raise AuthorityError("D5 is inactive")
    ready = readiness.require(state_dir)
    if not isinstance(boundary, Boundary):
        raise AuthorityError("D5 submission requires an explicit effect boundary")
    if not isinstance(operation, Operation):
        raise ValueError("D5 operation requires immutable fields")
    provider = completion.provider_for(operation.kind)
    if hasattr(provider, "validate_request"):
        provider.validate_request(operation, boundary)
    entry = registry.lookup(checkout, plan_id, state_dir)
    if entry is None:
        raise AuthorityError("not registered")
    readiness.bound(ready, entry)
    request = dict(grant_id=grant_id, session_id=session_id, anchor=anchor,
                   generation=generation, scope=operation.scope)
    with protocol.pin(entry["record_dir"]) as pinned, pinned.drain() as drain:
        if pinned._record._prior(1) is not None:
            raise AuthorityError("D5 submission inside START is forbidden")
        # H: holder owned; A: Start may have run; N: durable not-submitted;
        # S: normal START exit; F: valid Finish return; C: durable completion.
        H = A = N = S = F = C = False
        holder = os.dup(drain)
        H = True
        attempt, state, start_error = None, Initiation.UNCERTAIN, None
        capability, accounting_error = None, None
        outcome, response_error = None, None
        try:
            observation, observation_error = None, None
            phases = ("prepare", "launch") if operation.kind == "merge" else ("both",)
            for phase in phases:
                with pinned.start():
                    try:
                        if phase != "launch":
                            admission._validate_locked(pinned, checkout, plan_id, state_dir, **request)
                            attempt = provider.prepare(checkout, plan_id, state_dir,
                                record_id=grant_id, custody=dict(generation=generation,
                                session_id=session_id, anchor_pid=anchor.pid, anchor_start_us=anchor.start_us),
                                kind=operation.kind, scope=json.loads(operation.scope),
                                correlation={"request_id": operation.request_id, "intent": bytes.hex(operation.intent)})
                        if phase != "prepare":
                            admission._validate_locked(pinned, checkout, plan_id, state_dir, **request)
                            if operation.kind == "merge":
                                if observation_error is not None:
                                    raise observation_error
                                boundary.check_observation(operation, observation)
                            readiness.lease_required(state_dir)
                            A = True
                            state = boundary.start(operation)
                    except BaseException as exc:
                        start_error = exc
                if start_error is not None:
                    break
                if phase == "prepare":
                    # OD2: one bounded target read with lifetime DRAIN held,
                    # no START, and no journal fsync before the eventual launch.
                    try:
                        observation = boundary.observe(operation)
                    except BaseException as exc:
                        observation_error = exc
            S = True
            if not A:
                if attempt is not None:
                    try:
                        provider.classify(attempt, "not_submitted")
                    except BaseException as exc:
                        # OD3: a lost coordinator lease forbids even N. Keep
                        # Bound and propagate, but still reach the R6 epilogue.
                        accounting_error = exc
            else:
                if type(state) is not Initiation:
                    state = Initiation.UNCERTAIN
                    start_error = ValueError("D5 boundary returned an invalid initiation")
                try:
                    if state is Initiation.SUBMITTED:
                        capability = provider.bind(provider.classify(attempt, "submitted"))
                    elif state is Initiation.NOT_SUBMITTED:
                        provider.classify(attempt, "not_submitted")
                        N = True
                except BaseException as exc:
                    accounting_error = exc
        finally:
            # Drain the initiated operation on interruption too.
            if S and A and state is not Initiation.NOT_SUBMITTED:
                try:
                    outcome = boundary.finish()
                    provider.validate_outcome(outcome)
                    F = True
                except BaseException as exc:
                    response_error = exc
                finally:
                    if capability is None or response_error is not None:
                        provider.bind(attempt).uncertain()
                    else:
                        outcome = capability.complete(outcome)
                        provider.validate_outcome(outcome)
                        C = True
        if H and S and (not A or N or (F and C)):
            os.close(holder)
        if accounting_error is not None:
            raise accounting_error
        if start_error is not None:
            raise start_error
        if response_error is not None:
            raise response_error
        return Result(attempt.effect_id, state,
                      outcome if state is Initiation.SUBMITTED else None)
