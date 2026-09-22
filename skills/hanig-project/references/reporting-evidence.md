# Reporting evidence details

The generated declarations in `../SKILL.md` are the decision surface. This file
explains the evidence labels and findings shape. <!-- declaration: placement.reference-elaboration -->

## Completion report

The final unit reaching DONE is not run completion. The report must be produced, <!-- declaration: report.required -->
its fragment published, and the owner given the artifact link. <!-- declaration: report.required -->

The report is assembled from `plan.json`, coordinator state, and attempt
receipts. A narrative memory must never supply a fact absent from that evidence. <!-- declaration: report.evidence-source -->

Three sections carry the main claim:

- Evidence lists each delivered artifact with its judgment-time digest.
- Limits repeat each receipt's basis, including conventional rather than <!-- declaration: report.contents -->
  OS-enforced isolation and the absence of attribution-by-observation.
- Findings are labeled as project claims because the coordinator cannot verify <!-- declaration: report.contents -->
  their scientific meaning. <!-- declaration: report.contents -->

Dropping a receipt hedge while printing DONE silently strengthens the claim and
must not happen. <!-- declaration: report.contents -->

## Findings

Not every project has findings. Training artifacts and merged code changes can
be complete deliverables without a `findings.json`; the interview must ask <!-- declaration: findings.interview -->
rather than forcing an empty file. <!-- declaration: findings.interview -->

When findings exist, the producing unit must declare `findings.json` as an <!-- declaration: findings.contract -->
output and must promote it to the project directory where the report reads it. <!-- declaration: findings.contract -->

```json
{"findings":[{"title":"one sentence a reader can act on",
              "detail":"the numbers, and what they do not prove"}]}
```

A clean sample bounds a rate; it does not establish that no defects exist. <!-- declaration: findings.bound -->
Findings must state the measured bound, what is not proved, every deliberate <!-- declaration: findings.bound -->
omission, and why it was made so readers do not mistake silence for evidence. <!-- declaration: findings.bound -->
