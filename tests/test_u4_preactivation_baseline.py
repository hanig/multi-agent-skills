"""Pinned pre-U4 oracle: synthetic services, real state/plan/merge recording.
No production helper supplies expectations; no production-readiness claim.
Requires Git object 276e76af05a83846ea9c45ae7d030cf11a2e4e94.
Release CI uses fetch-depth: 0 (.github/workflows/release-validation.yml:30,65).
If the object is missing, recover with:
git fetch origin 276e76af05a83846ea9c45ae7d030cf11a2e4e94
Missing history is an error, never a skipped baseline.

Supported envelope and accepted limits
This prevents competing sessions from initiating operations. It does not
prevent planning mistakes by the valid holder or replace existing crash
reconciliation.

Oracle scope: the deterministic scenarios below, with synthetic Git/Paseo and
verifier services. State writers, launch/record/seed helpers and preservation
execute from each isolated tree. Directory inode inputs are fixed before
execution, not removed from recorded outputs. This is an M6 oracle, not H
enforcement, exhaustive branch coverage, or production-spawn certification.
"""
import ast
import contextlib
import datetime
import hashlib
import fcntl
from argparse import Namespace
import importlib.abc
import importlib.machinery
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock
BASE = '276e76af05a83846ea9c45ae7d030cf11a2e4e94'
ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SCENARIOS = ('run-ok', 'run-fail', 'run-noop', 'run-dry', 'advance-ok', 'advance-fail', 'advance-noop', 'advance-dry', 'continue-write', 'continue-preview', 'continue-fail', 'abandon-ok', 'abandon-repeat', 'abandon-fail', 'supersede-ok', 'supersede-repeat', 'supersede-fail', 'merge-ok', 'merge-merged', 'merge-dry', 'merge-fail', 'merge-pending', 'merge-cancel', 'merge-abandon', 'pipeline-ok', 'pipeline-fail', 'record-ok', 'record-fail',
  'code-ok', 'code-fail', 'code-dry', 'watch-start', 'watch-repeat',
  'watch-ok', 'watch-fail', 'watch-coordinator-fail',
  'resume-ok', 'resume-fail', 'resume-backoff', 'resume-disabled',
  'verify-ok', 'verify-fail', 'verify-refuse', 'verify-checkout')

