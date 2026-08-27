#!/usr/bin/env python3
"""traincontract.py — declare convergence before training, verify it after.

"The model trained" hides four different outcomes that tooling reports
identically:

    converged          a stopping criterion declared in advance was met
    budget exhausted   the wall-clock or step limit arrived first
    diverged           loss went to NaN/inf, or blew past a declared ceiling
    preempted          the scheduler took the node back

Only the first is success, and it is the only one you cannot determine after the
fact without lying to yourself: picking the best checkpoint once you have seen
the curve is legitimate, but it is *selection*, not convergence, and the receipt
says which one happened.

Usage:
    traincontract.py init  <run-dir> --metrics FILE --checkpoint-dir DIR
                           [--converge JSON] [--diverge JSON] [--max-steps N]
    traincontract.py record <run-dir> --exit-code N   # a run not under Slurm
    traincontract.py check  <run-dir> [--json]

Exit codes:
    0  CONVERGED           a pre-declared criterion was met
    1  RUNNING             still going
    2  DIVERGED            NaN/inf, or a declared ceiling breached
    3  BUDGET_EXHAUSTED    limit reached without meeting the criterion
    4  CONTRACT_VIOLATED   metrics unusable: non-monotonic steps, gaps, drift
    5  PREEMPTED           requeued; another attempt expected
    6  INCOMPLETE_EVIDENCE cannot determine -- no metrics, or no readable ckpt

Metrics format: JSONL, one object per evaluation, each with at least a "step"
and the metric keys the criterion names. Deliberately not tied to W&B or
TensorBoard -- a login node with no network still has to be able to verify.

Python 3.7+, standard library only.
"""

import argparse
import calendar
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1
CONTRACT = "training-contract.json"
VERIFICATION = "training-verification.json"
TERMINATION = "training-termination.json"
BINDING = "training-binding.json"

STATES = {"CONVERGED": 0, "RUNNING": 1, "DIVERGED": 2, "BUDGET_EXHAUSTED": 3,
          "CONTRACT_VIOLATED": 4, "PREEMPTED": 5, "INCOMPLETE_EVIDENCE": 6}

# Checkpoint files that are still being written must not be mistaken for a
# usable artifact; these suffixes are how the common frameworks stage them.
PARTIAL_SUFFIXES = (".tmp", ".part", ".partial", ".incomplete", ".lock", ".writing")

# A README is not a model. Only files that plausibly ARE checkpoints count as
# evidence that something loadable exists; --checkpoint-glob overrides this.
CHECKPOINT_SUFFIXES = (".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".h5",
                       ".hdf5", ".pkl", ".pickle", ".npz", ".msgpack", ".onnx",
                       ".gguf", ".pdparams")

# TensorFlow checkpoints are a PAIR: an .index plus one or more
# .data-NNNNN-of-NNNNN shards sharing its stem. Neither half alone is loadable,
# so neither counts on its own.
TF_SHARD = re.compile(r"\.data-(\d+)-of-(\d+)$")

# Documentation, logs and configs live beside checkpoints and must never be
# mistaken for one, however they are named.
NON_MODEL_SUFFIXES = (".txt", ".log", ".md", ".json", ".yaml", ".yml", ".csv",
                      ".tsv", ".out", ".err", ".ini", ".cfg", ".toml", ".html")

# A metrics file this large is a bug in the writer, not a run worth verifying;
# reading it unbounded risks an OOM kill before any receipt is written.
MAX_METRICS_BYTES = 512 * 1024 * 1024
# A byte cap alone does not bound memory: a file of newlines under the cap
# still materialises one list entry per line.
MAX_METRICS_LINES = 5_000_000
# One enormous but valid JSON line could still exhaust memory inside
# json.loads, outside any guard. Bound the line, not just the file.
MAX_METRICS_LINE_BYTES = 1_000_000

def arm_watchdog(seconds, on_timeout):
    """Emit a verdict and exit if the whole run overruns.

    deepseek is right that O_NONBLOCK does not save an open() on a hung NFS
    mount, and there is no portable way to open a file with a timeout. A SIGALRM
    watchdog is the honest mitigation: an interrupted syscall becomes a verdict
    instead of an unbounded hang. Not available on Windows; there it is a no-op.
    """
    if not hasattr(signal, "SIGALRM") or seconds <= 0:
        return

    def _fire(_signum, _frame):
        on_timeout(seconds)

    try:
        # int(seconds) can exceed the C int alarm() takes; clamp rather than
        # let the anti-hang guard crash without a verdict.
        secs = max(1, min(int(seconds), 86_400))
        signal.signal(signal.SIGALRM, _fire)
        signal.alarm(secs)
    except (OSError, ValueError, OverflowError, TypeError):
        pass


def disarm_watchdog():
    if hasattr(signal, "SIGALRM"):
        try:
            signal.alarm(0)
        except (OSError, ValueError):
            pass



def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run(argv, timeout=30):
    """Run a command, returning (rc, stdout). Never raises, never hangs.

    start_new_session + killpg: a PATH-provided sacct/squeue wrapper can fork a
    descendant that inherits the captured pipe, and killing only the direct
    child leaves the parent waiting on EOF -- including in Popen.__del__ after
    the verdict has been printed."""
    pr = None
    try:
        pr = subprocess.Popen(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8",
                              errors="replace", start_new_session=True)
        out, _ = pr.communicate(timeout=timeout)
        return pr.returncode, (out or "").strip()
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(pr.pid), signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                pr.kill()
            except OSError:
                pass
        try:
            pr.communicate(timeout=10)
        except Exception:
            pass
        return 127, ""
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return 127, ""


SLURM_NOT_DONE = ("PREEMPTED", "REQUEUED", "SUSPENDED", "RESIZING")
SLURM_ACTIVE = ("RUNNING", "PENDING", "CONFIGURING", "COMPLETING")
SLURM_BAD_END = ("FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
                 "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "REVOKED",
                 "SPECIAL_EXIT")
# The ONLY states that count as successful termination. Anything unrecognised
# is treated as unconfirmed rather than assumed good.
SLURM_OK_END = ("COMPLETED",)

# States in which Slurm has FINISHED with a job. squeue keeps a finished job
# listed for MinJobAge (default 300s), so for minutes after an honest run ends
# squeue still returns a row -- and treating any owned row as "still active"
# made the verifier report RUNNING and never evaluate the predicates
# (deepseek). Terminal is the enumerable set; anything NOT here reads as
# active, so an unrecognised state still fails toward "not finished" rather
# than certifying a live job. That keeps the property the previous comment was
# protecting ("enumerating live states missed real ones such as STAGE_OUT")
# while no longer blocking a completed one.
# DERIVED from the classification sets, never listed again: listing it
# separately drifted within one commit in the sibling verifier, where
# SPECIAL_EXIT was added to the terminal set and not to the failure set, so a
# row in that state matched nothing and fell through to RUNNING forever.
SLURM_TERMINAL_IN_QUEUE = frozenset(set(SLURM_OK_END) | set(SLURM_BAD_END)
                                    | {"SPECIAL_EXIT"})


# Both our ISO stamps and sacct's Submit are whole seconds, so each can sit up
# to a second either side of the real instant. Comparing them exactly refused
# honest rows: sbatch at 12:00:00.999 records 12:00:00 while sacct rounds Submit
# to 12:00:01 (deepseek). One second of slack on each bound, no more.
OWNERSHIP_SLACK_S = 1


def terminal_record_postdates(record_ts, newest_evidence_mtime):
    """Whether a terminal record can speak for this evidence.

    A record written BEFORE the artifacts it certifies belongs to an earlier
    run. Reusing a run directory is the ordinary way this happens: init once,
    train, `record --exit-code 0`; then train again in the same directory, and
    the first run's record certified the second run's metrics because the
    contract_id still matched (kimi). Ownership said WHICH contract; nothing
    said WHEN.

    The honest flow always orders correctly -- the run finishes, then it is
    recorded -- so requiring it costs nothing and refusing a record that
    predates its own evidence costs at most a re-record. One second of slack,
    since both sides can be whole seconds.
    """
    if record_ts is None or newest_evidence_mtime is None:
        return True                    # nothing to order against
    return int(record_ts) + OWNERSHIP_SLACK_S >= int(newest_evidence_mtime)


