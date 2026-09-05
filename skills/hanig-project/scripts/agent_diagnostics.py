#!/usr/bin/env python3
"""Read-only readiness evidence layered on :mod:`agent_discovery`.

``agent_discovery`` answers where a supported agent is expected to look.  This
module deliberately does *not* turn a directory listing or a SKILL.md into a
claim that a native loader accepted it: installed, discovery, and workflow
readiness remain three separately machine-readable facts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

# This module is copied into user skill stores and is itself a diagnostic.
# Importing its shared adapter must not leave a __pycache__ write behind.
sys.dont_write_bytecode = True
import agent_discovery


SCHEMA_VERSION = 1
MARKER = ".installed-by-multi-agent-skills"
MAX_ROOT_ENTRIES = 4_000
MAX_MARKER_BYTES = 16_384
DOCTOR_JSON_BYTES = 48_000
REPOSITORY_ID = "multi-agent-skills"
LIFECYCLE_SCHEMA = "2"
SIDECAR_DIR = ".multi-agent-skills-provenance"


def _state(state: str, reason: Optional[str] = None, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"state": state}
    if reason:
        item["reason"] = reason
    item.update(extra)
    return item


def _read_fields(path: Path) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Read lifecycle metadata without allowing a payload to exhaust memory."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_MARKER_BYTES + 1)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, str(exc)
    if len(raw) > MAX_MARKER_BYTES:
        return None, "metadata exceeds bounded read limit"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "metadata is not valid UTF-8"
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        if "=" not in line:
            return None, "metadata contains a malformed nonempty line"
        key, value = line.split("=", 1)
        if not key:
            return None, "metadata contains an empty field name"
        if key in values:
            return None, f"metadata contains duplicate field: {key}"
        values[key] = value
    return values, None


def _schema_two_problem(values: Mapping[str, str], destination: Path, *, linked: bool) -> Optional[str]:
    """Validate the self-contained schema-2 ownership fields.

    This mirrors lifecycle's conservative parser locally because installed
    copies of this diagnostic have no checkout ``lib`` package to import.
    """
    required = ("schema", "repo", "origin", "source_version", "version",
                "destination", "consumers", "mode", "installed_at",
                "link_target", "link_identity")
    missing = [key for key in required if key not in values]
    if missing:
        return "record is incomplete: missing " + ", ".join(missing)
    if not values["repo"] or "=" in values["repo"]:
        return "repository identity is invalid"
    if values["origin"] not in ("authored", "vendored"):
        return "origin is invalid"
    if not values["source_version"] or values["source_version"] != values["version"]:
        return "source version is empty or does not match compatibility version"
    if not values["installed_at"]:
        return "installation timestamp is empty"
    raw_consumers = values["consumers"]
    consumers = [] if raw_consumers == "" else raw_consumers.split(",")
    if any(not value or any(char in value for char in "\n\r=") for value in consumers):
        return "consumer list is invalid"
    expected_mode = "link" if linked else "copy"
    if values["mode"] not in ("copy", "link"):
        return "mode is invalid"
    if values["mode"] != expected_mode:
        return "recorded mode does not match this payload"
    if (not values["destination"] or not os.path.isabs(values["destination"]) or
            _destination_identity(values["destination"]) != _destination_identity(destination)):
        return "recorded destination does not match this payload"
    if linked:
        if (not values["link_target"] or not os.path.isabs(values["link_target"]) or
                not values["link_identity"]):
            return "link ownership fields are incomplete or invalid"
    elif values["link_target"] or values["link_identity"]:
        return "copy record contains link ownership fields"
    return None


def _destination_identity(path: Path | str) -> str:
    """Resolve parent aliases without following a final payload link."""
    absolute = os.path.abspath(os.fspath(path))
    return os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))


def _sidecar(path: Path) -> Path:
    identity = _destination_identity(path)
    digest = hashlib.sha256(os.fsencode(identity)).hexdigest()[:24]
    destination = Path(identity)
    return destination.parent / SIDECAR_DIR / f"{destination.name}-{digest}.provenance"


def _link_identity(path: Path) -> Optional[str]:
    try:
        stat = path.lstat()
    except OSError:
        return None
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_ctime_ns}"


def _marker(path: Path, *, linked: bool = False) -> tuple[dict[str, Any], Optional[str]]:
    """Classify only a complete lifecycle record as owned provenance.

    This deliberately mirrors the on-disk schema, not lifecycle's Python API:
    a copied survey has no source checkout or ``lib`` package to import.
    """
    source = _sidecar(path) if linked else path / MARKER
    values, error = _read_fields(source)
    base = {"ownership": "foreign", "installed_source_version": None,
            "provenance": _state("absent", path=str(source))}
    if error:
        return {**base, "ownership": "unknown",
                "provenance": _state("unknown", error, path=str(source))}, error
    if values is None:
        return base, None
    if values.get("schema") != LIFECYCLE_SCHEMA:
        repository = values.get("repo")
        if repository != REPOSITORY_ID:
            return {**base, "provenance": _state("foreign", path=str(source), repo=repository)}, None
        return {**base, "ownership": "unknown",
                "provenance": _state("legacy", "not lifecycle schema 2", path=str(source))}, None
    problem = _schema_two_problem(values, path, linked=linked)
    if problem:
        return {**base, "ownership": "unknown",
                "provenance": _state("stale", problem, path=str(source))}, None
    repository = values["repo"]
    if repository != REPOSITORY_ID:
        return {**base, "provenance": _state("foreign", path=str(source), repo=repository)}, None
    origin = values["origin"]
    mode = values["mode"]
    if linked:
        identity = _link_identity(path)
        try:
            target_matches = bool(values.get("link_target")) and (
                os.path.realpath(path) == os.path.realpath(values["link_target"]))
        except OSError:
            target_matches = False
        if not identity or identity != values.get("link_identity") or not target_matches:
            return {**base, "ownership": "unknown",
                    "provenance": _state("stale", "link object or target changed", path=str(source))}, None
    return {"ownership": "vendored" if origin == "vendored" else "owned",
            "installed_source_version": values.get("source_version", values.get("version")),
            "provenance": _state("valid", path=str(source), schema=LIFECYCLE_SCHEMA,
                                 mode=mode)}, None


def _payload(entry: os.DirEntry[str]) -> dict[str, Any]:
    path = Path(entry.path)
    item: dict[str, Any] = {"name": entry.name, "path": str(path)}
    try:
        is_link = entry.is_symlink()
        is_dir = entry.is_dir(follow_symlinks=True)
    except OSError as exc:
        return {**item, **_state("unusable", f"could not inspect entry: {exc}"),
                "ownership": "unknown", "installed_source_version": None}
    marker, marker_error = _marker(path, linked=is_link)
    item.update(marker)
    if marker_error:
        item["marker_error"] = marker_error
    if is_link:
        try:
            item["link_target"] = os.readlink(path)
        except OSError as exc:
            return {**item, **_state("unusable", f"could not read link: {exc}"),
                    "ownership": "unknown", "installed_source_version": None}
    if not is_dir:
        return {**item, **_state("unusable", "not a directory"),
                "ownership": "unknown", "installed_source_version": None}
    skill = path / "SKILL.md"
    try:
        skill_exists = skill.is_file()
        skill_readable = os.access(skill, os.R_OK) if skill_exists else False
    except OSError as exc:
        return {**item, **_state("unusable", f"could not inspect SKILL.md: {exc}"),
                "skill_file": _state("unknown")}
    item["skill_file"] = _state("present" if skill_readable else
                                ("unusable" if skill_exists else "absent"))
    if not skill_exists:
        return {**item, **_state("unusable", "SKILL.md is absent")}
    if not skill_readable:
        return {**item, **_state("unusable", "SKILL.md is not readable")}
    return {**item, **_state("present")}


def _installation(root: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(root["logical_path"])
    base = {key: root[key] for key in ("id", "kind", "preferred", "logical_path",
                                       "physical_path", "override")}
    try:
        with os.scandir(path) as entries:
            payloads = []
            for index, entry in enumerate(entries):
                if index >= MAX_ROOT_ENTRIES:
                    return {**base, **_state("unknown", "root entry limit reached"),
                            "payloads": payloads, "truncated": True}
                if entry.name.startswith("."):
                    continue
                payloads.append(_payload(entry))
    except FileNotFoundError:
        return {**base, **_state("absent"), "payloads": []}
    except NotADirectoryError:
        return {**base, **_state("unusable", "root is not a directory"), "payloads": []}
    except PermissionError:
        return {**base, **_state("unusable", "root is not readable"), "payloads": []}
    except OSError as exc:
        return {**base, **_state("unknown", f"could not read root: {exc}"), "payloads": []}
    status = "present" if payloads else "absent"
    return {**base, **_state(status), "payloads": payloads}


def _on_path(name: str, env: Mapping[str, str]) -> Optional[str]:
    for directory in env.get("PATH", "").split(os.pathsep):
        candidate = Path(directory or os.curdir) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _workflow(env: Mapping[str, str], roots: list[dict[str, Any]]) -> dict[str, Any]:
    """Report baseline and optional workflow evidence without probing agents."""
    home = Path(env.get("HOME", str(Path.home())))
    bus_home = Path(env.get("AGENT_BUS_HOME", home / ".agent-bus"))
    registry = bus_home / "models.json"
    if registry.is_file() and os.access(registry, os.R_OK):
        try:
            models = json.loads(registry.read_text(encoding="utf-8", errors="replace"))
            valid_registry = isinstance(models, dict) and isinstance(models.get("models"), list)
            bus = _state("present" if valid_registry else "unusable",
                         None if valid_registry else "models.json has no models list",
                         path=str(registry))
        except (OSError, json.JSONDecodeError) as exc:
            bus = _state("unusable", f"could not parse models.json: {type(exc).__name__}",
                         path=str(registry))
    elif registry.exists() or registry.is_symlink():
        bus = _state("unusable", "models.json is not a readable regular file", path=str(registry))
    else:
        bus = _state("absent", "models.json is absent", path=str(registry))
    paseo, bus_helper, git = (_on_path("paseo", env), _on_path("bus", env),
                               _on_path("git", env))
    baseline = {
        "python": _state("ready", path=sys.executable),
        "git": _state("ready", path=git) if git else _state("absent", "git is not on PATH"),
    }
    optional = {
        # An executable on PATH is not proof that its daemon/service is up.
        "paseo_executable": _state("found", path=paseo) if paseo else _state("absent", "paseo is not on PATH"),
        "paseo_service": _state("unverified", "no service health probe was run"),
        # The registry is input data, not a successful `bus models` run.
        "agent_bus_registry": bus,
        "agent_bus_helper": (_state("found", path=bus_helper) if bus_helper else
                             _state("absent", "bus is not on PATH")),
        "linear": _state("unverified", "credential-free diagnostics do not test Linear capability"),
    }
    blocked = [name for name, item in baseline.items() if item["state"] != "ready"]
    payloads = [payload for root in roots for payload in root.get("payloads", [])
                if payload.get("state") == "present"]
    per_skill: dict[str, Any] = {}
    for payload in payloads:
        name = payload["name"]
        requirements = ["python", "git"]
        if name.startswith(("paseo", "hanig-swarm", "pi-fleet")):
            requirements.extend(("paseo_executable", "paseo_service"))
        if name in ("paseo", "paseo-loop", "paseo-committee", "agent-bus", "pi-fleet"):
            requirements.extend(("agent_bus_registry", "agent_bus_helper"))
        if name.startswith(("hanig-project", "hanig-swarm", "start-a-sprint")):
            requirements.append("linear")
        facts = {**baseline, **optional}
        unavailable = [requirement for requirement in requirements
                       if facts[requirement]["state"] in ("absent", "unusable")]
        uncertain = [requirement for requirement in requirements
                     if facts[requirement]["state"] in ("unverified", "present", "found")]
        per_skill[name] = {"state": "unready" if unavailable else
                            "unverified" if uncertain else "baseline_ready",
                           "requirements": requirements,
                           "next_step": ("Install or expose " + ", ".join(unavailable) + "." if unavailable else
                                         "Confirm optional service/connector readiness without starting an agent session."
                                         if uncertain else None)}
    return {"state": "baseline_unready" if blocked else "baseline_ready",
            "baseline_dependencies": baseline, "optional_dependencies": optional,
            "skills": per_skill,
            "next_step": ("Install or expose " + ", ".join(blocked) + "." if blocked else
                          "Inspect each installed skill's optional readiness separately; no agent sessions were started.")}


def _duplicate_names(roots: list[dict[str, Any]]) -> dict[str, list[str]]:
    locations: dict[str, list[str]] = {}
    for root in roots:
        for payload in root.get("payloads", []):
            if payload.get("state") == "present":
                locations.setdefault(payload["name"], []).append(root["id"])
    return {name: ids for name, ids in sorted(locations.items()) if len(ids) > 1}


def diagnostics(env: Optional[Mapping[str, str]] = None,
                claude_prefix: Optional[str] = None) -> dict[str, Any]:
    """Return stable, additive installation/discovery/workflow facts.

    ``claude_prefix`` exists solely for ``doctor --prefix`` compatibility; it
    does not alter the adapter's selection contract or create a directory.
    """
    context = dict(os.environ if env is None else env)
    context.setdefault("HOME", str(Path.home()))
    report = agent_discovery.discover(env=context)
    agents: dict[str, Any] = {}
    for name, adapter in report["agents"].items():
        roots = list(adapter["roots"])
        if name == "claude" and claude_prefix is not None:
            logical = os.path.abspath(claude_prefix)
            roots[0] = {**roots[0], "logical_path": logical,
                        "physical_path": os.path.realpath(logical), "exists": os.path.isdir(logical),
                        "override": "doctor --prefix"}
        installations = [_installation(root) for root in roots]
        barriers = [root for root in installations if root["state"] in ("unusable", "unknown")]
        discovery_state = "blocked" if barriers else "unverified"
        discovery_reason = ("configured root is unreadable or indeterminate" if barriers else
                            "no known read-only native skill-loading probe was run")
        agents[name] = {
            "identity": adapter["identity"],
            "agent_present": _state(adapter["state"], version=adapter["version"],
                                      executable=adapter["evidence"].get("executable")),
            "installation": {"state": ("present" if any(r["state"] == "present" for r in installations)
                                         else "unusable" if barriers else "absent"),
                             "roots": installations,
                             "duplicate_names": _duplicate_names(installations)},
            "discovery": _state(discovery_state, discovery_reason,
                                verification=adapter["verification"],
                                native_probe="not run"),
            "workflow": _workflow(context, installations),
            "next_step": ("Use an explicitly supported version or pass an explicit target; this executable version is unverified."
                          if adapter["state"] == "executable_found" and adapter["verification"] == "unverified"
                          else "Install the agent executable or select this agent explicitly for an offline install."
                          if adapter["state"] == "absent" else None),
        }
    return {"schema_version": SCHEMA_VERSION, "adapter_schema_version": report["schema_version"],
            "read_only": True, "credential_free": True, "agents": agents,
            "destinations": report["destinations"],
            # Keep the exact planner result alongside the observations so
            # automation sees the same shared-root/duplicate decision that
            # install.sh will consume, rather than a parallel approximation.
            "selection": agent_discovery.select_targets(report)}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit compact stable JSON")
    parser.add_argument("--doctor-json", action="store_true",
                        help="emit complete JSON or a bounded truncation record for doctor")
    parser.add_argument("--claude-prefix", help="doctor compatibility diagnostic root")
    args = parser.parse_args(argv)
    value = diagnostics(claude_prefix=args.claude_prefix)
    if args.json or args.doctor_json:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if args.doctor_json and len(encoded.encode("utf-8")) > DOCTOR_JSON_BYTES:
            # doctor transports a child result through a fixed 64 KiB tail.
            # Never emit a clipped JSON document into that protocol.
            encoded = json.dumps({"schema_version": SCHEMA_VERSION, "state": "unknown",
                                  "truncated": True,
                                  "reason": "agent diagnostics exceed doctor's bounded transport; run agent_diagnostics.py --json directly",
                                  "estimated_bytes": len(encoded.encode("utf-8"))},
                                 sort_keys=True, separators=(",", ":"))
        print(encoded)
    else:
        # One line is intentional: bin/doctor's bounded supervisor retains a
        # final output line, so this remains readable when doctor calls us.
        facts = []
        for name, agent in value["agents"].items():
            facts.append(f"{name}[agent={agent['agent_present']['state']},"
                         f"installed={agent['installation']['state']},"
                         f"discovery={agent['discovery']['state']},"
                         f"workflow={agent['workflow']['state']}]")
        print("; ".join(facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
