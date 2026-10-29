"""Measured cutover matrix through real routes and synthetic tracker transports."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import test_linear_audit as audit
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
            # Each route starts with separate synthetic state. No real transport
            # or subprocess is permitted; the receipt subprocess is re-entered
            # in process to preserve its production parser and recorder.
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
                        # Leave one incomplete write so replay is an actual
                        # mutating route, not an already-confirmed no-op.
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
                    self.assertEqual(rc, 1 if active and route == 'drain' else 0,
                                     case.stdout + case.stderr)
                    if route == 'audit':
                        self.assertEqual(mutations, 0)
                    else:
                        self.assertGreater(mutations, 0)
                    if route == 'drain':
                        self.assertEqual(receipts, 0 if active else 1)
                        if active:
                            self.assertIn('D5 active:', case.stdout + case.stderr)
        print('U8 tracker transport matrix: ' + json.dumps(rows, sort_keys=True))

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
        # Existing consumer tests exercise real parser/transport in inactive
        # mode and prove zero subprocesses/queries in active mode.
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
