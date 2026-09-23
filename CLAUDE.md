# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Private repository: it carries cluster hostnames, partitions, account names and measured host probes. Do not publish any of it.

If you are driving a run rather than editing this repository, read `docs/orchestrator-mandate.md` first. It is the orchestrator's authority, it is owner-granted by merge rather than by conversation, and a new session must show the owner its enumerated grant and bounds and get confirmation before operating unattended. Until that confirmation you may read, survey, plan, run the gate and report, and you may not dispatch, merge, spend or mutate a tracker.

`MEMORY.md` is a dated snapshot, not live truth. It holds model routing and the reasoning behind the design, and its "next" list has gone stale while the code moved: it lists C11, a worktree per code attempt, as owed, and `swarm.py`, `worktree.py` and `tests/test_attempt_worktrees.py` ship it. `docs/plan-field-reports.md` is the live plan. `README.md` is the CLI reference for every skill, with the same caveat on its measurements. Routing truth is the `enabled` flags in `skills/hanig-review-gate/reviewers.json` plus `models.json`, never a roster quoted in prose. Verify any owed item against the tree before acting on it.

## Commands

<!-- docs-truth:suite-lower-bound -->
Full suite: at least 1,500 tests discoverable by unittest. Run the command below for the exact current total.

Only the standalone marker above declares the live suite total. A dated or
historical count in this canonical document must have the standalone marker
`<!-- docs-truth:historical -->` immediately above its prose paragraph. Other
documents reference this floor and keep their count-shaped prose advisory.

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

**Outward-facing actions are gated, each once.** Filing needs one explicit approval with the project and every issue title shown first, `promote` refuses without `--approver NAME`, and the literal phrase `swarm autopilot` is the only whole-run bypass. An approval with nobody attached to it is not an approval. Each once means once: these are `tickets.py`'s plan-to-tracker flow and `swarm.py promote`, and the gate is the program's, not a standing habit of consulting. An orchestrator operating under a confirmed `docs/orchestrator-mandate.md` already holds the grant for filing, updating and closing individual issues in this project, and re-asking per issue is a misreading of this line, not caution.

`needs`, `inputs`, `outputs`, `sbatch` and `requires_verification` must be JSON lists, and `validate` raises `PlanError` naming the field when one is a string. The rule exists because the readers iterate: for one release `"sbatch": "--partition=cpu_batch"` validated clean, reported "declares no partition", and ran on the cluster default. Preserve that refusal in every new reader.

Every authored skill locates its own programs through a variable, not the cwd: each sets `HANIG_<SKILL>_DIR` to the directory holding the `SKILL.md` this agent actually loaded, and `skill_paths.py` derives both that skill and its declared siblings from there. A deliberate mixed or link install sets `HANIG_SKILL_DEP_ROOTS` to the parent that holds the sibling; the resolver never searches an agent store. Those variables locate programs only, and plan, state, input and output paths stay relative to the project directory. Tests reach scripts by inserting the skill's `scripts/` directory on `sys.path`.

## Editing rules this repo paid for

Do not report a change complete, or assert that code works, until `hanig-review-gate` has run and passed. Exit 2 (`REVIEW_UNAVAILABLE`) and 3 (`REVIEW_PARTIAL`) are not a pass; if the gate cannot run, the change is unreviewed and must be described that way. The provider keys are exported from `~/.zshrc`, which a non-interactive shell does not source, so invoke through `zsh -ic` or export them explicitly. Reproduce a finding before acting on it, and keep an author off the panel reviewing its own work. `--escalate` walks `fast` → `standard` → `deep`, each tier adding only the reviewers the previous one did not run, and the first failing tier ends it. `sol` and `kimi-k3` are disabled, but `astra` is enabled and `deep`-only, so escalating does buy a reviewer — and buys it only when the cheaper tiers find nothing, which is why a `REVIEW_FAIL` at `fast` never reaches astra. Read the `enabled` flags and `profiles` lists in `reviewers.json` rather than this sentence; an earlier version of it said escalation bought nothing, which sent sessions past the one reviewer that had been added for them.

