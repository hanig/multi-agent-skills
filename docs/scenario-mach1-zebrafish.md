# Scenario: "we want to do project X" — the whole string of events

> Walked end to end by gpt-5.6-sol against the built code, 2026-08-28. Where a
> plan step is unbuilt it says **"today Hani does X by hand"** rather than
> narrating fiction. That is the point: walking it exposes where the design is
> thin.

Project: fine-tune Mach-1 on a zebrafish split, three seeds, against a frozen
baseline, capped at 160 H100-hours.

> **CORRECTION, Hani 2026-08-28.** This walkthrough spreads the DAG across
> chimera, lambda and andromeda. That is NOT the intended use: skills are
> installed on each server and a project runs entirely on ONE of them. Read the
> dispatch section as "these are the unit shapes", not "these are three
> clusters". Cross-cluster portability was invited by my problem statement and
> is now out of scope — see step 5 of docs/plan-swarm.md.

## Hani starts it

Hani says:

> Start a swarm project to fine-tune Mach-1 on the zebrafish split. Three seeds, compare against frozen baseline, maximum 160 H100-hours. Do not publish or overwrite canonical results without asking me.

Today this is just a conversation. Step 7 integration is not built.

## The grill

Before any ticket or job, the system extracts these decisions:

- Exact checkpoint path and SHA-256.
- Dataset release, train/validation/test identities, and leakage policy.
- Primary metric and minimum meaningful improvement.
- Whether three seeds support the intended claim.
- Convergence and divergence rules.
- Maximum reserved and actual GPU-hours.
- Whether preemptible partitions are permitted.
- Environment/container digest.
- Canonical promotion destination.
- Who may approve cost increases, scientific changes, and promotion.

Recommended defaults:

- Primary endpoint: held-out macro-AUROC.
- Success: mean improvement ≥0.02 and paired-bootstrap 95% CI lower bound >0.
- Convergence: validation loss movement below 0.2% for five evaluations, after at least 10,000 steps, but only after a minimum AUROC threshold is met.
- Divergence: non-finite loss or declared loss ceiling.
- No test-set access until hyperparameters are frozen.
- Non-preemptible training initially; preemptible evaluation allowed.
- No automatic budget override.

A key correction: `converge.py` treats a bad flat run as plateaued by design. Plateau alone cannot mean scientific success. It must be combined with a quality threshold or treated merely as “stop training.”

## Linear and GitHub

Target design:

1. Create Linear project `Mach-1 zebrafish fine-tune`.
2. Preview and obtain approval for four research issues:

   - Freeze dataset/checkpoint/training contract.
   - Run baseline and three-seed fine-tuning.
   - Evaluate and aggregate held-out results.
   - Produce reviewed comparison report.

3. Create a GitHub branch/PR only for code and configuration changes. Do not create duplicate GitHub issues.

Not built: today Hani or an agent using `linear-issues` previews each issue, waits for `go`, then files it through Linear. Project creation and issue-to-unit generation are manual. Actual issue IDs cannot be known until filing; call them `<ML-CONTRACT>`, `<ML-TRAIN>`, `<ML-EVAL>`, and `<ML-REPORT>`.

## The DAG

```text
freeze-contract
├── validate-data
└── baseline-eval
    └── train-seed-0 ─┐
        train-seed-1 ─┼── evaluate-all ── aggregate ── write-report ── promote
        train-seed-2 ─┘
```

More precisely, all training units depend on `freeze-contract` and `validate-data`; `evaluate-all` depends on the baseline and all three training units.

The initial target states should be `PLANNED`, then `READY` or `WAITING`. Today unstarted units have no durable state and display `-`.

## Dispatch

Intended resource requests:

- `validate-data`: chimera CPU, 16 CPUs, 64 GB, 2 hours.
- `baseline-eval`: lambda, 1 GPU, 8 CPUs, 64 GB, 2 hours.
- `train-seed-{0,1,2}`: andromeda `h100-reserved`, 2 H100s, 16 CPUs, 256 GB, 24 hours each.
- `evaluate-all`: lambda, 1 GPU, 16 CPUs, 128 GB, 6 hours.
- `aggregate`: chimera CPU, 16 CPUs, 64 GB, 2 hours.
- `write-report`: isolated Paseo code-agent worktree.

Step 5 is not built. Today one plan cannot submit locally to all three clusters. Hani must split it into per-cluster plans and manually carry completion evidence across their boundary. The current plans must contain raw cluster-specific `sbatch` flags.

On each applicable login node, today he runs:

```bash
python3 ~/multi-agent-skills/skills/hanig-swarm/scripts/swarm.py validate \
  /shared/goodarzilab/projects/mach1-zfish/swarm-plan.json

python3 ~/multi-agent-skills/skills/hanig-swarm/scripts/swarm.py run \
  /shared/goodarzilab/projects/mach1-zfish/swarm-plan.json \
  --state-dir /shared/goodarzilab/swarm-state/mach1-zfish/state \
  --root /shared/goodarzilab/swarm-state/mach1-zfish/runs
```

For a submitted unit, the actual path becomes something like:

```text
/shared/goodarzilab/swarm-state/mach1-zfish/runs/train-seed-0/8e41c18d0ae3b643/
├── unit.json
├── events.jsonl
├── submitted.json
├── job.sbatch
├── metrics.jsonl
├── checkpoint.pt
└── receipt.json
```

Its state moves:

```text
(no state) → ALLOCATED → SUBMITTED → RUNNING → DONE
```

## What runs unattended

Target design: a deterministic timer invokes `advance` every 5–15 minutes, protected by a project lease. It reads state, checks live jobs, advances dependencies, writes a scheduler-run record, and exits.

