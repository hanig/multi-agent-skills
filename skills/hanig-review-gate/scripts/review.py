#!/usr/bin/env python3
"""review.py — adversarial multi-model review gate.

Same principle as the workflow contract, turned on the author: my claim that
code works is exactly as inadmissible as a scheduler's COMPLETED. So before
anything is called done, the diff and the claims made about it go to
independent models that did not write the code, each prompted to REFUTE rather
than to approve.

Reviewers are adversarial by construction. A reviewer that cannot decide is
instructed to refute, because the cost of a false "looks good" is much higher
than the cost of one more look.

Usage:
    review.py --diff                     review the working-tree diff vs HEAD
    review.py --staged                   review staged changes
    review.py --range HEAD~3..HEAD       review a commit range
    review.py --file a.py --file b.py    review whole files
    review.py ... --claim "X is true" --claim "Y is handled"
    review.py --list                     show reviewers and live availability

Exit codes:
    0  REVIEW_PASS          quorum reviewed; no confirmed defect, no refuted claim
    1  REVIEW_FAIL          quorum reviewed; a confirmed defect
    2  REVIEW_UNAVAILABLE   no reviewer could run -- NOT a pass
    3  REVIEW_PARTIAL       some ran, quorum unmet -- caller decides
    4  REVIEW_ERROR         usage or configuration error
    6  REVIEW_INCOMPLETE    required reviewer returned no usable content
    7  REVIEW_CLAIMS_REFUTED quorum reviewed; refuted claims, no confirmed defect

Never treat a nonzero state as success. An unreviewed change is unreviewed.

Python 3.8+, standard library only.
"""

import argparse
import concurrent.futures
import errno
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Exit 5 belongs to ARC-709's REVIEW_ADJUDICATION recovery implementation;
# leave it reserved even while that state is absent from this branch.
STATES = {"REVIEW_PASS": 0, "REVIEW_FAIL": 1, "REVIEW_UNAVAILABLE": 2,
          "REVIEW_PARTIAL": 3, "REVIEW_ERROR": 4,
          "REVIEW_INCOMPLETE": 6, "REVIEW_CLAIMS_REFUTED": 7}

HERE = Path(__file__).resolve().parent
CONFIG = HERE.parent / "reviewers.json"
DEFAULT_PROFILE = "standard"
# Bound on rounds for ONE change. Five rounds on one change in a single session
# produced rounds 3, 4 and 5 each finding a defect in the previous round's fix.
# That is the signal to step back to root cause, not to review again.
MAX_ROUNDS = 3

HONEST_RUN_CLAIM = "This change cannot make an honest run fail."
DISPOSITIONS = {"reproduced", "not-reproduced", "deferred"}

# Keep payloads bounded; an oversized diff silently truncated is a lie about
# what was reviewed, so truncation is always reported in the output.
MAX_CHARS = 180_000

# Every reviewer here is a reasoning model, and reasoning tokens come out of the
# same budget as the answer. At 16000 a 150KB review spent the whole budget
# thinking and returned NO content -- which read as an unavailable reviewer, and
# cost kimi-k2.7-code a whole session before the error message was made to say
# so. Sized for the answer AFTER the thinking.
DEFAULT_MAX_OUTPUT_TOKENS = 64_000

JOURNAL_DIR = "hanig-review-gate"
JOURNAL_CHILD_ARG = "--_append-review-journal"
JOURNAL_TIMEOUT_SECONDS = 5
JOURNAL_DIAGNOSTIC_TIMEOUT_SECONDS = 0.25
JOURNAL_NAME = "review-rounds"
JOURNAL_TEST_MARKER = "HANIG_REVIEW_GATE_TESTING"
JOURNAL_TEST_ROOT_PREFIX = ".hanig-review-gate-tests-"
JOURNAL_HEADER = (
    "Append-only logical collection of immutable per-round JSON lines; "
    "audit-only attested review history. This is not the rejected "
    "mandatory per-change receipt: that receipt could lock honest authors out "
    "of the gate, while this non-gating record cannot decide or block a verdict."
)


SYSTEM = """You are an adversarial code reviewer. Your job is to REFUTE, not to approve.

You did not write this code and have no stake in it being correct. Assume a
defect exists and try to find it. If you cannot decide whether something is
correct, treat it as refuted -- a false "looks good" costs far more than one
more look.

Judge only what you can see. Do NOT invent problems to appear thorough:
speculative, stylistic, or pre-existing issues outside the change are noise and
must be omitted. A finding must come with a concrete failure scenario: specific
inputs or state, and the wrong behaviour that results. If you cannot write that
scenario, it is not a finding.

SCOPE. Adversarial does not mean unbounded. If a THREAT MODEL section is given,
it says which inputs are trusted and which are hostile. State each finding's
"preconditions" -- what someone must be able to do for it to occur -- and set
"in_scope" false when those preconditions are excluded by the threat model.
Report such findings anyway, marked, but they do not decide the verdict: a
defect requiring an attacker the tool never claimed to defend against is not
the same as one a careful user hits by accident. When no threat model is given,
everything is in scope.

Design disagreements are not defects. Two tools that solve different problems
will name and structure things differently. Report an inconsistency only where
it produces a wrong result, not where it offends symmetry.

Separately, assess EVERY asserted CLAIM against the evidence in the code. Give
one entry per claim, in order, with its zero-based "claim_index". Do not omit,
merge, or duplicate claims:
- supported:    the code clearly does what the claim says
- refuted:      the code contradicts the claim -- say exactly how
- unverifiable: cannot be determined from what is shown (say what is missing)

The mandatory counter-claim "This change cannot make an honest run fail." uses
a term of art. An "honest run" is otherwise admissible, defect-free work,
judged independently of whatever the changed rule accepts; "fail" means that
such work is wrongly rejected. Apply this definition with the following
decision rule; the uppercase names state the two facts a refutation must supply:

HONEST_RUN_REFUTED := DEFECT_FREE_WORK_NAMED and WRONGFUL_REJECTION_EXPLAINED

Thus the claim remains falsifiable: refuting it requires naming specific
otherwise admissible, defect-free work and explaining how the change wrongly
rejects it. Merely observing that a change strengthens
admission or withholds authorization when a declared requirement is unmet does
not meet that burden: the reviewer must still name defect-free work and explain
why its rejection is wrongful under acceptance criteria applicable independently
of the change. Correctly discovering a defect does not refute the claim. Do not
redefine "honest" or "defect-free" to mean whatever the changed rule accepts;
that would make the claim circular. A false finding that blocks otherwise
admissible, defect-free work does refute the claim.

Reply with ONLY a JSON object, no prose or code fences:

{
  "verdict": "upheld" | "refuted",
  "findings": [
    {"severity": "critical"|"major"|"minor",
     "confidence": "high"|"medium"|"low",
     "file": "path", "line": 0,
     "summary": "one sentence: the defect",
     "failure_scenario": "concrete inputs/state -> wrong result",
     "preconditions": "what someone must be able to do for this to happen",
     "in_scope": true|false}
  ],
  "claims": [
    {"claim_index": 0, "claim": "verbatim claim",
     "status": "supported"|"refuted"|"unverifiable",
     "why": "REQUIRED: one specific sentence citing the code that decides it"}
  ],
  "notes": "at most two sentences, or empty"
}

"verdict" is "refuted" if any IN-SCOPE finding is critical or major with high or
medium confidence, or if any claim is refuted. Otherwise "upheld"."""


MAX_FILE_READ_BYTES = 64 * 1024 * 1024


def read_text_bounded(path):
    """(text, error). Regular files only, size-capped, non-blocking open.

    A plain read_text() on a --file argument blocks forever if the path is a
    FIFO, and the gate never prints a verdict at all."""
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "", f"not a regular file ({stat.filemode(st.st_mode)})"
        if st.st_size > MAX_FILE_READ_BYTES:
            return "", (f"{st.st_size} bytes, above the "
                        f"{MAX_FILE_READ_BYTES}-byte read limit")
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = None
            raw = fh.read(MAX_FILE_READ_BYTES + 1)
        return raw.decode("utf-8", errors="replace"), None
    except (OSError, MemoryError) as e:
        return "", f"unreadable: {type(e).__name__}: {e}"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def journal_timestamp():
    """Observed UTC date recorded inside each immutable journal entry."""
    stamp = time.time_ns()
    seconds, nanos = divmod(stamp, 1_000_000_000)
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
            + f".{nanos:09d}Z")


def _resolved(path):
    return Path(path).expanduser().resolve()


def _inside(path, directory):
    try:
        _resolved(path).relative_to(_resolved(directory))
        return True
    except ValueError:
        return False


def _require_git_free_test_root(path):
    """Return a resolved test root only when no ancestor has a .git entry.

    This deliberately checks the filesystem rather than invoking Git.  Normal
    worktrees have a .git directory and linked worktrees have a .git file; a
    dangling symlink is also conservatively an entry.  Absence is normal, but
    an inspection error cannot establish eligibility and therefore fails
    closed.
    """
    root = _resolved(path)
    for directory in (root, *root.parents):
        marker = directory / ".git"
        try:
            marker.lstat()
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                continue
            raise OSError(
                f"cannot establish whether test root {str(root)!r} is below "
                f"a Git worktree: cannot inspect {str(marker)!r}: {exc}")
        raise OSError(
            f"test root {str(root)!r} is below Git worktree marker "
            f"{str(marker)!r}")
    return root


def _attached_worktrees(place):
    """All ordinary worktrees attached to the repository containing place."""
    top = git_out("-C", str(place), "rev-parse", "--show-toplevel").strip()
    if not top:
        return set()
    found = {_resolved(top)}
    raw = git_out("-C", top, "worktree", "list", "--porcelain")
    for line in raw.splitlines():
        if line.startswith("worktree "):
            found.add(_resolved(line[len("worktree "):]))
    return found


def review_worktrees(files=()):
    """Worktrees for every repository that supplies this review's input."""
    places = [Path.cwd()]
    for value in files:
        lexical = Path(os.path.abspath(os.path.expanduser(value)))
        lexical_place = (lexical if lexical.is_dir() and not lexical.is_symlink()
                         else lexical.parent)
        # git -C follows a symlinked directory component. Survey every lexical
        # ancestor as well, so /repo/link/out.py still records /repo even when
        # link resolves outside that worktree.
        places.extend((lexical_place, *lexical_place.parents))
        path = _resolved(value)
        places.append(path if path.is_dir() else path.parent)
    found = set()
    for place in places:
        found.update(_attached_worktrees(place))
    return sorted(found, key=str)


def review_journal_path(files=()):
    """Journal below a state home outside every operated Git worktree.

    This follows coordinator_paths.py's candidate order and containment
    doctrine. A relative XDG_STATE_HOME is not a state home, and a candidate
    inside any reviewed repository's attached worktrees is skipped before any
    directory is created.
    """
    worktrees = review_worktrees(files)
    candidates = []
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and os.path.isabs(os.path.expanduser(xdg)):
        # Preserve configured components for the descriptor-relative no-follow
        # walk. Resolution here would silently turn a configured symlink into
        # its target before _open_directory_chain could refuse it.
        candidates.append(Path(os.path.abspath(os.path.expanduser(xdg))))
    candidates.append(_resolved(Path.home() / ".local" / "state"))
    candidates.append(_resolved(Path(tempfile.gettempdir()) /
                                "hanig-review-gate-state"))
    base = next((candidate for candidate in candidates
                 if not any(_inside(candidate, worktree)
                            for worktree in worktrees)), None)
    if base is None:
        raise OSError("no review journal state location is available outside "
                      "the operated Git worktrees")
    path = base / JOURNAL_DIR / JOURNAL_NAME
    resolved_path = _resolved(path)
    for worktree in worktrees:
        if _inside(resolved_path, worktree):
            raise OSError(f"review journal {str(path)!r} resolves inside "
                          f"operated Git worktree {str(worktree)!r}")
    if not _inside(resolved_path, base):
        raise OSError(f"review journal {str(path)!r} resolves outside its "
                      f"state home {str(base)!r}")
    return path


