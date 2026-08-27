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
- *Is this ROW ours, or another job's reuse of the id?* Judged by an
  INTERVAL, at second resolution, failing closed when Submit cannot be parsed:

      declared_at  <=  Submit  <=  bound_at

  Both bounds are load-bearing, and each alone failed in the opposite
  direction. Lower only: a row from a LATER reuse also post-dates the
  declaration, so it was attributed to us -- which let a later clean COMPLETED
  row certify a training run that had actually exited non-zero (sol; a false
  pass) and made a later FAILED row report an honest successful run as FAILED.
  Upper only: `bind` runs after `sbatch`, so the honest Submit is always
  earlier than the moment we recorded, and every real submission was discarded.
  `bound_at` is recorded after the scheduler returned the id, so our Submit
  cannot be later than it. One second of slack each way, because both sides are
  whole seconds and each can sit up to a second off the real instant: sbatch at
  12:00:00.999 records 12:00:00 while sacct rounds Submit to 12:00:01, and an
  exact comparison refused that honest row (deepseek).

  An attempt to remove the interval was made and REVERTED. `submit` and `bind`
  asked the scheduler for the job's own Submit when they recorded the id and
  compared against that, making ownership an equality. sol showed it was
  unsound: if the id had already been reused before that query ran, the query
  returns the OTHER job's row, which then becomes the anchor, and the reused row
  matches itself and certifies the run. The anchor was drawn from the very
  source it was meant to validate. There is no way to establish after the fact
  which row is ours, so the interval is the honest maximum.

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

- **A reuse landing inside the ownership interval is indistinguishable from our
  own submission.** The window is exactly the gap between submitting and
  binding, so **bind promptly**: `contract.py submit` closes it to
  milliseconds because it runs sbatch itself, while `traincontract.py bind` is
  as wide as the delay before you run it. A reuse within one second of our
  submission is indistinguishable regardless, because sacct emits whole seconds
  and the slack is a second wide.
- **Ownership assumes the verifier host and the Slurm controller agree on the
  time to within the slack.** Our anchors come from the login host's clock and
  Submit comes from slurmctld, so skew shifts the whole interval: an honest row
  is refused at one end and a reuse admitted at the other (sol). Widening the
  slack is NOT the fix, because it widens the reuse window by the same amount.
  Clusters run NTP, and skew large enough to matter here would already be
  breaking Slurm's own scheduling and accounting. Stated as a requirement.
- **squeue rows are subject to the same ownership test as sacct rows.** They
  were not for eight rounds: `-o %T` fetched only the state, so a later reuse
  of the id sitting in the queue turned an honestly finished run back into
  RUNNING with its predicates never evaluated. Both verifiers now request
  `%T|%V` and place the row.
- **A TensorFlow checkpoint must be wholly from this run, not just complete.**
  Freshness applies to EVERY component of the selected set. Requiring only
  completeness plus a fresh newest file let a previous run's index and shard 1
  pair with one freshly written shard 0: complete by count, fresh by newest,
  and a mixture that loads to nothing (kimi, finding the half of sol's report
  the first fix missed).
- **A TensorFlow checkpoint must have every shard its own name declares.**
  `model.data-00000-of-00002` says two; accepting the set because each file had
  *any* counterpart let a stale `.index` from a previous run pair with one
  fresh shard and certify a run with no loadable model.
- **A DST fold can displace an offset-free sacct Submit by an hour.** In the
  ambiguous hour, an offset-free local timestamp has two valid readings and
  libc picks one; an honest row can land outside the interval and be refused
  (sol). One hour a year, fail-closed, and fixable only by making sacct emit
  offsets (`SLURM_TIME_FORMAT`). Accepted
  deliberately, because refusing it would reject every job submitted in its
  contract's second.
- A job queued before its contract was declared — `init` running inside a job
  that waited in the queue — fails closed. `record` is the way through.
- **Two runs interleaved with monotonic, non-duplicate, evenly-spaced steps are
  not detectable** from the metrics file alone (luna). The integrity check
  catches backwards steps, duplicates and gaps, which is every interleaving
  that leaves a trace in the step sequence; an interleaving that does not
  leave one is indistinguishable from a single noisy run. A threshold
  criterion is the exposed case, since one run's row can satisfy it. A
  `rel_improvement_below` plateau criterion is not, because alternating
  values do not plateau. Prefer plateau criteria over bare thresholds.

## Out of scope
Unsandboxed `command` predicates. Nextflow/Snakemake back ends. Any migration
path for contracts predating today.
