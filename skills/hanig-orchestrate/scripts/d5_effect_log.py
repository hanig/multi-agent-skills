"""Inert U3c ledger. No production callers or activation change.

Supported envelope and accepted limits
-------------------------------------
- Local durable accounting.
- Submitted-state transitions are an internal interface for the later submission helper.
- No arbitrary user-supplied output path or accounting callback.

Non-claims
----------
- Does not decide whether a transport actually submitted.
- Does not perform remote reconciliation or guarantee exactly-once execution.
- Does not protect against hostile same-process Python.

The helper owns admission/DRAIN. Keep secrets out of argv, environment and logs.
Hostile same-UID filesystem writers are excluded. Retry unconfirmed writes with
the same handle/result. Outcomes require status (succeeded/failed) and detail.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
import secrets
import stat

import d5_locks as locks
import d5_registry as registry


class EffectError(ValueError):
    pass


INVALID = "D5 completion refused: invalid receipt"
NOT_SUBMITTED = "D5 completion refused: receipt is not submitted"
CONFLICT = "D5 completion refused: conflicting outcome"
DURABILITY = "D5 accounting refused: durability is unconfirmed"
DIRECTORY = "authority.effects"
_STATES = {"attempting", "submitted", "not_submitted", "unknown", "completed"}


def _require(condition, message=INVALID):
    if not condition:
        raise EffectError(message)


def _active():
    _require(registry.d5_active(), "D5 inactive")


def _hex(value, width):
    return type(value) is str and re.fullmatch("[0-9a-f]{%d}" % width, value) is not None


def _text(value):
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


def _keys(value, expected):
    _require(type(value) is dict and value.keys() == expected)


def _integer(value):
    return type(value) is int and 0 < value < 2**63


def _binding(value):
    _keys(value, {"record_id", "registration_id", "record_key", "effects_key",
                  "custody", "kind", "scope", "correlation"})
    for key in ("record_id", "registration_id"):
        _require(_hex(value[key], 32))
    for key in ("record_key", "effects_key"):
        pair = value[key]
        _require(type(pair) is list and len(pair) == 2 and all(type(n) is int and n >= 0 for n in pair))
    custody = value["custody"]
    _keys(custody, {"generation", "session_id", "anchor_pid", "anchor_start_us"})
    _require(_text(custody["session_id"]) and bool(custody["session_id"]))
    for key in ("generation", "anchor_pid", "anchor_start_us"):
        _require(_integer(custody[key]))
    _require(custody["anchor_pid"] < 2**31)
    _require(_text(value["kind"]) and bool(value["kind"]))
    _keys(value["scope"], {"plans", "repositories", "actions", "trackers"})
    for items in value["scope"].values():
        _require(type(items) is list and all(_text(s) for s in items))
        _require(len(items) == len(set(items)))
    correlation = value["correlation"]
    _require(type(correlation) is dict and bool(correlation.get("request_id")) and
             all(_text(k) and _text(v) for k, v in correlation.items()))


def _outcome(value):
    _keys(value, {"status", "detail"})
    _require(type(value["status"]) is str and value["status"] in ("succeeded", "failed") and _text(value["detail"]))


def _encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise EffectError(INVALID) from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _decode(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (ValueError, RecursionError) as exc:
        raise EffectError(INVALID) from exc


def _key(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def _safe(fd, directory=False):
    info = os.fstat(fd)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    _require(kind(info.st_mode) and info.st_uid == os.geteuid() and
             not info.st_mode & 0o022 and (directory or info.st_nlink == 1))


@contextmanager
def _access(directory, create=False):
    try:
        _require(stat.S_ISDIR(os.lstat(directory).st_mode))
        with locks.pin(directory) as record:
            _safe(record.fd, directory=True)
            with record.state():
                if create:
                    try:
                        os.mkdir(DIRECTORY, 0o700, dir_fd=record.fd)
                    except FileExistsError:
                        pass
                    else:
                        os.chmod(DIRECTORY, 0o700, dir_fd=record.fd)
                fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY |
                             os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=record.fd)
                try:
                    _safe(fd, directory=True)
                    if create:
                        _sync(record.fd)
                    yield record, fd
                finally:
                    os.close(fd)
    except OSError as exc:
        raise EffectError(INVALID) from exc


def _sync(fd):
    try:
        os.fsync(fd)
    except OSError as exc:
        raise EffectError(DURABILITY) from exc


def _write(fd, name, entry, create=False):
    temporary = name if create else ".effect-" + secrets.token_hex(16)
    opened = False
    try:
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                      os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        opened = True
        with os.fdopen(out, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(_encode(entry) + b"\n")
            handle.flush()
            _sync(handle.fileno())
        if not create:
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        _sync(fd)
    except OSError as exc:
        raise EffectError(DURABILITY) from exc
    finally:
        if opened and not create:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass


def _load(fd, name, record):
    out = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW |
                  os.O_CLOEXEC, dir_fd=fd)
    with os.fdopen(out, "rb") as handle:
        _safe(handle.fileno())
        entry = _decode(handle.read())
    _keys(entry, {"schema_version", "effect_id", "binding", "receipt_verifier", "state", "outcome"})
    _require(type(entry["schema_version"]) is int and entry["schema_version"] == 1)
    _require(_hex(entry["effect_id"], 32) and name == entry["effect_id"] + ".json")
    _binding(entry["binding"])
    _require(entry["binding"]["record_key"] == _key(record.fd) and entry["binding"]["effects_key"] == _key(fd))
    _require(_hex(entry["receipt_verifier"], 64))
    _require(type(entry["state"]) is str and entry["state"] in _STATES)
    if entry["state"] == "completed":
        _outcome(entry["outcome"])
    else:
        _require(entry["outcome"] is None)
    return entry


@dataclass(frozen=True)
class Attempt:
    """Intent capability."""
    effect_id: str
    _directory: str = field(repr=False)
    _binding: bytes = field(repr=False)
    _secret: bytes = field(repr=False)


@dataclass(frozen=True)
class Receipt(Attempt):
    """Submitted capability."""


def _verifier(effect_id, binding, secret):
    return hmac.new(secret, effect_id.encode("ascii") + b"\0" + binding,
                    hashlib.sha256).hexdigest()


def prepare(checkout, plan_id, state_dir, *, record_id, custody, kind, scope, correlation):
    _active()
    registration = registry.lookup(checkout, plan_id, state_dir)
    _require(registration is not None)
    directory = registration["record_dir"]
    with _access(directory, create=True) as (record, fd):
        binding = dict(record_id=record_id, registration_id=registration["registration_id"],
                       record_key=_key(record.fd), effects_key=_key(fd), custody=custody,
                       kind=kind, scope=scope, correlation=correlation)
        _binding(binding)
        raw = _encode(binding)
        effect_id, secret = secrets.token_hex(16), secrets.token_bytes(32)
        entry = dict(schema_version=1, effect_id=effect_id, binding=_decode(raw),
                     receipt_verifier=_verifier(effect_id, raw, secret),
                     state="attempting", outcome=None)
        _write(fd, effect_id + ".json", entry, create=True)
    return Attempt(effect_id, directory, raw, secret)


@contextmanager
def _checked(handle):
    _require(type(handle) in (Attempt, Receipt) and _hex(handle.effect_id, 32) and
             type(handle._directory) is str and type(handle._binding) is bytes and
             type(handle._secret) is bytes and len(handle._secret) == 32)
    with _access(handle._directory) as (record, fd):
        name = handle.effect_id + ".json"
        entry = _load(fd, name, record)
        _require(_encode(entry["binding"]) == handle._binding)
        _require(hmac.compare_digest(entry["receipt_verifier"], _verifier(handle.effect_id, handle._binding, handle._secret)))
        yield fd, name, entry


def _classify(attempt, state):
    """Internal; exceptions mean unknown. Caller holds DRAIN."""
    _active()
    _require(type(attempt) is Attempt)
    if isinstance(state, BaseException):
        state = "unknown"
    _require(type(state) is str and state in ("submitted", "not_submitted", "unknown"))
    with _checked(attempt) as (fd, name, entry):
        prior = "submitted" if entry["state"] == "completed" else entry["state"]
        transition = prior == "attempting" or (entry["state"] == "submitted" and
                                                state == "unknown")
        _require(transition or prior == state, CONFLICT)
        if transition:
            entry["state"] = state
            _write(fd, name, entry)
        else:
            _confirm(fd, name)
    if state == "submitted":
        return Receipt(attempt.effect_id, attempt._directory, attempt._binding, attempt._secret)
    return None


def _confirm(fd, name):
    out = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        _sync(out)
        _sync(fd)
    finally:
        os.close(out)


def complete(receipt, outcome):
    _active()
    _require(type(receipt) is not Attempt, NOT_SUBMITTED)
    _require(type(receipt) is Receipt)
    _outcome(outcome)
    outcome = _decode(_encode(outcome))
    with _checked(receipt) as (fd, name, entry):
        _require(entry["state"] in ("submitted", "completed"), NOT_SUBMITTED)
        if entry["state"] == "completed":
            _require(entry["outcome"] == outcome, CONFLICT)
            _confirm(fd, name)
        else:
            entry.update(state="completed", outcome=outcome)
            _write(fd, name, entry)
    return outcome


def unresolved(checkout, plan_id, state_dir):
    """Unresolved entries; attempting/unknown require reconciliation, not liveness.

    Submitted entries are included; terminal entries remain on disk. Unreadable
    entries produce file/error diagnostics requiring reconciliation; bytes stay put.
    """
    _active()
    registration = registry.lookup(checkout, plan_id, state_dir)
    _require(registration is not None)
    result = []
    with _access(registration["record_dir"], create=True) as (record, fd):
        for name in sorted(os.listdir(fd)):
            if not name.endswith(".json"):
                continue
            try:
                entry = _load(fd, name, record)
            except (EffectError, OSError):
                result.append(dict(file=name, error=INVALID, reconciliation_required=True))
                continue
            if entry["state"] in ("attempting", "unknown", "submitted"):
                entry.pop("receipt_verifier")
                entry["reconciliation_required"] = entry["state"] != "submitted"
                result.append(entry)
    return result
