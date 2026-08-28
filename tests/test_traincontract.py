#!/usr/bin/env python3
"""Tests for traincontract.py.

The one that matters is test_budget_exhausted_is_not_convergence: a run that
hits its step limit without meeting its criterion must not report as converged.
Everything else follows from that distinction holding.

No GPU, no cluster, no network.

    python3 tests/test_traincontract.py
"""

import calendar
import json
import os
import shutil
import time
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "hanig-verified-training" / "scripts" / "traincontract.py"

CONVERGED, RUNNING, DIVERGED, BUDGET, VIOLATED, PREEMPTED, INCOMPLETE = range(7)

PLATEAU = {"metric": "val_loss", "mode": "min",
           "rel_improvement_below": 0.01, "over_evals": 3, "min_steps": 0}

# A flat curve that meets PLATEAU, for tests whose subject is not the curve.
PLATEAU_ROWS = [{"step": s, "val_loss": 2.0} for s in range(100, 900, 100)]


def tc(*argv):
    r = subprocess.run([sys.executable, str(SCRIPT), *argv],
                       capture_output=True, text=True)
    # Same reason as test_contract.py's wrapper: these fixtures stamp metrics
    # and checkpoints FORWARD to satisfy the provenance rule, inverting the real
    # order in which a run finishes and is then recorded. Emulate the real order
    # rather than weakening a rule that is correct.
    if argv and argv[0] == "record" and r.returncode == 0 and len(argv) > 1:
        _order_record_after_artifacts(Path(argv[1]))
    return r


def _order_record_after_artifacts(run_dir):
    path = run_dir / "training-termination.json"
    try:
        c = json.loads((run_dir / "training-contract.json").read_text())
        base = time.mktime(time.strptime(c["created_at"],
                                         "%Y-%m-%dT%H:%M:%S%z"))
        rec = json.loads(path.read_text())
    except (OSError, ValueError, KeyError):
        return
    rec["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                       time.localtime(base + 4))
    try:
        path.write_text(json.dumps(rec, indent=2) + "\n")
    except OSError:
        pass


def parse_local_iso(s):
    """Epoch from an ISO stamp with or without an offset, for fixture use."""
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            tm = time.strptime(s, fmt)
        except ValueError:
            continue
        off = getattr(tm, "tm_gmtoff", None)
        if off is not None:
            return float(calendar.timegm(tm) - off)
        return time.mktime(tm)
    raise ValueError(s)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "run"
        self.metrics = self.tmp / "metrics.jsonl"
        self.ckpt = self.tmp / "ckpt"
        self.ckpt.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_metrics(self, rows):
        self.metrics.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")

    def add_checkpoint(self, name="model-step-1000.pt", size=1024):
        (self.ckpt / name).write_bytes(b"x" * size)

    def init(self, *extra, converge=PLATEAU, record=True, stamp=None):
        """record=True declares a clean local termination, mirroring
        test_contract.py's record_attempt(). Without terminal evidence
        traincontract.py now refuses CONVERGED (as contract.py always has), so
        tests isolating metric logic supply it; the parity tests pass
        record=False to exercise its absence."""
        argv = ["init", str(self.run_dir), "--metrics", str(self.metrics),
                "--checkpoint-dir", str(self.ckpt)]
        if converge is not None:
            argv += ["--converge", json.dumps(converge)]
        argv += list(extra)
        r = tc(*argv)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Stamping and termination-recording are INDEPENDENT. Coupling them
        # made every record=False test timing-sensitive: without stamping, a
        # metrics file written before init trips post-hoc detection whenever
        # the two land in different seconds. Only provenance tests pass
        # stamp=False and control the ordering themselves.
        if stamp is None:
            stamp = True
        if stamp:
            self.stamp_artifacts()
        if record:
            self.record_terminal()
        return r

    def stamp_artifacts(self):
        """Move metrics and checkpoints to just after the contract, so tests
        about verdict logic are not affected by the provenance rule."""
        when = self._contract_time() + 2
        targets = list(self.ckpt.glob("*"))
        if self.metrics.exists():
            targets.append(self.metrics)
        for f in targets:
            try:
                os.utime(f, (when, when))
            except OSError:
                pass

    def _contract_time(self):
        """created_at of the current contract, as epoch seconds.

        Stamping artifacts from wall-clock `now` raced the freshness comparison
        inside the same second and made tests intermittently fail."""
        try:
            c = json.loads(
                (self.run_dir / "training-contract.json").read_text())
            return time.mktime(time.strptime(c["created_at"],
                                             "%Y-%m-%dT%H:%M:%S%z"))
        except Exception:
            return time.time()

    def record_terminal(self, exit_code=0):
        r = tc("record", str(self.run_dir), "--exit-code", str(exit_code))
        self.assertEqual(r.returncode, 0, r.stderr)


class TestTheDistinction(Base):
    def test_budget_exhausted_is_not_convergence(self):
        """A run that ran out of steps while still improving did not converge."""
        # Loss still dropping fast at every eval -- no plateau.
        self.write_metrics([{"step": s, "val_loss": 10.0 / (s / 100 + 1)}
                            for s in range(100, 1100, 100)])
        self.add_checkpoint()
        self.init("--max-steps", "1000")
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, BUDGET, r.stdout)
        self.assertIn("BUDGET_EXHAUSTED", r.stdout)
        self.assertIn("not because it converged", r.stdout)

    def test_plateau_meeting_criterion_converges(self):
        rows = [{"step": s, "val_loss": 2.0} for s in range(100, 900, 100)]
        self.write_metrics(rows)
        self.add_checkpoint()
        self.init("--max-steps", "10000")
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)

    def test_still_improving_under_budget_is_running(self):
        self.write_metrics([{"step": s, "val_loss": 10.0 / (s / 100 + 1)}
                            for s in range(100, 600, 100)])
        self.init("--max-steps", "100000")
        self.assertEqual(tc("check", str(self.run_dir)).returncode, RUNNING)

    def test_min_steps_blocks_premature_convergence(self):
        """A flat curve in the first few evals is not convergence."""
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(10, 60, 10)])
        self.add_checkpoint()
        self.init(converge={**PLATEAU, "min_steps": 10000})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, RUNNING, r.stdout)
        self.assertIn("requires 10000", r.stdout)


class TestDivergence(Base):
    def test_nan_is_divergence(self):
        self.metrics.write_text(
            '{"step": 100, "val_loss": 2.0}\n'
            '{"step": 200, "val_loss": NaN}\n')
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, DIVERGED, r.stdout)

    def test_declared_ceiling_breach_is_divergence(self):
        self.write_metrics([{"step": 100, "train_loss": 2.0},
                            {"step": 200, "train_loss": 500.0}])
        self.init("--diverge", json.dumps({"metric": "train_loss", "above": 100}))
        self.assertEqual(tc("check", str(self.run_dir)).returncode, DIVERGED)

    def test_divergence_beats_a_plateau(self):
        """A flat NaN is not a converged model."""
        self.metrics.write_text(
            "".join(f'{{"step": {s}, "val_loss": NaN}}\n'
                    for s in range(100, 900, 100)))
        self.add_checkpoint()
        self.init()
        self.assertEqual(tc("check", str(self.run_dir)).returncode, DIVERGED)


class TestMetricIntegrity(Base):
    def test_non_monotonic_steps_are_a_contract_violation(self):
        """Steps going backwards means two runs share one file; no verdict read
        off it describes a single run."""
        self.write_metrics([{"step": 100, "val_loss": 2.0},
                            {"step": 200, "val_loss": 2.0},
                            {"step": 150, "val_loss": 2.0},
                            {"step": 300, "val_loss": 2.0}])
        self.add_checkpoint()
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, VIOLATED, r.stdout)
        self.assertIn("monoton", r.stdout)

    def test_duplicate_steps_are_a_contract_violation(self):
        self.write_metrics([{"step": 100, "val_loss": 2.0},
                            {"step": 100, "val_loss": 2.1},
                            {"step": 200, "val_loss": 2.0}])
        self.init()
        self.assertEqual(tc("check", str(self.run_dir)).returncode, VIOLATED)

    def test_missing_metrics_file_is_incomplete_not_failure(self):
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)

    def test_garbage_lines_do_not_crash(self):
        self.metrics.write_text(
            'not json\n{"no_step": 1}\n{"step": 100, "val_loss": 2.0}\n'
            'null\n[1,2]\n{"step": "abc"}\n')
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())


class TestCheckpoints(Base):
    def test_converged_without_a_checkpoint_is_incomplete(self):
        """A converged curve with nothing loadable on disk is not a model."""
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 900, 100)])
        self.init()  # ckpt dir exists but is empty
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("no complete checkpoint", r.stdout)

    def test_partial_checkpoint_does_not_count(self):
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 900, 100)])
        (self.ckpt / "model-step-800.pt.tmp").write_bytes(b"x" * 512)
        (self.ckpt / "model-step-900.pt").write_bytes(b"")  # zero bytes
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("partial", r.stdout.lower())

    def test_checkpoint_tier_is_recorded(self):
        """Storage tier matters: andromeda's hot FS was at 97%."""
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 900, 100)])
        self.add_checkpoint()
        self.init()
        tc("check", str(self.run_dir))
        v = json.loads((self.run_dir / "training-verification.json").read_text())
        self.assertTrue(v["checkpoint"]["exists"])
        self.assertIsNotNone(v["checkpoint"]["filesystem"])


class TestHonesty(Base):
    def test_init_refuses_without_a_criterion(self):
        """Without a pre-declared criterion this can only say training stopped."""
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("never that it converged", r.stderr)

    def test_retrospective_is_allowed_but_labeled(self):
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--retrospective")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cannot establish convergence", r.stderr)
        c = json.loads((self.run_dir / "training-contract.json").read_text())
        self.assertTrue(c["retrospective"])

    def test_receipt_is_always_written(self):
        self.write_metrics([{"step": 100, "val_loss": 2.0}])
        self.init()
        tc("check", str(self.run_dir))
        v = json.loads((self.run_dir / "training-verification.json").read_text())
        self.assertIn("state", v)
        self.assertIn("checked_at", v)

    def test_json_mode_is_valid_json(self):
        self.write_metrics([{"step": 100, "val_loss": 2.0}])
        self.init()
        r = tc("check", str(self.run_dir), "--json")
        json.loads(r.stdout)


class TestFastProfileRegressions(Base):
    """Found by the `fast` profile (deepseek-v4-pro + gpt-5.6-luna) in 3 minutes
    for about three cents -- comparable yield to the expensive panel."""

    def test_midrun_divergence_is_caught_after_recovery(self):
        """CRITICAL, found independently by both: only the final value was
        checked, so a run that hit loss=200 and recovered looked fine."""
        self.write_metrics([{"step": 100, "train_loss": 200.0},
                            {"step": 200, "train_loss": 50.0},
                            {"step": 300, "train_loss": 2.0}])
        self.add_checkpoint()
        self.init("--diverge", json.dumps({"metric": "train_loss", "above": 100}))
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, DIVERGED, r.stdout)
        self.assertIn("exceeded", r.stdout)

    def test_retrospective_never_exits_zero_as_converged(self):
        """The tool warned that retrospective cannot establish convergence and
        then exited 0 as CONVERGED -- contradicting itself."""
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 900, 100)])
        self.add_checkpoint()
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--checkpoint-dir", str(self.ckpt), "--retrospective",
               "--converge", json.dumps(PLATEAU))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stamp_artifacts()
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED,
                            "retrospective must never report CONVERGED")
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)

    def test_zero_max_steps_is_not_truthiness_tested(self):
        self.write_metrics([{"step": 0, "val_loss": 5.0}])
        self.init("--max-steps", "0")
        self.assertEqual(tc("check", str(self.run_dir)).returncode, BUDGET)

    def test_infinite_step_does_not_crash(self):
        self.metrics.write_text('{"step": 1e999, "val_loss": 0}\n'
                                '{"step": 100, "val_loss": 2.0}\n')
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())

    def test_gaps_block_a_convergence_verdict(self):
        """A criterion met across a hole in the metrics is not convergence."""
        self.write_metrics([{"step": 0, "val_loss": 2.0},
                            {"step": 100, "val_loss": 2.0},
                            {"step": 900, "val_loss": 2.0},
                            {"step": 1000, "val_loss": 2.0}])
        self.add_checkpoint()
        self.init("--expect-eval-every", "100")
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, VIOLATED, r.stdout)

    def test_unreadable_checkpoint_does_not_count(self):
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 900, 100)])
        ck = self.ckpt / "model.ckpt"
        ck.write_bytes(b"x" * 1024)
        os.chmod(ck, 0o000)
        try:
            self.init()
            r = tc("check", str(self.run_dir))
            self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        finally:
            os.chmod(ck, 0o644)


