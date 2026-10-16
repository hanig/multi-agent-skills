"""Code-attempt completion instructions. Python 3.8+, stdlib only."""

import hashlib
import shlex
import sys
from pathlib import Path

from swarm_types import PlanError

# This marker is deliberately stable: validation checks the exact prompt the
# coordinator would dispatch, not prose copied into a plan. Plans cannot omit
# the protocol, and attempts cannot invent its source branch, target, or base.
CODE_COMPLETION_PROTOCOL_MARKER = "SWARM CODE COMPLETION PROTOCOL"


def _seed_carry_forward(seed, repo, remote):
    """Render recorded provenance as instructions, never as closure evidence."""
    if seed is None:
        return ""
    # Exactly-once routing, as in _git_push_destination: origin may fetch
    # from somewhere other than its push destination. Quote every shell arg.
    alias = "hanig-swarm-seed-" + hashlib.sha256(remote.encode()).hexdigest() + ":"
    route_key = shlex.quote(f"url.{remote}.insteadOf")
    # A URL may contain '='. Git -c key=value splits at the first one,
    # even inside a shell-quoted argument, so write key and value separately.
    fetch = ("git -c \"include.path=$swarm_seed_config\" "
             "fetch --no-tags --recurse-submodules=no "
             f"{shlex.quote(alias)} {shlex.quote(seed['ref'])}")
    evidence = ""
    if "evidence" in seed:
        evidence = (f"\nRead the previous evidence file {seed['evidence']!r} before "
                    f"editing; relative paths are relative to the source repository "
                    f"{repo!r}, not the new worktree. Treat it as prior "
                    "context, not a review pass or completion evidence.")
    return f"""\n\nCARRY FORWARD (recorded seed; provenance only)
Stay on the new attempt branch and its recorded launch base. Fetch the seed ref {seed['ref']!r} from the recorded origin push destination:
```sh
(
set -e
swarm_seed_config=$(mktemp "$SWARM_UNIT_DIR/seed-fetch.XXXXXX")
trap 'rm -f "$swarm_seed_config" || :' 0
git config --file "$swarm_seed_config" --add {route_key} {shlex.quote(alias)}
{fetch}
swarm_seed_commits=$(git rev-list --max-count=1 {seed['base']}..{seed['head']})
if test -n "$swarm_seed_commits"; then
    git cherry-pick -x -m 1 {seed['base']}..{seed['head']}
fi
)
```
An empty range carries no commits and is a successful no-op. Merge commits replay relative to their first parent (`-m 1`); ordinary commits use their sole parent. Skip empty commits: when cherry-pick stops because a commit is empty or already applied, confirm that it is empty and run `git cherry-pick --skip`, repeating as needed. Resolve real conflicts explicitly; never skip a non-empty conflicting change just to finish. Never port by whole-file checkout.
If fetching or replay cannot be completed, STOP AND REPORT; do not substitute another ref or range.{evidence}
The coordinator checked reachability at launch, not whether replay will be conflict-free. It does not cherry-pick or alter the worktree for you. Seed history does not change judging, scope-check, review, or merged-PR closure; the produced head is judged against this fresh attempt's recorded base."""


