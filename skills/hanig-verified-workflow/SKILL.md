---
name: hanig-verified-workflow
description: >-
  Launch, monitor, and verify batch compute against a contract declared before
  the run. Use when submitting, resuming, retrying, or deciding whether a Slurm
  job, Nextflow pipeline, Snakemake workflow, or long-running analysis actually
  finished — especially when asked "did that job work?", "is it done?", "did the
  run succeed?", or when a job reports COMPLETED but the result looks wrong. Not
  for choosing partitions, accounts, or storage tiers (use the cluster's own
  skill), and not for editing pipeline code that will not be executed.
---

# hanig-verified-workflow

## Host capability boundary

Read project instructions through the current host's conventions, then use
only the shell/filesystem, Python, Git, and scheduler capabilities that are
actually available.  A missing program or denied approval is absent evidence:
state the blocked check and the resulting incomplete/unverified limit instead
of fabricating output or changing the host's permissions.  Paseo and the agent
bus are optional services, not prerequisites that this skill installs; their
portable fallback is documented in `docs/agent-compatibility.md`.

A scheduler reporting `COMPLETED` is not evidence that work was produced.

These all look like success and are not:

- Python catches an exception, logs it, and returns 0.
- A pipeline stage emits a header-only table.
- A training run hits its wall-clock limit and exits cleanly.
- A job re-runs against inputs that silently changed underneath it.
- A step is skipped because a stale cached intermediate looked fresh.

So completion is decided by `contract.py`, which did not do the work: a contract
is declared **before** the run, and a verifier checks it **after**, independently
of the job's own exit code.

## Never do this

Do not report a job as done based on `sacct` state, a zero exit code, a "done"
message in a log, or the job's own claim. Those are necessary, never sufficient.
If no contract exists, say what evidence you actually have and what you cannot
confirm — do not fill the gap with an assumption.

## The three commands

Set `HANIG_VERIFIED_WORKFLOW_DIR` to the directory containing the `SKILL.md`
instance this agent actually loaded. It is a normal shell variable, not a
Claude-only expansion; quoted commands work from any launch directory and keep
relative run, input, and output paths relative to that directory.

```bash
export HANIG_VERIFIED_WORKFLOW_DIR="/path/to/loaded/hanig-verified-workflow"
C="$HANIG_VERIFIED_WORKFLOW_DIR/scripts/contract.py"

python3 "$C" init   <run-dir> --command "..." [--output P] [--input P] [--predicate JSON]
python3 "$C" submit <run-dir> [--sbatch-arg ...]     # sbatches <run-dir>/job.sbatch
python3 "$C" check  <run-dir> [--json]               # verdict + receipt
```

`init` records, before anything runs: the command, git commit and a digest of
any uncommitted diff, environment identity (interpreter, conda prefix, Slurm
partition/account, any `NCCL_*` settings), input identities, declared outputs,
and the predicates that must hold.

`check` writes `verification.json` and exits with the state's code.

## Verification states

| Exit | State | Meaning |
|---|---|---|
| 0 | `SCIENTIFIC_PASS` | Terminal, and every declared predicate holds |
| 1 | `RUNNING` | Not terminal — re-check later |
| 2 | `FAILED` | Scheduler or wrapper reports failure |
| 3 | `TECHNICALLY_COMPLETE` | **Exited cleanly, produced nothing declared** |
| 4 | `CONTRACT_VIOLATED` | Inputs drifted, or ran outside declared scope |
| 5 | `PREEMPTED` | Requeued — another attempt is expected |
| 6 | `INCOMPLETE_EVIDENCE` | Cannot determine; missing logs or predicates |

**`TECHNICALLY_COMPLETE` is the state this skill exists for.** It is a failure
that every other tool reports as success. Surface it plainly, name which
predicates went unmet, and do not let it pass as done.

`PREEMPTED` is not failure — on a preemptible partition it is expected. Resubmit
and record a new attempt rather than reporting a problem.

## Writing predicates that catch real failures

Declared outputs (`--output`) auto-generate `exists` + non-empty checks. Those
are the floor, not the goal — an empty file and a wrong file both "exist".

Prefer **one cross-cutting invariant** over many shallow file checks. The useful
question is: *what would be true of this output if the science worked, and false
if it silently didn't?*

```bash
# Weak: the file is there.
--output results/de_genes.tsv

# Stronger: it has the rows it must have.
--predicate '{"kind":"min_lines","path":"results/de_genes.tsv","lines":1000}'

# Stronger still: a caught traceback did not masquerade as success.
--predicate '{"kind":"log_matches","path":"logs/job.log","pattern":"Traceback","expect":false}'

# Strongest: a domain invariant, via the shell escape hatch.
--predicate '{"kind":"command","run":"python3 qc.py --assert-cells-retained 0.9"}'
```

Predicate kinds: `exists`, `min_size`, `min_lines`, `log_matches`
(with `expect` true/false), and `command` (any shell exit code).

## Input identity at genomics scale

Digesting a multi-TB dataset is not viable, so `init` records the strongest
affordable rung and marks the weak ones:

1. `content-digest` — full sha256 (files under `--hash-limit-mb`, default 256)
2. `prefix-digest` — sha256 of the leading N MB — **weak**
3. `dir-mtime-size` — directory mtime and entry count — **weak**

Only `content-digest` inputs are re-checked for drift on `check`. When an input
is weak, say so rather than implying provenance you do not have.

## Retrospective contracts

`--retrospective` audits a run that already happened. It is labeled in the
contract and every receipt, and carries weaker assurance — criteria chosen after
seeing results are not the same as criteria declared before. Use it to document
history, never to claim a run was validated.

## Cluster notes

Measured 2026-08-25 on all three; `contract.py` passes its suite on each.

- **`sacct` works on chimera, lambda, and andromeda.** Scheduler history is used
  as primary terminal-state evidence, with predicates as the fallback that
  catches exit-0-with-no-output — which `sacct` structurally cannot see.
- Missing accounting data is treated as *absent evidence*, never as failure.
- **No `nextflow` or `snakemake` is installed system-wide on any login node.**
  Locate them per-project (usually a conda env); never assume a global binary.
- Usernames differ: `hani` on chimera and lambda, `hgoodarzi` on andromeda.
  Never hardcode `$USER` or a home path.
- Storage is tight — andromeda `/mnt/weka` is ~97% full, lambda `/data` ~93%.
  A run can fail on write. `min_size` predicates catch truncated output.

## What this skill does not do

It cannot tell you the result is scientifically correct — only that the run
produced what it declared. Interpretation stays with you. Choosing partitions,
accounts, and storage tiers belongs to the cluster's own skill; figure and table
provenance was `hanig-reproducible-result`, deleted 2026-08-28: figures and tables are not a swarm unit kind.
