---
name: start-a-sprint
description: "Plan and launch a guarded, Mac-monitored multi-agent engineering sprint as ticket pods: one native GPT-5.6-Sol coordinator plus bounded DeepSeek, GPT-5.6-Luna, or other declared workers per Linear ticket. Use when the user asks to start, launch, orchestrate, compare workers in, or monitor a sprint, worker swarm, ticket wave, or multi-agent implementation effort."
---

# Start a Sprint

Turn a prioritized ticket set into isolated, observable ticket pods without making the primary
session babysit individual workers.

## 1. Read the operating context

Read the repository and global `AGENTS.md` files. Inspect the candidate Linear tickets, current Git
state, dependencies, and likely production write scopes. Do not edit a shared checkout.

## 2. Propose a wave

Build a wave with at most three ticket pods. Sequence tickets whose dependencies are uncleared or
whose production write scopes overlap. Prefer this shape:

- One native `gpt-5.6-sol` coordinator per Linear ticket.
- Two to four declared workers per coordinator.
- No more than twelve workers across the wave.
- One isolated integration worktree and branch per ticket.
- One bounded, disjoint worker task and worktree per implementation worker.
- One testable acceptance contract and one declared production write scope list per ticket.
- One Mac-local, read-only sprint monitor for the wave.

Choose worker executors deliberately. Default bounded, text-only backend edits and focused test work
to self-hosted DeepSeek V4 Flash through Paseo's pi provider. Use native `gpt-5.6-luna` for work needing stronger
instruction following, broader repository judgment, or image-capable frontend inspection. Keep Sol
as the integration and review gate. Follow repository instructions when they prescribe another
model.

When tuning worker choice, state one hypothesis and comparison key in `worker_experiment`. Compare
similar task classes and acceptance contracts across waves. Do not assign two live implementation
workers the same write scope merely to create an A/B test; use separate comparable tickets or a
read-only replay for direct comparisons.

Write the proposed wave to a JSON sprint plan and validate it before showing it to the user:

```bash
python ~/.agents/skills/start-a-sprint/scripts/validate_sprint_plan.py <plan.json>
```

The plan uses this compact contract:

```json
{
  "sprint": "multi-user-backend-wave-1",
  "monitor": {
    "mode": "mac-local",
    "url": "http://127.0.0.1:8142",
    "command": "uv run tahoebench-sprint-monitor --config ops/sprints/multi-user-backend.json --open"
  },
  "worker_experiment": {
    "comparison_key": "bounded-backend-edit-v1",
    "hypothesis": "DeepSeek is faster per accepted bounded edit; Luna needs fewer review corrections"
  },
  "pods": [{
    "ticket": "ML-31",
    "acceptance_contract": "Focused and full tests pass; one reviewed integration commit",
    "integration_branch": "ml-31/service-auth",
    "integration_worktree": "/private/tmp/ml-31-service-auth",
    "production_write_scopes": ["src/tahoe_tasks/auth/", "tests/test_auth.py"],
    "dependencies": [{"ticket": "ML-30", "cleared": true, "evidence": "merged in PR #12"}],
    "workers": [{
      "task": "Implement the credential provider seam and focused tests",
      "branch": "worker/ml-31-provider",
      "worktree": "/private/tmp/ml-31-provider",
      "write_scopes": ["src/tahoe_tasks/auth/"],
      "executor": {"kind": "paseo", "provider": "pi", "model": "deepseek-runai/deepseek-v4-flash-0731"}
    }, {
      "task": "Add contract tests for missing and malformed credentials",
      "branch": "worker/ml-31-contract-tests",
      "worktree": "/private/tmp/ml-31-contract-tests",
      "write_scopes": ["tests/test_auth.py"],
      "executor": {"kind": "native", "model": "gpt-5.6-luna"}
    }]
  }]
}
```

Show the dependency order, pod scopes, acceptance contracts, concurrency, and expected validation.
Wait for explicit approval before launching. Approval to the wave authorizes its worktrees,
implementation, validation, commits, branch pushes, and draft PRs. It never authorizes merging,
deploying, or closing tickets.

## 3. Start the Mac-local monitor

Keep the monitor on the developer Mac; do not publish it as a Site or product dashboard. Prefer the
repository's checked-in monitor command. For Tahoebench:

```bash
uv run tahoebench-sprint-monitor --config <sprint-config.json> --open
curl -fsS http://127.0.0.1:8142/health
```

