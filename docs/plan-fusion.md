# Plan: fuse coordination with adjudication

> **STATUS: v1 REJECTED. Plan review 2026-08-28, deepseek-v4-pro + luna,
> quorum 2, both verdicts in. 6 MAJOR findings, 2 found independently by both; 1 later voided by an architectural correction from Hani, so 5 stand.
> Author gpt-5.6-sol excluded from the panel.**
>
> The direction is endorsed: one system at the level of authority and state,
> two engines underneath, an admission layer as the sole authority on "done",
> and a sealed work order as the minimum unit. What is rejected is that v1
> ENFORCES any of it.
>
> **1. The seal is not sealed (both members, independently).** The work order
> has no integrity protection, so it can be mutated after sealing. deepseek's
> case: an operator edits the `verifier` field to a lenient one, dispatch
> proceeds, and the admission controller admits using the wrong verifier. luna
> reached the same conclusion from immutability. A "sealed" object that nothing
> seals is the self-assertion this whole system exists to refuse, relocated
> into the fusion layer.
>
> **2. The existing Paseo path bypasses admission entirely (luna).** An agent
> goes idle and writes `{"validation":{"passed":true}}`; a consumer runs
> `bus await`, accepts the truthy status, and launches dependent work. No
> sealed work order, no receipt, no admission. So this is two things bolted
> together after all: removing the admission layer would not break the system,
> because the old path still works.
>
> **3. ~~The verifier cannot run where the artifacts are~~ (deepseek) --
> VOID, on Hani's correction 2026-08-28.** The finding assumed a central
> controller on the Mac reaching out to clusters. That is not the architecture:
>
> > "The controller will be on each machine. I'm not planning to control
> > everything from a Mac... projects on each server will be controlled from
> > that server. I'm just making a uniform tooling here."
>
> So the verifier ALWAYS runs where the artifacts are, because the controller
> is local to them. There is no remote-verification problem, no cross-machine
> artifact transport, and no credential path to design. "Distributed admission"
> is not a hard problem here; it is the default, and the unit of deployment is
> the machine.
>
> The assumption was MINE, not sol's and not deepseek's: I wrote the problem
> statement that framed this as a fleet dispatched from one place, and both
> reasoned correctly from what I gave them. Ask what the topology is before
> designing for one.
>
> **What this changes.** The install story carries weight the design does not:
> identical tooling must land on the Mac, lambda, andromeda and chimera and
> behave identically, which is exactly why skills install individually
> (`install.sh --only NAME`) with no sibling imports. Peer messaging becomes
> notification between equals, never control. Nothing in the fusion needs to
> move an artifact.
>
> **4. Nothing requires an independent contract authorizer (luna).** The prose
> calls for a separate adjudicator when a coordinator is an agent; the FIELDS
> reject only `issuer == executor`. So a coordinating agent picks weak
> thresholds, names itself issuer and a worker executor, and seals. The
> self-assertion one level up, which is exactly the question this plan was
> asked to answer.
>
> **5. ADMITTED does not freeze the artifacts (luna).** The receipt binds hash
> H; a later retry, transfer or cleanup makes it H2; downstream sees ADMITTED
> and consumes H2 against a receipt for H.
>
> **6. Human review is a dead end (luna).** `AWAITING_HUMAN_REVIEW` has no
> transition that produces ADMITTED, so an approved result leaves downstream
> work blocked "or encourages an out-of-band bypass".
>
> **What v2 must settle before any code.** Findings 1, 4 and 5 are the same
> question wearing three hats: what makes a claim in this system evidence
> rather than an assertion, when the threat model says contract files are
> trusted input? That trust is defensible for a human author and NOT
> defensible for an agent author, and v1 did not notice the difference.
> Finding 3 is void: admission is per-machine by design, so the verifier is
> always co-located with the artifacts it judges.
>
> Finding 2 is the cheapest and the most urgent: as long as `bus await` accepts
> an agent-authored status, none of the rest matters.

---

## Verdict

The fusion should be **one system at the level of authority and state**, with two deliberately separate engines underneath:

- Paseo coordinates execution.
- Domain verifiers adjudicate evidence.
- A new admission layer is the only component allowed to turn either into “done.”

If Paseo merely launches an agent and later calls `contract.py check`, the result is two things bolted together. The fusion becomes real only when the shared state machine makes this invariant unavoidable:

> No work item reaches an accepted terminal state unless a fresh verifier receipt, bound to that exact work item, contract, attempt, and artifacts, passes its declared admission policy.

