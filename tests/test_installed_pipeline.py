"""Hermetic installed-copy workflow acceptance; the test is the trusted grader.

Only coordinator status and the installed report determine the run's grade.
The scripted worker and its stdout supply no verdict. See workflow-acceptance.md.
"""
import ast
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "installed_pipeline"
SKILLS = ("hanig-project", "hanig-swarm")
OUTPUTS = ["result.txt", "details.txt"]


def outside_checkout_temp():
    """Keep the project/store outside the forbidden source even with TMPDIR."""
    errors = []
    for parent in (Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")):
        resolved = parent.resolve()
        if resolved == ROOT or ROOT in resolved.parents:
            continue
        try:
            return tempfile.TemporaryDirectory(prefix="installed-pipeline-", dir=parent)
        except OSError as exc:
            errors.append(str(exc))
    raise OSError("no writable temporary parent outside checkout: " + "; ".join(errors))


class InstalledPipeline(unittest.TestCase):
    def setUp(self):
        self.temp = outside_checkout_temp()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.project = self.base / "separate project"
        self.bin = self.base / "bin"
        self.traces = self.base / "traces"
        for path in (self.home, self.project, self.bin, self.traces):
            path.mkdir()
        self.store = self.home / ".agents" / "skills"
        self.env = {
            "HOME": str(self.home), "PATH": str(self.bin),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "TMPDIR": str(self.base), "LC_ALL": "C",
        }
        # Same closed executable / REPLACED PATH seam as
        # test_swarm.py::_fake_scheduler (the current tree has no fake agent
        # launcher there). A pipeline needs only a foreground worker and sh.
        (self.bin / "sh").symlink_to("/bin/sh")
        (self.bin / "sleep").symlink_to("/bin/sleep")
        worker = self.bin / "fixture-worker"
        shutil.copyfile(FIXTURES / "worker.sh", worker)
        worker.chmod(0o755)
        source = self.base / "source checkout"
        shutil.copytree(ROOT / "lib", source / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for name in SKILLS:
            shutil.copytree(ROOT / "skills" / name, source / "skills" / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        # Use the real installer and its dependency validation/lifecycle, with
        # an explicit offline destination. No discovery of the host's agents.
        installed = self.command([
            sys.executable, "-I", "-B", str(source / "lib" / "skill_installer.py"),
            "--prefix", str(self.store), "--mode", "copy", "--json",
            "--only", "hanig-project", "--only", "hanig-swarm"])
        self.assertEqual(installed.returncode, 0, installed.stderr + installed.stdout)
        self.assertEqual({p.name for p in self.store.iterdir() if p.is_dir()}, set(SKILLS))
        for entry in self.store.iterdir():
            if not entry.is_dir():
                self.assertRegex(entry.name, r"^\.multi-agent-skills-[0-9a-f]{20}\.lock$")
        self.assertFalse(any(p.is_symlink() for p in self.store.rglob("*")))
        shutil.rmtree(source)
        self.assertFalse(source.exists())

        # -I -S exclude ambient Python paths, site configuration and user site;
        # no PYTHONPATH is supplied. Children use the same guarded interpreter.
        runner = self.base / "python_runner.py"
        shutil.copyfile(FIXTURES / "python_runner.py", runner)
        python = self.bin / "python3"
        python.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) +
                          " -I -S -B " + shlex.quote(str(runner)) + ' "$@"\n')
        python.chmod(0o755)
        self.env.update({
            "PIPELINE_FORBIDDEN": json.dumps([str(ROOT), str(source)]),
            "PIPELINE_PYTHON": str(python), "PIPELINE_TRACES": str(self.traces),
        })
        self.select_store(self.store)

    def select_store(self, store):
        self.store = store
        self.env.update({
            "PIPELINE_STORE": str(store),
            "HANIG_SWARM_DIR": str(store / "hanig-swarm"),
            "HANIG_PROJECT_DIR": str(store / "hanig-project"),
        })

    def command(self, argv):
        return subprocess.run(argv, cwd=self.project, env=self.env,
                              capture_output=True, text=True, timeout=15)

    def cli(self, skill, script, *args, allowed=(0,)):
        # Expand the documented skill-directory variable in a real shell,
        # preserving the unrelated project cwd and paths containing spaces.
        variable = "HANIG_" + skill.upper() + "_DIR"
        result = self.command([
            "/bin/sh", "-c", 'exec python3 "$' + variable +
            '/scripts/' + script + '" "$@"', "installed-cli", *args])
        self.assertIn(result.returncode, allowed, result.stdout + result.stderr)
        return result

    def exercise(self, mode, on_observation=None):
        plan = {"name": "installed acceptance", "units": [{
            "id": "work", "kind": "pipeline", "runtime": "none",
            "command": "fixture-worker " + mode, "outputs": OUTPUTS,
            "max_attempts": 1,
        }]}
        (self.project / "plan.json").write_text(json.dumps(plan))
        self.cli("swarm", "swarm.py", "validate", "plan.json")
        self.cli("swarm", "swarm.py", "run", "plan.json")
        # Wait only on coordinator observations. A bounded loop allows the
        # detached shell to finish without a race-prone fixed sleep.
        deadline = time.monotonic() + 10
        while True:
            self.cli("swarm", "swarm.py", "advance", "plan.json", allowed=(0, 2))
            status = json.loads(self.cli(
                "swarm", "swarm.py", "status", "plan.json", "--json",
                allowed=(0, 2)).stdout)
            self.assertEqual(len(status["units"]), 1)
            unit = status["units"][0]
            report = json.loads(self.cli("project", "report.py", ".", "--json").stdout)
            self.assertEqual(len(report["units"]), 1)
            notes = " ".join(report["units"][0]["notes"])
            if on_observation is not None:
                on_observation(status, report)
            # With ps intentionally absent, a still-starting wrapper can be
            # INCOMPLETE because liveness is unknown. That is not a terminal
            # observation: wait for the report to record the wrapper's exit.
            if "engine exited" in notes:
                break
            self.assertLess(time.monotonic(), deadline, (status, report))
            time.sleep(0.02)
        self.assertEqual(unit["id"], "work")
        self.assertEqual(unit["attempts"], 1, status)
        # Both human and machine reports go through the installed sibling.
        self.cli("project", "report.py", ".", "--out", "report.html")
        self.assertTrue((self.project / "report.html").is_file())
        self.assertEqual(report["units"][0]["id"], "work")
        self.assertEqual(report["units"][0]["attempts"], 1)
        # These receipt fields are consumed through the pipeline's report,
        # not read from engine.rc or the worker's own summary by this grader.
        notes = " ".join(report["units"][0]["notes"])
        self.assertIn("engine exited 0", notes, report)
        self.assert_installed_modules()
        return status, report

    def assert_installed_modules(self):
        traces = [json.loads(p.read_text()) for p in self.traces.glob("*.json")]
        self.assertTrue(traces)
        scripts = set()
        for trace in traces:
            self.assertTrue(trace["source_guard_exercised"])
            self.assertTrue(trace["proc_guard_exercised"])
            modules = trace["modules"]
            self.assertTrue(Path(modules["__main__"]).is_relative_to(self.store))
            scripts.add(Path(modules["__main__"]).name)
            for name in ("unit", "swarm", "worktree", "coordinator_paths", "skill_paths"):
                if name in modules:
                    self.assertTrue(Path(modules[name]).is_relative_to(self.store), modules)
        self.assertEqual(scripts, {"swarm.py", "unit.py", "report.py"})
        report_trace = next(t for t in traces if Path(t["modules"]["__main__"]).name == "report.py")
        for name in ("swarm", "unit", "skill_paths"):
            self.assertIn(name, report_trace["modules"])

    def grade_hollow(self, status, report):
        self.assertNotEqual(status["units"][0]["state"], "DONE", "hollow coordinator reached DONE")
        self.assertNotEqual(report["units"][0]["state"], "DONE", "hollow report reached DONE")
        self.assertNotEqual(report["verdict"], "COMPLETE")
        self.assertEqual(report["units"][0]["missing_outputs"], sorted(OUTPUTS))

    def test_000_hollow_worker_is_not_done(self):
        self.grade_hollow(*self.exercise("hollow"))

    def test_010_honest_worker_is_done(self):
        status, report = self.exercise("honest")
        self.assertEqual(status["units"][0]["state"], "DONE", status)
        self.assertEqual(report["units"][0]["state"], "DONE", report)
        self.assertEqual(report["verdict"], "COMPLETE", report)
        self.assertEqual(set(report["units"][0]["outputs"]), set(OUTPUTS))
        self.assertFalse(report["units"][0]["missing_outputs"])

    def ignore_missing_outputs(self, predicate):
        tree = ast.parse(predicate.read_text())
        functions = [node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "_pipeline_state"]
        self.assertEqual(len(functions), 1, "pipeline function not found or ambiguous")
        function = functions[0]
        parameters = {arg.arg for arg in function.args.posonlyargs +
                      function.args.args + function.args.kwonlyargs}

        def returns(statement, state):
            return (isinstance(statement, ast.Return)
                    and isinstance(statement.value, ast.Constant)
                    and statement.value.value == state)

        def appends_note(statement):
            call = statement.value if isinstance(statement, ast.Expr) else None
            return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "append" and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in parameters)

        # Select the callee's refusal decision, independently of call syntax
        # and local names. Changed control-flow shape is an explicit refusal.
        guards = [node for index, node in enumerate(function.body)
                  if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                  and node.test.id in parameters and not node.orelse
                  and any(returns(item, "INCOMPLETE") for item in node.body)
                  and any(appends_note(item) for item in node.body)
                  and any(returns(item, "DONE") for item in function.body[index + 1:])]
        self.assertTrue(guards, "missing-output guard not identified")
        self.assertEqual(len(guards), 1, "ambiguous missing-output guards")
        guards[0].test = ast.Constant(value=False)
        predicate.write_text(ast.unparse(tree) + "\n")

    def test_020_missing_output_mutation_is_caught_by_hollow_grader(self):
        mutant = self.home / "mutant skills"
        shutil.copytree(self.store, mutant)
        predicate = mutant / "hanig-swarm" / "scripts" / "unit.py"
        pristine = self.store / "hanig-swarm" / "scripts" / "unit.py"
        original = pristine.read_bytes()
        before = ast.parse(original)
        self.ignore_missing_outputs(predicate)
        after = ast.parse(predicate.read_text())
        # Independently restore the one disabled condition in the observed
        # mutant AST; the complete executable tree must then match the input.
        function_index = next(i for i, node in enumerate(before.body)
                              if isinstance(node, ast.FunctionDef)
                              and node.name == "_pipeline_state")
        function = after.body[function_index]
        original_function = before.body[function_index]
        self.assertEqual(len(function.body), len(original_function.body))
        disabled = [(i, node) for i, node in enumerate(function.body)
                    if isinstance(node, ast.If) and isinstance(node.test, ast.Constant)
                    and node.test.value is False and isinstance(original_function.body[i], ast.If)
                    and ast.dump(node.test) != ast.dump(original_function.body[i].test)]
        self.assertEqual(len(disabled), 1)
        index, guard = disabled[0]
        guard.test = before.body[function_index].body[index].test
        self.assertEqual(ast.dump(after), ast.dump(before), "mutation changed other executable code")
        self.select_store(mutant)
        status, report = self.exercise("hollow")
        self.assertEqual(status["units"][0]["state"], "DONE", status)
        self.assertEqual(report["units"][0]["state"], "DONE", report)
        self.assertEqual(report["units"][0]["missing_outputs"], sorted(OUTPUTS))
        with self.assertRaisesRegex(AssertionError, "hollow coordinator reached DONE"):
            self.grade_hollow(status, report)
        self.assertEqual(pristine.read_bytes(), original)

    def test_030_mutation_survives_formatting_and_local_name_changes(self):
        reformatted = self.home / "reformatted skills"
        shutil.copytree(self.store, reformatted)
        predicate = reformatted / "hanig-swarm" / "scripts" / "unit.py"
        tree = ast.parse(predicate.read_text())
        check = next(n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "check_unit")
        callee = next(n for n in tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == "_pipeline_state")
        parameters = [arg.arg for arg in callee.args.args]
        role = next(node.test.id for node in callee.body
                    if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                    and node.test.id in parameters)
        calls = [node for node in ast.walk(check)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "_pipeline_state"]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        position = parameters.index(role)
        actual = (call.args[position] if position < len(call.args) else
                  next(keyword.value for keyword in call.keywords if keyword.arg == role))
        self.assertIsInstance(actual, ast.Name)
        local_name = actual.id
        for node in ast.walk(check):
            if isinstance(node, ast.Name) and node.id == local_name:
                node.id = "refactored_" + local_name
        for node in ast.walk(callee):
            if isinstance(node, ast.arg) and node.arg == role:
                node.arg = "refactored_" + role
            if isinstance(node, ast.Name) and node.id == role:
                node.id = "refactored_" + role
        # This fixture accepts already-keyword calls too; the production
        # mutation itself never examines or binds a call site's arguments.
        call.keywords = [ast.keyword(arg=name, value=value)
                         for name, value in zip(parameters[2:], call.args[2:])] + call.keywords
        for keyword in call.keywords:
            if keyword.arg == role:
                keyword.arg = "refactored_" + role
        call.args = call.args[:2]
        # Parenthesized definitions/calls permit a newline after the opening
        # parenthesis. Only the disposable store gets this harmless refactor.
        predicate.write_text(ast.unparse(tree).replace(
            "_pipeline_state(", "_pipeline_state(\n    ") + "\n")
        self.select_store(reformatted)
        self.test_020_missing_output_mutation_is_caught_by_hollow_grader()

    def test_040_starting_worker_is_not_graded_as_finished(self):
        release = self.base / "release worker"
        self.env["PIPELINE_RELEASE"] = str(release)
        observations = []

        def release_after_first_observation(status, report):
            if not observations:
                self.assertEqual(status["units"][0]["state"], "INCOMPLETE")
                self.assertNotIn("engine exited", " ".join(report["units"][0]["notes"]))
                release.touch()
            observations.append(status["units"][0]["state"])

        try:
            self.grade_hollow(*self.exercise("hollow", release_after_first_observation))
        finally:
            # Also release on an assertion failure; the fixture itself has
            # a bounded timeout if the test process is killed.
            release.touch()
        self.assertGreaterEqual(len(observations), 2)

    def test_050_tmpdir_inside_checkout_still_runs_an_external_honest_project(self):
        with tempfile.TemporaryDirectory(prefix="pipeline-temp-parent-", dir=ROOT) as parent:
            # A fresh interpreter avoids tempfile's process-wide cached parent.
            # Its HOME remains disposable; only this owned temp parent is
            # created in the checkout and it is removed on every exit path.
            result = subprocess.run([
                sys.executable, "-I", "-S", str(Path(__file__).resolve()),
                "InstalledPipeline.test_010_honest_worker_is_done", "-v"],
                cwd=self.project, env=dict(self.env, TMPDIR=parent),
                capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(Path(parent).iterdir()), [])

    def test_060_unknown_or_ambiguous_mutation_target_is_refused_without_writing(self):
        original = (self.store / "hanig-swarm" / "scripts" / "unit.py").read_text()
        for variant, message in (("renamed", "pipeline function not found"),
                                 ("absent", "missing-output guard not identified"),
                                 ("duplicate", "ambiguous missing-output guards")):
            with self.subTest(variant=variant):
                tree = ast.parse(original)
                function = next(n for n in tree.body
                                if isinstance(n, ast.FunctionDef) and n.name == "_pipeline_state")
                if variant == "renamed":
                    function.name = "renamed_pipeline_state"
                else:
                    guard = next(n for n in function.body
                                 if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                                 and isinstance(n.body[-1], ast.Return)
                                 and isinstance(n.body[-1].value, ast.Constant)
                                 and n.body[-1].value.value == "INCOMPLETE")
                    if variant == "absent":
                        function.body.remove(guard)
                    else:
                        function.body.insert(function.body.index(guard), guard)
                predicate = self.home / (variant + ".py")
                predicate.write_text(ast.unparse(tree) + "\n")
                unchanged = predicate.read_bytes()
                with self.assertRaisesRegex(AssertionError, message):
                    self.ignore_missing_outputs(predicate)
                self.assertEqual(predicate.read_bytes(), unchanged)


if __name__ == "__main__":
    unittest.main()
