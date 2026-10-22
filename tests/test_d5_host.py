"""Captured ancestry and synthetic kernel identity fixtures."""
import ctypes, errno, json, os, struct, subprocess, sys, unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'skills/hanig-orchestrate/scripts'))
import d5_host as H
identity=H.process_identity
Identity=H.ProcessIdentity
Record=H.ProcessRecord
patch=mock.patch.object

F=json.loads((ROOT/'tests/fixtures/d5/host-chains.json').read_text())

class TestD5Host(unittest.TestCase):
 eq=unittest.TestCase.assertEqual
 none=unittest.TestCase.assertIsNone
 instance=unittest.TestCase.assertIsInstance
 def test_captured_order(self):
  for name in ('observed_shape', 'next_call'):
   rows=[Record(*r) for r in F[name]]
   lookup={r.pid: r for r in rows}
   lookup[1]=H.Unknown('not captured')
   with patch(H, '_read', side_effect=lookup.__getitem__):
    self.eq(H.ancestry(rows[0].pid), tuple(rows))

 def test_inverted_hop(self):
  rows=[Record(9, 8, 30), Record(8, 7, 40), Record(7, 0, 10)]
  lookup={r.pid: r for r in rows}
  with patch(H, '_read', side_effect=lookup.__getitem__) as read:
   self.eq(H.ancestry(9), tuple(rows[:1]))
   self.assertNotIn(mock.call(7), read.call_args_list)
  lookup[8]=Record(8, 7, 30)  # ties are valid
  with patch(H, '_read', side_effect=lookup.__getitem__):
   self.eq(H.ancestry(9), (rows[0], lookup[8], rows[2]))

 def test_unknown(self):
  for failure in (PermissionError(errno.EPERM, 'denied'), OSError('lookup failed')):
   with patch(H, '_read', side_effect=failure):
    self.instance(identity(123), H.Unknown)
    self.instance(H.ancestry(123), H.Unknown)
  with patch(H, '_read', return_value=H.Unknown('absent')):
   self.instance(identity(123), H.Unknown)
  with patch(H.sys, 'platform', 'linux'):
   self.instance(identity(123), H.Unknown)

 def test_reuse(self):
  with patch(H, '_read', side_effect=[
    Record(7, 1, 1000001), Record(7, 1, 1000002)]):
   self.assertNotEqual(identity(7), identity(7))

 def test_ancestry_order(self):
  rows=[Record(7, 6, 30), Record(6, 5, 20), Record(5, 0, 20)]
  lookup={r.pid: r for r in rows}
  with patch(H, '_read', side_effect=lookup.__getitem__):
   self.eq(H.ancestry(7), tuple(rows))
  lookup[5]=H.Unknown('unreachable')
  with patch(H, '_read', side_effect=lookup.__getitem__):
   self.eq(H.ancestry(7), tuple(rows[:2]))
  lookup[5]=Record(5, 0, 21)
  with patch(H, '_read', side_effect=lookup.__getitem__):
   self.eq(H.ancestry(7), tuple(rows[:2]))
  with patch(H, '_read', side_effect=rows+[H.Unknown('gone')]):
   self.instance(H.ancestry(7), H.Unknown)

 def test_kernel_identity(self):
  raw=bytearray(648)
  struct.pack_into('<qi', raw, 0, 100, 123456)
  struct.pack_into('<i', raw, 40, 7)
  struct.pack_into('<i', raw, 560, 1)
  struct.pack_into('<I', raw, 392, 1234)
  def lookup(mib, count, output, size, new, newsize):
   self.eq(list(mib), [1, 14, 1, 7])
   data=bytes(raw)
   if output is not None:
    ctypes.memmove(output, data, len(data))
   size._obj.value=len(data)
   return 0
  kernel=mock.Mock()
  kernel.sysctl.side_effect=lookup
  host=os.uname_result(('Darwin', 'synthetic', '25.5.0', 'fixture', 'arm64'))
  with patch(H.sys, 'platform', 'darwin'), patch(H.os, 'uname', return_value=host), patch(H.C, 'CDLL', return_value=kernel):
   self.eq(identity(7), Identity(7, 100123456))
   self.eq(H._inspect(7), Record(7, 1, 100123456))
   self.eq(H.process_uid(7), 1234)
   kernel.sysctl.side_effect=lambda*a: -1
   with patch(H.C, 'get_errno', return_value=errno.EPERM):
    self.instance(identity(7), H.Unknown)
    self.instance(H.process_uid(7), H.Unknown)
   kernel.sysctl.side_effect=lookup
   struct.pack_into('<i', raw, 40, 8)
   self.instance(H.process_uid(7), H.Unknown)
   raw.clear()
   self.instance(identity(7), H.Unknown)
   self.instance(H.process_uid(7), H.Unknown)

 def test_session(self):
  with mock.patch.dict(os.environ, {}, clear=True):
   self.none(H.session_id())
   os.environ['CLAUDE_CODE_SESSION_ID']='synthetic-session'
   self.eq(H.session_id(), 'synthetic-session')

