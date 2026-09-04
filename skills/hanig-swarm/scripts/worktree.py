#!/usr/bin/env python3
"""Did this attempt actually produce anything? Two transitions, one module.

Split out of unit.py, which has a size guard precisely to stop it quietly
becoming a library. Raising that guard because this tripped it would be how a
guard dies; the judging is a separable concern, so it separates.

unit.py used to say the agent's git worktree was judged by
`bus await --base HEAD --require-clean` and that reimplementing it "would be
the mistake this plan exists to undo". We never called it, so nothing judged
the worktree at all. And that predicate was never production evidence: the
caller supplies the base, so HEAD may already be past the work, and a clean
tree is clean precisely when nobody touched it.

The second transition is the ARTIFACT one, and it is the same defect in the
other half of the receipt. unit.py's premise is "isolation replaces
attribution": the write root is exclusive, so an artifact found there was
produced here. That inference is sound only if the write root was EMPTY of
that artifact when the attempt was dispatched, and until B1 nothing ever
checked it. Post-hoc observation cannot tell an input from an output -- a unit
that declared its INPUT path as its output would record a file it never wrote
as produced evidence and read DONE.

`judge_artifacts` is therefore NOT the attribution machinery the committee's
drift guard forbids. It never asks which process wrote a file. It asks the
same question `judge_detail` asks of a repository -- did the thing we digested
before the attempt started differ afterwards -- and it establishes the premise
that isolation-based attribution has been resting on unchecked.

Python 3.8+, standard library only.
"""
import hashlib
import json
import os
import posixpath
from pathlib import Path

# One per ATTEMPT: `launch-<attempt-id>.json`, beside the attempt rather than
# inside it. A single shared record let a retry inherit the previous attempt's
# baseline, so the previous attempt's commits satisfied the new attempt.
LAUNCH_RECORD = "launch.json"        # legacy name; see path construction

# What a produced-tree receipt does NOT claim. Written out because a reader
# six weeks later sees a production verdict and needs to know its exact reach.
PRODUCTION_DENIES = (
    "quality", "relevance", "tests-pass", "authorship", "review",
    "pull-request", "merge",
)

# DECLARED LIMIT AT THE JUDGMENT BOUNDARY. Per-attempt worktree paths prevent
# ordinary agents and a human checkout from colliding by accident; they are
# not process isolation. Git worktrees share refs in one common directory, so
# another same-UID attempt can move this attempt's branch before judgment. A
# simple move normally makes the index/worktree dirty and is refused below,
# but a determined same-UID process can also write the other worktree and its
# index. No Git lock closes that: the same principal can bypass it, and even a
# separate clone is writable by the same principal. Closing intentional
# interference requires a different Unix identity, container, or equivalent
# OS boundary. After judgment the coordinator pins an immutable commit, so a
# later ref rewrite cannot change merge admission or verification. The
# recorded directory inodes below close whole-directory substitution, not
# in-place edits of HEAD, index, refs, or objects. Those files must change for
# an honest commit, so launch-time inode/content equality cannot distinguish
# work from interference. Judgment instead checks their semantics together:
# expected branch, descendant history, changed tree, and clean index/worktree.
WORKTREE_REF_ISOLATION_LIMIT = (
    "per-attempt Git worktrees isolate paths, not hostile same-UID processes "
    "or their shared mutable refs before judgment"
)


def decode_launch_facts(payload):
    """Decode coordinator-transported launch facts without consulting disk."""
    if not payload:
        return None, ("no trusted launch snapshot was supplied by the "
                      "coordinator")
    try:
        facts = json.loads(payload)
    except (TypeError, ValueError) as exc:
        return None, f"trusted launch snapshot is malformed JSON: {exc}"
    if not isinstance(facts, dict):
        return None, "trusted launch snapshot is not a JSON object"
    return facts, None


def _git(runner, repo, *args, timeout=60):
    rc, out, err = runner(["git", "-C", str(repo)] + list(args),
                          timeout=timeout)
    return rc, (out or "").strip(), (err or "").strip()


def repo_status(runner, repo):
    """Return ``(rc, entries)`` from the canonical Git dirty predicate.

    Porcelain v1 with ``-z`` covers the index, worktree, untracked paths,
    conflicts, renames, and dirty submodules without parsing human output.
    Paths are split only on NUL. A filename containing a newline therefore
    remains one entry and can be escaped safely by the caller.

    No path is excluded. Coordinator state now lives outside every operated
    worktree, so an in-repository path is user-authored dirt and must be named
    rather than silently ignored.
    """
    rc, out, _ = _git(runner, repo, "status", "--porcelain=v1", "-z",
                       "--untracked-files=all")
    if rc != 0:
        return rc, []
    fields = out.split("\x00")
    entries, i = [], 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        status = field[:2]
        path = field[3:] if len(field) > 2 and field[2] == " " else field[2:]
        entry = {"status": status, "path": path}
        if ("R" in status or "C" in status) and i < len(fields):
            entry["original_path"] = fields[i]
            i += 1
        entries.append(entry)
    return rc, entries


