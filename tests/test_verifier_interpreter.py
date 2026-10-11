"""The native verifier's PATH, not the launcher, determines implicit Python."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                       "skills" / "hanig-swarm" / "scripts"))
import verify as V


class TestImplicitInterpreter(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cwd = self.root / "candidate"
        self.cwd.mkdir()
        self.program = self.root / "check"
        self.program.write_text('#!/usr/bin/env python3\n'
                                'import os\nprint(os.path.abspath(os.environ["ACTUAL_PYTHON"]))\n')
        self.executables = V.RV.resolve_executables()

    def python(self, directory, version):
        directory.mkdir(exist_ok=True)
        path = directory / "python3"
        path.write_text('#!' + sys.executable + '\nimport os, sys\n'
                        'if sys.argv[1:] == ["--version"]:\n'
                        '    print(%r)\n    raise SystemExit(0)\n'
                        'os.environ["ACTUAL_PYTHON"] = __file__\n'
                        'os.execv(%r, [%r] + sys.argv[1:])\n'
                        % ('Python ' + version, sys.executable, sys.executable))
        path.chmod(0o755)
        return path

    def run_verifier(self, path):
        with mock.patch.dict(os.environ, PATH=path):
            outcome, error = V.run_pinned(
                None, self.program, V.digest_file(self.program)[0], cwd=self.cwd,
                observe_completion=True, executables=self.executables)
        self.assertIsNone(error, error)
        return outcome

    def test_first_child_path_match_is_recorded_and_executed(self):
        first = self.python(self.root / "first", "3.9.6")
        second = self.python(self.root / "second", "3.14.8")
        outcome = self.run_verifier(str(first.parent) + os.pathsep + str(second.parent))
        self.assertEqual(V.outcome_result(outcome), {"result": "pass"})
        self.assertEqual(outcome["stdout"].strip(), str(first))
        self.assertEqual(outcome["execution"]["executables"]["python"]["path"], str(first))
        refused = self.run_verifier(str(second.parent) + os.pathsep + str(first.parent))
        self.assertEqual(V.outcome_result(refused)["result"], "incomplete")
        self.assertIn(str(second), refused["incomplete_reason"])
        # Each receipt owns its observation, even when tool configuration is reused.
        self.assertEqual(outcome["execution"]["executables"]["python"]["path"], str(first))
        self.assertEqual(self.executables["python"]["role"], "launcher")

    def test_relative_and_empty_path_entries_use_child_cwd(self):
        for entry in ("bin", ""):
            with self.subTest(entry=entry):
                selected = self.python(self.cwd / entry, "3.9.6")
                outcome = self.run_verifier(entry)
                self.assertEqual(V.outcome_result(outcome), {"result": "pass"})
                self.assertEqual(outcome["stdout"].strip(), str(selected))
                self.assertEqual(outcome["execution"]["executables"]["python"]["path"],
                                 str(selected))

    def test_missing_python_is_incomplete_not_a_candidate_failure(self):
        empty = self.root / "empty"
        empty.mkdir()
        outcome = self.run_verifier(str(empty))
        self.assertEqual(V.outcome_result(outcome)["result"], "incomplete")
        self.assertIsNone(outcome["exit_code"])
        self.assertIn("python3", outcome["incomplete_reason"])
        self.assertIn("supported range", outcome["incomplete_reason"])


if __name__ == "__main__":
    unittest.main()
