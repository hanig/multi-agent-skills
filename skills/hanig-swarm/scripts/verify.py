#!/usr/bin/env python3
"""Authorized, content-pinned verifiers.

A production receipt says a change was PRODUCED. It says nothing about whether
the change is any good, and `PRODUCTION_DENIES` spells that out. A verifier is
what can say more: tests pass, a schema validates, a benchmark did not
regress.

Four properties make a verifier's word admissible when its policy declares a
corpus. The existing three still apply unchanged when it does not:

  AUTHORIZED   the policy naming it is read from the ANCHORED BASE COMMIT, not
               from the agent's branch. An agent that can add a verifier to
               its own branch authorizes itself, which is the whole game.
  PINNED       the policy records the verifier's exact content digest, and the
               bytes that run are the bytes that hashed. A path is not an
               identity: the file at that path can change between the check
               and the run.
  CORPUS       every declared verdict input is unchanged from the anchored
               base. Running pinned verifier bytes against candidate-edited
               tests only pins the program that consumed the wrong tests.
  BOUND        the receipt names the production receipt and head it verified.
               A pass for some other commit is not a pass for this one.

The reserved integration-tests claim adds a second binding: it runs in a
disposable candidate merge of the produced head into an exact target commit,
and its receipt names that target and their unique merge base. The connected
merge operator also admits changed-tests-stable against this same binding when
the target policy declares it. Ordinary swarm verification keeps its existing
anchored-base authorization and produced-head behavior for other claims.

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
import base64
import hashlib
import json
import os
import posixpath
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import child_environment as CE
import remote_verify as RV

POLICY_FILE = "verifiers.json"
INTEGRATION_CLAIM = "integration-tests"
MERGE_VERIFIER = "merge-precondition"
MERGE_VERIFIER_PATH = "verifiers/integration_tests.py"
STABILITY_CLAIM = "changed-tests-stable"
STABILITY_VERIFIER_PATH = "verifiers/changed_tests_stable.py"
MERGE_BASIS_FIELDS = ("produced_head", "target_commit", "merge_base", "candidate_tree")

# A mixed-version rollout must fail closed rather than silently accept a
# policy written for different rules.
POLICY_SCHEMA = 1

MAX_POLICY_BYTES = 1_000_000
MAX_VERIFIER_BYTES = 5_000_000
MAX_CORPUS_PATHS = 10_000
MAX_CORPUS_FILE_BYTES = 256_000_000
MAX_CORPUS_TOTAL_BYTES = 1_000_000_000


def _git(runner, repo, *args, timeout=60):
    rc, out, err = runner(["git", "-C", str(repo)] + list(args),
                          timeout=timeout)
    return rc, (out or ""), (err or "").strip()


def _isolated_git(runner, repo, *args, timeout=60):
    """Run Git without host or user configuration.

    Candidate construction must not inherit hooks, filters, merge drivers, or
    rerere state from the operated repository. ``env`` is used instead of a
    process-global environment mutation so concurrent coordinator work keeps
    its own environment.
    """
    unset = (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_EXEC_PATH",
        "GIT_NAMESPACE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
        "GIT_ATTRIBUTES_FILE", "GIT_TEMPLATE_DIR",
    )
    env_program = shutil.which("env", path=os.defpath)
    git_program = getattr(runner, "git_program", None) or shutil.which("git", path=os.defpath)
    if not env_program or not git_program:
        return 127, "", "system env or git executable is unavailable"
    argv = [env_program]
    for name in unset:
        argv.extend(("-u", name))
    argv.extend((
        "GIT_CONFIG_COUNT=0", "GIT_CONFIG_NOSYSTEM=1",
        f"GIT_CONFIG_SYSTEM={os.devnull}",
        f"GIT_CONFIG_GLOBAL={os.devnull}", "GIT_ATTR_NOSYSTEM=1",
        f"HOME={os.devnull}", f"XDG_CONFIG_HOME={os.devnull}",
        f"PATH={os.defpath}", "GIT_NO_LAZY_FETCH=1",
        "GIT_TERMINAL_PROMPT=0", git_program, "-C", str(repo),
    ))
    rc, out, err = runner(argv + list(args), timeout=timeout)
    return rc, (out or ""), (err or "").strip()


def _isolated_checkout(runner, repo, commit, prefix):
    """Create a fresh repository that borrows only ``repo``'s object bytes."""
    tmp = tempfile.mkdtemp(prefix=prefix)
    tree = os.path.join(tmp, "tree")
    try:
        os.mkdir(tree)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, f"cannot create isolated checkout: {exc}"
    init_args = ["init", "--quiet", "--template="]
    if len(str(commit)) == 64:
        init_args.append("--object-format=sha256")
    rc, _out, err = _isolated_git(
        runner, tree, *init_args, timeout=60)
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, f"cannot initialize isolated checkout: {err[:200]}"
    rc, objects, err = _isolated_git(
        runner, repo, "--no-replace-objects", "rev-parse", "--git-path",
        "objects")
    if rc != 0 or not objects.strip():
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, (
            f"cannot locate the supplied repository objects: {err[:200]}")
    object_dir = objects.strip()
    if not os.path.isabs(object_dir):
        object_dir = os.path.join(str(repo), object_dir)
    object_dir = os.path.realpath(object_dir)
    if "\n" in object_dir or not os.path.isdir(object_dir):
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, "the supplied Git object directory is unavailable"
    alternate = os.path.join(tree, ".git", "objects", "info", "alternates")
    try:
        with open(alternate, "w") as fh:
            fh.write(object_dir + "\n")
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, f"cannot bind supplied Git objects: {exc}"
    rc, _out, err = _isolated_git(
        runner, tree, "-c", "core.hooksPath=/dev/null", "checkout",
        "--detach", "--quiet", str(commit), timeout=300)
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, (
            f"cannot check out {str(commit)[:12]} in isolation: {err[:200]}")
    return tmp, tree, None


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


