# Plan: mechanical control of the Linear DAG

Status: APPROVED by the owner on 2026-10-05 (v3, with D1-D3 as recommended). v1 by the orchestrator session (Claude Opus
5.5); v2 and v3 fold in two co-design rounds with gpt-6-astra, which agreed
with v3's shape. For owner approval. Not a review-gate pass.

## Problem (owner, 2026-10-05)

The orchestrator claims Linear is up to date on the strength of its own saves,
files new issues with blockers written into the description instead of as
relations, loses track of which issues can form a wave, and once lost the
project and filed orphan issues.

## Measured (read-only API, 2026-10-05; to be requeried before any repair)

ARC team, last 30 days, issues created by the API user: 847. None lack a
project, and none do in any team over 90 days, so the orphan incident was under
another account, was moved since, or was a wrong-project filing this query
cannot see. 34 have no relations (11 open). A narrow pattern found 3
dependency references with no backing relation.

## Principles

- **Partitioned authority.** Swarm-plan units: `plan.json` `needs` plus
  coordinator completion evidence, as today; tracker state never drives the
  swarm judge. Ad-hoc repo issues: Linear `blocks` relations. Disagreement is
  reported as a concrete edge or state diff with identities and read time,
  never silently unioned or deleted.
- **Three-valued results.** Every check reports CLEAN, DRIFT or UNKNOWN.
  Incomplete pagination, a pagination race or repeated cursor, rate limits,
  an inaccessible blocker or a failed read is UNKNOWN, never CLEAN. A
  paginated read is not an atomic snapshot: the audit records its read
  interval, re-checks identities it saw at the start against the end, and
  reports UNKNOWN when they disagree.
- **Bound identity.** Every command is bound to workspace, team and project
  UUIDs plus repository identity, recorded once in a binding file and checked
  before any read is trusted or any mutation is sent. Derived ids are
  namespaced by workspace and repository, with a migration for the ids the
  current branch would mint.
- **Scoped claims.** The furthest any output goes is "consistent within this
  audited scope at <read interval>". Repository code cannot stop an agent
  saying "in sync" in conversation; it can make every generated report and
  controlled publication path carry only audited claims.

## Slices (each its own plan-reviewed PR; existing paths stay until replaced)

**PR 1 = slices 1 and 2 together** (both read-only; slice 2 is the consumer
that proves slice 1 is used), as separate modules with separate tests.

**Slice 1: binding and read-only audit.** Shared API reader (the
`linear_api.py` transport seam from `hanig/linear-api`), the binding file and
its validation, and `linear_sync.py audit`. Audit checks, each CLEAN, DRIFT or
UNKNOWN with evidence:
- issues carrying this repository's operation or unit markers, or known
  through recorded mappings (draft identifiers, outbox receipts), that sit
  outside the bound project (a project mention alone does not establish
  ownership);
- open ad-hoc issues with neither a relation nor a recorded independence
  reason;
- dependency candidates in prose (an identifier near "blocked by", "depends
  on", "must land before", preferably in a dependency section; quotations,
  code, negations, historical descriptions and "related" excluded) with no matching relation, reported
  with the sentence and proposed direction as advisory, not as failure;
- cycles; In Progress issues with open blockers; canceled, duplicate,
  archived and completed blockers distinguished rather than all read as done;
- tracker state contradicting coordinator state or plan `needs` for swarm
  units.
It records scope, read interval, pagination completeness, bindings and the
plan/state/outbox inputs it read. No mutation anywhere in this slice.

**Slice 2: audit-backed reporting.** The report producer (hanig-orchestrate's
report step and `merge_unit.py` after advance and drain) invokes the audit and
renders its structured result. A clean tracker section requires matching
binding, complete coverage, successful reads, matching plan/state/outbox
versions, no unresolved discrepancy and a stated interval. A write this
program made, or a changed local input, invalidates reuse of a prior result;
an external write after the read interval is outside what the result claims. Merge success and tracker status
are reported separately, and an unavailable audit neither implies sync nor
blocks the merge. Tests drive the real report consumer with absent, stale,
mismatched, partial and failing audits; none renders clean; removing the
requirement fails them.

