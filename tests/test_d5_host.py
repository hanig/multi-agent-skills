"""Synthetic observed host shape and kernel error behavior; no live identifiers."""
import ctypes
import errno
import json
import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock
patch = mock.patch.object

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/hanig-orchestrate/scripts'))
import d5_host as H


class TestD5Host(unittest.TestCase):
    def unknown(self, value):
        self.assertIsInstance(value, H.Unknown)

    def test_persistent_ancestor_across_calls(self):
        fixtures = json.loads((ROOT / 'tests/fixtures/d5/host-chains.json').read_text())
        for chain in fixtures.values():
            self.assertEqual(H.select_session_ancestor(chain), H.ProcessIdentity(103, 1000001))
            self.assertNotEqual(H.select_session_ancestor(chain).pid, chain[0][1])
        self.unknown(H.select_session_ancestor([[7, 1, 10, 'zsh'], [1, 0, 1, 'launchd']]))
        self.unknown(H.select_session_ancestor([[7, 6, 10, 'python'], [5, 1, 1, 'claude']]))
        self.unknown(H.select_session_ancestor(H.Unknown('failed')))

    def test_inspection_failure_never_means_dead(self):
        for failure in (PermissionError(errno.EPERM, 'denied'), OSError('lookup failed')):
            with patch(H, '_kernel_rec', side_effect=failure):
                self.unknown(H.process_identity(123))
                self.unknown(H.ancestry(123))
        with patch(H, '_kernel_rec', return_value=H.Unknown('absent')):
            self.unknown(H.process_identity(123))
        with patch(H.sys, 'platform', 'linux'):
            self.unknown(H.process_identity(123))

    def test_pid_reuse_differs_at_one_microsecond(self):
        with patch(H, '_kernel_rec', side_effect=[
                H.ProcessRecord(7, 1, 1000001, 'claude'), H.ProcessRecord(7, 1, 1000002, 'claude')]):
            self.assertNotEqual(H.process_identity(7), H.process_identity(7))

    def test_kernel_abi_and_failure_results(self):
        raw = bytearray(648)
        struct.pack_into('<qi', raw, 0, 100, 123456)
        struct.pack_into('<i', raw, 40, 7)
        struct.pack_into('<i', raw, 560, 1)
        raw[243:250] = b'claude\0'
        def lookup(mib, count, output, size, new, newsize):
            self.assertEqual(list(mib), [1, 14, 1, 7])
            ctypes.memmove(output, bytes(raw), len(raw))
            return 0
        kernel = mock.Mock()
        kernel.sysctl.side_effect = lookup
        host = os.uname_result(('Darwin', 'synthetic', '25.5.0', 'fixture', 'arm64'))
        with patch(H.sys, 'platform', 'darwin'), patch(H.os, 'uname', return_value=host), patch(H.C, 'CDLL', return_value=kernel):
            self.assertEqual(H.process_identity(7), H.ProcessIdentity(7, 100123456))
            self.assertEqual(H._inspect(7).ppid, 1)
            kernel.sysctl.side_effect = lambda *args: -1
            with patch(H.C, 'get_errno', return_value=errno.EPERM):
                self.unknown(H.process_identity(7))
            def absent(*args):
                args[3]._obj.value = 0
                return 0
            kernel.sysctl.side_effect = absent
            self.unknown(H.process_identity(7))

    def test_ancestry_and_revalidation(self):
        rows = {7:H.ProcessRecord(7, 6, 30, 'python'), 6:H.ProcessRecord(6, 1, 20, 'claude'),
                1:H.ProcessRecord(1, 0, 10, 'launchd')}
        with patch(H, '_kernel_rec', side_effect=rows.__getitem__):
            self.assertEqual(H.ancestry(7), tuple(rows.values()))
        with patch(H, '_kernel_rec', side_effect=list(rows.values()) + [H.Unknown('gone')]):
            self.unknown(H.ancestry(7))

    def test_only_native_session_id(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(H.session_id())
            os.environ['CLAUDE_CODE_SESSION_ID'] = 'synthetic-session'
            self.assertEqual(H.session_id(), 'synthetic-session')


if __name__ == '__main__':
    unittest.main()
