---
name: hanig-swarm
description: >-
  Coordinate a swarm of agents to build projects autonomously on Slurm clusters.
  Use when dispatching, isolating, tracking or judging units of autonomous work
  across lambda/andromeda/chimera: a single Slurm job, a Nextflow or Snakemake
  pipeline, or a code-editing agent. Allocates an exclusive per-attempt write
  root so a cheap done-predicate is conclusive, binds a scheduler job to an
  attempt, and reports DONE / RUNNING / FAILED / PREEMPTED / INCOMPLETE. Use
  with the paseo skills for agent lifecycle and bus await for code units.
---

# hanig-swarm

Step 1 of `docs/plan-swarm.md`: the unit contract.

## The one idea

**Isolation replaces attribution.** The coordinator allocates an exclusive,
never-reused write root per attempt. The worker writes only there. Shared
datasets and environments are immutable pinned read-only inputs, not isolated.
Slurm cgroups already isolate GPU and memory.

Once the write root is exclusive:

    a declared output exists in the run-dir
      + a terminal-OK sacct row OWNED by this attempt
      == this unit produced it

No check anywhere asks "did the command write this file". That question is
unanswerable by observation on a shared filesystem, and it does not need
answering when nothing else may write here. Three plan versions died learning
that.

Shreshth's repo had this all along: Paseo gives each agent its own git worktree,
so his cheap `--base` predicate is conclusive because nothing else writes that
tree. He never solved attribution; an exclusive namespace made it unnecessary.

**Exactly one property does the isolating: the exclusive write root.** The pinned
commit, immutable spec and versioned environment are immutability of INPUTS --
they matter for reproducibility and ownership, never for making the predicate
conclusive. Keep that distinction: when a new expensive check is proposed, ask
"does write-root isolation already make this conclusive?" If yes, delete it.

## Usage

```bash
U=skills/hanig-swarm/scripts/unit.py

# 1. allocate an exclusive write root (prints the path on stdout)
D=$(python3 $U allocate --root .swarm/runs --task align-reads --kind slurm \
      --command "bwa mem ref.fa r1.fq r2.fq > out.bam" \
      --output out.bam --input ref.fa --gpu-hours 4 --charge-to hani)

# 2. submit however you like, then bind the job to the attempt
python3 $U bind "$D" --job-id 187196

# 3. the done predicate
python3 $U check "$D"        # DONE 0 | RUNNING 1 | FAILED 2 | PREEMPTED 3 | INCOMPLETE 4
```

Declared outputs are **relative to the run-dir**. One that resolves outside it is
refused, because an output the coordinator did not isolate supports no
conclusion.

## The three kinds

| kind | isolation | done predicate |
|---|---|---|
| `slurm` | exclusive run-dir + Slurm allocation | terminal-OK owned row AND declared output present |
| `pipeline` | fresh work dir + fresh publish dir, boundary only | engine's terminal exit AND final outputs present |
| `code` | git worktree + branch via Paseo | delegate to `bus await`; not duplicated here |

**A pipeline's interior is UNJUDGEABLE.** The engine owns its DAG, retries and
work directory, so per-task success and which internal step produced which
intermediate are not established. The receipt says so
(`basis.interior_judged: false`) rather than implying otherwise. Do not
reimplement the engine's scheduler.

## Drift guard

From the committee, verbatim: *"the center of gravity is the coordinator and the
human interface, not the predicate. If the surviving module grows past ~300 lines
or reacquires any 'the command wrote this' check, stop."*

The Slurm knowledge is lifted verbatim from `contract.py` and must not be edited
here. It was bought against a real scheduler: sacct row ownership under job-id
reuse, `0:0` on a CANCELLED job not counting as success, `End` arriving as the
literal `Unknown`, states containing spaces (`CANCELLED by 10025`), and a
timezone bug that made one instant read as three epochs nine hours apart.

## converge.py — did it converge, or just stop?

`unit.py` answers existence and terminal state. For a training run that is not
enough: a run that executes 40,000 steps, exits 0 and writes a checkpoint is
DONE by that predicate even if the loss was flat for the last 30,000.

```bash
python3 skills/hanig-swarm/scripts/converge.py check metrics.jsonl \
  --criterion '{"metric":"val_loss","mode":"min","rel_improvement_below":0.002,"over_evals":5,"min_steps":10000}' \
  --diverge '{"metric":"train_loss","above":1e9}' --budget 40000
# 0 CONVERGED | 1 NOT_YET | 2 DIVERGED | 3 BUDGET_EXHAUSTED | 4 INCOMPLETE
```

**BUDGET_EXHAUSTED is the distinction it exists for**: the run stopped, it did
not finish. Divergence is checked BEFORE convergence, because a run that blew up
and later coincidentally satisfied a threshold has not converged.

**Nothing in Shreshth's repo does this**, checked directly: its apparent hits
are a Unix timestamp ("epoch seconds"), a GPU-load scrape, and a REVIEWER
agreeing a diff is fixed. `paseo-loop`'s verification shapes cannot reach it --
a shell check answers "exit 0" and "checkpoint exists", never "did val_loss
improve by more than 0.002 over the last 5 evaluations". Not an oversight: his
domain is code changes, so his predicates are git-shaped.

**Inherited semantics worth knowing:** a plateau criterion reports CONVERGED for
a run plateaued at a BAD value, because it asks "has improvement stalled" and a
flat run has. Pair it with a threshold when the value matters.

## Cluster gotchas, paid for on lambda 2026-08-28

**`--mem` is REQUIRED on lambda**, and `sbatch --test-only` does NOT enforce it.
Without it a real submission fails with Slurm's most misleading message,
"Requested node configuration is not available", while `--test-only` accepts the
identical flags. Put `--mem=` in every unit's `sbatch` list. Fourth time in one
session that test-only and real submission disagreed.

**`--test-only` start times are pessimistic.** It predicted 22:08 for a job that
started and finished within one second of submission. Do not use its estimate to
decide whether to wait.

**A lift needs its callees, its imports AND its constants.** All three were
missing at some point in this module. The constants were the expensive ones:
`OWNERSHIP_SLACK_S` and `MAX_DIR_ENTRIES_SCANNED` were absent, `py_compile` was
clean, and 22 local tests passed -- because off-cluster there is no `sacct`, so
`sacct_state` returns before it can reach `sacct_row_is_ours`. One real job found
it in seconds. `tests/test_swarm.py::TestTheLiftIsClosed` now checks all three on
the AST, and is verified to fail when a constant is renamed.

Python 3.8+, stdlib only, login-node safe, no network.
