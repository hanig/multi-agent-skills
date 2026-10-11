"""Remote verification through the real operator, Git bundle, SSH and Slurm shims."""
import base64
from contextlib import redirect_stdout
import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from tests import test_arc683_shared_guard as fixtures

S, V = fixtures.S, fixtures.V
RV = V.RV
PYTHON = '#!' + sys.executable + '\n'

def read_json(path):
    return json.loads(path.read_text())


SSH = r'''
import io, json, os, pathlib, subprocess, sys, tarfile
args = sys.argv[1:]
assert args[:2] == ['-oBatchMode=yes', '-oConnectTimeout=15']
assert args[2] == 'fixture-host'
command = args[3]
with open(os.environ['REMOTE_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if "tarfile" in command:
    records = list(pathlib.Path(os.environ['REMOTE_LEDGER_DIR']).glob('*.json'))
    assert records, 'coordinator launch must be durable before SSH'
    assert any(any(run['stage'] in command for run in json.loads(p.read_text())['runs'])
               for p in records), 'SSH launch must have its exact saved locator'
    for record in records:
        ledger = json.loads(record.read_text())
        for run in ledger['runs']:
            if run['stage'] in command:
                marker = record.with_suffix('.launches') / (run['launch_id'] + '.json')
                witness = json.loads(marker.read_text())
                assert run['witness_required'] is True
                assert witness['unit'] == ledger['unit'] and witness['basis'] == ledger['basis']
                assert all(witness[key] == run[key] for key in
                           ('launch_id', 'stage', 'verification_host', 'request'))
    mode = pathlib.Path(os.environ['REMOTE_MODE']).read_text()
    if mode == 'ssh-fail': raise SystemExit(255)
    raw = sys.stdin.buffer.read()
    if mode in ('tamper', 'direct-supervisor-died'):
        dst = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(raw)) as source, tarfile.open(fileobj=dst, mode='w') as dest:
            for member in source:
                data = source.extractfile(member).read()
                if member.name == 'candidate.bundle' and mode == 'tamper':
                    data = pathlib.Path(os.environ['TAMPER_BUNDLE']).read_bytes()
                    member.size = len(data)
                if member.name == 'remote_verify.py' and mode == 'direct-supervisor-died':
                    needle = b'    execution = result.get("execution", {})\n    publish(stage'
                    assert needle in data
                    data = data.replace(needle, b'    os.kill(os.getpid(), 9)\n' + needle)
                    member.size = len(data)
                dest.addfile(member, io.BytesIO(data))
        raw = dst.getvalue()
    result = subprocess.run(['/bin/sh', '-c', command], input=raw,
                            stdout=subprocess.DEVNULL if mode in ('ssh-lost-response', 'ssh-lost-both') else None)
    if mode in ('ssh-lost-response', 'ssh-lost-both', 'ssh-complete-255'): raise SystemExit(255)
else:
    if pathlib.Path(os.environ['REMOTE_MODE']).read_text() in ('cleanup-ssh-fail', 'ssh-lost-both'):
        raise SystemExit(255)
    result = subprocess.run(['/bin/sh', '-c', command])
raise SystemExit(result.returncode)
'''

SBATCH = r'''
import json, os, pathlib, subprocess, sys
with open(os.environ['SCHED_LOG'], 'a') as log:
    log.write(json.dumps(['sbatch'] + sys.argv[1:]) + '\n')
assert '--no-requeue' in sys.argv
request = json.loads((pathlib.Path(sys.argv[-1]).parent / 'request.json').read_text())
import hashlib
assert '--job-name=verify-' + hashlib.sha256(request['launch_id'].encode()).hexdigest() in sys.argv
assert '--worker' in pathlib.Path(sys.argv[-1]).read_text()
assert '--partition=fixture-cpu' in sys.argv
assert '--mem=2G' in sys.argv
assert '--time=00:05:00' in sys.argv
if pathlib.Path(os.environ['REMOTE_MODE']).read_text() not in ('slurm-missing', 'slurm-pending', 'slurm-running', 'slurm-lost-job-id'):
    result = subprocess.run(['/bin/sh', sys.argv[-1]], capture_output=True)
    assert result.returncode == 0 or pathlib.Path(os.environ['REMOTE_MODE']).read_text() == 'slurm-worker-died', result.stderr
    if pathlib.Path(os.environ['REMOTE_MODE']).read_text() == 'slurm-supervisor-died':
        import signal
        os.kill(os.getppid(), signal.SIGKILL)
    if pathlib.Path(os.environ['REMOTE_MODE']).read_text() == 'slurm-forced-requeue':
        pathlib.Path(os.environ['REMOTE_MODE']).write_text('pass')
        result = subprocess.run(['/bin/sh', sys.argv[-1]], capture_output=True)
        assert result.returncode == 0, result.stderr
print('accepted-with-lost-response' if pathlib.Path(os.environ['REMOTE_MODE']).read_text() == 'slurm-lost-job-id' else '321')
'''

SACCT = r'''
import json, os, pathlib, sys
if '--name' in sys.argv:
    print('321|' + sys.argv[sys.argv.index('--name') + 1])
    raise SystemExit(0)
mode = pathlib.Path(os.environ['REMOTE_MODE']).read_text()
print('321|PENDING|0:0' if mode in ('slurm-pending', 'slurm-lost-job-id') else
      '321|RUNNING|0:0' if mode == 'slurm-running' else
      '321|CANCELLED|0:0' if mode == 'slurm-cancelled' else
      '321|FAILED|1:0' if mode in ('slurm-missing', 'slurm-completed-fail', 'slurm-worker-died') else '321|COMPLETED|0:0')
'''


SQUEUE = r'''
import subprocess, sys
state = subprocess.check_output(['sacct', '-j', '321'], text=True).split('|')[1]
if state in ('PENDING', 'RUNNING', 'COMPLETING'):
    print('321|' + sys.argv[sys.argv.index('--name') + 1])
'''


