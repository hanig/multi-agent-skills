"""Fixtures for the read-only user-agent discovery contract."""
import os
import sys
import tempfile
import time
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


def fake_cli(directory, name, body):
    path = directory / name
    path.write_text("#!%s\n%s\n" % (sys.executable, body))
    path.chmod(0o755)
    return path


class TestAgentDiscovery(unittest.TestCase):
    def test_schema_and_one_runnable_agent_select_automatically(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(fixture_env(raw), finder({"claude": "/fixtures/claude"}),
                                        probe_for({"claude": VERSIONS["claude"]}))
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["agents"]["claude"]["state"], "executable_found")
        self.assertTrue(report["agents"]["claude"]["eligible_for_automatic_target"])
        self.assertEqual(discovery.select_target(report)["agent"], "claude")

    def test_all_four_are_detected_and_covered_agents_are_selected_owners(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = {agent: "/fixtures/" + agent for agent in discovery.adapters()}
            report = discovery.discover(fixture_env(raw), finder(paths), probe_for(VERSIONS))
        self.assertEqual(set(report["agents"]), {"claude", "codex", "opencode", "pi"})
        plan = discovery.select_targets(report)
        self.assertEqual({item["agent"] for item in plan["selected"]}, set(VERSIONS))
        self.assertEqual([item["agent"] for item in plan["skipped"]], [])
        self.assertEqual({item["agent"] for item in plan["selected"] if item["covered_by"]}, {"opencode", "pi"})
        self.assertTrue(plan["competing_visibility"])
        destinations = {item["destination"]["id"]: item for item in plan["destinations"]}
        self.assertEqual(destinations["claude-user"]["selected_agents"], ["claude", "opencode"])
        self.assertEqual(destinations["agents-user"]["selected_agents"], ["codex", "pi"])
        self.assertNotIn("pi", destinations["claude-user"]["selected_agents"])

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

    def test_opencode_claude_compatibility_uses_home_not_claude_override(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            report = discovery.discover(fixture_env(home, CLAUDE_CONFIG_DIR="custom/claude"), finder({}), probe_for({}))
        claude_root = report["agents"]["claude"]["roots"][0]["logical_path"]
        opencode_roots = {item["id"]: item["logical_path"] for item in report["agents"]["opencode"]["roots"]}
        self.assertEqual(claude_root, os.path.join(raw, "custom/claude/skills"))
        self.assertEqual(opencode_roots["opencode-claude-compatible"], os.path.join(raw, ".claude/skills"))
        custom = next(item for item in report["destinations"] if item["physical_path"] == os.path.realpath(claude_root))
        self.assertEqual(custom["consumers"], ["claude"])

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

    def test_real_noisy_cli_retains_only_a_bounded_tail(self):
        with tempfile.TemporaryDirectory() as raw:
            home, bin_dir = Path(raw) / "home", Path(raw) / "bin"
            home.mkdir()
            bin_dir.mkdir()
            fake_cli(bin_dir, "claude", "import sys; sys.stdout.write('x' * 1000000 + ' 2.1.261')")
            report = discovery.discover(fixture_env(home, PATH=str(bin_dir)))
        output = report["agents"]["claude"]["evidence"]["executable"]["output"]
        self.assertLessEqual(len(output.encode()), discovery.PROBE_OUTPUT_BYTES)
        self.assertTrue(output.endswith("2.1.261"))

    def test_real_hung_cli_times_out_with_a_fixed_deadline(self):
        with tempfile.TemporaryDirectory() as raw:
            home, bin_dir = Path(raw) / "home", Path(raw) / "bin"
            home.mkdir()
            bin_dir.mkdir()
            fake_cli(bin_dir, "claude", "import time; time.sleep(10)")
            start = time.monotonic()
            report = discovery.discover(fixture_env(home, PATH=str(bin_dir)), timeout=0.1)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.5)
        self.assertEqual(report["agents"]["claude"]["state"], "undetermined")
        self.assertIn("timeout", report["agents"]["claude"]["evidence"]["executable"]["output"])

    def test_real_parent_exit_cannot_leave_inherited_output_writer(self):
        with tempfile.TemporaryDirectory() as raw:
            home, bin_dir, marker = Path(raw) / "home", Path(raw) / "bin", Path(raw) / "escaped"
            home.mkdir()
            bin_dir.mkdir()
            fake_cli(bin_dir, "claude", "\n".join([
                "import subprocess, sys",
                "subprocess.Popen([sys.executable, '-c', 'import pathlib, time; time.sleep(.8); pathlib.Path(sys.argv[1]).write_text(\"escaped\")', %r])" % str(marker),
                "print('2.1.261')",
            ]))
            start = time.monotonic()
            report = discovery.discover(fixture_env(home, PATH=str(bin_dir)))
            elapsed = time.monotonic() - start
            time.sleep(0.9)
            escaped = marker.exists()
        self.assertLess(elapsed, 1.5)
        self.assertEqual(report["agents"]["claude"]["state"], "executable_found")
        self.assertFalse(escaped, "probe left an inherited writer running")

    def test_explicit_selection_is_bootstrap_safe_and_exclusions_are_visible(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(fixture_env(raw), finder({}), probe_for({}))
        plan = discovery.select_targets(report, agents=("claude", "opencode"), exclude_agents=("opencode",))
        self.assertEqual(plan["selected"][0]["agent"], "claude")
        self.assertEqual(plan["selected"][0]["mode"], "explicit")
        self.assertEqual(plan["skipped"], [{"agent": "opencode", "reason": "excluded"}])

    def test_flag_order_preserves_display_order_but_not_destination_topology(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(fixture_env(raw), finder({}), probe_for({}))
        forward = discovery.select_targets(report, agents=("codex", "opencode"))
        reverse = discovery.select_targets(report, agents=("opencode", "codex"))

        self.assertEqual([item["agent"] for item in forward["selected"]], ["codex", "opencode"])
        self.assertEqual([item["agent"] for item in reverse["selected"]], ["opencode", "codex"])
        self.assertEqual(forward["destinations"], reverse["destinations"])
        self.assertEqual(forward["competing_visibility"], reverse["competing_visibility"])
        self.assertEqual(len(reverse["destinations"]), 1)
        self.assertEqual(reverse["destinations"][0]["destination"]["id"], "agents-user")
        self.assertEqual(reverse["destinations"][0]["selected_agents"], ["codex", "opencode"])
        self.assertEqual(reverse["selected"][0]["covered_by"], ["codex"])

    def test_competing_visibility_is_stable_and_keeps_exposure_distinct_from_registration(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(fixture_env(raw), finder({}), probe_for({}))
        forward = discovery.select_targets(report, agents=("claude", "codex"))
        reverse = discovery.select_targets(report, agents=("codex", "claude"))

        self.assertEqual(forward["destinations"], reverse["destinations"])
        self.assertEqual(forward["competing_visibility"], reverse["competing_visibility"])
        conflict = forward["competing_visibility"][0]
        self.assertEqual(conflict["consumer"], "opencode")
        self.assertEqual(conflict["selected_agents"], ["claude", "codex"])
        self.assertNotIn("opencode", conflict["selected_agents"])
        self.assertIn("opencode", forward["destinations"][0]["consumers"])

    def test_custom_root_remains_authoritative_under_deterministic_selection(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(fixture_env(raw, CLAUDE_CONFIG_DIR="custom/claude"),
                                        finder({}), probe_for({}))
        forward = discovery.select_targets(report, agents=("claude", "opencode"))
        reverse = discovery.select_targets(report, agents=("opencode", "claude"))

        self.assertEqual(forward["destinations"], reverse["destinations"])
        self.assertEqual(len(forward["destinations"]), 2)
        self.assertEqual(forward["competing_visibility"], [])
        claude = next(item for item in forward["destinations"]
                      if item["destination"]["id"] == "claude-user")
        self.assertEqual(claude["consumers"], ["claude"])
        self.assertEqual(claude["selected_agents"], ["claude"])
