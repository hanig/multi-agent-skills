#!/usr/bin/env python3
"""Hermetic acceptance seam for portable agent skill installation.

These tests deliberately exercise discovery, selection, and lifecycle without
a real agent binary, account, or user configuration directory.  Native loader
proof belongs in docs/cross-agent-acceptance.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_PATH = ROOT / "skills" / "hanig-project" / "scripts" / "agent_discovery.py"
SPEC = importlib.util.spec_from_file_location("agent_discovery_acceptance",
                                              DISCOVERY_PATH)
discovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = discovery
SPEC.loader.exec_module(discovery)
sys.path.insert(0, str(ROOT))
from lib import skill_lifecycle as lifecycle  # noqa: E402

AGENTS = ("claude", "codex", "opencode", "pi")


class HermeticRoots(unittest.TestCase):
    """Every path is rooted below a temporary home/config tree.

    In particular, inherited agent override variables are removed.  An Orca
    session can set OPENCODE_CONFIG_DIR itself; allowing it to leak into this
    test would turn a supposedly isolated check into a probe of user state.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cross-agent-accept-")
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.config = self.base / "config"
        self.home.mkdir()
        self.config.mkdir()
        self.addCleanup(self.temp.cleanup)

    def env(self, **extra):
        result = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": str(self.base / "empty-bin"),
        }
        result.update(extra)
        return result

    def assert_confined(self, report):
        permitted = (self.base.resolve(),)
        for agent in report["agents"].values():
            for root in agent["roots"]:
                path = Path(root["physical_path"])
                self.assertTrue(any(path.is_relative_to(base) for base in permitted),
                                f"unconfined root: {path}")
                self.assertFalse(root["exists"], f"test unexpectedly created {path}")

    def report(self, available=(), **env):
        paths = {name: str(self.base / "fake-bin" / name) for name in available}

        def which(name):
            return paths.get(name)

        def probe(path, timeout):
            name = Path(path).name
            versions = {"claude": "2.1.261", "codex": "0.153.4",
                        "opencode": "1.18.29", "pi": "0.73.1"}
            return True, versions[name]

        report = discovery.discover(self.env(**env), which=which, probe=probe)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(tuple(report["agents"]), AGENTS)
        self.assert_confined(report)
        return report

    def test_one_agent_automatic_selection_is_hermetic(self):
        report = self.report(("codex",))
        selected = discovery.select_target(report)
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["mode"], "automatic")
        self.assertEqual(selected["agent"], "codex")
        self.assertTrue(selected["destination"]["physical_path"].startswith(
            str(self.base.resolve())))

    def test_all_agents_are_selected_without_duplicate_shared_writes(self):
        report = self.report(AGENTS)
        plan = discovery.select_targets(report)
        self.assertEqual([item["agent"] for item in plan["selected"]], list(AGENTS))
        self.assertEqual(plan["selected"][2]["covered_by"], ["claude"])
        self.assertEqual(plan["selected"][3]["covered_by"], ["codex"])
        # The compatibility helper refuses to pick one of two physical writes
        # by accident, but automatic multi-target installation uses the plan.
        selected = discovery.select_target(report)
        self.assertEqual(selected["status"], "requires_explicit_target")
        self.assertEqual(selected["eligible_agents"], ["claude", "codex"])

    def test_no_agent_requires_an_explicit_target(self):
        report = self.report()
        selected = discovery.select_target(report)
        self.assertEqual(selected["status"], "requires_explicit_target")
        self.assertEqual(selected["eligible_agents"], [])
        self.assertEqual(report["agents"]["claude"]["state"], "absent")

    def test_explicit_absent_target_is_a_safe_bootstrap_route(self):
        report = self.report()
        selected = discovery.select_target(report, "pi")
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["mode"], "explicit")
        self.assertEqual(selected["agent"], "pi")
        self.assertEqual(report["agents"]["pi"]["verification"], "unverified")

    def test_custom_paths_do_not_escape_the_disposable_roots(self):
        custom = self.base / "custom-agent-root"
        report = self.report(
            ("claude", "opencode", "pi"),
            CLAUDE_CONFIG_DIR=str(custom / "claude"),
            OPENCODE_CONFIG_DIR=str(custom / "opencode"),
            PI_CODING_AGENT_DIR=str(custom / "pi"),
        )
        self.assertEqual(
            report["agents"]["claude"]["roots"][0]["logical_path"],
            str(custom / "claude" / "skills"),
        )
        self.assertEqual(
            report["agents"]["opencode"]["roots"][0]["logical_path"],
            str(self.config / "opencode" / "skills"),
        )
        self.assertIn(str(custom / "opencode" / "skills"),
                      [root["logical_path"] for root in report["agents"]["opencode"]["roots"]])
        self.assertEqual(
            report["agents"]["pi"]["roots"][0]["logical_path"],
            str(custom / "pi" / "skills"),
        )

    def test_shared_root_overlap_names_all_visible_consumers(self):
        shared = self.base / "shared"
        report = self.report(
            ("claude", "pi"),
            CLAUDE_CONFIG_DIR=str(shared),
            PI_CODING_AGENT_DIR=str(shared),
        )
        destinations = [entry for entry in report["destinations"]
                        if Path(entry["physical_path"]).resolve()
                        == (shared / "skills").resolve()]
        self.assertEqual(len(destinations), 1)
        self.assertEqual(destinations[0]["root_ids"], ["claude-user", "pi-user"])
        self.assertEqual(destinations[0]["consumers"], ["claude", "opencode", "pi"])


