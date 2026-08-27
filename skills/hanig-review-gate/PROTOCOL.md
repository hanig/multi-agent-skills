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
| panel | **two contrasting models** | cheapest-first ladder |
| escalation | **never** | `--escalate`, always from `fast` |
| flag | `--plan` | `--escalate --round N` |
| judged against | do these criteria hold together | does the code meet the criteria |

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

## Bound it to three rounds per change

`--round N`, refused past `MAX_ROUNDS = 3`.

**Step back when round N+1 finds a defect in round N's fix.** Not at N+3. One
session ran five rounds where rounds 3, 4 and 5 each found a defect in the
previous round's fix: an annotation hatch, then a bypass of the hatch, then a
filter that hid the bypass. Each fix was correct and each was one level too
shallow. The step back, when it finally came, replaced the whole mechanism
rather than patching it again.

The signal is not "the reviewers are still finding things." It is "the
reviewers are finding things about my last fix."

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
