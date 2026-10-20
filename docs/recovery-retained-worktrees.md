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
   git -C "<path>" symbolic-ref -q HEAD
   git -C "<path>" rev-parse HEAD
   git -C "<path>" rev-parse "refs/heads/<attempt-branch>"
   ```

   Both routes must name the anchored destination, with no extra push
   destinations; resolve any mismatch before publication. Read the full
   `head_at_retention` from `DIR/swarm-state.json` at `units[unit].attempt_provider_stops[attempt].head_at_retention` (coordinator-owned state).
   If it is missing, stop for reconciliation. If symbolic-ref exits 0, its output
   must be exactly `refs/heads/<attempt-branch>`; an attachment to any other
   branch stops. Exit 1 with no output means detached; any other result stops.
   Compare both the live branch and HEAD with the recorded head before accepting
   it as the recovery baseline:

   ```sh
   test "$(git -C "<path>" rev-parse "refs/heads/<attempt-branch>")" = "<head_at_retention>"
   test "$(git -C "<path>" rev-parse HEAD)" = "<head_at_retention>"
   ```

   A nonzero exit means stop for reconciliation before detaching, publishing or
   creating a successor. Do not adopt a post-stop commit as the baseline or
   reset it away; retain those bytes for inspection. Save the matching full SHA
   for the unchanged-branch check below. Apply this table; the different-branch
   stop overrides the other rows:

   | Branch | HEAD | Action |
   |---|---|---|
   | at head_at_retention | at head_at_retention, attached or detached | proceed; no prior preparation evidence needed |
   | not at head_at_retention | any | stop |
   | at head_at_retention | detached at another commit | stop for reconciliation (the only case where prior preparation evidence matters) |
   | any | attached to a different branch | stop |

2. Confirm the retained attempt's agent is no longer running using its exact
   recorded agent ID in the agent manager. Record the observation and time;
   a retained record or FAILED unit alone is not a liveness check. Confirm no
   descendant process or other writer is still changing the worktree. If
   liveness is unknown, or a writer remains, stop and retain the worktree.

3. Inspect tracked, untracked and ignored paths:

   ```sh
   git -C "<path>" status --porcelain=v1 --ignored
   git -C "<path>" status --porcelain=v1 --untracked-files=all --ignored > "<listing-file>"
   cat "<listing-file>"
   ```

   Use a new `<listing-file>` outside the worktree, and require both commands
   to succeed. Keep this full listing and the launch base identity there.
   Inspect the actual changes for secrets and unrelated files before staging.
   Untracked ignored files are not captured by the commit; neither are special
   files or nested repositories' contents. Already tracked files remain tracked
   even if an ignore rule now matches them. A gitlink records an object ID, not
   the nested repository's contents. Inspect submodules and nested repositories
   separately, including ignored ones; do not rely on an add warning to find them.

   For this manual procedure only, owner decision ARC-2676-OD1 (2026-10-20)
   makes the verified recovery commit the mandate's
   [snapshot of uncommitted work](orchestrator-mandate.md#preserve-before-any-cleanup-path-runs):

   - Base identity is the recovery commit's parent, which must equal
     `head_at_retention`.
   - The content digest is the commit and tree ID, confirmed on the remote by
     the verification step.
   - The full porcelain listing is this step-3 listing, saved outside the worktree.
   - The restore check is the verified remote ref, plus the coordinator's own
     reachability check in `swarm.py continue`.

   A failed check leaves the work in place. Ignored, nested-repository and
   special-file data require retaining the worktree under the removal checks
   below; they are not backed up by the recovery commit.

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
   git -C "<path>" rev-parse "HEAD^{tree}"
   git -C "<path>" rev-parse "refs/heads/<attempt-branch>"
   git -C "<path>" rev-parse HEAD^
   test "$(git -C "<path>" rev-parse HEAD^)" = "<head_at_retention>"
   git -C "<path>" status --porcelain=v1 --untracked-files=all
   ```

   Require the parent check to exit 0 and the status command to succeed and print
   nothing. Otherwise stop before pushing. Save the full commit and tree IDs.
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
   this ref between the absence query and push. This detects the race
   **after the fact; it does not prevent it**. No work is lost by that
   fast-forward: the other commit remains an ancestor of the prepared commit.
   Save the old head from the update summary too, resolving its abbreviated
   object name to the full SHA in the retained repository.

   `!` means rejected. On `!` or **any flag other than `*`**, including a space
   or `=` for an identical ref, run the step-7 ls-remote command and report the
   full remote SHA next to the full prepared HEAD. Neither continue nor remove
   anything. A missing/ambiguous row or nonzero push exit also takes this stop
   path. If the query fails or names no exact ref, report that observation
   alongside the prepared HEAD; never invent a remote SHA or retry blindly.

   One terminal session is the sole operator. Concurrent publishers of the
   same recovery ref are outside the supported envelope. If detected, keep
   the worktree and both publication records, reconcile them under the sole
   operator, and obtain continuation authority before resuming. Never reset
   the remote ref or remove work to undo the race.

