"""Recovery-only failing counterexample; no repair or review pass claimed."""
import struct
import unittest
from unittest import mock
from tests.test_d5_host import H


class TestD5AcceptanceRegression(unittest.TestCase):
    def test_empty_argv0_cannot_identify_a_host(self):
        # KERN_PROCARGS2: argc, exec path, padding, empty argv[0], argv[1].
        raw = struct.pack('<i', 2) + b'/bin/worker\0\0\0claude\0'
        with mock.patch.object(H, '_sysctl', return_value=raw):
            command = H._argv0(7)
        with self.subTest(check='argv0'):
            self.assertEqual(command, '')
        with self.subTest(check='selection'):
            self.assertIsNone(H.select_session_ancestor([
                H.ProcessRecord(7, 1, 10, command)]))


if __name__ == '__main__':
    unittest.main()
