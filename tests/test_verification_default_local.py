"""Pin local-by-default verification through the operator's real CLI entry point."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from tests import test_remote_verification as fixtures

S, RV = fixtures.S, fixtures.RV
TRANSPORT = re.compile(
    r"(?:^|[\s'\";&|()<>`])(?:[^\s'\";&|()<>`]+/)?"
    r"(?:ssh|scp|sbatch|srun)(?=$|[\s'\";&|()<>`])")


class TestVerificationDefaultLocal(unittest.TestCase):
    def setUp(self):
        # Composition reuses the real Git/verifier/forge and SSH fixtures without
        # rediscovering their test methods in this module.
        self.remote = fixtures.TestRemoteVerification()
        self.addCleanup(self.remote.doCleanups)
        self.remote.setUp()
        self.f = self.remote.f
        self.policy_path = self.f.state_dir / RV.POLICY
        self.policy_path.unlink()

    def check_effective_launch(self, argv, options, calls):
        """Permit the measured fixture launch types; do not interpret shell text.

        This observes this process's Popen boundary, not arbitrary descendants.
        Git, the forge shim, and the pinned fixture verifier remain trusted.
        """
        reason = "unexpected effective launch"
        self.assertFalse(options.get("shell"), reason)
        cwd = Path(options.get("cwd") or Path.cwd())
        if not cwd.is_absolute():
            cwd = Path.cwd() / cwd
        program = os.fsdecode(options.get("executable") or argv[0])
        if os.path.dirname(program):
            executable = Path(program)
            if not executable.is_absolute():
                executable = cwd / executable
        else:
            executable = None
            for entry in os.get_exec_path(options.get("env")):
                candidate = cwd / entry / program
                if candidate.is_file() and os.access(str(candidate), os.X_OK):
                    executable = candidate
                    break
        calls.append([str(executable)])
        git = Path(shutil.which("git", path=os.defpath))
        git_paths = {self.f.bin / "git", (self.f.bin / "git").resolve(), git}
        env = Path(shutil.which("env", path=os.defpath))
        pythons = {Path(sys.executable), Path(sys.executable).resolve()}
        allowed = git_paths | {self.f.bin / "gh", env, self.f.bin / "ssh"} | pythons
        self.assertIn(executable, allowed, reason)
        if executable == env:
            # The only env launch here is isolated Git: -u names and assignments,
            # followed by an absolute Git path. Unknown wrappers fail closed.
            tail = list(argv[1:])
            while tail:
                if tail[0] == "-u" and len(tail) >= 2:
                    self.assertRegex(tail[1], r"^[A-Za-z_][A-Za-z_0-9]*$", reason)
                    tail = tail[2:]
                elif re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tail[0]):
                    tail = tail[1:]
                else:
                    break
            self.assertTrue(tail, reason)
            self.assertIn(Path(tail[0]), git_paths, reason)
        if executable in pythons:
            self.assertIn(Path(argv[0]), pythons, reason)
            if argv[1:] == ["--version"]:
                return executable
            scope = [S.__file__, "scope-check", str(self.f.plan_path),
                     "--state-dir", str(self.f.state_dir), "--unit", "u", "--json"]
            if argv[1:] == scope:
                return executable
            # Pin the measured launcher, not arbitrary Python -c code. Its final
            # executable must contain the existing fixture's verifier bytes.
            self.assertEqual(len(argv), 11, reason)
            self.assertEqual(argv[1:4], ["-I", "-S", "-c"], reason)
            self.assertEqual(hashlib.sha256(argv[4].encode()).hexdigest(),
                             "bd86553d09b8cab6ddb53a32057c4df146d9317827af141fa6584811ee3faf2d",
                             reason)
            self.assertTrue(argv[5].isdigit(), reason)
            self.assertEqual(Path(argv[6]), Path(argv[9]).parent / "bin", reason)
            self.assertIn(Path(argv[7]), git_paths, reason)
            self.assertEqual(argv[8], "", reason)
            self.assertEqual(argv[9], argv[10], reason)
            self.assertEqual(Path(argv[9]).read_bytes(), self.f.verifier_bytes, reason)

        return executable

    def verify(self):
        before = len(self.remote.rows())
        argv = [str(self.f.operator), str(self.f.plan_path),
                "--state-dir", str(self.f.state_dir), "--unit", "u", "--pr", "7",
                "--approver", "Operator", "--verify-integration"]
        stdout, stderr = io.StringIO(), io.StringIO()
        calls = []
        popen = subprocess.Popen
        signature = inspect.signature(popen)
        violations = []

        def record(command, *args, **kwargs):
            argv = [command] if isinstance(command, str) else list(command)
            calls.append(argv)
            try:
                # A shell or env wrapper must not hide an absolute transport path.
                # The direct fixture transport below is the only allowed launch.
                nested = argv if isinstance(command, str) else argv[1:]
                for argument in nested:
                    self.assertNotRegex(str(argument), TRANSPORT, "nested transport launch")
                # Even a regressed default may only reach the existing local shim.
                if Path(argv[0]).name in ("ssh", "scp", "sbatch", "srun"):
                    executable = shutil.which(argv[0], path=self.f.env["PATH"])
                    self.assertIsNotNone(executable, argv)
                    self.assertEqual(Path(executable).resolve(),
                                     self.f.bin / Path(argv[0]).name, argv)
                options = signature.bind(command, *args, **kwargs).arguments
                self.check_effective_launch(argv, options, calls)
            except Exception as exc:
                violations.append(str(exc))
                raise
            return popen(command, *args, **kwargs)

        with mock.patch.dict(os.environ, self.f.env, clear=True), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "path", [str(self.f.operator.parent)] + sys.path), \
                mock.patch.object(S.U, "run", wraps=S.U.run) as runner, \
                mock.patch.object(subprocess, "Popen", side_effect=record), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                with self.assertRaises(SystemExit) as exit_result:
                    runpy.run_path(str(self.f.operator), run_name="__main__")
            finally:
                self.assertFalse(violations, "; ".join(violations))
        self.assertEqual(exit_result.exception.code, 0, stdout.getvalue() + stderr.getvalue())
        runner.assert_called()
        # Keep every runner call, plus direct subprocess calls (transport and
        # verifier observation bypass the injected Git runner).
        calls.extend(list(call.args[0]) for call in runner.call_args_list)
        self.assertTrue(calls)
        rows = self.remote.rows()[before:]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result"], "pass")
        self.assertEqual(rows[0]["subject_head"], self.f.head)
        self.assertEqual(self.remote.merges(), [])
        return rows[0]["execution"], calls

    def assert_local(self):
        execution, calls = self.verify()
        self.assertEqual(execution["location"], "local")
        self.assertEqual(execution["executor"], "direct")
        for command in calls:
            self.assertNotIn(Path(command[0]).name, ("ssh", "scp"), calls)
            for argument in command:
                self.assertNotRegex(str(argument), TRANSPORT, calls)
                self.assertNotIn("sbatch", str(argument), calls)
                self.assertNotIn("srun", str(argument), calls)
        self.assertFalse((self.f.state_dir / "remote-verifications").exists())
        self.assertFalse(Path(self.f.env["REMOTE_LOG"]).exists())
        self.assertFalse(Path(self.f.env["SCHED_LOG"]).exists())
        return execution

    def test_absent_policy_stays_local(self):
        self.assertFalse(self.policy_path.exists())
        self.assert_local()
        self.assertFalse(self.policy_path.exists())

    def test_empty_policy_matches_absent_policy(self):
        # Compare both operator runs: an absent-file default mutation must also
        # break the claim that the explicit empty policy behaves identically.
        absent = self.assert_local()
        self.policy_path.write_text(json.dumps({"schema_version": 1}))
        empty = self.assert_local()
        self.assertEqual(empty, absent)

    def test_declared_host_launches_through_fake_transport(self):
        self.remote.save_policy()
        execution, calls = self.verify()
        self.assertEqual(execution["location"], "remote")
        self.assertEqual(execution["executor"], "direct")
        self.assertTrue(any(Path(command[0]).name == "ssh" for command in calls), calls)
        self.assertEqual(self.remote.ssh_launches(), 1)
        self.assertTrue(list((self.f.state_dir / "remote-verifications").glob("*.json")))

    def test_nested_transports_are_refused_before_shell_launch(self):
        original_run = S.U.run
        for program in ("ssh", "scp", "sbatch", "srun"):
            for shell_string in (False, True):
                with self.subTest(program=program, shell_string=shell_string):
                    witness = self.f.directory / (program + str(shell_string))
                    # Existing fixture programs (or absent fixture paths), never
                    # real clients, also keep the guard-removal mutation safe.
                    command = "printf reached > {}; {} --version".format(
                        shlex.quote(str(witness)), shlex.quote(str(self.f.bin / program)))
                    invoked = []

                    def inject(*args, **kwargs):
                        if not invoked:
                            invoked.append(True)
                            subprocess.run(command if shell_string else ["/bin/sh", "-c", command],
                                           shell=shell_string, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
                        return original_run(*args, **kwargs)

                    with mock.patch.object(S.U, "run", inject), \
                            self.assertRaisesRegex(AssertionError, "nested transport launch"):
                        self.verify()
                    self.assertTrue(invoked)
                    self.assertFalse(witness.exists(), "shell ran before the guard refused it")


    def assert_injected_launch_refused(self, command, positional=(), swallow=False,
                                      process_cwd=None, **options):
        original_run = S.U.run
        invoked = []
        witness = Path(self.f.env["REMOTE_LOG"])
        if witness.exists():
            witness.unlink()

        def inject(*args, **kwargs):
            if not invoked:
                invoked.append(True)
                previous = Path.cwd()
                try:
                    if process_cwd is not None:
                        os.chdir(str(process_cwd))
                    subprocess.run(command, *positional, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   **options)
                except AssertionError:
                    if not swallow:
                        raise
                finally:
                    os.chdir(str(previous))
            return original_run(*args, **kwargs)

        with mock.patch.object(S.U, "run", inject), \
                self.assertRaisesRegex(AssertionError, "unexpected effective launch"):
            self.verify()
        self.assertTrue(invoked)
        self.assertFalse(witness.exists(), "copied transport ran before refusal")

    def copied_transport(self):
        directory = self.f.directory / "alternate" / "bin"
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("ssh", "probe", "git"):
            shutil.copy2(str(self.f.bin / "ssh"), str(directory / name))
        return directory, ["-oBatchMode=yes", "-oConnectTimeout=15", "fixture-host", "true"]

    def test_expanded_shell_is_refused_before_launch(self):
        directory, arguments = self.copied_transport()
        command = 'p={}; q=pro; r=be; "$p$q$r" {}'.format(
            shlex.quote(str(directory) + "/"), shlex.join(arguments))
        for shell in (False, True):
            with self.subTest(shell=shell):
                self.assert_injected_launch_refused(
                    command if shell else ["/bin/sh", "-c", command], shell=shell)

    def test_effective_environment_is_used(self):
        directory, arguments = self.copied_transport()
        self.assert_injected_launch_refused(
            ["ssh"] + arguments, env=dict(self.f.env, PATH=str(directory)))
        self.assert_injected_launch_refused(
            ["ssh"] + arguments, env=dict(self.f.env, PATH="bin"), cwd=directory.parent,
            process_cwd=self.f.directory)

    def test_effective_executable_is_used(self):
        directory, arguments = self.copied_transport()
        argv = [str(self.f.bin / "git")] + arguments
        with self.subTest(form="keyword"):
            self.assert_injected_launch_refused(argv, executable=str(directory / "probe"))
        with self.subTest(form="positional"):
            self.assert_injected_launch_refused(argv, positional=(-1, str(directory / "probe")))
        with self.subTest(form="relative-cwd"):
            self.assert_injected_launch_refused(argv, executable="./git", cwd=directory,
                                               process_cwd=self.f.bin)

    def test_wrappers_and_aliases_are_refused(self):
        directory, arguments = self.copied_transport()
        alias = directory / "gh"
        alias.symlink_to(directory / "probe")
        commands = [[str(alias)] + arguments,
                    [str(directory / "git")] + arguments,
                    [shutil.which("env", path=os.defpath), "-u", "UNUSED",
                     str(directory / "probe")] + arguments,
                    [sys.executable, str(directory / "probe")] + arguments]
        for command in commands:
            with self.subTest(command=command[0]):
                self.assert_injected_launch_refused(command)

    def test_local_checks_effective_transport_with_neutral_argv(self):
        original_run = S.U.run
        invoked = []

        def inject(*args, **kwargs):
            if not invoked:
                invoked.append(True)
                # The existing shim rejects this before logging. Only the
                # effective-executable record can expose it to assert_local.
                subprocess.run([str(self.f.bin / "git"), "invalid-fixture-args"],
                               executable=str(self.f.bin / "ssh"),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return original_run(*args, **kwargs)

        with mock.patch.object(S.U, "run", inject), self.assertRaisesRegex(AssertionError, "ssh"):
            self.assert_local()
        self.assertTrue(invoked)
        self.assertFalse(Path(self.f.env["REMOTE_LOG"]).exists())

    def test_changed_python_launcher_is_refused(self):
        directory, _arguments = self.copied_transport()
        verifier = self.f.directory / "verifier"
        verifier.write_bytes(self.f.verifier_bytes)
        verifier.chmod(0o755)
        launcher = next(value for value in fixtures.V._observe_execution.__code__.co_consts
                        if isinstance(value, str) and value.startswith("import os, sys\nfd ="))
        argv = [sys.executable, "-I", "-S", "-c", launcher, "1", str(self.f.bin),
                str((self.f.bin / "git").resolve()), "", str(verifier), str(verifier)]
        changes = [{1: "-s"}, {11: "extra"}, {4: launcher + "\n# changed"},
                   {5: "invalid"}, {6: str(directory)},
                   {7: str(directory / "probe")}, {8: str(directory / "probe")},
                   {10: str(directory / "probe")},
                   {6: str(directory / "bin"), 9: str(directory / "probe"),
                    10: str(directory / "probe")}]
        for change in changes:
            with self.subTest(positions=sorted(change)):
                modified = list(argv)
                for index, value in change.items():
                    if index == len(modified):
                        modified.append(value)
                    else:
                        modified[index] = value
                self.assert_injected_launch_refused(modified)

    def test_refusal_cannot_be_swallowed(self):
        directory, arguments = self.copied_transport()
        self.assert_injected_launch_refused([str(directory / "probe")] + arguments, swallow=True)


if __name__ == "__main__":
    unittest.main()