Start it from the integration checkout that contains the monitor implementation. Require a loopback
URL, a read-only server, and no browser credentials or mutation endpoints. If port `8142` is occupied,
first verify whether the existing process is the correct healthy monitor; do not kill or replace an
unidentified process. The sprint may launch only after the health check succeeds and the config lists
the current tickets, worktrees, coordinator records, and worker records.

## 4. Launch ticket pods

Create each integration and worker worktree in the primary session. Spawn each Sol coordinator as a
native subagent and give it the ticket, acceptance contract, integration worktree, declared scope,
and validated plan. After the coordinator freezes any shared interface, launch Paseo implementation
workers with `~/.agent-bus/bin/bus launch-worker`, not an unconstrained narrative-and-launch turn.
Write each bounded worker prompt to a sprint scratch file and preserve the wrapper's atomic launch
record. Launch native Luna workers through the native subagent API so completion returns directly to
the parent; do not route them through Paseo merely for symmetry.

```bash
~/.agent-bus/bin/bus launch-worker \
  --provider pi --model deepseek-runai/deepseek-v4-flash-0731 --thinking max \
  --cwd <worker-worktree> --branch <worker-branch> --base <base-commit> \
  --title "<ticket · scope>" --prompt-file <prompt.txt> \
  --label ticket=<ticket> --label role=worker --record <launch-record.json>
```

Pass `--mode` only when provider inspection declares that mode. Pi exposes no modes; passing
`--mode default` rejects the worker. The wrapper must refuse dirty, wrong-branch, or wrong-base
worktrees and stop a worker whose inspected cwd/provider/model/reasoning differs from the plan.

Require each Sol coordinator to:

1. Preflight only the executors declared in its plan, following the active `AGENTS.md`.
2. Define two to four narrowly scoped workers. Use the deterministic wrapper above for every Paseo
   launch and verify its launch record before counting the worker as active. Record native subagent
   IDs from their creation result and retain their automatic completion notification.
3. Keep ownership explicit: the primary session alone writes the shared tracker config; the Sol
   coordinator alone writes its atomic pod-status file; workers write neither. Copy verified launch
   records into the pod status and tracker rather than reconstructing IDs from lifecycle listings.
4. Arm an artifact-aware wait as a background task per Paseo worker. Require the exact launch
   contract, a clean commit beyond the base, and bounded continuation for planning-only turns:

   ```bash
   BUS_NAME=<session> ~/.agent-bus/bin/bus await <agent-id> --timeout 3600 \
     --expect-cwd <worker-worktree> --expect-branch <worker-branch> \
     --expect-provider <provider> --expect-model <model> --expect-thinking <level> \
     --base <base-commit> --require-clean --continue-on-idle --max-continuations 3
   ```

   For native workers, use the native completion callback and then apply the same Git/test evidence
   checks. `bus await` polls local inspect state and retries transient inspect and send failures. A bare
   `notifyOnFinish`, `paseo wait`, `completed`, `idle`, or send acknowledgment is never proof of
   success. `IDLE WITHOUT ARTIFACT` is an actionable failure, not completion.
5. Treat `idle` or `completed` only as lifecycle state, never as proof of success.
6. Inspect worker logs, Git status, commit hash, diff, and focused test output.
7. Reject unsafe or incorrect patches; reimplement critical fixes when needed.
8. Cherry-pick accepted worker commits, resolve overlaps, run combined and full validation, and
   produce one clean integration commit.
9. Return the integration commit, validation evidence, rejected work, residual risk, and any
   uncompleted acceptance criterion to the parent.

Workers never integrate one another. Do not send images to text-only DeepSeek workers.

## 5. Monitor outcomes, not acknowledgements

Keep the sprint monitor open throughout the wave. Track start/completion time, worker makespan,
coordinator review wait, active slots by executor, token input/output when available, cost when
reported, retries, provider errors, Git state, focused-test result, reviewer acceptance/rejection,
and integration outcome. Display `usage unavailable`, never zero, when a provider exposes no usage.
The primary session should receive one native coordinator completion notification per ticket rather
than one notification per worker.

Treat pod-status files as authoritative for native Sol and Luna sessions until the local monitor can
ingest native-session state. If the page only sees Paseo workers, do not let it label completed native
review as worker execution; report the native phase from the coordinator-owned status file and record
native ingestion as a monitor gap.

