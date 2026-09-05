---
name: hanig-portable-handoff
description: >-
  Capture and restore durable research state across machines, clusters, and
  sessions. Use when picking work back up on a different host, resuming after a
  break, or being asked "where was I?", "what was running?", "is this the same
  code I left?" — and to regenerate the factual sections of a project's
  MEMORY.md from real state rather than from recollection. Not for moving data
  (it records pointers, never copies), not for credential transfer, and not for
  deciding whether a run succeeded (use hanig-verified-workflow or
  hanig-verified-training, whose verdicts this records).
---

# hanig-portable-handoff

## Host capability boundary

Read the active host's project instructions and preserve `AGENTS.md`,
`CLAUDE.md`, and `MEMORY.md` as repository context, not host-owned state.  Any
host may consume a handoff; `capture`, `resume`, and factual `MEMORY.md`
refresh require usable local Python, Git, and referenced paths.  If one is
unavailable, report that bounded limit and retain the handoff unchanged; never
copy credentials, provision services, or alter approvals to make a resume
appear clean.  See `docs/agent-compatibility.md` for the shared contract.

Context switching between institutions and clusters loses two things: which
code was running, and which of your runs were unfinished. Both are recoverable
from state already on disk, so neither should depend on remembering.

## The rule

**A handoff records identities and pointers. It never copies data, and it never
decides anything a verifier already decided.**

## Run from the loaded skill, not a checkout

Before running a command, set `HANIG_PORTABLE_HANDOFF_DIR` to the directory
containing the `SKILL.md` instance this agent actually loaded. This is an
ordinary shell variable rather than agent-specific interpolation, so it works
with Claude, Codex, OpenCode, and Pi; it may contain spaces. Commands retain
your current project directory, so relative run, input, and output paths below
still mean paths in that project.

```bash
export HANIG_PORTABLE_HANDOFF_DIR="/path/to/loaded/hanig-portable-handoff"
H="$HANIG_PORTABLE_HANDOFF_DIR/scripts/handoff.py"

python3 "$H" capture run1 run2 --out handoff.json    # before you leave
python3 "$H" resume handoff.json                     # where you land
python3 "$H" memory .                                # regenerate MEMORY.md facts
```

## `resume` exit codes

| code | state | what it means | what to do |
|---|---|---|---|
| 0 | `HANDOFF_CLEAN` | code and inputs match, pointers resolve here | continue |
| 1 | `HANDOFF_DRIFTED` | code, input identity, or an artifact size differs | reconcile before running |
| 2 | `HANDOFF_ELSEWHERE` | code matches; an artifact is not reachable from here | go to that host, or `--base` |
| 3 | `HANDOFF_MALFORMED` | the handoff file itself is unusable | re-capture |

Checked in that order, first match wins, so they cannot overlap. Never a
boolean: "the code differs" and "I cannot see the data from here" need
different actions from you, and collapsing them would hide which one you have.

**A different host is not drift.** Captured on lambda, resumed on andromeda,
same commit, shared filesystem: that is `CLEAN`, and the host difference is
reported. **A changed mtime is not drift either** — a checkpoint restored from
backup with fresh mtimes and identical bytes must not send you back to the
queue. Only existence and size feed the verdict.

## What `capture` will not tell you

It records the verdict from a verifier's receipt **only when that receipt names
the same contract instance**. If you ran `init --force`, the old receipt is
still on disk and describes a contract that no longer exists — so the verdict
comes back null with that reason, not as a pass. Neither verifier stamped its
receipt with a contract id until this skill was designed and the gap showed up;
the fix went into them first.

It also never runs `check`. A handoff records what was decided, and re-deciding
would silently replace the state you are trying to preserve.

## Credentials

The contract is recorded twice: a **digest of the original** for comparison, and
a **redacted copy** for reading. Nothing you or a log will ever see carries a
credential-shaped value, and because the digest is taken before redaction, a
redacted field cannot make an honest resume report drift.

`CONDA_PREFIX` is kept. It is reproducibility information, not a secret, and
stripping it would destroy what the contract exists to record.

## `memory`, and the standing MEMORY.md rule

`CLAUDE.md` requires a `MEMORY.md` per project and warns that a stale one is
worse than none — which today depends on someone remembering. This generates
the factual half from real state and never touches the rest:

- commits in range, uncommitted paths, open contracts and their attributed
  verdicts, between `<!-- handoff:facts:begin sha=... -->` and its end marker
- **never** decisions, blockers or recommendations; those are yours, and the
  command refuses to write a block that even mentions such a heading

**Scoped by commit sha, not by time.** A restored file or a skewed clock makes a
time boundary omit or repeat commits silently, and stamping a fresh timestamp
each run makes determinism impossible. The marker carries the sha the facts were
generated from, and the command checks that sha is an ancestor of HEAD first:
after a rebase it is not, and `git log <sha>..HEAD` then returns the entire
branch rather than failing. When ancestry breaks it regenerates in full and says
why.

Deterministic given its inputs — HEAD, the recorded sha, the receipts on disk.
Not "no diff on an unchanged repo": the first run has no marker, so it
bootstraps with recent history and legitimately differs from the run after it.

## Design record

`docs/plan-portable-handoff.md` carries the plan and the thirteen acceptance
criteria, plus what three plan reviews rejected before any code was written.
Worth reading before changing this: one review found a defect in two already
shipped skills, and two others caught contradictions introduced while fixing the
round before.

## What this does not do

Move data. Transfer credentials. Run a daemon. Decide whether a run succeeded.
