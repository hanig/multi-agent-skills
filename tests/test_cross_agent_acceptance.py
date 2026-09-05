#!/usr/bin/env python3
"""Hermetic acceptance seam for portable agent skill installation.

These tests deliberately exercise the discovery/selection contract without a
real agent binary, account, or user configuration directory.  Filesystem
lifecycle tests are added beside this seam once install.sh consumes the public
planner contract; native loader proof belongs in docs/cross-agent-acceptance.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_PATH = ROOT / "skills" / "hanig-project" / "scripts" / "agent_discovery.py"
SPEC = importlib.util.spec_from_file_location("agent_discovery_acceptance",
                                              DISCOVERY_PATH)
discovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = discovery
SPEC.loader.exec_module(discovery)

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

    def test_all_agents_require_explicit_targets_not_arbitrary_default(self):
        report = self.report(AGENTS)
        selected = discovery.select_target(report)
        self.assertEqual(selected["status"], "requires_explicit_target")
        self.assertEqual(selected["eligible_agents"], list(AGENTS))

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
            str(custom / "opencode" / "skills"),
        )
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