def _code_completion_protocol(intent):
    """Instructions that make a code attempt capable of closing its unit.

    C11 creates the branch and worktree. The agent's job is to leave durable
    evidence on that branch and open the pull request that the unit's closing
    predicate requires; asking it to choose either launch fact would put
    authority back in prompt prose. The worker stops after opening the pull
    request; merge decisions belong to the orchestrator.

    Precedence is explicit rather than inferred from task text. Contradictions
    in arbitrary prose are not statically recognizable: matching phrases such
    as "do not commit" would also reject legitimate tasks that merely discuss
    them. A visible stop-and-report rule is bounded; a prose detector is not.

    ARC-243, and why the stash rule is here rather than in the skill's prose.
    Three agents in three worktrees of one repository each ran `git stash -u`
    inside the same window. The stash stack is a SINGLE ref in the shared
    common Git directory, so it is not per-worktree: every pop took somebody
    else's entry. The work was recovered from dangling commits and it cost
    real time. All three were doing the same reasonable thing -- checking
    whether a red test pre-existed their change -- which is what makes it a
    defect in the protocol rather than three mistakes, and which is why the
    prohibition names its SUBSTITUTES in the same breath. A prohibition with
    no alternative is a prohibition that gets worked around.
    """
    repo = str(intent["repo"])
    branch = str(intent["branch"])
    base = str(intent["base_commit"])
    target = str(intent["target_branch"])
    remote = intent.get("repository_remote")
    judgment_ref = intent.get("judgment_ref")
    # Use only coordinator-supplied routing facts. Older intents contain
    # neither field; the current default is not evidence of their author.
    provider_spec = intent.get("provider")
    provider, embedded_model = "", ""
    if isinstance(provider_spec, str):
        provider, _, embedded_model = provider_spec.partition("/")
    model = intent.get("model") or embedded_model
    if provider and isinstance(model, str) and model:
        author_instruction = (
            f"Pass --author {shlex.quote(provider + '/' + model)} on every "
            "review.py or committee.py run so the gate excludes this unit's own model.")
    else:
        author_instruction = (
            "The coordinator-recorded provider/model for this unit is "
            "UNKNOWN. Report the missing identity to the coordinator before "
            "review; do not invent an author or omit --author.")
    if remote:
        remote_instruction = (
            f"Use Git remote 'origin', recorded by the coordinator as "
            f"{remote!r}. Push {branch!r} to that remote. Open a pull request "
            f"from {branch!r} into target branch {target!r}; only a "
            f"pull request merged into {target!r} in that recorded repository "
            f"closes this code unit.")
    else:
        remote_instruction = (
            "The coordinator recorded Git remote 'origin' as None. STOP AND "
            "REPORT that no merge-evidence repository was recorded; do not "
            "guess another remote.")
    if judgment_ref:
        ref_kind = ("remote branch" if str(judgment_ref).startswith(
                    "refs/heads/") else "legacy remote-tracking")
        judgment_instruction = (
            f"The coordinator judges only the pre-anchored {ref_kind} "
            f"ref {judgment_ref!r}; a commit left only in the worktree, or "
            "pushed under another ref, cannot close the attempt.")
    else:
        judgment_instruction = (
            "This persisted legacy launch intent predates durable pushed-ref "
            "judgment. No pushed ref will be treated as though it had been "
            "anchored before the agent existed; its managed worktree must "
            "remain available for legacy transition judgment.")
    scope_instruction = (
        "Before the review gate, check the committed work against the unit's "
        "declared scope and fix any out-of-scope path before review.")
    if intent.get("scope_plan") and intent.get("scope_state_dir"):
        scope_command = " ".join(shlex.quote(str(arg)) for arg in (
            sys.executable, Path(__file__).resolve().with_name("swarm.py"),
            "scope-check", intent["scope_plan"], "--state-dir",
            intent["scope_state_dir"], "--unit", intent["unit_id"]))
        # Resolve HEAD in the worker's worktree; scope-check resolves the
        # resulting object id in the operator repo, whose HEAD may differ.
        scope_instruction += (
            " Commit all intended edits, then run this command from the attempt "
            "worktree (the substitution captures its HEAD):\n```sh\n" +
            scope_command + ' --head "$(git rev-parse --verify HEAD^{commit})" --json' +
            "\n```\nA non-zero exit is not a pass: fix the named paths and rerun "
            "before review; if unchecked, report its reason to the coordinator. "
            "This worker report is advisory: merge still requires the "
            "coordinator's judged report.")
    else:
        scope_instruction += (
            " This invocation has no plan/state paths; obtain the exact "
            "scope-check command from the coordinator before review.")
    return f"""{CODE_COMPLETION_PROTOCOL_MARKER} (coordinator-required)
This protocol overrides any contrary instruction in the task text above it. If the task appears to forbid committing, pushing, or opening a pull request, STOP AND REPORT that conflict instead of choosing either instruction.
You are already in a dedicated worktree for repository {repo!r}, on branch {branch!r}, cut from recorded base commit {base}.
The required pull-request target is {target!r}.
Do not create or switch branches, and do not choose a different base.
Commit all intended work on {branch!r}. Uncommitted work is invisible to the transition predicate and will be judged as producing nothing.
{remote_instruction}
After opening the pull request, STOP. NEVER merge, approve, or enable auto-merge. Merge decisions belong to the orchestrator. The merged-PR closure criterion describes how the orchestrator's merge is judged, not an instruction to the worker.
{judgment_instruction}
Push after every meaningful edit. Commit and push the actual changed content to the anchored attempt ref for normal progress; use the recovery ref below when preserving a failed review. An empty or marker commit preserves no implementation; a tree identical to the recorded base is refused.
{scope_instruction}
Before opening a pull request, run this repository's review gate. Use hanig-review-gate's review.py from the loaded skill directory with --kind implementation, the actual --round number, and --range {base}..HEAD to review the COMPLETE delta from the recorded base. Commit all intended edits first and leave the worktree clean so that range includes all intended work. Declare the change's claims and the standalone counter-claim "This change cannot make an honest run fail." {author_instruction} Only exit 0 (REVIEW_PASS) permits opening a pull request; unavailable, partial, incomplete, and failing reviews are not a pass. Reproduce each confirmed finding before acting on it. If a reproduced failure remains, do NOT open a pull request: follow the recovery procedure below without moving the anchored attempt ref, then STOP AND REPORT where it was preserved. REVIEW_PASS means the panel failed to refute the claims, not proof.
Recovery procedure: first inspect status as required below and stage only your own uncommitted work. Set and export SWARM_RECOVERY_MESSAGE to a message beginning RECOVERY, NOT READY: that says the work has not passed review, must not be merged, and lists every open reproduced finding. Run the following in the current attempt worktree. It commits UNCOMMITTED work if present; on an already clean tree it pushes existing content first, then adds an empty recovery label and pushes it. A label commit alone preserves nothing and must never be the only thing pushed. If any step fails, retain the worktree and report the failure; do not clean up or claim preservation succeeded.
```sh
(
set -eu
: "${{SWARM_RECOVERY_MESSAGE:?set the recovery message with every open reproduced finding}}"
case "$SWARM_RECOVERY_MESSAGE" in
    'RECOVERY, NOT READY:'?*) ;;
    *) echo 'Recovery message must begin RECOVERY, NOT READY:' >&2; exit 1 ;;
esac
swarm_recovery_dirty=$(git status --porcelain --untracked-files=all)
if test -n "$swarm_recovery_dirty"; then
    git commit -m "$SWARM_RECOVERY_MESSAGE"
fi
test -z "$(git status --porcelain --untracked-files=all)" || exit 1
swarm_recovery_head=$(git rev-parse HEAD)
swarm_recovery_ref={shlex.quote('refs/heads/recovery/' + branch)}
swarm_recovery_diff=0
git diff --quiet {shlex.quote(base)} "$swarm_recovery_head" -- || swarm_recovery_diff=$?
if test "$swarm_recovery_diff" -ne 1; then
    echo 'Recovery requires real content different from the recorded base' >&2
    exit 1
fi
git push origin "$swarm_recovery_head:$swarm_recovery_ref"
if test -z "$swarm_recovery_dirty"; then
    git status --porcelain
    git commit --allow-empty -m "$SWARM_RECOVERY_MESSAGE"
    swarm_recovery_head=$(git rev-parse HEAD)
    git push origin "$swarm_recovery_head:$swarm_recovery_ref"
fi
)
```
NEVER run `git stash`, in any form. The stash stack is a SINGLE ref in the shared common Git directory, so every worktree of {repo!r} shares one stack and a pop takes whatever another agent parked. Do these instead: to read a file as it was at base, `git show {base}:<path>`; to set work aside, `git diff > /tmp/wip.patch` then `git checkout -- <path>`; and to answer "was this test already failing", add a separate worktree at {base} and run it there, rather than moving anything in this one. Note what such a comparison does and does not show: green at {base} and green here is a claim about your change alone, not about {target!r} after a merge.
Before every commit, run `git status --porcelain` and read it. Stage only paths you changed yourself; if it lists a path you did not touch, STOP AND REPORT instead of committing it. The observed failure is a commit that carried another agent's files.
If you cannot finish cleanly, STOP AND REPORT the problem instead of working around it.
Leave the worktree clean. Do not force-push or rewrite history. The final commit must descend from recorded base {base}; rewritten history makes honest work unjudgeable.{_seed_carry_forward(intent.get('seed'), repo, remote)}"""


