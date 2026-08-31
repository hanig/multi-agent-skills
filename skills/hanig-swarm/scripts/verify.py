#!/usr/bin/env python3
"""Authorized, content-pinned verifiers.

A production receipt says a change was PRODUCED. It says nothing about whether
the change is any good, and `PRODUCTION_DENIES` spells that out. A verifier is
what can say more: tests pass, a schema validates, a benchmark did not
regress.

Three properties make a verifier's word admissible, and all three must hold or
none of it means anything:

  AUTHORIZED   the policy naming it is read from the ANCHORED BASE COMMIT, not
               from the agent's branch. An agent that can add a verifier to
               its own branch authorizes itself, which is the whole game.
  PINNED       the policy records the verifier's exact content digest, and the
               bytes that run are the bytes that hashed. A path is not an
               identity: the file at that path can change between the check
               and the run.
  BOUND        the receipt names the production receipt and head it verified.
               A pass for some other commit is not a pass for this one.

WHAT THIS DOES NOT ESTABLISH. The agent runs as the same Unix user as the
coordinator, so it can write any file the coordinator can, including the
launch record and the attempt receipts. No arrangement of files defends
against that, and every "the agent could edit X" finding is true for every X
this module might add. What is defended is an agent that fails to do the work
and an operator who runs the wrong thing: authorization cannot be granted by a
candidate branch, a named verifier cannot be swapped for other bytes, and a
pass cannot be reused for another commit or under other rules. A HOSTILE agent
is out of scope and would need a container or a separate Unix user, which is
what the receipts have always said about isolation.

Python 3.8+, standard library only.
"""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

POLICY_FILE = "verifiers.json"

# A mixed-version rollout must fail closed rather than silently accept a
# policy written for different rules.
POLICY_SCHEMA = 1

MAX_POLICY_BYTES = 1_000_000
MAX_VERIFIER_BYTES = 5_000_000


def _git(runner, repo, *args, timeout=60):
    rc, out, err = runner(["git", "-C", str(repo)] + list(args),
                          timeout=timeout)
    return rc, (out or ""), (err or "").strip()


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_file(path):
    """(digest, size, error). Bounded: an unbounded read is a way to be
    stopped by a file somebody chose."""
    try:
        st = os.stat(path)
    except OSError as exc:
        return None, None, f"cannot stat {path}: {exc}"
    if not os.path.isfile(path):
        return None, None, f"{path} is not a regular file"
    if st.st_size > MAX_VERIFIER_BYTES:
        return None, None, (f"{path} is {st.st_size} bytes, over the "
                            f"{MAX_VERIFIER_BYTES} limit for a verifier")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return None, None, f"cannot read {path}: {exc}"
    return digest_bytes(data), len(data), None


def read_policy(runner, repo, base_commit):
    """(policy, digest, error), read from the ANCHORED BASE.

    NOT from the working tree, and not from HEAD. The agent owns both. Reading
    the policy from the commit that was anchored before the agent started is
    what stops a candidate change from authorizing its own verifier, and it is
    the only part of this that an agent cannot reach.
    """
    if not base_commit:
        return None, None, ("no anchored base commit, so there is no "
                            "authorization source an agent could not have "
                            "written")
    # --no-replace-objects. `git show A:...` follows refs/replace/A, and the
    # agent can write a replacement ref pointing at a commit of its own, so
    # the "anchored" base resolves to a policy it authored. The anchor is only
    # an anchor if the object it names cannot be swapped underneath it.
    rc, out, err = _git(runner, repo, "--no-replace-objects", "show",
                        f"{base_commit}:{POLICY_FILE}")
    if rc != 0:
        return None, None, (f"no {POLICY_FILE} at the anchored base "
                            f"{str(base_commit)[:12]}: nothing authorizes any "
                            f"verifier for this unit ({err[:120]})")
    raw = out.encode() if isinstance(out, str) else out
    if len(raw) > MAX_POLICY_BYTES:
        return None, None, f"{POLICY_FILE} is over {MAX_POLICY_BYTES} bytes"
    try:
        policy = json.loads(raw)
    except ValueError as exc:
        return None, None, f"{POLICY_FILE} at the base does not parse: {exc}"
    if not isinstance(policy, dict):
        return None, None, f"{POLICY_FILE} is not an object"
    got = policy.get("schema_version")
    if got != POLICY_SCHEMA:
        return None, None, (
            f"{POLICY_FILE} declares schema_version {got!r}; this build "
            f"understands {POLICY_SCHEMA}. Refusing rather than guessing "
            f"which rules were meant.")
    if not isinstance(policy.get("verifiers"), list):
        return None, None, f"{POLICY_FILE} declares no 'verifiers' list"
    return policy, digest_bytes(raw), None


