# Handoff and takeover detail

## Three-part handoff

Authority references carry the owner mandate, durable approvals, anchored policy, and authoritative run identity. The decision ledger carries attempts, anchors, reviewed heads and bases, findings, evidence, dispositions, challenges, clauses, residual obligations, and invalidation conditions. Operational continuation carries pending actions, uncertain effects, pull requests, liveness observations, budgets, next safe actions, blockers, and wakeups. <!-- declaration: handoff.contents -->

An explicitly undecided unit is recoverable; a judgment that exists only in session memory is not. The ledger distinguishes unadjudicated, investigation pending, blocked, and durably adjudicated states. <!-- declaration: handoff.contents -->

## Authorization transfer

Durable successor authority identifies the run, permitted actions and decisions, limits, policy binding, duration, and revocation. Credentials and predecessor assertions confer none of those properties. <!-- declaration: handoff.transfer -->

## Arrival checks

The successor re-establishes repository, run, coordinator, anchor, installed-code, evidence, remote, budget, liveness, and exclusion facts before outward action. Drift invalidates only the decisions whose premises it changes, while ambiguous authority blocks the affected action. <!-- declaration: takeover.verify -->

Pointer identity does not preserve evidence bytes. Remote coordinator state stays canonical for an SSH-only run, and desktop-local exclusion does not exclude the old writer. <!-- declaration: takeover.verify -->

An instruction has no liveness after its session terminates. The handoff records an actual wakeup or the literal state `handoff persisted; restart unscheduled`. <!-- declaration: limit.session-liveness -->
