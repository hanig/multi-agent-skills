"""The front door: survey and tickets.

Both scripts exist to enforce one rule each. survey.py enforces "never ask
what you can look up"; tickets.py enforces "every unit maps to an issue and
back". These test the rules, not the plumbing.
"""
import ast
import json
import os
import re
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
    {"id": "a", "kind": "slurm", "runtime": "none", "command": "true", "outputs": ["o.txt"],
     "description": "first"},
    {"id": "b", "kind": "slurm", "runtime": "none", "command": "true", "outputs": ["p.txt"],
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
        self.assertEqual(sorted(set(writes)), ["mkdir", "write_text"],
                         "survey may only write the file it is given, and "
                         "create that file's parent")

    def test_the_only_directory_it_creates_is_its_output_s_parent(self):
        """`mkdir` is allowed now because the skill's very first command is
        `--out .swarm/survey.json` in a directory where .swarm does not exist
        yet, and it raised FileNotFoundError. Creating the parent of the file
        you were told to write is part of writing it. Creating anything ELSE
        is the scattering this guard exists to stop."""
        tree = ast.parse(SURVEY.read_text())
        targets = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "mkdir":
                targets.append(ast.unparse(n.func.value))
        self.assertEqual(targets, ["out.parent"],
                         "survey creates a directory that is not its "
                         "output's parent: %s" % targets)


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
            {"id": "vague", "kind": "slurm", "runtime": "none", "command": "true", "outputs": []}]}
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
            {"id": "pipe", "kind": "pipeline", "runtime": "none", "command": "nf run",
             "outputs": ["out/"]},
            {"id": "agent", "kind": "code", "repo": "/tmp/fixture-repo", "branch": "fx", "mode": "bypass", "prompt": "do it",
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
            {"id": "c", "kind": "slurm", "runtime": "none", "command": "true", "outputs": ["q"]}]}
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
        # Structure, not text: ast.unparse renders this as "for (k, v) in",
        # so matching the source literally was brittle while the property held.
        self.assertIn("obj.items()", code,
                      "scrub must walk every key, not name known ones")
        self.assertIn("scrub(v)", code, "scrub must recurse")
        self.assertIn("isinstance(obj, list)", code,
                      "a secret inside a list must be scrubbed too")

    def test_known_token_shapes_are_caught_anywhere_they_appear(self):
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        # Fixtures that look like REAL tokens: base62 with mixed case and
        # digits. The earlier all-one-case fixtures were not representative,
        # and passing them told me nothing about real keys.
        for secret in ("ghp_AbCd1234EfGh5678IjKlMnOp9012",
                       "sk-ant-api03-AbCd1234EfGh5678IjKl",
                       "xoxb-1234567890-AbCdEfGhIjKl",
                       "lin_api_AbCd1234EfGh5678IjKl"):
            self.assertNotIn(secret, S2.redact(f"leaked {secret} here"),
                             f"{secret[:8]}... survived redaction")

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