Do not merge the repositories wholesale. Fuse their protocol and authority boundary.

## Assumptions

- Agents are honest-but-fallible, not malicious processes actively forging files or exploiting the host.
- Contract files remain trusted input, matching the current verifier threat model. A malicious agent with write access to both contracts and receipts cannot be stopped by JSON conventions; that would require OS isolation, signatures, or an external authority.
- “Independent” means separation of roles and execution context, not necessarily a different human.
- Scientific thresholds often require project or human authority. A general agent may instantiate approved parameters but must not invent post hoc biological success criteria.
- Paseo runs the control plane on the Mac. Verification may execute on the cluster where the data lives.
- Existing Slurm, Snakemake, and Nextflow remain the execution schedulers. Paseo schedules agents, not GPUs.
- Ceremony must scale with risk. A code-formatting task and a Tahoe-100M training run should use the same envelope protocol but different contract policies.

## Root cause: why three levels deep

### Why 1: Why can coordinated work be called done without proof?

Because the consumer accepts the wrong object.

Paseo reports that an agent became idle, completed a turn, or emitted a status file. `bus await` improves this substantially: it checks launch identity, branch, base commit, cleanliness, and optionally truthy JSON fields. But a truthy `validation.passed` remains a claim written by the work process. It is not equivalent to a verifier independently reaching a bound verdict.

The coordination repo therefore already proves some useful facts—it is not evidence-free—but it cannot adjudicate scientific or artifact completion.

### Why 2: Why is the wrong object authoritative?

Because the repositories have no common acceptance object.

The coordination side reasons in terms of agents, workspaces, prompts, commits, statuses, and notifications. The adjudication side reasons in terms of contract instances, criteria digests, attempts, artifacts, and receipts. Today a human or coordinating agent informally translates between those worlds.

That informal translation is the hole. There is no machine-enforced statement that:

- Paseo agent `A`
- executing attempt `B`
- under contract instance `C`
- produced artifacts `D`
- that verifier `V` checked against criteria digest `E`.

The existing receipt-binding work correctly prevents stale evidence from attaching to a new contract. The same binding is missing across the coordination boundary.

### Why 3: Why was that translation left informal?

Because each half was designed around a different failure:

- Paseo minimizes coordination failure: wrong workspace, wrong provider, lost notifications, idle workers, unbounded loops.
- The verifier family minimizes epistemic failure: stale artifacts, missing evidence, retrospective criteria, unbound attempts, silently weakened predicates.

Neither owns the final question: **who has authority to admit a result into downstream work?**

The deepest root cause is therefore not a missing dispatcher or another verifier. It is **missing admission control**. “Done” currently conflates four separate facts:

1. The agent stopped.
2. An artifact appeared.
3. The artifact satisfied predeclared criteria.
4. The result is scientifically acceptable for downstream use.

Those must remain distinct states.

There is a symmetric bypass risk on the strict side. If every small task requires bespoke criteria, two plan reviewers, a fleet verifier, and human approval, users will route around the system. The corresponding three-level cause is: excess ceremony → contracts are bespoke → the system lacks reusable project-approved templates and risk classes. The answer is not weaker receipts; it is cheap contract instantiation for routine work.

## 1. Minimum unit: the sealed work order

The minimum unit is not an agent and not a dispatcher. It is a **sealed work order**.

An agent is an interchangeable executor. A dispatcher is transport. Neither is the durable object whose completion is being asserted.

A work order should consist of:

```text
.agent-runs/<work-id>/
  work-order.json       immutable after sealing
  contract/...          domain contract, or a content-bound reference to it
  events.jsonl          append-only dispatch, binding, retry, and lifecycle events
  attempts.jsonl        executor and scheduler attempts
  receipts/...          verifier-produced receipts
```

`work-order.json` needs at least:

- `work_id`: random instance identity, not content-derived.
- `issuer`: role and session/person identity.
- `contract_type`: workflow, training, result, code change, or another registered type.
- `contract_id` and `criteria_digest`.
- Exact executor prompt or command digest.
- Workspace, repository, base commit, cluster, and declared write scope.
- Verifier command/profile and the receipt schema expected from it.
- Acceptance policy: which receipt states admit the work.
- Retry, timeout, and human-review policy.
- Sealing record: when criteria became immutable and what approved them.

Post-launch events bind the work order to:

- Paseo agent and workspace IDs.
- Provider/model settings as observed, not merely requested.
- Slurm job/array IDs where applicable.
- Attempt IDs.
- Artifact identities.

The state is derived from these files rather than trusted as a mutable agent-authored field:

