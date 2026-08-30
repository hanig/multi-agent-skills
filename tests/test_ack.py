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
            recs, problems = S._read_receipts_raw(d)
            self.assertEqual([r["key"] for r in recs], ["k1"])
            self.assertEqual([p["kind"] for p in problems], ["truncated_tail"])
            self.assertEqual(S.fatal_problems(problems), [],
                             "an interrupted write is recoverable")

    def test_corruption_mid_journal_is_not_silently_skipped(self):
        """Skipping a bad middle line is how a missing acknowledgment turns
        into a false one."""
        with tempfile.TemporaryDirectory() as d:
            S.record_receipt(d, "k1", "ARC-1")
            # Append the bad line and the following good one WITHOUT
            # record_receipt: it now refuses to extend a broken journal, which
            # is the point. This fixture builds the damaged state directly.
            with open(self._path(d), "a") as fh:
                fh.write("NOT JSON\n")
                fh.write('{"key": "k3", "ref": "ARC-3", "attested": true}\n')
            _, problems = S._read_receipts_raw(d)
            kinds = [p["kind"] for p in problems]
            self.assertIn("corrupt", kinds)
            self.assertNotIn("truncated_tail", kinds)
            self.assertTrue(S.fatal_problems(problems))

    def test_the_write_is_fsynced(self):
        src = SWARM.read_text()
        i = src.index("def _fsync_append")
        j = src.index("def _read_receipts_raw")
        seg = src[i:j]
        self.assertIn("os.fsync", seg)
        self.assertIn("LOCK_EX", seg)

    def test_records_survive_a_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            for n in range(5):
                S.record_receipt(d, "k%d" % n, "ARC-%d" % n)
            recs, problems = S._read_receipts_raw(d)
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
                fh.write('{"key": "k2", "ref": "ARC-2", "attested": true}\n')

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


class TestTheWireValueCarriesTheWeakness(unittest.TestCase):
    """Round 2: I relabelled only the text output, so --json still said
    "acknowledged" and a machine consumer read an attestation as verified."""

    def _fixture(self, d):
        with open(Path(d) / S.OUTBOX, "w") as fh:
            fh.write(json.dumps({
                "key": "k1", "verb": "close", "unit": "u", "why": "w",
                "unit_state": "DONE", "evidence": {"x": 1}}) + "\n")
        S.record_receipt(d, "k1", "ARC-1")

    def test_the_status_value_itself_says_attested(self):
        with tempfile.TemporaryDirectory() as d:
            self._fixture(d)
            st, _ = S.acknowledgment_status(d)
            self.assertEqual(st["k1"][0], "attested")

    def test_no_output_path_ever_says_acknowledged(self):
        src = SWARM.read_text()
        i = src.index("UNACKNOWLEDGED = ")
        j = src.index("def _status_rows")
        self.assertNotIn('"acknowledged"', src[i:j],
                         "a consumer reading 'acknowledged' would take an "
                         "attestation for verified tracker state")

    def test_json_carries_the_caveat_not_just_the_value(self):
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as d:
            self._fixture(d)

            class A:
                state_dir = d
                all = True
                json = True
                record_receipt = None
                ref = None
                op = None
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                S.cmd_outbox(A())
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["intents"][0]["ack_status"], "attested")
            self.assertIn("not verified tracker state", payload["note"])


