"""Contract tests for lifecycle staging, ownership, uninstall, and migration."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib import skill_lifecycle as lifecycle  # noqa: E402


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home"
        self.home.mkdir()

    def source(self, name="alpha", body="one"):
        path = self.root / "checkout" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n")
        return path

    def target(self, name="alpha", *, source=None, destination=None, consumers=("claude",),
               origin="authored", mode="copy", version="v1"):
        return lifecycle.LifecycleTarget(
            name=name, source=source or self.source(name),
            destination=destination or self.home / ".claude" / "skills" / name,
            origin=origin, consumers=consumers, mode=mode, source_version=version,
        )

    def test_writes_complete_provenance_compatible_with_marker_reader(self):
        target = self.target(consumers=("codex", "claude"), version="abc123")
        result = lifecycle.install([target])
        self.assertEqual(result[0].status, "installed")
        record = lifecycle.read_provenance(target.destination)
        self.assertEqual(record.repository, lifecycle.REPOSITORY_ID)
        self.assertEqual(record.origin, "authored")
        self.assertEqual(record.source_version, "abc123")
        self.assertEqual(record.destination, str(target.destination))
        self.assertEqual(record.consumers, ("claude", "codex"))
        marker = (target.destination / lifecycle.MARKER).read_text()
        self.assertIn("version=abc123\n", marker)
        self.assertIn("schema=2\n", marker)

    def test_foreign_and_legacy_markers_are_not_promoted_to_authored(self):
        dest = self.home / ".claude" / "skills" / "alpha"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("---\nname: alpha\n---\n")
        (dest / lifecycle.MARKER).write_text("repo=multi-agent-skills\norigin=vendored\nversion=old\n")
        record = lifecycle.read_provenance(dest)
        self.assertTrue(record.legacy)
        self.assertEqual(record.origin, "unknown")
        self.assertEqual(lifecycle.preflight([self.target(destination=dest)])[0].action, "upgrade")
        self.assertEqual(lifecycle.uninstall([dest])[0].status, "blocked")
        foreign = self.home / ".claude" / "skills" / "foreign"
        foreign.mkdir()
        (foreign / "SKILL.md").write_text("---\nname: foreign\n---\n")
        item = lifecycle.preflight([self.target("foreign", destination=foreign)])[0]
        self.assertEqual(item.action, "blocked")

    def test_failed_copy_stages_before_touching_existing_destination(self):
        target = self.target()
        self.assertEqual(lifecycle.install([target])[0].status, "installed")
        old = (target.destination / "SKILL.md").read_text()
        upgrade = self.target(source=self.source("alpha", "two"), version="v2")
        with mock.patch.object(lifecycle.shutil, "copytree", side_effect=OSError("disk full")):
            result = lifecycle.install([upgrade])
        self.assertEqual(result[0].status, "failed")
        self.assertIn("staging failed", result[0].detail)
        self.assertEqual((target.destination / "SKILL.md").read_text(), old)

    def test_failed_publish_restores_predecessor(self):
        target = self.target()
        lifecycle.install([target])
        old = (target.destination / "SKILL.md").read_text()
        upgrade = self.target(source=self.source("alpha", "two"), version="v2")
        real_replace = lifecycle.os.replace

        def fail_stage_publish(source, destination):
            if Path(source).name.startswith(".alpha.stage-") and Path(destination) == target.destination:
                raise OSError("simulated publish interruption")
            return real_replace(source, destination)

        with mock.patch.object(lifecycle.os, "replace", side_effect=fail_stage_publish):
            result = lifecycle.install([upgrade])
        self.assertEqual(result[0].status, "failed")
        self.assertTrue(result[0].predecessor_recoverable)
        self.assertEqual((target.destination / "SKILL.md").read_text(), old)
        self.assertFalse(list(target.destination.parent.glob(".alpha.stage-*")))

    def test_repeated_upgrade_is_idempotent_and_leaves_no_staging(self):
        target = self.target(version="v1")
        self.assertEqual(lifecycle.install([target])[0].status, "installed")
        upgrade = self.target(source=self.source("alpha", "two"), version="v2")
        self.assertEqual(lifecycle.install([upgrade])[0].status, "upgraded")
        self.assertEqual(lifecycle.install([upgrade])[0].status, "upgraded")
        self.assertEqual((target.destination / "SKILL.md").read_text().splitlines()[-1], "two")
        self.assertFalse(list(target.destination.parent.glob(".alpha.stage-*")))
        self.assertFalse(list(target.destination.parent.glob(".alpha.backup-*")))

    def test_selective_uninstall_retains_shared_payload_for_other_consumer(self):
        target = self.target(consumers=("claude", "codex"))
        lifecycle.install([target])
        first = lifecycle.uninstall([target.destination], consumers=("codex",))
        self.assertEqual(first[0].status, "retained-shared")
        self.assertTrue(target.destination.is_dir())
        self.assertEqual(lifecycle.read_provenance(target.destination).consumers, ("claude",))
        second = lifecycle.uninstall([target.destination], consumers=("claude",))
        self.assertEqual(second[0].status, "removed")
        self.assertFalse(target.destination.exists())

    def test_selective_uninstall_never_touches_unselected_or_vendored_paths(self):
        alpha = self.target("alpha")
        beta = self.target("beta", source=self.source("beta"), destination=self.home / ".claude" / "skills" / "beta")
        vendored = self.target("vendor", source=self.source("vendor"), destination=self.home / ".claude" / "skills" / "vendor", origin="vendored")
        lifecycle.install([alpha, beta, vendored])
        self.assertEqual(lifecycle.uninstall([alpha.destination])[0].status, "removed")
        self.assertTrue(beta.destination.exists())
        self.assertEqual(lifecycle.uninstall([vendored.destination])[0].status, "retained")
        self.assertEqual(lifecycle.uninstall([vendored.destination], include_vendored=True)[0].status, "removed")

    def test_missing_source_is_a_preflight_block_without_writes(self):
        target = self.target(source=self.root / "gone")
        item = lifecycle.preflight([target])[0]
        self.assertEqual(item.action, "blocked")
        self.assertIn("unavailable", item.reason)
        self.assertEqual(lifecycle.install([target])[0].status, "blocked")
        self.assertFalse(target.destination.parent.exists())

    def test_link_uses_sidecar_and_uninstalls_without_source_checkout(self):
        target = self.target(mode="link")
        self.assertEqual(lifecycle.install([target])[0].status, "installed")
        sidecar = lifecycle.provenance_path(target.destination)
        self.assertTrue(target.destination.is_symlink())
        self.assertTrue(sidecar.is_file())
        shutil.rmtree(target.source.parent)
        self.assertEqual(lifecycle.uninstall([target.destination])[0].status, "removed")
        self.assertFalse(target.destination.exists())
        self.assertFalse(sidecar.exists())

    def test_migration_plan_is_dry_run_and_never_deletes_legacy_copy(self):
        legacy = self.home / ".claude" / "skills" / "alpha"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("---\nname: alpha\n---\n")
        plan = lifecycle.plan_migration(
            legacy_roots_by_consumer={"claude": self.home / ".claude" / "skills"},
            destination_for=lambda consumer, name, source: self.home / ".codex" / "skills" / name,
            selected_names=("alpha",),
        )
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].status, "ready")
        self.assertTrue(legacy.exists())
        self.assertFalse(plan[0].destination.exists())
        roots = lifecycle.legacy_roots(self.home)
        self.assertEqual(roots["codex"], self.home / ".codex" / "skills")


if __name__ == "__main__":
    unittest.main()
