# Plan: PR 3, file and edit ad-hoc issues only as part of the DAG

**Owner decisions, 2026-10-05:** approved as revised after its exhausted
plan review and a design check with Sol; the orchestrator is authorized to
create two throwaway issues in the skills project for criterion 9's live
check and to cancel them afterwards.

Part of slice 4 of `docs/plan-linear-dag-control.md` (owner-approved
2026-10-05). Slice 4 is split for reviewability: this PR is the ad-hoc
`issue` command, the write path behind the owner's original complaint (new
issues filed with blockers written as prose, outside the graph); approved
`tickets.json` filing follows as PR 4. PR 1 (pull 185) shipped the binding
and audit; PR 2 (pull 186) the drain. The Linear MCP write path stays usable
until PR 5 denies it.

## Command

```
linear_sync.py issue new  --binding FILE --title T (--body-file F | --body-stdin)
                          [--blocked-by ID ...] [--blocks ID ...] [--independent REASON]
                          --approver NAME [--preview]
linear_sync.py issue edit IDENTIFIER --binding FILE [--title T] [--body-file F | --body-stdin]
                          [--add-blocked-by ID] [--remove-blocked-by ID]
                          [--add-blocks ID] [--remove-blocks ID]
                          [--independent REASON | --clear-independent]
                          --approver NAME [--preview]
linear_sync.py issue replay OPERATION_ID --binding FILE
```

Bodies come from a file or stdin, never argv. `new` requires at least one dependency (`--blocked-by` and `--blocks`, each
repeatable and combinable, so an intermediate issue can be filed with both)
or `--independent REASON`, and refuses a dependency together with
`--independent`. An issue
that depends on nothing says why; that is the rule the audit's `relationless`
check enforces after the fact, enforced here before filing. The same rule
binds `edit`: an edit whose resulting open issue would have no relation and
no independence reason (for example, removing its last relation) refuses
unless it also sets `--independent`. It binds the other endpoint of every
removed edge too: if that issue is open, in the bound project, and would be
left with no relation and no independence reason, the edit refuses and names
it, so the owner first gives it a relation or a reason with its own edit. `--preview`
runs every pre-mutation check first, including the key check, and only then
prints the specification and the check results; it sends nothing and writes
nothing.
Exit codes: 0 done and read back, 3 applied incompletely or not confirmed
(the operation stays resumable), 2 usage, configuration or refusal before
any mutation.

## Authority

`--approver NAME` is required and recorded in the operation record and in
the issue's marker trailer. It is an attestation, the same model as
`tickets.py approve` and `swarm.py promote --approver`: the program records
who authorized the write and cannot verify it. Under a confirmed
orchestrator mandate the orchestrator names the owner who confirmed it;
outside one, a person must approve the specific issue. A blank or missing
approver refuses before any network call. The mandate confirmation itself is
conversational and has no durable record, a declared limit.

## Operation record