class TestPreactivation(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    raw = subprocess.run(['git', '-C', str(ROOT), 'archive', BASE, 'skills'], check=True, capture_output=True, timeout=60).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
      cls.base_files = {m.name: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}
    cls.current_files = {str(p.relative_to(ROOT)): p.read_bytes() for p in (ROOT / 'skills').rglob('*') if p.is_file() and '__pycache__' not in p.parts}

  def pair(self, scenario, mutation=None):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp).resolve()
      results = []
      for (side, files) in (('oracle', self.base_files), ('candidate', self.current_files)):
        (source, work) = (root / 'source', root / 'work')
        for directory in (source, work):
          if directory.exists():
            shutil.rmtree(directory)
          directory.mkdir()
        for (name, data) in files.items():
          path = source / name
          path.parent.mkdir(parents=True, exist_ok=True)
          path.write_bytes(data)
        hashes = {name: hashlib.sha256(data).hexdigest() for (name, data) in self.base_files.items()}
        (root / 'pins.json').write_text(json.dumps(hashes))
        if mutation and side == 'candidate':
          name = mutation[0] if isinstance(mutation, tuple) else 'swarm_preflight.py' if mutation == 'baseline-drop-seed' else 'swarm.py'
          path = source / 'skills/hanig-swarm/scripts' / name
          data = path.read_bytes()
          tree = ast.parse(data)
          if isinstance(mutation, tuple):
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == mutation[1])
            replacement = 'def ' + node.name + '(*a, **kw): return ' + mutation[2]
          elif mutation == 'baseline-drop-seed':
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_seed_reachability_problem')
            replacement = 'def _seed_reachability_problem(*args): return None'
          elif mutation == 'baseline-drop-subprocess':
            nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'run' and n.args and isinstance(n.args[0], ast.List) and any(isinstance(x, ast.Constant) and x.value == 'sbatch' for x in n.args[0].elts)]
            self.assertEqual(len(nodes), 1)
            node, replacement = nodes[0], '(0, "123", "")'
          else:
            nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Dict) and {'merged', 'attested'} <= {x.value for x in n.keys if isinstance(x, ast.Constant)}]
            self.assertEqual(len(nodes), 1)
            node = nodes[0]
            node.keys.append(ast.Constant('unexpected'))
            node.values.append(ast.Constant(True))
            replacement = ast.unparse(node)
          lines = data.splitlines(keepends=True)
          start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
          end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
          path.write_bytes(data[:start] + replacement.encode() + data[end:])
        if mutation == 'oracle-uses-new-helper' and side == 'oracle':
          scripts = source / 'skills/hanig-swarm/scripts'
          (scripts / 'u4_unpinned_helper.py').write_text('VALUE = 1\n')
          with (scripts / 'swarm.py').open('a') as stream:
            stream.write('\nimport u4_unpinned_helper\n')
        env = {'PATH': os.defpath, 'HOME': str(work / 'home'), 'USER': 'fixture', 'TZ': 'UTC', 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, str(SELF), '--probe', str(root), side, scenario], env=env, cwd=str(work), capture_output=True, timeout=60)
        if mutation == 'oracle-uses-new-helper' and side == 'oracle':
          self.assertNotEqual(result.returncode, 0)
          self.assertIn(b'unpinned oracle import', result.stderr)
          return
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        results.append(json.loads(result.stdout))
      return results

  def test_inactive_matrix(self):
    for scenario in SCENARIOS:
      with self.subTest(scenario=scenario):
        (before, after) = self.pair(scenario)
        if scenario.startswith('merge-'):
          expected = 2 if scenario == 'merge-dry' else 1 if scenario in ('merge-fail', 'merge-pending', 'merge-cancel') else 0
          self.assertEqual(before['result'], expected, before)
        if scenario in ('continue-write', 'continue-preview'):
          self.assertEqual(before['result'][0], 0, before)
        if scenario.startswith('code-'):
          self.assertEqual(before['result'][1], None if scenario != 'code-fail' else 'paseo run failed: launch refused', before)
          if scenario == 'code-ok':
            self.assertEqual(before['result'][0], 'agent-a1', before)
        if scenario.startswith('watch-'):
          expected = ([True, False] if scenario == 'watch-repeat' else [True]
                if scenario == 'watch-start' else
                0 if scenario == 'watch-ok' else 1)
          actual = before['result'][:-1] if isinstance(expected, list) else before['result']
          self.assertEqual(actual, expected, before)
        if scenario.startswith('resume-'):
          self.assertEqual(before['result'][0], scenario in ('resume-ok', 'resume-backoff'), before)
          sends = [c for c in before['calls'] if c[0] == 'Popen' and c[1][:2] == ['paseo', 'send']]
          self.assertEqual(len(sends), int(scenario in ('resume-ok', 'resume-fail')), before)
          if sends:
            self.assertTrue(any('recovery-snapshots/' in f for f in before['files']), before)
        if scenario.startswith('verify-'):
          if scenario == 'verify-refuse':
            self.assertIsNone(before['result'][0], before)
            self.assertIn('expected', before['result'][1], before)
          else:
            self.assertEqual(before['result'][0]['exit_code'], int(scenario == 'verify-fail'), before)
            self.assertEqual(before['files']['verified'], b'verifier effect\n'.hex())
        self.assertEqual(before, after)

  def test_oracle_uses_new_helper(self):
    self.pair('run-ok', 'oracle-uses-new-helper')

  def test_baseline_drop_subprocess(self):
    (before, after) = self.pair('run-ok', 'baseline-drop-subprocess')
    self.assertNotEqual(before['calls'], after['calls'])

  def test_baseline_change_record_bytes(self):
    (before, after) = self.pair('record-ok', 'baseline-change-record-bytes')
    self.assertNotEqual(before['files'], after['files'])


  def test_added_family_sensitivity(self):
    for scenario, module, function, value in (
        ('code-ok', 'swarm_code_launch', '_create_code_worktree', '(None, None, "stopped")'),
        ('watch-ok', 'swarm', 'cmd_watch_code_terminal', '0'),
        ('resume-ok', 'swarm_provider_resume', 'maybe_resume', 'False'),
        ('verify-ok', 'verify', 'run_pinned', '({}, None)')):
      with self.subTest(scenario=scenario):
        a, b = self.pair(scenario, (module + '.py', function, value))
        self.assertNotEqual(a['calls'], b['calls'])

  def test_comment_only_candidate(self):
    name = 'skills/hanig-swarm/scripts/swarm.py'
    files = dict(self.current_files)
    lines = files[name].decode().splitlines(keepends=True)
    lines.insert(1, '# Receipt fields: "merged": True, "attested": True,\n')
    files[name] = ''.join(lines).encode()
    self.current_files = files
    before, after = self.pair('record-ok')
    self.assertEqual(before, after)
    self.test_baseline_change_record_bytes()

  def test_seed_effect_sensitivity(self):
    for mode in ('write', 'preview'):
      before, after = self.pair('continue-' + mode, 'baseline-drop-seed')
      self.assertNotEqual(before['calls'], after['calls'])