class TestSessionAnchor(unittest.TestCase):
 def setUp(self):
  for target, name, value in ((H, '_measured_platform', True),
    (H.os, 'getpid', 90), (H.os, 'getppid', 80), (H.os, 'getuid', 501),
    (H, 'process_uid', 501)):
   context=patch(target, name, return_value=value)
   context.start()
   self.addCleanup(context.stop)
  context=mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'native', 'CLAUDE_PID': '70'}, clear=True)
  context.start()
  self.addCleanup(context.stop)
  self.rows=[Record(90, 80, 30), Record(80, 70, 20), Record(70, 1, 10)]
  context=patch(H, 'ancestry', return_value=tuple(self.rows))
  self.chain=context.start()
  self.addCleanup(context.stop)

 def test_declared_ancestor_not_tool_parent(self):  # U2-M1
  self.assertEqual(H.session_anchor(), ('native', Identity(70, 10)))
  self.chain.assert_called_once_with(90)
  H.process_uid.assert_called_once_with(70)
  H.os.getppid.assert_not_called()
  with patch(H, 'session_id', return_value='sole-reader') as reader:
   self.assertEqual(H.session_anchor(), ('sole-reader', Identity(70, 10)))
   reader.assert_called_once_with()

 def test_missing_session_has_no_fallback(self):  # U2-M2
  for value in (None, ''):
   with self.subTest(value=value):
    if value is None:
     os.environ.pop('CLAUDE_CODE_SESSION_ID', None)
    else:
     os.environ['CLAUDE_CODE_SESSION_ID']=value
    self.assertIsInstance(H.session_anchor(), H.Unknown)
  self.chain.assert_not_called()

 def test_strict_pid_parse(self):  # LOOSE-PID-PARSE
  for value in (' 70', '70\n', '+70', '0', '７０', '٧٠', '01', '-70',
                '1.0', '2147483648', '9999999999', '10000000000', ''):
   with self.subTest(value=value):
    os.environ['CLAUDE_PID']=value
    self.assertIsInstance(H.session_anchor(), H.Unknown)
  os.environ.pop('CLAUDE_PID')
  self.assertIsInstance(H.session_anchor(), H.Unknown)
  self.chain.assert_not_called()
  os.environ['CLAUDE_PID']='2147483647'
  self.chain.return_value=(self.rows[0], Record(2147483647, 0, 1))
  self.assertEqual(H.session_anchor(), ('native', Identity(2147483647, 1)))

 def test_strict_ancestor_only(self):  # ANCHOR-NOT-IN-CHAIN / SELF-AS-ANCHOR
  for pid in (60, 90):
   with self.subTest(pid=pid), patch(H, 'process_identity', return_value=Identity(pid, 10)):
    os.environ['CLAUDE_PID']=str(pid)
    self.assertIsInstance(H.session_anchor(), H.Unknown)
  H.process_uid.assert_not_called()

 def test_uid_required(self):  # SKIP-UID-CHECK
  for uid in (0, 502, H.Unknown('unreadable')):
   with self.subTest(uid=uid), patch(H, 'process_uid', return_value=uid):
    self.assertIsInstance(H.session_anchor(), H.Unknown)

 def test_unknown_ancestry(self):
  self.chain.return_value=H.Unknown('changed ancestry')
  self.assertIsInstance(H.session_anchor(), H.Unknown)
  H.process_uid.assert_not_called()

 def test_contract_keyword_arguments(self):
  self.assertTrue(H.verify_anchor(session_id='native', identity=Identity(70, 10)))

 def test_every_verification_rechecks_id_incarnation_chain_and_uid(self):
  wanted=Identity(70, 10)
  self.assertTrue(H.verify_anchor('native', wanted))
  self.assertTrue(H.verify_anchor('native', wanted))
  self.assertEqual(self.chain.call_count, 2)
  self.assertFalse(H.verify_anchor('different', wanted))
  self.assertFalse(H.verify_anchor('native', Identity(70, 9)))
  os.environ['CLAUDE_CODE_SESSION_ID']='changed'
  self.assertFalse(H.verify_anchor('native', wanted))
  os.environ['CLAUDE_CODE_SESSION_ID']='native'
  self.chain.return_value=tuple(self.rows[:2])
  self.assertFalse(H.verify_anchor('native', wanted))
  self.chain.return_value=tuple(self.rows)
  with patch(H, 'process_uid', return_value=0):
   self.assertFalse(H.verify_anchor('native', wanted))
  self.chain.return_value=H.Unknown('lost inspection')
  self.assertFalse(H.verify_anchor('native', wanted))