# Porcelain v1's code for a path no index entry mentions. Every other code
# describes a TRACKED path, and those are already named: a code unit's
# judgment refuses on any of them, and a non-code unit's launch preflight
# refuses before it starts. Dirt that git has never heard of is the class that
# reached DONE with nobody mentioning it.
UNTRACKED_STATUS = "??"

# Enough to name the problem without turning a receipt into a directory
# listing. One stubbed `cp` wrote one file; a loop that writes a thousand
# needs the count, and `count` above `len(paths)` says the list was cut.
MAX_STRAY_PATHS = 50


def _declared_names(spec):
    """Declared outputs as relative names, ready for prefix containment.

    Compared by NAME rather than by resolved path, and that is not laziness.
    Declared outputs are relative to the attempt write root, and a run root is
    required to sit OUTSIDE every operated worktree, so the two roots can
    never share an absolute prefix; resolving both sides would make every
    declared artifact look stray and the exclusion would mean nothing.

    A directory counts for everything under it. `results/table.csv` is what
    the unit declared when it declared `results`, whereas the sibling
    `results.bak/x` it never mentioned is dirt -- which is why the test is
    ``p == d or p.startswith(d + "/")`` and not ``p.startswith(d)``.
    """
    names = set()
    for rel in (spec.get("declared_outputs") or []):
        name = posixpath.normpath(str(rel).replace(os.sep, "/")).strip("/")
        if name and name != "." and not name.startswith(".."):
            names.add(name)
    return names


def stray_untracked(runner, spec, launch_facts=None):
    """Untracked paths in the execution workspace that no output declares.

    18 bytes of debris named `phase0b/--reflink=auto` appeared from a stubbed
    `cp` writing into its source directory instead of its destination, and the
    unit still read DONE. `repo_status` had collected that path at launch
    preflight; nothing carried it as far as the check. This is the carry.

    NEW without needing a second baseline. Dispatch refuses a workspace that
    is not clean: a code attempt gets a fresh per-attempt worktree at the base
    commit, and a non-code unit with a clean-git policy is refused outright.
    So `clean_at_launch` is READ rather than assumed, and when it is false
    this looked at nothing, because an untracked path could then predate the
    attempt and calling it new would be a guess.

    The workspace comes from the coordinator's launch facts, never from the
    spec's `repo` or from the launch record: the same rule `judge_detail`
    follows, for the same reason. No facts means nothing trustworthy names a
    workspace, so the answer is None -- "we did not look" is a different claim
    from "we looked and it was clean", and a list cannot carry both.

    AUDIT ONLY, like the rest of the receipt. Nothing may close, fail or admit
    a unit on this list. It is read from a repository the agent owns, at a
    moment nothing pins, and its only job is to stop debris being silent.
    """
    repo = (launch_facts or {}).get("execution_workspace")
    if not repo or not (launch_facts or {}).get("clean_at_launch"):
        return None
    rc, entries = repo_status(runner, repo)
    if rc != 0:
        return {"workspace": repo, "paths": [], "count": 0,
                "error": f"cannot read git status in {repo!r}"}
    declared = _declared_names(spec)
    stray = sorted(e["path"] for e in entries
                   if e.get("status") == UNTRACKED_STATUS
                   and not any(e["path"] == d or e["path"].startswith(d + "/")
                               for d in declared))
    return {"workspace": repo, "paths": stray[:MAX_STRAY_PATHS],
            "count": len(stray)}


def outputs_present(unit_dir, spec):
    """Which declared outputs exist INSIDE the exclusive write root.

    Paths are resolved under the run-dir and an escape is refused rather than
    followed: a declared output that resolves outside the root is not isolated,
    so nothing about it can be concluded.

    Lives here rather than in unit.py because it answers the same question
    `_declared_names` above answers for the execution workspace -- what did
    this unit declare, and where does that name resolve -- and because both
    halves of the artifact transition below have to agree about which paths
    are in scope.
    """
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


# --- the artifact transition (B1) ----------------------------------------
#
# The pre-dispatch digest is AUTHORITY: it decides admission. So it lives in
# coordinator state, is captured before anything is dispatched, and reaches
# this judge by value, exactly as `--launch-facts` does and for exactly the
# same reason. It is never written into the launch record and never into the
# attempt directory: a baseline the party being judged can rewrite is not a
# baseline. Everything below is PURE -- it reads no file and runs no command,
# so there is no second observation for a moving target to sit inside.
ARTIFACT_BASIS_SCHEMA = 1

# Machine-readable REASON, distinct from `outputs-absent` on purpose. "It
# produced nothing" asks for another turn; "what is there is what was already
# there, or nothing tells us what was there" is an evidence failure, and
# prodding the agent again would answer the wrong question.
REASON_ARTIFACT_UNCHANGED = "artifact-not-produced"


