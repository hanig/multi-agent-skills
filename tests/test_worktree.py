"""Judging whether a code unit produced a committed change.

Each test is a way for work to LOOK like it happened. `bus await --base HEAD
--require-clean`, which unit.py used to defer to and never called, passes
several of them: the caller picks the base, so HEAD may already be past the
work, and a clean tree is clean exactly when nobody touched it.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import worktree as W  # noqa: E402
import unit as U  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def git(repo, *a):
    return subprocess.run(["git", "-C", repo] + list(a), check=True, env=ENV,
                          capture_output=True, text=True)


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "r")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True, env=ENV)
        self.write("a.txt", "one\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c1")
        self.unit_dir = os.path.join(self.tmp, "runs", "u1", "att1")
        os.makedirs(self.unit_dir)
        self.spec = {"id": "u1", "kind": "code", "repo": self.repo}

    def write(self, name, text):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(text)

    def anchor(self, **over):
        rc, head, _ = W._git(U.run, self.repo, "rev-parse", "HEAD")
        rc, tree, _ = W._git(U.run, self.repo, "rev-parse", "HEAD^{tree}")
        rc, br, _ = W._git(U.run, self.repo, "rev-parse", "--abbrev-ref",
                           "HEAD")
        rec = {"repo": self.repo, "branch": br, "base_commit": head,
               "base_tree": tree, "clean_at_launch": True,
               "dirty_paths_at_launch": 0}
        rec.update(over)
        # Seal what was WRITTEN, the way the coordinator does: it digests the
        # bytes it wrote and keeps the digest in its own state.
        payload = json.dumps(rec)
        W.launch_record_path(self.unit_dir).write_text(payload)
        self.seal = hashlib.sha256(payload.encode()).hexdigest()
        return rec

    def judge(self):
        return W.judge(U.run, self.unit_dir, self.spec,
                       getattr(self, "seal", None))


class TestWorkThatDidNotHappen(Base):

    def test_no_commit_at_all(self):
        self.anchor()
        produced, why = self.judge()
        self.assertFalse(produced)
        self.assertIn("HEAD has not moved", why)

    def test_an_empty_commit_is_not_production(self):
        """Moves HEAD, changes nothing. This is why the comparison is tree to
        tree: a commit always differs from its parent."""
        self.anchor()
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "nothing")
        produced, why = self.judge()
        self.assertFalse(produced)
        self.assertIn("tree is identical", why)

    def test_change_then_revert_before_committing(self):
        self.anchor()
        self.write("a.txt", "two\n")
        self.write("a.txt", "one\n")
        produced, why = self.judge()
        self.assertFalse(produced)
        self.assertIn("HEAD has not moved", why)

    def test_change_committed_then_reverted_and_committed(self):
        """Two commits, net zero. HEAD descends from base and is clean, so
        every predicate except the tree comparison passes."""
        self.anchor()
        self.write("a.txt", "two\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c2")
        self.write("a.txt", "one\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c3")
        produced, why = self.judge()
        self.assertFalse(produced)
        self.assertIn("tree is identical", why)

    def test_uncommitted_work_is_not_production(self):
        self.anchor()
        self.write("b.txt", "new\n")
        produced, why = self.judge()
        self.assertFalse(produced)
        self.assertIn("uncommitted", why)

    def test_an_unrelated_history_reset_does_not_count(self):
        """The branch is reset onto an orphan history: SAME branch name, clean
        tree, different content, and no descent from what we anchored. Every
        other predicate passes, which is what makes this the interesting case
        rather than the branch check catching it first."""
        rec = self.anchor()
        branch = rec["branch"]
        git(self.repo, "checkout", "-q", "--orphan", "fresh")
        git(self.repo, "rm", "-rqf", ".")
        self.write("z.txt", "unrelated\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "orphan")
        # Point the anchored branch at the unrelated history and go back to it.
        git(self.repo, "branch", "-f", branch, "HEAD")
        git(self.repo, "checkout", "-q", branch)
        produced, why = self.judge()
        self.assertFalse(produced)
        self.assertIn("does not descend", why)

    def test_dirty_at_launch_disqualifies(self):
        """No clean state to transition FROM, so a change now is
        unattributable to this attempt."""
        self.anchor(clean_at_launch=False, dirty_paths_at_launch=3)
        self.write("b.txt", "new\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c2")
        produced, why = self.judge()
        self.assertFalse(produced)
        self.assertIn("already dirty at launch", why)

    def test_a_different_branch_does_not_count(self):
        self.anchor()
        git(self.repo, "checkout", "-qb", "elsewhere")
        self.write("b.txt", "new\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c2")
        produced, why = self.judge()
        self.assertFalse(produced)
        self.assertIn("anchored on", why)

    def test_a_missing_anchor_is_not_production(self):
        self.write("b.txt", "new\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c2")
        produced, why = self.judge()
        self.assertFalse(produced)
        self.assertIn("no launch record", why)


class TestWorkThatDidHappen(Base):

    def test_a_committed_change_is_production(self):
        self.anchor()
        self.write("b.txt", "new\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c2")
        produced, why = self.judge()
        self.assertTrue(produced)
        self.assertIn("differs from the anchored base tree", why)

    def test_several_commits_still_count(self):
        self.anchor()
        for n in ("b.txt", "c.txt"):
            self.write(n, "x\n")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-qm", n)
        self.assertTrue(self.judge()[0])


class TestNothingToJudge(Base):

    def test_a_unit_declaring_no_repo_is_not_a_failure(self):
        """None, not False. "Declared no repository" and "declared one and did
        not transition" call for opposite responses."""
        produced, why = W.judge(U.run, self.unit_dir, {"id": "u1",
                                                       "kind": "code"})
        self.assertIsNone(produced)
        self.assertIn("declared no repository", why)

    def test_the_spec_decides_before_the_anchor_is_read(self):
        """Asking the anchor first conflated "no repository" with "never
        anchored"."""
        produced, _ = W.judge(U.run, "/nonexistent/att", {"id": "u1"})
        self.assertIsNone(produced)


