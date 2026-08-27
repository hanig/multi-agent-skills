# Plan v6 — hanig-reproducible-result

Regenerate and verify figures, tables, benchmark results and exports with
input-to-output provenance.

## Three levels, never a boolean

| code | state | what has been established |
|---|---|---|
| 0 | `REVIEWED` | a named person accepted the scientific result |
| 1 | `VALIDATED` | structural and numeric checks pass, provenance recorded |
| 2 | `GENERATED` | the command succeeded and outputs exist |
| 3 | `STALE` | outputs predate their declared inputs |
| 4 | `NONDETERMINISTIC` | a second render differs where determinism was declared |
| 5 | `FAILED` | the build command failed |
| 6 | `INCOMPLETE_EVIDENCE` | cannot judge — a check could not run, or determinism was declared and never tested |
| 7 | `CONTRACT_DRIFTED` | the build consumed inputs that differ from the declared ones |

## Evaluation order, stated

Disqualifiers first, achievements last. v1 left this unstated and the table was
the only order present, which put `REVIEWED` first -- so a reviewed result whose
inputs later changed still exited 0, and the review survived the change it was
supposed to be invalidated by (kimi, CRITICAL):

```
STEP 0 -- TRUST GATE, before any disqualifier is evaluated:
0. INCOMPLETE_EVIDENCE  no attempt names this contract instance.
                        NOTHING from an unbound attempt is read: not its rc,
                        not its consumed digests, not its double-render
                        record. The gate is not a ranked disqualifier, it is
                        a precondition on reading the attempt at all.

DISQUALIFIERS, most serious first, over the BOUND attempt only:
1. FAILED               the bound attempt's build command failed
2. INCOMPLETE_EVIDENCE  a declared check could not run, OR the contract
                        declared deterministic: true and no double render was
                        ever recorded
3. NONDETERMINISTIC     a recorded double render differed
4. CONTRACT_DRIFTED     the bound attempt's consumed inputs differ from the
                        declared
5. STALE                the bound attempt's consumed inputs differ from the
                        inputs now

ACHIEVEMENT, highest reached:
6. GENERATED            outputs exist
7. VALIDATED            and every declared check passed
8. REVIEWED             and a named person accepted THESE digests
```

**The exit code carries the most serious DISQUALIFIER, or the highest
ACHIEVEMENT when there is none.** v3 said "the most serious finding" over a
single list, and since `REVIEWED`'s condition logically contains `VALIDATED`'s,
both held for a reviewed result and the rule picked `VALIDATED` -- exit 1 for
something fully reviewed, breaking the "0 means done" contract (kimi-k3). One
sentence, because the alternative is that semantics gets baked into tests and
consumers before anyone notices.

`deterministic: true` with no recorded double render is `INCOMPLETE_EVIDENCE`,
not a pass. v3 made the double render not-a-check, so the fail-closed rule for
checks did not reach it and a genuinely nondeterministic pipeline could be
reviewed with determinism never tested -- the only trace being
`double_render.ran: false` inside a receipt nobody reads (kimi-k3).

**`check` verifies the ATTEMPT names this contract instance** before trusting
its rc, its consumed digests or its double-render record. The receipt-binding
rule reached receipts and reviews and not attempts, so a build from a previous
contract could satisfy a re-declared one with no build ever run for it
(glm-5.1). Fifth place this same rule has had to be applied.

**The trust gate is STEP 0 and not disqualifier 2, because a ranking cannot
express it.** v5 listed `FAILED` first and folded "the attempt does not name
this contract instance" into disqualifier 2, so an attempt left over from a
previous contract with rc=1 satisfied `FAILED` before the trust rule was ever
consulted: `check` would report "the build failed" (exit 5) for a contract that
was never built, instead of `INCOMPLETE_EVIDENCE` (exit 6). The two states carry
opposite actions -- "fix your build" versus "run the build" -- so this is not a
cosmetic ordering point (deepseek-v4-pro, MAJOR). Binding is a precondition on
reading the attempt, so it cannot sit in a list of findings ranked against each
other. **Sixth place this same receipt-binding rule has had to be applied**,
which is itself the argument for making it a gate rather than a rule to
remember at each new site.

**These conditions OVERLAP, so this is a priority rule and not mutual
exclusivity**, which v1 claimed and could not deliver: a build can succeed and
its inputs be touched afterwards, satisfying both `GENERATED` and `STALE`
(kimi). So the receipt lists EVERY finding and the exit code carries the most
serious one. A user who fixes only what the exit code named can re-run and see
the next; a user who reads the receipt sees all of it at once.

