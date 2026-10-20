"""Inactive integration inventory: real parsers, source symbols and mandate bytes."""
import argparse
import ast
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock
from tests.watchdog_guard import guarded_main

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/d5'


class Captured(Exception):
    def __init__(self, parser):
        self.parser = parser


def parser_for(tool):
    path = ROOT / tool['file']
    with mock.patch.object(sys, 'path', [str(path.parent)] + sys.path):
        spec = importlib.util.spec_from_file_location('_d5_' + path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if path.name == 'review.py':
            return module.build_parser()
        def capture(parser, *args, **kwargs):
            raise Captured(parser)
        with mock.patch.object(argparse.ArgumentParser, 'parse_args', capture):
            try:
                guarded_main(module.main)
            except Captured as exc:
                return exc.parser
    raise AssertionError('main did not reach parse_args')


def subcommands(parser, prefix=''):
    result = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                key = (prefix + ' ' + name).strip()
                result.add(key)
                result.update(subcommands(child, key))
    return result or {prefix or '<root>'}


def function_exists(file, function):
    paths = [ROOT / file] if '/' in file else list(ROOT.glob('skills/hanig-*/scripts/' + file))
    assert len(paths) == 1, file
    nodes = ast.parse(paths[0].read_text()).body
    for part in function.split('.'):
        match = next((n for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                      and n.name == part), None)
        assert match is not None, (file, function)
        nodes = match.body


def sections(text):
    result = {}
    for h in ('Granted, without asking', 'Bounded by', 'Always stop and ask'):
        body = re.search(r'^## ' + re.escape(h) + r'\n(.*?)(?=^## |\Z)', text, re.M | re.S)[1]
        result[h] = re.findall(r'^- .*?(?=\n[^ ]|\Z)', body, re.M | re.S)
    return result


class TestD5Integration(unittest.TestCase):
    def test_parsers_and_manifest(self):
        m = json.loads((FIXTURES / 'integration-manifest.json').read_text())
        assert set(m['tools']) == {'swarm.py','merge_unit.py','linear_sync.py',
                                   'tickets.py','review.py','committee.py'}
        for name, tool in m['tools'].items():
            parser = parser_for(tool)
            assert subcommands(parser) == set(tool['commands']) - {'continue --write'}, name
            assert tool['parser_check']
            if name == 'swarm.py':
                assert 'continue --write' in tool['commands']
                sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
                assert any('--write' in a.option_strings for a in sub.choices['continue']._actions)
            rows = [dict(zip(m['entry_columns'], r)) for r in tool['commands'].values()]
            for row in rows + list(tool.get('modes', {}).values()):
                function_exists(tool['file'], row['source_function'])
                assert row['classification'] in ('read_only','outward','local_mutation','unresolved')
                assert row['worker'] is (name == 'review.py')
                assert all(lock in m['locks'] for lock in row['locks'])
                if row['classification'] == 'unresolved':
                    assert row['note']
                if row['classification'] == 'outward':
                    assert row['boundaries']
                assert all(b in m['boundaries'] for b in row['boundaries'])
        for raw in m['boundaries'].values():
            b = dict(zip(m['boundary_columns'], raw))
            function_exists(b['file'], b['function'])
            assert b['starts'] and b['lifetime'] and type(b['can_outlive_command']) is bool
        for lock in list(m['locks'].values()) + [m['worker_gate']['source']]:
            function_exists(lock['file'], lock['function'])
        for tool, expected in {
            'swarm.py': {'status':'read_only','validate':'read_only','run':'outward','advance':'outward'},
            'merge_unit.py': {'<root>':'outward'},
            'linear_sync.py': {'drain':'outward','file':'outward','issue new':'outward'}
        }.items():
            for command, classification in expected.items():
                assert m['tools'][tool]['commands'][command][0] == classification

    def test_parser_fixture_and_missing_function(self):
        parser = argparse.ArgumentParser()
        parser.add_subparsers().add_parser('added')
        assert subcommands(parser) != {'<root>'}
        with self.assertRaises(AssertionError):
            function_exists('skills/hanig-project/scripts/linear_sync.py', 'nonexistent_function')

    def test_mandate_baseline(self):
        self.assertEqual(sections((ROOT / 'docs/orchestrator-mandate.md').read_text()),
                         json.loads((FIXTURES / 'mandate-baseline.json').read_text()))

    def test_no_production_import(self):
        paths = list((ROOT / 'skills').glob('hanig-*/scripts/**/*.py'))
        paths += list((ROOT / 'lib').rglob('*.py')) + list((ROOT / 'scripts').rglob('*.py'))
        for path in paths:
            if path.name == 'd5_host.py':
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    assert all('d5_host' not in a.name.split('.') for a in node.names), path
                elif isinstance(node, ast.ImportFrom):
                    assert 'd5_host' not in (node.module or '').split('.'), path
                    assert all(a.name != 'd5_host' for a in node.names), path
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert node.value != 'd5_host' and not node.value.endswith('/d5_host.py'), path


if __name__ == '__main__':
    unittest.main()
