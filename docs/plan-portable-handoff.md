# Plan — hanig-portable-handoff

Capture and restore durable research state across machines, clusters and
sessions. Two commands and a rule.

## What it is for

Context switching between Arc, UCSF and three HPCs, which is the stated pain
point. And a second thing this session proved necessary: a fresh session
rebuilds retired designs. Fifteen limits and three retired rules are written in
`MEMORY.md` precisely because I re-derived the same timestamp-attribution
mistake three times inside one session with those notes in front of me.

## What it does NOT do

- **No data movement.** A handoff is pointers plus identities. Copying a
  checkpoint directory is not this tool's job and would make it unusable.
- **No content-identity staleness.** Retired in `hanig-verified-workflow` and
  the reason carries: a deterministic pipeline regenerating identical bytes is
  the SUCCESS case. A handoff may report that bytes match; it may not conclude
  from that that work was skipped.
- **No environment dump.** Credential-shaped names are redacted, values never
  recorded.

## `handoff capture <run_dir...> --out handoff.json`

Deterministic, no model judgment:

- **Code identity**: repo URL, commit, branch, dirty-diff sha256, changed paths.
  Reuses `contract.py`'s `repo_state()` rather than a second implementation.
- **Contract state**: for each run dir given, the contract, its verdict from the
  existing `verification.json` receipt, and the exit code that receipt carries.
  It does NOT re-run `check`: a handoff records what was decided, not a fresh
  opinion, and re-running would silently change the recorded state.
- **Scheduler links**: job ids from `attempts.jsonl` / `training-binding.json`.
- **Artifact pointers**: declared outputs, metrics path, checkpoint dir, with
  existence, size and mtime. Paths only.
- **Host identity**: hostname, user, python, cluster hints, filesystem of each
  pointer, so a resume can say "this path is on lambda's /scratch and you are
  on andromeda".
- **Unresolved**: every run dir whose recorded verdict is non-zero, listed
  first, because that is what a returning reader needs.

## `handoff resume <handoff.json>`

Compares this machine against the record and **reports mismatches; never
silently continues**. Exit codes, mirroring the verifiers' state machines:

| code | state | meaning |
|---|---|---|
| 0 | `HANDOFF_CLEAN` | code identity matches, every pointer resolves |
| 1 | `HANDOFF_DRIFTED` | code or inputs differ — enumerated, not summarised |
| 2 | `HANDOFF_UNREACHABLE` | pointers exist but not from this host |
| 3 | `HANDOFF_MALFORMED` | the handoff file itself is unusable |

Never a boolean. Same reason the verifiers aren't: "it differs" and "I cannot
see it from here" need different actions.

## The MEMORY.md rule, automated

`handoff memory <project_dir>` regenerates the FACTUAL sections in place,
between explicit markers, and leaves everything outside them untouched:

- git log since the last update, changed files
- open contracts and their current recorded verdicts
- unresolved items

The judgment sections — decisions, blockers, recommendations — are never
written by this command. It emits them as headings with the previous content
preserved. Every factual error in a memory file is a transcription failure and
those are automatable; judgment is not.

## Acceptance criteria

1. `capture` never reads a file's contents except to hash a declared input, and
   never copies one.
2. `capture` records the verdict from an existing receipt and never re-runs
   `check`.
3. A pointer that does not resolve is recorded as unresolved WITH its reason,
   not dropped.
4. `resume` distinguishes drift from unreachability, and enumerates every
   mismatch rather than reporting a count.
5. No environment variable value is ever written; names matching a credential
   pattern are redacted from the names list too.
6. `memory` rewrites only between markers, is idempotent, and never touches a
   judgment section. Running it twice on an unchanged repo produces no diff.
7. Every refusal names an action, per `tests/test_symmetry.py`.
8. Python 3.7+ stdlib only, POSIX sh, no non-stdlib import.
9. A handoff written on one host and resumed on another reports the host
   difference explicitly, even when everything else matches.
10. No test fixture generates its own timestamps at check time — the trap that
    produced five defects this session.

## Out of scope
Data sync. Credential transfer. Anything requiring a daemon.
