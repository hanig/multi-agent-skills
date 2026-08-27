# Plan v4 — hanig-portable-handoff

Capture and restore durable research state across machines, clusters and
sessions. Two commands and a rule.

Two plan reviews before any code existed. v1 drew six findings, one of which
was a defect in the two SHIPPED verifiers rather than in the plan: their
receipts carried no `contract_id`, so `capture` reading a receipt left behind by
`init --force` would have recorded a pass for a contract never verified. Fixed
in `e65595a`.

v2 drew three more, all of them contradictions I introduced while fixing v1 --
the CLEAN row still said "same host" while a new criterion said a host
difference is CLEAN; a criterion forbade recording "environment values" while
the design records hostname and user; and the sha boundary did not actually
make `memory` idempotent. That is two plan revisions in which fixing a tension
created another, which is the argument for reviewing a plan more than once.

## What it is for

Context switching between Arc, UCSF and three HPCs, the stated pain point. And
a second thing this repo proved: a fresh session rebuilds retired designs.
Fifteen limits and three retired rules are written down precisely because the
same timestamp-attribution mistake was re-derived three times inside one
session with those notes present.

## What it does NOT do

- **No data movement.** A handoff is pointers plus identities.
- **No content-identity staleness.** Retired in `hanig-verified-workflow`: a
  deterministic pipeline regenerating identical bytes is the SUCCESS case. A
  handoff may report that bytes match; it may never conclude work was skipped.
- **No new environment capture.** See criterion 5, which v1 stated wrongly.

## `handoff capture <run_dir...> --out handoff.json`

Deterministic, no model judgment.

- **Code identity**: repo URL, commit, branch, dirty-diff sha256, changed paths,
  via `contract.py`'s `repo_state()` rather than a second implementation.
- **Contract state**: the contract, and the verdict from the existing receipt
  **only when that receipt names this contract instance**. A receipt whose
  `contract_id` differs is recorded as `verdict: null, reason:
  "receipt belongs to a different contract instance"` — never as a verdict, and
  never dropped. `capture` does NOT re-run `check`: a handoff records what was
  decided, and re-running would silently replace the state being preserved.
- **Scheduler links**: job ids from `attempts.jsonl` / `training-binding.json`.
- **Artifact pointers**: path, exists, size, mtime, and the filesystem each
  lives on. Paths only, contents never read.
- **Host identity**: hostname, user, python, cluster hint.
- **Unresolved first**: every run dir whose recorded verdict is non-zero OR
  whose receipt could not be attributed, listed before anything else.

## `handoff resume <handoff.json> [--base DIR]`

**Pointers are recorded absolute and resolved absolute.** `capture` resolves
every path before writing it, so `resume` needs no cwd and cannot be fooled by
being run from elsewhere. v3 left this unstated, and a relative pointer would
have made an artifact that exists look like it was on another host — reporting
`ELSEWHERE` and telling the user to change machines (kimi).

`--base DIR` re-roots the recorded paths for a tree that legitimately moved,
e.g. `/scratch/alice/run1` on lambda appearing at `/data/alice/run1` elsewhere.
A relative pointer in a handoff file is `HANDOFF_MALFORMED`, not `ELSEWHERE`:
the file was written wrong, and telling the user to switch hosts would send
them somewhere the problem is not.


Compares this machine against the record and **enumerates every mismatch**;
never silently continues, never reports a count in place of a list.

| code | state | meaning | action it implies |
|---|---|---|---|
| 0 | `HANDOFF_CLEAN` | code AND input identity match, every pointer resolves from here at the recorded size | continue |
| 1 | `HANDOFF_DRIFTED` | code or input identity differs, or a pointer's size differs | reconcile before running |
| 2 | `HANDOFF_ELSEWHERE` | code matches; some pointer is not reachable from here | go to that host, or re-point |
| 3 | `HANDOFF_MALFORMED` | the handoff file itself is unusable | re-capture |

The states are checked in that order and the first match wins, so they are
exclusive by construction rather than by hoping the conditions do not overlap.
v3's CLEAN row named only code and pointers while DRIFTED named inputs too, so
a changed contract satisfied both and an implementer could exit 0 and run with
the wrong input specification (deepseek).

`HANDOFF_ELSEWHERE` replaces v1's `HANDOFF_UNREACHABLE`, whose stated meaning
("pointers exist but not from this host") could not distinguish a path missing
locally from a path on another cluster — a reviewer showed an implementation
could classify neither (deepseek). One state, one question: *is the code the
same, and can I see the artifacts from here?*

