#!/usr/bin/env python3
"""Tests for handoff.py.

Written against the thirteen acceptance criteria in
docs/plan-portable-handoff.md, which three plan reviews produced before any of
the code existed.

Two fixture rules, both from defects this repo shipped:

  - No fixture generates a timestamp at CHECK time. Five defects here came from
    a fixture whose inputs were built from the same assumption as the code, so
    it could not fail: `$(date)` for a scheduler stamp that had to precede an
    event, `date -u` where the tool emits local time. Stamps here are explicit
    offsets from the contract's own declaration time.
  - Assert the invariant, not the message that happens to win locally. A test
    pinning one reason lost a race to a watchdog on a slower cluster.

    python3 tests/test_handoff.py
"""

import calendar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HANDOFF = REPO / "skills" / "hanig-portable-handoff" / "scripts" / "handoff.py"
CONTRACT = REPO / "skills" / "hanig-verified-workflow" / "scripts" / "contract.py"
TRAINING = REPO / "skills" / "hanig-verified-training" / "scripts" / "traincontract.py"

CLEAN, DRIFTED, ELSEWHERE, MALFORMED = 0, 1, 2, 3
USAGE = 64

PLATEAU = {"metric": "val_loss", "mode": "min", "rel_improvement_below": 0.01,
           "over_evals": 3, "min_steps": 0}


def sh(script, *argv, env=None):
    return subprocess.run([sys.executable, str(script), *[str(a) for a in argv]],
                          capture_output=True, text=True, env=env)


