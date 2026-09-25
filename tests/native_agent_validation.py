#!/usr/bin/env python3
"""Credentialless native-loader validation for the cross-agent installer.

This is an explicitly invoked host harness, not a unit test.  It installs one
representative authored skill below a TemporaryDirectory, exercises native
loader/list surfaces where the installed host exposes them, and deletes the
entire temporary tree on exit.  It never reads the normal agent homes and it
never claims that loading a skill or running its helper script is an LLM-driven
skill invocation.

Exit codes:
  0  all four version and native-discovery gates passed (not the LLM gate)
  1  a check that could run produced contradictory or failing evidence
  2  the bounded checks ran, but required evidence was unavailable/incomplete
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
SKILL = "hanig-portable-handoff"
AGENTS = ("claude", "codex", "opencode", "pi")
NON_CLAUDE_AGENTS = ("codex", "opencode", "pi")
NON_CLAUDE_HELPERS = ("dirname", "git", "node", "python3", "sh")
EXPECTED_VERSIONS = {
    "claude": "2.1.282",
    "codex": "0.154.0",
    "opencode": "1.18.29",
    "pi": "0.86.1",
}
VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")
MAX_CAPTURE_CHARS = 1_000_000
PI_PACKAGE_NAMES = (
    "@mariozechner/pi-coding-agent",
    "@earendil-works/pi-coding-agent",
)

def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _terminate_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, PermissionError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float = 30,
    input_text: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            text=True,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except FileNotFoundError as exc:
        return {
            "status": "unavailable",
            "returncode": None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stdout": "",
            "stderr": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            _terminate_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = exc.stdout, exc.stderr
        else:
            stdout, stderr = exc.stdout, exc.stderr
        return {
            "status": "failed",
            "returncode": None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stdout": _text(stdout)[-4000:],
            "stderr": _text(stderr)[-4000:],
            "reason": f"timed out after {timeout:g}s",
        }
    stdout, stderr = _text(stdout), _text(stderr)
    truncated = len(stdout) > MAX_CAPTURE_CHARS or len(stderr) > MAX_CAPTURE_CHARS
    if truncated:
        stdout, stderr = stdout[-MAX_CAPTURE_CHARS:], stderr[-MAX_CAPTURE_CHARS:]
    return {
        "status": "passed" if proc.returncode == 0 and not truncated else "failed",
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout,
        "stderr": stderr,
        "output_truncated": truncated,
        **(
            {"reason": "output exceeded the 1,000,000-character capture limit"}
            if truncated
            else {}
        ),
    }


def _isolated_env(paths: Mapping[str, Path]) -> dict[str, str]:
    """Allowlist the process environment; credentials are intentionally absent."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(paths["home"]),
        "XDG_CONFIG_HOME": str(paths["xdg"]),
        "CODEX_HOME": str(paths["codex"]),
        "CLAUDE_CONFIG_DIR": str(paths["claude"]),
        "PI_CODING_AGENT_DIR": str(paths["pi"]),
        "TMPDIR": str(paths["tmp"]),
        "CLAUDE_CODE_TMPDIR": str(paths["tmp"]),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _version(agent: str, *, cwd: Path, env: Mapping[str, str]) -> dict[str, Any]:
    executable = shutil.which(agent, path=env.get("PATH"))
    if executable is None:
        return {
            "status": "unavailable",
            "path": None,
            "version": None,
            "expected_version": EXPECTED_VERSIONS[agent],
            "version_gate": "missing",
            "reason": f"{agent} is not in PATH",
        }
    result = _run([executable, "--version"], cwd=cwd, env=env, timeout=15)
    output = result["stdout"] or result["stderr"]
    match = VERSION_RE.search(output)
    version = match.group(1) if match else None
    gate = "passed" if version == EXPECTED_VERSIONS[agent] else "failed"
    return {
        "status": result["status"],
        "path": executable,
        "version": version,
        "expected_version": EXPECTED_VERSIONS[agent],
        "version_gate": gate,
        "elapsed_seconds": result["elapsed_seconds"],
        "stdout": result["stdout"].strip(),
        "stderr": result["stderr"].strip(),
    }


def _confined(path: str, scratch: Path) -> bool:
    try:
        Path(path).resolve().relative_to(scratch.resolve())
        return True
    except (OSError, ValueError):
        return False