def decode_artifact_basis(payload):
    """Decode the coordinator-transported pre-dispatch digest. No disk."""
    if not payload:
        return None, ("no pre-dispatch artifact digest was supplied by the "
                      "coordinator")
    try:
        basis = json.loads(payload)
    except (TypeError, ValueError) as exc:
        return None, f"the pre-dispatch artifact digest is malformed JSON: {exc}"
    if not isinstance(basis, dict):
        return None, "the pre-dispatch artifact digest is not a JSON object"
    return basis, None


def artifact_basis_problem(basis, unit_dir=None, spec=None):
    """Return why a pre-dispatch artifact digest is unusable, or ``None``.

    The identity check is what makes "pinned per ATTEMPT" structural rather
    than a convention. `produced_head` was a unit-level scalar that was never
    cleared, so a retry inherited the previous attempt's commit; a basis keyed
    by attempt AND restating its own attempt id cannot be inherited by the
    next one even if a caller hands it over.
    """
    if not isinstance(basis, dict):
        return ("coordinator state holds no pre-dispatch digest of this "
                "attempt's declared artifacts")
    if basis.get("schema_version") != ARTIFACT_BASIS_SCHEMA:
        return (f"the pre-dispatch artifact digest declares schema_version "
                f"{basis.get('schema_version')!r}; this build understands "
                f"{ARTIFACT_BASIS_SCHEMA}")
    if unit_dir is not None and basis.get("attempt_id") != Path(unit_dir).name:
        return (f"the pre-dispatch artifact digest belongs to attempt "
                f"{basis.get('attempt_id')!r}, not {Path(unit_dir).name!r}")
    expected_unit = (spec or {}).get("task_id") or (spec or {}).get("id")
    if expected_unit and basis.get("unit_id") != expected_unit:
        return (f"the pre-dispatch artifact digest belongs to unit "
                f"{basis.get('unit_id')!r}, not {expected_unit!r}")
    if not isinstance(basis.get("declared"), list):
        return ("the pre-dispatch artifact digest names no declared artifact "
                "list")
    for key in ("absent", "escaped"):
        if not isinstance(basis.get(key), list):
            return f"the pre-dispatch artifact digest has no {key!r} list"
    if not isinstance(basis.get("present"), dict):
        return "the pre-dispatch artifact digest has no 'present' map"
    return None


def _artifact_changed(was, now):
    """(changed, weak). ``changed`` is None when the two are incomparable.

    Incomparable fails CLOSED at the caller. A basis digested by content and
    an observation recorded by size+mtime describe the artifact with different
    strength, and calling that pair "changed" would admit a unit on the
    weaker of the two without saying so.
    """
    if not isinstance(was, dict) or not isinstance(now, dict):
        return None, False
    if was.get("error") or now.get("error"):
        return None, False
    before, after = was.get("sha256"), now.get("sha256")
    if before and after:
        return before != after, False
    if before or after:
        return None, False
    for key in ("size", "mtime"):
        if was.get(key) is None or now.get(key) is None:
            return None, False
    # The interim the field report named, reached ONLY for an artifact over
    # the digest limit, and named as weak wherever it is used: a rewrite to
    # the same length inside the same second is invisible to it.
    return ((was["size"], was["mtime"]) != (now["size"], now["mtime"]), True)


def artifact_transition_problem(basis, unit_dir, spec, observed):
    """(problem, weak_paths). Are the declared artifacts evidence of production?

    Fails closed at every gap, and there is deliberately no path that takes a
    fresh look: a digest computed now would be a digest of whatever the run
    left behind, which is the question rather than the answer.
    """
    problem = artifact_basis_problem(basis, unit_dir, spec)
    if problem:
        return (problem + ". Nothing distinguishes an artifact this attempt "
                "wrote from one that was already there, and the basis is "
                "never re-observed after the fact. Re-dispatch this unit "
                "through the coordinator into a fresh attempt."), []
    # The coordinator's list, not the spec's. `declared_outputs` lives in
    # unit.json inside the attempt directory, so whoever is being judged can
    # edit it; emptying it made every output "present" vacuously. Disagreement
    # is refused rather than reconciled.
    declared = [str(rel) for rel in basis["declared"]]
    now_declared = [str(rel) for rel in (spec.get("declared_outputs") or [])]
    if sorted(now_declared) != sorted(declared):
        return (f"this attempt's spec now declares "
                f"{', '.join(sorted(now_declared)) or 'nothing'}, but the "
                f"coordinator digested {', '.join(sorted(declared)) or 'nothing'} "
                f"before dispatch. The declaration changed after the baseline "
                f"was taken, so the baseline does not cover what is being "
                f"judged"), []
    if not declared:
        # Nothing to judge, which is NOT the same as nothing established, and
        # is deliberately not turned into a refusal here. `validate_plan`
        # already refuses a unit with no outputs, for the reason this would
        # otherwise duplicate: a unit with nothing declared can never be
        # judged done. Refusing it a second time from the artifact gate would
        # be this change reaching past its own question. Same shape as
        # `judge_detail` returning None for a unit that declared no repository.
        return None, []
    absent = set(str(rel) for rel in basis["absent"])
    escaped = set(str(rel) for rel in basis["escaped"])
    refusals, weak = [], []
    for rel in declared:
        if rel in escaped:
            refusals.append(f"{rel} resolved outside the exclusive write root "
                            f"before dispatch, so it was never isolated")
            continue
        if rel in absent:
            continue          # nothing was there; whatever is there now is new
        was = (basis["present"] or {}).get(rel)
        if not isinstance(was, dict):
            # Declared, and the basis says neither "absent" nor what it
            # looked like. That is a hole in the baseline, not a pass.
            refusals.append(f"{rel} is declared, and nothing was digested for "
                            f"it before dispatch")
            continue
        changed, is_weak = _artifact_changed(was, (observed or {}).get(rel))
        if changed is None:
            refusals.append(
                f"{rel} cannot be compared against its pre-dispatch digest "
                f"(before: {was.get('method', was.get('error', 'nothing recorded'))!r}, "
                f"now: {((observed or {}).get(rel) or {}).get('method', 'nothing recorded')!r})")
        elif not changed:
            refusals.append(
                f"{rel} is identical to the artifact that was already there "
                f"when this attempt was dispatched, so nothing shows this "
                f"attempt produced it. A declared output that existed "
                f"beforehand and did not change is an input")
        elif is_weak:
            weak.append(rel)
    if refusals:
        return "; ".join(refusals), weak
    return None, weak


