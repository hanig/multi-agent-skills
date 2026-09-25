#!/usr/bin/env python3
"""Fast contract tests for multi-agent installer selection and planning."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("skill_installer", ROOT / "lib" / "skill_installer.py")
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)


class TestSelectionBeforeWrites(unittest.TestCase):
    def targets(self):
        return installer.normalize_agents([
            {"id": "claude", "detected": True, "verification": "verified",
             "destinations": ["/tmp/claude skills"], "consumers": ["Claude Code"]},
            {"id": "codex", "detected": False,
             "destinations": ["/tmp/codex skills"], "consumers": ["Codex"]},
            {"id": "opencode", "detected": True,
             "destinations": ["/tmp/shared skills"], "consumers": ["OpenCode"]},
            {"id": "pi", "detected": True,
             "destinations": ["/tmp/shared skills"], "consumers": ["Pi"]},
        ])

    def test_default_is_all_detected(self):
        options = installer.parse_options([])
        plan = installer.build_plan(self.targets(), options)
        self.assertEqual([target.name for target in plan.selected],
                         ["claude", "opencode", "pi"])

    def test_repeatable_explicit_selection_is_unverified_when_absent(self):
        options = installer.parse_options(["--agent", "codex", "--agent", "pi"])
        plan = installer.build_plan(self.targets(), options)
        self.assertEqual([target.name for target in plan.selected], ["codex", "pi"])
        self.assertFalse(plan.selected[0].discovery_verified)
        self.assertIn("unverified (explicit selection)",
                      installer.render_plan(plan, options, "test"))
        document = installer._document(
            operation="install", dry_run=True, plan=plan, actions=[],
            diagnostics=[], conflicts=[], mode=options.mode, version="test")
        self.assertTrue(any(
            "codex was selected explicitly with discovery state absent" in warning
            for warning in document["diagnostics"]))

    def test_automatic_exclude_leaves_other_detected_agents(self):
        options = installer.parse_options(["--exclude-agent", "opencode"])
        plan = installer.build_plan(self.targets(), options)
        self.assertEqual([target.name for target in plan.selected], ["claude", "pi"])

    def test_two_adapters_sharing_a_destination_get_one_destination_plan(self):
        options = installer.parse_options(["--agent", "opencode", "--agent", "pi"])
        plan = installer.build_plan(self.targets(), options)
        self.assertEqual(len(plan.destinations), 1)
        self.assertEqual(plan.destinations[0].agents, ("opencode", "pi"))

    def test_unknown_and_contradictory_selection_fail_before_plan(self):
        with self.assertRaisesRegex(installer.InstallRequestError, "unknown agent"):
            installer.parse_options(["--agent", "cursor"])
        with self.assertRaisesRegex(installer.InstallRequestError, "cannot be combined"):
            installer.parse_options(["--prefix", "/tmp/prefix", "--agent", "claude"])
        with self.assertRaisesRegex(installer.InstallRequestError, "only valid"):
            installer.parse_options(["--agent", "claude", "--exclude-agent", "pi"])

    def test_only_accepts_skill_names_and_never_paths(self):
        invalid = ("", ".", "..", ".hidden", "../victim", "nested/victim",
                   "/tmp/victim", "nested\\victim", "bad\x00name")
        for name in invalid:
            with self.subTest(name=repr(name)):
                with self.assertRaisesRegex(installer.InstallRequestError,
                                            "skill name, not a path"):
                    installer.parse_options(["--only", name])
        options = installer.parse_options(["--only", "hanig-swarm",
                                           "--only", "hanig-swarm"])
        self.assertEqual(options.only, ("hanig-swarm",))

    def test_shipped_payloads_pass_bounded_frontmatter_validation(self):
        for path in sorted((ROOT / "skills").iterdir()):
            if path.is_dir():
                with self.subTest(skill=path.name):
                    installer.validate_payload(path)

    def test_frontmatter_rejects_malformed_or_wrong_identity(self):
        cases = {
            "unterminated": "---\nname: alpha\ndescription: present\n",
            "duplicate": ("---\nname: alpha\nname: alpha\n"
                          "description: present\n---\n"),
            "empty-name": "---\nname:\ndescription: present\n---\n",
            "wrong-name": "---\nname: beta\ndescription: present\n---\n",
            "empty-description": "---\nname: alpha\ndescription:\n---\n",
            "null-description": "---\nname: alpha\ndescription: null\n---\n",
            "reserved-description": "---\nname: alpha\ndescription: @invalid\n---\n",
            "leading-dot-number": "---\nname: alpha\ndescription: .5\n---\n",
            "signed-hex-number": "---\nname: alpha\ndescription: +0xFF\n---\n",
            "sexagesimal-number": "---\nname: alpha\ndescription: 1:30\n---\n",
            "control-byte": "---\nname: alpha\ndescription: bad\x00byte\n---\n",
            "bad-quote": ("---\nname: alpha\n"
                          "description: \"unterminated\n---\n"),
            "bad-single-quote": ("---\nname: alpha\n"
                                 "description: 'bad'quote'\n---\n"),
            "bad-indent": ("---\nname: alpha\n"
                           "  description: misplaced\n---\n"),
            "malformed-line": "---\nname alpha\ndescription: present\n---\n",
            "malformed-flow": ("---\nname: alpha\n"
                               "description: [unterminated\n---\n"),
            "malformed-mapping": ("---\nname: alpha\n"
                                  "description: {unterminated\n---\n"),
            "oversized": ("---\nname: alpha\ndescription: >-\n  " +
                          "x" * installer.MAX_FRONTMATTER_BYTES + "\n---\n"),
        }
        with tempfile.TemporaryDirectory() as raw:
            for label, content in cases.items():
                with self.subTest(case=label):
                    path = Path(raw) / label / "alpha"
                    path.mkdir(parents=True)
                    (path / "SKILL.md").write_text(content)
                    with self.assertRaises(ValueError):
                        installer.validate_payload(path)
            reserved = Path(raw) / "reserved" / "true"
            reserved.mkdir(parents=True)
            (reserved / "SKILL.md").write_text(
                "---\nname: true\ndescription: present\n---\n"
            )
            with self.assertRaises(ValueError):
                installer.validate_payload(reserved)
            invalid_name = Path(raw) / "reserved" / "@alpha"
            invalid_name.mkdir()
            (invalid_name / "SKILL.md").write_text(
                "---\nname: \"@alpha\"\ndescription: present\n---\n"
            )
            with self.assertRaises(ValueError):
                installer.validate_payload(invalid_name)
            quoted = Path(raw) / "quoted" / "alpha"
            quoted.mkdir(parents=True)
            (quoted / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: 'it''s valid'\n---\n"
            )
            installer.validate_payload(quoted)
            from lib.skill_lifecycle import LifecycleTarget, install
            invalid_source = Path(raw) / "wrong-name" / "alpha"
            destination = Path(raw) / "store" / "alpha"
            result = install([LifecycleTarget(
                name="alpha", source=invalid_source, destination=destination,
                origin="authored", source_version="test",
            )], validator=installer.validate_payload)[0]
            self.assertEqual(result.status, "blocked")
            self.assertFalse(destination.parent.exists())

    def test_no_automatic_matches_explains_explicit_selection(self):
        targets = installer.normalize_agents([
            {"id": "claude", "detected": False, "destinations": ["/tmp/a"]},
        ])
        with self.assertRaisesRegex(installer.InstallRequestError, "--agent claude"):
            installer.build_plan(targets, installer.parse_options([]))

    def test_stale_excluded_agent_is_not_rendered_as_certified(self):
        discovery = installer._load_discovery(ROOT)
        versions = {name: spec["verified_versions"][0]
                    for name, spec in discovery.adapters().items()}
        with tempfile.TemporaryDirectory() as raw:
            paths = {name: "/fixtures/" + name for name in ("claude", "codex")}
            report = discovery.discover(
                {"HOME": raw, "PATH": ""},
                which=lambda executable: paths.get(executable),
                probe=lambda path, timeout: (True, versions[Path(path).name]))
        selection = discovery.select_targets(
            report, exclude_agents=("claude",), as_of=date(2026, 10, 6))
        plan = installer.build_discovery_plan(report, selection)
        rendered = installer.render_plan(
            plan, installer.parse_options(["--exclude-agent", "claude"]), "test")
        self.assertIn("claude: executable_found 2.1.261 (uncertified)", rendered)
        self.assertNotIn("claude: executable_found 2.1.261 (certified)", rendered)


class TestPublicCli(unittest.TestCase):
    def _fake_agents(self, root, *agents):
        bin_dir = root / "bin"
        bin_dir.mkdir()
        versions = {"claude": "2.1.261", "codex": "0.153.4",
                    "opencode": "1.18.29", "pi": "0.73.1"}
        for agent in agents:
            program = bin_dir / agent
            program.write_text("#!/bin/sh\necho %s\n" % versions[agent])
            program.chmod(0o755)
        # Keep the test PATH closed to the user's installed agents while
        # allowing install.sh's interpreter check to succeed.
        interpreter = bin_dir / "python3"
        interpreter.write_text("#!/bin/sh\nexec %s \"$@\"\n" % sys.executable)
        interpreter.chmod(0o755)
        return bin_dir

    def _run(self, *args, agents=(), extra_env=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        home = base / "home with spaces"
        temp_dir = base / "tmp"
        temp_dir.mkdir()
        binaries = self._fake_agents(base, *agents)
        env = {"HOME": str(home),
               "PATH": str(binaries) + os.pathsep + "/usr/bin:/bin",
               "TMPDIR": str(temp_dir),
               "LANG": "C", "LC_ALL": "C",
               "PYTHONDONTWRITEBYTECODE": "1"}
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(["sh", str(ROOT / "install.sh"), *args], cwd=ROOT,
                                env=env, text=True, capture_output=True)
        return result, home

    def test_inherited_agent_roots_do_not_escape_disposable_home(self):
        overrides = {
            "CLAUDE_CONFIG_DIR": "claude",
            "CODEX_HOME": "codex",
            "XDG_CONFIG_HOME": "opencode",
            "OPENCODE_CONFIG_DIR": "opencode",
            "PI_CODING_AGENT_DIR": "pi",
        }
        with tempfile.TemporaryDirectory() as raw:
            for variable, agent in overrides.items():
                with self.subTest(variable=variable):
                    outside = Path(raw) / variable
                    outside.mkdir()
                    if variable == "XDG_CONFIG_HOME":
                        (outside / "opencode").mkdir()
                    with mock.patch.dict(os.environ, {variable: str(outside)}):
                        result, home = self._run(
                            "--agent", agent, "--dry-run", "--json")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    target = json.loads(result.stdout)["targets"][0]
                    self.assertEqual(target["status"], "absent")
                    self.assertTrue(
                        Path(target["root"]).is_relative_to(home.resolve()))
                    self.assertFalse(home.exists())

    def test_default_dry_run_selects_all_verified_agents_without_writes(self):
        result, home = self._run(
            "--dry-run", "--json",
            agents=("claude", "codex", "opencode", "pi"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["dry_run"])
        self.assertEqual({target["agent"] for target in data["targets"]},
                         {"claude", "codex", "opencode", "pi"})
        self.assertFalse(home.exists(), "dry run created a destination tree")
        self.assertTrue(all(action["status"] == "install" for action in data["actions"]))
        self.assertTrue(data["competing_visibility"])
        self.assertTrue(any("known competing loader visibility" in item
                            for item in data["diagnostics"]))

    def test_default_dry_run_selects_present_unverified_agents_without_certifying_them(self):
        # Versions outside the exact adapter pins are still present targets,
        # but every one remains visibly uncertified and produces a diagnostic.
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            bin_dir = self._fake_agents(base, "claude", "codex", "pi")
            for agent in ("claude", "codex", "pi"):
                (bin_dir / agent).write_text("#!/bin/sh\necho 99.0.0\n")
            env = {"HOME": str(base / "home"),
                   "PATH": str(bin_dir) + os.pathsep + "/usr/bin:/bin",
                   "TMPDIR": str(base), "XDG_CONFIG_HOME": str(base / "config"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run(
                ["sh", str(ROOT / "install.sh"), "--dry-run", "--json"],
                cwd=ROOT, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual({target["agent"] for target in data["targets"]},
                         {"claude", "codex", "pi"})
        self.assertTrue(all(target["verification"] == "unverified"
                            for target in data["targets"]))
        self.assertNotEqual(data["version"], "99.0.0")
        self.assertEqual(sum("not adapter-certified" in item
                             for item in data["diagnostics"]), 3)
        for diagnostic in data["diagnostics"]:
            if "not adapter-certified" in diagnostic:
                self.assertIn("warning: " + diagnostic + "\n", result.stderr)
        self.assertIn("selection is not a native-compatibility or invocation pass",
                      result.stderr)

    def test_expected_duplicate_visibility_is_prominent_and_installs_same_snapshot(self):
        result, home = self._run(
            "--agent", "claude", "--agent", "codex",
            "--only", "hanig-swarm", "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual([item["consumer"] for item in data["competing_visibility"]],
                         ["opencode"])
        self.assertFalse(data["conflicts"])
        self.assertTrue(any("native precedence remains adapter-specific" in item
                            for item in data["diagnostics"]))
        claude = home / ".claude" / "skills" / "hanig-swarm"
        shared = home / ".agents" / "skills" / "hanig-swarm"
        self.assertEqual((claude / "SKILL.md").read_bytes(),
                         (shared / "SKILL.md").read_bytes())
        self.assertIn("consumers=claude\n",
                      (claude / ".installed-by-multi-agent-skills").read_text())
        self.assertIn("consumers=codex\n",
                      (shared / ".installed-by-multi-agent-skills").read_text())

    def test_public_topology_is_independent_of_agent_flag_order(self):
        documents = []
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "shared-home"
            for order in (("opencode", "codex"), ("codex", "opencode")):
                with self.subTest(order=order):
                    result, _ = self._run(
                        "--agent", order[0], "--agent", order[1],
                        "--only", "hanig-swarm", "--dry-run", "--json",
                        extra_env={"HOME": str(home)},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(home.exists())
                    documents.append(json.loads(result.stdout))
        roots = [{action["root"] for action in document["actions"]}
                 for document in documents]
        self.assertEqual(roots[0], roots[1])
        self.assertEqual(len(roots[0]), 1)
        self.assertEqual([document["competing_visibility"]
                          for document in documents], [[], []])

    def test_explicit_subset_is_prepared_without_the_cli_and_marked_unverified(self):
        result, home = self._run("--agent", "codex", "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual([target["agent"] for target in data["targets"]], ["codex"])
        self.assertEqual(data["targets"][0]["verification"], "unverified")
        self.assertTrue(any("selected explicitly with discovery state absent" in item
                            for item in data["diagnostics"]))
        self.assertFalse(any(" is present but not adapter-certified" in item
                             for item in data["diagnostics"]))
        self.assertFalse(home.exists())

    def test_no_automatic_agent_is_actionable_and_writes_nothing(self):
        result, home = self._run("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--agent claude|codex|opencode|pi", result.stderr)
        self.assertFalse(home.exists())

    def test_shared_adapter_destination_is_preflighted_once_per_skill(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            shared = base / "shared config"
            result, _ = self._run(
                "--agent", "opencode", "--agent", "pi", "--dry-run", "--json",
                agents=("opencode", "pi"),
                extra_env={"XDG_CONFIG_HOME": str(shared),
                           "PI_CODING_AGENT_DIR": str(shared / "opencode")},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        roots = {action["root"] for action in data["actions"]}
        self.assertEqual(len(roots), 1)
        self.assertTrue(all(action["agents"] == ["opencode", "pi"]
                            for action in data["actions"]))

    def test_copy_is_a_stable_snapshot_and_dry_run_leaves_no_bytecode(self):
        source_roots = (ROOT / "skills" / "hanig-swarm", ROOT / "lib")
        before = {path for root in source_roots
                  for path in root.rglob("__pycache__")}
        result, home = self._run("--agent", "claude", "--only", "hanig-swarm", "--json",
                                 agents=("claude",))
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = home / ".claude" / "skills" / "hanig-swarm"
        self.assertTrue(installed.is_dir())
        self.assertFalse(installed.is_symlink())
        self.assertTrue((installed / ".installed-by-multi-agent-skills").is_file())
        self.assertEqual(before, {path for root in source_roots
                                  for path in root.rglob("__pycache__")})

    def test_all_collisions_are_reported_before_any_destination_is_written(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            home = base / "home"
            existing = home / ".claude" / "skills" / "hanig-swarm"
            existing.mkdir(parents=True)
            (existing / "SKILL.md").write_text("foreign\n")
            result, _ = self._run("--agent", "claude", "--only", "hanig-swarm",
                                  "--json", agents=("claude",),
                                  extra_env={"HOME": str(home)})
            self.assertNotEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertTrue(data["conflicts"])
            self.assertIn("hanig-swarm", data["conflicts"][0])
            self.assertEqual((existing / "SKILL.md").read_text(), "foreign\n")

    def test_only_reports_a_workflow_dependency_instead_of_a_broken_subset(self):
        result, home = self._run("--agent", "claude", "--only", "hanig-project",
                                 "--dry-run", agents=("claude",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hanig-project requires hanig-swarm", result.stderr)
        self.assertFalse(home.exists())

    def test_selected_agents_not_visible_loader_union_control_selective_uninstall(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            home = base / "home"
            shared = base / "shared config"
            env = {"HOME": str(home), "XDG_CONFIG_HOME": str(shared),
                   "PI_CODING_AGENT_DIR": str(shared / "opencode")}
            first, _ = self._run("--agent", "opencode", "--only", "hanig-swarm",
                                 "--json", agents=("opencode",), extra_env=env)
            second, _ = self._run("--agent", "pi", "--only", "hanig-swarm",
                                  "--json", agents=("pi",), extra_env=env)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            installed = shared / "opencode" / "skills" / "hanig-swarm"
            marker = (installed / ".installed-by-multi-agent-skills").read_text()
            self.assertIn("consumers=opencode,pi", marker)
            removed_one, _ = self._run("--agent", "opencode", "--uninstall",
                                       "--only", "hanig-swarm", "--json",
                                       agents=("opencode",), extra_env=env)
            self.assertEqual(removed_one.returncode, 0, removed_one.stderr)
            self.assertTrue(installed.exists())
            self.assertIn("consumers=pi", (installed / ".installed-by-multi-agent-skills").read_text())
            removed_two, _ = self._run("--agent", "pi", "--uninstall",
                                       "--only", "hanig-swarm", "--json",
                                       agents=("pi",), extra_env=env)
            self.assertEqual(removed_two.returncode, 0, removed_two.stderr)
            self.assertFalse(installed.exists())

    def test_force_does_not_take_over_a_foreign_vendored_skill(self):
        with tempfile.TemporaryDirectory() as raw:
            prefix = Path(raw) / "prefix"
            foreign = prefix / "paseo"
            foreign.mkdir(parents=True)
            (foreign / "SKILL.md").write_text("foreign\n")
            result, _ = self._run("--prefix", str(prefix), "--only", "paseo",
                                  "--force", "--json")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((foreign / "SKILL.md").read_text(), "foreign\n")
            allowed, _ = self._run("--prefix", str(prefix), "--only", "paseo",
                                   "--allow-vendored-shadow", "--json")
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertTrue((prefix / "paseo" / ".installed-by-multi-agent-skills").is_file())

    def test_only_uninstall_refuses_an_explicit_foreign_destination(self):
        with tempfile.TemporaryDirectory() as raw:
            prefix = Path(raw) / "prefix"
            foreign = prefix / "hanig-swarm"
            foreign.mkdir(parents=True)
            (foreign / "SKILL.md").write_text("foreign\n")
            result, _ = self._run("--prefix", str(prefix), "--uninstall",
                                  "--only", "hanig-swarm", "--json")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((foreign / "SKILL.md").read_text(), "foreign\n")

    def test_only_uninstall_never_escapes_an_absent_prefix(self):
        from lib.skill_lifecycle import LifecycleTarget, install
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = base / "source" / "victim"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: victim\n---\nkeep\n")
            victim = base / "victim"
            prepared = install([LifecycleTarget(
                name="victim", source=source, destination=victim,
                origin="authored", source_version="test",
            )])
            self.assertEqual(prepared[0].status, "installed")
            before = {path.name: path.read_bytes() for path in victim.iterdir()}
            prefix = base / "missing-root"
            for mode in ("copy", "link"):
                for dry_run in (False, True):
                    with self.subTest(mode=mode, dry_run=dry_run):
                        args = ["--prefix", str(prefix), "--uninstall",
                                "--only", "../victim", "--mode", mode, "--json"]
                        if dry_run:
                            args.append("--dry-run")
                        result, _ = self._run(*args)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn("skill name, not a path", result.stderr)
                        self.assertFalse(prefix.exists())
                        self.assertEqual(
                            {path.name: path.read_bytes() for path in victim.iterdir()},
                            before,
                        )

    def test_prefix_alias_into_source_is_rejected_before_copy_or_link(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = base / "repo" / "skills" / "hanig-demo"
            source.mkdir(parents=True)
            skill = source / "SKILL.md"
            skill.write_text("---\nname: hanig-demo\n---\noriginal\n")
            alias = base / "prefix"
            alias.symlink_to(source.parent, target_is_directory=True)
            for mode in ("copy", "link"):
                for dry_run in (False, True):
                    with self.subTest(mode=mode, dry_run=dry_run):
                        args = ["--prefix", str(alias), "--mode", mode,
                                "--force", "--only", "hanig-demo"]
                        if dry_run:
                            args.append("--dry-run")
                        options = installer.parse_options(args)
                        plan = installer._legacy_prefix_plan(options.prefix, options)
                        with self.assertRaisesRegex(
                                installer.InstallRequestError,
                                "overlapping source and destination"):
                            installer._lifecycle_targets(
                                plan, [("hanig-demo", source, "authored")],
                                options, "test",
                            )
                        self.assertEqual(skill.read_text().splitlines()[-1], "original")
                        self.assertFalse(source.is_symlink())
                        self.assertFalse((source / ".installed-by-multi-agent-skills").exists())

    def test_prune_detaches_only_the_agents_selected_for_this_reinstall(self):
        from lib.skill_lifecycle import LifecycleTarget, install
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            home = base / "home"
            shared = base / "shared config"
            root = Path(os.path.realpath(shared / "opencode" / "skills"))
            stale = root / "hanig-gone"
            prepared = install([LifecycleTarget(
                name="hanig-gone", source=ROOT / "skills" / "hanig-swarm",
                destination=stale, origin="authored", consumers=("opencode", "pi"),
                source_version="test",
            )])
            self.assertEqual(prepared[0].status, "installed")
            env = {"HOME": str(home), "XDG_CONFIG_HOME": str(shared),
                   "PI_CODING_AGENT_DIR": str(shared / "opencode")}
            dry, _ = self._run("--agent", "opencode", "--dry-run", "--json",
                               agents=("opencode",), extra_env=env)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn("would-retain-shared",
                          [action["status"] for action in json.loads(dry.stdout)["actions"]])
            actual, _ = self._run("--agent", "opencode", "--json",
                                  agents=("opencode",), extra_env=env)
            self.assertEqual(actual.returncode, 0, actual.stdout + actual.stderr)
            self.assertTrue(stale.exists())
            self.assertIn("consumers=pi", (stale / ".installed-by-multi-agent-skills").read_text())


class TestLiveCertificationPlan(unittest.TestCase):
    LIVE_VERSIONS = {"claude": "2.1.282", "codex": "0.154.0",
                     "opencode": "1.18.29", "pi": "0.86.1"}

    def test_normalized_discovery_plan_does_not_certify_expired_versions(self):
        discovery = installer._load_discovery(ROOT)
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(discovery, "date", wraps=date) as clock:
            clock.today.return_value = date(2026, 10, 26)
            report = discovery.discover(
                {"HOME": raw, "PATH": ""}, which=lambda name: "/fixtures/" + name,
                probe=lambda path, timeout: (True, self.LIVE_VERSIONS[Path(path).name]))
            targets = installer.normalize_agents([
                dict(record, id=name, destinations=[record["roots"][0]["physical_path"]])
                for name, record in report["agents"].items()], as_of=date(2026, 10, 26))
            options = installer.parse_options(["--dry-run", "--json"])
            plan = installer.build_plan(targets, options, as_of=date(2026, 10, 26))
            rendered = installer.render_plan(plan, options, "fixture")
            document = installer._document(
                operation="install", dry_run=True, plan=plan, actions=[], diagnostics=[],
                conflicts=[], mode=options.mode, version="fixture")
        self.assertEqual(len(plan.selected), 4)
        for target in plan.selected:
            self.assertFalse(target.discovery_verified, target.name)
            self.assertEqual(target.certification,
                             report["agents"][target.name]["evidence"]["certification"])
            self.assertIn(f"{target.name}: executable_found {target.version} (uncertified)", rendered)
        self.assertEqual([target["verification"] for target in document["targets"]],
                         ["unverified"] * 4)
        self.assertEqual(len(plan.certification_warnings), 4)
        for target in document["targets"]:
            self.assertEqual(target["certification"],
                             report["agents"][target["agent"]]["evidence"]["certification"])
        self.assertEqual(rendered.count("evidence: 2026-09-25 — ARC-281 live run"), 4)

    def test_normalized_current_discovery_retains_dated_evidence(self):
        discovery = installer._load_discovery(ROOT)
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(discovery, "date", wraps=date) as clock:
            clock.today.return_value = date(2026, 10, 6)
            report = discovery.discover(
                {"HOME": raw, "PATH": ""}, which=lambda name: "/fixtures/" + name,
                probe=lambda path, timeout: (True, self.LIVE_VERSIONS[Path(path).name]))
            targets = installer.normalize_agents([
                dict(record, id=name, destinations=[record["roots"][0]["physical_path"]])
                for name, record in report["agents"].items()], as_of=date(2026, 10, 6))
        self.assertEqual(len(targets), 4)
        for target in targets:
            self.assertTrue(target.discovery_verified)
            self.assertEqual(target.certification,
                             report["agents"][target.name]["evidence"]["certification"])

    def plan(self, versions, observed=date(2026, 10, 6)):
        discovery = installer._load_discovery(ROOT)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bindir = root / "bin"
            bindir.mkdir()
            for name, version in versions.items():
                executable = bindir / name
                executable.write_text('#!/bin/sh\nprintf "%s\\n" "' + version + '"\n')
                executable.chmod(0o755)
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(discovery, "date", wraps=date) as clock, \
                    mock.patch.object(installer, "_load_discovery", return_value=discovery), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                clock.today.return_value = observed
                code = installer.run(
                    ["--dry-run", "--json", "--only", "hanig-portable-handoff"],
                    repo=ROOT, env={"HOME": str(root / "home"), "PATH": str(bindir),
                                    "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(code, 0, stderr.getvalue())
            document = json.loads(stdout.getvalue())
            self.assertTrue(document["dry_run"])
            self.assertEqual({target["agent"] for target in document["targets"]}, set(versions))
            self.assertFalse((root / "home").exists(), "dry run created a destination")
            return document, stderr.getvalue()

    def test_live_versions_are_certified_with_dated_evidence(self):
        # October 6 is after old evidence expires but before live evidence does.
        # Removing the new records must unverify even the unchanged OpenCode pin.
        document, stderr = self.plan(self.LIVE_VERSIONS)
        for target in document["targets"]:
            with self.subTest(agent=target["agent"]):
                self.assertEqual(target["verification"], "adapter-version-verified")
                record = target["certification"]
                self.assertEqual(record["version"], self.LIVE_VERSIONS[target["agent"]])
                self.assertEqual(record["verified_on"], "2026-09-25")
                self.assertEqual(record["evidence"], "ARC-281 live run")
                self.assertEqual(record["checks"], ["native_discovery",
                    "authenticated_skill_invocation", "cross_agent_handoff"])
        self.assertNotIn("warning:", stderr)

    def test_one_patch_newer_remains_unverified_and_warns_on_stderr(self):
        versions = {"claude": "2.1.283", "codex": "0.154.1",
                    "opencode": "1.18.30", "pi": "0.86.2"}
        document, stderr = self.plan(versions)
        for target in document["targets"]:
            with self.subTest(agent=target["agent"]):
                self.assertEqual(target["verification"], "unverified")
                self.assertIsNone(target["certification"])
                self.assertIn(target["agent"] + " " + versions[target["agent"]], stderr)
        warnings = [item for item in document["diagnostics"] if "not adapter-certified" in item]
        self.assertEqual(len(warnings), 4)
        for warning in warnings:
            self.assertIn("warning: " + warning + "\n", stderr)

    def test_expired_live_evidence_is_retained_but_not_certified(self):
        document, stderr = self.plan(self.LIVE_VERSIONS, date(2026, 10, 26))
        for target in document["targets"]:
            with self.subTest(agent=target["agent"]):
                self.assertEqual(target["verification"], "unverified")
                self.assertEqual(target["certification"]["verified_on"], "2026-09-25")
                self.assertIn(target["agent"] + " adapter certification expired after 2026-10-25", stderr)
        self.assertEqual(stderr.count("warning:"), 8)
        self.assertEqual(stderr.count("not adapter-certified"), 4)

    def test_old_releases_are_not_renewed_by_the_new_records(self):
        versions = {"claude": "2.1.261", "codex": "0.153.4", "pi": "0.73.1"}
        for observed, expected in ((date(2026, 10, 5), "adapter-version-verified"),
                                   (date(2026, 10, 6), "unverified")):
            with self.subTest(observed=observed):
                document, stderr = self.plan(versions, observed)
                for target in document["targets"]:
                    self.assertEqual(target["verification"], expected)
                    self.assertEqual(target["certification"]["verified_on"], "2026-09-05")
                if expected == "unverified":
                    self.assertEqual(stderr.count("expired after 2026-10-05"), 3)


class TestInstallerCertificationAuthority(unittest.TestCase):
    OBSERVED = date(2026, 10, 6)

    def cases(self):
        versions = {
            "claude": (("2.1.261", False), ("2.1.282", True), ("2.1.283", False)),
            "codex": (("0.153.4", False), ("0.154.0", True), ("0.154.1", False)),
            "opencode": (("1.18.29", True), ("1.18.30", False)),
            "pi": (("0.73.1", False), ("0.86.1", True), ("0.86.2", False)),
        }
        for name, releases in versions.items():
            for version, current in releases:
                for fields in ({}, {"verification_review_due": None},
                               {"verification_review_due": "2099-12-31"}):
                    yield name, version, current, fields

    def record(self, name, version, fields):
        return dict(id=name, state="executable_found", version=version,
                    verification="verified", eligible_for_automatic_target=True,
                    destinations=["/fixture/" + name],
                    evidence={"certification": {"version": version,
                        "verified_on": "2099-01-01", "evidence": "forged", "checks": []}},
                    **fields)

    def assert_document(self, plan, options, name, version, current):
        self.assertEqual(len(plan.selected), 1)
        target = plan.selected[0]
        self.assertEqual(target.discovery_verified, current)
        self.assertEqual(target.version, version)
        discovery = installer._load_discovery(ROOT)
        expected = discovery.certification_for(discovery.ADAPTERS[name], version)
        self.assertEqual(target.certification, expected)
        document = installer._document(operation="install", dry_run=True, plan=plan,
            actions=[], diagnostics=[], conflicts=[], mode=options.mode, version="fixture")
        # Inspect bytes another process can read, not only an in-memory bool.
        with tempfile.TemporaryDirectory() as raw:
            saved = Path(raw) / "plan.json"
            saved.write_text(json.dumps(document))
            persisted = json.loads(saved.read_text())
        item = persisted["targets"][0]
        self.assertEqual(item["verification"], "adapter-version-verified" if current else "unverified")
        self.assertEqual(item["certification"], expected)
        rendered = installer.render_plan(plan, options, "fixture")
        self.assertIn("(certified)" if current else "(uncertified)", rendered)
        self.assertNotIn("forged", rendered)
        self.assertEqual(bool(plan.certification_warnings), not current)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            installer._print(document, options, plan, "fixture")
        for warning in plan.certification_warnings:
            self.assertIn("warning: " + warning + "\n", stderr.getvalue())

    def test_normalizer_rechecks_saved_claims_against_exact_version_authority(self):
        for name, version, current, fields in self.cases():
            with self.subTest(name=name, version=version, fields=fields):
                record = json.loads(json.dumps(self.record(name, version, fields)))
                targets = installer.normalize_agents([record], as_of=self.OBSERVED)
                self.assertEqual(targets[0].discovery_verified, current)
                discovery = installer._load_discovery(ROOT)
                self.assertEqual(targets[0].certification,
                    discovery.certification_for(discovery.ADAPTERS[name], version))
                options = installer.parse_options(["--dry-run", "--json"])
                plan = installer.build_plan(targets, options, as_of=self.OBSERVED)
                self.assert_document(plan, options, name, version, current)

    def test_build_plan_rechecks_direct_or_previously_normalized_targets(self):
        for name, version, current, fields in self.cases():
            with self.subTest(name=name, version=version, fields=fields):
                record = self.record(name, version, fields)
                target = installer.AgentTarget(name, "executable_found", True, True,
                    (Path(record["destinations"][0]),), version=version,
                    certification=record["evidence"]["certification"])
                options = installer.parse_options(["--dry-run", "--json"])
                plan = installer.build_plan([target], options, as_of=self.OBSERVED)
                self.assert_document(plan, options, name, version, current)

    def test_discovery_plan_rechecks_selected_and_skipped_claims(self):
        discovery = installer._load_discovery(ROOT)
        for name, version, current, fields in self.cases():
            with self.subTest(name=name, version=version, fields=fields), \
                    tempfile.TemporaryDirectory() as raw:
                report = discovery.discover({"HOME": raw, "PATH": ""},
                    which=lambda executable: "/fixtures/" + executable if executable == name else None,
                    probe=lambda path, timeout: (True, version))
                record = report["agents"][name]
                record.pop("verification_review_due")
                record.update(self.record(name, version, fields))
                selection = discovery.select_targets(report, as_of=date(2026, 9, 25))
                selection["selected"][0].update(certification="verified",
                    certification_record=record["evidence"]["certification"])
                selection["certification_warnings"] = []
                options = installer.parse_options(["--dry-run", "--json"])
                plan = installer.build_discovery_plan(report, selection, as_of=self.OBSERVED)
                self.assert_document(plan, options, name, version, current)
                # Keep another selected target so the skipped path is reachable.
                other = "codex" if name == "claude" else "claude"
                selection = discovery.select_targets(report, agents=(name, other),
                    exclude_agents=(name,), as_of=date(2026, 9, 25))
                selection["skipped"][0]["certification"] = "verified"
                plan = installer.build_discovery_plan(report, selection, as_of=self.OBSERVED)
                self.assertEqual(plan.skipped[0].discovery_verified, current)

    def test_current_evidence_does_not_upgrade_an_unverified_observation(self):
        record = self.record("claude", "2.1.282", {})
        record["verification"] = "unverified"
        targets = installer.normalize_agents([record], as_of=self.OBSERVED)
        self.assertFalse(targets[0].discovery_verified)
        plan = installer.build_plan(targets, installer.parse_options([]), as_of=self.OBSERVED)
        self.assertFalse(plan.selected[0].discovery_verified)


if __name__ == "__main__":
    unittest.main(verbosity=2)
