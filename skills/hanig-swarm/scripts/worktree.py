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

# One per ATTEMPT: `launch-<attempt-id>.json`, beside the attempt rather than
# inside it. A single shared record let a retry inherit the previous attempt's
# baseline, so the previous attempt's commits satisfied the new attempt.
LAUNCH_RECORD = "launch.json"        # legacy name; see path construction

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


def repo_status(runner, repo, exclude=None):
    """(rc, dirty_lines) for `repo`, ignoring the coordinator's own state.

    Running a swarm inside the repository it works on is ordinary, and
    `allocate` writes `runs/<unit>/<attempt>/unit.json` BEFORE the anchor is
    taken. Counting that as dirt made `clean_at_launch` false for every code
    unit in such a layout, so honest work could never satisfy production. The
    coordinator's own files are attributable to the coordinator; they are not
    the agent's uncommitted work, which is what this predicate is about.
    """
    args = ["status", "--porcelain"]
    if exclude:
        try:
            rel = os.path.relpath(str(exclude), str(repo))
        except ValueError:
            rel = None
        if rel and not rel.startswith(os.pardir) and not os.path.isabs(rel):
            args += ["--", ".", f":(exclude){rel}"]
    rc, out, _ = _git(runner, repo, *args)
    return rc, [l for l in out.splitlines() if l.strip()]


def swarm_root_of(unit_dir):
    """<root>/<unit>/<attempt> -> <root>. The coordinator's state tree."""
    return str(Path(unit_dir).parent.parent)


def read_launch_record(unit_dir):
    """The anchor the coordinator wrote before the agent existed."""
    path = Path(unit_dir).parent / f"launch-{Path(unit_dir).name}.json"
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


def judge_detail(runner, unit_dir, spec):
    """(produced, head, detail).

    Returns the head it VALIDATED, not one re-read afterwards. Splitting those
    was a time-of-check/time-of-use gap: judge() checked commit B, and a
    second `rev-parse HEAD` a moment later could return C, because the agent
    owns that repository and nothing stops it moving HEAD. The binding then
    pinned C, which nothing had judged.

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
    # A missing `repo` key and an explicit `"repo": null` are deliberately
    # the SAME answer. A reviewer wanted them distinguished; there is no
    # action a reader would take differently, and inventing a distinction
    # nobody acts on is how a vocabulary starts lying.
    if not spec.get("repo"):
        return None, None, ("this unit declared no repository, so no git transition "
                      "is judged for it")

    rec, err = read_launch_record(unit_dir)
    if err:
        return False, None, err
    repo = rec.get("repo")
    if not repo:
        return False, None, (
            f"this unit declares repo {spec['repo']!r}, but its launch record "
            f"anchored no repository. The anchor was written before the unit "
            f"declared one, or _write_launch_record failed: either way "
            f"nothing captured a baseline, so re-dispatch this unit rather "
            f"than reading this as a configuration mistake")
    if not os.path.isdir(repo):
        return False, None, f"the anchored repository {repo!r} is gone"

    if not rec.get("clean_at_launch", False):
        return False, None, (
            f"the repository was already dirty at launch "
            f"({rec.get('dirty_paths_at_launch', '?')} path(s)), so there was "
            f"no clean state to transition FROM and any change now is "
            f"unattributable to this attempt")

    # `--porcelain` without `--ignored` ON PURPOSE. A reviewer read the
    # omission as a hole: an agent can leave an uncommitted file that
    # .gitignore covers, and both cleanliness checks still pass. True, and
    # taking `--ignored` would fail every repository with a venv, a build
    # directory or __pycache__, which is a nuisance failure on honest work.
    # An ignored path is DECLARED not to be part of the artifact, by the
    # repository itself, and production here is a claim about the committed
    # tree. The claim was too broad; the code is right.
    rc, dirty = repo_status(runner, repo, exclude=swarm_root_of(unit_dir))
    if rc != 0:
        return False, None, f"cannot read git status in {repo!r}"
    if dirty:
        return False, None, (
            f"{len(dirty)} path(s) are uncommitted. Work left in the working "
            f"tree is not production: it is recorded nowhere another attempt "
            f"or reader could find it")

    rc, branch, _ = _git(runner, repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc == 0 and rec.get("branch") and branch != rec["branch"]:
        return False, None, (f"the repository is on branch {branch!r}, but this "
                       f"attempt was anchored on {rec['branch']!r}")

    base = rec.get("base_commit")
    rc, head, _ = _git(runner, repo, "rev-parse", "HEAD")
    if rc != 0:
        return False, None, f"cannot read HEAD in {repo!r}"
    if head == base:
        return False, None, ("HEAD has not moved since launch, so nothing was "
                       "committed")

    rc, _, _ = _git(runner, repo, "merge-base", "--is-ancestor", base, head)
    if rc != 0:
        return False, None, (
            f"HEAD {head[:12]} does not descend from the anchored base "
            f"{str(base)[:12]}. The history was replaced rather than extended, "
            f"so what is there now was not built on what we anchored")

    # The tree of the CAPTURED head, not of HEAD. Reading `HEAD^{tree}` was a
    # second look at a moving target: the agent could leave an empty
    # descendant at HEAD for the first read and a content-changing one for
    # this, so the tree that satisfied the check belonged to a commit other
    # than the one returned and pinned.
    rc, tree, _ = _git(runner, repo, "rev-parse", head + "^{tree}")
    if rc != 0:
        return False, None, f"cannot read HEAD's tree in {repo!r}"
    if tree == rec.get("base_tree"):
        return False, None, (
            "HEAD advanced but its tree is identical to the anchored base "
            "tree, so the content is unchanged. An empty commit, or a change "
            "reverted before committing, moves HEAD without producing "
            "anything")

    return True, head, (f"tree {tree[:12]} differs from the anchored base tree "
                  f"{str(rec.get('base_tree'))[:12]}, on a commit descending "
                  f"from {str(base)[:12]}, with a clean tree at both ends")


def judge(runner, unit_dir, spec):
    """(produced, detail). The two-value view, for callers that do not need
    the head."""
    produced, _head, why = judge_detail(runner, unit_dir, spec)
    return produced, why


def produced_head(runner, unit_dir, spec):
    """The commit this attempt produced, or None.

    What a merge attestation gets PINNED to. Without it the attester names
    whatever commit it likes and the coordinator has no way to object; with
    it, an attestation about some other branch's work cannot close this unit.
    """
    _produced, head, _why = judge_detail(runner, unit_dir, spec)
    return head


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
