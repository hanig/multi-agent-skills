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

Built and green: 392 tests.

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

Not built: plan steps 4-7. Operator controls (`status --json`, notifications,
`NEEDS_HUMAN`, promotion), per-server install, the project front door, and the
`grill-with-docs` gate. The `pipeline` and `code` dispatch paths have never
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
