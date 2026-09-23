# Operating loop detail

## The coordinator pass

Source inspection of `swarm.py` shows that `cmd_advance` returns `cmd_run`, `cmd_run` calls `advance` once under the state lock, and then prints that the coordinator is exiting. `advance` rechecks live units, settles state, dispatches ready units, emits tracker intents, and returns. Repetition comes from the host scheduler. <!-- declaration: loop.advance -->

The session-side cycle adds remote observation, review, adjudication, merge, tracker connector work, and reporting around that offline pass. <!-- declaration: loop.quiescence -->

## Watching and idleness

Liveness, work evidence, permission events, and observation time remain separate fields in a report. <!-- declaration: watch.facts -->

The worker record and permission events provide evidence that directory timestamps and a running label do not. <!-- declaration: watch.source -->

A watcher is tested against a condition already present, and its watch set is derived from coordinator state. <!-- declaration: watch.proof -->

## Preservation, dispatch, and merge

Recovery uses a separate ref because moving the coordinator's judged branch destroys the stable comparison route. A restorable snapshot includes base identity, content digest, full porcelain including untracked paths, and a restore check. <!-- declaration: preservation.before-cleanup -->

Dispatch preserves the destination meaning of `target_branch`, the completion protocol, complete output, current source head, and the shared-stash prohibition. <!-- declaration: dispatch.mechanics -->

Merge admission compares the full judged and pull-request heads, reads the diff, waits for terminal green checks, and records any weaker judgment basis. <!-- declaration: merge.requirements -->

## Tracker and the hourly report

The tracker mirrors coordinator state, and GitHub's pull-request attachment does not perform the issue transition. <!-- declaration: tracker.authority -->

The sweep examines every in-progress issue regardless of age and repeats after each dispatch, push, merge, and close. A dispatch immediately records the tracker event that moves every covered issue to in progress with its unit; the connector applies it when available, while an outage leaves pending synchronization without blocking unrelated dispatch. A stopped attempt that did not ship similarly records its state and preservation ref. <!-- declaration: tracker.reconcile -->

The reporting order incorporates the still-open pull request 60 source material:

1. Running work: agents, pull requests, checks, failures, skips, corrections, and observation times.
2. Tracker: every in-progress, merged-but-open, and filed-but-unmoved issue, swept by state.
3. Ready work: compare the backlog to what landed and dispatch; otherwise state the exact blocker or saturation reason.

The order and the dispatch action are part of the decision surface rather than a pointer-only recommendation. Before the report is written, step-three dispatches reach the tracker or the report names their pending synchronization when the connector is unavailable. <!-- declaration: report.three-parts -->
