"""U8 cutover policy and real default wiring; external services are never used."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_d5_u4_1_active_surface import SurfaceFixture, S_ENTRIES, M_ENTRIES
from tests.test_d5_u4_0b1_issuance import no_effects
from tests import test_d5_lease as lease_fixture
from tests.fixtures.d5.ready import ready
import d5_activation as activation
import d5_authority as authority
import d5_authority_store as store
import d5_lease as lease
import d5_registry as registry
import merge_unit

PRODUCTION_ACTIVE = registry.d5_active

ROOT = Path(__file__).resolve().parents[1]
MANDATE = ROOT / 'docs/orchestrator-mandate.md'
BASELINE = ROOT / 'tests/fixtures/d5/u8-mandate-baseline.json'


def section(text, heading):
 lines = text.splitlines()
 candidates = [i for i, line in enumerate(lines)
    if line.strip() == '## ' + heading]
 assert len(candidates) == 1, 'ambiguous heading: ' + heading
 start = candidates[0]
 assert lines[start] == '## ' + heading, 'noncanonical heading'
 stop = next((i for i in range(start + 1, len(lines))
    if re.match(r'^##\s', lines[i])), len(lines))
 return lines[start + 1:stop]


def preserve(text):
 baseline = json.loads(BASELINE.read_text())['sections']
 for heading, expected in baseline.items():
  actual = iter(section(text, heading))
  for line in expected:
   assert any(candidate == line for candidate in actual), (heading, line)


class TestU8Policy(unittest.TestCase):
 def test_M3_preserves_grants_bounds_stops_and_judgment(self):
  text = MANDATE.read_text()
  preserve(text)
  lines = [line for line in section(text, 'Adjudication policy') if line.strip()]
  self.assertEqual(hashlib.sha256('\n'.join(lines).encode()).hexdigest(),
      json.loads(BASELINE.read_text())['adjudication_sha256'])

 def test_M3_delete_old_bound_and_duplicate_heading_mutations(self):
  text = MANDATE.read_text()
  baseline = json.loads(BASELINE.read_text())['sections']
  for heading, lines in baseline.items():
   for line in lines:
    with self.subTest(heading=heading, deleted=line), self.assertRaises(AssertionError):
     preserve(text.replace(line, '', 1))
   with self.subTest(heading=heading), self.assertRaises(AssertionError):
    preserve(text + '\n## ' + heading + '  \n')

 def test_M4_normative_owner_fixed_mechanisms(self):
  text = MANDATE.read_text()
  for clause in ('native `CLAUDE_CODE_SESSION_ID`',
     "ancestor's PID and kernel start identity",
     'only after verified predecessor death',
     'There is no\nsupervisor wrapper',
     'issue time < expiry <= issue time + 24 hours',
     'The session cap is exactly one',
     'D5-OD1 means no new outward-action admissions',
     'Already-submitted effects may finish',
     'restoring old\nbytes does not resurrect it',
     'Only the OWNER merges',
     'P1 is a hard precondition',
     'before running linear_sync drain'):
   with self.subTest(clause=clause):
    self.assertIn(clause, text)


class TestU8Wiring(unittest.TestCase):
 def test_M6_positive_submission_uses_production_activation(self):
  from tests import test_d5_submission as t
  fixture = t.TestSubmission()
  self.addCleanup(fixture.doCleanups)
  fixture.setUp()
  # Real activation; synthetic clock, identity and submission.
  with mock.patch.object(registry, 'd5_active', PRODUCTION_ACTIVE):
   self.assertIs(registry.d5_active(), True)
   result = fixture.submit()
  self.assertEqual(result.outcome, t.OK)
  self.assertEqual([call[0] for call in fixture.boundary.calls], ['start', 'finish'])

 def test_M1_M6_default_production_guards_refuse_before_effects(self):
  self.assertIs(activation.d5_active(), True)
  self.assertIs(registry.d5_active(), True)
  self.assertIs(merge_unit.merge_launch_admitted(), False)
  for tool, names in (('swarm', S_ENTRIES), ('operator', M_ENTRIES)):
   for name in names:
    with self.subTest(tool=tool, entry=name), tempfile.TemporaryDirectory() as directory:
     fixture = SurfaceFixture(directory)
     with ExitStack() as stack:
      call = fixture.case(stack, tool, name)
      out = stack.enter_context(redirect_stdout(io.StringIO()))
      err = stack.enter_context(redirect_stderr(io.StringIO()))
      with no_effects() as effects:
       try:
        result = call()
       except (Exception, SystemExit) as exc:
        result = str(exc)
      self.assertEqual(effects, ([], []))
      self.assertIn('D5 active:', str(result) + out.getvalue() + err.getvalue())

 def test_M6_direct_roots_use_production_activation(self):
  from tests.test_d5_containment import cli_probe
  for tool, commands in {'unit': ('allocate', 'bind', 'check'),
       'remote_verify': ('worker', 'supervise')}.items():
   for command in commands:
    with self.subTest(tool=tool, command=command):
     result = cli_probe(tool, command, None)
     self.assertIn('D5 active:', str(result['outcome']))
     self.assertEqual(result['outcome'][0], 'SystemExit')
     for channel in ('launches', 'writes', 'transports'):
      self.assertEqual(result[channel], [], result)
     self.assertTrue(result['protected_unchanged'])

 def test_M6_outbox_helpers_and_merge_receipt_use_production_activation(self):
  from tests import test_d5_u6_tracker_reject as t
  import swarm_evidence
  for route in t.ENTRIES:
   with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
    case = t.OutboxFixture(tmp)
    before = case.tree()
    with no_effects() as effects:
     with self.assertRaisesRegex(t.SO.OutboxError, 'D5 active:'):
      case.call(route)
     self.assertEqual(effects, ([], []))
    self.assertEqual(case.tree(), before)
  with no_effects() as effects:
   with self.assertRaisesRegex(t.SO.OutboxError, 'D5 active:'):
    swarm_evidence.admit_merge('unused', 'u', 'a' * 40)
   self.assertEqual(effects, ([], []))

 def test_helper_silently_global_is_detected(self):
  # Import and use must restore the active default.
  import importlib
  from tests import d5_test_support as support
  importlib.reload(support)
  def child_active():
   code = ('import sys; sys.path.insert(0, sys.argv[1]); '
     'import d5_activation; print(d5_activation.d5_active())')
   result = subprocess.run([sys.executable, '-c', code,
    str(ROOT / 'skills/hanig-swarm/scripts')],
    capture_output=True, text=True, check=True, timeout=30)
   return result.stdout.strip()
  self.assertEqual(child_active(), 'True')
  self.assertIs(activation.d5_active(), True)
  with support.inactive():
   self.assertIs(activation.d5_active(), False)
   self.assertEqual(child_active(), 'False')
  self.assertEqual(child_active(), 'True')
  self.assertIs(activation.d5_active(), True)
  self.assertIs(registry.d5_active(), True)

 def test_child_exec_bootstrap_and_identity_restoration(self):
  import os
  from tests.d5_test_support import inactive
  originals = {name: getattr(os, name) for name in ('execv', 'execve')}
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   (root / 'd5_activation.py').write_bytes(Path(activation.__file__).read_bytes())
   target = root / 'exec_target.py'
   target.write_text('import d5_activation, json, os, subprocess, sys\n'
    'print(json.dumps([d5_activation.d5_active(), sys.argv, os.environ.get("FIXTURE_EXEC")]))\n'
    'command = subprocess.check_output(["/bin/ps","-p",str(os.getpid()),"-o","command="],text=True).strip()\n'
    'suffix = " " + globals().get("_d5_process_entry", sys.argv[0]) + " payload"\n'
    'assert command.endswith(suffix), command\nprint(command[:-len(suffix)])\n')
   for name in originals:
    code = ('import os,sys;os.' + name +
     '(sys.executable,["fixture-argv-zero",sys.argv[1],"payload"]' +
     (',dict(os.environ)' if name == 'execve' else '') + ')')
    with self.subTest(entry=name), mock.patch.dict(os.environ, {'FIXTURE_EXEC': 'retained'}):
     command = [sys.executable, '-c', code, str(target)]
     plain = subprocess.run(command, capture_output=True, text=True, timeout=30)
     self.assertEqual(plain.returncode, 0, plain.stderr)
     self.assertEqual(json.loads(plain.stdout.splitlines()[0]), [True, [str(target), 'payload'], 'retained'])
     with inactive():
      pinned = subprocess.run(command, capture_output=True, text=True, timeout=30)
     self.assertEqual(pinned.returncode, 0, pinned.stderr)
     self.assertEqual(json.loads(pinned.stdout.splitlines()[0]), [False, [str(target), 'payload'], 'retained'])
     self.assertEqual(pinned.stdout.splitlines()[1], plain.stdout.splitlines()[1])
     for entry, original in originals.items():
      self.assertIs(getattr(os, entry), original)
   self.assertEqual(sorted(p.name for p in root.iterdir() if p.is_file()),
                    ['d5_activation.py', 'exec_target.py'])
  self.assertIs(activation.d5_active(), True)

 def test_child_hook_keeps_its_file_and_refusal(self):
  import os
  from tests.d5_test_support import inactive
  for mode in ('absolute', 'relative', 'inherited', 'nested', 'package'):
   with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
    root = Path(tmp)
    cwd = root / 'child'
    for base, tag in ((root, 'parent'), (cwd, 'injected')):
     hook = base / 'hooks'
     hook.mkdir(parents=True)
     if mode == 'package':
      hook /= 'sitecustomize'
      hook.mkdir()
     (hook / 'flag').write_text(tag)
     (hook / ('__init__.py' if mode == 'package' else 'sitecustomize.py')).write_text(
      'from pathlib import Path\nimport os, subprocess\n'
      'os.environ["FIXTURE_FAULT"] = Path(__file__).with_name("flag").read_text()\n'
      'class Reject(subprocess.Popen):\n'
      ' def __init__(self, *a, **k): raise RuntimeError("unexpected spawn")\n'
      'subprocess.Popen = Reject\n')
    stack.callback(os.chdir, Path.cwd())
    os.chdir(root)
    effect = root / 'effect'
    write = f'from pathlib import Path; Path({str(effect)!r}).write_text("sent")'
    probe = ('import os,subprocess,sys; print(os.environ["FIXTURE_FAULT"]); '
        f'subprocess.run([sys.executable,"-c",{write!r}],check=True)')
    env = {'PYTHONPATH': 'hooks' if mode == 'relative' else str(cwd / 'hooks')}
    stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
    if mode == 'nested':
     stack.enter_context(inactive())
    child = stack.enter_context(inactive(environ=env if mode in ('absolute', 'relative') else None))
    for e in (env, child):
     r = subprocess.run([sys.executable, '-c', probe], cwd=cwd, env=e,
          capture_output=True, text=True, timeout=30)
     self.assertEqual(r.stdout.strip(), 'injected', r.stderr)
     self.assertNotEqual(r.returncode, 0)
     self.assertIn('unexpected spawn', r.stderr)
     self.assertFalse(effect.exists())

 def test_child_search_path_is_preserved(self):
  from tests.d5_test_support import inactive
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   (root / 'json.py').write_text('project_name = "example"\n')
   (root / 'tools').mkdir()
   script = root / 'tools/probe.py'
   script.write_text('import sys,json\nprint(sys.path)\nprint(json.dumps({"ok": True}))\n')
   for value in (None, ''):
    env = {} if value is None else {'PYTHONPATH': value}
    with self.subTest(value=value), inactive(environ=env) as child:
     results = [subprocess.run([sys.executable, str(script)], cwd=root, env=e,
      capture_output=True, text=True, timeout=30) for e in (env, child)]
     for r in results:
      self.assertEqual(r.returncode, 0, r.stderr)
      self.assertTrue(r.stdout.endswith('{"ok": true}\n'))
     self.assertEqual(results[0].stdout, results[1].stdout)

 def test_pin_preloaded_consumers_audit_reload_and_restoration(self):
  import importlib
  import types
  from tests import d5_test_support as support
  import swarm
  import swarm_evidence
  original, original_class, popen = activation.d5_active, type(activation), subprocess.Popen
  path, meta = list(sys.path), list(sys.meta_path)
  consumer = types.ModuleType('d5_uncovered_consumer')
  with mock.patch.dict(sys.modules, {consumer.__name__: consumer}):
   consumer.bound = original
   with self.assertRaisesRegex(AssertionError, 'd5_uncovered_consumer.bound'):
    with support.inactive():
     self.fail('uncovered reference admitted')
   del consumer.bound
   for fail in (False, True):
    try:
     with support.inactive():
      self.assertIs(swarm.D5, activation)
      self.assertIs(swarm.D5.d5_active(), False)
      importlib.reload(activation)
      with mock.patch.dict(vars(swarm_evidence)):
       importlib.reload(swarm_evidence)
       self.assertIs(swarm_evidence.D5.d5_active(), False)
      self.assertIs(swarm.D5.d5_active(), False)
      consumer.bound = activation.d5_active
      del consumer.bound
      with support.inactive():
       self.assertIs(swarm.D5.d5_active(), False)
      consumer.bound = activation.d5_active
      if fail:
       raise RuntimeError('fixture interruption')
    except RuntimeError:
     self.assertTrue(fail)
    self.assertIs(consumer.bound, original)
    del consumer.bound
    self.assertIs(activation.d5_active, original)
    self.assertIs(type(activation), original_class)
    self.assertIs(subprocess.Popen, popen)
    self.assertEqual(sys.path, path)
    self.assertEqual(sys.meta_path, meta)
   with mock.patch.object(support.inactive, '_install', return_value=None):
    with self.assertRaisesRegex(AssertionError, 'did not take effect'):
     with support.inactive():
      self.fail('no-op pin admitted')
  from tests.test_swarm_evidence import TestEvidenceExtraction
  TestEvidenceExtraction().test_every_moved_definition_is_reexported_identically()

 def test_preloaded_child_and_original_hook_survive_pin(self):
  import os
  import venv
  from tests.d5_test_support import inactive
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   venv.EnvBuilder(with_pip=False).create(root / 'venv')
   executable = str(root / 'venv/bin/python')
   env = dict(os.environ, PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
   env.pop('PYTHONHOME', None)
   env['PYTHONPATH'] = os.pathsep.join(str(p) for p in (
    ROOT, ROOT / 'skills/hanig-swarm/scripts', ROOT / 'skills/hanig-orchestrate/scripts'))
   site = subprocess.check_output([executable, '-c',
    'import sysconfig; print(sysconfig.get_paths()["purelib"])'], env=env, text=True, timeout=30).strip()
   preload = Path(site, 'd5-preload.pth')
   preload.write_text('import swarm\n')
   command = [executable, '-c', 'import json,swarm,d5_activation; print(json.dumps([swarm.D5.d5_active(),d5_activation.d5_active()]))']
   plain = subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=30)
   with inactive(environ=env) as child:
    wrapped = subprocess.run(command, env=child, capture_output=True, text=True, check=True, timeout=30)
   self.assertEqual(json.loads(plain.stdout), [True, True])
   self.assertEqual(json.loads(wrapped.stdout), [False, False])
   preload.unlink()
   hooks = root / 'hooks'
   hooks.mkdir()
   (hooks / 'importlib.py').write_text('project_marker = 1\n')
   (hooks / 'sitecustomize.py').write_text('import os\nos.environ["D5_HOOK_MARKER"] = "ok"\n')
   env['PYTHONPATH'] = str(hooks)
   env.pop('D5_HOOK_MARKER', None)
   command = [executable, '-c', 'import os; assert os.environ["D5_HOOK_MARKER"] == "ok"; print("ok")']
   plain = subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=30)
   with inactive(environ=env) as child:
    wrapped = subprocess.run(command, env=child, capture_output=True, text=True, check=True, timeout=30)
   self.assertEqual(plain.stdout, wrapped.stdout)
   self.assertEqual(wrapped.stdout.strip(), 'ok')
   script = root / 'probe.py'
   script.write_text(command[2] + '\n')
   command = [executable, str(script)]
   plain = subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=30)
   with inactive(environ=env) as child:
    wrapped = subprocess.run(command, env=child, capture_output=True, text=True, check=True, timeout=30)
   self.assertEqual(plain.stdout, wrapped.stdout)
   self.assertEqual(wrapped.stdout.strip(), 'ok')

 def test_child_script_bootstrap_preserves_context_and_grandchildren(self):
  from tests.d5_test_support import inactive
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   tools = root / 'tools'
   tools.mkdir()
   (tools / 'd5_activation.py').symlink_to(Path(activation.__file__))
   script = tools / 'probe.py'
   script.write_text('import d5_activation,json,os,sys\n'
    'print(json.dumps([d5_activation.d5_active(),sys.argv,__file__,os.getcwd(),sys.path]))\n')
   command = [sys.executable, 'tools/probe.py', 'argument with spaces']
   def run(argv=command):
    result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
    self.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout)
   plain = run()
   with inactive():
    pinned = run()
    with inactive():
     self.assertEqual(run(), pinned)
    grandchild = run([sys.executable, '-c',
     'import subprocess,sys; subprocess.run([sys.executable,"tools/probe.py",'
     '"argument with spaces"],check=True)'])
    self.assertEqual(grandchild, pinned)
   self.assertEqual(run(), plain)
   self.assertIs(plain[0], True)
   self.assertIs(pinned[0], False)
   self.assertEqual(pinned[1], plain[1])
   self.assertEqual(pinned[2:], plain[2:])
   self.assertEqual(list(tools.glob('.d5-test-*')), [])

 def test_child_audit_refuses_uncovered_startup_reference(self):
  from tests.d5_test_support import inactive
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   (root / 'd5_activation.py').symlink_to(Path(activation.__file__))
   (root / 'sitecustomize.py').write_text('from d5_activation import d5_active as bound\n')
   effect = root / 'effect'
   script = root / 'probe.py'
   script.write_text('from pathlib import Path\nPath(' + repr(str(effect)) + ').touch()\n')
   with inactive():
    result = subprocess.run([sys.executable, str(script)],
     env={'PYTHONPATH': str(root)}, capture_output=True, text=True, timeout=30)
   self.assertNotEqual(result.returncode, 0)
   self.assertIn('uncovered D5 reference: sitecustomize.bound', result.stderr)
   self.assertFalse(effect.exists())

 def test_bootstrap_marker_is_not_inactive_evidence(self):
  from tests.d5_test_support import inactive
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   (root / 'd5_activation.py').symlink_to(Path(activation.__file__))
   script = root / '.d5-test-peer.py'
   code = 'import d5_activation; print(d5_activation.d5_active())'
   script.write_text(code + '\n')
   for command in ([sys.executable, str(script)],
       [sys.executable, '-c', '# d5-test-bootstrap\n' + code]):
    with self.subTest(command=command):
     with inactive():
      result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
     self.assertEqual(result.returncode, 0, result.stderr)
     self.assertEqual(result.stdout.strip(), 'False')
     plain = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
     self.assertEqual(plain.returncode, 0, plain.stderr)
     self.assertEqual(plain.stdout.strip(), 'True')

 def test_child_entry_hook_retains_script_suffix(self):
  from tests.d5_test_support import inactive
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   (root / 'sitecustomize.py').write_text('import os,sys\n'
    'if sys.argv[0].endswith("probe.py"): os.environ["FIXTURE_ENTRY"]="seen"\n')
   script = root / 'probe.py'
   script.write_text('import os; assert os.environ.get("FIXTURE_ENTRY")=="seen"\n')
   command, env = [sys.executable, str(script)], {'PYTHONPATH': str(root)}
   plain = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
   with inactive():
    wrapped = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
   self.assertEqual(plain.returncode, 0, plain.stderr)
   self.assertEqual(wrapped.returncode, 0, wrapped.stderr)

 def test_M5_replacement_needs_fresh_confirmation(self):
  fixture = lease_fixture.TestD5Lease()
  self.addCleanup(fixture.doCleanups)
  fixture.setUp()
  entry = ready(fixture, fixture.repo, 'p', fixture.state)
  first = fixture.grant()
  before = fixture.tree()
  for changes in ({}, {'expires_at': 201},
      {'owner_response': 'copied receipt with changed words'}):
   with self.subTest(changes=changes), self.assertRaisesRegex(
     authority.AuthorityError, 'fresh confirmation'):
    fixture.grant(**changes)
   self.assertEqual(fixture.tree(), before)
  second = fixture.grant(confirmed_at=102, expires_at=201,
       owner_response='Fresh confirmation\n')
  self.assertNotEqual(first['grant_id'], second['grant_id'])
  self.assertEqual(store.read(Path(entry['record_dir'])), second)
  self.assertEqual(second['confirmation']['owner_response'], 'Fresh confirmation\n')

 def test_M5_no_grant_from_conversational_receipt(self):
  fixture = lease_fixture.TestD5Lease()
  self.addCleanup(fixture.doCleanups)
  fixture.setUp()
  entry = ready(fixture, fixture.repo, 'p', fixture.state)
  directory = Path(entry['record_dir'])
  # Required fields reach the real API/schema before installation.
  for missing in ('expires_at', 'owner_response', 'displayed_bounds'):
   args = dict(fixture.args)
   del args[missing]
   with self.subTest(missing=missing), self.assertRaises(TypeError):
    lease.grant(fixture.repo, 'p', fixture.state, **args)
   self.assertFalse((directory / store.RECORD_NAME).exists())
  for changes in ({'owner_response': ''}, {'displayed_bounds': ''}, {'owner_response': ' \t\n'},
      {'displayed_bounds': ' \t\n'},
      {'expires_at': 100 + 86401}):
   with self.subTest(changes=changes), self.assertRaises(authority.AuthorityError):
    fixture.grant(**changes)
   self.assertFalse((directory / store.RECORD_NAME).exists())
  record = fixture.grant(expires_at=100 + 86400)
  self.assertEqual(store.read(directory), record)
  self.assertEqual(record['session_cap'], 1)
  self.assertEqual(record['confirmation']['owner_response'], fixture.args['owner_response'])
  self.assertEqual(authority.evaluate(record, 150, b'old mandate')[0], 'mandate_changed')


if __name__ == '__main__':
 unittest.main()