class LifecycleAcceptance(unittest.TestCase):
    """Lifecycle coverage uses real modules but never a native agent loader.

    The source used here is a disposable checkout copy, not this worktree.  A
    passing copy-mode workflow after that copy is removed catches accidental
    links back into the checkout and proves the installed payload is runnable
    from a separate project cwd.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cross-agent-lifecycle-")
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.project = self.base / "separate-project"
        self.home.mkdir()
        self.project.mkdir()
        self.addCleanup(self.temp.cleanup)

    def source(self, name="hanig-verified-workflow"):
        source = self.base / "source-copy" / name
        shutil.copytree(ROOT / "skills" / name, source)
        return source

    def target(self, source, agent, *, destination=None, consumers=(), version="acceptance"):
        root = destination or self.home / ".agents" / "skills"
        return lifecycle.LifecycleTarget(
            name=source.name,
            source=source,
            destination=root / source.name if destination is None else destination,
            origin="authored",
            consumers=consumers or (agent,),
            source_version=version,
        )

    def test_copy_installs_all_four_and_runs_without_source_or_claude_tree(self):
        source = self.source()
        roots = {
            "claude": self.home / ".claude" / "skills",
            "codex": self.home / ".agents" / "skills",
            "opencode": self.home / ".config" / "opencode" / "skills",
            "pi": self.home / ".pi" / "agent" / "skills",
        }
        targets = [self.target(source, agent, destination=roots[agent] / source.name,
                               consumers=(agent,)) for agent in AGENTS]
        installed = lifecycle.install(targets)
        self.assertEqual([item.status for item in installed], ["installed"] * 4)
        for target in targets:
            record = lifecycle.read_provenance(target.destination)
            self.assertEqual(record.mode, "copy")
            self.assertEqual(record.consumers, target.consumers)
            self.assertFalse(target.destination.is_symlink())
            self.assertEqual(target.destination.stat().st_mode & 0o022, 0,
                             f"payload must not be group/world writable: {target.destination}")

        # Test a non-Claude payload on its own so a Claude tree cannot
        # accidentally satisfy the workflow lookup.
        self.assertTrue((roots["codex"] / source.name).is_dir())
        shutil.rmtree(source.parent)
        shutil.rmtree(roots["claude"].parent)
        self.assertFalse(source.exists())
        self.assertFalse((self.home / ".claude").exists())
        script = roots["codex"] / "hanig-verified-workflow" / "scripts" / "contract.py"
        run_dir, output = self.project / "run", self.project / "result.tsv"
        init = subprocess.run(
            [sys.executable, str(script), "init", str(run_dir), "--command", "/bin/true",
             "--output", str(output)], cwd=self.project, capture_output=True, text=True)
        self.assertEqual(init.returncode, 0, init.stderr)
        output.write_text("value\n1\n")
        record = subprocess.run([sys.executable, str(script), "record", str(run_dir),
                                 "--exit-code", "0"], cwd=self.project,
                                capture_output=True, text=True)
        self.assertEqual(record.returncode, 0, record.stderr)
        checked = subprocess.run([sys.executable, str(script), "check", str(run_dir), "--json"],
                                 cwd=self.project, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr + checked.stdout)
        self.assertEqual(json.loads(checked.stdout)["state"], "SCIENTIFIC_PASS")

    def test_foreign_conflict_upgrade_and_failure_recovery_are_honest(self):
        source = self.source()
        destination = self.home / ".agents" / "skills" / source.name
        destination.mkdir(parents=True)
        foreign = destination / "SKILL.md"
        foreign.write_text("foreign\n")
        target = self.target(source, "codex", destination=destination)
        blocked = lifecycle.install([target])[0]
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(foreign.read_text(), "foreign\n")

        target = lifecycle.LifecycleTarget(
            name=source.name, source=source, destination=destination,
            origin="authored", consumers=("codex",), source_version="v1",
            allow_foreign_replace=True)
        self.assertEqual(lifecycle.install([target])[0].status, "upgraded")
        original = (destination / "SKILL.md").read_text()
        replacement = self.base / "replacement"
        shutil.copytree(source, replacement)
        (replacement / "SKILL.md").write_text("---\nname: hanig-verified-workflow\n---\nreplacement\n")
        upgrade = lifecycle.LifecycleTarget(
            name=source.name, source=replacement, destination=destination,
            origin="authored", consumers=("codex",), source_version="v2")
        with mock.patch.object(lifecycle.shutil, "copytree", side_effect=OSError("simulated full disk")):
            result = lifecycle.install([upgrade])[0]
        self.assertEqual(result.status, "failed")
        self.assertIn("staging failed", result.detail)
        self.assertEqual((destination / "SKILL.md").read_text(), original)

    def test_shared_consumer_is_retained_until_the_last_selective_uninstall(self):
        source = self.source()
        destination = self.home / "shared" / "skills" / source.name
        target = self.target(source, "claude", destination=destination,
                             consumers=("claude", "pi"))
        self.assertEqual(lifecycle.install([target])[0].status, "installed")
        retained = lifecycle.uninstall([destination], consumers=("pi",))[0]
        self.assertEqual(retained.status, "retained-shared")
        self.assertEqual(lifecycle.read_provenance(destination).consumers, ("claude",))
        removed = lifecycle.uninstall([destination], consumers=("claude",))[0]
        self.assertEqual(removed.status, "removed")
        self.assertFalse(destination.exists())

    def test_migration_plan_is_non_destructive(self):
        source = self.source()
        legacy = self.home / ".claude" / "skills" / source.name
        legacy.parent.mkdir(parents=True)
        shutil.copytree(source, legacy)
        plan = lifecycle.plan_migration(
            legacy_roots_by_consumer={"claude": legacy.parent},
            destination_for=lambda consumer, name, old: self.home / ".agents" / "skills" / name,
            selected_names=(source.name,),
        )
        self.assertEqual([(item.legacy_consumer, item.status) for item in plan],
                         [("claude", "ready")])
        self.assertTrue(legacy.is_dir(), "planning migration must retain legacy content")
        self.assertFalse(plan[0].destination.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
