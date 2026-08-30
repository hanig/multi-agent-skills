# multi-agent-skills

Portable Claude Code skills for dispatching autonomous work onto HPC clusters
and refusing to call it done without evidence. Installable across laptops and
login nodes with one script.

**Private.** Contains cluster hostnames, partitions, account names, storage
layouts, and measured environment probes.

---

## The one idea

A scheduler reporting `COMPLETED` is not evidence that work was produced.
Neither is a zero exit code, a "done" line in a log, or an agent's own claim of
success.

Python that catches an exception and returns 0, a pipeline stage that emits a
header-only table, a training run that hit its wall-clock limit, an agent that
reports "finished" because it stopped: all of these look like success to every
tool that reports on them.

So every skill here declares an **artifact contract**: a verifiable statement of
what "done" means, written *before* execution and checked afterwards by
something that did not do the work. The idea comes from code review, applied to
schedulers and workflows instead of to commits.

The corollary is the reason this repo exists at all. An agent reporting "done"
is a bare self-assertion, and a self-assertion is exactly what these tools were
built to refuse. Coordination without adjudication just moves the unverified
claim one layer up.

---

## Quick start

```bash
git clone git@github.com:hanig/multi-agent-skills.git
cd multi-agent-skills
./install.sh          # copies into ~/.claude/skills/
./bin/doctor          # what is installed, from where, and does it still run
```

Then, on a cluster, in an empty directory or a half-finished repo:

```
Use the hanig-project skill to start a project here.
```

That is the front door. It surveys the host, interviews you only about what
inspection cannot answer, writes a plan, shows you the project overview and
every issue title for approval, files them in Linear, and dispatches.

---

## The five skills

| Skill | One line |
|---|---|
| `hanig-project` | The front door: survey, interview, plan, approve, file, dispatch, report. |
| `hanig-swarm` | The coordinator: validate a DAG, isolate each attempt, dispatch, advance, judge. |
| `hanig-verified-workflow` | Declare and verify what "done" means for one batch job. |
| `hanig-review-gate` | Adversarial multi-model review of code and of the claims made about it. |
| `hanig-portable-handoff` | Capture and resume run state across machines. |

All are prefixed `hanig-` so they can never collide with an Arc org-managed
skill name.

### hanig-project

Gets you from "I am on a server with an intention" to units of work that are
dispatched, tracked, and verifiable.

```sh
python3 scripts/survey.py --repo . --out .swarm/survey.json

python3 scripts/tickets.py draft   plan.json --team Arc --out .swarm/tickets.json
python3 scripts/tickets.py check   plan.json .swarm/tickets.json
python3 scripts/tickets.py approve .swarm/tickets.json --approver hani

python3 scripts/report.py  .          --out report.html      # ends every run
python3 scripts/report.py  . --fragment --out frag.html      # to publish
```

`draft` takes the plan positionally and accepts `--brief` and `--autopilot`.
`approve` requires `--approver`: an approval with no one attached to it is not
an approval.

`survey.py` reports the host, python, schedulers, partitions, accounts, whether
`--mem` is required here, free disk, and for a repo: git history, size, language
mix, existing docs, and whether a swarm project already exists. **Every fact in
it is a question you must not ask the human.**

It is bounded three ways (entry count, depth, wall-clock) because an unbounded
walk of a cluster home directory hangs. It neutralises `core.fsmonitor`,
`core.hooksPath` and `core.sshCommand` before running git, because a surveyed
repo is untrusted input and its config should not execute. It redacts anything
that looks like a credential, after leaking a git token once.

`tickets.py` drafts the Linear project and issues, requires **one** explicit
approval before anything is transmitted, and re-arms that approval if the unit
digests change underneath it. Default team is `Arc`. Full automation requires
the literal phrase `swarm autopilot`; absent it, approval is always required.

**The stopping condition is the whole point.** The interview ends when the plan
can RUN, not when the interviewer runs out of prepared questions. Every value
dispatch needs (input paths and globs, output destinations, account, partition,
any config file the command reads) must be settled before finishing. This is
enforced, not remembered: `swarm.py validate` refuses a plan whose declared
inputs are empty, still placeholders, or match nothing.

### hanig-swarm

The coordinator. Roughly 2,200 lines in `swarm.py`, 1,300 in `unit.py`.

