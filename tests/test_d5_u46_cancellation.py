"""U4.6 same-lifetime release measured through the real operator and R6."""
from contextlib import nullcontext
from tests.fixtures.d5.ready import ready_installation
from contextlib import ExitStack
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import test_d5_u45_retry_recovery as recovery
from tests.test_d5_u4_0b1_issuance import no_effects
from tests.test_d5_u4_2_request_authority import tree_bytes
from tests.fixtures.d5.u45_operator import probe
import d5_activation as activation
import merge_unit as M

BASE = 'd48e6324aae6ace2a296152b4d6365a509d51aea'
FIXTURES = Path(__file__).parent / 'fixtures/d5'
MUTATION = None


def inactive_capture():
    """Capture real legacy cancellation writes on an empty, replaced PATH."""
    with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
        root = Path(root)
        tools = root / 'tools'
        tools.mkdir()
        stack.enter_context(mock.patch.dict(os.environ, {'PATH': str(tools)}))
        assert shutil.which('sinfo') is None
        stack.enter_context(nullcontext())
        launches = []
        def launch(*args, **kwargs):
            launches.append(True)
            raise AssertionError('unexpected launch')
        for name in ('Popen', 'run', 'call', 'check_call', 'check_output', 'getoutput', 'getstatusoutput'):
            stack.enter_context(mock.patch.object(subprocess, name, side_effect=launch))
        for name in dir(os):
            if (name.startswith(('exec', 'spawn', 'posix_spawn')) or
                    name in ('fork', 'forkpty', 'system', 'popen', 'startfile')) and callable(getattr(os, name)):
                stack.enter_context(mock.patch.object(os, name, side_effect=launch))
        path = root / 'merge-unit-fixture.json'
        intent = dict(operation_id='fixture', phase='merge_requested')
        path.write_text(json.dumps(intent))
        writes = []
        write = M.durable_write
        def writing(path, row):
            writes.append(path.name)
            return write(path, row)
        stack.enter_context(mock.patch.object(M, 'durable_write', side_effect=writing))
        result = M.cancel_before_request(path, intent, None, 'target read failed')
        files = {p.name: json.loads(p.read_text()) for p in root.iterdir() if p.is_file()}
        for row in files.values():
            datetime.fromisoformat(row['cancellation']['observed_at'])
            row['cancellation']['observed_at'] = '<time>'
        return dict(result=result, launches=launches, writes=writes, files=files)