```text
DRAFT
  → SEALED
  → DISPATCHED
  → EXECUTION_SETTLED
  → VERIFIED | REJECTED | INCOMPLETE_EVIDENCE
  → ADMITTED | AWAITING_HUMAN_REVIEW
```

Only `ADMITTED` means done for downstream automation. `EXECUTION_SETTLED` is explicitly not completion.

## 2. Who declares the contract?

The rule should be:

> Criteria must be authorized upstream of the executor and sealed before that executor starts.

There are three legitimate declaration paths.

### Project-authorized template

The strongest practical default for genomics work.

A human or project maintainer checks a reusable contract template into the project: expected sample manifest, reference build, outputs, schemas, QC thresholds, convergence policy, and required review class. A coordinating agent may fill declared parameters such as sample set, output directory, seed, or allocation. It may not alter predicates or thresholds.

This removes most ceremony from routine runs.

### Independent contract author

For novel work, the parent coordinator may draft the contract, but the worker that will satisfy it may not author or modify it. If the coordinator is an agent, a separate plan adjudicator must approve the criteria digest before sealing.

The existing plan-review gate is relevant, but its output needs a durable receipt bound to the exact criteria digest. The repo currently acknowledges that this binding is not enforced in [PROTOCOL.md](/Users/hani/multi-agent-skills/skills/hanig-review-gate/PROTOCOL.md:1). The previously rejected per-change round ledger should not be revived wholesale; contract authorization needs a smaller receipt that says only, “these criteria were reviewed before dispatch.”

### Human scientific authority

For claims such as biological validity, acceptable batch effects, model selection, or readiness for publication, an agent cannot manufacture the standard. The work order must either reference a previously approved project policy or stop at `AWAITING_HUMAN_REVIEW`.

A retrospective contract remains useful for audit but cannot authorize `ADMITTED`.

### Enforcement

The dispatcher—not the prompt—enforces this:

- It refuses to launch an execution-role agent unless the work order is sealed.
- It refuses a seal whose issuer is the designated executor.
- Agent workspaces receive the sealed contract read-only where practical.
- Any change to the criteria digest invalidates the seal and requires a new work-order instance.
- An executor may append evidence and observed bindings but cannot replace criteria.
- The admission controller reruns the declared verifier; it does not trust a receipt merely because one exists.

This prevents accidental self-assertion. It is not tamper-proof against a malicious same-user process; that limitation must remain explicit.

## 3. What a real three-cluster genomics fleet still needs

Neither repository currently supplies the full cross-host control loop.

- **Durable identity across layers.** One work ID must connect the Paseo agent, remote checkout, Slurm job and array elements, training attempt, artifacts, verifier receipt, and handoff.

- **Remote verifier execution.** Large artifacts should be checked on lambda, andromeda, or chimera. Pulling data back to the Mac is frequently impossible and sometimes scientifically wrong because it changes the environment being certified.

- **Connection-loss recovery.** The control plane must distinguish an unreachable cluster from a failed job and from incomplete evidence. A Mac sleep, SSH interruption, or accounting delay cannot erase ownership of a running job.

- **Idempotent dispatch and reconciliation.** After restart, the controller must discover whether it already launched the agent or submitted the Slurm job before attempting either again.

- **Array-aware and DAG-aware receipts.** A 10,000-sample array needs per-element outcomes plus an aggregation policy. “9,997 passed” is neither total failure nor scientific pass unless the contract declared the permitted missingness.

- **Dataset and reference identity.** Sample sheets, genome assemblies, annotations, tokenizer/model versions, train/validation splits, and immutable dataset manifests need first-class identities using the existing identity ladder.

- **Storage locality and transfer provenance.** A file copied between clusters becomes a new observed artifact. The system needs source identity, destination identity, transfer verification, and an explicit statement of whether byte identity or semantic equivalence was checked.

- **Resource and retry semantics.** Preemption, OOM, wall-clock exhaustion, requeue, partial checkpoints, and resumption must be attempts under one work order—not new work silently replacing old work.

- **Permission boundaries and secrets.** Worker prompts and receipts must not contain credentials. Write scopes need enforcement or at least preflight/reconciliation, not only prose.

- **Scientific review routing.** Mechanical verification, adversarial code review, and human scientific acceptance are distinct receipt types. Projects need policies specifying which combination is required.

- **Downstream dependency gating.** A dependent task may consume only `ADMITTED` upstream artifacts, not merely paths produced by settled jobs.

The portable handoff machinery is valuable here, but it should carry the work-order identity and current admission state rather than becoming a second source of truth.

