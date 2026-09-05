#!/usr/bin/env python3
"""Fast contract tests for multi-agent installer selection and planning."""

import importlib.util
import sys
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
            {"id": "claude", "detected": True,
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
        self.assertFalse(plan.selected[0].verified)
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

    def test_no_automatic_matches_explains_explicit_selection(self):
        targets = installer.normalize_agents([
            {"id": "claude", "detected": False, "destinations": ["/tmp/a"]},
        ])
        with self.assertRaisesRegex(installer.InstallRequestError, "--agent claude"):
            installer.build_plan(targets, installer.parse_options([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
