---
name: hanig-review-gate
description: >-
  Adversarial multi-model review before anything is called done. Use before
  reporting work complete, before committing or opening a PR, and whenever a
  claim is being asserted about code that was just written — "this works", "this
  is covered", "this handles X". Sends the diff and the claims to independently
  routed models prompted to refute rather than approve. Use also when asked to
  double-check, get a second opinion, or verify a change against other models.
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
| panel | **two contrasting models** | fixed profile or cheapest-first ladder |
| escalation | **never** | optional; `--escalate` starts at the selected tier |
| flag | `--kind plan` | `--kind implementation --round N [--escalate]` |
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

Declare every author with repeatable `--author PROVIDER/MODEL`, for example
`--author codex/gpt-5.6-sol`. The gate removes matching models from fixed,
explicit `--only`, and escalated panels and names each removal, such as
`sol excluded: authored this change`. It compares the complete model ID after
the first slash exactly, independent of transport provider: Codex's
`codex/gpt-5.6-sol` matches the `openai` reviewer model `gpt-5.6-sol`, and
`openrouter/moonshotai/kimi-k2.7-code` retains the nested model ID. Model
substrings, case variants, and seat names are not model matches. If exclusion
leaves too few eligible reviewers for quorum (including a fresh-cycle floor),
the gate returns `REVIEW_UNAVAILABLE` before calling any reviewer; it never
reduces quorum. Author declarations are caller-supplied, not inferred from Git
or dispatch, so an omitted author is not automatically excluded.

Committee `open` accepts the same repeatable flag and refuses a matching
explicit or default member before calling providers. The author set persists;
`ask`, `review`, `synthesize`, and `tiebreak` check it on reuse, and accept the
flag to declare authors on older sessions. A conflicting replacement set is
refused. Existing bare model IDs and configured seat aliases remain accepted
by committee for compatibility. A refused legacy session replaces its current
resolution with `OWNER` while retaining previous decisions as history.

A design's author is the worst judge of whether
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
  history of what was tried. Declare the replaced profile with
  `--round 1 --fresh-cycle-from fast|standard|deep`, and repeat
  `--fresh-cycle-from` on each round of that cycle. The replacement must select
  at least that profile's current enabled reviewer count (minimum two); the
  gate raises quorum to that floor, including on the escalation ladder.
  The verdict and audit journal label this caller-declared provenance. The
  journal has no change identity or historical profile membership, so it is
  not used to infer exhaustion from unrelated runs. Declaring the correct
  predecessor and carrying the history remain the caller's responsibility.
  An implementation `--quorum 1` requires `--allow-single-reviewer REASON`, a
  non-empty one-line reason printed on the verdict and recorded in the journal.
  This explicit exception cannot lower a declared fresh cycle's floor.

## The step-back committee

When stuck, looping, or patching symptoms, stop reviewing and convene two
reviewers from contrasting providers with one question:

> Here is what keeps happening across rounds: <history>. Do root cause analysis.
> Ask why three levels deep. Am I patching a symptom or removing the problem?
> Propose the change that makes this class of defect impossible, not the next
> individual fix. Analysis only — do not write code.

The purpose is to step back, not double down. The committee may well say the
design is wrong, which is the point of asking.

After challenging the members, run
`python3 "$HANIG_REVIEW_GATE_DIR/scripts/committee.py" synthesize SESSION --author PROVIDER/MODEL`.
Convergence produces a unified plan; divergence automatically calls the
`astra-xhigh` seat (`gpt-6-astra`, effort `xhigh`, profile `tiebreak` only).
For an already identified split, use `tiebreak SESSION --author PROVIDER/MODEL` directly.
It receives the question and every member's final position verbatim and saves
a RULING adopting a named position with the deciding evidence, not a fresh plan.

