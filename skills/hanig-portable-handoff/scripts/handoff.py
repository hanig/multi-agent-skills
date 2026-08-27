#!/usr/bin/env python3
"""handoff.py — capture and restore durable research state across machines.

Three commands:

    handoff.py capture RUN_DIR... --out handoff.json
    handoff.py resume  handoff.json [--base DIR]
    handoff.py memory  PROJECT_DIR

Design and its thirteen acceptance criteria: docs/plan-portable-handoff.md.
Three plan reviews preceded any of this code; the findings that shaped it are
named at the rules they produced.

Exit codes for `resume` (never a boolean, same reason the verifiers are not):

    0  HANDOFF_CLEAN       code and inputs match, pointers resolve here
    1  HANDOFF_DRIFTED     code or input identity differs, or a size differs
    2  HANDOFF_ELSEWHERE   code matches; a pointer is not reachable from here
    3  HANDOFF_MALFORMED   the handoff file itself is unusable

Checked in that order, first match wins, so they are exclusive by construction
rather than by hoping the conditions do not overlap (deepseek, plan review 3).

Python 3.7+, standard library only: it runs on an HPC login node with no pip.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

STATES = {"HANDOFF_CLEAN": 0, "HANDOFF_DRIFTED": 1,
          "HANDOFF_ELSEWHERE": 2, "HANDOFF_MALFORMED": 3}

SCHEMA_VERSION = 1

# Receipt and state filenames written by the sibling verifiers. Read-only here.
WORKFLOW_FILES = ("contract.json", "verification.json", "attempts.jsonl")
TRAINING_FILES = ("training-contract.json", "training-verification.json",
                  "training-termination.json", "training-binding.json")

# The ONLY files capture may open. Criterion 1, stated as an allowlist because
# v1 said "never reads a file's contents" while another criterion required
# reading a receipt -- the two could not both hold (deepseek, plan review 1).
CAPTURE_READS = frozenset(WORKFLOW_FILES + TRAINING_FILES)

# Environment names whose VALUES must never appear in a handoff or a log.
CREDENTIAL_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|BEARER|PRIVATE|"
    r"SESSION|COOKIE|SIGNATURE)", re.I)

# Named as in contract.py, because read_text_bounded is copied verbatim and a
# verbatim copy needs its constants too: copying the function alone left a
# NameError that py_compile cannot see and only a runtime call reveals.
MAX_PREDICATE_READ_BYTES = 256 * 1024 * 1024
MAX_READ_BYTES = MAX_PREDICATE_READ_BYTES
MAX_DIR_ENTRIES = 20_000        # same bound as contract.py's directory walk


USAGE_ERROR = 64      # not 1: that is HANDOFF_DRIFTED, and a bad command
                      # line is not a verdict about anything.


def usage_error(msg):
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(USAGE_ERROR)


# --- helpers, byte-identical to the sibling verifiers -----------------------
# Copied rather than imported: `install.sh --only NAME` installs one skill, so
# this file must run with no sibling present. tests/test_symmetry.py asserts
# these copies are identical across all three scripts, because duplication is
# safe only when something mechanical enforces it -- sixteen defects here came
# from a rule living in one copy and not the other.

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


def git(cwd, *args):
    rc, out, _ = run(["git", "-C", str(cwd), *args])
    return out if rc == 0 else ""


# --- redaction --------------------------------------------------------------

# https://user:pass@host/repo is a perfectly ordinary git remote and a
# credential in plain sight. It was written verbatim into the handoff and
# printed by resume (deepseek, CRITICAL).
CREDENTIAL_VALUE = re.compile(
    r"(bearer\s+[\w.\-]{12,}"
    r"|(?:aws)?(?:secret|access)[_-]?key\s*[:=]\s*\S{8,}"
    r"|(?:api[_-]?)?token\s*[:=]\s*\S{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|sk-[A-Za-z0-9_\-]{16,})", re.I)


def scrub_value(value):
    """Redact a credential-shaped VALUE wherever it appears, whatever its key."""
    if not isinstance(value, str):
        return value
    value = scrub_url(value)
    if CREDENTIAL_VALUE.search(value):
        return "[redacted: credential-shaped value]"
    return value


def redact_env(mapping):
    """Replace credential-shaped VALUES, keep the names.

    Names are useful -- knowing a run had AWS_SECRET_ACCESS_KEY set is real
    information -- and values never are. Returns a new dict; the input is not
    mutated, because the digest is computed over the original.
    """
    if not isinstance(mapping, dict):
        return mapping
    out = {}
    for k, v in mapping.items():
        if isinstance(k, str) and CREDENTIAL_NAME.search(k):
            out[k] = "[redacted]"
        elif isinstance(v, dict):
            out[k] = redact_env(v)
        elif isinstance(v, list):
            out[k] = [scrub_value(x) if isinstance(x, str) else x for x in v]
        else:
            out[k] = scrub_value(v)
    return out


def contract_digest(contract):
    """Identity of a contract as captured, computed BEFORE redaction.

    Comparison and display are separated on purpose. Carrying values as-is
    leaks a credential; redacting them makes the recorded contract differ from
    the real one, so an honest resume reports drift. Three plan revisions
    carried that contradiction (kimi). So: compare this digest, show the
    redacted copy.
    """
    try:
        payload = json.dumps(contract, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode()).hexdigest()


# --- pointers ---------------------------------------------------------------

def describe_pointer(spec, base_dir):
    """What a declared artifact looks like from here. Never opens the file.

    Paths are resolved ABSOLUTE at capture time so resume needs no cwd: a
    relative pointer made an artifact that exists look like it was on another
    host, reporting ELSEWHERE and telling the user to change machines (kimi).
    """
    rec = {"declared": spec}
    try:
        p = Path(spec)
        if not p.is_absolute():
            p = Path(base_dir) / p
        p = p.resolve()
        rec["path"] = str(p)
        st = os.stat(str(p))
        rec["exists"] = True
        rec["is_dir"] = stat.S_ISDIR(st.st_mode)
        if rec["is_dir"]:
            n, newest, total = 0, None, 0
            for root, _dirs, files in os.walk(str(p)):
                for name in files:
                    n += 1
                    if n > MAX_DIR_ENTRIES:
                        rec["scan_truncated"] = True
                        break
                    try:
                        s2 = os.stat(os.path.join(root, name))
                    except OSError:
                        continue
                    total += s2.st_size
                    newest = (s2.st_mtime if newest is None
                              else max(newest, s2.st_mtime))
                if rec.get("scan_truncated"):
                    break
            rec["file_count"] = n
            rec["size"] = total
            rec["mtime"] = newest
        else:
            rec["size"] = st.st_size
            rec["mtime"] = st.st_mtime
        rec["filesystem"] = filesystem_of(p)
    except OSError as e:
        rec["exists"] = False
        rec["reason"] = str(e)          # recorded WITH its reason, never dropped
    return rec


def filesystem_of(path):
    """Which filesystem a path lives on, so a resume can say 'that is lambda's
    scratch and you are on andromeda'. None when df cannot say."""
    rc, out, _ = run(["df", "-P", str(path)], timeout=10)
    if rc != 0:
        return None
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    return lines[-1].split()[0]