```sh
python3 scripts/swarm.py validate plan.json
python3 scripts/swarm.py run      plan.json --state-dir .swarm [--dry-run]
python3 scripts/swarm.py status   plan.json --state-dir .swarm [--json]
python3 scripts/swarm.py advance  plan.json --state-dir .swarm
python3 scripts/swarm.py outbox            --state-dir .swarm [--all] [--json]
python3 scripts/swarm.py outbox --state-dir .swarm \
        --record-receipt KEY --ref ARC-171     # after the tracker confirms
python3 scripts/swarm.py promote  plan.json --unit ID --approve --approver hani

python3 scripts/unit.py allocate --root ROOT --task ID --kind slurm \
                                 --command CMD --output PATH [--gpu-hours N]
python3 scripts/unit.py bind  <unit_dir> --job-id 12345
python3 scripts/unit.py check <unit_dir> [--json]
```

`run` and `advance` take `--max-new-dispatches` to throttle a fan-out, and
`--accept-plan-change` to proceed when the plan digest no longer matches the
state directory. `promote` takes `--accept-weak-evidence`, which is exactly the
kind of thing that should have to be typed out in full.

`converge.py check` answers whether a training run converged or merely stopped.
It was ported out of a deleted skill before that skill was removed, because it
was the one capability with no replacement anywhere.

### hanig-verified-workflow

```sh
python3 scripts/contract.py init   --command CMD --output PATH [--input PATH] \
                                   [--predicate EXPR] [--write-scope DIR] \
                                   [--require-production-evidence]
python3 scripts/contract.py submit <run_dir> [--sbatch-arg ARG]
python3 scripts/contract.py record <run_dir> --exit-code N [--job-id ID]
python3 scripts/contract.py check  <run_dir> [--json] [--watchdog SECS]
```

Distinguishes `SCIENTIFIC_PASS` from `TECHNICALLY_COMPLETE`. Its Slurm state
machine is the ancestor of `unit.py`'s.

### hanig-review-gate

Sends the diff **and the specific claims made about it** to models that did not
write the code, each prompted to refute rather than approve. A reviewer that
cannot decide is instructed to refute, because a false "looks good" costs far
more than one more look.

```sh
python3 scripts/review.py --kind plan --plan design.md
python3 scripts/review.py --kind implementation --staged --escalate --round 2
python3 scripts/review.py --list          # live provider probe
```

Panels, from `reviewers.json`:

| profile | membership | use |
|---|---|---|
| `plan` | deepseek-v4-pro, luna | acceptance criteria and designs, before code exists |
| `fast` | deepseek-v4-pro, luna | cheapest implementation panel, first tier of `--escalate` |
| `standard` | fast + kimi-k2.7-code, kimi-k3, glm-5.3 | the usual implementation panel |
| `deep` | everything, including sol at `xhigh` | reached only via `--escalate` |

**Two contrasting models for a plan, never escalated.** A third adds agreement,
not insight. That is measured, not assumed. `reviewers.json` carries routing
only (endpoint, model id, effort, profile membership) and deliberately no
quality scores, because a stale ranking is worse than none. Availability is
probed live by `--list`; a key being set is not availability.

`committee.py` is the other half of this skill: a persistent two-member
committee that plans, then reviews its own plan's implementation
(`open`, `ask`, `review`, `show`). It is modelled on Shreshth's
`paseo-committee`.

`PROTOCOL.md` states the full model. The enforceable parts are enforced by
`review.py` rather than left to memory, because these rules were written down
once and drifted anyway.

### hanig-portable-handoff

```sh
python3 scripts/handoff.py capture run1 run2 --out handoff.json
python3 scripts/handoff.py resume handoff.json [--base DIR] [--watchdog SECS]
python3 scripts/handoff.py memory .
```

A handoff records identities and pointers. It never copies data, and it never
decides anything a verifier already decided.

| code | state | meaning |
|---|---|---|
| 0 | `HANDOFF_CLEAN` | code and inputs match, pointers resolve here |
| 1 | `HANDOFF_DRIFTED` | code, input identity, or an artifact size differs |
| 2 | `HANDOFF_ELSEWHERE` | code matches; an artifact is unreachable from here |
| 3 | `HANDOFF_MALFORMED` | the handoff file is unusable; re-capture |

---

## The invariants

These are the load-bearing decisions. Most were paid for with a real failure.

