# Slurm execution evidence

The done predicate remains: declared outputs in the exclusive attempt root,
the coordinator-pinned pre-dispatch basis proving they were absent or changed,
and terminal-OK execution evidence for this attempt. This establishes neither
process attribution nor scientific correctness.

## Source precedence

1. Query `scontrol show job -o JOB_ID`. Apply `sacct_row_is_ours` to SubmitTime
  with the same declaration/bind interval, and preserve `SLURM_OK`, failure,
  preemption and clean-exit rules. EndTime ordering is diagnostic only. <!-- declaration: isolation.done-predicate -->
  Match the numeric JobId or exact ArrayJobId/ArrayTaskId pair for a bound task.
  Ambiguous one-line fields trigger one `scontrol show job JOB_ID` query.
  Its native line groups provide a complete replacement snapshot, not a mix of
  responses. Unrelated metadata, including spaced names, is not parsed.
  Both queries capture bytes before decoding, preserving native LF boundaries
  without converting carriage returns inside opaque metadata into new lines.
  Duplicated groups or failed refinement stay unknown and never enable <!-- declaration: isolation.done-predicate -->
  accounting, even if the job disappears between the two queries.
2. Only controller absence, or an unavailable `scontrol` executable, permits <!-- declaration: isolation.done-predicate -->
  `sacct`. A query failure, malformed row or ownership mismatch is not a <!-- declaration: isolation.done-predicate -->
  purge. In particular, a requeue whose reset SubmitTime falls outside the
  anchored interval stays incomplete; ownership is never widened. <!-- declaration: isolation.done-predicate -->
3. If accounting selects no owned row, an authorized exit record may supply
  the last fallback. An owned row, even one with a missing state, blocks this
  weaker evidence. Unowned historical rows neither certify nor veto it.

Accounting queries have a 20-second timeout. One unsuccessful query disables
accounting for the remaining coordinator pass, including checker subprocesses
and orphan reconciliation. A private coordinator-owned descriptor carries the
outage indication, independently of receipt writes. The next `advance` tries
again. Missing evidence never means the job itself failed. <!-- declaration: isolation.done-predicate -->

## Attempt-scoped capability

Only fresh Slurm allocation opts into `slurm_exit_record` in the write-once <!-- declaration: compatibility.judgment-generation -->
coordinator artifact basis: `{"version": 1, "path": "slurm-exit.json"}`.
It is persisted before dispatch and passed by value to the checker. Existing
bases are never backfilled. Neither `unit.json`, a generated script, a receipt, <!-- declaration: compatibility.judgment-generation -->
nor the presence of an apparent exit record can grant this capability.

One policy governs trap generation, metadata reservation and fallback. A
present capability must have the recognized version/path and a valid basis <!-- declaration: compatibility.judgment-generation -->
bound to this unit and attempt before either DONE or FAILED can come from its
record. Invalid authority is an explicit refusal, not a downgrade to legacy. <!-- declaration: compatibility.judgment-generation -->

Absent capability means a legacy, scheduler-only attempt. It retains its old <!-- declaration: compatibility.judgment-generation -->
payload namespace: an already-running job can legitimately produce a payload
named `slurm-exit.json`, but that file is never interpreted as execution <!-- declaration: compatibility.judgment-generation -->
evidence. Legacy dispatch does not remove or overwrite such a payload with a <!-- declaration: compatibility.judgment-generation -->
new wrapper. No exit status is reconstructed after the fact.

Prospective `validate` reserves the metadata namespace. Operational
`run`/`advance` loading skips only that new restriction so legacy attempts <!-- declaration: usage.outputs -->
remain judgeable; all earlier plan validation still applies. Fresh allocation,
including retries, enforces the reservation against its prospective root
before creating an attempt. This is not a plan-controlled exemption.

## Exit records

