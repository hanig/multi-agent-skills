"""Narrow U4.9 closure: documented guarantees, definitions and behavior tests.

This names and exercises merged mechanisms, not arbitrary route coverage or
interpreter isolation. Active behavior uses the cited tests' synthetic fixtures.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import re
import unittest

from tests.test_docs_truth import lex_document


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
        'cde6f723e324e1d088f444f6375f104fa243a381ee73b8e42f7dd1ca620c16d9',
    'direct-root-refusals':
        '529fd01b59fe99808a1c11ca72df281782ba10a950b60696026d7671ac43cd83',
    'merge-admission-switch':
        '6ccaebe2ea1bf15d3418885f3cb68b6681da4af8b9984d127c1415b8c6d9e4cc',
    'unavailable-audit':
        '246bb384ff5ecb35fb6a90aa3c73c4e538d1521deb514a33380d14c509fa29ab',
    'entry-point-routing':
        '7813887f86b650f086555622f7d7b60d591baf5bf3f8a1eb2f29b567a672eee0',
    'bytecode-advisory':
        'e9683c0e7013093345e68b9bc1c89683e75ee2276a17554449445f5b6b031e0d',
}
START = '## U4.9 guarantees\n\n'
END = '\n\n## Explicit non-claims\n'


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate manifest key: ' + key)
        result[key] = value
    return result


def read_guarantees(raw):
    return json.loads(raw, object_pairs_hook=unique_object)['u49_guarantees']


def guarantee_table(rows):
    lines = ['| ID | Guarantee | Mechanisms | Tests |', '|---|---|---|---|']
    for row in rows:
        lines.append('| {id} | {claim} | {mechanisms} | {tests} |'.format(
            id=row['id'], claim=row['claim'],
            mechanisms='<br>'.join('`' + s + '`' for s in row['mechanisms']),
            tests='<br>'.join('`' + s + '`' for s in row['tests'])))
    return '\n'.join(lines)


def check_docs(test, document, rows):
    # Locate visible Markdown headings, not a decoy inside a comment or fence.
    _, lines = lex_document(document)
    bounds = []
    for title in ('U4.9 guarantees', 'Explicit non-claims'):
        pattern = r' {0,3}##[ \t]+' + re.escape(title) + r'(?:[ \t]+#+)?[ \t]*'
        matches = [number for number, text, hidden in lines
                   if not hidden and re.fullmatch(pattern, text)]
        test.assertEqual(len(matches), 1, title)
        bounds.append(matches[0])
    start, end = bounds
    test.assertLess(start, end)
    section = '\n'.join(document.splitlines()[start:end - 1]).strip('\n')
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
        # Reference order is not coverage; retain every member, including duplicates.
        contract = dict(row, mechanisms=sorted(row['mechanisms']), tests=sorted(row['tests']))
        binding = json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()
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
        self.rows = read_guarantees(MANIFEST.read_text(encoding='utf-8'))
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

    def test_duplicate_manifest_members_refuse(self):
        for key in ('id', 'claim', 'tests', 'mechanisms'):
            raw = '{"u49_guarantees": [{' + json.dumps(key) + ': null, ' + json.dumps(self.rows[0])[1:] + ']}'
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'duplicate manifest key'):
                read_guarantees(raw)
        with self.assertRaisesRegex(ValueError, 'duplicate manifest key'):
            read_guarantees('{"u49_guarantees": [], "u49_guarantees": []}')

    def test_hidden_decoy_cannot_replace_visible_guarantees(self):
        bad = copy.deepcopy(self.rows)
        bad[0]['claim'] += ' Review and committee admission is enforced under D5.'
        prefix, rest = self.document.split(START, 1)
        _, suffix = rest.split(END, 1)
        for opening, closing in (('<!--\n', '-->'), ('```\n', '```'), ('~~~\n', '~~~')):
            changed = (prefix + opening + START + guarantee_table(self.rows) + END + closing
                + '\n\n## U4.9 guarantees \n\n' + guarantee_table(bad)
                + '\n\n## Explicit non-claims \n' + suffix)
            with self.subTest(wrapper=opening), self.assertRaises(AssertionError):
                check_docs(self, changed, self.rows)
            valid = changed.replace(guarantee_table(bad), guarantee_table(self.rows))
            check_docs(self, valid, self.rows)

    def test_reference_order_does_not_change_coverage(self):
        rows = copy.deepcopy(self.rows)
        for row in rows:
            for field in ('mechanisms', 'tests'):
                row[field].reverse()
        document = self.document.replace(guarantee_table(self.rows), guarantee_table(rows))
        check_docs(self, document, rows)
        check_coverage(self, rows)


if __name__ == '__main__':
    unittest.main()
