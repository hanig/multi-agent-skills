<!-- docs-truth:status -->
Status: live
Date: 2026-10-20
Title: Recover retained provider-stopped worktrees
Purpose: Manually preserve a retained provider-stopped attempt on its recovery ref and seed a successor without moving the attempt branch.

# Recover retained provider-stopped worktrees

Owner decision F1-OD4 (2026-10-20, ARC-2676) makes this an operator procedure.
Since PR #256 (ARC-2616), a code unit with `provider_resume` can retain its
worktree after exhausting its resume budget or encountering a content-filter
stop. Retention prevents cleanup; it does not prepare a commit, publish a ref,
release the worktree, or authorize a continuation. Automated preparation,
publication and release (ARC-2615, ARC-2614, ARC-2485) are deferred after review
did not converge.

Use the coordinator's state and launch anchors, not agent-written receipts, for
the unit, attempt, repository, branch, agent ID and stop reason. Run on the
coordinator host under its existing authority. Replace quoted placeholders with
the recorded values; `<ref>` is the full `refs/heads/recovery/<unit>-<attempt>`
printed by status. Set `HANIG_SWARM_DIR` to the directory of the loaded swarm
skill. `PLAN`, `DIR`, `ROOT` and `FILE` below mean the existing plan, coordinator
state directory, attempt root and a new UTF-8 continuation prompt file; use
absolute paths. Run commands individually and inspect each result.

## Procedure

1. Find retained attempts, including historical attempts, in status:

   ```sh
   python3 "$HANIG_SWARM_DIR/scripts/swarm.py" status "PLAN" --state-dir "DIR"
   ```

   Status can exit 2 because the failed unit needs a person. Its retention line
   has this form (also printed by advance):

   ```text
   <unit>: retained provider stop <attempt>; worktree <path>; required recovery ref <ref>
   ```

   If it appends `; reconciliation needed: <problem>`, stop and reconcile the
   binding with the coordinator before touching the worktree. Do not guess a
   path or erase the record to silence it. Confirm the path, attempt branch and
   repository against the launch anchors. Inspect both origin routes:

   ```sh
   git -C "<path>" remote get-url --all origin
   git -C "<path>" remote get-url --push --all origin
   git -C "<path>" symbolic-ref HEAD
   git -C "<path>" rev-parse "refs/heads/<attempt-branch>"
   ```

   Both routes must name the anchored destination, with no extra push
   destinations; resolve any mismatch before publication. Save the full branch
   SHA for the unchanged-branch check below. A previously detached checkout
   needs reconciliation with the saved preparation evidence before restarting.

2. Confirm the retained attempt's agent is no longer running using its exact
   recorded agent ID in the agent manager. Record the observation and time;
   a retained record or FAILED unit alone is not a liveness check. Confirm no
   descendant process or other writer is still changing the worktree. If
   liveness is unknown, or a writer remains, stop and retain the worktree.

3. Inspect tracked, untracked and ignored paths:

   ```sh
   git -C "<path>" status --porcelain=v1 --ignored
   git -C "<path>" status --porcelain=v1 --untracked-files=all --ignored
   ```

   Keep the full listing and the launch base identity outside the worktree.
   Inspect the actual changes for secrets and unrelated files before staging.
   Untracked ignored files are not captured by the commit; neither are special
   files or nested repositories' contents. Already tracked files remain tracked
   even if an ignore rule now matches them. A gitlink records an object ID, not
   the nested repository's contents. Inspect submodules and nested repositories
   separately, including ignored ones; do not rely on an add warning to find them.

   The mandate's [preservation requirement](orchestrator-mandate.md#preserve-before-any-cleanup-path-runs)
   still applies: keep an external snapshot of uncommitted work with base
   identity, content digest, full porcelain listing and a successful restore
   check, including patch application against the recorded base. Record its
   location and validation. This procedure does not automate that snapshot.
   Failed preservation leaves the work in place. Preserve excluded data
   separately before any later removal; never treat a commit as its backup.

