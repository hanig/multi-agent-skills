---
name: pi-fleet
description: >-
  Delegate work from a pi session to the local agent fleet (Claude Code,
  Codex, Kimi Code, other pi models) through Paseo, and get the result back.
  Use when the user asks to hand a task to another model or harness, run
  something in the background, get a second opinion from Claude/GPT, or
  message another agent session.
---

# pi-fleet — calling other model+harness combos from pi

Paseo supervises every harness on this machine; pi is one of its providers.
Delegation from pi is three shell commands away. All state lives in Paseo
(`~/.paseo`) and the agent bus (`~/.agent-bus`) — nothing pi-specific.

## Provider choice

Read `~/.paseo/orchestration-preferences.json` before choosing a provider.
It maps roles (`impl`, `ui`, `research`, `planning`, `audit`) to provider
strings and carries freeform preferences. Only hardcode a provider when the
user named one. For difficulty-matched routing, `~/.agent-bus/bin/bus models --json`
gives intelligence scores, modalities, and live quota/load signals.

## Delegate a task

```bash
paseo run -d --provider <provider/model> --title "<task>" \
  --new-workspace worktree --worktree-mode branch-off --new-branch <branch> --base main \
  "<self-contained prompt>" --json
```

- The prompt must be fully self-contained: the receiving agent has zero
  context. Give task, relevant file paths, constraints, acceptance criteria.
- Use a worktree for anything that edits a shared checkout.
- Note the `agentId` from the JSON output.

## Get the result (this is the notification mechanism)

Paseo's CLI cannot push into a pi session, so wait explicitly:

```bash
~/.agent-bus/bin/bus await <agentId> --timeout 1800
```

`bus await` blocks until the agent settles, then prints a report: verdict
(`finished` / `AWAITING PERMISSION` / `TIMEOUT`), the agent's last activity
lines, token usage, and git evidence (HEAD commit, dirty files, diffstat).
On `TIMEOUT` (exit 1) re-arm it to keep waiting. On `AWAITING PERMISSION`
the agent needs a human — tell the user.

Run it foreground when the pi session is blocked on the result anyway. If pi
has a background-task mechanism in your build, prefer that — the task
completion then acts as the wake-up.

Follow up to a running agent with `paseo send <agentId> "<message>"` — or
equivalently `~/.agent-bus/bin/bus send <shortId-or-name> "<message>"`, which
routes to Paseo when the target isn't a bus session.

## Peer messaging

Other sessions (Kimi Code, Codex, Claude Code) coordinate over the agent bus.
To be reachable:

```bash
~/.agent-bus/bin/bus register pi-<topic> --tool pi --pid $$
~/.agent-bus/bin/bus check pi-<topic>     # read when asked; nothing is deleted on read
```

Full semantics are in the agent-bus skill (`~/.agents/skills/agent-bus/SKILL.md`).

## Rules

- Verify results by artifacts (commit hash, test output), never by the
  agent's own "done" claim. `finished (idle)` is lifecycle state, not proof.
- Never send images to text-only models (e.g. DeepSeek).
- Delegation is opt-in: don't launch fleet agents unless the user asked.