def sacct_row_is_ours(sacct_submit, declared_at, bound_at=None):
    """Whether an sacct row can be evidence about the job WE submitted.

    Returns (ok, why_not). An INTERVAL, which is all the evidence there is:

        declared_at - slack  <=  Submit  <=  bound_at + slack

    Both bounds are load-bearing and each alone failed in the opposite
    direction. Lower only: a row from a LATER reuse also post-dates the
    declaration, which let a later clean COMPLETED row certify a run that had
    exited non-zero (sol; a false pass). Upper only: `bind` runs after
    `sbatch`, so the honest Submit is always earlier than the moment we
    recorded, and every real submission was discarded.

    A previous version tried to remove the interval by asking sacct for the
    job's own Submit when the id was recorded, and comparing against that. It
    was UNSOUND and sol found it: if the id had already been reused before that
    query ran, the query returns the OTHER job's row and it becomes the
    "exact" anchor, after which the reused row matches itself and certifies the
    run. The anchor was drawn from the very source it was meant to validate.
    There is no way to establish after the fact which row is ours, so the
    interval is the honest maximum and its limits are documented rather than
    papered over.

    KNOWN LIMITS: a reuse landing inside the interval is indistinguishable
    from our own submission (luna), so bind promptly -- the window is exactly
    the gap between submitting and binding. And a reuse within one second of
    our submission is indistinguishable regardless, because sacct emits whole
    seconds and the slack is a second wide.
    """
    if declared_at is None:
        return False, ("the contract carries no usable declaration time, so an "
                       "sacct row cannot be placed relative to it")
    if sacct_submit is None:
        # Fails CLOSED. Skipping the check when Submit would not parse let an
        # old COMPLETED row for a reused id certify a new run.
        return False, ("sacct reported no usable Submit time, so its row "
                       "cannot be confirmed to describe this contract's job "
                       "rather than another job reusing the id. This fails "
                       "CLOSED on purpose, and it does refuse an honest run "
                       "whose sacct build omits Submit: declare the outcome "
                       "with `record` instead, or set SLURM_TIME_FORMAT so "
                       "Submit is populated")
    if int(sacct_submit) < int(declared_at) - OWNERSHIP_SLACK_S:
        return False, ("the sacct row was submitted before this contract was "
                       "declared, so the job id has been reused and the row "
                       "describes an earlier job")
    if (bound_at is not None
            and int(sacct_submit) > int(bound_at) + OWNERSHIP_SLACK_S):
        return False, ("the sacct row was submitted after this contract's job "
                       "id was recorded, so the id has been reused and the row "
                       "describes a later job")
    return True, None


# Digest at most this much of a metrics file. A metrics file is text and
# normally small; a prefix settles the question for anything larger, because a
# run that appended to it changed the prefix's length if not its bytes.
METRICS_DIGEST_LIMIT = 64 << 20


def file_fingerprint(path, limit=METRICS_DIGEST_LIMIT):
    """(sha256, size) for a file, or None. Bounded, and never raises."""
    h = hashlib.sha256()
    total = 0
    try:
        st = os.stat(str(path))
        if not stat.S_ISREG(st.st_mode):
            return None
        with open(str(path), "rb") as fh:
            while total < limit:
                chunk = fh.read(min(1 << 20, limit - total))
                if not chunk:
                    break
                total += len(chunk)
                h.update(chunk)
    except OSError:
        return None
    return {"sha256": h.hexdigest(), "size": st.st_size}


def metrics_unchanged_since_declaration(contract):
    """Whether the metrics file is byte-identical to the one that was already
    there when the contract was declared. None when unknowable.

    mtime is the only cheap provenance signal a filesystem gives, and `touch`
    defeats it: a converged curve from a previous run, touched after init,
    passed post-hoc detection and certified CONVERGED (kimi, CRITICAL). The
    accidental form matters more than the adversarial one, since `cp` without
    -p updates mtimes. Content settles it.
    """
    fp = contract.get("preexisting_metrics")
    if not isinstance(fp, dict) or not fp.get("sha256"):
        return None                # nothing was there, or an older contract
    now = file_fingerprint(contract.get("metrics_file", ""))
    if now is None:
        return None
    if now["size"] != fp.get("size"):
        return False
    return now["sha256"] == fp["sha256"]


def artifact_is_fresh(mtime, declared_at, declared_epoch=None):
    """Whether an artifact could have been produced by this contract instance.

    `mtime + 1 < declared_at` left a one-second window in which pre-existing
    evidence passed. ISO `created_at` has only second resolution, so an artifact
    written 0.2s BEFORE the contract still compared equal once truncated
    (luna); `created_at_epoch` carries the sub-second part.

    A whole-second mtime is a filesystem that truncated it, so the artifact
    could be from anywhere inside that second, including before the contract.
    Accepting it re-opened the same hole for coarse filesystems (luna), so it
    must land in a strictly LATER second. The cost is a run that both starts
    and finishes inside the declaration second, on a filesystem with no
    sub-second mtimes, reporting INCOMPLETE_EVIDENCE; the alternative is
    certifying a stale artifact, which this tool exists to refuse.
    """
    if mtime is None:
        return True
    if declared_epoch is not None:
        try:
            m, d = float(mtime), float(declared_epoch)
        except (TypeError, ValueError):
            return True
        if m == int(m):
            return int(m) > int(d)
        return m >= d
    if declared_at is None:
        return True                       # nothing to compare against
    return int(mtime) >= int(declared_at)


def contract_epoch(contract):
    """Sub-second declaration time, or None when it cannot be trusted.

    None on contracts written before the field existed. Bools are ints in
    Python, so they are excluded explicitly. NaN and infinity are excluded for
    the reason finite_number gives: every comparison against NaN is false, so a
    NaN here would silently judge every artifact stale.
    """
    v = (contract or {}).get("created_at_epoch")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def exit_code_is_clean(code):
    """Whether an sacct ExitCode ("<exit>:<signal>") POSITIVELY shows success.

    An absent code is not evidence of a clean exit; assuming it was let
    COMPLETED-with-no-code count as terminal success (luna)."""
    if code is None or str(code).strip() == "":
        return False
    # Enumerate the good shape rather than the bad ones. Skipping empty
    # components made ":" and "0:" read as clean, because `all()` over the
    # surviving parts was vacuously true (luna).
    parts = [p.strip() for p in str(code).strip().split(":")]
    if len(parts) > 2 or any(p == "" for p in parts):
        return False
    try:
        return all(int(p) == 0 for p in parts)
    except ValueError:
        return False


def squeue_state(job_id, declared_at=None, bound_at=None):
    """State of OUR job from squeue, or None. sacct has no row for a job that
    has not started, so squeue is the only thing that knows it is still queued.

    Ownership applies here as it does to an sacct row, and for eight rounds it
    did not: `-o %T` fetched only the state, so a later reuse of the id sitting
    PENDING turned a converged run with owned terminal evidence back into
    RUNNING (sol). %V is the submit time, which is what places the row."""
    rc, out = run(["squeue", "-h", "-j", str(job_id), "-o", "%T|%V"])
    if rc != 0 or not out:
        return None
    # EVERY row, not just the first, and the LAST owned one: squeue returns
    # several for job arrays and for a reused id, so a stale row printed first
    # made this return None while our own live row sat unread below it
    # (deepseek). Missing a live job is how a still-running run gets certified.
    found = None
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        submit = parse_iso_ts(parts[1].strip()) if len(parts) > 1 else None
        ours, _why = sacct_row_is_ours(submit, declared_at, bound_at)
        if ours:
            state = parts[0].strip()
            # An owned row whose state is TERMINAL is not evidence of activity:
            # squeue lists a finished job for MinJobAge, and reporting it as
            # live turned an honest converged run into RUNNING (deepseek).
            if state and state.split()[0].upper() in SLURM_TERMINAL_IN_QUEUE:
                continue
            found = state or found
    # None means no owned row that is still live; reporting an unattributable
    # one would let another job's queue state overrule evidence we own.
    return found


def slurm_state(job_id, declared_at=None, bound_at=None,
                newest_evidence_mtime=None, note_out=None):
    """Terminal state of OUR job from sacct, or None when unavailable.

    Ownership is applied to EVERY row and the last OWNED one wins. Taking
    rows[-1] and testing only that discarded an honest converged run's row
    whenever a later job reused the id, because the reuse's row sorts last and
    fails the test (kimi). The squeue path was fixed to scan every row one
    commit earlier and this one was not.

    Last-owned, not first-owned: a requeued job has one row per attempt, oldest
    first.
    """
    rc, out = run(["sacct", "-n", "-X", "-P", "-j", str(job_id),
                   "-o", "State,ExitCode,Submit,End"])
    if rc != 0 or not out:
        return None
    chosen, last_why, saw_any = None, None, False
    if note_out is None:
        note_out = []
    for line in out.splitlines():
        if not line.strip():
            continue
        saw_any = True
        f = line.split("|")
        state = f[0].split()[0] if f and f[0].strip() else None
        code = f[1] if len(f) > 1 else ""
        submit = parse_iso_ts(f[2].strip()) if len(f) > 2 else None
        end = parse_iso_ts(f[3].strip()) if len(f) > 3 else None
        ours, why = sacct_row_is_ours(submit, declared_at, bound_at)
        if not ours:
            last_why = why
            continue
        # Ordering is REPORTED, not enforced. It was enforced for one round and
        # three findings across two reviewers showed why it cannot be:
        #   - it cannot say WHICH attempt produced an artifact, so a later
        #     no-op job certified an earlier run's outputs anyway (kimi);
        #   - an archiving or sync script touching a checkpoint after the run
        #     advances the evidence past the job's End and REFUSES honest
        #     evidence, which is routine on a shared filesystem (deepseek);
        #   - where sacct does not populate End it skips entirely (deepseek).
        # Underneath all three is one fact: artifact-to-attempt attribution is
        # not derivable from timestamps. A contract certifies ONE run, and a
        # second run in the same directory is something the contract cannot
        # distinguish. Reported so a human can judge; see the plan's limits.
        if state in SLURM_OK_END and not terminal_record_postdates(
                end, newest_evidence_mtime):
            note_out.append("the scheduler row for this job ended before the "
                            "metrics or checkpoint it certifies; if this "
                            "directory was reused for a second run, that job "
                            "is not the one which produced them")
        chosen = (state, code)
    if chosen is None:
        return f"STALE_ROW:{last_why}" if saw_any and last_why else None
    state, code = chosen
    if state in SLURM_OK_END and not exit_code_is_clean(code):
        # COMPLETED beside a non-zero exit code is not a clean finish; the code
        # was previously not even fetched.
        return f"COMPLETED_DIRTY:{code}"
    return state



