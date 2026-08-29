# Plan: autonomous agent swarm (goal (b))

> **Committee: claude-opus-4-8 + gpt-5.6-sol, fresh context, same prompt, in
> parallel. Both converged. 2026-08-28.**
>
> **The goal, in Hani's words:** "I really want (b)... I want what he has...
> which is the ability to coordinate a swarm of agents to build projects
> autonomously more or less". Verification is ONE PREDICATE AMONG SEVERAL, not
> the point of the system.
>
> **What this replaces.** Hani handed me Shreshth's repo and said "I wanna make
> it my own". I narrowed that to "extract the artifact-contract idea, discard
> the coordination machinery", recorded my own narrowing as "the settled premise
> of the repo", and built three CLI verifiers with 603 tests. Good engineering
> aimed at the wrong target. Three subsequent fusion plans (v1, v2, v3) were all
> rejected, the last on a CRITICAL both reviewers reached independently:
> production ATTRIBUTION is unreachable by observation on a shared filesystem.
>
> ## The convergence
>
> **Isolation replaces attribution**, and Shreshth had this all along: Paseo
> gives each agent its own worktree, so his cheap `--base` predicate is
> conclusive because nothing else writes that tree. He never solved attribution;
> an exclusive namespace made it unnecessary.
>
> Both members reached the same mechanism from different angles. sol called the
> unit an "attempt capsule"; claude called it "the write namespace".
>
> ## The one disagreement, resolved in claude's favour
>
> sol's capsule BUNDLES isolation with provenance, implying the whole bundle
> isolates. claude's correction: exactly ONE property does the isolating, the
> exclusive write root. The pinned commit, immutable spec and versioned env are
> IMMUTABILITY OF INPUTS -- they matter for reproducibility and ownership, not
> for making the done-predicate conclusive.
>
> Adopted because it is a DRIFT GUARD, not a naming preference: when someone
> later proposes an expensive check, the test becomes *"does isolation of the
> write root already make this conclusive? If yes, delete the check."* The
> capsule framing hides that test; the write-namespace framing surfaces it.
> Same implementation, sharper discipline. sol's concrete field list is adopted
> wholesale as the shape of a unit.
>
> ## What gets deleted, per both members independently
>
> Everything built for production attribution: `--exclusive-outputs` as proof,
> the before/after inventories, `production-window.json` as a success gate,
> `--require-production-evidence`, the training bracket, every wording that
> upgrades `changed_during_window` into "the command produced this", and the
> tests preserving them. `traincontract.py` and `result.py` in full. The
> conformance and symmetry suites. `review.py` survives as ONE OPTIONAL
> PREDICATE, never a universal gate.
>
> **What survives, named exactly by claude** -- all from `contract.py`: state
> sets `61-80`, `exit_code_is_clean` `840-858`, `parse_iso_ts` `796-837`,
> `sacct_row_is_ours` `582-636`, `sacct_state` `861-918`, freshness `646-793`,
> watchdog `87-116`, plus ~7 domain tests. That knowledge was bought with real
> bugs against a real scheduler and would otherwise be rediscovered.
>
> **Drift guard, claude's own words:** "the center of gravity is the coordinator
> and the human interface, not the predicate. If the surviving module grows past
> ~300 lines or reacquires any 'the command wrote this' check, stop."
>
> ## The unit is polymorphic, because Hani chose three kinds
>
> Single Slurm job, Nextflow/Snakemake pipeline, and code-editing agent. NOT
> analysis/figure jobs, which is why `result.py`'s domain leaves the unit
> boundary entirely.

---

## The unit of isolation

The unit of isolation is the WRITE NAMESPACE, not the filesystem, GPU, conda env, or dataset. A worker READS shared datasets and envs; it WRITES outputs, logs, scratch. Only the write target needs an exclusive namespace; reads need immutability, not isolation.

