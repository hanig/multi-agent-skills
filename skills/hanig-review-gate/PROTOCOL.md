# Review protocol

Adapted from Shreshth's committee model. Every rule here was learned by
violating it, and each names the failure that produced it.

The rules used to live in a memory file and were drifted from anyway, in the
same session that wrote them. Prose is not a constraint, so the ones that can
be enforced are now enforced by `review.py` and named here with the flag that
enforces them.

## The two phases are different reviews

| | plan review | implementation review |
|---|---|---|
| when | before code exists | after the change is written |
| panel | **two contrasting models** | fixed profile or cheapest-first ladder |
| escalation | **never** | optional; `--escalate` starts at `fast` |
| flag | `--kind plan` | `--kind implementation --round N [--escalate]` |
| judged against | do these criteria hold together | does the code meet the criteria |

The shipped implementation ladder accumulates results: each tier adds reviewers
not called by earlier tiers. `tests/test_profiles.py` pins that
configuration invariant so an enabled `deep` panel cannot silently collapse to
`standard` again. This is a repository test of the shipped routing file, not a
runtime validation of a locally edited installed copy.

**Plan review is Phase 1 and it is where the value is.** Three plan reviews
cost about ten cents and twelve minutes and rejected two designs before any
code existed. A separate session ran 28 implement-then-refute rounds that all
accepted the framing and hunted defects inside it.

**Two contrasting models, never escalated.** A third adds agreement, not
insight. Measured, not assumed: a four-model panel on one design proposal
produced one reviewer upholding every claim and adding nothing, while the
other two found all five defects. Contrasting means different provider and
family, so they do not share a failure mode. `--plan` fixes the panel at two
and refuses `--escalate`.

## What the tool enforces, and what it does not

Audited by gpt-5.6-sol on 2026-08-27, which found the first version was
"largely caller-attested": it refused the literal flag combinations I had
thought of and nothing else.

**Enforced, checked against the EFFECTIVE panel after `--only`, `--profile`
and enabled-state have all been applied** (checking the intended panel instead
of the actual one is what made the first version bypassable):

| rule | how it is bypassed now |
|---|---|
| a review declares its kind | it cannot; `--kind` is required |
| an implementation review declares its round | it cannot; `--round` is required with `--kind implementation` |
| at most 3 rounds per change | claiming `--round 1` forever. Not detectable without a change identity the tool does not have. |
| an implementation review asserts the honest-run counter-claim | it cannot; every implementation invocation must include `--claim "This change cannot make an honest run fail."` |
| round 2+ dispositions every prior confirmed finding | completeness rests on the supplied map, but `--dispositions FILE` is mandatory, every entry is digest-bound to its location and summary, and every disposition needs a one-line reason |
| a not-reproduced finding reaches the next panel | it cannot; its summary is injected verbatim into the next round's prompt |
| a plan panel is exactly two | it cannot; size is checked after selection |
| the two are on different providers | it cannot; providers are compared after selection |
| a plan review is never escalated | it cannot |
| a plan review needs both verdicts | it cannot; quorum must be 2 |
| an undeclared reviewer joins no profile | it cannot; membership must be declared |
| empty reviewer content counts as coverage or a candidate defect | it cannot; it produces `REVIEW_INCOMPLETE` and contributes no completed judgment |

**Not enforced, and honestly out of reach of this tool:**

- **The round bound rests on an honest `--round`.** Nothing ties a round number
  to a change, so `--round 1` can be claimed forever. This is now a DECISION,
  not a gap: a per-change receipt was designed, reviewed, and rejected. Both
  plan reviewers independently showed it locks honest authors out -- editing
  your own plan mid-change forfeits the change, adding an explicitly permitted
  annotation invalidates the receipt, and three flaky reviewers exhaust the
  bound on a change nobody reviewed. The gate is run by the person it
  constrains, who can edit this file anyway; buying tamper-resistance with
  honest-work failures is the wrong trade. See
  `docs/proposal-protocol-hardening.md`.
- **A structured threat model is not required.** Also designed and rejected:
  validation that accepts `trusted=["x"], hostile=["y"], out_of_scope=["z"]`
  buys ceremony, not constraint. The threat model stays free text, judged by
  the reviewers who read it.
- **Stepping back when round N+1 finds a defect in round N's fix.** The tool
  cannot see that a finding is about the previous fix.
- **Excluding a design's author from the panel judging it.** The tool does not
  know who wrote the thing under review.
- **Declaring acceptance criteria before implementing.** The tool does not
  carry a receipt proving that the criteria predate the implementation.