**Isolation replaces attribution.** Observation shows that an artifact changed,
never which process changed it. So each attempt gets an exclusive, never-reused
write root, created with `mkdir(exist_ok=False)` as enforcement rather than as
convention. Given isolation, a cheap predicate ("is there output under this
root?") becomes conclusive, and the machinery built to *prove* which process
wrote a file was answering a question that no longer needed asking.

**A unit is the retry boundary.** A retry starts in a fresh, empty attempt
directory, so a retry redoes the whole unit. The quantity that matters is
therefore maximum unrecoverable work, not unit size, and it is the human's to
set: what is the most work you are willing to repeat after one interruption?
Absent an answer, the default is one independently executable shard per unit.

`retry.mode: "resume"` is **refused** today. A checkpoint counts only if it
survives the failed attempt, is made available to the new attempt, has an atomic
completion marker, is validated before reuse, actually causes completed work to
be skipped, and still yields the complete declared outputs. An engine that "can
resume in principle" does not count.

**Closure authority is fixed by kind, and is not configurable.** Configurable
means configurable wrong.

| kind | what closes it |
|---|---|
| `code` | a merged PR |
| `slurm` | a predicate receipt |
| `pipeline` | a predicate receipt |

**Enforce only declared facts.** Never infer exposure from a partition name, a
walltime, a `gpu_hours` figure, or a fan-out width. If the plan does not declare
it, the coordinator does not guess it.

**Structural separation over semantic.** `<attempt-dir>/repo` and
`<attempt-dir>/artifacts` are separated by location, not by banning file
extensions.

**The coordinator has no network imports.** It writes intents to an idempotent
outbox; a session that has MCP drains them. A tracker that is unreachable can
therefore never block dispatch, and draining twice cannot file an issue twice.

Acknowledgment is an **attestation, not evidence**. The coordinator has no
network imports, so it cannot ask the tracker whether ARC-171 really closed;
anyone who can run the command can write any reference. The record establishes
only that an identified writer said so at a given time, which is the same class
of claim as an agent reporting "done". The mechanism stays, because across a
deliberate network gap there is no other one, and the label carries the
weakness: every display says attested, never verified.

Status is derived from those attestations, never stored:
`acknowledged`, `conflict` (two receipts, two refs, one intent), or
`unacknowledged`. The last does **not** mean "not filed": it means this machine
has no confirmation either way, and claiming otherwise asserts knowledge it does
not have. A false acknowledgment is strictly worse than a missing one, because
re-draining is safe and un-filing is not.

**Non-vacuity by mutation.** A test that passes proves nothing until reverting
the fix makes it fail. A broken test once gave a false negative on a CRITICAL
finding: bad quoting meant a git filter never installed, and only a positive
control revealed the filter did fire.

**Correct the persisted state, not only the forward path.** Three consecutive
review rounds fixed how a state would be computed next time and left the
already-written state wrong on disk.

---

## Data model

A plan is JSON with a `units` list. Fields the coordinator reads:

| field | meaning |
|---|---|
| `id` | unique unit id, used for the attempt directory and the env var |
| `kind` | `slurm`, `pipeline`, or `code`; sets closure authority |
| `needs` | ids this unit depends on; forms the DAG |
| `inputs` | declared inputs; validated for existence and non-placeholder-ness |
| `outputs` | declared outputs; the done-predicate checks these |
| `command` | what to run (`slurm`, `pipeline`) |
| `prompt`, `provider`, `model` | what to dispatch (`code`) |
| `sbatch` | scheduler flags **as a list**, parsed for the declared partition |
| `max_attempts` | retry budget; the attempts list *is* the budget |
| `retry` | `{"mode": "restart"}`; `resume` is refused |
| `promote_to` | where verified outputs are promoted after closure |
| `runtime` | what this executes in, and what checks it (see below) |
| `pool`, `gpu_hours`, `env` | declared resource facts |

**`needs`, `inputs`, `outputs` and `sbatch` must be JSON lists.** A string is
refused, because the code that reads them iterates character by character: for
one release `"sbatch": "--partition=cpu_batch"` validated clean, reported
"declares no partition", and silently ran on the cluster default. Write
`["--partition=cpu_batch"]`.

**Every `slurm` and `pipeline` unit must declare a `runtime`**, either inline,
as a reference into the plan's `runtimes` catalogue, or the literal `"none"`
meaning it runs only tools the base image guarantees. A profile declares
`resolution` (one of direct, path, conda, container, module, uv, wrapper),
an `entrypoint`, and `verified_by`: `canary:<unit-id>`, `preflight`, or
`unverified:<why>`. A canary must be an ancestor of every unit it verifies, and
a unit may not be its own canary.

The validator deliberately does NOT parse the command or stat the entrypoint.
There is no reliable static shell chokepoint (`srun python`, `conda run`,
`apptainer exec`, `uv run` and `bash -lc` with modules are all legitimate and
unparseable), and a submit-host stat is an observed submit-host fact: concluding
from it that the path resolves on a compute node would be inferring an
undeclared fact, with both a false-accept and a false-refuse mode.

`validate` refuses a plan that cannot run. Refused: empty inputs, placeholders
(`<...>`, `TODO`, `FIXME`, `TBD`, `...`), and absolute paths or globs matching
nothing. Deliberately allowed, to avoid blocking honest work: an input produced
by an upstream unit, a relative path resolved against a working directory the
validator cannot know, and a unit declaring no inputs at all.

Dependencies reach a unit's command as shell-quoted environment variables:
`SWARM_DEP_<UNIT_ID>`, `SWARM_UNIT_ID`, `SWARM_UNIT_DIR`.

### Unit states

```
DONE 0   RUNNING 1   FAILED 2   PREEMPTED 3   INCOMPLETE 4   NEEDS_HUMAN 5
USAGE_ERROR 64
```

`READY_FOR_PR` is a coordinator-level state for `code` units whose declared
outputs exist but whose PR has not merged. No mechanism can leave that state
today, so `status` explains it explicitly rather than letting a DAG stall
silently.

Per-attempt files: `unit.json`, `events.jsonl`, `receipt.json`.

---

## End to end

1. **Land on the cluster.** `git clone`, `./install.sh`, `./bin/doctor`.
2. **Open a project directory** and start Claude Code there.
3. **Survey.** The host, scheduler, partitions, accounts and repo are read, not
   asked about.
4. **Interview.** One question at a time, each with a recommended answer, only
   about what inspection cannot settle. Ends when the plan can run.
5. **Plan.** Units with declared inputs, outputs, kinds and retry exposure.
   `validate` refuses anything that cannot dispatch.
6. **Approve, once.** The project overview and every issue title are shown.
   Nothing has been transmitted yet. Full automation needs `swarm autopilot`.
7. **File.** Intents go to the outbox; a session with MCP drains them into
   Linear (team `Arc`), where the project can be connected to GitHub for PRs.
8. **Dispatch and advance.** Each attempt gets an exclusive write root; the
   scheduler job is bound to the attempt.
9. **Close on evidence.** A predicate receipt for `slurm` and `pipeline`, a
   merged PR for `code`. Never on a self-report.
10. **Report.** `report.py` assembles the run from `plan.json`, the
    coordinator state and every `receipt.json`, and publishes it. A run is not
    finished until it has one.

Worked example: `vitrine-provenance-manifest` ran 2,406 files and 1.42 TiB
across five Linear issues, each closed only on a predicate receipt.

---

## Install

```bash
./install.sh [--prefix DIR] [--mode copy|link] [--only NAME] [--dry-run]
             [--force] [--uninstall]
```

Copy is the default deliberately. A symlink into a live checkout breaks when the
checkout sits under a synced folder, when a branch switch silently mutates every
installed skill, and when the checkout is mid-update. Use `--mode link` only
when developing a skill.

The installer validates frontmatter and script syntax before touching the
destination, aborts on a name collision with an org-managed skill, prunes skills
that are no longer shipped, and refuses to replace a directory it did not
install.

**`--uninstall` deletes only what this repo installed**, determined by our own
marker file or by a symlink whose target is inside this checkout. It is written
that way because an earlier version inferred ownership from shape (`[ -L "$d" ]`,
"any symlink is ours") and deleted two unrelated skills off a cluster. A
symlink is not evidence that we created it. Read a destructive command and
dry-run it before trusting it, including one you wrote yourself, and especially
then, because familiarity feels like knowledge.

---

## Deployment targets

Measured 2026-08-25 with `bin/probe.sh`. Full output in `docs/probes/`.

| | chimera | lambda | andromeda |
|---|---|---|---|
| host | `chimera-login` | `vci-steady-state-login-001` | `ac-gefion-login-0` |
| user | `hani` | `hani` | `hgoodarzi` |
| `$HOME` | `/home/hani` (nfs4) | `/home/hani` (nfs) | `/mnt/weka/home/hgoodarzi` (wekafs) |
| python3 | 3.10.12 | 3.12.3 | 3.10.12 |
| Slurm | 25.11.0 | 25.05.0 | 24.11.5 (SUNK) |
| `sacct` | works | works | works |
| org skills | none | none | none |

### Gotchas that bite

- **Org skills reach none of the clusters.** Skills here must be self-sufficient.
- **`claude` and `node` live behind conda/micromamba prefixes loaded by
  `.bashrc`.** A non-interactive `ssh host 'command -v node'` reports ABSENT and
  is wrong. Check with `bash -lic`.
- **Usernames differ across clusters.** Never hardcode `$USER` or a home path.
- **git is 2.34+ on the clusters but 2.23 on the Mac.** Avoid `git init -b`,
  `git switch`, and GNU-only `sed`/`readlink` flags.
- Use `chimera-login`, not `chimera`: the latter carries `RemoteCommand sh_dev`
  and rejects a passed command.
- Lambda's `/tmp` is not writable; stage to `$HOME`. Andromeda needs Tailscale up.
- **`sacct` defaults to today** and needs `-S` for anything older. A `CANCELLED`
  job can carry exit code `0:0`. Slurm reuses job ids, so a job id alone does not
  identify a run.
- **Partition QOS caps are real.** On chimera, partition `cpu` maps to QOS
  `cpu_interact` with `MaxJobsPU=2`; a fan-out belongs on `cpu_batch`
  (20 concurrent, 200 submitted).

---

## Tests

```bash
python3 -m unittest discover -s tests
```

621 tests, standard library only, no network and no cluster required. Green on
macOS 3.10.16 and on all three clusters (3.10.12, 3.12.3, 3.10.12).

| file | lines | classes |
|---|---|---|
| `test_contract.py` | 2077 | 28 |
| `test_outbox.py` | 2654 | 39 |
| `test_review.py` | 1628 | 37 |
| `test_project.py` | 1093 | 15 |
| `test_swarm.py` | 854 | 8 |
| `test_handoff.py` | 612 | 8 |

Two failure modes this suite has actually suffered, both worth knowing:

A `__main__` block placed above later class definitions silently hid 13 security
tests in `test_project.py` and 9 classes in `test_outbox.py`. The suite was
green and was not running them. Keep the guard at the bottom.

Guards that match their own source produced three separate false passes: a grep
matching the comment explaining an absence, and a `__main__` check finding its
own string literal. A test must not be satisfiable by the text of the test.

---

## Layout

```
install.sh                  copy-based installer (--mode link for development)
bin/doctor                  what is installed, from where, and does it still run
bin/probe.sh                read-only environment probe for a new host
bin/make-release            release packaging
skills/hanig-project/       survey.py, tickets.py
skills/hanig-swarm/         swarm.py, unit.py, converge.py, swarm-cron.sh
skills/hanig-review-gate/   review.py, committee.py, reviewers.json, PROTOCOL.md
skills/hanig-verified-workflow/  contract.py
skills/hanig-portable-handoff/   handoff.py
tests/                      stdlib unittest
docs/probes/                measured environment data per cluster
MEMORY.md                   portable project state; read this first
```

### Docs

| file | what it is |
|---|---|
| `docs/PLAN.md` | full design, and where two proposals disagreed |
| `docs/plan-swarm.md` | 67 acceptance criteria for the swarm |
| `docs/plan-swarm-sol-variant.md` | the contrasting design, kept for the disagreements |
| `docs/tracker-outbox.md` | the network-free intent outbox |
| `docs/first-real-workloads.md` | what the first real runs exposed |
| `docs/audit-protocol-enforcement.md` | which written rules are actually enforced |
| `docs/proposal-refusal-chokepoint.md` | where a refusal has to live to be real |
| `docs/scenario-mach1-zebrafish.md` | a worked scenario |

---

## Deleted, recoverable from history

`hanig-verified-training` and `hanig-reproducible-result`, plus the conformance
and symmetry suites, removed 2026-08-28.

A committee concluded that isolation replaces attribution, so the machinery
built to prove which process wrote a file was answering a question that no
longer needed asking. `traincontract.py`'s convergence evaluator was ported to
`hanig-swarm/converge.py` **before** the deletion. `result.py` went because
figures and tables are not a swarm unit kind. The two suites expired with their
subject: a cross-tool conformance lock needs several tools, and a symmetry lock
needs a twin.

---

## Coordination: Paseo, and the fusion

These verifiers adjudicate evidence. They answer "was the work actually done"
and refuse a self-assertion. They do not dispatch work.

Shreshth's `multi-agent-skills` (extracted to `~/paseo-multi-agent-skills`) is
the other half: agent coordination on the Paseo daemon, covering who does the
work, in what isolation, notified how. Its eleven skills install alongside these.

**As of 2026-08-28 the two are being fused deliberately.** An earlier note in
MEMORY.md recorded "do not port the coordination machinery" as a settled
premise. It was not settled; it was a self-narrowed brief, and it was overturned.
The reason it gave (do not rebuild a fleet and message bus) expired when Paseo
was installed, because there is nothing left to rebuild.

The fusion's point: his agents coordinate but cannot prove anything, and an
agent reporting "done" is the bare claim these tools exist to refuse. Ours prove
but cannot dispatch. Neither repo has the combination.

**A conclusion two models agree on is still a proposal.** "Settled" requires the
person whose project it is. Consensus between AI drafts does not get written
into MEMORY.md as premise.
