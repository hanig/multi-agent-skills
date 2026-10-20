"""D5 parser parity and boundary definitions."""
import argparse, ast, importlib.util, json, sys, unittest
from pathlib import Path
from unittest import mock
patch=mock.patch.object
from tests.watchdog_guard import guarded_main

SUB=argparse._SubParsersAction
ROOT=Path(__file__).resolve().parents[1]
F=ROOT/'tests/fixtures/d5'

class Captured(Exception):
 def __init__(self, parser):
  self.parser=parser

def source(file):
 paths=list(ROOT.glob('skills/hanig-*/scripts/'+file))
 assert len(paths)==1, file
 return paths[0]

def parser_for(tool):
 path=source(tool['file'])
 with patch(sys, 'path', [str(path.parent)]+sys.path):
  spec=importlib.util.spec_from_file_location('_d5_'+path.stem, path)
  module=importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  if path.name=='review.py':
   return module.build_parser()
  def capture(parser,*args, **kwargs):
   raise Captured(parser)
  with patch(argparse.ArgumentParser, 'parse_args', capture):
   try:
    guarded_main(module.main)
   except Captured as exc:
    return exc.parser
 raise AssertionError('main did not reach parse_args')

def subcommands(parser, prefix=''):
 result=set()
 for action in parser._actions:
  if isinstance(action, SUB):
   for name, child in action.choices.items():
    key=(prefix+' '+name).strip()
    result.add(key)
    result.update(subcommands(child, key))
 return result or {prefix or '<root>'}

def parity(parser, commands):
 assert subcommands(parser)==set(commands)

def exists(file, function):
 nodes=ast.parse(source(file).read_text()).body
 assert any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
   and n.name==function for n in nodes), (file, function)

class TestD5Integration(unittest.TestCase):
 def test_manifest(self):
  m=json.loads((F/'integration-manifest.json').read_text())
  assert set(m['tools'])=={'swarm.py', 'merge_unit.py', 'linear_sync.py',
        'tickets.py', 'review.py', 'committee.py'}
  for name, tool in m['tools'].items():
   parser=parser_for(tool)
   parity(parser, set(tool['commands']) - {'continue --write'})
   assert tool['parser_check']
   if name=='swarm.py':
    assert 'continue --write' in tool['commands']
    sub=next(a for a in parser._actions if isinstance(a, SUB))
    assert any('--write' in a.option_strings for a in sub.choices['continue']._actions)
   rows=[dict(zip(m['entry_columns'], r)) for r in tool['commands'].values()]
   for row in rows+list(tool.get('modes', {}).values()):
    exists(tool['file'], row.get('handler', row['rationale'].split(':', 1)[0]))
    assert row['classification'] in ('read_only', 'outward', 'local_mutation', 'unresolved')
    assert row['rationale'] and len(row['rationale'].splitlines())==1
    assert row['worker'] is (name=='review.py')
    assert all(lock in m['locks'] for lock in row['locks'])
    if row['classification']=='unresolved':
     assert row['note']
    if row['classification']=='outward':
     assert row['boundaries']
    assert all(b in m['boundaries'] for b in row['boundaries'])
  for raw in m['boundaries'].values():
   b=dict(zip(m['boundary_columns'], raw))
   exists(b['file'], b['function'])
   assert b['starts'] and b['lifetime'] and type(b['can_outlive_command']) is bool
  for lock in list(m['locks'].values())+[m['worker_gate']['source']]:
   exists(lock['file'], lock['function'])
  for tool, expected in {
   'swarm.py': {'status': 'read_only', 'validate': 'read_only', 'run': 'outward', 'advance': 'outward'},
   'merge_unit.py': {'<root>': 'outward'},
   'linear_sync.py': {'drain': 'outward', 'file': 'outward', 'issue new': 'outward'}
  }.items():
   for command, kind in expected.items():
    assert m['tools'][tool]['commands'][command][0]==kind

 def test_parser_fixture(self):
  parser=argparse.ArgumentParser()
  sub=parser.add_subparsers()
  issue=sub.add_parser('issue').add_subparsers()
  leaf=issue.add_parser('new').add_subparsers()
  leaf.add_parser('nested')
  expected={'issue', 'issue new', 'issue new nested'}
  parity(parser, expected)
  leaf.add_parser('unclassified')
  with self.assertRaises(AssertionError):
   parity(parser, expected)
  with self.assertRaises(AssertionError):
   parity(parser, expected | {'issue new unclassified', 'extra'})

 def test_definitions_only(self):
  with patch(Path, 'read_text', return_value='async def boundary(): pass'):
   exists('linear_sync.py', 'boundary')
  for text in ('class X:\n def boundary(self): pass',
     'def outer():\n def boundary(): pass', 'def unrelated(): pass'):
   with patch(Path, 'read_text', return_value=text):
    with self.assertRaises(AssertionError):
     exists('linear_sync.py', 'boundary')

if __name__=='__main__':
 unittest.main()
