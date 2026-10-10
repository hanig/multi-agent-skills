# Tracker synchronization details

The generated declarations in `../SKILL.md` decide behavior. This reference
explains the coordinator and connector boundary. <!-- declaration: placement.reference-elaboration -->

## Capability boundary

The coordinator drafts tracker work without network access. If the active
session has no real connector, it must preserve the reviewed draft or outbox <!-- declaration: capability.tracker -->
intent, report the pending synchronization to the owner, and must not invent a <!-- declaration: capability.tracker -->
remote result. <!-- declaration: capability.tracker -->
Tracker credentials and remote operations stay in the authorized connector
session, never on the shared login-node coordinator. <!-- declaration: tracker.credential-boundary -->

## Approval and creation

The owner sees the project, every issue title, and the count before one named
approval. No project or issue may be created while approval remains required. <!-- declaration: tracker.approval -->

After approval, run `linear_sync.py file --draft tickets.json`; use `--preview` to run checks without writes and `replay OPERATION_ID --draft tickets.json` after a crash. The binding file is `.hanig/linear-binding.json` beside the draft. <!-- declaration: tracker.apply -->

The digest covers project name, slug, summary, description, team and repository, plus each unit, title, body and `blocked_by`. Progress identifiers, URLs and read-back do not change it. Old approvals without a digest need one re-approval. <!-- declaration: tracker.approval -->

`--repository OWNER/REPO` on `tickets.py draft` defaults to the draft directory origin. Derived ids include workspace, repository and plan. Every recorded project needs exact `swarm-plan` and `swarm-repo` lines, and unit issues need `swarm-unit`, `swarm-repo` and `swarm-body`. The body marker holds SHA-256 of the approved body's exact UTF-8 bytes, before adding identity lines. Foreign projects and unmarked objects need an owner-reviewed audit and `--adopt-checked`; ambiguity refuses before creation. <!-- declaration: tracker.apply -->

Linear re-renders Markdown. Filing, replay and CONFIRMED compare exact titles and identity lines, including the body digest and any PR 3 trailer, never rendered issue prose. Missing or different body digests write the approved body plus identity block; matching digests leave prose alone. Project content is compared by identity lines too; project name and plain-text description (the summary on creation) remain exactly compared after the existing terminal-whitespace trim. Prose edited before an intact terminal identity block is not detected by `file` or `issue` commands; changing the approved body is. Ad-hoc issues carry a `swarm-body` digest above the final `swarm-deps`, `swarm-op` and `swarm-approver` lines. Issue read-back compares exact titles, digests, independence declarations and dependency/provenance lines; supplied body edits replace the digest, while edits without a supplied body retain it. Project content prose is also outside detection. Filing replay of older operations without body identities refuses before network access; run `file` with the approved draft to add markers under a new operation, preserving the old record. <!-- declaration: tracker.apply -->

One identity parser serves binding, conflicts, rewriting, replay and audit. Only a complete terminal block counts: project plan/repo, or issue unit/repo with a body digest (optional for legacy bindings). Lines are plain or backticked, in any order with blank separators. Above a PR 3 trailer, adjacent `swarm-deps-by` provenance is preserved. Lone marker-like lines and blocks followed by prose neither bind nor conflict. Dependency-marker scans likewise read only PR 3's terminal position; fenced or quoted examples outside that position do not refuse. Declared limit (owner decision 2026-10-07): a backtick-quoted `swarm-deps:` example on an issue description's final line is read as a malformed dependency trailer and refuses adoption. Move the example or change the line order to recover. <!-- declaration: tracker.apply -->

For a project, text a person appends after the identity lines makes the next `file` refuse as unmarked and name its ID. After audit, `--adopt-checked` re-binds it and writes the identity block after that text, preserving the text. For an issue, `--adopt-checked` re-binds it and restores the approved body: text added in Linear is replaced. Explicitly selected unmarked issues, including derived-ID issues, are recoverable this way. Complete terminal identities naming another unit or repository still refuse adoption. Declared limit (owner decision 2026-10-07): text a person appends after an issue's PR 3 dependency trailer in Linear is dropped when that issue is re-adopted with `--adopt-checked`. <!-- declaration: tracker.apply -->

Issue adoption writes approved prose and preserves earlier operation provenance; project adoption preserves its prose. Identity lines sit above PR 3's final dependency trailer. Changed edges rewrite each marked endpoint's full adjacency, including kept blockers, before the relation changes. These are managed before/desired marker values in the same durable operation engine. <!-- declaration: tracker.edges -->

