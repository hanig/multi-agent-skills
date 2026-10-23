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

Boundary is an explicit trusted-adapter interface, not a sandbox or a transport.
No production adapter is supplied here. A named implementation must expose one
actual initiation in start(operation), report Initiation, and keep transport
state for finish(). Start must not wait for a response or process completion,
defer a request, retry, or commission another effect. Finish consumes the
already-started operation, including after an ambiguous start exception; it
must leave no unprotected effect-capable activity when it returns or raises.
Managed surviving children require the separate protected-child protocol.

An Operation contains immutable scope JSON and exact intent bytes understood
by that named adapter; accurate effect/scope declaration is its obligation.
The caller supplies a unique request_id; the ledger also assigns an effect_id.
Neither identifier provides remote deduplication. No shell text is inspected.
Completion/uncertainty failures, including D5 inactive, propagate and retain
the ledger entry. No local accounting success is inferred from a response.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import json

import d5_admission as admission
from d5_authority import AuthorityError
import d5_completion as completion
import d5_effect_log as ledger
import d5_protocol_locks as protocol
import d5_registry as registry


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
                type(self.scope) is not bytes or type(self.intent) is not bytes):
            raise ValueError("D5 operation requires immutable fields")


class Boundary(ABC):
    """Implement deliberately in a named, trusted adapter for one operation."""

    @abstractmethod
    def start(self, operation):
        """Make one concrete initiation attempt, returning Initiation."""

    @abstractmethod
    def finish(self):
        """Consume the started operation; return the ledger outcome dictionary."""


@dataclass(frozen=True)
class Result:
    effect_id: str
    initiation: Initiation
    outcome: object


def submit(checkout, plan_id, state_dir, *, grant_id, session_id, anchor,
           generation, operation, boundary):
    """Freshly admit one immutable operation and account for its response.

    Uses R1's lexical, thread-owned contexts. A nested call from Finish gets
    its own fresh admission and accounting. Start cannot recursively wait for
    another submission: that would violate the adapter's short-Start contract.
    """
    if not registry.d5_active():
        raise AuthorityError("D5 is inactive")
    if not isinstance(boundary, Boundary):
        raise AuthorityError("D5 submission requires an explicit effect boundary")
    if type(operation) is not Operation:
        raise ValueError("D5 operation requires immutable fields")
    entry = registry.lookup(checkout, plan_id, state_dir)
    if entry is None:
        raise AuthorityError("not registered")
    request = dict(grant_id=grant_id, session_id=session_id, anchor=anchor,
                   generation=generation, scope=operation.scope)
    with protocol.pin(entry["record_dir"]) as pinned, pinned.drain():
        attempt, began, state, start_error = None, False, Initiation.UNCERTAIN, None
        try:
            with pinned.start():
                # Preflight keeps authority/refusal precedence before ledger
                # schema checks. It never authorizes the eventual boundary.
                admission._validate_locked(pinned, checkout, plan_id, state_dir, **request)
                attempt = ledger.prepare(checkout, plan_id, state_dir,
                    record_id=grant_id, custody=dict(generation=generation,
                    session_id=session_id, anchor_pid=anchor.pid, anchor_start_us=anchor.start_us),
                    kind=operation.kind, scope=json.loads(operation.scope),
                    correlation={"request_id": operation.request_id, "intent": operation.intent.hex()})
                admission._validate_locked(pinned, checkout, plan_id, state_dir, **request)
                began = True
                state = boundary.start(operation)
        except BaseException as exc:
            if not began:
                # A durable intent may exist after final admission refuses.
                # There was no boundary invocation in this branch.
                if attempt is not None:
                    ledger._classify(attempt, "not_submitted")
                raise
            start_error = exc
        if type(state) is not Initiation:
            state = Initiation.UNCERTAIN
            start_error = ValueError("D5 boundary returned an invalid initiation")

        # START is closed before accounting or response work. Classification
        # failures must still drain the started operation before releasing SH.
        capability, accounting_error = None, None
        try:
            if state is Initiation.SUBMITTED:
                capability = completion.bind(ledger._classify(attempt, "submitted"))
            elif state is Initiation.UNCERTAIN:
                completion.bind(attempt).uncertain()
            else:
                ledger._classify(attempt, "not_submitted")
        except BaseException as exc:
            accounting_error = exc

        outcome, response_error = None, None
        if state is not Initiation.NOT_SUBMITTED:
            try:
                outcome = boundary.finish()
            except BaseException as exc:
                response_error = exc
        if accounting_error is not None:
            raise accounting_error
        if capability is not None:
            if response_error is not None:
                capability.uncertain()
            else:
                outcome = capability.complete(outcome)
        if start_error is not None:
            raise start_error
        if response_error is not None:
            raise response_error
        return Result(attempt.effect_id, state,
                      outcome if state is Initiation.SUBMITTED else None)
