#!/usr/bin/env python3
"""contract.py — declare, then verify, what "done" means for a batch job.

The premise: a scheduler reporting COMPLETED, or a process exiting 0, is not
evidence that the work produced anything. Python that catches an exception and
returns cleanly, a pipeline stage that emits a zero-row table, a training run
that hit its wall-clock limit -- all of these look like success to `sacct`.

So the contract is declared BEFORE the run and checked by this script, which
did not do the work and has no stake in its succeeding.

Usage:
    contract.py init   <run-dir> --command CMD [--output PATH ...] [options]
    contract.py submit <run-dir> [--sbatch-arg ARG ...]
    contract.py record <run-dir> --exit-code N     # a run not made via submit
    contract.py check  <run-dir> [--json]

Exit codes from `check` (also the verification state):
    0  SCIENTIFIC_PASS        terminal, and every declared predicate holds
    1  RUNNING                not terminal yet -- re-check later
    2  FAILED                 scheduler or wrapper reports failure
    3  TECHNICALLY_COMPLETE   exited cleanly, predicates NOT met  <-- the point
    4  CONTRACT_VIOLATED      ran outside its declared scope, or inputs drifted
    5  PREEMPTED              requeued/preempted; another attempt is expected
    6  INCOMPLETE_EVIDENCE    cannot determine -- missing logs or scheduler data

Python 3.7+ (subprocess.run(capture_output=...)), standard library only, no
network. Verified on chimera 3.10.12, lambda 3.12.3, andromeda 3.10.12.
"""

import argparse
import calendar
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
CONTRACT = "contract.json"
ATTEMPTS = "attempts.jsonl"
VERIFICATION = "verification.json"

STATES = {
    "SCIENTIFIC_PASS": 0,
    "RUNNING": 1,
    "FAILED": 2,
    "TECHNICALLY_COMPLETE": 3,
    "CONTRACT_VIOLATED": 4,
    "PREEMPTED": 5,
    "INCOMPLETE_EVIDENCE": 6,
}

# Slurm states that mean "this attempt is over and it did not succeed".
SLURM_FAILED = {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
                "BOOT_FAIL", "DEADLINE", "REVOKED", "SPECIAL_EXIT"}
SLURM_PREEMPTED = {"PREEMPTED", "REQUEUED", "RESIZING", "SUSPENDED"}
SLURM_OK = {"COMPLETED"}

# Every state in which Slurm has FINISHED with a job, DERIVED from the two sets
# above rather than listed again. squeue keeps a finished job listed for
# MinJobAge (default 300s), so treating any owned row as "still active" made
# the verifier report RUNNING and never evaluate the predicates. Terminal is
# the enumerable set; anything NOT here reads as active, so an unrecognised
# state still fails toward "not finished" rather than certifying a live job --
# the property the earlier behaviour protected ("enumerating live states missed
# real ones such as STAGE_OUT").
#
# Derived, because listing it separately immediately drifted: SPECIAL_EXIT was
# added here and not to SLURM_FAILED, so an sacct row in that state matched no
# classification and fell through to RUNNING forever. Both reviewers found it
# independently, in the same commit that fixed the classification it broke.
SLURM_TERMINAL_IN_QUEUE = frozenset(SLURM_OK | SLURM_FAILED)


# --- small helpers ----------------------------------------------------------

MAX_PREDICATE_READ_BYTES = 256 * 1024 * 1024

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



def append_line(path, line):
    """Append one line without blocking. open('a') hangs on a FIFO with no
    reader, so the descriptor is opened non-blocking and fstat-checked."""
    fd = None
    try:
        fd = os.open(str(path),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND
                     | getattr(os, "O_NONBLOCK", 0), 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return f"not a regular file ({stat.filemode(st.st_mode)})"
        os.write(fd, (line + "\n").encode())
        return None
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


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
def read_text_bounded(path):
    """(text, error). Regular files only, size-capped, non-blocking open.

    A plain read_text() here can block forever on a FIFO and exhaust memory on
    a large log; neither leaves a usable verdict."""
    import stat as _stat
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            return "", f"not a regular file ({_stat.filemode(st.st_mode)})"
        if st.st_size > MAX_PREDICATE_READ_BYTES:
            return "", (f"{st.st_size} bytes, above the "
                        f"{MAX_PREDICATE_READ_BYTES}-byte read limit")
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = None
            raw = fh.read(MAX_PREDICATE_READ_BYTES + 1)
        return raw.decode("utf-8", errors="replace"), None
    except (OSError, MemoryError) as e:
        return "", f"unreadable: {type(e).__name__}: {e}"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run(argv, cwd=None, timeout=30):
    """Run a command, returning (rc, stdout, stderr). Never raises, never hangs.

    start_new_session + killpg: a repository can configure diff.external to
    spawn a descendant that inherits the captured pipe, and killing only the
    child leaves the wait blocked on EOF forever.
    errors="replace": undecodable bytes must not raise UnicodeDecodeError.
    """
    pr = None
    try:
        pr = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8",
                              errors="replace", start_new_session=True)
        out, err = pr.communicate(timeout=timeout)
        return pr.returncode, (out or "").strip(), (err or "").strip()
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
        return 127, "", f"timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as e:
        return 127, "", str(e)


def git(cwd, *args):
    rc, out, _ = run(["git", "-C", str(cwd), *args])
    return out if rc == 0 else ""


def sha256_file(path, limit_bytes=None):
    """Digest a regular file. limit_bytes caps work on huge inputs (weak rung).

    Refuses anything that is not a regular file: if a declared input is replaced
    by a FIFO or a character device, opening it can block forever and the
    verifier would never emit a verdict at all."""
    import stat as _stat
    st = os.stat(path)
    if not _stat.S_ISREG(st.st_mode):
        raise OSError(f"not a regular file (mode {_stat.filemode(st.st_mode)})")
    h = hashlib.sha256()
    read = 0
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb", closefd=True) as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            read += len(chunk)
            if limit_bytes and read >= limit_bytes:
                return h.hexdigest(), True  # truncated
    return h.hexdigest(), False


def identify_input(path, hash_limit_mb=256):
    """Input identity on a declared ladder, strongest rung that is affordable.

    Hashing every byte of a multi-TB dataset is not viable, so the rung used is
    recorded explicitly and weaker rungs are marked as weak evidence.
    """
    p = Path(path)
    try:
        if not p.exists():
            return {"path": str(p), "rung": "missing", "weak": True}
        st = p.stat()
    except OSError as e:
        return {"path": str(p), "rung": "unreadable", "weak": True,
                "error": str(e)}
    if p.is_dir():
        try:
            entries = len(list(p.iterdir()))
        except OSError:
            entries = None
        return {"path": str(p), "rung": "dir-mtime-size", "weak": True,
                "mtime": int(st.st_mtime), "entries": entries}
    try:
        if st.st_size <= hash_limit_mb * (1 << 20):
            digest, truncated = sha256_file(p)
            return {"path": str(p), "rung": "content-digest", "weak": False,
                    "sha256": digest, "size": st.st_size,
                    "mtime": int(st.st_mtime)}
        digest, _ = sha256_file(p, limit_bytes=hash_limit_mb * (1 << 20))
    except OSError as e:
        return {"path": str(p), "rung": "unreadable", "weak": True,
                "error": str(e)}
    return {"path": str(p), "rung": "prefix-digest", "weak": True,
            "sha256_prefix": digest, "prefix_mb": hash_limit_mb,
            "size": st.st_size, "mtime": int(st.st_mtime)}