The remaining items require judgment or state that this invocation does not
possess. The round identity is a gap with a deliberately rejected fix.

## Review against declared criteria, never against perfection

"Refute any claim about this code" is unbounded, and reviewers are instructed
to refute when uncertain, so it cannot terminate. Declare acceptance criteria
BEFORE implementing; then the question is "does this meet them, and where did
it drift". This is the repo's own thesis, applied to itself.

## Always assert the counter-claim

Every round must carry a claim of the form **"this cannot make an honest run
fail."** Without it each round tightens the screws with no counter-pressure.
The first round that asserted it caught a real regression that two reviewers
found independently: a stricter key check that refused honest criteria carrying
a harmless annotation.

`review.py` enforces the canonical assertion **"This change cannot make an
honest run fail."** for every implementation review. Case and terminal
period may vary; omitting the assertion is `REVIEW_ERROR` before any reviewer
runs.

## Bound it to three rounds per change

`--round N`, refused past `MAX_ROUNDS = 3`.

Past three rounds the choice is NOT merge-or-abandon. `SKILL.md` states the
third path and this file did not, which cost one orchestrator a wrong framing:
**after 3 rounds without convergence, start fresh — new reviewers, full history
of what was tried.** A fresh cycle is legitimate only when the mechanism
changed or the history travels with it; restarting the counter on the same
change with the same design is a dishonest round 4.

Implementation reviews require `--round N` and a stable checkout identity.
`--staged`, `--range`, `--diff` and file reviews derive it from the origin URL,
target merge-base (default `origin/main`) and current branch. Use `--target REF`
for another target; in detached HEAD, declare `--plan-name NAME --unit UNIT`.
Either explicit identifier replaces the branch component; Git origin and shared
target history are still required. Fix commits retain the identity while that
merge-base is unchanged. Renames, remote changes, rebases, declaration changes or
a different state home can create another history; this is an honest-caller
bound, not a tamper barrier. The enforcing ledger refuses repeated, lower or
skipped rounds. Start at 1 only for a new identity; subsequent rounds require
`--dispositions FILE`. For an existing ledger history, fresh cycles require
an exhausted predecessor plus `--fresh-cycle-from PROFILE`; an unseen identity
records the explicit declaration as cycle 1 with no declared predecessor. Sizing, listing, plan reviews and adjudication do
not reserve implementation rounds.

The ledger reserves before provider dispatch, separately from the non-gating
audit journal. It checks contiguous rounds, a consistent selected profile per
cycle and an exhausted predecessor for a fresh cycle. It does not determine
whether the mechanism changed, whether the operator carried the full history,
or whether a finding is about the previous fix. Those remain judgment.

**Step back when round N+1 finds a defect in round N's fix.** Not at N+3. One
session ran five rounds where rounds 3, 4 and 5 each found a defect in the
previous round's fix: an annotation hatch, then a bypass of the hatch, then a
filter that hid the bypass. Each fix was correct and each was one level too
shallow. The step back, when it finally came, replaced the whole mechanism
rather than patching it again.

The signal is not "the reviewers are still finding things." It is "the
reviewers are finding things about my last fix."

## Verify a finding before acting on it

Reviewers are asked for a `counterexample` with non-empty `setup`, `input`,
`expected` and `observed` strings on every critical or major finding, or a
non-empty `non_executable_reason` explaining why no check can be written.
They should omit the unused alternative. Either, both or neither is valid;
absence never changes a finding's status. Null, blank reasons, and empty
counterexample objects (including objects whose values are all null or blank
strings) are placeholders. Other malformed values, including partially filled
counterexamples, are refused. Placeholders render as absent; confirmed findings
without supplied evidence show "no counterexample". JSON output and the journal
retain the values verbatim. The gate validates and displays evidence but never
executes counterexamples.

Never tell the panel what a previous round decided. A round's context
describes what changed, not how the last round went. The same rule covers
every `--file`: do not disclose previous verdicts, pass/fail summaries, tiers,
quorum outcomes, or reviewer-attributed opinions. Names quoted as documentation
or code examples are not themselves assertions about a previous decision.

Carry disputed code allegations forward with locations, reproduction commands
and outputs, without reviewer attribution or panel decisions. A measurement
that an allegation did not reproduce is not a round outcome. Before dispatch,
the guard strips the exact ARC-720 opening rerun annotation and whole matched
JSON gate-report values. Leading whitespace/BOMs and all unmatched character
ranges remain unchanged. Consecutive opening signatures are each removed.
Ordinary prose, standalone state names, documentation tables, JSON Schemas,
unrelated nested state data and the supplied live dispute fixtures are untouched.

