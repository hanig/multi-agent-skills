#!/usr/bin/env python3
"""unit.py: the unit contract for an autonomous agent swarm.

Step 1 of docs/plan-swarm.md, planned by a two-member committee
(claude-opus-4-8 + gpt-5.6-sol, fresh context, in parallel, both converged).

THE IDEA, and it is the opposite of what this repo previously built:

    Isolation replaces attribution.

Three earlier plans died trying to prove "this agent produced this artifact".
Two reviewers independently showed that is unreachable by observation: a window
shows an artifact CHANGED, never which process changed it, and on a filesystem
shared by ~18 people a concurrent writer is ordinary. No window length fixes it.

Shreshth never had the problem. Paseo gives each agent its own git worktree --
"one bounded, disjoint worker task and worktree per implementation worker" --
so his cheap `--base` predicate is CONCLUSIVE because nothing else writes that
tree. He did not solve attribution; an exclusive namespace made it unnecessary.

So: the coordinator allocates an exclusive, never-reused write root per attempt.
The worker writes only there. Shared datasets and environments are immutable
PINNED READ-ONLY inputs, not isolated. Slurm cgroups already isolate GPU and
memory. Once the write root is exclusive:

    output exists in the run-dir  +  terminal-OK OWNED sacct row
      ==  this unit produced it

Attribution by construction.

EXACTLY ONE PROPERTY DOES THE ISOLATING: the exclusive write root. The pinned
commit, immutable spec and versioned environment are IMMUTABILITY OF INPUTS --
they matter for reproducibility and ownership, never for making the done
predicate conclusive. Keep that distinction, because it is the drift guard: when
someone proposes an expensive new check, ask "does write-root isolation already
make this conclusive?" If yes, delete the check.

DRIFT GUARD, from the committee verbatim: "the center of gravity is the
coordinator and the human interface, not the predicate. If the surviving module
grows past ~300 lines or reacquires any 'the command wrote this' check, stop."
tests/test_unit.py enforces both halves.

THE UNIT IS POLYMORPHIC. Hani chose three kinds; only the done predicate and the
isolation boundary differ.

  slurm     terminal-OK owned sacct row AND a declared output in the run-dir.
            The clean case.
  pipeline  the engine (Nextflow/Snakemake) owns its interior DAG, retries and
            work directory. Isolation is possible ONLY at the boundary: a fresh
            work dir and a fresh publish dir per unit. Per-task interior success
            is UNJUDGEABLE and this module does not pretend otherwise -- the
            receipt says "interior not judged; engine's self-report trusted at
            the boundary". Do not reimplement the engine's scheduler.
  code      delegate to `bus await` (Shreshth's artifact contract over a git
            worktree). Taken UNCHANGED; nothing here redesigns it.

The Slurm knowledge below is lifted VERBATIM from contract.py, which earned it
against a real scheduler: sacct row ownership under job-id reuse, `0:0` on a
CANCELLED job not counting as success, `End` arriving as the literal "Unknown",
states containing spaces (`CANCELLED by 10025`), and a timezone bug that made
one instant read as three epochs nine hours apart.

Python 3.8+, stdlib only, login-node safe. No network.
"""

import argparse
import calendar
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

STATES = {"DONE": 0, "RUNNING": 1, "FAILED": 2, "PREEMPTED": 3,
          "INCOMPLETE": 4}
USAGE_ERROR = 64

UNIT = "unit.json"
EVENTS = "events.jsonl"
RECEIPT = "receipt.json"
KINDS = ("slurm", "pipeline", "code")

# Constants the LIFTED code needs. Both were missing, and neither
# py_compile nor 22 local tests could see it: off-cluster there is no `sacct`,
# so sacct_state returns before it ever reaches sacct_row_is_ours. One real job
# on lambda found it immediately. Third variant of one defect class today --
# missing callee, missing import, now missing constant.
OWNERSHIP_SLACK_S = 1              # seconds of clock slack when binding a row
# Written by the launcher wrapper, never by the engine itself.
ENGINE_RC = "engine.rc"
MAX_DIR_ENTRIES_SCANNED = 20_000   # bound on a directory freshness walk

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


