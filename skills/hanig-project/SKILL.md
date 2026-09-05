---
name: hanig-project
description: Start a swarm project on this machine, or adopt a half-finished repo into one. Surveys the host and the code first, interviews the human only about what inspection cannot answer, writes a plan whose units have declared outputs, files a Linear project and issues after ONE approval, then dispatches and tracks it. Use when asked to start a project, set up work on a server, plan a repo, turn a repo into tickets, or file issues from a plan.
---

# The front door

## Host capability boundary

Follow the active host's discovered project instructions and approval policy;
`AGENTS.md`, `CLAUDE.md`, and `MEMORY.md` remain project context rather than a
claim that a particular host has loaded a particular tool.  For tracker work,
use only the current session's real connector and operation names.  If none is
available, preserve the reviewed draft/outbox intent and report pending
synchronization; do not invent a receipt, ticket reference, or confirmation.
Installing this skill never provisions an account, copies credentials, changes
permission/model defaults, or grants a worker the coordinator's authority.

You are on a server. There is either an empty directory and an intention, or a
half-finished repo and a vague plan. The job is to get from there to units of
work that are dispatched, tracked, and verifiable, without asking the human
anything a machine could have looked up.

Run in this order. Each step's output is the next step's input.

## 1. Survey. Never ask what you can read.

```sh
python3 scripts/survey.py --repo . --out .swarm/survey.json
```

This reports the host, python, schedulers, partitions, accounts, whether
`--mem` is required here, free disk, and, for a repo: git history, size,
language mix, existing docs, and whether a swarm project already exists.

The tree walk is bounded by a **child process killed on a real deadline**,
not by a check inside its own loop: `os.scandir` blocks inside `opendir()` on
a stale NFS handle or a dead automount, and a loop that only looks at the
clock between entries never gets its turn. So `repo.walk` reports how the
walk ENDED -- `complete`, `truncated` (a declared cap stopped it: there was
too much), `stuck` (a directory did not answer, and `stuck_at` names it), or
`unknown`. **`truncated` and `stuck` are not the same fact**: the first
describes the tree, the second warns about the host. On anything but
`complete`, `file_count`, `size_mb` and `extensions` are FLOORS -- if it comes
back `stuck`, say so and name the directory instead of planning off the count.

Per partition it also reports **who may use it and what the allowance costs**:
`allow_accounts`, `deny_accounts`, the partition `qos` and its `qos_grptres`,
and `max_mem_per_cpu_mb`. Read these before sizing anything. Each carries
`state` = `set` | `unrestricted` | `unknown`, and **`unknown` is not
`unrestricted`** -- it means the query did not answer, so say so instead of
planning around it. Two failures paid for this: hours lost to
`QOSGrpCpuLimit` on a 736-CPU partition sitting 202 CPUs idle, because nothing
said which accounts were allowed in it; and `MaxMemPerCPU`, which fixes the
CPU count a memory request costs and then refuses the job by naming CPUs
rather than memory -- 700 GB at `MaxMemPerCPU=5120` costs 140 CPUs, not the 32
that were asked for.

Read it before speaking. Every fact in it is a question you must not ask.
If the repo is half-finished, also read its README, CONTEXT.md, MEMORY.md and
any `docs/adr/*`, and skim the last dozen commits. The point is to arrive at
the interview already knowing what was built and roughly why.

## 2. Grill. One question at a time, each with a recommended answer.

Invoke `grill-with-docs` if it is installed; otherwise follow its rule
directly, which is the rule that matters:

> If a question can be answered by exploring the codebase, explore the
> codebase instead.

### Where code lives: decide from the survey, not from the human

The survey already reported whether this directory is a git repo, its remote
and its branch. So this is mostly NOT a question:

| what the survey found | what you do |
|---|---|
| a repo WITH a remote | **Adopt it. Do not ask.** Say which remote and branch you are using, in one line, and move on. |
| a repo with NO remote | Ask once whether the work should be pushed, and where. Recommend staying local. |
| no repo at all | Ask once: does this produce code someone else will run or review? Existing repo, a new one, or nowhere. Recommend nowhere. |

