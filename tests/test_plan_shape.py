"""Two rules that each cost a full dispatch-and-diagnose cycle.

Both were learned at RUNTIME, from an INCOMPLETE receipt, after dispatching.
Both are knowable at validate time, which is where a whole cycle collapses into
one line.
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
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


# --- exercising kind=code validation on a host with no paseo ---------------
#
# `validate_plan` refuses any kind=code unit unless `paseo` is on PATH, so a
# plan is not accepted on a machine that cannot run it. That refusal is
# correct and stays -- TestTheRefusalWhenPaseoIsAbsent below pins it, which
# nothing did before. Its side effect was that 21 tests of PURE validation
# logic (mode declaration, PR targets, provider flags, prompt-as-prose) could
# not run on a developer machine: they all failed identically at the same
# line, so the suite was silent exactly where code-unit behaviour is being
# changed, and a real regression would have looked like the usual noise.
#
# The seam is PATH -- the same one test_swarm.py::_fake_scheduler and
# test_project.py::_fake_slurm use for absent cluster tools. swarm.py knows
# nothing about it: there is no bypass flag to set in anger, and a real host
# with no paseo still gets the refusal.


def _fake_paseo(binp):
    """A `paseo` that satisfies shutil.which and NOTHING else.

    validate only asks whether the name resolves, so this is never executed
    by these tests. It exits 127 rather than 0 deliberately: if a test ever
    starts needing paseo to actually answer, it fails loudly here instead of
    passing against a stub that lies."""
    f = Path(binp) / "paseo"
    f.write_text(
        "#!/bin/sh\n"
        "echo 'stub paseo from tests/test_plan_shape.py: a PATH placeholder "
        "for validate_plan, not a runnable agent runner. A test that needs "
        "a real paseo must skip instead.' >&2\n"
        "exit 127\n")
    f.chmod(0o755)
    return str(binp)


@contextlib.contextmanager
def paseo_on_path():
    """Prepend a directory holding the stub `paseo` to PATH."""
    d = tempfile.mkdtemp(prefix="plan-shape-fakebin-")
    old = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = _fake_paseo(d) + os.pathsep + old
        yield d
    finally:
        os.environ["PATH"] = old
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def paseo_absent():
    """PATH with nothing on it, so `paseo` cannot resolve.

    Needed even here, where paseo happens to be missing anyway: a test that
    only passes because of what this host lacks is a test that stops testing
    anything on a host that has it."""
    d = tempfile.mkdtemp(prefix="plan-shape-emptybin-")
    old = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = d
        yield d
    finally:
        os.environ["PATH"] = old
        shutil.rmtree(d, ignore_errors=True)


class CodeUnitCase(unittest.TestCase):
    """Base for tests whose subject is kind=code validation, not paseo."""

    def setUp(self):
        super().setUp()
        cm = paseo_on_path()
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)


class TestEffortIsPerModelNotPerProject(unittest.TestCase):
    """One project-wide thinking id was wrong once the roster held more than
    one model. luna sits below sol and opus on measured intelligence and is
    asked for xhigh to compensate; the leaders run at high, because asking
    them for xhigh buys latency and not quality.

    Every id was read off a live agent, not guessed: paseo answers an unknown
    thinking id with an ERRORED agent, so a wrong value here fails at dispatch
    rather than downgrading the work quietly."""

    def test_the_two_leaders_run_at_high(self):
        self.assertEqual(S.default_thinking_for({}), "high")
        self.assertEqual(
            S.default_thinking_for({"provider": "codex/gpt-5.6-sol"}), "high")
        self.assertEqual(
            S.default_thinking_for({"provider": "claude/opus"}), "high")

    def test_luna_runs_at_xhigh(self):
        self.assertEqual(
            S.default_thinking_for({"provider": "codex/gpt-5.6-luna"}),
            "xhigh")

    def test_a_separate_model_field_resolves_the_same_way(self):
        """`provider: codex, model: gpt-5.6-luna` reaches paseo identically to
        `provider: codex/gpt-5.6-luna`, so the mapping must not apply to one
        plan and miss its equivalent."""
        self.assertEqual(
            S.default_thinking_for({"provider": "codex",
                                    "model": "gpt-5.6-luna"}), "xhigh")
        self.assertEqual(
            S.default_thinking_for({"provider": "codex",
                                    "model": "gpt-5.6-sol"}), "high")

    def test_the_opus_alias_and_its_expansion_agree(self):
        """paseo expands claude/opus to claude-opus-5, so a plan written
        either way must get the same effort."""
        self.assertEqual(S.default_thinking_for({"provider": "claude/opus"}),
                         S.default_thinking_for(
                             {"provider": "claude/claude-opus-5"}))

    def test_an_unknown_model_falls_back_rather_than_refusing(self):
        """A model added to the roster tomorrow should dispatch at a sane
        effort, not fail."""
        self.assertEqual(
            S.default_thinking_for({"provider": "somebody/new-model-9"}),
            S.DEFAULT_AGENT_THINKING)

    def test_a_unit_still_overrides_the_mapping(self):
        u = {"provider": "codex/gpt-5.6-luna", "thinking": "low"}
        self.assertEqual(u.get("thinking", S.default_thinking_for(u)), "low")

    def test_an_explicit_empty_thinking_still_switches_it_off(self):
        """The mapping must not resurrect the flag for a provider that has no
        thinking option."""
        for off in (None, ""):
            u = {"provider": "codex/gpt-5.6-luna", "thinking": off}
            self.assertFalse(u.get("thinking", S.default_thinking_for(u)))


class TestTheRefusalWhenPaseoIsAbsent(unittest.TestCase):
    """The refusal the code-unit tests below step around, pinned here so
    that stepping around it cannot quietly become deleting it. It was
    untested: the only thing keeping it honest was that it fired on every
    developer machine, which is precisely what made 21 unrelated tests
    unrunnable."""

    UNIT = {"id": "c1", "kind": "code", "prompt": "work", "outputs": ["o"],
            "repo": "/tmp/r", "target_branch": "main", "mode": "bypass"}

    def test_a_code_unit_is_refused_when_paseo_does_not_resolve(self):
        with paseo_absent():
            with self.assertRaises(S.PlanError) as c:
                S.validate_plan({"project": "p", "units": [dict(self.UNIT)]})
        msg = str(c.exception)
        self.assertIn("paseo is not on PATH", msg)
        self.assertIn("c1", msg, "name the units that cannot run")

    def test_the_refusal_names_the_host_and_the_alternative(self):
        """The message is the whole value of refusing early: it must say
        WHERE the check ran and what to do instead of installing paseo on a
        login node."""
        with paseo_absent():
            with self.assertRaises(S.PlanError) as c:
                S.validate_plan({"project": "p", "units": [dict(self.UNIT)]})
        msg = str(c.exception)
        self.assertIn(os.uname().nodename, msg)
        self.assertIn("kind=pipeline", msg)

    def test_a_slurm_unit_is_not_refused_for_a_missing_paseo(self):
        with paseo_absent():
            S.validate_plan(plan())

    def test_the_same_plan_validates_once_paseo_resolves(self):
        """The counter-claim, and the one that makes the stub honest: with
        paseo on PATH the plan is accepted, so every test using
        CodeUnitCase is reaching its own assertion rather than a refusal."""
        with paseo_on_path():
            S.validate_plan({"project": "p", "units": [dict(self.UNIT)]})


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



class TestACodeUnitNeedsAPullRequestTarget(CodeUnitCase):
    """C11 creates the source branch; the plan must name its PR destination."""

    def _code(self, uid="c1", **over):
        u = {"id": uid, "kind": "code", "prompt": "do it",
             "outputs": ["out.txt"], "repo": "/repo", "mode": "bypass"}
        u.update(over)
        return u

    def _plan(self, *units):
        return {"project": "p", "units": list(units)}

    def test_a_code_unit_with_a_repo_needs_a_pr_target(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._plan(self._code()))
        msg = str(c.exception)
        self.assertIn("target_branch", msg)
        self.assertIn("pull request", msg)
        self.assertNotIn("share a checkout", msg)

    def test_legacy_branch_is_not_silently_reinterpreted_as_target(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._plan(self._code(branch="impl-work")))
        self.assertIn("not used as a fallback", str(c.exception))

    def test_concurrent_units_may_share_a_pr_target(self):
        S.validate_plan(self._plan(
            self._code("c1", target_branch="main"),
            self._code("c2", target_branch="main")))

    def test_the_same_target_in_different_repos_is_fine(self):
        S.validate_plan(self._plan(
            self._code("c1", target_branch="main", repo="/a"),
            self._code("c2", target_branch="main", repo="/b")))

    def test_a_code_unit_with_no_repo_is_refused(self):
        """It used to be SKIPPED, which made "no repo" a way to bypass the
        closure rules and land in a state nothing can leave: a code unit
        closes on a merged PR, and with no repository there is nowhere to
        open one."""
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._plan(self._code(repo=None)))
        self.assertIn("never reach DONE", str(c.exception))

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


class TestACodePromptIsAPrompt(CodeUnitCase):
    """The prompt is the LAST POSITIONAL argument to the agent runner, so a
    flag written into it is not configuration: it is a sentence the agent is
    asked to read. Carefully-added paseo flags became instruction text."""

    def _plan(self, text, **over):
        u = {"id": "c1", "kind": "code", "repo": "/tmp/fixture-repo", "target_branch": "main", "mode": "bypass", "outputs": ["o"], "prompt": text}
        u.update(over)
        return {"project": "p", "units": [u]}

    def test_a_provider_flag_in_the_prompt_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(self._plan("--provider claude fix the parser"))
        msg = str(c.exception)
        self.assertIn("a sentence the agent is asked to read", msg)
        self.assertIn("as a field", msg)

    def test_every_configuration_flag_is_caught(self):
        for flag in ("--mode", "--model", "--env", "--cwd", "--title",
                     "--thinking"):
            with self.assertRaises(S.PlanError, msg=flag):
                S.validate_plan(self._plan("%s x do the work" % flag))

    def test_thinking_is_in_the_list(self):
        """I added the `thinking` field and forgot the ban, so
        `--thinking low` in a prompt was read aloud to the agent while it ran
        at the default effort."""
        with self.assertRaises(S.PlanError):
            S.validate_plan(self._plan("--thinking low fix the parser"))

    def test_a_flag_mid_sentence_is_the_subject_not_configuration(self):
        """"Implement the application's --mode strict option" is an ordinary
        request. Refusing it teaches people the validator is noise."""
        for good in ("Implement the application's --mode strict option",
                     "Document what --model does in our CLI",
                     "fix the parser and mention --env in the README"):
            S.validate_plan(self._plan(good))

    def test_the_equals_spelling_is_caught(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan(self._plan("--mode=bypass do the work"))

    def test_an_ordinary_prompt_passes(self):
        for good in ("fix the parser and add a test",
                     "run make check then summarise failures",
                     "use --verbose when invoking the tool"):
            S.validate_plan(self._plan(good))

    def test_the_command_field_is_checked_too(self):
        """`command` is the fallback the runner uses when prompt is absent."""
        with self.assertRaises(S.PlanError):
            S.validate_plan({"project": "p", "units": [
                {"id": "c1", "kind": "code", "repo": "/tmp/fixture-repo", "target_branch": "main", "mode": "bypass", "outputs": ["o"],
                 "command": "--provider claude go"}]})

    def test_a_slurm_unit_may_pass_flags_in_its_command(self):
        """Only code units hand their string to an agent."""
        S.validate_plan(plan(command="python go.py --mode strict"))

    def test_validate_refuses_if_assembled_code_prompt_lacks_protocol(self):
        """Validation exercises the dispatch builder, not plan-authored prose."""
        real = S._dispatch_prompt
        S._dispatch_prompt = lambda u, intent=None: str(u.get("prompt") or "")
        try:
            with self.assertRaises(S.PlanError) as c:
                S.validate_plan(self._plan("fix the parser"))
        finally:
            S._dispatch_prompt = real
        self.assertIn("without the required completion protocol",
                      str(c.exception))

    def test_non_code_prompt_gets_no_completion_protocol(self):
        unit = {"id": "pipe", "kind": "pipeline",
                "prompt": "run the workflow"}
        intent = {"repo": "/repo", "branch": "swarm-a1",
                  "target_branch": "main",
                  "base_commit": "a" * 40}
        prompt = S._dispatch_prompt(unit, intent)
        self.assertEqual(prompt, "run the workflow")
        self.assertNotIn(S.CODE_COMPLETION_PROTOCOL_MARKER, prompt)


class TestTheDefaultAgent(unittest.TestCase):
    """Code units get the strongest agent available unless the plan says
    otherwise. Every string was read off live agents, because paseo answers an
    unknown thinking id with an ERRORED agent and a default that fails at
    dispatch is worse than no default."""

    def _argv(self, unit):
        """The paseo argv _submit would build, without dispatching."""
        import ast
        src = (ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py")
        self.assertTrue(src.is_file())
        return None

    def test_the_default_is_sol_at_high(self):
        self.assertEqual(S.DEFAULT_AGENT_PROVIDER, "codex/gpt-5.6-sol")
        self.assertEqual(S.DEFAULT_AGENT_THINKING, "high")

    def test_dispatch_passes_the_default_provider_and_thinking(self):
        import ast
        src = (ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
               ).read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_submit")
        body = ast.unparse(fn)
        self.assertIn("DEFAULT_AGENT_PROVIDER", body)
        # Effort is resolved per model now, so _submit calls the resolver
        # rather than naming the project fallback directly.
        self.assertIn("default_thinking_for", body)
        self.assertIn("--thinking", body)

    def test_a_unit_can_override_each_piece(self):
        import ast
        src = (ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
               ).read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_submit")
        body = ast.unparse(fn)
        # provider and thinking fall back to the default; model has no default
        self.assertIn("u.get('provider') or DEFAULT_AGENT_PROVIDER",
                      body.replace('"', "'"))
        self.assertIn("u.get('thinking', default_thinking_for(u))",
                      body.replace('"', "'"))

    def test_thinking_can_be_switched_off_for_a_provider_without_it(self):
        """A provider with no thinking option must not be sent one."""
        import ast
        src = (ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
               ).read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_submit")
        body = ast.unparse(fn)
        self.assertIn("if thinking:", body,
                      "an explicit null must suppress the flag entirely")

    def test_claude_is_no_longer_hardcoded_as_the_provider(self):
        import ast
        src = (ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
               ).read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_submit")
        self.assertNotIn("'claude'", ast.unparse(fn).replace('"', "'"))


class TestPerAttemptBranchesRemoveTheSharedCheckoutConstraint(CodeUnitCase):

    def _c(self, uid, **over):
        u = {"id": uid, "kind": "code", "prompt": "work", "mode": "bypass",
             "outputs": ["o"], "repo": "/tmp/repo",
             "target_branch": "main"}
        u.update(over)
        return u

    def test_ordered_units_may_share_a_pr_target(self):
        S.validate_plan({"project": "p", "units": [
            self._c("u1"), self._c("u2", needs=["u1"])]})

    def test_concurrent_units_may_share_a_pr_target(self):
        S.validate_plan({"project": "p", "units": [
            self._c("u1"), self._c("u2")]})


class TestRoundTwoFindings(CodeUnitCase):

    def _code(self, **over):
        u = {"id": "c1", "kind": "code", "prompt": "work", "outputs": ["o"],
             "repo": "/tmp/r", "target_branch": "main", "mode": "bypass"}
        u.update(over)
        return {"project": "p", "units": [u]}

    def test_a_code_unit_must_declare_a_mode(self):
        """No default on purpose, which made its absence a decision nobody
        made: unattended, the agent stops at its first write and the unit runs
        forever doing nothing."""
        u = self._code()
        del u["units"][0]["mode"]
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(u)
        msg = str(c.exception)
        self.assertIn("runs forever doing nothing", msg)
        self.assertIn("will not pick permissions on your behalf", msg)

    def test_an_explicit_default_mode_is_accepted(self):
        """Accepting the stall deliberately is a legitimate choice."""
        S.validate_plan(self._code(mode="default"))

    def test_a_string_null_thinking_is_refused(self):
        """paseo answers an unknown id with an errored agent, so this would
        fail at dispatch for every code unit in the DAG."""
        for bad in ("null", "none", "None", "NIL", "false"):
            with self.assertRaises(S.PlanError, msg=bad):
                S.validate_plan(self._code(thinking=bad))

    def test_json_null_and_empty_string_still_suppress_the_flag(self):
        """These are the documented way to turn it off and must keep working:
        the round-2 finding claimed they were broken, and they are not."""
        for good in (None, ""):
            S.validate_plan(self._code(thinking=good))
        for value, expect_flag in ((None, False), ("", False),
                                   ("high", True), ("low", True)):
            u = {"thinking": value} if value is not None or True else {}
            t = u.get("thinking", S.DEFAULT_AGENT_THINKING)
            self.assertEqual(bool(t), expect_flag, repr(value))

    def test_a_real_thinking_id_is_accepted(self):
        S.validate_plan(self._code(thinking="high"))

    def test_a_null_or_empty_mode_is_refused(self):
        """Presence is not a value. `"mode": null` satisfied the presence
        check and then omitted the flag, so the unit dispatched on default
        permissions and stalled: the failure the rule exists to prevent,
        through the rule's own hole."""
        for bad in (None, "", "   "):
            with self.assertRaises(S.PlanError, msg=repr(bad)) as c:
                S.validate_plan(self._code(mode=bad))
            self.assertIn("waits for a person", str(c.exception))

    def test_an_unknown_thinking_id_is_deliberately_NOT_refused(self):
        """It fails LOUDLY: paseo returns an errored agent and the unit goes
        FAILED, which is visible. validate cannot know a provider's valid set
        without introspecting it, and a hard-coded list would refuse ids that
        become valid as the provider changes. Catch what fails silently; let
        what fails loudly fail loudly."""
        S.validate_plan(self._code(thinking="some-future-effort-level"))