def _install(
    env: Mapping[str, str], scratch: Path, source_root: Path
) -> dict[str, Any]:
    argv = [
        "sh",
        str(source_root / "install.sh"),
        "--agent",
        "claude",
        "--agent",
        "codex",
        "--agent",
        "opencode",
        "--agent",
        "pi",
        "--only",
        SKILL,
        "--json",
    ]
    result = _run(argv, cwd=source_root, env=env, timeout=60)
    record: dict[str, Any] = {
        "status": result["status"],
        "command": "./install.sh --agent claude --agent codex --agent opencode "
        f"--agent pi --only {SKILL} --json",
        "returncode": result["returncode"],
        "stderr": result["stderr"].strip(),
    }
    try:
        document = json.loads(result["stdout"])
    except (TypeError, json.JSONDecodeError) as exc:
        record.update(status="failed", reason=f"installer did not emit JSON: {exc}")
        return record
    record["document"] = document
    roots = sorted({action["root"] for action in document.get("actions", [])})
    record["physical_roots"] = roots
    record["all_roots_confined"] = all(_confined(root, scratch) for root in roots)
    if result["returncode"] != 0 or not record["all_roots_confined"]:
        record["status"] = "failed"
        record["reason"] = "installer failed or emitted a destination outside scratch"
    elif len(roots) != 2:
        record["status"] = "failed"
        record["reason"] = (
            f"expected two de-duplicated physical roots, found {len(roots)}"
        )
    return record


def _filtered_non_claude_env(
    paths: Mapping[str, Path], source_env: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, Any]]:
    """Expose only real non-Claude agents and named system helpers."""
    filtered_bin = paths["bin"]
    filtered_bin.mkdir()
    required = (*NON_CLAUDE_AGENTS, *NON_CLAUDE_HELPERS)
    targets: dict[str, str] = {}
    missing: list[str] = []
    for name in required:
        executable = shutil.which(name, path=source_env.get("PATH"))
        if executable is None:
            missing.append(name)
            continue
        resolved = Path(executable).resolve()
        (filtered_bin / name).symlink_to(resolved)
        targets[name] = str(resolved)

    env = {
        "PATH": str(filtered_bin),
        "HOME": str(paths["home"]),
        "XDG_CONFIG_HOME": str(paths["xdg"]),
        "CODEX_HOME": str(paths["codex"]),
        "PI_CODING_AGENT_DIR": str(paths["pi"]),
        "TMPDIR": str(paths["tmp"]),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    exposed = sorted(path.name for path in filtered_bin.iterdir())
    exact_names = sorted(required)
    checks = {
        "filtered_path_is_one_directory": os.pathsep not in env["PATH"],
        "only_named_executables_exposed": exposed == exact_names,
        "real_executables_are_symlinked": all(
            (filtered_bin / name).is_symlink()
            and str((filtered_bin / name).resolve()) == target
            for name, target in targets.items()
        ),
        "claude_binary_not_resolvable": shutil.which("claude", path=env["PATH"])
        is None,
        "claude_config_variable_absent": "CLAUDE_CONFIG_DIR" not in env,
        "home_claude_tree_absent_before": not (paths["home"] / ".claude").exists(),
    }
    status = (
        "unavailable" if missing else ("passed" if all(checks.values()) else "failed")
    )
    return env, {
        "status": status,
        "path": env["PATH"],
        "exposed_names": exposed,
        "resolved_targets": targets,
        "missing_executables": missing,
        "checks": checks,
        **(
            {
                "minimal_requirement": "real executables in the launch PATH: "
                + ", ".join(missing)
            }
            if missing
            else {}
        ),
    }


def _installed_snapshot(paths: Mapping[str, Path]) -> dict[str, Any]:
    root = paths["home"] / ".agents" / "skills" / SKILL
    selected = (root / "SKILL.md", root / "scripts" / "handoff.py")
    digests = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in selected
        if path.is_file()
    }
    checks = {
        "installed_directory_is_not_symlink": root.is_dir() and not root.is_symlink(),
        "skill_manifest_is_regular_file": selected[0].is_file()
        and not selected[0].is_symlink(),
        "handoff_helper_is_regular_file": selected[1].is_file()
        and not selected[1].is_symlink(),
        "representative_digests_captured": len(digests) == len(selected),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "path": str(root),
        "checks": checks,
        "sha256": digests,
    }


