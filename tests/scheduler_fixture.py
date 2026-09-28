"""Closed executable fixtures for tests that may discover a scheduler.

Only named ordinary tools are linked; scheduler/agent/forge executables must
be supplied by the individual test. Neither inherited PATH nor os.defpath is
appended, including when a scheduler happens to be installed in /usr/bin.
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = ("sh", "bash", "git", "basename", "cat", "chmod", "cp", "cut",
         "date", "df", "diff", "dirname", "env", "false", "find", "head", "hostname",
         "id", "ln", "ls", "mkdir", "mktemp", "mv", "perl", "ps", "pwd", "readlink",
         "rm", "sed", "sleep", "sort", "stat", "tail", "tar", "touch", "tr",
         "true", "uname", "wc", "xargs")


def closed_bin(directory, tools=TOOLS):
    """Materialize an allowlist, retaining this test runner's Python version."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name in tools:
        # Resolve only named ordinary tools from system defaults when the
        # launching PATH omits them; never append those directories to PATH.
        target = shutil.which(name) or shutil.which(name, path=os.defpath)
        if target and not (directory / name).exists():
            (directory / name).symlink_to(os.path.abspath(target))
    for name in ("python", "python3"):
        if not (directory / name).exists():
            (directory / name).symlink_to(sys.executable)
    return str(directory)


def isolated_module_path():
    """A unittest module fixture; per-test scheduler stubs may replace it."""
    temporary = tempfile.TemporaryDirectory(prefix="scheduler-free-")
    unittest.addModuleCleanup(temporary.cleanup)
    patch = mock.patch.dict(os.environ, {"PATH": closed_bin(temporary.name)})
    patch.start()
    unittest.addModuleCleanup(patch.stop)


def cleanup_module_path():
    """Enroll the module in Python 3.9.6's cleanup lifecycle.

    That runner drains registered cleanups only when tearDownModule exists;
    newer runners drain them unconditionally. The runner owns the actual
    cleanup call after this hook returns. The consumer regression checks
    both environment restoration and removal of the temporary directory.
    """
