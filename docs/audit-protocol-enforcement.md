## Verdict

No. The literal `--plan --escalate` combination is correctly refused, and explicit `--round 4` is correctly refused. But the protocol is still largely caller-attested:

- A plan review can use one, three, or non-contrasting reviewers through `--only` or valid config.
- The same proposal can be reviewed by the deep implementation panel simply by omitting `--plan`.
- The round limit can be bypassed by omitting `--round` or repeatedly claiming `--round 1`.
- Several mechanically enforceable requirements remain prose-only.
- The new tests mostly inspect constants, config, and source strings rather than observable behavior.
- One new test is not Python 3.7-compatible.

## Findings

### 1. `--plan` does not enforce the effective panel

**Scenario: widen through `--only`:**

```console
review.py --plan \
  --only NAME_A,NAME_B,NAME_C \
  --quorum 2 \
  --file proposal.md
```

where the names are any three valid enabled names shown by `--list`.

`args.plan` sets `args.profile = "plan"`, but `--only` takes precedence during reviewer selection. There is no post-selection check that the resulting panel has exactly two members. Three reviewers run and the command can reach a verdict.

**Scenario: narrow to one reviewer:**

```console
review.py --plan \
  --only NAME_A \
  --quorum 1 \
  --file proposal.md
```

One reviewer can produce a verdict. The protocol depends on two independent judgments, not merely two configured candidates.

The same happens without `--only` if one plan provider is unavailable and the caller passes `--quorum 1`: the remaining reviewer can decide the review.

**Scenario: defeat contrast:**

```console
review.py --plan \
  --only TWO_NAMES_USING_THE_SAME_PROVIDER_OR_FAMILY \
  --quorum 2 \
  --file proposal.md
```

Nothing validates provider or family after selection.

The correct enforcement point is after all profile, `--only`, enabled-state, and config processing:

- effective panel size must equal two;
- effective quorum must equal two;
- providers must differ;
- explicitly represented families must differ.

`--only` should remain available for author exclusion, but the resulting panel must still satisfy these invariants.

### 2. Valid config can widen the plan panel, while the new test still passes

Production selection explicitly treats a reviewer with no declared profiles as belonging to every profile:

```python
# A reviewer with no declared profiles is in every profile.
```

The test does not model that rule:

```python
panel = [r for r in revs if "plan" in (r.get("profiles") or []) ...]
```

**Scenario:**

Change the Kimi reviewer to valid config such as:

```json
"profiles": []
```

Leave the existing two explicit `"plan"` memberships unchanged, then run:

```console
review.py --plan --quorum 2 --file proposal.md
```

The runtime panel contains the two explicit plan reviewers plus Kimi. The test still counts only the two explicitly tagged reviewers and passes.

Runtime also performs no plan-specific config validation. Tests are not enforcement for installed skills, especially when `reviewers.json` is intended to be editable routing config.

### 3. “Different provider and family” is not enforced or fully represented

The test checks only:

```python
len({r["provider"] for r in panel}) == 2
```

There is no `family` field or equivalent validation.

**Scenario:**

Configure two plan entries routed through different supported providers, but point both at models from the same family—for example, a direct route and an aggregator route to the same underlying model family. The provider-set test passes and the review reaches a verdict, despite violating the documented family requirement.

The current checked-in DeepSeek/GPT pair is in fact from different families. The defect is that the claimed invariant is not enforced under valid future configuration or `--only`.

### 4. Plan mode is optional, so the original failure remains reproducible

All new restrictions are conditional on remembering `--plan`. The tool still defaults omission to an implementation review.

**Scenario:**

`proposal.md` contains only a design and acceptance criteria, with no implementation. Run:

```console
review.py --profile deep --file proposal.md
```

with the ordinary required claim inputs, if any.

The full deep implementation panel reviews the proposal and can reach a verdict. No code checks that the input is a plan or requires the caller to explicitly choose a review kind. This reproduces the original widened plan review by omitting one optional flag.

A required mutually exclusive mode such as:

```text
--kind plan
--kind implementation
```

would at least prevent accidental omission. Stronger enforcement would bind a plan-review receipt to the later implementation review.

### 5. `--profile plan` is either an unguarded alias or incorrect documentation

