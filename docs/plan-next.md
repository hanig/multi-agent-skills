# Plan: what remains after the report/runtime/acknowledgment work

> Committee 2026-08-30: gpt-5.6-sol (xhigh) + deepseek-v4-pro, both verdicts
> in. This synthesises them. Two REAL DIVERGENCES are marked and are the
> human's to settle, per the committee rule.
>
> **Excluded by construction, do not revisit:** persisting the review round
> counter. `proposal-protocol-hardening.md` specified it and it was REJECTED,
> because a receipt-bound counter locks honest authors out three ways and the
> tool is run by its own author, who can edit the state anyway. The bound
> stays an honest number.

## The organising defect

`bus await` can return `ARTIFACT READY` today on `--require-clean` plus
`--expect-*` alone. Every one of those is satisfied by a worker that did
nothing: a clean tree is clean if you never touched it, and the launch scope
matches whether or not work happened.

Sol named this in `plan-fusion.md` v3 as "a predicate that can be satisfied
without doing the work", and it is the same shape as every defect the last
review cycle found on our side: a canary whose command was `true`, a stored
DONE with no receipt reporting COMPLETE, an attestation presented as
verification. Six instances, one cause.

## Order

| # | Item | Disposition | Both agree? |
|---|---|---|---|
| 1 | Reviewer profile eligibility (`escalate()` bug) | DO NOW, first | yes |
| 2 | Type every `bus await` predicate | DO NOW | yes |
| 3 | Launch-anchored Git tree transition | DO NOW, ship with 2 | yes |
| 4 | Authorization + verifier pinning + receipt admission | DO NOW, atomically | yes |
| 5 | Code-unit delivery (branch/PR/merge) | **DIVERGENCE** | no |
| 6 | Bounded continuation for code attempts | after 5 | yes |
| 7 | Budget over declared metrics | DEFER, narrowed | yes |
| 8 | `retry.mode: resume` | stays refused; add negative tests | yes |

### 1. Reviewer profile eligibility. DO NOW.

Sol's own fix for "a reviewer with no `profiles` key lands in every profile"
was applied to the normal selection path in `review.py` and NOT to
`escalate()`, which still reads `not r.get("profiles") or tier in
r["profiles"]`. Latent only because all six reviewers currently declare
profiles. Every review this session ran through `--escalate`.

Done when: both paths call ONE eligibility authority; a missing or empty
`profiles` key excludes from every named profile; an escalation test includes
a reviewer with no `profiles` and proves it is not selected; and the absence
of an eligible escalation reviewer fails explicitly rather than falling back
to the whole roster.

### 2. Type every `bus await` predicate. DO NOW.

Categories: scope, hygiene, declaration/shape, lifecycle, operational control,
production, verification.

Done when: `--expect-*`, `--require-clean`, `--status-file` and
`--require-json` cannot yield `ARTIFACT READY` alone or in any combination; a
clean correctly-launched worker that does nothing never returns it; bare
`await` is rejected without `--lifecycle-only`; lifecycle-only has its own
verdict that never contains the words ARTIFACT READY; lifecycle-only combined
with a production request is rejected as ambiguous; and every existing caller
is inventoried and classified rather than silently keeping old behaviour.

**Ship with item 3**, or production callers are left with no valid predicate.

### 3. Launch-anchored Git tree transition. DO NOW.

The first real production predicate. The base comes from the worker's own
verified launch record, never from the caller: an arbitrary `--base` is not
production evidence because HEAD may have advanced earlier.

Done when: the launcher captures repo identity, branch, base commit/tree,
clean state, attempt and runtime identity BEFORE execution, in a record the
worker's worktree cannot mutate; the final commit descends from that base,
both ends are clean, and the final tree differs; empty commits,
change-and-revert, dirty output, unrelated-history resets and a caller-crafted
`--base` all fail; the receipt is content-digested and bound to the attempt;
and its `basis` explicitly denies quality, relevance, test, authorship, PR and
merge claims.

### 4. Authorization, verifier pinning, receipt admission. DO NOW, ATOMICALLY.

There must never be a release where `bus await --verifier /bin/true` reaches
`ARTIFACT READY`.

