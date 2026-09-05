---
name: hanig-review-gate
description: >-
  Adversarial multi-model review before anything is called done. Use before
  reporting work complete, before committing or opening a PR, and whenever a
  claim is being asserted about code that was just written — "this works", "this
  is covered", "this handles X". Sends the diff and the claims to independent
  models (DeepSeek-V4-Pro, GPT-5.6-Luna, Kimi-K2.7-Code, GLM-5.3, and
  GPT-5.6-Sol at xhigh) prompted to refute
  rather than approve. Use also when asked to double-check, get a second
  opinion, or verify a change against other models.
---

# hanig-review-gate

## Host capability boundary

Use the active host's real file, shell, and review capabilities under its
normal approval policy.  A loaded skill does not supply reviewer providers,
their configuration, or coordinator-held credentials; when they are absent,
retain the evidence and report the change as **unreviewed** rather than
substituting self-confidence or a paid worker-side call.  See
`docs/agent-compatibility.md` for the portable capability and credential
contract.

My claim that code works is exactly as inadmissible as a scheduler's
`COMPLETED`. Same principle as `hanig-verified-workflow`, turned on the author.

So before anything is reported complete, the work **and the specific claims made
about it** go to models that did not write the code, each prompted to **refute**.
A reviewer that cannot decide is instructed to refute, because a false "looks
good" costs far more than one more look.

## Two phases, two different reviews

A plan review and an implementation review are not the same review and must not
use the same panel. `PROTOCOL.md` in this directory states the full model; the
enforceable parts are enforced by `review.py` rather than left to memory,
because these rules were written down once and drifted from anyway.

| | plan review | implementation review |
|---|---|---|
| when | before code exists | after the change is written |
| panel | **two contrasting models** | cheapest-first ladder |
| escalation | **never** | `--escalate`, always from `fast` |
| flag | `--kind plan` | `--kind implementation --escalate --round N` |
| judged against | do these criteria hold together | does the code meet them |

**Two contrasting models for a plan, never escalated.** A third adds agreement,
not insight. Measured, not assumed: a four-model panel on one design proposal
had one reviewer uphold every claim and add nothing, while the other two found
all five defects. Contrasting means a different provider and family, so the two
cannot share a failure mode. `--plan` fixes the panel at two and refuses
`--escalate` or a quorum above 2.

**Plan review is Phase 1 and it is where the value is.** Three plan reviews cost
about ten cents and twelve minutes and rejected two designs before any code
existed. The 28-round session below spent all 28 rounds inside a framing that a
plan review would have rejected.

## The author does not judge their own work

When a reviewer proposes a design, exclude it from the panel that reviews the
proposal: `--only` the others. A design's author is the worst judge of whether
its residual is complete. Asked to fix a recurring problem, one reviewer wrote a
proposal with a "residual risk" section listing three ways through; the panel
found two more it had not named, one of them inside the proposal's own API.

## Reviewers refute; they do not design

The system prompt says so, and a finding carries severity, location, summary and
failure scenario with **no remediation field**. Do not ask the panel for a plan.
The author writes the plan and the panel attacks it. Asking a refutation panel
to design is a category error that produces agreement-shaped noise.

## Review against a declared plan, never against perfection

**This is the rule that keeps the process finite, and the one this repo learned
the hard way.** A first version of this skill asked reviewers "can you refute
any claim about this code," which is unbounded by construction: with reviewers
instructed to refute when uncertain, it can never terminate. It ran 28 rounds.

The fix is the skill's own thesis, turned on itself. `hanig-verified-workflow`
exists to say *declare the criteria before you execute, then check against
them*. Apply that here:

1. **Declare what the change is supposed to do, before writing it.** Goal,
   acceptance criteria, constraints, what is explicitly out of scope.
2. **Implement.**
3. **Review the implementation AGAINST THAT PLAN** — "flag drift and missing
   pieces" — not against an ideal.

"Does this meet the declared criteria" is a finite question with an answer.
"Can you find any flaw" is not.

## Always assert the counter-claim

Every round must assert **"this change cannot make an honest run fail."**
Without it, each round tightens the screws with no counter-pressure, and the
verifier drifts toward refusing legitimate work. The first round that asserted
this caught a real regression that two reviewers found independently. A verifier
that cries wolf gets switched off, which costs more than the defect it prevents.

## Bound the loop, and know when to step back

Open-ended loops are how runaways happen, so every review cycle is bounded:

- **Max 3 rounds per change.** Not per session: per change.
- **If round N+1 finds a defect in round N's fix, stop patching.** That is the
  signal that the problem is upstream of the symptom. Convene a step-back
  committee (below) with the full history rather than shipping another patch.
- **After 3 rounds without convergence, start fresh** — new reviewers, full
  history of what was tried. The current context has drifted too far to help.

## The step-back committee

When stuck, looping, or patching symptoms, stop reviewing and convene two
reviewers from contrasting providers with one question:

> Here is what keeps happening across rounds: <history>. Do root cause analysis.
> Ask why three levels deep. Am I patching a symptom or removing the problem?
> Propose the change that makes this class of defect impossible, not the next
> individual fix. Analysis only — do not write code.