Staying local is a first-class answer, not a fallback. Most compute projects
never need a repo, and the ones that do usually already are one.

The test is not "does this produce code". It is **does anything here need to
outlive the attempt directory as SOURCE**. Compute outputs never do: a
manifest, a checkpoint, a TSV is DATA, it lives in an exclusive write root,
and it reaches a shared path only through `swarm.py promote` with a named
approver. Nothing in this workflow may commit a swarm output to a repo.

Creating a new repo is outward-facing and sits behind the same gate as filing
a tracker project: default is to stop and ask, and `swarm autopilot` in the
request runs it end to end.

### The interview itself

Ask ONLY about judgment: what counts as done, what the scientific claim is,
what may be thrown away, what the budget is, what must never be overwritten,
and **the most work they are willing to repeat after one interruption**. That
last one sets the retry boundary and cannot be inferred from anything; a
planner that invents it produces a readable DAG whose units are larger than
the failures they will meet.
Every question carries your recommended answer so the default costs one word.

**Stop when the plan can RUN, not when you run out of questions.** These are
different, and getting it wrong is expensive: a plan was once built, validated
and had five tracker issues filed for it before anyone noticed the corpus path
had never been asked for. The planner had even written "I'll need the subpath
and the glob" in an earlier answer, and then never came back for it. It sat
waiting for a value nobody had been asked to give.

So before you finish the interview, list every value the plan needs to
dispatch, and check each one is settled. The list is not a matter of taste:

```sh
python3 ../hanig-swarm/scripts/swarm.py schema
```

prints every field, whether it is required for that kind, and what it couples
to. Anything marked required is a refusal already waiting for you, so it is a
question now or a wasted plan later.

- **Inputs**: paths, globs, and any config file the command reads. Empty,
  still a placeholder, or matching nothing are all refused.
- **The partition and the account**, per unit. A partition this cluster does
  not have is refused by name; **the account is NOT checked**, so a wrong one
  survives validation and fails at submission instead.
- **`promote_to`, and who approves it**, for anything that must reach a
  shared path. A unit's `outputs` are relative to its own attempt directory,
  so "where does this end up" is a separate question and `promote_to` is the
  only thing that answers it.
- **`mode` and `provider`** for every code unit. An absent `mode` is refused;
  `provider` is not, because it has a default -- ask anyway, because modes are
  provider-specific and which spelling of `mode` is legal depends on the
  answer.
- **`target_branch`** for every code unit: the branch its pull request merges
  INTO. Not the branch it works on, which the coordinator creates itself per
  attempt. A code unit closes on a merged PR, so a plan that never named the
  destination is refused.
- **The runtime** each unit executes in (resolution, entrypoint, probe) and
  **how it gets verified**. If that is a canary, the canary must declare the
  SAME runtime, run its `probe` command verbatim, match the unit's partition
  AND account, and be a DAG ancestor of it -- so a plan spanning two
  partitions needs one canary per partition, and `runtime: "none"` is a claim
  to make on purpose rather than a way out.
- **The retry contract**, for any unit that may attempt more than once. The
  "most work you are willing to repeat" answer has to land as `retry.max_lost`
  in a metric the plan also caps in `retry_limits`; `max_attempts` above 1
  without that pair is refused.

Two answers the interview still owns, though neither is unchecked any more.
**`--mem`**: `swarm.py validate` refuses a `slurm` unit that requests no
memory when the survey it reads reports `mem_flag_required`. That refusal is
only as good as the survey, and on a host where none was recorded the flag is
yours to get right -- validate says which of the two happened rather than
printing a bare "plan is valid". **`findings.json`**: whether this project
has findings to publish is a judgement no validator can make, so ask. What
validate enforces is narrower and mechanical -- a unit that declares
`findings.json` must also declare `promote_to`, because an output that is
never promoted stays in the attempt's write root, where the report never
looks. Both otherwise fail after the plan validated clean, which is the most
expensive moment to learn anything.

