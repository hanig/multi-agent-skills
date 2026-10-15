"""Offline tracker intent keys, envelopes, and observation reconciliation.

Python 3.8+, standard library only.
"""

import fcntl
from datetime import datetime, timezone
import hashlib
import json
import os
import shlex
import stat
import sys
import tempfile
import time
from pathlib import Path

import swarm_evidence as SE
import swarm_journal as SJ
import swarm_state as SS
import unit as U
from swarm_types import OutboxError


INTENT_SCHEMA_VERSION = 1
OBSERVATION_SCHEMA_VERSION = 1
RECONCILIATION_SCHEMA_VERSION = 1
INTENT_CONNECTOR_CAPABILITY = (
    "tracker.intent.idempotent-mutation-readback.v1")
INTENT_OPERATIONS = frozenset(
    ("start", "close", "cancel", "reopen", "note", "block", "open_pr"))
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
    if "seq" in intent and (type(intent["seq"]) is not int or intent["seq"] <= 0):
        problems.append("intent.seq must be a positive integer")
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


def _has_attested_merge_receipt(uid, receipt):
    """Receipt shape check; admission also requires coordinator binding below."""
    return (isinstance(receipt, dict)
            and receipt.get("unit") == uid
            and receipt.get("merged") is True
            and receipt.get("attested") is True
            and SE._merge_shape_problem(receipt) is None)


def _has_bound_merge_evidence(uid, us, evidence):
    """Sole receipt admission for supersede, close emission and collection."""
    receipt = evidence.get("receipt") if isinstance(evidence, dict) else None
    if (not isinstance(us, dict) or not _has_attested_merge_receipt(uid, receipt)
            or us.get("merge_receipt") != receipt):
        return False
    attempt = Path(us.get("attempt_dir") or "").name
    produced = (us.get("attempt_produced_heads") or {}).get(attempt)
    return (bool(produced)
            and receipt.get("head") == produced
            and receipt.get("merged_as") == us.get("merged_as")
            and receipt.get("pr") == us.get("merge_pr"))


def superseded_evidence(us, state_dir):
    """Recheck the pinned receipt against the successor's coordinator binding."""
    decision = us.get("terminal_decision") or {}
    receipt = decision.get("merge_receipt")
    successor = decision.get("successor")
    state, error = U.read_json(Path(state_dir) / SS.STATE_FILE)
    next_us = ((state or {}).get("units") or {}).get(successor) if not error else None
    if (us.get("state") != "SUPERSEDED" or decision.get("state") != "SUPERSEDED"
            or not successor
            or not _has_bound_merge_evidence(successor, next_us, {"receipt": receipt})):
        return None
    return {"successor": successor, "receipt": receipt}


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


# --- tracker outbox -------------------------------------------------------
# The coordinator runs on a cluster LOGIN NODE. An MCP connector lives in the
# Claude client on a laptop, so `swarm.py` cannot call Linear or Asana, and it
# has no network code at all -- deliberately. Putting a tracker API token on a
# shared login node would be the alternative, and it is worse.
#
# So the coordinator writes INTENTS and nothing else. Something that can reach
# the tracker drains them later. Three properties fall out of that separation,
# and each is a real defect avoided:
#
#   - a tracker outage NEVER alters swarm state; the swarm is authoritative and
#     the tracker is a view of it
#   - every intent carries an idempotency key that a capable receiver can use
#     for deduplication; the key alone does not make a blind replay safe
#   - a CLOSE intent is emitted only from a predicate verdict, never from a
#     unit's own report. An agent saying "done" on a ticket is exactly the
#     self-assertion this whole family refuses.
OUTBOX = "outbox.jsonl"
OUTBOX_SEQ = "outbox-seq.json"
OUTBOX_RECOVERING = "outbox-seq.recovering"
RECEIPT_SCHEMA_VERSION = 2