The purpose is to step back, not double down. The committee may well say the
design is wrong, which is the point of asking.

## Argue with findings; do not silently filter them

Reviewers produce findings that do not reproduce — one model here retracted its
own conclusion inside the finding text more than a dozen times. The instinct is
to add a severity filter and move on. **Reproduce the finding first.** If it
does not reproduce, say so explicitly in the next round's context rather than
quietly dropping it, and if a reviewer keeps asserting it, that disagreement is
itself information.

## Convergence

Review is done when the findings still arriving are **out of scope, minor, or
matters of taste**. It is never done when the findings list is empty; that will
not happen, and waiting for it is the loop.

### What "still finding things" actually meant here

Across this repo's rounds the findings did not stop, but they changed character,
and that change is the useful signal:

| phase | what the findings were |
|---|---|
| early | false passes reachable by accident, no tampering needed |
| middle | the same rule missing from a sibling path — twelve instances |
| late | real findings whose honest fix had to be WEAKER than the finding implied |

Recognising that last phase matters. Three times the strong fix was
unavailable: an ownership anchor drawn from the evidence it validated, which
created a worse false pass than it closed; interleaving detection that step
numbers cannot support; and content-identity freshness that would have refused
every deterministic re-run, which is a reproducibility tool's success case.
**A real finding does not imply an available fix.** Documenting the limit is
then the answer, and shipping the strong version of an unavailable fix costs
more than the finding did. Once, building it was the only way to find that out.

## The rule

**Do not report work as complete, and do not assert that code works, until the
gate has run and passed.** If it cannot run, say the work is unreviewed — do not
substitute your own confidence for the review.

**Whether to run another round is the user's call, not the agent's.**

```bash
R=~/.claude/skills/hanig-review-gate/scripts/review.py

python3 $R --escalate --diff \
  --context "what this change is for" \
  --claim "the specific thing being asserted" \
  --claim "another assertion being made"
```

**Use `--escalate`.** It runs the tiers cheapest-first and stops at the first
failure, adding only the reviewers the previous tier did not run:

```
fast      deepseek-v4-pro + luna          ~2-3 min   ~$0.03
  ↓ pass
standard  + kimi-k2.7-code                ~+3 min    ~+$0.06
  ↓ pass
deep      + sol @ xhigh                   ~+5 min    ~+$0.18
```

A failing change costs one cheap tier, not the whole panel. Only code that
already survived the cheap readers pays for Sol. This matters more than it
sounds: across six review rounds on this repo, **every single one failed**, and
running the full panel each time paid the slowest, dearest reviewer to re-find
defects a cheap one had already caught.

### Sizing a round

Two failures here were the reviewer infrastructure, not the code, and both read
as an unavailable reviewer rather than as what they were.

**Reasoning tokens come out of the answer's budget.** At 16000 a large review
spent the whole allowance thinking and returned NO content, which cost one
reviewer an entire session before the error message was made to say so. The
default is 64000 and the error now reports the token counts.

**Review one file at a time past roughly 100KB.** Raising the budget bought
exactly one round before the input grew past it too. Splitting keeps working and
sharpens the per-file context.

**Put the limits in a FILE, not in the context prose.** The context grew into a
wall of thirteen inlined "do not re-report" clauses, and a reviewer then burned
its whole 64000-token budget reasoning over that wall on a 97KB file it had
answered fine one round earlier. Pass the limits document with `--file` and keep
the prose to what changed this round.

**Pass `--threat-model`** whenever the code has one. A finding whose
preconditions it excludes is printed under OUT OF SCOPE and does not decide the
verdict; everything else gates as before. Without the flag, every finding gates.

Sources: `--diff` (working tree, default), `--staged`, `--range HEAD~3..HEAD`,
`--file PATH` (repeatable). `--list` shows reviewers and live availability.
`--profile plan|fast|standard|deep` picks a single fixed panel instead of the
ladder. `--only NAME` restricts to named reviewers, accepts `a,b,c` or repeated
flags, and is not combinable with `--escalate`.

`--kind plan|implementation` is REQUIRED for a real review, because a plan and
an implementation get different panels and different rules, and leaving it
implicit is how a design proposal got reviewed by the implementation panel.
`--plan` is an alias for `--kind plan`. A plan review is validated on the panel
that will ACTUALLY run, after `--only` and `--profile`: exactly two reviewers,
on two different providers, quorum 2, never escalated.

`--round N` is required with `--kind implementation` and declares which round
this is for the change under review. Past `MAX_ROUNDS` (3) the gate refuses and
names the step-back. **It rests on an honest round number:** nothing ties a
round to a change, so `--round 1` can be claimed forever. Closing that needs a
per-change receipt keyed to a plan digest, which is not built. See PROTOCOL.md
for the full list of what is and is not enforced.
`--json` for machine consumption.

## Claims are the point

Reviewing a diff finds bugs. Reviewing *claims against* a diff finds the more
dangerous thing: a true-sounding statement the code does not support. Pass the
actual assertions, verbatim — the ones that would go in the summary.

Each reviewer marks every claim `supported`, `refuted`, or `unverifiable`.
**Any refuted claim fails the gate**, regardless of findings.

