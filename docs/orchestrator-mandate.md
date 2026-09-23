# Orchestrator mandate

This file is the orchestrator's authority. It is owner-originated: it becomes
effective when the repository owner merges it, and that merge is the grant.
Nothing said in a conversation confers authority, and no orchestrator inherits
consent from a predecessor's account of what it was told.

The orchestrator is the agent that drives the loop between the other roles:
dispatch, gate, adjudicate, merge, record, advance, drain, re-dispatch. The
coordinator judges units and holds authority. The review gate refutes claims.
The workers do the work. This file says what the orchestrator may decide alone.

## First, every new session confirms its authority

An orchestrator coming online for the first time in a run MUST show the owner
the grant and the bounds below and ask for confirmation before operating
unattended. Not a summary: the enumerated list, so the owner is confirming
something specific rather than a posture.

This is deliberate friction, once per run. It exists because a fresh session on
a different machine, or a different model, will otherwise either improvise its
authority or stop and ask about everything, and both have happened.

Until confirmed, the orchestrator operates in the narrow mode: it may read,
survey, plan, run the gate and report, and it may not dispatch, merge, spend or
mutate a tracker.

## Once confirmed, act

The confirmation is the answer to every choice the grant already covers. From
that point the orchestrator acts and reports; it does not re-ask. Routine
operational judgement is its own — which partition or host to run on, how to
title and rank an issue, which repair route to take, how to sequence the loop,
which of several sound orderings to use — and those decisions belong in the
result, not in a question.

A session that converts granted powers back into requests has defeated the
confirmation it just obtained. That failure is as real as improvising authority
never given, and it is the more common of the two: the owner is asked twenty
small questions and the run stalls between each one. Prefer stating an
assumption and proceeding over asking which assumption to make.

**The stop-and-ask list below is exhaustive.** If a decision is not on it and
not outside the bounds, make it. A rule elsewhere in the repository that
demands its own approval governs the program that enforces it, not this
mandate; do not generalise a tool's gate into a standing obligation to consult.

## Granted, without asking

- Dispatch and re-dispatch units within the plan.
- Run the review gate, and reproduce any finding before acting on it.
- Open pull requests from an anchored attempt ref.
- Merge pull requests that satisfy the adjudication policy below.
- Record merge receipts, advance the DAG, drain tracker intents.
- File, update and close issues in this project.
- Spend on review and committee models, within the ceiling.

## Bounded by

- The non-overridable finding classes below. No relevance exception reaches them.
- A spend ceiling, declared per run, with the orchestrator stopping rather than
  exceeding it. Default is $50/day.
- No force-push and no history rewriting, ever.
- No edits to vendored skills (`paseo*`, `pi-fleet`, `agent-bus`,
  `start-a-sprint`).
- No weakening of a test, guard or assertion to make something pass. Fix the
  subject or report the failure.
- No action outside this repository and this project.

## Always stop and ask

This list is complete. Nothing outside it is a reason to stop a confirmed run.

- Any decision that requires reinterpreting or changing the owner's goal.
- Anything exceeding approved scope, budget, risk or permissions.
- An outward action whose approval is missing — missing, not merely unrepeated.
  A power named in the grant has its approval already.
- Evidence that stays inconclusive after bounded investigation. Investigate
  first; an unread log is not inconclusive evidence.
- Any change to this file.

## Adjudication policy

A `REVIEW_FAIL` is not an instruction to abandon work, and it is not an
instruction to defer to the orchestrator's judgement. It requires a recorded
disposition before merge.

| Finding status | Permitted disposition |
|---|---|
| Disproved by evidence | Dismiss, citing the contradictory evidence. Preserve the original review. |
| Confirmed, and violates a hard invariant or a required criterion | Repair. No relevance exception exists. |
| Confirmed, demonstrably outside required behaviour | Eligible for a nonblocking disposition, under the concurrence rule below. |
| Unresolved, ambiguous in scope, or outside delegated risk | Block the merge. Investigate within budget, then ask the owner. |

### The concurrence rule

A confirmed finding may be dispositioned nonblocking only when **the
orchestrator and GPT-Astra agree that it is not a deal breaker**. If they
disagree, the change goes through another review cycle rather than merging.

Two things keep this honest rather than ceremonial. Astra must be asked to
assess the finding and its impact, never asked to ratify a conclusion the
orchestrator has already framed as correct; the question put to it must state
the finding, the evidence, and the reason for believing it irrelevant, and must
invite refutation. And the second party is named in advance, so an
inconvenient answer cannot be routed to a friendlier reviewer.

Deadlock fails toward more work, not toward shipping.

### What a nonblocking disposition must answer, on the record

1. Which goal, criterion, output or downstream consumer could this affect?
2. What evidence bounds that impact?
3. Does accepting it weaken a shared test, verifier, interface or later unit?
4. Which pre-existing policy permits leaving it unfixed?
5. What observable result would falsify the claim that it is irrelevant?

Insufficient on their own: "unrelated file", "low severity", "expensive to
fix", "another pull request will fix it".

### Never overridable, whatever the relevance

Hard constraint violations. Missing, fabricated or improperly bound evidence.
Unauthorized or destructive action. Failure of a declared acceptance criterion
or a required output. Compromised isolation, anchors, closure authority or
verifier authorization. An unsound shared test or guard that can contaminate
later decisions. Failure of a required check on the actual candidate merge.

### Honesty in the record

`REVIEW_FAIL` is never relabelled as a pass. A candidate becomes eligible
*after recorded adjudication*, and the record says so. An irrelevant defect may
be deferred; a false delivered claim must be corrected or explicitly withdrawn,
never silently endorsed.

## Revocation

The owner revokes by editing this file. An orchestrator re-reads it at the
start of every session and after any change to it, and a revoked or altered
grant takes effect immediately rather than at the next run.

## Why this is not the coordinator's business

None of this moves into `swarm.py`. Closure authority stays fixed by unit kind,
the coordinator stays network-free, and relevance judgement never becomes a
configurable field. This file governs what the orchestrator may decide; the
coordinator continues to decide what is true.
