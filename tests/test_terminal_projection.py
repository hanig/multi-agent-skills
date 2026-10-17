"""Frozen issue projection through the CLIs and durable state."""
import copy
import hashlib
import json
import unittest

from tests.test_abandon_supersede import (
    StoppedDrainFixture, SO, S, setUpModule, tearDownModule)


class TestTerminalProjection(StoppedDrainFixture):
    def tick(self, n):
        self.clock = '2026-10-16T20:00:00.%d00000+00:00' % n

    def advance(self):
        self.assert_ok(self.swarm('advance', '--root', str(self.runs),
                                  '--max-new-dispatches', '0'))

    def start(self, unit):
        state = self.load()
        state['units'][unit].update(state='SUBMITTED', attempt_dir=None, attempts=[])
        self.save(state)
        self.advance()

    def settled(self, expected):
        self.assert_drained(expected)
        self.assert_drained(expected, repeat=True)
        self.map_shared()
        self.assertEqual(self.audit(plan=True, state=True), 0, self.stdout + self.stderr)
        self.assertEqual(self.checks['intent_order'], 'CLEAN')
        self.assertEqual(self.checks['swarm_state'], 'CLEAN')

    def test_terminal_decision_defeats_earlier_starts(self):
        for command, expected in (('abandon', 'canceled'), ('supersede', 'completed')):
            with self.subTest(command=command):
                self.setup_stop()
                self.fake.comments.clear()
                (self.state / SO.RECEIPTS).unlink(missing_ok=True)
                successor = copy.deepcopy(self.load()['units']['successor'])
                self.shared_issue()
                if command == 'supersede':
                    state = self.load()
                    state['units']['successor'] = successor
                    self.save(state)
                self.tick(7)
                self.start('old')
                self.assert_drained('started')
                self.tick(8)
                self.assert_ok(self.stop(command))
                self.settled(expected)

    def test_later_sibling_start_wins_without_close(self):
        self.shared_issue()
        self.tick(8)
        self.assert_ok(self.stop())
        self.settled('canceled')
        self.tick(9)
        self.start('successor')
        self.assertNotIn('close', [i['verb'] for i in self.intents()])
        self.settled('started')

    def test_cancel_never_replaces_shared_close(self):
        self.shared_issue()
        raw = json.dumps({'state': 'DONE', 'kind': 'slurm',
                          'task_id': 'old', 'attempt_id': 'attempt1'}).encode()
        (self.attempt / 'receipt.json').write_bytes(raw)
        state = self.load()
        state['units']['old'].update(state='DONE', job_id='finished',
            attempt_receipt_seals={'attempt1': hashlib.sha256(raw).hexdigest()})
        self.save(state)
        self.tick(7)
        self.advance()
        self.assert_drained('completed')
        self.tick(8)
        self.assert_ok(self.stop(unit='successor'))
        self.assertEqual([i['verb'] for i in self.intents()], ['close', 'cancel'])
        self.settled('completed')

    def test_same_second_missing_outbox_repair_follows_drained_seq7(self):
        self.shared_issue()
        plan, state = json.loads(self.plan.read_text()), self.load()
        fillers = [dict(plan['units'][0], id='filler%d' % n) for n in range(6)]
        plan['units'] = fillers + plan['units']
        for unit in fillers:
            state['units'][unit['id']] = dict(state='SUBMITTED', attempts=[],
                                            attempt_dir=None, gpu_hours=0)
        self.plan.write_text(json.dumps(plan))
        state['plan_digest'] = S.SPR.plan_digest(plan)
        self.save(state)
        self.tick(7)
        self.start('old')
        start = next(i for i in self.intents() if i['unit'] == 'old')
        self.assertEqual(start['seq'], 7)
        self.assert_drained('started')
        self.tick(8)
        self.assert_ok(self.stop())
        decision = self.load()['units']['old']['terminal_decision']
        (self.state / SO.OUTBOX).unlink()
        self.assert_ok(self.stop())
        repaired = self.intents()[0]
        self.assertEqual(repaired['at'], start['at'])
        self.assertEqual(self.load()['units']['old']['terminal_decision'], decision)
        self.fake.hide_comments = True
        self.assertNotEqual(self.drain(), 0)
        self.fake.hide_comments = False
        self.assert_drained('canceled')
        self.assert_drained('canceled', repeat=True)
        self.assertGreaterEqual(repaired['seq'], 8)


if __name__ == '__main__':
    unittest.main()