def declared_epoch(run_dir, name="contract.json"):
    """The contract's own declaration time. Every fixture stamp is an offset
    from THIS, never from wall-clock at check time."""
    c = json.loads((Path(run_dir) / name).read_text())
    if "created_at_epoch" in c:
        return c["created_at_epoch"]
    return time.mktime(time.strptime(c["created_at"], "%Y-%m-%dT%H:%M:%S%z"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "run"
        self.out = self.tmp / "o.tsv"
        self.hf = self.tmp / "handoff.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def workflow_run(self, size=100, verify=True):
        """A verified workflow run directory, stamped from its own contract."""
        r = sh(CONTRACT, "init", self.run_dir, "--command", "echo hi",
               "--output", self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = declared_epoch(self.run_dir)
        self.out.write_text("x" * size)
        os.utime(self.out, (d + 2, d + 2))
        sh(CONTRACT, "record", self.run_dir, "--exit-code", "0")
        # `record` writes its attempt at wall-clock now, which these fixtures
        # invert by stamping outputs forward. Order it after the outputs, as a
        # real run does: the run finishes, THEN it is recorded.
        ap = self.run_dir / "attempts.jsonl"
        lines = [json.loads(l) for l in ap.read_text().splitlines() if l.strip()]
        if lines and lines[-1].get("terminal") is True:
            lines[-1]["submitted_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(d + 4))
            ap.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        if verify:
            sh(CONTRACT, "check", self.run_dir)
        return d

    def capture(self, *extra):
        r = sh(HANDOFF, "capture", self.run_dir, "--out", self.hf, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(self.hf.read_text())

    def resume(self, *extra):
        return sh(HANDOFF, "resume", self.hf, *extra)


class TestCaptureRecordsOnlyWhatItMay(Base):
    """Criterion 1: an explicit allowlist. v1 said capture never reads file
    contents while another criterion required reading a receipt -- the two could
    not both hold, which is the tension a criterion had been added to prevent."""

    def test_the_allowlist_is_explicit_and_small(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hf", HANDOFF)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertIn("contract.json", m.CAPTURE_READS)
        self.assertIn("verification.json", m.CAPTURE_READS)
        self.assertIn("training-binding.json", m.CAPTURE_READS)
        self.assertNotIn("o.tsv", m.CAPTURE_READS)
        self.assertLessEqual(len(m.CAPTURE_READS), 8)

    def test_reading_a_non_allowlisted_file_is_a_programming_error(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hf2", HANDOFF)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with self.assertRaises(AssertionError):
            m.read_state_file(self.tmp, "o.tsv")

    def test_an_artifact_is_stated_never_opened(self):
        """The pointer carries size and mtime, which stat gives. If the
        implementation ever opened it, a FIFO would hang the tool."""
        self.workflow_run()
        h = self.capture()
        ptr = h["runs"][0]["pointers"][0]
        self.assertTrue(ptr["exists"])
        self.assertEqual(ptr["size"], 100)
        self.assertIn("mtime", ptr)


class TestVerdictAttribution(Base):
    """Criterion 2, and the finding that fixed the two shipped verifiers: a
    receipt is a verdict only when it names this contract instance."""

    def test_a_matching_receipt_is_recorded_as_the_verdict(self):
        self.workflow_run()
        h = self.capture()
        v = h["runs"][0]["verdict"]
        self.assertEqual(v["verdict"], "SCIENTIFIC_PASS")
        self.assertEqual(v["exit_code"], 0)
        self.assertIsNone(v["reason"])

    def test_a_receipt_from_another_instance_is_not_a_verdict(self):
        """`init --force` leaves the old receipt behind. Recording its PASS for
        the new contract would report a state that was never verified."""
        self.workflow_run()
        r = sh(CONTRACT, "init", self.run_dir, "--force", "--command",
               "echo hi", "--output", self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        h = self.capture()
        v = h["runs"][0]["verdict"]
        self.assertIsNone(v["verdict"])
        self.assertIn("different contract instance", v["reason"])
        self.assertIn("check", v["reason"])          # names an action

    def test_a_receipt_naming_no_instance_is_not_a_verdict(self):
        self.workflow_run()
        vp = self.run_dir / "verification.json"
        rec = json.loads(vp.read_text())
        del rec["contract_id"]
        vp.write_text(json.dumps(rec))
        h = self.capture()
        self.assertIsNone(h["runs"][0]["verdict"]["verdict"])

    def test_no_receipt_at_all_is_not_a_verdict(self):
        self.workflow_run(verify=False)
        h = self.capture()
        self.assertIsNone(h["runs"][0]["verdict"]["verdict"])

    def test_capture_never_runs_check(self):
        """Criterion 3. If capture ran check it would write a receipt, so the
        absence of one after capture is the observable proof."""
        self.workflow_run(verify=False)
        self.assertFalse((self.run_dir / "verification.json").exists())
        self.capture()
        self.assertFalse((self.run_dir / "verification.json").exists())

    def test_a_non_zero_verdict_is_listed_unresolved_first(self):
        self.workflow_run(verify=False)
        # a predicate that cannot hold: TECHNICALLY_COMPLETE, exit 3
        self.out.unlink()
        sh(CONTRACT, "check", self.run_dir)
        h = self.capture()
        self.assertEqual(h["unresolved"], [h["runs"][0]["run_dir"]])


class TestResumeStateMachine(Base):
    """Criteria 6, 7, 8 and the ordering. States are checked in order and the
    first match wins, so they are exclusive by construction -- v3's CLEAN and
    DRIFTED overlapped when the contract changed, and an implementer could have
    exited 0 and run with the wrong input specification."""

    def test_same_machine_unchanged_is_clean(self):
        self.workflow_run()
        self.capture()
        r = self.resume()
        self.assertEqual(r.returncode, CLEAN, r.stdout + r.stderr)

    def test_a_size_change_is_drift(self):
        d = self.workflow_run()
        self.capture()
        self.out.write_text("x" * 200)
        os.utime(self.out, (d + 2, d + 2))
        r = self.resume()
        self.assertEqual(r.returncode, DRIFTED, r.stdout)
        self.assertIn("200 bytes, was 100", r.stdout)

    def test_mtime_alone_is_never_drift(self):
        """A checkpoint copied or restored with fresh mtimes and unchanged bytes
        must not prompt a re-run."""
        d = self.workflow_run()
        self.capture()
        os.utime(self.out, (d + 99_999, d + 99_999))
        r = self.resume()
        self.assertEqual(r.returncode, CLEAN, r.stdout)

    def test_a_vanished_pointer_is_elsewhere_not_drift(self):
        """Different action: go to that host, or re-point. Not 'reconcile the
        code', which is what DRIFTED tells you."""
        self.workflow_run()
        self.capture()
        self.out.unlink()
        r = self.resume()
        self.assertEqual(r.returncode, ELSEWHERE, r.stdout)
        self.assertIn("not reachable here", r.stdout)

    def test_drift_wins_over_elsewhere(self):
        """Order matters and is asserted: reconcile the code before chasing a
        path, because a code difference explains a missing artifact."""
        self.workflow_run()
        h = self.capture()
        h["code"]["commit"] = "0" * 40
        self.hf.write_text(json.dumps(h))
        self.out.unlink()
        r = self.resume()
        self.assertEqual(r.returncode, DRIFTED, r.stdout)

    def test_a_relative_pointer_is_malformed_not_elsewhere(self):
        """Telling the user to switch hosts would send them where the problem
        is not: the file was written wrong."""
        self.workflow_run()
        h = self.capture()
        h["runs"][0]["pointers"][0]["path"] = "o.tsv"
        self.hf.write_text(json.dumps(h))
        r = self.resume()
        self.assertEqual(r.returncode, MALFORMED, r.stdout)
        self.assertIn("relative", r.stdout)
        self.assertIn("Re-capture", r.stdout)

    def test_an_unreadable_handoff_is_malformed_and_says_what_to_do(self):
        (self.tmp / "junk.json").write_text("{ not json")
        r = sh(HANDOFF, "resume", self.tmp / "junk.json")
        self.assertEqual(r.returncode, MALFORMED, r.stdout)
        self.assertIn("Re-capture", r.stdout)

    def test_a_missing_handoff_is_malformed_not_a_traceback(self):
        r = sh(HANDOFF, "resume", self.tmp / "nope.json")
        self.assertEqual(r.returncode, MALFORMED, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_every_mismatch_is_enumerated_never_counted(self):
        d = self.workflow_run()
        h = self.capture()
        h["code"]["commit"] = "0" * 40
        h["code"]["branch"] = "some-other-branch"
        self.hf.write_text(json.dumps(h))
        r = self.resume()
        self.assertEqual(r.returncode, DRIFTED)
        self.assertIn("commit:", r.stdout)
        self.assertIn("branch:", r.stdout)

    def test_a_host_difference_alone_is_clean_and_reported(self):
        """Criterion 7. v2's table demanded 'same host' for CLEAN while this
        criterion said otherwise, so the same scenario was both."""
        self.workflow_run()
        h = self.capture()
        h["host"]["hostname"] = "some-other-cluster"
        h["host"]["user"] = "someone"
        self.hf.write_text(json.dumps(h))
        r = self.resume()
        self.assertEqual(r.returncode, CLEAN, r.stdout)
        self.assertIn("some-other-cluster", r.stdout)
        self.assertIn("host:", r.stdout)

    def test_base_reroots_a_tree_that_moved(self):
        self.workflow_run()
        self.capture()
        moved = self.tmp / "moved"
        moved.mkdir()
        shutil.move(str(self.out), str(moved / self.out.name))
        self.assertEqual(self.resume().returncode, ELSEWHERE)
        r = self.resume("--base", str(moved))
        self.assertEqual(r.returncode, CLEAN, r.stdout)


class TestRedactionAndDigest(Base):
    """Criterion 5, wrong in all three plan revisions before the last. Carrying
    values as-is leaks a credential; redacting them makes the record differ from
    reality so an honest resume reports drift. Compare a digest of the
    original, display a redacted copy."""

    def credential_run(self):
        env = dict(os.environ, AWS_SECRET_ACCESS_KEY="hunter2",
                   MY_API_TOKEN="t0ps3cret", CONDA_PREFIX="/opt/conda")
        r = subprocess.run(
            [sys.executable, str(CONTRACT), "init", str(self.run_dir),
             "--command", "echo hi", "--output", str(self.out)],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.out.write_text("x\n")
        sh(CONTRACT, "record", self.run_dir, "--exit-code", "0")

    def test_no_credential_value_reaches_the_handoff(self):
        self.credential_run()
        self.capture()
        blob = self.hf.read_text()
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("t0ps3cret", blob)

    def test_a_non_credential_env_value_is_kept(self):
        """CONDA_PREFIX is reproducibility information, not a secret. Redacting
        it would destroy what the contract exists to record."""
        self.credential_run()
        self.capture()
        self.assertIn("/opt/conda", self.hf.read_text())

    def test_the_digest_is_computed_over_the_original(self):
        """So redaction cannot manufacture drift on an honest resume."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("hf3", HANDOFF)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        original = {"environment": {"AWS_SECRET_ACCESS_KEY": "hunter2"}}
        redacted = m.redact_env(original)
        self.assertEqual(redacted["environment"]["AWS_SECRET_ACCESS_KEY"],
                         "[redacted]")
        self.assertEqual(original["environment"]["AWS_SECRET_ACCESS_KEY"],
                         "hunter2", "redact_env must not mutate its input")
        self.assertNotEqual(m.contract_digest(original),
                            m.contract_digest(redacted))

    def test_credential_names_are_kept_only_values_go(self):
        """Knowing a run had a token set is real information."""
        self.credential_run()
        h = self.capture()
        env = h["runs"][0]["contract"]["environment"]
        self.assertNotIn("hunter2", json.dumps(env))

class TestMemoryIsScopedByShaNotTime(Base):
    """Criteria 9 and 10. v1 scoped this by "git log since the last update",
    which is the timestamp-as-boundary mistake that retired three rules in this
    repo: a restored file or clock skew silently omits or repeats commits, and
    stamping a fresh time each run makes determinism impossible."""

    def git_project(self, commits=3):
        d = self.tmp / "proj"
        d.mkdir()
        def g(*a):
            r = subprocess.run(["git", *a], cwd=str(d), capture_output=True,
                               text=True)
            return r
        g("init", "-q")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "T")
        for i in range(commits):
            (d / f"f{i}.txt").write_text(f"{i}\n")
            g("add", "-A")
            g("commit", "-q", "-m", f"commit {i}")
        return d

    def head(self, d):
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(d),
                           capture_output=True, text=True)
        return r.stdout.strip()

    def test_it_records_the_sha_it_generated_from(self):
        d = self.git_project()
        r = sh(HANDOFF, "memory", d)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (d / "MEMORY.md").read_text()
        self.assertIn(self.head(d)[:12], text)
        self.assertIn("handoff:facts:begin", text)
        self.assertIn("handoff:facts:end", text)

    def test_it_is_deterministic_given_its_inputs(self):
        """Criterion 9 as stated: deterministic given HEAD, the recorded sha,
        and the receipts on disk -- NOT "no diff on an unchanged repo", which
        v2 claimed and could not deliver.

        The first run has no recorded sha, so it bootstraps with recent history
        and writes the marker. That CHANGES an input, so run 1 -> run 2 may
        differ legitimately; every run after that must not."""
        d = self.git_project()
        sh(HANDOFF, "memory", d)                    # bootstrap: no marker yet
        second = sh(HANDOFF, "memory", d)
        self.assertEqual(second.returncode, 0, second.stderr)
        settled = (d / "MEMORY.md").read_text()
        third = sh(HANDOFF, "memory", d)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual((d / "MEMORY.md").read_text(), settled,
                         "inputs unchanged, so the output must not move")
        self.assertIn("no change", third.stdout)

    def test_the_bootstrap_run_shows_recent_history(self):
        """A first-ever run must be useful, not empty: there is no delta to
        show, so it shows what is there."""
        d = self.git_project(commits=3)
        sh(HANDOFF, "memory", d)
        text = (d / "MEMORY.md").read_text()
        self.assertIn("commit 2", text)
        self.assertIn("no previous marker", text)

    def test_a_new_commit_appears_in_the_range(self):
        d = self.git_project()
        sh(HANDOFF, "memory", d)
        (d / "new.txt").write_text("new\n")
        subprocess.run(["git", "add", "-A"], cwd=str(d), capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "the new one"],
                       cwd=str(d), capture_output=True)
        sh(HANDOFF, "memory", d)
        self.assertIn("the new one", (d / "MEMORY.md").read_text())

    def test_a_non_ancestor_sha_regenerates_in_full_and_says_why(self):
        """After a rebase the recorded sha is not an ancestor of HEAD, and
        `git log <sha>..HEAD` then returns the WHOLE branch rather than
        erroring -- a silent corruption of every MEMORY.md written after a
        rebase (kimi, plan review 2)."""
        d = self.git_project()
        sh(HANDOFF, "memory", d)
        mem = d / "MEMORY.md"
        text = mem.read_text().replace(self.head(d), "0" * 40)
        # also fix the short form the marker carries
        import re as _re
        text = _re.sub(r"sha=[0-9a-f]{7,40}", "sha=" + "0" * 40, text)
        mem.write_text(text)
        r = sh(HANDOFF, "memory", d)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = mem.read_text()
        self.assertIn("not an ancestor", out)
        self.assertIn("rebase", out)

    def test_content_outside_the_markers_is_untouched(self):
        d = self.git_project()
        mem = d / "MEMORY.md"
        mem.write_text("# Mine\n\n## Decisions\n\nDo not touch this line.\n")
        sh(HANDOFF, "memory", d)
        out = mem.read_text()
        self.assertIn("Do not touch this line.", out)
        self.assertIn("## Decisions", out)

    def test_a_judgment_section_is_never_authored(self):
        """It emits facts. Decisions, blockers and recommendations are the
        reader's, and the command refuses to write a block that mentions one."""
        d = self.git_project()
        sh(HANDOFF, "memory", d)
        block = (d / "MEMORY.md").read_text()
        i = block.index("handoff:facts:begin")
        j = block.index("handoff:facts:end")
        generated = block[i:j].lower()
        for word in ("decision", "blocker", "recommend"):
            self.assertNotIn(word, generated)

    def test_a_non_git_directory_says_so_rather_than_failing(self):
        d = self.tmp / "plain"
        d.mkdir()
        r = sh(HANDOFF, "memory", d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Not a git work tree", (d / "MEMORY.md").read_text())

    def test_open_contracts_are_listed_with_their_attributed_verdicts(self):
        d = self.git_project()
        rd = d / "run1"
        out = d / "out.tsv"
        sh(CONTRACT, "init", rd, "--command", "echo hi", "--output", out)
        dec = declared_epoch(rd)
        out.write_text("x\n")
        os.utime(out, (dec + 2, dec + 2))
        sh(CONTRACT, "record", rd, "--exit-code", "0")
        sh(CONTRACT, "check", rd)
        sh(HANDOFF, "memory", d)
        text = (d / "MEMORY.md").read_text()
        self.assertIn("Open contracts", text)
        self.assertIn("run1", text)

    def test_a_missing_directory_is_a_usage_error_not_a_verdict(self):
        r = sh(HANDOFF, "memory", self.tmp / "nope")
        self.assertNotIn(r.returncode, (CLEAN, DRIFTED, ELSEWHERE, MALFORMED))


class TestPortability(unittest.TestCase):
    def test_stdlib_only(self):
        allowed = {"argparse", "calendar", "hashlib", "json", "os", "re",
                   "shutil", "signal", "stat", "subprocess", "sys", "tempfile",
                   "time", "unittest", "pathlib", "importlib"}
        for line in HANDOFF.read_text().splitlines():
            s = line.strip()
            if s.startswith("import ") and not s.startswith("import ("):
                self.assertIn(s.split()[1].split(".")[0], allowed, s)
            elif s.startswith("from ") and " import " in s:
                self.assertIn(s.split()[1].split(".")[0], allowed, s)

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(HANDOFF)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_usage_error_does_not_collide_with_a_verdict(self):
        r = sh(HANDOFF, "resume")          # missing required argument
        self.assertEqual(r.returncode, USAGE)

if __name__ == "__main__":
    unittest.main(verbosity=2)