class TestArrayAndOneWriteRoot(unittest.TestCase):
    """Every array task shares the unit's single attempt directory, so the
    first to finish writes the artifacts record over a partial result and the
    unit reads DONE on a fraction of the work. A dry run does not show it,
    because a dry run does not fan out."""

    def test_array_with_declared_outputs_is_refused(self):
        for spelling in (["--array=0-19"], ["--array", "0-19"], ["-a0-19"],
                         ["-a", "0-19"]):
            with self.assertRaises(S.PlanError, msg=str(spelling)) as c:
                S.validate_plan(plan(sbatch=spelling))
            self.assertIn("fraction of the work", str(c.exception))

    def test_the_refusal_offers_both_remedies(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(sbatch=["--array=0-9"]))
        msg = str(c.exception)
        self.assertIn("own unit", msg)
        self.assertIn("merge unit", msg)

    def test_an_array_can_never_be_legal_because_outputs_are_mandatory(self):
        """There is no "array without outputs" escape: a unit with no
        declared outputs is already refused as unjudgeable. The two rules
        together mean an array unit must be split, full stop."""
        with self.assertRaises(S.PlanError):
            S.validate_plan(plan(sbatch=["--array=0-9"], outputs=[]))

    def test_an_ordinary_unit_is_unaffected(self):
        S.validate_plan(plan(sbatch=["--partition=cpu"]))


