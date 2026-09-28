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

Remove `--dry-run` to execute after inspecting the preview. The operator must supply a nonblank approver and, when coordinator state has no recorded root, `--root`; a conflicting root is refused. The plan digest and attempt anchors are checked before forge access. Worker files provide no authority. Supervisor loss before `supervision-finished`, or accounting expiry after a positive observation, leaves the ordinary retrieval path blocked. A named operator can resolve that launch using `--verify-integration --retrieve-remote-evidence --remote-recovery-attestation FILE --approver NAME` on the same `merge_unit.py` command. This path records an **attested**, never verified, resolution in the external coordinator ledger before using it. It neither requests a merge nor contacts the remote host, and retains the stage. An unreachable supervisor is not proof of death. Missing evidence never establishes quiescence. The operator must establish that the supervisor cannot submit, run or publish, that every launch job and descendant is fenced against execution and publication, and that administrators have prevented privileged requeue or resubmission of this launch. Record the concrete investigation and enforcement evidence for each assertion. Killing a supervisor alone, a terminal accounting row alone, an empty queue, or a successful cancellation request is insufficient. The trust boundary is the named operator's truthful investigation and durable fencing, not authentication or independent verification of their testimony. A false attestation or subsequently removed fence can admit evidence while a writer remains live; this residual risk is explicitly operator-owned. `FILE` is a JSON object with exactly `binding`, `supervisor`, `jobs`, and `privileged_requeue`. Each of the latter three contains exactly `{"status": "fenced", "evidence": "concrete investigation and enforcement record"}`. Values such as `unreachable` refuse. `binding` contains `launch_id`, `stage`, `request_sha256`, and `receipts_sha256` for the selected coordinator ledger run. The digests use SHA-256 of UTF-8 JSON with sorted keys and separators `(',', ':')`, as returned by `remote_verify.recovery_binding(run)`. Read these values from `STATE/remote-verifications/*.json`, matching the exact unit and candidate basis; a binding copied from a different launch or receipt set refuses. Retrieve first to preserve every complete, validated, bound claim receipt in coordinator state. Recovery refuses missing, incomplete, conflicting or invalid receipts and never creates or upgrades one; a completed FAIL remains FAIL. Slurm PASS still requires recorded terminal-success execution evidence. A read-only scheduler observation after supervisor loss can retain that success without granting quiescence; absent recorded success, attestation cannot supply it. A separate coordinator-owned success observation binds the launch, request, receipts, every discovered job ID, and the final empty-queue snapshot. Stale null markers and accounting expiry cannot erase it. Newly observed jobs, live/requeue indications, or contrary non-null results block its reuse until a fresh complete success observation covers every retained job identity. The saved snapshot is historical execution evidence, not continuous supervision; a wholly unobserved requeue before fencing remains part of the operator-owned residual risk. Missing or partial historical metadata cannot create this proof. The operator resolution substitutes only for the missing lifecycle authority. Repeated retrieval uses the journaled authority and existing receipts without further remote calls, and publication still passes the operator's original observation fence. The remote supervisor marker is never fabricated, and this recovery route supplies no automatic cleanup acknowledgment. <!-- declaration: code.merge-command -->

Before a new merge, at least one CI check must exist and every check must report `SUCCESS`; failed or unavailable reads also refuse. An exit-0 scope report must be an object with in_scope status, matching unit/attempt/head, a valid base ID, a scope list of strings, and empty outside/deletion lists; malformed success cannot be waived. Scope exits 1 and 2 require `--allow-unchecked-scope "reason"`, recorded with the approver and scope result (or verbatim malformed stdout/stderr on a nonzero exit) in a durable `merge-unit-OPERATION.json` in the state directory. An already merged matching PR is reconciled without reapplying current CI or scope policy; a different head or target refuses. New intents retain captured scope stdout/stderr in addition to the parsed report; legacy intents lacking raw fields retain their original observations without reconstructing lost text. Original precondition observations remain in an existing intent, while a newly observed historical merge does not fabricate them. An exact existing receipt is reused, and an older receipt with the wrong repository spelling is supplemented by the correct anchored URL. Dry-run prints the conditional command sequence with placeholders for the unobserved PR URL and commit IDs, performs no forge calls or state writes, and exits 2. Ordinary exit 0 means the receipt exists and advance returned success. To resolve an investigated uncertain request, pass `--abandon-intent OPERATION_ID --approver "Operator" --reason "Investigation outcome"`. The named operation must be the current unresolved intent for this attempt and PR. OPEN at the judged head records abandonment and exits 0 without merging or advancing; MERGED at that head reconciles instead, while a different head or target refuses. The atomic, fsynced `merge-abandonment-OPERATION.json` beside the retained intent records the original intent, who, why, the observed PR, and UTC observation time. The intent is marked `resolved_by_abandonment`; a crash between record publication and marking is repaired from the record on the next invocation. The next ordinary call can create one successor intent with a new operation ID and rechecks scope and CI. A second abandonment of the old ID refuses, including after a successor was created. <!-- declaration: code.merge-command -->


