<!-- docs-truth:status -->
Status: live
Date: 2026-10-19
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

### Resolver: four-digit dated ids in hyphen families with an empty suffix

- Issue and unit: ARC-2478, `m1d` and `m1e`, [PR #247](https://github.com/hanig/multi-agent-skills/pull/247).
- Original verdict and round: Sol, m1d round 1, `REVIEW_FAIL`: a family member was simultaneously labelled a dated snapshot and adopted by `select()`. The point-release parse predates m1; PR #247 makes candidate reporting disjoint from family membership, without changing that parse.
- Tested revision: m1e `60d13445d26707acca733749a88d937d28191911`, retaining the existing `version_of` and `select` behavior.
- Reproduction: The m1e [plan](plan-model-resolution.md) and evidence at `$HOME/.local/state/hanig-swarm/projects/multi-agent-skills-82e3a975eef5/runs/m1e-resolver-candidates/5258a0f4d7f2f890/evidence.md` record the m1d scenario: `version_of` returned `(3, 1017)` and `select` chose `kimi-k3-1017`. With a hyphen separator and empty suffix, four digits after the major parse as a point release even when they denote a date. Since PR #247 a `CANDIDATE` row never names a family member.
- Consequence bound: No configured seat is affected by the listings measured on 2026-10-18. Anthropic's hyphen-separated seats received `claude-sonnet-4-5-20250929` and `claude-haiku-4-5-20251001`; their eight-digit dates exceed the six-digit numeric-run bound. Kimi K2.7 requires `-code` and uses a dot separator. This bounds the observed roster, not future listings or new family configurations.
- Operator recovery (and demonstration, if required): Pin the seat exactly by omitting its `family`; the resolver skips seats without a family. The m1e evidence records exact adopted dated-pin tests with no family. Review a routing change before adopting a dated id.
- The five nonblocking answers:
  1. The affected consumer is automatic point-release selection for a hyphen-separated, empty-suffix family; candidate reporting no longer mislabels that member as unadopted.
  2. The `(3, 1017)` reproduction establishes the parse, while the dated catalog observations and configured suffixes above bound its current impact.
  3. No parser, selection rule, reader, or shared guard is weakened by recording this existing limit; candidate/family disjointness remains required.
  4. The owner's 2026-10-18 m1 continuation retained `version_of` and `select`, withdrew the snapshot field, and made exact dated adoption a pin without `family`; the mandate's supported-envelope rule governs reassessment.
  5. A configured seat selecting a four-digit dated id as a point release would falsify the current-roster impact bound; a family member in a `CANDIDATE` row would violate the repaired reporting rule.
- Maintainer: The orchestrator.
- Reassessment triggers: A listing gains four-digit dated ids for a hyphen-separated, empty-suffix family, or a hyphen-separated, empty-suffix family is newly configured relative to the 2026-10-18 roster above.

### Flake triage: tests run with the operator's privileges

- Issue and unit: ARC-2479, `p4` through `p4d`, [PR #248](https://github.com/hanig/multi-agent-skills/pull/248).
- Original verdict and round: Findings across p4 round 3, p4b and p4c; p4c's final round 1 returned `REVIEW_FAIL`. The owner accepted the residual on 2026-10-18; p4d records withdrawal of every isolation/path-hiding claim. Those decisions do not relabel the original verdicts as passes.
- Tested revision: p4 `1c0995a08cc7ced1145b5426b4281518d015be9b` for detached-process survival; p4c `a27d0017c388052ed4becc9c16b56b8f45be688e` for environment and committed-content exposure.
- Reproduction: Sources are `$HOME/.local/state/hanig-swarm/projects/multi-agent-skills-82e3a975eef5/runs/p4-flake-triage/879f762ad7f37dcd/reproduction-round-3.txt` and `runs/p4c-flake-triage/5e5b541f8462f593/evidence.md` under the same project directory. The detached-helper run recorded `timeout_seconds: 0.5`, `cli_elapsed_seconds: 1.1300773749826476`, `late_side_effect_seconds_after_return: 2.3932172500062734`, `helper_alive_after_return: true`, `side_effect_after_return: true`, `scratch_removed: true`, and `registry_unchanged: true`; the CLI returned `0` / `CONSISTENT_FAILURE`. Separately, `OPERATOR_REPO`, `REPO`, and committed `operator.txt` exposed a fixture operator path; tests changed its canary ref and triage returned `0` / `INCONCLUSIVE`. The cited p4c evidence describes "separate-session helper tests" and says its path-discovery fixture did not search process arguments. These probes do not measure process-argument discovery.
- Consequence bound: These are local tests running with the operator's privileges. The measured environment/content probes changed refs while config, worktree list and objects stayed unchanged; that fixture result is not a general bound on privileged code. A helper can outlive the timeout and scratch cleanup. The owner's 2026-10-18 accepted limit, supplied by the `l2-ledger-flake-declaration` task, also covers discovery through process arguments and processes that detach into their own session. The accepted limit supplies no process containment or protection for a repository discovered by other means, and a classification never substitutes for a complete green run.
- Operator recovery (and demonstration, if required): Kill identified processes still running from the scratch path and inspect the helper's outputs, even if the scratch directory has been removed. Compare repository refs with an independent pre-run snapshot and inspect affected state; restore incident-specific changes only after operator review. Review the selected test code and committed content and remove known path-bearing environment variables before reuse, without treating that as a sandbox. The recorded helper survival and canary-ref changes demonstrate why cleanup alone is insufficient; they do not demonstrate general containment or recovery of arbitrary privileged side effects.
- The five nonblocking answers:
  1. The affected consumers are the local triage operator and any repository or output reachable by the selected tests or detached helpers.
  2. The timeout/late-effect measurement and fixture-only ref changes above establish concrete consequences, not a guarantee that arbitrary test code preserves the operator's work.
  3. No shared test, verifier, merge check, or required isolation boundary is relaxed: the owner withdrew the tool's isolation/path-hiding claims, and a complete green run remains required for merge.
  4. The owner's explicit 2026-10-18 acceptance of full-privilege execution and withdrawal of containment claims establishes this limit; this is not an author-created exception to the mandate's never-overridable classes.
  5. Use on code the operator did not write or review, unexpected surviving helpers or side effects, or failed recovery invalidates reuse of the accepted operational bound and requires reassessment.
- Maintainer: The orchestrator.
- Reassessment triggers: The tool is used on test code the operator did not write or review.

### Flake triage: bare repositories are refused

- Issue and unit: ARC-2479, `p4d`, [PR #248](https://github.com/hanig/multi-agent-skills/pull/248).
- Original verdict and round: Sonnet, p4d round 1, minor / low confidence, reproduced. The overall verdict was `REVIEW_FAIL`; the owner accepted this input-form refusal as a documented limit on 2026-10-18, not as a passing review.
- Tested revision: p4d `0b78ad0724245c6a604059eb342f8ab499ee4f59`, compared with original p4 `1c0995a08cc7ced1145b5426b4281518d015be9b` and p4c `a27d0017c388052ed4becc9c16b56b8f45be688e`.
- Reproduction: `$HOME/.local/state/hanig-swarm/projects/multi-agent-skills-82e3a975eef5/runs/p4d-flake-triage/f4b00ba9fcfecc7c/evidence.md` records otherwise passing pinned tests with `--repo` naming a bare repository, a full checkout's `.git`, or `.git/objects`: each returned exit `2` / `SETUP_ERROR`, with `fatal: this operation must be run in a work tree`. Original p4 returned exit `0` / `INCONCLUSIVE` for all three; p4c already returned the same refusal as p4d.
- Consequence bound: A loud setup refusal for those repository path forms. In all nine comparisons, refs, config digest, worktree list and object counts stayed unchanged; no work was lost. This supplies no passing test result and does not change merge authority.
- Operator recovery (and demonstration, if required): Retain the repository and pinned commits. For a non-bare checkout, pass its working-tree root as `--repo`, rather than `.git` or a descendant. If only a bare repository exists, create a separately owned full non-bare checkout, verify both pinned commits there, and use its working-tree root. Success is triage completing without this setup refusal, not a merge pass. The evidence records green working-tree tests and unchanged operator state for the refused fixtures; it does not record a separate bare-to-full-checkout recovery trial.
- The five nonblocking answers:
  1. The affected consumer is an operator supplying a bare repository or a path inside `.git` to local flake triage.
  2. All three path forms fail explicitly with exit `2` / `SETUP_ERROR`; nine comparisons preserved the measured Git state and lost no work.
  3. No test, verifier, classification rule, or merge requirement is weakened; a refused setup remains unavailable comparison evidence.
  4. The owner's 2026-10-18 acceptance makes this a documented input limit; the mandate's supported-envelope policy permits recording bounded refusals with written recovery and preserved work.
  5. The same refusal on a supported full checkout's working-tree root, changed operator state, or failure of the written checkout recovery would falsify the bound.
- Maintainer: The orchestrator.
- Reassessment triggers: The orchestrator needs to triage a repository it holds only as a bare clone.

### Plan gate: the verdict rejects clean task texts

- Issue and unit: ARC-2578, `w2` and `w2b`, [PR #251](https://github.com/hanig/multi-agent-skills/pull/251); also cited by ARC-2483, as identified in the owner's 2026-10-19 ledger task. [PR #252](https://github.com/hanig/multi-agent-skills/pull/252) implements ARC-2483's advisory planning workflow.
- Original verdict and round: Luna in w2 round 1 and Kimi K2.7 in w2b round 1, both `REVIEW_FAIL`. The w2b reviewer is recorded as `kimi-k2.7-code` / `moonshotai/kimi-k2.7-code` in `$HOME/.local/state/hanig-swarm/projects/multi-agent-skills-82e3a975eef5/runs/w2b-plan-pair/110f704fc2998a7b/review-round-1.json`. PR #251 records the orchestrator's adjudication and deferral; neither verdict becomes a pass.
- Tested revision: w2 `59c70c271b14614c90dc206e3ebbfb68532516e7`, recorded in `$HOME/.local/state/hanig-swarm/projects/multi-agent-skills-82e3a975eef5/runs/w2-plan-pair/df4a8b6a95700597/finding-reproduction.txt`.
- Reproduction: Replaying the archived benchmark responses, every measured pair returned `PLAN_FAIL` on every completed clean control: astra and kimi-k3 (the pair on main at the time), 8 of 8; luna and kimi-k3, 8 of 8; kimi-k3 and deepseek-v4-pro, 7 of 7. There were three clean task texts and three trials per pair; the remaining four trials were `REVIEW_UNAVAILABLE`. The bad specs drew roughly two to five times as many major findings per completed review, even though the verdict did not distinguish them from the clean controls. Sources: the w2 reproduction above and `$HOME/.local/state/hanig-swarm/orchestrator/bench/` files `spec-adjudication.json`, `runs-spec/`, `runs-spec-controls/`, and `spec-controls/SHA256SUMS`. This is an offline replay of archived responses, not a new live model sample.
- Consequence bound: In these task-text measurements the verdict carries no signal. Since PR #252, the planning phase uses plan-gate findings as advisory input recorded in the decision ledger, so no plan is blocked by the verdict; no work is lost. This limit does not waive implementation review or candidate-merge checks.
- Operator recovery (and demonstration, if required): Read the findings, not the verdict. Use PR #252's existing advisory planning workflow to consider the findings and record their dispositions in the decision ledger. This supplies planning input, not permission to waive a requirement or relabel a review. No lost-work restoration is required; the archived replay demonstrates the refusal, and no separate recovery trial is claimed.
- The five nonblocking answers:
  1. The affected consumer is project planning's use of the plan gate on task texts; findings can inform decisions even when the verdict cannot distinguish clean controls from bad specs.
  2. The archived clean-control replay and bad-spec comparison above bound the observation; PR #252 removes verdict-based obstruction in the planning phase and preserves the findings in its decision ledger.
  3. Recording this limit changes no test, prompt, roster, verifier, shared guard or candidate-merge requirement; implementation review remains required.
  4. The owner's 2026-10-19 ledger task directs recording this deferred limit; PR #251 records its historical orchestrator adjudication under the mandate's *Findings outside the supported envelope* rule. This entry does not classify clean text as an unsupported input or authorize deferral of another finding. The mandate's concurrence rule and never-overridable requirements still apply.
  5. A pair or prompt change that passes clean controls would invalidate the measured verdict limit; any use of the verdict as a pass/fail gate would invalidate the no-obstruction bound.
- Maintainer: The orchestrator.
- Reassessment triggers: A pair or prompt change that passes clean controls, or any use of the plan gate's verdict as a pass/fail gate.
