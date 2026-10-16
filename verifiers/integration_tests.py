#!/usr/bin/env python3
"""Run the complete repository suite in the supplied candidate merge tree.

The operator reads these bytes from the trusted target and pins their digest.
Tests and implementation are the candidate tree's bytes; this is integration
validation, not an immutable test-corpus guarantee or an OS security boundary.
Modules use min(cpu_count or 1, 8) processes, overridden by the positive integer
HANIG_VERIFIER_WORKERS; each child has a two-hour timeout.
"""
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


MODULE_TIMEOUT = 7200
# Match the runner's final report, not nested unittest output from test bodies
# (including test_review's supervised worker). stdout and stderr share a file.
REPORT = re.compile(
    r"^Ran (\d+) tests? in [^\r\n]+\r?\n\r?\n"
    r"(OK|FAILED)(?: \(([^\r\n]*)\))?\r?$", re.MULTILINE)
# Only zero-result modules need a second discovery. The probe imports candidate
# code in a CHILD with the same discovery start/top-level directory. It runs no
# tests. A separate descriptor keeps incidental import output out of the count.
COUNT_DISCOVERY = """import json, os, sys, unittest
fd = int(sys.argv[2])
os.set_inheritable(fd, False)
suite = unittest.defaultTestLoader.discover('tests', pattern=sys.argv[1])
os.write(fd, json.dumps(suite.countTestCases()).encode('ascii'))
"""


def discover_modules(root):
    tests = root / 'tests'
    if not tests.is_dir():
        raise ValueError('tests/ is missing')
    for directory, subdirs, files in os.walk(str(tests)):
        if Path(directory) != tests and '__init__.py' in files:
            raise ValueError('package discovery is unsupported: ' +
                             str(Path(directory).relative_to(root)))
    return sorted(p.name for p in tests.iterdir()
                  if p.is_file() and fnmatch.fnmatchcase(p.name, 'test*.py'))


def worker_count():
    value = os.environ.get('HANIG_VERIFIER_WORKERS')
    if value is None:
        return min(os.cpu_count() or 1, 8)
    if not re.fullmatch(r'[0-9]+', value) or int(value) < 1:
        raise ValueError('HANIG_VERIFIER_WORKERS must be a positive integer')
    return int(value)


def run_child(argv, root, output, pass_fds=()):
    try:
        child = subprocess.run(argv, cwd=str(root), stdout=output,
                               stderr=subprocess.STDOUT, pass_fds=pass_fds,
                               timeout=MODULE_TIMEOUT,
                               env=dict(os.environ, PYTHONUNBUFFERED='1'))
    except subprocess.TimeoutExpired:
        return 'child timed out after {} seconds'.format(MODULE_TIMEOUT)
    except OSError as exc:
        return 'child could not start: {}'.format(exc)
    if child.returncode != 0:
        return 'child exited {}'.format(child.returncode)
    return None


def run_module(root, name):
    counts = dict(tests=0, failures=0, errors=0, skips=0)
    with tempfile.TemporaryFile() as output:
        problem = run_child([sys.executable, '-m', 'unittest', 'discover',
                             '-s', 'tests', '-p', name], root, output)
        output.seek(0)
        raw = output.read()
        reports = list(REPORT.finditer(raw.decode('utf-8', errors='replace')))
        if not reports:
            problem = problem or 'missing unittest completion report'
        else:
            report = reports[-1]
            counts['tests'] = int(report[1])
            details = {key.strip(): value for key, value in
                       re.findall(r'([a-zA-Z ]+)=(\d+)', report[3] or '')}
            for source, target in (('failures', 'failures'), ('errors', 'errors'),
                                   ('skipped', 'skips')):
                counts[target] = int(details.get(source, 0))
            if report[2] != 'OK':
                problem = problem or 'unittest reported failure'
            if not problem and counts['tests'] == 0:
                with tempfile.TemporaryFile() as count:
                    output.seek(0, os.SEEK_END)
                    problem = run_child(
                        [sys.executable, '-c', COUNT_DISCOVERY, name,
                         str(count.fileno())], root, output, (count.fileno(),))
                    count.seek(0)
                    try:
                        expected = json.loads(count.read())
                        if type(expected) is not int or expected < 0:
                            raise ValueError('invalid count')
                        if expected:
                            problem = 'discovery found {} tests but child ran zero'.format(expected)
                    except (ValueError, UnicodeError):
                        problem = problem or 'missing discovery count'
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
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