def env_identity():
    """Record which interpreter and environment actually ran, not which was
    nominally configured."""
    env = {
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "hostname": os.uname().nodename,
        "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
    }
    for key in ("CONDA_PREFIX", "CONDA_DEFAULT_ENV", "VIRTUAL_ENV",
                "SLURM_JOB_PARTITION", "SLURM_JOB_ACCOUNT"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    # NCCL settings materially change multi-node performance; a run that
    # silently lost InfiniBand and fell back to TCP should be explainable.
    nccl = {k: v for k, v in os.environ.items() if k.startswith("NCCL_")}
    if nccl:
        env["nccl"] = nccl
    return env


# https://user:pass@host/repo is an ordinary git remote and a credential in
# plain sight. Recorded verbatim, it reached every contract and every handoff,
# and `resume` printed it when listing code differences (deepseek, CRITICAL).
USERINFO_URL = re.compile(r"^([a-zA-Z][\w+.-]*://)([^/@]*@)")


def scrub_url(value):
    """Strip userinfo from a URL, keeping the rest legible."""
    if not isinstance(value, str):
        return value
    return USERINFO_URL.sub(r"\1[redacted]@", value)


def repo_state(cwd):
    """Git identity of the code that ran, including a digest of uncommitted
    changes -- a dirty tree is still reproducible if we fingerprint the diff."""
    if not git(cwd, "rev-parse", "--git-dir"):
        return {"tracked": False}
    diff = git(cwd, "diff", "HEAD")
    return {
        "tracked": True,
        "commit": git(cwd, "rev-parse", "HEAD"),
        "branch": git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(diff),
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest() if diff else None,
        "remote": scrub_url(git(cwd, "config", "--get",
                                        "remote.origin.url")),
    }


# --- predicates -------------------------------------------------------------
#
# A predicate is a mechanical claim about an output that must hold for the run
# to count. Kept deliberately small: a few structural checks plus an escape
# hatch to a shell command for anything domain-specific.

# Every predicate kind and the exact keys it reads. Enumerated, not inferred:
# an unrecognised key used to be silently ignored, so a typo'd criterion
# (`{"kind":"min_lines","min":3}` -- the reader is `lines`) fell back to the
# default of 1 and the declared criterion became WEAKER than declared with no
# warning. Found by real use on lambda. The residual (unknown key) must be the
# refusal, never the lenient default.
PREDICATE_SCHEMA = {
    "exists":      {"required": ("path",),            "optional": ()},
    "min_size":    {"required": ("path",),            "optional": ("bytes",)},
    "min_lines":   {"required": ("path",),            "optional": ("lines",)},
    "log_matches": {"required": ("path", "pattern"),  "optional": ("expect",)},
    "command":     {"required": ("run",),             "optional": ("timeout",)},
}


def predicate_fault(pred):
    """Return a human-readable fault string, or None if the predicate is
    interpretable. Names the allowed keys so the refusal carries the fix."""
    if not isinstance(pred, dict):
        return (f"predicate is not an object: {pred!r}. Use a JSON object, "
                f'e.g. {{"kind":"exists","path":"out.tsv"}}')
    kind = pred.get("kind")
    spec = PREDICATE_SCHEMA.get(kind)
    if spec is None:
        return (f"unknown predicate kind {kind!r}; use one of: "
                f"{', '.join(sorted(PREDICATE_SCHEMA))}")
    allowed = {"kind", *spec["required"], *spec["optional"]}
    readable = sorted(allowed - {"kind"})
    missing = [k for k in spec["required"] if k not in pred]
    if missing:
        # Name the fix, not just the fault: a refusal a user cannot act on is
        # a defect here even when the refusal is correct (kimi-k2.7-code).
        example = {"kind": kind}
        example.update({k: "..." for k in spec["required"]})
        return (f"{kind} predicate is missing required key(s) "
                f"{', '.join(missing)}; add them, e.g. "
                f"{json.dumps(example)}")
    # Annotation keys are opt-in and underscore-prefixed. Refusing EVERY
    # unlisted key also refused a criterion the previous version evaluated
    # exactly as the author intended -- `{"lines":3,"description":"output"}`
    # (deepseek-v4-pro, MAJOR). Refusing an honest declaration is as serious
    # here as accepting a dishonest one, so there has to be a way to say
    # "this key is documentation, not criteria".
    for key in sorted(pred):
        if not key.startswith("_"):
            continue
        # ...but an underscored form of a key the kind READS is a typo, not an
        # annotation. Without this, `_lines` would be ignored, `lines` would
        # default, and the criterion would be silently weakened again -- the
        # very defect this function exists to stop, reintroduced through the
        # escape hatch added to fix a different one.
        if key.lstrip("_") in allowed:
            return (f"{kind} predicate has {key!r}, which reads as an "
                    f"annotation and is ignored. Did you mean "
                    f"{key.lstrip('_')!r}? Rename it, or use a name that is "
                    f"not one of {', '.join(readable)}.")
    unknown = sorted(k for k in set(pred) - allowed if not k.startswith("_"))
    if unknown:
        return (f"{kind} predicate has unrecognised key(s) "
                f"{', '.join(unknown)}; it reads only "
                f"{', '.join(readable)}. A typo here would silently weaken "
                f"the criterion. If these are notes, prefix them with '_' "
                f"and they will be ignored.")
    return None


def check_predicate(pred, base_dir):
    """Return (ok, detail). Never raises -- enforced by the wrapper above."""
    fault = predicate_fault(pred)
    if fault:
        return False, fault
    kind = pred.get("kind")
    target = pred.get("path", "")
    # Resolve against the contract's cwd so the verdict does not depend on
    # where the verifier was invoked from.
    p = Path(target)
    if not p.is_absolute():
        p = Path(base_dir) / p

    if kind == "exists":
        return (p.exists(), f"{target} {'exists' if p.exists() else 'MISSING'}")

    if kind == "min_size":
        if not p.exists():
            return False, f"{target} MISSING"
        size = p.stat().st_size
        want = int(pred.get("bytes", 1))
        return (size >= want, f"{target} is {size}B (need >= {want}B)")

    if kind == "min_lines":
        if not p.exists():
            return False, f"{target} MISSING"
        want = int(pred.get("lines", 1))
        text, err = read_text_bounded(p)
        if err:
            return False, f"{target}: {err}"
        n = 0
        for n, _ in enumerate(text.splitlines(), 1):
            if n >= want:
                break
        return (n >= want, f"{target} has >= {n} lines (need >= {want})")

    if kind == "log_matches":
        if not p.exists():
            return False, f"{target} MISSING"
        needle = pred.get("pattern", "")
        # Bounded, regular-file-only, non-blocking: an unbounded read here OOMs
        # on a large log, and a FIFO blocks forever with no exception to catch.
        text, err = read_text_bounded(p)
        if err:
            return False, f"{target}: {err}"
        found = needle in text
        want = pred.get("expect", True)
        return (found == want,
                f"{target} {'contains' if found else 'lacks'} {needle!r} (expected {'present' if want else 'absent'})")

    if kind == "command":
        # Escape hatch: any domain check expressible as a shell exit code.
        # NOTE: runs UNSANDBOXED with the verifier's privileges. A hostile
        # predicate can SIGKILL this process; no handler can prevent that.
        # Treat contract.json as trusted input -- see SKILL.md.
        if not isinstance(pred.get("run"), str):
            return False, f"command predicate `run` must be a string, got {type(pred.get('run')).__name__}"
        rc, out, err = run(["sh", "-c", pred["run"]], cwd=str(base_dir),
                           timeout=int(pred.get("timeout", 300)))
        return (rc == 0, f"`{pred['run']}` -> rc={rc} {(out or err)[:200]}")

    return False, f"unknown predicate kind: {kind!r}"


def evaluate_predicate(pred, base_dir):
    """Filesystem-facing wrapper. A predicate must never be able to crash the
    verifier: an output deleted between exists() and stat(), an unreadable path,
    a predicate that is not even an object -- all become an ordinary FAIL with a
    reason, never a traceback that leaves no verification.json behind.

    base_dir is the contract's recorded cwd. Relative predicate paths resolve
    against it, NOT against wherever `check` happens to be run from: resolving
    against the caller's cwd made a correct run look unmet, and could make a
    failed run pass if an unrelated same-named file existed there."""
    if not isinstance(pred, dict):
        return False, f"predicate is not an object: {pred!r}"
    try:
        return check_predicate(pred, base_dir)
    except OSError as e:
        return False, f"{pred.get('path') or pred.get('kind') or '?'}: I/O error: {e}"
    except (SystemExit, KeyboardInterrupt):
        # The watchdog exits via SystemExit; swallowing it here made check
        # print the timeout verdict and then continue to a second verdict.
        raise
    except BaseException as e:
        return False, f"predicate {pred!r} raised {type(e).__name__}: {e}"


# --- scheduler evidence -----------------------------------------------------

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


# Every field that can change the verdict. traincontract.py has fingerprinted
# its criteria since round 4; contract.py had only artifact mtimes, so editing
# a predicate after the run to match what the run happened to produce was
# undetectable (luna). The two tools now mean the same thing by provenance.
DIGESTED_FIELDS = (
    # What the verdict is measured against.
    "predicates", "declared_outputs", "command", "cwd",
    "retrospective", "preemption_expected", "inputs",
    # And the provenance anchors themselves. Fingerprinting only the criteria
    # left the anchors editable: setting created_at_epoch to 0 made every
    # pre-existing artifact fresh, and rewriting contract_id re-bound a stale
    # attempt, neither of which changed the digest (luna).
    "created_at", "created_at_epoch", "contract_id",
)


def criteria_digest(contract):
    """Stable fingerprint of every field that decides the verdict."""
    payload = json.dumps({k: (contract or {}).get(k) for k in DIGESTED_FIELDS},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


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
                       "sacct row cannot be placed relative to it. Re-declare "
                       "the contract with `init --force` so it records a "
                       "created_at, then re-submit.")
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


# A directory tree is walked only this far when judging freshness. An
# unbounded walk on a scratch directory with millions of entries would stall
# the verifier; the watchdog would catch it, but reporting INCOMPLETE_EVIDENCE
# because we gave up counting is worse than sampling and saying so.
MAX_DIR_ENTRIES_SCANNED = 20_000


def newest_declared_mtime(contract, base_dir):
    """Newest mtime among the declared outputs and predicate paths, or None.

    Its own pass, because the scheduler block needs it and the predicate walk
    runs after that block. Directories are walked, bounded the same way.
    """
    specs = list(contract.get("declared_outputs") or [])
    for pred in (contract.get("predicates") or []):
        if isinstance(pred, dict) and isinstance(pred.get("path"), str):
            specs.append(pred["path"])
    newest = None
    for spec in specs:
        if not isinstance(spec, str) or not spec:
            continue
        pth = Path(spec)
        if not pth.is_absolute():
            pth = Path(base_dir) / pth
        try:
            if pth.is_dir():
                _f, _s, _t, dnew = directory_freshness(pth, None, None)
                cand = dnew
            elif pth.exists():
                cand = pth.stat().st_mtime
            else:
                cand = None
        except OSError:
            cand = None
        if cand is not None:
            newest = cand if newest is None else max(newest, cand)
    return newest


def directory_freshness(path, declared_at, declared_epoch):
    """(has_fresh_file, scanned, truncated, newest_mtime) for a directory.

    A directory's OWN mtime records when entries were added or removed, not
    when their contents changed, so it is not evidence about the data. Judging
    a declared directory by it passed a contract whose outputs were entirely a
    previous run's: one new entry made the directory look fresh while every
    file in it predated the contract. Same shape as a TensorFlow checkpoint
    set judged by its newest member.

    A directory is a container rather than a single artifact, so the rule is
    weaker than for a checkpoint set: at least ONE regular file must post-date
    the contract. Old files sitting alongside new output are normal.
    """
    scanned, newest = 0, None
    found_fresh = False
    for root, dirs, files in os.walk(str(path)):
        for name in files:
            scanned += 1
            if scanned > MAX_DIR_ENTRIES_SCANNED:
                return found_fresh, scanned - 1, True, newest
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            newest = (st.st_mtime if newest is None
                      else max(newest, st.st_mtime))
            if artifact_is_fresh(st.st_mtime, declared_at, declared_epoch):
                # Keep walking: the caller needs the NEWEST mtime for the
                # terminal-record ordering guard, not just the first fresh hit.
                found_fresh = True
        del dirs
    return found_fresh, scanned, False, newest


def preexisting_fingerprints(specs, base_dir, hash_limit_mb):
    """Digest each declared output that already exists, keyed by path.

    A file that does not exist yet needs no fingerprint: whatever appears there
    later can only have come from this run or later. It is the ALREADY PRESENT
    output that mtime cannot speak for.
    """
    out = {}
    for spec in specs:
        if not isinstance(spec, str) or not spec:
            continue
        pth = Path(spec)
        if not pth.is_absolute():
            pth = Path(base_dir) / pth
        try:
            if not pth.is_file():
                continue
            st = pth.stat()
            digest, truncated = sha256_file(pth, limit_bytes=hash_limit_mb
                                            * (1 << 20))
        except OSError:
            continue
        out[str(spec)] = {"sha256": digest, "size": st.st_size,
                          "prefix_only": bool(truncated)}
    return out


def unchanged_since_declaration(spec, base_dir, contract, hash_limit_mb):
    """Whether this declared output is byte-identical to the file that was
    already there when the contract was declared. None when unknowable."""
    fps = contract.get("preexisting_outputs")
    if not isinstance(fps, dict):
        return None                       # contract predates the field
    fp = fps.get(str(spec))
    if not isinstance(fp, dict) or not fp.get("sha256"):
        return None                       # nothing was there to compare
    pth = Path(spec)
    if not pth.is_absolute():
        pth = Path(base_dir) / pth
    try:
        if not pth.is_file():
            return None
        if pth.stat().st_size != fp.get("size"):
            return False                  # different length, different file
        digest, _ = sha256_file(pth, limit_bytes=hash_limit_mb * (1 << 20))
    except OSError:
        return None
    return digest == fp["sha256"]


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


def exit_code_is_clean(code):
    """Whether an sacct ExitCode field positively represents success.

    sacct renders it as "<exit>:<signal>". An ABSENT code is not evidence of a
    clean exit -- assuming it was let COMPLETED-with-no-code count as positive
    terminal evidence. Where sacct does not populate it, use `record` to
    declare the outcome explicitly."""
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


def sacct_state(job_id, declared_at=None, bound_at=None,
                newest_evidence_mtime=None, note_out=None):
    """Terminal state of OUR job from Slurm accounting.

    Returns (state, exit_code, submit, why_not). An all-None state is absent
    evidence, never failure.

    Ownership is applied to EVERY row and the last OWNED one wins. Taking
    rows[-1] and testing only that discarded an honest COMPLETED row whenever a
    later job reused the id, because the reuse's row sorts last and fails the
    test, so a successful run reported INCOMPLETE_EVIDENCE (kimi). The squeue
    path was fixed to scan every row one commit earlier and this one was not,
    which is the twelfth time in this repo a rule landed in one place and not
    its twin.

    Last-owned rather than first-owned: a requeued job has one row per attempt,
    oldest first, and reading the first pinned it at PREEMPTED permanently.
    """
    if not shutil.which("sacct"):
        return None, None, None, None
    rc, out, _ = run(["sacct", "-n", "-X", "-P", "-j", str(job_id),
                      "-o", "State,ExitCode,Submit,End"])
    if rc != 0 or not out:
        return None, None, None, None
    chosen, last_why, saw = None, None, None
    if note_out is None:
        note_out = []
    for line in out.splitlines():
        if not line.strip():
            continue
        f = line.split("|")
        submit = f[2].strip() if len(f) > 2 else ""
        saw = submit
        ours, why = sacct_row_is_ours(parse_iso_ts(submit), declared_at,
                                      bound_at)
        if not ours:
            last_why = why
            continue
        state = f[0].strip().split()[0] if f[0].strip() else None
        end = parse_iso_ts(f[3].strip()) if len(f) > 3 else None
        # Ordering is REPORTED, not enforced. Enforced for one round, three
        # findings across two reviewers showed why: it cannot say WHICH attempt
        # produced an artifact, so a later no-op job certified an earlier run's
        # outputs anyway (kimi, CRITICAL); an archiving or sync script touching
        # an output after the run advances the evidence past the job's End and
        # REFUSES honest evidence (deepseek); and where sacct omits End it skips
        # entirely (deepseek). Artifact-to-attempt attribution is not derivable
        # from timestamps. A contract certifies ONE run.
        if state in SLURM_OK and not terminal_record_postdates(
                end, newest_evidence_mtime):
            note_out.append("the scheduler row for this job ended before the "
                            "declared output(s) it certifies; if this "
                            "directory was reused, that job is not the one "
                            "which produced them")
        chosen = (state, f[1].strip() if len(f) > 1 else None, submit)
    if chosen is None:
        return None, None, saw, last_why
    return chosen[0], chosen[1], chosen[2], None


def squeue_active(job_id, declared_at=None, bound_at=None):
    """Whether OUR job is still in the queue.

    Ownership applies here exactly as it does to an sacct row, and for eight
    rounds it did not: `-o %T` fetched only the state, so a later reuse of the
    id that happened to be RUNNING made an honestly finished run report RUNNING
    forever, its predicates never evaluated (sol). %V is the submit time, which
    is what places the row.
    """
    if not shutil.which("squeue"):
        return False
    rc, out, _ = run(["squeue", "-h", "-j", str(job_id), "-o", "%T|%V"])
    if rc != 0 or not out.strip():
        return False
    # EVERY row, not just the first. squeue returns several for job arrays and,
    # exactly in the case this ownership test exists for, when an id has been
    # reused: a stale row printed first made this return "not active" and our
    # own live row further down was never read (deepseek). sacct_state has
    # taken the last row for requeues since round 2; this path took the first.
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        submit = parse_iso_ts(parts[1].strip()) if len(parts) > 1 else None
        ours, _why = sacct_row_is_ours(submit, declared_at, bound_at)
        if ours:
            # An owned row whose state is TERMINAL is not evidence of activity:
            # squeue lists a finished job for MinJobAge.
            state = parts[0].strip().split()[0] if parts[0].strip() else ""
            if state.upper() in SLURM_TERMINAL_IN_QUEUE:
                continue
            return True
    # No owned row that is still live. Not evidence that OUR job is running, so
    # this fails toward "not active" and the sacct path decides -- applying the
    # same ownership test.
    return False


# --- commands ---------------------------------------------------------------

def cmd_init(args):
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    cpath = run_dir / CONTRACT
    if cpath.exists() and not args.force:
        sys.exit(f"error: {cpath} exists (use --force to replace)")

    predicates = []
    for spec in args.output:
        predicates.append({"kind": "exists", "path": spec})
        predicates.append({"kind": "min_size", "path": spec, "bytes": 1})
    for spec in args.predicate:
        try:
            pred = json.loads(spec)
        except json.JSONDecodeError as e:
            sys.exit(f"error: --predicate is not valid JSON: {e}\n  {spec}\n"
                     f'  Pass one JSON object per --predicate, e.g. '
                     f'--predicate \'{{"kind":"exists","path":"out.tsv"}}\'')
        # Refuse at declare time. A malformed predicate caught only at check
        # time has already let the run proceed under a criterion that is not
        # the one the user wrote.
        fault = predicate_fault(pred)
        if fault:
            sys.exit(f"error: {fault}\n  {spec}")
        predicates.append(pred)

    if not predicates:
        sys.exit("error: a contract with no predicates cannot verify anything; "
                 "pass --output or --predicate")

    contract = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        # Sub-second companion to created_at. The ISO form has only second
        # resolution, so an artifact written earlier in the same second was
        # indistinguishable from one written after the contract (luna).
        "created_at_epoch": time.time(),
        "retrospective": bool(args.retrospective),
        # Per-instance nonce. A content digest is not enough: `init --force`
        # with identical criteria yields an identical digest, so a stale
        # record from the previous run still matched.
        "contract_id": os.urandom(8).hex(),
        "command": args.command,
        "cwd": str(Path(args.cwd or os.getcwd()).resolve()),
        "repo": repo_state(Path(args.cwd or os.getcwd())),
        "environment": env_identity(),
        "inputs": [identify_input(
            i if os.path.isabs(i)
            else os.path.join(str(Path(args.cwd or os.getcwd()).resolve()), i),
            args.hash_limit_mb) for i in args.input],
        "declared_outputs": list(args.output),
        # Fingerprints of declared outputs that ALREADY EXIST at declaration
        # time. mtime is the only cheap provenance signal a filesystem gives,
        # and `touch` defeats it: a file from a previous run, touched after
        # init, passed every freshness check (kimi, CRITICAL). The accidental
        # form matters more than the adversarial one, because `cp` without -p
        # updates mtimes, so a stale artifact copied into place looks new.
        #
        # Content settles it: if a declared output is byte-identical to what
        # was there when the contract was declared, this run did not produce
        # it, whatever its mtime says. Only pre-existing outputs are digested,
        # which is exactly the suspicious case and costs nothing in the normal
        # one where the file does not exist yet.
        # The limit must match at check time or the digests cannot be
        # compared at all.
        "hash_limit_mb": int(args.hash_limit_mb),
        "preexisting_outputs": preexisting_fingerprints(
            list(args.output) + [pr.get("path") for pr in predicates
                                 if isinstance(pr, dict)],
            args.cwd or os.getcwd(), args.hash_limit_mb),
        "predicates": predicates,
        # Recorded for audit only -- NOT enforced. Enforcing would require
        # observing everything the job wrote, which this tool does not do.
        # Naming it honestly beats implying a guarantee that does not exist.
        "declared_write_scopes_unenforced": list(args.write_scope),
        "preemption_expected": bool(args.preemptible),
        # Filled in below, once the fields it covers exist.
        "criteria_digest": None,
    }
    contract["criteria_digest"] = criteria_digest(contract)
    cpath.write_text(json.dumps(contract, indent=2) + "\n")
    if contract["retrospective"]:
        print("WARNING: retrospective contract -- criteria were declared AFTER "
              "the run and carry weaker assurance.", file=sys.stderr)
    print(f"wrote {cpath}")
    print(f"  {len(predicates)} predicate(s), {len(contract['inputs'])} input(s)")
    weak = [i for i in contract["inputs"] if i.get("weak")]
    if weak:
        print(f"  {len(weak)} input(s) identified by weak evidence "
              f"(too large to digest fully)")


