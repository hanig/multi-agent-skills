"""R1-R7: ordered D5 mechanisms with stub services, never OS isolation.

Registration/readiness and host observations are fixture inputs. Admission,
START/DRAIN, authority records, submission, merge journal and receipt readback
remain real. Temporary storage holds logical records across simulated death;
no oracle depends on source text, path spelling or filesystem layout.
"""
from contextlib import nullcontext
from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/hanig-orchestrate/scripts'))
sys.path.insert(0, str(ROOT / 'skills/hanig-project/scripts'))
import merge_unit as M
import d5_activation as activation
import d5_admission as admission
import d5_authority as authority
import d5_authority_store as store
import d5_control as control
import d5_host as host
import d5_merge_journal as J
import d5_protocol_locks as protocol
import d5_readiness as readiness
import d5_registry as registry
import d5_submission as submission
import linear_api
import remote_verify
import swarm_outbox as outbox
import swarm_state as state
import unit
from tests.fixtures.d5.ready import ready_installation, missing_installation


class World:
    """External services are stubs; persisted authority and journal are real."""
    def __init__(self, case):
        self.case = case
        self.stack = ExitStack()
        case.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.directory, self.state = self.root / 'authority', self.root / 'state'
        self.directory.mkdir(mode=0o700)
        self.state.mkdir()
        self.now = 150
        self.effects, self.children, self.holders = [], [], set()
        self.merged = False
        self.on_observe = lambda: None
        self.on_wait = lambda: None
        self.mandate = b'fixture authority'
        self.anchor = host.ProcessIdentity(42, 9000001)
        self.other = host.ProcessIdentity(43, 9000002)
        self.binding = dict(unit='u', attempt='a', head='a' * 40,
                            repo='https://github.com/example/project', target='main', pr=7)
        self.scope = dict(plans=['p'], repositories=[self.binding['repo']], actions=['merge'], trackers=[])
        self.effect = hashlib.sha256(json.dumps(self.binding, sort_keys=True).encode()).hexdigest()
        self.intent = dict(schema_version=1, operation_id=self.effect, binding=self.binding,
            root=str(self.root / 'runs'), phase='merge_requested', target_before_request='b' * 40,
            preconditions=dict(integration=dict(target_commit='b' * 40, subject_head='a' * 40,
                                               result='pass')))
        self.args = SimpleNamespace(pr=7, plan='fixture', state_dir=str(self.state), unit='u',
            verify_integration=False, abandon_intent=None, retrieve_remote_evidence=False,
            remote_recovery_attestation=None)
        self.cmd = M.commands(self.args, self.intent['root'], self.binding, 'github.com', 'example/project')
        self.operation = submission.Operation(self.effect, 'merge', J.encoded(self.scope), J.encoded(self.intent))
        with protocol.pin(self.directory) as pinned:
            key = list(pinned._record.key)
        self.ready = dict(registration_id='1' * 32, plan_id='p', state_dir=str(self.state),
            record_dir=str(self.directory), repo_key=[1, 2], state_key=[1, 3], record_key=key,
            schema_version=1, generation='2' * 32, profile=readiness.PROFILE)
        self.ready.update(state_key=[self.state.stat().st_dev, self.state.stat().st_ino])
        ready_installation(self.root, self.ready)
        self.patch(registry, 'lookup', return_value=dict(self.ready, checkout=str(self.root)))
        self.patch(readiness, 'require', side_effect=self.installed)
        self.patch(admission, '_mandate_bytes', return_value=self.mandate)
        self.patch(host, 'verify_anchor', return_value=True)
        self.patch(admission.time, 'time', side_effect=lambda: self.now)
        self.patch(M, 'retained_integration_problem', return_value=None)
        self.patch(J, 'PROVIDER', J.MergeProvider())
        ok, reason = state.acquire_lease(self.state, bump_epoch=False)
        case.assertTrue(ok, reason)
        self.stack.callback(state.release_lease, self.state)
        record = dict(schema_version=1, grant_id='a' * 32, issued_at=100, confirmed_at=100,
            expires_at=200, session_cap=1, mandate_sha256=authority.mandate_digest(self.mandate),
            scope=self.scope, confirmation=dict(owner_response='fixture yes', displayed_bounds='fixture bounds'),
            custody=None, invalidation=None)
        store.create(self.directory, record)
        self.claim('A', self.anchor)
        # Park a real legacy outbox item; compare its exact contents at teardown.
        with missing_installation(self.ready):
            outbox.emit_intent(self.state, 'p', 'u', 'SUBMITTED',
                               dict(attempt_dir='/fixture/attempt', job_id='1'))
        self.outbox_before = (self.state / outbox.OUTBOX).read_bytes()
        self.tracker = self.patch(linear_api, 'transport', side_effect=AssertionError('tracker invoked'))
        self.wire = self.patch(urllib.request, 'urlopen', side_effect=AssertionError('network invoked'))
        self.patch(socket, 'create_connection', side_effect=AssertionError('socket invoked'))
        self.patch(subprocess, 'Popen', side_effect=self.launch)
        self.patch(subprocess, 'run', side_effect=self.read)
        self.patch(M, '_bounded_observation', side_effect=self.read)
        for name in ('call', 'check_call', 'check_output', 'getoutput', 'getstatusoutput'):
            self.patch(subprocess, name, side_effect=AssertionError('unexpected subprocess'))
        for name in ('system', 'popen', 'fork', 'forkpty', 'posix_spawn', 'posix_spawnp'):
            if hasattr(os, name):
                self.patch(os, name, side_effect=AssertionError('unexpected process'))
        self.patch(admission.time, 'sleep', side_effect=AssertionError('unexpected sleep'))
        # Track only submission's retained duplicates, to simulate process exit.
        local_os = mock.Mock(wraps=os)
        local_os.dup.side_effect = self.duplicate
        local_os.close.side_effect = self.close
        self.patch(submission, 'os', local_os)
        self.stack.callback(self.drop_holders)
        self.boundary = self.new_boundary()
        self.stack.callback(self.invariant)

    def patch(self, owner, name, *args, **kwargs):
        return self.stack.enter_context(mock.patch.object(owner, name, *args, **kwargs))

    def installed(self, directory):
        readiness.lease_required(directory)
        return copy.deepcopy(self.ready)

    def claim(self, session, anchor):
        record = store.claim(self.directory, self.now, self.mandate, session, anchor, lambda _: 'dead')
        self.request = dict(grant_id=record['grant_id'], session_id=session, anchor=anchor,
                            generation=record['custody']['generation'])

    def duplicate(self, fd):
        duplicate = os.dup(fd)
        self.holders.add(duplicate)
        return duplicate

    def close(self, fd):
        os.close(fd)
        self.holders.remove(fd)

    def drop_holders(self):
        for fd in tuple(self.holders):
            self.close(fd)

    def die(self):
        # Simulated death only; production must exit (ARC-3762-OD1).
        self.drop_holders()
        state.release_lease(self.state)
        self.boundary = None
        J.PROVIDER = J.MergeProvider()  # Discard all old provider/capability state.
        ok, reason = state.acquire_lease(self.state, bump_epoch=False)
        self.case.assertTrue(ok, reason)
        self.boundary = self.new_boundary()

    def invariant(self):
        self.case.assertEqual(self.tracker.call_count, 0)
        self.case.assertEqual(self.wire.call_count, 0)
        self.case.assertEqual((self.state / outbox.OUTBOX).read_bytes(), self.outbox_before)

    def new_boundary(self):
        return M._MergeBoundary(self.state, self.binding, str(self.root), 'github.com',
                                'example/project', self.cmd, self.intent)

    def row(self):
        return M.current_intent(self.state, self.binding, self.intent['root'], repair=False)[2]

    def receipts(self):
        rows, problems = M.S.load_merge_receipts(self.state)
        self.case.assertEqual(problems, [])
        return rows

    def submit(self, **request):
        return submission.submit(self.root, 'p', self.state, **dict(self.request, **request),
                                 operation=self.operation, boundary=self.boundary)

    def enter(self):
        # Same admission consumer as the operator, then the real R6 lifecycle.
        M._d5_merge_entry(self.args)
        return self.submit()

    def read(self, argv, **kwargs):
        if argv == self.cmd['target']:
            if kwargs.get('timeout') == 60:
                self.on_observe()
            value = dict(ref='refs/heads/main', object=dict(type='commit', sha='b' * 40))
        elif argv == self.cmd['view']:
            value = dict(number=7, url=self.binding['repo'] + '/pull/7', baseRefName='main',
                headRefOid='a' * 40, state='MERGED' if self.merged else 'OPEN',
                mergeCommit=dict(oid='c' * 40) if self.merged else None)
        elif argv == M.parent_command('github.com', 'example/project', 'c' * 40):
            value = dict(sha='c' * 40, parents=[dict(sha='b' * 40)])
        else:
            raise AssertionError('unexpected command: ' + repr(argv))
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), '')

    def launch(self, argv, **kwargs):
        self.case.assertEqual(argv, self.cmd['merge'])
        self.effects.append(tuple(argv))
        child = SimpleNamespace(options=kwargs, returncode=0, alive=True)
        def communicate(**unused):
            self.on_wait()
            child.alive = False
            self.merged = True
            return '', ''
        child.communicate = communicate
        child.kill = lambda: None  # A local kill never certifies remote completion.
        self.children.append(child)
        return child

    def recover(self):
        with J.recovery_drain(self.state):
            return J.recover(self.state, self.row(), self.boundary.completion())


