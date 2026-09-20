# Declared limits

This file expands the limit index in `../SKILL.md`. The index is authoritative
for discoverability; details here do not silently add a new closed-looking
assumption.

## Runtime canary scope

A canary proves that one runtime worked on one node at one time. That is a
reasonable declared assurance for homogeneous nodes and shared storage. It is
not enough for heterogeneous partitions, node-local paths, or images assembled
at run time; those need a preflight in every allocation.

## Trusted-writer isolation

`mkdir(exist_ok=False)` ensures no two attempts are allocated one root. It does
not stop another process under the same Unix identity from writing there.
Receipts expose `os_enforced_isolation` and `attribution_by_observation` rather
than upgrading this convention into an OS claim.

## Container isolation scope

Apptainer/Singularity still executes as the invoking user. The declared profile
restricts writable host bind mounts of the dispatched workload. It does not
block an existing same-UID process from touching the host-side root, reading a
submitted script, or forging the application marker; it claims no network or
PID isolation. Private scratch and overlays may remain writable. Tools needing
a writable host `$HOME` cannot use this profile.

## Pre-dispatch artifact basis

Three accepted costs remain:

- An attempt dispatched before artifact bases existed can never become DONE;
  re-dispatch it. A post-run digest would be the output, not a baseline.
- Over 256 MB, comparison uses size and mtime instead of hashing; a same-length
  rewrite inside the timestamp resolution can be invisible. Receipts name the
  method and every artifact using this weaker comparison.
- Byte-identical regeneration and no production are observationally identical.
  In practice an allocated root should be empty; this matters when something
  populated the artifact before dispatch.

## Same-UID authority access

A same-UID descendant can inspect checker `/proc` state or ptrace it and obtain
an authority descriptor. Modes, Git locks, and descriptor discipline cannot
defend against the same principal. Separate Unix identities or an equivalent
OS boundary are needed. The intended threat model covers careless workers and
operators, not a malicious peer sharing the coordinator identity.

## Process-tree quiescence

Neither Slurm terminal accounting nor Paseo terminal/idle state supplies a
portable handle proving all same-UID descendants are dead. A background process
can mutate files during or after checking. The receipt digest binds bytes read;
it does not establish quiescence. Accepted receipt provenance records this.

## Remote-ref durability

The anchored route and exact branch make a pushed commit durable past worktree
cleanup. The agent controls the ref value. Coordinator checks of base ancestry
and tree change establish production on that route, not authorship,
correctness, review, merge, branch protection, or safety from another writer
who can mutate the ref. Launch preflight is a point-in-time availability and
collision check, not a promise that the remote remains reachable.

The terminal watcher is best-effort, same-host, and waits at most 60 seconds
for a busy coordinator lock. Crash, unavailable Paseo, or a longer lock holder
leaves scheduled advance as the fallback.

## Verifier corpus boundary

Corpus protection covers only exact regular files named by policy. Imported
helpers, generated fixtures, configuration, toolchains, and environment remain
unprotected unless their repository files are also named. The empty-corpus
compatibility path intentionally offers no corpus protection.

## Integration verification topology

Candidate construction never fetches. Both commits must exist locally, have a
unique merge base, merge without conflict, and yield a tree change. Admission
rejects a target that moved since the receipt. Squash/merge topology must
unambiguously expose the pre-merge target; rebase results cannot do so when
target and replayed commits may carry the same patch.

## Write scopes

`write_scopes` lets validation reject overlap among concurrently runnable
units. No runtime mechanism prevents a process from writing elsewhere under
its Unix permissions.

## Worktree inode identity

Device/inode checks close whole-directory substitution under the same path.
They cannot make Git metadata immutable: HEAD, index, refs, and objects must
change during honest work. Semantic Git checks and the pinned produced commit
carry that part of judgment.

## Child credentials

The denylist strips exact environment names from coordinator children. Paseo's
long-running daemon independently supplies provider credentials, `HOME` passes
through with stored authentication, and unknown or embedded names remain. A
daemon that starts with ambient keys is outside swarm's containment boundary.

## Worktree adoption

Paseo lacks a conditional reserve-and-launch primitive. Registry checks reduce
accidental adoption but a same-UID process may register between the checks and
adoption. The post-check full Git identity match fails closed; only a Paseo
atomic reservation or a different OS identity closes the race.

## Paseo workspace ID

The workspace ID is parsed from Paseo human-readable output. It may select
cleanup/archive bookkeeping only. Repo, base, branch, path, and Git identity
are re-derived independently; no trust decision may consume the parsed ID.

## Pipeline interior

The workflow engine owns internal scheduling, retries, and work data. Swarm can
establish only engine termination plus final declared artifacts at its isolated
publish boundary. It cannot certify which internal step made an intermediate.

## Convergence plateau

A plateau criterion asks whether improvement stalled, not whether the result is
good. A flat bad run satisfies it. Add a value threshold whenever outcome
quality matters.

## Coordinator lock topology

The advisory state lock has no TTL: the kernel releases it on process death.
On NFS, server reboot or lost client lock state can drop a live lock without
notifying the process. Trials exercised only same-node concurrency. Cross-node
exclusion remains uncertified. One coordinator node per plan is the supported
topology; node-local state trades the NFS failure away by also removing shared
visibility.

## Output-claim registry

An output claim is a durable directory rather than a process-lifetime lock
because writers outlive `advance`. It is shared only within one run root. Two
different `--root` values do not arbitrate. Foreign release requires positive
scheduler/Paseo absence; missing, failed, or inapplicable liveness is unknown
and refuses, potentially requiring an operator to remove a stale named claim.

## Base-branch comparison

Testing the recorded base and feature head independently attributes a
regression to the feature change. It cannot detect semantic conflict with a
target branch that changes later. Only a test of the actual candidate/merged
tree answers that integration question.
