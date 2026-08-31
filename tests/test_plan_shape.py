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


if __name__ == "__main__":
    unittest.main()
