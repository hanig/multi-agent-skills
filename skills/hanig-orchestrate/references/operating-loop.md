# Operating loop detail

## The coordinator pass

Source inspection of `swarm.py` shows that `cmd_advance` returns `cmd_run`, `cmd_run` calls `advance` once under the state lock, and then prints that the coordinator is exiting. `advance` rechecks live units, settles state, dispatches ready units, emits tracker intents, and returns. Repetition comes from the host scheduler. <!-- declaration: loop.advance -->

The session-side cycle adds remote observation, review, adjudication, merge, tracker connector work, and reporting around that offline pass. <!-- declaration: loop.quiescence -->

## Watching and idleness

Liveness, work evidence, permission events, and observation time remain separate fields in a report. <!-- declaration: watch.facts -->

The worker record and permission events provide evidence that directory timestamps and a running label do not. <!-- declaration: watch.source -->

A watcher is armed in the dispatching turn, tested against a condition already present, and its watch set is derived from coordinator state. Arming it later is the same defect as omitting it, because nothing else wakes the session when a worker finishes and `swarm.py status` records neither an observation time nor a unit age. <!-- declaration: watch.proof -->

## Preservation, dispatch, and merge

Recovery uses a separate ref because moving the coordinator's judged branch destroys the stable comparison route. A restorable snapshot includes base identity, content digest, full porcelain including untracked paths, and a restore check. <!-- declaration: preservation.before-cleanup -->

Dispatch preserves the destination meaning of `target_branch`, the completion protocol, complete output, current source head, and the shared-stash prohibition. <!-- declaration: dispatch.mechanics -->

Merge admission compares the full judged and pull-request heads, reads the diff, waits for terminal green checks, and records any weaker judgment basis. <!-- declaration: merge.requirements -->

## Guarded merge and reconciliation

```bash
python3 "$HANIG_ORCHESTRATE_DIR/scripts/merge_unit.py" plan.json \
  --state-dir "$STATE" --unit impl --pr 123 --approver "Operator" \
  --root "$RUNS" --dry-run
```

Remove `--dry-run` to execute after inspecting the preview. The operator must supply a nonblank approver and, when coordinator state has no recorded root, `--root`; a conflicting root is refused. The plan digest and attempt anchors are checked before forge access. Worker files provide no authority. <!-- declaration: code.merge-command -->

Before a new merge, at least one CI check must exist and every check must report `SUCCESS`; failed or unavailable reads also refuse. An exit-0 scope report must be an object with in_scope status, matching unit/attempt/head, a valid base ID, a scope list of strings, and empty outside/deletion lists; malformed success cannot be waived. Scope exits 1 and 2 require `--allow-unchecked-scope "reason"`, recorded with the approver and scope result (or verbatim malformed stdout/stderr on a nonzero exit) in a durable `merge-unit-OPERATION.json` in the state directory. An already merged matching PR is reconciled without reapplying current CI or scope policy; a different head or target refuses. New intents retain captured scope stdout/stderr in addition to the parsed report; legacy intents lacking raw fields retain their original observations without reconstructing lost text. Original precondition observations remain in an existing intent, while a newly observed historical merge does not fabricate them. An exact existing receipt is reused, and an older receipt with the wrong repository spelling is supplemented by the correct anchored URL. Dry-run prints the conditional command sequence with placeholders for the unobserved PR URL and commit IDs, performs no forge calls or state writes, and exits 2. Ordinary exit 0 means the receipt exists and advance returned success. To resolve an investigated uncertain request, pass `--abandon-intent OPERATION_ID --approver "Operator" --reason "Investigation outcome"`. The named operation must be the current unresolved intent for this attempt and PR. OPEN at the judged head records abandonment and exits 0 without merging or advancing; MERGED at that head reconciles instead, while a different head or target refuses. The atomic, fsynced `merge-abandonment-OPERATION.json` beside the retained intent records the original intent, who, why, the observed PR, and UTC observation time. The intent is marked `resolved_by_abandonment`; a crash between record publication and marking is repaired from the record on the next invocation. The next ordinary call can create one successor intent with a new operation ID and rechecks scope and CI. A second abandonment of the old ID refuses, including after a successor was created. <!-- declaration: code.merge-command -->

