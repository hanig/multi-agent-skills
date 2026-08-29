# Three candidate first workloads, one per machine

Drawn from the live Linear workspace `Arc - projects` rather than invented, so
the first real run produces something you actually wanted. Each is scoped to be
finishable and to exercise a different part of the coordinator.

The previous scenario doc (`scenario-mach1-zebrafish.md`) was hypothetical and
its multi-cluster DAG is explicitly NOT the topology. Prefer these.

## 1. chimera: ARC-32, SUPPA generateEvents (recommended first)

> "D: Script SUPPA generateEvents - reproduce the event universe (pinned, full
> run)" - Entwine, Backlog, Medium

The best first workload, because the thing being asked for IS a declared
artifact: a pinned, reproducible event universe. That is what the unit contract
exists to certify.

Shape: three `slurm` units on `cpu`.

    fetch-annotation  -> outputs [gencode.vXX.gtf]        (pinned, digested)
    generate-events   -> outputs [events/*.ioe]  needs [fetch-annotation]
    manifest          -> outputs [checksums.tsv] needs [generate-events]

What it exercises that nothing has yet: a genuinely long single unit, real
declared outputs of non-trivial size, and **promotion**. ARC-36 asks for a
"Provenance & checksum manifest for the canonical dataset", which is precisely
what `swarm.py promote` writes: unit, attempt, digest, approver, timestamp.
Point `promote_to` at the canonical Entwine tree and the ticket's deliverable
falls out of the mechanism.

Watch for: outputs over 256MB fall to size+mtime, so promotion will refuse
until you pass `--accept-weak-evidence`. That refusal is the design working,
and it is the first place you will feel whether the digest limit is set right.

## 2. lambda: CSA-RNA-FM corpus build

> "Phase 1: build a state-labelled isoform corpus (SRA-Curator -> Nextflow ->
> MPAQT -> single-cell)" - CSA-RNA-FM, In Progress

Shape: a `pipeline` unit wrapping the Nextflow stage, with `slurm` units either
side.

    sra-curate    kind=slurm     -> outputs [manifest.tsv]
    nf-quantify   kind=pipeline  -> outputs [publish/mpaqt/]   needs [sra-curate]
    assemble      kind=slurm     -> outputs [corpus.h5ad]      needs [nf-quantify]

Why lambda: the GPUs are here, and `--mem` is required here and nowhere else,
so it also proves the plan carries its own cluster's flags.

This is the **highest-information** run of the three, because the `pipeline`
kind has executed exactly once, on a `sleep`. A real Nextflow invocation will
test the detached launcher, the `engine.rc` wrapper and the directory-tree
digest all at once. Expect to find something.

Watch for: the wrapper records the FOREGROUND command's status. If your
Nextflow invocation backgrounds itself, the exit code will be the launch's,
not the run's. Declared outputs still protect you, but read the receipt.

## 3. andromeda: MultiDep re-run

> "The near-final target selection (TYMS/RAB10/CFLAR/ITGAV) is being re-run
> rigorously (lineage-decoupled, FDR-corrected, validated) before locking"

Shape: fan-out, one `slurm` unit per lineage split, then one aggregator.

    split-<lineage>   xN, independent, each -> outputs [shap/<lineage>.parquet]
    fdr-combine       needs all splits      -> outputs [targets.tsv]

What it exercises: width. Everything so far has been 2-3 units. This is the
first plan where the budget ceiling, `--max-new-dispatches` and the HELD
propagation matter, and where one failing split among twenty has to hold the
aggregator without stopping the other nineteen.

Watch for: andromeda's Weka home was 93% full. Set `budget.gpu_hours` low on
the first pass and let it halt; a halted swarm that says why is a better first
result than a full one.

## Order I would run them

chimera first: smallest, and its deliverable is a manifest, so a wrong answer
is obvious. Then lambda, because the pipeline kind is the least proven code in
the repo and you want that failure early. Then andromeda for width.

Run each with `--dry-run` first: it allocates and records without submitting,
so the DAG shape is checked before a single job is queued.
