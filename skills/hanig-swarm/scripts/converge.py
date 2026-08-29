#!/usr/bin/env python3
"""converge.py: did the training actually converge, or just stop?

A swarm predicate. `unit.py` answers "did the job end cleanly and produce its
declared outputs"; that is existence and terminal state, and for a training run
it is not enough. A run that executes 40,000 steps, exits 0 and writes a
checkpoint is DONE by that predicate even if the loss was flat for the last
30,000 of them.

WHY THIS EXISTS SEPARATELY. Two reasons, both worth stating.

Shreshth's repo has no counterpart. Checked directly: every apparent hit is a
false positive -- `bin/bus`'s "epoch seconds" is a Unix timestamp, `vllm_load`
scrapes GPU load, and `start-a-sprint`'s "convergence" is a REVIEWER agreeing a
diff is fixed. Nothing there scores a numeric criterion over a series, and
`paseo-loop`'s two verification shapes cannot: a shell check answers "exit 0"
and "checkpoint exists", never "did val_loss improve by more than 0.002 over the
last 5 evaluations at or beyond step 10,000". That is not an oversight -- his
domain is code changes, where done is a commit that passes tests. His predicates
are git-shaped; a loss curve needs a different instrument.

And it is separate from `unit.py` on purpose. The unit contract carries a size
guard ("if the surviving module grows past ~300 lines, stop"), and convergence is
a predicate over a metrics SERIES, not part of deciding whether a job ran. A
`slurm` unit opts in by declaring a criterion; units that do not declare one
never touch this code.

WHAT IT REFUSES TO DO. It does not decide a unit's state -- it answers one
question and returns an exit code, and `unit.py` or the coordinator decides what
that means. It does not read a checkpoint. It does not smooth, extrapolate or
infer: a criterion is met by the recorded numbers or it is not.

    converge.py check METRICS.jsonl --criterion JSON [--diverge JSON] [--budget N]

    0 CONVERGED          the declared criterion is met
    1 NOT_YET            not met, and the budget is not spent
    2 DIVERGED           a declared divergence rule fired
    3 BUDGET_EXHAUSTED   the step budget is spent and the criterion is unmet
                         -- the distinction this exists for
    4 INCOMPLETE         cannot judge: unreadable, unusable values, or a
                         criterion that cannot be interpreted

The evaluator below is lifted VERBATIM from traincontract.py, which earned it:
NaN bounds that made every comparison silently false, 10**399 values that are
finite JSON and overflow float, `min_step` typos that silently defaulted, and a
plateau rule that certified convergence over a single evaluation.

Python 3.8+, stdlib only, login-node safe.
"""

import argparse
import json
import math
import os
import re
import stat
import sys
import time
from pathlib import Path

STATES = {"CONVERGED": 0, "NOT_YET": 1, "DIVERGED": 2,
          "BUDGET_EXHAUSTED": 3, "INCOMPLETE": 4}
# Constants the LIFT needs. Caught by the closure check the previous
# lift taught me to run -- callees, imports AND constants -- rather than
# by a real job failing on a cluster.
MAX_METRICS_BYTES = 512 * 1024 * 1024
MAX_METRICS_LINES = 5_000_000
MAX_METRICS_LINE_BYTES = 1_000_000

CONVERGE_KEYS = frozenset({"metric", "mode", "threshold",
                           "rel_improvement_below", "over_evals", "min_steps"})


DIVERGE_KEYS = frozenset({"metric", "above", "below"})


ANNOTATION_KEYS = frozenset({"note"})


