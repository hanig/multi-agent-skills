#!/usr/bin/env python3
"""Fast contract tests for multi-agent installer selection and planning."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
            "bad-quote": ("---\nname: alpha\n"
                          "description: \"unterminated\n---\n"),
            "bad-indent": ("---\nname: alpha\n"
                           "  description: misplaced\n---\n"),
            "malformed-line": "---\nname alpha\ndescription: present\n---\n",
            "malformed-flow": ("---\nname: alpha\n"
                               "description: [unterminated\n---\n"),
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
        binaries = self._fake_agents(base, *agents)
        env = dict(os.environ, HOME=str(home),
                   PATH=str(binaries) + os.pathsep + "/usr/bin:/bin",
                   PYTHONDONTWRITEBYTECODE="1")
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(["sh", str(ROOT / "install.sh"), *args], cwd=ROOT,
                                env=env, text=True, capture_output=True)
        return result, home

    def test_default_dry_run_selects_all_verified_agents_without_writes(self):
        blocked, blocked_home = self._run(
            "--dry-run", "--json",
            agents=("claude", "codex", "opencode", "pi"),
        )
        self.assertEqual(blocked.returncode, 1, blocked.stderr)
        blocked_data = json.loads(blocked.stdout)
        self.assertTrue(blocked_data["competing_visibility"])
        self.assertTrue(any("--allow-duplicate-visibility" in item
                            for item in blocked_data["conflicts"]))
        self.assertFalse(blocked_home.exists())
        result, home = self._run(
            "--dry-run", "--json", "--allow-duplicate-visibility",
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
        self.assertTrue(any("duplicate loader visibility explicitly allowed" in item
                            for item in data["diagnostics"]))

    def test_duplicate_visibility_blocks_live_writes_without_acknowledgment(self):
        result, home = self._run(
            "--agent", "claude", "--agent", "codex",
            "--only", "hanig-swarm", "--json",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual([item["consumer"] for item in data["competing_visibility"]],
                         ["opencode"])
        self.assertTrue(data["conflicts"])
        self.assertFalse(home.exists(), "known duplicate visibility wrote a target")

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
        before = set(ROOT.rglob("__pycache__"))
        result, home = self._run("--agent", "claude", "--only", "hanig-swarm", "--json",
                                 agents=("claude",))
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = home / ".claude" / "skills" / "hanig-swarm"
        self.assertTrue(installed.is_dir())
        self.assertFalse(installed.is_symlink())
        self.assertTrue((installed / ".installed-by-multi-agent-skills").is_file())
        self.assertEqual(before, set(ROOT.rglob("__pycache__")))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
