"""R4 real-journal binding, Finish/revoke barriers, and cmd_merge's local M6."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock

from tests import test_d5_submission as t
import d5_journal_completion as journal
import swarm

ROOT = Path(__file__).resolve().parents[1]
RECORD = dict(unit='u', repo='r', pr='1', target='main', head='a'*40,
              merged_as='b'*40, method='squash', merged=True, attested=True,
              by='fixture', at='fixture-time', schema_version=1)


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.c = t.TestSubmission()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.spec = journal.prepare(self.c.state_dir, RECORD)
        self.path = swarm.merge_journal_path(self.c.state_dir)

    def submit(self):
        return self.c.submit(journal=self.spec)

    def test_binding_before_start_and_no_journal_IO_under_START(self):
        c, bound = self.c, []
        original = journal.bind
        def binding(*args):
            result = original(*args)
            bound.append(result)
            return result
        def start():
            self.assertEqual(len(bound), 1)
            self.assertEqual(bound[0].specification, self.spec)
            self.assertIs(bound[0].operation, c.operation)
            self.assertEqual(bound[0].directory, str(c.directory))
            self.assertEqual(bound[0].owner, (os.getpid(), threading.get_ident()))
            self.assertFalse(self.path.exists())
        c.boundary.on_start = start
        append = swarm.SJ._fsync_append
        def record(path, data):
            self.assertEqual(c.probe('start'), 'available')
            self.assertEqual(c.probe('drain'), 'blocked')
            return append(path, data)
        with mock.patch.object(journal, 'bind', side_effect=binding), \
                mock.patch.object(swarm.SJ, '_fsync_append', side_effect=record):
            self.assertEqual(self.submit().outcome, t.OK)
        self.assertEqual(json.loads(self.path.read_bytes()), RECORD)
        self.assertEqual(c.rows()[0]['state'], 'completed')
        self.assertEqual(c.probe('drain'), 'available')

    def test_binding_and_admission_fail_before_effect(self):
        c = self.c
        self.spec = replace(self.spec, path=str(c.root / 'other'))
        with self.assertRaisesRegex(t.ledger.EffectError, 'target mismatch'):
            self.submit()
        self.assertEqual(c.boundary.calls, [])
        self.assertEqual(c.rows()[0]['state'], 'not_submitted')
        self.assertEqual(c.probe('drain'), 'available')
        self.spec = journal.prepare(c.state_dir, RECORD)
        t.control.revoke(*c.args, 151, 'fixture revoke')
        with self.assertRaises(t.authority.AuthorityError):
            self.submit()
        self.assertEqual(c.boundary.calls, [])
        self.assertFalse(self.path.exists())

    def test_independent_revoke_returns_before_real_recorder(self):
        c = self.c
        # Hold the actual journal flock. Finish must wait here without START.
        self.path.touch()
        fh = self.path.open('r+b')
        self.addCleanup(fh.close)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        reached = threading.Event()
        original = swarm.SJ._fsync_append
        def append(path, data):
            reached.set()
            return original(path, data)
        with mock.patch.object(swarm.SJ, '_fsync_append', side_effect=append):
            thread, results = c.launch(self.submit)
            try:
                self.assertTrue(reached.wait(10), 'recorder watchdog')
                self.assertEqual(c.boundary.calls[-1], ('finish',))
                self.assertEqual(c.probe('start'), 'available')
                self.assertEqual(c.probe('drain'), 'blocked')
                self.assertEqual(self.path.read_bytes(), b'')
                # Independent control returns while the recorder is still blocked.
                revoker, revoked = c.launch(lambda: t.control.revoke(*c.args, 151, 'independent'))
                self.assertNotIsInstance(c.join(revoker, revoked), BaseException)
                self.assertTrue(t.registry.d5_active())
                self.assertTrue(thread.is_alive())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                result = c.join(thread, results)
        self.assertIsInstance(result, t.submission.Result)
        self.assertEqual(result.outcome, t.OK)
        self.assertEqual(json.loads(self.path.read_bytes()), RECORD)
        self.assertEqual(c.rows()[0]['state'], 'completed')
        c.boundary = t.DeterministicBoundary()
        with self.assertRaises(t.authority.AuthorityError):
            self.submit()
        self.assertEqual(c.boundary.calls, [])

    def test_completion_does_not_revoke(self):
        self.submit()
        current = t.store.read(self.c.directory)
        self.assertIsNone(current['invalidation'])
        self.c.operation = replace(self.c.operation, request_id='B')
        self.assertEqual(self.submit().outcome, t.OK)
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_recorder_failure_is_uncertain_and_retains_lifetime(self):
        c = self.c
        original, saved = swarm.SJ._fsync_append, []
        bind = t.completion.bind
        def binding(*args, **kwargs):
            result = bind(*args, **kwargs)
            if kwargs:
                saved.append(result)
            return result
        for after in (False, True):
            with self.subTest(after_durable_append=after):
                def fail(path, data):
                    if after:
                        original(path, data)
                    raise OSError('recorder acknowledgement lost')
                c.operation = replace(c.operation, request_id=str(after))
                with mock.patch.object(swarm.SJ, '_fsync_append', side_effect=fail), \
                        mock.patch.object(t.completion, 'bind', side_effect=binding):
                    with self.assertRaisesRegex(OSError, 'acknowledgement lost'):
                        self.submit()
                self.assertTrue(all(r['state'] == 'unknown' for r in c.rows()))
                self.assertEqual(c.probe('drain'), 'blocked')
                before = self.path.read_bytes() if self.path.exists() else None
                with self.assertRaises(t.ledger.EffectError):
                    saved[-1].complete(t.OK)
                self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
                unresolved = t.ledger.unresolved(*c.args)
                self.assertTrue(all(r['reconciliation_required'] for r in unresolved))
        self.assertEqual(json.loads(self.path.read_bytes()), RECORD)

    def test_ledger_success_alone_cannot_complete_bound_obligation(self):
        c, caps = self.c, []
        bind = t.completion.bind
        def binding(*args, **kwargs):
            cap = bind(*args, **kwargs)
            if kwargs:
                t.ledger.complete(args[0], t.OK)
                caps.append(cap)
            return cap
        with mock.patch.object(t.completion, 'bind', side_effect=binding), \
                mock.patch.object(swarm.SJ, '_fsync_append', side_effect=OSError('no append')):
            with self.assertRaises((OSError, t.ledger.EffectError)):
                self.submit()
        self.assertEqual(c.rows()[0]['state'], 'completed')
        self.assertFalse(self.path.exists())
        self.assertEqual(c.probe('drain'), 'blocked')
        with self.assertRaises(t.ledger.EffectError):
            caps[0].complete(t.OK)
        self.assertFalse(self.path.exists())

    def test_immutable_spec_wrong_receipt_owner_and_START_refused(self):
        c = self.c
        with self.assertRaises(FrozenInstanceError):
            self.spec.path = 'elsewhere'
        bound = journal.bind(self.spec, c.operation, c.state_dir, str(c.directory))
        self.submit()  # Obtain a separate completed receipt for mismatch checks.
        row = c.rows()[0]
        # An unrelated ledger receipt may not be attached to this invocation.
        request = dict(record_id=c.request['grant_id'], custody=row['binding']['custody'],
            kind='fixture', scope=c.scope, correlation={'request_id': 'other', 'intent': '00'})
        attempt = t.ledger.prepare(*c.args, **request)
        receipt = t.ledger._classify(attempt, 'submitted')
        with self.assertRaisesRegex(t.ledger.EffectError, 'invocation mismatch'):
            t.completion.bind(receipt, journal=bound)
        other = replace(c.operation, request_id='other', intent=b'\0')
        bound = journal.bind(self.spec, other, c.state_dir, str(c.directory))
        cap = t.completion.bind(receipt, journal=bound)
        thread, results = c.launch(lambda: cap.complete(t.OK))
        self.assertIsInstance(c.join(thread, results), t.ledger.EffectError)
        before = self.path.read_bytes()
        with t.protocol.pin(c.directory) as record, record.drain(), record.start():
            with self.assertRaisesRegex(t.ledger.EffectError, 'inside START'):
                cap.complete(t.OK)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_effect_does_not_attest_merge(self):
        self.c.boundary.on_finish = lambda: dict(status='failed', detail='no merge')
        self.assertEqual(self.submit().outcome['status'], 'failed')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.c.rows()[0]['state'], 'completed')


class TestM6(unittest.TestCase):
    def capture(self, version, case):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/fixtures/d5/r4_m6.py'),
                                 version, case], capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_inactive_cmd_merge_matches_pinned_effects(self):
        for case in ('success', 'corrupt', 'invalid'):
            with self.subTest(case=case):
                before, after = self.capture('base', case), self.capture('current', case)
                self.assertEqual(after, before)
                if case == 'success':
                    self.assertEqual(after['result'], 0)
                    self.assertEqual(set(after['changed']), {'state/merge-receipts.jsonl'})
                    self.assertTrue(after['events'])

    def test_premature_activation_has_zero_production_effects(self):
        capture = self.capture('current', 'active')
        self.assertEqual(capture['result'], swarm.EXIT_CONFLICT)
        self.assertEqual(capture['events'], [])
        self.assertEqual(capture['changed'], {})
        self.assertEqual(capture['removed'], [])


if __name__ == '__main__':
    unittest.main()