4. Detach before staging so the attempt branch cannot move. Run the first two
   commands separately, applying the stop rule in step 5 before committing:

   ```sh
   git -C "<path>" checkout --detach
   git -C "<path>" add -A
   ```

   After successful staging without an embedded-repository warning, commit:

   ```sh
   git -C "<path>" commit -m "RECOVERY, NOT READY: <unit> <attempt>; <stop-reason>; no review ran; must not be merged"
   ```

   Name the exact unit, attempt and stop reason in the message. Include every
   open reproduced finding, if any. If the attempt already committed all its
   work and Git reports nothing to commit, inspect that existing content, then
   add the recovery label without changing it:

   ```sh
   git -C "<path>" commit --allow-empty -m "RECOVERY, NOT READY: <unit> <attempt>; <stop-reason>; no review ran; must not be merged"
   ```

   The label is not itself preserved implementation. Record the prepared HEAD
   and compare the attempt branch to the SHA saved in step 1:

   ```sh
   git -C "<path>" rev-parse HEAD
   git -C "<path>" rev-parse "refs/heads/<attempt-branch>"
   ```

   Stop if the branch moved. Never commit recovery on that branch or push to it.

5. Stop instead of continuing if Git warns about an embedded repository, or
   if add fails, even if it staged some paths. Do not suppress the warning or
   publish a partial index. A failed detach, unexplained commit failure or
   changed binding also stops the procedure. Leave the worktree in place and
   report what remains unpreserved; resolve the cause before resuming.

6. Check the exact recovery ref on origin before pushing:

   ```sh
   git -C "<path>" ls-remote origin "<ref>"
   ```

   Require a successful query. If the ref already exists, compare its full SHA
   with the prepared HEAD and stop this publication attempt, even if they
   match. Record whether it is identical or different and reconcile with the
   prior recovery evidence before continuing. Do not overwrite it. If it is
   absent, publish only to the required recovery ref:

   ```sh
   git -C "<path>" push origin "HEAD:<ref>"
   ```

   Never force. If publication fails or another operator may have published
   concurrently, retain the worktree and reconcile; do not retry blindly.

7. Verify the publication independently:

   ```sh
   git -C "<path>" ls-remote origin "<ref>"
   ```

   Require the exact ref and full prepared commit SHA, not merely exit 0 or a
   similarly named ref. Save this result in the recovery evidence. Missing or
   different output is not verified preservation. Preserved bytes are not
   completion evidence, a review pass, or authority to resume.

8. Under the operator's continuation authority, write `FILE` describing the
   preserved work, stop reason, review status and remaining task. Choose a fresh
   unit ID. The existing continue command uses the old unit's **current**
   recorded attempt for its launch anchors; if recovering an older retained
   attempt, stop for coordinator reconciliation rather than silently borrowing
   a newer attempt's base. Then append the successor using the verified ref:

   ```sh
   python3 "$HANIG_SWARM_DIR/scripts/swarm.py" continue "PLAN" --state-dir "DIR" --unit "<unit>" --new-id "<new>" --prompt-file "FILE" --head-ref "<ref>" --write
   ```

   Inspect the emitted unit and plan: its seed head must equal the verified
   recovery commit. Continue copies the old unit's settings and dependencies;
   it does not mark the old unit DONE, retarget dependents or close a ticket.
   Review the whole plan change, then ratify it and dispatch ready work:

   ```sh
   python3 "$HANIG_SWARM_DIR/scripts/swarm.py" advance "PLAN" --state-dir "DIR" --root "ROOT" --accept-plan-change
   ```

   Ratification is needed because continue changed the plan digest. Inspect all
   dispatch output; advance can also dispatch other ready units. Keep the
   original worktree until the successor has merged.

## Manual removal and remaining status

The coordinator never removes a retained worktree. Only after the recovery ref
is verified **and the successor has merged**, the operator may remove it:

```sh
git -C "<repo>" worktree remove "<path>"
```

First recheck that no writer or new work appeared, the external snapshot and its
restore check are complete, and excluded data has been separately preserved.
Ignored files can be removed along with an otherwise clean worktree: do not
skip that check. If any requirement is unmet, leave the worktree in place. Do
not force removal or delete the attempt branch as part of this procedure.

The status line stays even after manual removal, until a release tool exists:
ARC-2485 is deferred. Do not edit coordinator state to hide it. Keep recovery
and successor evidence so the stale path can be explained.

## Supported envelope and accepted limits

- Supported: retained attempts as PR 256 records them, and an operator with push access to the anchored route.
- Excluded: any code change, any change to the coordinator, the mandate or skill declarations, and automation of these steps.
