# ARC-720 validation and residual measurement

## Boundary

The object classified by `review.py` is auxiliary input documents: `--context`,
each `--file`, and disputed disposition fields forwarded to the panel. It is
not a classifier of whether an allegation about code is true, and it does not
cover every channel in a review request. Recognized signatures are stripped
before dispatch. Every unmatched decoded-character range stays unchanged.

The guard recognizes only the literal ARC-720 opening rerun annotation
(optional leading whitespace/BOMs) and entire JSON documents with the gate's
typed report envelope. It does not scan arbitrary English for decisions,
reject quoted state names, or recurse through unrelated JSON. Paraphrased
verdicts, reviewer attribution, standalone states, nested/fenced receipts,
other languages, zero-width and bidi controls, and encoded disclosures are
blind spots. Exact synthetic
receipts and opening annotations cannot be distinguished from actual history
and are stripped too. A receipt match removes the whole JSON value, including
facts inside it; the remainder is not a verbatim copy of that matching input.
Diffs, claims and threat-model text remain unscanned caller-trusted subjects.
Acceptance does not prove independence from history.

`tests/fixtures/arc720/README.md` records the live fixtures' provenance.
Tests drive `review.main` into an offline transport and inspect received
prompts, result receipts and the serialized audit append payload. A contaminated
positive control must demonstrate removal before clean-input acceptance. No
structural call-presence test stands in for the decision. Prompt, stderr,
result and audit notices identify source, kind, original character offsets and
removed character count without echoing the removed outcome. Visibility
exposes missing evidence; it cannot recover or assess that evidence.

## Offline reproduction

```sh
python3.10 -m unittest tests.test_arc720_contamination -v
python3.10 tests/mutate_arc720.py
TMPDIR=/var/tmp python3.10 -m unittest tests.test_review
TMPDIR=/var/tmp python3.10 -m unittest discover -s tests
```

### Historical refusal-design mutation binding audit

The alleged stale-module execution did not reproduce. Before changing the
driver, all original sixteen mutations still died at their intended assertions
(`.swarm/arc720/round2/original-sixteen.json`). A constant mutation changed the
observed CLI exit from 4 to 44, while the original module's constant stayed 4.
A function mutation disabling the refusal changed the exit from 4 to 0 and
delivered two prompts. Restoring the original returned exit 4 and no prompts.
The helper, main and error handler all resolved the selected module's globals.

There was a different weakness: the expected error code followed the mutated
constant, so that additional constant mutant survived. Exit expectations, the
counter-claim fixture and disposition digests now use independent oracles.
Both the constant and function mutants fail the context-refusal assertion.
Evidence: `binding-before.json`, `binding-after.json` and
`independent-oracles.json` under `.swarm/arc720/round2`; the latter records
seventeen intended kills, including all original sixteen. These historical
probes are scratch evidence, not a prerequisite for running the shipped tests.

The narrowed rule retires the broad English matcher and its attribution
mutations. The round-2 in-repo driver embedded constant/function execution
probes, required a green baseline and a mutation for every test method, and
reported **22 intended kills across 12 tests** in `narrow-mutations.json` under
that scratch directory. These are not claimed to be the same sixteen mutations.
Each acceptance mutation dies at the named acceptance assertion, not its
preceding positive control. Refusal mutations die at refusal assertions;
dropped evidence dies at prompt-delivery assertions. Errors or unrelated
failures do not count. No production files or deployed skills are mutated.

### Historical refusal-design finding reproduction

`python3.10 .swarm/arc720/round2/reproduce.py` compares saved pre-narrowing
production source against the current source through the same CLI consumer.
`reproductions-after.json` retains both sets of observations. Flaky-CI prose,
the protocol and README, a quoted-state table, source literals and nested
CI state data originally exited 4 without delivery; now they exit 0 with
two captured prompts. The original alleged JSON Schema refusal did **not**
reproduce: the exact schema was accepted before and after. The alleged
quoted README sentence was also accepted before; the table refusal did
reproduce. The actual gate receipt and issue annotation are refused before
and after. The supplied live allegation and disputed reason remain separate
verbatim-delivery fixtures, not invented replacements.

