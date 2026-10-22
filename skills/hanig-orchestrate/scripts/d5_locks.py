"""Inert U3a lock primitives; no production callers or activation change.

Supported envelope and accepted limits
-------------------------------------
- Local flock-capable host; stable record/lock identities.
- Same-record nesting.
- Test watchdogs may detect hangs; they may not authorize lock theft.

Non-claims
----------
- Does not validate authority.
- Does not change store callers or prove anything about external locking.

The caller resolves D through the registry's safe record-directory contract.
pin() pins that directory, not authority. Future operational entry points must
check activation and authority themselves. These primitives are inert like
the existing record store. No lock is unlinked, replaced, or explicitly unlocked.

Guards are lexical, thread-owned contexts. Nested shared contexts dup one
description and close only their own copy.
For delegation, explicitly pass the yielded shared fd to an exec child; import
it with drain(inherited_fd=fd). The caller guarantees it is a held shared drain
and owns/closes the original fd. Import checks file identity, not lock ownership.
Do not use an inherited Python manager after a raw fork of a threaded process.
"""
from contextlib import contextmanager
import os
import stat
import threading
import weakref

try:
    import fcntl
except ImportError:
    fcntl = None


class LockError(RuntimeError):
    """A lock prerequisite or supported acquisition rule was refused."""


_UNSAFE = "D5 lock refused: unsafe lock file"
_ORDER = "D5 lock refused: acquisition order"
_UPGRADE = "D5 custody change refused: caller holds shared drain"
_REFERENCE = "D5 lock refused: invalid shared drain reference"
_NAMES = {0: "authority.drain.lock", 1: "authority.start.lock", 2: "authority.lock"}
_DIR_FD = os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
_NOFOLLOW_STAT = os.stat in os.supports_follow_symlinks
_pool_lock = threading.Lock()
_pool = weakref.WeakValueDictionary()
_local = threading.local()


def _facilities():
    if (fcntl is None or not callable(getattr(fcntl, "flock", None)) or
            not _DIR_FD or not _NOFOLLOW_STAT or
            not all(hasattr(os, n) for n in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW"))):
        raise LockError("D5 lock refused: safe locking unavailable")


def _key(info):
    return info.st_dev, info.st_ino


def _safe(info):
    # Stable, same-UID infrastructure: private files without hard links.
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
            info.st_nlink == 1 and not info.st_mode & 0o077)


def _held():
    if not hasattr(_local, "held"):
        _local.held = []
    return _local.held


class _Entry:
    def __init__(self):
        self.guards = {1: threading.RLock(), 2: threading.RLock()}


class _Record:
    def __init__(self, fd):
        self.fd = fd
        self.key = _key(os.fstat(fd))
        with _pool_lock:
            self.entry = _pool.get(self.key)
            if self.entry is None:
                self.entry = _Entry()
                _pool[self.key] = self.entry

    def _prior(self, rank):
        if self.fd is None:
            raise LockError("D5 lock refused: closed record")
        held = _held()
        if any(key != self.key for key, _, _, _ in held):
            raise LockError("D5 lock refused: cross-record nesting")
        return next((item for item in reversed(held) if item[1] == rank), None)

    def _order(self, rank):
        if any(item[1] > rank for item in _held()):
            raise LockError(_ORDER)

    def _open(self, rank):
        name = _NAMES[rank]
        try:
            try:
                info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None and not _safe(info):
                raise LockError(_UNSAFE)
            flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
            created = False
            try:
                try:
                    fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.fd)
                    created = True
                except FileExistsError:
                    fd = os.open(name, flags, 0o600, dir_fd=self.fd)
            except PermissionError:
                # Recover a prior/overlapping creator's restrictive-umask file.
                # Identity stability is the declared envelope, including here:
                # repair owner permissions in pinned D, never replace the file.
                info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if not _safe(info):
                    raise
                if info.st_mode & 0o600 != 0o600:
                    os.chmod(name, 0o600, dir_fd=self.fd)
                fd = os.open(name, flags, 0o600, dir_fd=self.fd)
            try:
                info = os.fstat(fd)
                if not _safe(info):
                    raise LockError(_UNSAFE)
                if created:
                    os.fchmod(fd, 0o600)
                elif info.st_mode & 0o600 != 0o600:
                    os.fchmod(fd, 0o600)
                return fd
            except BaseException:
                os.close(fd)
                raise
        except OSError as exc:
            raise LockError(_UNSAFE) from exc

    @contextmanager
    def _track(self, rank, shared, fd):
        held = _held()
        held.append((self.key, rank, shared, fd))
        try:
            yield fd
        finally:
            held.pop()

    @contextmanager
    def _exclusive(self, rank):
        prior = self._prior(rank)
        if prior is None:
            self._order(rank)
        with self.entry.guards[rank]:
            fd = prior[3] if prior is not None else self._open(rank)
            try:
                if prior is None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                with self._track(rank, False, fd):
                    yield
            finally:
                if prior is None:
                    os.close(fd)

    def start(self):
        """START exclusive, reentrant in the owning thread."""
        return self._exclusive(1)

    def state(self):
        """STATE exclusive, reentrant in the owning thread."""
        return self._exclusive(2)

    @contextmanager
    def drain(self, *, shared=True, inherited_fd=None):
        """DRAIN reference; inherited_fd must already hold shared protection.

        Never flock or convert an imported/duplicated description. The yielded
        fd is borrowed until context exit; use pass_fds for explicit inheritance.
        """
        prior = self._prior(0)
        if not shared and (inherited_fd is not None or (prior and prior[2])):
            raise LockError(_UPGRADE)
        if prior is not None and prior[2] != shared:
            raise LockError("D5 lock refused: drain mode change")
        if inherited_fd is not None:
            try:
                info = os.fstat(inherited_fd)
                expected = os.stat(_NAMES[0], dir_fd=self.fd, follow_symlinks=False)
                if not _safe(info) or _key(info) != _key(expected):
                    raise LockError(_REFERENCE)
            except OSError as exc:
                raise LockError(_REFERENCE) from exc
        source = prior[3] if prior is not None else inherited_fd
        if source is None:
            self._order(0)
        fd = self._open(0) if source is None else os.dup(source)
        try:
            if source is None:
                fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            with self._track(0, shared, fd):
                yield fd
        finally:
            os.close(fd)


@contextmanager
def pin(directory):
    """Pin an already safely resolved D; no registry or authority validation."""
    _facilities()
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise LockError(_UNSAFE) from exc
    record = None
    try:
        record = _Record(fd)
        yield record
    finally:
        if record is not None:
            record.fd = None
        os.close(fd)
