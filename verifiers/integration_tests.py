#!/usr/bin/env python3
"""Run the complete repository suite in the supplied candidate merge tree.

The operator reads these bytes from the trusted target and pins their digest.
Tests and implementation are the candidate tree's bytes; this is integration
validation on POSIX hosts, not an immutable corpus or an OS security boundary.
Modules must be independent of process-global state in other test modules.
Modules use min(cpu_count or 1, 8) processes, overridden by the positive integer
HANIG_VERIFIER_WORKERS; each child has a two-hour timeout.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


MODULE_TIMEOUT = 7200
# Startup instrumentation preserves the exact `-m unittest discover` command.
# Restore both sys.path and PYTHONPATH before candidate imports, and chain any
# existing sitecustomize. Only the outer TestProgram can record completion;
# nested runners and early process exits cannot supply its result by printing.
BOOTSTRAP = """import os, sys
fd = __CHANNEL_FD__
os.set_inheritable(fd, False)
sys.path.remove(__BOOTSTRAP_DIR__)
previous_path = __ORIGINAL_PYTHONPATH__
if previous_path is None:
    os.environ.pop('PYTHONPATH', None)
else:
    os.environ['PYTHONPATH'] = previous_path
this_module = sys.modules.pop('sitecustomize')
try:
    __import__('sitecustomize')
except ModuleNotFoundError as exc:
    if exc.name != 'sitecustomize':
        raise
    sys.modules['sitecustomize'] = this_module
import unittest
original = unittest.TestProgram.runTests
original_init = unittest.TestProgram.__init__
getpid, write, render = os.getpid, os.write, repr
pid = getpid()
program = []
def initialize(self, *args, **kwargs):
    if not program:
        program.append(self)
    if sys.modules.get('sitecustomize') is this_module:
        del sys.modules['sitecustomize']
    return original_init(self, *args, **kwargs)
def runTests(self):
    if self is not program[0]:
        return original(self)
    found = self.test.countTestCases()
    try:
        return original(self)
    finally:
        result = getattr(self, 'result', None)
        if result is not None and getpid() == pid:
            record = dict(pid=pid, discovered=found, tests=result.testsRun,
                          failures=len(result.failures), errors=len(result.errors),
                          skips=len(result.skipped), successful=result.wasSuccessful(),
                          stopped=result.shouldStop)
            payload = render(record).encode('ascii')
            if write(fd, payload) != len(payload):
                raise RuntimeError('incomplete completion write')
