[User] Plan a system, from a clean start. You have fresh context deliberately: the
previous planning agents were steeped in a framing that turned out to be the
wrong goal.

## The goal, stated plainly by the person whose project this is

> "I really want (b)... I want what he has... which is the ability to coordinate
> a swarm of agents to build projects autonomously more or less"

Coordination of an autonomous agent swarm that builds projects. Verification is
ONE PREDICATE AMONG SEVERAL, not the point of the system.

## The mistake this replaces, because it explains what to be careful of

Hani handed me Shreshth's repo and said "I wanna make it my own". I read that as
"extract the artifact-contract idea and discard the coordination machinery",
recorded my own narrowing as "the settled premise of the repo", and spent days
building evidence-adjudication tools: three CLI verifiers, 603 tests. Good
engineering aimed at the wrong target. He wanted the swarm.

Do not repeat that. If this plan drifts toward "a better verifier", say so.

## Who and what for

Hani Goodarzi: Core Investigator at Arc Institute, Associate Professor at UCSF,
running a dual wet-lab/computational group of ~18 people across both. Three HPC
clusters (lambda, andromeda, chimera; Slurm). Projects: Evo 2 (40B DNA
foundation model), Mach-1 RNA foundation models, CodonFM, Tahoe-100M (largest
single-cell drug perturbation atlas), scBaseCount, STATE. Python, PyTorch,
scanpy/scverse, snakemake, Nextflow.

The work is long-running GPU jobs, data pipelines, model training, and analysis
producing figures and tables. Not primarily web-app code changes.

**Topology, stated by Hani:** the controller runs on EACH machine; projects on a
server are controlled from that server; this is uniform tooling per machine, NOT
a central orchestrator.

## What already exists and works

**Shreshth's repo, `~/paseo-multi-agent-skills`, INSTALLED and live.** Eleven
skills symlinked into `~/.claude/skills`. Paseo 0.6.1 is running (daemon on
127.0.0.1:6767); providers claude, codex, opencode, pi all available. Read:
`skills/paseo` (workspaces, agents, schedules, heartbeats), `paseo-committee`
(two contrasting agents plan, then review their own plan), `paseo-advisor`,
`paseo-loop` (worker/verifier cycle, `--max-iterations`), `paseo-handoff`,
`agent-bus` + `bin/bus` (1050 lines: peer messaging, notification, model
routing), `start-a-sprint` (coordinator plus bounded workers per ticket, one
disjoint worktree each), `models.json`.

**Our repo, `~/multi-agent-skills`, 603 tests.** `contract.py` (Slurm outputs),
`traincontract.py` (training runs), `result.py` (figures/tables/exports),
`review.py` + `PROTOCOL.md` (an adversarial review gate whose rules are enforced
in argparse), plus symmetry and conformance suites.

Its genuinely expensive asset is SLURM DOMAIN KNOWLEDGE bought with real bugs:
`sacct` row ownership under job-id reuse; exit code `0:0` on a CANCELLED job
must not count as success; `End` arrives as the literal string `Unknown`;
terminal states contain spaces (`CANCELLED by 10025`); a timezone bug made one
instant read as three epochs nine hours apart. Validated against real Slurm.

## The insight that should organise this plan

**Isolation beats attribution.** Three plan rounds died on trying to prove
"this agent produced this artifact". Two reviewers independently showed it is
unreachable: observation across a window shows an artifact CHANGED, never which
process changed it, and on a filesystem shared by ~18 people a concurrent writer
is ordinary. No window length fixes it.

**Shreshth never had that problem.** Paseo gives each agent its own git
worktree -- "one bounded, disjoint worker task and worktree per implementation
worker". His cheap `--base` predicate is conclusive BECAUSE nothing else writes
that tree. He did not solve attribution; an exclusive namespace made it
unnecessary.

So: isolation is the load-bearing primitive, and cheap predicates over an
isolated namespace beat expensive predicates over a shared one.

## The hard question, which is why you are being asked rather than told

