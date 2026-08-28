#!/usr/bin/env python3
"""result.py — regenerate and verify a figure, table, benchmark or export, with
input-to-output provenance.

Third tool in the artifact-contract family, after contract.py (Slurm workflow
outputs) and traincontract.py (training runs). Same thesis: a declared,
independently verifiable definition of "done", written BEFORE execution and
checked by something other than the process that did the work. Lifecycle state
is not completion.

    result.py declare <dir> --command CMD --output PATH... --input PATH...
    result.py build   <dir> [--double]
    result.py check   <dir> [--reference DIR]
    result.py review  <dir> --by NAME --note TEXT

STATES. Ascending numbers are NOT ascending goodness; 0 must mean "fully done"
so that shell composition works, and everything else is a state, not a score.

    0  REVIEWED             a named person accepted THESE digests
    1  VALIDATED            declared checks pass, provenance recorded
    2  GENERATED            the command ran and produced the outputs
    3  STALE                inputs changed after the build
    4  NONDETERMINISTIC     a recorded double render differed
    5  FAILED               the build command failed
    6  INCOMPLETE_EVIDENCE  cannot judge
    7  CONTRACT_DRIFTED     the build consumed inputs differing from declared

TWO GATES, not one. This is the correction that plan v8 exists for, found by a
committee reading the shipped siblings:

  Gate 1, INTEGRITY: the manifest binds each output to a digest, a contract_id
  and an attempt_id, and `check` recomputes the digest itself. That is a
  measurement, not a claim, so it is not a self-assertion. It proves integrity
  and instance binding, and NOTHING ELSE.

  Gate 2, PRODUCTION: proving the declared command WROTE the file is a separate
  claim, and v7 did not make it. `build` digests every declared output
  immediately before and after the command and records whether it appeared or
  changed in that window. Content over a bounded interval the tool itself opens,
  never a timestamp -- a timestamp supports an inference, never an attribution,
  and this repo has retired three rules for forgetting that.

SINGLE DECIDER. The dominant defect class here is not cross-tool drift, it is
intra-tool duplicated decision logic: defect #19 was two functions in ONE file
inlining the same quorum comparison, which extraction, byte symmetry and the
conformance suite all structurally cannot see. So exactly one pure function,
`evaluate()`, chooses the state and the exit code. Gates live in GATES as data,
each exactly once. Command handlers gather evidence and call evaluate(); they
never compare states or select an exit code. tests/test_result.py AST-asserts
that, with its limit stated: it catches DUPLICATION of a predicate, never a
single WRONG predicate.

Python 3.8+, stdlib only. No PDF or image library: magic bytes and structure
only, and the receipt says that is what was checked.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --- states ---------------------------------------------------------------
STATES = {
    "REVIEWED": 0,
    "VALIDATED": 1,
    "GENERATED": 2,
    "STALE": 3,
    "NONDETERMINISTIC": 4,
    "FAILED": 5,
    "INCOMPLETE_EVIDENCE": 6,
    "CONTRACT_DRIFTED": 7,
}
USAGE_ERROR = 64          # never collides with a state (states are 0..7)

CONTRACT = "result-contract.json"
MANIFEST = "result-manifest.json"
ATTEMPTS = "attempts.jsonl"
RECEIPT = "verification.json"
REVIEWS = "reviews.jsonl"

# Annotation keys: recorded, never evaluated, allowed on every criterion. ONE
# reserved name, not a naming CONVENTION -- a prefix rule was broken by
# `_lines_` within a day of being written, because lstrip("_") leaves "lines_",
# which is not a read key, so it passed as an annotation while the real key
# fell back to its default. A closed set has no boundary to probe.
ANNOTATION_KEYS = frozenset({"note"})

MAX_READ_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_S = 3600


# --- the gate registry ----------------------------------------------------
# Every gate and disqualifier, exactly once, as DATA. Nothing outside this table
# may decide a state. Order within each role is the precedence order.
#
#   role="trust"        evaluated FIRST, before any disqualifier. An untrusted
#                       attempt contributes NOTHING: not its rc, not its
#                       digests, not its double-render record. This is a
#                       precondition on reading the attempt at all, which is why
#                       it cannot be a ranked disqualifier: with FAILED ranked
#                       above it, an old attempt's rc=1 reported "the build
#                       failed" for a contract that was never built.
#   role="disqualifier" most serious first. Any hit ends the verdict.
#   role="achievement"  highest reached, and only when no disqualifier hit.
#                       Never compared against disqualifiers: REVIEWED's
#                       condition contains VALIDATED's, so one ranking made a
#                       fully reviewed result exit 1.
GATES = (
    # --- trust ------------------------------------------------------------
    dict(id="no_attempt", role="trust", state="INCOMPLETE_EVIDENCE",
         why="no attempt names this contract instance",
         action="run `result.py build` for this contract"),
    dict(id="attempt_unfinalised", role="trust", state="INCOMPLETE_EVIDENCE",
         why="the latest attempt for this contract never finished",
         action="re-run `result.py build`, or wait for it to finish"),

    # --- disqualifiers, most serious first --------------------------------
    dict(id="command_failed", role="disqualifier", state="FAILED",
         why="the build command exited non-zero",
         action="fix the command, then re-run `result.py build`"),
    dict(id="check_could_not_run", role="disqualifier",
         state="INCOMPLETE_EVIDENCE",
         why="a declared check could not run",
         action="fix what the check reads, then re-run `result.py check`"),
    dict(id="determinism_untested", role="disqualifier",
         state="INCOMPLETE_EVIDENCE",
         why="deterministic: true was declared and no double render was ever "
             "recorded",
         action="run `result.py build --double`"),
    dict(id="no_production_evidence", role="disqualifier",
         state="INCOMPLETE_EVIDENCE",
         why="an output carries no evidence that the command produced it",
         action="re-run `result.py build` so the build window is recorded, or "
                "declare deterministic: true and run `build --double`"),
    dict(id="double_render_differed", role="disqualifier",
         state="NONDETERMINISTIC",
         why="a recorded double render differed where determinism was declared",
         action="make the command deterministic, or drop deterministic: true"),
    dict(id="consumed_differs_from_declared", role="disqualifier",
         state="CONTRACT_DRIFTED",
         why="the build consumed inputs differing from the declared ones",
         action="re-declare the contract, or restore the inputs"),
    dict(id="inputs_changed_since_build", role="disqualifier", state="STALE",
         why="the inputs changed after the build that produced these outputs",
         action="re-run `result.py build`"),

    # --- achievements, highest last ---------------------------------------
    dict(id="outputs_exist", role="achievement", state="GENERATED",
         why="the command ran and produced the declared outputs", action=None),
    dict(id="checks_passed", role="achievement", state="VALIDATED",
         why="every declared check passed", action=None),
    dict(id="accepted_by_person", role="achievement", state="REVIEWED",
         why="a named person accepted these digests", action=None),
)

_TRUST = tuple(g for g in GATES if g["role"] == "trust")
_DISQUALIFIERS = tuple(g for g in GATES if g["role"] == "disqualifier")
_ACHIEVEMENTS = tuple(g for g in GATES if g["role"] == "achievement")


def evaluate(hits):
    """THE single decider. Pure: takes a set of gate ids that fired and returns
    (state_name, exit_code, [reasons]). Reads nothing, runs nothing.

    Nothing else in this file may choose a state or an exit code. That is
    enforced by tests/test_result.py, which AST-walks this module and asserts
    each state constant is returned from exactly one function."""
    fired = set(hits)
    reasons = []

    for gate in _TRUST:
        if gate["id"] in fired:
            reasons.append(_explain(gate))
            return gate["state"], STATES[gate["state"]], reasons

    for gate in _DISQUALIFIERS:
        if gate["id"] in fired:
            reasons.append(_explain(gate))
            # Every finding is reported, not only the deciding one: a user who
            # fixes what the exit code named can re-run and meet the next.
            for later in _DISQUALIFIERS:
                if later is not gate and later["id"] in fired:
                    reasons.append(_explain(later))
            return gate["state"], STATES[gate["state"]], reasons

    reached = None
    for gate in _ACHIEVEMENTS:
        if gate["id"] in fired:
            reached = gate
    if reached is None:
        g = dict(id="nothing_established", state="INCOMPLETE_EVIDENCE",
                 why="no declared output exists",
                 action="run `result.py build`")
        return g["state"], STATES[g["state"]], [_explain(g)]
    return reached["state"], STATES[reached["state"]], [_explain(reached)]


def _explain(gate):
    """One reason line. Every refusal names an action -- a refusal a user cannot
    act on is a defect here even when the refusal is correct."""
    if gate.get("action"):
        return f"{gate['why']}. {gate['action'][0].upper()}{gate['action'][1:]}."
    return gate["why"]


def gate_by_id(gid):
    for g in GATES:
        if g["id"] == gid:
            return g
    raise KeyError(f"no gate {gid!r}; every gate must be declared in GATES")


# --- bounded, non-blocking reads -----------------------------------------
def digest_file(path, cap=None):
    """(sha256_hex, size, error). Regular files only, bounded.

    Copied in shape from contract.py: a FIFO blocks forever with no exception
    to catch, and an unbounded read OOMs on a large artifact. Figures and
    exports are routinely large, so the cap is a real limit here, not a
    theoretical one, and exceeding it is reported rather than silently ignored."""
    p = Path(path)
    try:
        if not p.exists():
            return None, None, "missing"
        if p.is_dir():
            return None, None, "is a directory"
        if not p.is_file():
            return None, None, "not a regular file"
        size = p.stat().st_size
        h = hashlib.sha256()
        read = 0
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                read += len(chunk)
                if cap is not None and read > cap:
                    return None, size, f"larger than the {cap} byte cap"
                h.update(chunk)
        return h.hexdigest(), size, None
    except OSError as e:
        return None, None, f"unreadable: {e}"


def fingerprint(path):
    """What a path looks like right now, as data. `None` digest with a reason
    is a first-class answer: absent is not the same as unreadable."""
    d, size, err = digest_file(path)
    return {"path": str(path), "sha256": d, "size": size, "error": err}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run(argv, cwd=None, timeout=DEFAULT_TIMEOUT_S, shell=False):
    try:
        r = subprocess.run(argv, cwd=cwd, timeout=timeout, shell=shell,
                           capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except (OSError, ValueError) as e:
        return 127, "", f"could not run: {e}"


def read_json(path):
    """(obj, error). Never raises."""
    try:
        p = Path(path)
        if not p.is_file():
            return None, "missing"
        if p.stat().st_size > MAX_READ_BYTES:
            return None, f"larger than the {MAX_READ_BYTES} byte cap"
        return json.loads(p.read_text()), None
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def write_json(path, obj):
    """Atomic. A half-written receipt read as evidence is worse than none."""
    try:
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
        tmp.replace(p)
        return None
    except (OSError, ValueError) as e:
        return f"{type(e).__name__}: {e}"


def scrub_url(url):
    """Strip userinfo from a git remote: a token in a URL must never reach a
    receipt. Same rule as contract.py."""
    if not isinstance(url, str) or "@" not in url:
        return url
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            return f"{scheme}://{rest.split('@', 1)[1]}"
    return url


def git_context(cwd):
    """Source revision and dirtiness, best effort. Absent is recorded as absent
    rather than guessed."""
    ctx = {"commit": None, "dirty_diff_sha256": None, "remote": None}
    rc, out, _ = run(["git", "rev-parse", "HEAD"], cwd=cwd, timeout=30)
    if rc == 0:
        ctx["commit"] = out.strip()
    rc, out, _ = run(["git", "remote", "get-url", "origin"], cwd=cwd, timeout=30)
    if rc == 0:
        ctx["remote"] = scrub_url(out.strip())
    rc, out, _ = run(["git", "diff", "HEAD"], cwd=cwd, timeout=60)
    if rc == 0 and out.strip():
        ctx["dirty_diff_sha256"] = hashlib.sha256(out.encode()).hexdigest()
    return ctx


# --- criterion keys -------------------------------------------------------
# Every check kind and the exact keys it is READ with. Enumerated, not
# inferred: an unrecognised key used to be silently ignored, so a typo'd
# criterion fell back to a default and the declared criterion became WEAKER
# than declared with no warning. The residual must be the refusal.
CHECK_SCHEMA = {
    "exists":      {"required": ("path",),           "optional": ()},
    "min_size":    {"required": ("path",),           "optional": ("bytes",)},
    "min_lines":   {"required": ("path",),           "optional": ("lines",)},
    "magic":       {"required": ("path", "kind"),    "optional": ()},
    "csv_columns": {"required": ("path", "columns"), "optional": ("delimiter",)},
    "numeric":     {"required": ("path", "column", "tolerance"),
                    "optional": ("min", "max")},
    "command":     {"required": ("run",),            "optional": ("timeout",)},
}


def check_fault(check):
    """Why a declared check cannot be interpreted, or None. Names the allowed
    keys and the annotation route, so the refusal carries its own fix."""
    if not isinstance(check, dict):
        return (f"check is not an object: {check!r}. Use a JSON object, e.g. "
                f'{{"kind":"exists","path":"fig1.pdf"}}')
    kind = check.get("kind")
    spec = CHECK_SCHEMA.get(kind)
    if spec is None:
        return (f"unknown check kind {kind!r}; use one of: "
                f"{', '.join(sorted(CHECK_SCHEMA))}")
    allowed = {"kind", *ANNOTATION_KEYS, *spec["required"], *spec["optional"]}
    readable = sorted(allowed - {"kind"} - ANNOTATION_KEYS)
    missing = [k for k in spec["required"] if k not in check]
    if missing:
        example = {"kind": kind}
        example.update({k: "..." for k in spec["required"]})
        return (f"{kind} check is missing required key(s) "
                f"{', '.join(missing)}; add them, e.g. {json.dumps(example)}")
    unknown = sorted(set(check) - allowed)
    if unknown:
        return (f"{kind} check has unrecognised key(s) {', '.join(unknown)}; "
                f"it reads only {', '.join(readable)}. A typo here would "
                f"silently weaken the criterion. Put any commentary in "
                f"{sorted(ANNOTATION_KEYS)[0]!r}, which is recorded and never "
                f"evaluated.")
    if kind == "numeric":
        t = check.get("tolerance")
        if not isinstance(t, (int, float)) or isinstance(t, bool) or t < 0:
            return (f"numeric check needs a non-negative 'tolerance', got "
                    f"{t!r}. A missing tolerance is a refusal, never zero: "
                    f"zero silently demands bit-exactness of a float.")
    return None