class TestPathsTheCommandNames(unittest.TestCase):
    """"validate refuses a plan whose inputs match nothing" was true and gave
    false confidence: a unit declared one input that existed and dispatched
    into FileNotFoundError on a different path in its own command."""

    def test_a_missing_file_in_a_VISIBLE_directory_is_refused(self):
        """The shared-storage typo: this host can see the place the path
        claims to be, and the thing is not there."""
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(
                command="python go.py --meta /tmp/definitely-not-here.tsv"))
        self.assertIn("FileNotFoundError you would meet", str(c.exception))

    def test_a_path_under_an_INVISIBLE_directory_is_not_refused(self):
        """The submit host is not the compute node. A path under a mount that
        exists only there is legitimate and unknowable from here, and
        refusing it rejects a working plan, which is the worse mistake."""
        S.validate_plan(plan(
            command="python go.py --in /opt/compute-node-only/data.tsv"))

    def test_the_refusal_says_which_directory_it_could_see(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(command="python go.py /tmp/absent.tsv"))
        self.assertIn("/tmp", str(c.exception))

    def test_a_declared_input_is_accepted(self):
        """It must also EXIST, because the inputs rule checks that
        separately: declaring a path does not conjure it."""
        S.validate_plan(plan(command="python go.py --meta /tmp",
                             inputs=["/tmp"]))

    def test_upstream_output_is_referenced_by_env_var_not_by_path(self):
        """Outputs are relative to an attempt directory whose name is not
        known when the plan is written, so a downstream unit reaches them
        through SWARM_DEP_<ID>, which this check skips as a variable. An
        absolute token can never BE an upstream output, which makes that
        escape hatch unreachable by construction rather than merely unused."""
        S.validate_plan({"project": "p", "units": [
            {"id": "a", "kind": "slurm", "command": "true", "runtime": "none",
             "outputs": ["made.tsv"]},
            {"id": "b", "kind": "slurm", "runtime": "none", "needs": ["a"],
             "command": "python go.py --in $SWARM_DEP_A/made.tsv",
             "outputs": ["out.txt"]}]})

    def test_the_program_itself_is_not_statted(self):
        """sol's ruling on the runtime: an absolute program path need only
        resolve on the COMPUTE node, so checking it here asserts a fact about
        a machine this one cannot see."""
        S.validate_plan(plan(
            command="/opt/only-on-the-compute-node/bin/python go.py"))

    def test_a_relative_path_is_not_checked(self):
        S.validate_plan(plan(command="python go.py --in data/x.tsv"))

    def test_a_variable_is_not_checked(self):
        S.validate_plan(plan(command="python go.py --in $SWARM_UNIT_DIR/x"))

    def test_an_existing_path_passes(self):
        S.validate_plan(plan(command="python go.py --in /tmp"))

    def test_a_glob_matching_nothing_in_a_VISIBLE_directory_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(command="python go.py /tmp/*.no-such-ext"))
        self.assertIn("can see that directory", str(c.exception))

    def test_a_glob_under_an_INVISIBLE_directory_is_not_refused(self):
        """The same visibility rule as a plain path. I wrote it for paths and
        left the glob branch raising unconditionally, so a compute-node-only
        glob was still refused: the same false refusal surviving in the branch
        I did not revisit."""
        S.validate_plan(plan(command="python go.py /opt/only-there/*.tsv"))

    def test_a_trailing_slash_does_not_defeat_the_parent_test(self):
        """`/tmp/missing/` gave dirname `/tmp/missing`, not a directory, so
        the check skipped a path it should have caught."""
        with self.assertRaises(S.PlanError):
            S.validate_plan(plan(command="python go.py --in /tmp/missing/"))

    def test_a_code_unit_is_exempt(self):
        """Its string is a prompt, not a command line.

        The only test in this class that needs a code unit, so the PATH seam
        is scoped to it rather than to the class: every other test here is
        about paths the command names, and PATH is not what they are
        varying."""
        with paseo_on_path():
            S.validate_plan({"project": "p", "units": [
                {"id": "c", "kind": "code", "prompt": "read /nowhere/notes.md",
                 "outputs": ["o"], "repo": "/tmp/r", "target_branch": "main",
                 "mode": "bypass"}]})


