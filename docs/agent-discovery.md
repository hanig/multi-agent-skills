# Agent skill discovery contract

`skills/hanig-project/scripts/agent_discovery.py` is the one read-only
contract for user-level skill discovery on macOS and Linux. It is bundled with
the project skill, so a copied survey can import it without the source checkout.
It does not make a directory, start an interactive agent, read credentials, or
make a model request.

Dated certification is exact-version evidence, retained in each adapter's
`certifications` list. The 2026-09-05 package-manifest records remain in that
history. **ARC-281 live run** adds 2026-09-25 records for Claude Code 2.1.282,
Codex CLI 0.154.0, OpenCode 1.18.29, and Pi 0.86.1, covering native discovery,
authenticated skill invocation, and cross-agent handoff. The observed roots
and directed handoffs are recorded in
[native-agent-validation.md](native-agent-validation.md#arc-281-live-certification--2026-09-25).
Discovery reads this evidence; it does not itself start a real agent session.

`verified_versions` and adapter-level `verified_on` summarize the retained
records. A detected version uses its own newest matching certification for
`verified_on`, `verification_review_due`, and `evidence.certification`.
`source_verification` distinguishes historical source inspection from the live
checks. A different release receives no matching certification. Selection
keeps the existing 30-day expiry rule: the new records expire after 2026-10-25,
and older distinct versions after 2026-10-05. Unknown or stale versions remain
`unverified` and emit selection warnings; refreshing one version cannot extend
another's date. No record is deleted just because it ages out.

## Consumer API

The module uses only Python's standard library. Its public functions return
JSON-serializable dictionaries:

```python
from agent_discovery import discover, select_targets

report = discover()                 # bounded `<agent> --version` probes only
plan = select_targets(report)        # consider every detected supported agent
bootstrap = select_targets(report, agents=("pi",))  # binary may be absent
```

`schema()` returns the draft 2020-12 JSON Schema. Every discovery report has
`schema_version: 2`, an `agents` object, and a normalized `destinations` list.
Each agent reports `state` (`executable_found`, `slow`, `probe_failed`, `configured`, or
`absent`), `verification`, roots, evidence, source URLs, and duplicate
behaviour, plus `source_verification` for the release/root-policy/native-runtime
distinction. `adapters()` exposes the versioned static records for callers that
need a UI without probing the machine.

`discover()` accepts injectable `which` and `probe` callables. Its normal
probe is `<resolved executable> --version` with a monotonic deadline derived from the adapter's measured probe timing.
It drains stdout/stderr into fixed 240-byte in-memory tails, uses a short-lived
supervisor process group, and kills inherited writers after the direct child
reports, so noisy or detached-looking probes neither spool output nor survive.
It does not treat an existing configuration directory as a runnable installation:
that is `configured` evidence only. Conversely, a successful, version-verified
binary is eligible even if no skill directory exists yet.

`select_targets(report, agents=(), exclude_agents=())` considers every detected
agent by default. Automatic mode selects successful and slow executable probes,
independently of certification, and reports absent, configured, and failed-probe
agents in `skipped`. Unverified or expired versions still receive destinations
and certification warnings. An explicit `agents` sequence supports
offline/bootstrap installation.
It plans destinations in the fixed adapter declaration order, so reversing
equivalent `--agent` flags does not change filesystem topology. The `selected`
and `skipped` presentation records still retain caller order. It collapses a
target when an already planned physical destination serves that agent, then
returns `competing_visibility` for any unavoidable overlap; callers must carry
that field into their public result rather than replacing it with lifecycle
preflight conflicts.
`select_target()` remains a compatibility helper for callers that need exactly
one target. In each planned destination, `consumers` is all loader exposure,
whereas `selected_agents` is only the requested lifecycle ownership; a covered
requested agent is included in `selected_agents`, but an exposed unrequested
agent is not.

## Effective user roots

`resolve_roots()` records both the logical path and `realpath` physical path.
`destination_consumers()` groups physical paths and lists every known consumer,
so a symlink alias is one destination rather than two writes.

| Agent | User roots and environment behaviour | Probe | Official evidence |
| --- | --- | --- | --- |
| Claude Code | `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills`; `CLAUDE_CONFIG_DIR` replaces the complete user config root. | `claude --version` (version probe only) | [2.1.261 manifest](https://registry.npmjs.org/@anthropic-ai/claude-code/2.1.261), [environment variables](https://code.claude.com/docs/en/env-vars) |
| Codex CLI | `$HOME/.agents/skills` is the current shared/preferred root. `${CODEX_HOME:-$HOME/.codex}/skills` remains a loader-supported legacy root. | `codex --version` (version probe only) | [0.153.4 manifest](https://registry.npmjs.org/@openai/codex/0.153.4), [current loader](https://github.com/openai/codex/blob/main/codex-rs/core-skills/src/loader.rs) |
| OpenCode | `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills` and the loader-supported `$HOME/.opencode/skills`; `OPENCODE_CONFIG_DIR/skills` is an additional (not replacing) config-directory source. Its Claude-compatible root is fixed `$HOME/.claude/skills`, not `CLAUDE_CONFIG_DIR`. | `opencode --version` (version probe only) | [1.18.29 manifest](https://registry.npmjs.org/opencode-ai/1.18.29), [1.18.29 loader](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/skill/index.ts) |
| Pi | `${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/skills`; the override replaces Pi's user config root. Its automatic shared root is `$HOME/.agents/skills`. The native harness accepts the old `@mariozechner` and current `@earendil-works` package scopes. | `pi --version` (version probe only) | [validated 0.73.1 manifest](https://registry.npmjs.org/@mariozechner/pi-coding-agent/0.73.1), [current renamed package](https://registry.npmjs.org/@earendil-works/pi-coding-agent), [0.73.1 skills source](https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/docs/skills.md) |

`CODEX_HOME` is intentionally not used to relocate `$HOME/.agents/skills`.
The latter is a user-home shared root; the former is Codex's legacy config
root. `OPENCODE_CONFIG_DIR` is intentionally additive: OpenCode's current
`ConfigPaths.directories()` returns its normal global directory before that
custom directory. An XDG value is resolved only for OpenCode's standard global
configuration directory; it does not alter the other agents' homes.

## Shared roots, precedence, and safety

The `destinations` map represents the loader overlap which an installer must
show before copying a second copy of the same skill:

| Destination root | Known consumers | Same-name/symlink result |
| --- | --- | --- |
| Claude user root | Claude; OpenCode only when it is the fixed `$HOME/.claude` path | Claude's collision and symlink semantics are not documented in the verified source set: `unverified`. |
| `.agents` user root | Codex, OpenCode, Pi | Codex's collision and symlink semantics are `unverified`; OpenCode uses last registered name; Pi deduplicates canonical-path aliases. |
| Codex legacy root | Codex | Codex collision and symlink semantics are `unverified`. Pi can consume it only if a user separately lists it in Pi settings, which this read-only contract does not infer. |
| OpenCode native/custom roots | OpenCode | A duplicate name replaces the earlier registered item (the loader emits a warning); target-symlink equivalence remains `unverified`. |
| Pi native root | Pi | Pi's documented source ordering makes later sources win; its current changelog records canonical-path symlink deduplication. |

OpenCode's fixed-home Claude and `.agents` compatibility stores, and Pi's
`.agents` store, are represented as actual roots even though they are not every
agent's preferred write root. Pi does not automatically scan Claude or Codex
roots; it can consume them only through a user settings entry, which this module
does not parse. Whether a particular Pi session loads a project resource can
additionally be gated by Pi project-trust/settings policy. The contract makes no claim that
an unprobed version will load any root, and it cannot infer `skills` entries
from a user-managed OpenCode or Pi settings file without a separate,
format-versioned configuration reader.

For a fleet containing Claude and Codex, no single built-in user root serves
both. A caller may explicitly request both destinations, but must surface that
OpenCode can see both copies, with its documented precedence; Pi automatically
sees only the `.agents` copy. The module does not hide that conflict or mutate
configuration to resolve it.
