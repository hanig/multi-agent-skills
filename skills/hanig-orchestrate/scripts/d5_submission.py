"""Inert U3-R6 concrete submission lifecycle; no production callers.

Supported envelope and accepted limits
-------------------------------------
P/H plus the explicit adapter contract. Local ledger operations may lengthen
START; remote response waits may not. Trusted local execution, stable record
and lock identities, and participating control/install callers are required.
R1 repairs owner permissions in place to 0600, without directory chmod or lock
replacement. Resources and filesystem operations may fail. Raw mandate edits
are not serialized; the registered source is read through R5 at admission.

The registered checkout stays at the path recorded at registration for the registration's lifetime. A renamed or moved registered checkout makes admission refuse, before any store or boundary call, with the missing registered mandate path in the error; the grant, custody and mandate bytes are unchanged. Operator recovery: restore the checkout to its registered absolute path; recovery remains subject to ordinary expiry and the other admission checks.

Non-claims: the helper cannot prove nonblocking behavior of arbitrary injected
code; no exactly-once execution; no implicit retries.

Named, trusted Boundary adapters start one effect without response waits,
deferral, retries or additional effects. A normal Finish return means the
started operation has terminated; children use the protected-child protocol.
Callers supply unique request_id and accurate immutable scope/intent.
Accounting errors, including inactive D5, propagate and retain the entry.
A shared descriptor survives unconfirmed exits until process exit. Uncertainty
accounting never authorizes release. The terminal epilogue is the release gate.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import json
import os

import d5_admission as admission
from d5_authority import AuthorityError
import d5_completion as completion
import d5_effect_log as ledger
import d5_protocol_locks as protocol
import d5_registry as registry
import d5_journal_completion as journal_completion


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


class Boundary(ABC):
    """Explicit adapter: subclass or register a named, trusted implementation."""

    @abstractmethod
    def start(self, operation):
        """Return Initiation."""

    @abstractmethod
    def finish(self):
        """Wait for termination, including ambiguous starts; return ledger outcome."""


@dataclass(frozen=True)
class Result:
    effect_id: str
    initiation: Initiation
    outcome: object


def submit(checkout, plan_id, state_dir, *, grant_id, session_id, anchor,
           generation, operation, boundary, journal=None):
    """Admit one operation; refuse ambient START."""
    if not registry.d5_active():
        raise AuthorityError("D5 is inactive")
    if not isinstance(boundary, Boundary):
        raise AuthorityError("D5 submission requires an explicit effect boundary")
    if not isinstance(operation, Operation):
        raise ValueError("D5 operation requires immutable fields")
    entry = registry.lookup(checkout, plan_id, state_dir)
    if entry is None:
        raise AuthorityError("not registered")
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
        recording = None
        outcome, response_error = None, None
        try:
            with pinned.start():
                try:
                    admission._validate_locked(pinned, checkout, plan_id, state_dir, **request)
                    attempt = ledger.prepare(checkout, plan_id, state_dir,
                        record_id=grant_id, custody=dict(generation=generation,
                        session_id=session_id, anchor_pid=anchor.pid, anchor_start_us=anchor.start_us),
                        kind=operation.kind, scope=json.loads(operation.scope),
                        correlation={"request_id": operation.request_id, "intent": bytes.hex(operation.intent)})
                    admission._validate_locked(pinned, checkout, plan_id, state_dir, **request)
                    if journal is not None:
                        recording = journal_completion.bind(journal, operation,
                                                            state_dir, entry["record_dir"])
                    A = True
                    state = boundary.start(operation)
                except BaseException as exc:
                    start_error = exc
            S = True
            if not A:
                if attempt is not None:
                    ledger._classify(attempt, "not_submitted")
            else:
                if type(state) is not Initiation:
                    state = Initiation.UNCERTAIN
                    start_error = ValueError("D5 boundary returned an invalid initiation")
                try:
                    if state is Initiation.SUBMITTED:
                        receipt = ledger._classify(attempt, "submitted")
                        capability = (completion.bind(receipt) if recording is None else
                                      completion.bind(receipt, journal=recording))
                    elif state is Initiation.NOT_SUBMITTED:
                        ledger._classify(attempt, "not_submitted")
                        N = True
                except BaseException as exc:
                    accounting_error = exc
        finally:
            # Drain the initiated operation on interruption too.
            if S and A and state is not Initiation.NOT_SUBMITTED:
                try:
                    outcome = boundary.finish()
                    ledger._outcome(outcome)
                    F = True
                except BaseException as exc:
                    response_error = exc
                finally:
                    if capability is None or response_error is not None:
                        completion.bind(attempt).uncertain()
                    else:
                        outcome = capability.complete(outcome)
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