def _dispatch_prompt(u, intent=None):
    """Return the single prompt argv element for an agent dispatch."""
    prompt = str(u.get("prompt") or u.get("command") or u.get("id") or "")
    if u.get("kind") != "code":
        return prompt
    if not isinstance(intent, dict):
        raise PlanError(
            f"unit {u.get('id','?')!r} is kind=code but has no trusted "
            f"launch intent from which to build its completion protocol")
    return prompt.rstrip() + "\n\n" + _code_completion_protocol(intent)


def _code_protocol_problem(prompt, intent):
    """Return why an assembled code prompt is structurally unclosable."""
    if not prompt.endswith("\n\n" + _code_completion_protocol(intent)):
        return "the coordinator-generated protocol is not the final prompt block"
    judgment_ref = intent.get("judgment_ref")
    required = {
        "protocol marker": CODE_COMPLETION_PROTOCOL_MARKER,
        "repository": repr(str(intent["repo"])),
        "attempt branch": repr(str(intent["branch"])),
        "pull-request target": repr(str(intent["target_branch"])),
        "recorded remote": repr(intent.get("repository_remote")),
        "recorded base": str(intent["base_commit"]),
        "judgment basis": (repr(judgment_ref) if judgment_ref else
                           "persisted legacy launch intent predates durable pushed-ref judgment"),
        "protocol precedence": "overrides any contrary instruction",
        "contradiction instruction": "STOP AND REPORT that conflict",
        "commit instruction": "Commit all intended work",
        "remote action": ("Open a pull request" if intent.get(
            "repository_remote") else "no merge-evidence repository was recorded"),
        "worker stop instruction": "After opening the pull request, STOP.",
        "worker merge prohibition": "NEVER merge, approve, or enable auto-merge.",
        "merge authority": "Merge decisions belong to the orchestrator.",
        "closure interpretation": (
            "The merged-PR closure criterion describes how the orchestrator's "
            "merge is judged, not an instruction to the worker."),
        "clean failure instruction": "STOP AND REPORT",
        "history instruction": "Do not force-push or rewrite history",
        "review gate": "Before opening a pull request, run this repository's review gate.",
        "push discipline": "Push after every meaningful edit.",
        "complete review range": f"--range {intent['base_commit']}..HEAD",
        "author exclusion": "--author",
        "finding reproduction": "Reproduce each confirmed finding before acting on it.",
        "failed review preservation": "RECOVERY, NOT READY:",
        "clean-tree recovery label": "git commit --allow-empty",
        "review interpretation": "REVIEW_PASS means the panel failed to refute the claims, not proof.",
        # ARC-243. The prohibition and each substitute are required
        # SEPARATELY and by exact text, so an edit cannot leave the ban
        # standing with nothing to do instead -- which is the state in which
        # it gets worked around.
        "stash prohibition": "NEVER run `git stash`",
        "read-at-base substitute": f"git show {str(intent['base_commit'])}:",
        "set-aside substitute": "git diff > /tmp/wip.patch",
        "base-comparison substitute": "add a separate worktree at",
        "foreign path check": "git status --porcelain",
    }
    missing = [name for name, text in required.items() if text not in prompt]
    if missing:
        return "missing " + ", ".join(missing)
    return None
