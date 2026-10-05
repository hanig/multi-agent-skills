# Plan: PR 1, Linear binding, read-only audit, audit-backed reporting

Slices 1 and 2 of `docs/plan-linear-dag-control.md` (owner-approved
2026-10-05). This PR makes no Linear mutation anywhere. It replaces "the
orchestrator says Linear is in sync" with a structured, timestamped audit
that report producers render, and nothing renders a clean tracker section
without one.

## Files

- `skills/hanig-project/scripts/linear_api.py`: brought from the unmerged
  `hanig/linear-api` branch: stdlib GraphQL client, one `transport()` seam,
  key from `LINEAR_API_KEY` or `~/.config/hanig/linear.env` parsed with shell
  word splitting (quotes and trailing comments). Redaction applies to output
  streams and error text only, never to data files (D2: this program never
  copies the key it loads; a key an operator placed elsewhere is a declared
  limit).
- `skills/hanig-project/scripts/linear_sync.py`: network-capable, read-only in
  this PR, with subcommands `bind`, `audit` and `section`. Later PRs add
  `drain`, `file` and `issue` to the same program.
- `skills/hanig-project/scripts/tracker_audit.py`: offline. Validates an audit
  record and renders the tracker section. Imported by `linear_sync.py
  section`, `report.py` and `merge_unit.py`, so all three apply one rule.
- `skills/hanig-swarm/scripts/child_environment.py`: add `LINEAR_API_KEY` to
  the exact-name strip list.
- `report.py`, `merge_unit.py`, the `hanig-project` and `hanig-orchestrate`
  declarations and references, `docs/tracker-outbox.md`, CLAUDE.md.

`swarm.py`, `tickets.py` and `drain_contract.py` stay network-free and import
neither new network module.

## Binding

`.hanig/linear-binding.json`, committed at the repository root, binds a
repository's ad-hoc issues to one Linear project:

```json
{"schema_version": 1,
 "repository": "OWNER/REPO",
 "workspace": {"id": "<uuid>", "name": "..."},
 "team": {"id": "<uuid>", "key": "ARC"},
 "project": {"id": "<uuid>", "name": "..."}}
```

`linear_sync.py bind --project ID --repository OWNER/REPO` reads the project,
its team and the viewer's organization from Linear and writes the file. The
names are for humans; every decision uses ids. For a swarm plan the binding
comes instead from `tickets.json` (`project.linear_id`, team) plus
`plan.json`, passed as `--draft` and `--plan`.

Before any read is trusted, `audit` checks that the viewer's organization id
equals `workspace.id` and that the project exists and belongs to `team.id`. A
mismatch is DRIFT on the `binding` check; a failed read is UNKNOWN.

The audit does not compare `repository` with the checkout's git remotes. The
binding file is committed in the repository it describes, so a checkout's
binding is that repository's by construction, and a remote comparison would
misjudge forks, case-insensitive forge paths and remotes changed after the
read. `repository` is recorded in the scope for people and for the markers
later PRs write; no verdict depends on it.

## Audit

`linear_sync.py audit (--binding FILE | --draft tickets.json) [--plan
plan.json] [--state-dir DIR] [--out FILE]` writes one JSON record:

```json
{"schema_version": 1, "verdict": "CLEAN|DRIFT|UNKNOWN",
 "read_started": "<ISO 8601 UTC>", "read_finished": "<ISO 8601 UTC>",
 "scope": {"workspace": "<id>", "team": "<id>", "project": "<id>",
           "repository": "OWNER/REPO or null", "plan": "name or null"},
 "inputs": {"<path>": "<sha256 of bytes>"},
 "coverage": {"complete": true, "pages": 3, "issues": 145},
 "checks": [{"id": "...", "verdict": "CLEAN|DRIFT|UNKNOWN|ADVISORY",
             "evidence": [...]}]}
```

