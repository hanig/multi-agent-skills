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

  def refuses(self, field, operation, *args):
    with self.assertRaisesRegex(A.AuthorityError, field):
      operation(*args)

  def successor(self, old=None, fresh=False):
    new = copy.deepcopy(old if old is not None else grant())
    new["grant_id"] = "b" * 32
    if fresh:
      new["confirmation"]["owner_response"] += " fresh"
      new["invalidation"] = None
    return new

  def test_confirmation_and_custody_roundtrip(self):
    record = grant()
    self.eq(A.create(self.d, record), record)
    loaded = A.read(self.d)
    self.eq(loaded, record)
    for key in record["confirmation"]:
      self.eq(loaded["confirmation"][key].encode("utf-8"),
              record["confirmation"][key].encode("utf-8"))
    loaded["custody"]["opaque"].append("local")
    self.eq(A.read(self.d), record)
    self.refuses("record", A.create, self.d, grant())

  def test_exact_top_level_schema(self):
    for key in grant():
      with self.subTest(missing=key):
        record = grant()
        del record[key]
        self.refuses(key, A.create, self.d, record)
    for value in (None, [], "record", True):
      self.refuses("record", A.create, self.d, value)
    record = grant()
    record["extension"] = 1
    self.refuses("extension", A.create, self.d, record)
    self.assertFalse(self.path.exists())

  def test_integer_types_and_cap(self):
    for key in ("schema_version", "issued_at", "confirmed_at", "expires_at",
          "session_cap"):
      for value in (None, True, False, 1.0, "1", []):
        with self.subTest(key=key, value=value):
          record = grant()
          record[key] = value
          self.refuses(key, A.create, self.d, record)
    for key in ("schema_version", "session_cap"):
      for value in (0, 2):
        record = grant()
        record[key] = value
        self.refuses(key, A.create, self.d, record)

  def test_expiry_ceiling_and_boundary(self):
    for duration in (-1, 0, 86401):
      record = grant()
      record["expires_at"] = record["issued_at"] + duration
      self.refuses("expires_at", A.create, self.d, record)
    A.create(self.d, grant())  # exactly 86400 seconds
    self.eq(A.check(self.d, 86499, MANDATE), "valid")
    self.eq(A.check(self.d, 86500, MANDATE), "expired")
    self.eq(A.check(self.d, 86501, MANDATE), "expired")
    record = grant()
    record["expires_at"] = 101
    A.validate(record)  # minimum positive integer duration

  def test_hex_fields(self):
    for key, width in (("grant_id", 32), ("mandate_sha256", 64)):
      for value in (None, 1, "A" * width, "g" * width,
             "a" * (width - 1), "a" * (width + 1), "a" * width + "\n"):
        record = grant()
        record[key] = value
        self.refuses(key, A.create, self.d, record)

  def test_nested_schemas(self):
    for field, keys in (("scope", ("plans", "repositories", "actions", "trackers")),
              ("confirmation", ("owner_response", "displayed_bounds")),
              ("invalidation", ("at", "kind", "reason"))):
      template = grant()
      template["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
      for key in keys:
        record = copy.deepcopy(template)
        del record[field][key]
        self.refuses(field + "." + key, A.create, self.d, record)
      record = copy.deepcopy(template)
      record[field]["extra"] = 1
      self.refuses(field + ".extra", A.create, self.d, record)
      for value in ([], "object", True):
        record = copy.deepcopy(template)
        record[field] = value
        self.refuses(field, A.create, self.d, record)
    for key in grant()["scope"]:
      for value in ("p", ["p", "p"], [1], [True], [None], [[]], None):
        record = grant()
        record["scope"][key] = value
        self.refuses("scope." + key, A.create, self.d, record)
    for key in grant()["confirmation"]:
      record = grant()
      record["confirmation"][key] = None
      self.refuses("confirmation." + key, A.create, self.d, record)
    for key, value in (("at", True), ("at", 1.5), ("kind", "expired"),
             ("kind", []), ("reason", ""), ("reason", 1)):
      record = grant()
      record["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
      record["invalidation"][key] = value
      self.refuses("invalidation." + key, A.create, self.d, record)

  def test_opaque_custody_requires_lossless_json(self):
    for value in ([], True, "holder", {1: "key"}, {"x": (1, 2)},
           {"x": float("nan")}, {"x": float("inf")}, {"x": object()}):
      record = grant()
      record["custody"] = value
      self.refuses("custody", A.create, self.d, record)
    for value in (None, {}, {"float": 1.5, "nested": [False, {}]}):
      with tempfile.TemporaryDirectory() as directory:
        record = grant()
        record["custody"] = value
        A.create(directory, record)
        self.eq(A.read(directory)["custody"], value)

  def test_digest_exact_bytes_and_terminal_mandate_change(self):
    for content in (MANDATE.replace(b"grant", b"Grant"),
            MANDATE.replace(b"\n", b"\r\n"), MANDATE + b" ", b"\xff"):
      with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
        self.eq(A.mandate_digest(content), hashlib.sha256(content).hexdigest())
        self.assertNotEqual(A.mandate_digest(content), A.mandate_digest(MANDATE))
        A.create(directory, grant())
        self.eq(A.check(directory, 102, content), "mandate_changed")
        invalidated = A.read(directory)
        self.eq(invalidated["invalidation"]["kind"], "mandate_changed")
        self.eq(invalidated["invalidation"]["at"], 102)
        self.eq(A.check(directory, 103, MANDATE), "invalidated")
        self.eq(A.check(directory, 104, content), "invalidated")
        self.eq(A.revoke(directory, 105, "later"), invalidated)
        self.eq(A.read(directory), invalidated)

  def test_revoke_and_expired_mismatch(self):
    A.create(self.d, grant())
    old = A.revoke(self.d, 102, "  owner said stop\n")
    self.eq(old["invalidation"],
            {"at": 102, "kind": "revoked", "reason": "  owner said stop\n"})
    self.eq(A.revoke(self.d, 103, "again"), old)
    self.eq(A.check(self.d, 86501, b"changed"), "invalidated")
    with tempfile.TemporaryDirectory() as directory:
      A.create(directory, grant())
      self.eq(A.check(directory, 86501, b"changed"), "mandate_changed")

  def test_argument_refusals(self):
    A.create(self.d, grant())
    for now in (True, 1.0, "101", None):
      self.refuses("now", A.check, self.d, now, MANDATE)
      self.refuses("now", A.revoke, self.d, now, "stop")
    for content in ("text", None, bytearray(MANDATE)):
      self.refuses("mandate_bytes", A.check, self.d, 101, content)
    for reason in ("", None, 1):
      self.refuses("reason", A.revoke, self.d, 101, reason)
    self.eq(A.read(self.d), grant())

  def test_replacement_single_slot_and_unchanged_custody(self):
    A.create(self.d, grant())
    for custody in (None, {}, {"other": "session"},
            {"opaque": [True, True, None, {"label": "  untouched  "}]}):
      new = self.successor(fresh=True)
      new["custody"] = custody
      self.refuses("custody", A.replace, self.d, new)
    new = self.successor()
    new["scope"]["actions"] = ["read"]
    new["expires_at"] -= 1
    self.eq(A.replace(self.d, new), new)
    self.eq(A.read(self.d), new)
    self.eq(list(self.d.glob("*.json")), [self.path])
    self.eq(json.loads((self.d / "superseded" /
                  ("a" * 32 + ".json")).read_bytes()), grant())

  def test_replacement_requires_consent_for_each_expansion(self):
    A.create(self.d, grant())
    new = self.successor()
    new["issued_at"] += 1
    new["expires_at"] += 1  # still within its own 24-hour ceiling
    self.refuses("expires_at", A.replace, self.d, new)
    for key in grant()["scope"]:
      new = self.successor()
      new["scope"][key].append("additional")
      self.refuses("scope." + key, A.replace, self.d, new)
    self.eq(A.read(self.d), grant())
    new = self.successor(fresh=True)
    new["issued_at"] += 1
    new["expires_at"] += 1
    for values in new["scope"].values():
      values.append("additional")
    self.eq(A.replace(self.d, new), new)

  def test_reordered_scope_and_new_bounds_confirmation(self):
    A.create(self.d, grant())
    new = self.successor()
    new["scope"]["actions"].reverse()
    self.eq(A.replace(self.d, new), new)
    later = self.successor(new)
    later["grant_id"] = "c" * 32
    later["confirmation"]["displayed_bounds"] += " new"
    later["scope"]["actions"].append("more")
    self.eq(A.replace(self.d, later), later)

  def test_same_grant_id_never_replaced(self):
    A.create(self.d, grant())
    self.refuses("grant_id", A.replace, self.d, grant())
    A.revoke(self.d, 101, "stop")
    new = grant()
    new["confirmation"]["owner_response"] += " new"
    self.refuses("grant_id", A.replace, self.d, new)
    self.assertIsNotNone(A.read(self.d)["invalidation"])

  def test_invalidated_replacement_archives_verbatim_and_cannot_recycle(self):
    A.create(self.d, grant())
    A.check(self.d, 101, b"changed")
    old = A.read(self.d)
    # An existing valid file need not use this library's formatting.
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
    self.path.unlink()  # even explicit removal cannot recycle a historical ID
    self.refuses("grant_id", A.create, self.d, recycled)
    self.eq(archive.read_bytes(), raw)

  def test_fresh_consent_requires_clear_invalidation(self):
    A.create(self.d, grant())
    new = self.successor(fresh=True)
    new["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
    self.refuses("invalidation", A.replace, self.d, new)

  def test_archive_conflict_refuses_before_head_write(self):
    A.create(self.d, grant())
    archive = self.d / "superseded"
    archive.mkdir()
    predecessor = archive / ("a" * 32 + ".json")
    predecessor.write_bytes(b"different")
    self.refuses("superseded", A.replace, self.d, self.successor())
    self.eq(A.read(self.d), grant())
    self.eq(predecessor.read_bytes(), b"different")

  def test_fault_before_replace_keeps_old_record_complete(self):
    A.create(self.d, grant())
    old_raw = self.path.read_bytes()
    for operation in (lambda: A.revoke(self.d, 101, "stop"),
             lambda: A.check(self.d, 101, b"changed")):
      with mock.patch.object(A.os, "replace", side_effect=OSError("injected before replace")):
        with self.assertRaisesRegex(OSError, "injected"):
          operation()
      self.eq(self.path.read_bytes(), old_raw)
      self.eq(A.read(self.d), grant())
      self.eq(list(self.d.glob(".authority-*")), [])

  def test_partial_replacement_retry_and_archive_failure(self):
    A.create(self.d, grant())
    old_raw = self.path.read_bytes()
    replace = os.replace
    def fail_head(source, destination):
      if Path(destination) == self.path:
        raise OSError("head interrupted")
      return replace(source, destination)
    with mock.patch.object(A.os, "replace", side_effect=OSError("archive interrupted")):
      with self.assertRaisesRegex(OSError, "archive interrupted"):
        A.replace(self.d, self.successor())
    self.eq(A.read(self.d), grant())
    with mock.patch.object(A.os, "replace", side_effect=fail_head):
      with self.assertRaisesRegex(OSError, "head interrupted"):
        A.replace(self.d, self.successor())
    self.eq(A.read(self.d), grant())
    archive = self.d / "superseded" / ("a" * 32 + ".json")
    self.eq(archive.read_bytes(), old_raw)
    self.eq(A.replace(self.d, self.successor()), self.successor())
    self.eq(archive.read_bytes(), old_raw)

  def test_archive_sync_failure_retry(self):
    A.create(self.d, grant())
    sync = A._sync_directory
    def fail_archive(path):
      if path.name == "superseded":
        raise OSError("archive fsync failed")
      return sync(path)
    with mock.patch.object(A, "_sync_directory", side_effect=fail_archive):
      with self.assertRaisesRegex(OSError, "archive fsync failed"):
        A.replace(self.d, self.successor())
    self.eq(A.read(self.d), grant())
    with mock.patch.object(A.os, "replace", wraps=A.os.replace) as install:
      A.replace(self.d, self.successor())
    self.eq([Path(call.args[1]).parent for call in install.call_args_list],
            [self.d / "superseded", self.d])

  def test_corrupt_reads_refuse(self):
    record = grant()
    record["session_cap"] = True
    duplicate = json.dumps(grant()).replace('"session_cap": 1',
                        '"session_cap": 2, "session_cap": 1')
    for raw in (b'{"grant_id":', b"", b"not-json", b"\xff", b"[]",
          json.dumps(record).encode(), duplicate.encode()):
      self.path.write_bytes(raw)
      for operation in (lambda: A.read(self.d),
               lambda: A.check(self.d, 101, MANDATE),
               lambda: A.revoke(self.d, 101, "stop"),
               lambda: A.replace(self.d, self.successor())):
        with self.assertRaises(A.AuthorityError):
          operation()
      self.eq(self.path.read_bytes(), raw)

  def test_target_directory_is_input(self):
    missing = self.d / "not-created"
    for operation in (lambda: A.create(missing, grant()), lambda: A.read(missing),
             lambda: A.check(missing, 101, MANDATE),
             lambda: A.revoke(missing, 101, "stop"),
             lambda: A.replace(missing, self.successor())):
      with self.assertRaises(FileNotFoundError):
        operation()
      self.assertFalse(missing.exists())

  def test_every_write_holds_real_lock_and_syncs_in_order(self):
    events = []
    fsync, replace = os.fsync, os.replace
    def sync(fd):
      events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
      return fsync(fd)
    def install(source, destination):
      source, destination = Path(source), Path(destination)
      self.eq(source.parent, destination.parent)
      self.assertNotEqual(source, destination)
      with (self.d / A.LOCK_NAME).open("rb") as handle:
        with self.assertRaises(BlockingIOError):
          fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
      self.eq(events[-1], "file")
      events.append("replace")
      return replace(str(source), str(destination))
    with mock.patch.object(A.os, "fsync", side_effect=sync), \
        mock.patch.object(A.os, "replace", side_effect=install):
      A.create(self.d, grant())
      A.check(self.d, 101, b"changed")
      A.replace(self.d, self.successor(fresh=True))
      A.revoke(self.d, 102, "stop")
    self.eq(events.count("replace"), 5)  # includes predecessor archive
    for index, event in enumerate(events):
      if event == "replace":
        self.eq(events[index - 1:index + 2], ["file", "replace", "directory"])

  def test_surrogate_strings_remain_distinct(self):
    pair, scalar = chr(0xD83D) + chr(0xDE00), chr(0x1F600)
    tokens = [pair, scalar, chr(0xD800), chr(0xDFFF), r"\ud83d\ude00", "\\", '"', "é", "\n"]
    record = grant()
    record["scope"]["plans"] = tokens
    record["confirmation"] = {key: "|".join(a + b for a in tokens for b in tokens)
                 for key in record["confirmation"]}
    record["custody"] = {token: tokens for token in tokens}
    self.eq(A.create(self.d, record), record)
    self.eq(A.read(self.d), record)
    self.path.read_bytes().decode("utf-8")  # the stored JSON remains valid UTF-8
    new = self.successor(record)
    self.eq(A.replace(self.d, new), new)
    self.eq(A.read(self.d), new)
    revoked = A.revoke(self.d, 101, pair)
    self.eq(A.read(self.d), revoked)
    self.eq(revoked["invalidation"]["reason"], pair)

  def test_cyclic_custody_refuses_but_shared_values_pass(self):
    A.create(self.d, grant())
    for cycle in ({}, []):
      if isinstance(cycle, dict):
        cycle["self"] = cycle
      else:
        cycle.append(cycle)
      new = self.successor()
      new["custody"] = {"cycle": cycle}
      for operation in (A.create, A.replace):
        self.refuses("custody", operation, self.d, new)
      self.eq(A.read(self.d), grant())
    shared = []
    record = grant()
    record["custody"] = {"a": shared, "b": shared}
    with tempfile.TemporaryDirectory() as directory:
      self.eq(A.create(directory, record), record)

  def test_reused_consent_can_store_an_invalidated_successor(self):
    A.create(self.d, grant())
    new = self.successor()
    new["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
    self.eq(A.replace(self.d, new), new)
    self.eq(A.check(self.d, 102, MANDATE), "invalidated")
    self.eq(A.read(self.d), new)


if __name__ == "__main__":
  unittest.main()