Never edit a vendored skill document. A needed correction goes in `docs/upstream-*.md` plus a test that pins the behavior, the way the `bin/bus` patch is pinned; routing decisions go in `~/.paseo/orchestration-preferences.json` (example in `examples/`). `tests/test_skill_capabilities.py::test_vendored_sources_are_not_rewritten_on_this_branch` diffs `skills/` against `origin/main` and fails on any vendored path. Agreement with `origin/main` is not upstream provenance, only a guard against editing here.

An invariant written in prose is not an invariant. If a docstring claims a test enforces something, run the test before believing it: three shipped capabilities turned out to be exercised by nothing, and `docs/audit-protocol-enforcement.md` catalogues which written rules are actually enforced.

**Test a guard through the thing that consumes it.** Emitted output is not evidence of delivery. A PostToolUse hook written here to force a tracker check printed to stdout and exited 0, which the harness shows only in transcript mode, so every reminder it produced reached a transcript and never the model — a no-op carrying a header that claimed the harness made forgetting impossible. False assurance is worse than no guard. The enforcing test must trigger the guard through its real harness and inspect what the consumer actually received; mutating delivery back to transcript-only must fail it. **Nothing generalises this**: no test requires a new guard to be exercised through its consumer, so the next one can ship hollow the same way.

**Bind a mutation to its intended repository and branch before making it.** `git checkout -q BRANCH && <heredoc>` died on a shell parse error, so the checkout never ran, and the edit, commit and push landed on `main` and bypassed the gate. The push was reported truthfully and the destination was never checked. Verify repo and branch before editing, and push an explicit source SHA to an explicit destination ref rather than trusting ambient state. **Nothing here enforces this**: no tool checks the current branch or the push target, so the same parse error would land on `main` again. What would close it is a branch-protection rule on `main` requiring review, which is a repository setting rather than a line in this file.

**Report status with the time it was observed, and never read activity as progress.** A dispatched agent sat at `running` for 34 minutes having executed nothing, blocked on permission requests nobody would answer, while its watcher reported health. Running, progressing, permission-blocked and last-observed are four different facts. A watcher must track work evidence and permission events separately from liveness, and a stale observation must never be presented as current. **Nothing enforces this either**; `swarm.py status` emits no timestamp and no unit age, which is ARC-691.

**Preserve a blocked attempt's work before any cleanup path runs.** Of two attempts blocked the same way on one day, one wrote a digested patch and one deleted its implementation "to leave the worktree clean"; nothing in the protocol decided which. Preservation must not be agent discretion: cleanup depends on a completed, restore-checked snapshot carrying base identity, content digest and validation result, and a failed preservation leaves the work in place. Preserving work neither resumes it nor makes it verified. **Unenforced today**: cleanup does not depend on a completed snapshot, which is ARC-682.

Check every fix by mutation: revert the fix and the test must fail. A green suite after a change proves nothing on its own. When fixing an instance, sweep mechanically for its siblings instead of fixing the one in front of you. Correct the persisted state, not only the forward path: three review rounds in a row fixed how a value would be computed next time and left the wrong value on disk.

<!-- docs-truth:historical -->
Keep `if __name__ == "__main__"` at the bottom of a test file. A `__main__` block above later class definitions once hid 13 tests in one file and 9 classes in another while the suite stayed green. A test must not be satisfiable by its own source text: a grep that matches the comment explaining an absence is a false pass.

## Host facts that bite

A plan written for one cluster does not run on another: of the 35 partitions across the three, almost none repeat, and `--mem` is required on lambda and nowhere else. Usernames and home paths differ across chimera, lambda and andromeda, so hardcoding either is a portability bug. `claude` and `node` live behind conda prefixes loaded by `.bashrc`, so a non-interactive `ssh host 'command -v node'` reports ABSENT and is wrong; use `bash -lic`. Use `chimera-login` rather than `chimera`, whose config carries `RemoteCommand sh_dev` and rejects a passed command. Lambda's `/tmp` is not writable, so stage to `$HOME`. git is 2.34+ on the clusters and 2.23 on the Mac, so avoid `git init -b`, `git switch` and GNU-only `sed`/`readlink` flags. Org-managed skills reach none of the clusters, so a skill here must be self-sufficient. `docs/clusters.md` is the partition and account reference and `docs/probes/` the measured data behind it.