Per resource:
- Outputs/logs: coordinator allocates a fresh, never-reused directory per unit (`.swarm/runs/<task-id>/<attempt-id>/`); the job writes only there. This is the generalization of Shreshth's worktree from code to arbitrary outputs.
- Scratch: `$SLURM_TMPDIR`, per-job by construction (Slurm tears it down).
- GPU + memory: the Slurm allocation. Slurm cgroups already isolate these per-job; no swarm mechanism needed. Do not oversubscribe.
- Conda env / container: an immutable, versioned reference (container image or versioned prefix). No mid-run installs. If a unit needs a new package, it builds a new versioned env as its own unit, it does not mutate a shared one.
- Datasets: read-only, pinned by path plus content identity, recorded in the unit spec. Pinning gives immutability; isolation is unnecessary for reads.

Once the write namespace is exclusive, the done-predicate collapses to a cheap, conclusive check: declared output exists in the unit's dir, written after job start, with the Slurm row in a terminal-OK state owned by this attempt. No production-window, no digest-before/after, no foreign-write scan.

---

## Per-kind treatment of the three unit types

Common to all three: a never-reused attempt id, an exclusive write root, an immutable spec, declared read-only inputs, a resource/budget charge, an append-only event log, and a done-predicate whose success is conclusive because the write root is exclusive. Retries mint a new attempt, never reuse one. Per-kind, only the done-predicate and the isolation boundary differ.

**Single Slurm job.** Maps directly onto the write-namespace answer and the surviving Slurm-state knowledge. Isolation: exclusive run-dir + Slurm allocation. Done: terminal-OK owned row (`sacct_state`) AND declared output exists in the run-dir. This is the clean case.

**Nextflow/Snakemake pipeline.** The hard one. The engine owns its internal DAG, retries, and work directory; the swarm cannot see or isolate inside it. Isolation is possible ONLY at the pipeline boundary: give the engine a fresh `-work-dir`/working directory and a fresh publish/output directory per unit, and let it manage its interior. What is therefore UNJUDGEABLE: per-task success inside the pipeline, and any claim about which internal step produced which intermediate. The coordinator does not try. It judges the boundary only: the engine's own terminal exit is honoured as the engine's verdict (Nextflow exit code, Snakemake exit code), plus declared FINAL outputs exist in the exclusive publish dir. State this limit plainly in the receipt: "interior not judged; engine's self-report trusted at the boundary." Do not reimplement the engine's scheduler.

**Code-editing agent.** Shreshth already solves this; take it UNCHANGED. Isolation: git worktree+branch via Paseo. Done: `bus await` artifact contract (`--base` HEAD advanced, `--require-clean`, optional `--status-file`). Route it straight through `bus launch-worker` / `bus await`. No redesign.

Analysis/figure jobs were not selected; `result.py`'s domain is out of scope for the unit boundary, which strengthens the case for deleting it.

---

## Where I disagree with gpt-5.6-sol's "attempt capsule" framing

Agreement on the mechanism: never-reused attempt id, fresh writable root, pinned commit, immutable job spec, declared read-only inputs, versioned env identity, Slurm resources under a budget charge, append-only event log, retries create NEW capsules. Adopt all of it as the concrete shape of a unit.

Disagreement on the framing: "capsule" bundles isolation and provenance into one object and implies the whole bundle is what isolates. Sharper: exactly ONE property in the bundle does the isolating, the exclusive write root. The pinned commit, immutable spec, and versioned env are IMMUTABILITY of inputs, not isolation, and they matter for reproducibility and ownership, not for making the done-predicate conclusive. Keeping the distinction explicit prevents the next drift: if someone later proposes an expensive check, the test is "does isolation of the write root already make this conclusive?" If yes, delete the check. The capsule bundle hides that test; the write-namespace framing surfaces it. Same implementation, sharper discipline.

---

## NEEDS A DECISION

Two questions could not be confirmed interactively; each is answered here with the most conservative reading that still ships, marked so it can be corrected.

`ASSUMPTION` (promotion): outputs stay in the exclusive run-dir and promotion to any shared canonical tree is a SEPARATE, human-gated, atomic, recorded step (which unit, which digest, approved by whom), per-unit opt-in. This mirrors Shreshth's no-merge-until-gate and is the safe default on a shared filesystem: a swarm that silently writes a shared tree gets switched off. Recommendation: keep the gate; make per-project-isolated the trivial case (no promotion declared). Correctable to "per-project isolated only" (drop the gate) or "auto-promote" (remove the human step) without touching the isolation core.

