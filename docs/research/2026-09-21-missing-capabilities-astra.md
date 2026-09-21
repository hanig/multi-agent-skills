**1. Experience that becomes retrievable knowledge—and can subsequently be corrected.**

**Source:** [Graphiti](https://github.com/getzep/graphiti) stores raw episodes, extracted entities, and relationships with temporal validity and source provenance. Retrieval combines semantic, keyword, and graph search. Its [contradiction-handling implementation](https://raw.githubusercontent.com/getzep/graphiti/main/graphiti_core/utils/maintenance/edge_operations.py) invalidates superseded relationships while retaining history. This is a concrete correction mechanism, although identifying contradictions still depends on model judgments. [Voyager](https://voyager.minedojo.org/) implements a different kind of memory: successful executable skills indexed by descriptions, with relevant skills retrieved and composed for later tasks.

**For you:** Attempt 6 could retrieve what attempt 3 established: the unsuccessful approach, the observation that refuted it, the applicable environment, and a working procedure. This prevents repeated discovery costs that currently appear as unrelated attempts. It also creates something less obvious: **institutional knowledge of what does not work**, including abandoned hypotheses.

**Cost and fit:** Moderate for an evidence-linked episode store; substantial for automatic extraction, retrieval evaluation, and procedural skill promotion. Memories need scope, provenance, applicability conditions, and a correction path. A newer assertion cannot automatically outrank an older measurement. Your integration would need explicit supersession or quarantine for wrong memories, and revalidation when their environment changes; timestamps alone cannot establish truth. Retrieval can happen outside the network-free coordinator. Retrieved memories must remain advice, never authority to authorize a verifier or close a unit.

**Would you notice?** Immediately, though mostly as repeated effort rather than failures. Graphiti and Voyager establish mechanisms, not reliable autonomous knowledge maintenance across multi-day coding campaigns. I found no basis for claiming that problem solved.

**2. A progress model coupled to durable human intervention.**

**Source:** [Magentic-One’s orchestrator code](https://raw.githubusercontent.com/microsoft/autogen/main/python/packages/autogen_agentchat/src/autogen_agentchat/teams/_group_chat/_magentic_one/_magentic_one_orchestrator.py) explicitly asks whether progress is occurring and whether the team is looping, maintains a stall counter, and returns to planning after sufficient stalls. [Temporal](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/detecting-activity-failures.mdx) separately implements queue, execution, and heartbeat timeouts. Its [message-passing API](https://docs.temporal.io/develop/python/workflows/message-passing) supports validated updates to running workflows. [PagerDuty](https://support.pagerduty.com/main/docs/escalation-policies) implements assigned responsibility, acknowledgment, and escalation when nobody responds.

**For you:** The missing faculty is a durable answer to **“what is this attempt waiting for, and who or what must act next?”** A complete mechanism distinguishes:

- Working: observable activity or declared milestones continue.
- Blocked: a particular dependency, permission, or answer is required.
- Looping: activity continues but repeats without useful change.
- Finished but silent: execution ended and the ordinary checker should run.

Your permission detection and terminal watcher cover pieces. What remains absent is progress history, loop recognition, response deadlines, and an acknowledged intervention channel that survives the driving session. A steering message could carry an identity and record whether it was accepted and applied.

**Cost and fit:** Substantial adapter work; moderate escalation machinery. Model-based loop detection brings false positives and observation cost. Telemetry can trigger inspection or a bounded intervention, but cannot establish DONE. Activity deadlines must not become lock-stealing TTLs or permission to release claims. Network notifications can live in an external relay.

**Would you notice?** Now. A `NEEDS_HUMAN` state can still wait forever. Deterministic timeout and escalation machinery is established; reliable semantic distinction between hard thinking and unproductive looping is not.

**3. Metered execution with enforceable spending envelopes.**

**Source:** [LiteLLM’s budget implementation](https://docs.litellm.ai/docs/proxy/users) supports budgets associated with identities and sessions. For supported token-priced requests, it estimates maximum request cost, reserves that amount before forwarding, then reconciles against actual cost. Its documentation explicitly identifies limits: database requirements, additional fail-closed configuration, and batch workloads whose full cost cannot be reserved at submission.

**For you:** You have dispatch-time GPU commitments. The absent capability is accounting for **actual expenditure while an attempt is alive**, including worker inference, reviews, continuations, and auxiliary agents, with attribution to the responsible unit. Otherwise, a unit can remain within its dispatch allowance while accumulating an unobserved inference bill.

It also permits useful stopping rules: no further model call beyond the unit’s envelope; reserve enough for verification; stop expanding a search when its marginal allocation is exhausted.

**Cost and fit:** Moderate to substantial. Every billable call must pass through an enforceable boundary, and usage must be mapped to stable run/unit/attempt identities. An already-running CLI with unrestricted credentials cannot be capped merely by updating coordinator state. Provider metering and enforcement can remain outside the coordinator; the coordinator consumes trusted accounting observations. Stopping expenditure does not declare the work unsuccessful or successful.

**Would you notice?** Yes for API-billed work; less directly under subscriptions, where the scarce resource may be quota or elapsed time. **Budget enforcement is demonstrated. Reliably deciding whether arbitrary work is economically worth attempting is not.** I found no convincing general-purpose implementation of that stronger faculty.

**4. A portfolio of competing solutions, with selective continuation.**

**Source:** [SWE-Search / Moatless tree search](https://github.com/aorwall/moatless-tree-search) implements branching solution trajectories, value estimates, test execution, and a discriminator that selects among candidates. [Ray Tune’s schedulers](https://docs.ray.io/en/latest/tune/api/schedulers.html) implement resource allocation among trials: for example, ASHA terminates weaker trials using intermediate metrics. These are different implementations of searching among alternatives rather than exhausting one trajectory.

**For you:** One logical task could receive several independent approaches, with resources concentrated on promising candidates. This supports best-of selection, bounded n-of-k exploration, and races whose losers stop when an admissible winner emerges. The silently absorbed failure today is **path dependence**: the first plausible approach consumes the available time, and retries reproduce it.

**Cost and fit:** High unless used selectively. You need separate attempt identities, isolated candidates, a comparison contract, loser cancellation, and promotion of exactly one result. Candidates cannot share the logical output destination. Model preference can rank candidates for further work; it cannot replace your evidence requirements. A winner still passes the existing checker and merge path.

**Would you notice?** On difficult, ambiguous units—not routine changes. Sampling several nearly identical agents can multiply cost without adding useful diversity. SWE-Search supplies benchmark evidence; Ray supplies implemented trial scheduling. Neither establishes that racing general coding agents remains economical over unattended multi-day runs.

**5. Reusable subresults below the unit boundary.**

**Source:** [Bazel](https://bazel.build/remote/caching) keys action results by declared inputs, commands, and environment, and stores output content separately. That permits build and test work to be reused across executions. At another level, [Cursor’s cloud-agent engineering report](https://cursor.com/blog/cloud-agent-lessons) describes durable agent execution on Temporal, separated from VM and conversation storage, supporting recovery through infrastructure interruptions during runs lasting days or weeks.

**For you:** Two things become possible: reuse a still-valid result from another attempt, and recover completed internal steps when the worker infrastructure fails. The missing integration is automatic reuse with an explicit validity boundary. Preserving a patch or skipping an already-DONE unit does not provide that.

This prevents spending hours re-establishing unchanged facts or restarting a long sequence because its final operation failed. Incremental verification is especially valuable when one small change currently causes the same expensive checks to run again.

**Cost and fit:** Substantial. Cache validity needs content identities, toolchain/configuration identities, and a trustworthy producer. Selective testing needs dependency information; “the agent thinks these tests are enough” is a different, weaker mechanism.

There is a **direct contract interaction**: cached outputs cannot silently satisfy a predicate that requires fresh production in an exclusive attempt, its pinned pre-dispatch basis, and execution evidence. Reused material can be declared input; accepting cached verification as evidence requires an explicit policy extension. Same-attempt recovery and cross-attempt reuse also need different contracts.

**Would you notice?** Strongly for expensive builds and cluster work; less for short edits. Cursor provides an operator report of actual multi-day use, not an independent reliability audit.

**6. A persistent planning process that discovers and revises the work frontier.**

**Source:** [Cursor’s long-running coding experiments](https://cursor.com/blog/scaling-agents) use planners that continuously explore the codebase and create tasks, including recursive sub-planners. Workers execute those tasks, and another role determines whether another cycle is needed. Cursor reports week-scale experiments and a migration lasting over three weeks, while explicitly saying some results still required careful review.

**For you:** Your project skill can help author a plan, and the committee can reconsider it. The missing faculty is a maintained planning process that survives sessions and continually asks: **what work has become necessary, unnecessary, or impossible because of what we just learned?**

That prevents a perfectly functioning DAG from efficiently completing an obsolete plan. A particularly useful extension would record the premises behind tasks: “this approach is necessary because X.” If X is refuted, its descendants become candidates for cancellation or redesign. That premise-to-task invalidation is my proposed application, not something Cursor’s report establishes.

**Cost and fit:** High. New tasks require bounded scope and budget; plan revisions need identities, validation, and treatment of existing attempts. A network-capable planner can propose amendments to a network-free coordinator. Agent-produced plans must not become authority merely because they are valid JSON. Planner beliefs also cannot retroactively redefine completed evidence.

**Would you notice?** On exploratory projects lasting days. Much less on a stable, well-understood DAG. There is real multi-day operator evidence for continuous planning, but no general guarantee against task churn or planner drift.

**7. Executable rehearsal of failure paths before spending on a plan.**

**Source:** [Temporal’s testing framework](https://docs.temporal.io/develop/python/best-practices/testing-suite) provides mocked activities, time-skipping through long waits, and replay of recorded workflow histories against changed workflow code. Replay detects incompatibility with recorded execution history; it does not reproduce fresh model reasoning.

**For you:** You have validation, a dry-run path, and fault-oriented tests. What I did not find is a rehearsal facility for a **particular proposed plan**: advance a simulated clock, inject permission waits, scheduler delays, provider outages, exhausted budgets, and late completions, then inspect the resulting run.

This catches failures such as “one unanswered request holds the critical path indefinitely” before discovering them after a weekend. It also reveals whether the intended interventions and budget rules can actually reach a terminal state.

**Cost and fit:** Moderate to high: explicit time and external-observation interfaces, scenario definitions, and strict separation from real submission. A local simulator fits all three architectural constraints. Recorded histories used in rehearsal remain test inputs, not fresh execution evidence.

**Would you notice?** When introducing complicated plans or recovery behavior; probably not for small linear runs. **I found no credible general dry-run that predicts whether an LLM will solve a task or accurately predicts a live cluster queue.** The demonstrated capability is rehearsing control behavior under specified scenarios.

**8. Shared-incident handling—and monitoring whether the operator itself is alive.**

**Source:** [LiteLLM’s router](https://docs.litellm.ai/docs/routing) tracks deployment failures and temporarily removes unhealthy deployments from service. [Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/) groups related alerts and suppresses downstream notifications when a parent incident is active. [Healthchecks](https://healthchecks.io/docs/) alerts when expected scheduled-task signals stop arriving.

**For you:** These supply two missing operational perspectives:

- Several failing attempts may represent **one broken dependency**, not several task failures.
- No new failures may mean **the observer stopped running**, not that the run is healthy.

Your per-call retries, concurrency pools, checker watchdogs, and scheduler entry point do not constitute either faculty. Without them, a provider outage can consume many attempts independently, while a dead scheduling process leaves durable state looking merely unchanged.

**Cost and fit:** Low for an independently monitored scheduler signal; moderate for incident correlation and scoped admission control. Policies need to distinguish authentication failures, exhausted quota, provider trouble, and task-specific failure. An external monitor or provider adapter can handle networking. Incident status may pause new spending, but must not fabricate a unit verdict or establish that old writers are dead.

**Would you notice?** The independent observer matters even with one run. Elaborate incident grouping pays off mainly with concurrent units or multiple projects. These are documented operational mechanisms; they do not depend on speculative agent intelligence.

**9. Compensation: recording obligations to undo or clean up external effects.**

**Source:** [Temporal’s Saga implementation and examples](https://temporal.io/blog/compensating-actions-part-of-a-complete-breakfast-with-sagas) register compensating actions for completed or possibly completed operations. The examples deliberately register compensation before an operation, covering the case where it succeeds externally but its acknowledgment is lost. Temporal’s Java SDK supplies a Saga helper; the article shows explicit compensation lists for other languages.

**For you:** Isolation limits where an attempt writes; an outbox controls repeated tracker actions. Neither provides a general answer to **“what obligations remain after this attempt is abandoned?”**

A worker might create temporary storage, start a service, reserve an external resource, or open a provisional artifact before failing. The unit can finish administratively while these effects continue costing money or confusing later work. You already clean up worktrees and claims; the absent faculty is a declared lifecycle for arbitrary external effects.

**Cost and fit:** Moderate machinery, potentially high integration cost per tool. Every effect needs an identity, an authorized compensator, idempotency, and reconciliation when its outcome is unknown. Some actions cannot be undone; their compensation may only notify or create a corrective action.

Cleanup authorization belongs in trusted policy and coordinator state. Agents cannot nominate arbitrary cleanup commands and thereby authorize them. Network actions can use an external executor. A compensation receipt describes cleanup, not successful task completion.

**Would you notice?** Little if all work remains disposable local files plus ordinary batch jobs. Quickly once agents provision resources or perform multi-step external workflows.

**10. Versioned execution: changing the system while old runs remain valid.**

**Source:** [Temporal Worker Versioning](https://docs.temporal.io/worker-versioning) supports workflows pinned to a deployment version, gradual routing to newer versions, and tracking old versions until their workflows drain. [Anthropic’s production research-system report](https://www.anthropic.com/engineering/multi-agent-research-system) describes keeping old and new agent versions running simultaneously during deployment to avoid disrupting active agents.

**For you:** You bind important identities already, including plan content and verifier authorization. The absent capability is the lifecycle of the **running orchestration and harness implementation itself**: which version continues an old run, which changes may migrate it, and when an old version can safely disappear.

Without it, the replacement driving session may resume a days-old run using newly installed behavior. A changed prompt, adapter, or interpretation of state then appears as ordinary continuation. Failures become difficult to distinguish from model variability.

**Cost and fit:** Moderate to high: immutable release identities, retention of old code, explicit migration rules, and representative replay checks. This fits a network-free coordinator and strengthens evidence interpretation, provided version choices live in trusted state. Full provider-model immutability is not always available, so harness pinning cannot promise identical inference.

**Would you notice?** Whenever you improve this system while runs remain active. It matters much less if deployments occur only after every run drains. The deployment mechanisms are concrete; their existence does not establish that arbitrary agent-state migrations are safe.
