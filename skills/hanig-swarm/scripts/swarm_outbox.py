"""Offline tracker intent keys, envelopes, and observation reconciliation.

Python 3.8+, standard library only.
"""

import hashlib
import json
from pathlib import Path

import swarm_evidence as SE
from swarm_types import OutboxError


INTENT_SCHEMA_VERSION = 1
OBSERVATION_SCHEMA_VERSION = 1
RECONCILIATION_SCHEMA_VERSION = 1
INTENT_CONNECTOR_CAPABILITY = (
    "tracker.intent.idempotent-mutation-readback.v1")
INTENT_OPERATIONS = frozenset(
    ("start", "close", "reopen", "note", "block", "open_pr"))
OPERATION_ACCEPTED = "operation_accepted"
ASYNC_COMPLETED = "asynchronously_completed"
CONFIRMED_BY_READBACK = "confirmed_by_readback"
UNKNOWN = "unknown"
OBSERVATION_OUTCOMES = frozenset(
    (OPERATION_ACCEPTED, ASYNC_COMPLETED, CONFIRMED_BY_READBACK, UNKNOWN))
MUTATION_RESPONSE = "mutation_response"
A2A_LIFECYCLE = "a2a_lifecycle"
RECEIVER_READBACK = "receiver_readback"
RECEIVER_DEDUPLICATION = "receiver_deduplication"
CONFIRMING_SOURCES = frozenset(
    (RECEIVER_READBACK, RECEIVER_DEDUPLICATION))
OBSERVATION_SOURCES = frozenset(
    (MUTATION_RESPONSE, A2A_LIFECYCLE,
     RECEIVER_READBACK, RECEIVER_DEDUPLICATION))

# WHICH EVIDENCE MAY CLOSE WHICH KIND. Hard-coded and deliberately not
# configurable per plan: a configurable authority is one somebody can
# configure wrong, and the failure is silent -- the tracker would report work
# as verified on evidence that never established it.
#
# A merged PR is the right evidence for CODE and the wrong evidence for a
# 1.42 TiB hash. A predicate receipt is the reverse.
# READY_FOR_PR is deliberately NOT a unit state yet. The closure-by-exclusion
# guard caught me declaring one that nothing produces, which is the same error
# as accepting retry.mode "resume" before cross-attempt handoff exists: a name
# for a mechanism that is not built. Stage 1 needs no new state, because the
# rewrite below turns a code unit's close into open_pr at the point of
# emission. The state arrives with stage 3, alongside the branch and PR flow
# that can actually reach it.
CLOSING_EVIDENCE = {
    "code": "merged_pr",
    "slurm": "predicate_receipt",
    "pipeline": "predicate_receipt",
}


def closing_evidence_for(kind):
    return CLOSING_EVIDENCE.get(kind or "slurm", "predicate_receipt")


def outbox_key(project, uid, state, attempt_dir):
    """Idempotency key. Same project, unit, state and attempt yields the same
    key, allowing receiver-side deduplication after an ambiguous drain."""
    basis = f"{project}\x00{uid}\x00{state}\x00{attempt_dir or ''}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def _intent_evidence_digest(evidence):
    """Digest the exact evidence value carried by the intent envelope."""
    # Keep json.dumps' ASCII escaping. The existing outbox writer accepts
    # surrogateescaped filesystem names by persisting them as ``\udxxx``;
    # ensure_ascii=False would make the following UTF-8 encode crash.
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _intent_attempt_identity(attempt_dir):
    directory = str(attempt_dir) if attempt_dir else None
    return {"id": Path(directory).name if directory else None,
            "directory": directory}


def _intent_envelope(project, uid, attempt_dir, key, verb, evidence):
    """The connector-independent, versioned routing and binding contract."""
    return {
        "schema_version": INTENT_SCHEMA_VERSION,
        "project": project,
        "unit": uid,
        "attempt": _intent_attempt_identity(attempt_dir),
        "idempotency_key": key,
        "requested_operation": verb,
        "evidence_digest": _intent_evidence_digest(evidence),
        "required_connector_capability": INTENT_CONNECTOR_CAPABILITY,
    }


def normalize_intent(intent):
    """Give persisted pre-envelope intents the same read contract as new ones.

    Envelope migration leaves JSONL audit history untouched. Normalizing
    at the sole reader keeps a restart from exposing
    the obsolete connector shape while preserving their original bytes.
    """
    if not isinstance(intent, dict) or "envelope" in intent:
        return intent
    normalized = dict(intent)
    normalized["envelope"] = _intent_envelope(
        intent.get("project"), intent.get("unit"), intent.get("attempt_dir"),
        intent.get("key"), intent.get("verb"), intent.get("evidence"))
    return normalized