def read_metrics(path):
    """Parse JSONL metrics. Returns (rows, problems). Never raises."""
    rows, problems, lines = [], [], []
    p = Path(path)
    try:
        if not p.exists():
            return [], [f"metrics file not found: {path}"]
        # Open once and fstat the descriptor: stat-then-reopen let the path be
        # swapped for a FIFO in between, and the read would block forever.
        # O_NONBLOCK means even a FIFO open returns instead of hanging.
        fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return [], [f"metrics path is not a regular file "
                            f"({stat.filemode(st.st_mode)}): {path}"]
            if st.st_size > MAX_METRICS_BYTES:
                return [], [f"metrics file is {st.st_size} bytes, above the "
                            f"{MAX_METRICS_BYTES}-byte limit; evaluate less "
                            f"often or split the file"]
            with os.fdopen(fd, "r", encoding="utf-8", errors="replace",
                           closefd=True) as fh:
                fd = None  # fdopen owns it now
                # Stream: splitlines() on a file of newlines under the byte cap
                # still built a list of hundreds of millions of entries.
                lines = []
                for i, line in enumerate(fh, 1):
                    if i > MAX_METRICS_LINES:
                        problems.append(
                            f"TRUNCATED: metrics file has more than "
                            f"{MAX_METRICS_LINES} lines; later evaluations were "
                            f"not read, so contradictory evidence may exist "
                            f"beyond the cap. Evaluate less often or split the "
                            f"file")
                        break
                    if len(line) > MAX_METRICS_LINE_BYTES:
                        problems.append(
                            f"TRUNCATED: line {i} is {len(line)} bytes, above "
                            f"the {MAX_METRICS_LINE_BYTES}-byte per-line limit "
                            f"and was skipped; it may have contained "
                            f"contradictory evidence")
                        continue
                    if line.strip():
                        lines.append((i, line))
        finally:
            if fd is not None:
                os.close(fd)
    except (OSError, MemoryError) as e:
        return [], [f"metrics file unreadable: {type(e).__name__}: {e}"]

    for i, line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, RecursionError, ValueError, MemoryError):
            problems.append(f"SKIPPED: line {i} is not valid JSON")
            continue
        if not isinstance(rec, dict):
            problems.append(f"SKIPPED: line {i} is not an object")
            continue
        if "\ufffd" in line:
            # errors="replace" kept the line readable, but its original bytes
            # were not valid UTF-8, so its content cannot be trusted.
            problems.append(f"SKIPPED: line {i} contained undecodable bytes")
            continue
        if "step" not in rec:
            problems.append(f"SKIPPED: line {i} has no 'step' key")
            continue
        try:
            raw_step = rec["step"]
            if isinstance(raw_step, bool):
                raise ValueError("boolean step")
            if isinstance(raw_step, int):
                rec["step"] = raw_step  # exact; never route through float
            else:
                step_val = float(raw_step)
                if math.isnan(step_val) or math.isinf(step_val):
                    raise ValueError("non-finite step")
                if step_val != int(step_val):
                    # Truncating 100.9 to 100 silently moved out-of-budget
                    # evidence onto the budget boundary.
                    raise ValueError("fractional step")
                rec["step"] = int(step_val)
        except (TypeError, ValueError, OverflowError):
            problems.append(f"SKIPPED: line {i} 'step' is not a finite integer")
            continue
        rows.append(rec)
    return rows, problems


def metric_series_with_problems(rows, key):
    """As metric_series, plus the values that could not be used.

    Silently dropping an unusable value let it slip past a declared divergence
    ceiling: a loss of 10**399 is finite JSON, overflows float, and simply
    vanished. Unusable values are reported instead."""
    out, problems = [], []
    if not isinstance(key, str):
        return [], [f"metric name is not a string: {key!r}"]
    for r in rows:
        if key not in r:
            continue
        v = r[key]
        if isinstance(v, bool):
            problems.append(f"step {r['step']}: {key} is a boolean")
            continue
        if not isinstance(v, (int, float)):
            try:
                shown = repr(v)[:80]
            except (RecursionError, Exception):
                shown = f"<unrepresentable {type(v).__name__}>"
            problems.append(f"step {r['step']}: {key}={shown} is not numeric")
            continue
        try:
            fv = float(v)  # a 400-digit int is valid JSON and overflows here
        except (OverflowError, ValueError):
            problems.append(f"step {r['step']}: {key} is too large to evaluate")
            continue
        # Python's json parser accepts the non-standard literals Infinity,
        # -Infinity and NaN. An infinite value clears ANY threshold, so a run
        # that blew up numerically was reported CONVERGED: a false pass, which
        # is the failure this whole family of tools exists to prevent. NaN is
        # equally dangerous in the other direction, since every comparison
        # against it is False.
        if fv != fv or fv in (float("inf"), float("-inf")):
            problems.append(
                f"step {r['step']}: {key} is {v}, which is not a finite "
                f"number. A run producing it has diverged numerically; it "
                f"cannot be judged converged.")
            continue
        out.append((r["step"], fv))
    return sorted(out, key=lambda x: x[0]), problems


