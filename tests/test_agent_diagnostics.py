"""Acceptance evidence for separate agent/install/discovery/workflow facts."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-project" / "scripts"
DOCTOR = ROOT / "bin" / "doctor"
SURVEY = SCRIPTS / "survey.py"
sys.path.insert(0, str(SCRIPTS))
import agent_diagnostics as D  # noqa: E402


class TestAgentDiagnostics(unittest.TestCase):
    def _env(self, home):
        return {"HOME": str(home), "PATH": ""}

    @staticmethod
    def _record(destination, *, origin="authored", mode="copy", source_version="abc123",
                link_target="", link_identity="", repo=D.REPOSITORY_ID):
        return (f"schema=2\nrepo={repo}\norigin={origin}\nsource_version={source_version}\n"
                f"version={source_version}\ndestination={destination}\nconsumers=claude\n"
                f"mode={mode}\ninstalled_at=2026-09-05T00:00:00Z\n"
                f"link_target={link_target}\nlink_identity={link_identity}\n")

    def test_no_claude_still_reports_every_agent_and_each_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = D.diagnostics(env=self._env(Path(tmp)))
        self.assertEqual(set(data["agents"]), {"claude", "codex", "opencode", "pi"})
        for agent in data["agents"].values():
            self.assertIn(agent["agent_present"]["state"], {"absent", "configured", "undetermined", "executable_found"})
            self.assertIn("installation", agent)
            self.assertEqual(agent["discovery"]["native_probe"], "not run")
            self.assertIn("workflow", agent)
        self.assertIn("selection", data)

    def test_custom_root_payload_ownership_version_and_duplicate_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            claude = home / "custom" / "skills"
            shared = home / ".agents" / "skills"
            for root in (claude, shared):
                skill = root / "hanig-example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("---\nname: example\n---\n")
            target = claude / "hanig-example"
            (target / D.MARKER).write_text(self._record(target))
            env = self._env(home)
            env["CLAUDE_CONFIG_DIR"] = str(home / "custom")
            data = D.diagnostics(env=env)
        claude_root = data["agents"]["claude"]["installation"]["roots"][0]
        self.assertEqual(claude_root["logical_path"], str(claude))
        payload = claude_root["payloads"][0]
        self.assertEqual(payload["ownership"], "owned")
        self.assertEqual(payload["installed_source_version"], "abc123")
        self.assertEqual(data["agents"]["codex"]["installation"]["roots"][0]["id"], "agents-user")
        # The shared .agents payload is visible through OpenCode's explicit
        # compatibility root, while a separately configured Claude root is
        # not silently treated as OpenCode configuration.
        open_roots = data["agents"]["opencode"]["installation"]["roots"]
        shared_root = next(root for root in open_roots if root["id"] == "agents-user")
        self.assertEqual(shared_root["payloads"][0]["name"], "hanig-example")
        self.assertNotIn("hanig-example", data["agents"]["opencode"]["installation"]["duplicate_names"])

    def test_unreadable_root_blocks_discovery_instead_of_claiming_absence(self):
        root = {"id": "fixture", "kind": "native", "preferred": True,
                "logical_path": "/unreadable", "physical_path": "/unreadable", "override": None}
        with mock.patch("agent_diagnostics.os.scandir", side_effect=PermissionError):
            result = D._installation(root)
        self.assertEqual(result["state"], "unusable")
        self.assertNotEqual(result["state"], "absent")

    def test_workflow_separates_baseline_optional_and_per_skill_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            skill = root / "paseo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("ok\n")
            workflow = D._workflow(self._env(Path(tmp)), [D._installation(
                {"id": "fixture", "kind": "native", "preferred": True,
                 "logical_path": str(root), "physical_path": str(root), "override": None})])
        self.assertEqual(workflow["optional_dependencies"]["linear"]["state"], "unverified")
        self.assertEqual(workflow["optional_dependencies"]["paseo_executable"]["state"], "absent")
        self.assertIn("agent_bus_registry", workflow["skills"]["paseo"]["requirements"])

    def test_foreign_marker_is_not_promoted_to_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "foreign"
            path.mkdir()
            (path / D.MARKER).write_text(self._record(path, repo="another-installer"))
            record, _ = D._marker(path)
        self.assertEqual(record["ownership"], "foreign")
        self.assertEqual(record["provenance"]["state"], "foreign")

    def test_oversized_or_legacy_marker_is_never_claimed_as_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload"
            path.mkdir()
            marker = path / D.MARKER
            marker.write_bytes(b"repo=multi-agent-skills\n" + b"x" * D.MAX_MARKER_BYTES)
            oversized, _ = D._marker(path)
            marker.write_text("repo=multi-agent-skills\norigin=authored\nversion=old\n")
            legacy, _ = D._marker(path)
        self.assertEqual(oversized["ownership"], "unknown")
        self.assertEqual(oversized["provenance"]["state"], "unknown")
        self.assertEqual(legacy["ownership"], "unknown")
        self.assertEqual(legacy["provenance"]["state"], "legacy")

    def test_schema_two_record_without_source_version_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload"
            path.mkdir()
            content = self._record(path).replace("source_version=abc123\n", "")
            (path / D.MARKER).write_text(content)
            record, _ = D._marker(path)
        self.assertEqual(record["ownership"], "unknown")
        self.assertEqual(record["provenance"]["state"], "stale")

    def test_valid_and_stale_link_sidecars_are_distinguished(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source, destination = tmp / "source", tmp / "link"
            source.mkdir()
            (source / "SKILL.md").write_text("ok\n")
            destination.symlink_to(source, target_is_directory=True)
            sidecar = D._sidecar(destination)
            sidecar.parent.mkdir()
            sidecar.write_text(self._record(destination, mode="link", link_target=str(source),
                                             link_identity=D._link_identity(destination)))
            valid, _ = D._marker(destination, linked=True)
            destination.unlink()
            foreign = tmp / "foreign"
            foreign.mkdir()
            destination.symlink_to(foreign, target_is_directory=True)
            stale, _ = D._marker(destination, linked=True)
        self.assertEqual(valid["ownership"], "owned")
        self.assertEqual(stale["ownership"], "unknown")
        self.assertEqual(stale["provenance"]["state"], "stale")

    def test_skill_without_a_loadable_skill_file_is_unusable_not_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "disabled-by-missing-definition").mkdir(parents=True)
            item = D._installation({"id": "fixture", "kind": "native", "preferred": True,
                                    "logical_path": str(root), "physical_path": str(root),
                                    "override": None})
        self.assertEqual(item["payloads"][0]["state"], "unusable")
        self.assertEqual(item["payloads"][0]["skill_file"]["state"], "absent")

    def test_mixed_versions_remain_separate_from_installation_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bindir = tmp / "bin"
            bindir.mkdir()
            for name, version in (("claude", "2.1.261"), ("codex", "9.9.9")):
                tool = bindir / name
                tool.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
                tool.chmod(0o755)
            env = self._env(tmp)
            env["PATH"] = str(bindir)
            data = D.diagnostics(env=env)
        self.assertEqual(data["agents"]["claude"]["agent_present"]["version"], "2.1.261")
        self.assertEqual(data["agents"]["codex"]["agent_present"]["version"], "9.9.9")
        self.assertEqual(data["agents"]["codex"]["discovery"]["verification"], "unverified")
        self.assertEqual(data["agents"]["codex"]["installation"]["state"], "absent")


class TestDoctorAndSurveyAgentOutput(unittest.TestCase):
    def _bin(self, directory):
        bindir = Path(directory) / "bin"
        bindir.mkdir()
        for name, version in (("claude", "2.1.261"), ("codex", "0.153.4"),
                              ("opencode", "1.18.29"), ("pi", "0.73.1")):
            path = bindir / name
            path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
            path.chmod(0o755)
        for name in ("perl", "sh"):
            found = shutil.which(name)
            if found:
                os.symlink(found, bindir / name)
        os.symlink(sys.executable, bindir / "python3")
        return bindir

    def test_doctor_json_honors_prefix_and_contains_all_agent_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            home, prefix = tmp / "home", tmp / "chosen-skills"
            (prefix / "x" ).mkdir(parents=True)
            (prefix / "x" / "SKILL.md").write_text("ok\n")
            env = {"HOME": str(home), "PATH": str(self._bin(tmp))}
            result = subprocess.run(["sh", str(DOCTOR), "--prefix", str(prefix), "--json"],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(set(value["agents"]), {"claude", "codex", "opencode", "pi"})
        self.assertEqual(value["agents"]["claude"]["installation"]["roots"][0]["logical_path"], str(prefix))

    def test_doctor_without_prefix_uses_effective_claude_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            home, config = tmp / "home", tmp / "custom-claude"
            (config / "skills" / "fixture").mkdir(parents=True)
            (config / "skills" / "fixture" / "SKILL.md").write_text("ok\n")
            env = {"HOME": str(home), "PATH": str(self._bin(tmp)),
                   "CLAUDE_CONFIG_DIR": str(config)}
            result = subprocess.run(["sh", str(DOCTOR), "--json"], cwd=ROOT, env=env,
                                    text=True, capture_output=True, timeout=20)
        value = json.loads(result.stdout)
        self.assertEqual(value["agents"]["claude"]["installation"]["roots"][0]["logical_path"],
                         str(config / "skills"))

    def test_doctor_json_returns_a_complete_truncation_record_for_large_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            prefix = tmp / "large-skills"
            for number in range(500):
                skill = prefix / ("skill-" + str(number).zfill(4) + "-metadata" * 3)
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("ok\n")
            env = {"HOME": str(tmp / "home"), "PATH": str(self._bin(tmp))}
            result = subprocess.run(["sh", str(DOCTOR), "--prefix", str(prefix), "--json"],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
        value = json.loads(result.stdout)
        self.assertTrue(value.get("truncated"), value)
        self.assertEqual(value["state"], "unknown")

    def test_doctor_json_keeps_an_ordinary_thirteen_skill_install_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            prefix = tmp / "normal-skills"
            for number in range(13):
                skill = prefix / f"skill-{number}"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("ok\n")
            env = {"HOME": str(tmp / "home"), "PATH": str(self._bin(tmp))}
            result = subprocess.run(["sh", str(DOCTOR), str(prefix), "--json"],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=20)
        value = json.loads(result.stdout)
        self.assertNotIn("truncated", value)
        self.assertEqual(len(value["agents"]["claude"]["installation"]["roots"][0]["payloads"]), 13)

    def test_survey_preserves_existing_keys_and_adds_agent_diagnostics_without_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            env = {"HOME": str(tmp / "home"), "PATH": str(self._bin(tmp))}
            # Make the no-Claude case explicit without relying on this host.
            (tmp / "bin" / "claude").unlink()
            result = subprocess.run([sys.executable, str(SURVEY), "--repo", str(tmp), "--json"],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["schema_version"], 4)
        self.assertTrue({"machine", "scheduler", "repo", "storage"}.issubset(value))
        self.assertEqual(value["agent_diagnostics"]["agents"]["claude"]["agent_present"]["state"], "absent")
