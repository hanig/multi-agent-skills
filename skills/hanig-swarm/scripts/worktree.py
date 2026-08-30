#!/usr/bin/env python3
"""Did a code unit actually produce a committed change?

Split out of unit.py, which has a size guard precisely to stop it quietly
becoming a library. Raising that guard because this tripped it would be how a
guard dies; the judging is a separable concern, so it separates.

unit.py used to say the agent's git worktree was judged by
`bus await --base HEAD --require-clean` and that reimplementing it "would be
the mistake this plan exists to undo". We never called it, so nothing judged
the worktree at all. And that predicate was never production evidence: the
caller supplies the base, so HEAD may already be past the work, and a clean
tree is clean precisely when nobody touched it.

Python 3.8+, standard library only.
"""
import json
import os
from pathlib import Path

LAUNCH_RECORD = "launch.json"

# What a produced-tree receipt does NOT claim. Written out because a reader
# six weeks later sees a production verdict and needs to know its exact reach.
PRODUCTION_DENIES = (
    "quality", "relevance", "tests-pass", "authorship", "review",
    "pull-request", "merge",
)


def _git(runner, repo, *args, timeout=60):
    rc, out, err = runner(["git", "-C", str(repo)] + list(args),
                          timeout=timeout)
    return rc, (out or "").strip(), (err or "").strip()


def read_launch_record(unit_dir):
    """The anchor the coordinator wrote before the agent existed."""
    path = Path(unit_dir).parent / LAUNCH_RECORD
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except FileNotFoundError:
        return None, ("no launch record: nothing captured this repository's "
                      "state before the agent ran, so no transition can be "
                      "judged. Re-dispatch through the coordinator.")
    except (OSError, ValueError) as exc:
        return None, f"launch record at {path} is unreadable: {exc}"
    if not isinstance(rec, dict):
        return None, f"launch record at {path} is not an object"
    return rec, None


def judge(runner, unit_dir, spec):
    """(produced, detail).

    `produced` is None when the unit declared no repository, so there is
    nothing to judge; False when a repository was declared and did not
    transition; True when it did.

    Every clause exists because its absence admits work that never happened:

      descends-from-base  else an unrelated-history reset, or a branch already
                          ahead at launch, reads as production.
      tree differs        else an empty commit, or a change reverted before
                          committing, reads as production. The comparison is
                          TREE to TREE, not commit to commit, because a commit
                          always differs from its parent.
      clean at both ends  dirty output is unattributable, and dirt at launch
                          means there was no clean state to start from.
      same repo/branch    else a transition somewhere else counts here.
    """
    # The SPEC decides whether there is anything to judge. Asking the launch
    # record first conflated "declared no repository" with "was never
    # anchored", and those call for opposite responses.
    if not spec.get("repo"):
        return None, ("this unit declared no repository, so no git transition "
                      "is judged for it")

    rec, err = read_launch_record(unit_dir)
    if err:
        return False, err
    repo = rec.get("repo")
    if not repo:
        return False, ("the launch record anchored no repository, but this "
                       "unit declares one, so the anchor does not match the "
                       "unit it belongs to")
    if not os.path.isdir(repo):
        return False, f"the anchored repository {repo!r} is gone"

    if not rec.get("clean_at_launch", False):
        return False, (
            f"the repository was already dirty at launch "
            f"({rec.get('dirty_paths_at_launch', '?')} path(s)), so there was "
            f"no clean state to transition FROM and any change now is "
            f"unattributable to this attempt")

    rc, status, _ = _git(runner, repo, "status", "--porcelain")
    if rc != 0:
        return False, f"cannot read git status in {repo!r}"
    if status:
        return False, (
            f"{len(status.splitlines())} path(s) are uncommitted. Work left in "
            f"the working tree is not production: it is recorded nowhere "
            f"another attempt or reader could find it")

    rc, branch, _ = _git(runner, repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc == 0 and rec.get("branch") and branch != rec["branch"]:
        return False, (f"the repository is on branch {branch!r}, but this "
                       f"attempt was anchored on {rec['branch']!r}")

    base = rec.get("base_commit")
    rc, head, _ = _git(runner, repo, "rev-parse", "HEAD")
    if rc != 0:
        return False, f"cannot read HEAD in {repo!r}"
    if head == base:
        return False, ("HEAD has not moved since launch, so nothing was "
                       "committed")

    rc, _, _ = _git(runner, repo, "merge-base", "--is-ancestor", base, head)
    if rc != 0:
        return False, (
            f"HEAD {head[:12]} does not descend from the anchored base "
            f"{str(base)[:12]}. The history was replaced rather than extended, "
            f"so what is there now was not built on what we anchored")

    rc, tree, _ = _git(runner, repo, "rev-parse", "HEAD^{tree}")
    if rc != 0:
        return False, f"cannot read HEAD's tree in {repo!r}"
    if tree == rec.get("base_tree"):
        return False, (
            "HEAD advanced but its tree is identical to the anchored base "
            "tree, so the content is unchanged. An empty commit, or a change "
            "reverted before committing, moves HEAD without producing "
            "anything")

    return True, (f"tree {tree[:12]} differs from the anchored base tree "
                  f"{str(rec.get('base_tree'))[:12]}, on a commit descending "
                  f"from {str(base)[:12]}, with a clean tree at both ends")


def basis(runner, unit_dir, spec):
    """What the receipt can say about the agent's repository.

    A string, not a bool: "we did not look", "there was nothing to look at"
    and "we looked and it produced" are three different claims, and a boolean
    carries only two.
    """
    produced, _why = judge(runner, unit_dir, spec)
    if produced is None:
        return "no-repository-declared"
    return "produced-committed-change" if produced else "no-produced-change"