# Lifted callee: sha256_file. Found by a closure check that enumerates by
# EXCLUSION (what does this module load that is defined nowhere) rather than
# against a hand-written list. Three inclusion-based checks missed it.
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


# ==========================================================================
# The unit contract. Everything above this line is lifted verbatim from
# contract.py and must not be edited here; everything below is new.
# ==========================================================================

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def event(unit_dir, name, **fields):
    """Append-only history. A unit's story is its events, not its final state:
    a coordinator that crashes mid-dispatch must be able to resume from this."""
    rec = {"at": now_iso(), "event": name}
    rec.update(fields)
    try:
        with (Path(unit_dir) / EVENTS).open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as e:
        print(f"WARNING: could not append event: {e}", file=sys.stderr)


def read_json(path):
    try:
        p = Path(path)
        if not p.is_file():
            return None, "missing"
        return json.loads(p.read_text()), None
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def write_json(path, obj):
    """Atomic. A half-written unit spec read by a resuming coordinator is worse
    than none."""
    try:
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
        tmp.replace(p)
        return None
    except (OSError, ValueError) as e:
        return f"{type(e).__name__}: {e}"


def cmd_allocate(args):
    """Allocate an EXCLUSIVE, NEVER-REUSED write root for one attempt.

    This is the whole isolation mechanism. `mkdir` with exist_ok=False is the
    enforcement: two units cannot be handed the same root, because the second
    allocation fails rather than sharing. Nothing later has to attribute a write,
    because nothing else may write here."""
    root = Path(args.root).resolve()
    if args.kind not in KINDS:
        sys.exit(f"error: kind must be one of {', '.join(KINDS)}, got "
                 f"{args.kind!r}")
    if not args.output:
        sys.exit("error: a unit with no declared outputs cannot be judged done. "
                 "Pass --output PATH (relative to the run-dir) at least once.")

    attempt_id = os.urandom(8).hex()
    unit_dir = root / args.task / attempt_id
    try:
        # exist_ok=False is the exclusivity. Never relax this.
        unit_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        sys.exit(f"error: {unit_dir} already exists, so it is not an exclusive "
                 f"write root. Allocate a new attempt rather than reusing one.")
    except OSError as e:
        sys.exit(f"error: cannot create {unit_dir}: {e}. Check the path is "
                 f"writable, then re-run `allocate`.")

    spec = {
        "schema_version": 1,
        "task_id": args.task,
        "attempt_id": attempt_id,
        "kind": args.kind,
        "created_at": now_iso(),
        "created_at_epoch": time.time(),
        "command": args.command,
        # Relative to the run-dir on purpose: an absolute path outside it would
        # be a write the coordinator did not isolate.
        "declared_outputs": list(args.output),
        # Immutability of INPUTS, not isolation. They make the result
        # reproducible and owned; they are not what makes `check` conclusive.
        "pinned_inputs": {i: _digest(i) for i in (args.input or [])},
        "env_identity": {k: os.environ.get(k) for k in sorted(args.record_env or [])},
        "budget": {"gpu_hours": args.gpu_hours, "charged_to": args.charge_to},
        "job_id": None,
        "bound_at": None,
    }
    err = write_json(unit_dir / UNIT, spec)
    if err:
        sys.exit(f"error: cannot write the unit spec: {err}")
    event(unit_dir, "allocated", attempt_id=attempt_id, kind=args.kind)
    print(unit_dir)                       # stdout is the path, for scripting
    print(f"  attempt {attempt_id}  kind {args.kind}  "
          f"outputs {len(args.output)}", file=sys.stderr)
    return 0


def _digest(path):
    """Content identity of a pinned input, or a stated reason it has none."""
    p = Path(path)
    try:
        if not p.is_file():
            return {"sha256": None, "error": "not a regular file"}
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return {"sha256": h.hexdigest(), "size": p.stat().st_size}
    except OSError as e:
        return {"sha256": None, "error": f"{type(e).__name__}: {e}"}