A git worktree isolates code. **It does not isolate a Slurm job's output
directory, a shared scratch filesystem, a GPU, a conda environment, or a dataset
on shared storage.** So what is the unit of isolation for an autonomous swarm
doing genomics and ML on three shared clusters? Answer that concretely, because
everything else follows from it.

## What I want back

Root cause analysis first. State assumptions. Ask why three levels deep.

1. **What is the minimum viable autonomous swarm here?** Concretely: what
   dispatches work, what bounds it, what decides a unit is done, what stops a
   runaway, and what a human sees. Smallest thing that is genuinely useful.
2. **The unit of isolation** for cluster work, per the hard question above.
3. **What to take from his repo unchanged**, what to adapt, and what does not
   transfer because his domain is code changes and this one is data and models.
4. **What of our 603-test verifier work survives as a PREDICATE** inside this,
   and what should be deleted outright. Be willing to say most of it.
5. **What autonomy actually requires that neither repo has today.** He
   coordinates agents that edit code in worktrees. Building a genomics project
   autonomously means multi-day GPU jobs, data dependencies, cost, and failure
   modes no worktree isolates.
6. **The first three things to build**, ranked by value per line, each with
   acceptance criteria checkable by someone who did not write them.

Constraints: Paseo is an accepted dependency. Python 3.8+ stdlib only for
anything installed on a cluster login node. Skills install individually
(`install.sh --only NAME`) so a skill CANNOT import from a sibling; shared code
is copied byte-identical. Per-machine deployment. Agents honest-but-fallible,
not malicious. A system that blocks or slows legitimate scientific work gets
switched off, after which it prevents nothing. Seven rules have been retired in
this repo as unfixable rather than shipped weak, so "leave it out" is respected.

End with a section headed PLAN: numbered steps and checkable acceptance
criteria. You will later review the implementation against your own criteria.

This is analysis only. Do NOT edit, create, or delete any files. Do NOT write code.
Use only Read/Grep/Glob; do NOT use Bash, it will hang awaiting approval.
I’ll use the `paseo` skill because this plan depends on Paseo’s actual operating model. I’ll stay read-only, inspect the two repositories with search/list operations only, and keep the analysis centered on swarm coordination; if the design starts turning into “a better verifier,” I’ll call that drift out explicitly.
[Shell] rg --files /Users/hani/.agents/skills/paseo /Users/hani/multi-agent-skills /Users/hani/paseo-multi-agent-skills
[Shell] rg -n "''^" "/Users/hani/.agents/skills/paseo/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/README.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/paseo/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/paseo-committee/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/paseo-loop/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/start-a-sprint/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/models.json"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/agent-bus/SKILL.md"
---

