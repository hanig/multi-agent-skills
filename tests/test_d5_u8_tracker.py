"""Measured cutover matrix through real routes and synthetic tracker transports."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import test_linear_issue as issue
from tests import test_linear_file as filing
from tests import test_linear_drain as drain
from tests import test_d5_u6_tracker_reject as outbox
import d5_activation as activation


def fixture(case, cls):
    value = cls()
    case.addCleanup(value.doCleanups)
    value.setUp()
    return value


def receipt_child(command, **kwargs):
    """Run the real offline child CLI under the same explicit activation fixture."""
    assert Path(command[1]).name == 'drain_contract.py', command
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = drain.DC.main(command[2:])
    return subprocess.CompletedProcess(command, rc, out.getvalue(), err.getvalue())


class TestTrackerMatrix(unittest.TestCase):
    def test_linear_routes_in_both_activation_states(self):
        rows = []
        for active in (False, True):
            # Separate stub state per route; real receipt parser/recorder, no child.
            for route in ('new', 'edit', 'replay', 'file', 'audit', 'drain'):
                with self.subTest(active=active, route=route), ExitStack() as stack:
                    cls = drain.DrainCase if route == 'drain' else (
                        filing.FileCase if route == 'file' else issue.IssueCase)
                    case = cls()
                    stack.callback(case.doCleanups)
                    case.setUp()
                    stack.enter_context(mock.patch.object(activation, 'd5_active', return_value=active))
                    stack.enter_context(mock.patch.object(subprocess, 'run', side_effect=receipt_child))
                    if route == 'new':
                        rc = case.issue('new', '--independent', 'fixture')
                    elif route == 'edit':
                        rc = case.issue('edit', 'ARC-1', '--title', 'changed')
                    elif route == 'replay':
                        # Replay must perform an incomplete write.
                        case.fake.ignore_update = True
                        self.assertNotEqual(case.issue('edit', 'ARC-1', '--title', 'changed'), 0)
                        case.fake.ignore_update = False
                        case.fake.mutations.clear()
                        rc = case.replay()
                    elif route == 'file':
                        rc = case.file()
                    elif route == 'audit':
                        rc = case.audit()
                    else:
                        case.intent()
                        rc = case.drain()
                    mutations = len(case.fake.mutations)
                    receipts = len(case.receipts()) if route == 'drain' else None
                    rows.append(dict(active=active, route=route, exit=rc,
                                     mutations=mutations, receipts=receipts))
                    self.assertEqual(rc, 3 if active and route == 'drain' else 0,
                                     case.stdout + case.stderr)
                    if route == 'audit':
                        self.assertEqual(mutations, 0)
                    else:
                        self.assertGreater(mutations, 0)
                    if route == 'drain':
                        self.assertEqual(receipts, 0 if active else 1)
                        if active:
                            self.assertIn('D5 active:', case.stdout + case.stderr)
                            case.fake.mutations.clear()
                            repeated = case.drain()
                            rows.append(dict(active=active, route='drain-repeat',
                                             exit=repeated, mutations=len(case.fake.mutations),
                                             receipts=len(case.receipts())))
                            self.assertEqual(repeated, 3)
                            self.assertEqual(len(case.fake.mutations), 0)
                            self.assertEqual(case.receipts(), [])
                            self.assertIn('D5 active:', case.stdout + case.stderr)
        print('U8 tracker transport matrix: ' + json.dumps(rows, sort_keys=True))

    def test_swarm_cli_tracker_routes(self):
        rows = []
        S, SO = outbox.S, outbox.SO
        for active in (False, True):
            for route in ('outbox', 'seq-floor', 'receipt', 'backfill'):
                with self.subTest(active=active, route=route), tempfile.TemporaryDirectory() as tmp:
                    case = outbox.OutboxFixture(tmp)
                    plan = Path(tmp) / 'plan.json'
                    plan.write_text(json.dumps({'name': 'fixture', 'units': [dict(
                        id='u', kind='slurm', command='true', outputs=['out'], runtime='none')]}))
                    common = ['--state-dir', str(case.state_dir)]
                    argv = {
                        'outbox': ['outbox', '--json'],
                        'seq-floor': ['seq-floor', '--floor', '3', '--approver',
                                      'Operator', '--reason', 'fixture recovery'],
                        'receipt': ['outbox', '--record-receipt', case.key, '--ref', 'fixture'],
                        'backfill': ['advance', str(plan), '--max-new-dispatches', '0'],
                    }[route] + common
                    before, output = case.tree(), io.StringIO()
                    with ExitStack() as stack:
                        stack.enter_context(mock.patch.object(activation, 'd5_active', return_value=active))
                        stack.enter_context(mock.patch.object(sys, 'argv', ['swarm.py'] + argv))
                        stack.enter_context(mock.patch.dict(os.environ, {'PATH': str(case.tools)}))
                        stack.enter_context(mock.patch.object(S.CP, 'resolve_paths', return_value=(
                            case.state_dir, Path(tmp) / 'runs', [])))
                        stack.enter_context(mock.patch.object(S.SR, 'apply_agent_resolution'))
                        stack.enter_context(mock.patch.object(subprocess, 'Popen',
                            side_effect=AssertionError('no external process in tracker probe')))
                        stack.enter_context(redirect_stdout(output))
                        stack.enter_context(redirect_stderr(output))
                        backfill = stack.enter_context(mock.patch.object(SO,
                            'backfill_tracker_intents', wraps=SO.backfill_tracker_intents))
                        try:
                            S.main()
                        except SystemExit as exc:
                            result = exc.code
                        except S.PlanError as exc:
                            result = str(exc)
                    if active:
                        self.assertIn('D5 active:', str(result))
                        self.assertEqual(case.tree(), before)
                        backfill.assert_not_called()
                    else:
                        self.assertEqual(result, 0, output.getvalue())
                        if route == 'backfill':
                            backfill.assert_called_once()
                    rows.append(dict(active=active, route=route, outcome=result))
        print('U8 swarm CLI tracker matrix: ' + json.dumps(rows, sort_keys=True))

    def test_swarm_outbox_sequence_backfill_and_receipt_routes(self):
        rows = []
        for active in (False, True):
            for route in outbox.ENTRIES:
                with self.subTest(active=active, route=route), tempfile.TemporaryDirectory() as tmp:
                    case = outbox.OutboxFixture(tmp)
                    before = case.tree()
                    with mock.patch.object(activation, 'd5_active', return_value=active):
                        if active:
                            with self.assertRaisesRegex(outbox.SO.OutboxError, 'D5 active:'):
                                case.call(route)
                            self.assertEqual(case.tree(), before)
                        else:
                            case.call(route)
                    rows.append(dict(active=active, route=route, refused=active))
        print('U8 tracker helper matrix: ' + json.dumps(rows, sort_keys=True))

    def test_merge_drain_and_audit_routes(self):
        # Real inactive audit consumer; active mode must make no child/query call.
        audit_case = fixture(self, outbox.TestAuditObservation)
        audit_case.test_active_audit_invokes_only_observations_at_real_transport()
        rows = []
        for active in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                observation = outbox.SurfaceFixture(tmp).observe('operator', 'print_tracker_drain', active)
                rows.append(dict(active=active, route='print_tracker_drain', observation=observation))
                if active:
                    self.assertIn('D5 active:', str(observation['outcome']))
                    self.assertEqual(observation['launches'], [])
                else:
                    self.assertTrue(observation['launches'])
        print('U8 merge tracker matrix: ' + json.dumps(rows, sort_keys=True))


if __name__ == '__main__':
    unittest.main()