7. Query the remote independently, both after a successful new-ref publication
   and on the publication stop path:

   ```sh
   git -C "<path>" ls-remote origin "<ref>"
   ```

   On the stop path, report the full remote SHA alongside the full prepared
   HEAD and stop here, even when they match. Leave the worktree in place.
   On the successful new-ref path, require the exact ref and full prepared
   commit SHA, not merely exit 0 or a similarly named ref. That commit ID binds
   the recorded tree ID; matching it confirms both. Save this result in the
   recovery evidence. Missing or different output is not verified preservation. Preserved bytes are not
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

The coordinator never removes a retained worktree. The default is to keep it.
Only after the recovery ref is verified **and the successor has merged**, recheck
that no writer or new work appeared and the step-3 listing and all snapshot
checks above succeeded. Then inspect the saved listing and run:

```sh
cat "<listing-file>"
find "<path>" \( -type p -o -type s -o -type b -o -type c \) -print
find "<path>" -mindepth 2 -name .git -print
```

Removal is allowed only when all three commands succeed, the saved listing has
no `!!` entry, and both find commands print nothing. Otherwise retain the
worktree and report which check refused removal. Porcelain cannot show special
files: a FIFO can be absent even from the full ignored listing. A nested
repository may appear only as `?? sub/n/`; the second find detects its `.git`.
The linked worktree's own `.git` is at depth 1 and is excluded by `-mindepth 2`.
If the saved listing itself identifies a nested repository or special file,
retain the worktree even if it has since disappeared from these scans.
Ignored files can be deleted by an otherwise successful worktree removal, so
never skip the saved-listing check. Only if every prerequisite holds, run:

```sh
git -C "<repo>" worktree remove "<path>"
```

Do not force removal or delete the attempt branch as part of this procedure.

The status line stays even after manual removal, until a release tool exists:
ARC-2485 is deferred. Do not edit coordinator state to hide it. Keep recovery
and successor evidence so the stale path can be explained.

## Manual regression tests: baseline and snapshot

Use only disposable local repositories and bare remotes, with stopped fixture
writers and retained records built through the real `record_stop` path at A.
Execute the quoted commands and apply their stop rules; do not use coordinator
preservation helpers to produce a snapshot.

- Branch and attached HEAD at A proceed through verification. Detached HEAD at
  A also proceeds with no prior preparation evidence; reinstating that evidence
  gate must fail this success assertion.
- Branch at A with detached HEAD at B stops at step 1 before any commit; assert
  the stop point and absence of publication. Removing only the step-1 HEAD
  comparison must fail the step-1 stop assertion, while the post-commit parent
  check still stops before any push. Removing both comparisons must publish,
  failing the no-publication assertion. Branch at B and attachment to another
  branch each stop at step 1 too.
- After a normal commit, show its parent equals A and its post-commit status is
  empty. Exercise the empty-label alternative separately, starting at A.
- Use the commit, tree ID, saved full listing, remote verification and continue
  reachability check as the snapshot. Remove a clean worktree only after the
  fixture successor has merged. Independently add an ignored file, a FIFO and a
  nested repository; each must refuse removal and keep the worktree. Omitting
  each corresponding removal check must fail its retention assertion.

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
  space. The procedure must query ls-remote, report the full remote and
  prepared heads (both R after this update), save old head A too, and stop
  before continuation or removal with the worktree retained. Verify A is an ancestor of R. Removing the
  new-ref-only flag check must fail the no-continuation assertion even though
  the push and subsequent remote-head comparison both succeed.
- Instead interpose a diverging commit. The documented push must exit nonzero
  with `!`; the remote remains at the other head and the retained worktree
  keeps the prepared commit. Query ls-remote and report its full head alongside
  the prepared head; reverting that instruction must fail the reporting check.
  No continuation or removal is allowed.

These tests supplement the full synthetic retained-attempt run through the
real `record_stop` path, continuation, successor replay and guarded removal.

## Supported envelope and accepted limits

- Supported: retained attempts as PR 256 records them, and an operator with push access to the anchored route.
- Excluded: any code change, any change to the coordinator, the mandate or skill declarations, and automation of these steps.

Supported envelope amendment (marked; orchestrator, 2026-10-20): one operator publishes a given recovery ref at a time, per the owner's 2026-10-20 decision that one terminal session is the sole operator. Two concurrent publishers of the same recovery ref are outside the envelope, and the page's porcelain check is the detection.
