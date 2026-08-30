"""The runtime a unit will actually execute in.

Nothing validated this, and it is the likeliest reason a scientific unit dies
on first contact: in a real run the plan artifact could not answer "which
python runs this?" without interrogating the planner.

My first design refused a bare `python` and statted absolute interpreter
paths. Sol rejected both. There is no reliable static shell chokepoint, and a
submit-host stat asserts a fact about a machine this one cannot see. These
tests pin the replacement: DECLARE the runtime uniformly, and require
something that proves it where the job actually lands.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
sys.path.insert(0, str(SWARM.parent))
import swarm as S  # noqa: E402

PROBE = "/abs/python -c 'import h5py'"
RT = {"id": "py", "resolution": "direct", "entrypoint": "/abs/python",
      "probe": PROBE,
      "verified_by": "unverified: shared homogeneous partition, checked "
                     "by hand this morning"}


def plan(units, runtimes=None):
    p = {"project": "p", "units": units}
    if runtimes:
        p["runtimes"] = runtimes
    return p


def unit(uid="a", **kw):
    u = {"id": uid, "kind": "slurm", "command": "true", "outputs": ["o"]}
    u.update(kw)
    return u


class TestRuntimeMustBeDeclared(unittest.TestCase):

    def test_a_slurm_unit_without_a_runtime_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([unit()]))
        self.assertIn("declares no 'runtime'", str(c.exception))

    def test_a_pipeline_unit_also_needs_one(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan(plan([unit(kind="pipeline")]))

    def test_refusal_does_not_depend_on_spotting_python(self):
        """Sol's point: uniform rejection, never a parser guessing."""
        for cmd in ("srun python x.py", "apptainer exec i.sif R -f x.R",
                    "bash -lc 'module load x && ./run.sh'", "uv run x.py",
                    "./wrapper.sh"):
            with self.assertRaises(S.PlanError, msg=cmd):
                S.validate_plan(plan([unit(command=cmd)]))

    def test_a_declared_runtime_validates(self):
        S.validate_plan(plan([unit(runtime=RT)]))

    def test_a_profile_can_be_referenced_by_id(self):
        S.validate_plan(plan([unit(runtime="py")], runtimes={"py": RT}))

    def test_a_dangling_profile_reference_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([unit(runtime="nope")], runtimes={"py": RT}))
        self.assertIn("not defined", str(c.exception))