def host_identity():
    """Host identity is NOT an environment dump. v2 conflated the two, so a
    test for one criterion would have rejected what another requires (kimi)."""
    u = os.uname()
    return {
        "hostname": u.nodename,
        "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
        "python": sys.version.split()[0],
        "system": f"{u.sysname} {u.release}",
    }


# --- capture ----------------------------------------------------------------

def read_state_file(run_dir, name):
    """One of the allowlisted files, parsed, or None. Criterion 1."""
    if name not in CAPTURE_READS:
        raise AssertionError(f"{name} is not in CAPTURE_READS")
    raw, err = read_text_bounded(Path(run_dir) / name)
    if err or raw is None:
        return None
    try:
        val = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return val if isinstance(val, dict) else None


def attributed_verdict(run_dir, contract, receipt_name):
    """The receipt's verdict, but ONLY when the receipt names this contract
    instance.

    A receipt left behind by `init --force` is indistinguishable from a current
    one unless it says which instance it is about. It did not, in either
    verifier, until reviewing THIS plan surfaced it -- so `capture` would have
    recorded a pass for a contract never verified (kimi, plan review 1).
    """
    rec = read_state_file(run_dir, receipt_name)
    if rec is None:
        return {"verdict": None, "reason": "no verification receipt"}
    want = (contract or {}).get("contract_id")
    got = rec.get("contract_id")
    if not want:
        # No instance to match against. `if want:` alone accepted the receipt,
        # which is the opposite of what criterion 2 says (kimi).
        return {"verdict": None, "state": rec.get("state"),
                "reason": "the contract names no instance, so no receipt can "
                          "be attributed to it; re-declare it with `init`"}
    if want:
        if got is None:
            return {"verdict": None, "state": rec.get("state"),
                    "reason": "the receipt names no contract instance, so it "
                              "cannot be attributed to this one; re-run "
                              "`check` to produce a receipt that does"}
        if got != want:
            return {"verdict": None, "state": rec.get("state"),
                    "reason": "the receipt belongs to a different contract "
                              "instance, probably left behind by "
                              "`init --force`; re-run `check`"}
    return {"verdict": rec.get("state"), "exit_code": rec.get("exit_code"),
            "checked_at": rec.get("checked_at"), "reason": None}