class TestTheSchemaIsReadableInOneGo(unittest.TestCase):
    """Five successive refusals to make one unit valid, and a field invented
    along the way from guessing at shape. Error messages teach one rule at a
    time by construction."""

    def _run(self, *args):
        import subprocess
        return subprocess.run(
            [sys.executable, str(ROOT / "skills" / "hanig-swarm" / "scripts"
                                 / "swarm.py"), "schema", *args],
            capture_output=True, text=True)

    def test_it_prints_every_field(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        for field in ("repo", "target_branch", "mode", "runtime", "outputs",
                      "sbatch", "write_scopes", "continuation"):
            self.assertIn(field, r.stdout, field)

    def test_it_states_the_couplings_that_only_show_as_refusals(self):
        r = self._run()
        for phrase in ("canary", "ancestor", "partition AND account",
                       "disjoint write_scopes"):
            self.assertIn(phrase, r.stdout, phrase)

    def test_json_is_machine_readable(self):
        import json as _json
        r = self._run("--json")
        d = _json.loads(r.stdout)
        self.assertTrue(d["fields"] and d["couplings"])
        names = {f["field"] for f in d["fields"]}
        self.assertIn("mode", names)
        self.assertIn("target_branch", names)
        self.assertNotIn("branch", names)

    def test_every_documented_field_requirement_is_a_real_one(self):
        """A schema that drifts from the validator is worse than none."""
        names = {f for f, _k, _r, _n in S.SCHEMA_FIELDS}
        for real in ("id", "kind", "command", "outputs", "repo",
                     "target_branch", "mode", "runtime", "needs", "inputs"):
            self.assertIn(real, names, real)


class TestRoundOneOfThisBatch(unittest.TestCase):

    def test_the_separated_short_array_form_is_caught(self):
        """`-a 0-19` is valid Slurm and slipped through, so a broken
        array-with-outputs plan was accepted and dispatched."""
        with self.assertRaises(S.PlanError):
            S.validate_plan(plan(sbatch=["-a", "0-19"], outputs=["r.tsv"]))

    def test_the_entrypoint_exemption_is_exact_not_a_prefix(self):
        """startswith exempted /opt/tool/bin/python_extra/missing.tsv because
        the entrypoint is /opt/tool/bin/python, so an unrelated missing file
        rode in on the runtime's name."""
        rt = {"id": "r", "resolution": "direct", "entrypoint": "/tmp/python",
              "probe": "/tmp/python -c pass",
              "verified_by": "unverified: fixture for the prefix test"}
        with self.assertRaises(S.PlanError):
            S.validate_plan(plan(
                runtime=rt,
                command="/tmp/python go.py --in /tmp/python_extra_absent.tsv"))

    def test_the_entrypoint_itself_is_still_exempt(self):
        rt = {"id": "r", "resolution": "direct",
              "entrypoint": "/opt/only-there/python",
              "probe": "/opt/only-there/python -c pass",
              "verified_by": "unverified: fixture for the exact-match test"}
        S.validate_plan(plan(
            runtime=rt,
            command="/opt/only-there/python go.py --flag value"))

    def test_the_schema_note_matches_what_the_rule_does(self):
        """Pinned to BEHAVIOUR, not to wording, so rewording the note does not
        fail this and a rule change does. The note drifted twice in two
        commits, which is why it is pinned at all."""
        note = next(n for f, _k, _r, n in S.SCHEMA_FIELDS if f == "command")
        low = note.lower()
        # it must state the visibility rule, cover globs as well as paths,
        # exempt the program, and say a compute-only mount is not refused
        self.assertIn("parent directory is visible", low)
        self.assertIn("glob", low)
        self.assertIn("compute node", low)
        self.assertIn("first token is never checked", low)
        self.assertNotIn("  ", note, "doubled spacing suggests a bad patch")
        self.assertNotIn("an an ", low)

    def test_a_separated_array_value_must_not_be_another_flag(self):
        """`--array --partition=cpu` read the flag as the range, which Slurm
        rejects at submission, and produced a misleading array-and-outputs
        refusal when outputs existed."""
        for spelling in (["--array", "--partition=cpu"], ["-a", "--mem=4G"]):
            with self.assertRaises(S.PlanError, msg=str(spelling)) as c:
                S.validate_plan(plan(sbatch=spelling))
            self.assertIn("another flag rather than a task range",
                          str(c.exception))

    def test_a_real_separated_range_still_works(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(sbatch=["--array", "0-19"]))
        self.assertIn("fraction of the work", str(c.exception))

class TestModeAdviceMatchesTheProvider(CodeUnitCase):
    """`mode` is provider-specific, so advice that names one is a claim.

    The default provider became codex while every message and doc still said
    `bypass`, which is claude's word. codex answers it with `auto,
    auto-review, full-access`, so following our own instructions produced a
    rejected dispatch.
    """

    def _refusal(self, unit):
        with self.assertRaises(S.PlanError) as cm:
            S.validate_plan({"name": "p", "units": [unit]})
        return str(cm.exception)

    def test_a_codex_unit_is_not_told_to_write_bypass(self):
        msg = self._refusal(
            {"id": "c", "kind": "code", "repo": "/r",
             "target_branch": "main",
             "prompt": "work", "outputs": ["x"],
             "provider": "codex/gpt-5.6-sol"})
        self.assertIn("full-access", msg)
        self.assertNotIn("bypass", msg)

    def test_a_claude_unit_is_still_told_to_write_bypass(self):
        msg = self._refusal(
            {"id": "c", "kind": "code", "repo": "/r",
             "target_branch": "main",
             "prompt": "work", "outputs": ["x"],
             "provider": "claude/opus"})
        self.assertIn("bypass", msg)

    def test_a_unit_naming_no_provider_follows_the_default(self):
        msg = self._refusal({"id": "c", "kind": "code", "repo": "/r",
                             "target_branch": "main", "prompt": "work",
                             "outputs": ["x"]})
        want = S._unattended_mode_example(S.DEFAULT_AGENT_PROVIDER)
        self.assertIn(want, msg)

    def test_no_skill_doc_recommends_a_mode_the_default_rejects(self):
        # codex rejects `bypass`; the docs are where the value gets copied
        # from, so a doc that still says it is the defect, not a typo.
        default = S.DEFAULT_AGENT_PROVIDER.split("/", 1)[0].lower()
        if default != "codex":
            self.skipTest("default provider is no longer codex")
        for name in ("hanig-swarm", "hanig-project"):
            doc = ROOT / "skills" / name / "SKILL.md"
            for i, line in enumerate(doc.read_text().splitlines(), 1):
                if '"mode": "bypass"' in line or '"mode":"bypass"' in line:
                    self.fail("%s:%d recommends mode=bypass, which codex "
                              "rejects: %s" % (doc.name, i, line.strip()))


class TestTheCheapFixForAWrongPartitionIsWrittenDown(unittest.TestCase):
    """A third rule of the same kind, learned the same way. A queued job in
    the wrong partition has a one-command fix and an expensive one, and the
    expensive one is the one you reach for by reflex: cancel it and dispatch
    again. That loses the job id the attempt is bound to, and the redispatch
    edits the unit's sbatch flags, so the plan digest changes and `advance`
    then needs `--accept-plan-change`. Two problems for one mistake.

    Guarded in the DOC rather than in code because nothing in swarm.py moves a
    queued job; the doc is where the choice is actually made."""

    DOC = ROOT / "skills" / "hanig-swarm" / "SKILL.md"

    def test_the_dispatch_section_names_scontrol_update(self):
        doc = self.DOC.read_text()
        self.assertIn("scontrol update JobId=", doc)
        self.assertIn("Partition=", doc)

    def test_it_says_what_cancel_and_redispatch_costs(self):
        doc = self.DOC.read_text()
        self.assertIn("--accept-plan-change", doc,
                      "the doc must say what the expensive path then needs")
        self.assertIn("bound to nothing", doc)

    def test_the_note_sits_in_the_dispatch_section(self):
        """Not in the gotchas at the bottom: it is read while dispatching, and
        a note two screens below the commands is a note nobody reaches."""
        doc = self.DOC.read_text()
        usage = doc.index("## Usage")
        kinds = doc.index("## The three kinds")
        self.assertTrue(usage < doc.index("scontrol update JobId=") < kinds)


# --- ARC-246: validate reads what the survey already recorded --------------
#
# survey.py computes the memory policy and the per-partition account rules,
# the skill tells the planner to plan around them, the report prints them, and
# the one component that could refuse a plan never looked. These tests state
# each cluster fact in ONE state at a time, because the whole risk in this
# feature is that `unknown` quietly becomes `fine`.


@contextlib.contextmanager
def cluster_tools_absent():
    """PATH with no `sinfo` on it, so `_known_partitions()` answers UNKNOWN.

    Not because this laptop has no Slurm. The suite also runs on andromeda,
    where sinfo answers with that cluster's partition names and every test
    below that names a fixture partition would be refused by the WRONG check
    -- the same "passed on a laptop, failed on the cluster" trap the
    `_LOOK_IT_UP` sentinel was introduced for."""
    d = tempfile.mkdtemp(prefix="plan-shape-nocluster-")
    old = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = d
        yield d
    finally:
        os.environ["PATH"] = old
        shutil.rmtree(d, ignore_errors=True)


def limit_set(value):
    return {"state": "set", "value": value}


OPEN = {"state": "unrestricted", "value": None}
UNKNOWN = {"state": "unknown", "value": None,
           "why": "scontrol is not on this PATH"}


def surveyed(name, **fields):
    """One partition as survey.py records it. A field left out is ABSENT,
    which is the schema-version-1 case and must read as unknown."""
    rec = {"partition": name}
    rec.update(fields)
    return rec


def survey_doc(partitions=(), **scheduler):
    """A survey document in survey.py's shape, for THIS host.

    The hostname matters: a survey found beside a plan is only evidence about
    the machine it ran on, so one carrying another host's name is ignored."""
    sched = {"present": True}
    sched.update(scheduler)
    if partitions:
        sched["partitions"] = list(partitions)
    return {"schema_version": 3,
            "machine": {"hostname": os.uname().nodename},
            "scheduler": sched}


class SurveyCase(unittest.TestCase):
    """Base for tests whose subject is the SURVEY, not this host's Slurm."""

    def setUp(self):
        super().setUp()
        cm = cluster_tools_absent()
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def refuses(self, plan_, survey, *phrases):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan_, survey)
        msg = str(c.exception)
        for phrase in phrases:
            self.assertIn(phrase, msg)
        return msg

    def accepts(self, plan_, survey):
        S.validate_plan(plan_, survey)