def cmd_bind(args):
    """Record which scheduler job belongs to this attempt.

    Separate from `allocate` because the job id does not exist until submission,
    and separate from `check` because a verifier that discovers its own evidence
    is not a verifier."""
    unit_dir = Path(args.unit_dir).resolve()
    spec, err = read_json(unit_dir / UNIT)
    if err:
        sys.exit(f"error: no readable unit spec at {unit_dir / UNIT}: {err}. "
                 f"Run `allocate` first.")
    if spec.get("job_id") is not None:
        sys.exit(f"error: this attempt is already bound to job "
                 f"{spec['job_id']}. Allocate a new attempt rather than "
                 f"rebinding one.")
    if not re.fullmatch(r"\d+(_\d+)?", str(args.job_id)):
        sys.exit(f"error: implausible job id {args.job_id!r}. Pass the numeric "
                 f"id the scheduler printed.")
    spec["job_id"] = str(args.job_id)
    spec["bound_at"] = now_iso()
    err = write_json(unit_dir / UNIT, spec)
    if err:
        sys.exit(f"error: cannot record the binding: {err}")
    event(unit_dir, "bound", job_id=spec["job_id"])
    print(f"bound job {spec['job_id']} to attempt {spec['attempt_id']}")
    return 0


def _outputs_present(unit_dir, spec):
    """Which declared outputs exist INSIDE the exclusive write root.

    Paths are resolved under the run-dir and an escape is refused rather than
    followed: a declared output that resolves outside the root is not isolated,
    so nothing about it can be concluded."""
    present, missing, escaped = [], [], []
    root = Path(unit_dir).resolve()
    for rel in spec.get("declared_outputs") or []:
        p = (root / rel).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            escaped.append(rel)
            continue
        (present if p.exists() else missing).append(rel)
    return present, missing, escaped