class TestSurveyIsSafeInSomeoneElsesDirectory(unittest.TestCase):
    """It runs against a repo its author does not own, on a shared login node.
    The failure to avoid is not exotic: it is git blocking forever on a
    credential prompt or a pager, which stalls the whole interview."""

    def test_children_never_run_through_a_shell(self):
        """A repo path containing shell metacharacters must be data."""
        tree = ast.parse(SURVEY.read_text())
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "run"):
                for kw in n.keywords:
                    if kw.arg == "shell":
                        self.fail("survey passes shell= to subprocess")

    def test_a_path_with_metacharacters_executes_nothing(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as base:
            # No slash in the component: a directory name cannot contain one,
            # so the payload writes into the cwd the child is given instead.
            d = Path(base) / "we ird;$(touch PWNED)`touch PWNED2`repo"
            d.mkdir()
            sp.run(["git", "init", "-q", "."], cwd=d, capture_output=True)
            r = run(SURVEY, "--repo", str(d), "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            for marker in ("PWNED", "PWNED2"):
                self.assertFalse(
                    (d / marker).exists() or (Path(base) / marker).exists(),
                    f"a path with metacharacters caused execution ({marker})")

    def test_git_can_neither_prompt_nor_page(self):
        """Either one blocks until the timeout and turns a survey into a stall."""
        src = SURVEY.read_text()
        self.assertIn("GIT_TERMINAL_PROMPT", src)
        self.assertIn("GIT_PAGER", src)
        self.assertIn("GIT_OPTIONAL_LOCKS", src,
                      "the survey must not take a lock in someone's repo")

    def test_children_get_no_stdin(self):
        """A child that reads stdin would block until the timeout."""
        self.assertIn("stdin=subprocess.DEVNULL", SURVEY.read_text())

    def test_child_output_is_bounded(self):
        """Enormous commit messages, or scontrol on a large cluster, must not
        inflate the survey without limit."""
        src = SURVEY.read_text()
        self.assertIn("MAX_OUTPUT", src)
        # Bounded at the READ, not sliced after the fact. capture_output=True
        # buffers the whole thing into this process first, so a 500MB commit
        # subject was already in memory before any truncation ran.
        self.assertIn("read(MAX_OUTPUT)", src)
        self.assertNotIn("capture_output=True", src,
                         "capture_output buffers without limit")

    def test_every_child_has_a_timeout(self):
        src = SURVEY.read_text()
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "run"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "subprocess"):
                self.assertTrue(
                    any(kw.arg == "timeout" for kw in n.keywords),
                    "a subprocess call without a timeout can hang the survey")

    def test_an_unreachable_remote_does_not_stall_it(self):
        import subprocess as sp
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            sp.run(["git", "init", "-q", "."], cwd=d, capture_output=True)
            sp.run(["git", "remote", "add", "origin",
                    "https://example.invalid/private.git"], cwd=d,
                   capture_output=True)
            t0 = _t.time()
            r = run(SURVEY, "--repo", d, "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(_t.time() - t0, 30,
                            "the survey stalled on an unreachable remote")


class TestNoTestGoesUncollectedAnywhere(unittest.TestCase):
    """A reviewer found the __main__ block in this file sitting ABOVE two
    later classes, so running it directly collected 15 tests, printed a green
    OK, and exited before the 13 security tests were even defined.

    I fixed that file and did not check the others. The SAME defect was in
    test_outbox.py, hiding nine classes including every lock test. So this
    guard covers the whole suite: a property about the test suite cannot be
    guarded one file at a time."""

    def test_no_test_class_is_defined_after_a_main_block(self):
        import re
        offenders = {}
        for f in sorted((ROOT / "tests").glob("test_*.py")):
            src = f.read_text()
            # Anchor to a TOP-LEVEL statement. Searching for the literal found
            # the copy inside this test's own source, so the guard matched its
            # own reflection. A test that can match itself tests the wrong file.
            m = list(re.finditer(r'^if __name__ == "__main__":', src, re.M))
            if not m:
                continue
            self.assertEqual(len(m), 1,
                             f"{f.name} has more than one top-level __main__")
            orphaned = re.findall(r"^class (\w+)", src[m[0].start():], re.M)
            if orphaned:
                offenders[f.name] = orphaned
        self.assertEqual(offenders, {},
                         f"these classes never run when their file is executed "
                         f"directly: {offenders}")

    def test_every_declared_class_is_actually_collected(self):
        """Compare what the loader COLLECTS against what each file DECLARES,
        across the whole suite.

        The obvious version re-executes each file, which makes this file run
        itself, which re-executes it: I wrote that and hung the suite. Asking
        the loader is the same question without the recursion."""
        import re
        missing = {}
        for f in sorted((ROOT / "tests").glob("test_*.py")):
            src = f.read_text()
            # Only classes that actually CONTAIN tests. A shared `Base`
            # subclassing TestCase with no test_ methods is legitimately not
            # collected, and flagging it would be the cries-wolf failure that
            # gets a guard deleted.
            declared = set()
            for node in ast.parse(src).body:
                if not isinstance(node, ast.ClassDef):
                    continue
                has_tests = any(isinstance(b, (ast.FunctionDef,
                                               ast.AsyncFunctionDef))
                                and b.name.startswith("test_")
                                for b in node.body)
                if has_tests:
                    declared.add(node.name)
            loaded = set()
            try:
                suite = unittest.defaultTestLoader.loadTestsFromName(
                    f"tests.{f.stem}")
            except Exception as e:      # noqa: BLE001 - report, do not hide
                missing[f.name] = f"could not load: {e}"
                continue

            def walk(s):
                for t in s:
                    if isinstance(t, unittest.TestSuite):
                        walk(t)
                    else:
                        loaded.add(type(t).__name__)
            walk(suite)
            gap = declared - loaded
            if gap:
                missing[f.name] = sorted(gap)
        self.assertEqual(missing, {},
                         f"declared but never collected: {missing}")




class TestSurveyAgainstAHostileRepo(unittest.TestCase):
    """It exists to study repos its author did not write, so a repo's own
    .git/config is untrusted input."""

    def test_repo_configured_hooks_do_not_execute(self):
        """core.fsmonitor and core.hooksPath run on `git status`. Surveying a
        repo therefore ran its author's code."""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            root, marker = Path(d) / "r", Path(d) / "HOOK_RAN"
            root.mkdir()
            sp.run(["git", "init", "-q", "."], cwd=root, capture_output=True)
            (root / "f.txt").write_text("x\n")
            sp.run(["git", "add", "-A"], cwd=root, capture_output=True)
            sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "i"], cwd=root, capture_output=True)
            hooks = root / "evil"
            hooks.mkdir()
            for hook in ("post-index-change", "pre-auto-gc", "post-checkout"):
                h = hooks / hook
                h.write_text(f"#!/bin/sh\ntouch {marker}\n")
                h.chmod(0o755)
            sp.run(["git", "config", "core.hooksPath", str(hooks)], cwd=root,
                   capture_output=True)
            sp.run(["git", "config", "core.fsmonitor", f"touch {marker}"],
                   cwd=root, capture_output=True)
            r = run(SURVEY, "--repo", str(root), "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(marker.exists(),
                             "the repo's own git config executed code")

    def test_the_safety_flags_are_actually_passed_to_git(self):
        src = SURVEY.read_text()
        for key in ("core.fsmonitor=", "core.hooksPath=/dev/null",
                    "core.sshCommand=false"):
            self.assertIn(key, src)

    def test_the_mitigation_is_not_described_as_a_sandbox(self):
        """git's surface is large and only a container could make that claim.
        Overstating it is how someone points this at a genuinely hostile repo."""
        src = SURVEY.read_text()
        self.assertIn("NOT a sandbox", src)

    def test_a_directory_heavy_tree_cannot_outrun_the_bounds(self):
        """Both guards used to sit inside the per-file loop, so a subtree of
        EMPTY directories -- what a failed fan-out leaves -- was walked without
        limit and then reported as a complete count."""
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "r"
            for i in range(3000):
                (root / f"run-{i}").mkdir(parents=True)
            t0 = _t.time()
            r = run(SURVEY, "--repo", str(root), "--json")
            elapsed = _t.time() - t0
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(elapsed, 60, "the walk outran its own deadline")
            data = json.loads(r.stdout)
            self.assertIn("MAX_DIRS", SURVEY.read_text())
            self.assertEqual(data["repo"]["file_count"], 0)

    def test_non_utf8_output_does_not_crash_the_survey(self):
        """A commit message in ISO-8859-1 raised UnicodeDecodeError and the
        survey exited with a traceback instead of a result."""
        self.assertIn('decode("utf-8", "replace")', SURVEY.read_text())

    def test_a_credential_in_a_query_string_is_redacted(self):
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        got = S2.redact("https://gl.example.com/o/r.git?private_token=7f3a9b2c1d4e")
        self.assertNotIn("7f3a9b2c1d4e", got)
        self.assertIn("gl.example.com/o/r.git", got)

    def test_a_credential_shaped_dict_KEY_is_redacted(self):
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        got = S2.scrub({".sk-AbCd1234EfGh5678IjKl": 3})
        self.assertNotIn("sk-AbCd1234EfGh5678IjKl", json.dumps(got))

    def test_the_redaction_claim_is_scoped_honestly(self):
        """A closed pattern list can never be complete, and saying otherwise
        would invite someone to treat the survey as sanitised."""
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        doc = S2.redact.__doc__ or ""
        self.assertIn("NOT a guarantee", doc)
        self.assertIn("sensitive, not as sanitised", doc)