def claim_digests(claims):
    """Ordered digests retain claim identity without copying claim text."""
    return [hashlib.sha256(claim.encode("utf-8")).hexdigest()
            for claim in claims]


def _open_directory_chain(path):
    """Open/create an absolute directory path without retraversing names.

    Every component is opened relative to its already-open parent. A
    concurrent rename leaves the descriptor on the directory that was
    actually checked; an intermediate symlink is never followed.
    """
    path = Path(path)
    if not path.is_absolute():
        raise OSError(f"journal directory {str(path)!r} is not absolute")
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if not all(hasattr(os, name) for name in required):
        raise OSError("descriptor-anchored journal paths require O_DIRECTORY "
                      "and O_NOFOLLOW on this host")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(os.path.sep, flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    # A cooperating creator won the race. The anchored,
                    # no-follow open below decides whether it made a directory.
                    pass
                child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def review_journal_details(results):
    """Classify the completed results before redacting audit text.

    Keep the gate's classification even if a configured key collides
    with a status token. The helper must not reclassify scrubbed text.
    """
    return {
        "results": [{
            **result,
            "findings": [{
                **finding,
                "location": f"{finding.get('file', '?')}:{finding.get('line', '?')}",
                "confirmed": is_confirmed(finding),
            } for finding in result.get("findings", [])],
        } for result in results],
        "refuted_claims": [
            {**claim, "reviewer": result["name"]}
            for result in results for claim in result.get("claims", [])
            if norm(claim.get("status")) == "refuted"
        ],
        "rejecting_reviewers": [
            result["name"] for result in results
            if norm(result.get("verdict")) == "refuted"
        ],
    }


def prepare_review_journal(kind, round_no, effective_panel, verdict, claims,
                           *, panel_policy=None, results=()):
    """Finish record semantics, redaction and serialization before I/O."""
    record = {
        "type": "review_round",
        "schema_version": 2,
        "journal_header": JOURNAL_HEADER,
        "date": journal_timestamp(),
        "kind": kind,
        "round": round_no,
        "effective_panel": list(effective_panel),
        "verdict": verdict,
        # Hash the original text. Mandatory redaction
        # can obscure a digest that contains a configured key value.
        "claim_digests": claim_digests(claims),
        "claims": list(claims),
    }
    record.update(review_journal_details(results))
    if panel_policy is not None:
        record["panel_policy"] = panel_policy
    record = deep_redact(record)
    return record, json.dumps(record, sort_keys=True) + "\n"


def append_review_journal(path, kind, round_no, effective_panel, verdict,
                          claims, *, panel_policy=None, results=()):
    """Prepare and atomically publish one immutable audit record."""
    record, line = prepare_review_journal(
        kind, round_no, effective_panel, verdict, claims,
        panel_policy=panel_policy, results=results)
    return record, _write_review_journal(path, line)


def _write_review_journal(path, line):
    """Atomically publish one immutable audit record.

    State-directory topology and non-cooperating same-UID relinking are trusted
    while this transaction runs. The journal is a logical append-only
    collection: every completed round is one newline-terminated JSON file in
    an exclusive event directory. Partial writes remain private pending files
    and are never canonical history.
    """
    path = Path(path)
    test_root = os.environ.get(JOURNAL_TEST_MARKER)
    if test_root is not None:
        raw_root = Path(test_root)
        if not test_root or not raw_root.is_absolute():
            raise OSError(
                f"{JOURNAL_TEST_MARKER} must name the absolute temporary "
                "root allowed for review-test journals")
        lexical_root = Path(os.path.expanduser(test_root))
        allowed_root = _resolved(lexical_root)
        try:
            root_status = lexical_root.lstat()
        except OSError as exc:
            raise OSError(
                f"{JOURNAL_TEST_MARKER} root {str(lexical_root)!r} is not "
                f"an existing directory: {exc}")
        if (test_root != str(allowed_root)
                or not stat.S_ISDIR(root_status.st_mode)
                or not allowed_root.name.startswith(JOURNAL_TEST_ROOT_PREFIX)):
            raise OSError(
                f"{JOURNAL_TEST_MARKER} must name a canonical directory "
                f"whose basename starts with {JOURNAL_TEST_ROOT_PREFIX!r}")
        try:
            _require_git_free_test_root(allowed_root)
        except OSError as exc:
            raise OSError(
                f"{JOURNAL_TEST_MARKER} root {str(allowed_root)!r} is not "
                f"eligible for isolated test state: {exc}")
        if not _inside(path, allowed_root):
            raise OSError(
                f"test-marked review journal {str(path)!r} resolves outside "
                f"the isolated temporary root {str(allowed_root)!r}")
    if not isinstance(line, str) or not line.endswith("\n") or line.count("\n") != 1:
        raise ValueError("prepared review journal record must be one "
                         "newline-terminated JSON line")
    line = line.encode("utf-8")
    collection_fd = event_fd = fd = None
    event_name = (f"{time.time_ns():020d}-{os.getpid()}-"
                  f"{os.urandom(12).hex()}")
    pending_name = "record.pending"
    final_name = "record.jsonl"
    try:
        collection_fd = _open_directory_chain(path)
        os.mkdir(event_name, 0o700, dir_fd=collection_fd)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        event_fd = os.open(event_name, directory_flags,
                           dir_fd=collection_fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(pending_name, flags, 0o600, dir_fd=event_fd)
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise OSError("pending review journal record is not a regular file")
        if status.st_nlink != 1:
            raise OSError("pending review journal record has multiple links")
        written = 0
        while written < len(line):
            try:
                count = os.write(fd, line[written:])
            except InterruptedError:
                continue
            if count <= 0:
                raise OSError(f"journal append made no progress after "
                              f"{written} of {len(line)} bytes")
            written += count
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.rename(pending_name, final_name,
                  src_dir_fd=event_fd, dst_dir_fd=event_fd)
        os.fsync(event_fd)
        os.fsync(collection_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if event_fd is not None:
            os.close(event_fd)
        if collection_fd is not None:
            os.close(collection_fd)
    return path / event_name / final_name


def _journal_child(payload):
    """Perform one append in the bounded audit-only helper process."""
    path = review_journal_path(payload["files"])
    record_path = _write_review_journal(path, payload["record_line"])
    return {"ok": True, "path": str(record_path)}


def _run_journal_child(args, completed, verdict):
    """Run journal I/O out of process so stalled storage cannot gate review."""
    # Only the finished record is redacted. The non-persisted control envelope
    # is wrapped afterwards, and the child never interprets record fields.
    _record, line = prepare_review_journal(
        args.kind, args.round, [result["name"] for result in completed],
        verdict, args.claim, panel_policy=getattr(args, "panel_policy", None),
        results=completed)
    payload = json.dumps({"files": args.file, "record_line": line})
    env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(name, None)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), JOURNAL_CHILD_ARG],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", env=env)
    try:
        stdout, child_stderr = process.communicate(
            input=payload, timeout=JOURNAL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.communicate(timeout=1)
        except Exception:
            pass
        raise TimeoutError(
            f"review journal append exceeded {JOURNAL_TIMEOUT_SECONDS}s")
    if process.returncode != 0:
        try:
            result = json.loads(stdout)
            error = result["error"]
        except Exception:
            error = child_stderr.strip() or "journal helper returned no error"
        raise OSError(f"journal helper failed: {error}")
    try:
        result = json.loads(stdout)
    except (TypeError, ValueError) as exc:
        raise OSError(f"journal helper returned invalid JSON: {exc}")
    if not result.get("ok") or not isinstance(result.get("path"), str):
        raise OSError("journal helper returned an invalid success record")
    return result["path"]


def _emit_journal_failure(message):
    """Report a non-gating failure without letting diagnostics gate review."""
    # Unit tests and in-process callers use a memory stream, which cannot block
    # on an external reader and preserves their ability to inspect diagnostics.
    try:
        if (type(sys.stderr).__module__ == "_io" and
                type(sys.stderr).__name__ == "StringIO"):
            sys.stderr.write(message + "\n")
            return
    except Exception:
        return

    data = (message + "\n").encode("utf-8", "backslashreplace")
    try:
        pid = os.fork()
    except (AttributeError, OSError):
        return
    if pid == 0:
        try:
            os.write(2, data)
        except Exception:
            pass
        os._exit(0)

    deadline = time.monotonic() + JOURNAL_DIAGNOSTIC_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            finished, _status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if finished == pid:
            return
        time.sleep(0.01)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def record_review_round(args, completed, verdict):
    """Write bounded, non-gating audit history; report failure if possible."""
    try:
        path = _run_journal_child(args, completed, verdict)
        return {"path": path, "written": True, "status": "confirmed",
                "error": None}
    except Exception as exc:
        try:
            error = redact(f"{type(exc).__name__}: {exc}")
        except Exception:
            error = type(exc).__name__
        message = ("JOURNAL_WRITE_FAILED — audit history persistence was not "
                   "confirmed: "
                   f"{error}. The review verdict is unchanged because the "
                   "journal is non-gating.")
        _emit_journal_failure(message)
        return {"path": None, "written": False, "status": "unconfirmed",
                "error": error}


def deep_redact(obj):
    """Recursively scrub every string in reviewer-authored structures.

    Dictionary KEYS are scrubbed as well: a reviewer returning a finding object
    keyed by a credential would otherwise leak it untouched."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [deep_redact(x) for x in obj]
    if isinstance(obj, dict):
        return {redact(k) if isinstance(k, str) else k: deep_redact(v)
                for k, v in obj.items()}
    return obj


def redact(text):
    """Remove API key values from anything we are about to print or return.

    Transport errors can embed the Authorization header verbatim (http.client
    does exactly this when a key contains a newline), so no error string leaves
    this module without being scrubbed."""
    if not text:
        return text
    s = str(text)
    for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        val = os.environ.get(var)
        # Values under 4 characters are not real credentials, and scrubbing
        # them would corrupt ordinary text. Documented limit, not an oversight.
        if not val or len(val) < 4:
            continue
        tag = f"<{var} redacted>"
        # Every form the value can take on the way out: raw, Python repr, and
        # JSON-escaped (json.dumps escapes quotes and backslashes, which a
        # literal replace on the serialized report would then miss).
        for form in (val,
                     repr(val).strip("'\""),
                     json.dumps(val)[1:-1]):
            if form:
                s = s.replace(form, tag)
    return s


# --- providers --------------------------------------------------------------

def no_content_error(rev, text, *, finish_reason=None, reasoning_tokens=None,
                     completion_tokens=None, status=None):
    """Classify an empty provider reply consistently across both APIs."""
    if (text or "").strip():
        return None
    if finish_reason in ("length", "max_output_tokens"):
        return (f"no content: the model used its whole output budget "
                f"({completion_tokens} tokens"
                + (f", {reasoning_tokens} of them reasoning"
                   if reasoning_tokens else "")
                + f") before emitting any. Raise max_output_tokens for "
                  f"{rev['name']} in reviewers.json, or review fewer files.")
    details = []
    if status is not None:
        details.append(f"status={status!r}")
    if finish_reason is not None:
        details.append(f"finish_reason={finish_reason!r}")
    if reasoning_tokens:
        details.append(f"{reasoning_tokens} reasoning tokens")
    return "no content in the reply" + (
        " (" + ", ".join(details) + ")" if details else "")


def _post(url, payload, headers, timeout, retries=3, deadline=None):
    """POST with backoff on transient failures. A gateway hiccup must not
    silently remove a reviewer from the panel -- that would quietly shrink the
    quorum and make the gate weaker than it claims to be."""
    last = None
    for attempt in range(1, retries + 1):
        if deadline is not None:
            left = deadline - time.time()
            if left <= 1:
                return None, (f"{last or 'no attempt made'} "
                              f"(reviewer deadline reached)")
            timeout = min(timeout, max(1, int(left)))
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                chunks, total = [], 0
                while True:
                    if deadline is not None and time.time() > deadline:
                        return None, ("reviewer deadline reached while the "
                                      "response was still arriving")
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        return None, "response exceeded 64MB"
                return json.loads(b"".join(chunks).decode(
                    "utf-8", errors="replace")), None
        except urllib.error.HTTPError as e:
            # Bounded and deadline-aware: an error body can trickle just as a
            # success body can, and e.read() was unbounded.
            parts, got = [], 0
            try:
                while got < 65536:
                    if deadline is not None and time.time() > deadline:
                        break
                    piece = e.read(8192)
                    if not piece:
                        break
                    parts.append(piece)
                    got += len(piece)
            except Exception:
                pass
            body = redact(b"".join(parts).decode(errors="replace"))[:300]
            last = f"HTTP {e.code}: {body}"
            # 4xx is our fault (bad key, bad model id) -- retrying cannot help.
            if e.code < 500 and e.code != 429:
                return None, last
        except Exception as e:  # timeout, DNS, TLS reset
            last = f"{type(e).__name__}: {redact(e)}"
        if attempt < retries:
            nap = min(2 ** attempt, 15)
            if deadline is not None:
                nap = min(nap, max(0, deadline - time.time()))
            if nap > 0:
                time.sleep(nap)
    return None, redact(f"{last} (after {retries} attempts)")


def call_openai(rev, prompt, timeout, deadline=None):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None, "OPENAI_API_KEY not set"
    payload = {
        "model": rev["model"],
        "input": f"{SYSTEM}\n\n---\n\n{prompt}",
        # Same default as call_openrouter. This path was left at 16000 when
        # that one was raised to 64000, and reviewers.json's own comment
        # already claimed "All reviewers use DEFAULT_MAX_OUTPUT_TOKENS
        # (64000)". luna at effort=high then spent its whole 16000 budget on
        # reasoning for a 53KB document and returned an empty response twice,
        # costing a plan review its quorum. It had worked minutes earlier only
        # because it was temporarily routed via openrouter.
        "max_output_tokens": rev.get("max_output_tokens",
                                     DEFAULT_MAX_OUTPUT_TOKENS),
    }
    if rev.get("effort"):
        payload["reasoning"] = {"effort": rev["effort"]}
    data, err = _post("https://api.openai.com/v1/responses", payload,
                      {"Authorization": f"Bearer {key}",
                       "Content-Type": "application/json"}, timeout,
                      deadline=deadline)
    if err:
        return None, err
    try:
        text = "".join(
            c.get("text", "")
            for o in (data.get("output") or [])
            if isinstance(o, dict) and o.get("type") == "message"
            for c in (o.get("content") or [])
            if isinstance(c, dict)
        )
    except (AttributeError, TypeError):
        return None, redact(f"unexpected response shape: {str(data)[:200]}")
    usage = data.get("usage", {})
    detail = usage.get("output_tokens_details") or {}
    incomplete_detail = data.get("incomplete_details") or {}
    empty_error = no_content_error(
        rev, text, status=data.get("status"),
        finish_reason=incomplete_detail.get("reason"),
        reasoning_tokens=detail.get("reasoning_tokens"),
        completion_tokens=usage.get("output_tokens"))
    if empty_error:
        return None, empty_error
    return {"text": text,
            "in_tokens": usage.get("input_tokens"),
            "out_tokens": usage.get("output_tokens")}, None


def call_openrouter(rev, prompt, timeout, deadline=None):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None, "OPENROUTER_API_KEY not set"
    payload = {
        "model": rev["model"],
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "max_tokens": rev.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
    }
    if rev.get("effort"):
        payload["reasoning"] = {"effort": rev["effort"]}
    data, err = _post("https://openrouter.ai/api/v1/chat/completions", payload,
                      {"Authorization": f"Bearer {key}",
                       "Content-Type": "application/json"}, timeout,
                      deadline=deadline)
    if err:
        return None, err
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        # redact: an error body can echo the Authorization header back.
        return None, redact(f"unexpected response shape: {str(data)[:200]}")
    usage = data.get("usage", {})
    detail = usage.get("completion_tokens_details") or {}
    empty_error = no_content_error(
        rev, text, finish_reason=choice.get("finish_reason"),
        reasoning_tokens=detail.get("reasoning_tokens"),
        completion_tokens=usage.get("completion_tokens"))
    if empty_error:
        return None, empty_error
    return {"text": text,
            "in_tokens": usage.get("prompt_tokens"),
            "out_tokens": usage.get("completion_tokens")}, None


PROVIDERS = {"openai": call_openai, "openrouter": call_openrouter}


# --- parsing ----------------------------------------------------------------

def parse_verdict(text):
    """Models sometimes wrap JSON in fences or prose. Recover the object."""
    if not text or not text.strip():
        return None, "empty response"
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s[3:]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    start, depth, found = None, 0, None
    in_str, esc = False, False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                found = s[start:i + 1]
                break
    if not found:
        if start is not None:
            # Braces opened and never closed: the model hit its token ceiling.
            # Say that, rather than "no JSON found" for text starting with '{'.
            return None, ("response truncated mid-JSON (raise max_output_tokens "
                          f"for this reviewer); {len(s)} chars received")
        return None, f"no JSON object found in: {text[:150]}"
    try:
        return json.loads(found), None
    except json.JSONDecodeError as e:
        return None, f"malformed JSON: {e}"


def norm(v):
    """Model-emitted enums vary in case ('Refuted' vs 'refuted'). Matching them
    case-sensitively silently dropped refuted claims and let the gate pass."""
    return str(v).strip().lower() if v is not None else ""


# Words that appear in nearly every claim carry no discriminating signal; left
# in, they let two unrelated claims "match" on filler alone.
CLAIM_STOPWORDS = frozenset("""
claim claims about that this then than there their they when where which while
with without from into onto over under only ever never always cannot must
should would could shall will does done doing have been being were was are
same such each every both either neither also just even more most less least
other another using used uses use make makes made give given gives take taken
case cases thing things text output input value values return returns
""".split())


def claim_key(s):
    """Normalise a claim for matching: models reword, so compare on distinctive
    content words rather than demanding an exact string."""
    words = [w for w in re.findall(r"[a-z0-9_]+", str(s).lower())
             if len(w) > 3 and w not in CLAIM_STOPWORDS]
    return set(words)


def unassessed_claims(returned, asserted):
    """Asserted claims with no corresponding entry in the reviewer's response.

    Assignment is ONE-TO-ONE: an entry is consumed once it accounts for a claim.
    Matching many-to-one let two identical entries for claim A satisfy both A
    and B, so a claim could go unexamined while the count looked right."""
    missing = []
    used = set()
    entries = [(j, c) for j, c in enumerate(returned) if isinstance(c, dict)]

    # Explicit indices first: they are unambiguous, so honour them before
    # spending entries on fuzzy content matches.
    by_index, spent_text = {}, set()
    for j, c in entries:
        idx = c.get("claim_index")
        if not (isinstance(idx, int) and not isinstance(idx, bool)
                and 0 <= idx < len(asserted) and idx not in by_index):
            continue
        # An index alone was trusted, so unrelated text could "cover" a claim.
        # A non-string claim (e.g. the integer 0) has no distinctive words and
        # slipped past the overlap test entirely.
        if not isinstance(c.get("claim"), str) or not c["claim"].strip():
            continue
        akey, ckey = claim_key(asserted[idx]), claim_key(c["claim"])
        if akey and (not ckey
                     or len(akey & ckey) < max(1, int(0.3 * len(akey)))):
            continue
        # Two entries with the same wording are one assessment, whatever they
        # index. Otherwise duplicate text indexed 0 and 1 covered both claims.
        sig = frozenset(claim_key(c.get("claim", "")))
        if sig and sig in spent_text:
            continue
        if sig:
            spent_text.add(sig)
        by_index[idx] = j

    for i, a in enumerate(asserted):
        if i in by_index:
            used.add(by_index[i])

    # Score every (claim, entry) pair, then assign globally best-first AND
    # exclusively: an entry may only account for the claim it most resembles.
    # Global best-first alone was not enough -- a duplicate entry for claim A
    # still cleared the threshold for a near-identical claim B and covered it.
    # Under-crediting (reporting unassessed) is the safe direction here.
    pending, scores = [], []
    for i, a in enumerate(asserted):
        if i in by_index:
            continue
        akey = claim_key(a)
        if not akey:
            # No distinctive words to match on. Only an explicit claim_index
            # can account for this claim; skipping it silently counted an
            # unexamined claim as assessed.
            missing.append(a[:70] + "  [no distinctive terms; needs claim_index]")
            continue
        pending.append(i)
        need = max(2, int(0.6 * len(akey)))
        for j, c in entries:
            ckey = claim_key(c.get("claim", ""))
            if not ckey or frozenset(ckey) in spent_text:
                continue
            overlap = len(akey & ckey)
            if overlap < need:
                continue
            # Jaccard breaks ties between claims that share wording: the entry
            # goes to the claim it actually resembles most.
            scores.append((overlap / len(akey | ckey), overlap, i, j))
    # An entry's own best claim. If an entry resembles A more than B, it cannot
    # be spent covering B, however far above threshold that pairing scored.
    best_for_entry = {}
    for score, overlap, i, j in scores:
        prev = best_for_entry.get(j)
        if prev is None or (score, overlap) > prev[0]:
            best_for_entry[j] = ((score, overlap), i)

    scores.sort(reverse=True)
    assigned = {}
    for score, overlap, i, j in scores:
        if i in assigned or j in used:
            continue
        owner = best_for_entry.get(j)
        if owner is not None and owner[1] != i:
            continue  # this entry belongs to a claim it resembles more
        assigned[i] = j
        used.add(j)
    for i in pending:
        if i not in assigned:
            missing.append(asserted[i][:70])
    return missing


def verdict_schema_error(v, require_claims=0, asserted=None):
    """Reject anything that is not actually a review.

    require_claims: when claims were asserted, a reviewer must actually assess
    them. An empty claims array passed the schema while assessing nothing."""
    if not isinstance(v, dict):
        return "not an object"
    if norm(v.get("verdict")) not in ("upheld", "refuted"):
        return f"verdict must be 'upheld' or 'refuted', got {v.get('verdict')!r}"
    # Required, not optional: a response carrying only a verdict word assessed
    # nothing, and two of those could fill the quorum and pass the gate.
    for key in ("findings", "claims"):
        if key not in v:
            return f"missing required '{key}' array"
        if not isinstance(v[key], list):
            return f"{key} must be a list"
    for f in v.get("findings") or []:
        if not isinstance(f, dict):
            return "findings must contain objects"
        sev = norm(f.get("severity"))
        if sev not in ("critical", "major", "minor"):
            return f"finding severity must be critical/major/minor, got {f.get('severity')!r}"
        if norm(f.get("confidence")) not in ("high", "medium", "low"):
            return f"finding confidence must be high/medium/low, got {f.get('confidence')!r}"
        # Without this, a major finding lacking a scenario was accepted and then
        # silently ignored, so the gate could pass over a reported defect.
        scen = f.get("failure_scenario")
        if sev in ("critical", "major") and \
                (not isinstance(scen, str) or not scen.strip()):
            return (f"{sev} finding has no failure_scenario: "
                    f"{str(f.get('summary'))[:60]!r}")
    for c in v.get("claims") or []:
        if not isinstance(c, dict):
            return "claims must contain objects"
        # An entry with no status and no text asserts nothing; counting it as
        # an assessment let content-free stubs satisfy the claims requirement.
        if norm(c.get("status")) not in ("supported", "refuted", "unverifiable"):
            return (f"claim entry has no valid status: "
                    f"{str(c.get('status'))[:40]!r}")
        # An entry with an index and a status but no text asserts nothing
        # about the claim, and unassessed_claims counted it as covered.
        # Third iteration on this validator: text + status alone is still
        # content-free -- a reviewer can echo the claim back and mark it
        # supported without examining anything. Require the reasoning.
        why = str(c.get("why", "")).strip()
        # A length check alone is defeated by filler: fifteen x's passed.
        # Require several distinct words, not just characters.
        words = {w for w in re.findall(r"[a-z0-9_]{2,}", why.lower())}
        if len(words) < 4:
            return (f"claim entry's 'why' is not substantive "
                    f"({len(words)} distinct word(s)): {why[:40]!r}")
        if not isinstance(c.get("claim"), str) or not c["claim"].strip():
            return ("claim entry has no claim TEXT (a non-string value does "
                    "not identify a claim); an index alone does not show "
                    "which claim was assessed")
    if require_claims and len(v.get("claims") or []) < require_claims:
        return (f"assessed {len(v.get('claims') or [])} of {require_claims} "
                f"asserted claims")
    if asserted:
        missing = unassessed_claims(v.get("claims") or [], asserted)
        if missing:
            return f"did not assess: {'; '.join(missing)}"
    return None


def is_confirmed(f):
    """A finding counts against the gate only if it is serious AND the reviewer
    was reasonably sure AND it came with a concrete failure scenario AND it is
    reachable inside the declared threat model.

    in_scope defaults to True when the reviewer omits it: an unstated scope is
    not evidence of irrelevance, and this gate fails closed. Only an explicit
    false takes a finding out of the verdict, and it is still reported."""
    if f.get("in_scope") is False:
        return False
    return (norm(f.get("severity")) in ("critical", "major")
            and norm(f.get("confidence")) in ("high", "medium")
            and bool(str(f.get("failure_scenario", "")).strip()))


# --- input gathering --------------------------------------------------------

def git_out(*args):
    """Never raises. errors="replace" matters: a non-UTF-8 byte in a tracked
    file makes strict decoding throw UnicodeDecodeError out of the gate before
    it can print anything. contract.py has guarded this since round 4."""
    # start_new_session + killpg: a repository can set diff.external to a
    # command that spawns a background descendant holding the captured pipe.
    # Killing only git leaves subprocess.run waiting on EOF forever, so the
    # timeout alone was not enough -- the whole group has to go.
    pr = None
    try:
        pr = subprocess.Popen(["git", *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8",
                              errors="replace", start_new_session=True)
        out, _ = pr.communicate(timeout=120)
        return out or "" if pr.returncode == 0 else ""
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
        return ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def range_endpoints(range_spec):
    """Split at the first dotted operator outside braced revision suffixes."""
    index = 0
    while index < len(range_spec):
        if range_spec.startswith(("^{", "@{"), index):
            # Git ends these suffixes at the first }, even when a search
            # contains a literal {. Ordinary ref names may also contain {
            # without opening a revision suffix.
            end = range_spec.find("}", index + 2)
            if end < 0:
                return None
            index = end + 1
        elif range_spec.startswith("..", index):
            separator = "..." if range_spec.startswith("...", index) else ".."
            return (separator, range_spec[:index] or "HEAD",
                    range_spec[index + len(separator):] or "HEAD")
        else:
            index += 1
    return None


def resolve_range_ref(ref):
    """Let Git parse a revision before peeling its resolved object to a commit.

    Appending ^{commit} directly to :/regex changes the search pattern.
    Return failed Git results intact so the caller can name the original ref.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", timeout=120)
        if result.returncode == 0:
            result = subprocess.run(
                ["git", "rev-parse", "--verify",
                 f"{result.stdout.strip()}^{{commit}}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", timeout=120)
        return result
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        config_error(f"cannot resolve ref {ref!r}: {exc}. "
                     "Pass a resolvable commit ref in --range.")


def validate_range_endpoints(range_spec):
    """Reject bad refs before diff collection; return whether this is two-dot.

    Give Git the whole expression first: dots can be commit-search text.
    Only failed single-expression resolution needs range/shorthand parsing.
    """
    whole = resolve_range_ref(range_spec)
    if whole.returncode == 0:
        return False
    endpoints = range_endpoints(range_spec)
    refs = list(endpoints[1:]) if endpoints else [range_spec]
    if not endpoints:
        # Git's single-commit range shorthands expand to commit endpoints;
        # appending ^{commit} to the shorthand itself is not valid syntax.
        if len(range_spec) > 2 and range_spec.endswith(("^!", "^@")):
            refs = [range_spec[:-2]]
        else:
            parent_range = re.fullmatch(r"(.+)\^-(\d*)", range_spec)
            if parent_range:
                ref, parent = parent_range.groups()
                refs = [ref, f"{ref}^{parent or '1'}"]
    for ref in refs:
        result = whole if ref == range_spec else resolve_range_ref(ref)
        if result.returncode != 0:
            config_error(f"unresolvable ref {ref!r}: {result.stderr.strip()} "
                         "Pass a resolvable commit ref in --range.")
    return bool(endpoints and endpoints[0] == "..")


def range_divergence_warning(range_spec):
    """Advisory only: a two-dot diff may reverse changes unique to its base.

    git_out returns an empty string on failure. Every ancestry command here
    must return non-empty output before we can report known divergence.
    """
    endpoints = range_endpoints(range_spec)
    if not endpoints or endpoints[0] != "..":
        return ""
    _separator, left, right = endpoints
    alternative = (f"{left}...{right} would review only changes from the "
                   f"merge-base to {right}. The requested two-dot diff is "
                   f"unchanged.")
    unknown = ("divergence is UNKNOWN (Git could not establish ancestry or "
               "the base-only commit count). " + alternative)
    left_commit = git_out("rev-parse", "--verify", f"{left}^{{commit}}").strip()
    right_commit = git_out("rev-parse", "--verify", f"{right}^{{commit}}").strip()
    if not left_commit or not right_commit:
        return unknown
    base = git_out("merge-base", left_commit, right_commit).strip()
    if not base:
        return unknown
    if base == left_commit:
        return ""
    count = git_out("rev-list", "--count",
                    f"{right_commit}..{left_commit}").strip()
    if not re.fullmatch(r"[0-9]+", count) or int(count) < 1:
        return unknown
    noun, verb = ("commit", "is") if int(count) == 1 else ("commits", "are")
    return (f"{count} {noun} on {left} {verb} NOT in {right}; base-only "
            f"changes can appear as deletions. " + alternative)


def gather(args):
    parts, label = [], ""
    if args.diff:
        label = "working tree vs HEAD"
        parts.append(git_out("diff", "HEAD"))
    elif args.staged:
        label = "staged changes"
        parts.append(git_out("diff", "--cached"))
    elif args.range:
        label = f"commit range {args.range}"
        two_dot = validate_range_endpoints(args.range)
        parts.append(git_out("diff", args.range))
        warning = range_divergence_warning(args.range) if two_dot else ""
        if warning:
            print(f"WARNING: two-dot range {args.range} -- {warning}",
                  file=sys.stderr)
            label += f" -- {warning}"
    for f in args.file:
        p = Path(f)
        if not p.exists():
            config_error(f"no such file: {f}")
        text, err = read_text_bounded(p)
        if err:
            config_error(f"cannot read {f}: {err}")
        parts.append(f"--- FILE: {f} ---\n{text}")
        label = label or "explicit files"
    body = "\n\n".join(x for x in parts if x.strip())
    return body, (label or "nothing")


def finding_digest(location, summary):
    """Stable key for a prior finding's human-visible identity."""
    identity = json.dumps([location, summary], ensure_ascii=False,
                          separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def reject_duplicate_keys(pairs):
    """JSON object hook that refuses data lost by duplicate-key overwrite."""
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate object key {key!r}")
        obj[key] = value
    return obj


def load_dispositions(path):
    """Read and validate a digest-keyed prior-finding disposition map."""
    p = Path(path)
    if not p.exists():
        config_error(f"no dispositions file at {path}. Pass an existing JSON "
                     f"file to --dispositions.")
    raw, err = read_text_bounded(p)
    if err:
        config_error(f"cannot read dispositions file {path}: {err}. Pass a "
                     f"readable regular JSON file to --dispositions.")
    try:
        data = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as e:
        config_error(f"invalid JSON in dispositions file {path}: {e}. Pass a "
                     f"JSON object keyed by finding digest.")
    if not isinstance(data, dict):
        config_error("--dispositions must contain a JSON object keyed by "
                     "finding digest. Pass an object, not a list or scalar.")

    entries = []
    for digest, entry in data.items():
        if not isinstance(digest, str) or not isinstance(entry, dict):
            config_error("each --dispositions key must be a digest and each "
                         "value must be an object. Pass digest: {...} entries.")
        location = entry.get("location")
        summary = entry.get("summary")
        disposition = entry.get("disposition")
        reason = entry.get("reason")
        if not isinstance(location, str) or not location.strip():
            config_error(f"disposition {digest!r} needs a non-empty location. "
                         f"Pass the prior finding's file:line location.")
        if not isinstance(summary, str) or not summary.strip():
            config_error(f"disposition {digest!r} needs a non-empty summary. "
                         f"Pass the prior confirmed finding's summary.")
        try:
            expected = finding_digest(location, summary)
        except UnicodeEncodeError:
            config_error(
                f"disposition {digest!r} has a location or summary that is "
                f"not valid UTF-8 text. Pass text without lone surrogates.")
        if digest != expected:
            config_error(
                f"disposition key {digest!r} does not match the SHA-256 digest "
                f"of its location and summary (expected {expected}). Recompute "
                f"the key with finding_digest(location, summary), then Pass "
                f"that digest as the map key.")
        if not isinstance(disposition, str) or disposition not in DISPOSITIONS:
            config_error(
                f"disposition {digest} must be reproduced, not-reproduced, or "
                f"deferred, got {disposition!r}. Pass one of those values.")
        if (not isinstance(reason, str) or not reason.strip()
                or reason.splitlines() != [reason]):
            config_error(f"disposition {digest} needs a non-empty one-line "
                         f"reason. Pass the reason without a line break.")
        try:
            reason.encode("utf-8")
        except UnicodeEncodeError:
            config_error(f"disposition {digest!r} has a reason that is not "
                         f"valid UTF-8 text. Pass text without lone surrogates.")
        entries.append(entry)
    return entries


def has_honest_run_claim(claims):
    """The required assertion may vary only in case and a terminal period."""
    allowed = {HONEST_RUN_CLAIM.casefold(),
               HONEST_RUN_CLAIM.rstrip(".").casefold()}
    return any(isinstance(claim, str)
               and claim.strip().casefold() in allowed
               for claim in claims)


def build_prompt(body, claims, truncated, context, threat_model=None,
                 dispositions=None):
    out = []
    if context:
        out.append(f"CONTEXT\n{context}\n")
    if threat_model:
        out.append(f"THREAT MODEL — what this code does and does not defend "
                   f"against:\n{threat_model}\n")
    disputed = [entry for entry in (dispositions or [])
                if entry["disposition"] == "not-reproduced"]
    if disputed:
        out.append("PRIOR CONFIRMED FINDINGS REPORTED AS NOT REPRODUCED — "
                   "re-evaluate these disagreements; do not silently filter "
                   "them:")
        for entry in disputed:
            out.append(f"  - {entry['location']}: {entry['summary']}")
            out.append(f"    reason: {entry['reason']}")
        out.append("")
    if claims:
        out.append("CLAIMS ASSERTED ABOUT THIS WORK — assess each one:")
        for c in claims:
            out.append(f"  - {c}")
        out.append("")
    else:
        out.append("No explicit claims were asserted; review the change itself.\n")
    if truncated:
        out.append(f"NOTE: input truncated to {MAX_CHARS} chars; you are not "
                   f"seeing the whole change.\n")
    out.append("CODE UNDER REVIEW:\n")
    out.append(body)
    return "\n".join(out)


# --- reviewers --------------------------------------------------------------

def config_error(msg):
    """Configuration problems exit REVIEW_ERROR (4), never REVIEW_FAIL (1):
    a missing config is not a failed review, and must not read as one."""
    print(f"error: {msg}", file=sys.stderr)
    print("REVIEW_ERROR — configuration problem; no review took place.",
          file=sys.stderr)
    sys.exit(STATES["REVIEW_ERROR"])


def load_reviewers():
    if not CONFIG.exists():
        config_error(f"no reviewer config at {CONFIG}")
    try:
        raw, rerr = read_text_bounded(CONFIG)
        if rerr:
            config_error(f"unreadable reviewer config {CONFIG}: {rerr}")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        config_error(f"unreadable reviewer config {CONFIG}: {e}")
    global DEFAULT_PROFILE
    if isinstance(data, dict) and isinstance(data.get("default_profile"), str):
        DEFAULT_PROFILE = data["default_profile"]
    revs = data.get("reviewers") if isinstance(data, dict) else None
    if not isinstance(revs, list) or not revs:
        config_error(f"{CONFIG} has no 'reviewers' list")
    for r in revs:
        if not isinstance(r, dict):
            config_error(f"reviewer entry is not an object: {r!r}. Each entry "
                         f"in reviewers.json must be a JSON object with at "
                         f"least name, provider and model. Fix that entry.")
        for field in ("name", "provider", "model"):
            val = r.get(field)
            if not isinstance(val, str) or not val.strip():
                config_error(f"reviewer '{field}' must be a non-empty string, "
                             f"got {val!r}")
        if r.get("effort") is not None and not isinstance(r["effort"], str):
            config_error(f"reviewer 'effort' must be a string or null, got "
                         f"{r['effort']!r}")
        if r.get("profiles") is not None and (
                not isinstance(r["profiles"], list)
                or not all(isinstance(x, str) for x in r["profiles"])):
            config_error(f"reviewer 'profiles' must be a list of strings, got "
                         f"{r['profiles']!r}")
    return revs


def availability(rev):
    """Why a reviewer cannot run, or None if it can."""
    if not rev.get("enabled", True):
        return "disabled in reviewers.json"
    env = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
    var = env.get(rev["provider"])
    if var and not os.environ.get(var):
        return f"{var} not set"
    if rev["provider"] not in PROVIDERS:
        return f"unknown provider {rev['provider']!r}"
    return None


def probe_liveness(rev, timeout=30):
    """Actually ASK the provider whether this reviewer can run. None if it can.

    `availability()` only checks that the key VARIABLE is set, so `--list`
    printed "ready" for reviewers whose account had no credits left and the
    real call then failed with HTTP 429. The skill claimed availability was
    "resolved live, never asserted"; it was asserted. One tiny request per
    reviewer makes the claim true, and it only runs for `--list`.

    The reply is discarded: this asks whether the provider ANSWERS, not whether
    it answers well.
    """
    why = availability(rev)
    if why:
        return why
    try:
        _text, err = PROVIDERS[rev["provider"]](rev, "ping", timeout)
    except Exception as e:                      # never let --list traceback
        return redact(f"{type(e).__name__}: {e}")
    if err:
        return redact(_short_error(err))
    return None


def _short_error(err):
    """One readable line from a provider error, whose body is usually JSON.

    Taking the first line gave "HTTP 429: {" -- technically the first line,
    and useless. The message field is the part a human needs."""
    s = str(err).strip()
    prefix = s.split(":", 1)[0][:40] if ":" in s[:20] else ""
    brace = s.find("{")
    if brace != -1:
        try:
            body = json.loads(s[brace:])
            msg = body.get("error")
            if isinstance(msg, dict):
                msg = msg.get("message")
            if isinstance(msg, str) and msg.strip():
                out = f"{prefix}: {msg.strip()}" if prefix else msg.strip()
                return out[:150]
        except (ValueError, TypeError):
            pass
    return " ".join(s.split())[:150]


def run_one(rev, prompt, timeout, require_claims=0, asserted=None):
    """Never raises: one malformed provider response must degrade that reviewer,
    not take the whole panel down with a traceback."""
    try:
        return _run_one(rev, prompt, timeout, require_claims, asserted)
    except Exception as e:
        return {"name": rev.get("name", "?"), "ok": False,
                "error": redact(f"reviewer crashed: {type(e).__name__}: {e}"),
                "elapsed_s": 0}


RETRY_NUDGE = ("\n\nIMPORTANT: your previous reply could not be used: {why}\n"
               "Reply with ONLY a single valid JSON object matching the schema "
               "exactly. No prose, no code fences, no trailing commas. Include "
               "both the \"findings\" and \"claims\" arrays, with one claims "
               "entry per asserted claim carrying its \"claim_index\" and a "
               "\"status\" of supported, refuted, or unverifiable.")


def _run_one(rev, prompt, timeout, require_claims=0, asserted=None):
    t0 = time.time()
    # One reviewer never gets more than 2x its timeout in total, however many
    # transport retries or schema retries occur inside that budget.
    deadline = t0 + max(30, timeout * 2)
    attempt_prompt, last_why = prompt, None
    # Two attempts: a malformed reply is retried once with an explicit nudge,
    # so a formatting slip does not drop a reviewer and shrink the quorum.
    for attempt in (1, 2):
        if time.time() >= deadline:
            return {"name": rev["name"], "ok": False,
                    "error": redact(f"{last_why or 'no verdict'} "
                                    f"(reviewer budget of "
                                    f"{int(deadline - t0)}s exhausted)"),
                    "elapsed_s": round(time.time() - t0, 1)}
        result, err = PROVIDERS[rev["provider"]](rev, attempt_prompt, timeout,
                                                deadline=deadline)
        elapsed = round(time.time() - t0, 1)
        if err:
            return {"name": rev["name"], "ok": False, "error": err,
                    "incomplete": str(err).lower().startswith("no content"),
                    "elapsed_s": elapsed}
        verdict, perr = parse_verdict(result["text"])
        why = perr
        if not why:
            why = verdict_schema_error(verdict, require_claims, asserted)
            kind = "invalid verdict schema"
        else:
            kind = "unparseable verdict"
        if not why:
            break
        last_why = f"{kind}: {why}"
        if attempt == 2:
            return {"name": rev["name"], "ok": False,
                    "error": redact(f"{last_why} (after a retry)"),
                    "elapsed_s": elapsed}
        attempt_prompt = prompt + RETRY_NUDGE.format(why=why)
    # Scrub at ingestion. Redacting the serialized report was not enough: a key
    # containing a quote is JSON-escaped on the way out and stops matching.
    return {"name": rev["name"], "ok": True, "elapsed_s": elapsed,
            "retried": last_why is not None,
            "model": rev["model"], "effort": rev.get("effort"),
            "in_tokens": result.get("in_tokens"),
            "out_tokens": result.get("out_tokens"),
            "verdict": verdict.get("verdict"),
            "findings": deep_redact(verdict.get("findings", []) or []),
            "claims": deep_redact(verdict.get("claims", []) or []),
            "notes": redact(verdict.get("notes", ""))}


# --- commands ---------------------------------------------------------------

def cmd_list(reviewers, probe=True):
    """Reviewer table. STATUS is MEASURED by default, not inferred from whether
    a key variable happens to be set: that printed "ready" for an account with
    no credits left, and the real call then failed with HTTP 429. `--no-probe`
    skips the calls and says so."""
    print(f"{'REVIEWER':<20} {'PROVIDER':<12} {'MODEL':<30} {'EFFORT':<8} STATUS")
    ready = 0
    for rev in reviewers:
        why = probe_liveness(rev) if probe else availability(rev)
        if why is None:
            ready += 1
            status = "ready" if probe else "key set (UNVERIFIED)"
        else:
            status = f"UNAVAILABLE ({why})"
        print(f"{rev['name']:<20} {rev['provider']:<12} {rev['model']:<30} "
              f"{str(rev.get('effort') or '-'):<8} {status}")
    print(f"\n{ready} of {len(reviewers)} reviewers "
          + ("answered a live probe" if probe
             else "have a key set -- NOT probed, so this is not availability"))
    if ready == 0:
        print("No reviewer can run. The gate will report REVIEW_UNAVAILABLE, "
              "which is not a pass.")
    return 0 if ready else STATES["REVIEW_UNAVAILABLE"]


class _ArgParser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, colliding with REVIEW_UNAVAILABLE.
    A malformed command line is a configuration problem: REVIEW_ERROR (4)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        print("REVIEW_ERROR — usage problem; no review took place.",
              file=sys.stderr)
        sys.exit(STATES["REVIEW_ERROR"])


LADDER = ["fast", "standard", "deep"]


def changed_paths(args):
    """Collect paths for the same sources as gather; uncertainty costs a tier.

    NUL delimiters preserve whitespace in names; --no-relative retains the
    repository prefix even from a subdirectory with diff.relative enabled.
    Disabling rename detection
    includes both the removed and added path, so renaming code to docs cannot
    hide the code side. Range syntax remains Git's, not another parser here.
    """
    paths = set()
    revision = (["HEAD"] if args.diff else ["--cached"] if args.staged
                else [args.range] if args.range else None)
    if revision is not None:
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "-z", "--no-relative", "--no-renames",
                 "--no-ext-diff", "--no-textconv", *revision, "--"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            if result.returncode:
                return None, "Git could not determine changed paths"
            raw = result.stdout.decode("utf-8")
            if raw and not raw.endswith("\0"):
                return None, "Git returned an incomplete changed-path set"
            paths.update(raw[:-1].split("\0") if raw else [])
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            return None, "Git changed-path lookup was unavailable"
    if args.file:
        top = git_out("rev-parse", "--show-toplevel").rstrip("\n")
        root = Path(top) if top else Path.cwd()
        for value in args.file:
            path = Path(value)
            if ".." in path.parts:
                return None, "a --file path contains unresolved parent components"
            absolute = path if path.is_absolute() else Path.cwd() / path
            try:
                lexical = absolute.relative_to(root)
                target = absolute.resolve(strict=True).relative_to(root.resolve())
                # Keep BOTH identities: a symlink must not hide either an
                # executable lexical directory or an executable target path.
                paths.update((lexical.as_posix(), target.as_posix()))
            except (OSError, RuntimeError, ValueError):
                return None, "a --file path or target is outside the review root or unresolved"
    return paths, ""


def profile_for_paths(paths, unknown_reason=""):
    """Only a known, nonempty, wholly documentary path set earns fast."""
    if paths is None:
        return "standard", unknown_reason or "changed paths could not be determined"
    if not paths:
        return "standard", "no changed paths could be determined"
    for path in paths:
        parts = path.split("/")
        # Executable/gate locations take precedence over the .md convention.
        if (parts[-1] == "reviewers.json"
                or any(p in ("scripts", "lib", "bin", "tests") for p in parts[:-1])
                or not (path.endswith(".md")
                        or path.startswith(("docs/", "examples/")))):
            return "standard", "at least one changed path is not documentation"
    return "fast", f"all {len(paths)} changed paths are documentation"


def select_profile(args):
    if args.profile is not None:
        return args.profile, ("plan review" if args.kind == "plan"
                              else "explicit --profile")
    if args.list:
        return DEFAULT_PROFILE, "configured default for --list"
    return profile_for_paths(*changed_paths(args))


def escalation_tiers(args):
    # Direct callers predating automatic selection retain the original ladder.
    start = getattr(args, "selected_profile", "fast")
    if start not in LADDER:
        config_error("--escalate requires an implementation tier: "
                     "Pass --profile fast, standard, or deep.")
    return LADDER[LADDER.index(start):]


def author_argument(value):
    """Keep the complete model ID after the first provider separator."""
    provider, separator, model = value.partition("/")
    if (not separator or not provider or not model
            or any(c.isspace() for c in value)):
        raise argparse.ArgumentTypeError(
            "author must be PROVIDER/MODEL without whitespace; "
            "pass e.g. codex/gpt-5.6-sol")
    return value


def author_model_ids(authors, roster=(), legacy=False):
    """Match model IDs exactly, independently of the author's transport.

    Codex authors and OpenAI API reviewers can use the same model. Split only
    the outer provider prefix: openrouter/moonshotai/kimi-k2.7-code retains
    moonshotai/kimi-k2.7-code. Only committee's older bare declarations accept
    configured seat aliases (case-insensitive), never model substrings.
    """
    models = set()
    for author in authors:
        if legacy and author in {r["model"] for r in roster}:
            models.add(author)
        elif legacy and "/" not in author:
            aliases = {r["model"] for r in roster
                       if r["name"].casefold() == author.casefold()}
            models.update(aliases or {author})
        else:
            models.add(author.partition("/")[2])
    return models


def exclude_authors(reviewers, models):
    """One exact-model exclusion rule shared with committee membership."""
    kept, excluded = [], []
    for reviewer in reviewers:
        if reviewer["model"] in models:
            excluded.append({"name": reviewer["name"],
                             "model": reviewer["model"],
                             "reason": "authored this change"})
        else:
            kept.append(reviewer)
    return kept, excluded


def in_profile(rev, profile):
    """THE eligibility rule. Both selection paths must call this one.

    An UNDECLARED reviewer is in NO profile. It used to be in every profile,
    so adding an entry without a `profiles` key silently put a third model on
    the two-model plan panel. That was fixed once, in the normal selection
    path, and NOT in escalate(), which kept
    `not r.get("profiles") or tier in r["profiles"]`: the exact inclusive
    spelling, still live on the path every escalated review takes. One fix,
    two call sites, one of them missed, which is this repo's most repeated
    defect.

    An EMPTY list is treated the same as a missing key. "profiles": [] is a
    reviewer who declared membership in nothing, and reading that as
    membership in everything is the same inversion.
    """
    return profile in (rev.get("profiles") or [])


def implementation_panel_policy(args, roster, selected):
    """Validate the declared cycle, without turning audit history into authority.

    The journal has no change identity or historical profile membership. A
    fresh cycle therefore declares its predecessor profile explicitly, just
    as --round is caller-declared. Count enabled, distinct reviewers from the
    current routing config, never only those whose credentials are available.
    """
    override = args.allow_single_reviewer
    if override is not None and (
            not override.strip() or override.splitlines() != [override]):
        config_error("--allow-single-reviewer needs a non-empty one-line "
                     "reason. Pass the reason for the exceptional quorum.")
    if args.kind != "implementation":
        if override is not None or args.fresh_cycle_from is not None:
            config_error("--allow-single-reviewer and --fresh-cycle-from are "
                         "implementation-only. Drop them for a plan review.")
        return {"single_reviewer_override": None, "fresh_cycle": None}
    if override is not None and args.quorum != 1:
        config_error("--allow-single-reviewer is only for --quorum 1. Drop "
                     "the override when requesting a larger quorum.")

    fresh = None
    if args.fresh_cycle_from is not None:
        previous = {r["name"] for r in roster
                    if r.get("enabled", True)
                    and in_profile(r, args.fresh_cycle_from)}
        if not previous:
            config_error("the replaced profile has no enabled reviewers. "
                         "Pass --fresh-cycle-from with a populated profile.")
        floor = max(2, len(previous))
        selected_names = [r["name"] for r in selected if r.get("enabled", True)
                          and (not args.escalate
                               or any(in_profile(r, tier)
                                      for tier in escalation_tiers(args)))]
        panel = set(selected_names)
        if len(panel) != len(selected_names):
            config_error("a fresh cycle cannot count duplicate reviewer "
                         "names as independent verdicts. Pass a routing "
                         "configuration with distinct enabled reviewer names.")
        if len(panel) < floor:
            config_error(
                f"fresh cycle from {args.fresh_cycle_from} requires at least "
                f"{floor} reviewers; the selected panel has {len(panel)}. "
                "Drop --only or select a profile with enough reviewers; "
                "a single-reviewer override cannot lower this floor.")
        fresh = {"replaces_profile": args.fresh_cycle_from,
                 "profile_reviewers": sorted(previous),
                 "minimum_reviewers": floor,
                 "provenance": "caller-declared; current enabled profile"}

    if args.quorum == 1 and override is None:
        config_error(
            "an implementation review needs at least two verdicts. Pass "
            "--quorum 2 or, for an explicit exception, "
            "--allow-single-reviewer REASON; the override will be recorded.")
    # Use the same floor in decide_state and the ladder's early-stop test.
    # Availability cannot silently shrink a declared replacement panel.
    if fresh is not None:
        args.quorum = max(args.quorum, fresh["minimum_reviewers"])
    return {"single_reviewer_override": override, "fresh_cycle": fresh}


def panel_policy_text(policy):
    """Provenance belongs on the verdict line, including non-pass outcomes."""
    parts = []
    fresh = policy["fresh_cycle"]
    if fresh is not None:
        parts.append(f"FRESH_CYCLE from {fresh['replaces_profile']}; "
                     f"minimum {fresh['minimum_reviewers']} reviewers; "
                     f"{fresh['provenance']}")
    if policy["single_reviewer_override"] is not None:
        parts.append("SINGLE_REVIEWER_OVERRIDE: "
                     + redact(policy["single_reviewer_override"]))
    return "" if not parts else " — " + "; ".join(parts)


def escalate(all_reviewers, prompt, args, truncated, label, body_len):
    """Cheapest-first with early exit.

    Running the full panel every time means paying for the slowest, dearest
    reviewer to re-find what a cheap one already caught. Each tier adds only
    the reviewers the previous tier did not run; the first failing tier with
    quorum ends it, including a claims-only REVIEW_CLAIMS_REFUTED result.
    """
    seen, completed, failed, unavailable = set(), [], [], []
    tiers_run = []
    tiers = escalation_tiers(args)
    # Findings ACCUMULATE across tiers. Computed per tier, a finding from an
    # earlier tier was forgotten once a later tier came back clean with quorum
    # met, so the ladder fell through to the dearest reviewer with a defect
    # already on the table (glm-5.1, MAJOR).
    # An escalation over an empty ladder used to run zero reviewers and
    # return four empty lists, which reads downstream as "nobody found
    # anything". Tightening in_profile() makes that reachable: a roster whose
    # entries all lack a `profiles` key now matches no tier at all. Refuse it
    # here, where the cause is still visible.
    if not any(in_profile(r, tier) for tier in tiers for r in all_reviewers):
        config_error(
            "no reviewer is in any escalation tier "
            f"({', '.join(tiers)}). A reviewer with no 'profiles' key, or an "
            f"empty one, is in NO profile, so --escalate would run nobody "
            f"and report no findings, which is indistinguishable from a "
            f"clean review. Name one or more tiers in that reviewer's "
            f"'profiles' in reviewers.json, or pass --only to select "
            f"reviewers directly.")

    bad_so_far = False
    for tier in tiers:
        panel = [r for r in all_reviewers
                 if in_profile(r, tier) and r["name"] not in seen]
        runnable, tier_unavail = [], []
        for rev in panel:
            why = availability(rev)
            (tier_unavail if why else runnable).append(
                {"name": rev["name"], "reason": why} if why else rev)
        unavailable.extend(tier_unavail)
        for r in panel:
            seen.add(r["name"])
        if not runnable:
            continue
        tiers_run.append(tier)
        if not args.json:
            names = ", ".join(r["name"] for r in runnable)
            print(f"\ntier {tier}: {names}")
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(runnable)) as ex:
            res = list(ex.map(
                lambda r: run_one(r, prompt, args.timeout, len(args.claim),
                                  args.claim),
                runnable))
        tier_completed = [r for r in res if r["ok"]]
        completed.extend(tier_completed)
        failed.extend(r for r in res if not r["ok"])
        if not args.json:
            for r in res:
                if r["ok"]:
                    print(f"  {r['name']:<18} {str(r['verdict']).upper():<9} "
                          f"{len(r['findings'])} finding(s)  {r['elapsed_s']}s")
                else:
                    print(f"  {r['name']:<18} ERROR     {r['error'][:70]}")
        # A confirmed defect or refuted claim ends the ladder -- but only once
        # QUORUM is met. Checked before the quorum test, one reviewer's finding
        # ended the ladder on its own: four of five reviews in one session were
        # decided by a single model, and the committee never formed. A finding
        # from one reviewer is a hypothesis; adjudicating it is what a panel is
        # for, and stopping also leaves every other claim unexamined by anyone
        # else. The round that did reach quorum is the one where the second and
        # third readers found what the first had upheld.
        bad_so_far = bad_so_far \
            or any(is_confirmed(f) for r in tier_completed for f in r["findings"]) \
            or any(norm(c.get("status")) == "refuted"
                   for r in tier_completed for c in r["claims"])
        bad = bad_so_far
        if bad and len(completed) >= args.quorum:
            if not args.json:
                print(f"  -> tier {tier} found problems with "
                      f"{len(completed)} verdict(s) (quorum {args.quorum}); "
                      f"stopping before the dearer tiers.")
            break
        if bad:
            # Still cheapest-first: this recruits the next tier only, and the
            # ladder stops as soon as quorum is reached.
            if not args.json:
                print(f"  -> tier {tier} found problems but only "
                      f"{len(completed)} verdict(s) (quorum {args.quorum}); "
                      f"escalating to adjudicate rather than acting on one "
                      f"opinion.")
            continue
        if len(completed) < args.quorum:
            # Below quorum through reviewer errors, not findings. Escalating
            # recruits more readers; stopping would waste the run and report a
            # weaker result than the panel could actually give.
            if not args.json:
                print(f"  -> only {len(completed)} verdict(s) so far "
                      f"(quorum {args.quorum}); escalating to recruit more.")
            continue
    return completed, failed, unavailable, tiers_run


def arm_watchdog(seconds):
    """Bound total runtime. urlopen's timeout is per-read: a response that
    trickles one byte below the idle timeout forever never trips it, and the
    executor waits indefinitely without emitting a verdict."""
    if not hasattr(signal, "SIGALRM") or seconds <= 0:
        return

    def _fire(_s, _f):
        print(f"REVIEW_UNAVAILABLE — exceeded the {seconds}s total watchdog "
              f"before any verdict; no review took place.", file=sys.stderr)
        os._exit(STATES["REVIEW_UNAVAILABLE"])

    try:
        signal.signal(signal.SIGALRM, _fire)
        signal.alarm(max(1, min(int(seconds), 86_400)))
    except (OSError, ValueError, OverflowError, TypeError):
        pass


def disarm_watchdog():
    """A non-gating audit write cannot replace an already-known verdict."""
    if hasattr(signal, "SIGALRM"):
        try:
            signal.alarm(0)
        except (OSError, ValueError, OverflowError, TypeError):
            pass


def decide_state(n_completed, n_failed, confirmed, refuted_claims,
                 rejecting, truncated, quorum, n_out_of_scope_critical=0,
                 n_incomplete=0):
    """The gate's verdict. Extracted from main() so it can be tested directly:
    inline, the failed-reviewer hole below was invisible to every test."""
    if n_completed == 0:
        return "REVIEW_INCOMPLETE" if n_incomplete else "REVIEW_UNAVAILABLE"
    # Quorum gates the FAIL as well as the PASS. It used to sit below the
    # finding check, so one completed reviewer could deliver a verdict that the
    # panel never reached: found live when a plan review returned REVIEW_FAIL
    # on a single verdict because the second reviewer returned an empty
    # response. The same asymmetry was fixed in escalate() and missed here --
    # one rule, two places, the defect class this repo has hit 19 times.
    #
    # The finding is NOT discarded: it is still printed, and REVIEW_PARTIAL is
    # not a pass (the caller is told never to treat it as one). What changes is
    # that one opinion is not dignified as a committee verdict.
    if n_completed >= quorum:
        if confirmed:
            return "REVIEW_FAIL"
        if refuted_claims:
            return "REVIEW_CLAIMS_REFUTED"
    if n_incomplete:
        return "REVIEW_INCOMPLETE"
    if n_completed < quorum:
        return "REVIEW_PARTIAL"
    if n_failed:
        # A reviewer that errored, timed out, or returned unparseable output
        # produced no verdict. Quorum among the others does not speak for it,
        # and this gate's premise is that absent evidence is not a pass (luna).
        # `unavailable` differs: it never ran at all, and quorum covers that.
        return "REVIEW_PARTIAL"
    if n_out_of_scope_critical:
        # `in_scope: false` is a reviewer's judgment about MY threat model, and
        # a reviewer that misreads it could dismiss a real defect with one word
        # (deepseek). So it can demote a critical finding out of the verdict,
        # but it cannot buy a clean pass: a human reads it and decides.
        return "REVIEW_PARTIAL"
    if rejecting:
        # Rejected, but with nothing that met the confirmation filter. Neither a
        # clean pass nor an actionable failure: a human has to read their notes.
        return "REVIEW_PARTIAL"
    if truncated:
        # Part of the change was never sent. Upholding what was visible says
        # nothing about the rest, so this must not read as a pass.
        return "REVIEW_PARTIAL"
    return "REVIEW_PASS"


def main():
    ap = _ArgParser(prog="review.py", description=__doc__,
                    formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--diff", action="store_true", help="working tree vs HEAD")
    src.add_argument("--staged", action="store_true", help="staged changes")
    src.add_argument("--range", help="commit range, e.g. HEAD~3..HEAD")
    ap.add_argument("--file", action="append", default=[], help="whole file; repeatable")
    ap.add_argument("--claim", action="append", default=[],
                    help="a claim to be checked against the code; repeatable")
    ap.add_argument("--context", default="", help="what this change is for")
    ap.add_argument("--no-probe", action="store_true",
                    help="with --list, skip the live probe and report only "
                         "whether a key is set (which is not availability)")
    ap.add_argument("--threat-model", default=None,
                    help="what the code does and does not defend against; a "
                         "finding whose preconditions fall outside it is "
                         "reported but does not decide the verdict")
    ap.add_argument("--quorum", type=int, default=2,
                    help="reviewers that must complete for a verdict (default 2)")
    ap.add_argument("--allow-single-reviewer", metavar="REASON",
                    help="explicit implementation quorum-1 exception; reason "
                         "is recorded in the verdict and audit journal")
    ap.add_argument("--fresh-cycle-from", choices=LADDER, metavar="PROFILE",
                    help="declare the profile replaced after an exhausted "
                         "cycle; repeat on each round of the fresh cycle. "
                         "Its enabled panel size floors selection and quorum")
    ap.add_argument("--timeout", type=int, default=1200,
                    help="per reviewer seconds (default 1200: at 64000 output "
                         "tokens a reasoning model can still be streaming at "
                         "600s, and a cut-off reviewer is REVIEW_PARTIAL, not "
                         "a pass)")
    ap.add_argument("--only", action="append", default=[],
                    help="run only these reviewers; repeatable, and also "
                         "accepts a comma-separated list")
    ap.add_argument("--author", action="append", default=[],
                    type=author_argument, metavar="PROVIDER/MODEL",
                    help="exclude this author's exact model ID from every "
                         "selected panel; repeatable; never lowers quorum")
    ap.add_argument("--profile", default=None,
                    choices=["plan", "fast", "standard", "deep"],
                    help="reviewer panel (default: fast for documentation-only "
                         "paths, standard otherwise or when paths are unknown). "
                         "--only overrides it.")
    ap.add_argument("--kind", choices=["plan", "implementation"], default=None,
                    help="what is being reviewed. REQUIRED for a real review: "
                         "a plan review and an implementation review have "
                         "different panels and different rules, and leaving it "
                         "implicit is how a design proposal got reviewed by the "
                         "implementation panel.")
    ap.add_argument("--watchdog", type=int, default=None,
                    help="hard bound on total runtime in seconds; exits "
                         "REVIEW_UNAVAILABLE if exceeded (default: 3x the "
                         "per-reviewer timeout, minimum 1800)")
    ap.add_argument("--escalate", action="store_true",
                    help="run tiers from the selected profile up to deep, "
                         "stopping at the first failure. Each tier adds only "
                         "the reviewers the previous tier did not run, so a "
                         "failure costs one cheap tier instead of the whole "
                         "panel. Strongly preferred over --profile deep.")
    ap.add_argument("--plan", action="store_true",
                    help="plan review: TWO contrasting models, never "
                         "escalated. For acceptance criteria and designs, "
                         "before code exists. A third reviewer adds agreement, "
                         "not insight.")
    ap.add_argument("--round", type=int, default=None, metavar="N",
                    help="which round this is for the change under review. "
                         "Past MAX_ROUNDS the gate refuses: more rounds on one "
                         "change means the framing is wrong, not the code.")
    ap.add_argument("--dispositions", metavar="FILE",
                    help="round 2 and later: digest-keyed JSON mapping every "
                         "prior confirmed finding to reproduced, "
                         "not-reproduced, or deferred with a one-line reason")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true", help="show reviewers and availability")
    args = ap.parse_args()
    # A total bound on top of every per-reviewer timeout.
    arm_watchdog(args.watchdog if args.watchdog is not None
                 else max(1800, args.timeout * 3))

    reviewers = load_reviewers()
    roster = reviewers
    # --- the protocol, enforced rather than remembered ---------------------
    # Every rule below was already written down, and drifted from anyway,
    # because prose in a memory file is not a constraint. See PROTOCOL.md.
    # --plan is an alias for --kind plan; --kind is what the rules key on, so
    # `--profile plan` cannot set the panel while leaving the rules off.
    if args.plan:
        if args.kind and args.kind != "plan":
            config_error(f"--plan and --kind {args.kind} disagree. Pass one.")
        args.kind = "plan"
    if args.profile == "plan" and args.kind is None:
        args.kind = "plan"
    if not args.list and args.kind is None:
        config_error(
            "--kind is required: pass `--kind plan` for acceptance criteria or "
            "a design (two contrasting models, never escalated), or "
            "`--kind implementation --round N` for code. Leaving it implicit "
            "is how a design proposal got reviewed by the four-model "
            "implementation panel.")
    if args.kind == "implementation" and args.round is None and not args.list:
        config_error(
            f"--kind implementation requires --round N (1..{MAX_ROUNDS}), so "
            f"the bound on rounds per change can be applied. Pass --round 1 if "
            f"this is the first round for this change.")
    if (args.kind == "implementation" and not args.list
            and not has_honest_run_claim(args.claim)):
        config_error(
            f"every implementation review must assert the required "
            f"counter-claim: {HONEST_RUN_CLAIM!r} Pass it with --claim so the "
            f"gate checks that honest work still succeeds.")
    if (args.kind == "implementation" and args.round is not None
            and args.round >= 2 and not args.list and not args.dispositions):
        config_error(
            "implementation round 2 and later requires --dispositions FILE "
            "mapping every prior confirmed finding to reproduced, "
            "not-reproduced, or deferred with a one-line reason. Pass the "
            "digest-keyed JSON file from the prior round.")
    if args.kind == "plan":
        if args.escalate:
            config_error("--plan and --escalate are contradictory: a plan "
                         "review is TWO contrasting models and stops there. "
                         "Drop --escalate, or drop --plan if this is an "
                         "implementation.")
        if args.profile and args.profile != "plan":
            config_error(f"a plan review uses the 'plan' panel; remove "
                         f"--profile {args.profile}.")
        args.profile = "plan"
        if args.quorum > 2:
            config_error(f"--plan runs two reviewers, so quorum {args.quorum} "
                         f"can never be met. Pass --quorum 2, or drop --plan.")
    if args.round is not None:
        if args.round < 1:
            config_error(f"--round must be 1 or more, got {args.round}.")
        if args.round > MAX_ROUNDS:
            config_error(
                f"round {args.round} exceeds the bound of {MAX_ROUNDS} for one "
                f"change. Past this, more rounds have not converged -- they "
                f"have been finding defects in the previous round's fixes. "
                f"Step back to root cause: state what keeps recurring, fix "
                f"THAT, declare new acceptance criteria, and start again at "
                f"--round 1. Override only if you have done that and the "
                f"change is genuinely new.")
    if not (args.diff or args.staged or args.range or args.file):
        args.diff = True  # same default source for selection and gathering
    profile, profile_reason = select_profile(args)
    args.selected_profile = profile
    tier_text = f"tier: {profile} ({profile_reason})"
    if not args.json:
        print(tier_text)
    if not args.only and not args.escalate:
        # An UNDECLARED reviewer is in NO profile. It used to be in every
        # profile, so adding an entry without a `profiles` key silently put a
        # third model on the two-model plan panel (sol). The residual must be
        # exclusion, not inclusion.
        reviewers = [r for r in reviewers if in_profile(r, profile)]
        if not reviewers:
            config_error(f"no reviewers in profile {profile!r}")
    # --escalate deliberately keeps the full roster: the ladder filters per
    # tier itself, and pre-filtering here made the deep tier unreachable.
    if args.only:
        # Accept both `--only a --only b` and `--only a,b`. The second form is
        # the one a person actually types, and rejecting it cost a full review
        # round to a usage error.
        wanted = [n.strip() for spec in args.only for n in spec.split(",")
                  if n.strip()]
        reviewers = [r for r in reviewers if r["name"] in wanted]
        if not reviewers:
            # Name an action, and name the available reviewers: this refusal
            # said only what was wrong. The rule "every refusal names an
            # action" had been applied to the two verifiers and never to this
            # tool, which is the sibling-miss class it exists to catch.
            have = ", ".join(sorted(r["name"] for r in load_reviewers()))
            config_error(f"no reviewer matches {wanted}. Available: {have}. "
                         f"Pass one of those, or drop --only to use the "
                         f"profile.")
    excluded = []
    if args.author:
        reviewers, excluded = exclude_authors(
            reviewers, author_model_ids(args.author))
        if not args.json:
            for removal in excluded:
                print(f"{removal['name']} excluded: {removal['reason']}")
        # Check capacity before contacting anyone, including the entire ladder
        # and any fresh-cycle floor. Author removal cannot relax either floor.
        required = max(args.quorum, 2 if args.kind == "plan" else 1)
        if args.fresh_cycle_from:
            required = max(required, 2, len({r["name"] for r in roster
                           if r.get("enabled", True)
                           and in_profile(r, args.fresh_cycle_from)}))
        remaining = sum(1 for r in reviewers if r.get("enabled", True)
                        and (not args.escalate or
                             any(in_profile(r, tier)
                                 for tier in escalation_tiers(args))))
        if excluded and remaining < required and not args.list:
            reason = (f"author exclusion leaves {remaining} eligible reviewers; "
                      f"required quorum is {required}. Select more independent "
                      "reviewers; author exclusion cannot lower quorum.")
            disarm_watchdog()
            if args.json:
                print(json.dumps({"state": "REVIEW_UNAVAILABLE",
                                  "profile": profile,
                                  "profile_reason": profile_reason,
                                  "checked_at": now(), "reason": reason,
                                  "quorum": required, "excluded": excluded,
                                  "results": []}, indent=2))
            else:
                print("REVIEW_UNAVAILABLE — " + reason)
            sys.exit(STATES["REVIEW_UNAVAILABLE"])
    # --- validate the EFFECTIVE panel -------------------------------------
    # Every earlier version of this checked the panel BEFORE selection, so
    # `--plan --only a,b,c` ran three reviewers and `--plan --only x --quorum 1`
    # ran one. A rule applied to the intended panel and not the actual one is
    # not enforcement (sol). This runs after --only, --profile and enabled
    # state, and before any reviewer is called.
    if args.kind == "plan" and not args.list:
        runnable = [r for r in reviewers if r.get("enabled", True)]
        names = ", ".join(sorted(r["name"] for r in runnable)) or "none"
        if len(runnable) != 2:
            config_error(
                f"a plan review is exactly TWO contrasting models; this panel "
                f"has {len(runnable)} ({names}). A third adds agreement, not "
                f"insight, and one is not a committee. Drop --only, or name "
                f"exactly two reviewers from different providers.")
        if len({r["provider"] for r in runnable}) != 2:
            config_error(
                f"the two plan reviewers ({names}) share a provider, so they "
                f"can share a failure mode. Contrasting means different "
                f"providers. Name one from each.")
        if args.quorum != 2:
            config_error(
                f"a plan review needs both verdicts: pass --quorum 2, not "
                f"{args.quorum}. Below that one model decides; above it, the "
                f"panel can never reach quorum.")

    if args.list:
        print(f"profile: {profile}"
              + ("  (--only overrides profile filtering)" if args.only else ""))
        sys.exit(cmd_list(reviewers, probe=not args.no_probe))

    if args.escalate and args.only:
        config_error("--escalate and --only are mutually exclusive: the "
                     "ladder picks the panel per tier, and --only fixes it. "
                     "Drop --only to let the ladder choose, or drop "
                     "--escalate to review with exactly the named reviewers.")
    if args.quorum < 1:
        config_error(f"--quorum must be at least 1, got {args.quorum}")
    args.panel_policy = implementation_panel_policy(args, roster, reviewers)
    policy_text = panel_policy_text(args.panel_policy)
    dispositions = (load_dispositions(args.dispositions)
                    if args.dispositions else [])

    body, label = gather(args)
    if not body.strip():
        print(f"nothing to review ({label} is empty)")
        sys.exit(STATES["REVIEW_ERROR"])
    truncated = len(body) > MAX_CHARS
    if truncated:
        body = body[:MAX_CHARS]
    prompt = build_prompt(body, args.claim, truncated, args.context,
                          args.threat_model, dispositions)

    runnable, unavailable = [], []
    for rev in reviewers:
        why = availability(rev)
        (unavailable if why else runnable).append(
            {"name": rev["name"], "reason": why} if why else rev)

    if not runnable:
        disarm_watchdog()
        journal = record_review_round(args, [], "REVIEW_UNAVAILABLE")
        report = {"state": "REVIEW_UNAVAILABLE", "checked_at": now(),
                  "profile": profile, "profile_reason": profile_reason,
                  "reviewed": label, "unavailable": unavailable, "results": [],
                  "excluded": excluded,
                  "panel_policy": args.panel_policy,
                  "journal": journal}
        if args.json:
            print(redact(json.dumps(report, indent=2)))
        else:
            print("REVIEW_UNAVAILABLE" + policy_text
                  + " — no reviewer could run:")
            for u in unavailable:
                print(f"  - {u['name']}: {u['reason']}")
            print("\nThis is NOT a pass. The change is unreviewed.")
        sys.exit(STATES["REVIEW_UNAVAILABLE"])

    if not args.json:
        print(f"reviewing {label} ({len(body)} chars"
              f"{', TRUNCATED' if truncated else ''}) "
              f"with {len(runnable)} reviewer(s) [profile: {profile}]...")

    tiers_run = [profile]
    if args.escalate:
        completed, failed, unavailable, tiers_run = escalate(
            reviewers, prompt, args, truncated, label, len(body))
        if not completed and not failed:
            disarm_watchdog()
            record_review_round(args, [], "REVIEW_UNAVAILABLE")
            print("REVIEW_UNAVAILABLE" + policy_text
                  + " — no reviewer could run in any tier", file=sys.stderr)
            sys.exit(STATES["REVIEW_UNAVAILABLE"])
    else:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(runnable)) as ex:
            results = list(ex.map(
                lambda r: run_one(r, prompt, args.timeout, len(args.claim),
                                  args.claim),
                runnable))
        completed = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]

    confirmed, refuted_claims, rejecting = [], [], []
    out_of_scope = []
    for r in completed:
        if norm(r.get("verdict")) == "refuted":
            rejecting.append(r["name"])
        for f in r["findings"]:
            if is_confirmed(f):
                confirmed.append({**f, "reviewer": r["name"]})
            elif f.get("in_scope") is False:
                # Never silently dropped: excluded from the verdict, still
                # shown, because the threat model can be wrong too.
                out_of_scope.append({**f, "reviewer": r["name"]})
        for c in r["claims"]:
            if norm(c.get("status")) == "refuted":
                refuted_claims.append({**c, "reviewer": r["name"]})

    oos_critical = [f for f in out_of_scope
                    if norm(f.get("severity")) == "critical"]
    incomplete = [r for r in failed if r.get("incomplete")]
    state = decide_state(n_completed=len(completed), n_failed=len(failed),
                         confirmed=confirmed, refuted_claims=refuted_claims,
                         rejecting=rejecting, truncated=truncated,
                         quorum=args.quorum,
                         n_out_of_scope_critical=len(oos_critical),
                         n_incomplete=len(incomplete))
    disarm_watchdog()
    journal = record_review_round(args, completed, state)

    report = {
        "state": state, "checked_at": now(), "reviewed": label,
        "profile": profile, "escalated": bool(args.escalate),
        "profile_reason": profile_reason,
        "excluded": excluded,
        "tiers_run": tiers_run,
        "panel_policy": args.panel_policy,
        "truncated": truncated, "quorum": args.quorum,
        "completed": len(completed), "unavailable": unavailable,
        "failed": [{"name": r["name"], "error": r["error"],
                    "incomplete": bool(r.get("incomplete"))}
                   for r in failed],
        "confirmed_findings": confirmed, "refuted_claims": refuted_claims,
        "out_of_scope_findings": out_of_scope,
        "rejecting_reviewers": rejecting,
        "journal": journal,
        "results": completed,
    }
    if state == "REVIEW_CLAIMS_REFUTED":
        report["next_action"] = (
            "No confirmed defect, but refuted claims are not a pass. "
            "Correct the claim (or the code), then re-run the gate. "
            "Do not argue a refuted claim into a pass.")

    if args.json:
        # Reviewer-authored text is untrusted and may quote a key found in the
        # reviewed source; redact the whole rendered report, not just errors.
        print(redact(json.dumps(report, indent=2)))
    else:
        print()
        if not args.escalate:
            for r in completed:
                tok = (f"{r.get('in_tokens') or '?'}in/"
                       f"{r.get('out_tokens') or '?'}out")
                print(f"  {r['name']:<18} {str(r['verdict']).upper():<9} "
                      f"{len(r['findings'])} finding(s)  {r['elapsed_s']}s  {tok}")
            for r in failed:
                print(f"  {r['name']:<18} ERROR     {r['error'][:80]}")
        else:
            spent = sum((r.get("out_tokens") or 0) for r in completed)
            print(f"  tiers run: {' -> '.join(tiers_run)}  "
                  f"({len(completed)} verdict(s), {spent} output tokens)")
        for u in unavailable:
            print(f"  {u['name']:<18} SKIPPED   {u['reason']}")

        if rejecting and not confirmed and not refuted_claims:
            print(f"\nREJECTED BY: {', '.join(rejecting)}")
            print("  (a top-level 'refuted' verdict, with no finding or claim "
                  "that met the confirmation filter — read their notes)")
            for r in completed:
                if r["name"] in rejecting and r.get("notes"):
                    print(f"  [{r['name']}] {redact(str(r['notes']))[:300]}")
        if refuted_claims:
            print("\nREFUTED CLAIMS:")
            for c in refuted_claims:
                print(redact(f"  [{c['reviewer']}] {c.get('claim','')}"))
                print(redact(f"      {c.get('why','')}"))
        if confirmed:
            print("\nCONFIRMED FINDINGS:")
            for f in confirmed:
                loc = redact(f"{f.get('file','?')}:{f.get('line','?')}")
                print(f"  [{f['reviewer']}] {f.get('severity','?').upper()} {loc}")
                print(redact(f"      {f.get('summary','')}"))
                print(redact(f"      -> {f.get('failure_scenario','')}"))

        if out_of_scope:
            print("\nOUT OF SCOPE (reported, not counted against the verdict "
                  "— the threat model excludes their preconditions):")
            for f in out_of_scope:
                loc = redact(f"{f.get('file','?')}:{f.get('line','?')}")
                print(f"  [{f['reviewer']}] {f.get('severity','?').upper()} {loc}")
                print(redact(f"      {f.get('summary','')}"))
                if f.get("preconditions"):
                    print(redact(f"      requires: {f.get('preconditions')}"))

        print(f"\n{state}{policy_text} [{tier_text}]")
        if state == "REVIEW_CLAIMS_REFUTED":
            print("  " + report["next_action"])
        elif state == "REVIEW_PARTIAL":
            if rejecting:
                print(f"  Rejected by {', '.join(rejecting)} with no finding or "
                      f"claim that met the confirmation filter. Not a pass and "
                      f"not an actionable failure — read their notes above.")
            elif truncated:
                print(f"  Input was truncated at {MAX_CHARS} chars — part of the "
                      f"change was never reviewed. Not a pass. Split it and "
                      f"re-run.")
            else:
                print(f"  Only {len(completed)} of {args.quorum} required "
                      f"reviewers completed. Not a pass — say so rather than "
                      f"implying review.")
        elif state == "REVIEW_UNAVAILABLE":
            print("  No reviewer completed. The change is unreviewed.")
        elif state == "REVIEW_INCOMPLETE":
            names = ", ".join(r["name"] for r in incomplete)
            print(f"  Required reviewer content was unusable ({names}). "
                  "This spent review budget but supplied no judgment; it is "
                  "not an implementation defect and not a pass.")
        elif state == "REVIEW_PASS":
            names = ", ".join(r["name"] for r in completed)
            print(f"  Reviewed by: {names}. Absence of a finding is not proof "
                  f"of correctness — it is {len(completed)} model(s) failing to "
                  f"refute it.")

    sys.exit(STATES[state])


if __name__ == "__main__":
    if sys.argv[1:] == [JOURNAL_CHILD_ARG]:
        try:
            child_result = _journal_child(json.load(sys.stdin))
        except Exception as child_exc:
            child_result = {
                "ok": False,
                "error": redact(
                    f"{type(child_exc).__name__}: {child_exc}"),
            }
            print(json.dumps(child_result, sort_keys=True))
            sys.exit(1)
        print(json.dumps(child_result, sort_keys=True))
        sys.exit(0)
    main()
