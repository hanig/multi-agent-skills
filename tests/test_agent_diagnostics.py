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
sys.path.insert(0, str(ROOT))
from lib import skill_lifecycle as lifecycle  # noqa: E402


class TestAgentDiagnostics(unittest.TestCase):
    def _env(self, home):
        return {"HOME": str(home), "PATH": ""}

    @staticmethod
    def _record(destination, *, origin="authored", mode="copy", source_version="abc123",
                consumers="claude", link_target="", link_identity="", repo=D.REPOSITORY_ID):
        return (f"schema=2\nrepo={repo}\norigin={origin}\nsource_version={source_version}\n"
                f"version={source_version}\ndestination={destination}\nconsumers={consumers}\n"
                f"mode={mode}\ninstalled_at=2026-09-05T00:00:00Z\n"
                f"link_target={link_target}\nlink_identity={link_identity}\n")

    @staticmethod
    def _without_field(record, field):
        return "".join(line for line in record.splitlines(keepends=True)
                       if not line.startswith(field + "="))

    @classmethod
    def _invalid_copy_records(cls, destination):
        valid = cls._record(destination)
        records = {
            "invalid UTF-8": valid.encode() + b"\xff",
            "duplicate key": (valid + "repo=multi-agent-skills\n").encode(),
            "malformed nonempty line": (valid + "not-a-field\n").encode(),
            "empty field name": (valid + "=value\n").encode(),
            "empty repository": valid.replace("repo=multi-agent-skills\n", "repo=\n").encode(),
            "empty source version": valid.replace("source_version=abc123\n", "source_version=\n").encode(),
            "version mismatch": valid.replace(
                "\nversion=abc123\n", "\nversion=different\n").encode(),
            "invalid consumer equals": cls._record(destination, consumers="claude=admin").encode(),
            "invalid consumer empty component": cls._record(destination, consumers="claude,,pi").encode(),
            "invalid consumer newline": cls._record(destination, consumers="claude\npi").encode(),
            "invalid consumer carriage return": cls._record(destination, consumers="claude\rpi").encode(),
            "invalid mode": cls._record(destination, mode="mirror").encode(),
            "invalid origin": cls._record(destination, origin="unknown").encode(),
            "relative destination": valid.replace(
                f"destination={destination}\n", "destination=relative/payload\n").encode(),
            "empty installed at": valid.replace(
                "installed_at=2026-09-05T00:00:00Z\n", "installed_at=\n").encode(),
            "copy link target": valid.replace("link_target=\n", "link_target=/tmp/source\n").encode(),
            "copy link identity": valid.replace("link_identity=\n", "link_identity=1:2:3\n").encode(),
        }
        for field in ("repo", "origin", "source_version", "version", "destination",
                      "consumers", "mode", "installed_at", "link_target", "link_identity"):
            records["missing " + field] = cls._without_field(valid, field).encode()
        return records

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

    def test_parent_alias_uses_the_same_destination_identity_as_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            physical = tmp / "physical"
            physical.mkdir()
            alias = tmp / "alias"
            alias.symlink_to(physical, target_is_directory=True)
            path = alias / "payload"
            path.mkdir()
            physical_path = physical / "payload"
            (path / D.MARKER).write_text(self._record(physical_path))
            record, _ = D._marker(path)
        self.assertEqual(record["ownership"], "owned")
        self.assertEqual(record["provenance"]["state"], "valid")

    def test_parent_alias_finds_the_canonical_link_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("ok\n")
            physical = tmp / "physical"
            physical.mkdir()
            alias = tmp / "alias"
            alias.symlink_to(physical, target_is_directory=True)
            destination = alias / "payload"
            destination.symlink_to(source, target_is_directory=True)
            sidecar = D._sidecar(destination)
            sidecar.parent.mkdir()
            sidecar.write_text(self._record(
                physical / "payload", mode="link", link_target=str(source),
                link_identity=D._link_identity(destination),
            ))
            record, _ = D._marker(destination, linked=True)
        self.assertEqual(record["ownership"], "owned")
        self.assertEqual(record["provenance"]["state"], "valid")

    def test_schema_two_parser_rejects_malformed_field_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload"
            path.mkdir()
            marker = path / D.MARKER
            cases = self._invalid_copy_records(path)
            cases["wrong destination identity"] = self._record(path.parent / "elsewhere").encode()
            for label, content in cases.items():
                with self.subTest(label=label):
                    marker.write_bytes(content)
                    record, _ = D._marker(path)
                    self.assertEqual(record["ownership"], "unknown")
                    self.assertNotEqual(record["provenance"]["state"], "valid")

    def test_malformed_record_corpus_agrees_with_lifecycle_ownership(self):
        """Pin shared rejection cases without importing lifecycle from diagnostics."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("---\nname: payload\n---\n")
            labels = list(self._invalid_copy_records(tmp / "placeholder"))
            labels.append("wrong destination identity")
            for label in labels:
                with self.subTest(label=label):
                    path = tmp / ("payload-" + label.replace(" ", "-"))
                    path.mkdir()
                    (path / "SKILL.md").write_text("---\nname: payload\n---\n")
                    cases = self._invalid_copy_records(path)
                    cases["wrong destination identity"] = self._record(
                        path.parent / "elsewhere").encode()
                    (path / D.MARKER).write_bytes(cases[label])
                    diagnostic, _ = D._marker(path)
                    target = lifecycle.LifecycleTarget(
                        name=path.name, source=source, destination=path,
                        origin="authored", consumers=("claude",), source_version="next",
                    )
                    decision = lifecycle.preflight([target])[0]
                    self.assertEqual(diagnostic["ownership"], "unknown")
                    self.assertEqual(decision.action, "blocked")
                    if label != "wrong destination identity":
                        self.assertIsNone(lifecycle.read_provenance(path))

    def test_valid_historical_schema_two_records_keep_empty_and_normalized_consumers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("ok\n")
            for label, consumers, expected in (
                    ("empty", "", ()),
                    ("unsorted-duplicates", "pi,claude,pi", ("claude", "pi"))):
                with self.subTest(label=label):
                    path = tmp / label
                    path.mkdir()
                    (path / "SKILL.md").write_text("ok\n")
                    (path / D.MARKER).write_text(self._record(path, consumers=consumers))
                    diagnostic, _ = D._marker(path)
                    parsed = lifecycle.read_provenance(path)
                    target = lifecycle.LifecycleTarget(
                        name=path.name, source=source, destination=path,
                        origin="authored", consumers=(), source_version="next",
                    )
                    decision = lifecycle.preflight([target])[0]
                    self.assertEqual(diagnostic["ownership"], "owned")
                    self.assertEqual(diagnostic["provenance"]["state"], "valid")
                    self.assertIsNotNone(parsed)
                    self.assertEqual(parsed.consumers, expected)
                    self.assertEqual(decision.action, "upgrade")

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
            lifecycle_decision = lifecycle.preflight([lifecycle.LifecycleTarget(
                name=destination.name, source=source, destination=destination,
                origin="authored", consumers=("claude",), mode="link", source_version="next",
            )])[0]
        self.assertEqual(valid["ownership"], "owned")
        self.assertEqual(stale["ownership"], "unknown")
        self.assertEqual(stale["provenance"]["state"], "stale")
        self.assertEqual(lifecycle_decision.action, "blocked")

    def test_link_schema_and_object_identity_match_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("ok\n")
            for label in ("valid", "relative-target", "missing-target", "missing-identity",
                          "wrong-target", "wrong-identity"):
                with self.subTest(label=label):
                    destination = tmp / ("link-" + label)
                    destination.symlink_to(source, target_is_directory=True)
                    target = str(source)
                    identity = D._link_identity(destination)
                    if label == "relative-target":
                        target = "relative/source"
                    elif label == "missing-target":
                        target = ""
                    elif label == "missing-identity":
                        identity = ""
                    elif label == "wrong-target":
                        other = tmp / "other"
                        other.mkdir(exist_ok=True)
                        target = str(other)
                    elif label == "wrong-identity":
                        identity = "0:0:0"
                    sidecar = D._sidecar(destination)
                    sidecar.parent.mkdir(exist_ok=True)
                    sidecar.write_text(self._record(
                        destination, mode="link", link_target=target,
                        link_identity=identity,
                    ))
                    diagnostic, _ = D._marker(destination, linked=True)
                    parsed = lifecycle.read_provenance(destination)
                    lifecycle_target = lifecycle.LifecycleTarget(
                        name=destination.name, source=source, destination=destination,
                        origin="authored", consumers=("claude",), mode="link",
                        source_version="next",
                    )
                    decision = lifecycle.preflight([lifecycle_target])[0]
                    if label == "valid":
                        self.assertEqual(diagnostic["ownership"], "owned")
                        self.assertIsNotNone(parsed)
                        self.assertEqual(decision.action, "upgrade")
                    else:
                        self.assertEqual(diagnostic["ownership"], "unknown")
                        self.assertEqual(decision.action, "blocked")
                        if label in ("relative-target", "missing-target", "missing-identity"):
                            self.assertIsNone(parsed)

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

    def test_read_only_diagnostics_do_not_write_bytecode_into_copied_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            copied = tmp / "scripts"
            copied.mkdir()
            for name in ("agent_diagnostics.py", "agent_discovery.py"):
                shutil.copy2(SCRIPTS / name, copied / name)
            env = {"HOME": str(tmp / "home"), "PATH": str(self._bin(tmp))}
            result = subprocess.run([sys.executable, str(copied / "agent_diagnostics.py"), "--json"],
                                    cwd=tmp, env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((copied / "__pycache__").exists())
