---
name: hanig-project
description: Start a swarm project on this machine, or adopt a half-finished repo into one. Surveys the host and the code first, interviews the human only about what inspection cannot answer, writes a plan whose units have declared outputs, files a Linear project and issues after ONE approval, then dispatches and tracks it. Use when asked to start a project, set up work on a server, plan a repo, turn a repo into tickets, or file issues from a plan.
---

# The front door

You are on a server. There is either an empty directory and an intention, or a
half-finished repo and a vague plan. The job is to get from there to units of
work that are dispatched, tracked, and verifiable, without asking the human
anything a machine could have looked up.

Run in this order. Each step's output is the next step's input.

## 1. Survey. Never ask what you can read.

```sh
python3 scripts/survey.py --repo . --out .swarm/survey.json
```

This reports the host, python, schedulers, partitions, accounts, whether
`--mem` is required here, free disk, and, for a repo: git history, size,
language mix, existing docs, and whether a swarm project already exists.

Read it before speaking. Every fact in it is a question you must not ask.
If the repo is half-finished, also read its README, CONTEXT.md, MEMORY.md and
any `docs/adr/*`, and skim the last dozen commits. The point is to arrive at
the interview already knowing what was built and roughly why.

## 2. Grill. One question at a time, each with a recommended answer.

Invoke `grill-with-docs` if it is installed; otherwise follow its rule
directly, which is the rule that matters:

> If a question can be answered by exploring the codebase, explore the
> codebase instead.

Ask ONLY about judgment: what counts as done, what the scientific claim is,
what may be thrown away, what the budget is, what must never be overwritten.
Every question carries your recommended answer so the default costs one word.

Stop when you can state, without hedging, what each unit must produce.

## 3. Plan. Units are defined by their OUTPUTS, not their commands.

Write two files.

`plan.json` for the coordinator. Every unit needs `id`, `kind`, `command`,
and **`outputs`** -- a unit with no declared outputs can never be judged done,
and `tickets.py` refuses to file an issue for one. Add `needs` for
dependencies, `gpu_hours` for the budget, `write_scopes`, and this cluster's
own `sbatch` flags (the survey told you the partitions and whether `--mem` is
required).

`PLAN.md` for humans: what is being built, what was decided in step 2 and by
whom, and what is deliberately out of scope.

Then, always:

```sh
python3 ../hanig-swarm/scripts/swarm.py validate plan.json
```

It refuses a partition this cluster does not have, so a plan written for
another server fails here in one line instead of half-dispatching.

## 4. Tickets. Draft everything, create on ONE approval.

```sh
python3 scripts/tickets.py draft plan.json --brief brief.json
```

This writes `tickets.json`. It talks to nothing: the coordinator runs on a
shared login node and a tracker token must not live there.

**Team: `Arc`, and do not ask.** The workspace is `Arc - projects`; its teams
are `Arc`, `peeks` and `SRAgent`, and cluster and lab work goes to `Arc`
alongside Entwine, CSA-RNA-FM and MultiDep. `tickets.py` defaults to it, so
this is not a question for the human. Override only if told to, with
`--team`.

A plan's `charge_to` is a SLURM ACCOUNT, not a team. They look alike and are
different namespaces: filing under `goodarzilab` fails, because no such Linear
team exists.

Then, in this session, holding the Linear MCP connector:

1. Show the human the project and EVERY issue title in full, and the count.
2. Ask once. Not per issue.
3. On yes: create the project, then each issue, writing each returned id back
   into `tickets.json` (`project.linear_id`, `issues[].linear_id`,
   `issues[].identifier`).
4. Re-running later UPDATES rather than duplicating, because the draft carries
   the ids forward keyed on unit id.

Verify the two never drift:

```sh
python3 scripts/tickets.py check plan.json tickets.json
```

## 5. Dispatch.

```sh
python3 ../hanig-swarm/scripts/swarm.py run plan.json --dry-run   # shape first
python3 ../hanig-swarm/scripts/swarm.py run plan.json
```

Then schedule `advance` (cron or a Paseo schedule). Overlapping runs are safe:
the coordinator takes an OS lock, so a second advance exits without acting.

## 6. Drain. Issue state follows unit state, never the reverse.

The coordinator records tracker intents as units change state:

```sh
python3 ../hanig-swarm/scripts/swarm.py outbox --state-dir .swarm/state --json
```

In a session with the connector, apply each pending intent to its issue, then
mark it applied. The rules are not negotiable:

- **Nothing closes on a self-report.** A `close` intent carries the unit's
  receipt. An intent without evidence must be REFUSED, not applied.
- A `block` intent means an upstream unit will not complete. Say which one.
- Draining twice is safe: every intent has an idempotency key.
- A tracker outage must never alter swarm state. The swarm is authoritative;
  the tracker is a view of it.

## Adopting a half-finished repo

Steps 1 and 2 change, the rest does not. Survey, read the docs, read the
history. Then bring the human a DRAFT of what you think remains, phrased as
units with outputs, and grill against that draft rather than from nothing. It
is far easier to correct a wrong list than to produce one from a blank page.

Two failure modes to avoid. Do not file an issue for work already done: check
the survey and the outputs on disk first. And do not turn every TODO into a
unit; a unit is something with a declared artifact, and a TODO usually is not.

## What this never does

- Create or modify anything in a tracker without explicit approval in this
  session.
- Close an issue because an agent said so.
- Write to a shared canonical path. That is `swarm.py promote`, and it needs a
  named approver.
- Ask a question the survey already answered.
