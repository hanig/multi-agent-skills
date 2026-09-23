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

The live worktree then became a different false dependency: Paseo deleted it
when an agent closed, so judgment raced cleanup. New attempts instead use a
coordinator-owned worktree and anchor an exact remote branch ref in coordinator
state before the agent exists. The
judge resolves that ref directly from the anchored remote, so a narrow fetch
refspec cannot hide a successful push, then validates the immutable commit
without opening the worktree. Legacy launch snapshots retain the old worktree
route only until they finish.

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


def _as_text(value):
    """Any runner return value, as text, without ever raising.

    THE boundary. Every value the injected runner produces enters this
    module through `_git`, and `_git` used to call `(value or "").strip()`
    on it -- so a runner returning a list of stderr lines, "a natural
    runner shape", raised AttributeError here and a validation failure
    became an exception rather than a refusal.

    Three review rounds fixed consumers of this function instead of this
    function: first `render_for_record`, then `render_git_diagnostic`, then
    the exit status. Each fix was correct and none of them was the
    boundary. This is the boundary, and there is exactly one coercion in
    the module now.
    """
    # `type(value) is str`, not isinstance: luna found that a str SUBCLASS
    # passes isinstance and then `_git` calls its overridden `.strip()`,
    # which can raise. A subclass falls through to the str() branch below,
    # which produces a real str with real methods.
    if type(value) is str:
        return value
    if value is None:
        return ""
    try:
        # `.decode` is inside the guard: kimi-k2.7-code pointed out that a
        # bytes SUBCLASS can override it, and it was being called outside.
        if isinstance(value, bytes):
            rendered = value.decode("utf-8", "replace")
        else:
            rendered = str(value)
        # str() and .decode() both RETURN A SUBCLASS when handed one, so
        # the subclass this branch exists to defuse walked straight
        # through it and `_git` called its poisoned `.strip()` anyway.
        #
        # My first repair was `"" + rendered`, justified in a comment
        # saying the subclass could not intervene "because it is the
        # right-hand operand of a real str". glm-5.3: "The in-code
        # justification is backwards: being the right-hand operand of a
        # str is what gives the subclass first crack at __radd__, not
        # what prevents it." Reproduced -- a Poison(str) with __radd__
        # returning self comes straight back out of `"" + p`, and out
        # of `str(p)` and `"%s" % p` too. I had cited the mechanism
        # that defeats the coercion as the reason it works.
        #
        # `"".join([x])` copies the characters in C. There is no
        # protocol for an element to intercept a join, so a subclass
        # cannot return itself from one, and a value that is not a str
        # at all raises TypeError into the handler below rather than
        # escaping as something else. Those are the only two outcomes:
        # exact str, or the fallback.
        #
        # A `type(...) is not str` check sat here afterwards and
        # reverting it changed nothing, because join has no third
        # outcome to catch. A guard that cannot fire is the "invariant
        # written in prose" this repository warns about, so it is gone
        # and the reasoning is here instead.
        if type(rendered) is not str:
            rendered = "".join([rendered])
        return rendered
    except BaseException:
        # BaseException, not Exception: kimi-k2.7-code pointed out that a
        # __str__ raising SystemExit escapes an `except Exception`. The
        # only call inside this try is str(value), so any BaseException
        # from it is the value's doing, and this function's contract is
        # that it does not raise.
        return "<a value that cannot be rendered>"


class _UnestablishedStatus(object):
    """Nonzero, with a magnitude this runner did not establish.

    NOT an int, deliberately. Every branch in this module that reads an
    exact status -- `rc == 1` for merge-base's documented "not an
    ancestor", `rc == 2` for ls-remote's "ref absent", `rc in (0, 1)` for
    config's "key not found" -- is asking a question that a value like
    "128" or 128.0 or True cannot answer. Collapsing those to 1, which is
    what the previous version did, made every one of them answer YES to a
    question about a status nobody reported.
    """

    __slots__ = ()

    def __repr__(self):
        return "<a status the runner did not report as a number>"

    __str__ = __repr__


UNESTABLISHED_STATUS = _UnestablishedStatus()


def _as_status(value):
    """A runner's exit status, as an int when one can be established.

    luna and kimi-k2.7-code, independently: `rc` was never coerced, so a
    status object with a custom comparison raised at `rc != 0` before any
    refusal could be built. A status whose comparison to zero cannot be
    evaluated is treated as FAILURE, because a runner that cannot say it
    succeeded did not.

    Four rounds of this function were four wrong answers to one question,
    "is this value zero, and if not, which nonzero is it":

      * `rc != 0` accepted 0, 0.0 and False, refused "0", and raised on a
        hostile comparison.
      * `int(value)` ADMITTED "0" -- a previously-refused run (luna,
        glm-5.3).
      * Rejecting everything non-int REFUSED 0.0 and False, which a runner
        legitimately returns (kimi-k2.7-code).
      * Collapsing every nonzero to 1 made 128.0 and "128" read as
        merge-base's documented "not an ancestor", so a fatal error became
        a false lineage verdict (luna, glm-5.3, both again).

    The fourth is the one worth stating as a rule: the callers need two
    different facts, and only one of them survives a collapse. So a value
    that is not an int yields zero if it compares equal to zero, and
    otherwise UNESTABLISHED_STATUS -- nonzero, and no more than that. The
    magnitude is never PARSED out of a string; "0" is still refused, and
    the refusal now says the status was not reported as a number rather
    than naming a cause.
    """
    # Only a genuine number may claim zero. luna: an object whose
    # __eq__(0) returns True was ADMITTED as success -- a runner could
    # wrap a real exit 128 in one and validate_pinned_head would
    # return None instead of refusing. Every earlier version of this
    # function erred towards refusing; this one erred towards
    # admitting, which is the direction that matters.
    #
    # The comparison is kept for float and False because a runner
    # legitimately returns those (kimi-k2.7-code, two rounds ago), and
    # denied to everything else because nothing else can be trusted to
    # mean zero by saying so.
    if value is False:
        return 0
    if value is True:
        return UNESTABLISHED_STATUS
    if type(value) is float:
        return 0 if value == 0 else UNESTABLISHED_STATUS
    if type(value) is int:
        # An int passes through UNCHANGED, magnitude and all: 1 and 128
        # mean different things and both callers depend on the difference.
        #
        # `type(value) is int` rather than isinstance, and bool is the
        # reason as much as an int subclass is: True == 1, so an
        # isinstance check would let a bool answer YES to "is this
        # merge-base's documented not-an-ancestor". It falls through to
        # the comparison below instead, where False is zero and True is
        # nonzero-and-nothing-more. I wrote that as an explicit bool
        # branch first; reverting the branch changed no behaviour and no
        # test, so it was doing nothing but claiming to.
        return value
    return UNESTABLISHED_STATUS


