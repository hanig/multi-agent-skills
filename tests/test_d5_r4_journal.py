"""R4 journal and cmd_merge M6."""
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
  merged_as='b'*40, method='squash', merged=True, attested=True, by='fixture', at='fixture-time', schema_version=1)

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

  def assert_record(self, path=None):
    row = json.loads((path or self.path).read_bytes())
    key = row.pop('d5_effect_key')
    self.eq(row.pop('d5_outcome'), t.OK)
    self.eq(row, RECORD)
    self.eq(len(key.split(':')), 2)

  def capture_receipt(self):
    saved, bind = [], t.completion.bind
    def capture(handle, **kw):
      cap = bind(handle, **kw)
      if kw.get('journal') is not None:
        saved.append(handle)
      return cap
    with patch.object(t.completion, 'bind', side_effect=capture):
      self.submit()
    return saved[0]

  def rebound(self, handle):
    return t.completion.bind(handle, journal=journal.bind(self.spec, self.c.operation, self.c.args, str(self.c.directory)))

  def test_rebind_and_child_discover_completed_record(self):
    receipt = self.capture_receipt()
    before = self.path.read_bytes()
    for cap in (self.rebound(receipt), self.rebound(receipt)):
      self.eq(cap.complete(dict(t.OK)), t.OK)
      self.eq(self.path.read_bytes(), before)
    c = self.c
    message = dict(receipt=dict(effect_id=receipt.effect_id, directory=receipt._directory,
      binding=receipt._binding.hex(), secret=receipt._secret.hex()),
      args=[str(a) for a in c.args], operation=dict(request_id=c.operation.request_id,
      kind=c.operation.kind, scope=c.operation.scope.hex(), intent=c.operation.intent.hex()),
      record=RECORD, outcome=t.OK)
    child = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/fixtures/d5/r4_child.py')],
      input=json.dumps(message), capture_output=True, text=True, timeout=30)
    self.eq(child.returncode, 0, child.stderr)
    self.eq(json.loads(child.stdout), t.OK)
    self.eq(self.path.read_bytes(), before)
    self.eq(t.ledger.unresolved(*c.args), [])

  def test_independent_objects_concurrent(self):
    c, caps = self.c, []
    bind = t.completion.bind
    entered, release = threading.Event(), threading.Event()
    append = journal._append_locked
    def blocked(fh, row):
      entered.set()
      if not release.wait(10):
        raise AssertionError('append watchdog')
      return append(fh, row)
    # Construct the independent object before replacing completion.bind.
    def capture(handle, **kw):
      cap = bind(handle, **kw)
      if kw.get('journal') is not None:
        caps.append(bind(handle, journal=journal.bind(self.spec, c.operation, c.args, str(c.directory))))
      return cap
    with patch.object(t.completion, 'bind', side_effect=capture), patch.object(journal, '_append_locked', side_effect=blocked):
      first, results = c.launch(self.submit)
      try:
        self.yes(entered.wait(10))
        second, duplicates = c.launch(lambda: caps[0].complete(t.OK))
      finally:
        release.set()
        result = c.join(first, results)
      self.assertIsInstance(result, t.submission.Result)
      self.eq(c.join(second, duplicates), t.OK)
    self.assert_record()
    self.eq(c.rows()[0]['state'], 'completed')
    self.eq(c.probe('drain'), 'available')

  def test_finish_error_keeps_precompleted_obligation(self):
    c, bind = self.c, t.completion.bind
    def precomplete(handle, **kw):
      cap = bind(handle, **kw)
      if type(handle) is t.ledger.Receipt:
        t.ledger.complete(handle, t.OK)
      return cap
    def fail():
      raise OSError('finish acknowledgement lost')
    c.boundary.on_finish = fail
    with patch.object(t.completion, 'bind', side_effect=precomplete):
      with self.raises(OSError, 'finish acknowledgement lost'):
        self.submit()
    self.eq([r['state'] for r in c.rows()], ['completed'])
    pending, = t.ledger.unresolved(*c.args)
    self.yes(pending['reconciliation_required'])
    self.eq(pending['journal']['path'], str(self.path))
    self.eq(pending['journal']['state'], 'uncertain')
    self.eq(self.path.read_bytes(), b'')
    self.eq(c.probe('drain'), 'blocked')

  def test_pre_start_binding(self):
    c, bound, caps = self.c, [], []
    real = journal.bind
    def binding(*args):
      result = real(*args)
      bound.append(result)
      return result
    def start():
      self.eq(len(bound), 1)
      self.assertIs(bound[0].operation, c.operation)
      row, = c.rows()
      self.eq(row['journal'], dict(path=self.spec.path, payload=self.spec.payload.decode(), state='pending'))
      self.yes(t.ledger.unresolved(*c.args)[0]['reconciliation_required'])
      self.eq((bound[0].spec, bound[0].directory, bound[0].owner, self.path.exists()), (self.spec, str(c.directory), os.getpid(), False))
    c.boundary.on_start = start
    bind_cap = t.completion.bind
    def capture(*args, **kwargs):
      cap = bind_cap(*args, **kwargs)
      if kwargs.get('journal') is not None:
        caps.append(cap)
      return cap
    append = journal._append_locked
    ready, resume = threading.Event(), threading.Event()
    def record(path, data):
      self.eq(c.probe('start'), 'available')
      self.eq(c.probe('drain'), 'blocked')
      ready.set()
      if not resume.wait(15):
        raise AssertionError('recorder timeout')
      return append(path, data)
    with patch.object(journal, 'bind', side_effect=binding), \
        patch.object(journal, '_append_locked', side_effect=record), \
        patch.object(t.completion, 'bind', side_effect=capture):
      first, results = c.launch(self.submit)
      try:
        self.yes(ready.wait(10), 'recorder timeout')
        with self.assertRaises(t.ledger.EffectError):
          t.ledger.complete(caps[0].handle, t.OK)
        second, duplicates = c.launch(lambda: caps[0].complete(dict(t.OK)))
        second.join(3)
      finally:
        resume.set()
        result = c.join(first, results)
      self.assertIsInstance(result, t.submission.Result)
      self.eq(result.outcome, t.OK)
      self.eq(c.join(second, duplicates), t.OK)
    self.assert_record()
    self.eq(c.rows()[0]['state'], 'completed')
    self.eq(c.probe('drain'), 'available')

  def test_pre_start_refusal(self):
    c, bind = self.c, journal.bind
    def expired(*args):
      bound = bind(*args)
      c.clock.return_value = 200
      return bound
    with patch.object(journal, 'bind', side_effect=expired):
      with self.raises(t.authority.AuthorityError, 'expired'):
        self.submit()
    c.clock.return_value = 150
    spec = self.spec
    self.spec = replace(spec, payload=bytearray(spec.payload))
    with self.raises(t.ledger.EffectError, 'target mismatch'):
      self.submit()
    self.spec = replace(spec, path=str(c.root / 'other'))
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
        raise AssertionError('recorder timeout')
      return record(bound, outcome)
    real = journal.BoundJournal._locked
    from contextlib import contextmanager
    @contextmanager
    def locked(bound):
      reached.set()
      with real(bound) as fh:
        yield fh
    with patch.object(journal.BoundJournal, '_locked', locked), \
        patch.object(journal.BoundJournal, 'record', paused):
      thread, results = c.launch(self.submit)
      try:
        self.yes(ready.wait(10), 'recorder timeout')
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
    self.assert_record()
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
    for after, prior in ((False, False), (True, False), (False, True), (True, True)):
      with self.subTest(after=after, prior=prior):
        c = t.TestSubmission()
        c.setUp()
        self.addCleanup(c.doCleanups)
        path = swarm.merge_journal_path(c.state_dir)
        real, saved, bind = journal._append_locked, [], t.completion.bind
        def binding(*args, **kwargs):
          cap = bind(*args, **kwargs)
          if kwargs.get('journal') is not None:
            saved.append(cap)
            if prior:
              t.ledger.complete(args[0], t.OK)
          return cap
        def fail(path, data):
          if after:
            real(path, data)
          if prior:
            t.control.revoke(*c.args, 151, 'recorder failure')
          raise OSError('recorder acknowledgement lost')
        with patch.object(journal, '_append_locked', side_effect=fail), \
            patch.object(t.completion, 'bind', side_effect=binding):
          with self.raises(OSError, 'acknowledgement lost'):
            c.submit(journal=journal.prepare(c.state_dir, RECORD))
        self.eq(sorted(r['state'] for r in c.rows()), ['completed'] if prior else ['unknown'])
        self.eq(c.probe('drain'), 'blocked')
        before = path.read_bytes() if path.exists() else None
        if after:
          self.assert_record(path)
        else:
          self.no(before)
        with self.assertRaises(t.ledger.EffectError):
          saved[0].complete(t.OK)
        self.eq(path.read_bytes() if path.exists() else None, before)
        unresolved = t.ledger.unresolved(*c.args)
        self.eq(len(unresolved), 1)
        self.yes(all(r['reconciliation_required'] for r in unresolved))
        self.eq(unresolved[0]['effect_id'], saved[0].handle.effect_id)
        self.eq(unresolved[0]['journal']['state'], 'uncertain')
        # A newly bound admitted owner consults durable uncertainty, too.
        new = t.completion.bind(saved[0].handle, journal=journal.bind(saved[0].spec, c.operation, c.args, str(c.directory)))
        with self.assertRaises(t.ledger.EffectError):
          new.complete(t.OK)
        if after:
          self.eq(new.reconcile(), t.OK)
          self.eq(t.ledger.unresolved(*c.args), [])
        else:
          with self.raises(t.ledger.EffectError, 'absent'):
            new.reconcile()
        self.eq(path.read_bytes(), before)
        self.eq(c.probe('drain'), 'blocked')

  def test_binding_refusals(self):
    c = self.c
    with self.assertRaises(FrozenInstanceError):
      self.spec.path = 'elsewhere'
    bound = journal.bind(self.spec, c.operation, c.args, str(c.directory))
    self.submit()
    row = c.rows()[0]
    request = dict(record_id=c.request['grant_id'], custody=row['binding']['custody'], kind='fixture', scope=c.scope, correlation={'request_id': 'other', 'intent': '00'})
    attempt = t.ledger.prepare(*c.args, **request)
    receipt = t.ledger._classify(attempt, 'submitted')
    with self.raises(t.ledger.EffectError, 'invocation mismatch'):
      t.completion.bind(receipt, journal=bound)
    other = replace(c.operation, request_id='other', intent=b'\0')
    bound = journal.bind(self.spec, other, c.args, str(c.directory))
    cap = t.completion.bind(receipt, journal=bound)
    for call in (lambda: cap.complete(t.OK), cap.uncertain):
      with patch.object(journal.os, 'getpid', return_value=os.getpid()+1):
        with self.raises(t.ledger.EffectError, 'another invocation owner'):
          call()
      with t.ledger._checked(receipt) as (_, _, entry):
        self.eq(entry['state'], 'submitted')
    before = self.path.read_bytes()
    with t.protocol.pin(c.directory) as record, record.drain(), record.start():
      with self.raises(t.ledger.EffectError, 'inside START'):
        cap.complete(t.OK)
    self.eq(self.path.read_bytes(), before)
    thread, results = c.launch(lambda: cap.complete(t.OK))
    self.eq(c.join(thread, results), t.OK)
    after = self.path.read_bytes()
    self.eq(cap.complete(t.OK), t.OK)
    self.eq(self.path.read_bytes(), after)

  def test_prior_outcome_conflict(self):
    bind = t.completion.bind
    def binding(*args, **kwargs):
      cap = bind(*args, **kwargs)
      if kwargs.get('journal') is not None:
        t.ledger.complete(args[0], dict(status='failed', detail='prior failure'))
      return cap
    with patch.object(t.completion, 'bind', side_effect=binding):
      with self.raises(t.ledger.EffectError, 'conflict'):
        self.submit()
    self.no(self.path.exists())
    self.eq(self.c.probe('drain'), 'blocked')

  def test_failed_effect(self):
    self.c.boundary.on_finish = lambda: dict(status='failed', detail='no merge')
    self.eq(self.submit().outcome['status'], 'failed')
    self.no(self.path.exists())
    self.eq(self.c.rows()[0]['state'], 'completed')

class TestM6(unittest.TestCase):
  eq = unittest.TestCase.assertEqual
  yes = unittest.TestCase.assertTrue
  def capture(self, version, case):
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/fixtures/d5/r4_m6.py'), version, case], capture_output=True, text=True, timeout=40)
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
