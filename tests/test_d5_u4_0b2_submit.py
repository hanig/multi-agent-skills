"""U4.0b2: submit barriers and read-only successor gates through public entries."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import unittest
from unittest import mock

from tests import test_d5_submission as t
from tests.test_d5_u4_0b1_issuance import no_effects
import d5_install as install
import d5_readiness as readiness
import swarm_state as state
import merge_unit


class TestSubmitBarrier(unittest.TestCase):
    def setUp(self):
        self.c = t.TestSubmission()
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()
        self.marker = self.c.state_dir / readiness.MARKER

    def refused_without_effects(self, message):
        with no_effects() as observed:
            with self.assertRaisesRegex(t.authority.AuthorityError, message):
                self.c.submit()
            self.assertEqual(observed, ([], []))
        self.assertEqual(self.c.rows(), [])
        self.assertEqual(self.c.boundary.calls, [])

    def test_submit_without_lease_before_readiness(self):
        state.release_lease(self.c.state_dir)
        with mock.patch.object(readiness, '_read', side_effect=AssertionError('read')) as read:
            self.refused_without_effects('caller-held coordinator lease required')
            read.assert_not_called()

    def test_submit_readiness_before_lookup_zero_launches_and_writes(self):
        self.marker.unlink()
        # no_effects instruments all subprocess/os launch primitives, even
        # attempts whose exceptions a caller catches, and transient writes.
        with mock.patch.object(t.registry, 'lookup', side_effect=AssertionError('lookup')) as lookup:
            self.refused_without_effects('missing or malformed installation state')
            lookup.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_submit_validates_marker_installation_and_registration_binding(self):
        raw = self.marker.read_bytes()
        installed = self.c.directory / readiness.INSTALLATION
        for bad in (b'{}', b'null', b'{"schema_version":1,"schema_version":1}'):
            self.marker.write_bytes(bad)
            with self.subTest(bad=bad):
                self.refused_without_effects('D5 readiness:')
        self.marker.write_bytes(raw)
        value = json.loads(raw)
        value['generation'] = 'f' * 32
        installed.write_text(json.dumps(value))
        self.refused_without_effects('generation or binding mismatch')
        installed.write_bytes(raw)
        entry = dict(self.c.entry, registration_id='f' * 32)
        with mock.patch.object(t.registry, 'lookup', return_value=entry):
            self.refused_without_effects('registration binding mismatch')

    def test_submit_canonical_lease_and_final_recheck(self):
        alias = self.c.root / 'state-alias'
        alias.symlink_to(self.c.state_dir, target_is_directory=True)
        self.c.args = self.c.repo, 'p', alias
        validate = t.admission._validate_locked
        calls = []

        def validated(*args, **kwargs):
            result = validate(*args, **kwargs)
            calls.append('validate')
            if len(calls) == 2:
                state.release_lease(self.c.state_dir)
            return result

        with mock.patch.object(t.admission, '_validate_locked', side_effect=validated):
            with self.assertRaisesRegex(t.authority.AuthorityError, 'caller-held coordinator lease required'):
                self.c.submit()
        self.assertEqual(calls, ['validate', 'validate'])
        self.assertEqual(self.c.boundary.calls, [])
        self.assertEqual([row['state'] for row in self.c.rows()], ['not_submitted'])
        self.assertEqual(self.c.probe('drain'), 'available')

    def test_revoked_in_process_submit_keeps_R5_refusal(self):
        t.control.revoke(*self.c.args, 151, 'stop')
        self.assertTrue(state.holds_lease(self.c.state_dir))
        with self.assertRaisesRegex(t.authority.AuthorityError, '^D5 authority is invalidated$'):
            self.c.submit()
        self.assertEqual(self.c.boundary.calls, [])
        self.assertEqual(self.c.rows(), [])
        self.assertEqual(self.c.probe('drain'), 'available')

    def test_M6_inactive_submit_matches_recorded_base(self):
        source = subprocess.check_output(['git', 'show',
            '3f17265c35d5e44cd2c04fa366976968c0b00fb3:skills/hanig-orchestrate/scripts/d5_submission.py'],
            cwd=str(t.ROOT), text=True, timeout=30)
        base = ModuleType('d5_submission_recorded_base')
        with mock.patch.dict(sys.modules, {base.__name__: base}):
            exec(compile(source, '<recorded-base>', 'exec'), base.__dict__)
            self.c.active.return_value = False
            state.release_lease(self.c.state_dir)
            self.marker.unlink()
            results = []
            for module in (base, t.submission):
                with no_effects() as observed:
                    with self.assertRaises(t.authority.AuthorityError) as error:
                        module.submit(*self.c.args, **self.c.request,
                                      operation=None, boundary=None)
                    results.append((type(error.exception), str(error.exception), observed))
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[1][1:], ('D5 is inactive', ([], [])))
        self.assertEqual(self.c.rows(), [])
        self.assertEqual(self.c.boundary.calls, [])


class TestSuccessorGate(unittest.TestCase):
    def setUp(self):
        self.c = t.TestSubmission()
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()
        self.marker = self.c.state_dir / readiness.MARKER
        self.installation = self.c.directory / readiness.INSTALLATION

    def operations(self):
        new = dict(self.c.original, grant_id='b' * 32)
        return (('promote', lambda: install.promote(*self.c.args)),
                ('replace', lambda: install.replace_authority(self.c.directory, new)))

    def snapshot(self):
        return {str(path): path.read_bytes() for root in (self.c.state_dir, self.c.directory)
                for path in root.rglob('*') if path.is_file()}

    def effect(self):
        return t.ledger.prepare(*self.c.args, record_id=self.c.request['grant_id'],
            custody=dict(generation=1, session_id='native', anchor_pid=t.ANCHOR.pid,
                         anchor_start_us=t.ANCHOR.start_us), kind='fixture',
            scope=self.c.scope, correlation={'request_id': 'gate'})

    def intent(self, phase='merge_requested'):
        binding = dict(head='a' * 40, target='main', pr=1)
        operation = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        value = dict(schema_version=1, operation_id=operation, binding=binding,
                     root=str(self.c.repo), phase=phase)
        path = self.c.state_dir / ('merge-unit-' + operation + '.json')
        path.write_text(json.dumps(value))
        return path, value

    def refusal_preserves(self, message):
        before = self.snapshot()
        for name, operation in self.operations():
            with self.subTest(operation=name), self.assertRaisesRegex(
                    (t.authority.AuthorityError, t.ledger.EffectError), message):
                operation()
            self.assertEqual(self.snapshot(), before)

    def test_gate_before_mutation_for_all_unresolved_ledger_states(self):
        attempt = self.effect()
        for phase in ('attempting', 'unknown', 'submitted'):
            if phase != 'attempting':
                t.ledger._classify(attempt, phase)
            with self.subTest(phase=phase):
                self.refusal_preserves('unresolved effect ledger')

    def test_gate_before_mutation_for_unresolved_merge_intent(self):
        self.intent()
        self.refusal_preserves('unresolved merge intent')

    def test_gate_never_repairs_pending_intent(self):
        path, value = self.intent()
        pending = path.with_suffix('.cancellation-pending')
        pending.write_bytes(path.read_bytes())
        for absent in (False, True):
            if absent:
                path.unlink()
            with self.subTest(absent=absent):
                self.refusal_preserves('unresolved merge intent')

    def test_gate_follows_abandonment_read_only_and_checks_successor(self):
        path, value = self.intent()
        sidecar = path.with_name('merge-abandonment-' + value['operation_id'] + '.json')
        sidecar.write_text(json.dumps(dict(schema_version=1, operation_id=value['operation_id'],
            intent=value, approver='fixture', reason='closed', observed_at='2026-10-24T00:00:00',
            observed_pr=dict(headRefOid='a' * 40, baseRefName='main', number=1, state='OPEN'))))
        successor = hashlib.sha256((value['operation_id'] + ':after-abandonment').encode()).hexdigest()
        next_path = path.with_name('merge-unit-' + successor + '.json')
        next_path.write_text(json.dumps(dict(value, operation_id=successor)))
        self.refusal_preserves('unresolved merge intent')
        next_path.unlink()
        original = path.read_bytes()
        with mock.patch.object(merge_unit, 'durable_write', side_effect=AssertionError('intent repair')):
            install.promote(*self.c.args)
        self.assertEqual(path.read_bytes(), original)

    def test_gate_allows_terminal_sources_and_orders_publication(self):
        receipt = t.ledger._classify(self.effect(), 'submitted')
        t.ledger.complete(receipt, t.OK)
        self.intent('receipt_recorded')
        original = readiness.publish
        events = []
        replace = t.store.replace

        def replacing(*args):
            self.assertFalse(self.marker.exists())
            events.append('authority')
            return replace(*args)

        def publishing(path, value):
            self.assertTrue(state.holds_lease(self.c.state_dir))
            self.assertEqual([(rank, shared) for _, rank, shared, _ in t.protocol.locks._held()],
                             [(0, False), (1, False)])
            self.assertFalse(self.marker.exists())
            if path.name == readiness.MARKER:
                self.assertEqual(json.loads(self.installation.read_bytes()), value)
            events.append(path.name)
            return original(path, value)

        for name, operation in self.operations():
            before = readiness.require(self.c.state_dir)
            events.clear()
            with mock.patch.object(t.store, 'replace', side_effect=replacing), \
                    mock.patch.object(readiness, 'publish', side_effect=publishing):
                operation()
            expected = ['authority'] if name == 'replace' else []
            self.assertEqual(events, expected + [readiness.INSTALLATION, readiness.MARKER])
            self.assertNotEqual(readiness.require(self.c.state_dir)['generation'], before['generation'])

    def test_gate_refuses_malformed_sources_without_mutation(self):
        path, _ = self.intent()
        path.write_bytes(b'{}')
        self.refusal_preserves('malformed merge intent')
        path.unlink()
        attempt = self.effect()
        row = self.c.directory / t.ledger.DIRECTORY / (attempt.effect_id + '.json')
        row.write_bytes(b'{}')
        self.refusal_preserves('D5 effect')


if __name__ == '__main__':
    unittest.main()
