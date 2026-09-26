"""Remote verification through the real operator, Git bundle, SSH and Slurm shims."""
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import unittest

from tests import test_arc683_shared_guard as fixtures

S, V = fixtures.S, fixtures.V
RV = V.RV

SSH = r'''
import io, json, os, pathlib, subprocess, sys, tarfile
args = sys.argv[1:]
assert args[:2] == ['-oBatchMode=yes', '-oConnectTimeout=15']
assert args[2] == 'fixture-host'
command = args[3]
with open(os.environ['REMOTE_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if "tarfile" in command:
    mode = pathlib.Path(os.environ['REMOTE_MODE']).read_text()
    if mode == 'ssh-fail': raise SystemExit(255)
    raw = sys.stdin.buffer.read()
    if mode == 'tamper':
        dst = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(raw)) as source, tarfile.open(fileobj=dst, mode='w') as dest:
            for member in source:
                data = source.extractfile(member).read()
                if member.name == 'candidate.bundle':
                    data = pathlib.Path(os.environ['TAMPER_BUNDLE']).read_bytes()
                    member.size = len(data)
                dest.addfile(member, io.BytesIO(data))
        raw = dst.getvalue()
    result = subprocess.run(['/bin/sh', '-c', command], input=raw)
else:
    result = subprocess.run(['/bin/sh', '-c', command])
raise SystemExit(result.returncode)
'''

SBATCH = r'''
import json, os, pathlib, subprocess, sys
with open(os.environ['SCHED_LOG'], 'a') as log:
    log.write(json.dumps(['sbatch'] + sys.argv[1:]) + '\n')
assert '--no-requeue' in sys.argv
assert '--partition=fixture-cpu' in sys.argv
assert '--mem=2G' in sys.argv
assert '--time=00:05:00' in sys.argv
if pathlib.Path(os.environ['REMOTE_MODE']).read_text() not in ('slurm-missing', 'slurm-pending'):
    result = subprocess.run(['/bin/sh', sys.argv[-1]], capture_output=True)
    assert result.returncode == 0, result.stderr
    if pathlib.Path(os.environ['REMOTE_MODE']).read_text() == 'slurm-forced-requeue':
        pathlib.Path(os.environ['REMOTE_MODE']).write_text('pass')
        result = subprocess.run(['/bin/sh', sys.argv[-1]], capture_output=True)
        assert result.returncode == 0, result.stderr
print('321')
'''

SACCT = r'''
import os, pathlib
mode = pathlib.Path(os.environ['REMOTE_MODE']).read_text()
print('321|PENDING|0:0' if mode == 'slurm-pending' else
      '321|FAILED|1:0' if mode in ('slurm-missing', 'slurm-completed-fail') else '321|COMPLETED|0:0')
'''