def _git(runner, repo, *args, timeout=60):
    rc, out, err = runner(["git", "-C", str(repo)] + list(args),
                          timeout=timeout)
    return _as_status(rc), _as_text(out).strip(), _as_text(err).strip()


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
    rc, out, err = _git(runner, repo, "status", "--porcelain=v1", "-z",
                        "--untracked-files=all")
    if rc != 0:
        # git's own sentence travels with the status. Discarding it here
        # is why the caller had to invent one, and "cannot read git
        # status" was the invention -- luna.
        return rc, [render_git_diagnostic(rc, err)]
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
    "repo", "remote", "repository_remote", "repository_remote_raw",
    "workspace_identity", "branch", "base_commit", "base_tree",
    "execution_workspace", "clean_at_launch", "dirty_paths", "judgment_ref",
    "launch_host",
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
    judgment_ref = facts.get("judgment_ref")
    schema = facts.get("schema_version", 0)
    if schema >= 3:
        expected = (f"refs/heads/{facts.get('branch')}" if schema >= 4 else
                    f"refs/remotes/origin/{facts.get('branch')}")
        if judgment_ref != expected:
            return (f"the trusted launch snapshot has judgment_ref "
                    f"{judgment_ref!r}, not the schema-{schema} ref "
                    f"{expected!r}")
        if not facts.get("repository_remote"):
            return ("the trusted launch snapshot has no anchored origin URL "
                    "for direct remote-ref judgment")
        if schema >= 5 and not facts.get("repository_remote_raw"):
            return ("the trusted launch snapshot has no anchored raw origin "
                    "push URL for single-pass URL rewriting")
    return None


def effective_remote_ref(facts):
    """Exact wire ref selected by a validated ref-era launch snapshot.

    Schema 3 recorded the local observation vehicle, but its coordinator had
    already anchored the origin URL and generated branch before the agent
    existed. Deriving the remote heads ref from those primitive anchors changes
    only how that selected branch is observed; it does not adopt a new ref.
    """
    schema = (facts or {}).get("schema_version", 0)
    if schema == 3:
        return f"refs/heads/{facts.get('branch')}"
    if schema >= 4:
        return facts.get("judgment_ref")
    return None


def attempt_belongs_to_host(facts, current_host=None):
    """Whether this host owns the attempt: ``True``, ``False``, or unknown.

    The recorded identifier is ``os.uname().nodename``, not a claim about
    physical hardware. If that identifier changes, this predicate must return
    ``False``: no trusted fact proves the renamed environment is the launch
    host, and falling through to its device/inode namespace would recreate the
    cross-host comparison this boundary exists to prevent.

    Unknown is the answer for snapshots written before ``launch_host``. An
    evaluator deciding whether to act on host-local timestamps must require
    ``True``; treating unknown as local recreates the cross-host escalation
    defect, while treating it as foreign invents launch authority.
    """
    launched = (facts or {}).get("launch_host")
    if launched is None:
        return None
    here = os.uname().nodename if current_host is None else current_host
    return launched == here


def launch_host_problem(facts, current_host=None):
    """Why host-scoped launch facts are unjudgeable here, or ``None``.

    ``workspace_identity`` contains device and inode numbers assigned by one
    kernel.  They have no meaning on another host, so callers must ask this
    question before resolving, stating, or running Git in the recorded path.

    Hostless snapshots predate this field.  They retain their legacy identity
    checks: inventing a host after launch would rewrite the anchor rather than
    explain it.  The optional ``current_host`` makes the predicate reusable by
    evaluators that already captured their observation and by focused tests.
    """
    belongs = attempt_belongs_to_host(facts, current_host)
    if belongs is not False:
        return None
    here = os.uname().nodename if current_host is None else current_host
    launched = facts.get("launch_host")
    return (
        f"UNJUDGEABLE HERE: this attempt was launched on host "
        f"{render_for_record(launched, _DIAGNOSTIC_LIMIT)}, but the current "
        f"coordinator is host "
        f"{render_for_record(here, _DIAGNOSTIC_LIMIT)}. Host-scoped paths, "
        f"device numbers, and inode numbers are not compared across that "
        f"boundary")