Removal is visible in the prompt, stderr, result receipt and audit record:
each event names its input, signature kind, original decoded-character offsets
and removed character count. Removed outcomes are not echoed in those notices.
Original files are not rewritten. An entirely removed subject still reaches
the panel with the warning that removed material cannot be assessed; removal
is neither a configuration failure nor evidence of correctness.

Exact synthetic receipts cannot be distinguished from history and are stripped
too, including facts inside the matched JSON value. This avoids rejecting the
honest fixture, but the panel cannot evaluate what was removed. A notice exposes
that loss rather than repairing it. No recognized signature needs a refusal
fallback; unreadable, oversized or otherwise invalid inputs retain their
configuration errors. Deliberately supplying history is still prohibited, even
though the action is now visible stripping rather than rejection.

This deliberately narrow rule misses paraphrased verdicts, attributed opinions,
nested/fenced receipts, other languages and encoded disclosures. Diffs, claims
and threat-model text remain unscanned caller-trusted review subjects. None
of these blind spots permits disclosure of previous decisions. The caller
must remove that history; an accepted input is not proof of independence.

A CONFIRMED finding is a reviewer's claim, not a fact. Check the code it names
before you change anything.

Measured, 2026-08-30. A round-3 escalation returned seven MAJOR findings
against `swarm.py`. Five were spot-checked and **none survived**: one named a
line that does something else entirely, one described a limitation already
documented in the comment directly above it, and three reported defects that
had been found by earlier rounds and FIXED, where the comment explaining the
old bug was still in place.

That last group is the failure mode to know. The reviewer read our own prose
describing a defect we had already closed, and reported the defect. It is the
mirror of the bug this repo has made four times in its own guards: matching the
comment that explains an absence. Detailed comments about past defects are
worth keeping, and they will produce stale findings; the answer is verification,
not thinner comments.

Cost of not verifying: seven fixes to code that was already correct, each one a
new chance to break something that worked.

For round 2 and later, `--dispositions FILE` is mandatory. The file is a JSON
object mapping each prior confirmed finding's digest to its disposition:

```json
{
  "SHA256": {
    "location": "path/to/file.py:123",
    "summary": "the prior finding text",
    "disposition": "reproduced",
    "reason": "one line explaining the reproduction result"
  }
}
```

The key is the lowercase SHA-256 hex digest of the UTF-8 JSON encoding of
`[location, summary]`, with `ensure_ascii=False` and separators `(",", ":")`.
The disposition is exactly `reproduced`, `not-reproduced`, or `deferred`, and
the reason must be one non-empty line. Every `not-reproduced` summary is copied
verbatim into the next round's prompt so disagreement cannot be silently
filtered. The invocation cannot independently know whether the map omitted a
prior finding; completeness of the supplied map remains caller-attested.

If a disputed entry contains reviewer attribution or a panel decision, remove
that framing, not the code allegation or measurements. Recompute the finding
digest when its location or summary changes; the original audit record remains
unchanged. Literal decision examples that are themselves the code change
belong in its diff, not auxiliary context.

## Convergence

### Audit ledger identity and external adjudication

`--adjudicate` records an independent acceptor's declared disposition for a
confirmed finding, round and full reviewed head. Declared authors cannot accept
their own rebuttals. This is caller-attested audit history, not authentication or
merge authority; an adjudication never changes the original review verdict.

Decision-bearing identity fields are validated before redaction exemption:
canonical lowercase 40-/64-hex Git object IDs and lowercase 64-hex SHA-256
finding/claim digests retain their exact bytes. Rounds are null or positive
integers (never booleans); confirmed classifications are booleans. Schema
versions and type/kind/decision/root-verdict labels use exact enumerations. Timestamps use
the emitted UTC nanosecond format with a valid calendar date. Author lists
are nonempty and require PROVIDER/MODEL tokens; accepted_by permits a name or
slash-separated identity. Both use 1–256 ASCII characters, nonempty components
starting with a letter or digit and continuing with letters, digits, `_ . : @ + -`.
Invalid fields are refused before recording, not stored redacted. Reasons must
be nonempty text and remain redacted, as do notes and arbitrary reviewer extras.
No trimming, case-folding or reconstruction supplies a decision identity.
Recognized schema keys and fixed ledger type, decision and output-state tokens
are program vocabulary and retain their spelling; an API-key collision cannot
rename the field that holds a validated identity. This exemption is restricted
to known paths: lookalike keys in arbitrary reviewer extras remain redacted.