The first inspection confirms the key boundary: Paseo supervises agent processes and git workspaces, while the bus’s “artifact ready” logic is explicitly commit/status-file oriented. I’m now separating those reusable control-plane ideas from cluster execution, where a Slurm allocation and namespaced run directory—not an agent process or worktree—must become the durable unit.
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/paseo-committee/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/paseo-loop/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/paseo-advisor/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/paseo-handoff/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/paseo-multi-agent-skills/skills/start-a-sprint/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/multi-agent-skills/README.md"
[Shell] rg -n "''^" "/Users/hani/multi-agent-skills/skills/hanig-verified-training/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/multi-agent-skills/skills/hanig-verified-workflow/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/multi-agent-skills/skills/hanig-reproducible-result/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/multi-agent-skills/skills/hanig-review-gate/PROTOCOL.md"
[Shell] rg -n "''^" "/Users/hani/multi-agent-skills/skills/hanig-review-gate/SKILL.md"
[Shell] rg -n "''^" "/Users/hani/multi-agent-skills/docs/audit-stepback-both-repos.md"
[Shell] rg -n "''^(STATES|GATES|DIGESTED_FIELDS|CRITERIA_FIELDS|def |class )|add_(parser|argument)|production|exclusive" /Users/hani/multi-agent-skills/skills/hanig-reproducible-result/scripts/result.py
[Shell] rg -n "''^(STATES|GATES|DIGESTED_FIELDS|CRITERIA_FIELDS|def |class )|add_(parser|argument)" /Users/hani/multi-agent-skills/skills/hanig-review-gate/scripts/review.py
[Shell] rg -n "''^(STATES|GATES|DIGESTED_FIELDS|CRITERIA_FIELDS|def |class )|add_(parser|argument)|production|exclusive" /Users/hani/multi-agent-skills/skills/hanig-verified-workflow/scripts/contract.py
[Shell] rg -n "''^(STATES|GATES|DIGESTED_FIELDS|CRITERIA_FIELDS|def |class )|add_(parser|argument)|production|exclusive" /Users/hani/multi-agent-skills/skills/hanig-verified-training/scripts/traincontract.py
[Shell] rg -n "''^[[:space:]]+def test_" /Users/hani/multi-agent-skills/tests
[Shell] rg -n "''^#|''^##|''^###|isolation|orchestrat|dispatch|Slurm|runaway|autonom|predicate|delete|retire" /Users/hani/multi-agent-skills/docs/plan-fusion.md /Users/hani/multi-agent-skills/docs/PLAN.md ...
[Shell] rg -n "''^#|''^##|''^###|state|manifest|job|run|handoff|review|gate|must|never|receipt|contract" "/Users/hani/multi-agent-skills/docs/plan-portable-handoff.md"
[Shell] rg -n "''^#|''^##|''^###|state|manifest|job|run|handoff|review|gate|must|never|receipt|contract" "/Users/hani/multi-agent-skills/skills/hanig-review-gate/PROTOCOL.md"
[Shell] rg -n "''^#|''^##|''^###|state|manifest|job|run|handoff|review|gate|must|never|receipt|contract" "/Users/hani/multi-agent-skills/skills/hanig-portable-handoff/SKILL.md"
---

The system should be a per-machine scientific project controller built on Paseo, with durable task state and isolated execution attempts. The verifier work becomes a library of completion predicates. It does not become the controller.

A useful drift test is:

> If a proposed feature only makes a verdict more elaborate, but does not improve dispatch, dependency handling, isolation, recovery, resource control, or human visibility, it is probably “a better verifier” and is not first-wave work.

## Root cause analysis: three levels deep

1. Why was the wrong system built?

   “Make Shreshth’s repo my own” was interpreted as extracting its artifact-contract idea instead of reproducing its capability: coordinating agents that collectively build something.

2. Why did that interpretation survive?

   Progress was measured by verifier rigor and test count. Those were locally coherent measures, but they did not measure the actual outcome: whether a swarm could advance a scientific project without continuous human direction.

3. Why did verification consume the design?

   Shared-filesystem attribution looked like the missing correctness problem, so increasingly elaborate observations were used to infer who wrote an artifact. That inference is unavailable in a shared writable namespace. Shreshth’s system avoided it structurally through exclusive worktrees.

The deeper technical mistake was treating completion evidence as the orchestration state. An autonomous project also needs dependencies, resource budgets, durable execution state, retries, cancellation, publication, and human interruption. Neither a verifier receipt nor an agent’s lifecycle state supplies those.

## Assumptions

- Controllers and Paseo run independently on each machine. There is no controller that dispatches remotely across lambda, andromeda, and chimera.
- Agents are honest but fallible. They may misunderstand a path or retry incorrectly; they are not intentionally escaping assigned scopes.
- Agents on a machine may share Hani’s Unix identity. Therefore `chmod 0700` is not agent isolation; isolation comes from controller allocation, path validation, and never assigning the same writable namespace twice.
- Slurm is the exclusive allocator for GPUs, CPUs, memory, and nodes. Agents do not manually select or share GPUs outside Slurm.
- Scientific programs can be given explicit output, temporary, and cache locations. Programs that insist on mutating shared canonical state need an adapter or a human-operated exception.
- Large datasets normally cannot be copied per attempt. They must be immutable/versioned inputs, or staged as a separate producer task before autonomous consumers use them.
- Resource limits must be generous enough for real work. Hard limits protect against catastrophic runaway; ordinary variance should cause warnings or bounded recovery, not constant human prompts.
- All cluster-installed code remains Python 3.8+ standard library. A new swarm skill should contain its own scripts and modules. Existing sibling skills are invoked as commands, never imported.
- Cross-cluster dependencies are explicit external inputs or handoffs. A chimera controller may report “waiting for an andromeda artifact,” but it does not control andromeda.

