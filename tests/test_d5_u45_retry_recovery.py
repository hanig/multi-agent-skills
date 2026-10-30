"""U4.5 fresh-process crash recovery; the actual CLI never replays a merge."""
from contextlib import nullcontext
from contextlib import ExitStack
from datetime import datetime
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import test_merge_unit as legacy
from tests import test_d5_submission as t
from tests.test_d5_u4_0b1_issuance import no_effects
from tests.test_d5_u4_1_active_surface import SurfaceFixture
from tests.test_d5_u4_2_request_authority import tree_bytes
from tests.fixtures.d5.u45_operator import probe
import d5_activation as activation
import d5_merge_journal as J
import d5_readiness as readiness
import merge_unit as M
import swarm_state as state

BASE = 'eb90c2c50081291c43e7177bcd3edfc2197fe61e'
FIXTURES = Path(__file__).parent / 'fixtures/d5'


def inactive_script_capture(script=None):
    """Actual inactive script, real local Git/state and only fixture tools."""
    c = legacy.TestMergeUnit()
    c.setUp()
    try:
        if script is not None:
            c.operator = script
        c.env['HANIG_ORCHESTRATE_DIR'] = str(legacy.OPERATOR.parent.parent)
        c.env['PYTHONPATH'] = str(legacy.OPERATOR.parent)
        # Python may report a resolved launcher path in its child process.
        child_python = subprocess.check_output(
            [sys.executable, '-c', 'import sys; print(sys.executable)'],
            env=environment, text=True).strip()
        before = tree_bytes(c.state_dir)
        result = c.invoke()
        c.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = tree_bytes(c.state_dir)
        captured = dict(code=result.returncode, output=result.stdout, error=result.stderr,
                        calls=c.calls(), receipts=c.receipts(),
                        changed=sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)))
        for receipt in captured['receipts']:
            datetime.strptime(receipt['at'], '%Y-%m-%dT%H:%M:%S%z')
            c.assertIsInstance(receipt['by'], str)
            c.assertTrue(receipt['by'])
            receipt.update(at='<time>', by='<operator>')
        encoded = json.dumps(captured, sort_keys=True)
        for old, new in ((child_python, '<python>'), (sys.executable, '<python>'),
                         (str(c.directory), '<fixture>'), (c.head, '<head>'),
                         (c.merged, '<merge>'), (c.base, '<base>'),
                         (c.merged[:12], '<merge-short>'),
                         (c.intent()['operation_id'], '<operation>'),
                         (str(legacy.SCRIPTS), '<swarm-scripts>'),
                         (str(legacy.SCRIPTS.parents[1]), '<skills>')):
            encoded = encoded.replace(old, new)
        for line in (c.state_dir / legacy.S.OUTBOX).read_text().splitlines():
            encoded = encoded.replace(json.loads(line)['key'], '<outbox-key>')
        return json.loads(encoded)
    finally:
        c.doCleanups()


