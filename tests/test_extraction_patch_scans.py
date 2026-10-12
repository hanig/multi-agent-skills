"""Exercise extraction patch guards through their real test-suite consumers."""

import ast
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import test_swarm_evidence as evidence
from tests import test_swarm_outbox as outbox
from tests.patch_scan import patch_targets


class TestPatchScanClassification(unittest.TestCase):
    def check_sources(self, sources, line=None):
        guards = (
            (outbox, outbox.TestOutboxExtraction,
             "test_tests_patch_outbox_owner_not_facade", "validate_intent",
             "swarm_outbox"),
            (evidence, evidence.TestEvidenceExtraction,
             "test_tests_patch_evidence_owner_not_facade", "load_merge_receipts",
             "swarm_evidence"),
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
            for module, case, method, name, owner in guards:
                for source, rejection in sources:
                    with self.subTest(guard=method, source=source):
                        fixture.write_text(source.format(name=name, owner=owner))
                        with mock.patch.object(module, "ROOT", root):
                            guard = getattr(case(method), method)
                            if rejection:
                                with self.assertRaisesRegex(
                                        AssertionError,
                                        rejection + ".*test_patch_scan_fixture.py") as caught:
                                    guard()
                                if line is not None:
                                    self.assertIn("'test_patch_scan_fixture.py', %d," % line,
                                                  str(caught.exception))
                            else:
                                guard()

    def test_readonly_lookups_pass_but_facade_mutations_fail(self):
        self.check_sources((
            ('import pydoc\npydoc.locate("swarm.{name}")\n', None),
            ('mock.patch("swarm.{name}")\n', "facade"),
            ('patch("swarm.{name}")\n', "facade"),
            ('import swarm as S\nmock.patch.object(S, "{name}")\n', "facade"),
            ('import swarm as S\nsetattr(S, "{name}", replacement)\n', "facade"),
            ('import swarm as S\nS.{name} = replacement\n', "facade"),
        ))

    def test_owner_alias_binding_passes(self):
        self.check_sources((
            ('import {owner} as swarm\n'
             'mock.patch.object(swarm, "{name}", return_value=[])\n', None),
        ))

    def test_facade_alias_binding_fails(self):
        self.check_sources((
            ('import swarm as S\nmock.patch.object(S, "{name}")\n', "facade"),
        ))

    def test_unbound_name_fails_as_unresolved(self):
        self.check_sources((
            ('mock.patch.object(unbound, "{name}")\n', "unresolved"),
        ))

    def test_keyword_facade_patch_fails_as_unresolved(self):
        self.check_sources((
            ('import swarm as S\n'
             'mock.patch.object(target=S, attribute="{name}", return_value=[])\n',
             "unresolved"),
        ), line=2)

    def test_variable_attribute_facade_patch_fails_as_unresolved(self):
        self.check_sources((
            ('import swarm as S\nmock.patch.object(S, name, replacement)\n',
             "unresolved"),
        ), line=2)

    def test_variable_attribute_unrelated_targets_pass(self):
        self.check_sources((
            ('import unrelated as S\nmock.patch.object(S, name, replacement)\n', None),
            ('target = object()\nmock.patch.object(target, name, replacement)\n', None),
        ))

    def test_ignored_outer_call_still_scans_nested_patch(self):
        self.check_sources((
            ('import swarm as S\n'
             'mock.patch.object(local, name, mock.patch.object(S, "{name}"))\n',
             "facade"),
        ), line=2)

    def test_unsupported_patch_object_forms_fail_as_unresolved(self):
        for module in ("swarm", "{owner}"):
            self.check_sources(tuple(
                ('import ' + module + ' as S\nmock.patch.object(' + args + ')\n',
                 "unresolved")
                for args in (
                    'target=S, attribute="{name}", return_value=[]',
                    'S, attribute="{name}", return_value=[]',
                    'S, member, return_value=[]',
                    'S, 42, return_value=[]',
                    'S, "unrelated" + "_member", return_value=[]',
                    '*arguments',
                    '**keywords',
                    '*targets, "unrelated", replacement',
                    'S, *attributes',
                )
            ), line=2)

    def test_all_four_import_forms_at_module_and_function_scope(self):
        forms = (
            ("import swarm_outbox", "swarm_outbox", "swarm_outbox"),
            ("import swarm_outbox as swarm", "swarm", "swarm_outbox"),
            ("from package import swarm_outbox", "swarm_outbox", "package.swarm_outbox"),
            ("from package import swarm_outbox as swarm", "swarm", "package.swarm_outbox"),
        )
        for statement, target, expected in forms:
            source = statement + '\nmock.patch.object(' + target + ', "member")\n'
            for function in (False, True):
                with self.subTest(statement=statement, function=function):
                    text = ("def test_case():\n" + "".join(
                        "    " + line for line in source.splitlines(keepends=True))
                            if function else source)
                    records = list(patch_targets(ast.parse(text), {"member"}))
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0], (3 if function else 2,
                                                  target, "member", expected))

    def test_function_bindings_are_local_and_override_outer_imports(self):
        source = '''import swarm as target
def owner_case():
    import swarm_outbox as target
    mock.patch.object(target, "member")
def facade_case():
    mock.patch.object(target, "member")
def other_local():
    import swarm_outbox as local_only
def unresolved_case():
    mock.patch.object(local_only, "member")
'''
        records = list(patch_targets(ast.parse(source), {"member"}))
        self.assertEqual(records, [
            (4, "target", "member", "swarm_outbox"),
            (6, "target", "member", "swarm"),
            (10, "local_only", "member", None),
        ])

    def test_patch_strings_keep_module_identity_despite_local_alias(self):
        source = 'import swarm_outbox as swarm\nmock.patch("swarm.member")\n'
        self.assertEqual(list(patch_targets(ast.parse(source), {"member"})),
                         [(2, "swarm", "member", "swarm")])


if __name__ == "__main__":
    unittest.main()
