"""U3-R10: acceptance through the composed R1--R9 helpers.

Supported envelope and accepted limits
-------------------------------------
Envelope/limits: Deterministic local test harness; no claim of deployed
integration or performance.
Non-claims: no repository-wide call-flow exclusivity claim and no static
“arbitrary text” guard.

Trusted local participants, stable record/lock inodes, registered checkout at
its original path, explicit fixture adapters, and managed exec children.
Host identity, activation and time are synthetic in active scenarios; registry,
store, ledger, admission, protocol locks and subprocess consumers are real.
The default-inactive checks restore the unpatched activation function. R1 uses
authority.start.lock / authority.drain.lock and may repair owner modes to 0600;
STATE remains store-owned authority.lock. Events/pipes establish ordering;
nonblocking flock probes establish exclusion. Timeouts are watchdogs.

Original M6: "same-authority nested admission completes with distinct operation
accounting; peer START ownership remains excluded."
Amendment (ARC-2871-OD8/OD10): "Same-authority nested submission under caller-held
START refuses before validation, preparation or Start, leaving no boundary call
and no ledger row. When the caller holds DRAIN and START, the refusal is
`D5 submission inside START is forbidden`. When the caller holds START without
DRAIN, R1's acquisition-order refusal is raised first. Sequential same-authority
submissions complete with distinct operation accounting. Peer START ownership
remains excluded."

ARC-2875-OD1 replaces inactive/no-protocol-I/O with the predecessor's measured
bookkeeping: R7 adopts before refusing; R4 accounting pins before refusing;
worker_spawn is a separate role without activation and exports no descriptors.
ARC-3636-OD1 retains completion's active requirement: deactivation refuses with
`D5 inactive`, preserves entry bytes, and reactivation permits the same receipt.
ARC-2875-OD2 retains the merged per-entry inactive messages below.

Remaining obligations (not claims):
1. U4 to U7 add their designated bootstraps, which construct their named trusted
   adapters, to DESIGNATED_BOOTSTRAPS (ARC-3637-OD1).
2. U8 activation review reassesses a successor-installation barrier keyed on
   unresolved intents (ARC-2871-OD1 carry-forward).
3. U8 decides whether a submitted entry whose holders have all died should report
   reconciliation-required (ARC-2871-OD3 carry-forward).
4. The mandate source is the registered checkout's working tree, not the
   owner-merged blob (ARC-2868-OD18 non-claim). U8 decides whether to compare them.
5. The renamed-checkout refusal is deferred in docs/deferred-edge-cases.md,
   with Sol's concurrence (ARC-2870).
6. Normalize inactive refusal messages, or amend the adopted refusal table,
   before U8 activation (ARC-2875-OD2).

The unchanged tests.test_d5_authority_store is run separately, without these
fixtures' patches. Independent mutations are in fixtures/d5/r10_mutations.py.
"""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock

from tests import test_d5_submission as t
from tests import test_d5_process as p
import d5_custody as custody
import d5_install as install

DEFAULT_ACTIVE = lambda: False  # Explicit pre-cutover fixture, not the production default.
PEER = str(Path(__file__).parent / 'fixtures/d5/r10_peer.py')


