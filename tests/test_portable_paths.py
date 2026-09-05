#!/usr/bin/env python3
"""Installed-snapshot path contracts for the authored hanig skills.

These tests intentionally execute copied artifacts after their staging source
has gone away, from a project cwd containing spaces and with an empty HOME.
They exercise path resolution only; no model, scheduler, or network call runs.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
AUTHORED = ("hanig-portable-handoff", "hanig-project", "hanig-review-gate",
            "hanig-swarm", "hanig-verified-workflow")


class InstalledSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "empty home"
        self.home.mkdir()
        self.project = self.root / "project cwd with spaces"
        self.project.mkdir()
        source = self.root / "source checkout" / "skills"
        prefix = self.root / "custom prefix with spaces"
        source.mkdir(parents=True)
        prefix.mkdir()
        for name in AUTHORED:
            shutil.copytree(SKILLS / name, source / name)
            shutil.copytree(source / name, prefix / name)
        # The copied installation is all that remains.  An accidentally
        # checkout-relative helper would therefore fail rather than masking
        # the portability defect with this test repository.
        shutil.rmtree(source.parent)
        self.prefix = prefix
        # Portability must not depend on coordinator credentials or config.
        self.env = {"HOME": str(self.home), "PATH": os.defpath}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, relative, *args, cwd=None):
        return subprocess.run([sys.executable, str(self.prefix / relative), *args],
                              cwd=str(cwd or self.project), env=self.env,
                              text=True, capture_output=True)

    def test_authored_markdown_has_no_claude_path_or_cwd_script_path(self):
        for name in AUTHORED:
            text = (SKILLS / name / "SKILL.md").read_text()
            self.assertNotIn("~/.claude/skills", text, name)
            self.assertNotIn("python3 scripts/", text, name)
        project = (SKILLS / "hanig-project" / "SKILL.md").read_text()
        self.assertNotIn("../hanig-swarm/scripts", project)

    def test_contract_handoff_survey_and_validation_use_copy_not_checkout(self):
        contract = self.invoke("hanig-verified-workflow/scripts/contract.py", "init",
                               "run", "--command", "true", "--output", "out.txt")
        self.assertEqual(contract.returncode, 0, contract.stderr)
        self.assertTrue((self.project / "run" / "contract.json").is_file())
        self.assertFalse((self.prefix / "hanig-verified-workflow" / "run").exists())

        run_dir = self.project / "handoff run"
        run_dir.mkdir()
        handoff = self.invoke("hanig-portable-handoff/scripts/handoff.py", "capture",
                              str(run_dir), "--out", "handoff.json")
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertTrue((self.project / "handoff.json").is_file())

        survey = self.invoke("hanig-project/scripts/survey.py", "--repo", ".",
                             "--out", ".swarm/survey.json")
        self.assertEqual(survey.returncode, 0, survey.stderr)
        self.assertTrue((self.project / ".swarm" / "survey.json").is_file())

        plan = {"name": "portable", "units": [{"id": "check", "kind": "slurm",
                "runtime": "none", "command": "true", "outputs": ["out.txt"]}]}
        (self.project / "plan.json").write_text(json.dumps(plan))
        valid = self.invoke("hanig-swarm/scripts/swarm.py", "validate", "plan.json")
        self.assertEqual(valid.returncode, 0, valid.stderr + valid.stdout)

    def test_review_configuration_and_project_sibling_are_portable(self):
        review = self.invoke("hanig-review-gate/scripts/review.py", "--list", "--no-probe")
        # Loading a valid copied config is distinct from having provider keys.
        # An empty HOME/environment honestly reports REVIEW_UNAVAILABLE (2).
        self.assertEqual(review.returncode, 2, review.stderr + review.stdout)
        config = json.loads((self.prefix / "hanig-review-gate" / "reviewers.json").read_text())
        expected = [reviewer for reviewer in config["reviewers"]
                    if config["default_profile"] in reviewer.get("profiles", [])]
        self.assertTrue(expected)
        for reviewer in expected:
            self.assertIn(reviewer["name"], review.stdout)
        self.assertIn("No reviewer can run", review.stdout)
        self.assertNotIn("REVIEW_ERROR", review.stdout + review.stderr)

        resolver = self.prefix / "hanig-project" / "scripts" / "skill_paths.py"
        found = subprocess.run([sys.executable, str(resolver), "sibling",
                                str(self.prefix / "hanig-project"), "hanig-project",
                                "hanig-swarm"], env=self.env, text=True, capture_output=True)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(Path(found.stdout.strip()),
                         (self.prefix / "hanig-swarm").resolve())

        shutil.rmtree(self.prefix / "hanig-swarm")
        missing = subprocess.run([sys.executable, str(resolver), "sibling",
                                  str(self.prefix / "hanig-project"), "hanig-project",
                                  "hanig-swarm"], env=self.env, text=True, capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("missing declared installed dependency 'hanig-swarm'",
                      missing.stderr)

    def test_linked_loaded_skill_keeps_logical_parent_and_uses_known_root(self):
        linked_parent = self.root / "link store"
        linked_parent.mkdir()
        linked_project = linked_parent / "hanig-project"
        linked_project.symlink_to(self.prefix / "hanig-project", target_is_directory=True)
        separate = self.root / "separate known parent"
        separate.mkdir()
        shutil.copytree(SKILLS / "hanig-swarm", separate / "hanig-swarm")
        resolver = self.prefix / "hanig-project" / "scripts" / "skill_paths.py"
        found = subprocess.run([sys.executable, str(resolver), "sibling",
                                str(linked_project), "hanig-project", "hanig-swarm",
                                "--root", str(separate)], env=self.env,
                               text=True, capture_output=True)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(Path(found.stdout.strip()),
                         (separate / "hanig-swarm").resolve())

        mixed_env = dict(self.env, HANIG_SKILL_DEP_ROOTS=str(separate))
        report = subprocess.run([sys.executable,
                                 str(linked_project / "scripts" / "report.py"),
                                 ".", "--json"], cwd=self.project, env=mixed_env,
                                text=True, capture_output=True)
        self.assertEqual(report.returncode, 0, report.stderr)

    def test_project_report_resolves_its_sibling_without_source_layout(self):
        report = self.invoke("hanig-project/scripts/report.py", ".", "--json")
        self.assertEqual(report.returncode, 0, report.stderr)


if __name__ == "__main__":
    unittest.main()