def cmd_submit(args):
    run_dir = Path(args.run_dir).resolve()
    cpath = run_dir / CONTRACT
    if not cpath.exists():
        sys.exit(f"error: no contract at {cpath}; run `contract.py init` first")
    try:
        raw, rerr = read_text_bounded(cpath)
        if rerr:
            raise ValueError(f"contract unreadable: {rerr}")
        contract = json.loads(raw)
    except (OSError, ValueError, RecursionError) as e:
        sys.exit(f"error: contract unreadable or malformed: {e}")
    shape = contract_problems(contract)
    if shape:
        sys.exit("error: unusable contract: " + "; ".join(shape[:3]))

    if not shutil.which("sbatch"):
        sys.exit("error: sbatch not found on PATH")

    script = run_dir / "job.sbatch"
    if not script.exists():
        sys.exit(f"error: expected a batch script at {script}")

    argv = ["sbatch", "--parsable", *args.sbatch_arg, str(script)]
    rc, out, err = run(argv, cwd=str(run_dir), timeout=60)
    if rc != 0:
        sys.exit(f"error: sbatch failed: {err or out}")
    job_id = out.split(";")[0].strip()

    attempt = {
        "contract_id": contract.get("contract_id"),
        "attempt": _next_attempt(run_dir),
        "job_id": job_id,
        "submitted_at": now_iso(),
        "sbatch_args": list(args.sbatch_arg),
        "host": os.uname().nodename,
    }
    aerr = append_line(run_dir / ATTEMPTS, json.dumps(attempt))
    if aerr:
        sys.exit(f"error: cannot record the attempt: {aerr}")
    print(f"submitted job {job_id} (attempt {attempt['attempt']})")
    print(f"verify with: contract.py check {run_dir}")


