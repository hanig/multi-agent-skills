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
             "outputs": ["out.txt"], "repo": "/repo", "mode": "bypass"}
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

    def test_a_code_unit_with_no_repo_is_refused(self):
        """It used to be SKIPPED, which made "no repo" a way to bypass the
        branch rule and land in a state nothing can leave: a code unit closes
        on a merged PR, and with no repository there is nowhere to open one."""
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


class TestACodePromptIsAPrompt(unittest.TestCase):
    """The prompt is the LAST POSITIONAL argument to the agent runner, so a
    flag written into it is not configuration: it is a sentence the agent is
    asked to read. Carefully-added paseo flags became instruction text."""

    def _plan(self, text, **over):
        u = {"id": "c1", "kind": "code", "repo": "/tmp/fixture-repo", "branch": "fx", "mode": "bypass", "outputs": ["o"], "prompt": text}
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
                {"id": "c1", "kind": "code", "repo": "/tmp/fixture-repo", "branch": "fx", "mode": "bypass", "outputs": ["o"],
                 "command": "--provider claude go"}]})

    def test_a_slurm_unit_may_pass_flags_in_its_command(self):
        """Only code units hand their string to an agent."""
        S.validate_plan(plan(command="python go.py --mode strict"))


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
        self.assertIn("DEFAULT_AGENT_THINKING", body)
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
        self.assertIn("u.get('thinking', DEFAULT_AGENT_THINKING)",
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


class TestSerialisedUnitsMaySharaABranch(unittest.TestCase):
    """The rule refused what the skill's own text recommends. A validator that
    refuses its own advice is worse than none: it teaches people the advice is
    unreliable."""

    def _c(self, uid, **over):
        u = {"id": uid, "kind": "code", "prompt": "work", "mode": "bypass",
             "outputs": ["o"], "repo": "/tmp/repo", "branch": "shared"}
        u.update(over)
        return u

    def test_units_ordered_with_needs_may_share_a_branch(self):
        S.validate_plan({"project": "p", "units": [
            self._c("u1"), self._c("u2", needs=["u1"])]})

    def test_a_transitive_order_also_counts(self):
        S.validate_plan({"project": "p", "units": [
            self._c("u1"),
            self._c("mid", needs=["u1"]),
            self._c("u2", needs=["mid"])]})

    def test_concurrent_units_on_one_branch_are_still_refused(self):
        with self.assertRaises(S.PlanError) as c:
            S.validate_plan({"project": "p", "units": [
                self._c("u1"), self._c("u2")]})
        msg = str(c.exception)
        self.assertIn("CONCURRENTLY", msg)
        self.assertIn("order them with 'needs'", msg,
                      "the refusal must offer the remedy the skill does")

    def test_a_partial_order_is_not_enough(self):
        """Three units, only two ordered: the third still runs alongside."""
        with self.assertRaises(S.PlanError):
            S.validate_plan({"project": "p", "units": [
                self._c("u1"), self._c("u2", needs=["u1"]), self._c("u3")]})


class TestARepoIsAPathNotAString(unittest.TestCase):

    def _c(self, uid, repo):
        return {"id": uid, "kind": "code", "prompt": "work", "outputs": ["o"],
                "repo": repo, "branch": "shared", "mode": "bypass"}

    def test_equivalent_spellings_are_one_repository(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan({"project": "p", "units": [
                self._c("u1", "/tmp/repo"),
                self._c("u2", "/tmp/repo/.")]})

    def test_a_trailing_slash_does_not_evade_it(self):
        with self.assertRaises(S.PlanError):
            S.validate_plan({"project": "p", "units": [
                self._c("u1", "/tmp/repo"),
                self._c("u2", "/tmp/repo/")]})

    def test_genuinely_different_repos_are_fine(self):
        S.validate_plan({"project": "p", "units": [
            self._c("u1", "/tmp/repo-a"),
            self._c("u2", "/tmp/repo-b")]})


class TestRoundTwoFindings(unittest.TestCase):

    def _code(self, **over):
        u = {"id": "c1", "kind": "code", "prompt": "work", "outputs": ["o"],
             "repo": "/tmp/r", "branch": "b", "mode": "bypass"}
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

if __name__ == "__main__":
    unittest.main()
