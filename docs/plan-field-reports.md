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

Status verified against code on 2026-09-03, not against memory of the session.

## Tracker

Linear is the system of record for status. This doc holds the reasoning; the
issue holds the state. Project: **Swarm skills: field-report hardening**
(`linear.app/arc-projects/project/swarm-skills-field-report-hardening-4978bafcf9e3`),
team Arc.

Field-report items, and where each ended up. **Every one is now closed and on
`dev`**, except the two that were never code:

| Item | Issue | Item | Issue |
|---|---|---|---|
| C14 | ARC-228 | converge.py | ARC-234 |
| B1 | ARC-229 | A4 | ARC-235 |
| B6 | ARC-230 | B8 stale-edge | ARC-236 |
| B4 + `scontrol` note | ARC-231 | A8 | ARC-237 |
| B9 | ARC-232 | wandb | ARC-238 DEFERRED |
| E2 | ARC-233 | paseo provider pinning | ARC-239 BLOCKED |

`ARC-238` is a product decision plus an external service, so it waits for a
human. `ARC-239` cannot be done from this machine: the `paseo-*` and `pi-fleet`
skills are not installed here, so there is no file to edit.

Nine more were filed from the work itself. Six are closed; three are not:

| Issue | What | State |
|---|---|---|
| ARC-240 | 21 tests could not run where `paseo` is absent. The suite is now GREEN and mutation-verified five ways | done |
| ARC-241 | Three further limits that lived only in code comments or `MEMORY.md` | done |
| ARC-243 | The dispatch protocol forbids `git stash` AND the coordinator refuses a non-empty stack | done |
| ARC-244 | `WALK_SECONDS` was COOPERATIVE, so a blocked `opendir()` never reached it. Now a child with a real kill deadline | done |
| ARC-245 | `install.sh` and `doctor` stat `.git`, a FILE in a worktree, so installs from one recorded `version=unknown` | done |
| ARC-249 | Four sites routed code units through `bus await`, which nothing ever called | done |
| ARC-246 | `--mem` enforced nowhere; the account never checked against the partition, which B4 now makes possible | OPEN |
| ARC-247 | `findings.json` required by the docs, checked by nothing | OPEN |
| ARC-248 | The `flock` claim is now scoped to its evidence; whether `$HOME` is NFS on the clusters is unanswered | HALF |

**Whole-suite baseline, `dev` at `cbc1b56`: 1298 passed, 0 failed, no
deselection, 7m26s.** Before this cycle: 31 failed, 1102 passed, 64 minutes --
of which ~59 minutes was one hang, and every failure was environmental.

Green means something here now. ARC-240 broke `swarm.py` five ways and
confirmed each break is caught, so a red result is a regression rather than
noise. Two tests that asserted nothing were found and fixed on the way, plus
one that could not fail at all.

Each issue carries its own done-predicate, copied from the item below it. If
you change a predicate here, change it there.

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
| E1 | Children no longer inherit the coordinator's credentials; exact-name denial | `child_environment.py` |
| C11 | A worktree per code attempt, verified and bound by inode; TOCTOU closed | `swarm.py`, `worktree.py` |
| C12 | The completion protocol is appended at dispatch: branch, base, remote, PR target | `swarm.py` |
| C14 | The receipt lists untracked paths no declared output covers; audit-only | `worktree.py`, `report.py` |
| B4 | Per-partition `allow_accounts`, `deny_accounts`, `max_mem_per_cpu_mb`, `qos`, `qos_grptres`, each `set`/`unrestricted`/`unknown` | `survey.py` |
| B9 | The two partition questions `sinfo` cannot answer, with the association-QOS caveat | `hanig-project/SKILL.md` |
| A8 | Interview audited against `SCHEMA_FIELDS`; `target_branch` and the retry contract were asked by the wrong name or not at all | `hanig-project/SKILL.md` |
| A4 | The isolation described as the per-attempt worktree; the branch constraint C11 removed is no longer claimed | both `SKILL.md`s |
| E2 | The two declared limits stated where a skill reader meets them, plus three contradicting claims fixed | `hanig-swarm/SKILL.md`, `README.md` |

Plus one item nobody asked for, which the cycle forced: the launch record and
the receipt are now **audit-only**, and authority lives in coordinator state.
See "The authority work" below. That section replaces the earlier sealing
design, which a committee rejected and which is no longer what the code does.

## Open, in the order to do them

> **Read the Tracker table above for what is still open.** C14, B4, B9, A8, A4
> and E2 are DONE and live on `dev`; their sections are kept below because the
> reasoning is why the fix looks the way it does, and deleting it would lose
> the failure that paid for it. B1 and B6 are the two field-report items that
> remain.
>
> Shipped to `origin/main` at `d8591d4`: items 10 and 13, the authority work,
> the model routing, E1, C11 and C12. Everything since is on `dev` and has not
> been pushed.
>
> On estimating: C11 took four review rounds and 26 findings; C12 took one
> round and three, all three against "can an agent actually FOLLOW this",
> which no test checks. The second wave cost less per item and found more --
> nine issues that no field report contained, two of them (ARC-244, ARC-248)
> more serious than most of the list they came from. Dispatching against a
> written plan surfaces the plan's own blind spots.

### C14. The receipt flags untracked files not in `produces`. DO FIRST.

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

### E2. The two limits this design declares but does not solve.

Neither is a defect; both are unclosed and should be revisited deliberately
rather than discovered by someone assuming they are closed.