def _next_attempt(run_dir):
    path = run_dir / ATTEMPTS
    if not path.exists():
        return 1
    raw, err = read_text_bounded(path)
    if err:
        return 1
    return sum(1 for line in raw.splitlines() if line.strip()) + 1


def _attempts(run_dir):
    path = run_dir / ATTEMPTS
    out = []
    if not path.exists():
        return out
    raw, err = read_text_bounded(path)
    if err:
        # A FIFO or oversized attempts log must not hang or crash the verifier.
        return out
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, RecursionError, ValueError):
            continue
        # Only well-formed records count. A hand-edited or half-written line
        # must not be able to crash the verifier into a permanent RUNNING.
        if not isinstance(rec, dict):
            continue
        jid = rec.get("job_id")
        if isinstance(jid, bool) or not isinstance(jid, (str, int)):
            continue
        # Strict typing matters: `False == 0` is True in Python, so an
        # exit_code of JSON false would otherwise read as a clean exit, and a
        # truthy string would satisfy `terminal`. Both manufacture a pass.
        if "terminal" in rec and rec["terminal"] is not True:
            continue
        if "exit_code" in rec:
            ec = rec["exit_code"]
            if isinstance(ec, bool) or not isinstance(ec, int):
                continue
        out.append(rec)
    return out