class TestTheReceiptSaysWhatItDoesNotCover(Base):

    def test_basis_distinguishes_three_outcomes(self):
        self.assertEqual(W.basis(U.run, self.unit_dir, {"id": "u"}),
                         "no-repository-declared")
        self.anchor()
        self.assertEqual(W.basis(U.run, self.unit_dir, self.spec, self.seal),
                         "no-produced-change")
        self.write("b.txt", "new\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c2")
        self.assertEqual(W.basis(U.run, self.unit_dir, self.spec, self.seal),
                         "produced-committed-change")

    def test_production_denies_the_claims_it_cannot_make(self):
        for denied in ("tests-pass", "review", "merge", "quality"):
            self.assertIn(denied, W.PRODUCTION_DENIES)



class TestTheFeatureIsReachableAtAll(Base):
    """It was not. `unit.json` had no `repo` field and `allocate` had no
    `--repo`, so `spec.get("repo")` was always None and judge() always said
    "declared no repository". The whole transition check was dead code.

    Every earlier test passed because it hand-built a spec dict with `repo`
    in it. A fixture that cannot diverge from what production writes is the
    only kind that would have caught this, so these go through allocate.
    """

    def _allocate(self, repo=None):
        root = os.path.join(self.tmp, "root")
        argv = [sys.executable, str(SCRIPTS / "unit.py"), "allocate",
                "--root", root, "--task", "u1", "--kind", "code",
                "--output", "out.txt"]
        if repo:
            argv += ["--repo", repo]
        r = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        unit_dir = r.stdout.strip().splitlines()[-1].strip()
        return unit_dir, json.load(open(os.path.join(unit_dir, "unit.json")))

    def test_allocate_records_the_repo_in_the_spec(self):
        _dir, spec = self._allocate(self.repo)
        self.assertEqual(spec["repo"], self.repo)

    def test_a_spec_written_by_allocate_reaches_the_judge(self):
        unit_dir, spec = self._allocate(self.repo)
        produced, why = W.judge(U.run, unit_dir, spec)
        self.assertIsNot(produced, None,
                         "the judge saw no repository in a spec that "
                         "declares one, so the check is dead code")

    def test_without_repo_the_spec_says_so(self):
        _dir, spec = self._allocate()
        self.assertIsNone(spec["repo"])