`mode` in particular has to be ASKED. An agent under default permissions stops
at its first write and waits for a person, which is correct behaviour and fatal
to an unattended DAG; a coordinator that silently bypassed permissions on the
human's behalf would be a worse bug than a stalled unit. So the plan must say
what it wants, and the human is the only one who can say it. Discovered any
later, it presents as an agent that runs forever doing nothing.
If a value is still a placeholder, that is a QUESTION, not a detail to settle
later. `swarm.py validate` refuses a plan whose declared inputs are empty,
still placeholders, or match nothing, so this is enforced rather than
remembered -- but reaching that refusal means the interview already failed.

Stop when you can state, without hedging, what each unit must produce AND
what it will read.

### Two questions `sinfo` cannot answer

The survey reports the allowance. It cannot report the judgement, and these
two questions are the judgement. Hours were lost to `QOSGrpCpuLimit` while a
736-CPU partition sat 202 CPUs idle, because nobody was ever asked whether the
lab's own queue was fair game for that work. Ask each with a recommended
answer, and quote this cluster's own numbers back so the human is deciding
about their cluster rather than in the abstract.

**1. May CPU-only work run in the partition your account is exclusively
allowed into?** Recommend YES when the shared queue is capped and the lab's
own partition is idle: idle CPUs beside a GPU you are not using still finish
the job sooner than a queue you are throttled in. Read BOTH sides of the
account rule from the survey before asking, because there are two ways to be
shut out and only one of them is an absent allowance. `allow_accounts` naming
the account is a route in; `deny_accounts` is the other half, since Slurm
prints `DenyAccounts` *instead of* `AllowAccounts`, so a partition that denies
this account reads as wide open if you only look at the allowance. If
`deny_accounts` names it, this is not a question -- say the partition is
closed to this account and move on. If either field reads `unknown`, say that
instead of asking: `unknown` is not `unrestricted`, and a recommendation built
on a query that never answered is a guess wearing a number. `swarm.py
validate` applies the same three-state rule to the plan and refuses a unit
whose `--account` a partition denies, or leaves out of a `set`
`allow_accounts`; it stays silent on `unknown`, so a clean validate is not
evidence that the pairing was checked.

**2. Is the per-job footprint still acceptable once `max_mem_per_cpu_mb` is
applied?** Recommend the size the limit actually charges, and shrinking the
per-job memory until that number is one the account can hold. A set
`max_mem_per_cpu_mb` does not cap memory; it fixes the CPU count a memory
request costs. Slurm charges `ceil(mem_mb / max_mem_per_cpu_mb)` CPUs and then
refuses the job by naming CPUS, not memory, so the error points away from its
cause. This is not hypothetical: 700 GB at `MaxMemPerCPU=5120` costs 140 CPUs,
not the 32 that were asked for. The survey has already done that arithmetic in
`limits_note`, so quote the CPU number this cluster will charge and ask whether
the unit still fits the budget and the queue -- not whether the memory figure
looks reasonable, which it always does.

**Carry this caveat into both answers, and do not let it drop quietly:**
`qos_grptres` resolves the PARTITION QOS only. An account or association QOS
can impose a `GrpTRES` the survey never sees, which is a second route to the
same `QOSGrpCpuLimit` that cost those hours. So `unrestricted` on that field
is not a promise the job will run -- it says nothing at the partition level
caps you, and nothing more. Say so out loud when you recommend a size, so the
human's answer is given against what is actually known.

## 3. Plan. Units are defined by their OUTPUTS, not their commands.

Write two files. Size units by the retry answer from step 2, not by what
reads nicely: see "How big should a unit be?" in `hanig-swarm/SKILL.md`. A
unit with `max_attempts > 1` must declare a `retry` contract or validation
refuses it.

### The unit contract, by kind. Read this before writing a plan.