## 1. Minimum viable autonomous swarm

The smallest genuinely useful system is one local scientific project pod:

- One Paseo coordinator agent decomposes an approved objective into a small dependency graph.
- A deterministic, lightweight controller—not the coordinator’s prose—validates and dispatches eligible units.
- Two or three bounded workers can operate concurrently.
- Code-producing workers receive separate git worktrees.
- Every cluster execution receives a separate attempt capsule.
- A durable local ledger survives agent exits, controller restarts, and multi-day Slurm jobs.
- One initial human approval establishes the objective, resource envelope, allowed roots, and irreversible-action policy. Work within that envelope proceeds autonomously.

A representative MVP project should support a chain such as:

```text
prepare analysis code
        ↓
run dataset QC ───→ train/evaluate model
                         ↓
                 build figure/table
                         ↓
                 publish versioned result
```

### What dispatches work

A lightweight per-machine controller runs as a Paseo-supervised workspace script. It reads an approved project plan and an append-only event ledger.

The coordinator agent may propose or revise task units, but only the deterministic controller can:

- Allocate a worktree or attempt capsule.
- Launch a Paseo worker.
- Submit an `sbatch`.
- Retry an attempt.
- Publish an artifact.
- Charge the project’s resource envelope.

Workers produce code, job specifications, interpretations, and proposed follow-up tasks. They do not grant themselves more resources or submit unrecorded retries.

### What bounds work

Every project has:

- Maximum active agents and Slurm jobs.
- Maximum task count and plan expansion.
- Maximum attempts per task.
- Project deadline.
- Allowed filesystem roots.
- Allowed partitions/accounts/resource shapes.
- Reserved GPU-hour and CPU-hour envelope.
- Per-job Slurm walltime, GPU, memory, and CPU limits.
- Maximum diagnostic/replanning rounds.
- Explicit prohibition on deletion, overwrite, deployment, or canonical publication unless authorized.

The safe GPU accounting rule is to reserve the requested upper bound before submission:

`requested GPUs × requested walltime`

Actual Slurm usage can later replace the reservation. This is conservative, so the human should choose a realistic project envelope rather than approving every job.

### What decides a unit is done

There is no universal “verification succeeded” boolean. Completion is a conjunction selected by unit type:

1. Dependencies are satisfied.
2. The assigned worker or owned Slurm attempt reached an acceptable lifecycle state.
3. Required artifacts exist inside the attempt capsule.
4. Task-specific predicates pass.
5. Code review passes if the unit changed shared project code.
6. The resource and execution contract was not violated.
7. If publication is required, the versioned publish transaction succeeded.

Examples:

- A preprocessing task may require a terminal clean Slurm state, expected row count, schema, and QC range.
- A training task may require a loadable checkpoint and a declared convergence or budget-exhaustion classification.
- A figure task may require declared source identities, nonempty outputs, and selected numerical invariants.
- A planning task may require a valid dependency expansion, without any Slurm or artifact predicate.

An agent saying “done,” Paseo reporting `idle`, and Slurm reporting `COMPLETED` are observations—not standalone completion decisions.

### What stops a runaway

Three independent bounds apply:

- Paseo bounds agent time, iterations, and continuations.
- Slurm enforces walltime and allocated compute.
- The controller enforces project-wide submissions, concurrency, attempts, deadline, and reserved compute.

`pause` prevents further dispatch while leaving owned jobs alone. `stop` prevents dispatch and cancels only jobs whose job ID, submit time, cluster, user, and attempt binding establish controller ownership. The existing Slurm ownership logic is essential here.

Retry policy is classified and bounded:

- Preemption: retry from the latest valid checkpoint.
- Transient infrastructure failure: retry unchanged.
- OOM: one policy-authorized resource increase, within the envelope.
- Program failure: launch a diagnostic worker.
- Predicate failure: do not publish; diagnose or replan.
- Repeated identical failure: stop the task and notify Hani.
- Budget or policy violation: never self-expand; request approval.

