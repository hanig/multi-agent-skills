"""Acknowledgment: did the drain actually land?

Every intent used to be written {"applied": false} and nothing ever set it
true, so after a clean run all eight still read pending. The key permits
receiver-side deduplication but never makes a blind replay safe; the outbox
also could not answer the one question it exists to answer.

Sol's three corrections are what these tests pin, since each is a thing I had
wrong: append-only JSONL is not automatically crash-safe; status is derived
from a success receipt rather than stored as a boolean; and absence of a
receipt is `unacknowledged`, never "not applied".
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
sys.path.insert(0, str(SWARM.parent))
import swarm as S  # noqa: E402


_REAL_RECORD_RECEIPT = S.record_receipt


def _persist_intent(state_dir, key, op=None):
    """Give receipt tests the same persisted authority as a real drain."""
    normalized_key = str(key or "").strip()
    if not normalized_key:
        return None
    matches = [intent for intent in S.read_outbox(state_dir)
               if intent.get("key") == normalized_key]
    if matches:
        return matches[0]
    intent = {
        "key": normalized_key, "project": "fixture", "unit": "u",
        "verb": op or "close", "attempt_dir": None,
        "unit_state": "DONE", "why": "fixture",
        "evidence": {"fixture": True},
    }
    intent["envelope"] = S._intent_envelope(
        intent["project"], intent["unit"], None, intent["key"],
        intent["verb"], intent["evidence"])
    S._fsync_append(Path(state_dir) / S.OUTBOX, intent)
    return S.read_outbox(state_dir)[-1]


def _record_receipt(state_dir, key, ref, op=None, by=None, at=None,
                    source="receiver_readback"):
    """Build the complete confirmed observation used by journal tests."""
    intent = _persist_intent(state_dir, key, op)
    observation = None
    if source is not None and intent is not None:
        envelope = intent["envelope"]
        observation = {
            "schema_version": S.OBSERVATION_SCHEMA_VERSION,
            "project": envelope["project"], "unit": envelope["unit"],
            "attempt": envelope["attempt"],
            "idempotency_key": envelope["idempotency_key"],
            "requested_operation": envelope["requested_operation"],
            "evidence_digest": envelope["evidence_digest"],
            "connector_capability":
                envelope["required_connector_capability"],
            "outcome": S.RECEIPT_CONFIRMED, "source": source,
            "matched": True, "reference": ref, "by": by,
        }
    return _REAL_RECORD_RECEIPT(state_dir, key, ref, op=op,
                                by=by, at=at, observation=observation)


class TestStatusIsDerivedNotStored(unittest.TestCase):

    def test_no_receipt_is_unacknowledged_not_not_applied(self):
        with tempfile.TemporaryDirectory() as d:
            st, problems = S.acknowledgment_status(d)
            self.assertEqual(st, {})
            self.assertEqual(problems, [])

    def test_a_receipt_makes_the_key_acknowledged(self):
        with tempfile.TemporaryDirectory() as d:
            receipt = _record_receipt(d, "k1", "ARC-1")
            self.assertEqual(receipt["outcome"], "confirmed_by_readback")
            st, _ = S.acknowledgment_status(d)
            self.assertEqual(st["k1"][0], S.ACKNOWLEDGED)

    def test_the_same_ref_twice_stays_acknowledged(self):
        """Repeating a local receipt must not manufacture a conflict from an
        equivalent receiver observation."""
        with tempfile.TemporaryDirectory() as d:
            _record_receipt(d, "k1", "ARC-1")
            _record_receipt(d, "k1", "ARC-1")
            st, _ = S.acknowledgment_status(d)
            self.assertEqual(st["k1"][0], S.ACKNOWLEDGED)

    def test_writer_refuses_a_second_ref_for_one_key(self):
        with tempfile.TemporaryDirectory() as d:
            _record_receipt(d, "k1", "ARC-1")
            with self.assertRaises(S.OutboxError):
                _record_receipt(d, "k1", "ARC-2")
            st, _ = S.acknowledgment_status(d)
            self.assertEqual(st["k1"][0], S.ACKNOWLEDGED)
            self.assertEqual([r["ref"] for r in st["k1"][1]], ["ARC-1"])

    def test_intents_no_longer_carry_the_misleading_applied_field(self):
        src = SWARM.read_text()
        i = src.index("def emit_intent")
        j = src.index("def read_outbox")
        self.assertNotIn('"applied": False', src[i:j],
                         "a permanently-false field reads as 'not filed' "
                         "when the truth is 'this machine does not know'")


class TestReceiptAdmissionIsAtomic(unittest.TestCase):

    def test_concurrent_drainers_cannot_admit_two_references(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            _persist_intent(d, "k1")
            program = (
                "import sys,time; sys.path.insert(0,sys.argv[1]); "
                "import swarm as S; start=float(sys.argv[3]); "
                "\nwhile time.time()<start: pass"
                "\ntry: S.record_receipt(sys.argv[2],'k1',sys.argv[4])"
                "\nexcept S.OutboxError: sys.exit(23)"
            )
            start = time.time() + 0.5
            processes = [subprocess.Popen(
                [sys.executable, "-c", program, str(SWARM.parent), d,
                 str(start), "ARC-%d" % (index % 2)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for index in range(8)]
            for process in processes:
                process.communicate(timeout=20)
            returncodes = [process.returncode for process in processes]
            records, problems = S.load_acknowledgments(d)
            self.assertEqual(problems, [])
            self.assertEqual(len({record["ref"] for record in records}), 1)
            self.assertEqual(len(records), 1)
            self.assertIn(0, returncodes)
            self.assertIn(23, returncodes)


class TestJournalDurability(unittest.TestCase):
    """Sol: append-only JSONL is NOT automatically crash-safe. A process can
    die having written half a line."""

    def _path(self, d):
        return Path(d) / S.RECEIPTS

    def test_a_truncated_final_line_is_dropped_and_reported(self):
        with tempfile.TemporaryDirectory() as d:
            _record_receipt(d, "k1", "ARC-1")
            with open(self._path(d), "a") as fh:
                fh.write('{"key": "k2", "ref": "ARC-2"')   # no newline, cut
            j = S._read_receipts_raw(d)
            recs, problems = j._records, j.problems
            self.assertEqual([r["key"] for r in recs], ["k1"])
            self.assertEqual([p["kind"] for p in problems], ["truncated_tail"])
            self.assertEqual(S.fatal_problems(problems), [],
                             "an interrupted write is recoverable")

    def test_corruption_mid_journal_is_not_silently_skipped(self):
        """Skipping a bad middle line is how a missing acknowledgment turns
        into a false one."""
        with tempfile.TemporaryDirectory() as d:
            _record_receipt(d, "k1", "ARC-1")
            # Append the bad line and the following good one WITHOUT
            # record_receipt: it now refuses to extend a broken journal, which
            # is the point. This fixture builds the damaged state directly.
            with open(self._path(d), "a") as fh:
                fh.write("NOT JSON\n")
                fh.write('{"key": "k3", "ref": "ARC-3", "attested": true}\n')
            problems = S._read_receipts_raw(d).problems
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
                _record_receipt(d, "k%d" % n, "ARC-%d" % n)
            j = S._read_receipts_raw(d)
            recs, problems = j._records, j.problems
            self.assertEqual(len(recs), 5)
            self.assertEqual(problems, [])

    def test_a_complete_json_record_without_newline_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            first = _record_receipt(d, "k1", "ARC-1")
            path = Path(d) / S.RECEIPTS
            path.write_text(json.dumps(first))
            _record_receipt(d, "k2", "ARC-2")
            records, problems = S.load_acknowledgments(d)
            self.assertEqual([record["key"] for record in records],
                             ["k1", "k2"])
            self.assertEqual(problems, [])


class TestReceiptsRequireAKnownIntent(unittest.TestCase):

    def _outbox(self, d, key="abc"):
        os.makedirs(d, exist_ok=True)
        with open(Path(d) / S.OUTBOX, "w") as fh:
            fh.write(json.dumps({
                "key": key, "project": "p", "verb": "close", "unit": "u1",
                "unit_state": "DONE", "why": "w", "evidence": {"x": 1}}) + "\n")

    def _args(self, d, **kw):
        class A:
            state_dir = d
            all = False
            json = False
            record_receipt = None
            ref = None
            op = None
            source = None
            matched = False
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
            self.assertEqual(st["abc"][0], S.ATTESTED_UNSPECIFIED)

    def test_flags_cannot_synthesize_the_stronger_grade(self):
        with tempfile.TemporaryDirectory() as d:
            self._outbox(d)
            rc = S.cmd_outbox(self._args(
                d, record_receipt="abc", ref="ARC-171",
                source="receiver_readback", matched=True))
            self.assertEqual(rc, S.EXIT_USAGE)
            self.assertFalse((Path(d) / S.RECEIPTS).exists())

    def test_historical_cli_form_still_records_the_weaker_grade(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            self._outbox(d)
            result = subprocess.run(
                [sys.executable, str(SWARM), "outbox", "--state-dir", d,
                 "--record-receipt", "abc", "--ref", "ARC-171"],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, S.EXIT_OK, result.stderr)
            status, _ = S.acknowledgment_status(d)
            self.assertEqual(status["abc"][0], S.ATTESTED_UNSPECIFIED)

    def test_help_routes_confirmed_observations_to_the_contract_cli(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(SWARM), "outbox", "--help"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("drain_contract.py", result.stdout)
        self.assertNotIn("receipt when --source", result.stdout)

    def test_a_conflict_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            self._outbox(d)
            first = _record_receipt(d, "abc", "ARC-1")
            second = dict(first, ref="ARC-2")
            S._fsync_append(Path(d) / S.RECEIPTS, second)
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
                    _record_receipt(d, "k1", "ARC-1")
            self.assertIn("serialise", str(c.exception))

    def test_corruption_fails_closed_rather_than_reporting_survivors(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.OUTBOX, "w") as fh:
                for k in ("k1", "k2"):
                    fh.write(json.dumps({
                        "key": k, "project": "p", "verb": "close",
                        "unit": "u", "why": "w",
                        "unit_state": "DONE", "evidence": {"x": 1}}) + "\n")
            _record_receipt(d, "k1", "ARC-1")
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
            rec = _record_receipt(d, "k1", "ARC-1")
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
                "key": "k1", "project": "p", "verb": "close",
                "unit": "u", "why": "w",
                "unit_state": "DONE", "evidence": {"x": 1}}) + "\n")
        _record_receipt(d, "k1", "ARC-1", source=None)

    def test_the_status_value_itself_says_attested(self):
        with tempfile.TemporaryDirectory() as d:
            self._fixture(d)
            st, _ = S.acknowledgment_status(d)
            self.assertEqual(st["k1"][0], S.ATTESTED_UNSPECIFIED)

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
            self.assertEqual(payload["intents"][0]["ack_status"],
                             S.ATTESTED_UNSPECIFIED)
            self.assertIn("Neither is independently verified tracker state",
                          payload["note"])


class TestRoundThreeFindings(unittest.TestCase):
    """Four more MAJOR findings, all real. Every one of them let a journal
    that could not be read in full still produce a comfortable answer."""

    def _intent(self, d, key="k1"):
        with open(Path(d) / S.OUTBOX, "w") as fh:
            fh.write(json.dumps({
                "key": key, "project": "p", "verb": "close", "unit": "u",
                "why": "w",
                "unit_state": "DONE", "evidence": {"x": 1}}) + "\n")

    def _args(self, d, **kw):
        class A:
            state_dir = d
            all = True
            json = False
            record_receipt = None
            ref = None
            op = None
            source = None
            matched = False
        a = A()
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_a_complete_but_malformed_last_line_is_corruption(self):
        """splitlines() cannot tell an interrupted write from a finished one.
        A trailing newline means the line was written in full."""
        with tempfile.TemporaryDirectory() as d:
            _record_receipt(d, "k1", "ARC-1")
            with open(Path(d) / S.RECEIPTS, "a") as fh:
                fh.write("NOT JSON\n")           # note: complete line
            problems = S._read_receipts_raw(d).problems
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
            j = S._read_receipts_raw(d)
            recs, problems = j._records, j.problems
            self.assertEqual(recs, [])
            self.assertEqual([p["kind"] for p in problems], ["malformed"])

    def test_lifecycle_completion_is_not_an_acknowledgment_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.RECEIPTS, "w") as fh:
                fh.write(json.dumps({
                    "key": "k1", "ref": "a2a-task-1", "attested": True,
                    "outcome": "asynchronously_completed",
                    "source": "a2a_lifecycle", "schema_version": 2,
                }) + "\n")
            journal = S._read_receipts_raw(d)
            self.assertEqual(journal._records, [])
            self.assertEqual([p["kind"] for p in journal.problems],
                             ["malformed"])

    def test_explicit_lifecycle_source_is_rejected_on_legacy_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.RECEIPTS, "w") as fh:
                fh.write(json.dumps({
                    "key": "k1", "ref": "a2a-task-1", "attested": True,
                    "source": "a2a_lifecycle", "schema_version": 1,
                }) + "\n")
            journal = S._read_receipts_raw(d)
            self.assertEqual(journal._records, [])
            self.assertEqual([p["kind"] for p in journal.problems],
                             ["malformed"])

    def test_legacy_schema_cannot_claim_the_confirmed_grade(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.RECEIPTS, "w") as fh:
                fh.write(json.dumps({
                    "key": "k1", "ref": "ARC-1", "attested": True,
                    "outcome": S.CONFIRMED_BY_READBACK,
                    "source": S.RECEIVER_READBACK, "matched": True,
                    "schema_version": 1,
                }) + "\n")
            journal = S._read_receipts_raw(d)
            self.assertEqual(journal._records, [])
            self.assertEqual([p["kind"] for p in journal.problems],
                             ["malformed"])

    def test_boolean_is_not_a_legacy_receipt_schema(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.RECEIPTS, "w") as fh:
                fh.write(json.dumps({
                    "key": "k1", "ref": "ARC-1", "attested": True,
                    "schema_version": True,
                }) + "\n")
            journal = S._read_receipts_raw(d)
            self.assertEqual(journal._records, [])
            self.assertEqual([p["kind"] for p in journal.problems],
                             ["malformed"])

    def test_lifecycle_completion_cannot_enter_through_the_writer(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(S.OutboxError):
                _record_receipt(d, "k1", "a2a-task-1",
                                 source="a2a_lifecycle")
            self.assertFalse((Path(d) / S.RECEIPTS).exists())

    def test_legacy_key_and_ref_form_is_a_weaker_attestation(self):
        with tempfile.TemporaryDirectory() as d:
            self._intent(d)
            rec = _REAL_RECORD_RECEIPT(d, "k1", "ARC-1")
            self.assertIsNone(rec["outcome"])
            status, _ = S.acknowledgment_status(d)
            self.assertEqual(status["k1"][0], S.ATTESTED_UNSPECIFIED)

    def test_invalid_evidence_less_close_cannot_mint_a_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.OUTBOX, "w") as fh:
                fh.write(json.dumps({
                    "key": "k1", "project": "p", "unit": "u",
                    "verb": "close", "attempt_dir": None,
                    "evidence": None,
                }) + "\n")
            with self.assertRaises(S.OutboxError):
                _REAL_RECORD_RECEIPT(d, "k1", "ARC-1")
            self.assertFalse((Path(d) / S.RECEIPTS).exists())

    def test_an_unreadable_journal_fails_closed(self):
        """Keying the failure on the word 'corruption' meant an OSError
        matched nothing and the command exited zero.

        A real unreadable journal (a directory where a file belongs) rather
        than a mock, so this also pins that absence and unreadability are
        told apart by errno instead of by is_file().
        """
        with tempfile.TemporaryDirectory() as d:
            self._intent(d)
            os.makedirs(Path(d) / S.RECEIPTS)
            problems = S._read_receipts_raw(d).problems
            self.assertEqual([p["kind"] for p in problems], ["unreadable"])
            self.assertTrue(S.fatal_problems(problems))

    def test_an_absent_journal_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(S._read_receipts_raw(d).problems, [])

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
    WRITER = "_append_receipt_atomic"

    def _analyse(self):
        """(name, lineno) -> (references_journal, calls_chokepoint, calls_raw).

        Two corrections a reviewer earned, both reachable by an honest
        maintainer rather than only by someone smuggling:

        - Keyed by name AND line. Keying by name alone meant `Second.get`
          overwrote `First.get`, so an offending method vanished behind a
          well-behaved namesake.
        - Nested definitions are NOT credited to their parent. Walking the
          whole subtree let an enclosing function that reads RECEIPTS look
          compliant because some inner helper called the chokepoint.

        WHAT THIS TEST IS. A lint against forgetting, not a proof. It reads
        literal names, so an alias, a lambda, a getattr or a same-named method
        on an unrelated object all slip past, and a reviewer demonstrated
        every one of them. Chasing those is an arms race no static check over
        a shared namespace wins. The real barrier against accidental misuse is
        `_RawJournal`, which does not hand over records at all; this test
        catches the ordinary slip of adding a function that reads the journal
        and forgets the gate.
        """
        import ast
        tree = ast.parse(SWARM.read_text())
        out = {}
        # Lambdas get their OWN entry. Excluding them from a parent's credits
        # without listing them separately meant a journal read inside a lambda
        # was attributed to nobody and the lint missed it entirely, which the
        # old whole-subtree walk did at least flag. glm-5.3 caught the
        # regression; it is small, and it is still a regression.
        defs = [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda))]
        for node in defs:
            nested = set()
            for sub in ast.walk(node):
                if sub is node:
                    continue
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.Lambda)):
                    nested.update(id(x) for x in ast.walk(sub))
            refs = calls_choke = calls_raw = False
            for sub in ast.walk(node):
                if id(sub) in nested:
                    continue
                if isinstance(sub, ast.Name) and sub.id == "RECEIPTS":
                    refs = True
                elif isinstance(sub, ast.Call):
                    fn = sub.func
                    name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                    if name == self.CHOKEPOINT:
                        calls_choke = True
                    elif name == self.RAW:
                        calls_raw = True
            name = getattr(node, "name", "<lambda>")
            out[(name, node.lineno)] = (refs, calls_choke, calls_raw)
        return out

    def test_every_journal_toucher_goes_through_the_chokepoint(self):
        """Not an allowlist: a rule. Touch the journal, accept the refusal.
        A function added tomorrow either calls the chokepoint or fails."""
        offenders = sorted(
            name for (name, _line), (refs, choke, _) in
            self._analyse().items()
            if refs and not choke and name not in (
                self.RAW, self.CHOKEPOINT, self.WRITER))
        self.assertEqual(offenders, [],
                         "these reach the receipt journal without accepting "
                         "its refusal, which is how one cause produced four "
                         "separate bugs: %s" % offenders)

    def test_nothing_calls_the_raw_reader_except_the_chokepoint(self):
        callers = sorted(name for (name, _line), (_, _, raw) in
                         self._analyse().items()
                         if raw and name != self.RAW)
        self.assertEqual(callers, [self.CHOKEPOINT],
                         "the raw reader reports problems; it does not "
                         "refuse. Anything deciding on it must go through "
                         "the chokepoint, got: %s" % callers)

    def test_atomic_writer_is_the_only_admission_exception(self):
        """The writer must inspect and append while holding one lock."""
        import ast
        fn = next(node for node in ast.parse(SWARM.read_text()).body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == self.WRITER)
        calls = {}
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = (getattr(node.func, "id", None)
                    or getattr(node.func, "attr", None))
            if name in ("flock", "_parse_receipt_text", "write", "fsync"):
                calls.setdefault(name, node.lineno)
        self.assertLess(calls["flock"], calls["_parse_receipt_text"])
        self.assertLess(calls["_parse_receipt_text"], calls["write"])
        self.assertLess(calls["write"], calls["fsync"])

    def test_the_records_are_not_reachable_without_the_refusal(self):
        """The real barrier. `_RawJournal` hands back no records at all, so
        a caller cannot obtain them by forgetting to check; it has to reach
        into a private attribute on purpose."""
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / S.RECEIPTS, "w") as fh:
                fh.write("NOT JSON\n")
            box = S._read_receipts_raw(d)
            self.assertFalse(hasattr(box, "records"))
            self.assertTrue(box.problems)
            with self.assertRaises(S.OutboxError):
                S.load_acknowledgments(d)

    def test_a_nested_helper_does_not_launder_its_parent(self):
        """An enclosing function that reads the journal must not look
        compliant because some inner function calls the chokepoint."""
        import ast
        src = ("def bypass(state_dir, name=RECEIPTS):\n"
               "    def helper():\n"
               "        load_acknowledgments(state_dir)\n"
               "    return (Path(state_dir) / name).read_text()\n")
        node = ast.parse(src).body[0]
        nested = set()
        for sub in ast.walk(node):
            if sub is node:
                continue
            if isinstance(sub, (ast.FunctionDef, ast.Lambda)):
                nested.update(id(x) for x in ast.walk(sub))
        choke = any(isinstance(sub, ast.Call)
                    and getattr(sub.func, "id", None) == self.CHOKEPOINT
                    for sub in ast.walk(node) if id(sub) not in nested)
        self.assertFalse(choke, "the parent was credited with its child's "
                                "call to the chokepoint")

    def test_a_lambda_is_attributed_to_itself_not_to_nobody(self):
        """Excluding lambdas from a parent's credits without giving them
        entries of their own left a hole the previous version did not have."""
        import ast
        tree = ast.parse("f = lambda d: (Path(d) / RECEIPTS).read_text()\n")
        lams = [n for n in ast.walk(tree) if isinstance(n, ast.Lambda)]
        self.assertEqual(len(lams), 1)
        self.assertTrue(any(isinstance(sub, ast.Name) and sub.id == "RECEIPTS"
                            for sub in ast.walk(lams[0])))

    def test_same_named_functions_do_not_hide_each_other(self):
        keys = list(self._analyse())
        self.assertEqual(len(keys), len(set(keys)),
                         "keying by name alone lets a compliant namesake "
                         "overwrite an offender")

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
                _record_receipt(d, "k1", "ARC-1")

    def test_a_healthy_journal_passes_through(self):
        with tempfile.TemporaryDirectory() as d:
            _record_receipt(d, "k1", "ARC-1")
            recs, problems = S.load_acknowledgments(d)
            self.assertEqual(len(recs), 1)
            self.assertEqual(problems, [])


class TestARefIsRequiredWhereItIsWritten(unittest.TestCase):
    """The CLI checked truthiness; a direct caller and a whitespace-only ref
    both walked past it and poisoned the journal later."""

    def test_a_none_ref_is_refused_at_the_writer(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(S.OutboxError):
                _record_receipt(d, "k1", None)

    def test_a_whitespace_ref_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(S.OutboxError):
                _record_receipt(d, "k1", "   ")

    def test_an_empty_key_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(S.OutboxError):
                _record_receipt(d, "  ", "ARC-1")

    def test_a_ref_is_stored_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            rec = _record_receipt(d, " k1 ", "  ARC-1  ")
            self.assertEqual((rec["key"], rec["ref"]), ("k1", "ARC-1"))


class TestAppendingAfterACrashDoesNotDestroyTwoRecords(unittest.TestCase):
    """A truncated tail is recoverable on its own. Appending onto it made one
    malformed line and took the next receipt down with it."""

    def test_a_half_written_tail_is_dropped_before_appending(self):
        with tempfile.TemporaryDirectory() as d:
            _record_receipt(d, "k1", "ARC-1")
            with open(Path(d) / S.RECEIPTS, "a") as fh:
                fh.write('{"key": "k2", "ref": "ARC')     # crash mid-write
            _record_receipt(d, "k3", "ARC-3")
            recs, problems = S.load_acknowledgments(d)
            self.assertEqual(sorted(r["key"] for r in recs), ["k1", "k3"])
            self.assertEqual(problems, [],
                             "the interrupted write should be gone, not "
                             "fused to the record that followed it")

    def test_a_clean_journal_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            _record_receipt(d, "k1", "ARC-1")
            before = (Path(d) / S.RECEIPTS).read_bytes()
            _record_receipt(d, "k2", "ARC-2")
            after = (Path(d) / S.RECEIPTS).read_bytes()
            self.assertTrue(after.startswith(before))

    def test_a_journal_that_is_only_a_partial_line_is_emptied(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d, exist_ok=True)
            with open(Path(d) / S.RECEIPTS, "w") as fh:
                fh.write('{"key": "k1"')
            _record_receipt(d, "k2", "ARC-2")
            recs, problems = S.load_acknowledgments(d)
            self.assertEqual([r["key"] for r in recs], ["k2"])
            self.assertEqual(problems, [])


class TestHealingHappensUnderTheLock(unittest.TestCase):
    """Healing ran BEFORE the lock was taken, so two writers could interleave:
    A reads a partial tail, B truncates it and appends, then A truncates using
    its stale view and deletes B's receipt. The repair for one crash ate a
    good record."""

    def test_the_lock_is_taken_before_anything_is_truncated(self):
        """Compares CALL positions, not text.

        The first version searched the unparsed source for the words, and the
        docstring explaining the race says "truncates" before any code runs,
        so it failed on prose. That is the guard-matching-its-own-message bug
        appearing inside a test written to prevent it, which is funny once and
        instructive twice.
        """
        import ast
        fn = next(n for n in ast.parse(SWARM.read_text()).body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_fsync_append")
        calls = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = (getattr(node.func, "attr", None)
                        or getattr(node.func, "id", None))
                if name in ("flock", "_heal_jsonl_tail"):
                    calls.setdefault(name, node.lineno)
        self.assertIn("flock", calls)
        self.assertIn("_heal_jsonl_tail", calls)
        self.assertLess(calls["flock"], calls["_heal_jsonl_tail"],
                        "the tail is repaired before the lock is held, so a "
                        "concurrent writer's receipt can be truncated away")

    def test_there_is_no_unlocked_healing_helper(self):
        self.assertFalse(hasattr(S, "_heal_truncated_tail"),
                         "a standalone healer can be called without the lock")

    def test_healing_still_works_through_the_locked_path(self):
        with tempfile.TemporaryDirectory() as d:
            _record_receipt(d, "k1", "ARC-1")
            with open(Path(d) / S.RECEIPTS, "a") as fh:
                fh.write('{"key": "k2", "ref": "ARC')
            _record_receipt(d, "k3", "ARC-3")
            recs, problems = S.load_acknowledgments(d)
            self.assertEqual(sorted(r["key"] for r in recs), ["k1", "k3"])
            self.assertEqual(problems, [])


class TestDoneCodeDrain(unittest.TestCase):
    def _receipt(self):
        return {
            "unit": "u", "repo": "git@github.com:hanig/private.git",
            "pr": "https://github.com/hanig/private/pull/1",
            "target": "main", "head": "a" * 40,
            "merged_as": "b" * 40, "method": "merge",
            "merged": True, "attested": True,
        }

    def test_done_code_emits_close_with_its_bound_merge_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            receipt = self._receipt()
            us = {
                "attempt_dir": "/runs/u/attempt-1",
                "attempt_produced_heads": {"attempt-1": receipt["head"]},
                "merged_as": receipt["merged_as"],
                "merge_pr": receipt["pr"], "merge_receipt": receipt,
            }
            S.emit_intent(d, "p", "u", "DONE", us,
                          evidence={"receipt": receipt}, kind="code")
            intent = S.read_outbox(d)[0]
            self.assertEqual(intent["verb"], "close")
            self.assertEqual(intent["closing_evidence"], "merged_pr")
            self.assertEqual(intent["evidence"]["receipt"], receipt)

    def test_code_close_without_bound_merge_evidence_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            S.emit_intent(d, "p", "u", "DONE",
                          {"attempt_dir": "/runs/u/attempt-1"},
                          evidence=None, kind="code")
            intents = S.read_outbox(d)
            self.assertFalse(any(i["verb"] == "close" for i in intents))
            self.assertEqual(intents[0]["verb"], "open_pr")

    def test_old_open_pr_key_does_not_suppress_the_bound_close(self):
        with tempfile.TemporaryDirectory() as d:
            attempt = "/runs/u/attempt-1"
            S.emit_intent(d, "p", "u", "DONE",
                          {"attempt_dir": attempt}, evidence=None,
                          kind="code")
            receipt = self._receipt()
            us = {
                "attempt_dir": attempt,
                "attempt_produced_heads": {"attempt-1": receipt["head"]},
                "merged_as": receipt["merged_as"],
                "merge_pr": receipt["pr"], "merge_receipt": receipt,
            }
            S.emit_intent(d, "p", "u", "DONE", us,
                          evidence={"receipt": receipt}, kind="code")
            intents = S.read_outbox(d)
            self.assertEqual([i["verb"] for i in intents],
                             ["open_pr", "close"])
            self.assertNotEqual(intents[0]["key"], intents[1]["key"])

if __name__ == "__main__":
    unittest.main()