def judge_artifacts(state, basis, unit_dir, spec, observed, notes):
    """Gate a DONE on the artifact transition. Any other state passes through.

    Only DONE is gated, and that is the point rather than an optimisation: an
    artifact that has not changed yet is the NORMAL condition of a running
    unit, and turning that into a refusal would report every live attempt as
    broken.
    """
    if state != "DONE":
        return state
    problem, weak = artifact_transition_problem(basis, unit_dir, spec, observed)
    if problem:
        notes.append(f"REASON={REASON_ARTIFACT_UNCHANGED}")
        notes.append(problem)
        return "INCOMPLETE"
    if weak:
        notes.append(
            f"production of {', '.join(sorted(weak))} was established by "
            f"size and mtime rather than content, because the artifact is "
            f"over the digest limit. A rewrite to the same length inside the "
            f"same second would be invisible to that comparison.")
    return state


# Every launch field that can select or alter a judgment. The launch record is
# audit-only now, so an unsealed EvidenceRecord must refuse all of them -- in
# particular `repo`, whose earlier omission let a record choose where verify
# operated even though the base was later cross-checked against state.
AUTHORITY_KEYS = frozenset({
    "repo", "remote", "workspace_identity", "branch", "base_commit",
    "base_tree", "execution_workspace", "clean_at_launch", "dirty_paths",
})


class AuthorityFromEvidence(KeyError):
    """Raised when someone asks an unsealed record to decide something."""


class EvidenceRecord(dict):
    """A launch record read WITHOUT its seal, which refuses to be authority.

    A reviewer's point, and a fair one: the static tests that guard this
    invariant match string literals and direct reader names, so a computed key
    or an alias walks straight past them. Detection at one chokepoint is
    weaker than making the thing unrepresentable, so the object itself now
    refuses. Static tests stay as the fast guard; this is the real control.

    Legitimate readers of these fields cross-check them against authority and
    refuse on disagreement. They say so by calling `record_claim`, which names
    what it is returning: a claim, not a fact.

    WHAT THIS IS NOT. It is not a sandbox. `dict.get(rec, key)` and
    `dict(rec)` reach the fields, and they must, because `record_claim` is
    built on exactly that. Python has no way to stop a determined caller
    inside the same process, and pretending otherwise would be the same
    overclaim this class exists to clean up after. What it stops is the
    accidental read and the casually-spelled one -- a computed key, an aliased
    reader, an `items()` loop -- while the deliberate route stays visible,
    allowlisted and tested.
    """

    def _refuse(self, key):
        raise AuthorityFromEvidence(
            f"{key!r} decides something, and this launch record was read "
            f"without its seal, so it is evidence rather than authority. Take "
            f"the value from the plan or from coordinator state; if you are "
            f"cross-checking the record's claim against one of those and will "
            f"refuse on disagreement, say so by calling record_claim().")

    def get(self, key, default=None):
        if key in AUTHORITY_KEYS:
            self._refuse(key)
        return super().get(key, default)

    def __getitem__(self, key):
        if key in AUTHORITY_KEYS:
            self._refuse(key)
        return super().__getitem__(key)

    def pop(self, key, *default):
        if key in AUTHORITY_KEYS:
            self._refuse(key)
        return super().pop(key, *default)

    def setdefault(self, key, default=None):
        if key in AUTHORITY_KEYS:
            self._refuse(key)
        return super().setdefault(key, default)

    def items(self):
        # Iterating VALUES hands out the authority fields without ever naming
        # them, which is the same access wearing a different spelling. Keys
        # stay visible, so a caller can still see what the record contains.
        return [(k, v) for k, v in super().items()
                if k not in AUTHORITY_KEYS]

    def values(self):
        return [v for k, v in super().items() if k not in AUTHORITY_KEYS]


