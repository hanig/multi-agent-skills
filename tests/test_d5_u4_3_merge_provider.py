"""U4.3 actual merge boundary, split START, durable provider and no replay."""
import argparse
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tests import test_d5_submission as t
from tests import test_merge_unit as legacy
from tests.test_d5_u4_1_active_surface import SurfaceFixture
from tests.test_d5_u4_2_request_authority import tree_bytes
import d5_activation as activation
import d5_merge_journal as J
import d5_readiness as readiness
import merge_unit as M
import swarm_state as state

FIXTURES = Path(__file__).parent / 'fixtures/d5'
BASE = '46423308a6a30bc4a2b96fa6f728ca8072e4fb12'


class TestMergeProvider(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        tools = Path(temp) / 'tools'
        tools.mkdir()
        (tools / 'git').symlink_to(shutil.which('git'))
        gh = tools / 'gh'
        gh.write_text('#!' + sys.executable + '\n' + legacy.GH_STUB)
        gh.chmod(0o755)
        self.stack.enter_context(mock.patch.dict(os.environ, {'PATH': str(tools)}))
        self.c = t.TestSubmission()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.stack.enter_context(mock.patch.object(activation, 'd5_active', return_value=True))
        self.binding = dict(unit='u', attempt='a', head='a' * 40,
            repo='https://github.com/example/project', target='main', pr=7)
        self.scope = dict(plans=['p'], repositories=[self.binding['repo']], actions=['merge'], trackers=[])
        self.c.original['scope'] = self.scope
        (self.c.directory / t.store.RECORD_NAME).write_bytes(t.authority.encode(self.c.original))
        self.effect = hashlib.sha256(json.dumps(self.binding, sort_keys=True).encode()).hexdigest()
        self.intent = dict(schema_version=1, operation_id=self.effect, binding=self.binding,
            root=str(self.c.root / 'runs'), phase='merge_requested', target_before_request='b' * 40,
            preconditions=dict(approver='Operator', integration=dict(target_commit='b' * 40,
                subject_head='a' * 40, result='pass'), required_merge_claims=['integration-tests']))
        self.path = self.c.state_dir / ('merge-unit-' + self.effect + '.json')
        self.receipts = self.c.state_dir / 'merge-receipts.jsonl'
        self.args = argparse.Namespace(pr=7, plan='fixture', state_dir=str(self.c.state_dir), unit='u')
        self.cmd = M.commands(self.args, self.intent['root'], self.binding, 'github.com', 'example/project')
        self.forge = dict(pr=dict(number=7, url='https://github.com/example/project/pull/7',
            state='OPEN', headRefOid='a' * 40, baseRefName='main', title='Fixture', mergeCommit=None),
            checks=[dict(name='fixture', state='SUCCESS')],
            ref=dict(ref='refs/heads/main', object=dict(type='commit', sha='b' * 40)),
            ref_command=self.cmd['target'][1:], commit=dict(sha='c' * 40, parents=[dict(sha='b' * 40)]))
        self.forge_path, self.log = self.c.root / 'forge.json', self.c.root / 'forge.log'
        self.save_forge()
        self.stack.enter_context(mock.patch.dict(os.environ, dict(FORGE_STATE=str(self.forge_path),
            FORGE_LOG=str(self.log), COORDINATOR_STATE=str(self.c.state_dir))))
        self.stack.enter_context(mock.patch.object(M, 'retained_integration_problem', return_value=None))
        self.c.operation = t.submission.Operation(self.effect, 'merge', J.encoded(self.scope), J.encoded(self.intent))
        self.boundary = M._MergeBoundary(self.c.state_dir, self.binding, str(self.c.repo),
            'github.com', 'example/project', self.cmd, self.intent)
        self.c.boundary = self.boundary

    def save_forge(self):
        self.forge_path.write_text(json.dumps(self.forge))

    def row(self):
        return json.loads(self.path.read_bytes())

    def journal_bytes(self):
        return {p.name: p.read_bytes() for p in self.c.state_dir.glob('merge-*') if p.is_file()}

    def merges(self):
        calls = [json.loads(s) for s in self.log.read_text().splitlines()] if self.log.exists() else []
        return [call for call in calls if call[:2] == ['pr', 'merge']]

    def handle(self):
        return J.Handle(self.effect, str(self.c.state_dir), J.encoded(self.row()['d5']['binding']))

    def test_actual_process_order_and_in_process_recording(self):
        events = []
        write, popen, sync = M.durable_write, subprocess.Popen, os.fsync
        def writing(path, row):
            self.assertTrue(state.holds_lease(self.c.state_dir))
            events.append(row.get('d5', {}).get('state'))
            return write(path, row)
        def launching(argv, *args, **kwargs):
            if argv[:3] == ['gh', 'pr', 'merge']:
                self.assertEqual(self.row()['d5']['state'], 'Bound')
                self.assertEqual(self.c.probe('start'), 'blocked')
                self.assertEqual(events[-1], 'observed')
                self.assertFalse(self.receipts.exists())
                events.append('launched')
            self.assertNotEqual(argv[:3], [sys.executable, M.SWARM, 'merge'])
            return popen(argv, *args, **kwargs)
        observe = self.boundary.observe
        def observing(operation):
            self.assertEqual(self.row()['d5']['state'], 'Bound')
            self.assertEqual(self.c.probe('start'), 'available')
            value = observe(operation)
            events.append('observed')
            return value
        def syncing(fd):
            self.assertFalse(events and events[-1] == 'observed', 'fsync between observation and launch')
            return sync(fd)
        with mock.patch.object(M, 'durable_write', writing), mock.patch.object(subprocess, 'Popen', launching), \
                mock.patch.object(self.boundary, 'observe', observing), mock.patch.object(os, 'fsync', syncing):
            result = self.c.submit()
        self.assertIsInstance(result.outcome, J.Observed)
        self.assertEqual(events, ['Bound', 'observed', 'launched', 'Attempted', 'Recorded'])
        self.assertEqual(len(self.merges()), 1)
        self.assertEqual(self.row()['phase'], 'receipt_recorded')
        self.assertEqual(self.c.probe('drain'), 'available')
        self.assertFalse((self.c.directory / t.ledger.DIRECTORY).exists())
        self.assertEqual(json.loads(self.receipts.read_text())['d5']['binding']['installation'], readiness.require(self.c.state_dir))

    def gap(self, change, lease_loss=False):
        paused, resume = threading.Event(), threading.Event()
        snapshot = []
        def observing(operation):
            snapshot.append(self.journal_bytes())
            paused.set()
            if not resume.wait(8):
                raise AssertionError('observation watchdog')
            return self.intent['target_before_request']
        with mock.patch.object(self.boundary, 'observe', observing):
            thread, results = self.c.launch(self.c.submit)
            try:
                self.assertTrue(paused.wait(8), 'preparation watchdog')
                self.assertEqual(self.c.probe('start'), 'available')
                self.assertEqual(self.c.probe('drain'), 'blocked')
                change()
            finally:
                resume.set()
                result = self.c.join(thread, results)
        self.assertIsInstance(result, t.authority.AuthorityError)
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.c.probe('drain'), 'available')
        if lease_loss:
            self.assertIn('caller-held coordinator lease required', str(result))
            self.assertEqual(self.journal_bytes(), snapshot[0])
            self.assertEqual(self.row()['d5']['state'], 'Bound')
            ok, reason = state.acquire_lease(self.c.state_dir, bump_epoch=False)
            self.assertTrue(ok, reason)
        else:
            self.assertEqual(self.row()['d5']['classification'], 'not_submitted')
        self.assertFalse(self.receipts.exists())
        # Both consumers must retain the unresolved fence after DRAIN release.
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            with t.protocol.pin(self.c.directory) as pinned, pinned.drain(shared=False), pinned.start():
                readiness.successor_gate(self.c.entry)
        with mock.patch.object(M, 'authority', return_value=(self.c.state_dir, self.intent['root'],
                self.binding, 'github.com', 'example/project', {}, str(self.c.repo))), \
                mock.patch.object(M, 'validate_scope_report'), \
                mock.patch.object(self.boundary.__class__, 'read', side_effect=lambda command, **kw:
                    subprocess.CompletedProcess(command, 0, json.dumps(self.forge['pr'] if command == self.cmd['view'] else {}), '')):
            args = argparse.Namespace(**dict(vars(self.args), dry_run=False, root=self.intent['root'],
                verify_integration=False, abandon_intent=None, retrieve_remote_evidence=False,
                remote_recovery_attestation=None))
            with self.assertRaisesRegex(ValueError, 'unresolved outcome'):
                M.reconcile(args, dict(name='p'))
        self.assertEqual(self.merges(), [])
        if lease_loss:
            self.assertEqual(self.journal_bytes(), snapshot[0])
        return result

    def test_r_revocation_completes_in_gap(self):
        self.gap(lambda: t.control.revoke(*self.c.args, 151, 'stop'))

    def test_a_expiry_in_gap(self):
        self.gap(lambda: setattr(self.c.clock, 'return_value', 200))

    def test_b_relinquishment_in_gap(self):
        self.gap(lambda: t.control.relinquish(*self.c.args, now=151))

    def test_c_regeneration_in_gap(self):
        def change():
            # Fault injection: participating transfer is DRAIN-fenced. The
            # second validation must still reject a changed stored generation.
            t.store.relinquish(self.c.directory, 151, 'native', t.ANCHOR)
            t.store.claim(self.c.directory, 152, t.MANDATE, 'native', t.ANCHOR, lambda _: 'alive')
        self.gap(change)

    def test_d_lease_loss_keeps_bound_bytes_and_releases_drain(self):
        self.gap(lambda: state.release_lease(self.c.state_dir), lease_loss=True)

    def test_e_anchor_loss_in_gap(self):
        patch = mock.patch.object(t.host, 'verify_anchor', return_value=False)
        self.gap(lambda: self.stack.enter_context(patch))

    def test_revocation_waits_in_first_start_then_wins_gap(self):
        prepared, release, observed, revoked = (threading.Event() for _ in range(4))
        prepare = J.PROVIDER.prepare
        def preparing(*args, **kwargs):
            handle = prepare(*args, **kwargs)
            prepared.set()
            if not release.wait(8):
                raise AssertionError('prepare watchdog')
            return handle
        def observe(operation):
            observed.set()
            if not revoked.wait(8):
                raise AssertionError('revocation watchdog')
            return self.intent['target_before_request']
        def revoke():
            result = t.control.revoke(*self.c.args, 151, 'stop')
            revoked.set()
            return result
        with mock.patch.object(J.PROVIDER, 'prepare', preparing), mock.patch.object(self.boundary, 'observe', observe):
            thread, results = self.c.launch(self.c.submit)
            self.assertTrue(prepared.wait(8))
            worker, output = self.c.launch(revoke)
            try:
                self.assertFalse(revoked.wait(.15))
                self.assertEqual(self.c.probe('start'), 'blocked')
            finally:
                release.set()
                result = self.c.join(thread, results)
                self.c.join(worker, output)
        self.assertTrue(observed.is_set() and revoked.is_set())
        self.assertIsInstance(result, t.authority.AuthorityError)
        self.assertEqual(self.row()['d5']['classification'], 'not_submitted')
        self.assertEqual(self.merges(), [])

    def test_observation_failure_and_movement_cancel_without_replay(self):
        for failure in (OSError('read failed'), 'd' * 40):
            with self.subTest(failure=failure):
                case = TestMergeProvider()
                case.setUp()
                try:
                    def observe(operation):
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    with mock.patch.object(case.boundary, 'observe', observe), case.assertRaises((OSError, ValueError)):
                        case.c.submit()
                    case.assertEqual(case.row()['d5']['classification'], 'not_submitted')
                    case.assertEqual(case.merges(), [])
                    with case.assertRaisesRegex(ValueError, 'unresolved'):
                        case.c.submit()
                finally:
                    case.doCleanups()

    def test_response_wait_is_outside_start_with_drain(self):
        paused, release = threading.Event(), threading.Event()
        finish = self.boundary.finish
        def finishing():
            paused.set()
            if not release.wait(8):
                raise AssertionError('finish watchdog')
            return finish()
        with mock.patch.object(self.boundary, 'finish', finishing):
            thread, results = self.c.launch(self.c.submit)
            try:
                self.assertTrue(paused.wait(8))
                self.assertEqual(self.c.probe('start'), 'available')
                self.assertEqual(self.c.probe('drain'), 'blocked')
                t.control.revoke(*self.c.args, 151, 'stop')
            finally:
                release.set()
                result = self.c.join(thread, results)
        self.assertIsInstance(result, t.submission.Result)
        self.assertEqual(self.row()['d5']['state'], 'Recorded')

    def test_failed_merge_never_recorded(self):
        self.forge['fail_merge'] = True
        self.save_forge()
        with self.assertRaisesRegex(ValueError, 'merge command failed'):
            self.c.submit()
        self.assertEqual(len(self.merges()), 1)
        self.assertFalse(self.receipts.exists())
        self.assertEqual(self.row()['d5']['classification'], 'unknown')
        self.assertEqual(self.c.probe('drain'), 'blocked')

    def test_ambiguous_popen_is_not_not_submitted(self):
        popen = subprocess.Popen
        def ambiguous(argv, *args, **kwargs):
            if argv[:3] == ['gh', 'pr', 'merge']:
                raise OSError('ambiguous launch')
            return popen(argv, *args, **kwargs)
        with mock.patch.object(subprocess, 'Popen', ambiguous), self.assertRaisesRegex(OSError, 'ambiguous'):
            self.c.submit()
        self.assertEqual(self.row()['d5']['classification'], 'unknown')
        self.assertEqual(self.c.probe('drain'), 'blocked')
        self.assertFalse(self.receipts.exists())

    def test_legacy_boolean_cannot_complete(self):
        with mock.patch.object(self.boundary, 'completion', return_value=dict(merged=True, attested=True)), \
                self.assertRaisesRegex(ValueError, 'invalid observed outcome'):
            self.c.submit()
        self.assertFalse(self.receipts.exists())
        self.assertEqual(self.row()['d5']['classification'], 'unknown')
        self.assertEqual(self.c.probe('drain'), 'blocked')

    def test_recorded_absorbing_and_receipt_readback_required(self):
        result = self.c.submit()
        before = self.journal_bytes()
        with t.protocol.pin(self.c.directory) as pinned, pinned.drain():
            capability = J.PROVIDER.bind(self.handle())
            capability.uncertain()
            self.assertEqual(capability.complete(result.outcome), result.outcome)
            self.assertEqual(self.journal_bytes(), before)
            self.receipts.write_text(json.dumps(dict(merged=True, attested=True)) + '\n')
            with self.assertRaises(ValueError):
                capability.complete(result.outcome)

    def test_missing_or_mismatched_readback_never_records(self):
        with mock.patch.object(J, '_readback', side_effect=ValueError('missing receipt')), \
                self.assertRaisesRegex(ValueError, 'missing receipt'):
            self.c.submit()
        self.assertEqual(self.row()['d5']['state'], 'Attempted')
        self.assertEqual(self.c.probe('drain'), 'blocked')

    def test_receipt_append_flock_and_directory_durability(self):
        sync, flock = J.journal._fsync_directory, J.journal.fcntl.flock
        events = []
        def locked(fd, mode):
            if mode == J.journal.fcntl.LOCK_EX:
                events.append('lock')
            return flock(fd, mode)
        def directory(path):
            events.append('directory')
            self.assertTrue(self.receipts.exists())
            return sync(path)
        with mock.patch.object(J.journal.fcntl, 'flock', locked), \
                mock.patch.object(J.journal, '_fsync_directory', directory):
            self.c.submit()
        self.assertIn('lock', events[:events.index('directory')])
        self.assertEqual(events.count('directory'), 1)

    def test_bound_completion_only_recovery_no_new_merge_after_expiry(self):
        def interrupted():
            raise RuntimeError('crash after launch')
        with mock.patch.object(self.boundary, 'finish', interrupted), self.assertRaises(RuntimeError):
            self.c.submit()
        self.boundary.process.communicate(timeout=5)
        self.c.clock.return_value = 300
        before = len(self.merges())
        with t.protocol.pin(self.c.directory) as pinned, pinned.drain():
            outcome = self.boundary.completion()
            J.recover(self.c.state_dir, self.row(), outcome)
        self.assertEqual(self.row()['d5']['state'], 'Recorded')
        self.assertEqual(len(self.merges()), before)


class TestInactiveAndEntry(unittest.TestCase):
    def test_inactive_embedded_base_and_inherited_scheduler_path(self):
        data = json.loads((FIXTURES / 'u4_3_inactive_base.json').read_text())
        self.assertEqual(data['base'], BASE)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'sinfo'
            fake.write_text('#!/bin/sh\nexit 97\n')
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': tmp}):
                self.assertEqual(shutil.which('sinfo'), str(fake))
                for name, expected in data['captures'].items():
                    with self.subTest(entry=name), tempfile.TemporaryDirectory() as root:
                        self.assertEqual(SurfaceFixture(root).observe('operator', name, False), expected)

    def test_premature_activation_all_operator_entries_zero_effects(self):
        for name in ('main', 'reconcile', 'current_intent'):
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as root:
                observed = SurfaceFixture(root).observe('operator', name, True)
                self.assertEqual(observed['launches'], [])
                self.assertEqual(observed['writes'], [])
                self.assertEqual(observed['files_changed'], [])
                self.assertIn('D5 active:', str(observed['outcome']) + str(observed['output']))


if __name__ == '__main__':
    unittest.main()
