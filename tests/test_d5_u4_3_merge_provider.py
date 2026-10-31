"""U4.3 actual merge boundary, split START, durable provider and no replay."""
from contextlib import nullcontext
import argparse
from contextlib import ExitStack
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
from swarm_types import OutboxError

FIXTURES = Path(__file__).parent / 'fixtures/d5'
BASE = '1b61800ff263b4a10978afdbbdfc64b117195914'


class TestMergeProvider(unittest.TestCase):
    eq = unittest.TestCase.assertEqual
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        tools = Path(temp) / 'tools'
        tools.mkdir()
        (tools / 'git').symlink_to(shutil.which('git'))
        gh = tools / 'gh'
        gh.write_text('#!' + sys.executable + '\n' + legacy.GH_STUB.replace(
            'elif args[:1] ==', '    if data.get("fail_merge_after"): sys.exit(1)\nelif args[:1] =='))
        gh.chmod(0o755)
        self.stack.enter_context(mock.patch.dict(os.environ, {'PATH': str(tools)}))
        self.c = t.TestSubmission()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.stack.enter_context(nullcontext())
        self.stack.enter_context(mock.patch.object(M, 'merge_launch_admitted', return_value=True))
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

    def test_journal_callers_resolve_relative_state_before_classifying(self):
        from tests.fixtures.d5.ready import missing_installation
        entry = readiness.require(self.c.state_dir)
        previous = Path.cwd()
        try:
            os.chdir(self.c.state_dir.parent)
            relative = Path(self.c.state_dir.name)
            for locator in (relative, self.c.state_dir):
                with self.subTest(locator=locator):
                    with J.protocol.pin(entry['record_dir']) as record:
                        with record.drain():
                            self.assertEqual(J._held(locator), entry)
                    with J.recovery_drain(locator):
                        pass
                    with missing_installation(entry):
                        with self.assertRaisesRegex(J.AuthorityError, '^D5 merge journal: inactive$'):
                            J._held(locator)
                        with self.assertRaisesRegex(J.AuthorityError, '^D5 merge journal: inactive$'):
                            with J.recovery_drain(locator):
                                pass
        finally:
            os.chdir(previous)

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

    def test_coordinator_lease_descriptor_is_non_inheritable(self):
        fd = state._LOCK_FDS[str(self.c.state_dir.resolve())]
        self.assertTrue(state.holds_lease(self.c.state_dir))
        self.assertFalse(os.get_inheritable(fd))

    def test_merge_child_receives_no_lease_or_drain_descriptors(self):
        popen = subprocess.Popen
        launches = []
        def launching(argv, *args, **kwargs):
            if argv[:3] == ['gh', 'pr', 'merge']:
                launches.append(argv)
                self.assertFalse('pass_fds' in kwargs, 'merge child must not receive extra descriptors')
                self.assertTrue(kwargs.get('close_fds', True))
            return popen(argv, *args, **kwargs)
        with mock.patch.object(subprocess, 'Popen', launching):
            self.c.submit()
        self.eq(len(launches), 1)
        self.eq(len(self.merges()), 1)

    def test_actual_process_order_and_in_process_recording(self):
        events = []
        write, popen, sync = M.durable_write, subprocess.Popen, os.fsync
        def writing(path, row):
            self.assertTrue(state.holds_lease(self.c.state_dir))
            events.append(row.get('d5', {}).get('state'))
            return write(path, row)
        def launching(argv, *args, **kwargs):
            if argv[:3] == ['gh', 'pr', 'merge']:
                self.eq(self.row()['d5']['state'], 'Bound')
                self.eq(self.c.probe('start'), 'blocked')
                self.eq(events[-1], 'observed')
                self.assertFalse(self.receipts.exists())
                events.append('launched')
            self.assertNotEqual(argv[:3], [sys.executable, M.SWARM, 'merge'])
            return popen(argv, *args, **kwargs)
        observe = self.boundary.observe
        def observing(operation):
            self.eq(self.row()['d5']['state'], 'Bound')
            self.eq(self.c.probe('start'), 'available')
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
        self.eq(events, ['Bound', 'observed', 'launched', 'Attempted', 'Recorded'])
        self.eq(len(self.merges()), 1)
        self.eq(self.row()['phase'], 'receipt_recorded')
        self.eq(self.c.probe('drain'), 'available')
        self.assertFalse((self.c.directory / t.ledger.DIRECTORY).exists())
        self.eq(json.loads(self.receipts.read_text())['d5']['binding']['installation'], readiness.require(self.c.state_dir))

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
                self.eq(self.c.probe('start'), 'available')
                self.eq(self.c.probe('drain'), 'blocked')
                change()
            finally:
                resume.set()
                result = self.c.join(thread, results)
        self.assertIsInstance(result, t.authority.AuthorityError)
        self.eq(self.merges(), [])
        self.eq(self.c.probe('drain'), 'available')
        if lease_loss:
            self.assertIn('caller-held coordinator lease required', str(result))
            self.eq(self.journal_bytes(), snapshot[0])
            self.eq(self.row()['d5']['state'], 'Bound')
            ok, reason = state.acquire_lease(self.c.state_dir, bump_epoch=False)
            self.assertTrue(ok, reason)
        else:
            self.eq(self.row()['d5']['classification'], 'not_submitted')
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
        self.eq(self.merges(), [])
        if lease_loss:
            self.eq(self.journal_bytes(), snapshot[0])
        return result

    def test_r_revocation_completes_in_gap(self):
        self.gap(lambda: t.control.revoke(*self.c.args, 151, 'stop'))

    def test_slow_reads_keep_legacy_budget_outside_gap(self):
        calls = []
        def slow(command, **kwargs):
            calls.append(kwargs['timeout'])
            # A virtual 61-second response is valid under the legacy budget.
            if kwargs['timeout'] < 61:
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            return subprocess.CompletedProcess(command, 0, json.dumps(self.forge['ref']), '')
        with mock.patch.object(subprocess, 'run', side_effect=slow), \
                mock.patch.object(M, '_bounded_observation', side_effect=slow):
            for name in ('scope', 'view', 'checks'):
                self.eq(self.boundary.read(self.cmd[name]).returncode, 0)
            self.eq(self.boundary.target(), self.intent['target_before_request'])
            parent = M.parent_command('github.com', 'example/project', 'c' * 40)
            self.eq(self.boundary.read(parent).returncode, 0)
            with self.assertRaises(subprocess.TimeoutExpired) as refused:
                self.boundary.observe(self.c.operation)
        self.eq(calls[:-1], [300] * 5)
        self.assertGreater(refused.exception.timeout, 0)
        self.assertLessEqual(refused.exception.timeout, 60)

    def test_a_expiry_in_gap(self):
        self.gap(lambda: setattr(self.c.clock, 'return_value', 200))

    def test_observation_deadline_covers_process_creation(self):
        entered, resume, finished = (threading.Event() for _ in range(3))
        observed = threading.Event()
        observe = self.boundary.observe
        popen = subprocess.Popen
        process_exit = popen.__exit__
        def stalled(command, *args, **kwargs):
            if command == self.cmd['target']:
                entered.set()
                if not resume.wait(45):
                    raise AssertionError('constructor watchdog')
            return popen(command, *args, **kwargs)
        def observing(operation):
            try:
                return observe(operation)
            finally:
                observed.set()
        def exiting(process, *args):
            try:
                return process_exit(process, *args)
            finally:
                if process.args == self.cmd['target']:
                    finished.set()
        with mock.patch.object(self.boundary, '_observation_timeout', 1, create=True), \
                mock.patch.object(self.boundary, 'observe', observing), \
                mock.patch.object(popen, '__exit__', exiting), \
                mock.patch.object(subprocess, 'Popen', stalled):
            thread, results = self.c.launch(self.c.submit)
            try:
                self.assertTrue(entered.wait(8))
                self.assertTrue(observed.wait(3), 'observation deadline excludes process creation')
                thread.join(30)  # Durable refusal accounting is outside the read deadline.
                self.assertFalse(thread.is_alive(), 'refusal accounting watchdog')
                self.assertIsInstance(results[0], subprocess.TimeoutExpired)
                self.eq(self.merges(), [])
                self.eq(self.row()['d5']['classification'], 'not_submitted')
                self.eq(self.c.probe('drain'), 'available')
                self.assertTrue(state.holds_lease(self.c.state_dir))
                self.assertFalse(self.receipts.exists())
            finally:
                resume.set()
                self.c.join(thread, results)
                self.assertTrue(finished.wait(8), 'late read cleanup watchdog')
        self.eq(self.merges(), [])

    def test_b_relinquishment_in_gap(self):
        self.gap(lambda: t.control.relinquish(*self.c.args, now=151))

    def test_c_regeneration_in_gap(self):
        def change():
            # Fault injection: participating transfer is DRAIN-fenced. The
            # merged store transition permits a successor only after death;
            # do not rewrite custody or relax its relinquished-holder refusal.
            t.store.relinquish(self.c.directory, 151, 'native', t.ANCHOR)
            with self.assertRaisesRegex(t.authority.AuthorityError, 'custody relinquished'):
                t.store.claim(self.c.directory, 152, t.MANDATE, 'native', t.ANCHOR, lambda _: 'alive')
            liveness = mock.Mock(return_value='dead')
            successor = t.store.claim(self.c.directory, 152, t.MANDATE, 'successor',
                                      t.host.ProcessIdentity(43, 9000002), liveness)
            liveness.assert_called_once_with(t.ANCHOR)
            self.eq(successor['custody']['generation'], 2)
            self.eq(t.store.read(self.c.directory)['custody'], successor['custody'])
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
                self.eq(self.c.probe('start'), 'blocked')
            finally:
                release.set()
                result = self.c.join(thread, results)
                self.c.join(worker, output)
        self.assertTrue(observed.is_set() and revoked.is_set())
        self.assertIsInstance(result, t.authority.AuthorityError)
        self.eq(self.row()['d5']['classification'], 'not_submitted')
        self.eq(self.merges(), [])

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
                    case.eq(case.row()['d5']['classification'], 'not_submitted')
                    case.eq(case.merges(), [])
                    with case.assertRaisesRegex(ValueError, 'unresolved'):
                        case.c.submit()
                finally:
                    case.doCleanups()

    def test_response_wait_is_outside_start_with_drain(self):
        paused, release = threading.Event(), threading.Event()
        communicate = subprocess.Popen.communicate
        def finishing(process, *args, **kwargs):
            if process.args[:3] == ['gh', 'pr', 'merge']:
                paused.set()
                if not release.wait(8):
                    raise AssertionError('finish watchdog')
            return communicate(process, *args, **kwargs)
        with mock.patch.object(subprocess.Popen, 'communicate', finishing):
            thread, results = self.c.launch(self.c.submit)
            try:
                self.assertTrue(paused.wait(8))
                self.eq(self.c.probe('start'), 'available')
                self.eq(self.c.probe('drain'), 'blocked')
                t.control.revoke(*self.c.args, 151, 'stop')
            finally:
                release.set()
                result = self.c.join(thread, results)
        self.assertIsInstance(result, t.submission.Result)
        self.eq(self.row()['d5']['state'], 'Recorded')

    def test_failed_merge_never_recorded(self):
        self.forge['fail_merge_after'] = True
        self.save_forge()
        with self.assertRaisesRegex(ValueError, 'merge command failed'):
            self.c.submit()
        self.eq(len(self.merges()), 1)
        self.assertFalse(self.receipts.exists())
        self.eq(self.row()['d5']['classification'], 'unknown')
        self.eq(self.c.probe('drain'), 'blocked')

    def test_ambiguous_popen_is_not_not_submitted(self):
        popen = subprocess.Popen
        def ambiguous(argv, *args, **kwargs):
            if argv[:3] == ['gh', 'pr', 'merge']:
                raise OSError('ambiguous launch')
            return popen(argv, *args, **kwargs)
        with mock.patch.object(subprocess, 'Popen', ambiguous), self.assertRaisesRegex(OSError, 'ambiguous'):
            self.c.submit()
        self.eq(self.row()['d5']['classification'], 'unknown')
        self.eq(self.c.probe('drain'), 'blocked')
        self.assertFalse(self.receipts.exists())

    def test_legacy_boolean_cannot_complete(self):
        with mock.patch.object(self.boundary, 'completion', return_value=dict(merged=True, attested=True)), \
                self.assertRaisesRegex(ValueError, 'invalid observed outcome'):
            self.c.submit()
        self.assertFalse(self.receipts.exists())
        self.eq(self.row()['d5']['classification'], 'unknown')
        self.eq(self.c.probe('drain'), 'blocked')

    def test_recorded_absorbing_and_receipt_readback_required(self):
        result = self.c.submit()
        before = self.journal_bytes()
        with t.protocol.pin(self.c.directory) as pinned, pinned.drain():
            capability = J.PROVIDER.bind(self.handle())
            capability.uncertain()
            self.eq(capability.complete(result.outcome), result.outcome)
            self.eq(self.journal_bytes(), before)
            self.receipts.write_text(json.dumps(dict(merged=True, attested=True)) + '\n')
            with self.assertRaisesRegex(OutboxError, 'merge receipt journal cannot be read in full'):
                capability.complete(result.outcome)

    def resolved_successor(self, disposition):
        if disposition == 'abandonment':
            name = 'merge-abandonment-' + self.effect + '.json'
            self.path.write_text(json.dumps(dict(self.intent,
                phase='resolved_by_abandonment', abandonment=name)))
            (self.c.state_dir / name).write_text(json.dumps(dict(
                schema_version=1, operation_id=self.effect, intent=self.intent,
                approver='Owner', reason='Approved fresh operation',
                observed_at='2025-01-01T00:00:00+00:00', observed_pr=self.forge['pr'])))
        else:
            self.path.write_text(json.dumps(dict(self.intent,
                phase='cancelled_before_request', cancellation=dict(
                    reason='Target moved before request', observed_target='d' * 40,
                    observed_at='2025-01-01T00:00:00+00:00'))))
        operation, path, prior = M.current_intent(
            self.c.state_dir, self.binding, self.intent['root'], repair=False)
        self.assertIsNone(prior)
        self.assertNotEqual(operation, self.effect)
        self.intent = dict(self.intent, operation_id=operation)
        self.boundary.intent = self.intent
        self.c.operation = t.submission.Operation(
            operation, 'merge', J.encoded(self.scope), J.encoded(self.intent))
        self.c.submit()
        self.eq(json.loads(path.read_text())['d5']['state'], 'Recorded')
        self.eq(len(self.merges()), 1)

    def test_review_resolved_abandonment_permits_successor(self):
        self.resolved_successor('abandonment')

    def test_review_resolved_cancellation_permits_successor(self):
        self.resolved_successor('cancellation')

    def test_review_bound_recovery_remains_idempotent(self):
        self.c.submit()
        row = self.row()
        row['phase'] = 'merge_requested'
        row['d5'] = dict(state='Bound', binding=row['d5']['binding'])
        self.path.write_text(json.dumps(row))
        self.receipts.unlink()
        with t.protocol.pin(self.c.directory) as pinned, pinned.drain():
            outcome = self.boundary.completion()
            J.recover(self.c.state_dir, self.row(), outcome)
            self.eq(self.row()['d5']['state'], 'Recorded')
            before = self.journal_bytes()
            J.recover(self.c.state_dir, self.row(), outcome)
            self.eq(self.journal_bytes(), before)
            J.PROVIDER.bind(self.handle()).uncertain()
            self.eq(self.journal_bytes(), before)
        self.eq(len(self.merges()), 1)

    def test_review_interrupted_recording_reuses_durable_receipt(self):
        write = J._write
        def interrupted(handle, row):
            if row['d5']['state'] == 'Recorded':
                raise OSError('interruption after receipt')
            return write(handle, row)
        with mock.patch.object(J, '_write', interrupted), \
                self.assertRaisesRegex(OSError, 'interruption after receipt'):
            self.c.submit()
        self.eq(self.row()['d5']['state'], 'Attempted')
        before = self.receipts.read_bytes()
        with t.protocol.pin(self.c.directory) as pinned, pinned.drain():
            J.recover(self.c.state_dir, self.row(), self.boundary.completion())
        self.eq(self.receipts.read_bytes(), before)
        self.eq(self.row()['d5']['state'], 'Recorded')
        self.eq(len(self.merges()), 1)

    def test_idempotent_receipt_append_accepts_readable_blank_lines(self):
        before = '\n \t\n\u00a0\n'.encode('utf-8')
        self.receipts.write_bytes(before)
        self.eq(M.S.load_merge_receipts(self.c.state_dir), ([], []))
        self.c.submit()
        self.assertTrue(self.receipts.read_bytes().startswith(before))
        self.eq(len(M.S.load_merge_receipts(self.c.state_dir)[0]), 1)
        self.eq(self.row()['d5']['state'], 'Recorded')
        self.eq(len(self.merges()), 1)

    def test_reader_line_separators_survive_interrupted_recording(self):
        separators = ('\n', '\r', '\r\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')
        prior = dict(unit='prior', repo=self.binding['repo'], target='main', head='d' * 40,
            pr='https://github.com/example/project/pull/5', merged_as='e' * 40, method='squash',
            target_commit='b' * 40, merged=True, attested=True, integration_status='candidate-verified')
        before = (''.join(json.dumps(dict(prior, unit='prior-' + str(n))) + separator
                          for n, separator in enumerate(separators)) + json.dumps(prior)).encode('utf-8')
        self.receipts.write_bytes(before)
        rows, problems = M.S.load_merge_receipts(self.c.state_dir)
        self.eq(len(rows), len(separators) + 1)
        self.eq(problems, [])
        write = J._write
        def interrupted(handle, row):
            if row['d5']['state'] == 'Recorded':
                raise OSError('interruption after receipt')
            return write(handle, row)
        with mock.patch.object(J, '_write', interrupted), \
                self.assertRaisesRegex(OSError, 'interruption after receipt'):
            self.c.submit()
        self.assertTrue(self.receipts.read_bytes().startswith(before))
        durable = self.receipts.read_bytes()
        with t.protocol.pin(self.c.directory) as pinned, pinned.drain():
            J.recover(self.c.state_dir, self.row(), self.boundary.completion())
        self.eq(self.receipts.read_bytes(), durable)
        self.eq(len(M.S.load_merge_receipts(self.c.state_dir)[0]), len(separators) + 2)
        self.eq(self.row()['d5']['state'], 'Recorded')
        self.eq(len(self.merges()), 1)

    def test_successor_id_cannot_skip_unresolved_predecessor(self):
        self.c.submit()
        before = self.journal_bytes()
        successor = hashlib.sha256((self.effect + ':after-abandonment').encode()).hexdigest()
        self.intent = dict(self.intent, operation_id=successor)
        self.boundary.intent = self.intent
        self.c.operation = t.submission.Operation(
            successor, 'merge', J.encoded(self.scope), J.encoded(self.intent))
        with self.assertRaisesRegex(ValueError, 'operation mismatch'):
            self.c.submit()
        self.eq(self.journal_bytes(), before)
        self.eq(len(self.merges()), 1)

    def test_missing_or_mismatched_readback_never_records(self):
        with mock.patch.object(J, '_readback', side_effect=ValueError('missing receipt')), \
                self.assertRaisesRegex(ValueError, 'missing receipt'):
            self.c.submit()
        self.eq(self.row()['d5']['state'], 'Attempted')
        self.eq(self.c.probe('drain'), 'blocked')

    def test_receipt_append_flock_and_directory_durability(self):
        sync, flock = J.journal._fsync_directory, J.journal.fcntl.flock
        fsync = os.fsync
        events = []
        def locked(fd, mode):
            if mode == J.journal.fcntl.LOCK_EX:
                events.append('lock')
            return flock(fd, mode)
        def directory(path):
            events.append('directory')
            self.assertTrue(self.receipts.exists())
            fd = os.open(self.receipts, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    flock(fd, J.journal.fcntl.LOCK_EX | J.journal.fcntl.LOCK_NB)
            finally:
                os.close(fd)
            synced = []
            def syncing(fd):
                synced.append(os.path.samestat(os.fstat(fd), Path(path).stat()))
                return fsync(fd)
            with mock.patch.object(os, 'fsync', syncing):
                result = sync(path)
            self.eq(synced, [True], 'directory entry must reach os.fsync')
            return result
        with mock.patch.object(J.journal.fcntl, 'flock', locked), \
                mock.patch.object(J.journal, '_fsync_directory', directory):
            self.c.submit()
        self.assertIn('lock', events[:events.index('directory')])
        self.eq(events.count('directory'), 1)

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
        self.eq(self.row()['d5']['state'], 'Recorded')
        self.eq(len(self.merges()), before)


class TestOperator(unittest.TestCase):
    def test_ready_operator_merges_and_recovers_without_recorder_or_advance(self):
        c = legacy.TestMergeUnit()
        c.setUp()
        self.addCleanup(c.doCleanups)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, c.env))
            stack.enter_context(nullcontext())
            stack.enter_context(mock.patch.object(M, 'merge_launch_admitted', return_value=True))
            stack.enter_context(mock.patch.object(t.registry.CP, '_external_base', return_value=c.directory / 'external'))
            stack.enter_context(mock.patch.object(t.host, 'session_anchor', return_value=('native', t.ANCHOR)))
            stack.enter_context(mock.patch.object(t.admission.time, 'time', return_value=150))
            mandate = c.repo / 'docs/orchestrator-mandate.md'
            mandate.parent.mkdir(exist_ok=True)
            mandate.write_bytes(t.MANDATE)
            entry = t.ready(self, c.repo, c.plan['name'], c.state_dir)
            scope = dict(plans=[c.plan['name']], repositories=[c.remote], actions=['merge'], trackers=[])
            t.lease.grant(c.repo, c.plan['name'], c.state_dir, issued_at=100, confirmed_at=100,
                expires_at=200, mandate_bytes=t.MANDATE, scope=scope, owner_response='fixture', displayed_bounds='fixture')
            t.store.claim(entry['record_dir'], 101, t.MANDATE, 'native', t.ANCHOR, lambda _: 'alive')
            argv = [str(c.plan_path), '--state-dir', str(c.state_dir), '--unit', 'u', '--pr', '7', '--approver', 'Operator']
            self.assertEqual(M.main(argv), 0)
            before = tree_bytes(c.state_dir)
            self.assertEqual(M.main(argv), 0)
            self.assertEqual(tree_bytes(c.state_dir), before)
            forge = json.loads(Path(c.env['FORGE_STATE']).read_text())
            self.assertEqual(len(forge['merge_operations']), 1)
            self.assertEqual(json.loads((c.state_dir / legacy.S.MERGE_RECEIPTS).read_text())['integration_status'], 'candidate-verified')


