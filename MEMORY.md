# MEMORY.md — multi-agent-skills

Portable state for this repo. Written for a fresh session with zero context,
on any machine.

## What this is

A set of Claude Code skills for running a swarm of agents against scientific
compute, installed per server and used on that server for that server's
projects. Cross-machine portability is explicitly NOT a goal: skills go on
chimera, lambda and andromeda, and a project runs entirely on one of them.

The repo's thesis, arrived at after three plans died: **isolation replaces
attribution.** Observation across a time window can show that an artifact
CHANGED; it can never show which process changed it, not on a filesystem
shared by ~18 people. So the coordinator allocates an exclusive, never-reused
write root per attempt, `mkdir(exist_ok=False)` is the enforcement, and
"declared output present + terminal-OK owned sacct row" is allowed to mean
"this unit produced it".

The receipt states plainly what that does and does not establish: exclusive by
coordinator allocation under a **trusted-writer convention**, NOT OS-enforced.
A command can write an absolute path elsewhere, and another process running as
the same Unix user can write into the directory. Real isolation would need a
container or mount namespace with the attempt directory as the only writable
bind mount.

## Status as of 2026-08-28

Built and green: 424 tests.

| Piece | File | State |
|---|---|---|
| Unit contract | `skills/hanig-swarm/scripts/unit.py` | allocate / bind / check |
| Coordinator | `skills/hanig-swarm/scripts/swarm.py` | validate / run / advance / status / outbox |
| Convergence | `skills/hanig-swarm/scripts/converge.py` | 5 states, divergence before convergence |
| Review gate | `skills/hanig-review-gate/` | `--kind plan\|implementation`, quorum, escalation |
| Tracker outbox | in `swarm.py`, `docs/tracker-outbox.md` | intents written; NO drain yet |

Validated live on lambda: a 3-unit DAG (A -> {B,C}) with real `sbatch` ordered
correctly and all reached DONE; two concurrent advances, one refused by the
lease; `job_id` wiped from state while job 187880 was RUNNING and advance
recovered it by job name rather than resubmitting; cron fired unattended.

Live on lambda right now: a crontab line `*/5 * * * *` tagged
`# hanig-swarm`, and test directories under `~/swarm-live/dag/`. Remove with
`crontab -e` when done.

Live re-validation on lambda after the review fixes (2026-08-28), all on the
real cluster and its NFS home, not locally:

- the atomic lease: 192 contended acquisitions on NFS, 16 contenders per
  trial, exactly one winner every time. NFS matters here: `O_EXCL` is
  unreliable on it, which is why the lease is published by `link()`.
- crash between the ALLOCATED save and `sbatch`: released and re-dispatched
  into a fresh directory, stale one left on disk.
- `sbatch` succeeded but the bind write failed: rebound to the ORIGINAL job
  187893 rather than resubmitting, then reached DONE.
- plan freeze: an edit to `gpu_hours` (which the old digest ignored entirely)
  now refuses, ratifies under `--accept-plan-change`, and does not re-dispatch
  the DONE unit.
- retry charges accumulate: 2 attempts, 0.1 GPU-hours, not 0.05.

The `pipeline` path ran for the FIRST time here and was broken: `check_unit`
demanded a scheduler binding that a pipeline unit never has, so a healthy
engine with its declared output on disk sat INCOMPLETE and would have decayed
to FAILED_EVIDENCE, holding its dependents. It now has its own predicate
(`_pipeline_state`), and its receipt says `exit_status_attested_by: launcher
wrapper (no scheduler)`, because a login node offers no third party equivalent
to Slurm's accounting database. Weaker evidence, named as such.

The `code` path (paseo agents) also ran for the FIRST time and was broken in
four independent ways, each alone enough to make the kind unusable: the agent
ran in the coordinator's directory because `--cwd` was never passed (the
isolation premise silently void for that kind); the agent id was read from
`id` when paseo calls it `agentId`, leaving a live agent orphaned; `bind`
rejected a UUID outright, demanding a numeric scheduler id; and the
coordinator skipped binding any non-numeric id, so the agent id never reached
unit.json. All fixed and verified end to end on the Mac.

Its predicate delegates lifecycle to paseo and judges artifacts itself. It
does NOT reimplement the git-worktree contract, and the receipt says so
(`worktree_judged: false`). Verified live in both directions: an agent that
went idle having written nothing returns INCOMPLETE, not DONE.