class TestCancellation(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery.TestRecovery()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c = self.fixture.c

    def invoke(self, mode):
        result = subprocess.run([sys.executable, str(FIXTURES / 'u46_operator.py'),
            str(self.fixture.data), mode] + ([MUTATION] if MUTATION else []), cwd=self.c.repo, env=self.c.env,
            capture_output=True, text=True, timeout=45)
        self.assertNotIn('Traceback', result.stderr, result.stderr)
        records = [json.loads(line[len('LIFETIME '):]) for line in result.stdout.splitlines()
                   if line.startswith('LIFETIME ')]
        self.assertEqual(len(records), 1, result.stdout + result.stderr)
        self.assertEqual(probe(self.fixture.record), 'available', 'owner has exited')
        return result, records[0]

    def retained(self, mode, expected_code=1):
        result, facts = self.invoke(mode)
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        self.assertEqual(facts['drain'], 'blocked', facts)
        self.assertTrue(facts['lease'], facts)
        self.assertEqual(facts['reaped'], True, facts)
        self.assertEqual(len(self.c.calls(['pr', 'merge'])), 1)
        self.assertEqual(self.c.receipts(), [])
        row = self.c.intent()
        self.assertEqual(row['phase'], 'merge_requested')
        self.assertNotEqual(row['d5'].get('classification'), 'not_submitted')
        self.assertNotEqual(row['d5']['state'], 'Recorded')
        self.fixture.fence(True)
        # Fresh-process recovery cannot infer completion from a local exit.
        forge = json.loads(Path(self.c.env['FORGE_STATE']).read_text())
        forge['pr'].update(state='OPEN', mergeCommit=None)
        Path(self.c.env['FORGE_STATE']).write_text(json.dumps(forge))
        before = tree_bytes(self.c.state_dir)
        refused = self.fixture.invoke()
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn('unresolved outcome', refused.stderr)
        self.assertEqual(tree_bytes(self.c.state_dir), before)
        self.assertEqual(len(self.c.calls(['pr', 'merge'])), 1)
        return facts

    def test_cancel_after_dispatch_cannot_use_legacy_noeffect(self):
        facts = self.retained('cancel-after-dispatch', 130)
        self.assertTrue(facts['cancel_refused'])
        self.assertTrue(facts['cancel_unchanged'])
        self.assertEqual(self.c.intent()['d5']['classification'], 'unknown')

    def test_timeout_does_not_complete_or_release(self):
        self.retained('timeout')

    def test_interruption_does_not_complete_or_release(self):
        self.retained('interrupt', 130)

    def test_external_exit_and_reap_do_not_release(self):
        self.c.forge['fail_merge'] = True
        self.c.save()
        self.retained('exit')

    def test_unknown_queued_operation_does_not_release(self):
        self.c.forge['queued'] = True
        self.c.save()
        self.retained('queued')

    def test_prior_lease_loss_detection_never_writes_completion(self):
        # Inject detection of prior loss, without unlocking/sharing a descriptor
        # during submit (OD5). No writes may follow the failed capability check.
        facts = self.retained('lease-loss')
        self.assertTrue(facts['loss_detected'])
        self.assertEqual(facts['writes_after_loss'], [])

    def test_pre_start_refusal_releases_without_invocation(self):
        result, facts = self.invoke('pre-start')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(facts['drain'], 'available')
        self.assertTrue(facts['lease'])
        self.assertEqual(self.c.calls(['pr', 'merge']), [])
        self.assertEqual(list(self.c.state_dir.glob('merge-unit-*.json')), [])
        self.assertEqual(self.c.receipts(), [])

    def test_pre_dispatch_observation_timeout_uses_existing_release(self):
        result, facts = self.invoke('pre-dispatch')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(facts['drain'], 'available')
        self.assertTrue(facts['lease'])
        self.assertEqual(self.c.calls(['pr', 'merge']), [])
        self.assertEqual(self.c.intent()['d5']['classification'], 'not_submitted')
        self.assertEqual(self.c.receipts(), [])
        # R6 release and the unresolved installation fence are distinct.
        self.fixture.fence(True)

    def test_authoritative_no_invocation_uses_existing_n_release(self):
        result, facts = self.invoke('not-submitted')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(facts['drain'], 'available')
        self.assertEqual(self.c.calls(['pr', 'merge']), [])
        self.assertEqual(self.c.intent()['d5']['classification'], 'not_submitted')
        self.assertEqual(self.c.receipts(), [])

    def test_normal_completion_releases_after_receipt_under_lease(self):
        result, facts = self.invoke('normal')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(facts['drain'], 'available')
        self.assertTrue(facts['lease'])
        self.assertTrue(facts['reaped'])
        self.assertEqual(facts['completion'], [{'drain': 'blocked', 'lease': True}])
        self.assertEqual(facts['writes'], ['Bound', 'Attempted', 'Recorded'])
        self.assertEqual(len(self.c.calls(['pr', 'merge'])), 1)
        self.assertEqual(len(self.c.receipts()), 1)
        self.assertEqual(self.c.intent()['phase'], 'receipt_recorded')
        self.fixture.fence(False)


class TestInactiveAndPremature(unittest.TestCase):
    def test_inactive_base_capture_with_inherited_sinfo(self):
        data = json.loads((FIXTURES / 'u46-inactive-base.json').read_text())
        self.assertEqual(data['base'], BASE)
        with tempfile.TemporaryDirectory() as inherited:
            fake = Path(inherited) / 'sinfo'
            fake.write_text('#!/bin/sh\nexit 97\n')
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': inherited}):
                self.assertEqual(shutil.which('sinfo'), str(fake))
                self.assertEqual(inactive_capture(), data['cancel_before_request'])

    def test_premature_activation_zero_effects(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            ready_installation(root, state_dir=root)
            path = root / 'merge-unit-fixture.json'
            before = tree_bytes(root)
            with nullcontext(), no_effects() as effects:
                with self.assertRaisesRegex(M.Refusal, 'D5 active: cancel_before_request'):
                    M.cancel_before_request(path, {}, None, 'cancel')
                self.assertEqual(effects, ([], []))
            self.assertEqual(tree_bytes(root), before)


if __name__ == '__main__':
    unittest.main()