class TestARetryDoesNotInheritTheLastAttemptsBaseline(Base):
    """One shared `launch.json` per unit meant a retry kept the FIRST
    attempt's anchor, so the first attempt's commits satisfied the second."""

    def test_each_attempt_anchors_separately(self):
        sys.path.insert(0, str(SCRIPTS))
        import swarm as S

        att1 = self.unit_dir                       # runs/u1/att1
        att2 = os.path.join(self.tmp, "runs", "u1", "att2")
        os.makedirs(att2)
        u = {"id": "u1", "repo": self.repo}

        err1, anchor1 = S._write_launch_record(att1, u)
        self.assertIsNone(err1)
        # att1 commits, then is interrupted.
        self.write("b.txt", "from att1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "att1 work")
        # att2 starts here and produces NOTHING.
        err2, anchor2 = S._write_launch_record(att2, u)
        self.assertIsNone(err2)
        # Each attempt's own seal, from the writer, as the coordinator keeps
        # them keyed by attempt in its state.
        self.assertNotEqual(anchor1["seal"], anchor2["seal"])

        produced, why = W.judge(U.run, att2, {"id": "u1", "kind": "code",
                                              "repo": self.repo},
                                anchor2["seal"])
        self.assertFalse(produced,
                         "att2 produced nothing, but inherited att1's anchor "
                         "so att1's commit counted as att2's production")
        self.assertIn("HEAD has not moved", why)

    def test_the_first_attempts_verdict_is_unaffected(self):
        sys.path.insert(0, str(SCRIPTS))
        import swarm as S
        att1 = self.unit_dir
        _err, anchor = S._write_launch_record(att1,
                                              {"id": "u1", "repo": self.repo})
        self.write("b.txt", "real work\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "att1 work")
        produced, _ = W.judge(U.run, att1, {"id": "u1", "kind": "code",
                                            "repo": self.repo},
                              anchor["seal"])
        self.assertTrue(produced)


class TestCoordinatorFilesAreNeverBlanketExcluded(Base):
    """External defaults removed the reason for hiding a whole path prefix."""

    def setUp(self):
        super().setUp()
        # The coordinator's state tree lives INSIDE the repository.
        self.inside = os.path.join(self.repo, "runs", "u1", "att1")
        os.makedirs(self.inside)
        with open(os.path.join(self.inside, "unit.json"), "w") as fh:
            json.dump({"task_id": "u1"}, fh)

    def test_a_plan_authored_path_is_reported_as_dirt(self):
        rc, dirty = W.repo_status(U.run, self.repo)
        self.assertEqual(rc, 0)
        self.assertEqual(len(dirty), 1)
        self.assertEqual(dirty[0]["path"], "runs/u1/att1/unit.json")

    def test_without_the_exclusion_it_looks_dirty(self):
        """The control: this is what the judge used to see."""
        rc, dirty = W.repo_status(U.run, self.repo)
        self.assertEqual(rc, 0)
        self.assertTrue(dirty)

    def test_other_dirt_is_also_caught(self):
        self.write("leftover.txt", "uncommitted\n")
        _rc, dirty = W.repo_status(U.run, self.repo)
        self.assertEqual(len(dirty), 2)

    def test_an_in_repository_run_root_is_refused_at_preflight(self):
        sys.path.insert(0, str(SCRIPTS))
        import swarm as S
        u = {"id": "u1", "repo": self.repo}
        err, _base = S._write_launch_record(self.inside, u)
        self.assertIn("preflight refused", err or "")
        rec = json.load(open(Path(self.inside).parent
                             / ("launch-%s.json" % Path(self.inside).name)))
        self.assertEqual(rec["preflight"]["status"], "refused")

    def test_a_missing_anchor_names_the_declared_repo(self):
        produced, why = W.judge(U.run, self.inside,
                                {"id": "u1", "kind": "code",
                                 "repo": self.repo})
        self.assertFalse(produced)
        self.assertIn("no launch record", why)

if __name__ == "__main__":
    unittest.main()