Everything that goes wrong here comes from writing a plan as though the
contract lives in `command`. It does not.

| | `slurm` | `pipeline` | `code` |
|---|---|---|---|
| `command` is | the work itself | the engine invocation | **not used** |
| the prompt goes in | n/a | n/a | `prompt`, and only there |
| submitted by | the coordinator, so **never** `sbatch`/`srun` here | the coordinator | `paseo run` |
| configured by | `sbatch` flags | `command` | fields: `provider`, `mode`, `model`, `thinking`, `env` |
| `outputs` are | relative to the run-dir | relative to the run-dir | relative to the run-dir |
| judged by | Slurm accounting + declared outputs | launcher exit + declared outputs | agent lifecycle + outputs + a produced commit |
| closed by | a predicate receipt | a predicate receipt | a **merged PR** |
| also needs | `--mem` if the survey says so, and an `--account` the `--partition` accepts | a fresh work and publish dir | `repo`, a `target_branch`, and an explicit `mode` |

Three of those cost a full dispatch cycle each to learn, so they are worth
reading twice:

**`outputs` are relative to the run-dir, for every kind.** The done-predicate
looks inside the attempt's exclusive write root and NOWHERE else, so an
absolute path elsewhere is unfindable by construction: the work can succeed
completely and the unit can never close. Use `$SWARM_UNIT_DIR` if a tool needs
an absolute path, and `promote_to` to publish somewhere shared.

**A `slurm` command is the work.** `sbatch --wrap='...'` nests one job inside
another: the outer job queues an inner job nothing is bound to and exits in
00:00:00, and Slurm reports COMPLETED with ExitCode 0:0 for work that never
ran. Scheduler flags go in `sbatch`.

**A `code` unit's `prompt` is a prompt.** It becomes the last positional
argument to the agent runner, so a flag written into it is not configuration,
it is a sentence the agent is asked to read. `provider`, `mode`, `model`,
`thinking` and `env` are fields on the unit.

The default agent is **`codex/gpt-5.6-sol` at `thinking: high`**, the strongest
one available locally. Override per unit with `provider`, `model` or
`thinking`; set `thinking` to null for a provider that has no such option.
`mode` has no default and `validate` REFUSES a code unit without one, because
an absent default is otherwise a decision nobody made: unattended, an agent on
default permissions stops at its first write and the unit runs forever doing
nothing. Say `"mode": "full-access"` for unattended work on the default codex
provider, or `"mode": "default"` to accept the stall deliberately. Modes are
provider-specific: `bypass` is claude's word and codex rejects it, so the mode
has to follow whichever provider the unit names. A code unit also cannot omit `repo`: it closes
on a merged PR, so with no repository there is nowhere to open one from.

`validate` refuses all three. Reaching one of those refusals means the plan was
written from the wrong model of what a unit is, which is what this table is
for.

**Read the field reference before writing units, not after being refused:**

```sh
python3 ../hanig-swarm/scripts/swarm.py schema
```

Every field, whether it is required for that kind, and what it couples to.
Refusals teach one rule at a time by construction, so learning the shape from
them takes as many dispatch attempts as there are coupled rules.

**A `slurm` unit may not combine `--array` with declared outputs.** Every array
task shares the unit's ONE attempt directory, so the first task to finish
writes the artifacts record over a partially complete result and the unit reads
DONE on a fraction of the work. A dry run will not show it, because a dry run
does not fan out. Make each shard its own unit, or have the array write
per-task paths and declare a separate merge unit that produces the outputs.

`plan.json` for the coordinator. Every unit needs `id`, `kind`, `command`,
and **`outputs`**. Add `needs` for dependencies, `gpu_hours` for the budget,
`write_scopes`, and this cluster's own `sbatch` flags (the survey told you the
partitions and whether `--mem` is required).