class TestD5U7Races(unittest.TestCase):
    def test_R1_competing_merge_sessions(self):
        w = World(self)
        def contender():
            with self.assertRaisesRegex(authority.AuthorityError, '^D5 custody is not current$'):
                w.submit(session_id='B', anchor=w.other)
            self.assertEqual(w.effects, [])
        w.on_observe = contender  # A has prepared; B attempts before A launches.
        result = w.submit()
        self.assertEqual(result.initiation, submission.Initiation.SUBMITTED)
        self.assertEqual(len(w.effects), 1)
        self.assertEqual(w.row()['d5']['state'], 'Recorded')
        self.assertEqual(len(w.receipts()), 1)

    def test_R2_expiry_and_revocation_fence_then_successor(self):
        # Expiry is grant expiry, never a TTL on the coordinator flock.
        for mode in ('expiry', 'revocation'):
            for in_gap in (False, True):
                with self.subTest(mode=mode, in_gap=in_gap):
                    w = World(self)
                    try:
                        def fence():
                            if mode == 'expiry':
                                w.now = 200
                            else:
                                control.revoke(w.root, 'p', w.state, w.now, 'stop')
                        if in_gap:
                            w.on_observe = fence
                        else:
                            fence()
                        reason = 'expired' if mode == 'expiry' else 'is invalidated'
                        with self.assertRaisesRegex(authority.AuthorityError, 'D5 authority ' + reason):
                            w.submit()
                        self.assertEqual(w.effects, [])
                        self.assertEqual(w.receipts(), [])
                        if in_gap:
                            self.assertEqual(w.row()['d5']['classification'], 'not_submitted')
                            # U4.5 deliberately retains this unresolved request;
                            # there is no safe automatic retry seam. U8 finding.
                            continue
                        # No intent exists: a fresh confirmed grant and observed
                        # dead holder allow B to claim and complete normally.
                        record = store.read(w.directory)
                        record.update(grant_id='b' * 32, issued_at=w.now, confirmed_at=w.now,
                                      expires_at=w.now + 100, invalidation=None)
                        record['confirmation']['owner_response'] = 'fresh fixture consent'
                        store.replace(w.directory, record)
                        w.claim('B', w.other)
                        self.assertEqual(w.request['generation'], 2)
                        w.submit()
                        self.assertEqual(len(w.effects), 1)
                        self.assertEqual(len(w.receipts()), 1)
                    finally:
                        w.stack.close()

    def test_R3_cancellation_fencing(self):
        # U4.6: "Pre-START/no-invocation refusal uses only the existing
        # authoritative no-effect release conditions." Also: "Cancellation
        # after dispatch does not manufacture a no-effect outcome."
        w = World(self)
        def cancel():
            raise KeyboardInterrupt('cancel before launch')
        w.on_observe = cancel
        with self.assertRaisesRegex(KeyboardInterrupt, 'cancel before launch'):
            w.submit()
        self.assertEqual(w.effects, [])
        self.assertEqual(w.row()['d5']['classification'], 'not_submitted')
        self.assertEqual(w.holders, set())
        w.stack.close()
        w = World(self)
        w.submit()
        before, receipts = w.row(), w.receipts()
        with self.assertRaisesRegex(M.Refusal, 'D5 active: cancel_before_request'):
            M.cancel_before_request(w.state / ('merge-unit-' + w.effect + '.json'),
                                    before, None, 'cancel after completion')
        self.assertEqual(w.row(), before)
        self.assertEqual(w.receipts(), receipts)
        with self.assertRaisesRegex(authority.AuthorityError, 'unresolved request cannot replay'):
            w.submit()
        self.assertEqual(len(w.effects), 1)

    def test_R4_process_death_and_persisted_recovery(self):
        w = World(self)
        append = J.journal._fsync_append
        def crash(*args, **kwargs):
            append(*args, **kwargs)
            raise KeyboardInterrupt('death after receipt before Recorded')
        with mock.patch.object(J.journal, '_fsync_append', side_effect=crash):
            with self.assertRaisesRegex(KeyboardInterrupt, 'death after receipt'):
                w.submit()
        self.assertEqual(w.row()['d5']['state'], 'Attempted')
        self.assertEqual(len(w.receipts()), 1)
        w.die()
        result = w.recover()
        self.assertEqual(w.row()['d5']['state'], 'Recorded')
        self.assertEqual(w.recover(), result)
        self.assertEqual(len(w.receipts()), 1)
        with self.assertRaisesRegex(authority.AuthorityError, 'unresolved request cannot replay'):
            w.submit()
        self.assertEqual(len(w.effects), 1)

    def test_R5_request_plan_identity_mismatch(self):
        w = World(self)
        scope = dict(w.scope, plans=['other-plan'])
        with protocol.pin(w.directory) as pinned, pinned.drain():
            with self.assertRaisesRegex(authority.AuthorityError, 'plan mismatch'):
                J.PROVIDER.prepare(w.root, 'other-plan', w.state, kind='merge', scope=scope,
                    correlation=dict(request_id=w.effect, intent=w.operation.intent.hex()),
                    record_id=w.request['grant_id'], custody={})
        self.assertEqual(w.effects, [])
        self.assertIsNone(w.row())
        w.submit()  # Matching identity remains usable.
        self.assertEqual(len(w.effects), 1)

    def test_R6_admission_default_override_and_restore(self):
        w = World(self)
        self.assertIs(M.merge_launch_admitted(), False)
        with self.assertRaisesRegex(M.Refusal, 'merge launch is not admitted'):
            w.enter()
        self.assertEqual(w.effects, [])
        with mock.patch.object(M, 'merge_launch_admitted', return_value=True):
            w.enter()
        self.assertEqual(len(w.effects), 1)
        self.assertIs(M.merge_launch_admitted(), False)
        with self.assertRaisesRegex(M.Refusal, 'merge launch is not admitted'):
            w.enter()
        self.assertEqual(len(w.effects), 1)

    def test_R7_surviving_merge_child_containment(self):
        w = World(self)
        def death():
            raise KeyboardInterrupt('parent death while child survives')
        w.on_wait = death
        names = ('D5_AUTHORITY_FD', 'HANIG_D5_COMPLETION', 'CLAUDE_CODE_SESSION_ID',
                 'CLAUDE_PID', 'LINEAR_API_KEY', 'OPENAI_API_KEY', 'SSH_AUTH_SOCK')
        with mock.patch.dict(os.environ, dict.fromkeys(names, 'synthetic')):
            with self.assertRaisesRegex(KeyboardInterrupt, 'parent death'):
                w.submit()
        child, = w.children
        self.assertTrue(child.alive)
        self.assertIs(child.options['shell'], False)
        self.assertIs(child.options['close_fds'], True)
        self.assertNotIn('pass_fds', child.options)
        self.assertFalse(set(names) & set(child.options['env']))
        self.assertEqual(w.row()['d5']['classification'], 'unknown')
        self.assertEqual(w.receipts(), [])
        w.die()
        with self.assertRaisesRegex(M.Refusal, 'merge not observed'):
            w.recover()
        with self.assertRaisesRegex(authority.AuthorityError, 'unresolved request cannot replay'):
            w.submit()
        self.assertEqual(w.row()['d5']['classification'], 'unknown')
        self.assertEqual(w.receipts(), [])
        entries = [(unit.main, ['unit', 'allocate', '--root', str(w.root), '--task', 'x', '--kind', 'code']),
                   (unit.main, ['unit', 'bind', str(w.root), '--job-id', '1']),
                   (unit.main, ['unit', 'check', str(w.root)]),
                   (remote_verify.main, ['remote_verify', '--worker', str(w.root)]),
                   (remote_verify.main, ['remote_verify', '--supervise', str(w.root)])]
        for main, argv in entries:
            with self.subTest(entry=argv[:2]), mock.patch.object(sys, 'argv', argv):
                with self.assertRaisesRegex(SystemExit, 'D5 active: .*refused before first effect'):
                    main()
        child.alive = False
        w.merged = True  # Stub forge now observes the original child's effect.
        w.recover()
        self.assertEqual(w.row()['d5']['state'], 'Recorded')
        self.assertEqual(len(w.receipts()), 1)
        self.assertEqual(len(w.effects), 1)


if __name__ == '__main__':
    unittest.main()
