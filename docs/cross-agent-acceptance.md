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

### Current automated evidence (2026-09-05)

Release-validation follow-ups are collected in draft
[PR #6](https://github.com/hanig/multi-agent-skills/pull/6). Independent safety
review and fix verification are recorded in
[the review report](arc-274-independent-review.md) and
[the verification report](arc-274-fix-verification.md).

The coordinator passed 111 integrated focused tests in 45.389 seconds, then
11 final frontmatter/default-selection tests in 3.888 seconds after the last
scalar fix. The first CI run with both native and regression jobs ran 1,564
tests on each OS, with one error on each: the shallow checkout lacked the
`origin/main` reference required by the vendored-source audit. CI now fetches
the baseline refs; the final full-suite result is a separate required PR check,
not inferred from these focused passes. Exact evidence is retained in the PR
checks and workflow artifacts.

### Historical implementation checkpoint

The final focused installer, lifecycle, diagnostic, report-schema, cross-agent
acceptance, and affected legacy compatibility suites pass in the supported
local Python 3.9 environment: 112 tests in 196.765 seconds
(`/tmp/arc-281-final-targeted.DFsa5J`).  A hermetic full-suite run completed
1,539 tests in 568.709 seconds and initially exposed 15 failures; the
retained transcript is
`/tmp/arc-281-full-regression.JPLo0k/full.log`.  The bounded follow-up fixes
schema-4 report support, failure recovery for a linked payload's sidecar,
foreign-entry uninstall reporting, prefix diagnostics, and a hard-stop survey
path; no native-agent conclusion is inferred from that automated evidence.

## Native release record

For each declared supported release of Claude Code, Codex, OpenCode, and Pi,
record the following in the release artifact before approving the release:

| Host agent and version | OS | Discovery observation | Representative invocation and result | Status / blocker |
| --- | --- | --- | --- | --- |
| Claude Code 2.1.261 | macOS and Linux | Native skill debug trace found the installed copy | Not passed; credentialless resolution reached the model boundary only | Discovery verified; invocation unverified |
| Codex CLI 0.153.4 | macOS and Linux | Native app-server skills/list returned the installed copy without errors | Not run | Discovery verified; invocation unverified |
| OpenCode 1.18.29 | macOS and Linux | Native debug skill command returned the installed copy | Not run | Discovery verified; invocation unverified |
| Pi 0.73.1 | macOS and Linux | Native SDK DefaultResourceLoader returned the installed copy without diagnostics | Not run | Discovery verified; invocation unverified |

Exact commands, package provenance, OS versions, and the distinction between
discovery, standalone capture, and model invocation are in
[native-agent-validation.md](native-agent-validation.md). The first two-platform
native pass is [CI run 33974800468](https://github.com/hanig/multi-agent-skills/actions/runs/33974800468),
on PR head `a1df089` (merge ref `459841f`). The later no-Claude fixture and
installed handoff capture passed locally with pinned packages; their final CI
checks remain distinct from that earlier run. None of these observations proves
model-driven invocation or cross-host agent handoff consumption.

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
