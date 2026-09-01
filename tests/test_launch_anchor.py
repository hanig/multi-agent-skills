"""The launch record: anchoring a code unit before its agent exists.

`unit.py` used to say the agent's git worktree was judged by
`bus await --base HEAD --require-clean`, and that reimplementing it "would be
the mistake this plan exists to undo". Two things were wrong with that. We
never called it, so nothing judged the worktree at all. And the predicate it
named is not production evidence: the caller picks `--base`, so HEAD may have
advanced past the work already, and a clean tree is clean exactly when nobody
touched it.

An anchor captured before the agent exists, by the coordinator rather than the
worker, is what makes a later transition mean anything.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
sys.path.insert(0, str(SWARM.parent))
import swarm as S  # noqa: E402


def _repo(path, commit=True):
    os.makedirs(path, exist_ok=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    subprocess.run(["git", "init", "-q", path], check=True, env=env)
    if commit:
        with open(os.path.join(path, "a.txt"), "w") as fh:
            fh.write("one\n")
        subprocess.run(["git", "-C", path, "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", path, "commit", "-qm", "c1"],
                       check=True, env=env)
    return path


class TestTheAnchorIsCapturedBeforeDispatch(unittest.TestCase):

    def _attempt(self, tmp):
        d = os.path.join(tmp, "runs", "u1", "att1")
        os.makedirs(d, exist_ok=True)
        return d

    def test_a_declared_repo_is_anchored(self):
        with tempfile.TemporaryDirectory() as t:
            repo = _repo(os.path.join(t, "r"))
            d = self._attempt(t)
            err, base = S._write_launch_record(d, {"id": "u1", "repo": repo})
            self.assertIsNone(err)
            self.assertTrue(base)
            rec = json.load(open(Path(d).parent / ("launch-%s.json" % Path(d).name)))
            self.assertTrue(rec["base_commit"])
            self.assertTrue(rec["base_tree"])
            self.assertTrue(rec["clean_at_launch"])
            self.assertEqual(rec["attempt"], "att1")

    def test_a_dirty_repo_is_recorded_and_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as t:
            repo = _repo(os.path.join(t, "r"))
            with open(os.path.join(repo, "b.txt"), "w") as fh:
                fh.write("x\n")
            d = self._attempt(t)
            err, base = S._write_launch_record(d, {"id": "u1", "repo": repo})
            self.assertIn("preflight refused", err or "")
            self.assertIn('"b.txt"', err or "")
            self.assertIsNone(base)
            rec = json.load(open(Path(d).parent / ("launch-%s.json" % Path(d).name)))
            self.assertFalse(rec["clean_at_launch"])
            self.assertEqual(rec["dirty_paths_at_launch"], 1)
            self.assertEqual(rec["preflight"]["status"], "refused")

    def test_no_declared_repo_is_a_declaration_not_a_silence(self):
        with tempfile.TemporaryDirectory() as t:
            d = self._attempt(t)
            err, base = S._write_launch_record(d, {"id": "u1"})
            self.assertIsNone(err)
            self.assertIsNone(base)
            rec = json.load(open(Path(d).parent / ("launch-%s.json" % Path(d).name)))
            self.assertIsNone(rec["repo"])
            self.assertIn("no git transition", rec["note"])

    def test_a_non_repository_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            plain = os.path.join(t, "plain")
            os.makedirs(plain)
            d = self._attempt(t)
            err, _base = S._write_launch_record(d, {"id": "u1", "repo": plain})
            self.assertIn("not a git repository", err or "")

    def test_a_missing_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            d = self._attempt(t)
            err, _base = S._write_launch_record(
                d, {"id": "u1", "repo": os.path.join(t, "nope")})
            self.assertIn("not a directory", err or "")

    def test_an_empty_repository_gives_nothing_to_transition_from(self):
        with tempfile.TemporaryDirectory() as t:
            repo = _repo(os.path.join(t, "r"), commit=False)
            d = self._attempt(t)
            err, _base = S._write_launch_record(d, {"id": "u1", "repo": repo})
            self.assertIn("nothing to transition FROM", err or "")


class TestTheAnchorCannotBeMovedAfterwards(unittest.TestCase):

    def test_it_is_written_once_and_never_rewritten(self):
        """A worker that can rewrite its own baseline can manufacture a
        transition."""
        with tempfile.TemporaryDirectory() as t:
            repo = _repo(os.path.join(t, "r"))
            d = os.path.join(t, "runs", "u1", "att1")
            os.makedirs(d)
            S._write_launch_record(d, {"id": "u1", "repo": repo})
            first = json.load(open(Path(d).parent / ("launch-%s.json" % Path(d).name)))

            env = dict(os.environ, GIT_AUTHOR_NAME="t",
                       GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
                       GIT_COMMITTER_EMAIL="t@x")
            with open(os.path.join(repo, "c.txt"), "w") as fh:
                fh.write("later\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True,
                           env=env)
            subprocess.run(["git", "-C", repo, "commit", "-qm", "c2"],
                           check=True, env=env)

            S._write_launch_record(d, {"id": "u1", "repo": repo})
            again = json.load(open(Path(d).parent / ("launch-%s.json" % Path(d).name)))
            self.assertEqual(first["base_commit"], again["base_commit"],
                             "the anchor moved after the repository advanced")

    def test_it_lives_beside_the_attempt_not_inside_it(self):
        """Inside the agent's own write root, the worker could edit it."""
        with tempfile.TemporaryDirectory() as t:
            repo = _repo(os.path.join(t, "r"))
            d = os.path.join(t, "runs", "u1", "att1")
            os.makedirs(d)
            S._write_launch_record(d, {"id": "u1", "repo": repo})
            self.assertFalse(os.path.exists(os.path.join(d, "launch-%s.json" % Path(d).name)))
            self.assertTrue(os.path.exists(
                os.path.join(t, "runs", "u1", "launch-att1.json")))


class TestDispatchAnchorsFirst(unittest.TestCase):

    def test_the_anchor_is_written_before_paseo_runs(self):
        import ast
        fn = next(n for n in ast.walk(ast.parse(SWARM.read_text()))
                  if isinstance(n, ast.FunctionDef) and n.name == "_submit")
        pos = {}
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call):
                name = (getattr(sub.func, "attr", None)
                        or getattr(sub.func, "id", None))
                if name == "_write_launch_record":
                    pos.setdefault(name, sub.lineno)
                # The code launch is U.run(argv); scheduler calls pass a list.
                if (name == "run" and sub.args
                        and isinstance(sub.args[0], ast.Name)
                        and sub.args[0].id == "argv"):
                    pos.setdefault("run", sub.lineno)
        self.assertIn("_write_launch_record", pos,
                      "the code branch never anchors")
        self.assertIn("run", pos, "no dispatch call found to compare against")
        self.assertLess(pos["_write_launch_record"], pos["run"],
                        "the agent is dispatched before its baseline is "
                        "captured, so the baseline is not a baseline")


if __name__ == "__main__":
    unittest.main()
