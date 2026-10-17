"""Default-local routing through merge_unit's real --verify-integration entry.

Observe remote_verify.run_remote, remote_verify.coordinator_ssh and
remote_verify.remote_call, plus verify.run_pinned and verify._observe_execution
at the local verifier spawn. The existing fake transport executes the positive
control; the observers forward to the real functions without replacing routing.

This test proves the coordinator does not select the remote verification path by default. It does not prove that no process can reach a network.
"""

from contextlib import redirect_stderr, redirect_stdout
import inspect
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

from tests import test_remote_verification as fixtures

S, V, RV = fixtures.S, fixtures.V, fixtures.RV


class TestMergeVerificationRouting(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="verification-routing-home-")
        self.addCleanup(home.cleanup)
        # Keep PATH only for fixture tool discovery; the fixture replaces it
        # with its own bin before running the operator. All other inherited
        # variables (HANIG_*, SWARM_*, XDG_*, GIT_*, PYTHON*, provider and
        # coordinator variables included) are discarded before fixture setup.
        environment = mock.patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": home.name}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.transport = fixtures.TestRemoteVerification()
        self.addCleanup(self.transport.doCleanups)
        self.transport.setUp()
        self.f = self.transport.f
        Path(self.f.env["HOME"]).mkdir()
        self.policy_path = self.f.state_dir / RV.POLICY
        self.policy_path.unlink()
        self.assertFalse(self.transport.journal.exists())
        self.assertFalse((self.f.state_dir / "remote-verifications").exists())

    def invoke(self):
        output, errors = io.StringIO(), io.StringIO()
        self.pinned_signature = inspect.signature(V.run_pinned)
        self.remote_signature = inspect.signature(RV.run_remote)
        argv = [str(self.f.operator), str(self.f.plan_path),
                "--state-dir", str(self.f.state_dir), "--unit", "u", "--pr", "7",
                "--approver", "Operator", "--verify-integration"]
        # Fixture-owned forge/transport variables are the only additions to the
        # scrubbed environment. Import the operator under that same environment.
        with mock.patch.dict(os.environ, self.f.env, clear=True), \
                mock.patch.object(sys, "path", [str(self.f.operator.parent)] + sys.path), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(RV, "run_remote", wraps=RV.run_remote) as remote, \
                mock.patch.object(RV, "coordinator_ssh", wraps=RV.coordinator_ssh) as ssh, \
                mock.patch.object(RV, "remote_call", wraps=RV.remote_call) as transport, \
                mock.patch.object(V, "run_pinned", wraps=V.run_pinned) as pinned, \
                mock.patch.object(V, "_observe_execution", wraps=V._observe_execution) as spawn, \
                redirect_stdout(output), redirect_stderr(errors), \
                self.assertRaises(SystemExit) as exit_result:
            runpy.run_path(str(self.f.operator), run_name="__main__")
        self.remote, self.ssh, self.remote_call = remote, ssh, transport
        self.pinned, self.spawn = pinned, spawn
        self.diagnostic = output.getvalue() + errors.getvalue()
        self.error_output = errors.getvalue()
        self.assertEqual(self.f.calls(["pr", "merge"]), [])
        return exit_result.exception.code

    def assert_no_remote_seams(self):
        for seam in (self.remote, self.ssh, self.remote_call):
            with self.subTest(seam=seam._mock_name):
                seam.assert_not_called()

    def assert_local_seams(self):
        self.assert_no_remote_seams()
        self.pinned.assert_called_once()
        self.spawn.assert_called_once()
        # These are the verifier API's arguments, not subprocess argv checks.
        # There is no host selection or remote option in the local launch.
        arguments = self.pinned_signature.bind(
            *self.pinned.call_args.args, **self.pinned.call_args.kwargs).arguments
        self.assertEqual(arguments["args"], [])
        self.assertFalse({"host", "remote", "verification_host"}.intersection(arguments))
        self.assertTrue(arguments["observe_completion"])

    def assert_local_evidence(self):
        # Separate subtests expose both durable regressions in a remote-default
        # mutation, even when the receipt's location assertion fails first.
        with self.subTest(evidence="receipt"):
            receipts, _ = S.load_verifications(self.f.state_dir)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["result"], "pass")
            self.assertEqual(receipts[0]["claim"], V.INTEGRATION_CLAIM)
            self.assertEqual(receipts[0]["execution"]["location"], "local")
            self.assertEqual(receipts[0]["execution"]["executor"], "direct")
        with self.subTest(evidence="ledger"):
            self.assertEqual(list((self.f.state_dir / "remote-verifications").glob("*.json")), [])

    def test_absent_policy_uses_local_seams(self):
        self.assertEqual(self.invoke(), 0, self.diagnostic)
        self.assert_local_seams()

    def test_absent_policy_records_local_execution(self):
        self.assertEqual(self.invoke(), 0, self.diagnostic)
        self.assert_local_evidence()

    def test_zero_byte_policy_refuses_before_verification(self):
        self.policy_path.write_bytes(b"")
        self.assertEqual(self.invoke(), 1, self.diagnostic)
        self.assertTrue(self.error_output.strip(), self.diagnostic)
        self.assert_no_remote_seams()
        self.pinned.assert_not_called()
        self.spawn.assert_not_called()
        self.assertEqual(S.load_verifications(self.f.state_dir)[0], [])
        self.assertEqual(list((self.f.state_dir / "remote-verifications").glob("*.json")), [])

    def test_schema_only_policy_uses_local_seams(self):
        self.policy_path.write_text(json.dumps({"schema_version": 1}))
        self.assertEqual(self.invoke(), 0, self.diagnostic)
        self.assert_local_seams()

    def test_schema_only_policy_records_local_execution(self):
        self.policy_path.write_text(json.dumps({"schema_version": 1}))
        self.assertEqual(self.invoke(), 0, self.diagnostic)
        self.assert_local_evidence()

    def test_declared_host_reaches_remote_seams(self):
        self.transport.save_policy()
        self.assertEqual(self.invoke(), 0, self.diagnostic)
        self.remote.assert_called_once()
        arguments = self.remote_signature.bind(
            *self.remote.call_args.args, **self.remote.call_args.kwargs).arguments
        self.assertEqual(arguments["remote"],
                         self.transport.policy["verification_host"])
        self.ssh.assert_called_once()
        self.assertGreater(self.remote_call.call_count, 0)
        self.pinned.assert_not_called()
        self.spawn.assert_not_called()
        receipts, _ = S.load_verifications(self.f.state_dir)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["result"], "pass")
        execution = receipts[0]["execution"]
        self.assertEqual((execution["location"], execution["executor"]), ("remote", "direct"))
        ledgers = list((self.f.state_dir / "remote-verifications").glob("*.json"))
        self.assertEqual(len(ledgers), 1)
        runs = json.loads(ledgers[0].read_text())["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["launch_id"], execution["launch_id"])
        self.assertTrue(runs[0]["published"])
        self.assertTrue(Path(self.f.env["REMOTE_LOG"]).read_text().strip())


if __name__ == "__main__":
    unittest.main()