Discovery before narrowing, on 2026-09-27, reported 2579 runner tests, one skip,
a skill line-budget failure and 46 errors in `test_arc709_adjudication`.
All 46 errors were fixture refusals under the pre-existing `/tmp/.git` marker;
that marker was not removed or changed. Rerunning that module with
`TMPDIR=/var/tmp` passed all 46 tests. The skill guidance was consolidated to
respect its line budget. That discovery is not a clean full-suite pass and
is not presented as a test of the narrowed implementation.

## Residual experiment: five completed observations

The original ARC-713 pair changed its context between runs. It does not
measure sampling variance or establish a verdict distribution.

The owner ran the frozen-input experiment on 2026-09-27. Requested **N = 5**,
completed **N = 5**, distribution **{REVIEW_FAIL: 5}**. Every invocation had
the complete fixed panel, `luna` and `deepseek-v4-pro`, quorum 2, identical
argv, input and clean context. The observations span 12:37:45 through
13:00:33 UTC. Routing came from this worktree, not the installed copies.

This input fails decisively, so unanimous failure is expected and says little
about variability near a decision boundary, where the ARC-713 flip occurred.
It samples only the FAIL direction and cannot measure PASS-to-FAIL manufacture.
**Five identical verdicts do not establish that the gate is deterministic.**
It also measures the frozen pre-narrowing implementation, not the current patch.

Evidence is `.swarm/arc720/residual-live/manifest.json`, `summary.json`, and
`run-01.json` through `run-05.json`. The manifest digest is
`c01d30bbdbf05997e62abb63ec43851a24a71d0cdb21132ed4b3879f41b2d82a`.
`python3.10 .swarm/arc720/round2/audit_residual.py` verifies frozen file hashes,
manifest identity, every answering model, quorum and summary distribution;
its output is `residual-audit.json` in the same round2 scratch directory.
The original scratch runner `.swarm/arc720/run-residual.py` checks frozen
hashes before each invocation and never feeds a prior result into a prompt.
No fresh experiment was substituted for the owner's completed observations.

## Gate handoff

The round-2 command is `.swarm/arc720/round2/run-gate.sh`. It names the author
for exclusion, carries finding dispositions and an outcome-free context,
and retains the exact standalone honest-run counter-claim. Its local receipt
is separate from `.swarm/arc720/gate.json`. The command uses an unwritable
journal destination to keep writes in this worktree; stdout/stderr receipts
remain local and journal unavailability is non-gating. No installation or PR
is part of this handoff, and offline tests do not constitute a gate pass.

## Round three: stopped for committee

Observed on 2026-09-27 at 08:40:18 -0700 (15:40:18 UTC), the receipt in
`.swarm/arc720/round3/gate.json` records `REVIEW_FAIL`, quorum 2 and completed
2. **Only luna and kimi-k2.7-code provided usable answers. This was not a
three-reviewer result.** The input was not truncated.

ARC-661 observed instance: the intended third reviewer, `glm-5.3`, appears in
`failed` with `incomplete: true` and this exact error:

```text
no content in the reply (finish_reason='error', 29833 reasoning tokens)
```

It supplied no usable answer after the recorded reasoning-token expenditure;
the usable panel shrank from three reviewers to two. Quorum was still met.
This records the observed failure, not a measurement of the provider's actual
token limit or a successful answer from that reviewer.

Both synthetic-fixture findings were minor and marked out of scope, as the
threat model requested. The same behavior nevertheless supported two
refutations of the absolute counter-claim. Finding scope does not cancel a
claim refutation in the gate's verdict calculation. The in-scope bounded-read
finding was also confirmed. These are distinct parts of the receipt, not
three reviewers agreeing on a result. The claims/findings scope conflict is
preserved for the owner's step-back committee; no claim validator, counter-claim,
confirmation filter, or round bound was changed to obtain another attempt.