`ASSUMPTION` (detachment substrate): use Paseo schedules/heartbeats to wake the checker, since Paseo is already a dependency and this adds no OS-level daemon per machine. The checker reads durable per-unit state and advances the DAG. Recommendation: Paseo schedules. Correctable to an external cron entry per machine (fewer Paseo moving parts, one more OS one) without touching the unit contract, since the checker is invoked identically either way.

---

## Every ASSUMPTION line

`ASSUMPTION` (promotion): outputs stay in the exclusive run-dir and promotion to any shared canonical tree is a SEPARATE, human-gated, atomic, recorded step (which unit, which digest, approved by whom), per-unit opt-in.

`ASSUMPTION` (detachment substrate): use Paseo schedules/heartbeats to wake the checker; the checker reads durable per-unit state and advances the DAG.

---

## PLAN

Build order is by value per line: the isolation-conclusive predicate first (smallest, load-bearing), the coordinator second (the swarm itself), the human interface third (what keeps it switched on).

**SHARPENED 2026-08-28 by gpt-5.6-sol against the built code.** Two things I
asserted were wrong, and both are the same shape as errors this project has
already made.

**1. `advance` is NOT safe to schedule as it stands.** I called it "idempotent
and safe to re-enter". Four concrete holes: two concurrent `advance` processes
can both load old state and submit the same unit; the crash-before-bind window
is unresolved; `INCOMPLETE` can stay live forever; and there is no plan digest
or owner lease. So the step is **"safe autonomous advance", not "install a
schedule"**, and its acceptance test must run two advances CONCURRENTLY and
inject crashes immediately before `sbatch`, immediately after `sbatch`, and
immediately after bind.

**2. The isolation claim over-reaches, exactly as the attribution claim did.**
The run directory is unique but is NOT an enforced boundary: a command can write
an absolute path outside it, another process under the same Unix account can
write into it, `write_scopes` describe intent rather than constraining writes,
directory inputs are recorded as "not a regular file" rather than content-pinned,
and neither the plan nor `unit.json` is frozen. The receipt must say:

> Exclusive by coordinator allocation under a trusted-writer convention; not
> isolated from other processes running as the same Unix user.

OS-enforced isolation needs a container or mount namespace with the attempt
directory as the only writable bind mount. Until then, do not claim it. **Third
time this project has claimed more than its mechanism establishes.**

**3. Scheduling substrate decided: deterministic cron/launchd/systemd-user, NOT
a Paseo schedule.** "Starting a fresh LLM agent every few minutes to invoke a
deterministic command adds cost and another failure mode." Paseo keeps alerts and
agent work. Paseo schedules are the fallback only if cluster policy forbids cron.

**4. Five cuts, seven nominal steps to six substantive ones:**

- `grill-with-docs` is not a build step; it is a mandatory pre-dispatch GATE
  inside the project front door.
- GitHub Issues out of v1. Linear as the work ledger, GitHub PRs as code
  evidence. "Pluggable backend" is abstraction before one proven backend.
- No bespoke web monitor in v1. Ship `status --json`, a good terminal view, and
  notifications; add a page once Hani knows what he actually reads.
- Defer `pipeline` units until a real Nextflow/Snakemake project needs them.
- No automatic cross-cluster failover. On outage, enter `BLOCKED_CLUSTER` and
  ask. Rerouting is unsafe until dataset, container, checkpoint and canonical
  output availability are profiled per cluster.

**5. Eighteen of the 45 criteria are weak**, with named fixes. The sharpest:
1(a) two random allocations differing does not prove exclusive writers, so test a
forced collision; 1(h)'s "≤300 line core" is fuzzy and `unit.py` is already 864
lines; 2(c) "GPU-hours spent" is undefined and today charges declared capacity
once, never per retry; 3(e) atomic promotion by rename fails across filesystems,
so promote a versioned directory and swap one canonical pointer; 3(g) "the page
never executes" contradicts promotion approval, so separate a read-only monitor
from an authenticated promotion command.

