"""Real installation publication; no activation patch or hand-written records."""
from pathlib import Path
import uuid

import d5_install as install
import d5_readiness as readiness
import d5_protocol_locks as protocol
import swarm_state as state


def ready_installation(tmp, entry=None, *, state_dir=None, record_dir=None):
    """Publish under the real installer lease and DRAIN/START protocol.

    A standalone fixture needs only filesystem bindings, not grant authority.
    Registry consumers supply the entry returned by real installer staging.
    """
    tmp = Path(tmp).resolve()
    if entry is None:
        directory = Path(state_dir).resolve() if state_dir is not None else tmp / 'state'
        record = Path(record_dir).resolve() if record_dir is not None else tmp / 'record'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        record.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = lambda p: [p.stat().st_dev, p.stat().st_ino]
        entry = dict(registration_id=uuid.uuid4().hex, plan_id='p',
                     state_dir=str(directory), record_dir=str(record),
                     state_key=key(directory), record_key=key(record), repo_key=key(tmp))
    with readiness.installer_lease(entry['state_dir']):
        with protocol.pin(entry['record_dir']) as pinned:
            with pinned.drain(shared=False), pinned.start():
                readiness.successor_gate(entry)
                readiness.withdraw(Path(entry['state_dir']))
                readiness.publish_installation(entry)
    return entry


def ready(case, checkout, plan_id, state_dir, promote=True):
    ok, reason = state.acquire_lease(state_dir, bump_epoch=False)
    if not ok:
        raise AssertionError(reason)
    case.addCleanup(state.release_lease, state_dir)
    entry = install.stage(checkout, plan_id, state_dir)
    if promote:
        ready_installation(Path(state_dir).parent, entry)
    return entry


from contextlib import contextmanager, ExitStack
import builtins
import io
import os
import sys
import tempfile
from unittest import mock
import d5_installation as classifier


@contextmanager
def missing_installation(entry):
    """Corruption fixture: remove both state witnesses, then restore exact bytes.

    This simulates loss of all local installation evidence, an accepted same-UID
    limit. It is not withdrawal: normal withdrawal keeps the sentinel.
    """
    directory = Path(entry['state_dir'])
    original = directory.stat()
    with tempfile.TemporaryDirectory() as tmp:
        moved = []
        absent = original
        try:
            for name in (readiness.MARKER, readiness.SENTINEL):
                source, target = directory / name, Path(tmp) / name
                if source.exists():
                    source.rename(target)
                    moved.append((source, target))
            absent = directory.stat()
            yield
        finally:
            after = directory.stat()
            untouched = (after.st_ino, after.st_mtime_ns) == (absent.st_ino, absent.st_mtime_ns)
            for source, target in reversed(moved):
                target.rename(source)
            # Undo fixture metadata only when the consumer left it untouched.
            # An extra entry or write must remain visible to snapshot assertions.
            if untouched:
                os.utime(directory, ns=(original.st_atime_ns, original.st_mtime_ns))


@contextmanager
def classification_only(state_dir):
    """OD-U8R-8: only the pure classifier's named read-only filesystem calls."""
    directory = Path(state_dir).absolute()
    allowed = {str(p) for p in (directory,) + tuple(directory.parents)}
    allowed.update(str(directory / name) for name in (readiness.MARKER, readiness.SENTINEL))
    def check(path):
        frame = sys._getframe(2)
        while frame and frame.f_code.co_filename != classifier.__file__:
            frame = frame.f_back
        if frame is None or not isinstance(path, (str, bytes, os.PathLike)) or os.fsdecode(path) not in allowed:
            raise AssertionError('inactive IO outside classifier or named paths: ' + repr(path))
    def reader(real):
        def call(path, *args, **kwargs):
            check(path)
            return real(path, *args, **kwargs)
        return call
    def opened(real, flags=False):
        def call(path, mode='r', *args, **kwargs):
            check(path)
            selected = kwargs.get('flags' if flags else 'mode', mode)
            if flags:
                if not isinstance(selected, int) or selected & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    raise AssertionError('inactive write')
            elif selected not in ('r', 'rb', 'rt'):
                raise AssertionError('inactive write')
            return real(path, mode, *args, **kwargs)
        return call
    with ExitStack() as stack:
        for name in ('lstat', 'stat', 'readlink'):
            stack.enter_context(mock.patch.object(os, name, reader(getattr(os, name))))
        stack.enter_context(mock.patch.object(os, 'open', opened(os.open, flags=True)))
        for target in (builtins, io):
            stack.enter_context(mock.patch.object(target, 'open', opened(target.open)))
        for name in ('mkdir', 'makedirs', 'unlink', 'remove', 'rename', 'replace', 'rmdir', 'chmod', 'utime'):
            stack.enter_context(mock.patch.object(os, name, side_effect=AssertionError('inactive mutation: ' + name)))
        yield


class InstallationWitnesses:
    """Explicit corruption/restoration used by ordered completion fixtures."""
    def __init__(self, entry):
        self.entry = entry
        self._missing = None

    @property
    def present(self):
        return self._missing is None

    @present.setter
    def present(self, value):
        if value and self._missing is not None:
            self._missing.__exit__(None, None, None)
            self._missing = None
        elif not value and self._missing is None:
            self._missing = missing_installation(self.entry)
            self._missing.__enter__()

    def close(self):
        self.present = True