def remote_push_transport(runner, repo):
    """Return (raw push URL, once-expanded URL, problem) for origin.

    `git remote get-url --push` returns an already-expanded URL. Passing that
    value back to Git can apply another `url.*.insteadOf` rule and reach a
    different repository. Read the configured raw spelling, require one push
    destination, and expand it exactly once for the anchored identity.
    """
    rc, raw, err = _git(
        runner, repo, "config", "--null", "--get-all",
        "remote.origin.pushurl")
    if rc not in (0, 1):
        return None, None, (err or "cannot read remote.origin.pushurl")
    values = [v for v in raw.split("\0") if v] if rc == 0 else []
    if not values:
        rc, raw, err = _git(
            runner, repo, "config", "--null", "--get-all",
            "remote.origin.url")
        if rc != 0:
            return None, None, (err or "origin has no configured URL")
        values = [v for v in raw.split("\0") if v]
    if len(values) != 1:
        return None, None, (
            f"origin has {len(values)} push destinations; one code attempt "
            "can anchor and judge exactly one repository")
    raw_url = values[0]
    # `ls-remote --get-url` applies `url.*.insteadOf` but NOT
    # `url.*.pushInsteadOf`, so for a repository configured with the latter it
    # returns the FETCH destination while `git push origin` writes somewhere
    # else. Judgment then queries a repository the attempt never pushed to and
    # reports that it produced nothing. Reproduced: with
    # `url.<write>.pushInsteadOf=<read>`, the push lands in <write> while
    # `ls-remote --get-url` reports <read>.
    #
    # `git remote get-url --push` is the only resolution that applies push
    # rewrites, and there is no `ls-remote --push`; that flag does not exist.
    rc, resolved, err = _git(
        runner, repo, "remote", "get-url", "--push", "origin")
    if rc != 0 or not resolved:
        return None, None, (
            err or f"cannot resolve origin push destination from {raw_url!r}")
    resolved = resolved.strip().splitlines()
    if len(resolved) != 1 or not resolved[0]:
        return None, None, (
            "origin resolves to %d push destinations; one code attempt can "
            "anchor and judge exactly one repository" % len(resolved))
    return raw_url, resolved[0], None


def _anchored_remote_transport(runner, facts):
    """Revalidate the push route and return its raw, single-pass spelling."""
    raw, resolved, problem = remote_push_transport(runner, facts["repo"])
    if problem:
        return None, problem
    anchored_raw = facts.get("repository_remote_raw")
    if anchored_raw is not None and raw != anchored_raw:
        return None, (
            f"origin raw push URL changed after launch ({anchored_raw!r} -> "
            f"{raw!r}); refusing to select a new repository")
    if resolved != facts.get("repository_remote"):
        return None, (
            f"origin push destination changed after launch "
            f"({facts.get('repository_remote')!r} -> {resolved!r}); refusing "
            "to select a new repository")
    return raw, None


def _set_judgment_state(judgment, state):
    if judgment is not None:
        judgment["production_state"] = state


def _worktree_residue_state(runner, facts):
    """Classify what remains after the authoritative remote ref is absent.

    This is diagnostic only. It never supplies a produced head and cannot turn
    an absent remote ref into production. The worktree is agent-controlled and
    same-UID mutable, so the receipt names this observation's weaker status.
    """
    workspace = facts.get("execution_workspace")
    if not workspace or not os.path.isdir(workspace):
        return "worktree-lost-without-pushed-ref"
    rc, head, _ = _git(runner, workspace, "rev-parse", "HEAD")
    if rc != 0:
        if not os.path.isdir(workspace):
            return "worktree-lost-without-pushed-ref"
        return "no-pushed-ref-worktree-unreadable"
    if head == facts["base_commit"]:
        return "no-produced-change"
    return "worktree-only-change-not-pushed"


def _judge_anchored_ref(runner, facts, judgment=None):
    """Resolve and validate the one durable ref selected before launch.

    The ref name is authority from coordinator state. Its value is not: the
    checker derives that value with Git and validates the resulting immutable
    commit against the separately anchored base and base tree. A different
    pushed ref is deliberately never searched or substituted.
    """
    repo = facts["repo"]
    remote, route_problem = _anchored_remote_transport(runner, facts)
    ref = effective_remote_ref(facts)
    if route_problem:
        _set_judgment_state(judgment, "remote-route-changed")
        return False, None, route_problem
    rc, out, err = _git(runner, repo, "ls-remote", "--exit-code",
                         remote, ref)
    if rc == 2:
        state = _worktree_residue_state(runner, facts)
        _set_judgment_state(judgment, state)
        if state == "worktree-lost-without-pushed-ref":
            detail = ("the managed worktree is also gone, so any work left "
                      "only there died before it could become durable")
        elif state == "no-produced-change":
            detail = ("the managed worktree still names the launch base, so "
                      "this attempt produced no committed change")
        elif state == "worktree-only-change-not-pushed":
            detail = ("a changed commit remains in the mutable managed "
                      "worktree, but it was never pushed to the durable ref")
        else:
            detail = "the remaining managed worktree is unreadable"
        return False, None, (
            f"the anchored remote ref {ref!r} is absent on the anchored "
            f"origin; {detail}. A commit pushed under another ref is never "
            "substituted")
    if rc != 0:
        _set_judgment_state(judgment, "remote-ref-unreadable")
        return False, None, (
            f"cannot resolve anchored remote ref {ref!r} from the anchored "
            f"origin: {render_git_diagnostic(rc, err or out)}")
    lines = [line.split() for line in out.splitlines() if line.strip()]
    if (len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != ref
            or len(lines[0][0]) not in (40, 64)
            or any(c not in "0123456789abcdef"
                   for c in lines[0][0].lower())):
        _set_judgment_state(judgment, "remote-ref-unreadable")
        return False, None, (
            f"anchored origin returned an invalid exact-ref answer for "
            f"{ref!r}; refusing to guess a produced head")
    head = lines[0][0]
    # Fetch the exact anchored ref into a coordinator namespace. ls-remote
    # establishes which value was observed; this fetch makes its commit/tree
    # available even after coordinator cleanup removes the worktree.
    # The explicit refspec ignores remote.origin.fetch and changes no config.
    cache_ref = ("refs/hanig-swarm/judgments/" +
                 str(facts["attempt_id"]))
    rc, _fetch_out, fetch_err = _git(
        runner, repo, "fetch", "--no-tags", "--force",
        "--recurse-submodules=no", remote,
        f"+{ref}:{cache_ref}", timeout=120)
    if rc != 0:
        _set_judgment_state(judgment, "remote-head-unavailable-locally")
        return False, None, (
            f"anchored remote ref {ref!r} resolves to {head[:12]}, but its "
            f"exact commit could not be fetched: {fetch_err[:160]}")
    rc, fetched_head, _ = _git(
        runner, repo, "rev-parse", "--verify", cache_ref + "^{commit}")
    if rc != 0 or fetched_head != head:
        _set_judgment_state(judgment, "remote-ref-moved-during-judgment")
        return False, None, (
            f"anchored remote ref {ref!r} changed while it was being "
            "resolved; refusing to choose between two values")
    base = facts["base_commit"]
    if head == base:
        _set_judgment_state(judgment, "pushed-ref-no-tree-change")
        return False, None, (
            f"the anchored remote ref {ref!r} was pushed but still names the launch base, "
            "so it contains no produced commit")
    rc, _, _ = _git(runner, repo, "merge-base", "--is-ancestor", base, head)
    if rc != 0:
        _set_judgment_state(judgment, "pushed-ref-invalid-history")
        return False, None, (
            f"the anchored remote ref {ref!r} names {head[:12]}, which does "
            f"not descend from anchored base {base[:12]}")
    rc, tree, _ = _git(runner, repo, "rev-parse", head + "^{tree}")
    if rc != 0:
        _set_judgment_state(judgment, "remote-head-tree-unreadable")
        return False, None, (
            f"cannot read the tree of {head[:12]} from anchored remote ref "
            f"{ref!r}")
    if tree == facts["base_tree"]:
        _set_judgment_state(judgment, "pushed-ref-no-tree-change")
        return False, None, (
            f"the anchored remote ref {ref!r} was pushed and advanced, but its tree is "
            "identical to the launch base tree")
    _set_judgment_state(judgment, "pushed-ref-produced-change")
    return True, head, (
        f"coordinator resolved anchored remote ref {ref!r} to {head[:12]}; "
        f"its tree {tree[:12]} differs from anchored base tree "
        f"{facts['base_tree'][:12]}, and it descends from {base[:12]}")