Forge routing supports standard HTTP(S) and SSH remotes without explicit ports and keeps the observed HTTP(S) PR URL; ports are refused rather than silently discarded. Reconciliation requires a single-parent squash result; merge observations and the squash method remain attested. The actual merge parent's SHA supplies `--target-commit`, including when the target moved after the original observation. Head matching does not atomically freeze CI reruns or concurrent PR retargeting, and a one-parent result alone does not distinguish squash from a one-commit rebase by another operator. <!-- declaration: limit.merge-command -->

The command never resubmits an unresolved merge request: a queued request, a lost response, or a crash between intent persistence and transmission leaves a durable operation for inspection. Rerun after GitHub reports MERGED to record and advance, or explicitly abandon the investigated OPEN request using the recorded path above. Neither action deletes the intent. Abandonment attests an OPEN observation; it cannot prove an earlier queued request will never execute, so the named operator retains responsibility for investigating that uncertainty. The intent and abandonment journals retain the same-node trusted-writer boundary. The local coordinator lease is released before advance acquires it; advancement can still halt or retain a unit for its existing verification policy. A successful advance does not assert that every unit closed. <!-- declaration: limit.merge-command -->

## Drain the post-merge close intent

After receipt recording and successful advancement, `merge_unit.py` displays the current attempt's pending close intent with its key and `tracker` issue, or `no tracker declared`, followed by the acknowledgment command. It performs no tracker call. If no unacknowledged close intent exists, it says so; that message establishes neither tracker delivery nor unit closure. <!-- declaration: tracker.drain -->

The authorized session applies each pending intent to the named issue using its connector. Before retrying an ambiguous operation, resolve it by receiver read-back or deduplication. Once the operation has landed, replace `KEY` with the displayed key and `ID` with the returned tracker reference: <!-- declaration: tracker.drain -->

```bash
python3 "$HANIG_SWARM_DIR/scripts/swarm.py" outbox --state-dir "$STATE"
python3 "$HANIG_SWARM_DIR/scripts/swarm.py" outbox --state-dir "$STATE" \
  --record-receipt KEY --ref ID
```

The receipt is an attestation, not independently verified tracker state. Without a connector or a known issue, keep the intent unacknowledged and report pending synchronization; never guess the issue or acknowledge an unapplied operation. The tracker label supplies no authority for admission, closure, or DONE. <!-- declaration: tracker.drain, tracker.authority -->

## Tracker and the hourly report

The tracker mirrors coordinator state, and GitHub's pull-request attachment does not perform the issue transition. <!-- declaration: tracker.authority -->

The sweep examines every in-progress issue regardless of age and repeats after each dispatch, push, merge, and close. A dispatch immediately records the tracker event that moves every covered issue to in progress with its unit; the connector applies it when available, while an outage leaves pending synchronization without blocking unrelated dispatch. A stopped attempt that did not ship similarly records its state and preservation ref. <!-- declaration: tracker.reconcile -->

Blocking relationships are recorded as tracker relations, established at filing and at dispatch, and dispatch order is read from the resulting graph. A dependency stated only in an issue's prose is not traversable, so no query surfaces what a piece of work is waiting on. <!-- declaration: tracker.dag -->

The reporting order incorporates the still-open pull request 60 source material:

1. Running work: agents, pull requests, checks, failures, skips, corrections, and observation times.
2. Tracker: every in-progress, merged-but-open, and filed-but-unmoved issue, swept by state.
3. Ready work: compare the backlog to what landed and dispatch; otherwise state the exact blocker or saturation reason.

The order and the dispatch action are part of the decision surface rather than a pointer-only recommendation. Before the report is written, step-three dispatches reach the tracker or the report names their pending synchronization when the connector is unavailable. <!-- declaration: report.three-parts -->
