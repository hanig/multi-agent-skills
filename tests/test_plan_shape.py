"""Two rules that each cost a full dispatch-and-diagnose cycle.

Both were learned at RUNTIME, from an INCOMPLETE receipt, after dispatching.
Both are knowable at validate time, which is where a whole cycle collapses into
one line.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
sys.path.insert(0, str(SWARM.parent))
import swarm as S  # noqa: E402


def plan(**over):
    u = {"id": "u1", "kind": "slurm", "command": "true", "runtime": "none",
         "outputs": ["out.txt"]}
    u.update(over)
    return {"project": "p", "units": [u]}


class TestOutputsMustLandInTheWriteRoot(unittest.TestCase):
    """The most load-bearing constraint in the model, and the one place it was
    never written down. The done predicate looks inside the attempt's
    exclusive write root and nowhere else, so an output declared elsewhere is
    unfindable by construction: the work can succeed completely and the unit
    can never close."""

    def test_an_absolute_output_path_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(outputs=["/large_storage/hani/table.parquet"]))
        msg = str(c.exception)
        self.assertIn("can never be found", msg)
        self.assertIn("promote_to", msg,
                      "the refusal must name the thing you actually wanted")

    def test_an_output_climbing_out_of_the_root_is_refused(self):
        for bad in ("../elsewhere/x.txt", "a/../../x.txt"):
            with self.assertRaises(S.PlanError, msg=bad):
                S.validate_plan(plan(outputs=[bad]))

    def test_an_empty_output_is_refused(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan(plan(outputs=["   "]))

    def test_relative_outputs_are_fine(self):
        for good in ("out.txt", "results/table.parquet", "./a/b.tsv"):
            S.validate_plan(plan(outputs=[good]))

    def test_the_refusal_explains_where_they_must_live(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(outputs=["/tmp/x"]))
        self.assertIn("exclusive write root", str(c.exception))


class TestASlurmCommandIsTheWork(unittest.TestCase):
    """`sbatch --wrap='...'` as a unit command: the coordinator wrapped it in
    its own sbatch, the outer job submitted an inner job it was not bound to
    and exited in 00:00:00, and Slurm reported COMPLETED with ExitCode 0:0.
    Only the missing declared output caught it, a dispatch cycle later. That
    is precisely the confusion this system exists to prevent."""

    def test_a_command_starting_with_sbatch_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(command="sbatch --wrap='python go.py'"))
        msg = str(c.exception)
        self.assertIn("nests one job inside another", msg)
        self.assertIn("0:0", msg, "name the symptom, or nobody connects it")

    def test_srun_and_salloc_too(self):
        for bad in ("srun python go.py", "salloc -N1 ./x",
                    "/usr/bin/sbatch job.sh"):
            with self.assertRaises(S.PlanError, msg=bad):
                S.validate_plan(plan(command=bad))

    def test_the_work_itself_is_accepted(self):
        for good in ("python go.py", "./run.sh", "bash -lc 'make all'",
                     "singularity exec i.sif python go.py"):
            S.validate_plan(plan(command=good))

    def test_a_command_merely_mentioning_sbatch_is_not_refused(self):
        """The check is on the program being run, not on the word appearing."""
        S.validate_plan(plan(command="python go.py --note 'ran under sbatch'"))

    def test_only_slurm_units_are_checked(self):
        S.validate_plan(plan(kind="pipeline", command="srun nextflow run x"))

    def test_the_refusal_says_where_scheduler_flags_belong(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(command="sbatch x.sh"))
        self.assertIn("'sbatch'", str(c.exception))



class TestACodeUnitNeedsItsOwnBranch(unittest.TestCase):
    """write_scopes names FILES and isolates the attempt directory. It does
    not isolate a repository: agents share a checkout, so several code units on
    one repo run against one working tree, one branch and one index. And a code
    unit closes on a merged PR, so with no branch it is structurally
    unclosable."""

    def _code(self, uid="c1", **over):
        u = {"id": uid, "kind": "code", "prompt": "do it",
             "outputs": ["out.txt"], "repo": "/repo"}
        u.update(over)
        return u

    def _plan(self, *units):
        return {"project": "p", "units": list(units)}

    def test_a_code_unit_with_a_repo_needs_a_branch(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._plan(self._code()))
        msg = str(c.exception)
        self.assertIn("never close", msg)
        self.assertIn("share a checkout", msg,
                      "the refusal must say why scopes do not cover this")

    def test_two_units_may_not_share_a_branch(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._plan(
                self._code("c1", branch="work"),
                self._code("c2", branch="work")))
        self.assertIn("interleave", str(c.exception))

    def test_distinct_branches_are_accepted(self):
        S.validate_plan(self._plan(self._code("c1", branch="c1-work"),
                                   self._code("c2", branch="c2-work")))

    def test_the_same_branch_in_different_repos_is_fine(self):
        S.validate_plan(self._plan(
            self._code("c1", branch="work", repo="/a"),
            self._code("c2", branch="work", repo="/b")))

    def test_a_code_unit_with_no_repo_is_unaffected(self):
        S.validate_plan(self._plan(self._code(repo=None)))

    def test_slurm_units_are_unaffected(self):
        S.validate_plan(plan())


class TestTheSurveyFlagsWhatMustNotBeOverwritten(unittest.TestCase):
    """The skill is told to write PLAN.md. On the adopt path one may already
    exist, and this repo's own is a 26 KB design document."""

    def test_an_existing_plan_md_is_reported_as_protected(self):
        import subprocess
        import tempfile
        import json as _json
        survey = (ROOT / "skills" / "hanig-project" / "scripts" / "survey.py")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "PLAN.md").write_text("someone's design document\n")
            r = subprocess.run([sys.executable, str(survey), "--repo", d,
                                "--json"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            repo = _json.loads(r.stdout).get("repo") or {}
            names = [x["path"] for x in (repo.get("protected_docs") or [])]
            self.assertIn("PLAN.md", names)
            self.assertIn("never replace a PLAN.md you did not create",
                          repo.get("protected_docs_note", ""))

    def test_an_empty_directory_reports_none(self):
        import subprocess
        import tempfile
        import json as _json
        survey = (ROOT / "skills" / "hanig-project" / "scripts" / "survey.py")
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, str(survey), "--repo", d,
                                "--json"], capture_output=True, text=True)
            repo = _json.loads(r.stdout).get("repo") or {}
            self.assertEqual(repo.get("protected_docs"), [])
            self.assertNotIn("protected_docs_note", repo)

if __name__ == "__main__":
    unittest.main()