The command never resubmits an unresolved merge request: a queued request, a lost response, or a crash between intent persistence and transmission leaves a durable operation for inspection. Rerun after GitHub reports MERGED to record and advance, or explicitly abandon the investigated OPEN request using the recorded path above. Neither action deletes the intent. Abandonment attests an OPEN observation; it cannot prove an earlier queued request will never execute, so the named operator retains responsibility for investigating that uncertainty. The intent and abandonment journals retain the same-node trusted-writer boundary. The local coordinator lease is released before advance acquires it; advancement can still halt or retain a unit for its existing verification policy. A successful advance does not assert that every unit closed. <!-- declaration: limit.merge-command -->

## Declaring a Slurm verification host

In the external coordinator state's `verification-execution.json`, declare
`verification_host` with `ssh_alias`, `executor: "slurm"`, absolute `workdir_root`,
`python` and `git` paths, and a `slurm` object containing `partition`, `mem` and
`time`. The parser accepts these three values as plain string tokens and rejects
a missing `mem`. Keep actual host and resource values in that private policy.
The remote root exists before launch. The supervisor submits and polls;
verification runs exclusively inside the allocated worker.

A cleanup-unconfirmed warning means removal was not confirmed; retain any
surviving stage. Inspect the reported job IDs, launch job name and `cleanup.json`
(or the adjacent `STAGE.cleanup.json` once removal starts);
a successful cancellation request alone is insufficient to establish termination.
Before deleting stage contents, cleanup persists its final evidence beside the
stage and closes the in-stage lock while retaining an external cleanup lock.
The stage directory is removed last; `cleanup: removed` reports confirmed absence.
Final cleanup evidence stays beside the removed stage until its exact receipt is
durable in coordinator state; acknowledgment then retires the auxiliary files.
Audit-publication errors leave the actual scheduler observation unchanged.
After an interrupted deletion, the coordinator's durable ordinary acknowledgment
and positive lifecycle state permit completion of an empty or absent stage.
Completion uses atomic `rmdir` under the external lock, imports no staged harness,
and runs no verifier or scheduler. Any remaining files stay subject to normal
cleanup guards; a partially deleted nonempty stage with no harness stays retained.
Before `rmdir`, completion checks the adjacent journal's stage, launch identity,
and digest against its saved receipt. An unreadable or mismatched journal leaves
the stage intact and reports cleanup unconfirmed. A lost cleanup response causes
retrieval to save the bound snapshot first, then check its digest and finish
within the same invocation. Removed status follows an actual absence check.
When a nonempty stage returns to normal cleanup, the coordinator saves retirement
of its old receipt pin before that cleanup can replace the journal. Empty-stage
validation preserves the existing pin on a mismatch.
External snapshots remain nested audit data, separate from fresh lifecycle
observations. Retrieval preserves their exact contents in coordinator state
before retirement, including after an interrupted final write. When the journal
is already gone, the recorded removal permits retiring its leftover lock; a
present mismatched journal stays intact.
Cleanup is conditional on terminal accounting for all launch jobs, finished
supervision and acknowledged claim evidence. Missing job IDs are recovered by
name from `squeue` and `sacct`; unavailable or empty discovery leaves the stage
intact. Restore access and retrieve the original launch with
`--verify-integration --retrieve-remote-evidence`. Retain unresolved stages and
receipts; the binding blocks replacement submissions and admission until its
evidence is resolved, and reruns retrieve the original launch.

Cleanup finishes scheduler observation with accounting by name followed by the
live queue, after every individual accounting/cancellation operation, and records
`scheduler_observed_at` in UTC. A live row, failed or ambiguous query, or newly
discovered ID lacking already-collected terminal accounting retains the stage.
New IDs are saved for the next bounded retrieval; the final queue observation
ends that cleanup pass's scheduler queries. An observed negative result resets the
coordinator's stale reconciliation/publication flags and acknowledgment;
a transport failure alone leaves them unchanged. Admission also blocks persisted
negative lifecycle evidence despite stale flags. Later positive observations can
reconcile the original immutable receipts. A positive cleanup observation in the
same invocation triggers one bounded re-ingestion and reconciliation under the
binding lock, followed by cleanup with the durable acknowledgment. That cleanup
again checks the scheduler; a negative result still revokes reconciliation.
A privileged requeue remains possible after the final observation;
`--no-requeue` and the worker-started guard protect against
ordinary restarts and receipt replacement, without an atomic scheduler fence.

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


