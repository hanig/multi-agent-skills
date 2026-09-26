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


class InstalledPipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="installed-pipeline-")
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
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "_pipeline_state"]
        self.assertEqual(len(calls), 1, "pipeline mutation target is ambiguous")
        self.assertEqual(len(calls[0].args), 5, "pipeline call signature changed")
        self.assertFalse(calls[0].keywords)
        calls[0].args[3] = ast.List(elts=[], ctx=ast.Load())
        predicate.write_text(ast.unparse(tree) + "\n")

    def test_020_missing_output_mutation_is_caught_by_hollow_grader(self):
        mutant = self.home / "mutant skills"
        shutil.copytree(self.store, mutant)
        predicate = mutant / "hanig-swarm" / "scripts" / "unit.py"
        self.ignore_missing_outputs(predicate)
        self.select_store(mutant)
        status, report = self.exercise("hollow")
        self.assertEqual(status["units"][0]["state"], "DONE", status)
        self.assertEqual(report["units"][0]["state"], "DONE", report)
        with self.assertRaisesRegex(AssertionError, "hollow coordinator reached DONE"):
            self.grade_hollow(status, report)

    def test_030_mutation_survives_formatting_and_local_name_changes(self):
        reformatted = self.home / "reformatted skills"
        shutil.copytree(self.store, reformatted)
        predicate = reformatted / "hanig-swarm" / "scripts" / "unit.py"
        tree = ast.parse(predicate.read_text())
        check = next(n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "check_unit")
        renamed = 0
        for node in ast.walk(check):
            if isinstance(node, ast.Name) and node.id == "missing":
                node.id = "absent_outputs"
                renamed += 1
        self.assertGreater(renamed, 0)
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


if __name__ == "__main__":
    unittest.main()