**Ascending states are NOT ascending exit codes**, because 0 must mean "fully
done" for shell composition. `GENERATED` is exit 2: a figure that rendered is
not a figure that is right, and the shell must not treat it as success. The
number is a state, not a score.

**`REVIEWED` requires a name and cannot be inferred.** A regenerated figure is
`VALIDATED` at best. The receipt must never claim semantic correctness because
a PDF opened.

## What this repo has already learned, applied here

These are not speculative. Each cost real rounds in the two verifiers:

- **A timestamp supports an inference, never an attribution.** And a
  DECLARE-TIME digest is an attribution too, which v3 missed: using it as
  evidence of "what the build consumed" is the same error one level deeper, and
  a fresh review panel found it independently on both sides (kimi-k3, glm-5.1).
  It fails in both directions. False alarm: declare D0, build, the data is
  legitimately updated to D1, rebuild honestly -- `check` compares declared D0
  against current D1 and reports STALE forever, and no rebuild clears it.
  False pass: declare D0, build, edit the input to D1, build again so the
  outputs reflect D1, then `git checkout` the input back to D0 -- declared and
  current now agree, so the outputs pass as current although they were built
  from data that no longer exists.

  **So `build` records the digest of every input it actually consumed, in the
  attempt.** Two different questions, two different comparisons:

  | question | comparison | state |
  |---|---|---|
  | are the outputs from the current inputs? | attempt's consumed vs current | `STALE` |
  | was the build run against what was declared? | declared vs attempt's consumed | `CONTRACT_DRIFTED` |

  This is the fifth appearance of one lesson in this repo, and the first time it
  appeared as a digest standing in for an attribution rather than a timestamp.
  Cheap now, expensive once the attempt schema ships.

- **Old:** `STALE` was decided by comparing input digests recorded at declare
  time against the
  inputs as they stand now -- not by mtime. v2 specified the weaker signal while
  the stronger one was already being recorded two sections away, and it would
  have called an honest result stale whenever `git checkout` refreshed an input's
  mtime without changing a byte (kimi). mtime is reported as a note and decides
  nothing. This is the fourth time in this repo that a digest was captured and
  the comparison was done on a timestamp instead.
- **Content identity cannot prove staleness.** A deterministic pipeline
  regenerating byte-identical output is the SUCCESS case, so an unchanged digest
  is reported and never penalised. This retired a rule in `contract.py`.
- **Enumerate the good shape.** A validator lists what a figure must have and
  fails closed on anything unrecognised, rather than listing known-bad cases.
- **Every refusal names an action**, enforced by `tests/test_symmetry.py`.
- **A receipt names the contract instance it verified**, because a receipt left
  behind by a re-declaration is otherwise indistinguishable from a current one.
- **No boolean.** "The build failed", "it rendered but has three panels not
  four", and "nobody has looked at it" need different actions.

## `result.py declare <result_dir> --command CMD --output PATH... --input PATH...`

Writes `result-contract.json` BEFORE the build: command, source commit,
dirty-diff hash (with a scrubbed remote), environment, input digests, declared
outputs, declared checks, a per-instance `contract_id`, `created_at` and
`created_at_epoch`.

## What "consumed inputs" means, exactly

Criteria 11 and 17 both rest on "the digests the build recorded as consumed",
and v4 never said how that set is determined. Left open, an implementer could
record every file the command opened -- including system libraries, so the next
OS update makes an honest result permanently `STALE` and it can never reach
`REVIEWED` -- or use a narrow heuristic that misses a real input, letting
`CONTRACT_DRIFTED` go undetected (deepseek-v4-pro, MAJOR).

**The consumed set is exactly the DECLARED inputs, digested by `build` at the
moment it runs. The tool never traces, discovers, or infers file access.** No
`strace`, no `lsof`, no filesystem watching: none of it is stdlib-only, and all
of it would pull system files into the provenance set.

That gives three digest comparisons for the same path, all of them content
comparisons and none of them a time comparison:

| digest | taken at | disagreement means |
|---|---|---|
| declared | `declare` | -- |
| consumed | `build`, as it runs | vs declared: `CONTRACT_DRIFTED`, the inputs changed between declaring and building |
| current | `check` | vs consumed: `STALE`, the inputs changed after the build |

