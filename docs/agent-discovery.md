# Agent skill discovery contract

`skills/hanig-project/scripts/agent_discovery.py` is the one read-only
contract for user-level skill discovery on macOS and Linux. It is bundled with
the project skill, so a copied survey can import it without the source checkout.
It does not make a directory, start an interactive agent, read credentials, or
make a model request.

The version gates were recorded on 2026-09-05 from the exact package manifests:
Claude Code 2.1.261, Codex CLI 0.153.4, OpenCode 1.18.29, and Pi coding agent
0.73.1. `source_verification` separates that immutable release evidence from
root-policy evidence and from native discovery/invocation; the latter two are
explicitly `unverified` because this module does not start a real agent session.
A detected executable at another version is useful evidence, but is deliberately
`unverified`; do not describe it as compatible until its adapter is checked and
versioned here.

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
`schema_version: 1`, an `agents` object, and a normalized `destinations` list.
Each agent reports `state` (`executable_found`, `configured`, `absent`, or
`undetermined`), `verification`, roots, evidence, source URLs, and duplicate
behaviour, plus `source_verification` for the release/root-policy/native-runtime
distinction. `adapters()` exposes the versioned static records for callers that
need a UI without probing the machine.

`discover()` accepts injectable `which` and `probe` callables. Its normal
probe is `<resolved executable> --version` with a two-second monotonic deadline.
It drains stdout/stderr into fixed 240-byte in-memory tails, uses a short-lived
supervisor process group, and kills inherited writers after the direct child
reports, so noisy or detached-looking probes neither spool output nor survive.
It does not treat an existing configuration directory as a runnable installation:
that is `configured` evidence only. Conversely, a successful, version-verified
binary is eligible even if no skill directory exists yet.

`select_targets(report, agents=(), exclude_agents=())` considers every detected
agent by default. Automatic mode selects only successful, exact-version probes
and reports absent, configured, failed-probe, and unverified-version agents in
`skipped`; an explicit `agents` sequence supports offline/bootstrap installation.
It collapses a later target when an already selected physical destination serves
that agent, then returns `competing_visibility` for any unavoidable overlap.
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
| Claude Code | `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills`; `CLAUDE_CONFIG_DIR` replaces the complete user config root. | `claude --version` (unverified native invocation) | [2.1.261 manifest](https://registry.npmjs.org/@anthropic-ai/claude-code/2.1.261), [environment variables](https://code.claude.com/docs/en/env-vars) |
| Codex CLI | `$HOME/.agents/skills` is the current shared/preferred root. `${CODEX_HOME:-$HOME/.codex}/skills` remains a loader-supported legacy root. | `codex --version` (unverified native invocation) | [0.153.4 manifest](https://registry.npmjs.org/@openai/codex/0.153.4), [current loader](https://github.com/openai/codex/blob/main/codex-rs/core-skills/src/loader.rs) |
| OpenCode | `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills` and the loader-supported `$HOME/.opencode/skills`; `OPENCODE_CONFIG_DIR/skills` is an additional (not replacing) config-directory source. Its Claude-compatible root is fixed `$HOME/.claude/skills`, not `CLAUDE_CONFIG_DIR`. | `opencode --version` (unverified native invocation) | [1.18.29 manifest](https://registry.npmjs.org/opencode-ai/1.18.29), [1.18.29 loader](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/skill/index.ts) |
| Pi | `${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/skills`; the override replaces Pi's user config root. Its automatic shared root is `$HOME/.agents/skills`. | `pi --version` (unverified native invocation) | [0.73.1 manifest](https://registry.npmjs.org/@mariozechner/pi-coding-agent/0.73.1), [0.73.1 skills source](https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/docs/skills.md) |

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
