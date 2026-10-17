"""Pin local-by-default verification through the operator's real CLI entry point."""

from contextlib import redirect_stderr, redirect_stdout
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

    def verify(self):
        before = len(self.remote.rows())
        argv = [str(self.f.operator), str(self.f.plan_path),
                "--state-dir", str(self.f.state_dir), "--unit", "u", "--pr", "7",
                "--approver", "Operator", "--verify-integration"]
        stdout, stderr = io.StringIO(), io.StringIO()
        calls = []
        popen = subprocess.Popen

        def record(command, *args, **kwargs):
            argv = [command] if isinstance(command, str) else list(command)
            calls.append(argv)
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
            return popen(command, *args, **kwargs)

        with mock.patch.dict(os.environ, self.f.env, clear=True), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "path", [str(self.f.operator.parent)] + sys.path), \
                mock.patch.object(S.U, "run", wraps=S.U.run) as runner, \
                mock.patch.object(subprocess, "Popen", side_effect=record), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exit_result:
                runpy.run_path(str(self.f.operator), run_name="__main__")
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


if __name__ == "__main__":
    unittest.main()
