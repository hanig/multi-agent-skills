"""State-directory sequence ordering through emission, drain and audit."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_linear_drain import DrainCase, DC, LS
import swarm_outbox as SO


AT = '2026-10-16T10:00:00+0000'


class TestSequenceDrain(DrainCase):
    def emit_pair(self, directory, keys):
        directory.mkdir(exist_ok=True)
        with mock.patch.object(SO, '_intent_key', side_effect=keys), \
                mock.patch.object(SO.time, 'strftime', return_value=AT):
            for state, evidence in [('SUBMITTED', None),
                                    ('DONE', {'receipt': {'state': 'DONE'}})]:
                SO.emit_intent(directory, 'project', 'u', state,
                               {'attempt_dir': '/runs/u/attempt1'},
                               evidence=evidence, kind='slurm', tracker='ARC-1')
        return SO.read_outbox(directory)

    def test_same_second_drain_and_latest_state_follow_emission_for_both_key_orders(self):
        for keys in [('a', 'z'), ('z', 'a')]:
            with self.subTest(keys=keys):
                self.fake.comments.clear()
                self.fake.mutations.clear()
                self.fake.issues['1']['state']['type'] = 'unstarted'
                directory = self.root / ('order-' + keys[0])
                intents = self.emit_pair(directory, keys)
                self.assertEqual([i['seq'] for i in intents], [1, 2])
                self.assertEqual([i['at'] for i in intents], [AT, AT])
                self.assertEqual(self.drain(directory), 0, self.stdout + self.stderr)
                sent = [LS.comment_markers({'body': v['input']['body']})['intent']
                        for v in self.writes('IntentCommentCreate')]
                self.assertEqual(sent, list(keys))
                parsed = [LS.genuine_comment(c, 'workspace', 'project', '1')
                          for c in self.fake.comments.values()]
                self.assertEqual(LS.latest_state(parsed)['key'], keys[-1])
                self.assertEqual(self.fake.issues['1']['state']['type'], 'completed')
                self.assertEqual([r['key'] for r in self.receipts(directory)], [keys[-1]])
                self.assertEqual(self.audit(), 0, self.stdout + self.stderr)
                self.assertEqual(self.checks['intent_order'], 'CLEAN')
                self.fake.issues['1']['state']['type'] = 'started'
                self.assertEqual(self.audit(), 1)
                self.assertEqual(self.checks['intent_order'], 'DRIFT')
                self.assertEqual(self.drain(directory), 0, self.stdout + self.stderr)
                self.assertEqual(len(self.writes('IntentCommentCreate')), 2)
                self.assertEqual(self.fake.issues['1']['state']['type'], 'started')
                self.assertIn('0 acknowledged, 0 superseded, 0 failures, 1 issues skipped',
                              self.stdout)
                self.assertEqual(self.drain(directory, full=True), 0,
                                 self.stdout + self.stderr)
                self.assertEqual(len(self.writes('IntentCommentCreate')), 2)
                self.assertEqual(self.fake.issues['1']['state']['type'], 'completed')

    def test_legacy_first_at_same_instant(self):
        legacy = self.intent('z', 'start', at=AT)
        sequenced = self.intent('a', 'close', at=AT, seq=1)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        sent = [LS.comment_markers({'body': v['input']['body']})['intent']
                for v in self.writes('IntentCommentCreate')]
        self.assertEqual(sent, [legacy['key'], sequenced['key']])
        self.assertEqual(self.fake.issues['1']['state']['type'], 'completed')

    def test_read_failure_defers_emission_until_sequence_can_be_read(self):
        self.intent('z', 'start', at=AT, seq=5)
        path = self.state / SO.OUTBOX
        before = path.read_bytes()
        errors = io.StringIO()
        open_file = SO.os.open
        def unreadable(name, *args, **kwargs):
            if Path(name) == self.state / SO.OUTBOX_SEQ:
                raise OSError('transient read failure')
            return open_file(name, *args, **kwargs)
        with mock.patch.object(SO, '_intent_key', return_value='a'), \
                mock.patch.object(SO.time, 'strftime', return_value=AT):
            with mock.patch.object(SO.os, 'open', side_effect=unreadable), \
                    contextlib.redirect_stderr(errors):
                key = SO.emit_intent(self.state, 'project', 'u', 'DONE', {},
                                     evidence={'receipt': {'state': 'DONE'}}, tracker='ARC-1')
            self.assertIsNone(key)
            self.assertEqual(path.read_bytes(), before)
            self.assertIn('transient read failure', errors.getvalue())
            self.assertEqual(SO.emit_intent(
                self.state, 'project', 'u', 'DONE', {},
                evidence={'receipt': {'state': 'DONE'}}, tracker='ARC-1'), 'a')
        self.assertEqual(SO.read_outbox(self.state)[-1]['seq'], 6)
        self.assertEqual(self.drain(), 0, self.stdout + self.stderr)
        sent = [LS.comment_markers({'body': v['input']['body']})['intent']
                for v in self.writes('IntentCommentCreate')]
        self.assertEqual(sent, ['z', 'a'])
        self.assertEqual(self.fake.issues['1']['state']['type'], 'completed')

    def test_legacy_and_sequenced_trailers_round_trip(self):
        for at in (AT, '2026-10-16 10:00:00+00:00'):
            for seq in (None, 7):
                with self.subTest(at=at, seq=seq):
                    intent = self.intent(at=at, **({'seq': seq} if seq else {}))
                    expected = '%s key start' % at + (' 7' if seq else '')
                    self.assertEqual(LS.intent_markers(intent)['order'], expected)
                    comment = self.seed(intent)
                    parsed = LS.genuine_comment(comment, 'workspace', 'project', '1')
                    self.assertIsNotNone(parsed)
                    self.assertEqual(parsed['order'][1:],
                                     (1, 7, 'key') if seq else (0, 0, 'key'))
                    self.assertEqual(len(LS.comment_markers(comment)), 3)

    def test_invalid_sequence_trailers_rejected(self):
        intent = self.intent(at=AT, seq=7)
        original = self.seed(intent)
        for suffix in ('0', '07', '-1', 'x', '7 extra', '7 8', '', ' 7', '7 ',
                       '1.0', '+7', '\u0667'):
            with self.subTest(suffix=suffix):
                comment = dict(original, body=original['body'].replace(
                    'key start 7`', 'key start %s`' % suffix))
                self.assertIsNone(LS.genuine_comment(comment, 'workspace', 'project', '1'))

    def test_invalid_intent_sequences_refused_by_drain(self):
        intent = self.intent(at=AT)
        for seq in (None, True, False, 0, -1, '7', 1.5, [], {}):
            with self.subTest(seq=seq):
                bad = dict(intent, seq=seq)
                (self.state / SO.OUTBOX).write_text(json.dumps(bad) + '\n')
                self.assertTrue(any('seq' in p for p in DC.validate_intent(bad)))
                self.assertEqual(self.drain(), 3, self.stdout + self.stderr)
                self.assertEqual(self.fake.mutations, [])

    def test_instant_precedes_sequence_and_legacy_key_order_is_unchanged(self):
        rows = [(AT, 'z', None), (AT, 'a', None),
                ('2026-10-16T09:59:59+0000', 'earlier', 999),
                ('2026-10-16T11:00:00+0100', 'sequenced', 1)]
        self.assertEqual([r[1] for r in sorted(rows, key=lambda r: LS.intent_order(*r))],
                         ['earlier', 'a', 'z', 'sequenced'])
        self.assertLess(LS.intent_order(AT, 'a', 7), LS.intent_order(AT, 'z', 7))


class TestSequenceEmission(unittest.TestCase):
    def test_partial_tail_recovers_after_the_completed_sequence_maximum(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / SO.OUTBOX
            prefix = json.dumps({'key': 'old', 'seq': 4}).encode() + b'\n'
            path.write_bytes(prefix + b'{"key": "interrupted", "seq":')
            self.assertIsNotNone(SO.emit_intent(tmp, 'project', 'u', 'SUBMITTED', {}))
            after = path.read_bytes()
            self.assertTrue(after.startswith(prefix))
            self.assertEqual(json.loads(after[len(prefix):])['seq'], 5)

    def test_sequence_uses_maximum_and_preserves_legacy_bytes(self):
        for history, expected in [([], 1), ([None, None], 1), ([None, 4], 5),
                                  ([9, 4, None], 10)]:
            with self.subTest(history=history), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / SO.OUTBOX
                lines = []
                for n, seq in enumerate(history):
                    row = {'key': 'old-%d' % n, 'project': 'project', 'unit': 'u',
                           'verb': 'start', 'at': AT, 'evidence': None}
                    if seq is not None:
                        row['seq'] = seq
                    lines.append(json.dumps(row) + '\n')
                before = ''.join(lines).encode()
                path.write_bytes(before)
                key = SO.emit_intent(tmp, 'project', 'new-unit', 'SUBMITTED', {})
                after = path.read_bytes()
                self.assertTrue(after.startswith(before))
                new = json.loads(after[len(before):])
                self.assertEqual(new['key'], key)
                self.assertEqual(new['seq'], expected)
                self.assertEqual(SO.validate_intent(new), [])
                self.assertIsNone(SO.emit_intent(tmp, 'project', 'new-unit', 'SUBMITTED', {}))
                self.assertEqual(path.read_bytes(), after)

    def test_sequence_changes_neither_key_nor_evidence_digest(self):
        envelopes = []
        keys = []
        for initial in (None, 4):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / SO.OUTBOX
                if initial is not None:
                    path.write_text(json.dumps({'key': 'old', 'seq': initial}) + '\n')
                evidence = {'receipt': {'state': 'DONE'}}
                key = SO.emit_intent(tmp, 'project', 'u', 'DONE',
                                     {'attempt_dir': '/runs/u/a'}, evidence=evidence)
                intent = json.loads(path.read_text().splitlines()[-1])
                self.assertEqual(intent['seq'], 1 if initial is None else 5)
                keys.append(key)
                envelopes.append(intent['envelope'])
                legacy = dict(intent)
                del legacy['seq']
                del legacy['envelope']
                self.assertEqual(DC.normalize_intent(legacy)['envelope'], intent['envelope'])
                self.assertNotIn('seq', DC.normalize_intent(legacy))
                self.assertEqual(SO.validate_intent(legacy), [])
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(envelopes[0], envelopes[1])
        self.assertEqual(keys[0], SO.outbox_key('project', 'u', 'DONE', '/runs/u/a'))
        # A pre-change key still deduplicates without rewriting its record.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / SO.OUTBOX
            path.write_text(json.dumps(legacy) + '\n')
            before = path.read_bytes()
            self.assertIsNone(SO.emit_intent(tmp, 'project', 'u', 'DONE',
                                            {'attempt_dir': '/runs/u/a'}, evidence=evidence))
            self.assertEqual(path.read_bytes(), before)


from tests.d5_test_support import inactive_classes
inactive_classes(
    TestSequenceDrain, TestSequenceEmission)


if __name__ == '__main__':
    unittest.main()