class TestRecovery(unittest.TestCase):
    def setUp(self):
        self.c = c = legacy.TestMergeUnit()
        c.prepare_active_fixture()
        self.addCleanup(c.doCleanups)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, c.env))
        self.stack.enter_context(nullcontext())
        self.stack.enter_context(mock.patch.object(M, 'merge_launch_admitted', return_value=True))
        self.stack.enter_context(nullcontext())
        self.stack.enter_context(mock.patch.object(t.registry.CP, '_external_base', return_value=c.directory / 'external'))
        self.stack.enter_context(mock.patch.object(t.host, 'session_anchor', return_value=('native', t.ANCHOR)))
        self.stack.enter_context(mock.patch.object(t.admission.time, 'time', return_value=150))
        mandate = c.repo / 'docs/orchestrator-mandate.md'
        mandate.parent.mkdir(exist_ok=True)
        mandate.write_bytes(t.MANDATE)
        self.entry = t.ready(self, c.repo, c.plan['name'], c.state_dir)
        self.record = Path(self.entry['record_dir'])
        scope = dict(plans=[c.plan['name']], repositories=[c.remote], actions=['merge'], trackers=[])
        t.lease.grant(c.repo, c.plan['name'], c.state_dir, issued_at=100, confirmed_at=100,
            expires_at=200, mandate_bytes=t.MANDATE, scope=scope,
            owner_response='fixture', displayed_bounds='fixture')
        t.store.claim(self.record, 101, t.MANDATE, 'native', t.ANCHOR, lambda _: 'alive')
        self.installed = readiness.require(c.state_dir)
        self.argv = [str(c.plan_path), '--state-dir', str(c.state_dir), '--unit', 'u',
                     '--pr', '7', '--approver', 'Operator']
        self.data = c.directory / 'u45.json'
        self.data.write_text(json.dumps(dict(argv=self.argv, external=str(c.directory / 'external'),
                                             record=str(self.record))))
        # No submit is in flight: only fixture construction held this lease.
        state.release_lease(c.state_dir)

    def invoke(self, mode='recover'):
        result = subprocess.run([sys.executable, str(FIXTURES / 'u45_operator.py'),
            str(self.data), mode], cwd=self.c.repo, env=self.c.env,
            capture_output=True, text=True, timeout=45)
        self.assertNotIn('Traceback', result.stderr, result.stderr)
        return result

    def fence(self, unresolved):
        before = tree_bytes(self.c.state_dir)
        with readiness.installer_lease(self.c.state_dir):
            self.assertEqual(readiness.require(self.c.state_dir), self.installed)
            with t.protocol.pin(self.record) as pinned, pinned.drain(shared=False), pinned.start():
                if unresolved:
                    with self.assertRaisesRegex(ValueError, 'unresolved merge intent'):
                        readiness.successor_gate(self.entry)
                else:
                    readiness.successor_gate(self.entry)
        self.assertEqual(tree_bytes(self.c.state_dir), before)

    def crash(self, mode, expected):
        result = self.invoke(mode)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(probe(self.record), 'available', 'dead owner must release its holder')
        row = self.c.intent()
        self.assertEqual(row['phase'], 'merge_requested')
        self.fence(True)
        return result

    def matrix(self, mode, expected):
        result = self.crash(mode, expected)
        if mode == 'receipt':
            self.assertIn('injected durable receipt before intent update', result.stderr)
            self.assertIn('before-process-exit DRAIN=blocked', result.stdout)
        merges = len(self.c.calls(['pr', 'merge']))
        self.assertEqual(merges, 0 if mode == 'prepare' else 1)
        before = tree_bytes(self.c.state_dir)
        old_receipts = self.c.receipts()
        # An OPEN observation can never turn retained crash state into a
        # fresh request. Exercise that no-replay fence for all four cases.
        forge_path = Path(self.c.env['FORGE_STATE'])
        original_forge = forge_path.read_bytes()
        pending = json.loads(original_forge)
        pending['pr'].update(state='OPEN', mergeCommit=None)
        forge_path.write_text(json.dumps(pending))
        refused = self.invoke()
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn('unresolved outcome', refused.stderr)
        self.assertEqual(tree_bytes(self.c.state_dir), before)
        self.assertEqual(len(self.c.calls(['pr', 'merge'])), merges)
        forge_path.write_bytes(original_forge)
        recovered = self.invoke()
        if mode == 'prepare':
            self.assertEqual(recovered.returncode, 1, recovered.stderr)
            self.assertIn('unresolved outcome', recovered.stderr)
            self.assertEqual(tree_bytes(self.c.state_dir), before)
            self.fence(True)
        else:
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(self.c.intent()['phase'], 'receipt_recorded')
            receipt, = self.c.receipts()
            self.assertEqual(receipt['d5']['operation_id'], self.c.intent()['operation_id'])
            self.assertEqual(receipt['d5']['binding']['installation'], self.installed)
            if mode == 'receipt':
                self.assertEqual([receipt], old_receipts)
            self.fence(False)
            after = tree_bytes(self.c.state_dir)
            self.assertEqual(self.invoke().returncode, 0)
            self.assertEqual(tree_bytes(self.c.state_dir), after)
        self.assertEqual(len(self.c.calls(['pr', 'merge'])), merges)
        self.assertEqual(probe(self.record), 'available')
        # Recovery never publishes a successor installation, even on completion.
        self.assertEqual(json.loads((self.c.state_dir / readiness.MARKER).read_text()), self.installed)

    def test_preparation_before_dispatch(self):
        self.matrix('prepare', 71)

    def test_dispatch_before_observed_outcome(self):
        self.matrix('dispatch', 72)

    def test_observed_merge_before_receipt_append(self):
        self.matrix('observed', 73)

    def test_durable_receipt_before_intent_update(self):
        self.matrix('receipt', 1)

    def test_live_holder_refused_without_writes_then_exit_allows_recovery(self):
        self.crash('receipt', 1)
        before = tree_bytes(self.c.state_dir)
        holder = subprocess.Popen([sys.executable, str(FIXTURES / 'u45_operator.py'),
            str(self.data), 'hold'], env=self.c.env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([holder.stdout], [], [], 15)[0], 'holder watchdog')
            self.assertEqual(holder.stdout.readline(), 'READY\n')
            self.assertEqual(probe(self.record), 'blocked')
            refused = self.invoke()
            self.assertEqual(refused.returncode, 1, refused.stderr)
            self.assertIn('DRAIN is held', refused.stderr)
            self.assertEqual(tree_bytes(self.c.state_dir), before)
            self.assertEqual(len(self.c.calls(['pr', 'merge'])), 1)
            self.assertIsNone(holder.poll())
            self.assertEqual(probe(self.record), 'blocked', 'refusal must not unlock the original holder')
        finally:
            out, err = holder.communicate('\n', timeout=15)
        self.assertEqual(holder.returncode, 0, out + err)
        self.assertEqual(probe(self.record), 'available')
        recovered = self.invoke()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(self.c.intent()['phase'], 'receipt_recorded')
        self.assertEqual(len(self.c.calls(['pr', 'merge'])), 1)

    def test_pending_dispatch_is_unresolved_then_observed_completion(self):
        self.c.forge['queued'] = True
        self.c.save()
        self.crash('dispatch', 72)
        before = tree_bytes(self.c.state_dir)
        refused = self.invoke()
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn('unresolved outcome', refused.stderr)
        self.assertEqual(tree_bytes(self.c.state_dir), before)
        self.fence(True)
        forge = json.loads(Path(self.c.env['FORGE_STATE']).read_text())
        forge['pr'].update(state='MERGED', mergeCommit=dict(oid=self.c.merged))
        Path(self.c.env['FORGE_STATE']).write_text(json.dumps(forge))
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(len(self.c.calls(['pr', 'merge'])), 1)
        self.fence(False)

    def test_recovery_is_bound_to_installed_generation_and_exact_outcome(self):
        self.crash('observed', 73)
        path, = self.c.state_dir.glob('merge-unit-*.json')
        original = path.read_bytes()
        row = json.loads(original)
        row['d5']['binding']['installation']['generation'] = 'f' * 32
        path.write_text(json.dumps(row))
        before = tree_bytes(self.c.state_dir)
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(tree_bytes(self.c.state_dir), before)
        path.write_bytes(original)
        forge = json.loads(Path(self.c.env['FORGE_STATE']).read_text())
        forge['commit']['parents'] = [dict(sha='d' * 40)]
        Path(self.c.env['FORGE_STATE']).write_text(json.dumps(forge))
        before = tree_bytes(self.c.state_dir)
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(tree_bytes(self.c.state_dir), before)
        self.assertEqual(self.c.receipts(), [])
        self.assertEqual(len(self.c.calls(['pr', 'merge'])), 1)

    def test_recovery_requires_live_lease_and_does_not_borrow_or_reorder(self):
        self.crash('observed', 73)
        before = tree_bytes(self.c.state_dir)
        with self.assertRaisesRegex(ValueError, 'coordinator lease required'):
            with J.recovery_drain(self.c.state_dir):
                self.fail('unleased recovery')
        with readiness.installer_lease(self.c.state_dir):
            marker = self.c.state_dir / readiness.MARKER
            saved = marker.read_bytes()
            marker.unlink()
            try:
                with no_effects() as effects:
                    with self.assertRaisesRegex(ValueError, 'missing or malformed installation'):
                        with J.recovery_drain(self.c.state_dir):
                            self.fail('recovery bypassed readiness')
                    self.assertEqual(effects, ([], []))
            finally:
                marker.write_bytes(saved)
            for guard in ('drain', 'start'):
                with t.protocol.pin(self.record) as pinned, getattr(pinned, guard)():
                    with self.assertRaisesRegex(ValueError, 'independent DRAIN before START'):
                        with J.recovery_drain(self.c.state_dir):
                            self.fail('borrowed or reordered recovery')
            with J.recovery_drain(self.c.state_dir):
                self.assertEqual(probe(self.record), 'blocked')
                # Prior lease loss is detected before recovery writes. No
                # callback, signal or concurrent lease release during a call.
                state.release_lease(self.c.state_dir)
                with self.assertRaisesRegex(ValueError, 'coordinator lease required'):
                    J.recover(self.c.state_dir, self.c.intent(), None)
        self.assertEqual(tree_bytes(self.c.state_dir), before)
        self.assertEqual(probe(self.record), 'available')


