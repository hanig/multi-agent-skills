#!/usr/bin/env python3
"""Exercise both plan turns through the real CLI and a fake HTTP transport."""

import copy
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock
from tests.watchdog_guard import guarded_main

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'skills' / 'hanig-review-gate' / 'scripts'))
import committee
import review
plan_review = review._load_plan_review()

CLAIM = 'The design preserves every input record in its original order.'
FINDING = {'severity': 'major', 'confidence': 'high', 'file': 'plan.md',
           'line': 1, 'summary': 'Sorting reorders records.',
           'failure_scenario': 'Input B,A is emitted as A,B.'}
MEMBERS = [
    {'name': 'alpha', 'provider': 'openai', 'model': 'stub-alpha', 'profiles': ['plan']},
    {'name': 'beta', 'provider': 'openrouter', 'model': 'stub/beta', 'profiles': ['plan']},
]


def answer(findings=(), status='supported'):
    return {'verdict': 'upheld', 'findings': list(findings), 'claims': [
        {'claim_index': 0, 'claim': CLAIM, 'status': status,
         'why': 'The design retains the original sequence without sorting.'}]}


def answers(names=('alpha', 'beta')):
    first = {n: answer() for n in names}
    return first, {n: dict(answer(), own_findings=[], peer_responses=[]) for n in first}