class TestRemoteVerification(unittest.TestCase):
    def setUp(self):
        self.shared = fixtures.TestSharedGuard()
        self.shared.setUp()
        self.addCleanup(self.shared.doCleanups)
        self.f = self.shared.f
        self.journal = self.f.state_dir / S.VERIFY_RECEIPTS
        self.mode = self.f.directory / 'remote-mode'
        self.mode.write_text('pass')
        self.remote_root = self.f.directory / 'remote-root'
        self.remote_root.mkdir()
        self.witness = self.f.directory / 'verifier-ran'
        self.f.env.update(REMOTE_LOG=str(self.f.directory / 'ssh.log'),
                          REMOTE_MODE=str(self.mode), SCHED_LOG=str(self.f.directory / 'scheduler.log'),
                          REMOTE_LEDGER_DIR=str(self.f.state_dir / 'remote-verifications'))
        for name, body in (('ssh', SSH), ('sbatch', SBATCH), ('sacct', SACCT), ('squeue', SQUEUE),
                           ('scancel', 'import os\nfrom pathlib import Path\n'
                            'p=Path(os.environ["REMOTE_MODE"])\n'
                            'if p.read_text() == "slurm-pending": p.write_text("slurm-cancelled")\n')):
            program = self.f.bin / name
            program.write_text(PYTHON + body)
            program.chmod(0o755)
        self.policy = {'schema_version': 1,
                       'local': {'python': sys.executable, 'git': str((self.f.bin / 'git').resolve())},
                       'verification_host': {'ssh_alias': 'fixture-host', 'executor': 'direct',
                                  'workdir_root': str(self.remote_root), 'python': sys.executable,
                                  'git': str((self.f.bin / 'git').resolve())}}
        self.save_policy()
        self.journal.unlink()

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_failed(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def write_ssh(self, body):
        (self.f.bin / 'ssh').write_text(PYTHON + body)

    def locations(self):
        yield 'remote'
        self.local_only()
        yield 'local'

    def slurm_policy(self):
        self.policy['verification_host'].update(executor='slurm', slurm={
            'partition': 'fixture-cpu', 'mem': '2G', 'time': '00:05:00'})
        self.save_policy()

    def generic_verify(self, target):
        return subprocess.run([
            sys.executable, S.__file__, 'verify', '--state-dir', str(self.f.state_dir),
            '--unit', 'u', '--attempt', str(self.f.attempt), '--claim', V.INTEGRATION_CLAIM,
            '--target-commit', target, '--verifier', V.MERGE_VERIFIER,
            '--path', str(self.f.repo / V.MERGE_VERIFIER_PATH)],
            cwd=self.f.repo, env=self.f.env, capture_output=True, text=True, timeout=60)

    def empty_system_path(self):
        empty = self.f.directory / 'empty-defpath'
        empty.mkdir()
        (empty / 'env').symlink_to(shutil.which('env', path=os.defpath))
        site = self.f.directory / 'default-git-probe'
        site.mkdir()
        (site / 'sitecustomize.py').write_text('import os\nos.defpath = %r\n' % str(empty))
        self.f.env['PYTHONPATH'] = str(site)

    def assert_pending(self, result):
        self.assert_failed(result)
        self.assertIn('unresolved remote evidence at', result.stderr)

    def merges(self):
        return self.f.calls(['pr', 'merge'])

    def local_only(self):
        self.policy.pop('verification_host')
        self.save_policy()

    def capture_timeout(self, needle):
        site = self.f.directory / 'transport-timeout'
        site.mkdir()
        (site / 'sitecustomize.py').write_text(
            'import pathlib, subprocess, sys\n'
            'if sys.argv[0].endswith("merge_unit.py"):\n'
            '    original = subprocess.run\n'
            '    def capture(argv, *args, **kwargs):\n'
            '        result = original(argv, *args, **kwargs)\n'
            '        if pathlib.Path(argv[0]).name == "ssh" and %r in argv[-1]:\n'
            '            raise subprocess.TimeoutExpired(argv, 1, output=result.stdout.encode(), stderr=b"")\n'
            '        return result\n'
            '    subprocess.run = capture\n' % needle)
        self.f.env['PYTHONPATH'] = str(site)

    def legacy_prefix(self):
        row = dict(self.f.record_integration(), schema_version=1, execution=None)
        raw = json.dumps(row).encode() + b'\n'
        self.journal.write_bytes(raw)
        return raw

    def save_policy(self):
        (self.f.state_dir / RV.POLICY).write_text(json.dumps(self.policy))

    def authorize_program(self, program):
        policy = dict(self.f.policy, verifiers=[dict(
            self.f.policy['verifiers'][0], sha256=hashlib.sha256(program.encode()).hexdigest())])
        return self.shared.precondition.target_commit({V.MERGE_VERIFIER_PATH: program,
                                                       V.POLICY_FILE: json.dumps(policy)})

    def program(self, tail=''):
        return self.authorize_program(
            (PYTHON + 'from pathlib import Path\n'
             'Path(%r).write_text("ran")\n' % str(self.witness)) + tail)

    def test_launcher_does_not_repeat_verifier_python_startup(self):
        site = self.f.directory / 'verifier-site'
        site.mkdir()
        counter = self.f.directory / 'startup-count'
        (site / 'sitecustomize.py').write_text(
            'from pathlib import Path\np=Path(%r)\n'
            'p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n' % str(counter))
        wrapper = self.f.directory / 'declared-python-wrapper'
        wrapper.write_text(PYTHON + 'import os,sys\n'
                           'os.execv(%r, [%r]+sys.argv[1:])\n' % (sys.executable, sys.executable))
        wrapper.chmod(0o755)
        original = json.loads(json.dumps(self.policy))
        for selected, expected in ((sys.executable, 1), (str(wrapper), 2)):
            program = ('#!/usr/bin/env -S PYTHONHOME=' + sys.base_prefix + ' PYTHONPATH=' + str(site) + ' python3\n'
                       'from pathlib import Path\nimport os,sys\n'
                       'assert sys.flags.isolated == 0 and sys.flags.no_site == 0\n'
                       'assert os.environ["PYTHONPATH"] == %r\n'
                       'count=Path(%r).read_text()\nprint("startup count="+count)\n'
                       'assert count == %r, count\n' % (str(site), str(counter), str(expected)))
            control = self.f.directory / 'startup-control'
            control.write_text(program)
            if counter.exists():
                counter.unlink()
            native = subprocess.run([selected, str(control)],
                                    env=dict(self.f.env, PYTHONPATH=str(site), PYTHONHOME=sys.base_prefix),
                                    capture_output=True, text=True, timeout=10)
            self.assert_ok(native)
            self.authorize_program(program)
            self.policy = json.loads(json.dumps(original))
            self.policy['local']['python'] = selected
            self.policy['verification_host']['python'] = selected
            self.save_policy()
            for location in ('remote', 'local'):
                with self.subTest(selected=selected, location=location):
                    if location == 'local':
                        self.local_only()
                    counter.unlink()
                    result = self.verify()
                    self.assert_ok(result)
                    row = self.rows()[-1]
                    self.assertEqual(row['result'], 'pass')
                    self.assertEqual(counter.read_text(), str(expected))
                    self.assertEqual(row['stdout_tail'], native.stdout)
                    self.assertEqual(row['execution']['executables']['python']['path'],
                                     os.path.realpath(selected))
                    self.assert_clean()

    def authorize_both(self, program):
        self.shared.install_policy(repetitions=1, program=program)
        policy = self.shared.policy
        policy['verifiers'][0]['sha256'] = hashlib.sha256(program.encode()).hexdigest()
        return self.shared.precondition.target_commit({V.MERGE_VERIFIER_PATH: program,
                                                       V.POLICY_FILE: json.dumps(policy)})

    def admit_stable(self, target):
        row = next(r for r in self.rows() if r['claim'] == V.STABILITY_CLAIM)
        return S.admit_verification(self.f.state_dir, 'u', V.STABILITY_CLAIM, self.f.head,
                                   row['policy_sha256'], self.shared.policy, repo=self.f.repo,
                                   base_commit=target, runner=self.f.verifier_runner)

    def test_generic_admission_blocks_same_basis_but_allows_distinct_binding(self):
        first = self.authorize_both(PYTHON + 'print("pass")\n')
        host = self.policy.pop('verification_host')
        self.save_policy()
        self.assertEqual(self.verify().returncode, 0)
        self.policy['verification_host'] = host
        self.save_policy()
        self.mode.write_text('ssh-lost-response')
        old = "    if mode in ('ssh-lost-response', 'ssh-lost-both', 'ssh-complete-255'): raise SystemExit(255)"
        new = ("    for marker in pathlib.Path(os.environ['REMOTE_ROOT']).glob('verify-*/supervision-finished'):\n"
               "        marker.unlink()\n") + old
        self.f.env['REMOTE_ROOT'] = str(self.remote_root)
        self.write_ssh(SSH.replace(old, new))
        self.assertNotEqual(self.verify().returncode, 0)
        admitted, error = self.admit_stable(first)
        self.assertIsNone(admitted, 'an earlier local PASS must not escape the same pending basis')
        self.assertIn('unresolved remote evidence at', error)
        journal = self.f.state_dir / S.VERIFY_RECEIPTS
        saved = journal.read_bytes()
        partial = next(r for r in self.rows() if r['claim'] == V.STABILITY_CLAIM)
        partial.pop('candidate_tree')
        journal.write_text(json.dumps(partial) + '\n')
        admitted, error = self.admit_stable(first)
        self.assertIsNone(admitted)
        self.assertIn('basis', error)
        journal.write_bytes(saved)
        second = self.shared.precondition.target_commit({'other-target.txt': 'distinct binding\n'})
        self.local_only()
        self.mode.write_text('pass')
        result = self.verify()
        self.assert_ok(result)
        admitted, error = self.admit_stable(second)
        self.assertIsNone(error, error)
        self.assertEqual(admitted['target_commit'], second)
        legacy = dict(admitted, schema_version=1)
        for key in V.MERGE_BASIS_FIELDS + ('execution',):
            legacy.pop(key)
        for optional in ({}, {'execution': None}):
            journal.write_text(json.dumps(dict(legacy, **optional)) + '\n')
            admitted, error = self.admit_stable(second)
            self.assertIsNone(error, error)
            self.assertEqual(admitted['schema_version'], 1)
        self.assertEqual(self.merges(), [])

    def test_generic_stability_completed_failure_poisons_later_pass(self):
        verdict = self.f.directory / 'stable-verdict'
        verdict.write_text('fail')
        target = self.authorize_both(PYTHON + 'from pathlib import Path\n'
                                    'raise SystemExit(125 if Path(%r).read_text() == "fail" else 0)\n'
                                    % str(verdict))
        self.assertNotEqual(self.verify().returncode, 0)
        verdict.write_text('pass')
        self.assertEqual(self.verify().returncode, 0)
        admitted, error = self.admit_stable(target)
        self.assertIsNone(admitted)
        self.assertIn('FAIL', error)

    def test_large_old_receipts_retrieve_without_reexecution_or_harness_upgrade(self):
        for character, code in (('x', 0), ('\U0001f642', 0), ('\U0001f642', 125), ('\x00', 0), ('\x00', 125)):
            with self.subTest(character=repr(character), exit_code=code):
                self.authorize_both(PYTHON + 'import sys\n'
                                    'sys.stdout.write(%r * 5000)\nsys.stderr.write(%r * 3000)\n'
                                    'raise SystemExit(%d)\n' % (character, character, code))
                self.mode.write_text('ssh-lost-both')
                self.assertNotEqual(self.verify().returncode, 0)
                stage = Path(self.rows()[-1]['execution']['stage'])
                originals = {p.name: p.read_bytes() for p in stage.iterdir() if p.is_file()}
                for name in ('remote_verify.py', 'verify.py'):
                    (stage / name).unlink()
                before = self.ssh_launches()
                self.mode.write_text('pass')
                retrieved = self.verify('--retrieve-remote-evidence')
                self.assertEqual(retrieved.returncode, 0 if code == 0 else 1,
                                 retrieved.stdout + retrieved.stderr)
                for row in self.rows()[-2:]:
                    self.assertEqual((row['result'], row['exit_code']),
                                     ('pass' if code == 0 else 'fail', code))
                    self.assertEqual(row['stdout_tail'], character * 4000)
                    self.assertEqual(row['stderr_tail'], character * 2000)
                    self.assertTrue(row['execution']['evidence_reconciled'])
                    self.assertEqual(row['execution']['cleanup'], 'unconfirmed')
                ledger = RV.load_ledger(RV.ledger_path(self.f.state_dir, 'u',
                    {k: self.rows()[-1][k] for k in V.MERGE_BASIS_FIELDS}), 'u',
                    {k: self.rows()[-1][k] for k in V.MERGE_BASIS_FIELDS}, self.rows())
                run = ledger['runs'][-1]
                for index in range(2):
                    name = 'claim-%d.json' % index
                    self.assertEqual((stage / name).read_bytes(), originals[name])
                    self.assertEqual(run['receipts'][str(index)], json.loads(originals[name]))
                self.assertEqual(self.ssh_launches(), before)
                for name in ('remote_verify.py', 'verify.py'):
                    (stage / name).write_bytes(originals[name])
                count = len(self.rows())
                self.verify('--retrieve-remote-evidence')
                self.assertEqual(len(self.rows()), count)
                self.assertFalse(stage.exists())
                self.assertEqual(self.ssh_launches(), before)
                self.assertEqual(self.merges(), [])

    def test_chunk_frames_survive_transport_failure_and_later_claim_loss(self):
        self.authorize_both(PYTHON + 'import sys\n'
                            'sys.stdout.write("\\U0001f642" * 5000)\nraise SystemExit(125)\n')
        self.mode.write_text('ssh-lost-both')
        self.assertNotEqual(self.verify().returncode, 0)
        stage = Path(self.rows()[-1]['execution']['stage'])
        second = (stage / 'claim-1.json').read_bytes()
        header = (stage / 'request.json').read_bytes()
        (stage / 'claim-1.json').unlink()
        (stage / 'request.json').unlink()
        # Every frame is complete but its SSH process exits 255; the later
        # missing claim/header must not erase the first completed failure.
        old = "    result = subprocess.run(['/bin/sh', '-c', command])"
        self.assertIn(old, SSH)
        persisted = self.f.directory / 'claim-fsynced'
        audit = ("if \"'claim-1.json', 0,\" in __import__('shlex').split(command)[-1]:\n"
                 "    records = pathlib.Path(os.environ['REMOTE_LEDGER_DIR']).glob('*.json')\n"
                 "    assert any('0' in r['receipts'] for p in records for r in json.loads(p.read_text())['runs'])\n"
                 "    pathlib.Path(%r).write_text('saved')\n" % str(persisted))
        self.write_ssh(SSH.replace(old, old + '\n    raise SystemExit(255)').replace(
            'if "tarfile" in command:', audit + 'if "tarfile" in command:'))
        self.mode.write_text('pass')
        result = self.verify('--retrieve-remote-evidence')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(persisted.exists(), 'claim 0 must be durable before claim 1 retrieval')
        row = next(r for r in self.rows() if r['result'] == 'fail')
        self.assertEqual((row['claim'], row['exit_code']), (V.INTEGRATION_CLAIM, 125))
        self.assertFalse(row['execution']['evidence_reconciled'])
        self.assertTrue(stage.exists())
        self.assertIn('unresolved remote evidence', result.stderr)
        (stage / 'claim-1.json').write_bytes(second)
        (stage / 'request.json').write_bytes(header)
        self.write_ssh(SSH)
        self.capture_timeout('stage, name, offset')
        recovered = self.verify('--retrieve-remote-evidence')
        self.assertNotEqual(recovered.returncode, 0)
        self.assertEqual(self.rows()[-1]['result'], 'fail')
        self.assertEqual(self.rows()[-1]['claim'], V.STABILITY_CLAIM)
        self.assertTrue(self.rows()[-1]['execution']['evidence_reconciled'])
        self.assertEqual(self.ssh_launches(), 1)
        self.assert_clean()

    def test_remote_frames_validate_size_identity_digest_and_budgets(self):
        stage = self.remote_root / 'frames'
        stage.mkdir()
        value = {'output': '\U0001f642' * 6000}
        original = RV.encoded(value)
        (stage / 'claim-0.json').write_bytes(original)
        prefix = [str(self.f.bin / 'ssh'), '-oBatchMode=yes', '-oConnectTimeout=15', 'fixture-host']
        def call(code):
            with mock.patch.dict(os.environ, self.f.env, clear=True):
                proc = RV.remote_call(prefix, self.policy['verification_host'], code)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertLessEqual(len(proc.stdout), 100000)
            return proc.stdout
        limit = len(original) + 100
        self.assertEqual(RV.read_remote_record(call, str(stage), 'claim-0.json', limit, 100,
                         {'bytes': limit, 'calls': 3}), value)
        def corrupted(kind):
            calls = []
            def inner(code):
                frame = json.loads(call(code)); calls.append(frame)
                if kind == 'oversized': frame['padding'] = ' ' * 100001
                if kind == 'identity': frame['name'] = 'claim-1.json'
                if kind == 'offset': frame['offset'] += 1
                if kind == 'size': frame['size'] = limit + 1
                if kind == 'digest': frame['sha256'] = '0' * 64
                if kind == 'changed' and len(calls) == 2: frame['sha256'] = '0' * 64
                if kind == 'length': frame['data'] = base64.b64encode(original).decode('ascii')
                return json.dumps(frame)
            return inner
        for kind in ('oversized', 'identity', 'offset', 'size', 'digest', 'changed', 'length'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                RV.read_remote_record(corrupted(kind), str(stage), 'claim-0.json', limit, 100,
                                      {'bytes': limit, 'calls': 3})
        for budget in ({'bytes': 0, 'calls': 3}, {'bytes': limit, 'calls': 0}):
            with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, 'budget'):
                RV.read_remote_record(call, str(stage), 'claim-0.json', limit, 100, budget)
        with self.assertRaisesRegex(ValueError, 'file budget'):
            RV.read_remote_record(call, str(stage), 'claim-0.json', 10, 100,
                                  {'bytes': limit, 'calls': 3})
        self.assertEqual((stage / 'claim-0.json').read_bytes(), original)

    def test_shell_shebang_in_py_path_runs_without_completed_failure(self):
        self.authorize_program('#!/bin/sh\nprintf "shell verifier passed\\n"\nexit 0\n')
        for location in ('remote', 'local'):
            with self.subTest(location=location):
                if location == 'local':
                    self.local_only()
                result = self.verify()
                self.assert_ok(result)
                row = self.rows()[-1]
                self.assertEqual(row['result'], 'pass')
                self.assertIn('shell verifier passed', row['stdout_tail'])
                self.assertEqual(row['execution']['location'], location)
                self.assert_clean()
        self.assertNotIn('fail', [r['result'] for r in self.rows()])

    def test_python_shebang_and_no_shebang_use_declared_interpreter(self):
        log = self.f.directory / 'python-invocations'
        wrapper = self.f.directory / 'declared-python'
        wrapper.write_text(PYTHON + 'import os, sys\n'
                           'with open(%r, "a") as log: log.write(repr(sys.argv) + "\\n")\n'
                           'os.execv(%r, [%r] + sys.argv[1:])\n'
                           % (str(log), sys.executable, sys.executable))
        wrapper.chmod(0o755)
        self.policy['local']['python'] = str(wrapper)
        self.policy['verification_host']['python'] = str(wrapper)
        self.save_policy()
        for location in self.locations():
            for shebang in ('#!/absent/python3.10 -u\n', '#!/usr/bin/env python3\n',
                            '#!/usr/bin/env -S python3 -u\n', ''):
                with self.subTest(location=location, shebang=shebang):
                    self.authorize_program(shebang + 'print("declared Python ran")\n')
                    log.write_text('')
                    result = self.verify()
                    self.assert_ok(result)
                    row = self.rows()[-1]
                    self.assertEqual(row['result'], 'pass')
                    self.assertIn('declared Python ran', row['stdout_tail'])
                    self.assertIn('pinned-verifier-', log.read_text())
                    self.assert_clean()

    def test_local_implicit_python_preserves_native_shebang_and_child_selection(self):
        self.policy.pop('verification_host')
        self.policy['local'].pop('python')
        self.save_policy()
        # A binary symlink works in native Darwin shebangs and preserves spelling.
        native = self.f.directory / 'native-bin'
        native.mkdir()
        python = native / 'python3'
        python.symlink_to(sys.executable)
        self.f.env['PATH'] = str(native) + os.pathsep + self.f.env['PATH']
        for shebang in ('#!' + str(python) + '\n', '#!/usr/bin/env python3\n',
                        '#!/usr/bin/env -S python3 -u\n'):
            with self.subTest(shebang=shebang):
                self.authorize_program(shebang +
                    'import os, subprocess, sys\n'
                    'expected = %r\nassert os.path.abspath(sys.executable) == expected, sys.executable\n'
                    'assert "HANIG_VERIFICATION_PYTHON" not in os.environ\n'
                    'child = subprocess.check_output(["python3", "-c", "import sys; print(sys.executable)"], text=True)\n'
                    'assert os.path.abspath(child.strip()) == expected, child\n' % str(python))
                result = self.verify()
                self.assert_ok(result)
                row = self.rows()[-1]
                self.assertEqual(row['result'], 'pass')
                self.assertFalse(row['execution']['executables']['python']['declared'])
                self.assertEqual(row['execution']['executables']['python']['role'], 'resolved')
                self.assertEqual(row['execution']['executables']['python']['path'], str(python))
                self.assertEqual(row['execution']['executables']['launcher']['role'], 'launcher')
                self.assert_clean()

    def implicit_python(self, version):
        self.local_only()
        self.policy['local'].pop('python', None)
        self.save_policy()
        python = self.f.bin / 'python3'
        if python.exists():
            python.unlink()
        python.write_text(PYTHON + 'import os, sys\n'
                          'if sys.argv[1:] == ["--version"]:\n'
                          '    print(%r)\n    raise SystemExit(0)\n'
                          'os.execv(%r, [%r] + sys.argv[1:])\n'
                          % ('Python ' + version, sys.executable, sys.executable))
        python.chmod(0o755)
        self.authorize_program('#!/usr/bin/env python3\n'
                               'from pathlib import Path\nPath(%r).write_text("ran")\n'
                               % str(self.witness))
        return python

    def test_unsupported_implicit_python_is_incomplete_with_provenance(self):
        python = self.implicit_python('3.14.8')
        result = self.verify()
        self.assert_failed(result)
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        self.assertIsNone(row['exit_code'])
        self.assertIn(str(python), row['incomplete_reason'])
        self.assertIn('3.14.8', row['incomplete_reason'])
        self.assertIn('3.8 through 3.12.x', row['incomplete_reason'])
        self.assertEqual(row['execution']['executables']['python'],
                         dict(path=str(python), version='3.14.8', declared=False, role='resolved'))
        self.assertFalse(self.witness.exists())

    def test_supported_implicit_python_records_resolution_and_admits(self):
        python = self.implicit_python('3.9.6')
        self.assert_ok(self.verify())
        row, = self.rows()
        self.assertEqual(row['result'], 'pass')
        self.assertEqual(row['execution']['executables']['python'],
                         dict(path=str(python), version='3.9.6', declared=False, role='resolved'))
        launcher = row['execution']['executables']['launcher']
        self.assertEqual(launcher['role'], 'launcher')
        self.assertEqual(launcher['path'], os.path.realpath(sys.executable))
        self.assertEqual(launcher['version'], '.'.join(map(str, sys.version_info[:3])))
        self.assertEqual(self.witness.read_text(), 'ran')
        self.assert_ok(self.admitted())

    def test_legacy_fail_does_not_poison_supported_pass(self):
        self.implicit_python('3.9.6')
        self.assert_ok(self.verify())
        row, = self.rows()
        legacy = json.loads(json.dumps(row))
        legacy.update(result='fail', exit_code=1)
        legacy['execution']['executables']['python'] = dict(
            legacy['execution']['executables']['launcher'])
        self.journal.write_text(json.dumps(legacy) + '\n' + json.dumps(row) + '\n')
        self.assert_ok(self.admitted())

    def test_legacy_pass_without_interpreter_provenance_cannot_admit(self):
        self.implicit_python('3.9.6')
        self.assert_ok(self.verify())
        row, = self.rows()
        row['execution']['executables']['python'] = dict(
            row['execution']['executables']['launcher'])
        self.journal.write_text(json.dumps(row) + '\n')
        result = self.admitted()
        self.assert_failed(result)
        self.assertIn('unknown interpreter provenance', result.stderr)

    def test_stability_legacy_receipts_neither_poison_nor_admit(self):
        self.implicit_python('3.9.6')
        self.authorize_both('#!/usr/bin/env python3\nprint("both claims")\n')
        self.assert_ok(self.verify())
        rows = self.rows()
        stability = next(r for r in rows if r['claim'] == V.STABILITY_CLAIM)
        legacy = json.loads(json.dumps(stability))
        legacy.pop('execution')
        legacy.update(result='fail', exit_code=1)
        for result in ('pass', 'fail'):
            with self.subTest(result=result):
                legacy.update(result=result, exit_code=0 if result == 'pass' else 1)
                only_legacy = [r for r in rows if r['claim'] != V.STABILITY_CLAIM] + [legacy]
                self.journal.write_text(''.join(json.dumps(r) + '\n' for r in only_legacy))
                refused = self.admitted()
                self.assert_failed(refused)
                self.assertIn('no passing changed-tests-stable receipt', refused.stderr)
        self.journal.write_text(''.join(json.dumps(r) + '\n' for r in [legacy] + rows))
        self.assert_ok(self.admitted())

    def test_local_implicit_shebang_preserves_native_parser_outcome(self):
        self.policy.pop('verification_host')
        self.policy['local'].pop('python')
        self.save_policy()
        program = '#!/usr/bin/env -S python3 \\c "\nprint("native split")\n'
        control = self.f.directory / 'native-control'
        control.write_text(program)
        control.chmod(0o755)
        native = subprocess.run([str(control)], cwd=self.f.repo, env=self.f.env,
                                capture_output=True, text=True, timeout=10)
        # Preserve the native result, including kernel-specific shebang splitting.
        self.authorize_program(program)
        result = self.verify()
        row, = self.rows()
        self.assertEqual(row['exit_code'], native.returncode)
        self.assertEqual(row['result'], 'pass' if native.returncode == 0 else 'fail')
        self.assertNotIn('incomplete_reason', row)
        self.assertEqual(result.returncode, 0 if native.returncode == 0 else 1)
        self.assert_clean()

    def test_declared_python_with_env_options_and_assignments(self):
        self.f.env['REMOVE_FOR_VERIFIER'] = 'must disappear'
        for location in self.locations():
            for options in ('-i', '-u REMOVE_FOR_VERIFIER', '-uREMOVE_FOR_VERIFIER'):
                with self.subTest(location=location, options=options):
                    self.authorize_program(
                        '#!/usr/bin/env -S ' + options + ' VERIFIER_VALUE="two words" python3 -u\n'
                        'import os, subprocess, sys\n'
                        'assert os.path.realpath(sys.executable) == %r, sys.executable\n'
                        'assert "REMOVE_FOR_VERIFIER" not in os.environ\n'
                        'assert os.environ["VERIFIER_VALUE"] == "two words"\n'
                        'assert os.environ["HANIG_VERIFICATION_GIT"] == %r\n'
                        'child = subprocess.check_output(["python3", "-c", "import os,sys;print(os.path.realpath(sys.executable))"], text=True)\n'
                        'assert child.strip() == %r, child\n'
                        % (os.path.realpath(sys.executable), str((self.f.bin / 'git').resolve()),
                           os.path.realpath(sys.executable)))
                    result = self.verify()
                    self.assert_ok(result)
                    self.assertEqual(self.rows()[-1]['result'], 'pass')
                    self.assert_clean()

    def test_implicit_local_no_shebang_preserves_native_launch_failure(self):
        self.policy.pop('verification_host')
        self.policy['local'].pop('python')
        self.save_policy()
        self.authorize_program('printf "shell verifier passed\\n"\nexit 0\n')
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        self.assertIn('could not exec verifier', row['incomplete_reason'])
        self.assertNotIn('SyntaxError', row['stderr_tail'])

    def test_declared_python_preserves_final_process_argv0(self):
        expected = 'verifier-process-name'
        observe = ('import os, subprocess, sys\n'
                   'command = subprocess.check_output(["/bin/ps", "-p", str(os.getpid()), "-o", "command="], text=True).strip()\n')
        # Independent real exec control: process argv0 is not Python sys.argv[0].
        # Framework stubs may re-exec Python.app and replace argv0 themselves.
        # Compare that native behavior; still demand the requested name wherever
        # the interpreter preserves it. Both paths execute every harness case.
        control = self.f.directory / 'argv0-control.py'
        control.write_text(observe +
            'suffix = " -u " + sys.argv[0]\n'
            'assert command.endswith(suffix), command\n'
            'print(command[:-len(suffix)])\n')
        result = subprocess.run([sys.executable, '-c',
            'import os,sys;os.execv(sys.argv[1],[sys.argv[2],"-u",sys.argv[3]])',
            sys.executable, expected, str(control)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        native_argv0 = result.stdout.rstrip('\n')
        self.assertTrue(native_argv0, result.stdout)
        assertion = ('assert command.startswith(%r + " "), command\n' % expected
                     if native_argv0 == expected else
                     'assert command == %r + " -u " + sys.argv[0], command\n' % native_argv0)
        body = observe + assertion + ('assert sys.version_info[:2] == %r, sys.version\n'
                                      % (sys.version_info[:2],))
        for location in self.locations():
            for option in ('-a ' + expected, '-a' + expected,
                           '--argv0=' + expected, '-a ignored --argv0 ' + expected):
                with self.subTest(location=location, option=option):
                    self.authorize_program('#!/usr/bin/env -S ' + option + ' python3 -u\n' + body)
                    result = self.verify()
                    self.assert_ok(result)
                    self.assertEqual(self.rows()[-1]['result'], 'pass')
                    self.assert_clean()

    def test_malformed_explicit_env_selectors_finish_incomplete(self):
        for location in self.locations():
            for declaration in ('-S', '-S -a', '-S -u', '-S -a "" python3',
                                '-S "unterminated', '-S --unknown python3', '-S python3 \\c ignored'):
                with self.subTest(location=location, declaration=declaration):
                    self.authorize_program('#!/usr/bin/env ' + declaration + '\nprint("must not run")\n')
                    result = self.verify()
                    self.assertNotEqual(result.returncode, 0)
                    row = self.rows()[-1]
                    self.assertEqual(row['result'], 'incomplete')
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertNotIn('unresolved remote evidence', result.stderr)
                    self.assertNotIn('must not run', row['stdout_tail'])
                    self.assert_clean()

    def test_env_operand_and_assignment_do_not_select_python(self):
        self.local_only()
        (self.f.bin / 'sh').symlink_to('/bin/sh')
        for prefix in ('-u python3 ', '-- '):
            with self.subTest(prefix=prefix):
                self.authorize_program('#!/usr/bin/env -S ' + prefix +
                    'python3=an-assignment sh\n'
                    'test "$python3" = an-assignment || exit 76\n'
                    'printf "native shell selected\\n"\n')
                result = self.verify()
                self.assert_ok(result)
                self.assertIn('native shell selected', self.rows()[-1]['stdout_tail'])

    def test_env_changes_cwd_before_declared_tools_are_restored(self):
        directory = self.f.directory / 'env-fixture'
        directory.mkdir()
        env = directory / 'env'
        cwd = directory / 'selected cwd'
        cwd.mkdir()
        env.write_text(PYTHON + 'import os,sys\n'
            'a=sys.argv[1:]\n'
            'assert a[:8] == %r, a\n'
            'os.chdir(a[2]);os.environ.clear();os.environ["VERIFIER_VALUE"]="two  words"\n'
            'os.execve(a[8], a[8:], os.environ)\n'
            % ['-i', '-C', str(cwd), '-P', '/absent', '-u', 'python3', 'VERIFIER_VALUE=two  words'])
        env.chmod(0o755)
        self.authorize_program('#!' + str(env) + ' -S -i -C ' + shlex.quote(str(cwd)) +
            ' -P /absent -u python3 VERIFIER_VALUE="two  words" python3 -u\n'
            'import os,subprocess,sys\n'
            'assert os.getcwd() == %r\n'
            'assert os.environ["VERIFIER_VALUE"] == "two  words"\n'
            'assert os.environ["HANIG_VERIFICATION_GIT"] == %r\n'
            'assert os.path.realpath(os.environ["HANIG_VERIFICATION_PYTHON"]) == %r\n'
            'child=subprocess.check_output(["python3","-c","import os,sys;print(os.path.realpath(sys.executable))"],text=True)\n'
            'assert child.strip() == %r\n'
            % (str(cwd), str((self.f.bin / 'git').resolve()), os.path.realpath(sys.executable),
               os.path.realpath(sys.executable)))
        result = self.verify()
        self.assert_ok(result)
        self.assertEqual(self.rows()[-1]['result'], 'pass')
        self.assert_clean()

    def cancellation_fixture(self, mode='slurm-pending', retry_succeeds=False,
                             terminal_on_success=True):
        self.slurm_policy()
        self.program()
        self.mode.write_text(mode)
        witness = self.f.directory / 'cancellation-calls'
        terminal = self.f.directory / 'cancelled'
        (self.f.bin / 'sacct').write_text(
            PYTHON + 'from pathlib import Path\nimport sys\n'
            'if "--name" in sys.argv:\n'
            '    print("321|" + sys.argv[sys.argv.index("--name") + 1]); raise SystemExit(0)\n'
            'print("321|CANCELLED|0:0" if Path(%r).exists() else "321|PENDING|0:0")\n'
            % str(terminal))
        (self.f.bin / 'scancel').write_text(
            PYTHON + 'from pathlib import Path\nimport sys\n'
            'p=Path(%r)\n'
            'p.write_text((p.read_text() if p.exists() else "") + "attempt\\n")\n'
            'print("fixture cancellation denied", file=sys.stderr)\n'
            'success = %r and len(p.read_text().splitlines()) > 1\n'
            'if success and %r: Path(%r).write_text("terminal")\n'
            'raise SystemExit(0 if success else 1)\n'
            % (str(witness), retry_succeeds, terminal_on_success, str(terminal)))
        return witness

    def assert_unconfirmed_cancellation(self, mode):
        witness = self.cancellation_fixture(mode)
        result = self.verify('--verification-timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        expected = 'incomplete' if mode == 'slurm-pending' else 'pass'
        self.assertEqual(row['result'], expected)
        self.assertIs(row['execution']['evidence_reconciled'], False)
        execution = row['execution']
        self.assertEqual(execution['job_id'], '321')
        self.assertEqual(execution['cancellation'], 'unconfirmed')
        self.assertEqual(execution['cleanup'], 'unconfirmed')
        self.assertTrue(Path(execution['stage'], 'job-id').exists())
        self.assertTrue(execution['cancellation_attempts'])
        for attempt in execution['cancellation_attempts']:
            self.assertEqual(attempt['job_id'], '321')
            self.assertEqual(attempt['exit_code'], 1)
            self.assertIn('fixture cancellation denied', attempt['stderr_tail'])
        self.assertRegex(result.stderr, r'WARNING: .*job 321')
        self.assertIn('cancel', result.stderr)
        self.assertEqual(self.merges(), [])
        return witness, execution

    def test_failed_scancel_is_unconfirmed_with_operator_warning(self):
        witness, execution = self.assert_unconfirmed_cancellation('slurm-pending')
        self.assertEqual(len(witness.read_text().splitlines()), 2)
        self.assertEqual(len(execution['cancellation_attempts']), 2)
        self.assertIsNone(execution['sacct_state'])

    def test_secondary_cleanup_retains_cancellation_after_lost_response(self):
        witness, execution = self.assert_unconfirmed_cancellation('ssh-lost-response')
        self.assertEqual(len(witness.read_text().splitlines()), 2)
        self.assertEqual(len(execution['cancellation_attempts']), 2)

    def test_lost_connections_preserve_remote_cancellation_evidence_and_recovery_path(self):
        self.cancellation_fixture('ssh-lost-both')
        result = self.verify('--verification-timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        execution = row['execution']
        self.assertEqual(execution['cleanup'], 'unconfirmed')
        self.assertEqual(execution['cancellation'], 'unconfirmed')
        self.assertIsNone(execution['job_id'], 'an unseen job ID cannot be invented')
        stage, = self.remote_root.iterdir()
        self.assertEqual(execution['stage'], str(stage))
        self.assertTrue((stage / 'cleanup.json').is_file())
        retained = read_json(stage / 'cleanup.json')
        self.assertEqual(retained['job_id'], '321')
        self.assertEqual(retained['cancellation'], 'unconfirmed')
        self.assertIn('fixture cancellation denied', retained['cancellation_attempts'][0]['stderr_tail'])
        self.assertIn(str(stage / 'job-id'), result.stderr)
        self.assertIn(str(stage / 'cleanup.json'), result.stderr)
        self.assertEqual(self.merges(), [])

    def test_confirmed_removal_requires_reconciliation_acknowledgment(self):
        self.program()
        result = self.verify()
        self.assert_ok(result)
        row, = self.rows()
        self.assertEqual(row['result'], 'pass')
        self.assertEqual(row['execution']['cleanup'], 'removed')
        self.assertTrue(self.witness.exists())
        self.assertEqual(len(Path(self.f.env['REMOTE_LOG']).read_text().splitlines()), 2)
        self.assert_clean()

    def test_candidate_ssh_is_excluded_from_relative_absolute_and_symlink_path(self):
        self.program()
        marker = self.f.directory / 'candidate-ssh-ran'
        self.shared.candidate({'ssh': PYTHON + 'from pathlib import Path\n'
                               'Path(%r).write_text("candidate ssh executed")\n'
                               'raise SystemExit(59)\n' % str(marker)})
        (self.f.repo / 'ssh').chmod(0o755)
        self.shared.candidate({'record-mode.txt': 'commit executable mode\n'})
        linked = self.f.directory / 'external-path'
        linked.mkdir()
        (linked / 'ssh').symlink_to(self.f.repo / 'ssh')
        original_path = self.f.env['PATH']
        for prefix in ('.', str(self.f.repo), str(linked)):
            with self.subTest(prefix=prefix):
                if self.witness.exists():
                    self.witness.unlink()
                self.f.env['PATH'] = prefix + os.pathsep + original_path
                result = self.verify()
                self.assert_ok(result)
                self.assertFalse(marker.exists())
                self.assertTrue(self.witness.exists())
                self.assertEqual(self.rows()[-1]['result'], 'pass')
                self.assert_clean()

    def cleanup_stage(self):
        stage = self.remote_root / 'cleanup-stage'
        stage.mkdir()
        (stage / 'job-id').write_text('321')
        (stage / 'supervision-finished').write_text('{}')
        # Exercise the legacy cleanup plumbing; TestSlurmLifecycle below uses
        # a real Slurm request and scheduler processes for the discovery path.
        (stage / 'request.json').write_text(json.dumps({
            'verification_host': {'executor': 'direct'}}))
        return stage

    def test_secondary_cleanup_cannot_remove_stage_before_submission_finishes(self):
        stage = self.remote_root / 'submission-in-progress'
        stage.mkdir()
        with mock.patch.object(RV, '_command', return_value=(1, '', 'denied')) as command:
            execution = RV.cleanup(stage)
        self.assertEqual(execution['cleanup'], 'unconfirmed')
        self.assertTrue(stage.exists())
        command.assert_not_called()

    def test_missing_job_id_cannot_remove_a_pending_stage(self):
        stage = self.cleanup_stage()
        (stage / 'job-id').unlink()
        (stage / 'supervision-finished').write_text(json.dumps({
            'job_id': '321', 'sacct_state': None}))
        with mock.patch.object(RV, 'scheduler_state', return_value=None) as state, \
                mock.patch.object(RV, '_command', return_value=(1, '', 'denied')):
            execution = RV.cleanup(stage)
        self.assertEqual(execution['cleanup'], 'unconfirmed')
        self.assertEqual(execution['job_id'], '321')
        self.assertTrue(stage.exists())
        state.assert_called_once_with('321')

    def test_cleanup_lock_prevents_overlapping_cancellation(self):
        stage = self.cleanup_stage()
        with (stage / 'cleanup.lock').open('a') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(RV, 'scheduler_state', return_value=None), \
                    mock.patch.object(RV, '_command', return_value=(1, '', 'denied')) as command:
                execution = RV.cleanup(stage)
            self.assertEqual(execution['cleanup'], 'unconfirmed')
            self.assertTrue(stage.exists())
            command.assert_not_called()

    def test_cleanup_attempt_bound_retains_history_and_stage(self):
        stage = self.cleanup_stage()
        with mock.patch.object(RV, 'scheduler_state', return_value=None), \
                mock.patch.object(RV, '_command', return_value=(1, '', 'denied')) as command:
            for _ in range(5):
                execution = RV.cleanup(stage)
        self.assertEqual(command.call_count, 4)
        self.assertEqual(execution['cleanup'], 'unconfirmed')
        retained = read_json(stage / 'cleanup.json')
        self.assertEqual(len(retained['cancellation_attempts']), 4)
        self.assertLessEqual((stage / 'cleanup.json').stat().st_size, 65536)
        self.assertTrue((stage / 'job-id').exists())

    def test_cleanup_persistence_failure_refuses_cancellation_and_removal(self):
        stage = self.cleanup_stage()
        with mock.patch.object(RV, 'scheduler_state', return_value=None), \
                mock.patch.object(RV, 'publish', side_effect=OSError('journal unavailable')), \
                mock.patch.object(RV, '_command', return_value=(1, '', 'denied')) as command:
            execution = RV.cleanup(stage)
        self.assertEqual(execution['cleanup'], 'unconfirmed')
        self.assertIn('journal unavailable', execution['cleanup_error'])
        self.assertTrue((stage / 'job-id').exists())
        command.assert_not_called()

    def test_cleanup_oversized_history_is_retained_without_cancellation(self):
        stage = self.cleanup_stage()
        path = stage / 'cleanup.json'
        before = ' ' * 65537
        path.write_text(before)
        with mock.patch.object(RV, '_command', return_value=(1, '', 'denied')) as command:
            execution = RV.cleanup(stage)
        self.assertEqual(execution['cleanup'], 'unconfirmed')
        self.assertEqual(path.read_text(), before)
        command.assert_not_called()

    def test_failed_secondary_transport_keeps_first_cancellation_diagnostic(self):
        witness, execution = self.assert_unconfirmed_cancellation('cleanup-ssh-fail')
        self.assertEqual(len(witness.read_text().splitlines()), 1)
        self.assertIn('cleanup transport exited 255', execution['cleanup_error'])

    def test_successful_cancellation_retry_records_request_not_termination(self):
        witness = self.cancellation_fixture(retry_succeeds=True)
        result = self.verify('--verification-timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        execution = row['execution']
        self.assertEqual(execution['cancellation'], 'requested')
        self.assertIsNone(execution['sacct_state'])
        self.assertEqual(execution['cleanup'], 'unconfirmed')
        self.assertEqual(execution['cleanup_sacct_state'], 'CANCELLED')
        self.assertEqual([a['exit_code'] for a in execution['cancellation_attempts']], [1, 0])
        self.assertIn('unresolved remote evidence at', result.stderr)
        self.assertTrue(Path(execution['stage'], 'job-id').exists())
        self.assertFalse(Path(execution['stage'], 'worker-complete').exists())
        self.assertEqual(self.merges(), [])

    def test_accepted_cancellation_without_terminal_job_retains_stage(self):
        self.cancellation_fixture(retry_succeeds=True, terminal_on_success=False)
        result = self.verify('--verification-timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        execution = row['execution']
        self.assertEqual(execution['cancellation'], 'requested')
        self.assertEqual(execution['cleanup'], 'unconfirmed')
        self.assertIsNone(execution['sacct_state'])
        self.assertTrue(Path(execution['stage'], 'job-id').is_file())
        self.assertRegex(result.stderr, r'WARNING: .*job 321')
        self.assertEqual(self.merges(), [])

    def ssh_launches(self):
        calls = [json.loads(line) for line in Path(self.f.env['REMOTE_LOG']).read_text().splitlines()]
        return sum('tarfile' in call[-1] for call in calls)

    def verify(self, *extra):
        return self.f.invoke('--verify-integration', *extra)

    def rows(self):
        return S.load_verifications(self.f.state_dir)[0]

    def admitted(self):
        return self.f.invoke()

    def assert_clean(self):
        self.assertEqual(list(self.remote_root.iterdir()), [])
        self.assertEqual(self.merges(), [])

    def test_direct_bundle_runs_pinned_program_and_records_full_binding(self):
        legacy = self.legacy_prefix()
        target = self.program()
        result = self.verify()
        self.assert_ok(result)
        _, row = self.rows()
        self.assertEqual(row['subject_head'], self.f.head)
        self.assertEqual(row['target_commit'], target)
        basis, error = V.candidate_merge_basis(self.f.verifier_runner, self.f.repo, self.f.head, target)
        self.assertIsNone(error)
        for key, value in basis.items():
            self.assertEqual(row[key], value)
        execution = row['execution']
        self.assertEqual(execution['verified_tree'], row['candidate_tree'])
        self.assertEqual(execution['executor'], 'direct')
        self.assertEqual(execution['host_identity'], os.uname().nodename)
        for name in ('python', 'git'):
            self.assertEqual(execution['executables'][name]['path'],
                             os.path.realpath(self.policy['verification_host'][name]))
            self.assertTrue(execution['executables'][name]['version'])
        self.assertTrue(self.witness.exists())
        self.assert_clean()
        saved = self.rows()
        self.assert_ok(self.verify('--retrieve-remote-evidence'))
        self.assertEqual(self.rows(), saved)
        self.assertEqual(self.ssh_launches(), 1)
        self.assertTrue(self.journal.read_bytes().startswith(legacy))
        self.assertEqual(self.admitted().returncode, 0)

    def test_completed_fail_survives_ssh_255_and_poisoning(self):
        legacy = self.legacy_prefix()
        self.program('raise SystemExit(125 * int(Path(%r).read_text() != "pass"))\n'
                     % str(self.mode))
        self.mode.write_text('ssh-complete-255')
        self.assertNotEqual(self.verify().returncode, 0)
        self.assertEqual((self.rows()[-1]['result'], self.rows()[-1]['exit_code']), ('fail', 125))
        self.mode.write_text('pass')
        self.assertEqual(self.verify().returncode, 0)
        refused = self.admitted()
        self.f.assert_refused(refused)
        self.assertIn('FAIL', refused.stderr)
        self.assertTrue(self.journal.read_bytes().startswith(legacy))

    def test_completed_pass_survives_ssh_255(self):
        self.program()
        self.mode.write_text('ssh-complete-255')
        result = self.verify()
        self.assert_ok(result)
        self.assertEqual(self.rows()[-1]['result'], 'pass')
        self.assert_clean()

    def test_completed_failure_in_timeout_capture_is_still_evidence(self):
        self.program('raise SystemExit(125)\n')
        self.mode.write_text('cleanup-ssh-fail')
        self.capture_timeout('tarfile')
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual((row['result'], row['exit_code']), ('fail', 125))
        self.assertEqual(row['execution']['cleanup'], 'unconfirmed')
        self.assertTrue(Path(row['execution']['stage'], 'claim-0.json').exists())

    def test_unretrievable_launch_blocks_rerun_then_retrieves_failure(self):
        self.program('raise SystemExit(125 * int(Path(%r).read_text() != "pass"))\n'
                     % str(self.mode))
        self.mode.write_text('ssh-lost-both')
        self.assertNotEqual(self.verify().returncode, 0)
        self.assertEqual(self.rows()[-1]['result'], 'incomplete')
        stage = Path(self.rows()[-1]['execution']['stage'])
        self.assertTrue(stage.exists())
        blocked = self.verify()
        self.assert_pending(blocked)
        self.assertIn(str(stage), blocked.stderr)
        self.assertEqual(list(self.remote_root.iterdir()), [stage])
        # Reconnection retrieves the old failure; it never runs a green trial.
        self.mode.write_text('pass')
        retrieved = self.verify()
        self.assertNotEqual(retrieved.returncode, 0)
        self.assertEqual((self.rows()[-1]['result'], self.rows()[-1]['exit_code']), ('fail', 125))
        self.assertEqual(self.ssh_launches(), 1)
        self.f.assert_refused(self.admitted())
        self.assert_clean()

    def test_partial_failure_survives_later_worker_loss_and_blocks_local_escape(self):
        first = PYTHON + 'raise SystemExit(125)\n'
        second = (PYTHON + 'import os, signal\n'
                  'os.kill(os.getppid(), signal.SIGKILL)\n')
        self.shared.install_policy(repetitions=1)
        policy = self.shared.policy
        policy['verifiers'][0]['sha256'] = hashlib.sha256(first.encode()).hexdigest()
        policy['verifiers'][1]['sha256'] = hashlib.sha256(second.encode()).hexdigest()
        self.shared.precondition.target_commit({V.MERGE_VERIFIER_PATH: first,
            V.STABILITY_VERIFIER_PATH: second, V.POLICY_FILE: json.dumps(policy)})
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        first_row, later = self.rows()
        self.assertEqual((first_row['result'], first_row['exit_code']), ('fail', 125))
        self.assertEqual(later['result'], 'incomplete')
        stage = Path(first_row['execution']['stage'])
        self.assertTrue((stage / 'claim-0.json').exists())
        self.assertFalse((stage / 'worker-complete').exists())
        before = (stage / 'claim-0.json').read_bytes()
        self.local_only()
        retry = self.verify()
        self.assert_pending(retry)
        self.assertEqual((stage / 'claim-0.json').read_bytes(), before)
        self.assertEqual(len(self.rows()), 2, 'retrieval must be idempotent')
        self.f.assert_refused(self.admitted())
        self.assertEqual(self.ssh_launches(), 1)

    def test_slurm_late_terminal_accounting_appends_admissible_evidence(self):
        self.cancellation_fixture('pass')
        self.assert_pending(self.verify('--verification-timeout', '1'))
        original = self.journal.read_bytes()
        first, = self.rows()
        self.assertEqual(first['result'], 'pass')
        self.assertIsNone(first['execution']['sacct_state'])
        (self.f.bin / 'sacct').write_text(PYTHON + 'import sys\n'
            'print("321|" + sys.argv[sys.argv.index("--name")+1] '
            'if "--name" in sys.argv else "321|COMPLETED|0:0")\n')
        self.verify('--retrieve-remote-evidence')  # refresh lifecycle observation
        self.assert_ok(self.verify('--retrieve-remote-evidence'))
        self.assertEqual(self.ssh_launches(), 1)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(self.journal.read_bytes().startswith(original))
        self.assertEqual(self.rows()[-1]['execution']['sacct_state'], 'COMPLETED')
        self.assert_ok(self.verify('--retrieve-remote-evidence'))
        self.assertEqual(len(self.rows()), 2, 'terminal retrieval remains idempotent')
        self.assert_ok(self.admitted())
        self.assertFalse(Path(first['execution']['stage']).exists())

    def recovery_proof(self):
        path, = (self.f.state_dir / 'remote-verifications').glob('*.json')
        run = read_json(path)['runs'][-1]
        proof = {'binding': RV.recovery_binding(run),
                 'supervisor': {'status': 'fenced', 'evidence': 'Supervisor terminated; launch capability revoked.'},
                 'jobs': {'status': 'fenced', 'evidence': 'All launch jobs and descendants terminated; submission capability revoked.'},
                 'privileged_requeue': {'status': 'fenced', 'evidence': 'Scheduler administrator disabled requeue for this launch permanently.'}}
        proof_path = self.f.directory / 'recovery-attestation.json'
        proof_path.write_text(json.dumps(proof))
        return path, run, proof_path

    def resolve_remote(self, proof_path):
        return self.verify('--retrieve-remote-evidence',
                           '--remote-recovery-attestation', str(proof_path))

    def test_supervisor_loss_requires_authority_then_admits_existing_pass(self):
        self.slurm_policy()
        self.authorize_both(PYTHON + 'raise SystemExit(0)\n')
        self.mode.write_text('slurm-supervisor-died')
        self.assert_pending(self.verify())
        path, before, proof = self.recovery_proof()
        stage = Path(before['stage'])
        self.assertFalse((stage / 'supervision-finished').exists())
        self.assertEqual(len(before['receipts']), 2)
        self.assertEqual(before['execution']['sacct_state'], 'COMPLETED')
        self.assert_pending(self.admitted())
        self.assert_pending(self.verify('--retrieve-remote-evidence'))
        launches = self.ssh_launches()
        self.assert_ok(self.resolve_remote(proof))
        after = read_json(path)['runs'][-1]
        self.assertEqual(after['receipts'], before['receipts'])
        self.assertEqual(after['recovery_authority']['evidence_class'], 'attested')
        self.assertTrue(after['reconciled'])
        self.assertTrue(after['published'])
        self.assertTrue(stage.exists())
        self.assertFalse((stage / 'supervision-finished').exists())
        self.assertEqual(self.ssh_launches(), launches)
        self.assert_ok(self.verify('--retrieve-remote-evidence'))
        self.assert_ok(self.admitted())  # Uses only the fixture gh on replaced PATH.

    def test_old_staged_harness_recovers_without_receipt_or_harness_rewrite(self):
        self.slurm_policy()
        self.authorize_both(PYTHON + 'raise SystemExit(0)\n')
        self.mode.write_text('slurm-supervisor-died')
        # Retain the original missing-marker cleanup behavior, with no current
        # coordinator observation helpers available in the staged harness.
        base = (Path(RV.__file__).read_text() + '\n').encode()
        original = b'        if not finished:\n            raise ValueError("supervision may still submit or publish a job ID; stage retained")'
        self.assertIn(original, base)
        text = base.decode()
        start = text.index('def observe_slurm_success(')
        end = text.index('def cleanup_slurm(', start)
        base = (text[:start] + text[end:]).encode()
        self.f.env['LEGACY_HARNESS'] = str(self.f.directory / 'legacy-harness.py')
        Path(self.f.env['LEGACY_HARNESS']).write_bytes(base)
        fixture = SSH.replace("if mode in ('tamper', 'direct-supervisor-died'):",
                              "if mode in ('tamper', 'direct-supervisor-died', 'slurm-supervisor-died'):")
        needle = '                dest.addfile(member, io.BytesIO(data))'
        fixture = fixture.replace(needle,
            "                if member.name == 'remote_verify.py' and mode == 'slurm-supervisor-died':\n"
            "                    data = pathlib.Path(os.environ['LEGACY_HARNESS']).read_bytes()\n"
            "                    member.size = len(data)\n" + needle)
        self.write_ssh(fixture)
        self.assert_pending(self.verify())
        path, before, proof = self.recovery_proof()
        staged = Path(before['stage']) / 'remote_verify.py'
        self.assertEqual(staged.read_bytes(), base)
        self.assertEqual(before['execution']['sacct_state'], 'COMPLETED')
        self.assert_ok(self.resolve_remote(proof))
        self.assertEqual(staged.read_bytes(), base)
        self.assertEqual(read_json(path)['runs'][-1]['receipts'], before['receipts'])
        self.assert_ok(self.admitted())

    def test_supervisor_loss_recovery_preserves_completed_fail(self):
        self.slurm_policy()
        self.authorize_both(PYTHON + 'raise SystemExit(7)\n')
        self.mode.write_text('slurm-supervisor-died')
        self.assert_pending(self.verify())
        path, before, proof = self.recovery_proof()
        self.assertEqual({r['result'] for r in self.rows()}, {'fail'})
        self.assert_pending(self.admitted())
        result = self.resolve_remote(proof)
        self.assert_failed(result)
        self.assertIn('candidate merge verifier failed', result.stderr)
        self.assertEqual(read_json(path)['runs'][-1]['receipts'], before['receipts'])
        self.assert_failed(self.admitted())
        self.assertEqual(self.merges(), [])

    def test_direct_supervisor_loss_uses_same_explicit_recovery_authority(self):
        self.authorize_both(PYTHON + 'raise SystemExit(0)\n')
        self.mode.write_text('direct-supervisor-died')
        self.assert_pending(self.verify())
        path, before, proof = self.recovery_proof()
        self.assertFalse((Path(before['stage']) / 'supervision-finished').exists())
        self.assert_pending(self.admitted())
        self.assert_ok(self.resolve_remote(proof))
        self.assertEqual(read_json(path)['runs'][-1]['receipts'], before['receipts'])
        self.assertTrue(Path(before['stage']).exists())
        self.assert_ok(self.admitted())

    def test_unreachable_supervisor_is_not_recovery_authority(self):
        self.slurm_policy()
        self.authorize_both(PYTHON + 'raise SystemExit(0)\n')
        self.mode.write_text('slurm-supervisor-died')
        self.assert_pending(self.verify())
        path, before, proof_path = self.recovery_proof()
        proof = read_json(proof_path)
        proof['supervisor']['status'] = 'unreachable'
        proof_path.write_text(json.dumps(proof))
        self.mode.write_text('cleanup-ssh-fail')
        result = self.resolve_remote(proof_path)
        self.assert_failed(result)
        self.assertIn('unreachable or missing evidence is not proof', result.stderr)
        self.assertNotIn('recovery_authority', read_json(path)['runs'][-1])
        self.assertEqual(read_json(path)['runs'][-1]['receipts'], before['receipts'])
        self.assert_pending(self.admitted())
        self.assertEqual(self.merges(), [])

    def test_accounting_expiry_requires_authority_after_positive_observation(self):
        self.slurm_policy()
        self.authorize_both(PYTHON + 'raise SystemExit(0)\n')
        # Lose only the coordinator cleanup connection after the supervisor's
        # positive lifecycle observation, retaining the real stage.
        self.write_ssh(SSH.replace("else:\n    if pathlib.Path", "else:\n    if 'from remote_verify import cleanup' in command: raise SystemExit(255)\n    if pathlib.Path"))
        self.assert_ok(self.verify())
        path, before, proof = self.recovery_proof()
        self.assertTrue(before['reconciled'])
        self.assertTrue(Path(before['stage']).exists())
        self.write_ssh(SSH)
        (self.f.bin / 'sacct').write_text(PYTHON + 'raise SystemExit(0)\n')
        (self.f.bin / 'squeue').write_text(PYTHON + 'raise SystemExit(0)\n')
        self.assert_pending(self.verify('--retrieve-remote-evidence'))
        self.assert_pending(self.admitted())
        self.assert_ok(self.resolve_remote(proof))
        self.assertEqual(read_json(path)['runs'][-1]['receipts'], before['receipts'])
        self.assertTrue(Path(before['stage']).exists())
        self.assert_ok(self.admitted())

    def test_null_marker_cannot_erase_recorded_success_before_accounting_expiry(self):
        self.cancellation_fixture('pass')
        self.authorize_both(PYTHON + 'raise SystemExit(0)\n')
        self.assert_pending(self.verify('--verification-timeout', '1'))
        path, initial, proof = self.recovery_proof()
        stage = Path(initial['stage'])
        marker = read_json(stage / 'supervision-finished')
        self.assertIsNone(marker['sacct_state'])
        (self.f.bin / 'sacct').write_text(PYTHON + SACCT)
        self.write_ssh(SSH.replace("else:\n    if pathlib.Path",
            "else:\n    if 'from remote_verify import cleanup' in command: raise SystemExit(255)\n    if pathlib.Path"))
        self.assert_pending(self.verify('--retrieve-remote-evidence'))
        observed = read_json(path)['runs'][-1]
        self.assertEqual(observed['execution']['sacct_state'], 'COMPLETED')
        self.write_ssh(SSH)
        (self.f.bin / 'sacct').write_text(PYTHON + 'raise SystemExit(0)\n')
        (self.f.bin / 'squeue').write_text(PYTHON + 'raise SystemExit(0)\n')
        self.assert_pending(self.verify('--retrieve-remote-evidence'))
        self.assert_pending(self.admitted())
        (self.f.bin / 'ssh').unlink()  # Recovery and publication use only saved evidence.
        self.assert_ok(self.resolve_remote(proof))
        saved = read_json(path)['runs'][-1]
        self.assertEqual(saved['receipts'], initial['receipts'])
        self.assertTrue(saved['published'])
        self.assertTrue(stage.exists())
        self.assertEqual(read_json(stage / 'supervision-finished'), marker)
        self.assert_ok(self.admitted())

    def test_slurm_negative_cleanup_blocks_operator_admission_and_recovers(self):
        self.slurm_policy()
        self.program()
        original = "else:\n    if pathlib.Path(os.environ['REMOTE_MODE']).read_text() in ('cleanup-ssh-fail', 'ssh-lost-both'):"
        requeue = "else:\n    if 'from remote_verify import cleanup' in command:\n        pathlib.Path(os.environ['REMOTE_MODE']).write_text('slurm-running')\n    if pathlib.Path(os.environ['REMOTE_MODE']).read_text() in ('cleanup-ssh-fail', 'ssh-lost-both'):"
        self.assertIn(original, SSH)
        self.write_ssh(SSH.replace(original, requeue))
        self.assert_pending(self.verify('--verification-timeout', '1'))
        self.assert_failed(self.admitted())
        row, = self.rows()
        self.assertEqual(row['result'], 'pass')
        self.assertFalse(row['execution']['quiescent'])
        self.assertFalse(row['execution']['evidence_reconciled'])
        stage = Path(row['execution']['stage'])
        claim = (stage / 'claim-0.json').read_bytes()
        self.write_ssh(SSH)
        self.mode.write_text('pass')
        self.assert_ok(self.verify('--retrieve-remote-evidence'))
        path = RV.ledger_path(self.f.state_dir, 'u',
                             {key: row[key] for key in V.MERGE_BASIS_FIELDS})
        self.assertEqual(read_json(path)['runs'][-1]['receipts']['0'], json.loads(claim))
        self.assert_ok(self.admitted())
        self.assertEqual(self.ssh_launches(), 1)
        self.assertFalse(stage.exists())

    def test_slurm_partial_failure_survives_later_worker_loss_and_blocks_rerun(self):
        self.slurm_policy()
        self.mode.write_text('slurm-worker-died')
        self.test_partial_failure_survives_later_worker_loss_and_blocks_local_escape()
        first, later = self.rows()
        self.assertEqual(first['execution']['job_id'], '321')
        self.assertEqual(first['execution']['cleanup'], 'unconfirmed')
        self.assertEqual(first['execution']['sacct_state'], 'FAILED')

    def test_missing_ledger_blocks_unresolved_pass_and_restoration_recovers(self):
        target = self.program()
        self.mode.write_text('ssh-lost-response')
        old = "    if mode in ('ssh-lost-response', 'ssh-lost-both', 'ssh-complete-255'): raise SystemExit(255)"
        new = ("    for marker in list(pathlib.Path(os.environ['REMOTE_ROOT']).glob('verify-*/*')):\n"
               "        if marker.name in ('worker-complete', 'supervision-finished'):\n"
               "            marker.rename(marker.with_name(marker.name + '.saved'))\n") + old
        self.assertIn(old, SSH)
        self.write_ssh(SSH.replace(old, new))
        self.f.env['REMOTE_ROOT'] = str(self.remote_root)
        self.assertNotEqual(self.verify().returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'pass')
        self.assertIs(row['execution']['evidence_reconciled'], False)
        stage = Path(row['execution']['stage'])
        ledger, = (self.f.state_dir / 'remote-verifications').glob('*.json')
        saved = ledger.read_bytes()
        ledger.unlink()
        for operation in (self.admitted, self.verify):
            refused = operation()
            self.assert_pending(refused)
            self.assertIn(str(stage), refused.stderr)
        self.local_only()
        refused = self.verify()
        self.assert_pending(refused)
        generic = self.generic_verify(target)
        self.assert_pending(generic)
        self.assertEqual(self.merges(), [])
        self.assertTrue(stage.exists())
        ledger.write_bytes(saved)
        for marker in stage.glob('*.saved'):
            marker.rename(marker.with_name(marker.name[:-6]))
        self.mode.write_text('pass')
        retrieved = self.verify('--retrieve-remote-evidence')
        self.assert_ok(retrieved)
        # The original row is immutable; admission must consult recovered
        # authority, not permanently reject its now-stale reconciliation flag.
        self.assertEqual(len(self.rows()), 1)
        self.assertIs(self.rows()[0]['execution']['evidence_reconciled'], False)
        self.assertFalse(stage.exists())
        admitted = self.admitted()
        self.assert_ok(admitted)
        self.assertEqual(self.ssh_launches(), 1)
        self.assertEqual(len(self.merges()), 1)

    def test_missing_ledger_cannot_escape_unretrieved_failure(self):
        self.program('raise SystemExit(125 * int(Path(%r).read_text() != "pass"))\n'
                     % str(self.mode))
        self.mode.write_text('ssh-lost-both')
        self.assertNotEqual(self.verify().returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        stage = Path(row['execution']['stage'])
        ledger, = (self.f.state_dir / 'remote-verifications').glob('*.json')
        saved = ledger.read_bytes()
        ledger.unlink()
        self.mode.write_text('pass')
        refused = self.verify()
        self.assert_pending(refused)
        self.assertTrue(stage.exists())
        ledger.write_bytes(saved)
        retrieved = self.verify('--retrieve-remote-evidence')
        self.assertNotEqual(retrieved.returncode, 0)
        self.assertEqual((self.rows()[-1]['result'], self.rows()[-1]['exit_code']), ('fail', 125))
        self.f.assert_refused(self.admitted())
        self.assertEqual(self.ssh_launches(), 1)

    def test_retained_merge_reconciliation_refuses_lost_remote_ledger(self):
        self.program()
        result = self.verify()
        self.assert_ok(result)
        ledger, = (self.f.state_dir / 'remote-verifications').glob('*.json')
        saved = ledger.read_bytes()
        self.f.forge['fail_view_once'] = True
        self.f.save()
        self.assertNotEqual(self.admitted().returncode, 0)
        self.assertEqual(len(self.merges()), 1)
        ledger.unlink()
        refused = self.admitted()
        self.assert_failed(refused)
        self.assertEqual(self.f.intent()['integration_status'], 'integration-unverified')
        self.assertIn('unresolved remote evidence at', refused.stderr)
        self.assertEqual(len(self.merges()), 1)
        ledger.write_bytes(saved)
        restored = self.admitted()
        self.assert_ok(restored)
        self.assertEqual(self.f.intent()['integration_status'], 'candidate-verified')
        self.assertEqual(len(self.merges()), 1)

    def test_lost_launch_ledger_before_receipt_publication_blocks_new_run(self):
        changed = dict(self.policy, local={})
        self.program('Path(%r).write_text(%r)\n' % (
            str(self.f.state_dir / RV.POLICY), json.dumps(changed)))
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('execution policy changed during verification', result.stderr)
        self.assertEqual(self.rows(), [])
        self.save_policy()
        ledger, = (self.f.state_dir / 'remote-verifications').glob('*.json')
        saved = ledger.read_bytes()
        ledger.unlink()
        refused = self.verify()
        self.assert_pending(refused)
        self.assertEqual(self.ssh_launches(), 1)
        ledger.write_bytes(saved)
        self.save_policy()
        retrieved = self.verify('--retrieve-remote-evidence')
        self.assert_ok(retrieved)
        self.assertEqual(self.rows()[-1]['result'], 'pass')

    def test_completed_receipts_survive_missing_aggregate_and_cleanup_disconnect(self):
        self.program('raise SystemExit(125)\n')
        self.mode.write_text('ssh-lost-both')
        self.assertNotEqual(self.verify().returncode, 0)
        stage = Path(self.rows()[-1]['execution']['stage'])
        (stage / 'result.json').unlink()
        self.mode.write_text('pass')
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.rows()[-1]['result'], self.rows()[-1]['exit_code']), ('fail', 125))
        self.assertFalse(stage.exists())

    def test_older_ledger_and_missing_required_witness_refuse_admission(self):
        self.program()
        self.assertEqual(self.verify().returncode, 0)
        ledger, = (self.f.state_dir / 'remote-verifications').glob('*.json')
        older = ledger.read_bytes()
        self.assertEqual(self.verify().returncode, 0)
        saved = ledger.read_bytes()
        current = json.loads(saved)
        empty = dict(current, runs=[])
        for damaged in (older, json.dumps(empty).encode()):
            ledger.write_bytes(damaged)
            refused = self.admitted()
            self.assert_pending(refused)
            self.assertEqual(self.merges(), [])
        ledger.write_bytes(saved)
        witness = ledger.with_suffix('.launches') / (current['runs'][-1]['launch_id'] + '.json')
        original = witness.read_bytes()
        witness.unlink()
        for operation in (self.admitted, self.verify):
            refused = operation()
            self.assert_failed(refused)
            self.assertIn('restore the required launch witness', refused.stderr)
        witness.write_bytes(original)
        admitted = self.admitted()
        self.assert_ok(admitted)
        self.assertEqual(self.ssh_launches(), 2)

    def test_legacy_remote_journal_blocks_loss_of_pre_witness_ledger(self):
        self.program('raise SystemExit(125 * int(Path(%r).read_text() != "pass"))\n'
                     % str(self.mode))
        self.mode.write_text('ssh-lost-both')
        self.assertNotEqual(self.verify().returncode, 0)
        ledger, = (self.f.state_dir / 'remote-verifications').glob('*.json')
        legacy = read_json(ledger)
        for run in legacy['runs']:
            run.pop('witness_required')
        shutil.rmtree(ledger.with_suffix('.launches'))
        ledger.write_text(json.dumps(legacy))
        self.assertNotEqual(self.verify().returncode, 0)
        ledger.unlink()
        self.mode.write_text('pass')
        refused = self.verify()
        self.assert_failed(refused)
        self.assertIn('restore the matching coordinator ledger', refused.stderr)
        ledger.write_text(json.dumps(legacy))
        self.assertNotEqual(self.verify('--retrieve-remote-evidence').returncode, 0)
        self.assertEqual((self.rows()[-1]['result'], self.rows()[-1]['exit_code']), ('fail', 125))
        self.f.assert_refused(self.admitted())
        self.assertEqual(self.ssh_launches(), 1)

    def test_cleanup_disconnect_does_not_downgrade_completed_pass(self):
        self.program()
        self.mode.write_text('cleanup-ssh-fail')
        result = self.verify()
        self.assert_ok(result)
        row, = self.rows()
        self.assertEqual(row['result'], 'pass')
        self.assertEqual(row['execution']['cleanup'], 'unconfirmed')
        stage = Path(row['execution']['stage'])
        self.assertTrue((stage / 'claim-0.json').exists())
        self.assertTrue((stage / 'worker-complete').exists())
        self.mode.write_text('pass')
        for _ in range(2):
            result = self.verify('--retrieve-remote-evidence')
            self.assert_ok(result)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(stage.exists())
        self.assertEqual(self.ssh_launches(), 1)

    def test_slurm_policy_without_mem_is_refused_before_transport(self):
        self.slurm_policy()
        del self.policy['verification_host']['slurm']['mem']
        self.save_policy()
        result = self.f.invoke('--verify-integration')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('slurm requires partition, mem and time', result.stderr)
        self.assertFalse(Path(self.f.env['REMOTE_LOG']).exists())
        self.assertEqual(self.rows(), [])

    def test_slurm_first_cleanup_observation_reconciles_in_one_operator_call(self):
        self.assert_first_cleanup_reconciles()

    def test_slurm_complete_reingest_frames_with_transport_error_still_admit(self):
        write_ssh = self.write_ssh
        def transport_after_frames(script):
            call = "    result = subprocess.run(['/bin/sh', '-c', command])"
            disconnect = ("\n    if 'request-header' in command:\n"
                          "        for record in pathlib.Path(os.environ['REMOTE_LEDGER_DIR']).glob('*.json'):\n"
                          "            for run in json.loads(record.read_text())['runs']:\n"
                          "                history = pathlib.Path(run['stage']) / 'cleanup.json'\n"
                          "                if history.is_file() and json.loads(history.read_text()).get('quiescent') is True:\n"
                          "                    raise SystemExit(255)\n")
            self.assertEqual(script.count(call), 1)
            write_ssh(script.replace(call, call + disconnect))
        with mock.patch.object(self, 'write_ssh', side_effect=transport_after_frames):
            run = self.assert_first_cleanup_reconciles()
        self.assertIn('transport_error', run)
        self.assertIn('255', run['transport_error'])

    def assert_first_cleanup_reconciles(self):
        self.slurm_policy()
        self.authorize_both(PYTHON + 'raise SystemExit(0)\n')
        # Model an older supervisor that publishes completion but leaves the
        # final scheduler observation to the coordinator's cleanup connection.
        launch = "    result = subprocess.run(['/bin/sh', '-c', command], input=raw,"
        delayed = ("    dst = io.BytesIO()\n"
                   "    with tarfile.open(fileobj=io.BytesIO(raw)) as source, tarfile.open(fileobj=dst, mode='w') as dest:\n"
                   "        for member in source:\n"
                   "            data = source.extractfile(member).read()\n"
                   "            if member.name == 'remote_verify.py':\n"
                   "                old = b'        cleanup(stage)\\n'\n"
                   "                assert data.count(old) == 1\n"
                   "                data = data.replace(old, b'        pass\\n')\n"
                   "                member.size = len(data)\n"
                   "            dest.addfile(member, io.BytesIO(data))\n"
                   "    raw = dst.getvalue()\n")
        self.assertIn(launch, SSH)
        self.write_ssh(SSH.replace(launch, delayed + launch))
        result = self.verify()
        self.assert_ok(result)
        self.assertEqual(self.ssh_launches(), 1)
        self.assertEqual(self.merges(), [])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row['result'], 'pass')
            self.assertTrue(row['execution']['evidence_reconciled'])
            self.assertEqual(row['execution']['cleanup'], 'removed')
            self.assertFalse(Path(row['execution']['stage']).exists())
        path = RV.ledger_path(self.f.state_dir, 'u',
                             {key: rows[0][key] for key in V.MERGE_BASIS_FIELDS})
        run, = read_json(path)['runs']
        self.assertTrue(run['reconciled'])
        self.assertTrue(run['published'])
        self.assertEqual(set(run['receipts']), {'0', '1'})
        return run

    def test_slurm_runs_both_claims_with_target_repetitions_and_handshake(self):
        self.slurm_policy()
        self.shared.install_policy(repetitions=2)
        self.shared.candidate({'tests/test_remote.py': self.shared.counter_test()})
        result = self.verify()
        self.assert_ok(result)
        integration, stable = self.rows()
        self.assertEqual(self.shared.counter.read_text(), '2')
        self.assertEqual(stable['repetitions'], 2)
        for row in (integration, stable):
            self.assertEqual(row['execution']['job_id'], '321')
            self.assertEqual(row['execution']['sacct_state'], 'COMPLETED')
            self.assertEqual(row['execution']['sacct_exit_code'], '0:0')
            self.assertEqual(row['candidate_tree'], row['execution']['verified_tree'])
        self.assert_clean()
        self.assertEqual(self.admitted().returncode, 0)

    def test_tampered_bundle_refuses_before_any_verifier_runs(self):
        self.program()
        self.f.git('checkout', '-q', '--detach', self.f.head)
        (self.f.repo / 'change.txt').write_text('tampered\n')
        self.f.git('commit', '-qam', 'tampered transfer fixture')
        self.f.git('update-ref', RV.BUNDLE_REF, 'HEAD')
        bundle = self.f.directory / 'tampered.bundle'
        self.f.git('bundle', 'create', str(bundle), RV.BUNDLE_REF)
        self.f.git('checkout', '-q', 'swarm-a1')
        self.f.env['TAMPER_BUNDLE'] = str(bundle)
        self.mode.write_text('tamper')
        result = self.verify()
        self.assert_failed(result)
        self.assertFalse(self.witness.exists())
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        self.assertIn('tree digest mismatch', row['incomplete_reason'])
        self.assert_clean()

    def assert_retry_admits(self, mode, tail=''):
        self.program(tail)
        self.mode.write_text(mode)
        first = self.verify('--verification-timeout', '1')
        self.assert_failed(first)
        incomplete = self.rows()[-1]
        self.f.assert_refused(self.admitted())
        self.mode.write_text('pass')
        second = self.verify('--verification-timeout', '10')
        self.assert_ok(second)
        for field in V.MERGE_BASIS_FIELDS:
            self.assertEqual(incomplete[field], self.rows()[-1][field])
        self.assert_clean()
        admitted = self.admitted()
        self.assert_ok(admitted)
        self.assertEqual(incomplete['result'], 'incomplete')

    def test_ssh_failure_without_retrievable_stage_blocks_rerun(self):
        self.program()
        self.mode.write_text('ssh-fail')
        self.assertNotEqual(self.verify().returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        self.mode.write_text('pass')
        result = self.verify()
        self.assert_pending(result)
        self.assertFalse(self.witness.exists())
        self.assertEqual(self.rows(), [row])
        self.f.assert_refused(self.admitted())

    def test_remote_timeout_then_pass_for_same_binding_is_admitted(self):
        tail = ('import time\nif Path(%r).read_text() == "timeout": time.sleep(30)\n'
                % str(self.mode))
        self.assert_retry_admits('timeout', tail)

    def test_completed_remote_fail_poisoning_survives_later_pass(self):
        self.program('raise SystemExit(125 * int(Path(%r).read_text() == "fail"))\n' % str(self.mode))
        self.mode.write_text('fail')
        self.assertNotEqual(self.verify().returncode, 0)
        self.assertEqual(self.rows()[-1]['result'], 'fail')
        self.assertEqual(self.rows()[-1]['exit_code'], 125)
        self.mode.write_text('pass')
        result = self.verify()
        self.assert_ok(result)
        refused = self.admitted()
        self.f.assert_refused(refused)
        self.assertIn('FAIL', refused.stderr)
        self.assert_clean()

    def test_completed_verifier_fail_survives_a_failed_slurm_job(self):
        self.slurm_policy()
        self.program('raise SystemExit(int(Path(%r).read_text() == "slurm-completed-fail"))\n'
                     % str(self.mode))
        self.mode.write_text('slurm-completed-fail')
        self.assertNotEqual(self.verify().returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'fail')
        self.assertEqual(row['execution']['sacct_state'], 'FAILED')
        self.mode.write_text('pass')
        result = self.verify()
        self.assert_ok(result)
        result = self.admitted()
        self.f.assert_refused(result)
        self.assertIn('FAIL', result.stderr)
        self.assert_clean()

    def test_forced_scheduler_restart_cannot_overwrite_completed_failure(self):
        self.slurm_policy()
        self.program('raise SystemExit(int(Path(%r).read_text() == "slurm-forced-requeue"))\n'
                     % str(self.mode))
        self.mode.write_text('slurm-forced-requeue')
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'fail')
        self.assertEqual(self.mode.read_text(), 'pass')
        result = self.verify()
        self.assert_ok(result)
        result = self.admitted()
        self.f.assert_refused(result)
        self.assertIn('FAIL', result.stderr)
        self.assert_clean()

    def assert_slurm_unresolved(self, mode):
        self.program()
        self.mode.write_text(mode)
        result = self.verify('--verification-timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        stage = Path(row['execution']['stage'])
        self.assertTrue(stage.exists())
        self.mode.write_text('pass')
        retry = self.verify('--verification-timeout', '1')
        self.assert_pending(retry)
        self.assertEqual(self.rows(), [row])
        self.assertTrue(stage.exists())
        calls = [json.loads(line) for line in Path(self.f.env['SCHED_LOG']).read_text().splitlines()]
        self.assertEqual(sum(call[0] == 'sbatch' for call in calls), 1)

    def test_slurm_without_completed_worker_retains_evidence_and_blocks_retry(self):
        self.slurm_policy()
        self.assert_slurm_unresolved('slurm-missing')

    def test_slurm_without_terminal_state_retains_evidence_and_blocks_retry(self):
        self.slurm_policy()
        self.assert_slurm_unresolved('slurm-pending')

    def test_slurm_running_at_timeout_retains_stage_and_blocks_retry(self):
        self.slurm_policy()
        self.assert_slurm_unresolved('slurm-running')
        row, = self.rows()
        self.assertEqual(row['execution']['job_id'], '321')
        self.assertEqual(row['execution']['cleanup'], 'unconfirmed')

    def test_slurm_lost_submission_response_discovers_pending_job(self):
        self.slurm_policy()
        self.assert_slurm_unresolved('slurm-lost-job-id')
        row, = self.rows()
        stage = Path(row['execution']['stage'])
        self.assertFalse((stage / 'job-id').exists())
        self.assertEqual(row['execution']['job_id'], '321')
        self.assertEqual(row['execution']['job_ids'], ['321'])
        self.assertEqual(row['execution']['cleanup'], 'unconfirmed')
        self.assertFalse(row['execution']['quiescent'])
        self.assertIn('job_name', row['execution'])

    def test_candidate_policy_cannot_choose_host_or_interpreter(self):
        self.shared.candidate({RV.POLICY: json.dumps({
            'schema_version': 1, 'verification_host': {'ssh_alias': 'candidate-host'},
            'local': {'python': '/candidate/python'}})})
        result = self.verify()
        self.assert_ok(result)
        self.assertEqual(self.rows()[-1]['execution']['ssh_alias'], 'fixture-host')
        self.assert_clean()

    def test_execution_policy_symlink_into_candidate_is_refused(self):
        self.shared.candidate({RV.POLICY: json.dumps(self.policy)})
        path = self.f.state_dir / RV.POLICY
        path.unlink()
        path.symlink_to(self.f.repo / RV.POLICY)
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a symlink', result.stderr)
        self.assertEqual(self.rows(), [])
        self.assertFalse(Path(self.f.env['REMOTE_LOG']).exists())

    def test_local_default_and_declared_executable_evidence(self):
        self.local_only()
        result = self.verify()
        self.assert_ok(result)
        execution = self.rows()[-1]['execution']
        self.assertEqual(execution['location'], 'local')
        self.assertEqual(execution['executables']['python']['path'], os.path.realpath(sys.executable))
        self.assertTrue(execution['executables']['git']['version'].startswith('git version'))
        self.assertFalse(Path(self.f.env['REMOTE_LOG']).exists())

    def test_declared_executables_are_used_for_checkout_and_verifier_children(self):
        log = self.f.directory / 'executables.log'
        for name in ('python', 'git'):
            actual = self.policy['verification_host'][name]
            wrapper = self.f.directory / ('declared-' + name)
            wrapper.write_text(PYTHON + 'import json, os, sys\n'
                               'with open(%r, "a") as log: log.write(json.dumps([%r] + sys.argv[1:]) + "\\n")\n'
                               'os.execv(%r, [%r] + sys.argv[1:])\n'
                               % (str(log), name, actual, actual))
            wrapper.chmod(0o755)
            self.policy['local'][name] = str(wrapper)
            self.policy['verification_host'][name] = str(wrapper)
        self.save_policy()
        self.shared.install_policy(repetitions=1)
        self.shared.candidate({'tests/test_executables.py': self.shared.counter_test()})
        result = self.verify()
        self.assert_ok(result)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(any(c[0] == 'python' and any('verifier' in a for a in c[1:]) for c in calls))
        self.assertTrue(any(c[0] == 'git' and 'checkout' in c for c in calls))
        self.assertTrue(any(c[0] == 'git' and 'diff' in c for c in calls))
        for row in self.rows():
            for name in ('python', 'git'):
                executable = row['execution']['executables'][name]
                self.assertEqual(executable['path'], self.policy['verification_host'][name])
                self.assertTrue(executable['version'])
        self.assert_clean()

    def test_default_policy_admission_ignores_ambient_git(self):
        (self.f.state_dir / RV.POLICY).unlink()
        result = self.verify()
        self.assert_ok(result)

        git = self.f.bin / 'git'
        actual = str(git.resolve())
        git.unlink()
        git.write_text(PYTHON + 'import os, sys\n'
                       'if "show" in sys.argv and sys.argv[-1].endswith(":verifiers.json"):\n'
                       '    raise SystemExit("ambient Git must not read policy")\n'
                       'os.execv(%r, [%r] + sys.argv[1:])\n' % (actual, actual))
        git.chmod(0o755)
        result = self.admitted()
        self.assert_ok(result)

    def test_default_git_falls_back_to_external_operator_path(self):
        (self.f.state_dir / RV.POLICY).unlink()
        self.empty_system_path()
        result = self.verify()
        self.assert_ok(result)
        row, = self.rows()
        self.assertEqual(row['execution']['executables']['git']['path'],
                         str((self.f.bin / 'git').resolve()))
        result = self.admitted()
        self.assert_ok(result)

    def test_fallback_git_excludes_candidate_and_external_symlink(self):
        (self.f.state_dir / RV.POLICY).unlink()
        marker = self.f.directory / 'candidate-git-selected'
        actual = str((self.f.bin / 'git').resolve())
        program = (PYTHON + 'import os, sys\nfrom pathlib import Path\n'
                   'if "--version" in sys.argv or ("show" in sys.argv and sys.argv[-1].endswith(":verifiers.json")):\n'
                   '    Path(%r).write_text("candidate policy tool selected")\n'
                   'os.execv(%r, [%r] + sys.argv[1:])\n' % (str(marker), actual, actual))
        self.shared.candidate({'git': program})
        (self.f.repo / 'git').chmod(0o755)
        self.shared.candidate({'git-mode.txt': 'record executable mode\n'})
        linked = self.f.directory / 'linked-tools'
        linked.mkdir()
        (linked / 'git').symlink_to(self.f.repo / 'git')
        self.empty_system_path()
        self.f.env['PATH'] = os.pathsep.join(('.', str(self.f.repo), str(linked), self.f.env['PATH']))
        result = self.verify()
        self.assert_ok(result)
        self.assertFalse(marker.exists())
        self.assertEqual(self.rows()[-1]['execution']['executables']['git']['path'], actual)
        generic = self.generic_verify(self.f.base)
        self.assert_ok(generic)
        self.assertFalse(marker.exists())
        self.assertEqual(self.rows()[-1]['execution']['executables']['git']['path'], actual)
        result = self.admitted()
        self.assert_ok(result)
        self.assertFalse(marker.exists())

    def test_absent_ssh_client_does_not_create_an_unresolved_launch(self):
        self.program()
        ssh = self.f.bin / 'ssh'
        content = ssh.read_bytes()
        ssh.unlink()
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        self.assertIn('ssh is unavailable', row['incomplete_reason'])
        self.assertEqual(list((self.f.state_dir / 'remote-verifications').glob('*.json')), [])
        self.assertFalse(Path(self.f.env['REMOTE_LOG']).exists())
        ssh.write_bytes(content)
        ssh.chmod(0o755)
        result = self.verify()
        self.assert_ok(result)
        self.assertEqual(self.rows()[-1]['result'], 'pass')
        self.assert_clean()
    def test_generic_integration_policy_reads_and_admission_use_declared_git(self):
        self.policy.pop('verification_host')
        log = self.f.directory / 'generic-git.log'
        actual = self.policy['local']['git']
        wrapper = self.f.directory / 'generic-git'
        wrapper.write_text(PYTHON + 'import json, os, sys\n'
                           'with open(%r, "a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                           'os.execv(%r, [%r] + sys.argv[1:])\n' % (str(log), actual, actual))
        wrapper.chmod(0o755)
        self.policy['local']['git'] = str(wrapper)
        self.save_policy()
        result = self.generic_verify(self.f.base)
        self.assert_ok(result)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(any('show' in c and self.f.base + ':' + V.POLICY_FILE in c for c in calls))
        before = sum('checkout' in c for c in calls)
        row, = self.rows()
        receipt, error = S.admit_verification(
            self.f.state_dir, 'u', V.INTEGRATION_CLAIM, self.f.head,
            row['policy_sha256'], self.f.policy, repo=self.f.repo,
            base_commit=self.f.base, target_commit=self.f.base)
        self.assertIsNone(error, error)
        self.assertEqual(receipt['execution']['executables']['git']['path'], str(wrapper))
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertGreater(sum('checkout' in c for c in calls), before)

    def test_local_verifier_preserves_other_declared_host_path_tools(self):
        self.local_only()
        helper = self.f.bin / 'fixture-host-helper'
        helper.write_text(PYTHON + 'print("HOST_HELPER_RAN")\n')
        helper.chmod(0o755)
        self.f.env.update(OPENAI_API_KEY='fixture-secret', SSH_AUTH_SOCK='fixture-agent')
        self.program('import os, subprocess\n'
                     'assert "OPENAI_API_KEY" not in os.environ\n'
                     'assert "SSH_AUTH_SOCK" not in os.environ\n'
                     'subprocess.run(["fixture-host-helper"], check=True)\n')
        result = self.verify()
        self.assert_ok(result)
        self.assertIn('HOST_HELPER_RAN', self.rows()[-1]['stdout_tail'])

    def test_child_launcher_exec_failure_is_incomplete_and_retryable(self):
        self.local_only()
        self.program()
        site = self.f.directory / 'launcher-fault'
        site.mkdir()
        (site / 'sitecustomize.py').write_text(
            'import subprocess, sys\n'
            'if sys.argv[0].endswith("merge_unit.py"):\n'
            '    original = subprocess.Popen\n'
            '    def launch(argv, *args, **kwargs):\n'
            '        if "-c" in argv and any("pinned-verifier-" in x for x in argv):\n'
            '            argv = list(argv)\n'
            '            argv[argv.index("-c") + 6] = "/missing-fixture-executable"\n'
            '        return original(argv, *args, **kwargs)\n'
            '    subprocess.Popen = launch\n')
        self.f.env['PYTHONPATH'] = str(site)
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.witness.exists())
        self.assertEqual(self.rows()[-1]['result'], 'incomplete')
        self.assertIn('child launcher', self.rows()[-1]['incomplete_reason'])
        self.f.env.pop('PYTHONPATH')
        result = self.verify()
        self.assert_ok(result)
        result = self.admitted()
        self.assert_ok(result)

    def test_verifier_cannot_report_a_launcher_error(self):
        self.local_only()
        self.program(
            'import os, stat\n'
            'for name in os.listdir("/dev/fd"):\n'
            '    fd = int(name)\n'
            '    if fd <= 2: continue\n'
            '    try:\n'
            '        if stat.S_ISFIFO(os.fstat(fd).st_mode):\n'
            '            os.write(fd, b"verifier must not attest a launcher error")\n'
            '    except OSError: pass\n'
            'raise SystemExit(125)\n')
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual((row['result'], row['exit_code']), ('fail', 125))
        self.assertNotIn('incomplete_reason', row)
        self.f.assert_refused(self.admitted())

    def test_changed_remote_module_without_handshake_fails(self):
        self.shared.install_policy(repetitions=2)
        self.shared.candidate({'tests/test_exit.py': 'raise SystemExit(0)\n'})
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        stable = self.rows()[-1]
        self.assertEqual(stable['claim'], V.STABILITY_CLAIM)
        self.assertEqual(stable['result'], 'fail')
        self.assertIn('missing or malformed', stable['stderr_tail'])
        self.f.assert_refused(self.admitted())
        self.assert_clean()

    def test_policy_change_during_remote_execution_prevents_publication(self):
        changed = dict(self.policy, local={})
        self.program('Path(%r).write_text(%r)\n' % (
            str(self.f.state_dir / RV.POLICY), json.dumps(changed)))
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('execution policy changed during verification', result.stderr)
        self.assertEqual(self.rows(), [])
        self.assert_clean()
        self.save_policy()
        retrieved = self.verify('--retrieve-remote-evidence')
        self.assert_ok(retrieved)
        self.assertEqual(self.rows()[-1]['result'], 'pass')
        self.assertEqual(self.ssh_launches(), 1)

    def test_candidate_executable_path_in_coordinator_policy_is_refused(self):
        self.policy['local']['python'] = str(self.f.repo / 'python3')
        self.save_policy()
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inside the operated repository', result.stderr)
        self.assertFalse(Path(self.f.env['REMOTE_LOG']).exists())
        self.assertEqual(self.rows(), [])

    def test_candidate_executable_is_refused_during_receipt_admission(self):
        self.policy.pop('verification_host')
        original = dict(self.policy['local'])
        for name in ('python', 'git'):
            with self.subTest(executable=name):
                program = self.f.repo / ('candidate-' + name)
                marker = self.f.directory / ('candidate-executed-' + name)
                program.write_text(PYTHON + 'from pathlib import Path\n'
                                   'Path(%r).write_text("executed")\nprint("fixture version")\n' % str(marker))
                program.chmod(0o755)
                self.policy['local'] = dict(original, **{name: str(program)})
                self.save_policy()
                result = self.admitted()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists(), 'admission executed a candidate-supplied executable')
                self.assertIn('inside the operated repository', result.stderr)
                self.assertEqual(self.merges(), [])

    def test_missing_remote_execution_evidence_is_not_legacy_local(self):
        result = self.verify()
        self.assert_ok(result)
        row, = self.rows()
        row.pop('execution')
        self.journal.write_text(json.dumps(row) + '\n')
        result = self.admitted()
        self.f.assert_refused(result)
        self.assertIn('missing execution evidence', result.stderr)

    def test_retained_remote_digest_loss_corrects_persisted_verified_labels(self):
        result = self.verify()
        self.assert_ok(result)
        self.f.forge['queued'] = True
        self.f.save()
        self.assertNotEqual(self.admitted().returncode, 0)
        intent = self.f.intent()
        intent['preconditions']['integration']['execution'].pop('verified_tree')
        intent['integration_status'] = 'candidate-verified'
        path = self.f.state_dir / ('merge-unit-' + intent['operation_id'] + '.json')
        path.write_text(json.dumps(intent))
        self.f.forge['pr'].update(state='MERGED', mergeCommit={'oid': self.f.merged})
        self.f.us['merge_receipt'] = {
            'unit': 'u', 'repo': self.f.remote, 'pr': self.f.remote + '/pull/7',
            'target': 'main', 'head': self.f.head, 'merged_as': self.f.merged,
            'target_commit': self.f.base, 'integration_status': 'candidate-verified'}
        self.f.save()
        result = self.admitted()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.f.intent()['integration_status'], 'integration-unverified')
        self.assertEqual(self.f.receipts()[-1]['integration_status'], 'integration-unverified')
        saved = read_json(self.f.state_dir / S.STATE_FILE)
        self.assertEqual(saved['units']['u']['merge_receipt']['integration_status'],
                         'integration-unverified')
        self.assertEqual(saved['units']['u']['state'], 'READY_FOR_PR')
        self.assertNotIn(' advance ', result.stdout)
        self.assertEqual(len(self.merges()), 1)

    def test_remote_receipt_cannot_lose_verified_digest_on_admission(self):
        result = self.verify()
        self.assert_ok(result)
        row, = self.rows()
        row['execution'].pop('verified_tree')
        self.journal.write_text(json.dumps(row) + '\n')
        result = self.admitted()
        self.f.assert_refused(result)
        self.assertIn('tree digest', result.stderr)


class TestWriteOnceRemoteEvidence(unittest.TestCase):
    def test_completed_receipt_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claim-0.json'
            failure = {'exit_code': 125, 'completion': True}
            RV.publish(path, failure, once=True)
            before, inode = path.read_bytes(), path.stat().st_ino
            RV.publish(path, failure, once=True)
            self.assertEqual(path.stat().st_ino, inode)
            with self.assertRaisesRegex(ValueError, 'conflicting write-once'):
                RV.publish(path, {'exit_code': 0, 'completion': True}, once=True)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])


class TestSlurmLifecycle(unittest.TestCase):
    """Fast scheduler processes on a replaced PATH; no SSH or real scheduler."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.state = self.root / 'scheduler.json'
        self.log = self.root / 'calls.jsonl'
        self.launch = 'f' * 32
        self.name = 'verify-' + hashlib.sha256(self.launch.encode()).hexdigest()
        self.request = {'launch_id': self.launch, 'basis': {'candidate_tree': 'a' * 40},
                        'checks': [], 'verification_host': {'executor': 'slurm'}}
        RV.publish(self.stage / 'request.json', self.request)
        RV.publish(self.stage / 'supervision-finished', {'launch_id': self.launch, 'job_id': None})
        RV.publish(self.stage / 'worker-complete', {'launch_id': self.launch})
        body = r"""
import json, pathlib, sys
state_path, log_path = pathlib.Path(STATE), pathlib.Path(LOG)
state = json.loads(state_path.read_text())
command = pathlib.Path(sys.argv[0]).name
with log_path.open('a') as log: log.write(json.dumps([command] + sys.argv[1:]) + '\n')
if state.get('unavailable') == command: raise SystemExit(1)
if command == 'scancel':
    if state.get('cancel_exit', 1): raise SystemExit(state['cancel_exit'])
    if state.get('cancel_terminal'):
        state['jobs'][sys.argv[1]] = 'CANCELLED'
        state_path.write_text(json.dumps(state))
elif command == 'squeue':
    state['queue_reads'] = state.get('queue_reads', 0) + 1
    state_path.write_text(json.dumps(state))
    if state['queue_reads'] == 2 and state.get('final_queue_error'):
        raise SystemExit(1)
    if state['queue_reads'] == 2 and state.get('final_queue_ambiguous'):
        print('ambiguous'); raise SystemExit(0)
    for job, status in state['jobs'].items():
        if status in ('PENDING', 'RUNNING', 'COMPLETING') and not state.get('empty_queue'):
            print(job + '|' + NAME)
    if state.get('requeue_after_empty') and state['queue_reads'] == 1:
        state['jobs']['321'] = 'RUNNING'
        state_path.write_text(json.dumps(state))
elif '--name' in sys.argv:
    assert sys.argv[sys.argv.index('--name') + 1] == NAME
    assert '--starttime' in sys.argv
    state['name_reads'] = state.get('name_reads', 0) + 1
    if state.get('final_new_id') and state['name_reads'] == 2:
        state['jobs']['322'] = 'COMPLETED'
    state_path.write_text(json.dumps(state))
    for job in state['jobs']:
        if not (job == '322' and state.get('hide_new_id')): print(job + '|' + NAME)
else:
    job = sys.argv[sys.argv.index('-j') + 1]
    if job in state['jobs']:
        print(job + '|' + ('COMPLETED' if state.get('stale_terminal') else state['jobs'][job]) + '|0:0')
""".replace('STATE', repr(str(self.state))).replace('LOG', repr(str(self.log))).replace('NAME', repr(self.name))
        for command in ('sbatch', 'squeue', 'sacct', 'scancel'):
            path = self.bin / command
            path.write_text(PYTHON + body)
            path.chmod(0o755)
        env = mock.patch.dict(os.environ, {'PATH': str(self.bin)})
        env.start()
        self.addCleanup(env.stop)
        self.configure({'321': 'PENDING'})

    def configure(self, jobs, **options):
        self.state.write_text(json.dumps(dict(jobs=jobs, cancel_exit=1, **options)))

    def ack(self):
        return RV.evidence_digest(RV.collect(self.stage))

    def test_missing_job_id_discovers_pending_job_and_retains_stage(self):
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertTrue(self.stage.exists())
        self.assertEqual(result['job_id'], '321')
        self.assertEqual(result['job_ids'], ['321'])
        self.assertEqual(result['job_name'], self.name)
        self.assertFalse(result['quiescent'])
        self.assertEqual(read_json(self.stage / 'cleanup.json'), result)

    def test_missing_job_id_unqueryable_scheduler_retains_stage(self):
        for command in ('squeue', 'sacct'):
            with self.subTest(command=command):
                self.configure({'321': 'PENDING'}, unavailable=command)
                result = RV.cleanup(self.stage, self.ack())
                self.assertEqual(result['cleanup'], 'unconfirmed')
                self.assertTrue(self.stage.exists())
                self.assertIn(command, result['cleanup_error'])
                self.assertFalse(result['quiescent'])

    def test_nonterminal_accounting_cannot_authorize_removal(self):
        for status in ('PENDING', 'RUNNING', 'COMPLETING'):
            with self.subTest(status=status):
                self.configure({'321': status})
                result = RV.cleanup(self.stage, self.ack())
                self.assertEqual(result['cleanup'], 'unconfirmed')
                self.assertTrue(self.stage.exists())
                self.assertFalse(result['quiescent'])

    def test_nonterminal_accounting_with_empty_queue_retains_stage(self):
        self.configure({'321': 'PENDING'}, empty_queue=True)
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertTrue(self.stage.exists())
        self.assertFalse(result['quiescent'])

    def test_all_jobs_need_terminal_accounting(self):
        self.configure({'321': 'COMPLETED', '322': 'RUNNING'})
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['job_ids'], ['321', '322'])
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertTrue(self.stage.exists())

    def test_terminal_jobs_still_require_reconciliation(self):
        self.configure({'321': 'COMPLETED'})
        result = RV.cleanup(self.stage)
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertTrue(result['quiescent'])
        self.assertTrue(self.stage.exists())
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'removed')
        self.assertFalse(self.stage.exists())

    def test_dead_supervisor_still_reports_discovered_job(self):
        (self.stage / 'supervision-finished').unlink()
        result = RV.cleanup(self.stage)
        self.assertEqual(result['job_id'], '321')
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertTrue(self.stage.exists())

    def test_unreadable_job_id_uses_scheduler_identity(self):
        path = self.stage / 'job-id'
        for kind in ('directory', 'invalid-utf8'):
            with self.subTest(kind=kind):
                if kind == 'directory':
                    path.mkdir()
                else:
                    path.rmdir()
                    path.write_bytes(b'\xff')
                result = RV.cleanup(self.stage, self.ack())
                self.assertEqual(result['job_id'], '321')
                self.assertEqual(result['cleanup'], 'unconfirmed')
                self.assertTrue(self.stage.exists())

    def test_job_finishing_after_queue_snapshot_needs_no_cancellation(self):
        (self.bin / 'squeue').write_text(PYTHON + 'import json\nfrom pathlib import Path\n'
            'p=Path(%r)\ns=json.loads(p.read_text())\n'
            'if s["jobs"]["321"] != "COMPLETED":\n'
            ' print("321|" + %r)\n'
            ' s["jobs"]["321"]="COMPLETED"\n p.write_text(json.dumps(s))\n'
            % (str(self.state), self.name))
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'removed')
        self.assertTrue(result['quiescent'])
        self.assertEqual(result['cancellation'], 'not-required')
        calls = [json.loads(line)[0] for line in self.log.read_text().splitlines()]
        self.assertNotIn('scancel', calls)
        self.assertFalse(self.stage.exists())

    def test_failed_scancel_racing_with_completion_uses_terminal_evidence(self):
        (self.bin / 'scancel').write_text(PYTHON + 'import json\nfrom pathlib import Path\n'
            'p=Path(%r)\ns=json.loads(p.read_text())\n'
            's["jobs"]["321"]="COMPLETED"\np.write_text(json.dumps(s))\n'
            'raise SystemExit(1)\n' % str(self.state))
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'removed')
        self.assertTrue(result['quiescent'])
        self.assertEqual(result['cancellation'], 'unconfirmed')
        self.assertEqual(result['cancellation_attempts'][0]['exit_code'], 1)
        self.assertEqual(result['cleanup_sacct_states'], {'321': ['COMPLETED', '0:0']})
        self.assertFalse(self.stage.exists())

    def test_transient_request_error_cannot_bypass_slurm_discovery(self):
        (self.stage / 'job-id').write_text('321')
        RV.publish(self.stage / 'supervision-finished', {
            'launch_id': self.launch, 'job_id': '321',
            'sacct_state': 'COMPLETED', 'sacct_exit_code': '0:0'})
        ack = self.ack()
        original, failed = Path.read_text, []
        def transient(path, *args, **kwargs):
            if path == self.stage / 'request.json' and not failed:
                failed.append(True)
                raise OSError('transient request read')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', transient):
            result = RV.cleanup(self.stage, ack)
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertTrue(self.stage.exists())
        self.assertIn('cannot read cleanup request', result['cleanup_error'])
        recovered = RV.cleanup(self.stage, ack)
        self.assertEqual(recovered['job_id'], '321')
        self.assertEqual(recovered['cleanup'], 'unconfirmed')
        self.assertFalse(recovered['quiescent'])

    def test_slurm_cleanup_lock_prevents_scheduler_mutation(self):
        with (self.stage / 'cleanup.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(RV, '_command') as command:
                result = RV.cleanup(self.stage, self.ack())
            command.assert_not_called()
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertFalse((self.stage / 'cleanup.json').exists())
        self.assertTrue(self.stage.exists())

    def test_slurm_cancellation_requires_durable_intent(self):
        with mock.patch.object(RV, 'publish', side_effect=OSError('journal unavailable')):
            result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertIn('journal unavailable', result['cleanup_error'])
        self.assertTrue(self.stage.exists())
        calls = [json.loads(line)[0] for line in self.log.read_text().splitlines()]
        self.assertNotIn('scancel', calls)

    def test_slurm_cancellation_attempt_bound_retains_evidence(self):
        for _ in range(5):
            result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertEqual(len(result['cancellation_attempts']), 4)
        calls = [json.loads(line)[0] for line in self.log.read_text().splitlines()]
        self.assertEqual(calls.count('scancel'), 4)
        self.assertEqual(read_json(self.stage / 'cleanup.json'), result)
        self.assertTrue(self.stage.exists())

    def test_slurm_oversized_cancellation_history_is_retained(self):
        path = self.stage / 'cleanup.json'
        original = ' ' * 65537
        path.write_text(original)
        with mock.patch.object(RV, '_command') as command:
            result = RV.cleanup(self.stage, self.ack())
        command.assert_not_called()
        self.assertEqual(path.read_text(), original)
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertTrue(self.stage.exists())

    def completed_claims(self):
        self.request['verification_host']['ssh_alias'] = 'fixture-host'
        self.request['checks'] = [{'binding': {'claim': str(i)}, 'digest': str(i) * 64}
                                  for i in range(2)]
        execution = {'location': 'remote', 'executor': 'slurm', 'ssh_alias': 'fixture-host',
                     'host_identity': 'fixture-node', 'verified_tree': 'a' * 40,
                     'executables': {key: {'path': '/fixture/' + key, 'version': 'fixture',
                                          'role': 'configured-interpreter'}
                                     for key in ('python', 'git')}}
        receipts, outcomes = {}, []
        for i, check in enumerate(self.request['checks']):
            outcome = {'exit_code': 125 if i == 0 else 0, 'stdout': '', 'stderr': ''}
            receipt = RV.completion_receipt(self.request, check, outcome, execution)
            RV.publish(self.stage / ('claim-%s.json' % i), receipt, once=True)
            receipts[str(i)] = RV.record_digest(receipt)
            outcomes.append(outcome)
        RV.publish(self.stage / 'request.json', self.request)
        RV.publish(self.stage / 'worker-complete', {'launch_id': self.launch,
                   'basis': self.request['basis'], 'receipts': receipts, 'outcomes': outcomes})

    def test_completed_fail_survives_every_scheduler_state(self):
        self.completed_claims()
        original = (self.stage / 'claim-0.json').read_bytes()
        for status in ('PENDING', 'RUNNING', 'FAILED', 'TIMEOUT', 'NODE_FAIL', 'OUT_OF_MEMORY', 'COMPLETED'):
            with self.subTest(status=status):
                self.configure({'321': status})
                result = RV.cleanup(self.stage)
                self.assertEqual(result['cleanup'], 'unconfirmed')
                self.assertTrue(self.stage.exists())
                run = {'request': self.request, 'launch_id': self.launch,
                       'stage': str(self.stage), 'receipts': {}, 'execution': {}}
                RV.ingest(run, RV.collect(self.stage))
                self.assertEqual(run['receipts']['0']['outcome']['exit_code'], 125)
                self.assertNotIn('incomplete_reason', run['receipts']['0']['outcome'])
                self.assertEqual(run['reconciled'], status in RV.TERMINAL)
                self.assertEqual((self.stage / 'claim-0.json').read_bytes(), original)

    def test_zero_scancel_does_not_mean_terminal(self):
        self.state.write_text(json.dumps({'jobs': {'321': 'RUNNING'}, 'cancel_exit': 0}))
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertEqual(result['cancellation'], 'requested')
        self.assertFalse(result['quiescent'])
        self.assertTrue(self.stage.exists())
        self.assertEqual(read_json(self.stage / 'cleanup.json'), result)

    def test_confirmed_cancellation_and_ack_allow_removal(self):
        self.state.write_text(json.dumps({'jobs': {'321': 'RUNNING'}, 'cancel_exit': 0,
                                         'cancel_terminal': True}))
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'removed')
        self.assertEqual(result['cancellation'], 'requested')
        self.assertEqual(result['cleanup_sacct_states'], {'321': ['CANCELLED', '0:0']})
        self.assertFalse(self.stage.exists())

    def test_empty_discovery_is_unresolved(self):
        self.configure({})
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertFalse(result['quiescent'])
        self.assertTrue(self.stage.exists())

    def scheduler_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_queue_last(self, result):
        calls = self.scheduler_calls()
        self.assertEqual(calls[-1][0], 'squeue')
        self.assertEqual(calls[-2][0], 'sacct')
        self.assertIn('--name', calls[-2])
        self.assertRegex(result['scheduler_observed_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_initially_empty_queue_is_refreshed_after_accounting(self):
        self.configure({'321': 'COMPLETED'}, requeue_after_empty=True, stale_terminal=True)
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertFalse(result['quiescent'])
        self.assertEqual(result['queued_job_ids'], ['321'])
        self.assertEqual(result['cleanup_sacct_states'], {'321': ['COMPLETED', '0:0']})
        self.assertEqual(read_json(self.stage / 'cleanup.json'), result)
        self.assert_queue_last(result)

    def test_probe_late_completion_is_retryable_and_keeps_queue_last(self):
        self.configure({'321': 'RUNNING'})
        scheduler = self.bin / 'sacct'
        source = scheduler.read_text()
        emit = "        print(job + '|' + ('COMPLETED' if state.get('stale_terminal') else state['jobs'][job]) + '|0:0')"
        self.assertIn(emit, source)
        scheduler.write_text(source.replace(emit, emit +
            "\n        state['jobs'][job] = 'COMPLETED'"
            "\n        state_path.write_text(json.dumps(state))"))
        shutil.copyfile(Path(RV.__file__).with_name('child_environment.py'),
                        self.stage / 'child_environment.py')
        run = dict(launch_id=self.launch, stage=str(self.stage), execution={},
                   request=self.request)
        code = RV.slurm_success_probe(run)
        results = []
        for _ in range(2):
            probe = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                   text=True, check=True, timeout=30)
            result = json.loads(probe.stdout)
            results.append(result)
            self.assert_queue_last(result)
        first, later = results
        # Discovery supplies identities and a queue snapshot, never fresh states.
        # A nonterminal accounting read cannot become success from an empty queue.
        self.assertEqual(first['cleanup_sacct_states'], {'321': None})
        self.assertFalse(RV.slurm_quiescent(first, self.request))
        self.assertNotIn('sacct_state', first)
        self.assertEqual((later['sacct_state'], later['sacct_exit_code']), ('COMPLETED', '0:0'))
        self.assertNotIn('quiescent', later, 'scheduler success is not launch authority')

    def test_final_new_identity_retains_then_recovers_from_saved_identity(self):
        self.configure({'321': 'COMPLETED'}, final_new_id=True)
        ack = self.ack()
        result = RV.cleanup(self.stage, ack)
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertFalse(result['quiescent'])
        self.assertEqual(result['job_ids'], ['321', '322'])
        self.assertEqual(result['cleanup_sacct_states'], {'321': ['COMPLETED', '0:0']})
        self.assertEqual(read_json(self.stage / 'cleanup.json'), result)
        self.assert_queue_last(result)
        self.assertFalse(any('-j' in call and '322' in call for call in self.scheduler_calls()))
        state = read_json(self.state)
        state['hide_new_id'] = True
        self.state.write_text(json.dumps(state))
        recovered = RV.cleanup(self.stage, ack)
        self.assertEqual(recovered['cleanup'], 'removed')
        self.assertEqual(set(recovered['cleanup_sacct_states']), {'321', '322'})
        self.assert_queue_last(recovered)
        self.assertFalse(self.stage.exists())

    def test_final_discovery_errors_retain_stage(self):
        for option in ('final_queue_error', 'final_queue_ambiguous'):
            with self.subTest(option=option):
                self.configure({'321': 'COMPLETED'}, **{option: True})
                result = RV.cleanup(self.stage, self.ack())
                self.assertEqual(result['cleanup'], 'unconfirmed')
                self.assertFalse(result['scheduler_confirmed'])
                self.assertFalse(result['quiescent'])
                self.assertTrue(self.stage.exists())
                self.assert_queue_last(result)

    def test_oversized_final_cleanup_snapshot_is_refused_before_stage_deletion(self):
        self.configure({'321': 'COMPLETED'})
        jobs = [str(job) for job in range(321, 2821)]
        discovery = dict(RV.discover_slurm_jobs(self.request, {'321'}),
                         job_ids=jobs, queued_job_ids=[], scheduler_confirmed=True)
        outside = self.stage.with_name(self.stage.name + '.cleanup.json')
        before = {p.name: p.read_bytes() for p in self.stage.iterdir()}
        with mock.patch.object(RV, 'discover_slurm_jobs', return_value=discovery), \
                mock.patch.object(RV, 'scheduler_state', return_value=('COMPLETED', '0:0')):
            result = RV.cleanup(self.stage, self.ack())
        self.assertGreater(len(RV.encoded(result)), 65536)
        self.assertIn('exceeds 64 KiB', result['cleanup_error'])
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertTrue(result['quiescent'])
        self.assertFalse(outside.exists())
        for name, data in before.items():
            self.assertEqual((self.stage / name).read_bytes(), data)

    def test_removal_persists_final_cleanup_evidence_outside_stage(self):
        self.configure({'321': 'COMPLETED'})
        outside = self.stage.with_name(self.stage.name + '.cleanup.json')
        rmtree = shutil.rmtree
        observations = []

        def remove(path, **kwargs):
            # Real filesystem deletion; inspect what is durable before it starts.
            observations.append(read_json(outside) if outside.exists() else None)
            if outside.exists():
                with self.stage.with_name(self.stage.name + '.cleanup.lock').open('a') as guard:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (self.stage / 'cleanup.lock').open('a') as legacy:
                    fcntl.flock(legacy.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            rmtree(path, **kwargs)

        with mock.patch.object(RV.shutil, 'rmtree', side_effect=remove):
            result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'removed')
        self.assertFalse(self.stage.exists())
        self.assertIsNotNone(observations[0], 'final evidence was not durable outside the stage')
        self.assertTrue(observations[0]['quiescent'])
        self.assertEqual(observations[0]['cleanup'], 'unconfirmed')
        self.assertEqual(read_json(outside), result)

    def test_post_removal_evidence_write_failure_preserves_positive_status(self):
        self.configure({'321': 'COMPLETED'})
        publish = RV.publish_cleanup
        outside = self.stage.with_name(self.stage.name + '.cleanup.json')
        def fail_final(path, evidence):
            if path == outside and evidence['cleanup'] == 'removed':
                raise OSError('final evidence write unavailable')
            publish(path, evidence)
        with mock.patch.object(RV, 'publish_cleanup', side_effect=fail_final):
            result = RV.cleanup(self.stage, self.ack())
        self.assertFalse(self.stage.exists())
        self.assertEqual(result['cleanup'], 'removed')
        self.assertTrue(result['quiescent'])
        self.assertEqual(result['retained_cleanup'], read_json(outside))
        self.assertEqual(result['retained_cleanup']['cleanup'], 'unconfirmed')
        self.assertIn('final evidence write unavailable', result['cleanup_error'])

    def test_removal_requires_durable_external_evidence(self):
        self.configure({'321': 'COMPLETED'})
        publish = RV.publish_cleanup
        outside = self.stage.with_name(self.stage.name + '.cleanup.json')
        before = {p.name: p.read_bytes() for p in self.stage.iterdir()}
        def fail_outside(path, evidence):
            if path == outside:
                raise OSError('external evidence unavailable')
            publish(path, evidence)
        with mock.patch.object(RV, 'publish_cleanup', side_effect=fail_outside):
            result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertIn('external evidence unavailable', result['cleanup_error'])
        for name, content in before.items():
            self.assertEqual((self.stage / name).read_bytes(), content)

    def test_removal_cannot_report_removed_while_stage_exists(self):
        self.configure({'321': 'COMPLETED'})
        with mock.patch.object(RV.shutil, 'rmtree'):
            result = RV.cleanup(self.stage, self.ack())
        self.assertTrue(self.stage.exists())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertEqual(read_json(self.stage.with_name(self.stage.name + '.cleanup.json')), result)

    def test_failed_final_rmdir_does_not_repopulate_stage_with_cleanup_evidence(self):
        self.configure({'321': 'COMPLETED'})
        rmdir = os.rmdir
        def fail_final(path, *args, **kwargs):
            if Path(path) == self.stage:
                raise OSError(39, 'Directory not empty', str(path))
            return rmdir(path, *args, **kwargs)
        # Inject only the final filesystem error from the live report. All file
        # deletion is real; a local filesystem alone does not reproduce its cause.
        with mock.patch.object(RV.os, 'rmdir', side_effect=fail_final):
            result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'unconfirmed')
        self.assertTrue(self.stage.exists())
        self.assertEqual(list(self.stage.iterdir()), [], 'cleanup recreated evidence inside the stage')
        self.assertEqual(read_json(self.stage.with_name(self.stage.name + '.cleanup.json')), result)

    def test_happy_path_removes_only_after_final_queue(self):
        self.configure({'321': 'COMPLETED'})
        result = RV.cleanup(self.stage, self.ack())
        self.assertEqual(result['cleanup'], 'removed')
        self.assertTrue(result['quiescent'])
        self.assert_queue_last(result)
        self.assertFalse(self.stage.exists())

    def test_incomplete_final_is_distinct_from_absent_or_unexplained_final(self):
        self.completed_claims()
        original = (self.stage / 'claim-0.json').read_bytes()
        (self.stage / 'claim-1.json').unlink()
        final = read_json(self.stage / 'worker-complete')
        final['receipts'].pop('1')
        self.configure({'321': 'COMPLETED'})
        RV.cleanup(self.stage)
        for kind in ('absent', 'unexplained', 'incomplete'):
            with self.subTest(kind=kind):
                run = {'request': self.request, 'launch_id': self.launch,
                       'stage': str(self.stage), 'receipts': {}, 'execution': {}}
                if kind == 'absent':
                    (self.stage / 'worker-complete').unlink()
                else:
                    if kind == 'incomplete':
                        final['outcomes'][1] = RV.incomplete('controlled worker timeout')
                    RV.publish(self.stage / 'worker-complete', final)
                RV.ingest(run, RV.collect(self.stage))
                self.assertEqual(run['reconciled'], kind == 'incomplete')
                self.assertEqual(run['receipts']['0']['outcome']['exit_code'], 125)
                self.assertNotIn('incomplete_reason', run['receipts']['0']['outcome'])
                self.assertEqual((self.stage / 'claim-0.json').read_bytes(), original)
                if kind != 'incomplete':
                    self.assertEqual(RV.cleanup(self.stage, run.get('ack'))['cleanup'], 'unconfirmed')
        self.assertIsNone(run['final']['outcomes'][1]['exit_code'])
        self.assertTrue(RV.execution_problem(dict(result='pass', exit_code=None,
            execution=dict(run['execution'], location='remote', executables={}))))
        self.assertEqual(RV.cleanup(self.stage, run['ack'])['cleanup'], 'removed')
        self.assertFalse(self.stage.exists())


class TestSlurmReconciliation(unittest.TestCase):
    """Run the coordinator's locked retrieval against a local scheduler fixture."""
    def setUp(self):
        self.f = TestSlurmLifecycle()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.completed_claims()
        self.f.request['verification_host'].update(python=sys.executable, git='/usr/bin/git')
        RV.publish(self.f.stage / 'request.json', self.f.request)
        self.f.configure({'321': 'COMPLETED'})
        RV.cleanup(self.f.stage)
        self.state_dir = self.f.root / 'coordinator'
        self.state_dir.mkdir()
        self.run = dict(request=self.f.request, launch_id=self.f.launch,
                        stage=str(self.f.stage), receipts={}, execution={},
                        verification_host=self.f.request['verification_host'])
        RV.ingest(self.run, RV.collect(self.f.stage))
        self.run['published'] = True
        self.ledger = dict(unit='u', basis=self.f.request['basis'], runs=[self.run])
        self.path = RV.ledger_path(self.state_dir, 'u', self.ledger['basis'])
        self.path.parent.mkdir()
        RV.publish(self.path, self.ledger)

    def remote_call(self, prefix, remote, code, timeout=45):
        # The real consumer holds its binding lock during both frames and cleanup.
        with self.assertRaisesRegex(ValueError, 'already in progress'):
            with RV.binding_lock(self.state_dir, 'u', self.ledger['basis']):
                self.fail('coordinator lock was released during retrieval')
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        unlink = Path.unlink
        def audited_unlink(path, *args, **kwargs):
            if path == outside:
                saved = read_json(self.path)['runs'][0]
                receipt = saved['cleanup_receipt']
                self.assertEqual(receipt.get('retained_cleanup', receipt), read_json(outside))
                self.assertFalse(self.f.stage.exists())
            return unlink(path, *args, **kwargs)
        # Execute the actual wrapper and observe durable delivery at the unlink
        # consumer, including recovery before its staged-harness import.
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(sys, 'path', list(sys.path)), \
                mock.patch.object(Path, 'unlink', audited_unlink):
            exec(code, {})
        output = output.getvalue()
        return subprocess.CompletedProcess([], 0, output, '')

    def retrieve(self, call=None, recovery=None):
        with mock.patch.object(RV, 'coordinator_ssh', return_value='/fixture/ssh'), \
                mock.patch.object(RV, 'remote_call', side_effect=call or self.remote_call), \
                mock.patch.object(RV, 'make_bundle') as bundle:
            result = RV.run_remote(None, self.f.root, self.ledger['basis'],
                self.f.request['checks'], None, 1, self.f.root, self.state_dir,
                'u', [], retrieve_only=True, recovery=recovery)
            bundle.assert_not_called()
        return result

    def assert_revoked(self):
        run = read_json(self.path)['runs'][0]
        self.assertIs(run['reconciled'], False)
        self.assertIs(run['published'], False)
        self.assertNotIn('ack', run)
        self.assertIs(run['execution']['evidence_reconciled'], False)
        self.assertIn('unresolved remote evidence', RV.pending_problem(
            self.state_dir, 'u', self.ledger['basis'], []))
        return run

    def test_cleanup_evidence_retirement_waits_for_durable_receipt_and_retries(self):
        def disconnect(prefix, remote, code, timeout=45):
            if 'history.unlink()' in code:
                return subprocess.CompletedProcess([], 255, '', 'connection lost')
            return self.remote_call(prefix, remote, code, timeout)
        self.retrieve(disconnect)
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        saved = read_json(self.path)['runs'][0]
        self.assertFalse(self.f.stage.exists())
        self.assertEqual(saved['cleanup_receipt'], read_json(outside))
        self.assertEqual(saved['execution']['cleanup'], 'removed')
        self.assertTrue(saved['reconciled'])
        self.assertIn('cleanup_evidence_error', saved['execution'])
        self.retrieve()
        self.assertFalse(outside.exists())
        self.assertFalse(self.f.stage.with_name(self.f.stage.name + '.cleanup.lock').exists())
        self.assertEqual(read_json(self.path)['runs'][0]['receipts'], saved['receipts'])

    def test_lost_removal_response_recovers_retained_cleanup_snapshot(self):
        def lose_response(prefix, remote, code, timeout=45):
            result = self.remote_call(prefix, remote, code, timeout)
            if 'from remote_verify import cleanup' in code:
                return subprocess.CompletedProcess([], 255, '', 'response lost')
            return result
        self.retrieve(lose_response)
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        retained = read_json(outside)
        self.assertFalse(self.f.stage.exists())
        self.assertEqual(read_json(self.path)['runs'][0]['execution']['cleanup'], 'unconfirmed')
        self.retrieve()
        saved = read_json(self.path)['runs'][0]
        self.assertEqual(saved['cleanup_receipt']['retained_cleanup'], retained)
        self.assertNotIn('quiescent', saved['cleanup_receipt'])
        self.assertEqual(saved['execution']['cleanup'], 'removed')
        self.assertFalse(outside.exists())

    def test_interruption_after_rmdir_recovers_actual_absence(self):
        publish = RV.publish_cleanup
        def interrupt_final(path, evidence):
            if evidence['cleanup'] == 'removed':
                raise OSError('final evidence write interrupted')
            publish(path, evidence)
        def lose_process(prefix, remote, code, timeout=45):
            with mock.patch.object(RV, 'publish_cleanup', side_effect=interrupt_final):
                result = self.remote_call(prefix, remote, code, timeout)
            if 'from remote_verify import cleanup' in code:
                return subprocess.CompletedProcess([], 255, '', 'cleanup response lost')
            return result
        self.retrieve(lose_process)
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        retained = read_json(outside)
        self.assertFalse(self.f.stage.exists())
        self.assertEqual(retained['cleanup'], 'unconfirmed')
        self.assertTrue(read_json(self.path)['runs'][0]['reconciled'])
        self.retrieve()
        saved = read_json(self.path)['runs'][0]
        self.assertEqual(saved['execution']['cleanup'], 'removed')
        self.assertTrue(saved['reconciled'])
        self.assertEqual(saved['cleanup_receipt']['retained_cleanup'], retained)
        self.assertFalse(outside.exists())
        self.assertFalse(self.f.stage.with_name(self.f.stage.name + '.cleanup.lock').exists())

    def test_post_removal_evidence_write_failure_does_not_revoke_reconciliation(self):
        publish = RV.publish_cleanup
        def fail_final(path, evidence):
            if evidence['cleanup'] == 'removed':
                raise OSError('final evidence write unavailable')
            publish(path, evidence)
        with mock.patch.object(RV, 'publish_cleanup', side_effect=fail_final):
            self.retrieve()
        saved = read_json(self.path)['runs'][0]
        self.assertFalse(self.f.stage.exists())
        self.assertEqual(saved['execution']['cleanup'], 'removed')
        self.assertTrue(saved['reconciled'])
        self.assertTrue(saved['ack'])
        self.assertIsNone(RV.pending_problem(self.state_dir, 'u', self.ledger['basis'], []))
        self.assertFalse(self.f.stage.with_name(self.f.stage.name + '.cleanup.json').exists())

    def strand_empty_stage(self):
        rmdir = os.rmdir
        def fail_final(path, *args, **kwargs):
            if Path(path) == self.f.stage:
                raise OSError(39, 'Directory not empty', str(path))
            return rmdir(path, *args, **kwargs)
        with mock.patch.object(RV.os, 'rmdir', side_effect=fail_final):
            self.retrieve()
        self.assertEqual(list(self.f.stage.iterdir()), [])
        saved = read_json(self.path)['runs'][0]
        self.assertTrue(saved['reconciled'])
        self.assertTrue(saved['ack'])
        return saved

    def test_retry_after_final_rmdir_error_removes_only_empty_stage(self):
        saved = self.strand_empty_stage()
        receipts = saved['receipts']
        self.retrieve()
        saved = read_json(self.path)['runs'][0]
        self.assertFalse(self.f.stage.exists())
        self.assertEqual(saved['execution']['cleanup'], 'removed')
        self.assertEqual(saved['receipts'], receipts)
        self.assertFalse(self.f.stage.with_name(self.f.stage.name + '.cleanup.json').exists())

    def test_empty_stage_recovery_validates_journal_before_removal(self):
        self.strand_empty_stage()
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        original = outside.read_bytes()
        snapshot = json.loads(original)
        for field, value, error in (
                ('stage', str(self.f.root / 'different-stage'), 'binding mismatch'),
                ('launch_id', 'e' * 32, 'binding mismatch'),
                ('extra', 'changed after receipt', 'digest mismatch')):
            with self.subTest(field=field):
                changed = dict(snapshot, **{field: value})
                RV.publish(outside, changed)
                self.retrieve()
                saved = read_json(self.path)['runs'][0]
                self.assertEqual(saved['execution']['cleanup'], 'unconfirmed')
                self.assertIn(error, saved['execution']['cleanup_evidence_error'])
                self.assertTrue(self.f.stage.is_dir())
                self.assertEqual(list(self.f.stage.iterdir()), [])
                self.assertEqual(read_json(outside), changed)
                self.assertEqual(saved['receipts'], self.run['receipts'])
        outside.write_bytes(original)
        self.retrieve()
        self.assertFalse(self.f.stage.exists())
        self.assertEqual(read_json(self.path)['runs'][0]['execution']['cleanup'], 'removed')

    def test_empty_stage_recovery_retains_stage_on_unreadable_journal(self):
        self.strand_empty_stage()
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        original = outside.read_bytes()
        for content in (None, b'{invalid', b'x' * 65537):
            with self.subTest(content=None if content is None else len(content)):
                if content is None:
                    outside.unlink()
                else:
                    outside.write_bytes(content)
                self.retrieve()
                saved = read_json(self.path)['runs'][0]
                self.assertEqual(saved['execution']['cleanup'], 'unconfirmed')
                self.assertIn('cleanup_evidence_error', saved['execution'])
                self.assertTrue(self.f.stage.is_dir())
                self.assertEqual(list(self.f.stage.iterdir()), [])
        outside.write_bytes(original)
        self.retrieve()
        self.assertFalse(self.f.stage.exists())

    def test_empty_stage_recovery_confirms_absence_after_rmdir(self):
        self.strand_empty_stage()
        with mock.patch.object(Path, 'rmdir'):
            self.retrieve()
        self.assertTrue(self.f.stage.is_dir())
        self.assertEqual(read_json(self.path)['runs'][0]['execution']['cleanup'], 'unconfirmed')

    def test_empty_stage_recovery_retains_dangling_symlink(self):
        self.strand_empty_stage()
        self.f.stage.rmdir()
        self.f.stage.symlink_to(self.f.root / 'missing-stage-target')
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        self.retrieve()
        saved = read_json(self.path)['runs'][0]
        self.assertTrue(self.f.stage.is_symlink())
        self.assertEqual(saved['execution']['cleanup'], 'unconfirmed')
        self.assertTrue(outside.is_file())
        self.assertTrue(saved['reconciled'])

    def test_auxiliary_retirement_retains_evidence_when_symlink_appears(self):
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        def replace_stage(prefix, remote, code, timeout=45):
            if 'history.unlink()' in code:
                self.f.stage.symlink_to(self.f.root / 'missing-stage-target')
            return self.remote_call(prefix, remote, code, timeout)
        self.retrieve(replace_stage)
        self.assertTrue(self.f.stage.is_symlink())
        self.assertTrue(outside.is_file())
        self.assertTrue(self.f.stage.with_name(self.f.stage.name + '.cleanup.lock').is_file())

    def test_empty_stage_recovery_retains_child_created_before_rmdir(self):
        self.strand_empty_stage()
        late = self.f.stage / 'late-output'
        rmdir = Path.rmdir
        def create_child(path, *args, **kwargs):
            if Path(path) == self.f.stage:
                late.write_bytes(b'late write survives atomic rmdir refusal')
            return rmdir(path, *args, **kwargs)
        with mock.patch.object(Path, 'rmdir', create_child):
            self.retrieve()
        saved = read_json(self.path)['runs'][0]
        self.assertEqual(late.read_bytes(), b'late write survives atomic rmdir refusal')
        self.assertEqual(saved['execution']['cleanup'], 'unconfirmed')
        self.assertTrue(saved['reconciled'])
        self.assertTrue(saved['execution']['quiescent'])

    def test_empty_stage_recovery_preserves_late_file(self):
        self.strand_empty_stage()
        late = self.f.stage / 'late-output'
        late.write_bytes(b'preserve these bytes')
        self.retrieve()
        self.assertEqual(late.read_bytes(), b'preserve these bytes')
        self.assertEqual(read_json(self.path)['runs'][0]['execution']['cleanup'], 'unconfirmed')

    def test_empty_stage_recovery_refuses_persisted_negative_observation(self):
        self.strand_empty_stage()
        ledger = read_json(self.path)
        ledger['runs'][0]['execution']['quiescent'] = False
        RV.publish(self.path, ledger)
        self.retrieve()
        self.assert_revoked()
        self.assertTrue(self.f.stage.is_dir())
        self.assertEqual(list(self.f.stage.iterdir()), [])

    def test_empty_stage_recovery_respects_external_lock(self):
        self.strand_empty_stage()
        lock = self.f.stage.with_name(self.f.stage.name + '.cleanup.lock')
        with lock.open('a') as guard:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.retrieve()
            self.assertTrue(self.f.stage.is_dir())
        self.retrieve()
        self.assertFalse(self.f.stage.exists())

    def test_empty_stage_recovery_after_lost_cleanup_response(self):
        rmdir = os.rmdir
        def fail_final(path, *args, **kwargs):
            if Path(path) == self.f.stage:
                raise OSError(39, 'Directory not empty', str(path))
            return rmdir(path, *args, **kwargs)
        def lose_response(prefix, remote, code, timeout=45):
            result = self.remote_call(prefix, remote, code, timeout)
            if 'from remote_verify import cleanup' in code:
                return subprocess.CompletedProcess([], 255, '', 'response lost')
            return result
        with mock.patch.object(RV.os, 'rmdir', side_effect=fail_final):
            self.retrieve(lose_response)
        self.assertEqual(list(self.f.stage.iterdir()), [])
        saved = read_json(self.path)['runs'][0]
        self.assertTrue(saved['reconciled'])
        self.assertTrue(saved['ack'])
        self.assertNotIn('cleanup_receipt', saved)
        observations = []
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        path_rmdir = Path.rmdir
        def check_saved_snapshot(path):
            if path == self.f.stage:
                receipt = read_json(self.path)['runs'][0]['cleanup_receipt']
                observations.append(receipt)
                self.assertEqual(receipt['retained_cleanup'], read_json(outside))
            return path_rmdir(path)
        with mock.patch.object(Path, 'rmdir', check_saved_snapshot):
            self.retrieve()
        self.assertEqual(len(observations), 1)
        self.assertFalse(self.f.stage.exists())
        self.assertEqual(read_json(self.path)['runs'][0]['receipts'], saved['receipts'])

    def test_empty_stage_journal_failure_does_not_fabricate_negative_observation(self):
        rmdir, publish = os.rmdir, RV.publish_cleanup
        def fail_rmdir(path, *args, **kwargs):
            if Path(path) == self.f.stage:
                raise OSError(39, 'Directory not empty', str(path))
            return rmdir(path, *args, **kwargs)
        def fail_final(path, evidence):
            if path.name.endswith('.cleanup.json') and not (self.f.stage / 'request.json').exists():
                raise OSError('final journal write failed')
            publish(path, evidence)
        with mock.patch.object(RV.os, 'rmdir', side_effect=fail_rmdir), \
                mock.patch.object(RV, 'publish_cleanup', side_effect=fail_final):
            self.retrieve()
        self.assertEqual(list(self.f.stage.iterdir()), [])
        saved = read_json(self.path)['runs'][0]
        self.assertTrue(saved['reconciled'])
        self.assertTrue(saved['ack'])
        self.assertTrue(saved['execution']['quiescent'])
        self.retrieve()
        self.assertFalse(self.f.stage.exists())

    def test_empty_stage_recovery_requires_original_acknowledgment(self):
        self.strand_empty_stage()
        ledger = read_json(self.path)
        ledger['runs'][0].pop('ack')
        RV.publish(self.path, ledger)
        self.retrieve()
        self.assertTrue(self.f.stage.is_dir())
        self.assertNotIn('ack', read_json(self.path)['runs'][0])

    def test_empty_stage_recovery_requires_matching_coordinator_binding(self):
        self.strand_empty_stage()
        ledger = read_json(self.path)
        ledger['runs'][0]['execution']['launch_id'] = 'e' * 32
        RV.publish(self.path, ledger)
        self.retrieve()
        self.assertTrue(self.f.stage.is_dir())
        self.assertEqual(read_json(self.path)['runs'][0]['execution']['cleanup'], 'unconfirmed')

    def test_auxiliary_retirement_preserves_changed_snapshot(self):
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        def change_snapshot(prefix, remote, code, timeout=45):
            if 'history.unlink()' in code:
                snapshot = read_json(outside)
                snapshot['changed-after-receipt'] = True
                RV.publish(outside, snapshot)
            return self.remote_call(prefix, remote, code, timeout)
        self.retrieve(change_snapshot)
        self.assertFalse(self.f.stage.exists())
        self.assertTrue(read_json(outside)['changed-after-receipt'])
        saved = read_json(self.path)['runs'][0]
        self.assertTrue(saved['reconciled'])
        self.assertNotIn('changed-after-receipt', saved['cleanup_receipt'])
        self.assertIn('cleanup_evidence_error', saved['execution'])
        self.retrieve()
        self.assertTrue(read_json(outside)['changed-after-receipt'])

    def test_auxiliary_retirement_retries_after_history_unlink(self):
        unlink = Path.unlink
        lock = self.f.stage.with_name(self.f.stage.name + '.cleanup.lock')
        def fail_lock(path, *args, **kwargs):
            if path == lock:
                raise OSError('lock unlink interrupted')
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', fail_lock):
            self.retrieve()
        self.assertFalse(self.f.stage.exists())
        self.assertFalse(self.f.stage.with_name(self.f.stage.name + '.cleanup.json').exists())
        self.assertTrue(lock.exists())
        saved = read_json(self.path)['runs'][0]
        self.assertTrue(saved['reconciled'])
        self.assertEqual(saved['execution']['cleanup'], 'removed')
        self.retrieve()
        self.assertFalse(lock.exists())
        self.assertEqual(read_json(self.path)['runs'][0]['receipts'], saved['receipts'])

    def first_observation_during_cleanup(self, second_transport_failure=False):
        self.run.update(reconciled=False, published=False)
        self.run.pop('ack', None)
        (self.f.stage / 'cleanup.json').unlink()
        self.run['execution'] = {}
        RV.publish(self.path, self.ledger)
        calls = []
        def call(prefix, remote, code, timeout=45):
            if 'from remote_verify import cleanup' in code:
                calls.append(code)
                if len(calls) == 2:
                    saved = read_json(self.path)['runs'][0]
                    self.assertTrue(saved['reconciled'])
                    self.assertTrue(saved['ack'])
                    if second_transport_failure:
                        return subprocess.CompletedProcess([], 255, '', 'connection lost')
                    self.f.configure({'321': 'RUNNING'})
            return self.remote_call(prefix, remote, code, timeout)
        result = self.retrieve(call)
        self.assertEqual(len(calls), 2)
        return result

    def test_same_invocation_negative_second_cleanup_revokes_new_ack(self):
        self.first_observation_during_cleanup()
        saved = self.assert_revoked()
        self.assertTrue(self.f.stage.exists())
        self.assertEqual(saved['receipts'], self.run['receipts'])

    def test_same_invocation_transport_failure_keeps_positive_reconciliation(self):
        self.first_observation_during_cleanup(second_transport_failure=True)
        saved = read_json(self.path)['runs'][0]
        self.assertTrue(saved['reconciled'])
        self.assertTrue(saved['ack'])
        self.assertTrue(saved['execution']['quiescent'])
        self.assertEqual(saved['execution']['cleanup'], 'unconfirmed')
        self.assertTrue(self.f.stage.exists())
        self.assertEqual(saved['receipts'], self.run['receipts'])

    def test_lost_second_cleanup_response_does_not_pin_earlier_observation(self):
        self.run.update(reconciled=False, published=False)
        self.run.pop('ack', None)
        (self.f.stage / 'cleanup.json').unlink()
        self.run['execution'] = {}
        RV.publish(self.path, self.ledger)
        calls = []
        def lose_second(prefix, remote, code, timeout=45):
            result = self.remote_call(prefix, remote, code, timeout)
            if 'from remote_verify import cleanup' in code:
                calls.append(code)
                if len(calls) == 2:
                    return subprocess.CompletedProcess([], 255, '', 'cleanup response lost')
            return result
        self.retrieve(lose_second)
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        saved = read_json(self.path)['runs'][0]
        self.assertNotIn('cleanup_receipt', saved)
        self.assertTrue(outside.is_file())
        self.assertEqual(saved['execution']['cleanup'], 'unconfirmed')
        self.retrieve()
        saved = read_json(self.path)['runs'][0]
        self.assertEqual(saved['execution']['cleanup'], 'removed')
        self.assertEqual(saved['receipts'], self.run['receipts'])
        self.assertFalse(outside.exists())
        self.assertFalse(self.f.stage.exists())

    def test_lost_cleanup_response_after_partial_deletion_does_not_keep_stale_pin(self):
        with mock.patch.object(RV.shutil, 'rmtree', side_effect=OSError('partial removal')):
            self.retrieve()
        old_receipt = read_json(self.path)['runs'][0]['cleanup_receipt']
        outside = self.f.stage.with_name(self.f.stage.name + '.cleanup.json')
        self.assertEqual(old_receipt, read_json(outside))
        self.assertTrue((self.f.stage / 'request.json').exists())
        def lose_response(prefix, remote, code, timeout=45):
            if 'from remote_verify import cleanup' in code:
                self.assertNotIn('cleanup_receipt', read_json(self.path)['runs'][0])
            result = self.remote_call(prefix, remote, code, timeout)
            if 'from remote_verify import cleanup' in code:
                return subprocess.CompletedProcess([], 255, '', 'cleanup response lost')
            return result
        self.retrieve(lose_response)
        self.assertNotEqual(old_receipt, read_json(outside))
        self.retrieve()
        saved = read_json(self.path)['runs'][0]
        self.assertEqual(saved['execution']['cleanup'], 'removed')
        self.assertEqual(saved['receipts'], self.run['receipts'])
        self.assertFalse(outside.exists())
        self.assertFalse(self.f.stage.exists())

    def test_negative_cleanup_revokes_flags_and_later_reconciles_same_receipts(self):
        receipts = self.run['receipts']
        self.f.configure({'321': 'RUNNING'})
        outcomes, execution = self.retrieve()
        self.assertFalse(execution['quiescent'])
        self.assertEqual(outcomes[0]['exit_code'], 125)
        self.assert_revoked()
        RV.acknowledge(self.state_dir, 'u', self.ledger['basis'], self.f.launch,
                       execution['publication_digest'])
        self.assert_revoked()  # Publishing the old receipts cannot revive the flags.
        self.f.configure({'321': 'COMPLETED'})
        _, execution = self.retrieve()  # Reconcile the fresh observation in this call.
        recovered = read_json(self.path)['runs'][0]
        self.assertTrue(recovered['reconciled'])
        self.assertEqual(recovered['receipts'], receipts)
        self.assertFalse(self.f.stage.exists())
        RV.acknowledge(self.state_dir, 'u', self.ledger['basis'], self.f.launch,
                       execution['publication_digest'])
        self.assertIsNone(RV.pending_problem(self.state_dir, 'u', self.ledger['basis'], []))

    def test_persisted_negative_blocks_read_only_admission_and_repairs_on_retrieval(self):
        self.run['execution']['quiescent'] = False
        RV.publish(self.path, self.ledger)
        before = self.path.read_bytes()
        self.assertIn('unresolved remote evidence', RV.pending_problem(
            self.state_dir, 'u', self.ledger['basis'], []))
        self.assertEqual(self.path.read_bytes(), before, 'admission must stay read-only')
        # Repair must be durable even when a later binding check refuses retrieval.
        with self.assertRaisesRegex(ValueError, 'claim binding changed'):
            RV.run_remote(None, self.f.root, self.ledger['basis'], [], None, 1,
                          self.f.root, self.state_dir, 'u', [], retrieve_only=True)
        self.assert_revoked()
        self.retrieve(mock.Mock(side_effect=OSError('transport unavailable')))
        repaired = self.assert_revoked()
        self.assertEqual(repaired['receipts'], self.run['receipts'])

    def test_transport_failure_alone_does_not_revoke_positive_observation(self):
        _, execution = self.retrieve(mock.Mock(side_effect=OSError('transport unavailable')))
        saved = read_json(self.path)['runs'][0]
        for key in ('reconciled', 'published', 'ack'):
            self.assertEqual(saved[key], self.run[key])
        self.assertTrue(execution['quiescent'])
        self.assertTrue(execution['evidence_reconciled'])
        self.assertIsNone(RV.pending_problem(self.state_dir, 'u', self.ledger['basis'], []))
        self.assertTrue(self.f.stage.exists())
        _, recovered = self.retrieve()
        self.assertEqual(recovered['cleanup'], 'removed')

    def recovery(self):
        run = read_json(self.path)['runs'][0]
        proof = {'binding': RV.recovery_binding(run)}
        for subject in ('supervisor', 'jobs', 'privileged_requeue'):
            proof[subject] = {'status': 'fenced', 'evidence': subject + ' permanently fenced by operator'}
        return proof, 'fixture-operator'

    def retain_probe_success(self):
        (self.f.stage / 'supervision-finished').unlink()
        (self.f.stage / 'cleanup.json').unlink()
        self.run['execution'].pop('sacct_state')
        RV.publish(self.path, self.ledger)
        self.retrieve()
        run = read_json(self.path)['runs'][0]
        self.assertIn('probe_terminal_success', run)
        self.assertFalse(run['reconciled'])
        return run

    def assert_probe_contradiction(self, states):
        before = self.retain_probe_success()
        self.f.configure(states)
        self.retrieve()
        contradicted = read_json(self.path)['runs'][0]
        self.f.configure({})
        self.retrieve()  # Missing/stale later evidence cannot erase the blocker.
        with self.assertRaisesRegex(ValueError, 'superseded'):
            self.retrieve(recovery=self.recovery())
        self.assertTrue(contradicted['probe_terminal_success']['contradiction'])
        self.assert_revoked()
        saved = read_json(self.path)['runs'][0]
        self.assertEqual(saved['receipts'], before['receipts'])
        self.assertNotIn('recovery_authority', saved)
        # A fresh full success observation can cover every retained job identity.
        self.f.configure({j: 'COMPLETED' for j in states})
        self.retrieve()
        self.retrieve(recovery=self.recovery())
        self.assertTrue(read_json(self.path)['runs'][0]['reconciled'])

    def test_new_job_cannot_disappear_from_recovery_success_evidence(self):
        self.assert_probe_contradiction({'321': 'COMPLETED', '322': 'RUNNING'})

    def test_failed_job_blocks_old_recovery_success_even_after_expiry(self):
        self.assert_probe_contradiction({'321': 'FAILED'})

    def test_live_job_blocks_old_recovery_success_even_after_expiry(self):
        self.assert_probe_contradiction({'321': 'RUNNING'})

    def test_fresh_negative_identical_to_cached_pre_probe_bytes_blocks_recovery(self):
        (self.f.stage / 'supervision-finished').unlink()
        self.run['execution'].pop('sacct_state')
        self.run.update(reconciled=False, published=False)
        RV.publish(self.path, self.ledger)
        with mock.patch.object(RV.time, 'strftime', return_value='2026-09-27T22:40:00Z'):
            self.f.configure({'321': 'RUNNING'})
            cached = RV.cleanup(self.f.stage)
            def transition(prefix, remote, code, timeout=45):
                if 'def observe_slurm_success' in code:
                    self.f.configure({'321': 'COMPLETED'})
                    result = self.remote_call(prefix, remote, code, timeout)
                    self.f.configure({'321': 'RUNNING'})
                    return result
                return self.remote_call(prefix, remote, code, timeout)
            self.retrieve(call=transition)
            self.assertEqual(read_json(self.f.stage / 'cleanup.json'), cached)
            with self.assertRaisesRegex(ValueError, 'superseded'):
                self.retrieve(recovery=self.recovery())
            self.assert_revoked()

    def test_recovery_success_observation_is_bound_and_validated(self):
        self.retain_probe_success()
        original = self.path.read_bytes()
        for defect in ('binding', 'job-set', 'state', 'queue', 'missing'):
            with self.subTest(defect=defect):
                ledger = json.loads(original)
                proof = ledger['runs'][0]['probe_terminal_success']
                if defect == 'binding':
                    proof['binding']['request_sha256'] = '0' * 64
                elif defect == 'job-set':
                    proof['known_job_ids'].append('322')
                elif defect == 'state':
                    proof['observation']['cleanup_sacct_states']['321'] = ['FAILED', '1:0']
                elif defect == 'queue':
                    proof['observation']['queued_job_ids'] = ['321']
                else:
                    proof['observation'] = {}
                RV.publish(self.path, ledger)
                with self.assertRaisesRegex(ValueError, 'invalid or superseded'):
                    self.retrieve(recovery=self.recovery())
                self.assertNotIn('recovery_authority', read_json(self.path)['runs'][0])

    def test_recovery_authority_is_durable_before_use_and_repairs_interruption(self):
        self.f.configure({})  # Purged accounting, retained immutable receipts.
        self.retrieve()
        self.assert_revoked()
        proof = self.recovery()
        publish = RV.publish
        def interrupt(path, ledger, **kwargs):
            saved = ledger['runs'][0]
            if saved.get('recovery_authority') and saved['reconciled']:
                durable = read_json(self.path)['runs'][0]
                self.assertEqual(durable['recovery_authority']['attestation'], proof[0])
                self.assertFalse(durable['reconciled'])
                raise OSError('interrupted after authority publication')
            return publish(path, ledger, **kwargs)
        with mock.patch.object(RV, 'publish', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'interrupted after'):
                self.retrieve(recovery=proof)
        call = mock.Mock(side_effect=AssertionError('recovery contacted remote host'))
        outcomes, execution = self.retrieve(call=call)
        self.assertEqual([o['exit_code'] for o in outcomes], [125, 0])
        self.assertTrue(execution['evidence_reconciled'])
        self.assertEqual(execution['recovery_evidence_class'], 'attested')
        call.assert_not_called()
        self.assertTrue(self.f.stage.exists())
        RV.acknowledge(self.state_dir, 'u', self.ledger['basis'], self.f.launch,
                       execution['publication_digest'])
        self.assertIsNone(RV.pending_problem(self.state_dir, 'u', self.ledger['basis'], []))
        ledger = read_json(self.path)
        ledger['runs'][0].pop('recovery_authority')
        RV.publish(self.path, ledger)
        self.assertIn('restore the coordinator recovery authority',
                      RV.pending_problem(self.state_dir, 'u', self.ledger['basis'], []))

    def test_recovery_refuses_missing_unbound_or_conflicting_completed_receipts(self):
        original = self.path.read_bytes()
        for defect in ('missing', 'binding', 'conflict', 'no-success'):
            with self.subTest(defect=defect):
                ledger = json.loads(original)
                run = ledger['runs'][0]
                if defect == 'missing':
                    run['receipts'].pop('1')
                elif defect == 'binding':
                    run['receipts']['1']['claim_binding'] = {'claim': 'different'}
                elif defect == 'conflict':
                    run['error'] = 'conflicting completed remote receipt'
                else:
                    run['execution'].pop('sacct_state')
                RV.publish(self.path, ledger)
                with self.assertRaises(ValueError):
                    self.retrieve(recovery=self.recovery())
                self.assertNotIn('recovery_authority', read_json(self.path)['runs'][0])

    def test_recovery_requires_every_fence_and_exact_binding(self):
        for subject in ('supervisor', 'jobs', 'privileged_requeue', 'binding'):
            with self.subTest(subject=subject):
                proof, operator = self.recovery()
                if subject == 'binding':
                    proof['binding']['launch_id'] = 'b' * 32
                else:
                    proof[subject]['status'] = 'unreachable'
                with self.assertRaises(ValueError):
                    self.retrieve(recovery=(proof, operator))
                self.assertNotIn('recovery_authority', read_json(self.path)['runs'][0])


class TestSlurmPolicy(unittest.TestCase):
    def test_slurm_requires_all_plain_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = {'schema_version': 1, 'verification_host': {
                'executor': 'slurm', 'ssh_alias': 'fixture-host', 'python': '/fixture/python',
                'git': '/fixture/git', 'workdir_root': '/fixture/stage',
                'slurm': {'partition': 'fixture-cpu', 'mem': '2G', 'time': '00:05:00'}}}
            path = Path(tmp) / RV.POLICY
            path.write_text(json.dumps(policy))
            self.assertEqual(RV.read_policy(tmp)[0], policy)
            original = dict(policy['verification_host']['slurm'])
            for key in original:
                for value in (None, '', 2, [], 'has space', 'line\n', '--option', '$(cmd)', 'x;y'):
                    with self.subTest(key=key, value=value):
                        cfg = dict(original)
                        if value is None:
                            del cfg[key]
                        else:
                            cfg[key] = value
                        policy['verification_host']['slurm'] = cfg
                        path.write_text(json.dumps(policy))
                        with self.assertRaises(ValueError):
                            RV.read_policy(tmp)
            policy['verification_host']['slurm'] = original
            policy['verification_host']['executor'] = 'direct'
            path.write_text(json.dumps(policy))
            with self.assertRaisesRegex(ValueError, 'direct executor'):
                RV.read_policy(tmp)


if __name__ == '__main__':
    unittest.main()