def write_receipt(path, payload):
    """Write a receipt without ever blocking.

    write_text() blocks in open() on a FIFO with no reader, so an OSError guard
    cannot help. A predictable .tmp-<pid> name was itself attackable the same
    way, so the temp file is created with mkstemp in the target directory
    (O_EXCL, unpredictable name) and renamed over the target.
    """
    import tempfile
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        return None
    except (OSError, MemoryError) as e:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return f"{type(e).__name__}: {e}"
# --- metrics ----------------------------------------------------------------

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


def metric_series(rows, key):
    """(step, value) pairs for a metric, in step order, numeric values only."""
    out, _ = metric_series_with_problems(rows, key)
    return out


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
        out.append((r["step"], fv))
    return sorted(out, key=lambda x: x[0]), problems


def blocking_integrity(whole_file, scoped, contract):
    """Structural faults serious enough to refuse a verdict.

    The two halves are scoped differently, and conflating them broke a run in
    each direction:

    Order faults (backwards steps, duplicate steps) are judged over the WHOLE
    file. Scoping them to the budget window dropped the out-of-budget row from
    a backward pair, so rows 0, 101, 50 under a 100-step budget read as
    monotonic and a file interleaving two runs certified as CONVERGED (luna).

    Gaps are judged over the BUDGET WINDOW only. A gap is measured against
    expect_eval_every, and evaluations after the budget legitimately thin out
    or stop; judging them whole-file failed an honest run whose last row landed
    far past the budget (deepseek and luna, independently, on the very claim
    added to catch exactly this).
    """
    problems = [p for p in whole_file if "monoton" in p or "duplicate" in p]
    problems += [p for p in integrity_problems(
                     scoped, contract.get("expect_eval_every"))
                 if "gaps larger" in p]
    return problems


def integrity_problems(rows, expect_every=None):
    """Structural faults that make any verdict untrustworthy.

    A metrics file that skips backwards, repeats a step, or has a hole in it
    describes a run that was restarted or interleaved -- reading a convergence
    verdict off it would be reading the wrong run."""
    problems = []
    if not rows:
        return ["no usable metric rows"]
    steps = [r["step"] for r in rows]
    if any(b < a for a, b in zip(steps, steps[1:])):
        problems.append("steps are not monotonically increasing "
                        "(restart or interleaved runs in one file)")
    seen_once, dupes = set(), set()
    for s in steps:  # linear; steps.count() per row was quadratic
        if s in seen_once:
            dupes.add(s)
        else:
            seen_once.add(s)
    if dupes:
        sample = sorted(dupes)[:5]
        problems.append(f"duplicate steps: {sample}"
                        f"{' ...' if len(dupes) > 5 else ''}")
    if expect_every:
        # Integer arithmetic: `expect_every * 1.5` overflowed on a huge
        # hand-edited value and killed check before any receipt.
        try:
            tolerance = (int(expect_every) * 3) // 2
        except (TypeError, ValueError, OverflowError):
            problems.append(f"expect_eval_every is unusable: {expect_every!r}")
            return problems
        gaps = [(a, b) for a, b in zip(steps, steps[1:])
                if b - a > tolerance]
        if gaps:
            problems.append(f"gaps larger than {expect_every} steps: {gaps[:3]}"
                            f"{' ...' if len(gaps) > 3 else ''}")
    return problems


def nonfinite(rows, metric_keys=None):
    """Non-finite metric values, including ones written as strings.

    metric_keys scopes the string rule to slots actually used as metrics -- the
    ones named by the convergence criterion or a divergence rule. A row may
    carry annotation fields ({"note": "ok"}, {"phase": "train"}) and those are
    not metrics; flagging them as divergence was an overcorrection.

    Within a real metric slot, ANY string is unusable evidence. Parsing and
    enumerating spellings both miss cases ("1.#INF"), so the whole class is
    treated as unusable rather than guessed at."""
    bad = []
    for r in rows:
        for k, v in r.items():
            if k == "step" or isinstance(v, bool):
                continue
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                bad.append((r["step"], k, v))
            elif isinstance(v, str) and (metric_keys is None or k in metric_keys):
                bad.append((r["step"], k, v))
    return bad


def finite_number(v):
    """(value, error). Rejects NaN and infinity: a NaN bound makes every
    comparison silently false, which reads as "rule satisfied" and can produce
    a false CONVERGED."""
    try:
        fv = float(v)
    except (TypeError, ValueError, OverflowError):
        return None, f"not a number: {v!r}"
    if math.isnan(fv) or math.isinf(fv):
        return None, f"must be finite: {v!r}"
    return fv, None


def nonneg_int(v):
    """(value, error). Rejects fractional values rather than truncating them --
    min_steps of 1.5 became 1 and allowed convergence below the declared
    minimum -- and catches the OverflowError from int(float('inf'))."""
    if isinstance(v, bool):
        return None, f"must be an integer: {v!r}"
    if isinstance(v, int):
        return (v, None) if v >= 0 else (None, f"must not be negative: {v!r}")
    try:
        fv = float(v)
    except (TypeError, ValueError, OverflowError):
        return None, f"not an integer: {v!r}"
    if math.isnan(fv) or math.isinf(fv):
        return None, f"must be finite: {v!r}"
    if fv != int(fv):
        return None, f"must be a whole number, not {v!r}"
    iv = int(fv)
    return (iv, None) if iv >= 0 else (None, f"must not be negative: {v!r}")


CONTRACT_TYPES = {
    "metrics_file": str,
    "checkpoint_dir": (str, type(None)),
    "checkpoint_glob": (str, type(None)),
    "sparse_metric": (bool, type(None)),
    "preexisting_metrics": (dict, type(None)),
    "criteria_digest": (str, type(None)),
    "contract_id": (str, type(None)),
    "converge": (dict, type(None)),
    "diverge": (list, type(None)),
    "max_steps": (int, type(None)),
    "expect_eval_every": (int, type(None)),
    "preemptible": (bool, type(None)),
    "retrospective": (bool, type(None)),
    "run": (dict, type(None)),
    "schema_version": (int, type(None)),
    "created_at": (str, type(None)),
    "created_at_epoch": (int, float, type(None)),
}


def contract_problems(contract):
    """Every field's type checked in one place, before anything reads it.

    A contract can be hand-edited after init, so check cannot assume init's
    validation still holds. Booleans are rejected where ints are expected
    because bool is an int subclass in Python."""
    problems = []
    if not isinstance(contract, dict):
        return ["contract is not a JSON object"]
    for field, want in CONTRACT_TYPES.items():
        if field not in contract:
            continue
        val = contract[field]
        if want in (int, (int, type(None))) and isinstance(val, bool):
            problems.append(f"'{field}' must be an integer, not a boolean")
            continue
        if not isinstance(val, want):
            names = (want.__name__ if isinstance(want, type)
                     else "/".join(w.__name__ for w in want))
            problems.append(f"'{field}' must be {names}, got "
                            f"{type(val).__name__}")
    if not isinstance(contract.get("metrics_file"), str) \
            or not contract.get("metrics_file"):
        problems.append("'metrics_file' is required and must be a non-empty string")
    for field in ("max_steps", "expect_eval_every"):
        val = contract.get(field)
        if val is not None and not isinstance(val, bool) and isinstance(val, int):
            if val < 0:
                problems.append(f"'{field}' must not be negative")
    if isinstance(contract.get("converge"), dict):
        bad = criterion_problem(contract["converge"])
        if bad:
            problems.append(f"convergence criterion: {bad}")
    for i, rule in enumerate(contract.get("diverge") or []):
        if not isinstance(rule, dict):
            problems.append(f"divergence rule {i} is not an object")
    return problems


MAX_CONTRACT_BYTES = 16 * 1024 * 1024


def read_json_bounded(path):
    """(text, error). Regular files only, size-capped, non-blocking open.

    The contract file itself was still read with a plain read_text(), so a FIFO
    there hung check before it could emit any verdict."""
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "", f"not a regular file ({stat.filemode(st.st_mode)})"
        if st.st_size > MAX_CONTRACT_BYTES:
            return "", f"{st.st_size} bytes, above the {MAX_CONTRACT_BYTES}-byte limit"
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = None
            raw = fh.read(MAX_CONTRACT_BYTES + 1)
        return raw.decode("utf-8", errors="replace"), None
    except (OSError, MemoryError) as e:
        return "", f"unreadable: {type(e).__name__}: {e}"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def parse_iso_ts(s):
    """Epoch seconds from an ISO timestamp, or None. Timezone-aware.

    sacct emits fractional seconds on some builds; an unparsed timestamp became
    None, which artifact_is_fresh treats as "no comparison possible" and
    therefore fresh, so a stale reused row was accepted.

    The offset is HONOURED, not discarded. time.strptime parses %z and
    time.mktime then throws it away by reading the fields as local time, so the
    same instant written +0000, -0700 and +0200 produced three epochs spanning
    nine hours (deepseek CRITICAL, luna independently). That silently misplaced
    every sacct row whose rendering did not match the verifier host, in both
    directions: a reused row could pass, and an honest one be refused.
    """
    if not isinstance(s, str) or not s:
        return None
    s = s.strip()
    # Strip ONLY the fractional-seconds run. A first attempt collected every
    # digit after the dot, swallowing the offset's digits and producing an
    # unparsable string -- so the guard failed open on exactly the timestamps
    # it was added for.
    s = re.sub(r"\.\d+", "", s, count=1)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            tm = time.strptime(s, fmt)
        except (ValueError, OverflowError):
            continue
        try:
            off = tm.tm_gmtoff
        except AttributeError:              # pragma: no cover
            off = None
        if off is not None:
            # Offset given: read the fields as UTC, then correct by the offset.
            return float(calendar.timegm(tm) - off)
        # No offset: a naive local timestamp, which is what sacct emits by
        # default, so local is the correct reading.
        try:
            return time.mktime(tm)
        except (ValueError, OverflowError):
            return None
    return None


