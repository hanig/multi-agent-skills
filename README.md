# multi-agent-skills

Portable coding-agent skills for dispatching autonomous work onto HPC clusters
and refusing to call it done without evidence. Installable across laptops and
login nodes with one script.

This repository is public by owner decision (2026-09-24). It carries cluster
hostnames, partition and account names, storage layouts and measured host
probes; never commit credentials, tokens or secrets. Pull-request titles,
bodies and comments must not add further cluster detail.

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
# Inspect automatic selection first; this changes nothing.
./install.sh --dry-run --json
# Name targets explicitly for a portable, auditable install.
./install.sh --agent claude --agent codex --agent opencode --agent pi \
  --allow-org-shadow
./bin/doctor --json                # installed skills, provenance, and health
```

Then, on a cluster, in an empty directory or a half-finished repo:

```
Use the hanig-project skill to start a project here.
```

That is the front door. It surveys the host, interviews you only about what
inspection cannot answer, writes a plan, shows you the project overview and
every issue title for approval, files them in Linear, and dispatches.

---

## The six skills we wrote

| Skill | One line |
|---|---|
| `hanig-project` | The front door: survey, interview, plan, approve, file, dispatch, report. |
| `hanig-orchestrate` | The authorized loop: delegate, watch, adjudicate, merge, reconcile, report, hand off. |
| `hanig-swarm` | The coordinator: validate a DAG, isolate each attempt, dispatch, advance, judge. |
| `hanig-verified-workflow` | Declare and verify what "done" means for one batch job. |
| `hanig-review-gate` | Adversarial multi-model review of code and of the claims made about it. |
| `hanig-portable-handoff` | Capture and resume run state across machines. |

All six are prefixed `hanig-` so they can never collide with an Arc
org-managed skill name. That prefix is now load-bearing rather than tidy:
`install.sh` reads it as the authorship namespace, and anything under
`skills/` outside it is treated as vendored -- installed by us, written
upstream, and **left alone by `--uninstall`**.

## The eight skills we vendor

`skills/` also carries `paseo`, `paseo-advisor`, `paseo-committee`,
`paseo-handoff`, `paseo-loop`, `pi-fleet`, `start-a-sprint` and `agent-bus`,
taken **verbatim** from Shreshth's repo so the swarm's dependency lives here
rather than in a second clone nobody's machine is guaranteed to have. They are
not edited -- an upstream re-sync is meant to be a real diff -- so the routing
decision they need goes in `~/.paseo/orchestration-preferences.json` instead
(see "Orchestration preferences, per machine").

The accompanying `bin/bus` has one deliberately tracked local correction for
ARC-270 because the archived source has no reachable upstream remote. It keeps
missing, unreadable, malformed, invalid, and empty model registries distinct
without treating the runtime state directory as proof of a registry. The
upstream report, provenance, and resync contract are in
`docs/upstream-agent-bus-model-registry.md`; `tests/test_bus_models.py` pins the
behavior so a future resync cannot silently discard the patch.

Vendoring skills does **not** make this repo standalone at runtime: the hard
dependency is the `paseo` binary, and copying markdown does not supply one.
`bin/bus`, `bin/agent-manager`, `bin/agent-view` and `models.json` came across
with them.

### Agent bus executable, in this checkout

The executable, model registry, and runtime state are three separate things.
`install.sh` installs skill directories only. With no selector it plans the
preferred physical roots of all eligible detected agents; `--agent` chooses an
explicit set and `--prefix` chooses one compatibility root. It deploys none of
those three runtime artifacts.

#### Executable discovery

In this checkout the executable is `bin/bus`. Set
`MULTI_AGENT_SKILLS_CHECKOUT` to the checkout's absolute path and use
`$MULTI_AGENT_SKILLS_CHECKOUT/bin/bus` from elsewhere. The vendored skills'
fixed `~/.agent-bus/bin/bus` path belongs to upstream's install layout, not to
this repository's installer.

#### Model-routing registry input

`bus models` reads `models.json` from `AGENT_BUS_HOME`; it does not read the
repository-root copy directly. The root `models.json` is a starter input to
copy into an explicitly chosen bus state directory and review before relying
on its routing data. It is not an executable-discovery mechanism.

#### Runtime state and cache ownership

Every bus invocation creates `sessions`, `inbox`, `cursors`, and `cache` under
`AGENT_BUS_HOME`. Live model-routing signals are cached there too. Therefore a
one-off check should use disposable state, while a real messaging session
should use a deliberately chosen durable state directory. Neither case should
write runtime state into this checkout merely because the executable lives
here.

#### Disposable model-routing check

After exporting `MULTI_AGENT_SKILLS_CHECKOUT` as the absolute checkout path,
set `AGENT_BUS_SCRATCH` to an existing writable directory whose resolved path
is outside both the checkout and `$HOME`. The block checks that precondition,
then can run from any other directory. Its error-exit subshell makes every
setup failure abort without changing the caller's shell options. It copies the
shipped registry only into a private temporary state directory. A disposable
`HOME` inside that same directory contains any side effects from live-signal
collectors that `models` invokes. The EXIT and signal traps remove the printed
directory on success, failure, or interruption, and refuse to delete anything
except the temporary child they allocated.

```bash
(
set -eu
checkout="$(CDPATH= cd -- "${MULTI_AGENT_SKILLS_CHECKOUT:?set the checkout path}" && pwd -P)"
home_dir="$(CDPATH= cd -- "$HOME" && pwd -P)"
scratch="$(CDPATH= cd -- "${AGENT_BUS_SCRATCH:?set external scratch}" && pwd -P)"
test -w "$scratch"
case "$scratch" in
  /|"$home_dir"|"$home_dir"/*|"$checkout"|"$checkout"/*)
    echo "AGENT_BUS_SCRATCH must resolve outside HOME and the checkout" >&2
    exit 2
    ;;
esac
bus_bin="$checkout/bin/bus"
test -x "$bus_bin"
bus_state=""
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ -n "$bus_state" ]; then
    case "$bus_state" in
      "$scratch"/agent-bus.*)
        rm -rf "$bus_state" || status=1
        ;;
      *)
        echo "refusing to clean unexpected path: $bus_state" >&2
        status=1
        ;;
    esac
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
bus_state="$(mktemp -d "$scratch/agent-bus.XXXXXX")"
printf 'disposable AGENT_BUS_HOME=%s\n' "$bus_state" >&2
mkdir "$bus_state/home"
cp "$checkout/models.json" "$bus_state/models.json"
chmod 600 "$bus_state/models.json"
HOME="$bus_state/home" AGENT_BUS_HOME="$bus_state" "$bus_bin" models --json
)
```

The `~/.agent-bus/bin/bus` commands in the vendored `agent-bus`, `paseo`,
`pi-fleet`, and `start-a-sprint` skills assume upstream's installer has placed
the executable there. They do not describe a path this repository installs.
Until upstream discovers the executable instead of hardcoding an install
layout, agents using only this repository need the checkout-local path above.
Do not create `~/.agent-bus/bin/bus` merely to make the instruction true.

The upstream-ready report and proposed correction are in
`docs/upstream-agent-bus-path-discovery.md`. The vendored skill files remain
unchanged so a future upstream re-sync remains an honest diff.

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

It carries the plan's DAG as `blockedBy`, and because that relation is
append-only through this interface, it also names the edges to REMOVE. Not
re-adding an edge does not delete it, so a shrunken `needs` list would
otherwise leave the tracker asserting a dependency the plan has dropped. It
decides that from a read-back of what the tracker holds, supplied by the
session with the connector as `--tracker-edges` -- never from the previous
draft, which records what was asked for rather than what landed. With no
read-back, `remove_blocked_by` is `null` rather than `[]`, and `check` calls
a filed project's edges unknown rather than in sync.

**The stopping condition is the whole point.** The interview ends when the plan
can RUN, not when the interviewer runs out of prepared questions. Every value
dispatch needs (input paths and globs, output destinations, account, partition,
any config file the command reads) must be settled before finishing. This is
enforced, not remembered: `swarm.py validate` refuses a plan whose declared
inputs are empty, still placeholders, or match nothing.

### hanig-orchestrate

The operating mode for a session driving an existing run. It points to the
owner-originated mandate as the sole authority and covers authority
confirmation, delegated prompts, watcher honesty, preservation, review
adjudication, merge, tracker reconciliation, the ordered three-part report,
and takeover.

It has no coordinator executable of its own. Set `HANIG_ORCHESTRATE_DIR` to the
loaded skill directory, then use the programs from the separately loaded
`hanig-project`, `hanig-swarm`, `hanig-review-gate`, and
`hanig-portable-handoff` skills through their own directory variables. A
selective install names all five bundles:

```sh
./install.sh --agent codex \
  --only hanig-orchestrate --only hanig-project --only hanig-swarm \
  --only hanig-review-gate --only hanig-portable-handoff
```

`swarm.py advance` remains one offline, locked pass that exits. A deterministic
host scheduler repeats it; the orchestrating session performs connected review,
merge, tracker, and reporting work around those passes.

The connected `merge_unit.py` operator requires passing `integration-tests`
and, when declared by the target policy, `changed-tests-stable` evidence
for the exact judged head and observed target commit, including the
rederived merge base and candidate tree. The target comes from the anchored
remote's `refs/heads/<target>` through the forge ref API; a PR's `baseRefOid`
or `base.sha` can be stale and supplies no current-tip evidence. CI must also be green. There is no
integration override. `--allow-unchecked-scope REASON` permits only a complete,
schema-valid `unchecked` report with exit 2; exit 1 is never waivable. Every
report must carry the exact coordinator binding and state epoch.

```sh
python3 "$HANIG_ORCHESTRATE_DIR/scripts/merge_unit.py" plan.json \
  --state-dir "$STATE" --unit impl --pr 123 --approver "Operator" \
  --verify-integration --verification-timeout 1800
```

This mode runs no merge. It reads `verifiers.json` and the pinned verifier
programs from the observed target commit, runs both in the same disposable
candidate-merge checkout, and records each result in the external coordinator
verification journal. Supply both Git
objects locally first; the verifier never fetches. The coordinator lease is
released while the candidate verifier runs. Before appending evidence, the
operator reacquires it and rechecks the exact unit, attempt, judged head,
target ref tip, and state epoch, along with the plan and merge intent. Its own
reacquisition advances the epoch once; any intervening coordinator acquisition
invalidates the observation, even if that coordinator changed no unit. A changed
binding or epoch refuses publication and asks for verification to be rerun.
The real merge path retains its existing lease scope. Ordinary invocation then
admits that evidence before issuing one guarded merge. A changed head or
target needs fresh evidence. The operator re-observes the branch ref before
publishing the durable intent and again immediately before requesting the merge,
after intent publication. A moved or unreadable ref at that last read durably
marks the intent `cancelled_before_request` and refuses without a merge call.
The next invocation follows that resolved intent to a fresh operation, so
verification can rerun without abandonment; the cancelled record is retained.
If cancellation publication fails, the original intent remains unresolved and
the operator refuses a second request. A pending cancellation record restores
that barrier even if publication failed after rename. Only after cancellation's
file and directory fsync succeed does the operator publish a separate
`.cancellation-committed` marker bound to the complete resolved intent. A rerun
with that marker and a leftover pending record retains the cancellation,
persists the marker and removes the pending record before fresh verification.
A visible cancelled phase without the marker remains unresolved when a pending
record exists, including older ambiguous cancellations. Markers are retained so
an interrupted cleanup cannot revive a committed request; they cannot resolve
a successor operation's request. A crash before marker publication remains
conservatively unresolved.
A red run for the same exact binding cannot be
hidden by an earlier or later pass. Coordinator-observed timeouts and
launch/capture failures are recorded as `incomplete`, with an
`incomplete_reason`; they neither admit nor poison that binding. A later
completed pass may supply the missing evidence. Returned nonzero child statuses,
including exit 127 and signals not sent by the coordinator, remain `fail`.
Child diagnostic text never determines completion. Existing `fail` receipts
retain their meaning, including historical timeout receipts.
A failure already observed before timeout cleanup remains `fail`, including
when a descendant keeps a capture pipe open after the verifier exits.
A nonzero exit reaped during cleanup also remains `fail` when the coordinator's
SIGKILL could not have caused that status; finishing between the initial poll
and the kill does not erase the failure.

The optional `verification-execution.json` belongs directly in the external
coordinator state directory. It is never read from the plan, PR or candidate
checkout. Without it verification stays local. Local defaults select the
operator's Python and Git from `os.defpath`; explicit paths replace those
defaults. A declaration can select a remote SSH alias and either `direct` or
`slurm` execution:

```json
{
  "schema_version": 1,
  "local": {"python": "/absolute/local/python3", "git": "/absolute/local/git"},
  "remote": {
    "ssh_alias": "verification-host",
    "executor": "slurm",
    "workdir_root": "/absolute/remote/scratch",
    "python": "/absolute/remote/python3",
    "git": "/absolute/remote/git",
    "slurm": {"partition": "cpu", "mem": "4G", "time": "03:00:00"}
  }
}
```

The remote root must already exist and, for Slurm, be visible at the same path
on the login host and allocated compute node (normally shared storage).
Node-local temporary storage on the login host cannot carry the transferred
bundle into another node. Paths are absolute host paths supplied by
the operator; no username, home path or host is inferred. Use Slurm on a login
host. For a dedicated execution host, `executor: "direct"` omits `slurm`.
Python and Git paths and versions appear in each new merge-verifier receipt;
remote receipts also retain the coordinator's construction executables. A
worker that cannot start leaves its host identity and versions unavailable
in the incomplete receipt.
The shipped verifier programs reuse the selected Python and Git for subprocesses.
Their updated digest pins must land on the target before their new behavior is
authorized; candidate changes cannot update the programs used to check themselves.
Ordinary generic verifier scripts retain their existing executable semantics.

Candidate construction remains local. The operator sends a self-contained Git
bundle and its own harness over batch-mode SSH, without pushing to a forge.
The remote worker checks out the bundle and rehashes the checkout before running
any pinned program. Both claims use that same checkout, with the target's
repetition count and completion handshake. Receipts retain the existing head,
target, merge-base, candidate-tree and verifier bindings, plus the independently
checked remote tree, host identity and executor. Slurm receipts also require the
exact job's terminal `COMPLETED` / `0:0` accounting row for a pass. Jobs request
no automatic requeue; an exclusive worker-start marker also prevents a forced
restart from replacing a previous worker’s completed or partial results. The execution
policy digest is rechecked with the existing observation fence before publication.
Retained remote evidence is checked again during reconciliation; missing tree or
execution evidence corrects an old verified label and withholds advancement.

The timeout covers each verifier; the Slurm supervision budget, including queue
wait, is that timeout multiplied by the number of claims. SSH, launch, timeout,
missing result and nonterminal scheduler outcomes are `incomplete`. A completed
verifier failure remains `fail`, including a retained failure before a later job
failure. Output tails are bounded. Remote directories are removed on completion;
a second SSH cleanup attempts cancellation and removal after transport failure.
If connectivity prevents confirmation, the receipt records that cleanup is
unconfirmed. A disconnected job can remain until its declared time limit; this
is not a remote process sandbox or a guarantee against same-UID writers.

The designated `merge-precondition` verifier runs
`python3 -m unittest discover -s tests` in the candidate tree. Its policy and
program must first exist on the trusted target; missing policy or mismatched
program bytes refuse. Reading target policy lets attempts whose launch bases
predate installation use the new precondition without allowing a candidate
to authorize itself. This merge-only authorization leaves ordinary
anchored-base verification unchanged. Candidate tests remain candidate bytes;
this policy does not promise an immutable test corpus.

The second designated verifier, `changed-tests-stable`, runs
`verifiers/changed_tests_stable.py`. It selects added or modified `test_*.py`
files under `tests/` from the PR's merge-base-to-head diff (renames count as
delete/add), then runs each module in a fresh host `python3` process five times.
Each invocation loads that module by its dotted name, retaining package
identities for relative imports and the integration runner's `tests/` helper
import path. Package-level `load_tests` hooks cannot replace the module's
suite; the module's own hook, including supervised delegation, is honored.
Any failure, error, or unexpected success fails the claim; a selected module
that executes zero tests also fails, even if its suite reports a positive size
or another module ran tests. Every repetition must exit zero AND deliver a
complete result through a fresh anonymous file descriptor outside the candidate
tree. The verifier's runner writes counts only after unittest returns; the
parent checks those counts, completion, and the original child PID independently
of stdout and exit status. Missing, malformed, partial, zero-test, stopped, or
unsuccessful results fail, including early successful exits and a forked
descendant returning after its parent exits. No result pathname is supplied to
candidate code, and the descriptor is closed across exec into delegates. This
guards accidental termination within the existing trusted-writer model; it is
not protection from hostile in-process introspection or same-UID writes.
No changed test modules is a successful no-op. The target's
verifier entry can set a positive integer `repetitions`; candidate policy and
program edits cannot change that count or the pinned program used for this PR.
Repeated passes sample stability; they do not prove a test cannot flake later.
If the coordinator times out and kills the verifier and its repetition, the
claim is `incomplete`; a child that exits on its own without the handshake still
fails. A process killed outside the coordinator's observation cannot safely be
distinguished from candidate-caused termination and is conservatively a failure.
The declaration is the unique policy entry named `changed-tests-stable` or
listing that claim. Its own name is used consistently for authorization and
receipts, while the program path and digest pin remain designated by the
coordinator. Multiple declarations or duplicate selected names refuse;
malformed claims never grant authority.

Bootstrap follows target authorization: a target without the new declaration
keeps the original single-claim rule, so the PR introducing it is checked under
the already-merged policy. Once the declaration lands, later merge requests
require both claims with no waiver. Missing or invalid declared verifier bytes,
policy, or evidence refuse. A retained failure for either claim at the exact
binding cannot be hidden by another pass. New merge intents retain the required
claim list and both admitted receipts for reconciliation after Git cleanup;
legacy intents require readable target policy to establish the claim list.
An unreadable legacy policy or missing required stability claim corrects any
incorrect persisted integration label to `integration-unverified` and withholds
advancement for human intervention. Unreadable policy never means no requirement.
Retained reconciliation also reads the current verification journal. A later
exact-binding `fail` for either retained claim, or an unreadable journal,
corrects the intent, merge receipt and any matching saved receipt label to
`integration-unverified` and withholds advancement. The journal remains
append-only; old results are not rewritten. `swarm.py verify --claim
integration-tests` checks coordinator merge-intent files under the lease both
before running and before publishing evidence. Any intent for that unit refuses
this generic path; use the connected operator, which understands cancellation
and abandonment, for subsequent verification. The lease remains released during
execution.

After a merge, the operator compares its actual parent with the checked
target. A race is recorded as `integration-unverified` in the intent and merge
receipt, reported with a warning, and withholds advancement. It never retries
the merge call. The ref read and merge are not atomic; this parent check remains
the backstop for movement after the final read. Historical merges without a retained precondition can still
be reconciled as attestations; unavailable integration evidence is explicitly
labelled `integration-unverified`. Forge observations remain attestations,
and the local lock and journals retain their same-node, trusted-writer limit.

### hanig-swarm

The coordinator. Roughly 2,200 lines in `swarm.py`, 1,300 in `unit.py`.

```sh
python3 scripts/swarm.py validate plan.json
python3 scripts/swarm.py run      plan.json [--dry-run]
python3 scripts/swarm.py status   plan.json [--json]
python3 scripts/swarm.py advance  plan.json
python3 scripts/swarm.py outbox            [--all] [--json]
python3 scripts/swarm.py outbox \
        --record-receipt KEY --ref ARC-171     # after the tracker confirms
python3 scripts/swarm.py promote  plan.json --unit ID --approve --approver hani
python3 scripts/swarm.py merge    --unit ID --repo o/r \
        --pr URL --head SHA --target main --merged-as SHA --method merge
python3 scripts/swarm.py verify   --unit ID --attempt DIR --claim tests-pass \
        --verifier NAME --path ./check.sh

python3 scripts/unit.py allocate --root /external/swarm-runs --task ID --kind slurm \
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
was the one capability with no replacement anywhere. A unit that declares a
`converge` block is judged by it: a run that spent its step budget without
meeting the criterion becomes `NEEDS_HUMAN`, not `DONE`, so it closes no ticket
and releases no dependent. Units that declare none never reach that code.

Coordinator state and attempt roots default to a stable per-project directory
under `$XDG_STATE_HOME`, or `~/.local/state` when it is unset. Both resolve
outside every operated Git worktree. Explicit paths that resolve inside one,
including through a symlink, are refused before a directory is created.
Existing `.swarm/state` and `.swarm/runs` trees are copied to the external
default on first use; the legacy files are retained and historical attempt
paths are not rewritten.

For a fresh code repair attempt, optional unit `seed` records the previous
work as `{"ref":"refs/heads/previous-attempt","base":"<full commit ID>",
"head":"<full commit ID>","evidence":"/previous/attempt/evidence.md"}`.
Both IDs must be 40 or 64 hex characters. Dispatch checks that `base` is an
ancestor of `head` and `head` is reachable from the exact ref on origin's push
destination, then persists the raw seed in coordinator launch intent. The
worker receives instructions to fetch, cherry-pick `-x -m 1 base..head` (first-parent mainline), skip empty
commits, and read the optional evidence; whole-file checkout is forbidden.
Relative evidence paths use the source repository directory. The coordinator
does not replay the seed, and reachability does not promise conflict-free
replay. Judging, scope-check and merged-PR closure still use the fresh
attempt's recorded base and produced head. Omission preserves existing intent,
prompt and plan digests; a persisted attempt's seed is never replaced from a
changed plan. See the [skill](skills/hanig-swarm/SKILL.md#seed-a-fresh-code-repair-attempt).

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
write root, created with `mkdir(exist_ok=False)`, which enforces that no two
attempts are ever handed the same root -- not that the OS keeps another process
running as the same Unix user out of one. Given that allocation, a cheap
predicate ("is there output under this root?") becomes conclusive, and the
machinery built to *prove* which process wrote a file was answering a question
that no longer needed asking.

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
| `code` | a merged PR, attested and bound (below) |
| `slurm` | a predicate receipt |
| `pipeline` | a predicate receipt |

**A `code` unit closes on a merged PR, and the merge is attested, not
verified.** The coordinator has no network, so it cannot ask GitHub anything: a
session that can see the PR records what it observed. That alone would let any
attester close any unit, so the receipt is **bound**. The PR head it pins must
equal the head the coordinator independently judged this attempt to have
produced, from an anchor written before the agent existed. The attester can lie
about whether a PR merged; it cannot make this unit's produced commit be a
different commit. A merge method outside `merge`/`squash`/`rebase` fails closed,
because an unrecorded method means `merged_as` cannot be interpreted.

**What the verifier chain does not establish.** The agent runs as the same
Unix user as the coordinator, so it can write any file the coordinator can,
including the launch record and the attempt receipts. No arrangement of files
defends against that. What is defended is an agent that fails to do the work
and an operator who runs the wrong thing. A hostile agent would need a
container or a separate Unix user, which is what the receipts have always said
about isolation. Nor is the agent's process tree established to be dead when a
check runs: no portable handle proves every same-UID descendant of a Paseo
agent or a scheduler job is gone, so a lingering one could touch outputs
mid-digest. That is recorded on each accepted receipt rather than claimed
closed. Both are DECLARED limits, stated in full in `hanig-swarm/SKILL.md`;
neither is scheduled, and neither should be assumed closed by a later reader.

**A verifier is admissible only if it is authorized, pinned and bound.** The
policy naming it is read from the **anchored base commit**, never from the
agent's branch, so a candidate change cannot authorize its own verifier. The
policy records the verifier's content digest, and the bytes that run are the
bytes that hashed, copied to a private temporary file: a path is not an
identity. The receipt names the head it verified, the policy digest it ran
under, and the claim it was authorized to make, so a pass for another commit or
under different rules is refused. A unit gates on this only when it declares
`requires_verification`, because a requirement everybody must satisfy is one
everybody learns to satisfy trivially.

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
| `requires_verification` | claims an authorized verifier must establish before this closes |
| `continuation` | `{"max": N}`: bounded nudges to a code agent that settled without producing |
| `pool`, `gpu_hours`, `env` | declared resource facts |

Every `code` unit runs in a coordinator-created linked worktree at its anchored
base; Paseo receives that existing path and never owns its cleanup. Before the
coordinator removes it, tracked and untracked bytes must be copied outside the
checkout, restore-checked by content digest, and atomically published as a
complete snapshot.
Failed preservation retains the worktree. The recovery copy is audit-only,
never completion evidence or resume authority. A non-code unit can require a
NUL-safe clean checkout with `workspace_policy: {"requires_clean_git": true,
"path":"/checkout"}`. No refusal stashes, resets, deletes, commits, or ignores
a path.

Only the exact coordinator-bound linked-worktree `.git` pointer is excluded as
Git metadata; if that object is replaced, its bytes are preserved like any
other worktree content. Legacy records with an absent path, remaining cleanup
retries, and identified Paseo ownership can be annotated as unrecoverable.
Known legacy limits remain: exhausted cleanup skips annotation; ID-only records
can be marked absent without lookup; path-only records without an owner or
workspace ID lack migration. An annotation cannot restore previously deleted
bytes.

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

Cross-agent installation and target selection are documented in
[`docs/agent-migration.md`](docs/agent-migration.md).  The release acceptance
record, including the native-CLI evidence that automated tests cannot supply,
is in [`docs/cross-agent-acceptance.md`](docs/cross-agent-acceptance.md).

```bash
./install.sh [--agent NAME ... | --exclude-agent NAME ... | --prefix DIR]
             [--mode copy|link] [--only NAME] [--dry-run] [--json]
             [--force] [--uninstall] [--allow-org-shadow]
             [--allow-vendored-shadow] [--include-vendored]
```

With no selector, the installer targets agents whose bounded `--version` probe
succeeds or is slow. Exact-version certification is reported separately;
unexercised or stale releases remain `unverified`, with warnings on stderr even
with `--json`. Selection alone is not a compatibility or invocation pass.
`--agent` is repeatable and is the right choice for a deliberate subset or a
bootstrap install of an absent CLI.
`--exclude-agent` applies only to automatic selection.  `--prefix` remains the
legacy single-root compatibility spelling; it cannot be combined with agent
selectors.  Inspect the JSON payload before a broad install: it reports each
target, physical root, verification, shared consumers, requested actions,
diagnostics, and conflicts.

If one loader would see requested copies in multiple physical roots, the plan
prominently reports `competing_visibility` and the unresolved adapter-specific
precedence. The ordinary mixed-host install proceeds because every planned root
receives the same source snapshot in that operation; deterministic selection
prevents flag order from changing placement. Existing foreign paths and
org-managed collisions remain hard failures, and separately edited copies can
still drift later.

**`--allow-org-shadow` is required here, and permanently.** A copy of these
skills is maintained on Claude Science, so all six also arrive in the Arc org
store through catalog sync. That is deliberate, not a mistake to clean up.

The installer still refuses by default, because the hazard is real: which store
the loader prefers is not guaranteed, so the copy that loads may not be the one
you just installed, and the two copies drift as each is edited. Passing the flag
says you know there are two and you mean to shadow the other. Rename ours
instead only if a colliding name is ever an unrelated Arc skill rather than the
Claude Science twin.

Copy is the default deliberately. A symlink into a live checkout breaks when the
checkout sits under a synced folder, when a branch switch silently mutates every
installed skill, and when the checkout is mid-update. Use `--mode link` only
when developing a skill.

The installer performs bounded frontmatter validation (including exact skill
identity) and script syntax checks before touching the destination. Its
conservative stdlib-only scalar subset requires ambiguous YAML-like values such
as numbers, dates, booleans, nulls, and sexagesimal forms to be quoted. It also
aborts on a name collision with an org-managed skill, prunes skills that are no
longer shipped, and refuses to replace a directory it did not install.

### Dated agent compatibility

The **ARC-281 live run** (2026-09-25) checked native skill discovery,
authenticated `hanig-portable-handoff` invocation, and cross-agent handoff on
the operator host:

| Agent | Exact certified version | Date | Observed skill root |
| --- | --- | --- | --- |
| Claude Code | 2.1.282 | 2026-09-25 | `~/.claude/skills` |
| Codex CLI | 0.154.0 | 2026-09-25 | `~/.agents/skills` |
| OpenCode | 1.18.29 | 2026-09-25 | `~/.agents/skills` |
| Pi | 0.86.1 | 2026-09-25 | `~/.agents/skills` |

[The native validation record](docs/cross-agent-acceptance.md#arc-281-live-certification--2026-09-25)
names the four exercised handoff directions and the limits of this evidence.
One patch newer remains unverified. The 30-day review window ends on
2026-10-25; older distinct versions retain their 2026-09-05 records and expire
after 2026-10-05. No new OS, root policy, or untested handoff pair is certified.

### Paseo, per machine

`code` units dispatch a coding agent through the Paseo CLI, so it has to be on
PATH on the machine that runs them:

```bash
npm install -g @getpaseo/cli && paseo
```

`swarm.py validate` refuses a plan containing a `code` unit when `paseo` is
absent, before anything is dispatched, rather than failing at `paseo run` with
half a DAG already live.

Install it where you actually run agents. A `code` unit runs a coding agent as
a local process, so putting Paseo on a shared cluster login node means running
those processes there, which is usually a sign the unit is on the wrong machine.
`slurm` and `pipeline` units need none of this.

**`--uninstall` deletes only what this repo installed**, determined by an exact
copy marker or a sidecar bound to the installed link's destination, target, and
link-object identity. It is written that way because an earlier version inferred
ownership from shape (`[ -L "$d" ]`, "any symlink is ours") and deleted two
unrelated skills off a cluster. A symlink, including one into this checkout, is
not evidence that we created it. Read a destructive command and dry-run it
before trusting it, including one you wrote yourself, and especially then,
because familiarity feels like knowledge.

### Orchestration preferences, per machine

Six of the vendored skills — `paseo`, `paseo-advisor`, `paseo-committee`,
`paseo-handoff`, `paseo-loop` and `pi-fleet` — refuse to choose a provider
until they have read `~/.paseo/orchestration-preferences.json`, and `agent-bus`
names the same file as the policy layer over `bus models`. `skills/paseo/SKILL.md`
is explicit that reading means an actual file read, and that no other skill may
hardcode a provider string. So the routing decision lives in that one file, not
in the skills — and this repo shipped the skills that need it without shipping
an example of it.

`examples/orchestration-preferences.json` is that example, carrying this
project's routing decision of 2026-09-01 (`docs/plan-field-reports.md`, "Model
routing"). Copy it into place on the machine that runs agents:

```bash
mkdir -p ~/.paseo && cp examples/orchestration-preferences.json ~/.paseo/
```

Nothing reads it from the repo. `install.sh` does not deploy it, because the
live file is user-specific configuration and overwriting a machine's routing
policy from a skill install is not a thing an installer should do.

**`models.json` is routing metadata, not a credential grant.** A model appearing
in `models.json` means it can be considered for a Paseo dispatch; it does not
mean a dispatched worker can call that or any other model from inside its task.
`OPENAI_API_KEY` and `OPENROUTER_API_KEY` stay coordinator-side: `review.py` and
`committee.py` use them directly, while `child_environment.py` removes both
exact names from every coordinator child. Paseo may still start the worker with
credentials held independently by its daemon, but that does not put either
ambient API key in the worker's environment. Keep additional-model calls in the
coordinator-side review and committee paths unless a separately designed proxy
or named exception deliberately changes that boundary.

**Copying it is not the end of the job.** The file was written on a machine
with no Paseo and no `~/.paseo`, so nothing in it was dispatched at the time.
Two of its three distinct provider strings have been since — `claude/opus` and
`codex/gpt-5.6-sol`, on 2026-09-04, both of which come up on the intended
model; `codex/gpt-5.6-luna` still has not.
Every provider string in it is copied verbatim from an attested id — the
examples in `skills/paseo/SKILL.md`, or an id in `models.json` — but a
well-formed string is not evidence that an agent comes up on the intended
model. Confirm that separately, on the host, with `paseo run` plus
`paseo inspect`, or launch through `bus launch-worker`, which inspects the
agent it created and stops it when the provider, model, thinking id, cwd or
branch does not match the plan. For a worker something else launched,
`bus await --expect-provider/--expect-model/--expect-thinking` makes the same
comparison and exits 4 on a mismatch. Those two are the whole surface:
`bus launch-worker` and `bus await`. This README and the template both used to
point at a third subcommand, which has never existed.

**The file cannot set reasoning effort, and no longer implies it can.** Effort
is a separate axis — `paseo run --thinking <id>`, or
`settings.thinkingOptionId` on `create_agent` — and the schema is provider
strings plus freeform prompt text, with no slot for it. The per-model effort
decision in `docs/plan-field-reports.md` therefore binds `hanig-swarm`'s code
units, which pass it from `swarm.py`'s `THINKING_BY_MODEL`, and binds nothing
else: a Paseo skill dispatching from these categories gets the provider
default. Measured on 2026-09-04 by dispatching and inspecting real agents,
`claude/opus` comes up at `auto` against an intended `high`, so `ui` and
`planning` silently run below intent; `codex/gpt-5.6-sol` comes up at `xhigh`
against an intended `high`, above it. Read the effort lines in that file as
intent to pass through, and check with `--expect-thinking` rather than
assuming they took.

Two things in the file are worth knowing before you rely on it. Three of the
five categories are judgement calls — `impl` is settled, `ui` is carried
forward unchanged, and `planning`, `research` and `audit` are readings of a
decision that was not written in these terms; each says so in its
`_judgement_calls` entry rather than presenting a guess as settled. And the
decision does not fully fit: `audit` is one provider string, while the real
review gate is three models across two API providers run by `review.py` over
`reviewers.json`, and DeepSeek's Paseo route needs a provider *and* a model,
which a one-string category cannot express. Those gaps are recorded in
`_unmapped` instead of being papered over with invented strings.

---

## Deployment targets

Measured 2026-08-25 with `bin/probe.sh`. Full output in `docs/probes/`.
Partition names, the rules for each cluster and which partition to use for what
are in [docs/clusters.md](docs/clusters.md).

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

`python3 -m unittest tests.test_review` and discovery that includes
`test_review.py` run the whole review module through the supervised worker.
The parent reports one delegated test and prints the audited worker's test
count and skips; full discovery launches the module once. `-k` filters do
not narrow this delegated module: the command announces that it runs the
whole module, so module loading cannot silently report zero tests.
Fully qualified class or method names (for example,
`python3 -m unittest tests.test_review.TestVerdictSchema.test_empty_object_is_not_a_review`)
bypass the module hook and run in-process under the existing per-test
fixtures, without the worker's session and journal-manifest audit.
A named class whose filter matches no methods fails with a delegation
diagnostic instead of reporting success with zero tests.

Standard library only; no network or cluster required. Green on macOS 3.10.16
and on all three clusters (3.10.12, 3.12.3, 3.10.12).

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
skills/hanig-orchestrate/   authorized operating loop and takeover contract
skills/hanig-swarm/         swarm.py, unit.py, converge.py, swarm-cron.sh
skills/hanig-review-gate/   review.py, committee.py, reviewers.json, PROTOCOL.md
skills/hanig-verified-workflow/  contract.py
skills/hanig-portable-handoff/   handoff.py
tests/                      stdlib unittest
examples/                   reference config to copy elsewhere; nothing reads it here
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
