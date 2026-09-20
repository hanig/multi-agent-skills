# Handoff contract for a mortal orchestrator

Status: PLAN. Produced by GPT-Astra 2026-09-20, asked what a handoff must
contain so a successor can take over without reconstructing judgement from
branches and receipts. Tracked under ARC-678 and ARC-681.

Its sharpest correction to the orchestrator's instinct, worth keeping at the
top: **do not rush pending decisions to completion before handing off. An
explicitly undecided unit is recoverable; a judgement existing only in
session memory is not.**

---

## FINALIZATION

**The six are adequate containers; no seventh issue is needed.** From the filed summaries:

- **ARC-679:** explicitly require coordinator-owned decision records, exact attempt/head/base/candidate and policy bindings, reproducible impact evidence, and independent non-author challenge under a fixed reviewer-selection policy. Enforce these at **merge eligibility**, not merely dispatch. Its title still advertises push discipline that moved elsewhere.
- **ARC-681 → ARC-679:** publishing the mandate requirement is insufficient; coordinator ingress must enforce mandate, policy and authorization references.
- **ARC-681:** add the operational handoff contract below. Transition tests may be organized here, but must be acceptance dependencies of the implementing issues—not a later documentation exercise.
- **Re-rank now:** the takeover/handoff slices of **ARC-678 and ARC-681 are operational P0**. The eight pending units need a successor, not necessarily completed adjudication before departure. Preservation and integration guardrails remain mandatory; do not enable exceptions before owner-approved policy changes.

## HANDOFF CONTENTS

Carry three separately labelled sections:

1. **Authority references:** authenticated owner mandate and durable approvals; anchored policy; authoritative coordinator host/path/run identity and state version. Coordinator state controls bindings/status; it does not make unsupported claims true. Hashes establish identity, not authorization.
2. **Decision ledger for all eight units:** unit/attempt/anchor; reviewed head/base/candidate; complete reviews and finding IDs; evidence locations; finding status and disposition; criterion-impact reasoning; independent challenge; authorizing clause/reference; residual obligations and invalidation conditions. Explicitly distinguish **unadjudicated, investigation pending, blocked, and durably adjudicated**. `READY_FOR_PR` alone means none of these decisions has been approved.
3. **Operational continuation:** pending actions/outbox IDs, known or uncertain remote effects, PR identities, claimed liveness, remaining budgets, next safe action per unit, blockers, and actual successor launch/wakeup arrangements.

Narrative, hypotheses and recommendations are context. Plans supply authority only where owner-approved and bound into the mandate. Include pointers to the plan, tickets and survey, without promoting their contents wholesale into authority.

## BEFORE HANDOFF

- **Record every judgement already made**, including its evidence and rationale. Incomplete reasoning must be recorded as provisional—not retroactively dressed as adjudication.
- Do not rush eight decisions to completion. **An explicitly undecided unit is recoverable; a judgement existing only in session memory is not.**
- Quiesce dispatch and outward effects; flush decision/action records. Reconcile ambiguous actions where possible; otherwise record **unknown**, with the required reconciliation step.
- Preserve work and evidence bytes outside disposable locations; capture a consistent state identity and verify pointers are readable.
- Arrange an actual desktop/owner-initiated launch and preferably obtain successor acknowledgement. If none exists, state **“handoff persisted; restart unscheduled.”** Do not promise self-resurrection.

## ON ARRIVAL

Before acting, the successor must:

- Verify repository/run/coordinator identity, original anchors, installed executable and mandatory skill/policy versions.
- Read original owner approvals and authoritative records—not just this handoff.
- Verify evidence availability and bindings; inspect criterion mappings and support for consequential dispositions. A different model must not treat persuasive predecessor prose as proof or reinterpret scope to make a finding disappear.
- Reconcile coordinator checks with current remote heads, target, PR/merge state, pending effects and actual worker/orchestrator liveness.
- Establish exclusive control. Reuse recorded decisions only where authorized and their premises remain valid; recheck affected evidence after drift.

Missing evidence, ambiguous authority or unresolved material objections block the affected action—not necessarily unrelated work.

## AUTHORIZATION TRANSFER

Authorization must already be explicitly durable, or the owner must issue a durable grant covering successors: **run, permitted actions/delegated decisions, limits, policy binding, duration and revocation conditions**. Pin a reference to the verifiable owner-originated source.

The predecessor can transfer references and operational custody, **not manufacture consent**. Verify grant validity and remaining budgets. Session-scoped or ambiguous permission requires owner clarification; SSH credentials, `autopilot`, or “the previous orchestrator said yes” confer none.

## SSH-ONLY STATE

- Keep remote coordinator state canonical; a desktop clone is not a resumed coordinator.
- Record verified SSH host identity, account, absolute state/evidence paths and access method—never secret credentials. Verify HOME and SSH access will survive the Slurm allocation; current reachability is insufficient.
- Verify availability of **bytes**, not merely digests. If the source will disappear, arrange separately authorized durable storage/migration; pointer capture cannot preserve data.
- Route all writers through one stable state-host exclusion mechanism. Desktop-local locks do not exclude the old orchestrator. Release/acquire control explicitly; never infer takeover safety from heartbeat expiry.
- On SSH/lock loss, stop outward effects and reconcile before retrying. Do not silently create replacement local state.

## GAPS IN hanig-portable-handoff

It must carry—or reference a durable, verified companion record containing:

- The decision ledger, rationale and explicit unadjudicated frontier.
- Owner-originated authorization references and successor applicability.
- Pending/ambiguous external actions and reconciliation obligations.
- Custody/exclusion status, actual restart mechanism, budgets and next safe actions.
- Semantic invalidation conditions and residual obligations.

Capture these consistently with the referenced coordinator version. **HANDOFF_CLEAN means artifact/code identity checks passed—not that judgements are sound, authorization exists, or takeover is safe.** Resolve `DRIFTED`, `ELSEWHERE` or `MALFORMED` for the affected resources before relying on them. The extension can remain pointer-only, but its referenced decision and authorization records must actually exist and remain reachable.