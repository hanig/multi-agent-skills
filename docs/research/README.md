# External research, 2026-09-21

Three reports commissioned while the orchestrator was idle, asking what
long-horizon agent systems do that this one does not. Kept because the
findings outlived the session that asked for them.

| file | model | question |
|---|---|---|
| `2026-09-21-long-horizon-orchestrators-claude.md` | claude-opus-5, web search | Compare our design against comparable systems; where are we over-engineered; how do you know a run is going wrong early |
| `2026-09-21-long-horizon-orchestrators-astra.md` | gpt-6-astra, web search | The same brief, answered independently |
| `2026-09-21-missing-capabilities-astra.md` | gpt-6-astra, web search | A deliberately different question: what faculties are ABSENT entirely, not whether ours are well judged |

The third brief exists because the first two were framed around our eight
load-bearing choices, which can only return a critique of what we have. It
could not surface memory, because memory is not a flaw in any of the eight.
That reframing came from the repository owner and is the reason the most
useful findings exist.

## What came of them

- **ARC-691** (Urgent) — eight invariants about truth, none about time. Every
  supporting code claim independently re-verified before filing.
- **ARC-692** (High) — fence state writes with a monotonic epoch. Filed in a
  weakened form: the reviewer's supporting claim about the `acquire_lease`
  docstring did not survive checking, and the ticket says so.
- Capability inventory recorded on **ARC-678**, rather than spawning four
  orphaned issues. ARC-690 exists because ten orphans was the problem.

## Reading them

Treat every claim as a reviewer's claim, not a fact. Two examples from these
very reports:

- Claude asserted the `acquire_lease` docstring documents NFS dropping a live
  holder's lock. It does not; it documents the opposite, with measurements.
- Both reports are explicit about what they could NOT establish, and those
  passages are the most valuable parts. Neither found an apples-to-apples
  comparison against another system on unattended multi-day runs, and neither
  found evidence that autonomous memory maintenance is a solved problem.

The Astra files are trimmed to their analysis; the raw transcripts echoed the
repository back and were mostly our own files.