Both commands read the current `docs/orchestrator-mandate.md` from the project;
use `--mandate-file PATH` when it lives elsewhere. The mandate's stop-and-ask
list and bounds go to the model. A known owner-only question must be declared
with `--stop-and-ask REASON`, which refuses without calling a provider and is
retained in the session. Semantic classification otherwise rests on the model
and an honest caller; a ruling supplies analysis, never additional authority.
An unavailable, empty, truncated or malformed answer routes to OWNER (exit 1)
with a persisted reason. Any Astra coauthor (`--author codex/gpt-6-astra`, also
recognized by its legacy bare model and configured aliases) refuses the
tie-break using the same model exclusion rule as membership. Missing or conflicting author
declarations also route to the owner; existing sessions can declare their author
on first use. `show SESSION` displays the current resolution, and a later member
turn invalidates it while retaining the earlier decision's audit record.

## Argue with findings; do not silently filter them

Reviewers produce findings that do not reproduce — one model here retracted its
own conclusion inside the finding text more than a dozen times. The instinct is
to add a severity filter and move on. **Reproduce the finding first.** If it
does not reproduce, say so explicitly in the next round's context rather than
quietly dropping it, and if a reviewer keeps asserting it, that disagreement is
itself information.

## Record external adjudication

The owner or the orchestrator acting under the owner's mandate can adjudicate a
confirmed finding; the author can never accept its own rebuttal. This ledger
records their declared decision, identity and reason, not authenticated authority.
It grants no merge authority and never changes a recorded verdict: `REVIEW_FAIL` remains
`REVIEW_FAIL`, including after an adjudication or a subsequent review.

```bash
python3 "$HANIG_REVIEW_GATE_DIR/scripts/review.py" --open-findings --head SHA
python3 "$HANIG_REVIEW_GATE_DIR/scripts/review.py" --adjudicate FINDING_DIGEST \
  --head SHA --accepted-by OWNER_OR_ORCHESTRATOR --reason "Evidence and rationale" \
  --decision overruled --author codex/gpt-6-astra
```

Both commands are offline and emit JSON. Review input flags, including `--file`,
are refused for ledger commands. `--head` takes the complete lowercase
commit SHA. Reviews using two- or three-dot `--range` bind the journal head to the
resolved commits supplied to Git's diff. Working-tree, single-revision and file
inputs have no bound head; older headless records remain intact and unattributed.
Use the effective state home from the original `journal.path` and the original
project directory; the configured state home may have fallen back during review.

Recording requires a matching confirmed finding on that head and a non-empty
reason. If the digest occurs in multiple rounds, add `--round N`. Decisions are
`overruled`, `accepted` or `refuted_by_reproduction`; each records a disposition,
not a fix or a pass. Declare all authors with repeatable `--author`; the record's
`author` field is that list. Acceptor models use ARC-755's exact model-ID comparison
after the outer provider prefix; names are not case-folded or substring-matched.

The read-only query exits 1 when confirmed findings lack an adjudication for
that head, round and digest, 0 when none are recorded as open, and 4 on invalid
input or unreadable canonical history. Zero certifies neither review coverage nor
complete historical capture. It ignores unpublished pending files.

Adjudications append redacted, immutable JSON lines through the same atomic,
bounded, non-gating writer as reviews. A valid recording request exits 0 even
when persistence is unconfirmed: inspect `journal.written` and the
`ADJUDICATION_RECORDED` or `ADJUDICATION_UNCONFIRMED` status. A write failure
emits `JOURNAL_WRITE_FAILED`; it never closes a finding without a canonical record.

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

Set `HANIG_REVIEW_GATE_DIR` to the directory containing the `SKILL.md`
instance this agent actually loaded. This normal shell variable has no
agent-specific interpolation requirement; the quoted script path leaves the
working directory, and therefore `--diff` or relative `--file` arguments,
unchanged.

```bash
export HANIG_REVIEW_GATE_DIR="/path/to/loaded/hanig-review-gate"
R="$HANIG_REVIEW_GATE_DIR/scripts/review.py"

python3 "$R" --escalate --diff \
  --context "what this change is for" \
  --claim "the specific thing being asserted" \
  --claim "another assertion being made"
```