def capture_run_dir(run_dir):
    """Everything durable about one run directory. Reads only allowlisted
    files, stats pointers, never opens an artifact."""
    d = Path(run_dir).resolve()
    out = {"run_dir": str(d), "kind": None, "pointers": [], "job_ids": []}

    wf = read_state_file(d, "contract.json")
    tr = read_state_file(d, "training-contract.json")
    if wf is None and tr is None:
        out["reason"] = ("no contract.json or training-contract.json here, so "
                         "there is nothing to capture. Point at a run "
                         "directory created by `contract.py init` or "
                         "`traincontract.py init`")
        return out

    contract = wf if wf is not None else tr
    out["kind"] = "workflow" if wf is not None else "training"
    out["contract_digest"] = contract_digest(contract)
    out["contract"] = redact_env(contract)
    base = contract.get("cwd") or str(d)

    if out["kind"] == "workflow":
        out["verdict"] = attributed_verdict(d, contract, "verification.json")
        specs = list(contract.get("declared_outputs") or [])
        for pred in (contract.get("predicates") or []):
            if isinstance(pred, dict) and isinstance(pred.get("path"), str):
                specs.append(pred["path"])
        # job ids come from the attempts log, which is JSONL not JSON
        raw, err = read_text_bounded(d / "attempts.jsonl")
        if not err and raw:
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                if isinstance(a, dict) and a.get("job_id") and not a.get("local"):
                    out["job_ids"].append(str(a["job_id"]))
    else:
        out["verdict"] = attributed_verdict(d, contract,
                                            "training-verification.json")
        specs = [contract.get("metrics_file"), contract.get("checkpoint_dir")]
        binding = read_state_file(d, "training-binding.json")
        if binding and binding.get("job_id"):
            out["job_ids"].append(str(binding["job_id"]))
        elif (contract.get("run") or {}).get("slurm_job_id"):
            out["job_ids"].append(str(contract["run"]["slurm_job_id"]))

    for spec in specs:
        if isinstance(spec, str) and spec:
            out["pointers"].append(describe_pointer(spec, base))
    return out


def cmd_capture(args):
    run_dirs = [Path(d) for d in args.run_dir]
    missing = [str(d) for d in run_dirs if not d.is_dir()]
    if missing:
        usage_error(f"error: not a directory: {', '.join(missing)}. Pass run "
                 f"directories created by `init`")

    captured = [capture_run_dir(d) for d in run_dirs]
    # Unresolved first: a non-zero verdict, or a verdict that could not be
    # attributed, is what a returning reader needs before anything else.
    def unresolved(c):
        v = c.get("verdict") or {}
        return (c.get("reason") is not None
                or v.get("reason") is not None
                or (v.get("exit_code") not in (None, 0)))
    captured.sort(key=lambda c: (not unresolved(c), c["run_dir"]))

    handoff = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": now_iso(),
        "host": host_identity(),
        "code": repo_state(str(Path(args.cwd or os.getcwd()).resolve())),
        "runs": captured,
        "unresolved": [c["run_dir"] for c in captured if unresolved(c)],
    }
    err = write_receipt(Path(args.out), handoff)
    if err:
        usage_error(f"error: cannot write {args.out}: {err}")
    print(f"wrote {args.out}")
    print(f"  {len(captured)} run dir(s), {len(handoff['unresolved'])} unresolved")
    for c in captured:
        v = (c.get("verdict") or {})
        label = v.get("verdict") or v.get("reason") or c.get("reason") or "?"
        print(f"  {c['run_dir']}: {label}")
    if handoff["unresolved"]:
        print("  unresolved are listed first in the file")


# --- resume -----------------------------------------------------------------