**The residual, stated rather than hidden: an input that was never declared is
invisible to this tool.** A build that secretly reads `~/scratch/fudge.csv`
reaches `REVIEWED` with no complaint. Provenance is exactly as good as the
declaration, and the receipt must say so in those words, next to the input list
it actually checked. This is a limit, not a defect to be fixed later: closing it
needs syscall tracing, which the stdlib cannot do and which would drag every
system library into the same trap described above.

## `result.py build <result_dir> [--double]`

Executes the declared command, bounded by a watchdog, capturing rc and output
tails. Records an attempt with `contract_id` and our own `submitted_at`. Never
decides more than `GENERATED`.

`--double` runs it a second time into a scratch directory and records whether
the declared outputs came out byte-identical. Running the command is `build`'s
job, which is why the double render lives here and not in `check`.

## `result.py check <result_dir> [--reference DIR] [--double-render]`

Declared checks, all optional and all fail-closed when they cannot run:

- **exists / min_size / non_empty** — the floor.
- **parses** — by extension: PDF (`%PDF` magic + `%%EOF`), PNG/JPEG magic, SVG
  root element, CSV/TSV (consistent column count), JSON, NPZ/NPY magic. An
  unknown extension is `INCOMPLETE_EVIDENCE`, not a pass.
- **schema** — for tabular output: required columns present, row count in a
  declared range, no all-NaN required column.
- **finite** — no NaN or infinity in declared numeric columns. This is the
  check that catches a broken pipeline that still renders.
- **panels** — a declared count of subplot-like objects for SVG (count `<g>`
  with a plot class) or a page count for PDF. Stated as a heuristic in the
  receipt, because it is one.
- **numeric comparison vs `--reference`** — per-column absolute or relative
  tolerance, declared in the contract, never inferred. A missing tolerance is a
  refusal, not a default of zero.
- **double render** — NOT a `check` option. v1 put it there while criterion 6
  said `check` never runs the build, which cannot both hold (kimi). It belongs
  to `build --double`, which runs the command twice into separate directories,
  compares digests, and records the outcome in the attempt. `check` then READS
  that record. Differing output where the contract declared
  `deterministic: true` is `NONDETERMINISTIC`, a distinct state because the
  action is "seed it, or declare the pipeline non-deterministic", not "re-run".

## The receipt

`result-verification.json`, and the schema matters because three findings came
from leaving it unstated (deepseek, twice; kimi):

```json
{
  "schema_version": 1,
  "checked_at": "...",
  "contract_id": "...",          // WHICH instance this judges
  "criteria_digest": "...",
  "state": "STALE",              // the most serious finding
  "exit_code": 3,
  "findings": [                  // EVERY finding, not just the decisive one
    {"state": "STALE",     "reason": "...", "action": "..."},
    {"state": "GENERATED", "reason": "...", "action": null}
  ],
  "outputs": [{"path": "...", "sha256": "...", "size": 0, "mtime": 0}],
  "double_render": {"ran": true, "identical": false, "differing": ["fig1.pdf"]},
  "review": {"by": "...", "at": "...", "accepted_digests": {"fig1.pdf": "..."}},
  "currency": "This receipt is a claim about the moment `check` ran."
}
```

Every finding carries its own `action`, which is where "every refusal names an
action" actually lives: v2 asserted the requirement without saying where the
string goes, so an implementer could have printed it to stderr and left the
receipt unactionable.

`build --double` writes its outcome into the attempt record as
`double_render: {"identical": bool, "differing": [paths]}`, and `check` reads it
from there. v2 said "records the outcome" without naming the location, so
`check` had no defined path to read and could have skipped nondeterminism
detection silently (deepseek).

## `result.py review <result_dir> --by NAME --note TEXT`

The only way to reach `REVIEWED`. Records who, when, their note, **the
`contract_id` they reviewed**, and **the digest of every declared output as it
stood when they accepted it**.

The `contract_id` is not optional and not cosmetic: without it, re-declaring in
the same directory leaves the old review file in place, and if the new build
happens to produce matching digests then `check` reports `REVIEWED` with the
previous reviewer's name for a contract they never saw (kimi). That is the same
receipt-binding defect this repo already fixed in both verifiers and in the
handoff -- the fourth appearance of one lesson. Refuses
below `VALIDATED`: you cannot accept a result that does not pass its own checks.