**`write_scopes` does not isolate a code unit.** It reads like an isolation
mechanism and it is not one: it names FILES, and it DECLARES where a unit
intends to write. What the declaration buys is a refusal -- `validate` rejects
two units that can run concurrently and whose scopes overlap, because two
units that may write the same place cannot both have an exclusive write root,
and that exclusivity is what makes the done-predicate conclusive. Give them
disjoint scopes, or order them with `needs`.

The isolation for a code unit is the **per-attempt Git worktree**: every
attempt gets its own checkout at the anchored base commit, verified and bound
by inode, so two code units no longer share one working tree. That is why
concurrent code units MAY share a pull-request target -- a constraint the
earlier design needed and this one does not.
`TestPerAttemptBranchesRemoveTheSharedCheckoutConstraint` in
`tests/test_plan_shape.py` pins it. Worktrees isolate PATHS and not
principals; `hanig-swarm/SKILL.md` states what that does and does not defend
against.

A code unit closes on a MERGED PULL REQUEST (step 6), so it must name the
branch the request merges INTO. `validate` refuses a code unit that has a
`repo` and no `target_branch`: the coordinator creates the source branch, but
it cannot open a mergeable pull request without the plan naming the
destination, and the unit is then structurally unclosable -- it will dispatch,
run, be judged, and never reach DONE. The legacy field `branch` is NOT a
fallback, and a plan that names it instead is told so.

`PLAN.md` for humans: what is being built, what was decided in step 2 and by
whom, and what is deliberately out of scope.

**Never overwrite a PLAN.md you did not write.** On the adopt path a repo may
already have one, and it may be a large design document. The survey reports
`protected_docs` for exactly this: if `PLAN.md`, `MEMORY.md` or `README.md`
already exist, write yours to `.swarm/PLAN.md` instead and say so. Destroying
the document that explains the project you were asked to adopt is not a
recoverable mistake.

Then, always:

```sh
python3 ../hanig-swarm/scripts/swarm.py validate plan.json
```

It refuses a partition this cluster does not have, so a plan written for
another server fails here in one line instead of half-dispatching.

## 4. Tickets. Draft everything, create on ONE approval.

```sh
python3 scripts/tickets.py draft plan.json --brief brief.json
```

This writes `tickets.json`. It talks to nothing: the coordinator runs on a
shared login node and a tracker token must not live there.

**Team: `Arc`, and do not ask.** The workspace is `Arc - projects`; its teams
are `Arc`, `peeks` and `SRAgent`, and cluster and lab work goes to `Arc`
alongside Entwine, CSA-RNA-FM and MultiDep. `tickets.py` defaults to it, so
this is not a question for the human. Override only if told to, with
`--team`.

A plan's `charge_to` is a SLURM ACCOUNT, not a team. They look alike and are
different namespaces: filing under `goodarzilab` fails, because no such Linear
team exists.

**THE DEFAULT IS TO STOP HERE.** The draft carries
`approval.state: "required"`, and while it says that, you may not create
anything. Filing a project is outward-facing: other people see it and undoing
it is manual.

The human clears it by saying so, and you record that:

```sh
python3 scripts/tickets.py approve tickets.json --approver <name>
```

**One phrase skips the gate for a whole run: `swarm autopilot`.** If the
request contains it, pass `--autopilot` to `draft` and go end to end without
stopping. Nothing else counts: not "yes", not "go ahead", not "sounds good".
Those appear in ordinary conversation and would make the gate meaningless.
An approval already granted is not re-requested when the draft is rebuilt.

Then, in this session, holding the Linear MCP connector:

1. Show the human the project and EVERY issue title in full, and the count.
   Not a summary. The titles are what they are approving.
2. Ask once. Not per issue.
3. On yes: create the project, then each issue, writing each returned id back
   into `tickets.json` (`project.linear_id`, `issues[].linear_id`,
   `issues[].identifier`).
4. Re-running later UPDATES rather than duplicating, because the draft carries
   the ids forward keyed on unit id.
5. Apply the DAG: for each issue, add every `add_blocked_by` relation, and
   **call `removeBlockedBy` for every `remove_blocked_by` entry.**