unittest.TestProgram.__init__ = initialize
unittest.TestProgram.runTests = runTests
"""


def discover_modules(root):
    tests = root / 'tests'
    if not tests.is_dir():
        raise ValueError('tests/ is missing')
    seen = set()
    for directory, subdirs, files in os.walk(str(tests), followlinks=True):
        info = os.stat(directory)
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            subdirs[:] = []
            continue
        seen.add(identity)
        for name in subdirs:
            package = Path(directory) / name
            if (package / '__init__.py').is_file():
                raise ValueError('package discovery is unsupported: ' +
                                 str(package.relative_to(root)))
    return sorted(p.name for p in tests.iterdir()
                  if p.is_file() and fnmatch.fnmatchcase(p.name, 'test*.py')
                  and re.match(r'[_a-z]\w*\.py$', p.name, re.IGNORECASE))


def worker_count():
    value = os.environ.get('HANIG_VERIFIER_WORKERS')
    if value is None:
        return min(os.cpu_count() or 1, 8)
    if not re.fullmatch(r'[0-9]+', value) or int(value) < 1:
        raise ValueError('HANIG_VERIFIER_WORKERS must be a positive integer')
    return int(value)


def completion(raw, pid):
    counts = dict(tests=0, failures=0, errors=0, skips=0)
    try:
        record = ast.literal_eval(raw.decode('ascii'))
        numeric = set(counts) | {'pid', 'discovered'}
        if (not isinstance(record, dict)
                or set(record) != numeric | {'successful', 'stopped'}
                or any(type(record[k]) is not int or record[k] < 0 for k in numeric)
                or record['pid'] != pid
                or type(record['successful']) is not bool
                or type(record['stopped']) is not bool):
            raise ValueError('invalid record')
    except (ValueError, SyntaxError, UnicodeError):
        return counts, 'missing or invalid unittest completion record'
    counts = {key: record[key] for key in counts}
    if record['discovered'] and not record['tests']:
        return counts, 'discovery found {} tests but child ran zero'.format(record['discovered'])
    if (not record['successful'] or record['stopped']
            or record['failures'] or record['errors']):
        return counts, 'unittest reported an unsuccessful or stopped run'
    return counts, None


def run_module(root, name):
    counts = dict(tests=0, failures=0, errors=0, skips=0)
    problem = None
    with tempfile.TemporaryDirectory() as startup, \
            tempfile.TemporaryFile() as record, tempfile.TemporaryFile() as output:
        previous = os.environ.get('PYTHONPATH')
        hook = BOOTSTRAP.replace('__CHANNEL_FD__', str(record.fileno()))
        hook = hook.replace('__BOOTSTRAP_DIR__', repr(startup))
        hook = hook.replace('__ORIGINAL_PYTHONPATH__', repr(previous))
        Path(startup, 'sitecustomize.py').write_text(hook)
        env = dict(os.environ, PYTHONPATH=startup +
                   (os.pathsep + previous if previous is not None else ''))
        try:
            child = subprocess.Popen(
                [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', name],
                cwd=str(root), stdout=output, stderr=subprocess.STDOUT,
                pass_fds=(record.fileno(),), env=env)
            try:
                child.wait(timeout=MODULE_TIMEOUT)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                problem = 'child timed out after {} seconds'.format(MODULE_TIMEOUT)
            record.seek(0)
            counts, record_problem = completion(record.read(4097), child.pid)
            # Python 3.12 uses exit 5 for zero tests; older versions use 0.
            # Only a complete, successful, genuinely empty discovery accepts 5.
            empty = (sys.version_info >= (3, 12) and not record_problem
                     and not counts['tests'] and child.returncode == 5)
            if child.returncode != 0 and not empty:
                problem = problem or 'child exited {}'.format(child.returncode)
            if record_problem:
                problem = '; '.join(p for p in (problem, record_problem) if p)
        except OSError as exc:
            problem = 'child could not start: {}'.format(exc)
        output.seek(0)
        raw = output.read()
    return name, raw, counts, problem


def run_modules(root, modules, workers):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Consume in submission order; faster later modules wait for their turn
        # to print. Each output is complete, never interleaved with another.
        futures = [pool.submit(run_module, root, name) for name in modules]
        for name, future in zip(modules, futures):
            try:
                yield future.result()
            except Exception as exc:
                yield name, b'', dict(tests=0, failures=0, errors=0, skips=0), str(exc)


def main():
    started = time.monotonic()
    root = Path.cwd()
    try:
        workers = worker_count()
        modules = discover_modules(root)
    except (OSError, ValueError) as exc:
        print('parallel verifier refused: {}'.format(exc), flush=True)
        return 2
    totals = dict(tests=0, failures=0, errors=0, skips=0)
    ran = []
    failed = 0
    for name, output, counts, problem in run_modules(root, modules, workers):
        ran.append(name)
        print('=== {} ==='.format(name), flush=True)
        sys.stdout.buffer.write(output)
        if output and not output.endswith(b'\n'):
            sys.stdout.buffer.write(b'\n')
        sys.stdout.buffer.flush()
        for key in totals:
            totals[key] += counts[key]
        if problem:
            failed += 1
            print('{}: FAIL: {}'.format(name, problem), flush=True)
        elif not counts['tests']:
            print('{}: discovered zero tests'.format(name), flush=True)
    if set(ran) != set(modules) or len(ran) != len(modules):
        failed += 1
        print('module set mismatch: missing={} unexpected={}'.format(
            sorted(set(modules) - set(ran)), sorted(set(ran) - set(modules))), flush=True)
    print('SUMMARY tests={tests} failures={failures} errors={errors} skips={skips} '
          'modules={modules} failed_modules={failed} wall_seconds={wall:.3f}'.format(
              modules=len(ran), failed=failed, wall=time.monotonic() - started,
              **totals), flush=True)
    if not modules and sys.version_info >= (3, 12):
        return 5  # Match discovery when there are no modules at all.
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
