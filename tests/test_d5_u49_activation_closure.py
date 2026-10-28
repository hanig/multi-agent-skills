"""U4.9 ledger closure; prose-to-ledger completeness is a review obligation.

Fence grammar (physical lines, independent of prose): exactly three backticks
immediately followed by d5-claims; zero to three leading spaces and trailing
spaces/tabs are allowed on both fences. LF and CRLF are accepted. The closing
line has only the three backticks and that whitespace. Extra info tokens,
longer fences, tabs/four spaces of indentation and an unclosed block refuse.
No headings, prose claims or Markdown container semantics are interpreted.
"""
import ast
import copy
import importlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / 'docs/d5-integration.md'
MANIFEST = ROOT / 'tests/fixtures/d5/integration-manifest.json'
CANDIDATE = re.compile(r'^[ \t]*`{3,}[ \t]*d5-claims\b[^\n]*$', re.M)
OPEN = re.compile(r' {0,3}```d5-claims[ \t]*\r?')
CLOSE = re.compile(r'^ {0,3}```[ \t]*\r?$', re.M)
ID = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def claims_block(document):
    starts = list(CANDIDATE.finditer(document))
    assert starts, 'missing-claims-block'
    assert len(starts) == 1, 'duplicate-claims-block'
    opening = starts[0]
    assert OPEN.fullmatch(opening.group()), 'invalid-claims-fence'
    closing = CLOSE.search(document, opening.end())
    assert closing, 'unclosed-claims-block'
    try:
        data = json.loads(document[opening.end():closing.start()],
                          object_pairs_hook=unique_object)
    except ValueError as exc:
        raise AssertionError('invalid-claims-json: ' + str(exc)) from exc
    return opening.start(), closing.end(), data


def ids(values):
    assert isinstance(values, list), 'claim IDs must be a list'
    assert all(isinstance(v, str) and ID.fullmatch(v) for v in values), 'invalid claim ID'
    assert len(values) == len(set(values)), 'duplicate-id-in-list'
    return set(values)


def resolve_test(name):
    assert isinstance(name, str) and re.fullmatch(
        r'tests\.test_\w+\.[A-Za-z_]\w*\.test_\w+', name), 'guarantee-test-missing'
    module, cls_name, method = name.rsplit('.', 2)
    try:
        cls = getattr(importlib.import_module(module), cls_name)
        target = getattr(cls, method)
    except (ImportError, AttributeError) as exc:
        raise AssertionError('guarantee-test-missing: ' + name) from exc
    assert isinstance(cls, type) and issubclass(cls, unittest.TestCase), 'not a TestCase'
    assert callable(target), 'guarantee-test-missing: ' + name
    assert not (cls.__module__.endswith('test_d5_u49_activation_closure') or
                getattr(target, '__module__', '').endswith('test_d5_u49_activation_closure')), \
        'closure-test-self-reference'
    assert not (getattr(cls, '__unittest_skip__', False) or
                getattr(target, '__unittest_skip__', False)), 'guarantee-test-skipped'
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(name)
    assert not loader.errors and suite.countTestCases() == 1, 'guarantee-test-missing'
    return suite


def check_contract(document, manifest):
    _, _, data = claims_block(document)
    assert isinstance(data, dict) and set(data) == {'guarantees', 'non_claims'}, 'claims schema'
    rows = data['guarantees']
    assert isinstance(rows, list), 'guarantees must be a list'
    assert all(isinstance(r, dict) and set(r) == {'id', 'test'} for r in rows), 'guarantee schema'
    guarantees = ids([r['id'] for r in rows])
    non_claims = ids(data['non_claims'])
    assert guarantees.isdisjoint(non_claims), 'id-both-guarantee-and-nonclaim'
    assert guarantees == ids([r['id'] for r in manifest['u49_guarantees']]), 'manifest-id-mismatch'
    assert non_claims == ids(manifest['u49_non_claims']), 'manifest-id-mismatch'
    for row in rows:
        resolve_test(row['test'])
    return data


def render(data):
    return '```d5-claims\n' + json.dumps(data, indent=2) + '\n```'


