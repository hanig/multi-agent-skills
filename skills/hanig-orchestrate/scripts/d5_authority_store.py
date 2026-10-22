"""Inert D5 record I/O. Keep the D5 lock files permanent.

Supported envelope and accepted limits
-------------------------------------
Exclusive drain installation is completed in U3h; no claim of completed
takeover protection in this unit. The named store calls share U3a's local,
stable-identity, same-record nesting envelope. No production activation.

Does not serialize arbitrary mandate-file editors.
Does not claim all repository writers use these entry points.

No authority decision is made on unknown filesystem state: a lookup or
durability failure refuses. At or after os.replace, durability is unconfirmed
on failure: the complete old or new record may be visible (create may leave
no record). Never roll back. A visible mandate_changed invalidation is fail-safe.

A visible replacement has already passed
replacement_allowed. It carries either fresh owner consent, or re-used consent
that extends neither expiry nor scope.
"""
import os
import stat
import tempfile
from contextlib import contextmanager, ExitStack
from pathlib import Path

import d5_locks as locks
from d5_locks import fcntl
from d5_authority import (AuthorityError, validate, encode, decode, evaluate,
                          revoked, claimed, relinquished, replacement_allowed)
from d5_host import ProcessIdentity

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
def _preserving(guard):
    """Release a lock context without replacing the store's primary error."""
    entry_frame = guard.gen.gi_frame
    try:
        value = guard.__enter__()
    except BaseException as secondary:
        primary = secondary.__context__
        trace = primary.__traceback__ if primary is not None else None
        # U3a's generator may fail in its entry cleanup. Restore an exception
        # that already reached THIS invocation, not an unrelated caller's
        # exception or an intentional `raise ... from ...` wrapper.
        while trace is not None and not secondary.__suppress_context__:
            if trace.tb_frame is entry_frame:
                primary.cleanup_errors = getattr(primary, "cleanup_errors", ()) + (secondary,)
                raise primary
            trace = trace.tb_next
        raise
    with _cleanup(guard.__exit__, None, None, None):
        yield value


@contextmanager
def _locked(directory, *, start=False, drain=False, initialize=False):
    directory = Path(directory)
    try:
        with ExitStack() as stack:
            record = stack.enter_context(_preserving(locks.pin(directory)))
            if drain:
                stack.enter_context(_preserving(record.drain(shared=False)))
            if start:
                stack.enter_context(_preserving(record.start()))
            stack.enter_context(_preserving(record.state()))
            if initialize:
                # Create the permanent drain inode before initial publication.
                # A later refused replacement must not change the directory.
                # This uses U3a's safe opener without acquiring a drain lock.
                os.close(record._open(0))
            yield directory
    except (OSError, locks.LockError) as exc:
        # The lock manager wraps open failures. Keep the store's public
        # filesystem refusal and original OSError cause for these failures.
        if isinstance(exc, locks.LockError):
            if not isinstance(exc.__cause__, OSError):
                raise AuthorityError(str(exc)) from exc
            exc = exc.__cause__
        raise AuthorityError("record: filesystem operation failed; "
                             "durability is unconfirmed: {}".format(exc)) from exc


@contextmanager
def _file(fd, mode):
    with _cleanup(os.close, fd):
        handle = os.fdopen(fd, mode, closefd=False)
        with _cleanup(handle.close):
            yield handle


def read_bytes(path):
    """Strict regular-file read, also shared by the D5 registry."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with _file(fd, "rb") as handle:
        _require(stat.S_ISREG(os.fstat(fd).st_mode), "record", "expected regular file")
        raw = handle.read()
    return raw


def _load(path):
    raw = read_bytes(path)
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


# Public primitives for the registry; the record-store protocol is unchanged.
cleanup = _cleanup
present = _present
sync_directory = _sync_directory
write_bytes = _write


def create(directory, record):
    validate(record)
    raw = encode(record)
    with _locked(directory, start=True, initialize=True) as directory:
        _require(not _present(directory / RECORD_NAME, "record"),
                 "record", "already exists")
        _unused_id(directory, record)
        _write(directory / RECORD_NAME, raw)
    return decode(raw)


def read(directory):
    with _locked(directory) as directory:
        return _load(directory / RECORD_NAME)[0]


def check(directory, now, mandate_bytes):
    with _locked(directory, start=True) as directory:
        record, _ = _load(directory / RECORD_NAME)
        state, invalidation = evaluate(record, now, mandate_bytes)
        if state == "mandate_changed":
            record["invalidation"] = invalidation
            _write(directory / RECORD_NAME, encode(record))
        return state


def revoke(directory, now, reason):
    with _locked(directory, start=True) as directory:
        record, raw = _load(directory / RECORD_NAME)
        updated = revoked(record, now, reason)
        _write(directory / RECORD_NAME, encode(updated) if updated != record else raw)
        return updated


def observe_liveness(anchor, liveness):
    """Inspection failures and unexpected results never establish death."""
    try:
        result = liveness(anchor)
    except Exception:
        return "unknown"
    return result if type(result) is str and result in ("alive", "dead", "unknown") else "unknown"


def claim(directory, now, mandate_bytes, session_id, anchor, liveness):
    """Evaluate and claim under the permanent record lock; persist invalidation."""
    with _locked(directory, start=True, drain=True) as directory:
        record, _ = _load(directory / RECORD_NAME)
        state, invalidation = evaluate(record, now, mandate_bytes)
        if state == "mandate_changed":
            record["invalidation"] = invalidation
            _write(directory / RECORD_NAME, encode(record))
        _require(state == "valid", "record", state)
        custody = record["custody"]
        holder = "unknown"
        if custody is not None:
            identity = ProcessIdentity(custody["anchor_pid"], custody["anchor_start_us"])
            if (session_id, anchor) != (custody["session_id"], identity):
                holder = observe_liveness(identity, liveness)
        updated = claimed(record, session_id, anchor.pid, anchor.start_us, now, holder)
        if updated != record:
            _write(directory / RECORD_NAME, encode(updated))
        return updated


def relinquish(directory, now, session_id, anchor):
    """Retain the exact holder and its first relinquishment under the lock."""
    with _locked(directory, start=True) as directory:
        record, _ = _load(directory / RECORD_NAME)
        updated = relinquished(record, session_id, anchor.pid, anchor.start_us, now)
        if updated != record:
            _write(directory / RECORD_NAME, encode(updated))
        return updated


def replace(directory, new):
    raw = encode(new)
    new = decode(raw)
    with _locked(directory, start=True, drain=True) as directory:
        old, old_raw = _load(directory / RECORD_NAME)
        if new["custody"] is None:
            new["custody"] = old["custody"]
        replacement_allowed(old, new)
        raw = encode(new)
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
            try:
                archived, archived_raw = _load(predecessor)
            except AuthorityError as exc:
                raise AuthorityError("record: superseded: invalid predecessor archive") from exc
            if archived_raw != old_raw:
                # Only add the current grant's first terminal invalidation.
                monotone = archived["invalidation"] is None and old["invalidation"] is not None
                archived["invalidation"] = old["invalidation"]
                _require(monotone and archived == old, "superseded",
                         "predecessor archive differs beyond first invalidation")
        _write(predecessor, old_raw)
        _write(directory / RECORD_NAME, raw)
    return new
