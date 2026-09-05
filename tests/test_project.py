"""The front door: survey and tickets.

Both scripts exist to enforce one rule each. survey.py enforces "never ask
what you can look up"; tickets.py enforces "every unit maps to an issue and
back". These test the rules, not the plumbing.
"""
import ast
import contextlib
import hashlib
import http.server
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
            {"id": "agent", "kind": "code", "repo": "/tmp/fixture-repo", "target_branch": "main", "mode": "bypass", "prompt": "do it",
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


class TestTheInstalledVersionSurvivesAWorktree(unittest.TestCase):
    """Both scripts asked `[ -d "$REPO/.git" ]`, which is false in a worktree
    because `.git` is a FILE there. So every install from an attempt worktree
    -- the normal way to try a change before merging it -- stamped
    version=unknown, and doctor reported provenance it had been handed for
    free. `unknown` reads like a value, not like a failure to look."""

    def _repo_head(self):
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short",
                            "HEAD"], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def test_the_marker_records_the_commit_not_unknown(self):
        head = self._repo_head()
        if not head:
            self.skipTest("not a git checkout, so there is no version to find")
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            r = subprocess.run(
                ["sh", str(ROOT / "install.sh"), "--prefix", str(prefix),
                 "--allow-org-shadow"],
                capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(r.returncode, 0, r.stderr)
            marker = (prefix / "hanig-swarm"
                      / ".installed-by-multi-agent-skills").read_text()
            version = next(l.split("=", 1)[1] for l in marker.splitlines()
                           if l.startswith("version="))
            self.assertNotEqual(version, "unknown", marker)
            self.assertTrue(version.startswith(head),
                            f"marker says {version!r}, HEAD is {head!r}")

    def test_doctor_names_the_commit(self):
        if not self._repo_head():
            self.skipTest("not a git checkout, so there is no version to find")
        r = subprocess.run(["sh", str(ROOT / "bin" / "doctor")],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertIn("commit: ", r.stdout)
        self.assertNotIn("commit: unknown", r.stdout)


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


MARKER = ".installed-by-multi-agent-skills"


def _skill_dirs():
    return sorted(d.name for d in (ROOT / "skills").iterdir()
                  if d.is_dir() and not d.name.startswith("."))


def _authored():
    return [n for n in _skill_dirs() if n.startswith("hanig-")]


def _vendored():
    return [n for n in _skill_dirs() if not n.startswith("hanig-")]


def _doctor_sections(path):
    """bin/doctor split by the headers it prints, so a rule can apply to one
    part of it. Derived from the file's own output, never from line numbers,
    which is the same reason vendoredness is read off the tree."""
    out, cur = {}, "(preamble)"
    for line in path.read_text().splitlines():
        m = re.match(r"printf '\\n=== (.+?) ===\\n'", line.strip())
        if m:
            cur = m.group(1)
        out.setdefault(cur, []).append(line)
    return out


class TestVendoredSkillsAreNotOursToDelete(unittest.TestCase):
    """19c5171 vendored eight skills from another author's repo verbatim, and
    install.sh stamped them with the same ownership marker it writes on the
    skills we wrote. That marker is the whole basis of `--uninstall`, so on a
    host where upstream is also installed -- the setup
    docs/plan-swarm-sol-variant.md records -- removing "our" skills took
    theirs out of ~/.claude/skills too. Same shape as the chimera incident
    TestUninstallOnlyRemovesOurOwn guards: ownership came from something we
    recorded, but what we recorded no longer meant what it used to.

    Vendored-ness is read off the tree, never from a list: this repo names
    everything it writes hanig-*, so a directory under skills/ outside that
    namespace arrived from somebody else."""

    def _install(self, prefix, *extra):
        return subprocess.run(
            ["sh", str(ROOT / "install.sh"), "--prefix", str(prefix),
             "--allow-org-shadow", *extra],
            capture_output=True, text=True, cwd=ROOT)

    def _upstream_install(self, base, prefix, name="paseo", shape="link"):
        """What upstream's own install looks like from here: a symlink into
        their checkout, or a plain directory we never stamped."""
        theirs = base / "their-checkout" / name
        theirs.mkdir(parents=True)
        (theirs / "SKILL.md").write_text("theirs\n")
        prefix.mkdir(exist_ok=True)
        if shape == "link":
            os.symlink(theirs, prefix / name)
        else:
            (prefix / name).mkdir()
            (prefix / name / "SKILL.md").write_text("theirs\n")
        return theirs

    # --- uninstall ----------------------------------------------------------

    def test_a_vendored_skill_survives_uninstall(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            self._install(prefix)
            self.assertTrue((prefix / "paseo" / "SKILL.md").is_file())
            r = self._install(prefix, "--uninstall")
            self.assertEqual(r.returncode, 0, r.stderr)
            for name in _vendored():
                self.assertTrue((prefix / name).is_dir(),
                                f"uninstall deleted {name}, which this repo "
                                f"vendored rather than wrote")
            self.assertIn("vendored", r.stdout)

    def test_an_authored_skill_is_still_removed_by_a_plain_uninstall(self):
        """The counter-claim. --uninstall must not become gentler in general;
        for the skills this repo wrote it stays a plain delete, no flag."""
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            self._install(prefix)
            authored = _authored()
            self.assertTrue(authored)
            r = self._install(prefix, "--uninstall")
            for name in authored:
                self.assertFalse((prefix / name).exists(),
                                 f"--uninstall left {name}, which this repo "
                                 f"wrote: it has gone gentle in general")
            self.assertIn(f"removed {len(authored)} skill(s)", r.stdout)

    def test_include_vendored_removes_them_when_asked(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            self._install(prefix)
            r = self._install(prefix, "--uninstall", "--include-vendored")
            self.assertEqual(r.returncode, 0, r.stderr)
            for name in _skill_dirs():
                self.assertFalse((prefix / name).exists(), name)

    def test_a_marker_written_before_origin_existed_is_not_deleted(self):
        """Anything installed by 19c5171 itself carries a marker with no
        origin= line. Reading that as "authored here" would delete exactly the
        skills this change exists to protect, on exactly the hosts that
        already have them."""
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            prefix.mkdir()
            for name, keep in (("paseo", True), ("hanig-swarm", False)):
                (prefix / name).mkdir()
                (prefix / name / MARKER).write_text(
                    "repo=multi-agent-skills\nversion=19c5171\n")
            self._install(prefix, "--uninstall")
            self.assertTrue((prefix / "paseo").is_dir(),
                            "an origin-less marker on a vendored name was "
                            "read as ours to delete")
            self.assertFalse((prefix / "hanig-swarm").exists())

    # --- installing over somebody else's copy -------------------------------

    def test_a_pre_existing_upstream_install_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            prefix = base / "prefix"
            theirs = self._upstream_install(base, prefix)
            r = self._install(prefix)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("paseo", r.stderr)
            self.assertIn(str(theirs), r.stderr,
                          "the refusal must name the collision, not just "
                          "report that there was one")
            self.assertTrue((prefix / "paseo").is_symlink())
            self.assertEqual((theirs / "SKILL.md").read_text(), "theirs\n")
            self.assertFalse((prefix / "hanig-swarm").exists(),
                             "refused at second zero, or it did not refuse")

    def test_a_directory_with_someone_elses_marker_is_a_collision(self):
        """Absent marker or a marker naming a different source: both mean
        this install is not ours to replace."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            prefix = base / "prefix"
            prefix.mkdir()
            (prefix / "paseo").mkdir()
            (prefix / "paseo" / MARKER).write_text("repo=someone-elses\n")
            r = self._install(prefix)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("paseo", r.stderr)

    def test_the_refusal_is_not_confused_with_org_shadowing(self):
        """An operator who reads "org shadow" does not think "I am about to
        replace the upstream author's paseo"."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            prefix = base / "prefix"
            self._upstream_install(base, prefix)
            err = self._install(prefix).stderr
            self.assertIn("--allow-vendored-shadow", err)
            self.assertIn("NOT the --allow-org-shadow case", err)

    def test_taking_the_name_over_is_allowed_but_announced(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            prefix = base / "prefix"
            theirs = self._upstream_install(base, prefix)
            r = self._install(prefix, "--allow-vendored-shadow")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("taking over 'paseo'", r.stderr)
            self.assertFalse((prefix / "paseo").is_symlink())
            self.assertTrue((prefix / "paseo" / MARKER).is_file())
            self.assertEqual((theirs / "SKILL.md").read_text(), "theirs\n",
                             "replacing the link must not follow it")

    def test_an_authored_name_installed_by_someone_else_is_still_skipped(self):
        """Unchanged: a foreign hanig-* is skipped, not refused. The vendored
        refusal is about names we ship but did not write."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            prefix = base / "prefix"
            self._upstream_install(base, prefix, name="hanig-swarm")
            r = self._install(prefix)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("skip hanig-swarm", r.stdout)
            self.assertTrue((prefix / "hanig-swarm").is_symlink())

    # --- where "vendored" comes from ----------------------------------------

    def test_the_marker_records_which_it_is(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            self._install(prefix)
            for name in _skill_dirs():
                origin = [l.split("=", 1)[1] for l
                          in (prefix / name / MARKER).read_text().splitlines()
                          if l.startswith("origin=")]
                want = "authored" if name.startswith("hanig-") else "vendored"
                self.assertEqual(origin, [want], f"{name}: {origin}")

    def test_vendoredness_is_derived_from_the_tree_not_a_written_list(self):
        """A list of vendored names in the installer would rot the first time
        skills/ changed, and rot silently in the direction that deletes. The
        classification is the hanig- namespace applied to whatever is on disk,
        so no executable line may name a specific vendored skill.

        Scoped to the OWNERSHIP code rather than the whole file, because the
        guard was written as a substring search and one vendored skill is
        named after an external program. ARC-265 added a PREREQUISITES section
        that has to say `paseo` -- the binary swarm.py looks for on PATH -- and
        the path `~/.agent-bus/models.json`. Neither is a claim about who
        wrote a skill, which is the only thing this rule is about, and the
        assertions below keep the classification itself out of that section so
        the exemption cannot be used to smuggle a hardcoded list back in."""
        code = [l for l in (ROOT / "install.sh").read_text().splitlines()
                if not l.lstrip().startswith("#")]
        sections = _doctor_sections(ROOT / "bin" / "doctor")
        self.assertIn("PREREQUISITES", sections,
                      "the exempt section does not exist, so exempting it "
                      "hides nothing and this test has drifted")
        for name, lines in sections.items():
            if name == "PREREQUISITES":
                # $MARKER and $OWN_PREFIX are the only two inputs the
                # ownership verdict has, so an exempt section that touches
                # neither cannot be classifying anything.
                for var in ("$MARKER", "OWN_PREFIX"):
                    self.assertNotIn(
                        var, "\n".join(lines),
                        f"skill ownership has moved into {name}, which is "
                        f"exempt from the no-hardcoded-names rule")
                continue
            code += [l for l in lines if not l.lstrip().startswith("#")]
        joined = "\n".join(code)
        self.assertIn("vendored (installed by us", joined,
                      "the classification is no longer where this test "
                      "checks it")
        for name in _vendored():
            offenders = [l for l in code if name in l]
            self.assertEqual(offenders, [],
                             f"install.sh or bin/doctor names {name} in "
                             f"ownership code: {offenders}")
        self.assertTrue(_vendored(), "nothing vendored: this test proves "
                                     "nothing, and the guard is untested")

    def test_doctor_does_not_call_a_vendored_skill_ours(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            self._install(prefix)
            out = subprocess.run(["sh", str(ROOT / "bin" / "doctor"),
                                  "--prefix", str(prefix)],
                                 capture_output=True, text=True,
                                 cwd=ROOT).stdout
            line = next(l for l in out.splitlines()
                        if l.strip().startswith("paseo "))
            self.assertIn("vendored", line)
            ours = next(l for l in out.splitlines()
                        if l.strip().startswith("hanig-swarm "))
            self.assertIn("ours", ours)

    # --- the prefix is spelled the same way twice ---------------------------

    def test_doctor_takes_the_prefix_the_way_install_prints_it(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            r = self._install(prefix)
            self.assertIn(f"bin/doctor --prefix {prefix}", r.stdout,
                          "install told you to verify a prefix it did not "
                          "install into")
            flagged = subprocess.run(
                ["sh", str(ROOT / "bin" / "doctor"), "--prefix", str(prefix)],
                capture_output=True, text=True, cwd=ROOT)
            self.assertIn("paseo", flagged.stdout)
            self.assertIn(str(prefix), flagged.stdout)

    def test_the_old_positional_prefix_still_works(self):
        """Aligning the spelling is not worth breaking a script or a shell
        history that already passes it positionally."""
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "prefix"
            self._install(prefix)
            positional = subprocess.run(
                ["sh", str(ROOT / "bin" / "doctor"), str(prefix)],
                capture_output=True, text=True, cwd=ROOT).stdout
            flagged = subprocess.run(
                ["sh", str(ROOT / "bin" / "doctor"), "--prefix", str(prefix)],
                capture_output=True, text=True, cwd=ROOT).stdout
            skills = lambda o: o.split("=== INSTALLED SKILLS ===")[1] \
                .split("=== SCRIPT HEALTH ===")[0]
            self.assertEqual(skills(positional), skills(flagged))


class TestVendoredAgentBusLayoutIsExplicit(unittest.TestCase):
    """ARC-272. The vendored skills name upstream's executable location, but
    this repository installs only skills and keeps bus in its checkout. The
    local documentation must leave an operator with a workflow that runs."""

    def test_the_readme_names_both_layouts_and_a_working_local_command(self):
        readme = (ROOT / "README.md").read_text()
        heading = "### Agent bus executable, in this checkout"
        self.assertIn(heading, readme)
        section = readme.split(heading, 1)[1].split("\n### ", 1)[0]

        self.assertIn("`~/.agent-bus/bin/bus`", section)
        for boundary in ("#### Executable discovery",
                         "#### Model-routing registry input",
                         "#### Runtime state and cache ownership",
                         "#### Disposable model-routing check"):
            self.assertIn(boundary, section)
        self.assertIn('cp "$checkout/models.json" '
                      '"$bus_state/models.json"', section)
        self.assertIn('HOME="$bus_state/home" AGENT_BUS_HOME="$bus_state" '
                      '"$bus_bin" models --json', section)
        self.assertIn("(\nset -eu\n", section)
        self.assertIn("AGENT_BUS_SCRATCH must resolve outside HOME", section)
        for trapped in ("trap cleanup EXIT", "trap 'exit 129' HUP",
                        "trap 'exit 130' INT", "trap 'exit 143' TERM"):
            self.assertIn(trapped, section)
        self.assertNotIn('AGENT_BUS_HOME="$checkout', section)
        self.assertNotIn('mkdir -p ~/.agent-bus', section)
        self.assertNotIn('cp models.json ~/.agent-bus', section)
        self.assertIn("upstream", section.lower())

        local_bus = ROOT / "bin" / "bus"
        self.assertTrue(local_bus.is_file(), local_bus)
        self.assertTrue(os.access(local_bus, os.X_OK),
                        "README's checkout-local bus is not executable")

        def snapshot(root):
            # Deliberately walk the filesystem rather than `git status`: the
            # contract includes ignored and untracked artifacts too.
            entries = []
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(root).as_posix()
                mode = path.lstat().st_mode
                if path.is_symlink():
                    entries.append((rel, "link", mode, os.readlink(path)))
                elif path.is_file():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    entries.append((rel, "file", mode, digest))
                else:
                    entries.append((rel, "dir", mode, None))
            return entries

        with tempfile.TemporaryDirectory() as d:
            sandbox = Path(d)
            fake_home = sandbox / "home"
            outside = sandbox / "outside"
            external_scratch = sandbox / "external-scratch"
            for path in (fake_home, outside, external_scratch):
                path.mkdir()
            env = dict(os.environ)
            env.update({
                "HOME": str(fake_home),
                "AGENT_BUS_SCRATCH": str(external_scratch),
                "MULTI_AGENT_SKILLS_CHECKOUT": str(ROOT),
            })
            env.pop("AGENT_BUS_HOME", None)

            home_before = snapshot(fake_home)
            outside_before = snapshot(outside)
            checkout_before = snapshot(ROOT)
            check = section.split("#### Disposable model-routing check", 1)[1]
            documented = check.split("```bash", 1)[1].split("```", 1)[0]

            # Inspect a real invocation before cleanup so the cache-location
            # claim is tested independently of the example's EXIT trap.
            runtime_state = sandbox / "runtime-probe"
            runtime_home = runtime_state / "home"
            runtime_home.mkdir(parents=True)
            runtime_registry = runtime_state / "models.json"
            shutil.copyfile(ROOT / "models.json", runtime_registry)
            runtime_registry.chmod(0o600)
            runtime_env = dict(env)
            runtime_env.update({"HOME": str(runtime_home),
                                "AGENT_BUS_HOME": str(runtime_state)})
            probed = subprocess.run(
                [str(local_bus), "models", "--json"], cwd=outside,
                env=runtime_env, capture_output=True, text=True, timeout=30)
            self.assertEqual(probed.returncode, 0, probed.stderr)
            for dirname in ("sessions", "inbox", "cursors", "cache"):
                self.assertTrue((runtime_state / dirname).is_dir(), dirname)
            cache_files = list((runtime_state / "cache").iterdir())
            self.assertTrue(cache_files, "models produced no runtime cache")
            self.assertTrue(all(path.is_file() for path in cache_files),
                            cache_files)

            wrapped = ("caller_options_before=$-\n" + documented +
                       "\ncaller_options_after=$-\n"
                       "printf 'caller-options-before=%s after=%s\\n' "
                       '"$caller_options_before" "$caller_options_after" >&2\n')
            ran = subprocess.run(
                ["sh", "-c", wrapped], cwd=outside, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            rows = json.loads(ran.stdout)
            expected = json.loads((ROOT / "models.json").read_text())
            self.assertEqual({row["id"] for row in rows},
                             {row["id"] for row in expected["models"]})

            match = re.search(r"^disposable AGENT_BUS_HOME=(.+)$",
                              ran.stderr, re.MULTILINE)
            self.assertIsNotNone(match, ran.stderr)
            printed_state = Path(match.group(1))
            options = re.search(r"caller-options-before=(\S*) after=(\S*)",
                                ran.stderr)
            self.assertIsNotNone(options, ran.stderr)
            self.assertEqual(options.group(1), options.group(2),
                             "the pasted block changed caller shell options")
            self.assertFalse(printed_state.exists(),
                             "EXIT trap left its private state behind")
            self.assertEqual(list(external_scratch.iterdir()), [])
            self.assertFalse((fake_home / ".agent-bus").exists())
            self.assertEqual(snapshot(fake_home), home_before,
                             "the disposable check wrote into HOME")
            self.assertEqual(snapshot(outside), outside_before,
                             "models wrote runtime cache into its cwd")
            self.assertEqual(snapshot(ROOT), checkout_before,
                             "the disposable check changed the checkout")

            bad_home = sandbox / "bad-home"
            bad_scratch = bad_home / "scratch"
            bad_scratch.mkdir(parents=True)
            bad_env = dict(env)
            bad_env.update({"HOME": str(bad_home),
                            "AGENT_BUS_SCRATCH": str(bad_scratch)})
            bad_before = snapshot(bad_home)
            refused = subprocess.run(
                ["sh", "-c", documented], cwd=outside, env=bad_env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("must resolve outside HOME", refused.stderr)
            self.assertEqual(snapshot(bad_home), bad_before,
                             "a refused scratch path was modified")

    def test_the_documented_trap_cleans_failure_and_interruption(self):
        readme = (ROOT / "README.md").read_text()
        section = readme.split("### Agent bus executable, in this checkout",
                               1)[1].split("\n### ", 1)[0]
        check = section.split("#### Disposable model-routing check", 1)[1]
        documented = check.split("```bash", 1)[1].split("```", 1)[0]

        def printed_path(stderr):
            match = re.search(r"^disposable AGENT_BUS_HOME=(.+)$", stderr,
                              re.MULTILINE)
            self.assertIsNotNone(match, stderr)
            return Path(match.group(1))

        with tempfile.TemporaryDirectory() as d:
            sandbox = Path(d)
            fake_home = sandbox / "home"
            outside = sandbox / "outside"
            scratch = sandbox / "external-scratch"
            checkout = sandbox / "fake-checkout"
            fake_bin = checkout / "bin"
            for path in (fake_home, outside, scratch, fake_bin):
                path.mkdir(parents=True)
            shutil.copyfile(ROOT / "models.json", checkout / "models.json")
            fake_bus = fake_bin / "bus"
            env = dict(os.environ)
            env.update({"HOME": str(fake_home),
                        "AGENT_BUS_SCRATCH": str(scratch),
                        "TMPDIR": str(scratch),
                        "MULTI_AGENT_SKILLS_CHECKOUT": str(checkout)})
            env.pop("AGENT_BUS_HOME", None)

            fake_bus.write_text("#!/bin/sh\nexit 7\n")
            fake_bus.chmod(0o700)
            failed = subprocess.run(
                ["sh", "-c", documented], cwd=outside, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(failed.returncode, 7, failed.stderr)
            self.assertFalse(printed_path(failed.stderr).exists())
            self.assertEqual(list(scratch.iterdir()), [])

            fake_bus.write_text(
                "#!/bin/sh\n"
                "trap 'exit 143' HUP INT TERM\n"
                "while :; do sleep 1; done\n")
            fake_bus.chmod(0o700)
            proc = subprocess.Popen(
                ["sh", "-c", documented], cwd=outside, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL, start_new_session=True)
            line = proc.stderr.readline()
            active_state = printed_path(line)
            self.assertTrue(active_state.is_dir(), line)
            os.killpg(os.getpgid(proc.pid), 15)
            _out, err = proc.communicate(timeout=15)
            # The wrapper `sh -c` may itself die from the group signal before
            # the documented subshell exits 143; cleanup is the contract.
            self.assertIn(proc.returncode, (-15, 143), line + err)
            self.assertFalse(active_state.exists())
            self.assertEqual(list(scratch.iterdir()), [])
            self.assertFalse((fake_home / ".agent-bus").exists())

    def test_setup_failures_never_reach_bus_or_escape_scratch(self):
        readme = (ROOT / "README.md").read_text()
        section = readme.split("### Agent bus executable, in this checkout",
                               1)[1].split("\n### ", 1)[0]
        check = section.split("#### Disposable model-routing check", 1)[1]
        documented = check.split("```bash", 1)[1].split("```", 1)[0]

        def snapshot(root):
            entries = []
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(root).as_posix()
                mode = path.lstat().st_mode
                if path.is_symlink():
                    value = os.readlink(path)
                elif path.is_file():
                    value = hashlib.sha256(path.read_bytes()).hexdigest()
                else:
                    value = None
                entries.append((rel, mode, value))
            return entries

        with tempfile.TemporaryDirectory() as d:
            sandbox = Path(d)
            fake_home = sandbox / "home"
            outside = sandbox / "outside"
            scratch = sandbox / "external-scratch"
            checkout = sandbox / "fake-checkout"
            fake_bin = checkout / "bin"
            for path in (fake_home, outside, scratch, fake_bin):
                path.mkdir(parents=True)
            shutil.copyfile(ROOT / "models.json", checkout / "models.json")
            invoked = sandbox / "bus-invoked"
            fake_bus = fake_bin / "bus"
            fake_bus.write_text(
                "#!/bin/sh\n"
                "printf invoked > \"$BUS_INVOKED\"\n"
                "mkdir -p \"${AGENT_BUS_HOME:-.}/cache\"\n")
            fake_bus.chmod(0o700)
            base_env = dict(os.environ)
            base_env.update({"HOME": str(fake_home),
                             "AGENT_BUS_SCRATCH": str(scratch),
                             "MULTI_AGENT_SKILLS_CHECKOUT": str(checkout),
                             "BUS_INVOKED": str(invoked)})
            base_env.pop("AGENT_BUS_HOME", None)

            def assert_setup_failure(label, run_env, candidate_scratch,
                                     expected_code=None):
                before = tuple(snapshot(path)
                               for path in (fake_home, outside, checkout))
                result = subprocess.run(
                    ["sh", "-c", documented], cwd=outside, env=run_env,
                    capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0,
                                    f"{label} unexpectedly continued")
                if expected_code is not None:
                    self.assertEqual(result.returncode, expected_code,
                                     f"{label} failed at the wrong step")
                self.assertFalse(invoked.exists(),
                                 f"{label} reached the bus executable")
                self.assertFalse((fake_home / ".agent-bus").exists())
                self.assertEqual(
                    tuple(snapshot(path)
                          for path in (fake_home, outside, checkout)), before,
                    f"{label} wrote outside disposable scratch")
                if candidate_scratch.exists():
                    self.assertEqual(list(candidate_scratch.iterdir()), [],
                                     f"{label} left scratch artifacts")

            original_path = base_env.get("PATH", "")
            # Start with cp so the non-vacuity run against the old README is
            # contained in a valid scratch directory before it exposes that
            # the block continued to bus.
            for command, exit_code in (("cp", 33), ("mktemp", 31),
                                       ("mkdir", 32), ("chmod", 34)):
                tools = sandbox / ("fail-" + command)
                tools.mkdir()
                failing = tools / command
                failing.write_text("#!/bin/sh\nexit %d\n" % exit_code)
                failing.chmod(0o700)
                failed_env = dict(base_env)
                failed_env["PATH"] = str(tools) + os.pathsep + original_path
                assert_setup_failure(command, failed_env, scratch, exit_code)

            missing = sandbox / "does-not-exist"
            missing_env = dict(base_env)
            missing_env["AGENT_BUS_SCRATCH"] = str(missing)
            assert_setup_failure("missing scratch", missing_env, missing)

            unwritable = sandbox / "unwritable"
            unwritable.mkdir()
            unwritable.chmod(0o500)
            try:
                if not os.access(unwritable, os.W_OK):
                    unwritable_env = dict(base_env)
                    unwritable_env["AGENT_BUS_SCRATCH"] = str(unwritable)
                    assert_setup_failure("unwritable scratch", unwritable_env,
                                         unwritable)
            finally:
                unwritable.chmod(0o700)

    def test_the_upstream_report_separates_executable_and_state_paths(self):
        report = (ROOT / "docs" /
                  "upstream-agent-bus-path-discovery.md").read_text()
        for contract in ("AGENT_BUS_BIN", "command -v bus",
                         "~/.agent-bus/bin/bus", "AGENT_BUS_HOME"):
            self.assertIn(contract, report)
        self.assertIn("state lookup only", report)

        references = {}
        for skill in (ROOT / "skills").glob("*/SKILL.md"):
            count = skill.read_text().count("~/.agent-bus/bin/bus")
            if count:
                references[skill.relative_to(ROOT).as_posix()] = count
        expected = {
            "skills/agent-bus/SKILL.md": 2,
            "skills/paseo/SKILL.md": 2,
            "skills/pi-fleet/SKILL.md": 5,
            "skills/start-a-sprint/SKILL.md": 3,
        }
        self.assertEqual(references, expected)
        for path, count in references.items():
            self.assertIn(f"`{path}` ({count} hardcoded occurrences)", report)
        self.assertIn("every bus invocation in all four skills", report)


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


class TestTheSurveySaysWhoMayUseAPartition(unittest.TestCase):
    """Bought on a real cluster: hours went into `QOSGrpCpuLimit` while a
    736-CPU partition sat with 202 CPUs idle, because the survey reported
    partition SIZES and never reported who was allowed in. Three facts decide
    that and none of them come from `sinfo` -- AllowAccounts, the QOS
    GrpTRES, and MaxMemPerCPU, which refuses a job while naming CPUs rather
    than the memory that actually broke the limit.

    THIS MACHINE HAS NO SLURM, so every test here drives a stubbed sinfo /
    scontrol / sacctmgr on PATH, the same mechanism test_swarm.py uses for a
    fake scheduler. Nothing below was measured against a real controller."""

    # One line per partition, exactly as `scontrol -o show partition` prints
    # it. TRES sits directly before MaxMemPerCPU on purpose: its value
    # contains its own '=' signs, and a parser that mis-consumed it would lose
    # every field after it.
    PARTITIONS = (
        'PartitionName=cpu AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL '
        'Default=YES State=UP MaxTime=7-00:00:00 TotalNodes=12 '
        'TRES=cpu=736,mem=6000000M MaxMemPerCPU=UNLIMITED QOS=N/A',
        'PartitionName=lab AllowGroups=ALL AllowAccounts=goodarzilab,shared '
        'Default=NO State=UP MaxTime=infinite TotalNodes=8 '
        'TRES=cpu=736,mem=6000000M MaxMemPerCPU=5120 QOS=lab_qos',
        'PartitionName=preemptible AllowGroups=ALL '
        'DenyAccounts=goodarzilab Default=NO State=UP MaxTime=1-00:00:00 '
        'TotalNodes=40 MaxMemPerCPU=0 QOS=normal',
        # Neither Allow nor Deny printed. Not a shape Slurm emits today,
        # which is the point: the field must go UNKNOWN rather than assume.
        'PartitionName=odd Default=NO State=UP MaxTime=1:00:00 '
        'TotalNodes=1 MaxMemPerCPU=1024 QOS=N/A',
        # Hidden from sinfo, present in scontrol. A plan can still name it.
        'PartitionName=hidden_dev AllowAccounts=goodarzilab Hidden=YES '
        'Default=NO State=UP MaxTime=4:00:00 TotalNodes=1 '
        'MaxMemPerCPU=8192 QOS=dev_qos',
    )
    QOS_ROWS = ("normal|", "lab_qos|cpu=512,mem=2000G", "dev_qos|cpu=8")

    def _fake_slurm(self, d, tools=("sinfo", "scontrol", "sacctmgr"),
                    qos_rows=None):
        """A Slurm on PATH that answers only what the survey asks.

        Tools are named explicitly so a test can REMOVE one: a host with sinfo
        and no sacctmgr is the case that must report unknown rather than
        unrestricted, and it cannot be tested by deleting code."""
        binp = Path(d) / "fakebin"
        binp.mkdir(exist_ok=True)
        rows = self.QOS_ROWS if qos_rows is None else qos_rows
        bodies = {
            "sinfo": "#!/bin/sh\n"
                     'echo "cpu*|up|7-00:00:00|12"\n'
                     'echo "lab|up|infinite|8"\n'
                     'echo "preemptible|up|1-00:00:00|40"\n',
            "scontrol": "#!/bin/sh\ncase \"$*\" in\n"
                        "  *'show config'*)\n"
                        '    echo "DefMemPerCPU = 4096"\n'
                        '    echo "DefMemPerNode = UNLIMITED"\n'
                        '    echo "SchedulerType = sched/backfill" ;;\n'
                        "  *'show partition'*)\n"
                        + "".join(f"    echo '{line}'\n"
                                  for line in self.PARTITIONS)
                        + "    ;;\nesac\n",
            "sacctmgr": "#!/bin/sh\ncase \"$*\" in\n"
                        "  *'show qos'*)\n"
                        + "".join(f"    echo '{r}'\n" for r in rows)
                        + "    ;;\n"
                        "  *'show assoc'*) echo 'goodarzilab' ;;\nesac\n",
        }
        for name in tools:
            f = binp / name
            f.write_text(bodies[name])
            f.chmod(0o755)
        return str(binp)

    def _survey(self, tools=("sinfo", "scontrol", "sacctmgr"), qos_rows=None):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ)
            env["PATH"] = (self._fake_slurm(d, tools, qos_rows)
                           + os.pathsep + env.get("PATH", ""))
            r = subprocess.run([sys.executable, str(SURVEY), "--repo", d,
                                "--json"], capture_output=True, text=True,
                               env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)

    def _part(self, data, name):
        for p in data["scheduler"]["partitions"]:
            if p["partition"] == name:
                return p
        self.fail(f"{name} missing from {[p['partition'] for p in data['scheduler']['partitions']]}")

    def test_a_restricted_partition_names_its_accounts(self):
        p = self._part(self._survey(), "lab")
        self.assertEqual(p["allow_accounts"],
                         {"state": "set", "value": ["goodarzilab", "shared"]})

    def test_an_open_partition_is_unrestricted_not_empty(self):
        p = self._part(self._survey(), "cpu")
        self.assertEqual(p["allow_accounts"]["state"], "unrestricted")
        self.assertIsNone(p["allow_accounts"]["value"])

    def test_a_missing_scontrol_is_UNKNOWN_and_never_unrestricted(self):
        """The assertion this whole item exists for. 'no AllowAccounts
        restriction' and 'could not determine AllowAccounts' are different
        facts, and on a host without scontrol only the second is true."""
        p = self._part(self._survey(tools=("sinfo",)), "lab")
        for field in ("allow_accounts", "deny_accounts", "max_mem_per_cpu_mb",
                      "qos", "qos_grptres"):
            self.assertEqual(p[field]["state"], "unknown", field)
            self.assertIsNone(p[field]["value"], field)
            self.assertIn("scontrol", p[field]["why"], field)

    def test_max_mem_per_cpu_is_a_number_and_UNLIMITED_is_not_zero(self):
        data = self._survey()
        self.assertEqual(self._part(data, "lab")["max_mem_per_cpu_mb"],
                         {"state": "set", "value": 5120})
        self.assertEqual(
            self._part(data, "cpu")["max_mem_per_cpu_mb"]["state"],
            "unrestricted")
        # Slurm prints a bare 0 for "no per-CPU cap", which as an integer
        # would divide a memory request by zero.
        self.assertEqual(
            self._part(data, "preemptible")["max_mem_per_cpu_mb"]["state"],
            "unrestricted")

    def test_the_note_states_the_cpu_cost_the_error_message_hides(self):
        """Slurm refuses the job by naming CPUs, so the error points away from
        the cause. 700G at 5120 costs 140 CPUs, not the 32 requested."""
        note = self._survey()["scheduler"]["limits_note"]
        self.assertIn("140", note)
        self.assertIn("5120", note)
        self.assertIn("UNKNOWN IS NOT UNRESTRICTED", note)

    def test_the_qos_grptres_is_joined_onto_the_partition(self):
        p = self._part(self._survey(), "lab")
        self.assertEqual(p["qos"], {"state": "set", "value": "lab_qos"})
        self.assertEqual(p["qos_grptres"],
                         {"state": "set",
                          "value": {"cpu": 512, "mem": "2000G"}})

    def test_a_count_is_an_int_and_a_unit_stays_a_string(self):
        """2000G and 2000 are not the same number, and guessing which was
        meant would put a wrong figure in the one file the interview trusts."""
        v = self._part(self._survey(), "lab")["qos_grptres"]["value"]
        self.assertIsInstance(v["cpu"], int)
        self.assertIsInstance(v["mem"], str)

    def test_a_qos_with_no_grptres_is_unrestricted(self):
        """`normal` exists and has no group limit. That is an answer, not a
        failure, and it must not read as one."""
        p = self._part(self._survey(), "preemptible")
        self.assertEqual(p["qos"]["value"], "normal")
        self.assertEqual(p["qos_grptres"]["state"], "unrestricted")

    def test_no_partition_qos_is_unrestricted_but_the_note_scopes_it(self):
        data = self._survey()
        self.assertEqual(self._part(data, "cpu")["qos"]["state"],
                         "unrestricted")
        self.assertIn("association QOS",
                      data["scheduler"]["qos_grptres_note"])

    def test_a_missing_sacctmgr_keeps_the_qos_name_and_admits_the_rest(self):
        """Half an answer, reported as half an answer: the partition's QOS is
        known from scontrol, its GrpTRES is not knowable without sacctmgr."""
        p = self._part(self._survey(tools=("sinfo", "scontrol")), "lab")
        self.assertEqual(p["qos"]["value"], "lab_qos")
        self.assertEqual(p["qos_grptres"]["state"], "unknown")
        self.assertIn("sacctmgr", p["qos_grptres"]["why"])

    def test_a_qos_sacctmgr_does_not_list_is_unknown_not_unrestricted(self):
        p = self._part(self._survey(qos_rows=("normal|",)), "lab")
        self.assertEqual(p["qos_grptres"]["state"], "unknown")
        self.assertIn("lab_qos", p["qos_grptres"]["why"])

    def test_deny_accounts_are_not_read_as_an_allowance(self):
        """AllowAccounts=ALL on a partition that denies your account is not
        the open partition it reads as. Reporting only the allowance would
        recreate the original bug with a second field."""
        p = self._part(self._survey(), "preemptible")
        self.assertEqual(p["allow_accounts"]["state"], "unrestricted")
        self.assertEqual(p["deny_accounts"],
                         {"state": "set", "value": ["goodarzilab"]})

    def test_a_partition_that_denies_nobody_does_not_cry_unknown(self):
        """Slurm prints DenyAccounts INSTEAD of AllowAccounts, never both, so
        a missing DenyAccounts beside a present AllowAccounts is an answer.
        Calling it unknown put `denied=UNKNOWN` beside every partition on a
        cluster that denies nobody -- the cries-wolf direction that gets a
        field ignored, which this repo weights as heavily as a false pass."""
        data = self._survey()
        for name in ("cpu", "lab"):
            self.assertEqual(self._part(data, name)["deny_accounts"]["state"],
                             "unrestricted", name)
        self.assertEqual(
            self._part(data, "preemptible")["allow_accounts"]["state"],
            "unrestricted")

    def test_a_record_with_neither_account_key_is_unknown_for_both(self):
        """The absence of BOTH keys is ignorance, not permission."""
        p = self._part(self._survey(), "odd")
        for field in ("allow_accounts", "deny_accounts"):
            self.assertEqual(p[field]["state"], "unknown", field)
            self.assertIn("neither", p[field]["why"])

    def test_a_partition_only_scontrol_knows_about_is_not_dropped(self):
        """Hidden=YES keeps it out of sinfo, and a plan can still name it."""
        p = self._part(self._survey(), "hidden_dev")
        self.assertEqual(p["allow_accounts"]["value"], ["goodarzilab"])
        self.assertEqual(p["nodes"], "1")
        self.assertFalse(p["default"])

    def test_the_state_vocabulary_is_closed(self):
        """A fourth state would be a fourth thing every consumer must handle,
        and value must be None whenever the state is not `set` -- otherwise
        `if p[f]["value"]` starts meaning something on an unknown field."""
        data = self._survey()
        for part in data["scheduler"]["partitions"]:
            for field in ("allow_accounts", "deny_accounts",
                          "max_mem_per_cpu_mb", "qos", "qos_grptres"):
                lim = part[field]
                self.assertIn(lim["state"],
                              ("set", "unrestricted", "unknown"),
                              f"{part['partition']}.{field}")
                if lim["state"] != "set":
                    self.assertIsNone(lim["value"])
                if lim["state"] == "unknown":
                    self.assertTrue(lim.get("why"),
                                    "an unknown field must say why")

    def test_the_whole_qos_table_is_reported_for_units_that_name_one(self):
        """A unit can carry a --qos of its own, so the join table is kept and
        not only the entries some partition happens to point at."""
        qos = self._survey()["scheduler"]["qos"]
        self.assertEqual(qos["lab_qos"]["grptres"]["value"]["cpu"], 512)
        self.assertEqual(qos["normal"]["grptres"]["state"], "unrestricted")

    def test_a_truncated_qos_table_is_unknown_rather_than_half_parsed(self):
        """The read is capped at MAX_OUTPUT, so the last row of a huge table
        is half a value: `cpu=51` would parse cleanly and be WRONG. Unknown
        beats a plausible wrong number."""
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        rows = tuple(f"q{i}|cpu=512,mem=2000G" for i in range(12_000))
        self.assertGreater(sum(len(r) + 1 for r in rows), S2.MAX_OUTPUT)
        p = self._part(self._survey(qos_rows=rows), "lab")
        self.assertEqual(p["qos_grptres"]["state"], "unknown")
        self.assertIn("read cap", p["qos_grptres"]["why"])

    def test_the_schema_version_moved_so_a_consumer_can_tell(self):
        """An older survey has no limits block at all, and that is not the
        same as a cluster with no limits."""
        self.assertGreaterEqual(self._survey()["schema_version"], 2)

    def test_a_value_containing_its_own_equals_sign_does_not_eat_the_line(self):
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        f = S2._oneliner_fields(
            "PartitionName=cpu TRES=cpu=736,mem=6000000M MaxMemPerCPU=5120")
        self.assertEqual(f["TRES"], "cpu=736,mem=6000000M")
        self.assertEqual(f["MaxMemPerCPU"], "5120")

    def test_a_machine_with_no_slurm_at_all_still_surveys(self):
        """This one is not stubbed: it is this laptop, which has no sinfo."""
        with tempfile.TemporaryDirectory() as d:
            data = json.loads(run(SURVEY, "--repo", d, "--json").stdout)
            if data["scheduler"].get("present"):
                self.skipTest("this host has a real Slurm")
            self.assertEqual(data["scheduler"], {"present": False})


class TestTheWalkCannotBeHeldOpenByASyscall(unittest.TestCase):
    """WALK_SECONDS was a COOPERATIVE bound: `deadline = time.time() +
    WALK_SECONDS`, checked in the `while stack` loop between entries. When
    os.scandir blocks inside opendir() the loop never reaches its own check,
    so the walk never returns. Measured against $HOME: still running at 45s,
    and two orphaned instances wedged 59 and 47 minutes on 0.07s of CPU,
    parked under ~/Library/CloudStorage. The machines this skill targets keep
    $HOME on NFS, where a stale handle or a dead automount blocks opendir()
    far longer than a sync daemon does, and surveying the host is step 1 --
    so the first thing anyone runs hangs, with no output and no error.

    The fan-out guards (MAX_DIRS, MAX_ENTRIES_PER_DIR, the scandir stack) are
    correct and are not what these tests are about: this failure is LATENCY,
    not volume.

    A directory whose opendir() never returns is simulated with a
    sitecustomize.py on PYTHONPATH, which every child interpreter imports at
    startup. That makes the hostile filesystem a property of the WALK CHILD
    rather than of this test process -- the only way to reproduce a blocking
    syscall without a real dead NFS mount, and it holds whatever mechanism
    the walk uses to bound itself."""

    HOSTILE = "wedged"

    def _hostile_tree(self, d, block=600):
        """A tree of two files and one directory that never answers."""
        root = Path(d) / "root"
        (root / "good").mkdir(parents=True)
        (root / self.HOSTILE).mkdir()
        (root / "b.py").write_text("y\n")
        (root / "good" / "a.py").write_text("x\n")
        site = Path(d) / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import os, time\n"
            "_real = os.scandir\n"
            "def scandir(path='.', *a, **kw):\n"
            "    if %r in str(path):\n"
            "        time.sleep(%d)\n"
            "    return _real(path, *a, **kw)\n"
            "os.scandir = scandir\n" % (self.HOSTILE, block))
        return root, site

    def test_a_directory_that_never_answers_does_not_hold_the_walk(self):
        """The bound has to hold against a syscall that does not return, and
        the result has to say it was cut short AND WHY. Fails against the
        cooperative deadline, which never gets a turn."""
        import time as _t
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        with tempfile.TemporaryDirectory() as d:
            root, site = self._hostile_tree(d)
            # The kill deadline is shortened rather than waited out: what is
            # under test is that the walk returns when the deadline passes,
            # not the particular number of seconds in it.
            keep = (S2.WALK_KILL_SECONDS, S2.REAP_SECONDS,
                    os.environ.get("PYTHONPATH"))
            S2.WALK_KILL_SECONDS, S2.REAP_SECONDS = 3, 1
            os.environ["PYTHONPATH"] = str(site)
            try:
                t0 = _t.time()
                out = S2.repo(str(root))
                elapsed = _t.time() - t0
            finally:
                S2.WALK_KILL_SECONDS, S2.REAP_SECONDS = keep[0], keep[1]
                if keep[2] is None:
                    os.environ.pop("PYTHONPATH", None)
                else:
                    os.environ["PYTHONPATH"] = keep[2]
            self.assertLess(elapsed, 30,
                            "a blocked opendir outlasted the kill deadline")
            walk = out["walk"]
            self.assertEqual(walk["state"], "stuck", walk)
            self.assertIn(self.HOSTILE, walk["why"])
            self.assertIn(self.HOSTILE, walk["stuck_at"])
            # A floor must not read as a total, in EITHER field.
            self.assertTrue(out["counted_truncated_at"], out)
            self.assertIn("FLOOR", walk["note"])
            # And what it did see before it stopped is still reported: a walk
            # killed on its first hostile directory used to report zero files
            # for a tree it had already counted, which reads as empty.
            self.assertGreaterEqual(out["file_count"], 1, out)

    def test_the_survey_itself_returns_within_the_bound_it_declares(self):
        """`_walk` in isolation is not the thing anybody runs. This drives the
        command, at its real deadline, and reads the terminal output a human
        gets -- the reader who loses the hours is the one watching stdout.

        Bounded by this test's own subprocess timeout, so a regression fails
        here instead of hanging the suite; start_new_session + killpg so the
        walk child goes too."""
        import signal as _sig
        import time as _t
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        with tempfile.TemporaryDirectory() as d:
            root, site = self._hostile_tree(d)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(site)
            t0 = _t.time()
            proc = subprocess.Popen(
                [sys.executable, str(SURVEY), "--repo", str(root)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL, env=env, start_new_session=True)
            try:
                out, err = proc.communicate(
                    timeout=S2.WALK_KILL_SECONDS + 90)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
                proc.communicate()
                self.fail("the survey never returned: a blocked opendir is "
                          "still able to hang it")
            elapsed = _t.time() - t0
            self.assertEqual(proc.returncode, 0, err)
            self.assertLess(elapsed, S2.WALK_KILL_SECONDS + 30,
                            f"the survey took {elapsed:.0f}s against a "
                            f"declared {S2.WALK_KILL_SECONDS}s bound")
            # "capped" is what too-much says. A walk that got stuck must not
            # borrow that word, and must name the directory.
            self.assertIn("CUT SHORT", out, out)
            self.assertIn("STUCK", out, out)
            self.assertIn(self.HOSTILE, out, out)
            self.assertNotIn("(capped)", out, out)

    def test_too_much_and_stuck_are_not_the_same_verdict(self):
        """counted_truncated_at carried ONE fact. `truncated` has to keep
        meaning THERE WAS TOO MUCH: a plan author reads a capped count as a
        big repo and a stuck one as a host with a filesystem that does not
        answer, and does something different about each."""
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
            walk = data["repo"]["walk"]
            self.assertEqual(walk["state"], "truncated", walk)
            self.assertIn("MAX_ENTRIES_PER_DIR", walk["why"])
            self.assertTrue(data["repo"]["counted_truncated_at"],
                            "a floor rendering as a total is the failure the "
                            "old flag exists to stop")

    def test_a_walk_that_finished_says_so_and_invents_no_reason(self):
        """The cries-wolf direction: an ordinary repo must not come back
        carrying a warning, or the warning stops being read."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.py").write_text("print(1)\n")
            res = run(SURVEY, "--repo", d, "--json")
            data = json.loads(res.stdout)
            walk = data["repo"]["walk"]
            self.assertEqual(walk["state"], "complete", walk)
            self.assertNotIn("why", walk)
            self.assertNotIn("note", walk)
            self.assertIsNone(data["repo"]["counted_truncated_at"])
            self.assertNotIn("capped", res.stdout)

    def test_the_walk_state_vocabulary_is_closed_and_never_silent(self):
        """Same rule as the LIMIT_* states: a fourth spelling is a fourth
        thing every consumer must handle, and a state that is not `complete`
        must say why or it reads as fine."""
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        self.assertEqual(
            sorted({S2.WALK_OK, S2.WALK_TRUNCATED, S2.WALK_STUCK,
                    S2.WALK_UNKNOWN}),
            ["complete", "stuck", "truncated", "unknown"])
        for state in (S2.WALK_TRUNCATED, S2.WALK_STUCK, S2.WALK_UNKNOWN):
            v = S2._walk_verdict(state, None, {"dirs": 0})
            self.assertTrue(v.get("why"), f"{state} did not say why")
            self.assertIn("FLOOR", v.get("note", ""))
        self.assertNotIn("why", S2._walk_verdict(S2.WALK_OK, None,
                                                 {"dirs": 1}))

    def test_the_skill_tells_a_reader_the_two_verdicts_differ(self):
        """The same lesson as the partition limits: a distinction that only
        exists in the JSON is a distinction the agent reading the skill will
        flatten, so the vocabulary goes where the reader hits it."""
        doc = (ROOT / "skills" / "hanig-project" / "SKILL.md").read_text()
        self.assertIn("`truncated` and `stuck` are not the same fact", doc)
        self.assertIn("stuck_at", doc)
        self.assertIn("FLOORS", doc)

    def test_no_wait_in_this_file_is_unbounded(self):
        """`run` killed a child that outlived its timeout and then called
        proc.wait() with no timeout. A process parked in an uninterruptible
        syscall -- a dead automount, a stale NFS handle -- does not die until
        that syscall returns, so that wait handed the hang straight back. The
        same defect as the walk's, one function above it."""
        tree = ast.parse(SURVEY.read_text())
        bare = [ast.unparse(n) for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "wait" and not n.keywords]
        self.assertEqual(bare, [],
                         f"an unbounded wait can hang the survey: {bare}")

    def test_the_walk_child_is_the_bound_and_says_when_it_is_not(self):
        """If no child can be launched the walk still runs, but the survey
        must not claim a bound it no longer has."""
        sys.path.insert(0, str(SCRIPTS))
        import survey as S2
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.py").write_text("print(1)\n")
            keep = sys.executable
            try:
                sys.executable = ""      # nothing to launch a child with
                counts, walk = S2._walk(d)
            finally:
                sys.executable = keep
            self.assertEqual(counts["files"], 1)
            self.assertEqual(walk["bound"], "cooperative")
            self.assertIn("never returns", walk["bound_note"])

def _filed(d, prefix="ARC-", start=200, project="proj-1"):
    """Simulate the applying session: it creates the project and the issues
    and writes the ids back into the draft, which is how 6(g) works."""
    d = json.loads(json.dumps(d))
    d["project"]["linear_id"] = project
    for n, issue in enumerate(d["issues"]):
        issue["linear_id"] = f"iss-{n}"
        issue["identifier"] = f"{prefix}{start + n}"
    return d


def _readback(edges, read_at="2026-09-03T10:00:00-0700", **kw):
    rb = {"schema_version": 1, "read_at": read_at,
          "source": "linear mcp list_issues + blockedBy", "edges": edges}
    rb.update(kw)
    return rb


class TestAStaleBlockedByEdgeIsRemovedNotMerelyNotAdded(unittest.TestCase):
    """B8, second half. `blockedBy` is APPEND-ONLY through this interface --
    Linear exposes `removeBlockedBy` as a separate operation -- so a shrunken
    `needs` list leaves behind an edge the plan no longer declares, and not
    re-adding it does not delete it.

    The draft therefore has to name the removal, and it has to decide on a
    READ of the tracker rather than on the previous draft, which records only
    what was asked for. This repo has a scar from a docstring that claimed a
    test enforced an invariant when no such test existed; these are the tests
    that were missing."""

    PLAN_WIDE = {"name": "p", "units": [
        {"id": "a", "kind": "slurm", "runtime": "none", "command": "true",
         "outputs": ["o.txt"]},
        {"id": "b", "kind": "slurm", "runtime": "none", "command": "true",
         "outputs": ["p.txt"], "needs": ["a"]}]}

    def _shrunk(self):
        plan = json.loads(json.dumps(self.PLAN_WIDE))
        plan["units"][1].pop("needs")
        return plan

    def test_a_shrunken_needs_names_the_now_absent_edge_for_removal(self):
        """The whole issue in one test: two runs against the same project,
        `needs` shrinks, and the draft must say REMOVE."""
        first = _filed(T.draft(self.PLAN_WIDE))
        # What the tracker holds after the first apply: b blockedBy a.
        rb = _readback({"ARC-200": [], "ARC-201": ["ARC-200"]})
        second = T.draft(self._shrunk(), existing=first, readback=rb)
        by_unit = {i["unit"]: i for i in second["issues"]}
        self.assertEqual(by_unit["b"]["remove_blocked_by"], ["ARC-200"])
        self.assertEqual(by_unit["b"]["add_blocked_by"], [])
        self.assertFalse(by_unit["b"]["blocked_by_in_sync"])
        self.assertIs(second["blocked_by_sync"]["in_sync"], False)

    def test_the_removal_is_a_problem_the_check_reports(self):
        first = _filed(T.draft(self.PLAN_WIDE))
        rb = _readback({"ARC-200": [], "ARC-201": ["ARC-200"]})
        plan = self._shrunk()
        second = T.draft(plan, existing=first, readback=rb)
        problems = T.edge_problems(plan, second)
        self.assertTrue(any("no longer declares" in x for x in problems),
                        problems)
        self.assertTrue(any("removeBlockedBy" in x for x in problems),
                        problems)

    def test_not_re_adding_is_not_treated_as_removing(self):
        """The counter-claim the old code implicitly made. `blocked_by` no
        longer lists `a`, and that alone must not be read as the edge being
        gone."""
        first = _filed(T.draft(self.PLAN_WIDE))
        rb = _readback({"ARC-200": [], "ARC-201": ["ARC-200"]})
        second = T.draft(self._shrunk(), existing=first, readback=rb)
        by_unit = {i["unit"]: i for i in second["issues"]}
        self.assertEqual(by_unit["b"]["blocked_by"], [])
        self.assertTrue(by_unit["b"]["remove_blocked_by"],
                        "the plan stopped declaring the edge, which is "
                        "exactly when the tracker still holds it")

    def test_once_the_removal_is_applied_a_re_read_agrees(self):
        """The verification loop closes. Without this the suite could not
        tell a check that always complains from one that checks."""
        first = _filed(T.draft(self.PLAN_WIDE))
        plan = self._shrunk()
        applied = _readback({"ARC-200": [], "ARC-201": []},
                            read_at="2026-09-03T10:05:00-0700")
        third = T.draft(plan, existing=first, readback=applied)
        self.assertEqual(T.edge_problems(plan, third), [])
        self.assertIs(third["blocked_by_sync"]["in_sync"], True)
        self.assertEqual([i["remove_blocked_by"] for i in third["issues"]],
                         [[], []])

    def test_an_edge_the_plan_still_declares_is_left_alone(self):
        first = _filed(T.draft(self.PLAN_WIDE))
        rb = _readback({"ARC-200": [], "ARC-201": ["ARC-200"]})
        second = T.draft(self.PLAN_WIDE, existing=first, readback=rb)
        by_unit = {i["unit"]: i for i in second["issues"]}
        self.assertEqual(by_unit["b"]["remove_blocked_by"], [])
        self.assertEqual(by_unit["b"]["add_blocked_by"], [],
                         "the tracker already holds it; re-adding is noise")
        self.assertEqual(T.edge_problems(self.PLAN_WIDE, second), [])

    def test_a_declared_edge_the_tracker_lacks_is_still_added(self):
        """Both directions, or the fix trades one silent disagreement for
        another."""
        first = _filed(T.draft(self.PLAN_WIDE))
        rb = _readback({"ARC-200": [], "ARC-201": []})
        second = T.draft(self.PLAN_WIDE, existing=first, readback=rb)
        by_unit = {i["unit"]: i for i in second["issues"]}
        self.assertEqual(by_unit["b"]["add_blocked_by"], ["a"])
        self.assertEqual(by_unit["b"]["remove_blocked_by"], [])

    def test_a_deleted_unit_is_still_resolvable_enough_to_remove(self):
        """The commonest stale edge points at a unit the plan DELETED, so the
        current draft cannot name it. The draft that filed it can, which is
        why prior issues join the alias map. Without that, the one edge this
        work exists to remove is the one that resolves to nothing."""
        first = _filed(T.draft(self.PLAN_WIDE))
        gone = {"name": "p", "units": [
            {"id": "b", "kind": "slurm", "runtime": "none", "command": "true",
             "outputs": ["p.txt"]}]}
        rb = _readback({"ARC-201": ["ARC-200"]})
        second = T.draft(gone, existing=first, readback=rb)
        by_unit = {i["unit"]: i for i in second["issues"]}
        self.assertEqual(by_unit["b"]["remove_blocked_by"], ["ARC-200"])
        self.assertEqual(second["blocked_by_sync"]["unresolved"], [],
                         "a former unit of this project is not a mystery")

    def test_a_handle_nothing_has_ever_known_is_reported_not_hidden(self):
        first = _filed(T.draft(self.PLAN_WIDE))
        rb = _readback({"ARC-200": [], "ARC-201": ["ARC-9999"]})
        plan = self.PLAN_WIDE
        second = T.draft(plan, existing=first, readback=rb)
        unresolved = second["blocked_by_sync"]["unresolved"]
        self.assertEqual([u["blocker"] for u in unresolved], ["ARC-9999"])
        self.assertTrue(any("ARC-9999" in x
                            for x in T.edge_problems(plan, second)))

    def test_a_foreign_issue_with_no_edges_does_not_cry_wolf(self):
        """A hand-created issue in the same tracker project cannot be holding
        a stale edge if it holds none. Flagging it is the cries-wolf failure
        that gets a guard deleted."""
        first = _filed(T.draft(self.PLAN_WIDE))
        rb = _readback({"ARC-200": [], "ARC-201": ["ARC-200"], "ARC-777": []})
        second = T.draft(self.PLAN_WIDE, existing=first, readback=rb)
        self.assertEqual(second["blocked_by_sync"]["unresolved"], [])
        self.assertEqual(T.edge_problems(self.PLAN_WIDE, second), [])

    def test_a_foreign_issue_that_does_hold_edges_is_flagged(self):
        first = _filed(T.draft(self.PLAN_WIDE))
        rb = _readback({"ARC-200": [], "ARC-201": ["ARC-200"],
                        "ARC-777": ["ARC-200"]})
        second = T.draft(self.PLAN_WIDE, existing=first, readback=rb)
        self.assertEqual([u["holder"] for u
                          in second["blocked_by_sync"]["unresolved"]],
                         ["ARC-777"])
        self.assertTrue(any("ARC-777" in x for x
                            in T.edge_problems(self.PLAN_WIDE, second)))

    def test_a_unit_id_read_back_resolves_as_well_as_an_identifier(self):
        """The applying session may key the read-back however it likes; the
        shape says so, so all three handles must work."""
        first = _filed(T.draft(self.PLAN_WIDE))
        for edges in ({"a": [], "b": ["a"]},
                      {"iss-0": [], "iss-1": ["iss-0"]},
                      {"arc-200": [], "arc-201": ["arc-200"]}):
            second = T.draft(self._shrunk(), existing=first,
                             readback=_readback(edges))
            by_unit = {i["unit"]: i for i in second["issues"]}
            self.assertEqual(len(by_unit["b"]["remove_blocked_by"] or []), 1,
                             edges)


class TestAnAbsentReadBackCannotClaimSync(unittest.TestCase):
    """Fail closed. An absent read-back must not render as "no stale edges":
    that is the class of bug this repo keeps fighting, where unknown reads as
    fine. `remove_blocked_by` is null, never [], and the check refuses."""

    PLAN = {"name": "p", "units": [
        {"id": "a", "kind": "slurm", "runtime": "none", "command": "true",
         "outputs": ["o.txt"]},
        {"id": "b", "kind": "slurm", "runtime": "none", "command": "true",
         "outputs": ["p.txt"], "needs": ["a"]}]}

    def test_no_read_back_leaves_removals_null_not_empty(self):
        d = T.draft(self.PLAN)
        for issue in d["issues"]:
            self.assertIsNone(issue["remove_blocked_by"], issue["unit"])
            self.assertIsNone(issue["blocked_by_in_sync"], issue["unit"])
        self.assertEqual(d["blocked_by_sync"]["state"], "absent")
        self.assertIsNone(d["blocked_by_sync"]["in_sync"])

    def test_a_filed_project_with_no_read_back_fails_the_check(self):
        filed = _filed(T.draft(self.PLAN))
        redrafted = T.draft(self.PLAN, existing=filed)
        redrafted[T.READBACK] = None
        problems = T.edge_problems(self.PLAN, redrafted)
        self.assertTrue(any("CANNOT BE TOLD" in x for x in problems), problems)
        self.assertTrue(any("not a claim that there are none" in x
                            for x in problems), problems)

    def test_check_exits_nonzero_and_says_so_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_p = Path(tmp) / "plan.json"
            plan_p.write_text(json.dumps(self.PLAN))
            tk = Path(tmp) / "tickets.json"
            tk.write_text(json.dumps(_filed(T.draft(self.PLAN))))
            r = run(TICKETS, "check", str(plan_p), str(tk))
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("DRIFTED", r.stdout)
            self.assertIn("never read back", r.stdout)

    def test_nothing_filed_yet_is_not_a_fault(self):
        """A first draft has filed nothing, so no edge can be stale. Crying
        wolf here is what gets a guard deleted."""
        self.assertEqual(T.edge_problems(self.PLAN, T.draft(self.PLAN)), [])

    def test_a_malformed_read_back_is_unreadable_not_empty(self):
        bad = [None, [], {}, {"edges": {}}, {"read_at": "x"},
               {"read_at": "", "edges": {}},
               {"schema_version": 9, "read_at": "x", "edges": {}},
               {"read_at": "x", "edges": {"a": "b"}},
               {"read_at": "x", "edges": {"a": [1]}},
               {"read_at": "x", "edges": {"": []}}]
        for rb in bad:
            edges, _meta, reason = T.readback_edges(rb)
            self.assertIsNone(edges, rb)
            self.assertTrue(reason, rb)
            d = T.draft(self.PLAN, readback=rb)
            self.assertIn(d["blocked_by_sync"]["state"],
                          ("absent", "unreadable"))
            self.assertTrue(all(i["remove_blocked_by"] is None
                                for i in d["issues"]), rb)

    def test_a_named_read_back_that_cannot_be_read_refuses(self):
        """Absent is honest by accident. A human who believed they supplied a
        read-back and silently got the absent path is not being told."""
        with tempfile.TemporaryDirectory() as tmp:
            plan_p = Path(tmp) / "plan.json"
            plan_p.write_text(json.dumps(self.PLAN))
            r = run(TICKETS, "draft", str(plan_p),
                    "--tracker-edges", str(Path(tmp) / "nope.json"))
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("REFUSING", r.stdout)
            self.assertFalse((Path(tmp) / "tickets.json").exists())

    def test_a_read_back_of_the_wrong_shape_refuses_on_the_command_line(self):
        """It parses as JSON, so nothing errors; it would land in the quiet
        `unreadable` state. A human who passed the flag is told instead."""
        with tempfile.TemporaryDirectory() as tmp:
            plan_p = Path(tmp) / "plan.json"
            plan_p.write_text(json.dumps(self.PLAN))
            bad = Path(tmp) / "edges.json"
            bad.write_text(json.dumps({"edges": {"a": []}}))   # no read_at
            r = run(TICKETS, "draft", str(plan_p), "--tracker-edges", str(bad))
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("REFUSING", r.stdout)
            self.assertIn("read_at", r.stdout)
            self.assertFalse((Path(tmp) / "tickets.json").exists())

    def test_a_good_read_back_on_the_command_line_is_applied(self):
        """The counter-claim: the path that is supposed to work must work,
        end to end, through the actual CLI."""
        with tempfile.TemporaryDirectory() as tmp:
            plan_p, tk = Path(tmp) / "plan.json", Path(tmp) / "tickets.json"
            shrunk = json.loads(json.dumps(self.PLAN))
            shrunk["units"][1].pop("needs")
            plan_p.write_text(json.dumps(shrunk))
            tk.write_text(json.dumps(_filed(T.draft(self.PLAN))))
            edges = Path(tmp) / "edges.json"
            edges.write_text(json.dumps(
                _readback({"ARC-200": [], "ARC-201": ["ARC-200"]})))
            r = run(TICKETS, "draft", str(plan_p),
                    "--tracker-edges", str(edges))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("REMOVE: b <- ARC-200", r.stdout)
            written = json.loads(tk.read_text())
            by_unit = {i["unit"]: i for i in written["issues"]}
            self.assertEqual(by_unit["b"]["remove_blocked_by"], ["ARC-200"])
            self.assertEqual(written["blocked_by_sync"]["state"], "read")

    def test_a_filed_issue_omitted_from_the_read_back_is_unknown(self):
        """Omitting an edgeless issue is indistinguishable from skipping it,
        so the shape requires an empty list and this refuses to guess."""
        filed = _filed(T.draft(self.PLAN))
        second = T.draft(self.PLAN, existing=filed,
                         readback=_readback({"ARC-200": []}))
        by_unit = {i["unit"]: i for i in second["issues"]}
        self.assertIsNone(by_unit["b"]["remove_blocked_by"])
        self.assertEqual(second["blocked_by_sync"]["not_read_back"], ["b"])
        self.assertIsNone(second["blocked_by_sync"]["in_sync"])
        self.assertTrue(any("not having looked" in x for x in
                            T.edge_problems(self.PLAN, second)))

    def test_a_read_back_is_carried_forward_across_a_redraft(self):
        """Otherwise editing the plan throws away the only knowledge of the
        tracker's real edges this machine has, and every re-draft would
        re-arm the unknown."""
        filed = _filed(T.draft(self.PLAN))
        rb = _readback({"ARC-200": [], "ARC-201": ["ARC-200"]})
        second = T.draft(self.PLAN, existing=filed, readback=rb)
        third = T.draft(self.PLAN, existing=second)
        self.assertEqual(third["blocked_by_sync"]["state"], "read")
        self.assertEqual(third["blocked_by_sync"]["read_at"], rb["read_at"])

    def test_a_sync_claim_is_scoped_to_when_the_tracker_was_read(self):
        """"In sync" is a statement about a moment. A carried-forward
        read-back can be old, so the timestamp travels with the claim rather
        than being dropped once it is convenient."""
        filed = _filed(T.draft(self.PLAN))
        rb = _readback({"ARC-200": [], "ARC-201": ["ARC-200"]})
        second = T.draft(self.PLAN, existing=filed, readback=rb)
        with tempfile.TemporaryDirectory() as tmp:
            plan_p, tk = Path(tmp) / "plan.json", Path(tmp) / "tickets.json"
            plan_p.write_text(json.dumps(self.PLAN))
            tk.write_text(json.dumps(second))
            r = run(TICKETS, "check", str(plan_p), str(tk))
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn(rb["read_at"], r.stdout)

    def test_the_schema_version_moved_so_a_consumer_can_tell(self):
        """A consumer that only knows schema 1 applies `blocked_by` and never
        removes anything, which is the bug. It must be able to notice."""
        self.assertGreaterEqual(T.draft(self.PLAN)["schema_version"], 2)

    def test_a_schema_1_draft_is_not_read_as_in_sync(self):
        old = T.draft(self.PLAN)
        old = _filed(old)
        old["schema_version"] = 1
        old.pop("blocked_by_sync")
        for i in old["issues"]:
            i.pop("add_blocked_by", None)
            i.pop("remove_blocked_by", None)
            i.pop("blocked_by_in_sync", None)
        problems = T.edge_problems(self.PLAN, old)
        self.assertTrue(any("no `blocked_by_sync`" in x for x in problems),
                        problems)

    def test_the_skill_tells_the_applying_session_to_remove(self):
        """The draft is only half the interface. If the session with the
        connector is never told to call `removeBlockedBy`, the field is a
        request nobody reads -- which is the same defect one layer up."""
        skill = (ROOT / "skills" / "hanig-project" / "SKILL.md").read_text()
        self.assertIn("removeBlockedBy", skill)
        self.assertIn("remove_blocked_by", skill)
        self.assertIn("--tracker-edges", skill)
        self.assertIn("append-only", skill)

    def test_the_read_back_is_labelled_attested_not_verified(self):
        """The same care the outbox receipts needed. There is no network here,
        so a read-back is a session SAYING what the tracker holds; anyone who
        can write the file can write anything. It is a much better basis than
        diffing against the last draft -- that is a claim about our own
        intentions -- but it is not proof, and the label carries the weakness
        rather than the reader having to remember it."""
        filed = _filed(T.draft(self.PLAN))
        for rb in (None, _readback({"ARC-200": [], "ARC-201": ["ARC-200"]})):
            sync = T.draft(self.PLAN, existing=filed,
                           readback=rb)["blocked_by_sync"]
            self.assertIn("attested", sync["basis"])
            self.assertIn("not verified", sync["basis"])
        src = TICKETS.read_text()
        self.assertNotIn("verified evidence", src)

    def test_the_attestation_travels_into_what_a_human_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_p, tk = Path(tmp) / "plan.json", Path(tmp) / "tickets.json"
            plan_p.write_text(json.dumps(self.PLAN))
            rb = _readback({"ARC-200": [], "ARC-201": ["ARC-200"]})
            tk.write_text(json.dumps(
                T.draft(self.PLAN, existing=_filed(T.draft(self.PLAN)),
                        readback=rb)))
            r = run(TICKETS, "check", str(plan_p), str(tk))
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("ATTESTED", r.stdout)

    def test_it_still_talks_to_no_tracker(self):
        """The read-back arrives as a FILE. If reading the tracker back had
        been implemented by reading the tracker, the token would be back on
        the login node and the whole separation would be gone."""
        tree = ast.parse(TICKETS.read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        self.assertEqual(mods & {"urllib", "http", "requests", "socket",
                                 "httpx", "ssl"}, set())


DOCTOR = ROOT / "bin" / "doctor"


class _Health(http.server.BaseHTTPRequestHandler):
    """The one route the paseo skill documents as liveness."""
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        if self.path == "/api/health":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


@contextlib.contextmanager
def _answering_daemon():
    """A real listener on a real ephemeral port, so the daemon branch is
    exercised on a host that has never had Paseo installed."""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


@contextlib.contextmanager
def _silent_listener():
    """Accepts the connection out of the backlog and answers nothing. This is
    what a hung daemon looks like from outside, and it is the shape that has
    to be BOUNDED rather than waited on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        yield "127.0.0.1:%d" % s.getsockname()[1]
    finally:
        s.close()


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "127.0.0.1:%d" % port


class TestDoctorSeesThePrerequisitesTheSkillsRefuseWithout(unittest.TestCase):
    """ARC-265. Paseo was installed, its daemon was answering on
    127.0.0.1:6767 and ~/.paseo was populated -- and `command -v paseo` found
    nothing, because the macOS CLI lives inside the app bundle. swarm.py gates
    every kind=code unit on exactly that lookup, so the install left code
    units refused; doctor said "installed, from here, still works" throughout,
    because the only thing it had ever inspected was the skill tree. In the
    same session `./bin/bus models` failed for want of
    ~/.agent-bus/models.json, a file this repo ships at its root and installs
    nowhere.

    Both facts are simulated here through PATH, HOME, PASEO_LISTEN and
    AGENT_BUS_HOME. Nothing below asks this host whether Paseo is installed,
    so the four states are all reachable on the machine that had it and on one
    that never will."""

    # Everything doctor shells out to. The sandbox PATH is built from this
    # list alone: leaving /usr/bin on it would put this host's python3 back
    # within reach, and then the "could not determine" branch could not be
    # reached on a machine that has one.
    TOOLS = ("sh", "bash", "hostname", "git", "perl", "readlink",
             "mktemp", "sleep", "rm")

    def _bin(self, d, cli=False, python=True, claude=True, overrides=None):
        """A PATH with nothing on it but what doctor needs, so this host's own
        ~/.local/bin cannot answer a question the test is asking. `claude` is
        stubbed only to stop doctor falling back to `bash -lic`, which is slow
        and reads whatever profile the machine happens to have."""
        overrides = overrides or {}
        b = Path(d) / "bin"
        b.mkdir()
        for name in (["paseo"] if cli else []) + (["claude"] if claude else []):
            p = b / name
            p.write_text("#!/bin/sh\necho 0.0.0-test\n")
            p.chmod(0o755)
        for name in self.TOOLS:
            if name in overrides:
                p = b / name
                p.write_text(overrides[name])
                p.chmod(0o755)
                continue
            real = shutil.which(name)
            if real:
                os.symlink(real, b / name)
        if python and "python3" in overrides:
            p = b / "python3"
            p.write_text(overrides["python3"])
            p.chmod(0o755)
        elif python:
            os.symlink(sys.executable, b / "python3")
        return b

    def _doctor(self, d, cli=False, python=True, listen=None, bus_home=None,
                home=None, claude=True, overrides=None, prefix=None):
        home = Path(home) if home else Path(d) / "home"
        home.mkdir(parents=True, exist_ok=True)
        env = {"PATH": str(self._bin(d, cli, python, claude, overrides)),
               "HOME": str(home),
               "USER": os.environ.get("USER", "test"),
               "PASEO_LISTEN": listen or _closed_port()}
        if bus_home:
            env["AGENT_BUS_HOME"] = str(bus_home)
        argv = ["sh", str(DOCTOR)]
        if prefix:
            argv.extend(["--prefix", str(prefix)])
        r = subprocess.run(argv, capture_output=True,
                           text=True, cwd=ROOT, env=env, timeout=120)
        self.assertIn("=== PREREQUISITES ===", r.stdout, r.stderr)
        return r

    @staticmethod
    def _section(out):
        return out.split("=== PREREQUISITES ===", 1)[1].split("\n===", 1)[0]

    def _field(self, out, name):
        for line in self._section(out).splitlines():
            if line.strip().startswith(name + ":"):
                return line.strip()[len(name) + 1:].strip()
        self.fail("doctor printed no %r line:%s" % (name, self._section(out)))

    @staticmethod
    def _supervisor_source():
        src = DOCTOR.read_text()
        return src.split("SUPERVISOR_PERL='", 1)[1].split(
            "'\n\n# bounded SECONDS", 1)[0]

    # --- the four states, which are two facts and not one -------------------

    def test_the_cli_on_PATH_is_reported_as_the_path_it_was_found_at(self):
        with tempfile.TemporaryDirectory() as d:
            with _answering_daemon() as addr:
                out = self._doctor(d, cli=True, listen=addr).stdout
            self.assertEqual(self._field(out, "paseo"),
                             str(Path(d) / "bin" / "paseo"))
            self.assertNotIn("WARNING", self._section(out))

    def test_a_live_daemon_with_no_CLI_on_PATH_is_said_in_those_words(self):
        """The reported defect. A reinstall does not fix this and a symlink
        does, so the report has to name the symlink rather than leave a reader
        to conclude "not installed" from a CLI that is missing."""
        with tempfile.TemporaryDirectory() as d:
            with _answering_daemon() as addr:
                out = self._doctor(d, cli=False, listen=addr).stdout
            sec = self._section(out)
            self.assertRegex(self._field(out, "paseo"), r"^NOT ")
            self.assertIn("answering", self._field(out, "daemon"))
            self.assertIn("WARNING", sec)
            self.assertIn("daemon is up and the CLI is NOT on PATH", sec)
            self.assertIn("reinstalling it will not", sec)
            self.assertTrue("ln -s" in sec or "app bundle" in sec, sec)

    def test_neither_the_CLI_nor_the_daemon_is_absence_not_ignorance(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._doctor(d, cli=False).stdout
            daemon = self._field(out, "daemon")
            self.assertIn("not running", daemon)
            self.assertNotIn("COULD NOT DETERMINE", daemon)
            self.assertRegex(self._field(out, "paseo"), r"^NOT ")
            self.assertIn("kind=code units are refused", self._section(out))

    def test_a_daemon_that_cannot_be_probed_reads_unknown_never_absent(self):
        """survey.py's rule, in doctor's words: `unknown` is not `absent`, and
        it always says which look failed. Reported as absent, this sends
        someone to install what they have -- the errand ARC-244, ARC-245 and
        ARC-251 each closed somewhere else."""
        with tempfile.TemporaryDirectory() as d:
            out = self._doctor(d, cli=False, python=False).stdout
            daemon = self._field(out, "daemon")
            self.assertIn("COULD NOT DETERMINE", daemon)
            self.assertIn("python3", daemon, "unknown must say why")
            self.assertNotIn("not running", daemon)
            self.assertIn("UNKNOWN -- not absent", self._section(out))

    @staticmethod
    def _plausible_python_then_hang(marker, needle, answer):
        return ("#!/bin/sh\n"
                "if [ \"$1\" = -c ]; then\n"
                "  case \"$2\" in\n"
                "    *%s*)\n"
                "      printf '%s\\n' '%s'\n"
                "      printf invoked >%s\n"
                "      trap 'exit 0' TERM\n"
                "      while :; do sleep 10; done ;;\n"
                "  esac\n"
                "fi\n"
                "exec %s \"$@\"\n") % (
                    needle, "%s", answer, marker, sys.executable)

    def test_timed_out_health_output_cannot_report_the_daemon_up(self):
        """Retained output is evidence only when the child answered. A probe
        can print a plausible success prefix and then exceed its deadline."""
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "health-plausible-ran"
            stub = self._plausible_python_then_hang(
                marker, "socket.create_connection",
                "answering:HTTP/1.1 200 OK")
            out = self._doctor(d, cli=False,
                               overrides={"python3": stub}).stdout
            self.assertTrue(marker.exists(), "selective health stub did not run")
            daemon = self._field(out, "daemon")
            self.assertIn("COULD NOT DETERMINE", daemon)
            self.assertIn("indeterminate", daemon)
            self.assertNotIn("answering at", daemon)

    def test_the_CLI_and_the_daemon_are_never_collapsed_into_one_verdict(self):
        """A live daemon is what made the old report confident. It must not
        put a path on the CLI line."""
        with tempfile.TemporaryDirectory() as d:
            with _answering_daemon() as addr:
                out = self._doctor(d, cli=False, listen=addr).stdout
            self.assertNotRegex(self._field(out, "paseo"), r"^/")

    # --- the probe is bounded, because doctor is what you run when it hangs --

    def test_a_listener_that_never_answers_does_not_hang_doctor(self):
        with tempfile.TemporaryDirectory() as d:
            with _silent_listener() as addr:
                start = time.monotonic()
                out = self._doctor(d, cli=True, listen=addr).stdout
                elapsed = time.monotonic() - start
            daemon = self._field(out, "daemon")
            self.assertNotIn("answering", daemon)
            self.assertIn("listening", daemon)
            self.assertLess(elapsed, 60, "the health probe was not bounded")

    def test_the_probe_bounds_both_the_command_and_its_reaper(self):
        """The supervisor polls its direct child and stops polling after a
        separate reap deadline. Thus an uninterruptible child cannot hand an
        unbounded wait back to doctor."""
        src = DOCTOR.read_text()
        for guard in ("RUN_SECONDS", "PROBE_RUN_SECONDS", "REAP_SECONDS",
                      "CLOCK_MONOTONIC", "RUNNING", "TIMED_OUT", "WNOHANG",
                      'kill "TERM"', 'kill "KILL"'):
            self.assertIn(guard, src, "%s missing" % guard)

    def test_every_diagnostic_program_is_routed_through_the_deadline(self):
        src = DOCTOR.read_text()
        for invocation in (
                'bounded "$RUN_SECONDS" hostname',
                'bounded "$RUN_SECONDS" python3 --version',
                'bounded "$RUN_SECONDS" git --version',
                'bounded "$RUN_SECONDS" bash -lic',
                'bounded "$PROBE_RUN_SECONDS" python3 -c "$HEALTH_PY"',
                'bounded "$RUN_SECONDS" python3 -c "$COUNT_PY"',
                'bounded "$RUN_SECONDS" readlink "$d"',
                'bounded "$RUN_SECONDS" python3 -m py_compile "$py"',
                'bounded "$RUN_SECONDS" git -C "$REPO" rev-parse --git-dir',
                'bounded "$RUN_SECONDS" git -C "$REPO" rev-parse --short HEAD',
                'bounded "$RUN_SECONDS" git -C "$REPO" status --porcelain'):
            self.assertIn(invocation, src, invocation)

    def test_the_deadline_launches_no_unbounded_helper_programs(self):
        """ARC-271's first fix put mktemp, every polling sleep, and rm outside
        the watchdog. A hanging mktemp therefore wedged doctor before it could
        bound anything. Poison all three: doctor must finish, and none may be
        invoked. The hanging python test below proves the replacement deadline
        still runs rather than merely avoiding these helpers."""
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "unbounded-helper-ran"
            poison = ("#!/bin/sh\n"
                      "printf 'invoked' >%s\n"
                      "trap '' TERM\n"
                      "while :; do :; done\n") % marker
            start = time.monotonic()
            r = self._doctor(
                d, overrides={name: poison
                              for name in ("mktemp", "sleep", "rm")})
            self.assertLess(time.monotonic() - start, 20, r.stdout)
            self.assertFalse(marker.exists(),
                             "bounded() still invokes an unbounded helper")

    def test_a_continuously_noisy_child_cannot_starve_its_deadline(self):
        """An unbounded drain-until-EAGAIN loop can stay inside drain forever
        when several writers keep the pipe readable. Each drain quantum must
        yield to the monotonic state machine while retaining the 64 KiB tail."""
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "noisy"
            pidfile = Path(d) / "noisy.pid"
            script.write_text(
                "#!/bin/sh\n"
                "printf '%s' \"$$\" >" + str(pidfile) + "\n"
                "trap '' TERM\n"
                "writer() { while :; do printf '%01024d' 0; done; }\n"
                "writer & writer & writer & writer &\n"
                "writer & writer & writer & writer &\n"
                "wait\n")
            script.chmod(0o755)
            proc = subprocess.Popen(
                [shutil.which("perl"), "-e", self._supervisor_source(),
                 "1", "1", str(script)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            child = None
            try:
                out, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, err)
                lines = out.splitlines()
                self.assertEqual(lines[:2], ["timeout", "124"], out[:200])
                self.assertEqual(len(lines[2]), 65536,
                                 "the retained output tail is not 64 KiB")
                self.assertEqual(set(lines[2]), {"0"})
            except subprocess.TimeoutExpired:
                self.fail("continuous output starved the monotonic deadline")
            finally:
                if pidfile.exists():
                    child = int(pidfile.read_text())
                    try:
                        os.kill(-child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()

    def test_group_setup_and_wait_errors_fail_closed(self):
        """This is structural/code-inspection coverage: setpgid/waitpid
        failures are impractical to inject portably without adding a broad
        production seam. Guard ordering and classifications directly, then
        prove the handshake does not reserve a command exit status."""
        src = self._supervisor_source()
        grouped = src.index("my $grouped = setpgid(0, 0)")
        checked = src.index("unless (defined $grouped)", grouped)
        refused = src.index("_exit($SETUP_EXIT)", checked)
        ready = src.index('$write_setup->("ready")', refused)
        executed = src.index("exec {$cmd[0]}", refused)
        self.assertLess(grouped, checked)
        self.assertLess(checked, refused)
        self.assertLess(refused, executed,
                        "a diagnostic can execute after setpgid failure")
        self.assertLess(ready, executed,
                        "a diagnostic can execute without the setup handshake")
        launch_mask = src.index(
            "sigprocmask(SIG_BLOCK, $blocked_signals, $old_signal_mask)")
        chld_default = src.index('$SIG{CHLD} = "DEFAULT"', launch_mask)
        chld_failure = src.index(
            'or answer("unknown", 127, "SIGCHLD disposition")', chld_default)
        forked = src.index("my $pid = fork()", chld_default)
        self.assertLess(launch_mask, chld_default)
        self.assertLess(chld_default, chld_failure)
        self.assertLess(chld_failure, forked)
        self.assertLess(chld_default, forked,
                        "an inherited ignored SIGCHLD can auto-reap the child")
        self.assertIn('answer("setup-error", 127, $1)', src)
        self.assertNotIn("while ($got == -1 && $! == EINTR)", src)
        self.assertNotIn("while ($last == -1 && $! == EINTR)", src)
        self.assertIn("elsif ($got == -1 && $poll_errno_number != EINTR", src)
        self.assertIn("unless defined $cleanup_deadline", src)
        self.assertIn('answer("supervisor-error", 127, $wait_error)', src)
        self.assertIn("unless ($direct_reaped || defined $cleanup_deadline)",
                      src)
        timeout_start = src.index(
            'if ($state eq "RUNNING" && !$wait_error && $now >= $run_deadline)')
        timeout_end = src.index("\n    }", timeout_start)
        timeout_transition = src[timeout_start:timeout_end]
        self.assertIn("$state = \"TIMED_OUT\"", timeout_transition)
        self.assertIn("$cleanup_deadline = $grace_deadline\n"
                      "            unless defined $cleanup_deadline",
                      timeout_transition)
        self.assertIn("for my $signal_name (qw(HUP INT TERM))", src)
        self.assertIn('"interrupted by SIG$interrupted; termination was attempted"',
                      src)
        handler_start = src.index('$SIG{$signal_name} = sub {')
        handler_end = src.index("\n    };", handler_start)
        handler = src[handler_start:handler_end]
        self.assertIn("return unless $active_pid", handler)
        self.assertIn("$interrupted = $_[0]", handler)
        for forbidden in ("kill ", "waitpid(", "clock_gettime",
                          "cleanup_deadline", "answer("):
            self.assertNotIn(forbidden, handler,
                             "signal handler does more than latch state")
        poll_mask = src.index(
            "sigprocmask(SIG_BLOCK, $blocked_signals, $poll_mask)")
        latched_before_poll = src.index("if (length $interrupted)", poll_mask)
        poll_wait = src.index("my $got = waitpid", poll_mask)
        active_clear = src.index("$active_pid = 0", poll_wait)
        interrupt_clear = src.index('$interrupted = ""', active_clear)
        poll_restore = src.index(
            "sigprocmask(SIG_SETMASK, $poll_mask)", interrupt_clear)
        self.assertLess(poll_mask, latched_before_poll)
        self.assertLess(latched_before_poll, poll_wait)
        self.assertLess(poll_wait, active_clear)
        self.assertLess(active_clear, interrupt_clear)
        self.assertLess(active_clear, poll_restore)
        wait_classification = src.index("if (length $wait_error)")
        explicit_setup = src.index("if ($setup_error =~", wait_classification)
        missing_ready = src.index(
            'if ($state eq "RUNNING" && $direct_reaped', explicit_setup)
        self.assertLess(wait_classification, explicit_setup)
        self.assertLess(wait_classification, missing_ready)
        final_start = src.index("# One final nonblocking attempt only")
        final_end = src.index("\n        }\n        last;", final_start)
        final = src[final_start:final_end]
        cleanup_start = src.index(
            "if (defined($cleanup_deadline) && $now >= $cleanup_deadline)")
        group_kill = src.index(
            'kill "KILL", -$active_pid if $active_pid', cleanup_start)
        final_wait = src.index("my $last = waitpid", group_kill)
        self.assertLess(group_kill, final_wait,
                        "the group leader was reaped before final group KILL")
        self.assertNotIn('kill "KILL", -$pid',
                         src[cleanup_start:final_wait])
        self.assertEqual(final.count("waitpid("), 1,
                         "final cleanup retries a nonblocking wait")
        self.assertIn("$last_errno_number != EINTR", final)
        self.assertIn('final waitpid failed: $!', final)
        self.assertNotIn('kill "', final)
        self.assertNotIn("sigprocmask", final,
                         "a mask failure can skip the sole final reap")
        self.assertNotIn("cleanup_deadline =", final)

        r = subprocess.run(
            [shutil.which("perl"), "-e", src, "1", "1",
             shutil.which("sh"), "-c", "exit 125"],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[:3], ["answered", "125", ""])

        missing = str(Path(tempfile.gettempdir()) / "doctor-no-such-command")
        r = subprocess.run(
            [shutil.which("perl"), "-e", src, "1", "1", missing],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[:3],
                         ["setup-error", "127", "exec"])

    def test_inherited_ignored_sigchld_cannot_auto_reap_the_probe(self):
        """POSIX preserves ignored dispositions across exec. Start the Perl
        supervisor that way and prove its short child remains waitable rather
        than disappearing before the supervisor's sole-reaper state machine."""
        outer = (
            'my $program = shift @ARGV; '
            '$SIG{CHLD} = "IGNORE"; '
            'exec {$program} $program, @ARGV or die $!;')
        # Some Perl runtimes normalize SIGCHLD while starting the interpreter.
        # Seed it again at the supervisor boundary so the waitability claim is
        # exercised on every supported host, while the outer exec still models
        # the inherited-disposition launch that prompted the fix.
        inherited_source = (
            '$SIG{CHLD} = "IGNORE";\n' + self._supervisor_source())
        r = subprocess.run(
            [shutil.which("perl"), "-e", outer,
             shutil.which("perl"), "-e", inherited_source,
             "2", "1", shutil.which("sh"), "-c",
             "printf inherited-sigchld-ok"],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[:3],
                         ["answered", "0", "inherited-sigchld-ok"])

    def test_timeout_wording_claims_only_a_termination_attempt(self):
        src = DOCTOR.read_text()
        self.assertNotIn("was killed", src)
        self.assertNotIn("and was killed", src)
        self.assertIn("termination was attempted", src)

    def test_timeout_cleans_up_an_ordinary_login_profile_descendant(self):
        """bash profiles commonly start a child without changing its process
        group. The first fix killed bash alone and left that child behind.
        Group cleanup is best-effort (a hostile setsid escape is out of scope),
        but the ordinary case is both useful and runtime-testable."""
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "profile-child.pid"
            bash = ("#!/bin/sh\n"
                    "/bin/sleep 600 &\n"
                    "printf '%s\\n' \"$!\" >%s\n"
                    "trap '' TERM\n"
                    "while :; do :; done\n") % ("%s", pidfile)
            r = self._doctor(
                d, claude=False, overrides={"bash": bash})
            self.assertTrue(pidfile.exists(),
                            "the descendant-producing profile did not run")
            pid = int(pidfile.read_text())
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("login-profile descendant survived timeout")
            finally:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.assertIn("login shell timed out", r.stdout)

    def test_timeout_waits_for_grace_then_kills_group_after_parent_exits(self):
        """Regression for the timeout state machine. The direct shell exits
        when TERM arrives while its same-group child ignores TERM. Reaping the
        shell must not turn TIMED_OUT back into completion or skip the KILL at
        the one absolute grace deadline."""
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "term-ignoring-child.pid"
            bash = ("#!/bin/sh\n"
                    "(trap '' TERM; while :; do :; done) &\n"
                    "printf '%s\\n' \"$!\" >%s\n"
                    "trap 'exit 0' TERM\n"
                    "while :; do :; done\n") % ("%s", pidfile)
            start = time.monotonic()
            r = self._doctor(
                d, claude=False, overrides={"bash": bash})
            elapsed = time.monotonic() - start
            self.assertTrue(pidfile.exists(),
                            "the TERM-ignoring group child did not run")
            pid = int(pidfile.read_text())
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("group child survived the grace-deadline KILL")
            finally:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.assertGreaterEqual(
                elapsed, 4.5, "supervisor returned when the parent was reaped")
            self.assertLess(elapsed, 15, "grace deadline did not bound timeout")
            self.assertIn("claude: COULD NOT DETERMINE", r.stdout)
            self.assertIn("login shell timed out", r.stdout)

    def test_timed_out_group_leader_reserves_its_pid_until_group_kill(self):
        """After TERM the direct parent exits while a same-group child ignores
        it. The parent must remain an unreaped zombie until the absolute grace
        deadline, reserving the PID/PGID that the final negative-PID KILL uses."""
        with tempfile.TemporaryDirectory() as d:
            leader_file = Path(d) / "leader.pid"
            child_file = Path(d) / "child.pid"
            script = Path(d) / "term-parent"
            script.write_text(
                "#!/bin/sh\n"
                "(trap '' HUP INT TERM; while :; do :; done) &\n"
                "printf '%s' \"$!\" >" + str(child_file) + "\n"
                "printf '%s' \"$$\" >" + str(leader_file) + "\n"
                "trap 'exit 0' TERM\n"
                "while :; do :; done\n")
            script.chmod(0o755)
            started = time.monotonic()
            proc = subprocess.Popen(
                [shutil.which("perl"), "-e", self._supervisor_source(),
                 "1", "3", str(script)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True)
            leader = child = None
            try:
                deadline = started + 3
                while time.monotonic() < deadline:
                    if leader_file.exists() and child_file.exists():
                        leader = int(leader_file.read_text())
                        child = int(child_file.read_text())
                        state = subprocess.run(
                            ["ps", "-o", "stat=", "-p", str(leader)],
                            capture_output=True, text=True).stdout.strip()
                        if state.startswith("Z"):
                            break
                    time.sleep(0.02)
                else:
                    self.fail("TERM-exited group leader was reaped before grace")
                self.assertLess(time.monotonic() - started, 3.5)
                out, err = proc.communicate(timeout=4)
                self.assertEqual(proc.returncode, 0, err)
                self.assertEqual(out.splitlines()[:2], ["timeout", "124"])
                self.assertGreaterEqual(time.monotonic() - started, 3.5)
                gone = time.monotonic() + 3
                while time.monotonic() < gone:
                    try:
                        os.kill(child, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("TERM-ignoring group child survived final KILL")
            finally:
                if leader is not None:
                    try:
                        os.kill(-leader, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()

    def test_interrupt_signals_clean_up_the_isolated_probe_group(self):
        """The supervisor receives the interruption, not its isolated probe.
        Each supported signal must trigger real group cleanup and a bounded
        supervisor answer; merely installing handlers would make this vacuous."""
        for signum, signame in ((signal.SIGHUP, "HUP"),
                                (signal.SIGINT, "INT"),
                                (signal.SIGTERM, "TERM")):
            with self.subTest(signal=signame), tempfile.TemporaryDirectory() as d:
                leader_file = Path(d) / "leader.pid"
                child_file = Path(d) / "child.pid"
                script = Path(d) / "ignore-signals"
                script.write_text(
                    "#!/bin/sh\n"
                    "trap '' HUP INT TERM\n"
                    "(trap '' HUP INT TERM; while :; do :; done) &\n"
                    "printf '%s' \"$!\" >" + str(child_file) + "\n"
                    "printf '%s' \"$$\" >" + str(leader_file) + "\n"
                    "while :; do :; done\n")
                script.chmod(0o755)
                proc = subprocess.Popen(
                    [shutil.which("perl"), "-e", self._supervisor_source(),
                     "30", "2", str(script)], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, start_new_session=True)
                leader = child = None
                try:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if leader_file.exists() and child_file.exists():
                            leader = int(leader_file.read_text())
                            child = int(child_file.read_text())
                            break
                        time.sleep(0.02)
                    else:
                        self.fail("signal-ignoring probe group did not start")
                    started = time.monotonic()
                    # Repeated delivery must not move the first interruption's
                    # absolute cleanup deadline. An implementation that resets
                    # it on every signal takes well over this test's bound.
                    for _ in range(5):
                        os.kill(proc.pid, signum)
                        time.sleep(0.3)
                    out, err = proc.communicate(timeout=3)
                    self.assertLess(time.monotonic() - started, 2.8)
                    self.assertEqual(proc.returncode, 0, err)
                    self.assertEqual(out.splitlines()[:2],
                                     ["supervisor-error", "127"])
                    self.assertIn("interrupted by SIG" + signame, out)
                    for pid in (leader, child):
                        gone = time.monotonic() + 3
                        while time.monotonic() < gone:
                            try:
                                os.kill(pid, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.02)
                        else:
                            self.fail("%s left probe pid %s alive" %
                                      (signame, pid))
                finally:
                    if leader is not None:
                        try:
                            os.kill(-leader, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.communicate()

    def test_near_deadline_interrupt_keeps_its_first_cleanup_deadline(self):
        """An interrupt within one reap interval of the run deadline arms
        cleanup first. Crossing the run deadline may latch TIMED_OUT, but must
        not replace that earlier absolute cleanup deadline with timeout grace."""
        with tempfile.TemporaryDirectory() as d:
            leader_file = Path(d) / "leader.pid"
            script = Path(d) / "ignore-term"
            script.write_text(
                "#!/bin/sh\n"
                "trap '' HUP INT TERM\n"
                "printf '%s' \"$$\" >" + str(leader_file) + "\n"
                "while :; do :; done\n")
            script.chmod(0o755)
            proc = subprocess.Popen(
                [shutil.which("perl"), "-e", self._supervisor_source(),
                 "4", "2", str(script)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True)
            leader = None
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if leader_file.exists():
                        leader = int(leader_file.read_text())
                        break
                    time.sleep(0.02)
                else:
                    self.fail("signal-ignoring probe did not start")

                # Signal 1.5 seconds before the run deadline: its two-second
                # grace expires about 2 seconds from here. The faulty timeout
                # overwrite instead returns about 3.5 seconds from here.
                time.sleep(2.5)
                started = time.monotonic()
                os.kill(proc.pid, signal.SIGTERM)
                out, err = proc.communicate(timeout=3)
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 1.7)
                self.assertLess(elapsed, 2.7,
                                "timeout moved the interruption deadline")
                self.assertEqual(proc.returncode, 0, err)
                self.assertEqual(out.splitlines()[:2],
                                 ["supervisor-error", "127"])
                self.assertIn("interrupted by SIGTERM", out)
                with self.assertRaises(ProcessLookupError):
                    os.kill(leader, 0)
            finally:
                if leader is not None:
                    try:
                        os.kill(-leader, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()

    def test_timeout_does_not_wait_for_a_descendant_that_calls_setsid(self):
        """Arbitrary-grandchild quiescence is outside the declared boundary.
        A child that deliberately leaves the dedicated group can survive, but
        it cannot extend the supervisor's absolute grace deadline."""
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "setsid-child.pid"
            code = ("use POSIX qw(setsid); setsid(); "
                    "open(my $f, q(>), q(%s)) or die $!; "
                    "print $f $$; close $f; sleep 600") % pidfile
            bash = ("#!/bin/sh\n"
                    "perl -e '%s' &\n"
                    "trap 'exit 0' TERM\n"
                    "while :; do :; done\n") % code
            start = time.monotonic()
            r = self._doctor(
                d, claude=False, overrides={"bash": bash})
            elapsed = time.monotonic() - start
            self.assertTrue(pidfile.exists(), "the setsid child did not run")
            pid = int(pidfile.read_text())
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                self.fail("the supervisor claimed quiescence outside its group")
            finally:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.assertLess(elapsed, 15, "a setsid escape held doctor open")
            self.assertIn("login shell timed out", r.stdout)

    @staticmethod
    def _hanging_probe(marker, when, delegate):
        return """#!/bin/sh
if %s; then
  printf invoked >%s
  trap 'exit 0' TERM
  while :; do sleep 10; done
fi
exec %s "$@"
""" % (when, marker, delegate)

    def test_a_hung_python_version_is_timeout_not_absence(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "python-version-ran"
            stub = self._hanging_probe(
                marker, '[ "$1" = --version ]', sys.executable)
            out = self._doctor(d, overrides={"python3": stub}).stdout
            self.assertTrue(marker.exists(), "the hanging stub was not invoked")
            self.assertIn("python3: TIMEOUT", out)
            self.assertNotIn("python3: ABSENT", out)

    def test_a_hung_git_version_is_timeout_not_absence(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "git-version-ran"
            stub = self._hanging_probe(
                marker, '[ "$1" = --version ]', shutil.which("git"))
            out = self._doctor(d, overrides={"git": stub}).stdout
            self.assertTrue(marker.exists(), "the hanging stub was not invoked")
            self.assertIn("git: TIMEOUT", out)
            self.assertNotIn("git: ABSENT", out)

    def test_a_hung_login_shell_is_unknown_not_claude_absence(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "login-shell-ran"
            stub = self._hanging_probe(marker, "true", shutil.which("bash"))
            out = self._doctor(
                d, claude=False, overrides={"bash": stub}).stdout
            self.assertTrue(marker.exists(), "the hanging stub was not invoked")
            self.assertIn("claude: COULD NOT DETERMINE", out)
            self.assertIn("login shell timed out", out)
            self.assertNotIn("claude: ABSENT", out)

    def test_the_bounded_login_fallback_still_finds_cluster_claude(self):
        with tempfile.TemporaryDirectory() as d:
            bash = """#!/bin/sh
printf 'profile chatter that is not the answer\n'
printf 'found:/cluster/profile/bin/claude\n'
"""
            out = self._doctor(
                d, claude=False, overrides={"bash": bash}).stdout
            self.assertIn(
                "claude: /cluster/profile/bin/claude (login shell only)", out)

    def test_a_successful_silent_compiler_is_an_answer_not_a_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "skills"
            script = prefix / "example" / "scripts" / "ok.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('valid')\n")
            r = self._doctor(d, prefix=prefix)
            self.assertIn("ok    example/ok.py", r.stdout)
            self.assertNotIn("TIMEOUT  example/ok.py", r.stdout)
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_nonzero_compiler_is_a_syntax_failure_not_a_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "skills"
            script = prefix / "example" / "scripts" / "bad.py"
            script.parent.mkdir(parents=True)
            script.write_text("def nope(:\n")
            r = self._doctor(d, prefix=prefix)
            self.assertIn("FAIL  example/bad.py does not compile", r.stdout)
            self.assertNotIn("TIMEOUT  example/bad.py", r.stdout)
            self.assertNotEqual(r.returncode, 0)

    def test_a_slow_valid_compiler_is_timeout_not_a_syntax_failure(self):
        """The public deadline is a classification boundary, not a claim the
        valid work would never finish. Slow valid work is indeterminate and
        makes doctor nonzero; it is neither absent nor a syntax failure."""
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "compiler-ran"
            prefix = Path(d) / "skills"
            script = prefix / "example" / "scripts" / "ok.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('valid')\n")
            stub = ("#!/bin/sh\n"
                    "if [ \"$1\" = -m ] && [ \"$2\" = py_compile ]; then\n"
                    "  printf invoked >%s\n"
                    "  sleep 30\n"
                    "fi\n"
                    "exec %s \"$@\"\n") % (marker, sys.executable)
            r = self._doctor(d, overrides={"python3": stub}, prefix=prefix)
            self.assertTrue(marker.exists(), "the slow compiler was not invoked")
            self.assertIn("TIMEOUT  example/ok.py", r.stdout)
            self.assertNotIn("FAIL  example/ok.py does not compile", r.stdout)
            self.assertNotEqual(r.returncode, 0)

    # --- the registry bus models refuses without -----------------------------

    def _registry(self, where, text=None):
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(text if text is not None
                         else (ROOT / "models.json").read_text())
        return where

    def test_timed_out_registry_output_cannot_report_a_model_count(self):
        """A plausible retained count from a child that never completed is
        indeterminate, not an answered registry inspection."""
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "registry-plausible-ran"
            home = Path(d) / "home"
            reg = self._registry(home / ".agent-bus" / "models.json")
            stub = self._plausible_python_then_hang(
                marker, "json.loads", "ok:999")
            out = self._doctor(d, cli=True, home=home,
                               overrides={"python3": stub}).stdout
            self.assertTrue(marker.exists(),
                            "selective registry stub did not run")
            field = self._field(out, "models registry")
            self.assertIn(str(reg), field)
            self.assertIn("CONTENTS COULD NOT BE DETERMINED", field)
            self.assertNotIn("999 models", field)

    def test_a_present_registry_is_reported_with_what_is_in_it(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            reg = self._registry(home / ".agent-bus" / "models.json")
            out = self._doctor(d, cli=True, home=home).stdout
            field = self._field(out, "models registry")
            self.assertIn(str(reg), field)
            n = len(json.loads((ROOT / "models.json").read_text())["models"])
            self.assertIn("(%d models)" % n, field)

    def test_an_absent_registry_names_the_copy_this_repo_already_ships(self):
        """`bus models` dies without it, skills are told to run `bus models
        --json` to route, and the file that belongs there is in this
        checkout. Saying only "missing" would leave the reader to find that
        out."""
        with tempfile.TemporaryDirectory() as d:
            out = self._doctor(d, cli=True).stdout
            sec, field = self._section(out), self._field(out, "models registry")
            self.assertIn("NOT PRESENT", field)
            self.assertIn(".agent-bus/models.json", field)
            self.assertIn(str(ROOT / "models.json"), sec)

    def test_an_unreadable_registry_is_unknown_and_not_missing(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything, so nothing is unreadable")
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            reg = self._registry(home / ".agent-bus" / "models.json")
            reg.chmod(0o000)
            try:
                field = self._field(self._doctor(d, cli=True, home=home).stdout,
                                    "models registry")
            finally:
                reg.chmod(0o600)
            self.assertIn("COULD NOT DETERMINE", field)
            self.assertNotIn("NOT PRESENT", field)

    def test_a_registry_that_will_not_parse_is_not_reported_as_working(self):
        """`bus models` fails on a corrupt file exactly as it fails on no
        file: load_models_registry swallows JSONDecodeError and returns []."""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            self._registry(home / ".agent-bus" / "models.json", "{oops")
            field = self._field(self._doctor(d, cli=True, home=home).stdout,
                                "models registry")
            self.assertIn("UNUSABLE", field)

    def test_the_registry_is_looked_for_where_bus_would_look_for_it(self):
        """bin/bus honours AGENT_BUS_HOME, so a report about ~/.agent-bus on a
        host that sets it would be a fact about a path nothing reads."""
        with tempfile.TemporaryDirectory() as d:
            elsewhere = Path(d) / "bus-elsewhere"
            reg = self._registry(elsewhere / "models.json")
            field = self._field(self._doctor(d, cli=True,
                                             bus_home=elsewhere).stdout,
                                "models registry")
            self.assertIn(str(reg), field)

    # --- diagnosing is not deploying ----------------------------------------

    def test_the_installer_still_writes_nothing_outside_the_skill_prefix(self):
        """ARC-265 deliberately stopped at diagnosis. install.sh has just been
        made careful about what it claims to own (ARC-257), and writing into
        ~/.agent-bus or ~/.local/bin is a wider footprint than installing
        skills. If that is ever revisited it should be revisited on purpose,
        which is what this test makes it."""
        code = [l for l in (ROOT / "install.sh").read_text().splitlines()
                if not l.lstrip().startswith("#")]
        for path in (".agent-bus", ".local/bin"):
            self.assertEqual([l for l in code if path in l], [],
                             "install.sh reaches into %s" % path)

if __name__ == "__main__":
    unittest.main(verbosity=2)
