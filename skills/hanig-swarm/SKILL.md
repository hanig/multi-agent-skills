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

## Running it unattended

`advance` is safe to schedule: it takes a lease, so a second invocation while
one is running exits without acting. Use a DETERMINISTIC scheduler, not a Paseo
schedule -- starting a fresh LLM agent every few minutes to invoke a
deterministic command adds cost and another failure mode.

```bash
# once per project, on the server that owns it
LINE="*/5 * * * * $HOME/swarm-live/scripts/swarm-cron.sh $HOME/swarm-live/PROJ  # hanig-swarm"
{ crontab -l 2>/dev/null | grep -v swarm-cron.sh; echo "$LINE"; } | crontab -
crontab -l | grep swarm      # confirm
# remove with: crontab -e
```

**Verified on lambda 2026-08-28:** cron fired on its own and advanced the DAG
with no human present. `crontab` exists on the lambda login node and the entry
survives disconnection.

The four things that make this safe, each proven by breaking it:

| hole | guard | proven |
|---|---|---|
| two controllers both dispatch a unit | lease keyed on the state dir, 900s TTL, `--force` to steal | one of two concurrent advances refused, live |
| crash between `sbatch` and bind | jobs named `swarm-<attempt>`; `reconcile_orphan` asks squeue/sacct | job id wiped from state on a LIVE job; advance recovered 187880 and did NOT resubmit |
| `INCOMPLETE` forever | terminal `FAILED_EVIDENCE` after a 600s settle window; holds dependents | unit test |
| plan edited mid-flight | canonical digest over dispatchable fields; refuses to advance | unit test |

## First real DAG, lambda 2026-08-28

Three units, `A -> {B, C}`, real `sbatch`. A submitted alone; B and C held; A
reached DONE and released both in one pass; all three finished with each output
in its own exclusive write root. `status` exited 0.

## Cluster gotchas, paid for on lambda 2026-08-28

**`--mem` is required on LAMBDA ONLY, and I first wrote this as if it were a
cluster fact.** Corrected after checking all three:

| | lambda | andromeda | chimera |
|---|---|---|---|
| default memory | `DefMemPerNode = UNLIMITED` | `DefMemPerCPU = 4096` | `DefMemPerCPU = 4096` |
| `--mem` required | **yes** | no | no |
| SelectTypeParameters | `CR_CORE_MEMORY,CR_ONE_TASK_PER_CORE` | `CR_CPU_MEMORY,CR_PACK_NODES` | `CR_CORE_MEMORY` |
| python3 | 3.12.3 | 3.10.12 | 3.10.12 |

Lambda is the only one with no default memory, so a job without `--mem` there
fails with Slurm's most misleading message, "Requested node configuration is not
available", while `sbatch --test-only` accepts the identical flags. Elsewhere it
just works, which is exactly how a one-cluster quirk gets written down as a
universal rule.

**Partition names share NOTHING across the three** (`labinloop model_dev
preemptible` / `all h100-reserved preemptible standard` / `gpu gpu_batch cpu
gpu_high_mem`), so a unit's `sbatch` list is NOT portable. A plan written on one
cluster does not run on another. See docs/plan-swarm.md step 5.

**Python floor is 3.10**, not 3.12: andromeda and chimera both run 3.10.12. Test
there, not on the newest.

**`chimera` cannot take a remote command** -- its ssh config carries
`RemoteCommand sh_dev`. Use `chimera-login`.

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
