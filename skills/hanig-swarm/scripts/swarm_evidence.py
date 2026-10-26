"""Coordinator evidence readers and admission, without network or facade imports.

Python 3.8+, standard library only.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import coordinator_paths as CP
import d5_activation as D5
import swarm_journal as SJ
import swarm_state as SS
import unit as U
import verify as V
import worktree as W
from swarm_types import OutboxError


# --- merge attestation: how a code unit closes -----------------------------
#
# Closure authority for `code` is a merged PR, and the coordinator has no
# network imports, so it cannot ask GitHub anything. The evidence therefore
# arrives the way tracker acknowledgment does: a session that CAN reach the
# network records what it observed.
#
# That would be worthless on its own, because an attester naming any commit
# could close any unit. What makes it admissible is the BINDING: the head the
# attestation pins must equal the head this coordinator independently judged
# as produced, from an anchor written before the agent existed. The attester
# can lie about whether a PR merged. It cannot make this unit's produced
# commit be some other commit.
MERGE_RECEIPTS = "merge-receipts.jsonl"

# Every method changes the resulting commit differently, and an unrecorded
# method means `merged_as` cannot be interpreted at all. Fail closed rather
# than approximate head identity.
MERGE_METHODS = ("merge", "squash", "rebase")

_MERGE_REQUIRED = ("unit", "repo", "pr", "target", "head", "merged_as",
                   "method")


def _merge_shape_problem(rec):
    if not isinstance(rec, dict):
        return "not an object"
    for field in _MERGE_REQUIRED:
        if not str(rec.get(field) or "").strip():
            return f"no {field}"
    # NOT checked here: whether `merged` is true. A record saying a PR is
    # still open is a legitimate observation somebody may want on file, and
    # treating it as corruption would make the whole journal unreadable over
    # a record that is merely uninteresting. Shape answers "can this be
    # read"; admission answers "does this close the unit".
    if rec.get("method") not in MERGE_METHODS:
        return (f"method {rec.get('method')!r} is not one of "
                f"{', '.join(MERGE_METHODS)}; an unrecognised method means "
                f"`merged_as` cannot be interpreted")
    target_commit = rec.get("target_commit")
    if target_commit is not None and (
            not isinstance(target_commit, str)
            or len(target_commit) not in (40, 64)
            or any(ch not in "0123456789abcdef" for ch in target_commit)):
        return ("target_commit is not an exact lowercase 40- or 64-character "
                "Git object id")
    return None


def _read_merge_raw(state_dir):
    p = Path(state_dir) / MERGE_RECEIPTS
    try:
        with open(p) as fh:
            raw = fh.read()
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [{"kind": "unreadable", "detail": f"cannot read {p}: {exc}"}]
    lines = raw.splitlines()
    complete_tail = raw.endswith("\n")
    out, problems = [], []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        last = idx == len(lines) - 1
        try:
            rec = json.loads(line)
        except ValueError:
            if last and not complete_tail:
                problems.append({"kind": "truncated_tail",
                                 "detail": "the last merge receipt is a "
                                           "half-written line; re-record it"})
            else:
                problems.append({"kind": "corrupt",
                                 "detail": f"merge receipt line {idx + 1} was "
                                           f"written in full and does not "
                                           f"parse"})
            continue
        bad = _merge_shape_problem(rec)
        if bad:
            problems.append({"kind": "malformed",
                             "detail": f"merge receipt line {idx + 1}: {bad}"})
            continue
        out.append(rec)
    return out, problems


def load_merge_receipts(state_dir):
    """THE ONLY way to read merge receipts. Raises OutboxError.

    Same chokepoint discipline as the acknowledgment journal, for the same
    reason: detection in the reader and the decision to refuse in each caller
    is how one bug appeared in four places.
    """
    recs, problems = _read_merge_raw(state_dir)
    fatal = SJ.fatal_problems(problems)
    if fatal:
        raise OutboxError(
            "the merge receipt journal cannot be read in full, so no closure "
            "derived from it can be trusted:\n" +
            "\n".join(f"  [{f['kind']}] {f['detail']}" for f in fatal))
    return recs, problems


def _same_repo(a, b):
    """Do two repository names refer to the same place?

    Compares the last two path segments, so `git@github.com:o/r.git`,
    `https://github.com/o/r` and `o/r` all match. Anything that cannot be
    reduced to owner/name compares literally rather than being waved through.
    """
    def split(x):
        s = str(x or "").strip().rstrip("/")
        if s.endswith(".git"):
            s = s[:-4]
        for scheme in ("https://", "http://", "ssh://", "git://"):
            if s.lower().startswith(scheme):
                s = s[len(scheme):]
                break
        if "@" in s.split("/")[0]:
            s = s.split("@", 1)[1]
        parts = [p for p in s.replace(":", "/").split("/") if p]
        if len(parts) < 2:
            return None, s.lower()
        name = "/".join(parts[-2:]).lower()
        # The host is the FIRST segment, never parts[-3]. Counting back from
        # the end let https://evil.example/github.com/acme/app present
        # "github.com" as its host, which is precisely the lookalike this
        # check exists to catch. A bare "owner/name" has no host at all.
        host = parts[0].lower() if len(parts) > 2 else None
        return host, name

    ha, na = split(a)
    hb, nb = split(b)
    if not a or not b or na != nb:
        return False
    # The HOST is part of a repository's identity. Reducing to owner/name
    # alone let github.com/acme/app and evil.example:acme/app compare equal,
    # so a PR in a lookalike repository could close a unit. Compared only
    # when both sides carry one: a bare "owner/name" receipt is a legitimate
    # shorthand, not a mismatch.
    # BOTH parts, including both being absent. Comparing hosts only when
    # each side had one let a hostless anchor (`acme/app`) match a
    # host-bearing lookalike (`https://evil.example/acme/app`). Accepting
    # shorthand is what the hole was made of, so it is not accepted: two names
    # for the same repository must agree on where it lives.
    return ha == hb


VERIFY_RECEIPTS = "verify-receipts.jsonl"

_VERIFY_REQUIRED = ("unit", "claim", "verifier", "verifier_sha256",
                    "policy_sha256", "subject_head", "result")


def _verify_shape_problem(rec):
    if not isinstance(rec, dict):
        return "not an object"
    for f in _VERIFY_REQUIRED:
        if not str(rec.get(f) or "").strip():
            return f"no {f}"
    if rec.get("result") not in ("pass", "fail", "incomplete"):
        return f"result {rec.get('result')!r} is not 'pass', 'fail' or 'incomplete'"
    if rec.get("result") == "incomplete" and (
            not isinstance(rec.get("incomplete_reason"), str)
            or not rec["incomplete_reason"].strip()):
        return "incomplete receipt has no coordinator reason"
    if rec.get("claim") == V.INTEGRATION_CLAIM:
        for field in ("produced_head", "target_commit", "merge_base",
                      "candidate_tree"):
            if not str(rec.get(field) or "").strip():
                return f"integration receipt has no {field}"
        if rec.get("produced_head") != rec.get("subject_head"):
            return ("integration receipt produced_head disagrees with its "
                    "subject_head")
    return None


def _read_verify_raw(state_dir):
    p = Path(state_dir) / VERIFY_RECEIPTS
    try:
        with open(p) as fh:
            raw = fh.read()
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [{"kind": "unreadable", "detail": f"cannot read {p}: {exc}"}]
    lines = raw.splitlines()
    complete = raw.endswith("\n")
    out, problems = [], []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        last = idx == len(lines) - 1
        try:
            rec = json.loads(line)
        except ValueError:
            problems.append({"kind": "truncated_tail" if (last and not complete)
                             else "corrupt",
                             "detail": f"verification receipt line {idx + 1}"})
            continue
        bad = _verify_shape_problem(rec)
        if bad:
            problems.append({"kind": "malformed",
                             "detail": f"verification receipt line "
                                       f"{idx + 1}: {bad}"})
            continue
        out.append(rec)
    return out, problems


def load_verifications(state_dir):
    """THE ONLY way to read verification receipts. Raises OutboxError."""
    recs, problems = _read_verify_raw(state_dir)
    fatal = SJ.fatal_problems(problems)
    if fatal:
        raise OutboxError(
            "the verification journal cannot be read in full, so no claim "
            "derived from it can be trusted:\n" +
            "\n".join(f"  [{f['kind']}] {f['detail']}" for f in fatal))
    return recs, problems


def admit_verification(state_dir, unit, claim, produced, policy_digest,
                       policy, repo=None, base_commit=None,
                       target_commit=None, runner=None):
    """(receipt, refusal) for one required claim.

    Four bindings, and all of them must hold. Any one missing turns the
    verifier's word into a self-report with extra steps:

      subject   the head it verified is the head this attempt produced
      policy    the authorization it ran under is the one at the anchored base
      claim     it is the claim the unit requires
      result    it passed
    """
    if not produced:
        return None, ("this attempt has no produced commit, so there is "
                      "nothing a verification could be about")
    if not isinstance(policy, dict):
        return None, ("no anchored policy was supplied to admission, so the "
                      "verifier a receipt names cannot be checked against "
                      "anything. Refusing rather than taking the receipt's "
                      "word for which verifier ran.")
    if runner is None and claim == V.INTEGRATION_CLAIM:
        try:
            execution_policy, _ = V.RV.read_policy(state_dir)
            for executable in execution_policy.get("local", {}).values():
                if CP._inside(executable, repo):
                    return None, "verification executable is inside the operated repository"
            runner = V.RV.GitRunner(
                U.run, V.RV.resolve_executables(execution_policy.get("local"), names=("git",),
                                              excluded_roots=(repo,)))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return None, "verification executables unavailable: " + str(exc)
    runner = runner or U.run
    integration_basis = None
    if claim == V.INTEGRATION_CLAIM:
        if not target_commit:
            return None, (
                "integration-tests evidence has no recorded pre-merge target "
                "commit to bind to. Record the target commit in the merge "
                "attestation; branch-local evidence cannot substitute for it.")
        integration_basis, basis_error = V.candidate_merge_basis(
            runner, repo, produced, target_commit)
        if basis_error:
            return None, basis_error
        recs, _p = load_verifications(state_dir)
        pending = V.RV.pending_problem(state_dir, unit, integration_basis, recs)
        if pending:
            return None, pending
    else:
        recs, _p = load_verifications(state_dir)
    mine = [r for r in recs if r.get("unit") == unit
            and r.get("claim") == claim]
    if not mine:
        return None, (f"no verification receipt for {unit!r} claiming "
                      f"{claim!r}, which the unit declares it requires. Run "
                      f"it:\n  swarm.py verify --unit {unit} --claim {claim} "
                      f"--verifier NAME --path PATH")
    stale, moved_target, wrong_policy, failed, unauthorized = [], [], [], [], []
    corpus_refusals, candidate_refusals = [], []
    incomplete = []
    for r in mine:
        if str(r.get("subject_head")) != str(produced):
            stale.append(str(r.get("subject_head"))[:12])
            continue
        if claim == V.INTEGRATION_CLAIM:
            if str(r.get("target_commit")) != str(target_commit):
                moved_target.append(str(r.get("target_commit"))[:12])
                continue
            if any(r.get(field) != value
                   for field, value in integration_basis.items()):
                moved_target.append(str(r.get("target_commit"))[:12])
                continue
        if policy_digest and r.get("policy_sha256") != policy_digest:
            wrong_policy.append(str(r.get("policy_sha256"))[:12])
            continue
        if r.get("result") == "incomplete":
            incomplete.append(r)
            continue
        if r.get("result") != "pass":
            failed.append(r)
            continue
        # RE-CHECK the verifier against the policy, here, at admission, with
        # NO way to skip it. `if policy is not None` made the whole check
        # optional: a caller that passed nothing got the receipt's own word
        # about which verifier ran, which is the self-authorization this
        # exists to refuse. The argument is required and None is refused.
        entry, refusal = V.authorized(policy, r.get("verifier"),
                                      r.get("verifier_sha256"), claim)
        if refusal:
            unauthorized.append(refusal)
            continue
        evidence, refusal = V.corpus_evidence(
            runner, repo, base_commit, produced, entry)
        if refusal:
            corpus_refusals.append(refusal)
            continue
        refusal = V.corpus_change_refusal(entry, evidence, claim)
        if refusal:
            corpus_refusals.append(refusal)
            continue
        if evidence and any(r.get(field) != value
                            for field, value in evidence.items()):
            corpus_refusals.append(
                f"receipt corpus evidence does not match the anchored base "
                f"and produced commit for claim {claim!r}")
            continue
        problem = V.RV.execution_problem(r)
        if problem:
            corpus_refusals.append(problem)
            continue
        candidate = (any(k in r for k in V.MERGE_BASIS_FIELDS[1:])
                     or (r.get("execution") or {}).get("location") == "remote")
        if candidate:
            basis = {k: r.get(k) for k in V.MERGE_BASIS_FIELDS}
            if (any(not isinstance(v, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", v)
                    for v in basis.values()) or basis["produced_head"] != r["subject_head"]):
                candidate_refusals.append("incomplete or invalid candidate basis in verification receipt")
                continue
            problem = V.RV.pending_problem(state_dir, unit, basis, recs)
            if problem:
                candidate_refusals.append(problem)
                continue
            problem = V.merge_failure_problem(recs, r)
            if problem:
                if claim == V.INTEGRATION_CLAIM:
                    return None, problem
                candidate_refusals.append(problem)
                continue
        return r, None
    if candidate_refusals:
        return None, candidate_refusals[0]
    if failed:
        subject = ("the candidate merge" if claim == V.INTEGRATION_CLAIM
                   else "the produced commit")
        return None, (f"the verifier ran against {subject} and "
                      f"returned FAIL for {claim!r}. That is a result, not a "
                      f"missing receipt: fix the work rather than re-running "
                      f"until it passes.")
    if unauthorized:
        return None, (f"a verification receipt for {unit!r} is bound to the "
                      f"right head and policy, but the verifier it names is "
                      f"not authorized by that policy: {unauthorized[0]}")
    if corpus_refusals:
        return None, corpus_refusals[0]
    if moved_target:
        return None, (
            f"integration verification for {unit!r} tested target commit(s) "
            f"{', '.join(sorted(set(moved_target)))}, but the merge was made "
            f"from target {str(target_commit)[:12]}. The target moved after "
            f"the check, so that evidence is invalid; re-run "
            f"integration-tests against the new candidate merge.")
    if wrong_policy:
        return None, (f"verification for {unit!r} ran under policy "
                      f"{', '.join(sorted(set(wrong_policy)))}, but this "
                      f"attempt was anchored to {str(policy_digest)[:12]}. "
                      f"The rules changed after the check.")
    if incomplete:
        return None, f"verification for {unit!r} is incomplete; no passing receipt for {claim!r}"
    return None, (f"verification for {unit!r} names head(s) "
                  f"{', '.join(sorted(set(stale)))}, but this attempt "
                  f"produced {produced[:12]}. A pass for another commit is "
                  f"not a pass for this one.")


def _receipt_reason(state, unit, attempt_dir):
    """The machine-readable REASON a check came back INCOMPLETE.

    Attested, because this drives a continuation: an agent that can write its
    own REASON can ask for another turn. Existed in two copies, this one and
    an inline duplicate in `advance`, so a fix to either left the other.
    """
    rp, _why = attested_receipt(state, unit, attempt_dir)
    for note in ((rp or {}).get("notes") or []):
        if str(note).startswith("REASON="):
            return str(note).split("=", 1)[1]
    return ""


def attested_receipt(state, unit, attempt_dir):
    """THE ONLY way to read an attempt's receipt for a decision.

    Returns (receipt, why_not). A receipt is admissible when its bytes match
    the digest the coordinator recorded for a check IT caused; otherwise the
    file is a claim by whoever wrote it, and the agent can write there.

    This exists because report.py was taught to check attestation and the FIVE
    readers in this module were not. Every one of them opened receipt.json and
    decided its own trust, which is how `produced_head` -- the commit a merge
    attestation gets pinned to -- came to be taken from an agent-writable file
    and then STORED as coordinator state. Same shape as the base_commit defect
    three review rounds ago, in a different field, found only by looking at
    the neighbours instead of the instance in front of me.

    One accessor, no fallback, and a test asserts nothing else opens the file.
    """
    if not attempt_dir:
        return None, "this unit has no attempt directory"
    recorded = ((((state.get("units") or {}).get(unit) or {})
                 .get("attempt_receipt_seals") or {})
                .get(Path(attempt_dir).name))
    if not recorded:
        return None, ("nothing records this coordinator causing a check for "
                      "attempt %s, so any receipt there is a claim by "
                      "whoever wrote it" % Path(attempt_dir).name)
    path = Path(attempt_dir) / U.RECEIPT
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    if hashlib.sha256(raw).hexdigest() != recorded:
        return None, (f"the receipt at {path} is not the file this "
                      f"coordinator's check wrote; it was replaced afterwards")
    try:
        rec = json.loads(raw)
    except ValueError as exc:
        return None, f"the receipt at {path} is not readable JSON: {exc}"
    if not isinstance(rec, dict):
        return None, f"the receipt at {path} is not a JSON object"
    attempt = Path(attempt_dir).name
    if rec.get("task_id") != unit or rec.get("attempt_id") != attempt:
        return None, (f"the attested receipt identifies unit "
                      f"{rec.get('task_id')!r}, attempt "
                      f"{rec.get('attempt_id')!r}, not unit {unit!r}, "
                      f"attempt {attempt!r}; cross-wired evidence is refused")
    return rec, None


# Shared with submission validation through the swarm facade.
ISOLATION_MARKER_NAME = ".swarm-isolation-applied-v1"

_HERE = Path(__file__).resolve().parent


def trusted_base(state, unit, attempt_dir):
    """THE ONLY way to obtain the base commit an attempt was anchored to.

    Three rounds of review found the same defect in three places, each time
    because some caller reached for the base wherever it was handy. The launch
    record is the handiest place and the wrong one: it sits on a filesystem
    the agent's Unix user can write, so reading it and then treating the value
    as trusted launders exactly what the trust was for.

    So there is one function, it reads ONLY coordinator state, and it has no
    fallback. Returning None means "this attempt cannot be verified", which is
    an honest answer; inventing a base from a file the agent can edit is not.
    A test asserts nothing else reads base_commit out of a launch record.
    """
    if not attempt_dir:
        return None
    facts = SS.trusted_launch_facts(state, unit, attempt_dir)
    return facts.get("base_commit") if facts else None


def trusted_produced_head(state, unit, attempt_dir):
    """The one judgment basis pinned for this exact attempt; no fallback."""
    if not attempt_dir:
        return None
    us = (state.get("units") or {}).get(unit) or {}
    return (us.get("attempt_produced_heads") or {}).get(
        Path(attempt_dir).name)


def _capture_artifact_basis(state, unit, unit_dir, u, enable_slurm_exit=False):
    """Digest every declared artifact BEFORE anything is dispatched.

    B1, and the strongest thing this function does is happen EARLY. The done
    predicate concludes "the write root is exclusive, so an artifact found
    there was produced here", and that inference needs the write root to have
    been empty of that artifact when the attempt started. Nothing checked it,
    so a unit that declared its input path as its output recorded a file it
    never wrote as produced evidence and read DONE.

    PINNED ONCE PER ATTEMPT, and the `if attempt in bases` guard is the whole
    of it. `_submit` is re-entered on the code-launch recovery path and a
    continuation re-runs the agent inside the SAME write root; either one, if
    it re-digested, would adopt whatever the previous turn left behind as the
    baseline it is about to be judged against. That is `produced_head`'s bug
    -- a value that was never cleared and so was inherited -- and this is
    where it would have been reproduced a third time.

    The outputs come from the PLAN, never from the attempt's `unit.json`: the
    party being judged can write that file, and a baseline over a list it
    chose is a baseline it chose.
    """
    us = state.setdefault("units", {}).setdefault(unit, {})
    attempt = Path(unit_dir).name
    bases = us.setdefault("attempt_artifact_bases", {})
    if attempt in bases:
        return bases[attempt]
    declared = [str(o) for o in (u.get("outputs") or [])]
    present, missing, escaped = W.outputs_present(
        unit_dir, {"declared_outputs": declared})
    bases[attempt] = {
        "schema_version": W.ARTIFACT_BASIS_SCHEMA,
        # Both identities, so a basis lifted from another attempt or another
        # unit is useless rather than merely unlikely to be lifted.
        "attempt_id": attempt,
        "unit_id": unit,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "declared": declared,
        # Absent is the ordinary case and the strongest one: nothing was
        # there, so anything there afterwards can only be new.
        "absent": sorted(missing),
        "escaped": sorted(escaped),
        "present": U.fingerprint_outputs(unit_dir, present),
    }
    if enable_slurm_exit and u.get("kind") == "slurm":
        bases[attempt]["slurm_exit_record"] = {"version": 1, "path": U.SLURM_EXIT_RECORD}
    return bases[attempt]


def raw_artifact_basis(state, unit, attempt_dir):
    """Transport coordinator state without turning invalid capability into absence."""
    if not attempt_dir:
        return None
    return ((((state.get("units") or {}).get(unit) or {})
             .get("attempt_artifact_bases") or {}).get(Path(attempt_dir).name))


def trusted_artifact_basis(state, unit, attempt_dir):
    """The pre-dispatch artifact digest pinned for THIS attempt; no fallback.

    Never derived, never defaulted, and never read from the attempt directory
    or the launch record. Returning None means "this attempt cannot be judged
    for production", which the checker turns into a refusal.
    """
    if not attempt_dir:
        return None
    basis = ((((state.get("units") or {}).get(unit) or {})
              .get("attempt_artifact_bases") or {}).get(Path(attempt_dir).name))
    if W.artifact_basis_problem(basis, attempt_dir,
                                {"id": unit, "task_id": unit}):
        return None
    return basis


def trusted_isolation_facts(state, u, attempt_dir):
    """Applied submission boundary for this attempt, from state only.

    The job script and unit.json live in the attempt namespace and are
    audit-only.  A declaration removed or changed after dispatch cannot reuse
    stale facts: the canonical profile digest must still match the plan.
    """
    if not attempt_dir or not isinstance(u, dict) or not u.get("isolation"):
        return None
    attempt = Path(attempt_dir).name
    us = (state.get("units") or {}).get(u.get("id")) or {}
    facts = (us.get("attempt_isolation_facts") or {}).get(attempt)
    if not isinstance(facts, dict):
        return None
    canonical = json.dumps(
        u["isolation"], sort_keys=True, separators=(",", ":"))
    expected_digest = hashlib.sha256(canonical.encode()).hexdigest()
    expected_root = str(Path(attempt_dir).resolve())
    expected_marker = str(Path(expected_root) / ISOLATION_MARKER_NAME)
    token = facts.get("application_token_sha256")
    if not (
            facts.get("schema_version") == 1
            and facts.get("unit_id") == u.get("id")
            and facts.get("attempt_id") == attempt
            and facts.get("applied_to_submission") is True
            and facts.get("mechanism") == "container-host-bind-write-scope"
            and facts.get("backend") == u["isolation"].get("backend")
            and facts.get("image") == u["isolation"].get("image")
            and facts.get("profile_sha256") == expected_digest
            and facts.get("writable_host_binds") == [expected_root]
            and facts.get("application_marker") == expected_marker
            and isinstance(token, str)
            and re.fullmatch(r"[0-9a-f]{64}", token) is not None
            and facts.get("read_only_host_binds") == [
                os.path.normpath(str(p))
                for p in (u["isolation"].get("read_only") or [])]):
        return None
    return facts


CHECK_RESULT_PREFIX = "SWARM_CHECK_RESULT"


def _reported_check_result(result_channel):
    """Parse the sole exact authority result from its dedicated channel.

    The coordinator owns the underlying anonymous file and gives only its fd
    to the checker. stdout and stderr are diagnostics, never inputs here.

    Returns ``(result, problem)``. Exactly one line is accepted; extra text is
    malformed and more than one line is ambiguous even when values agree.
    """
    lines = (result_channel or "").splitlines()
    if not lines:
        return None, None
    if len(lines) != 1:
        return None, (f"checker result channel contained {len(lines)} lines; "
                      "exactly one is required")
    prefix = CHECK_RESULT_PREFIX + " "
    line = lines[0]
    if not line.startswith(prefix):
        return None, (f"malformed checker result {line!r}; expected "
                      f"'{CHECK_RESULT_PREFIX} ' followed by canonical JSON")
    payload = line[len(prefix):]
    try:
        result = json.loads(payload)
    except (TypeError, ValueError) as exc:
        return None, f"malformed checker result {line!r}: {exc}"
    required = {"produced_head", "receipt_sha256"}
    if not isinstance(result, dict) or set(result) != required:
        return None, ("checker result must be an object with exactly "
                      "produced_head and receipt_sha256")
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if payload != canonical:
        return None, "checker result JSON is not in canonical form"
    digest = result.get("receipt_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None, ("checker result receipt_sha256 must be exactly 64 "
                      "lowercase hexadecimal characters")
    produced = result.get("produced_head")
    if produced is not None and (
            not isinstance(produced, str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", produced) is None):
        return None, ("checker result produced_head must be null or a 40/64 "
                      "character lowercase hexadecimal object id")
    return result, None


RECEIPT_PROVENANCE_LIMIT = (
    "The coordinator has no portable process-group or cgroup handle that "
    "proves every same-UID descendant of a Paseo agent or scheduler job is "
    "dead. The digest binds the trusted checker's receipt bytes, but does "
    "not prove that an escaped background process stopped mutating other "
    "artifacts during or after the check. OS-enforced isolation requires a "
    "different principal, container, or scheduler-exposed cgroup barrier."
)


def _record_receipt_provenance(state, unit, attempt_dir, digest):
    """Record that the COORDINATOR caused the receipt now on disk.

    An agent can write `receipt.json` into its own attempt directory, and the
    report deliberately lets a receipt outrank coordinator state because a
    receipt judged artifacts. Both are reasonable and together they are a
    hole: the agent's self-report wins.

    A seal cannot close it, because a seal handed in as an argument can be
    computed by whoever hands it in. What cannot be forged is a fact the
    coordinator wrote to its OWN state, which the threat model trusts and the
    agent is never given the path to. So after a check the coordinator has
    caused, it digests the receipt that check produced and records that here.
    A receipt with no matching entry was not produced by a check this
    coordinator ran, and the report says so instead of trusting it.
    """
    if not attempt_dir:
        return
    us = state.setdefault("units", {}).setdefault(unit, {})
    if not digest:
        # The check wrote no receipt, so the coordinator vouches for NOTHING
        # here any more. Leaving an earlier seal in place let a favourable
        # receipt from a previous check keep validating: restore that file and
        # it still matches the stale seal, outranking the newer FAILED state.
        # Withdrawing the seal is the whole point of holding it.
        (us.get("attempt_receipt_seals") or {}).pop(Path(attempt_dir).name,
                                                    None)
        (us.get("attempt_receipt_provenance_limits") or {}).pop(
            Path(attempt_dir).name, None)
        return
    us.setdefault("attempt_receipt_seals", {})[Path(attempt_dir).name] = digest
    # DECLARED LIMIT AT THE ACCEPTANCE POINT. Slurm terminal accounting and a
    # terminal/idle Paseo lifecycle do not expose one portable handle with
    # which this coordinator can prove every escaped same-UID descendant is
    # gone. Do not silently upgrade this byte binding into process isolation.
    us.setdefault("attempt_receipt_provenance_limits", {})[
        Path(attempt_dir).name] = RECEIPT_PROVENANCE_LIMIT


def trusted_record_seal(state, unit, attempt_dir):
    """THE ONLY way to obtain an attempt's launch-record seal.

    Same shape and same reason as `trusted_base`: coordinator state, no
    fallback. None means "this attempt cannot be judged against its anchor",
    which callers must treat as a refusal rather than as permission to read
    the record unsealed.
    """
    if not attempt_dir:
        return None
    us = (state.get("units") or {}).get(unit) or {}
    return (us.get("attempt_record_seals") or {}).get(Path(attempt_dir).name)


def admit_merge(state_dir, unit, produced, expect_repo=None, repo=None,
                require_target_binding=False, expect_target=None):
    """(receipt, refusal). A merged-PR attestation, admitted or refused.

    `produced` is the commit this coordinator judged the attempt to have
    produced. An attestation that pins a different head is refused: that is
    the whole binding, and without it any attester closes any unit.

    Active D5 admits only the operator's synchronous merge receipt readback.
    Coordinator closure consumption remains outside that restricted profile;
    neither a legacy attestation nor a D5 receipt enables DONE here.
    """
    if D5.d5_active():
        raise OutboxError("D5 active: merge receipt consumption is outside the supported merge profile")
    if not produced:
        return None, ("this attempt has no produced commit, so there is "
                      "nothing a merge could be bound to. A unit reaches "
                      "READY_FOR_PR only after producing one.")
    recs, _problems = load_merge_receipts(state_dir)
    mine = [r for r in recs if r.get("unit") == unit
            and r.get("merged") is True]
    if not mine:
        return None, (f"no merge receipt for {unit!r}. Record one from a "
                      f"machine that can see the PR:\n  swarm.py merge "
                      f"--unit {unit} --pr URL --head {produced[:12]} "
                      f"--target BRANCH --merged-as SHA --method merge")
    wrong_repo, wrong_target, target_refusals = [], [], []
    for r in mine:
        if str(r.get("head")) != str(produced):
            continue
        if expect_repo and not _same_repo(r.get("repo"), expect_repo):
            # Collect and keep looking. Returning on the first mismatch let a
            # single wrong-repository receipt mask every correct one appended
            # after it, so one attester slip parked the unit forever.
            wrong_repo.append(str(r.get("repo")))
            continue
        if expect_target and r.get("target") != expect_target:
            wrong_target.append(str(r.get("target")))
            continue
        claimed_target = r.get("target_commit")
        if require_target_binding:
            if not claimed_target:
                target_refusals.append(
                    "the integration-tests unit has no recorded pre-merge "
                    "target commit")
                continue
            if not repo:
                target_refusals.append(
                    "the receipt names a pre-merge target commit, but no "
                    "coordinator-held repository was supplied to check it")
                continue
            actual_target, target_error = V.target_before_merge(
                U.run, repo, produced, r.get("merged_as"), r.get("method"),
                claimed_target)
            if target_error:
                target_refusals.append(target_error)
                continue
            if actual_target != claimed_target:
                target_refusals.append(
                    f"the merge object establishes pre-merge target "
                    f"{actual_target[:12]}, not claimed target "
                    f"{str(claimed_target)[:12]}; the target moved after the "
                    f"integration check")
                continue
        return r, None
    if wrong_repo:
        return None, (
            f"{len(wrong_repo)} merge receipt(s) for {unit!r} pin the right "
            f"head but name repositor(y/ies) {', '.join(sorted(set(wrong_repo)))}, "
            f"and this attempt was anchored to {expect_repo!r}. The attester "
            f"is trusted to report what it saw, not to decide which "
            f"repository this unit belongs to. Record a corrected receipt; "
            f"the wrong one does not block it.")
    if wrong_target:
        return None, (
            f"merge receipt(s) for {unit!r} target "
            f"{', '.join(sorted(set(wrong_target)))}, not the plan's target "
            f"{expect_target!r}. Record a corrected receipt; a wrong target "
            f"does not block a later correct one.")
    if target_refusals:
        return None, (
            f"no merge receipt for {unit!r} has an admissible pre-merge "
            f"target: {target_refusals[0]}. A connected session must supply "
            f"the merge Git objects locally; the coordinator never contacts "
            f"a forge.")
    heads = ", ".join(sorted({str(r.get("head"))[:12] for r in mine}))
    return None, (
        f"{len(mine)} merge receipt(s) for {unit!r} pin head(s) {heads}, but "
        f"this attempt produced {produced[:12]}. A merge of something else "
        f"does not close this unit, and re-pointing the receipt would defeat "
        f"the only check the coordinator can make without a network.")



def _authority_result_sink():
    """Anonymous result file with no live handle on fd 0, 1, or 2.

    The complete containment property is split deliberately across two
    chokepoints: this function removes every standard-fd alias before launch;
    U.run replaces stdin, captures stdout/stderr, closes every other fd except
    the explicit >=3 result fd, and contains timeout cleanup to the new process
    group. Thus an agent-controlled git/paseo descendant neither inherits nor
    keeps alive a handle to this file.
    """
    sink = tempfile.TemporaryFile(mode="w+b")
    if sink.fileno() >= 3:
        return sink
    duplicates = []
    try:
        promoted_fd = sink.fileno()
        while promoted_fd < 3:
            promoted_fd = os.dup(sink.fileno())
            duplicates.append(promoted_fd)
        # Do NOT pop before fdopen succeeds. fdopen takes ownership of the
        # descriptor only on success; popping first means an exception leaves
        # that one fd owned by nobody and closed by no one.
        promoted = os.fdopen(duplicates[-1], "w+b")
        duplicates.pop()
    except BaseException:
        for duplicate in duplicates:
            os.close(duplicate)
        sink.close()
        raise
    sink.close()
    for duplicate in duplicates:
        os.close(duplicate)
    return promoted


def _check(unit_dir, launch_facts=None, artifact_basis=None,
           isolation_facts=None, isolation_required=False):
    argv = [sys.executable, str(_HERE / "unit.py"), "check", str(unit_dir)]
    if U.SACCT_UNAVAILABLE:
        argv.append("--skip-sacct")
    # The separate judge receives the complete authority snapshot directly
    # from coordinator state. It never opens the launch audit record.
    if launch_facts:
        argv += ["--launch-facts", json.dumps(
            launch_facts, sort_keys=True, separators=(",", ":"))]
    # Same channel, same provenance, for the same reason: the pre-dispatch
    # artifact digest decides admission, so it travels BY VALUE from
    # coordinator state. Passing a path to it, or letting the checker find it
    # beside the attempt, would put the baseline where the judged party can
    # write. What an onlooker could read off this argv is a digest of files it
    # can already read; what it cannot do is change the baseline.
    if artifact_basis:
        argv += ["--artifact-basis", json.dumps(
            artifact_basis, sort_keys=True, separators=(",", ":"))]
    if isolation_facts:
        argv += ["--isolation-facts", json.dumps(
            isolation_facts, sort_keys=True, separators=(",", ":"))]
    if isolation_required:
        argv.append("--isolation-required")
    # Anonymous coordinator-owned storage is the authority channel. stdout
    # contains diagnostics derived from agent-writable artifacts and cannot
    # become authority merely by printing a reserved-looking prefix.
    with _authority_result_sink() as result_sink, _authority_result_sink() as scheduler_sink:
        result_fd = result_sink.fileno()
        scheduler_fd = scheduler_sink.fileno()
        argv += ["--result-fd", str(result_fd)]
        argv += ["--sacct-status-fd", str(scheduler_fd)]
        rc, out, err = U.run(
            argv, timeout=300, pass_fds=(result_fd, scheduler_fd))
        result_sink.seek(0)
        result_channel = result_sink.read(4097).decode("ascii", "replace")
        scheduler_sink.seek(0)
        if scheduler_sink.read(2) == b"1":
            U.SACCT_UNAVAILABLE = True
    return rc, out or "", err or "", result_channel
