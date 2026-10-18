#!/usr/bin/env python3
"""Local unittest triage. The class is a hint, never exoneration: an untouched
 test file does not exclude a regression in production code the test exercises,
 and isolated reruns miss order-dependent flakes. No CI, state or tracker writes.

Each run executes in a standalone scratch repository with no remote, alternates or shared Git directory linking it to the operator's repository, and with GIT_* environment variables removed; the operator repository's Git state is not reachable through the scratch. Tests still run with the operator's full privileges and can affect anything reachable by absolute path. Processes that detach into a new session may outlive the tool; this is a declared limit, not closed.
"""
import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess as SP
import sys
import tempfile

NOTICE = ("The class is a hint, never exoneration: an untouched test file does not exclude "
          "a regression in production code the test exercises, and isolated reruns miss "
          "order-dependent flakes. Merge still requires a complete green run.")
NOTICE += "\nEach run executes in a standalone scratch repository with no remote, alternates or shared Git directory linking it to the operator's repository, and with GIT_* environment variables removed; the operator repository's Git state is not reachable through the scratch. Tests still run with the operator's full privileges and can affect anything reachable by absolute path. Processes that detach into a new session may outlive the tool; this is a declared limit, not closed."
RERUNS = {
    "SETUP_ERROR": "Repair setup or head test id and repeat triage.",
    "FAILS_ON_BASE": "Diagnose base failure before a full CI/verifier rerun.",
    "FLAKY_ON_HEAD": "Request a complete rerun of the failed CI/verifier; track the flake.",
    "CONSISTENT_FAILURE": "Repair head before requesting a rerun.",
    "INCONCLUSIVE": "Request a complete rerun of the failed CI/verifier.",
}

class SetupError(ValueError):
    pass

def child_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith("GIT_") and k not in ("PYTHONPATH", "PYTHONHOME")}

@contextmanager
def signals_ignored():
    old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)

def execute(argv, cwd, timeout):
    # Detached descendants cannot hold these open as pipes.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = SP.Popen(argv, cwd=str(cwd), env=child_env(), stdin=SP.DEVNULL,
                     stdout=out, stderr=err, start_new_session=True)
        expired = False
        try:
            try:
                p.wait(timeout=timeout)
            except SP.TimeoutExpired:
                expired = True
        finally:
            with signals_ignored():
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()
        out.seek(0)
        err.seek(0)
        return dict(returncode=p.returncode, stdout=out.read().decode('utf-8', 'replace'),
                    stderr=err.read().decode('utf-8', 'replace'), timed_out=expired)

def git(repo, *args, check=True):
    r = execute(['git', '-C', str(repo), *args], repo, 60)
    if check and (r['returncode'] or r['timed_out']):
        raise SetupError('git {}: {}'.format(args[0], r['stderr'] or 'timed out'))
    return r

def commit(repo, ref):
    if not ref or ref.startswith('-'):
        raise SetupError('commit must be a non-option revision')
    sha = git(repo, 'rev-parse', '--verify', ref + '^{commit}')['stdout'].strip()
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
        raise SetupError('not a full commit id')
    return sha

def restore_directory_access(tree):
    # Restore disposable directories, never symlink targets.
    if tree.is_symlink() or not tree.is_dir():
        return
    tree.chmod(tree.stat().st_mode | 0o700)
    for parent, directories, _ in os.walk(str(tree), followlinks=False):
        for name in directories:
            path = Path(parent) / name
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o700)

@contextmanager
def scratch_repository(repo, sha):
    root = Path(tempfile.mkdtemp(prefix='flake-triage-')).resolve()
    tree = root / 'checkout'
    try:
        tree.mkdir()
        git(tree, 'init', '--template=')
        git(tree, 'fetch', '--no-tags', str(repo), sha)
        git(tree, 'checkout', '--detach', 'FETCH_HEAD')
        yield tree
    finally:
        with signals_ignored():
            restore_directory_access(tree)
            shutil.rmtree(str(root))

def outcome(r, module_file):
    if r['timed_out']:
        return 'timeout'
    err = r['stderr']
    if re.search(r'^ERROR: .*\(unittest\.loader\._FailedTest[.)]', err, re.M):
        frames = re.findall(r'File "([^"]+)", line \d+, in ([^\n]+)', err)
        loader_origin = not frames or (frames[-1][0].endswith('/unittest/loader.py')
                                       and frames[-1][1] == 'loadTestsFromName')
        missing_attribute = loader_origin and re.search(
            r'^AttributeError: (?:module|type object) .* has no attribute ', err, re.M)
        if not module_file.is_file() or missing_attribute:
            return 'absent'
    if r['returncode'] == 0:
        return 'pass' if re.search(r'^Ran [0-9]+ tests? in ', err, re.M) else 'error'
    summaries = re.findall(r'^FAILED \(([^\n]*)\)$', err, re.M)
    return 'fail' if summaries and 'failures=' in summaries[-1] and 'errors=' not in summaries[-1] else 'error'

