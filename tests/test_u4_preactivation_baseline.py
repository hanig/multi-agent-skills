"""Pinned pre-U4 oracle: synthetic services, real state/plan/merge recording.
No production helper supplies expectations; no production-readiness claim.
"""
import contextlib
import datetime
import hashlib
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
SCENARIOS = ('run-ok', 'run-fail', 'run-noop', 'run-dry', 'advance-ok', 'advance-fail', 'advance-noop', 'advance-dry', 'continue-write', 'continue-preview', 'continue-fail', 'abandon-ok', 'abandon-repeat', 'abandon-fail', 'supersede-ok', 'supersede-repeat', 'supersede-fail', 'merge-ok', 'merge-merged', 'merge-dry', 'merge-fail', 'merge-pending', 'merge-cancel', 'merge-abandon', 'pipeline-ok', 'pipeline-fail', 'record-ok', 'record-fail')

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
                    path = source / 'skills/hanig-swarm/scripts/swarm.py'
                    text = path.read_text()
                    if mutation == 'baseline-drop-subprocess':
                        old = 'rc, out, err = U.run(["sbatch", "--parsable", str(script)],\n                             cwd=str(unit_dir), timeout=120)'
                        new = 'rc, out, err = 0, "123", ""'
                    else:
                        old = '"merged": True, "attested": True,'
                        new = '"merged": True, "attested": True, "unexpected": True,'
                    self.assertIn(old, text)
                    path.write_text(text.replace(old, new, 1))
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
                self.assertEqual(before, after)

    def test_oracle_uses_new_helper(self):
        self.pair('run-ok', 'oracle-uses-new-helper')

    def test_baseline_drop_subprocess(self):
        (before, after) = self.pair('run-ok', 'baseline-drop-subprocess')
        self.assertNotEqual(before['calls'], after['calls'])

    def test_baseline_change_record_bytes(self):
        (before, after) = self.pair('record-ok', 'baseline-change-record-bytes')
        self.assertNotEqual(before['files'], after['files'])

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
        if argv[0] == 'git':
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
    with contextlib.ExitStack() as stack:
        patch = lambda *a, **kw: stack.enter_context(mock.patch(*a, **kw))
        patch('time.time', return_value=1700000000.0)
        patch('time.strftime', side_effect=lambda fmt, when=None: original_strftime(fmt, time.gmtime(1700000000) if when is None else when))
        patch('datetime.datetime', Clock)
        patch('os.getpid', return_value=4242)
        patch('os.urandom', side_effect=lambda n: b'\x01' * n)
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
            patch('swarm_preflight._seed_reachability_problem', return_value=None)
            patch('swarm_preflight._git', return_value=(0, 'c' * 40, ''))
            (work / 'prompt').write_bytes(b'new\r\n  prompt\t\n')
        plan_path.write_text(json.dumps(plan))
        (state / S.STATE_FILE).write_text(json.dumps(record))
        binding = dict(unit='u', attempt='a1', pr=7, head=head, target='main', repo='https://github.com/example/project')
        scope = dict(binding, base='c' * 40, state_epoch=1, head_source='judged', status='in_scope', scope=['*'], out_of_scope=[], deletions_out_of_scope=[])
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if scenario.startswith(('run-', 'advance-')):
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
