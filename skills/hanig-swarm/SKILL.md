---
name: hanig-swarm
description: >-
  Coordinate a swarm of agents to build projects autonomously on Slurm clusters.
  Use when dispatching, isolating, tracking or judging units of autonomous work
  across lambda/andromeda/chimera: a single Slurm job, a Nextflow or Snakemake
  pipeline, or a code-editing agent. Allocates an exclusive per-attempt write
  root so a cheap done-predicate is conclusive, binds a scheduler job to an
  attempt, and reports DONE / RUNNING / FAILED / PREEMPTED / INCOMPLETE. Use
  with the paseo skills for agent lifecycle; a code unit gets a per-attempt
  worktree and is judged here.
---

# hanig-swarm

## Host capability boundary

Read the project instructions exposed by the current host before acting.  This
skill's Markdown can be loaded by another host, but that does not make its
Paseo/Slurm tools, worker provider, credentials, or approvals available there.
Use only capabilities actually present in the session; report missing Python,
Git, scheduler, Paseo, or bus capabilities with the bounded fallback described
in `docs/agent-compatibility.md`.  Never relax coordinator/worker credential
containment, copy credentials, or infer that a host such as OpenCode is a
supported worker backend.

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

**That argument has a premise: the artifact was not already there.** "Found in
an exclusive root, therefore produced here" holds only if the root was empty of
that artifact when the attempt was dispatched, and nothing checked it until B1.
Post-hoc observation cannot tell an input from an output: a unit that declared
its input path as its output recorded a file it never wrote as produced
evidence and read DONE. So the coordinator digests every declared artifact
BEFORE dispatch, pins the result per ATTEMPT in its own state, and the checker
refuses DONE for any declared artifact identical to that digest. A missing
digest is a refusal, never a fresh look -- a digest taken afterwards is a
digest of the run's own output. This is not the attribution machinery the drift
guard forbids: it never asks which process wrote a file, only whether the thing
digested first differs now, which is the question `judge_detail` already asks
of a repository.

**Its costs, stated rather than discovered.** Three, in order of how likely
you are to meet them.

- **An attempt dispatched before this existed has no basis**, so it can never
  reach DONE. Re-dispatch it. There is no recovery path on purpose: the only
  basis recoverable now would be a digest of what the run already wrote.
- **An artifact over the 256 MB digest limit is compared by size and mtime**,
  because hashing a 40 GB checkpoint on every check makes the predicate too
  slow to run and a predicate nobody runs prevents nothing. A same-length
  rewrite inside one second is invisible to it. The receipt records which
  method was used per artifact and names, in its notes, every artifact whose
  production rests on the weaker comparison.
- **A byte-identical regeneration reads as no production.** Unchanged and
  not-produced are indistinguishable from outside the run, and escaping that
  would need the tool to digest immediately before and after the command it
  launched -- which the coordinator does not launch; Slurm and Paseo do. In
  practice this is nearly unreachable, because a coordinator-allocated write
  root is empty at dispatch and an absent artifact is the passing case. It
  bites only when something put the artifact there first, which is exactly
  the condition this gate exists to catch.

## The authority path, and the two limits it does not close

A predicate is only as good as the channel that reports it, so: **the launch
record on disk and the attempt receipt are audit-only.** Authority lives in
coordinator state, outside the operated repository and outside the attempt
directory. The launch snapshot is captured there before the spawn and handed to
the separate checker as `--launch-facts`, and the pre-dispatch artifact digest
travels the same way as `--artifact-basis` for the same reason -- it decides
admission, so a copy the judged party can rewrite would not be a baseline. Both
are pinned per ATTEMPT: `produced_head` was a unit-level scalar that was never
cleared, so a retry inherited the previous attempt's commit, and a unit-level
artifact digest would have been that bug a third time. The checker reports its
result back over an anonymous file with no live alias on fd 0, 1 or 2, and
`unit.run`
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

## What isolates a code unit

The paragraph above says what a per-attempt worktree does NOT do. What it
does is the whole isolation story for a `code` unit, and it is worth stating
positively, because the reading it displaces -- that `write_scopes` confines
an agent's writes -- was in these docs and is wrong.

**`write_scopes` is a planning declaration, not a boundary.** It names paths,
`validate` refuses concurrently runnable units whose scopes overlap, and
nothing at run time stops any process writing anywhere its Unix user can. It
constrains the PLAN, not the agent, and it never isolated a code unit's
repository. C11 is what does that.

