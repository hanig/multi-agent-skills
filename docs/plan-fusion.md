# Plan v3: fuse coordination with adjudication

> Author: gpt-5.6-sol. v1 rejected on 6 MAJOR, v2 on 3 MAJOR. Round 3 of the
> 3 the protocol allows on one change.
>
> **Settled by v2 and accepted by both reviewers:** extend `bus await` rather
> than build a parallel controller; no sealed work order; admission is
> point-in-time, not a durable ADMITTED label; per-machine topology.
>
> **What v3 answers.** luna's three findings against v2 were one idea -- a
> predicate that can be satisfied without doing the work -- and that shape has
> since been found THREE times in this repo's own shipped verifiers, twice as a
> false pass at exit 0 (`contract.py` SCIENTIFIC_PASS and `traincontract.py`
> CONVERGED on artifacts written by an unrelated process; both closed at
> 23678b8 and 40ea773). So v3 treats it as the organising principle: a
> five-condition rule for what counts as production evidence, a partition of
> every existing predicate against it, and enforcement in one verdict function.
>
> Note it reclassifies `--base`, which I had assumed WAS the production
> predicate: an arbitrary caller-supplied base is not, because HEAD may have
> advanced earlier. Only a base anchored to the worker's verified launch record
> qualifies.

## The production-evidence rule

A predicate is production-evidencing only when all five conditions hold:

1. **Pre-state:** An observer outside the executor captured the artifact’s state before execution.
2. **Post-state:** An independent observer captured its state after execution.
3. **Material transition:** The predicate requires appearance, content change, or append-only growth—not merely existence, freshness, cleanliness, or a lifecycle transition.
4. **Attempt binding:** The observation window is bound to the same agent/job attempt being admitted.
5. **Artifact binding:** The resulting receipt identifies the exact post-state being accepted.

If any condition is missing, the predicate belongs to another category.

This is a production-attribution rule under a single-writer assumption. A concurrent unrelated process writing the same output during the window remains indistinguishable from the intended process. The existing `result.py` comments correctly acknowledge this limit. Unique run directories, isolated worktrees, and declared output ownership reduce the honest accidental form; solving hostile races would exceed the threat model.

## Actual predicate partition

| Predicate | Category | Can pass through inaction? | Can satisfy production requirement? |
|---|---|---:|---:|
| Agent `idle` | Lifecycle | Yes | No |
| `--expect-cwd` | Execution scope | Yes | No |
| `--expect-provider` | Execution scope | Yes | No |
| `--expect-model` | Execution scope | Yes | No |
| `--expect-thinking` | Execution scope | Yes | No |
| `--expect-branch` | Execution scope | Yes | No |
| `--require-clean` | Hygiene/integrity | Yes | No |
| `--status-file` exists/parses | Agent assertion transport | Yes | No |
| `--require-json PATH` truthy | Agent assertion | Yes | No |
| `git_evidence()` output | Diagnostics | Yes | No |
| Current `--base` with arbitrary caller-supplied base | Relative-state check | Yes: HEAD may have advanced earlier | No |
| Base anchored by this worker’s verified launch record, with final tree changed | Production transition | No | Yes, for a Git artifact |
| Authorized verifier exit 0 without an explicit production claim | Validation | Possibly | No |
| Authorized, content-pinned verifier emitting a bound production receipt | Production plus validation | No, if its production gate is sound | Yes |

### Execution-scope checking

Execution-scope checks are a separate category, not production evidence.

They answer:

> Did the expected executor run in the expected place and configuration?

Production checks answer:

> Did an artifact materially transition during that execution?

Both matter. Neither implies the other.

A correctly configured agent may do nothing. An artifact may change under the wrong agent, wrong job, or wrong environment. Therefore a strong admission requires scope checks and production evidence as separate conjuncts.

The verifier family should eventually adopt the same distinction:

- `contract_id` and attempt binding establish instance ownership.
- Scheduler/job identity establishes execution scope.
- The digest window establishes production.
- Predicates establish artifact validity.
- Current digests establish currency.

Do not collapse those into one “provenance” boolean.

## Classifying `--base` correctly

The existing `--base` is insufficient because `bus await` does not know that the supplied base was the worker’s starting state.

For Git work, production evidence requires:

- A verified `launch-worker` record for the same Paseo agent.
- The record’s cwd and branch matching current inspected state.
- The record’s base matching the HEAD observed during launch preflight.
- Final HEAD being a descendant of that base.
- Final tree digest differing from the base tree digest.

The tree comparison matters. An empty commit advances HEAD but produces no material artifact.

This proves that the isolated worktree’s content changed during the worker interval. It does not prove that the diff satisfies the task; tests, review, write-scope checks, and other validators remain separate predicates.

