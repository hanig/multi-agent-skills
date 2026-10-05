# Plan: PR 2, drain the swarm outbox into Linear

Slice 3 of `docs/plan-linear-dag-control.md` (owner-approved 2026-10-05).

**Owner decisions, 2026-10-05:** this plan is approved as revised after its
exhausted plan review and the Sol consult; the boundary that two hosts
draining one project is unsupported but self-repairing is accepted; and the
orchestrator is authorized to create one throwaway issue in the skills
project for criterion 11's live check.
PR 1 (pull 185) shipped the binding, the read-only audit and audit-backed
reporting. This PR adds the first Linear mutation path: applying the
coordinator's outbox intents to their issues and recording a receipt only
after reading the effect back. It replaces the hand step in which a session
applied an intent through the Linear MCP connector and ran
`swarm.py outbox --record-receipt KEY --ref ID`. That command stays for
compatibility.

## Measured starting point (2026-10-05, read-only)

This repository's orchestrator plans are nameless (`plan.name` absent), so
every intent carries project `swarm`, and every unit names its issue in the
plan's `tracker` field (for example `ARC-698`). There are 20 wave state
directories under the orchestrator's state root; the three sampled hold 2 to
5 intents each, all already receipted with the legacy `attested` grade. No
`tickets.json` exists for them. The drain must therefore work from the
repository binding plus each intent's `tracker`, for nameless plans, across
many state directories that can touch the same issue.

## Command

```
linear_sync.py drain (--binding FILE | --draft tickets.json)
                     --state-dir DIR [--state-dir DIR ...] [--dry-run]
```

Exit 0 when nothing is left unacknowledged except superseded intents, 3
when any other intent is left unacknowledged (each with a reason), 2 for a usage or configuration error,
including a key problem. `--dry-run` reads and prints what it would do and
sends no mutation.

## Which issue an intent goes to

- **Binding mode.** The issue is the intent's `tracker` identifier. An
  intent without one is left unacknowledged ("no tracker issue"). Nameless
  plans are accepted: identity comes from the binding, not the plan name.
- **Draft mode.** The issue is the draft's identifier for the unit. The
  intent's envelope project must equal the draft's `project.slug` exactly;
  a nameless plan (`swarm`) is refused in draft mode, because the draft
  cannot prove which plan it belongs to (this replaces the alias that joined
  `swarm` to `unnamed-swarm-project`, finding 7 on `hanig/linear-api`). When
  the intent also names a `tracker`, it must equal the draft's identifier.

In both modes the issue is read before any mutation and must belong to the
bound or drafted project and team; otherwise the intent is left
unacknowledged ("issue outside the bound project") and nothing is sent.

## What an intent does

| operation | state change | comment |
|---|---|---|
| `start` | to the team's first `started` state, unless already in a `started` state | yes |
| `close` | to the first `completed` state, unless already `completed` | yes |
| `reopen` | to the first `unstarted` state, unless already `unstarted` | yes |
| `note`, `block`, `open_pr` | none | yes |

Every intent posts exactly one comment whose id is derived (Linear's
`CommentCreateInput` accepts a client `id`, confirmed by schema
introspection on 2026-10-04; that Linear honors it is checked live by
criterion 11) from a namespaced name, `comment:<workspace id>/<project id>/<issue id>/<intent key>` (SHA-256, UUID v4
version and variant bits). The comment body carries the reason, unit state,
attempt and evidence digest, and three marker lines written as code spans:
`swarm-intent: <key>`, `swarm-evidence: <evidence digest>` and
`swarm-order: <intent at as written> <key> <operation>`. A rejected create is
accepted only when the comment read back by that id is on the same issue and
carries all three markers with this intent's values; anything else stops that
intent. (An outbox record is append-only and its key and evidence digest are
bound in its envelope, so a matching comment is this intent's comment.)

Before mutating, the drain runs the shared contract check
(`drain_contract.validate_intent`). A `close` needs evidence with a receipt;
when `closing_evidence` is `merged_pr`, the receipt must have the merge shape
swarm uses (`swarm._merge_shape_problem`) and name the same unit.

## Ordering, reconciliation and what is guaranteed

Redesigned after an exhausted plan-review cycle and a design consult with
gpt-6.1-sol (2026-10-05). Linear has no lock and its comment listing may lag,
so no protocol here can be race-free across hosts. The guarantee is
**eventual reconciliation**: after writes stop and comments become visible,
a successful drain covering an issue restores it to the target of its
latest-ordered state intent. It is not a promise that every transient error
is detected.

**Lock.** One drainer per Linear project per host: an exclusive `flock` on
`~/.local/state/hanig-swarm/linear-drain-<workspace id>-<project id>.lock`,
held from reading inputs through mutations, reconciliation, read-back and
receipt recording. Bindings that name the same project share it. Two hosts
draining one project is an owner-accepted operational boundary, not
prevented; the audit detects resulting drift once it is visible and the next
drain repairs it, but continuous competing writes can keep undoing repair.

**Order.** `(instant, key)`: the intent's `at`, which swarm writes with a
local UTC offset (`%Y-%m-%dT%H:%M:%S%z`), parsed into an instant (never
compared as text), then the key as a deterministic tie-break. An `at` that
does not parse leaves the intent unacknowledged and posts nothing.

**Run.**
1. Collect every intent from every given state directory, receipted or not.
2. In order, post each unreceipted intent's comment (or confirm it by
   derived id). Comments carry no state change.