## 4. What should not be fused

Several separations are essential.

- **Do not make the message bus an evidence store.** Bus messages and completion notifications remain untrusted hints. They may point to receipts but never substitute for them.

- **Do not merge agent lifecycle with result state.** `idle`, `completed`, and `notifyOnFinish` remain coordination facts only.

- **Do not treat a Git commit as a universal artifact receipt.** It is useful evidence for code work, irrelevant or insufficient for most cluster runs.

- **Do not turn `paseo-loop` verifier prompts into mechanical proof.** A verifier agent provides judgment evidence. Objective checks still require domain verifiers.

- **Do not combine design committees with refutation panels.** A committee may propose a plan. Its members must not approve their own plan. The review gate’s “reviewers refute; they do not design” boundary is correct.

- **Do not combine advisor output with gating.** Advisors remain advisory.

- **Do not make model rankings part of correctness.** Model selection affects cost and capability, not whether a receipt is admissible.

- **Do not force one universal domain contract.** Workflow, training, results, code review, and human scientific acceptance need different criteria and states. Share the envelope, binding rules, and admission protocol—not every field.

- **Do not introduce a Paseo dependency into the verifiers.** The stdlib verifiers must remain usable on isolated login nodes. Paseo belongs above them.

- **Do not let skills import siblings.** Integration should occur through versioned JSON and subprocess exit/receipt protocols. Any shared parser or validator copied among independently installable skills must remain byte-identical and symmetry-tested.

- **Do not claim cryptographic or scientific proof.** The system proves bounded statements under a declared threat model. Review-model agreement is evidence, not proof; structural validation is not biological correctness.

The key correction to the initial gap statement is: the coordination repo already verifies useful launch and Git facts, and the adjudication repo does not prove arbitrary truth. The missing capability is trustworthy composition of their bounded claims.

## PLAN

1. **Define and test the sealed work-order protocol.**

   Acceptance criteria:

   - A versioned `work-order.json` schema identifies the work instance, issuer, executor role, domain contract ID/digest, execution scope, verifier, and admission policy.
   - Criteria are sealed before any dispatch event can be recorded.
   - Changing any criterion, verifier, acceptance state, command/prompt, or declared scope invalidates the seal.
   - The designated executor cannot also be the sole contract authorizer.
   - Retrospective contracts can be recorded but cannot reach `ADMITTED`.
   - Existing workflow, training, result, and review receipts can be represented without collapsing their domain-specific states.
   - Fixtures prove that identical criteria in a new work-order instance cannot reuse an old receipt.
   - The protocol is stdlib-compatible and requires no sibling-skill import.

2. **Build receipt-based admission independently of Paseo.**

   Acceptance criteria:

   - Given a sealed work order, the admission command runs the declared verifier and derives state from its fresh output.
   - It accepts only the explicitly configured success state and exit code; arbitrary truthy JSON cannot pass.
   - The receipt must match the work ID, contract ID, criteria digest, attempt ID, and current artifact identities required by that contract type.
   - Missing, malformed, stale, mismatched, unavailable, partial, or retrospectively produced evidence never admits the work.
   - Mutating an artifact after a passing check causes the next admission evaluation to refuse it.
   - Mechanical success requiring human scientific review yields `AWAITING_HUMAN_REVIEW`, not `ADMITTED`.
   - Tests include all three existing artifact-contract tools and assert the shared binding/refusal behavior without requiring identical domain states.

3. **Add the Paseo dispatch-and-reconcile adapter, starting with one vertical slice.**

   First slice: one Paseo worker submits or monitors one Slurm workflow contract and returns a workflow receipt.

   Acceptance criteria:

   - Dispatch refuses an unsealed or unauthorized work order.
   - The recorded launch binding matches the observed Paseo agent, workspace, cwd, provider/model settings, base revision, and declared write scope.
   - Agent `idle`, a clean commit, a status-file claim, and Slurm `COMPLETED` are each independently demonstrated insufficient for admission.
   - On lifecycle settlement, the adapter invokes the admission command; only `ADMITTED` unlocks downstream work.
   - Retries and Slurm requeues create new attempts under the same work ID and cannot reuse prior-attempt receipts.
   - Restarting the controller reconciles existing Paseo and Slurm identities without duplicate launch or submission.
   - Cluster unreachability produces `INCOMPLETE_EVIDENCE`, preserves the work binding, and can be retried safely.
   - An end-to-end fixture proves both paths: `idle + commit + no valid receipt` is refused; a fresh, correctly bound verifier receipt is admitted.