**Same-UID process access.** kimi filed a CRITICAL, correctly out of scope: a
same-UID descendant can reach the checker's `/proc` entries and obtain the
authority descriptor. Nothing short of separate principals or containers
closes it. Revisit if these ever run somewhere the agent is a real adversary
rather than a careless one.

**Process-tree quiescence.** The agent's process group is not guaranteed dead
before the check runs, so a lingering descendant could still touch output
files mid-digest. Recorded on each accepted receipt. deepseek raised it as a
precondition for the result channel being authoritative at all, and sol could
not make it portable across paseo agents and scheduler jobs.

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

## The authority work, as it actually ended up

Six review rounds, a three-member committee, and eight commits. The short
version: **the launch record and the receipt are audit-only. Authority lives in
coordinator state and reaches the separate judge over a channel the agent
cannot obtain a handle to.**

### What kept recurring

Every defect was one mistake wearing different clothes: the authority path was
established by DISTINGUISHING it from the untrusted path rather than by
construction, and each distinguishing property turned out to be shared with
something the agent or the OS also controls.

| # | the property that was supposed to distinguish it | how it leaked |
|---|---|---|
| 1 | a file the coordinator wrote first | the agent can write it too |
| 2 | a digest of that file, held in state | detects tampering, is not authority |
| 3 | a reserved prefix on a shared stdout | a newline in agent-written `engine.json` injects a line |
| 4 | a dedicated descriptor, above 1 and 2 | `TemporaryFile` lands on fd 0, inherited as the child's stdin |

`trusted_base`'s docstring had claimed a test enforced the invariant. No such
test existed, which is why each violation had to be found by hand, one at a
time, and why the worst of them -- `judge_detail` deciding production from a
record it read itself -- was never found at all.

### What it is now

- The complete launch snapshot is captured in coordinator state before the
  spawn and passed to the judge as `--launch-facts`. Deleting or rewriting the
  launch record cannot change a judgment; there is a test that says so.
- `produced_head` is pinned per ATTEMPT. It was a unit-level scalar that was
  never cleared, so a retry inherited the previous attempt's commit and pinned
  its merge admission to it -- the same bug already fixed once for the launch
  record, reproduced in state instead of on disk.
- The checker reports its result over an anonymous `TemporaryFile` promoted
  above fd 2, with every low alias closed. `unit.run` sets `stdin=DEVNULL`,
  `close_fds=True` and `start_new_session=True`, and REFUSES any `pass_fds`
  entry below 3 at the API boundary, so the mistake is unrepresentable rather
  than avoided by convention.
- No re-observation fallback. A missing basis fails closed.

### The rule that makes re-checking safe

Not "never ask again" -- that was the old code's over-correction, which refused
a merge attestation for a genuinely produced commit because the branch had
moved. The rule is **never ask a MUTABLE source again**. A commit and its tree
are immutable; a ref and `HEAD` are not. Pin `A` once, and every later consumer
uses `A`. A branch moved to `C` afterwards is harmless. A missing object `A` is
an availability failure that fails closed, never a re-judgment.

### Declared limits, not solved

- **Same-UID process compromise.** Nothing here defends against another
  process running as the same user reaching in through `/proc` or `ptrace`.
  That needs separate principals or containers.
- **Process-tree quiescence.** The agent's process group is not guaranteed
  dead before the check runs, so a lingering descendant could still touch
  output files mid-digest. Recorded on each accepted receipt rather than
  claimed closed.
- **Ambient environment inheritance. NEW, and the most serious open item.**
  Children inherit the coordinator's environment, which on this machine
  includes `OPENAI_API_KEY` and `OPENROUTER_API_KEY`. A code agent the threat
  model treats as untrusted is handed live credentials. Found during the
  descriptor audit and deliberately not fixed there: sanitising it is a
  compatibility-sensitive policy change. **Do this before C11.**

### Model routing, set 2026-09-01

Coding agents are `codex/gpt-5.6-sol` at thinking high, including
`start-a-sprint`'s workers, which previously used self-hosted DeepSeek Flash
and native Luna. Sol coordinates and integrates; sol does NOT review, because
an author reviewing itself is what the roster exists to prevent. The review
gate is kimi-k2.7-code, luna and glm-5.3. The committee is luna, deepseek and
kimi across two providers -- deepseek plans well and concedes under challenge
but over-claims as a refuter, so it sits on the committee and in no gate
profile. `committee.py` takes 2-3 members; `reviewers.json` has a `committee`
profile separate from the gate tiers.

Evidence for that split, from this session: kimi found a CRITICAL that luna,
deepseek and glm all upheld past; luna then found the fd-0 MAJOR that kimi
upheld past. Two rounds, two different models catching what the other missed.

## Loose ends

- `converge.py` is 647 lines that nothing calls: not wired into `swarm.py` or
  `unit.py`. Either wire it in or delete it.
- No wandb integration for ML training units. Asked about and never closed.
- Orphan paseo workspace `wks_acba5d75` from a probe run on 2026-08-31.
- `paseo-advisor`, `paseo-committee`, `paseo-loop`, `paseo-handoff` and
  `pi-fleet` pin no provider and fall back to Paseo's discovery, so they are
  NOT sol. They live outside this repo; pinning them is a separate change.
- `committee.py` phase 3 had never run: it called `R.run`, which does not exist
  in `review.py`. Fixed at e122243. Found by using it, which is the only way
  that class of bug surfaces.
