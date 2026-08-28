# Plan v2: fuse coordination with adjudication

> **STATUS: v2 CLOSER, NOT ACCEPTED. Plan review 2026-08-28: deepseek-v4-pro
> UPHELD all 7 claims with 0 findings; luna refuted 1 and found 3 MAJOR. Split
> verdict, and the dissenter was right — the fourth time this session.**
>
> All four withdrawals were accepted by both reviewers. Extending `bus await`
> instead of building a parallel admission controller stands. What fails is the
> classification of predicates and the ORDER of the steps.
>
> **The one idea behind all three findings: a predicate that can be satisfied
> without doing the work.** This is the fourth appearance of that shape in this
> project — it is the same error as `result.py`'s missing production gate, where
> integrity proved the bytes were bound to an attempt and nothing proved the
> command wrote them.
>
> **1. `--require-clean` alone is a completion path (MAJOR).** An idle agent
> already sitting at a clean HEAD, `bus await AGENT --require-clean` with no
> `--base`, no verifier and no `--lifecycle-only`: cleanliness passes and the
> await reports ARTIFACT READY, exit 0, with no new artifact and no work done.
> Step 1 excluded only status/json assertions and counted `--require-clean` as a
> measured git predicate. **Measured is not the same as evidence of
> production.** A clean worktree is a NEGATIVE property, satisfied by inaction.
> Predicates must be classified by whether they can only be true if work
> happened, not by whether the tool measures them itself.
>
> **2. Step 2 opens a hole that step 3 closes (MAJOR).** `bus await AGENT
> --verifier /bin/true` exits 0 and reports ARTIFACT READY. v2 was honest that
> a freely chosen verifier is not independently authorized, but it still
> sequenced the success path BEFORE the authorization mechanism. Honesty about
> a gap does not license shipping the gap first. Either authorization lands with
> the verifier predicate, or the predicate is refused until it does.
>
> **3. Policy from the base commit does not pin the verifier EXECUTABLE
> (MAJOR).** The base policy authorizes `python verify.py`; `verify.py` in the
> inspected worktree is changed to return 0 without validating anything; the
> verifier runs in that worktree cwd, so bus accepts the zero exit. **The policy
> bytes are unchanged while the behaviour is not.** Pinning the authorizing
> policy is not pinning what the policy invokes.
>
> **What v3 must settle.** Classify every predicate as production-evidencing or
> not, and require at least one of the former for ARTIFACT READY. Land verifier
> authorization with the verifier predicate rather than after it. Pin the
> verifier's identity by content, not by the name of the policy that authorizes
> it — and if that cannot be done inside the threat model (agents
> honest-but-fallible, contract files trusted, no signatures), say so and scope
> the claim down rather than implying independence.

---

## What I am withdrawing from v1

I withdraw four material claims:

- **A new admission controller is not warranted.** `bus await` already is the admission controller. The right design extends that chokepoint.
- **The sealed work order should not be the minimum unit.** It duplicates launch records, contract files, attempts, and receipts already present across the two repos.
- **`ADMITTED` should not be a durable universal state.** Admission is a point-in-time verifier result. A permanent `ADMITTED` label creates the stale-artifact problem unless artifacts are immutable.
- **The Mac-centered remote-control topology was wrong.** Deployment is per machine. Verification runs locally beside the artifacts; cross-machine verification, transport, credentials, and central reconciliation are out.

I also withdraw the broad statement that the coordination repo cannot prove anything. It already has a real, tested enforcement point.

## What `bus await` proves today

The implementation confirms your correction.

Sound measurements:

- `launch_contract_issues` compares observed cwd, provider, model, thinking setting, and Git branch with declared expectations.
- `--base` reads Git state itself and refuses when HEAD is absent, the base cannot resolve, or HEAD equals the base.
- `--require-clean` reads the worktree rather than trusting an agent report.
- `cmd_await` distinguishes `idle`, `ARTIFACT READY`, `IDLE WITHOUT ARTIFACT`, and `INVALID LAUNCH CONTRACT`.
- The tests prove that idle without the required commit exits nonzero and a launch mismatch fails closed.

