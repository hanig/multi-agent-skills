"""Contract tests for lifecycle staging, ownership, uninstall, and migration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
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

    def test_schema_two_parser_is_strict_but_normalizes_consumer_order(self):
        destination = self.home / ".claude" / "skills" / "alpha"
        destination.mkdir(parents=True)
        marker = destination / lifecycle.MARKER
        record = lifecycle.Provenance(
            repository=lifecycle.REPOSITORY_ID, origin="authored",
            source_version="v1", destination=str(destination.resolve()),
            consumers=(), mode="copy", installed_at="2026-09-05T00:00:00Z",
        )
        valid = lifecycle._serialize(record)
        marker.write_text(valid)
        self.assertEqual(lifecycle.read_provenance(destination).consumers, ())
        marker.write_text(valid.replace("consumers=\n", "consumers=pi,claude,pi\n"))
        self.assertEqual(lifecycle.read_provenance(destination).consumers,
                         ("claude", "pi"))

        malformed = {
            "invalid-utf8": valid.encode() + b"\xff\n",
            "duplicate": valid.replace("repo=multi-agent-skills\n",
                                       "repo=multi-agent-skills\nrepo=other\n"),
            "malformed-line": valid + "not-a-field\n",
            "missing-field": valid.replace("installed_at=2026-09-05T00:00:00Z\n", ""),
            "version-mismatch": valid.replace("\nversion=v1\n", "\nversion=v2\n"),
            "invalid-origin": valid.replace("origin=authored\n", "origin=unknown\n"),
            "relative-destination": valid.replace(
                f"destination={destination.resolve()}\n", "destination=relative/alpha\n"),
            "empty-consumer": valid.replace("consumers=\n", "consumers=claude,,pi\n"),
            "copy-link-fields": valid.replace("link_target=\n", "link_target=/tmp/source\n"),
            "link-without-identity": (valid.replace("mode=copy\n", "mode=link\n")
                                      .replace("link_target=\n", "link_target=/tmp/source\n")),
        }
        for label, content in malformed.items():
            with self.subTest(case=label):
                if isinstance(content, bytes):
                    marker.write_bytes(content)
                else:
                    marker.write_text(content)
                self.assertIsNone(lifecycle.read_provenance(destination))

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
            if (Path(source).parent.name.startswith(".alpha.stage-") and
                    Path(destination) == target.destination):
                raise OSError("simulated publish interruption")
            return real_replace(source, destination)

        with mock.patch.object(lifecycle.os, "replace", side_effect=fail_stage_publish):
            result = lifecycle.install([upgrade])
        self.assertEqual(result[0].status, "failed")
        self.assertTrue(result[0].predecessor_recoverable)
        self.assertEqual((target.destination / "SKILL.md").read_text(), old)
        self.assertFalse(list(target.destination.parent.glob(".alpha.stage-*")))

    def test_failed_predecessor_rename_leaves_old_install_untouched(self):
        target = self.target()
        lifecycle.install([target])
        old = (target.destination / "SKILL.md").read_text()
        upgrade = self.target(source=self.source("alpha", "two"), version="v2")
        real_replace = lifecycle.os.replace

        def fail_backup_rename(source, destination):
            if Path(source) == target.destination and Path(destination).name.startswith(".alpha.backup-"):
                raise OSError("simulated predecessor rename failure")
            return real_replace(source, destination)

        with mock.patch.object(lifecycle.os, "replace", side_effect=fail_backup_rename):
            result = lifecycle.install([upgrade])
        self.assertEqual(result[0].status, "failed")
        self.assertEqual((target.destination / "SKILL.md").read_text(), old)

    def test_backup_cleanup_failure_keeps_good_replacement_and_backup(self):
        target = self.target()
        lifecycle.install([target])
        upgrade = self.target(source=self.source("alpha", "two"), version="v2")
        real_rmtree = lifecycle.shutil.rmtree

        def fail_backup_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".alpha.backup-"):
                raise OSError("simulated cleanup failure")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(lifecycle.shutil, "rmtree", side_effect=fail_backup_cleanup):
            result = lifecycle.install([upgrade])
        self.assertEqual(result[0].status, "upgraded")
        self.assertTrue(result[0].predecessor_recoverable)
        self.assertEqual((target.destination / "SKILL.md").read_text().splitlines()[-1], "two")
        self.assertTrue(list(target.destination.parent.glob(".alpha.backup-*")))

    def test_repeated_upgrade_is_idempotent_and_leaves_no_staging(self):
        target = self.target(version="v1")
        self.assertEqual(lifecycle.install([target])[0].status, "installed")
        upgrade = self.target(source=self.source("alpha", "two"), version="v2")
        self.assertEqual(lifecycle.install([upgrade])[0].status, "upgraded")
        self.assertEqual(lifecycle.install([upgrade])[0].status, "upgraded")
        self.assertEqual((target.destination / "SKILL.md").read_text().splitlines()[-1], "two")
        self.assertFalse(list(target.destination.parent.glob(".alpha.stage-*")))
        self.assertFalse(list(target.destination.parent.glob(".alpha.backup-*")))

    def test_concurrent_installs_do_not_share_staging_or_corrupt_payload(self):
        target = self.target()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: lifecycle.install([target])[0], range(2)))
        self.assertTrue(any(result.status == "installed" for result in results))
        self.assertTrue(all(result.status in ("installed", "upgraded", "blocked") for result in results))
        self.assertEqual((target.destination / "SKILL.md").read_text().splitlines()[-1], "one")
        self.assertEqual(lifecycle.read_provenance(target.destination).repository, lifecycle.REPOSITORY_ID)
        self.assertFalse(list(target.destination.parent.glob(".alpha.stage-*")))

    def test_source_destination_alias_overlap_is_rejected_before_writes(self):
        source = self.source()
        alias = self.root / "checkout-alias"
        alias.symlink_to(source.parent, target_is_directory=True)
        before = (source / "SKILL.md").read_bytes()
        for mode in ("copy", "link"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "payloads overlap"):
                    self.target(destination=alias / source.name, mode=mode)
                with self.assertRaisesRegex(ValueError, "payloads overlap"):
                    self.target(destination=source / "nested", mode=mode)
                self.assertEqual((source / "SKILL.md").read_bytes(), before)
                self.assertFalse(source.is_symlink())
                self.assertFalse(list(source.parent.glob(".alpha.*")))

    def test_destination_parent_aliases_have_one_identity(self):
        source = self.source()
        physical = self.home / "physical" / "skills"
        physical.mkdir(parents=True)
        alias = self.home / "alias"
        alias.symlink_to(physical, target_is_directory=True)
        direct = self.target(source=source, destination=physical / "alpha")
        indirect = self.target(source=source, destination=alias / "alpha")
        self.assertEqual(direct.destination, indirect.destination)
        inspection = lifecycle.preflight([direct, indirect])
        self.assertEqual([item.action for item in inspection], ["install", "blocked"])
        self.assertIn("one destination", inspection[1].reason)
        self.assertFalse((physical / "alpha").exists())

    def test_foreign_final_symlink_target_is_not_followed_on_replacement(self):
        source = self.source()
        foreign = self.root / "foreign"
        foreign.mkdir()
        sentinel = foreign / "sentinel.txt"
        sentinel.write_text("do not touch\n")
        for mode in ("copy", "link"):
            with self.subTest(mode=mode):
                destination = self.home / mode / "alpha"
                destination.parent.mkdir(parents=True)
                destination.symlink_to(foreign, target_is_directory=True)
                target = lifecycle.LifecycleTarget(
                    name="alpha", source=source, destination=destination,
                    origin="authored", mode=mode, source_version="test",
                    allow_foreign_replace=True,
                )
                self.assertEqual(
                    target.destination,
                    Path(os.path.realpath(destination.parent)) / destination.name,
                )
                result = lifecycle.install([target])[0]
                self.assertEqual(result.status, "upgraded")
                self.assertEqual(sentinel.read_text(), "do not touch\n")
                self.assertTrue(foreign.is_dir())
                if mode == "copy":
                    self.assertTrue(destination.is_dir())
                    self.assertFalse(destination.is_symlink())
                else:
                    self.assertTrue(destination.is_symlink())
                    self.assertEqual(os.path.realpath(destination), str(source.resolve()))

    def test_upgrade_unions_prior_recorded_consumers(self):
        target = self.target(consumers=("claude", "codex"))
        lifecycle.install([target])
        upgrade = self.target(source=self.source("alpha", "two"), consumers=("claude",), version="v2")
        self.assertEqual(lifecycle.install([upgrade])[0].status, "upgraded")
        self.assertEqual(lifecycle.read_provenance(target.destination).consumers, ("claude", "codex"))

    def test_foreign_replacement_does_not_import_unproven_consumers(self):
        source = self.source()
        destination = self.home / ".claude" / "skills" / "alpha"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("foreign\n")
        forged = lifecycle.Provenance(
            repository=lifecycle.REPOSITORY_ID, origin="authored",
            source_version="foreign", destination=str(self.root / "elsewhere" / "alpha"),
            consumers=("pi",), mode="copy", installed_at="2026-09-05T00:00:00Z",
        )
        lifecycle.write_provenance(destination, forged)
        target = lifecycle.LifecycleTarget(
            name="alpha", source=source, destination=destination,
            origin="authored", consumers=("claude",), source_version="owned",
            allow_foreign_replace=True,
        )
        inspection = lifecycle.preflight([target])[0]
        self.assertEqual(inspection.action, "upgrade")
        self.assertFalse(inspection.previous_owned)
        self.assertEqual(lifecycle.install([target])[0].status, "upgraded")
        record = lifecycle.read_provenance(destination)
        self.assertEqual(record.consumers, ("claude",))
        self.assertEqual(lifecycle.uninstall(
            [destination], consumers=("claude",),
        )[0].status, "removed")

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
        self.assertTrue(lifecycle.read_provenance(target.destination).link_identity)
        shutil.rmtree(target.source.parent)
        self.assertEqual(lifecycle.uninstall([target.destination])[0].status, "removed")
        self.assertFalse(target.destination.exists())
        self.assertFalse(sidecar.exists())

    def test_link_never_overwrites_an_unowned_or_dangling_sidecar(self):
        target = self.target(mode="link")
        sidecar = lifecycle.provenance_path(target.destination)
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("repo=another-installer\n")
        result = lifecycle.install([target])
        self.assertEqual(result[0].status, "blocked")
        self.assertFalse(target.destination.exists())
        self.assertIn("another-installer", sidecar.read_text())

    def test_failed_link_identity_write_restores_owned_predecessor(self):
        target = self.target(mode="link", version="v1")
        self.assertEqual(lifecycle.install([target])[0].status, "installed")
        upgrade = self.target(source=self.source("alpha", "two"), mode="link",
                              version="v2")
        real_write = lifecycle.write_provenance

        def fail_new_identity(destination, record, **kwargs):
            if record.source_version == "v2":
                raise OSError("simulated sidecar identity write failure")
            return real_write(destination, record, **kwargs)

        with mock.patch.object(lifecycle, "write_provenance",
                               side_effect=fail_new_identity):
            result = lifecycle.install([upgrade])
        self.assertEqual(result[0].status, "failed")
        self.assertTrue(target.destination.is_symlink())
        restored = lifecycle.read_provenance(target.destination)
        self.assertEqual(restored.source_version, "v1")
        self.assertEqual(lifecycle.preflight([target])[0].action, "upgrade")
        self.assertEqual(lifecycle.uninstall([target.destination])[0].status,
                         "removed")

    def test_link_to_copy_transition_removes_sidecar_and_allows_fresh_link(self):
        linked = self.target(mode="link", version="v1")
        self.assertEqual(lifecycle.install([linked])[0].status, "installed")
        sidecar = lifecycle.provenance_path(linked.destination)
        self.assertTrue(sidecar.is_file())
        copied = self.target(source=self.source("alpha", "two"), mode="copy",
                             version="v2")
        self.assertEqual(lifecycle.install([copied])[0].status, "upgraded")
        self.assertFalse(sidecar.exists())
        self.assertFalse(copied.destination.is_symlink())
        self.assertEqual(lifecycle.uninstall([copied.destination])[0].status,
                         "removed")
        self.assertFalse(sidecar.exists())
        fresh = self.target(source=linked.source, mode="link", version="v3")
        self.assertEqual(lifecycle.preflight([fresh])[0].action, "install")
        self.assertEqual(lifecycle.install([fresh])[0].status, "installed")

    def test_failed_link_to_copy_transition_restores_link_and_sidecar(self):
        linked = self.target(mode="link", version="v1")
        self.assertEqual(lifecycle.install([linked])[0].status, "installed")
        sidecar = lifecycle.provenance_path(linked.destination)
        old_record = lifecycle.read_provenance(linked.destination)
        copied = self.target(source=self.source("alpha", "two"), mode="copy",
                             version="v2")
        real_replace = lifecycle.os.replace

        def fail_copy_publish(source, destination):
            if (Path(source).parent.name.startswith(".alpha.stage-") and
                    Path(destination) == copied.destination):
                raise OSError("simulated copy publish failure")
            return real_replace(source, destination)

        with mock.patch.object(lifecycle.os, "replace",
                               side_effect=fail_copy_publish):
            result = lifecycle.install([copied])[0]
        self.assertEqual(result.status, "failed")
        self.assertTrue(linked.destination.is_symlink())
        self.assertTrue(sidecar.is_file())
        restored = lifecycle.read_provenance(linked.destination)
        self.assertEqual(restored.source_version, old_record.source_version)
        self.assertEqual(restored.consumers, old_record.consumers)
        self.assertEqual(lifecycle.preflight([linked])[0].action, "upgrade")
        self.assertFalse(list(sidecar.parent.glob(".alpha.sidecar-backup-*")))

    def test_stale_link_sidecar_cannot_authorize_replaced_foreign_link(self):
        target = self.target(mode="link")
        lifecycle.install([target])
        foreign = self.source("foreign")
        target.destination.unlink()
        target.destination.symlink_to(foreign, target_is_directory=True)
        self.assertEqual(lifecycle.preflight([target])[0].action, "blocked")
        self.assertEqual(lifecycle.uninstall([target.destination])[0].status, "blocked")
        self.assertEqual(os.path.realpath(target.destination), os.path.realpath(foreign))

    def test_recreated_identical_link_does_not_reuse_old_sidecar_ownership(self):
        target = self.target(mode="link")
        lifecycle.install([target])
        target.destination.unlink()
        target.destination.symlink_to(target.source, target_is_directory=True)
        self.assertEqual(lifecycle.preflight([target])[0].action, "blocked")
        self.assertEqual(lifecycle.uninstall([target.destination])[0].status, "blocked")

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
