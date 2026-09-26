"""Acceptance evidence for separate agent/install/discovery/workflow facts."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
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


# These tests exercise diagnostic content, not production latency limits.
# Keep real probes, with room for process startup on a shared host. Doctor's
# outer supervisor must allow all four probes plus their cleanup to finish.
REAL_PROBE_SECONDS = 60
REAL_REAP_SECONDS = 5
DOCTOR_SECONDS = 360
WATCHDOG_SECONDS = 420


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
            self.assertIn(agent["agent_present"]["state"],
                          {"absent", "configured", "slow", "probe_failed",
                           "executable_found"})
            self.assertIn("installation", agent)
            self.assertEqual(agent["discovery"]["native_probe"], "not run")
            self.assertIn("workflow", agent)
        self.assertIn("selection", data)

    def test_diagnostics_rechecks_report_claims_and_next_step(self):
        discovery = D.agent_discovery
        for version, current in (("2.1.261", False), ("2.1.282", True), ("2.1.283", False)):
            for fields in ({}, {"verification_review_due": None},
                           {"verification_review_due": "2099-12-31"}):
                with self.subTest(version=version, fields=fields), \
                        tempfile.TemporaryDirectory() as raw, \
                        mock.patch.object(discovery, "date", wraps=date) as clock:
                    clock.today.return_value = date(2026, 10, 6)
                    env = self._env(Path(raw))
                    report = discovery.discover(env,
                        which=lambda name: "/fixtures/claude" if name == "claude" else None,
                        probe=lambda path, timeout: (True, version))
                    record = report["agents"]["claude"]
                    record.pop("verification_review_due")
                    record.update(verification="verified", **fields)
                    with mock.patch.object(discovery, "discover", return_value=report):
                        result = D.diagnostics(env=env)
                    agent = result["agents"]["claude"]
                    expected = "verified" if current else "unverified"
                    self.assertEqual(agent["discovery"]["verification"], expected)
                    self.assertEqual(result["selection"]["selected"][0]["certification"], expected)
                    if current:
                        self.assertIsNone(agent["next_step"])
                    else:
                        self.assertIn("this executable version is unverified", agent["next_step"])

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
            with mock.patch.object(D.agent_discovery, "probe_deadline",
                                   return_value=REAL_PROBE_SECONDS), \
                    mock.patch.object(D.agent_discovery, "PROBE_REAP_SECONDS", REAL_REAP_SECONDS):
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
        shell = shutil.which("sh")
        if shell:
            os.symlink(shell, bindir / "sh")
        perl = shutil.which("perl")
        if perl:
            # Run doctor's actual Perl supervisor, changing only its fixture
            # budgets. Its production six-second outer limit would otherwise
            # kill diagnostics before the fixture's probe budget can help.
            # PATH contains only bindir, whose python3 symlink selects this
            # test's interpreter without putting its path into a shebang.
            wrapper = bindir / "perl"
            wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "args = sys.argv[1:]\n"
                "budget = args.index('-e') + 2\n"
                "assert len(args) > budget + 2\n"
                "assert all(float(value) > 0 for value in args[budget:budget + 2])\n"
                f"args[budget:budget + 2] = [{str(DOCTOR_SECONDS)!r}, {str(REAL_REAP_SECONDS)!r}]\n"
                f"os.execv({perl!r}, [{perl!r}] + args)\n")
            wrapper.chmod(0o755)
        os.symlink(sys.executable, bindir / "python3")
        return bindir

    def _probe_env(self, directory):
        """Give actual doctor/survey children test-owned discovery budgets."""
        bindir = self._bin(directory)
        hooks = directory / "clock-fixture"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text(
            "import agent_discovery\n"
            f"agent_discovery.probe_deadline = lambda spec: {REAL_PROBE_SECONDS!r}\n"
            f"agent_discovery.PROBE_REAP_SECONDS = {REAL_REAP_SECONDS!r}\n")
        return {"HOME": str(directory / "home"), "PATH": str(bindir),
                "PYTHONPATH": os.pathsep.join((str(hooks), str(SCRIPTS))),
                "PYTHONDONTWRITEBYTECODE": "1"}

    def _frozen_env(self, directory, version, observed):
        """Freeze discovery's date as well as its child probe budgets."""
        env = self._probe_env(directory)
        (directory / "bin" / "claude").write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
        with (directory / "clock-fixture" / "sitecustomize.py").open("a") as hook:
            hook.write(
                "import datetime\nimport agent_discovery\n"
                "class FrozenDate(datetime.date):\n"
                "    @classmethod\n"
                "    def today(cls):\n"
                f"        return cls.fromisoformat({observed.isoformat()!r})\n"
                "agent_discovery.date = FrozenDate\n")
        return env

    def _populated_env(self, directory):
        env = self._frozen_env(directory, "2.1.261", date(2026, 9, 25))
        home = Path(env["HOME"])
        for relative in (".claude/skills", ".agents/skills",
                         ".config/opencode/skills", ".pi/agent/skills"):
            for number in range(30):
                skill = home / relative / f"hanig-fixture-{number:02d}"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("fixture skill\n")
                (skill / D.MARKER).write_text(TestAgentDiagnostics._record(skill))
        return env

    @staticmethod
    def _without_perl_json(directory, env):
        shadow = directory / "perl-lib" / "JSON"
        shadow.mkdir(parents=True)
        (shadow / "PP.pm").write_text('die "fixture: JSON::PP unavailable\\n";\n')
        env["PERL5LIB"] = str(shadow.parent)

    def test_doctor_json_expiry_changes_verification_and_next_step(self):
        for version, deadline in (("2.1.261", date(2026, 10, 5)),
                                  ("2.1.282", date(2026, 10, 25))):
            for offset, expected in ((-1, "verified"), (0, "verified"), (1, "unverified")):
                observed = deadline + timedelta(days=offset)
                with self.subTest(version=version, observed=observed), \
                        tempfile.TemporaryDirectory() as raw:
                    directory = Path(raw)
                    env = self._frozen_env(directory, version, observed)
                    result = subprocess.run([str(DOCTOR), "--json"], cwd=directory,
                                            env=env, text=True, capture_output=True, timeout=WATCHDOG_SECONDS)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    value = json.loads(result.stdout)
                    agent = value["agents"]["claude"]
                    self.assertEqual(agent["agent_present"]["version"], version)
                    self.assertEqual(agent["discovery"]["verification"], expected)
                    self.assertEqual(agent["next_step"],
                        "Use an explicitly supported version or pass an explicit target; this executable version is unverified."
                        if expected == "unverified" else None)
                    selected = next(item for item in value["selection"]["selected"]
                                    if item["agent"] == "claude")
                    self.assertEqual(selected["certification"], expected)
                    self.assertFalse((directory / "home").exists())

    def test_doctor_rechecks_forged_report_deadlines(self):
        for version, expected in (("2.1.261", "unverified"), ("2.1.283", "unverified"),
                                  ("2.1.282", "verified")):
            for fields in ({}, {"verification_review_due": None},
                           {"verification_review_due": "2099-12-31"}):
                with self.subTest(version=version, fields=fields), \
                        tempfile.TemporaryDirectory() as raw:
                    directory = Path(raw)
                    env = self._frozen_env(directory, version, date(2026, 10, 6))
                    hook = directory / "clock-fixture" / "sitecustomize.py"
                    with hook.open("a") as handle:
                        handle.write(
                            "original_discover = agent_discovery.discover\n"
                            "def forged_discover(*args, **kwargs):\n"
                            "    report = original_discover(*args, **kwargs)\n"
                            "    record = report['agents']['claude']\n"
                            "    record.pop('verification_review_due')\n"
                            f"    record.update(verification='verified', **{fields!r})\n"
                            "    return report\n"
                            "agent_discovery.discover = forged_discover\n")
                    result = subprocess.run([str(DOCTOR), "--json"], cwd=directory,
                        env=env, text=True, capture_output=True, timeout=WATCHDOG_SECONDS)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    value = json.loads(result.stdout)
                    agent = value["agents"]["claude"]
                    self.assertEqual(agent["discovery"]["verification"], expected)
                    selected = next(item for item in value["selection"]["selected"]
                                    if item["agent"] == "claude")
                    self.assertEqual(selected["certification"], expected)
                    if expected == "unverified":
                        self.assertIn("this executable version is unverified", agent["next_step"])
                    else:
                        self.assertIsNone(agent["next_step"])

    def test_saved_survey_does_not_certify_expired_evidence(self):
        for version, observed in (("2.1.261", date(2026, 10, 6)),
                                   ("2.1.282", date(2026, 10, 26))):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                env = self._frozen_env(directory, version, observed)
                output = directory / "survey.json"
                result = subprocess.run(
                    [sys.executable, str(SURVEY), "--repo", str(directory), "--out", str(output)],
                    cwd=directory, env=env, text=True, capture_output=True, timeout=WATCHDOG_SECONDS)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(output.read_text())["agent_diagnostics"]
                agent = value["agents"]["claude"]
                self.assertEqual(agent["agent_present"]["version"], version)
                self.assertEqual(agent["discovery"]["verification"], "unverified")
                self.assertIn("this executable version is unverified", agent["next_step"])
                self.assertFalse((directory / "home").exists())

    def test_doctor_json_honors_prefix_and_contains_all_agent_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            home, prefix = tmp / "home", tmp / "chosen-skills"
            (prefix / "x" ).mkdir(parents=True)
            (prefix / "x" / "SKILL.md").write_text("ok\n")
            env = self._probe_env(tmp)
            result = subprocess.run(["sh", str(DOCTOR), "--prefix", str(prefix), "--json"],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=WATCHDOG_SECONDS)
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
            env = self._probe_env(tmp)
            env["CLAUDE_CONFIG_DIR"] = str(config)
            result = subprocess.run(["sh", str(DOCTOR), "--json"], cwd=ROOT, env=env,
                                    text=True, capture_output=True, timeout=WATCHDOG_SECONDS)
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
            env = self._probe_env(tmp)
            result = subprocess.run(["sh", str(DOCTOR), "--prefix", str(prefix), "--json"],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=WATCHDOG_SECONDS)
        value = json.loads(result.stdout)
        self.assertTrue(value.get("truncated"), value)
        self.assertEqual(value["state"], "unknown")

    def test_doctor_json_keeps_realistic_four_agent_host_complete(self):
        """Exercise the public consumer with detail larger than its old tail."""
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            env = self._populated_env(directory)
            full = subprocess.run(
                [str(directory / "bin" / "python3"),
                 str(SCRIPTS / "agent_diagnostics.py"), "--json"],
                cwd=directory, env=env, capture_output=True, text=True,
                timeout=WATCHDOG_SECONDS)
            self.assertEqual(full.returncode, 0, full.stderr)
            self.assertGreaterEqual(len(full.stdout.encode("utf-8")), 70_000)
            result = subprocess.run([str(DOCTOR), "--json"], cwd=directory,
                                    env=env, text=True, capture_output=True,
                                    timeout=WATCHDOG_SECONDS)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertNotEqual(value.get("state"), "unknown", value)
            self.assertNotIn("truncated", value)
            self.assertEqual(set(value["agents"]), {"claude", "codex", "opencode", "pi"})
            # scandir order and measured probe duration vary between runs.
            # Compare every other fact exactly.
            expected = json.loads(full.stdout)
            for report in (value, expected):
                for agent in report["agents"].values():
                    probe = agent["agent_present"]["executable"]
                    self.assertGreaterEqual(probe.pop("elapsed_seconds"), 0)
                    for root in agent["installation"]["roots"]:
                        root["payloads"].sort(key=lambda payload: payload["name"])
            self.assertEqual(value, expected)
            for agent in value["agents"].values():
                self.assertEqual(agent["agent_present"]["state"], "executable_found")
                self.assertEqual(agent["installation"]["state"], "present")

    def test_doctor_json_keeps_an_ordinary_thirteen_skill_install_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            prefix = tmp / "normal-skills"
            for number in range(13):
                skill = prefix / f"skill-{number}"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("ok\n")
            env = self._probe_env(tmp)
            result = subprocess.run(["sh", str(DOCTOR), str(prefix), "--json"],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=WATCHDOG_SECONDS)
        value = json.loads(result.stdout)
        self.assertNotIn("truncated", value)
        self.assertEqual(len(value["agents"]["claude"]["installation"]["roots"][0]["payloads"]), 13)

    def _fixture_json_result(self, directory, body, deadline=DOCTOR_SECONDS,
                             without_perl_json=False, validator_body=None):
        """Drive doctor itself with arbitrary child output and private scratch."""
        bindir = directory / "bin"
        bindir.mkdir()
        scratch = directory / "scratch"
        scratch.mkdir()
        child = directory / "result.py"
        child.write_text(body)
        python = bindir / "python3"
        validator_args = ' "$@"'
        if validator_body is not None:
            validator = directory / "validator.py"
            validator.write_text(validator_body)
            validator_args = ' ' + shlex.quote(str(validator))
        python.write_text('#!/bin/sh\nif [ "$1" = -c ]; then exec ' +
                          shlex.quote(sys.executable) + validator_args + '; fi\nexec ' +
                          shlex.quote(sys.executable) + " " +
                          shlex.quote(str(child)) + "\n")
        python.chmod(0o755)
        perl = bindir / "perl"
        perl.write_text(
            '#!/bin/sh\nsource=$2\nshift 4\nexec ' + shlex.quote(shutil.which("perl")) +
            f' -e "$source" {deadline} {REAL_REAP_SECONDS} "$@"\n')
        perl.chmod(0o755)
        env = {"HOME": str(directory / "home"), "PATH": str(bindir),
               "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"}
        if without_perl_json:
            self._without_perl_json(directory, env)
        result = subprocess.run([str(DOCTOR), "--json"], cwd=directory, env=env,
                                capture_output=True, text=True, timeout=WATCHDOG_SECONDS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(scratch.iterdir()), [], "doctor left its result file behind")
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 262_145)
        return json.loads(result.stdout)

    def test_doctor_without_perl_json_keeps_large_host_health_in_summary(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            env = self._populated_env(directory)
            self._without_perl_json(directory, env)
            full = subprocess.run([str(directory / "bin" / "python3"),
                                   str(SCRIPTS / "agent_diagnostics.py"), "--json"],
                                  env=env, capture_output=True, text=True,
                                  timeout=WATCHDOG_SECONDS)
            self.assertEqual(full.returncode, 0, full.stderr)
            self.assertGreaterEqual(len(full.stdout.encode()), 70_000)
            result = subprocess.run([str(DOCTOR), "--json"], env=env,
                                    capture_output=True, text=True, timeout=WATCHDOG_SECONDS)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertNotEqual(summary.get("state"), "unknown", summary)
            self.assertEqual(summary["detail"], "summary")
            self.assertNotIn("truncated", summary)
            self.assertLessEqual(len(result.stdout.encode()), 48_001)
            expected = json.loads(full.stdout)
            self.assertEqual(set(summary["agents"]), {"claude", "codex", "opencode", "pi"})
            self.assertEqual(summary["selection"], expected["selection"])
            for name, agent in summary["agents"].items():
                original = expected["agents"][name]
                for key in ("identity", "discovery", "next_step"):
                    self.assertEqual(agent[key], original[key])
                for key in ("state", "version"):
                    self.assertEqual(agent["agent_present"][key], original["agent_present"][key])
                self.assertEqual(agent["installation"]["state"], "present")
                self.assertEqual(agent["workflow"]["state"], original["workflow"]["state"])
                for root in agent["installation"]["roots"]:
                    self.assertNotIn("payloads", root)
                    if root["state"] == "present":
                        self.assertEqual(root["payload_count"], 30)
                        self.assertEqual(root["ownership_counts"], {"owned": 30})

    def test_doctor_without_perl_json_still_bounds_an_oversized_summary(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            env = self._frozen_env(directory, "2.1.261", date(2026, 9, 25))
            self._without_perl_json(directory, env)
            with (directory / "clock-fixture" / "sitecustomize.py").open("a") as hook:
                hook.write(
                    "original_discover = agent_discovery.discover\n"
                    "def oversized_discover(*args, **kwargs):\n"
                    "    report = original_discover(*args, **kwargs)\n"
                    "    report['agents']['claude']['identity'] = 'x' * 50000\n"
                    "    return report\n"
                    "agent_discovery.discover = oversized_discover\n")
            result = subprocess.run([str(DOCTOR), "--json"], env=env,
                                    capture_output=True, text=True, timeout=WATCHDOG_SECONDS)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["state"], "unknown")
            self.assertTrue(value["truncated"])
            self.assertGreater(value["estimated_bytes"], 48_000)
            self.assertLessEqual(len(result.stdout.encode()), 48_001)

    def test_doctor_result_file_io_failures_retain_the_health_summary(self):
        faults = (
            ("syswrite", "return undef", 'CORE::syswrite($_[0], $_[1], length($_[1]))'),
            ("syswrite", "return CORE::syswrite($_[0], $_[1], 1)",
             'CORE::syswrite($_[0], $_[1], length($_[1]))'),
            ("sysseek", "return undef", 'CORE::sysseek($_[0], $_[1], $_[2])'),
            ("sysread", "return undef", 'CORE::sysread($_[0], $_[1], $_[2])'),
            ("sysread", "return 0", 'CORE::sysread($_[0], $_[1], $_[2])'),
        )
        for operation, fault, forward in faults:
            with self.subTest(operation=operation, fault=fault), tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                env = self._populated_env(directory)
                library = directory / "perl-fault"
                library.mkdir()
                # Fault only the supervisor-owned regular file. Its setup and
                # data pipes still perform real I/O through the real consumer.
                (library / "DoctorFileFault.pm").write_text(
                    "package DoctorFileFault;\nuse Errno qw(ENOSPC);\nBEGIN {\n"
                    f"*CORE::GLOBAL::{operation} = sub {{\n"
                    f'if (ref($_[0]) eq "File::Temp") {{ $! = ENOSPC; {fault}; }}\n'
                    f"return {forward};\n}};\n}}\n1;\n")
                env.update(PERL5LIB=str(library), PERL5OPT="-MDoctorFileFault")
                result = subprocess.run([str(DOCTOR), "--json"], env=env,
                                        capture_output=True, text=True, timeout=WATCHDOG_SECONDS)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertNotEqual(value.get("state"), "unknown", (value, result.stderr))
                self.assertNotIn("truncated", value)
                self.assertEqual(value["detail"], "summary")
                self.assertEqual(set(value["agents"]), {"claude", "codex", "opencode", "pi"})
                for agent in value["agents"].values():
                    self.assertEqual(agent["agent_present"]["state"], "executable_found")
                    self.assertEqual(agent["installation"]["state"], "present")
                    for root in agent["installation"]["roots"]:
                        if root["state"] == "present":
                            self.assertEqual(root["ownership_counts"], {"owned": 30})

    def test_doctor_without_perl_json_validates_the_fallback_document(self):
        for output, state in (('broken', 'unknown'), ('[]', 'unknown'),
                              ('{"state":"ready"}', 'ready')):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as raw:
                value = self._fixture_json_result(
                    Path(raw), "print(" + repr(output) + ")\n", without_perl_json=True)
                self.assertEqual(value["state"], state)

    def test_doctor_summary_stays_out_of_the_exec_argument_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            env = self._populated_env(directory)
            self._without_perl_json(directory, env)
            # Leave room for the supervisor's fixed code and ordinary command
            # arguments, but not another copy of the report. The assertions
            # exercise actual exec and doctor output, not this source text.
            supervisor = DOCTOR.read_text().split("SUPERVISOR_PERL='", 1)[1].split(
                "'\n\n# bounded SECONDS", 1)[0]
            environment_bytes = sum(len(os.fsencode(k)) + len(os.fsencode(v)) + 2
                                    for k, v in env.items())
            padding = (os.sysconf("SC_ARG_MAX") - len(supervisor.encode()) -
                       environment_bytes - 6000)
            self.assertGreater(padding, 0)
            for offset in range(0, padding, 60000):
                env["DOCTOR_FIXTURE_PADDING_" + str(offset)] = "x" * min(60000, padding - offset)
            control = subprocess.run([str(directory / "bin" / "python3"),
                                      str(SCRIPTS / "agent_diagnostics.py"), "--doctor-summary"],
                                     env=env, capture_output=True, text=True,
                                     timeout=WATCHDOG_SECONDS)
            self.assertEqual(control.returncode, 0, control.stderr)
            self.assertGreater(len(control.stdout.encode()), 6000)
            expected = json.loads(control.stdout)
            result = subprocess.run([str(DOCTOR), "--json"], env=env,
                                    capture_output=True, text=True, timeout=WATCHDOG_SECONDS)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertNotEqual(value.get("state"), "unknown", (value, result.stderr))
            self.assertNotIn("truncated", value)
            self.assertEqual(value["detail"], "summary")
            self.assertEqual(set(value["agents"]), {"claude", "codex", "opencode", "pi"})
            for name, agent in value["agents"].items():
                self.assertEqual(agent["installation"], expected["agents"][name]["installation"])
                self.assertEqual(agent["agent_present"]["state"], "executable_found")

    def test_doctor_summary_stdin_is_bounded_and_requires_the_whole_document(self):
        for size in (48000, 48001):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as raw:
                body = ("import json\n"
                        "value = {'padding': ''}\n"
                        "base = json.dumps(value, separators=(',', ':'))\n"
                        f"value['padding'] = 'x' * ({size} - len(base))\n"
                        "print(json.dumps(value, separators=(',', ':')))\n")
                value = self._fixture_json_result(Path(raw), body, without_perl_json=True)
                if size == 48000:
                    self.assertEqual(len(json.dumps(value, separators=(",", ":"))), size)
                    self.assertNotIn("truncated", value)
                else:
                    self.assertEqual(value["state"], "unknown")
        for output in ('noise\n{}', '{}\n{}', '{"text":"\u03bb"}'):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as raw:
                value = self._fixture_json_result(Path(raw), "print(" + repr(output) + ")\n",
                                                  without_perl_json=True)
                self.assertEqual(value["state"], "unknown")

    def test_doctor_summary_preserves_producer_failure_and_bounds_both_stages(self):
        for stage in ("producer-failed", "producer-stalled", "validator-stalled"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as raw:
                body = "print('{\"state\":\"ready\"}', flush=True)\n"
                validator_body = None
                deadline = DOCTOR_SECONDS
                if stage == "producer-failed":
                    body += "raise SystemExit(7)\n"
                else:
                    deadline = 1
                    if stage == "producer-stalled":
                        body += "import time; time.sleep(600)\n"
                    else:
                        validator_body = "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(600)\n"
                value = self._fixture_json_result(Path(raw), body, deadline=deadline,
                                                  without_perl_json=True,
                                                  validator_body=validator_body)
                self.assertEqual(value["state"], "unknown")
                self.assertNotIn("truncated", value)

    def test_doctor_json_result_boundary_and_stderr_are_independent(self):
        # Exact bytes include the newline. The payload also carries escaped
        # Unicode and newlines, which must survive the single-line protocol.
        for size in (262_144, 262_145):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as raw:
                body = (
                    "import json, sys\n"
                    "value = {'text': '\\u03bb\\n', 'padding': ''}\n"
                    "base = json.dumps(value, separators=(',', ':'))\n"
                    f"value['padding'] = 'x' * ({size} - len(base) - 1)\n"
                    "sys.stderr.write('diagnostic warning\\n' * 20000)\n"
                    "print(json.dumps(value, separators=(',', ':')))\n")
                value = self._fixture_json_result(Path(raw), body)
                if size == 262_144:
                    self.assertNotIn("truncated", value)
                    self.assertEqual(value["text"], "\u03bb\n")
                    self.assertEqual(len(json.dumps(value, separators=(",", ":"))) + 1, size)
                else:
                    self.assertTrue(value["truncated"])
                    self.assertEqual(value["state"], "unknown")
                    self.assertEqual(value["limit_bytes"], 262_144)

    def test_doctor_json_rejects_invalid_document_even_with_valid_final_line(self):
        for output in ('{"unfinished":', 'broken\n{}\n', '[1,2]', ''):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as raw:
                value = self._fixture_json_result(
                    Path(raw), "import sys\nsys.stdout.write(" + repr(output) + ")\n")
                self.assertEqual(value["state"], "unknown")
                self.assertNotIn("truncated", value)

    def test_doctor_json_does_not_accept_success_output_after_timeout_or_nonzero_exit(self):
        for end, deadline in (("raise SystemExit(7)", DOCTOR_SECONDS),
                              ("import time; time.sleep(600)", 1)):
            with self.subTest(end=end), tempfile.TemporaryDirectory() as raw:
                value = self._fixture_json_result(
                    Path(raw), "print('{\"state\":\"ready\"}', flush=True)\n" + end + "\n",
                    deadline=deadline)
                self.assertEqual(value["state"], "unknown")
                self.assertNotIn("truncated", value)

    def test_survey_preserves_existing_keys_and_adds_agent_diagnostics_without_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            env = self._probe_env(tmp)
            # Make the no-Claude case explicit without relying on this host.
            (tmp / "bin" / "claude").unlink()
            result = subprocess.run([sys.executable, str(SURVEY), "--repo", str(tmp), "--json"],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=WATCHDOG_SECONDS)
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
            # Do not preload discovery or disable bytecode here: either would
            # mask a missing read-only guard in the copied entrypoint. This
            # test accepts slow probes; only exit status and writes matter.
            env = {"HOME": str(tmp / "home"), "PATH": str(self._bin(tmp))}
            result = subprocess.run([sys.executable, str(copied / "agent_diagnostics.py"), "--json"],
                                    cwd=tmp, env=env, text=True, capture_output=True, timeout=WATCHDOG_SECONDS)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((copied / "__pycache__").exists())
