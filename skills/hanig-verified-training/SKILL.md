---
name: hanig-verified-training
description: >-
  Declare convergence before training and verify it after. Use when launching,
  resuming, monitoring, or judging a training or fine-tuning run, when selecting
  a checkpoint, and whenever the question "did it converge?" or "is the model
  trained?" comes up. Separates converged from budget-exhausted, diverged, and
  preempted — four outcomes that every dashboard reports identically. Not for
  choosing architectures or hyperparameters, and not for inference-only work.
---

# hanig-verified-training

"The model trained" hides four outcomes that tooling reports the same way:

| | |
|---|---|
| **converged** | a criterion declared *in advance* was met |
| **budget exhausted** | the step or wall-clock limit arrived first |
| **diverged** | loss went non-finite, or breached a declared ceiling |
| **preempted** | the scheduler took the node back |

Only the first is success. And it is the one you cannot honestly determine
afterwards: picking the best checkpoint once you have seen the curve is
legitimate, but it is **selection**, not convergence, and the receipt says which
one happened.

## Commands

```bash
T=~/.claude/skills/hanig-verified-training/scripts/traincontract.py

python3 $T init <run-dir> --metrics metrics.jsonl --checkpoint-dir ckpt/ \
  --converge '{"metric":"val_loss","mode":"min","rel_improvement_below":0.002,
               "over_evals":5,"min_steps":10000}' \
  --diverge  '{"metric":"train_loss","above":100}' \
  --max-steps 200000 --expect-eval-every 500

python3 $T check <run-dir> [--json]
```

`init` **refuses to write a contract with no convergence criterion** unless you
pass `--retrospective`. Without one, this tool can only report that training
stopped — never that it converged, which is the whole question.

## Exit codes

| Exit | State | Meaning |
|---|---|---|
| 0 | `CONVERGED` | A pre-declared criterion was met |
| 1 | `RUNNING` | Still going |
| 2 | `DIVERGED` | Non-finite metric, or a declared ceiling breached |
| 3 | `BUDGET_EXHAUSTED` | **Limit reached without meeting the criterion** |
| 4 | `CONTRACT_VIOLATED` | Metrics unusable — steps non-monotonic or duplicated |
| 5 | `PREEMPTED` | Requeued; another attempt expected |
| 6 | `INCOMPLETE_EVIDENCE` | No metrics, or no loadable checkpoint |

**`BUDGET_EXHAUSTED` is the state this skill exists for.** Hitting the step
limit is not convergence. Report it as what it is.

## Criteria

Two shapes, both declared before the run:

```jsonc
// plateau: relative improvement below a floor, sustained
{"metric":"val_loss","mode":"min","rel_improvement_below":0.002,
 "over_evals":5,"min_steps":10000}

// threshold: an absolute bar
{"metric":"val_auroc","mode":"max","threshold":0.90,"min_steps":5000}
```

`min_steps` guards against a flat opening being read as a plateau — early
training is often briefly flat, and without it a criterion can be met before the
run has done anything.

## Metrics format

JSONL, one object per evaluation, each with `step` plus the metric keys the
criterion names:

```jsonl
{"step": 500, "val_loss": 3.21, "train_loss": 3.44}
{"step": 1000, "val_loss": 2.87, "train_loss": 3.01}
```

Deliberately **not** tied to W&B or TensorBoard — a login node with no egress
still has to be able to verify. Adapters can emit this from either.

Steps that go backwards or repeat are `CONTRACT_VIOLATED`, not a warning: they
mean two runs are writing to one file, and no convergence verdict read off it
describes a single run. `--expect-eval-every N` additionally flags holes.

## Checkpoints

A converged curve with nothing loadable on disk is not a trained model —
that returns `INCOMPLETE_EVIDENCE`. Files ending `.tmp`, `.part`, `.partial`,
`.incomplete`, `.lock`, `.writing`, or of zero bytes are treated as partial and
do not count.

`check` records **which storage tier** the checkpoint sits on, because
"retained" and "on the hot tier" are different facts. From the 2026-08-25 probes:
andromeda's `/mnt/weka` was at **97% (3 TB free of 80 TB)** serving 256 H100s,
lambda's `/checkpoints` at 90% — while both 1 PB cold tiers sat at 0% used. A
large run can fail on write, and a checkpoint you assume is safe may be on the
tier that is about to fill.

## Honesty rules

- **Reaching `--max-steps` is `BUDGET_EXHAUSTED`.** Never call it converged.
- **Post-hoc checkpoint selection is selection.** `check` reports the newest
  checkpoint and says so explicitly; choosing a different one after seeing the
  curve is fine but must be labeled.
- **`--retrospective` cannot establish convergence**, only document a past run.
  It is labeled in the contract and every receipt.
- **A non-finite metric beats a plateau.** A flat NaN is not a converged model.

## What this does not do

It cannot tell you the model is *good* — only that it met a criterion you chose
in advance. Whether that criterion was the right one, whether the validation
split was appropriate, and whether the result is biologically meaningful all
remain yours. Nothing here evaluates a model; it evaluates a claim about one.