An unbound `--base` can remain diagnostic or supporting evidence, but cannot unlock `ARTIFACT READY`.

## Enforcing the classification

The classification must live in `bus` data and the single verdict decision—not in help text or caller convention.

Each evaluated predicate should produce a structured internal result with:

- Predicate identifier.
- Category: `lifecycle`, `scope`, `hygiene`, `assertion`, `validation`, `production`, or `diagnostic`.
- Passed/failed/unavailable.
- Evidence detail.
- Binding identifiers where relevant.

The category is assigned by trusted implementation code. The caller must never be able to add `--class production` to an arbitrary predicate.

The admission decision is then:

```text
agent is idle
AND every declared scope predicate passed
AND every declared required predicate passed
AND at least one internally classified production predicate passed
```

Otherwise:

- No production predicate declared: usage refusal naming `--lifecycle-only` or a production-capable path.
- Production predicate declared but unmet: `IDLE WITHOUT ARTIFACT`.
- Scope mismatch: `INVALID LAUNCH CONTRACT`.
- Explicit lifecycle-only wait: `LIFECYCLE SETTLED — no artifact judgment`.

This should be one decision function. The repository already learned that duplicated state selection inside one file is the dominant drift mechanism.

## The new verifier predicate

Do not ship free-form `--verifier CMD` as a production predicate.

A generic verifier can prove many things that are not production:

- `/bin/true` proves nothing.
- A test suite may already pass before the worker starts.
- A schema checker may accept a stale artifact.
- `contract.py check` without `--require-production-evidence` can still reach its legacy success behavior.
- `traincontract.py check` without a production bracket can validate a curve written by another run.

The interface should therefore be policy-based from its first successful version, for example conceptually:

```text
--verifier-policy <base-committed-policy>
```

The policy specifies:

- Exact verifier source SHA-256.
- Invocation template.
- Whether it is validation-capable, production-capable, or both.
- Receipt schema and required success state.
- Required binding fields.
- Required machine-readable production claim.
- Contract fields that must enable production evidence.
- Allowed run-specific substitutions.

No free-form verifier may be classified as production.

A manual free-form check could eventually exist as diagnostic validation, but it must not unlock `ARTIFACT READY`.

## Pinning the verifier by content

Content pinning is achievable for the current standalone Python verifier model, with a deliberately narrow claim.

The base-committed policy stores the expected SHA-256 of the verifier source. At admission, `bus` should:

1. Open the verifier as a bounded regular file.
2. Hash its exact bytes.
3. Compare with the authorized digest.
4. Copy those verified bytes to a private temporary file.
5. Execute that copy with the same Python interpreter running `bus`, preferably isolated from user-site imports.
6. Record verifier digest, interpreter path/version, argv, cwd, and receipt.

Executing a verified copy closes the worktree mutation:

```text
policy authorizes python verify.py
worker changes verify.py
bus hashes changed bytes
digest mismatch
verification never runs
```

Or, if the installed file still matches, the copied authorized bytes run regardless of later worktree edits.

### Scoped claim

This pins the verifier’s Python source bytes. It does not pin all behavior transitively:

- Python interpreter implementation.
- `git`, `sacct`, `squeue`, `shasum`, or other subprocesses.
- Kernel/filesystem behavior.
- Environment variables the verifier intentionally reads.
- Remote services.

Those identities should be recorded when relevant, but “exact behavior is content-pinned” would be false. The honest claim is:

> `bus` executed the exact authorized standalone verifier source bytes under the recorded local interpreter and environment assumptions.

Arbitrary shell commands, binaries, local-import trees, and dynamically loaded verifier plugins should not be production-capable in the first version. Pinning their transitive behavior is not available cheaply and should be left out.

## Verifier receipts must expose production explicitly

Exit 0 alone cannot tell `bus` whether the verifier established production.

A verifier eligible for production admission needs a machine-readable receipt field with at least:

- Contract ID.
- Criteria digest.
- Attempt ID.
- Production state.
- Production method.
- Pre/post artifact identities or growth observations.
- Current accepted artifact identities.
- Verification timestamp.

For example, semantically:

```text
production.state = PASS
production.method = content_window | append_growth | anchored_git_tree_change
production.attempt_id = ...
```

The field is trustworthy only because `bus` has just executed the authorized verifier bytes and parses the fresh output from that invocation. Reading a pre-existing receipt file alone would reintroduce stale self-assertion.

### Current verifier eligibility

- **`result.py`:** conceptually eligible. Its production window is mandatory on the successful path, and its check remeasures current digests. Its receipt should expose that production conclusion explicitly.
- **`contract.py`:** eligible only when the contract has `require_production_evidence: true`, the bracket evidence is bound to the current contract and attempt, and the fresh receipt states that it passed.
- **`traincontract.py`:** eligible only after growth-based production evidence is complete and bound.