def _install_non_claude(
    env: Mapping[str, str], scratch: Path, source_root: Path
) -> dict[str, Any]:
    argv = ["sh", str(source_root / "install.sh")]
    for agent in NON_CLAUDE_AGENTS:
        argv.extend(("--agent", agent))
    argv.extend(("--only", SKILL, "--json"))
    result = _run(argv, cwd=source_root, env=env, timeout=60)
    record: dict[str, Any] = {
        "status": result["status"],
        "command": "./install.sh --agent codex --agent opencode --agent pi "
        f"--only {SKILL} --json",
        "returncode": result["returncode"],
        "stderr": result["stderr"].strip(),
    }
    try:
        document = json.loads(result["stdout"])
    except (TypeError, json.JSONDecodeError) as exc:
        record.update(status="failed", reason=f"installer did not emit JSON: {exc}")
        return record
    actions = document.get("actions", [])
    roots = sorted({action.get("root", "") for action in actions})
    expected_root = Path(env["HOME"]) / ".agents" / "skills"
    selected_agents = sorted(
        {agent for action in actions for agent in action.get("agents", [])}
    )
    checks = {
        "command_succeeded": result["returncode"] == 0,
        "one_shared_physical_root": roots == [str(expected_root)],
        "root_is_confined": all(_confined(root, scratch) for root in roots),
        "only_non_claude_agents_selected": selected_agents == sorted(NON_CLAUDE_AGENTS),
        "no_claude_action": all(
            "claude" not in action.get("agents", []) for action in actions
        ),
        "no_claude_destination": not any(
            ".claude" in Path(root).parts for root in roots
        ),
        "installed_snapshot_exists": (expected_root / SKILL / "SKILL.md").is_file(),
    }
    record.update(
        document=document,
        physical_roots=roots,
        selected_agents=selected_agents,
        checks=checks,
        status="passed" if all(checks.values()) else "failed",
    )
    if record["status"] == "failed":
        record["reason"] = (
            "non-Claude installer plan contradicted its isolation contract"
        )
    return record


def _usage_is_zero(payload: Mapping[str, Any]) -> bool:
    usage = payload.get("usage") or {}
    return (
        payload.get("total_cost_usd") == 0
        and usage.get("input_tokens") == 0
        and usage.get("output_tokens") == 0
        and usage.get("cache_creation_input_tokens") == 0
        and usage.get("cache_read_input_tokens") == 0
    )


