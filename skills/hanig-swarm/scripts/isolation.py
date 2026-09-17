"""Declared container host-bind isolation and receipt rendering.

The coordinator owns the applied-wrapper facts.  This module admits those
facts only when they identify this attempt and when the application marker
matches.  Attempt files alone never upgrade the receipt basis.

Python 3.8+, standard library only.
"""

import json
import os
import re
from pathlib import Path


DEFAULT_FIELDS = {
    "conclusive_because": (
        "exclusive by coordinator allocation under a trusted-writer "
        "convention"),
    "os_enforced_isolation": False,
    "note": (
        "not isolated from other processes running as the same Unix user. "
        "OS-enforced isolation would need a container or mount namespace "
        "with this directory as the only writable bind mount."),
}


def _decode(payload, unit_dir, spec):
    if payload is None:
        return None, None
    try:
        facts = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, ValueError) as exc:
        return None, f"isolation facts are not readable JSON: {exc}"
    root = str(Path(unit_dir).resolve())
    if not isinstance(facts, dict):
        return None, "isolation facts are not an object"
    required = {
        "schema_version": 1,
        "unit_id": spec.get("task_id"),
        "attempt_id": spec.get("attempt_id"),
        "applied_to_submission": True,
        "mechanism": "container-host-bind-write-scope",
    }
    for key, expected in required.items():
        if facts.get(key) != expected:
            return None, (f"isolation facts {key}={facts.get(key)!r}, not "
                          f"the expected {expected!r}")
    if facts.get("backend") not in ("apptainer", "singularity"):
        return None, (f"isolation facts name unsupported backend "
                      f"{facts.get('backend')!r}")
    if facts.get("writable_host_binds") != [root]:
        return None, ("isolation facts do not name this attempt root as the "
                      "one writable host bind")
    read_only = facts.get("read_only_host_binds")
    if not isinstance(read_only, list) or any(
            not isinstance(path, str) or not os.path.isabs(path)
            for path in read_only):
        return None, "isolation facts contain invalid read-only host binds"
    marker = str(Path(root) / ".swarm-isolation-applied-v1")
    token = facts.get("application_token_sha256")
    if (facts.get("application_marker") != marker
            or not isinstance(token, str)
            or re.fullmatch(r"[0-9a-f]{64}", token) is None):
        return None, "isolation facts contain no valid application marker"
    try:
        observed = Path(marker).read_text()[:128].strip()
    except OSError as exc:
        return None, (f"application marker is absent or unreadable "
                      f"({exc})")
    if observed != token:
        return None, "application marker does not match coordinator facts"
    return facts, None


def receipt_fields(payload, unit_dir, spec):
    """Return (basis fields, error); defaults preserve historical wording."""
    facts, error = _decode(payload, unit_dir, spec)
    if not facts:
        if error:
            error += (". OS-enforced isolation is not reported; attempt-"
                      "directory files cannot reconstruct this coordinator "
                      "fact.")
        return dict(DEFAULT_FIELDS), error
    return {
        "conclusive_because": (
            "exclusive by coordinator allocation under a trusted-writer "
            "convention; the dispatched workload's host writes were "
            "OS-confined to the attempt-root bind"),
        "os_enforced_isolation": True,
        "isolation_profile": {
            "kind": "container",
            "backend": facts.get("backend"),
            "image": facts.get("image"),
            "writable_host_binds": facts.get("writable_host_binds"),
            "read_only_host_binds": facts.get("read_only_host_binds"),
            "applied_to_submission": True,
            "application_evidence": (
                "post-success container execution marker matched"),
        },
        "note": (
            "the container restricted this workload's writable host binds "
            "to the attempt root and mounted declared inputs read-only. It "
            "still ran as the invoking Unix user, so another process already "
            "running as that user could write into the attempt root or forge "
            "the application marker; PID, network, and process-tree "
            "quiescence are not claimed. Tools needing a writable cache in "
            "the host HOME cannot use this profile."),
    }, None


def require_application(required, fields, state, notes):
    """Prevent a declared profile from closing under the weaker basis."""
    if (required and not fields["os_enforced_isolation"]
            and state == "DONE"):
        notes.append(
            "declared OS-backed isolation has no matching application "
            "evidence; refusing to close under the weaker trusted-writer "
            "basis")
        return "INCOMPLETE"
    return state