**6. Eleven missing failure modes**, to fold into the safe-advance and operator
steps rather than becoming new top-level ones: cluster outage (`BLOCKED_CLUSTER`,
and never read a missing `sacct` row as failure OR success), a `NEEDS_HUMAN`
state carrying question/recommendation/options/deadline, reserved-vs-accrued-vs
-projected cost with a halt before the ceiling, agent-loop bounds, a plan digest
that refuses advancement when the file changes mid-flight, an owner lease keyed
by project so a second controller exits without acting, an idempotency token for
crash-during-dispatch, escalation from `INCOMPLETE` to terminal `FAILED_OUTPUT`
once accounting settles, an idempotent outbox so a tracker outage never alters
swarm state, an agent secrets/permissions policy, and input locality per cluster.

**7. On `converge.py`, independently flagged:** a plateau criterion treats a bad
flat run as converged BY DESIGN, so plateau alone must never mean scientific
success: pair it with a quality threshold, or treat it as "stop training"
rather than "done". This matches the note already in the code and goes further.

**The immediate next milestone is one SCHEDULED, single-writer, crash-injected
Slurm DAG on lambda**, not the dashboard, not the ticket abstraction. After it
runs unattended for several days, INSTALL the same tooling on andromeda and
chimera and run a separate project on each (not one plan spanning three, per
Hani's correction), then connect Linear.

See `docs/scenario-mach1-zebrafish.md` for the whole string of events walked end
to end.

**Status 2026-08-28.** Steps 1 and 2 are BUILT and one real Slurm job ran end to
end on lambda (`DONE`, exit 0). `converge.py` was ported before deleting
`traincontract.py`. Steps 3 and 4 below are NOT built, and they are the two
capabilities Shreshth's repo has that ours lacks: he has a Mac-local read-only
sprint monitor, and Paseo `create_schedule` / `create_heartbeat`. Our coordinator
was designed to detach and be re-entered by a schedule, and no schedule has ever
been created -- so today the "autonomous" swarm only advances when a human types
`swarm.py advance`. That is the gap between a coordinator and a swarm.

**1. Unit contract + Slurm-state predicate (the load-bearing primitive).**
A stdlib-only module (Python 3.8+, login-node safe). Allocate an exclusive, never-reused run-dir per attempt; record command, declared outputs, pinned read-only inputs, env identity, Slurm job-id, budget charge; append-only event log. `check` returns {RUNNING, DONE, FAILED, PREEMPTED, INCOMPLETE} using ONLY the surviving functions lifted from `contract.py:61-80,582-636,646-918` plus the watchdog `87-116`. Per-kind done: Slurm job = terminal-OK owned row + output-exists; pipeline = engine terminal exit + final-output-exists, receipt marks interior unjudged; code agent = delegate to `bus await`.

Acceptance criteria:
- a. Two units cannot be allocated the same run-dir; the allocator/validator rejects it (reuse `validate_sprint_plan.py` overlap logic).
- b. A CANCELLED job with ExitCode `0:0` returns FAILED, not DONE.
- c. An output present in the exclusive dir, mtime after job start, with a terminal-OK OWNED sacct row returns DONE.
- d. Under simulated job-id reuse (a later Submit inside/after the interval), the wrong row is not bound: DONE is not returned for a reused id.
- e. `End == "Unknown"` and states like `CANCELLED by 10025` parse correctly.
- f. The same UTC instant rendered `+0000`, `-0700`, `+0200` yields one epoch.
- g. Runs under Python 3.8 stdlib only, no network, on a login node.
- h. Module executable core is <= ~300 lines and contains no production-window or foreign-write code (grep-checkable).

**2. Coordinator: dispatch, bound, detach, gate the DAG (the swarm).**
Adapt `start-a-sprint`. Read a plan of polymorphic units (kind, command/spec, declared outputs, deps, pinned inputs, resource budget, optional promotion, optional review). Validate exclusive namespaces + acyclic deps (`validate_sprint_plan.py` shape). Per ready unit: allocate namespace, submit (Slurm via sbatch, pipeline via engine, code via `bus launch-worker`), write durable state, DETACH. A checker (Paseo schedule per ASSUMPTION) resumes, runs the unit contract check, advances the DAG when upstream predicates pass, mints a new attempt on retryable failure per the taxonomy, and halts new dispatch on budget/runaway.

Acceptance criteria:
- a. Given a 3-unit DAG (A -> B, A -> C), B and C submit only after A's predicate returns DONE.
- b. Killing the coordinator process and re-running resumes from durable state without resubmitting completed units and without minting duplicate attempts.
- c. Exceeding the declared GPU-hour budget halts new dispatch and reports which units were skipped.
- d. A runaway unit hitting its Slurm `--time` is classified TIMEOUT/FAILED, its downstream units are held, and a retry (if policy allows) mints a NEW run-dir rather than reusing the old one.
- e. A preempted unit is classified PREEMPTED and requeued/re-attempted per policy, not marked FAILED.
- f. A pipeline unit whose engine exits non-zero is FAILED regardless of any partial outputs in the publish dir.

**3. Human monitor + gated promotion (what keeps it switched on).**

Adapt `start-a-sprint`'s Mac-local read-only monitor. Per unit: kind, state,
job-id, wall-clock, GPU-hours against budget, predicate verdict, attempt count,
promotion status. Reads durable state ONLY, so it renders with the coordinator
stopped. Promotion moves outputs from the exclusive write root to a declared
shared canonical path on explicit human approval, atomically, recorded.

Acceptance criteria:

- a. Loopback only (127.0.0.1), read-only, no daemon beyond the page server, and
  it renders correctly while the coordinator process is stopped.
- b. Every unit in the plan appears with a live verdict, including HELD units and
  the reason they are held.
- c. Budget is shown as spent-of-declared, and a halted swarm says so on the page
  rather than only in a log.
- d. No output reaches a shared canonical path without explicit approval; a unit
  that declares no promotion never touches a shared path at all.
- e. Promotion is ATOMIC (rename or link, never a partial copy) and appends a
  record naming unit, attempt, digest, approver and timestamp.
- f. A promotion whose source digest no longer matches the receipt is REFUSED,
  naming the mismatch. The receipt is evidence about a moment, and promotion
  happens later.
- g. The page never executes anything: no dispatch, no retry, no cancel. Reading
  and approving are the only verbs, so a stale page cannot start work.

**4. Scheduling: make `advance` run without a human (what makes it autonomous).**

`swarm.py advance` is idempotent and safe to re-enter -- that was designed in at
step 2 and has never been wired. Until it is, the swarm is a dispatcher a person
must poke. Shreshth has this: Paseo `create_schedule` starts a fresh agent on a
cron cadence, `create_heartbeat` returns a prompt to an existing session.

Acceptance criteria:

- a. One command registers a recurring `advance` for a named plan, per machine,
  and one removes it. Registering twice does not create two schedules.
- b. The schedule survives the session that created it, and survives a logout on
  a cluster login node (or the plan states plainly that it does not, and says
  what to use instead).
- c. A failing `advance` does not silently stop the schedule; the failure is
  visible on the monitor page from step 3.
- d. Two `advance` runs that overlap do not double-dispatch a unit. Either a lock
  or the existing persist-before-act ordering must be shown to cover it, with a
  test that runs two advances concurrently against one state dir.
- e. The cadence is declared in the plan, not hardcoded, and a unit kind whose
  jobs run for days does not get polled every minute.
- f. Stopping the schedule leaves the DAG resumable by hand: no state lives only
  in the scheduler.

**ASSUMPTION** (substrate): Paseo schedules, since Paseo is already a dependency
and this adds no per-machine OS daemon. Correctable to a cron entry per machine
without touching the unit contract, because `advance` is invoked identically
either way. Criterion 4(b) is the one that decides it: if a Paseo schedule does
not survive logout on a login node, cron wins.

**5. Install cleanly on each server; a project lives on ONE server.**

**Corrected by Hani 2026-08-28**, and it removes the largest piece of
complexity sol had proposed:

> "I expect to install these skills on each of the respective servers, and just
> use it on those servers for distinct projects. I don't necessarily expect to
> jump between them."

So **cross-cluster plan portability is NOT a requirement**. A project is bound to
one server, its plan carries that server's `sbatch` flags, and nothing has to
translate a resource request across clusters. Dropped as a result: the
resource-request vocabulary, per-machine profile translation, dataset and
container path mapping across clusters, and cross-cluster completion evidence.
Sol's scenario put `validate-data` on chimera, training on andromeda and
evaluation on lambda; that is not the use case, and it was my problem statement
that invited it.

What the cluster differences DO still mean is that the tooling must install and
behave identically wherever it lands, and must not bake in one cluster's quirks:

| | lambda | andromeda | chimera |
|---|---|---|---|
| default memory | `DefMemPerNode = UNLIMITED` | `DefMemPerCPU = 4096` | `DefMemPerCPU = 4096` |
| `--mem` required | **yes** | no | no |
| python3 | 3.12.3 | 3.10.12 | 3.10.12 |
| partitions | `labinloop model_dev preemptible ...` | `all* h100-reserved preemptible standard` | `gpu gpu_batch cpu* gpu_high_mem ...` |

Acceptance criteria:

- a. `install.sh` puts the swarm skill on the Mac, lambda, andromeda and
  chimera, and the SAME test suite passes on each.
- b. The suite runs under **python3.10**, the floor two of the three run. 3.12
  is not the development target.
- c. No cluster's quirk is hardcoded. `--mem` is a plan-level flag the author
  supplies, not a tool assumption; nothing refers to a partition by name.
- d. `chimera` cannot take a remote command (`RemoteCommand sh_dev`); anything
  scripted uses `chimera-login`. Recorded so it is not rediscovered.
- e. One real unit dispatched and judged DONE on EACH server, in that server's
  own project, not one plan spanning three.
- f. A plan validated on one server that names another server's partition fails
  at validate time with a clear message, rather than at submit.

**6. Tickets: the orchestrator CREATES the project and populates its issues.**

**Clarified by Hani 2026-08-28:**

> "I want the project creation and population of issues to be done
> automatically (asking me for approval is fine of course)"

I had written this as "outward-facing, gated on explicit approval per run",
which read as heavy and manual. The requirement is the opposite: **automatic,
with one approval**. The orchestrator drafts the project and the full issue set,
shows them, and on a single yes creates and populates everything. It does not
ask per issue.

Linear is the work ledger for v1; GitHub PRs are code evidence. A pluggable
backend is abstraction before one proven backend, so GitHub Issues wait.

Acceptance criteria:

- a. From a grilled project brief, the orchestrator DRAFTS the project and every
  issue, and creates them all after ONE approval. No per-issue prompting.
- b. The draft is shown in full before anything is created, and a dry run prints
  exactly what would be created while creating nothing.
- c. Every issue maps to one or more unit predicates, and every unit maps back
  to one issue, so neither can drift silently from the other.
- d. BUILT. Issue state follows unit state, never the reverse. Swarm durable
  state stays authoritative; the tracker is a view. Enforced structurally: the
  coordinator imports no network module, checked by AST in `test_outbox.py`.
- e. BUILT. **No issue is ever closed on a self-report.** A `close` intent is
  emitted only from a predicate verdict and carries the receipt. An agent
  saying "done" on a ticket is exactly the self-assertion this family refuses.
- f. BUILT. Tracker mutations go through an idempotent outbox
  (`<state-dir>/outbox.jsonl`, `swarm.py outbox`). Keyed on
  `(project, unit, state, attempt_dir)`, so a retried drain converges instead
  of duplicating, and an unwritable outbox warns rather than stalling the DAG.
  See `docs/tracker-outbox.md`. No drain is written yet: the registry offers no
  Linear connector, so that half waits on the account.
- g. Re-running against an existing project updates rather than duplicating,
  keyed on the project and unit ids.

**7. `grill-with-docs`: interrogate the HUMAN before dispatching anything.**

FOUND on chimera, byte-identical in three colleagues' skill directories
(`rishiv`, `ivyliu`, `aadduri`), now installed on this Mac along with its
richer variant `grill-with-docs`. My earlier guess -- "adversarial questioning,
belongs beside paseo-committee" -- was wrong in the way that matters. Its ten
lines:

> Interview me relentlessly about every aspect of this plan until we reach a
> shared understanding. Walk down each branch of the design tree, resolving
> dependencies between decisions one-by-one. **For each question, provide your
> recommended answer.** Ask the questions one at a time. **If a question can be
> answered by exploring the codebase, explore the codebase instead.**

**It is the input side, not another reviewer.** `paseo-committee` is two AI
agents arguing with each other; `review.py` is a panel attacking a claim. Both
are AI-to-AI. `grill-me` extracts the decisions only Hani holds, one at a time,
with a recommendation attached so answering is cheap.

That gap is measurable in this very session. Three times a wrong assumption of
mine survived into built work: the mandate ("make it my own" narrowed to
"extract one idea", recorded as settled premise and quoted back for three days),
the topology (a fleet dispatched from a Mac, when the controller is per-machine),
and the goal itself ((a) verified evidence vs (b) an autonomous swarm -- days of
verifiers before it surfaced). Each was one question with a recommended answer.
Each cost more than the question would have.

**`grill-with-docs` is the one installed; the lighter `grill-me` was removed.**
Both interrogate one question at a time with a recommended answer, and both
refuse to ask what inspection can answer. The variant adds what this project
actually needs:

- **A glossary discipline.** This session's most expensive errors were
  DEFINITIONAL. "Production evidence" was used to mean "changed during a
  window", and three tools shipped receipts implying the command wrote the
  file. Two reviewers had to correct the LABEL before the fix was visible:
  "relocated self-assertion" pointed at rewriting the binding, "missing
  production evidence" pointed at adding a gate. Same for "committee" (two
  agents vs a five-model panel), "unit", and "settled".