class TestRemoteVerification(unittest.TestCase):
    def setUp(self):
        self.shared = fixtures.TestSharedGuard()
        self.shared.setUp()
        self.addCleanup(self.shared.doCleanups)
        self.f = self.shared.f
        self.mode = self.f.directory / 'remote-mode'
        self.mode.write_text('pass')
        self.remote_root = self.f.directory / 'remote-root'
        self.remote_root.mkdir()
        self.witness = self.f.directory / 'verifier-ran'
        self.f.env.update(REMOTE_LOG=str(self.f.directory / 'ssh.log'),
                          REMOTE_MODE=str(self.mode), SCHED_LOG=str(self.f.directory / 'scheduler.log'))
        for name, body in (('ssh', SSH), ('sbatch', SBATCH), ('sacct', SACCT),
                           ('scancel', 'raise SystemExit(0)\n')):
            program = self.f.bin / name
            program.write_text('#!' + sys.executable + '\n' + body)
            program.chmod(0o755)
        self.policy = {'schema_version': 1,
                       'local': {'python': sys.executable, 'git': str((self.f.bin / 'git').resolve())},
                       'remote': {'ssh_alias': 'fixture-host', 'executor': 'direct',
                                  'workdir_root': str(self.remote_root), 'python': sys.executable,
                                  'git': str((self.f.bin / 'git').resolve())}}
        self.save_policy()
        (self.f.state_dir / S.VERIFY_RECEIPTS).unlink()

    def save_policy(self):
        (self.f.state_dir / RV.POLICY).write_text(json.dumps(self.policy))

    def program(self, tail=''):
        program = ('#!' + sys.executable + '\nfrom pathlib import Path\n'
                   'Path(%r).write_text("ran")\n' % str(self.witness)) + tail
        policy = dict(self.f.policy, verifiers=[dict(
            self.f.policy['verifiers'][0], sha256=hashlib.sha256(program.encode()).hexdigest())])
        return self.shared.precondition.target_commit({V.MERGE_VERIFIER_PATH: program,
                                                       V.POLICY_FILE: json.dumps(policy)})

    def verify(self, *extra):
        return self.f.invoke('--verify-integration', *extra)

    def rows(self):
        return S.load_verifications(self.f.state_dir)[0]

    def admitted(self):
        return self.f.invoke()

    def assert_clean(self):
        self.assertEqual(list(self.remote_root.iterdir()), [])
        self.assertEqual(self.f.calls(['pr', 'merge']), [])

    def test_direct_bundle_runs_pinned_program_and_records_full_binding(self):
        target = self.program()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row, = self.rows()
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
                             os.path.realpath(self.policy['remote'][name]))
            self.assertTrue(execution['executables'][name]['version'])
        self.assertTrue(self.witness.exists())
        self.assert_clean()
        self.assertEqual(self.admitted().returncode, 0)

    def test_slurm_runs_both_claims_with_target_repetitions_and_handshake(self):
        self.policy['remote'].update(executor='slurm', slurm={
            'partition': 'fixture-cpu', 'mem': '2G', 'time': '00:05:00'})
        self.save_policy()
        self.shared.install_policy(repetitions=2)
        self.shared.candidate({'tests/test_remote.py': self.shared.counter_test()})
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
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
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.witness.exists())
        row, = self.rows()
        self.assertEqual(row['result'], 'incomplete')
        self.assertIn('tree digest mismatch', row['incomplete_reason'])
        self.assert_clean()

    def assert_retry_admits(self, mode, tail=''):
        self.program(tail)
        self.mode.write_text(mode)
        first = self.verify('--verification-timeout', '1')
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        incomplete = self.rows()[-1]
        self.f.assert_refused(self.admitted())
        self.mode.write_text('pass')
        second = self.verify('--verification-timeout', '10')
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        for field in V.MERGE_BASIS_FIELDS:
            self.assertEqual(incomplete[field], self.rows()[-1][field])
        self.assert_clean()
        admitted = self.admitted()
        self.assertEqual(admitted.returncode, 0, admitted.stdout + admitted.stderr)
        self.assertEqual(incomplete['result'], 'incomplete')

    def test_ssh_failure_then_pass_for_same_binding_is_admitted(self):
        self.assert_retry_admits('ssh-fail')

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
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        refused = self.admitted()
        self.f.assert_refused(refused)
        self.assertIn('FAIL', refused.stderr)
        self.assert_clean()

    def test_completed_verifier_fail_survives_a_failed_slurm_job(self):
        self.policy['remote'].update(executor='slurm', slurm={
            'partition': 'fixture-cpu', 'mem': '2G', 'time': '00:05:00'})
        self.save_policy()
        self.program('raise SystemExit(int(Path(%r).read_text() == "slurm-completed-fail"))\n'
                     % str(self.mode))
        self.mode.write_text('slurm-completed-fail')
        self.assertNotEqual(self.verify().returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'fail')
        self.assertEqual(row['execution']['sacct_state'], 'FAILED')
        self.mode.write_text('pass')
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.admitted()
        self.f.assert_refused(result)
        self.assertIn('FAIL', result.stderr)
        self.assert_clean()

    def test_forced_scheduler_restart_cannot_overwrite_completed_failure(self):
        self.policy['remote'].update(executor='slurm', slurm={
            'partition': 'fixture-cpu', 'mem': '2G', 'time': '00:05:00'})
        self.save_policy()
        self.program('raise SystemExit(int(Path(%r).read_text() == "slurm-forced-requeue"))\n'
                     % str(self.mode))
        self.mode.write_text('slurm-forced-requeue')
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual(row['result'], 'fail')
        self.assertEqual(self.mode.read_text(), 'pass')
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.admitted()
        self.f.assert_refused(result)
        self.assertIn('FAIL', result.stderr)
        self.assert_clean()

    def test_slurm_without_completed_worker_is_incomplete_and_retryable(self):
        self.policy['remote'].update(executor='slurm', slurm={
            'partition': 'fixture-cpu', 'mem': '2G', 'time': '00:05:00'})
        self.save_policy()
        self.assert_retry_admits('slurm-missing')

    def test_slurm_without_terminal_state_is_incomplete_and_retryable(self):
        self.policy['remote'].update(executor='slurm', slurm={
            'partition': 'fixture-cpu', 'mem': '2G', 'time': '00:05:00'})
        self.save_policy()
        self.assert_retry_admits('slurm-pending')

    def test_candidate_policy_cannot_choose_host_or_interpreter(self):
        self.shared.candidate({RV.POLICY: json.dumps({
            'schema_version': 1, 'remote': {'ssh_alias': 'candidate-host'},
            'local': {'python': '/candidate/python'}})})
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
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
        self.policy.pop('remote')
        self.save_policy()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        execution = self.rows()[-1]['execution']
        self.assertEqual(execution['location'], 'local')
        self.assertEqual(execution['executables']['python']['path'], os.path.realpath(sys.executable))
        self.assertTrue(execution['executables']['git']['version'].startswith('git version'))
        self.assertFalse(Path(self.f.env['REMOTE_LOG']).exists())

    def test_declared_executables_are_used_for_checkout_and_verifier_children(self):
        log = self.f.directory / 'executables.log'
        for name in ('python', 'git'):
            actual = self.policy['remote'][name]
            wrapper = self.f.directory / ('declared-' + name)
            wrapper.write_text('#!' + sys.executable + '\nimport json, os, sys\n'
                               'with open(%r, "a") as log: log.write(json.dumps([%r] + sys.argv[1:]) + "\\n")\n'
                               'os.execv(%r, [%r] + sys.argv[1:])\n'
                               % (str(log), name, actual, actual))
            wrapper.chmod(0o755)
            self.policy['local'][name] = str(wrapper)
            self.policy['remote'][name] = str(wrapper)
        self.save_policy()
        self.shared.install_policy(repetitions=1)
        self.shared.candidate({'tests/test_executables.py': self.shared.counter_test()})
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(any(c[0] == 'python' and any('verifier' in a for a in c[1:]) for c in calls))
        self.assertTrue(any(c[0] == 'git' and 'checkout' in c for c in calls))
        self.assertTrue(any(c[0] == 'git' and 'diff' in c for c in calls))
        for row in self.rows():
            for name in ('python', 'git'):
                executable = row['execution']['executables'][name]
                self.assertEqual(executable['path'], self.policy['remote'][name])
                self.assertTrue(executable['version'])
        self.assert_clean()

    def test_default_policy_admission_ignores_ambient_git(self):
        (self.f.state_dir / RV.POLICY).unlink()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        git = self.f.bin / 'git'
        actual = str(git.resolve())
        git.unlink()
        git.write_text('#!' + sys.executable + '\nimport os, sys\n'
                       'if "show" in sys.argv and sys.argv[-1].endswith(":verifiers.json"):\n'
                       '    raise SystemExit("ambient Git must not read policy")\n'
                       'os.execv(%r, [%r] + sys.argv[1:])\n' % (actual, actual))
        git.chmod(0o755)
        result = self.admitted()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_generic_integration_policy_reads_and_admission_use_declared_git(self):
        self.policy.pop('remote')
        log = self.f.directory / 'generic-git.log'
        actual = self.policy['local']['git']
        wrapper = self.f.directory / 'generic-git'
        wrapper.write_text('#!' + sys.executable + '\nimport json, os, sys\n'
                           'with open(%r, "a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                           'os.execv(%r, [%r] + sys.argv[1:])\n' % (str(log), actual, actual))
        wrapper.chmod(0o755)
        self.policy['local']['git'] = str(wrapper)
        self.save_policy()
        result = subprocess.run([
            sys.executable, S.__file__, 'verify', '--state-dir', str(self.f.state_dir),
            '--unit', 'u', '--attempt', str(self.f.attempt), '--claim', V.INTEGRATION_CLAIM,
            '--target-commit', self.f.base, '--verifier', V.MERGE_VERIFIER,
            '--path', str(self.f.repo / V.MERGE_VERIFIER_PATH)],
            cwd=self.f.repo, env=self.f.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
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
        self.policy.pop('remote')
        self.save_policy()
        helper = self.f.bin / 'fixture-host-helper'
        helper.write_text('#!' + sys.executable + '\nprint("HOST_HELPER_RAN")\n')
        helper.chmod(0o755)
        self.f.env.update(OPENAI_API_KEY='fixture-secret', SSH_AUTH_SOCK='fixture-agent')
        self.program('import os, subprocess\n'
                     'assert "OPENAI_API_KEY" not in os.environ\n'
                     'assert "SSH_AUTH_SOCK" not in os.environ\n'
                     'subprocess.run(["fixture-host-helper"], check=True)\n')
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('HOST_HELPER_RAN', self.rows()[-1]['stdout_tail'])

    def test_child_launcher_exec_failure_is_incomplete_and_retryable(self):
        self.policy.pop('remote')
        self.save_policy()
        self.program()
        site = self.f.directory / 'launcher-fault'
        site.mkdir()
        (site / 'sitecustomize.py').write_text(
            'import os, sys\n'
            'original = os.execv\n'
            'def launch(path, args):\n'
            '    if sys.argv[0] == "-c" and any("pinned-verifier-" in x for x in sys.argv[1:]):\n'
            '        raise OSError("injected child launcher exec failure")\n'
            '    return original(path, args)\n'
            'os.execv = launch\n')
        self.f.env['PYTHONPATH'] = str(site)
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.witness.exists())
        self.assertEqual(self.rows()[-1]['result'], 'incomplete')
        self.assertIn('child launcher', self.rows()[-1]['incomplete_reason'])
        self.f.env.pop('PYTHONPATH')
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.admitted()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_verifier_cannot_report_a_launcher_error(self):
        self.policy.pop('remote')
        self.save_policy()
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

    def test_candidate_executable_path_in_coordinator_policy_is_refused(self):
        self.policy['local']['python'] = str(self.f.repo / 'python3')
        self.save_policy()
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inside the operated repository', result.stderr)
        self.assertFalse(Path(self.f.env['REMOTE_LOG']).exists())
        self.assertEqual(self.rows(), [])

    def test_candidate_executable_is_refused_during_receipt_admission(self):
        self.policy.pop('remote')
        original = dict(self.policy['local'])
        for name in ('python', 'git'):
            with self.subTest(executable=name):
                program = self.f.repo / ('candidate-' + name)
                marker = self.f.directory / ('candidate-executed-' + name)
                program.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n'
                                   'Path(%r).write_text("executed")\nprint("fixture version")\n' % str(marker))
                program.chmod(0o755)
                self.policy['local'] = dict(original, **{name: str(program)})
                self.save_policy()
                result = self.admitted()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists(), 'admission executed a candidate-supplied executable')
                self.assertIn('inside the operated repository', result.stderr)
                self.assertEqual(self.f.calls(['pr', 'merge']), [])

    def test_missing_remote_execution_evidence_is_not_legacy_local(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row, = self.rows()
        row.pop('execution')
        (self.f.state_dir / S.VERIFY_RECEIPTS).write_text(json.dumps(row) + '\n')
        result = self.admitted()
        self.f.assert_refused(result)
        self.assertIn('missing execution evidence', result.stderr)

    def test_retained_remote_digest_loss_corrects_persisted_verified_labels(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
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
        saved = json.loads((self.f.state_dir / S.STATE_FILE).read_text())
        self.assertEqual(saved['units']['u']['merge_receipt']['integration_status'],
                         'integration-unverified')
        self.assertEqual(saved['units']['u']['state'], 'READY_FOR_PR')
        self.assertNotIn(' advance ', result.stdout)
        self.assertEqual(len(self.f.calls(['pr', 'merge'])), 1)

    def test_remote_receipt_cannot_lose_verified_digest_on_admission(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row, = self.rows()
        row['execution'].pop('verified_tree')
        (self.f.state_dir / S.VERIFY_RECEIPTS).write_text(json.dumps(row) + '\n')
        result = self.admitted()
        self.f.assert_refused(result)
        self.assertIn('tree digest', result.stderr)


if __name__ == '__main__':
    unittest.main()
