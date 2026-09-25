#!/usr/bin/env python3
"""Repeat PR-changed test modules in the supplied disposable candidate merge.

The connected operator supplies identities and repetitions from target-pinned
policy, never from candidate configuration. Tests remain candidate bytes and
run as the operator's user; this is a stability check, not a hostile-code sandbox.
"""
import argparse
import glob
import os
from pathlib import Path
import re
import shutil
import subprocess


# Use unittest's normal discovery and load_tests hook (including the supervised
# review module), but a selected file that discovers zero cases is not evidence.
RUN_MODULE = """import os, sys, unittest
# Keep the helper import path supplied by the integration runner's -s tests,
# while allowing discovery to retain package identities for relative imports.
sys.path.insert(0, os.path.abspath('tests'))
program = unittest.main(module=None, argv=[sys.argv[0], 'discover', '-s',
                         sys.argv[1], '-p', sys.argv[2], '-t', sys.argv[3]], exit=False)
if not program.result.testsRun:
    raise SystemExit('changed test module discovered no tests')
raise SystemExit(0 if program.result.wasSuccessful() else 1)
"""


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
    git = shutil.which("git", path=os.defpath)
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
        for repetition in range(args.repetitions):
            print("{}: repetition {}/{}".format(
                module, repetition + 1, args.repetitions), flush=True)
            result = subprocess.run(
                ["python3", "-c", RUN_MODULE, str(path.parent), glob.escape(path.name), top],
                check=False)
            if result.returncode:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
