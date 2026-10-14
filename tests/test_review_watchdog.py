"""Real SIGALRM lifecycle through the CLI and offline provider transports."""

import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch

from tests.watchdog_guard import guarded_main


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/hanig-review-gate/scripts/review.py"
sys.path.insert(0, str(SCRIPT.parent))
import committee
SPEC = importlib.util.spec_from_file_location("watchdog_review", SCRIPT)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


@unittest.skipUnless(hasattr(signal, "SIGALRM"), "requires POSIX alarms")
class TestReviewWatchdog(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.subject = Path(temporary.name) / "subject.txt"
        self.subject.write_text("offline review candidate\n")
        self.roster = [
            {"name": provider, "provider": provider, "model": "fixture",
             "profiles": ["plan", "fast", "standard", "deep"]}
            for provider in ("openai", "openrouter")]
        self.observed = []

    def transport(self, _url, payload, *_args, **_kwargs):
        # Runs inside the actual provider functions (and executor threads).
        self.observed.append(signal.getitimer(signal.ITIMER_REAL)[0])
        answer = json.dumps({"verdict": "upheld", "findings": [], "claims": [
            {"claim_index": 0, "status": "supported",
             "claim": review.HONEST_RUN_CLAIM,
             "why": "The offline fixture retains its declared behavior."}],
            "notes": "Offline transport.", "own_findings": [], "peer_responses": []})
        if "input" in payload:
            return {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": answer}]}]}, None
        return {"choices": [{"message": {"content": answer},
                             "finish_reason": "stop"}]}, None

    def invoke(self, flags, expected_exit=0, transport=None, catalog=True):
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", [str(SCRIPT), "--author", "codex/gpt-6-astra"] + flags))
            stack.enter_context(patch.dict(os.environ, {
                "HANIG_ROUTING_SNAPSHOTS": "off", "OPENAI_API_KEY": "fixture",
                "OPENROUTER_API_KEY": "fixture"}))
            if catalog:
                stack.enter_context(patch.object(review, "load_reviewers",
                                                return_value=self.roster))
            stack.enter_context(patch.object(review, "_post",
                                            side_effect=transport or self.transport))
            stack.enter_context(patch.object(committee.R, "_post",
                                            side_effect=transport or self.transport))
            stack.enter_context(patch.object(review.urllib.request, "urlopen",
                                            side_effect=AssertionError("real network")))
            stack.enter_context(patch.object(review, "record_review_round", return_value={}))
            stack.enter_context(patch.object(review, "reserve_review_round"))
            arm = stack.enter_context(patch.object(review, "arm_watchdog",
                                                  wraps=review.arm_watchdog))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            with self.assertRaises(SystemExit) as stopped:
                guarded_main(review.main)
            self.assertEqual(stopped.exception.code, expected_exit)
        return arm

    def review_flags(self, extra=()):
        return ["--kind", "plan", "--file", str(self.subject), "--json",
                "--claim", review.HONEST_RUN_CLAIM, *extra]

    def assert_provider_bound(self, duration):
        self.assertTrue(self.observed, "the fake transport was never dispatched")
        for remaining in self.observed:
            self.assertGreater(remaining, duration - 60)
            self.assertLessEqual(remaining, duration)

    def test_list_catalog_refusal_leaves_no_alarm(self):
        config = self.subject.parent / "reviewers.json"
        config.write_text(json.dumps({"reviewers": [
            {"name": "invalid", "provider": "anthropic", "model": "fixture",
             "effort": "xhigh", "profiles": ["standard"]}]}))
        with patch.object(review, "CONFIG", config):
            arm = self.invoke(["--list"], expected_exit=4, catalog=False)
        arm.assert_not_called()
        self.assertEqual(self.observed, [])

    def test_configuration_refusals_leave_no_alarm(self):
        cases = [[], ["--kind", "implementation"],
                 ["--kind", "plan", "--only", "openai"],
                 ["--kind", "plan", "--escalate"]]
        for flags in cases:
            with self.subTest(flags=flags):
                arm = self.invoke(flags, expected_exit=4)
                arm.assert_not_called()
        self.assertEqual(self.observed, [])

    def test_empty_escalation_panel_refuses_before_arming(self):
        for seat in self.roster:
            seat["profiles"] = []
        arm = self.invoke(["--kind", "implementation", "--round", "1",
                           "--profile", "fast", "--escalate", "--json",
                           "--file", str(self.subject), "--claim", review.HONEST_RUN_CLAIM],
                          expected_exit=4)
        arm.assert_not_called()
        self.assertEqual(self.observed, [])

    def test_provider_phase_retains_default_minimum_and_override_durations(self):
        for extra, duration in (([], 3600), (["--timeout", "10"], 1800),
                                (["--timeout", "1300"], 3900),
                                (["--watchdog", "321"], 321)):
            with self.subTest(extra=extra):
                self.observed = []
                arm = self.invoke(self.review_flags(extra))
                self.assertEqual(len(self.observed), 4)
                self.assert_provider_bound(duration)
                arm.assert_called_once_with(duration)

    def test_one_alarm_bounds_all_escalation_tiers(self):
        for seat, tier in zip(self.roster, ("fast", "deep")):
            seat["profiles"] = [tier]
        arm = self.invoke(["--kind", "implementation", "--round", "1",
                           "--profile", "fast", "--escalate", "--json",
                           "--file", str(self.subject), "--claim", review.HONEST_RUN_CLAIM])
        arm.assert_called_once_with(3600)
        self.assertEqual(len(self.observed), 2)
        self.assert_provider_bound(3600)

    def test_live_list_probes_are_bounded_and_disarmed(self):
        arm = self.invoke(["--list"])
        arm.assert_called_once_with(3600)
        self.assertEqual(len(self.observed), 2)
        self.assert_provider_bound(3600)

    def test_no_probe_does_not_arm(self):
        arm = self.invoke(["--list", "--no-probe"])
        arm.assert_not_called()
        self.assertEqual(self.observed, [])

    def test_explicit_disable_remains_supported(self):
        self.invoke(self.review_flags(["--watchdog", "0"]))
        self.assertEqual(self.observed, [0, 0, 0, 0])

    def test_system_exit_from_provider_disarms(self):
        def exiting_transport(*args, **kwargs):
            self.transport(*args, **kwargs)
            raise SystemExit(17)

        self.invoke(self.review_flags(), expected_exit=17, transport=exiting_transport)
        self.assert_provider_bound(3600)

    def test_keyboard_interrupt_from_provider_disarms(self):
        def interrupted_transport(*args, **kwargs):
            self.transport(*args, **kwargs)
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.invoke(self.review_flags(), transport=interrupted_transport)
        self.assert_provider_bound(3600)

    def test_exception_before_verdict_disarms(self):
        with patch.object(review._load_plan_review(), "_joint_plan_verdict",
                          side_effect=RuntimeError("fixture")):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                self.invoke(self.review_flags())
        self.assert_provider_bound(3600)

    def test_normal_return_disarms(self):
        def returning():
            review.arm_watchdog(3600)
            return "returned"

        with patch.object(review, "_main", side_effect=returning):
            self.assertEqual(guarded_main(review.main), "returned")


class TestWatchdogGuard(unittest.TestCase):
    @unittest.skipUnless(hasattr(signal, "SIGALRM"), "requires POSIX alarms")
    def test_guard_cancels_and_rejects_a_leak_on_return_or_exception(self):
        for error in (None, SystemExit(4), RuntimeError("fixture"), KeyboardInterrupt()):
            with self.subTest(error=error):
                def leaking():
                    signal.alarm(3600)
                    if error is not None:
                        raise error

                try:
                    with self.assertRaisesRegex(AssertionError, "pending SIGALRM"):
                        guarded_main(leaking)
                    self.assertEqual(signal.alarm(0), 0)
                finally:
                    signal.alarm(0)

    def test_test_entrypoints_use_shared_guard(self):
        # Include conventional dynamic-import names plus ordinary import aliases.
        # Inspect executable AST only: examples/comments cannot satisfy this guard.
        offenders = []
        guarded = 0
        for path in sorted((ROOT / "tests").rglob("*.py")):
            tree = ast.parse(path.read_text())
            modules = {"review", "R", "committee", "C", "contract", "handoff", "unit"}
            entries = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in {"review", "committee", "contract", "handoff", "unit"}:
                            modules.add(alias.asname or alias.name)
                if isinstance(node, ast.ImportFrom) and node.module in modules:
                    entries.update(alias.asname or alias.name for alias in node.names
                                   if alias.name == "main")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if ((isinstance(fn, ast.Attribute) and fn.attr == "main"
                     and isinstance(fn.value, ast.Name) and fn.value.id in modules)
                        or (isinstance(fn, ast.Name) and fn.id in entries)):
                    offenders.append("%s:%s" % (path.relative_to(ROOT), node.lineno))
                if isinstance(fn, ast.Name) and fn.id == "guarded_main":
                    guarded += 1
        self.assertEqual(offenders, [], "use guarded_main: " + ", ".join(offenders))
        self.assertGreater(guarded, 0, "the shared guard must have real consumers")


if __name__ == "__main__":
    unittest.main()