## Authorized post-round-three read correction

The static-file scenario as stated did not reproduce: a file already larger
than the 67,108,864-byte read cap was refused by the existing `fstat` check,
before dispatch, for both disclosure shapes. The limit is bytes, not decoded
characters. A related hole did reproduce: grow the real file after its size
check but before its bounded read, with newline padding extending one byte
beyond the cap and then either the annotation or the complete receipt. The
reader returned a successful partial prefix; both cases dispatched two offline
prompts and exited 3 because the review body was truncated.

The correction checks the bytes actually read before decoding. An overflow
byte now produces an error instead of a successful prefix. The read remains
bounded; the fix does not read an arbitrarily large file or weaken the
synthetic-receipt refusal. It is not a coherent-snapshot guarantee against
all concurrent file changes.

The actual-cap probe now records exit 4 and zero dispatched prompts for both
growing-file cases; the static cases still refuse. Equivalent `--context`
inputs refused before and after: context is an argparse string passed directly
to the guard and does not use `read_text_bounded`. These long-context probes
inject argv in-process, not through an OS exec; they do not claim that this
host accepts an argument that large. The shared reader also serves reviewer
configuration, disposition maps and journal records; its callers already
handle the error result. Prompt-body truncation is a separate later boundary.
The mechanical sibling scan also found unchecked overflow-sentinel reads in
`hanig-portable-handoff/scripts/handoff.py` and
`hanig-verified-workflow/scripts/contract.py` under `skills/`. Those separate
readers were not changed or behaviorally reproduced in this authorized patch;
this record does not claim repository-wide closure of that pattern.

Reproduce offline with
`python3.10 .swarm/arc720/post-round3/read_limit_probe.py --baseline` and
the same command without `--baseline`. The saved source and measurements are
`before-review.py`, `read-limit-before.json` and `read-limit-after.json` in
that scratch directory. Both generated fixture specifications are stored
under `tests/fixtures/arc720/past-limit-*.json`; unit tests materialize them
with a 1 MiB cap, including growth after the descriptor's actual size check.

Offline validation records **17 regression tests and 29 targeted mutation
kills** in `tests.log` and `mutations.json` in that directory. The new kills
target each disclosure's overflow refusal, admission at exactly the byte
limit, and refusal of an unclipped context. They do not die merely at an
earlier positive control or an unrelated exception. This correction is
**not gate-reviewed**: no fourth round or fresh cycle was opened, no PR was
opened, and no installation was performed. Work stopped at that checkpoint
for the committee.

## Replacement design: visible stripping

The owner supplied the committee's replacement criterion: strip recognized
history instead of refusing matching input. This starts a new design cycle,
not an unannounced fourth attempt at the refusal design. The script at
`.swarm/arc720/strip-cycle/run-gate.sh` declares
`--round 1 --fresh-cycle-from standard`. Existing panel policy raises the
quorum to the predecessor profile's current enabled reviewer count (three
in this worktree's roster). The absolute counter-claim, claim validator,
confirmation filter and `MAX_ROUNDS` are unchanged.

No prior gate, measurement, mutation or reproduction artifact was overwritten.
`.swarm/arc720/strip-cycle/before/` additionally preserves the pre-strip source,
test module, mutation driver, validation document and complete staged patch
against `e333f9a`. Historical scratch scripts target their historical helper
interfaces; replay those against the saved state, not the replacement tests.
The bounded-read correction was staged before this design began and is retained.

### What survives and what does not

The supplied live context, disputed summary and disputed reason still reach
both offline transports verbatim because they match neither signature. That
claim is deliberately limited to those fixtures and to unmatched text. An
opening annotation's exact span is removed; its surrounding whitespace, BOMs
and trailing factual evidence survive. A whole matched JSON receipt value is
removed, preserving its outer whitespace/BOMs. Consecutive opening signatures
are each stripped, with offsets relative to the original decoded input.

