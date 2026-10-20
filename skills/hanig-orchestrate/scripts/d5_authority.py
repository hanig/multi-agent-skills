"""Inert D5 store. Keep authority.lock permanent; reread after filesystem errors."""
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

RECORD_NAME = "authority.json"
LOCK_NAME = "authority.lock"
_FIELDS = {"schema_version", "grant_id", "issued_at", "confirmed_at",
           "expires_at", "session_cap", "mandate_sha256", "scope",
           "confirmation", "custody", "invalidation"}
_SCOPES = {"plans", "repositories", "actions", "trackers"}

class AuthorityError(ValueError):
    """Named-field schema or transition refusal."""

def _require(condition, field, description):
    if not condition:
        raise AuthorityError("{}: {}".format(field, description))

def _keys(value, keys, field):
    _require(type(value) is dict, field, "expected object")
    _require(all(type(k) is str for k in value), field, "expected string keys")
    for key in keys - value.keys():
        raise AuthorityError("{}.{}: missing field".format(field, key))
    for key in value.keys() - keys:
        raise AuthorityError("{}.{}: unknown field".format(field, key))

def _integer(value, field):
    _require(type(value) is int, field, "expected integer, not bool")

def _timestamp(value, field):
    _integer(value, field)
    _require(0 < value < 2**63, field, "must satisfy 0 < timestamp < 2**63")

def _string(value, field):
    _require(type(value) is str, field, "expected string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuthorityError(field + ": expected Unicode scalar values") from exc

def validate(record):
    _keys(record, _FIELDS, "record")
    for field in ("schema_version", "session_cap"):
        _integer(record[field], field)
        _require(record[field] == 1, field, "must equal 1")
    for field in ("issued_at", "confirmed_at", "expires_at"):
        _timestamp(record[field], field)
    for field, width in (("grant_id", 32), ("mandate_sha256", 64)):
        value = record[field]
        _require(type(value) is str and
                re.fullmatch("[0-9a-f]{%d}" % width, value) is not None,
                field, "expected lowercase hex of length {}".format(width))
    _require(0 < record["expires_at"] - record["issued_at"] <= 86400,
            "expires_at", "must be after issued_at and within 86400 seconds")
    _keys(record["scope"], _SCOPES, "scope")
    for key, values in record["scope"].items():
        field = "scope." + key
        _require(type(values) is list, field, "expected list")
        for value in values:
            _string(value, field)
        _require(len(values) == len(set(values)), field, "duplicate element")
    _keys(record["confirmation"], {"owner_response", "displayed_bounds"},
          "confirmation")
    for key, value in record["confirmation"].items():
        _string(value, "confirmation." + key)
    _require(record["custody"] is None, "custody", "must be null in U1a")
    invalidation = record["invalidation"]
    if invalidation is not None:
        _keys(invalidation, {"at", "kind", "reason"}, "invalidation")
        _timestamp(invalidation["at"], "invalidation.at")
        _require(type(invalidation["kind"]) is str and
                invalidation["kind"] in ("revoked", "mandate_changed"),
                "invalidation.kind", "expected revoked or mandate_changed")
        _reason(invalidation["reason"])

def _reason(reason):
    _string(reason, "invalidation.reason")
    _require(bool(reason), "invalidation.reason", "expected non-empty string")

def mandate_digest(mandate_bytes):
    _require(type(mandate_bytes) is bytes, "mandate_bytes", "expected bytes")
    return hashlib.sha256(mandate_bytes).hexdigest()

def _encode(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")

def _decode(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)

def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, key, "duplicate JSON field")
        result[key] = value
    return result

def _load(path):
    raw = path.read_bytes()
    try:
        record = _decode(raw)
    except (ValueError, RecursionError) as exc:
        raise AuthorityError("record: invalid JSON: {}".format(exc)) from exc
    validate(record)
    return record, raw

@contextmanager
def _locked(directory):
    directory = Path(directory)
    fd = os.open(str(directory / LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(fd)

def _sync_directory(directory):
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def _write(path, raw):
    """Caller holds the directory lock."""
    fd, name = tempfile.mkstemp(prefix=".authority-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, str(path))
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def _unused_id(directory, record):
    _require(not os.path.lexists(directory / "superseded" /
                                (record["grant_id"] + ".json")),
            "grant_id", "already superseded; use a fresh grant ID")

def create(directory, record):
    """Create once, without recycling retired IDs."""
    validate(record)
    raw = _encode(record)
    with _locked(directory) as directory:
        _require(not os.path.lexists(directory / RECORD_NAME), "record", "already exists")
        _unused_id(directory, record)
        _write(directory / RECORD_NAME, raw)
    return _decode(raw)

def read(directory):
    """Read a complete validated record."""
    with _locked(directory) as directory:
        return _load(directory / RECORD_NAME)[0]

def check(directory, now, mandate_bytes):
    """Precedence: invalidated, durable mandate_changed, expired, valid."""
    _timestamp(now, "now")
    digest = mandate_digest(mandate_bytes)
    with _locked(directory) as directory:
        record, _ = _load(directory / RECORD_NAME)
        if record["invalidation"] is not None:
            return "invalidated"
        if record["mandate_sha256"] != digest:
            record["invalidation"] = {"at": now, "kind": "mandate_changed",
                                      "reason": "mandate byte digest changed"}
            _write(directory / RECORD_NAME, _encode(record))
            return "mandate_changed"
        return "expired" if now >= record["expires_at"] else "valid"

def revoke(directory, now, reason):
    """Preserve the first terminal invalidation."""
    _timestamp(now, "now")
    _reason(reason)
    with _locked(directory) as directory:
        record, _ = _load(directory / RECORD_NAME)
        if record["invalidation"] is None:
            record["invalidation"] = {"at": now, "kind": "revoked", "reason": reason}
            _write(directory / RECORD_NAME, _encode(record))
        return record

def replace(directory, new):
    """Archive verbatim, then replace without moving custody."""
    validate(new)
    raw = _encode(new)
    with _locked(directory) as directory:
        old, old_raw = _load(directory / RECORD_NAME)
        _require(new["grant_id"] != old["grant_id"], "grant_id", "must be fresh")
        _unused_id(directory, new)
        if new["confirmation"] == old["confirmation"]:
            _require(old["invalidation"] is None, "confirmation", "invalidated consent")
            _require(new["expires_at"] <= old["expires_at"],
                    "expires_at", "extension requires fresh confirmation")
            for key in _SCOPES:
                _require(set(new["scope"][key]) <= set(old["scope"][key]),
                        "scope." + key, "expansion requires fresh confirmation")
        else:
            _require(new["invalidation"] is None, "invalidation", "new grant must be clear")
        archive = directory / "superseded"
        archive.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(directory)
        predecessor = archive / (old["grant_id"] + ".json")
        if os.path.lexists(predecessor):
            _require(predecessor.read_bytes() == old_raw, "superseded",
                    "predecessor archive has different bytes")
        _write(predecessor, old_raw)
        _write(directory / RECORD_NAME, raw)
    return _decode(raw)
