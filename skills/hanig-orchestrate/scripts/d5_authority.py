"""Pure D5 grant records; persistence and enforcement belong to callers."""
import copy
import hashlib
import json
import re

_FIELDS = {"schema_version", "grant_id", "issued_at", "confirmed_at",
           "expires_at", "session_cap", "mandate_sha256", "scope",
           "confirmation", "custody", "invalidation"}
_SCOPES = {"plans", "repositories", "actions", "trackers"}
_CUSTODY_FIELDS = {"session_id", "anchor_pid", "anchor_start_us", "generation",
                   "claimed_at", "relinquished_at"}

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

def _claimant(session_id, anchor_pid, anchor_start_us):
    _string(session_id, "custody.session_id")
    _require(bool(session_id), "custody.session_id", "expected non-empty string")
    _integer(anchor_pid, "custody.anchor_pid")
    _require(0 < anchor_pid < 2**31, "custody.anchor_pid",
             "must satisfy 0 < pid < 2**31")
    _timestamp(anchor_start_us, "custody.anchor_start_us")

def _custody(value):
    _keys(value, _CUSTODY_FIELDS, "custody")
    _claimant(value["session_id"], value["anchor_pid"], value["anchor_start_us"])
    _timestamp(value["generation"], "custody.generation")
    _timestamp(value["claimed_at"], "custody.claimed_at")
    if value["relinquished_at"] is not None:
        _timestamp(value["relinquished_at"], "custody.relinquished_at")
        _require(value["relinquished_at"] >= value["claimed_at"],
                 "custody.relinquished_at", "must not precede claimed_at")

def _same_holder(custody, session_id, anchor_pid, anchor_start_us):
    return (custody["session_id"] == session_id and
            custody["anchor_pid"] == anchor_pid and
            custody["anchor_start_us"] == anchor_start_us)

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
    if record["custody"] is not None:
        _custody(record["custody"])
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


def claimed(record, session_id, anchor_pid, anchor_start_us, now, holder):
    """Return independent custody; only observed death permits a successor.

    Liveness is supplied by the caller, which must check the mandate separately.
    A matching session AND anchor is compaction, never a new generation.
    """
    validate(record)
    _timestamp(now, "now")
    _claimant(session_id, anchor_pid, anchor_start_us)
    _require(type(holder) is str and holder in ("alive", "dead", "unknown"),
             "holder", "expected alive, dead or unknown")
    _require(record["invalidation"] is None, "invalidation", "invalidated")
    _require(now < record["expires_at"], "expires_at", "expired")
    custody = record["custody"]
    if custody is not None:
        if _same_holder(custody, session_id, anchor_pid, anchor_start_us):
            _require(custody["relinquished_at"] is None, "custody", "custody relinquished")
            return copy.deepcopy(record)
        _require(holder == "dead", "custody", "successor requires dead holder")
    result = copy.deepcopy(record)
    result["custody"] = {
        "session_id": session_id, "anchor_pid": anchor_pid,
        "anchor_start_us": anchor_start_us,
        "generation": 1 if custody is None else custody["generation"] + 1,
        "claimed_at": now, "relinquished_at": None,
    }
    _custody(result["custody"])
    return result


def relinquished(record, session_id, anchor_pid, anchor_start_us, now):
    """Return an independent record retaining the holder's first relinquishment."""
    validate(record)
    _timestamp(now, "now")
    _claimant(session_id, anchor_pid, anchor_start_us)
    custody = record["custody"]
    _require(custody is not None and
             _same_holder(custody, session_id, anchor_pid, anchor_start_us),
             "custody", "only the exact holder may relinquish")
    _require(now >= custody["claimed_at"], "now", "must not precede claimed_at")
    result = copy.deepcopy(record)
    if result["custody"]["relinquished_at"] is None:
        result["custody"]["relinquished_at"] = now
    return result


def replacement_allowed(old, new):
    """Decide consent reuse or fresh confirmation; neither record is modified."""
    validate(old)
    validate(new)
    _require(new["custody"] == old["custody"], "custody", "must carry over unchanged")
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
