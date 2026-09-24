# Protocol details and commands

The decision surface is `../SKILL.md`. This file gives longer command examples
and compatibility detail without changing those rules.

## Convergence hand tool

```bash
python3 "$HANIG_SWARM_DIR/scripts/converge.py" check metrics.jsonl \
  --criterion '{"metric":"val_loss","mode":"min","rel_improvement_below":0.002,"over_evals":5,"min_steps":10000}' \
  --diverge '{"metric":"train_loss","above":1e9}' --budget 40000
```

Exit codes are 0 `CONVERGED`, 1 `NOT_YET`, 2 `DIVERGED`,
3 `BUDGET_EXHAUSTED`, and 4 `INCOMPLETE`.
`BUDGET_EXHAUSTED` means stopped without converging, not success.

## Authorized verifier policy

```json
{"schema_version":1,"verifiers":[
 {"name":"tests","sha256":"...","claims":["tests-pass"],
  "corpus":["tests/test_api.py","tests/fixtures/api.json"]}
]}
```

## Integration verifier invocation

```bash
python3 "$HANIG_SWARM_DIR/scripts/swarm.py" verify \
  --state-dir "$STATE" --unit impl --attempt "$ATTEMPT" \
  --claim integration-tests --target-commit "$TARGET_COMMIT" \
  --verifier tests --path /approved/run-tests
```

The connected session supplies both commit objects locally. At merge recording
it also supplies `swarm.py merge --target-commit SHA`, the target head
immediately before the merge. A mismatch invalidates the integration receipt.

## Judgment fact generations

Schema-1 intents/schema-2 facts predate pushed-ref judgment and retain the old
live-worktree predicate. Preserved schema-2 intents/schema-3 facts anchored an
origin URL and generated branch but stored a local tracking spelling; readers
derive `refs/heads/{branch}` without rewriting stored bytes. Schema-3
intents/schema-4 facts store the exact remote ref and one revalidated origin
URL. Schema-4 intents/schema-5 facts additionally anchor the raw URL used for
exactly-once `insteadOf` expansion. A ref-era snapshot missing its generation's
anchors fails closed; deleting coordinator state is not migration.

## Merge scope precondition

```json
{"scope":["tests/**","skills/hanig-swarm/scripts/swarm.py"]}
```

```bash
python3 "$HANIG_SWARM_DIR/scripts/swarm.py" scope-check plan.json \
  --state-dir "$STATE" --unit impl --json
```

Matching uses Python's case-sensitive `fnmatchcase`: `*`, `**`, `?`, and bracket
classes match raw Git paths, and slashes are ordinary characters. For example,
`tests/**` covers descendants at every depth and `*.py` also matches nested
Python files. An explicit empty list allows no changed paths; an absent field
is unchecked. Paths use repository-relative `/` spelling.

The orchestrator requires exit 0 before merging unless it records an explicit scope exception with `hanig-orchestrate/scripts/merge_unit.py` as documented in the operator skill; exit 1 reports `out_of_scope`, and exit 2 reports `unchecked`, including missing scope, launch intent, judged head, or local objects. Neither nonzero result is a pass. <!-- declaration: code.merge-scope -->

The command compares immutable commits from the current attempt's coordinator
state using local `git diff --name-status -M` with NUL-delimited paths. Both
rename endpoints appear in the changed-path comparison. JSON reports
`out_of_scope` and the separate `deletions_out_of_scope` subset, where a rename
contributes its old path. Missing authority has no worker-file or branch
fallback. State and closure authority stay unchanged; advancement is independent
of this advisory command. The comparison describes that base and judged head,
not the eventual merge tree or a later PR head.

## Cron shape

Use the host's deterministic scheduler, for example:

```bash
LINE="*/5 * * * * $HOME/swarm-live/scripts/swarm-cron.sh $HOME/swarm-live/PROJ  # hanig-swarm"
{ crontab -l 2>/dev/null | grep -v swarm-cron.sh; echo "$LINE"; } | crontab -
crontab -l | grep swarm
```

Cron on lambda was observed advancing the DAG without a connected human on
2026-08-28. This observation does not strengthen the lock topology. <!-- declaration: placement.reference-elaboration -->
