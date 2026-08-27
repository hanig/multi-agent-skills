# Plan — establish Slurm job ownership, stop inferring it from timestamps

Four revisions, three of them rejected by plan review before any code existed
and one rejected after implementation. The history is kept because each
rejection names a distinct failure mode worth not repeating.

## Root cause, corrected (v4 — what was actually built)

v1 special-cased sacct's coarse timestamps at two call sites (rejected: patches
instances, leaves the class). v2 proposed deleting the timestamp comparison
entirely (rejected: binding a job id proves this contract CLAIMS id 123, not
that sacct's row FOR 123 is ours). v3 proposed comparing Submit against our own
recorded bind time — **and that was implemented and found wrong in Phase 3**:
`bind` runs AFTER `sbatch`, so the honest Submit is always EARLIER than the
moment we recorded it, and every real submission was discarded as a reused id.

The defect was never that a timestamp is used. It was that the comparison put
two incomparable things on either side: a whole-second scheduler Submit against
a sub-second contract-declaration epoch. Repairing a mismatched comparison only
ever yields a threshold, and a threshold fails open at one end (D1, unparseable
Submit skipped the check) or closed at the other (D2, whole-second Submit read
as predating a declaration made later in the same second).

**Two independent questions, one of which is not a timestamp at all:**

- *Which job id is ours?* Established by the BINDING — `contract.py`'s attempts
  log, `traincontract.py`'s `bind` — which records the id against this contract
  instance. Established, not inferred.
- *Is this ROW from a prior job that reused the id?* Judged against the
  contract's DECLARATION, at second resolution, failing closed when Submit
  cannot be parsed. Our job is submitted after the contract is declared, so a
  row predating the declaration belongs to an earlier job.

## The change

1. `sacct_row_is_ours(sacct_submit, declared_at)` in both verifiers, one copy
   each, identical semantics.
2. Absent or unparseable Submit => the row is not terminal evidence, and the
   reason says so specifically rather than diagnosing reuse.
3. `traincontract.py bind <run_dir> --job-id N` — writes
   `training-binding.json` with the job id bound to `contract_id`. Refuses an
   already-terminal contract (filtered by `termination_matches`, so a receipt
   left by `init --force` does not block an honest new run) and a different id
   without `--force`; preserves the first timestamp when re-binding the same
   id; requires an id shaped like a Slurm id.
4. A job id captured at `init` from `$SLURM_JOB_ID` counts as bound.
5. `run.slurm_job_id` is digested via `DIGESTED_RUN_KEYS`; the rest of `run`
   (hostname, user, python, NCCL env) is not, because digesting it made an
   unrelated edit read as post-hoc criterion selection.
6. `contract.py`'s `owned_attempts` no longer returns ALL attempts when
   `contract_id` is absent.

## Acceptance criteria (v4)

1. A bound job supplies scheduler evidence for any honest ordering:
   declaration, then `sbatch`, then `bind`, then `check`, with Submit anywhere
   at or after the declaration second. **v3 stated this against the BINDING
   time, which is what made the implementation reject every honest run.**
2. A row whose Submit predates the declaration second never supplies terminal
   evidence.
3. An absent or unparseable Submit yields INCOMPLETE_EVIDENCE naming that
   cause, never a pass and never FAILED.
4. `init` then `record` then `check` inside one wall-clock second passes **on a
   filesystem with sub-second mtimes**; on a whole-second filesystem the
   artifact rule still requires a later second, deliberately (criterion 8).
5. A contract with no binding of either kind reports INCOMPLETE_EVIDENCE naming
   `bind`.
6. `bind` is idempotent for the same id, refuses a different id or an
   already-terminal contract without `--force`, and rejects a malformed id.
7. One helper per file, identical semantics, no third copy.
8. Filesystem-mtime freshness untouched. It was never the bug.
9. No existing test keeps its old meaning by accident: six tests were passing
   only because the absent-Submit case failed open, and their fixtures now
   emit the Submit column real sacct emits.

## Known limits, stated rather than papered over

- A reuse INSIDE the declaration second is indistinguishable from an honest
  same-second submission: sacct emits no finer precision. Accepted
  deliberately, because refusing it would reject every job submitted in its
  contract's second.
- A job queued before its contract was declared — `init` running inside a job
  that waited in the queue — fails closed. `record` is the way through.

## Out of scope
Unsandboxed `command` predicates. Nextflow/Snakemake back ends. Any migration
path for contracts predating today.
