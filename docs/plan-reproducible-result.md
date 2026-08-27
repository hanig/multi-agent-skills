# Plan v3 — hanig-reproducible-result

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
| 6 | `INCOMPLETE_EVIDENCE` | cannot judge — a check could not run |

## Evaluation order, stated

Disqualifiers first, achievements last. v1 left this unstated and the table was
the only order present, which put `REVIEWED` first -- so a reviewed result whose
inputs later changed still exited 0, and the review survived the change it was
supposed to be invalidated by (kimi, CRITICAL):

```
1. FAILED               the build command failed
2. INCOMPLETE_EVIDENCE  a declared check could not run
3. NONDETERMINISTIC     a recorded double render differed
4. STALE                an output predates a declared input
5. GENERATED            outputs exist but a declared check did not pass
6. VALIDATED            every declared check passed
7. REVIEWED             and a named person accepted THESE digests
```

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

- **A timestamp supports an inference, never an attribution.** So `STALE` is
  decided by comparing **input DIGESTS** recorded at declare time against the
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

## Acceptance criteria

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
11. `STALE` is decided by input digests, never by mtime. mtime is reported and
   decides nothing.
12. A review names the `contract_id` it accepted, and a review for another
   instance is ignored with a stated reason.
13. The double-render outcome lives in the attempt record under a named field
   that `check` reads; `check` never runs the build to obtain it.
14. Python 3.7+ stdlib only. No PDF/image library: magic bytes and structure
    only, and the receipt says that is what was checked.
15. No test fixture generates a timestamp at check time.
16. Helpers copied from a sibling are byte-identical and exercised by
    `tests/test_symmetry.py`.

## Out of scope
Visual design, narrative, plot aesthetics. Rendering engines. Anything needing
a non-stdlib parser to decide a verdict.