If a coordinator stalls, inspect its launch records and artifact-aware reports before intervening.
For the coordinator itself, require its integration commit plus validation fields using
`--status-file` and repeatable `--require-json <dotted.path>`. Do not busy-poll or restart Paseo.

For each worker, preserve this scorecard in the sprint result even if some fields are null:

- Comparison key, task class, executor kind, provider, and exact model.
- Start, completion, elapsed, and review-wait times.
- Turns, input/output tokens, reported cost, retries, and provider failures.
- Commit produced, focused tests, accepted/rejected status, and review corrections required.

Do not rank models from a single ticket. After several comparable waves, prefer accepted integration
work per reviewer-minute as the primary operational measure; latency and cost are secondary, and raw
commit count is not a quality measure.

## 6. Run a risk-calibrated review gate

Every candidate PR needs at least one independent, read-only whole-diff review from native
GPT-5.6-Luna. Add a fresh independent GPT-5.6-Sol reviewer session, distinct from the ticket
coordinator, for concurrency, security, cross-user isolation, authentication, deployment, or
data-loss risk. For a large phase or dependency gate, use Kimi K3
when its usage is known available; if it is unavailable, use Sol rather than pausing the sprint.
Reviewers must not edit, push, merge, deploy, or mutate Linear or GitHub state.

Use the same reviewer for convergence. One correction cycle is enough when the updated diff clearly
closes every confirmed finding; cap the normal process at two cycles unless the second pass finds a
new substantive defect. Do not add adversarial passes merely because they were planned earlier.

The first review packet must include:

- the overall sprint goal and concrete success definition;
- the current wave's purpose, dependency order, and what earlier waves already guarantee;
- the ticket acceptance contract and declared production write scopes;
- the primary security, correctness, concurrency, rollback, and operational risks;
- explicit later-wave and pre-existing scope boundaries;
- the exact base and candidate commits or diff range.

The primary coordinator independently reproduces every substantive finding. Treat reviewer output as
claims, reject speculative or pre-existing findings, and implement only confirmed fixes. The
convergence review receives the exact updated diff plus a table mapping each original finding to
fixed, rejected, or accepted-residual status and the validation evidence. Reviewer approval is
evidence for the gate; it is never permission to merge or deploy.

Review every selectable backend affected by the diff. If a legacy implementation is not a supported
production contract, remove or explicitly reject it instead of accepting an untested residual path.
For a CLI, mirror every server invariant knowable before I/O and prove invalid input produces zero
network and storage requests.

Harden tests from these reviews without accumulating a line-by-line test museum:

- add regression coverage only for a confirmed defect or an explicit acceptance risk;
- prefer one cross-layer invariant test over many narrow implementation tests;
- keep fast contract tests in the default suite and concurrency, load, browser, and deployment cases
  in their existing specialized gates;
- record rejected and residual findings in the gate report rather than adding speculative tests;
- remove redundant tests when a stronger invariant test fully subsumes them.

Record review duration, confirmed/rejected finding counts, correction rounds, and tests added or
removed in the sprint monitor so later waves can measure the gate's reviewer cost and defect yield.

## 7. Close the wave safely

Verify each coordinator's integration commit and rerun validation proportionate to risk. Fix
confirmed findings, resolve merge conflicts and CI failures, push the ticket branch, and open or
update its draft PR without another approval pause. Review and CI evidence apply only to the exact
candidate head: any rebase or post-review commit requires a fresh whole-diff review and CI run. Keep
the PR draft while agents are changing it; at the user gate, “mergeable” means conflict-free with
required checks green, and the user may mark it ready and merge in GitHub. Update the Linear ticket
and sprint tracker with the evidence and remaining work. Present the user with:

- Accepted commit(s) and branch(es).
- Tests and checks actually run.
- Rejected worker outputs and residual risks.
- Bottleneck and worker-comparison metrics from the local monitor.
- The next dependency-safe wave.

Ask the user only when the draft PR is independently reviewed, CI-green, mergeable, and ready for the
user's final GitHub check. Never merge a GitHub PR without explicit user approval. Deployment and
ticket closure also remain explicit gates. When the user reports a merge, confirm it, update Linear
and the tracker, and immediately continue with the next dependency-safe ticket in the already-approved
wave. Propose and obtain approval before launching another wave.