def finite_number(v):
    """(value, error). Rejects NaN and infinity: a NaN bound makes every
    comparison silently false, which reads as "rule satisfied" and can produce
    a false CONVERGED."""
    try:
        fv = float(v)
    except (TypeError, ValueError, OverflowError):
        return None, f"not a number: {v!r}; use a plain number, e.g. 0.5"
    if math.isnan(fv) or math.isinf(fv):
        return None, f"must be finite: {v!r}; use a real bound, not NaN or infinity"
    return fv, None


def nonneg_int(v):
    """(value, error). Rejects fractional values rather than truncating them --
    min_steps of 1.5 became 1 and allowed convergence below the declared
    minimum -- and catches the OverflowError from int(float('inf'))."""
    if isinstance(v, bool):
        return None, f"must be an integer: {v!r}; use a whole number of steps"
    if isinstance(v, int):
        return (v, None) if v >= 0 else (None, f"must not be negative: {v!r}; use 0 or more")
    try:
        fv = float(v)
    except (TypeError, ValueError, OverflowError):
        return None, f"not an integer: {v!r}; use a whole number of steps"
    if math.isnan(fv) or math.isinf(fv):
        return None, f"must be finite: {v!r}; use a real bound, not NaN or infinity"
    if fv != int(fv):
        return None, f"must be a whole number, not {v!r}"
    iv = int(fv)
    return (iv, None) if iv >= 0 else (None, f"must not be negative: {v!r}; use 0 or more")


def unread_key_problem(crit, allowed, what):
    """Why a criterion's key set is not interpretable, or None.

    ONE reserved annotation key, not a naming convention. The first version
    allowed any underscore-prefixed key; deepseek-v4-pro broke it with
    `_min_steps_`, which strips to "min_steps_", is not a read key, and so
    passed as an annotation while `min_steps` fell back to its default -- the
    defect this exists to stop, through the hatch added to fix it. A closed set
    has no boundary left to probe."""
    readable = ", ".join(sorted(allowed - ANNOTATION_KEYS))
    unknown = sorted(set(crit) - allowed - ANNOTATION_KEYS)
    if unknown:
        return (f"{what} has unrecognised key(s) {', '.join(unknown)}; it "
                f"reads only {readable}. A typo here would silently weaken "
                f"the criterion. Put any commentary in "
                f"{sorted(ANNOTATION_KEYS)[0]!r}, which is recorded and never "
                f"evaluated.")
    return None