# Every field that can change the verdict. Digesting only converge/diverge/
# max_steps let a post-run flip of sparse_metric -- which decides whether a
# metric absent from a row disqualifies convergence -- go undetected, as did
# retargeting metrics_file or widening checkpoint_glob (luna).
DIGESTED_FIELDS = (
    # What the verdict is measured against.
    "converge", "diverge", "max_steps", "sparse_metric",
    "expect_eval_every", "metrics_file", "checkpoint_dir",
    "checkpoint_glob", "retrospective", "preemptible",
    # And the provenance anchors themselves: created_at_epoch decides what
    # counts as fresh and contract_id binds the termination record, so leaving
    # them outside the digest left both editable after the fact (luna).
    "created_at", "created_at_epoch", "contract_id",
)

# `run` is NOT digested wholesale: it carries hostname, user, python version
# and NCCL env, none of which decide anything, and digesting them made an
# unrelated edit look like post-hoc criterion selection. Only the job id
# matters, because read_binding falls back to it when no `bind` record exists.
DIGESTED_RUN_KEYS = ("slurm_job_id",)


def criteria_digest(contract):
    """Stable fingerprint of every field that decides the verdict."""
    c = contract or {}
    fields = {k: c.get(k) for k in DIGESTED_FIELDS}
    run = c.get("run") if isinstance(c.get("run"), dict) else {}
    for k in DIGESTED_RUN_KEYS:
        fields[f"run.{k}"] = run.get(k)
    payload = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


CONVERGE_KEYS = frozenset({"metric", "mode", "threshold",
                           "rel_improvement_below", "over_evals", "min_steps"})
DIVERGE_KEYS = frozenset({"metric", "above", "below"})


def criterion_problem(crit):
    """Why a convergence criterion cannot be evaluated, or None.

    Checked at init as well as at check: a criterion with a typo in it used to
    be accepted, then crash the verifier when it was finally evaluated."""
    if not isinstance(crit, dict):
        return "not a JSON object"
    if not isinstance(crit.get("metric"), str) or not crit["metric"].strip():
        return f"'metric' must be a non-empty string, got {crit.get('metric')!r}"
    if crit.get("mode", "min") not in ("min", "max"):
        return f"mode must be 'min' or 'max', got {crit.get('mode')!r}"
    if "threshold" not in crit and "rel_improvement_below" not in crit:
        return "needs either 'threshold' or 'rel_improvement_below'"
    for key in ("threshold", "rel_improvement_below"):
        if key in crit:
            _, err = finite_number(crit[key])
            if err:
                return f"'{key}' {err}"
    for key in ("over_evals", "min_steps"):
        if key in crit:
            _, err = nonneg_int(crit[key])
            if err:
                return f"'{key}' {err}"
    if "rel_improvement_below" in crit:
        n, err = nonneg_int(crit.get("over_evals", 5))
        if err or n < 1:
            return "'over_evals' must be a whole number of at least 1"
    # Enumerate the keys this criterion is READ with. Every known key above was
    # validated, but an unknown one was silently ignored, so a typo
    # (`min_step` for `min_steps`) left the real criterion WEAKER than the
    # declared one with no warning. Same defect found in contract.py's
    # predicates by real use on lambda; fixed in both.
    unknown = sorted(set(crit) - CONVERGE_KEYS)
    if unknown:
        return (f"unrecognised key(s) {', '.join(unknown)}; this criterion "
                f"reads only {', '.join(sorted(CONVERGE_KEYS))}. A typo here "
                f"would silently weaken the criterion.")
    return None


# --- convergence ------------------------------------------------------------

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
                       f"({unusable[0]}); cannot certify convergence")
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


# --- checkpoints ------------------------------------------------------------

def _tf_shards_complete(stem, siblings):
    """Whether EVERY data shard a TensorFlow checkpoint declares is present.

    `model.data-00000-of-00002` says there are two shards. Accepting the set
    because each file had *any* matching counterpart passed a half-written
    checkpoint, and let a stale .index left by a previous run pair with one
    fresh shard (sol). The name states the expected count; require it.
    """
    shards = {}
    for s in siblings:
        m = TF_SHARD.search(s.name)
        if not m or s.name[: m.start()] != stem:
            continue
        if s.size <= 0 or not s.readable:
            return False            # a zero-byte or unreadable shard is nothing
        idx, total = m.group(1), m.group(2)
        try:
            shards[int(idx)] = int(total)
        except (TypeError, ValueError):
            return False
    if not shards:
        return False
    totals = set(shards.values())
    if len(totals) != 1:
        return False                # shards disagree on how many there are
    total = totals.pop()
    return total > 0 and set(shards) == set(range(total))


def checkpoint_set_members(name, files):
    """Every file that has to be present for `name` to be loadable.

    A TensorFlow checkpoint is an .index plus its data shards, so freshness has
    to hold for the WHOLE set. Checking only the newest file let a previous
    run's index and shard 1 pair with one freshly written shard 0: complete by
    count, fresh by newest, and a stale/new mixture that loads to nothing
    (kimi). Single-file formats are their own set.
    """
    stem = None
    if name.lower().endswith(".index"):
        stem = name[: -len(".index")]
    else:
        m = TF_SHARD.search(name)
        if m:
            stem = name[: m.start()]
    if stem is None:
        return [name]
    members = [f["name"] for f in files
               if f["name"] == stem + ".index"
               or (TF_SHARD.search(f["name"])
                   and f["name"][: TF_SHARD.search(f["name"]).start()] == stem)]
    return members or [name]


def looks_like_checkpoint(name, pattern=None, siblings=()):
    """Whether a file plausibly IS a loadable checkpoint.

    A TensorFlow .index carries no weights -- the data shard is a separate
    file -- so an .index alone is not a checkpoint."""
    if pattern:
        return fnmatch.fnmatch(name, pattern)
    low = name.lower()
    if low.endswith(".index"):
        stem = name[: -len(".index")]
        return _tf_shards_complete(stem, siblings)
    m = TF_SHARD.search(name)
    if m:
        # A shard is only evidence alongside its index AND its siblings.
        stem = name[: m.start()]
        if not any(s.name == stem + ".index" and s.size > 0 and s.readable
                   for s in siblings):
            return False
        return _tf_shards_complete(stem, siblings)
    if low.endswith(NON_MODEL_SUFFIXES):
        return False  # checkpoint_notes.txt is not a checkpoint
    return low.endswith(CHECKPOINT_SUFFIXES) or "checkpoint" in low


def checkpoint_survey(ckpt_dir, pattern=None):
    """What is actually on disk, and which storage tier it sits on.

    Tier matters operationally: a checkpoint on a hot filesystem at 97% capacity
    is retained in a very different sense from one on a cold tier."""
    if not isinstance(ckpt_dir, str) or not ckpt_dir:
        return {"dir": None, "exists": False, "files": [], "partial": [],
                "other": [], "newest": None, "tier": None, "filesystem": None,
                "pattern": pattern,
                "error": f"checkpoint_dir is not a path: {ckpt_dir!r}"}
    d = Path(os.path.expanduser(ckpt_dir))
    info = {"dir": str(d), "exists": d.is_dir(), "files": [], "partial": [],
            "other": [], "newest": None, "tier": None, "filesystem": None,
            "pattern": pattern}
    if not info["exists"]:
        return info
    try:
        entries = [f for f in d.iterdir() if f.is_file()]
    except OSError as e:
        info["error"] = str(e)
        return info
    class _Sib:
        __slots__ = ("name", "size", "readable")

        def __init__(self, name, size, readable):
            self.name, self.size, self.readable = name, size, readable

    siblings = []
    for f in entries:
        try:
            siblings.append(_Sib(f.name, f.stat().st_size,
                                 os.access(f, os.R_OK)))
        except OSError:
            siblings.append(_Sib(f.name, 0, False))
    for f in entries:
        try:
            st = f.stat()
        except OSError:
            continue
        # NOT int(st.st_mtime): truncating here destroyed the sub-second part
        # before artifact_is_fresh could use it, so every checkpoint took the
        # coarse-filesystem path even on a nanosecond filesystem (luna).
        rec = {"name": f.name, "size": st.st_size, "mtime": st.st_mtime}
        readable = os.access(f, os.R_OK)
        if not readable:
            rec["unreadable"] = True
        if not looks_like_checkpoint(f.name, pattern, siblings):
            rec["not_a_checkpoint"] = True
            info["other"].append(rec)
        # Lowercased: the check was case-sensitive, so a framework staging
        # `checkpoint-100.pt.TMP` had it counted as a complete checkpoint (luna).
        #
        # Applied even when --checkpoint-glob selected the file, which luna
        # raised. Deliberate: a name ending .tmp/.part/.writing means something
        # is mid-write, and the failure direction here is "not yet a usable
        # checkpoint", which is the safe one. A glob that deliberately matches
        # staged files is asking to certify a partial model.
        elif (f.name.lower().endswith(PARTIAL_SUFFIXES) or st.st_size == 0
              or not readable):
            info["partial"].append(rec)
        else:
            info["files"].append(rec)
    if info["files"]:
        info["newest"] = max(info["files"], key=lambda r: r["mtime"])
    rc, out = run(["df", "-P", str(d)])
    if rc == 0 and out:
        lines = out.splitlines()
        if len(lines) > 1:
            parts = lines[-1].split()
            info["filesystem"] = parts[0] if parts else None
            if len(parts) >= 5:
                info["tier"] = {"capacity": parts[1], "used_pct": parts[4]}
    return info