After every pre-mutation check passes and before the first mutation, `new`
and `edit` write an operation record and fsync it:
`~/.local/state/hanig-swarm/linear-ops/<workspace id>/<project id>/<operation id>.json`,
outside every Git worktree. The operation id is a random UUID minted once.
The record holds an immutable specification (binding ids, title, body,
dependency changes, independence reason, approver, the spec's SHA-256) and
an append-only progress log beside it (`<operation id>.progress.jsonl`), one
fsynced line per completed step. `replay` reloads the specification and refuses if its digest does not
match. The record stores, for every managed value (title, body, trailer,
each edge), the value read before the operation and the desired value.
Replay of a CONFIRMED operation only verifies: a later divergence is
reported as DRIFT, never repaired, because a newer operation may have made
it. Replay of an INCOMPLETE operation re-reads every managed value and
continues only where the live value equals either the recorded before value
or the desired value; a relation to add that is present, or to remove that
is absent, is satisfied. Any other live value means something newer changed
the issue, and replay refuses with exit 3 naming the value rather than
restoring an old snapshot. Nothing outside
the record decides what a replay does. If the record shows a confirmed
create but the issue no longer exists, replay reports "issue deleted after
completion" with exit 3 and recreates nothing.

Derived ids (SHA-256 with UUID v4 version and variant bits): the issue a
`new` operation creates is `issue:<workspace>/<project>/op/<operation id>`;
an `edit` operation addresses its existing issue by the identifier given and
the Linear id read for it, never by a derived id; each relation is
`relation:<workspace>/<project>/<blocking issue id>/<blocked issue id>`. A
rejected create is accepted only when the object read back by that id
carries this operation's marker (issue) or the same type and endpoints
(relation); anything else stops the operation.

The issue body is the given body, then a plain `swarm-independent: <reason>`
line when independent (the exact form the audit matches), then a marker
trailer as the last three non-empty lines, written as code spans:
`swarm-deps: blocked-by=<ids> blocks=<ids>` (the issue's intended edges after
this operation, identifiers sorted, `-` for none), `swarm-op: <operation id>`
and `swarm-approver: <name>`. The trailer is written in the same mutation as
the issue's create or edit, before any relation changes, so the intended
graph is visible in Linear from the first step, on every host. An edge
belongs to both of its endpoints, so an operation that adds or removes an
edge also rewrites the `swarm-deps` trailer of the other endpoint when that
issue carries one, before the relation change. The operation's touched set
is its target plus every such counterpart; authorization, every pre-mutation
check and the final read-back cover the whole set. A counterpart outside the
bound project that carries a `swarm-deps` trailer refuses the operation
before mutation, because this command writes only inside the bound project.
A counterpart's structured dependency lines are checked against its
resulting edges like the target's, and its prose is preserved unchanged; the other endpoint's
operation id and approver lines are kept and a `swarm-deps-by: <operation
id>` line records the rewrite. Each rewrite is a progress step that replay
completes.

## Checks before any mutation

- The binding validates as in PR 1 (workspace, team and project ids).
- Every referenced issue exists and is readable in the bound workspace.
  Blockers may sit in other projects; an issue being edited must be in the
  bound project.
- No title or body contains the loaded key; such content is refused, not
  redacted (owner decision D2).
- Structured dependency lines in the body must name exactly the issue's resulting relations, which are its current
  relations plus additions minus removals for `edit`, and the declared ones
  for `new`; any mismatch refuses. A structured line is a whole line
  consisting only of `blocked by:`, `depends on:` or `blocks:`
  (case-insensitive) and a comma- or space-separated list of issue
  identifiers, outside fenced code blocks, indented code and blockquotes. A
  line with any other words is prose, which only the advisory detector
  sees. PR 1's prose-dependency detector runs on the body too, and its
  candidates are printed as warnings, not failures.
- The resulting graph, which is the current graph (the bound project's
  issues and every referenced issue, read with PR 1's audit reader and its
  coverage rules) minus removed edges plus added edges, must have no
  `blocks` cycle, so reversing a dependency in one edit is allowed; an
  incomplete read refuses.

Every check in this list runs, and passes, before the operation record is
written. A refused request writes nothing to disk.

## Mutation and read-back

Order: create or edit the issue; create each added relation; delete each
removed relation (found by type and endpoints); write the progress line
after each step. Then read back by id: the issue in the bound project with
its title, body markers and independence line; every added relation present;
every removed relation absent; and the issue's full set of `blocks` edges, in
both directions, equal to its `swarm-deps` marker. An edge another writer
added meanwhile makes the sets differ: the operation reports it as
`declared_edges` DRIFT with exit 3 rather than claiming a clean result. After the read-back, re-run cycle detection
over the same scope, because a pre-write check races concurrent edits; a
cycle found now is reported as DRIFT with exit 3 and is not undone
automatically.

An operation whose progress log does not end in a confirmed read-back is
incomplete. PR 1's audit gains two checks. `declared_edges`, which needs no
local state: an issue whose `swarm-deps` marker lists an edge Linear does
not hold, or holds a `blocks` edge its marker omits, is DRIFT, so a
partially filed issue reads as DRIFT from any host. `op_incomplete`, local
and supplementary: an incomplete operation record on this host for the
audited project is DRIFT, naming the operation and `replay`.

Two hosts filing at once can each pass the pre-write cycle check and
together close a cycle; the post-write check and the audit's `cycle` check
report it and nothing undoes it automatically. That is the same
owner-accepted boundary as PR 2's drain.

The per-project lock from PR 2 serializes `issue`, `drain` and other writers
on one host.

## Consumers and docs

`hanig-orchestrate`'s `tracker.dag` declaration says ad-hoc issues are filed
and edited only through `linear_sync.py issue`, with relations declared at
filing, and that prose dependencies are not a substitute. `hanig-project`'s
tracker references and README describe the command. CLAUDE.md's
network-boundary paragraph names it.

## Acceptance criteria

1. `new` refuses, before any network call, a missing approver and a request
   with neither dependencies nor an independence reason, or with both.
2. `new` creates the issue and relations under derived ids, reads them back,
   and a replay or re-run of the same operation creates nothing new; a
   rejected create whose object lacks the operation marker stops.
3. A crash after any step before the remote state matches the
   specification (simulated after each) leaves an issue the audit reports
   as `declared_edges` DRIFT from a host without the record; a crash after
   the remote state already matches but before confirmation is logged can
   read CLEAN remotely, and is reported as `op_incomplete` DRIFT on the host
   with the record; `replay` completes it
   without duplicating anything, treats externally satisfied steps as done,
   refuses a tampered specification by digest, and reports a deleted issue
   without recreating it. Replay of a confirmed operation changes nothing
   and reports later divergence as DRIFT; replay of an incomplete one
   refuses when a managed value matches neither its before nor its desired
   value.
3a. Every counterpart in the touched set is pre-checked and read back; a
   marked counterpart outside the bound project refuses before mutation.
4. Structured dependency lines that disagree with the resulting relations
   refuse; the same text inside a code block or blockquote, or with other
   words on the line, does not; prose candidates warn only. A body
   containing the key is refused with nothing written to disk.
4a. Filing an issue blocked by a marked issue, and removing an edge from
   either side, leaves both endpoints' `swarm-deps` trailers matching
   Linear, so `declared_edges` stays CLEAN.
5. A request whose resulting graph has a cycle refuses before mutation,
   while reversing an edge in one edit is accepted; a cycle that appears
   after the write is reported DRIFT with exit 3.
6. `edit` adds and removes relations, changes title, body and independence,
   refuses an issue outside the bound project, refuses removing an open
   issue's last relation without `--independent`, and accepts a body whose
   structured lines describe unchanged existing relations.
7. Bodies are read from a file or stdin, never argv. Title or body content
   containing the loaded key is refused before mutation; the program never
   puts the key in output, a written file, or the argv or environment of a
   child other than `linear_sync.py`.
8. Every network call goes through `linear_api.transport`; swarm.py,
   tickets.py and drain_contract.py import no network module.
9. Live check before merge, owner-authorized: one throwaway issue filed with
   one `--blocked-by` relation to a second throwaway issue, read back, the
   audit showing it CLEAN on `relationless`, a replay changing nothing, then
   both canceled.

## Not in this PR

`tickets.json` filing (PR 4), waves and the MCP deny (PR 5), migrating the
relation-less backlog, and any automatic undo.