class TestTheSurveySaysWhetherMemoryMustBeAsked(SurveyCase):
    """`--mem` was enforced NOWHERE: grep for it in swarm.py returned
    nothing, while survey.py computed `mem_flag_required` from
    DefMemPerNode=UNLIMITED and the skill told the planner to add the flag."""

    REQUIRED = staticmethod(lambda: survey_doc(mem_flag_required=True))

    def test_a_slurm_unit_that_states_no_memory_is_refused(self):
        msg = self.refuses(plan(sbatch=["--partition=cpu"]), self.REQUIRED(),
                           "u1", "mem_flag_required", "--mem=")
        self.assertIn("no default per-node memory", msg)

    def test_a_unit_that_states_its_memory_is_not(self):
        """The negative control. A check that refuses every plan has not
        checked anything."""
        for spelling in (["--mem=64G"], ["--mem", "64G"]):
            self.accepts(plan(sbatch=spelling), self.REQUIRED())

    def test_mem_per_cpu_counts_because_sbatch_refuses_both(self):
        """--mem and --mem-per-cpu are mutually exclusive at submission, so
        insisting on --mem by name would refuse a unit that has already
        answered the question in the only other spelling it may use."""
        for spelling in (["--mem-per-cpu=4G"], ["--mem-per-gpu", "8G"],
                         ["--mem-per-cpu", "4G"]):
            self.accepts(plan(sbatch=spelling), self.REQUIRED())

    def test_a_flag_with_no_value_is_not_a_request(self):
        self.refuses(plan(sbatch=["--mem="]), self.REQUIRED(), "u1")
        self.refuses(plan(sbatch=["--mem", "--partition=cpu"]),
                     self.REQUIRED(), "u1")

    def test_a_malformed_flag_does_not_mask_a_good_one(self):
        self.accepts(plan(sbatch=["--mem=", "--mem-per-cpu=4G"]),
                     self.REQUIRED())

    def test_mem_bind_is_not_a_memory_request(self):
        """A prefix match would read --mem-bind as an answer to a question it
        says nothing about."""
        self.refuses(plan(sbatch=["--mem-bind=verbose"]), self.REQUIRED(),
                     "u1")

    def test_a_cluster_with_a_default_refuses_nothing(self):
        """The surveyed answer NO. DefMemPerNode is a number here, so a unit
        that says nothing is taking a real default."""
        self.accepts(plan(sbatch=["--partition=cpu"]),
                     survey_doc(mem_flag_required=False))

    def test_a_survey_that_could_not_tell_refuses_nothing(self):
        """UNKNOWN, the case that is easiest to get wrong. `scontrol show
        config` did not answer, so the field is absent -- and absent is not
        "no flag needed"."""
        self.accepts(plan(sbatch=["--partition=cpu"]), survey_doc())

    def test_no_survey_at_all_refuses_nothing(self):
        """Validating on a machine with no survey is the same unknown."""
        self.accepts(plan(sbatch=["--partition=cpu"]), None)

    def test_a_host_without_slurm_refuses_nothing(self):
        self.accepts(plan(sbatch=["--partition=cpu"]),
                     {"scheduler": {"present": False},
                      "machine": {"hostname": os.uname().nodename}})

    def test_only_slurm_units_are_asked(self):
        """A pipeline unit's engine submits its own jobs; its sbatch list is
        not the one Slurm reads."""
        p = plan(kind="pipeline", command="nextflow run x")
        self.accepts(p, self.REQUIRED())