This is better for the honest synthetic-fixture caller in one specific sense:
the signature itself no longer produces a configuration rejection. The loss
of that subject still limits what reviewers can assess. Even a completely
removed subject reaches the panel with an explicit warning; this does not
manufacture evidence or guarantee a favorable review. The caller sees the
same source/count notice on stderr and in the result, and the audit record
retains the event metadata. An intentional supplier of history loses that
content too. Visible stripping is feedback, not permission to disclose history
through remaining blind spots. No recognized signature needs a refusal
fallback; malformed, unreadable and over-limit inputs retain their independent
configuration errors.

### Offline checks for the replacement

The new suite has **23 tests and 40 targeted mutation kills**, with logs in
`.swarm/arc720/strip-cycle/tests.log` and `mutations.json`. Every method has a
targeted production mutation. The driver checks the actual failing assertion;
an unrelated positive-control failure or exception does not count. A constant
probe changes the actual CLI exit from 0 to 44; a function probe restores
delivery of the forbidden signature. Both resolve the selected module, and
restoration removes the signature again. Historical refusal-design counts
are retained above, not represented as tests of the new action.

Coverage includes exact removal and remainder preservation, each file before
prompt truncation, disputed fields, BOM/whitespace prefixes, whole receipts,
prompt/caller/result/audit notices, human-readable and unavailable results,
empty escalation, consecutive signatures, an entirely removed subject, and
the retained read-overflow and context-boundary cases. The existing supervised
review module passed all 254 tests; 98 adjacent documentation/skill/stdlib
checks also passed. Offline evidence is not a live gate pass, and no PR or
installation is part of this handoff. The five-run residual measurement above
still concerns the frozen earlier input, not this replacement design.

Credential-free preflight on September 27, 2026 accepted the exact standalone
counter-claim and fresh-cycle declaration, then returned `REVIEW_UNAVAILABLE`
(exit 2), with no answering reviewers. The clean context produced no redactions;
astra and astra-xhigh were excluded as author, and the declared cycle requires
three reviewers. The saved preflight receipt is
`.swarm/arc720/strip-cycle/preflight.json`; this is not a live gate pass.
The script intentionally sets `XDG_STATE_HOME=/dev/null` to avoid external audit
writes, so journal persistence reports failure; the local JSON receipt remains
the handoff artifact. Run the same script with credentials for the live review.

## Live replacement-cycle result

The owner ran the replacement script with credentials. The receipt at
`.swarm/arc720/strip-cycle/gate.json`, observed September 27, 2026 at
09:34:22 -0700, records `REVIEW_PASS`: four completed reviewers, quorum three,
standard then deep tiers, `truncated: false`, no confirmed findings and no
refuted claims. **luna, kimi-k2.7-code, glm-5.3 and sol answered**; astra and
astra-xhigh were excluded as author. Kimi and GLM supported the absolute
counter-claim; luna and sol marked it unverifiable. Nobody refuted it.
This means the answering reviewers failed to refute the claims under the
gate's criteria, not that correctness or the absolute counter-claim was proved.
The journal write was intentionally unavailable; the local receipt is retained.

The full design history is preserved, not renumbered away:

| Design / round | Result | Answering reviewers | Local receipt |
| --- | --- | --- | --- |
| Refusal 1 | REVIEW_FAIL | luna, kimi-k2.7-code, glm-5.3 | `.swarm/arc720/gate.json` |
| Refusal 2 | REVIEW_FAIL | luna, kimi-k2.7-code, glm-5.3 | `.swarm/arc720/round2/gate.json` |
| Refusal 3 | REVIEW_FAIL | luna, kimi-k2.7-code | `.swarm/arc720/round3/gate.json` |
| Visible stripping 1 | REVIEW_PASS | luna, kimi-k2.7-code, glm-5.3, sol | `.swarm/arc720/strip-cycle/gate.json` |

