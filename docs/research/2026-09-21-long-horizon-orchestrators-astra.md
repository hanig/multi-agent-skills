Your no-TTL lock is avoiding the right failure.**

**External evidence.** A lease holder can pause, lose its lease, and resume after another owner has taken over. Checking expiry immediately before writing is insufficient because the pause can occur after the check. Fencing works when the destination rejects operations carrying an older monotonically increasing token. [Kleppmann’s distributed-locking analysis](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html)

**Repository evidence.** Your lock implementation describes precisely this family of failures in earlier designs. It also explicitly records the limits of same-host measurements and NFS lock recovery. [Lock implementation](/Users/hani/multi-agent-skills/skills/hanig-swarm/scripts/swarm.py:2127)

**Assessment.** Keep `flock`. It trades automatic takeover of a hung process for substantially simpler exclusion. Within your stated topology, that is a good trade.

The operational response to a suspected hang is bounded investigation and, when authorized, terminating the actual owner and confirming termination—not deleting its lock file or declaring it dead from elapsed time. An already-sent external request may still require reconciliation afterward.

You can detect overdue progress without introducing an ownership heartbeat. Monitoring timestamps are observations, not permission to take custody.

The same distinction applies to workers: cancellation requested is not cancellation established. No timeout should release a shared-output claim while scheduler liveness remains unknown.

**Where you are over-engineered.**

My strongest criticism concerns **choice 5’s application**, followed by the cost of applying **choices 2 and 6 at overly fine or overly coarse unit boundaries**. I would not weaken the evidence and authority boundaries.

| Choice | Assessment |
|---|---|
| 1. Isolation replaces attribution | Good simplification. Attempting stronger process attribution under a trusted-writer model would cost more than it returns. Keep the pre-dispatch basis. |
| 2. Fixed closure by kind | Worth keeping. But a tiny bookkeeping edit can incur disproportionate PR/review overhead. Improve unit sizing and combine tightly related changes; do not invent alternate closure authorities. |
| 3. Network-free coordinator | Worth keeping. The operational tax is real, but a durable connected adapter is cheaper than mixing tracker availability and credentials into judgment. |
| 4. Coordinator-state authority | Worth keeping. Your history contains repeated failures at this boundary. More cryptographic ceremony around same-UID files would not itself create a new security boundary. |
| 5. Adversarial multi-model review | Highest risk of diminishing returns. Breadth, repetition, and committees need measured marginal value, not an assumption that more scrutiny is always safer. |
| 6. Fresh attempts | Sound default, potentially expensive for large compute units. Use bounded units first; introduce validated checkpoint support for demonstrated expensive workloads. |
| 7. One coordinator, no TTL | Deliberate simplicity, not over-engineering. |
| 8. Evidence-derived reports | Worth keeping. Generating readable synthesis from evidence is useful; repeatedly reconstructing the same evidence manually is avoidable toil. |

