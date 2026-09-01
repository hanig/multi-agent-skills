# Plan: the field-report items

> Source: three messages from Hani carrying reports from other agents that
> actually ran these skills, plus his own run. Every item is something that
> cost real time, not a style preference.
>
> This doc exists because that plan lived only in a conversation. The recurring
> lesson of the whole cycle is that an invariant which is not written down and
> enforced gets rediscovered one instance at a time; a plan which is not
> written down is the same failure at the project level.
>
> **Numbering:** the three source messages numbered independently, so items are
> prefixed by list. A = Hani's own eight, B = the second agent's nine plus a
> note, C = the third set, 10-14. `docs/plan-next.md` is a DIFFERENT and older
> plan (2026-08-30 committee, its own items 1-8, largely delivered); do not
> read the two numbering schemes as one.

Status verified against code on 2026-09-01, not against memory of the session.

## Done

| Item | What | Where |
|---|---|---|
| A1 | Outputs must live in the attempt's write root; validate refuses otherwise | `swarm.py` validate |
| A2 | A slurm command must be the work, not an `sbatch` invocation | `swarm.py` validate |
| A3 | Adopt path cannot destroy an existing `PLAN.md` | `survey.py` `protected_docs` |
| A5/B5 | Code units require `repo`, `branch`, `mode`; contract table by kind | `swarm.py`, `hanig-project/SKILL.md` |
| A6 | `survey.py` creates its output directory | `survey.py` |
| A7 | `tickets.py --name` for a human project title, separate from the slug | `tickets.py` |
| B2 | `--array` combined with declared outputs is refused (all four spellings) | `swarm.py` validate |
| B3 | Path-like tokens in a command are cross-checked against declared inputs | `swarm.py` validate |
| B7 | Field reference: `SCHEMA_FIELDS`, `SCHEMA_COUPLINGS`, `swarm.py schema` | `swarm.py` |
| B8 | `tickets.py` carries the DAG into the tracker as `blockedBy` | `tickets.py` |
| C10 | Refuse to dispatch a code unit into a dirty tree, at second zero | `swarm.py` `_write_launch_record` |
| C13 | Coordinator state lives outside the repository it operates on | `coordinator_paths.py` |

Plus one item nobody asked for, which the cycle forced: the launch record is
now **evidence rather than authority**, with structural tests enforcing it.
See "The root cause" below, and its open defects.

## Open, in the order to do them

### C11. A worktree per code attempt. DO FIRST.

`paseo run` supports `--new-workspace worktree --worktree-mode branch-off
--new-branch <name> --base <ref>`, and `paseo inspect` reports
`Worktree: None` for every agent this project has ever run: the coordinator
has never asked. The change belongs in `swarm.py`, which builds its own argv,
NOT in the plan, which is where the reporter's earlier attempt was silently
ignored.

This is first because it is now load-bearing for something already shipped.
Two reviewers independently raised the shared-checkout TOCTOU as MAJOR: C10
observes cleanliness at a point in time, and another agent can dirty the tree
between that observation and the spawn. Both times the answer was "declared
limit, C11 is the structural fix." That answer is only honest if C11 happens.

Done when: each code attempt gets its own worktree on its own branch off a
recorded base; the preflight checks the tree the agent will actually run in;
the repo pool can exceed capacity 1; agents cannot collide with each other or
with the human operator in one shared checkout; and the point-in-time caveat
is deleted from `_submit` because it is no longer true.

### C12. Generate the branch/commit/PR protocol into every code prompt.

All 16 units in the field report were specified to close on a merged PR and
not one prompt mentioned a branch, a commit, or a PR. The agents behaved
correctly by editing files and stopping. The instruction to produce the
closing evidence was never given, which makes such a plan structurally
unclosable.

Done when: the plan generator appends the protocol to any unit whose closure
is a merged PR, including "if `git status` is not clean, stop and report
rather than working around it"; it cannot be omitted per-plan; and a code unit
whose prompt lacks it is refused rather than dispatched.

Note there is currently no draft/prompt-generator script in
`hanig-project/scripts` (only `survey.py`, `tickets.py`, `report.py`), so this
item includes deciding where prompt generation lives.

### C14. The receipt flags untracked files not in `produces`.

18 bytes of test debris (`phase0b/--reflink=auto`) appeared from a stubbed
`cp` writing into the source directory, and nothing surfaced it. `repo_status`
already collects untracked paths; nothing carries them into the receipt.

Done when: a receipt lists new untracked paths outside the unit's declared
outputs, and that list is visible in the report.

### B1. Snapshot external artifact paths BEFORE the unit runs.

The strongest remaining correctness item. Post-hoc observation cannot tell an
input from an output: a unit that passed its input path to the receipt with no
`--out` would have recorded a file it never wrote as produced evidence and
read DONE. A pre-dispatch digest of every declared external path, required to
differ at completion, catches it.

Interim named by the reporter: refuse any artifact whose mtime predates the
attempt directory's creation.

### B6. Detect the same unit id live in more than one state directory.

NOT the existing duplicate-id check, which is within one plan. The skill
encourages ad-hoc sub-plans; the reporter made three, and one unit was
submitted twice from two plans into identical output paths. A check against
`squeue` for a job already bound to that unit id, or a lock keyed on declared
outputs, refuses the second dispatch.

