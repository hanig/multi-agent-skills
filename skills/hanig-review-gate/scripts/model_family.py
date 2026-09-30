"""Model families and the per-project resolution snapshot. No network.

A seat in reviewers.json or agents.json may declare a `family`: a literal
vendor, prefix and suffix around a version `N` or `N<sep>M`, plus `major`, the
owner's ceiling. `resolve_models.py` lists a provider's catalog, picks the
greatest same-major point release above the shipped pin, probes it, and writes
one snapshot per project under the state home. Readers (review.py,
committee.py, and swarm.py by file) apply a snapshot only when it matches the
project and the exact bytes of the installed routing config, and only after
re-checking each entry. See docs/plan-model-resolution.md.

This module is imported by swarm.py, so it must stay free of network code and
must never import review.py or committee.py.
"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

SCHEMA = 1
STATE_DIR = ("hanig-review-gate", "routing")
LISTINGS = ("openai", "openrouter")
PASEO_PREFIX = "paseo:"
FAMILY_FIELDS = ("listing", "vendor", "prefix", "separator", "suffix", "major")


class FamilyError(ValueError):
    """A family declaration that cannot be used."""


def split_id(model_id):
    """(vendor path, name): split once at the last "/"; no "/" means no vendor."""
    vendor, slash, name = model_id.rpartition("/")
    return (vendor if slash else ""), name


def check_family(family):
    """Validate a family declaration; return it, or raise FamilyError."""
    if not isinstance(family, dict):
        raise FamilyError("family must be an object")
    listing = family.get("listing")
    if not (listing in LISTINGS or (isinstance(listing, str)
                                    and listing.startswith(PASEO_PREFIX)
                                    and len(listing) > len(PASEO_PREFIX))):
        raise FamilyError("family.listing must be openai, openrouter or "
                          "paseo:<provider>, got %r" % (listing,))
    for field in ("vendor", "prefix", "suffix"):
        if not isinstance(family.get(field, ""), str):
            raise FamilyError("family.%s must be a string" % field)
    if not family.get("prefix"):
        raise FamilyError("family.prefix must be a non-empty string")
    if family.get("separator", ".") not in (".", "-"):
        raise FamilyError("family.separator must be \".\" or \"-\"")
    major = family.get("major")
    if not (isinstance(major, int) and not isinstance(major, bool) and major >= 0):
        raise FamilyError("family.major must be a non-negative integer")
    return family


def version_of(model_id, family):
    """(major, minor) when model_id is in the family, else None.

    In the family means: vendor path equals family.vendor exactly, and the
    name is exactly prefix + digits [+ separator + digits] + suffix. Nothing
    else is accepted, so -pro, :batch, dated snapshots and previews are simply
    not in the family.
    """
    if not isinstance(model_id, str):
        return None
    vendor, name = split_id(model_id)
    if vendor != family.get("vendor", ""):
        return None
    prefix, suffix = family["prefix"], family.get("suffix", "")
    if not (name.startswith(prefix) and name.endswith(suffix)
            and len(name) > len(prefix) + len(suffix)):
        return None
    core = name[len(prefix):len(name) - len(suffix)]
    sep = re.escape(family.get("separator", "."))
    match = re.fullmatch(r"([0-9]+)(?:%s([0-9]+))?" % sep, core)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def select(listed_ids, family, pinned):
    """Choose from a catalog. Returns (chosen or None, newer-generation ids).

    Candidates are listed ids in the family whose major equals family.major
    and whose version is above the pin's. The greatest is chosen. An id with a
    greater major is reported, never chosen: raising the ceiling is a reviewed
    config change.
    """
    floor = version_of(pinned, family)
    if floor is None:
        raise FamilyError("pinned model %r is not in its own family" % pinned)
    major = family["major"]
    same, newer = [], []
    for model_id in listed_ids:
        version = version_of(model_id, family)
        if version is None:
            continue
        if version[0] == major and version > floor:
            same.append((version, model_id))
        elif version[0] > major:
            newer.append(model_id)
    same.sort()
    return (same[-1][1] if same else None), sorted(set(newer))


def file_sha256(path):
    """Digest of a file's exact bytes, or None when it does not exist."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def config_digests(reviewers_path, agents_path):
    return {"reviewers.json": file_sha256(reviewers_path),
            "agents.json": file_sha256(agents_path)}


def project_key(project_dir):
    resolved = os.path.realpath(str(project_dir))
    return hashlib.sha256(resolved.encode("utf-8", "surrogateescape")).hexdigest()