def _claude_discovery(
    paths: Mapping[str, Path], env: Mapping[str, str]
) -> dict[str, Any]:
    if shutil.which("claude", path=env.get("PATH")) is None:
        return {"status": "unavailable", "reason": "claude is not in PATH"}
    debug_log = paths["tmp"] / "claude-skills.log"
    # An explicit invalid key takes precedence over OAuth/keychain auth.  /help
    # is handled locally, so the installed skill body is not sent to a model.
    probe_env = dict(env)
    probe_env.update(
        {
            "ANTHROPIC_API_KEY": "invalid-native-validation-key",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
    )
    argv = [
        "claude",
        "--no-session-persistence",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "user",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--debug",
        "skills",
        "--debug-file",
        str(debug_log),
        "--output-format",
        "json",
        "--print",
        "/help",
    ]
    result = _run(argv, cwd=paths["workspace"], env=probe_env, timeout=30)
    log = debug_log.read_text(errors="replace") if debug_log.is_file() else ""
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError:
        payload = {}
    isolated_root = str(paths["claude"] / "skills")
    checks = {
        "isolated_root_named": f"user={isolated_root}" in log,
        "one_user_skill_loaded": bool(
            re.search(r"Loaded 1 unique skills .*user: 1", log)
        ),
        "one_skill_command_returned": "getSkills returning: 1 skill dir commands"
        in log,
        "zero_model_usage": _usage_is_zero(payload),
        "no_session_persistence": "--no-session-persistence" in " ".join(argv),
    }
    return {
        "status": (
            "passed" if result["returncode"] == 0 and all(checks.values()) else "failed"
        ),
        "kind": "native_discovery",
        "command": "ANTHROPIC_API_KEY=<invalid-test-key> CLAUDE_CONFIG_DIR=$SCRATCH/claude "
        "claude --no-session-persistence --setting-sources user --debug skills "
        "--output-format json --print /help",
        "checks": checks,
        "returncode": result["returncode"],
        "total_cost_usd": payload.get("total_cost_usd"),
        "input_tokens": (payload.get("usage") or {}).get("input_tokens"),
        "output_tokens": (payload.get("usage") or {}).get("output_tokens"),
        "evidence": "native debug trace loaded exactly one user skill from the isolated root",
        "invocation": {
            "status": "not_run",
            "reason": "actual skill execution requires an authenticated model turn",
        },
    }


def _read_response(
    proc: subprocess.Popen[str], request_id: int, timeout: float
) -> tuple[dict[str, Any], list[str]]:
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    observed: list[str] = []
    try:
        while time.monotonic() < deadline:
            events = selector.select(max(0.0, min(1.0, deadline - time.monotonic())))
            if not events:
                continue
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed stdout")
            item = json.loads(line)
            if item.get("id") == request_id:
                return item, observed
            if "method" in item:
                observed.append(str(item["method"]))
    finally:
        selector.close()
    raise TimeoutError(f"no codex app-server response for request {request_id}")


def _codex_discovery(
    paths: Mapping[str, Path], env: Mapping[str, str]
) -> dict[str, Any]:
    if shutil.which("codex", path=env.get("PATH")) is None:
        return {"status": "unavailable", "reason": "codex is not in PATH"}
    proc: subprocess.Popen[str] | None = None
    ignored: list[str] = []
    try:
        proc = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            cwd=paths["workspace"],
            env=dict(env),
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            start_new_session=True,
        )
        assert proc.stdin is not None

        def send(message: Mapping[str, Any]) -> None:
            assert proc is not None and proc.stdin is not None
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            proc.stdin.flush()

        send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "native-agent-validation",
                        "title": "Native agent validation",
                        "version": "1",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                    },
                },
            }
        )
        initialized, notices = _read_response(proc, 1, 20)
        ignored.extend(notices)
        send({"method": "initialized"})
        send(
            {
                "method": "skills/list",
                "id": 2,
                "params": {"cwds": [str(paths["workspace"])], "forceReload": True},
            }
        )
        response, notices = _read_response(proc, 2, 20)
        ignored.extend(notices)
        entries = (response.get("result") or {}).get("data") or []
        matches = [
            skill
            for entry in entries
            for skill in entry.get("skills", [])
            if skill.get("name") == SKILL
        ]
        errors = [error for entry in entries for error in entry.get("errors", [])]
        expected = paths["home"] / ".agents" / "skills" / SKILL / "SKILL.md"
        checks = {
            "initialize_succeeded": "result" in initialized,
            "one_representative_skill": len(matches) == 1,
            "enabled_user_skill": bool(
                matches
                and matches[0].get("enabled") is True
                and matches[0].get("scope") == "user"
            ),
            "path_is_installed_copy": bool(
                matches
                and Path(matches[0].get("path", "")).resolve() == expected.resolve()
            ),
            "loader_errors_empty": not errors,
        }
        status = "passed" if all(checks.values()) else "failed"
        return {
            "status": status,
            "kind": "native_discovery",
            "command": "CODEX_HOME=$SCRATCH/codex codex app-server --listen stdio://; "
            "initialize; initialized; skills/list(forceReload=true)",
            "checks": checks,
            "skill": matches[0] if matches else None,
            "loader_errors": errors,
            "ignored_notifications": ignored,
            "evidence": "native app-server skills/list returned the installed user skill",
            "invocation": {
                "status": "not_run",
                "reason": "skills/list is discovery; a model turn is required for invocation",
            },
        }
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return {"status": "failed", "kind": "native_discovery", "reason": str(exc)}
    finally:
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_group(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _terminate_group(proc)
                    proc.wait(timeout=5)


def _opencode_discovery(
    paths: Mapping[str, Path], env: Mapping[str, str]
) -> dict[str, Any]:
    if shutil.which("opencode", path=env.get("PATH")) is None:
        return {"status": "unavailable", "reason": "opencode is not in PATH"}
    probe_env = dict(env)
    probe_env.update(
        {
            "OPENCODE_PURE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        }
    )
    result = _run(
        ["opencode", "debug", "skill", "--pure"],
        cwd=paths["workspace"],
        env=probe_env,
        timeout=30,
    )
    try:
        skills = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {
            "status": "failed",
            "kind": "native_discovery",
            "reason": f"debug skill output was not JSON: {exc}",
            "stderr": result["stderr"].strip(),
        }
    matches = [skill for skill in skills if skill.get("name") == SKILL]
    expected = paths["home"] / ".agents" / "skills" / SKILL / "SKILL.md"
    checks = {
        "command_succeeded": result["returncode"] == 0,
        "one_representative_skill": len(matches) == 1,
        "path_is_installed_copy": bool(
            matches
            and Path(matches[0].get("location", "")).resolve() == expected.resolve()
        ),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "kind": "native_discovery",
        "command": "HOME=$SCRATCH/home XDG_CONFIG_HOME=$SCRATCH/xdg "
        "opencode debug skill --pure",
        "checks": checks,
        "skill": (
            {"name": matches[0]["name"], "location": matches[0]["location"]}
            if matches
            else None
        ),
        "stderr": result["stderr"].strip(),
        "evidence": "native debug skill command returned the installed shared skill",
        "invocation": {
            "status": "not_run",
            "reason": "debug skill is discovery; a provider/model turn is required for invocation",
        },
    }


def _readable_regular_file(path: Path) -> bool:
    """Return whether the harness can actually read a regular-file entry."""
    try:
        if not path.is_file():
            return False
        with path.open("rb"):
            return True
    except OSError:
        return False


def _same_resolved_path(value: Any, expected: Path) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return Path(value).resolve() == expected.resolve()
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return False


def _pi_entry_path(package_root: Path, manifest: Mapping[str, Any]) -> Path | None:
    main = manifest.get("main", "./dist/index.js")
    if not isinstance(main, str) or not main:
        return None
    try:
        return package_root / main
    except (TypeError, ValueError, UnicodeError):
        return None


def _pi_package_roots(
    executable: str, *, cwd: Path, env: Mapping[str, str]
) -> list[Path]:
    candidates: list[Path] = []
    resolved = Path(executable).resolve()
    candidates.extend(resolved.parents)
    global_roots: list[Path] = []
    for command in (("npm", "root", "-g"), ("pnpm", "root", "-g")):
        if shutil.which(command[0], path=env.get("PATH")) is None:
            continue
        result = _run(command, cwd=cwd, env=env, timeout=15)
        if result["returncode"] == 0 and result["stdout"].strip():
            global_roots.append(Path(result["stdout"].strip()))
    # Preserve the legacy lookup order across every global package root.  The
    # renamed identity is a fallback, so a partial renamed install in an
    # earlier registry cannot shadow a working legacy install in a later one.
    for package_name in PI_PACKAGE_NAMES:
        for package_root in global_roots:
            scope, name = package_name.split("/", 1)
            candidates.append(package_root / scope / name)
    matching: list[tuple[Path, dict[str, Any], bool]] = []
    for candidate in candidates:
        manifest = candidate / "package.json"
        try:
            if not manifest.is_file():
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("name") in PI_PACKAGE_NAMES:
            try:
                resolved.relative_to(candidate.resolve())
                owns_executable = True
            except (OSError, RuntimeError, ValueError):
                owns_executable = False
            matching.append((candidate, data, owns_executable))
    # A supported package containing the resolved PATH executable owns that
    # executable.  Prefer that ownership fact before applying scope preference
    # to unrelated global candidates.  Complete SDKs still precede incomplete
    # ones so an unusable owner cannot shadow a usable fallback.
    complete_owned: list[Path] = []
    complete_unrelated: list[Path] = []
    incomplete_owned: list[Path] = []
    incomplete_unrelated: list[Path] = []
    for candidate, data, owns_executable in matching:
        entry = _pi_entry_path(candidate, data)
        target = (
            complete_owned
            if owns_executable and entry is not None and _readable_regular_file(entry)
            else incomplete_owned
            if owns_executable
            else None
        )
        if target is not None:
            target.append(candidate)
    for package_name in PI_PACKAGE_NAMES:
        for candidate, data, owns_executable in matching:
            if owns_executable or data.get("name") != package_name:
                continue
            entry = _pi_entry_path(candidate, data)
            if entry is not None and _readable_regular_file(entry):
                complete_unrelated.append(candidate)
            else:
                incomplete_unrelated.append(candidate)
    # Preserve the more specific "entry point is absent" diagnostic when no
    # candidate loads, without allowing an incomplete candidate to shadow a
    # complete SDK later in the search order.
    return (
        complete_owned
        + complete_unrelated
        + incomplete_owned
        + incomplete_unrelated
    )


def _pi_package_root(
    executable: str, *, cwd: Path, env: Mapping[str, str]
) -> Path | None:
    """Return the first ordered candidate for focused resolution checks."""
    roots = _pi_package_roots(executable, cwd=cwd, env=env)
    return roots[0] if roots else None


def _pi_candidate_discovery(
    package_root: Path,
    *,
    paths: Mapping[str, Path],
    env: Mapping[str, str],
    script: Path,
) -> dict[str, Any]:
    manifest_path = package_root / "package.json"
    try:
        if not manifest_path.is_file():
            raise OSError(f"Pi SDK manifest is not a regular file: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        return {"status": "failed", "kind": "native_discovery", "reason": str(exc)}
    if not isinstance(manifest, dict):
        return {
            "status": "failed",
            "kind": "native_discovery",
            "reason": "Pi SDK package.json is not a JSON object",
        }
    entry = _pi_entry_path(package_root, manifest)
    if entry is None or not _readable_regular_file(entry):
        return {
            "status": "unavailable",
            "kind": "native_discovery",
            "reason": f"Pi SDK entry point is absent or unreadable: {entry}",
            "minimal_requirement": (
                "a readable normal Node package build, not a CLI-only compiled binary"
            ),
            "invocation": {
                "status": "not_run",
                "reason": "actual invocation requires a configured model",
            },
        }
    result = _run(
        ["node", str(script), str(entry), str(paths["workspace"]), str(paths["pi"])],
        cwd=paths["workspace"],
        env=env,
        timeout=30,
    )
    try:
        payload = json.loads(result["stdout"])
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        return {
            "status": "failed",
            "kind": "native_discovery",
            "reason": f"Pi SDK loader output was not JSON: {exc}",
            "stderr": result["stderr"].strip(),
        }
    if not isinstance(payload, dict):
        return {
            "status": "failed",
            "kind": "native_discovery",
            "reason": "Pi SDK loader output was not a JSON object",
            "stderr": result["stderr"].strip(),
        }
    skills = payload.get("skills", [])
    if not isinstance(skills, list):
        skills = []
    matches = [
        skill
        for skill in skills
        if isinstance(skill, dict) and skill.get("name") == SKILL
    ]
    expected = paths["home"] / ".agents" / "skills" / SKILL / "SKILL.md"
    matched_path = matches[0].get("filePath") if matches else None
    diagnostics = payload.get("diagnostics")
    checks = {
        "command_succeeded": result["returncode"] == 0,
        "one_representative_skill": len(matches) == 1,
        "path_is_installed_copy": _same_resolved_path(matched_path, expected),
        # Pi 0.73.1 leaves loader.diagnostics undefined, so JSON.stringify
        # omits the key.  Only a present, non-empty value is an error.
        "loader_diagnostics_empty": not diagnostics,
    }
    package_name = manifest.get("name")
    package_version = manifest.get("version")
    loader_passed = all(checks.values())
    version_is_gated = (
        package_name == PI_PACKAGE_NAMES[1]
        and package_version == EXPECTED_VERSIONS["pi"]
    )
    return {
        "status": (
            "failed"
            if not loader_passed
            else ("passed" if version_is_gated else "unverified")
        ),
        "kind": "native_discovery",
        "command": "node $SCRATCH/tmp/pi-native-loader.mjs <Pi SDK entry> "
        "$SCRATCH/workspace $PI_CODING_AGENT_DIR",
        "checks": checks,
        "package_version_gate": {
            "status": "passed" if version_is_gated else "failed",
            "observed_package_name": package_name,
            "observed_version": package_version,
            "expected_package_name": PI_PACKAGE_NAMES[1],
            "expected_version": EXPECTED_VERSIONS["pi"],
        },
        "skill": matches[0] if matches else None,
        "loader_diagnostics": diagnostics,
        "evidence": "Pi DefaultResourceLoader returned the installed shared skill",
        "invocation": {
            "status": "not_run",
            "reason": "SDK loader discovery does not execute a configured model turn",
        },
    }


def _pi_discovery(paths: Mapping[str, Path], env: Mapping[str, str]) -> dict[str, Any]:
    executable = shutil.which("pi", path=env.get("PATH"))
    if executable is None:
        return {
            "status": "unavailable",
            "kind": "native_discovery",
            "reason": "pi is not in PATH; its native package/SDK loader could not be exercised",
            "minimal_requirement": f"Pi coding agent {EXPECTED_VERSIONS['pi']} with its importable SDK package",
            "invocation": {
                "status": "not_run",
                "reason": "Pi is absent and actual invocation also requires a configured model",
            },
        }
    package_roots = _pi_package_roots(executable, cwd=paths["workspace"], env=env)
    if not package_roots:
        supported_names = " or ".join(PI_PACKAGE_NAMES)
        return {
            "status": "unavailable",
            "kind": "native_discovery",
            "reason": (
                "Pi CLI is present, but its installed SDK package root is not "
                f"resolvable under either supported package name: {supported_names}"
            ),
            "minimal_requirement": (
                f"an importable {supported_names} package; the validated release "
                f"gate is {EXPECTED_VERSIONS['pi']}"
            ),
            "invocation": {
                "status": "not_run",
                "reason": "actual invocation requires a configured model",
            },
        }
    script = paths["tmp"] / "pi-native-loader.mjs"
    script.write_text(
        """import { pathToFileURL } from "node:url";
const modulePath = process.argv[2];
const cwd = process.argv[3];
const agentDir = process.argv[4];
const { DefaultResourceLoader } = await import(pathToFileURL(modulePath).href);
const loader = new DefaultResourceLoader({
  cwd,
  agentDir,
  noExtensions: true,
  noPromptTemplates: true,
  noThemes: true,
  noContextFiles: true,
});
await loader.reload();
const result = loader.getSkills();
console.log(JSON.stringify({
  skills: result.skills.map((skill) => ({name: skill.name, filePath: skill.filePath})),
  diagnostics: result.diagnostics,
}));
""",
        encoding="utf-8",
    )
    first_attempt: dict[str, Any] | None = None
    for root in package_roots:
        attempt = _pi_candidate_discovery(root, paths=paths, env=env, script=script)
        if first_attempt is None:
            first_attempt = attempt
        if attempt.get("status") in ("passed", "unverified"):
            return attempt
    if first_attempt is None:
        return {
            "status": "failed",
            "kind": "native_discovery",
            "reason": "Pi SDK candidates disappeared during discovery",
        }
    supported_names = " or ".join(PI_PACKAGE_NAMES)
    first_reason = first_attempt.get("reason", "native loader checks failed")
    first_attempt["reason"] = (
        "Pi CLI is present, but no importable SDK resolved under either "
        f"supported package name: {supported_names}. First candidate failure: "
        f"{first_reason}"
    )
    first_attempt["minimal_requirement"] = (
        f"an importable {supported_names} package; the validated release gate "
        f"is {EXPECTED_VERSIONS['pi']}"
    )
    return first_attempt


def _payload_execution(
    paths: Mapping[str, Path], env: Mapping[str, str]
) -> dict[str, Any]:
    script = paths["home"] / ".agents" / "skills" / SKILL / "scripts" / "handoff.py"
    fixture = paths["workspace"] / "capture-fixture"
    run_dir = fixture / "run"
    artifact = fixture / "result.tsv"
    handoff = fixture / "handoff.json"
    run_dir.mkdir(parents=True)
    artifact.write_text("value\n1\n", encoding="utf-8")
    (run_dir / "contract.json").write_text(
        json.dumps(
            {
                "contract_id": "native-agent-validation-fixture",
                "cwd": str(fixture),
                "declared_outputs": [str(artifact)],
                "environment": {},
            }
        ),
        encoding="utf-8",
    )
    result = _run(
        [
            sys.executable,
            str(script),
            "capture",
            str(run_dir),
            "--out",
            str(handoff),
            "--cwd",
            str(paths["workspace"]),
        ],
        cwd=paths["workspace"],
        env=env,
    )
    try:
        captured = json.loads(handoff.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        captured = {}
    runs = captured.get("runs") or []
    pointers = runs[0].get("pointers", []) if len(runs) == 1 else []
    checks = {
        "command_succeeded": result["returncode"] == 0,
        "handoff_schema": captured.get("schema_version") == 1,
        "one_run_captured": len(runs) == 1,
        "local_artifact_pointer": bool(
            len(pointers) == 1
            and pointers[0].get("path") == str(artifact)
            and pointers[0].get("exists") is True
            and pointers[0].get("size") == artifact.stat().st_size
        ),
        "missing_receipt_remains_unresolved": captured.get("unresolved")
        == [str(run_dir)],
    }
    passed = all(checks.values())
    return {
        "status": "passed" if passed else "failed",
        "kind": "standalone_script_execution",
        "command": "python3 $INSTALLED_HANDOFF capture $SCRATCH/workspace/"
        "capture-fixture/run --out $SCRATCH/workspace/capture-fixture/handoff.json "
        "--cwd $SCRATCH/workspace",
        "returncode": result["returncode"],
        "checks": checks,
        "evidence": "installed helper captured a local artifact pointer from a separate cwd",
        "stderr": result["stderr"].strip(),
        "native_agent_invocation": False,
    }


def _version_gaps(
    agents: Sequence[str],
    versions: Mapping[str, Mapping[str, Any]],
    native: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Report either a CLI pin mismatch or a loadable but unpinned SDK."""
    return [
        agent
        for agent in agents
        if versions[agent].get("version_gate") != "passed"
        or native[agent].get("status") == "unverified"
    ]


def _host() -> dict[str, Any]:
    result: dict[str, Any] = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    if platform.system() == "Darwin":
        sw = subprocess.run(["sw_vers"], text=True, capture_output=True, check=False)
        result["sw_vers"] = sw.stdout.strip()
    return result


def _git_commit(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() or None


def _non_claude_fixture(
    source_env: Mapping[str, str], scratch: Path, source_root: Path
) -> dict[str, Any]:
    fixture_root = scratch / "non-claude"
    fixture_root.mkdir()
    paths = {
        name: fixture_root / name
        for name in ("home", "xdg", "codex", "pi", "tmp", "workspace", "bin")
    }
    for name, path in paths.items():
        if name != "bin":
            path.mkdir()
    env, preconditions = _filtered_non_claude_env(paths, source_env)
    versions = {
        agent: _version(agent, cwd=paths["workspace"], env=env)
        for agent in NON_CLAUDE_AGENTS
    }

    if preconditions["status"] == "passed":
        installer = _install_non_claude(env, fixture_root, source_root)
    else:
        installer = {
            "status": "unavailable",
            "reason": "filtered executable preconditions were not satisfied",
        }

    if installer["status"] == "passed":
        snapshot = _installed_snapshot(paths)
        native = {
            "codex": _codex_discovery(paths, env),
            "opencode": _opencode_discovery(paths, env),
            "pi": _pi_discovery(paths, env),
        }
        installed_execution = _payload_execution(paths, env)
    else:
        snapshot = {
            "status": "unavailable",
            "reason": "non-Claude installation did not pass",
        }
        native = {
            agent: {
                "status": "unavailable",
                "kind": "native_discovery",
                "reason": "non-Claude installation did not pass",
                "invocation": {"status": "not_run"},
            }
            for agent in NON_CLAUDE_AGENTS
        }
        installed_execution = {
            "status": "unavailable",
            "kind": "standalone_script_execution",
            "native_agent_invocation": False,
            "reason": "non-Claude installation did not pass",
        }

    postconditions = {
        "status": "passed",
        "checks": {
            "home_claude_tree_absent_after": not (paths["home"] / ".claude").exists(),
            "claude_binary_still_not_resolvable": shutil.which(
                "claude", path=env["PATH"]
            )
            is None,
            "claude_config_variable_still_absent": "CLAUDE_CONFIG_DIR" not in env,
        },
    }
    if not all(postconditions["checks"].values()):
        postconditions["status"] = "failed"

    named_checks = (
        ("preconditions", preconditions),
        ("installer", installer),
        ("installed_snapshot", snapshot),
        ("installed_snapshot_execution", installed_execution),
        ("postconditions", postconditions),
        *native.items(),
    )
    failures = [name for name, check in named_checks if check["status"] == "failed"]
    unavailable = [
        name for name, check in named_checks if check["status"] == "unavailable"
    ]
    version_gaps = _version_gaps(NON_CLAUDE_AGENTS, versions, native)
    status = (
        "failed"
        if failures
        else ("unavailable" if unavailable or version_gaps else "passed")
    )
    return {
        "status": status,
        "purpose": "native discovery with no Claude binary or ~/.claude tree",
        "preconditions": preconditions,
        "versions": versions,
        "installer": installer,
        "installed_snapshot": snapshot,
        "native_discovery": native,
        "installed_snapshot_execution": installed_execution,
        "actual_llm_driven_invocation": {
            "status": "not_run",
            "reason": "native discovery and direct helper execution start no model turn",
        },
        "postconditions": postconditions,
        "gate": {
            "passed": status == "passed",
            "observed_failures": failures,
            "unavailable_checks": unavailable,
            "version_gaps": version_gaps,
        },
    }


def validate(source_root: Path = ROOT) -> tuple[dict[str, Any], int]:
    with tempfile.TemporaryDirectory(prefix="native-agent-validation-") as raw:
        scratch = Path(raw).resolve()
        paths = {
            name: scratch / name
            for name in ("home", "xdg", "codex", "claude", "pi", "tmp", "workspace")
        }
        for path in paths.values():
            path.mkdir()
        env = _isolated_env(paths)
        versions = {
            agent: _version(agent, cwd=paths["workspace"], env=env) for agent in AGENTS
        }
        installer = _install(env, scratch, source_root)

        if installer["status"] == "passed":
            native = {
                "claude": _claude_discovery(paths, env),
                "codex": _codex_discovery(paths, env),
                "opencode": _opencode_discovery(paths, env),
                "pi": _pi_discovery(paths, env),
            }
            payload = _payload_execution(paths, env)
        else:
            native = {
                agent: {
                    "status": "unavailable",
                    "reason": "representative installation did not pass",
                    "invocation": {"status": "not_run"},
                }
                for agent in AGENTS
            }
            payload = {
                "status": "unavailable",
                "kind": "standalone_script_execution",
                "native_agent_invocation": False,
            }

        observed_failures = [
            name
            for name, check in (
                ("installer", installer),
                ("payload", payload),
                *native.items(),
            )
            if check.get("status") == "failed"
        ]
        missing = [
            agent
            for agent in AGENTS
            if native[agent].get("status") in ("failed", "unavailable")
        ]
        version_gaps = _version_gaps(AGENTS, versions, native)
        actual_invocation = {
            "status": "not_run",
            "reason": "this credentialless harness deliberately starts no paid/configured model",
        }
        non_claude = _non_claude_fixture(env, scratch, source_root)
        if non_claude["status"] == "failed":
            observed_failures.append("non_claude_fixture")
        safe_gate_passed = (
            not observed_failures
            and not missing
            and not version_gaps
            and non_claude["status"] == "passed"
        )
        report = {
            "schema_version": 1,
            "host": _host(),
            "repository": {
                "harness_root": str(ROOT),
                "harness_commit": _git_commit(ROOT),
                "installer_source_root": str(source_root),
                "installer_source_commit": _git_commit(source_root),
            },
            "isolation": {
                "temporary_root_deleted_on_exit": True,
                "credential_environment": "allowlisted; no inherited credential variables",
                "normal_user_stores_read_or_written": False,
                "workspace_cwd_preserved": True,
            },
            "versions": versions,
            "installer": installer,
            "native_discovery": native,
            "standalone_script_execution": payload,
            "non_claude_fixture": non_claude,
            "actual_llm_driven_invocation": actual_invocation,
            "gate": {
                "safe_native_gate_passed": safe_gate_passed,
                "observed_failures": observed_failures,
                "missing_native_discovery": missing,
                "version_gaps": version_gaps,
                "non_claude_fixture_status": non_claude["status"],
                "actual_llm_invocation_is_not_proven": True,
            },
        }
        if observed_failures:
            return report, 1
        if not safe_gate_passed:
            return report, 2
        return report, 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=ROOT,
        help="installer source tree to exercise (default: this checkout)",
    )
    args = parser.parse_args(argv)
    source_root = args.source_root.resolve()
    if not (source_root / "install.sh").is_file():
        parser.error(f"installer source tree has no install.sh: {source_root}")
    report, code = validate(source_root)
    print(json.dumps(report, indent=None if args.compact else 2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
