"""ARC-692: an observed successor fences a stale coordinator's next save."""

import errno
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
SWARM = SCRIPTS / "swarm.py"
HALT = "another coordinator has written state under us"


class TestStateEpoch(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.first = self.coordinator()
        self.path = self.state_dir / self.first.STATE_FILE
        self.epoch_path = self.state_dir / self.first.STATE_EPOCH_FILE

    def coordinator(self):
        # Independent module objects stand in for process-local lease memory.
        spec = importlib.util.spec_from_file_location("epoch_coordinator", SWARM)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.addCleanup(self.release_all, module)
        return module

    @staticmethod
    def release_all(module):
        for key in list(module._LOCK_FDS):
            module.release_lease(key)

    def acquire(self, module, directory=None):
        ok, reason = module.acquire_lease(directory or self.state_dir)
        self.assertTrue(ok, reason)

    def read(self):
        return json.loads(self.path.read_text())

    def read_epoch(self):
        return json.loads(self.epoch_path.read_text())["epoch"]

    def test_000_superseded_writer_refuses_and_preserves_successor(self):
        second = self.coordinator()
        # Bypass ONLY flock: both objects hold real descriptors to the same
        # inode, with independent acquisition and save paths otherwise intact.
        with mock.patch("fcntl.flock"):
            self.acquire(self.first)
            stale = self.first.load_state(self.state_dir)
            stale["writer"] = "first"
            self.first.save_state(self.state_dir, stale)
            self.assertEqual(self.read_epoch(), 1)

            self.acquire(second)
            successor = second.load_state(self.state_dir)
            successor["writer"] = "second"
            second.save_state(self.state_dir, successor)
            self.assertEqual(self.read_epoch(), 2)
            intact = self.path.read_bytes()

            with self.assertRaisesRegex(SystemExit, HALT):
                self.first.save_state(self.state_dir, stale)
            self.assertEqual(self.path.read_bytes(), intact)
            # Neither editing nor reloading the snapshot refreshes the pin.
            for snapshot in (dict(stale, epoch=2),
                             self.first.load_state(self.state_dir)):
                with self.assertRaisesRegex(SystemExit, HALT):
                    self.first.save_state(self.state_dir, snapshot)
                self.assertEqual(self.path.read_bytes(), intact)

    def test_nested_acquire_keeps_epoch_and_sequential_acquire_increments(self):
        self.acquire(self.first)
        state = self.first.load_state(self.state_dir)
        intact = self.epoch_path.read_bytes()
        self.acquire(self.first)
        self.assertEqual(self.epoch_path.read_bytes(), intact)
        self.assertFalse(self.path.exists())
        self.first.save_state(self.state_dir, state)
        self.first.release_lease(self.state_dir)
        self.acquire(self.first)
        intact = self.path.read_bytes()
        with self.assertRaisesRegex(SystemExit, HALT):
            self.first.save_state(self.state_dir, state)
        self.assertEqual(self.path.read_bytes(), intact)
        self.first.save_state(self.state_dir,
                              self.first.load_state(self.state_dir))
        self.assertNotIn("epoch", state)
        self.assertEqual(self.read_epoch(), 2)

    def test_epoch_pins_are_per_state_directory(self):
        other = self.root / "other"
        self.acquire(self.first)
        self.first.release_lease(self.state_dir)
        self.acquire(self.first)
        self.acquire(self.first, other)
        self.first.save_state(other, self.first.load_state(other))
        self.first.save_state(self.state_dir,
                              self.first.load_state(self.state_dir))
        self.assertEqual(self.read_epoch(), 2)
        self.assertEqual(json.loads((other / self.first.STATE_EPOCH_FILE).read_text())["epoch"], 1)

    def test_legacy_state_loads_and_advances_without_losing_fields(self):
        legacy = {"schema_version": 1, "units": {}, "halted": None,
                  "preserved": {"receipt": "old"}}
        self.path.write_text(json.dumps(legacy))
        self.assertEqual(self.first.load_state(self.state_dir), legacy)
        self.acquire(self.first)
        state = self.first.load_state(self.state_dir)
        report, dispatched, halted = self.first.advance(
            {"name": "legacy", "units": []}, state, self.state_dir,
            str(self.root / "runs"), False, max_new=0)
        self.assertEqual(dispatched, 0, report)
        self.assertIsNone(halted, report)
        self.assertEqual(self.read_epoch(), 1)
        self.assertEqual(self.read()["preserved"], legacy["preserved"])

    def cli(self, command, *extra):
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"name": "epoch", "units": [
            {"id": "u", "kind": "slurm", "runtime": "none",
             "command": "true", "outputs": ["out"]}]}))
        return subprocess.run(
            [sys.executable, str(SWARM), command, str(plan),
             "--state-dir", str(self.state_dir), *extra],
            cwd=self.root, capture_output=True, text=True, timeout=30)

    def test_status_preserves_legacy_and_epoch_state_bytes(self):
        for epoch in (None, 7):
            with self.subTest(epoch=epoch):
                state = {"schema_version": 1, "units": {}, "halted": None}
                if epoch is not None:
                    self.epoch_path.write_text(json.dumps({"epoch": epoch}))
                epoch_bytes = (self.epoch_path.read_bytes()
                               if self.epoch_path.exists() else None)
                self.path.write_text(json.dumps(state) + "\n")
                intact = self.path.read_bytes()
                result = self.cli("status", "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.path.read_bytes(), intact)
                self.assertFalse((self.state_dir / self.first.LOCK).exists())
                self.assertEqual(
                    self.epoch_path.read_bytes() if self.epoch_path.exists() else None,
                    epoch_bytes)

    def test_status_does_not_create_state(self):
        result = self.cli("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.epoch_path.exists())

    def test_sequential_dry_run_commands_each_use_their_own_epoch(self):
        for command, epoch in (("run", 1), ("advance", 2)):
            result = self.cli(command, "--dry-run", "--root",
                              str(self.root / "runs"))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.read_epoch(), epoch)

    def test_real_child_acquisition_after_parent_release(self):
        self.acquire(self.first)
        self.first.release_lease(self.state_dir)
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import swarm as S; "
             "d=sys.argv[2]; assert S.acquire_lease(d)[0]; "
             "s=S.load_state(d); s['child']=True; S.save_state(d,s); "
             "S.release_lease(d)", str(SCRIPTS), str(self.state_dir)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_epoch(), 2)
        self.assertTrue(self.read()["child"])
        self.acquire(self.first)
        self.first.save_state(self.state_dir,
                              self.first.load_state(self.state_dir))
        self.assertEqual(self.read_epoch(), 3)

    def test_contended_acquire_does_not_bump(self):
        self.acquire(self.first)
        intact = self.epoch_path.read_bytes()
        second = self.coordinator()
        with mock.patch("fcntl.flock", side_effect=OSError(errno.EAGAIN, "busy")):
            ok, _ = second.acquire_lease(self.state_dir)
        self.assertFalse(ok)
        self.assertEqual(self.epoch_path.read_bytes(), intact)
        self.assertFalse(self.path.exists())

    def test_invalid_epoch_cannot_be_acquired_or_saved(self):
        self.path.write_text(json.dumps({"units": {}, "preserved": True}))
        state_bytes = self.path.read_bytes()
        records = [{"epoch": value} for value in (-1, True, 1.0, "1", None)]
        records.extend([{}, [], None, 1])
        for record in records:
            with self.subTest(record=record):
                self.epoch_path.write_text(json.dumps(record))
                intact = self.epoch_path.read_bytes()
                with self.assertRaisesRegex(SystemExit, "invalid.*epoch"):
                    self.first.acquire_lease(self.state_dir)
                self.assertFalse(self.first._LOCK_FDS)
                with self.assertRaisesRegex(SystemExit, HALT):
                    self.first.save_state(self.state_dir, {"units": {}})
                self.assertEqual(self.epoch_path.read_bytes(), intact)
                self.assertEqual(self.path.read_bytes(), state_bytes)

    def test_epoch_write_failure_releases_descriptor_and_does_not_acquire(self):
        with mock.patch.object(self.first.U, "write_json", return_value="disk full"):
            with self.assertRaisesRegex(SystemExit, "cannot persist.*epoch"):
                self.first.acquire_lease(self.state_dir)
        self.assertFalse(self.first._LOCK_FDS)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.epoch_path.exists())
        self.acquire(self.first)
        self.assertEqual(self.read_epoch(), 1)

    def test_missing_or_unreadable_epoch_cannot_be_overwritten_by_holder(self):
        self.acquire(self.first)
        state = self.first.load_state(self.state_dir)
        self.first.save_state(self.state_dir, state)
        intact = self.path.read_bytes()
        self.epoch_path.unlink()
        with self.assertRaisesRegex(SystemExit, HALT):
            self.first.save_state(self.state_dir, state)
        self.assertFalse(self.epoch_path.exists())
        self.assertEqual(self.path.read_bytes(), intact)
        self.epoch_path.write_text("{broken")
        with self.assertRaisesRegex(SystemExit, "unreadable"):
            self.first.save_state(self.state_dir, state)
        self.assertEqual(self.epoch_path.read_text(), "{broken")
        self.assertEqual(self.path.read_bytes(), intact)

    def test_unleased_helper_pins_on_first_save_and_refuses_a_later_successor(self):
        self.acquire(self.first)
        self.first.release_lease(self.state_dir)
        helper = self.coordinator()
        state = helper.load_state(self.state_dir)
        state["helper"] = True
        helper.save_state(self.state_dir, state)
        self.assertEqual(self.read_epoch(), 1)
        self.assertFalse(helper.renew_lease(self.state_dir))
        self.assertNotIn("epoch", self.read())
        self.acquire(self.first)
        intact = self.path.read_bytes()
        with self.assertRaisesRegex(SystemExit, HALT):
            helper.save_state(self.state_dir, state)
        self.assertEqual(self.path.read_bytes(), intact)

    def test_forked_child_cannot_use_parent_epoch_pin(self):
        self.acquire(self.first)
        state = self.first.load_state(self.state_dir)
        self.first.save_state(self.state_dir, state)
        intact = self.path.read_bytes()
        pid = os.fork()
        if pid == 0:
            try:
                self.first.save_state(self.state_dir, state)
            except SystemExit as exc:
                os._exit(0 if HALT in str(exc) else 2)
            os._exit(1)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(status, 0)
        self.assertEqual(self.path.read_bytes(), intact)

    def test_noop_lease_acquisition_preserves_state_bytes_and_mtime(self):
        original = b'{ "units": {}, "halted": null, "preserved": "legacy" }\n'
        self.path.write_bytes(original)
        os.utime(self.path, ns=(1_000_000_000, 1_000_000_000))
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        for epoch in (1, 2):
            self.acquire(self.first)
            self.first.release_lease(self.state_dir)
            self.assertEqual(self.read_epoch(), epoch)
            self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns),
                             before)

    def test_acquire_and_save_fence_never_load_coordinator_state(self):
        with mock.patch.object(self.first, "load_state", side_effect=
                               AssertionError("fence loaded coordinator state")):
            self.acquire(self.first)
            self.assertFalse(self.path.exists())
            self.first.save_state(self.state_dir, {"units": {}, "halted": None})
        self.assertNotIn("epoch", self.read())
        self.assertEqual(self.read_epoch(), 1)

    def test_obsolete_inline_epoch_is_removed_only_on_normal_save(self):
        legacy = {"schema_version": 1, "units": {}, "halted": None,
                  "epoch": 99, "preserved": {"receipt": "old"}}
        self.path.write_text(json.dumps(legacy))
        intact = self.path.read_bytes()
        self.assertEqual(self.first._read_state_epoch(self.state_dir), (0, None))
        self.acquire(self.first)
        self.assertEqual(self.path.read_bytes(), intact)
        self.assertEqual(self.read_epoch(), 1)
        state = self.first.load_state(self.state_dir)
        self.first.save_state(self.state_dir, state)
        del legacy["epoch"]
        self.assertEqual(self.read(), legacy)
        self.assertEqual(state, legacy)

    def test_unleased_legacy_save_does_not_create_epoch_file(self):
        self.first.save_state(self.state_dir, {"units": {}, "halted": None})
        self.assertFalse(self.epoch_path.exists())
        self.assertNotIn("epoch", self.read())
        self.assertFalse(self.first.renew_lease(self.state_dir))


if __name__ == "__main__":
    unittest.main()