def owned_attempts(attempts, contract):
    """Attempts belonging to THIS contract instance.

    An unbound attempts log survived `init --force`, so a stale local exit-0
    record certified a run that never happened under the new contract."""
    want = contract.get("contract_id")
    if not isinstance(want, str) or not want.strip():
        # Returning ALL attempts here made the binding opt-out, the same
        # absent-field bypass already closed for criteria_digest. contract_id
        # is digested now and every `init` writes one, so an absent one is a
        # malformed contract, not an old one; nothing predating it exists.
        return []
    return [a for a in attempts if a.get("contract_id") == want]


def _load_contract_for_record(run_dir):
    raw, err = read_text_bounded(run_dir / CONTRACT)
    if err:
        sys.exit(f"error: contract unreadable: {err}")
    try:
        c = json.loads(raw)
    except (ValueError, RecursionError) as e:
        sys.exit(f"error: contract malformed: {e}")
    if not isinstance(c, dict):
        sys.exit("error: contract is not a JSON object")
    return c


def cmd_record(args):
    """Record the terminal outcome of a run executed outside `submit`.

    Not every job goes through sbatch -- interactive sessions, salloc, and other
    runners all produce real results with no sacct row. Recording the exit code
    explicitly is honest evidence of termination; silently assuming it would not
    be."""
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / CONTRACT).exists():
        sys.exit(f"error: no contract at {run_dir / CONTRACT}")
    contract = _load_contract_for_record(run_dir)
    attempt = {
        "contract_id": contract.get("contract_id"),
        "attempt": _next_attempt(run_dir),
        "job_id": args.job_id or f"local-{os.getpid()}",
        "submitted_at": now_iso(),
        "local": True,
        "terminal": True,
        "exit_code": args.exit_code,
        "host": os.uname().nodename,
    }
    aerr = append_line(run_dir / ATTEMPTS, json.dumps(attempt))
    if aerr:
        sys.exit(f"error: cannot record the attempt: {aerr}")
    print(f"recorded local attempt {attempt['attempt']} "
          f"(exit {args.exit_code}) for {run_dir}")


CONTRACT_TYPES = {
    "command": (str, type(None)),
    "cwd": (str, type(None)),
    "repo": (dict, type(None)),
    "environment": (dict, type(None)),
    # Collections: list or absent. Allowing null here is what let `inputs:
    # null` pass validation and then get iterated.
    "inputs": (list,),
    "declared_outputs": (list,),
    "preexisting_outputs": (dict, type(None)),
    "hash_limit_mb": (int, type(None)),
    "predicates": (list,),
    "declared_write_scopes_unenforced": (list,),
    "preemption_expected": (bool, type(None)),
    "retrospective": (bool, type(None)),
    "criteria_digest": (str, type(None)),
    "contract_id": (str, type(None)),
    "schema_version": (int, type(None)),
    "created_at": (str, type(None)),
    "created_at_epoch": (int, float, type(None)),
}


