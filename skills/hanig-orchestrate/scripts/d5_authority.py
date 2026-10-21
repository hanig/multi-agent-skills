"""Pure D5 grant records; persistence and enforcement belong to callers."""
import copy
import hashlib
import json
import re

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

def encode(record):
    """Validate and encode exact scalar strings as UTF-8 JSON bytes."""
    validate(record)
    return (json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def decode(data):
    """Refuse malformed bytes, duplicate keys and schema-invalid records."""
    _require(type(data) is bytes, "record", "expected bytes")
    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (ValueError, RecursionError) as exc:
        raise AuthorityError("record: invalid JSON: {}".format(exc)) from exc
    validate(record)
    return record


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, key, "duplicate JSON field")
        result[key] = value
    return result


def evaluate(record, now, mandate_bytes):
    """Return state and the mandate-change invalidation a caller must persist.

    Precedence: invalidated, mandate_changed, expired, valid. This function
    does not modify the record; callers must retain the returned invalidation
    before relying on a later evaluation of the same grant.
    """
    validate(record)
    _timestamp(now, "now")
    digest = mandate_digest(mandate_bytes)
    if record["invalidation"] is not None:
        return "invalidated", None
    if record["mandate_sha256"] != digest:
        return "mandate_changed", {"at": now, "kind": "mandate_changed",
                                   "reason": "mandate byte digest changed"}
    return ("expired" if now >= record["expires_at"] else "valid"), None


def revoked(record, now, reason):
    """Return an independent record, preserving the first terminal invalidation."""
    validate(record)
    _timestamp(now, "now")
    _reason(reason)
    result = copy.deepcopy(record)
    if result["invalidation"] is None:
        result["invalidation"] = {"at": now, "kind": "revoked", "reason": reason}
    return result


def replacement_allowed(old, new):
    """Decide consent reuse or fresh confirmation; neither record is modified."""
    validate(old)
    validate(new)
    _require(new["grant_id"] != old["grant_id"], "grant_id", "must be fresh")
    if new["confirmation"] == old["confirmation"]:
        _require(old["invalidation"] is None, "confirmation", "invalidated consent")
        _require(new["expires_at"] <= old["expires_at"],
                 "expires_at", "extension requires fresh confirmation")
        for key in _SCOPES:
            _require(set(new["scope"][key]) <= set(old["scope"][key]),
                     "scope." + key, "expansion requires fresh confirmation")
    else:
        _require(new["invalidation"] is None, "invalidation", "new grant must be clear")
    return True
