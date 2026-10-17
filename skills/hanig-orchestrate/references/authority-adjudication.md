# Authority and adjudication detail

The owner-originated mandate remains the authority; this skill publishes its decision surface without turning installed prose into consent. <!-- declaration: authority.source -->

## Confirmation boundary

The startup presentation reads the complete grant, bounds, and stop lists from the current mandate rather than copying them into this skill. It is once per run, and a successor reads the durable confirmation rather than accepting a predecessor's recollection. <!-- declaration: authority.confirmation -->

Before confirmation the session follows the current mandate's narrow mode, whose contents remain authoritative there rather than in this skill. <!-- declaration: authority.narrow-mode -->

## Findings

The matrix distinguishes evidence that disproves a finding, a confirmed required defect, a confirmed excluded defect, and an unresolved finding. The review gate's digest-keyed `--dispositions` entries retain `location`, `summary`, `disposition` and a one-line `reason`. A `reproduced` or `not-reproduced` entry includes a `reproduction` object with non-empty `setup`, `command`, `expected`, `observed` and `revision` strings; `revision` is the full lowercase 40- or 64-hex commit id tested. A `deferred` entry needs no reproduction. Missing or malformed reproduction evidence is a configuration error before network access. Reviewers receive the evidence with the prior finding, subject to the gate's existing removal of recognized prior-review decision text. The record supports refutation and adjudication, never automatic severity downgrading or execution of reviewer-supplied commands. Inability to reproduce is evidence for the adjudicator, not proof of harmlessness. <!-- declaration: adjudication.matrix -->

For a proposed nonblocking disposition, the mandate's concurrence party receives neutral premises and an invitation to refute instead of a request to ratify. <!-- declaration: adjudication.concurrence -->

The task declares its supported envelope and accepted limits before work begins and passes them to reviewers, who may refute those boundaries. Deferral at adjudication confirms or refutes that declared claim against the reproduced trigger, consequence bounds and recovery evidence under the mandate. <!-- declaration: adjudication.envelope -->

The decision record answers five questions:

1. Which goal, criterion, output, or downstream consumer is exposed?
2. What evidence bounds that impact?
3. Does acceptance weaken a shared test, verifier, interface, or later unit?
4. Which pre-existing policy authorizes leaving it?
5. What observable result falsifies irrelevance?

Those answers are mandatory fields of the reasoning record rather than persuasive substitutes for evidence. <!-- declaration: adjudication.record -->

Hard constraints, required evidence and outputs, authority boundaries, shared guards, and candidate checks remain outside relevance exceptions. <!-- declaration: adjudication.nonoverridable -->

The three-round ceiling bounds one unchanged design. A repair-induced defect signals step-back earlier; exhausting the ceiling sends the whole history to the committee. Implementation gates require `--kind implementation --round N` with the next admissible round, including a re-gate after a changed head. The loaded gate derives identity from origin, target merge-base and branch; use `--target REF` for a target other than `origin/main`, and stable `--plan-name NAME --unit UNIT` when declared for the unit. Keep that choice across fixes. Later rounds also need `--dispositions FILE`; a changed diff never resets the ledger. An exhausted cycle requires the step-back and explicit `--fresh-cycle-from PROFILE` with its predecessor retained. <!-- declaration: review.rounds -->