def workspace_identity_problem(runner, facts):
    """Return why judgment no longer addresses the launched Git worktree."""
    host_problem = launch_host_problem(facts)
    if host_problem:
        return host_problem
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
        return (f"cannot identify the anchored worktree "
                f"{render_for_record(workspace, _PATH_LIMIT, collapse=False)}"
                f": {render_for_record(exc, _DIAGNOSTIC_LIMIT)}")
    # WHICH of the three differed. kimi-k2.7-code and glm-5.3 gave the
    # same example: rename /build to /newbuild and leave a symlink, and
    # stat returns the identical device and inode while only resolve()
    # moves. The old message said "(device/inode changed)" -- a cause
    # the stat in this very conditional disproves, sending an operator
    # to hunt a replaced directory that is the same directory.
    differences = []
    if current_path != identity["realpath"]:
        differences.append(
            "the resolved path is now "
            + render_for_record(current_path, _PATH_LIMIT, collapse=False))
    if current.st_dev != identity["device"]:
        differences.append("the device differs")
    if current.st_ino != identity["inode"]:
        differences.append("the inode differs")
    if differences:
        # The join goes through the renderer too. My own AST guard
        # flagged it, correctly: it cannot know the pieces were
        # rendered individually, and rendering the assembled string
        # bounds the COMBINED length, which nothing else did.
        return (f"the anchored worktree path "
                f"{render_for_record(workspace, _PATH_LIMIT, collapse=False)}"
                f" no longer names the launched directory: "
                f"{render_for_record('; '.join(differences), _DIAGNOSTIC_LIMIT, collapse=False)}")
    observed = {}
    for key, args in (
            ("top", ("rev-parse", "--show-toplevel")),
            ("git_common_dir", ("rev-parse", "--git-common-dir")),
            ("git_dir", ("rev-parse", "--git-dir")),
            ("branch", ("rev-parse", "--abbrev-ref", "HEAD"))):
        rc, value, identity_err = _git(runner, workspace, *args)
        if rc != 0:
            # kimi-k2.7-code: "is unreadable" named a cause for every
            # nonzero exit, so `fatal: not a git repository` sent an
            # operator to check file permissions. Report what was asked
            # and what git said; do not decide between them.
            return (f"cannot verify the anchored worktree's Git identity: "
                    f"{render_for_record(key, 32)} could not be determined. "
                    f"{render_git_diagnostic(rc, identity_err)}")
        observed[key] = value
    top = str(Path(observed["top"]).resolve())
    common = str((Path(workspace) / observed["git_common_dir"]).resolve())
    git_dir = str((Path(workspace) / observed["git_dir"]).resolve())
    try:
        common_st = os.stat(common)
        git_st = os.stat(git_dir)
    except OSError as exc:
        return ("cannot stat the anchored Git metadata: "
                + render_for_record(exc, _DIAGNOSTIC_LIMIT))
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
        return (f"the anchored directory "
                f"{render_for_record(workspace, _PATH_LIMIT, collapse=False)}"
                f" no longer has the launched Git worktree metadata "
                f"identity")
    if observed["branch"] != facts.get("branch"):
        # The message luna's 10,000-character branch actually reached.
        # `judge_detail` carries the same sentence and was fixed first;
        # this one fired before it and is the sibling that matters.
        return (f"the repository is on branch "
                f"{render_for_record(observed['branch'], _DIAGNOSTIC_LIMIT)}"
                f", but this attempt was anchored on "
                f"{render_for_record(facts.get('branch'), _DIAGNOSTIC_LIMIT)}")
    return None


