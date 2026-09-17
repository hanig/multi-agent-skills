# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Private repository: it carries cluster hostnames, partitions, account names and measured host probes. Do not publish any of it.

`MEMORY.md` is a dated snapshot, not live truth. It holds model routing and the reasoning behind the design; `docs/plan-field-reports.md` is the live plan, and `README.md` is the CLI reference for every skill. `tests/test_docs_truth.py` pins their suite counts and reviewer rosters to discovered tests and configuration. Routing truth is the `enabled` flags in `skills/hanig-review-gate/reviewers.json` plus `models.json`, never a roster or count quoted in prose. Verify any owed item against the tree before acting on it.

## Commands

<!-- docs-truth:suite-count -->
Full suite: 1580 tests on 2026-09-17.

```sh
python3 -m unittest discover -s tests            # full suite, ~8 min
python3 -m unittest tests.test_swarm             # one module
python3 skills/hanig-swarm/scripts/swarm.py schema           # every unit field, before writing a plan
python3 skills/hanig-review-gate/scripts/review.py --kind implementation --staged --round 1
./install.sh --dry-run --json --agent claude     # plan an install, changes nothing
./bin/doctor --json                              # what is installed, from where, still runs
./bin/probe.sh                                   # read-only environment probe for a new host
python3 tests/native_agent_validation.py         # release-time host harness, not a unit test
```

Automatic install selection is dead on this host: `./install.sh --dry-run --json` with no selector exits 2 with "no supported agents were detected" because the adapters allowlist exact CLI releases and this host has claude 2.1.273, codex 0.154.0 and pi 0.84.3. Name targets with `--agent`. A real install here also needs `--allow-vendored-shadow`, because four `paseo*` destinations already exist without this repo's ownership record, and permanently needs `--allow-org-shadow`, which is the separate Claude Science twin case. Copy is the default deliberately; `--mode link` is for developing a skill.

No linter, no formatter, no build. Standard library only, and three floors exist for three reasons: the import floor is 3.7 in `contract.py` and `handoff.py` and 3.8 in the swarm, review and installer code, while the host floor is 3.10, because andromeda and chimera both run 3.10.12. Release validation pins 3.9 in both jobs, so 3.9 is the binding constraint for anything a test imports: `sys.stdlib_module_names` is 3.10+ and reading it unguarded turned both CI runners red while passing on a 3.12 host. Test against 3.10, not the newest. A third-party import in a skill script or test is a defect.

Scheduler-isolation fixtures replace PATH rather than prepending to it, so a
tool deliberately absent from a fixture cannot leak in from a real Slurm
installation on the host. `tests/test_stdlib_imports.py` parses every authored
skill script and installer module and rejects imports outside the standard
library or the scanned repo-local modules.

## Structure

`lib/` is the installer: `skill_installer.py` plans and `skill_lifecycle.py` mutates, with discovery and filesystem writes kept at the edges so an unsafe request is refused before any destination is touched. It installs skill directories only. It deploys neither Paseo, the bus executable, `models.json`, routing state, credentials nor connectors, and `--uninstall` deletes only what an exact copy marker or a binding sidecar proves this repo installed.

`skills/` holds two populations, and the `hanig-` prefix is the classifier both `install.sh` and `bin/doctor` read off the tree. Five authored bundles (`hanig-project`, `hanig-swarm`, `hanig-verified-workflow`, `hanig-review-gate`, `hanig-portable-handoff`) are ours. Eight vendored bundles (`paseo*`, `pi-fleet`, `agent-bus`, `start-a-sprint`) are upstream verbatim and `--uninstall` leaves them alone.

The bus executable in this checkout is `bin/bus`, reached from elsewhere as `$MULTI_AGENT_SKILLS_CHECKOUT/bin/bus`. The `~/.agent-bus/bin/bus` path hardcoded in the vendored skills describes upstream's install layout, not ours; never create that path to make a vendored instruction true. `bin/bus` carries one deliberately tracked local patch pinned by `tests/test_bus_models.py`.

## Architecture

The pipeline runs one direction. `hanig-project/survey.py` reads the host and repo, an interview settles only what inspection cannot, and the result is a `plan.json` whose units declare `inputs`, `outputs`, `kind` and retry exposure. `swarm.py validate` refuses a plan that cannot dispatch, `run` and `advance` allocate attempts and submit, and `report.py` assembles the verdict from `plan.json`, coordinator state and every `receipt.json`. A run is not finished until it has a report, and the report is built from evidence, never from a narrative of what happened.