This is genuinely the same thesis: lifecycle settlement is not completion.

But its proof boundary is narrower than its labels imply:

- `HEAD != base` proves that HEAD differs, not that it descends from the base, implements the task, or passes tests.
- `--require-clean` has a fail-open edge: `git_value()` returns `""` both for a clean worktree and for Git failure/timeout. By contrast, `--base` notices missing values and fails closed.
- `git_evidence()` is diagnostic only. Its comment explicitly says evidence may degrade while the verdict still ships.
- `--require-json` proves only that a non-empty value exists in an agent-writable document. It does not prove the value is true.
- An await invocation with no contract flags returns exit 0 for idle. The prose is honest, but shell composition sees success.

So `bus await` is a sound admission layer for the facts it independently measures, with two implementation defects and one intentionally weak predicate. It is not yet an adjudication bridge to the domain verifiers.

## Revised minimum unit

The minimum fused unit is a **contracted await**, not a new work-order subsystem:

```text
Paseo agent identity
+ launch constraints
+ independently measured artifact predicates
+ an authorized verifier invocation
= one admission evaluation
```

The durable components already exist:

- The launch record binds the worker to its execution scope.
- The domain contract binds criteria to a contract instance.
- The domain verifier binds its receipt to criteria and artifacts.
- `bus await` decides whether those obligations have been met.

The missing operation is for `bus await` to execute the declared verifier and treat its result as another conjunctive artifact predicate:

```text
idle
AND launch contract matches
AND Git predicates pass, when declared
AND verifier exits 0
= ARTIFACT READY
```

That is materially smaller and clearer than another state machine.

## Should `--verifier CMD` replace the sealed work order?

Yes, with one qualification: **the command needs independent authorization**.

Adding only:

```text
bus await <agent> --verifier "contract.py check ..."
```

would close the `--require-json` weakness relative to the worker. The worker no longer asserts `validation.passed`; `bus` executes a different process and reads its exit status.

But it does not close the coordinator-level self-assertion. A coordinating agent could select `/bin/true`, a lenient verifier, or a legitimate verifier pointed at a weak contract.

Therefore:

- Extend `bus await`; do not build a parallel controller.
- Keep domain contracts and receipts.
- Add a small verifier-policy mechanism only after the direct execution path works.
- Do not call a freely chosen verifier “independently authorized.”

## What makes a claim evidence rather than assertion?

The distinction is not “JSON versus Git” or even “human versus agent.” It is:

> Evidence is an observation made by an authority that did not control the proposition being judged, using criteria fixed before the judged execution.

Examples:

- The worker says `tests_passed: true`: assertion.
- `bus` runs the test command and observes exit 0: evidence of that command’s result.
- `bus` observes a clean worktree: evidence of cleanliness at that moment.
- A verifier reads an agent-authored contract containing `min_rows: 1`: evidence that the artifact has one row, but no evidence that one row was an adequate criterion.
- A human/project policy fixed `min_rows: 10000` before dispatch and a separate verifier measures it: evidence against independently authorized criteria.

This supports your framing, with one rejection: a field saying `"author": "human"` does not make trusted input human-authored. On one Unix account, an agent can write the same field and file. Under the honest-but-fallible threat model, forgery is not the concern, but accidental self-authorization still is.

The code must enforce provenance, not asserted identity.

## Enforcing human versus agent authorization

Do not add `issuer` and `executor` strings and compare them. That was weak.

Use two mechanically distinguishable authority classes.

### 1. Project-authorized criteria

The acceptance policy exists at the declared base commit before the worker launches.

The await/launch machinery reads it from the Git object database:

```text
git show <base-commit>:<policy-path>
```

It does not read the worker’s current worktree copy.

The policy fixes:

- Verifier argv or an allowed verifier identifier.
- Contract type.
- Which criteria are locked.
- Which fields may be instantiated per run.
- Required success state and receipt fields.
- Whether human scientific review is required.

The machine can prove that this policy predates the worker and that the worker’s edits cannot change what is invoked. “Human-authorized” is then a repository trust assumption: humans approve the base revision. That matches the existing trusted-input threat model.

Fields should describe provenance rather than personalities:

```text
authority.kind = "base_policy"
authority.base_commit
authority.policy_path
authority.policy_blob_oid
```

Code enforcement:

- Resolve the exact base commit.
- Read policy bytes from that commit.
- Verify their blob identity.
- Construct the verifier invocation from those bytes.
- Refuse a policy present only in the worktree.
- Refuse worktree changes that alter locked criteria.
- Never accept an argv supplied by the worker as the authorized verifier.

### 2. Independently reviewed agent-authored criteria

This should be a later capability, not part of the first useful release.

An agent may draft novel criteria, but they become authorized only through a plan-review receipt that binds:

- The exact criteria digest.
- The threat model digest.
- Reviewer identities/providers.
- A passing two-reviewer quorum.
- A timestamp before worker launch.
- The execution agent identity, which must not be one of the authorizing sessions.

The present review gate does not yet emit all of that as a durable, content-bound authorization receipt. Until it does, agent-authored novel criteria should be labeled `UNAUTHORIZED_CRITERIA` and must not produce `ARTIFACT READY`.

That is stricter but honest. Do not ship an `author: human` field as a substitute.

## Revisiting the five findings

### 1. The seal is not sealed

Confirmed against v1. The work-order proposal offered immutability by declaration.

The revised design avoids the problem in two ways:

- Direct `--verifier` is process configuration for one running await, not a mutable file later reread.
- Agent-safe automated use loads the verifier policy from the immutable base commit, not the worktree.

No signatures are needed under the stated threat model.

### 2. The old path bypasses admission

Confirmed and first priority.

No-contract `idle` must cease returning an ordinary success unless the caller explicitly requests lifecycle-only waiting.

The clean interface is:

```text
bus await ... <artifact predicate>
bus await ... --lifecycle-only
```

Without either, refuse before waiting.

`--lifecycle-only` should print something like:

```text
LIFECYCLE SETTLED — no artifact judgment requested
```

not “finished.”

This retains valid analysis/advisor use cases without allowing accidental admission.

### 3. No independent contract authorizer

Confirmed against v1.

Comparing claimed identities does not solve it. Base-commit policy provenance does. Independently reviewed agent-authored policies can follow later.

### 4. ADMITTED does not freeze artifacts

Confirmed—and the right response is to remove universal `ADMITTED`, not build a generic freezer.

`ARTIFACT READY` means “the predicates held when `bus await` ran.” The verifier receipt must say when and what digests it judged.

Downstream consumers have three honest options:

- Rerun the verifier immediately before consumption.
- Consume content-addressed or otherwise immutable outputs.
- Use a domain-specific promotion step that copies or renames verified outputs into an immutable release location.

A generic bus cannot freeze arbitrary Slurm outputs, checkpoints, directories, databases, or object-store paths. Leave that out.

Also, once an await returns ready, automated retries must stop. A later retry creates new evidence and requires a new await evaluation.

### 5. Human review is a dead end

Confirmed against v1, but mostly created by my unnecessary global state machine.

Human review belongs inside the applicable domain contract:

- `result.py review` already writes a review bound to exact output digests.
- Rerunning `result.py check` can then reach its success state.
- `bus await --verifier ...` reruns that verifier and observes exit 0.

For domains without a review transition, the domain verifier must define one or explicitly stop at mechanically validated. The bus should not invent a generic human-review state it cannot complete.

## What should remain separate