def authorized(policy, name, digest, claim):
    """(entry, refusal). Is this exact verifier allowed to make this claim?"""
    entries = [v for v in (policy.get("verifiers") or [])
               if isinstance(v, dict) and v.get("name") == name]
    if not entries:
        return None, (f"the policy at the anchored base authorizes no "
                      f"verifier named {name!r}")
    for v in entries:
        if v.get("sha256") != digest:
            continue
        claims = v.get("claims")
        if not isinstance(claims, list):
            return None, (
                f"verifier {name!r} declares claims={claims!r}, which is not "
                f"a list. `claim not in \"tests-pass-and-more\"` is a "
                f"substring test, so a string there would grant every claim "
                f"spelled inside it.")
        if claim not in claims:
            return None, (
                f"verifier {name!r} is authorized, but not to claim "
                f"{claim!r}. It may claim: {', '.join(claims) or 'nothing'}. "
                f"A verifier that can assert anything asserts nothing.")
        return v, None
    known = ", ".join(sorted({str(v.get("sha256"))[:12] for v in entries}))
    return None, (
        f"verifier {name!r} hashes to {str(digest)[:12]}, and the policy "
        f"authorizes {known}. The file at that path is not the file that was "
        f"approved.")


def head_matches(runner, repo, expect_head):
    """(ok, detail). Is the working tree at the commit being claimed about?

    The verifier used to run wherever the operator happened to be standing,
    while the receipt recorded the produced head regardless. Test failing
    commit A, move to passing commit B, run verify from B, and the receipt
    says A passed.
    """
    rc, head, _ = _git(runner, repo, "rev-parse", "HEAD")
    if rc != 0:
        return False, f"cannot read HEAD in {repo!r}"
    head = head.strip()
    if head != str(expect_head):
        return False, (
            f"the working tree is at {head[:12]} but this attempt produced "
            f"{str(expect_head)[:12]}. A verifier run against a different "
            f"commit says nothing about this one: check out the produced "
            f"commit before verifying.")
    rc, status, err = _git(runner, repo, "status", "--porcelain")
    if rc != 0:
        # "could not look" is not "nothing there". Treating a failed status
        # as clean let a modified working tree through whenever git happened
        # to be unhappy.
        return False, (f"cannot read git status in {repo!r} ({err[:100]}), so "
                       f"whether the tree matches the commit being claimed "
                       f"about is unknown")
    if status.strip():
        return False, ("the working tree has uncommitted changes, so what "
                       "the verifier tests is not the commit being claimed "
                       "about")
    return True, None


def run_pinned(runner, path, expect_digest, args=None, timeout=900,
               cwd=None):
    """Execute the bytes that hashed, not the path that was named.

    Hashing a file and then executing the path re-reads it, so the bytes that
    ran need never be the bytes that were checked. The verified bytes are
    copied to a private temporary file and that copy is executed.
    """
    got, _size, err = digest_file(path)
    if err:
        return None, err
    if got != expect_digest:
        return None, (f"{path} hashes to {got[:12]}, expected "
                      f"{str(expect_digest)[:12]}")
    tmpdir = tempfile.mkdtemp(prefix="pinned-verifier-")
    try:
        copy = os.path.join(tmpdir, "verifier")
        shutil.copyfile(path, copy)
        after, _s, err2 = digest_file(copy)
        if err2 or after != expect_digest:
            return None, "the verified bytes changed while being copied"
        os.chmod(copy, 0o500)
        before = None
        if cwd:
            _rc, before, _e = _git(runner, cwd, "rev-parse", "HEAD")
        rc, out, errout = runner([copy] + list(args or []), timeout=timeout,
                                 cwd=cwd)
        if cwd:
            # The HEAD check and the run were separate observations, so the
            # tree could be reset between them and the verifier would test a
            # commit the receipt does not name. Re-observing afterwards does
            # not make the pair atomic, but it turns a silent substitution
            # into a refusal.
            _rc2, after, _e2 = _git(runner, cwd, "rev-parse", "HEAD")
            if before and after and before.strip() != after.strip():
                return None, (
                    f"HEAD moved from {before.strip()[:12]} to "
                    f"{after.strip()[:12]} while the verifier ran, so what it "
                    f"tested is not the commit this would claim about")
        return {"exit_code": rc, "stdout": (out or "")[-4000:],
                "stderr": (errout or "")[-2000:]}, None
    except OSError as exc:
        return None, f"cannot run the pinned verifier: {exc}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