**Each code ATTEMPT gets its own worktree, cut before the agent exists.** The
coordinator records a launch intent in coordinator state first -- repo, base
commit, base tree, the generated `swarm-<attempt>` branch, the worktree slug,
and the plan's `target_branch` -- and only then has Paseo create the checkout
with `--new-workspace worktree --worktree-mode branch-off --new-branch
<branch> --base <base-commit>`. The base is a commit id, never a ref: cutting
from a ref would let the ref move between the decision and the creation,
which is the shared-checkout TOCTOU this replaced. `--cwd` names the trusted
SOURCE repository only, so Paseo knows what to branch from; the agent's cwd
is the path Paseo returns.

Two consequences at planning time. The per-unit `branch` field is vestigial
and is NOT read as a fallback -- a code unit declares `target_branch`, the
destination of its pull request. And two concurrent code units on one
repository need neither separate branches nor `needs` between them, because
they no longer share a checkout.

**The returned path is verified from Git, then bound by inode.** Nothing
trusts Paseo's notice: the path must be a worktree root, its
`--git-common-dir` must be the trusted source repository's, its `--git-dir`
must DIFFER from that common dir or it is the main checkout rather than a
linked per-attempt worktree, HEAD must be the recorded base commit, and the
branch must be the recorded one. Only then are the worktree root's device and
inode, and those of both Git metadata directories, written into the launch
facts. At judgment `workspace_identity_problem` re-resolves and re-stats all
three and refuses if any of them moved, which is what closes whole-directory
substitution under the same path. Launch records written before those fields
existed keep the weaker path-only check: the absence of a field that did not
exist yet is unverifiable, not evidence of substitution.

**Inode equality is not content tamper-evidence, and is not claimed as it.**
HEAD, the index, refs and objects must all change for an honest commit, so no
launch-time snapshot of them separates work from interference. That is why
judgment checks their semantics together -- expected branch, descendant
history from the recorded base, a changed tree, and a clean index and
worktree -- rather than checking that nothing moved. After judgment the
coordinator pins the produced commit, so a later ref rewrite cannot change
merge admission or verification.

The launch record is per ATTEMPT, `launch-<attempt-id>.json`, written beside
the attempt directory rather than inside it. One shared record let a retry
inherit the previous attempt's baseline, so the previous attempt's commits
satisfied the new attempt.

## Three more limits, recorded where a skill reader never looks

Same shape as the two above, and here for the same reason. Each was accepted
deliberately and written down in a code comment or in `MEMORY.md`, neither of
which is a file a reader of this skill opens. None is scheduled work; two are
safe only because of a restriction nothing enforces, so the restriction is
stated with them.

**A dispatched agent DOES hold credentials.** `child_environment.py` stops the
COORDINATOR handing its own authority to a child: an exact-name denylist
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, the GitHub tokens, the AWS set,
`SSH_AUTH_SOCK`, and `SBATCH_GET_USER_ENV`, which would otherwise let sbatch
reacquire the login environment from the far side) is dropped from every
coordinator spawn, and `SWARM_UNIT_*` / `SWARM_DEP_*` are never inherited, so
a constructed variable cannot be spoofed by the ambient environment. Do not
read that as an agent running without credentials:

- Paseo's long-lived daemon supplies provider credentials to the agent
  independently of the short-lived client the coordinator spawns. A live
  probe showed a key reaching an agent while absent from that client's
  environment, and `paseo run --env` cannot replace those provider
  variables. The daemon must itself be launched without ambient keys, or the
  containment does not exist; it is not a swarm-side boundary.
- `HOME` passes through, because a code agent cannot work without it, and
  `HOME` is where codex's stored auth lives. The denylist covers process
  environment names; it is not a filesystem boundary.
- The list is finite and EXACT on purpose. A credential under a name nobody
  enumerated, or embedded in another variable's value, passes through
  untouched. Two rounds of name- and value-shape matching broke legitimate
  runtime configuration -- `HF_TOKEN` under a second name is the standing
  example, and a gated model will not download without it -- so exactness is
  the policy rather than an omission.