class TestInactiveAndEntry(unittest.TestCase):
    def test_review_m6_matches_recorded_attempt_base(self):
        with mock.patch(__name__ + '.BASE', '1b61800ff263b4a10978afdbbdfc64b117195914'):
            self.test_inactive_embedded_base_and_inherited_scheduler_path()

    def test_installed_ready_operator_uses_its_shipped_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / 'installed'
            for name in ('hanig-orchestrate', 'hanig-swarm', 'hanig-project',
                         'hanig-review-gate', 'hanig-portable-handoff'):
                shutil.copytree(t.ROOT / 'skills' / name, installed / name,
                                ignore=shutil.ignore_patterns('__pycache__'))
            probe = r'''
import importlib, pathlib, sys, unittest
root = pathlib.Path(sys.argv[1]).resolve()
operator = root / 'hanig-orchestrate/scripts'
sys.path.insert(0, str(operator))
import merge_unit
assert pathlib.Path(merge_unit.__file__).parent == operator
# Load the shipped runtime before exposing the source-tree test fixtures.
for path in sorted(operator.glob('d5_*.py')):
    importlib.import_module(path.stem)
sys.path.append(sys.argv[2])
from tests.test_d5_u4_3_merge_provider import TestOperator
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
    TestOperator('test_ready_operator_merges_and_recovers_without_recorder_or_advance')]))
for name, module in list(sys.modules.items()):
    if name.startswith('d5_'):
        assert root in pathlib.Path(module.__file__).resolve().parents, (name, module.__file__)
sys.exit(not result.wasSuccessful())
'''
            env = {k: v for k, v in os.environ.items() if not k.startswith('HANIG_')}
            result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', probe,
                str(installed), str(t.ROOT)], cwd=tmp, env=env,
                capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