def read_policy(runner, repo, base_commit, source="anchored base"):
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
        return None, None, (f"no {POLICY_FILE} at the {source} "
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


def declared_corpus(entry):
    """(paths, refusal) for one authorized verifier entry.

    Corpus entries are exact repository-relative file names, not pathspecs.
    Refusing non-canonical spellings makes the policy, diff, and receipt use
    one identity for each file instead of comparing aliases such as a/../b.
    """
    raw = entry.get("corpus")
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, (f"verifier {entry.get('name')!r} declares corpus="
                      f"{raw!r}, which is not a list")
    if len(raw) > MAX_CORPUS_PATHS:
        return None, (f"verifier {entry.get('name')!r} declares "
                      f"{len(raw)} corpus paths, over the "
                      f"{MAX_CORPUS_PATHS} limit")
    paths = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value:
            return None, (f"verifier {entry.get('name')!r} corpus[{index}]="
                          f"{value!r}; each corpus path must be a non-empty "
                          f"string")
        if "\x00" in value or value.startswith("/") or value != value.strip():
            return None, (f"verifier {entry.get('name')!r} corpus path "
                          f"{value!r} is not a repository-relative path")
        normal = posixpath.normpath(value)
        if normal != value or normal in (".", "..") \
                or normal.startswith("../"):
            return None, (f"verifier {entry.get('name')!r} corpus path "
                          f"{value!r} is not canonical and repository-relative")
        if value in paths:
            return None, (f"verifier {entry.get('name')!r} declares corpus "
                          f"path {value!r} more than once")
        paths.append(value)
    return paths, None


def _digest_base_blob(repo, base_commit, path, git_program="git"):
    """(sha256, size, error) for exact blob bytes at the anchored base.

    A blob is not necessarily a regular file: a symlink is a blob containing
    its target spelling.  The verifier checkout would follow that link and
    read different bytes, so admissibility starts with the anchored tree entry
    mode rather than `cat-file`'s object type alone.

    The normal coordinator runner returns decoded, stripped text, which is
    intentionally convenient for commands but cannot hash a blob: leading or
    trailing whitespace and non-UTF-8 bytes are content. `cat-file` therefore
    writes its raw stdout to a temporary file under the same scrubbed child
    environment, and Python hashes those bytes.
    """
    spec = f"{base_commit}:{path}"
    argv = [git_program, "-C", str(repo), "--no-replace-objects", "cat-file"]
    try:
        entry = subprocess.run(
            [git_program, "-C", str(repo), "--no-replace-objects", "ls-tree",
             "-z", "--full-tree", str(base_commit), "--",
             f":(literal){path}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=CE.child_env(), close_fds=True,
            timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, f"cannot inspect corpus path {path!r}: {exc}"
    if entry.returncode != 0:
        detail = entry.stderr.decode("utf-8", "replace").strip()[:160]
        return None, None, (f"cannot inspect corpus path {path!r} at the "
                            f"anchored base: {detail}")
    records = [record for record in entry.stdout.split(b"\0") if record]
    exact = []
    for record in records:
        try:
            metadata, found_path = record.split(b"\t", 1)
            mode, kind, _object_id = metadata.split(b" ", 2)
        except ValueError:
            return None, None, (f"cannot parse the anchored tree entry for "
                                f"corpus path {path!r}")
        if found_path == os.fsencode(path):
            exact.append((mode.decode("ascii", "replace"),
                          kind.decode("ascii", "replace")))
    if len(exact) != 1:
        return None, None, (f"declared corpus path {path!r} has no exact tree "
                            f"entry at anchored base "
                            f"{str(base_commit)[:12]}")
    mode, tree_kind = exact[0]
    if mode not in ("100644", "100755"):
        return None, None, (f"declared corpus path {path!r} has mode {mode} "
                            f"at anchored base {str(base_commit)[:12]}; only "
                            f"regular files (100644 or 100755) are admissible")
    if tree_kind != "blob":
        return None, None, (f"declared corpus path {path!r} has mode {mode} "
                            f"but object type {tree_kind!r} at the anchored "
                            f"base")
    try:
        kind = subprocess.run(
            argv + ["-t", spec], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=CE.child_env(), close_fds=True, encoding="utf-8",
            errors="replace", timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, f"cannot inspect corpus path {path!r}: {exc}"
    if kind.returncode != 0 or kind.stdout.strip() != "blob":
        detail = kind.stderr.strip()[:160]
        return None, None, (f"declared corpus path {path!r} is not a file at "
                            f"anchored base {str(base_commit)[:12]}"
                            f"{': ' + detail if detail else ''}")
    try:
        size_run = subprocess.run(
            argv + ["-s", spec], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=CE.child_env(), close_fds=True, encoding="utf-8",
            errors="replace", timeout=60, check=False)
        size = int(size_run.stdout.strip()) if size_run.returncode == 0 else -1
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, None, f"cannot size corpus path {path!r}: {exc}"
    if size < 0:
        return None, None, f"cannot size corpus path {path!r} at the base"
    if size > MAX_CORPUS_FILE_BYTES:
        return None, None, (f"corpus path {path!r} is {size} bytes at the "
                            f"base, over the {MAX_CORPUS_FILE_BYTES} limit")
    try:
        with tempfile.TemporaryFile() as raw:
            read = subprocess.run(
                argv + ["blob", spec], stdin=subprocess.DEVNULL, stdout=raw,
                stderr=subprocess.PIPE, env=CE.child_env(), close_fds=True,
                timeout=60, check=False)
            if read.returncode != 0:
                detail = read.stderr.decode("utf-8", "replace").strip()[:160]
                return None, None, (f"cannot read corpus path {path!r} at "
                                    f"the base: {detail}")
            raw.seek(0)
            hashed, seen = hashlib.sha256(), 0
            while True:
                chunk = raw.read(1024 * 1024)
                if not chunk:
                    break
                hashed.update(chunk)
                seen += len(chunk)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, f"cannot read corpus path {path!r}: {exc}"
    if seen != size:
        return None, None, (f"corpus path {path!r} yielded {seen} bytes, but "
                            f"Git reported {size}")
    return hashed.hexdigest(), size, None


def corpus_evidence(runner, repo, base_commit, subject_commit, entry):
    """(receipt fields, error) binding a subject to its declared corpus.

    An empty or absent corpus deliberately returns an empty mapping before it
    asks Git anything. Updating a legacy receipt with that mapping is a no-op,
    which keeps both its behavior and serialized bytes unchanged.
    """
    corpus, refusal = declared_corpus(entry)
    if refusal:
        return None, refusal
    if not corpus:
        return {}, None
    if not repo or not base_commit or not subject_commit:
        return None, ("a declared verifier corpus needs repository, anchored "
                      "base, and subject commit identities")
    rc, out, err = _git(
        runner, repo, "--no-replace-objects", "diff", "--name-only", "-z",
        "--no-renames", "--no-ext-diff", str(base_commit),
        str(subject_commit), "--")
    if rc != 0:
        return None, (f"cannot compare subject {str(subject_commit)[:12]} to "
                      f"anchored base {str(base_commit)[:12]}: {err[:200]}")
    changed = sorted(p for p in out.split("\x00") if p)
    digests, total = {}, 0
    for path in corpus:
        digest, size, derr = _digest_base_blob(
            repo, base_commit, path, getattr(runner, "git_program", "git"))
        if derr:
            return None, derr
        total += size
        if total > MAX_CORPUS_TOTAL_BYTES:
            return None, (f"declared corpus exceeds the "
                          f"{MAX_CORPUS_TOTAL_BYTES}-byte total limit")
        digests[path] = digest
    return {"subject_changed_paths": changed,
            "corpus_base_sha256": digests}, None


def corpus_change_refusal(entry, evidence, claim):
    """Why this evidence cannot support `claim`, or None."""
    corpus, refusal = declared_corpus(entry)
    if refusal:
        return refusal
    changed = set((evidence or {}).get("subject_changed_paths") or [])
    touched = sorted(changed.intersection(corpus))
    if not touched:
        return None
    return (f"subject commit changed declared corpus path {touched[0]!r}; "
            f"refusing the receipt that would have established claim "
            f"{claim!r} from candidate-controlled verifier inputs")


def run_in_checkout(runner, repo, commit, path, expect_digest, args=None,
                    timeout=900):
    """Run a pinned verifier against a checkout WE create, at `commit`.

    Verifying in the agent's own working tree was two problems wearing one
    coat. Checking HEAD before the run and again after left a window: move to
    B, let the verifier test B, move back to A, and both observations agree.
    And nothing stopped a tracked file being edited mid-run with HEAD never
    moving at all.

    Both dissolve if we stop asking the agent's tree anything. A detached
    worktree at the produced commit is clean by construction, is not where the
    agent is working, and cannot drift underneath the run. It also makes the
    question honest: what was tested IS the commit named, rather than whatever
    happened to be checked out when somebody typed the command.
    """
    tmp = tempfile.mkdtemp(prefix="verify-checkout-")
    tree = os.path.join(tmp, "tree")
    rc, _out, err = _git(runner, repo, "worktree", "add", "--detach",
                         "--quiet", tree, str(commit), timeout=300)
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, (f"cannot check out {str(commit)[:12]} to verify it: "
                      f"{err[:200]}")
    try:
        return run_pinned(runner, path, expect_digest, args=args,
                          timeout=timeout, cwd=tree)
    finally:
        _git(runner, repo, "worktree", "remove", "--force", tree, timeout=120)
        shutil.rmtree(tmp, ignore_errors=True)


def integration_basis(runner, repo, produced_head, target_commit):
    """Return the immutable identities needed to test a candidate merge.

    Both commits must already exist locally.  This module has no fetch path:
    a connected session may add objects to the repository, while an object
    that is still absent makes integration evidence unavailable.
    """
    for label, commit in (("produced head", produced_head),
                          ("target commit", target_commit)):
        if not commit:
            return None, f"no {label} was supplied"
        if (not isinstance(commit, str) or len(commit) not in (40, 64)
                or any(ch not in "0123456789abcdef" for ch in commit)):
            return None, (
                f"{label} must be an exact 40- or 64-character lowercase "
                f"hexadecimal object id, not a ref or abbreviated name")
        rc, _out, err = _isolated_git(
            runner, repo, "--no-replace-objects", "cat-file", "-e",
            f"{commit}^{{commit}}")
        if rc != 0:
            return None, (
                f"{label} {str(commit)[:12]} is not available in the local "
                f"repository ({err[:160]}). A connected session must supply "
                f"the Git object; verification never contacts a forge.")
    if produced_head == target_commit:
        return None, (
            "produced head and target commit are the same commit, so there "
            "is no candidate merge to verify; branch-local evidence cannot "
            "satisfy integration-tests")
    rc, _out, err = _isolated_git(
        runner, repo, "--no-replace-objects", "merge-base", "--is-ancestor",
        str(produced_head), str(target_commit))
    if rc == 0:
        return None, (
            f"target commit {str(target_commit)[:12]} already contains "
            f"produced head {str(produced_head)[:12]}, so Git has no "
            "candidate change to merge; branch-local evidence cannot "
            "satisfy integration-tests")
    if rc != 1:
        return None, (
            f"cannot compare produced head {str(produced_head)[:12]} with "
            f"target {str(target_commit)[:12]}: {err[:160]}")
    rc, out, err = _isolated_git(
        runner, repo, "--no-replace-objects", "merge-base", "--all",
        str(target_commit), str(produced_head))
    bases = [line.strip() for line in out.splitlines() if line.strip()]
    if rc != 0 or not bases:
        return None, (
            f"cannot find a merge base for produced head "
            f"{str(produced_head)[:12]} and target "
            f"{str(target_commit)[:12]} ({err[:160]})")
    if len(bases) != 1:
        return None, (
            f"produced head {str(produced_head)[:12]} and target "
            f"{str(target_commit)[:12]} have {len(bases)} best merge bases; "
            f"the receipt format binds one, so this history is unavailable "
            f"to integration verification")
    return {"produced_head": str(produced_head),
            "target_commit": str(target_commit),
            "merge_base": bases[0]}, None


def target_before_merge(runner, repo, produced_head, merged_as, method,
                        claimed_target):
    """Return the target commit established by locally supplied Git objects.

    A merge attester may state the pre-merge target, but its string does not
    establish that fact.  Merge and squash commits expose it as their first
    parent. A rebase does not encode that boundary: a target commit can be
    patch-equivalent to a replayed produced commit. It is unavailable rather
    than accepting a caller-selected interpretation of that topology.
    """
    for label, commit in (("produced head", produced_head),
                          ("merged commit", merged_as),
                          ("claimed target", claimed_target)):
        if (not isinstance(commit, str) or len(commit) not in (40, 64)
                or any(ch not in "0123456789abcdef" for ch in commit)):
            return None, (
                f"{label} must be an exact lowercase Git object id")
        rc, _out, err = _isolated_git(
            runner, repo, "--no-replace-objects", "cat-file", "-e",
            f"{commit}^{{commit}}")
        if rc != 0:
            return None, (
                f"{label} {commit[:12]} is not available in the local "
                f"repository ({err[:160]}). A connected session must supply "
                f"the Git object; verification never contacts a forge.")

    rc, out, err = _isolated_git(
        runner, repo, "--no-replace-objects", "rev-list", "--parents",
        "--max-count=1", str(merged_as))
    fields = out.split()
    if rc != 0 or not fields or fields[0] != merged_as:
        return None, f"cannot inspect merged commit {merged_as[:12]}: {err[:160]}"
    parents = fields[1:]
    if method == "merge":
        if len(parents) != 2 or parents[1] != produced_head:
            return None, (
                f"merged commit {merged_as[:12]} is not a two-parent merge "
                f"whose second parent is produced head {produced_head[:12]}")
        return parents[0], None
    if method == "squash":
        if len(parents) != 1:
            return None, (
                f"squash result {merged_as[:12]} does not have exactly one "
                f"parent from which to establish the pre-merge target")
        return parents[0], None
    if method != "rebase":
        return None, f"merge method {method!r} has no target derivation rule"
    return None, (
        "integration evidence for a rebase is unavailable: the rebased "
        "commit chain does not encode which patch-equivalent commit was the "
        "pre-merge target")


def _candidate_checkout(runner, repo, produced_head, target_commit):
    """Return ``(tmp, tree, basis, error)`` for a nontrivial candidate merge."""
    basis, error = integration_basis(
        runner, repo, produced_head, target_commit)
    if error:
        return None, None, None, error
    tmp, tree, err = _isolated_checkout(
        runner, repo, target_commit, "verify-integration-")
    if err:
        return None, None, basis, err
    rc, _out, err = _isolated_git(
        runner, tree, "--no-replace-objects", "-c",
        "core.hooksPath=/dev/null", "-c", "rerere.enabled=false", "-c",
        "rerere.autoupdate=false", "-c", "user.name=hanig-verifier",
        "-c", "user.email=hanig-verifier.invalid", "merge", "--no-commit",
        "--no-ff", str(produced_head), timeout=300)
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, basis, (
            f"candidate merge of produced head "
            f"{str(produced_head)[:12]} into target "
            f"{str(target_commit)[:12]} is unavailable: {err[:300]}")
    rc, candidate_tree, err = _isolated_git(
        runner, tree, "--no-replace-objects", "write-tree")
    if rc != 0 or not candidate_tree.strip():
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, basis, (
            f"cannot identify the candidate merge tree: {err[:200]}")
    rc, target_tree, err = _isolated_git(
        runner, tree, "--no-replace-objects", "rev-parse", "HEAD^{tree}")
    if rc != 0 or not target_tree.strip():
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, basis, (
            f"cannot identify the target tree: {err[:200]}")
    basis["candidate_tree"] = candidate_tree.strip()
    if candidate_tree.strip() == target_tree.strip():
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, basis, (
            "candidate merge has no produced tree change relative to the "
            "target; branch-local evidence cannot satisfy integration-tests")
    return tmp, tree, basis, None


def candidate_merge_basis(runner, repo, produced_head, target_commit):
    """Rederive the candidate tree identity without running a verifier."""
    tmp, _tree, basis, error = _candidate_checkout(
        runner, repo, produced_head, target_commit)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return basis, error


def run_in_candidate_merge(runner, repo, produced_head, target_commit, path,
                           expect_digest, args=None, timeout=900, executables=None):
    """Run pinned verifier bytes in a disposable candidate-merge checkout.

    The checkout starts at the exact target commit and receives the produced
    head with Git's ordinary recursive merge. A conflict or target-identical
    result is evidence unavailability, not permission to test either branch.
    Returns ``(outcome, basis, error)``.
    """
    executables = executables or RV.resolve_executables()
    runner = RV.GitRunner(runner, executables)
    tmp, tree, basis, error = _candidate_checkout(
        runner, repo, produced_head, target_commit)
    if error:
        return None, basis, error
    try:
        outcome, run_error = run_pinned(
            runner, path, expect_digest, args=args, timeout=timeout, cwd=tree,
            observe_completion=True, executables=executables)
        if outcome is not None:
            outcome["execution"] = RV.local_execution(executables)
        return outcome, basis, run_error
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def stability_declarations(policy):
    """Find declarations without granting authority from malformed claims."""
    return [v for v in policy["verifiers"] if isinstance(v, dict)
            and (v.get("name") == STABILITY_CLAIM
                 or (isinstance(v.get("claims"), (list, str))
                     and STABILITY_CLAIM in v["claims"]))]


def merge_precondition_policy(runner, repo, target_commit,
                              claim=INTEGRATION_CLAIM):
    """Authorize a designated merge verifier from the observed target.

    An absent stability declaration is the only compatibility exception. A
    declared but malformed, missing, or unpinned verifier always refuses.
    """
    if claim not in (INTEGRATION_CLAIM, STABILITY_CLAIM):
        return None, None, None, "unknown merge-precondition claim"
    policy, policy_digest, error = read_policy(
        runner, repo, target_commit, source="target commit")
    if error:
        return None, None, None, "merge-precondition target policy: " + error
    name, path = MERGE_VERIFIER, MERGE_VERIFIER_PATH
    if claim == STABILITY_CLAIM:
        name, path = STABILITY_CLAIM, STABILITY_VERIFIER_PATH
        declarations = stability_declarations(policy)
        if not declarations:
            return dict(policy, verifiers=[]), policy_digest, None, None
        if len(declarations) != 1:
            return None, None, None, "ambiguous changed-tests-stable target policy"
        name = declarations[0].get("name")
        if not isinstance(name, str) or not name.strip():
            return None, None, None, "changed-tests-stable declaration has no verifier name"
        # Detection and authorization select the same entry. A second entry
        # with its name is ambiguous even if it declares a different claim.
        if sum(isinstance(v, dict) and v.get("name") == name
               for v in policy["verifiers"]) != 1:
            return None, None, None, "ambiguous changed-tests-stable verifier name"
    digest, _size, error = _digest_base_blob(
        repo, target_commit, path, getattr(runner, "git_program", "git"))
    if error:
        return None, None, None, "merge-precondition target verifier: " + error
    entry, error = authorized(policy, name, digest, claim)
    if error:
        return None, None, None, "merge-precondition target authorization: " + error
    if claim == STABILITY_CLAIM:
        repetitions = entry.get("repetitions", 5)
        if type(repetitions) is not int or repetitions < 1:
            return None, None, None, "changed-tests-stable repetitions must be a positive integer"
    return dict(policy, verifiers=[entry]), policy_digest, digest, None


def run_merge_precondition(runner, repo, produced_head, target_commit,
                           timeout=900):
    """Compatibility API for the original single integration claim."""
    receipts, error = run_merge_preconditions(
        runner, repo, produced_head, target_commit, timeout=timeout,
        claims=(INTEGRATION_CLAIM,))
    return (receipts[0] if receipts else None), error


def run_merge_preconditions(runner, repo, produced_head, target_commit,
                            timeout=900, claims=(INTEGRATION_CLAIM, STABILITY_CLAIM),
                            execution_policy=None):
    """Run target-pinned claims in one disposable candidate merge.

    Return receipts plus any execution error. A completed FAIL remains evidence
    even if a later verifier cannot execute; the caller must publish completed
    receipts under its original observation fence before reporting the error.
    """
    execution_policy = execution_policy or {}
    executables = RV.resolve_executables(execution_policy.get("local"))
    runner = RV.GitRunner(runner, executables)
    checks = []
    for claim in claims:
        policy, policy_digest, digest, error = merge_precondition_policy(
            runner, repo, target_commit, claim=claim)
        if error:
            return [], error
        if not policy["verifiers"]:
            continue
        entry = policy["verifiers"][0]
        corpus, error = corpus_evidence(
            runner, repo, target_commit, produced_head, entry)
        if not error:
            error = corpus_change_refusal(entry, corpus, claim)
        if error:
            return [], error
        checks.append((claim, entry, policy_digest, digest, corpus))
    tmp, policy_tree, error = _isolated_checkout(
        runner, repo, target_commit, "verify-target-policy-")
    if error:
        return [], error
    candidate_tmp = None
    receipts = []
    try:
        candidate_tmp, tree, basis, error = _candidate_checkout(
            runner, repo, produced_head, target_commit)
        if error:
            return [], error
        programs = []
        extras = []
        for claim, entry, policy_digest, digest, corpus in checks:
            path, args, extra = MERGE_VERIFIER_PATH, [], {}
            if claim == STABILITY_CLAIM:
                path = STABILITY_VERIFIER_PATH
                extra = {"repetitions": entry.get("repetitions", 5)}
                args = ["--merge-base", basis["merge_base"], "--head", produced_head,
                        "--repetitions", str(extra["repetitions"])]
            programs.append({"path": os.path.join(policy_tree, path), "digest": digest,
                             "args": args, "program": base64.b64encode(
                                 Path(policy_tree, path).read_bytes()).decode("ascii")})
            extras.append(extra)
        remote = execution_policy.get("remote")
        if remote:
            outcomes, execution = RV.run_remote(
                runner, tree, basis, programs, remote, timeout)
            execution["coordinator_executables"] = executables
        else:
            outcomes = []
            execution = RV.local_execution(executables)
            for program in programs:
                outcome, error = run_pinned(
                    runner, program["path"], program["digest"], args=program["args"],
                    timeout=timeout, cwd=tree, observe_completion=True,
                    executables=executables)
                outcomes.append(outcome if not error else RV.incomplete(error))
        for check, extra, outcome in zip(checks, extras, outcomes):
            claim, entry, policy_digest, digest, corpus = check
            receipt = dict(corpus, **basis, **extra)
            receipt["execution"] = execution
            receipt.update({
                "claim": claim, "verifier": entry["name"],
                "verifier_sha256": digest, "policy_sha256": policy_digest,
                "authorization_commit": target_commit,
                "subject_head": produced_head,
                **outcome_result(outcome),
                "exit_code": outcome["exit_code"],
                "stdout_tail": outcome["stdout"], "stderr_tail": outcome["stderr"],
                "schema_version": 2,
            })
            receipts.append(receipt)
        return receipts, None
    finally:
        if candidate_tmp:
            shutil.rmtree(candidate_tmp, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


def admit_stability(runner, repo, target, integration, unit, receipts):
    """Bind stability to the independently admitted integration candidate.

    This merge-only claim is consumed by the connected operator, not the
    ordinary anchored-base swarm verification path. No receipt chooses its
    policy, repetitions, subject or candidate basis.
    """
    policy, policy_digest, digest, error = merge_precondition_policy(
        runner, repo, target, claim=STABILITY_CLAIM)
    if error:
        return None, error
    if not policy["verifiers"]:
        return None, None
    entry = policy["verifiers"][0]
    expected = {field: integration[field] for field in MERGE_BASIS_FIELDS}
    expected.update(unit=unit, claim=STABILITY_CLAIM, verifier=entry["name"],
                    subject_head=integration["subject_head"],
                    authorization_commit=target, policy_sha256=policy_digest,
                    verifier_sha256=digest, repetitions=entry.get("repetitions", 5))
    corpus, error = corpus_evidence(
        runner, repo, target, integration["subject_head"], entry)
    if not error:
        error = corpus_change_refusal(entry, corpus, STABILITY_CLAIM)
    if error:
        return None, error
    expected.update(corpus)
    matching = [r for r in receipts if all(r.get(k) == v for k, v in expected.items())
                and type(r.get("repetitions")) is int]
    if any(r.get("result") == "fail" for r in matching):
        return None, "changed-tests-stable returned FAIL for this exact candidate binding"
    for receipt in matching:
        if (receipt.get("result") == "pass" and receipt.get("exit_code") == 0
                and not RV.execution_problem(receipt)):
            return receipt, None
    return None, "no passing changed-tests-stable receipt for this exact candidate binding"


def merge_failure_problem(receipts, evidence):
    """Reject any FAIL for an admitted claim's exact binding, including legacy FAILs.

    Incomplete runs supply no evidence. Never infer completion from an old
    receipt's exit code or diagnostic, or reinterpret its stored result.
    """
    fields = ("unit", "claim", "verifier", "verifier_sha256", "policy_sha256",
              "subject_head") + MERGE_BASIS_FIELDS
    if evidence.get("claim") == STABILITY_CLAIM:
        fields += ("authorization_commit", "repetitions")
    for receipt in receipts:
        if (receipt.get("result") == "fail"
                and all(receipt.get(k) == evidence.get(k) for k in fields)
                and (evidence.get("claim") != STABILITY_CLAIM
                     or type(receipt.get("repetitions")) is int)):
            return "{} returned FAIL for this exact candidate binding".format(evidence["claim"])
    return None


def outcome_result(outcome):
    """Receipt result from coordinator execution facts, never child diagnostics."""
    if outcome.get("incomplete_reason"):
        return {"result": "incomplete", "incomplete_reason": outcome["incomplete_reason"]}
    return {"result": "pass" if outcome["exit_code"] == 0 else "fail"}


def _observe_execution(argv, timeout, cwd, env=None):
    """Observe merge-verifier completion without unit.run's overloaded exit 127.

    Only our timeout or a launch/transport exception is incomplete. Every
    returned child status (including 127 or a signal) remains a candidate
    failure unless zero. In particular a stability child exiting without its
    handshake is a FAIL from the pinned parent, not an infrastructure timeout.
    Keep the shared child environment and process-group containment.
    """
    child = None
    completed_failure = None
    out, err, reason = "", "", None
    try:
        child = subprocess.Popen(
            argv, cwd=cwd, env=env if env is not None else CE.child_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
            encoding="utf-8", errors="replace", start_new_session=True)
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        reason = "coordinator timed out after {}s".format(timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        reason = "coordinator could not launch or collect verifier: {}".format(exc)
    finally:
        if reason and child is not None:
            # Capture the child's status BEFORE our intervention. A finished
            # failure is still evidence when a descendant holds a pipe open;
            # the capture timeout must not launder it into a retryable result.
            finished = child.poll()
            if finished is not None and finished != 0:
                completed_failure = finished
            # Kill the session even if its leader exited while descendants
            # kept a capture pipe open. The runner created this process group.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                out, err = child.communicate(timeout=10)
            except (OSError, subprocess.SubprocessError):
                for stream in (child.stdout, child.stderr):
                    if stream:
                        stream.close()
    outcome = {"exit_code": child.returncode if child is not None else None,
               "stdout": (out or "")[-4000:], "stderr": (err or "")[-2000:]}
    if (reason and completed_failure is None
            and outcome["exit_code"] not in (None, 0, -signal.SIGKILL)):
        # The child can finish between poll and killpg. SIGKILL cannot produce
        # a positive exit or a different terminating signal, so that observed
        # nonzero status is still a completed failure, however late it arrived.
        completed_failure = outcome["exit_code"]
    if completed_failure is not None:
        outcome["exit_code"] = completed_failure
        outcome["stderr"] = (outcome["stderr"] + "\n" + reason)[-2000:]
    elif reason:
        outcome["incomplete_reason"] = reason
    return outcome


def run_pinned(runner, path, expect_digest, args=None, timeout=900,
               cwd=None, observe_completion=False, executables=None):
    """Execute the bytes that hashed, not the path that was named.

    Hashing a file and then executing the path re-reads it, so the bytes that
    ran need never be the bytes that were checked. The verified bytes are
    copied to a private temporary file and that copy is executed. Merge claims
    use coordinator-local completion observation; their injected runner is
    used for Git, not to collapse infrastructure errors into child exit codes.
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
        if observe_completion:
            argv = [copy] + list(args or [])
            env = None
            if executables is not None:
                # The pinned programs and their subprocesses share declared
                # executables. A candidate's cwd/PATH never supplies either.
                first_line = Path(copy).read_bytes().split(b"\n", 1)[0]
                if str(path).endswith(".py") or b"python" in first_line:
                    argv.insert(0, executables["python"]["path"])
                bindir = Path(tmpdir) / "bin"
                bindir.mkdir()
                for name, key in (("python3", "python"), ("git", "git")):
                    (bindir / name).symlink_to(executables[key]["path"])
                env = CE.child_env()
                env["PATH"] = str(bindir) + os.pathsep + os.defpath
                env["HANIG_VERIFICATION_GIT"] = executables["git"]["path"]
                env["HANIG_VERIFICATION_PYTHON"] = executables["python"]["path"]
            return _observe_execution(argv, timeout, cwd, env=env), None
        # No before/after dance here any more. `run_in_checkout` gives this a
        # worktree the agent is not working in, so there is nothing to drift.
        rc, out, errout = runner([copy] + list(args or []), timeout=timeout,
                                 cwd=cwd)
        return {"exit_code": rc, "stdout": (out or "")[-4000:],
                "stderr": (errout or "")[-2000:]}, None
    except OSError as exc:
        return None, f"cannot run the pinned verifier: {exc}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