The owner reported that both step-back committee members, deepseek-v4-pro and
sol, converged on stripping rather than refusing. Their exit criterion changes
the action while retaining exact-signature detection: matching synthetic
material must not itself cause rejection, unmatched evidence must survive,
and removals must be visible. The issue permitted either refusal or stripping
from the outset. This replacement design explicitly declares a fresh cycle;
it is not a hidden fourth refusal round. The earlier finding/claim scope gap
is separately owned and was not changed here.

## Shipped-unfixed findings

### Disputed-finding identity after stripping: measured non-suppression

Luna's minor/medium finding concerns a valid `not-reproduced` disposition whose
location or summary opens with the exact annotation. Its input map key hashes
the original fields, while the forwarded allegation contains stripped fields.
The two identities differ. This remains unfixed by owner direction.

The actual consumer does **not** retain that key for downstream matching:
`load_dispositions` validates it against the original fields, then returns the
entry values without their map keys. `build_prompt` forwards the stripped
allegation and reproduction reason as a disagreement to re-evaluate, not as an
instruction to suppress findings. The verdict loop has no disposition lookup.
Consequently “the disposition is ignored” is only accurate as a statement about
automatic suppression, not about prompt delivery: the allegation is delivered.

An offline measurement drove real `main`, prompt construction, confirmation,
verdict calculation and serialized audit delivery with injected provider
responses. It covered both contaminated fields independently. In each case,
two provider responses returned either the stripped allegation, the original
allegation, or an unrelated allegation, each major/high with a concrete scenario.
Both reviewer verdicts were `upheld` and no claim was refuted, isolating the
finding decision from other reasons to fail.

| Cases | Measured result |
| --- | --- |
| 2 fields × 3 returned identities | All six exited 1 / REVIEW_FAIL; both findings remained confirmed |
| Clean-disposition control for each field | Both exited 1 / REVIEW_FAIL; both findings remained confirmed |
| Old map key reused with stripped fields, for each field | Both exited 4 / REVIEW_ERROR before any provider call |

The serialized audit records hash the newly returned location/summary, rather
than reusing the disposition key; no adjudication record is generated. Disabling
the actual confirmed-finding accumulation decision in an in-memory production
mutant killed the measurement at `[finding-remains-gating]`, not at a collateral
claim, transport or parsing assertion. **Measured direction: fails safe against
silent suppression**, with a stale-key reuse causing an explicit configuration
error, not application to a different allegation. This does not measure whether
a stochastic reviewer will choose to re-report the allegation; the responses
were injected, not sampled.

Scratch evidence, preserved locally rather than installed or committed as a
unit test: `.swarm/arc720/post-pass/measure_dispositions.py` and
`.swarm/arc720/post-pass/disposition-measurement.json`. Replay with
`TMPDIR=/var/tmp python3.10 .swarm/arc720/post-pass/measure_dispositions.py`.
The report includes actual prompts, result and audit payloads, all ten cases,
the targeted mutation assertion, and production SHA-256
`4275cf653b64751256d7ef553d884eff45043c27264bce4882961fe0bd9111d1`.
No production code changed after the live pass for this measurement.

### Whole-document receipt boundary: known and separately tracked

GLM's minor/medium finding is known and unfixed: two concatenated top-level
typed receipts, or a receipt followed by non-whitespace text, fail the
whole-document JSON parse and are delivered without stripping. It was reported
out of scope for the declared exact signature, not disproved. The owner is
tracking this boundary-evasion pattern separately; no new issue identifier
was supplied here. Together with the earlier BOM prefix and bounded-read
discoveries, it motivates a boundary-level investigation rather than a fourth
instance patch inside this change. No detector broadening was made.

These two findings ship disclosed. Matched receipt content still cannot be
assessed after removal; exact-signature detection is not arbitrary-English or
Unicode-obfuscation detection, and zero-width and bidi controls remain named
blind spots. All failing-round artifacts, pre-strip snapshots and the earlier
five-run residual experiment remain preserved locally. That experiment still
does not measure this replacement design or establish determinism.
