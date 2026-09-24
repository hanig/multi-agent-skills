"""Portable skill metadata and capability-contract regression tests.

These tests intentionally install into a disposable prefix and HOME-equivalent
directory. They test loader artifacts and documented fallbacks; they do not
try to provision a host agent, connector, credential, or optional daemon.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
COMPATIBILITY = ROOT / "docs" / "agent-compatibility.md"
AUTHORED = {
    "hanig-orchestrate",
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
VENDORED_FILES = {
    "skills/agent-bus/SKILL.md",
    "skills/paseo/SKILL.md",
    "skills/paseo-advisor/SKILL.md",
    "skills/paseo-committee/SKILL.md",
    "skills/paseo-handoff/SKILL.md",
    "skills/paseo-loop/SKILL.md",
    "skills/pi-fleet/SKILL.md",
    "skills/start-a-sprint/SKILL.md",
    "skills/start-a-sprint/agents/openai.yaml",
    "skills/start-a-sprint/scripts/validate_sprint_plan.py",
}
BUS_EXCEPTION = "bin/bus"
VENDORED_MANIFEST_GUARD_BOUND = (
    "observed inventory and byte equality apply to a stable vendored tree at "
    "the checker's observation points; this check does not provide OS "
    "isolation or detect a same-UID writer that adds a file after its one-shot "
    "inventory walk"
)


def _frontmatter(path):
    match = re.match(r"\A---\n(.*?)\n---\n", path.read_text(), re.DOTALL)
    if not match:
        return None
    return match.group(1)


def _read_regular(relative):
    """Read a checker-owned path without following any symlink component."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(ROOT, directory_flags)
    file_fd = None
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        file_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise OSError(f"{relative} is not a regular file")
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = None
            return handle.read()
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _sha256(relative):
    return hashlib.sha256(_read_regular(relative)).hexdigest()


def _collect_regular_files(directory_fd, relative, result):
    """Collect regular files beneath an already-open directory."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    for name in os.listdir(directory_fd):
        mode = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False,
        ).st_mode
        child = relative / name
        if stat.S_ISREG(mode):
            result.add(child.as_posix())
        elif stat.S_ISDIR(mode):
            child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
            try:
                _collect_regular_files(child_fd, child, result)
            finally:
                os.close(child_fd)


def _vendored_regular_files():
    """Return the regular-file inventory of every vendored skill tree."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(ROOT, directory_flags)
    skills_fd = None
    try:
        skills_fd = os.open("skills", directory_flags, dir_fd=root_fd)
        result = set()
        for name in sorted(VENDORED):
            directory_fd = os.open(name, directory_flags, dir_fd=skills_fd)
            try:
                _collect_regular_files(
                    directory_fd, Path("skills") / name, result,
                )
            finally:
                os.close(directory_fd)
        return result
    finally:
        if skills_fd is not None:
            os.close(skills_fd)
        os.close(root_fd)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


class TestSkillCapabilities(unittest.TestCase):
    def test_all_fourteen_bundles_have_portable_loader_metadata(self):
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
            "hanig-orchestrate": "Host capability boundary",
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
        available = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "origin/main^{commit}"],
            cwd=ROOT, text=True, capture_output=True,
        )
        if available.returncode != 0:
            self.skipTest("origin/main is unavailable; offline manifest guard still ran")
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

    def test_vendored_manifest_guard_states_stable_tree_limit(self):
        bound = VENDORED_MANIFEST_GUARD_BOUND
        self.assertIn("stable vendored tree", bound)
        self.assertIn("does not provide OS isolation", bound)
        self.assertIn("same-UID writer", bound)
        self.assertIn("after its one-shot inventory walk", bound)

    def test_vendored_payload_matches_offline_manifest(self):
        """Hash one stable vendored tree without Git or a subprocess.

        This checker establishes inventory and byte equality at its observation
        points. It does not provide OS isolation or detect a same-UID writer
        that adds a file after its one-shot inventory walk.
        """
        manifest = json.loads(
            _read_regular("docs/upstream-manifest.json").decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
        notice = " ".join(manifest.get("_notice", []))
        self.assertIn("DATA, NOT AUTHORITY", notice)
        self.assertIn("not evidence of upstream provenance", notice)
        self.assertEqual(manifest.get("schema_version"), 1)
        self.assertEqual(manifest.get("algorithm"), "sha256")
        self.assertEqual(
            manifest.get("vendored_skill_directories"),
            sorted(VENDORED),
            "manifest directory inventory must match the installer's non-hanig- classifier",
        )

        files = manifest.get("files")
        exceptions = manifest.get("expected_exceptions")
        self.assertIsInstance(files, dict)
        self.assertIsInstance(exceptions, dict)
        self.assertEqual(
            set(files), VENDORED_FILES,
            "manifest file inventory must match the shipped vendored snapshot",
        )
        self.assertEqual(
            _vendored_regular_files(), set(files),
            VENDORED_MANIFEST_GUARD_BOUND,
        )
        self.assertEqual(
            set(exceptions), {BUS_EXCEPTION},
            "bin/bus must remain an explicit expected exception for ARC-270",
        )
        digest_pattern = re.compile(r"\A[0-9a-f]{64}\Z")
        for relative in sorted(VENDORED_FILES):
            with self.subTest(path=relative):
                expected = files[relative]
                self.assertIsInstance(expected, str)
                self.assertRegex(expected, digest_pattern)
                self.assertEqual(
                    _sha256(relative),
                    expected,
                    f"{relative}: digest differs from the shipped vendored bytes",
                )

        record = exceptions[BUS_EXCEPTION]
        self.assertIsInstance(record, dict)
        self.assertEqual(set(record), {"sha256", "upstream_sha256", "reason"})
        self.assertRegex(record.get("sha256", ""), digest_pattern)
        self.assertRegex(record.get("upstream_sha256", ""), digest_pattern)
        self.assertNotEqual(record["sha256"], record["upstream_sha256"])
        self.assertIn("ARC-270", record.get("reason", ""))
        actual = _sha256(BUS_EXCEPTION)
        if actual == record["upstream_sha256"]:
            self.fail(
                f"{BUS_EXCEPTION}: tracked expected exception disappeared; "
                "the ARC-270 patch was dropped"
            )
        self.assertEqual(
            actual,
            record["sha256"],
            f"{BUS_EXCEPTION}: digest differs from its expected exception",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