def criterion_problem(crit):
    """Why a convergence criterion cannot be evaluated, or None.

    Checked at init as well as at check: a criterion with a typo in it used to
    be accepted, then crash the verifier when it was finally evaluated."""
    if not isinstance(crit, dict):
        return ('not a JSON object; pass one JSON object, e.g. '
                '{"metric":"val_loss","mode":"min","threshold":0.5}')
    if not isinstance(crit.get("metric"), str) or not crit["metric"].strip():
        return (f"'metric' must be a non-empty string, got "
                f"{crit.get('metric')!r}; use the metric's name exactly as the "
                f"metrics file writes it")
    if crit.get("mode", "min") not in ("min", "max"):
        return (f"mode must be 'min' or 'max', got {crit.get('mode')!r}; "
                f"use 'min' for a loss, 'max' for an accuracy")
    if "threshold" not in crit and "rel_improvement_below" not in crit:
        return ("needs either 'threshold' or 'rel_improvement_below'; add "
                "one, e.g. \"threshold\": 0.5 for an absolute target, or "
                "\"rel_improvement_below\": 0.002 for a plateau")
    # The action comes from finite_number / nonneg_int, which now name one.
    # Restated here because `err` is too generic a name for the action check to
    # exempt: elsewhere it holds a raw exception, which never names an action.
    for key in ("threshold", "rel_improvement_below"):
        if key in crit:
            _, err = finite_number(crit[key])
            if err:
                return f"'{key}' {err} (use a finite number)"
    for key in ("over_evals", "min_steps"):
        if key in crit:
            _, err = nonneg_int(crit[key])
            if err:
                return f"'{key}' {err} (use a whole number of 0 or more)"
    if "rel_improvement_below" in crit:
        n, err = nonneg_int(crit.get("over_evals", 5))
        if err or n < 1:
            return ("'over_evals' must be a whole number of at least 1; "
                    "set it to the number of evaluations the plateau must "
                    "hold for, or omit it to use the default of 5")
    # Enumerate the keys this criterion is READ with. Every known key above
    # was validated, but an unknown one was silently ignored, so a typo
    # (`min_step` for `min_steps`) left the real criterion WEAKER than the
    # declared one with no warning. Same defect found in contract.py's
    # predicates by real use on lambda; fixed in both, with the same
    # underscore-annotation hatch in both.
    problem = unread_key_problem(crit, CONVERGE_KEYS, "this criterion")
    if problem:
        return problem
    return None


