"""U4.4 consumes provider results without granting historical closure authority."""
from tests.fixtures.d5.ready import ready_installation
from contextlib import nullcontext
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import test_d5_u4_3_merge_provider as provider
from tests.test_d5_u4_0b1_issuance import no_effects
from tests.test_d5_u4_1_active_surface import SurfaceFixture
from tests.test_d5_u4_2_request_authority import tree_bytes
import d5_activation as activation
import d5_merge_journal as J
import d5_submission as DS
import merge_unit as M
import swarm_evidence as SE

BASE = '687e68de5ddab6f85d9109e2cb42951fcf6f1f9e'
FIXTURE = Path(__file__).parent / 'fixtures/d5/u44-inactive-base.json'


def admission_capture(root):
    """A real legacy receipt consumer, with no host executables available."""
    root = Path(root)
    tools = root / 'tools'
    tools.mkdir()
    receipt = dict(unit='u', repo='https://github.com/example/project',
        pr='https://github.com/example/project/pull/7', target='main', head='a' * 40,
        merged_as='c' * 40, method='squash', merged=True, attested=True)
    (root / SE.MERGE_RECEIPTS).write_text(json.dumps(receipt) + '\n')
    before = tree_bytes(root)
    with mock.patch.dict(os.environ, {'PATH': str(tools)}), \
            nullcontext(), no_effects() as effects:
        result = SE.admit_merge(root, 'u', 'a' * 40)
    return dict(result=list(result), launches=effects[0], writes=effects[1],
                unchanged=tree_bytes(root) == before)


