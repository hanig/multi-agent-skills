#!/usr/bin/env python3
"""Repeat PR-changed test modules in the supplied disposable candidate merge.

The connected operator supplies identities and repetitions from target-pinned
policy, never from candidate configuration. Tests remain candidate bytes and
run as the operator's user; this is a stability check, not a hostile-code sandbox.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


# Load the selected module itself: package discovery can supply package tests
# or let a package hook hide this module. Its OWN load_tests stays authoritative
# (including the supervised review module). Count execution, not suite size.
RUN_MODULE = """import importlib, json, os, sys, unittest
def run():
    # An anonymous external channel, removed from argv before candidate import.
    # Do not leak it across exec into a delegated unittest worker.
    completion_fd = int(sys.argv.pop())
    os.set_inheritable(completion_fd, False)
    getpid, write, dumps = os.getpid, os.write, json.dumps
    runner_pid = getpid()
# Keep the helper import path supplied by the integration runner's -s tests,
# and the package root needed for the selected dotted module's relative imports.
    sys.path.insert(0, os.path.abspath(sys.argv[3]))
    sys.path.insert(0, os.path.abspath('tests'))
    expected_file = os.path.abspath(sys.argv[2])
    module = importlib.import_module(sys.argv[1])
    if module.__file__ != expected_file:
        raise SystemExit('changed test module imported from a different file')
    program = unittest.main(module=module, argv=[sys.argv[0]], exit=False)
    result = program.result
    # A forked descendant returning through this frame cannot finish the
    # original process's invocation after that process has exited early.
    if getpid() != runner_pid:
        raise SystemExit('changed test runner continued in a forked descendant')
    record = {'schema': 1, 'complete': True, 'pid': runner_pid,
              'tests_run': result.testsRun, 'failures': len(result.failures),
              'errors': len(result.errors), 'skips': len(result.skipped),
              'expected_failures': len(result.expectedFailures),
              'unexpected_successes': len(result.unexpectedSuccesses),
              'successful': result.wasSuccessful(), 'stopped': result.shouldStop}
    payload = dumps(record).encode('utf-8')
    if write(completion_fd, payload) != len(payload):
        raise SystemExit('incomplete changed test completion write')
    if not result.testsRun:
        raise SystemExit('changed test module executed no tests')
    raise SystemExit(0 if result.wasSuccessful() and not result.shouldStop else 1)
run()
"""


def completion_problem(raw, pid):
    """Validate one finished invocation independently of the child's exit code.

    The descriptor has no pathname advertised to candidate code. This is an
    accidental-termination guard under the existing trusted-writer convention,
    not secrecy from hostile same-process introspection or same-UID writers.
    """
    counts = ('tests_run', 'failures', 'errors', 'skips', 'expected_failures',
              'unexpected_successes')
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError):
        return 'missing or malformed changed test completion handshake'
    if (not isinstance(record, dict)
            or set(record) != set(counts) | {'schema', 'complete', 'pid', 'successful', 'stopped'}
            or type(record['schema']) is not int or record['schema'] != 1
            or record['complete'] is not True
            or type(record['pid']) is not int or record['pid'] != pid
            or any(type(record[key]) is not int or record[key] < 0 for key in counts)
            or type(record['successful']) is not bool or type(record['stopped']) is not bool):
        return 'invalid changed test completion handshake'
    if not record['tests_run']:
        return 'changed test module executed no tests'
    if (record['failures'] or record['errors'] or record['unexpected_successes']
            or not record['successful'] or record['stopped']):
        return 'changed test completion reports an unsuccessful or stopped run'
    return None


def run_repetition(name, module, top):
    # Each repetition owns a new unlinked file, outside the candidate tree.
    # Read it only after waiting for the exact child; stdout is never evidence.
    with tempfile.TemporaryFile() as completion:
        child = subprocess.Popen(
            [sys.executable, '-c', RUN_MODULE, name, module, top, str(completion.fileno())],
            pass_fds=(completion.fileno(),))
        code = child.wait()
        completion.seek(0)
        raw = completion.read(4097)
        problem = ('oversized changed test completion handshake' if len(raw) > 4096
                   else completion_problem(raw, child.pid))
        if problem:
            print(problem, file=sys.stderr, flush=True)
        return code == 0 and problem is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merge-base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repetitions", required=True, type=int)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    for value in (args.merge_base, args.head):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            parser.error("merge-base and head must be exact object IDs")
    # Do not let caller Git configuration or replacement refs select a different
    # diff. The disposable repository supplies only the candidate's Git objects.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_COUNT="0",
               GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    git = os.environ.get("HANIG_VERIFICATION_GIT") or shutil.which("git", path=os.defpath)
    if not git:
        raise SystemExit("system git is unavailable")
    diff = subprocess.run(
        [git, "--no-replace-objects", "diff", "--name-only", "-z",
         "--no-renames", "--no-ext-diff", "--diff-filter=AMT",
         args.merge_base, args.head, "--", "tests/"], env=env,
        stdout=subprocess.PIPE, check=True, timeout=60)
    modules = sorted(os.fsdecode(p) for p in diff.stdout.split(b"\0") if p
                     and Path(os.fsdecode(p)).name.startswith("test_")
                     and Path(os.fsdecode(p)).suffix == ".py")
    if not modules:
        print("changed-tests-stable: no changed test modules; pass", flush=True)
        return 0
    top = "." if Path("tests/__init__.py").is_file() else "tests"
    for module in modules:
        path = Path(module)
        if path.is_symlink() or not path.is_file():
            raise SystemExit("changed test module is not a regular candidate file: " + module)
        name = ".".join(path.relative_to(top).with_suffix("").parts)
        for repetition in range(args.repetitions):
            print("{}: repetition {}/{}".format(
                module, repetition + 1, args.repetitions), flush=True)
            if not run_repetition(name, module, top):
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