def mutant(document, name):
    start, end, data = claims_block(document)
    if name == 'guarantee-test-missing':
        data['guarantees'][0]['test'] += '_missing'
    elif name == 'manifest-id-mismatch':
        data['guarantees'][0]['id'] += '-mismatch'
    elif name == 'id-both-guarantee-and-nonclaim':
        data['non_claims'].append(data['guarantees'][0]['id'])
    elif name == 'duplicate-id-in-list':
        data['guarantees'].append(copy.deepcopy(data['guarantees'][0]))
    block = render(data)
    if name == 'duplicate-claims-block':
        block += '\n\n' + block
    elif name == 'missing-claims-block':
        block = ''
    elif name == 'invalid-claims-json':
        block = block.replace('"guarantees":', '"guarantees"', 1)
    return document[:start] + block + document[end:]


MUTATIONS = ('guarantee-test-missing', 'manifest-id-mismatch',
             'duplicate-claims-block', 'missing-claims-block', 'invalid-claims-json',
             'id-both-guarantee-and-nonclaim', 'duplicate-id-in-list')


class TestActivationClosure(unittest.TestCase):
    def setUp(self):
        self.document = DOCUMENT.read_bytes().decode('utf-8')
        self.manifest = json.loads(MANIFEST.read_text(encoding='utf-8'),
                                   object_pairs_hook=unique_object)

    def test_docs_manifest_closure(self):
        check_contract(self.document, self.manifest)

    def check_file(self, document, manifest=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'd5-integration.md'
            path.write_bytes(document.encode('utf-8'))
            case = TestActivationClosure('test_docs_manifest_closure')
            case.document = path.read_bytes().decode('utf-8')
            case.manifest = self.manifest if manifest is None else manifest
            case.test_docs_manifest_closure()

    def assert_mutation(self, name, document=None):
        document = self.document if document is None else document
        self.check_file(document)
        with self.assertRaisesRegex(AssertionError, name):
            self.check_file(mutant(document, name))

    def test_guarantee_test_missing(self):
        self.assert_mutation('guarantee-test-missing')

    def test_manifest_id_mismatch(self):
        self.assert_mutation('manifest-id-mismatch')
        for key in ('u49_guarantees', 'u49_non_claims'):
            for action in ('add', 'remove'):
                changed = copy.deepcopy(self.manifest)
                if action == 'add':
                    changed[key].append({'id': 'extra'} if key == 'u49_guarantees' else 'extra')
                else:
                    changed[key].pop()
                with self.subTest(key=key, action=action), \
                        self.assertRaisesRegex(AssertionError, 'manifest-id-mismatch'):
                    self.check_file(self.document, changed)

    def test_duplicate_claims_block(self):
        self.assert_mutation('duplicate-claims-block')

    def test_missing_claims_block(self):
        self.assert_mutation('missing-claims-block')

    def test_invalid_claims_json(self):
        self.assert_mutation('invalid-claims-json')

    def test_id_both_guarantee_and_nonclaim(self):
        self.assert_mutation('id-both-guarantee-and-nonclaim')

    def test_duplicate_id_in_list(self):
        self.assert_mutation('duplicate-id-in-list')
        data = claims_block(self.document)[2]
        data['non_claims'].append(data['non_claims'][0])
        with self.assertRaisesRegex(AssertionError, 'duplicate-id-in-list'):
            self.check_file(render(data))

    def test_equivalent_prose_rewording_is_accepted(self):
        start, end, _ = claims_block(self.document)
        block = self.document[start:end]
        for document in ('# Reworded\nThe entries below describe the contract.\n' + block +
                         '\n## Reordered explanation\nDifferent words, same inventory.\n', block):
            self.check_file(document)

    def test_heading_whitespace_preserves_mutation_fixtures(self):
        block = render(claims_block(self.document)[2])
        document = ('# U4.9 guarantees \t\n\n\n' + block +
                    '\n\n## Explicit non-claims \t\n\n').replace('\n', '\r\n')
        for name in MUTATIONS:
            with self.subTest(mutation=name):
                self.assert_mutation(name, document)

    def test_fence_grammar_variants(self):
        block = render(claims_block(self.document)[2])
        for indent in ('', ' ', '   '):
            for trailing in ('', ' ', ' \t'):
                for newline in ('\n', '\r\n'):
                    lines = block.splitlines()
                    lines[0] = indent + lines[0] + trailing
                    lines[-1] = indent + lines[-1] + trailing
                    document = newline.join(lines)
                    with self.subTest(indent=indent, trailing=trailing, newline=newline):
                        self.check_file(document)
                        for name in MUTATIONS:
                            self.assert_mutation(name, document)
        for opener in ('```d5-claims json', '```d5-claims\textra',
                       '    ```d5-claims', '\t```d5-claims', '````d5-claims', '``` d5-claims'):
            with self.subTest(opener=opener), self.assertRaisesRegex(AssertionError, 'invalid-claims-fence'):
                self.check_file(opener + '\n' + block.split('\n', 1)[1])
        for closer in ('', '    ```', '\t```', '````', '``` extra'):
            with self.subTest(closer=closer), self.assertRaisesRegex(AssertionError, 'unclosed-claims-block'):
                self.check_file(block.rsplit('\n', 1)[0] + '\n' + closer)

    def test_schema_and_duplicate_json_keys_refuse(self):
        data = claims_block(self.document)[2]
        for changed in ([], dict(data, extra=[]), dict(data, guarantees={}),
                        dict(data, non_claims='id'), dict(data, non_claims=['']),
                        dict(data, guarantees=[{'id': 'x'}])):
            with self.subTest(changed=changed), self.assertRaises(AssertionError):
                self.check_file(render(changed))
        for raw in ('{"guarantees": [], "guarantees": [], "non_claims": []}',
                    '{"guarantees": [{"id":"a", "id":"b", "test":"x"}], "non_claims":[]}'):
            with self.assertRaisesRegex(AssertionError, 'invalid-claims-json: duplicate JSON key'):
                self.check_file('```d5-claims\n' + raw + '\n```')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            json.loads('{"u49_guarantees": [], "u49_guarantees": []}', object_pairs_hook=unique_object)

    def test_test_resolution_rejects_skipped_noncase_and_self(self):
        data = claims_block(self.document)[2]
        name = data['guarantees'][0]['test']
        module, cls_name, method = name.rsplit('.', 2)
        cls = getattr(importlib.import_module(module), cls_name)
        for owner in (cls, getattr(cls, method)):
            with mock.patch.object(owner, '__unittest_skip__', True, create=True), \
                    self.assertRaisesRegex(AssertionError, 'guarantee-test-skipped'):
                self.check_file(render(data))
        with mock.patch.object(importlib.import_module(module), cls_name, type('NonCase', (), {method: lambda: None})), \
                self.assertRaisesRegex(AssertionError, 'not a TestCase'):
            self.check_file(render(data))
        data['guarantees'][0]['test'] = __name__ + '.TestActivationClosure.test_docs_manifest_closure'
        with self.assertRaisesRegex(AssertionError, 'closure-test-self-reference'):
            self.check_file(render(data))

    def test_named_mechanisms_and_behavior_tests(self):
        data = check_contract(self.document, self.manifest)
        names = {r['test'] for r in data['guarantees']}
        for row in self.manifest['u49_guarantees']:
            for field in ('mechanisms', 'tests'):
                self.assertIsInstance(row[field], list)
                self.assertTrue(row[field], row['id'] + ' has no ' + field)
            names.update(row['tests'])
            for reference in row['mechanisms']:
                filename, name = reference.split(':')
                self.assertEqual(Path(filename).name, filename)
                paths = list(ROOT.glob('skills/hanig-*/scripts/' + filename))
                self.assertEqual(len(paths), 1, reference)
                nodes = ast.parse(paths[0].read_text(encoding='utf-8')).body
                for part in name.split('.'):
                    found = [n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef,
                              ast.AsyncFunctionDef)) and n.name == part]
                    self.assertEqual(len(found), 1, reference)
                    node, = found
                    nodes = node.body
                self.assertIsInstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for name in sorted(names):
            with self.subTest(behavior=name):
                result = unittest.TestResult()
                resolve_test(name).run(result)
                self.assertEqual(result.testsRun, 1, name)
                self.assertEqual(result.skipped, [], name)
                self.assertEqual(result.expectedFailures, [], name)
                self.assertTrue(result.wasSuccessful(), '{}\n{}'.format(name, result.errors + result.failures))


if __name__ == '__main__':
    unittest.main()
