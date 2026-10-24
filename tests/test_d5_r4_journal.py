"""R4 real-journal binding, Finish/revoke barriers, and cmd_merge's local M6."""
from dataclasses import FrozenInstanceError, replace
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from tests import test_d5_submission as t
import d5_journal_completion as journal
import swarm

ROOT = Path(__file__).resolve().parents[1]
RECORD = dict(unit='u', repo='r', pr='1', target='main', head='a'*40,
              merged_as='b'*40, method='squash', merged=True, attested=True,
              by='fixture', at='fixture-time', schema_version=1)


class TestJournal(unittest.TestCase):
    eq = unittest.TestCase.assertEqual
    raises = unittest.TestCase.assertRaisesRegex
    yes = unittest.TestCase.assertTrue
    no = unittest.TestCase.assertFalse
    def setUp(self):
        self.c = t.TestSubmission()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.spec = journal.prepare(self.c.state_dir, RECORD)
        self.path = swarm.merge_journal_path(self.c.state_dir)

    def submit(self):
        return self.c.submit(journal=self.spec)

    def test_pre_start_binding(self):
        c, bound = self.c, []
        original = journal.bind
        def binding(*args):
            result = original(*args)
            bound.append(result)
            return result
        def start():
            self.eq(len(bound), 1)
            self.assertIs(bound[0].operation, c.operation)
            self.eq((bound[0].specification, bound[0].directory, bound[0].owner, self.path.exists()),
                    (self.spec, str(c.directory), (os.getpid(), threading.get_ident()), False))
        c.boundary.on_start = start
        append = swarm.SJ._fsync_append
        def record(path, data):
            self.eq(c.probe('start'), 'available')
            self.eq(c.probe('drain'), 'blocked')
            return append(path, data)
        with patch.object(journal, 'bind', side_effect=binding), \
                patch.object(swarm.SJ, '_fsync_append', side_effect=record):
            self.eq(self.submit().outcome, t.OK)
        self.eq(json.loads(self.path.read_bytes()), RECORD)
        self.eq(c.rows()[0]['state'], 'completed')
        self.eq(c.probe('drain'), 'available')

    def test_pre_start_refusal(self):
        c = self.c
        self.spec = replace(self.spec, path=str(c.root / 'other'))
        with self.raises(t.ledger.EffectError, 'target mismatch'):
            self.submit()
        self.eq(c.boundary.calls, [])
        self.eq(c.rows()[0]['state'], 'not_submitted')
        self.eq(c.probe('drain'), 'available')
        self.spec = journal.prepare(c.state_dir, RECORD)
        t.control.revoke(*c.args, 151, 'fixture revoke')
        with self.assertRaises(t.authority.AuthorityError):
            self.submit()
        self.eq(c.boundary.calls, [])
        self.no(self.path.exists())

    def test_independent_revoke(self):
        c = self.c
        self.path.touch()
        fh = self.path.open('r+b')
        self.addCleanup(fh.close)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        ready, resume, reached = [threading.Event() for _ in range(3)]
        record = journal.BoundJournal.record
        def paused(bound, outcome):
            ready.set()
            if not resume.wait(10):
                raise AssertionError('recorder watchdog')
            return record(bound, outcome)
        original = swarm.SJ._fsync_append
        def append(path, data):
            reached.set()
            return original(path, data)
        with patch.object(swarm.SJ, '_fsync_append', side_effect=append), \
                patch.object(journal.BoundJournal, 'record', paused):
            thread, results = c.launch(self.submit)
            try:
                self.yes(ready.wait(10), 'recorder watchdog')
                self.eq(c.boundary.calls[-1], ('finish',))
                self.eq(c.probe('start'), 'available')
                self.eq(c.probe('drain'), 'blocked')
                self.eq(self.path.read_bytes(), b'')
                revoker, revoked = c.launch(lambda: t.control.revoke(*c.args, 151, 'independent'))
                self.assertNotIsInstance(c.join(revoker, revoked), BaseException)
                self.yes(t.registry.d5_active())
                self.yes(thread.is_alive())
                resume.set()
                self.yes(reached.wait(10), 'journal watchdog')
                self.eq(self.path.read_bytes(), b'')
            finally:
                resume.set()
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                result = c.join(thread, results)
        self.assertIsInstance(result, t.submission.Result)
        self.eq(result.outcome, t.OK)
        self.eq(json.loads(self.path.read_bytes()), RECORD)
        self.eq(c.rows()[0]['state'], 'completed')
        c.boundary = t.DeterministicBoundary()
        with self.assertRaises(t.authority.AuthorityError):
            self.submit()
        self.eq(c.boundary.calls, [])

    def test_completion_does_not_revoke(self):
        self.submit()
        current = t.store.read(self.c.directory)
        self.assertIsNone(current['invalidation'])
        self.c.operation = replace(self.c.operation, request_id='B')
        self.eq(self.submit().outcome, t.OK)
        self.eq(len(self.path.read_text().splitlines()), 2)

    def test_recorder_failure(self):
        for after, ledger_first in ((False, False), (True, False), (False, True)):
            with self.subTest(after=after, ledger_first=ledger_first):
                c = t.TestSubmission()
                c.setUp()
                self.addCleanup(c.doCleanups)
                path = swarm.merge_journal_path(c.state_dir)
                original, saved, bind = swarm.SJ._fsync_append, [], t.completion.bind
                def binding(*args, **kwargs):
                    cap = bind(*args, **kwargs)
                    if kwargs.get('journal') is not None:
                        saved.append(cap)
                        if ledger_first:
                            t.ledger.complete(args[0], t.OK)
                    return cap
                def fail(path, data):
                    if after:
                        original(path, data)
                    raise OSError('recorder acknowledgement lost')
                with patch.object(swarm.SJ, '_fsync_append', side_effect=fail), \
                        patch.object(t.completion, 'bind', side_effect=binding):
                    with self.raises(t.ledger.EffectError if ledger_first else OSError,
                                     'conflict' if ledger_first else 'acknowledgement lost'):
                        c.submit(journal=journal.prepare(c.state_dir, RECORD))
                self.eq(c.rows()[0]['state'], 'completed' if ledger_first else 'unknown')
                self.eq(c.probe('drain'), 'blocked')
                before = path.read_bytes() if path.exists() else None
                self.eq(json.loads(before) if before else None, RECORD if after else None)
                with self.assertRaises(t.ledger.EffectError):
                    saved[0].complete(t.OK)
                self.eq(path.read_bytes() if path.exists() else None, before)
                unresolved = t.ledger.unresolved(*c.args)
                self.eq(len(unresolved), 0 if ledger_first else 1)
                self.yes(all(r['reconciliation_required'] for r in unresolved))

    def test_binding_refusals(self):
        c = self.c
        with self.assertRaises(FrozenInstanceError):
            self.spec.path = 'elsewhere'
        bound = journal.bind(self.spec, c.operation, c.state_dir, str(c.directory))
        self.submit()  # Obtain a separate completed receipt for mismatch checks.
        row = c.rows()[0]
        request = dict(record_id=c.request['grant_id'], custody=row['binding']['custody'],
            kind='fixture', scope=c.scope, correlation={'request_id': 'other', 'intent': '00'})
        attempt = t.ledger.prepare(*c.args, **request)
        receipt = t.ledger._classify(attempt, 'submitted')
        with self.raises(t.ledger.EffectError, 'invocation mismatch'):
            t.completion.bind(receipt, journal=bound)
        other = replace(c.operation, request_id='other', intent=b'\0')
        bound = journal.bind(self.spec, other, c.state_dir, str(c.directory))
        cap = t.completion.bind(receipt, journal=bound)
        thread, results = c.launch(lambda: cap.complete(t.OK))
        self.assertIsInstance(c.join(thread, results), t.ledger.EffectError)
        before = self.path.read_bytes()
        with t.protocol.pin(c.directory) as record, record.drain(), record.start():
            with self.raises(t.ledger.EffectError, 'inside START'):
                cap.complete(t.OK)
        self.eq(self.path.read_bytes(), before)

    def test_failed_effect(self):
        self.c.boundary.on_finish = lambda: dict(status='failed', detail='no merge')
        self.eq(self.submit().outcome['status'], 'failed')
        self.no(self.path.exists())
        self.eq(self.c.rows()[0]['state'], 'completed')


class TestM6(unittest.TestCase):
    eq = unittest.TestCase.assertEqual
    yes = unittest.TestCase.assertTrue
    def capture(self, version, case):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/fixtures/d5/r4_m6.py'),
                                 version, case], capture_output=True, text=True, timeout=40)
        self.eq(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_inactive_equivalence(self):
        for case in ('success', 'corrupt', 'invalid'):
            with self.subTest(case=case):
                before, after = self.capture('base', case), self.capture('current', case)
                self.eq(after, before)
                if case == 'success':
                    self.eq(after['result'], 0)
                    self.eq(set(after['changed']), {'state/merge-receipts.jsonl'})
                    self.yes(after['events'])

    def test_premature_activation(self):
        capture = self.capture('current', 'active')
        self.eq(capture, dict(result=swarm.EXIT_CONFLICT, events=[], changed={}, removed=[]))


if __name__ == '__main__':
    unittest.main()