`--open-findings --head SHA` validates identities across all canonical history
before selecting that head. A missing or damaged decision field, including earlier
redaction damage, is listed by record path as `UNATTRIBUTABLE` and exits 1.
This takes precedence over `NO_OPEN_FINDINGS`, even if all attributable findings
have dispositions. The query is read-only: damaged history is neither silently
skipped nor rebound to a guessed identity. Existing headless records also block
a clean query; they do not block new review verdicts. Adjudication can still
record a disposition for a separate valid finding but cannot clear the damage.

A digest appearing at several rounds requires an explicit occurrence selector.
The refusal names all available selectors: `--round N` for each numbered round
and `--round plan` for a null round. Disposing a numbered occurrence does not
select or dispose the null occurrence. `plan` is an adjudication selector only;
ordinary implementation reviews still require an integer round within the bound.

Convergence is when arriving findings are **out of scope, minor, or taste**.
Never an empty findings list: that will not happen, because reviewers are told
to refute when uncertain.

## Always pass a threat model

`--threat-model` states which inputs are trusted. Without one, 6 of 14 findings
in a single round required a hand-edited `contract.json` that the tool never
claimed to defend against. A finding outside the threat model is reported and
marked, but does not decide the verdict.

## Claims are about behaviour, never taste

A claim must be refutable by pointing at code. "The design is clean" is not a
claim. "No spelling of a key the criterion is read with can enter as an
annotation" is.

Write claims you expect might FAIL. Claims written to be upheld waste the
panel. In practice roughly two of six claims per round have been refuted, and
the refuted ones are the entire value of the round.

## Required refusals do not exempt the absolute counter-claim

**ARC-1163, 2026-09-29: the orchestrator chose this documented-limit path;
the owner may reverse it. This is not an owner decision.** The gate cannot
independently distinguish a required refusal from a wrongful one. Keep
`This change cannot make an honest run fail.` absolute and standalone: no caller
qualification and no whitelist of a qualified sentence. Its refutation always
blocks a pass; quorum determines the non-passing verdict, not permission to
ignore the refutation.

### Why claim scope is not applied

The missing distinction is semantic: deciding whether a refusal is wrongful
requires independent acceptance criteria and evidence applying them. A required
refusal can therefore be reported as a counter-claim refutation. At quorum it
blocks a pass regardless of findings or claim-level `in_scope`; without quorum
the result is still non-passing. Finding scope remains a separate mechanism.

Claim-level `in_scope` is deliberately not consumed in adjudication. It may be
retained as reviewer extension data, but cannot exempt a refutation. The rejected
scope-filter design produced a concrete false pass: a candidate wrongly refused
UTC under a contract admitting it; one reviewer explicitly refuted the absolute
claim but marked it out of scope under a misleading caller boundary, while the
other supported it. Both top-level verdicts were upheld. The unchanged gate
returned exit 7; filtering that refutation made the same replies pass at quorum
2. Keeping the sentence unchanged while suppressing its counterexample does not
preserve its protection.

The committee identified the shared source of both failure directions: a
reviewer-set scope flag supplies the needed applicability judgment from the
same fallible source as the claim assessment. Neither a validated boolean nor
a rationale supplies independent authority. Under the available criteria and
authority, wiring the field cannot guarantee both protection from wrongful
refusal and freedom from false rejection of required refusals. This is a
documented limit, not proof that every bounded mechanism under different,
independently supplied criteria is impossible.

Rejected alternatives include consuming claim scope, qualifying the claim,
whitelisting a qualified sentence, and rewriting the absolute counter-claim.
The scope filter creates the reproduced false-pass path; the latter alternatives
weaken the counter-pressure and are forbidden here. More agreement or stronger
prompt wording does not itself establish independent acceptance criteria. The
committee supplied analysis, not the authority for the orchestrator's decision.

### Durable reproduction and optional local rationale

The in-repository positive control is `tests/test_arc1163_claim_scope.py`,
specifically
`TestCounterClaimScope.test_000_wrongful_refusal_with_false_scope_still_blocks`.
It executes the wrongful UTC refusal first, then sends the two responses through
real gate parsing and adjudication, requiring exit 7 and preserving the explicit
refutation in output and serialized audit evidence. Companion tests cover
escalation, text output and refusal of a caller-qualified counter-claim.