def step_from_name(name):
    m = re.search(r"(?:step|iter|ckpt)[-_]?(\d+)", name, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{3,})", name)
    return int(m.group(1)) if m else None


# --- commands ---------------------------------------------------------------

def cmd_init(args):
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    cpath = run_dir / CONTRACT
    if cpath.exists() and not args.force:
        sys.exit(f"error: {cpath} exists (use --force to replace)")

    try:
        converge = json.loads(args.converge) if args.converge else None
        diverge = [json.loads(d) for d in args.diverge]
    except json.JSONDecodeError as e:
        sys.exit(f"error: criterion is not valid JSON: {e}")

    if converge is not None:
        problem = criterion_problem(converge)
        if problem:
            sys.exit(f"error: unusable convergence criterion: {problem}")
    for i, rule in enumerate(diverge):
        if not isinstance(rule, dict) or not rule.get("metric"):
            sys.exit(f"error: divergence rule {i} needs a 'metric'")
        if not any(k in rule for k in ("above", "below")):
            sys.exit(f"error: divergence rule {i} needs 'above' or 'below'")
        if not isinstance(rule.get("metric"), str):
            sys.exit(f"error: divergence rule {i} 'metric' must be a string")
        for k in ("above", "below"):
            if k in rule:
                _, err = finite_number(rule[k])
                if err:
                    sys.exit(f"error: divergence rule {i} '{k}' {err}")
        extra = sorted(set(rule) - DIVERGE_KEYS)
        if extra:
            sys.exit(f"error: divergence rule {i} has unrecognised key(s) "
                     f"{', '.join(extra)}; it reads only "
                     f"{', '.join(sorted(DIVERGE_KEYS))}. A typo here would "
                     f"silently weaken the rule.")

    if not args.checkpoint_dir and not args.retrospective:
        print("NOTE: no --checkpoint-dir given; a CONVERGED verdict will be "
              "downgraded to INCOMPLETE_EVIDENCE because nothing shows a "
              "loadable model exists.", file=sys.stderr)

    if not converge and not args.retrospective:
        sys.exit("error: no --converge criterion. Without one, this can report "
                 "that training stopped, never that it converged. Declare the "
                 "criterion now, or pass --retrospective to audit a past run.")

    contract = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        # Sub-second companion to created_at; see artifact_is_fresh.
        "created_at_epoch": time.time(),
        "retrospective": bool(args.retrospective),
        "metrics_file": str(Path(args.metrics).resolve()),
        "checkpoint_dir": str(Path(args.checkpoint_dir).resolve())
        if args.checkpoint_dir else None,
        "checkpoint_glob": args.checkpoint_glob,
        "sparse_metric": bool(args.sparse_metric),
        # Fingerprint of the metrics file if it ALREADY EXISTS at declaration.
        # See metrics_unchanged_since_declaration: `touch` defeats mtime.
        "preexisting_metrics": file_fingerprint(str(Path(args.metrics).resolve())),
        # Fingerprint of the declared criteria, so a later edit to an unrelated
        # field is not mistaken for post-hoc criterion selection. Filled in
        # below, once the fields it covers exist.
        "criteria_digest": None,
        # Per-instance nonce. The criteria digest alone matched after
        # `init --force` with identical criteria, so a stale exit-0 record
        # certified a later crashed run.
        "contract_id": os.urandom(8).hex(),
        "converge": converge,
        "diverge": diverge,
        "max_steps": args.max_steps,
        "expect_eval_every": args.expect_eval_every,
        "preemptible": bool(args.preemptible),
        "run": {
            "hostname": os.uname().nodename,
            "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
            "python": sys.version.split()[0],
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            "partition": os.environ.get("SLURM_JOB_PARTITION"),
            "nccl": {k: v for k, v in os.environ.items() if k.startswith("NCCL_")},
        },
    }
    contract["criteria_digest"] = criteria_digest(contract)
    cpath.write_text(json.dumps(contract, indent=2) + "\n")
    if contract["retrospective"]:
        print("WARNING: retrospective -- any criterion here was chosen after "
              "seeing results and cannot establish convergence.", file=sys.stderr)
    print(f"wrote {cpath}")
    if converge:
        print(f"  convergence: {json.dumps(converge)}")
    if args.max_steps:
        print(f"  budget: {args.max_steps} steps")


def read_binding(run_dir):
    """The job id bound to this contract, as (job_id, submitted_at_iso).

    Two ways a binding is genuine, and only these two:
      - `bind` wrote one after sbatch returned an id (the login-node flow);
      - `init` ran INSIDE the job and captured $SLURM_JOB_ID, in which case the
        contract's own created_at IS the submission time. Requiring a redundant
        `bind` there would break a workflow that is already correct.
    """
    contract = {}
    raw, rerr = read_json_bounded(run_dir / CONTRACT)
    if not rerr:
        try:
            c = json.loads(raw)
            if isinstance(c, dict):
                contract = c
        except (ValueError, RecursionError):
            pass
    cid = contract.get("contract_id")

    raw, rerr = read_json_bounded(run_dir / BINDING)
    if not rerr and raw:
        try:
            b = json.loads(raw)
        except (ValueError, RecursionError):
            b = None
        # Bound to THIS contract instance, exactly as the termination record
        # is: an unbound binding survived `init --force`.
        if (isinstance(b, dict) and b.get("job_id")
                and (cid is None or b.get("contract_id") == cid)):
            return str(b["job_id"]), b.get("submitted_at")

    init_id = (contract.get("run") or {}).get("slurm_job_id")
    if init_id:
        return str(init_id), contract.get("created_at")
    return None, None


def cmd_bind(args):
    """Bind a Slurm job id to this contract after submission.

    `init` captures $SLURM_JOB_ID, which does not exist when the contract is
    declared on a login node before sbatch. Without this there was no recorded
    submission time to compare an sacct row against, so the verifier had to
    infer ownership from timestamps -- which fails open on an unparseable
    Submit and closed on a whole-second one."""
    run_dir = Path(args.run_dir).resolve()
    cpath = run_dir / CONTRACT
    if not cpath.exists():
        sys.exit(f"error: no training contract at {cpath}")
    raw, rerr = read_json_bounded(cpath)
    if rerr:
        sys.exit(f"error: contract unreadable: {rerr}")
    try:
        contract = json.loads(raw)
    except (ValueError, RecursionError) as e:
        sys.exit(f"error: contract malformed: {e}")
    if not isinstance(contract, dict):
        sys.exit("error: contract is not a JSON object")

    job_id = str(args.job_id).strip()
    # Slurm ids are numeric, with _ for array tasks and . for steps. Accepting
    # any alphanumeric string took "abc" (luna); a leading digit is the cheap
    # discriminator that keeps 12345, 12345_7 and 12345.batch.
    if not re.fullmatch(r"\d[\w.]*", job_id):
        sys.exit(f"error: implausible Slurm job id {args.job_id!r}")

    # Refuse a contract that already has terminal evidence: binding a new job
    # id afterwards points the verifier at different accounting and can flip a
    # settled verdict.
    term = read_termination(run_dir)
    if term is not None and not termination_matches(term, contract):
        # A receipt left behind by `init --force` belongs to the PREVIOUS
        # contract. Checking read_termination alone let it block binding an
        # honest new run (luna); every other consumer of this file already
        # filters by contract instance.
        term = None
    if term is not None and term.get("terminal") and not args.force:
        sys.exit(f"error: {run_dir} already has a recorded termination "
                 f"(exit {term.get('exit_code')}); binding a job id now would "
                 f"point the verifier at other accounting. Use --force only if "
                 f"that record is wrong.")

    existing_id, existing_at = read_binding(run_dir)
    if existing_id is not None and existing_id != job_id and not args.force:
        sys.exit(f"error: this contract is already bound to job {existing_id}; "
                 f"binding it to {job_id} would let a second job's accounting "
                 f"certify it. Use --force only if the first binding was wrong.")

    rec = {"job_id": job_id,
           # Re-binding the SAME id keeps the FIRST time: rewriting it made the
           # operation non-idempotent, and a retry moved the anchor forward.
           "submitted_at": (existing_at if existing_id == job_id and existing_at
                            else now_iso()),
           "host": os.uname().nodename,
           "contract_id": contract.get("contract_id"),
           "criteria_digest": contract.get("criteria_digest")}
    werr = write_receipt(run_dir / BINDING, rec)
    if werr:
        sys.exit(f"error: cannot write the binding: {werr}")
    print(f"bound job {job_id} to {run_dir}")
    print(f"verify with: traincontract.py check {run_dir}")


