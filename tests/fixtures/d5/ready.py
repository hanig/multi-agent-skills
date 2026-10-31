"""Real installation publication; no activation patch or hand-written records."""
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[3]
for bundle in ('hanig-swarm', 'hanig-orchestrate'):
    sys.path.insert(0, str(ROOT / 'skills' / bundle / 'scripts'))

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
import tempfile
import threading
import _thread
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


# Audit events reach cached constructors such as ``from io import FileIO``.
# This is test observation only; an empty per-thread stack has no effect.
_io_audits = threading.local()
_io_audit_installed = False


def _classification_audit(event, args):
    for guard in getattr(_io_audits, 'guards', ()):
        guard(event, args)


@contextmanager
def classification_only(state_dir):
    """Own-thread IO only, with both thread-start APIs forbidden in this window.

    Cached/indirect thread launch and arbitrary syscalls remain review-enforced.
    Subprocess calls are observed at the individual consumer boundary.
    """
    global _io_audit_installed
    directory = Path(state_dir).absolute()
    allowed = {str(p) for p in (directory,) + tuple(directory.parents)}
    allowed.update(str(directory / name) for name in (readiness.MARKER, readiness.SENTINEL))
    checked = []
    def check(path):
        frame = sys._getframe(2)
        while frame and frame.f_code.co_filename != classifier.__file__:
            frame = frame.f_back
        if frame is None or not isinstance(path, (str, bytes, os.PathLike)) or os.fsdecode(path) not in allowed:
            raise AssertionError('inactive IO outside classifier or named paths: ' + repr(path))
    def reader(real):
        def call(path, *args, **kwargs):
            check(path)
            checked.append(real)
            try:
                return real(path, *args, **kwargs)
            finally:
                checked.pop()
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
            checked.append(real)
            try:
                return real(path, mode, *args, **kwargs)
            finally:
                checked.pop()
        return call
    forbidden = {
            os: ('access', 'listdir', 'scandir', 'getcwd', 'getcwdb', 'chdir', 'fchdir',
                 'mkdir', 'makedirs', 'unlink', 'remove', 'rename', 'replace', 'rmdir',
                 'removedirs', 'renames', 'link', 'symlink', 'chmod', 'fchmod', 'lchmod',
                 'chown', 'fchown', 'lchown', 'utime', 'truncate', 'ftruncate', 'mkfifo',
                 'mknod', 'read', 'readv', 'pread', 'preadv', 'write', 'writev', 'pwrite',
                 'pwritev', 'sendfile', 'copy_file_range', 'splice', 'fstat', 'statvfs',
                 'fstatvfs', 'pathconf', 'fpathconf', 'getxattr', 'listxattr', 'setxattr',
                 'removexattr', 'posix_fallocate', 'fsync', 'fdatasync', 'lseek', 'dup',
                 'dup2', 'close'),
            Path: ('iterdir', 'glob', 'rglob', 'walk', 'mkdir', 'unlink', 'rmdir',
                   'rename', 'replace', 'touch', 'symlink_to', 'hardlink_to', 'link_to',
                   'chmod', 'lchmod'),
            io: ('FileIO', 'open_code'),
        }
    # Audit events cover cached constructors; profiling covers cached C calls
    # such as os.stat and methods on a file opened before this observation.
    reads = {getattr(os, name) for name in ('stat', 'lstat', 'readlink', 'open')}
    reads.update((builtins.open, io.open))
    filesystem_calls = set(reads)
    for owner, names in forbidden.items():
        filesystem_calls.update(getattr(owner, name) for name in names if hasattr(owner, name))
    def audit(event, args):
        if event == 'open':
            path, _mode, flags = args
            check(path)
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise AssertionError('inactive write')
    def profile(frame, event, call):
        if event == 'c_call':
            if call in filesystem_calls and not (checked and call in reads):
                raise AssertionError('inactive cached filesystem operation')
            handle = getattr(call, '__self__', None)
            # In-memory streams and these descriptor/state queries do no
            # filesystem IO (argparse queries stdout's descriptor for width).
            if (isinstance(handle, io.IOBase)
                    and not isinstance(handle, (io.StringIO, io.BytesIO))
                    and getattr(call, '__name__', '') not in ('fileno', 'readable', 'writable', '__enter__')):
                check(handle.name)
                if handle.writable() or getattr(call, '__name__', '') not in (
                        'read', 'read1', 'readall', 'readinto', 'readinto1',
                        'readline', 'readlines', 'close', '__enter__', '__exit__'):
                    raise AssertionError('inactive file operation')
        if previous_profile is not None:
            previous_profile(frame, event, call)
    with ExitStack() as stack:
        for owner, name in ((threading.Thread, "start"), (_thread, "start_new_thread")):
            stack.enter_context(mock.patch.object(owner, name,
                side_effect=AssertionError("inactive thread start")))
        for owner in (os, Path):
            for name in ('lstat', 'stat', 'readlink'):
                if hasattr(owner, name):
                    stack.enter_context(mock.patch.object(owner, name, reader(getattr(owner, name))))
        stack.enter_context(mock.patch.object(os, 'open', opened(os.open, flags=True)))
        for target in (builtins, io, Path):
            stack.enter_context(mock.patch.object(target, 'open', opened(target.open)))
        for owner, names in forbidden.items():
            for name in names:
                if hasattr(owner, name):
                    stack.enter_context(mock.patch.object(owner, name,
                        side_effect=AssertionError('inactive filesystem operation: ' + name)))
        if not _io_audit_installed:
            sys.addaudithook(_classification_audit)
            _io_audit_installed = True
        guards = getattr(_io_audits, 'guards', None)
        if guards is None:
            guards = _io_audits.guards = []
        previous_profile = sys.getprofile()
        guards.append(audit)
        try:
            sys.setprofile(profile)
            yield
        finally:
            sys.setprofile(previous_profile)
            guards.pop()



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
