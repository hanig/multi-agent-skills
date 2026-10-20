"""Inert D5 store. Keep authority.lock permanent; reread after filesystem errors."""
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from decimal import Decimal
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

def _scalar(value):
 return (str(Decimal(value)) if type(value) is int else
   json.dumps(value, ensure_ascii=False, allow_nan=False))

def _tokens(value, field):
 """Iterative JSON with cycle/type validation."""
 pending, active = [(0, value, field)], set()
 while pending:
  kind, value, field = pending.pop()
  if kind == 1:
   yield value
   continue
  if kind == 2:
   active.remove(id(value))
   yield "}" if type(value) is dict else "]"
   continue
  if value is None or type(value) in (str, int, bool, float):
   _require(type(value) is not float or math.isfinite(value),
     field, "expected finite JSON number")
   yield _scalar(value)
   continue
  _require(type(value) in (dict, list), field, "expected JSON value")
  _require(id(value) not in active, field, "cyclic JSON value")
  active.add(id(value))
  if type(value) is dict:
   _require(all(type(k) is str for k in value), field, "expected string key")
   children = [(_scalar(k) + ":", v, field + "." + k)
      for k, v in sorted(value.items())]
   yield "{"
  else:
   children = [("", v, "{}[{}]".format(field, i)) for i, v in enumerate(value)]
   yield "["
  pending.append((2, value, None))
  for i in range(len(children) - 1, -1, -1):
   prefix, child, path = children[i]
   if i < len(children) - 1:
    pending.append((1, ",", None))
   pending.append((0, child, path))
   pending.append((1, prefix, None))

def _json_value(value, field):
 for _ in _tokens(value, field):
  pass

def validate(record):
 _keys(record, _FIELDS, "record")
 for field in ("schema_version", "issued_at", "confirmed_at", "expires_at",
    "session_cap"):
  _integer(record[field], field)
 for field in ("schema_version", "session_cap"):
  _require(record[field] == 1, field, "must equal 1")
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
  _require(all(type(v) is str for v in values), field, "expected strings")
  _require(len(values) == len(set(values)), field, "duplicate element")
 _keys(record["confirmation"], {"owner_response", "displayed_bounds"},
  "confirmation")
 for key, value in record["confirmation"].items():
  _require(type(value) is str, "confirmation." + key, "expected string")
 custody = record["custody"]
 _require(custody is None or type(custody) is dict, "custody", "expected object or null")
 _json_value(custody, "custody")
 invalidation = record["invalidation"]
 if invalidation is not None:
  _keys(invalidation, {"at", "kind", "reason"}, "invalidation")
  _integer(invalidation["at"], "invalidation.at")
  _require(type(invalidation["kind"]) is str and
    invalidation["kind"] in ("revoked", "mandate_changed"),
    "invalidation.kind", "expected revoked or mandate_changed")
  _reason(invalidation["reason"])

def _reason(reason):
 _require(type(reason) is str and bool(reason), "invalidation.reason",
   "expected non-empty string")

def mandate_digest(mandate_bytes):
 _require(type(mandate_bytes) is bytes, "mandate_bytes", "expected bytes")
 return hashlib.sha256(mandate_bytes).hexdigest()

def _encode(value):
 return ("".join(_tokens(value, "record")) + "\n").encode("utf-8", "backslashreplace")

def _decode(raw):
 # Scalars are literal UTF-8; escaped surrogate code units stay separate.
 text = raw.decode(json.detect_encoding(raw))
 text = re.sub(r"\\(?:u([dD][89a-fA-F][0-9a-fA-F]{2})|.)",
    lambda m: chr(int(m[1], 16)) if m[1] else m[0], text)
 scalar = json.JSONDecoder(parse_int=lambda s: int(Decimal(s)))
 frames, position = [([], [])], 0
 while position < len(text):
  char = text[position]
  parts, values = frames[-1]
  if char in " \t\r\n":
   position += 1
  elif char in "[{":
   frames.append(([char], []))
   position += 1
  elif char in "]}":
   _require(len(frames) > 1, "record", "unmatched JSON delimiter")
   parts, values = frames.pop()
   # Validate each container's grammar with stdlib JSON. Its values
   # are flat null placeholders; children have already been decoded.
   box = json.loads("".join(parts) + char, object_pairs_hook=_pairs)
   _require(len(box) == len(values), "record", "invalid JSON container")
   if type(box) is dict:
    for key, value in zip(box, values):
     box[key] = value
   else:
    box[:] = values
   frames[-1][0].append("null")
   frames[-1][1].append(box)
   position += 1
  elif char in ",:":
   parts.append(char)
   position += 1
  else:
   value, end = scalar.raw_decode(text, position)
   following = end
   while following < len(text) and text[following] in " \t\r\n":
    following += 1
   if type(value) is str and following < len(text) and text[following] == ":":
    parts.append(text[position:end])
   else:
    parts.append("null")
    values.append(value)
   position = end
 _require(len(frames) == 1, "record", "unterminated JSON container")
 parts, values = frames[0]
 root = json.loads("[" + "".join(parts) + "]")
 _require(len(root) == len(values) == 1, "record", "expected one JSON value")
 return values[0]

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
 except (ValueError, UnicodeError) as exc:
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
 _integer(now, "now")
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
 _integer(now, "now")
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
  _require(_encode(new["custody"]) == _encode(old["custody"]),
    "custody", "replacement cannot move custody")
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