class TestTicketsFailClosed(unittest.TestCase):
    def test_a_corrupt_existing_draft_refuses_rather_than_duplicating(self):
        """The blast radius is a live shared workspace: dropping the ids
        silently would file a second project and a duplicate of every issue."""
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d) / "plan.json"
            plan.write_text(json.dumps(PLAN))
            (Path(d) / "tickets.json").write_text("{ truncated")
            r = run(TICKETS, "draft", str(plan))
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("REFUSING", r.stdout)
            self.assertIn("duplicate", r.stdout.lower())

    def test_a_changed_unit_makes_its_existing_issue_stale(self):
        """Comparing ids alone said "no drift" while a unit's declared outputs
        had changed underneath the issue that describes them."""
        d = T.draft(PLAN)
        changed = json.loads(json.dumps(PLAN))
        changed["units"][0]["outputs"] = ["something-else.txt"]
        problems = T.check(changed, d)
        self.assertTrue(any("no longer the work" in p for p in problems),
                        problems)

    def test_an_unchanged_plan_reports_no_drift(self):
        """The counter-claim: a checker that always complains gets ignored."""
        self.assertEqual(T.check(PLAN, T.draft(PLAN)), [])


class TestRound2ProjectFindings(unittest.TestCase):
    """Round 2 of the pre-publication gate. Every test here is written to fail
    against the implementation it replaced, because a reviewer showed several
    of my earlier ones would not."""

    def _repo_with_a_filter(self, base):
        """A repo that runs a command whenever git touches file CONTENT."""
        import subprocess as sp
        hook = Path(base) / "hook"
        marker = Path(base) / "FILTER_RAN"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
        hook.chmod(0o755)
        r = Path(base) / "r"
        r.mkdir()
        sp.run(["git", "init", "-q", "."], cwd=r, capture_output=True)
        sp.run(["git", "config", "filter.evil.clean", str(hook)], cwd=r,
               capture_output=True)
        sp.run(["git", "config", "filter.evil.smudge", str(hook)], cwd=r,
               capture_output=True)
        (r / ".gitattributes").write_text("f.txt filter=evil\n")
        (r / "f.txt").write_text("aaaa\n")
        sp.run(["git", "add", "-A"], cwd=r, capture_output=True)
        return r, marker

    def test_a_repo_configured_filter_does_not_execute(self):
        """CRITICAL. `git status` and `git diff` read working-tree CONTENT and
        so run a filter driver the repo configures for itself. My first
        attempt at this test had broken quoting, never installed the filter,
        and reported a comfortable 'no' -- which would have let me dismiss a
        real finding. Hence the POSITIVE CONTROL below: if the filter cannot
        be made to fire at all, this test has proved nothing."""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as base:
            r, marker = self._repo_with_a_filter(base)
            self.assertTrue(marker.exists(),
                            "positive control failed: the filter never fired "
                            "even on `git add`, so this test cannot detect it")
            sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "i"], cwd=r, capture_output=True)
            (r / "f.txt").write_text("bbbb\n")
            marker.unlink()
            res = run(SURVEY, "--repo", str(r), "--json")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse(marker.exists(),
                             "surveying the repo executed its filter driver")

    def test_the_survey_does_not_run_git_status_or_diff(self):
        """Those two commands are the vector. Naming them keeps the fix from
        being undone by someone restoring a dirty-file count."""
        src = SURVEY.read_text()
        calls = re.findall(r'run\(\["git",\s*"([a-z-]+)"', src)
        self.assertNotIn("status", calls)
        self.assertNotIn("diff", calls)

    def test_a_child_that_outlives_its_timeout_is_killed(self):
        """The timeout was a fiction: the parent blocked in stdout.read()
        BEFORE reaching wait(timeout=...), so it never applied, and a child
        filling the stderr pipe deadlocked outright. Fails against a
        pipe-reading implementation."""
        import time as _t
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        t0 = _t.time()
        rc, out, err = S2.run(["sh", "-c", "sleep 30"], timeout=2)
        elapsed = _t.time() - t0
        self.assertLess(elapsed, 15, "the timeout did not apply")
        self.assertIn("timed out", err)

    def test_a_child_flooding_stderr_does_not_deadlock(self):
        """Reading stdout first while the child fills stderr past the pipe
        buffer hangs until the timeout. Fails against the pipe version."""
        import time as _t
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        t0 = _t.time()
        rc, out, err = S2.run(
            ["sh", "-c", "i=0; while [ $i -lt 4000 ]; do "
                         "echo xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 1>&2; "
                         "i=$((i+1)); done; echo done"], timeout=20)
        self.assertLess(_t.time() - t0, 15, "deadlocked on a full stderr pipe")
        self.assertIn("done", out)

    def test_one_enormous_directory_is_bounded_as_it_is_read(self):
        """os.walk builds the whole entry list for a directory before any
        guard sees it, so a root with a million children was materialised in
        full. The per-directory cap replaced that.

        Uses SUBDIRECTORIES, not files: the file cap is lower, so a directory
        full of files trips that instead and the test passes either way. My
        first version did exactly that and was vacuous, which the mutation
        check caught."""
        with tempfile.TemporaryDirectory() as d:
            r = Path(d) / "r"
            r.mkdir()
            sys.path.insert(0, str(SCRIPTS))
            import survey as S2
            for i in range(S2.MAX_ENTRIES_PER_DIR + 200):
                (r / f"run-{i}").mkdir()
            res = run(SURVEY, "--repo", str(r), "--json")
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)
            self.assertEqual(data["repo"]["file_count"], 0,
                             "no files exist, so the FILE cap must not be "
                             "what stopped the walk")
            self.assertTrue(
                data["repo"]["counted_truncated_at"],
                "a directory past the per-entry cap was reported as fully "
                "counted, so the partial walk reads as a total")

    def test_redaction_keeps_legitimate_names(self):
        """The cries-wolf direction, found by three reviewers: a file named
        hf_my_model_weights_1234567890123456 was replaced wholesale. Fails
        against the unconditional family match."""
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        # These must MATCH the token pattern to be a real test. Three of the
        # examples a reviewer gave did not match at all, so asserting on them
        # proved nothing -- caught by mutating the code and finding this test
        # still passed. `hf_bertbaseuncased2024run` does match, and was being
        # redacted.
        for legit in ("hf_bertbaseuncased2024run",
                      "hf_llamathreeeightbinstruct",
                      "sk-learnpipelineexample2024"):
            self.assertTrue(S2._TOKENISH.search(legit),
                            f"{legit} does not match the pattern, so this "
                            f"assertion would pass vacuously")
            self.assertEqual(S2.redact(legit), legit,
                             f"redaction destroyed legitimate data: {legit}")

    def test_redaction_still_catches_real_tokens(self):
        """The counter-claim to the test above: narrowing must not blind it."""
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        for secret in ("ghp_AbCd1234EfGh5678IjKl",
                       "glpat-Xy9Zw8Vu7Ts6Rq5Po4",
                       "hf_QwErTyUiOp1234567890",
                       "sk-ant-api03-AbCd1234EfGh5678"):
            self.assertEqual(S2.redact(secret), "<redacted>", secret)

    def test_two_keys_redacting_alike_do_not_collapse(self):
        """`a.sk-aaa....txt` and `a.sk-bbb....txt` both become `a.<redacted>`,
        and the second silently overwrote the first, losing a row."""
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        got = S2.scrub({"a.sk-AbCd1234EfGh5678.txt": 1,
                        "a.sk-Zz9Yy8Xx7Ww6Vv5Uu4.txt": 2})
        self.assertEqual(len(got), 2, f"a row was lost: {got}")
        self.assertNotIn("sk-AbCd1234EfGh5678", json.dumps(got))

    def test_an_issue_without_a_digest_fails_the_drift_check(self):
        """"Cannot tell" must not read as "no drift" in the check whose whole
        job is detecting what cannot otherwise be told."""
        d = T.draft(PLAN)
        d["issues"][0].pop("unit_digest")
        problems = T.check(PLAN, d)
        self.assertTrue(any("no unit digest" in p for p in problems), problems)

    def test_a_first_run_with_no_existing_draft_is_not_refused(self):
        """The fail-closed path must not fire when there is simply nothing
        there yet, which would block every first use."""
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d) / "plan.json"
            plan.write_text(json.dumps(PLAN))
            r = run(TICKETS, "draft", str(plan))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestTheRedactionGapIsRecorded(unittest.TestCase):
    """What this scrubber deliberately does NOT catch, written down so nobody
    later mistakes silence for safety."""

    def test_an_all_lowercase_sk_string_is_knowingly_left_alone(self):
        """`sk-` collides with `sk-learn`, and an all-lowercase run with no
        digits is indistinguishable from an ordinary descriptive name. A real
        OpenAI key is 48 base62 characters with mixed case and digits, so this
        trade-off costs nothing real and buys not mangling filenames. It is a
        KNOWN GAP, recorded rather than hidden."""
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        self.assertEqual(S2.redact("sk-aaaaaaaaaaaaaaaaaaaa"),
                         "sk-aaaaaaaaaaaaaaaaaaaa")
        # And the realistic shape IS caught, which is the point.
        self.assertEqual(S2.redact("sk-AbCd1234EfGh5678IjKlMnOp"), "<redacted>")

    def test_the_docstring_says_it_is_not_a_guarantee(self):
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        doc = S2.redact.__doc__ or ""
        self.assertIn("NOT a guarantee", doc)
        self.assertIn("sensitive, not as sanitised", doc)


