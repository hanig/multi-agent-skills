"""R7 through fresh exec children, real admission/accounting and flock probes."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/hanig-orchestrate/scripts'))
import d5_effect_child as child
from tests import test_d5_submission as t

PEER = ROOT / 'tests/fixtures/d5/r7_child.py'


class TestEffectChild(unittest.TestCase):
    def setUp(self):
        self.case = t.TestSubmission()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        self.data = dict(args=list(map(str, c.args)), directory=str(c.directory),
            request={k: v for k, v in c.request.items() if k != 'anchor'},
            scope=c.operation.scope.decode(), external=str(c.root / 'external'))

    def launch(self, mode='submitted', wrong=False, ancestor=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = self.case.directory
        if wrong:
            directory = self.case.root / 'wrong'
            directory.mkdir(mode=0o700)
        if ancestor:
            descriptor, self.death = os.pipe()
            stack.callback(os.close, self.death)
            action = 'ancestor'
        else:
            record = stack.enter_context(t.protocol.pin(directory))
            descriptor = stack.enter_context(record.drain())
            action = 'child'
        proc = subprocess.Popen([sys.executable, str(PEER), action,
            json.dumps(self.data), str(descriptor), mode], pass_fds=(descriptor,),
            close_fds=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0)
        if ancestor:
            os.close(descriptor)
        def cleanup():
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()  # EOF breaks the child's control barrier.
                proc.stdin = None
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=15)
        self.addCleanup(cleanup)
        return proc, stack

    def event(self, proc, name):
        # Unbuffered binary pipe: select cannot miss bytes buffered by readline.
        ready, _, _ = select.select([proc.stdout], [], [], 20)
        self.assertTrue(ready, 'child watchdog')
        raw = proc.stdout.readline()
        self.assertTrue(raw, 'child exited before ' + name)
        message = json.loads(raw)
        self.assertEqual(message['event'], name, message)
        return message

    def send(self, proc, word):
        proc.stdin.write((word + '\n').encode())

    def finish(self, proc):
        self.send(proc, 'exit')
        stdout, stderr = proc.communicate(timeout=20)
        self.assertEqual((proc.returncode, stdout, stderr), (0, b'', b''))

    def test_bootstrap_validates_protection(self):
        # R7-ACCEPT-WRONG-DRAIN: valid SH descriptor, wrong record's inode.
        proc, _ = self.launch(wrong=True)
        result = self.event(proc, 'returned')
        self.assertEqual(result['error'], 'D5 inherited drain mismatch')
        self.assertEqual(result['calls'], [])
        self.assertEqual(self.case.rows(), [])
        self.finish(proc)

    def test_bootstrap_closes_inheritance_window(self):
        # R7-LEAVE-INHERITABLE / R7-LATE-NONINHERITABLE: child pin hook
        # observes the original before R1 opens/duplicates anything.
        proc, stack = self.launch()
        adopted = self.event(proc, 'adopted')
        self.assertFalse(adopted['inheritable'])
        self.assertEqual(self.case.rows(), [])  # Launch is not submission.
        self.send(proc, 'request')
        self.event(proc, 'waiting')
        self.send(proc, 'finish')
        result = self.event(proc, 'returned')
        self.assertIsNone(result['error'])
        self.assertFalse(result['retained'])
        self.assertEqual(result['order'][:2], ['set_inheritable', 'pin'])
        # R7-UNLOCK-ON-ADOPTED-CLOSE: child's close must leave our SH intact.
        self.assertEqual(self.case.probe('drain'), 'blocked')
        stack.close()
        self.assertEqual(self.case.probe('drain'), 'available')
        self.finish(proc)

    def test_launched_child_rechecks_at_request(self):
        # U3-M9 / R7-TRUST-LAUNCH-CHECK: revocation outside final START.
        proc, stack = self.launch()
        self.event(proc, 'adopted')
        self.assertEqual(self.case.rows(), [])
        t.control.revoke(*self.case.args, 151, 'fixture stop')
        stack.close()
        self.send(proc, 'request')
        result = self.event(proc, 'returned')
        self.assertEqual(result['error'], 'D5 authority is invalidated')
        self.assertEqual(result['calls'], [])
        self.assertEqual(self.case.rows(), [])
        self.assertTrue(result['retained'])
        self.assertEqual(self.case.probe('drain'), 'blocked')
        self.finish(proc)
        self.assertEqual(self.case.probe('drain'), 'available')

    def test_child_finishes_after_parent_death(self):
        # U3-M5: child's fresh R6 operation owns its local R4 capability.
        proc, _ = self.launch('close-barrier', ancestor=True)
        self.event(proc, 'adopted')
        self.send(proc, 'request')
        self.event(proc, 'waiting')
        self.assertEqual(self.case.rows()[0]['state'], 'submitted')
        os.write(self.death, b'die')
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertEqual(self.case.probe('drain'), 'blocked')
        self.assertEqual(self.case.probe('start'), 'available')
        # Completion must not reauthorize after revocation or ancestor death.
        t.control.revoke(*self.case.args, 151, 'ancestor gone')
        self.send(proc, 'finish')
        self.event(proc, 'closing')
        row, = self.case.rows()
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['outcome'], t.OK)
        self.assertEqual(row['binding']['correlation']['request_id'], 'child')
        # R6 has returned and closed its holder; R7's original still protects.
        self.assertEqual(self.case.probe('drain'), 'blocked')
        self.send(proc, 'close')
        result = self.event(proc, 'returned')
        self.assertIsNone(result['error'])
        self.assertEqual(result['calls'], ['start', 'finish'])
        self.assertFalse(result['retained'])
        self.assertEqual(self.case.probe('drain'), 'available')
        self.finish(proc)

    def test_uncertainty_and_errors_retain_original_until_exit(self):
        # R7-RELEASE-UNKNOWN / R7-CLOSE-ON-ERROR: fstat of the original
        # prevents R6's separate retained holder from masking this defect.
        scenarios = [('unknown', 'unknown', None),
            ('start-error', 'unknown', 'start error'),
            ('finish-error', 'unknown', 'finish error'),
            ('accounting-error', 'submitted', t.ledger.DURABILITY),
            ('inactive-completion', 'submitted', 'D5 inactive'),
            ('close-error', 'completed', 'adopted close error')]
        for mode, state, error in scenarios:
            with self.subTest(mode=mode):
                proc, stack = self.launch(mode)
                self.event(proc, 'adopted')
                stack.close()
                before = {r['effect_id'] for r in self.case.rows()}
                self.send(proc, 'request')
                self.event(proc, 'waiting')
                self.send(proc, 'finish')
                result = self.event(proc, 'returned')
                self.assertEqual(result['error'], error)
                self.assertTrue(result['retained'])
                self.assertEqual(result['calls'], ['start', 'finish'])
                row, = [r for r in self.case.rows() if r['effect_id'] not in before]
                self.assertEqual(row['state'], state)
                self.assertEqual(self.case.probe('drain'), 'blocked')
                self.finish(proc)
                self.assertEqual(self.case.probe('drain'), 'available')

    def test_not_submitted_and_inactive(self):
        for mode, error, state, retained, calls in [
                ('not-submitted', None, 'not_submitted', False, ['start']),
                ('inactive', 'D5 is inactive', None, True, [])]:
            with self.subTest(mode=mode):
                proc, stack = self.launch(mode)
                self.event(proc, 'adopted')
                stack.close()
                before = len(self.case.rows())
                self.send(proc, 'request')
                result = self.event(proc, 'returned')
                self.assertEqual(result['error'], error)
                self.assertEqual(result['retained'], retained)
                self.assertEqual(result['calls'], calls)
                rows = self.case.rows()
                self.assertEqual(len(rows), before + (state is not None))
                if state:
                    self.assertEqual(rows[-1]['state'], state)
                self.assertEqual(self.case.probe('drain'), 'blocked' if retained else 'available')
                self.finish(proc)

    def test_import_refusal_translation_does_not_catch_body_errors(self):
        # R7-WIDE-TRANSLATION: matching text from work must remain untouched.
        c = self.case
        with t.protocol.pin(c.directory) as record, record.drain() as fd:
            original = os.dup(fd)
            self.addCleanup(os.close, original)
            error = t.protocol.LockError(t.protocol.locks._REFERENCE)
            with mock.patch.object(child.submission, 'submit', side_effect=error):
                with self.assertRaises(t.protocol.LockError) as caught:
                    child.bootstrap(original, c.directory, *c.args, **c.request,
                                    operation=c.operation, boundary=c.boundary)
            self.assertIs(caught.exception, error)
            self.assertFalse(os.get_inheritable(original))


if __name__ == '__main__':
    unittest.main()
