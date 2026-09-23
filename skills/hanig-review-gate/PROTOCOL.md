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

The shipped implementation ladder is cumulative: every tier's enabled panel
is a strict superset of the tier below it. `tests/test_profiles.py` pins that
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

**Nothing enforces that distinction.** `review.py` counts the `--round` you
pass and refuses past three; it cannot tell a legitimate fresh cycle from a
relabelled fourth round, and the audit journal records the round number you
declared rather than deriving it. So this is a rule you keep, not one the
program keeps for you — which is exactly the weakness this repository warns
about, stated here rather than left for a reader to discover.

What would close it: the journal already records `claim_digests` per round, so
a gate could refuse `--round 1` when a recent record carries overlapping
digests unless the invocation explicitly declares the prior history it
carries. That is unbuilt.

**Step back when round N+1 finds a defect in round N's fix.** Not at N+3. One
session ran five rounds where rounds 3, 4 and 5 each found a defect in the
previous round's fix: an annotation hatch, then a bypass of the hatch, then a
filter that hid the bypass. Each fix was correct and each was one level too
shallow. The step back, when it finally came, replaced the whole mechanism
rather than patching it again.

The signal is not "the reviewers are still finding things." It is "the
reviewers are finding things about my last fix."

## Verify a finding before acting on it

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

## Convergence

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

## The author does not judge their own work

When a reviewer proposes a design, exclude it from the panel that reviews the
proposal (`--only`, naming the others). A design's author is the worst judge of
whether its residual is complete: the panel found two gaps the author's own
"residual risk" section did not mention.

## Reviewers refute; they do not design

The system prompt says so explicitly, and findings carry no remediation field.
Do not ask the panel for a plan. The author writes the plan; the panel attacks
it.
