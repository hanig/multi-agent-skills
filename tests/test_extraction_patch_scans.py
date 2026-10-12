"""Exercise extraction patch guards through their real test-suite consumers."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import test_swarm_evidence as evidence
from tests import test_swarm_outbox as outbox

ROOT = Path(__file__).resolve().parents[1]


class TestPatchScanClassification(unittest.TestCase):
    def test_readonly_lookups_pass_but_facade_mutations_fail(self):
        guards = (
            (outbox, outbox.TestOutboxExtraction,
             "test_tests_patch_outbox_owner_not_facade", "normalize_intent"),
            (evidence, evidence.TestEvidenceExtraction,
             "test_tests_patch_evidence_owner_not_facade", "load_merge_receipts"),
        )
        sources = (
            ('import pydoc\npydoc.locate("swarm.{name}")\n', False),
            ('mock.patch("swarm.{name}")\n', True),
            ('patch("swarm.{name}")\n', True),
            ('mock.patch.object(S, "{name}")\n', True),
            ('setattr(S, "{name}", replacement)\n', True),
            ('S.{name} = replacement\n', True),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            # Use the real suite as the fixture: required owner patches and
            # child-program audits must remain non-vacuous in both consumers.
            for path in (ROOT / "tests").glob("test_*.py"):
                (tests / path.name).symlink_to(path)
            fixture = tests / "test_patch_scan_fixture.py"
            for module, case, method, name in guards:
                for source, rejected in sources:
                    with self.subTest(guard=method, source=source):
                        fixture.write_text(source.format(name=name))
                        with mock.patch.object(module, "ROOT", root):
                            guard = getattr(case(method), method)
                            if rejected:
                                with self.assertRaisesRegex(
                                        AssertionError, "test_patch_scan_fixture.py"):
                                    guard()
                            else:
                                guard()


if __name__ == "__main__":
    unittest.main()
