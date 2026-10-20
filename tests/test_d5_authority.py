"""Real temporary grant stores: schema, terminal history and durable transitions."""
import copy
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_authority as A

MANDATE = b"grant\nbounds\n"

def grant(identifier="a" * 32):
 return {
  "schema_version": 1, "grant_id": identifier,
  "issued_at": 100, "confirmed_at": 100, "expires_at": 86500,
  "session_cap": 1, "mandate_sha256": hashlib.sha256(MANDATE).hexdigest(),
  "scope": {"plans": ["p"], "repositories": ["r"],
    "actions": ["read", "write"], "trackers": ["t"]},
  "confirmation": {"owner_response": "  yes\r\n\t\u00e9\x00 ",
      "displayed_bounds": "\tbound\nexact\r\n "},
  "custody": {"opaque": [1, True, None, {"label": "  untouched  "}]},
  "invalidation": None,
 }

class TestD5Authority(unittest.TestCase):
 eq = unittest.TestCase.assertEqual

 def setUp(self):
  self.temp = tempfile.TemporaryDirectory()
  self.addCleanup(self.temp.cleanup)
  self.d = Path(self.temp.name)
  self.path = self.d / A.RECORD_NAME

 def refuses(self, field, op, *args):
  with self.assertRaisesRegex(A.AuthorityError, field):
   op(*args)

 def bad_field(self, key, value):
  r = grant()
  r[key] = value
  self.refuses(key, A.create, self.d, r)

 def create(self):
  A.create(self.d, grant())

 def stored(self, expected):
  self.eq(A.read(self.d), expected)

 def successor(self, old=None, fresh=False):
  new = copy.deepcopy(old if old is not None else grant())
  new["grant_id"] = "b" * 32
  if fresh:
   new["confirmation"]["owner_response"] += " fresh"
   new["invalidation"] = None
  return new

 def test_exact_roundtrip(self):
  r = grant()
  self.eq(A.create(self.d, r), r)
  loaded = A.read(self.d)
  self.eq(loaded, r)
  for key in r["confirmation"]:
   self.eq(loaded["confirmation"][key].encode("utf-8"),
       r["confirmation"][key].encode("utf-8"))
  loaded["custody"]["opaque"].append("local")
  self.stored(r)
  self.refuses("record", A.create, self.d, grant())

 def test_top_level_schema(self):
  for key in grant():
   with self.subTest(missing=key):
    r = grant()
    del r[key]
    self.refuses(key, A.create, self.d, r)
  for value in (None, [], "record", True):
   self.refuses("record", A.create, self.d, value)
  r = grant()
  r["extension"] = 1
  self.refuses("extension", A.create, self.d, r)
  self.assertFalse(self.path.exists())

 def test_integers_and_cap(self):
  for key in ("schema_version", "issued_at", "confirmed_at", "expires_at",
     "session_cap"):
   for value in (None, True, False, 1.0, "1", []):
    with self.subTest(key=key, value=value):
     r = grant()
     r[key] = value
     self.refuses(key, A.create, self.d, r)
  for key in ("schema_version", "session_cap"):
   for value in (0, 2):
    self.bad_field(key, value)

 def test_expiry_bounds(self):
  for duration in (-1, 0, 86401):
   r = grant()
   r["expires_at"] = r["issued_at"] + duration
   self.refuses("expires_at", A.create, self.d, r)
  self.create()
  self.eq(A.check(self.d, 86499, MANDATE), "valid")
  self.eq(A.check(self.d, 86500, MANDATE), "expired")
  self.eq(A.check(self.d, 86501, MANDATE), "expired")
  r = grant()
  r["expires_at"] = 101
  A.validate(r)

 def test_hex_fields(self):
  for key, width in (("grant_id", 32), ("mandate_sha256", 64)):
   for value in (None, 1, "A" * width, "g" * width,
      "a" * (width - 1), "a" * (width + 1), "a" * width + "\n"):
    self.bad_field(key, value)

 def test_nested_schemas(self):
  for field, keys in (("scope", ("plans", "repositories", "actions", "trackers")),
       ("confirmation", ("owner_response", "displayed_bounds")),
       ("invalidation", ("at", "kind", "reason"))):
   base = grant()
   base["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
   for key in keys:
    r = copy.deepcopy(base)
    del r[field][key]
    self.refuses(field + "." + key, A.create, self.d, r)
   r = copy.deepcopy(base)
   r[field]["extra"] = 1
   self.refuses(field + ".extra", A.create, self.d, r)
   for value in ([], "object", True):
    r = copy.deepcopy(base)
    r[field] = value
    self.refuses(field, A.create, self.d, r)
  for key in grant()["scope"]:
   for value in ("p", ["p", "p"], [1], [True], [None], [[]], None):
    r = grant()
    r["scope"][key] = value
    self.refuses("scope." + key, A.create, self.d, r)
  for key in grant()["confirmation"]:
   r = grant()
   r["confirmation"][key] = None
   self.refuses("confirmation." + key, A.create, self.d, r)
  for key, value in (("at", True), ("at", 1.5), ("kind", "expired"),
      ("kind", []), ("reason", ""), ("reason", 1)):
   r = grant()
   r["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
   r["invalidation"][key] = value
   self.refuses("invalidation." + key, A.create, self.d, r)

 def test_custody_json_types(self):
  for value in ([], True, "holder", {1: "key"}, {"x": (1, 2)},
     {"x": float("nan")}, {"x": float("inf")}, {"x": object()}):
   r = grant()
   r["custody"] = value
   self.refuses("custody", A.create, self.d, r)
  for value in (None, {}, {"float": 1.5, "nested": [False, {}]}):
   with tempfile.TemporaryDirectory() as d:
    r = grant()
    r["custody"] = value
    A.create(d, r)
    self.eq(A.read(d)["custody"], value)

 def test_digest_and_terminal_change(self):
  for content in (MANDATE.replace(b"grant", b"Grant"),
      MANDATE.replace(b"\n", b"\r\n"), MANDATE + b" ", b"\xff"):
   with self.subTest(content=content), tempfile.TemporaryDirectory() as d:
    self.eq(A.mandate_digest(content), hashlib.sha256(content).hexdigest())
    self.assertNotEqual(A.mandate_digest(content), A.mandate_digest(MANDATE))
    A.create(d, grant())
    self.eq(A.check(d, 102, content), "mandate_changed")
    old = A.read(d)
    self.eq(old["invalidation"]["kind"], "mandate_changed")
    self.eq(old["invalidation"]["at"], 102)
    self.eq(A.check(d, 103, MANDATE), "invalidated")
    self.eq(A.check(d, 104, content), "invalidated")
    self.eq(A.revoke(d, 105, "later"), old)
    self.eq(A.read(d), old)

 def test_revoke_and_expired_mismatch(self):
  self.create()
  old = A.revoke(self.d, 102, "  owner said stop\n")
  self.eq(old["invalidation"],
      {"at": 102, "kind": "revoked", "reason": "  owner said stop\n"})
  self.eq(A.revoke(self.d, 103, "again"), old)
  self.eq(A.check(self.d, 86501, b"changed"), "invalidated")
  with tempfile.TemporaryDirectory() as d:
   A.create(d, grant())
   self.eq(A.check(d, 86501, b"changed"), "mandate_changed")

 def test_argument_refusals(self):
  self.create()
  for now in (True, 1.0, "101", None):
   self.refuses("now", A.check, self.d, now, MANDATE)
   self.refuses("now", A.revoke, self.d, now, "stop")
  for content in ("text", None, bytearray(MANDATE)):
   self.refuses("mandate_bytes", A.check, self.d, 101, content)
  for reason in ("", None, 1):
   self.refuses("reason", A.revoke, self.d, 101, reason)
  self.stored(grant())

 def test_replacement_custody(self):
  self.create()
  for custody in (None, {}, {"other": "session"},
      {"opaque": [True, True, None, {"label": "  untouched  "}]}):
   new = self.successor(fresh=True)
   new["custody"] = custody
   self.refuses("custody", A.replace, self.d, new)
  new = self.successor()
  new["scope"]["actions"] = ["read"]
  new["expires_at"] -= 1
  self.eq(A.replace(self.d, new), new)
  self.stored(new)
  self.eq(list(self.d.glob("*.json")), [self.path])
  self.eq(json.loads((self.d / "superseded" /
         ("a" * 32 + ".json")).read_bytes()), grant())

 def test_expansion_needs_consent(self):
  self.create()
  new = self.successor()
  new["issued_at"] += 1
  new["expires_at"] += 1
  self.refuses("expires_at", A.replace, self.d, new)
  for key in grant()["scope"]:
   new = self.successor()
   new["scope"][key].append("additional")
   self.refuses("scope." + key, A.replace, self.d, new)
  self.stored(grant())
  new = self.successor(fresh=True)
  new["issued_at"] += 1
  new["expires_at"] += 1
  for values in new["scope"].values():
   values.append("additional")
  self.eq(A.replace(self.d, new), new)

 def test_reordering_and_new_bounds(self):
  self.create()
  new = self.successor()
  new["scope"]["actions"].reverse()
  self.eq(A.replace(self.d, new), new)
  later = self.successor(new)
  later["grant_id"] = "c" * 32
  later["confirmation"]["displayed_bounds"] += " new"
  later["scope"]["actions"].append("more")
  self.eq(A.replace(self.d, later), later)

 def test_same_id_refused(self):
  self.create()
  self.refuses("grant_id", A.replace, self.d, grant())
  A.revoke(self.d, 101, "stop")
  new = grant()
  new["confirmation"]["owner_response"] += " new"
  self.refuses("grant_id", A.replace, self.d, new)
  self.assertIsNotNone(A.read(self.d)["invalidation"])

 def test_terminal_archive(self):
  self.create()
  A.check(self.d, 101, b"changed")
  old = A.read(self.d)

  raw = json.dumps(old, indent=4).encode() + b" \n"
  self.path.write_bytes(raw)
  self.refuses("confirmation", A.replace, self.d, self.successor(old))
  new = self.successor(old, fresh=True)
  self.eq(A.replace(self.d, new), new)
  archive = self.d / "superseded" / (old["grant_id"] + ".json")
  self.eq(archive.read_bytes(), raw)
  self.eq(json.loads(archive.read_bytes())["invalidation"], old["invalidation"])
  self.eq(A.check(self.d, 102, MANDATE), "valid")
  recycled = grant()
  recycled["confirmation"]["owner_response"] += " new again"
  self.refuses("grant_id", A.replace, self.d, recycled)
  self.path.unlink()
  self.refuses("grant_id", A.create, self.d, recycled)
  self.eq(archive.read_bytes(), raw)

 def test_fresh_consent_clear(self):
  self.create()
  new = self.successor(fresh=True)
  new["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
  self.refuses("invalidation", A.replace, self.d, new)

 def test_archive_conflict(self):
  self.create()
  archive = self.d / "superseded"
  archive.mkdir()
  prior = archive / ("a" * 32 + ".json")
  prior.write_bytes(b"different")
  self.refuses("superseded", A.replace, self.d, self.successor())
  self.stored(grant())
  self.eq(prior.read_bytes(), b"different")

 def test_interrupted_write(self):
  self.create()
  old_raw = self.path.read_bytes()
  for op in (lambda: A.revoke(self.d, 101, "stop"),
      lambda: A.check(self.d, 101, b"changed")):
   with mock.patch.object(A.os, "replace", side_effect=OSError("injected before replace")):
    with self.assertRaisesRegex(OSError, "injected"):
     op()
   self.eq(self.path.read_bytes(), old_raw)
   self.stored(grant())
   self.eq(list(self.d.glob(".authority-*")), [])

 def test_replacement_retry(self):
  self.create()
  old_raw = self.path.read_bytes()
  replace = os.replace
  def fail_head(src, dst):
   if Path(dst) == self.path:
    raise OSError("head interrupted")
   return replace(src, dst)
  with mock.patch.object(A.os, "replace", side_effect=OSError("archive interrupted")):
   with self.assertRaisesRegex(OSError, "archive interrupted"):
    A.replace(self.d, self.successor())
  self.stored(grant())
  with mock.patch.object(A.os, "replace", side_effect=fail_head):
   with self.assertRaisesRegex(OSError, "head interrupted"):
    A.replace(self.d, self.successor())
  self.stored(grant())
  archive = self.d / "superseded" / ("a" * 32 + ".json")
  self.eq(archive.read_bytes(), old_raw)
  self.eq(A.replace(self.d, self.successor()), self.successor())
  self.eq(archive.read_bytes(), old_raw)

 def test_archive_fsync_retry(self):
  self.create()
  sync = A._sync_directory
  def fail_archive(path):
   if path.name == "superseded":
    raise OSError("archive fsync failed")
   return sync(path)
  with mock.patch.object(A, "_sync_directory", side_effect=fail_archive):
   with self.assertRaisesRegex(OSError, "archive fsync failed"):
    A.replace(self.d, self.successor())
  self.stored(grant())
  with mock.patch.object(A.os, "replace", wraps=A.os.replace) as install:
   A.replace(self.d, self.successor())
  self.eq([Path(call.args[1]).parent for call in install.call_args_list],
      [self.d / "superseded", self.d])

 def test_corrupt_reads_refuse(self):
  r = grant()
  r["session_cap"] = True
  duplicate = json.dumps(grant()).replace('"session_cap": 1',
            '"session_cap": 2, "session_cap": 1')
  for raw in (b'{"grant_id":', b"", b"not-json", b"\xff", b"[]",
     json.dumps(r).encode(), duplicate.encode()):
   self.path.write_bytes(raw)
   for op in (lambda: A.read(self.d),
       lambda: A.check(self.d, 101, MANDATE),
       lambda: A.revoke(self.d, 101, "stop"),
       lambda: A.replace(self.d, self.successor())):
    with self.assertRaises(A.AuthorityError):
     op()
   self.eq(self.path.read_bytes(), raw)

 def test_target_directory_is_input(self):
  missing = self.d / "not-created"
  for op in (lambda: A.create(missing, grant()), lambda: A.read(missing),
      lambda: A.check(missing, 101, MANDATE),
      lambda: A.revoke(missing, 101, "stop"),
      lambda: A.replace(missing, self.successor())):
   with self.assertRaises(FileNotFoundError):
    op()
   self.assertFalse(missing.exists())

 def test_durable_write_protocol(self):
  events = []
  fsync, replace = os.fsync, os.replace
  def sync(fd):
   events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
   return fsync(fd)
  def install(src, dst):
   src, dst = Path(src), Path(dst)
   self.eq(src.parent, dst.parent)
   self.assertNotEqual(src, dst)
   with (self.d / A.LOCK_NAME).open("rb") as handle:
    with self.assertRaises(BlockingIOError):
     fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
   self.eq(events[-1], "file")
   events.append("replace")
   return replace(str(src), str(dst))
  with mock.patch.object(A.os, "fsync", side_effect=sync), \
    mock.patch.object(A.os, "replace", side_effect=install):
   self.create()
   A.check(self.d, 101, b"changed")
   A.replace(self.d, self.successor(fresh=True))
   A.revoke(self.d, 102, "stop")
  self.eq(events.count("replace"), 5)
  for index, event in enumerate(events):
   if event == "replace":
    self.eq(events[index - 1:index + 2], ["file", "replace", "directory"])

 def test_distinct_surrogates(self):
  pair, scalar = chr(0xD83D) + chr(0xDE00), chr(0x1F600)
  tokens = [pair, scalar, chr(0xD800), chr(0xDFFF), r"\ud83d\ude00", "\\", '"', "é", "\n"]
  r = grant()
  r["scope"]["plans"] = tokens
  r["confirmation"] = {key: "|".join(a + b for a in tokens for b in tokens)
        for key in r["confirmation"]}
  r["custody"] = {token: tokens for token in tokens}
  self.eq(A.create(self.d, r), r)
  self.stored(r)
  self.path.read_bytes().decode("utf-8")
  new = self.successor(r)
  self.eq(A.replace(self.d, new), new)
  self.stored(new)
  revoked = A.revoke(self.d, 101, pair)
  self.stored(revoked)
  self.eq(revoked["invalidation"]["reason"], pair)

 def test_cycles_and_shared_values(self):
  self.create()
  for cycle in ({}, []):
   if isinstance(cycle, dict):
    cycle["self"] = cycle
   else:
    cycle.append(cycle)
   new = self.successor()
   new["custody"] = {"cycle": cycle}
   for op in (A.create, A.replace):
    self.refuses("custody", op, self.d, new)
   self.stored(grant())
  shared = []
  r = grant()
  r["custody"] = {"a": shared, "b": shared}
  with tempfile.TemporaryDirectory() as d:
   self.eq(A.create(d, r), r)

 def test_reused_consent_invalidated(self):
  self.create()
  new = self.successor()
  new["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
  self.eq(A.replace(self.d, new), new)
  self.eq(A.check(self.d, 102, MANDATE), "invalidated")
  self.stored(new)

 def test_malformed_utf8(self):
  self.create()
  raw = self.path.read_bytes()
  for bad in (b'\xed\xa0\x80', b'\xff', b'\xc0\xaf', b'\xe2\x82'):
   corrupt = raw.replace(b'"p"', b'"' + bad + b'"', 1)
   self.path.write_bytes(corrupt)
   self.refuses("record", A.read, self.d)
   self.refuses("record", A.check, self.d, 100, MANDATE)
   self.eq(self.path.read_bytes(), corrupt)

 def test_large_integers(self):
  limit = getattr(sys, "get_int_max_str_digits", lambda: None)
  before = limit()
  big = 1 << 16384
  r = grant()
  r.update(issued_at=big, confirmed_at=big, expires_at=big + 86400)
  r["custody"] = {"positive": big, "negative": -big}
  self.eq(A.create(self.d, r), r)
  self.stored(r)
  new = dict(r, grant_id="b" * 32)
  self.eq(A.replace(self.d, new), new)
  self.eq(A.check(self.d, big, MANDATE), "valid")
  A.revoke(self.d, big, "stop")
  self.eq(A.read(self.d)["invalidation"]["at"], big)
  self.eq(limit(), before)
  for field in (None, "scope", "confirmation", "invalidation"):
   r = grant()
   r["invalidation"] = {"at": 100, "kind": "revoked", "reason": "stop"}
   (r if field is None else r[field])[big] = 0
   self.refuses(field or "record", A.create, self.d, r)

 def test_deep_custody(self):
  depth = sys.getrecursionlimit() + 100
  node = {"leaf": "exact"}
  for _ in range(depth):
   node = {"next": [node]}
  r = grant()
  r["custody"] = node
  A.create(self.d, r)
  A.replace(self.d, dict(r, grant_id="b" * 32))
  self.eq(A.check(self.d, 101, MANDATE), "valid")
  loaded = A.revoke(self.d, 102, "stop")["custody"]
  for _ in range(depth):
   loaded = loaded["next"][0]
  self.eq(loaded, {"leaf": "exact"})
  tail = node
  for _ in range(depth):
   tail = tail["next"][0]
  tail["cycle"] = node
  self.refuses("custody", A.replace, self.d, dict(r, grant_id="c" * 32))

 def test_json_grammar(self):
  r = grant()
  r["custody"] = {"opaque": [1, 2]}
  raw = json.dumps(r, separators=(",", ":")).encode()
  for fragment in (b'[1 2]', b'[1,]', b'{"x" 1}', b'{"x":}', b'{"x":1,}',
          b'{"x":1 "y":2}', b'[truefalse]', b'01', b'"a":"b"',
          b'NaN', b'Infinity', b'-Infinity'):
   self.path.write_bytes(raw.replace(b'[1,2]', fragment))
   self.refuses("record|custody", A.read, self.d)
  for suffix in (b' {}', b',null', b','):
   self.path.write_bytes(raw + suffix)
   self.refuses("record", A.read, self.d)

if __name__ == "__main__":
 unittest.main()
