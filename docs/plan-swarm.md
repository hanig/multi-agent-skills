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
Adapt `start-a-sprint`'s loopback dashboard. Per-unit: kind, state, job-id, wall-clock, GPU-hours/cost, predicate verdict, promotion status. Reads durable state, so it works with the coordinator down. Promotion moves outputs from the exclusive dir to the declared shared canonical path only on explicit human approval, atomically, recorded (unit, digest, approver, timestamp).

Acceptance criteria:
- a. Dashboard shows all units with live verdicts, read-only, loopback-only (127.0.0.1), no daemon required beyond the page server.
- b. No output reaches the shared canonical path without explicit approval; a unit with no promotion declared never touches a shared path.
- c. Promotion is atomic (rename/link, not partial copy) and appends a record naming unit, digest, approver, timestamp.
- d. The dashboard renders correctly while the coordinator process is stopped, reading only durable per-unit state.

---

## Verification (end to end)

- Unit predicate: run the module's unit tests (the ~7 kept domain cases) under Python 3.8; assert exit-code semantics per 1(b)-(f); grep the source to confirm 1(h).
- Coordinator: drive a synthetic 3-unit DAG of trivial Slurm jobs (or `sleep` stubs where Slurm is absent) on a real cluster login node; verify 2(a)-(f), including a coordinator-kill-and-resume and a forced budget overrun.
- Pipeline kind: run a minimal Nextflow/Snakemake "hello" with a fresh work-dir and publish-dir; verify boundary-only judging and 2(f).
- Code kind: launch one code worker through `bus launch-worker`/`bus await` unchanged; verify it still passes on `--base` + `--require-clean`.
- Monitor: load the loopback page with the coordinator up, then killed; confirm 3(a)-(d), including that an un-promoted unit's outputs never appear in the shared path.

---

The full plan is at `/Users/hani/.claude/plans/groovy-forging-noodle.md`. It also carries the Context, root-cause analysis (three levels), the transfer table (unchanged/adapted/does-not-transfer from Shreshth), and the survive/delete list for the 603-test verifier with exact line ranges.