def rebase_path(recorded, base):
    """Re-root a recorded absolute path under --base, for a tree that moved.

    The RECORDED path wins when it still exists: --base is for the parts that
    moved, and re-rooting everything broke a run directory that had not, so an
    honest resume reported ELSEWHERE about a contract sitting where it always
    was.
    """
    if not base:
        return recorded
    if Path(recorded).exists():
        return recorded
    p = Path(recorded)
    parts = [x for x in p.parts if x not in ("/", "")]
    for i in range(len(parts)):
        cand = Path(base).joinpath(*parts[i:])
        if cand.exists():
            return str(cand)
    return str(Path(base) / p.name)


def compare_code(recorded, current):
    """Every code difference, enumerated. Never a count."""
    diffs = []
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return ["the handoff records no usable code identity"]
    if recorded.get("git") and not current.get("git"):
        diffs.append("the handoff was captured in a git work tree and this "
                     "directory is not one")
        return diffs
    for key, label in (("remote", "git remote"), ("commit", "commit"),
                       ("branch", "branch"), ("diff_sha256", "uncommitted diff")):
        a, b = recorded.get(key), current.get(key)
        if a != b:
            diffs.append(f"{label}: handoff has {a!r}, here it is {b!r}")
    return diffs


def cmd_resume(args):
    raw, err = read_text_bounded(Path(args.handoff))
    if err or raw is None:
        bail_resume("MALFORMED", [f"cannot read {args.handoff}: {err}. "
                                 f"Re-capture on the machine that wrote it"])
    try:
        h = json.loads(raw)
    except (ValueError, RecursionError) as e:
        bail_resume("MALFORMED", [f"{args.handoff} is not valid JSON: {e}. "
                                 f"Re-capture it"])
    if not isinstance(h, dict) or not isinstance(h.get("runs"), list):
        bail_resume("MALFORMED", [f"{args.handoff} has no runs list. Re-capture it"])

    reports, drift, elsewhere, malformed = [], [], [], []

    code_diffs = compare_code(h.get("code"),
                              repo_state(str(Path(args.cwd or os.getcwd()).resolve())))
    drift.extend(code_diffs)

    here = host_identity()
    there = h.get("host") or {}
    host_note = None
    if there.get("hostname") and there["hostname"] != here["hostname"]:
        # A pure host difference is NOT drift: reported, and the verdict is
        # decided by code and pointers. v2's table demanded "same host" for
        # CLEAN while a criterion said otherwise (kimi).
        host_note = (f"captured on {there.get('hostname')} as "
                     f"{there.get('user')!r}; you are on {here['hostname']} as "
                     f"{here['user']!r}")

    for run in h["runs"]:
        if not isinstance(run, dict):
            malformed.append("a runs entry is not an object")
            continue
        rd = run.get("run_dir", "?")
        for ptr in (run.get("pointers") or []):
            if not isinstance(ptr, dict):
                malformed.append(f"{rd}: a pointer entry is not an object")
                continue
            recorded = ptr.get("path")
            if not recorded:
                malformed.append(
                    f"{rd}: pointer {ptr.get('declared')!r} has no absolute "
                    f"path. The handoff was written wrong -- re-capture it; "
                    f"switching hosts will not help")
                continue
            if not str(recorded).startswith("/"):
                malformed.append(
                    f"{rd}: pointer {recorded!r} is relative. Re-capture: "
                    f"paths are recorded absolute so resume needs no cwd")
                continue
            path = rebase_path(recorded, args.base)
            try:
                st = os.stat(path)
            except OSError:
                if ptr.get("exists"):
                    elsewhere.append(
                        f"{rd}: {path} was present at capture and is not "
                        f"reachable here"
                        + (f" (it was on {ptr.get('filesystem')})"
                           if ptr.get("filesystem") else ""))
                continue
            # Size feeds the verdict; mtime never does. A checkpoint copied
            # with fresh mtimes and unchanged bytes must not prompt a re-run.
            size_now = (dir_size(path) if stat.S_ISDIR(st.st_mode)
                        else st.st_size)
            if ptr.get("size") is not None and size_now != ptr["size"]:
                drift.append(f"{rd}: {path} is {size_now} bytes, was "
                             f"{ptr['size']} at capture")
        # Input identity, which is why contract_digest is recorded at all. It
        # was captured and never compared, so a changed contract passed as
        # CLEAN -- the criterion says code AND inputs must match, and both
        # reviewers found this independently.
        recorded_digest = run.get("contract_digest")
        if recorded_digest:
            name = ("training-contract.json" if run.get("kind") == "training"
                    else "contract.json")
            live = read_state_file(rebase_path(rd, args.base), name)
            if live is None:
                elsewhere.append(
                    f"{rd}: {name} is not readable from here, so the input "
                    f"identity cannot be compared")
            else:
                now_digest = contract_digest(live)
                if now_digest != recorded_digest:
                    drift.append(
                        f"{rd}: the contract has changed since capture "
                        f"(recorded {recorded_digest[:12]}, here "
                        f"{(now_digest or 'unreadable')[:12]}). Re-capture, or "
                        f"restore the contract this handoff was taken against")

        v = run.get("verdict") or {}
        if v.get("reason"):
            reports.append(f"{rd}: verdict not attributable -- {v['reason']}")
        elif v.get("exit_code") not in (None, 0):
            reports.append(f"{rd}: recorded verdict {v.get('verdict')} "
                           f"(exit {v.get('exit_code')})")

    # First match wins, in this order: exclusive by construction.
    if malformed:
        finish("MALFORMED", malformed, reports, host_note)
    elif drift:
        finish("DRIFTED", drift, reports, host_note)
    elif elsewhere:
        finish("ELSEWHERE", elsewhere, reports, host_note)
    else:
        finish("CLEAN", [], reports, host_note)


