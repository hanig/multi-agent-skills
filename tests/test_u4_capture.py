"""Calibrate shared U4 capture, not a global production-path oracle.

Contract: tests/fixtures/d5/u4_capture.py. This unit changes only that fixture
and its tests, removing test_u4_preactivation_baseline.py. The sample inactive
path below is independent before/after source, not a production coverage claim.
The two equivalent rewrites reproduce U4.0b's sbatch argument-variable and
Popen(args=argv) counterexamples without modifying any production source.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/fixtures/d5"))
sys.path.insert(0, str(ROOT / "skills/hanig-swarm/scripts"))
from u4_capture import FilesystemRecorder, SubprocessRecorder, TrackerIntentRecorder
import swarm_outbox as SO


INACTIVE = '''\
def inactive(output, launch, enqueue, guard, premature=False):
    if premature:
        guard()
    launch(["sbatch", "--parsable", "job.sh"])
    (output / "record").write_bytes(b"recorded\\n")
    enqueue()
'''

POSITIONAL = '''\
def launch(argv, options):
    with subprocess.Popen(argv, **options) as child:
        out, err = child.communicate(timeout=10)
        if child.returncode:
            raise AssertionError((child.returncode, out, err))
        return out
'''


def function(source, name):
    namespace = {"subprocess": subprocess}
    exec(compile(source, "<u4-capture-example>", "exec"), namespace)
    return namespace[name]


class EarlyActivation(AssertionError):
    pass


def deny_activation():
    raise EarlyActivation("inactive path attempted D5 activation")


class TestCapture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # A disposable local program; PATH never falls through to real Slurm.
        sbatch = self.bin / "sbatch"
        sbatch.write_text("import json, sys\n"
                          "print(json.dumps(sys.argv[1:]))\n")
        self.sbatch_argv = [sys.executable, str(sbatch)]
        self.options = dict(cwd=str(self.output), env={"PATH": str(self.bin)},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def enqueue(self):
        # The real offline enqueue writer; no tracker connection or drain.
        with mock.patch.object(SO.time, "strftime", return_value="fixture-clock"):
            key = SO.emit_intent(str(self.output), "fixture-project", "unit",
                                 "SUBMITTED", {"job_id": "123"}, kind="slurm")
        self.assertIsNotNone(key)

    def observe(self, source=INACTIVE, launcher=POSITIONAL, premature=False):
        # Reuse identical paths, resetting inputs before each side, not
        # normalizing nondeterminism out of the observations afterwards.
        shutil.rmtree(self.output)
        self.output.mkdir()
        processes = SubprocessRecorder()
        files = FilesystemRecorder(self.output)
        intents = TrackerIntentRecorder(self.output / SO.OUTBOX)
        outputs = []
        launch_impl = function(launcher, "launch")

        def launch(argv):
            outputs.append(launch_impl(self.sbatch_argv + argv[1:], self.options))

        error = None
        with processes.capture():
            try:
                function(source, "inactive")(
                    self.output, launch, self.enqueue, deny_activation, premature)
            except EarlyActivation as exc:
                error = str(exc)
        return dict(commands=processes.commands, files=files.snapshot(),
                    intents=intents.snapshot(), outputs=outputs, error=error)

    def assert_equivalent(self, before, after):
        self.assertIsNone(before["error"])
        self.assertEqual(before, after)

    def test_real_subprocess_capture_and_native_interfaces(self):
        original = subprocess.Popen
        for api in ("popen-positional", "popen-keyword", "run-positional", "run-keyword"):
            with self.subTest(api=api):
                recorder = SubprocessRecorder()
                argv = self.sbatch_argv + ["--parsable", "job.sh"]
                expected = tuple(os.fsencode(arg) for arg in argv)
                with recorder.capture():
                    if api.startswith("popen"):
                        child = (subprocess.Popen(argv, **self.options)
                                 if api.endswith("positional") else
                                 subprocess.Popen(args=argv, **self.options))
                        with child:
                            stdout, stderr = child.communicate(timeout=10)
                            self.assertEqual(child.returncode, 0)
                            self.assertEqual(stderr, b"")
                    else:
                        child = (subprocess.run(argv, timeout=10, check=True, **self.options)
                                 if api.endswith("positional") else
                                 subprocess.run(args=argv, timeout=10, check=True, **self.options))
                        stdout = child.stdout
                self.assertIs(subprocess.Popen, original)
                self.assertEqual(json.loads(stdout), ["--parsable", "job.sh"])
                self.assertEqual(recorder.commands, [
                    (os.fsencode(sys.executable), expected)])
                argv.append("later caller edit")
                self.assertEqual(recorder.commands[0][1], expected)

    def test_capture_consumes_generator(self):
        """Mutation capture-consumes-generator: native argv runs exactly once."""
        argv = [sys.executable, "-c", 'print("fixture")']
        expected = tuple(os.fsencode(arg) for arg in argv)
        native = subprocess.run((arg for arg in argv), check=True, timeout=10,
                                **self.options)
        self.assertEqual(native.stdout, b"fixture\n")
        for api in ("popen-positional", "popen-keyword", "run-positional", "run-keyword"):
            with self.subTest(api=api):
                recorder = SubprocessRecorder()
                generated = (arg for arg in argv)
                with recorder.capture():
                    if api.startswith("popen"):
                        child = (subprocess.Popen(generated, **self.options)
                                 if api.endswith("positional") else
                                 subprocess.Popen(args=generated, **self.options))
                        with child:
                            stdout, stderr = child.communicate(timeout=10)
                            self.assertEqual(child.returncode, native.returncode)
                    else:
                        child = (subprocess.run(generated, check=True, timeout=10,
                                                **self.options)
                                 if api.endswith("positional") else
                                 subprocess.run(args=generated, check=True, timeout=10,
                                                **self.options))
                        stdout, stderr = child.stdout, child.stderr
                self.assertEqual((stdout, stderr), (native.stdout, native.stderr))
                self.assertEqual(recorder.commands, [(expected[0], expected)])

    def test_fixture_shebang_whitespace(self):
        """Mutation fixture-shebang-whitespace: use installed Python via argv."""
        executable = self.root / "interpreter with whitespace" / "python3"
        executable.parent.mkdir()
        executable.symlink_to(sys.executable)
        native = subprocess.run([str(executable), "-c", 'print("fixture")'],
                                check=True, capture_output=True, timeout=10)
        self.assertEqual(native.stdout, b"fixture\n")
        fixture = TestCapture()
        self.addCleanup(fixture.doCleanups)
        with mock.patch.object(sys, "executable", str(executable)):
            fixture.setUp()
            observed = fixture.observe()
            fixture.test_equivalent_sbatch_variable_and_popen_keyword_refactors()
        expected = tuple(os.fsencode(arg) for arg in
                         [str(executable), str(fixture.bin / "sbatch"),
                          "--parsable", "job.sh"])
        self.assertEqual(observed["commands"], [(os.fsencode(executable), expected)])
        self.assertEqual(observed["outputs"], [b'["--parsable", "job.sh"]\n'])
        self.assertEqual(observed["files"]["record"], b"recorded\n")
        self.assertEqual(len(observed["intents"]), 1)
        self.assertIsNone(observed["error"])

    def test_program_override_and_argument_containers(self):
        recorder = SubprocessRecorder()
        program = Path(sys.executable)
        script = self.bin / "sbatch"
        with recorder.capture():
            for argv in (["display-name", str(script), "job.sh"],
                         (b"display-name", script, Path("job.sh"))):
                result = subprocess.run(argv, executable=program, check=True,
                                        timeout=10, **self.options)
                self.assertEqual(json.loads(result.stdout), ["job.sh"])
                self.assertIs(result.args, argv)
        expected = (os.fsencode(program), (b"display-name", os.fsencode(script), b"job.sh"))
        self.assertEqual(recorder.commands, [expected, expected])

    def test_scalar_argument_forms_retain_native_semantics(self):
        for argv in (sys.executable, os.fsencode(sys.executable), Path(sys.executable)):
            with self.subTest(argv=argv):
                native = subprocess.run(argv, input=b"", check=True, timeout=10,
                                        **self.options)
                recorder = SubprocessRecorder()
                with recorder.capture():
                    observed = subprocess.run(argv, input=b"", check=True, timeout=10,
                                              **self.options)
                self.assertIs(observed.args, argv)
                self.assertEqual((observed.returncode, observed.stdout, observed.stderr),
                                 (native.returncode, native.stdout, native.stderr))
                expected = os.fsencode(argv)
                self.assertEqual(recorder.commands, [(expected, (expected,))])

    def test_shell_refusal_and_exception_restore_native_popen(self):
        original = subprocess.Popen
        recorder = SubprocessRecorder()
        with self.assertRaisesRegex(ValueError, "direct argv"):
            with recorder.capture():
                subprocess.Popen("exit 0", shell=True)
        self.assertIs(subprocess.Popen, original)
        self.assertEqual(recorder.commands, [])
        with self.assertRaises(FileNotFoundError):
            with recorder.capture():
                subprocess.Popen([str(self.root / "missing")])
        self.assertIs(subprocess.Popen, original)
        self.assertEqual(recorder.commands, [])

    def test_filesystem_bytes_overwrite_and_deletion(self):
        recorder = FilesystemRecorder(self.output)
        self.assertEqual(recorder.snapshot(), {})
        path = self.output / "nested" / "payload"
        path.parent.mkdir()
        path.write_bytes(b"first\x00\xff\n")
        before = recorder.snapshot()
        self.assertEqual(before, {"nested/payload": b"first\x00\xff\n"})
        path.write_bytes(b"changed\n")
        self.assertEqual(recorder.snapshot(), {"nested/payload": b"changed\n"})
        path.unlink()
        self.assertEqual(recorder.snapshot(), {})
        self.assertEqual(before, {"nested/payload": b"first\x00\xff\n"})

    def test_filesystem_refuses_symlink_without_following(self):
        (self.output / "link").symlink_to(self.bin, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            FilesystemRecorder(self.output).snapshot()

    def test_real_tracker_enqueue_captures_exact_appended_bytes(self):
        path = self.output / SO.OUTBOX
        recorder = TrackerIntentRecorder(path)
        self.assertEqual(recorder.snapshot(), ())
        self.enqueue()
        captured = recorder.snapshot()
        self.assertEqual(captured, (path.read_bytes(),))
        intent = json.loads(captured[0])
        self.assertEqual((intent["verb"], intent["unit"], intent["job_id"]),
                         ("start", "unit", "123"))
        self.assertEqual(intent["at"], "fixture-clock")
        # A real idempotent re-enqueue appends nothing, regardless of calls.
        later = TrackerIntentRecorder(path)
        with mock.patch.object(SO.time, "strftime", return_value="fixture-clock"):
            self.assertIsNone(SO.emit_intent(str(self.output), "fixture-project",
                                           "unit", "SUBMITTED", {"job_id": "123"},
                                           kind="slurm"))
        self.assertEqual(later.snapshot(), ())
        self.assertEqual(recorder.snapshot(), captured)
        # Reader preserves order, duplicate lines and bytes, not JSON semantics.
        with path.open("ab") as stream:
            stream.write(captured[0] + b'{"note": "different"}\n')
        self.assertEqual(later.snapshot(), (captured[0], b'{"note": "different"}\n'))
        path.write_bytes(b"rewritten\n")
        with self.assertRaisesRegex(ValueError, "rewritten"):
            later.snapshot()

    def test_equivalent_sbatch_variable_and_popen_keyword_refactors(self):
        before = self.observe()
        self.assertEqual(len(before["commands"]), 1)
        self.assertEqual(before["files"]["record"], b"recorded\n")
        self.assertEqual(len(before["intents"]), 1)
        variable = INACTIVE.replace(
            '    launch(["sbatch", "--parsable", "job.sh"])',
            '    sbatch_argv = ["sbatch", "--parsable", "job.sh"]\n'
            '    launch(sbatch_argv)')
        keyword = POSITIONAL.replace("Popen(argv,", "Popen(args=argv,")
        self.assertNotEqual(variable, INACTIVE)
        self.assertNotEqual(keyword, POSITIONAL)
        for source, launcher in ((INACTIVE, POSITIONAL), (variable, POSITIONAL),
                                 (INACTIVE, keyword), (variable, keyword)):
            with self.subTest(variable=source == variable, keyword=launcher == keyword):
                self.assert_equivalent(before, self.observe(source, launcher))

    def test_capture_ignores_argv_change(self):
        """Mutation capture-ignores-argv-change: altered argv must differ."""
        before = self.observe()
        changed = INACTIVE.replace('"--parsable"', '"--changed"')
        after = self.observe(changed)
        # Compare the subprocess channel itself, independent of echoed stdout.
        self.assertNotEqual(before["commands"], after["commands"])
        with self.assertRaises(AssertionError):
            self.assert_equivalent(before, after)

    def test_changed_file_and_intent_are_not_equivalent(self):
        before = self.observe()
        changed = INACTIVE.replace('b"recorded', 'b"different')
        after = self.observe(changed)
        self.assertNotEqual(before["files"], after["files"])
        with self.assertRaises(AssertionError):
            self.assert_equivalent(before, after)
        with mock.patch.object(self, "enqueue", side_effect=lambda: SO.emit_intent(
                str(self.output), "fixture-project", "unit", "SUBMITTED",
                {"job_id": "changed"}, kind="slurm")):
            after = self.observe()
        self.assertNotEqual(before["intents"], after["intents"])
        with self.assertRaises(AssertionError):
            self.assert_equivalent(before, after)

    def test_capture_misses_early_activation(self):
        """Mutation capture-misses-early-activation: denial before all effects."""
        before = self.observe()
        after = self.observe(premature=True)
        self.assertEqual(after["error"], "inactive path attempted D5 activation")
        self.assertEqual(after["commands"], [])
        self.assertEqual(after["files"], {})
        self.assertEqual(after["intents"], ())
        self.assertEqual(after["outputs"], [])
        with self.assertRaises(AssertionError):
            self.assert_equivalent(before, after)


if __name__ == "__main__":
    unittest.main()
