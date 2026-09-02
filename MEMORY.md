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

## Status as of 2026-09-02

Built and green: **1085 tests**. Sixteen commits ahead of where this session
started (`413baca`), 30 files, ~4900 insertions.

| Piece | File | State |
|---|---|---|
| Unit contract | `skills/hanig-swarm/scripts/unit.py` | allocate / bind / check |
| Coordinator | `skills/hanig-swarm/scripts/swarm.py` | validate / run / advance / status / outbox / verify / merge / promote |
| Worktree judging | `skills/hanig-swarm/scripts/worktree.py` | git predicate over a coordinator-supplied snapshot |
| External paths | `skills/hanig-swarm/scripts/coordinator_paths.py` | state and runs live OUTSIDE the operated repo |
| Convergence | `skills/hanig-swarm/scripts/converge.py` | 5 states — **nothing calls it** |
| Review gate | `skills/hanig-review-gate/` | kimi-k2.7-code, luna, glm-5.3 |
| Committee | `skills/hanig-review-gate/scripts/committee.py` | luna, deepseek, kimi; 2-3 members |
| End-of-run report | `skills/hanig-project/scripts/report.py` | verdict from evidence, not from stored state |

The big change this session: **the launch record and the receipt are
audit-only; authority lives in coordinator state.** Six review rounds and a
three-member committee got there. Full account, including the four ways the
authority channel leaked and the limits that are declared rather than solved,
is in `docs/plan-field-reports.md` under "The authority work".

**Most serious open item:** children inherit the coordinator's environment,
which on this machine includes `OPENAI_API_KEY` and `OPENROUTER_API_KEY`. A
code agent the threat model treats as untrusted gets live credentials. Do this
before C11.

Model routing, set 2026-09-01: coding is `codex/gpt-5.6-sol` at thinking high;
review is kimi/luna/glm; committee is luna/deepseek/kimi. Sol never reviews its
own work. codex authenticates with an API key rather than the ChatGPT
subscription (`codex login --with-api-key`), which removed the wall-clock usage
ceiling that stalled two agent runs — it is metered now, ~200k input tokens per
substantial sol run.

## The lesson worth carrying, 2026-09-02

A three-round review cycle was stopped by the gate for not converging: each
round was finding defects in the previous round's fixes. Four separate defects
turned out to be one move, reading a trust-deciding value back out of the
agent-writable launch record. The invariant against it already existed -- in a
docstring, claiming a test enforced it. No such test existed.

So: an invariant that is written in prose is not an invariant. Write the
enforcement, or expect to rediscover the violation one instance at a time. The
same applies one level up: the field-report plan lived only in a conversation
until 2026-09-01, which is why `docs/plan-field-reports.md` now exists.

Three artefacts this session documented a capability nothing had ever
exercised: the test `trusted_base`'s docstring promised, a `produced_head`
path that always returned None once sealing landed, and `committee.py`'s
entire phase 3, which called a function that does not exist. Each was found by
USING the thing, never by reading it. A docstring is a claim; run it.

Two habits earned the same way. Check every fix by mutation, because reverting
it must fail a test; a green suite after a fix proves nothing on its own. And
when fixing an instance, sweep for its siblings mechanically rather than
fixing the one in front of you -- the sweep test for unclassified unit states
found three more the targeted fix would have missed.

## Files

- `docs/plan-field-reports.md` — **the live plan.** Field-report items by list (A/B/C), what is done, what is open, and the 3 open MAJOR in the sealing work.
- `docs/plan-next.md` — older 2026-08-30 committee plan, own numbering, largely delivered.
- `docs/plan-swarm.md` — 7 steps, 45 acceptance criteria. Step 6 d/e/f now BUILT.
- `docs/tracker-outbox.md` — the outbox and how to write a drain.
- `docs/scenario-mach1-zebrafish.md` — end-to-end walkthrough. Its multi-cluster DAG is NOT the intended topology; annotated as such.
- `docs/plan-fusion.md`, `docs/audit-*.md` — prior analyses.
