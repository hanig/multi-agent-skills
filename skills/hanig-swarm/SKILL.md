---
name: hanig-swarm
description: >-
  Coordinate a swarm of agents to build projects autonomously on Slurm clusters.
  Use when dispatching, isolating, tracking or judging units of autonomous work
  across lambda/andromeda/chimera: a single Slurm job, a Nextflow or Snakemake
  pipeline, or a code-editing agent. Allocates an exclusive per-attempt write
  root so a cheap done-predicate is conclusive, binds a scheduler job to an
  attempt, and reports DONE / RUNNING / FAILED / PREEMPTED / INCOMPLETE. Use
  with the paseo skills for agent lifecycle; a code unit gets a per-attempt
  worktree and is judged here.
---

# hanig-swarm

## Host capability boundary

Read the current host's project instructions first. Loading this Markdown does
not supply Paseo, Slurm, Python, Git, a worker provider, credentials, or
approvals. Use only capabilities actually present. Never copy credentials or
weaken coordinator/worker containment to compensate for a missing capability.

Set `HANIG_SWARM_DIR` to the directory containing the `SKILL.md` instance this
agent loaded. It locates bundled programs only; project, plan, state, input and
output paths remain relative to the project directory. Paseo and `bus` are
separately installed dependencies.

## Default code agent

A `code` unit defaults to `codex/gpt-5.6-sol` with `thinking: high`:

```json
{"id":"impl","kind":"code","repo":"/path","target_branch":"main",
 "prompt":"...","mode":"full-access"}
```

Override `provider`, `model`, or `thinking` on the unit. `thinking: null`
suppresses the flag. `mode` is provider-specific and has no portable default:
codex accepts `auto`, `auto-review`, or `full-access`; claude accepts `bypass`
or `default`. Dispatch refuses a mode the selected provider does not support.

## Declare the runtime; prove it where the job lands

Every `slurm` and `pipeline` unit declares a runtime. A base-image-only unit
uses `"runtime":"none"`, which is a claim rather than an omission.

```json
"runtimes":{"py":{"id":"py","resolution":"direct",
  "entrypoint":"/home/me/envs/x/bin/python3",
  "probe":"python3 -c 'import h5py'",
  "verified_by":"canary:runtime-probe"}},
"units":[{"id":"runtime-probe","kind":"slurm","runtime":"py"},
         {"id":"work","kind":"slurm","runtime":"py",
          "needs":["runtime-probe"]}]
```

`resolution` is `direct`, `path`, `conda`, `container`, `module`, `uv`, or
`wrapper`; `verified_by` is `canary:<unit-id>`, `preflight`, or
`unverified:<why>`. Validation neither parses the command nor stats the
entrypoint: submit-host existence says nothing about a compute node. A canary
is an ordinary cheap Slurm ancestor that runs the probe through the same
launcher, partition, and account. Node-local paths, heterogeneous partitions,
and run-time-built containers require allocation-local `preflight` instead.

## Unit sizing and retries

**A unit is the retry boundary.** A retry is a fresh, empty attempt and redoes
the whole unit. Size by maximum unrecoverable work: absent a human choice, use
one independently executable shard per unit. `max_attempts` defaults to 1.

A checkpoint permits larger units only when it survives the failed attempt, is
made available to the new attempt, has an atomic completion marker, is
validated before reuse, actually skips completed work, and still yields every
declared output. `retry.mode:"resume"` is refused until that handoff exists.

Declare restart exposure; the coordinator never infers it from walltime,
partition, `gpu_hours`, or DAG shape:

```json
"retry_limits":{"read_bytes":100000000000},
"units":[{"id":"hash-00","max_attempts":3,
 "retry":{"mode":"restart","max_lost":{"read_bytes":97559511040}}}]
```

Splitting creates concurrent filesystem load. Bound live attempts across
invocations with `"limits":{"max_running":8,"pools":{"shared-fs-read":2}}`;
`--max-new-dispatches` limits only one invocation. Keep presentation stages in
`PLAN.md`; execution granularity need not match them.

## The one idea

**Isolation replaces attribution.** Every attempt receives an exclusive,
never-reused write root created with `mkdir(exist_ok=False)`, and the worker
writes only there. Inputs are immutable and pinned; Slurm cgroups isolate GPU
and memory. For `slurm`:

    declared output exists in the attempt run-dir
      + coordinator-pinned pre-dispatch basis says it was absent or changed
      + a terminal-OK sacct row owned by this attempt
      == this unit produced it

