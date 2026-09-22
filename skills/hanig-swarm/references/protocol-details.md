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

## Cron shape

Use the host's deterministic scheduler, for example:

```bash
LINE="*/5 * * * * $HOME/swarm-live/scripts/swarm-cron.sh $HOME/swarm-live/PROJ  # hanig-swarm"
{ crontab -l 2>/dev/null | grep -v swarm-cron.sh; echo "$LINE"; } | crontab -
crontab -l | grep swarm
```

Cron on lambda was observed advancing the DAG without a connected human on
2026-08-28. This observation does not strengthen the lock topology. <!-- declaration: placement.reference-elaboration -->
