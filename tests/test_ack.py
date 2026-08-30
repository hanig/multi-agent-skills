"""Acknowledgment: did the drain actually land?

Every intent used to be written {"applied": false} and nothing ever set it
true, so after a clean run all eight still read pending. Re-draining is keyed
and safe, so this was never a correctness bug; it was worse in a quieter way,
because the outbox could not answer the one question it exists to answer.

Sol's three corrections are what these tests pin, since each is a thing I had
wrong: append-only JSONL is not automatically crash-safe; status is derived
from a success receipt rather than stored as a boolean; and absence of a
receipt is `unacknowledged`, never "not applied".
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
sys.path.insert(0, str(SWARM.parent))
import swarm as S  # noqa: E402


class TestStatusIsDerivedNotStored(unittest.TestCase):

    def test_no_receipt_is_unacknowledged_not_not_applied(self):
        with tempfile.TemporaryDirectory() as d:
            st, problems = S.acknowledgment_status(d)
            self.assertEqual(st, {})
            self.assertEqual(problems, [])

    def test_a_receipt_makes_the_key_acknowledged(self):
        with tempfile.TemporaryDirectory() as d:
            S.record_receipt(d, "k1", "ARC-1")
            st, _ = S.acknowledgment_status(d)
            self.assertEqual(st["k1"][0], S.ACKNOWLEDGED)

    def test_the_same_ref_twice_stays_acknowledged(self):
        """Re-draining is safe, so re-recording must not manufacture a
        conflict out of an idempotent repeat."""
        with tempfile.TemporaryDirectory() as d:
            S.record_receipt(d, "k1", "ARC-1")
            S.record_receipt(d, "k1", "ARC-1")
            st, _ = S.acknowledgment_status(d)
            self.assertEqual(st["k1"][0], S.ACKNOWLEDGED)

    def test_two_refs_for_one_key_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            S.record_receipt(d, "k1", "ARC-1")
            S.record_receipt(d, "k1", "ARC-2")
            st, _ = S.acknowledgment_status(d)
            self.assertEqual(st["k1"][0], S.CONFLICT)

    def test_intents_no_longer_carry_the_misleading_applied_field(self):
        src = SWARM.read_text()
        i = src.index("def emit_intent")
        j = src.index("def read_outbox")
        self.assertNotIn('"applied": False', src[i:j],
                         "a permanently-false field reads as 'not filed' "
                         "when the truth is 'this machine does not know'")


class TestJournalDurability(unittest.TestCase):
    """Sol: append-only JSONL is NOT automatically crash-safe. A process can
    die having written half a line."""

    def _path(self, d):
        return Path(d) / S.RECEIPTS

    def test_a_truncated_final_line_is_dropped_and_reported(self):
        with tempfile.TemporaryDirectory() as d:
            S.record_receipt(d, "k1", "ARC-1")
            with open(self._path(d), "a") as fh:
                fh.write('{"key": "k2", "ref": "ARC-2"')   # no newline, cut
            recs, problems = S.read_receipts(d)
            self.assertEqual([r["key"] for r in recs], ["k1"])
            self.assertTrue(any("truncated" in p for p in problems))

    def test_corruption_mid_journal_is_not_silently_skipped(self):
        """Skipping a bad middle line is how a missing acknowledgment turns
        into a false one."""
        with tempfile.TemporaryDirectory() as d:
            S.record_receipt(d, "k1", "ARC-1")
            with open(self._path(d), "a") as fh:
                fh.write("NOT JSON\n")
            S.record_receipt(d, "k3", "ARC-3")
            _, problems = S.read_receipts(d)
            self.assertTrue(any("corruption" in p for p in problems))
            self.assertFalse(any("truncated" in p for p in problems))

    def test_the_write_is_fsynced(self):
        src = SWARM.read_text()
        i = src.index("def _fsync_append")
        j = src.index("def read_receipts")
        seg = src[i:j]
        self.assertIn("os.fsync", seg)
        self.assertIn("LOCK_EX", seg)

    def test_records_survive_a_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            for n in range(5):
                S.record_receipt(d, "k%d" % n, "ARC-%d" % n)
            recs, problems = S.read_receipts(d)
            self.assertEqual(len(recs), 5)
            self.assertEqual(problems, [])


class TestReceiptsRequireAKnownIntent(unittest.TestCase):

    def _outbox(self, d, key="abc"):
        os.makedirs(d, exist_ok=True)
        with open(Path(d) / S.OUTBOX, "w") as fh:
            fh.write(json.dumps({
                "key": key, "verb": "close", "unit": "u1",
                "unit_state": "DONE", "why": "w", "evidence": {"x": 1}}) + "\n")

    def _args(self, d, **kw):
        class A:
            state_dir = d
            all = False
            json = False
            record_receipt = None
            ref = None
            op = None
        a = A()
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_an_unknown_key_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._outbox(d)
            rc = S.cmd_outbox(self._args(d, record_receipt="nope",
                                         ref="ARC-1"))
            self.assertEqual(rc, S.EXIT_USAGE)
            self.assertFalse((Path(d) / S.RECEIPTS).exists())

    def test_a_receipt_without_a_ref_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._outbox(d)
            rc = S.cmd_outbox(self._args(d, record_receipt="abc"))
            self.assertEqual(rc, S.EXIT_USAGE)

    def test_a_known_key_with_a_ref_is_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            self._outbox(d)
            rc = S.cmd_outbox(self._args(d, record_receipt="abc",
                                         ref="ARC-171"))
            self.assertEqual(rc, S.EXIT_OK)
            st, _ = S.acknowledgment_status(d)
            self.assertEqual(st["abc"][0], S.ACKNOWLEDGED)

    def test_a_conflict_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            self._outbox(d)
            S.record_receipt(d, "abc", "ARC-1")
            S.record_receipt(d, "abc", "ARC-2")
            self.assertEqual(S.cmd_outbox(self._args(d)), S.EXIT_CONFLICT)

class TestReviewFindings(unittest.TestCase):
    """Three MAJOR findings from the review panel, each of which was real.

    I had claimed the receipt establishes tracker success, that corruption is
    never silently skipped, and that writes are serialised. All three claims
    were stronger than the code.
    """

    def test_an_flock_failure_is_not_swallowed(self):
        """I wrote `except OSError: pass` with the comment 'the write still
        happens'. It does, WITHOUT the serialisation the caller was promised,
        and both concurrent writers then report success."""
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(S.fcntl, "flock",
                                   side_effect=OSError("nolock")):
                with self.assertRaises(S.OutboxError) as c:
                    S.record_receipt(d, "k1", "ARC-1")
            self.assertIn("serialise", str(c.exception))

    def test_corruption_fails_closed_rather_than_reporting_survivors(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.OUTBOX, "w") as fh:
                for k in ("k1", "k2"):
                    fh.write(json.dumps({
                        "key": k, "verb": "close", "unit": "u", "why": "w",
                        "unit_state": "DONE", "evidence": {"x": 1}}) + "\n")
            S.record_receipt(d, "k1", "ARC-1")
            with open(Path(d) / S.RECEIPTS, "a") as fh:
                fh.write("NOT JSON\n")
            S.record_receipt(d, "k2", "ARC-2")

            class A:
                state_dir = d
                all = True
                json = False
                record_receipt = None
                ref = None
                op = None
            self.assertEqual(S.cmd_outbox(A()), S.EXIT_CONFLICT)

    def test_the_record_does_not_claim_to_verify_the_tracker(self):
        """The coordinator has no network imports, so it cannot check that
        ARC-171 really closed. The label must carry that weakness."""
        with tempfile.TemporaryDirectory() as d:
            rec = S.record_receipt(d, "k1", "ARC-1")
            self.assertTrue(rec["attested"])
        doc = S.record_receipt.__doc__
        self.assertIn("NOT verified evidence", doc)
        self.assertIn("attested, never", doc)

if __name__ == "__main__":
    unittest.main()