### What the human sees

The MVP needs a local terminal view, not a central web application:

- Project objective and approved envelope.
- Task DAG and dependency blockers.
- Active coordinator and workers.
- Worktree and attempt IDs.
- Slurm job ID, state, elapsed time, requested resources, and attempt number.
- Predicate summary.
- Artifact path and publication state.
- Consumed/reserved budget.
- Last meaningful event and next controller action.
- Paused, blocked, failed, or awaiting-human states.

Notifications should be limited to milestones, actionable failures, approaching budget limits, permission requests, and project completion. Normal queueing and multi-day execution should not page the human.

## 2. Unit of isolation for cluster work

The unit is an **attempt capsule**, belonging to a logical task.

A git worktree remains part of the capsule when code is involved, but it is not the whole capsule.

Each attempt capsule contains or binds:

- A unique, never-reused attempt ID.
- A fresh writable root such as `.swarm/runs/<task-id>/<attempt-id>/`.
- A pinned code worktree and commit.
- Immutable command/job specification.
- Declared input identities.
- Environment identity.
- Dedicated output, log, temporary, and writable cache locations.
- Requested Slurm resources and budget charge.
- Exact Slurm job identity, including submit time and attempt binding.
- Required completion predicates.
- An append-only event history and final receipt.

Retries create new attempt capsules. A later attempt may read a previous checkpoint, but writes new checkpoints and metrics into its own namespace.

### Isolation by resource

| Resource | Isolation rule |
|---|---|
| Code | One worktree and branch per code-writing worker |
| Outputs | Fresh per-attempt output root; no canonical path writes |
| Scratch/temp | Per-attempt path or job-local `$SLURM_TMPDIR` |
| GPU/CPU/memory | Slurm allocation and GRES/cgroup enforcement |
| Environment | Immutable container or versioned conda prefix; no `conda install` during a run |
| Dataset | Immutable/versioned read-only reference; mutable datasets must first be published as a new version |
| Metrics/checkpoints | Per-attempt files; earlier checkpoints are inputs to later attempts |
| Large caches | Prebuilt immutable project cache, or per-attempt writable cache |
| External trackers | Unique run ID bound to the attempt; never used as sole evidence |
| Canonical result | Separate serialized publish transaction |

Environment construction and large cache population are themselves producer tasks. They write a new versioned namespace, validate it, then make it read-only for downstream jobs.

Publication is deliberately outside the execution namespace. A successful attempt publishes to a new versioned result directory, followed by an atomic pointer update where supported. Two attempts never write the same canonical directory. A conflict blocks publication rather than choosing a winner.

This does not provide hostile-process security when agents share a Unix account. It provides conclusive attribution under the stated honest-but-fallible model because the controller allocates each writable namespace once, supplies only that namespace to the worker, and rejects path escapes before submission.

## 3. What transfers from Shreshth’s repo

### Take unchanged

- Paseo’s per-machine daemon and agent lifecycle.
- Provider/profile discovery and explicit model selection.
- Agent creation, follow-up, completion notifications, schedules, and heartbeats.
- Worktree isolation for code-writing workers.
- Same-machine `agent-bus` messaging and model routing.
- The committee, advisor, and handoff interaction patterns.
- Bounded loops with maximum time and iterations.
- Deterministic launch records that verify provider, model, cwd, branch, and base.
- “Lifecycle state is not proof of task success.”
- One coordinator notification rather than one notification per leaf worker.

### Adapt

- A sprint pod becomes a scientific task pod.
- Ticket dependencies become a durable scientific DAG.
- Worker write scopes include worktree paths, attempt roots, datasets, environments, caches, and publication targets.
- `bus await` becomes lifecycle reporting. It should not claim `ARTIFACT READY`.
- Git status files become general task/attempt event records.
- The sprint monitor becomes a per-machine terminal status view.
- Worker limits expand from model turns to agents, Slurm jobs, retries, walltime, and compute budgets.
- Integration review becomes risk-calibrated: required for code-changing units, not every scientific execution.
- A coordinator can add tasks only inside the approved plan-expansion and budget envelope.

