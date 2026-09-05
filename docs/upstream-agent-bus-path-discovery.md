# Upstream report: discover the agent-bus executable

## Summary

The `paseo` and `agent-bus` skills hardcode `~/.agent-bus/bin/bus`, although
that location is a property of one installer rather than part of the bus's
state contract. A consumer that vendors the skills and `bin/bus` without
reproducing that installer layout receives instructions that cannot run.

Affected instructions in the 2026-08-25 vendored snapshot:

- `skills/paseo/SKILL.md`: model selection runs
  `~/.agent-bus/bin/bus models --json`.
- `skills/paseo/SKILL.md`: CLI-only completion arms
  `~/.agent-bus/bin/bus await ...`.
- `skills/agent-bus/SKILL.md`: the introduction and command examples declare
  `~/.agent-bus/bin/bus` as the CLI location.

## Reproduction

1. Vendor the skill directories and `bin/bus` into a checkout.
2. Install the skill directories without running upstream's installer. This
   is a valid integration: the consumer may use its own skill prefix and keep
   repository executables in the checkout.
3. Confirm `<checkout>/bin/bus` exists and `~/.agent-bus/bin/bus` does not.
4. Ask the `paseo` skill to choose a model when its routing category leaves
   room.

The documented command exits before `bus` can inspect the model registry,
even when `~/.agent-bus/models.json` is present and valid.

## Requested behavior

Treat the executable location and the state directory as separate concerns.
`AGENT_BUS_HOME` may continue to select bus state. Skill instructions should
resolve the executable, fail with an actionable diagnostic when it cannot be
resolved, and never infer an executable under the state directory merely
because the directory exists.

A portable resolution contract would be:

1. Use an explicit `AGENT_BUS_BIN` path when configured, after checking that
   it is an executable file.
2. Otherwise use `command -v bus` when the installer placed `bus` on `PATH`.
3. Otherwise use `~/.agent-bus/bin/bus` only when that file exists and is
   executable, preserving compatibility with the current upstream installer.
4. If none resolve, stop and name all supported installation/configuration
   routes. Do not continue with a guessed path.

The installer should either put `bus` on `PATH` or record the executable path
as `AGENT_BUS_BIN` in whatever launch environment it owns. Consumers that
vendor the files can then set `AGENT_BUS_BIN=<checkout>/bin/bus` without
claiming ownership of `~/.agent-bus`.

## Suggested skill wording

Replace direct references to `~/.agent-bus/bin/bus` with a prerequisite such
as:

> Resolve the agent-bus executable before use: prefer an executable
> `AGENT_BUS_BIN`, then `bus` on `PATH`, then the legacy
> `~/.agent-bus/bin/bus` only if it is executable. If none resolves, stop and
> tell the user how to configure or install it. Use the resolved executable
> for `models`, `await`, and every other bus command.

Apply the same rule in both skills so model routing, peer messaging, and
completion notification cannot disagree about where the CLI lives.

## Acceptance checks

- An upstream-installed setup continues to resolve its existing
  `~/.agent-bus/bin/bus` executable.
- A vendored setup works with `AGENT_BUS_BIN=<checkout>/bin/bus` and no
  `~/.agent-bus/bin/bus`.
- A setup with `bus` on `PATH` works without either fixed path.
- A missing or non-executable candidate produces an actionable error before a
  model-routing or await operation starts.
- Changing `AGENT_BUS_HOME` changes state lookup only; it does not silently
  change the executable path.