- `--require-json` may remain as a supporting coordination assertion, but it must not be sufficient by itself for `ARTIFACT READY`.
- Model reviewers authorize claims only through a content-bound review receipt; their prose is not a verifier result.
- The bus runs verifier commands but does not learn workflow, training, figure, table, or genomics semantics.
- Domain verifiers remain stdlib-only and Paseo-independent.
- Artifact immutability remains domain/storage-specific.
- Agent-authored novel contract authorization should remain unsupported until the review gate can produce a properly bound plan receipt.
- Per-machine deployments remain independent. Portable handoff may report other machines’ work, but no machine is the global completion authority.

## PLAN

1. **Close the no-contract exit-zero path in `bus await`.**

   Change:

   - Require at least one real artifact predicate, or an explicit `--lifecycle-only`.
   - Rename the lifecycle-only verdict so it cannot be read as completion.
   - Do not count `--status-file`/`--require-json` alone as independently verified evidence.

   Acceptance criteria:

   - `bus await AGENT` with no artifact predicate and no `--lifecycle-only` exits nonzero before polling and names both remedies.
   - `bus await AGENT --lifecycle-only` may exit 0 on idle but prints `LIFECYCLE SETTLED` and explicitly says no artifact judgment occurred.
   - Idle with an unmet declared predicate still exits with `IDLE WITHOUT ARTIFACT`.
   - A non-empty agent-authored status field alone cannot produce `ARTIFACT READY`.
   - Existing permission, error, timeout, continuation, and launch-mismatch behavior remains unchanged.
   - Tests are added alongside `TestAwaitVerdicts`, including a shell-composition assertion that bare idle cannot unlock a following command.

2. **Extend `bus await` with an independently executed verifier predicate and harden its existing Git measurements.**

   Change:

   - Add a verifier command that `bus` executes locally after the agent is idle and launch constraints pass.
   - Make verifier success conjunctive with `--base`, `--require-clean`, and other measured predicates.
   - Give Git queries a tri-state result so “Git failed” cannot mean “clean.”
   - Require HEAD to descend from the declared base, not merely differ from it.

   Acceptance criteria:

   - Verifier exit 0 plus all other predicates passing yields `ARTIFACT READY`.
   - Verifier nonzero, timeout, missing executable, signal termination, or unreadable required receipt never yields exit 0.
   - Verifier stdout/stderr are bounded and included as evidence without becoming the verdict source.
   - The verifier runs in the inspected agent cwd unless an explicit, validated cwd is declared.
   - The command is executed without an implicit shell, or the interface explicitly documents and tests its trusted-command threat boundary.
   - `--require-clean` fails closed when Git errors or times out.
   - An unrelated Git history with `HEAD != base` fails the ancestry check.
   - A worker-authored `status.json` claiming success cannot override a failing verifier.
   - Tests use fake verifier executables and cover success, failure, timeout, malformed receipt, conjunction with Git predicates, and retry after initially incomplete evidence.

3. **Authorize verifier selection through a base-commit policy; defer agent-authored policy authorization.**

   Change:

   - Add a small versioned acceptance-policy format loaded from the declared base commit.
   - The policy fixes verifier argv, contract type, locked criteria, allowed run parameters, expected success state, and required receipt bindings.
   - Automated agent workflows must use this policy path. Freely supplied verifier commands are caller-authorized/manual mode and must be reported as such.

   Acceptance criteria:

   - The policy is read from `git show <base>:<path>`, never from the worker’s worktree.
   - The observed base commit and policy blob identity are recorded in the launch/await report.
   - Editing the worktree policy to replace the verifier with a lenient command has no effect on the invoked verifier.
   - A policy added only after the base commit is refused.
   - A run contract that weakens a locked criterion is refused before verifier execution.
   - Only explicitly declared parameter fields may vary per run; an unread or unknown field is refused.
   - The verifier receipt must bind the domain contract ID, criteria digest, and current artifact identities required by its policy.
   - No `author: human` field is trusted as evidence.
   - Agent-authored novel policies without a content-bound independent plan-review receipt return `UNAUTHORIZED_CRITERIA`; building that receipt path is explicitly deferred.
