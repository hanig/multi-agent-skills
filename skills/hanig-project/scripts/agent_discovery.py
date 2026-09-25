"""Read-only, versioned user-skill discovery for supported coding agents.

The public entry points are :func:`discover`, :func:`resolve_roots`,
:func:`destination_consumers`, and :func:`select_targets`.  They intentionally
do not create directories, read credentials, or start an agent session.
"""

from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


SCHEMA_VERSION = 2
# A probe deadline is derived from an observed wall time, rather than being a
# single guess shared by unlike programs.  The multiplier is headroom for host
# variance; it is not used to decide whether a timed-out executable exists.
PROBE_DEADLINE_HEADROOM = 2.0
PROBE_DEADLINE_FLOOR_SECONDS = 1.0
PROBE_REAP_SECONDS = 0.5
PROBE_OUTPUT_BYTES = 240
VERIFICATION_MAX_AGE_DAYS = 30
STATES = ("executable_found", "slow", "probe_failed", "configured", "absent")
VERIFICATION = ("verified", "unverified")

# These are exact release gates. Root-policy evidence is tracked separately:
# some upstreams do not publish a source snapshot for the matching package.
# A newer release is deliberately reported as unverified until this table is
# updated; treating a new layout as compatible would make an installer lie.
ADAPTERS: dict[str, dict[str, Any]] = {
    "claude": {
        "identity": "Claude Code",
        "executable": "claude",
        "certifications": [
            {"version": "2.1.261", "verified_on": "2026-09-05",
             "checks": ["package_manifest"], "evidence": "2026-09-05 package manifest"},
            {"version": "2.1.282", "verified_on": "2026-09-25",
             "checks": ["native_discovery", "authenticated_skill_invocation",
                        "cross_agent_handoff"],
             "evidence": "ARC-281 live run", "skill_root": "~/.claude/skills"},
        ],
        "invocation": ["claude", "--version"],
        "sources": [
            "https://registry.npmjs.org/@anthropic-ai/claude-code/2.1.261",
            "https://code.claude.com/docs/en/env-vars",
            "https://code.claude.com/docs/en/claude-directory",
        ],
        "probe_timing": {"measured_on": "2026-09-22", "observed_max_seconds": 0.123},
        "source_verification": {"release": "package_manifest", "root_policy": "unverified",
                                "native_discovery": "unverified", "invocation": "unverified"},
        "roots": [
            {"id": "claude-user", "kind": "native", "base": "claude", "suffix": "skills",
             "override": "CLAUDE_CONFIG_DIR", "preferred": True},
        ],
        "duplicates": {"same_name": "unverified", "symlink_identity": "unverified",
                       "consumed_by": ["claude", "opencode"]},
    },
    "codex": {
        "identity": "Codex CLI",
        "executable": "codex",
        "certifications": [
            {"version": "0.153.4", "verified_on": "2026-09-05",
             "checks": ["package_manifest"], "evidence": "2026-09-05 package manifest"},
            {"version": "0.154.0", "verified_on": "2026-09-25",
             "checks": ["native_discovery", "authenticated_skill_invocation",
                        "cross_agent_handoff"],
             "evidence": "ARC-281 live run", "skill_root": "~/.agents/skills"},
        ],
        "invocation": ["codex", "--version"],
        "sources": [
            "https://registry.npmjs.org/@openai/codex/0.153.4",
            "https://github.com/openai/codex/blob/main/codex-rs/core-skills/src/loader.rs",
            "https://github.com/openai/skills/blob/main/skills/.system/skill-installer/SKILL.md",
        ],
        "probe_timing": {"measured_on": "2026-09-22", "observed_max_seconds": 0.018},
        "source_verification": {"release": "package_manifest", "root_policy": "unverified",
                                "native_discovery": "unverified", "invocation": "unverified"},
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
        "certifications": [
            {"version": "1.18.29", "verified_on": "2026-09-05",
             "checks": ["package_manifest"], "evidence": "2026-09-05 package manifest"},
            {"version": "1.18.29", "verified_on": "2026-09-25",
             "checks": ["native_discovery", "authenticated_skill_invocation",
                        "cross_agent_handoff"],
             "evidence": "ARC-281 live run", "skill_root": "~/.agents/skills"},
        ],
        "invocation": ["opencode", "--version"],
        "sources": [
            "https://registry.npmjs.org/opencode-ai/1.18.29",
            "https://opencode.ai/docs/skills",
            "https://dev.opencode.ai/docs/config",
            "https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/skill/index.ts",
        ],
        "probe_timing": {"measured_on": "2026-09-22", "observed_max_seconds": 2.460},
        "source_verification": {"release": "package_manifest", "root_policy": "source_verified",
                                "native_discovery": "unverified", "invocation": "unverified"},
        "roots": [
            {"id": "opencode-user", "kind": "native", "base": "opencode", "suffix": "skills",
             "override": None, "preferred": True},
            {"id": "opencode-home", "kind": "compatibility", "base": "opencode-home", "suffix": "skills",
             "override": None, "preferred": False},
            # OpenCode's loader uses its own global.home/.claude; it does not
            # inherit CLAUDE_CONFIG_DIR from a separately launched Claude CLI.
            {"id": "opencode-claude-compatible", "kind": "claude-compatible", "base": "claude-home", "suffix": "skills",
             "override": None, "preferred": False},
            {"id": "agents-user", "kind": "shared", "base": "agents", "suffix": "skills",
             "override": None, "preferred": False},
        ],
        "duplicates": {"same_name": "last_registered_wins", "symlink_identity": "unverified",
                       "consumed_by": ["opencode"]},
    },
    "pi": {
        "identity": "Pi coding agent",
        "executable": "pi",
        "certifications": [
            {"version": "0.73.1", "verified_on": "2026-09-05",
             "checks": ["package_manifest"], "evidence": "2026-09-05 package manifest"},
            {"version": "0.86.1", "verified_on": "2026-09-25",
             "checks": ["native_discovery", "authenticated_skill_invocation",
                        "cross_agent_handoff"],
             "evidence": "ARC-281 live run", "skill_root": "~/.agents/skills"},
        ],
        "invocation": ["pi", "--version"],
        "sources": [
            "https://registry.npmjs.org/@mariozechner/pi-coding-agent/0.73.1",
            "https://registry.npmjs.org/@earendil-works/pi-coding-agent",
            "https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/docs/skills.md",
            "https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/src/config.ts",
        ],
        "probe_timing": {"measured_on": "2026-09-22", "observed_max_seconds": 0.615},
        "source_verification": {"release": "package_manifest", "root_policy": "source_verified",
                                "native_discovery": "unverified", "invocation": "unverified"},
        "roots": [
            {"id": "pi-user", "kind": "native", "base": "pi", "suffix": "skills",
             "override": "PI_CODING_AGENT_DIR", "preferred": True},
            {"id": "agents-user", "kind": "shared", "base": "agents", "suffix": "skills",
             "override": None, "preferred": False},
        ],
        "duplicates": {"same_name": "later_source_wins", "symlink_identity": "canonical_path_deduplicated",
                       "consumed_by": ["pi"]},
    },
}