class TestPlanExchange(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=review.JOURNAL_TEST_ROOT_PREFIX)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.plan = self.root / 'plan.md'
        self.plan.write_text('Read records sequentially and emit them without sorting.\n')
        self.env = mock.patch.dict(os.environ, {
            'XDG_STATE_HOME': str(self.root / 'state'),
            review.JOURNAL_TEST_MARKER: str(self.root),
            'OPENAI_API_KEY': 'test-plan-alpha-key',
            'OPENROUTER_API_KEY': 'test-plan-beta-key',
            'ANTHROPIC_API_KEY': 'test-plan-anthropic-key',
            'HANIG_ROUTING_SNAPSHOTS': 'off',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.first, self.second = answers()
        self.model_names = {m["model"]: m["name"] for m in MEMBERS}
        self.raw = {}
        self.requests = {}
        self.request_bodies = {}
        self.failures = {}
        self.responses = {}

    def finding(self, response='refute', defense='', kept=False):
        self.first['alpha']['findings'] = [copy.deepcopy(FINDING)]
        self.second['alpha']['own_findings'] = [
            {'finding_id': 'alpha:1', 'kept': kept, 'defense': defense}]
        self.second['beta']['peer_responses'] = [
            {'finding_id': 'alpha:1', 'response': response,
             'text': 'The plan explicitly retains input order and never sorts.'}]

    def transport(self, url, payload, headers, timeout, **kwargs):
        name = self.model_names[payload['model']]
        messages = payload.get('input', payload.get('messages'))
        turn = sum(m['role'] == 'user' for m in messages)
        self.requests[(name, turn)] = copy.deepcopy(messages)
        self.request_bodies[(name, turn)] = copy.deepcopy(payload)
        if (name, turn) in self.failures:
            return None, self.failures[(name, turn)]
        if (name, turn) in self.responses:
            return self.responses[(name, turn)], None
        raw = self.raw.get((name, turn), json.dumps(
            (self.first if turn == 1 else self.second)[name], indent=2))
        if 'api.anthropic.com' in url:
            return {'stop_reason': 'end_turn',
                    'content': [{'type': 'text', 'text': raw}],
                    'usage': {'input_tokens': 100 * turn, 'output_tokens': 40 * turn}}, None
        if 'api.openai.com' in url:
            return {'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'text': raw}]}],
                'usage': {'input_tokens': 100 * turn, 'output_tokens': 40 * turn}}, None
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}],
                'usage': {'prompt_tokens': 100 * turn, 'completion_tokens': 40 * turn}}, None

    def run_cli(self, *flags, json_output=True, members=MEMBERS):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ['review.py', '--kind', 'plan', '--file', str(self.plan),
                '--claim', CLAIM] + (['--json'] if json_output else []) + list(flags)
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(review, 'load_effective_reviewers', return_value=members), \
                mock.patch.object(committee.R, '_post', side_effect=self.transport), \
                mock.patch.object(review, 'run_one', side_effect=AssertionError('stateless path')), \
                redirect_stdout(stdout), redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as stopped:
            guarded_main(review.main)
        output = stdout.getvalue()
        return stopped.exception.code, (json.loads(output) if json_output and output else output), stderr.getvalue()

    def run_checked(self, state='PLAN_PASS'):
        code, report, _ = self.run_cli()
        expected = {'PLAN_PASS': 0, 'PLAN_FAIL': 1, 'REVIEW_UNAVAILABLE': 2}
        self.assertEqual((code, report['state']), (expected[state], state))
        return report

    def journal(self, report):
        self.assertTrue(report['journal']['written'], report['journal'])
        records = [json.loads(line) for line in
                   Path(report['journal']['path']).read_text().splitlines()]
        self.assertEqual(len(records), 1)
        return records[0]

    def test_plan_findings_accept_counterexample_fields_or_neither(self):
        example = {"setup": "records B,A", "input": "read records",
                   "expected": "B,A", "observed": "A,B"}
        reason = "Design has no executable implementation yet."
        for fields in ({}, {"counterexample": example}, {"non_executable_reason": reason},
                       {"counterexample": example, "non_executable_reason": reason}):
            with self.subTest(fields=fields):
                self.first, self.second = answers()
                self.finding(response='concur', kept=True)
                finding = dict(FINDING, **fields)
                self.first['alpha']['findings'] = [finding]
                self.second['beta']['findings'] = [finding]
                report = self.run_checked('PLAN_FAIL')
                record = self.journal(report)
                self.assertIsNone(record['round'])
                for result in record['results']:
                    for stored in result['findings']:
                        for field, value in finding.items():
                            self.assertEqual(stored[field], value)

    def assert_direct_panel_refused(self, members):
        args = SimpleNamespace(claim=[CLAIM], timeout=1, json=True,
                               panel_policy={}, input_redactions=[])
        stderr = io.StringIO()
        with mock.patch.object(committee.R, '_post', side_effect=self.transport) as transport, \
                mock.patch.object(review, 'record_review_round', return_value={}), \
                mock.patch.object(review, 'disarm_watchdog'), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = review.cmd_plan_review(
                args, members, 'Review this plan.', str(self.plan), False,
                'plan review', [])
        self.assertEqual(code, 4)
        transport.assert_not_called()
        self.assertEqual(self.requests, {})
        self.assertIn('REVIEW_ERROR', stderr.getvalue())

    def test_direct_one_seat_refused_before_transport(self):
        self.assert_direct_panel_refused(MEMBERS[:1])

    def test_direct_three_seats_refused_before_transport(self):
        self.assert_direct_panel_refused(MEMBERS + [
            dict(MEMBERS[0], name='gamma', model='stub-gamma')])

    def test_direct_same_maker_refused_before_transport(self):
        self.assert_direct_panel_refused([
            MEMBERS[0], dict(MEMBERS[1], model='openai/stub-beta')])

    def test_direct_duplicate_seat_names_refused_before_transport(self):
        self.assert_direct_panel_refused([
            MEMBERS[0], dict(MEMBERS[1], name=MEMBERS[0]['name'])])

    def test_cli_invalid_panels_refused_before_transport(self):
        for members in ([], MEMBERS[:1], MEMBERS + [dict(MEMBERS[0], name='gamma')],
                        [MEMBERS[0], dict(MEMBERS[1], model='openai/stub-beta')],
                        [MEMBERS[0], dict(MEMBERS[1], name=MEMBERS[0]['name'])]):
            with self.subTest(members=members):
                code, _, stderr = self.run_cli(members=members)
                self.assertEqual(code, 4)
                self.assertEqual(self.requests, {})
                self.assertIn('REVIEW_ERROR', stderr)

    def assert_exchange_panel_refused(self, members):
        stderr = io.StringIO()
        with mock.patch.object(committee.R, '_post', side_effect=self.transport) as transport, \
                redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
            plan_review._run_plan_exchange(
                members, self.plan.read_text(), [CLAIM], 1, plan_review._plan_schema_error)
        self.assertEqual(stopped.exception.code, 4)
        transport.assert_not_called()
        self.assertEqual(self.requests, {})
        self.assertIn('REVIEW_ERROR', stderr.getvalue())
        self.assertIn(plan_review._plan_panel_error(members), stderr.getvalue())

    def test_exchange_same_maker_refused_before_transport(self):
        self.assert_exchange_panel_refused([
            MEMBERS[0], dict(MEMBERS[1], model='openai/stub-beta')])

    def test_exchange_other_invalid_panels_refused_before_transport(self):
        for members in ([], MEMBERS[:1], MEMBERS + [dict(MEMBERS[0], name='gamma')],
                        [MEMBERS[0], dict(MEMBERS[1], name=MEMBERS[0]['name'])]):
            with self.subTest(members=members):
                self.assert_exchange_panel_refused(members)

    def run_shipped_pair(self, names):
        roster = json.loads((REPO / 'skills/hanig-review-gate/reviewers.json').read_text())
        members = [m for m in roster['reviewers'] if m['name'] in names]
        self.assertEqual({m['name'] for m in members}, set(names))
        self.first, self.second = answers(names)
        self.model_names = {m['model']: m['name'] for m in members}
        code, report, stderr = self.run_cli('--only', ','.join(names), members=members)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(report['state'], 'PLAN_PASS')
        self.assertEqual(set(self.requests), {(n, turn) for n in names for turn in (1, 2)})

    def test_two_makers_on_one_provider_complete_both_plan_turns(self):
        self.run_shipped_pair(('kimi-k3', 'deepseek-v4-pro'))

    def test_shipped_plan_pair_completes_both_plan_turns(self):
        self.run_shipped_pair(('astra', 'kimi-k3'))

    def test_maker_contrast_uses_vendor_or_bare_model_provider(self):
        for members in (
                [MEMBERS[0], dict(MEMBERS[1], model='openai/stub-beta')],
                [dict(MEMBERS[0], model='/stub-alpha'), dict(MEMBERS[1], model='openai/stub-beta')],
                [dict(MEMBERS[0], model='stub/alpha'), MEMBERS[1]],
                [MEMBERS[0], dict(MEMBERS[1], model='stub-beta', provider='openai')]):
            with self.subTest(members=members):
                self.assertIn('model maker', plan_review._plan_panel_error(members))
                code, _, stderr = self.run_cli(members=members)
                self.assertEqual(code, 4)
                self.assertIn('model maker', stderr)
                self.assertEqual(self.requests, {})

    def test_invalid_model_ids_refuse_without_transport(self):
        for model in (None, '', 1, []):
            with self.subTest(model=model):
                self.assert_direct_panel_refused([MEMBERS[0], dict(MEMBERS[1], model=model)])

    def test_cli_uses_joint_verdict_in_plan_owner(self):
        self.finding()
        with mock.patch.object(plan_review, '_joint_plan_verdict',
                               wraps=plan_review._joint_plan_verdict) as decide:
            code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (0, 'PLAN_PASS'))
        decide.assert_called_once()
        members, first, second, claims, truncated = decide.call_args.args
        self.assertEqual(members, MEMBERS)
        self.assertEqual(first, self.first)
        self.assertEqual(second, self.second)
        self.assertEqual(claims, [CLAIM])
        self.assertFalse(truncated)
        self.assertEqual(len(self.requests), 4)

    def test_file_loader_from_unrelated_cwd_runs_plan_command(self):
        (self.root / 'plan_review.py').write_text("raise AssertionError('cwd import')\n")
        code = '''
import importlib.util, io, sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
repo = Path(sys.argv[1])
scripts = repo / 'skills/hanig-review-gate/scripts'
assert str(scripts) not in sys.path
def load(name):
    spec = importlib.util.spec_from_file_location(name, scripts / 'review.py')
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
    return subject
subject = load('file_review')
owner = subject._load_plan_review()
assert sys.modules['_hanig_review_gate_plan_review'] is owner
assert Path(owner.__file__).resolve() == scripts / 'plan_review.py'
assert load('second_file_review')._load_plan_review() is owner
sys.path.insert(0, str(repo))
from tests.test_plan_review import TestPlanExchange, MEMBERS, CLAIM
import committee, review
assert review._load_plan_review() is owner
assert committee.R._load_plan_review() is owner
case = TestPlanExchange()
case.setUp()
try:
    args = SimpleNamespace(claim=[CLAIM], timeout=1, json=True,
                           panel_policy={}, input_redactions=[])
    with mock.patch.object(committee.R, '_post', side_effect=case.transport), \\
            mock.patch.object(subject, 'record_review_round', return_value={}), \\
            mock.patch.object(subject, 'disarm_watchdog'), redirect_stdout(io.StringIO()):
        result = subject.cmd_plan_review(args, MEMBERS, 'Review this plan.',
                                         'plan.md', False, 'plan review', [])
    assert result == 0, result
    assert len(case.requests) == 4, case.requests
finally:
    case.doCleanups()
'''
        result = subprocess.run([sys.executable, '-c', code, str(REPO)],
                                cwd=self.root, capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_refuted_finding_dropped_and_both_texts_recorded(self):
        self.finding()
        report = self.run_checked()
        table = report['plan_exchange']['joint_table']
        self.assertEqual(table[0]['outcome'], 'refuted')
        self.assertEqual(table[0]['finding'], FINDING)
        self.assertEqual(table[0]['response'], self.second['beta']['peer_responses'][0])
        self.assertEqual(report['results'][0]['findings'], [])
        record = self.journal(report)
        self.assertEqual(record['verdict'], 'PLAN_PASS')
        self.assertIsNone(record['round'])
        self.assertEqual(record['plan_exchange'], report['plan_exchange'])
        self.assertEqual(len(self.requests), 4)

    def test_unrefuted_new_finding_stands(self):
        self.second['alpha']['findings'] = [FINDING]
        report = self.run_checked('PLAN_FAIL')
        row = report['plan_exchange']['joint_table'][0]
        self.assertEqual(row['outcome'], 'standing')
        self.assertIsNone(row['response'])

    def test_vacuous_refutation_keeps_finding_and_reason(self):
        self.finding()
        self.second['beta']['peer_responses'][0]['text'] = 'No.'
        report = self.run_checked('PLAN_FAIL')
        row = report['plan_exchange']['joint_table'][0]
        self.assertEqual(row['outcome'], 'refutation_too_short')
        self.assertEqual(row['response']['text'], 'No.')
        self.assertEqual(report['results'][0]['findings'], [FINDING])
        self.assertEqual(self.journal(report)['plan_exchange'], report['plan_exchange'])

    def test_refutation_length_boundary_after_collapsing_whitespace(self):
        for length, outcome, state, code in (
                (39, 'refutation_too_short', 'PLAN_FAIL', 1),
                (40, 'refuted', 'PLAN_PASS', 0)):
            with self.subTest(length=length):
                self.finding()
                reason = '  \n' + 'x' * (length - 2) + '\t  \n y  '
                self.assertEqual(len(' '.join(reason.split())), length)
                self.second['beta']['peer_responses'][0]['text'] = reason
                actual_code, report, _ = self.run_cli()
                self.assertEqual((actual_code, report['state']), (code, state))
                row = report['plan_exchange']['joint_table'][0]
                self.assertEqual(row['outcome'], outcome)
                self.assertEqual(row['response']['text'], reason)

    def test_missing_or_blank_refutation_reason_keeps_finding(self):
        for text in (None, '', ' \t\n '):
            with self.subTest(text=text):
                self.finding()
                response = self.second['beta']['peer_responses'][0]
                if text is None:
                    del response['text']
                else:
                    response['text'] = text
                report = self.run_checked('PLAN_FAIL')
                row = report['plan_exchange']['joint_table'][0]
                self.assertEqual(row['outcome'], 'refutation_too_short')
                self.assertEqual(row['response'], response)
                self.assertEqual(report['results'][0]['findings'], [FINDING])

    def test_restated_refuted_finding_merges_and_is_dropped(self):
        self.finding()
        self.second['alpha']['findings'] = [copy.deepcopy(FINDING)]
        report = self.run_checked()
        table = report['plan_exchange']['joint_table']
        self.assertEqual([row['outcome'] for row in table], ['refuted', 'restated'])
        self.assertEqual(table[1]['finding'], FINDING)
        self.assertEqual(table[1]['restated_finding_id'], 'alpha:1')
        self.assertEqual(table[1]['response_outcome'], 'refuted')
        self.assertEqual(table[1]['response'], self.second['beta']['peer_responses'][0])
        self.assertEqual(report['results'][0]['findings'], [])
        self.assertEqual(self.journal(report)['plan_exchange'], report['plan_exchange'])
        self.assertEqual(len(self.requests), 4)

    def assert_restatement_severity(self, initial, restatements, expected,
                                   response='concur'):
        self.finding(response, kept=True)
        original = dict(FINDING, severity=initial)
        self.first['alpha']['findings'] = [original]
        self.second['alpha']['findings'] = [dict(FINDING, severity=severity)
                                            for severity in restatements]
        code, report, _ = self.run_cli()
        blocking = response != 'refute' and expected in ('major', 'critical')
        self.assertEqual((code, report['state']),
                         (1, 'PLAN_FAIL') if blocking else (0, 'PLAN_PASS'))
        table = report['plan_exchange']['joint_table']
        merged = dict(original, severity=expected)
        self.assertEqual(table[0]['finding'], merged)
        outcome = 'refuted' if response == 'refute' else 'standing'
        self.assertEqual(table[0]['outcome'], outcome)
        self.assertEqual(len(table), 1 + len(restatements))
        for row, severity in zip(table[1:], restatements):
            self.assertEqual(row['finding']['severity'], severity)
            self.assertEqual(row['outcome'], 'restated')
            self.assertEqual(row['restated_finding_id'], 'alpha:1')
            self.assertEqual(row['response_outcome'], outcome)
            self.assertEqual(row['response'], self.second['beta']['peer_responses'][0])
        self.assertEqual(report['results'][0]['findings'],
                         [] if response == 'refute' else [merged])
        recorded = report['plan_exchange']['members']['alpha']['answers']
        self.assertEqual(recorded[0]['verdict']['findings'], [original])
        self.assertEqual(json.loads(recorded[0]['raw'])['findings'], [original])
        self.assertEqual(recorded[1]['verdict']['findings'],
                         self.second['alpha']['findings'])
        self.assertEqual(self.journal(report)['plan_exchange'], report['plan_exchange'])
        self.assertEqual(len(self.requests), 4)

    def test_minor_restated_as_critical_fails_plan(self):
        self.assert_restatement_severity('minor', ['critical'], 'critical')

    def test_critical_restated_as_minor_stays_critical(self):
        self.assert_restatement_severity('critical', ['minor'], 'critical')

    def test_restatement_severity_order_and_repeated_merges(self):
        for initial, restatements, expected in (
                ('minor', ['minor'], 'minor'),
                ('minor', ['major'], 'major'),
                ('major', ['minor'], 'major'),
                ('major', ['major'], 'major'),
                ('major', ['critical'], 'critical'),
                ('critical', ['major'], 'critical'),
                ('critical', ['critical'], 'critical'),
                ('minor', ['critical', 'major', 'minor'], 'critical'),
                ('minor', ['major', 'critical'], 'critical')):
            with self.subTest(initial=initial, restatements=restatements):
                self.assert_restatement_severity(initial, restatements, expected)

    def test_escalated_restatement_keeps_peer_refutation(self):
        self.assert_restatement_severity('minor', ['critical'], 'critical',
                                        response='refute')

    def test_restated_text_normalization_is_exact_and_closed(self):
        original = dict(FINDING, summary='Caf\u00e9 STRASSE sorts records.',
                        failure_scenario='Input B,A becomes A,B.')
        for summary, scenario, merged in (
                ('  CAFE\u0301 stra\u00dfe\t sorts\nrecords. ',
                 '\n INPUT\t B,A   becomes A,B. ', True),
                ('Caf\u00e9 STRASSE sorts records!', original['failure_scenario'], False),
                ('Cafe STRASSE sorts records.', original['failure_scenario'], False),
                ('Caf\u00e9 STRASSE rearranges records.', original['failure_scenario'], False),
                (original['summary'], 'Input C,B becomes B,C.', False),
                ('\uff23af\u00e9 STRASSE sorts records.', original['failure_scenario'], False)):
            with self.subTest(summary=summary, scenario=scenario):
                self.finding()
                self.first['alpha']['findings'] = [original]
                restated = dict(original, summary=summary, failure_scenario=scenario)
                self.second['alpha']['findings'] = [restated]
                code, report, _ = self.run_cli()
                self.assertEqual((code, report['state']),
                                 (0, 'PLAN_PASS') if merged else (1, 'PLAN_FAIL'))
                row = report['plan_exchange']['joint_table'][1]
                self.assertEqual(row['outcome'], 'restated' if merged else 'standing')
                self.assertEqual(row['finding'], restated)

    def test_restated_finding_retains_original_response_outcome(self):
        for response, defense, reason, outcome in (
                ('concur', '', 'The ordering concern remains applicable.', 'standing'),
                ('refine', '', 'Only duplicate keys reorder records.', 'standing'),
                ('refute', '', 'No.', 'refutation_too_short'),
                ('refute', 'The plan separately sorts duplicate keys.',
                 'The plan explicitly retains input order and never sorts.', 'standing')):
            with self.subTest(response=response, defense=defense):
                self.finding(response, defense, kept=bool(defense))
                self.second['beta']['peer_responses'][0]['text'] = reason
                self.second['alpha']['findings'] = [copy.deepcopy(FINDING)]
                report = self.run_checked('PLAN_FAIL')
                table = report['plan_exchange']['joint_table']
                self.assertEqual(table[0]['outcome'], outcome)
                self.assertEqual(table[1]['outcome'], 'restated')
                self.assertEqual(table[1]['response_outcome'], outcome)
                self.assertEqual(report['results'][0]['findings'], [FINDING])

    def test_peer_finding_is_not_merged_as_own_restatement(self):
        self.finding()
        self.second['beta']['findings'] = [copy.deepcopy(FINDING)]
        report = self.run_checked('PLAN_FAIL')
        row = report['plan_exchange']['joint_table'][1]
        self.assertEqual((row['author'], row['outcome']), ('beta', 'standing'))
        self.assertIsNone(row['response'])

    def test_nontext_finding_fields_are_not_restatement_keys(self):
        for field in ('summary', 'failure_scenario'):
            for value in (None, [], {}):
                with self.subTest(field=field, value=value):
                    self.finding()
                    finding = dict(FINDING, severity='minor', **{field: value})
                    self.first['alpha']['findings'] = [finding]
                    self.second['alpha']['findings'] = [finding]
                    report = self.run_checked()
                    self.assertEqual(report['plan_exchange']['joint_table'][1]['outcome'],
                                     'standing')
                    self.assertEqual(report['results'][0]['findings'], [finding])

    def test_concurred_finding_stands(self):
        self.finding('concur', kept=True)
        report = self.run_checked('PLAN_FAIL')
        self.assertEqual(report['plan_exchange']['joint_table'][0]['outcome'], 'standing')

    def test_refined_finding_stands_with_corrected_text(self):
        self.finding('refine', kept=True)
        self.second['beta']['peer_responses'][0]['text'] = 'Only duplicate keys reorder records.'
        code, report, _ = self.run_cli()
        self.assertEqual(code, 1)
        row = report['plan_exchange']['joint_table'][0]
        self.assertEqual(row['outcome'], 'standing')
        self.assertEqual(row['response']['text'], 'Only duplicate keys reorder records.')

    def test_refuted_but_defended_finding_stands(self):
        self.finding(defense='The plan separately sorts duplicate keys, changing their order.', kept=True)
        report = self.run_checked('PLAN_FAIL')
        self.assertEqual(report['plan_exchange']['joint_table'][0]['outcome'], 'standing')

    def test_kept_without_defense_does_not_overrule_refutation(self):
        self.finding(kept=True)
        report = self.run_checked()
        self.assertEqual(report['plan_exchange']['joint_table'][0]['outcome'], 'refuted')

    def test_withdrawal_alone_does_not_drop_unrefuted_finding(self):
        self.finding('concur', kept=False)
        report = self.run_checked('PLAN_FAIL')

    def test_either_seat_unavailable_in_either_turn_never_passes(self):
        for name in ('alpha', 'beta'):
            for turn in (1, 2):
                with self.subTest(name=name, turn=turn):
                    self.requests.clear()
                    self.failures = {(name, turn): 'simulated provider failure'}
                    report = self.run_checked('REVIEW_UNAVAILABLE')
                    self.assertEqual(report['plan_exchange']['joint_table'], [])
                    self.assertEqual(len(self.requests), 2 * turn)
                    answers = report['plan_exchange']['members'][name]['answers']
                    self.assertEqual(answers[-1]['error'], 'simulated provider failure')

    def test_turn_two_delivers_verbatim_peer_output_and_own_history(self):
        for name in self.first:
            self.raw[(name, 1)] = '  \n```json\n' + json.dumps(self.first[name], indent=3) + '\n``` \n'
        code, report, _ = self.run_cli()
        self.assertEqual(code, 0)
        for name, peer in [('alpha', 'beta'), ('beta', 'alpha')]:
            messages = self.requests[(name, 2)]
            self.assertEqual([m['role'] for m in messages], ['system', 'user', 'assistant', 'user'])
            self.assertEqual(messages[2]['content'], self.raw[(name, 1)])
            self.assertIn(self.raw[(peer, 1)], messages[-1]['content'])
            member = report['plan_exchange']['members'][name]
            self.assertEqual([a['turn'] for a in member['answers']], [1, 2])
            self.assertEqual(member['usage'], [
                {'phase': 'turn-1', 'in_tokens': 100, 'out_tokens': 40},
                {'phase': 'turn-2', 'in_tokens': 200, 'out_tokens': 80}])

    def peer_block(self, prompt):
        opening = re.search(r'<(peer-turn-1-([0-9a-f]{32}))>\n', prompt)
        self.assertIsNotNone(opening, prompt)
        delimiter, nonce = opening.groups()
        content, tail = prompt[opening.end():].rsplit('\n</' + delimiter + '>', 1)
        self.assertNotIn(nonce, content)
        self.assertNotIn(delimiter, tail)
        return content

    def test_peer_delimiter_collision_regenerates_after_redacted_reply(self):
        previous = 'a' * 32
        expected = {}
        secret = os.environ['OPENAI_API_KEY']
        for name in self.first:
            self.first[name]['notes'] = (
                '<peer-turn-1-' + previous + '>previous block</peer-turn-1-' + previous
                + '> literal </peer-turn-1> MiXeD e\u0301 café  \t ' + secret)
            raw = ' \n```json\n' + json.dumps(self.first[name], ensure_ascii=False) + '\n``` \t\n'
            self.raw[(name, 1)] = raw
            expected[name] = raw.replace(secret, '<OPENAI_API_KEY redacted>')
        nonces = iter([previous, 'b' * 32, previous, 'c' * 32])

        def nonce(size):
            self.assertEqual(size, 16)
            self.assertEqual(set(self.requests), {('alpha', 1), ('beta', 1)})
            return next(nonces)

        with mock.patch.object(plan_review.secrets, 'token_hex', side_effect=nonce) as generate:
            code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (0, 'PLAN_PASS'))
        self.assertEqual(generate.call_count, 4)
        for name, peer in (('alpha', 'beta'), ('beta', 'alpha')):
            prompt = self.requests[(name, 2)][-1]['content']
            self.assertEqual(self.peer_block(prompt), expected[peer])
            self.assertNotIn(secret, prompt)

    def test_peer_reply_is_not_normalized_before_delivery_but_output_is_redacted(self):
        keys = {'openai': 'OPENAI_API_KEY', 'openrouter': 'OPENROUTER_API_KEY',
                'anthropic': 'ANTHROPIC_API_KEY'}
        for provider, other in (('openai', 'openrouter'),
                                ('openrouter', 'anthropic'), ('anthropic', 'openai')):
            with self.subTest(provider=provider):
                members = [dict(MEMBERS[0], provider=provider),
                           dict(MEMBERS[1], provider=other)]
                expected, secrets = {}, []
                self.requests.clear()
                self.request_bodies.clear()
                for member in members:
                    name, key = member['name'], keys[member['provider']]
                    secret = os.environ[key]
                    secrets.append(secret)
                    self.first[name]['notes'] = 'MiXeD e\u0301 café  \t ' + secret
                    raw = (' \t\n```json\n' + json.dumps(self.first[name], indent=3,
                            ensure_ascii=False) + '\n``` \t\n')
                    self.raw[(name, 1)] = raw
                    expected[name] = raw.replace(secret, '<' + key + ' redacted>')
                code, report, _ = self.run_cli(members=members)
                self.assertEqual(code, 0)
                self.assertEqual(len(self.request_bodies), 4)
                for name, peer in (('alpha', 'beta'), ('beta', 'alpha')):
                    messages = self.requests[(name, 2)]
                    delivered = self.peer_block(messages[-1]['content'])
                    self.assertEqual(delivered, expected[peer])
                    own_history = next(m['content'] for m in messages
                                       if m['role'] == 'assistant')
                    self.assertEqual(own_history, expected[name])
                journal = self.journal(report)
                for secret in secrets:
                    for body in self.request_bodies.values():
                        self.assertNotIn(secret, json.dumps(body, ensure_ascii=False))
                    self.assertNotIn(secret, json.dumps(report))
                    self.assertNotIn(secret, json.dumps(journal))

    def test_claim_upheld_by_only_one_seat_fails(self):
        for status in ('refuted', 'unverifiable'):
            with self.subTest(status=status):
                self.second['beta']['claims'][0]['status'] = status
                report = self.run_checked('PLAN_FAIL')

    def test_turn_one_claim_refutation_can_be_reconsidered(self):
        self.first['beta']['claims'][0]['status'] = 'refuted'
        report = self.run_checked()

    def test_minor_finding_does_not_block_supported_claims(self):
        self.second['alpha']['findings'] = [dict(FINDING, severity='minor')]
        report = self.run_checked()

    def test_standing_major_is_not_filtered_by_confidence_or_scope(self):
        self.second['alpha']['findings'] = [dict(FINDING, confidence='low', in_scope=False)]
        report = self.run_checked('PLAN_FAIL')

    def test_missing_duplicate_or_unknown_response_is_unavailable(self):
        self.finding('concur', kept=True)
        valid = self.second['beta']['peer_responses'][0]
        for entries in ([], [valid, valid], [dict(valid, finding_id='alpha:9')],
                        [dict(valid, response='abstain')], [dict(valid, text='')]):
            with self.subTest(entries=entries):
                self.second['beta']['peer_responses'] = entries
                report = self.run_checked('REVIEW_UNAVAILABLE')

    def test_invalid_own_position_is_unavailable(self):
        self.finding()
        for entries in ([], [{'finding_id': 'alpha:1', 'kept': 'true', 'defense': ''}],
                        [{'finding_id': 'alpha:1', 'kept': False, 'defense': 'contradiction'}]):
            with self.subTest(entries=entries):
                self.second['alpha']['own_findings'] = entries
                report = self.run_checked('REVIEW_UNAVAILABLE')

    def test_malformed_answer_is_unavailable_without_third_turn(self):
        self.raw[('beta', 2)] = '{bad JSON'
        report = self.run_checked('REVIEW_UNAVAILABLE')
        self.assertEqual(len(self.requests), 4)

    def assert_incomplete_turn_unavailable(self, turn):
        for seat in ('alpha', 'beta'):
            with self.subTest(seat=seat):
                self.raw = {(seat, turn): json.dumps({'findings': [],
                            'own_findings': [], 'peer_responses': []})}
                with mock.patch.object(plan_review, '_joint_plan_verdict',
                                       wraps=plan_review._joint_plan_verdict) as decide:
                    code, report, _ = self.run_cli()
                self.assertEqual((code, report['state']), (2, 'REVIEW_UNAVAILABLE'))
                self.assertEqual(report['plan_exchange']['joint_table'], [])
                decide.assert_called_once()

    def test_cli_incomplete_turn_one_is_unavailable(self):
        self.assert_incomplete_turn_unavailable(1)

    def test_cli_incomplete_turn_two_is_unavailable(self):
        self.assert_incomplete_turn_unavailable(2)

    def test_truncated_provider_reply_is_unavailable(self):
        self.responses[('beta', 2)] = {'choices': [
            {'finish_reason': 'length', 'message': {'content': json.dumps(self.second['beta'])}}]}
        report = self.run_checked('REVIEW_UNAVAILABLE')

    def test_truncated_input_cannot_pass(self):
        with mock.patch.object(review, 'MAX_CHARS', 10):
            code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))
        self.assertTrue(report['truncated'])

    def test_missing_key_does_not_run_single_seat(self):
        with mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': ''}):
            code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (2, 'REVIEW_UNAVAILABLE'))
        self.assertEqual(self.requests, {})

    def test_author_exclusion_remains_binding(self):
        code, report, _ = self.run_cli('--author', 'codex/stub-alpha')
        self.assertEqual((code, report['state']), (2, 'REVIEW_UNAVAILABLE'))
        self.assertEqual(self.requests, {})

    def test_round_and_escalation_refused_before_transport(self):
        for flags in (('--round', '1'), ('--escalate',)):
            with self.subTest(flags=flags):
                code, _, _ = self.run_cli(*flags)
                self.assertEqual(code, 4)
                self.assertEqual(self.requests, {})

    def test_text_output_shows_both_turns_and_joint_table(self):
        self.finding()
        code, output, _ = self.run_cli(json_output=False)
        self.assertEqual(code, 0)
        for text in ('alpha TURN 1', 'alpha TURN 2', 'beta TURN 1', 'beta TURN 2',
                     'JOINT TABLE:', 'refuted', 'PLAN_PASS'):
            self.assertIn(text, output)