def _proc_alive(pid, launched_at):
    """Is the process we launched STILL the process at that pid?

    Pids are reused, so a bare kill(pid, 0) can report a stranger's process as
    our engine. On Linux, field 22 of /proc/<pid>/stat is the process start
    time in clock ticks since boot; a process that started before we launched
    ours cannot be ours. Returns True, False, or None when it cannot be told."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError, OverflowError):
        return None
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            fields = fh.read().rsplit(") ", 1)[-1].split()
        ticks = float(fields[19])                       # field 22, 1-indexed
        hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
        started = time.time() - uptime + (ticks / hz)
        if launched_at and started < float(launched_at) - 60:
            return False                                # a different process
    except (OSError, ValueError, IndexError, ZeroDivisionError):
        return True                                     # alive; identity unsure
    return True


def _pipeline_state(unit_dir, spec, present, missing, notes):
    """The done predicate for an engine launched OUTSIDE a scheduler.

    Weaker than the Slurm predicate, and it must say so. There is no third
    party here: Slurm's accounting database is written by Slurm, but a login
    node offers nothing equivalent, so the exit status is recorded by our own
    launcher wrapper. That is stronger than the engine asserting its own
    success and weaker than `sacct`, and the receipt records which.

    Written after the pipeline path ran for the first time and sat INCOMPLETE
    with its output on disk, because this function demanded a scheduler
    binding that a pipeline unit never has."""
    rec, err = read_json(Path(unit_dir) / "engine.json")
    if err or not isinstance(rec, dict):
        notes.append("no engine.json in this attempt, so nothing shows an "
                     "engine was ever launched here. Dispatch it with "
                     "`swarm.py advance`.")
        return "INCOMPLETE"

    try:
        rc_text = (Path(unit_dir) / ENGINE_RC).read_bytes()[:64].decode(
            "utf-8", "replace")
        rerr = None
    except OSError as e:
        rc_text, rerr = None, e
    pid, host = rec.get("pid"), rec.get("host")
    here = os.uname().nodename

    if rerr:
        # No exit status recorded yet: either still going, or it died in a way
        # that skipped the wrapper.
        if host and host != here:
            notes.append(f"the engine was launched on {host} and this check is "
                         f"running on {here}, so its liveness cannot be read "
                         f"from here. Check from {host}, or wait for "
                         f"{ENGINE_RC} to appear.")
            return "INCOMPLETE"
        alive = _proc_alive(pid, rec.get("launched_at"))
        if alive is not False:
            notes.append(f"engine pid {pid} is still running on {here}.")
            return "RUNNING"
        notes.append(f"engine pid {pid} is gone and no {ENGINE_RC} was "
                     f"written, so its exit status was never recorded. It was "
                     f"killed, or the node rebooted. Re-dispatch this unit; "
                     f"see engine.log in the attempt directory.")
        return "FAILED"

    try:
        code = int((rc_text or "").strip())
    except (TypeError, ValueError):
        notes.append(f"{ENGINE_RC} holds {(rc_text or '').strip()[:40]!r}, "
                     f"which is not an exit code, so the attempt cannot be "
                     f"judged. Re-dispatch it.")
        return "INCOMPLETE"

    if code != 0:
        notes.append(f"the engine exited {code}. See engine.log in the attempt "
                     f"directory.")
        return "FAILED"
    if missing:
        # The failure sacct structurally cannot see either, and the reason a
        # clean exit is never sufficient on its own.
        notes.append(f"the engine exited 0 but {len(missing)} declared "
                     f"output(s) are absent: {', '.join(sorted(missing))}.")
        return "INCOMPLETE"
    notes.append(f"the engine exited 0 and all {len(present)} declared "
                 f"output(s) are present in the exclusive write root. Exit "
                 f"status is self-recorded by the launcher wrapper, NOT "
                 f"attested by a scheduler.")
    return "DONE"


def check_unit(unit_dir, spec, notes):
    """The done predicate. Returns a state name.

    Conclusive ONLY because the write root is exclusive. There is deliberately
    no check anywhere in this function for "did the command write this file":
    that question is unanswerable by observation and does not need answering
    when nothing else may write here."""
    kind = spec.get("kind")
    present, missing, escaped = _outputs_present(unit_dir, spec)
    if escaped:
        notes.append(f"declared output(s) resolve outside the exclusive write "
                     f"root and are therefore not isolated: "
                     f"{', '.join(escaped)}. Declare paths relative to the "
                     f"run-dir.")
        return "INCOMPLETE"

    if kind == "code":
        # Shreshth's artifact contract over a git worktree, taken unchanged.
        # Reimplementing it here would be the mistake this plan exists to undo.
        notes.append("kind=code: the done predicate is `bus await` over the "
                     "agent's worktree (--base HEAD advanced, --require-clean). "
                     "Run that; this module does not duplicate it.")
        return "INCOMPLETE"

    if kind == "pipeline":
        return _pipeline_state(unit_dir, spec, present, missing, notes)

    job_id = spec.get("job_id")
    if not job_id:
        notes.append("no scheduler job is bound to this attempt, so nothing "
                     "shows it ran. Submit it and record the id with `bind`.")
        return "INCOMPLETE"

    declared_at = parse_iso_ts(spec.get("created_at"))
    bound_at = parse_iso_ts(spec.get("bound_at"))
    newest = newest_declared_mtime({"declared_outputs":
                                    [str(Path(unit_dir) / o)
                                     for o in (spec.get("declared_outputs") or [])],
                                    "cwd": str(unit_dir)}, str(unit_dir))
    why = []
    sstate, scode, ssubmit, why_not = sacct_state(
        str(job_id), declared_at, bound_at, newest, why)
    notes.extend(why)

    if sstate is None:
        if why_not:
            notes.append(f"sacct row(s) for job {job_id} discarded: {why_not}")
        notes.append(f"no usable accounting row for job {job_id}, so its "
                     f"terminal state is unknown. Wait, or check `sacct -j "
                     f"{job_id}` by hand.")
        return "INCOMPLETE"

    if sstate in SLURM_PREEMPTED:
        notes.append(f"job {job_id} state {sstate}: preempted, not failed. "
                     f"Re-attempt it; a new attempt gets a new write root.")
        return "PREEMPTED"

    if sstate in SLURM_FAILED:
        # `0:0` on a CANCELLED job is why this is a state check and not an
        # exit-code check. Earned against a real scheduler.
        notes.append(f"job {job_id} state {sstate} exit {scode}")
        return "FAILED"

    if sstate not in SLURM_OK:
        notes.append(f"job {job_id} state {sstate}: still in the queue")
        return "RUNNING"

    if not exit_code_is_clean(scode):
        notes.append(f"job {job_id} reached {sstate} but its exit code {scode!r} "
                     f"is not clean, so it is not counted as success")
        return "FAILED"

    if kind == "pipeline":
        # THE HONEST LIMIT. The engine owns its interior DAG, retries and work
        # directory, so per-task interior success is UNJUDGEABLE and no amount
        # of looking inside changes that. Judge the boundary and SAY SO.
        notes.append("kind=pipeline: interior not judged; the engine's own "
                     "terminal exit is trusted at the boundary, and only the "
                     "declared FINAL outputs in the exclusive publish dir are "
                     "checked. Per-task interior success and which internal "
                     "step produced which intermediate are not established.")

    if missing:
        notes.append(f"job {job_id} finished cleanly but {len(missing)} declared "
                     f"output(s) are absent from the exclusive write root: "
                     f"{', '.join(missing[:3])}. The job ended; it did not "
                     f"produce what the unit declared.")
        return "INCOMPLETE"

    notes.append(f"job {job_id} {sstate} exit {scode}, and all "
                 f"{len(present)} declared output(s) are present in the "
                 f"exclusive write root")
    return "DONE"


def cmd_check(args):
    unit_dir = Path(args.unit_dir).resolve()
    spec, err = read_json(unit_dir / UNIT)
    if err:
        sys.exit(f"error: no readable unit spec at {unit_dir / UNIT}: {err}. "
                 f"Run `allocate` first.")
    notes = []
    state = check_unit(unit_dir, spec, notes)

    receipt = {
        "schema_version": 1, "checked_at": now_iso(),
        "task_id": spec.get("task_id"), "attempt_id": spec.get("attempt_id"),
        "kind": spec.get("kind"), "job_id": spec.get("job_id"),
        "state": state, "exit_code": STATES[state], "notes": notes,
        # What this receipt does and does not establish, machine-readable, so a
        # consumer sees the boundary without parsing prose.
        # What this receipt establishes, and its BOUNDARY. Corrected after an
        # audit: the run directory is unique but it is NOT an enforced
        # boundary. A command can write an absolute path outside it, another
        # process under the same Unix user can write into it, `write_scopes`
        # state intent rather than constrain writes, and neither the plan nor
        # this spec is frozen. Claiming OS-enforced isolation here would be the
        # third time this project claimed more than its mechanism establishes.
        "basis": {
            "conclusive_because": "exclusive by coordinator allocation under a "
                                  "trusted-writer convention",
            "os_enforced_isolation": False,
            "attribution_by_observation": False,
            "interior_judged": spec.get("kind") != "pipeline",
            # A pipeline unit has no scheduler behind it, so its exit status
            # comes from our own launcher wrapper rather than from Slurm's
            # accounting database. Weaker evidence, named as such.
            "exit_status_attested_by": (
                "launcher wrapper (no scheduler)"
                if spec.get("kind") == "pipeline" else "slurm accounting"),
            "note": "not isolated from other processes running as the same "
                    "Unix user. OS-enforced isolation would need a container "
                    "or mount namespace with this directory as the only "
                    "writable bind mount.",
        },
    }
    werr = write_json(unit_dir / RECEIPT, receipt)
    if werr:
        print(f"WARNING: could not write {RECEIPT}: {werr}", file=sys.stderr)
    event(unit_dir, "checked", state=state)

    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"{state}  ({unit_dir})")
        for n in notes:
            print(f"  - {n}")
    return STATES[state]


def main():
    ap = argparse.ArgumentParser(
        prog="unit.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("allocate", help="create an exclusive write root")
    a.add_argument("--root", required=True, help="e.g. .swarm/runs")
    a.add_argument("--task", required=True)
    a.add_argument("--kind", required=True, choices=KINDS)
    a.add_argument("--command", default=None)
    a.add_argument("--output", action="append", default=[],
                   help="declared output, RELATIVE to the run-dir; repeatable")
    a.add_argument("--input", action="append", default=[],
                   help="pinned read-only input; digested at allocation")
    a.add_argument("--record-env", action="append", default=[])
    a.add_argument("--gpu-hours", type=float, default=None)
    a.add_argument("--charge-to", default=None)
    a.set_defaults(fn=cmd_allocate)

    b = sub.add_parser("bind", help="record the scheduler job id")
    b.add_argument("unit_dir")
    b.add_argument("--job-id", required=True)
    b.set_defaults(fn=cmd_bind)

    c = sub.add_parser("check", help="the done predicate")
    c.add_argument("unit_dir")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
