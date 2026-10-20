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
coordinator host under its existing authority. Shell-escape substituted values (including quotes, backslashes, dollar signs and backticks) rather than pasting raw text into the quoted placeholders; use
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
   destinations; resolve any mismatch before publication. Read the full
   `head_at_retention` from `DIR/swarm-state.json` at `units[unit].attempt_provider_stops[attempt].head_at_retention` (coordinator-owned state).
   If it is missing, stop for reconciliation. Compare the live branch with that
   recorded head before accepting it as the recovery baseline:

   ```sh
   test "$(git -C "<path>" rev-parse "refs/heads/<attempt-branch>")" = "<head_at_retention>"
   ```

   A nonzero exit means stop for reconciliation before detaching, publishing or
   creating a successor. Do not adopt a post-stop commit as the baseline or
   reset it away; retain those bytes for inspection. Save the matching full SHA
   for the unchanged-branch check below. A previously detached checkout needs
   reconciliation with the saved preparation evidence before restarting.

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
   prior recovery evidence before continuing. Do not push to a ref observed to
   exist. If it is absent, publish only to the required recovery ref:

   ```sh
   git -C "<path>" push --porcelain --no-follow-tags origin "HEAD:<ref>"
   ```

   Never force. The explicit no-follow-tags option prevents inherited
   `push.followTags` from submitting unrelated annotated tags. Save the full
   output and exit status. In porcelain output, the first field is a flag and
   the second field names the source and destination ref, separated by a colon;
   fields are separated by tabs. Inspect the row for the exact recovery ref,
   preserving the leading space in its flag.
   Continue only when the command exits 0 and the flag is `*` for the exact recovery ref.

   `*` means a new ref. A space means fast-forward: another publisher created
   this ref between the absence query and push. Stop, report both the old head
   shown in the update summary and the full prepared head, and do not continue
   or remove anything. Resolve the old abbreviated object name to its full SHA
   in the retained repository when recording those heads. This detects the race
   **after the fact; it does not prevent it**. No work is lost by that
   fast-forward: the other commit remains an ancestor of the prepared commit.
   `!` means rejected and also stops the procedure. Any other flag (including
   `=` for an already identical ref), missing/ambiguous row or nonzero exit
   means stop and retain the worktree; do not retry blindly.

   One terminal session is the sole operator. Concurrent publishers of the
   same recovery ref are outside the supported envelope. If detected, keep
   the worktree and both publication records, reconcile them under the sole
   operator, and obtain continuation authority before resuming. Never reset
   the remote ref or remove work to undo the race.

7. Verify the publication independently:

   ```sh
   git -C "<path>" ls-remote origin "<ref>"
   ```

   Require the exact ref and full prepared commit SHA, not merely exit 0 or a
   similarly named ref. Save this result in the recovery evidence. Missing or
   different output is not verified preservation. Preserved bytes are not
   completion evidence, a review pass, or authority to resume.

8. Under the operator's continuation authority, write `FILE` describing the
   preserved work, stop reason, review status and remaining task. Tell the
   successor to follow its injected seed protocol: fetch the recovery ref and
   replay the declared range on its new attempt branch, skipping confirmed
   empty label commits. The coordinator checks reachability but does not replay
   the work for the agent. Choose a fresh unit ID. The existing continue command
   uses the old unit's **current**
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

## Manual regression test: a branch changed after retention

Use only a disposable local repository and bare remote. Build a retained attempt
through the real `record_stop` path at commit A, then commit an extra file on
its attempt branch to make commit B. Stop the fixture writer and execute step 1
with the retained record still naming A. The comparison must exit nonzero;
assert that the recovery ref is absent and no successor was appended. Keep B
in the retained worktree. As a positive control, a branch still at A must pass
the comparison and complete the procedure. Reverting the comparison must make
the changed-head scenario fail its no-publication assertion: saving B as the
initial branch SHA alone does not detect the mismatch.

## Manual regression tests: publication

Use disposable local repositories and bare remotes, with no real services.
Execute the page's push command and apply its exit-status and exact-ref flag
check to the actual porcelain output; do not trim the leading space.

- Enable `push.followTags=true`, create a reachable annotated tag, and install
  a local remote hook that rejects tags but permits recovery branches. Confirm
  the hook rejects the tag. The documented command must create the recovery
  ref at the prepared commit, exit 0 with `*`, and leave the tag unpublished.
  Removing no-follow-tags must fail this success assertion.
- After the successful absence query, interpose a publication at ancestor A.
  The documented push then fast-forwards to prepared commit R and emits a
  space. The procedure must stop before continuation or removal, report A and
  R, and retain the worktree. Verify A is an ancestor of R. Removing the
  new-ref-only flag check must fail the no-continuation assertion even though
  the push and subsequent remote-head comparison both succeed.
- Instead interpose a diverging commit. The documented push must exit nonzero
  with `!`; the remote remains at the other head and the retained worktree
  keeps the prepared commit. No continuation or removal is allowed.

These tests supplement the full synthetic retained-attempt run through the
real `record_stop` path, continuation, successor replay and guarded removal.

## Supported envelope and accepted limits

- Supported: retained attempts as PR 256 records them, and an operator with push access to the anchored route.
- Excluded: any code change, any change to the coordinator, the mandate or skill declarations, and automation of these steps.

Supported envelope amendment (marked; orchestrator, 2026-10-20): one operator publishes a given recovery ref at a time, per the owner's 2026-10-20 decision that one terminal session is the sole operator. Two concurrent publishers of the same recovery ref are outside the envelope, and the page's porcelain check is the detection.