### B4. Survey per-partition `AllowAccounts`, QOS `GrpTRES`, `MaxMemPerCPU`.

Hours lost to `QOSGrpCpuLimit` while a 736-CPU partition sat 202 CPUs idle,
because the survey reports partitions but not who owns them. `MaxMemPerCPU`
belongs in the same report: exceeding it makes Slurm refuse the job while
naming CPUs, not memory.

### B9. `grill-with-docs` asks about the lab's priority partition.

Complements B4 rather than repeating it: B4 is machine-readable, this is the
judgement no `sinfo` output expresses. Two questions with recommended answers:
may CPU-only work use the exclusively-allowed partition (recommend yes when
the shared queue is capped and the lab's is idle), and is the per-job
footprint acceptable once `MaxMemPerCPU` is applied. Not hypothetical: 700 GB
at `MaxMemPerCPU=5120` costs 140 CPUs, not the 32 requested.

### The `scontrol` note.

For a partition change on an already-queued job, `scontrol update JobId=N
Partition=...` preserves the coordinator's job binding, whereas
cancel-and-redispatch loses it and needs `--accept-plan-change` afterward.
Belongs in the dispatch section of the skill.

## Deferred, with the reason

- **A4.** `write_scopes` does not isolate a code unit, and the skill reads as
  though it does. Superseded by C11: worktrees are the isolation, and the doc
  fix should describe what C11 actually does rather than describing scopes.
- **B8's stale-edge half.** Delivered for missing edges. Linear's relation API
  is append-only through this interface, so a `needs` that shrinks leaves a
  wrong edge behind. Needs read-back verification, not trust in the write.
- **A8.** Interview checklist should ask for the runtime profile and a canary
  per partition. Partly covered by the grill work; needs an audit of what step
  2 asks against what `validate` enforces.

## The root cause, and what it left open

A three-round review cycle was stopped by the gate itself: `round 4 exceeds
the bound of 3 for one change. Past this, more rounds have not converged --
they have been finding defects in the previous round's fixes.`

It was right. Four defects across three rounds were one move: deciding a trust
question by reading the value back out of the launch record, which sits one
level above the attempt directory handed to a full-access agent as
`SWARM_UNIT_DIR`.

| round | field read back | decided |
|---|---|---|
| 1 | `base_commit` | the verified base |
| 2 | `preflight.status` | whether to charge a retry |
| 3 | `execution_workspace` | which tree `paseo` runs in |
| 3 | `dirty_paths` | whether to refuse |

`trusted_base`'s docstring said *"A test asserts nothing else reads
base_commit out of a launch record."* No such test existed. That is the actual
root cause: the invariant was prose, so violations were found by humans one at
a time, and the worst was never found at all. `judge_detail` decided whether
an attempt PRODUCED anything from a base, tree, branch and `clean_at_launch`
it read from that record itself.

The fix: the coordinator digests the record's bytes as it writes them and
keeps the digest in its own state; `judge_detail` reads only through
`read_sealed_launch_record`; the seal travels to the separate judging process
as `unit.py check --record-seal`. Structural tests now enforce the invariant
that used to be prose.

### Open defects in that fix (round 1 review, 3 MAJOR)

1. **The seal is not an authenticator.** It is an unkeyed SHA-256 of bytes the
   agent can read, passed as a command-line argument. Anything that can read
   the record can recompute it. It detects tampering ONLY when the seal comes
   from the coordinator, which is true for `advance`'s invocation and false
   for an agent invoking `unit.py check` itself with a rewritten record and a
   matching digest. **The claim "a seal cannot be recomputed by anything that
   does not hold coordinator state" was mine and it was wrong.** Options: an
   HMAC keyed on a secret in the state dir, or pass the anchor values instead
   of a digest. Note that neither closes the deeper question below.

2. **Fail-closed too hard, twice.** A crash between writing the record and
   storing the seal, or a pre-existing record from before this change, leaves
   `attempt_record_seals` empty; `_write_launch_record` returns no seal, the
   attempt still dispatches, and it can then never be judged. A legitimate
   unit becomes permanently stuck. Re-deriving the seal from the file is NOT
   the fix, since that is the laundering being prevented; failing the attempt
   with a clear reason so it retries into a fresh attempt directory is.

3. **The structural tests match literals only.** `_key_uses` and
   `_raw_reader_calls` see direct string constants and direct reader names, so
   a computed key or an alias introduces a violating site without failing.
   Worth tightening, and worth stating the limit where the tests live.

### The deeper question these findings expose

`unit.py check` writes the receipt, and an agent with write access to the
attempt directory can write `receipt.json` by hand regardless of any seal. The
report's `_state_of` says the receipt WINS over coordinator state, deliberately
and for good reasons. Coordinator closure comes from `advance`'s own
invocation, and code units additionally close only on a merged PR, so this is
not a straight path to DONE. But "which consumers trust a receipt, and what
binds a receipt to a coordinator-caused check" has not been answered, and the
seal work does not answer it.

This is its own item, not a follow-up to the above.

## Loose ends

- `converge.py` is 647 lines that nothing calls: not wired into `swarm.py` or
  `unit.py`. Either wire it in or delete it.
- No wandb integration for ML training units. Asked about and never closed.
- Orphan paseo workspace `wks_acba5d75` from a probe run on 2026-08-31.