def eval_convergence(rows, crit, sparse_ok=False):
    """Evaluate a pre-declared stopping criterion. Returns (met, detail).

    Shape:
      {"metric": "val_loss", "mode": "min",
       "rel_improvement_below": 0.002, "over_evals": 5, "min_steps": 10000}
    or
      {"metric": "val_auroc", "mode": "max", "threshold": 0.9,
       "min_steps": 5000}
    """
    key = crit.get("metric")
    if not key:
        return False, "criterion names no metric"
    series, unusable = metric_series_with_problems(rows, key)
    if unusable:
        # Certifying convergence while an evaluation of the very metric being
        # judged could not be read is certifying from incomplete evidence.
        return False, (f"{len(unusable)} unusable value(s) for {key!r} "
                       f"({unusable[0]}); cannot certify convergence. Fix the "
                       f"writer so it emits finite numbers for this metric, or "
                       f"re-declare against a metric it writes cleanly.")
    if not series:
        return False, f"no numeric values for {key!r}"

    if not sparse_ok:
        # Rows inside the judged span that carry no value for this metric mean
        # the window is not contiguous evidence: a bad evaluation that failed
        # to log is indistinguishable from one that was never scheduled.
        first = series[0][0]
        last = max(r["step"] for r in rows)   # last ROW, not last metric row:
        have = {s for s, _ in series}          # an unusable final record was
        blind = [r["step"] for r in rows       # otherwise outside the span
                 if first <= r["step"] <= last and r["step"] not in have]
        if blind:
            return False, (
                f"{len(blind)} evaluation(s) between steps {first} and {last} "
                f"carry no {key!r} value (e.g. step {blind[0]}); the window is "
                f"not contiguous evidence. Log {key!r} on every evaluation, or "
                f"declare --sparse-metric if it is logged less often by design.")

    mode = crit.get("mode", "min")
    if mode not in ("min", "max"):
        return False, f"mode must be 'min' or 'max', got {mode!r}"
    last_step = series[-1][0]
    min_steps, err = nonneg_int(crit.get("min_steps", 0))
    if err:
        return False, f"min_steps {err}"
    if last_step < min_steps:
        return False, (f"only {last_step} steps; criterion requires "
                       f"{min_steps} before it may be met")

    if "threshold" in crit:
        want, err = finite_number(crit["threshold"])
        if err:
            return False, f"threshold {err}"
        cur = series[-1][1]
        ok = cur >= want if mode == "max" else cur <= want
        return ok, (f"{key}={cur:g} vs threshold {want:g} ({mode}) at step "
                    f"{last_step}")

    rel = crit.get("rel_improvement_below")
    if rel is None:
        return False, "criterion has neither 'threshold' nor 'rel_improvement_below'"
    rel, err = finite_number(rel)
    if err:
        return False, f"rel_improvement_below {err}"
    n, err = nonneg_int(crit.get("over_evals", 5))
    if err:
        return False, f"over_evals {err}"
    if n < 1:
        return False, f"over_evals must be >= 1, got {n}"
    if len(series) < n + 1:
        return False, (f"only {len(series)} evaluations of {key!r}; need "
                       f"{n + 1} to judge a plateau over {n}")
    window = series[-(n + 1):]
    best_before = window[0][1]
    improvements = []
    for _, v in window[1:]:
        delta = (best_before - v) if mode == "min" else (v - best_before)
        if best_before == 0:
            # A zero baseline has no relative scale. Only remaining exactly at
            # zero is a plateau; ANY movement -- better or worse -- means no
            # relative plateau can be established. Recording 0.0 for a
            # worsening metric was the previous bug.
            improvements.append(0.0 if v == 0 else float("inf"))
        else:
            improvements.append(delta / abs(best_before))
        best_before = min(best_before, v) if mode == "min" else max(best_before, v)
    if not improvements:
        return False, "convergence window is empty (over_evals must be >= 1)"
    if any(math.isinf(x) for x in improvements):
        return False, (f"{key} moved away from a zero baseline inside the "
                       f"window; no relative plateau can be established")
    # Largest movement in EITHER direction. Using max() alone let one small
    # early gain mask a large later regression, so a metric that ended worse
    # than it started still read as a plateau. A plateau means it barely moved.
    swing = max(abs(x) for x in improvements)
    worst_drop = min(improvements)
    met = swing < rel
    detail = (f"{key} largest relative movement over last {n} evals is "
              f"{swing:.4%} (worst single change {worst_drop:+.4%}), "
              f"criterion < {rel:.4%}")
    if not met and worst_drop < 0 and abs(worst_drop) >= rel:
        detail += "; the metric regressed, which is not a plateau"
    return met, detail


def eval_divergence(rows, rules):
    """Declared blow-up conditions, scanned over EVERY evaluation.

    Checking only the latest value let a mid-run breach disappear as soon as the
    metric recovered -- but a run that hit loss=200 and came back down did
    diverge, and the receipt has to say so."""
    breaches = []
    for rule in rules or []:
        # Type first: reading rule.get() before this guard is what let
        # diverge:[1] raise AttributeError out of check.
        bounds, unevaluable = {}, False
        if not isinstance(rule, dict):
            breaches.append(f"unusable divergence rule {rule!r}; the run cannot "
                            f"be shown to have stayed within it")
            continue
        key = rule.get("metric")
        # Validate the RULE before touching the data: putting this after the
        # empty-series check let an unusable bound on a never-emitted metric
        # slip through with no breach at all.
        if not isinstance(key, str) or not key:
            breaches.append(f"unusable divergence rule {rule!r}; the run cannot "
                            f"be shown to have stayed within it")
            continue
        if not any(k in rule for k in ("above", "below")):
            breaches.append(f"divergence rule for {key!r} declares no bound; "
                            f"the run cannot be shown to have stayed within it")
            continue
        for k in ("above", "below"):
            if k in rule:
                bv, err = finite_number(rule[k])
                if err:
                    breaches.append(f"divergence rule for {key!r} has an "
                                    f"unusable '{k}' bound ({err}); the run "
                                    f"cannot be shown to have stayed within it")
                    unevaluable = True
                else:
                    bounds[k] = bv
        if unevaluable:
            continue
        series, unusable = metric_series_with_problems(rows, key)
        for u in unusable:
            breaches.append(f"{u} -- cannot evaluate its declared ceiling")
        if not series:
            # A declared bound with no data behind it was never checked. The run
            # cannot be shown to have stayed under it, so it is not satisfied.
            breaches.append(f"divergence rule declares a bound on {key!r} but "
                            f"the metrics contain no usable values for it; the "
                            f"rule was never evaluated")
            continue
        for step, val in series:
            if "above" in bounds and val > bounds["above"]:
                breaches.append(f"{key}={val:g} exceeded {bounds['above']:g} "
                                f"at step {step}")
                break
            if "below" in bounds and val < bounds["below"]:
                breaches.append(f"{key}={val:g} fell below {bounds['below']:g} "
                                f"at step {step}")
                break
    return breaches


