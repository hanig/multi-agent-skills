# Connecting the swarm to an issue tracker

## The network boundary

The coordinator runs on a cluster login node. A tracker connector runs in a
connected client elsewhere. `swarm.py` therefore has no network code and holds
no tracker credentials: it writes intents, and a connected session drains them.
Tracker state is a view of swarm state and never an input to the DAG judge.

MCP core is stateless as of the 2026-07-28 specification and tasks are an
extension. A drain must not infer an initialization sequence or task model from
the word “MCP.” A2A 1.0.0 also has asynchronous task lifecycle states;
`TASK_STATE_COMPLETED` means that a remote task lifecycle settled, not that the
requested tracker mutation is present. Both protocols are connector details
outside the coordinator.

## The versioned intent envelope

`<state-dir>/outbox.jsonl` is append-only, with one intent per line. Existing
payload fields remain readable, but every record has an `envelope` that is the
drain contract:

```json
{
  "envelope": {
    "schema_version": 1,
    "project": "rna-bench",
    "unit": "train",
    "attempt": {"id": "attempt-1", "directory": "runs/train/attempt-1"},
    "idempotency_key": "2bc0934b78772626",
    "requested_operation": "close",
    "evidence_digest": "<sha256 of canonical JSON evidence>",
    "required_connector_capability": "tracker.intent.idempotent-mutation-readback.v1"
  },
  "key": "2bc0934b78772626",
  "project": "rna-bench",
  "unit": "train",
  "verb": "close",
  "attempt_dir": "runs/train/attempt-1",
  "evidence": {"receipt": {}}
}
```

The duplicated routing fields are checked for equality; they are compatibility
aliases, not two sources of truth. The evidence digest binds a connected
session's observation to the evidence it was asked to apply. It does not make
that evidence verified. A persisted pre-envelope record is normalized by the
outbox reader without rewriting its append-only audit bytes.

Inspect intents locally:

```sh
python3 skills/hanig-swarm/scripts/swarm.py outbox --state-dir STATE
```

Validate their shape and the connector capability before a drain:

```sh
python3 skills/hanig-project/scripts/drain_contract.py validate \
  STATE/outbox.jsonl \
  --capability tracker.intent.idempotent-mutation-readback.v1
```

Validation is offline. It checks versions, field bindings, the evidence digest,
attempt identity, close evidence, and the declared connector capability. It
does not probe a connector.

## Drain outcomes are not interchangeable

The connected session reports one of four values:

| Outcome | What it establishes | Receipt? |
|---|---|---|
| `operation_accepted` | The receiver accepted a request | no |
| `asynchronously_completed` | A remote task lifecycle settled | no |
| `confirmed_by_readback` | A receiver read-back or receiver-side idempotency match found the bound operation and reference | yes, confirmed attestation |
| `unknown` | The drain or read-back was ambiguous | no |

Accepted is not completed, and completed is not confirmed. In particular, an
A2A lifecycle report is a hint and never closing evidence. Only a confirmed
receiver match may create the stronger receipt grade. Until a receipt exists,
the intent stays `unacknowledged`. That word means this machine has no receipt
either way. It does not mean the mutation was not filed.

An observation binds all three of the intent's idempotency key, requested
operation and evidence digest:

```json
{
  "schema_version": 1,
  "project": "rna-bench",
  "unit": "train",
  "attempt": {"id": "attempt-1", "directory": "runs/train/attempt-1"},
  "idempotency_key": "2bc0934b78772626",
  "requested_operation": "close",
  "evidence_digest": "<same sha256>",
  "connector_capability": "tracker.intent.idempotent-mutation-readback.v1",
  "outcome": "confirmed_by_readback",
  "source": "receiver_readback",
  "matched": true,
  "reference": "ARC-171"
}
```

Reconcile without writing anything:

```sh
python3 skills/hanig-project/scripts/drain_contract.py reconcile \
  --intent intent.json --observation observation.json
```

After inspecting the result, add `--state-dir STATE` to append the outcome to
`outbox-reconciliations.jsonl`. A confirmed match also appends its attested
receipt. The program is still offline: “confirmed” means the connected session
reported a read-back, not that this program independently queried the tracker.

The two append-only journals have different jobs:

- `outbox-reconciliations.jsonl` records descriptive observations, including
  accepted, asynchronous and unknown outcomes. It is never acknowledgment or
  closure authority.
- `outbox-receipts.jsonl` is the input from which outbox acknowledgment status
  is derived. A version-2 receipt with a confirming source is displayed as
  `attested_confirmed`. The historical key/ref command remains available and
  is displayed as the weaker `attested`; it records no read-back basis.

For compatibility, this still records the weaker form:

```sh
python3 skills/hanig-swarm/scripts/swarm.py outbox --state-dir STATE \
  --record-receipt KEY --ref ARC-171
```

The compatibility command cannot synthesize the stronger grade from flags.
The connected drainer instead supplies a complete bound observation to the
reconciliation command:

```sh
python3 skills/hanig-project/scripts/drain_contract.py reconcile \
  --intent intent.json --observation observation.json --state-dir STATE
```

Neither grade is independent verification by this offline program. Receipt
admission reloads the unique persisted outbox intent; caller-supplied copies
cannot redefine its project, operation, evidence digest, or attempt.

## Crash and replay rule

The dangerous case is: the tracker mutation succeeds, then the draining
session dies before recording a receipt. On restart, absence of that receipt
does not authorize another blind mutation. Record `unknown` and do not replay.
Resolve it through either:

- receiver-side deduplication using the original idempotency key, returning the
  original reference; or
- a read-back bound to the project, unit, operation and evidence digest.

An ambiguous read-back establishes neither success nor absence. Reconciliation
therefore records `unknown`, `replay: false`, and no receipt. This is why the
required connector capability includes both idempotent mutation and read-back.

A connected drainer claiming that capability must present the original key to
the receiver's deduplication mechanism before any mutation, or first perform a
read-back that can resolve it. The offline tools cannot implement that remote
step and deliberately do not pretend to. They always return `replay: false`.

When reconciliation is confirmed, the receipt is fsynced before the descriptive
reconciliation record. A crash between those writes can omit the description,
but it cannot lose the acknowledgment and then invite replay. Retrying repairs
the description without appending another equivalent receipt. Receipt
read/repair/admission/append is covered by one journal lock, so concurrent
drainers cannot admit two receiver references for one key.

## State to requested operation

| Unit state | Operation | Meaning |
|---|---|---|
| `SUBMITTED` | `start` | work started |
| `DONE` | `close` | the kind's fixed closure evidence was admitted |
| `FAILED` | `reopen` | the command failed |
| `FAILED_EVIDENCE` | `reopen` | no verdict arrived |
| `PREEMPTED` | `note` | a fresh attempt will be minted |
| `HELD` | `block` | an upstream unit will not complete |
| `NEEDS_HUMAN` | `block` | a person must act |
| `READY_FOR_PR` | `open_pr` | code output exists but no admitted merge does |

A `close` intent always carries the evidence admitted by the coordinator.
Lifecycle observations from MCP tasks, A2A tasks, Paseo agents, or any other
remote worker cannot replace it.