def judge_detail(runner, unit_dir, spec, launch_facts=None, judgment=None):
    """(produced, head, detail).

    Returns the head it VALIDATED, not one re-read afterwards. Splitting those
    was a time-of-check/time-of-use gap: judge() checked commit B, and a
    second `rev-parse HEAD` a moment later could return C, because the agent
    owns that repository and nothing stops it moving HEAD. The binding then
    pinned C, which nothing had judged.

    `produced` is None when the unit declared no repository, so there is
    nothing to judge; False when a repository was declared and did not
    transition; True when it did.

    Every history clause exists because its absence admits work that never happened:

      descends-from-base  else an unrelated-history reset, or a branch already
                          ahead at launch, reads as production.
      tree differs        else an empty commit, or a change reverted before
                          committing, reads as production. The comparison is
                          TREE to TREE, not commit to commit, because a commit
                          always differs from its parent.
      selected ref        else a push somewhere else counts here.

    The clean-worktree and inode clauses below apply only to launch snapshots
    predating durable-ref judgment.
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
    err = launch_host_problem(rec)
    if err:
        _set_judgment_state(judgment, "unjudgeable-here")
        return False, None, err
    # Ref-era attempts are judged from the durable remote branch selected
    # before the agent existed. Schema 3 recorded its local tracking name, so
    # effective_remote_ref derives the wire name from the separately anchored
    # origin + branch. Schema <=2 retains worktree-only judgment. This check
    # intentionally precedes every worktree observation: Paseo may delete that
    # directory as soon as the agent closes.
    if effective_remote_ref(rec):
        return _judge_anchored_ref(runner, rec, judgment)
    err = workspace_identity_problem(runner, rec)
    if err:
        return False, None, err
    repo = rec.get("execution_workspace")
    if not repo:
        return False, None, (
            f"this unit declares repo "
            f"{render_for_record(spec['repo'], _PATH_LIMIT, collapse=False)}"
            f", but its launch record "
            f"anchored no execution worktree. The anchor was written before the unit "
            f"declared one, or _write_launch_record failed: either way "
            f"nothing captured a baseline, so re-dispatch this unit rather "
            f"than reading this as a configuration mistake")
    if not os.path.isdir(repo):
        return False, None, (
            # `not isdir` is also true for a regular file and for a
            # path this process cannot stat, so "is gone" sends an
            # operator looking for a deletion that may not have
            # happened -- kimi-k2.7-code, and the same
            # claims-more-than-it-knows shape as the lineage branches.
            f"the anchored repository "
            f"{render_for_record(repo, _PATH_LIMIT, collapse=False)} is "
            f"not a directory. It may be absent, replaced by a file, or "
            f"unreadable from here; this does not distinguish them")

    if not rec.get("clean_at_launch", False):
        return False, None, (
            f"the repository was already dirty at launch "
            f"({render_for_record(rec.get('dirty_paths_at_launch', '?'), 12)}"
            f" path(s)), so there was "
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
        # Name the command and carry git's words; do not decide WHY it
        # failed. A nonzero status here is equally an absent
        # repository, an invalid one, or a git that could not run.
        # The diagnostic is rendered by repo_status, but it arrives
        # here as a list element and the AST guard cannot see that --
        # it flagged the conditional, which is the guard working. One
        # renderer at one boundary means the value is rendered where
        # it is INTERPOLATED, not merely somewhere upstream.
        said = render_for_record(
            dirty[0] if dirty else "git said nothing", _DIAGNOSTIC_LIMIT)
        return False, None, (
            f"git status could not be determined in "
            f"{render_for_record(repo, _PATH_LIMIT, collapse=False)}. "
            f"{said}. That is unknown, not a verdict on the working tree")
    if dirty:
        return False, None, (
            f"{len(dirty)} path(s) are uncommitted. Work left in the working "
            f"tree is not production: it is recorded nowhere another attempt "
            f"or reader could find it")

    rc, branch, _ = _git(runner, repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc == 0 and rec.get("branch") and branch != rec["branch"]:
        return False, None, (
            f"the repository is on branch "
            f"{render_for_record(branch, _DIAGNOSTIC_LIMIT)}, but this attempt "
            f"was anchored on "
            f"{render_for_record(rec['branch'], _DIAGNOSTIC_LIMIT)}")

    base = rec.get("base_commit")
    # The SIBLING of validate_pinned_head's lineage branch, swept with it.
    # Fixing the one a reviewer named and leaving this one is the exact
    # failure CLAUDE.md warns about, and this is the same wound: nine
    # units read a false cause off a collapsed exit status, and this
    # function collapses the same status in the same way one screen up.
    # Every value this function records goes through the renderer. The
    # first pass covered only the four messages I had just rewritten,
    # which luna and kimi-k2.7-code and glm-5.3 all then refuted from a
    # different direction: `rec['branch']` was recorded raw, and an
    # attempt record with a 10,000-character branch put all of it in a
    # durable refusal. An AST sweep of both functions found nine raw
    # interpolations, not one. All nine are rendered now; `len(dirty)`
    # is an int and the PIN_VALIDATION_REFUSAL prefix is ours.
    #
    # The renderer's own docstring records this claim running ahead of
    # the code, once per field, three times. This was the fourth, and
    # fixing the field a reviewer names instead of sweeping is exactly
    # the failure CLAUDE.md warns about -- so the sweep here was
    # mechanical rather than by eye.
    # All three reviewers pointed at the same thing in the same round:
    # I rewrote these refusals and left them interpolating raw, while
    # claiming one renderer at one boundary. The renderer's own docstring
    # already records that this claim ran ahead of the code three times,
    # once per field; this is the fourth, and it is the last place in
    # either function that bypasses it.
    shown_repo = render_for_record(repo, _PATH_LIMIT, collapse=False)
    rc, head, head_err = _git(runner, repo, "rev-parse", "HEAD")
    if rc != 0:
        return False, None, (
            f"HEAD could not be read in {shown_repo}. "
            f"{render_git_diagnostic(rc, head_err)}. That is unknown, not a "
            f"verdict on what the attempt produced")
    if head == base:
        # luna: this established only that HEAD equals the launch base
        # NOW. A run that commits and then resets leaves exactly this
        # state, so "nothing was committed" names a history the check
        # never observed.
        return False, None, (
            "HEAD is the launch base, so this attempt produced nothing "
            "to judge. Whether it never committed or committed and "
            "moved back, this does not distinguish")

    rc, _, ancestor_err = _git(
        runner, repo, "merge-base", "--is-ancestor", base, head)
    if rc == 1:
        # Exit 1 is the DOCUMENTED "not an ancestor". Only here is a
        # verdict on lineage something git actually established.
        # luna, one round after the exit STATUS stopped being collapsed:
        # the prose still was. Exit 1 establishes "not an ancestor" and
        # nothing else -- a sibling-branch commit, a branch already
        # ahead at launch and a force-pushed replacement all produce it,
        # and only one of them is a replacement. Naming that one is the
        # same claims-more-than-it-knows defect this whole change exists
        # to remove, surviving in the sentence after the fix.
        return False, None, (
            f"HEAD {render_for_record(head[:12], 12)} does not descend from "
            f"the anchored base {render_for_record(str(base)[:12], 12)}, so "
            f"what is there now was not built on what we anchored. What "
            f"put it there -- a replaced history, a branch already ahead "
            f"at launch, an unrelated commit checked out -- this does not "
            f"distinguish")
    if rc != 0:
        return False, None, (
            f"the lineage of HEAD {render_for_record(head[:12], 12)} against "
            f"the anchored base {render_for_record(str(base)[:12], 12)} could "
            f"not be determined. {render_git_diagnostic(rc, ancestor_err)}. "
            f"That is unknown, not a verdict on lineage")

    # The tree of the CAPTURED head, not of HEAD. Reading `HEAD^{tree}` was a
    # second look at a moving target: the agent could leave an empty
    # descendant at HEAD for the first read and a content-changing one for
    # this, so the tree that satisfied the check belonged to a commit other
    # than the one returned and pinned.
    rc, tree, tree_err = _git(runner, repo, "rev-parse", head + "^{tree}")
    if rc != 0:
        return False, None, (
            f"the tree of HEAD {render_for_record(head[:12], 12)} could not "
            f"be validated in {shown_repo}. "
            f"{render_git_diagnostic(rc, tree_err)}. That is unknown, not a "
            f"verdict on the tree")
    if tree == rec.get("base_tree"):
        return False, None, (
            "HEAD advanced but its tree is identical to the anchored base "
            "tree, so the content is unchanged. An empty commit, or a change "
            "reverted before committing, moves HEAD without producing "
            "anything")

    return True, head, (
        f"tree {render_for_record(tree[:12], 12)} differs from the anchored "
        f"base tree {render_for_record(str(rec.get('base_tree'))[:12], 12)}, "
        f"on a commit descending from "
        f"{render_for_record(str(base)[:12], 12)}, with a clean tree at both "
        f"ends")


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


def capture_code_judgment(spec, launch_facts, produced, judged_head,
                          judgment=None):
    """Freeze the single repository observation for receipt formatting."""
    schema = (launch_facts or {}).get("schema_version", 0)
    judgment_ref = effective_remote_ref(launch_facts)
    launch_judgment_ref = ((launch_facts or {}).get("judgment_ref")
                           if judgment_ref else None)
    derivation = (
        "derived-from-coordinator-anchored-origin-and-branch"
        if schema == 3 else
        "recorded-exact-remote-ref" if schema >= 4 else None)
    spec.update({
        "produced_head": judged_head,
        "judgment_ref": judgment_ref,
        "launch_judgment_ref": launch_judgment_ref,
        "repository_remote": (launch_facts or {}).get("repository_remote"),
        "repository_remote_raw": (launch_facts or {}).get(
            "repository_remote_raw"),
        "judgment_ref_derivation": derivation,
        "produced_head_derived_from": (
            "coordinator-resolved-exact-remote-ref" if judgment_ref
            else "legacy-live-worktree"),
        "production_state": (judgment or {}).get(
            "production_state",
            "legacy-worktree-produced-change" if produced else
            "legacy-worktree-no-produced-change"),
        "worktree_judged": (
            "no-repository-declared" if produced is None else
            "produced-committed-change" if produced else
            "no-produced-change"),
    })


def judge_and_capture(runner, unit_dir, spec, launch_facts=None):
    """Make one repository observation and freeze all of its receipt fields."""
    judgment = {}
    produced, head, why = judge_detail(
        runner, unit_dir, spec, launch_facts, judgment)
    capture_code_judgment(spec, launch_facts, produced, head, judgment)
    return produced, why


def code_failure_reason(production_state):
    """Machine reason for one failed durable-ref judgment."""
    return {
        "unjudgeable-here": "unjudgeable-here",
        "worktree-lost-without-pushed-ref": "worktree-lost-before-push",
        "worktree-only-change-not-pushed": "no-pushed-ref",
        "no-pushed-ref-worktree-unreadable": "no-pushed-ref",
        "remote-ref-unreadable": "remote-ref-unreadable",
        "remote-head-unavailable-locally": "remote-ref-unreadable",
        "remote-ref-moved-during-judgment": "remote-ref-unreadable",
        "remote-route-changed": "remote-ref-unreadable",
    }.get(production_state, "outputs-absent")


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
                "production_denies": None, "judgment_ref": None,
                "produced_head_derived_from": None,
                "judgment_ref_anchored_before_agent": None,
                "judgment_ref_value_controlled_by": None,
                "judgment_ref_limit": None, "production_state": None,
                "launch_judgment_ref": None,
                "judgment_ref_derivation": None,
                "repository_remote": None, "repository_remote_raw": None}
    return {"worktree_judged": spec.get("worktree_judged"),
            "produced_head": spec.get("produced_head"),
            "production_denies": list(PRODUCTION_DENIES),
            "judgment_ref": spec.get("judgment_ref"),
            "launch_judgment_ref": spec.get("launch_judgment_ref"),
            "judgment_ref_derivation": spec.get(
                "judgment_ref_derivation"),
            "repository_remote": spec.get("repository_remote"),
            "repository_remote_raw": spec.get("repository_remote_raw"),
            "produced_head_derived_from": spec.get(
                "produced_head_derived_from"),
            "production_state": spec.get("production_state"),
            "judgment_ref_anchored_before_agent": bool(
                spec.get("judgment_ref")),
            "judgment_ref_value_controlled_by": (
                "agent push / remote same-UID writers"
                if spec.get("judgment_ref") else None),
            "judgment_ref_limit": ((
                "the coordinator anchored the origin URL and generated branch "
                "selector before the agent existed; the receipt states whether "
                "the exact remote spelling was recorded or derived, resolves "
                "that ref directly from the remote, and validates its commit "
                "against the launch base/tree; the agent controls the pushed "
                "ref value, and this establishes neither authorship nor "
                "protection from hostile same-UID mutation")
                if spec.get("judgment_ref") else
                "legacy attempt was judged from its live worktree")}


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


# A stable prefix so a refusal stays greppable across git versions, wording
# and locale, WITHOUT claiming a category. astra, on the committee: "A stable
# generic label solves discovery; diagnostic localization, if required, is a
# separate presentation decision."
PIN_VALIDATION_REFUSAL = "pinned commit validation failed"

_DIAGNOSTIC_LIMIT = 400
_PATH_LIMIT = 200


def render_for_record(text, limit, collapse=True):
    """One untrusted string, made safe to put in a durable record.

    Applied to EVERY value this refusal interpolates. Bounding git's
    diagnostic and then inserting a recorded repository path verbatim left
    the refusal unbounded through the other field -- kimi-k2.7-code
    demonstrated it with a 10,000 character path -- and the round after
    that found the commit id going in unrendered. Each time the claim was
    ahead of the code by one field, so there is now one renderer and every
    interpolation goes through it.

    ``collapse`` is False for a PATH. A path may legitimately contain a
    space, and collapsing runs of whitespace silently rewrites it, so for
    paths every non-printable -- including a newline or a tab, which a path
    must not contain in a record -- becomes a visible marker and spaces are
    left exactly as recorded. luna and kimi-k2.7-code both caught the first
    version trimming a path it had promised not to trim.
    """
    # TOTAL by construction. luna: a runner that returns bytes made the
    # string join raise TypeError, so a validation failure became an
    # exception instead of a refusal -- the one thing a function whose job
    # is to produce a refusal must never do. `_git` stringifies in the real
    # path, but the runner is injected and nothing enforces its types.
    raw = _as_text(text)
    source = " ".join(raw.split()) if collapse else raw
    safe = "".join(c if c.isprintable() else "?" for c in source)
    if len(safe) <= limit:
        return safe
    # kimi-k2.7-code: with a limit below the marker's own length the result
    # was LONGER than the limit, which is the defect this function exists
    # to prevent, in miniature.
    marker = " [truncated]"
    if limit <= len(marker):
        return safe[:max(0, limit)]
    return safe[:limit - len(marker)] + marker


def render_git_diagnostic(rc, err):
    """Git's own words, attributed to git and safe to put in a record.

    Five review rounds went into classifying this text -- is the object
    absent, is the pack unreadable, is the path missing -- and each round
    found another way for English to be ambiguous. A step-back committee
    (astra, deepseek-v4-pro) agreed unanimously that the classification was
    never load-bearing: no caller reads a category, every consumer treats
    the return value as a human-facing refusal, and git's own sentence is
    what actually diagnoses the case. So the text is reported, not decoded.

    It is still rendered rather than dumped, and the bound covers the
    RENDERED string rather than the payload inside it: bounding the payload
    and then prefixing it put a "400 character" limit at 435.
    """
    # `rc` goes through the renderer too. It is an int from subprocess in
    # every real path, but the runner is an injected callable and nothing
    # enforces its return type -- and "no value reaches the refusal
    # unrendered" is either true or it is a claim a reviewer refutes, which
    # luna did, for this field, after the same claim had already been
    # refuted for the path and for the commit id.
    # render_for_record does the coercion; calling str() here would put the
    # same unguarded conversion back outside the total function, one
    # argument over from where it was just removed.
    if rc is UNESTABLISHED_STATUS:
        # Not "git exited <a status the runner did not report as a
        # number>", which reads as though that phrase were the status.
        code = "with a status this runner did not report as a number"
        prefix = f"git exited {code} and said: "
        rendered = render_for_record(
            err, max(0, _DIAGNOSTIC_LIMIT - len(prefix)))
        return (prefix + rendered).strip() if rendered.strip() else (
            "git exited with a status this runner did not report as a "
            "number and said nothing")
    code = render_for_record(rc, 12)
    # Render BEFORE touching the value. luna, kimi-k2.7-code and glm-5.3 all
    # found the same thing here, and glm named it exactly: this line
    # dereferenced the runner's stderr outside the total renderer, so a
    # truthy value that is neither str nor bytes raised AttributeError and
    # "a validation failure became an exception instead of a refusal -- the
    # exact defect class this change claims to have eliminated".
    #
    # Three rounds running I fixed the call site a reviewer named instead of
    # the boundary. The boundary is this: NOTHING from the runner is touched
    # until it has been through render_for_record, including to ask whether
    # it is empty.
    prefix = f"git exited {code} and said: "
    rendered = render_for_record(
        err, max(0, _DIAGNOSTIC_LIMIT - len(prefix)))
    if not rendered.strip():
        return f"git exited {code} with no diagnostic output"
    return prefix + rendered


def validate_pinned_head(runner, launch_facts, produced):
    """Validate immutable commit ``produced`` against its pinned launch base.

    This never reads HEAD, a branch, the index, or the worktree. A branch may
    move after judgment without changing the answer. Object disappearance is
    an availability failure and fails closed; another ref is never substituted.
    """
    problem = launch_facts_problem(launch_facts)
    if problem:
        return problem
    problem = launch_host_problem(launch_facts)
    if problem:
        return problem
    if not isinstance(produced, str) or len(produced) not in (40, 64) or any(
            c not in "0123456789abcdef" for c in produced.lower()):
        return "the per-attempt produced commit is not a valid object id"
    # Immutable objects remain in the source repository after Paseo archives
    # the finished worktree. Judgment itself uses execution_workspace above;
    # this later pin validation deliberately needs no live checkout.
    repo, base = launch_facts["repo"], launch_facts["base_commit"]
    if not repo:
        return f"{PIN_VALIDATION_REFUSAL}: this attempt recorded no repository"
    rc, _out, cat_err = _git(runner, repo, "cat-file", "-e",
                             produced + "^{commit}")
    if rc != 0:
        # Do NOT name a cause. A nonzero rc here has many: the object is
        # absent; the recorded repository path does not exist on this host;
        # the path is not a repository; a pack is unreadable; a loose object
        # is corrupt; permissions changed. Deciding between them from git's
        # English is what five rounds of review kept finding holes in, and
        # the cost of guessing wrong is not cosmetic -- nine units read
        # "no longer available" while all nine commits were present, and
        # that sentence sent a session looking for work that was never lost.
        #
        # `cat-file -e` is an existence-and-type check, so "could not be
        # validated" is what this establishes; "could not be read" claims
        # more than it knows.
        # `produced` is already validated as 40 or 64 hex characters above,
        # so this cannot smuggle anything -- but "every value goes through
        # the renderer" is either true or it is a claim a reviewer gets to
        # refute, and it has been refuted once for exactly this field.
        return (f"{PIN_VALIDATION_REFUSAL}: the pinned produced commit "
                f"{render_for_record(produced[:12], 12)} could not be "
                f"validated at "
                f"{render_for_record(repo, _PATH_LIMIT, collapse=False)}. "
                f"{render_git_diagnostic(rc, cat_err)}. Refusing rather than "
                f"substituting the current ref; the object may still exist "
                f"in another checkout of the same remote")
    rc, _out, ancestor_err = _git(runner, repo, "merge-base",
                                  "--is-ancestor", base, produced)
    # Every refusal this function emits goes through the renderer, not only
    # the one a reviewer named. glm-5.3 pointed out that these three
    # branches still interpolated raw; it was filed out of scope and is
    # swept anyway, because "some refusals are rendered" is not a property
    # anyone can rely on.
    if rc != 0:
        # `merge-base --is-ancestor` DOCUMENTS exit 1 as "not an ancestor".
        # Any other nonzero is a fatal error -- a bad or missing base
        # object, a shallow clone cut below the base, a corrupt pack --
        # and saying "does not descend" there is a false lineage verdict.
        # glm-5.3 found this surviving here after I swept these branches
        # for the renderer and not for the property the branch exists to
        # enforce: the whole point is that a refusal does not claim a
        # cause the evidence does not establish.
        if rc == 1:
            return (f"{PIN_VALIDATION_REFUSAL}: pinned produced commit "
                    f"{render_for_record(produced[:12], 12)} does not "
                    f"descend from trusted base "
                    f"{render_for_record(base[:12], 12)}")
        return (f"{PIN_VALIDATION_REFUSAL}: the lineage of pinned produced "
                f"commit {render_for_record(produced[:12], 12)} against "
                f"base {render_for_record(base[:12], 12)} could not be "
                f"determined. {render_git_diagnostic(rc, ancestor_err)}. "
                f"That is unknown, not a verdict on lineage")
    rc, tree, tree_err = _git(runner, repo, "rev-parse", produced + "^{tree}")
    if rc != 0:
        # "could not be READ" names the tree as the thing that failed.
        # luna: cat-file and merge-base can both succeed and this still
        # exit nonzero because the checkout went away between commands,
        # and the record then sends an operator after a tree that is
        # fine. The same claims-more-than-it-knows shape as the lineage
        # branch, in the branch after it -- which is the third time this
        # sweep has had to reach one message further down the function.
        return (f"{PIN_VALIDATION_REFUSAL}: the tree of pinned commit "
                f"{render_for_record(produced[:12], 12)} could not be "
                f"validated at "
                f"{render_for_record(repo, _PATH_LIMIT, collapse=False)}. "
                f"{render_git_diagnostic(rc, tree_err)}. That is unknown, "
                f"not a verdict on the tree")
    if tree == launch_facts["base_tree"]:
        return (f"{PIN_VALIDATION_REFUSAL}: the pinned produced commit has "
                f"the launch base's unchanged tree")
    return None
