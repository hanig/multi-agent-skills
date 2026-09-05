"""Read-only, versioned user-skill discovery for supported coding agents.

The public entry points are :func:`discover`, :func:`resolve_roots`,
:func:`destination_consumers`, and :func:`select_target`.  They intentionally
do not create directories, read credentials, or start an agent session.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


SCHEMA_VERSION = 1
PROBE_TIMEOUT_SECONDS = 2.0
STATES = ("executable_found", "configured", "absent", "undetermined")
VERIFICATION = ("verified", "unverified")

# These are exact releases whose directory behaviour was checked on 2026-09-05.
# A newer release is deliberately reported as unverified until this table is
# updated; treating a new layout as compatible would make an installer lie.
ADAPTERS: dict[str, dict[str, Any]] = {
    "claude": {
        "identity": "Claude Code",
        "executable": "claude",
        "verified_versions": ["2.1.261"],
        "invocation": ["claude", "--version"],
        "sources": [
            "https://code.claude.com/docs/en/env-vars",
            "https://code.claude.com/docs/en/claude-directory",
        ],
        "verified_on": "2026-09-05",
        "roots": [
            {"id": "claude-user", "kind": "native", "base": "claude", "suffix": "skills",
             "override": "CLAUDE_CONFIG_DIR", "preferred": True},
        ],
        "duplicates": {"same_name": "unverified", "symlink_identity": "unverified",
                       "consumed_by": ["claude", "opencode", "pi"]},
    },
    "codex": {
        "identity": "Codex CLI",
        "executable": "codex",
        "verified_versions": ["0.153.4"],
        "invocation": ["codex", "--version"],
        "sources": [
            "https://github.com/openai/codex/blob/main/codex-rs/core-skills/src/loader.rs",
            "https://github.com/openai/skills/blob/main/skills/.system/skill-installer/SKILL.md",
        ],
        "verified_on": "2026-09-05",
        "roots": [
            {"id": "agents-user", "kind": "shared", "base": "agents", "suffix": "skills",
             "override": None, "preferred": True},
            {"id": "codex-legacy", "kind": "legacy", "base": "codex", "suffix": "skills",
             "override": "CODEX_HOME", "preferred": False},
        ],
        "duplicates": {"same_name": "unverified", "symlink_identity": "unverified",
                       "consumed_by": ["codex", "opencode", "pi"]},
    },
    "opencode": {
        "identity": "OpenCode",
        "executable": "opencode",
        "verified_versions": ["1.18.29"],
        "invocation": ["opencode", "--version"],
        "sources": [
            "https://opencode.ai/docs/skills",
            "https://dev.opencode.ai/docs/config",
            "https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/skill/index.ts",
        ],
        "verified_on": "2026-09-05",
        "roots": [
            {"id": "opencode-user", "kind": "native", "base": "opencode", "suffix": "skills",
             "override": "OPENCODE_CONFIG_DIR", "preferred": True},
        ],
        "duplicates": {"same_name": "last_registered_wins", "symlink_identity": "unverified",
                       "consumed_by": ["opencode"]},
    },
    "pi": {
        "identity": "Pi coding agent",
        "executable": "pi",
        "verified_versions": ["0.73.1"],
        "invocation": ["pi", "--version"],
        "sources": [
            "https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/skills.md",
            "https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/config.ts",
        ],
        "verified_on": "2026-09-05",
        "roots": [
            {"id": "pi-user", "kind": "native", "base": "pi", "suffix": "skills",
             "override": "PI_CODING_AGENT_DIR", "preferred": True},
        ],
        "duplicates": {"same_name": "later_source_wins", "symlink_identity": "unverified",
                       "consumed_by": ["pi"]},
    },
}

# Consumer edges include compatibility stores, not just an adapter's preferred
# destination.  They let an installer show that two requested copies will be
# visible to the same loader before it writes either one.
CONSUMER_EDGES = {
    "claude-user": ("claude", "opencode", "pi"),
    "agents-user": ("codex", "opencode", "pi"),
    "codex-legacy": ("codex", "pi"),
    "opencode-user": ("opencode",),
    "pi-user": ("pi",),
}

DISCOVERY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "hanig agent discovery report",
    "type": "object",
    "required": ["schema_version", "agents", "destinations"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "agents": {"type": "object", "additionalProperties": {
            "type": "object", "required": ["state", "verification", "roots", "evidence"],
            "properties": {
                "state": {"enum": list(STATES)},
                "verification": {"enum": list(VERIFICATION)},
                "roots": {"type": "array"}, "evidence": {"type": "object"},
            },
        }},
        "destinations": {"type": "array"},
    },
}

_VERSION = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")


def schema() -> dict[str, Any]:
    """Return the JSON Schema for :func:`discover` reports."""
    return DISCOVERY_SCHEMA


def adapters() -> dict[str, dict[str, Any]]:
    """Return static, serializable adapter records and their evidence links."""
    return ADAPTERS


def _environment(env: Optional[Mapping[str, str]]) -> dict[str, str]:
    result = dict(os.environ if env is None else env)
    result.setdefault("HOME", str(Path.home()))
    return result


def _absolute(value: str, env: Mapping[str, str]) -> str:
    value = os.path.expandvars(value)
    if value.startswith("~/"):
        value = os.path.join(env["HOME"], value[2:])
    if not os.path.isabs(value):
        value = os.path.join(env["HOME"], value)
    return os.path.normpath(value)


def _base(agent: str, env: Mapping[str, str]) -> str:
    home = env["HOME"]
    if agent == "claude":
        return _absolute(env.get("CLAUDE_CONFIG_DIR", os.path.join(home, ".claude")), env)
    if agent == "codex":
        return _absolute(env.get("CODEX_HOME", os.path.join(home, ".codex")), env)
    if agent == "opencode":
        default = os.path.join(env.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "opencode")
        return _absolute(env.get("OPENCODE_CONFIG_DIR", default), env)
    if agent == "pi":
        return _absolute(env.get("PI_CODING_AGENT_DIR", os.path.join(home, ".pi", "agent")), env)
    if agent == "agents":
        return _absolute(os.path.join(home, ".agents"), env)
    raise KeyError(agent)


def _root_path(root: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return os.path.join(_base(root["base"], env), root["suffix"])


def resolve_roots(agent: str, env: Optional[Mapping[str, str]] = None) -> list[dict[str, Any]]:
    """Resolve a supported agent's user roots without creating them.

    Each result preserves a logical path and also has ``physical_path`` for
    destination de-duplication.  Unknown agent IDs raise ``KeyError``.
    """
    context = _environment(env)
    spec = ADAPTERS[agent]
    result = []
    for root in spec["roots"]:
        logical = _root_path(root, context)
        exists = os.path.isdir(logical)
        result.append({
            "id": root["id"], "kind": root["kind"], "preferred": root["preferred"],
            "logical_path": logical, "physical_path": os.path.realpath(logical), "exists": exists,
            "override": root["override"],
        })
    return result


def destination_consumers(env: Optional[Mapping[str, str]] = None) -> list[dict[str, Any]]:
    """Group normalized destinations and list every agent that consumes each."""
    context = _environment(env)
    grouped: dict[str, dict[str, Any]] = {}
    for agent in ADAPTERS:
        for root in resolve_roots(agent, context):
            item = grouped.setdefault(root["physical_path"], {
                "physical_path": root["physical_path"], "logical_paths": [], "root_ids": [],
                "consumers": [], "exists": root["exists"],
            })
            if root["logical_path"] not in item["logical_paths"]:
                item["logical_paths"].append(root["logical_path"])
            if root["id"] not in item["root_ids"]:
                item["root_ids"].append(root["id"])
            for consumer in CONSUMER_EDGES[root["id"]]:
                if consumer not in item["consumers"]:
                    item["consumers"].append(consumer)
            item["exists"] = item["exists"] or root["exists"]
    return sorted(grouped.values(), key=lambda entry: entry["physical_path"])


def _default_probe(path: str, timeout: float) -> tuple[bool, str]:
    try:
        proc = subprocess.run([path, "--version"], text=True, capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, type(exc).__name__
    if proc.returncode:
        return False, "exit %d: %s" % (proc.returncode, (proc.stderr or proc.stdout).strip()[:240])
    return True, (proc.stdout or proc.stderr).strip()[:240]


def _version(output: str) -> Optional[str]:
    found = _VERSION.search(output)
    return found.group(1) if found else None


def discover(
    env: Optional[Mapping[str, str]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    probe: Optional[Callable[[str, float], tuple[bool, str]]] = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return a read-only discovery report using bounded ``--version`` probes.

    ``which`` and ``probe`` are dependency-injection points for fixture tests;
    neither needs an agent account, a config write, or a model invocation.
    """
    context, finder, runner = _environment(env), (which or shutil.which), (probe or _default_probe)
    found_agents: dict[str, Any] = {}
    for key, spec in ADAPTERS.items():
        roots = resolve_roots(key, context)
        base = _base(key, context)
        executable = finder(spec["executable"])
        evidence: dict[str, Any] = {"config_directory": {"path": base, "exists": os.path.isdir(base)}}
        if executable:
            ok, output = runner(executable, timeout)
            evidence["executable"] = {"path": executable, "probe": "--version", "ok": ok, "output": output}
            if not ok:
                state, version = "undetermined", None
            else:
                state, version = "executable_found", _version(output)
        else:
            state, version = ("configured", None) if evidence["config_directory"]["exists"] else ("absent", None)
        verified = bool(version and version in spec["verified_versions"])
        found_agents[key] = {
            "identity": spec["identity"], "state": state,
            "verification": "verified" if verified else "unverified",
            "version": version, "verified_versions": spec["verified_versions"],
            "roots": roots, "evidence": evidence, "sources": spec["sources"],
            "verified_on": spec["verified_on"], "duplicate_behavior": spec["duplicates"],
            "eligible_for_automatic_target": state == "executable_found" and verified,
            "explicit_path_route": "select_target(report, agent_id) accepts this agent without a binary",
        }
    return {"schema_version": SCHEMA_VERSION, "agents": found_agents,
            "destinations": destination_consumers(context)}


def select_target(report: Mapping[str, Any], agent_id: Optional[str] = None) -> dict[str, Any]:
    """Choose one preferred destination, or return an explicit-selection route.

    Automatic selection only accepts a single, version-verified executable.  An
    explicit agent is allowed for offline/bootstrap installs, including an
    absent binary; consumers should still surface the report's verification.
    """
    agents = report["agents"]
    if agent_id is not None:
        if agent_id not in ADAPTERS:
            raise KeyError(agent_id)
        chosen, mode = agent_id, "explicit"
    else:
        eligible = [name for name, item in agents.items() if item["eligible_for_automatic_target"]]
        if len(eligible) != 1:
            return {"status": "requires_explicit_target", "eligible_agents": eligible,
                    "reason": "no verified executable" if not eligible else "multiple verified executables"}
        chosen, mode = eligible[0], "automatic"
    root = next(root for root in agents[chosen]["roots"] if root["preferred"])
    consumers = next(item["consumers"] for item in report["destinations"]
                     if item["physical_path"] == root["physical_path"])
    return {"status": "selected", "mode": mode, "agent": chosen, "destination": root,
            "consumers": consumers,
            "warning": "shared destination is visible to: " + ", ".join(consumers) if len(consumers) > 1 else None}
