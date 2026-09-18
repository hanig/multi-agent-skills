#!/usr/bin/env python3
"""Validate and reconcile tracker drain observations, entirely offline.

The tracker contract is owned by the installed hanig-swarm sibling. This
project-side program is only its connector-neutral CLI; it has no network
imports and carries no second validator or receipt writer.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import skill_paths


_PROJECT_DIR = os.environ.get("HANIG_PROJECT_DIR") or Path(__file__).parents[1]
_SWARM_DIR = skill_paths.sibling_skill_root(
    _PROJECT_DIR, "hanig-project", "hanig-swarm")
sys.path.insert(0, str(_SWARM_DIR / "scripts"))
import swarm as contract  # noqa: E402  declared installed sibling


INTENT_SCHEMA_VERSION = contract.INTENT_SCHEMA_VERSION
OBSERVATION_SCHEMA_VERSION = contract.OBSERVATION_SCHEMA_VERSION
RECEIPT_SCHEMA_VERSION = contract.RECEIPT_SCHEMA_VERSION
RECONCILIATION_SCHEMA_VERSION = contract.RECONCILIATION_SCHEMA_VERSION
CONNECTOR_CAPABILITY = contract.INTENT_CONNECTOR_CAPABILITY
OPERATION_ACCEPTED = contract.OPERATION_ACCEPTED
ASYNC_COMPLETED = contract.ASYNC_COMPLETED
CONFIRMED_BY_READBACK = contract.CONFIRMED_BY_READBACK
UNKNOWN = contract.UNKNOWN
MUTATION_RESPONSE = contract.MUTATION_RESPONSE
A2A_LIFECYCLE = contract.A2A_LIFECYCLE
RECEIVER_READBACK = contract.RECEIVER_READBACK
RECEIVER_DEDUPLICATION = contract.RECEIVER_DEDUPLICATION
CONFIRMING_SOURCES = contract.CONFIRMING_SOURCES
OUTBOX = contract.OUTBOX
RECEIPTS = contract.RECEIPTS
RECONCILIATIONS = "outbox-reconciliations.jsonl"


class ContractError(ValueError):
    """Input does not satisfy the shared offline drain contract."""


canonical_digest = contract._intent_evidence_digest
attempt_identity = contract._intent_attempt_identity
build_envelope = contract._intent_envelope
normalize_intent = contract.normalize_intent
validate_intent = contract.validate_intent
validate_observation = contract.validate_observation


def _raise_as_contract_error(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except contract.OutboxError as exc:
        raise ContractError(str(exc))


def reconcile(intent, observation):
    """Classify an observation; no returned value authorizes remote replay."""
    result = _raise_as_contract_error(
        contract.reconcile_observation, intent, observation)
    receipt = None
    if result.pop("receipt_admissible"):
        normalized = _raise_as_contract_error(
            contract.require_valid_intent, intent)
        envelope = normalized["envelope"]
        receipt = {
            "key": envelope["idempotency_key"],
            "ref": observation["reference"].strip(),
            "op": envelope["requested_operation"],
            "outcome": observation["outcome"],
            "source": observation["source"],
            "matched": observation.get("matched"),
            "evidence_digest": envelope["evidence_digest"],
            "attested": True,
            "by": observation.get("by") or os.environ.get("USER") or "?",
            "at": observation.get("at") or time.strftime(
                "%Y-%m-%dT%H:%M:%S%z"),
            "schema_version": RECEIPT_SCHEMA_VERSION,
        }
    result["receipt"] = receipt
    result["note"] = (
        "receiver-side match confirmed by read-back; receipt may be recorded"
        if receipt else
        "a remote lifecycle or ambiguous drain report is a hint only; it "
        "cannot close work and does not authorize replay")
    return result


def record_reconciliation(state_dir, intent, observation):
    """Persist a bound hint; persist a confirmed receipt first if admissible.

    The supplied intent locates a key only. The append-only outbox record is
    reloaded as authority, so a self-consistent caller forgery cannot mint a
    receipt or even a misleading lifecycle reconciliation.
    """
    supplied = _raise_as_contract_error(contract.require_valid_intent, intent)
    key = supplied["envelope"]["idempotency_key"]
    matches = [item for item in _raise_as_contract_error(
                   contract.load_outbox_contract, state_dir)
               if item.get("key") == key]
    if len(matches) != 1:
        raise ContractError(
            "no unique persisted outbox intent has key %r" % key)
    persisted = _raise_as_contract_error(
        contract.require_valid_intent, matches[0])
    result = reconcile(persisted, observation)
    if result["receipt"] is not None:
        _raise_as_contract_error(
            contract.record_receipt, state_dir,
            persisted["envelope"]["idempotency_key"],
            result["receipt"]["ref"], observation=observation)
    record = dict(result)
    record.pop("receipt")
    record["record_kind"] = "reconciliation"
    record["at"] = observation.get("at") or time.strftime(
        "%Y-%m-%dT%H:%M:%S%z")
    _raise_as_contract_error(
        contract._fsync_append, Path(state_dir) / RECONCILIATIONS, record)
    return result


def _json_records(path):
    raw = Path(path).read_text()
    stripped = raw.strip()
    if not stripped:
        return []
    try:
        value = json.loads(stripped)
    except ValueError:
        records = []
        for number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError as exc:
                raise ContractError("%s line %d: %s" %
                                    (path, number, exc))
        return records
    return value if isinstance(value, list) else [value]


def _load_one(path, what):
    records = _json_records(path)
    if len(records) != 1:
        raise ContractError("%s must contain exactly one object" % what)
    return records[0]


def cmd_validate(args):
    capabilities = args.capability if args.capability else None
    records = _json_records(args.path)
    failures = []
    for index, intent in enumerate(records, 1):
        for problem in validate_intent(intent, capabilities):
            failures.append({"record": index, "problem": problem})
    payload = {"valid": not failures, "records": len(records),
               "failures": failures}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif failures:
        for failure in failures:
            print("record %d: %s" % (failure["record"],
                                     failure["problem"]), file=sys.stderr)
    else:
        print("VALID: %d intent envelope(s)" % len(records))
    return 0 if not failures else 2


def cmd_reconcile(args):
    intent = _load_one(args.intent, "intent")
    observation = _load_one(args.observation, "observation")
    result = (record_reconciliation(args.state_dir, intent, observation)
              if args.state_dir else reconcile(intent, observation))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate JSON or JSONL intents")
    validate.add_argument("path")
    validate.add_argument("--capability", action="append", default=[])
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(function=cmd_validate)
    reconciler = sub.add_parser("reconcile", help="classify a drain observation")
    reconciler.add_argument("--intent", required=True)
    reconciler.add_argument("--observation", required=True)
    reconciler.add_argument("--state-dir")
    reconciler.set_defaults(function=cmd_reconcile)
    args = parser.parse_args(argv)
    try:
        return args.function(args)
    except (ContractError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