6. Read the edges back and hand them to the next draft, below. Applying
   without doing this leaves the next run unable to say whether the write
   landed.

### The DAG is append-only, so removal has to be asked for

`blockedBy` is append-only through this interface: Linear exposes
`removeBlockedBy` as a separate operation. So when a unit's `needs` list
SHRINKS, the edge that no longer exists stays in the tracker, and not
re-adding it does not delete it. The tracker then says an issue is blocked by
something the plan no longer says blocks it.

`tickets.py` cannot see that on its own -- it talks to nothing. It decides it
by diffing the plan against what you tell it the tracker holds:

```sh
# In the session with the connector: list every issue in the project with its
# blockedBy relations, and write
#   {"schema_version": 1, "read_at": "<ISO 8601>", "source": "...",
#    "edges": {"<issue>": ["<blocker>", ...]}}
# Each key is the BLOCKED issue; handles may be unit ids, identifiers
# (ARC-236) or uuids. An issue with no blockers MUST appear with an empty
# list: omitting it is indistinguishable from not having looked.
python3 scripts/tickets.py draft plan.json --tracker-edges edges.json
```

The draft then carries, per issue, `add_blocked_by`, `remove_blocked_by` and
`blocked_by_in_sync`, plus a top-level `blocked_by_sync` saying when the
tracker was read and by what.

**A read-back is ATTESTED, not verified**, the same way an outbox receipt is:
there is no network on the coordinator, so what it has is your report of what
you saw. Every rendering says so. It is still far better than diffing against
the last draft -- that only records what was asked for, and would be
confidently wrong the moment an apply half-finished or a human edited a
relation in Linear by hand.

**Without a read-back, `remove_blocked_by` is `null`, not `[]`.** Unknown is
not empty: `[]` would say "the tracker holds no stale edges", which nothing on
the coordinator can know. Once anything has been filed, `check` treats a
missing read-back as drift and exits 2. That is deliberate -- the loop is
apply, re-read, re-draft, check -- and it is what makes this a verification of
the write rather than trust in it.

Verify the two never drift:

```sh
python3 scripts/tickets.py check plan.json tickets.json
```

It reports both halves: the unit/issue mapping, and the blockedBy edges as
read at a stated time. It never says "in sync" without naming that time.

## 5. Dispatch.

```sh
python3 ../hanig-swarm/scripts/swarm.py run plan.json --dry-run   # shape first
python3 ../hanig-swarm/scripts/swarm.py run plan.json
```

Then schedule `advance` (cron or a Paseo schedule). Overlapping runs are safe:
the coordinator takes an OS lock, so a second advance exits without acting.

## 6. Drain. Issue state follows unit state, never the reverse.

The coordinator records tracker intents as units change state:

```sh
python3 ../hanig-swarm/scripts/swarm.py outbox --json
```

In a session with the connector, apply each pending intent to its issue, then
mark it applied. The rules are not negotiable:

- **Nothing closes on a self-report.** A `close` intent carries the unit's
  receipt. An intent without evidence must be REFUSED, not applied.
- **Two kinds of evidence, and they are not interchangeable.** Every intent
  names its `closing_evidence`. A `slurm` or `pipeline` unit closes on a
  predicate receipt. A `code` unit closes on a MERGED PULL REQUEST and never
  on a receipt: its receipt says an agent went idle and files exist, which is
  `open_pr`, not done. Apply an `open_pr` intent by opening or linking a PR
  and leaving the issue open.
- **An issue closed without its authorised evidence is an integrity
  violation.** Say so loudly and reopen it. Never mark the intent applied
  because the issue "looks done": that is the tracker being believed over the
  verdict, which is the whole failure this system exists to prevent.
