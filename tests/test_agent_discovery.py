"""Fixtures for the read-only user-agent discovery contract."""
import os
import copy
import json
import select
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "hanig-project" / "scripts"))
import agent_discovery as discovery  # noqa: E402


VERSIONS = {name: spec["verified_versions"][0] for name, spec in discovery.adapters().items()}
# Real process startup is not the property under test. Give it headroom on a
# shared host; short production deadlines are covered separately below.
REAL_PROBE_SECONDS = 60
REAL_REAP_SECONDS = 1
# Measure only after observed readiness/forced expiry, excluding interpreter
# startup. The 1s drain window plus 7s slack gives an 8s bound. The slack covers
# the additional post-kill/finally reap windows and scheduling/exit delays,
# while still rejecting an extra 10s wait or the full 60s probe budget. The
# 120s watchdog separately detects deadlock.
REAL_COMPLETION_SLACK_SECONDS = 7
WATCHDOG_SECONDS = 120


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


@contextmanager
def real_discovery(env):
    """Run the real probe with fixture budgets and an independent watchdog.

    Callers await the Future before releasing any fixture barrier. Cleanup
    uses the owned supervisor, independently of the production kill helper.
    The worker timestamps discovery's return so a delayed observer cannot add
    time after completion to the measured latency.
    """
    answer = Future()
    processes = []
    popen = discovery.subprocess.Popen

    def launch(*args, **kwargs):
        proc = popen(*args, **kwargs)
        processes.append(proc)
        return proc

    def run():
        try:
            report = discovery.discover(env, timeout=REAL_PROBE_SECONDS)
            answer.set_result((report, time.monotonic()))
        except BaseException as error:
            answer.set_exception(error)

    with mock.patch.object(discovery, "PROBE_REAP_SECONDS", REAL_REAP_SECONDS), \
            mock.patch.object(discovery.subprocess, "Popen", side_effect=launch):
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            yield answer
        finally:
            for proc in processes:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            worker.join(WATCHDOG_SECONDS)
            if worker.is_alive():
                raise AssertionError("discovery worker survived cleanup")


