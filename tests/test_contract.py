#!/usr/bin/env python3
"""Tests for contract.py.

The one that matters is test_exit_zero_without_output_is_not_success: a job
that exits cleanly and produces nothing must NOT verify as done. Everything
else in the design follows from that distinction holding.

No cluster, no network, no Slurm required.

    python3 tests/test_contract.py
"""

import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "hanig-verified-workflow" / "scripts" / "contract.py"

PASS, RUNNING, FAILED, TECH, VIOLATED, PREEMPTED, INCOMPLETE = range(7)


def contract(*argv, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *argv],
                          capture_output=True, text=True, cwd=cwd)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "run"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def init(self, *extra, stamp=True):
        """stamp: move declared outputs to just after the contract, so tests
        about verdict logic are unaffected by the provenance rule. Coupling
        this to record_attempt made every test that skipped it
        timing-sensitive. Provenance tests pass stamp=False."""
        r = contract("init", str(self.run_dir), "--command", "echo hi", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        if stamp:
            self.touch_outputs()
        return r

    def touch_outputs(self):
        """Stamp declared outputs forward so they post-date the contract.

        Most tests create the output before init to keep setup readable, but the
        provenance rule (correctly) rejects artifacts older than the contract.
        Provenance tests control the ordering themselves instead."""
        cpath = self.run_dir / "contract.json"
        try:
            c = json.loads(cpath.read_text())
            decl = list(c.get("declared_outputs") or [])
            # Predicate targets count as evidence too, so they need stamping.
            for pred in (c.get("predicates") or []):
                if isinstance(pred, dict) and isinstance(pred.get("path"), str):
                    decl.append(pred["path"])
            base = time.mktime(time.strptime(c["created_at"],
                                             "%Y-%m-%dT%H:%M:%S%z"))
        except Exception:
            return
        now = base + 2
        for spec in decl:
            pth = Path(spec)
            if pth.exists():
                try:
                    os.utime(pth, (now, now))
                except OSError:
                    pass

    def record_attempt(self, run_dir=None, exit_code=0):
        """Record a TERMINAL run. A bare submission is deliberately not enough:
        gpt-5.6-sol showed that treating "was submitted" as "has terminated"
        lets a pending job plus a stale artifact pass."""
        d = Path(run_dir or self.run_dir)
        r = contract("record", str(d), "--exit-code", str(exit_code))
        self.assertEqual(r.returncode, 0, r.stderr)

    def record_submission_only(self, run_dir=None, job_id="999999"):
        """A submission with no terminal evidence -- must NOT be enough to pass.

        Stamped with this contract's id, so the test exercises "submitted but
        never terminated" rather than "belongs to another contract"."""
        d = Path(run_dir or self.run_dir)
        d.mkdir(parents=True, exist_ok=True)
        cid = None
        cpath = d / "contract.json"
        if cpath.exists():
            try:
                cid = json.loads(cpath.read_text()).get("contract_id")
            except (ValueError, OSError):
                cid = None
        with (d / "attempts.jsonl").open("a") as fh:
            fh.write(json.dumps({"contract_id": cid, "attempt": 1,
                                 "job_id": job_id,
                                 "submitted_at": "2026-01-01T00:00:00+0000",
                                 "sbatch_args": [], "host": "test"}) + "\n")


class TestInit(Base):
    def test_contract_is_written_and_wellformed(self):
        self.init("--output", str(self.tmp / "out.tsv"))
        c = json.loads((self.run_dir / "contract.json").read_text())
        self.assertEqual(c["schema_version"], 1)
        self.assertEqual(c["command"], "echo hi")
        self.assertTrue(c["predicates"])
        self.assertFalse(c["retrospective"])

    def test_refuses_contract_with_nothing_to_verify(self):
        r = contract("init", str(self.run_dir), "--command", "echo hi")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot verify anything", r.stderr)

    def test_retrospective_is_labeled_and_warned(self):
        r = self.init("--output", str(self.tmp / "o"), "--retrospective")
        self.assertIn("retrospective", r.stderr.lower())
        c = json.loads((self.run_dir / "contract.json").read_text())
        self.assertTrue(c["retrospective"])

    def test_input_identity_records_digest_for_small_files(self):
        f = self.tmp / "in.txt"
        f.write_text("hello")
        self.init("--output", str(self.tmp / "o"), "--input", str(f))
        c = json.loads((self.run_dir / "contract.json").read_text())
        rec = c["inputs"][0]
        self.assertEqual(rec["rung"], "content-digest")
        self.assertFalse(rec["weak"])

    def test_large_input_falls_back_to_weak_evidence(self):
        f = self.tmp / "big.bin"
        f.write_bytes(b"x" * (3 << 20))
        self.init("--output", str(self.tmp / "o"), "--input", str(f),
                  "--hash-limit-mb", "1")
        rec = json.loads((self.run_dir / "contract.json").read_text())["inputs"][0]
        self.assertEqual(rec["rung"], "prefix-digest")
        self.assertTrue(rec["weak"], "oversized inputs must be marked weak")

    def test_does_not_clobber_without_force(self):
        self.init("--output", str(self.tmp / "o"))
        r = contract("init", str(self.run_dir), "--command", "x",
                     "--output", str(self.tmp / "o"))
        self.assertNotEqual(r.returncode, 0)


class TestVerdicts(Base):
    def test_exit_zero_without_output_is_not_success(self):
        """The core claim. Nothing ran, nothing was produced -> not done."""
        self.init("--output", str(self.tmp / "never_written.tsv"))
        self.record_attempt()
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, TECH, r.stdout)
        self.assertIn("TECHNICALLY_COMPLETE", r.stdout)
        self.assertIn("NOT success", r.stdout)

    def test_all_predicates_met_is_pass(self):
        out = self.tmp / "out.tsv"
        self.init("--output", str(out))
        self.record_attempt()
        out.write_text("col\nvalue\n")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, PASS, r.stdout)
        self.assertIn("SCIENTIFIC_PASS", r.stdout)

    def test_empty_output_file_fails_min_size(self):
        """A zero-row table is the classic silent pipeline failure."""
        out = self.tmp / "out.tsv"
        self.init("--output", str(out))
        self.record_attempt()
        out.write_text("")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, TECH, r.stdout)

    def test_row_count_predicate_discriminates(self):
        out = self.tmp / "out.tsv"
        self.init("--output", str(out), "--predicate",
                  json.dumps({"kind": "min_lines", "path": str(out),
                              "lines": 100}))
        self.record_attempt()
        out.write_text("only\ntwo\n")
        self.assertEqual(contract("check", str(self.run_dir)).returncode, TECH)
        out.write_text("\n".join(str(i) for i in range(200)) + "\n")
        self.assertEqual(contract("check", str(self.run_dir)).returncode, PASS)

    def test_log_error_pattern_is_caught_despite_clean_exit(self):
        log = self.tmp / "job.log"
        out = self.tmp / "out.tsv"
        self.init("--output", str(out), "--predicate",
                  json.dumps({"kind": "log_matches", "path": str(log),
                              "pattern": "Traceback", "expect": False}))
        self.record_attempt()
        out.write_text("data\n")
        log.write_text("starting\nTraceback (most recent call last):\ncaught\n")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, TECH,
                         "a caught traceback must not verify as success")

    def test_command_predicate_escape_hatch(self):
        out = self.tmp / "out.tsv"
        self.init("--output", str(out), "--predicate",
                  json.dumps({"kind": "command", "run": "test -s " + str(out)}))
        self.record_attempt()
        out.write_text("x\n")
        self.assertEqual(contract("check", str(self.run_dir)).returncode, PASS)

    def test_input_drift_is_a_contract_violation(self):
        """Re-running against silently changed inputs is not a valid result."""
        inp = self.tmp / "in.txt"
        out = self.tmp / "out.tsv"
        inp.write_text("original")
        self.init("--output", str(out), "--input", str(inp))
        self.record_attempt()
        out.write_text("result\n")
        self.assertEqual(contract("check", str(self.run_dir)).returncode, PASS)
        inp.write_text("MUTATED")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, VIOLATED, r.stdout)
        self.assertIn("content changed", r.stdout)

    def test_verification_receipt_is_written_and_parseable(self):
        out = self.tmp / "out.tsv"
        self.init("--output", str(out))
        self.record_attempt()
        out.write_text("x\n")
        contract("check", str(self.run_dir))
        v = json.loads((self.run_dir / "verification.json").read_text())
        self.assertEqual(v["state"], "SCIENTIFIC_PASS")
        self.assertEqual(v["exit_code"], PASS)
        self.assertIn("checked_at", v)

    def test_json_mode_is_valid_json(self):
        out = self.tmp / "out.tsv"
        self.init("--output", str(out))
        self.record_attempt()
        out.write_text("x\n")
        r = contract("check", str(self.run_dir), "--json")
        json.loads(r.stdout)

    def test_missing_contract_errors_clearly(self):
        r = contract("check", str(self.tmp / "nonexistent"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no contract", r.stderr)


class TestPredicateRobustness(Base):
    """Regression: deepseek-v4-pro refuted the claim that predicate evaluation
    cannot raise. It was right -- check_predicate touched the filesystem
    unguarded, so a TOCTOU delete crashed the verifier and left no receipt."""

    def test_malformed_predicate_fails_without_crashing(self):
        out = self.tmp / "out.tsv"
        self.init("--output", str(out), "--predicate",
                  json.dumps({"kind": "min_lines", "path": str(out),
                              "lines": "not-a-number"}))
        out.write_text("x\n")
        self.record_attempt()
        r = contract("check", str(self.run_dir))
        self.assertIn(r.returncode, (TECH, VIOLATED), r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "verification.json").exists(),
                        "a receipt must be written even when a predicate is bad")

    def test_unreadable_path_fails_without_crashing(self):
        blocked = self.tmp / "blocked"
        blocked.mkdir()
        target = blocked / "out.tsv"
        target.write_text("data\n")
        self.init("--output", str(target))
        self.record_attempt()
        os.chmod(blocked, 0o000)
        try:
            r = contract("check", str(self.run_dir))
            self.assertNotIn("Traceback", r.stderr)
            self.assertTrue((self.run_dir / "verification.json").exists())
        finally:
            os.chmod(blocked, 0o755)

    def test_predicate_on_vanished_file_is_a_fail_not_a_crash(self):
        out = self.tmp / "gone.tsv"
        self.init("--predicate",
                  json.dumps({"kind": "min_size", "path": str(out),
                              "bytes": 10}))
        self.record_attempt()
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, TECH, r.stdout)
        self.assertNotIn("Traceback", r.stderr)