- A `block` intent means an upstream unit will not complete. Say which one.
- **After the tracker confirms, record what landed**:
  `swarm.py outbox --state-dir DIR --record-receipt KEY --ref ARC-171`.
  Only after it confirms. A false acknowledgment is worse than a missing one,
  because re-draining is safe and un-filing is not. An intent with no receipt
  reads `unacknowledged`, which means this machine has no confirmation either
  way -- it does NOT mean the issue was never filed.
- Draining twice is safe: every intent has an idempotency key.
- A tracker outage must never alter swarm state. The swarm is authoritative;
  the tracker is a view of it.

## Last step, every run: the report

**A run is not finished until it has produced a report.** Not when the last
unit reaches DONE, and not when the issues close.

```sh
python3 scripts/report.py . --out report.html            # standalone
python3 scripts/report.py . --fragment --out frag.html   # to publish
python3 scripts/report.py . --json                       # for a machine
```

Then publish the fragment as an artifact and give the human the link.

It is assembled from `plan.json`, the coordinator state and each attempt's
`receipt.json`. It is NEVER written from your account of what happened: a
narrative summary of a run is the same self-assertion as an agent reporting
"done", and it is inadmissible for the same reason. If a fact is not on disk,
it does not go in the report.

Three sections carry most of the value, and two of them are easy to leave out:

- **Evidence.** Every delivered artifact with the digest recorded when it was
  judged, so the claim stays checkable by re-hashing months later.
- **What this evidence does not establish.** Read from each receipt's own
  `basis` block. A receipt records that isolation is by convention rather than
  OS-enforced, and that nothing observed attributes those bytes to that
  process. A report that prints DONE and drops this has silently upgraded a
  hedged claim to an unhedged one.
- **Findings reported by this project.** Rendered only if the run wrote
  `findings.json`, and labelled as the project's claims, because the
  coordinator cannot verify them: it has no idea what a column means.

**So a project that has findings emits `findings.json` from the unit that
produces them, and publishes it.** Not every project has any: a plan whose
last unit is a training run or a code change has an artifact and a merged PR
to show, not claims about data, and a rule demanding the file from every
terminal unit would refuse honest plans and be routed around with an empty
`{"findings": []}`. So this is the interview's question, not the validator's.

What IS enforced, because it is mechanical: **the unit declaring
`findings.json` must also declare `promote_to`, or `swarm.py validate`
refuses the plan.** Declared outputs live in the attempt's exclusive write
root and the report reads `findings.json` from the project directory, so an
unpromoted findings file is written, digested, and read by nobody -- which
looks exactly like never writing one. Declare it as one of that unit's
outputs, with a promotion destination, in this shape:

```json
{"outputs": ["findings.json"], "promote_to": "/abs/path/to/the/project"}
```

```json
{"findings": [{"title": "one sentence a reader can act on",
               "detail": "the numbers, and what they do NOT prove"}]}
```

State the bound, not the wish. "A clean 3.6% sample bounds the defect rate near
0.15%" is a finding; "no defects found" is a claim the sample cannot support.
Write what was deliberately not done and why, so an omission is declared rather
than discovered later by someone trusting a gap that was never flagged.

## Adopting a half-finished repo

Steps 1 and 2 change, the rest does not. Survey, read the docs, read the
history. Then bring the human a DRAFT of what you think remains, phrased as
units with outputs, and grill against that draft rather than from nothing. It
is far easier to correct a wrong list than to produce one from a blank page.

Three failure modes to avoid. Do not file an issue for work already done:
check the survey and the outputs on disk first. Do not turn every TODO into a
unit; a unit is something with a declared artifact, and a TODO usually is not.
And do not write over the repo's own documents: step 3's "write PLAN.md" does
NOT apply when one already exists. The survey's `protected_docs` names them;
write to `.swarm/PLAN.md` instead.

## What this never does

- Create or modify anything in a tracker without explicit approval in this
  session.
- Close an issue because an agent said so.
- Write to a shared canonical path. That is `swarm.py promote`, and it needs a
  named approver.
- Ask a question the survey already answered.
- Report a run as finished without a report, or write that report from
  recollection rather than from the receipts.
