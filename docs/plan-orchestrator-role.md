# The orchestrator role: plan

Status: PLAN, not policy. Nothing here is enforced, and several parts contradict
how this project was actually run between 2026-09-16 and 2026-09-20.

## Why this exists

The repository documents four roles well: the coordinator (`hanig-swarm`), the
project front door (`hanig-project`), the review gate and its protocol, and the
contract for editing this codebase (`CLAUDE.md`).

It documents nothing about the ORCHESTRATOR: the agent that drives dispatch,
gate, adjudicate, merge, record, advance, drain and re-dispatch, and that must
decide what to do when a gate verdict and the project's goal disagree.

In the run that produced this document, that role was played by a Claude session
for four days, improvising. The repo owner's diagnosis: "the orchestrator should
also pass judgement on reviews. Sometimes if a wrong claim is found but is in the
end not relevant to the goal of the project, the PR should happen. What I'm
hearing is that this project lacks sufficient instructions for the orchestrator
itself."

## Provenance

The plan below is GPT-Astra's, produced 2026-09-20 from a brief containing this
repository's contract and front-door skill, plus a factual log of the run
including six orchestrator failures and the judgement calls that went both ways.
It is reproduced verbatim. Tracked as ARC-678 through ARC-683.

## Where it contradicts how this run was operated

Recorded because the disagreements are the useful part:

1. **Merging known breakage to preserve attempt binding.** I did this twice, on
   PR #11 and PR #20. Astra rejects it outright: repair and rejudge through a
   supported lifecycle, and allocate a fresh attempt if that is what it takes.
   Astra is right, and #20 went on to fail three unrelated pull requests for
   three days. Tracked as ARC-683.

2. **"Stop rather than open a pull request on a reproduced failure."** I put this
   in every dispatch prompt. It produced good behaviour and destroyed an entire
   unit's work when an agent obeyed it and then lost its worktree. Astra: stopping
   and destroying work are separate defects, and no policy should make a
   successful review a prerequisite for preserving unsuccessful work. Tracked as
   ARC-682.

3. **"A human must close the unit."** I filed ARC-678 on that premise. It is
   wrong: the orchestrator is the non-human actor already doing that work. The
   real gap is that the driving session is ephemeral and interactive.

---

## ROLE

**The orchestrator owns progression and bounded integration decisions—not evidence creation by assertion and not unit closure.** The missing role is real; missing prose is only part of the defect.

Plan an explicit authority table:

| Role | Authority |
|---|---|
| Human owner | Sets goals, acceptance criteria, exclusions, budgets, permissions and allowable risk. Approves outward actions under the existing approval rules. |
| Orchestrator | Drives the loop; commissions reviews; reproduces findings; applies the adjudication procedure; requests repairs; opens and merges eligible PRs within granted authority; records observations; advances and drains. |
| Coordinator | Holds authoritative state and bindings. `unit.py check`, with its existing judging components, determines unit status. No network capability or semantic relevance judge moves into `swarm.py`. |
| Review gate | Attempts to refute claims. Neither a pass nor a reviewer’s severity label decides closure. |
| Worker | Produces work and performs self-correction. Cannot authorize its own exception, merge eligibility or completion. |

Before dispatch, establish an **owner-approved run mandate**, pinned in coordinator state: goal and criterion IDs, exclusions, budgets, permissions, applicable policy versions and delegated decisions. This adds no configurable closure authority.

Within that mandate, the orchestrator decides routine sequencing, bounded retries, repair requests, finding dispositions and eligible merges without repeatedly asking.

It must stop and ask when:
- relevance cannot be established without interpreting or changing the owner’s goal;
- a decision exceeds approved scope, budget, risk or permissions;
- an outward action lacks its required approval;
- required evidence remains inconclusive after bounded investigation.

`swarm autopilot` retains its existing meaning; it does not waive integrity constraints or grant tool permissions. Fresh sessions inherit only explicitly durable authorization—not inferred consent from a predecessor’s narrative.

The orchestrator must never rewrite anchors, edit state to manufacture success, authorize candidate-defined verifiers, replace required evidence with judgment, or silently relax criteria.

## JUDGEMENT PROCEDURE

**Replace “REVIEW_FAIL means abandon” with “REVIEW_FAIL requires disposition before merge.”** Do not replace it with “the orchestrator decides what matters.”

### 1. Preserve before adjudicating