Capable batch scripts atomically write `job_id`, `exit_status` and `end_time`
to `slurm-exit.json` from a native batch-shell `EXIT` trap. Array tasks also
carry `array_job_id` and `array_task_id` for exact composite-binding matches.
The `restart_count` field preserves `SLURM_RESTART_COUNT` as a digit string,
defaulting to `"0"` when unset and empty when malformed. It is diagnostic data.
Job, array and restart identity are captured as quoted literals when the wrapper
starts, before the payload can change its environment. Trap setup uses isolated
positional bookkeeping, preserving the payload's variables and arguments.
Record initialization follows the original builtin prologue: changing directory
and exporting all unit/dependency values precede every wrapper-added child.
Clearing runs at this hook, before payload execution.
Normal startup and later record invalidations use the shell's standard utility path,
not the payload PATH or command cache. Startup clearing failure still stops
execution rather than retaining stale evidence; optional recording cleanup runs
in isolation, preserving the payload's status even when cleanup fails.
A clean Bash helper generates default handlers without sourcing startup files
or using the parent shell's IFS. Installation leaves existing native handlers
intact. A preinstalled EXIT handler is retained instead of the recorder, leaving
no wrapper record for that invocation after startup clearing.
Startup xtrace, functrace, extdebug or a native CHLD disposition selects a
child-free opt-out before captures or cleanup children. The direct GNU Bash
`trap -p CHLD` probe temporarily closes stdout: printing a disposition fails,
whereas an unconfigured non-POSIX disposition emits nothing and succeeds.
This Bash-specific output-status behavior has executable coverage. Handler
bodies are neither stored nor evaluated, stdout is restored, and native child
notifications are not suppressed. POSIX default-disposition output conservatively
selects opt-out as well.
Opt-out empties an existing nonempty regular record using a builtin, preserving
native noclobber mode while overriding it for this reserved metadata path.
Missing and empty records are left unchanged. Observable symlinks and nonregular
destinations trigger a startup error; this is not a race-proof no-follow boundary against
same-UID writers. Failed invalidation stops startup. The filename may remain
empty instead of being unlinked; empty data is already absent evidence to the
unchanged reader. No recorder or default traps are installed on this path,
and judging relies on scheduler evidence.
Capable wrappers give otherwise-unhandled fatal signals explicit `128+signal`
exits. Payload-installed handlers still run in the native batch shell and can
replace these defaults. Inherited ignored signals, normal Bash `SIGQUIT`
ignore, and stop/continue/child dispositions are left alone. Default `SIGINT`
aborts rather than using Bash's cooperative foreground-command behavior.
The isolated recorder stages JSON; only its successful completion permits <!-- declaration: compatibility.judgment-generation -->
atomic publication. Default fatal handlers invalidate published records, so
signals during recording do not leave a false zero. Top-level `exec` or <!-- declaration: compatibility.judgment-generation -->
replacement of the `EXIT` trap can prevent recording; that is absent evidence,
never invented success or failure. Record initialization, <!-- declaration: compatibility.judgment-generation -->
including on requeue, invalidates the previous JSON evidence.

The fallback requires an all-states `squeue` query confirming absence: empty <!-- declaration: isolation.done-predicate -->
successful output or the explicit unknown-job diagnostic. An unresolved queue
error or any listed job blocks it. A matching id and valid zero status permit
the output/basis checks; nonzero means failure. Mismatched or malformed records
are absent evidence, never job failure. <!-- declaration: isolation.done-predicate -->

The record and its temporary-file namespace are not payload outputs. Capable
attempts cannot declare the entire root or an alias of this metadata; otherwise <!-- declaration: usage.outputs -->
the wrapper could manufacture its own production evidence. A similarly named
file inside a separate payload directory is not reserved.

Receipts identify `slurmctld`, `sacct` or `exit-record` in
`basis.exit_status_attested_by`. Exit records are explicitly job-wrapper
attestations, not scheduler authority or an OS boundary against same-UID
writers. SIGKILL and node loss may leave no record, but can also interrupt after
publication; the record does not prove the shell's eventual OS-level exit. <!-- declaration: isolation.done-predicate -->
These limits do not relax <!-- declaration: isolation.done-predicate -->
the pre-dispatch artifact basis or the coordinator's receipt-provenance checks.

## Requeue limitation

The owner accepts invocation-level exit records as explicitly weaker evidence.
A successful record can survive a later requeue that fails or is cancelled
before stale-record invalidation succeeds, including failure of the builtin prologue.
After controller purge and absent accounting evidence, the old record
and outputs can therefore certify success despite that unobserved requeue.
The recorded restart count describes the writer, not future incarnations.
Successful initialization invalidates the prior record before payload execution.
Units submitted with `--no-requeue` avoid this case when that option is honored;
Slurm's documented `PrologFlags=ForceRequeueOnFail` site override can defeat it.
The skill neither forces `--no-requeue` nor adds incarnation-tracking machinery.
