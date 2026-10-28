"""Narrow U4.9 closure: documented guarantees, definitions and behavior tests.

This names and exercises merged mechanisms, not arbitrary route coverage or
interpreter isolation. Active behavior uses the cited tests' synthetic fixtures.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / 'docs/d5-integration.md'
MANIFEST = ROOT / 'tests/fixtures/d5/integration-manifest.json'
GUARANTEES = frozenset({
    'merge-child-containment', 'direct-root-refusals', 'merge-admission-switch',
    'unavailable-audit', 'entry-point-routing', 'bytecode-advisory',
})
# Pin the reviewed literal contract independently of the two editable sources.
# A new claim or evidence mapping needs an explicit test-contract change and
# review. These fingerprints are not a semantic proof or protection from an
# editor who also rewrites the test. They prevent synchronized prose/manifest
# changes from silently reusing coverage for a different claim.
REVIEWED_BINDINGS = {
    'merge-child-containment':
        '006696a325f050075805674995350905cda3f709fbcc399d9c63cb4db36ed2ae',
    'direct-root-refusals':
        'a6f482af7fe843e504b0bc48ffd471062f365663c55c81384e2600da224d3bed',
    'merge-admission-switch':
        '173162dfdfa7e0f8ce164c249ab0ee43fa7cbe68db040399b531c08e74657cdf',
    'unavailable-audit':
        '246bb384ff5ecb35fb6a90aa3c73c4e538d1521deb514a33380d14c509fa29ab',
    'entry-point-routing':
        '31f881f1d884ddafa0b89bc697694a1ffa5255028a730568d0dca9844a407b1c',
    'bytecode-advisory':
        '812c67f2c5fea3d950522e02044d3a8ab94668eb53a32fdcc0e8ed9ee17a0948',
}
START = '## U4.9 guarantees\n\n'
END = '\n\n## Explicit non-claims\n'


def guarantee_table(rows):
    lines = ['| ID | Guarantee | Mechanisms | Tests |', '|---|---|---|---|']
    for row in rows:
        lines.append('| {id} | {claim} | {mechanisms} | {tests} |'.format(
            id=row['id'], claim=row['claim'],
            mechanisms='<br>'.join('`' + s + '`' for s in row['mechanisms']),
            tests='<br>'.join('`' + s + '`' for s in row['tests'])))
    return '\n'.join(lines)


def check_docs(test, document, rows):
    # Compare the entire section, including prose: an unlisted line cannot hide
    # between recognized rows. Other sections explicitly record non-claims and
    # earlier preparation, and are not parsed as U4.9 activation guarantees.
    test.assertEqual(document.count(START), 1)
    test.assertEqual(document.count(END), 1)
    section = document.split(START, 1)[1].split(END, 1)[0]
    test.assertEqual(section, guarantee_table(rows), 'manifest-docs-mismatch')


def definition(test, path, qualified_name):
    nodes = ast.parse(path.read_text(encoding='utf-8')).body
    for name in qualified_name.split('.'):
        matches = [n for n in nodes if isinstance(n, (
            ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        test.assertEqual(len(matches), 1, '{}:{}'.format(path, qualified_name))
        node, = matches
        nodes = node.body
    return node


def check_coverage(test, rows):
    test.assertEqual({row['id'] for row in rows}, GUARANTEES)
    test.assertEqual(len(rows), len(GUARANTEES), 'duplicate guarantee')
    for row in rows:
        test.assertEqual(set(row), {'id', 'claim', 'mechanisms', 'tests'})
        test.assertTrue(row['claim'])
        for key in ('mechanisms', 'tests'):
            test.assertIsInstance(row[key], list)
            test.assertTrue(row[key], '{} has no {}'.format(row['id'], key))
        binding = json.dumps(row, sort_keys=True, separators=(',', ':')).encode()
        test.assertEqual(hashlib.sha256(binding).hexdigest(), REVIEWED_BINDINGS[row['id']],
                         'unreviewed guarantee binding: ' + row['id'])
        for reference in row['mechanisms']:
            filename, name = reference.split(':')
            test.assertEqual(Path(filename).name, filename)
            paths = list(ROOT.glob('skills/hanig-*/scripts/' + filename))
            test.assertEqual(len(paths), 1, reference)
            test.assertIsInstance(definition(test, paths[0], name),
                                  (ast.FunctionDef, ast.AsyncFunctionDef))
        for name in row['tests']:
            package, module, cls, method = name.split('.')
            test.assertEqual(package, 'tests')
            test.assertIn(module, ('test_d5_containment', 'test_d5_u49_entrypoints'))
            test.assertTrue(method.startswith('test_'))
            test.assertIsInstance(definition(test, ROOT / package / (module + '.py'),
                                             cls + '.' + method), ast.FunctionDef)


class TestActivationClosure(unittest.TestCase):
    def setUp(self):
        self.rows = json.loads(MANIFEST.read_text(encoding='utf-8'))['u49_guarantees']
        self.document = DOCUMENT.read_text(encoding='utf-8')

    def test_docs_manifest_match(self):
        check_docs(self, self.document, self.rows)

    def test_documented_guarantees_have_tested_mechanisms(self):
        check_docs(self, self.document, self.rows)
        check_coverage(self, self.rows)

    def test_named_behavior_tests_pass(self):
        check_coverage(self, self.rows)
        names = sorted({name for row in self.rows for name in row['tests']})
        for name in names:
            with self.subTest(behavior=name):
                loader = unittest.TestLoader()
                suite = loader.loadTestsFromName(name)
                self.assertEqual(loader.errors, [], name)
                self.assertEqual(suite.countTestCases(), 1, name)
                result = unittest.TestResult()
                suite.run(result)
                self.assertEqual(result.testsRun, 1, name)
                self.assertEqual(result.skipped, [], name)
                self.assertEqual(result.expectedFailures, [], name)
                self.assertTrue(result.wasSuccessful(),
                    '{}\n{}'.format(name, result.errors + result.failures))

    def test_documented_guarantee_untested_mutation(self):
        extra = '| untested | A new guarantee without a test. | | |'
        changed = self.document.replace(END, '\n' + extra + END)
        with self.assertRaisesRegex(AssertionError, 'manifest-docs-mismatch'):
            check_docs(self, changed, self.rows)
        # Matching text alone must not certify an empty evidence list either.
        rows = copy.deepcopy(self.rows)
        rows[0]['tests'] = []
        with self.assertRaisesRegex(AssertionError, 'has no tests'):
            check_coverage(self, rows)

    def test_manifest_docs_mismatch_mutation(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['claim'] += ' Unmatched claim.'
        with self.assertRaisesRegex(AssertionError, 'manifest-docs-mismatch'):
            check_docs(self, self.document, rows)
        # Pin deletion and free prose as well as a new recognized row.
        with self.assertRaisesRegex(AssertionError, 'manifest-docs-mismatch'):
            check_docs(self, self.document, rows[1:])
        changed = self.document.replace(END, '\nAn untested prose guarantee.' + END)
        with self.assertRaisesRegex(AssertionError, 'manifest-docs-mismatch'):
            check_docs(self, changed, self.rows)

    def assert_synchronized_change_refused(self, rows):
        document = self.document.replace(guarantee_table(self.rows), guarantee_table(rows))
        check_docs(self, document, rows)  # Both editable sources agree.
        consumer = TestActivationClosure('test_documented_guarantees_have_tested_mechanisms')
        consumer.rows, consumer.document = rows, document
        with self.assertRaisesRegex(AssertionError, 'unreviewed guarantee binding'):
            consumer.test_documented_guarantees_have_tested_mechanisms()

    def test_synchronized_unsupported_claim_is_rejected(self):
        for index, row in enumerate(self.rows):
            with self.subTest(guarantee=row['id']):
                rows = copy.deepcopy(self.rows)
                rows[index]['claim'] += ' Review and committee admission is enforced under D5.'
                self.assert_synchronized_change_refused(rows)

    def test_synchronized_evidence_rebinding_is_rejected(self):
        for index, row in enumerate(self.rows):
            for field in ('mechanisms', 'tests'):
                with self.subTest(guarantee=row['id'], field=field):
                    rows = copy.deepcopy(self.rows)
                    rows[index][field] = self.rows[(index + 1) % len(rows)][field][:]
                    self.assert_synchronized_change_refused(rows)


if __name__ == '__main__':
    unittest.main()
