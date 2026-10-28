<!-- docs-truth:status -->
Status: live
Date: 2026-10-28
Title: D5 preparatory cutover runbook
Purpose: Bound the U7 race evidence and list the human checks and owner decisions before U8.

The owner's 2026-10-28 "Drop U5, shrink U7" decision drops U5 because U6
closes row 75. This preparation grants no activation, write or merge authority.
U8 remains owner-controlled; [the mandate](orchestrator-mandate.md) is unchanged.

## Quiescence and human inventory

Quiescent means **no live session holds a lease, no merge is in flight, and
no merge child is outstanding**. Unknown liveness or an unresolved remote
outcome is not quiescence. A local process exit, timeout or successful kill
request does not establish remote completion. Grant expiry does not steal or
release the coordinator flock; custody succession requires observed holder
death, not a TTL.

The authorized human records timestamped evidence and unknowns for this
read-only checklist. No automated route scanner or state mutation is involved.

1. Inventory sessions/coordinators, native process incarnations, grant/custody
   generations and leases. Compare host observations with registry/authority
   records. Diagnostic status is neither permission nor proof of death.
2. Inventory workers, merge children and commissioned operations, including
   orphaned children. Compare retained intents, receipts and read-only forge
   observations; record unresolved recovery status. Never replay a merge to
   discover its outcome.
3. Read queued items and acknowledgments without invoking drain. Preserve each
   item and its acknowledgment state: unacknowledged does not mean unsent.
4. Check all three quiescence conditions independently; unknowns block cutover.
   Review the activation revision, installation generation, prepared state and
   U8 list with the owner.

## Queued writes and rollback

**Park existing writes.** Pre-D5 items lack D5 plan identity, scope and
provenance. U6's row-75 drain refuses. Preserve them unprovenanced and parked
until a separately authorized post-D5 unit handles them.

**P1 is a hard precondition:** before any change makes any row-75 route stop
refusing, a prior unit must deliver per-item lease authority (the original U5
M1-M5 scope) AND migration or disposition of the pre-D5 queue. Activation does
not lift U6, stamp old items, or make their provenance retroactively valid.

Preparatory rollback is to leave `merge_launch_admitted()` **False** and **not
merge U8**. Retain records and outstanding-work evidence; do not delete an
unresolved intent, invent a receipt or clear a holder as rollback. This is not
a post-activation deactivation or in-flight cancellation procedure.

## What the seven scenarios measure

[The single test module](../tests/test_d5_u7_races.py) uses an injected clock
and ordered callbacks. Host identity, registration/readiness and forge services
are fixture inputs. Real admission, authority store, START/DRAIN, submission,
merge journal and receipt readback run on temporary persisted records. The
stub executor launches no process. Every fixture teardown checks zero tracker
transport invocations and byte-identical parked outbox contents. These checks
do not claim a general tracker fence.

| Scenario | Merged mechanism and observation |
| --- | --- |
| R1 | `d5_submission.submit` and admission custody checks: A prepares, B is refused with `D5 custody is not current`, A records one merge. |
| R2 | Admission's clock and `d5_control.revoke`: expiry and revocation before START and in the split-START gap fence launch. Before preparation, fresh confirmed authority plus observed-dead custody succession lets B complete. Gap recovery limit below. |
| R3 | U4.6 no-invocation classification on pre-launch interruption, then refusal of legacy `cancel_before_request` after recorded completion; the result stands and no replay occurs. |
| R4 | U4.5 journal recovery after receipt append but before Recorded: discard provider state and simulate process exit, recover from persisted records, retain one receipt and one effect. |
| R5 | `MergeProvider.prepare` refuses a request plan different from the installed plan before any effect; matching identity remains usable. |
| R6 | The real admission consumer refuses with the default False switch; a scoped override permits launch and restoration refuses again. |
| R7 | U4.9a launch options/environment, retained unknown state, completion-only recovery, and the direct `unit` and remote-verification CLI refusals while a stub child survives. No OS survival/isolation assertion. |

U4.6's clauses are: "Pre-START/no-invocation refusal uses only the existing
authoritative no-effect release conditions" and "Cancellation after dispatch
does not manufacture a no-effect outcome." Active D5 has no legacy cancellation
marker API: it refuses. R3's before-effect cancellation is a recorded
`not_submitted` classification; its after-effect case cannot erase Recorded.

**U8 findings / limits of available seams:** R2's gap case has already published
Bound, then records `not_submitted`; U4.5 retains the unresolved request. There
is no merged automatic successor/retry path to complete that same unsubmitted
request. The test does not invent one. Keep it parked for explicit U8 recovery
policy; the before-preparation subcases cover successful successor acquisition.
R4/R7 simulate death by dropping local provider state and closing the simulation's
retained descriptors between calls. Production requires process exit after an
unreleased submit (ARC-3762-OD1); do not copy this fixture into an operator.

The U4.x test names were checked at base `7a7559bdff5ef60d00e064a51d5ba892622ff63c`
(`test_d5_u4*.py` and `test_d5_containment.py`): none names compaction. U7 drops
context-compaction testing. No scenario R1-R7 is silently omitted.

## Non-claims

- Process isolation and same-UID isolation; neither the stubs nor descriptor
  options establish an OS security boundary. Interpreter effects, including
  bytecode suppression, are outside this work.
- Repository-wide quiet, non-canonical guard coverage, OD4 closure, the
  `linear_sync` mutator direct-root refusals, and a route-inventory drift guard.
  No source/path scanner is added. The checklist is human-run and read-only.
- Worker review, scoped writes, blocked submissions, `linear_sync` behavior,
  and context-compaction behavior. Their omission is deliberate.
- gh executable identity and integrity are not bound; a same-UID actor can
  replace the executable or its path. No hashing or path guard is supplied.
  The refusal text still calls binding a U7 obligation; the owner decision
  carries it to U8 as a non-claim.
- The carried OD5 tracker lease precondition is vacuous only while U6 refuses.
  Tracker sends have no per-item lease fencing. Separately, the merge caller's
  OD5 obligation to hold the coordinator lease throughout submit still applies:
  prior-loss detection is not a fence against later concurrent release.
- Exactly-once remote execution, production death detection, deployed durability,
  or exhaustive interleavings. Counts describe these stub histories only.
  Test timeouts bound failed tests, not production liveness. Registration,
  readiness and integration verification are supplied inputs, not revalidated
  by this race suite. Persisted identities are trusted-writer evidence, not
  independent authentication.

## U8 owner review

- Resolve U4.7a, U4.9b and U4.9d explicitly as owner decisions; U7 implements
  none of their deferred work. Review OD4 closure and the carried non-claims.
- Review acceptance of the gh binding/integrity non-claim and the stale
  admission-refusal wording before admitting merge launch.
- Review the lease non-claims and keep P1 binding on every future U6 lift.
- Check that **U6's refusal is still in place at activation** on every row-75
  route; U8 must not enable tracker enqueue, send, drain or acknowledgment writes.
  Review parked items separately.
- Review R2's unresolved pre-launch gap and the simulated-death evidence limits.
  Confirm quiescence, installed/prepared-state eligibility and required candidate
  checks on the actual U8 revision. U7 changes no activation or authority policy.