Done when: trusted policy names each authoritative verifier and what it may
claim; candidate branch changes cannot authorize their own verifier;
verification receipts bind the production receipt, final tree, policy digest,
verifier content and runtime; unapproved `--verify`/`--verify-check` stay
diagnostic; we reject malformed, stale, forged, unauthorized,
digest-mismatched, subject-mismatched and basis-free receipts; the report sees
only admitted receipts, so `ARTIFACT READY` on stdout without one yields no
verdict; and a protocol version gate makes mixed-version rollout fail closed.

### 5. Code-unit delivery. DIVERGENCE.

Sol: DO NOW, as the thing that makes items 2-4 useful, with a merged-PR
receipt pinning the PR head and descendants consuming the merge revision.

deepseek: DEFER to its own plan document; it is a design problem, not an
implementation one.

Both agree on the content when it happens: distinct repo and artifact
locations per attempt; a worker cannot obtain authority by writing a
receipt-shaped file; push/PR acknowledgements are attestations only; an open
PR does not complete a unit; a changed PR head invalidates prior verification;
unsupported merge methods fail closed.

### 6. Bounded continuation. AFTER 5.

Both agree this must NOT become a Repo A unit state or retry mode, and that no
Slurm analogue should exist. A conversational turn is not a retry boundary: a
planning-only turn can settle while the session, worktree, launch identity,
attempt root and budget all remain valid, and starting a fresh attempt would
discard context and spend a retry on a provider-level liveness defect.

So it is intra-attempt behaviour inside a code unit: same attempt id, worktree
and artifact root; triggered only when lifecycle settled and production is
unmet; never while permission is pending, after invalid launch, cancellation,
deadline or budget exhaustion; never as a correction loop after verifier
rejection; every continuation recorded; and after the bound, the unit FAILS
for missing production evidence.

### 7. Budgets. DEFER, NARROWED.

Sol: drop arbitrary metric accumulation; allow only typed, authority-backed
metrics. An unrestricted "any declared metric" budget lets a unit declare a
metric nothing can measure.

### 8. `retry.mode: resume`. STAYS REFUSED.

Both agree the refusal was correct. Add negative tests so the refusal is
pinned rather than incidental. Revisit only if preemption rates justify
checkpoint-and-re-attest.

## Adopting from the other repo: DIVERGENCE

deepseek: **nothing** beyond items 1-8. Most of that surface is already
covered by A's model, out of scope, or superseded once receipts gate
admission.

Sol: four specific things.

| Capability | Why | Cost |
|---|---|---|
| `bus check` (and `bus models` if machine-stable) as declared runtime probes | Supplies concrete probes for the code-agent runtime instead of inventing a second availability mechanism. Establishes availability and identity only, never production. | Low. If output is prose-only, take `check` and defer `models`. |
| `AWAITING PERMISSION` and `INVALID LAUNCH CONTRACT` as distinct outcomes, plus re-armable observer timeout | Prevents futile continuation, and stops a polling timeout being read as worker failure. Fits inside the existing output-or-fail model without a new terminal state. | Low to medium. |
| `bus list/check` and `paseo loop inspect/logs` linked from attempt ids | Live operational views for a system whose disk report is authoritative only at the end. Diagnostics, never evidence. | Low, if only ids and commands are surfaced. |
| `paseo loop stop` | Operator cancellation of code workers. Must produce cancellation or failure, never readiness. | Medium; defer until cancellation races are specified. |

## Rejected by sol, worth recording

- Repo B heartbeats and schedules as authority: a second scheduler creates
  split-brain. Diagnostic only, never closure.
- `pi-fleet` as a second fleet coordinator: overlaps `hanig-swarm` with no gap
  addressed.
- `paseo loop --verify-check`/`--verify` as authority: arbitrary shell strings
  and prompts are not policy authorization or content pinning.
- Status-file existence or valid JSON as production: worker-controlled
  declaration.
- `ack` as verification: transport acknowledgement only.
- `paseo-committee`, advisor, or another loop around the review gate:
  duplicate control machinery.
- Direct Linear or Slack integration in the coordinator: violates the
  network-free boundary. Use outbox intents or content-digested snapshots.

## The two questions for Hani

1. **Item 5 timing.** Build code-unit delivery now as part of this arc (sol),
   or write it as its own plan document first (deepseek)?
2. **Repo B adoption.** Take sol's four (probes, richer outcomes, diagnostic
   links, deferred stop), or deepseek's nothing?