class TestRamificationsOfTheRoundTwoFixes(unittest.TestCase):
    """Written after Hani pointed out that editing code and running the unit
    suite is not verification. These are the consequences the suite did not
    catch, found by running the tool on the machines it is for."""

    def test_a_symlink_is_neither_walked_nor_counted(self):
        """The scandir rewrite changed classification: os.walk listed a
        symlink-to-directory under dirnames, and
        entry.is_dir(follow_symlinks=False) returns False for one, so it fell
        through to the FILE branch. Home directories are full of symlinks, so
        every count was inflated and a bogus "(none)" extension row appeared."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "r"
            (root / "real").mkdir(parents=True)
            (root / "top.txt").write_text("x\n")
            (root / "real" / "inner.py").write_text("y\n")
            os.symlink(root / "real", root / "link-to-dir")
            os.symlink(root / "top.txt", root / "link-to-file")
            data = json.loads(run(SURVEY, "--repo", str(root), "--json").stdout)
            self.assertEqual(data["repo"]["file_count"], 2,
                             f"symlinks were counted: "
                             f"{data['repo']['extensions']}")
            self.assertNotIn("(none)", data["repo"]["extensions"],
                             "a symlink produced a bogus extension row")

    def test_a_symlink_cycle_does_not_hang_the_walk(self):
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "r"
            root.mkdir()
            (root / "f.txt").write_text("x\n")
            os.symlink(root, root / "loop")
            t0 = _t.time()
            r = run(SURVEY, "--repo", str(root), "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(_t.time() - t0, 30, "a symlink cycle stalled it")

    def test_the_same_filesystem_is_not_reported_twice(self):
        """Surveying a home directory printed the same disk twice on every
        host, because home and the repo resolve to one path."""
        with tempfile.TemporaryDirectory() as d:
            out = run(SURVEY, "--repo", str(Path.home())).stdout
            self.assertEqual(out.count("  disk "), 1, out)
            out2 = run(SURVEY, "--repo", d).stdout
            self.assertEqual(out2.count("  disk "), 2, out2)

    def test_dropping_git_status_left_no_stale_reader(self):
        """`dirty_files` became None for every consumer. Checked repo-wide:
        nothing else reads it, and the field carries a note saying why it is
        empty rather than looking forgotten."""
        readers = []
        for f in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.md")):
            if ".git/" in str(f) or "test_project" in f.name:
                continue
            if "dirty_files" in f.read_text():
                readers.append(str(f.relative_to(ROOT)))
        self.assertEqual(readers,
                         ["skills/hanig-project/scripts/survey.py"],
                         f"a stale reader of dirty_files: {readers}")
        with tempfile.TemporaryDirectory() as d:
            import subprocess as sp
            sp.run(["git", "init", "-q", "."], cwd=d, capture_output=True)
            data = json.loads(run(SURVEY, "--repo", d, "--json").stdout)
            self.assertIn("dirty_files_note", data["repo"],
                          "the field must say why it is empty, or it reads "
                          "as forgotten")


class TestUninstallOnlyRemovesOurOwn(unittest.TestCase):
    """I ran `install.sh --uninstall` on chimera to clear my own test install
    and it deleted two of Hani's skills. Its ownership test was
    `[ -L "$d" ] || [ -f "$d/$MARKER" ]`, so ANY symlink counted as ours.
    Ownership must come from something we recorded, never from a file's shape."""

    def _install_into(self, prefix):
        import subprocess as sp
        # --allow-org-shadow: this repo's own skills now exist in the Arc
        # org store, so an install without it is refused. These tests are
        # deliberately installing shadowing copies.
        return sp.run(["sh", str(ROOT / "install.sh"), "--prefix", str(prefix),
                       "--allow-org-shadow"],
                      capture_output=True, text=True, cwd=ROOT)

    def test_a_foreign_symlink_survives_uninstall(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            prefix, elsewhere = Path(d) / "prefix", Path(d) / "elsewhere"
            (elsewhere / "their-skill").mkdir(parents=True)
            (elsewhere / "their-skill" / "SKILL.md").write_text("theirs\n")
            prefix.mkdir()
            self._install_into(prefix)
            os.symlink(elsewhere / "their-skill", prefix / "their-skill")
            r = sp.run(["sh", str(ROOT / "install.sh"), "--prefix",
                        str(prefix), "--uninstall", "--allow-org-shadow"],
                       capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((prefix / "their-skill").exists(),
                            "uninstall removed a skill it did not install")
            self.assertTrue((elsewhere / "their-skill" / "SKILL.md").is_file())
            self.assertIn("left 1 skill", r.stdout)

    def test_our_own_skills_are_still_removed(self):
        """The counter-claim: an uninstall that removes nothing is useless."""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            prefix.mkdir()
            self._install_into(prefix)
            self.assertTrue((prefix / "hanig-swarm").is_dir())
            sp.run(["sh", str(ROOT / "install.sh"), "--prefix", str(prefix),
                    "--uninstall", "--allow-org-shadow"],
                   capture_output=True, text=True, cwd=ROOT)
            self.assertFalse((prefix / "hanig-swarm").exists())

    def test_install_does_not_clobber_a_foreign_symlink_either(self):
        """The same inference lived in the install path's replace check."""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            prefix, elsewhere = Path(d) / "prefix", Path(d) / "elsewhere"
            (elsewhere / "hanig-swarm").mkdir(parents=True)
            (elsewhere / "hanig-swarm" / "SKILL.md").write_text("not ours\n")
            prefix.mkdir()
            os.symlink(elsewhere / "hanig-swarm", prefix / "hanig-swarm")
            r = self._install_into(prefix)
            self.assertIn("skip hanig-swarm", r.stdout,
                          "install replaced a symlink it did not create")
            self.assertEqual(
                (elsewhere / "hanig-swarm" / "SKILL.md").read_text(),
                "not ours\n")

    def test_ownership_is_never_inferred_from_being_a_symlink(self):
        src = (ROOT / "install.sh").read_text()
        self.assertNotIn('if [ -L "$d" ] || [ -f "$d/$MARKER" ]', src,
                         "a symlink is not evidence that we created it")


class TestInstallerSymlinkEdgeCases(unittest.TestCase):
    """The shapes a symlink can take, because getting this wrong already cost
    two of someone's skills. Attacked before the reviewers reported, since
    this is the one place in the repo that deletes."""

    def _install(self, prefix, *extra):
        import subprocess as sp
        # --allow-org-shadow: this repo's own skills now exist in the Arc org
        # store, so a plain install is refused. These tests deliberately
        # install shadowing copies into a temp prefix.
        return sp.run(["sh", str(ROOT / "install.sh"), "--prefix", str(prefix),
                       "--allow-org-shadow", *extra],
                      capture_output=True, text=True, cwd=ROOT)

    def test_foreign_links_of_every_shape_survive_uninstall(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            prefix, outside = base / "prefix", base / "outside" / "theirs"
            outside.mkdir(parents=True)
            (outside / "SKILL.md").write_text("theirs\n")
            prefix.mkdir()
            self._install(prefix)
            os.symlink("../outside/theirs", prefix / "relative")   # relative
            os.symlink(base / "nope", prefix / "broken")           # dangling
            (base / "mid").mkdir()
            os.symlink(base / "mid", prefix / "mid")
            os.symlink(prefix / "mid", prefix / "nested")          # link->link
            r = self._install(prefix, "--uninstall")
            self.assertEqual(r.returncode, 0, r.stderr)
            for name in ("relative", "broken", "mid", "nested"):
                self.assertTrue(
                    (prefix / name).is_symlink(),
                    f"uninstall removed a foreign {name} symlink")
            self.assertTrue((outside / "SKILL.md").is_file())

    def test_a_link_into_this_checkout_IS_ours(self):
        """--mode link installs a symlink into the repo; uninstall must clear
        it, and must not touch what it points at."""
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            prefix.mkdir()
            os.symlink(ROOT / "skills" / "hanig-swarm", prefix / "ours-by-link")
            self._install(prefix, "--uninstall")
            self.assertFalse((prefix / "ours-by-link").is_symlink())
            self.assertTrue((ROOT / "skills" / "hanig-swarm" / "SKILL.md")
                            .is_file(), "uninstall followed the link and "
                                        "deleted the real skill")

    def test_prune_leaves_anything_without_our_marker(self):
        """Prune deletes on a NEGATIVE condition -- absent from this repo --
        which is the risky direction."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            prefix = base / "prefix"
            prefix.mkdir()
            self._install(prefix)
            marker = ".installed-by-multi-agent-skills"
            (prefix / "hanig-gone").mkdir()
            (prefix / "hanig-gone" / marker).write_text("stale\n")
            (prefix / "their-dir").mkdir()
            (prefix / "their-dir" / "SKILL.md").write_text("theirs\n")
            (base / "elsewhere").mkdir()
            os.symlink(base / "elsewhere", prefix / "their-link")
            r = self._install(prefix)
            self.assertIn("pruned 1", r.stdout)
            self.assertFalse((prefix / "hanig-gone").exists())
            self.assertTrue((prefix / "their-dir" / "SKILL.md").is_file())
            self.assertTrue((prefix / "their-link").is_symlink())

    def test_prune_does_not_run_under_only(self):
        """--only narrows the set deliberately; pruning everything else would
        turn a targeted install into a sweep."""
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            prefix.mkdir()
            self._install(prefix)
            marker = ".installed-by-multi-agent-skills"
            (prefix / "hanig-gone").mkdir()
            (prefix / "hanig-gone" / marker).write_text("stale\n")
            r = self._install(prefix, "--only", "hanig-swarm")
            self.assertNotIn("prune", r.stdout)
            self.assertTrue((prefix / "hanig-gone").is_dir())


class TestTheTrackerTeamIsNotAQuestion(unittest.TestCase):
    """A draft with no team made the session stop and ask, mid-run, with 1.42
    TiB of hashing already in flight. It also guessed "goodarzilab", which is
    the SLURM account from the plan's charge_to and not a Linear team at all:
    two namespaces that look alike."""

    def test_a_draft_carries_the_default_team(self):
        self.assertEqual(T.draft(PLAN)["project"]["team"], T.DEFAULT_TEAM)
        self.assertEqual(T.DEFAULT_TEAM, "Arc")

    def test_a_brief_can_still_override_it(self):
        got = T.draft(PLAN, brief={"team": "peeks"})
        self.assertEqual(got["project"]["team"], "peeks")

    def test_an_empty_brief_does_not_erase_the_default(self):
        """A brief with no team key must not produce a null team, which is
        what sent the session back to the human."""
        for brief in ({}, {"summary": "x"}, {"team": None}, {"team": ""}):
            self.assertEqual(T.draft(PLAN, brief=brief)["project"]["team"],
                             T.DEFAULT_TEAM, f"brief={brief}")

    def test_charge_to_is_never_read_as_a_team(self):
        """The actual confusion: a SLURM account is not a tracker team."""
        plan = dict(PLAN)
        plan["charge_to"] = "goodarzilab"
        self.assertEqual(T.draft(plan)["project"]["team"], "Arc")

    def test_the_skill_says_not_to_ask(self):
        doc = (ROOT / "skills" / "hanig-project" / "SKILL.md").read_text()
        self.assertIn("do not ask", doc.lower())
        self.assertIn("charge_to", doc)


class TestFilingRequiresApproval(unittest.TestCase):
    """Asked for after the first real run filed a project and five issues and
    dispatched 1.42 TiB of work without a separate yes. Creating a tracker
    project is outward-facing: other people see it and undoing it is manual."""

    def test_a_fresh_draft_requires_approval(self):
        self.assertEqual(T.draft(PLAN)["approval"]["state"], "required")

    def test_approving_records_who_and_when(self):
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d) / "plan.json"
            plan.write_text(json.dumps(PLAN))
            run(TICKETS, "draft", str(plan))
            tk = Path(d) / "tickets.json"
            r = run(TICKETS, "approve", str(tk), "--approver", "hani")
            self.assertEqual(r.returncode, 0, r.stderr)
            a = json.loads(tk.read_text())["approval"]
            self.assertEqual((a["state"], a["granted_by"]), ("granted", "hani"))
            self.assertTrue(a["at"])

    def test_the_phrase_is_not_an_everyday_word(self):
        """"yes" or "go" would appear in ordinary conversation and make the
        gate meaningless."""
        self.assertEqual(T.AUTOPILOT_PHRASE, "swarm autopilot")
        for casual in ("yes", "go", "ok", "sure", "go ahead", "sounds good"):
            self.assertNotEqual(casual, T.AUTOPILOT_PHRASE)
            self.assertNotIn(T.AUTOPILOT_PHRASE, casual)

    def test_autopilot_clears_the_gate(self):
        self.assertEqual(T.draft(PLAN, autopilot=True)["approval"]["state"],
                         "autopilot")

    def test_an_existing_approval_is_not_re_requested(self):
        """Re-drafting after a plan edit must not send the human back through
        a gate for work they already accepted."""
        first = T.draft(PLAN)
        first["approval"] = {"state": "granted", "granted_by": "hani",
                             "at": "2026-08-29T00:00:00+0000"}
        self.assertEqual(T.draft(PLAN, existing=first)["approval"]["state"],
                         "granted")

    def test_a_required_gate_is_not_silently_inherited_as_granted(self):
        """The counter-claim: a prior draft that was NEVER approved must not
        satisfy the gate."""
        first = T.draft(PLAN)
        self.assertEqual(first["approval"]["state"], "required")
        self.assertEqual(T.draft(PLAN, existing=first)["approval"]["state"],
                         "required")

    def test_the_draft_output_says_nothing_may_be_created(self):
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d) / "plan.json"
            plan.write_text(json.dumps(PLAN))
            out = run(TICKETS, "draft", str(plan)).stdout
            self.assertIn("APPROVAL REQUIRED", out)
            self.assertIn(T.AUTOPILOT_PHRASE, out)

    def test_the_skill_names_the_gate_and_the_phrase(self):
        doc = (ROOT / "skills" / "hanig-project" / "SKILL.md").read_text()
        self.assertIn("swarm autopilot", doc)
        self.assertIn("DEFAULT IS TO STOP HERE", doc)


class TestTheRepoDecisionComesFromTheSurvey(unittest.TestCase):
    """An existing repo with a remote is ADOPTED, not asked about. The survey
    already knows; asking would be the tedium the survey exists to remove."""

    def _survey(self, d):
        return json.loads(run(SURVEY, "--repo", str(d), "--json").stdout)

    def test_a_repo_with_a_remote_is_fully_identified(self):
        """Everything the front door needs to adopt silently and say which."""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            sp.run(["git", "init", "-q", "."], cwd=d, capture_output=True)
            sp.run(["git", "remote", "add", "origin",
                    "git@github.com:goodarzilab/example.git"], cwd=d,
                   capture_output=True)
            (Path(d) / "f.py").write_text("x = 1\n")
            sp.run(["git", "add", "-A"], cwd=d, capture_output=True)
            sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "i"], cwd=d, capture_output=True)
            r = self._survey(d)["repo"]
            self.assertTrue(r["git"])
            self.assertEqual(r["remote"],
                             "git@github.com:goodarzilab/example.git")
            self.assertTrue(r.get("branch"))

    def test_a_repo_without_a_remote_is_distinguishable(self):
        """A different answer is needed here: local-only is legitimate, so
        this is the one case worth a question."""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            sp.run(["git", "init", "-q", "."], cwd=d, capture_output=True)
            r = self._survey(d)["repo"]
            self.assertTrue(r["git"])
            self.assertIsNone(r.get("remote"))

    def test_a_plain_directory_is_distinguishable(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._survey(d)["repo"]
            self.assertFalse(r["git"])

    def test_the_skill_says_adopt_without_asking(self):
        doc = (ROOT / "skills" / "hanig-project" / "SKILL.md").read_text()
        self.assertIn("Adopt it. Do not ask", doc)
        self.assertIn("first-class answer", doc)

    def test_the_skill_forbids_committing_swarm_outputs(self):
        """Outputs are DATA. They reach a shared path through promote, with a
        named approver, and never through a repo."""
        doc = (ROOT / "skills" / "hanig-project" / "SKILL.md").read_text()
        self.assertIn("may commit a swarm output to a repo", doc)
        self.assertIn("outlive the attempt directory as SOURCE", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