`validate` checks declared facts, not shell behavior: it deliberately does not parse commands or stat entrypoints, so a clean validate says nothing about whether a path resolves on a compute node. That is what a declared `runtime` plus a canary or allocation-local preflight is for.

**Nothing judges except `unit.py check`.** `run` and `advance` allocate, submit, bind and act on its exit code, which is the only input. The rest of judging lives in `worktree.py` for the code half, `verify.py` for an authorized verifier, and `converge.py` for a declared metrics gate, where an unmet criterion becomes `NEEDS_HUMAN`, closes no ticket and releases no dependent. Do not move judging into the coordinator.

**Isolation replaces attribution, not execution evidence.** Each attempt gets an exclusive, never-reused write root created with `mkdir(exist_ok=False)`, which is what makes a cheap done-predicate conclusive. That predicate has a premise: the artifact was not already there. DONE therefore needs the declared outputs, the coordinator-pinned per-attempt pre-dispatch artifact basis whose absence fails closed (`tests/test_pre_dispatch_artifacts.py`), and the kind's execution evidence, which for `slurm` is a terminal-OK sacct row owned by this attempt. Never restate the predicate without the basis clause. Exclusivity is a trusted-writer convention, not an OS boundary; same-UID writers and live descendant processes are declared limits, not closed ones.

What is forbidden is attribution by observation, proving which process wrote a file, and three plans died on it. Per-attempt provenance is a different thing and is required: `worktree.py:24` says so of `judge_artifacts`, which never asks who wrote a file, only whether what was digested before the attempt differs now. The pre-dispatch artifact digest, the anchored launch intent and the inode-bound worktree identity all answer that question. Do not delete one for resembling the forbidden machinery.

**Authority lives in coordinator state, never in agent-writable files.** The per-attempt launch record and the receipts are audit-only, pinned by `tests/test_record_is_not_authority.py`. Reading a trust-deciding value back out of them is the single move that produced four separate review findings. `coordinator_paths.py` keeps state and attempt roots outside every operated Git worktree, and an explicit in-repo path is refused before its parent is created.

**Closure authority is fixed by kind and is not configurable.** `code` closes on a merged PR whose head must equal the head the coordinator independently judged the attempt to have produced; `slurm` and `pipeline` close on a predicate receipt. Keep the evidence labels exact. A tracker acknowledgment and a merge observation are attested, never verified. An authorized verifier receipt is a different class, admissible only when it is authorized by policy read from the anchored base commit, pinned to the verifier's content digest, and bound to the head and claim it ran under, so candidate code cannot authorize its own verifier. `REVIEW_PASS` means the named reviewers failed to refute the claims, not that anything was proved.

**A retry is a fresh attempt.** `max_attempts` defaults to 1, a retry redoes the whole unit in a new empty attempt, and `retry.mode: "resume"` is refused until a checkpoint survives the failed attempt, has an atomic marker, is validated before reuse, actually skips completed work, and still yields the complete declared outputs. Bounded `continuation` nudges stay inside their attempt and are neither a retry nor a unit state.

**One coordinator node per plan.** Exclusion is an advisory `flock` on the state directory, which the kernel frees when the holder dies, so there is nothing to steal and no TTL. Never add a heartbeat or a TTL: that reintroduces exactly the stolen-lock failure the design avoids. The measured exclusion trials were all same-node, so cross-node exclusion is uncertified, and output claims arbitrate only among coordinators sharing one run root. Unknown scheduler liveness is not permission to release a claim.

**The swarm coordinator has no network imports.** `swarm.py` must not import `review.py` or `committee.py`: those are network-capable programs the operating session runs with its own credentials. Tracker work goes to an idempotent outbox that a session with MCP drains, so an unreachable tracker cannot block dispatch and draining twice cannot file twice. An intent with no receipt reads `unacknowledged`, which means this machine has no confirmation either way and never means the issue was not filed. A false acknowledgment is strictly worse than a missing one, because re-draining is safe and un-filing is not. `child_environment.py` strips 21 exact names from every coordinator child, covering the Anthropic, OpenAI and OpenRouter keys, GitHub and AWS credentials and `SSH_AUTH_SOCK`, with suffix matching deliberately abandoned after a regression. It is not a general secret filter: Paseo's daemon supplies the worker its own keys and `$HOME` passes through. That residue is accepted and recorded; do not re-file it as a defect.