No check asks which command wrote a file. Exclusivity makes that unnecessary.
It is a **trusted-writer convention, not an OS boundary**: another same-UID
process can write the root. Receipts and reports state
`os_enforced_isolation:false` and `attribution_by_observation:false` unless the
declared container profile was actually applied.

The artifact-basis premise is mandatory: an output already present at dispatch
cannot prove production. Before dispatch, the coordinator digests every
declared artifact into per-attempt coordinator state. Missing basis fails
closed; an identical artifact fails; an absent-then-present or changed artifact
passes this premise. This compares before and after content, never authorship.

A `slurm` or `pipeline` unit may request an enforced host-write surface:

```json
"isolation":{"kind":"container","backend":"apptainer",
 "image":"/images/tool.sif","writable":["$SWARM_UNIT_DIR"],
 "read_only":["/shared/pinned/input.tsv"]}
```

`writable` must be exactly the attempt root and `read_only` must exactly match
declared inputs. The coordinator disables implicit home, cwd, hostfs,
administrator, and requested binds, adds the declared binds and a writable
tmpfs overlay, and accepts only one executable plus literal arguments. Shell,
expansion, redirection, pipelines, and backgrounding are refused. A random
coordinator-held token written only after successful direct container execution
plus the pinned wrapper fact is required for `os_enforced_isolation:true`;
otherwise the unit cannot close by silently degrading.

## Authority and closure

Launch records and attempt receipts are audit-only. Trust-deciding launch
facts, artifact bases, produced heads, and receipt provenance live in
coordinator state outside both the attempt root and operated Git worktree.
They are pinned per attempt, never inherited from a previous retry. The checker
returns its decision over an anonymous descriptor; `unit.run` refuses
`pass_fds` entries below 3.

Closure is fixed by kind. `code` closes only on a merged PR whose head equals
the head independently judged for the attempt. `slurm` and `pipeline` close on
a predicate receipt. Tracker acknowledgement and merge observation are
attested, not verified. An authorized verifier receipt is admissible only when
policy from the anchored base authorizes its content digest and binds it to the
head and claim. `REVIEW_PASS` means named reviewers failed to refute claims; it
is not proof.

Before a code agent exists, the coordinator anchors the exact
`refs/heads/swarm-<attempt>` judgment ref, the raw and once-expanded origin push
URL, base commit/tree, generated branch, and target branch. It refuses an
unreadable origin or a pre-existing local/remote attempt branch. Judgment
revalidates the route, fetches only that exact ref with submodules disabled,
and requires a changed tree descending from the anchored base. Worktree-only
commits and commits pushed under another ref do not count. The pushed remote
ref survives Paseo cleanup. A detached watcher best-effort invokes the same
locked `advance` path after terminal Paseo state; scheduled advance remains the
fallback, and `unit.py check` remains the only judge.

## Verifier admissibility

A verifier policy may name exact repository-relative regular files in
`corpus`. Verification refuses a subject commit that changed one and records
the full changed-path set plus each base digest. Admission re-derives those
facts from coordinator-held repo/base/head state. Corpus entries cannot be
directories, globs, symlinks, or gitlinks; limits are 10,000 paths, 256 MB per
file, and 1 GB total. An absent/empty corpus is the compatibility path and adds
no Git reads or receipt fields.

The reserved `integration-tests` claim runs a pinned verifier against a fresh,
disposable no-commit merge of produced head into an explicitly supplied target
commit. System/global Git config, hooks, filters, merge drivers, rerere,
repository-selection variables, HOME/XDG, attributes/templates, executable
path variables, lazy fetch, and network fetch are excluded. Conflict, missing
objects, no candidate tree change, ambiguous merge base/topology, or a moved
target refuses evidence. The receipt binds head, pre-merge target, unique merge
base, candidate tree, verifier policy, and verifier digest; admission rebuilds
the candidate. Rebase topology is unavailable. Integration verification
supplements, never replaces, merged-PR closure.

## Code-attempt isolation

`write_scopes` is planning metadata: validation refuses overlap among
concurrently runnable units, but it confines no process. Each code attempt gets
its own Paseo branch-off worktree from the immutable recorded base commit.
`target_branch` names the PR destination; the legacy per-unit `branch` field is
not a fallback. Concurrent code units need no artificial dependency.