**Slice 3: drain.** Outbox draining with project membership checked before
any state change and the identity namespacing above. Revised 2026-10-05
after its plan review exhausted and a design consult with Sol: one drainer
per project per host (flock), comments posted in order, one reconciliation
per touched issue to its latest-ordered genuine state intent on every run,
and an `intent_order` audit check. The guarantee is eventual reconciliation,
not race-free correctness across hosts; detail in `docs/plan-linear-drain.md`.
Finding 7 is repaired here.

**Slice 4: writes.** Approved `tickets.json` filing (approval digest covering
every executed mutation, including edge lists; the immutable approved spec
separated from mutable progress; secret-bearing content rejected before
mutation rather than redacted into different bytes) and a direct
`linear_sync.py issue` command for ad-hoc issues backed by a durable operation
record: bindings, title, body, dependency changes or an independence reason,
a stable operation id persisted before creation, and the authorization
reference, with `--preview` and replay of a recorded operation. Under a confirmed orchestrator mandate it executes within scope
without asking again; outside it, it needs approval; a caller-supplied flag
cannot manufacture authority. Partial creation (issue made, relation failed)
is resumable and withholds readiness. Edits and removals go through the same
path. Structured dependency syntax that contradicts the relations is a hard
failure. Every write is followed by a read-back with cycle detection, because
a pre-write cycle check races concurrent graph edits. Findings 4, 5 and 6 are
repaired here. The `tickets.sync_blocked_by` behavior of listing every undeclared
blocker for removal gets an explicit cross-boundary policy first, so filing
cannot erase an ad-hoc edge a plan unit depends on.

**Slice 5: waves and harness restriction.** `linear_sync.py waves` computes
dependency eligibility for ad-hoc issues from Linear and keeps plan-based
dispatch for swarm units; eligibility is not dispatchability (active
attempts, resources, authorization, concurrency, and an execution claim
before dispatch still apply). An explicit prerequisite-completion policy
decides which terminal states satisfy a blocker (proposed: completed does;
canceled, duplicate and archived are UNKNOWN until a person says otherwise). Deny the Linear MCP write tools in this repo's
`.claude/settings.json`, described as a Claude Code control only, not a
Linear-wide boundary. Evidence: (a) a unit test comparing the settings with
an independently maintained inventory of Linear write tools; (b) a separate
release-time harness beside `tests/native_agent_validation.py` that runs the
authenticated CLI against a controlled MCP server recording real requests:
reads arrive, denied writes do not, and with only the deny removed the same
write probes arrive. A declined call, an unrelated prompt or a missing tool
is not a pass; a missing CLI or credential is UNAVAILABLE. It records CLI
version, launch arguments, settings identity, revision and time, covers every
denied tool and every claimed launch configuration, and reruns when any of
those change. Untested combinations stay unverified.

## The relation-less backlog

The 34 issues are a dated migration queue, not 34 defects. Requery; take the
11 open issues and 3 prose references first; classify each as independent,
dependent, historical or unresolved with a recorded reason; repair through
the slice-4 path and read back. No mass edge creation, no blanket exemption.

## Disposition of the open hanig/linear-api findings

Branch and findings are preserved; the split must not reset their history.
Each stays visibly open until a mutation-tested repair closes it:
1, 2 -> slice 4 (reject, don't redact; digest every executed mutation);
3 -> slice 4, per D3; 4, 5, 6 -> slice 4; 7 -> slice 3; 8 -> D2.

## Review exit criterion

The two exhausted cycles on `hanig/linear-api` show that a 2,400-line,
135k-character change does not converge. Each PR here gets plan review before
code and one implementation cycle. A cycle that exhausts goes to the
step-back process (consult Sol on the design before re-dispatching) and then
to the owner for adjudication of what remains, never to an open-ended third
cycle.

## Owner decisions

D1. Credential scope. DECIDED 2026-10-05: out of scope. The substantive change it
would make is removing agents' access to every write route (the shared key,
OAuth connectors such as the Linear MCP, credential files, browser sessions)
and routing writes through an independently enforced writer, which may still
run on each host. A same-user wrapper around a readable key does not create
that separation.
D2. Finding 8. DECIDED 2026-10-05: the first option. Accept "this program never copies the key it loads" as the
promise (a key the operator put in another variable or in outbox data is a
declared limit), or require scrubbing. Whichever is chosen, every acceptance
criterion and document changes together.
D3. Old approvals. DECIDED 2026-10-05: re-approve.

## Resolved between the co-designers (round 2)

The MCP-deny evidence (slice 5) and shipping slices 1 and 2 as one PR.