class TestJointPlanPanel(unittest.TestCase):
    def assert_unavailable(self, members, first, second):
        self.assertEqual(plan_review._joint_plan_verdict(members, first, second, [CLAIM]),
                         ('REVIEW_UNAVAILABLE', []))

    def test_only_command_is_public_and_helpers_stay_in_plan_module(self):
        self.assertTrue(callable(review.cmd_plan_review))
        for name in ('MIN_PLAN_REFUTATION_CHARS', 'plan_finding_ids',
                     'plan_finding_text', 'plan_cross_prompt', 'plan_schema_error',
                     'joint_plan_verdict', 'run_plan_exchange', 'plan_panel_error'):
            with self.subTest(name=name):
                self.assertFalse(hasattr(review, name))
                self.assertFalse(hasattr(plan_review, name))
        for name in ('_joint_plan_verdict', '_run_plan_exchange',
                     '_plan_panel_error', '_plan_schema_error'):
            self.assertFalse(hasattr(review, name))
            self.assertTrue(callable(getattr(plan_review, name)))
        for name in ('_MIN_PLAN_REFUTATION_CHARS', '_plan_finding_ids',
                     '_plan_finding_text', '_plan_cross_prompt'):
            self.assertFalse(hasattr(review, name))
            self.assertTrue(hasattr(plan_review, name))

    def test_incomplete_answer_is_unavailable_directly(self):
        for turn in (1, 2):
            for seat in ('alpha', 'beta'):
                for bad in (None, [], {}, {'findings': []},
                            dict(answer(), verdict=None), dict(answer(), claims=[]),
                            dict(answer(), findings='not an array')):
                    with self.subTest(turn=turn, seat=seat, bad=bad):
                        first, second = answers()
                        (first if turn == 1 else second)[seat] = bad
                        self.assert_unavailable(MEMBERS, first, second)

    def test_turn_two_must_cover_both_finding_id_sets_directly(self):
        first = {'alpha': answer([FINDING]), 'beta': answer()}
        second = {'alpha': dict(answer(), own_findings=[
            {'finding_id': 'alpha:1', 'kept': False, 'defense': ''}], peer_responses=[]),
            'beta': dict(answer(), own_findings=[], peer_responses=[
                {'finding_id': 'alpha:1', 'response': 'refute',
                 'text': 'The plan retains the input order and never sorts any records.'}])}
        self.assertEqual(plan_review._joint_plan_verdict(
            MEMBERS, first, second, [CLAIM])[0], 'PLAN_PASS')
        for seat, field in (('alpha', 'own_findings'), ('beta', 'peer_responses')):
            for bad in (None, [], [{'finding_id': 'unknown'}]):
                with self.subTest(seat=seat, field=field, bad=bad):
                    incomplete = copy.deepcopy(second)
                    incomplete[seat][field] = bad
                    self.assert_unavailable(MEMBERS, first, incomplete)

    def test_empty_panel_is_unavailable(self):
        self.assert_unavailable([], {}, {})

    def test_malformed_panel_or_turn_map_is_unavailable(self):
        first, second = answers()
        for bad in (None, 1, 'alpha,beta'):
            with self.subTest(bad=bad):
                for members, one, two in ((bad, first, second),
                                           (MEMBERS, bad, second), (MEMBERS, first, bad)):
                    self.assert_unavailable(members, one, two)

    def test_one_seat_is_unavailable(self):
        first, second = answers(['alpha'])
        self.assert_unavailable(MEMBERS[:1], first, second)

    def test_same_maker_is_unavailable(self):
        members = [MEMBERS[0], dict(MEMBERS[1], model='openai/stub-beta')]
        first, second = answers(m['name'] for m in members)
        self.assert_unavailable(members, first, second)

    def test_unknown_providers_are_unavailable(self):
        members = [dict(m, provider=p) for m, p in zip(MEMBERS, ('bogus-a', 'bogus-b'))]
        first, second = answers(m['name'] for m in members)
        self.assert_unavailable(members, first, second)

    def test_other_invalid_panels_are_unavailable(self):
        for members in (MEMBERS + [dict(MEMBERS[0], name='gamma')],
                        [MEMBERS[0], dict(MEMBERS[1], name=MEMBERS[0]['name'])],
                        ['alpha', 'beta'],
                        [MEMBERS[0], {'name': 'beta'}]):
            with self.subTest(members=members):
                names = [m['name'] if isinstance(m, dict) else m for m in members]
                first, second = answers(names)
                self.assert_unavailable(members, first, second)

    def test_both_seats_must_complete_both_turns(self):
        for turn in (1, 2):
            for seat in ('alpha', 'beta'):
                with self.subTest(turn=turn, seat=seat):
                    first, second = answers()
                    del (first if turn == 1 else second)[seat]
                    self.assert_unavailable(MEMBERS, first, second)


if __name__ == '__main__':
    unittest.main()
