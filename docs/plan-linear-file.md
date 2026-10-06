# Plan: PR 4, file an approved ticket draft into Linear

**Owner decisions, 2026-10-06:** approved as revised after its exhausted
plan review and a design check with Sol; the orchestrator is authorized to
create one throwaway Linear project in the Arc team with three issues for
criterion 8's live check and to cancel them afterwards.

The rest of slice 4 of `docs/plan-linear-dag-control.md` (owner-approved
2026-10-05; slice 4 was split, and PR 3, pull 187, shipped the ad-hoc
`issue` command). This PR replaces the step in `hanig-project` where a
session read `tickets.json` and created the project and issues by hand
through the Linear MCP connector. The draft contract and its one approval
stay; the program applies an approved draft.

It reuses what is merged: PR 1's binding validation, reader, coverage rules
and audit (pull 185), PR 2's per-project lock (pull 186), and PR 3's
operation engine, request budget and live-measured Linear facts (pull 187).
It also repairs findings 1-6 left open on the unmerged `hanig/linear-api`
branch, as the parent plan assigns.

## Approval covers exactly what is filed

`tickets.py approve` records `approval.content_digest`: SHA-256 of the
canonical JSON of the immutable specification, namely the project's name,
slug, summary, description and team, and for every issue its unit, title,
body and `blocked_by`, plus `project.repository` (below). `add_blocked_by`
and `remove_blocked_by` are derived views, not inputs. Fields the
program writes back (`project.linear_id`, issue `linear_id` and
`identifier`, `url`, the read-back) are progress and are not in the digest,
so writing them never invalidates the approval. Progress is never trusted on
its own: identity is proven by markers in Linear (below), so editing a
recorded id cannot redirect an approved filing. The read-back the program
writes holds only identifiers and edges, never issue text. `linear_sync.py file` recomputes the digest and refuses, before
any network call, a draft whose approval is missing, whose approver is
blank, whose digest is absent (owner decision D3: drafts approved before
this change are re-approved once), or whose digest differs. `swarm
autopilot` approvals pass without a digest as today. `tickets.py draft`
keeps carrying an approval forward only when the digest still matches.

Title, body and every other approved field are refused, not redacted, when
they contain the loaded key (D2).

## Identity and adoption

Every derived id and marker is namespaced by repository, as the parent plan
requires, so two repositories using the same plan slug never collide.
`tickets.py draft` records `project.repository` (the forge path, for example
`OWNER/REPO`) from `--repository`, or from the draft directory's `origin`
remote when not given; a draft without it is refused by `file`. It is part
of the approved specification, so approval covers it.

The project is bound by id. If `project.linear_id` is set, that project is
used and must belong to the draft's team; when it lacks this draft's
`swarm-plan` and `swarm-repo` markers it is a project this program did not
create, and the foreign-project rule below applies to it. If not, the program looks for its
derived project id, `project:<workspace id>/<repository>/<plan slug>`; a project found by
that id must carry the `swarm-plan: <slug>` marker in its content and `swarm-repo: <repository>`. If none
exists and a project with the same name exists that is unmarked, or marked
with this repository, the run stops before any create and names it (a
same-named project marked with another repository is that repository's and
is ignored), so the owner records the right one in
`project.linear_id`. After filing, the draft and a binding file (PR 1's
format) both record the project id.

Each unit's issue id is derived as `issue:<workspace id>/<repository>/<plan slug>/<unit>`
and its description carries `swarm-unit: <slug>/<unit>`,
`swarm-repo: <repository>` and `swarm-body: <sha256>` as trailer lines.
The first two lines bind the issue to the unit. The body digest is SHA-256
of the approved body's exact UTF-8 bytes, including whitespace, before any
identity lines are added.

**Content identity, not Markdown rendering.** Linear re-renders descriptions:
the live check changed `- kind:` to `* kind:` and dropped the blank line
before the marker block. Filing, replay's before/desired checks and
CONFIRMED therefore compare the exact plain-text title and the identity
lines (plus any PR 3 dependency trailer), never the rendered prose. A
different or missing body digest, including adoption, writes the approved
body plus identity block. A matching digest leaves the prose alone; a
title-only change writes only the title. Project content is also compared
by identity lines, while the project name and plain-text description
(the approved summary on creation) keep their exact comparisons after the
existing terminal-whitespace trim.

