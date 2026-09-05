"""Portable skill metadata and capability-contract regression tests.

These tests intentionally install into a disposable prefix and HOME-equivalent
directory. They test loader artifacts and documented fallbacks; they do not
try to provision a host agent, connector, credential, or optional daemon.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
COMPATIBILITY = ROOT / "docs" / "agent-compatibility.md"
AUTHORED = {
    "hanig-project",
    "hanig-swarm",
    "hanig-verified-workflow",
    "hanig-review-gate",
    "hanig-portable-handoff",
}
VENDORED = {
    "agent-bus",
    "paseo",
    "paseo-advisor",
    "paseo-committee",
    "paseo-handoff",
    "paseo-loop",
    "pi-fleet",
    "start-a-sprint",
}


def _frontmatter(path):
    match = re.match(r"\A---\n(.*?)\n---\n", path.read_text(), re.DOTALL)
    if not match:
        return None
    return match.group(1)


class TestSkillCapabilities(unittest.TestCase):
    def test_all_thirteen_bundles_have_portable_loader_metadata(self):
        bundles = {path.parent.name for path in SKILLS.glob("*/SKILL.md")}
        self.assertEqual(bundles, AUTHORED | VENDORED)
        for name in sorted(bundles):
            with self.subTest(bundle=name):
                frontmatter = _frontmatter(SKILLS / name / "SKILL.md")
                self.assertIsNotNone(frontmatter)
                self.assertRegex(frontmatter, rf"(?m)^name: {re.escape(name)}$")
                self.assertRegex(frontmatter, r"(?m)^description:")
                self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    def test_installed_artifacts_keep_all_bundle_documents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = root / "skills"
            home = root / "home"
            # The installer must only copy skill artifacts when optional fleet
            # services are absent.  A minimal system PATH is an intentionally
            # missing Paseo/agent-bus capability, not a production environment.
            env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
            self.assertIsNone(shutil.which("paseo", path=env["PATH"]))
            self.assertIsNone(shutil.which("bus", path=env["PATH"]))
            result = subprocess.run(
                ["sh", str(ROOT / "install.sh"), "--prefix", str(prefix),
                 "--allow-org-shadow"],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in sorted(AUTHORED | VENDORED):
                source = SKILLS / name / "SKILL.md"
                installed = prefix / name / "SKILL.md"
                with self.subTest(bundle=name):
                    self.assertEqual(installed.read_text(), source.read_text())
                    self.assertIsNotNone(_frontmatter(installed))
            for forbidden in (".agent-bus", ".paseo", ".config"):
                with self.subTest(home_state=forbidden):
                    self.assertFalse(
                        (home / forbidden).exists(),
                        "installation must not provision optional host state",
                    )

    def test_contract_distinguishes_unavailable_capabilities_and_fallbacks(self):
        text = COMPATIBILITY.read_text()
        for capability in (
            "Python 3 and Git",
            "Paseo and agent bus",
            "Reviewer providers and coordinator-held credentials",
            "Linear connector and authorized account",
        ):
            with self.subTest(capability=capability):
                self.assertIn(capability, text)
        self.assertIn("pending synchronization", text)
        self.assertIn("never invent a tool", text)
        self.assertIn("does **not** add an OpenCode", text)
        self.assertIn("never transfers\ncredentials", text)

    def test_authored_skills_state_their_host_safe_boundaries(self):
        expected = {
            "hanig-project": "current session's real connector",
            "hanig-swarm": "Host capability boundary",
            "hanig-verified-workflow": "Host capability boundary",
            "hanig-review-gate": "Host capability boundary",
            "hanig-portable-handoff": "Host capability boundary",
        }
        for name, phrase in expected.items():
            with self.subTest(bundle=name):
                self.assertIn(phrase, (SKILLS / name / "SKILL.md").read_text())

    def test_vendored_sources_are_not_rewritten_on_this_branch(self):
        """The audit records limitations rather than patching upstream Markdown.

        Compares the working tree against origin/main and requires that any
        skill bundle it touches is one we author. A branch that touches no
        skill at all (including main itself) trivially satisfies this; the
        earlier form demanded the diff equal AUTHORED exactly, which could
        only hold for the one change that introduced the contract.
        """
        changed = subprocess.run(
            ["git", "diff", "--name-only", "origin/main", "--", "skills"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        touched = {Path(path).parts[1] for path in changed}
        self.assertEqual(
            touched & VENDORED, set(),
            "a portability edit must not rewrite vendored skill documents",
        )
        self.assertLessEqual(
            touched, AUTHORED,
            "a changed skill bundle must be listed in AUTHORED or VENDORED",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
