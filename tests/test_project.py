"""The front door: survey and tickets.

Both scripts exist to enforce one rule each. survey.py enforces "never ask
what you can look up"; tickets.py enforces "every unit maps to an issue and
back". These test the rules, not the plumbing.
"""
import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-project" / "scripts"
SURVEY, TICKETS = SCRIPTS / "survey.py", SCRIPTS / "tickets.py"
sys.path.insert(0, str(SCRIPTS))
import tickets as T  # noqa: E402

PLAN = {"name": "p", "units": [
    {"id": "a", "kind": "slurm", "command": "true", "outputs": ["o.txt"],
     "description": "first"},
    {"id": "b", "kind": "slurm", "command": "true", "outputs": ["p.txt"],
     "needs": ["a"]}]}


def run(script, *argv, cwd=None):
    return subprocess.run([sys.executable, str(script), *argv],
                          capture_output=True, text=True, cwd=cwd)


class TestSurveyIsBounded(unittest.TestCase):
    """The first version was pointed at a cluster home directory and never
    returned: an unbounded `**` glob plus an unbounded walk. A survey that
    hangs is worse than one that reports a partial count."""

    def test_the_walk_is_bounded_three_ways(self):
        src = SURVEY.read_text()
        for guard in ("MAX_TREE_ENTRIES", "MAX_DEPTH", "WALK_SECONDS"):
            self.assertIn(guard, src, f"{guard} missing")

    def test_no_unbounded_recursive_glob(self):
        """`root.glob('**/...')` over a home directory is what hung it."""
        src = SURVEY.read_text()
        self.assertNotIn('glob("**/', src)
        self.assertNotIn("glob('**/", src)

    def test_a_truncated_count_says_so(self):
        """Reporting a capped count as if it were the whole tree would be a
        quiet lie in the one file the interview is meant to trust."""
        src = SURVEY.read_text()
        self.assertIn("counted_truncated_at", src)
        self.assertIn("(capped)", src)

    def test_it_runs_and_emits_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.py").write_text("print(1)\n")
            r = run(SURVEY, "--repo", d, "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(r.stdout)
            for key in ("machine", "scheduler", "repo", "storage"):
                self.assertIn(key, data)
            self.assertEqual(data["repo"]["file_count"], 1)

    def test_it_finds_a_tool_that_is_off_this_PATH(self):
        """`claude` lives in ~/.local/bin on the clusters, which a
        non-interactive ssh PATH omits, so `which` alone reported it missing on
        a host where it is installed and would have sent someone to install
        what they already had."""
        src = SURVEY.read_text()
        self.assertIn("not on this PATH", src)
        self.assertIn(".local/bin", src)

    def test_it_writes_nothing_it_was_not_asked_to(self):
        """It runs on someone's login node against someone's repo."""
        tree = ast.parse(SURVEY.read_text())
        writes = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in (
                    "write_text", "mkdir", "unlink", "rmtree", "write_bytes"):
                writes.append(n.attr)
        self.assertEqual(sorted(set(writes)), ["write_text"],
                         "survey may only write the file it is given")


class TestTicketsMapBothWays(unittest.TestCase):
    """Plan step 6(c): every issue maps to a unit predicate and every unit maps
    back to one issue, so neither drifts silently from the other."""

    def test_one_issue_per_unit(self):
        d = T.draft(PLAN)
        self.assertEqual([i["unit"] for i in d["issues"]], ["a", "b"])

    def test_a_unit_with_no_issue_is_drift(self):
        d = T.draft(PLAN)
        d["issues"].pop()
        problems = T.check(PLAN, d)
        self.assertTrue(any("no issue" in p for p in problems), problems)

    def test_an_issue_with_no_unit_is_drift(self):
        d = T.draft(PLAN)
        d["issues"].append({"unit": "ghost", "title": "x", "body": ""})
        problems = T.check(PLAN, d)
        self.assertTrue(any("no unit" in p for p in problems), problems)

    def test_a_unit_without_outputs_is_refused(self):
        """An issue whose done-condition is prose is an issue two people will
        disagree about. A unit with no declared outputs can never be closed by
        a predicate, so it must not get an issue at all."""
        plan = {"name": "p", "units": [
            {"id": "vague", "kind": "slurm", "command": "true", "outputs": []}]}
        problems = T.check(plan, T.draft(plan))
        self.assertTrue(any("no outputs" in p for p in problems), problems)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plan.json"
            p.write_text(json.dumps(plan))
            r = run(TICKETS, "draft", str(p))
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("REFUSING", r.stdout)

    def test_the_body_states_the_predicate_not_a_vibe(self):
        d = T.draft(PLAN)
        body = d["issues"][0]["body"]
        self.assertIn("Done means", body)
        self.assertIn("sacct", body)
        self.assertIn("not closed on a self-report", body)

    def test_each_kind_states_its_own_basis_honestly(self):
        """A pipeline unit has no scheduler behind it and a code unit has no
        exit status at all. An issue that implied otherwise would borrow
        authority the verdict does not have."""
        plan = {"name": "p", "units": [
            {"id": "pipe", "kind": "pipeline", "command": "nf run",
             "outputs": ["out/"]},
            {"id": "agent", "kind": "code", "prompt": "do it",
             "outputs": ["r.txt"]}]}
        bodies = {i["unit"]: i["body"] for i in T.draft(plan)["issues"]}
        self.assertIn("NOT attested by a scheduler", bodies["pipe"])
        self.assertIn("lifecycle state", bodies["agent"])
        self.assertIn("own report is not an input", bodies["agent"])

    def test_rerunning_carries_ids_forward_instead_of_duplicating(self):
        """6(g). Re-running against an existing project must update, not file
        a second copy of every issue."""
        first = T.draft(PLAN)
        first["project"]["linear_id"] = "proj-1"
        for i, issue in enumerate(first["issues"]):
            issue["linear_id"] = f"iss-{i}"
            issue["identifier"] = f"ARC-{100 + i}"
        second = T.draft(PLAN, existing=first)
        self.assertEqual(second["project"]["linear_id"], "proj-1")
        self.assertEqual([i["identifier"] for i in second["issues"]],
                         ["ARC-100", "ARC-101"])

    def test_a_new_unit_added_later_gets_no_stale_id(self):
        first = T.draft(PLAN)
        first["issues"][0]["linear_id"] = "iss-0"
        grown = {"name": "p", "units": PLAN["units"] + [
            {"id": "c", "kind": "slurm", "command": "true", "outputs": ["q"]}]}
        second = T.draft(grown, existing=first)
        by_unit = {i["unit"]: i for i in second["issues"]}
        self.assertEqual(by_unit["a"]["linear_id"], "iss-0")
        self.assertIsNone(by_unit["c"]["linear_id"])

    def test_it_talks_to_no_tracker(self):
        """It runs where the coordinator runs, on a shared login node, and a
        tracker token must not live there."""
        tree = ast.parse(TICKETS.read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        self.assertEqual(mods & {"urllib", "http", "requests", "socket",
                                 "httpx"}, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheSurveyLeaksNothing(unittest.TestCase):
    """The survey file is read into a session, often committed, sometimes
    pasted. The first version wrote a git remote verbatim, and a remote can
    carry a token in its userinfo."""

    def _survey_of(self, remote):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            sp.run(["git", "init", "-q", "."], cwd=d, capture_output=True)
            sp.run(["git", "remote", "add", "origin", remote], cwd=d,
                   capture_output=True)
            (Path(d) / "f.txt").write_text("x\n")
            sp.run(["git", "add", "-A"], cwd=d, capture_output=True)
            sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "i"], cwd=d, capture_output=True)
            r = run(SURVEY, "--repo", d, "--json")
            return json.loads(r.stdout)

    def test_a_token_in_a_remote_url_never_reaches_the_file(self):
        data = self._survey_of(
            "https://hani:ghp_AAAABBBBCCCCDDDDEEEE@github.com/h/private.git")
        blob = json.dumps(data)
        self.assertNotIn("ghp_AAAABBBBCCCCDDDDEEEE", blob)
        self.assertIn("<redacted>", data["repo"]["remote"])
        # The useful part must survive: redaction that destroys the host would
        # make the survey useless for the thing it exists for.
        self.assertIn("github.com/h/private.git", data["repo"]["remote"])

    def test_an_ordinary_remote_is_left_alone(self):
        data = self._survey_of("git@github.com:hanig/multi-agent-skills.git")
        self.assertEqual(data["repo"]["remote"],
                         "git@github.com:hanig/multi-agent-skills.git")

    def test_scrubbing_happens_at_the_boundary_not_per_field(self):
        """A field added next year must be covered without anyone remembering.
        Fail closed: scrub recurses over everything on the way out."""
        src = SURVEY.read_text()
        self.assertIn("data = scrub(data)", src)
        tree = ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "scrub")
        code = "\n".join(ast.unparse(x) for x in fn.body)
        self.assertIn("for k, v in obj.items()", code,
                      "scrub must recurse over dicts, not name known keys")

    def test_known_token_shapes_are_caught_anywhere_they_appear(self):
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        for secret in ("ghp_AAAABBBBCCCCDDDDEEEE",
                       "sk-AAAABBBBCCCCDDDDEEEEFFFF",
                       "xoxb-1234567890-abcdefghij",
                       "lin_api_AAAABBBBCCCCDDDDEEEE"):
            self.assertNotIn(secret, S2.redact(f"leaked {secret} here"),
                             f"{secret[:6]}... survived redaction")

    def test_it_collects_no_environment_variable_values(self):
        """Env values are where secrets actually live. The survey records USER
        and nothing else, and that has to stay true."""
        tree = ast.parse(SURVEY.read_text())
        got = set()
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get"
                    and isinstance(n.func.value, ast.Attribute)
                    and n.func.value.attr == "environ"):
                if n.args and isinstance(n.args[0], ast.Constant):
                    got.add(n.args[0].value)
        self.assertTrue(got <= {"USER"},
                        f"the survey reads environment values beyond USER: "
                        f"{sorted(got - {'USER'})}")
