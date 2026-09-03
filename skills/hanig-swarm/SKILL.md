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

## The default agent

A `code` unit runs `codex/gpt-5.6-sol` at `thinking: high` unless it says
otherwise: the strongest agent on this machine by `bus models`' measured
intelligence, ahead of `claude/opus`.

```json
{"id": "impl", "kind": "code", "repo": "/path", "target_branch": "main",
 "prompt": "...", "mode": "full-access"}
```

Override with `provider`, `model` or `thinking` on the unit. `thinking: null`
suppresses the flag for a provider that has no such option.

`mode` is PROVIDER-SPECIFIC and there is no portable value. codex takes
`auto`, `auto-review` or `full-access`; claude takes `bypass` or `default`.
Writing claude's word on a codex unit is rejected at dispatch, which is the
one way this gets caught, so the mode has to follow the provider.

Every one of those strings was read off live agents rather than guessed,
because paseo answers an unknown thinking id with an ERRORED agent: a default
that fails at dispatch is worse than no default. `mode` deliberately has none,
because an agent under default permissions stops at its first write and waits
for a person, and a coordinator that bypassed that on the human's behalf would
be a worse bug than a stalled unit.

## Declare the runtime; prove it where the job lands

Every `slurm` and `pipeline` unit declares what it executes in. Not because a
validator can check it, but because nothing else can:

```json
"runtimes": {"py": {"id": "py", "resolution": "direct",
                    "entrypoint": "/home/me/envs/x/bin/python3",
                    "probe": "python3 -c 'import h5py'",
                    "verified_by": "canary:runtime-probe"}},
"units": [{"id": "runtime-probe", "kind": "slurm", "runtime": "py", ...},
          {"id": "work", "kind": "slurm", "runtime": "py",
           "needs": ["runtime-probe"], ...}]
```

`resolution` is one of `direct`, `path`, `conda`, `container`, `module`, `uv`,
`wrapper`. `verified_by` is `canary:<unit-id>`, `preflight`, or
`unverified:<why>`. A unit running only base-image tools declares
`"runtime": "none"`, which is a claim, not an omission.

**The validator does not read your command and does not stat your
entrypoint.** There is no reliable static shell chokepoint: `srun python`,
`conda run -n e python`, `apptainer exec img python`, `uv run` and `bash -lc`
with modules are all legitimate, and shell hides the rest behind variables,
functions, wrappers and namespace changes. A submit-host stat is worse than
useless: it is an observed fact about the login node, and concluding from it
that the path resolves on a compute node is exactly the inferred-not-declared
move this system refuses. It fails both ways, accepting a path that exists
only on the login node and refusing one that exists only inside the container.

So the proof is a **canary**: an ordinary cheap Slurm unit that runs the probe
through the same launcher, in the same partition and account, and closes on a
normal predicate receipt. It must be an ancestor of everything it verifies, or
it is not gating anything. A unit may not be its own canary: by the time the
workload fails, the fan-out has already been dispatched.

A canary proves the runtime worked on one node at one time. For a homogeneous
partition on shared storage that is a reasonable declared assurance level. For
node-local paths, heterogeneous partitions, or containers assembled at run
time, use `preflight` and check inside each allocation.

## How big should a unit be?

**A unit is the retry boundary.** A retry starts in a FRESH, EMPTY attempt
directory, so it redoes the whole unit. The quantity that matters is therefore
maximum unrecoverable work, not unit size, and it is the human's to set: what
is the most work you are willing to repeat after one interruption?

Absent an answer, the conservative default is one independently executable
shard per unit.

A larger unit is only safe when a fresh attempt can find and validate durable
progress from a previous one. A checkpoint counts only if it survives the
failed attempt, is made available to the new attempt, has an atomic completion
marker, is validated before reuse, actually causes completed work to be
skipped, and still yields the complete declared outputs. An engine that
"can resume in principle" does not count, and `retry.mode: "resume"` is
REFUSED today because that handoff is not built.

Declare the exposure; the coordinator will not guess it:

```json
"retry_limits": {"read_bytes": 100000000000},
"units": [{"id": "hash-00", "max_attempts": 3,
           "retry": {"mode": "restart",
                     "max_lost": {"read_bytes": 97559511040}}}]
```