NEEDS_HUMAN (exit 5) is a sixth unit state, added because a real agent under
default permissions stopped at its first Write and sat `running` forever. It
does not accrue toward the settle window: waiting for a person must never
become FAILED_EVIDENCE because nobody was at the keyboard. For unattended
runs a code unit must set `"mode": "bypassPermissions"` EXPLICITLY in the
plan; the coordinator will never bypass permissions on its own.

Paseo is NOT on lambda or chimera and should probably stay that way: it runs
agent sessions as local processes, and lambda's own guidance is not to run
heavy processes on a login node. `validate` therefore refuses a code unit on
a host without paseo and says to run it where you run agents, or to declare
the work as kind=pipeline. Agents on the Mac, slurm and pipeline units on the
clusters.

Plan step 3 (human monitor + gated promotion) is BUILT. `status` and
`status --json` read durable state only, so they render with the coordinator
stopped; a HELD unit names what holds it; budget shows spent-of-declared. Exit
codes are the notification channel, since the coordinator has no network: 2
when a unit needs a person, 1 when halted, 0 otherwise. A cron wrapper reads
that and decides whether to wake anyone.

`swarm.py promote --unit X --approve --approver <name>` is the only way an
output reaches a shared path. It is a dry run without `--approve`, refuses
without `--approver`, refuses anything not DONE, and re-derives every output's
fingerprint before copying: a tampered output is refused and the canonical
pointer does not move. Promotion copies into a staging dir, renames into a
VERSIONED directory, then swaps one symlink, because rename is not atomic
across filesystems and a shared tree is usually a different mount from the run
root. A unit declaring no `promote_to` creates no shared path at all.

This required changing the receipt: it recorded no output fingerprints, so
criterion (f) was unimplementable. `check` now records size, mtime and a
content digest for files under 256MB, and names the method, so a size-mtime
match is reported as NOT establishing unchanged content rather than passing as
one.

Not built: plan steps 4 (install per server), 5-7. Operator controls (`status --json`, notifications,
`NEEDS_HUMAN`, promotion), per-server install, the project front door, and the
`grill-with-docs` gate. All three declared kinds now have a working predicate and have been
run for real.

## The open decision: which tracker

Linear is intended. The account exists but is not set up, and a registry
search returns NO Linear connector on this machine, so a drain cannot be
written against `mcp__linear__*` today. Asana IS connected and would work now.

The outbox is deliberately backend-agnostic, so this decision does not block
anything: `swarm.py` writes intents to `<state-dir>/outbox.jsonl` and never
calls a tracker. It has no network imports at all, checked by AST, because
putting a tracker API token on a shared cluster login node is the alternative
and it is worse. A drain is ~30 lines per backend and runs where the tracker
is reachable.

Rule that must survive any backend choice: **no issue is ever closed on a
self-report.** A close intent carries the predicate's receipt; a drain that
cannot see the evidence must refuse to close.

## Working notes

Three false passes have been found and closed in shipped code, and this
project has now over-claimed on the SAME axis twice: attribution, then the
isolation that replaced it. Assume the next claim is too strong until a
predicate backs it.

The review gate works and is worth running. Its last implementation review
returned REVIEW_FAIL with four reviewers, refuted two of seven asserted
claims, and found a genuine false pass plus a non-atomic lease. Findings that
several reviewers reach independently have been reliable; single-reviewer
findings have included retractions.

Verify a concurrency fix by REPEATING it. The lease took three passes, and
runs two and three were only exposed by looping the test: a single trial
passed against code that was still racy. Also check a new test is non-vacuous
by running it against the old implementation.

`sacct` defaults to jobs that started today; always pass `-S`. Job ids are
reused. `0:0` on a CANCELLED job is not success. `End` can be the literal
string "Unknown". States can contain spaces (`CANCELLED by 10025`).

Python floor is 3.10 (andromeda and chimera are 3.10.12, lambda 3.12.3),
standard library only, must be safe on a login node.

The `--mem` requirement is lambda-specific; do not write it down as a fact
about clusters generally.

## Files

- `docs/plan-swarm.md` — 7 steps, 45 acceptance criteria. Step 6 d/e/f now BUILT.
- `docs/tracker-outbox.md` — the outbox and how to write a drain.
- `docs/scenario-mach1-zebrafish.md` — end-to-end walkthrough. Its multi-cluster DAG is NOT the intended topology; annotated as such.
- `docs/plan-fusion.md`, `docs/audit-*.md` — prior analyses.
