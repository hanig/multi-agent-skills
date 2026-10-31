"""Lease-serialized merge requests and bound, in-process completion receipts.

Intent state is no-replay/recovery state, never successful-completion authority.
Only a matching durable receipt validated on readback establishes completion.
Trusted single-host writers, stable lock identities; no process attribution.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path

from d5_completion import Provider
from d5_authority import AuthorityError
import d5_protocol_locks as protocol
import d5_readiness as readiness
import d5_registry as registry
import swarm_journal as journal


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class Handle:
    effect_id: str
    state_dir: str
    binding: bytes


@dataclass(frozen=True)
class Observed:
    effect_id: str
    receipt: bytes


def _require(condition, message):
    if not condition:
        raise AuthorityError("D5 merge journal: " + message)


def _held(state_dir):
    state_dir = readiness.activation_directory(state_dir)
    _require(registry.d5_active(state_dir), "inactive")
    ready = readiness.require(state_dir)
    _require(any(key == tuple(ready["record_key"]) and rank == 0
                 for key, rank, _, _ in protocol.locks._held()), "caller DRAIN required")
    return ready


def _path(handle):
    return Path(handle.state_dir) / ("merge-unit-" + handle.effect_id + ".json")


def _read(handle):
    import merge_unit as M
    _require(type(handle) is Handle, "invalid handle")
    ready = _held(handle.state_dir)
    bound = json.loads(handle.binding)
    _require(bound["installation"] == ready, "installation mismatch")
    row = M.read_object(_path(handle))
    _require(row.get("operation_id") == handle.effect_id and
             row.get("d5", {}).get("binding") == bound, "binding mismatch")
    original = json.loads(bytes.fromhex(bound["correlation"]["intent"]))
    _require(all(row.get(k) == v for k, v in original.items() if k != "phase"), "request changed")
    _require(row["d5"]["state"] in ("Bound", "Attempted", "Recorded"), "invalid state")
    _require(row["phase"] == ("receipt_recorded" if row["d5"]["state"] == "Recorded"
                             else "merge_requested"), "invalid intent phase")
    return row


def _write(handle, row):
    import merge_unit as M
    # OD3: do not write even N after the coordinator lease has been lost.
    readiness.lease_required(handle.state_dir)
    M.durable_write(_path(handle), row)


def _receipt(handle, row, outcome):
    PROVIDER.validate_outcome(outcome)
    import merge_unit as M
    _require(outcome.effect_id == handle.effect_id, "outcome operation mismatch")
    value = json.loads(outcome.receipt)
    binding = row["binding"]
    expected = dict((k, binding[k]) for k in ("unit", "repo", "target", "head"))
    _require(all(value.get(k) == v for k, v in expected.items()), "outcome binding mismatch")
    host, repo = M.forge_route(binding["repo"])
    M.observed_pr_url({"url": value["pr"]}, host, repo, binding["pr"])
    _require(value["target_commit"] == row["target_before_request"], "target mismatch")
    value["d5"] = dict(schema_version=1, operation_id=handle.effect_id,
                       binding=json.loads(handle.binding))
    return value


def _readback(handle, expected):
    import merge_unit as M
    receipts, problems = M.S.load_merge_receipts(handle.state_dir)
    _require(not problems and any(row == expected for row in receipts),
             "missing or unconfirmed bound receipt")


@dataclass(frozen=True)
class Capability:
    handle: Handle
    recovery: bool = False

    def uncertain(self):
        row = _read(self.handle)
        if row["d5"]["state"] == "Recorded":
            _readback(self.handle, row["d5"]["receipt"])
            return
        _require(row["d5"].get("classification") != "not_submitted", "not submitted")
        row["d5"].update(state="Attempted", classification="unknown")
        _write(self.handle, row)

    def complete(self, outcome):
        row = _read(self.handle)
        expected = _receipt(self.handle, row, outcome)
        if row["d5"]["state"] == "Recorded":
            _require(row["d5"]["receipt"] == expected, "conflicting completion")
            _readback(self.handle, expected)
            return outcome
        _require(row["d5"].get("classification") in ("submitted", "unknown") or
                 (self.recovery and row["d5"]["state"] == "Bound"),
                 "submission is unconfirmed")
        readiness.lease_required(self.handle.state_dir)
        journal._fsync_append(Path(self.handle.state_dir) / "merge-receipts.jsonl",
                              expected, sync_directory=True, unique_key=("d5", "operation_id"))
        _readback(self.handle, expected)
        row["d5"].update(state="Recorded", receipt=expected)
        row["phase"] = "receipt_recorded"
        _write(self.handle, row)
        return outcome


class MergeProvider(Provider):
    def validate_request(self, operation, boundary):
        import merge_unit as M
        _require(type(boundary) is M._MergeBoundary and
                 operation.intent == encoded(boundary.intent), "journal binding requires designated merge boundary")

    def prepare(self, checkout, plan_id, state_dir, **binding):
        import merge_unit as M
        ready = _held(state_dir)
        _require(binding["kind"] == "merge", "wrong operation class")
        original = json.loads(bytes.fromhex(binding["correlation"]["intent"]))
        effect_id = binding["correlation"]["request_id"]
        # The legacy key starts the chain; durable resolutions select its
        # successor. The same reader retains every unresolved-intent fence.
        operation, path, prior = M.current_intent(
            Path(state_dir), original["binding"], original["root"], repair=False)
        _require(effect_id == operation == original["operation_id"], "operation mismatch")
        _require(original["phase"] == "merge_requested" and "d5" not in original,
                 "invalid request")
        _require(binding["scope"] == dict(plans=[plan_id], repositories=[original["binding"]["repo"]],
                 actions=["merge"], trackers=[]), "request scope mismatch")
        _require(ready["plan_id"] == plan_id, "plan mismatch")
        bound = dict(binding, installation=ready)
        handle = Handle(effect_id, str(readiness.lease_required(state_dir)), encoded(bound))
        _require(path == _path(handle) and prior is None, "unresolved request cannot replay")
        M.S.load_merge_receipts(state_dir)
        row = dict(original, d5=dict(state="Bound", binding=bound))
        _write(handle, row)
        return handle

    def classify(self, attempt, state):
        row = _read(attempt)
        _require(state in ("submitted", "not_submitted"), "invalid classification")
        if row["d5"]["state"] == "Recorded":
            _require(state == "submitted", "Recorded is absorbing")
            _readback(attempt, row["d5"]["receipt"])
            return attempt
        _require(row["d5"]["state"] == "Bound" and "classification" not in row["d5"],
                 "request already classified")
        row["d5"].update(state="Attempted", classification=state)
        _write(attempt, row)
        return attempt

    def bind(self, handle):
        _read(handle)
        return Capability(handle)

    def validate_outcome(self, outcome):
        import merge_unit as M
        _require(type(outcome) is Observed and type(outcome.receipt) is bytes, "invalid observed outcome")
        row = json.loads(outcome.receipt)
        _require(set(row) == {"unit", "repo", "target", "head", "pr", "merged_as", "method",
                             "target_commit", "merged", "attested", "integration_status"}, "invalid receipt")
        _require(row["merged"] is True and row["attested"] is True and row["method"] == "squash"
                 and row["integration_status"] == "candidate-verified", "unvalidated success")
        for key in ("head", "merged_as", "target_commit"):
            M.oid(row[key])


PROVIDER = MergeProvider()


@contextmanager
def recovery_drain(state_dir):
    """Own a nonblocking exclusive DRAIN for the completion-only operator.

    ARC-3762-OD1: the original submit owner must have exited after an
    exceptional unwind. Never borrow, upgrade, unlock or close its holder.
    Reuse R1's safe opener, ordering, tracking and close-only cleanup; the
    coordinator lease precedes this independent acquisition. No new lock or
    release protocol, and no readiness/installation substitution.
    """
    state_dir = readiness.activation_directory(state_dir)
    _require(registry.d5_active(state_dir), "inactive")
    ready = readiness.require(state_dir)
    locks = protocol.locks
    _require(not locks._held(), "recovery requires independent DRAIN before START")
    with locks.pin(ready["record_dir"]) as record:
        record._order(0)
        fd = record._open(0)
        with locks._cleanup(os.close, fd):
            try:
                locks.fcntl.flock(fd, locks.fcntl.LOCK_EX | locks.fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AuthorityError("D5 merge recovery: DRAIN is held; wait for the owner to exit") from exc
            with record._track(0, False, fd):
                yield


def recover(state_dir, intent, outcome):
    """Complete under caller DRAIN; the operator uses recovery_drain after exit.

    This provider capability neither releases an original R6 holder nor
    authorizes another submission or installation.
    """
    _require(type(intent.get("d5")) is dict, "legacy intent cannot authorize active recovery")
    handle = Handle(intent["operation_id"], str(Path(state_dir).resolve()),
                    encoded(intent["d5"]["binding"]))
    _read(handle)
    return Capability(handle, recovery=True).complete(outcome)
