# What long-horizon orchestrators do that we do not

Research report, 2026-09-21. Written against `hanig/coordinator-moved` at `fc2bcba`.

## How to read this

Claims are marked:

- **[read]** — from a source I fetched and read, cited inline.
- **[code]** — verified against this repository, with a file and line.
- **[mine]** — my inference or judgement, not a source claim.
- **[no evidence]** — I looked and did not find it. Stated rather than filled in.

Two honesty notes up front. First, I could not extract text from two arXiv PDFs
(`2606.17182` on concurrency anomalies in multi-agent LLM systems, and
`2603.29231` "Beyond pass@1: A Reliability Science Framework for Long-Horizon
LLM Agents") — the fetches returned structural metadata only. I have not used
either for any load-bearing claim. Second, several 2026 arXiv preprints I do
cite are read at abstract or summary depth, not fully; they are marked as such
where they carry weight.

---

## Part 1 — Your design, in my words

A run is a static DAG of units in `plan.json`. Each unit declares `inputs`,
`outputs`, `kind` and its retry exposure, and the plan is refused before
dispatch if it cannot dispatch: a partition this cluster does not have, an
account a partition denies, a string where a list is required, `--array` plus
declared outputs, a `code` unit with no `mode` or no `target_branch`, an output
path outside the attempt's write root. `validate` checks declared facts and
deliberately not shell behaviour, which is why a `runtime` declaration plus a
canary that is a DAG ancestor exists as a separate mechanism [code:
`swarm.py:1160`, `_validate_runtimes` at `762`].

Dispatch allocates an exclusive, never-reused write root per attempt with
`mkdir(exist_ok=False)`. That exclusivity is what makes a cheap done-predicate
conclusive, and the predicate is conjunctive: declared outputs present, plus a
coordinator-pinned pre-dispatch artifact basis that fails closed if absent, plus
the kind's execution evidence (for `slurm`, a terminal-OK `sacct` row owned by
this attempt). The premise being defended is "the artifact was not already
there", not "this process wrote that byte" — attribution by observation is
explicitly forbidden and per-attempt provenance (pre-dispatch digest, anchored
launch intent, inode-bound worktree identity) is explicitly required, which is
a distinction most systems never draw.

Only `unit.py check` judges. `run` and `advance` allocate, submit, bind, and act
on an exit code. Code judging lives in `worktree.py`, authorized verification in
`verify.py`, declared metrics gates in `converge.py`, and an unmet criterion
becomes `NEEDS_HUMAN`: it closes no ticket and releases no dependent.

Closure authority is fixed by kind and is not a field. A `code` unit closes on a
merged PR whose head equals the head the coordinator independently judged the
attempt to have produced; `slurm` and `pipeline` close on a predicate receipt.
An authorized verifier receipt is a third, narrower class — admissible only when
the policy authorizing it is read from the anchored base commit, the verifier is
pinned to its content digest, and the receipt is bound to the head and claim it
ran under, so candidate code cannot authorize its own verifier.

Authority lives in coordinator state. Launch records and receipts are audit-only
and there is a family of `trusted_*` accessors (`trusted_base`,
`trusted_produced_head`, `trusted_artifact_basis`, `trusted_launch_facts`) that
exist because three review rounds found the same defect in three places: a
caller reaching for a trust-deciding value wherever it was handy [code:
`swarm.py:5389` onward].

The coordinator has no network. Tracker mutations become intents in an
append-only `outbox.jsonl` with an idempotency key and an evidence digest, and a
separate connected session drains them. Drain outcomes are not interchangeable:
`operation_accepted` and `asynchronously_completed` yield no receipt, only
`confirmed_by_readback` does, and an intent with no receipt reads
`unacknowledged`, which is "this machine has no confirmation either way" and
never "the issue was not filed". A false acknowledgment is held to be strictly
worse than a missing one because re-draining is safe and un-filing is not.

Exclusion is an advisory `flock` on the state directory, sixth version, with no
TTL, no breaker, no ownership token and no renewal loop — because every defect
in the five hand-rolled predecessors lived in machinery that existed only
because a plain file cannot notice its owner died. The known limits are stated
in the docstring: cross-node exclusion is uncertified because every measured
trial was same-host, and NFS lock recovery can drop a live holder's lock
undetectably.

A retry is a fresh attempt in a new empty root; `max_attempts` defaults to 1;
`retry.mode: "resume"` is refused until a checkpoint is atomic, validated,
actually skips completed work and still yields the complete declared outputs.
Bounded `continuation` nudges answer exactly one condition — a code agent that
settled having produced nothing — and stay inside the attempt, because a
conversational turn is not a retry boundary.

A run is not finished until a report is assembled from `plan.json`, coordinator
state and every `receipt.json`, including a "what this evidence does not
establish" section read from each receipt's own `basis` block.

Around that sits the orchestrator mandate — authority granted by merge rather
than by conversation, confirmed once per session against an enumerated list,
then acted on without re-asking — and the review gate: plan review at exactly
two contrasting models and never escalated, implementation review bounded to
three rounds with a mandatory `--claim "This change cannot make an honest run
fail."`, mandatory digest-bound dispositions from round 2, and reviewers
prompted to refute with no remediation field.

**My one-line summary of the design's thesis** [mine]: *every claim must be
reducible to something on disk that was not there before, and no agent-writable
byte may decide anything.* That is a coherent and unusually disciplined
position, and most of what follows is not an attack on it.

---

## Part 2 — The thesis of this report

You asked where you are over-engineered. Here is the finding I would defend
hardest, and it reframes the rest:

> **You have eight invariants about truth and zero about time.** [mine]

Every one of the eight load-bearing choices is about *whether a claim is
admissible*. Not one is about *whether the run is still moving*. Concretely,
verified in the code:

- No unit has a wall-clock deadline. `timeout_s` appears exactly once in
  `swarm.py`, in a comment listing fields excluded from a digest; nothing
  implements it [code: `swarm.py:2097`, and `grep timeout_s` returns that line
  only].
- `allocated_at` is written into unit state but read by exactly one caller,
  `reconcile_orphan`, to bound an `sacct` query window. Nothing compares it to
  now [code: `swarm.py:6911` writes it, `6387` is the only consumer].
- `_status_rows` emits `state`, `job_id`, `attempt_dir`, `attempts`,
  `gpu_hours`, `needs`, `held_by`, `waiting_on`, `promotable`, `promoted`. No
  duration, no age, no timestamp [code: `swarm.py:8234`].
- `status_report` has a `needs_attention` list and a `halted` field, and exits
  non-zero on halt. The only enforced ceiling is `budget.gpu_hours` [code:
  `swarm.py:8257`].
- There is no liveness signal for a `code` unit. `_start_code_terminal_watchers`
  is event-driven — it waits in Paseo for a terminal judgment — and is
  explicitly best-effort with "scheduled advance remains the fallback". An agent
  that neither terminates nor produces is bounded by nothing the coordinator
  owns [code: `swarm.py:7139`].

