"""Synthetic observed host shape and kernel error behavior; no live identifiers."""
import ctypes, errno, json, os, struct, sys, unittest
from pathlib import Path
from unittest import mock
patch = mock.patch.object

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/hanig-orchestrate/scripts'))
import d5_host as H
Record = H.ProcessRecord
Identity = H.ProcessIdentity


class TestD5Host(unittest.TestCase):
    def unknown(self, value):
        assert isinstance(value, H.Unknown)

    def test_ancestor(self):
        fixtures = json.loads((ROOT / 'tests/fixtures/d5/host-chains.json').read_text())
        for chain in fixtures.values():
            assert H.select_session_ancestor(chain) == Identity(103, 1000001)
            assert H.select_session_ancestor(chain).pid != chain[0][1]
        self.unknown(H.select_session_ancestor([[7, 1, 10, 'zsh'], [1, 0, 1, 'launchd']]))
        self.unknown(H.select_session_ancestor([[7, 6, 10, 'python'], [5, 1, 1, 'claude']]))
        self.unknown(H.select_session_ancestor(H.Unknown('failed')))

    def test_unknown(self):
        for failure in (PermissionError(errno.EPERM, 'denied'), OSError('lookup failed')):
            with patch(H, '_kernel_rec', side_effect=failure):
                self.unknown(H.process_identity(123))
                self.unknown(H.ancestry(123))
        with patch(H, '_kernel_rec', return_value=H.Unknown('absent')):
            self.unknown(H.process_identity(123))
        with patch(H.sys, 'platform', 'linux'):
            self.unknown(H.process_identity(123))

    def test_reuse(self):
        with patch(H, '_kernel_rec', side_effect=[
                Record(7, 1, 1000001, 'claude'), Record(7, 1, 1000002, 'claude')]):
            assert H.process_identity(7) != H.process_identity(7)

    def test_abi(self):
        raw = bytearray(648)
        struct.pack_into('<qi', raw, 0, 100, 123456)
        struct.pack_into('<i', raw, 40, 7)
        struct.pack_into('<i', raw, 560, 1)
        raw[243:250] = b'claude\0'
        def lookup(mib, count, output, size, new, newsize):
            assert list(mib) == [1, 14, 1, 7]
            ctypes.memmove(output, bytes(raw), len(raw))
            return 0
        kernel = mock.Mock()
        kernel.sysctl.side_effect = lookup
        host = os.uname_result(('Darwin', 'synthetic', '25.5.0', 'fixture', 'arm64'))
        with patch(H.sys, 'platform', 'darwin'), patch(H.os, 'uname', return_value=host), patch(H.C, 'CDLL', return_value=kernel):
            assert H.process_identity(7) == Identity(7, 100123456)
            assert H._inspect(7).ppid == 1
            kernel.sysctl.side_effect = lambda *args: -1
            with patch(H.C, 'get_errno', return_value=errno.EPERM):
                self.unknown(H.process_identity(7))
            def absent(*args):
                args[3]._obj.value = 0
                return 0
            kernel.sysctl.side_effect = absent
            self.unknown(H.process_identity(7))

    def test_ancestry(self):
        rows = {7:Record(7, 6, 30, 'python'), 6:Record(6, 1, 20, 'claude'),
                1:Record(1, 0, 10, 'launchd')}
        with patch(H, '_kernel_rec', side_effect=rows.__getitem__):
            assert H.ancestry(7) == tuple(rows.values())
        with patch(H, '_kernel_rec', side_effect=list(rows.values()) + [H.Unknown('gone')]):
            self.unknown(H.ancestry(7))

    def test_session(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert H.session_id() is None
            os.environ['CLAUDE_CODE_SESSION_ID'] = 'synthetic-session'
            assert H.session_id() == 'synthetic-session'


if __name__ == '__main__':
    unittest.main()