# Unit states that justify a tracker mutation, and what each means to a reader.
TRACKER_EVENTS = {
    "SUBMITTED": ("start", "work started"),
    "DONE": ("close", "the unit's predicate returned DONE"),
    "ABANDONED": ("cancel", "stopped by an approver"),
    "SUPERSEDED": ("close", "superseded by a merged successor"),
    "FAILED": ("reopen", "the command failed"),
    "FAILED_EVIDENCE": ("reopen", "no verdict arrived; evidence never landed"),
    "PREEMPTED": ("note", "preempted; a new attempt will be minted"),
    "HELD": ("block", "an upstream unit will not complete"),
    "NEEDS_HUMAN": ("block", "blocked on a person, not on compute"),
    "READY_FOR_PR": ("open_pr", "the agent finished and its declared outputs "
                                "exist. That is not done: a code unit is "
                                "closed by a merged pull request."),
}

def _unique_pairs(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("duplicate JSON member %r" % key)
        record[key] = value
    return record


def _read_sequence(state_dir):
    path = Path(state_dir) / OUTBOX_SEQ
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK)) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OutboxError("unreadable counter %s: not a regular file" % path)
            record = json.load(handle, object_pairs_hook=_unique_pairs)
    except FileNotFoundError:
        if path.is_symlink():
            raise OutboxError("unreadable counter %s: dangling symlink" % path)
        return None
    except (OSError, UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise OutboxError("unreadable counter %s: %s" % (path, exc))
    if (not isinstance(record, dict)
            or not _schema_is(record.get("schema_version"), 1)
            or type(record.get("high_water")) is not int
            or record["high_water"] < 0):
        raise OutboxError("invalid outbox sequence counter %s" % path)
    return record


def _write_sequence(state_dir, record, name=OUTBOX_SEQ):
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".outbox-seq-", dir=str(directory))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / name)
        SJ._fsync_directory(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sequence_history(path, strict=True):
    """Read history, including complete newline-less JSON."""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return None
    records = []
    with handle:
        for number, line in enumerate(handle, 1):
            try:
                record = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_pairs)
                if not isinstance(record, dict) or not _nonblank_text(record.get("key")):
                    raise ValueError("invalid history key")
                if (strict and "seq" in record and
                        (type(record["seq"]) is not int or record["seq"] <= 0)):
                    raise ValueError("invalid history sequence")
            except (UnicodeDecodeError, RecursionError, ValueError) as exc:
                if (not line.endswith(b"\n") and
                        isinstance(exc, (UnicodeDecodeError, json.JSONDecodeError))):
                    continue
                if strict:
                    raise OutboxError("%s line %d: incomplete outbox history: %s" %
                                      (path, number, exc))
                continue
            records.append(record)
    return records


def _allocate_sequence(state_dir, history):
    """Use history only for counter-less migration."""
    record = _read_sequence(state_dir)
    if record is None:
        receipts, problems = load_acknowledgments(state_dir)
        if problems:
            raise OutboxError("incomplete receipt history")
        if history is None:
            # Even empty receipts rule out genesis.
            try:
                (Path(state_dir) / RECEIPTS).stat()
            except FileNotFoundError:
                pass
            else:
                raise OutboxError("outbox is missing but receipt history exists")
        keys = {row["key"] for row in history or []}
        if any(not _nonblank_text(row["key"]) or row["key"] not in keys
               for row in receipts):
            raise OutboxError("outbox is missing keys from receipt history")
        record = {"schema_version": 1,
                  "high_water": max((row.get("seq", 0) for row in history or []),
                                    default=0)}
        _write_sequence(state_dir, record)
    record = dict(record, high_water=record["high_water"] + 1)
    _write_sequence(state_dir, record)
    return record["high_water"]


