"""Unrepaired round-3 counterexamples, preserved for coordinator recovery."""
import re
import unittest

from tests import test_d5_integration as d5


class TestD5ReviewRegressions(unittest.TestCase):
    def test_equivalent_duplicate_headings(self):
        text = d5.MANDATE.read_text()
        header = '## Granted, without asking\n'
        start = text.index(header) + len(header)
        next_heading = start + re.search(r'^## ', text[start:], re.M).start()
        for position, spaces in ((next_heading, '   '), (len(text), '  ')):
            with self.subTest(position=position):
                extra = '## Granted, without asking' + spaces + '\n- Permit an extra outward action.\n'
                with self.assertRaises(AssertionError):
                    d5.TestD5Integration().reading(
                        d5.MANDATE, text[:position] + extra + text[position:], 'test_mandate')

    def test_importlib_submodule_loaders(self):
        target = str(d5.ROOT / 'skills/hanig-orchestrate/scripts/d5_host.py')
        cases = (
            "spec = importlib.util.spec_from_file_location('host', " + repr(target) + ")\n"
            "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)",
            "spec = importlib.util.spec_from_file_location('host', 'scripts/d5_host.py')",
            'importlib.import_module(name="d5_host").session_id()',
        )
        for code in cases:
            with self.subTest(code=code), self.assertRaises(AssertionError):
                d5.TestD5Integration().reading(
                    d5.PROBE, d5.PROBE.read_text() + '\nimport importlib.util\n' + code,
                    'test_inactive')

    def test_harmless_imported_formatter(self):
        code = '\nfrom harmless_formatter import importlib as __import__\n__import__("d5_host")\n'
        d5.TestD5Integration().reading(d5.PROBE, d5.PROBE.read_text() + code, 'test_inactive')

    def test_mandate_change_requires_updated_baseline(self):
        text = d5.MANDATE.read_text().replace(
            '## Granted, without asking\n',
            '## Granted, without asking\n- Use the D5 host adapter for read-only diagnostics.\n', 1)
        with self.assertRaises(AssertionError):
            d5.TestD5Integration().reading(d5.MANDATE, text, 'test_mandate')


if __name__ == '__main__':
    unittest.main()