class TestPathResolution(Base):
    """Regression: kimi-k3 found that relative predicate paths resolved against
    the CALLER's cwd, not the contract's. That could report a correct run as
    unmet -- and, worse, pass a run that produced nothing if an unrelated file
    of the same name sat in the caller's directory. A false pass."""

    def test_relative_output_resolves_against_contract_cwd(self):
        work = self.tmp / "work"
        (work / "results").mkdir(parents=True)
        r = contract("init", str(self.run_dir), "--command", "x",
                     "--cwd", str(work), "--output", "results/final.tsv")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.record_attempt()
        (work / "results" / "final.tsv").write_text("data\n")
        # Check from a completely different directory.
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        r = contract("check", str(self.run_dir), cwd=str(elsewhere))
        self.assertEqual(r.returncode, PASS,
                         "a correct run must verify regardless of caller cwd")

    def test_decoy_in_caller_cwd_cannot_cause_a_false_pass(self):
        work = self.tmp / "work2"
        (work / "results").mkdir(parents=True)
        contract("init", str(self.run_dir), "--command", "x",
                 "--cwd", str(work), "--output", "results/final.tsv")
        self.record_attempt()
        # The job produced nothing, but a same-named decoy exists where the
        # verifier is invoked from.
        decoy = self.tmp / "decoy"
        (decoy / "results").mkdir(parents=True)
        (decoy / "results" / "final.tsv").write_text("not the real output\n")
        r = contract("check", str(self.run_dir), cwd=str(decoy))
        self.assertEqual(r.returncode, TECH,
                         "a decoy file must never produce a false pass")