class TestDeepProfileRegressions(Base):
    """Found by the `deep` profile (gpt-5.6-sol xhigh + kimi-k3)."""

    def test_zero_baseline_does_not_fake_a_plateau(self):
        """kimi-k3, CRITICAL: any window opening at exactly 0.0 recorded 0.0
        improvement for every subsequent change, so a real jump read as a
        plateau and returned CONVERGED."""
        rows = [{"step": 100, "val_acc": 0.0}]
        rows += [{"step": s, "val_acc": 0.5} for s in range(200, 700, 100)]
        self.write_metrics(rows)
        self.add_checkpoint()
        self.init(converge={"metric": "val_acc", "mode": "max",
                            "rel_improvement_below": 0.002,
                            "over_evals": 5, "min_steps": 100})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED,
                            "a jump from a zero baseline is not a plateau")

    def test_criterion_met_past_budget_is_budget_exhausted(self):
        """gpt-5.6-sol: evidence beyond the declared budget is outside the
        contract, not convergence."""
        self.write_metrics([{"step": 100, "val_loss": 0.6},
                            {"step": 110, "val_loss": 0.4}])
        self.add_checkpoint()
        self.init("--max-steps", "100",
                  converge={"metric": "val_loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, BUDGET, r.stdout)

    def test_zero_over_evals_is_rejected_at_init(self):
        """Better than round 5's behaviour: a criterion that can never be
        evaluated is refused at declaration rather than at verification."""
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps({"metric": "val_loss", "mode": "min",
                                         "rel_improvement_below": 0.1,
                                         "over_evals": 0}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("over_evals", r.stderr)

    def test_bad_criterion_in_a_handedited_contract_still_yields_a_receipt(self):
        """init now rejects it, but a contract can be edited afterwards, and
        check must still produce a verdict rather than a traceback."""
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 500, 100)])
        self.init()
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["converge"] = {"metric": "val_loss", "mode": "min",
                         "rel_improvement_below": "oops", "over_evals": 1}
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())
        self.assertNotEqual(r.returncode, CONVERGED)

    def test_huge_integer_metric_does_not_crash(self):
        """kimi-k3: a 400-digit int is valid JSON, survives the non-finite
        check (which only inspects floats), then overflows float()."""
        self.metrics.write_text(
            '{"step": 100, "val_loss": ' + "9" * 400 + '}\n'
            '{"step": 200, "val_loss": 2.0}\n')
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())

    def test_non_numeric_diverge_threshold_is_rejected_at_init(self):
        self.write_metrics([{"step": 100, "train_loss": 200.0}])
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps(PLATEAU),
               "--diverge", json.dumps({"metric": "train_loss",
                                        "above": "not-a-number"}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a number", r.stderr)


class TestRound6Regressions(Base):
    """Round 6. All four reviewers independently refuted the zero-baseline fix
    from round 5 -- it handled improvement away from zero but not worsening."""

    def test_worsening_from_zero_baseline_is_not_convergence(self):
        """The bug all four caught: val_loss 0 -> 1 recorded 0.0 improvement."""
        self.write_metrics([{"step": 0, "val_loss": 0.0},
                            {"step": 1, "val_loss": 1.0},
                            {"step": 2, "val_loss": 2.0}])
        self.add_checkpoint()
        self.init(converge={"metric": "val_loss", "mode": "min",
                            "rel_improvement_below": 0.01, "over_evals": 2,
                            "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED,
                            "a worsening metric is never convergence")

    def test_flat_at_exactly_zero_is_still_a_plateau(self):
        """The legitimate case must keep working."""
        self.write_metrics([{"step": s, "val_loss": 0.0}
                            for s in range(0, 600, 100)])
        self.add_checkpoint()
        self.init(converge={"metric": "val_loss", "mode": "min",
                            "rel_improvement_below": 0.01, "over_evals": 3,
                            "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_criterion_met_exactly_at_budget_converges(self):
        """CONVENTION REVERSED in round 9. deepseek-v4-pro (round 6) argued a
        criterion met at step==max_steps is BUDGET_EXHAUSTED; luna (round 9)
        argued it is convergence. luna is right -- "you have 1000 steps" means
        step 1000 is yours to spend. The boundary is inclusive; only evidence
        strictly beyond the budget is outside the contract."""
        self.write_metrics([{"step": 500, "val_loss": 0.6},
                            {"step": 1000, "val_loss": 0.4}])
        self.add_checkpoint()
        self.init("--max-steps", "1000",
                  converge={"metric": "val_loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_criterion_met_only_beyond_budget_is_exhausted(self):
        """The case that convention still has to catch."""
        self.write_metrics([{"step": 1000, "val_loss": 0.6},
                            {"step": 1001, "val_loss": 0.4}])
        self.add_checkpoint()
        self.init("--max-steps", "1000",
                  converge={"metric": "val_loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, BUDGET)

    def test_criterion_met_within_budget_still_converges(self):
        """gpt-5.6-sol, the opposite error: a later out-of-budget row must not
        mask a criterion already satisfied inside the budget."""
        self.write_metrics([{"step": 90, "val_acc": 0.95},
                            {"step": 101, "val_acc": 0.95}])
        self.add_checkpoint()
        self.init("--max-steps", "100",
                  converge={"metric": "val_acc", "mode": "max",
                            "threshold": 0.9, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)

    def test_recursion_error_metrics_does_not_crash(self):
        self.metrics.write_text('{"step":1,"x":' + "[" * 3000 + "\n"
                                '{"step": 100, "val_loss": 2.0}\n')
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())

    def test_fractional_step_is_rejected_not_truncated(self):
        """luna: int(100.9) -> 100 quietly moved out-of-budget evidence onto
        the budget boundary."""
        self.metrics.write_text('{"step": 50, "val_loss": 5.0}\n'
                                '{"step": 100.9, "val_loss": 0.0}\n')
        self.add_checkpoint()
        self.init("--max-steps", "100",
                  converge={"metric": "val_loss", "mode": "min",
                            "threshold": 1.0, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_fifo_swap_after_stat_does_not_hang(self):
        """luna: stat-then-reopen was not atomic. Open-once + fstat closes it."""
        fifo = self.tmp / "swapped.jsonl"
        os.mkfifo(fifo)
        r = tc("init", str(self.run_dir), "--metrics", str(fifo),
               "--checkpoint-dir", str(self.ckpt),
               "--converge", json.dumps(PLATEAU))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertTrue((self.run_dir / "training-verification.json").exists())

    def test_plus_inf_string_is_non_finite(self):
        """luna: the fixed spelling list omitted "+inf"; strings are parsed now."""
        self.metrics.write_text('{"step": 1, "loss": 1.0}\n'
                                '{"step": 2, "loss": "+inf"}\n')
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 2.0, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, DIVERGED)

    def test_annotation_field_is_not_treated_as_a_metric(self):
        """An overcorrection caught by my own tests: flagging every string made
        a harmless annotation field read as divergence. Only keys the contract
        actually reads as metrics are subject to the string rule."""
        self.metrics.write_text(
            '{"step": 1, "loss": 1.0, "note": "ok", "phase": "train"}\n')
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 2.0, "min_steps": 0})
        self.assertNotEqual(tc("check", str(self.run_dir)).returncode, DIVERGED)

    def test_unparseable_string_in_a_real_metric_slot_diverges(self):
        """luna: enumerating spellings always misses one ("1.#INF"). Any string
        in a slot the contract reads as a metric is unusable evidence."""
        self.metrics.write_text('{"step": 1, "loss": "1.#INF"}\n')
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 2.0, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, DIVERGED)

    def test_huge_int_step_is_not_rounded_through_float(self):
        """gpt-5.6-sol: routing steps through float rounded anything above
        2**53 down, letting an over-budget step land on the budget."""
        self.write_metrics([{"step": 9007199254740993, "val_loss": 0.4}])
        self.add_checkpoint()
        self.init("--max-steps", "9007199254740992",
                  converge={"metric": "val_loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, BUDGET)

    def test_unevaluable_metric_cannot_bypass_a_ceiling(self):
        """gpt-5.6-sol: a loss too large for float was silently dropped and
        slipped past its declared divergence ceiling."""
        self.metrics.write_text(
            '{"step": 100, "val_acc": 1.0, "loss": ' + "9" * 400 + '}\n')
        self.add_checkpoint()
        self.init("--diverge", json.dumps({"metric": "loss", "above": 100}),
                  converge={"metric": "val_acc", "mode": "max",
                            "threshold": 0.9, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)


class TestRound7Regressions(Base):
    def test_divergence_beyond_budget_does_not_override_convergence(self):
        """luna: nonfinite() and eval_divergence() scanned every row, so a
        ceiling breach past the budget overrode a convergence inside it."""
        self.write_metrics([{"step": 50, "val_acc": 0.95, "loss": 1.0},
                            {"step": 150, "val_acc": 0.95, "loss": 500.0}])
        self.add_checkpoint()
        self.init("--max-steps", "100",
                  "--diverge", json.dumps({"metric": "loss", "above": 100}),
                  converge={"metric": "val_acc", "mode": "max",
                            "threshold": 0.9, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)

    def test_divergence_within_budget_still_wins(self):
        """The legitimate case must keep working."""
        self.write_metrics([{"step": 50, "val_acc": 0.95, "loss": 500.0},
                            {"step": 90, "val_acc": 0.95, "loss": 1.0}])
        self.add_checkpoint()
        self.init("--max-steps", "100",
                  "--diverge", json.dumps({"metric": "loss", "above": 100}),
                  converge={"metric": "val_acc", "mode": "max",
                            "threshold": 0.9, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, DIVERGED)

    def test_deeply_nested_metric_value_does_not_crash(self):
        """luna: repr() of a deeply nested value raised RecursionError inside
        the code path that exists to report problems."""
        nested = json.dumps({"step": 100, "x": eval("[" * 200 + "]" * 200)})
        self.metrics.write_text(nested + "\n"
                                '{"step": 200, "x": 1.0}\n')
        self.init(converge={"metric": "x", "mode": "min",
                            "threshold": 2.0, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())


class TestRound8Regressions(Base):
    def test_string_nan_is_divergence(self):
        """deepseek-v4-pro: "NaN" as a string was skipped by metric_series and
        invisible to nonfinite(), so a NaN loss simply vanished."""
        self.metrics.write_text('{"step": 100, "val_loss": "NaN"}\n')
        self.add_checkpoint()
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, DIVERGED, r.stdout)

    def test_fifo_metrics_path_does_not_hang(self):
        """deepseek-v4-pro: a FIFO blocks read_text() forever, so no receipt."""
        fifo = self.tmp / "fifo.jsonl"
        os.mkfifo(fifo)
        r = tc("init", str(self.run_dir), "--metrics", str(fifo),
               "--checkpoint-dir", str(self.ckpt),
               "--converge", json.dumps(PLATEAU))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("not a regular file", r.stdout)

    def test_duplicate_detection_is_linear(self):
        """luna: steps.count() per row is quadratic; 100k rows would be killed
        before the receipt was written."""
        import time as _t
        self.write_metrics([{"step": s, "val_loss": 1.0}
                            for s in range(40000)])
        self.init()
        t0 = _t.time()
        r = tc("check", str(self.run_dir))
        elapsed = _t.time() - t0
        self.assertTrue((self.run_dir / "training-verification.json").exists())
        self.assertLess(elapsed, 30, f"took {elapsed:.1f}s — likely quadratic")


class TestRound12Regressions(Base):
    def test_criterion_missing_both_bounds_is_rejected(self):
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps({"metric": "loss", "mode": "min"}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("threshold", r.stderr)

    def test_bad_mode_is_rejected(self):
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps({"metric": "loss", "mode": "sideways",
                                         "threshold": 1.0}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mode", r.stderr)

    def test_diverge_rule_without_a_bound_is_rejected(self):
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps(PLATEAU),
               "--diverge", json.dumps({"metric": "loss"}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("above", r.stderr)


class TestRound13Regressions(Base):
    def test_list_metric_name_is_rejected(self):
        """luna: an unhashable metric name crashed `key not in r` at check."""
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps({"metric": ["loss"], "threshold": 0.5}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("metric", r.stderr)

    def test_nan_threshold_is_rejected(self):
        """luna: float("nan") succeeds, so every later comparison was silently
        false and the run read as budget exhaustion."""
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps({"metric": "loss", "threshold": "nan"}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("finite", r.stderr)

    def test_infinite_diverge_bound_is_rejected(self):
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps(PLATEAU),
               "--diverge", json.dumps({"metric": "loss", "above": "inf"}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("finite", r.stderr)

    def test_handedited_min_steps_yields_a_receipt(self):
        """luna: a hand-edited min_steps of "oops" raised out of
        eval_convergence, so check produced a traceback and no receipt."""
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 600, 100)])
        self.init()
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["converge"]["min_steps"] = "oops"
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())
        self.assertNotEqual(r.returncode, CONVERGED)

    def test_handedited_list_metric_yields_a_receipt(self):
        self.write_metrics([{"step": 100, "val_loss": 2.0}])
        self.init()
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["converge"]["metric"] = ["val_loss"]
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())


class TestRound14Regressions(Base):
    """All three round-14 findings were the same class: ad-hoc numeric
    validation missing a case. Replaced with two shared validators."""

    def test_fractional_min_steps_is_rejected_not_truncated(self):
        """sol: min_steps 1.5 became 1, allowing convergence below the
        declared minimum."""
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps({"metric": "loss", "threshold": 0.5,
                                         "min_steps": 1.5}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("whole number", r.stderr)

    def test_overflowing_min_steps_is_rejected(self):
        """sol: int(float('inf')) raised OverflowError inside the validator
        itself, so check aborted with no receipt."""
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--converge", json.dumps({"metric": "loss", "threshold": 0.5,
                                         "min_steps": 1e309}))
        self.assertNotEqual(r.returncode, 0)

    def test_handedited_overflowing_min_steps_yields_a_receipt(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["converge"]["min_steps"] = 1e309
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())
        self.assertNotEqual(r.returncode, CONVERGED)

    def test_unevaluable_divergence_bound_blocks_convergence(self):
        """sol: a hand-edited NaN bound made the comparison silently false, so
        the rule was disabled and a false CONVERGED emitted. An unevaluable
        rule is a breach -- the run cannot be shown to have stayed under it."""
        self.write_metrics([{"step": 10, "val_loss": 0.4,
                             "train_loss": 1000000.0}])
        self.add_checkpoint()
        self.init("--diverge", json.dumps({"metric": "train_loss",
                                           "above": 100}),
                  converge={"metric": "val_loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["diverge"][0]["above"] = "NaN"
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertEqual(r.returncode, DIVERGED, r.stdout)


class TestRound15Regressions(Base):
    def test_unusable_bound_on_missing_metric_still_breaches(self):
        """luna + deepseek: the bound check sat after `if not series: continue`,
        so an unusable bound on a never-emitted metric produced no breach."""
        self.write_metrics([{"step": 10, "loss": 0.0}])
        self.add_checkpoint()
        self.init("--diverge", json.dumps({"metric": "guard", "above": 100}),
                  converge={"metric": "loss", "mode": "min",
                            "threshold": 1.0, "min_steps": 0})
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["diverge"][0]["above"] = 1e400  # json.loads -> inf
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_unusable_value_in_the_judged_metric_blocks_convergence(self):
        """luna: eval_convergence used metric_series(), which discards unusable
        values, so a 400-digit metric vanished and an earlier step certified."""
        self.metrics.write_text('{"step": 1, "loss": 0.0}\n'
                                '{"step": 2, "loss": ' + "9" * 400 + '}\n')
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 1.0, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_empty_contract_object_yields_a_receipt(self):
        """luna: a contract of {} raised KeyError and left no receipt."""
        self.write_metrics([{"step": 1, "loss": 0.0}])
        self.init()
        (self.run_dir / "training-contract.json").write_text("{}")
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())
        self.assertEqual(r.returncode, VIOLATED, r.stdout)

    def test_handedited_max_steps_string_yields_a_receipt(self):
        """deepseek: int(budget_cap) on a hand-edited string crashed check."""
        self.write_metrics([{"step": 1, "loss": 0.0}])
        self.init("--max-steps", "10")
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["max_steps"] = "oops"
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())

    def test_no_checkpoint_dir_cannot_report_converged(self):
        """luna: omitting --checkpoint-dir bypassed the rule that a converged
        curve with nothing loadable is not a trained model."""
        self.write_metrics([{"step": 10, "loss": 0.0}])
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--max-steps", "10",
               "--converge", json.dumps({"metric": "loss", "mode": "min",
                                         "threshold": 1.0, "min_steps": 0}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("downgraded", r.stderr)
        self.stamp_artifacts()   # direct tc("init") bypasses the helper
        self.record_terminal()   # isolate the checkpoint rule from termination
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("loadable model", r.stdout)


class TestRound16Regressions(Base):
    def test_non_dict_divergence_rule_yields_a_receipt(self):
        """luna: my round-15 guard sat AFTER rule.get(), so diverge:[1] still
        raised AttributeError out of check."""
        self.write_metrics([{"step": 1, "loss": 0.0}])
        self.add_checkpoint()
        self.init()
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["diverge"] = [1]
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())
        self.assertNotEqual(r.returncode, CONVERGED)

    def test_never_emitted_divergence_metric_is_not_satisfied(self):
        """luna: a declared bound with no data behind it was skipped, so the run
        was treated as having stayed under a bound never checked."""
        self.write_metrics([{"step": 100, "val_loss": 0.4}])
        self.add_checkpoint()
        self.init("--diverge", json.dumps({"metric": "train_loss",
                                           "above": 10}),
                  converge={"metric": "val_loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("never evaluated", r.stdout)

    def test_readme_is_not_a_checkpoint(self):
        """luna: any readable non-empty file counted as a loadable model."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "README.txt").write_text("notes about this run\n")
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("none look like a checkpoint", r.stdout)

    def test_checkpoint_glob_accepts_unusual_naming(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "weights_final").write_bytes(b"x" * 128)
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--checkpoint-dir", str(self.ckpt),
               "--checkpoint-glob", "weights_*",
               "--converge", json.dumps({"metric": "loss", "mode": "min",
                                         "threshold": 0.5, "min_steps": 0}))
        self.assertEqual(r.returncode, 0, r.stderr)
        stamp = self._contract_time() + 2
        for f in list(self.ckpt.glob("*")) + [self.metrics]:
            os.utime(f, (stamp, stamp))
        self.record_terminal()
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_post_budget_divergence_is_warned_not_hidden(self):
        """deepseek: a blown-up tail was silent. The round-7 convention stands
        (out-of-budget evidence does not change the verdict), but it must be
        reported rather than invisible."""
        self.metrics.write_text('{"step": 80, "val_acc": 0.95}\n'
                                '{"step": 200, "val_acc": NaN}\n')
        self.add_checkpoint()
        self.init("--max-steps", "100",
                  converge={"metric": "val_acc", "mode": "max",
                            "threshold": 0.9, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("WARNING", r.stdout)
        self.assertIn("past the budget", r.stdout)

    def test_unwritable_run_dir_still_reports_a_verdict(self):
        """deepseek: the receipt write itself was unguarded, so the one line
        whose job is guaranteeing a receipt produced a traceback."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        os.chmod(self.run_dir, 0o500)
        try:
            r = tc("check", str(self.run_dir))
            self.assertNotIn("Traceback", r.stderr)
            self.assertEqual(r.returncode, CONVERGED, r.stdout)
            self.assertIn("could not write", r.stderr)
        finally:
            os.chmod(self.run_dir, 0o755)


class TestContractSchema(Base):
    """Rounds 13/15/16/17 each found another unvalidated contract field
    crashing check. These tests cover the CLASS, not one field at a time."""

    def _edit(self, **fields):
        # --force: setUp runs once per test method, so successive subTests
        # would otherwise collide with the contract left by the previous one.
        self.write_metrics([{"step": 1, "loss": 0.0}])
        self.add_checkpoint()
        self.init("--force", record=True)
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c.update(fields)
        cpath.write_text(json.dumps(c))
        return tc("check", str(self.run_dir))

    def test_every_wrong_typed_field_yields_a_receipt(self):
        cases = [
            {"expect_eval_every": "daily"},
            {"checkpoint_dir": 123},
            {"max_steps": "oops"},
            {"max_steps": True},
            {"metrics_file": 5},
            {"metrics_file": None},
            {"converge": "not-an-object"},
            {"diverge": "not-a-list"},
            {"diverge": [1]},
            {"preemptible": "yes"},
            {"checkpoint_glob": 7},
            {"run": []},
            {"expect_eval_every": -5},
            {"max_steps": -1},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                r = self._edit(**fields)
                self.assertNotIn("Traceback", r.stderr,
                                 f"{fields} crashed check")
                self.assertTrue(
                    (self.run_dir / "training-verification.json").exists(),
                    f"{fields} left no receipt")
                self.assertNotEqual(r.returncode, CONVERGED,
                                    f"{fields} still reported CONVERGED")

    def test_valid_contract_is_unaffected(self):
        r = self._edit()
        self.assertNotIn("Traceback", r.stderr)


class TestRound17Regressions(Base):
    def test_lone_tf_index_is_not_a_checkpoint(self):
        """luna: a TensorFlow .index carries no weights; its data shard is a
        separate file, so an .index alone cannot load."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "model.index").write_bytes(b"x" * 64)
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)

    def test_tf_index_with_its_data_shard_is_a_checkpoint(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "model.index").write_bytes(b"x" * 64)
        (self.ckpt / "model.data-00000-of-00001").write_bytes(b"y" * 4096)
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_post_budget_nan_in_any_column_is_warned(self):
        """deepseek: the tail warning was scoped to the contract's metric keys,
        so a NaN in an unrelated column past the budget was silent."""
        self.metrics.write_text('{"step": 80, "val_acc": 0.95}\n'
                                '{"step": 200, "val_acc": 0.95, '
                                '"other_metric": NaN}\n')
        self.add_checkpoint()
        self.init("--max-steps", "100",
                  converge={"metric": "val_acc", "mode": "max",
                            "threshold": 0.9, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("WARNING", r.stdout)
        self.assertIn("other_metric", r.stdout)


class TestRound18Regressions(Base):
    def test_worsening_metric_is_not_a_plateau(self):
        """deepseek-v4-pro, the important one: a negative relative improvement
        is trivially below any positive threshold, so val_loss 0.5 -> 0.6
        satisfied "improvement < 0.1%" and reported CONVERGED."""
        self.write_metrics([{"step": 100, "val_loss": 0.5},
                            {"step": 200, "val_loss": 0.6}])
        self.add_checkpoint()
        self.init(converge={"metric": "val_loss", "mode": "min",
                            "rel_improvement_below": 0.001,
                            "over_evals": 1, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("regressed", r.stdout)

    def test_steadily_worsening_max_metric_is_not_a_plateau(self):
        self.write_metrics([{"step": 100, "val_acc": 0.90},
                            {"step": 200, "val_acc": 0.80},
                            {"step": 300, "val_acc": 0.70}])
        self.add_checkpoint()
        self.init(converge={"metric": "val_acc", "mode": "max",
                            "rel_improvement_below": 0.001,
                            "over_evals": 2, "min_steps": 0})
        self.assertNotEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_genuine_plateau_still_converges(self):
        """The legitimate case must survive the fix."""
        self.write_metrics([{"step": s, "val_loss": 0.5 - s * 1e-9}
                            for s in range(100, 900, 100)])
        self.add_checkpoint()
        self.init(converge={"metric": "val_loss", "mode": "min",
                            "rel_improvement_below": 0.001,
                            "over_evals": 3, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)

    def test_huge_expect_eval_every_yields_a_receipt(self):
        """luna: a 401-digit value overflowed on `* 1.5`."""
        self.write_metrics([{"step": 0, "val_loss": 1.0},
                            {"step": 1, "val_loss": 1.0}])
        self.add_checkpoint()
        self.init()
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["expect_eval_every"] = int("9" * 401)
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())

    def test_line_count_cap_is_reported(self):
        """luna: the byte cap did not bound the line count."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("tc2", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertTrue(hasattr(m, "MAX_METRICS_LINES"))
        self.assertLess(m.MAX_METRICS_LINES, 100_000_000)

    def test_zero_byte_data_shard_is_not_a_checkpoint(self):
        """luna: the sibling test only checked presence, not usability."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "model.ckpt.index").write_bytes(b"x" * 64)
        (self.ckpt / "model.ckpt.data-00000-of-00001").write_bytes(b"")
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, INCOMPLETE)

    def test_criteria_changed_after_declaration_cannot_converge(self):
        """The second post-hoc signal: editing the criteria after declaring them
        is selection even when the contract predates the metrics."""
        self.init()
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 900, 100)])
        self.add_checkpoint()
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["converge"]["rel_improvement_below"] = 0.5   # loosened after the fact
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("criteria were changed", r.stdout)

    def test_unrelated_contract_edit_does_not_block(self):
        """deepseek: mtime alone gave a false positive, so an edit to a field
        that does not decide the verdict must not block it."""
        self.init()
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 900, 100)])
        self.add_checkpoint()
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["run"] = dict(c.get("run") or {}, note="rerun after node swap")
        cpath.write_text(json.dumps(c))
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)

    def test_contract_written_after_metrics_cannot_converge(self):
        """luna, round 18 then again in round 19: a warning was too weak. A
        criterion demonstrably chosen once the curve was visible is selection,
        and selection cannot establish convergence -- so the verdict changes,
        not just the wording."""
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 900, 100)])
        old = time.time() - 3600
        os.utime(self.metrics, (old, old))
        self.init(record=False, stamp=False)   # keep the backdated metrics
        self.record_terminal()
        self.add_checkpoint()            # checkpoint itself is fresh
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("results already visible", r.stdout)
        self.assertIn("retrospective", r.stdout)

    def test_normal_ordering_is_unaffected(self):
        """The legitimate flow -- declare, then train -- must not trip this."""
        self.init()
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in range(100, 900, 100)])
        self.add_checkpoint()
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)


class TestRound19Regressions(Base):
    def test_net_worsening_inside_the_window_is_not_a_plateau(self):
        """luna: max(improvements) let one small early gain mask a large later
        regression -- [0.1, -0.222] gave worst=0.1 and reported CONVERGED on a
        metric that ended worse than it started."""
        self.write_metrics([{"step": 0, "val_loss": 1.0},
                            {"step": 1, "val_loss": 0.9},
                            {"step": 2, "val_loss": 1.1}])
        self.add_checkpoint()
        self.init(converge={"metric": "val_loss", "mode": "min",
                            "rel_improvement_below": 0.2, "over_evals": 2,
                            "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_symmetric_swing_is_not_a_plateau(self):
        """A metric oscillating by more than the threshold has not settled."""
        self.write_metrics([{"step": 0, "val_loss": 1.0},
                            {"step": 1, "val_loss": 1.5},
                            {"step": 2, "val_loss": 1.0},
                            {"step": 3, "val_loss": 1.5}])
        self.add_checkpoint()
        self.init(converge={"metric": "val_loss", "mode": "min",
                            "rel_improvement_below": 0.05, "over_evals": 3,
                            "min_steps": 0})
        self.assertNotEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_tf_shard_with_a_different_basename_is_not_a_match(self):
        """luna: startswith(stem) paired model.index with
        model-backup.data-00000-of-00001."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "model.index").write_bytes(b"x" * 64)
        (self.ckpt / "model-backup.data-00000-of-00001").write_bytes(b"y" * 4096)
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, INCOMPLETE)

    def test_lone_tf_shard_without_an_index_is_not_a_checkpoint(self):
        """The symmetric case, surfaced by my own test while fixing luna's: a
        shard without its index is no more loadable than an index without its
        shard. A TensorFlow checkpoint is a pair."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "model.ckpt.data-00000-of-00001").write_bytes(b"y" * 4096)
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, INCOMPLETE)

    def test_matching_tf_shard_is_still_accepted(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "model.ckpt.index").write_bytes(b"x" * 64)
        (self.ckpt / "model.ckpt.data-00000-of-00001").write_bytes(b"y" * 4096)
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_oversized_single_line_is_skipped_with_a_receipt(self):
        """luna: json.loads sat outside the guard, so one enormous valid line
        could raise MemoryError and kill check before any receipt."""
        big = json.dumps({"step": 1, "note": "x" * 1_200_000})
        self.metrics.write_text(big + "\n"
                                '{"step": 2, "val_loss": 2.0}\n')
        self.add_checkpoint()
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.run_dir / "training-verification.json").exists())
        self.assertIn("per-line limit", r.stdout)


class TestRound20Regressions(Base):
    def test_missing_criterion_value_inside_the_window_blocks_convergence(self):
        """luna, MAJOR: rows lacking the judged metric were silently dropped,
        so a window was assembled from a non-contiguous subset. A bad
        evaluation that failed to log simply vanished and the survivors looked
        flat."""
        self.metrics.write_text(
            '{"step": 0, "val_loss": 1.0}\n'
            '{"step": 10}\n'                       # the real value was bad
            '{"step": 20, "val_loss": 1.0}\n'
            '{"step": 30, "val_loss": 1.0}\n')
        self.add_checkpoint()
        self.init(converge={"metric": "val_loss", "mode": "min",
                            "rel_improvement_below": 0.01, "over_evals": 2,
                            "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("not contiguous evidence", r.stdout)

    def test_sparse_metric_opt_in_allows_it(self):
        """Logging the criterion metric less often is legitimate, but has to be
        declared rather than assumed."""
        self.metrics.write_text(
            '{"step": 0, "val_loss": 1.0}\n'
            '{"step": 10, "train_loss": 0.9}\n'
            '{"step": 20, "val_loss": 1.0}\n'
            '{"step": 30, "val_loss": 1.0}\n')
        self.add_checkpoint()
        self.init("--sparse-metric",
                  converge={"metric": "val_loss", "mode": "min",
                            "rel_improvement_below": 0.01, "over_evals": 2,
                            "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_contiguous_window_still_converges(self):
        self.write_metrics([{"step": s, "val_loss": 1.0}
                            for s in range(0, 40, 10)])
        self.add_checkpoint()
        self.init(converge={"metric": "val_loss", "mode": "min",
                            "rel_improvement_below": 0.01, "over_evals": 2,
                            "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_unreadable_tf_index_beside_readable_shard(self):
        """luna, MAJOR: the pair check tested the sibling's size but not whether
        it could be read."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        idx = self.ckpt / "model.index"
        idx.write_bytes(b"x" * 64)
        (self.ckpt / "model.data-00000-of-00001").write_bytes(b"y" * 4096)
        os.chmod(idx, 0o000)
        try:
            self.init(converge={"metric": "loss", "mode": "min",
                                "threshold": 0.5, "min_steps": 0})
            r = tc("check", str(self.run_dir))
            self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        finally:
            os.chmod(idx, 0o644)

    def test_unreadable_tf_shard_beside_readable_index(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        shard = self.ckpt / "model.data-00000-of-00001"
        (self.ckpt / "model.index").write_bytes(b"x" * 64)
        shard.write_bytes(b"y" * 4096)
        os.chmod(shard, 0o000)
        try:
            self.init(converge={"metric": "loss", "mode": "min",
                                "threshold": 0.5, "min_steps": 0})
            self.assertEqual(tc("check", str(self.run_dir)).returncode,
                             INCOMPLETE)
        finally:
            os.chmod(shard, 0o644)


class TestJointReviewRegressions(Base):
    def test_checkpoint_notes_txt_is_not_a_checkpoint(self):
        """luna: the "checkpoint" substring fallback accepted a one-byte
        checkpoint_notes.txt as a loadable model."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "checkpoint_notes.txt").write_text("n")
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)

    def test_extensionless_checkpoint_name_still_counts(self):
        """The substring fallback exists for frameworks that omit a suffix."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        (self.ckpt / "checkpoint-1000").write_bytes(b"x" * 2048)
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)


class TestFifoContract(Base):
    def test_fifo_contract_file_does_not_hang(self):
        """luna: training-contract.json was still read with a plain read_text()."""
        self.write_metrics([{"step": 1, "loss": 0.1}])
        self.init()
        (self.run_dir / "training-contract.json").unlink()
        os.mkfifo(self.run_dir / "training-contract.json")
        pr = subprocess.Popen(
            [sys.executable, str(SCRIPT), "check", str(self.run_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            pr.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pr.kill()
            self.fail("check hung on a FIFO training-contract.json")
        self.assertEqual(pr.returncode, VIOLATED)

    def test_any_squeue_state_blocks_convergence(self):
        """luna: enumerating live states missed real ones like STAGE_OUT."""
        import stat as _st
        bindir = self.tmp / "fakebin"
        bindir.mkdir()
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        rb = tc("bind", str(self.run_dir), "--job-id", "42")
        self.assertEqual(rb.returncode, 0, rb.stderr)
        # squeue rows carry a submit time now (`-o %T|%V`) and are subject to
        # the same ownership test as sacct rows: a queue entry for a REUSED id
        # must not overrule terminal evidence we own (sol). The fixture emits
        # what squeue emits, pinned inside the ownership window.
        bound = parse_local_iso(json.loads(
            (self.run_dir / "training-binding.json").read_text())["submitted_at"])
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(bound))
        (bindir / "squeue").write_text(
            f'#!/bin/sh\necho "STAGE_OUT|{stamp}"\n')
        (bindir / "sacct").write_text(
            f'#!/bin/sh\necho "COMPLETED|0:0|{stamp}"\n')
        for n in ("squeue", "sacct"):
            (bindir / n).chmod(0o755)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        r = subprocess.run([sys.executable, str(SCRIPT), "check",
                            str(self.run_dir)], capture_output=True,
                           text=True, env=env)
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("STAGE_OUT", r.stdout)


class TestTerminalEvidenceParity(Base):
    """Both reviewers converged on this: contract.py has required positive
    terminal evidence since round 4, while traincontract.py accepted metrics
    alone when no slurm_job_id was recorded. Same asymmetry class as
    --retrospective the round before."""

    def test_no_job_id_and_no_record_cannot_converge(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("nothing shows the run terminated", r.stdout)

    def test_recorded_local_termination_permits_convergence(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        r = tc("record", str(self.run_dir), "--exit-code", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("recorded local run exited 0", r.stdout)

    def test_recorded_nonzero_exit_blocks_convergence(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        tc("record", str(self.run_dir), "--exit-code", "1")
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, VIOLATED, r.stdout)

    def test_forged_termination_record_shapes_are_rejected(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        for bad in ({"terminal": "yes", "exit_code": 0},
                    {"terminal": True, "exit_code": False},
                    {"terminal": True},
                    {"exit_code": 0},
                    None, 5, []):
            with self.subTest(record=bad):
                (self.run_dir / "training-termination.json").write_text(
                    json.dumps(bad))
                r = tc("check", str(self.run_dir))
                self.assertNotEqual(r.returncode, CONVERGED,
                                    f"{bad!r} was accepted as evidence")

    def test_fifo_termination_record_does_not_hang(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        os.mkfifo(self.run_dir / "training-termination.json")
        pr = subprocess.Popen([sys.executable, str(SCRIPT), "check",
                               str(self.run_dir)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        try:
            pr.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pr.kill()
            self.fail("check hung on a FIFO termination record")
        self.assertNotEqual(pr.returncode, CONVERGED)


class TestFailClosedGaps(Base):
    """luna's round: three places that reported a pass on absent or partial
    evidence rather than failing closed."""

    def test_fifo_receipt_path_does_not_hang(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        os.mkfifo(self.run_dir / "training-verification.json")
        pr = subprocess.Popen([sys.executable, str(SCRIPT), "check",
                               str(self.run_dir)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        try:
            out_s, _ = pr.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pr.kill()
            self.fail("check hung writing to a FIFO receipt")
        self.assertEqual(pr.returncode, CONVERGED, out_s)

    def test_job_id_with_no_scheduler_record_and_no_local_record(self):
        """luna: a recorded job id that neither sacct nor squeue knows about is
        not evidence of anything, but it left CONVERGED standing on a note."""
        bindir = self.tmp / "silentbin"
        bindir.mkdir()
        for n in ("squeue", "sacct"):
            (bindir / n).write_text("#!/bin/sh\nexit 0\n")
            (bindir / n).chmod(0o755)
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        # Was hand-editing run.slurm_job_id into the contract, the workaround
        # that existed because `bind` did not. That field is digested now.
        rb = tc("bind", str(self.run_dir), "--job-id", "424242")
        self.assertEqual(rb.returncode, 0, rb.stderr)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        r = subprocess.run([sys.executable, str(SCRIPT), "check",
                            str(self.run_dir)], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("neither sacct nor squeue", r.stdout)

    def test_job_id_with_no_scheduler_record_but_local_record_passes(self):
        bindir = self.tmp / "silentbin2"
        bindir.mkdir()
        for n in ("squeue", "sacct"):
            (bindir / n).write_text("#!/bin/sh\nexit 0\n")
            (bindir / n).chmod(0o755)
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        # bind BEFORE record, which is the real order: sbatch, bind, the job
        # runs, then termination. `bind` refuses an already-terminal contract,
        # so recording first was the unrealistic part of this fixture.
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        rb = tc("bind", str(self.run_dir), "--job-id", "424242")
        self.assertEqual(rb.returncode, 0, rb.stderr)
        self.record_terminal()
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        r = subprocess.run([sys.executable, str(SCRIPT), "check",
                            str(self.run_dir)], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, CONVERGED, r.stdout)

    def test_truncated_metrics_cannot_converge(self):
        """luna: hitting the line cap silently dropped later rows, so a
        divergence beyond the cap could be excluded from a CONVERGED verdict."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("tc3", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        cap = m.MAX_METRICS_LINES
        # Write cap+2 lines cheaply by reusing one short record.
        line = json.dumps({"step": 1, "loss": 0.4})
        with self.metrics.open("w") as fh:
            for i in range(cap + 2):
                fh.write(json.dumps({"step": i, "loss": 0.4}) + "\n")
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        # The invariant is that it cannot converge. On a slow filesystem the
        # watchdog can fire before the truncation notice is reached -- chimera
        # ran this suite 15x slower than the laptop and tripped the 600s cap --
        # and that is ALSO fail-closed, so accept either reason rather than
        # pinning the one that happens to win locally.
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout[:400])
        out = r.stdout.lower()
        self.assertTrue("truncated" in out or "watchdog" in out,
                        f"expected a truncation or watchdog reason: {out[:300]}")


class TestFailClosedRound2(Base):
    def test_unknown_sacct_state_cannot_converge(self):
        """luna: enumerating bad states fails OPEN -- Slurm has states like
        SPECIAL_EXIT that no list covers, and they fell through leaving
        CONVERGED standing. Only COMPLETED now counts as success."""
        bindir = self.tmp / "sbin"
        bindir.mkdir()
        # Was a bare "SPECIAL_EXIT" with no ExitCode and no Submit column;
        # real sacct emits all three, and the missing Submit meant this test
        # stopped at the ownership check instead of reaching the state check.
        (bindir / "squeue").write_text("#!/bin/sh\nexit 0\n")
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        # Was hand-editing run.slurm_job_id into the contract, the workaround
        # that existed because `bind` did not. That field is digested now.
        rb = tc("bind", str(self.run_dir), "--job-id", "7")
        self.assertEqual(rb.returncode, 0, rb.stderr)
        # Submit pinned to the binding second, not `date` at check time: our
        # job is registered by the scheduler BEFORE we record its id, and a
        # later stamp is now correctly read as a different job reusing the id.
        bound = parse_local_iso(json.loads(
            (self.run_dir / "training-binding.json").read_text())["submitted_at"])
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(bound))
        (bindir / "sacct").write_text(
            # NOT SPECIAL_EXIT any more: that is now classified as a failure
            # in both verifiers, so it stopped being an unknown state.
            f'#!/bin/sh\necho "WARP_DRIVE_ENGAGED|0:0|{stamp}"\n')
        for n in ("sacct", "squeue"):
            (bindir / n).chmod(0o755)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        r = subprocess.run([sys.executable, str(SCRIPT), "check",
                            str(self.run_dir)], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("not a recognised successful terminal state", r.stdout)

    def test_oversized_line_skip_blocks_convergence(self):
        """Both reviewers, CRITICAL: an oversized line was skipped with no
        TRUNCATED marker, so a divergence hidden inside it vanished while
        CONVERGED stood."""
        big = json.dumps({"step": 1, "loss": 100.0, "pad": "x" * 1_200_000})
        self.metrics.write_text('{"step": 0, "loss": 0.4}\n' + big + "\n"
                                '{"step": 2, "loss": 0.4}\n')
        self.add_checkpoint()
        self.init("--diverge", json.dumps({"metric": "loss", "above": 50}),
                  converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("truncated", r.stdout.lower())

    def test_fifo_receipt_temp_path_does_not_hang(self):
        """luna: the temp name was predictable (.tmp-<pid>), so it was
        attackable the same way as the target. mkstemp closes that."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)
        leftovers = list(self.run_dir.glob("*.tmp*"))
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


class TestFailClosedRound3(Base):
    def test_malformed_metrics_line_blocks_convergence(self):
        """luna: skipped malformed lines only warned, so convergence could be
        certified from an incomplete series. Same class as the oversized-line
        fix -- any unread content means the evidence is partial."""
        self.metrics.write_text('{"step": 1, "loss": 1.0}\n'
                                'NOT JSON\n'
                                '{"step": 2, "loss": 0.4}\n')
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("SKIPPED", r.stdout)

    def test_clean_metrics_still_converge(self):
        self.write_metrics([{"step": 1, "loss": 1.0}, {"step": 2, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_record_uses_the_atomic_writer(self):
        """luna: cmd_record wrote its own temp with a predictable name and a
        blocking write_text, bypassing write_receipt entirely."""
        self.write_metrics([{"step": 1, "loss": 0.4}])
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        r = tc("record", str(self.run_dir), "--exit-code", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.run_dir / "training-termination.json").exists())
        self.assertEqual(list(self.run_dir.glob("*.tmp*")), [])

    def test_watchdog_option_exists_and_is_inert_normally(self):
        self.write_metrics([{"step": 1, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir), "--watchdog", "60")
        self.assertEqual(r.returncode, CONVERGED, r.stdout)
        self.assertNotIn("watchdog", r.stdout)


class TestFailClosedRound4(Base):
    def test_unusable_final_record_blocks_convergence(self):
        """luna: the blind-row span ended at the last METRIC-BEARING row, so an
        unusable FINAL record sat outside it and was ignored entirely."""
        self.metrics.write_text('{"step": 0, "loss": 1.0}\n'
                                '{"step": 1, "loss": 0.0}\n'
                                '{"step": 2}\n')
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_undecodable_bytes_block_convergence(self):
        """luna: errors="replace" let a non-UTF-8 line parse with U+FFFD and
        feed a CONVERGED verdict without any marker."""
        with self.metrics.open("wb") as fh:
            fh.write(b'{"step": 1, "loss": 0.4, "note": "\xff"}\n')
            fh.write(b'{"step": 2, "loss": 0.4}\n')
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("undecodable", r.stdout)

    def test_oversized_watchdog_does_not_crash(self):
        """luna: signal.alarm raises OverflowError on a huge value, so the
        anti-hang guard could itself exit without a verdict."""
        self.write_metrics([{"step": 1, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir), "--watchdog",
               "999999999999999999999")
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.returncode, CONVERGED, r.stdout)

    def test_run_kills_process_group_on_timeout(self):
        """Both reviewers: a PATH sacct wrapper forking a setsid descendant that
        holds the pipe kept the parent waiting -- including in Popen.__del__
        after the verdict was printed."""
        src = SCRIPT.read_text()
        self.assertIn("start_new_session=True", src)
        self.assertIn("killpg", src)


class TestVerdictLogicRound(Base):
    """The round where I redirected reviewers from adversarial input to verdict
    logic. All four findings were false-pass paths."""

    def _fake_sacct(self, state_line, submit=None):
        """state_line is "State|ExitCode"; a Submit column is appended.

        Two fixture bugs lived here. The column was once omitted entirely, so
        Submit parsed as None and the reuse check was skipped -- which is why
        six tests passed while an unparseable Submit was failing OPEN. Then it
        was computed with `date` at CHECK time, i.e. AFTER `bind`, which the
        real ordering never produces: sbatch registers the job, then bind
        records the id. With an upper bound on ownership that made honest rows
        look like a later reuse.

        So the default reads the Submit from a file that `_with_jid` fills in
        with the binding's own second, the tightest legitimate case. Pass
        `submit` explicitly for a specific instant.
        """
        b = self.tmp / f"sb{abs(hash((state_line, str(submit))))}"
        b.mkdir()
        if submit is None:
            (b / "submit").write_text("1970-01-01T00:00:00")   # _with_jid fills
            col = '$(cat "$(dirname "$0")/submit")'
        else:
            col = submit
        (b / "sacct").write_text(
            f'#!/bin/sh\necho "{state_line}|{col}"\n')
        (b / "squeue").write_text("#!/bin/sh\nexit 0\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        return b

    def _with_jid(self, bindir, job_id="5"):
        # Was rewriting run.slurm_job_id straight into the contract, which is
        # what a user had to do before `bind` existed. Use the real mechanism.
        r = tc("bind", str(self.run_dir), "--job-id", job_id)
        self.assertEqual(r.returncode, 0, r.stderr)
        # sacct's Submit must fall at or before the binding: our job was
        # registered by the scheduler before we recorded its id.
        sfile = bindir / "submit"
        if sfile.exists():
            b = json.loads(
                (self.run_dir / "training-binding.json").read_text())
            bound = parse_local_iso(b["submitted_at"])
            sfile.write_text(time.strftime("%Y-%m-%dT%H:%M:%S",
                                          time.localtime(bound)))
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        return subprocess.run([sys.executable, str(SCRIPT), "check",
                               str(self.run_dir)], capture_output=True,
                              text=True, env=env)

    def test_completed_with_nonzero_exit_code_cannot_converge(self):
        """deepseek, CRITICAL: the exit code was never fetched, so a COMPLETED
        row beside a non-zero code counted as terminal success."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        r = self._with_jid(self._fake_sacct("COMPLETED|1:0"))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("exit code", r.stdout)

    def test_completed_with_clean_exit_code_converges(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        r = self._with_jid(self._fake_sacct("COMPLETED|0:0"))
        self.assertEqual(r.returncode, CONVERGED, r.stdout)

    def test_stale_checkpoint_from_a_previous_run_cannot_converge(self):
        """luna: a reused checkpoint directory let an OLD artifact certify a new
        run that saved nothing. Same pre-existing-artifact false pass fixed in
        contract.py at round 2 and never considered here."""
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False,
                  stamp=False)
        # Metrics AFTER init so post-hoc detection does not fire first and mask
        # the checkpoint reason -- on lambda the two landed in different seconds.
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.record_terminal()
        self.add_checkpoint()
        old = time.time() - 7200
        os.utime(self.ckpt / "model-step-1000.pt", (old, old))
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        self.assertIn("predates this contract", r.stdout)

    def test_fresh_checkpoint_converges(self):
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        self.add_checkpoint()          # written after init
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)

    def test_stale_termination_record_is_ignored(self):
        """luna: the record was not bound to the contract, so a stale exit-0
        record survived `init --force` and certified a later crashed run."""
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        # Re-declare with DIFFERENT criteria; the old record must not carry over.
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--checkpoint-dir", str(self.ckpt), "--force",
               "--converge", json.dumps({"metric": "loss", "mode": "min",
                                         "threshold": 0.45, "min_steps": 0}))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        # Was asserting the word "stale". The message now names WHICH
        # fault it is, because reporting a contract mismatch for a record
        # that merely predates its evidence misdescribed the problem.
        self.assertIn("ignoring the termination record", r.stdout)


class TestInstanceBinding(Base):
    def test_identical_criteria_after_force_invalidates_the_record(self):
        """luna: the criteria digest alone matched after `init --force` with
        identical criteria, so a stale exit-0 record certified a crashed run."""
        crit = {"metric": "loss", "mode": "min", "threshold": 0.5,
                "min_steps": 0}
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.init(converge=crit)          # records termination
        self.add_checkpoint()
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)
        # Re-declare with the SAME criteria: digest identical, instance new.
        r = tc("init", str(self.run_dir), "--metrics", str(self.metrics),
               "--checkpoint-dir", str(self.ckpt), "--force",
               "--converge", json.dumps(crit))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, INCOMPLETE, r.stdout)
        # Was asserting the word "stale". The message now names WHICH
        # fault it is, because reporting a contract mismatch for a record
        # that merely predates its evidence misdescribed the problem.
        self.assertIn("ignoring the termination record", r.stdout)

    def test_each_init_gets_a_distinct_contract_id(self):
        self.write_metrics([{"step": 1, "loss": 0.4}])
        self.init()
        first = json.loads(
            (self.run_dir / "training-contract.json").read_text())
        tc("init", str(self.run_dir), "--metrics", str(self.metrics),
           "--checkpoint-dir", str(self.ckpt), "--force",
           "--converge", json.dumps(PLATEAU))
        second = json.loads(
            (self.run_dir / "training-contract.json").read_text())
        self.assertTrue(first["contract_id"])
        self.assertNotEqual(first["contract_id"], second["contract_id"])

    def test_completed_without_an_exit_code_cannot_converge(self):
        """luna: an absent ExitCode was treated as clean, so COMPLETED with no
        reported code counted as positive terminal evidence."""
        b = self.tmp / "noexit"
        b.mkdir()
        (b / "sacct").write_text("#!/bin/sh\necho 'COMPLETED|'\n")
        (b / "squeue").write_text("#!/bin/sh\nexit 0\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["run"]["slurm_job_id"] = "5"
        cpath.write_text(json.dumps(c))
        env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")
        r = subprocess.run([sys.executable, str(SCRIPT), "check",
                            str(self.run_dir)], capture_output=True,
                           text=True, env=env)
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)


class TestProvenanceTightening(Base):
    def test_freshness_helper_rejects_the_one_second_window(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("tc9", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertFalse(m.artifact_is_fresh(999, 1000))
        self.assertTrue(m.artifact_is_fresh(1000, 1000))

    def test_reused_job_id_row_is_rejected(self):
        b = self.tmp / "oldrow2"
        b.mkdir()
        (b / "sacct").write_text(
            "#!/bin/sh\necho 'COMPLETED|0:0|2020-01-01T00:00:00'\n")
        (b / "squeue").write_text("#!/bin/sh\nexit 1\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        self.write_metrics([{"step": 100, "loss": 0.4}])
        self.add_checkpoint()
        self.init(converge={"metric": "loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0}, record=False)
        cpath = self.run_dir / "training-contract.json"
        c = json.loads(cpath.read_text())
        c["run"]["slurm_job_id"] = "1000"
        cpath.write_text(json.dumps(c))
        env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")
        r = subprocess.run([sys.executable, str(SCRIPT), "check",
                            str(self.run_dir)], capture_output=True,
                           text=True, env=env)
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("reused", r.stdout)


class TestValidators(unittest.TestCase):
    """Direct tests of the shared validators, so the next instance of this bug
    class has one place to be caught."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("tcmod", SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_finite_number_rejects_non_finite(self):
        for bad in ("nan", "inf", "-inf", float("nan"), float("inf"),
                    1e309, "NaN", "Infinity"):
            with self.subTest(v=bad):
                _, err = self.m.finite_number(bad)
                self.assertIsNotNone(err, f"{bad!r} accepted")

    def test_finite_number_accepts_ordinary_values(self):
        for good in (0, -1, 0.5, "2.5", 1000000):
            with self.subTest(v=good):
                v, err = self.m.finite_number(good)
                self.assertIsNone(err)
                self.assertEqual(v, float(good))

    def test_nonneg_int_rejects_fractional_and_negative(self):
        for bad in (1.5, -1, "2.5", "oops", None, True, 1e309, float("nan")):
            with self.subTest(v=bad):
                _, err = self.m.nonneg_int(bad)
                self.assertIsNotNone(err, f"{bad!r} accepted")

    def test_nonneg_int_accepts_whole_numbers(self):
        for good in (0, 5, 5.0, "7"):
            with self.subTest(v=good):
                v, err = self.m.nonneg_int(good)
                self.assertIsNone(err)
                self.assertEqual(v, int(float(good)))


class TestPortability(unittest.TestCase):
    def test_stdlib_only(self):
        """AST-based, not textual. The line-scanning version read `import json,
        sys` out of a shell snippet this file emits as a STRING -- Python for a
        cluster job to run, not code this file imports -- and reported a
        non-stdlib module named "json,". Its twin in test_contract.py had the
        identical flaw; fixing one and not the other would have been the very
        sibling miss these suites exist to catch."""
        import ast
        allowed = {"argparse", "fnmatch", "calendar", "hashlib", "json",
                   "math", "os", "re", "shutil", "signal", "stat",
                   "subprocess", "sys", "tempfile", "time", "pathlib"}
        tree = ast.parse(SCRIPT.read_text())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        extra = sorted(found - allowed)
        self.assertFalse(extra, f"non-stdlib import(s): {', '.join(extra)}")
        self.assertGreater(len(found), 5,
                           "recovered too few imports to be measuring anything")

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestDigestCoversEveryDecidingField(Base):
    """luna: criteria_digest fingerprinted only converge/diverge/max_steps, so
    flipping sparse_metric after the run -- which decides whether a metric
    missing from a row disqualifies convergence -- changed the verdict without
    tripping post-hoc detection. Same for retargeting metrics_file or widening
    checkpoint_glob."""

    def contract_path(self):
        return self.run_dir / "training-contract.json"

    def edit(self, **fields):
        c = json.loads(self.contract_path().read_text())
        c.update(fields)
        self.contract_path().write_text(json.dumps(c, indent=2) + "\n")
        return c

    def test_sparse_metric_flip_is_detected(self):
        """The flip must be what changes the verdict, or the test proves
        nothing: a flat curve with a gap row, which sparse_metric decides."""
        rows = [{"step": s, "val_loss": 2.0} for s in range(100, 900, 100)]
        rows.append({"step": 450, "train_loss": 1.9})   # logged no val_loss
        rows.sort(key=lambda r: r["step"])
        self.write_metrics(rows)
        self.add_checkpoint()
        self.init()
        before = tc("check", str(self.run_dir))
        self.assertNotEqual(before.returncode, CONVERGED,
                            "the gap row breaks contiguous evidence")
        self.assertIn("not contiguous", before.stdout + before.stderr)
        # The flip alone would now certify convergence.
        self.edit(sparse_metric=True)
        after = tc("check", str(self.run_dir))
        self.assertNotEqual(after.returncode, CONVERGED,
                            "a post-run flag flip must not certify convergence")
        self.assertIn("changed after", after.stdout + after.stderr)

    def test_the_same_flag_declared_up_front_does_converge(self):
        """Proof the previous test is about provenance, not about the flag:
        declaring --sparse-metric BEFORE the run is legitimate."""
        rows = [{"step": s, "val_loss": 2.0} for s in range(100, 900, 100)]
        rows.append({"step": 450, "train_loss": 1.9})
        rows.sort(key=lambda r: r["step"])
        self.write_metrics(rows)
        self.add_checkpoint()
        self.init("--sparse-metric")
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_checkpoint_glob_widening_is_detected(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init()
        self.assertEqual(tc("check", str(self.run_dir)).returncode, CONVERGED)
        self.edit(checkpoint_glob="*")
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED)
        self.assertIn("changed after", r.stdout + r.stderr)

    def test_metrics_retarget_is_detected(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init()
        other = self.tmp / "other.jsonl"
        other.write_text(self.metrics.read_text())
        self.edit(metrics_file=str(other))
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED)

    def test_an_unrelated_edit_does_not_trip_it(self):
        """The digest exists so that editing a field the verdict does not
        depend on is NOT mistaken for post-hoc criterion selection."""
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init()
        self.edit(run={"hostname": "somewhere-else", "user": "someone"})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_a_contract_without_a_digest_cannot_certify(self):
        """Was asserting the opposite; see the sibling test in
        tests/test_contract.py. An absent digest made the mechanism opt-out."""
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init()
        c = json.loads(self.contract_path().read_text())
        del c["criteria_digest"]
        self.contract_path().write_text(json.dumps(c, indent=2) + "\n")
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout + r.stderr)
        self.assertIn("no criteria_digest", r.stdout + r.stderr)

    def test_nulling_the_digest_does_not_unlock_the_freshness_anchor(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(stamp=False)
        c = json.loads(self.contract_path().read_text())
        c["created_at_epoch"] = 0
        c["criteria_digest"] = None
        self.contract_path().write_text(json.dumps(c, indent=2) + "\n")
        for f in (self.metrics, *self.ckpt.glob("*")):
            os.utime(f, (1_000_000_000, 1_000_000_000))
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_backward_step_involving_an_out_of_budget_row_still_blocks(self):
        """luna: the structural check ran over the in-budget window, so an
        out-of-budget row sitting between two backward steps was dropped and
        the interleaving vanished. Rows 0, 101, 50 under a 100-step budget
        describe two runs however the budget falls."""
        self.write_metrics([{"step": 0, "val_loss": 2.0},
                            {"step": 101, "val_loss": 2.0},
                            {"step": 50, "val_loss": 0.01}])
        self.add_checkpoint()
        self.init("--max-steps", "100",
                  converge={"metric": "val_loss", "mode": "min",
                            "threshold": 0.5, "min_steps": 0})
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, VIOLATED, r.stdout + r.stderr)
        self.assertIn("monoton", r.stdout + r.stderr)


class TestTrainingSubSecondProvenance(Base):
    """luna: created_at has second resolution, so metrics written 0.2s BEFORE
    the contract compared equal once truncated and the criterion looked
    predictive when it was fitted."""

    def test_metrics_written_earlier_in_the_same_second_are_post_hoc(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(stamp=False)
        c = json.loads((self.run_dir / "training-contract.json").read_text())
        epoch = c["created_at_epoch"]
        self.assertIsInstance(epoch, float)
        stamp = int(epoch) + (epoch - int(epoch)) / 2
        self.assertEqual(int(stamp), int(epoch), "must be the same second")
        self.assertLess(stamp, epoch)
        for f in (self.metrics, *self.ckpt.glob("*")):
            os.utime(f, (stamp, stamp))
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED,
                            "a criterion declared after the curve existed "
                            "cannot establish convergence")

    def test_metrics_written_later_in_the_same_second_still_converge(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(stamp=False)
        c = json.loads((self.run_dir / "training-contract.json").read_text())
        epoch = c["created_at_epoch"]
        stamp = epoch + (1 - (epoch - int(epoch))) / 2
        self.assertEqual(int(stamp), int(epoch), "must be the same second")
        self.assertGreater(stamp, epoch)
        for f in (self.metrics, *self.ckpt.glob("*")):
            os.utime(f, (stamp, stamp))
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)


class TestIntegrityScoping(Base):
    """Two findings pulling in opposite directions, so both halves are pinned:
    order faults must be judged over the whole file, gaps only inside the
    budget. The first version blocked honest runs; the version before it
    certified interleaved ones."""

    def test_a_sparse_post_budget_row_does_not_fail_an_honest_run(self):
        """deepseek and luna, independently: after the budget, evaluations
        legitimately thin out or stop. Measuring them against
        expect_eval_every failed a run that had already converged."""
        # 5 in-budget evals: enough to judge a plateau over 3. With only 3 the
        # verdict was BUDGET_EXHAUSTED for lack of evidence, which said nothing
        # about the gap rule this test is actually about.
        rows = [{"step": s, "val_loss": 2.0} for s in (0, 25, 50, 75, 100)]
        rows.append({"step": 1000, "val_loss": 2.0})   # one late straggler
        self.write_metrics(rows)
        self.add_checkpoint()
        self.init("--max-steps", "100", "--expect-eval-every", "25")
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, VIOLATED, r.stdout + r.stderr)
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_a_gap_inside_the_budget_still_blocks(self):
        """The other half: within the declared window a missing evaluation
        means the curve is not contiguous evidence."""
        self.write_metrics([{"step": s, "val_loss": 2.0}
                            for s in (0, 50, 100, 400, 450, 500)])
        self.add_checkpoint()
        self.init("--max-steps", "500", "--expect-eval-every", "50")
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, VIOLATED, r.stdout + r.stderr)
        self.assertIn("gaps larger", r.stdout + r.stderr)

    def test_backward_steps_still_block_whichever_side_of_the_budget(self):
        for budget in ("100", "10000"):
            with self.subTest(budget=budget):
                self.setUp()
                self.write_metrics([{"step": 0, "val_loss": 2.0},
                                    {"step": 101, "val_loss": 2.0},
                                    {"step": 50, "val_loss": 0.01}])
                self.add_checkpoint()
                self.init("--max-steps", budget,
                          converge={"metric": "val_loss", "mode": "min",
                                    "threshold": 0.5, "min_steps": 0})
                r = tc("check", str(self.run_dir))
                self.assertEqual(r.returncode, VIOLATED, r.stdout + r.stderr)

class TestSacctRowOwnership(Base):
    """The realistic ordering, which my first implementation of this could not
    fail: sbatch submits, and `bind` runs SECONDS LATER. Comparing the sacct
    Submit against our own bind time had the direction inverted, so every
    honest submission was discarded as a reused job id. The verification that
    missed it generated the fake Submit at check time -- necessarily after bind
    -- so it was circular.

    Fixtures here pin Submit to an EXPLICIT time relative to the contract, never
    to `date` at check time.
    """

    def _sacct(self, line):
        b = self.tmp / f"sb{abs(hash(line))}"
        b.mkdir()
        (b / "sacct").write_text(f'#!/bin/sh\necho "{line}"\n')
        (b / "squeue").write_text("#!/bin/sh\nexit 0\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        return dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")

    def _iso_offset(self, seconds):
        """An ISO stamp `seconds` from the contract's declaration."""
        return time.strftime("%Y-%m-%dT%H:%M:%S",
                             time.localtime(self._contract_time() + seconds))

    def _check(self, env):
        return subprocess.run([sys.executable, str(SCRIPT), "check",
                               str(self.run_dir)], capture_output=True,
                              text=True, env=env)

    def setUpConverging(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)

    def test_submit_before_bind_but_after_declaration_is_ours(self):
        """THE case the inverted comparison broke: sbatch at +1s, bind at +3s.
        The row is honest and must count."""
        self.setUpConverging()
        submit = self._iso_offset(1)          # sbatch registers it at +1s
        # bind must land AFTER that, as it does in reality: sbatch returns the
        # id, then the user records it. The docstring said +3s; the code was
        # binding immediately, which is an ordering sbatch never produces.
        while time.time() < self._contract_time() + 2:
            time.sleep(0.1)
        r = tc("bind", str(self.run_dir), "--job-id", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self._check(self._sacct(f"COMPLETED|0:0|{submit}"))
        self.assertEqual(got.returncode, CONVERGED, got.stdout + got.stderr)

    def test_a_row_submitted_after_the_binding_is_a_later_reuse(self):
        """sol: ownership had only a lower bound, so a row from a LATER reuse
        of the id post-dated the declaration and was attributed to us. In
        traincontract that let a clean COMPLETED row from the other job certify
        a training run that had actually failed."""
        self.setUpConverging()
        r = tc("bind", str(self.run_dir), "--job-id", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        later = self._iso_offset(3600)        # the id reused an hour later
        got = self._check(self._sacct(f"COMPLETED|0:0|{later}"))
        self.assertNotEqual(got.returncode, CONVERGED, got.stdout)
        self.assertIn("later job", got.stdout)

    def test_submit_before_the_declaration_is_a_reused_id(self):
        self.setUpConverging()
        submit = self._iso_offset(-3600)
        tc("bind", str(self.run_dir), "--job-id", "5")
        got = self._check(self._sacct(f"COMPLETED|0:0|{submit}"))
        self.assertNotEqual(got.returncode, CONVERGED, got.stdout)
        self.assertIn("reused", got.stdout)

    def test_submit_in_the_declaration_second_is_accepted(self):
        """Deliberate, and a documented limit: sacct emits whole seconds, so a
        reuse inside that second cannot be told from an honest submission.
        Refusing it would reject every job submitted in its contract's second."""
        self.setUpConverging()
        submit = self._iso_offset(0)
        tc("bind", str(self.run_dir), "--job-id", "5")
        got = self._check(self._sacct(f"COMPLETED|0:0|{submit}"))
        self.assertEqual(got.returncode, CONVERGED, got.stdout + got.stderr)

    def test_absent_submit_column_fails_closed(self):
        self.setUpConverging()
        tc("bind", str(self.run_dir), "--job-id", "5")
        got = self._check(self._sacct("COMPLETED|0:0"))
        self.assertNotEqual(got.returncode, CONVERGED, got.stdout)
        self.assertIn("Submit", got.stdout)

    def test_unparseable_submit_fails_closed(self):
        self.setUpConverging()
        tc("bind", str(self.run_dir), "--job-id", "5")
        got = self._check(self._sacct("COMPLETED|0:0|not-a-timestamp"))
        self.assertNotEqual(got.returncode, CONVERGED, got.stdout)

    def test_init_inside_a_slurm_job_accepts_an_earlier_submit(self):
        """A job queued at 12:00:00 starts at 12:00:05, and init runs inside it,
        so created_at is 12:00:05 while the honest Submit is 12:00:00. Anchoring
        on created_at rejected the supported workflow; the declaration is the
        anchor and the row predates it, so this documents the residual gap
        rather than pretending it is closed."""
        self.setUpConverging()
        tc("bind", str(self.run_dir), "--job-id", "9")
        submit = self._iso_offset(-5)
        got = self._check(self._sacct(f"COMPLETED|0:0|{submit}"))
        # Fails closed, and says why, rather than certifying on a row it
        # cannot place. `record` is the documented way through.
        self.assertNotEqual(got.returncode, CONVERGED, got.stdout)


class TestBindDiscipline(Base):
    def test_bind_refuses_an_already_terminal_contract(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init()                        # records a local termination
        r = tc("bind", str(self.run_dir), "--job-id", "5")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already has a recorded termination", r.stderr)

    def test_force_overrides_the_terminal_refusal(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init()
        r = tc("bind", str(self.run_dir), "--job-id", "5", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_rebinding_the_same_id_preserves_the_first_timestamp(self):
        """Rewriting it made bind non-idempotent: a retry moved the anchor."""
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)
        self.assertEqual(tc("bind", str(self.run_dir), "--job-id", "7"
                            ).returncode, 0)
        first = json.loads(
            (self.run_dir / "training-binding.json").read_text())["submitted_at"]
        time.sleep(1.1)
        self.assertEqual(tc("bind", str(self.run_dir), "--job-id", "7"
                            ).returncode, 0)
        again = json.loads(
            (self.run_dir / "training-binding.json").read_text())["submitted_at"]
        self.assertEqual(first, again)

    def test_binding_a_different_id_is_refused_without_force(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)
        tc("bind", str(self.run_dir), "--job-id", "7")
        r = tc("bind", str(self.run_dir), "--job-id", "8")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already bound", r.stderr)

    def test_a_binding_from_another_contract_instance_is_ignored(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)
        tc("bind", str(self.run_dir), "--job-id", "7")
        bpath = self.run_dir / "training-binding.json"
        b = json.loads(bpath.read_text())
        b["contract_id"] = "0123456789abcdef"
        bpath.write_text(json.dumps(b))
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_bind_rejects_an_implausible_job_id(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)
        for bad in ("", "  ", "5; rm -rf /", "../../etc"):
            r = tc("bind", str(self.run_dir), "--job-id", bad)
            self.assertNotEqual(r.returncode, 0, repr(bad))

class TestBindPhase3Round2(Base):
    """Round-2 findings on the round-1 fixes. Both real, both in scope."""

    def test_a_stale_receipt_from_a_forced_reinit_does_not_block_bind(self):
        """luna: cmd_bind read read_termination() directly instead of filtering
        by contract instance, so a receipt left behind by `init --force`
        belonged to the PREVIOUS contract and refused to let the new honest run
        bind. Every other consumer of that file already filters."""
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init()                                  # contract A, with a receipt
        self.assertTrue((self.run_dir / "training-termination.json").exists())
        r = tc("init", str(self.run_dir), "--force",  # contract B, receipt stays
               "--metrics", str(self.metrics),
               "--checkpoint-dir", str(self.ckpt),
               "--converge", json.dumps(PLATEAU))
        self.assertEqual(r.returncode, 0, r.stderr)
        rb = tc("bind", str(self.run_dir), "--job-id", "42")
        self.assertEqual(rb.returncode, 0, rb.stderr)

    def test_a_receipt_for_THIS_contract_still_blocks_bind(self):
        """The other half: the guard must still do its job."""
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init()
        rb = tc("bind", str(self.run_dir), "--job-id", "42")
        self.assertNotEqual(rb.returncode, 0)
        self.assertIn("already has a recorded termination", rb.stderr)

    def test_job_id_must_look_like_a_slurm_id(self):
        """luna: any alphanumeric string was accepted, including "abc"."""
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)
        for good in ("12345", "12345_7", "12345.batch"):
            r = tc("bind", str(self.run_dir), "--job-id", good, "--force")
            self.assertEqual(r.returncode, 0, f"{good}: {r.stderr}")
        for bad in ("abc", "x1", "", "  ", "5; rm -rf /", "../../etc", "-1"):
            r = tc("bind", str(self.run_dir), "--job-id", bad, "--force")
            self.assertNotEqual(r.returncode, 0, repr(bad))

class TestTimezoneAwareTimestamps(Base):
    """deepseek CRITICAL and luna independently: time.strptime parses %z and
    time.mktime then discards it by reading the fields as local time, so the
    same instant written +0000, -0700 and +0200 produced three epochs nine
    hours apart. Every sacct row whose rendering did not match the verifier
    host was misplaced, in both directions."""

    def helper(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("tc_tz", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_the_same_instant_parses_identically_in_any_offset(self):
        m = self.helper()
        vals = [m.parse_iso_ts(s) for s in
                ("2024-08-12T15:05:00+0000", "2024-08-12T08:05:00-0700",
                 "2024-08-12T17:05:00+0200", "2024-08-12T15:05:00+00:00",
                 "2024-08-12T15:05:00.123+0000")]
        self.assertTrue(all(v is not None for v in vals), vals)
        self.assertEqual(len(set(vals)), 1, f"same instant, different epochs: {vals}")
        want = float(calendar.timegm(
            time.strptime("2024-08-12T15:05:00", "%Y-%m-%dT%H:%M:%S")))
        self.assertEqual(vals[0], want)

    def test_a_naive_timestamp_is_still_read_as_local(self):
        """sacct emits local time with no offset by default, so that reading
        must not change."""
        m = self.helper()
        naive = "2024-08-12T08:05:00"
        self.assertEqual(m.parse_iso_ts(naive),
                         time.mktime(time.strptime(naive, "%Y-%m-%dT%H:%M:%S")))

    def test_an_honest_utc_row_is_not_discarded_as_stale(self):
        """End to end: SLURM_TIME_FORMAT can render Submit in UTC while the
        contract's created_at carries the local offset. Before the fix those
        were compared across a 7-hour error."""
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)
        submit_utc = time.strftime("%Y-%m-%dT%H:%M:%S",
                                   time.gmtime(self._contract_time() + 1))
        # bind after the submission, as sbatch dictates.
        while time.time() < self._contract_time() + 2:
            time.sleep(0.1)
        tc("bind", str(self.run_dir), "--job-id", "5")
        b = self.tmp / "utcbin"
        b.mkdir()
        (b / "sacct").write_text(
            f'#!/bin/sh\necho "COMPLETED|0:0|{submit_utc}+0000"\n')
        (b / "squeue").write_text("#!/bin/sh\nexit 0\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")
        r = subprocess.run([sys.executable, str(SCRIPT), "check",
                            str(self.run_dir)], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)


class TestPartialCheckpointCase(Base):
    def test_uppercase_temp_suffix_is_still_partial(self):
        """luna: the suffix check was case-sensitive, so a framework staging
        `checkpoint-100.pt.TMP` had it counted as a complete checkpoint."""
        self.write_metrics(PLATEAU_ROWS)
        for name in ("model-100.pt.TMP", "model-100.pt.Part",
                     "model-100.pt.INCOMPLETE"):
            with self.subTest(name=name):
                self.setUp()
                self.write_metrics(PLATEAU_ROWS)
                (self.ckpt / name).write_bytes(b"x" * 1024)
                self.init()
                r = tc("check", str(self.run_dir))
                self.assertNotEqual(r.returncode, CONVERGED,
                                    f"{name}: {r.stdout}")

class TestTimestampsUnderForeignTimezones(Base):
    """A TZ sweep, because the nine-hour offset bug was invisible on a host
    whose local time matched what the fixtures emitted. Half-hour offsets are
    included deliberately: Adelaide is +0930, and an implementation that
    assumes whole-hour offsets passes every other case."""

    ZONES = ("UTC", "Asia/Tokyo", "America/New_York", "Australia/Adelaide",
             "Asia/Kolkata", "Pacific/Chatham")

    def helper(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("tc_tzsweep", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_one_instant_parses_the_same_in_every_zone(self):
        old = os.environ.get("TZ")
        try:
            results = {}
            for z in self.ZONES:
                os.environ["TZ"] = z
                time.tzset()
                m = self.helper()
                results[z] = m.parse_iso_ts("2024-08-12T15:05:00+0000")
            want = float(calendar.timegm(
                time.strptime("2024-08-12T15:05:00", "%Y-%m-%dT%H:%M:%S")))
            for z, got in results.items():
                self.assertEqual(got, want,
                                 f"{z}: an explicit offset must not depend on "
                                 f"the host zone")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_a_naive_stamp_tracks_the_host_zone_in_every_zone(self):
        """The other half: naive means local, so it MUST move with TZ. If this
        ever stops being true, the fixtures and sacct disagree again."""
        old = os.environ.get("TZ")
        try:
            seen = set()
            for z in self.ZONES:
                os.environ["TZ"] = z
                time.tzset()
                m = self.helper()
                v = m.parse_iso_ts("2024-08-12T15:05:00")
                self.assertEqual(
                    v, time.mktime(time.strptime("2024-08-12T15:05:00",
                                                 "%Y-%m-%dT%H:%M:%S")), z)
                seen.add(v)
            self.assertGreater(len(seen), 1,
                               "a naive stamp should differ between zones")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

class TestSqueueRowsAreOwnedToo(Base):
    """sol, round 3: ownership was applied to sacct rows for eight rounds and
    never to squeue rows. `-o %T` fetched only the state, so a LATER reuse of
    the id that happened to be queued turned an honestly finished run back into
    RUNNING, with its predicates never evaluated. Ninth instance in this repo of
    fixing one path and missing its sibling."""

    def _bin(self, squeue_line=None, sacct_line=None):
        b = self.tmp / f"qb{abs(hash((squeue_line, sacct_line)))}"
        b.mkdir()
        (b / "squeue").write_text(
            f'#!/bin/sh\necho "{squeue_line}"\n' if squeue_line
            else "#!/bin/sh\nexit 0\n")
        (b / "sacct").write_text(
            f'#!/bin/sh\necho "{sacct_line}"\n' if sacct_line
            else "#!/bin/sh\nexit 0\n")
        for n in ("squeue", "sacct"):
            (b / n).chmod(0o755)
        return dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")

    def _stamp(self, offset=0):
        b = json.loads(
            (self.run_dir / "training-binding.json").read_text())
        return time.strftime("%Y-%m-%dT%H:%M:%S",
                             time.localtime(parse_local_iso(b["submitted_at"])
                                            + offset))

    def _check(self, env):
        return subprocess.run([sys.executable, str(SCRIPT), "check",
                               str(self.run_dir)], capture_output=True,
                              text=True, env=env)

    def setUpBound(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)
        self.assertEqual(tc("bind", str(self.run_dir), "--job-id", "42"
                            ).returncode, 0)

    def test_a_later_reuse_sitting_in_the_queue_does_not_block_us(self):
        """THE case: our job finished, its id was reused hours later by a
        PENDING job, and squeue's row for that job used to overrule our owned
        terminal evidence."""
        self.setUpBound()
        env = self._bin(squeue_line=f"PENDING|{self._stamp(3600)}",
                        sacct_line=f"COMPLETED|0:0|{self._stamp(0)}")
        r = self._check(env)
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_our_own_queued_job_still_blocks(self):
        """The other half: a queue row inside the ownership window is ours and
        must still prevent a verdict."""
        self.setUpBound()
        env = self._bin(squeue_line=f"RUNNING|{self._stamp(0)}")
        r = self._check(env)
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("RUNNING", r.stdout)

    def test_a_queue_row_with_no_submit_time_is_not_ours(self):
        """Fails toward 'not active', which hands the decision to the sacct
        path -- which applies the same ownership test."""
        self.setUpBound()
        env = self._bin(squeue_line="RUNNING")
        r = self._check(env)
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)


class TestTensorFlowShardCompleteness(Base):
    """sol, round 3: a stale .index left by a previous run paired with ONE fresh
    shard passed as a complete checkpoint, because each file only needed *any*
    matching counterpart. The name states the expected count."""

    def test_a_partial_shard_set_is_not_a_checkpoint(self):
        self.write_metrics(PLATEAU_ROWS)
        (self.ckpt / "model.index").write_bytes(b"x" * 512)
        (self.ckpt / "model.data-00000-of-00002").write_bytes(b"x" * 4096)
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_a_complete_shard_set_is_a_checkpoint(self):
        self.write_metrics(PLATEAU_ROWS)
        (self.ckpt / "model.index").write_bytes(b"x" * 512)
        for i in range(2):
            (self.ckpt / f"model.data-0000{i}-of-00002").write_bytes(b"x" * 4096)
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_shards_disagreeing_on_the_total_are_refused(self):
        self.write_metrics(PLATEAU_ROWS)
        (self.ckpt / "model.index").write_bytes(b"x" * 512)
        (self.ckpt / "model.data-00000-of-00002").write_bytes(b"x" * 4096)
        (self.ckpt / "model.data-00001-of-00003").write_bytes(b"x" * 4096)
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_a_single_shard_checkpoint_still_works(self):
        self.write_metrics(PLATEAU_ROWS)
        (self.ckpt / "model.index").write_bytes(b"x" * 512)
        (self.ckpt / "model.data-00000-of-00001").write_bytes(b"x" * 4096)
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

class TestMixedGenerationCheckpointSet(Base):
    """kimi, after its output budget was raised and it could answer at all: the
    shard-completeness fix required every shard to be PRESENT but freshness was
    still checked only on the NEWEST file. A previous run's index and shard 1
    paired with one freshly written shard 0 is complete by count, fresh by
    newest, and loads to nothing. This was the half of sol's finding that the
    first fix missed."""

    def _set(self, stem="model.ckpt", shards=2):
        (self.ckpt / f"{stem}.index").write_bytes(b"x" * 512)
        for i in range(shards):
            (self.ckpt / f"{stem}.data-0000{i}-of-0000{shards}"
             ).write_bytes(b"x" * 4096)
        return stem

    def _age(self, *names, offset):
        when = self._contract_time() + offset
        for n in names:
            os.utime(self.ckpt / n, (when, when))

    def test_a_mixed_generation_set_cannot_converge(self):
        self.write_metrics(PLATEAU_ROWS)
        self._set()
        self.init()
        self._age("model.ckpt.index", "model.ckpt.data-00001-of-00002",
                  offset=-3600)                       # previous run
        self._age("model.ckpt.data-00000-of-00002", offset=2)   # this run
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_a_wholly_fresh_set_converges(self):
        self.write_metrics(PLATEAU_ROWS)
        self._set()
        self.init()
        self._age("model.ckpt.index", "model.ckpt.data-00000-of-00002",
                  "model.ckpt.data-00001-of-00002", offset=2)
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_a_wholly_stale_set_cannot_converge(self):
        self.write_metrics(PLATEAU_ROWS)
        self._set()
        self.init()
        self._age("model.ckpt.index", "model.ckpt.data-00000-of-00002",
                  "model.ckpt.data-00001-of-00002", offset=-3600)
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_a_stale_index_alone_is_enough_to_refuse(self):
        """The index carries no weights but the set is unloadable without it."""
        self.write_metrics(PLATEAU_ROWS)
        self._set()
        self.init()
        self._age("model.ckpt.data-00000-of-00002",
                  "model.ckpt.data-00001-of-00002", offset=2)
        self._age("model.ckpt.index", offset=-3600)
        r = tc("check", str(self.run_dir))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_a_single_file_checkpoint_is_its_own_set(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint("model-step-1000.pt")
        self.init()
        r = tc("check", str(self.run_dir))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

class TestMultiRowSqueue(Base):
    """deepseek: the squeue ownership check read only the FIRST row. squeue
    returns several for job arrays and, exactly in the case ownership exists
    for, when an id has been reused -- so a stale row printed first made this
    report "no live job" while our own live row sat unread below it. Missing a
    live job is how a still-running run gets certified.

    sacct_state has taken the LAST row for requeues since round 2. This path
    took the first, which is the eleventh instance of the same class."""

    def _squeue(self, *lines):
        b = self.tmp / f"mq{abs(hash(lines))}"
        b.mkdir()
        body = "\n".join(f'echo "{ln}"' for ln in lines)
        (b / "squeue").write_text(f"#!/bin/sh\n{body}\n")
        (b / "sacct").write_text("#!/bin/sh\nexit 0\n")
        for n in ("squeue", "sacct"):
            (b / n).chmod(0o755)
        return dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")

    def _stamp(self, offset):
        b = json.loads(
            (self.run_dir / "training-binding.json").read_text())
        return time.strftime("%Y-%m-%dT%H:%M:%S",
                             time.localtime(parse_local_iso(b["submitted_at"])
                                            + offset))

    def setUpBound(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)
        self.assertEqual(tc("bind", str(self.run_dir), "--job-id", "12345"
                            ).returncode, 0)

    def _check(self, env):
        return subprocess.run([sys.executable, str(SCRIPT), "check",
                               str(self.run_dir)], capture_output=True,
                              text=True, env=env)

    def test_our_live_row_below_a_stale_one_is_still_found(self):
        """THE case: the reused id's old row prints first, ours second."""
        self.setUpBound()
        env = self._squeue(f"COMPLETED|{self._stamp(-3600)}",
                           f"PENDING|{self._stamp(0)}")
        r = self._check(env)
        self.assertNotEqual(r.returncode, CONVERGED,
                            "our job is still queued; it cannot have converged")
        self.assertIn("PENDING", r.stdout)

    def test_all_rows_unownable_still_means_not_ours(self):
        self.setUpBound()
        env = self._squeue(f"PENDING|{self._stamp(-3600)}",
                           f"RUNNING|{self._stamp(7200)}")
        r = self._check(env)
        self.assertNotIn("PENDING", r.stdout)
        self.assertNotIn("RUNNING", r.stdout)

    def test_a_blank_line_in_the_output_is_skipped(self):
        self.setUpBound()
        env = self._squeue("", f"RUNNING|{self._stamp(0)}", "")
        r = self._check(env)
        self.assertIn("RUNNING", r.stdout)

    def test_the_last_owned_row_wins_for_a_requeue(self):
        """Matching sacct_state: a requeued job has one row per attempt and the
        latest is the live one."""
        self.setUpBound()
        env = self._squeue(f"PREEMPTED|{self._stamp(0)}",
                           f"RUNNING|{self._stamp(1)}")
        r = self._check(env)
        self.assertIn("RUNNING", r.stdout)

class TestTerminalSqueueRowIsNotActivity(Base):
    """deepseek: squeue keeps a finished job listed for MinJobAge (default
    300s), so for minutes after an honest run ends squeue still returns a row.
    Treating ANY owned row as still-active reported RUNNING and never evaluated
    the criterion -- a false negative on every check made inside that window.

    The previous comment defended treating all states as live because
    "enumerating live states missed real ones such as STAGE_OUT". Both
    properties are wanted, so TERMINAL is the enumerated set and anything not
    in it reads as active: an unknown state still fails toward not-finished."""

    def _bin(self, squeue_state, sacct_row=None):
        b = self.tmp / f"tq{abs(hash((squeue_state, sacct_row)))}"
        b.mkdir()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(self._contract_time()))
        (b / "squeue").write_text(
            f'#!/bin/sh\necho "{squeue_state}|{stamp}"\n')
        (b / "sacct").write_text(
            f'#!/bin/sh\necho "{sacct_row}|{stamp}"\n' if sacct_row
            else "#!/bin/sh\nexit 0\n")
        for n in ("squeue", "sacct"):
            (b / n).chmod(0o755)
        return dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")

    def setUpBound(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False)
        self.assertEqual(tc("bind", str(self.run_dir), "--job-id", "77"
                            ).returncode, 0)

    def _check(self, env):
        return subprocess.run([sys.executable, str(SCRIPT), "check",
                               str(self.run_dir)], capture_output=True,
                              text=True, env=env)

    def test_a_completed_squeue_row_does_not_block_the_verdict(self):
        self.setUpBound()
        r = self._check(self._bin("COMPLETED", "COMPLETED|0:0"))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_other_terminal_states_also_do_not_block(self):
        for st in ("CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY"):
            with self.subTest(state=st):
                self.setUp()
                self.setUpBound()
                r = self._check(self._bin(st, "COMPLETED|0:0"))
                self.assertEqual(r.returncode, CONVERGED,
                                 f"{st}: {r.stdout}")

    def test_a_live_state_still_blocks(self):
        self.setUpBound()
        r = self._check(self._bin("RUNNING"))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("RUNNING", r.stdout)

    def test_an_unknown_state_still_blocks(self):
        """The property the old behaviour was protecting: a state we have never
        seen must not be assumed finished."""
        self.setUpBound()
        r = self._check(self._bin("STAGE_OUT"))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)
        self.assertIn("STAGE_OUT", r.stdout)

    def test_a_requeue_state_still_reads_as_preempted(self):
        self.setUpBound()
        r = self._check(self._bin("REQUEUED"))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

class TestSchedulerRowMustPostdateItsEvidence(Base):
    """kimi, CRITICAL, and the SIBLING of the finding it gave in the same round:
    the ordering rule (a terminal record must post-date the evidence it
    certifies) was applied to the local termination record and never to the
    sacct terminal row. So a Slurm job that COMPLETED at T=10 certified metrics
    and checkpoints written at T=20 -- run training locally in a bound run
    directory and it borrowed the Slurm job's success.

    Fifteenth instance of a rule reaching one path and not its twin, and the
    second where the twin was named in the same review."""

    def _bin(self, submit_off, end_off):
        b = self.tmp / f"eb{abs(hash((submit_off, end_off)))}"
        b.mkdir()
        base = self._contract_time()
        sub = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base + submit_off))
        end = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base + end_off))
        (b / "sacct").write_text(
            f'#!/bin/sh\necho "COMPLETED|0:0|{sub}|{end}"\n')
        (b / "squeue").write_text("#!/bin/sh\nexit 0\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        return dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")

    def _age_artifacts(self, offset):
        when = self._contract_time() + offset
        for f in [self.metrics, *self.ckpt.glob("*")]:
            os.utime(f, (when, when))

    def setUpBound(self):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=False, stamp=False)
        self.assertEqual(tc("bind", str(self.run_dir), "--job-id", "55"
                            ).returncode, 0)

    def _check(self, env):
        return subprocess.run([sys.executable, str(SCRIPT), "check",
                               str(self.run_dir)], capture_output=True,
                              text=True, env=env)

    def test_a_job_that_ended_before_the_metrics_is_noted(self):
        """Was asserting it cannot certify. Enforced for one round, then
        reversed: ordering cannot say WHICH attempt produced an artifact, an
        archiving script touching a checkpoint after the run inverts it on an
        honest run, and an absent End skips it. All three from the reviewers
        that asked for the rule in the first place. Reported, not enforced."""
        self.setUpBound()
        self._age_artifacts(20)
        r = self._check(self._bin(submit_off=0, end_off=10))
        self.assertIn("ended before", r.stdout)
        self.assertIn("note:", r.stdout)

    def test_a_post_run_touch_does_not_refuse_an_honest_run(self):
        """deepseek: a post-processing or sync script that reads and touches a
        checkpoint advances the evidence past the job's End. Routine on a
        shared filesystem, and it must not turn a converged run into
        INCOMPLETE_EVIDENCE."""
        self.setUpBound()
        self._age_artifacts(10)
        env = self._bin(submit_off=0, end_off=20)
        self.assertEqual(self._check(env).returncode, CONVERGED)
        self._age_artifacts(900)          # the archiver touches it
        r = self._check(env)
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_the_honest_order_converges(self):
        """A job writes its metrics and then ends."""
        self.setUpBound()
        self._age_artifacts(10)
        r = self._check(self._bin(submit_off=0, end_off=20))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_an_absent_end_column_does_not_refuse_an_honest_run(self):
        """Not every sacct build populates End. Absent, there is nothing to
        order against, and refusing on that would break honest clusters."""
        self.setUpBound()
        self._age_artifacts(10)
        b = self.tmp / "noend"
        b.mkdir()
        sub = time.strftime("%Y-%m-%dT%H:%M:%S",
                            time.localtime(self._contract_time()))
        (b / "sacct").write_text(f'#!/bin/sh\necho "COMPLETED|0:0|{sub}"\n')
        (b / "squeue").write_text("#!/bin/sh\nexit 0\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")
        r = self._check(env)
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)

    def test_a_failed_row_is_not_subject_to_the_ordering_rule(self):
        """A FAILED job is bad news whenever it ended: ordering only gates
        whether a row can CERTIFY, never whether it can condemn."""
        self.setUpBound()
        self._age_artifacts(20)
        b = self.tmp / "failed"
        b.mkdir()
        base = self._contract_time()
        sub = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base))
        end = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base + 10))
        (b / "sacct").write_text(f'#!/bin/sh\necho "FAILED|1:0|{sub}|{end}"\n')
        (b / "squeue").write_text("#!/bin/sh\nexit 0\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        r = self._check(dict(os.environ, PATH=f"{b}:{os.environ['PATH']}"))
        self.assertEqual(r.returncode, VIOLATED, r.stdout + r.stderr)

class TestLaterLocalRetryDecidesHereToo(Base):
    """kimi: the SLURM_BAD_END branch set CONTRACT_VIOLATED and ignored a
    matching local termination record entirely, so an honest local retry after
    a failed batch job could never recover. contract.py already had the rule
    that the latest attempt decides; this was its sibling, unapplied.

    Sixteenth instance of a rule reaching one verifier and not the other."""

    def _sacct(self, row):
        b = self.tmp / f"lr{abs(hash(row))}"
        b.mkdir(exist_ok=True)      # the same row is used twice in one test
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(self._contract_time()))
        (b / "sacct").write_text(f'#!/bin/sh\necho "{row}|{stamp}|{stamp}"\n')
        (b / "squeue").write_text("#!/bin/sh\nexit 0\n")
        for n in ("sacct", "squeue"):
            (b / n).chmod(0o755)
        return dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")

    def _check(self, env):
        return subprocess.run([sys.executable, str(SCRIPT), "check",
                               str(self.run_dir)], capture_output=True,
                              text=True, env=env)

    def setUpBound(self, record=False):
        self.write_metrics(PLATEAU_ROWS)
        self.add_checkpoint()
        self.init(record=record)
        self.assertEqual(tc("bind", str(self.run_dir), "--job-id", "91"
                            ).returncode, 0)

    def test_a_failed_job_alone_still_violates(self):
        self.setUpBound(record=False)
        r = self._check(self._sacct("FAILED|1:0"))
        self.assertEqual(r.returncode, VIOLATED, r.stdout + r.stderr)
        self.assertIn("record --exit-code 0", r.stdout)

    def test_a_later_local_retry_recovers(self):
        self.setUpBound(record=False)
        self.assertEqual(self._check(self._sacct("FAILED|1:0")).returncode,
                         VIOLATED)
        self.record_terminal()
        r = self._check(self._sacct("FAILED|1:0"))
        self.assertEqual(r.returncode, CONVERGED, r.stdout + r.stderr)
        self.assertIn("later local run was recorded", r.stdout)

    def test_a_failed_local_retry_does_not_recover(self):
        self.setUpBound(record=False)
        self.record_terminal(exit_code=3)
        r = self._check(self._sacct("FAILED|1:0"))
        self.assertNotEqual(r.returncode, CONVERGED, r.stdout)

    def test_a_record_for_another_contract_does_not_recover(self):
        self.setUpBound(record=False)
        self.record_terminal()
        tpath = self.run_dir / "training-termination.json"
        rec = json.loads(tpath.read_text())
        rec["contract_id"] = "0123456789abcdef"
        tpath.write_text(json.dumps(rec))
        r = self._check(self._sacct("FAILED|1:0"))
        self.assertEqual(r.returncode, VIOLATED, r.stdout + r.stderr)

class TestUnreadCriterionKeys(unittest.TestCase):
    """Sibling of the contract.py predicate-key tests. Both tools must behave
    identically: typo refused, annotation accepted, underscored READ key
    refused as a typo rather than ignored."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.metrics = self.tmp / "m.jsonl"
        self.metrics.write_text("")

    def init_with(self, crit):
        return tc("init", str(self.tmp / f"r{abs(hash(str(crit)))}"),
                     "--metrics", str(self.metrics),
                     "--checkpoint-dir", str(self.tmp / "ck"),
                     "--converge", json.dumps(crit))

    BASE = {"metric": "val_loss", "mode": "min", "threshold": 0.5}

    def test_a_typod_key_is_refused(self):
        r = self.init_with({**self.BASE, "min_step": 10})
        self.assertNotEqual(r.returncode, 0,
                            "min_step accepted; min_steps would default and "
                            "the criterion would be silently weakened")
        self.assertIn("min_steps", r.stderr)

    def test_the_reserved_note_key_is_accepted(self):
        r = self.init_with({**self.BASE, "note": "picked from the v3 sweep"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_underscore_spelling_of_a_read_key_gets_through(self):
        """Sibling of the contract.py test. `_min_steps_` strips to
        "min_steps_" under lstrip('_') and passed as an annotation while
        `min_steps` fell back to its default (deepseek-v4-pro)."""
        for key in ("_min_steps", "min_steps_", "_min_steps_",
                    "__min_steps__", "min_step", "Min_Steps"):
            with self.subTest(key=key):
                r = self.init_with({**self.BASE, key: 10})
                self.assertNotEqual(
                    r.returncode, 0,
                    f"{key!r} was accepted; min_steps would default and the "
                    f"criterion would be silently weakened")

    def test_a_correct_criterion_is_still_accepted(self):
        r = self.init_with({**self.BASE, "min_steps": 10})
        self.assertEqual(r.returncode, 0, r.stderr)



class TestTrainingProductionEvidence(unittest.TestCase):
    """Sibling miss #20's twin: CONVERGED, exit 0, for metrics and a checkpoint
    written by something OTHER than the run. `metrics_unchanged` catches only a
    byte-identical file, and freshness proves "written after the contract", not
    "written by this run"."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "ck").mkdir()
        self.metrics = self.tmp / "m.jsonl"

    def declare(self, *extra):
        return tc("init", str(self.tmp), "--metrics", str(self.metrics),
                  "--checkpoint-dir", str(self.tmp / "ck"),
                  "--converge",
                  json.dumps({"metric": "val_loss", "mode": "min",
                              "threshold": 0.5}), *extra)

    def write_foreign_metrics(self):
        time.sleep(1.1)
        self.metrics.write_text('{"step":100,"val_loss":0.4}\n'
                                '{"step":200,"val_loss":0.3}\n')
        (self.tmp / "ck" / "ckpt-200.pt").write_text("fake\n")

    def test_metrics_this_run_did_not_write_are_refused(self):
        self.assertEqual(self.declare("--require-production-evidence").returncode,
                         0)
        self.write_foreign_metrics()
        tc("record", str(self.tmp), "--exit-code", "0")
        r = tc("check", str(self.tmp))
        self.assertEqual(
            r.returncode, 6,
            f"a criterion met over rows another process wrote returned exit "
            f"{r.returncode}:\n{r.stdout}")
        self.assertIn("production evidence", r.stdout)

    def test_a_contract_without_the_flag_is_unchanged(self):
        self.assertEqual(self.declare().returncode, 0)
        self.write_foreign_metrics()
        tc("record", str(self.tmp), "--exit-code", "0")
        r = tc("check", str(self.tmp))
        self.assertEqual(r.returncode, 0,
                         f"an existing contract's behaviour changed:\n"
                         f"{r.stdout}")

    def test_the_window_judges_growth_not_time(self):
        """A training run's evidence is an APPEND-ONLY file, so the window must
        judge rows gained, not appearance and not mtime. This is why the
        bracket was written separately rather than copied from contract.py."""
        import importlib.util as u
        spec = u.spec_from_file_location("t", SCRIPT)
        m = u.module_from_spec(spec)
        spec.loader.exec_module(m)
        snip = m.training_window_snippet("/tmp/m.jsonl", "/tmp/ck", "/tmp/e.json")
        self.assertIn("wc -l", snip)
        self.assertIn("grew_in_window", snip)
        for word in ("mtime", "date +", "stat -f"):
            self.assertNotIn(word, snip, f"the window uses {word!r}, which is "
                                         f"time-based attribution")

    def test_every_helper_the_new_code_calls_actually_exists(self):
        """I wrote read_text_bounded into this file, which has
        read_json_bounded, and read_json_bounded into contract.py, which has
        read_text_bounded. The same missing-callee error twice, in opposite
        directions, because I assumed a helper existed since the TWIN had it.
        py_compile cannot see it and test_symmetry only checks COPIED helpers.

        So: import the module and CALL the new functions."""
        import importlib.util as u
        spec = u.spec_from_file_location("t2", SCRIPT)
        m = u.module_from_spec(spec)
        spec.loader.exec_module(m)
        # No evidence file present: must return a fault string, not raise.
        fault = m.training_production_fault(
            self.tmp, {"require_production_evidence": True})
        self.assertIsInstance(fault, str)
        self.assertIn("production evidence", fault)
        # Not required: must return None.
        self.assertIsNone(m.training_production_fault(self.tmp, {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