`inputs` records every local source the arguments name (binding, draft,
plan, coordinator state, outbox, receipts), each with the sha256 of its bytes
or `null` when it was absent; the set of sources is fixed by the arguments,
not by which files happened to exist. The record verdict is UNKNOWN if any
check is UNKNOWN, else DRIFT if any is DRIFT, else CLEAN; ADVISORY never
changes it. Exit codes: 0 CLEAN, 1 DRIFT, 3 UNKNOWN, 2 usage or configuration
error. The key never appears in the record.

**Read stability.** The audit pages the project's issues with their relations
and inverse relations. A repeated cursor, a duplicate issue id, a nested
relation page it could not finish, a rate-limit or transport error is
`coverage.complete: false` and the `coverage` check is UNKNOWN. After the
main read it re-reads the `updatedAt` of every issue the verdict depended
on: the project's issues, every blocker outside the project, every issue
found by the marker search and every issue fetched by identifier. Any
difference from the first read means the snapshot moved under it, and
`coverage` is UNKNOWN. A paginated read is not an atomic snapshot, and the
record claims only its read interval.

**Checks.**

| id | DRIFT when | UNKNOWN when |
|---|---|---|
| `binding` | organization, team or project ids disagree | a binding read fails |
| `coverage` | | incomplete or moved read (above) |
| `misplaced` | an issue known to belong here is outside the bound project; known means a draft identifier, an outbox receipt ref, or a `swarm-unit:` marker for this plan, and each draft identifier and receipt ref is fetched by identifier, and issues whose description contains `swarm-unit: <plan>/` are searched for across the whole workspace (no team filter) with a description filter; neither relies on the project listing, and the marker search pages under the same coverage rules | such an issue cannot be read |
| `relationless` | an open AD-HOC issue in the project (one not mapped to a plan unit by a draft identifier or a `swarm-unit:` marker) has no relation and no `swarm-independent: <reason>` line; a unit's issue is judged by `plan_edges` instead, so a unit with `needs: []` and no relation is CLEAN | |
| `prose_dependency` | ADVISORY only: an identifier within one sentence after "blocked by", "depends on" or "must land before", outside code spans, quotes and lines starting with a negation or "historically", with no relation to that issue; the sentence and proposed direction are listed | |
| `cycle` | the `blocks` graph over project issues has a cycle | |
| `blocked_in_progress` | an issue in a `started` state has a blocker that is open | a blocker is canceled, duplicate, archived or unreadable |
| `plan_edges` | (whenever `--plan` is given, which also requires `--draft`) the plan's `needs` between filed units differ from Linear's `blocks` edges between their issues, in either direction; a `related` relation does not count; listed per edge | a unit's issue cannot be read |
| `swarm_state` | (with `--state-dir`) a unit's issue state contradicts coordinator state; listed per unit | coordinator state cannot be read |

A record whose `scope.plan` is set always carries a `plan_edges` check;
`render` treats its absence as INVALID.

Prerequisite-completion policy: a blocker in a `completed` state satisfies
it; canceled, duplicate and archived are UNKNOWN until a person decides; any
other state is open. Coordinator state is read through `swarm.py status
--json` in a subprocess whose environment lacks `LINEAR_API_KEY`. Coordinator
state rules for `swarm_state`: DONE wants a `completed` issue; SUBMITTED and
RUNNING want `started`; FAILED, FAILED_EVIDENCE, HELD and NEEDS_HUMAN want
anything but `completed`; other states are not compared.

The 34 relation-less issues measured on 2026-10-05 will make the first audit
of the skills project DRIFT. That is the intended result: the migration queue
in the parent plan clears it, through the existing write path until PR 3
replaces it.

## The tracker section