The provider credentials held by Paseo start the selected worker; they are not
a grant for that worker to call further models from inside its task.
`models.json` is routing metadata, not a credential grant. In particular,
`OPENAI_API_KEY` and `OPENROUTER_API_KEY` remain coordinator-side for
`review.py` and `committee.py`; every coordinator child loses both exact names.
Other-model work therefore belongs in those coordinator-side review and
committee paths. Changing that reach requires a deliberately designed proxy or
named credential exception -- never quietly deleting either name from the
denylist.

This is not a defect to re-file. A code unit is REQUIRED to push a branch and
open a pull request, so an agent holding nothing could not close its own
unit. The true claim is narrower than "the agent holds no credentials": the
coordinator no longer hands over the credentials IT holds, and the agent
still acts with whatever the Paseo daemon gives it and whatever is readable
under `HOME`. Accepted 2026-09-02; closing it is a Paseo change, not a swarm
one.

**Adopting a pre-existing worktree races a same-UID process.** If a
controller dies between `paseo run` and recording its result, the branch and
the worktree may already exist. Recovery looks for the named agent first;
failing that it will adopt exactly ONE unambiguous Git worktree on that
branch, and before it does, `_paseo_path_ownership_problem` asks Paseo's
workspace registry and then its agent registry whether anything already owns
that path, treating an unreadable registry as ownership-unknown rather than
as absence. That reduces ACCIDENTAL adoption, which is what it is for. It
does not close the race: Paseo has no conditional reserve-and-launch
primitive, so a same-UID process that registers the path after both list
calls still wins, and re-checking would only move the gap. What keeps
adoption safe is the check AFTER the lists -- the path must still match the
trusted launch intent by repo, base commit, branch, worktree root and Git
metadata inode, or it is refused rather than adopted. So do not replace the
list calls with a lock and read the race as closed: closing it needs Paseo to
reserve and launch atomically, or a different OS identity boundary.

**`_paseo_workspace_id` parses free text, and only bookkeeping may depend on
it.** The workspace id is recovered by matching `Created workspace wks_...`
in Paseo's human-readable notice. It is used for cleanup -- archiving the
managed checkout, naming a retained worktree a human has to remove -- and
NEVER to authenticate the returned cwd, branch, base or Git identity, each of
which is re-derived from Git and checked against the recorded launch intent.
That restriction is what makes a free-text parse acceptable, and nothing in
the code enforces it: a later caller could read the same string into a trust
decision, and hardening the pattern would not help, because a same-UID
process can falsify the registry evidence behind it anyway. If you need to
know which workspace an attempt owns for anything that DECIDES something,
take it from the launch intent.

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
| `code` | per-attempt git worktree, inode-bound | lifecycle settled + outputs + a committed change over the base; a merged PR closes it |

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

The six things that make this safe, each proven by breaking it -- and read
the lock row's scope, which is narrower than the row used to say:

| hole | guard | proven |
|---|---|---|
| two controllers both dispatch a unit | an OS advisory lock (`flock`) on the state dir; the kernel frees it when the holder dies, so there is no TTL and nothing to steal -- **on a local filesystem** | 8 concurrent advances x 3 trials on each of lambda, chimera and andromeda: one dispatcher every time. All SAME-NODE, which is what a local `flock` already guarantees |
| crash between `sbatch` and bind | jobs named `swarm-<attempt>`; `reconcile_orphan` asks squeue/sacct | job id wiped from state on a LIVE job; advance recovered 187880 and did NOT resubmit |
| `INCOMPLETE` forever | terminal `FAILED_EVIDENCE` after a 600s settle window; holds dependents | unit test |
| plan edited mid-flight | canonical digest over dispatchable fields; refuses to advance | unit test |
| two STATE DIRECTORIES dispatch one unit, or two units into one output path | a claim directory per declared output destination under `<root>/.output-claims`, `mkdir(exist_ok=False)` as the exclusivity; a foreign claim is released only when `squeue`/`paseo` positively says the attempt is gone | unit test. **A `squeue` that is absent or fails reads `unknown`, and unknown refuses** |
| an agent runs `git stash` in one of several worktrees of one repo | the dispatched completion protocol forbids it and names the substitutes in the same breath; dispatch refuses a code unit whose source checkout has a non-empty stack | unit test. Field: three agents, three worktrees, one window, each `pop` took another's entry |