class TestAgentDiscovery(unittest.TestCase):
    def test_schema_and_one_runnable_agent_select_automatically(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(fixture_env(raw), finder({"claude": "/fixtures/claude"}),
                                        probe_for({"claude": VERSIONS["claude"]}))
        self.assertEqual(report["schema_version"], 2)
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

    def test_unknown_version_and_failed_probe_are_present_but_unverified(self):
        with tempfile.TemporaryDirectory() as raw:
            unknown = discovery.discover(fixture_env(raw), finder({"codex": "/fixtures/codex"}),
                                         probe_for({"codex": "9.9.9"}))
            failed = discovery.discover(fixture_env(raw), finder({"pi": "/fixtures/pi"}),
                                        lambda path, timeout: (False, "TimeoutExpired"))
        self.assertEqual(unknown["agents"]["codex"]["verification"], "unverified")
        self.assertEqual(failed["agents"]["pi"]["state"], "probe_failed")
        self.assertTrue(unknown["agents"]["codex"]["eligible_for_automatic_target"])
        self.assertEqual([item["agent"] for item in discovery.select_targets(unknown)["selected"]],
                         ["codex"])
        self.assertEqual(discovery.select_target(unknown, "codex")["mode"], "explicit")

    def test_adapter_certification_expiry_respects_review_deadlines(self):
        for agent, spec in discovery.adapters().items():
            with self.subTest(agent=agent):
                deadline = discovery.verification_review_due(spec)
                self.assertNotIn(agent, discovery.stale_adapter_certifications(deadline))
                self.assertIn(agent, discovery.stale_adapter_certifications(
                    deadline + timedelta(days=1)))

    def test_stale_certification_changes_the_automatic_selection_result(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(
                fixture_env(raw), finder({"claude": "/fixtures/claude"}),
                probe_for({"claude": VERSIONS["claude"]}))
        plan = discovery.select_targets(report, as_of=date(2026, 10, 6))
        selected = plan["selected"][0]
        self.assertEqual(selected["agent"], "claude")
        self.assertEqual(selected["certification"], "unverified")
        self.assertTrue(any("expired after 2026-10-05" in warning
                            for warning in selected["certification_warnings"]))
        self.assertEqual(selected["certification_warnings"],
                         plan["certification_warnings"])

    def test_supplied_path_not_the_process_path_controls_default_finder(self):
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(discovery, "date", wraps=date) as clock:
            clock.today.return_value = date(2026, 9, 25)
            home, bin_dir = Path(raw) / "home", Path(raw) / "bin"
            home.mkdir()
            bin_dir.mkdir()
            executable = bin_dir / "claude"
            executable.write_text("#!/bin/sh\nprintf '2.1.261\\n'\n")
            executable.chmod(0o755)
            with real_discovery(fixture_env(home, PATH=str(bin_dir))) as answer:
                report, _ = answer.result(timeout=WATCHDOG_SECONDS)
        self.assertEqual(report["agents"]["claude"]["state"], "executable_found")
        self.assertEqual(report["agents"]["claude"]["verification"], "verified")

    def test_real_noisy_cli_retains_only_a_bounded_tail(self):
        with tempfile.TemporaryDirectory() as raw:
            home, bin_dir = Path(raw) / "home", Path(raw) / "bin"
            home.mkdir()
            bin_dir.mkdir()
            fake_cli(bin_dir, "claude", "import sys; sys.stdout.write('x' * 1000000 + ' 2.1.261')")
            with real_discovery(fixture_env(home, PATH=str(bin_dir))) as answer:
                report, _ = answer.result(timeout=WATCHDOG_SECONDS)
        output = report["agents"]["claude"]["evidence"]["executable"]["output"]
        self.assertLessEqual(len(output.encode()), discovery.PROBE_OUTPUT_BYTES)
        self.assertTrue(output.endswith("2.1.261"))

    def test_real_hung_cli_is_slow_not_probe_failed_or_undetermined(self):
        with tempfile.TemporaryDirectory() as raw, socket.socket() as listener:
            home, bin_dir = Path(raw) / "home", Path(raw) / "bin"
            home.mkdir()
            bin_dir.mkdir()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(WATCHDOG_SECONDS)
            fake_cli(bin_dir, "claude", "\n".join([
                "import socket",
                "peer = socket.create_connection(%r, timeout=%r)" % (
                    listener.getsockname(), WATCHDOG_SECONDS * 2),
                "peer.sendall(b'R')",
                "peer.recv(1)",
            ]))
            peers = []
            expired_at = None

            def expire_after_cli_started(readers, writers, errors, timeout):
                nonlocal expired_at
                peer, _ = listener.accept()
                peers.append(peer)
                peer.settimeout(WATCHDOG_SECONDS)
                self.assertEqual(peer.recv(1), b"R", "hung CLI never started")
                # Exercise the real timeout branch only after the CLI is
                # blocked. Zero readiness comes from the OS, not a fake report.
                self.assertGreater(timeout, 0)
                expired_at = time.monotonic()
                return select.select(readers, writers, errors, 0)

            try:
                with mock.patch.object(discovery, "select", wraps=select) as polling:
                    polling.select.side_effect = expire_after_cli_started
                    with real_discovery(fixture_env(home, PATH=str(bin_dir))) as answer:
                        report, completed_at = answer.result(timeout=WATCHDOG_SECONDS)
                        self.assertIsNotNone(expired_at)
                        self.assertLess(
                            completed_at - expired_at,
                            REAL_REAP_SECONDS + REAL_COMPLETION_SLACK_SECONDS,
                            "discovery exceeded the post-expiry completion bound")
                        # Observe termination before the fixture's fallback
                        # cleanup can kill a survivor and mask a probe defect.
                        self.assertEqual(len(peers), 1)
                        try:
                            response = peers[0].recv(1)
                        except ConnectionResetError:
                            # As in the inherited-writer case, reset also
                            # means the killed peer's connection has closed.
                            response = b""
                        self.assertEqual(response, b"", "timed-out CLI survived")
            finally:
                for peer in peers:
                    peer.close()
        agent = report["agents"]["claude"]
        self.assertEqual(agent["state"], "slow")
        self.assertEqual(agent["evidence"]["executable"]["outcome"], "SLOW")
        self.assertIn("timeout", agent["evidence"]["executable"]["output"])

    def test_failed_probe_is_distinct_from_slow_probe(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(
                fixture_env(raw), finder({"pi": "/fixtures/pi"}),
                lambda path, timeout: (False, "exit 9: broken"))
        self.assertEqual(report["agents"]["pi"]["state"], "probe_failed")
        self.assertEqual(report["agents"]["pi"]["evidence"]["executable"]["outcome"],
                         "FAILED")
        plan = discovery.select_targets(report)
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [
            {"agent": "claude", "reason": "absent", "certification": "unverified"},
            {"agent": "codex", "reason": "absent", "certification": "unverified"},
            {"agent": "opencode", "reason": "absent", "certification": "unverified"},
            {"agent": "pi", "reason": "probe_failed", "certification": "unverified"},
        ])

    def test_default_deadlines_are_derived_from_each_adapter_measurement(self):
        deadlines = {name: discovery.probe_deadline(spec)
                     for name, spec in discovery.adapters().items()}
        self.assertGreater(deadlines["opencode"], deadlines["pi"])
        self.assertEqual(deadlines["opencode"], 4.92)
        self.assertEqual(deadlines["claude"], discovery.PROBE_DEADLINE_FLOOR_SECONDS)

    def test_real_parent_exit_cannot_leave_inherited_output_writer(self):
        with tempfile.TemporaryDirectory() as raw, socket.socket() as listener:
            home, bin_dir, marker = Path(raw) / "home", Path(raw) / "bin", Path(raw) / "escaped"
            home.mkdir()
            bin_dir.mkdir()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(WATCHDOG_SECONDS)
            writer = "\n".join([
                "import os, pathlib, socket, sys",
                "peer = socket.create_connection(%r, timeout=%r)" % (
                    listener.getsockname(), WATCHDOG_SECONDS * 2),
                "peer.sendall(b'R')",
                "assert peer.recv(1) == b'A'",
                "os.write(int(sys.argv[1]), b'R')",
                "os.close(int(sys.argv[1]))",
                "if peer.recv(1) == b'W':",
                "    pathlib.Path(%r).write_text('escaped')" % str(marker),
                "    peer.sendall(b'E')",
                "peer.close()",
            ])
            fake_cli(bin_dir, "claude", "\n".join([
                "import os, subprocess, sys",
                "ready, notify = os.pipe()",
                "subprocess.Popen([sys.executable, '-c', %r, str(notify)], pass_fds=(notify,))" % writer,
                "os.close(notify)",
                "assert os.read(ready, 1) == b'R'",
                "os.close(ready)",
                "print('2.1.261')",
            ]))
            peer = None
            with real_discovery(fixture_env(home, PATH=str(bin_dir))) as answer:
                try:
                    peer, _ = listener.accept()
                    peer.settimeout(WATCHDOG_SECONDS)
                    self.assertEqual(peer.recv(1), b"R", "writer never reached its barrier")
                    ready_at = time.monotonic()
                    # Only this acknowledgment lets the writer notify the CLI
                    # to print/exit. The drain cannot precede our timestamp,
                    # even if this test thread was starved during startup.
                    peer.sendall(b"A")
                    # No release is sent until discovery returns. Waiting for
                    # inherited stdout EOF would therefore trip this watchdog.
                    report, completed_at = answer.result(timeout=WATCHDOG_SECONDS)
                    self.assertLess(
                        completed_at - ready_at,
                        REAL_REAP_SECONDS + REAL_COMPLETION_SLACK_SECONDS,
                        "discovery exceeded the post-readiness drain bound")
                    self.assertEqual(report["agents"]["claude"]["state"], "executable_found")
                    try:
                        peer.sendall(b"W")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    # A write to the killed peer can elicit TCP RST instead of
                    # EOF. Both mean the connection closed; a surviving writer
                    # writes the marker before acknowledging release with E.
                    try:
                        response = peer.recv(1)
                    except ConnectionResetError:
                        response = b""
                    self.assertEqual(response, b"", "probe left an inherited writer running")
                    self.assertFalse(marker.exists(), "probe left an inherited writer running")
                finally:
                    # Release a blocked writer even when the watchdog/assertion
                    # failed. The context then stops any owned supervisor.
                    if peer is None and select.select([listener], [], [], 0)[0]:
                        peer, _ = listener.accept()
                    if peer is not None:
                        peer.close()

    def test_explicit_selection_is_bootstrap_safe_and_exclusions_are_visible(self):
        with tempfile.TemporaryDirectory() as raw:
            report = discovery.discover(fixture_env(raw), finder({}), probe_for({}))
        plan = discovery.select_targets(report, agents=("claude", "opencode"), exclude_agents=("opencode",))
        self.assertEqual(plan["selected"][0]["agent"], "claude")
        self.assertEqual(plan["selected"][0]["mode"], "explicit")
        self.assertEqual(plan["skipped"], [{
            "agent": "opencode", "reason": "excluded",
            "certification": "unverified",
        }])

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


class TestDatedLiveCertifications(unittest.TestCase):
    LIVE = {"claude": "2.1.282", "codex": "0.154.0",
            "opencode": "1.18.29", "pi": "0.86.1"}

    def report(self, versions):
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(discovery, "date", wraps=date) as clock:
            clock.today.return_value = date(2026, 9, 25)
            return discovery.discover(
                fixture_env(raw), finder({name: "/fixtures/" + name for name in versions}),
                probe_for(versions))

    def test_live_exact_versions_select_their_own_dated_record(self):
        report = self.report(self.LIVE)
        plan = discovery.select_targets(report, as_of=date(2026, 10, 6))
        for item in plan["selected"]:
            with self.subTest(agent=item["agent"]):
                self.assertEqual(item["certification"], "verified")
                record = item["certification_record"]
                self.assertEqual(record["version"], self.LIVE[item["agent"]])
                self.assertEqual(record["verified_on"], "2026-09-25")
                self.assertEqual(record["evidence"], "ARC-281 live run")
                self.assertEqual(record["checks"], ["native_discovery",
                    "authenticated_skill_invocation", "cross_agent_handoff"])
                observed = report["agents"][item["agent"]]
                self.assertEqual(observed["verification_review_due"], "2026-10-25")
                self.assertEqual(observed["source_verification"]["native_discovery"], "live_verified")
                self.assertEqual(observed["source_verification"]["invocation"], "live_verified")
        self.assertEqual(plan["certification_warnings"], [])

    def test_one_patch_newer_has_no_certification_record(self):
        versions = {"claude": "2.1.283", "codex": "0.154.1",
                    "opencode": "1.18.30", "pi": "0.86.2"}
        plan = discovery.select_targets(self.report(versions), as_of=date(2026, 10, 6))
        self.assertEqual(len(plan["selected"]), 4)
        for item in plan["selected"]:
            with self.subTest(agent=item["agent"]):
                self.assertEqual(item["certification"], "unverified")
                self.assertIsNone(item["certification_record"])
                self.assertTrue(item["certification_warnings"])

    def test_new_live_records_expire_without_blocking_selection(self):
        report = self.report(self.LIVE)
        current = discovery.select_targets(report, as_of=date(2026, 10, 25))
        expired = discovery.select_targets(report, as_of=date(2026, 10, 26))
        self.assertEqual([item["certification"] for item in current["selected"]],
                         ["verified"] * 4)
        self.assertEqual([item["certification"] for item in expired["selected"]],
                         ["unverified"] * 4)
        self.assertEqual(len(expired["certification_warnings"]), 4)
        self.assertTrue(all("expired after 2026-10-25" in warning
                            for warning in expired["certification_warnings"]))

    def test_old_releases_keep_their_original_date_and_expiry(self):
        old = {"claude": "2.1.261", "codex": "0.153.4", "pi": "0.73.1"}
        report = self.report(old)
        current = discovery.select_targets(report, as_of=date(2026, 10, 5))
        expired = discovery.select_targets(report, as_of=date(2026, 10, 6))
        self.assertEqual([item["certification"] for item in current["selected"]],
                         ["verified"] * 3)
        self.assertEqual([item["certification"] for item in expired["selected"]],
                         ["unverified"] * 3)
        for item in expired["selected"]:
            self.assertEqual(item["certification_record"]["verified_on"], "2026-09-05")
            self.assertEqual(report["agents"][item["agent"]]["source_verification"]["invocation"],
                             "unverified")
        self.assertEqual([record["verified_on"] for record in
                          discovery.adapters()["opencode"]["certifications"]],
                         ["2026-09-05", "2026-09-25"])


    def test_adapter_staleness_includes_expired_distinct_versions(self):
        # A renewed OpenCode record supersedes the old record for that SAME
        # version. A newer Claude/Codex/Pi version cannot renew an older one.
        for observed, expected in (
                (date(2026, 10, 5), []),
                (date(2026, 10, 6), ["claude", "codex", "pi"]),
                (date(2026, 10, 25), ["claude", "codex", "pi"]),
                (date(2026, 10, 26), ["claude", "codex", "opencode", "pi"])):
            with self.subTest(observed=observed):
                self.assertEqual(discovery.stale_adapter_certifications(observed), expected)
        for agent, spec in discovery.adapters().items():
            with self.subTest(agent=agent):
                self.assertEqual(discovery.verification_review_due(spec),
                                 date(2026, 10, 25) if agent == "opencode"
                                 else date(2026, 10, 5))

    def test_discovery_verification_expires_with_each_exact_version_record(self):
        versions = [
            ("claude", "2.1.261", date(2026, 9, 5)),
            ("codex", "0.153.4", date(2026, 9, 5)),
            ("pi", "0.73.1", date(2026, 9, 5)),
        ] + [(name, version, date(2026, 9, 25))
             for name, version in self.LIVE.items()]
        for name, version, verified_on in versions:
            deadline = verified_on + timedelta(days=30)
            for offset, verification, freshness in (
                    (-1, "verified", "current"), (0, "verified", "current"),
                    (1, "unverified", "stale")):
                observed = deadline + timedelta(days=offset)
                with self.subTest(agent=name, version=version, observed=observed), \
                        tempfile.TemporaryDirectory() as raw, \
                        mock.patch.object(discovery, "date", wraps=date) as clock:
                    clock.today.return_value = observed
                    report = discovery.discover(
                        fixture_env(raw), finder({name: "/fixtures/" + name}),
                        probe_for({name: version}))
                    agent = report["agents"][name]
                    self.assertEqual(agent["verification"], verification)
                    self.assertEqual(agent["verification_freshness"], freshness)
                    self.assertEqual(agent["verified_on"], verified_on.isoformat())
                    self.assertEqual(agent["verification_review_due"], deadline.isoformat())
                    self.assertEqual(agent["evidence"]["certification"]["version"], version)
                    self.assertTrue(agent["eligible_for_automatic_target"])
                    selected = discovery.select_targets(report)["selected"]
                    self.assertEqual([item["agent"] for item in selected], [name])
                    self.assertEqual(selected[0]["certification"], verification)


class TestCertificationAuthority(unittest.TestCase):
    def report(self, version):
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(discovery, "date", wraps=date) as clock:
            clock.today.return_value = date(2026, 9, 25)
            return discovery.discover(
                fixture_env(raw), finder({"claude": "/fixtures/claude"}),
                probe_for({"claude": version}))

    def assert_forged_deadline_is_ignored(self, fields):
        for version, observed, deadline in (
                ("2.1.261", date(2026, 10, 6), "2026-10-05"),
                ("2.1.282", date(2026, 10, 26), "2026-10-25"),
                ("2.1.283", date(2026, 10, 6), None)):
            with self.subTest(version=version, fields=fields):
                report = self.report(version)
                record = report["agents"]["claude"]
                record.pop("verification_review_due")
                record.update(verification="verified", **fields)
                # Round-trip an old saved report, not only today's producer.
                with tempfile.TemporaryDirectory() as raw:
                    saved = Path(raw) / "report.json"
                    saved.write_text(json.dumps(report))
                    plan = discovery.select_targets(json.loads(saved.read_text()), as_of=observed)
                selected = plan["selected"][0]
                self.assertEqual(selected["agent"], "claude")
                self.assertEqual(selected["certification"], "unverified")
                warnings = selected["certification_warnings"]
                self.assertTrue(warnings)
                if deadline:
                    self.assertTrue(any("expired after " + deadline in warning
                                        for warning in warnings))
                else:
                    self.assertTrue(any("not adapter-certified" in warning for warning in warnings))

    def test_null_report_deadline_cannot_certify_expired_or_unknown_version(self):
        self.assert_forged_deadline_is_ignored({"verification_review_due": None})

    def test_future_report_deadline_cannot_certify_expired_or_unknown_version(self):
        self.assert_forged_deadline_is_ignored({"verification_review_due": "2099-12-31"})

    def test_absent_report_deadline_cannot_certify_expired_or_unknown_version(self):
        self.assert_forged_deadline_is_ignored({})

    def test_current_version_ignores_report_dates_and_other_versions_expiry(self):
        for deadline in (None, "2000-01-01", "2099-12-31"):
            with self.subTest(deadline=deadline):
                report = self.report("2.1.282")
                report["agents"]["claude"]["verification_review_due"] = deadline
                selected = discovery.select_targets(report, as_of=date(2026, 10, 6))["selected"][0]
                self.assertEqual(selected["certification"], "verified")
                self.assertEqual(selected["certification_warnings"], [])

    def test_excluded_and_explicit_targets_use_the_same_authority(self):
        report = self.report("2.1.261")
        report["agents"]["claude"]["verification_review_due"] = "2099-12-31"
        for excluded in ((), ("claude",)):
            with self.subTest(excluded=excluded):
                plan = discovery.select_targets(report, agents=("claude",),
                    exclude_agents=excluded, as_of=date(2026, 10, 6))
                items = plan["skipped"] if excluded else plan["selected"]
                self.assertEqual(items[0]["certification"], "unverified")
                if not excluded:
                    self.assertTrue(plan["certification_warnings"])

    def test_report_evidence_is_not_a_mutable_alias_of_authority(self):
        before = copy.deepcopy(discovery.ADAPTERS)
        report = self.report("2.1.261")
        try:
            record = report["agents"]["claude"]
            record["evidence"]["certification"]["verified_on"] = "2099-01-01"
            record["evidence"]["certification"]["checks"].append("forged")
            self.assertEqual(discovery.ADAPTERS, before)
            selected = discovery.select_targets(report, as_of=date(2026, 10, 6))["selected"][0]
            self.assertEqual(selected["certification"], "unverified")
            self.assertEqual(selected["certification_record"]["verified_on"], "2026-09-05")
            selected["certification_record"]["checks"].append("also forged")
            self.assertEqual(discovery.ADAPTERS, before)
        finally:
            discovery.ADAPTERS.clear()
            discovery.ADAPTERS.update(before)