### Does not transfer

- A commit beyond `--base` as the general definition of completion.
- Git worktrees as sufficient isolation.
- Cherry-pick, PR, Linear, and merge lifecycle as the project state machine.
- A Mac-local monitor as the controller for cluster projects.
- The assumption that the worker process and produced work have similar lifetimes.
- Re-running an agent turn as the main retry mechanism.
- Code write scopes as a substitute for dataset, environment, scratch, and GPU policies.
- Mandatory whole-diff review for units that only execute unchanged scientific code.
- Model benchmarking and reviewer metrics as first-order scientific project goals.

## 4. What survives from the 603-test work

The test count should not be preserved as an asset. Preserve capabilities and the regression knowledge they encode.

### Keep as predicates or supporting primitives

From `contract.py`:

- `sacct` row ownership under job-ID reuse.
- Binding by submission/declaration time.
- Correct handling of terminal states containing spaces.
- `CANCELLED` with exit code `0:0` remaining failure.
- Literal `End=Unknown`.
- Timezone-correct parsing.
- Requeue/preemption handling.
- Owned-attempt history.
- Artifact checks: existence, size, lines, log patterns, bounded command predicates.
- Input identity with honestly labeled weak modes.
- Criteria digests, contract IDs, bounded reads, atomic receipts, and nonzero incomplete-evidence states.

From `traincontract.py`:

- Converged versus budget-exhausted, diverged, preempted, and incomplete.
- Metrics integrity and monotonic-step checks.
- Sparse/evaluation cadence rules.
- Loadable checkpoint and shard-set validation.
- Fresh checkpoint generation checks.
- Storage-tier reporting.
- Slurm ownership and timezone logic shared with workflow verification.

From `result.py`:

- Declared source and output identities.
- File-format, schema, numeric, and domain checks.
- Determinism/double-render checks where affordable.
- Provenance and named human acceptance.
- Criteria-digest integrity, after making it fail closed.

From `review.py`:

- Independent review of code-changing or high-risk units.
- Exact claims, threat model, bounded rounds, redaction, quorum, and machine-readable findings.

From `handoff.py`:

- Durable pointers and identities.
- Contract-instance binding.
- Unresolved-work-first presentation.
- Cross-machine state reporting without pretending to control remote jobs.

### Delete outright

- `--exclusive-outputs` as proof of exclusivity.
- Before/after inventories intended to infer the writer.
- `production-window.json` as a success gate.
- `--require-production-evidence` where it means “changed during the window.”
- Training’s unused production bracket.
- All wording that upgrades `changed_during_window` into “the command produced this.”
- Tests whose only purpose is to preserve those mechanisms.
- The verifier-authorizer and content-pinned receipt architecture proposed in the old fusion plan.
- `ARTIFACT READY` semantics in `bus await`; report lifecycle and observed evidence instead.
- The universal rule that every completed action requires an adversarial multi-model panel.
- Symmetry tests that merely reproduce an unsound rule across tools.

Keep window observations only as optional diagnostics. They must not affect success.

## 5. What autonomy requires that neither repo has

Neither repository currently supplies:

- A durable scientific task DAG.
- State that survives coordinator and worker death.
- Separation between agent lifetime and Slurm-job lifetime.
- Per-attempt isolation beyond git.
- Immutable dataset and environment policy.
- Slurm-aware resource reservation and project budgets.
- Dependency-aware dispatch.
- Bounded autonomous plan expansion.
- Failure classification and policy-based retry.
- Checkpoint lineage across attempts.
- Atomic, serialized result publication.
- A project-level pause/stop mechanism.
- Ownership-safe cancellation.
- Local reconciliation after daemon, login session, or machine restart.
- Scientific units that can finish without modifying git.
- Explicit distinction between execution, validation, interpretation, and publication.
- A rule preventing agents from weakening predicates or increasing budgets after failure.
- A usable local view of what the swarm is doing and why.

