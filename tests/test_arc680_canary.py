"""ARC-680: one root closes before the rest of the plan is admitted."""
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills' / 'hanig-swarm' / 'scripts'))
import swarm as S


def plan():
    return {'name': 'canary-test', 'canary': 'a', 'units': [
        {'id': uid, 'kind': 'slurm', 'runtime': 'none', 'command': 'true',
         'outputs': [uid + '.txt']} for uid in 'abcd']}


class CanaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_dir = str(self.tmp / 'state')
        self.root = str(self.tmp / 'runs')
        self.plan = plan()
        self.submitted = []
        self.verdicts = {}
        self.checked = []
        for name, fake in (('_submit', self.submit), ('_check', self.check)):
            patcher = mock.patch.object(S, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def submit(self, unit, directory, dry_run, state, state_dir, **kwargs):
        self.submitted.append(copy.deepcopy(unit))
        return str(100 + len(self.submitted)), None

    def check(self, directory, *args):
        uid = json.loads((Path(directory) / S.U.UNIT).read_text())['task_id']
        self.checked.append(uid)
        rc = self.verdicts.get(uid, S.RUNNING)
        receipt = json.dumps({'state': S.NAME[rc], 'task_id': uid,
                              'attempt_id': Path(directory).name}).encode()
        (Path(directory) / S.U.RECEIPT).write_bytes(receipt)
        channel = S.CHECK_RESULT_PREFIX + ' ' + json.dumps({
            'produced_head': None,
            'receipt_sha256': hashlib.sha256(receipt).hexdigest(),
        }, sort_keys=True, separators=(',', ':'))
        return rc, '', '', channel

    def advance(self, **kwargs):
        ok, why = S.acquire_lease(self.state_dir)
        self.assertTrue(ok, why)
        try:
            state = S.load_state(self.state_dir)
            result = S.advance(self.plan, state, self.state_dir, self.root,
                               False, **kwargs)
            return result, S.load_state(self.state_dir)
        finally:
            S.release_lease(self.state_dir)

    def test_000_canary_then_fanout_through_real_advance(self):
        original = copy.deepcopy(self.plan)
        result, state = self.advance()
        self.assertEqual([u['id'] for u in self.submitted], ['a'])
        self.assertEqual(result[1], 1)
        for uid in 'bcd':
            self.assertIn(uid + ': waiting on canary a', result[0])
            self.assertIsNone(state['units'][uid]['attempt_dir'])
        self.verdicts['a'] = S.DONE
        result, state = self.advance()
        self.assertEqual(result[1], 3, result)
        self.assertEqual([u['id'] for u in self.submitted], list('abcd'))
        self.assertEqual(state['units']['a']['state'], 'DONE')
        for unit in self.submitted[1:]:
            self.assertEqual(dict(S._dep_env(unit, state))['SWARM_DEP_A'],
                             state['units']['a']['attempt_dir'])
        self.assertEqual(self.plan, original)

    def test_failure_holds_and_persists_every_other_unit(self):
        self.advance()
        self.verdicts['a'] = S.FAILED
        result, state = self.advance()
        self.assertEqual(result[1], 0)
        for uid in 'bcd':
            self.assertEqual(state['units'][uid]['state'], 'HELD')
            self.assertIn(uid + ': held, upstream a will not complete', result[0])
        rows = S.status_report(self.plan, state, self.state_dir)['units']
        self.assertEqual([r['held_by'] for r in rows[1:]], [['a']] * 3)
        blocks = [r['unit'] for r in S.read_outbox(self.state_dir)
                  if r['unit_state'] == 'HELD' and r['verb'] == 'block']
        self.assertEqual(blocks, list('bcd'))

    def test_persisted_failure_states_use_existing_held_path(self):
        for terminal in ('FAILED', 'FAILED_EVIDENCE', 'HELD'):
            with self.subTest(terminal=terminal):
                state = {'units': {}}
                S._unit_state(state, 'a')['state'] = terminal
                S.save_state(self.state_dir, state)
                result, state = self.advance()
                self.assertEqual(result[1], 0)
                for uid in 'bcd':
                    self.assertEqual(state['units'][uid]['state'], 'HELD')
                    self.assertIn(uid + ': held, upstream a will not complete',
                                  result[0])

    def test_omitting_canary_fans_out_as_before(self):
        self.plan.pop('canary')
        result, _ = self.advance()
        self.assertEqual(result[1], 4)
        self.assertEqual([u['id'] for u in self.submitted], list('abcd'))

    def test_canary_need_not_sort_first(self):
        self.plan['canary'] = 'd'
        self.advance()
        self.assertEqual([u['id'] for u in self.submitted], ['d'])

    def test_explicit_edges_and_per_admission_limits_still_apply(self):
        self.plan['units'][3]['needs'] = ['b']
        self.plan['limits'] = {'max_running': 1}
        self.advance()
        self.verdicts['a'] = S.DONE
        result, _ = self.advance()
        self.assertEqual(result[1], 1, result)
        self.assertEqual([u['id'] for u in self.submitted], ['a', 'b'])
        self.verdicts['b'] = S.DONE
        result, _ = self.advance(max_new=1)
        self.assertEqual(result[1], 1, result)
        self.assertEqual([u['id'] for u in self.submitted], ['a', 'b', 'c'])

    def test_dry_advance_preserves_live_real_state_bytes(self):
        self.advance()
        before = {p.relative_to(self.tmp): p.read_bytes()
                  for p in self.tmp.rglob('*') if p.is_file()}
        result = S.advance(self.plan, S.load_state(self.state_dir),
                           self.state_dir, self.root, True)
        self.assertEqual(result[1], 0)
        self.assertIn('REFUSING to dry-run', result[0][0])
        self.assertEqual(before, {p.relative_to(self.tmp): p.read_bytes()
                                 for p in self.tmp.rglob('*') if p.is_file()})

    def test_existing_attempt_still_checked_and_intent_unchanged(self):
        self.plan.pop('canary')
        self.advance()
        state = S.load_state(self.state_dir)
        legacy = {'schema_version': 1, 'legacy': 'retained bytes'}
        state['units']['b']['attempt_launch_intents'] = {'old': legacy}
        S.save_state(self.state_dir, state)
        self.plan['canary'] = 'a'
        _, state = self.advance(accept_plan_change=True)
        self.assertEqual(self.checked, list('abcd'))
        self.assertEqual(state['units']['b']['attempt_launch_intents'],
                         {'old': legacy})

    def test_status_names_waiting_canary_in_text_and_json(self):
        self.advance()
        path = self.tmp / 'plan.json'
        path.write_text(json.dumps(self.plan))
        for as_json in (False, True):
            with self.subTest(json=as_json), redirect_stdout(io.StringIO()) as out:
                rc = S.cmd_status(SimpleNamespace(
                    plan=str(path), state_dir=self.state_dir, json=as_json))
                self.assertEqual(rc, 0)
                self.assertIn('waiting on canary a', out.getvalue())


class CanaryPlanTests(unittest.TestCase):
    def test_invalid_canaries_are_refused(self):
        for value in (None, True, 1, 1.5, [], {}, ['a'], '', 'missing', ' a '):
            with self.subTest(value=value):
                p = plan()
                p['canary'] = value
                with self.assertRaisesRegex(S.PlanError, 'canary'):
                    S.validate_plan(p)
        p = plan()
        p['units'][0]['needs'] = ['b']
        with self.assertRaisesRegex(S.PlanError, 'canary.*root'):
            S.validate_plan(p)

    def test_implicit_edge_orders_inputs_and_scopes_without_mutation(self):
        p = plan()
        p['units'][1]['inputs'] = ['a.txt']
        p['units'][0]['write_scopes'] = ['shared/']
        p['units'][1]['write_scopes'] = ['shared/']
        p['units'][2]['needs'] = ['a']
        original = copy.deepcopy(p)
        self.assertEqual(S.validate_plan(p)['with_deps'], 3)
        self.assertEqual(p, original)
        p.pop('canary')
        with self.assertRaises(S.PlanError):
            S.validate_plan(p)

    def test_canary_does_not_hide_malformed_needs(self):
        p = plan()
        p['units'][1]['needs'] = 'a'
        with self.assertRaisesRegex(S.PlanError, 'needs.*must be a list'):
            S.validate_plan(p)

    def test_digest_compatibility_and_canary_binding(self):
        p = plan()
        p.pop('canary')
        # Fixed value captured by running the recorded base's plan_digest.
        self.assertEqual(S.plan_digest(p), '979aa7b8e88a1e3e3b6950098e493c6a00bad2694e31da09299f02f55e8a8d46')
        without = S.plan_digest(p)
        p['canary'] = 'a'
        first = S.plan_digest(p)
        p['canary'] = 'b'
        self.assertEqual(len({without, first, S.plan_digest(p)}), 3)

    def test_schema_distinguishes_plan_canary_from_runtime_probe(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(S.cmd_schema(SimpleNamespace(json=True)), 0)
        fields = json.loads(out.getvalue())['plan_fields']
        field = next(f for f in fields if f['field'] == 'canary')
        self.assertEqual(field['requirement'], 'optional')
        self.assertIn('root', field['notes'])
        self.assertIn('DONE', field['notes'])


if __name__ == '__main__':
    unittest.main()
