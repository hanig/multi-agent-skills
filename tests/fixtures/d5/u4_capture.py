"""Shared observable-effect recorders salvaged from U4.0b; no global oracle.

Per-unit contract (committee u40b-stepback, adopted by ARC-3727):
each U4.x unit that touches inactive paths must (1) declare the paths it
changes, (2) provide a before/after equivalence test on those paths (observable
effects only — subprocess argv, files written, tracker intents; no internal
call structure assertions), and (3) provide a mutation test proving premature
activation fails with zero production side effects.

Freeze rule: the per-unit contract specification changes only through a
separate ARC amendment.

M6 is now a per-unit requirement, met by each U4.x unit's equivalence test and
its premature-activation mutation. Later units cite this module's contract;
these recorders supply observations, never expectations or coverage claims.
Control fixture paths, clocks and identifiers before execution; do not scrub
differences out of captured effects. A unit's baseline must be independent of
its candidate implementation. Its premature-activation example must reach a
denying sentinel before any production effect and assert empty recordings.

Supported envelope and accepted limits
-------------------------------------
This prevents competing sessions from initiating operations. It does not
prevent planning mistakes by the valid holder or replace existing crash
reconciliation.

These test utilities do not enforce that production claim. Use disposable,
single-threaded POSIX fixtures, real direct-argv subprocesses and local outbox
files; never run a live scheduler or drain a tracker. The subprocess recorder
observes successful Popen launches in this interpreter during capture, not
grandchildren, previously cached Popen aliases, or shell expansion. It records
program/argv in their OS byte representation, not run/communicate calls or
Python argument container types. Native process behavior is retained. A failed
exec is an exception, not a successful launch record. Shell mode is refused
before launch rather than recording a misleading argv.

Filesystem snapshots retain regular-file bytes relative to the declared root,
including pre-existing files; compare snapshots to detect deletion/overwrites.
They are not a write-event log: transient or identical rewrites and metadata
are not observed. Start from an empty output root to capture files produced by
an example. Symlinks are refused rather than followed outside the fixture.
Tracker snapshots retain exact appended JSONL bytes, with order and duplicate
lines preserved, and diagnose a rewritten prefix. Neither reader interprets
authority or contacts a tracker. Join children and finish writes before reading.
"""

from contextlib import contextmanager
import inspect
import os
from pathlib import Path
import subprocess
from unittest import mock


class SubprocessRecorder:
    """Record (program bytes, argv tuple) while delegating to native Popen."""

    def __init__(self):
        self.commands = []

    @contextmanager
    def capture(self):
        native = subprocess.Popen
        signature = inspect.signature(native)
        commands = self.commands

        class RecordingPopen(native):
            def __init__(self, *positional, **keywords):
                options = signature.bind(*positional, **keywords).arguments
                if options.get("shell", False):
                    raise ValueError("capture requires direct argv, not shell mode")
                args = options["args"]
                # Native scalar and sequence forms retain their semantics;
                # one-shot iterables must be shared by capture and execution.
                if not isinstance(args, (str, bytes, os.PathLike, list, tuple)):
                    args = list(args)
                    if positional:
                        positional = (args,) + positional[1:]
                    else:
                        keywords["args"] = args
                argv = ([args] if isinstance(args, (str, bytes, os.PathLike))
                        else args)
                argv_bytes = tuple(os.fsencode(arg) for arg in argv)
                program = options.get("executable")
                program = os.fsencode(program) if program is not None else argv_bytes[0]
                super().__init__(*positional, **keywords)
                commands.append((program, argv_bytes))

        with mock.patch.object(subprocess, "Popen", RecordingPopen):
            yield self


class FilesystemRecorder:
    """Read exact retained bytes; the caller owns root and fixture lifetime."""

    def __init__(self, root):
        self.root = Path(root)

    def snapshot(self):
        if self.root.is_symlink():
            raise ValueError("capture root must not be a symlink")
        files = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink():
                raise ValueError("capture does not follow symlinks: " + str(path))
            if path.is_file():
                files[str(path.relative_to(self.root))] = path.read_bytes()
        return files


class TrackerIntentRecorder:
    """Observe appended local intent bytes after a caller-controlled baseline."""

    def __init__(self, path):
        self.path = Path(path)
        self.prefix = self._read()

    def _read(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return b""

    def snapshot(self):
        current = self._read()
        if not current.startswith(self.prefix):
            raise ValueError("tracker history was rewritten during capture")
        return tuple(current[len(self.prefix):].splitlines(keepends=True))
