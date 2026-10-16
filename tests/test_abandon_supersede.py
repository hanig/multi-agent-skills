"""ARC-1786: approver dispositions through CLI, outbox, fake drain and audit."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.test_linear_drain import DrainCase, DC, LS
from tests.test_dispatch_claims import fake_bin, SBATCH, SQUEUE_LISTS, SQUEUE_SILENT, SQUEUE_BROKEN
from tests.scheduler_fixture import isolated_module_path, cleanup_module_path as tearDownModule


def setUpModule():
    isolated_module_path()

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / 'skills/hanig-swarm/scripts'
PROJECT = ROOT / 'skills/hanig-project/scripts'
sys.path.insert(0, str(SWARM))
sys.path.insert(0, str(PROJECT))
import swarm as S
import swarm_state as SS
import swarm_outbox as SO
import report as R


class StopFixture:
    def setup_stop(self):
        self.plan = self.root / 'plan.json'
        self.state = self.root / 'state'
        self.state.mkdir(exist_ok=True)
        self.runs = self.root / 'runs'
        self.attempt = self.runs / 'old' / 'attempt1'
        self.attempt.mkdir(parents=True, exist_ok=True)
        for path in (self.state / SO.OUTBOX, self.state / SS.STATE_EPOCH_FILE,
                     self.attempt / "receipt.json"):
            path.unlink(missing_ok=True)
        (self.attempt / 'preserved.patch').write_bytes(b'preserved implementation\x00\xff')
        self.receipt = {'unit': 'successor', 'repo': 'owner/repo',
                        'pr': 'https://github.com/owner/repo/pull/42', 'target': 'main',
                        'head': 'a' * 40, 'merged_as': 'b' * 40, 'method': 'merge',
                        'merged': True, 'attested': True}
        units = [{'id': uid, 'kind': 'slurm', 'command': 'true', 'runtime': 'none',
                  'outputs': ['result'], 'needs': [], 'tracker': 'ARC-' + str(n)}
                 for n, uid in enumerate(('old', 'successor'), 1)]
        self.plan.write_text(json.dumps({'name': 'stop-test', 'units': units}))
        state = {'schema_version': 1, 'halted': None,
                 'plan_digest': S.SPR.plan_digest(json.loads(self.plan.read_text())), 'units': {
            'old': {'state': 'FAILED', 'attempt_dir': str(self.attempt),
                    'attempts': [str(self.attempt)], 'gpu_hours': 0,
                    'attempt_workspaces': {'attempt1': {'cleanup_pending': True}},
                    'preserved_branch': 'recovery/old'},
            'successor': {'state': 'DONE', 'attempt_dir': str(self.runs / 'successor' / 'attempt2'),
                          'attempts': [], 'gpu_hours': 0, 'job_id': 'finished',
                          'attempt_produced_heads': {'attempt2': self.receipt['head']},
                          'merge_receipt': self.receipt, 'merge_pr': self.receipt['pr'],
                          'merged_as': self.receipt['merged_as']}}}
        self.save(state)

    def load(self):
        return json.loads((self.state / SS.STATE_FILE).read_text())

    def save(self, state):
        (self.state / SS.STATE_FILE).write_text(json.dumps(state))

    def swarm(self, command, *args):
        argv = [str(SWARM / 'swarm.py'), command, str(self.plan),
                '--state-dir', str(self.state), *args]
        if getattr(self, 'clock', None):
            wrapper = '''
import datetime as dt
import os
from pathlib import Path
import runpy
import sys
import time
from unittest import mock
instant = dt.datetime.fromisoformat(sys.argv.pop(1))
sys.argv.pop(0)
sys.path.insert(0, str(Path(sys.argv[0]).parent))
os.environ['TZ'] = 'UTC'
time.tzset()
class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
strftime = time.strftime
def formatted(fmt, when=None):
    return strftime(fmt, time.gmtime(instant.timestamp()) if when is None else when)
with mock.patch.object(dt, 'datetime', Clock), mock.patch.object(time, 'strftime', formatted):
    runpy.run_path(sys.argv[0], run_name='__main__')
'''
            argv = ['-c', wrapper, self.clock, *argv]
        return subprocess.run([sys.executable, *argv],
                              cwd=str(self.root), capture_output=True, text=True, timeout=60)

    def stop(self, command='abandon', unit='old', approver='owner', reason='No longer needed', by='successor'):
        args = []
        for flag, value in (('--unit', unit), ('--approver', approver), ('--reason', reason)):
            if value is not None:
                args += [flag, value]
        if command == 'supersede' and by is not None:
            args += ['--by', by]
        return self.swarm(command, *args)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_refused(self, result, reason):
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn(reason, result.stderr)

    def intents(self):
        return SO.read_outbox(self.state)


class TestOperatorTerminal(StopFixture, unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.setup_stop()

    def test_cli_records_decision_and_preserves_evidence(self):
        for command, expected in (('abandon', 'ABANDONED'), ('supersede', 'SUPERSEDED')):
            with self.subTest(command=command):
                self.setup_stop()
                before = self.load()['units']['old']
                self.assert_ok(self.stop(command))
                us = self.load()['units']['old']
                decision = us['terminal_decision']
                self.assertEqual(us['state'], expected)
                self.assertEqual(decision['state'], expected)
                self.assertEqual(decision['prior_state'], 'FAILED')
                self.assertEqual(decision['approver'], 'owner')
                self.assertEqual(decision['reason'], 'No longer needed')
                self.assertEqual(dt.datetime.fromisoformat(decision['at']).utcoffset(), dt.timedelta(0))
                for key in ('attempt_dir', 'attempts', 'attempt_workspaces', 'preserved_branch'):
                    self.assertEqual(us[key], before[key])
                self.assertEqual((self.attempt / 'preserved.patch').read_bytes(), b'preserved implementation\x00\xff')
                self.assertGreater(json.loads((self.state / SS.STATE_EPOCH_FILE).read_text())['epoch'], 0)
                self.assertEqual(decision['successor'], 'successor' if command == 'supersede' else None)
                if command == 'supersede':
                    self.assertEqual(decision['merge_receipt'], self.receipt)
                status = self.swarm('status', '--json')
                self.assert_ok(status)
                row = next(r for r in json.loads(status.stdout)['units'] if r['id'] == 'old')
                self.assertEqual(row['state'], expected)
                self.assertEqual(row['reason'], decision['reason'])
                self.assertEqual(row['successor'], decision['successor'])
                text = self.swarm('status').stdout
                self.assertIn(expected, text)
                self.assertIn(decision['reason'], text)
                if command == 'supersede':
                    self.assertIn('successor: successor', text)

    def test_cli_refusals_are_usage_errors_and_leave_state_unchanged(self):
        original = (self.state / SS.STATE_FILE).read_bytes()
        for command in ('abandon', 'supersede'):
            cases = [({'unit': 'absent'}, 'unknown unit'), ({'unit': None}, 'unknown unit'),
                     ({'approver': None}, 'approver'), ({'reason': None}, 'reason'),
                     ({'approver': ''}, 'approver'), ({'approver': '  \t'}, 'approver'),
                     ({'reason': ''}, 'reason'), ({'reason': '  \t'}, 'reason'),
                     ({'unit': 'successor'}, 'DONE')]
            if command == 'supersede':
                cases += [({'by': 'elsewhere'}, 'unknown successor'),
                          ({'by': None}, 'unknown successor'),
                          ({'by': 'old'}, 'not DONE')]
            for values, reason in cases:
                with self.subTest(command=command, values=values):
                    self.assert_refused(self.stop(command, **values), reason)
                    self.assertEqual((self.state / SS.STATE_FILE).read_bytes(), original)
        self.assertFalse((self.state / SO.OUTBOX).exists())
        self.assertFalse((self.state / SS.STATE_EPOCH_FILE).exists())

    def test_cli_refuses_unreadable_or_malformed_plans_without_mutation(self):
        for command in ('abandon', 'supersede'):
            for plan in (None, '{', '[]', '{}', '{"units": "old"}',
                         '{"units": [null]}', '{"units": [{"id": " "}]}',
                         '{"units": [{"id": "old"}, {"id": "old"}]}'):
                with self.subTest(command=command, plan=plan):
                    self.setup_stop()
                    before = (self.state / SS.STATE_FILE).read_bytes()
                    if plan is None:
                        self.plan.unlink()
                    else:
                        self.plan.write_text(plan)
                    self.assert_refused(self.stop(command), 'error:')
                    self.assertEqual((self.state / SS.STATE_FILE).read_bytes(), before)
                    self.assertFalse((self.state / SO.OUTBOX).exists())
                    self.assertFalse((self.state / SS.STATE_EPOCH_FILE).exists())

    def test_cli_refuses_a_held_coordinator_lock_without_mutation(self):
        ok, holder = SS.acquire_lease(self.state, bump_epoch=False)
        self.assertTrue(ok, holder)
        try:
            before = (self.state / SS.STATE_FILE).read_bytes()
            for command in ('abandon', 'supersede'):
                self.assert_refused(self.stop(command), "cannot take this project's lock")
                self.assertEqual((self.state / SS.STATE_FILE).read_bytes(), before)
                self.assertFalse((self.state / SO.OUTBOX).exists())
                self.assertFalse((self.state / SS.STATE_EPOCH_FILE).exists())
        finally:
            SS.release_lease(self.state)

    def test_refuses_a_plan_that_is_not_the_coordinators_recorded_plan(self):
        state = self.load()
        state['plan_digest'] = S.SPR.plan_digest(json.loads(self.plan.read_text()))
        self.save(state)
        plan = json.loads(self.plan.read_text())
        plan['units'][0]['command'] = 'different-command'
        self.plan.write_text(json.dumps(plan))
        for command in ('abandon', 'supersede'):
            self.assert_refused(self.stop(command), 'plan differs')
            self.assertEqual(self.load(), state)

    def test_missing_plan_digest_refuses_both_commands_without_mutation(self):
        for command in ('abandon', 'supersede'):
            for missing in ('absent', 'null', 'empty'):
                with self.subTest(command=command, missing=missing):
                    self.setup_stop()
                    state = self.load()
                    state.pop('plan_digest')
                    if missing != 'absent':
                        state['plan_digest'] = None if missing == 'null' else ''
                    self.save(state)
                    before = (self.state / SS.STATE_FILE).read_bytes()
                    self.assert_refused(self.stop(command), 'missing plan_digest')
                    self.assertEqual((self.state / SS.STATE_FILE).read_bytes(), before)
                    self.assertFalse((self.state / SO.OUTBOX).exists())
                    self.assertFalse((self.state / SS.STATE_EPOCH_FILE).exists())

    def test_supersede_requires_successor_done_and_bound_merge_receipt(self):
        original = self.load()
        variants = [({'state': 'READY_FOR_PR'}, 'not DONE'),
                    ({'merge_receipt': None}, 'bound merge receipt'),
                    ({'attempt_produced_heads': {}}, 'bound merge receipt'),
                    ({'merged_as': 'c' * 40}, 'bound merge receipt'),
                    ({'merge_pr': 'another'}, 'bound merge receipt')]
        for field, value in (('head', 'c' * 40), ('unit', 'old'), ('merged', False),
                             ('attested', False), ('method', 'unknown')):
            variants.append(({'merge_receipt': dict(self.receipt, **{field: value})}, 'bound merge receipt'))
        for change, reason in variants:
            with self.subTest(change=change):
                state = copy.deepcopy(original)
                state['units']['successor'].update(change)
                self.save(state)
                self.assert_refused(self.stop('supersede'), reason)
                self.assertEqual(self.load(), state)
        self.assertFalse((self.state / SO.OUTBOX).exists())

    def test_idempotency_and_conflicting_second_calls(self):
        for command in ('abandon', 'supersede'):
            with self.subTest(command=command):
                self.setup_stop()
                self.assert_ok(self.stop(command))
                paths = [self.state / name for name in (SS.STATE_FILE, SS.STATE_EPOCH_FILE, SO.OUTBOX)]
                before = {p: p.read_bytes() for p in paths}
                self.assert_ok(self.stop(command))
                self.assertEqual({p: p.read_bytes() for p in paths}, before)
                for change in ({'reason': 'changed'}, {'approver': 'other'}):
                    self.assert_refused(self.stop(command, **change), 'conflicting')
                other = 'supersede' if command == 'abandon' else 'abandon'
                self.assert_refused(self.stop(other), 'conflicting')
                if command == 'supersede':
                    self.assert_refused(self.stop(command, by='old'), 'conflicting')
                self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_identical_command_repairs_missing_append_without_state_or_epoch_change(self):
        self.clock = '2026-10-16T20:00:00.800000+00:00'
        for command in ('abandon', 'supersede'):
            with self.subTest(command=command):
                self.setup_stop()
                self.assert_ok(self.stop(command))
                outbox = self.state / SO.OUTBOX
                intent = outbox.read_bytes()
                paths = [self.state / name for name in (SS.STATE_FILE, SS.STATE_EPOCH_FILE)]
                before = {p: p.read_bytes() for p in paths}
                outbox.unlink()
                self.assert_ok(self.stop(command))
                self.assertEqual(len(self.intents()), 1)
                self.assertEqual(outbox.read_bytes(), intent)
                self.assertEqual({p: p.read_bytes() for p in paths}, before)
                self.assert_ok(self.stop(command))
                self.assertEqual(outbox.read_bytes(), intent)
                self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_dependent_held_and_no_redispatch_or_cleanup_through_run_and_advance(self):
        for command in ('abandon', 'supersede'):
            for retained_attempt in (True, False):
                with self.subTest(command=command, retained_attempt=retained_attempt):
                    self.setup_stop()
                    state = self.load()
                    if not retained_attempt:
                        state['units']['old']['attempt_dir'] = None
                        self.save(state)
                    plan = json.loads(self.plan.read_text())
                    plan['units'].append({'id': 'dependent', 'kind': 'slurm', 'command': 'true',
                                          'runtime': 'none', 'needs': ['old'], 'outputs': ['result']})
                    self.plan.write_text(json.dumps(plan))
                    state['plan_digest'] = S.SPR.plan_digest(plan)
                    self.save(state)
                    self.assert_ok(self.stop(command))
                    saved = self.load()['units']['old']
                    for entry in ('run', 'advance', 'advance'):
                        result = self.swarm(entry, '--root', str(self.runs))
                        self.assert_ok(result)
                        current = self.load()
                        self.assertEqual(current['units']['old'], saved)
                        self.assertEqual(current['units']['dependent']['state'], 'HELD')
                        self.assertFalse(current['units']['dependent']['attempts'])
                        self.assertIn('dispatched 0 unit(s)', result.stdout)
                        self.assertEqual((self.attempt / 'preserved.patch').read_bytes(), b'preserved implementation\x00\xff')
                    status = json.loads(self.swarm('status', '--json').stdout)
                    self.assertEqual(next(r for r in status['units'] if r['id'] == 'dependent')['held_by'], ['old'])

    def test_outbox_uses_successor_evidence_and_repairs_missing_append(self):
        self.clock = '2026-10-16T20:00:00.800000+00:00'
        for command, verb in (('abandon', 'cancel'), ('supersede', 'close')):
            with self.subTest(command=command):
                self.setup_stop()
                (self.state / SO.OUTBOX).unlink(missing_ok=True)
                self.assert_ok(self.stop(command))
                intents = self.intents()
                self.assertEqual(len(intents), 1)
                intent = intents[0]
                self.assertEqual(intent['verb'], verb)
                self.assertEqual(intent['why'], 'No longer needed')
                self.assertEqual(SO.validate_intent(intent), [])
                if command == 'supersede':
                    self.assertEqual(intent['closing_evidence'], 'merged_pr')
                    self.assertEqual(intent['evidence'], {'successor': 'successor', 'receipt': self.receipt})
                (self.state / SO.OUTBOX).unlink()
                self.assert_ok(self.swarm('advance', '--root', str(self.runs)))
                repaired = next(i for i in self.intents() if i['unit'] == 'old')
                self.assertEqual(repaired, intent)

    def test_delayed_disposition_repair_uses_emission_time_and_next_sequence(self):
        for command in ('abandon', 'supersede'):
            with self.subTest(command=command):
                self.setup_stop()
                self.clock = '2026-10-16T20:00:00.800000+00:00'
                self.assert_ok(self.stop(command))
                original = self.intents()[0]
                paths = [self.state / name for name in (SS.STATE_FILE, SS.STATE_EPOCH_FILE)]
                before = {p: p.read_bytes() for p in paths}
                (self.state / SO.OUTBOX).unlink()
                with mock.patch.object(SO.time, 'strftime', return_value='2026-10-16T20:00:01+0000'):
                    SO.emit_intent(self.state, 'stop-test', 'sibling', 'SUBMITTED', {},
                                   tracker='ARC-1')
                self.clock = '2026-10-16T20:00:02.900000+00:00'
                self.assert_ok(self.stop(command))
                start, repaired = self.intents()
                self.assertEqual([start['seq'], repaired['seq']], [1, 2])
                self.assertEqual(repaired['at'], '2026-10-16T20:00:02+0000')
                self.assertEqual(original['at'], '2026-10-16T20:00:00+0000')
                self.assertEqual(repaired, dict(original, seq=2, at='2026-10-16T20:00:02+0000'))
                self.assertEqual({p: p.read_bytes() for p in paths}, before)
                outbox = (self.state / SO.OUTBOX).read_bytes()
                self.assert_ok(self.stop(command))
                self.assertEqual((self.state / SO.OUTBOX).read_bytes(), outbox)

    def test_code_unit_supersession_uses_successor_not_its_own_produced_head(self):
        plan = json.loads(self.plan.read_text())
        plan['units'][0].update(kind='code', repo=str(self.root / 'repo'), task='fix it')
        self.plan.write_text(json.dumps(plan))
        state = self.load()
        state['plan_digest'] = S.SPR.plan_digest(plan)
        state['units']['old']['attempt_produced_heads'] = {'attempt1': 'c' * 40}
        self.save(state)
        self.assert_ok(self.stop('supersede'))
        intent = self.intents()[0]
        self.assertEqual(intent['verb'], 'close')
        self.assertEqual(intent['evidence']['receipt']['head'], 'a' * 40)
        self.assertEqual(intent['evidence']['successor'], 'successor')

    def test_new_decision_fences_a_previously_loaded_snapshot(self):
        before = SS.load_state(self.state)
        self.assert_ok(self.stop())
        with self.assertRaisesRegex(SystemExit, 'another coordinator'):
            SS.save_state(self.state, before)
        self.assertEqual(self.load()['units']['old']['state'], 'ABANDONED')

    def test_live_stop_preserves_claim_occupancy_and_deadline_cannot_rewrite_it(self):
        plan = json.loads(self.plan.read_text())
        plan['units'][0]['deadline_s'] = 1
        self.plan.write_text(json.dumps(plan))
        state = self.load()
        state['plan_digest'] = S.SPR.plan_digest(plan)
        state['units']['old'].update(state='RUNNING', allocated_at=1, reason='deadline_exceeded',
                                     deadline_breach_active=True)
        self.save(state)
        self.assert_ok(self.stop())
        stopped = self.load()['units']['old']
        self.assertTrue(SS._occupies_live_resources(plan['units'][0], stopped))
        self.assert_ok(self.swarm('advance', '--root', str(self.runs)))
        self.assertEqual(self.load()['units']['old'], stopped)

    def test_stopped_slurm_job_releases_slot_only_after_registry_confirms_absence(self):
        for command in ('abandon', 'supersede'):
            with self.subTest(command=command):
                self.setup_stop()
                plan = json.loads(self.plan.read_text())
                plan['limits'] = {'max_running': 1, 'pools': {'workers': 1}}
                plan['units'][0]['pool'] = 'workers'
                plan['units'].append(dict(plan['units'][0], id='ready', tracker='ARC-3'))
                self.plan.write_text(json.dumps(plan))
                state = self.load()
                state['plan_digest'] = S.SPR.plan_digest(plan)
                state['units']['old'].update(state='RUNNING', job_id='778899')
                self.save(state)
                self.assert_ok(self.stop(command))
                stopped = self.load()['units']['old']
                self.assertTrue(SS._occupies_live_resources(plan['units'][0], stopped))
                for queue in (SQUEUE_LISTS, SQUEUE_BROKEN):
                    with fake_bin(sbatch=SBATCH, squeue=queue):
                        result = self.swarm('advance', '--root', str(self.runs))
                    self.assert_ok(result)
                    self.assertIn('dispatched 0 unit(s)', result.stdout)
                    self.assertEqual(self.load()['units']['old'], stopped)
                with fake_bin(sbatch=SBATCH, squeue=SQUEUE_SILENT):
                    self.assert_ok(self.swarm('advance', '--root', str(self.runs), '--dry-run'))
                    self.assertEqual(self.load()['units']['old'], stopped)
                    result = self.swarm('advance', '--root', str(self.runs))
                self.assert_ok(result)
                self.assertIn('dispatched 1 unit(s)', result.stdout)
                current = self.load()
                self.assertEqual(current['units']['ready']['state'], 'SUBMITTED')
                released = current['units']['old']
                self.assertFalse(SS._occupies_live_resources(plan['units'][0], released))
                observation = released.pop('terminal_resource_release')
                self.assertEqual(observation['attempt_dir'], stopped['attempt_dir'])
                self.assertEqual(observation['job_id'], stopped['job_id'])
                self.assertEqual(released, stopped)
                self.assertEqual((self.attempt / 'preserved.patch').read_bytes(),
                                 b'preserved implementation\x00\xff')
                for field in ('attempt_dir', 'job_id'):
                    changed = dict(current['units']['old'], terminal_resource_release=observation)
                    changed[field] = 'different'
                    self.assertTrue(SS._occupies_live_resources(plan['units'][0], changed))

    def test_resource_release_requires_a_local_registry_answer(self):
        for kind, host, answer, held in (
                ('code', os.uname().nodename, '[]', False),
                ('code', os.uname().nodename, '{}', True),
                ('code', os.uname().nodename, '', True),
                ('code', os.uname().nodename, '[null]', True),
                ('code', os.uname().nodename, '[{}]', True),
                ('code', 'another-host', '[]', True),
                ('code', None, '[]', True),
                ('pipeline', os.uname().nodename, '[]', True)):
            with self.subTest(kind=kind, host=host, answer=answer):
                self.setup_stop()
                plan = json.loads(self.plan.read_text())
                plan['units'][0]['kind'] = kind
                self.plan.write_text(json.dumps(plan))
                state = self.load()
                state['plan_digest'] = S.SPR.plan_digest(plan)
                state['units']['old'].update(state='RUNNING', job_id='agent',
                    attempt_launch_intents={'attempt1': {'launch_host': host}})
                self.save(state)
                self.assert_ok(self.stop())
                decision = self.load()['units']['old']['terminal_decision']
                ok, why = SS.acquire_lease(self.state)
                self.assertTrue(ok, why)
                try:
                    with fake_bin(paseo="printf '%s' '" + answer + "'"):
                        S.advance(plan, SS.load_state(self.state), str(self.state), str(self.runs), False)
                finally:
                    SS.release_lease(self.state)
                current = self.load()['units']['old']
                self.assertEqual(current['terminal_decision'], decision)
                self.assertEqual(SS._occupies_live_resources(plan['units'][0], current), held)

    def test_report_dispositions_override_preserved_attempt_verdict(self):
        for command, expected in (('abandon', 'ABANDONED'), ('supersede', 'SUPERSEDED')):
            with self.subTest(command=command):
                self.setup_stop()
                self.assert_ok(self.stop(command))
                # The retained receipt judged the earlier attempt; its missing
                # declared outputs do not invalidate an approver disposition.
                receipt = {'state': 'FAILED', 'outputs': {'other': {}}, 'kind': 'slurm'}
                data = {'plan': {'units': [json.loads(self.plan.read_text())['units'][0]]},
                        'state': {'units': {'old': self.load()['units']['old']}},
                        'receipts': {'old': receipt}, 'evidence': {}}
                rows = R.unit_rows(data)
                self.assertEqual(rows[0]['state'], expected)
                self.assertIsNone(rows[0]['state_conflict'])
                self.assertFalse(rows[0]['missing_outputs'])
                self.assertFalse(rows[0]['undeclared_outputs'])
                self.assertEqual(R.verdict(rows)[0], 'STOPPED')
                (self.attempt / 'receipt.json').write_text(json.dumps(receipt))
                result = subprocess.run([sys.executable, str(PROJECT / 'report.py'), str(self.root),
                                         '--state-dir', str(self.state), '--root', str(self.runs)],
                                        capture_output=True, text=True, timeout=60)
                self.assert_ok(result)
                rendered = (self.root / 'report.html').read_text()
                self.assertIn(expected, rendered)
                self.assertIn('No longer needed', rendered)
                if command == 'supersede':
                    self.assertIn('successor: successor', rendered)


class TestStoppedDrain(StopFixture, DrainCase):
    def setUp(self):
        super().setUp()
        self.setup_stop()
        self.fake.states += [{'id': 'canceled-second', 'type': 'canceled', 'position': 2},
                             {'id': 'canceled-first', 'type': 'canceled', 'position': 1}]

    def history(self):
        self.intent('old-start', 'start', unit='old')
        self.intent('old-pr', 'open_pr', unit='old', at='2026-10-05T11:00:00+0000')
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'started')

    def shared_issue(self):
        plan = json.loads(self.plan.read_text())
        plan['units'][1]['tracker'] = 'ARC-1'
        self.plan.write_text(json.dumps(plan))
        state = self.load()
        state['plan_digest'] = S.SPR.plan_digest(plan)
        state['units']['successor'] = {'state': 'WAITING', 'attempt_dir': None,
                                       'attempts': [], 'gpu_hours': 0}
        self.save(state)

    def assert_drained(self, expected, repeat=False):
        before = copy.deepcopy(self.fake.mutations)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.issues['1']['state']['type'], expected)
        if repeat:
            self.assertEqual(self.fake.mutations, before)
        self.assertEqual(self.audit(), 0, self.stdout + self.stderr)
        self.assertEqual(self.checks['intent_order'], 'CLEAN')

    def test_shared_issue_live_start_then_all_abandoned(self):
        self.shared_issue()
        self.assert_ok(self.stop())
        self.assert_drained('canceled')
        time.sleep(1.01)
        state = self.load()
        state['units']['successor']['state'] = 'SUBMITTED'
        self.save(state)
        self.assert_ok(self.swarm('advance', '--root', str(self.runs), '--max-new-dispatches', '0'))
        self.assertTrue(any(i['unit'] == 'successor' and i['verb'] == 'start' for i in self.intents()))
        self.assert_drained('started')
        self.assert_ok(self.stop(unit='successor'))
        self.assert_drained('canceled')
        self.assert_drained('canceled', repeat=True)

    def test_same_second_abandon_and_start_drain_in_both_emission_orders(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.setup_stop()
                self.shared_issue()
                self.fake.comments.clear()
                self.fake.mutations.clear()
                self.fake.issues['1']['state']['type'] = 'unstarted'
                (self.state / SO.RECEIPTS).unlink(missing_ok=True)
                operations = ('start', 'cancel') if reverse else ('cancel', 'start')
                for index, operation in enumerate(operations):
                    self.clock = '2026-10-16T20:00:00.%s00000+00:00' % (8 + index)
                    if operation == 'cancel':
                        self.assert_ok(self.stop())
                    else:
                        state = self.load()
                        state['units']['successor']['state'] = 'SUBMITTED'
                        self.save(state)
                        self.assert_ok(self.swarm('advance', '--root', str(self.runs),
                                                  '--max-new-dispatches', '0'))
                intents = self.intents()
                expected = 'canceled' if reverse else 'started'
                self.assert_drained(expected)
                sent = [LS.comment_markers({'body': v['input']['body']})['intent']
                        for v in self.writes('IntentCommentCreate')]
                self.assertEqual(sent, [i['key'] for i in intents])
                self.assertEqual([i['verb'] for i in intents], list(operations))
                self.assertEqual([i['seq'] for i in intents], [1, 2])
                self.assertEqual([i['at'] for i in intents],
                                 ['2026-10-16T20:00:00+0000'] * 2)
                self.assert_drained(expected, repeat=True)

    def test_shared_issue_cancel_never_replaces_done_close(self):
        self.shared_issue()
        state = self.load()
        raw = json.dumps({'state': 'DONE', 'kind': 'slurm',
                          'task_id': 'old', 'attempt_id': 'attempt1'}).encode()
        (self.attempt / 'receipt.json').write_bytes(raw)
        state['units']['old'].update(state='DONE', job_id='finished',
                                    attempt_receipt_seals={'attempt1': hashlib.sha256(raw).hexdigest()})
        self.save(state)
        self.assert_ok(self.swarm('advance', '--root', str(self.runs), '--max-new-dispatches', '0'))
        self.assertTrue(any(i['unit'] == 'old' and i['verb'] == 'close' for i in self.intents()))
        self.assert_drained('completed')
        self.assert_ok(self.stop(unit='successor'))
        self.assert_drained('completed')
        self.assert_drained('completed', repeat=True)
        self.draft.write_text(json.dumps({'project': {'linear_id': 'project', 'team': 'ARC'},
                                         'issues': [{'unit': u, 'identifier': 'ARC-1'}
                                                    for u in ('old', 'successor')]}))
        self.assertEqual(self.audit(plan=True, state=True), 0, self.stdout + self.stderr)
        self.assertEqual(self.checks['swarm_state'], 'CLEAN')
        self.fake.issues['1']['state']['type'] = 'unstarted'
        self.assertEqual(self.audit(plan=True, state=True), 1)
        self.assertEqual(self.checks['swarm_state'], 'DRIFT')
        self.assertEqual(self.checks['intent_order'], 'DRIFT')

    def test_two_drains_then_audit_have_no_intent_order_drift(self):
        self.history()
        self.assert_ok(self.stop())
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'canceled')
        self.assertEqual(self.writes('DrainState')[-1]['state'], 'canceled-first')
        writes = copy.deepcopy(self.fake.mutations)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.mutations, writes)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'canceled')
        self.assertEqual(self.audit(), 0, self.stdout + self.stderr)
        self.assertEqual(self.checks['intent_order'], 'CLEAN')
        self.assertTrue(any('No longer needed' in c['body'] for c in self.fake.comments.values()))

    def test_superseded_drains_completed_and_audits_clean_with_successor_url(self):
        self.history()
        self.assert_ok(self.stop('supersede'))
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.issues['1']['state']['type'], 'completed')
        self.assertEqual(self.writes('DrainState')[-1]['state'], 'completed-first')
        writes = copy.deepcopy(self.fake.mutations)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        self.assertEqual(self.fake.mutations, writes)
        self.assertEqual(self.audit(), 0, self.stdout + self.stderr)
        self.assertEqual(self.checks['intent_order'], 'CLEAN')
        body = '\n'.join(c['body'] for c in self.fake.comments.values())
        for text in ('No longer needed', 'successor', self.receipt['pr'], self.receipt['head']):
            self.assertIn(text, body)

    def test_cli_and_close_intent_consumers_share_receipt_admission(self):
        # Keep the coordinator binding fixed as receipt shape and identity vary.
        variants = [(None, None, True)]
        variants += [(field, value, False) for field, value in (
            ('attested', False), ('attested', None), ('attested', 1),
            ('merged', False), ('merged', None), ('merged', 1),
            ('unit', 'another'), ('repo', ''), ('pr', ''), ('target', ''),
            ('head', ''), ('merged_as', ''), ('method', 'unknown'),
            ('target_commit', 'invalid'), ('head', 'c' * 40),
            ('pr', 'https://github.com/owner/repo/pull/99'), ('merged_as', 'c' * 40))]
        for field, value, accepted in variants:
            with self.subTest(field=field, value=value):
                self.setup_stop()
                receipt = dict(self.receipt)
                if field:
                    if value is None:
                        receipt.pop(field)
                    else:
                        receipt[field] = value
                state = self.load()
                successor = state['units']['successor']
                successor['merge_receipt'] = receipt
                self.save(state)
                result = self.stop('supersede')
                if accepted:
                    self.assert_ok(result)
                else:
                    self.assert_refused(result, 'bound merge receipt')
                    self.assertEqual(self.load(), state)
                    self.assertFalse((self.state / SO.OUTBOX).exists())
                decision = dict(state='SUPERSEDED', approver='owner', reason='replacement',
                                at='2026-10-15T20:00:00+00:00',
                                successor='successor', merge_receipt=receipt)
                stopped = dict(state='SUPERSEDED', terminal_decision=decision)
                directory = self.root / 'consumer'
                directory.mkdir(exist_ok=True)
                (directory / SS.STATE_FILE).write_text(json.dumps(state))
                outbox = directory / SO.OUTBOX
                outbox.unlink(missing_ok=True)
                key = SO.emit_intent(directory, 'stop-test', 'old', 'SUPERSEDED', stopped)
                self.assertEqual(key is not None, accepted)
                # Also feed the collector an invalid candidate directly; an
                # emission refusal alone must not mask a lax drain consumer.
                valid = copy.deepcopy(stopped)
                valid['terminal_decision']['merge_receipt'] = self.receipt
                outbox.unlink(missing_ok=True)
                bound = copy.deepcopy(state)
                bound['units']['successor']['merge_receipt'] = self.receipt
                (directory / SS.STATE_FILE).write_text(json.dumps(bound))
                SO.emit_intent(directory, 'stop-test', 'old', 'SUPERSEDED', valid)
                intent = SO.read_outbox(directory)[0]
                (directory / SS.STATE_FILE).write_text(json.dumps(state))
                intent['evidence']['receipt'] = receipt
                intent.pop('envelope')
                outbox.write_text(json.dumps(SO.normalize_intent(intent)) + '\n')
                problems = []
                entries = LS.collect_intents([str(directory)], DC, problems.append)
                self.assertEqual(len(entries), 1 if accepted else 0, problems)
                self.assertEqual(bool(problems), not accepted)

    def test_section_lists_stopped_units_separately_from_running(self):
        self.assert_ok(self.stop())
        state = self.load()
        state['units']['replaced'] = {'state': 'SUPERSEDED', 'terminal_decision': {
            'reason': 'Replaced implementation', 'successor': 'successor'}}
        state['units']['working'] = {'state': 'RUNNING'}
        self.save(state)
        self.draft_file()
        self.assertEqual(self.cli('section', '--audit', str(self.out), '--draft', str(self.draft),
                                  '--plan', str(self.plan), '--state-dir', str(self.state)), 0)
        self.assertIn('ABANDONED: old: No longer needed', self.stdout)
        self.assertIn('SUPERSEDED: replaced: Replaced implementation (successor: successor)', self.stdout)
        self.assertIn('RUNNING: working\n', self.stdout)


if __name__ == '__main__':
    unittest.main()