`max_attempts` defaults to **1**. Repetition is opt-in, because a silent
default of 3 multiplies exposure by three without anyone asking for it.

Nothing infers exposure from a partition name, a walltime, `gpu_hours` or DAG
fan-out. None of those establishes how much work is lost, and a warning built
on them cries wolf until somebody switches it off.

**Splitting has a cost of its own.** Sixteen individually recoverable shards
are also sixteen simultaneous readers of a filesystem other people share.
Bound it:

```json
"limits": {"max_running": 8, "pools": {"shared-fs-read": 2}}
```

Counted across every live attempt, not per invocation: `--max-new-dispatches`
bounds one run, and cron starts another.

**Presentation is separate.** Keep the readable five-stage DAG in `PLAN.md`;
execution granularity does not have to match how a human reads the work.

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

"Nothing else may write here" is a **trusted-writer convention, not an OS
boundary.** `mkdir(exist_ok=False)` enforces that no two attempts are ever
handed the same root; it does not keep another process running as the same Unix
user out of one. The receipt says so in its `basis` block
(`os_enforced_isolation: false`, `attribution_by_observation: false`) and every
report reprints it. Do not restate the predicate without that clause -- this
project has claimed more than its mechanism establishes three times, and each
time the claim was the part that got copied forward.

Shreshth's repo had this all along: Paseo gives each agent its own git worktree,
so his cheap `--base` predicate is conclusive because nothing else writes that
tree. He never solved attribution; an exclusive namespace made it unnecessary.

**Exactly one property does the isolating: the exclusive write root.** The pinned
commit, immutable spec and versioned environment are immutability of INPUTS --
they matter for reproducibility and ownership, never for making the predicate
conclusive. Keep that distinction: when a new expensive check is proposed, ask
"does write-root isolation already make this conclusive?" If yes, delete it.

## The authority path, and the two limits it does not close

A predicate is only as good as the channel that reports it, so: **the launch
record on disk and the attempt receipt are audit-only.** Authority lives in
coordinator state, outside the operated repository and outside the attempt
directory. The launch snapshot is captured there before the spawn and handed to
the separate checker as `--launch-facts`; the checker reports its result back
over an anonymous file with no live alias on fd 0, 1 or 2, and `unit.run`
REFUSES any `pass_fds` entry below 3 at the API boundary, so the
result-file-lands-on-fd-0-and-is-inherited-as-stdin mistake is unrepresentable
rather than avoided by convention. Rewriting `launch.json` changes no judging
input, and `tests/test_record_is_not_authority.py` says so. That cost six
review rounds, because every earlier version established the authority path by
DISTINGUISHING it from the agent's path, and each distinguishing property in
turn was one the agent shared.

Two limits are declared and NOT closed. Neither is a defect. They are written
here, rather than only in the plan, because a reviewer filed the first as a
CRITICAL and was right that it belongs in the threat model, and because a limit
recorded nowhere a reader will meet it gets read as closed.

**Same-UID process access.** A descendant running as the same Unix user can
reach the checker's `/proc` entries or `ptrace` it and obtain the authority
descriptor. Nothing here defends against that, and no arrangement of files
can: modes, Git's own locks and descriptor discipline are all undoable by the
same principal, and even a separate clone is writable by it. Closing it
requires separate Unix identities, a container, or an equivalent OS boundary.
What IS defended is an agent that fails to do the work, and an operator who
runs the wrong thing. So the scope is explicit: if you are running this
somewhere the agent is a real adversary rather than a careless one -- an
account shared with someone you do not trust, or untrusted input steering a
full-access agent on the coordinator's own machine -- you are outside the
threat model, and none of the above helps you. Judged out of scope on purpose;
revisit deliberately, not by discovering it.

