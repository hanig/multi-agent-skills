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

## ARC-281 live certification — 2026-09-25

The orchestrator checked the ARC-281 live-run artifacts on the operator host.
Each agent received one short authenticated prompt in a scratch Git project,
discovered the installed `hanig-portable-handoff` skill itself, and ran its
script's `capture` followed by `resume`, yielding `HANDOFF_CLEAN`. An independent
`resume` of each produced handoff exited 0. This is retained evidence from
**ARC-281 live run**, not a new invocation performed by this documentation update.

| Agent | Exact version | Observed skill root | Certification date |
| --- | --- | --- | --- |
| Claude Code | 2.1.282 | `~/.claude/skills` | 2026-09-25 |
| Codex CLI | 0.154.0 | `~/.agents/skills` | 2026-09-25 |
| OpenCode | 1.18.29 | `~/.agents/skills` | 2026-09-25 |
| Pi | 0.86.1 | `~/.agents/skills` | 2026-09-25 |

The verified scope is native discovery, authenticated skill invocation, and
cross-agent handoff. Clean directed handoffs were Claude → Pi, Claude → OpenCode,
Codex → Claude, and Pi → Codex. Codex ran as `codex exec -s workspace-write`.
This evidence covers those versions, roots, and directions on that host; it does
not certify every root, all agent pairs, a newer patch, or another OS/host.
The credentialless harness remains a separate discovery check and still reports
its own authenticated invocation as `not_run`. Its `EXPECTED_VERSIONS` and
the release workflow keep the original CI pins; this live evidence adds
certification without changing which packages CI installs and checks.

The installer retains the 2026-09-05 records with their original dates. Each
exact version uses its newest matching record and the existing 30-day review
window: the new evidence is current through 2026-10-25, then selection reports
`unverified` and warns on stderr, including with `--json`. Older distinct
versions expire after 2026-10-05; refreshing a different version cannot renew
them. OpenCode retains both observations of 1.18.29 and uses the newer one.
An unexercised patch remains `unverified` while executable presence still permits
ordinary destination planning.

## Historical native release record (2026-09-05)

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
checks remain distinct from that earlier run. Those historical observations did not prove
model-driven invocation or cross-host agent handoff consumption. The separate
2026-09-25 operator-host observations above add authenticated invocation and
the four named cross-agent directions; they are not new Linux evidence.

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