def cmd_record(args):
    """Record the terminal outcome of a run not managed by Slurm.

    contract.py has had this since round 4. Without it, a locally-run training
    job had no way to supply termination evidence, so traincontract.py accepted
    metrics alone -- which is exactly the false pass its sibling refuses."""
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / CONTRACT).exists():
        sys.exit(f"error: no training contract at {run_dir / CONTRACT}")
    digest, cid = None, None
    try:
        raw, rerr = read_json_bounded(run_dir / CONTRACT)
        if not rerr:
            c = json.loads(raw)
            if isinstance(c, dict):
                digest, cid = c.get("criteria_digest"), c.get("contract_id")
    except (ValueError, RecursionError):
        pass
    rec = {"terminal": True, "exit_code": args.exit_code,
           "recorded_at": now_iso(), "host": os.uname().nodename,
           # Bind the record to THIS contract INSTANCE. The criteria digest
           # alone matched after `init --force` with identical criteria.
           "criteria_digest": digest, "contract_id": cid}
    werr = write_receipt(run_dir / TERMINATION, rec)
    if werr:
        sys.exit(f"error: cannot write the termination record: {werr}")
    print(f"recorded local termination (exit {args.exit_code}) for {run_dir}")


def read_termination(run_dir):
    """Recorded local terminal outcome, or None. Never raises."""
    path = run_dir / TERMINATION
    if not path.exists():
        return None
    raw, err = read_json_bounded(path)
    if err:
        return None
    try:
        rec = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(rec, dict) or rec.get("terminal") is not True:
        return None
    ec = rec.get("exit_code")
    if isinstance(ec, bool) or not isinstance(ec, int):
        return None
    return rec


def termination_problem(rec, contract, newest_evidence_mtime=None):
    """Why a termination record cannot be used, or None.

    Two distinct reasons, and reporting one for the other misdescribes the
    fault: a record can belong to a DIFFERENT contract instance, or belong to
    this one but predate the evidence it would certify."""
    if rec is None:
        return "no termination record"
    # NOT a rejection: see slurm_state's note. A post-run touch by an
    # archiving script inverts this ordering on an honest run, and ordering
    # cannot say which attempt produced an artifact anyway.
    want_id = contract.get("contract_id")
    if want_id is not None:
        if rec.get("contract_id") != want_id:
            return ("the termination record was written for a different "
                    "contract instance")
        return None
    want = contract.get("criteria_digest")
    if want is None:
        return None
    if rec.get("criteria_digest") != want:
        return ("the termination record was written for different criteria "
                "(digest mismatch)")
    return None


def termination_matches(rec, contract, newest_evidence_mtime=None):
    """Boolean form of termination_problem, which carries the reason."""
    return termination_problem(rec, contract, newest_evidence_mtime) is None