**Use `--escalate` for a ladder review.** It starts at the selected tier and
walks toward `deep`, stopping at the first `REVIEW_FAIL` or
`REVIEW_CLAIMS_REFUTED` with quorum and adding only the reviewers the previous
tier did not run:

```
fast      luna + kimi-k2.7-code
  ↓ pass
standard  + glm-5.3
  ↓ pass
deep      + sol @ xhigh
```

A failing change costs its starting tier when that tier reaches quorum.
Pass
`--author codex/gpt-5.6-sol` for a change Sol authored: the gate excludes Sol
even at `deep`, provided the remaining independent panel can reach quorum. This matters more than it
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
Without `--profile`, implementation reviews select `fast` only when every
changed path is documentation (`*.md`, `docs/**`, or `examples/**`). Paths
under any `scripts/`, `lib/`, `bin/`, or `tests/` directory, and any
`reviewers.json`, take precedence and select `standard`; all other changes
also select `standard`. Git supplies the paths for `--diff`, `--staged`, and
`--range`, including both sides of renames; repeatable `--file` inputs join
that set. Explicit file symlinks retain both their supplied path and their
resolved target in the set; targets outside the review root are undetermined.
Empty or undeterminable path sets use `standard` and say why.
Parent components in `--file` paths and files outside the review root are
conservatively undetermined. The chosen tier and reason appear in text and
JSON output. This is path classification, not an inspection of document contents.

An explicit `--profile plan|fast|standard|deep` always wins: it picks a fixed
panel, or the starting tier with `--escalate`. Plan reviews retain their
two-model panel, and `--list` without a profile retains the configured default.
Author exclusion and panel floors apply after tier selection.
`--only NAME` restricts to named reviewers, accepts `a,b,c` or repeated
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
With quorum and no confirmed findings, that is `REVIEW_CLAIMS_REFUTED`;
confirmed findings take precedence as `REVIEW_FAIL`. Correct the claim (or
the code), then re-run the gate; do not argue a refuted claim into a pass.
These rounds count toward the same round bound.

## Exit codes

| Exit | State | Meaning |
|---|---|---|
| 0 | `REVIEW_PASS` | Quorum reviewed; no confirmed defect, no refuted claim |
| 1 | `REVIEW_FAIL` | Quorum reviewed; a confirmed defect, with or without refuted claims |
| 2 | `REVIEW_UNAVAILABLE` | No reviewer ran — **not a pass** |
| 3 | `REVIEW_PARTIAL` | Some ran, quorum unmet — degraded, caller decides |
| 4 | `REVIEW_ERROR` | Usage or configuration error |
| 6 | `REVIEW_INCOMPLETE` | A required reviewer returned no usable content |
| 7 | `REVIEW_CLAIMS_REFUTED` | Quorum reviewed; refuted claims and zero confirmed findings — **not a pass** |

Exit 5 remains reserved; adjudication introduces no review verdict.

Every nonzero state is non-success. Incomplete review is not an implementation
failure, but it supplies no judgment and cannot satisfy required coverage.

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
| `luna` | OpenAI | `gpt-5.6-luna` (effort `high`) | `OPENAI_API_KEY` |
| `kimi-k2.7-code` | OpenRouter | `moonshotai/kimi-k2.7-code` | `OPENROUTER_API_KEY` |
| `glm-5.3` | OpenRouter | `z-ai/glm-5.3` | `OPENROUTER_API_KEY` |
| `astra` | OpenAI | `gpt-6-astra` (effort `high`) | `OPENAI_API_KEY` |

Both keys are exported from `~/.zshrc`. A non-interactive shell does not source
it, so run through a login shell (`zsh -ic`) or export the keys explicitly —
otherwise the gate reports `REVIEW_UNAVAILABLE`, which is correct behaviour but
not what you wanted.

`sol` and `kimi-k3` remain disabled. Sol writes code in the current routing and
an author does not review its own work; DeepSeek V4 Pro is a committee member,
not a gate reviewer. Reviewers run in parallel, so wall time is the slowest one.

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
somewhere, and any refuted claim still blocks a pass. Reserve claims for
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