The returned path is accepted only when Git proves it is a linked worktree of
the trusted source repo at the expected base and generated branch. Coordinator
state binds device/inode for the root, Git common dir, and linked Git dir;
judgment re-resolves and re-stats them. Honest Git content must change, so inode
identity is not content tamper-evidence. Judgment instead requires expected
branch/ref, descendant history, changed tree, and clean index/worktree, then
pins the produced commit.

Recovery after a controller crash adopts only a named agent or one unambiguous
worktree matching the complete launch intent after checking Paseo workspace
and agent registries. Unknown ownership refuses. The remaining same-UID race is
declared below. Paseo's free-text workspace ID is cleanup bookkeeping only and
must never decide trust.

## Usage

```bash
export HANIG_SWARM_DIR="/path/to/loaded/hanig-swarm"
U="$HANIG_SWARM_DIR/scripts/unit.py"
D=$(python3 "$U" allocate --root /external/swarm-runs --task align-reads \
  --kind slurm --command "bwa mem ref.fa r1.fq r2.fq > out.bam" \
  --output out.bam --input ref.fa --gpu-hours 4 --charge-to hani)
python3 "$U" bind "$D" --job-id 187196
python3 "$U" check "$D"
# DONE 0 | RUNNING 1 | FAILED 2 | PREEMPTED 3 | INCOMPLETE 4
```