The central new component is therefore a durable controller/reconciler, not another verifier.

# PLAN

1. **Build the isolated attempt capsule and retire unsound attribution gates.**

   This is the load-bearing primitive and the highest value per line. Put the capsule allocator, manifest, submission binding, and path validation in one independently installable skill. Invoke existing verifier CLIs as subprocesses rather than importing sibling code.

   Acceptance criteria:

   - Two simultaneous attempts for one task receive distinct worktrees, output roots, temp roots, writable caches, logs, metrics, and checkpoint paths.
   - Retrying always creates a new attempt ID and writable namespace.
   - A prior checkpoint may be declared read-only input; the new attempt cannot name its directory as an output.
   - Every declared output resolves beneath the attempt root.
   - Path traversal, symlink escape, reused attempt roots, canonical-result writes, and environment mutation are rejected before `sbatch`; a test proves zero submissions occurred.
   - The manifest binds cluster, user, contract ID, code commit, inputs, environment, resources, job ID, and submit time.
   - Slurm ownership tests cover reused job IDs, `0:0` cancellation, `End=Unknown`, spaced terminal states, requeue, and timezone-equivalent timestamps.
   - GPU work is submitted only through declared Slurm GRES/resources.
   - `--exclusive-outputs`, production-window success gates, and their supporting tests are absent.
   - One small real job on each cluster writes only inside its capsule and produces an independently readable receipt.

2. **Build the Paseo scientific-project coordinator and bounded dispatcher.**

   Adapt the sprint pattern into a per-machine scientific DAG. The agent proposes work; the deterministic dispatcher authorizes it.

   Acceptance criteria:

   - Given one objective and resource envelope, the coordinator produces bounded units with dependencies, unit type, worker prompt, allowed scopes, resource request, retry policy, and completion predicates.
   - The plan validator rejects cycles, missing predicates, overlapping writable namespaces, unbounded retries, undeclared publication targets, and worst-case resource use above the approved envelope.
   - The dispatcher launches no more than the configured agent and Slurm concurrency.
   - Code-writing workers receive independent worktrees; execution-only workers are not required to create commits.
   - A worker cannot submit a Slurm job directly through the supported workflow; it produces a job specification that the dispatcher validates and submits.
   - A three-stage demonstration coordinates at least two agents and one Slurm job, holds downstream work until dependencies pass, and finishes without per-unit human approval.
   - Agent `idle`, agent prose, a clean worktree, and Slurm `COMPLETED` cannot individually mark a scientific unit done.
   - Plan expansion beyond task, attempt, deadline, or compute caps stops with one actionable human request.
   - The same skill installs and runs with `install.sh --only NAME` on Python 3.8 without importing sibling skills.

3. **Build the durable reconciler, bounded recovery, and local status surface.**

   Run it as a lightweight Paseo-supervised workspace script on each machine. It owns the event ledger, not scientific artifacts.

   Acceptance criteria:

   - Killing and restarting the controller during queued, running, preempted, predicate-failed, and publish-ready states resumes each project without duplicate agents, jobs, or publications.
   - A Slurm job may outlive every agent that created or monitored it and is still reconciled correctly.
   - Preemption, transient infrastructure failure, OOM, program failure, predicate failure, and repeated identical failure take distinct, testable paths.
   - Retries stop at their declared limits and never silently weaken predicates or expand resources.
   - Reserved GPU-hours prevent submission beyond the hard envelope; approaching the soft threshold emits a notification without stopping legitimate in-envelope work.
   - `pause` prevents new dispatch within one reconciliation cycle.
   - `stop` cancels only job records proven to belong to this controller and leaves unrelated jobs untouched.
   - Publication is versioned and serialized; concurrent publish attempts cannot overwrite one another.
   - `status` shows the DAG, active agents, owned Slurm jobs, attempts, resources, predicate outcomes, artifact locations, blockers, last event, and next action.
   - Normal queueing and running produce no repeated notifications; milestones and actionable failures produce exactly one.
   - A reviewer who did not implement the system can run the demonstration, interrupt it twice, and determine from the ledger why every final state was reached.
