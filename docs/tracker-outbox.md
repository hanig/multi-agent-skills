# Connecting the swarm to an issue tracker

## The constraint that decides the design

The coordinator runs on a cluster login node. An MCP connector runs in the
Claude client on a laptop. These are different machines, and the coordinator
has no network code at all.

That is deliberate, not an oversight. The alternative is a tracker API token
sitting in a file on a shared login node, readable by anyone who can read your
home directory, on a machine you do not administer.

So `swarm.py` never calls a tracker. It writes **intents**, and something that
can reach the tracker applies them later.

## What the coordinator writes

`<state-dir>/outbox.jsonl`, append-only, one intent per line:

```json
{"key":"2bc0934b78772626","project":"rna-bench","unit":"train","verb":"close",
 "unit_state":"DONE","why":"the unit's predicate returned DONE",
 "at":"2026-08-28T14:02:11-0700","job_id":"918234",
 "attempt_dir":"runs/train/attempt-1","evidence":{"receipt":{...}},
 "applied":false}
```

Inspect before anything is sent:

```bash
python3 swarm.py outbox --state-dir .swarm/state
```

## Three properties, each a real defect avoided

**A tracker outage never alters swarm state.** The swarm is authoritative; the
tracker is a view of it. Linear being down stalls your ticket board, not your
DAG. The dependency points one way only.

**Draining twice cannot open two issues.** Every intent carries an idempotency
key over `(project, unit, state, attempt_dir)`. A drain that dies half way
through and retries converges instead of duplicating. Including `attempt_dir`
means a preempted unit that reruns is correctly treated as new work.

**Nothing closes on a self-report.** A `close` intent is emitted only from a
predicate verdict and carries the receipt that produced it. An agent writing
"done" on its own ticket is precisely the self-assertion this whole family of
tools refuses to accept. A drain that cannot see the evidence must refuse to
close the issue.

## State to tracker verb

| Unit state | Verb | Meaning |
|---|---|---|
| `SUBMITTED` | start | work started |
| `DONE` | close | the predicate returned DONE |
| `FAILED` | reopen | the command failed |
| `FAILED_EVIDENCE` | reopen | no verdict arrived; evidence never landed |
| `PREEMPTED` | note | preempted; a new attempt will be minted |
| `HELD` | block | an upstream unit will not complete |

A state absent from this table emits nothing. Enumerating the good set beats
guessing at a mutation from a state the table's author never considered.

## Writing a drain

A drain is thin by construction: read the outbox, skip `applied`, call the
tracker, mark applied. Roughly thirty lines per backend. It must:

1. refuse to apply a `close` whose `evidence` is null
2. mark `applied` only after the tracker confirms
3. be safe to rerun, which the key already guarantees

## Status as of 2026-08-28

The outbox ships and is tested (`tests/test_outbox.py`, 9 tests). No drain is
written yet.

Blocking on setup, not on design: a search of the connector registry returns no
Linear connector, so a drain cannot be written against `mcp__linear__*` from
this machine today. Two ways forward once the account is up. Add a Linear MCP
connector if one becomes available, which is the cleaner path since no token
ever touches disk. Otherwise write the drain against Linear's GraphQL API with
a token held **on the laptop only**, never synced to a cluster.

Asana is already connected here and would work as a backend today. The outbox
does not care which; that is the point of writing intents rather than calls.
