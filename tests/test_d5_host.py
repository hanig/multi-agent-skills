"""Captured and derived host fixtures."""
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
select=H.select_session_ancestor

F=json.loads((ROOT/'tests/fixtures/d5/host-chains.json').read_text())
IDENTITY=Identity(5808, 1792027014059298)

class TestD5Host(unittest.TestCase):
 eq=unittest.TestCase.assertEqual
 none=unittest.TestCase.assertIsNone
 instance=unittest.TestCase.assertIsInstance
 def test_ancestor(self):
  for name in ('observed_shape', 'next_call'):
   self.eq(select(F[name]), IDENTITY)
   self.eq(select([F['python_runner']]+F['observed_shape']), IDENTITY)

 def test_transient_claude(self):
  self.eq(select(F['transient_claude']), IDENTITY)

 def test_no_host(self):
  for chain in ([], H.Unknown('failed'), [(7, 1, 10, 'zsh'), (1, 0, 1, 'launchd')],
     [(7, 1, 10, 'claude-helper')], [(7, 1, 10, '')]):
   self.none(select(chain))
  self.eq(select([(7, 1, 10, '/bin/claude')]), Identity(7, 10))

 def test_inverted_hop(self):
  chain=[(9, 8, 30, 'zsh'), (8, 7, 40, 'claude'), (7, 1, 10, 'claude')]
  self.none(select(chain))
  chain[0]=(9, 8, 30, 'claude')
  self.eq(select(chain), Identity(9, 30))
  chain[1]=(8, 7, 30, 'claude')  # ties are valid
  self.eq(select(chain), Identity(7, 10))
  self.none(select([(9, 8, 30, 'zsh'), (7, 1, 10, 'claude')]))

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
    Record(7, 1, 1000001, 'claude'), Record(7, 1, 1000002, 'claude')]):
   self.assertNotEqual(identity(7), identity(7))

 def test_ancestry_order(self):
  rows=[Record(7, 6, 30, 'python'), Record(6, 5, 20, 'claude'),
    Record(5, 0, 20, '-zsh')]
  lookup={r.pid: r for r in rows}
  with patch(H, '_read', side_effect=lookup.__getitem__):
   self.eq(H.ancestry(7), tuple(rows))
  lookup[5]=H.Unknown('unreachable')
  with patch(H, '_read', side_effect=lookup.__getitem__):
   self.eq(H.ancestry(7), tuple(rows[:2]))
  lookup[5]=Record(5, 0, 21, 'claude')
  with patch(H, '_read', side_effect=lookup.__getitem__):
   self.eq(H.ancestry(7), tuple(rows[:2]))
  with patch(H, '_read', side_effect=rows+[H.Unknown('gone')]):
   self.instance(H.ancestry(7), H.Unknown)

 def test_kernel_argv0(self):
  raw=bytearray(648)
  struct.pack_into('<qi', raw, 0, 100, 123456)
  struct.pack_into('<i', raw, 40, 7)
  struct.pack_into('<i', raw, 560, 1)
  raw[243:251]=b'2.1.300\0'  # not argv[0]
  args=struct.pack('<i', 2)+b'/bin/versioned\0\0/bin/claude\0--fixture\0'
  def lookup(mib, count, output, size, new, newsize):
   key=list(mib)
   self.assertIn(key, ([1, 14, 1, 7], [1, 49, 7]))
   data=bytes(raw) if key[1]==14 else args
   if output is not None:
    ctypes.memmove(output, data, len(data))
   size._obj.value=len(data)
   return 0
  kernel=mock.Mock()
  kernel.sysctl.side_effect=lookup
  host=os.uname_result(('Darwin', 'synthetic', '25.5.0', 'fixture', 'arm64'))
  with patch(H.sys, 'platform', 'darwin'), patch(H.os, 'uname', return_value=host), patch(H.C, 'CDLL', return_value=kernel):
   self.eq(identity(7), Identity(7, 100123456))
   self.eq(H._inspect(7), Record(7, 1, 100123456, '/bin/claude'))
   args=struct.pack('<i', 1)+b'/bin/claude\0\0/bin/zsh\0'
   self.none(select([H._inspect(7)]))
   kernel.sysctl.side_effect=lambda*a: -1
   with patch(H.C, 'get_errno', return_value=errno.EPERM):
    self.instance(identity(7), H.Unknown)
   kernel.sysctl.side_effect=lookup
   raw.clear()
   self.instance(identity(7), H.Unknown)

 def test_unreadable_args(self):
  with patch(H, '_sysctl', side_effect=PermissionError(errno.EPERM, 'denied')):
   self.eq(H._argv0(7), '')
  for raw in (b'', struct.pack('<i', 0), struct.pack('<i', 1)+b'no terminator'):
   with patch(H, '_sysctl', return_value=raw):
    self.eq(H._argv0(7), '')

 def test_session(self):
  with mock.patch.dict(os.environ, {}, clear=True):
   self.none(H.session_id())
   os.environ['CLAUDE_CODE_SESSION_ID']='synthetic-session'
   self.eq(H.session_id(), 'synthetic-session')
   self.eq(select(F['observed_shape']), IDENTITY)

if __name__=='__main__':
 unittest.main()