def classify(head, base, errors):
    if errors or 'absent' in head:
        return 'SETUP_ERROR'
    base = [v for v in base if v != 'absent']
    if any(v != 'pass' for v in base):
        return 'FAILS_ON_BASE'
    if 'pass' in head and any(v != 'pass' for v in head):
        return 'FLAKY_ON_HEAD'
    if head and all(v != 'pass' for v in head) and all(v == 'pass' for v in base):
        return 'CONSISTENT_FAILURE'
    return 'INCONCLUSIVE'

def issue_command(title, body_file='BODY_FILE', binding='BINDING', approver='APPROVER'):
    return ' '.join("'" + v.replace("'", "'\"'\"'") + "'" for v in (
        'linear_sync.py', 'issue', 'new', '--binding', binding, '--title', title,
        '--body-file', body_file, '--approver', approver))

def test_id(value):
    if not re.fullmatch(r'tests\.[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){0,2}', value, re.ASCII):
        raise argparse.ArgumentTypeError('use tests.MODULE[.CLASS[.METHOD]]')
    return value

def triage(a):
    repo = Path(a.repo).resolve()
    repo = Path(git(repo, 'rev-parse', '--show-toplevel')['stdout'].strip()).resolve()
    head, base = commit(repo, a.head), commit(repo, a.base)
    python = shutil.which(a.python)
    if not python:
        raise SetupError('Python executable not found: ' + a.python)
    python = os.path.abspath(python)
    changed = git(repo, 'diff', '--name-only', '--no-renames', '-z', base + '...' + head, '--')['stdout'].split('\0')
    report = dict(repo=str(repo), head=head, base=base, notice=NOTICE, tests=[])
    for name in a.test:
        file = '/'.join(name.split('.')[:2]) + '.py'
        d = dict(test=name, file=file, touched=file in changed, head=[], base=[], setup_errors=[])
        for side, sha in (('head', head), ('base', base)):
            try:
                with scratch_repository(repo, sha) as tree:
                    for _ in range(a.runs):
                        r = execute([python, '-m', 'unittest', name], tree, a.timeout)
                        r['outcome'] = outcome(r, tree / file)
                        d[side].append(r)
            except (OSError, SetupError, SP.SubprocessError) as exc:
                d['setup_errors'].append(side + ': ' + str(exc))
        d['class'] = classify(*[[r['outcome'] for r in d[s]] for s in ('head', 'base')], d['setup_errors'])
        d['suggested_rerun'] = RERUNS[d['class']]
        d['issue_title'] = 'Investigate {}: {}'.format(d['class'], name)
        d['issue_body'] = '\n'.join([
            'TL;DR: ' + d['issue_title'], 'Head: {}; base: {}; PR: {}'.format(head, base, a.pr),
            'File: {}; touched by BASE...HEAD: {}'.format(file, d['touched']),
            'Python: {}; timeout: {}s'.format(python, a.timeout),
            *['{} run {}: {}'.format(s, i, r['outcome']) for s in ('head', 'base') for i, r in enumerate(d[s], 1)],
            *d['setup_errors'], 'Base absence is unavailable comparison evidence; new tests classify by head.',
            d['suggested_rerun'], NOTICE])
        d['issue_command'] = issue_command(d['issue_title'])
        report['tests'].append(d)
    return report

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ('repo', 'head', 'base'):
        p.add_argument('--' + flag, required=True)
    p.add_argument('--test', required=True, action='append', type=test_id)
    p.add_argument('--runs', type=int, default=5)
    p.add_argument('--timeout', type=float, default=60)
    p.add_argument('--python', default=sys.executable)
    p.add_argument('--pr', type=int)
    p.add_argument('--json', action='store_true')
    a = p.parse_args(argv)
    if a.runs <= 0 or not math.isfinite(a.timeout) or a.timeout <= 0 or (a.pr is not None and a.pr <= 0):
        p.error('runs, timeout and PR must be positive; timeout must be finite')
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    old = {s: signal.signal(s, interrupted) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        report = triage(a)
        if a.json:
            print(json.dumps(report, indent=2))
        else:
            for d in report['tests']:
                print('Suggested issue title: ' + d['issue_title'])
                print('Suggested issue body:\n' + d['issue_body'])
                print('Replace BODY_FILE, BINDING and APPROVER; command not executed:')
                print(d['issue_command'])
        return 2 if any(d['class'] == 'SETUP_ERROR' for d in report['tests']) else 0
    except (OSError, ValueError, SP.SubprocessError) as exc:
        print(json.dumps(dict(classification='SETUP_ERROR', error=str(exc), notice=NOTICE)))
        return 2
    except KeyboardInterrupt:
        print('Interrupted; triage incomplete.', file=sys.stderr)
        return 2
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)

if __name__ == '__main__':
    sys.exit(main())