def git_toplevel(start):
    """Nearest ancestor of start (inclusive) holding .git, or None."""
    here = Path(os.path.realpath(str(start)))
    for candidate in (here,) + tuple(here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def state_home():
    """The review gate's state home: absolute XDG_STATE_HOME or ~/.local/state."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and os.path.isabs(os.path.expanduser(xdg)):
        return Path(os.path.abspath(os.path.expanduser(xdg)))
    return Path.home() / ".local" / "state"


def snapshot_dir():
    return state_home().joinpath(*STATE_DIR)


def snapshot_path(project_dir):
    return snapshot_dir() / (project_key(project_dir) + ".json")


def _inside(path, root):
    try:
        Path(os.path.realpath(str(path))).relative_to(os.path.realpath(str(root)))
        return True
    except ValueError:
        return False


def write_snapshot(project_dir, snapshot):
    """Write atomically under the state home; refuse a path inside the project."""
    project = Path(os.path.realpath(str(project_dir)))
    top = git_toplevel(project) or project
    target = snapshot_path(project)
    if _inside(target.parent, top) or _inside(target.parent, project):
        raise OSError("routing snapshot %s would sit inside project %s; set "
                      "XDG_STATE_HOME outside it" % (target, top))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, tmp = tempfile.mkstemp(prefix=".snapshot-", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(snapshot, out, indent=1, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def find_snapshot(start=None):
    """Nearest snapshot for start or a parent, up to the Git top-level.

    Returns (path, parsed snapshot, project dir) or (None, None, None). An
    unreadable or malformed file is treated as absent: readers fall back to
    the pins.
    """
    if os.environ.get("HANIG_ROUTING_SNAPSHOTS") == "off":
        return None, None, None
    here = Path(os.path.realpath(str(start or os.getcwd())))
    top = git_toplevel(here)
    chain = [here]
    for parent in here.parents:
        if top is not None and not _inside(parent, top):
            break
        chain.append(parent)
    for candidate in chain:
        path = snapshot_path(candidate)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return path, data, candidate
    return None, None, None


def _snapshot_matches(data, project_dir, digests):
    return (isinstance(data, dict) and data.get("schema") == SCHEMA
            and data.get("project_key") == project_key(project_dir)
            and data.get("config_sha256") == digests)


def reviewer_override(seat, entry):
    """The (model, record) a snapshot entry may set on a reviewer seat, or None.

    Every check is against the LIVE seat, so an entry cannot raise the major,
    go below the pin, leave the family, or carry a record that names another
    model, provider, cap or effort.
    """
    family = seat.get("family")
    if not isinstance(entry, dict) or not isinstance(family, dict):
        return None
    try:
        check_family(family)
    except FamilyError:
        return None
    model, record = entry.get("model"), entry.get("record")
    version, floor = version_of(model, family), version_of(seat.get("model"), family)
    if version is None or floor is None:
        return None
    if version[0] != family["major"] or version <= floor:
        return None
    if not isinstance(record, dict):
        return None
    expected = {"model": model, "provider": seat.get("provider"),
                "max_output_tokens": seat.get("max_output_tokens"),
                "outcome": "completed"}
    if any(record.get(key) != value for key, value in expected.items()):
        return None
    if record.get("effort") != seat.get("effort"):
        return None
    tokens = record.get("output_tokens")
    if not (isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0):
        return None
    return model, record


def apply_to_reviewers(reviewers, digests, start=None):
    """Return (reviewers with valid snapshot entries applied, notes).

    Seats keep their pins unless a matching snapshot holds a valid entry for
    them. Each applied seat gains `_resolved_from` naming its pin.
    """
    path, data, project = find_snapshot(start)
    if data is None:
        return reviewers, []
    if not _snapshot_matches(data, project, digests):
        return reviewers, ["routing snapshot %s ignored: it does not match this "
                           "project and the installed routing config" % path]
    entries = data.get("reviewers") if isinstance(data.get("reviewers"), dict) else {}
    out, notes = [], []
    for seat in reviewers:
        override = reviewer_override(seat, entries.get(seat.get("name")))
        if override is None:
            if seat.get("name") in entries:
                notes.append("routing snapshot entry for %s ignored: it fails "
                             "the family, major, pin or probe checks"
                             % seat.get("name"))
            out.append(seat)
            continue
        model, record = override
        out.append(dict(seat, model=model, _max_output_tokens_accepted=record,
                        _resolved_from=seat["model"]))
    return out, notes


def agent_default_override(default, thinking_table, entry):
    """The provider string a snapshot may set as the code-agent default, or None."""
    family = default.get("family") if isinstance(default, dict) else None
    if not isinstance(entry, dict) or not isinstance(family, dict):
        return None
    try:
        check_family(family)
    except FamilyError:
        return None
    pinned = default.get("provider", "")
    route, slash, pinned_model = pinned.partition("/")
    provider = entry.get("provider")
    if not (isinstance(provider, str) and provider.startswith(route + "/")):
        return None
    model = provider[len(route) + 1:]
    version, floor = version_of(model, family), version_of(pinned_model, family)
    if version is None or floor is None:
        return None
    if version[0] != family["major"] or version <= floor:
        return None
    probe = entry.get("probe")
    thinking = thinking_table.get(pinned, default.get("thinking"))
    if not (isinstance(probe, dict) and probe.get("Provider") == route
            and probe.get("Model") == model and probe.get("Thinking") == thinking):
        return None
    return provider


def resolved_agent_default(default, thinking_table, digests, start=None):
    """(provider string, note) for the code-agent default; the pin when absent."""
    pinned = default.get("provider")
    path, data, project = find_snapshot(start)
    if data is None or not _snapshot_matches(data, project, digests):
        return pinned, None
    provider = agent_default_override(default, thinking_table, data.get("agent_default"))
    if provider is None:
        return pinned, None
    return provider, "code-agent default resolved %s -> %s" % (pinned, provider)
