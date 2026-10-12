"""Coordinator evidence readers and admission, without network or facade imports.

Python 3.8+, standard library only.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

import coordinator_paths as CP
import swarm_journal as SJ
import unit as U
import verify as V
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