class TestRuntimeFieldsAreChecked(unittest.TestCase):

    def _bad(self, **over):
        rt = dict(RT)
        rt.update(over)
        return plan([unit(runtime=rt)])

    def test_resolution_must_be_known(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan(self._bad(resolution="magic"))

    def test_entrypoint_is_required(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan(self._bad(entrypoint="  "))

    def test_verified_by_is_required(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._bad(verified_by=""))
        self.assertIn("nothing checks is a hope", str(c.exception))

    def test_every_documented_resolution_is_accepted(self):
        for r in S.RESOLUTIONS:
            S.validate_plan(self._bad(resolution=r))


class TestCanaryMustActuallyGate(unittest.TestCase):
    """A probe that does not block the fan-out proves nothing in time."""

    def test_a_canary_must_exist(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([unit(runtime=dict(
                RT, verified_by="canary:ghost"))]))
        self.assertIn("not a unit in this plan", str(c.exception))

    def test_a_unit_cannot_be_its_own_canary(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([unit("a", runtime=dict(
                RT, verified_by="canary:a"))]))
        self.assertIn("must not be its own probe", str(c.exception))

    def test_a_canary_that_is_not_an_ancestor_is_refused(self):
        p = plan([unit("probe", runtime=RT, command=PROBE),
                  unit("work", runtime=dict(RT, verified_by="canary:probe"))])
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(p)
        self.assertIn("not an ancestor", str(c.exception))

    def test_a_direct_dependency_satisfies_it(self):
        S.validate_plan(plan([
            unit("probe", runtime=RT, command=PROBE),
            unit("work", needs=["probe"],
                 runtime=dict(RT, verified_by="canary:probe"))]))

    def test_a_transitive_dependency_satisfies_it(self):
        S.validate_plan(plan([
            unit("probe", runtime=RT, command=PROBE),
            unit("mid", needs=["probe"], runtime=RT),
            unit("work", needs=["mid"],
                 runtime=dict(RT, verified_by="canary:probe"))]))


class TestUnverifiedMustBeADecision(unittest.TestCase):

    def test_unverified_needs_a_real_reason(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([unit(runtime=dict(
                RT, verified_by="unverified:meh"))]))
        self.assertIn("somebody made on purpose", str(c.exception))

    def test_a_real_reason_is_accepted(self):
        S.validate_plan(plan([unit(runtime=RT)]))

    def test_preflight_is_accepted(self):
        S.validate_plan(plan([unit(runtime=dict(
            RT, verified_by="preflight"))]))


class TestNoInferredFacts(unittest.TestCase):
    """Sol: a submit-host stat is an observed submit-host fact, and concluding
    compute-node availability from it violates declared-facts."""

    def test_the_validator_never_stats_an_entrypoint(self):
        src = SWARM.read_text()
        i = src.index("def _validate_runtimes")
        j = src.index("def validate_plan")
        seg = src[i:j]
        for forbidden in ("os.path.exists", "os.stat", "shutil.which",
                          "os.access", "isfile"):
            self.assertNotIn(forbidden, seg,
                             "runtime validation must not observe the "
                             "submit host and call it proof")

    def test_a_nonexistent_entrypoint_still_validates(self):
        """Because the path is meant to resolve on the COMPUTE node."""
        S.validate_plan(plan([unit(runtime=dict(
            RT, entrypoint="/definitely/not/here/python"))]))



class TestACanaryMustExerciseWhatItVouchesFor(unittest.TestCase):
    """Ordering is not proof: a runtime:none probe on cpu once stood as
    evidence for a python runtime on gpu."""

    OTHER = {"id": "other", "resolution": "conda", "entrypoint": "/x/py",
             "verified_by": "unverified: this is only the probe's own runtime"}

    def test_a_canary_with_a_different_runtime_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([
                unit("probe", runtime=self.OTHER, command=PROBE),
                unit("work", needs=["probe"],
                     runtime=dict(RT, verified_by="canary:probe"))]))
        self.assertIn("does not run the runtime it vouches for",
                      str(c.exception))

    def test_a_runtime_none_canary_cannot_vouch_for_a_real_runtime(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan(plan([
                unit("probe", runtime="none", command=PROBE),
                unit("work", needs=["probe"],
                     runtime=dict(RT, verified_by="canary:probe"))]))

    def test_a_canary_on_another_partition_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([
                unit("probe", runtime=RT, command=PROBE,
                     sbatch=["--partition=cpu"]),
                unit("work", needs=["probe"], sbatch=["--partition=gpu"],
                     runtime=dict(RT, verified_by="canary:probe"))]))
        self.assertIn("has to land where the work lands", str(c.exception))

    def test_an_omitted_partition_does_not_match_a_declared_one(self):
        """Absence is a value: no partition means the cluster default, which
        is a specific queue, not a wildcard."""
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([
                unit("probe", runtime=RT, command=PROBE),                 # default queue
                unit("work", needs=["probe"], sbatch=["--partition=gpu"],
                     runtime=dict(RT, verified_by="canary:probe"))]))
        self.assertIn("has to land where the work lands", str(c.exception))

    def test_both_omitting_a_partition_is_fine(self):
        S.validate_plan(plan([
            unit("probe", runtime=RT, command=PROBE),
            unit("work", needs=["probe"],
                 runtime=dict(RT, verified_by="canary:probe"))]))

    def test_a_canary_on_another_account_is_refused(self):
        """Access to a runtime and its files can differ by account."""
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([
                unit("probe", runtime=RT, command=PROBE,
                     sbatch=["--account=A"]),
                unit("work", needs=["probe"], sbatch=["--account=B"],
                     runtime=dict(RT, verified_by="canary:probe"))]))
        self.assertIn("does not establish this one works", str(c.exception))

    def test_the_same_account_is_accepted(self):
        S.validate_plan(plan([
            unit("probe", runtime=RT, command=PROBE, sbatch=["--account=A"]),
            unit("work", needs=["probe"], sbatch=["--account=A"],
                 runtime=dict(RT, verified_by="canary:probe"))]))

    def test_the_same_runtime_and_partition_is_accepted(self):
        S.validate_plan(plan([
            unit("probe", runtime=RT, command=PROBE,
                     sbatch=["--partition=cpu"]),
            unit("work", needs=["probe"], sbatch=["--partition=cpu"],
                 runtime=dict(RT, verified_by="canary:probe"))]))

    def test_verified_by_may_differ_between_probe_and_work(self):
        """A canary cannot be verified by itself, so that field must not be
        part of what has to match."""
        S.validate_plan(plan([
            unit("probe", runtime=RT, command=PROBE),
            unit("work", needs=["probe"],
                 runtime=dict(RT, verified_by="canary:probe"))]))


class TestACanaryMustRunTheDeclaredProbe(unittest.TestCase):
    """`true` closes cleanly and establishes nothing. We cannot tell from
    arbitrary shell whether a command exercises a runtime, so the check is
    declared-to-declared."""

    def test_a_canary_running_something_else_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([
                unit("probe", runtime=RT, command="true"),
                unit("work", needs=["probe"],
                     runtime=dict(RT, verified_by="canary:probe"))]))
        self.assertIn("proves the runtime works exactly as much as `true`",
                      str(c.exception))

    def test_a_runtime_verified_by_canary_must_declare_a_probe(self):
        rt = {k: v for k, v in RT.items() if k != "probe"}
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan([
                unit("probe", runtime=rt, command="anything"),
                unit("work", needs=["probe"],
                     runtime=dict(rt, verified_by="canary:probe"))]))
        self.assertIn("declares no 'probe'", str(c.exception))

    def test_running_the_probe_is_accepted(self):
        S.validate_plan(plan([
            unit("probe", runtime=RT, command=PROBE),
            unit("work", needs=["probe"],
                 runtime=dict(RT, verified_by="canary:probe"))]))

if __name__ == "__main__":
    unittest.main()
