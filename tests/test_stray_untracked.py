#!/usr/bin/env python3
"""A receipt names untracked files that no declared output covers.

The failure this is written against, verbatim from the field: 18 bytes of test
debris in a file literally named `phase0b/--reflink=auto`, left by a stubbed
`cp` that wrote into its source directory instead of its destination. Nothing
surfaced it and the unit still read DONE. `repo_status` had already collected
the path at launch preflight; nothing carried it as far as the receipt.

So the bar for these tests is not "the helper works". It is: a unit that
reaches DONE with that exact file in its execution workspace must come back
with the path written down. Python 3.8+, stdlib only.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
UNIT = SCRIPTS / "unit.py"
sys.path.insert(0, str(SCRIPTS))
import worktree as W  # noqa: E402
import swarm as S  # noqa: E402
import unit as U  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

DEBRIS = "phase0b/--reflink=auto"


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo)] + list(a), check=True,
                          env=ENV, capture_output=True, text=True)


class Base(unittest.TestCase):
    """One git repository as the execution workspace, one external run root.

    Separate roots on purpose rather than for tidiness: a run root is
    required to sit outside every operated worktree, which is why declared
    outputs and untracked paths are compared as NAMES and not as resolved
    paths.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       env=ENV)
        (self.repo / "a.txt").write_text("one\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c1")

    def facts(self, **over):
        rec = {"schema_version": 1, "execution_workspace": str(self.repo),
               "repo": str(self.repo), "clean_at_launch": True}
        rec.update(over)
        return rec

    def leave(self, rel, text="18 bytes of nothing"):
        """Write an untracked file into the workspace, as the run would."""
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def stray(self, outputs, **over):
        got = W.stray_untracked(U.run, {"declared_outputs": list(outputs)},
                                self.facts(**over))
        return got

    # -- the end-to-end shape: allocate, bind, stub sacct, check -----------

    def sacct_stub(self):
        """A COMPLETED row for whatever job id it is asked about.

        Submit and End come from `date` inside the stub so the row is owned by
        the attempt that was allocated seconds ago, which is the ownership
        window `sacct_row_is_ours` enforces.
        """
        b = self.tmp / "bin"
        b.mkdir(exist_ok=True)
        (b / "sacct").write_text(
            "#!/bin/sh\nnow=$(date +%Y-%m-%dT%H:%M:%S)\n"
            'echo "COMPLETED|0:0|$now|$now"\n')
        (b / "sacct").chmod(0o755)
        return dict(os.environ, PATH=f"{b}{os.pathsep}{os.environ['PATH']}")

    def unit_check(self, outputs=("phase0b/out.tsv",), facts=None):
        """(state, receipt) from a real `unit.py check` on a DONE slurm unit."""
        argv = [sys.executable, str(UNIT), "allocate", "--root", "runs",
                "--task", "phase0b", "--kind", "slurm",
                "--repo", str(self.repo)]
        for o in outputs:
            argv += ["--output", o]
        r = subprocess.run(argv, capture_output=True, text=True,
                           cwd=str(self.tmp), timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        unit_dir = Path(r.stdout.strip().splitlines()[-1])
        # BEFORE the outputs are written, exactly where the coordinator takes
        # it. Taken after, it would be a digest of the run's own output and
        # the unit could never be judged to have produced anything.
        basis = S._capture_artifact_basis(
            {}, "phase0b", str(unit_dir), {"outputs": list(outputs)})
        for o in outputs:
            p = unit_dir / o
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("result\n")
        r = subprocess.run([sys.executable, str(UNIT), "bind", str(unit_dir),
                            "--job-id", "4242"], capture_output=True,
                           text=True, cwd=str(self.tmp), timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = [sys.executable, str(UNIT), "check", str(unit_dir), "--json",
                "--artifact-basis", json.dumps(basis)]
        if facts is not False:
            argv += ["--launch-facts", json.dumps(facts or self.facts())]
        r = subprocess.run(argv, capture_output=True, text=True,
                           cwd=str(self.tmp), env=self.sacct_stub(),
                           timeout=300)
        receipt = json.loads(r.stdout)
        return receipt["state"], receipt


class TestTheDebrisThatReachedDone(Base):
    """The reported failure, reproduced, then refused a second silence."""

    def test_a_done_unit_still_names_the_file_nothing_declared(self):
        self.leave(DEBRIS)
        state, receipt = self.unit_check()
        # DONE is CORRECT and must stay correct. The receipt is audit-only:
        # a stray file is a question, not a verdict, and letting this list
        # close or fail a unit would move authority into the receipt.
        self.assertEqual(state, "DONE", receipt["notes"])
        got = receipt["basis"]["stray_untracked"]
        self.assertEqual(got["paths"], [DEBRIS])
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["workspace"], str(self.repo))

    def test_the_declared_output_itself_is_not_reported_as_debris(self):
        """The control. `phase0b/out.tsv` is declared, and it is written into
        the run root, not the workspace -- so if the exclusion ever compared
        resolved paths instead of names this would still pass and the next
        test would fail."""
        state, receipt = self.unit_check()
        self.assertEqual(state, "DONE", receipt["notes"])
        self.assertEqual(receipt["basis"]["stray_untracked"]["paths"], [])


class TestWhatADeclaredOutputCovers(Base):

    def test_an_exactly_declared_name_is_not_stray(self):
        self.leave("results.tsv")
        self.assertEqual(self.stray(["results.tsv"])["paths"], [])

    def test_a_path_under_a_declared_directory_is_declared_not_stray(self):
        """A unit that declares the directory `results` declared what is in
        it. Exact matching alone would report every file of a directory
        output as debris, which is the noise that gets a section ignored."""
        self.leave("results/table.csv")
        self.leave("results/nested/deep.csv")
        self.assertEqual(self.stray(["results"])["paths"], [])

    def test_a_sibling_sharing_the_prefix_is_still_stray(self):
        """The trap in the rule above: `startswith(declared)` would swallow
        `results.bak`, which the unit never mentioned."""
        self.leave("results/table.csv")
        self.leave("results.bak/table.csv")
        self.assertEqual(self.stray(["results"])["paths"],
                         ["results.bak/table.csv"])

    def test_a_declaration_is_normalised_before_it_is_compared(self):
        """`./results/` and `results` are the same declaration. Comparing the
        spelling would make the exclusion depend on how a plan was typed."""
        self.leave("results/table.csv")
        self.assertEqual(self.stray(["./results/"])["paths"], [])

    def test_a_tracked_modification_is_not_in_this_list(self):
        """Scope. A tracked path that changed is already named: the code
        judgment refuses on it and launch preflight refuses before it. This
        list is for what git has never heard of."""
        (self.repo / "a.txt").write_text("edited\n")
        self.leave(DEBRIS)
        self.assertEqual(self.stray([])["paths"], [DEBRIS])

    def test_the_list_is_capped_and_says_it_was(self):
        """A thousand paths in a receipt is a directory listing. The count
        stays honest so nothing reads a truncated list as the whole answer."""
        for i in range(W.MAX_STRAY_PATHS + 5):
            self.leave("junk/f%03d" % i)
        got = self.stray([])
        self.assertEqual(len(got["paths"]), W.MAX_STRAY_PATHS)
        self.assertEqual(got["count"], W.MAX_STRAY_PATHS + 5)


class TestWeDidNotLookIsNotWeFoundNothing(Base):
    """Three claims, not two, for the same reason `basis` is a string and not
    a bool: an empty list would say the workspace was observed and clean."""

    def test_no_launch_facts_means_no_answer(self):
        self.leave(DEBRIS)
        self.assertIsNone(W.stray_untracked(U.run, {"declared_outputs": []},
                                            None))

    def test_no_execution_workspace_means_no_answer(self):
        self.assertIsNone(self.stray([], execution_workspace=None))

    def test_a_workspace_dirty_at_launch_means_no_answer(self):
        """Without a clean baseline an untracked path may predate the attempt,
        and calling it new would be a guess. Dispatch refuses that workspace
        anyway; this is the check refusing to guess if one ever gets through."""
        self.leave(DEBRIS)
        self.assertIsNone(self.stray([], clean_at_launch=False))

    def test_a_unit_without_launch_facts_still_gets_a_receipt(self):
        self.leave(DEBRIS)
        state, receipt = self.unit_check(facts=False)
        self.assertEqual(state, "DONE", receipt["notes"])
        self.assertIsNone(receipt["basis"]["stray_untracked"])


class TestTheJudgedFactsAreStillNotReobserved(Base):
    """`receipt_basis` was allowed one repository read. It must not have
    acquired a second one for the fields that were decided at judgment: a
    pinned head re-read from a moving ref is the defect that rule exists for.
    """

    def test_the_judged_head_and_verdict_come_from_the_spec(self):
        def refuse(*_a, **_k):
            self.fail("code_basis re-observed mutable repository state")

        spec = {"kind": "code", "produced_head": "a" * 40,
                "worktree_judged": "produced-committed-change"}
        basis = W.code_basis(refuse, "/attempt", spec, None)
        self.assertEqual(basis["produced_head"], "a" * 40)
        self.assertEqual(basis["worktree_judged"], "produced-committed-change")

    def test_receipt_basis_carries_both_halves(self):
        self.leave(DEBRIS)
        spec = {"kind": "code", "produced_head": "b" * 40,
                "worktree_judged": "produced-committed-change",
                "declared_outputs": []}
        basis = W.receipt_basis(U.run, "/attempt", spec, self.facts())
        self.assertEqual(basis["produced_head"], "b" * 40)
        self.assertEqual(basis["stray_untracked"]["paths"], [DEBRIS])


if __name__ == "__main__":
    unittest.main()