- **An ADR test worth its weight**: offer one only when the decision is hard to
  reverse, surprising without context, AND a real trade-off. That describes
  "isolation replaces attribution", the deletion of `traincontract.py` after
  porting its evaluator, and all SEVEN retired rules -- which today live
  scattered across commit messages and MEMORY.md, which is exactly why the same
  idea was retired three times before anyone stepped back.
- **Cross-referencing claims against code**, the rule `grill-me` states in one
  line and this one operationalises.

There is no `CONTEXT.md` or `docs/adr/` in this repo yet; it creates them lazily
as decisions crystallise. `disable-model-invocation: true`, so it runs only when
asked for by name.

Acceptance criteria:

- a. Before a swarm plan is dispatched for the first time, its open decisions
  have been grilled: every `NEEDS A DECISION` and every `ASSUMPTION:` line is
  either answered by Hani or explicitly deferred by him, not by me.
- b. **A question answerable by inspection is never asked.** Cluster limits, ssh
  config, partition names, what a colleague's skill contains -- check, do not
  ask. Today's `--mem` correction and this skill's own location were both one
  command away.
- c. Every question carries a recommended answer, so the default costs one word.
- d. Grilling happens BEFORE step 6 files any ticket. A ticket generated from an
  ungrilled plan propagates a wrong assumption into a shared workspace, where it
  is expensive to retract.