**External counterexample.** Cursor’s research harness removed a centralized integrator after it became a bottleneck and explicitly accepted a small continuing error rate to increase throughput. It used separate worker copies and a single large host. The research project was not intended for external use. That demonstrates a coherent alternative objective, not that your correctness policy is wrong. [Cursor’s detailed account](https://cursor.com/blog/self-driving-codebases)

**Assessment.** You should borrow its willingness to measure coordination overhead, but not its willingness to leave the shared branch imperfect. This repository’s shared guards and authority machinery affect later decisions; your record already includes one knowingly unsound merge disrupting other PRs for days.

The more convincing over-engineering example is your own rejected protocol-hardening proposal: binding rounds too rigidly to a change identity locked honest work out, while structured threat-model fields could be satisfied ceremonially. Do not resurrect those mechanisms merely because workflow engines have more state. [Review protocol](/Users/hani/multi-agent-skills/skills/hanig-review-gate/PROTOCOL.md)

**The three-round limit is reasonable as a budget rule; the step-back trigger is the more valuable rule.**

The supplied day—four blocked PRs, zero contested merges, a caught command injection, and a caught unsound link guard—does not establish either success or failure of the policy. It establishes both significant protection and significant friction. I treat those counts and catches as **user-reported evidence**; I did not inspect all four complete review histories.

Your repository provides additional evidence in both directions:

- Repeated fixes to defects introduced by preceding fixes eventually required replacing the mechanism.
- A recorded escalation produced seven major findings; five were checked and none survived.
- The honest-run counterclaim caught a stricter guard rejecting harmless valid input.

These are unusually useful operational observations. They support adversarial review **with reproduction and scope control**, not unlimited panels. [Protocol and incidents](/Users/hani/multi-agent-skills/skills/hanig-review-gate/PROTOCOL.md)

**My recommendation:**

- Retain three substantive implementation rounds as the default ceiling for an unchanged design.
- Retain immediate step-back when a **reproduced** finding exposes a defect in the preceding repair.
- Give the committee a concrete question: which assumption is wrong, what simpler mechanism avoids the defect class, and what evidence would discriminate between alternatives?
- Require an explicit output: revised design, bounded investigation, or blocked decision. A committee that produces only more concerns has not resolved the escalation.
- Do not treat a timeout or malformed review as design evidence. Infrastructure retries still consume budget, but should be reported separately from substantive rounds. Changing round accounting requires an explicit policy decision; it is not permission to keep passing `--round 1`.
- Never require an empty findings list. Follow the mandate’s dispositions while preserving the original verdict and unresolved material objections.

I found no comparative study validating **three** as the optimal number of rounds or your exact committee trigger. Its justification is local experience and budget discipline.

Measure marginal value by reviewer and round: reproduced consequential defects, disproved findings, repair-induced regressions, latency, cost, and eventual accepted outcomes. Do not optimize for findings produced; that rewards reviewers for creating work. Nor should merge count alone be the objective: rejecting an injectable command path is valuable even if nothing merges that day.

For lower-risk work, test a smaller predetermined review route against historical cases and a shadow holdout panel before changing policy. Avoid selecting a friendlier reviewer after an inconvenient result.

**What “going wrong early” means, and the state of the art.**

There are three different detection problems.

**First: execution has stopped or cannot proceed.** Mature workflow engines handle this well through persisted states, timeouts, event delivery, and independent supervision. You already cover important pieces. The remaining work is making somebody reliably act on those observations.

**Second: execution is active but unproductive.** Progress ledgers, repeated-action signatures, queue aging, cost accumulation, and acceptance-milestone tracking can expose this early. Magentic’s bounded stall/replan mechanism is a concrete example. For you, objective observations should come first; an LLM can then inspect the evidence and suggest causes.

**Third: execution is confidently producing the wrong result.** This is much harder. More logs and a running process do not reveal an incorrect premise. Early consumer checks, representative end-to-end tests, and independently defined acceptance criteria are stronger signals than an agent’s assertion that it is progressing.

Recent research offers promising monitoring mechanisms but not a proven general solution for unattended days-long runs:

- **Automata from Agent Traces** constructs compact state models from action traces, then uses per-state behavior and cycling features to predict failures from partial trajectories. It reports held-out AUROC up to 0.94. Its limitations include dataset-specific activity extraction and limited cross-domain transfer evidence. This is an August 2026 preprint, not proof of reliable deployment in your setting. [Paper](https://arxiv.org/html/2608.23670v1)
- **Accurate Failure Prediction in Agents Does Not Imply Effective Failure Prevention** reports that a critic with strong offline discrimination could still reduce task success substantially when used to intervene. The relevant lesson is to evaluate the intervention’s net effect, not just the classifier. Its benchmark results should not be transferred numerically to your workloads. [Paper](https://arxiv.org/html/2602.03338v1)
- **Who&When** studies retrospective identification of the responsible agent and decisive failure step; **Who&When Pro** expands controlled failure-attribution evaluation. These are useful debugging research, but retrospective diagnosis is not advance warning. Their use of “attribution” concerns reasoning failures, not proof of filesystem authorship. [Original](https://arxiv.org/abs/2505.00212), [successor](https://arxiv.org/abs/2607.09996)

**My proposed operating policy** is therefore conservative about intervention, not passive about observation:

1. Collect low-cost mechanical signals continuously.
2. On an anomaly, gather fresh evidence and identify the affected work.
3. Invoke semantic diagnosis only when the mechanical evidence does not explain it.
4. Prefer pausing additional expenditure or fan-out over interrupting healthy live work.
5. Retain the existing closure path regardless of the monitor’s confidence.

Set thresholds from actual workload distributions. Before enough data exists, explicit owner-approved time and spending limits are more honest than a supposedly learned “normal.”

Evaluate a monitor using time-to-detection, useful advance warning, false alarms per run-day, investigation cost, wasted spending avoided, and successful runs harmed by intervention. Replay historical traces first, then run in shadow mode. An accurate warning system that constantly disrupts recovery is worse than a simpler one.

**Long-horizon evaluation needs to test your definition of long horizon.**

METR’s time-horizon work measures task difficulty using how long humans take, at a specified agent success probability. That is not the same as keeping a distributed operational process alive for days through session replacement. Both dimensions matter; neither certifies the other. [METR paper](https://arxiv.org/abs/2503.14499)

TheAgentCompany evaluates realistic workplace actions, while the multi-agent failure taxonomy identifies specification, coordination, verification, and termination failures. These support testing complete workflows rather than isolated answers; they do not establish that a particular framework survives your failure model. [TheAgentCompany](https://arxiv.org/abs/2412.14161), [multi-agent failure study](https://arxiv.org/html/2503.13657v3)

For repeated reliability, τ-bench’s `pass^k` asks whether repeated trials all succeed, rather than whether one lucky trial succeeds. That is closer to your operational concern. [τ-bench](https://arxiv.org/abs/2406.12045)

As an illustrative calculation—not an empirical model of your system—100 independent critical transitions each succeeding with probability 0.99 yield only about 36.6% probability that all succeed. Real failures are correlated and many are recoverable, so multiplying benchmark accuracies would be misleading. The useful lesson is to measure **recovery and containment**, not demand perfection from every LLM step.

Your acceptance workload should include replacement sessions, ambiguous external outcomes, delayed evidence, ordinary provider failures, policy revocation, and a shared defect affecting several units. Success means completing within the original authorization and budget, with correct evidence and explicit unresolved work—not merely eventually obtaining a green run after intervention.

**The known merge-result gap remains important, but the external mechanism is straightforward.**

GitHub’s merge queue tests a PR against the current target plus preceding queued changes. GitLab’s merged-results pipeline tests a temporary combination of source and target; conflicts can cause fallback to an ordinary MR pipeline, so the exact tested commit still matters. [GitHub merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue), [GitLab merged results](https://docs.gitlab.com/ci/pipelines/merged_results_pipelines/)

Keep separate identities for the judged PR head, tested integration candidate, and observed merge. A queue need not change your source head or closure rule.

I did not establish that this public repository has access to GitHub’s native queue; its documented availability depends on repository ownership and plan. If unavailable, serializing the authorized merge path and validating the exact candidate is a narrower alternative, provided target movement is prevented or detected before acceptance. A local read-then-merge sequence alone does not close that race.

Visibility correction (2026-09-24): this repository is public by owner decision. The credential and publication rules in [CLAUDE.md](../../CLAUDE.md) apply.

**What the evidence does not establish.**

I found no apples-to-apples comparison of your eight choices against another system on unattended, multi-day code-and-compute runs with driver replacement. Public agent-harness accounts are informative engineering reports, not controlled comparisons. Workflow documentation establishes supported mechanisms, not your application’s semantic correctness.

I also lack complete review-cost and adjudication records for the contested day, so I cannot calculate the return on its fourth blocked PR or the marginal value of its last panel.

The defensible investment order is still clear: make the driving role recoverable, detect and contain unproductive work, and qualify the deployed path before scaling it. Preserve the evidence boundaries. Put the strongest pressure for simplification on repeated review and operational ceremony whose benefit you have not measured.