def contract_problems(contract):
    """Every field's type checked in one place, before anything reads it.

    traincontract.py has had this since round 17; contract.py did not, which is
    exactly why `{"inputs": null}` still crashed check here after the [null]
    case was fixed."""
    if not isinstance(contract, dict):
        return ["contract is not a JSON object"]
    problems = []
    for field, want in CONTRACT_TYPES.items():
        if field not in contract:
            continue
        val = contract[field]
        if want == (int, type(None)) and isinstance(val, bool):
            problems.append(f"'{field}' must be an integer, not a boolean")
            continue
        if not isinstance(val, tuple(want)):
            names = "/".join(w.__name__ for w in want)
            problems.append(f"'{field}' must be {names}, got "
                            f"{type(val).__name__}")
    # Contents only after the container's own type is confirmed -- iterating
    # first is what made the validator crash on the input it rejects.
    if isinstance(contract.get("predicates"), list):
        for i, pred in enumerate(contract["predicates"]):
            if not isinstance(pred, dict):
                problems.append(f"predicate {i} is not an object")
    if isinstance(contract.get("inputs"), list):
        for i, rec in enumerate(contract["inputs"]):
            if not isinstance(rec, dict):
                problems.append(f"input record {i} is not an object")
    return problems


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
        sys.exit(f"error: no contract at {cpath}")
    try:
        raw, rerr = read_text_bounded(cpath)
        if rerr:
            raise ValueError(f"contract unreadable: {rerr}")
        contract = json.loads(raw)
        if not isinstance(contract, dict):
            raise ValueError("contract is not a JSON object")
        shape = contract_problems(contract)
        if shape:
            raise ValueError("; ".join(shape[:4]))
    except (OSError, ValueError, RecursionError) as e:
        # Still emit a receipt: a corrupt contract is a verdict, not a crash.
        verification = {
            "schema_version": SCHEMA_VERSION, "checked_at": now_iso(),
            # No instance to name: the contract could not be parsed. Recorded
            # as null rather than omitted, so a reader can tell "unknown" from
            # "this field predates the change".
            "contract_id": None,
            "criteria_digest": None,
            "state": "CONTRACT_VIOLATED",
            "exit_code": STATES["CONTRACT_VIOLATED"],
            "reasons": [f"contract unreadable or malformed: {e}"],
            "evidence": {}, "predicates": [],
        }
        write_receipt(run_dir / VERIFICATION, verification)
        print(f"CONTRACT_VIOLATED  ({run_dir})")
        print(f"  - contract unreadable or malformed: {e}")
        sys.exit(STATES["CONTRACT_VIOLATED"])

    # Needed by both the scheduler block and the provenance block below.
    declared_at = parse_iso_ts(contract.get("created_at"))
    declared_epoch = contract_epoch(contract)
    all_attempts = _attempts(run_dir)
    attempts = owned_attempts(all_attempts, contract)
    if all_attempts and not attempts:
        reasons_stale = (f"{len(all_attempts)} attempt record(s) belong to a "
                         f"different contract instance and were ignored as "
                         f"stale")
    else:
        reasons_stale = None
    evidence = {"scheduler": None, "job_id": None, "attempts": len(attempts)}
    state = None
    reasons = []
    if reasons_stale:
        reasons.append(reasons_stale)

    # The newest declared artifact, needed BEFORE the scheduler block: a
    # terminal row that ended before the outputs it certifies is not the run
    # that produced them, and section 2's walk happens too late to say so.
    base_dir = contract.get("cwd") or str(run_dir)
    newest_output_mtime = newest_declared_mtime(contract, base_dir)
    ordering_notes = []

    # 1. Scheduler evidence, when a job was submitted through us. Absence of
    #    accounting is missing evidence, never a failure verdict.
    #
    #    ONLY an attempt written by `submit` binds a scheduler job. `record`
    #    declares the outcome of a directly-executed run and accepts a
    #    --job-id for bookkeeping; using that id to query sacct meant
    #    `record --job-id <some clean job> --exit-code 0` made another job's
    #    COMPLETED row certify this contract (kimi, CRITICAL). The two are
    #    different kinds of evidence and must not cross: a local record
    #    carries its own exit code and needs no scheduler at all.
    submitted = [a for a in attempts if not a.get("local")]
    # Only the LATEST attempt decides, whichever kind it is. The scheduler block
    # used to run on submitted[-1] unconditionally and set state first, so a
    # failed submit followed by an honest local re-run and `record --exit-code 0`
    # kept the old FAILED verdict -- contradicting this file's own comment that
    # "a failed first try followed by a successful retry is the normal shape of
    # this work" (deepseek). If the newest attempt is local, its exit code is
    # the outcome and the scheduler has nothing to say about it.
    newest_is_local = bool(attempts and attempts[-1].get("local"))
    if submitted and not newest_is_local:
        binding = submitted[-1]
        job_id = binding["job_id"]
        evidence["job_id"] = job_id
        if squeue_active(job_id, declared_at,
                         parse_iso_ts(binding.get("submitted_at"))):
            state = "RUNNING"
            reasons.append(f"job {job_id} still in the queue")
        else:
            # sacct_state applies ownership to EVERY row and returns the last
            # OWNED one, so there is nothing left to null out here: an
            # unattributable row never becomes state in the first place.
            sstate, scode, ssubmit, why_not = sacct_state(
                job_id, declared_at,
                parse_iso_ts(binding.get("submitted_at")),
                newest_output_mtime, ordering_notes)
            evidence["scheduler"] = {"state": sstate, "exit_code": scode,
                                     "submit": ssubmit}
            if sstate is None and why_not:
                reasons.append(f"sacct row(s) for job {job_id} discarded: "
                               f"{why_not}")
                evidence["scheduler"]["stale"] = True
            if sstate is None:
                reasons.append("no Slurm accounting data (sacct unavailable "
                               "or job too old); relying on artifacts alone")
            elif sstate in SLURM_PREEMPTED:
                state = "PREEMPTED"
                reasons.append(f"job {job_id} state {sstate}")
            elif sstate in SLURM_FAILED:
                state = "FAILED"
                reasons.append(f"job {job_id} state {sstate} exit {scode}")
            elif sstate in SLURM_OK:
                reasons.append(f"job {job_id} COMPLETED (necessary, not sufficient)")
            else:
                state = "RUNNING"
                reasons.append(f"job {job_id} state {sstate}")

    # 2. Contract violations: did inputs drift under a re-run?
    drifted = []
    for rec in (contract.get("inputs") or []):
        if not isinstance(rec, dict):
            drifted.append(f"malformed input record: {rec!r}")
            continue
        if not isinstance(rec.get("path"), str) or not rec["path"]:
            drifted.append(f"input record has no usable path: {rec!r}")
            continue
        if rec.get("rung") == "content-digest":
            p = Path(rec["path"])
            if not p.is_absolute():
                p = Path(base_dir) / p
            if not p.exists():
                drifted.append(f"{rec['path']} disappeared")
            else:
                try:
                    digest, _ = sha256_file(p)
                except OSError as e:
                    drifted.append(f"{rec['path']} became unreadable: {e}")
                else:
                    if digest != rec.get("sha256"):
                        drifted.append(f"{rec['path']} content changed since declaration")
    if drifted:
        # Drift is always reported. A scheduler FAILED verdict still wins,
        # because the real failure reason must not be masked by the drift.
        reasons.extend(drifted)
        if state != "FAILED":
            state = "CONTRACT_VIOLATED"

    # Provenance of declared outputs: an artifact older than the contract
    # cannot have been produced under it.
    stale_outputs = []
    base_dir = contract.get("cwd") or str(run_dir)
    # Every path the verdict depends on: declared outputs AND the targets of
    # predicates, which can name arbitrary paths. Checking only the former let
    # a predicate on a pre-existing file certify a run that produced nothing.
    checked_paths = list(contract.get("declared_outputs") or [])
    command_only = False
    preds = [p for p in (contract.get("predicates") or [])
             if isinstance(p, dict)]
    # Only kinds that actually READ their path count as verifiable evidence.
    # A `command` predicate's `path` is never used by check_predicate, so
    # supplying one was enough to bypass the command-only refusal.
    PATH_KINDS = ("exists", "min_size", "min_lines", "log_matches")
    for pred in preds:
        if pred.get("kind") in PATH_KINDS and isinstance(pred.get("path"), str):
            checked_paths.append(pred["path"])
    if preds and not checked_paths and any(p.get("kind") == "command"
                                           for p in preds):
        # A command predicate reads paths this verifier cannot see, so nothing
        # about artifact provenance can be established from it.
        command_only = True
    unchanged_outputs = []
    for spec in checked_paths:
        if not isinstance(spec, str) or not spec:
            continue
        op = Path(spec)
        if not op.is_absolute():
            op = Path(base_dir) / op
        try:
            if not op.exists():
                continue
            if op.is_dir():
                # Judge the CONTENTS: the directory's own mtime says only that
                # an entry was added or removed.
                fresh, scanned, truncated, dir_newest = directory_freshness(
                    op, declared_at, declared_epoch)
                # A directory output must feed the ordering guard too: it did
                # not, so terminal_record_postdates saw None and a record from
                # before the run certified a directory written after it (kimi).
                if not fresh:
                    if truncated:
                        entry = (f"{spec} (no file from this run found in the "
                                 f"first {scanned} scanned; too large to judge)")
                    elif scanned == 0:
                        entry = f"{spec} (directory holds no regular file)"
                    else:
                        entry = (f"{spec} (all {scanned} file(s) predate the "
                                 f"contract)")
                    if entry not in stale_outputs:
                        stale_outputs.append(entry)
            else:
                limit = contract.get("hash_limit_mb")
                same = unchanged_since_declaration(
                    spec, base_dir, contract,
                    limit if isinstance(limit, int) and limit > 0 else 256)
                if same is True and spec not in unchanged_outputs:
                    # REPORTED, NOT BLOCKING. Content-identity cannot tell a
                    # file that was never regenerated from one a deterministic
                    # pipeline regenerated identically, and for this repo the
                    # second is the SUCCESS case: blocking on it would refuse
                    # exactly the runs most worth certifying. Kimi's finding is
                    # real and this is the honest limit of the signal, so it is
                    # said out loud rather than acted on.
                    unchanged_outputs.append(spec)
                if not artifact_is_fresh(op.stat().st_mtime, declared_at,
                                         declared_epoch):
                    entry = f"{spec} (mtime predates the contract)"
                    if entry not in stale_outputs:
                        stale_outputs.append(entry)
        except OSError:
            pass

    # 3. Execution evidence. Predicates say an artifact exists; they cannot say
    #    a job produced it. Without a recorded attempt, a file that was already
    #    there satisfies every predicate -- so passing requires proof of a run.
    # Only the LATEST attempt decides. A failed first try followed by a
    # successful retry is the normal shape of this work; letting the old
    # failure win made the verdict unrecoverable without hand-editing.
    last_attempt = attempts[-1] if attempts else None
    last_is_local = bool(last_attempt and last_attempt.get("terminal") is True)
    if last_is_local and not terminal_record_postdates(
            parse_iso_ts(last_attempt.get("submitted_at")), newest_output_mtime):
        # Reported, not enforced: see sacct_state. A post-run touch inverts
        # this ordering on an honest run.
        ordering_notes.append("the recorded local run predates the declared "
                              "output(s) it certifies; if this directory was "
                              "reused, that record is not this run's")
    if last_is_local and state is None:
        ec = last_attempt.get("exit_code")
        evidence["local"] = {"exit_code": ec}
        if ec != 0:
            state = "FAILED"
            reasons.append(f"latest recorded local run exited {ec}")
    # The exit code was fetched from sacct and then ignored. Slurm normally
    # reports FAILED for a non-zero exit, but job steps and arrays can leave a
    # COMPLETED row beside a non-zero code, and treating that as success is the
    # exact false pass this tool exists to refuse.
    sched = evidence.get("scheduler") or {}
    sched_ok = sched.get("state") in SLURM_OK and exit_code_is_clean(
        sched.get("exit_code"))
    if sched.get("state") in SLURM_OK and not exit_code_is_clean(
            sched.get("exit_code")):
        reasons.append(f"scheduler state COMPLETED but exit code "
                       f"{sched.get('exit_code')!r} is not clean; not counted "
                       f"as terminal success")
    terminal_confirmed = bool(
        sched_ok or (last_is_local and last_attempt.get("exit_code") == 0))
    # A retrospective contract may document that artifacts match predicates, but
    # it cannot certify that a run produced them: there is no execution
    # evidence at all. traincontract.py refuses the same thing for CONVERGED;
    # letting contract.py pass here made the two tools mean different things by
    # the same flag.
    has_execution_evidence = terminal_confirmed

    # 4. Predicates -- the part the job cannot fake by exiting 0.
    results = []
    if state not in ("RUNNING", "PREEMPTED", "CONTRACT_VIOLATED"):
        for pred in (contract.get("predicates") or []):
            ok, detail = evaluate_predicate(pred, contract.get("cwd") or str(run_dir))
            # Prefix the kind. `--output X` synthesises exists+min_size, so a
            # missing X yielded several byte-identical FAIL lines and the
            # receipt could not say which criterion each one judged.
            kind = pred.get("kind") if isinstance(pred, dict) else None
            if kind and not detail.startswith(f"{kind}:"):
                detail = f"{kind}: {detail}"
            results.append({"ok": ok, "detail": detail, "predicate": pred})
        # A predicate the verifier cannot interpret makes the contract
        # unevaluable; reporting it as a plain unmet criterion would read as
        # "the job did not produce this" when the real cause is the criterion
        # itself. init rejects these, so reaching here means a hand-edited or
        # foreign contract.
        pred_faults = [f for f in (predicate_fault(r["predicate"])
                                   for r in results) if f]
        if pred_faults and state not in ("FAILED",):
            state = "CONTRACT_VIOLATED"
            for f in pred_faults:
                reasons.append(f"uninterpretable predicate: {f}")
        failed = [r for r in results if not r["ok"]]
        if state == "FAILED":
            pass  # scheduler failure already decided it
        elif contract.get("retrospective"):
            # Before the generic no-attempt branch, so the receipt gives the
            # specific reason it has available.
            state = "INCOMPLETE_EVIDENCE"
            reasons.append("retrospective contract: the predicates were "
                           "declared after the run, so passing them documents "
                           "the artifacts but cannot certify the run. Auditing "
                           "a past run is legitimate; certifying it is not. "
                           "(traincontract.py refuses this identically.)")
        elif not has_execution_evidence and not attempts:
            # Nothing was ever submitted: say so, rather than describing the
            # run's exit behaviour, which did not occur.
            state = "INCOMPLETE_EVIDENCE"
            reasons.append("no submitted attempt is recorded; nothing ran "
                           "under this contract")
        elif not (contract.get("predicates") or []):
            state = "INCOMPLETE_EVIDENCE"
            reasons.append("contract declares no predicates")
        elif failed:
            # Exited cleanly but produced nothing verifiable. The whole point.
            state = "TECHNICALLY_COMPLETE"
            reasons.append(f"{len(failed)} of {len(results)} predicates unmet")
        elif command_only:
            state = "INCOMPLETE_EVIDENCE"
            reasons.append(
                "every predicate is a `command`, whose file accesses this "
                "verifier cannot see, so no artifact provenance could be "
                "established; declare at least one --output or path predicate "
                "to show the run produced something")
        elif stale_outputs:
            # Every predicate holds, but on files that predate this contract:
            # a previous run's outputs cannot certify this one.
            state = "INCOMPLETE_EVIDENCE"
            reasons.append(
                f"artifact(s) the verdict depends on predate this contract "
                f"instance and cannot have been produced by it: "
                f"{'; '.join(stale_outputs[:3])}. Re-run the job so it writes "
                f"them, or `init --force` to declare a new contract instance "
                f"before the run that will")
        elif not has_execution_evidence:
            # Every predicate holds, but nothing confirms a job ran to
            # completion. Pre-existing files must never pass as a result.
            state = "INCOMPLETE_EVIDENCE"
            if not attempts:
                reasons.append("all predicates hold, but no submitted attempt "
                               "is recorded -- artifacts may predate this "
                               "contract; use `submit`, or --retrospective")
            else:
                reasons.append("all predicates hold, but no scheduler evidence "
                               "confirms the job reached a terminal state "
                               "(sacct unavailable or job still pending); "
                               "cannot distinguish a result from a stale file")
        else:
            state = "SCIENTIFIC_PASS"
            reasons.append(f"all {len(results)} predicates hold")

    if state is None:
        state = "INCOMPLETE_EVIDENCE"
        reasons.append(
            "no scheduler evidence and no predicates were evaluated, so there "
            "is nothing to judge. Submit through `submit`, or declare the "
            "outcome of a directly-executed run with `record`")

    # Criteria edited after declaration. Timestamps miss this: rewriting
    # contract.json updates its own mtime, not the artifacts'. Absent on
    # contracts written before the field existed, which cannot be checked.
    recorded_digest = contract.get("criteria_digest")
    if state == "SCIENTIFIC_PASS":
        if not isinstance(recorded_digest, str) or not recorded_digest.strip():
            # Tolerating an absent digest made the whole mechanism opt-out:
            # null it while editing created_at_epoch and both the edit check
            # and the freshness anchor were disabled at once (luna). Every
            # contract `init` writes has one, so requiring it costs nothing.
            state = "INCOMPLETE_EVIDENCE"
            reasons.append("this contract carries no criteria_digest, so it "
                           "cannot be shown to be the one that was declared; "
                           "re-declare it with `init`")
        elif recorded_digest != criteria_digest(contract):
            state = "INCOMPLETE_EVIDENCE"
            reasons.append("the predicates were changed after the contract was "
                           "declared, so passing them shows only that the "
                           "criteria were fitted to the result; re-declare "
                           "before the run, or mark it --retrospective")

    for n in ordering_notes:
        reasons.append(f"note: {n}")

    if unchanged_outputs:
        reasons.append(
            f"note: {len(unchanged_outputs)} declared output(s) are "
            f"byte-identical to the file that was already there when the "
            f"contract was declared ({', '.join(unchanged_outputs[:3])}). A "
            f"deterministic re-run looks exactly like this, so it is not "
            f"treated as failure -- but if this pipeline is not deterministic, "
            f"nothing here shows the file was rewritten.")

    verification = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": now_iso(),
        # WHICH contract instance this verdict is about. Without it a receipt
        # left behind by `init --force` is indistinguishable from a current
        # one, so anything reading receipts -- a handoff, a dashboard, a human
        # -- attributes an old pass to a contract that was never verified
        # (kimi, reviewing the plan for a skill that would have done exactly
        # that). The termination record has been bound this way since round 4;
        # the receipt never was.
        "contract_id": contract.get("contract_id"),
        "criteria_digest": contract.get("criteria_digest"),
        "unchanged_outputs": unchanged_outputs,
        "state": state,
        "exit_code": STATES[state],
        "retrospective": contract.get("retrospective", False),
        "reasons": reasons,
        "evidence": evidence,
        "predicates": results,
    }
    werr = write_receipt(run_dir / VERIFICATION, verification)
    if werr:
        # The verdict must still reach the caller even when it cannot be
        # persisted; a FIFO here used to block before anything printed.
        print(f"WARNING: could not write {VERIFICATION}: {werr}",
              file=sys.stderr)

    if args.json:
        print(json.dumps(verification, indent=2))
    else:
        print(f"{state}  ({run_dir})")
        for r in reasons:
            print(f"  - {r}")
        for r in results:
            print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['detail']}")
        if state == "TECHNICALLY_COMPLETE":
            print("\n  The job ended cleanly but did not produce what it "
                  "declared.\n  This is NOT success. Do not treat it as done.")
        if verification["retrospective"]:
            print("\n  NOTE: retrospective contract -- weaker assurance.")

    disarm_watchdog()
    sys.exit(STATES[state])