A failed review must lead to bounded repair or a durable blocked handoff. Recovery commits and clearly blocked draft PRs may preserve work; they do not establish readiness.

Stopping and destroying work are separate defects. No policy should make successful review a prerequisite for preserving unsuccessful work.

### 2. Pin the decision’s premises

Bind every adjudication to:
- plan/mandate and policy digests;
- unit, attempt and original pre-agent anchor;
- exact reviewed head, target-base commit and tested candidate merge;
- applicable criteria and declared outputs;
- review panel, complete gate result and individual finding IDs.

Eligibility policy comes from the anchored, authorized policy—not the candidate’s proposed replacement.

### 3. Establish what the finding actually says

For each finding, retain the precise claim, preconditions, reproducer, environment, commands, exit status and outputs. Compare baseline and candidate where relevant.

Classify it as:
- **Disproved:** evidence contradicts the stated finding.
- **Confirmed:** the stated failure occurs.
- **Unresolved:** reproduction or interpretation is inconclusive.

A passing named test alone does not disprove a finding. Failure to reproduce is not automatically disproof.

### 4. Apply a fixed disposition matrix

| Finding status | Permitted disposition |
|---|---|
| Disproved | Dismiss that finding, citing the contradictory evidence. Preserve the original review. |
| Confirmed; violates a hard invariant or required criterion | Repair; no relevance exception. |
| Confirmed; demonstrated outside required behavior and within preapproved deferral policy | Eligible for an explicit nonblocking disposition, subject to independent challenge below. |
| Unresolved, ambiguous scope, or outside delegated risk | Block the affected merge; investigate within budget, then ask the owner. |

For a confirmed finding, the orchestrator must answer:

1. Which goal, criterion, output and downstream consumer could it affect?
2. What evidence bounds that impact?
3. Would accepting it weaken a shared test, verifier, interface or later unit?
4. Which **pre-existing** policy permits leaving it unfixed?
5. What observable result would falsify the claim that it is irrelevant?

“Unrelated file,” “low severity,” “expensive to fix,” and “another PR will fix it” are not sufficient answers.

A nonblocking disposition needs independently produced impact evidence and a non-author challenge review of the rationale. Use a predetermined reviewer-selection/escalation policy, not panel shopping. Agreement is supporting evidence, not proof. Unresolved material objections escalate.

### 5. Define non-overridable classes

No relevance exception for:
- violations of the stated repository hard constraints;
- missing, fabricated or improperly bound evidence;
- unauthorized actions, privacy/security violations or destructive data loss;
- failure of declared acceptance criteria or required outputs;
- compromised isolation, anchors, closure authority or verifier authorization;
- unsound shared tests or guards that can contaminate subsequent decisions;
- failure of required checks on the actual candidate merge.

Stale CI observations may be superseded only through a **preauthorized equivalent-check procedure** on the exact candidate merge. This is not permission to bypass branch protection or rerun a failure until it turns green.

I reject preserving attempt binding as a reason to merge known breakage. Repair and rejudge through a supported lifecycle; if that requires a fresh attempt, allocate one. Never rewrite the anchor.

Likewise, an irrelevant defect may be deferrable; **a false delivered claim must be corrected or explicitly withdrawn**, not silently endorsed.

### 6. Record the decision without laundering the verdict

Add a coordinator-owned adjudication record containing:
- all bindings and evidence references above;
- finding status and disposition;
- criterion-by-criterion impact analysis;
- independent challenge and responses;
- policy clause authorizing acceptance;
- decision-maker/session and authorization reference;
- residual limitation, follow-up obligation where applicable, and invalidation conditions.

Evidence bytes must remain available, not merely their hashes. Agent-written files are submissions, never authority. Network observations remain **attested**, except where the existing authorized-verifier contract establishes otherwise.

Keep `REVIEW_FAIL` intact. Report separately that the candidate became eligible after recorded adjudication; never relabel it `REVIEW_PASS`.

Any head change invalidates head-bound eligibility. Target movement invalidates candidate-integration evidence and requires rechecking affected premises.

A future reader should be able to reconstruct the decision without the conversation. A good exception has a prior authorization, reproducible impact bounds and attempted falsification. A convenient exception has only a persuasive explanation.

## LOCATION

Create a sixth authored skill, **`hanig-orchestrate`**, rather than burying the role in the front door or coordinator documentation.

Divide ownership:

- **`CLAUDE.md`:** short mandatory bootstrap, role boundaries and explicit instruction to load orchestration policy for starting, resuming or driving a run.
- **New `SKILL.md`:** operating loop, takeover procedure, decision boundaries and stop conditions.
- **Review `PROTOCOL.md`:** canonical finding/disposition semantics and evidence requirements.
- **`docs/`:** rationale, worked decisions, state-transition specification and enforcement coverage.
- **Existing project/swarm skills:** explicit handoff to orchestration, including resume/status triggers.

Do not depend solely on automatic skill triggering. Session startup/resume tooling should supply the required skill and policy version, including on non-Claude hosts.

Update installation/dependency discovery and loaded-skill path resolution accordingly. Leave vendored skills verbatim.

Explicitly reconcile the existing “gate must pass” editing rule with adjudicated eligibility. Until that policy change is approved, an exception cannot silently overrule it.

## ENFORCE VS WRITE

| Mechanically enforce | Enforcement boundary |
|---|---|
| Required mandate, policy version, approval references and complete adjudication fields | Coordinator-state ingress and dispatch validation |
| Attempt/head/base binding; invalidation after changes | Existing judging components reached through `unit.py check` |
| No eligibility with unresolved required findings or missing required review | Offline protocol checks plus the operating-session merge path |
| Fixed closure evidence by kind | Existing coordinator judgment; adjudication is an additional prerequisite, never substitute closure evidence |
| Work preservation before controlled cleanup | Authored lifecycle adapter and destructive-cleanup integration tests |
| Actual executable/checkout provenance and canary qualification | Dispatch refusal |
| Budgets, retry limits and repeated-failure circuit breakers | State transitions and driver |
| Idempotent external-action reconciliation and truthful pending status | Session-side driver/outbox |
| Standard-library/Python compatibility, no network imports, no vendored edits | Tests, including mutation tests |

**Not mechanically established by those checks:** genuine relevance, completeness of an impact analysis, scientific validity, honest external attestation or the independence of minds behind two sessions. These require instructions, challenge and audit; schema validity does not certify their truth.

Nor can an offline coordinator establish the latest remote state. The operating session fetches/observes it; local checks enforce consistency with that observation.

Do not claim a prompt, hook or local wrapper prevents arbitrary worker pushes. Enforce the authorized merge path and available remote restrictions; retain the documented same-UID trust limits. This is not a new attribution system.

## CONTINUOUS OPERATION

Plan the operating instructions around **“act until quiescent,” not “announce the next action.”**

Each cycle should reconcile authoritative state, service ready reviews and PRs, adjudicate, merge eligible heads, record observations, advance, attempt outbox drain, dispatch newly runnable work and update the evidence-derived report. A blocked unit must not stall unrelated work; tracker unavailability must not block dispatch.

A session may yield only with:
- completion and the required report;
- a precise human decision or permission request;
- an external wait with a registered wakeup;
- exhausted execution budget with a durable handoff.

A status response is an observation of this loop, not its clock.

**Instructions cannot make a terminated session run.** Add a session-side supervisor using supported scheduling/events, bounded polling and backoff. Persist pending actions before outward effects; after ambiguous failures, reconcile remote reality before retrying.

Use same-node kernel exclusion, not heartbeat expiry or lock stealing. Detect overdue runnable work and resume/escalate it; do not equate a model’s promise with a scheduled action.

## EXPECTED FAILURE MODES

- **Criterion laundering:** a new model rewrites scope until a finding becomes “irrelevant.”
- **Policy self-authorization:** candidate code weakens review rules or authorizes its own verifier.
- **Reviewer shopping:** repeated panels eventually produce a convenient answer.
- **Prompt injection:** reviewer output, repository text or issue comments are treated as authority rather than evidence.
- **Decision reuse:** a disposition survives a changed head, base, environment or policy.
- **Split-brain orchestration:** two sessions merge, retry or acknowledge the same action.
- **Crash ambiguity:** a merge succeeds remotely, its receipt is lost, and restart performs the wrong recovery.
- **Exception accumulation:** individually bounded deferrals collectively invalidate a downstream assumption.
- **Premature cleanup:** unknown worker liveness is mistaken for permission to delete or release claims.
- **Installed-copy drift:** the checkout is current but the dispatched coordinator or skill is not.
- **Self-review by role switching:** the orchestrator authors a repair, then adjudicates its own claims.
- **Ceremonial compliance:** every field is populated, but evidence does not support the recorded conclusion.