# Compatibility name for existing internal callers. The contract owner is
# this module; the project-side drain CLI delegates here rather than carrying
# another implementation.
_with_intent_envelope = normalize_intent


def _nonblank_text(value):
    return isinstance(value, str) and bool(value.strip())


def _schema_is(value, expected):
    """JSON booleans are not integer schema versions (`True == 1` in Python)."""
    return type(value) is int and value == expected


def validate_intent(intent, connector_capabilities=None):
    """Return envelope problems after explicit legacy normalization."""
    if not isinstance(intent, dict):
        return ["intent is not an object"]
    intent = normalize_intent(intent)
    envelope = intent.get("envelope")
    if not isinstance(envelope, dict):
        return ["intent has no versioned `envelope` object"]
    problems = []
    if not _schema_is(envelope.get("schema_version"), INTENT_SCHEMA_VERSION):
        problems.append("unsupported envelope schema_version %r "
                        "(understands %d)" %
                        (envelope.get("schema_version"),
                         INTENT_SCHEMA_VERSION))
    for field in ("project", "unit", "idempotency_key",
                  "requested_operation", "evidence_digest",
                  "required_connector_capability"):
        if not _nonblank_text(envelope.get(field)):
            problems.append("envelope.%s is missing or blank" % field)
    if envelope.get("requested_operation") not in INTENT_OPERATIONS:
        problems.append("envelope.requested_operation is not one of %s" %
                        ", ".join(sorted(INTENT_OPERATIONS)))
    if (envelope.get("required_connector_capability") !=
            INTENT_CONNECTOR_CAPABILITY):
        problems.append("envelope requires unsupported connector capability "
                        "%r" % envelope.get(
                            "required_connector_capability"))
    digest = envelope.get("evidence_digest")
    if (_nonblank_text(digest) and
            (len(digest) != 64 or
             any(c not in "0123456789abcdef" for c in digest))):
        problems.append("envelope.evidence_digest is not a lowercase SHA-256")
    attempt = envelope.get("attempt")
    if not isinstance(attempt, dict):
        problems.append("envelope.attempt is not an object")
    else:
        aid, directory = attempt.get("id"), attempt.get("directory")
        if (aid is None) != (directory is None):
            problems.append("envelope.attempt id and directory must both be "
                            "null or both be present")
        elif directory is not None:
            if not _nonblank_text(aid) or not _nonblank_text(directory):
                problems.append("envelope.attempt id and directory must be "
                                "non-blank strings")
            elif Path(directory).name != aid:
                problems.append("envelope.attempt.id does not name its "
                                "directory")
    aliases = (("project", "project"), ("unit", "unit"),
               ("idempotency_key", "key"),
               ("requested_operation", "verb"))
    for envelope_field, payload_field in aliases:
        if envelope.get(envelope_field) != intent.get(payload_field):
            problems.append("envelope.%s does not match intent.%s" %
                            (envelope_field, payload_field))
    if (isinstance(attempt, dict) and
            attempt.get("directory") != intent.get("attempt_dir")):
        problems.append("envelope.attempt.directory does not match "
                        "intent.attempt_dir")
    if envelope.get("evidence_digest") != _intent_evidence_digest(
            intent.get("evidence")):
        problems.append("envelope.evidence_digest does not match the "
                        "intent evidence")
    if intent.get("verb") == "close" and intent.get("evidence") is None:
        problems.append("a close intent has no evidence")
    if connector_capabilities is not None:
        capability = envelope.get("required_connector_capability")
        if capability not in set(connector_capabilities):
            problems.append("connector does not declare required capability "
                            "%r" % capability)
    return problems