Run `python3 -m unittest tests.test_arc1163_claim_scope` and
`python3 tests/mutate_arc1163.py`. The mutation runner adds the rejected scope
filter in memory and requires the positive control to fail specifically with
`0 != 7 : [arc1163-counter-claim-exit]`, not from a collateral error. These tracked
tests require no worktree-local receipt and reproduce the evidence on another
checkout.

Optional supporting committee session, **local evidence not in the repository**:
`/home/hani/multi-agent-skills-arc1163/.swarm/arc1163/stepback/sessions/arc1163-stepback-run-20260929T042532Z-8VYnZv.json`.
SHA-256: `9cfe9d2f83c3a1e172eddeb546fb892b6f175e3bf4e20e3ae1376758ee2a0dfb`.
The absolute path may become unavailable when that worktree is removed; the
digest identifies the observed bytes but does not make them repository evidence.
The regression and mutation runner above are the durable evidence.

### Adjudication stays outside the gate

Use `hanig-orchestrate`'s `adjudication.matrix` for required-refusal disputes
outside the gate, under the current `docs/orchestrator-mandate.md` rather than a
new blanket waiver. This decision grants no new override authority.
Demonstrably outside-required-behaviour findings need independent orchestrator
and mandate-named concurrence, excluding the author, plus the complete impact
record (affected goals, criteria, outputs and consumers; evidence bounding
impact; shared surfaces; pre-existing policy authority; observable falsifier).
Required-criterion and hard-invariant violations remain nonoverridable;
unresolved applicability blocks. Calling a refusal deliberate or required is
not itself adjudication. Preserve `REVIEW_FAIL` and
`REVIEW_CLAIMS_REFUTED` and their evidence. Eligibility after adjudication is
not a pass, and a false delivered claim must not be silently endorsed. This
choice retains the false-rejection limit; it changes neither the mandatory
assertion nor any gate decision logic.

## The author does not judge their own work

When a reviewer proposes a design, exclude it from the panel that reviews the
proposal (`--only`, naming the others). A design's author is the worst judge of
whether its residual is complete: the panel found two gaps the author's own
"residual risk" section did not mention.

## Reviewers refute; they do not design

The system prompt says so explicitly, and findings carry no remediation field.
Do not ask the panel for a plan. The author writes the plan; the panel attacks
it.

## Collaborative plan review

`--plan`: two unique seats, different supported providers/families, quorum 2,
no escalation or `--round`. Invalid panels return `REVIEW_ERROR` (4) before
transport. Committee conversations: two parallel turns (four requests, no third),
independent verdict-schema review then cross-examination retaining history. Peer replies: shared secret redaction, otherwise verbatim, inside tags using `secrets.token_hex(16)`, regenerated until absent
from the reply. All providers redact before history/delivery and in reports/journals.

Turn 2: final claims, new findings, own kept/withdrawn positions/defenses, and `concur`, `refute` or `refine` per peer finding. The enum governs despite contradictory recorded explanations. A finding
drops on refutation with at least 40 whitespace-collapsed characters unless
its author both keeps and explicitly defends it. This measures length, not
substance. Short/missing reasons retain finding, verbatim response and
`refutation_too_short`. Refinement retains severity; withdrawal alone cannot
drop findings. Simultaneous defenses address turn 1, never unseen turn-2 responses. Refuted rows retain finding, author position and peer response.
New/paraphrased findings stand without peer responses.

Own restatements merge only with the first own turn-1 summary/failure_scenario
matching after NFC, casefold, whitespace collapse and stripping, in order. Originals retain independent responses;
the original supplies merged response/outcome. Severity retains the higher
of minor < major < critical; recorded replies preserve original severities.
Restatements record `restated`, `restated_finding_id`, `response_outcome`,
adding nothing to verdict or final findings.

`PLAN_PASS` (0): both seats support every final claim, no standing major/critical
finding, regardless of confidence/scope. Otherwise `PLAN_FAIL` (1), including
truncated input. False standing findings can fail sound plans. Any seat failure
or incomplete/unusable answer in either turn gives `REVIEW_UNAVAILABLE` (2).
Public entry `review.cmd_plan_review` checks the panel and supplies the exchange's
schema validator before judgment. The private verdict independently checks panel,
both turn schemas and finding-ID coverage.
JSON, journal and text show both turns, joint table and token usage.

The shared loader lazily loads `plan_review.py` by path as
`_hanig_review_gate_plan_review`, refusing a private cache entry from another
path. It injects its own helpers/PROVIDERS once; plan_review refuses unconfigured
use, never importing its owner. Private panels require registered providers. Implementation reviews retain stateless behavior
and their confirmation filter.