Blocking relationships are recorded as tracker relations, established at filing and at dispatch, and dispatch order is read from the resulting graph. A dependency stated only in an issue's prose is not traversable, so no query surfaces what a piece of work is waiting on. <!-- declaration: tracker.dag -->

The reporting order incorporates the still-open pull request 60 source material:

1. Running work: agents, pull requests, checks, failures, skips, corrections, and observation times.
2. Tracker: every in-progress, merged-but-open, and filed-but-unmoved issue, swept by state.
3. Ready work: compare the backlog to what landed and dispatch; otherwise state the exact blocker or saturation reason.

The order and the dispatch action are part of the decision surface rather than a pointer-only recommendation. Before the report is written, step-three dispatches reach the tracker or the report names their pending synchronization when the connector is unavailable. <!-- declaration: report.three-parts -->

## Hourly reconciliation

The hourly loop includes this comparison, with every coordinator state directory for the repository and a window overlapping the preceding successful observation:

```bash
python3 "$HANIG_ORCHESTRATE_DIR/scripts/reconcile.py" \
  --state-dir "$STATE" --since 2026-09-26T00:00:00Z --json
```

The sweep examines every in-progress issue regardless of age and repeats after each dispatch, push, merge, and close. A dispatch immediately records the tracker event that moves every covered issue to in progress with its unit; the connector applies it when available, while an outage leaves pending synchronization without blocking unrelated dispatch. A stopped attempt that did not ship similarly records its state and preservation ref. The read-only reconciler defaults to the checkout origin and branch `main`; `--repo OWNER/REPO` (or a forge URL) and `--branch` select alternatives. The shorthand uses the host resolved by `gh repo view`; a failed resolution is unreadable input. Repeated `--state-dir` includes older runs. Its window is an inclusive timezone-bearing `--since`, the most recently merged `--limit N` PRs, or both. UNMEDIATED MERGE means no matching record accounts for the PR, repository, target and historical head/merge; the operator investigates bypass and reconciles evidence and tracker obligations. UNACKNOWLEDGED OBLIGATION means a matching source's outbox intent lacks a receipt; the operator resolves ambiguity by receiver read-back or deduplication before draining and attesting. MISMATCHED SOURCE means coordinator anchors name another repository or several; the operator selects the correct source, while the mismatched source contributes neither merge nor obligation coverage. Missing anchors are unreadable. Exit 0 means all sources were read without findings; exit 1 means findings; exit 2 means unreadable input and precludes a clean report. The command changes no forge, tracker or coordinator state. <!-- declaration: tracker.reconcile -->

Forge routing supports standard HTTP(S) and SSH remotes without explicit ports and keeps the observed HTTP(S) PR URL; ports are refused rather than silently discarded. Reconciliation requires a single-parent squash result; merge observations and the squash method remain attested. The actual merge parent's SHA supplies `--target-commit`, including when the target moved after the original observation. Head matching does not atomically freeze CI reruns or concurrent PR retargeting, and a one-parent result alone does not distinguish squash from a one-commit rebase by another operator. The same credential can bypass `merge_unit.py`. This detects bypass after the fact; it does not prevent it. Coverage is limited to supplied directories and the merged-PR window, so a direct push without a merged PR is outside the comparison. A pending request accounts for a lost response; cancelled or abandoned requests do not account for later merges. Historical reconciliation attests recording, not who executed the merge. A matching pending record cannot distinguish a lost response from a later direct merge of the same PR and head; this check establishes record presence, never execution attribution. The observation is not an atomic forge snapshot; a changed or unreadable page calls for a retry. <!-- declaration: limit.merge-command -->

The hook resolves a supported `gh` locator through a bounded `gh repo view` read, then reports counts after exact anchor matching for a single literal `git [-C DIR] push origin ...` without push options, or explicit `gh ... --repo OWNER/REPO` (`-R` also works). Other routing, compound commands, malformed inputs, missing anchors and differing hostname spellings produce repository-specific uncertainty. Hostname spelling is preserved in the comparison, and action detection is unchanged.