# Compatibility summaries for callers; decisions below use the exact version's
# dated record, so refreshing one release never renews another release's age.
for _adapter in ADAPTERS.values():
    _adapter["verified_versions"] = list(dict.fromkeys(
        record["version"] for record in _adapter["certifications"]))
    _adapter["verified_on"] = max(
        record["verified_on"] for record in _adapter["certifications"])

# Consumer edges include compatibility stores, not just an adapter's preferred
# destination.  They let an installer show that two requested copies will be
# visible to the same loader before it writes either one.
CONSUMER_EDGES = {
    "claude-user": ("claude",),
    "opencode-claude-compatible": ("opencode",),
    "agents-user": ("codex", "opencode", "pi"),
    "codex-legacy": ("codex",),
    "opencode-user": ("opencode",),
    "opencode-home": ("opencode",),
    "opencode-config-dir": ("opencode",),
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


def probe_deadline(spec: Mapping[str, Any]) -> float:
    """Deadline derived from the adapter's measured ``--version`` wall time."""
    observed = float(spec["probe_timing"]["observed_max_seconds"])
    return max(PROBE_DEADLINE_FLOOR_SECONDS,
               observed * PROBE_DEADLINE_HEADROOM)


def certification_for(spec: Mapping[str, Any], version: Optional[str]) -> Optional[dict[str, Any]]:
    """Newest retained evidence for this exact version, never a version range."""
    records = [record for record in spec["certifications"]
               if record["version"] == version]
    return max(records, key=lambda record: record["verified_on"]) if records else None


def verification_review_due(spec: Mapping[str, Any]) -> date:
    """One record's deadline, or an adapter's earliest exact-version deadline.

    A newer observation supersedes older evidence only for the same version.
    An adapter still needs review when any distinct version's evidence expires.
    """
    if "certifications" in spec:
        versions = {record["version"] for record in spec["certifications"]}
        return min(verification_review_due(certification_for(spec, version))
                   for version in versions)
    verified = datetime.strptime(spec["verified_on"], "%Y-%m-%d").date()
    return verified + timedelta(days=VERIFICATION_MAX_AGE_DAYS)


def stale_adapter_certifications(as_of: Optional[date] = None) -> list[str]:
    """Adapter IDs with an expired exact version's newest retained evidence."""
    observed = as_of or date.today()
    return [name for name, spec in ADAPTERS.items()
            if observed > verification_review_due(spec)]


def _environment(env: Optional[Mapping[str, str]]) -> dict[str, str]:
    result = dict(os.environ if env is None else env)
    result.setdefault("HOME", str(Path.home()))
    return result


def _absolute(value: str, env: Mapping[str, str]) -> str:
    # ``os.path.expandvars`` observes this Python process's environment, which
    # would make fixture and caller-provided environments lie.  Expand only
    # variables supplied to this discovery call instead.
    value = re.sub(r"\$(?:\{([^}]+)\}|([A-Za-z_][A-Za-z0-9_]*))",
                   lambda match: env.get(match.group(1) or match.group(2), match.group(0)), value)
    if value.startswith("~/"):
        value = os.path.join(env["HOME"], value[2:])
    if not os.path.isabs(value):
        value = os.path.join(env["HOME"], value)
    return os.path.normpath(value)


def _base(agent: str, env: Mapping[str, str]) -> str:
    home = env["HOME"]
    if agent == "claude":
        return _absolute(env.get("CLAUDE_CONFIG_DIR", os.path.join(home, ".claude")), env)
    if agent == "claude-home":
        return _absolute(os.path.join(home, ".claude"), env)
    if agent == "codex":
        return _absolute(env.get("CODEX_HOME", os.path.join(home, ".codex")), env)
    if agent == "opencode":
        return _absolute(os.path.join(env.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "opencode"), env)
    if agent == "opencode-home":
        return _absolute(os.path.join(home, ".opencode"), env)
    if agent == "pi":
        return _absolute(env.get("PI_CODING_AGENT_DIR", os.path.join(home, ".pi", "agent")), env)
    if agent == "agents":
        return _absolute(os.path.join(home, ".agents"), env)
    raise KeyError(agent)


def _root_path(root: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return os.path.join(_base(root["base"], env), root["suffix"])


def _config_bases(agent: str, env: Mapping[str, str]) -> list[str]:
    """Configuration directories which are evidence, never proof of a CLI."""
    bases = [_base(agent, env)]
    if agent == "opencode":
        bases.append(_base("opencode-home", env))
        if env.get("OPENCODE_CONFIG_DIR"):
            bases.append(_absolute(env["OPENCODE_CONFIG_DIR"], env))
    return list(dict.fromkeys(bases))


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
    # OpenCode's ConfigPaths keeps its normal global directory and appends
    # OPENCODE_CONFIG_DIR.  It is additive, not a replacement of XDG config.
    if agent == "opencode" and context.get("OPENCODE_CONFIG_DIR"):
        logical = os.path.join(_absolute(context["OPENCODE_CONFIG_DIR"], context), "skills")
        result.append({"id": "opencode-config-dir", "kind": "custom", "preferred": False,
                       "logical_path": logical, "physical_path": os.path.realpath(logical),
                       "exists": os.path.isdir(logical), "override": "OPENCODE_CONFIG_DIR"})
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


def _which(executable: str, env: Mapping[str, str]) -> Optional[str]:
    """A ``which`` whose result belongs to the supplied, not host, environment."""
    if os.path.dirname(executable):
        return executable if os.path.isfile(executable) and os.access(executable, os.X_OK) else None
    for directory in env.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory or os.curdir, executable)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class _OutputTail:
    """Thread-safe fixed-size output retention; discarded bytes never hit disk."""

    def __init__(self, limit: int = PROBE_OUTPUT_BYTES) -> None:
        self._limit, self._bytes, self._lock = limit, bytearray(), threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self._lock:
            self._bytes.extend(chunk)
            del self._bytes[:-self._limit]

    def text(self) -> str:
        with self._lock:
            return bytes(self._bytes).decode("utf-8", "replace").strip()


def _drain(stream: Any, tail: _OutputTail) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            tail.add(chunk)
    except (OSError, ValueError):
        # The parent closes descriptors after the fixed reap deadline.
        return


def _kill_group(proc: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


_PROBE_SUPERVISOR = """import os, subprocess, sys, time
fd = int(sys.argv[1])
try:
    child = subprocess.Popen(sys.argv[2:])
    result = str(child.wait()).encode("ascii")
except OSError as error:
    result = ("OSError:" + str(error)).encode("utf-8", "replace")
os.write(fd, result[:128])
os.close(fd)
os.close(1)
os.close(2)
while True:
    time.sleep(3600)
"""


def _default_probe(path: str, timeout: float,
                   env: Mapping[str, str]) -> tuple[str, str, float]:
    """Run ``--version`` with a measured deadline and classified outcome."""
    started = time.monotonic()
    timeout = max(0.1, float(timeout))
    deadline = time.monotonic() + timeout
    proc: Optional[subprocess.Popen[Any]] = None
    streams: list[Any] = []
    readers: list[threading.Thread] = []
    control_read: Optional[int] = None
    control_write: Optional[int] = None
    stdout_tail, stderr_tail = _OutputTail(), _OutputTail()
    try:
        control_read, control_write = os.pipe()
        proc = subprocess.Popen([sys.executable, "-c", _PROBE_SUPERVISOR, str(control_write), path, "--version"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=dict(env), start_new_session=True, pass_fds=(control_write,))
        os.close(control_write)
        control_write = None
        assert proc.stdout is not None and proc.stderr is not None
        streams = [proc.stdout, proc.stderr]
        for stream, tail in ((proc.stdout, stdout_tail), (proc.stderr, stderr_tail)):
            reader = threading.Thread(target=_drain, args=(stream, tail), daemon=True)
            reader.start()
            readers.append(reader)
        ready, _, _ = select.select([control_read], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            _kill_group(proc)
            try:
                proc.wait(timeout=PROBE_REAP_SECONDS)
            except subprocess.TimeoutExpired:
                return "slow", "timeout; process group did not exit", time.monotonic() - started
            return "slow", "timeout", time.monotonic() - started
        status = os.read(control_read, 128).decode("utf-8", "replace")
        if not status or status.startswith("OSError:"):
            _kill_group(proc)
            return "failed", status or "no child status", time.monotonic() - started
        try:
            code = int(status)
        except ValueError:
            _kill_group(proc)
            return "failed", "invalid child status", time.monotonic() - started
        # Normal pipes reach EOF when the direct child exits. Give their
        # readers only the fixed reap window; if a descendant inherited a
        # writer, it is killed rather than being allowed to hold the probe.
        drain_deadline = min(deadline, time.monotonic() + PROBE_REAP_SECONDS)
        for reader in readers:
            reader.join(max(0.0, drain_deadline - time.monotonic()))
        if any(reader.is_alive() for reader in readers):
            _kill_group(proc)
            reap_deadline = time.monotonic() + PROBE_REAP_SECONDS
            for reader in readers:
                reader.join(max(0.0, reap_deadline - time.monotonic()))
        output = stdout_tail.text() or stderr_tail.text()
        if code:
            return "failed", "exit %d: %s" % (code, output), time.monotonic() - started
        return "ok", output, time.monotonic() - started
    except OSError as exc:
        return "failed", type(exc).__name__, time.monotonic() - started
    finally:
        if proc is not None:
            _kill_group(proc)
            try:
                proc.wait(timeout=PROBE_REAP_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        if control_read is not None:
            try:
                os.close(control_read)
            except OSError:
                pass
        if control_write is not None:
            try:
                os.close(control_write)
            except OSError:
                pass
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass
        reap_deadline = time.monotonic() + PROBE_REAP_SECONDS
        for reader in readers:
            reader.join(max(0.0, reap_deadline - time.monotonic()))


def _version(output: str) -> Optional[str]:
    found = _VERSION.search(output)
    return found.group(1) if found else None


def discover(
    env: Optional[Mapping[str, str]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    probe: Optional[Callable[[str, float], tuple[Any, ...]]] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Return a read-only discovery report using bounded ``--version`` probes.

    ``which`` and ``probe`` are dependency-injection points for fixture tests;
    neither needs an agent account, a config write, or a model invocation.
    """
    context = _environment(env)
    finder = which or (lambda executable: _which(executable, context))
    runner = probe or (lambda path, seconds: _default_probe(path, seconds, context))
    observed = date.today()
    found_agents: dict[str, Any] = {}
    for key, spec in ADAPTERS.items():
        roots = resolve_roots(key, context)
        config_bases = _config_bases(key, context)
        executable = finder(spec["executable"])
        evidence: dict[str, Any] = {"config_directories": [
            {"path": path, "exists": os.path.isdir(path)} for path in config_bases]}
        if executable:
            seconds = float(timeout) if timeout is not None else probe_deadline(spec)
            result = runner(executable, seconds)
            if len(result) == 2:
                ok, output = result
                # The legacy bool/string injection contract cannot distinguish
                # why False was returned. Never infer authority from wording:
                # only the explicit three-value contract may report SLOW.
                outcome = "ok" if ok else "failed"
                elapsed = None
            else:
                outcome, output, elapsed = result
            if outcome not in ("ok", "slow", "failed"):
                raise ValueError("probe outcome must be ok, slow, or failed")
            evidence["executable"] = {
                "path": executable, "probe": "--version",
                "outcome": outcome.upper(), "ok": outcome == "ok",
                "output": output, "elapsed_seconds": elapsed,
                "deadline_seconds": seconds,
                "deadline_basis": ("caller override" if timeout is not None else
                                   "measured %s max %.3fs x %.1f headroom" % (
                                       spec["probe_timing"]["measured_on"],
                                       spec["probe_timing"]["observed_max_seconds"],
                                       PROBE_DEADLINE_HEADROOM)),
            }
            state = {"ok": "executable_found", "slow": "slow",
                     "failed": "probe_failed"}[outcome]
            version = _version(str(output)) if outcome == "ok" else None
        else:
            state, version = ("configured", None) if any(item["exists"] for item in evidence["config_directories"]) else ("absent", None)
        certification = certification_for(spec, version)
        review_due = verification_review_due(certification) if certification else None
        freshness = ("unverified" if review_due is None else
                     "stale" if observed > review_due else "current")
        verified = freshness == "current"
        evidence["certification"] = certification
        source_verification = dict(spec["source_verification"])
        if certification and "authenticated_skill_invocation" in certification["checks"]:
            if not any(record["version"] == version and "package_manifest" in record["checks"]
                       for record in spec["certifications"]):
                source_verification.update(release="live_version_probe", root_policy="unverified")
            source_verification.update(native_discovery="live_verified",
                                       invocation="live_verified",
                                       cross_agent_handoff="live_verified")
        found_agents[key] = {
            "identity": spec["identity"], "state": state,
            "verification": "verified" if verified else "unverified",
            "version": version, "verified_versions": spec["verified_versions"],
            "roots": roots, "evidence": evidence, "sources": spec["sources"],
            "verified_on": certification["verified_on"] if certification else None,
            "source_verification": source_verification,
            "verification_review_due": review_due.isoformat() if review_due else None,
            "verification_freshness": freshness,
            "duplicate_behavior": spec["duplicates"],
            # Presence chooses a destination; verification says whether that
            # adapter version is certified. Conflating them made four present
            # agents look exactly like no agents at all.
            "eligible_for_automatic_target": state in ("executable_found", "slow"),
            "explicit_path_route": "select_target(report, agent_id) accepts this agent without a binary",
        }
    return {"schema_version": SCHEMA_VERSION, "agents": found_agents,
            "destinations": destination_consumers(context)}


def select_targets(
    report: Mapping[str, Any], agents: Sequence[str] = (), exclude_agents: Sequence[str] = (),
    as_of: Optional[date] = None,
) -> dict[str, Any]:
    """Plan targets for all detected agents or an explicit offline/bootstrap set.

    An empty ``agents`` sequence means automatic mode: every reported executable
    is selected, while its independent verification field remains visible. A non-empty
    sequence is explicit mode and therefore permits absent/configured agents.
    Direct destinations are de-duplicated by physical path.  Destination
    planning uses the adapter declaration order, not flag order, so the same
    semantic agent set always gets the same topology.  ``selected`` and
    ``skipped`` retain caller order for display.  If a planned root already
    serves another requested consumer, that consumer is marked ``covered_by``
    instead of receiving an unnecessary second copy.
    """
    records = report["agents"]
    observed = as_of or date.today()
    stale_certifications = {
        name for name, record in records.items()
        if record.get("verification_review_due")
        and observed > datetime.strptime(record["verification_review_due"], "%Y-%m-%d").date()
    }
    requested, excluded = list(agents) or list(ADAPTERS), set(exclude_agents)
    if len(set(requested)) != len(requested):
        raise ValueError("agents contains a duplicate agent id")
    unknown = (set(requested) | excluded) - set(ADAPTERS)
    if unknown:
        raise KeyError(sorted(unknown)[0])
    mode = "explicit" if agents else "automatic"
    skipped: list[dict[str, str]] = []
    eligible: set[str] = set()
    for agent in requested:
        record = records[agent]
        certification = ("verified" if record["verification"] == "verified"
                         and agent not in stale_certifications else "unverified")
        if agent in excluded:
            skipped.append({"agent": agent, "reason": "excluded",
                            "certification": certification})
        elif not agents and not record["eligible_for_automatic_target"]:
            skipped.append({"agent": agent, "reason": record["state"],
                            "certification": certification})
        else:
            eligible.add(agent)

    direct: dict[str, dict[str, Any]] = {}
    assignments: dict[str, tuple[dict[str, Any], bool]] = {}
    destination_index = {item["physical_path"]: item for item in report["destinations"]}
    for agent in ADAPTERS:
        if agent not in eligible:
            continue
        record = records[agent]
        coverage = next((item for item in direct.values() if agent in item["consumers"]), None)
        if coverage:
            coverage["selected_agents"].append(agent)
            assignments[agent] = (coverage, True)
            continue
        root = next(root for root in record["roots"] if root["preferred"])
        consumers = destination_index[root["physical_path"]]["consumers"]
        item = direct.get(root["physical_path"])
        if item is None:
            item = {"physical_path": root["physical_path"], "destination": root,
                    # consumers is loader exposure; selected_agents is this
                    # plan's lifecycle registration. Never conflate them.
                    "consumers": consumers, "target_agents": [], "selected_agents": []}
            direct[root["physical_path"]] = item
        item["target_agents"].append(agent)
        item["selected_agents"].append(agent)
        assignments[agent] = (item, False)

    selected = []
    warnings = []
    for agent in requested:
        if agent not in eligible:
            continue
        item, covered = assignments[agent]
        record = records[agent]
        agent_warnings = []
        if record["verification"] != "verified":
            if mode == "automatic":
                version = record.get("version") or "unknown version"
                agent_warnings.append(
                    f"{agent} {version} was selected from executable presence, but "
                    "this version is not adapter-certified; selection is not a "
                    "native-compatibility or invocation pass")
            else:
                agent_warnings.append(
                    f"{agent} was selected explicitly with discovery state "
                    f"{record['state']}; bootstrap destination planning proceeded, "
                    "but presence, native compatibility, and invocation remain "
                    "unverified (skip is not pass)")
        if agent in stale_certifications:
            selection_basis = ("selection continued from executable presence"
                               if mode == "automatic"
                               else "explicit bootstrap selection proceeded")
            agent_warnings.append(
                f"{agent} adapter certification expired after "
                f"{record['verification_review_due']}; {selection_basis}, but "
                "the adapter evidence is stale")
        warnings.extend(agent_warnings)
        selected.append({
            "agent": agent, "status": "selected", "mode": mode,
            "covered_by": list(item["target_agents"]) if covered else None,
            "destination": item["destination"], "consumers": item["consumers"],
            "certification": ("verified" if record["verification"] == "verified"
                              and agent not in stale_certifications else "unverified"),
            "certification_record": record["evidence"].get("certification"),
            "certification_warnings": agent_warnings,
        })
    conflicts = []
    for consumer in ADAPTERS:
        visible = [item for item in direct.values() if consumer in item["consumers"]]
        if len(visible) > 1:
            conflicts.append({"consumer": consumer, "physical_paths": [item["physical_path"] for item in visible],
                              "selected_agents": [agent for item in visible for agent in item["selected_agents"]],
                              "reason": "the loader can see multiple requested copies; consult adapter precedence"})
    return {"schema_version": SCHEMA_VERSION, "mode": mode, "selected": selected,
            "skipped": skipped, "destinations": list(direct.values()),
            "competing_visibility": conflicts, "certification_warnings": warnings}


def select_target(report: Mapping[str, Any], agent_id: Optional[str] = None) -> dict[str, Any]:
    """Compatibility helper for callers that need exactly one chosen target."""
    if agent_id is not None:
        plan = select_targets(report, (agent_id,))
        return plan["selected"][0]
    plan = select_targets(report)
    direct = [item for item in plan["selected"] if item["covered_by"] is None]
    if len(direct) == 1:
        return direct[0]
    return {"status": "requires_explicit_target", "eligible_agents": [item["agent"] for item in direct],
            "reason": "no executable found" if not direct else "multiple executables found"}