**Process-tree quiescence.** Neither Slurm's terminal accounting nor a
terminal-or-idle Paseo lifecycle exposes one portable handle proving that every
same-UID descendant of the job or the agent is dead, so a lingering background
process can still touch output files while the digest is being computed. The
digest binds the bytes of the receipt that check produced; it does not
establish that nothing else moved during or after the check. So quiescence is
not claimed anywhere. It is RECORDED next to the receipt seal on every accepted
receipt (`attempt_receipt_provenance_limits` in coordinator state), which is
the honest state of it. Closing it needs a barrier that holds for a Paseo agent
and a scheduler job alike; a process group covers the scheduler job and not the
agent, a cgroup barrier needs the scheduler to expose one, and a mechanism that
works for one kind of unit while quietly doing nothing for the other would read
like a closure without being one. One reviewer treats this as a precondition
for the result channel being authoritative at all. Read it that way if your
units leave daemons behind.

Per-attempt Git worktrees sit in the same position, one level down. They
isolate PATHS, which is what stops two ordinary agents and a human checkout
from colliding by accident; they share one ref directory, so they do not
isolate principals. `worktree.py`'s `WORKTREE_REF_ISOLATION_LIMIT` states that
at the judgment boundary, and judgment therefore checks branch, descendant
history, changed tree and a clean index together rather than trusting the path.

## Usage

```bash
U=skills/hanig-swarm/scripts/unit.py

# 1. allocate an exclusive write root (prints the path on stdout)
D=$(python3 $U allocate --root /external/swarm-runs --task align-reads --kind slurm \
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

**To move a job that is ALREADY QUEUED to another partition, update it in
place. Do not cancel and redispatch.**

```bash
scontrol update JobId=187196 Partition=cpu    # same job id, same binding
```

The job id survives, so the attempt's binding survives with it and `check`
goes on answering about the same job. Cancel-and-redispatch buys two problems
instead: the new job id is bound to nothing, and redispatching means editing
the unit's `sbatch` flags, so the plan digest no longer matches and the next
`advance` refuses to move until it is given `--accept-plan-change`. One
command against a cancellation plus an override -- take the command.

Better still, do not pick the wrong partition: `survey.py` reports
`allow_accounts`, `qos_grptres` and `max_mem_per_cpu_mb` per partition, which
is where a partition your account may not use is caught before dispatch
rather than after a queued job has to be moved.

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

## converge.py: did it converge, or just stop?

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

### Declaring it in a plan, so the coordinator applies it

The command above is the hand tool. A `slurm` or `pipeline` unit that declares a
`converge` block is judged by it automatically, and the verdict gates `DONE`:

```json
{"id": "train-seed-0", "kind": "slurm", "command": "...",
 "outputs": ["metrics.jsonl", "ckpt.pt"],
 "converge": {"metrics": "metrics.jsonl",
              "criterion": {"metric": "val_auroc", "mode": "max",
                            "threshold": 0.78, "min_steps": 10000},
              "diverge": [{"metric": "train_loss", "above": 1000}],
              "budget": 40000}}
```

- Anything other than `CONVERGED` makes the unit **`NEEDS_HUMAN`**, not `DONE`.
  It closes no ticket, satisfies no dependent, and cannot be promoted. It is not
  `FAILED`, because the command did not fail: extending the budget, changing the
  recipe, or accepting the checkpoint anyway are decisions with a cost, and a
  coordinator that guessed among them would either burn a second full run or
  quietly accept a bad model. It also stays out of the retry path.
- `converge.metrics` must ALSO be a declared output. The gate is conclusive only
  over a file inside the attempt's exclusive write root, and only a declared
  output is checked to be there at all -- otherwise a run that produced no
  metrics reads as "cannot judge" instead of "produced nothing".
- The criterion is read from the PLAN, whose digest is frozen, and never from
  the attempt directory. The unit spec lives inside the write root the job
  writes to, so reading it from there would let a run rewrite the standard it is
  judged against.
- The whole block is validated by `swarm.py validate`, before anything is
  dispatched. A typo'd criterion key is refused rather than defaulted, because
  finding it when the job is finished means the GPU-hours are already spent.
- Declaring nothing changes nothing: a unit with no `converge` block never
  reaches this code. A `code` unit may not declare one -- it has no metrics
  series and is closed by a merged pull request.

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
| two controllers both dispatch a unit | an OS advisory lock (`flock`) on the state dir; the kernel frees it when the holder dies, so there is no TTL and nothing to steal | 8 concurrent advances x 3 trials on each of lambda, chimera and andromeda: one dispatcher every time |
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