def record_claim(rec, key):
    """What the record CLAIMS for an authority field. Not what is true.

    Only for a caller that compares this against the plan or coordinator
    state and refuses on disagreement. Using it to obtain a value to act on
    is the defect this whole mechanism exists to prevent.
    """
    return dict.get(rec or {}, key)


def launch_record_path(unit_dir):
    """One place for the convention, which three call sites had inlined."""
    return Path(unit_dir).parent / f"launch-{Path(unit_dir).name}.json"


def read_sealed_launch_record(unit_dir, seal):
    """Read the audit copy when it still matches the coordinator's digest.

    This is deliberately not an authority accessor. Judging uses the complete
    per-attempt launch snapshot in coordinator state; deleting or rewriting
    this file can change only the audit result returned here.

    WHAT THIS IS NOT. An unkeyed digest of bytes the agent can read, handed in
    as an argument, is not an authenticator: anyone who can read the record
    can recompute it. I claimed otherwise and a reviewer was right to refuse
    the claim. What it actually provides is narrower and still worth having:
    it binds an audit observation to the bytes the coordinator wrote. It does
    nothing about a party that supplies the seal itself and it does not make
    any field in the record suitable for a decision.
    """
    path = launch_record_path(unit_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, ("no launch record: nothing captured this repository's "
                      "state before the agent ran, so no transition can be "
                      "judged")
    except OSError as exc:
        return None, f"cannot read the launch record at {path}: {exc}"
    # Checked AFTER the file, so an absent record still reports as absent.
    # That case grants nothing either way, and the missing-anchor message is
    # the one that tells an operator what to do.
    if not seal:
        return None, ("no record seal was supplied, so this launch record "
                      "cannot be distinguished from one the agent rewrote. "
                      "Re-dispatch the unit rather than judging against an "
                      "unsealed anchor")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != seal:
        return None, (
            f"the launch record at {path} no longer matches the digest the "
            f"coordinator recorded when it wrote it (sealed {seal[:12]}, "
            f"found {actual[:12]}). It was changed after the agent started, "
            f"so nothing in it can be used to judge what the agent did")
    try:
        rec = json.loads(raw)
    except ValueError as exc:
        return None, f"the launch record at {path} is not readable JSON: {exc}"
    if not isinstance(rec, dict):
        return None, f"the launch record at {path} is not a JSON object"
    return rec, None


def refused_launch(unit_dir):
    """Why this attempt may not be bound, or None if it may.

    A REFUSED launch never dispatched, so there is no job to bind it to. The
    coordinator drops such an attempt from its state, which is why a reviewer
    looking for a recovery SCAN found none and called this unreachable. It is
    reachable: `unit.py bind` is a documented command that takes a directory
    path, and the refused directory is still on disk with its spec intact.
    Binding it would manufacture the one thing the preflight exists to
    prevent, a started attempt in a tree that was never allowed to run.
    """
    launch, _err = read_launch_record(unit_dir)
    pre = ((launch or {}).get("preflight") or {})
    if pre.get("status") != "refused":
        return None
    ws = pre.get("workspace") or "the workspace"
    return (f"attempt {Path(unit_dir).name} was REFUSED at launch preflight "
            f"({ws} was not clean), so nothing was dispatched and there is no "
            f"job to bind. Its receipt is at "
            f"{launch_record_path(unit_dir)}. Clean the workspace and "
            f"allocate a new attempt.")


def read_launch_record(unit_dir):
    """The anchor the coordinator wrote before the agent existed."""
    path = launch_record_path(unit_dir)
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except FileNotFoundError:
        return None, ("no launch record: nothing captured this repository's "
                      "state before the agent ran, so no transition can be "
                      "judged. Re-dispatch through the coordinator.")
    except (OSError, ValueError) as exc:
        return None, f"launch record at {path} is unreadable: {exc}"
    if not isinstance(rec, dict):
        return None, f"launch record at {path} is not an object"
    # Refusing type, so a computed key or an aliased reader cannot quietly
    # take an authority field from an unsealed record.
    return EvidenceRecord(rec), None


def launch_facts_problem(facts, unit_dir=None, spec=None):
    """Return why a trusted launch snapshot is unusable, or ``None``.

    The snapshot is transported to the separate judge as JSON, but its
    provenance is coordinator state, not the launch record. Validate identity
    here so an attempt can never be cross-wired to another attempt's facts.
    """
    if not isinstance(facts, dict):
        return ("coordinator state records no launch snapshot for this "
                "attempt. Re-dispatch it; do not reconstruct one from the "
                "agent-writable launch record")
    if unit_dir is not None:
        attempt = Path(unit_dir).name
        if facts.get("attempt_id") != attempt:
            return (f"the trusted launch snapshot belongs to attempt "
                    f"{facts.get('attempt_id')!r}, not {attempt!r}")
    expected_unit = (spec or {}).get("task_id") or (spec or {}).get("id")
    if expected_unit and facts.get("unit_id") != expected_unit:
        return (f"the trusted launch snapshot belongs to unit "
                f"{facts.get('unit_id')!r}, not {expected_unit!r}")
    required = ("repo", "execution_workspace", "workspace_identity",
                "base_commit", "base_tree", "branch", "clean_at_launch")
    missing = [key for key in required if key not in facts]
    if missing:
        return ("the trusted launch snapshot is incomplete (missing "
                f"{', '.join(missing)}). Re-dispatch this attempt")
    identity = facts.get("workspace_identity")
    if (not isinstance(identity, dict)
            or identity.get("realpath") != facts.get("execution_workspace")):
        return "the trusted launch snapshot has no matching worktree identity"
    for key in ("base_commit", "base_tree"):
        value = facts.get(key)
        if not isinstance(value, str) or len(value) not in (40, 64) or any(
                c not in "0123456789abcdef" for c in value.lower()):
            return f"the trusted launch snapshot has an invalid {key}"
    if facts.get("clean_at_launch") is not True:
        return ("the repository was already dirty at launch according to "
                "the trusted launch snapshot, so production is "
                "unattributable")
    return None


def workspace_identity_problem(runner, facts):
    """Return why judgment no longer addresses the launched Git worktree."""
    workspace = facts.get("execution_workspace")
    identity = facts.get("workspace_identity") or {}
    if not isinstance(identity, dict):
        return "the trusted launch snapshot has no worktree identity"
    if (identity.get("path") != workspace
            or identity.get("realpath") != workspace
            or not isinstance(identity.get("device"), int)
            or not isinstance(identity.get("inode"), int)
            or not identity.get("git_common_dir")
            or not identity.get("git_dir")):
        return "the trusted launch snapshot has an incomplete worktree identity"
    common_identity_fields = ("git_common_device", "git_common_inode")
    git_identity_fields = ("git_dir_device", "git_dir_inode")
    common_values = [identity.get(key) for key in common_identity_fields]
    git_values = [identity.get(key) for key in git_identity_fields]
    if (any(value is not None for value in common_values)
            and not all(isinstance(value, int) for value in common_values)):
        return "the trusted launch snapshot has an incomplete Git common-directory identity"
    if (any(value is not None for value in git_values)
            and not all(isinstance(value, int) for value in git_values)):
        return "the trusted launch snapshot has an incomplete Git directory identity"
    has_common_inode = all(isinstance(value, int) for value in common_values)
    has_git_inode = all(isinstance(value, int) for value in git_values)
    try:
        current_path = str(Path(workspace).resolve())
        current = os.stat(workspace)
    except OSError as exc:
        return f"cannot identify the anchored worktree {workspace!r}: {exc}"
    if (current_path != identity["realpath"]
            or current.st_dev != identity["device"]
            or current.st_ino != identity["inode"]):
        return (f"the anchored worktree path {workspace!r} no longer names "
                f"the launched directory (device/inode changed)")
    observed = {}
    for key, args in (
            ("top", ("rev-parse", "--show-toplevel")),
            ("git_common_dir", ("rev-parse", "--git-common-dir")),
            ("git_dir", ("rev-parse", "--git-dir")),
            ("branch", ("rev-parse", "--abbrev-ref", "HEAD"))):
        rc, value, _ = _git(runner, workspace, *args)
        if rc != 0:
            return (f"cannot verify the anchored worktree's Git identity "
                    f"({key} is unreadable)")
        observed[key] = value
    top = str(Path(observed["top"]).resolve())
    common = str((Path(workspace) / observed["git_common_dir"]).resolve())
    git_dir = str((Path(workspace) / observed["git_dir"]).resolve())
    try:
        common_st = os.stat(common)
        git_st = os.stat(git_dir)
    except OSError as exc:
        return f"cannot stat the anchored Git metadata: {exc}"
    # Migration: launch snapshots written before the Git-metadata identity
    # fields were added recorded paths but not device/inode. Those attempts
    # keep the older, weaker path + worktree-root check until they finish;
    # absence of a field that did not exist is unverifiable, not evidence of
    # substitution. Every newly-written record takes both inode checks.
    if (top != workspace
            or common != identity["git_common_dir"]
            or (has_common_inode
                and (common_st.st_dev != identity["git_common_device"]
                     or common_st.st_ino != identity["git_common_inode"]))
            or git_dir != identity["git_dir"]
            or (has_git_inode
                and (git_st.st_dev != identity["git_dir_device"]
                     or git_st.st_ino != identity["git_dir_inode"]))):
        return (f"the anchored directory {workspace!r} no longer has the "
                f"launched Git worktree metadata identity")
    if observed["branch"] != facts.get("branch"):
        return (f"the repository is on branch {observed['branch']!r}, but "
                f"this attempt was anchored on {facts.get('branch')!r}")
    return None


def judge_detail(runner, unit_dir, spec, launch_facts=None):
    """(produced, head, detail).

    Returns the head it VALIDATED, not one re-read afterwards. Splitting those
    was a time-of-check/time-of-use gap: judge() checked commit B, and a
    second `rev-parse HEAD` a moment later could return C, because the agent
    owns that repository and nothing stops it moving HEAD. The binding then
    pinned C, which nothing had judged.

    `produced` is None when the unit declared no repository, so there is
    nothing to judge; False when a repository was declared and did not
    transition; True when it did.

    Every clause exists because its absence admits work that never happened:

      descends-from-base  else an unrelated-history reset, or a branch already
                          ahead at launch, reads as production.
      tree differs        else an empty commit, or a change reverted before
                          committing, reads as production. The comparison is
                          TREE to TREE, not commit to commit, because a commit
                          always differs from its parent.
      clean at both ends  dirty output is unattributable, and dirt at launch
                          means there was no clean state to start from.
      same repo/branch    else a transition somewhere else counts here.
    """
    # The SPEC decides whether there is anything to judge. Asking the launch
    # record first conflated "declared no repository" with "was never
    # anchored", and those call for opposite responses.
    # A missing `repo` key and an explicit `"repo": null` are deliberately
    # the SAME answer. A reviewer wanted them distinguished; there is no
    # action a reader would take differently, and inventing a distinction
    # nobody acts on is how a vocabulary starts lying.
    if not spec.get("repo"):
        return None, None, ("this unit declared no repository, so no git transition "
                      "is judged for it")

    err = launch_facts_problem(launch_facts, unit_dir, spec)
    if err:
        return False, None, err
    rec = launch_facts
    err = workspace_identity_problem(runner, rec)
    if err:
        return False, None, err
    repo = rec.get("execution_workspace")
    if not repo:
        return False, None, (
            f"this unit declares repo {spec['repo']!r}, but its launch record "
            f"anchored no execution worktree. The anchor was written before the unit "
            f"declared one, or _write_launch_record failed: either way "
            f"nothing captured a baseline, so re-dispatch this unit rather "
            f"than reading this as a configuration mistake")
    if not os.path.isdir(repo):
        return False, None, f"the anchored repository {repo!r} is gone"

    if not rec.get("clean_at_launch", False):
        return False, None, (
            f"the repository was already dirty at launch "
            f"({rec.get('dirty_paths_at_launch', '?')} path(s)), so there was "
            f"no clean state to transition FROM and any change now is "
            f"unattributable to this attempt")

    # `--porcelain` without `--ignored` ON PURPOSE. A reviewer read the
    # omission as a hole: an agent can leave an uncommitted file that
    # .gitignore covers, and both cleanliness checks still pass. True, and
    # taking `--ignored` would fail every repository with a venv, a build
    # directory or __pycache__, which is a nuisance failure on honest work.
    # An ignored path is DECLARED not to be part of the artifact, by the
    # repository itself, and production here is a claim about the committed
    # tree. The claim was too broad; the code is right.
    rc, dirty = repo_status(runner, repo)
    if rc != 0:
        return False, None, f"cannot read git status in {repo!r}"
    if dirty:
        return False, None, (
            f"{len(dirty)} path(s) are uncommitted. Work left in the working "
            f"tree is not production: it is recorded nowhere another attempt "
            f"or reader could find it")

    rc, branch, _ = _git(runner, repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc == 0 and rec.get("branch") and branch != rec["branch"]:
        return False, None, (f"the repository is on branch {branch!r}, but this "
                       f"attempt was anchored on {rec['branch']!r}")

    base = rec.get("base_commit")
    rc, head, _ = _git(runner, repo, "rev-parse", "HEAD")
    if rc != 0:
        return False, None, f"cannot read HEAD in {repo!r}"
    if head == base:
        return False, None, ("HEAD has not moved since launch, so nothing was "
                       "committed")

    rc, _, _ = _git(runner, repo, "merge-base", "--is-ancestor", base, head)
    if rc != 0:
        return False, None, (
            f"HEAD {head[:12]} does not descend from the anchored base "
            f"{str(base)[:12]}. The history was replaced rather than extended, "
            f"so what is there now was not built on what we anchored")

    # The tree of the CAPTURED head, not of HEAD. Reading `HEAD^{tree}` was a
    # second look at a moving target: the agent could leave an empty
    # descendant at HEAD for the first read and a content-changing one for
    # this, so the tree that satisfied the check belonged to a commit other
    # than the one returned and pinned.
    rc, tree, _ = _git(runner, repo, "rev-parse", head + "^{tree}")
    if rc != 0:
        return False, None, f"cannot read HEAD's tree in {repo!r}"
    if tree == rec.get("base_tree"):
        return False, None, (
            "HEAD advanced but its tree is identical to the anchored base "
            "tree, so the content is unchanged. An empty commit, or a change "
            "reverted before committing, moves HEAD without producing "
            "anything")

    return True, head, (f"tree {tree[:12]} differs from the anchored base tree "
                  f"{str(rec.get('base_tree'))[:12]}, on a commit descending "
                  f"from {str(base)[:12]}, with a clean tree at both ends")


def judge(runner, unit_dir, spec, launch_facts=None):
    """(produced, detail). The two-value view, for callers that do not need
    the head."""
    produced, _head, why = judge_detail(runner, unit_dir, spec, launch_facts)
    return produced, why


def produced_head(runner, unit_dir, spec, launch_facts=None):
    """The commit this attempt produced, or None.

    What a merge attestation gets PINNED to. Without it the attester names
    whatever commit it likes and the coordinator has no way to object; with
    it, an attestation about some other branch's work cannot close this unit.

    This is valid only at the one judgment boundary. Later consumers must use
    the per-attempt head stored in coordinator state, never call this as a
    recovery path.
    """
    _produced, head, _why = judge_detail(runner, unit_dir, spec, launch_facts)
    return head


def basis(runner, unit_dir, spec, launch_facts=None):
    """What the receipt can say about the agent's repository.

    A string, not a bool: "we did not look", "there was nothing to look at"
    and "we looked and it produced" are three different claims, and a boolean
    carries only two.
    """
    produced, _why = judge(runner, unit_dir, spec, launch_facts)
    if produced is None:
        return "no-repository-declared"
    return "produced-committed-change" if produced else "no-produced-change"


def code_basis(runner, unit_dir, spec, launch_facts=None):
    """The code-only fields of a receipt's `basis`.

    Assembled here rather than spelled out in unit.py, which has a size guard
    whose job is to stop it accreting other modules' concerns. `produced_head`
    is the head that was JUDGED: a merge attestation is bound to it, and
    re-deriving it later asks a repository the agent owns a second question.
    `worktree_judged` is captured from that same judgment for the same reason;
    this formatter performs no repository observation.
    """
    if spec.get("kind") != "code":
        return {"worktree_judged": None, "produced_head": None,
                "production_denies": None}
    return {"worktree_judged": spec.get("worktree_judged"),
            "produced_head": spec.get("produced_head"),
            "production_denies": list(PRODUCTION_DENIES)}


def receipt_basis(runner, unit_dir, spec, launch_facts=None):
    """Every field worktree.py contributes to a receipt's `basis`.

    Two callees with OPPOSITE relationships to the repository, which is why
    they stay two functions. `code_basis` must not touch it at all: its
    fields were decided at judgment, and asking a mutable source a second
    time is how a pinned head stopped matching the tree that satisfied it. A
    test hands it a runner that fails the test on use.

    `stray_untracked` is the other kind of read: a first and only look, at
    something no judgment produced, deciding nothing. Refusing it here on the
    strength of the rule above would have been cargo cult -- the rule is
    "never ask a mutable source AGAIN", not "never look".
    """
    return dict(code_basis(runner, unit_dir, spec, launch_facts),
                stray_untracked=stray_untracked(runner, spec, launch_facts))


def validate_pinned_head(runner, launch_facts, produced):
    """Validate immutable commit ``produced`` against its pinned launch base.

    This never reads HEAD, a branch, the index, or the worktree. A branch may
    move after judgment without changing the answer. Object disappearance is
    an availability failure and fails closed; another ref is never substituted.
    """
    problem = launch_facts_problem(launch_facts)
    if problem:
        return problem
    if not isinstance(produced, str) or len(produced) not in (40, 64) or any(
            c not in "0123456789abcdef" for c in produced.lower()):
        return "the per-attempt produced commit is not a valid object id"
    # Immutable objects remain in the source repository after Paseo archives
    # the finished worktree. Judgment itself uses execution_workspace above;
    # this later pin validation deliberately needs no live checkout.
    repo, base = launch_facts["repo"], launch_facts["base_commit"]
    rc, _out, _err = _git(runner, repo, "cat-file", "-e", produced + "^{commit}")
    if rc != 0:
        return (f"pinned produced commit {produced[:12]} is no longer "
                "available; refusing rather than substituting the current ref")
    rc, _out, _err = _git(runner, repo, "merge-base", "--is-ancestor",
                           base, produced)
    if rc != 0:
        return (f"pinned produced commit {produced[:12]} does not descend "
                f"from trusted base {base[:12]}")
    rc, tree, _err = _git(runner, repo, "rev-parse", produced + "^{tree}")
    if rc != 0:
        return f"cannot read the tree of pinned commit {produced[:12]}"
    if tree == launch_facts["base_tree"]:
        return "the pinned produced commit has the launch base's unchanged tree"
    return None