The current `traincontract.py` visible on disk already contains an apparent growth/change bracket and `training_production_fault`, despite the prompt describing it as not closed. I treat that as in-progress rather than accepted: the receipt shown on disk does not yet expose the production conclusion, and the bracket evidence must be checked for current-attempt binding.

The same binding requirement should be checked in `contract.py`: the visible `production-window.json` payload records paths and before/after digests but does not visibly name the contract ID or attempt ID. A stale window must never satisfy a later contract merely because its old `written_in_window` values are true.

## Authorization and verifier execution must land together

This is settled: land them atomically.

There must never be an intermediate release where:

```text
bus await --verifier /bin/true
```

can reach `ARTIFACT READY`.

The first verifier-capable release must include all of:

- Base-committed authorization policy.
- Exact verifier source digest.
- Execution of verified source bytes.
- Receipt schema validation.
- Contract/criteria/attempt binding.
- An explicit production claim.
- Internal classification as production-capable.

If this cannot fit safely into one change, keep verifier admission disabled until it can.

## Human and agent authorization

The v2 answer still holds, narrowed further:

- Do not trust an `author: human` field.
- Base-committed project policy is the first supported authorization source.
- Agent-authored novel criteria remain ineligible for production admission until a durable plan-review receipt is bound to their exact digest.
- Do not implement that review-receipt path merely to complete the taxonomy. It is a separate later feature.

This means the first fused system supports repeatable, project-authorized workflows. Novel agent-designed scientific criteria require a human to put them into the trusted base policy before dispatch.

That is a usable limitation, not a missing checkbox.

## PLAN

1. **Introduce predicate categories and make non-production-only awaits incapable of `ARTIFACT READY`.**

   Acceptance criteria:

   - Every existing await predicate is assigned one internal category from the partition above.
   - `--require-clean` alone cannot return exit 0 as `ARTIFACT READY`.
   - `--status-file`/`--require-json` alone cannot return `ARTIFACT READY`.
   - Any combination of `--expect-*`, `--require-clean`, and status assertions remains non-production.
   - Bare await requires explicit `--lifecycle-only` and reports no artifact judgment.
   - An arbitrary unanchored `--base` is not counted as production.
   - A single verdict function requires at least one passing production-class result.
   - Tests enumerate every registered predicate and fail if a future predicate has no explicit category.
   - Tests exhaust all current non-production-only combinations and prove none exits 0 as artifact-ready.

2. **Make anchored Git tree transition the first production predicate.**

   Acceptance criteria:

   - Production-capable Git admission requires a verified `launch-worker` record for the same agent.
   - The launch record’s cwd, branch, provider/model/thinking settings, and base match current observed state.
   - The recorded base was HEAD during launch preflight.
   - Final HEAD descends from the base.
   - Final tree digest differs from the base tree digest; an empty commit is refused.
   - A branch already advanced before the worker launch is refused when paired with a mismatched or absent launch record.
   - `--require-clean` failures and Git command failures fail closed but remain hygiene results, not production.
   - A valid anchored tree transition plus all declared scope/hygiene predicates yields `ARTIFACT READY`.
   - Independent tests cover inaction, empty commit, unrelated pre-launch advancement, non-descendant history, dirty worktree, mismatched record, and a genuine post-launch tree change.

3. **Land policy authorization, verifier content pinning, and production-receipt admission atomically.**

   Acceptance criteria:

   - No free-form verifier command can produce `ARTIFACT READY`.
   - The policy is loaded from the declared base commit and records the authorized verifier SHA-256, invocation, capability class, receipt schema, and locked contract requirements.
   - Worktree edits to the policy or verifier cannot alter the invoked verifier.
   - `bus` executes a private copy of the exact verified Python source bytes with its recorded interpreter.
   - A digest mismatch, non-regular verifier, oversized verifier, unsupported imports/executable shape, timeout, signal, or nonzero exit fails closed.
   - Exit 0 without a fresh machine-readable production claim is validation-only and cannot satisfy the production requirement.
   - The receipt must bind the contract ID, criteria digest, current attempt, production method, and accepted artifact identities.
   - Workflow admission additionally requires `require_production_evidence: true`.
   - Result admission accepts only a success path whose receipt records its production gate.
   - Training admission remains disabled until its growth bracket and checkpoint evidence are bound to the current attempt and exposed in the receipt.
   - Tests replace an authorized worktree verifier with `/bin/true` behavior and prove the changed bytes are never executed.
   - Tests reuse a stale production receipt/window under a new contract or attempt and prove it is refused.
   - Tests prove that an authorized validation-only verifier can strengthen an admission but cannot be its sole production evidence.