## BlockedBy reconciliation

`blocked_by` is truth; filing computes the live delta. `add_blocked_by` and `remove_blocked_by` are previews. Remove only undeclared blockers that are current plan units; keep and report ad-hoc, external and deleted-unit blockers, using `issue edit` for an explicit removal. <!-- declaration: tracker.edges -->

Archived, trashed or deleted issues refuse only when managed for mutation: unit issues during filing, and the target and touched counterparts in issue commands. Graph-only neighbors remain observed for cycle checks; their edges are kept and reported even when archived or trashed. Missing endpoints still prevent complete reads, and managed endpoint checks remain in force. Fresh refusals before an operation record exists exit 2; replay refusals of every kind exit 3, including digest mismatches and retained deleted managed issues. <!-- declaration: tracker.edges -->

Read every unit and touched relation by id, even when a project listing lags. Confirmation requires binding, coverage, plan_edges, misplaced, declared_edges and expanded cycle audit checks all present and CLEAN. Plan units are exempt from relationless and judged by plan_edges. The read-back contains identifiers and edges, never issue text. <!-- declaration: tracker.apply -->

Each key represents the blocked issue and may use a unit id, tracker identifier, <!-- declaration: tracker.readback-shape -->
or UUID. An issue observed with no blockers must appear with an empty list; <!-- declaration: tracker.readback-shape -->
omitting it is indistinguishable from not looking. Without a read-back,
`remove_blocked_by` must remain `null`, and after filing `check` treats that <!-- declaration: tracker.readback-shape -->
unknown state as drift. <!-- declaration: tracker.readback-shape -->

The loop is apply, read, re-draft, and check. The read-back is attested because
the coordinator receives the connector session's report rather than making the
network read itself. Synchronization must not be claimed without its read time <!-- declaration: tracker.attestation -->
and source. <!-- declaration: tracker.attestation -->

## Outbox acknowledgments

Unit state remains authoritative when tracker access fails. A tracker outage
must never mutate swarm state. <!-- declaration: drain.authority -->

Run `linear_sync.py drain --binding .hanig/linear-binding.json --state-dir DIR` (repeat `--state-dir` for other waves), then `audit`. Draft mode uses `--draft tickets.json`, requires the exact envelope project slug, refuses nameless `swarm`, and requires any tracker identifier to agree exactly. Both modes check issue project and team before mutation. <!-- declaration: outbox.receipt -->

Comments carry reason, unit state, attempt, evidence digest and intent/evidence/order markers. IDs are derived from workspace, project, issue and key. State reconciliation reads every comment page, ignores copied non-derived IDs, and includes comments confirmed by id in this run despite listing lag. One host locks the project; two hosts are an accepted eventual-reconciliation boundary. The latest instant/key state intent wins in one state write; earlier intents are superseded. <!-- declaration: outbox.receipt -->

Receipts record read-back at a time, not present synchronization. Already-receipted intents receive no new comment or receipt but their issues are reconciled. Missing derived comments are uncovered history; incomplete reads or failed reconciliation return 3 even with all intents receipted. Missing receipt state means unacknowledged, never that no filing occurred. Drain exits are 0 when only superseded intents remain, 3 for other pending intents or failed reconciliation, and 2 for configuration/key/lock errors. Dry-run performs no mutation or receipt write and retains the same pending-intent exit convention. <!-- declaration: outbox.receipt -->

## Read-only audit <!-- declaration: tracker.check -->

`linear_sync.py bind --project ID --repository OWNER/REPO` writes `.hanig/linear-binding.json` in the repository root. Multi-team projects are ambiguous and refused. Names are descriptive; identity comparisons use exact ids. Legacy drafts resolve their exact team key or name from the project's teams, and obtain workspace identity from the viewer when the draft lacks it. <!-- declaration: tracker.check -->

Run `linear_sync.py audit --binding .hanig/linear-binding.json --out audit.json`, or `audit --draft tickets.json --plan plan.json --state-dir DIR --out audit.json`, then quote `linear_sync.py section --audit audit.json` with the same source arguments. <!-- declaration: tracker.check -->