## ISSUES

Ranked below. Locations for new files are proposed, not assertions about unseen implementation.

| Rank | Title | Change and location | Why now | First test |
|---|---|---|---|---|
| **1 / P0** | **ARC-679: Bind review adjudication to merge eligibility** | Add the owner-approved mandate/policy bindings, finding dispositions and invalidation rules in coordinator state, existing judging components and review `PROTOCOL.md`; reconcile `CLAUDE.md`. Keep networking session-side. | Neither unconditional veto nor discretionary override is acceptable. | A confirmed, demonstrably excluded finding can receive a nonblocking disposition; the identical record fails for a required criterion, changed head or candidate-authored policy. Worker files alone cannot authorize it. |
| **2 / P0** | **Preserve failed attempts before destructive cleanup** | Add durable recovery of tracked and untracked work outside disposable worktrees through authored lifecycle adapters; retain attempt bindings and evidence. Recovery material is not completion or `resume` authority. | Irrecoverable work loss makes adjudication impossible. | Produce uncommitted/untracked work, fail review, run real cleanup behavior and recover identical bytes. If cleanup cannot be intercepted or avoided reliably, refuse that launch configuration. |
| **3 / P0** | **Require sound candidate integration; support repair and rejudgment** | Specify and enforce candidate-merge checks, shared-guard validation and the supported amend/rejudge or fresh-attempt path in judging components and the session merge adapter. | Prevent another known-bad test or compatibility break from poisoning main. | Reject a known-failing candidate despite a follow-up promise; invalidate integration evidence when the target moves; show the repaired head requires fresh judgment. |
| **4 / P1** | **ARC-678: Drive READY_FOR_PR through merge, closure and restart** | Add the session-side durable driver, action reconciliation, wakeups and evidence-derived status/report path. No network imports enter the coordinator. | Removes human polling as the execution engine. | With fake connectors and one initial authorization, finish a DAG without status prompts; crash after remote merge but before recording, restart and reconcile without duplicate effects. |
| **5 / P1** | **ARC-680: Bind dispatch to observed remote and executable provenance; qualify fan-out** | External fetch/observation, offline consistency refusal, actual installed-code digests and an end-to-end orchestration canary keyed to relevant runtime/protocol changes. | Checkout freshness alone misses stale installed skills. | Fresh checkout plus stale executable refuses fan-out. A canary must reach merge recording and dependency advancement; tracker outage alone must not fail it. |
| **6 / P1** | **ARC-681: Publish and obligatorily load the orchestrator contract** | Add `hanig-orchestrate`, bootstrap/handoff references, installer dependencies, takeover checklist and canonical documentation ownership. Specify durable approval inheritance explicitly. | Fresh or different models otherwise improvise the role. | A fresh-session harness receives the required policy through both start and resume paths; missing policy or authorization produces a specific refusal. |
| **7 / P1** | **Test orchestrator transitions, not prose compliance** | Add adversarial transition/restart fixtures, native-session scenarios and enforcement coverage in `tests/` and `docs/audit-protocol-enforcement.md`. | New instructions can drift immediately unless refusals are exercised. | Mutation-disable each new guard and require a failing test. Run seeded semantic cases against fresh models; record those results as behavioral evidence, not universal guarantees. |

Implement all authored additions with the stated standard-library and Python compatibility constraints. Do not make policy enablement wait for polished documentation, but do require owner approval before enabling exceptions.

## VERDICT ON THE FOUR FILED ISSUES

- **ARC-678 — revise, not implement as written.** Its human-only premise is wrong. The missing component is an authorized, durable non-human driver across the network boundary.
- **ARC-679 — retain and broaden carefully.** Enforce adjudication and merge eligibility, not “any reproduced failure forbids creating a PR.” Preservation and blocked draft PRs must remain possible.
- **ARC-680 — retain and sharpen.** Verify observed remote freshness **and actual executed code**. Make the canary end-to-end and invalidate qualification when relevant premises change.
- **ARC-681 — revise and narrow its ownership.** Publish one canonical orchestration contract, with mandatory loading and explicit enforcement dependencies—not duplicated operating prose across five skills.

None should be closed as completed merely because instructions are written. ARC-678’s original framing should be retired; the underlying automation gap remains.