class TestInactiveAndPremature(unittest.TestCase):
    def test_inactive_actual_script_matches_recorded_base(self):
        data = json.loads((FIXTURES / 'u45-inactive-base.json').read_text())
        self.assertEqual(data['base'], BASE)
        self.assertEqual(inactive_script_capture(), data['script'])

    def test_inactive_capture_and_inherited_sinfo(self):
        data = json.loads((FIXTURES / 'u45-inactive-base.json').read_text())
        self.assertEqual(data['base'], BASE)
        self.assertEqual(set(data['captures']), {'main', 'reconcile'})
        case = SurfaceFixture.case
        def isolated(fixture, *args):
            call = case(fixture, *args)
            def checked():
                self.assertIsNone(shutil.which('sinfo'))
                return call()
            return checked
        with tempfile.TemporaryDirectory() as inherited:
            fake = Path(inherited) / 'sinfo'
            fake.write_text('#!/bin/sh\nexit 97\n')
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': inherited}), \
                    mock.patch.object(SurfaceFixture, 'case', isolated):
                self.assertEqual(shutil.which('sinfo'), str(fake))
                for name, expected in data['captures'].items():
                    with self.subTest(entry=name), tempfile.TemporaryDirectory() as root:
                        self.assertEqual(SurfaceFixture(root).observe('operator', name, False), expected)

    def test_premature_activation_zero_effects(self):
        for name in ('main', 'reconcile'):
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as root:
                observed = SurfaceFixture(root).observe('operator', name, True)
                for key in ('launches', 'writes', 'files_changed'):
                    self.assertEqual(observed[key], [])
                self.assertIn('D5 active:', str(observed['outcome']) + str(observed['output']))
        with tempfile.TemporaryDirectory() as root, \
                nullcontext(), no_effects() as effects:
            with self.assertRaisesRegex(ValueError, 'coordinator lease required'):
                with J.recovery_drain(root):
                    self.fail('entered recovery without readiness')
            self.assertEqual(effects, ([], []))


if __name__ == '__main__':
    unittest.main()