The fixed state inputs are `swarm-state.json`, `outbox.jsonl` and `outbox-receipts.jsonl`, including their absence. Paths in the record are absolute. The read interval covers paginated issue and relation reads plus comment pagination and updatedAt rechecks. The intent_order check detects a state differing from the latest visible genuine state-intent comment; it shares comment-list visibility limits. An external write after that interval is outside the claim. Audit exits are CLEAN 0, DRIFT 1, UNKNOWN 3 and configuration error 2. State comparison requires both plan and draft. <!-- declaration: tracker.check -->

The key comes from `LINEAR_API_KEY` or shell-word parsing of `~/.config/hanig/linear.env`. Output streams and error text are redacted; input files are never scrubbed. The program never copies its loaded key into data files, while operator-supplied secrets in other data remain a declared limit. <!-- declaration: tracker.credential-boundary -->

Request accounting uses `coverage.requests`, which counts transport calls including failed requests. `coverage.pages` also includes nested connection pages returned within a request. Project and marker stability reads select only IDs and `updatedAt`; other graph references use batches of 50, with nested relations limited to 20. Audit comment lists use the same 50-by-20 shape with overflow paging. The offline request-budget fixture has 150 project issues, 60 relations, 10 external blockers, and three relation overflows. Its limits are 20 requests for audit, 42 for an issue creation with two dependencies, 20 plus comment paging for one drain intent, and 120 for filing 30 units with 40 edges into an empty project. These fixture limits are not a bound for arbitrarily large graphs, transitive frontiers or overflow lists. <!-- declaration: tracker.check -->

## Ad-hoc issues <!-- declaration: tracker.apply -->

`linear_sync.py issue new --binding FILE --title TITLE --body-file BODY --blocked-by ARC-123 --approver NAME` files an issue with its dependency. Repeat or combine `--blocked-by` and `--blocks` for intermediate issues, or use `--independent REASON` alone. `--body-stdin` replaces `--body-file`; bodies never travel in argv. `--preview` runs the key, binding, content, touched-counterpart, structured-line and graph checks without creating an operation or taking the write lock. The approver is an attestation, not authenticated authority. <!-- declaration: tracker.apply -->

`issue edit ARC-123 --binding FILE --add-blocked-by ARC-124 --remove-blocks ARC-125 --approver NAME` changes the resulting graph. `--add-blocks` and `--remove-blocked-by` cover the other directions; all four flags repeat. Title and body are optional, and `--independent REASON` or `--clear-independent` controls the independence line. Removing an open issue's last relation requires a reason, including at the other endpoint. Structured whole lines (`blocked by:`, `depends on:`, `blocks:`) must agree with the resulting edges; code, blockquotes and ordinary prose are excluded. Prose candidates only warn. Supplied operation marker lines are refused. <!-- declaration: tracker.apply -->

An operation records each touched issue's before and desired values before writing. A new issue's server-assigned identifier fills a recorded template bound to its derived ID. Marked counterparts retain their prose, operation and approver and gain `swarm-deps-by`; marked counterparts outside the project refuse. Every trailer precedes relation changes. Read-back checks the full edge sets and repeats the graph cycle check; drift is reported without undo. Exits: 0 confirmed, 3 incomplete or divergent, 2 pre-mutation refusal/configuration error. <!-- declaration: tracker.apply -->

Records live at `~/.local/state/hanig-swarm/linear-ops/WORKSPACE/PROJECT/OPERATION.json`, with an append-only fsynced `.progress.jsonl`. `HANIG_LINEAR_OPS_DIR` relocates the record root for testing or another external state directory; a Git worktree location refuses. `issue replay OPERATION --binding FILE` verifies the specification digest, then only verifies a confirmed operation, or resumes an incomplete one where live managed values still match before or desired. Deleted confirmed issues are never recreated. A fresh `new` invocation is a new operation; recovery uses the printed operation ID. Legacy issue operations lacking a body digest can confirm against re-rendered prose using their recorded identity lines; replay leaves the immutable specification intact and cannot detect a body digest absent from that record. <!-- declaration: tracker.apply -->

The audit's `declared_edges` check needs no local record; `op_incomplete` additionally names local unconfirmed operations and their replay command. Local record and journal changes invalidate an earlier audit section. Issue writes share drain's project lock on this host. Two hosts can still close a cycle concurrently; post-write checking and the audit report that race. A refusal during the locked graph read can leave the shared lock file, but no operation record or remote write. <!-- declaration: tracker.apply -->