**A review record on disk cannot know the world changed.** deepseek: if an
output is edited and `check` is never run again, the receipt still says
`REVIEWED` and a downstream reader trusts it. No file can self-invalidate, so
this is stated rather than pretended away:

- the review records the digests it accepted, so verifying it is cheap and
  local;
- `check` recomputes them and reports `REVIEWED` only when they still match,
  naming the ones that do not when they differ;
- **the receipt is a claim about a moment, and `check` is what makes it current.**
  A consumer trusting a receipt without running `check` is trusting a
  photograph. Said in the receipt itself, not only here.

## Acceptance criteria (24)

1. `REVIEWED` is reachable only through `review`, and only from `VALIDATED`.
2. A review is invalidated by any subsequent change to the digests it recorded,
   named by digest rather than by time, and `check` is what detects it. The
   receipt states that it is current only as of the last `check`.
3. Exit 0 means `REVIEWED` and nothing else.
4. An unknown output type is `INCOMPLETE_EVIDENCE`, never a pass.
5. A missing tolerance for a numeric comparison is a refusal, not zero.
6. `check` never runs the build, including for a double render, which is
   `build --double`. `build` never validates beyond existence.
7. An unchanged output digest is reported, never penalised.
8. Every state is reachable. Conditions overlap, so evaluation follows the
   stated priority order with disqualifiers first, and the receipt lists every
   finding while the exit code carries the most serious.
9. Every refusal names an action.
10. The receipt names the contract instance and the digests it judged, and
   carries an `action` string on every finding.
11. `STALE` is decided by the digests the BUILD recorded as consumed,
   compared against the inputs as they stand now. Never by mtime, and
   never by the declared digests: a declare-time digest standing in for
   what the build used is an attribution, and it false-alarms an honest
   rebuild while false-passing an edit-build-revert.
12. A review names the `contract_id` it accepted, and a review for another
   instance is ignored with a stated reason.
13. The double-render outcome lives in the attempt record under a named field
   that `check` reads; `check` never runs the build to obtain it.
14. Python 3.7+ stdlib only. No PDF/image library: magic bytes and structure
    only, and the receipt says that is what was checked.
15. No test fixture generates a timestamp at check time.
16. Helpers copied from a sibling are byte-identical and exercised by
    `tests/test_symmetry.py`.
17. `build` records the digest of every input it consumed, in the attempt.
18. Declared-versus-consumed disagreement is `CONTRACT_DRIFTED`, its own state,
   because the action is "re-declare, or restore the inputs" and not "rebuild".
19. `deterministic: true` with no recorded double render is
   `INCOMPLETE_EVIDENCE`, never a pass.
20. `check` refuses to trust an attempt that does not name this contract
   instance -- its rc, its consumed digests and its double-render record alike.
   This is evaluated as STEP 0, BEFORE any disqualifier including `FAILED`. An
   unbound attempt yields `INCOMPLETE_EVIDENCE`, never `FAILED`, because the
   actions differ: "run the build" is not "fix your build".
21. The exit code carries the most serious DISQUALIFIER, or the highest
   ACHIEVEMENT when there is none. Achievements are never compared against
   disqualifiers, because `REVIEWED` logically contains `VALIDATED` and a single
   ranking made a fully reviewed result exit 1.

22. Every declared check enumerates the keys it is read with, and a key the
   check does not read is REFUSED at `declare` rather than ignored. v4 said
   checks fail closed when they *cannot run*, which does not reach a typo: a
   misspelled `min_sze` was simply not a check, so the intended verification
   never ran and the result could reach `VALIDATED` and then `REVIEWED` with
   the user believing it had been applied (deepseek-v4-pro, MAJOR). Carries the
   defect the siblings hit by real use, plus its fix: underscore-prefixed keys
   are annotations and ignored, EXCEPT an underscored form of a key the check
   actually reads, which is refused as a typo naming the key it resembles.
   Without that exception the hatch reintroduces the defect one level deeper.
23. The receipt records the outcome of EVERY declared check by name: passed,
   failed, or could-not-run with the reason. A check that could not run is
   distinguishable in the receipt from one that ran and failed, because the
   actions differ.
24. The consumed-input set is exactly the declared inputs, digested at build
   time. The tool never traces file access, and the receipt states that an
   undeclared input is not covered.

## Out of scope
Visual design, narrative, plot aesthetics. Rendering engines. Anything needing
a non-stdlib parser to decide a verdict.