# --- cli --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="contract.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="declare the contract, before running")
    p.add_argument("run_dir")
    p.add_argument("--command", required=True,
                   help="the command this run will execute (recorded, not run)")
    p.add_argument("--cwd", default=None, help="repo/working dir (default: cwd)")
    p.add_argument("--input", action="append", default=[],
                   help="input path whose identity to record; repeatable")
    p.add_argument("--output", action="append", default=[],
                   help="declared output; implies exists + non-empty predicates")
    p.add_argument("--predicate", action="append", default=[],
                   help='extra predicate as JSON, e.g. \'{"kind":"min_lines",'
                        '"path":"out.tsv","lines":1000}\'; repeatable')
    p.add_argument("--write-scope", action="append", default=[],
                   help="path prefix this run may write to; repeatable")
    p.add_argument("--hash-limit-mb", type=int, default=256,
                   help="digest files up to this size fully (default 256)")
    p.add_argument("--preemptible", action="store_true",
                   help="requeue/preemption is expected, not a failure")
    p.add_argument("--retrospective", action="store_true",
                   help="criteria declared after the run; weaker assurance")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("submit", help="sbatch run_dir/job.sbatch and record it")
    p.add_argument("run_dir")
    p.add_argument("--sbatch-arg", action="append", default=[],
                   help="extra arg passed through to sbatch; repeatable")
    p.set_defaults(fn=cmd_submit)

    p = sub.add_parser("record",
                       help="record the terminal outcome of a directly-executed run")
    p.add_argument("run_dir")
    p.add_argument("--exit-code", type=int, required=True)
    p.add_argument("--job-id", default=None,
                   help="recorded for bookkeeping only; a local record is "
                        "never used to query the scheduler, because a "
                        "caller-supplied id would let another job's accounting "
                        "certify this contract")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("check", help="verify the contract and emit a receipt")
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