# ==========================================================================
# Everything above was lifted from traincontract.py, which has since been
# deleted, so there is no longer a twin to drift from. It is edited only to fix
# a defect in the lifted behaviour itself, never to adapt it to swarm: the
# non-finite guard in metric_series_with_problems is such a fix, and closed a
# false CONVERGED. Everything below is the swarm predicate around it.
# ==========================================================================

# How long the metrics file must be QUIET before "the budget is spent"
# may be reported as "the run stopped". Below this, a run that just
# logged its final budgeted step is still alive.
BUDGET_QUIET_S = 900


def judge(metrics_path, criterion, diverge_rules, budget, sparse_ok=False):
    """Answer ONE question and return (state_name, [reasons]).

    Pure with respect to the verdict: it reads the metrics file and nothing
    else, decides nothing about the unit, and never writes."""
    reasons = []
    rows, rerr = read_metrics(metrics_path)
    if rerr:
        return "INCOMPLETE", [f"cannot read {metrics_path}: {rerr}. Point "
                              f"--metrics at the file the run appends to."]
    if not rows:
        return "INCOMPLETE", [f"{metrics_path} has no usable rows yet, so there "
                              f"is nothing to judge. Wait for the run to write "
                              f"an evaluation."]

    # Divergence FIRST. A run that blew up and then coincidentally satisfied a
    # threshold has not converged, and reporting convergence over it would be
    # the worst kind of false pass here.
    if diverge_rules:
        for i, rule in enumerate(diverge_rules):
            problem = unread_key_problem(rule, DIVERGE_KEYS,
                                         f"divergence rule {i}")
            if problem:
                return "INCOMPLETE", [problem]
        # eval_divergence returns a LIST of breaches, not (bool, detail). I
        # assumed the signature instead of reading it, which is the same
        # mistake as assuming a helper exists because a sibling has one.
        breaches = eval_divergence(rows, diverge_rules)
        if breaches:
            return "DIVERGED", list(breaches)
        reasons.append(f"no divergence rule fired ({len(diverge_rules)} checked)")

    problem = criterion_problem(criterion)
    if problem:
        return "INCOMPLETE", [f"unusable convergence criterion: {problem}"]

    # A non-finite value in the TRACKED metric is divergence by direct
    # measurement, and it must be said so rather than dropped. The series
    # builder already refuses to admit inf or NaN, which stops the false
    # CONVERGED; without this branch the refusal showed up as NOT_YET, telling
    # an operator to keep burning GPU-hours on a run that had blown up.
    _, series_problems = metric_series_with_problems(rows, criterion["metric"])
    nonfinite = [q for q in series_problems if "not a finite number" in q]
    if nonfinite:
        return "DIVERGED", nonfinite + [
            f"{criterion['metric']} left the finite range, so no threshold "
            f"comparison against it is meaningful."]

    met, detail = eval_convergence(rows, criterion, sparse_ok)
    reasons.append(detail)
    if met:
        return "CONVERGED", reasons

    # The distinction this module exists for. Without it, a flat run that spent
    # its whole budget reads as "not yet" forever, or worse, as done.
    if budget is not None:
        last = None
        for r in rows:
            step = r.get("step")
            if isinstance(step, (int, float)) and not isinstance(step, bool):
                last = step
        if last is not None and last >= float(budget):
            # "The run stopped" is a claim about the JOB, and the last logged
            # step alone cannot support it: a healthy run that has just written
            # step 40000 of a 40000 budget is still going and may log again in
            # seconds. Asserting it had stopped was an over-claim of exactly
            # the kind this repo keeps having to walk back, so require that the
            # metrics file has also gone quiet.
            try:
                quiet_for = time.time() - Path(metrics_path).stat().st_mtime
            except OSError:
                quiet_for = None
            if quiet_for is not None and quiet_for < BUDGET_QUIET_S:
                reasons.append(
                    f"step {last:g} has reached the {budget}-step budget, but "
                    f"{metrics_path} was written {int(quiet_for)}s ago and the "
                    f"run may still be going. Not calling it stopped yet; "
                    f"re-check after {BUDGET_QUIET_S}s of silence.")
                return "NOT_YET", reasons
            silence = (f", and the metrics file has not been written for "
                       f"{int(quiet_for)}s" if quiet_for is not None else "")
            reasons.append(f"reached the {budget}-step budget at step {last:g} "
                           f"without meeting the criterion{silence}. This is "
                           f"NOT convergence: the run stopped, it did not "
                           f"finish.")
            return "BUDGET_EXHAUSTED", reasons
    return "NOT_YET", reasons