class TestAcceptance(unittest.TestCase):
    eq = unittest.TestCase.assertEqual
    event = p.TestProcess.event
    send = p.TestProcess.send
    own = p.TestProcess.own
    finish = p.TestProcess.finish

    def setUp(self):
        self.assertIs(DEFAULT_ACTIVE(), False)
        self.eq(p.process.DESIGNATED_BOOTSTRAPS, frozenset())
        self.c = t.TestSubmission()
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()  # Includes a fresh pooled-lock map, restored by cleanup.
        c = self.c
        self.data = dict(args=list(map(str, c.args)), directory=str(c.directory),
            request={k: v for k, v in c.request.items() if k != 'anchor'},
            scope=c.operation.scope.decode(), external=str(c.root / 'external'))

    def snapshot(self):
        return {str(f.relative_to(self.c.root)): f.read_bytes()
                for f in self.c.root.rglob('*.json')}

    def worker(self, action, data):
        return self.own(p.process.worker_spawn(
            [sys.executable, p.PEER, action, json.dumps(data)]))

    def protected(self, fd):
        with p.designated():
            return self.own(p.process.protected_spawn(self.c.directory, fd,
                p.BOOTSTRAP, ['child', json.dumps(self.data), str(fd), 'submitted']))

    def ordered_control(self, invalidation):
        c = self.c
        ready, resume = threading.Event(), threading.Event()
        start = t.protocol._Protocol.start

        @contextmanager
        def paused(record):
            if threading.current_thread() is not threading.main_thread():
                ready.set()
                if not resume.wait(20):
                    raise AssertionError('request resume watchdog')
            with start(record):
                yield

        with mock.patch.object(t.protocol._Protocol, 'start', paused):
            thread, results = c.launch(c.submit)
            try:
                self.assertTrue(ready.wait(20), 'request entry watchdog')
                self.eq(c.boundary.calls, [])
                self.eq(c.rows(), [])
                if invalidation:
                    c.mandate.write_bytes(b'new policy')
                    self.eq(t.control.record_mandate_invalidation(*c.args, now=151),
                            'mandate_changed')
                    # Reverting raw bytes must not undo the recorded invalidation.
                    c.mandate.write_bytes(t.MANDATE)
                else:
                    t.lease.revoke(*c.args, 151, 'stop')
                record = t.store.read(c.directory)
                self.eq(record['invalidation']['kind'],
                        'mandate_changed' if invalidation else 'revoked')
            finally:
                resume.set()
                result = c.join(thread, results)
        self.assertIsInstance(result, t.authority.AuthorityError)
        self.eq(str(result), 'D5 authority is invalidated')
        self.eq(c.boundary.calls, [])
        self.eq(c.rows(), [])

    def test_M1_revocation_before_admission(self):
        self.ordered_control(False)

    def test_M2_recorded_mandate_change_before_admission(self):
        self.ordered_control(True)

    def response_wait(self, after_revoke=False):
        c = self.c
        waiting, release, persisted = (threading.Event() for _ in range(3))

        def finish():
            waiting.set()
            if not release.wait(20):
                raise AssertionError('response watchdog')
            self.assertTrue(persisted.is_set())
            return dict(t.OK)

        c.boundary.on_finish = finish
        thread, results = c.launch(c.submit)
        try:
            self.assertTrue(waiting.wait(20), 'Finish entry watchdog')
            row, = c.rows()
            self.eq(row['state'], 'submitted')
            self.eq(c.probe('drain'), 'blocked')
            # This measured probe catches Finish-under-START without deadlocking
            # a later synchronous control call in a mutant.
            self.eq(c.probe('start'), 'available')
            t.lease.revoke(*c.args, 151, 'response still outstanding')
            self.eq(t.store.read(c.directory)['invalidation']['kind'], 'revoked')
            persisted.set()
            self.assertFalse(release.is_set())
            self.eq(c.rows()[0]['state'], 'submitted')
        finally:
            release.set()
            result = c.join(thread, results)
        self.assertIsInstance(result, t.submission.Result)
        self.eq(result.outcome, t.OK)
        row, = c.rows()
        self.eq(row['effect_id'], result.effect_id)
        self.eq(row['state'], 'completed')
        self.eq(row['outcome'], t.OK)
        self.eq(c.probe('drain'), 'available')
        if after_revoke:
            before = self.snapshot()
            c.operation = t.submission.Operation('B', 'fixture', c.operation.scope, b'B')
            c.boundary = t.DeterministicBoundary()
            with self.assertRaisesRegex(t.authority.AuthorityError,
                                        '^D5 authority is invalidated$'):
                c.submit()
            self.eq(c.boundary.calls, [])
            self.eq(self.snapshot(), before)

    def test_M3_completion_after_revocation_and_B_refusal(self):
        self.response_wait(after_revoke=True)

    def test_M4_revocation_persists_before_response_release(self):
        self.response_wait()

    def test_M5_surviving_child_blocks_successor_until_accounted_and_closed(self):
        c = self.c
        ancestor = self.worker('ancestor', self.data)
        self.event(ancestor, 'adopted')
        original = (c.directory / t.store.RECORD_NAME).read_bytes()
        ancestor.kill()
        ancestor.wait(timeout=20)
        self.assertLess(ancestor.returncode, 0)
        self.send(ancestor, 'request')
        returned = self.event(ancestor, 'returned')
        self.eq(returned['error'], 'D5 readiness: caller-held coordinator lease required')
        self.assertTrue(returned['retained'])
        self.eq(returned['calls'], [])
        self.eq(c.rows(), [])
        # The successor owns the lease before waiting on the surviving DRAIN.
        t.submission.readiness.state.release_lease(c.state_dir)
        installer = self.worker('installer', self.data)
        self.assertTrue(self.event(installer, 'exclusive-probe')['blocked'])
        self.eq((c.directory / t.store.RECORD_NAME).read_bytes(), original)
        self.eq(c.probe('start'), 'available')
        self.eq(c.probe('drain'), 'blocked')
        self.send(ancestor, 'exit')
        self.event(installer, 'installed')
        out, err = installer.communicate(timeout=20)
        self.eq((installer.returncode, out, err), (0, b'', b''))
        self.eq(t.store.read(c.directory)['grant_id'], 'b' * 32)
        self.eq(c.rows(), [])
        self.eq(ancestor.communicate(timeout=20), (b'', b''))

    def test_M6_ambient_START_refusals_and_sequential_accounting(self):
        c = self.c
        for drain in (True, False):
            with ExitStack() as stack:
                record = stack.enter_context(t.protocol.pin(c.directory))
                if drain:
                    stack.enter_context(record.drain())
                stack.enter_context(record.start())
                message = ('D5 submission inside START is forbidden' if drain
                           else 'D5 lock refused: acquisition order')
                kind = t.authority.AuthorityError if drain else t.protocol.LockError
                with mock.patch.object(t.admission, '_validate_locked',
                        wraps=t.admission._validate_locked) as validate, \
                        mock.patch.object(t.ledger, 'prepare', wraps=t.ledger.prepare) as prepare:
                    with self.assertRaisesRegex(kind, '^' + message + '$'):
                        c.submit()
                    validate.assert_not_called()
                    prepare.assert_not_called()
                self.eq(c.probe('start'), 'blocked')
                self.eq(c.boundary.calls, [])
                self.eq(c.rows(), [])
        results = {}
        for name in ('A', 'B'):
            c.operation = t.submission.Operation(name, 'fixture', c.operation.scope, name.encode())
            c.boundary = t.DeterministicBoundary()
            c.boundary.on_start = lambda: self.eq(c.probe('start'), 'blocked')
            results[name] = c.submit()
            self.eq(c.boundary.calls, [('start', c.operation), ('finish',)])
        self.assertNotEqual(results['A'].effect_id, results['B'].effect_id)
        self.eq(len(c.rows()), 2)
        for row in c.rows():
            name = row['binding']['correlation']['request_id']
            self.eq(row['effect_id'], results[name].effect_id)
            self.eq(row['binding']['correlation']['intent'], name.encode().hex())
            self.eq(row['binding']['record_id'], c.request['grant_id'])
            self.eq(row['binding']['scope'], c.scope)
            self.eq(row['state'], 'completed')
            self.eq(row['outcome'], t.OK)
        self.eq(c.probe('start'), 'available')
        self.eq(c.probe('drain'), 'available')

    def test_M7_live_worker_inherits_no_protection(self):
        with ExitStack() as stack:
            record = stack.enter_context(t.protocol.pin(self.c.directory))
            fd = stack.enter_context(record.drain())
            alias = os.dup(fd)
            stack.callback(os.close, alias)
            for n in (fd, alias):
                os.set_inheritable(n, True)
            identities = {str(n): p.identity(n) for n in (fd, alias)}
            with mock.patch.object(p.process.subprocess, 'Popen', wraps=subprocess.Popen) as spawn:
                proc = self.worker('worker', [fd, alias])
            self.eq(spawn.call_args.kwargs['pass_fds'], ())
            self.assertIs(spawn.call_args.kwargs['close_fds'], True)
            observed = self.event(proc, 'ready')['observed']
            for n, identity in identities.items():
                self.assertNotEqual(observed[n], identity)
        self.assertIsNone(proc.poll())
        self.eq(self.c.probe('drain'), 'available')
        self.assertIsNone(proc.poll())
        self.finish(proc)

    def test_M8_final_expiry_equality_after_preparation(self):
        c = self.c
        prepare = t.ledger.prepare
        prepared = []

        def at_expiry(*args, **kwargs):
            attempt = prepare(*args, **kwargs)
            prepared.append(attempt.effect_id)
            self.eq(c.rows()[0]['state'], 'attempting')
            c.clock.return_value = c.original['expires_at']
            return attempt

        with mock.patch.object(t.ledger, 'prepare', side_effect=at_expiry):
            with self.assertRaisesRegex(t.authority.AuthorityError, '^D5 authority expired$'):
                c.submit()
        row, = c.rows()
        self.eq(prepared, [row['effect_id']])
        self.eq(row['state'], 'not_submitted')
        self.eq(c.boundary.calls, [])
        self.eq(c.probe('drain'), 'available')

    def test_M9_launched_helper_must_admit_after_control(self):
        c = self.c
        with t.protocol.pin(c.directory) as record, record.drain() as fd:
            proc = self.protected(fd)
            self.assertFalse(self.event(proc, 'adopted')['inheritable'])
            self.eq(c.rows(), [])
            t.lease.revoke(*c.args, 151, 'after launch, before request')
            before = self.snapshot()
        self.send(proc, 'request')
        result = self.event(proc, 'returned')
        self.eq(result['error'], 'D5 readiness: caller-held coordinator lease required')
        self.eq(result['calls'], [])
        self.assertTrue(result['retained'])
        self.eq(self.snapshot(), before)
        self.eq(c.probe('drain'), 'blocked')
        self.finish(proc)
        self.eq(c.probe('drain'), 'available')

    def test_inactive_entry_table_and_adopted_bookkeeping(self):
        c = self.c
        ordinary = [
            lambda: t.control.revoke(*c.args, 151, 'stop'),
            lambda: t.control.record_mandate_invalidation(*c.args, now=151),
            lambda: t.control.relinquish(*c.args, now=151),
            lambda: install.install_initial(c.directory, c.original),
            lambda: install.replace_authority(c.directory, c.original),
            lambda: install.claim_custody(c.directory, mandate_path=c.mandate),
            lambda: t.admission._validate_locked(None, *c.args, **c.request,
                                                 scope=c.operation.scope),
            c.submit]
        routed = [
            lambda: t.lease.revoke(*c.args, 151, 'stop'),
            lambda: custody.claim(*c.args, now=151, mandate_bytes=t.MANDATE),
            lambda: custody.relinquish(*c.args, now=151),
            lambda: t.lease.grant(*c.args, issued_at=100, confirmed_at=100,
                expires_at=200, mandate_bytes=t.MANDATE, scope=c.scope,
                owner_response='fixture', displayed_bounds='fixture')]
        table = [(call, t.authority.AuthorityError, 'D5 is inactive') for call in ordinary]
        table += [(call, t.authority.AuthorityError, 'D5 is not active') for call in routed]
        table += [(lambda: t.completion.bind(None), t.ledger.EffectError, 'D5 inactive'),
            (lambda: p.process.protected_spawn(c.directory, -1, p.BOOTSTRAP),
             t.authority.AuthorityError, 'D5 protected bootstrap is not designated')]
        before = self.snapshot()
        with mock.patch.object(t.registry, 'd5_active', DEFAULT_ACTIVE):
            self.assertIs(t.registry.d5_active(), False)
            self.eq(p.process.DESIGNATED_BOOTSTRAPS, frozenset())
            for index, (call, kind, message) in enumerate(table):
                with self.subTest(entry=index), \
                        mock.patch.object(t.protocol, 'pin', wraps=t.protocol.pin) as pin, \
                        mock.patch.object(t.registry, 'lookup', wraps=t.registry.lookup) as lookup, \
                        mock.patch.object(p.process.subprocess, 'Popen') as spawn:
                    with self.assertRaisesRegex(kind, '^' + message + '$'):
                        call()
                    pin.assert_not_called()
                    lookup.assert_not_called()
                    spawn.assert_not_called()
                    self.eq(self.snapshot(), before)
            with t.protocol.pin(c.directory) as record, record.drain() as fd:
                result = subprocess.run([sys.executable, PEER, str(c.directory), str(fd)],
                    pass_fds=(fd,), close_fds=True, capture_output=True, timeout=20)
                self.eq(result.returncode, 0, result.stderr.decode())
                self.eq(result.stderr, b'')
                self.eq(json.loads(result.stdout), dict(error='D5 is inactive', pins=1,
                    duplicates=1, before=True, after=False, retained=True, calls=[]))
            with mock.patch.object(p.process.subprocess, 'Popen', wraps=subprocess.Popen) as spawn:
                proc = self.worker('worker', [])
            self.eq(spawn.call_args.kwargs['pass_fds'], ())
            self.assertIs(spawn.call_args.kwargs['close_fds'], True)
            self.event(proc, 'ready')
            self.finish(proc)
        self.eq(self.snapshot(), before)
        self.eq(c.boundary.calls, [])
        self.eq(c.rows(), [])

    def test_deactivated_completion_preserves_bytes_then_recovers(self):
        c = self.c
        request = dict(record_id=c.request['grant_id'],
            custody=dict(generation=1, session_id='native', anchor_pid=t.ANCHOR.pid,
                         anchor_start_us=t.ANCHOR.start_us),
            kind='fixture', scope=c.scope, correlation={'request_id': 'local'})
        receipt = t.ledger._classify(t.ledger.prepare(*c.args, **request), 'submitted')
        attempt = t.ledger.prepare(*c.args, **dict(request, correlation={'request_id': 'uncertain'}))
        cap, uncertain = t.completion.bind(receipt), t.completion.bind(attempt)
        before = self.snapshot()
        with mock.patch.object(t.registry, 'd5_active', DEFAULT_ACTIVE), \
                mock.patch.object(t.protocol, 'pin', wraps=t.protocol.pin) as pin:
            for call in (lambda: cap.complete(t.OK), uncertain.uncertain):
                with self.assertRaisesRegex(t.ledger.EffectError, '^D5 inactive$'):
                    call()
                self.eq(self.snapshot(), before)
            self.eq(pin.call_count, 2)
        self.eq(cap.complete(t.OK), t.OK)
        self.assertIsNone(uncertain.uncertain())
        self.eq({r['effect_id']: r['state'] for r in c.rows()},
                {receipt.effect_id: 'completed', attempt.effect_id: 'unknown'})


if __name__ == '__main__':
    unittest.main()
