---
name: hanig-review-gate
description: >-
  Adversarial multi-model review before anything is called done. Use before
  reporting work complete, before committing or opening a PR, and whenever a
  claim is being asserted about code that was just written — "this works", "this
  is covered", "this handles X". Sends the diff and the claims to independent
  models (GPT-5.6-Sol at xhigh, Kimi K3, DeepSeek-V4-Pro) prompted to refute
  rather than approve. Use also when asked to double-check, get a second
  opinion, or verify a change against other models.
---

# hanig-review-gate

My claim that code works is exactly as inadmissible as a scheduler's
`COMPLETED`. Same principle as `hanig-verified-workflow`, turned on the author.

So before anything is reported complete, the diff **and the specific claims made
about it** go to models that did not write the code, each prompted to **refute**.
A reviewer that cannot decide is instructed to refute, because a false "looks
good" costs far more than one more look.

## The rule

**Do not report work as complete, and do not assert that code works, until the
gate has run and passed.** If it cannot run, say the work is unreviewed — do not
substitute your own confidence for the review.

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

**Pass `--threat-model`** whenever the code has one. A finding whose
preconditions it excludes is printed under OUT OF SCOPE and does not decide the
verdict; everything else gates as before. Without the flag, every finding gates.

Sources: `--diff` (working tree, default), `--staged`, `--range HEAD~3..HEAD`,
`--file PATH` (repeatable). `--list` shows reviewers and live availability.
`--profile fast|standard|deep` picks a single fixed panel instead of the ladder.
`--only NAME` restricts to named reviewers (not combinable with `--escalate`).
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

### Why rounds stop converging, and what to do about it

Three failure modes, all in how the gate is *driven*, not in the reviewers:

**Reviewing against an adversary the code never claimed to stop.** This repo's
own threat model says `contract.json` is trusted input, yet six of fourteen
findings across three rounds required hand-editing it. Pass `--threat-model` so
those are reported and marked rather than counted. Absent it, everything gates.

**Asserting universal claims.** "No path through check() can exit 0 when
evidence is missing" asks a reviewer to prove a program correct, and they are
told to refute when uncertain, so the gate fails by construction. Assert what
*changed this round*, in terms that a specific input could falsify.

**Asserting claims about taste.** "The three scripts agree on shared concepts"
is a design judgment; two tools solving different problems will always differ
somewhere, and any refuted claim is a `REVIEW_FAIL`. Reserve claims for
behaviour.

The signal that review is done is not an empty findings list. It is that the
findings still arriving are out of scope, minor, or matters of taste.

## Known limitation: `command` predicates are unsandboxed

A `command` predicate in a contract runs through `sh` with the verifier's full
privileges. A hostile one can `SIGKILL` the verifier, and no exception handler
can prevent that — `contract.py` catches `BaseException`, which SIGKILL bypasses
entirely. Sol raised this and it is correct.

The honest statement is therefore narrower than "predicates cannot crash the
verifier": *malformed* predicates cannot, but `contract.json` is trusted input.
Do not run `check` against a contract you did not write.