`tracker_audit.render(record, current_inputs, now, max_age)` is the one
renderer. It returns a section that is clean only when the record is
schema-valid, its verdict is CLEAN, `coverage.complete` is true, every source
in `record.inputs` is in the same state now (same digest, or still absent
when recorded `null`), the current invocation names the same set of sources, and `read_finished` is
no older than `max_age` (default 15 minutes). Otherwise the section says
which condition failed (`NO AUDIT`, `INVALID`, `STALE: <path> changed`,
`STALE: read <age> ago`, `INCOMPLETE`, `DRIFT: <n> finding(s)`, `UNKNOWN:
<check>`), and never says "in sync". The clean form is: "Linear consistent
with <scope> as read <read_started> to <read_finished>". A write this program
made would change an input or postdate the record; an external write after
`read_finished` is outside what the section claims, and the section says so.

Consumers:

- `linear_sync.py section --audit FILE [same inputs]` prints the section for
  the orchestrator's three-part report.
- `report.py --tracker-audit FILE` renders it in the Tracker section; without
  the option the Tracker section says `Tracker: UNKNOWN (no audit)` wherever
  it currently implies a state.
- `merge_unit.py`, after advance and the pending-close print, runs
  `linear_sync.py audit` then `section` when a binding or draft is found and
  a key is available (the operator's own environment; this is not a
  coordinator child), and prints the section. A missing binding, missing key
  or failed audit prints `Tracker: UNAVAILABLE (<reason>)`. The audit never
  changes merge_unit's exit code and never blocks a merge.

`hanig-orchestrate`'s `report.three-parts` declaration changes so part (2) is
the output of `linear_sync.py section`, quoted verbatim, and `tracker.dag`
names `audit` as the check. A conversational claim cannot be stopped by
repository code; the generated report and these commands are what this PR
guarantees.

## Acceptance criteria

1. Every check above has a test producing each of its verdicts through a
   fake `transport` (including a swarm unit with `needs: []` and no relation,
   which is CLEAN; a moved draft identifier, which is `misplaced` DRIFT; and
   `--plan` without `--state-dir`, which still runs `plan_edges`), and the record verdict follows the precedence rule.
1a. `binding` is DRIFT for a wrong organization, team or project and never
   reads a git remote; a fork checkout with a valid binding is CLEAN on it.
   An external blocker whose state changes during the read makes `coverage`
   UNKNOWN.
1b. A source absent at audit time and present at render time renders
   `STALE`.
2. A repeated cursor, a duplicate id, an unfinished relation page, a transport
   error and a moved snapshot each yield `coverage` UNKNOWN and exit 3.
3. Binding ids are compared exactly; no case folding or prefix matching.
4. `render` returns a clean section only under every condition above; tests
   drive `linear_sync.py section`, `report.py` and `merge_unit.py` with an
   absent, invalid, stale-by-input, stale-by-age, incomplete, DRIFT and
   UNKNOWN record, and none is clean. Removing any single condition fails a
   test (mutation-checked).
5. `merge_unit.py` with no key, no binding, or a failing audit prints
   UNAVAILABLE and exits exactly as it does today.
6. No mutation: an AST test asserts `linear_sync.py` in this PR sends no
   GraphQL document beginning with `mutation`, and the fake transport fails a
   test on any mutation.
7. The key never appears in stdout, stderr or the audit record. The only
   subprocess that receives `LINEAR_API_KEY` is `linear_sync.py` itself,
   started by `merge_unit.py` with the operator's environment because it
   needs the key; every other child (`swarm.py` from `linear_sync.py`, and
   every coordinator child) runs without it, and tests assert both halves.
   `LINEAR_API_KEY` is in `DENIED_ENV_NAMES`.
8. `swarm.py`, `tickets.py` and `drain_contract.py` import no network module;
   every network call goes through `linear_api.transport`.
9. Live check before merge: `audit` against the skills project from this
   host, recorded in the PR with its verdict, read interval and check counts
   (no identifiers beyond issue keys).

## Not in this PR

Draining, filing, the `issue` command, waves, the MCP deny, identity
namespacing of derived ids, and the migration of relation-less issues.