class OperatorCase(unittest.TestCase):
    """Real installed provider and subprocess forge; scope/verification inputs are local."""
    def setUp(self):
        self.fixture = provider.TestMergeProvider()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = f = self.fixture
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.plan = f.c.root / 'plan.json'
        self.plan.write_text('{"name":"p"}')
        self.argv = [str(self.plan), '--state-dir', str(f.c.state_dir), '--unit', 'u',
                     '--pr', '7', '--approver', 'Operator']
        self.stack.enter_context(mock.patch.object(M, 'authority', return_value=(
            f.c.state_dir, f.intent['root'], f.binding, 'github.com', 'example/project', {}, str(f.c.repo))))
        self.stack.enter_context(mock.patch.object(M, 'validate_scope_report'))
        self.stack.enter_context(mock.patch.object(M, 'integration_evidence', return_value=(
            f.intent['preconditions']['integration'], None)))
        read = M._MergeBoundary.read
        def observation(boundary, command, **kwargs):
            if command == boundary.cmd['scope']:
                return subprocess.CompletedProcess(command, 0, '{}', '')
            return read(boundary, command, **kwargs)
        self.stack.enter_context(mock.patch.object(M._MergeBoundary, 'read', observation))
        # A stale audit label would be corrected by the inactive path. Active
        # success must leave this state, its epoch, and all outbox files alone.
        self.state = f.c.state_dir / M.S.STATE_FILE
        self.state.write_text(json.dumps(dict(schema_version=1, units=dict(u=dict(state='READY_FOR_PR',
            merge_receipt=dict(merged=True, integration_status='integration-unverified'))))))

    def invoke(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = M.main(self.argv)
        return code, output.getvalue()

    def assert_refused(self):
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        self.assertIn('REFUSED:', output)

    def test_success_and_recovery_require_real_receipt_and_preserve_coordinator_state(self):
        before = self.state.read_bytes()
        self.assertEqual(self.invoke(), (0, ''))
        receipt = json.loads(self.f.receipts.read_text())
        self.assertEqual(receipt['d5']['operation_id'], self.f.effect)
        self.assertEqual(receipt['d5']['binding']['installation'],
                         provider.readiness.require(self.f.c.state_dir))
        after = tree_bytes(self.f.c.state_dir)
        self.assertEqual(self.invoke(), (0, ''))
        self.assertEqual(tree_bytes(self.f.c.state_dir), after)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(len(self.f.merges()), 1)
        self.assertFalse((self.f.c.state_dir / M.S.OUTBOX).exists())
        self.assertFalse((self.f.c.directory / provider.t.ledger.DIRECTORY).exists())

    def test_submit_noncompletion_and_wrong_operation_are_not_success(self):
        outcomes = (None, dict(merged=True, attested=True),
            DS.Result(self.f.effect, DS.Initiation.NOT_SUBMITTED, None),
            DS.Result('wrong', DS.Initiation.SUBMITTED, None),
            DS.Result(self.f.effect, DS.Initiation.SUBMITTED, dict(merged=True)))
        for result in outcomes:
            with self.subTest(result=result), mock.patch.object(DS, 'submit', return_value=result):
                before = tree_bytes(self.f.c.state_dir)
                self.assert_refused()
                self.assertEqual(tree_bytes(self.f.c.state_dir), before)
                self.assertEqual(self.f.merges(), [])

    def test_result_without_durable_readback_never_reports_success(self):
        # Inject loss/corruption AFTER the actual provider returns; its own
        # readback has succeeded. This specifically exercises M's consumer.
        submit = DS.submit
        def lost(*args, **kwargs):
            result = submit(*args, **kwargs)
            self.f.receipts.unlink()
            return result
        with mock.patch.object(DS, 'submit', lost):
            self.assert_refused()
        self.assertEqual(self.f.row()['phase'], 'receipt_recorded')
        self.assertEqual(len(self.f.merges()), 1)

    def test_recorded_intent_needs_matching_receipt_on_every_recovery(self):
        self.assertEqual(self.invoke(), (0, ''))
        receipt = json.loads(self.f.receipts.read_text())
        variants = [None, '{bad}\n', json.dumps(dict(merged=True, attested=True)) + '\n']
        legacy = copy.deepcopy(receipt)
        del legacy['d5']
        variants.append(json.dumps(legacy) + '\n')
        for field in ('operation_id', 'schema_version'):
            value = copy.deepcopy(receipt)
            value['d5'][field] = 'mismatch'
            variants.append(json.dumps(value) + '\n')
        for field in ('record_id', 'custody', 'installation', 'correlation'):
            value = copy.deepcopy(receipt)
            value['d5']['binding'][field] = 'mismatch'
            variants.append(json.dumps(value) + '\n')
        for field in ('unit', 'head', 'repo', 'pr', 'target', 'merged_as'):
            value = copy.deepcopy(receipt)
            value[field] = 'mismatch'
            variants.append(json.dumps(value) + '\n')
        for index, raw in enumerate(variants):
            with self.subTest(variant=index):
                if raw is None:
                    self.f.receipts.unlink()
                else:
                    self.f.receipts.write_text(raw)
                before = tree_bytes(self.f.c.state_dir)
                self.assert_refused()
                self.assertEqual(tree_bytes(self.f.c.state_dir), before)
                self.assertEqual(len(self.f.merges()), 1)

    def test_historical_intent_and_receipt_never_gain_active_authority(self):
        self.assertEqual(self.invoke(), (0, ''))
        row = self.f.row()
        del row['d5']
        row['merged_as'] = 'c' * 40
        receipt = json.loads(self.f.receipts.read_text())
        del receipt['d5']
        for phase in ('merge_requested', 'merged', 'receipt_recorded'):
            with self.subTest(phase=phase):
                self.f.path.write_text(json.dumps(dict(row, phase=phase)))
                self.f.receipts.write_text(json.dumps(receipt) + '\n')
                before = tree_bytes(self.f.c.state_dir)
                self.assert_refused()
                self.assertEqual(tree_bytes(self.f.c.state_dir), before)
                self.assertEqual(len(self.f.merges()), 1)

    def test_readback_rejects_boolean_integer_aliases(self):
        self.assertEqual(self.invoke(), (0, ''))
        receipt = json.loads(self.f.receipts.read_text())
        for path, value in ((('merged',), 1), (('attested',), 1),
                (('d5', 'schema_version'), True), (('d5', 'schema_version'), 1.0),
                (('d5', 'binding', 'custody', 'generation'), True)):
            with self.subTest(path=path, value=value):
                row = copy.deepcopy(receipt)
                target = row
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.f.receipts.write_text(json.dumps(row) + '\n')
                before = tree_bytes(self.f.c.state_dir)
                self.assert_refused()
                self.assertEqual(tree_bytes(self.f.c.state_dir), before)
                self.assertEqual(len(self.f.merges()), 1)

    def test_direct_consumption_refuses_even_ready_valid_receipt(self):
        self.assertEqual(self.invoke(), (0, ''))
        with no_effects() as effects:
            for consumer in (SE.admit_merge, M.S.admit_merge):
                with self.assertRaisesRegex(SE.OutboxError, 'D5 active:'):
                    consumer(self.f.c.state_dir, 'u', self.f.binding['head'])
            self.assertEqual(effects, ([], []))

    def test_matching_malformed_bindings_cannot_supply_their_own_expected_types(self):
        self.assertEqual(self.invoke(), (0, ''))
        original, receipt = self.f.row(), json.loads(self.f.receipts.read_text())
        cases = [(('custody', key), value) for key in ('generation', 'anchor_pid', 'anchor_start_us')
                 for value in (True, 1.0, 0, 2**63)]
        cases += [(('record_id',), 1), (('kind',), 'fixture'),
                  (('custody', 'session_id'), ''), (('scope', 'plans'), 'p'),
                  (('correlation', 'request_id'), 'wrong'),
                  (('installation', 'schema_version'), True)]
        request = json.loads(bytes.fromhex(original['d5']['binding']['correlation']['intent']))
        cases += [(('correlation', 'intent'), (b'{"schema_version":1,' + J.encoded(request)[1:]).hex())]
        cases += [(('correlation', 'intent'), J.encoded(r).hex()) for r in
                  [dict(schema_version=1, phase='merge_requested')] +
                  [{k: v for k, v in request.items() if k != omitted} for omitted in request]]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                row, rec = copy.deepcopy(original), copy.deepcopy(receipt)
                # Include the embedded copy: agreeing corrupted records do
                # not define the schema of the admitted request.
                for record in (row, row['d5']['receipt'], rec):
                    target = record['d5']['binding']
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                self.f.path.write_text(json.dumps(row))
                self.f.receipts.write_text(json.dumps(rec) + '\n')
                before = tree_bytes(self.f.c.state_dir)
                self.assert_refused()
                self.assertEqual(tree_bytes(self.f.c.state_dir), before)
                self.assertEqual(len(self.f.merges()), 1)

    def test_malformed_bound_request_refuses_before_recovery_writes(self):
        self.assertEqual(self.invoke(), (0, ''))
        row = self.f.row()
        row['d5']['state'], row['phase'] = 'Attempted', 'merge_requested'
        del row['d5']['receipt']
        row['d5']['binding']['custody']['generation'] = True
        self.f.path.write_text(json.dumps(row))
        self.f.receipts.unlink()
        before = tree_bytes(self.f.c.state_dir)
        self.assert_refused()
        self.assertEqual(tree_bytes(self.f.c.state_dir), before)
        self.assertEqual(len(self.f.merges()), 1)


class TestInactiveAndPremature(unittest.TestCase):
    def test_inactive_capture_on_recorded_base_with_inherited_sinfo(self):
        case = SurfaceFixture.case
        def isolated(fixture, *args):
            call = case(fixture, *args)
            def checked():
                self.assertIsNone(shutil.which('sinfo'))
                return call()
            return checked
        data = json.loads(FIXTURE.read_text())
        self.assertEqual(data['base'], BASE)
        self.assertEqual(set(data['captures']), {'main', 'reconcile', 'admit_merge'})
        with tempfile.TemporaryDirectory() as inherited:
            fake = Path(inherited) / 'sinfo'
            fake.write_text('#!/bin/sh\nexit 97\n')
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': inherited}), \
                    mock.patch.object(SurfaceFixture, 'case', isolated):
                self.assertEqual(shutil.which('sinfo'), str(fake))
                for name, expected in data['captures'].items():
                    with self.subTest(entry=name), tempfile.TemporaryDirectory() as root:
                        actual = (admission_capture(root) if name == 'admit_merge' else
                                  SurfaceFixture(root).observe('operator', name, False))
                        self.assertEqual(actual, expected)

    def test_premature_activation_zero_effects(self):
        for name in ('main', 'reconcile'):
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as root:
                observed = SurfaceFixture(root).observe('operator', name, True)
                self.assertEqual(observed['launches'], [])
                self.assertEqual(observed['writes'], [])
                self.assertEqual(observed['files_changed'], [])
                self.assertIn('D5 active:', str(observed['outcome']) + str(observed['output']))
        with tempfile.TemporaryDirectory() as root:
            ready_installation(root, state_dir=root)
            with no_effects() as effects:
                with self.assertRaisesRegex(SE.OutboxError, 'D5 active:'):
                    SE.admit_merge(root, 'u', 'a' * 40)
                with self.assertRaisesRegex(ValueError, 'coordinator lease required'):
                    M._d5_result_receipt(root, {}, 'runs', 'op', None, 'repo')
                self.assertEqual(effects, ([], []))


if __name__ == '__main__':
    unittest.main()