- e. What the grilling settles is written into the plan as answered, with who
  answered it, so a later session does not silently re-derive it -- the failure
  mode that produced "the settled premise of the repo".



---

## Verification (end to end)

- Unit predicate: run the module's unit tests (the ~7 kept domain cases) under Python 3.8; assert exit-code semantics per 1(b)-(f); grep the source to confirm 1(h).
- Coordinator: drive a synthetic 3-unit DAG of trivial Slurm jobs (or `sleep` stubs where Slurm is absent) on a real cluster login node; verify 2(a)-(f), including a coordinator-kill-and-resume and a forced budget overrun.
- Pipeline kind: run a minimal Nextflow/Snakemake "hello" with a fresh work-dir and publish-dir; verify boundary-only judging and 2(f).
- Code kind: launch one code worker through `bus launch-worker`/`bus await` unchanged; verify it still passes on `--base` + `--require-clean`.
- Monitor: load the loopback page with the coordinator up, then killed; confirm 3(a)-(d), including that an un-promoted unit's outputs never appear in the shared path.

---

The full plan is at `/Users/hani/.claude/plans/groovy-forging-noodle.md`. It also carries the Context, root-cause analysis (three levels), the transfer table (unchanged/adapted/does-not-transfer from Shreshth), and the survive/delete list for the 603-test verifier with exact line ranges.