def cmd_check(args):
    crit, diverge = {}, []
    try:
        if args.criterion:
            crit = json.loads(args.criterion)
        for d in (args.diverge or []):
            diverge.append(json.loads(d))
    except json.JSONDecodeError as e:
        sys.exit(f"error: criterion is not valid JSON: {e}. Pass one JSON "
                 f'object, e.g. --criterion \'{{"metric":"val_loss",'
                 f'"mode":"min","threshold":0.5}}\'')
    if not crit:
        sys.exit("error: no --criterion given, so there is nothing to judge. "
                 "Declaring the criterion BEFORE the run is the whole point; "
                 "pass one, or do not use this predicate.")

    state, reasons = judge(args.metrics, crit, diverge, args.budget,
                           args.sparse_metric)
    if args.json:
        print(json.dumps({"state": state, "exit_code": STATES[state],
                          "metrics": str(args.metrics), "criterion": crit,
                          "diverge_rules": diverge, "budget": args.budget,
                          "reasons": reasons,
                          "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                         indent=2, sort_keys=True))
    else:
        print(f"{state}  ({args.metrics})")
        for r in reasons:
            print(f"  - {r}")
    return STATES[state]


def main():
    ap = argparse.ArgumentParser(
        prog="converge.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="did it converge, or just stop?")
    c.add_argument("metrics")
    c.add_argument("--criterion", required=True,
                   help='e.g. \'{"metric":"val_loss","mode":"min",'
                        '"rel_improvement_below":0.002,"over_evals":5,'
                        '"min_steps":10000}\'')
    c.add_argument("--diverge", action="append", default=[],
                   help='e.g. \'{"metric":"train_loss","above":1e9}\'; '
                        'repeatable. Checked BEFORE convergence.')
    c.add_argument("--budget", type=float, default=None,
                   help="step budget; reaching it unmet is BUDGET_EXHAUSTED, "
                        "which is not convergence")
    c.add_argument("--sparse-metric", action="store_true",
                   help="tolerate rows that carry no value for the metric")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