The mandate's `$50/day` spend ceiling is also not enforced by any program,
because the spending happens in the network-capable session and the coordinator
cannot see it [code: `docs/orchestrator-mandate.md:61`; no `$`-budget field
exists in `swarm.py`'s budget handling].

For a system whose defining scenario is "runs measured in days where nobody
watches most of it", that asymmetry is the gap. Everything in Part 3 is either
that gap, a narrow safety hole inside a choice you made correctly, or a
mechanism you already have that nobody can see.

---

## Part 3 — What comparable systems do

### 3.1 Durable execution engines: three clocks, not zero

Temporal's answer to "the driving process dies and is replaced" is an event
history on the server plus replay: a worker crash hands the execution to another
process, which rebuilds state and resumes where it stopped [read:
https://docs.temporal.io/evaluate/understanding-temporal]. You have the durable
half of this — `load_state`/`save_state` and the persist-before-you-act rule are
the same idea, and `plan_digest` plays the role Temporal's determinism check
plays.

What Temporal has that you do not is **three distinct timeouts, each answering a
different question** [read:
https://docs.temporal.io/encyclopedia/detecting-activity-failures,
https://temporal.io/blog/activity-timeouts]:

| Timeout | Detects | Their stated rationale |
|---|---|---|
| Start-to-close | a worker crashed *after* starting the task | the server cannot detect lost communication, so this forces a retry |
| Heartbeat | a task that is alive but **stuck** | detects within one heartbeat interval instead of waiting out start-to-close |
| Schedule-to-close | total time across all retries | only meaningful with `MaximumAttempts > 1` |

The heartbeat is the interesting one for you, because it is the only mechanism in
the industrial set that distinguishes *dead* from *stuck*, and "stuck" is the
dominant long-horizon LLM failure. Note the shape: the heartbeat is reported
*by* the activity, and Temporal's own docs say heartbeat details are for
progress reporting. In your design a heartbeat reported by the worker would be
an agent-writable value, and you must not make it authority — but you do not
need to. The coordinator already owns a clock and `allocated_at`.

Airflow deleted its SLA feature in 3.0 and replaced it with **Deadline Alerts**
(AIP-86) in 3.1: a reference point, an interval, and a callback if exceeded
[read: https://airflow.apache.org/docs/apache-airflow/stable/howto/deadline-alerts.html].
The motivation is worth quoting because it is your design problem exactly: the
old SLA was contentious because nobody could say *when you start counting* —
scheduled, queued, or started. Airflow's resolution is to make the reference
point explicit and declared. Their deadline is still **DAG-run level only**;
task-level deadlines remain an open request [read:
https://github.com/apache/airflow/issues/72519]. So even Airflow does not have
what I am about to recommend at unit level.

Prefect's pause/suspend takes a `timeout` defaulting to 3600 seconds and
**fails the flow run if it is not resumed within it** [read:
https://docs.prefect.io/v3/api-ref/python/prefect-flow_runs]. Argo's suspend
template pauses "until a user or external process resumes it, **or until a
specified duration elapses**" [read:
https://argo-workflows.readthedocs.io/en/latest/walk-through/suspending/]. Both
are the same design decision: *a human-in-the-loop state with nobody watching is
a failure, and it gets a clock.* Your `NEEDS_HUMAN` has no clock and no
notification path [code: `swarm.py:4150` maps it to a `block` intent, which
lands in an outbox that nothing forces anyone to drain].

Dagster's freshness policies plus **blocking asset checks** are the closest
analogue to `converge.py`: `AutomationCondition.all_deps_blocking_checks_passed()`
prevents a downstream asset materializing when an upstream data-quality check
failed, and the run machinery prevents the child from materializing if checks
fail mid-run [read:
https://docs.dagster.io/guides/automate/declarative-automation/customizing-automation-conditions/customizing-on-cron-condition,
https://dagster.io/blog/dagster-asset-checks]. You already do the hard half —
an unmet criterion releases no dependent. What Dagster adds is the *freshness*
axis: a declared expectation about time, evaluated continuously, visible as
"overdue" before anything fails.

### 3.2 Pipeline engines: your isolation choice is the consensus, and resume is where they bleed

Nextflow computes a hash per task before execution — full file paths, last
modified timestamps, container ID, script content — and each task gets its own
work directory; `-resume` recovers a prior execution when the hash matches
[read: https://www.nextflow.io/docs/stable/cache-and-resume.html]. That is
**the same design as your per-attempt write root**: one exclusive directory per
unit of work, identity by content rather than by observation of who wrote what.
Nowhere in the pipeline-engine literature I read does anyone attempt to prove
which process wrote a file. Your choice #1 is not unusual rigor; it is the
industry's settled answer, and the three designs that died trying were trying
something nobody does [mine].

Your refusal of `retry.mode: "resume"` looks like over-caution until you read
Snakemake's issue tracker. Snakemake keeps hidden metadata tracking which
executions started and completed, deletes incomplete outputs on a clean error,
and offers `--rerun-incomplete` and `--cleanup-metadata` for when it did not.
The open and recurring failures are: hard-terminated jobs leave undeleted
incomplete files; `--cleanup-metadata` "sometimes does not work" (#1497, #828);
`IncompleteFilesException` while *using* `--rerun-incomplete` (#1318); and in
9.13.4, outputs of failed jobs stopped being marked incomplete at all, so
`--keep-incomplete` suppressed reruns [read:
https://github.com/snakemake/snakemake/issues/3808,
https://github.com/snakemake/snakemake/issues/1318,
https://github.com/snakemake/snakemake/issues/1497,
https://snakemake.readthedocs.io/en/stable/executing/cli.html].

**Finding** [mine]: your five-condition bar for resume (atomic marker,
validated before reuse, actually skips completed work, still yields complete
declared outputs) is a precise enumeration of the four ways Snakemake's resume
has actually broken. I would not relax it. This is the one place where "we are
more rigorous than everyone else" is supported by the other systems' bug
trackers rather than by taste.

### 3.3 Merge queues: the mechanism, and the trap in adopting it

You listed "candidate-merge validation is not enforced; CI validates the head,
not the merge result" as a known gap, so I will only add the mechanism and the
non-obvious hazard.

Zuul's dependent pipeline was the origin: it borrows speculative execution from
processor design, optimistically predicts that queued changes will pass, and
tests change N against `main` plus every change ahead of it, "exactly as if they
had been tested one at a time" [read:
https://zuul-ci.org/docs/zuul/latest/gating.html,
https://opensource.com/article/20/2/zuul]. GitHub's merge queue implements the
same thing: a `merge_group` is created from the base branch plus the changes
ahead in the queue, on a temporary branch with a special prefix [read:
https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue].
Mergify batches up to N and **bisects on failure** — a 32-PR batch isolates in
about 5 CI runs rather than 32 [read:
https://docs.mergify.com/merge-queue/batches/,
https://mergify.com/learn/merge-queue].

**The trap** [mine], stated because you asked me to flag recommendations that
quietly break a choice: *adopting a merge queue naively breaks choice #2.* A
merge queue merges the **speculative merge commit**, which is a head the
coordinator never judged. Your closure rule — merged PR head equals the head the
coordinator independently judged the attempt to have produced — would either
start failing on every merge or would have to be relaxed to "some ancestor",
which is closure authority becoming configurable through the back door.

The shape that fits your design instead: keep closure bound to the judged head
plus the recorded anchored base, and add merge-result validation as a **pre-merge
gate keyed on the pair `(base_sha, judged_head_sha)`** — compute the merge
result locally in a throwaway worktree, run the gate against it, and record a
receipt bound to both SHAs. If the base moves, the receipt is stale by
construction and the gate re-runs. That gives you Zuul's property (you tested
what will actually land) without a second head entering the closure predicate.

### 3.4 Leases and fencing: you are right, with one hole

You asked what exact failure the no-TTL flock avoids. Kleppmann's argument,
read in full: a lease with a TTL is unsafe because a holder can be paused
arbitrarily — a stop-the-world GC pause of minutes, or a write delayed in the
network for ~90 seconds as in the GitHub incident he cites — and resume
believing it still holds the lock. His conclusion is unambiguous: **no
distributed lock algorithm can prevent concurrent access without fencing at the
resource.** A fencing token is a monotonically increasing number returned on
acquire; every write carries it; the *storage server* rejects any write whose
token is lower than the highest it has accepted. He also draws the
efficiency/correctness distinction: for efficiency, an occasional double-execute
costs money; for correctness, it costs "a corrupted file, data loss, permanent
inconsistency" [read:
https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html].

Against that standard:

- Your removal of the TTL is correct and the reasoning in the docstring is
  Kleppmann's reasoning. A stealable lease under a pause is precisely the
  failure he describes, and your five predecessors each reinvented it. **Do not
  add a TTL or a heartbeat.** I am saying this explicitly so it is on the record
  that I checked and agree, not so I can propose it later.
- You already have fencing where it matters most, and I do not think this is
  written down as fencing anywhere. `mkdir(exist_ok=False)` on a never-reused
  attempt root *is* resource-side rejection of a stale writer: a deposed
  coordinator cannot re-enter an attempt root, because the create fails. So does
  `_take_output_claims` / `_claim_dir`, which arbitrates output destinations
  among coordinators sharing one run root [code: `swarm.py:3307`, `3175`].
- **The hole**: `save_state` is a blind atomic overwrite. It takes no generation
  number, reads nothing back, and compares nothing [code: `swarm.py:2339`, and
  `U.write_json` at `unit.py:637` is tmp-write-then-`replace`, atomic but
  unconditional]. Under the failure your own docstring names — NFS lock recovery
  dropping a live holder's lock — two coordinators both `load_state`, both
  advance, and the later `save_state` silently wins with a stale view of every
  unit. Nothing refuses; nothing notices. The replaced-inode window that
  `_holds_the_path` narrows but admits it cannot close has the same shape.

**This is my highest-confidence unlisted finding.** See §4.2 for the fix.

### 3.5 The outbox: you implemented a named pattern and left out the relay

Your tracker boundary is the **transactional outbox pattern**, exactly: write the
intent durably in the same step as the state change, and let a separate relay
forward it asynchronously; consumers deduplicate on a stable key [read:
https://microservices.io/patterns/data/transactional-outbox.html]. The
literature's blunt framing is that exactly-once *delivery* is unrealistic and
outbox-plus-deduplication is the practical answer, and that every participant
must be idempotent under a stable key supplied by the orchestrator. You have the
key, the envelope, the evidence digest and the four-valued drain outcome, which
is more careful than the pattern as usually described [read: same, plus
`docs/tracker-outbox.md`].

What the pattern has and you do not: **the relay is a process, and its depth and
lag are the canonical health metric.** Your outbox reached 67 unacknowledged.
That is not a flaw in choice #3 — it is the relay being a human, plus the fact
that nothing in `status` reports outbox depth or the age of the oldest
unacknowledged intent [code: `status_report` at `swarm.py:8257` reports neither;
`acknowledgment_status` exists at `4919` but is not wired into status].

I am not proposing the coordinator reach the network. I am proposing the number
67 be visible at 3. That is a read of two local files.

### 3.6 Reconciliation: your static plan is edge-triggered where it should be level-triggered

Kubernetes controllers are level-triggered by design: `controller-runtime` does
not pass the event type into `Reconcile`, only a key, deliberately, because "a
controller should not care *why* it was triggered, only what the current state of
the world looks like". Joe Beda's stated rationale: edge-triggered systems risk
compromising state and never being able to re-create it; level-triggered systems
are forgiving and let misbehaving components be rectified [read:
https://hackernoon.com/level-triggering-and-reconciliation-in-kubernetes-1f17fe30333d].

Your coordinator is level-triggered *within* a plan — `advance` reads durable
state and converges — and that is right. But the **plan itself is an edge**.
`plan_digest` is computed over all units at once, and a mismatch refuses the
whole advance until `--accept-plan-change` ratifies the entire file in one
gesture [code: `swarm.py:2104`, `6311`–`6337`]. There is no per-unit digest in
state.

For a days-long run this bites in a specific way [mine]: you learn something in
unit 3 that changes unit 9, and your options are stop the run or ratify a
whole-file diff whose recorded evidence is two truncated hashes. Worse, ratifying
one intended edit silently ratifies every *unintended* edit in the same file —
including an edit to a unit that is currently live.

Temporal's resolution of the same problem is instructive and is *not* "allow
anything". A **pinned** workflow is guaranteed to complete on the Worker
Deployment Version it started on; an **auto-upgrade** workflow moves to new code
and must be kept replay-safe by explicit patching. Their own guidance prefers
versioning at the deployment level over patching inside the code, because patches
accumulate into clutter [read:
https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning].
Anthropic's equivalent for long-running research agents is rainbow deployments —
gradually shifting traffic while keeping both versions running — specifically "to
avoid disrupting running agents" [read:
https://www.anthropic.com/engineering/multi-agent-research-system].

The transferable idea is **granularity, not permissiveness**: version the unit,
not the run.

### 3.7 What the agent literature says about long horizons

This is the part that argues for the whole of §4.1.

- **Error compounding is exponential, and has a single-parameter model.** METR's
  time-horizon work fits a logistic curve to success against human task
  duration; the 50% horizon has roughly doubled every 7 months, and the gap
  between the 50% and 80% horizons is large — for Claude 3.7 Sonnet, 59 minutes
  at 50% versus 15 minutes at 80% [read:
  https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/,
  https://arxiv.org/abs/2503.14499]. The follow-up models this as a **constant
  hazard rate**: a fixed probability of failing per minute of human-equivalent
  work, giving each agent a "half-life", and attributing it to tasks decomposing
  into increasingly many subtasks where failing any one fails the task. The
  authors explicitly say generalisation beyond their suite is unknown [read:
  https://arxiv.org/abs/2505.05115].
- **Reliability under repetition collapses faster than average success.**
  τ-bench's `pass^k` counts a task solved only if all k independent attempts
  succeed. A GPT-4o function-calling agent above 60% average success drops below
  25% at `pass^8`; a 90% `pass@1` agent is at 57% for k=8 [read:
  https://arxiv.org/abs/2406.12045, https://sierra.ai/blog/tau-bench-shaping-development-evaluation-agents].
  For a 30-unit DAG at 95% per unit, end-to-end is 0.95^30 ≈ 21% [mine,
  arithmetic]. This is the quantitative reason `NEEDS_HUMAN` must be *noticed*
  rather than merely recorded: over days, the expected number of
  human-attention events is not small.
- **Long-horizon coherence fails in named, recognisable ways.** Vending-Bench
  runs >20M tokens per episode. All models have runs that derail, by
  misinterpreting operational status (believing an order arrived), forgetting
  orders, or descending into tangential "meltdown" loops they rarely recover
  from. Its sharpest finding: models write thorough summaries to the provided
  scratchpad and key-value store and then **rarely or never retrieve them** —
  the limit is memory *strategy*, not memory capacity [read:
  https://arxiv.org/abs/2502.15840].
- **Ultra-long software work is where you actually live, and it is mostly
  unsolved.** SWE-Marathon: 20 tasks of 2–10 hours, logged attempts averaging
  27.2M tokens. Frontier agents solve under 30%. The named failure modes are
  **poor self-verification, self-reported infeasibility, and premature
  termination**, and reward-hacking behaviour appeared in 13.8% of rollouts
  [read: https://arxiv.org/abs/2606.07682, abstract and summary depth].
- **Verification, not generation, is the binding constraint, and it must
  co-evolve.** "The Verification Horizon" inverts the classical intuition:
  generating candidates is no longer hard, reliably verifying them is. Every
  verifier is a proxy for intent and never the intent; the catalogued failures
  are intent underspecification plus a widening proxy gap under optimisation
  pressure; and the conclusion is that no fixed reward function stays effective
  as capability grows [read: https://arxiv.org/abs/2606.26300, abstract depth].
  Related: SWE-bench's evaluator trusts test output produced inside a container
  the agent's own patch can modify [read:
  https://arxiv.org/html/2606.26300v1 discussion,
  https://arxiv.org/pdf/2605.12673].

**What this says about your design** [mine]: choice #2 and your verifier
authorization rules are the correct response to the verification-horizon
problem, and they are stronger than what the benchmarks themselves implement —
your rule that verifier policy is read from the *anchored base commit* and the
verifier is pinned by content digest is precisely the defence SWE-bench lacks.
Your `continuation` mechanism answers SWE-Marathon's "premature termination" and
"self-reported infeasibility" directly. Do not weaken either.

But SWE-Marathon's third failure mode — poor self-verification — and
Vending-Bench's meltdown loops are both **time-shaped**, and nothing in your
system can see time.

---

## Part 4 — Ranked findings

Three things matter. The rest, honestly, is noise or is already on your list.

### 4.1 — **A declared deadline per unit, with `NEEDS_HUMAN` as the outcome.** Do this first.

**Mechanism.** Add two optional declared fields to a unit: `deadline_s`
(seconds from `allocated_at`) and, for `code` units only, `no_progress_s`.
In `advance`, before dispatching anything new, for every unit in a live state:

1. If `now - us["allocated_at"] > deadline_s`, set state to `NEEDS_HUMAN` with
   reason `deadline_exceeded`, emit the `block` intent, and do **not** mint a
   retry. Airflow's own postmortem on SLAs says the reference point must be
   explicit; make yours `allocated_at`, which is coordinator-written and already
   in state [code: `swarm.py:6911`], and say so in the schema text.
2. For a `code` unit, compute a progress quantity the coordinator already owns
   and that no agent can forge: the count and digest-set of declared outputs
   present in the attempt root, plus `git rev-parse HEAD` in the inode-bound
   worktree, plus the `continuations` length. If none of those has changed in
   `no_progress_s`, that is Temporal's heartbeat timeout without a
   worker-reported heartbeat. Record the observation with its timestamp so the
   next advance can compare.
3. `swarm.py status --json` gains `age_s` and `since_progress_s` per row, and
   `needs_attention` gains a `stalling` class for units past 80% of their
   deadline. Exit non-zero on `stalling` so a cron wrapper trips before the
   deadline rather than after.
4. Default: absent. A unit with no `deadline_s` behaves exactly as today. For
   `slurm` units the natural default is the `--time` already in `sbatch`.

**What it prevents.** The unattended run that is not running. Temporal's stated
rationale for start-to-close is that the server cannot detect a worker that lost
communication, which is exactly your position with respect to a Paseo agent
[read: Temporal docs, above]. Vending-Bench's meltdown loops and
SWE-Marathon's premature termination are the two agent-side shapes this catches
[read, above].

**Have you suffered it?** I cannot show you a run that hung, and I will not
claim one. What the repository shows is that you know the shape:
`hanig-project/SKILL.md` says an absent `mode` "presents as an agent that runs
forever doing nothing"; `maybe_continue` exists because agents settle
empty-handed; `status_report`'s comment about `READY_FOR_PR` says the quiet part
— "no mechanism can leave that state today, so a DAG parked in it is stalled,
not progressing. Without this, `status` exits 0 and a cron wrapper polling it
reports a healthy project forever" [code: `swarm.py:8261`]. That comment is
about one state. The same sentence is true of `RUNNING`, and nothing guards it.

**Cost against the eight.** None that I can find. It reads a clock and
coordinator-owned state. It does not touch the network (#3). It reads no
agent-writable file for authority (#4) — the progress quantity is a coordinator
observation of the filesystem and of git, the same class of observation
`judge_artifacts` already makes. It does not touch closure authority (#2): a
deadline produces `NEEDS_HUMAN`, never `DONE` and never `FAILED`, which is the
distinction your own code already draws at `swarm.py:6589` ("NEEDS_HUMAN, not
FAILED: the command did not fail"). It does not add a lease TTL (#7) — this is a
clock on *work*, not on *exclusion*, and conflating the two is what killed your
five previous leases.

**The one real risk** [mine]: a deadline is a declared guess, and a wrong guess
converts a healthy long unit into a human-attention event. That is why the
outcome must be `NEEDS_HUMAN` and not a retry — a false deadline costs one
notification, never a re-dispatch. Also: do not let the deadline be inferred.
Add it to the interview beside "the most work you are willing to repeat", as
"the longest this may run before you want to be told". It is the same kind of
question and the same person is the only one who can answer it.

**Worth doing?** Yes, highest. It is the smallest change with the largest effect
on the scenario the system exists for.

### 4.2 — **Fence `save_state` with a monotonic epoch.**

**Mechanism.** On `acquire_lease`, read the current `state["epoch"]` (default 0),
set `state["epoch"] = epoch + 1`, and hold that number in memory. Every
`save_state` re-reads only the epoch field from the on-disk state and refuses if
it is not the value this process wrote — then `halt` with "another coordinator
has written state under us" rather than overwriting. Roughly fifteen lines.
`load_state`/`save_state` are the only call sites [code: `swarm.py:2321`,
`2339`].

**What it prevents.** Silent last-writer-wins on the entire coordinator state
under the two failure modes your own code documents but does not defend: NFS
lock recovery dropping a live holder's lock (`acquire_lease` docstring, "Nothing
in local state can notice"), and the replaced-inode window `_holds_the_path`
explicitly says it narrows but cannot close [code: `swarm.py:2173`, `2268`].
This is Kleppmann's fencing token, applied to the one resource in your system
that is not already fenced — `mkdir(exist_ok=False)` fences attempt roots and
`_claim_dir` fences output destinations, but state is an unconditional overwrite
[read: Kleppmann, above; code as cited].

**Have you suffered it?** [no evidence] I found no record of a state clobber in
the repository, and I would expect it to be nearly invisible if it happened —
that is the argument for the guard, not against it. Your measured exclusion
trials were all same-host, so the topology where this bites has never been
exercised.

**Cost against the eight.** It *strengthens* #7 rather than weakening it. It is
not a TTL, not a heartbeat, not a renewal, and not stealable: the epoch is
never used to *acquire* anything, only to refuse a write. Authority still lives
in coordinator state (#4) and the epoch is coordinator-written. The one cost is
an extra read per save, and one new halt reason to document.

The cheap version, if you want it in three lines instead of fifteen: on
`acquire_lease`, record `(host, pid, boot-unique id)` in state and have
`save_state` refuse when the on-disk value is not yours. That catches the
same-filesystem two-coordinator case, which is the one that can actually happen
today, and it composes with a later epoch.

**Worth doing?** Yes. Small, closes a hole inside a choice you made correctly,
and the failure it prevents is the silent kind you cannot audit after the fact.

### 4.3 — **Per-unit plan digests, replacing whole-file ratification.**

**Mechanism.** Store a digest per unit in `state["units"][uid]["spec_digest"]`
at first dispatch. On advance, compare per unit:

- A unit not yet dispatched: any change is accepted silently. This is most edits.
- A new unit whose `needs` are all existing units: accepted, no ratification.
- A unit that is live or `DONE`: changed spec is refused, naming the unit and
  the changed fields, and `--accept-unit-change UID` ratifies exactly that one.
- Keep the whole-plan digest as a cheap fast path, but derive the refusal from
  the per-unit comparison so the message names units and fields rather than two
  truncated hashes.

This is Temporal's pinned-versus-auto-upgrade distinction at the granularity
your units already have: a dispatched attempt is pinned to the spec it was
dispatched under; an undispatched unit auto-upgrades [read: Temporal worker
versioning, above].

**What it prevents.** Two things. The one you would notice: a days-long run that
cannot absorb what it learned without being stopped or wholesale-ratified. The
one you would not: today `--accept-plan-change` ratifies every edit in the file,
including an edit to a live unit made by a different session, and records only
`from`/`to` hashes [code: `swarm.py:6314`–`6322`]. A per-unit comparison makes
the dangerous case refusable and the harmless case silent — strictly safer than
the current escape hatch, not looser.

**Have you suffered it?** The presence of `--accept-plan-change` and its comment
("Raising a budget ceiling is a DESIGNED human intervention, so there must be a
way to say yes") shows you hit the refusal and built the hatch. I have no
evidence of a bad ratification [no evidence].

**Cost against the eight.** None. It does not touch closure, the network, or
authority location. It makes `plan_digest` finer, and the existing whole-plan
digest can stay as an index. The cost is that `ratified_edits` becomes per-unit,
which is a schema addition.

**Worth doing?** Yes, third. It is the precondition for the intake gap you
already know about — an intake path has nowhere to put a new unit until adding
one is cheap — but it earns its place on its own.

### 4.4 — Below the line

These are real and I would not spend on them now.

- **Outbox depth and lag in `status`.** Two lines of plumbing, wires
  `acknowledgment_status` into `status_report` [code: `swarm.py:4919`, `8257`].
  I have not ranked it above because it is a direct restatement of a known gap;
  I mention it because the *fix* is smaller than the gap description suggests,
  and the outbox pattern's own literature treats relay lag as the metric
  [read: microservices.io, above].
- **Burn-rate alerting on attempts.** Google SRE's multiwindow multi-burn-rate:
  a long window detects sustained consumption, a short window (1/12 the long
  one) confirms it is still burning, and both must exceed threshold — 14.4× over
  1h/5m to page, 6× over 6h/30m, 1× over 3d/6h to ticket [read:
  https://sre.google/workbook/alerting-on-slos/]. The analogue is attempts per
  `DONE` unit, or `NEEDS_HUMAN` events per hour. This is genuinely the right
  shape for a multi-day run, and I have put it below the line for one reason: at
  your DAG sizes (tens of units, not thousands) the sample is too small for a
  rate to be better than the list, and §4.1's per-unit deadline dominates it.
  Revisit if a plan ever has hundreds of units.
- **Declared exit handlers per unit.** Argo runs an exit handler regardless of
  success/failure/error, with the completed context, for releasing external
  resources and publishing status; Temporal's sagas pair each step with an
  idempotent compensation keyed by the orchestrator [read:
  https://argo-workflows.readthedocs.io/en/latest/walk-through/suspending/,
  https://oneuptime.com/blog/post/2026-08-02-argo-workflow-exit-handlers/view,
  https://microservices.io/patterns/data/transactional-outbox.html]. You have a
  partial one for code units already — `_archive_code_worktree`,
  `_worktree_cleanup_failed`, and an aggregate `NEEDS_HUMAN -- retained
  worktrees:` report [code: `swarm.py:6089`, `6032`]. Generalising it to a
  declared hook is the mechanism for your known "preserving a blocked attempt's
  work is agent discretion" gap. Below the line because generalising a hook is
  how you acquire a plugin point that runs arbitrary code in the coordinator's
  process, and the specific case is already handled.
- **Starved reviewer as a per-reviewer, not per-round, status.** Your gap is
  real but narrower than stated: `review.py` already has exit 2
  `REVIEW_UNAVAILABLE` and exit 3 `REVIEW_PARTIAL`, and CLAUDE.md already says
  neither is a pass. What is missing is promoting a *single* starved reviewer
  inside an otherwise-complete panel to exit 3. The industrial pattern is the
  distinction between "check failed" and "check never reported" — GitHub's merge
  queue treats a required check that never reports as blocking forever, which is
  the fail-closed choice [read: GitHub merge queue docs, above]. One condition
  in the verdict aggregation.
- **A learned trajectory monitor.** Discussed in §5 and not recommended.

### 4.5 — Things I considered and rejected as non-starters for you

Stated so you can see they were considered rather than missed.

- **A lease TTL, a heartbeat, or a renewal loop for exclusion.** Kleppmann's
  argument and your own five-rewrite history agree. Non-starter, correctly.
- **Reading a worker-reported heartbeat as liveness authority.** Breaks #4. The
  coordinator-side progress observation in §4.1 gets the same signal without it.
- **A merge queue as the closure path.** Breaks #2; see §3.3 for the version
  that does not.
- **Making closure authority per-unit configurable so a `code` unit could close
  on a verifier receipt.** This is the single most tempting change and the worst
  one. The verification-horizon literature's conclusion — no fixed proxy
  survives capability growth, and SWE-bench's evaluator trusting in-container
  test output is the concrete instance — is an argument *for* your fixed
  closure, not against it [read: arXiv 2606.26300, 2605.12673].
- **Any coordinator-side tracker read, including a conditional update.** Your
  known gap about atomic conditional updates against the tracker is real, but it
  belongs in the draining session (Kubernetes-style `resourceVersion` optimistic
  concurrency is the mechanism), not in `swarm.py`. Anything else breaks #3.

---

## Part 5 — The three questions you asked explicitly

### 5.1 Where are you over-engineered?

**Choice #5. The three-round bound is too loose, not too tight, and the
step-back committee is triggered too late.** [mine, supported below]

There is now direct evidence on this and it matches your own measurements
closely enough to be uncomfortable. "More Rounds, More Noise" tested whether
letting reviewers ask follow-ups and review again improves verification.
Single-pass F1 was 0.376; the multi-turn variant fell to 0.303 (p < 0.001) and
independent re-review to 0.263. Additional rounds bought +0.08 recall and cost
precision 0.30 → 0.20, producing **62% more false positives (8.5 vs 5.2)**. The
two mechanisms they name are: reviewers **fabricate findings once genuine errors
are exhausted**, and **Review Target Drift** — reviewers shift from critiquing
the artifact to analysing the conversation. Their conclusion: "the problem is not
what the reviewer sees, but that reviewing again invites noise" [read:
https://arxiv.org/abs/2603.16244, abstract and results depth].

Now read your own PROTOCOL.md against that. Round-3 escalation, 2026-08-30:
seven MAJOR findings, five spot-checked, **none survived** — one naming a line
that does something else, one describing a limitation documented in the comment
directly above it, and three reporting defects earlier rounds had already fixed,
found by reading your own comments about the old bug. That is fabrication once
genuine errors are exhausted, and it is target drift onto your prose. You
measured the paper's result independently, and your recorded cost — "seven fixes
to code that was already correct, each one a new chance to break something that
worked" — is the precision collapse priced in engineering time.

Your protocol also already contains the correct conclusion and does not act on
it: "**Plan review is Phase 1 and it is where the value is.** Three plan reviews
cost about ten cents and twelve minutes and rejected two designs before any code
existed. A separate session ran 28 implement-then-refute rounds that all
accepted the framing and hunted defects inside it."

The complementary evidence is that the value of an adversarial panel is in
**filtering its own output**, not in iterating. Refute-or-Promote — adversarial
kill mandates, cold-start reviewers to break anchoring, cross-family critics,
and a mandatory empirical testing gate at each promotion — reports killing ~79%
of 171 candidates retrospectively and 83% of 30 prospectively before disclosure,
and is explicit that nothing was discovered autonomously: the contribution is
*structural filtering of false positives* [read:
https://arxiv.org/abs/2604.19049]. And the overcorrection finding is that LLM
reviewers frequently misclassify correct code as non-compliant, and that
**prompts requiring explanations and proposed corrections make it worse** [read:
https://arxiv.org/abs/2603.00539, abstract only — no rate given in the
abstract]. Your "reviewers refute; they do not design" rule and the absent
remediation field are the right response to that, arrived at independently.

The scale of the ambient false-positive problem, for calibration: curl
permanently closed its bug bounty after AI-generated submissions drove the
confirmed rate below 5%, and HackerOne paused the Internet Bug Bounty in March
2026 citing AI-amplified volume [read: via
https://arxiv.org/pdf/2604.19049 introduction].

**So what I would change.** Not the bound — the default.

1. Make **round 1 terminal by default** for implementation review. Rounds 2 and
   3 require a named, recorded reason, and the only good reasons are: a reviewer
   refuted a *declared acceptance criterion*, or the counter-claim ("this cannot
   make an honest run fail") was refuted. "The reviewers found more things" is
   not a reason to run again; the paper says that is where the noise comes from
   and your round-3 escalation is the instance.
2. Move the spend from rounds to **verification of findings**. You already
   require reproduction before acting and digest-bound dispositions from round 2.
   Refute-or-Promote's measured value is in the kill gate, not the round count.
   A second pass whose *only* job is to kill round 1's findings is a better use
   of the same tokens than a second review pass that generates new ones.
3. Trigger the **step-back committee on the first finding-about-a-fix**, not as
   one disposition among several. Your own five-round history (annotation hatch →
   bypass of the hatch → filter that hid the bypass, each fix correct and one
   level too shallow) says the signal is reliable and early. Make it mechanical
   at the orchestrator level, since the tool cannot see it.

**Second over-engineering candidate, defended rather than cut: choice #6.** I
expected to recommend relaxing the resume bar and the evidence went the other
way — see §3.2. Snakemake's tracker is a list of the four exact failures your
five conditions enumerate. Keep the refusal. The load-bearing part of your retry
design is not the machinery anyway; it is the interview question about the most
work a human will repeat, which sizes units so that discarding one is
affordable. That question is doing more work than `retry.mode` ever will, and
the METR half-life model is the reason it works: if failure probability is
roughly constant per unit of work, shorter units are the only lever that moves
end-to-end reliability [read: arXiv 2505.05115].

**What is not over-engineered, stated plainly since you asked to be
challenged:** #1 is the industry's settled answer (§3.2), #2 is stronger than
the benchmarks that grade the field (§3.7), #3 is a named pattern implemented
more carefully than it is usually described (§3.5), #4 is the same principle as
a server-side event history, #7 is more correct than most production lock code
(§3.4), #8 is what the reward-hacking literature says you need. I went looking
for rigor to cut and found one place.

### 5.2 Is the three-round bound plus step-back committee a good idea or an expensive one?

Both, and the split is legible. [mine]

On the day in question: four blocked PRs, zero merges, one command injection
caught, one guard silently passing links to nonexistent files. A command
injection reaching `main` in a coordinator that submits jobs to shared clusters
under someone's account is not a defect class you want to price against a day of
throughput, and a guard that silently passes is *exactly* the failure your repo
has made four times ("a grep that matches the comment explaining an absence").
Those two findings justify the panel's existence on their own.

But zero merges is not the panel's fault, and reading it as a rigor problem will
send you in the wrong direction. **It is an adjudication throughput problem.**
[mine] Four PRs blocked means four findings that reached neither "disproved by
evidence" nor "repaired" nor a recorded nonblocking disposition. Look at what the
mandate requires to move one: the concurrence rule needs the orchestrator *and*
GPT-Astra to agree it is not a deal breaker, Astra must be asked to assess
rather than ratify, and "deadlock fails toward more work, not toward shipping."
Compose that with a three-round bound where each round can generate new findings
at declining precision, and the fixed point is a blocked PR with a growing
finding list. Nothing in the loop is wrong; the loop has no drain.

The two changes I would make are both about the drain, not the gate:

- **A finding that is not reproduced within the round it was raised does not
  block.** You already require reproduction before acting, and you already inject
  every `not-reproduced` summary verbatim into the next round's prompt so
  disagreement cannot be filtered. Make the consequence explicit: unreproduced
  findings are carried forward as *open questions on the record*, and they do not
  hold the merge. Your measured 0-of-5 survival rate for a round-3 escalation is
  the argument. Today the adjudication table sends "unresolved, ambiguous in
  scope" to *block*, which is the safe default and also the one that produced
  four blocked PRs.
- **`--escalate` currently buys nothing** — `sol` and `kimi-k3` are disabled, so
  `deep`'s enabled membership equals `standard`'s [per CLAUDE.md]. Either
  re-enable them or delete the flag. A flag that costs a round and adds no
  reviewer is the worst possible version of choice #5, and your own measured
  four-model-panel result (one reviewer upholding every claim and adding nothing,
  while two found all five defects) says a bigger panel is not the answer anyway.

And keep the step-back, but trigger it earlier — §5.1 item 3. Of everything in
the review protocol, the step-back rule is the one with the cleanest recorded
evidence of working: the five-round sequence where it eventually replaced a
mechanism instead of patching it a fourth time.

### 5.3 State of the art for knowing a run is going wrong early

The honest summary is in two halves, and the half that is deployable is
unglamorous. [mine]

**What is actually running in production, everywhere I looked:** clocks and
budgets.

| Mechanism | System | Granularity |
|---|---|---|
| Heartbeat timeout — detects stuck within one interval | Temporal | per activity |
| Start-to-close / schedule-to-close | Temporal | per activity / incl. retries |
| Deadline Alerts: reference point + interval + callback | Airflow 3.1+ | DAG run only; task-level still open |
| Freshness policies; blocking asset checks gate children | Dagster | per asset |
| Pause/suspend with a timeout that **fails** the run | Prefect (default 3600s) | per flow run |
| Suspend until resumed **or a duration elapses** | Argo | per node |
| Multiwindow multi-burn-rate alerts (14.4×/1h+5m; 6×/6h+30m; 1×/3d+6h) | Google SRE | per SLO |
| Gate reset / queue ejection on repeated failure | Zuul, merge queues | per change |

[read: all as cited in §3.1, §3.3, §4.4.]

The pattern across all of them: **a declared expectation about time, evaluated
by the orchestrator against its own clock, with an outcome that is a
notification rather than a verdict.** Note that not one of them asks the worker
whether it is fine, and not one treats the lagging final artifact as the
detection point. That is the transferable idea, and §4.1 is it.

**What Anthropic does for long-running agents**, since it is the closest
published practice to your scenario: external memory with summarisation at the
context boundary; checkpoints so a failed run resumes rather than restarting;
rainbow deployments so a code change does not disrupt agents mid-run; production
tracing that **monitors agent decision patterns and interaction structure
without tracking conversation contents**; LLM-as-judge on a rubric (factual
accuracy, citation accuracy, completeness, source quality, tool efficiency) with
0.0–1.0 scores plus pass/fail; **end-state** evaluation rather than validating
intermediate steps, because agents find alternative valid paths; and about 20
representative queries early, because small samples found the high-impact
changes before a comprehensive eval existed [read:
https://www.anthropic.com/engineering/multi-agent-research-system].

Two of those are directly instructive for you. The structure-not-contents
tracing is the right privacy-and-authority shape: it is exactly the class of
observation your coordinator is allowed to make. And "small samples first, about
20 queries" is an argument against building a comprehensive early-warning system
before you have watched twenty real runs derail.

**What the research is doing, and why I am not recommending it:** the 2026
agent-monitoring literature scores a *prefix* of a trajectory rather than its
end state. Trajectory Guard uses a Siamese recurrent autoencoder with a hybrid
contrastive/reconstruction loss and reports F1 0.88–0.94 on balanced sets and
recall 0.86–0.92 on imbalanced external benchmarks. PrefixGuard learns
domain-calibrated temporal statistics from outcome-labelled prefixes and emits
online risk scores with no deployment-time LLM inference. AgentForesight audits
each prefix with a 7B model. The stated common diagnosis is that existing safety
measures look at a single snapshot and lack temporal awareness, so they miss an
agent calling the same tool repeatedly without progress [read:
https://arxiv.org/pdf/2601.00516, https://arxiv.org/pdf/2605.06455, and the
survey at https://arxiv.org/pdf/2606.04990 — all at abstract/summary depth].

Three reasons that stays research for you [mine]: it needs labelled trajectories
you do not have; it needs the coordinator to read the agent's trajectory, which
is an agent-writable stream and cannot be authority under #4 (it could be
advisory, but an advisory signal nobody is watching is not early warning); and
the *only* signal in that whole literature that is cheap and robust — repeated
action without progress — is obtainable from coordinator-side observation of
declared outputs and worktree HEAD, which is §4.1 step 2.

**[no evidence]** I looked for, and did not find, any published account of a
production system running a learned trajectory monitor over unattended multi-day
autonomous code work. Every production mechanism I found is a clock, a budget, a
queue ejection rule, or a human looking at a trace after the fact. If someone is
doing better, it is not written down where I could reach it.

**So the state of the art, for your situation, is:** a declared deadline and a
coordinator-observed progress definition per unit; a stall class in `status` that
exits non-zero; outbox depth and oldest-unacknowledged age as first-class
numbers; and a notification path out of `NEEDS_HUMAN` that does not depend on
someone choosing to run `status`. The last one is the only part of this I cannot
locate in your design at all, and it is the part every engine in the table has.

---

## Part 6 — What I could not establish

- **Whether you have actually suffered a runaway or stalled unit.** §4.1's
  justification is the shape of the failure being documented in three places in
  your own code and skills, plus the benchmark literature. It is not an incident
  report. If you have one, it strengthens the case; if you have run for months
  without one, that is evidence against my ranking and I would want to know.
- **Whether `$HOME` is NFS on the clusters**, which `docs/plan-field-reports.md`
  records as unanswered (ARC-248, "HALF"). It determines how reachable the
  NFS-lock-recovery case in §4.2 actually is. I did not test it.
- **Quantitative overcorrection rates for LLM code reviewers.** arXiv 2603.00539
  establishes the direction (correct code misclassified as non-compliant, worse
  with explanation-demanding prompts) but its abstract gives no rate, and I read
  only the abstract.
- **Two papers I could not read.** arXiv 2606.17182 (concurrency anomalies in
  multi-agent LLM systems — would have been the most direct external commentary
  on choice #1) and arXiv 2603.29231 (reliability science for long-horizon
  agents). Both PDF fetches returned metadata only. Neither informs any claim
  here.
- **How Devin, Jules, or Codex-style hosted agents decide a task is done.** The
  Cognition/Anthropic architecture debate is well documented — Cognition's
  position is that naive parallel subagents fail because they carry no context of
  each other's work and their implicit decisions conflict, and that context
  engineering is the reliability lever [read:
  https://cognition.com/blog/dont-build-multi-agents,
  https://cognition.com/blog/multi-agents-working]. But I found no published
  description of any hosted product's *closure predicate*. If anyone has
  something as explicit as "a merged PR whose head equals the head the
  coordinator judged", it is not public. On the evidence I have, your choice #2
  is stricter than anything documented in the commercial field.
- **Any external validation that plan review beats implementation review.** Your
  own measurement (3 plan reviews, 10 cents, 2 designs rejected, versus 28
  implement-then-refute rounds that accepted the framing) is the strongest
  evidence I have seen on the question and I found nothing comparable published.
  It is a single-site observation; I am treating it as credible because the
  "More Rounds, More Noise" mechanism (review target drift, fabrication after
  exhaustion) explains why it would be true.

---

## Sources

Read and used:

- [Understanding Temporal](https://docs.temporal.io/evaluate/understanding-temporal)
- [Temporal — Detecting Activity failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures)
- [Temporal — The four types of Activity timeouts](https://temporal.io/blog/activity-timeouts)
- [Temporal — Worker Versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning)
- [Temporal — idempotency and durable execution](https://temporal.io/blog/idempotency-and-durable-execution)
- [Airflow — Deadline Alerts](https://airflow.apache.org/docs/apache-airflow/stable/howto/deadline-alerts.html)
- [Airflow — Migrating from SLA to Deadline Alerts](https://airflow.apache.org/docs/apache-airflow/stable/howto/sla-to-deadlines.html)
- [Airflow issue #72519 — task-level deadline alerts](https://github.com/apache/airflow/issues/72519)
- [Prefect — flow_runs API (pause/suspend timeouts)](https://docs.prefect.io/v3/api-ref/python/prefect-flow_runs)
- [Argo Workflows — Suspending](https://argo-workflows.readthedocs.io/en/latest/walk-through/suspending/)
- [Argo Workflows exit handlers](https://oneuptime.com/blog/post/2026-08-02-argo-workflow-exit-handlers/view)
- [Dagster — Asset checks](https://dagster.io/blog/dagster-asset-checks)
- [Dagster — customizing automation conditions (blocking checks)](https://docs.dagster.io/guides/automate/declarative-automation/customizing-automation-conditions/customizing-on-cron-condition)
- [Nextflow — Caching and resuming](https://www.nextflow.io/docs/stable/cache-and-resume.html)
- [Snakemake CLI (`--rerun-incomplete`, `--cleanup-metadata`)](https://snakemake.readthedocs.io/en/stable/executing/cli.html)
- [Snakemake #3808 — files not marked incomplete](https://github.com/snakemake/snakemake/issues/3808)
- [Snakemake #1318 — IncompleteFilesException with --rerun-incomplete](https://github.com/snakemake/snakemake/issues/1318)
- [Snakemake #1497 — --cleanup-metadata sometimes does not work](https://github.com/snakemake/snakemake/issues/1497)
- [Zuul — Project Gating](https://zuul-ci.org/docs/zuul/latest/gating.html)
- [Introducing Zuul for improved CI/CD](https://opensource.com/article/20/2/zuul)
- [GitHub Docs — Managing a merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
- [Mergify — Merge Queue Batches](https://docs.mergify.com/merge-queue/batches/)
- [Mergify — What is a merge queue](https://mergify.com/learn/merge-queue)
- [Kleppmann — How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html)
- [Level Triggering and Reconciliation in Kubernetes](https://hackernoon.com/level-triggering-and-reconciliation-in-kubernetes-1f17fe30333d)
- [Microservices.io — Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html)
- [Google SRE Workbook — Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/)
- [Anthropic — How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
- [Cognition — Don't Build Multi-Agents](https://cognition.com/blog/dont-build-multi-agents)
- [Cognition — Multi-Agents: What's Actually Working](https://cognition.com/blog/multi-agents-working)
- [METR — Measuring AI Ability to Complete Long Software Tasks](https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/) / [arXiv 2503.14499](https://arxiv.org/abs/2503.14499)
- [Is there a half-life for the success rates of AI agents? — arXiv 2505.05115](https://arxiv.org/abs/2505.05115)
- [τ-bench — arXiv 2406.12045](https://arxiv.org/abs/2406.12045) / [Sierra blog](https://sierra.ai/blog/tau-bench-shaping-development-evaluation-agents)
- [Vending-Bench — arXiv 2502.15840](https://arxiv.org/abs/2502.15840)
- [SWE-Marathon — arXiv 2606.07682](https://arxiv.org/abs/2606.07682) *(abstract/summary depth)*
- [The Verification Horizon — arXiv 2606.26300](https://arxiv.org/abs/2606.26300) *(abstract depth)*
- [More Rounds, More Noise — arXiv 2603.16244](https://arxiv.org/abs/2603.16244) *(abstract and results depth)*
- [Refute-or-Promote — arXiv 2604.19049](https://arxiv.org/abs/2604.19049) *(abstract depth)*
- [Are LLMs Reliable Code Reviewers? Systematic Overcorrection — arXiv 2603.00539](https://arxiv.org/abs/2603.00539) *(abstract only; no rate given)*
- [Trajectory Guard — arXiv 2601.00516](https://arxiv.org/pdf/2601.00516) *(abstract depth)*
- [PrefixGuard — arXiv 2605.06455](https://arxiv.org/pdf/2605.06455) *(abstract depth)*
- [From Agent Traces to Trust (survey) — arXiv 2606.04990](https://arxiv.org/pdf/2606.04990) *(abstract depth)*
- [BenchJack — auditing agent benchmarks — arXiv 2605.12673](https://arxiv.org/pdf/2605.12673) *(abstract depth)*

Fetched but unusable (PDF text extraction failed; not used for any claim):

- arXiv 2606.17182 — Verified Detection and Prevention of Concurrency Anomalies in Multi-Agent LLM Systems
- arXiv 2603.29231 — Beyond pass@1: A Reliability Science Framework for Long-Horizon LLM Agents

Repository, read directly: `CLAUDE.md`, `docs/orchestrator-mandate.md`,
`skills/hanig-review-gate/PROTOCOL.md`, `skills/hanig-project/SKILL.md`,
`docs/tracker-outbox.md`, `docs/plan-field-reports.md`, and
`skills/hanig-swarm/scripts/swarm.py` (outline plus the lease, state, digest,
continuation, watcher and status sections cited above).