**Declared limit:** prose edited by a person in Linear after filing is not
detected by `file`; changing the approved body is. The body marker records
the approved content identity, not a checksum of Linear's current rendered
prose. Project content prose is likewise outside the comparison.
Operations recorded before body identities existed refuse replay before any
network call; run `file` with the approved draft to add the missing markers
under a fresh operation, preserving the old operation record.

**Provenance by marker.** Every project and issue the draft names by id must
carry this draft's markers: the project its `swarm-plan` and `swarm-repo`
lines, each unit issue its own `swarm-unit` and `swarm-repo` lines. A named
object without them refuses before any mutation, unless `--adopt-checked` is
given (the owner's explicit re-adoption after reviewing the audit listing).
Adopting an existing project or issue (steps 1-2 below, or an explicit id)
writes these markers onto it as the operation's first step for that object,
so every later run, on any host, can prove the binding from Linear alone.

**Coexisting with PR 3's dependency trailer** (settled with Sol after the
plan review exhausted). An adopted issue may already carry PR 3's trailer
(`swarm-deps`, `swarm-op`, `swarm-approver` as its last three non-empty
lines). Filing never displaces it: the identity lines `swarm-unit`,
`swarm-repo` and `swarm-body` go immediately above that trailer, so PR 3's parser still finds
it last. When filing changes an edge, it rewrites the `swarm-deps` line of
every endpoint that carries one to that issue's resulting full adjacency,
including blockers it deliberately keeps (ad-hoc or external), and records
an operation-provenance line as PR 3 does for counterparts. These
description changes are managed values in the operation record (before and
desired), written before the relation change and resumed or refused by
replay exactly as in PR 3; prose and earlier provenance lines are preserved
unchanged.
For a unit with no identifier in the draft:

1. an issue at the derived id carrying that unit's marker is adopted;
2. otherwise one issue in the project with exactly the drafted title, not
   bound to another unit, is adopted;
3. two or more title candidates, or a title candidate already bound to
   another unit by identifier, derived id or marker, stop the run before any
   create and name them (finding 4);
4. otherwise the issue is created.

In a project that does not carry this draft's `swarm-plan` and `swarm-repo`
markers, the run refuses before any mutation unless
`--adopt-checked` is given after the owner reviewed `linear_sync.py audit`'s
listing: that covers adopting existing issues (step 2), creating issues
(step 4) and every edge change in that project, not only creation. Linear enforces no
unique titles and a listing can lag, so a hand-filed issue the listing
misses can still be duplicated; that is a declared limit. A project
carrying the markers is this program's whether or not its id is the derived
one, because adoption writes them (provenance by marker); someone who copies
the markers onto another project makes it look like this program's, the same
trusted-writer boundary the repository already declares.

## Edges and the cross-boundary policy

Each issue's `blocked_by` is the source of truth. `file` reads every unit
issue's current blockers and computes the changes itself: an edge declared
in `blocked_by` and absent in Linear is created with PR 3's relation ids and
confirmed from the endpoints' paged relation lists; an edge present in
Linear between two unit issues of this plan and not declared is removed. The
draft's `add_blocked_by` and `remove_blocked_by` remain a preview of that
computation for the human and are never trusted as instructions. Removal
touches only edges whose blocker is another unit issue of the same plan. An edge to an
issue that is not one of this plan's units (an ad-hoc blocker filed with
`issue`, or a blocker in another project) is never removed by filing; it is
reported, and the owner removes it with `issue edit` if it is truly stale.
`tickets.py` gains the same rule, so a draft no longer lists such edges for
removal. A removal that is retried after it succeeded, including one named
by UUID for an issue outside the project, is satisfied rather than an error
(finding 5).

## Crash safety, read-back and audit

