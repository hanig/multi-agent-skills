#!/usr/bin/env python3
"""Exercise both plan turns through the real CLI and a fake HTTP transport."""

import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'skills' / 'hanig-review-gate' / 'scripts'))
import committee
import review

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
            'HANIG_ROUTING_SNAPSHOTS': 'off',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.first = {'alpha': answer(), 'beta': answer()}
        self.second = {n: dict(answer(), own_findings=[], peer_responses=[])
                       for n in self.first}
        self.raw = {}
        self.requests = {}
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
        name = 'alpha' if payload['model'] == 'stub-alpha' else 'beta'
        messages = payload.get('input', payload.get('messages'))
        turn = sum(m['role'] == 'user' for m in messages)
        self.requests[(name, turn)] = copy.deepcopy(messages)
        if (name, turn) in self.failures:
            return None, self.failures[(name, turn)]
        if (name, turn) in self.responses:
            return self.responses[(name, turn)], None
        raw = self.raw.get((name, turn), json.dumps(
            (self.first if turn == 1 else self.second)[name], indent=2))
        if name == 'alpha':
            return {'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'text': raw}]}],
                'usage': {'input_tokens': 100 * turn, 'output_tokens': 40 * turn}}, None
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}],
                'usage': {'prompt_tokens': 100 * turn, 'completion_tokens': 40 * turn}}, None

    def run_cli(self, *flags, json_output=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ['review.py', '--kind', 'plan', '--file', str(self.plan),
                '--claim', CLAIM] + (['--json'] if json_output else []) + list(flags)
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(review, 'load_effective_reviewers', return_value=MEMBERS), \
                mock.patch.object(committee.R, '_post', side_effect=self.transport), \
                mock.patch.object(review, 'arm_watchdog'), \
                mock.patch.object(review, 'disarm_watchdog'), \
                mock.patch.object(review, 'run_one', side_effect=AssertionError('stateless path')), \
                redirect_stdout(stdout), redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as stopped:
            review.main()
        output = stdout.getvalue()
        return stopped.exception.code, (json.loads(output) if json_output and output else output), stderr.getvalue()

    def journal(self, report):
        self.assertTrue(report['journal']['written'], report['journal'])
        records = [json.loads(line) for line in
                   Path(report['journal']['path']).read_text().splitlines()]
        self.assertEqual(len(records), 1)
        return records[0]

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

    def test_direct_same_provider_refused_before_transport(self):
        self.assert_direct_panel_refused([
            MEMBERS[0], dict(MEMBERS[1], provider=MEMBERS[0]['provider'])])

    def test_direct_duplicate_seat_names_refused_before_transport(self):
        self.assert_direct_panel_refused([
            MEMBERS[0], dict(MEMBERS[1], name=MEMBERS[0]['name'])])

    def test_refuted_finding_dropped_and_both_texts_recorded(self):
        self.finding()
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (0, 'PLAN_PASS'))
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
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))
        row = report['plan_exchange']['joint_table'][0]
        self.assertEqual(row['outcome'], 'standing')
        self.assertIsNone(row['response'])

    def test_vacuous_refutation_keeps_finding_and_reason(self):
        self.finding()
        self.second['beta']['peer_responses'][0]['text'] = 'No.'
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))
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
                code, report, _ = self.run_cli()
                self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))
                row = report['plan_exchange']['joint_table'][0]
                self.assertEqual(row['outcome'], 'refutation_too_short')
                self.assertEqual(row['response'], response)
                self.assertEqual(report['results'][0]['findings'], [FINDING])

    def test_restated_refuted_finding_merges_and_is_dropped(self):
        self.finding()
        self.second['alpha']['findings'] = [copy.deepcopy(FINDING)]
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (0, 'PLAN_PASS'))
        table = report['plan_exchange']['joint_table']
        self.assertEqual([row['outcome'] for row in table], ['refuted', 'restated'])
        self.assertEqual(table[1]['finding'], FINDING)
        self.assertEqual(table[1]['restated_finding_id'], 'alpha:1')
        self.assertEqual(table[1]['response_outcome'], 'refuted')
        self.assertEqual(table[1]['response'], self.second['beta']['peer_responses'][0])
        self.assertEqual(report['results'][0]['findings'], [])
        self.assertEqual(self.journal(report)['plan_exchange'], report['plan_exchange'])
        self.assertEqual(len(self.requests), 4)

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
                code, report, _ = self.run_cli()
                self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))
                table = report['plan_exchange']['joint_table']
                self.assertEqual(table[0]['outcome'], outcome)
                self.assertEqual(table[1]['outcome'], 'restated')
                self.assertEqual(table[1]['response_outcome'], outcome)
                self.assertEqual(report['results'][0]['findings'], [FINDING])

    def test_peer_finding_is_not_merged_as_own_restatement(self):
        self.finding()
        self.second['beta']['findings'] = [copy.deepcopy(FINDING)]
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))
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
                    code, report, _ = self.run_cli()
                    self.assertEqual((code, report['state']), (0, 'PLAN_PASS'))
                    self.assertEqual(report['plan_exchange']['joint_table'][1]['outcome'],
                                     'standing')
                    self.assertEqual(report['results'][0]['findings'], [finding])

    def test_concurred_finding_stands(self):
        self.finding('concur', kept=True)
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))
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
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))
        self.assertEqual(report['plan_exchange']['joint_table'][0]['outcome'], 'standing')

    def test_kept_without_defense_does_not_overrule_refutation(self):
        self.finding(kept=True)
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (0, 'PLAN_PASS'))
        self.assertEqual(report['plan_exchange']['joint_table'][0]['outcome'], 'refuted')

    def test_withdrawal_alone_does_not_drop_unrefuted_finding(self):
        self.finding('concur', kept=False)
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))

    def test_either_seat_unavailable_in_either_turn_never_passes(self):
        for name in ('alpha', 'beta'):
            for turn in (1, 2):
                with self.subTest(name=name, turn=turn):
                    self.requests.clear()
                    self.failures = {(name, turn): 'simulated provider failure'}
                    code, report, _ = self.run_cli()
                    self.assertEqual((code, report['state']), (2, 'REVIEW_UNAVAILABLE'))
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

    def test_peer_reply_is_not_normalized_before_delivery_but_output_is_redacted(self):
        secret = os.environ['OPENAI_API_KEY']
        self.first['alpha']['notes'] = secret
        self.raw[('alpha', 1)] = json.dumps(self.first['alpha'])
        code, report, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn(self.raw[('alpha', 1)], self.requests[('beta', 2)][-1]['content'])
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn(secret, json.dumps(self.journal(report)))

    def test_claim_upheld_by_only_one_seat_fails(self):
        for status in ('refuted', 'unverifiable'):
            with self.subTest(status=status):
                self.second['beta']['claims'][0]['status'] = status
                code, report, _ = self.run_cli()
                self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))

    def test_turn_one_claim_refutation_can_be_reconsidered(self):
        self.first['beta']['claims'][0]['status'] = 'refuted'
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (0, 'PLAN_PASS'))

    def test_minor_finding_does_not_block_supported_claims(self):
        self.second['alpha']['findings'] = [dict(FINDING, severity='minor')]
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (0, 'PLAN_PASS'))

    def test_standing_major_is_not_filtered_by_confidence_or_scope(self):
        self.second['alpha']['findings'] = [dict(FINDING, confidence='low', in_scope=False)]
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (1, 'PLAN_FAIL'))

    def test_missing_duplicate_or_unknown_response_is_unavailable(self):
        self.finding('concur', kept=True)
        valid = self.second['beta']['peer_responses'][0]
        for entries in ([], [valid, valid], [dict(valid, finding_id='alpha:9')],
                        [dict(valid, response='abstain')], [dict(valid, text='')]):
            with self.subTest(entries=entries):
                self.second['beta']['peer_responses'] = entries
                code, report, _ = self.run_cli()
                self.assertEqual((code, report['state']), (2, 'REVIEW_UNAVAILABLE'))

    def test_invalid_own_position_is_unavailable(self):
        self.finding()
        for entries in ([], [{'finding_id': 'alpha:1', 'kept': 'true', 'defense': ''}],
                        [{'finding_id': 'alpha:1', 'kept': False, 'defense': 'contradiction'}]):
            with self.subTest(entries=entries):
                self.second['alpha']['own_findings'] = entries
                code, report, _ = self.run_cli()
                self.assertEqual((code, report['state']), (2, 'REVIEW_UNAVAILABLE'))

    def test_malformed_answer_is_unavailable_without_third_turn(self):
        self.raw[('beta', 2)] = '{bad JSON'
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (2, 'REVIEW_UNAVAILABLE'))
        self.assertEqual(len(self.requests), 4)

    def test_truncated_provider_reply_is_unavailable(self):
        self.responses[('beta', 2)] = {'choices': [
            {'finish_reason': 'length', 'message': {'content': json.dumps(self.second['beta'])}}]}
        code, report, _ = self.run_cli()
        self.assertEqual((code, report['state']), (2, 'REVIEW_UNAVAILABLE'))

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
    def test_empty_panel_is_unavailable(self):
        self.assertEqual(review.joint_plan_verdict([], {}, {}, [CLAIM]),
                         ('REVIEW_UNAVAILABLE', []))

    def test_one_seat_is_unavailable(self):
        self.assertEqual(review.joint_plan_verdict(
            ['alpha'], {'alpha': answer()},
            {'alpha': dict(answer(), own_findings=[], peer_responses=[])}, [CLAIM]),
            ('REVIEW_UNAVAILABLE', []))

    def test_both_seats_must_complete_both_turns(self):
        for turn in (1, 2):
            for seat in ('alpha', 'beta'):
                with self.subTest(turn=turn, seat=seat):
                    first = {n: answer() for n in ('alpha', 'beta')}
                    second = {n: dict(answer(), own_findings=[], peer_responses=[])
                              for n in first}
                    del (first if turn == 1 else second)[seat]
                    self.assertEqual(review.joint_plan_verdict(
                        ['alpha', 'beta'], first, second, [CLAIM]),
                        ('REVIEW_UNAVAILABLE', []))


if __name__ == '__main__':
    unittest.main()