class TestRoundThreeFindings(unittest.TestCase):
    """Four more MAJOR findings, all real. Every one of them let a journal
    that could not be read in full still produce a comfortable answer."""

    def _intent(self, d, key="k1"):
        with open(Path(d) / S.OUTBOX, "w") as fh:
            fh.write(json.dumps({
                "key": key, "verb": "close", "unit": "u", "why": "w",
                "unit_state": "DONE", "evidence": {"x": 1}}) + "\n")

    def _args(self, d, **kw):
        class A:
            state_dir = d
            all = True
            json = False
            record_receipt = None
            ref = None
            op = None
        a = A()
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_a_complete_but_malformed_last_line_is_corruption(self):
        """splitlines() cannot tell an interrupted write from a finished one.
        A trailing newline means the line was written in full."""
        with tempfile.TemporaryDirectory() as d:
            S.record_receipt(d, "k1", "ARC-1")
            with open(Path(d) / S.RECEIPTS, "a") as fh:
                fh.write("NOT JSON\n")           # note: complete line
            _, problems = S._read_receipts_raw(d)
            self.assertEqual([p["kind"] for p in problems], ["corrupt"])

    def test_record_receipt_refuses_to_append_to_a_bad_journal(self):
        with tempfile.TemporaryDirectory() as d:
            self._intent(d)
            with open(Path(d) / S.RECEIPTS, "w") as fh:
                fh.write("NOT JSON\n")
            rc = S.cmd_outbox(self._args(d, record_receipt="k1",
                                         ref="ARC-1"))
            self.assertEqual(rc, S.EXIT_CONFLICT)

    def test_valid_json_is_not_automatically_a_valid_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.RECEIPTS, "w") as fh:
                fh.write(json.dumps({"key": "k1", "ref": "ARC-1",
                                     "attested": False}) + "\n")
            recs, problems = S._read_receipts_raw(d)
            self.assertEqual(recs, [])
            self.assertEqual([p["kind"] for p in problems], ["malformed"])

    def test_an_unreadable_journal_fails_closed(self):
        """Keying the failure on the word 'corruption' meant an OSError
        matched nothing and the command exited zero."""
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as d:
            self._intent(d)
            S.record_receipt(d, "k1", "ARC-1")
            with mock.patch.object(Path, "read_text",
                                   side_effect=OSError("EIO")):
                _, problems = S._read_receipts_raw(d)
                self.assertEqual([p["kind"] for p in problems], ["unreadable"])
                self.assertTrue(S.fatal_problems(problems))

    def test_fatal_problems_is_structural_not_textual(self):
        src = SWARM.read_text()
        i = src.index("def fatal_problems")
        j = src.index("def record_receipt")
        self.assertNotIn('"corruption"', src[i:j],
                         "a guard that greps its own prose breaks when the "
                         "prose changes")


class TestTheRefusalCannotBeBypassed(unittest.TestCase):
    """Root cause, after the gate refused a fourth review round.

    Three rounds found the same defect in four places. They were not four
    bugs: detection lived in the reader and the decision to refuse lived in
    each caller, so every caller could be wrong separately and every NEW
    caller got a fresh chance to be wrong. Patching instance five would have
    changed nothing.

    So the omission is now unrepresentable rather than detectable, and this
    test checks architecture instead of wording.
    """

    CHOKEPOINT = "load_acknowledgments"
    RAW = "_read_receipts_raw"

    def _bodies(self):
        """Function name -> source of its BODY only.

        The body, not the whole def, because a function's own name appears in
        its signature and would match itself.
        """
        import ast
        tree = ast.parse(SWARM.read_text())
        out = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[node.name] = "\n".join(ast.unparse(b) for b in node.body)
        return out

    def test_every_journal_toucher_goes_through_the_chokepoint(self):
        """The invariant is not an allowlist. It is: touch the journal, and
        you accept the refusal. A new function added tomorrow either calls
        the chokepoint or fails this test."""
        offenders = []
        for name, body in self._bodies().items():
            if name in (self.RAW, self.CHOKEPOINT):
                continue
            if "RECEIPTS" not in body:
                continue
            if self.CHOKEPOINT + "(" not in body:
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "these reach the receipt journal without accepting "
                         "its refusal, which is how one bug appeared in four "
                         "places: %s" % sorted(offenders))

    def test_nothing_calls_the_raw_reader_except_the_chokepoint(self):
        callers = [n for n, b in self._bodies().items()
                   if self.RAW + "(" in b and n != self.RAW]
        self.assertEqual(callers, [self.CHOKEPOINT],
                         "the raw reader reports problems; it does not "
                         "refuse. Anything deciding on it must go through "
                         "the chokepoint, got: %s" % sorted(callers))

    def test_reading_and_refusing_are_the_same_operation(self):
        """You cannot obtain the records without accepting the refusal."""
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.RECEIPTS, "w") as fh:
                fh.write("NOT JSON\n")
            with self.assertRaises(S.OutboxError):
                S.load_acknowledgments(d)
            with self.assertRaises(S.OutboxError):
                S.acknowledgment_status(d)
            with self.assertRaises(S.OutboxError):
                S.record_receipt(d, "k1", "ARC-1")

    def test_a_healthy_journal_passes_through(self):
        with tempfile.TemporaryDirectory() as d:
            S.record_receipt(d, "k1", "ARC-1")
            recs, problems = S.load_acknowledgments(d)
            self.assertEqual(len(recs), 1)
            self.assertEqual(problems, [])

if __name__ == "__main__":
    unittest.main()