Filing runs under PR 2's project lock and records an operation record and
progress log with PR 3's engine (one operation per filing run). As in PR 3,
every refusable check runs first, and the operation record is written and
fsynced before the first Linear mutation, so a crash always leaves a
resumable record. `replay` recomputes the approval digest from the draft and
refuses when it differs from the digest the operation recorded, so a draft
edited after a crash is never filed on the old approval. Each step's
result is written back to the draft as progress, atomically. After the last
step it reads back the project, every unit issue (by id, so a lagging
listing cannot hide a just-filed issue, finding 6) and every touched edge,
writes the tracker read-back into the draft, and runs PR 1's audit scoped to
the plan (`plan_edges`, `misplaced`, `declared_edges`,
`cycle`, over the transitively expanded graph as in PR 3). The
run reports CONFIRMED only when the read-back matches and every required check
(`binding`, `coverage` and those four plan checks) is present and CLEAN;
otherwise exit 3, resumable with `replay`. Plan unit issues are exempt from
`relationless`; their dependencies are judged by `plan_edges`.

## Command

```
linear_sync.py file --draft tickets.json [--adopt-checked] [--preview]
linear_sync.py replay OPERATION_ID --draft tickets.json
```

Exit 0 confirmed, 3 incomplete or not confirmed, 2 refused before mutation.
`--preview` runs every check, prints what would be created, adopted, linked
and unlinked, and sends and writes nothing.

## Docs

`hanig-project` step 4 runs `linear_sync.py file` after approval instead of
the MCP connector; the `tracker.apply` and `tracker.edges` declarations say
so. `docs/tracker-outbox.md` and CLAUDE.md name it.

## Acceptance criteria

1. `file` refuses before any network call: no approval, blank approver, no
   digest, a changed digest (including a changed `blocked_by` or
   repository), a draft without `project.repository`, and a draft
   containing the key; writing identifiers back never invalidates approval.
2. Re-running `file` on a filed draft creates nothing; the operation record
   exists before the first mutation; a crash after any step (simulated after
   each) is completed by `replay` without duplicates; `replay` of an edited
   draft refuses; a recorded project or issue id edited to point at an
   object without this draft's markers refuses before mutation, and
   adoption writes the markers so a re-run proves the binding from Linear;
   recording ids never invalidates the approval. A fake that changes list
   bullets and collapses the blank line before trailing markers must still
   confirm, replay and re-file without prose rewrites. Restoring whole-body
   comparison must fail that regression. Reapproval changes the body digest;
   human prose edits with intact markers remain outside detection.
3. Adoption follows steps 1-4; every ambiguity, a bound title candidate, and
   a same-named unrecorded project stop before any create; in a foreign
   project, adoption, creation and edge changes all need `--adopt-checked`;
   two repositories with the same plan slug and project name file into
   separate projects without conflict; an explicitly recorded unmarked
   project needs `--adopt-checked`.
4. Edges: a draft stating dependencies only in `blocked_by` files them;
   additions are created and confirmed; removals touch only
   plan-unit blockers; an ad-hoc or external blocker is reported and kept;
   a repeated removal is satisfied.
4a. Integration: adopt issues that already carry PR 3 trailers, add a plan
   edge while keeping an external blocker, crash between the trailer write
   and the relation write, replay, and the scoped audit is CLEAN with both
   trailers matching Linear.
5. Read-back covers every unit issue by id; CONFIRMED requires `binding`,
   `coverage`, `plan_edges`, `misplaced`, `declared_edges` and `cycle` all
   present and CLEAN. A missing required check yields INCOMPLETE (exit 3),
   and a cycle closed by another writer through ad-hoc issues is not
   confirmed. `relationless` is not a filing requirement for plan units.
6. Request budget: filing a 30-unit plan with 40 edges into an empty project
   uses at most 120 requests, pinned by a counting test.
7. The key never reaches output, a written file (the read-back holds only
   identifiers and edges) or a child other than `linear_sync.py`; every network call goes through `linear_api.transport`;
   `swarm.py`, `tickets.py` and `drain_contract.py` import no network module.
8. Live check before merge, owner-authorized: a throwaway Linear project with
   three units (one blocked by another, one independent) filed, read back,
   audited CLEAN on its plan checks, re-filed with no change, then the
   project and its issues canceled.

## Not in this PR

Waves, the MCP write deny (PR 5), and converting existing hand-filed
projects.