**The lock's declared limit, which the table above used to talk past.**
`swarm.py`'s `_hold_state_lock` records it: **NFS lock recovery.** If the
server reboots, or evicts this client's lock state after a partition, the lock
can be dropped while this process is still alive, and another controller can
then acquire it. Nothing in local state can notice, because the kernel does
not tell us. A lock cannot be made stronger than the lock manager underneath
it, so do not add a heartbeat or a TTL -- a TTL reintroduces exactly the
stealing this design avoided.

**On our three clusters this limit is LIVE, not theoretical.** Measured
2026-09-03:

| host | `$HOME` | filesystem |
|---|---|---|
| lambda | `/home/hani` | nfs |
| chimera | `/home/hani` | nfs4 |
| andromeda | `/mnt/weka/home/hgoodarzi` | wekafs |

`XDG_STATE_HOME` is unset on all three, so the default state directory falls
through to `~/.local/state` (`coordinator_paths.py`) -- confirmed nfs on lambda
and chimera. The lock has been living on a network filesystem on every machine
this skill is for.

The trials in the row above were also same-node, which a local `flock` handles
whatever the backing store is. Two controllers on two different nodes sharing
the filesystem is outside the tested topology and would need a cross-node test
to certify. So the evidence establishes same-node exclusion, collected on the
filesystem where the guarantee is weakest.

**The output claims' declared limits, in the same spirit.** The lease excludes
two controllers over one STATE DIRECTORY. It says nothing about two state
directories over one output namespace, which is what three ad-hoc sub-plans of
one project produce -- and that is how one unit came to be dispatched twice
into identical output paths. The claim registry closes that, with three limits
worth knowing before you rely on it:

- It is **not an `flock`**, and it cannot be. `advance` dispatches and exits
  while the job runs for hours, so a lock tied to the coordinator's lifetime
  would be released seconds after the writer it protects started. The claim is
  a durable directory, and staleness is settled by asking the scheduler, never
  by a timeout. A coordinator frees its own claims -- including ones it left by
  dying -- from its own state file; it can never free anybody else's.
- It lives **under the run root**, so it is shared by exactly those
  coordinators that share a run root. That covers the reported case, because
  the default root is derived from the project checkout. Two coordinators
  pointed at different `--root` values do not see each other's claims.
- **A `squeue` that cannot be asked refuses.** Absent, failing, or a kind with
  no registry to ask all read `unknown`, and unknown is not free. That is a
  deliberate trade: it can block a unit whose job really did finish, and the
  refusal names the exact claim directory to remove. Reading silence as
  absence would instead start a second writer in a live namespace, which is
  the failure the whole mechanism exists for.

**Why the protocol forbids `git stash` and what it offers instead.** The stash
stack is a SINGLE ref in the repository's shared common Git directory, so it is
not per-worktree: every worktree of one repo shares one stack. Three agents in
three worktrees each ran `git stash -u` inside one window and each `pop` took
another's entry; it was recovered from dangling commits and cost real time. All
three were checking whether a red test pre-existed their change, which makes it
a protocol defect rather than three mistakes. So the dispatched protocol bans
it and names the substitutes in the same sentence -- `git show <base>:<path>`
to read a file at base, `git diff > /tmp/wip.patch` plus `git checkout --
<path>` to set work aside, and a separate worktree at the base commit for a
real comparison -- and tells the agent to read `git status --porcelain` for
foreign paths before every commit, because the observed damage was a commit
carrying another agent's files.

What that comparison does NOT show is worth saying, because it was learned the
same week: two branches can merge cleanly in text and not in meaning. Green at
base and green in your worktree is a claim about your change alone, not about
the integration branch after a merge. Only running the suite on the merged tree
answers that.

If you need the guard to hold, run **one coordinator per plan from one node**,
or put the state dir on local disk -- the fallback chain already ends at
`$TMPDIR/hanig-swarm-state`, and `/tmp` is ext2/ext3 on lambda and chimera.

Read the second option carefully before taking it. Local state removes the
hazard by construction, because two nodes cannot share a lock they cannot both
see -- but it also removes the sharing the lock existed to arbitrate. Any login
node being able to `advance`, and cron advancing unattended, both need the
state reachable from wherever the coordinator runs. On andromeda `/tmp` is
overlayfs, so it is node-local AND ephemeral in a way the other two are not.
There is no setting that is right everywhere; pick per host and write down
which you picked.

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
