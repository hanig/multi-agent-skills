# Delegation, evidence, and checked review facts

The initial unit owns its issue through the pull request rather than handing review triage back to the orchestrator. <!-- declaration: delegation.whole-loop -->

## Prompt construction

A complete delegated prompt contains:

- Objective and acceptance criteria. <!-- declaration: delegation.prompt -->
- Required source files and the facts already checked from them. <!-- declaration: delegation.prompt -->
- Repository constraints, scope, and deliberate exclusions. <!-- declaration: delegation.prompt -->
- Declared outputs and their location outside the repository when applicable. <!-- declaration: delegation.prompt -->
- Exact test command, mutation, review command, and delivered claims. <!-- declaration: delegation.prompt -->
- Push, pull-request, recovery, and coordinator completion protocol. <!-- declaration: delegation.prompt -->

Runner configuration remains structured plan data so the coordinator can validate and pass it as configuration. <!-- declaration: delegation.configuration -->

A continuation names its recovery ref and completed evidence, while a retry receives a fresh attempt and repeats the unit. <!-- declaration: delegation.continuation, retry.boundary -->

## Checked review configuration

These observations were checked against `skills/hanig-review-gate/reviewers.json` at the task base rather than copied from an older roster. <!-- declaration: evidence.checkable, review.panel-source -->

`plan`: luna, kimi-k2.7-code.

`fast`: luna, kimi-k2.7-code.

`standard`: luna, kimi-k2.7-code, glm-5.3.

`deep`: luna, kimi-k2.7-code, astra, glm-5.3.

`committee`: deepseek-v4-pro, luna, kimi-k2.7-code.

Enabled price hints recorded per million tokens are luna input $0.2 and output $1.2; kimi-k2.7-code input $0.67 and output $3.4; glm-5.3 input $1.4 and output $4.4; and deepseek-v4-pro input $0.87 and output $1.74. Astra has no `_cost` record, so the checked configuration does not support a fixed deep-round total. <!-- declaration: review.cost -->

Panel reporting names actual answers and absences instead of describing configured membership as completed review. <!-- declaration: review.honesty -->

## Checked effort measurement

The recorded 2026-09-19 note used three samples per cell on a real 120-line diff at a 16000 cap. GLM-5.3 returned zero characters after using the full reasoning budget in 3 of 3 high samples and 2 of 3 low samples, while null returned a 4581-character review costing $0.016 in 3 of 3. Kimi-K2.7-Code used about 12k completion tokens at every effort; high cost 83 percent more and returned slightly less answer. This supports null for those two reviewers, not a general claim that null is best for every model. <!-- declaration: review.effort -->
