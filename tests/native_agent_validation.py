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
EXPECTED_VERSIONS = {
    "claude": "2.1.261",
    "codex": "0.153.4",
    "opencode": "1.18.29",
    "pi": "0.73.1",
}
VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")
MAX_CAPTURE_CHARS = 1_000_000


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


def _install(env: Mapping[str, str], scratch: Path) -> dict[str, Any]:
    argv = [
        "sh",
        str(ROOT / "install.sh"),
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
    result = _run(argv, cwd=ROOT, env=env, timeout=60)
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


def _pi_package_root(
    executable: str, *, cwd: Path, env: Mapping[str, str]
) -> Path | None:
    candidates: list[Path] = []
    resolved = Path(executable).resolve()
    candidates.extend(resolved.parents)
    for command in (("npm", "root", "-g"), ("pnpm", "root", "-g")):
        if shutil.which(command[0], path=env.get("PATH")) is None:
            continue
        result = _run(command, cwd=cwd, env=env, timeout=15)
        if result["returncode"] == 0 and result["stdout"].strip():
            candidates.append(
                Path(result["stdout"].strip()) / "@mariozechner" / "pi-coding-agent"
            )
    for candidate in candidates:
        manifest = candidate / "package.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if data.get("name") == "@mariozechner/pi-coding-agent":
            return candidate
    return None


def _pi_discovery(paths: Mapping[str, Path], env: Mapping[str, str]) -> dict[str, Any]:
    executable = shutil.which("pi", path=env.get("PATH"))
    if executable is None:
        return {
            "status": "unavailable",
            "kind": "native_discovery",
            "reason": "pi is not in PATH; its native package/SDK loader could not be exercised",
            "minimal_requirement": "Pi coding agent 0.73.1 with its importable SDK package",
            "invocation": {
                "status": "not_run",
                "reason": "Pi is absent and actual invocation also requires a configured model",
            },
        }
    package_root = _pi_package_root(executable, cwd=paths["workspace"], env=env)
    if package_root is None:
        return {
            "status": "unavailable",
            "kind": "native_discovery",
            "reason": "Pi CLI is present, but its installed SDK package root is not resolvable",
            "minimal_requirement": "an importable @mariozechner/pi-coding-agent 0.73.1 package",
            "invocation": {
                "status": "not_run",
                "reason": "actual invocation requires a configured model",
            },
        }
    try:
        manifest = json.loads(
            (package_root / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"status": "failed", "kind": "native_discovery", "reason": str(exc)}
    entry = package_root / str(manifest.get("main", "./dist/index.js"))
    if not entry.is_file():
        return {
            "status": "unavailable",
            "kind": "native_discovery",
            "reason": f"Pi SDK entry point is absent: {entry}",
            "minimal_requirement": "the normal Node package build, not a CLI-only compiled binary",
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
    result = _run(
        ["node", str(script), str(entry), str(paths["workspace"]), str(paths["pi"])],
        cwd=paths["workspace"],
        env=env,
        timeout=30,
    )
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {
            "status": "failed",
            "kind": "native_discovery",
            "reason": f"Pi SDK loader output was not JSON: {exc}",
            "stderr": result["stderr"].strip(),
        }
    matches = [
        skill for skill in payload.get("skills", []) if skill.get("name") == SKILL
    ]
    expected = paths["home"] / ".agents" / "skills" / SKILL / "SKILL.md"
    checks = {
        "command_succeeded": result["returncode"] == 0,
        "package_version_is_gated": manifest.get("version") == EXPECTED_VERSIONS["pi"],
        "one_representative_skill": len(matches) == 1,
        "path_is_installed_copy": bool(
            matches
            and Path(matches[0].get("filePath", "")).resolve() == expected.resolve()
        ),
        "loader_diagnostics_empty": not payload.get("diagnostics"),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "kind": "native_discovery",
        "command": "node $SCRATCH/tmp/pi-native-loader.mjs <Pi SDK entry> "
        "$SCRATCH/workspace $PI_CODING_AGENT_DIR",
        "checks": checks,
        "skill": matches[0] if matches else None,
        "loader_diagnostics": payload.get("diagnostics", []),
        "evidence": "Pi DefaultResourceLoader returned the installed shared skill",
        "invocation": {
            "status": "not_run",
            "reason": "SDK loader discovery does not execute a configured model turn",
        },
    }


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


def validate() -> tuple[dict[str, Any], int]:
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
        installer = _install(env, scratch)

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
        missing = [agent for agent in AGENTS if native[agent].get("status") != "passed"]
        version_gaps = [
            agent for agent in AGENTS if versions[agent].get("version_gate") != "passed"
        ]
        actual_invocation = {
            "status": "not_run",
            "reason": "this credentialless harness deliberately starts no paid/configured model",
        }
        safe_gate_passed = not observed_failures and not missing and not version_gaps
        report = {
            "schema_version": 1,
            "host": _host(),
            "repository": {
                "root": str(ROOT),
                "commit": subprocess.run(
                    ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                    text=True,
                    capture_output=True,
                    check=False,
                ).stdout.strip(),
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
            "actual_llm_driven_invocation": actual_invocation,
            "gate": {
                "safe_native_gate_passed": safe_gate_passed,
                "observed_failures": observed_failures,
                "missing_native_discovery": missing,
                "version_gaps": version_gaps,
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
    args = parser.parse_args(argv)
    report, code = validate()
    print(json.dumps(report, indent=None if args.compact else 2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
