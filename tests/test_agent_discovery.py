"""Fixtures for the read-only user-agent discovery contract."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "hanig-project" / "scripts"))
import agent_discovery as discovery  # noqa: E402


VERSIONS = {name: spec["verified_versions"][0] for name, spec in discovery.adapters().items()}


def fixture_env(home, **extra):
    result = {"HOME": str(home), "PATH": ""}
    result.update(extra)
    return result


def finder(paths):
    return lambda executable: paths.get(executable)


def probe_for(versions):
    def probe(path, timeout):
        return True, "agent " + versions[Path(path).name]
    return probe


class TestAgentDiscovery(unittest.TestCase):
    def test_schema_and_one_runnable_agent_select_automatically(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(fixture_env(raw), finder({"claude": "/fixtures/claude"}),
                                        probe_for({"claude": VERSIONS["claude"]}))
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["agents"]["claude"]["state"], "executable_found")
        self.assertTrue(report["agents"]["claude"]["eligible_for_automatic_target"])
        self.assertEqual(discovery.select_target(report)["agent"], "claude")

    def test_all_four_are_detected_but_require_an_explicit_choice(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = {agent: "/fixtures/" + agent for agent in discovery.adapters()}
            report = discovery.discover(fixture_env(raw), finder(paths), probe_for(VERSIONS))
        self.assertEqual(set(report["agents"]), {"claude", "codex", "opencode", "pi"})
        plan = discovery.select_targets(report)
        self.assertEqual({item["agent"] for item in plan["selected"]}, set(VERSIONS))
        self.assertEqual([item["agent"] for item in plan["skipped"]], [])
        self.assertEqual({item["agent"] for item in plan["selected"] if item["covered_by"]}, {"opencode", "pi"})
        self.assertTrue(plan["competing_visibility"])

    def test_none_and_configured_but_not_on_path_are_not_runnable(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            (home / ".claude").mkdir()
            report = discovery.discover(fixture_env(home), finder({}), probe_for({}))
        self.assertEqual(report["agents"]["claude"]["state"], "configured")
        self.assertFalse(report["agents"]["claude"]["eligible_for_automatic_target"])
        self.assertEqual(report["agents"]["codex"]["state"], "absent")

    def test_custom_and_xdg_roots_are_resolved_without_writing(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            report = discovery.discover(fixture_env(
                home, CLAUDE_CONFIG_DIR="custom/claude", CODEX_HOME="custom/codex",
                XDG_CONFIG_HOME="xdg", OPENCODE_CONFIG_DIR="custom/opencode",
                PI_CODING_AGENT_DIR="custom/pi"), finder({}), probe_for({}))
        roots = {agent: item["roots"] for agent, item in report["agents"].items()}
        self.assertEqual(roots["claude"][0]["logical_path"], os.path.join(raw, "custom/claude/skills"))
        self.assertEqual(roots["codex"][1]["logical_path"], os.path.join(raw, "custom/codex/skills"))
        opencode_roots = {root["id"]: root["logical_path"] for root in roots["opencode"]}
        self.assertEqual(opencode_roots["opencode-user"], os.path.join(raw, "xdg/opencode/skills"))
        self.assertEqual(opencode_roots["opencode-config-dir"], os.path.join(raw, "custom/opencode/skills"))
        self.assertEqual(roots["pi"][0]["logical_path"], os.path.join(raw, "custom/pi/skills"))

    def test_duplicate_logical_paths_have_one_normalized_destination(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            real = home / "real"
            real.mkdir()
            (home / "alias").symlink_to(real, target_is_directory=True)
            report = discovery.discover(fixture_env(home, CLAUDE_CONFIG_DIR="alias", PI_CODING_AGENT_DIR="real"),
                                        finder({}), probe_for({}))
        destinations = [item for item in report["destinations"] if set(item["root_ids"]) >= {"claude-user", "pi-user"}]
        self.assertEqual(len(destinations), 1)
        self.assertEqual(set(destinations[0]["logical_paths"]),
                         {os.path.join(raw, "alias/skills"), os.path.join(raw, "real/skills")})

    def test_unknown_version_and_failed_probe_are_unverified(self):
        with tempfile.TemporaryDirectory() as raw:
            unknown = discovery.discover(fixture_env(raw), finder({"codex": "/fixtures/codex"}),
                                         probe_for({"codex": "9.9.9"}))
            failed = discovery.discover(fixture_env(raw), finder({"pi": "/fixtures/pi"}),
                                        lambda path, timeout: (False, "TimeoutExpired"))
        self.assertEqual(unknown["agents"]["codex"]["verification"], "unverified")
        self.assertEqual(failed["agents"]["pi"]["state"], "undetermined")
        self.assertEqual(discovery.select_target(unknown, "codex")["mode"], "explicit")

    def test_supplied_path_not_the_process_path_controls_default_finder(self):
        with tempfile.TemporaryDirectory() as raw:
            home, bin_dir = Path(raw) / "home", Path(raw) / "bin"
            home.mkdir()
            bin_dir.mkdir()
            executable = bin_dir / "claude"
            executable.write_text("#!/bin/sh\nprintf '2.1.261\\n'\n")
            executable.chmod(0o755)
            report = discovery.discover(fixture_env(home, PATH=str(bin_dir)))
        self.assertEqual(report["agents"]["claude"]["state"], "executable_found")
        self.assertEqual(report["agents"]["claude"]["verification"], "verified")

    def test_explicit_selection_is_bootstrap_safe_and_exclusions_are_visible(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(fixture_env(raw), finder({}), probe_for({}))
        plan = discovery.select_targets(report, agents=("claude", "opencode"), exclude_agents=("opencode",))
        self.assertEqual(plan["selected"][0]["agent"], "claude")
        self.assertEqual(plan["selected"][0]["mode"], "explicit")
        self.assertEqual(plan["skipped"], [{"agent": "opencode", "reason": "excluded"}])