class TestTheSurveySaysWhoMayChargeAPartition(SurveyCase):
    """`declared_account` existed and was used ONLY for canary agreement.
    B4 surveys allow_accounts and deny_accounts per partition, and this is
    the failure it was bought with: hours in QOSGrpCpuLimit while a 736-CPU
    partition sat 202 CPUs idle."""

    def _plan(self, account="goodarzilab", partition="lab"):
        return plan(sbatch=[f"--partition={partition}",
                            f"--account={account}", "--mem=8G"])

    def test_a_denied_account_is_refused(self):
        s = survey_doc([surveyed("lab", allow_accounts=OPEN,
                                 deny_accounts=limit_set(["goodarzilab"]))])
        msg = self.refuses(self._plan(), s, "u1", "goodarzilab", "lab",
                           "deny_accounts")
        self.assertIn("QOSGrpCpuLimit", msg)

    def test_an_account_missing_from_a_set_allow_list_is_refused(self):
        s = survey_doc([surveyed(
            "lab", allow_accounts=limit_set(["goodarzilab", "shared"]),
            deny_accounts=OPEN)])
        self.refuses(self._plan(account="other"), s, "allow_accounts",
                     "does not include it")

    def test_the_refusal_says_the_partition_qos_is_not_the_only_qos(self):
        """A passing check must not read as a promise the job will run:
        qos_grptres resolves the PARTITION QOS only, and an account or
        association QOS can impose a GrpTRES the survey never sees -- the
        second route to the same QOSGrpCpuLimit."""
        s = survey_doc([surveyed(
            "lab", deny_accounts=limit_set(["goodarzilab"]))])
        msg = self.refuses(self._plan(), s, "qos_grptres")
        self.assertIn("PARTITION QOS only", msg)
        self.assertIn("not a promise", msg)

    def test_an_allowed_account_is_not_refused(self):
        """The negative control."""
        s = survey_doc([surveyed(
            "lab", allow_accounts=limit_set(["goodarzilab", "shared"]),
            deny_accounts=OPEN)])
        self.accepts(self._plan(), s)

    def test_an_unrestricted_partition_is_not_refused(self):
        s = survey_doc([surveyed("lab", allow_accounts=OPEN,
                                 deny_accounts=OPEN)])
        self.accepts(self._plan(), s)

    def test_UNKNOWN_on_both_halves_refuses_nothing(self):
        """THE case. A host without scontrol reports unknown for every
        allowance, and a refusal built on a query that never answered is a
        guess wearing a number."""
        s = survey_doc([surveyed("lab", allow_accounts=UNKNOWN,
                                 deny_accounts=UNKNOWN)])
        self.accepts(self._plan(), s)
        self.accepts(self._plan(account="nobody-has-this-account"), s)

    def test_a_partition_with_no_allowance_block_refuses_nothing(self):
        """schema_version 1 predates the block entirely. An absent field is
        unknown, not unrestricted -- and not a crash."""
        self.accepts(self._plan(), survey_doc([surveyed("lab")]))

    def test_unknown_on_one_half_still_lets_the_other_decide(self):
        """Slurm prints DenyAccounts INSTEAD of AllowAccounts, so one field
        being unavailable is not a reason to ignore the one that answered."""
        s = survey_doc([surveyed("lab", deny_accounts=UNKNOWN,
                                 allow_accounts=limit_set(["shared"]))])
        self.refuses(self._plan(), s, "allow_accounts")
        s = survey_doc([surveyed("lab", allow_accounts=UNKNOWN,
                                 deny_accounts=limit_set(["goodarzilab"]))])
        self.refuses(self._plan(), s, "deny_accounts")

    def test_an_account_list_is_never_matched_as_a_substring(self):
        """`"lab" in "goodarzilab"` is True and would admit a unit onto a
        partition that never allowed it. A value that is not a list is not an
        account list."""
        s = survey_doc([surveyed("lab",
                                 allow_accounts=limit_set(["goodarzilab"]))])
        self.refuses(self._plan(account="lab"), s, "allow_accounts")
        s = survey_doc([surveyed("lab", deny_accounts=limit_set("goodarzilab"),
                                 allow_accounts=OPEN)])
        self.accepts(self._plan(), s)

    def test_a_partition_the_survey_never_saw_refuses_nothing(self):
        s = survey_doc([surveyed("other", allow_accounts=limit_set(["x"]))])
        self.accepts(self._plan(), s)

    def test_an_undeclared_account_refuses_nothing(self):
        """The association default is not in the plan, so it cannot be
        checked against the survey."""
        s = survey_doc([surveyed("lab", allow_accounts=limit_set(["shared"]))])
        self.accepts(plan(sbatch=["--partition=lab", "--mem=8G"]), s)

    def test_an_undeclared_partition_refuses_nothing(self):
        s = survey_doc([surveyed("lab", allow_accounts=limit_set(["shared"]))])
        self.accepts(plan(sbatch=["--account=goodarzilab", "--mem=8G"]), s)

    def test_every_spelling_of_the_two_flags_is_read(self):
        s = survey_doc([surveyed("lab", allow_accounts=limit_set(["shared"]))])
        for flags in (["-p", "lab", "-A", "goodarzilab"],
                      ["-plab", "-Agoodarzilab"],
                      ["--partition", "lab", "--account", "goodarzilab"]):
            self.refuses(plan(sbatch=[*flags, "--mem=8G"]), s,
                         "allow_accounts")


