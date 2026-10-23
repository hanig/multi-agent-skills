"""Opt-in R10 mutation experiments; serial, independent, exact-byte restoration.

Run from a clean attempt worktree. No mutation is committed. Each experiment
runs its acceptance test before, during and after the substitution. This is
not imported by unittest discovery and never activates a production caller.
"""
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'skills/hanig-orchestrate/scripts'
MODULE = 'tests.test_d5_acceptance.TestAcceptance.'
MUTATIONS = [
    ('M1-ACCEPT-REVOKED', 'd5_authority.py',
     'if record["invalidation"] is not None:\n        return "invalidated", None',
     'if False:\n        return "invalidated", None',
     'test_M1_revocation_before_admission'),
    ('M2-DROP-RECORDED-INVALIDATION', 'd5_control.py',
     'return store.check(entry["record_dir"], now, mandate)',
     'return "mandate_changed"',
     'test_M2_recorded_mandate_change_before_admission'),
    ('M3-REAUTHORIZE-COMPLETION', 'd5_completion.py',
     'return ledger.complete(self._handle, outcome)',
     'from d5_authority_store import read\n'
     '                if read(self._handle._directory)["invalidation"] is not None:\n'
     '                    raise ledger.EffectError("mutant current authority refusal")\n'
     '                return ledger.complete(self._handle, outcome)',
     'test_M3_completion_after_revocation_and_B_refusal'),
    ('M4-FINISH-UNDER-START', 'd5_submission.py',
     'outcome = boundary.finish()',
     'with pinned.start():\n                        outcome = boundary.finish()',
     'test_M4_revocation_persists_before_response_release'),
    ('M5-UNLOCK-TRANSFERABLE-DRAIN', 'd5_locks.py',
     'with self._track(0, shared, fd):\n                yield fd',
     'with self._track(0, shared, fd):\n                try:\n'
     '                    yield fd\n                finally:\n'
     '                    fcntl.flock(fd, fcntl.LOCK_UN)',
     'test_M5_surviving_child_blocks_successor_until_accounted_and_closed'),
    ('M6-ALLOW-AMBIENT-START', 'd5_submission.py',
     'if pinned._record._prior(1) is not None:', 'if False:',
     'test_M6_ambient_START_refusals_and_sequential_accounting'),
    ('M7-EXPORT-WORKER-DRAIN', 'd5_process.py',
     'return subprocess.Popen(argv, close_fds=True, pass_fds=(),',
     'return subprocess.Popen(argv, close_fds=True, '
     'pass_fds=(int(__import__("json").loads(argv[-1])[0]),),',
     'test_M7_live_worker_inherits_no_protection'),
    ('M8-ACCEPT-EXPIRY-EQUALITY', 'd5_authority.py',
     'now >= record["expires_at"]', 'now > record["expires_at"]',
     'test_M8_final_expiry_equality_after_preparation'),
    ('M9-EFFECT-BEFORE-CHILD-ADMISSION', 'd5_effect_child.py',
     'result = submission.submit(checkout, plan_id, state_dir,',
     'boundary.start(operation)\n'
     '        result = submission.submit(checkout, plan_id, state_dir,',
     'test_M9_launched_helper_must_admit_after_control'),
]


def run(test):
    command = [sys.executable, '-B', '-m', 'unittest', '-v', MODULE + test]
    print('$ ' + ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=120)
    print(result.stdout.decode(), end='', flush=True)
    print('exit=' + str(result.returncode), flush=True)
    return result.returncode


def clear_cache(path):
    # Python may otherwise reuse a same-size, same-second mutant pyc.
    for cache in path.parent.glob('__pycache__/' + path.stem + '.*.pyc'):
        cache.unlink()


def main():
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=str(ROOT))
    if status:
        raise SystemExit('mutation experiments require a clean worktree')
    for name, source, old, new, test in MUTATIONS:
        path = SCRIPTS / source
        before = path.read_bytes()
        original = hashlib.sha256(before).hexdigest()
        if before.count(old.encode()) != 1:
            raise AssertionError('nonunique mutation site: ' + name)
        print('\n' + name + ' sha256=' + original, flush=True)
        print('OLD: ' + repr(old) + '\nNEW: ' + repr(new), flush=True)
        if run(test) != 0:
            raise AssertionError('baseline failed: ' + name)
        try:
            path.write_bytes(before.replace(old.encode(), new.encode()))
            clear_cache(path)
            killed = run(test) != 0
        finally:
            path.write_bytes(before)
            clear_cache(path)
            assert path.read_bytes() == before
            print('restored sha256=' + hashlib.sha256(path.read_bytes()).hexdigest(), flush=True)
        if not killed:
            raise AssertionError('mutation survived: ' + name)
        if run(test) != 0:
            raise AssertionError('restored test failed: ' + name)
    print('Nine independent mutants killed; exact source bytes restored.', flush=True)


if __name__ == '__main__':
    main()