class TestLiveness(unittest.TestCase):
 def test_identity_and_reuse(self):  # U2-M8; zombies have no separate rule
  with patch(H, '_measured_platform', return_value=True), patch(H.os, 'kill') as kill:
   for start, expected in ((123, 'alive'), (124, 'dead')):
    with self.subTest(start=start), patch(H, '_read', return_value=Record(7, 1, start)):
     self.assertEqual(H.liveness(Identity(7, 123)), expected)
   kill.assert_not_called()

 def test_inspection_failure_needs_kernel_death_evidence(self):  # U2-M7
  failures=[H.Unknown('absent'), PermissionError(errno.EPERM, 'inspection'),
            OSError(errno.EIO, 'inspection'), ValueError('parse')]
  probes=[(None, 'unknown'), (PermissionError(errno.EPERM, 'probe'), 'unknown'),
          (OSError(errno.EIO, 'probe'), 'unknown'),
          (ProcessLookupError(errno.ESRCH, 'probe'), 'dead')]
  with patch(H, '_measured_platform', return_value=True):
   for failure in failures:
    for probe, expected in probes:
     with self.subTest(failure=failure, probe=probe):
      kwargs={'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
      with patch(H, '_read', **kwargs), patch(H.os, 'kill', side_effect=probe) as kill:
       self.assertEqual(H.liveness(Identity(7, 123)), expected)
       kill.assert_called_once_with(7, 0)

 def test_invalid_identities_never_probe(self):
  with patch(H, '_measured_platform', return_value=True), patch(H.os, 'kill') as kill:
   for value in (None, Identity(0, 1), Identity(-1, 1), Identity(True, 1),
                 Identity(2 ** 31, 1), Identity(7, 0), Identity(7, '1')):
    self.assertEqual(H.liveness(value), 'unknown')
   kill.assert_not_called()

 def test_unmeasured_platform_never_permits_or_probes(self):
  hosts=[('linux', '25.5.0', 'arm64'), ('darwin', '24.0.0', 'arm64'),
         ('darwin', '25.5.0', 'x86_64')]
  for platform, release, machine in hosts:
   with self.subTest(platform=platform, release=release, machine=machine):
    host=os.uname_result(('Synthetic', 'fixture', release, '', machine))
    with patch(H.sys, 'platform', platform), patch(H.os, 'uname', return_value=host), \
      patch(H.os, 'kill', side_effect=ProcessLookupError) as kill, patch(H, '_sysctl') as kernel:
     self.assertIsInstance(H.process_uid(7), H.Unknown)
     self.assertIsInstance(H.process_identity(7), H.Unknown)
     self.assertIsInstance(H.session_anchor(), H.Unknown)
     self.assertFalse(H.verify_anchor('native', Identity(7, 123)))
     self.assertEqual(H.liveness(Identity(7, 123)), 'unknown')
     kill.assert_not_called()
     kernel.assert_not_called()


@unittest.skipUnless(H._measured_platform(),
 'real-process tests require measured Darwin 25.5.0 arm64; current: ' + sys.platform + ' ' + str(os.uname()))
class TestRealProcesses(unittest.TestCase):
 def test_real_uid(self):
  self.assertEqual(H.process_uid(os.getpid()), os.getuid())
  self.assertEqual(H.process_uid(1), 0)

 def test_nested_shell_retains_declared_ancestor(self):
  expected=H.process_identity(os.getpid())
  self.assertIsInstance(expected, Identity)
  env=dict(os.environ, CLAUDE_PID=str(os.getpid()), CLAUDE_CODE_SESSION_ID='real-test-session')
  code="""import json, os, sys
sys.path.insert(0, sys.argv[1])
import d5_host as H
anchor=H.session_anchor()
print(json.dumps({'anchor': anchor, 'ppid': os.getppid(),
                  'verified': not isinstance(anchor, H.Unknown) and H.verify_anchor(*anchor)}))
"""
  # The background child plus wait keeps sh alive instead of exec-optimizing it away.
  result=subprocess.run(['/bin/sh', '-c', '"$@" & child=$!; wait "$child"', 'chain',
    sys.executable, '-c', code, str(ROOT/'skills/hanig-orchestrate/scripts')],
    env=env, capture_output=True, text=True, timeout=20, check=True)
  observed=json.loads(result.stdout)
  self.assertNotEqual(observed['ppid'], os.getpid())
  self.assertEqual(observed['anchor'], ['real-test-session', list(expected)])
  self.assertTrue(observed['verified'])

 def test_sleep_alive_then_reaped_dead_and_not_ancestor(self):
  child=subprocess.Popen(['/bin/sleep', '30'])
  try:
   original=H.process_identity(child.pid)
   self.assertIsInstance(original, Identity)
   self.assertEqual(H.liveness(original), 'alive')
   with mock.patch.dict(os.environ, CLAUDE_PID=str(child.pid), CLAUDE_CODE_SESSION_ID='sibling'):
    self.assertIsInstance(H.session_anchor(), H.Unknown)
  finally:
   child.kill()
   child.wait(timeout=10)
  self.assertEqual(H.liveness(original), 'dead')

if __name__=='__main__':
 unittest.main()