SKILL.md advertises:

```text
--profile plan|fast|standard|deep
```

but all plan-policy checks are inside:

```python
if args.plan:
```

**Scenario:**

```console
review.py --profile plan --escalate --file proposal.md
```

There are only two possible outcomes:

1. If the existing parser accepts the documented `plan` profile, `args.plan` remains false and the “never escalated” check is bypassed.
2. If the unchanged parser restricts `--profile` to `LADDER`, argparse rejects `--profile plan`, contradicting SKILL.md.

There should be one canonical plan entry point. Either remove `--profile plan` from the docs and parser surface, or normalize it to `args.plan = True` before any validation.

### 6. `--round` does not enforce a per-change bound

The code validates only a supplied integer. It does not require the flag and records no change identity or prior rounds.

**Scenario: omit the flag:**

Run five reviews of the same evolving change:

```console
review.py --diff --profile standard
```

No invocation supplies `--round`; all five can reach verdicts.

**Scenario: reset the declaration:**

Run every review with:

```console
review.py --diff --profile standard --round 1
```

The same change can be reviewed indefinitely. Nothing distinguishes an honest new change from a reset of the same one.

Thus `--round 4` is refused, but a fourth round is not. This is argument-range validation, not round enforcement.

A real bound needs a stable change/plan identifier and a skill-local ledger or review receipt. A diff hash alone is insufficient because every fix changes the hash.

### 7. The hard round count does not model the first N+1-defect rule

The protocol’s stated signal is finding a defect in the previous round’s fix, not reaching a particular ordinal.

**Scenario: false continuation**

- Round 1 finds defect A.
- The round-1 fix introduces defect B.
- Round 2 finds B.

The protocol says step back at this first occurrence. The tool nevertheless accepts a patch followed by:

```console
review.py --round 3 ...
```

because it knows only the number.

**Scenario: false refusal**

- Rounds 1–3 find three unrelated defects in unchanged parts of the original implementation.
- None is a defect in the previous round’s fix.
- A fourth bounded verification is warranted.

```console
review.py --round 4 ...
```

is refused even though the protocol’s root-cause signal never occurred.

A three-round cap can be retained as a separate budget policy, but it is not enforcement of the N+1 rule. The N+1 classification is legitimately judgment-heavy; pretending the ordinal implements it is the wrong model.

### 8. Mechanically enforceable requirements remain prose-only

#### Counter-claim

**Scenario:**

A stricter key check is reviewed with claims only about blocking malformed or bypassing keys. No claim covers harmless annotated criteria. The command reaches `REVIEW_PASS`, but an honest criterion carrying a harmless annotation is then refused.

The presence of a non-regression/counter-pressure claim can be structurally enforced through a dedicated claim category. Its truth remains reviewer judgment.

#### Threat model

SKILL.md already says omission is permitted and causes every finding to gate. PROTOCOL.md says “Always pass a threat model.”

**Scenario:**

Run an implementation review without `--threat-model`. Reviewers produce findings that require a hand-edited `contract.json`, an input the tool does not defend against. Those findings gate and produce `REVIEW_FAIL` for honest work.

Requiring the flag is straightforward. This matters in the false-refusal direction, which the stated constraint says is equally serious.

#### Author exclusion

**Scenario:**

DeepSeek authored `proposal.md`. DeepSeek is also one of the default two plan reviewers. Running:

```console
review.py --plan --file proposal.md
```

counts the author’s own judgment and can reach a verdict.

The tool cannot infer authorship, but it can require `--plan-author human|REVIEWER_NAME` and validate the effective panel against it. Leaving even the declaration to memory repeats the failure mode being addressed.

#### Criteria declared before implementation

**Scenario:**

The implementation already exists. Criteria are written afterward to fit it, then reviewed with:

```console
review.py --plan --file criteria.md
```

The later implementation review has no evidence that the criteria predated the code.

Semantic quality is judgment, but ordering is state. A plan receipt containing the criteria hash and baseline commit/tree can be required by the implementation phase.

### 9. The new tests do not constrain the claimed behavior

The four tests have different weaknesses:

1. **Plan-panel test:** constrains checked-in explicit `"plan"` tags, but not the runtime selector. The profileless-reviewer scenario above passes the test while running three reviewers.
2. **Round test:** checks only `MAX_ROUNDS == 3`. Removing the runtime refusal while leaving the constant makes the test pass.
3. **Plan/escalate test:** checks for an error-message substring in source. Leaving that string in dead code while removing the guard makes the test pass and allows `--plan --escalate`.
4. **Actionable-message test:** is a source-shape heuristic, not behavior. For example:

   ```python
   config_error("Invalid timeout value. Set --timeout to at least one second.")
   ```

   is actionable but fails because `"Set "` is not in the allowlist. Conversely, a non-actionable explanation containing `"must be"` passes.

None invokes `main()`, verifies the exit state, or asserts that the network/reviewer runner was not reached.

### 10. The new AST test is incompatible with the Python 3.7 minimum

The test extracts strings only through `ast.Constant`. Python 3.7 parses string literals as `ast.Str`; depending on the exact 3.7 release, `ast.Constant` is either unavailable or not produced for these nodes.

**Scenario:**

```console
python3.7 -m unittest \
  tests.test_review.TestProtocolIsEnforcedNotRemembered.test_every_protocol_refusal_names_an_action
```

The test errors on `ast.Constant` or recovers zero strings and fails `checked > 3`.

This is a concrete false downgrade of a supported environment. More importantly, this test should be replaced with behavioral CLI tests rather than made AST-version-aware.

### 11. The implementation-review documentation contradicts the code

PROTOCOL.md says implementation review uses a cheapest-first ladder and `--escalate --round N`. The code and SKILL.md still allow fixed panels.

**Scenario:**

```console
review.py --profile deep --round 1 --diff
```

directly runs the deep panel rather than starting at `fast`.

Likewise:

```console
review.py --round 1 --diff
```

defaults directly to `standard`, not the ladder.

`reviewers.json` compounds this by describing `deep` as “Reached only via `--escalate`,” while SKILL.md explicitly documents `--profile deep`.

Either fixed implementation profiles remain supported—in which case PROTOCOL.md and the config description are wrong—or the code must refuse them.

### 12. The protocol still has an asymmetric uncertainty model

SKILL.md says uncertainty must become refutation because a false pass is more costly. The supplied constraint says false pass and false refusal are equally serious.

**Scenario:**

The implementation is correct, but a reviewer cannot determine a Python 3.7 compatibility detail. Its prompt requires it to refute rather than return unresolved. The finding is in scope and gates, producing `REVIEW_FAIL` rather than `REVIEW_UNAVAILABLE` or “needs evidence.”

That is a systematic false-refusal mechanism. The counter-claim supplies useful opposing scope, but it does not fix forced verdicts under uncertainty. Uncertainty should remain a distinct state.

Similarly, different providers and families reduce correlated failures; they do not ensure the models “cannot share a failure mode.” Both can fail on the same ambiguous criterion or missing evidence.

## What is correct

- Literal `--plan --escalate` is refused before review execution.
- `--plan --profile fast|standard|deep` is refused.
- Explicit `--round 0`, negative rounds, and rounds above three are refused.
- The checked-in default plan models appear to be two distinct provider/model families.
- Excluding `plan` from `LADDER` is correct.
- No new production dependency, sibling-skill import, or shared-helper asymmetry is introduced.
- Leaving “is this finding about the previous fix?”, taste-versus-behavior, and convergence classification as reviewer/operator judgment is defensible. Those are semantic questions. The tool should capture their answers structurally, but should not guess them with string matching.

## Minimum changes before this can be called enforcement

1. Require an explicit review kind rather than making `--plan` optional.
2. Resolve the effective panel first, then validate exact size, quorum, provider, family, and declared author.
3. Require `--round` for implementation reviews and bind it to a stable change/plan receipt.
4. Require a threat model and a structured honest-run counter-claim.
5. Replace source/string tests with CLI behavior tests that assert refusal and assert the reviewer runner was not called.
6. Run those tests under Python 3.7.
7. Reconcile fixed implementation profiles, `--profile plan`, and the ladder across PROTOCOL.md, SKILL.md, config descriptions, and argparse.

The memory file was not included in the supplied content or diff, so I cannot verify that document against the code.