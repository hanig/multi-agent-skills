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

    def python(self, directory, version, version_info=None, response=None):
        directory.mkdir(exist_ok=True)
        path = directory / "python3"
        path.write_text('#!' + sys.executable + '\nimport os, sys\n'
                        'if sys.argv[1:] == ["--version"]:\n'
                        '    print(%r)\n    raise SystemExit(0)\n'
                        'if sys.argv[1:4] == ["-I", "-S", "-c"]:\n'
                        '    if %r is not None:\n'
                        '        sys.stdout.write(%r)\n        raise SystemExit(0)\n'
                        '    sys.version_info = %r\n'
                        '    exec(sys.argv[4])\n    raise SystemExit(0)\n'
                        'os.environ["ACTUAL_PYTHON"] = __file__\n'
                        'os.execv(%r, [%r] + sys.argv[1:])\n'
                        % ('Python ' + version, response, response,
                           version_info or tuple(map(int, version.split('.'))),
                           sys.executable, sys.executable))
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

    def test_non_python_shebang_does_not_acquire_python_provenance(self):
        self.program.write_text('#!/bin/sh\nprintf "shell ran\\n"\n')
        for version in ('3.9.6', '3.14.8'):
            with self.subTest(version=version):
                unrelated = self.python(self.root / 'unrelated', version)
                outcome = self.run_verifier(str(unrelated.parent))
                self.assertEqual(V.outcome_result(outcome), {'result': 'pass'})
                self.assertEqual(outcome['stdout'], 'shell ran\n')
                receipt = dict(outcome, claim=V.INTEGRATION_CLAIM, result='pass')
                problem = V.RV.execution_problem(receipt)
                self.assertIsNotNone(problem)
                self.assertIn('unknown interpreter provenance', problem)
                self.assertEqual(outcome['execution']['executables']['python']['role'], 'launcher')

    def test_supported_version_suffixes_preserve_major_minor_micro(self):
        for version, expected in (('3.11.4+', '3.11.4'), ('3.12.0rc1', '3.12.0'),
                                  ('3.12.0a1+', '3.12.0')):
            with self.subTest(version=version):
                selected = self.python(self.root / 'source-build', version,
                                       version_info=tuple(map(int, expected.split('.'))))
                outcome = self.run_verifier(str(selected.parent))
                self.assertEqual(V.outcome_result(outcome), {'result': 'pass'})
                self.assertEqual(outcome['stdout'].strip(), str(selected))
                self.assertEqual(outcome['execution']['executables']['python']['version'], expected)

    def test_machine_version_is_independent_of_display_banner(self):
        for banner in ('3.9.18\n[PyPy build details]', 'not a version banner at all'):
            with self.subTest(banner=banner):
                selected = self.python(self.root / 'runtime-query', banner, version_info=(3, 9, 18))
                outcome = self.run_verifier(str(selected.parent))
                self.assertEqual(V.outcome_result(outcome), {'result': 'pass'})
                self.assertEqual(outcome['execution']['executables']['python']['version'], '3.9.18')

    def test_malformed_runtime_versions_are_incomplete(self):
        for response in ('', 'null', '{}', '[3,9]', '[3,9,6,0]', '[true,9,6]',
                         '[3,"9",6]', '[3,9.0,6]', '[-3,9,6]', '[3,9,6]\n[3,9,6]'):
            with self.subTest(response=response):
                selected = self.python(self.root / 'malformed-query', '3.9.6', response=response)
                outcome = self.run_verifier(str(selected.parent))
                self.assertEqual(V.outcome_result(outcome)['result'], 'incomplete')
                self.assertIsNone(outcome['exit_code'])
                self.assertEqual(outcome['stdout'], '')

    def test_supported_minor_boundaries(self):
        for version, result in (('3.8.0', 'pass'), ('3.12.99', 'pass'),
                                ('3.7.99', 'incomplete'), ('3.13.0', 'incomplete')):
            with self.subTest(version=version):
                selected = self.python(self.root / 'boundary-query', version)
                outcome = self.run_verifier(str(selected.parent))
                self.assertEqual(V.outcome_result(outcome)['result'], result)

    def test_probe_skips_startup_hooks_but_native_verifier_keeps_them(self):
        site = self.root / 'site'
        site.mkdir()
        counter = self.root / 'startup-count'
        (site / 'sitecustomize.py').write_text(
            'from pathlib import Path\np = Path(%r)\n'
            'p.write_text(str(int(p.read_text()) + 1) if p.exists() else "1")\n'
            % str(counter))
        (self.cwd / 'json.py').write_text('raise RuntimeError("candidate json was imported")\n')
        self.program.write_text('#!/usr/bin/env python3\nprint("native verifier")\n')
        native = self.root / 'native'
        native.mkdir()
        (native / 'python3').symlink_to(sys.executable)
        with mock.patch.dict(os.environ, PYTHONPATH=str(site)):
            outcome = self.run_verifier(str(native))
        self.assertEqual(V.outcome_result(outcome), {'result': 'pass'})
        self.assertEqual(outcome['stdout'], 'native verifier\n')
        self.assertEqual(counter.read_text(), '1')


if __name__ == "__main__":
    unittest.main()