**Outward-facing actions are gated, each once.** Filing needs one explicit approval with the project and every issue title shown first, `promote` refuses without `--approver NAME`, and the literal phrase `swarm autopilot` is the only whole-run bypass. An approval with nobody attached to it is not an approval.

`needs`, `inputs`, `outputs`, `sbatch` and `requires_verification` must be JSON lists, and `validate` raises `PlanError` naming the field when one is a string. The rule exists because the readers iterate: for one release `"sbatch": "--partition=cpu_batch"` validated clean, reported "declares no partition", and ran on the cluster default. Preserve that refusal in every new reader.

Every authored skill locates its own programs through a variable, not the cwd: each sets `HANIG_<SKILL>_DIR` to the directory holding the `SKILL.md` this agent actually loaded, and `skill_paths.py` derives both that skill and its declared siblings from there. A deliberate mixed or link install sets `HANIG_SKILL_DEP_ROOTS` to the parent that holds the sibling; the resolver never searches an agent store. Those variables locate programs only, and plan, state, input and output paths stay relative to the project directory. Tests reach scripts by inserting the skill's `scripts/` directory on `sys.path`.

## Editing rules this repo paid for

Do not report a change complete, or assert that code works, until `hanig-review-gate` has run and passed. Exit 2 (`REVIEW_UNAVAILABLE`) and 3 (`REVIEW_PARTIAL`) are not a pass; if the gate cannot run, the change is unreviewed and must be described that way. The provider keys are exported from `~/.zshrc`, which a non-interactive shell does not source, so invoke through `zsh -ic` or export them explicitly. Reproduce a finding before acting on it, and keep an author off the panel reviewing its own work.

<!-- docs-truth:disabled-reviewers -->
Disabled reviewers: `sol`, `kimi-k3`.

Therefore `--escalate` adds `astra` only when it reaches the `deep` panel.

Never edit a vendored skill document. A needed correction goes in `docs/upstream-*.md` plus a test that pins the behavior, the way the `bin/bus` patch is pinned; routing decisions go in `~/.paseo/orchestration-preferences.json` (example in `examples/`). `tests/test_skill_capabilities.py::test_vendored_sources_are_not_rewritten_on_this_branch` diffs `skills/` against `origin/main` and fails on any vendored path. Agreement with `origin/main` is not upstream provenance, only a guard against editing here.

An invariant written in prose is not an invariant. If a docstring claims a test enforces something, run the test before believing it: three shipped capabilities turned out to be exercised by nothing, and `docs/audit-protocol-enforcement.md` catalogues which written rules are actually enforced.

Check every fix by mutation: revert the fix and the test must fail. A green suite after a change proves nothing on its own. When fixing an instance, sweep mechanically for its siblings instead of fixing the one in front of you. Correct the persisted state, not only the forward path: three review rounds in a row fixed how a value would be computed next time and left the wrong value on disk.

Keep `if __name__ == "__main__"` at the bottom of a test file. A `__main__` block above later class definitions once hid 13 tests in one file and 9 classes in another while the suite stayed green. A test must not be satisfiable by its own source text: a grep that matches the comment explaining an absence is a false pass.

## Host facts that bite

A plan written for one cluster does not run on another: of the 35 partitions across the three, almost none repeat, and `--mem` is required on lambda and nowhere else. Usernames and home paths differ across chimera, lambda and andromeda, so hardcoding either is a portability bug. `claude` and `node` live behind conda prefixes loaded by `.bashrc`, so a non-interactive `ssh host 'command -v node'` reports ABSENT and is wrong; use `bash -lic`. Use `chimera-login` rather than `chimera`, whose config carries `RemoteCommand sh_dev` and rejects a passed command. Lambda's `/tmp` is not writable, so stage to `$HOME`. git is 2.34+ on the clusters and 2.23 on the Mac, so avoid `git init -b`, `git switch` and GNU-only `sed`/`readlink` flags. Org-managed skills reach none of the clusters, so a skill here must be self-sufficient. `docs/clusters.md` is the partition and account reference and `docs/probes/` the measured data behind it.