## Exit codes

| Exit | State | Meaning |
|---|---|---|
| 0 | `REVIEW_PASS` | Quorum reviewed; no confirmed defect, no refuted claim |
| 1 | `REVIEW_FAIL` | A confirmed defect, or a refuted claim |
| 2 | `REVIEW_UNAVAILABLE` | No reviewer ran — **not a pass** |
| 3 | `REVIEW_PARTIAL` | Some ran, quorum unmet — degraded, caller decides |
| 4 | `REVIEW_ERROR` | Usage or configuration error |

2 and 3 are not success. If the gate could not run, the change is unreviewed and
must be described that way.

A finding counts against the gate only if it is critical or major, at high or
medium confidence, **and** carries a concrete failure scenario. That filter
exists to keep speculative and stylistic noise from blocking real work.

## Reading a pass honestly

`REVIEW_PASS` means *N models failed to refute this*. It is not proof of
correctness, and should be reported as what it is. Say which reviewers ran.
Never describe a change as "reviewed by three models" when two were skipped —
the gate prints exactly who ran, who errored, and who was unavailable, so the
honest sentence is always available.

## Reviewers

Configured in `reviewers.json` — routing only (endpoint, model id, effort). It
deliberately carries **no quality scores**: a stale ranking is worse than none,
and availability is resolved live by `--list` rather than asserted in a file.

| Name | Provider | Model | Needs |
|---|---|---|---|
| `sol` | OpenAI | `gpt-5.6-sol` (effort `xhigh`) | `OPENAI_API_KEY` |
| `kimi-k3` | OpenRouter | `moonshotai/kimi-k3` | `OPENROUTER_API_KEY` |
| `deepseek-v4-pro` | OpenRouter | `deepseek/deepseek-v4-pro` | `OPENROUTER_API_KEY` |

Both keys are exported from `~/.zshrc`. A non-interactive shell does not source
it, so run through a login shell (`zsh -ic`) or export the keys explicitly —
otherwise the gate reports `REVIEW_UNAVAILABLE`, which is correct behaviour but
not what you wanted.

`sol` at `xhigh` is slow (several minutes) because reasoning tokens count
against `max_output_tokens`; its ceiling is set high enough that the JSON verdict
is not truncated. Reviewers run in parallel, so wall time is the slowest one.

Transient 5xx and 429 responses are retried with backoff — a gateway hiccup must
not silently shrink the panel and make the gate weaker than it reports.

## What this does not do

It cannot prove correctness, only that several independent adversarial readers
failed to break the claim. It does not review anything outside the diff you give
it. And it is not a substitute for the tests — run those too; the gate reviews
code, it does not execute it.

## Reviewing the review: what three rounds actually produced

Run against `hanig-verified-workflow/contract.py`, which had a passing test
suite and had been reported as working:

| Round | Reviewers completing | Real defects found |
|---|---|---|
| 1 | 1 of 3 | 1 |
| 2 | 2 of 3 | 4 |
| 3 | 2 of 3 | 5 |

The two most serious were both false passes — the failure mode the tool exists
to prevent:

- Predicates alone could return `SCIENTIFIC_PASS` with nothing showing a job
  ever ran. Pre-create the declared output, never submit, and it certified.
- After that fix, a *submitted* job was treated as a *terminated* one. With
  `sacct` unreachable and a stale artifact present, a still-pending job passed.

Both were invisible to a suite of 21 passing tests, because the tests were
written by the same author, with the same blind spot, and several were relying
on the first bug. That is the argument for the gate: a test suite inherits its
author's assumptions, and an adversarial reader does not.

Findings keep arriving across rounds. That is normal for adversarial review and
not by itself evidence the code is bad — but the first round finding a false
pass is exactly why "my tests pass" is not a completion criterion.

### What made the rounds stop converging

Two failure modes beyond the unbounded question, both in how the gate was
*driven* rather than in the reviewers:

**Reviewing against an adversary the code never claimed to stop.** This repo's
own threat model says `contract.json` is trusted input, yet six of fourteen
findings across three rounds required hand-editing it. Pass `--threat-model` so
those are reported and marked rather than counted. Absent it, everything gates.

**Asserting claims about taste.** "The three scripts agree on shared concepts"
is a design judgment; two tools solving different problems will always differ
somewhere, and any refuted claim is a `REVIEW_FAIL`. Reserve claims for
behaviour.

Ultimately the process was modelled on a committee that plans, implements, then
reviews *against the plan*, with bounded iterations and a step-back rule when
it stalls. The version here that ran 28 rounds had no plan to check against, no
bound, and no step-back.

## Known limitation: `command` predicates are unsandboxed

A `command` predicate in a contract runs through `sh` with the verifier's full
privileges. A hostile one can `SIGKILL` the verifier, and no exception handler
can prevent that — `contract.py` catches `BaseException`, which SIGKILL bypasses
entirely. Sol raised this and it is correct.

The honest statement is therefore narrower than "predicates cannot crash the
verifier": *malformed* predicates cannot, but `contract.json` is trusted input.
Do not run `check` against a contract you did not write.