class TestMalformedPredicates(Base):
    """Regression: kimi-k3 and deepseek-v4-pro both refuted the no-crash claim a
    second time -- the wrapper caught OSError/KeyError/TypeError/ValueError but
    a predicate that is not a dict raises AttributeError from pred.get()."""

    def test_non_dict_predicates_do_not_crash(self):
        for bad in ('"oops"', "[1]", "null", "42"):
            with self.subTest(predicate=bad):
                run_dir = self.tmp / f"r{abs(hash(bad))}"
                out = self.tmp / "o.tsv"
                out.write_text("x\n")
                r = contract("init", str(run_dir), "--command", "x",
                             "--output", str(out), "--predicate", bad)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.record_attempt(run_dir)
                r = contract("check", str(run_dir))
                self.assertNotIn("Traceback", r.stderr,
                                 f"predicate {bad} crashed the verifier")
                self.assertTrue((run_dir / "verification.json").exists(),
                                f"predicate {bad} left no receipt")
                self.assertIn(r.returncode, range(7))


class TestExecutionEvidence(Base):
    """Regression: gpt-5.6-sol found that predicates alone could yield
    SCIENTIFIC_PASS with nothing showing a job ever ran. Pre-create the declared
    output, never submit, and the verifier certified it. A tool that exists to
    refuse false passes must not hand out the biggest one."""

    def test_preexisting_output_without_a_run_is_not_a_pass(self):
        out = self.tmp / "already_here.tsv"
        out.write_text("this file predates the contract\n")
        self.init("--output", str(out))
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("no submitted attempt", r.stdout)

    def test_same_artifacts_pass_once_a_run_is_recorded(self):
        out = self.tmp / "out.tsv"
        out.write_text("data\n")
        self.init("--output", str(out))
        self.assertEqual(contract("check", str(self.run_dir)).returncode,
                         INCOMPLETE)
        self.record_attempt()
        self.assertEqual(contract("check", str(self.run_dir)).returncode, PASS)

    def test_submission_alone_is_not_proof_of_termination(self):
        """gpt-5.6-sol, CRITICAL: with sacct unreachable, a merely-submitted job
        plus a pre-existing output returned SCIENTIFIC_PASS."""
        out = self.tmp / "out.tsv"
        out.write_text("stale artifact\n")
        self.init("--output", str(out))
        self.record_submission_only()
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("terminal state", r.stdout)

    def test_malformed_attempts_line_does_not_crash(self):
        """kimi-k3: a bad attempts.jsonl line crashed check, exiting 1 -- which
        this scheme means RUNNING, so a poller would wait forever."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        with (self.run_dir / "attempts.jsonl").open("a") as fh:
            fh.write("null\n")
            fh.write(json.dumps({"attempt": 1}) + "\n")
        r = contract("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "verification.json").exists())

    def test_nonzero_local_exit_is_a_failure(self):
        out = self.tmp / "out.tsv"
        out.write_text("partial\n")
        self.init("--output", str(out))
        self.record_attempt(exit_code=1)
        self.assertEqual(contract("check", str(self.run_dir)).returncode, FAILED)

    def test_retrospective_cannot_pass_without_execution_evidence(self):
        """Found only when all three scripts were reviewed together: this tool
        let `retrospective` substitute for execution evidence and report
        SCIENTIFIC_PASS, while traincontract.py had been fixed to refuse the
        same thing. The two tools meant different things by the same flag.
        Auditing a past run is legitimate; certifying it is not."""
        out = self.tmp / "out.tsv"
        out.write_text("data\n")
        self.init("--output", str(out), "--retrospective")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("retrospective", r.stdout.lower())
        self.assertIn("cannot certify the run", r.stdout)

    def test_retrospective_never_passes_even_with_evidence(self):
        """Reversed from the previous round: luna showed the downgrade only
        applied when there was NO terminal evidence, so a retrospective contract
        over a genuinely completed job still passed -- while traincontract.py
        refuses unconditionally. Predicates chosen after seeing the outputs are
        not certifiable however real the run was. Both tools now agree."""
        out = self.tmp / "out.tsv"
        out.write_text("data\n")
        self.init("--output", str(out), "--retrospective")
        self.record_attempt(exit_code=0)
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("cannot certify the run", r.stdout)


class TestAttemptForgery(Base):
    """Round-4 regressions. gpt-5.6-sol showed that loose typing on attempt
    records could manufacture SCIENTIFIC_PASS out of a hand-edited file."""

    def _write_attempt(self, obj):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with (self.run_dir / "attempts.jsonl").open("a") as fh:
            fh.write(json.dumps(obj) + "\n")

    def test_false_exit_code_cannot_forge_a_pass(self):
        """JSON false == 0 in Python; it must not read as a clean exit."""
        out = self.tmp / "out.tsv"
        out.write_text("stale\n")
        self.init("--output", str(out))
        self._write_attempt({"job_id": "local-fake", "terminal": "yes",
                             "exit_code": False})
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)

    def test_truthy_string_terminal_is_rejected(self):
        out = self.tmp / "out.tsv"
        out.write_text("stale\n")
        self.init("--output", str(out))
        self._write_attempt({"job_id": "x", "terminal": "true", "exit_code": 0})
        self.assertEqual(contract("check", str(self.run_dir)).returncode,
                         INCOMPLETE)

    def test_later_success_supersedes_earlier_local_failure(self):
        """A failed first try then a good retry is the normal shape of the work;
        the old failure must not pin the verdict forever."""
        out = self.tmp / "out.tsv"
        out.write_text("result\n")
        self.init("--output", str(out))
        self.record_attempt(exit_code=1)
        self.assertEqual(contract("check", str(self.run_dir)).returncode, FAILED)
        self.record_attempt(exit_code=0)
        self.assertEqual(contract("check", str(self.run_dir)).returncode, PASS)

    def test_non_utf8_attempts_file_does_not_crash(self):
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        with (self.run_dir / "attempts.jsonl").open("ab") as fh:
            fh.write(b"\xff\xfe garbage\n")
        r = contract("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "verification.json").exists())

    def test_deeply_nested_json_does_not_crash(self):
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        with (self.run_dir / "attempts.jsonl").open("a") as fh:
            fh.write("[" * 6000 + "]" * 6000 + "\n")
        r = contract("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "verification.json").exists())

    def test_malformed_contract_yields_a_receipt_not_a_crash(self):
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        (self.run_dir / "contract.json").write_text("{ not valid json")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, VIOLATED, r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "verification.json").exists())


class TestJointReviewRegressions(Base):
    """From the final all-three-files review. contract.py had not been reviewed
    since round 4 and never received the schema-validation pass."""

    def test_null_input_record_yields_a_receipt(self):
        """luna: {"inputs":[null]} called rec.get on None -> AttributeError,
        no receipt at all."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        cpath = self.run_dir / "contract.json"
        c = json.loads(cpath.read_text())
        c["inputs"] = [None]
        cpath.write_text(json.dumps(c))
        r = contract("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "verification.json").exists())
        self.assertNotEqual(r.returncode, PASS)

    def test_malformed_input_records_of_every_shape(self):
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        for bad in (None, 5, "str", [], {"rung": "content-digest"},
                    {"path": 7, "rung": "content-digest"}):
            with self.subTest(record=bad):
                r = contract("init", str(self.run_dir), "--command", "x",
                             "--output", str(out), "--force")
                self.assertEqual(r.returncode, 0, r.stderr)
                cpath = self.run_dir / "contract.json"
                c = json.loads(cpath.read_text())
                c["inputs"] = [bad]
                cpath.write_text(json.dumps(c))
                r = contract("check", str(self.run_dir))
                self.assertNotIn("Traceback", r.stderr, f"{bad!r} crashed")
                self.assertTrue((self.run_dir / "verification.json").exists())


class TestHardeningParity(Base):
    """contract.py never received the hardening traincontract.py got in rounds
    10-17. Found by auditing the two files against each other."""

    def test_null_collection_fields_yield_a_receipt(self):
        """luna: my previous fix handled [null] but not a null COLLECTION."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        for field, bad in (("inputs", None), ("predicates", None),
                           ("inputs", 5), ("predicates", "str"),
                           ("declared_outputs", 7), ("repo", []),
                           ("environment", "x"), ("retrospective", "yes")):
            with self.subTest(field=field, value=bad):
                r = contract("init", str(self.run_dir), "--command", "x",
                             "--output", str(out), "--force")
                self.assertEqual(r.returncode, 0, r.stderr)
                cpath = self.run_dir / "contract.json"
                c = json.loads(cpath.read_text())
                c[field] = bad
                cpath.write_text(json.dumps(c))
                r = contract("check", str(self.run_dir))
                self.assertNotIn("Traceback", r.stderr,
                                 f"{field}={bad!r} crashed")
                self.assertTrue((self.run_dir / "verification.json").exists())
                self.assertNotEqual(r.returncode, PASS)

    def test_log_matches_on_a_fifo_does_not_hang(self):
        """My own audit: log_matches did a plain read_text, so a FIFO in that
        path blocked forever with no exception for any handler to catch."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        fifo = self.tmp / "job.log"
        os.mkfifo(fifo)
        self.init("--output", str(out), "--predicate",
                  json.dumps({"kind": "log_matches", "path": str(fifo),
                              "pattern": "Traceback", "expect": False}))
        self.record_attempt(exit_code=0)
        r = contract("check", str(self.run_dir))
        self.assertNotIn("Traceback (most recent call last)", r.stderr)
        self.assertTrue((self.run_dir / "verification.json").exists())
        self.assertNotEqual(r.returncode, PASS)

    def test_min_lines_on_a_fifo_does_not_hang(self):
        """luna, verified by reproducing the hang: read_text_bounded was added
        for log_matches and used in exactly one place, so min_lines still did a
        blocking open() and a FIFO there hung the verifier with no exception."""
        fifo = self.tmp / "lines.fifo"
        os.mkfifo(fifo)
        self.init("--predicate",
                  json.dumps({"kind": "min_lines", "path": str(fifo),
                              "lines": 1}))
        self.record_attempt(exit_code=0)
        r = contract("check", str(self.run_dir))
        self.assertTrue((self.run_dir / "verification.json").exists())
        self.assertNotEqual(r.returncode, PASS)

    def test_attempts_log_as_a_fifo_does_not_hang(self):
        """luna: _attempts() read the log with a blocking read_text()."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        (self.run_dir / "attempts.jsonl").unlink(missing_ok=True)
        os.mkfifo(self.run_dir / "attempts.jsonl")
        r = contract("check", str(self.run_dir))
        self.assertTrue((self.run_dir / "verification.json").exists())
        self.assertNotEqual(r.returncode, PASS)

    def test_log_matches_still_works_on_a_real_log(self):
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        log = self.tmp / "job.log"
        log.write_text("all fine\n")
        self.init("--output", str(out), "--predicate",
                  json.dumps({"kind": "log_matches", "path": str(log),
                              "pattern": "Traceback", "expect": False}))
        self.record_attempt(exit_code=0)
        self.assertEqual(contract("check", str(self.run_dir)).returncode, PASS)

    def test_fifo_contract_file_does_not_hang(self):
        """luna: contract.json itself was still read with a plain read_text()."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        (self.run_dir / "contract.json").unlink()
        os.mkfifo(self.run_dir / "contract.json")
        pr = subprocess.Popen(
            [sys.executable, str(SCRIPT), "check", str(self.run_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            pr.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pr.kill()
            self.fail("check hung on a FIFO contract.json")
        self.assertEqual(pr.returncode, VIOLATED)

    def test_receipt_path_as_a_directory_still_reports(self):
        """luna: traincontract.py guarded its receipt write in round 16;
        contract.py's equivalent was left unguarded, so verification.json being
        a directory produced a traceback from the line whose job is to
        guarantee a receipt."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        self.record_attempt(exit_code=0)
        (self.run_dir / "verification.json").mkdir()
        r = contract("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("SCIENTIFIC_PASS", r.stdout)
        self.assertIn("could not write", r.stderr)

    def test_fifo_receipt_path_does_not_hang(self):
        """luna: the guarded write still HUNG on a FIFO -- write_text blocks in
        open() with no reader, and an OSError handler cannot help with a block.
        Now written to a temp file and renamed over the target."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        self.record_attempt(exit_code=0)
        os.mkfifo(self.run_dir / "verification.json")
        pr = subprocess.Popen(
            [sys.executable, str(SCRIPT), "check", str(self.run_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out_s, err_s = pr.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pr.kill()
            self.fail("check hung writing to a FIFO verification.json")
        self.assertEqual(pr.returncode, PASS, out_s)
        self.assertIn("SCIENTIFIC_PASS", out_s)

    def test_fifo_attempts_log_write_does_not_hang(self):
        """deepseek: reads were hardened but open('a') on attempts.jsonl still
        blocked on a FIFO, so `record` and `submit` hung with no output."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        (self.run_dir / "attempts.jsonl").unlink(missing_ok=True)
        os.mkfifo(self.run_dir / "attempts.jsonl")
        pr = subprocess.Popen(
            [sys.executable, str(SCRIPT), "record", str(self.run_dir),
             "--exit-code", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _, err = pr.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pr.kill()
            self.fail("record hung appending to a FIFO attempts.jsonl")
        self.assertNotEqual(pr.returncode, 0)
        self.assertIn("cannot record the attempt", err)

    def test_watchdog_emits_one_verdict_and_exits(self):
        """deepseek is right that O_NONBLOCK cannot save an open() on a hung
        NFS mount and there is no portable timed open. A SIGALRM watchdog turns
        an unbounded hang into a verdict. My own verification then caught that
        evaluate_predicate's `except BaseException` swallowed the watchdog's
        SystemExit, so check printed the timeout verdict AND a second one."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out), "--predicate",
                  json.dumps({"kind": "command", "run": "sleep 30"}))
        self.record_attempt(exit_code=0)
        r = contract("check", str(self.run_dir), "--watchdog", "2")
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("watchdog", r.stdout)
        # Exactly one verdict line.
        verdicts = [ln for ln in r.stdout.splitlines()
                    if ln and not ln.startswith((" ", "\t"))]
        self.assertEqual(len(verdicts), 1, f"multiple verdicts: {verdicts}")

    def test_watchdog_does_not_fire_on_a_normal_run(self):
        # Declare first, then produce: an output written before init now fails
        # the provenance rule, which is the point of that rule.
        out = self.tmp / "out.tsv"
        self.init("--output", str(out))
        out.write_text("x\n")
        self.record_attempt(exit_code=0)
        r = contract("check", str(self.run_dir), "--watchdog", "60")
        self.assertEqual(r.returncode, PASS, r.stdout)
        self.assertNotIn("watchdog", r.stdout)

    def test_oversized_watchdog_does_not_crash(self):
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        self.record_attempt(exit_code=0)
        r = contract("check", str(self.run_dir), "--watchdog",
                     "999999999999999999999")
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.returncode, PASS, r.stdout)

    def test_no_temp_files_left_after_check(self):
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        self.record_attempt(exit_code=0)
        contract("check", str(self.run_dir))
        leftovers = list(self.run_dir.glob("*.tmp*"))
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")

    def test_submit_on_a_malformed_contract_does_not_traceback(self):
        """My own audit: cmd_submit parsed the contract unguarded."""
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        (self.run_dir / "contract.json").write_text("{ not json")
        r = contract("submit", str(self.run_dir))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)


class TestExitCodeEnforcement(Base):
    def test_exit_code_helper(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cm", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        for clean in ("0:0", "0", " 0:0 "):
            self.assertTrue(m.exit_code_is_clean(clean), repr(clean))
        # An ABSENT code is not positive evidence of a clean exit: assuming it
        # was let COMPLETED-with-no-code count as terminal success (luna).
        #
        # Nor is a code whose shape we do not recognise. sacct documents
        # ExitCode as "<exit>:<signal>"; skipping empty components made ":",
        # "0:" and ":0" read as clean because `all()` over what survived was
        # vacuously true, and a third component means we are parsing something
        # other than an sacct ExitCode. Both now fail closed (luna).
        for dirty in (None, "", "  ", "1:0", "0:9", "2:0", "garbage", "1",
                      ":", "0:", ":0", "0:0:", "0:0:0", "::"):
            self.assertFalse(m.exit_code_is_clean(dirty), repr(dirty))


class TestContractOwnership(Base):
    """deepseek CRITICAL + luna: attempts were not bound to the contract, so a
    stale local exit-0 record survived `init --force` and certified a run that
    never happened. traincontract.py got this binding a round earlier; this
    file did not."""

    def test_attempt_from_another_contract_is_ignored(self):
        out = self.tmp / "out.tsv"
        out.write_text("data\n")
        self.init("--output", str(out))
        self.record_attempt(exit_code=0)
        self.assertEqual(contract("check", str(self.run_dir)).returncode, PASS)
        # Re-declare: the old attempt belongs to the previous instance.
        r = contract("init", str(self.run_dir), "--command", "x",
                     "--output", str(out), "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("different contract instance", r.stdout)

    def test_fresh_attempt_after_force_passes(self):
        out = self.tmp / "out.tsv"
        self.init("--output", str(out))
        contract("init", str(self.run_dir), "--command", "x",
                 "--output", str(out), "--force")
        out.write_text("data\n")      # produced under the current instance
        self.record_attempt(exit_code=0)
        self.assertEqual(contract("check", str(self.run_dir)).returncode, PASS)

    def test_each_init_gets_a_distinct_id(self):
        out = self.tmp / "out.tsv"
        out.write_text("x\n")
        self.init("--output", str(out))
        first = json.loads((self.run_dir / "contract.json").read_text())
        contract("init", str(self.run_dir), "--command", "x",
                 "--output", str(out), "--force")
        second = json.loads((self.run_dir / "contract.json").read_text())
        self.assertTrue(first["contract_id"])
        self.assertNotEqual(first["contract_id"], second["contract_id"],
                            "identical criteria must still yield a new id")


class TestProvenance(Base):
    """luna + deepseek: provenance was established by timestamp slack, which is
    too weak. `mtime + 1 < declared_at` left a one-second window, and predicate
    artifacts had no provenance check at all."""

    def test_output_predating_the_contract_cannot_pass(self):
        out = self.tmp / "out.tsv"
        out.write_text("left by a previous run\n")
        old = time.time() - 3600
        os.utime(out, (old, old))
        self.init("--output", str(out), stamp=False)
        r = contract("record", str(self.run_dir), "--exit-code", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("predate this contract", r.stdout)

    def test_output_produced_after_the_contract_passes(self):
        out = self.tmp / "out.tsv"
        self.init("--output", str(out))
        out.write_text("produced by this run\n")
        self.record_attempt(exit_code=0)
        self.assertEqual(contract("check", str(self.run_dir)).returncode, PASS)

    def test_freshness_helper_rejects_the_one_second_window(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cm2", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertFalse(m.artifact_is_fresh(999, 1000))
        self.assertTrue(m.artifact_is_fresh(1000, 1000))
        self.assertTrue(m.artifact_is_fresh(1001, 1000))
        self.assertTrue(m.artifact_is_fresh(None, 1000))

    def test_reused_job_id_row_is_rejected(self):
        """deepseek CRITICAL: Slurm resets and reuses job ids, so an old
        COMPLETED row for a reused id certified a run that never happened."""
        b = self.tmp / "oldrow"
        b.mkdir()
        (b / "sacct").write_text(
            "#!/bin/sh\necho 'COMPLETED|0:0|2020-01-01T00:00:00'\n")
        (b / "squeue").write_text("#!/bin/sh\nexit 1\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        out = self.tmp / "out.tsv"
        self.init("--output", str(out))
        out.write_text("x\n")
        self.record_submission_only()
        env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")
        r = subprocess.run([sys.executable, str(SCRIPT), "check",
                            str(self.run_dir)], capture_output=True,
                           text=True, env=env)
        self.assertNotEqual(r.returncode, PASS, r.stdout)
        self.assertIn("reused", r.stdout)


class TestTimestampParsing(unittest.TestCase):
    """luna: my own fractional-seconds fix was broken -- it collected every
    digit after the dot, swallowing the timezone offset, so the job-id-reuse
    guard silently failed open on exactly the timestamps it was added for."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cts", SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_sacct_timestamp_forms(self):
        expect = 1735758000.0
        for s in ("2025-01-01T11:00:00.123+0000",
                  "2025-01-01T11:00:00+0000",
                  "2025-01-01T11:00:00.123456",
                  "2025-01-01T11:00:00",
                  "2025-01-01 11:00:00"):
            with self.subTest(stamp=s):
                got = self.m.parse_iso_ts(s)
                self.assertIsNotNone(got, f"{s} failed to parse")

    def test_unparseable_returns_none(self):
        for s in ("garbage", "", None, "2025-13-45T99:99:99"):
            self.assertIsNone(self.m.parse_iso_ts(s), repr(s))

    def test_command_predicate_path_does_not_count_as_evidence(self):
        """luna: a command predicate's `path` is never read by check_predicate,
        so supplying one bypassed the command-only provenance refusal."""
        src = SCRIPT.read_text()
        self.assertIn("PATH_KINDS", src)
        self.assertIn('pred.get("kind") in PATH_KINDS', src)


class TestPortability(unittest.TestCase):
    def test_stdlib_only(self):
        """Must run on a login node with no pip install rights."""
        src = SCRIPT.read_text()
        allowed = {"argparse", "calendar", "hashlib", "json", "os", "re", "shutil", "signal", "stat", "math",
                   "subprocess", "sys", "tempfile", "time", "pathlib"}
        for line in src.splitlines():
            s = line.strip()
            if s.startswith("import ") and not s.startswith("import ("):
                mod = s.split()[1].split(".")[0]
                self.assertIn(mod, allowed, f"non-stdlib import: {mod}")
            elif s.startswith("from ") and " import " in s:
                mod = s.split()[1].split(".")[0]
                self.assertIn(mod, allowed, f"non-stdlib import: {mod}")

    def test_compiles_under_target_python(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestPredicateEditIsDetected(unittest.TestCase):
    """luna: contract.py had no fingerprint over its predicates, so editing one
    after the run to match whatever the run produced was undetectable. Only
    artifact mtimes were checked, and rewriting contract.json updates its own
    mtime, not the artifacts'. traincontract.py has fingerprinted its criteria
    since round 4; the two tools now mean the same thing by provenance."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "run"
        self.log = self.tmp / "out.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, pattern="SUCCESS"):
        pred = json.dumps({"kind": "log_matches", "path": str(self.log),
                           "pattern": pattern})
        r = contract("init", str(self.run_dir), "--command", "echo hi",
                     "--predicate", pred)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Artifact written after the contract, containing only DONE: the
        # declared predicate does not hold.
        self.log.write_text("DONE\n")
        c = json.loads((self.run_dir / "contract.json").read_text())
        base = time.mktime(time.strptime(c["created_at"],
                                         "%Y-%m-%dT%H:%M:%S%z")) + 2
        os.utime(self.log, (base, base))
        r = contract("record", str(self.run_dir), "--exit-code", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        return c

    def edit_predicate(self, pattern):
        cpath = self.run_dir / "contract.json"
        c = json.loads(cpath.read_text())
        c["predicates"][0]["pattern"] = pattern
        cpath.write_text(json.dumps(c, indent=2) + "\n")

    def test_init_records_a_criteria_digest(self):
        c = self.build()
        self.assertIsInstance(c.get("criteria_digest"), str)
        self.assertTrue(c["criteria_digest"])

    def test_unedited_contract_still_passes(self):
        self.build(pattern="DONE")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, PASS,
                         r.stdout + r.stderr)

    def test_predicate_edited_to_match_the_result_cannot_pass(self):
        self.build(pattern="SUCCESS")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, TECH,
                         "the declared predicate does not hold")
        # Now fit the criterion to the result, as an operator reading the log
        # afterwards would be tempted to.
        self.edit_predicate("DONE")
        r = contract("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, PASS,
                            "a predicate fitted to the result must not certify")
        self.assertEqual(r.returncode, INCOMPLETE)
        self.assertIn("changed after", r.stdout + r.stderr)

    def test_a_contract_without_a_digest_cannot_certify(self):
        """This test previously asserted the opposite, on a backward-compat
        argument that was never true: nothing has been released, so no contract
        predates the field. Tolerating an absent digest made the mechanism
        opt-out -- null it while editing created_at_epoch and both the edit
        check and the freshness anchor were disabled at once (luna)."""
        self.build(pattern="DONE")
        cpath = self.run_dir / "contract.json"
        for absent in (None, "", "   "):
            c = json.loads(cpath.read_text())
            if absent is None:
                c.pop("criteria_digest", None)
            else:
                c["criteria_digest"] = absent
            cpath.write_text(json.dumps(c, indent=2) + "\n")
            r = contract("check", str(self.run_dir))
            self.assertEqual(r.returncode, INCOMPLETE, r.stdout + r.stderr)
            self.assertIn("no criteria_digest", r.stdout + r.stderr)

    def test_nulling_the_digest_does_not_unlock_the_freshness_anchor(self):
        """The combined attack: neuter the anchor AND the check that would
        notice."""
        self.build(pattern="DONE")
        cpath = self.run_dir / "contract.json"
        c = json.loads(cpath.read_text())
        c["created_at_epoch"] = 0
        c["criteria_digest"] = None
        cpath.write_text(json.dumps(c, indent=2) + "\n")
        # An artifact from long before the contract now looks fresh vs epoch 0.
        os.utime(self.log, (1_000_000_000, 1_000_000_000))
        r = contract("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, PASS, r.stdout + r.stderr)


class TestSubSecondProvenance(unittest.TestCase):
    """luna: created_at has second resolution, so an artifact written 0.2s
    BEFORE the contract compared equal once both sides were truncated to whole
    seconds, and a pre-existing output certified the run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "run"
        self.out = self.tmp / "out.tsv"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_output_written_earlier_in_the_same_second_is_stale(self):
        self.out.write_text("pre-existing\n")
        r = contract("init", str(self.run_dir), "--command", "echo hi",
                     "--output", str(self.out))
        self.assertEqual(r.returncode, 0, r.stderr)
        c = json.loads((self.run_dir / "contract.json").read_text())
        epoch = c["created_at_epoch"]
        self.assertIsInstance(epoch, float)
        # Same whole second, 200ms earlier: indistinguishable before the fix.
        stamp = int(epoch) + (epoch - int(epoch)) / 2
        self.assertEqual(int(stamp), int(epoch), "must be the same second")
        self.assertLess(stamp, epoch)
        os.utime(self.out, (stamp, stamp))
        contract("record", str(self.run_dir), "--exit-code", "0")
        r = contract("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, PASS,
                            "an output predating the contract must not certify")
        self.assertEqual(r.returncode, INCOMPLETE)

    def test_output_written_later_in_the_same_second_still_passes(self):
        r = contract("init", str(self.run_dir), "--command", "echo hi",
                     "--output", str(self.out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.out.write_text("result\n")
        c = json.loads((self.run_dir / "contract.json").read_text())
        epoch = c["created_at_epoch"]
        stamp = epoch + (1 - (epoch - int(epoch))) / 2
        self.assertEqual(int(stamp), int(epoch), "must be the same second")
        self.assertGreater(stamp, epoch)
        os.utime(self.out, (stamp, stamp))
        contract("record", str(self.run_dir), "--exit-code", "0")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, PASS,
                         r.stdout + r.stderr)

    def test_whole_second_mtime_inside_the_declaration_second_is_stale(self):
        """A filesystem that truncates mtime to the second says only which
        second the artifact belongs to. If that is the contract's own second,
        the artifact may predate the contract, so it cannot certify: accepting
        it re-opened the same hole for coarse filesystems (luna)."""
        r = contract("init", str(self.run_dir), "--command", "echo hi",
                     "--output", str(self.out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.out.write_text("result\n")
        c = json.loads((self.run_dir / "contract.json").read_text())
        stamp = float(int(c["created_at_epoch"]))       # coarse filesystem
        os.utime(self.out, (stamp, stamp))
        contract("record", str(self.run_dir), "--exit-code", "0")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout + r.stderr)

    def test_whole_second_mtime_in_a_later_second_passes(self):
        """The other half: a coarse mtime that is strictly later cannot
        predate the contract, so it must still certify. Any real job takes
        longer than the sub-second remainder of the declaration second."""
        r = contract("init", str(self.run_dir), "--command", "echo hi",
                     "--output", str(self.out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.out.write_text("result\n")
        c = json.loads((self.run_dir / "contract.json").read_text())
        stamp = float(int(c["created_at_epoch"]) + 1)
        os.utime(self.out, (stamp, stamp))
        contract("record", str(self.run_dir), "--exit-code", "0")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, PASS, r.stdout + r.stderr)


class TestCorruptedEpochDoesNotRejectHonestRuns(Base):
    """Self-review of the created_at_epoch change: NaN compares false against
    everything, so a NaN in that field would have judged every artifact stale
    and failed an honest run. traincontract.py's finite_number has rejected
    non-finite bounds for the same reason since round 8; the new helper did
    not, which is the same sibling-miss."""

    def test_non_finite_epoch_falls_back_to_second_resolution(self):
        out = self.tmp / "out.tsv"
        self.init("--output", str(out), stamp=False)
        out.write_text("result\n")
        cpath = self.run_dir / "contract.json"
        # created_at_epoch is itself digested now, so ANY edit to it is caught
        # as tampering before the fallback matters. Both outcomes are non-zero;
        # what must never happen is a SCIENTIFIC_PASS built on a neutered
        # provenance anchor.
        for bad in ("NaN", "Infinity", "-Infinity", "true", "null", "0"):
            self._set_epoch(cpath, bad)
            self.touch_outputs()
            contract("record", str(self.run_dir), "--exit-code", "0")
            r = contract("check", str(self.run_dir))
            self.assertNotEqual(r.returncode, PASS,
                                f"epoch {bad}: {r.stdout}{r.stderr}")
            self.assertIn("changed after", r.stdout + r.stderr)

    def test_the_helper_itself_rejects_non_finite(self):
        """Unit level, because the end-to-end path above is now short-circuited
        by the digest: NaN compares false against everything, so a NaN epoch
        would judge every artifact stale. traincontract.py's finite_number has
        rejected non-finite bounds for this reason since round 8."""
        import importlib.util as _u
        spec = _u.spec_from_file_location("cm_epoch", SCRIPT)
        m = _u.module_from_spec(spec); spec.loader.exec_module(m)
        for bad in (float("nan"), float("inf"), float("-inf"), True, False,
                    "1.5", None, [], {}):
            self.assertIsNone(m.contract_epoch({"created_at_epoch": bad}),
                              repr(bad))
        for good in (1.5, 0, 0.0, 1700000000.25):
            self.assertEqual(m.contract_epoch({"created_at_epoch": good}), good)

    def test_a_wrongly_typed_epoch_is_a_malformed_contract(self):
        """A string there is not a degraded timestamp, it is a contract that
        does not typecheck, and schema validation says so before any verdict."""
        out = self.tmp / "out.tsv"
        self.init("--output", str(out), stamp=False)
        out.write_text("result\n")
        cpath = self.run_dir / "contract.json"
        self._set_epoch(cpath, '"1.5"')
        self.touch_outputs()
        contract("record", str(self.run_dir), "--exit-code", "0")
        r = contract("check", str(self.run_dir))
        self.assertEqual(r.returncode, VIOLATED, r.stdout + r.stderr)
        self.assertIn("created_at_epoch", r.stdout + r.stderr)

    @staticmethod
    def _set_epoch(cpath, literal):
        c = re.sub(r'"created_at_epoch": [^,\n]+',
                   f'"created_at_epoch": {literal}', cpath.read_text())
        cpath.write_text(c)

class TestEveryOffsetRenderingSacctCanEmit(unittest.TestCase):
    """deepseek asserted %z matches only +HHMM, so colon offsets and Z would
    return None and discard honest rows. It does not reproduce: %z has accepted
    +HH:MM and Z since Python 3.7, which is this repo's floor. Pinned here so
    the claim is settled by a test rather than re-argued, and so a future
    hand-rolled offset parser cannot regress it."""

    def module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cm_off", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_all_renderings_of_one_instant_agree(self):
        m = self.module()
        want = float(calendar.timegm(
            time.strptime("2024-08-12T15:05:00", "%Y-%m-%dT%H:%M:%S")))
        for form in ("2024-08-12T15:05:00+0000",
                     "2024-08-12T15:05:00+00:00",
                     "2024-08-12T15:05:00Z",
                     "2024-08-12T20:35:00+05:30",      # half-hour offset
                     "2024-08-12T15:05:00.123456+00:00",
                     "2024-08-12 15:05:00+0000"):      # space separator
            with self.subTest(form=form):
                self.assertEqual(m.parse_iso_ts(form), want)

    def test_python_floor_supports_what_this_relies_on(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 7),
                                "%z colon/Z support needs 3.7+")

    def test_garbage_still_returns_none(self):
        m = self.module()
        for bad in ("not-a-timestamp", "", "   ", "2024-13-45T99:99:99",
                    None, 12345):
            self.assertIsNone(m.parse_iso_ts(bad), repr(bad))

if __name__ == "__main__":
    unittest.main(verbosity=2)