3. Reconcile each issue any collected intent targets, receipted or not:
   page all its comments (a repeated cursor, page error or unfinished page
   makes the issue UNKNOWN and changes nothing further on it), keep only
   genuine intent comments (id equals the id derived from this issue and the
   key in the comment's own `swarm-intent` marker, with agreeing markers; a
   copied comment has a random id and is ignored), and add every collected
   intent whose comment was confirmed by derived id in this run, so a
   lagging listing cannot hide what the run already knows exists. Take the
   latest-ordered state intent L and set the issue's state to L's target
   type if it differs, once; read the state back.
4. Receipt (below), then report.

**Receipts.** A `note`, `block` or `open_pr` intent is receipted when its
comment is read back by id with its markers on the right issue. A
state-changing intent is receipted when it is L for its issue in this run and
the read-back shows L's target type. An earlier state intent is left
unacknowledged as "superseded by <key>" and does not count toward exit 3. A
receipt is a historical observation: the comment and target state were read
back at a recorded time. A later intent or repair changing the state does not
falsify it, and reporting keeps acknowledgment separate from current audited
state. Receipts go through `drain_contract.py reconcile --state-dir` in a
subprocess without `LINEAR_API_KEY`, with a `confirmed_by_readback`
observation whose reference is the issue identifier. Already-receipted
intents get no new comment and no new receipt; their issues are still
reconciled. One intent's failure, of any kind, is reported against that
intent and does not stop the rest.

**Failure reporting.** A reconciliation that fails or ends UNKNOWN makes the
run exit 3 even when every intent already has a receipt. A receipted intent
whose derived comment is missing (edited away, deleted, or never posted
because it predates this program, which is true of every legacy `attested`
receipt) is reported per issue as uncovered history; reconciliation then
works from the comments that exist and says so. Comment editing and
deletion are a declared limit.

**Audit.** PR 1's audit gains one check, `intent_order`: an issue whose state
differs from the target of its latest genuine state-intent comment is DRIFT.
It shares the listing's visibility limit, so it detects drift once the
comments are visible, not before.

## Consumers

- `merge_unit.py`'s pending-close message names the drain command instead of
  `--record-receipt`, and after advancing it runs the drain for that state
  directory when a binding and key are available, printing its summary. As
  with the audit, the drain never changes merge_unit's exit code or blocks a
  merge, and a missing binding, key or sibling prints `Tracker drain:
  UNAVAILABLE (<reason>)`.
- `hanig-project` step 6, the `outbox.receipt` and `tracker.drain`
  declarations, `docs/tracker-outbox.md` and the operating loop say to drain
  with `linear_sync.py drain`, then run `audit`.

## Key

As in PR 1: from `LINEAR_API_KEY` or the mode-600 env file; never in output,
argv, the observation or receipt files; the only child that receives it is
`linear_sync.py` itself; `swarm.py` and `drain_contract.py` subprocesses run
without it.

## Acceptance criteria

0. Comment reads page to completion and an incomplete read changes nothing
   further; a copied comment with valid-looking markers but a non-derived id
   is ignored by the ordering scan; equal intent keys on different issues
   derive different comment ids.
1. Each of the six operations maps as in the table, through a fake
   `transport`, and a re-run posts no second comment and makes no second
   state change. A duplicate comment whose evidence or order marker differs
   stops the intent.
2. Binding mode works for a nameless plan with `tracker` on each intent;
   draft mode refuses a nameless plan and an intent whose envelope project
   differs from the draft slug; a `tracker` that disagrees with the draft is
   refused.
3. An issue outside the bound or drafted project is refused before any
   mutation.
4. A receipt is recorded only after read-back shows both effects; a lagging
   comment or state read leaves the intent unacknowledged, and the next run
   acknowledges it without a duplicate.
5. Ordering and reconciliation: within one run across several state
   directories the issue ends at the latest-ordered target with one state
   write; only that intent is receipted and earlier ones are superseded;
   equal instants order by key; an unparseable `at` posts nothing; a listing
   that omits a comment this run confirmed by id does not change the winner;
   a receipted older intent's issue whose state was moved by a lagging
   second drainer is restored by the next run's reconciliation; the
   `intent_order` audit check reports that drift; a failed reconciliation
   exits 3 with every intent receipted; a missing comment for a receipted
   intent is reported as uncovered history.
6. A close without a receipt, and a code close without a merge-shaped
   receipt for that unit, are refused before any mutation.
7. A second drainer for the same project on the same host exits without
   applying anything; one failing or malformed intent does not stop the rest.
8. Already-receipted intents get no new comment or receipt, and their
   issues are still reconciled.
9. `merge_unit.py` keeps its exit code and prints UNAVAILABLE when it cannot
   drain.
10. The key never reaches output, argv, written files or a child other than
    `linear_sync.py`; `swarm.py` and `drain_contract.py` import no network
    module; every network call goes through `linear_api.transport`.
11. Live check before merge, on a disposable Linear issue the owner names or
    approves: one `note` intent drained, read back and receipted, then a
    second run that changes nothing. Recorded in the PR without identifiers
    beyond issue keys.

## Not in this PR

Filing projects or issues, the `issue` command, edge changes, waves, the MCP
deny, and converting existing `attested` receipts.