Step 4 is not built. Today nothing wakes it. Hani must run:

```bash
python3 ~/multi-agent-skills/skills/hanig-swarm/scripts/swarm.py advance \
  /shared/goodarzilab/projects/mach1-zfish/swarm-plan.json \
  --state-dir /shared/goodarzilab/swarm-state/mach1-zfish/state \
  --root /shared/goodarzilab/swarm-state/mach1-zfish/runs
```

Paseo 0.6.1 can create schedules and heartbeats, but no swarm schedule registration has been implemented or proven on the cluster login nodes.

## What Hani sees

Today, from a terminal:

```bash
python3 ~/multi-agent-skills/skills/hanig-swarm/scripts/swarm.py status \
  /shared/goodarzilab/projects/mach1-zfish/swarm-plan.json \
  --state-dir /shared/goodarzilab/swarm-state/mach1-zfish/state
```

That shows unit, kind, state, job ID, declared GPU-hours, and attempt count.

Not built:

- No Mac dashboard for Slurm units.
- No phone view.
- No stale-state age.
- No projected budget.
- No alerts.
- Paseo can show the report-writing agent, but not the Slurm DAG as one project.

The first useful operator version should expose the same state through `status --json` and send Paseo notifications for `NEEDS_HUMAN`, budget holds, terminal failures, and stale clusters.

## Human intervention points

A human must intervene at:

1. Scientific contract approval—the system cannot decide what biological claim is meaningful.
2. Linear/project creation approval—these are outward-facing shared-workspace mutations.
3. Initial plan approval—exact DAG, costs, inputs, resource requests, and promotion scope become frozen.
4. Any `NEEDS_HUMAN` scientific ambiguity—dataset mismatch, metric change, leakage concern.
5. OOM, timeout, divergence, repeated preemption, or requested budget increase.
6. Promotion into the canonical result tree.
7. PR merge and final Linear closure.

Ordinary dependency advancement and explicitly transient preemption retries should not require a human.

## Failure, preemption, and plateau

- **Ordinary failure:** Today `unit.py` returns `FAILED`; downstream units become `HELD`. There is no general retry policy.
- **Preemption:** On the next manual `advance`, the coordinator creates a fresh attempt, up to `max_attempts`. This works only while a human continues advancing it.
- **Accounting defect:** Retries are not cumulatively charged in today’s state; the unit retains one declared GPU-hour charge.
- **Missing output after clean exit:** Today this returns `INCOMPLETE` forever rather than terminal failure.
- **Cluster outage:** Missing accounting rows also return `INCOMPLETE`; no outage detector distinguishes this from normal accounting delay.
- **Plateau:** `converge.py` is not integrated into `swarm.py`. Hani must run it manually against each `metrics.jsonl`:

```bash
python3 ~/multi-agent-skills/skills/hanig-swarm/scripts/converge.py check \
  /shared/goodarzilab/swarm-state/mach1-zfish/runs/train-seed-0/<attempt>/metrics.jsonl \
  --criterion '{"metric":"val_auroc","mode":"max","threshold":0.78,"min_steps":10000}' \
  --diverge '{"metric":"train_loss","above":1000}' \
  --budget 40000 \
  --json
```

A job may therefore be `DONE` according to `unit.py` while convergence is `BUDGET_EXHAUSTED`. Today Hani must manually stop the DAG from treating that checkpoint as successful. The sharpened plan should support multiple predicates per unit and require all declared predicates to pass.

## Promotion

Step 3 promotion is not built, and current receipts do not contain output digests, so criterion 3(f) cannot yet be implemented as written.

Today Hani must manually:

1. Inspect the unit receipt and scientific aggregation.
2. Generate a digest manifest.
3. Copy into a staging directory on the canonical filesystem.
4. Verify the copied manifest.
5. Rename staging to a versioned release directory.
6. Atomically replace a `current` symlink.
7. Record approver, attempt IDs, digest, and time.

The run directory should remain immutable after promotion; promotion should never consume or rename the evidence directory itself.

## Issue closure

The aggregate issue closes only after:

- All required unit receipts are `DONE`.
- Every training run has a separate acceptable convergence verdict.
- `comparison.json` records checkpoint, dataset, environment, seed, metric, and confidence interval.
- The report names failed/retried attempts and budget consumed.
- Independent review accepts the exact report/code commit.
- Any PR is merged.
- Hani explicitly approves the Linear close patch.

Not built: today the Resolution, Acceptance evidence, links, and `Done` transition are drafted and applied manually through `linear-issues`.

## What Hani has at the end

He has:

- Three fine-tuned checkpoint attempt directories.
- A frozen baseline result.
- Per-attempt Slurm receipts and event histories.
- Convergence/divergence verdicts.
- A comparison artifact with seed-level results and confidence interval.
- A reviewed report and, where needed, a merged code/config PR.
- A versioned canonical release.
- Linear issues whose closure cites the evidence.

The system will not have established:

- That the result generalizes beyond this held-out zebrafish dataset.
- That three seeds provide adequate power for every biological conclusion.
- That the dataset construction was scientifically correct merely because its file digest matched.
- That no process under the same Unix account wrote into an attempt directory.
- That directory inputs or mutable external services were reproducibly pinned.
- That a pipeline’s internal steps were individually correct.
- That `DONE` alone means a model converged or beat baseline.
- That promotion implies publication, deployment, or biological validity.

The immediate next milestone should be one scheduled, single-writer, crash-injected Slurm DAG on lambda—not the dashboard or ticket abstraction. After that works unattended for several days, make the same plan run through cluster profiles on andromeda and chimera, then connect Linear.