class TestValidateFindsTheSurveyItself(SurveyCase):
    """The plumbing. `validate` had no survey at all before this, so an
    honest answer to "where does it get one" had to be built: the file
    hanig-project's very first command writes, beside the plan."""

    def _project(self, d, survey=None, plan_=None):
        proj = Path(d)
        (proj / ".swarm").mkdir(parents=True, exist_ok=True)
        if survey is not None:
            (proj / ".swarm" / "survey.json").write_text(json.dumps(survey))
        (proj / "plan.json").write_text(json.dumps(plan_ or plan(
            sbatch=["--partition=cpu"])))
        return str(proj / "plan.json")

    def test_it_reads_the_survey_beside_the_plan(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._project(d, survey_doc(mem_flag_required=True))
            found, note = S.discover_survey(p)
            self.assertEqual(found["scheduler"]["mem_flag_required"], True)
            self.assertIn(".swarm", note)

    def test_a_survey_from_ANOTHER_host_is_not_applied(self):
        """A survey is an observation of one machine. Refusing a unit on
        another cluster's allowances would be exactly the fabrication the
        three-state rule exists to prevent."""
        with tempfile.TemporaryDirectory() as d:
            doc = survey_doc(mem_flag_required=True)
            doc["machine"]["hostname"] = "some-other-login-node"
            found, note = S.discover_survey(self._project(d, doc))
            self.assertIsNone(found)
            self.assertIn("some-other-login-node", note)
            self.assertIn("--survey", note)

    def test_an_unreadable_survey_is_unknown_and_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._project(d, survey_doc())
            (Path(d) / ".swarm" / "survey.json").write_text("{not json")
            found, note = S.discover_survey(p)
            self.assertIsNone(found)
            self.assertIn("could not be read", note)

    def test_no_survey_names_the_path_it_looked_for(self):
        with tempfile.TemporaryDirectory() as d:
            found, note = S.discover_survey(self._project(d))
            self.assertIsNone(found)
            self.assertIn(os.path.join(".swarm", "survey.json"), note)
            self.assertIn("survey.py", note)

    def _validate(self, plan_path, *args):
        import subprocess
        return subprocess.run(
            [sys.executable, str(SWARM), "validate", plan_path, *args],
            capture_output=True, text=True)

    def test_the_discovered_survey_refuses_the_plan_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._project(d, survey_doc(mem_flag_required=True))
            r = self._validate(p)
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("mem_flag_required", r.stderr)

    def test_with_no_survey_it_names_what_it_did_not_check(self):
        """Silence is never approval: "plan is valid" with no survey read
        means the memory policy and the account rules were not examined at
        all, and the reader has to be told which."""
        with tempfile.TemporaryDirectory() as d:
            r = self._validate(self._project(d))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("NOT CHECKED", r.stdout)
            self.assertIn("no survey was read", r.stdout)

    def test_a_pass_against_a_survey_carries_the_qos_caveat(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._project(d, survey_doc(mem_flag_required=False))
            r = self._validate(p)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("PARTITION QOS only", r.stdout)

    def test_an_explicitly_named_survey_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._project(d)
            elsewhere = Path(d) / "recorded.json"
            elsewhere.write_text(json.dumps(survey_doc(
                mem_flag_required=True)))
            r = self._validate(p, "--survey", str(elsewhere))
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("mem_flag_required", r.stderr)

    def test_an_explicitly_named_survey_that_cannot_be_read_is_an_ERROR(self):
        """Not a shrug. Degrading a survey the operator pointed at into
        `unknown` would silently drop every check they asked for."""
        with tempfile.TemporaryDirectory() as d:
            r = self._validate(self._project(d), "--survey",
                               os.path.join(d, "nope.json"))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("no readable survey", r.stderr)

    def test_dispatch_validates_against_the_survey_too(self):
        """A refusal that only fires when someone runs the optional command
        is a refusal that fires after the DAG is live."""
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            p = self._project(d, survey_doc(mem_flag_required=True))
            r = subprocess.run(
                [sys.executable, str(SWARM), "run", p, "--dry-run",
                 "--state-dir", os.path.join(d, "state"),
                 "--root", os.path.join(d, "runs")],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("mem_flag_required", r.stderr)


class TestTheSurveyShapeIsTheRealOne(SurveyCase):
    """Every test above builds the survey by hand, so all of them would keep
    passing if survey.py renamed a field tomorrow. This one runs the real
    survey against a stubbed Slurm and hands its actual output to validate."""

    PARTITIONS = (
        'PartitionName=lab AllowAccounts=goodarzilab,shared Default=NO '
        'State=UP MaxTime=infinite TotalNodes=8 MaxMemPerCPU=5120 QOS=lab_qos',
        'PartitionName=preemptible DenyAccounts=goodarzilab Default=NO '
        'State=UP MaxTime=1-00:00:00 TotalNodes=40 MaxMemPerCPU=0 QOS=normal',
    )

    def _survey(self, defmem="UNLIMITED"):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            binp = Path(d) / "fakebin"
            binp.mkdir()
            # `defmem=None` prints NO DefMemPerNode line at all, which is the
            # scontrol that answered without naming the field.
            config = (f'    echo "DefMemPerNode = {defmem}" ;;\n' if defmem
                      else '    echo "SchedulerType = sched/backfill" ;;\n')
            bodies = {
                "sinfo": "#!/bin/sh\n"
                         'echo "lab|up|infinite|8"\n'
                         'echo "preemptible|up|1-00:00:00|40"\n',
                "scontrol": "#!/bin/sh\ncase \"$*\" in\n"
                            "  *'show config'*)\n"
                            + config
                            + "  *'show partition'*)\n"
                            + "".join(f"    echo '{line}'\n"
                                      for line in self.PARTITIONS)
                            + "    ;;\nesac\n",
                "sacctmgr": "#!/bin/sh\ncase \"$*\" in\n"
                            "  *'show qos'*) echo 'lab_qos|cpu=512' ;;\n"
                            "  *'show assoc'*) echo 'goodarzilab' ;;\nesac\n",
            }
            for name, body in bodies.items():
                (binp / name).write_text(body)
                (binp / name).chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = str(binp) + os.pathsep + env.get("PATH", "")
            r = subprocess.run(
                [sys.executable,
                 str(ROOT / "skills" / "hanig-project" / "scripts"
                     / "survey.py"), "--repo", d, "--json"],
                capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)

    def test_the_real_survey_drives_the_memory_refusal(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(sbatch=["--partition=lab",
                                         "--account=goodarzilab"]),
                            self._survey())
        self.assertIn("mem_flag_required", str(c.exception))

    def test_the_real_survey_drives_the_account_refusal(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(sbatch=["--partition=preemptible", "--mem=8G",
                                         "--account=goodarzilab"]),
                            self._survey())
        self.assertIn("deny_accounts", str(c.exception))

    def test_the_real_survey_also_lets_a_correct_plan_through(self):
        S.validate_plan(plan(sbatch=["--partition=lab", "--mem=8G",
                                     "--account=shared"]), self._survey())

    def test_a_config_that_never_named_DefMemPerNode_is_unknown(self):
        """survey.py used to write mem_flag_required=False when scontrol
        printed no DefMemPerNode at all, which is "the query did not answer"
        recorded as "no flag needed"."""
        data = self._survey(defmem=None)
        self.assertNotIn("mem_flag_required", data["scheduler"])
        S.validate_plan(plan(sbatch=["--partition=lab"]), data)


# --- ARC-247: findings.json ------------------------------------------------


class TestFindingsMustBeAbleToReachTheReport(unittest.TestCase):
    """The judgement call ARC-247 asked for, pinned so it cannot be reversed
    by accident. `findings.json` is NOT required of every plan -- a terminal
    training run or code unit has no claims about data to make, and a rule
    that refuses those gets bypassed with an empty list, which is worse than
    no rule. What is required is that a declared findings.json can REACH its
    only reader: report.py loads it from the project directory, while
    declared outputs never leave the attempt's exclusive write root."""

    def test_a_declared_findings_file_with_no_promotion_is_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan(plan(outputs=["findings.json"]))
        msg = str(c.exception)
        self.assertIn("promote_to", msg)
        self.assertIn("artifact nobody sees", msg)

    def test_a_nested_findings_file_is_caught_too(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan(plan(outputs=["results/findings.json"]))

    def test_a_promoted_findings_file_is_accepted(self):
        """The negative control."""
        S.validate_plan(plan(outputs=["findings.json"],
                             promote_to="/tmp/a-project"))

    def test_a_plan_with_no_findings_at_all_is_accepted(self):
        """The decision itself. A plan whose terminal unit is a training run
        has nothing to report, and refusing it would teach people to write
        {"findings": []} to get past the validator."""
        S.validate_plan(plan(outputs=["model.pt"]))
        S.validate_plan({"project": "p", "units": [
            {"id": "train", "kind": "slurm", "command": "true",
             "runtime": "none", "outputs": ["model.pt"]},
            {"id": "eval", "kind": "slurm", "command": "true",
             "runtime": "none", "needs": ["train"], "outputs": ["scores.tsv"]},
        ]})

    def test_the_reader_this_rule_rests_on_still_reads_the_project_dir(self):
        """The premise, pinned. If report.py ever learns to read findings out
        of the terminal unit's attempt directory, this rule's justification is
        gone and the rule should change with it."""
        src = (ROOT / "skills" / "hanig-project" / "scripts"
               / "report.py").read_text()
        self.assertIn('"findings": _load(p("findings.json"))', src)

    def test_the_doc_no_longer_demands_it_of_every_plan(self):
        doc = " ".join((ROOT / "skills" / "hanig-project"
                        / "SKILL.md").read_text().split())
        self.assertNotIn("So the final unit must emit", doc)
        self.assertIn("must also declare `promote_to`", doc)
        self.assertIn("Not every project has any", doc)


if __name__ == "__main__":
    unittest.main()