Outputs are relative to the run-dir; a path resolving outside it is refused.
Move an already queued job in place with
`scontrol update JobId=187196 Partition=cpu`; cancel-and-redispatch leaves the
new job id bound to nothing and edits the plan digest, forcing
`--accept-plan-change`. See [field evidence](references/field-evidence.md#moving-a-queued-job).

## The three kinds

| kind | isolation | done predicate |
|---|---|---|
| `slurm` | exclusive run-dir + allocation | pre-dispatch basis + terminal-OK owned row + outputs |
| `pipeline` | fresh work and publish dirs, boundary only | pre-dispatch basis + engine terminal exit + final outputs |
| `code` | per-attempt worktree while running; exact pushed ref for durable judgment | lifecycle settled + outputs + ref resolves to a committed tree change; merged PR closes |

Pipeline interiors are unjudgeable: the engine owns its DAG, retries, and work
directory, so receipts state `basis.interior_judged:false`. Do not reimplement
its scheduler.

## Drift guard

If the surviving coordinator module grows past roughly 300 lines or reacquires
any check that asks whether a particular command wrote an artifact, stop and
revisit the design. Isolation, not observed attribution, is the center.

## Convergence gates

`unit.py` proves execution evidence, not scientific success. A `slurm` or
`pipeline` unit can declare `converge`; the coordinator applies the plan-pinned
criterion automatically after the ordinary predicate:

```json
{"converge":{"metrics":"metrics.jsonl",
 "criterion":{"metric":"val_auroc","mode":"max","threshold":0.78,
              "min_steps":10000},
 "diverge":[{"metric":"train_loss","above":1000}],"budget":40000}}
```

Metrics must also be a declared output. Divergence is evaluated before
convergence. Anything but `CONVERGED` becomes `NEEDS_HUMAN`, closes no ticket,
releases no dependent, cannot be promoted, and stays out of retries. The block
is validated before dispatch and is forbidden on `code`. No block preserves
ordinary behavior.

## Running unattended

Use a deterministic scheduler to call `advance`; do not start a fresh LLM
agent periodically. These guards are mandatory:

- One coordinator node per plan: advisory `flock` on the state directory; the
  kernel releases it on death, so there is no heartbeat, TTL, or lock stealing.
- `sbatch`/bind crash: attempts have `swarm-<attempt>` names and orphan
  reconciliation consults `squeue`/`sacct` before any resubmit.
- `INCOMPLETE` settles to terminal `FAILED_EVIDENCE` after 600 seconds and
  holds dependents.
- A canonical digest over dispatchable plan fields refuses mid-flight edits.
- Durable output-claim directories under `<root>/.output-claims` arbitrate
  coordinators sharing a run root; foreign claims release only after positive
  scheduler/Paseo absence. Missing or failed liveness is `unknown`, never free.
- Code dispatch refuses a non-empty shared Git stash. Agents must use
  `git show <base>:<path>`, a patch file plus `git checkout -- <path>`, or a
  separate base worktree, and inspect `git status --porcelain` before commits.

`max_running` and named pools count all live attempts, including attempts from
earlier invocations. A queued partition fix preserves the existing attempt and
job binding as described above.

## Credential boundary

`child_environment.py` removes an exact-name coordinator denylist, including
OpenAI, Anthropic, GitHub, AWS, SSH, and `SBATCH_GET_USER_ENV`, and never
inherits `SWARM_UNIT_*` or `SWARM_DEP_*`. It is not a general secret filter.
Paseo's daemon may independently give provider credentials to a worker, and
`HOME` passes through. The exact list deliberately does not suffix-match or
inspect values. `models.json` is routing metadata, not a credential grant;
`OPENAI_API_KEY` and `OPENROUTER_API_KEY` remain coordinator-side for review
and committee programs.

## Declared limits: meet every one before relying on the system

- **LIMIT: runtime canary scope.** One node at one time does not establish heterogeneous or node-local runtime availability; use preflight. [Details](references/limits.md#runtime-canary-scope)
- **LIMIT: trusted-writer isolation.** Exclusive paths prevent accidental collision, not another same-UID writer. [Details](references/limits.md#trusted-writer-isolation)
- **LIMIT: container isolation scope.** The profile restricts launched host binds, not the Unix principal, network, all descendants, or private scratch. [Details](references/limits.md#container-isolation-scope)
- **LIMIT: pre-dispatch artifact basis.** Old attempts lack basis; artifacts over 256 MB use size/mtime; byte-identical regeneration is indistinguishable from no production. [Details](references/limits.md#pre-dispatch-artifact-basis)
- **LIMIT: same-UID authority access.** Same-UID descendants can reach `/proc` or ptrace authority descriptors; separate identities are required to close this. [Details](references/limits.md#same-uid-authority-access)
- **LIMIT: process-tree quiescence.** No portable barrier proves every Paseo/Slurm descendant is dead; accepted receipts record that limit. [Details](references/limits.md#process-tree-quiescence)
- **LIMIT: remote-ref durability.** The ref proves durable production on a preselected route, not authorship, correctness, review, merge, future reachability, or protection from another remote writer. [Details](references/limits.md#remote-ref-durability)
- **LIMIT: verifier corpus boundary.** Only exact declared corpus files are protected; undeclared helpers, generated data, toolchains, and environment remain outside it. [Details](references/limits.md#verifier-corpus-boundary)
- **LIMIT: integration verification topology.** Conflicts, moved targets, missing objects, ambiguous ancestry, and rebases make integration evidence unavailable. [Details](references/limits.md#integration-verification-topology)
- **LIMIT: write scopes.** `write_scopes` constrains the plan, not a process's writes. [Details](references/limits.md#write-scopes)
- **LIMIT: worktree inode identity.** Inodes detect path substitution, not Git-content tampering. [Details](references/limits.md#worktree-inode-identity)
- **LIMIT: child credentials.** Exact environment filtering does not remove daemon-supplied credentials, files under `HOME`, or unknown names. [Details](references/limits.md#child-credentials)
- **LIMIT: worktree adoption.** Registry checks reduce accidental adoption but cannot close the same-UID reserve/launch race. [Details](references/limits.md#worktree-adoption)
- **LIMIT: Paseo workspace ID.** A free-text parsed ID is cleanup bookkeeping and cannot authenticate anything. [Details](references/limits.md#paseo-workspace-id)
- **LIMIT: pipeline interior.** Only the engine boundary is judged; internal tasks and intermediates are not. [Details](references/limits.md#pipeline-interior)
- **LIMIT: convergence plateau.** Plateau alone can converge at a bad value; pair it with a threshold when value matters. [Details](references/limits.md#convergence-plateau)
- **LIMIT: coordinator lock topology.** Same-node `flock` trials do not certify NFS recovery or cross-node exclusion; use one coordinator node per plan. [Details](references/limits.md#coordinator-lock-topology)
- **LIMIT: output-claim registry.** Claims arbitrate only coordinators sharing one run root, persist beyond coordinator lifetime, and unknown liveness refuses release. [Details](references/limits.md#output-claim-registry)
- **LIMIT: base-branch comparison.** Green at base and on the feature head says nothing about the eventual merged tree; integration testing is separate. [Details](references/limits.md#base-branch-comparison)

Measured scheduler trials, the first real DAG, cluster memory/partition tables,
and operational gotchas are in [field evidence](references/field-evidence.md).
Authority generations and verifier command examples are in
[protocol details](references/protocol-details.md).

Python 3.8+ for swarm scripts, standard library only, login-node safe, no
network imports in the coordinator.
