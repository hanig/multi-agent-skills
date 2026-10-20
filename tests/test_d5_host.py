"""Captured ancestry and synthetic kernel identity fixtures."""
import ctypes, errno, json, os, struct, sys, unittest
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
   kernel.sysctl.side_effect=lambda*a: -1
   with patch(H.C, 'get_errno', return_value=errno.EPERM):
    self.instance(identity(7), H.Unknown)
   kernel.sysctl.side_effect=lookup
   raw.clear()
   self.instance(identity(7), H.Unknown)

 def test_session(self):
  with mock.patch.dict(os.environ, {}, clear=True):
   self.none(H.session_id())
   os.environ['CLAUDE_CODE_SESSION_ID']='synthetic-session'
   self.eq(H.session_id(), 'synthetic-session')

if __name__=='__main__':
 unittest.main()