def cmd_check(args):
    run_dir = Path(args.run_dir).resolve()

    def _timed_out(sec):
        print(f"INCOMPLETE_EVIDENCE  ({run_dir})")
        print(f"  - check exceeded its {sec}s watchdog before reaching a "
              f"verdict; a path it reads may be on an unresponsive filesystem")
        sys.exit(STATES["INCOMPLETE_EVIDENCE"])

    arm_watchdog(getattr(args, "watchdog", 600), _timed_out)
    cpath = run_dir / CONTRACT
    if not cpath.exists():
        sys.exit(f"error: no training contract at {cpath}")
    # Bound before the try below, so `bail` can read it on the path where the
    # contract could not be parsed. Reading it unconditionally there would
    # NameError and turn "a broken contract is a verdict, not a crash" into a
    # crash -- which is what the first version of this change did.
    contract = {}

    def bail(reason, state="CONTRACT_VIOLATED"):
        """Always leave a receipt: a broken contract is a verdict, not a crash."""
        verification = {
            "schema_version": SCHEMA_VERSION, "checked_at": now_iso(),
            # WHICH contract instance this verdict is about; see contract.py.
            # Empty when the contract itself could not be read, which is
            # honest: there is no instance to name.
            "contract_id": contract.get("contract_id"),
            "criteria_digest": contract.get("criteria_digest"),
            "state": state, "exit_code": STATES[state], "reasons": [reason],
            "evaluations": 0, "last_step": None, "checkpoint": {},
        }
        write_receipt(run_dir / VERIFICATION, verification)
        print(f"{state}  ({run_dir})")
        print(f"  - {reason}")
        sys.exit(STATES[state])

    try:
        raw, rerr = read_json_bounded(cpath)
        if rerr:
            raise ValueError(f"contract unreadable: {rerr}")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            contract.update(parsed)
        else:
            raise ValueError("contract is not a JSON object")
    except (OSError, ValueError, RecursionError) as e:
        bail(f"contract unreadable or malformed: {e}")
    problems = contract_problems(contract)
    if problems:
        bail("; ".join(problems[:4]))
    budget_raw = contract.get("max_steps")

    rows, parse_problems = read_metrics(contract["metrics_file"])
    reasons = list(parse_problems)

    # Provenance: a contract created after the metrics were last written means
    # the criterion was chosen with the curve already visible.
    post_hoc, post_hoc_why = False, ""
    if not contract.get("retrospective"):
        try:
            c_mtime = (run_dir / CONTRACT).stat().st_mtime
            m_mtime = os.stat(contract["metrics_file"]).st_mtime
            if rows:
                # Two independent signals, because neither alone is enough:
                #   created_at vs metrics mtime  -> was the contract first
                #     declared after the data already existed? (immune to later
                #     edits, which is why file mtime alone gave false positives)
                #   criteria_digest              -> were the criteria changed
                #     after declaration? (catches an edit the timestamps miss)
                declared_at = parse_iso_ts(contract.get("created_at"))
                declared_epoch = contract_epoch(contract)
                if metrics_unchanged_since_declaration(contract) is True:
                    # REPORTED, NOT BLOCKING, unlike a mtime that predates the
                    # contract. A training run that appends to an existing
                    # metrics file changes it, so byte-identity here is a
                    # strong hint the curve is the old one -- but a resumed run
                    # writing to a fresh path, or a deterministic replay, looks
                    # the same, and this tool exists to certify reproducible
                    # work. Said out loud for a human to judge.
                    reasons.append(
                        "note: the metrics file is byte-identical to the one "
                        "that already existed when this contract was declared, "
                        "so nothing here shows this run wrote it")
                if ((declared_at is not None or declared_epoch is not None)
                        and not artifact_is_fresh(m_mtime, declared_at,
                                                  declared_epoch)):
                    post_hoc = True
                    post_hoc_why = ("the contract was first declared after the "
                                    "metrics file already existed")
                recorded = contract.get("criteria_digest")
                if not isinstance(recorded, str) or not recorded.strip():
                    # An absent digest made the mechanism opt-out: null it
                    # while editing created_at_epoch and both the edit check
                    # and the freshness anchor were disabled at once (luna).
                    post_hoc = True
                    post_hoc_why = ("this contract carries no criteria_digest, "
                                    "so it cannot be shown to be the one that "
                                    "was declared")
                elif recorded != criteria_digest(contract):
                    post_hoc = True
                    post_hoc_why = ("the criteria were changed after the "
                                    "contract was declared")
        except OSError:
            pass
    state = None

    integrity = integrity_problems(rows, contract.get("expect_eval_every"))
    ckpt = checkpoint_survey(contract["checkpoint_dir"],
                             contract.get("checkpoint_glob")) \
        if contract.get("checkpoint_dir") else {}

    if not rows:
        state = "INCOMPLETE_EVIDENCE"
        reasons.append(
            f"no usable metric rows in {contract.get('metrics_file')}; nothing "
            f"can be judged. Check that the trainer wrote it and that each "
            f"line is a JSON object with a numeric 'step'")
    else:
        last_step = max(r["step"] for r in rows)
        budget_cap, _ = ((nonneg_int(budget_raw)[0], None)
                         if budget_raw is not None else (None, None))
        # Apply the budget window to EVERY test, not just convergence. A
        # divergence past the budget is outside the contract, exactly as a
        # convergence past the budget is.
        # Inclusive: step == max_steps is within budget. Only rows strictly
        # beyond it are outside the contract.
        scoped = ([r for r in rows if r["step"] <= int(budget_cap)]
                  if budget_cap is not None else rows)
        beyond = len(rows) - len(scoped)
        # Keys the contract actually reads as metrics. Needed by both the
        # in-budget checks and the past-budget warning below.
        metric_keys = set()
        if isinstance(contract.get("converge"), dict):
            mk = contract["converge"].get("metric")
            if isinstance(mk, str) and mk:  # a list name is unhashable
                metric_keys.add(mk)
        for rule in contract.get("diverge") or []:
            if isinstance(rule, dict) and isinstance(rule.get("metric"), str):
                metric_keys.add(rule["metric"])

        if beyond:
            reasons.append(f"note: {beyond} evaluation(s) beyond the declared "
                           f"budget of {budget_cap} are outside the contract "
                           f"and were not used")
            # Outside the contract, so it does not change the verdict -- but a
            # blown-up tail must never be silent. Report it explicitly.
            tail = [r for r in rows if r["step"] > int(budget_cap)]
            tail_bad = nonfinite(tail)  # every column: a warning is cheap
            tail_breaches = eval_divergence(tail, contract.get("diverge"))
            if tail_bad:
                s, k, v = tail_bad[0]
                reasons.append(f"WARNING: non-finite {k}={v} at step {s}, past "
                               f"the budget; training continued and diverged "
                               f"after the contract window")
            for b in tail_breaches[:2]:
                reasons.append(f"WARNING: past the budget, {b}")
        bad = nonfinite(scoped, metric_keys)
        breaches = eval_divergence(scoped, contract.get("diverge"))

        if bad:
            state = "DIVERGED"
            s, k, v = bad[0]
            reasons.append(f"non-finite {k}={v} at step {s}"
                           + (f" (and {len(bad) - 1} more)" if len(bad) > 1 else ""))
        elif breaches:
            state = "DIVERGED"
            reasons.extend(breaches)
        elif blocking_integrity(integrity, scoped, contract):
            # A restarted or interleaved metrics file describes more than one
            # run; no convergence verdict read off it would mean anything.
            state = "CONTRACT_VIOLATED"
            reasons.extend(blocking_integrity(integrity, scoped, contract))
        else:
            budget = budget_cap
            # Only evidence within the declared budget can establish
            # convergence. Filtering here settles both failure modes at once:
            # a criterion met exactly at the budget, and one met early that a
            # later out-of-budget row would otherwise have masked.
            in_budget = scoped
            if budget is not None and not in_budget:
                met, detail = False, (f"no evaluations below the declared "
                                      f"budget of {budget}")
            else:
                crit = contract.get("converge")
                bad_crit = criterion_problem(crit) if crit else None
                if bad_crit:
                    met, detail = False, f"unusable criterion: {bad_crit}"
                else:
                    met, detail = (
                        eval_convergence(in_budget, crit,
                                         bool(contract.get("sparse_metric")))
                        if crit else
                        (False, "no convergence criterion declared"))
            reasons.append(detail)
            if met:
                state = "CONVERGED"
            elif budget is not None and last_step >= int(budget):
                # The distinction this tool exists for.
                state = "BUDGET_EXHAUSTED"
                reasons.append(f"reached the {budget}-step budget without "
                               f"meeting the criterion -- this is not convergence")
            else:
                state = "RUNNING"
                reasons.append(f"at step {last_step}"
                               + (f" of {budget}" if budget else ""))

    # Checkpoint usability is orthogonal: converged-but-unloadable is not done.
    # Scheduler liveness first: a job still queued or requeued has not
    # finished, whatever the metrics so far show.
    declared_at = parse_iso_ts(contract.get("created_at"))
    declared_epoch = contract_epoch(contract)
    # The newest thing a termination record would be certifying. A record that
    # predates its own evidence belongs to an earlier run in this directory.
    ordering_notes = []
    newest_evidence = None
    try:
        newest_evidence = os.stat(contract["metrics_file"]).st_mtime
    except (OSError, KeyError, TypeError):
        pass
    _ck_newest = (ckpt.get("newest") or {}).get("mtime")
    if _ck_newest is not None:
        newest_evidence = (_ck_newest if newest_evidence is None
                           else max(newest_evidence, _ck_newest))
    # Ownership is established by the binding, never inferred from timestamps.
    # The binding establishes WHICH job id is ours; the declaration time is
    # what an sacct row gets placed against.
    jid, _bound_iso = read_binding(run_dir)
    bound_at = parse_iso_ts(_bound_iso)
    if state == "CONVERGED":
        if not jid:
            # No job id: the only admissible evidence is an explicitly recorded
            # local termination. Metrics alone are not evidence that a run
            # finished -- contract.py has refused this since round 4, and both
            # reviewers converged on the asymmetry.
            term = read_termination(run_dir)
            problem = termination_problem(term, contract, newest_evidence)
            if term is not None and problem:
                reasons.append(f"ignoring the termination record: {problem}")
                term = None
            if term is None:
                state = "INCOMPLETE_EVIDENCE"
                reasons.append(
                    "the criterion is met by the metrics present, but nothing "
                    "shows the run terminated: no job id is bound to this "
                    "contract and no local termination was declared. Run "
                    "`traincontract.py bind <run-dir> --job-id N` after sbatch, "
                    "or `traincontract.py record <run-dir> --exit-code N` after "
                    "a local run. Editing the job id into the contract by hand "
                    "is not a substitute: the binding records WHEN we submitted, "
                    "which is what an sacct row gets checked against.")
            elif term["exit_code"] != 0:
                state = "CONTRACT_VIOLATED"
                reasons.append(f"recorded local run exited "
                               f"{term['exit_code']}; its metrics cannot "
                               f"certify convergence")
            else:
                reasons.append("terminal evidence: recorded local run exited 0")
        else:
            # squeue FIRST: sacct can hold a COMPLETED row for an earlier
            # attempt while the job is running again, and a stale terminal row
            # must not outrank a live one.
            qstate = squeue_state(jid, declared_at, bound_at)
            sstate = slurm_state(jid, declared_at, bound_at,
                                 newest_evidence, ordering_notes)
            if qstate:
                # ANY squeue state means the job is still in the system --
                # enumerating live states missed real ones such as STAGE_OUT.
                state = "PREEMPTED" if qstate in SLURM_NOT_DONE else "RUNNING"
                reasons.append(f"squeue reports {qstate} for job {jid}; the job "
                               f"is still in the system, so the metrics so far "
                               f"are not a final result"
                               + (f" (sacct says {sstate})" if sstate else ""))
            elif sstate in SLURM_NOT_DONE:
                state = "PREEMPTED"
                reasons.append(f"scheduler reports {sstate} for job {jid}; the "
                               f"run has not finished, so the metrics so far "
                               f"are not a final result")
            elif sstate in SLURM_ACTIVE:
                state = "RUNNING"
                reasons.append(f"scheduler reports {sstate} for job {jid}; the "
                               f"criterion is met so far but the run is not over")
            elif str(sstate).startswith("STALE_ROW"):
                term = read_termination(run_dir)
                if not termination_matches(term, contract, newest_evidence) or \
                        term["exit_code"] != 0:
                    state = "INCOMPLETE_EVIDENCE"
                    why = str(sstate).split(":", 1)[1] if ":" in str(sstate) \
                        else "it cannot be attributed to this contract"
                    reasons.append(
                        f"the sacct row for job {jid} is not evidence for this "
                        f"run: {why}")
                else:
                    reasons.append(f"sacct row for job {jid} looks stale (id "
                                   f"reuse); relying on the recorded local "
                                   f"termination (exit 0)")
            elif sstate is not None and str(sstate).startswith("COMPLETED_DIRTY"):
                state = "CONTRACT_VIOLATED"
                reasons.append(f"sacct reports COMPLETED for job {jid} but with "
                               f"exit code {str(sstate).split(':', 1)[1]!r}; a "
                               f"non-clean exit cannot certify convergence")
            elif sstate is not None and sstate not in SLURM_OK_END \
                    and sstate not in SLURM_BAD_END:
                # Unknown state: enumerate-the-bad-ones fails open, and Slurm
                # has states such as SPECIAL_EXIT that no list will cover.
                term = read_termination(run_dir)
                if not termination_matches(term, contract, newest_evidence):
                    term = None
                if term is None or term["exit_code"] != 0:
                    state = "INCOMPLETE_EVIDENCE"
                    reasons.append(
                        f"sacct reports {sstate} for job {jid}, which is not a "
                        f"recognised successful terminal state; nothing "
                        f"confirms the run succeeded. If that state does mean "
                        f"success on this cluster, declare the outcome with "
                        f"`record` -- unrecognised states are never assumed "
                        f"good")
                else:
                    reasons.append(f"sacct reports {sstate} for job {jid}; "
                                   f"relying on the recorded local "
                                   f"termination (exit 0)")
            elif sstate in SLURM_BAD_END:
                # A LATER local retry decides, exactly as it does in
                # contract.py: `record` is run by hand after a run finishes, so
                # a matching record is a later attempt than the bound job. The
                # tie-break is in the plan doc -- falsifying a record needs
                # shell access as the user, which is out of scope, while
                # refusing an honest retry is a live false negative. This
                # branch ignored the record entirely, which is the sibling of
                # the rule contract.py already had (kimi).
                retry = read_termination(run_dir)
                if (retry is not None
                        and not termination_problem(retry, contract, None)
                        and retry.get("exit_code") == 0):
                    reasons.append(
                        f"scheduler reports {sstate} for job {jid}, but a "
                        f"later local run was recorded with exit 0 and decides "
                        f"the outcome; the failed job's accounting is not this "
                        f"attempt's")
                else:
                    # A failed job's partial metrics cannot certify convergence.
                    state = "CONTRACT_VIOLATED"
                    reasons.append(
                        f"scheduler reports {sstate} for job {jid}; the job did "
                        f"not end successfully, so its metrics cannot certify "
                        f"convergence. Fix the run and train again, then "
                        f"`record --exit-code 0` for a local retry or "
                        f"`init --force` to declare a new contract instance")
            elif sstate is None:
                # A recorded job id with no scheduler record either way is not
                # evidence of anything. Accept an explicit local termination,
                # otherwise refuse -- contract.py has required this since r4.
                term = read_termination(run_dir)
                if term is not None and not termination_matches(term, contract, newest_evidence):
                    # The sibling branch above has filtered this since round 4;
                    # this one never did, so a receipt left by `init --force`
                    # kept CONVERGED alive for the NEW contract (sol).
                    reasons.append("a termination record exists but was written "
                                   "for a different contract; ignoring it")
                    term = None
                if term is None:
                    state = "INCOMPLETE_EVIDENCE"
                    reasons.append(
                        f"job {jid} is recorded but neither sacct nor squeue "
                        f"has any record of it, so nothing confirms the run "
                        f"finished; declare a local termination with `record` "
                        f"if it ran outside Slurm")
                elif term["exit_code"] != 0:
                    state = "CONTRACT_VIOLATED"
                    reasons.append(f"recorded local run exited "
                                   f"{term['exit_code']}")
                else:
                    reasons.append(f"no scheduler record for job {jid}; "
                                   f"relying on the recorded local "
                                   f"termination (exit 0)")

    if state == "CONVERGED" and any(
            str(r).startswith(("TRUNCATED:", "SKIPPED:")) for r in reasons):
        # Part of the metrics was never read, so a later divergence could be
        # sitting past the cap. Not certifiable.
        state = "INCOMPLETE_EVIDENCE"
        reasons.append("part of the metrics could not be read (truncated or "
                       "skipped lines), so convergence cannot be certified "
                       "from a partial series; fix the writer or declare "
                       "--sparse-metric if the gaps are intentional")

    if state == "CONVERGED" and post_hoc:
        # Detectable post-hoc selection cannot be certified as convergence. A
        # warning was too weak: the whole point is separating a prediction from
        # a choice made after seeing the answer.
        state = "INCOMPLETE_EVIDENCE"
        reasons.append(
            f"{post_hoc_why}, so the criterion was chosen with results already "
            f"visible; that is selection, not a prediction, and cannot "
            f"establish convergence. Re-declare before the run, or mark it "
            f"--retrospective.")

    if state == "CONVERGED" and not contract.get("checkpoint_dir"):
        state = "INCOMPLETE_EVIDENCE"
        reasons.append("no checkpoint_dir was declared, so there is no evidence "
                       "a loadable model exists; a converged curve alone is not "
                       "a trained model")
    elif state == "CONVERGED" and contract.get("checkpoint_dir"):
        newest = ckpt.get("newest") or {}
        # EVERY component of the selected checkpoint, not just the newest file.
        stale_members = []
        if declared_at is not None and newest:
            by_name = {f["name"]: f for f in (ckpt.get("files") or [])}
            for member in checkpoint_set_members(newest.get("name", ""),
                                                 ckpt.get("files") or []):
                rec = by_name.get(member)
                if rec is not None and not artifact_is_fresh(
                        rec.get("mtime"), declared_at, declared_epoch):
                    stale_members.append(member)
        if stale_members:
            state = "INCOMPLETE_EVIDENCE"
            if stale_members == [newest.get("name")]:
                reasons.append(
                    f"the newest checkpoint ({newest.get('name')}) predates "
                    f"this contract, so no artifact from this run supports the "
                    f"verdict; a reused checkpoint directory cannot certify a "
                    f"new run. Train again so a checkpoint is written, or "
                    f"`init --force` to declare a contract for the run that "
                    f"will. On a filesystem with whole-second mtimes a run "
                    f"finishing inside the declaration second looks like this "
                    f"too -- see the Known limits")
            else:
                reasons.append(
                    f"the checkpoint selected ({newest.get('name')}) needs "
                    f"{len(stale_members)} file(s) that predate this contract "
                    f"({', '.join(stale_members[:3])}), so it is a mixture of "
                    f"this run's output and a previous run's; that is not a "
                    f"loadable model from this run. Clear the checkpoint "
                    f"directory, or point --checkpoint-dir at a fresh one, and "
                    f"train again")
        elif not ckpt.get("exists"):
            state = "INCOMPLETE_EVIDENCE"
            reasons.append(
                f"checkpoint dir missing: {ckpt.get('dir')}. Point "
                f"--checkpoint-dir at where the trainer actually writes, or "
                f"omit it and accept INCOMPLETE_EVIDENCE for convergence")
        elif not ckpt.get("files"):
            state = "INCOMPLETE_EVIDENCE"
            detail = "(directory empty)"
            if ckpt.get("partial"):
                detail = f"({len(ckpt['partial'])} look partial)"
            elif ckpt.get("other"):
                names = ", ".join(r["name"] for r in ckpt["other"][:3])
                detail = (f"({len(ckpt['other'])} file(s) present but none look "
                          f"like a checkpoint: {names}; use --checkpoint-glob "
                          f"if the naming is unusual)")
            reasons.append(f"no complete checkpoint files {detail}")

    # A criterion chosen after seeing the curve cannot establish convergence.
    # Warning about that while exiting 0 was the tool contradicting itself.
    if state == "CONVERGED" and contract.get("retrospective"):
        state = "INCOMPLETE_EVIDENCE"
        reasons.append("criterion was declared retrospectively; the run matches "
                       "it, but this cannot establish convergence")

    if state == "RUNNING" and contract.get("preemptible"):
        sstate = (slurm_state(jid, declared_at, bound_at,
                              newest_evidence, ordering_notes)
                  if jid else None)
        if sstate in ("PREEMPTED", "REQUEUED", "SUSPENDED"):
            state = "PREEMPTED"
            reasons.append(f"scheduler reports {sstate} for job {jid}; "
                           f"a requeue is expected, not a failure")
        else:
            reasons.append("preemptible: a requeue is expected, not a failure")

    for n in ordering_notes:
        reasons.append(f"note: {n}")

    if integrity and state not in ("CONTRACT_VIOLATED",):
        reasons.extend(f"note: {p}" for p in integrity)

    verification = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": now_iso(),
        # WHICH contract instance this verdict is about; see contract.py.
        "contract_id": contract.get("contract_id"),
        "criteria_digest": contract.get("criteria_digest"),
        "state": state,
        "exit_code": STATES[state],
        "retrospective": contract.get("retrospective", False),
        "reasons": reasons,
        "evaluations": len(rows),
        "last_step": max((r["step"] for r in rows), default=None),
        "checkpoint": ckpt,
    }
    werr = write_receipt(run_dir / VERIFICATION, verification)
    if werr:
        # The verdict still has to reach the caller even when it cannot be
        # persisted; failing here silently would be the worst outcome.
        print(f"WARNING: could not write {VERIFICATION}: {werr}",
              file=sys.stderr)

    if args.json:
        print(json.dumps(verification, indent=2))
    else:
        print(f"{state}  ({run_dir})")
        for r in reasons:
            print(f"  - {r}")
        if ckpt.get("newest"):
            n = ckpt["newest"]
            tier = ckpt.get("tier") or {}
            print(f"  checkpoint: {n['name']} ({n['size']} B) on "
                  f"{ckpt.get('filesystem')}"
                  + (f" [{tier.get('used_pct')} used of {tier.get('capacity')}]"
                     if tier else ""))
            step = step_from_name(n["name"])
            if step is not None:
                print(f"    selected by recency, step {step} -- if you pick a "
                      f"different one after seeing the curve, that is selection, "
                      f"not convergence")
        if ckpt.get("partial"):
            print(f"  WARNING: {len(ckpt['partial'])} partial/zero-byte "
                  f"checkpoint file(s) present")
        if state == "BUDGET_EXHAUSTED":
            print("\n  Training stopped because it ran out of budget, not "
                  "because it converged. Do not report this as a trained model.")
        if verification["retrospective"]:
            print("\n  NOTE: retrospective -- cannot establish convergence.")

    disarm_watchdog()
    sys.exit(STATES[state])