**A pure host difference is not drift.** The CLEAN row above says nothing about
the host for this reason; v2's did, contradicting the criterion below it. A handoff captured on lambda and
resumed on andromeda with the same commit and a shared filesystem is
`HANDOFF_CLEAN` and says the host differs in its output. v1's criterion 9
demanded the report while its table offered only CLEAN (which suppresses it) or
DRIFTED (which prescribes the wrong action) — a criterion in tension with the
design, which is the fault v1 added a criterion to prevent (kimi).

**mtime alone is never drift.** A checkpoint copied or restored with fresh
mtimes and unchanged bytes must not prompt a re-run (kimi). Pointer mtimes are
recorded and reported but never feed the verdict.

**A size difference IS drift**, and v3 said size feeds the verdict without
saying into which state, leaving an implementer to guess (deepseek). A pointer
that resolves at a different size is `HANDOFF_DRIFTED`: the artifact is not the
one that was captured, which is the same kind of fact as a changed commit.

## `handoff memory <project_dir>`

Regenerates the FACTUAL sections of `MEMORY.md` in place, between explicit
markers, leaving everything outside them byte-identical.

**Scoped by commit, never by time.** v1 said "git log since the last update",
which is the timestamp-as-boundary mistake this repo retired three rules over:
restoring a file from backup, or clock skew, silently omits or repeats commits,
and stamping a fresh time each run makes idempotence impossible (kimi). The
markers instead carry the **commit sha** the factual sections were generated
from, and the range is `<recorded sha>..HEAD`. A sha is an identity; a
timestamp is an inference.

- Emits: commits in that range, changed files, open contracts and their
  attributed verdicts, unresolved items.
- Never writes: decisions, blockers, recommendations. Those headings are
  preserved with their existing content untouched.
- Idempotent by construction: same HEAD, same recorded sha, no diff.

## Acceptance criteria

1. `capture` reads **only** `contract.json`, `verification.json`,
   `attempts.jsonl`, `training-*.json` and git metadata. It stats artifact
   pointers and never opens them. (v1 said "never reads a file's contents",
   which contradicted criterion 2 — the exact tension it was meant to prevent.)
2. A receipt is recorded as a verdict only when its `contract_id` matches the
   contract's; otherwise the verdict is null with a stated reason.
3. `capture` never runs `check`.
4. A pointer that does not resolve is recorded with its reason, never dropped.
5. `handoff` records **host identity** — hostname, user, python version,
   cluster hint — and **no environment variable values of its own**. Those are
   different things and v2 conflated them.

   For the captured contract, comparison and display are **separated**, which
   is what resolves the contradiction v3 still carried: carrying values as-is
   leaks a credential, and redacting them makes the recorded contract differ
   from the real one so an honest resume reports drift (kimi). So the handoff
   records a **digest of the original contract** for comparison, and a
   **redacted copy** for reading. Drift is decided on the digest, which is
   computed before redaction and is therefore faithful; nothing a human or a
   log ever sees carries a credential-shaped value. Compare identities, show
   redactions — the same principle as everywhere else here.
6. `resume` distinguishes drift from elsewhere-ness, and lists every mismatch.
7. A pure host difference is `HANDOFF_CLEAN` with the difference reported.
8. mtime differences alone never change the verdict.
9. `memory` is scoped by commit sha, rewrites only between markers, and never
   touches a judgment section. It is **deterministic given its inputs**, which
   are HEAD, the recorded sha, and the contract receipts on disk — not
   "idempotent on an unchanged repo", which v2 claimed and could not deliver:
   the factual section includes receipt verdicts read from the working tree, so
   an uncommitted change to one legitimately changes the output (kimi). Running
   it twice with all three inputs unchanged produces no diff.
10. `memory` verifies the recorded sha is an ancestor of HEAD before using
   `<sha>..HEAD`. After a rebase it is not, and that range then returns the
   whole branch history (kimi). When ancestry fails it regenerates the full
   factual section and says why, rather than emitting a silently wrong range.
11. Every refusal names an action, enforced by `tests/test_symmetry.py`.
12. Python 3.7+ stdlib only.
13. No test fixture generates a timestamp at check time.

## Out of scope
Data sync. Credential transfer. Any daemon.
