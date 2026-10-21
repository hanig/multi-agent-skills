"""Inert D5 record I/O. Keep authority.lock permanent.

No authority decision is made on unknown filesystem state: a lookup or
durability failure refuses. At or after os.replace, durability is unconfirmed
on failure: the complete old or new record may be visible (create may leave
no record). Never roll back. A visible mandate_changed invalidation is fail-safe.

Replacement belongs to U1a-2b. A visible replacement has already passed
replacement_allowed. It carries either fresh owner consent, or re-used consent
that extends neither expiry nor scope.
"""
import fcntl
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from d5_authority import AuthorityError, validate, encode, decode, evaluate

RECORD_NAME = "authority.json"
LOCK_NAME = "authority.lock"


def _require(condition, field, description):
    if not condition:
        raise AuthorityError("{}: {}".format(field, description))


def _present(path, field):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AuthorityError("record: {}: lookup failed; durability is unconfirmed".format(field)) from exc


@contextmanager
def _cleanup(action, *args):
    """Keep the primary error, attaching secondary cleanup errors for diagnosis."""
    try:
        yield
    except BaseException as primary:
        try:
            action(*args)
        except BaseException as secondary:
            primary.cleanup_errors = getattr(primary, "cleanup_errors", ()) + (secondary,)
        raise
    else:
        action(*args)


@contextmanager
def _locked(directory):
    directory = Path(directory)
    try:
        fd = os.open(str(directory / LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
        with _cleanup(os.close, fd):
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory
    except OSError as exc:
        raise AuthorityError("record: filesystem operation failed; "
                             "durability is unconfirmed: {}".format(exc)) from exc


@contextmanager
def _file(fd, mode):
    with _cleanup(os.close, fd):
        handle = os.fdopen(fd, mode, closefd=False)
        with _cleanup(handle.close):
            yield handle


def _load(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with _file(fd, "rb") as handle:
        _require(stat.S_ISREG(os.fstat(fd).st_mode), "record", "expected regular file")
        raw = handle.read()
    return decode(raw), raw


def _sync_directory(directory):
    fd = os.open(str(directory), os.O_RDONLY)
    with _cleanup(os.close, fd):
        os.fsync(fd)


def _remove_temporary(name):
    if _present(name, "temporary"):
        os.unlink(name)


def _write(path, raw):
    fd, name = tempfile.mkstemp(prefix=".authority-", dir=str(path.parent))
    with _cleanup(_remove_temporary, name):
        with _file(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, str(path))
        _sync_directory(path.parent)


def _unused_id(directory, record):
    _require(not _present(directory / "superseded" /
                          (record["grant_id"] + ".json"), "grant_id"),
             "grant_id", "already superseded; use a fresh grant ID")


def create(directory, record):
    validate(record)
    raw = encode(record)
    with _locked(directory) as directory:
        _require(not _present(directory / RECORD_NAME, "record"),
                 "record", "already exists")
        _unused_id(directory, record)
        _write(directory / RECORD_NAME, raw)
    return decode(raw)


def read(directory):
    with _locked(directory) as directory:
        return _load(directory / RECORD_NAME)[0]


def check(directory, now, mandate_bytes):
    with _locked(directory) as directory:
        record, _ = _load(directory / RECORD_NAME)
        state, invalidation = evaluate(record, now, mandate_bytes)
        if state == "mandate_changed":
            record["invalidation"] = invalidation
            _write(directory / RECORD_NAME, encode(record))
        return state
