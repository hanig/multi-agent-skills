"""Read-only D5 installation identity and tri-state classification.

No process, environment, cwd, registry discovery, locks or cached activation.
This module is also shipped to remote stages, where the originating path is
an audit locator and the originating host already performed the admission check.
"""
import json
import os
from pathlib import Path
import re
import stat

MARKER = "d5-readiness.json"
INSTALLATION = "installation.json"
SENTINEL = "d5-installed.json"
PROFILE = "synchronous-merge-v1"
NOT_INSTALLED, READY, BROKEN = "NOT_INSTALLED", "READY", "BROKEN"
BINDING = ("registration_id", "plan_id", "state_dir", "record_dir",
           "repo_key", "state_key", "record_key")


def _require(ok, message):
    if not ok:
        raise ValueError("D5 readiness: " + message)


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, "duplicate installation field")
        result[key] = value
    return result


def _state_directory(directory):
    _require(directory is not None, "state_dir is required and must exist")
    try:
        return Path(os.path.realpath(directory))
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        raise ValueError("D5 readiness: invalid state_dir") from exc


def _read(path):
    try:
        # No registry lookup or subprocess. Refuse special files before reading.
        _require(stat.S_ISREG(path.lstat().st_mode), "nonregular installation state")
        return json.loads(path.read_bytes(), object_pairs_hook=_pairs)
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError("D5 readiness: missing or malformed installation state") from exc


def _validated(value):
    _require(type(value) is dict and set(value) == set(BINDING) | {
        "schema_version", "generation", "profile"}, "invalid installation fields")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1,
             "invalid installation schema")
    for key in ("generation", "registration_id"):
        _require(type(value[key]) is str and re.fullmatch(r"[0-9a-f]{32}", value[key]),
                 "invalid " + key)
    _require(value["profile"] == PROFILE, "unsupported route profile")
    _require(type(value["plan_id"]) is str and
             re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value["plan_id"]),
             "invalid plan_id")
    for key in ("state_dir", "record_dir"):
        _require(type(value[key]) is str and Path(value[key]).is_absolute(), "invalid " + key)
        if key == "state_dir":
            _require(str(Path(value[key]).resolve()) == value[key], "noncanonical " + key)
    for key in ("repo_key", "state_key", "record_key"):
        pair = value[key]
        _require(type(pair) is list and len(pair) == 2 and
                 all(type(n) is int and n >= 0 for n in pair), "invalid " + key)
    return value


def installation(directory):
    """Read the record's installation binding without ambient state or Git."""
    directory = Path(directory)
    value = _validated(_read(directory / INSTALLATION))
    _require(directory.samefile(value["record_dir"]), "record directory mismatch")
    for name, key in (("state_dir", "state_key"), ("record_dir", "record_key")):
        _require([Path(value[name]).stat().st_dev, Path(value[name]).stat().st_ino] == value[key],
                 "installation object mismatch")
    return value


def _snapshot(state_dir):
    directory = _state_directory(state_dir)
    try:
        # lstat distinguishes absent from unreadable and broken symlinks.
        present = False
        for name in (MARKER, SENTINEL):
            try:
                (directory / name).lstat()
                present = True
            except FileNotFoundError:
                pass
        if not present:
            return NOT_INSTALLED, None
        sentinel = _validated(_read(directory / SENTINEL))
        value = _validated(_read(directory / MARKER))
        _require(value["state_dir"] == str(directory), "state directory mismatch")
        _require(value == sentinel, "installation sentinel mismatch")
        installed = installation(value["record_dir"])
        _require(value == installed, "installation generation or binding mismatch")
        return READY, value
    except (OSError, ValueError, RuntimeError, RecursionError):
        return BROKEN, None


def state(state_dir):
    """Uncached, read-only classification; no lease acquisition or Git."""
    return _snapshot(state_dir)[0]