def probe(root, side, scenario):
  """Test driver only; all imported repository code comes from one materialization."""
  (source, work) = (root / 'source', root / 'work')
  scripts = source / 'skills/hanig-swarm/scripts'
  operator = source / 'skills/hanig-orchestrate/scripts'
  sys.path[:] = [str(scripts), str(operator)] + [p for p in sys.path if p and (not Path(p).resolve().is_relative_to(ROOT))]
  pins = json.loads((root / 'pins.json').read_text())

  class ImportFence(importlib.abc.MetaPathFinder):

    def find_spec(self, fullname, path=None, target=None):
      spec = importlib.machinery.PathFinder.find_spec(fullname, path)
      if side == 'oracle' and spec and spec.origin and spec.origin.endswith('.py'):
        p = Path(spec.origin).resolve()
        if p.is_relative_to(source):
          name = str(p.relative_to(source))
          if name not in pins:
            raise AssertionError('unpinned oracle import: ' + name)
      return None
  sys.meta_path.insert(0, ImportFence())
  calls = []
  (stdout, stderr) = (io.StringIO(), io.StringIO())
  original_strftime = time.strftime

  class Clock(datetime.datetime):

    @classmethod
    def now(cls, tz=None):
      return cls.fromtimestamp(1700000000, tz)

  def encode(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
      return value
    if isinstance(value, bytes):
      return {'bytes': value.hex()}
    if isinstance(value, (list, tuple)):
      return [encode(x) for x in value]
    if isinstance(value, dict):
      return {k: encode(v) for (k, v) in value.items()}
    if isinstance(value, Path):
      return str(value)
    if hasattr(value, 'name'):
      return {'stream': str(value.name)}
    raise AssertionError('uncaptured argument: ' + str(type(value)))

  def call_cli(module, argv):
    (out, err) = (io.StringIO(), io.StringIO())
    with mock.patch.object(sys, 'argv', [module.__file__] + argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
      try:
        rc = module.main()
      except SystemExit as exc:
        rc = exc.code
        if isinstance(rc, str):
          print(rc, file=err)
          rc = 1
    return (rc or 0, out.getvalue(), err.getvalue())
  merged = scenario == 'merge-merged'
  code_family = scenario.startswith(('code-', 'resume-'))
  workspace = work / 'state/code-worktrees/a1'
  common = work / 'repo/.git'
  metadata = common / 'worktrees/a1'


  def respond(argv):
    nonlocal merged
    argv = list(map(str, argv))
    if len(argv) > 2 and argv[1] == str(scripts / 'unit.py'):
      return call_cli(S.U, argv[2:])
    if len(argv) > 2 and argv[1] == str(scripts / 'swarm.py'):
      if argv[2] == 'merge':
        return call_cli(S, argv[2:])
      if argv[2] == 'scope-check':
        return (0, json.dumps(scope), '')
      if argv[2] == 'watch-code-terminal':
        return (0, '', '')
      if argv[2] == 'advance-code-terminal':
        return (1, '', 'coordinator busy') if scenario == 'watch-coordinator-fail' else (0, 'checked\n', '')
      if argv[2] == 'advance':
        return (0, 'advanced\n', '')
    if argv[0] == 'sbatch':
      return (1, '', 'submission refused') if scenario.endswith('fail') else (0, '123\n', '')
    if argv[0] == 'sh':
      if scenario.endswith('fail'):
        raise OSError('synthetic launch failure')
      return (0, '', '')
    if argv[:2] == ['gh', 'pr']:
      if argv[2] == 'merge':
        merged = True
        return (0, '', '')
      if argv[2] == 'checks':
        return (0, json.dumps([{'state': 'FAILURE' if scenario == 'merge-fail' else 'SUCCESS'}]), '')
      pr = dict(number=7, url='https://github.com/example/project/pull/7', state='MERGED' if merged else 'OPEN', headRefOid='a' * 40, baseRefName='main', mergeCommit={'oid': 'b' * 40} if merged else None)
      return (0, json.dumps(pr), '')
    if argv[:2] == ['gh', 'api']:
      if '/git/ref/' in argv[-1]:
        target = 'd' if scenario == 'merge-cancel' and any(state.glob('merge-unit-*.json')) else 'c'
        return (0, json.dumps({'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': target * 40}}), '')
      return (0, json.dumps({'sha': 'b' * 40, 'parents': [{'sha': 'c' * 40}]}), '')
    if argv[0] == 'paseo':
      command = argv[1]
      if command == 'run':
        return ((1, '', 'launch refused') if scenario == 'code-fail' else
            (0, json.dumps(dict(agentId='agent-a1', cwd=str(workspace))), ''))
      if command == 'wait':
        return (1, '', 'wait failed') if scenario == 'watch-fail' else (0, 'idle', '')
      if command == 'inspect':
        return (0, '{"Status":"error"}', '')
      if command == 'logs':
        return (0, '[System Error] provider is at capacity', '')
      if command == 'send':
        return (1, '', 'send refused') if scenario == 'resume-fail' else (0, 'sent', '')
    if 'pinned-verifier-' in argv[0]:
      (work / 'verified').write_bytes(b'verifier effect\n')
      return (1 if scenario == 'verify-fail' else 0, 'verified\n', 'diagnostic\n')
    if argv[0] == 'git' and code_family:
      cwd = Path(argv[2]) if argv[1] == '-C' else work / 'repo'
      if 'config' in argv:
        if argv[-1] == 'remote.origin.pushurl':
          return (1, '', '')
        return (0, 'example/project\0' if argv[-1] == 'remote.origin.url' else '', '')
      if 'remote' in argv:
        return (0, 'example/project', '')
      if 'ls-remote' in argv:
        return ((0, 'c' * 40 + '\trefs/heads/main\n', '')
            if argv[-1] == 'refs/heads/main' else (2, '', ''))
      if 'show-ref' in argv:
        return (1, '', '')
      if 'stash' in argv or 'merge-base' in argv:
        return (0, '', '')
      if 'symbolic-ref' in argv:
        return (0, 'refs/heads/main', '')
      if 'worktree' in argv and 'add' in argv:
        workspace.mkdir(parents=True)
        metadata.mkdir(parents=True)
        (workspace / '.git').write_text('gitdir: ' + str(metadata) + '\n')
        (workspace / 'tracked.txt').write_text('base\n')
        return (0, '', '')
      if 'rev-parse' in argv:
        value = (str(cwd) if '--show-toplevel' in argv else
            str(common) if '--git-common-dir' in argv else
            str(metadata) if '--git-dir' in argv else
            'swarm-a1' if '--abbrev-ref' in argv else
            'e' * 40 if argv[-1].endswith('^{tree}') else 'c' * 40)
        return (0, value, '')
    if argv[0] == 'git' and scenario == 'verify-checkout':
      if 'add' in argv:
        Path(argv[-2]).mkdir()
      elif 'remove' in argv:
        shutil.rmtree(argv[-1])
      else:
        raise AssertionError(argv)
      return (0, '', '')
    if argv[0] == 'git':
      if scenario.startswith('continue'):
        if any(x in argv for x in ('config', 'fetch', 'update-ref', '--is-ancestor')):
          return (0, '', '')
        if 'cat-file' in argv:
          return (0, 'commit\n', '')
        if 'symbolic-ref' in argv:
          return (0, 'refs/heads/main\n', '')
        if 'merge-base' in argv:
          return (0, 'c' * 40, '')
        if 'rev-parse' in argv and '--show-toplevel' not in argv:
          return (0, argv[-1] if '--verify' in argv else 'c' * 40, '')
      return (1, '', 'not a git repository')
    raise AssertionError('unexpected external command: ' + repr(argv))

  class Process:

    def __init__(self, argv, **kwargs):
      calls.append(['Popen', encode(argv), encode(kwargs)])
      self.pid = 4242
      (self.returncode, self.out, self.err) = respond(argv)
      self.text = kwargs.get('encoding') or kwargs.get('text')

    def communicate(self, **kwargs):
      calls.append(['communicate', encode(kwargs)])
      return (self.out, self.err) if self.text else (self.out.encode(), self.err.encode())

  def run(argv, **kwargs):
    calls.append(['run', encode(argv), encode(kwargs)])
    (rc, out, err) = respond(argv)
    if not (kwargs.get('encoding') or kwargs.get('text')):
      (out, err) = (out.encode(), err.encode())
    return subprocess.CompletedProcess(argv, rc, out, err)

  def deny(*args, **kwargs):
    raise AssertionError('inactive path performed D5 I/O')
  real_stat, real_flock = os.stat, fcntl.flock

  def fixture_stat(path, *args, **kwargs):
    info = real_stat(path, *args, **kwargs)
    # Only the three declared synthetic directory identities are controlled.
    identities = {str(workspace): 101, str(common): 102, str(metadata): 103}
    if str(path) in identities:
      fields = list(info)
      fields[1:3] = [identities[str(path)], 7]
      return os.stat_result(fields)
    return info

  def flock(fd, operation):
    calls.append(['flock', operation])
    return real_flock(fd, operation)

  with contextlib.ExitStack() as stack:
    patch = lambda *a, **kw: stack.enter_context(mock.patch(*a, **kw))
    patch('time.time', return_value=1700000000.0)
    patch('time.strftime', side_effect=lambda fmt, when=None: original_strftime(fmt, time.gmtime(1700000000) if when is None else when))
    patch('datetime.datetime', Clock)
    patch('os.getpid', return_value=4242)
    patch('os.urandom', side_effect=lambda n: b'\x01' * n)
    (work / 'tmp').mkdir()
    patch('tempfile.tempdir', str(work / 'tmp'))
    patch('tempfile._RandomNameSequence.__next__', side_effect=(str(n).zfill(8) for n in range(1000)))
    patch('os.stat', side_effect=fixture_stat)
    patch('fcntl.flock', side_effect=flock)
    patch('subprocess.Popen', Process)
    patch('subprocess.run', run)
    import d5_registry
    import d5_protocol_locks
    import d5_completion
    import d5_submission
    import d5_process
    import d5_control
    import d5_authority_store
    assert d5_registry.d5_active() is False
    for (module, names) in ((d5_registry, ('lookup', 'register')), (d5_protocol_locks, ('pin',)), (d5_completion, ('bind',)), (d5_submission, ('submit',)), (d5_process, ('protected_spawn', 'worker_spawn')), (d5_control, ('_mandate_bytes',)), (d5_authority_store, ('read',))):
      for name in names:
        patch(module.__name__ + '.' + name, side_effect=deny)
    import swarm as S
    import merge_unit as M
    bindir = work / 'bin'
    bindir.mkdir()
    (bindir / 'paseo').write_text('#!/bin/sh\nexit 99\n')
    (bindir / 'paseo').chmod(493)
    os.environ['PATH'] = str(bindir) + os.pathsep + os.defpath
    (state, runs) = (work / 'state', work / 'runs')
    state.mkdir()
    unit = dict(id='u', kind='slurm', command='true', runtime='none', outputs=['out'])
    plan = dict(name='baseline', units=[unit])
    plan_path = work / 'plan.json'
    args = ['--state-dir', str(state), '--root', str(runs)]
    head = 'a' * 40
    receipt = dict(unit='next', repo='example/project', pr='https://github.com/example/project/pull/7', target='main', head=head, merged_as='b' * 40, method='squash', merged=True, attested=True)
    record = dict(schema_version=1, units={}, root=str(runs), plan_digest=S.plan_digest(plan))
    if scenario.startswith(('abandon', 'supersede')):
      plan['units'].append(dict(unit, id='next'))
      record['plan_digest'] = S.plan_digest(plan)
      record['units'] = {'u': dict(state='FAILED', attempts=[], gpu_hours=0), 'next': dict(state='DONE', attempts=[], gpu_hours=0, attempt_dir=str(runs / 'next/a1'), attempt_produced_heads={'a1': head}, merge_receipt=receipt, merge_pr=receipt['pr'], merged_as=receipt['merged_as'])}
    if scenario.startswith('continue'):
      unit.update(kind='code', repo=str(work / 'repo'), target_branch='main', mode='full-access', prompt='old')
      unit.pop('command')
      unit.pop('runtime')
      intent = dict(schema_version=5, unit_id='u', attempt_id='a1', repo=unit['repo'], base_commit='c' * 40, base_tree='e' * 40, repository_remote='example/project', repository_remote_raw='example/project', branch='swarm-a1', target_branch='main', judgment_ref='refs/heads/swarm-a1', worktree_slug='a1', launch_host='fixture', captured_at='fixture')
      record['units']['u'] = dict(attempt_dir=str(runs / 'u/a1'), attempt_launch_intents={'a1': intent}, attempt_produced_heads={'a1': head})
      target = dict(repo=unit['repo'], repository_remote='example/project', repository_remote_raw='example/project', target_commit='c' * 40, target_ref='refs/heads/main')
      patch('swarm_preflight._resolve_dispatch_target', return_value=(target, None))
      (work / 'prompt').write_bytes(b'new\r\n  prompt\t\n')
    plan_path.write_text(json.dumps(plan))
    (state / S.STATE_FILE).write_text(json.dumps(record))
    binding = dict(unit='u', attempt='a1', pr=7, head=head, target='main', repo='https://github.com/example/project')
    scope = dict(binding, base='c' * 40, state_epoch=1, head_source='judged', status='in_scope', scope=['*'], out_of_scope=[], deletions_out_of_scope=[])
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
      if code_family:
        common.mkdir(parents=True)
        attempt = runs / 'u/a1'
        attempt.mkdir(parents=True)
        unit = dict(id='u', kind='code', repo=str(work / 'repo'),
              target_branch='main', prompt='work', mode='full-access',
              provider='claude', outputs=['evidence.md'])
        job, error = S._submit(unit, str(attempt), scenario == 'code-dry',
                   record, str(state), plan_path=str(plan_path))
        result = (job, error)
        if scenario.startswith('resume-'):
          assert job == 'agent-a1' and error is None, result
          us = record['units']['u']
          us.update(attempt_dir=str(attempt), job_id=job, state='FAILED')
          unit['provider_resume'] = {'max': 0 if scenario == 'resume-disabled' else 2}
          if scenario != 'resume-backoff':
            us['provider_resumes'] = {'a1': dict(
              fingerprint={'message_class': 'is at capacity', 'observed_at': 'fixture'},
              resumes_attempted=0, next_not_before=0, snapshot_paths=[])}
          (workspace / 'tracked.txt').write_bytes(b'changed\r\n')
          report = []
          result = (S.SPRS.maybe_resume(str(state), 'u', unit, us, report, record), report)
      elif scenario.startswith('watch-'):
        attempt = runs / 'u/a1'
        attempt.mkdir(parents=True)
        watch_args = Namespace(plan=str(plan_path), state_dir=str(state),
                   root=str(runs), unit='u', attempt='a1', agent='agent-a1')
        if scenario in ('watch-start', 'watch-repeat'):
          unit['kind'] = 'code'
          record['units']['u'] = dict(state='RUNNING', attempt_dir=str(attempt), job_id='agent-a1')
          report = []
          result = [S._start_code_terminal_watchers(plan, record, watch_args, report)]
          if scenario == 'watch-repeat':
            result.append(S._start_code_terminal_watchers(plan, record, watch_args, report))
          result.append(report)
        else:
          result = S.cmd_watch_code_terminal(watch_args)
      elif scenario.startswith('verify-'):
        import verify as V
        script = work / 'check.sh'
        script.write_bytes(b'#!/bin/sh\nprintf verified\n')
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        if scenario == 'verify-refuse':
          digest = '0' * 64
        if scenario == 'verify-checkout':
          result = V.run_in_checkout(S.U.run, str(work / 'repo'), 'c' * 40,
                       str(script), digest, args=['fixture'], timeout=10)
        else:
          result = V.run_pinned(S.U.run, str(script), digest, args=['fixture'],
                     timeout=10, cwd=str(work), observe_completion=True)
      elif scenario.startswith(('run-', 'advance-')):
        (command, mode) = scenario.split('-')
        extra = ['--dry-run'] if mode == 'dry' else []
        if mode == 'noop':
          extra += ['--max-new-dispatches', '0']
        result = call_cli(S, [command, str(plan_path)] + args + extra)
      elif scenario.startswith(('abandon-', 'supersede-')):
        (command, mode) = scenario.split('-')
        argv = [command, str(plan_path), '--state-dir', str(state), '--unit', 'u', '--approver', '' if mode == 'fail' else 'Owner', '--reason', 'retired']
        if command == 'supersede':
          argv += ['--by', 'next']
        result = [call_cli(S, argv)]
        if mode == 'repeat':
          result.append(call_cli(S, argv))
      elif scenario.startswith('continue-'):
        result = call_cli(S, ['continue', str(plan_path), '--state-dir', str(state), '--unit', 'u', '--new-id', 'u' if scenario.endswith('fail') else 'next', '--prompt-file', str(work / 'prompt')] + (['--write'] if scenario.endswith('write') else []))
      elif scenario.startswith('pipeline-'):
        attempt = runs / 'u/a1'
        attempt.mkdir(parents=True)
        result = S._submit(dict(unit, kind='pipeline'), str(attempt), False, record, str(state))
      elif scenario.startswith('record-'):
        result = call_cli(S, ['merge', '--state-dir', str(state), '--unit', 'u', '--repo', binding['repo'], '--pr', receipt['pr'], '--target', 'main', '--head', 'bad' if scenario.endswith('fail') else head, '--merged-as', 'b' * 40, '--method', 'squash'])
      else:
        record['units']['u'] = {}
        (state / S.STATE_FILE).write_text(json.dumps(record))
        patch('merge_unit.authority', return_value=(state, str(runs), binding, 'github.com', 'example/project', binding, str(work / 'repo')))
        evidence = dict(target_commit='c' * 40, subject_head=head, result='pass')
        patch('merge_unit.integration_evidence', return_value=(evidence, None))
        patch('merge_unit.retained_integration_problem', return_value=None)
        if scenario in ('merge-pending', 'merge-abandon'):
          (operation, path, _) = M.current_intent(state, binding, str(runs))
          M.durable_write(path, dict(schema_version=1, operation_id=operation, binding=binding, root=str(runs), phase='merge_requested'))
        extra = ['--dry-run'] if scenario == 'merge-dry' else []
        if scenario == 'merge-abandon':
          extra += ['--abandon-intent', operation, '--reason', 'retired']
        result = M.main([str(plan_path), '--state-dir', str(state), '--unit', 'u', '--pr', '7', '--approver', 'Owner'] + extra)
    files = {str(p.relative_to(work)): p.read_bytes().hex() for p in sorted(work.rglob('*')) if p.is_file()}
    if side == 'oracle':
      for module in list(sys.modules.values()):
        path = getattr(module, '__file__', None)
        if path and Path(path).is_relative_to(source):
          name = str(Path(path).relative_to(source))
          if name.endswith('.py'):
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == pins[name], name
    return dict(result=encode(result), stdout=stdout.getvalue().encode().hex(), stderr=stderr.getvalue().encode().hex(), calls=calls, files=files)
if __name__ == '__main__':
  if len(sys.argv) > 1 and sys.argv[1] == '--probe':
    print(json.dumps(probe(Path(sys.argv[2]), sys.argv[3], sys.argv[4]), sort_keys=True))
  else:
    unittest.main()