def main():
    ap = argparse.ArgumentParser(prog="traincontract.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="declare convergence BEFORE training")
    p.add_argument("run_dir")
    p.add_argument("--metrics", required=True, help="JSONL metrics file")
    p.add_argument("--checkpoint-dir", default=None)
    p.add_argument("--sparse-metric", action="store_true",
                   help="the convergence metric is logged less often than other "
                        "metrics by design; without this, evaluations inside "
                        "the window that carry no value for it block a verdict")
    p.add_argument("--checkpoint-glob", default=None,
                   help="fnmatch pattern identifying checkpoint files when the "
                        "naming is unusual (default: known model suffixes)")
    p.add_argument("--converge", default=None,
                   help='JSON criterion, e.g. \'{"metric":"val_loss","mode":"min",'
                        '"rel_improvement_below":0.002,"over_evals":5,'
                        '"min_steps":10000}\'')
    p.add_argument("--diverge", action="append", default=[],
                   help='JSON blow-up rule, e.g. \'{"metric":"train_loss",'
                        '"above":100}\'; repeatable')
    p.add_argument("--max-steps", type=int, default=None,
                   help="budget; reaching it without the criterion is "
                        "BUDGET_EXHAUSTED, not convergence")
    p.add_argument("--expect-eval-every", type=int, default=None,
                   help="expected step interval between evals; enables gap detection")
    p.add_argument("--preemptible", action="store_true")
    p.add_argument("--retrospective", action="store_true",
                   help="audit a finished run; cannot establish convergence")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("bind",
                       help="bind a Slurm job id to this contract after "
                            "sbatch, when init could not capture $SLURM_JOB_ID")
    p.add_argument("run_dir")
    p.add_argument("--job-id", required=True)
    p.add_argument("--force", action="store_true",
                   help="replace an existing binding to a DIFFERENT job id")
    p.set_defaults(fn=cmd_bind)

    p = sub.add_parser("record",
                       help="record the terminal outcome of a run not managed "
                            "by Slurm (mirrors contract.py record)")
    p.add_argument("run_dir")
    p.add_argument("--exit-code", type=int, required=True)
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("check", help="verify against the declared criterion")
    p.add_argument("run_dir")
    p.add_argument("--watchdog", type=int, default=600,
                   help="emit INCOMPLETE_EVIDENCE and exit if check overruns "
                        "this many seconds (0 disables); guards against an "
                        "unresponsive filesystem, which no open() flag can")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