def set_sequence_floor(state_dir, floor, approver, reason):
    if type(floor) is not int or floor < 0:
        raise OutboxError("floor must be a non-negative integer")
    if not _nonblank_text(approver) or not _nonblank_text(reason):
        raise OutboxError("seq-floor requires a non-blank approver and reason")
    directory = Path(state_dir)
    path, marker = directory / OUTBOX_SEQ, directory / OUTBOX_RECOVERING
    recovering = os.path.lexists(marker)
    pending = json.loads(marker.read_text()) if recovering else {}
    try:
        obstruction = not stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        obstruction = False
    replaced_malformed = obstruction or pending.get("replaced_malformed_counter", False)
    try:
        previous = None if obstruction else _read_sequence(state_dir)
    except OutboxError:
        previous = None
        replaced_malformed = True
    if previous is not None and floor <= previous["high_water"] and not recovering:
        raise OutboxError("floor must exceed current high_water %d" %
                          previous["high_water"])
    if recovering:
        floor = max(floor, pending["high_water"],
                    previous["high_water"] if previous else 0)
    record = {"schema_version": 1, "high_water": floor,
              "approver": approver, "reason": reason,
              "replaced_malformed_counter": replaced_malformed,
              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if obstruction or recovering:
        record["replaced_obstruction"] = pending.get("replaced_obstruction") or (
            "outbox-seq.obstruction-" + datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%S.%fZ"))
        stem, suffix = record["replaced_obstruction"], 0
        while obstruction and os.path.lexists(directory / record["replaced_obstruction"]):
            suffix += 1
            record["replaced_obstruction"] = "%s-%d" % (stem, suffix)
        _write_sequence(directory, record, OUTBOX_RECOVERING)
        if obstruction:
            quarantine = directory / record["replaced_obstruction"]
            os.rename(path, quarantine)
            SJ._fsync_directory(directory)
    _write_sequence(state_dir, record)
    if obstruction or recovering:
        marker.unlink()
        SJ._fsync_directory(directory)
    return record


def emit_intent(state_dir, project, uid, unit_state, us, evidence=None,
                kind=None, tracker=None):
    """Append one tracker intent. Returns the key, or None if already emitted.

    Deterministic from state: replaying the same local transition produces the
    same key. A connected drainer must deduplicate or read back before retrying
    a remote mutation; the key alone cannot establish what landed.
    Durability and recovery premises: capability.tracker in declarations.json.
    """
    action = TRACKER_EVENTS.get(unit_state)
    if not action:
        return None
    verb, why = action
    if unit_state == "NEEDS_HUMAN" and us.get("reason") == "deadline_exceeded":
        why = "deadline_exceeded"

    decision = us.get("terminal_decision") or {}
    if unit_state in ("ABANDONED", "SUPERSEDED"):
        if (decision.get("state") != unit_state
                or not str(decision.get("approver") or "").strip()
                or not str(decision.get("reason") or "").strip()):
            return None
        why = decision["reason"]
        if unit_state == "SUPERSEDED":
            evidence = superseded_evidence(us, state_dir)
            if evidence is None:
                return None

    # A code predicate reaches READY_FOR_PR, but an admitted merge advances it
    # to DONE. Only the exact merge receipt persisted by that admission may
    # cross this last boundary into a close intent. Keeping the fallback is
    # still belt-and-braces for malformed or legacy DONE state: it may request
    # PR work, but it cannot manufacture a close without merge evidence.
    if (verb == "close" and unit_state != "SUPERSEDED"
            and closing_evidence_for(kind) == "merged_pr"
            and not _has_bound_merge_evidence(uid, us, evidence)):
        verb = "open_pr"
        why = ("the agent finished and its declared outputs exist, which "
               "makes this READY FOR A PR. It is not done: a code unit is "
               "closed by a bound merged-pull-request receipt, and none is "
               "available.")
    if verb == "close" and not evidence:
        # Three reviewers found this: the caller built evidence as
        # `{"receipt": rp} if rp else None`, so an NFS blip on the read
        # microseconds after the verdict produced a close intent with
        # evidence null. A drain following the rule must then refuse it and
        # the issue never closes; a lax drain closes on nothing. Refusing HERE
        # keeps the rule in the one place that cannot be forgotten, and the
        # next advance re-reads the receipt and emits it properly.
        print(f"WARNING: not recording a close intent for {uid}: the "
              f"predicate's receipt could not be read, and nothing closes on "
              f"a self-report. It will be retried on the next advance.",
              file=sys.stderr)
        return None
    key = _intent_key(project, uid, unit_state, us, verb, kind, evidence)
    path = Path(state_dir) / OUTBOX
    try:
        if os.path.lexists(Path(state_dir) / OUTBOX_RECOVERING):
            raise OutboxError("sequence recovery is incomplete")
        history = _sequence_history(path, strict=_read_sequence(state_dir) is None)
        if any(previous["key"] == key for previous in history or []):
            return None          # already emitted; do not duplicate
        seq = _allocate_sequence(state_dir, history)
    except (OSError, ValueError, OutboxError) as exc:
        print(f"WARNING: cannot allocate tracker sequence in {state_dir}: "
              f"{exc}. Recover with "
              f"swarm.py seq-floor --state-dir {shlex.quote(str(state_dir))} "
              f"--floor N --approver NAME --reason TEXT; choose N from "
              f"evidence. Intent emission will retry on the next advance.",
              file=sys.stderr)
        return None
    intent = {
        "key": key, "project": project, "unit": uid, "verb": verb,
        # The coordinator lease serializes emitters in this state directory.
        "seq": seq,
        # Named on every intent so a drainer never has to infer which kind of
        # evidence would justify acting on it.
        "closing_evidence": ("merged_pr" if unit_state == "SUPERSEDED"
                             else closing_evidence_for(kind)),
        "kind": kind,
        "unit_state": unit_state, "why": why,
        # `applied` is deliberately NOT written any more. It was always
        # false, nothing ever set it true, and a permanently false field reads
        # as "this was not filed" when the truth is "this machine does not
        # know". Status is derived from outbox-receipts.jsonl instead.
        "at": (decision["at"] if unit_state in ("ABANDONED", "SUPERSEDED")
               else time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        "job_id": us.get("job_id"), "attempt_dir": us.get("attempt_dir"),
        # A close intent MUST carry the verdict that justifies it. A drain that
        # cannot see the evidence must refuse to close.
        "evidence": evidence,
    }
    if tracker is not None:
        intent["tracker"] = tracker
    intent["envelope"] = _intent_envelope(
        project, uid, us.get("attempt_dir"), key, verb, evidence)
    try:
        SJ._fsync_append(path, intent)
    except (OSError, OutboxError) as e:
        print(f"WARNING: could not append a tracker intent: {e}",
              file=sys.stderr)
        return None
    return key


def backfill_tracker_intents(state_dir, project, units):
    """Add missing labels to historical intents under the coordinator lease.

    The accepted plan supplies labels, never evidence or acknowledgments.
    Existing labels and all other record values survive unchanged. This is an
    additive migration of pre-tracker records, not a replay or a new intent.
    Invalid journals remain intact; failure warns without halting dispatch.
    """
    labels = {uid: u["tracker"] for uid, u in units.items() if "tracker" in u}
    path = Path(state_dir) / OUTBOX
    if not labels:
        return
    temporary = None
    try:
        if not path.is_file():
            return
        # Refuse a partial/corrupt journal before replacing any bytes.
        load_outbox_contract(state_dir)
        lines = path.read_bytes().splitlines(keepends=True)
        changed = False
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            intent = json.loads(line)
            if (intent.get("project") == project
                    and intent.get("unit") in labels and "tracker" not in intent):
                intent["tracker"] = labels[intent["unit"]]
                lines[index] = (json.dumps(intent, sort_keys=True) + "\n").encode()
                changed = True
        if not changed:
            return
        fd, temporary = tempfile.mkstemp(prefix=".outbox-tracker-", dir=str(path.parent))
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), stat.S_IMODE(path.stat().st_mode))
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        SJ._fsync_directory(path.parent)
    except (OSError, ValueError, OutboxError) as exc:
        print(f"WARNING: could not backfill tracker labels: {exc}", file=sys.stderr)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError as exc:
                print(f"WARNING: could not remove tracker migration temporary: {exc}",
                      file=sys.stderr)


# --- acknowledgment: did the drain actually land? -------------------------
#
# Every intent was written {"applied": false} and NOTHING ever set it true, so
# after a clean run all eight intents still read pending. A receiver can use
# the key for deduplication, but a blind re-drain after ambiguity is unsafe.
# Worse, the outbox could not answer the one question it exists to answer, and
# a record that never advances is not a record.
#
# Sol's three corrections to my first design, each of which I had wrong:
#
# 1. Append-only JSONL is NOT automatically crash-safe. A process can die
#    having written half a line. So: flock, fsync, and a DEFINED rule for a
#    truncated tail (drop it, say so) versus a malformed record in the middle
#    (fail closed; that is corruption, not an interrupted write).
#
# 2. Do not store an authoritative `applied: true`. Store a success receipt
#    carrying the tracker's own reference and DERIVE status. An issue id by
#    itself proves nothing: an update or close intent already contains the
#    target id, and holding an id does not establish that the mutation ran.
#    The receipt means "the drainer observed THIS operation succeed and got
#    reference X".
#
# 3. Absence of a receipt is `unacknowledged`, never "not applied". The
#    operation may well have happened and the acknowledgment been lost.
#    Saying "not applied" claims knowledge this machine does not have.
RECEIPTS = "outbox-receipts.jsonl"
RECEIPT_CONFIRMED = CONFIRMED_BY_READBACK
RECEIPT_CONFIRMING_SOURCES = CONFIRMING_SOURCES

# The WIRE VALUES matter as much as the printed ones. Round 2 caught me
# relabelling only the text output: --json still emitted "acknowledged", so a
# machine consumer read an attestation as verified tracker success. The value
# itself now carries the weakness, so both paths say the same thing.
UNACKNOWLEDGED = "unacknowledged"
ATTESTED_UNSPECIFIED = "attested"  # historical wire value: source unspecified
ATTESTED_CONFIRMED = "attested_confirmed"
ACKNOWLEDGED = ATTESTED_CONFIRMED   # compatibility name, stronger grade
CONFLICT = "conflict"


def _parse_receipt_text(raw_text):
    """Parse already-read receipt bytes into the guarded journal result."""
    lines = raw_text.splitlines()
    complete_tail = raw_text.endswith("\n")
    out, problems = [], []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        is_last = idx == len(lines) - 1
        try:
            rec = json.loads(line)
        except ValueError:
            if is_last and not complete_tail:
                problems.append({
                    "kind": "truncated_tail",
                    "detail": "the last receipt line is truncated, which is "
                              "what an interrupted write looks like. It was "
                              "dropped; repeat offline reconciliation after "
                              "the receiver resolves that key."})
            else:
                problems.append({
                    "kind": "corrupt",
                    "detail": "receipt line %d was written in full and does "
                              "not parse, so this is corruption rather than "
                              "an interrupted write." % (idx + 1)})
            continue
        bad = _receipt_shape_problem(rec)
        if bad:
            problems.append({"kind": "malformed",
                             "detail": "receipt line %d: %s" %
                                       (idx + 1, bad)})
            continue
        out.append(rec)
    return SJ._RawJournal(out, problems)


def _read_receipts_raw(state_dir):
    """Parse the journal. PRIVATE: everything goes through the chokepoint.

    Do not call this to decide anything. It reports what it found, including
    what it could not read, and reporting is not refusing. Three review rounds
    found the same bug in four places because each caller decided for itself
    whether a problem mattered, and each new caller forgot again. Use
    `load_acknowledgments`, which cannot be bypassed by forgetting.

    Returns (receipts, problems), where a problem is STRUCTURED.

    Problems used to be prose, and the caller decided whether to fail closed
    by looking for the word "corruption" in it. That is the guard-matching-
    its-own-message bug this repo has now paid for four times: an OSError
    produced "cannot read ...", matched nothing, and the command cheerfully
    reported every intent as unacknowledged and exited zero.

    So the KIND is data:
      truncated_tail  interrupted local write; repeat reconciliation
      corrupt         a complete but unreadable record; fail closed
      unreadable      the journal itself cannot be read; fail closed
      malformed       parsed as JSON but not a receipt; fail closed
    """
    p = Path(state_dir) / RECEIPTS
    # is_file() answers False both for "not there" and for "cannot look",
    # which are opposite facts: absent means nothing was ever attested,
    # unreadable means we do not know. It can also raise outright on some
    # platforms. Ask to open it and read the errno instead. report.py's copy
    # of this reader was fixed first and this one was left behind, which is
    # the same copy-drift that put the removed `applied` field in two states.
    try:
        with open(p) as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return SJ._RawJournal([], [])
    except IsADirectoryError as exc:
        return SJ._RawJournal(
            [], [{"kind": "unreadable", "detail": f"{p} is not a file: {exc}"}])
    except OSError as exc:
        return SJ._RawJournal(
            [], [{"kind": "unreadable", "detail": f"cannot read {p}: {exc}"}])

    return _parse_receipt_text(raw_text)


def _receipt_shape_problem(rec):
    """Valid JSON is not a valid receipt.

    Round 3: {"key": "k1", "ref": "ARC-1", "attested": false} parsed fine and
    produced an ATTESTED status, because nothing looked past json.loads."""
    if not isinstance(rec, dict):
        return "not an object"
    if not str(rec.get("key") or "").strip():
        return "no key"
    if not str(rec.get("ref") or "").strip():
        return "no tracker ref"
    if rec.get("attested") is not True:
        return ("attested is not true, so this record does not assert that "
                "anything succeeded")
    schema = rec.get("schema_version")
    if (schema is not None and
            not (_schema_is(schema, 1) or
                 _schema_is(schema, RECEIPT_SCHEMA_VERSION))):
        return "unsupported receipt schema_version %r" % schema
    if not _schema_is(schema, RECEIPT_SCHEMA_VERSION):
        if any(rec.get(field) is not None
               for field in ("outcome", "source", "matched")):
            return ("legacy receipts cannot claim a version-2 outcome or "
                    "source")
    else:
        if rec.get("op") not in INTENT_OPERATIONS:
            return "version-2 receipt has no valid requested operation"
        digest = rec.get("evidence_digest")
        if (not _nonblank_text(digest) or len(digest) != 64 or
                any(c not in "0123456789abcdef" for c in digest)):
            return "version-2 receipt has no lowercase SHA-256 evidence digest"
    outcome, source = rec.get("outcome"), rec.get("source")
    if outcome is None:
        if source is not None or rec.get("matched") is not None:
            return "a legacy receipt with no outcome cannot claim a source"
    elif not _receipt_admissible(outcome, source, rec.get("matched")):
        return ("only matched receiver read-back or receiver-side "
                "deduplication may create a confirmed receipt; accepted "
                "operations and lifecycle reports are hints")
    return None


def load_acknowledgments(state_dir):
    """THE ONLY WAY to read the receipt journal. Raises OutboxError.

    Three rounds of review found the same defect in four different places: a
    complete-but-malformed final line, `--record-receipt` appending to a
    corrupt journal, a record that parsed but asserted nothing, and a
    fail-closed test that grepped its own prose. None of those were four bugs.
    They were one bug wearing four hats: detection lived here and the decision
    to refuse lived in each caller, so every caller got to be wrong
    separately, and every NEW caller got a fresh chance to be wrong.

    The fix is not another check. It is that reading this journal and refusing
    a bad one are now the same operation, so a future caller cannot obtain the
    records without also accepting the refusal. `test_ack` asserts
    structurally that no other function touches RECEIPTS.
    """
    journal = _read_receipts_raw(state_dir)
    problems = journal.problems
    fatal = SJ.fatal_problems(problems)
    if fatal:
        raise OutboxError(
            "the receipt journal cannot be read in full, so no acknowledgment "
            "status derived from it can be trusted:\n" +
            "\n".join(f"  [{f['kind']}] {f['detail']}" for f in fatal) +
            "\n  Repair or remove it, then re-record: intents are keyed, so "
            "re-recording is safe.")
    # Opened ONLY here, and only after the refusal above.
    return journal._records, problems


def _receipt_grade(rec):
    if (_schema_is(rec.get("schema_version"), RECEIPT_SCHEMA_VERSION) and
            _receipt_admissible(rec.get("outcome"), rec.get("source"),
                                rec.get("matched"))):
        return ATTESTED_CONFIRMED
    return ATTESTED_UNSPECIFIED


def _append_receipt_atomic(state_dir, rec):
    """Read, decide and append one receipt under the journal's single lock."""
    bad = _receipt_shape_problem(rec)
    if bad:
        raise OutboxError("refusing malformed receipt: %s" % bad)
    path = Path(state_dir) / RECEIPTS
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    with open(path, "r+b") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise OutboxError(
                "cannot serialise receipt admission on %s: %s. The existing "
                "records must be read under the same lock as the append, or "
                "two drainers can admit different references." % (path, exc))
        SJ._heal_jsonl_tail(fh)
        fh.seek(0)
        try:
            raw = fh.read().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OutboxError("receipt journal is not UTF-8: %s" % exc)
        journal = _parse_receipt_text(raw)
        fatal = SJ.fatal_problems(journal.problems)
        if fatal:
            raise OutboxError(
                "the receipt journal cannot be read in full, so no receipt "
                "may be admitted:\n" +
                "\n".join("  [%s] %s" % (f["kind"], f["detail"])
                          for f in fatal))
        same_key = [old for old in journal._records
                    if old.get("key") == rec["key"]]
        refs = {old.get("ref") for old in same_key}
        if refs and refs != {rec["ref"]}:
            raise OutboxError(
                "intent %s already has receipt reference(s) %s; refusing "
                "conflicting %s" %
                (rec["key"], ", ".join(sorted(refs)), rec["ref"]))
        # An equivalent repeat adds no bytes. A confirmed observation may
        # still upgrade a legacy attestation naming the same reference.
        if same_key and (_receipt_grade(rec) == ATTESTED_UNSPECIFIED or
                         any(_receipt_grade(old) == ATTESTED_CONFIRMED
                             for old in same_key)):
            return same_key[-1]
        fh.seek(0, os.SEEK_END)
        fh.write((json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    return rec


def record_receipt(state_dir, key, ref, op=None, by=None, at=None,
                   observation=None):
    """Record a legacy attestation or a bound receiver confirmation.

    Read that word carefully, because a reviewer caught me overclaiming here.
    This is NOT verified evidence and cannot be. The coordinator has no
    network imports, so it cannot ask the tracker whether ARC-171 really
    closed; anyone who can run this command can write any reference they like.
    What the record establishes is only: this identified writer, at this time,
    said this operation succeeded and returned this reference.

    That is the same class of claim as an agent reporting "done", and this
    repo exists to refuse exactly that when it is dressed up as proof. So the
    mechanism stays (there is no other one available across the network gap)
    and the LABEL carries the weakness: every display says attested, never
    verified. A reader can then go and check the reference by hand, which is
    the only thing that would settle it.

    The historical key/ref form remains a weaker attestation whose source is
    unspecified. Passing a complete observation records the stronger
    read-back-confirmed grade. Neither is verified by this offline process."""
    key = str(key or "").strip()
    ref = str(ref if ref is not None else "").strip()
    if not key or not ref:
        raise OutboxError("a receipt needs a non-blank intent key and tracker "
                          "reference")
    matches = [intent for intent in load_outbox_contract(state_dir)
               if intent.get("key") == key]
    if len(matches) != 1:
        raise OutboxError("no unique persisted outbox intent has key %r" % key)
    intent = require_valid_intent(matches[0])
    envelope = intent["envelope"]
    requested = op or envelope["requested_operation"]
    if requested != envelope["requested_operation"]:
        raise OutboxError("receipt operation %r does not match persisted %r" %
                          (requested, envelope["requested_operation"]))
    outcome = None
    source = None
    matched = None
    if observation is not None:
        result = reconcile_observation(intent, observation)
        if not result["receipt_admissible"]:
            raise OutboxError("only confirmed_by_readback may create a "
                              "confirmed receipt")
        if str(observation.get("reference") or "").strip() != ref:
            raise OutboxError("observation reference does not match --ref")
        outcome = observation["outcome"]
        source = observation["source"]
        matched = observation.get("matched")
    rec = {"key": key, "ref": ref, "op": requested,
           "outcome": outcome, "source": source, "matched": matched,
           "evidence_digest": envelope["evidence_digest"],
           "attested": True,
           "by": by or ((observation or {}).get("by")) or
                 os.environ.get("USER") or "?",
           "at": at or ((observation or {}).get("at")) or time.strftime(
               "%Y-%m-%dT%H:%M:%S%z"),
           "schema_version": RECEIPT_SCHEMA_VERSION}
    return _append_receipt_atomic(state_dir, rec)


def acknowledgment_status(state_dir):
    """Map key -> (status, receipts), plus problems. Derived, never stored.

    CALLERS MUST FAIL CLOSED ON `problems`. Detecting corruption and then
    deriving status from whatever survived is not a safeguard: a journal that
    lost a line reports the intents it can still see as attested, and the one
    it lost as unacknowledged, which is precisely the false-negative-turned-
    false-positive this check exists to prevent."""
    receipts, problems = load_acknowledgments(state_dir)
    by_key = {}
    for r in receipts:
        by_key.setdefault(r.get("key"), []).append(r)
    status = {}
    for key, rs in by_key.items():
        refs = {r.get("ref") for r in rs}
        if len(refs) > 1:
            grade = CONFLICT
        elif any(_receipt_grade(r) == ATTESTED_CONFIRMED for r in rs):
            grade = ATTESTED_CONFIRMED
        else:
            grade = ATTESTED_UNSPECIFIED
        status[key] = (grade, rs)
    return status, problems


def read_outbox(state_dir):
    out = []
    p = Path(state_dir) / OUTBOX
    if not p.is_file():
        return out
    try:
        for line in p.read_bytes().splitlines():
            if line.strip():
                try:
                    out.append(_with_intent_envelope(json.loads(line.decode("utf-8"))))
                except (UnicodeDecodeError, RecursionError, ValueError):
                    continue
    except OSError:
        pass
    return out


def load_outbox_contract(state_dir):
    """Read every persisted intent or fail closed for drain decisions."""
    path = Path(state_dir) / OUTBOX
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise OutboxError("cannot read persisted outbox: %s" % exc)
    intents = []
    keys = set()
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            intent = normalize_intent(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, RecursionError, ValueError) as exc:
            raise OutboxError("outbox line %d is not JSON: %s" %
                              (number, exc))
        problems = validate_intent(intent)
        if problems:
            raise OutboxError("outbox line %d: %s" %
                              (number, "; ".join(problems)))
        key = intent["envelope"]["idempotency_key"]
        if key in keys:
            raise OutboxError("outbox has duplicate idempotency key %r" % key)
        keys.add(key)
        intents.append(intent)
    return intents
