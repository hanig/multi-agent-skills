"""Inert D5 storage. Keep authority.lock permanent.

No authority decision is made on unknown filesystem state: a lookup or
durability failure refuses. At or after replace, durability is unconfirmed
on failure: the complete old or new record may be visible. Never roll back.
"""
import fcntl
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from d5_authority import (AuthorityError, validate, encode, decode, evaluate,
                          revoked, replacement_allowed)

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
def _locked(directory):
    directory = Path(directory)
    try:
        fd = os.open(str(directory / LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory
        finally:
            os.close(fd)
    except OSError as exc:
        raise AuthorityError("record: filesystem operation failed; "
                             "durability is unconfirmed: {}".format(exc)) from exc


def _load(path):
    raw = path.read_bytes()
    return decode(raw), raw


def _sync_directory(directory):
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, raw):
    fd, name = tempfile.mkstemp(prefix=".authority-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, str(path))
        _sync_directory(path.parent)
    finally:
        if _present(name, "temporary"):
            os.unlink(name)


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


def revoke(directory, now, reason):
    with _locked(directory) as directory:
        record, _ = _load(directory / RECORD_NAME)
        updated = revoked(record, now, reason)
        if updated != record:
            _write(directory / RECORD_NAME, encode(updated))
        return updated


def replace(directory, new):
    raw = encode(new)
    new = decode(raw)
    with _locked(directory) as directory:
        old, old_raw = _load(directory / RECORD_NAME)
        replacement_allowed(old, new)
        _unused_id(directory, new)
        archive = directory / "superseded"
        try:
            os.mkdir(archive, 0o700)
        except FileExistsError:
            pass
        info = _present(archive, "superseded")
        _require(info and stat.S_ISDIR(info.st_mode),
                 "superseded", "expected directory")
        _sync_directory(directory)
        predecessor = archive / (old["grant_id"] + ".json")
        if _present(predecessor, "superseded"):
            _require(predecessor.read_bytes() == old_raw, "superseded",
                     "predecessor archive has different bytes")
        _write(predecessor, old_raw)
        _write(directory / RECORD_NAME, raw)
    return new
