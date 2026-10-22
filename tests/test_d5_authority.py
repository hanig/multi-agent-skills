"""Pure grant schema, exact codec and terminal record transitions."""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

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
        "custody": None,
        "invalidation": None,
    }


def held():
    r = grant()
    r["custody"] = {"session_id": "native", "anchor_pid": 42,
                    "anchor_start_us": 9000001, "generation": 1,
                    "claimed_at": 101, "relinquished_at": None}
    return r


class TestD5Authority(unittest.TestCase):

    def refuses(self, field, op, *args):
        with self.assertRaisesRegex(A.AuthorityError, field):
            op(*args)

    def bad_field(self, key, value):
        r = grant()
        r[key] = value
        self.refuses(key, A.validate, r)

    def successor(self, old=None, fresh=False):
        new = copy.deepcopy(old if old is not None else grant())
        new["grant_id"] = "b" * 32
        if fresh:
            new["confirmation"]["owner_response"] += " fresh"
            new["invalidation"] = None
        return new

    def test_top_level_schema(self):
        for key in grant():
            with self.subTest(missing=key):
                r = grant()
                del r[key]
                self.refuses(key, A.validate, r)
        for value in (None, [], "record", True):
            self.refuses("record", A.validate, value)
        r = grant()
        r["extension"] = 1
        self.refuses("extension", A.validate, r)

    def test_integers_and_cap(self):
        for key in ("schema_version", "issued_at", "confirmed_at", "expires_at",
                    "session_cap"):
            for value in (None, True, False, 1.0, "1", []):
                with self.subTest(key=key, value=value):
                    r = grant()
                    r[key] = value
                    self.refuses(key, A.validate, r)
        for key in ("schema_version", "session_cap"):
            for value in (0, 2):
                self.bad_field(key, value)

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
                self.refuses(field + "." + key, A.validate, r)
            r = copy.deepcopy(base)
            r[field]["extra"] = 1
            self.refuses(field + ".extra", A.validate, r)
            for value in ([], "object", True):
                r = copy.deepcopy(base)
                r[field] = value
                self.refuses(field, A.validate, r)
        for key in grant()["scope"]:
            for value in ("p", ["p", "p"], [1], [True], [None], [[]], None):
                r = grant()
                r["scope"][key] = value
                self.refuses("scope." + key, A.validate, r)
        for key in grant()["confirmation"]:
            r = grant()
            r["confirmation"][key] = None
            self.refuses("confirmation." + key, A.validate, r)
        for key, value in (("at", True), ("at", 1.5), ("kind", "expired"),
                        ("kind", []), ("reason", ""), ("reason", 1)):
            r = grant()
            r["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
            r["invalidation"][key] = value
            self.refuses("invalidation." + key, A.validate, r)

    def test_surrogates_refuse_with_field(self):
        for value in (chr(0xD800), chr(0xDFFF), chr(0xD83D) + chr(0xDE00)):
            for group, keys in (("scope", grant()["scope"]), ("confirmation", grant()["confirmation"])):
                for key in keys:
                    r = grant()
                    r[group][key] = [value] if group == "scope" else value
                    self.refuses(group + "." + key, A.validate, r)
            r = grant()
            r["invalidation"] = {"at": 101, "kind": "revoked", "reason": value}
            self.refuses("invalidation.reason", A.validate, r)
        r = grant()
        self.refuses("invalidation.reason", A.revoked, r, 101, chr(0xD800))
        self.assertEqual(r, grant())

    def test_purity(self):
        source = Path(A.__file__).read_text()
        for forbidden in ("open(", "os.", "fcntl", "pathlib", "tempfile", "shutil",
                          "import os", "d5_host", "subprocess", "import time",
                          "datetime", "__import__"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_exact_roundtrip(self):
        r = grant()
        raw = A.encode(r)
        self.assertIs(type(raw), bytes)
        raw.decode("utf-8")
        self.assertEqual(A.decode(raw), r)
        loaded = A.decode(raw)
        self.assertEqual(loaded, r)
        for key in r["confirmation"]:
            self.assertEqual(loaded["confirmation"][key].encode("utf-8"),
                             r["confirmation"][key].encode("utf-8"))
        loaded["scope"]["plans"].append("local")
        self.assertEqual(A.decode(raw), r)
        self.assertEqual(r, grant())

    def test_expiry_bounds(self):
        for duration in (-1, 0, 86401):
            r = grant()
            r["expires_at"] = r["issued_at"] + duration
            self.refuses("expires_at", A.validate, r)
        r = grant()
        self.assertEqual(A.evaluate(r, 86499, MANDATE), ("valid", None))
        self.assertEqual(A.evaluate(r, 86500, MANDATE), ("expired", None))
        self.assertEqual(A.evaluate(r, 86501, MANDATE), ("expired", None))
        r["expires_at"] = 101
        A.validate(r)

    def test_custody_null_or_exact_object(self):
        for value in ({}, {"session": "opaque"}, [], True, False, "holder", 0, 1):
            r = grant()
            r["custody"] = value
            self.refuses("custody", A.validate, r)
            self.refuses("custody", A.decode, json.dumps(r).encode())
        self.assertIsNone(A.decode(A.encode(grant()))["custody"])
        self.assertEqual(A.decode(A.encode(held())), held())

    def test_custody_field_schema(self):
        base = held()
        for field in base["custody"]:
            with self.subTest(missing=field):
                r = copy.deepcopy(base)
                del r["custody"][field]
                self.refuses("custody." + field, A.validate, r)
            for value in (True, False, 1.5, [], {}):
                with self.subTest(field=field, value=value):
                    r = copy.deepcopy(base)
                    r["custody"][field] = value
                    self.refuses("custody." + field, A.validate, r)
                    self.refuses("custody." + field, A.decode, json.dumps(r).encode())
        r = copy.deepcopy(base)
        r["custody"]["extra"] = None
        self.refuses("custody.extra", A.validate, r)
        r = copy.deepcopy(base)
        r["custody"][1] = None
        self.refuses("custody", A.validate, r)
        for value in (None, 1, "", chr(0xD800), chr(0xDFFF),
                      chr(0xD83D) + chr(0xDE00)):
            r = copy.deepcopy(base)
            r["custody"]["session_id"] = value
            self.refuses("custody.session_id", A.validate, r)
        for field in ("anchor_pid", "anchor_start_us", "generation",
                      "claimed_at", "relinquished_at"):
            limit = 2**31 if field == "anchor_pid" else 2**63
            for value in ("1", 0, -1, limit, 1 << 16384, None):
                if value is None and field == "relinquished_at":
                    continue
                with self.subTest(field=field, value=value):
                    r = copy.deepcopy(base)
                    r["custody"][field] = value
                    self.refuses("custody." + field, A.validate, r)
            for value in (1, limit - 1):
                r = copy.deepcopy(base)
                r["custody"]["claimed_at"] = 1
                r["custody"][field] = value
                self.assertEqual(A.decode(A.encode(r)), r)
        r = held()
        r["custody"]["relinquished_at"] = 100
        self.refuses("custody.relinquished_at", A.validate, r)

    def test_claim_no_custody_and_independence(self):
        for holder in ("alive", "dead", "unknown"):
            original = grant()
            r = A.claimed(original, "native", 42, 9000001, 101, holder)
            self.assertEqual(r, held())
            self.assertEqual(original, grant())
            r["scope"]["plans"].append("local")
            r["confirmation"]["owner_response"] = "local"
            self.assertEqual(original, grant())
        # No normalization of scalar session IDs, or lower bound tied to issued_at.
        for session in (" ", "\x00", "e\u0301", "é", chr(0x10FFFF)):
            r = A.claimed(grant(), session, 1, 1, 1, "unknown")
            self.assertEqual(r["custody"]["session_id"], session)
            self.assertEqual(A.decode(A.encode(r)), r)

    def test_compaction_exact_identity(self):
        old = held()
        old["custody"]["generation"] = 2**63 - 1
        for holder in ("alive", "dead", "unknown"):
            r = A.claimed(old, "native", 42, 9000001, 102, holder)
            self.assertEqual(r, old)
            r["custody"]["generation"] = 1
            r["scope"]["plans"].append("local")
            self.assertEqual(old["custody"]["generation"], 2**63 - 1)
            self.assertEqual(old["scope"], grant()["scope"])
        for claimant in (("other", 42, 9000001), ("native", 43, 9000001),
                         ("native", 42, 9000002), ("other", 43, 9000002)):
            with self.subTest(claimant=claimant):
                self.refuses("dead holder", A.claimed, held(), *claimant, 102, "alive")

    def test_release_does_not_allow_live_takeover(self):
        old = A.relinquished(held(), "native", 42, 9000001, 102)
        self.refuses("dead holder", A.claimed, old, "other", 43, 9000002, 103, "alive")
        for holder in ("alive", "dead", "unknown"):
            self.refuses("custody relinquished", A.claimed,
                         old, "native", 42, 9000001, 103, holder)

    def test_stale_time_does_not_allow_takeover(self):
        # The schema imposes no relation between claimed_at and issued_at.
        old = held()
        old.update(issued_at=2**62, confirmed_at=2**62, expires_at=2**62 + 86400)
        for now in (2**62, old["expires_at"] - 1):
            self.refuses("dead holder", A.claimed, old,
                         "other", 43, 9000002, now, "alive")

    def test_dead_successor_keeps_grant_and_confirmation(self):
        for released in (False, True):
            old = held()
            old["custody"]["generation"] = 7
            if released:
                old["custody"]["relinquished_at"] = 102
            before = copy.deepcopy(old)
            for claimant in (("other", 43, 9000002), ("other", 42, 9000001),
                             ("native", 43, 9000001), ("native", 42, 9000002)):
                r = A.claimed(old, *claimant, 103, "dead")
                expected = copy.deepcopy(before)
                expected["custody"] = dict(zip(
                    ("session_id", "anchor_pid", "anchor_start_us"), claimant),
                    generation=8, claimed_at=103, relinquished_at=None)
                self.assertEqual(r, expected)
                self.assertEqual(A.decode(A.encode(r)), expected)
                r["confirmation"]["owner_response"] = "local"
                r["scope"]["plans"].append("local")
                self.assertEqual(old, before)

    def test_unknown_is_not_dead(self):
        for released in (False, True):
            old = held()
            if released:
                old["custody"]["relinquished_at"] = 102
            self.refuses("dead holder", A.claimed,
                         old, "other", 43, 9000002, 103, "unknown")

    def test_claim_invalid_grant(self):
        for old in (grant(), held()):
            for claimant in (("native", 42, 9000001), ("other", 43, 9000002)):
                for now in (old["expires_at"], old["expires_at"] + 1):
                    self.refuses("expired", A.claimed, old, *claimant, now, "dead")
                for kind in ("revoked", "mandate_changed"):
                    invalid = copy.deepcopy(old)
                    invalid["invalidation"] = {"at": 102, "kind": kind, "reason": "stop"}
                    for now in (103, old["expires_at"]):
                        self.refuses("invalidated", A.claimed, invalid, *claimant, now, "dead")
                self.assertEqual(old, grant() if old["custody"] is None else held())

    def test_claim_generation_exhaustion(self):
        old = held()
        old["custody"]["generation"] = 2**63 - 2
        r = A.claimed(old, "other", 43, 9000002, 102, "dead")
        self.assertEqual(r["custody"]["generation"], 2**63 - 1)
        before = copy.deepcopy(r)
        self.refuses("custody.generation", A.claimed,
                     r, "next", 44, 9000003, 103, "dead")
        self.assertEqual(r, before)

    def test_relinquish_by_exact_holder_only(self):
        self.refuses("custody", A.relinquished, grant(), "native", 42, 9000001, 102)
        for released in (False, True):
            old = held()
            if released:
                old["custody"]["relinquished_at"] = 102
            before = copy.deepcopy(old)
            for claimant in (("other", 42, 9000001), ("native", 43, 9000001),
                             ("native", 42, 9000002), ("other", 43, 9000002)):
                self.refuses("custody", A.relinquished, old, *claimant, 103)
            self.assertEqual(old, before)

    def test_relinquish_preserves_first_time_and_independence(self):
        old = held()
        self.refuses("now", A.relinquished, old, "native", 42, 9000001, 100)
        for now in (101, 102, old["expires_at"]):
            r = A.relinquished(old, "native", 42, 9000001, now)
            expected = held()
            expected["custody"]["relinquished_at"] = now
            self.assertEqual(r, expected)
            for later in (101, now, now + 1):
                again = A.relinquished(r, "native", 42, 9000001, later)
                self.assertEqual(again, expected)
                again["custody"]["relinquished_at"] = 200
                again["scope"]["plans"].append("local")
                self.assertEqual(r, expected)
            r["custody"]["generation"] = 2
            self.assertEqual(old, held())
        invalid = A.revoked(old, 102, "stop")
        r = A.relinquished(invalid, "native", 42, 9000001, 103)
        self.assertEqual(r["invalidation"], invalid["invalidation"])
        self.assertEqual(r["custody"]["relinquished_at"], 103)

    def test_custody_transition_arguments(self):
        old = held()
        for op, tail in ((A.claimed, ("dead",)), (A.relinquished, ())):
            for index, field, bad in (
                    (0, "session_id", (None, True, False, 1, "", [], chr(0xD800))),
                    (1, "anchor_pid", (None, True, False, 1.0, "42", 0, -1, 2**31)),
                    (2, "anchor_start_us", (None, True, False, 1.0, "1", 0, -1, 2**63)),
                    (3, "now", (None, True, False, 1.0, "1", 0, -1, 2**63))):
                for value in bad:
                    args = ["native", 42, 9000001, 102]
                    args[index] = value
                    self.refuses(field, op, old, *args, *tail)
            corrupt = held()
            corrupt["session_cap"] = True
            self.refuses("session_cap", op, corrupt, "native", 42, 9000001, 102, *tail)
        for holder in (None, True, False, 0, [], {}, "", "DEAD", "dead "):
            for record in (grant(), old):
                self.refuses("holder", A.claimed,
                             record, "native", 42, 9000001, 102, holder)
        self.assertEqual(old, held())

    def test_replacement_preserves_exact_custody(self):
        for fresh in (False, True):
            for released in (False, True):
                old = held()
                if released:
                    old["custody"]["relinquished_at"] = 102
                before = copy.deepcopy(old)
                new = self.successor(old, fresh=fresh)
                unchanged = copy.deepcopy(new)
                self.assertIs(A.replacement_allowed(old, new), True)
                self.assertEqual(new, unchanged)
                for field, value in (("session_id", "other"), ("anchor_pid", 43),
                                     ("anchor_start_us", 9000002), ("generation", 2),
                                     ("claimed_at", 100), ("relinquished_at", 103)):
                    changed = copy.deepcopy(new)
                    changed["custody"][field] = value
                    self.refuses("custody", A.replacement_allowed, old, changed)
                new["custody"] = None
                self.refuses("custody", A.replacement_allowed, old, new)
                self.refuses("custody", A.replacement_allowed, grant(), unchanged)
                self.assertEqual(old, before)

    def test_digest_and_terminal_change(self):
        for content in (MANDATE.replace(b"grant", b"Grant"),
                        MANDATE.replace(b"\n", b"\r\n"), MANDATE + b" ", b"\xff"):
            with self.subTest(content=content):
                self.assertEqual(A.mandate_digest(content), hashlib.sha256(content).hexdigest())
                self.assertNotEqual(A.mandate_digest(content), A.mandate_digest(MANDATE))
                r = grant()
                state, invalidation = A.evaluate(r, 102, content)
                self.assertEqual(state, "mandate_changed")
                self.assertEqual(r, grant())
                old = copy.deepcopy(r)
                old["invalidation"] = invalidation  # Future storage layer persists this.
                self.assertEqual(old["invalidation"]["kind"], "mandate_changed")
                self.assertEqual(old["invalidation"]["at"], 102)
                self.assertEqual(A.evaluate(old, 103, MANDATE), ("invalidated", None))
                self.assertEqual(A.evaluate(old, 104, content), ("invalidated", None))
                self.assertEqual(A.revoked(old, 105, "later"), old)
                self.assertEqual(A.decode(A.encode(old)), old)

    def test_revoke_and_expired_mismatch(self):
        r = grant()
        old = A.revoked(r, 102, "  owner said stop\n")
        self.assertEqual(old["invalidation"],
                         {"at": 102, "kind": "revoked", "reason": "  owner said stop\n"})
        self.assertEqual(A.revoked(old, 103, "again"), old)
        self.assertEqual(A.evaluate(old, 86501, b"changed"), ("invalidated", None))
        self.assertEqual(A.evaluate(r, 86501, b"changed")[0], "mandate_changed")
        self.assertEqual(r, grant())
        old["scope"]["plans"].append("local")
        self.assertEqual(r, grant())
        again = A.revoked(old, 103, "again")
        again["invalidation"]["reason"] = "local"
        self.assertEqual(old["invalidation"]["reason"], "  owner said stop\n")

    def test_argument_refusals(self):
        r = grant()
        for now in (True, 1.0, "101", None, 0, -1, 2**63, 1 << 16384):
            self.refuses("now", A.evaluate, r, now, MANDATE)
            self.refuses("now", A.revoked, r, now, "stop")
        for content in ("text", None, bytearray(MANDATE)):
            self.refuses("mandate_bytes", A.evaluate, r, 101, content)
            self.refuses("record", A.decode, content)
        for reason in ("", None, 1):
            self.refuses("reason", A.revoked, r, 101, reason)
        self.assertEqual(r, grant())

    def test_replacement_custody(self):
        old = grant()
        for custody in ({}, {"session": "new"}, [], True, "holder", 1):
            new = self.successor(fresh=True)
            new["custody"] = custody
            self.refuses("custody", A.replacement_allowed, old, new)
            bad_old = copy.deepcopy(old)
            bad_old["custody"] = custody
            self.refuses("custody", A.replacement_allowed, bad_old, self.successor())
        new = self.successor()
        new["scope"]["actions"] = ["read"]
        new["expires_at"] -= 1
        before = copy.deepcopy(new)
        self.assertIs(A.replacement_allowed(old, new), True)
        self.assertEqual(new, before)
        self.assertEqual(A.decode(A.encode(new)), new)
        self.assertEqual(old, grant())

    def test_expansion_needs_consent(self):
        old = grant()
        new = self.successor()
        new["issued_at"] += 1
        new["expires_at"] += 1
        self.refuses("expires_at", A.replacement_allowed, old, new)
        for key in grant()["scope"]:
            new = self.successor()
            new["scope"][key].append("additional")
            self.refuses("scope." + key, A.replacement_allowed, old, new)
        self.assertEqual(old, grant())
        new = self.successor(fresh=True)
        new["issued_at"] += 1
        new["expires_at"] += 1
        for values in new["scope"].values():
            values.append("additional")
        self.assertIs(A.replacement_allowed(old, new), True)

    def test_reordering_and_new_bounds(self):
        new = self.successor()
        new["scope"]["actions"].reverse()
        self.assertIs(A.replacement_allowed(grant(), new), True)
        later = self.successor(new)
        later["grant_id"] = "c" * 32
        later["confirmation"]["displayed_bounds"] += " new"
        later["scope"]["actions"].append("more")
        self.assertIs(A.replacement_allowed(new, later), True)

    def test_same_id_refused(self):
        self.refuses("grant_id", A.replacement_allowed, grant(), grant())
        old = A.revoked(grant(), 101, "stop")
        new = grant()
        new["confirmation"]["owner_response"] += " new"
        self.refuses("grant_id", A.replacement_allowed, old, new)
        self.assertIsNotNone(old["invalidation"])

    def test_terminal_archive(self):
        # Only the predecessor's pure consent/terminal-state assertions remain.
        old = grant()
        old["invalidation"] = A.evaluate(old, 101, b"changed")[1]
        self.refuses("confirmation", A.replacement_allowed, old, self.successor(old))
        new = self.successor(old, fresh=True)
        self.assertIs(A.replacement_allowed(old, new), True)
        self.assertEqual(A.evaluate(new, 102, MANDATE), ("valid", None))
        self.assertEqual(old["invalidation"]["kind"], "mandate_changed")
        old = A.revoked(grant(), 101, "stop")
        self.refuses("confirmation", A.replacement_allowed, old, self.successor(old))
        self.assertIs(A.replacement_allowed(old, self.successor(old, fresh=True)), True)

    def test_fresh_consent_clear(self):
        new = self.successor(fresh=True)
        new["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
        self.refuses("invalidation", A.replacement_allowed, grant(), new)

    def test_corrupt_reads_refuse(self):
        r = grant()
        r["session_cap"] = True
        duplicate = json.dumps(grant()).replace('"session_cap": 1',
                                                '"session_cap": 2, "session_cap": 1')
        for raw in (b'{"grant_id":', b"", b"not-json", b"\xff", b"[]",
                    json.dumps(r).encode(), duplicate.encode()):
            with self.assertRaises(A.AuthorityError):
                A.decode(raw)
        for op in (lambda: A.encode(r), lambda: A.evaluate(r, 101, MANDATE),
                   lambda: A.revoked(r, 101, "stop"),
                   lambda: A.replacement_allowed(r, self.successor()),
                   lambda: A.replacement_allowed(grant(), r)):
            self.refuses("session_cap", op)

    def test_scalar_strings_exact(self):
        tokens = [chr(0x1F600), chr(0x10FFFF), r"\ud83d\ude00", "\\", '"', "é",
                  "\n", "\x00", "e\u0301"]
        r = grant()
        r["scope"]["plans"] = tokens
        text = "|".join(a + b for a in tokens for b in tokens)
        r["confirmation"] = {key: text for key in r["confirmation"]}
        self.assertEqual(A.decode(A.encode(r)), r)
        self.assertEqual(A.decode(A.encode(r)), r)
        A.encode(r).decode("utf-8")
        new = self.successor(r)
        self.assertIs(A.replacement_allowed(r, new), True)
        self.assertEqual(A.decode(A.encode(new)), new)
        revoked = A.revoked(new, 101, text)
        self.assertEqual(A.decode(A.encode(revoked)), revoked)
        self.assertEqual(revoked["invalidation"]["reason"], text)

    def test_reused_consent_invalidated(self):
        new = self.successor()
        new["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
        self.assertIs(A.replacement_allowed(grant(), new), True)
        self.assertEqual(A.evaluate(new, 102, MANDATE), ("invalidated", None))
        self.assertEqual(A.decode(A.encode(new)), new)

    def test_malformed_utf8(self):
        raw = A.encode(grant())
        for bad in (b'\xed\xa0\x80', b'\xff', b'\xc0\xaf', b'\xe2\x82'):
            corrupt = raw.replace(b'"p"', b'"' + bad + b'"', 1)
            self.refuses("record", A.decode, corrupt)

    def test_timestamp_bounds(self):
        for key in ("issued_at", "confirmed_at", "expires_at", "at"):
            for value in (0, -1, 2**63, 1 << 16384):
                r = grant()
                r["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
                (r["invalidation"] if key == "at" else r)[key] = value
                self.refuses("invalidation.at" if key == "at" else key, A.validate, r)
        for issued, expires in ((1, 86401), (2**63 - 86401, 2**63 - 1)):
            for confirmed in (1, 2**63 - 1):
                r = grant()
                r.update(issued_at=issued, confirmed_at=confirmed, expires_at=expires)
                self.assertEqual(A.decode(A.encode(r)), r)
                self.assertEqual(A.decode(A.encode(r)), r)
                self.assertEqual(A.evaluate(r, expires, MANDATE), ("expired", None))
                revoked = A.revoked(r, confirmed, "stop")
                self.assertEqual(revoked["invalidation"]["at"], confirmed)
        for field in (None, "scope", "confirmation", "invalidation"):
            r = grant()
            r["invalidation"] = {"at": 100, "kind": "revoked", "reason": "stop"}
            (r if field is None else r[field])[1 << 16384] = 0
            self.refuses(field or "record", A.validate, r)

    def test_json_grammar(self):
        r = grant()
        r["scope"]["plans"] = ["p", "q"]
        raw = json.dumps(r, separators=(",", ":")).encode()
        self.assertEqual(A.decode(raw), r)
        for fragment in (b'[1 2]', b'[1,]', b'{"x" 1}', b'{"x":}', b'{"x":1,}',
                         b'{"x":1 "y":2}', b'[truefalse]', b'01', b'"a":"b"',
                         b'NaN', b'Infinity', b'-Infinity'):
            self.refuses("record|scope", A.decode, raw.replace(b'["p","q"]', fragment))
        for suffix in (b' {}', b',null', b','):
            self.refuses("record", A.decode, raw + suffix)

    def test_duplicate_keys_anywhere_refuse(self):
        r = grant()
        r["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
        raw = json.dumps(r)
        for key, value in (("session_cap", 1), ("plans", ["p"]),
                           ("owner_response", r["confirmation"]["owner_response"]),
                           ("kind", "revoked")):
            pair = json.dumps(key) + ": " + json.dumps(value)
            duplicate = raw.replace(pair, pair + ", " + pair, 1).encode()
            self.refuses("duplicate JSON field", A.decode, duplicate)


if __name__ == "__main__":
    unittest.main()
