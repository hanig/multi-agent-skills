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
            (claude / "hanig-example" / D.MARKER).write_text("version=abc123\norigin=authored\n")
            env = self._env(home)
            env["CLAUDE_CONFIG_DIR"] = str(home / "custom")
            data = D.diagnostics(env=env)
        claude_root = data["agents"]["claude"]["installation"]["roots"][0]
        self.assertEqual(claude_root["logical_path"], str(claude))
        payload = claude_root["payloads"][0]
        self.assertEqual(payload["ownership"], "owned")
        self.assertEqual(payload["installed_source_version"], "abc123")
        self.assertEqual(data["agents"]["codex"]["installation"]["roots"][0]["id"], "agents-user")
        # OpenCode consumes both configured Claude and shared .agents roots,
        # so it gets an explicit same-name conflict instead of a claim about
        # which copy it actually loaded.
        self.assertEqual(data["agents"]["opencode"]["installation"]
                         ["duplicate_names"]["hanig-example"],
                         ["claude-user", "agents-user"])

    def test_unreadable_root_blocks_discovery_instead_of_claiming_absence(self):
        root = {"id": "fixture", "kind": "native", "preferred": True,
                "logical_path": "/unreadable", "physical_path": "/unreadable", "override": None}
        with mock.patch("agent_diagnostics.os.scandir", side_effect=PermissionError):
            result = D._installation(root)
        self.assertEqual(result["state"], "unusable")
        self.assertNotEqual(result["state"], "absent")

    def test_workflow_does_not_claim_linear_ready_without_a_credential_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = D._workflow(self._env(Path(tmp)))
        self.assertEqual(workflow["dependencies"]["linear"]["state"], "unverified")
        self.assertEqual(workflow["dependencies"]["paseo"]["state"], "absent")

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
