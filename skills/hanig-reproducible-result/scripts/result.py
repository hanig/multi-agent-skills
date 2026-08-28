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

    # Achievements are CUMULATIVE: each requires every achievement below it.
    # Taking the highest FIRED one let a result with no outputs and no declared
    # checks reach VALIDATED, because `checks_passed` fired vacuously -- a false
    # pass in a tool whose whole job is refusing them. Found independently by
    # both committee members reviewing against criteria they wrote.
    reached = None
    for gate in _ACHIEVEMENTS:
        if gate["id"] not in fired:
            break
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
    # `format`, not `kind`: `kind` already names the CHECK kind, and
    # overloading it made the check read its own name as the file type.
    "magic":       {"required": ("path", "format"),  "optional": ()},
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
                    f"{t!r}; add one, e.g. \"tolerance\": 0.001. A missing "
                    f"tolerance is a refusal, never zero: zero silently "
                    f"demands bit-exactness of a float.")
    return None


# --- declare --------------------------------------------------------------
def cmd_declare(args):
    run_dir = Path(args.result_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    cpath = run_dir / CONTRACT
    if cpath.exists() and not args.force:
        sys.exit(f"error: {cpath} exists. Use --force to replace it, or "
                 f"declare into a different directory.")

    checks = []
    for spec in args.check:
        try:
            check = json.loads(spec)
        except json.JSONDecodeError as e:
            sys.exit(f"error: --check is not valid JSON: {e}\n  {spec}\n"
                     f'  Pass one JSON object per --check, e.g. '
                     f'--check \'{{"kind":"exists","path":"fig1.pdf"}}\'')
        fault = check_fault(check)
        if fault:
            sys.exit(f"error: {fault}\n  {spec}")
        checks.append(check)

    if not args.output:
        sys.exit("error: a contract with no declared outputs cannot verify "
                 "anything. Pass --output PATH for each artifact the command "
                 "produces.")

    cwd = str(Path(args.cwd).resolve()) if args.cwd else os.getcwd()

    # Fingerprints of declared outputs that ALREADY EXIST at declare time.
    # Ported from contract.py rather than reinvented: without it, a
    # pre-existing artifact satisfies a manifest and reaches VALIDATED with no
    # command having produced it. NOT carrying this across would have been the
    # 20th instance of the sibling-miss class, caught here before the code
    # existed rather than after it shipped.
    preexisting = {str(o): fingerprint(_abs(o, cwd)) for o in args.output}

    contract = {
        "schema_version": 1,
        "tool": "result.py",
        "created_at": now_iso(),
        "created_at_epoch": time.time(),
        # Per-instance nonce. A content digest alone is not enough: `declare
        # --force` with identical criteria produced an identical digest, so a
        # stale record re-bound itself to the new instance.
        "contract_id": os.urandom(8).hex(),
        "command": args.command,
        "cwd": cwd,
        "declared_outputs": list(args.output),
        "declared_inputs": list(args.input),
        "declared_checks": checks,
        "deterministic": bool(args.deterministic),
        "input_digests": {str(i): fingerprint(_abs(i, cwd)) for i in args.input},
        "preexisting_outputs": preexisting,
        "git": git_context(cwd),
        "env": {k: os.environ.get(k) for k in sorted(args.record_env or [])},
    }
    contract["criteria_digest"] = criteria_digest(contract)
    err = write_json(cpath, contract)
    if err:
        sys.exit(f"error: cannot write {cpath}: {err}. Check the directory is "
                 f"writable, then re-run `declare`.")

    already = [o for o, fp in preexisting.items() if fp.get("sha256")]
    print(f"declared {cpath}")
    print(f"  contract_id      {contract['contract_id']}")
    print(f"  outputs          {len(args.output)}")
    print(f"  inputs           {len(args.input)}")
    print(f"  checks           {len(checks)}")
    print(f"  deterministic    {contract['deterministic']}")
    if already:
        print(f"\n  NOTE: {len(already)} declared output(s) already exist: "
              f"{', '.join(already[:3])}")
        print("  Their current digests are recorded. An output that is not "
              "written\n  inside the build window will not count as produced.")
    return 0


DIGESTED_FIELDS = ("command", "declared_outputs", "declared_inputs",
                   "declared_checks", "deterministic", "cwd", "contract_id")


def criteria_digest(contract):
    """Fingerprint of the declared criteria, so a later edit is detectable.
    contract_id is included: without it, `declare --force` with identical
    criteria produced an identical digest and a stale receipt still matched."""
    payload = {k: contract.get(k) for k in DIGESTED_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _abs(path, base):
    p = Path(path)
    return p if p.is_absolute() else Path(base) / p


# --- build ----------------------------------------------------------------
def cmd_build(args):
    run_dir = Path(args.result_dir).resolve()
    contract, err = read_json(run_dir / CONTRACT)
    if err:
        sys.exit(f"error: no readable contract at {run_dir / CONTRACT}: {err}. "
                 f"Run `result.py declare {run_dir}` first.")
    if not isinstance(contract, dict):
        sys.exit(f"error: contract is not a JSON object. Re-declare it with "
                 f"`declare --force`.")

    cwd = contract.get("cwd") or os.getcwd()
    outputs = contract.get("declared_outputs") or []
    attempt_id = os.urandom(8).hex()

    # --- GATE 2: the production window ------------------------------------
    # Digest every declared output immediately BEFORE the command, and again
    # immediately AFTER. What lies between is the only interval in which this
    # command could have written anything.
    #
    # This is what plan v7 lacked and what a committee split out. The manifest
    # (gate 1) proves integrity and instance binding; it cannot prove the
    # command WROTE the file. The obvious alternative -- porting only
    # preexisting fingerprints and an mtime freshness test -- fails three ways:
    # penalise unchanged bytes and an honest deterministic regeneration is
    # refused; permit them and a no-op command adopts the old artifact; require
    # a fresh mtime and it is attribution by timestamp, which this repo has
    # retired three rules over.
    #
    # The window escapes all three because it is a CONTENT comparison across a
    # bounded interval the tool itself opens and closes. Never a clock.
    before = {str(o): fingerprint(_abs(o, cwd)) for o in outputs}

    started = now_iso()
    t0 = time.time()
    rc, out, err_text = run(contract.get("command"), cwd=cwd,
                            timeout=args.timeout, shell=True)
    elapsed = round(time.time() - t0, 3)

    after = {str(o): fingerprint(_abs(o, cwd)) for o in outputs}

    # The digests this BUILD consumed, captured at build time. Criterion 11
    # names the alternative verbatim as forbidden: "never by the declared
    # digests: a declare-time digest standing in for what the build used is an
    # attribution, and it false-alarms an honest rebuild while false-passing an
    # edit-build-revert." I shipped exactly that, and both reviewers caught it.
    # Without this field CONTRACT_DRIFTED is also unreachable, because there is
    # nothing to compare the declared digests against.
    consumed = {str(i): fingerprint(_abs(i, cwd))
                for i in (contract.get("declared_inputs") or [])}

    production = {}
    for o in outputs:
        o = str(o)
        b, a = before.get(o, {}), after.get(o, {})
        appeared = b.get("sha256") is None and a.get("sha256") is not None
        changed = (b.get("sha256") is not None
                   and a.get("sha256") is not None
                   and b["sha256"] != a["sha256"])
        production[o] = {
            "before": b.get("sha256"), "after": a.get("sha256"),
            "appeared_in_window": appeared,
            "changed_in_window": changed,
            # The honest name. "produced" would overstate it: a concurrent
            # writer racing this window is indistinguishable from the command,
            # a residual this family already cedes because a `command` check
            # runs unsandboxed and anyone who can race that write already holds
            # total directory authority.
            "written_in_window": bool(appeared or changed),
        }

    attempt = {
        "attempt_id": attempt_id,
        "contract_id": contract.get("contract_id"),
        "criteria_digest": contract.get("criteria_digest"),
        "started_at": started,
        "finished_at": now_iso(),
        "finalised": True,
        "elapsed_s": elapsed,
        "exit_code": rc,
        "stdout_tail": (out or "")[-4000:],
        "stderr_tail": (err_text or "")[-4000:],
        "production": production,
        "consumed_inputs": consumed,
        "double_render": None,
    }

    if args.double:
        attempt["double_render"] = _double_render(contract, cwd, outputs, args)

    with (run_dir / ATTEMPTS).open("a") as fh:
        fh.write(json.dumps(attempt, sort_keys=True) + "\n")

    manifest = {
        "schema_version": 1,
        "contract_id": contract.get("contract_id"),
        "attempt_id": attempt_id,
        "written_at": now_iso(),
        "outputs": {o: {**after.get(o, {}), **production.get(o, {})}
                    for o in map(str, outputs)},
    }
    werr = write_json(run_dir / MANIFEST, manifest)
    if werr:
        sys.exit(f"error: cannot write {run_dir / MANIFEST}: {werr}. Check the "
                 f"directory is writable, then re-run `build`.")

    produced = sum(1 for p in production.values() if p["written_in_window"])
    print(f"build {'ok' if rc == 0 else f'FAILED (rc={rc})'} in {elapsed}s")
    print(f"  attempt_id       {attempt_id}")
    print(f"  written in window {produced}/{len(outputs)}")
    for o, p in sorted(production.items()):
        mark = "wrote" if p["written_in_window"] else "UNTOUCHED"
        print(f"    [{mark}] {o}")
    if produced < len(outputs):
        print("\n  An output the command did not write carries no production\n"
              "  evidence. `check` will not call it VALIDATED on that basis.")
    print("\n  build never decides more than GENERATED. Run `result.py check`.")
    return 0


def _double_render(contract, cwd, outputs, args):
    """Run the command a second time into a scratch tree and compare bytes.

    Lives in `build`, not `check`: running the command is build's job, and a
    `check` that re-runs the build could manufacture the evidence it is meant
    to judge."""
    scratch = Path(tempfile.mkdtemp(prefix="result-double-"))
    try:
        rc2, _, err2 = run(contract.get("command"), cwd=cwd,
                           timeout=args.timeout, shell=True)
        comparisons = {}
        for o in map(str, outputs):
            src = _abs(o, cwd)
            d, _, derr = digest_file(src)
            comparisons[o] = {"second_sha256": d, "error": derr}
        return {"ran": True, "exit_code": rc2,
                "error": (err2 or "")[-500:] if rc2 != 0 else None,
                "outputs": comparisons}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- check ----------------------------------------------------------------
def owned_attempts(attempts, contract):
    """Only attempts naming THIS contract instance. An unbound attempts log
    survives `declare --force`, so without this a stale exit-0 record certifies
    a run that never happened under the new contract.

    Returning ALL attempts when the contract carries no id would make the
    binding opt-out -- the absent-field bypass. Every `declare` writes an id,
    so an absent one is a malformed contract, not an old one."""
    want = contract.get("contract_id")
    if not isinstance(want, str) or not want.strip():
        return []
    return [a for a in attempts if a.get("contract_id") == want]


def read_attempts(run_dir):
    out = []
    p = Path(run_dir) / ATTEMPTS
    if not p.is_file():
        return out
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    except OSError:
        return out
    return out


def evaluate_check(check, base):
    """(ok, detail, could_run). `could_run` is False when the check itself
    could not be evaluated, which is INCOMPLETE_EVIDENCE, never a failure: the
    two carry different actions and must stay distinguishable in the receipt."""
    fault = check_fault(check)
    if fault:
        return False, fault, False
    kind = check["kind"]
    if kind == "command":
        rc, out, err = run(check["run"], cwd=base,
                           timeout=int(check.get("timeout", 300)), shell=True)
        return rc == 0, f"`{check['run']}` -> rc={rc} {(out or err)[:200]}", True

    p = _abs(check["path"], base)
    d, size, derr = digest_file(p)
    if derr == "missing":
        return False, f"{check['path']} MISSING", True
    if derr:
        return False, f"{check['path']}: {derr}", False

    if kind == "exists":
        return True, f"{check['path']} exists", True
    if kind == "min_size":
        want = int(check.get("bytes", 1))
        return size >= want, f"{check['path']} is {size}B (need >= {want}B)", True
    if kind == "min_lines":
        want = int(check.get("lines", 1))
        try:
            n = sum(1 for _ in p.open("rb"))
        except OSError as e:
            return False, f"{check['path']}: unreadable: {e}", False
        return n >= want, f"{check['path']} has {n} lines (need >= {want})", True
    if kind == "magic":
        return _magic(p, check)
    if kind == "csv_columns":
        return _csv_columns(p, check)
    if kind == "numeric":
        return _numeric(p, check)
    return False, f"unknown check kind {kind!r}", False


MAGIC = {"pdf": b"%PDF-", "png": b"\x89PNG\r\n\x1a\n", "jpeg": b"\xff\xd8\xff",
         "gzip": b"\x1f\x8b", "svg": b"<", "zip": b"PK\x03\x04"}


def _magic(p, check):
    """Magic bytes only. No PDF or image library, so this proves the file
    STARTS like the declared format and nothing more. The receipt says so,
    because a receipt that implies a parser ran would overstate the evidence."""
    want = str(check.get("format", "")).lower()
    target = MAGIC.get(want)
    if target is None:
        return (False,
                f"magic check declares format {want!r}, which has no known "
                f"signature; use one of: {', '.join(sorted(MAGIC))}", False)
    try:
        with p.open("rb") as fh:
            head = fh.read(16)
    except OSError as e:
        return False, f"{check['path']}: unreadable: {e}", False
    ok = head.startswith(target)
    return (ok,
            f"{check['path']} {'starts' if ok else 'does NOT start'} with the "
            f"{want} signature (structure only; no parser was used)", True)


def _csv_columns(p, check):
    import csv as _csv
    want = check.get("columns")
    if not isinstance(want, list) or not all(isinstance(c, str) for c in want):
        return False, "'columns' must be a list of strings", False
    try:
        with p.open(newline="") as fh:
            reader = _csv.reader(fh, delimiter=check.get("delimiter", ","))
            header = next(reader, None)
    except (OSError, ValueError) as e:
        return False, f"{check['path']}: unreadable: {e}", False
    if header is None:
        return False, f"{check['path']} is empty", True
    missing = [c for c in want if c not in header]
    return (not missing,
            f"{check['path']} columns: "
            + ("all declared present" if not missing
               else f"missing {', '.join(missing)}"), True)


def _numeric(p, check):
    import csv as _csv
    col, tol = check["column"], float(check["tolerance"])
    lo, hi = check.get("min"), check.get("max")
    try:
        with p.open(newline="") as fh:
            rows = list(_csv.DictReader(fh))
    except (OSError, ValueError) as e:
        return False, f"{check['path']}: unreadable: {e}", False
    if not rows or col not in (rows[0] or {}):
        return False, f"{check['path']} has no column {col!r}", True
    vals = []
    for r in rows:
        try:
            vals.append(float(r[col]))
        except (TypeError, ValueError):
            return (False,
                    f"{check['path']} column {col!r} holds a non-numeric "
                    f"value {r[col]!r}", False)
    bad = []
    if lo is not None:
        bad += [v for v in vals if v < float(lo) - tol]
    if hi is not None:
        bad += [v for v in vals if v > float(hi) + tol]
    return (not bad,
            f"{check['path']}[{col}] n={len(vals)} "
            + (f"all within bounds (tol {tol})" if not bad
               else f"{len(bad)} value(s) outside bounds (tol {tol})"), True)


def gather(run_dir, contract, args):
    """Collect EVIDENCE. Returns (hits, notes, evidence). Gathers only; it never
    decides. Every state decision belongs to evaluate()."""
    hits, notes, ev = set(), [], {}
    cwd = contract.get("cwd") or os.getcwd()

    attempts = owned_attempts(read_attempts(run_dir), contract)
    if not attempts:
        hits.add("no_attempt")
        return hits, notes, ev

    # Only the LATEST finalised attempt decides. Requiring merely that SOME
    # attempt with the manifest's id exists would let a successful A survive a
    # later failing B, so B's failure disappears -- the "latest retry decides"
    # defect in the direction that grants a pass.
    latest = attempts[-1]
    ev["attempt_id"] = latest.get("attempt_id")
    if not latest.get("finalised"):
        hits.add("attempt_unfinalised")
        return hits, notes, ev

    if latest.get("exit_code") != 0:
        hits.add("command_failed")
        notes.append(f"the build command exited {latest.get('exit_code')}")

    # --- gate 1: integrity -------------------------------------------------
    manifest, merr = read_json(Path(run_dir) / MANIFEST)
    outputs = [str(o) for o in (contract.get("declared_outputs") or [])]
    if merr or not isinstance(manifest, dict):
        notes.append(f"no readable manifest ({merr}); no output can be bound "
                     f"to this build. Re-run `result.py build`.")
        hits.add("no_production_evidence")
        return hits, notes, ev
    if manifest.get("contract_id") != contract.get("contract_id"):
        notes.append("the manifest was written for a different contract "
                     "instance. Re-run `result.py build`.")
        hits.add("no_production_evidence")
        return hits, notes, ev
    if manifest.get("attempt_id") != latest.get("attempt_id"):
        notes.append("the manifest does not describe the latest attempt; an "
                     "older manifest never falls back into authority. Re-run "
                     "`result.py build`.")
        hits.add("no_production_evidence")
        return hits, notes, ev

    mout = manifest.get("outputs") or {}
    present, produced, integrity = [], [], []
    for o in outputs:
        rec = mout.get(o) or {}
        now = fingerprint(_abs(o, cwd))
        ev.setdefault("outputs", {})[o] = {
            "manifest_sha256": rec.get("sha256"), "now_sha256": now.get("sha256"),
            "written_in_window": rec.get("written_in_window"),
        }
        if now.get("sha256") is None:
            notes.append(f"{o}: {now.get('error') or 'missing'}")
            continue
        present.append(o)
        # Gate 1 is a MEASUREMENT check performs itself, not a claim it reads.
        if rec.get("sha256") != now.get("sha256"):
            integrity.append(o)
            notes.append(f"{o} changed since the build that recorded it")
        # Gate 2, and it is separate on purpose: integrity proves these bytes
        # were observed for this attempt, never that the command wrote them.
        if rec.get("written_in_window"):
            produced.append(o)

    if integrity:
        hits.add("inputs_changed_since_build")

    unproduced = [o for o in present if o not in produced]
    if unproduced:
        # deterministic:true plus a confirmed double render is the one honest
        # way an untouched output still counts: the command provably need not
        # have rewritten it.
        dr = latest.get("double_render") or {}
        if not (contract.get("deterministic") and dr.get("ran")
                and dr.get("exit_code") == 0):
            hits.add("no_production_evidence")
            for o in unproduced:
                notes.append(f"{o} exists but the command did not write it "
                             f"during the build window")

    if present:
        hits.add("outputs_exist")

    # --- inputs: drift vs staleness, both by content, both from the BUILD ---
    # Two comparisons, three digests of the same path, no timestamps:
    #   declared (at declare) vs consumed (at build)  -> CONTRACT_DRIFTED
    #   consumed (at build)   vs current (now)        -> STALE
    # Comparing declared directly to current is what criterion 11 forbids: it
    # false-alarms an honest rebuild and false-passes an edit-build-revert.
    declared = contract.get("input_digests") or {}
    consumed = latest.get("consumed_inputs")
    if consumed is None:
        # An attempt from before consumed_inputs was recorded cannot support
        # either verdict. Refuse rather than fall back to the declared digests.
        if declared:
            hits.add("check_could_not_run")
            notes.append("this attempt predates consumed-input recording, so "
                         "neither drift nor staleness can be judged. Re-run "
                         "`result.py build`.")
    else:
        for i, was in declared.items():
            at_build = (consumed.get(i) or {}).get("sha256")
            if was.get("sha256") and at_build and was["sha256"] != at_build:
                hits.add("consumed_differs_from_declared")
                notes.append(f"input {i} differed from its declared digest at "
                             f"build time")
        for i, at in consumed.items():
            now = fingerprint(_abs(i, cwd))
            if at.get("sha256") and now.get("sha256") \
                    and at["sha256"] != now["sha256"]:
                hits.add("inputs_changed_since_build")
                notes.append(f"input {i} changed after the build that consumed "
                             f"it")

    # --- determinism -------------------------------------------------------
    if contract.get("deterministic"):
        dr = latest.get("double_render")
        if not dr or not dr.get("ran"):
            hits.add("determinism_untested")
        else:
            for o, cmp_ in (dr.get("outputs") or {}).items():
                rec = (mout.get(o) or {}).get("sha256")
                if cmp_.get("second_sha256") and rec \
                        and cmp_["second_sha256"] != rec:
                    hits.add("double_render_differed")
                    notes.append(f"{o} differed on a second render")

    # --- declared checks ---------------------------------------------------
    results = []
    for check in (contract.get("declared_checks") or []):
        ok, detail, could_run = evaluate_check(check, cwd)
        kind = check.get("kind") if isinstance(check, dict) else "?"
        results.append({"kind": kind, "ok": ok, "could_run": could_run,
                        "detail": detail})
        if not could_run:
            hits.add("check_could_not_run")
    ev["checks"] = results
    # An EMPTY check set is not a pass. "Nothing declared, nothing unmet" reads
    # as reasonable and is exactly how a result with no outputs reached
    # VALIDATED. A contract that declares no checks has established GENERATED at
    # most: the command ran, and nothing examined what it produced.
    if results and all(r["ok"] and r["could_run"] for r in results):
        hits.add("checks_passed")
    elif not results:
        notes.append("no checks were declared, so nothing beyond existence was "
                     "examined. Add --check at declare time to reach VALIDATED.")

    # --- review ------------------------------------------------------------
    for rev in _reviews(run_dir):
        if rev.get("contract_id") != contract.get("contract_id"):
            notes.append(f"a review by {rev.get('by')!r} names a different "
                         f"contract instance and is ignored")
            continue
        if rev.get("output_digests") == {o: (mout.get(o) or {}).get("sha256")
                                         for o in outputs}:
            hits.add("accepted_by_person")
            ev["reviewed_by"] = rev.get("by")
        else:
            notes.append(f"the review by {rev.get('by')!r} was for different "
                         f"digests and no longer applies. Re-review, or "
                         f"restore the outputs it accepted.")
    return hits, notes, ev


def _reviews(run_dir):
    out = []
    p = Path(run_dir) / REVIEWS
    if not p.is_file():
        return out
    try:
        for line in p.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    if isinstance(r, dict):
                        out.append(r)
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def cmd_check(args):
    run_dir = Path(args.result_dir).resolve()
    contract, err = read_json(run_dir / CONTRACT)
    if err or not isinstance(contract, dict):
        sys.exit(f"error: no readable contract at {run_dir / CONTRACT}: "
                 f"{err or 'not a JSON object'}. Run `result.py declare "
                 f"{run_dir}` first.")

    if contract.get("criteria_digest") and \
            criteria_digest(contract) != contract["criteria_digest"]:
        sys.exit("error: the contract's criteria were edited after it was "
                 "declared, so its own digest no longer matches. Re-declare "
                 "with `declare --force`.")

    hits, notes, ev = gather(run_dir, contract, args)
    state, code, reasons = evaluate(hits)

    receipt = {
        "schema_version": 1, "tool": "result.py", "checked_at": now_iso(),
        "state": state, "exit_code": code,
        "contract_id": contract.get("contract_id"),
        "criteria_digest": contract.get("criteria_digest"),
        "gates_fired": sorted(hits), "reasons": reasons, "notes": notes,
        "evidence": ev,
        # Machine-readable scope, so a consumer sees the claim's boundary
        # without parsing prose. Provenance is exactly as good as the
        # declaration: an undeclared input is invisible to this tool.
        "provenance_scope": {
            "covers": "declared inputs only",
            "undeclared_inputs_detected": False,
            "structure_only": True,
            "note": "checks use magic bytes and structure; no PDF or image "
                    "parser was used, and an input that was never declared "
                    "is not covered.",
        },
    }
    werr = write_json(run_dir / RECEIPT, receipt)
    if werr:
        print(f"WARNING: could not write {RECEIPT}: {werr}", file=sys.stderr)

    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return code
    print(f"{state}  ({run_dir})")
    for r in reasons:
        print(f"  - {r}")
    for n in notes:
        print(f"  . {n}")
    for c in ev.get("checks") or []:
        mark = "PASS" if c["ok"] else ("FAIL" if c["could_run"] else "CANNOT RUN")
        print(f"  [{mark}] {c['kind']}: {c['detail']}")
    if state == "GENERATED":
        print("\n  The command ran and produced its outputs. That is NOT "
              "success:\n  nothing has checked whether they are right.")
    print(f"\n  provenance covers DECLARED inputs only; structure-only checks.")
    return code


def cmd_review(args):
    run_dir = Path(args.result_dir).resolve()
    contract, err = read_json(run_dir / CONTRACT)
    if err or not isinstance(contract, dict):
        sys.exit(f"error: no readable contract at {run_dir / CONTRACT}: "
                 f"{err or 'not a JSON object'}. Run `result.py declare` first.")
    receipt, rerr = read_json(run_dir / RECEIPT)
    if rerr or not isinstance(receipt, dict):
        sys.exit(f"error: no verification receipt at {run_dir / RECEIPT}. Run "
                 f"`result.py check {run_dir}` first: a review accepts a "
                 f"CHECKED result, never an unchecked one.")
    if receipt.get("state") != "VALIDATED":
        sys.exit(f"error: this result is {receipt.get('state')}, not "
                 f"VALIDATED, so there is nothing to accept. Fix what `check` "
                 f"reported, then re-run it before reviewing.")

    manifest, merr = read_json(run_dir / MANIFEST)
    mout = (manifest or {}).get("outputs") or {}
    digests = {o: (mout.get(o) or {}).get("sha256")
               for o in map(str, contract.get("declared_outputs") or [])}
    rec = {"by": args.by, "note": args.note, "at": now_iso(),
           "contract_id": contract.get("contract_id"),
           "criteria_digest": contract.get("criteria_digest"),
           "output_digests": digests}
    with (run_dir / REVIEWS).open("a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    print(f"recorded a review by {args.by}")
    print("  It names the exact digests it accepted, so any later change to "
          "an\n  output invalidates it by content, never by time.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="result.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("declare", help="write the contract BEFORE the build")
    d.add_argument("result_dir")
    d.add_argument("--command", required=True)
    d.add_argument("--output", action="append", default=[])
    d.add_argument("--input", action="append", default=[])
    d.add_argument("--check", action="append", default=[])
    d.add_argument("--record-env", action="append", default=[])
    d.add_argument("--deterministic", action="store_true")
    d.add_argument("--cwd")
    d.add_argument("--force", action="store_true")
    d.set_defaults(fn=cmd_declare)

    b = sub.add_parser("build", help="run the command; never decides more "
                                     "than GENERATED")
    b.add_argument("result_dir")
    b.add_argument("--double", action="store_true",
                   help="render a second time and record whether the outputs "
                        "came out byte-identical")
    b.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    b.set_defaults(fn=cmd_build)

    c = sub.add_parser("check", help="judge the result against the contract")
    c.add_argument("result_dir")
    c.add_argument("--reference")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)

    r = sub.add_parser("review", help="record a named person's acceptance")
    r.add_argument("result_dir")
    r.add_argument("--by", required=True)
    r.add_argument("--note", default="")
    r.set_defaults(fn=cmd_review)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
