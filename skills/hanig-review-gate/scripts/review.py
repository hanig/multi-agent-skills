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
    1  REVIEW_FAIL          a confirmed defect or a refuted claim
    2  REVIEW_UNAVAILABLE   no reviewer could run -- NOT a pass
    3  REVIEW_PARTIAL       some ran, quorum unmet -- caller decides
    4  REVIEW_ERROR         usage or configuration error

Never treat 2 or 3 as success. An unreviewed change is unreviewed.

Python 3.8+, standard library only.
"""

import argparse
import concurrent.futures
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATES = {"REVIEW_PASS": 0, "REVIEW_FAIL": 1, "REVIEW_UNAVAILABLE": 2,
          "REVIEW_PARTIAL": 3, "REVIEW_ERROR": 4}

HERE = Path(__file__).resolve().parent
CONFIG = HERE.parent / "reviewers.json"
DEFAULT_PROFILE = "standard"

# Keep payloads bounded; an oversized diff silently truncated is a lie about
# what was reviewed, so truncation is always reported in the output.
MAX_CHARS = 180_000

# Every reviewer here is a reasoning model, and reasoning tokens come out of the
# same budget as the answer. At 16000 a 150KB review spent the whole budget
# thinking and returned NO content -- which read as an unavailable reviewer, and
# cost kimi-k2.7-code a whole session before the error message was made to say
# so. Sized for the answer AFTER the thinking.
DEFAULT_MAX_OUTPUT_TOKENS = 64_000


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
        "max_output_tokens": rev.get("max_output_tokens", 16000),
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
    if not (text or "").strip():
        # "empty response" hid the cause for a whole session: a heavy reasoner
        # spent its ENTIRE output budget on reasoning tokens and never emitted
        # content, with finish_reason=length. Say which it was.
        reason = choice.get("finish_reason")
        detail = usage.get("completion_tokens_details") or {}
        think = detail.get("reasoning_tokens")
        if reason == "length":
            return None, (
                f"no content: the model used its whole output budget "
                f"({usage.get('completion_tokens')} tokens"
                + (f", {think} of them reasoning" if think else "")
                + f") before emitting any. Raise max_output_tokens for "
                  f"{rev['name']} in reviewers.json, or review fewer files.")
        return None, (f"no content in the reply (finish_reason="
                      f"{reason!r}"
                      + (f", {think} reasoning tokens" if think else "") + ")")
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
        parts.append(git_out("diff", args.range))
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


def build_prompt(body, claims, truncated, context, threat_model=None):
    out = []
    if context:
        out.append(f"CONTEXT\n{context}\n")
    if threat_model:
        out.append(f"THREAT MODEL — what this code does and does not defend "
                   f"against:\n{threat_model}\n")
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
            config_error(f"reviewer entry is not an object: {r!r}")
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


def escalate(all_reviewers, prompt, args, truncated, label, body_len):
    """Cheapest-first with early exit.

    Running the full panel every time means paying for the slowest, dearest
    reviewer to re-find what a cheap one already caught. Each tier adds only
    the reviewers the previous tier did not run; the first failing tier ends it.
    """
    seen, completed, failed, unavailable = set(), [], [], []
    tiers_run = []
    for tier in LADDER:
        panel = [r for r in all_reviewers
                 if (not r.get("profiles") or tier in r["profiles"])
                 and r["name"] not in seen]
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
        # Any confirmed defect or refuted claim so far ends the ladder: fix it
        # before paying a dearer tier to look at the same code.
        bad = any(is_confirmed(f) for r in tier_completed for f in r["findings"]) \
            or any(norm(c.get("status")) == "refuted"
                   for r in tier_completed for c in r["claims"])
        if bad:
            if not args.json:
                print(f"  -> tier {tier} found problems; stopping before the "
                      f"dearer tiers.")
            break
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


def decide_state(n_completed, n_failed, confirmed, refuted_claims,
                 rejecting, truncated, quorum, n_out_of_scope_critical=0):
    """The gate's verdict. Extracted from main() so it can be tested directly:
    inline, the failed-reviewer hole below was invisible to every test."""
    if n_completed == 0:
        return "REVIEW_UNAVAILABLE"
    if confirmed or refuted_claims:
        return "REVIEW_FAIL"
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
    ap.add_argument("--timeout", type=int, default=1200,
                    help="per reviewer seconds (default 1200: at 64000 output "
                         "tokens a reasoning model can still be streaming at "
                         "600s, and a cut-off reviewer is REVIEW_PARTIAL, not "
                         "a pass)")
    ap.add_argument("--only", action="append", default=[], help="run only these reviewers")
    ap.add_argument("--profile", default=None,
                    help="reviewer panel: fast | standard | deep "
                         "(default from reviewers.json). --only overrides it.")
    ap.add_argument("--watchdog", type=int, default=None,
                    help="hard bound on total runtime in seconds; exits "
                         "REVIEW_UNAVAILABLE if exceeded (default: 3x the "
                         "per-reviewer timeout, minimum 1800)")
    ap.add_argument("--escalate", action="store_true",
                    help="run tiers cheapest-first (fast -> standard -> deep), "
                         "stopping at the first failure. Each tier adds only "
                         "the reviewers the previous tier did not run, so a "
                         "failure costs one cheap tier instead of the whole "
                         "panel. Strongly preferred over --profile deep.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true", help="show reviewers and availability")
    args = ap.parse_args()
    # A total bound on top of every per-reviewer timeout.
    arm_watchdog(args.watchdog if args.watchdog is not None
                 else max(1800, args.timeout * 3))

    reviewers = load_reviewers()
    profile = args.profile or DEFAULT_PROFILE
    if not args.only and not args.escalate:
        # A reviewer with no declared profiles is in every profile.
        reviewers = [r for r in reviewers
                     if not r.get("profiles") or profile in r["profiles"]]
        if not reviewers:
            config_error(f"no reviewers in profile {profile!r}")
    # --escalate deliberately keeps the full roster: the ladder filters per
    # tier itself, and pre-filtering here made the deep tier unreachable.
    if args.only:
        reviewers = [r for r in reviewers if r["name"] in args.only]
        if not reviewers:
            config_error(f"no reviewer matches {args.only}")
    if args.list:
        print(f"profile: {profile}"
              + ("  (--only overrides profile filtering)" if args.only else ""))
        sys.exit(cmd_list(reviewers, probe=not args.no_probe))

    if args.escalate and args.only:
        config_error("--escalate and --only are mutually exclusive")
    if args.quorum < 1:
        config_error(f"--quorum must be at least 1, got {args.quorum}")
    if not (args.diff or args.staged or args.range or args.file):
        args.diff = True  # reviewing the current change is the common case

    body, label = gather(args)
    if not body.strip():
        print(f"nothing to review ({label} is empty)")
        sys.exit(STATES["REVIEW_ERROR"])
    truncated = len(body) > MAX_CHARS
    if truncated:
        body = body[:MAX_CHARS]
    prompt = build_prompt(body, args.claim, truncated, args.context,
                          args.threat_model)

    runnable, unavailable = [], []
    for rev in reviewers:
        why = availability(rev)
        (unavailable if why else runnable).append(
            {"name": rev["name"], "reason": why} if why else rev)

    if not runnable:
        report = {"state": "REVIEW_UNAVAILABLE", "checked_at": now(),
                  "reviewed": label, "unavailable": unavailable, "results": []}
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("REVIEW_UNAVAILABLE — no reviewer could run:")
            for u in unavailable:
                print(f"  - {u['name']}: {u['reason']}")
            print("\nThis is NOT a pass. The change is unreviewed.")
        sys.exit(STATES["REVIEW_UNAVAILABLE"])

    if not args.json:
        print(f"reviewing {label} ({len(body)} chars"
              f"{', TRUNCATED' if truncated else ''}) "
              f"with {len(runnable)} reviewer(s) [profile: {profile}]...")

    tiers_run = ["fast->deep" if args.escalate else profile]
    if args.escalate:
        completed, failed, unavailable, tiers_run = escalate(
            reviewers, prompt, args, truncated, label, len(body))
        if not completed and not failed:
            print("no reviewer could run in any tier", file=sys.stderr)
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
    state = decide_state(n_completed=len(completed), n_failed=len(failed),
                         confirmed=confirmed, refuted_claims=refuted_claims,
                         rejecting=rejecting, truncated=truncated,
                         quorum=args.quorum,
                         n_out_of_scope_critical=len(oos_critical))

    report = {
        "state": state, "checked_at": now(), "reviewed": label,
        "profile": profile, "escalated": bool(args.escalate),
        "tiers_run": tiers_run,
        "truncated": truncated, "quorum": args.quorum,
        "completed": len(completed), "unavailable": unavailable,
        "failed": [{"name": r["name"], "error": r["error"]} for r in failed],
        "confirmed_findings": confirmed, "refuted_claims": refuted_claims,
        "out_of_scope_findings": out_of_scope,
        "rejecting_reviewers": rejecting,
        "results": completed,
    }

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

        print(f"\n{state}")
        if state == "REVIEW_PARTIAL":
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
        elif state == "REVIEW_PASS":
            names = ", ".join(r["name"] for r in completed)
            print(f"  Reviewed by: {names}. Absence of a finding is not proof "
                  f"of correctness — it is {len(completed)} model(s) failing to "
                  f"refute it.")

    sys.exit(STATES[state])


if __name__ == "__main__":
    main()
