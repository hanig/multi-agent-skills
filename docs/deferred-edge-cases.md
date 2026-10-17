<!-- docs-truth:status -->
Status: live
Date: 2026-10-17
Title: Deferred edge cases
Purpose: This is the canonical ledger of reproduced findings deferred outside a task's declared supported envelope.

# Deferred edge cases

This is the canonical ledger of reproduced findings deferred outside a task's declared supported envelope. The *Findings outside the supported envelope* section of [the orchestrator mandate](orchestrator-mandate.md) governs eligibility, evidence, recovery and reassessment. Use the record format below for each entry.

## Record format

### Entry title

- Issue and unit:
- Original verdict and round:
- Tested revision:
- Reproduction:
- Consequence bound:
- Operator recovery (and demonstration, if required):
- The five nonblocking answers:
- Maintainer:
- Reassessment triggers:

## Entries

### Scope-check subshell and GIT_CEILING_DIRECTORIES

- Issue and unit: ARC-2208, `e3d-protocol-scope-check`, [PR #233](https://github.com/hanig/multi-agent-skills/pull/233).
- Original verdict and round: Found by the luna reviewer in a replay benchmark of e3d's round-1 review on 2026-10-17; the original round 1 passed.
- Tested revision: main at `bfa2a5f`.
- Reproduction: Measured on 2026-10-17 by the orchestrator: with `GIT_CEILING_DIRECTORIES` set to `<worktree>/skills`, `git rev-parse --verify HEAD^{commit}` resolves from the attempt worktree root and fails from `<worktree>/skills/x`.
- Consequence bound: Only the worker's advisory scope-check, and only when it runs from a subdirectory below an inherited ceiling. It fails loudly. Merge authority is untouched, because `merge_unit` requires the coordinator's judged report.
- Operator recovery (and demonstration, if required): Run the rendered command from the attempt worktree root, as the protocol instructs, or unset `GIT_CEILING_DIRECTORIES`. The root invocation resolving HEAD is demonstrated by the measurement above.
- The five nonblocking answers:
  1. Affects only the advisory worker check.
  2. Bounded by the measurement above.
  3. No shared test, verifier or guard is weakened.
  4. Permitted by the mandate's *Findings outside the supported envelope* section.
  5. Falsified if any supported path runs the check from a subdirectory.
- Maintainer: The orchestrator.
- Reassessment triggers: The protocol ever runs scope-check outside the worktree root, or a supported host is found that sets `GIT_CEILING_DIRECTORIES` globally.

### Named seams in the routing test

- Issue and unit: ARC-2284, `r2b-default-local`, [PR #241](https://github.com/hanig/multi-agent-skills/pull/241).
- Original verdict and round: `REVIEW_FAIL` at round 2 (`kimi-k2.7-code`); the other two reviewers upheld every claim. This was the first deferral under the envelope rule.
- Tested revision: `71f4d57`, merged as `bfa2a5f`.
- Reproduction: `tests/test_merge_verification_routing.py` patches production seam functions by exact name (for example `V._observe_execution`), so renaming one makes the suite error with `AttributeError`.
- Consequence bound: An immediate, loud test error, never a silent pass; no effect on production behaviour. Observing the named seams is required by the step-back exit criterion.
- Operator recovery (and demonstration, if required): Update the seam names in the test and in its docstring list.
- The five nonblocking answers:
  1. Affects only test maintenance.
  2. Bounded by the loud failure.
  3. No guard is weakened; the coupling is what keeps the guard honest.
  4. Permitted by the mandate's *Findings outside the supported envelope* section and the committee exit criterion.
  5. Falsified if a rename ever made the test pass while routing went remote.
- Maintainer: The orchestrator.
- Reassessment triggers: The seams are renamed, or `merge_unit` gains another remote route.
