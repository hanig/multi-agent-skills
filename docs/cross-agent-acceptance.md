# Cross-agent release acceptance

This is the release record for the portable skill installer.  It separates
repeatable, hermetic coverage from the small amount of native-agent evidence
that must be collected on real supported CLI versions.  A skipped native check
is a release gap, not a passing check.

## Automated matrix

Run the automated matrix from a checkout, with a disposable home and an
unrelated project directory.  It must never write an agent's real configuration
or skill store.

```sh
python3 -m unittest tests.test_cross_agent_acceptance -v
```

The test creates its own temporary source copy, home/config roots, and project
cwd.  Its required scenarios are:

- no target, one explicit target, all targets, and an explicitly absent target;
- custom roots and two agents sharing one root without deleting each other's
  managed skills;
- copy and link installation, deterministic bundled-workflow execution after
  the source checkout is unavailable in copy mode, upgrade, dry run, and
  selective uninstall;
- foreign same-name skills and managed same-name skills, including provenance
  and permissions; and
- migration, diagnostic payloads, and rollback/recovery after an injected
  install failure.

The test is contract coverage for the public commands.  It does not pretend to
prove that an installed directory is natively loaded by a vendor CLI.

## Native release record

For each declared supported release of Claude Code, Codex, OpenCode, and Pi,
record the following in the release artifact before approving the release:

| Host agent and version | OS | Discovery observation | Representative invocation and result | Status / blocker |
| --- | --- | --- | --- | --- |
| Claude Code 2.1.261 | macOS 26.0, 2026-09-05 | CLI found by disposable-root `--version` probe; no native load checked | not run | **unverified** — no native invocation was run |
| Codex CLI 0.153.4 | macOS 26.0, 2026-09-05 | CLI found by disposable-root `--version` probe; no native load checked | not run | **unverified** — no native invocation was run |
| OpenCode | macOS 26.0, 2026-09-05 | `--version` did not complete inside the bounded two-second probe (CPU/AVX warning printed) | not run | **unverified** — version probe is dependency-limited |
| Pi | macOS 26.0, 2026-09-05 | executable absent | not run | **unverified** — CLI unavailable |
| Claude Code | Linux | pending | pending | unverified |
| Codex | Linux | pending | pending | unverified |
| OpenCode | Linux | pending | pending | unverified |
| Pi | Linux | pending | pending | unverified |

Use a bundled workflow with a deterministic local script and run it from an
unrelated project directory.  In non-Claude rows, ensure no `claude` executable
is on `PATH` and no `~/.claude` tree is supplied.  Record the actual CLI version
and the script's observable result.  Do not use paid prompts or personal
credentials for this evidence; if a real CLI or connector is unavailable, keep
the row `unverified` and name the dependency.

## Cross-host handoff

In a disposable root, create a handoff with one supported host and consume it
with a different host.  Preserve the handoff artifact, the producer and
consumer versions, and the deterministic result.  Also exercise a workflow
whose optional host capability is unavailable: the output must state the
missing capability and the fallback or refusal, rather than silently claiming
completion.

## Signoff rule

Automated coverage is necessary but insufficient.  Release signoff requires
passing hermetic tests plus a completed native record for every supported
agent/OS combination, or an explicit, retained release exception for every
unverified combination.
