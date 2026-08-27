# Plan v3 — compare like with like, and bind what we can bind

Third revision. v1 special-cased sacct's coarse timestamps at two call sites
(rejected: patches instances, leaves the class). v2 proposed deleting the
timestamp comparison entirely (rejected: binding a job ID establishes that this
contract CLAIMS id 123, not that sacct's row FOR 123 is ours — if Slurm reused
the id, the row may be the older job, and Submit was the only thing separating
them).

## Root cause, corrected

Not "timestamps are used" — v2's claim, and wrong. The defect is that the
comparison **puts two incomparable things on either side**: a whole-second
scheduler Submit against a sub-second contract-declaration epoch. Every repair
of a mismatched comparison is a threshold, and a threshold fails open at one
end (D1: unparseable Submit skips the check) or closed at the other (D2:
whole-second Submit rejects an honest same-second submission).

Fix the comparison, not the threshold: compare sacct's Submit to **our own
recorded submission time**. `contract.py cmd_submit` already writes
`submitted_at` into `attempts.jsonl` alongside `contract_id` and `job_id`. Both
sides are then whole-second ISO describing the same event class, so there is no
precision mismatch left to special-case, and the question becomes the one we
actually care about: did this sacct row exist before we submitted?

## The change

1. **Compare Submit to the owning attempt's `submitted_at`**, in both
   verifiers, at second resolution. Not to `created_at` / `created_at_epoch`.
2. **Unparseable or absent Submit => the row is not terminal evidence.** We
   cannot confirm it is ours. A recorded local termination still is, and the
   reason says which. (D1, fail-closed, with a principled basis rather than a
   tuned one.)
3. **`traincontract.py bind <run_dir> --job-id N`** — writes a
   `contract_id`-bound attempt with `submitted_at`, mirroring
   `contract.py submit`, and re-stamps `criteria_digest`. This is what gives
   training runs an owning attempt to compare against at all.
4. **A job id captured at `init` from `$SLURM_JOB_ID` counts as bound**, with
   the contract's own `created_at` as its `submitted_at`: `init` ran inside
   that job, so the binding is genuine. This is a supported workflow today and
   must not require a redundant `bind`.
5. **`run.slurm_job_id` joins `DIGESTED_FIELDS`** once 3 and 4 exist.
6. **Close the `owned_attempts` absent-id bypass** (`contract.py:714`), which
   returns ALL attempts when `contract_id` is null — the same absent-field
   bypass already closed for `criteria_digest`. No migration concern: the repo
   has two commits, both from today, and has never been released.

## Acceptance criteria

1. A bound job supplies scheduler evidence whatever its Submit says, PROVIDED
   Submit does not predate the binding.
2. A sacct row that predates the binding never supplies terminal evidence,
   whatever its State or ExitCode. This is the job-id-reuse case.
3. An absent or unparseable Submit yields INCOMPLETE_EVIDENCE naming the cause,
   never a pass, and never a FAILED verdict.
4. `init` then `submit`/`bind` then `check` inside one wall-clock second passes
   **on a filesystem with sub-second mtimes**. On a whole-second filesystem the
   artifact rule still requires a later second, deliberately — criterion 8.
   (v2 stated this unqualified, contradicting its own criterion 6, which is
   precisely the fault v1 was rejected for. Twice is a pattern: state the
   qualification.)
5. A contract with a job id and no binding of either kind reports
   INCOMPLETE_EVIDENCE naming `bind`; a contract whose id came from `$SLURM_JOB_ID`
   at init does NOT, per change 4.
6. `bind` is refused when the contract is missing, unreadable, or already
   terminal, and is idempotent for the same job id.
7. Both verifiers do this identically; the comparison exists in one helper, not
   two copies.
8. Filesystem-mtime freshness is untouched. It was never the bug.
9. No existing test changes meaning to accommodate the change.

## Out of scope
Unsandboxed `command` predicates. Nextflow/Snakemake back ends. Any migration
path for contracts predating today.