def validate_observation(intent, observation):
    """Return problems binding one connected-session report to an intent."""
    problems = []
    if not isinstance(observation, dict):
        return ["observation is not an object"]
    if not _schema_is(observation.get("schema_version"),
                      OBSERVATION_SCHEMA_VERSION):
        problems.append("unsupported observation schema_version %r" %
                        observation.get("schema_version"))
    outcome = observation.get("outcome")
    if outcome not in OBSERVATION_OUTCOMES:
        problems.append("observation.outcome is not one of %s" %
                        ", ".join(sorted(OBSERVATION_OUTCOMES)))
    source = observation.get("source")
    if source not in OBSERVATION_SOURCES:
        problems.append("observation.source is not one of %s" %
                        ", ".join(sorted(OBSERVATION_SOURCES)))
    envelope = intent.get("envelope") or {}
    bindings = (("project", "project"), ("unit", "unit"),
                ("idempotency_key", "idempotency_key"),
                ("requested_operation", "requested_operation"),
                ("evidence_digest", "evidence_digest"),
                ("connector_capability",
                 "required_connector_capability"))
    for observed, expected in bindings:
        if observation.get(observed) != envelope.get(expected):
            problems.append("observation.%s does not match envelope.%s" %
                            (observed, expected))
    if observation.get("attempt") != envelope.get("attempt"):
        problems.append("observation.attempt does not match envelope.attempt")
    if outcome == CONFIRMED_BY_READBACK:
        if source not in CONFIRMING_SOURCES:
            problems.append("confirmed_by_readback requires receiver_readback "
                            "or receiver_deduplication, not %r" % source)
        if observation.get("matched") is not True:
            problems.append("confirmed_by_readback requires matched=true")
        if not _nonblank_text(observation.get("reference")):
            problems.append("confirmed_by_readback requires the receiver's "
                            "reference")
    elif outcome == OPERATION_ACCEPTED and source != MUTATION_RESPONSE:
        problems.append("operation_accepted requires mutation_response")
    elif outcome == ASYNC_COMPLETED and source != A2A_LIFECYCLE:
        problems.append("asynchronously_completed requires a2a_lifecycle")
    elif outcome == UNKNOWN and observation.get("matched") is True:
        problems.append("unknown cannot claim matched=true")
    return problems


def require_valid_intent(intent, connector_capabilities=None):
    intent = normalize_intent(intent)
    problems = validate_intent(intent, connector_capabilities)
    if problems:
        raise OutboxError("; ".join(problems))
    return intent


def _receipt_admissible(outcome, source, matched):
    """The single policy boundary between remote hints and receipts."""
    return (outcome == CONFIRMED_BY_READBACK
            and source in CONFIRMING_SOURCES
            and matched is True)


def reconcile_observation(intent, observation):
    """Classify offline data; ambiguity and lifecycle never authorize replay."""
    intent = require_valid_intent(intent)
    problems = validate_observation(intent, observation)
    if problems:
        raise OutboxError("; ".join(problems))
    confirmed = _receipt_admissible(
        observation["outcome"], observation.get("source"),
        observation.get("matched"))
    return {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "idempotency_key": intent["envelope"]["idempotency_key"],
        "outcome": observation["outcome"],
        "source": observation["source"],
        "reference": observation.get("reference"),
        "receipt_admissible": confirmed,
        "replay": False,
        "closing_evidence": False,
    }


def _has_bound_merge_evidence(uid, us, evidence):
    """Does *evidence* carry the merge already admitted for this attempt?"""
    receipt = evidence.get("receipt") if isinstance(evidence, dict) else None
    if not isinstance(receipt, dict) or us.get("merge_receipt") != receipt:
        return False
    attempt = Path(us.get("attempt_dir") or "").name
    produced = (us.get("attempt_produced_heads") or {}).get(attempt)
    return (bool(produced)
            and receipt.get("unit") == uid
            and receipt.get("head") == produced
            and receipt.get("merged_as") == us.get("merged_as")
            and receipt.get("pr") == us.get("merge_pr")
            and receipt.get("merged") is True
            and receipt.get("attested") is True
            and SE._merge_shape_problem(receipt) is None)


def _intent_key(project, uid, unit_state, us, verb, kind, evidence):
    key = outbox_key(project, uid, unit_state, us.get("attempt_dir"))
    if (verb == "block" and unit_state == "NEEDS_HUMAN"
            and us.get("reason") == "deadline_exceeded"):
        # A prior NEEDS_HUMAN intent for a different reason must not suppress
        # the newly declared deadline breach for the same retained attempt.
        # The coordinator-owned sequence also distinguishes a later breach
        # after an operator ratifies a raised deadline for that attempt.
        if us.get("deadline_breach_legacy_key") is True:
            key += "-deadline_exceeded"
        else:
            sequence = us.get("deadline_breach_seq")
            suffix = (str(sequence) if type(sequence) is int and sequence > 0
                      else "legacy")
            key += "-deadline_exceeded-" + suffix
    if verb == "close" and closing_evidence_for(kind) == "merged_pr":
        # Before merge-aware closure, a DONE code unit emitted open_pr under
        # the base key. Include the admitted receipt only for merge closes so
        # that stale entry cannot suppress the correction; compute keys retain
        # their existing idempotency contract.
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        key += "-" + hashlib.sha256(canonical.encode()).hexdigest()[:8]
    return key