def dir_size(path):
    total, n = 0, 0
    for root, _dirs, files in os.walk(str(path)):
        for name in files:
            n += 1
            if n > MAX_DIR_ENTRIES:
                return total
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def bail_resume(short, lines):
    finish(short, lines, [], None)


def finish(short, mismatches, reports, host_note):
    state = f"HANDOFF_{short}"
    print(state)
    if host_note:
        print(f"  host: {host_note}")
    for m in mismatches:
        print(f"  - {m}")
    for r in reports:
        print(f"  note: {r}")
    if short == "CLEAN":
        print("  code and inputs match and every pointer resolves here. "
              "Absence of a difference is not proof the work is sound -- it is "
              "the same code looking at the same artifacts.")
    sys.exit(STATES[state])


# --- memory -----------------------------------------------------------------

BEGIN = "<!-- handoff:facts:begin sha={sha} -->"
BEGIN_RE = re.compile(r"<!--\s*handoff:facts:begin(?:\s+sha=([0-9a-f]{7,40}))?\s*-->")
END = "<!-- handoff:facts:end -->"

JUDGMENT_HEADINGS = ("decision", "blocker", "recommend", "open question",
                     "next action", "working note")


def is_ancestor(sha, cwd):
    """Whether `sha` is an ancestor of HEAD.

    `git log <sha>..HEAD` where sha is NOT an ancestor silently returns the
    whole branch history rather than erroring, which after a rebase would
    rewrite the factual section with everything (kimi, plan review 2). Checked
    rather than assumed.
    """
    if not sha:
        return False
    rc, _out, _err = run(["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                         cwd=cwd)
    return rc == 0


def facts_block(project_dir, recorded_sha):
    """The factual sections, and the sha they were generated from.

    Scoped by COMMIT SHA, never by time: a restored file or clock skew silently
    omits or repeats commits, and stamping a fresh timestamp each run makes
    determinism impossible. A sha is an identity; a timestamp is an inference.
    """
    cwd = str(project_dir)
    rc, head, _ = run(["git", "rev-parse", "HEAD"], cwd=cwd)
    head = head if rc == 0 else None
    lines = []

    if head is None:
        lines.append("_Not a git work tree, so no commit history is available._")
        rng_note = None
    elif recorded_sha and is_ancestor(recorded_sha, cwd):
        rng_note = f"commits in `{recorded_sha[:12]}..HEAD`"
        rc, out, _ = run(["git", "log", "--oneline", "--no-decorate",
                          f"{recorded_sha}..HEAD"], cwd=cwd)
        entries = [l for l in (out or "").splitlines() if l.strip()][:200]
        lines.append(f"**{rng_note}** ({len(entries)}):")
        lines.extend(f"- `{e}`" for e in entries) if entries else lines.append(
            "- none")
    else:
        why = ("no previous marker" if not recorded_sha
               else f"`{recorded_sha[:12]}` is not an ancestor of HEAD, which "
                    f"is what a rebase produces, so a range from it would "
                    f"return the whole branch")
        rng_note = "full history"
        lines.append(f"**Recent commits** (regenerated in full: {why}):")
        rc, out, _ = run(["git", "log", "--oneline", "--no-decorate", "-n", "25"],
                         cwd=cwd)
        entries = [l for l in (out or "").splitlines() if l.strip()]
        lines.extend(f"- `{e}`" for e in entries) if entries else lines.append(
            "- none")

    rc, out, _ = run(["git", "status", "--porcelain"], cwd=cwd)
    dirty = [l for l in (out or "").splitlines() if l.strip()][:60]
    lines.append("")
    lines.append(f"**Uncommitted paths** ({len(dirty)}):")
    lines.extend(f"- `{d}`" for d in dirty) if dirty else lines.append("- none")

    runs = discover_run_dirs(project_dir)
    lines.append("")
    lines.append(f"**Open contracts** ({len(runs)}):")
    if not runs:
        lines.append("- none found under this directory")
    for rd in runs:
        c = capture_run_dir(rd)
        v = c.get("verdict") or {}
        label = v.get("verdict") or v.get("reason") or c.get("reason") or "?"
        lines.append(f"- `{Path(rd).name}` ({c.get('kind') or 'unknown'}): {label}")
    return "\n".join(lines), head


def discover_run_dirs(project_dir, limit=50):
    """Directories under project_dir holding a contract, bounded."""
    found = []
    for root, dirs, files in os.walk(str(project_dir)):
        dirs[:] = [d for d in dirs if not d.startswith(".")][:200]
        if "contract.json" in files or "training-contract.json" in files:
            found.append(root)
            if len(found) >= limit:
                break
    return sorted(found)


def cmd_memory(args):
    d = Path(args.project_dir).resolve()
    if not d.is_dir():
        usage_error(f"error: {d} is not a directory")
    mem = d / "MEMORY.md"
    existing = ""
    if mem.exists():
        raw, err = read_text_bounded(mem)
        if err:
            usage_error(f"error: cannot read {mem}: {err}")
        existing = raw or ""

    m = BEGIN_RE.search(existing)
    recorded_sha = m.group(1) if m else None
    body, head = facts_block(d, recorded_sha)
    block = (BEGIN.format(sha=head or "none") + "\n\n" + body + "\n\n" + END)

    if m and END in existing:
        start = m.start()
        end = existing.index(END) + len(END)
        updated = existing[:start] + block + existing[end:]
    else:
        # No markers: append, and never touch what is already there. A judgment
        # section is preserved by construction because nothing outside the
        # markers is rewritten.
        sep = "" if existing.endswith("\n") or not existing else "\n"
        updated = existing + sep + "\n## Generated facts\n\n" + block + "\n"

    if updated == existing:
        print(f"{mem}: no change")
        return
    guard = [h for h in JUDGMENT_HEADINGS
             if h in block.lower()]
    if guard:
        usage_error(f"error: refusing to write: the generated block mentions a "
                 f"judgment heading ({', '.join(guard)}), which this command "
                 f"must never author. Report this as a bug")
    try:
        mem.write_text(updated)
    except OSError as e:
        usage_error(f"error: cannot write {mem}: {e}")
    print(f"{mem}: factual sections regenerated at sha {(head or 'none')[:12]}")


# --- cli --------------------------------------------------------------------

class _ArgParser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which collides with
    HANDOFF_ELSEWHERE. A bad command line is a usage problem, not a verdict."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"error: {message}\n")
        sys.exit(64)


def main():
    ap = _ArgParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("capture", help="record durable state for one or more "
                                       "run directories")
    p.add_argument("run_dir", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--cwd", default=None, help="repo root for code identity")
    p.set_defaults(fn=cmd_capture)

    p = sub.add_parser("resume", help="compare this machine against a handoff")
    p.add_argument("handoff")
    p.add_argument("--base", default=None,
                   help="re-root recorded paths for a tree that moved")
    p.add_argument("--cwd", default=None, help="repo root for code identity")
    p.add_argument("--watchdog", type=int, default=600,
                   help="give up after this many seconds rather than hanging "
                        "on an unresponsive mount (0 disables)")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("memory", help="regenerate the factual sections of "
                                      "MEMORY.md between its markers")
    p.add_argument("project_dir")
    p.set_defaults(fn=cmd_memory)

    args = ap.parse_args()
    if getattr(args, "watchdog", None):
        def _timed_out(sec):
            print("HANDOFF_ELSEWHERE")
            print(f"  - gave up after {sec}s: a recorded path may be on an "
                  f"unresponsive mount. Try --base, or run this where those "
                  f"paths live")
            sys.exit(STATES["HANDOFF_ELSEWHERE"])
        arm_watchdog(args.watchdog, _timed_out)
    args.fn(args)


if __name__ == "__main__":
    main()
