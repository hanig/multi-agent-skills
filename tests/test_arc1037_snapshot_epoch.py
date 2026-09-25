"""ARC-1037: loaded snapshots cannot cross a lease acquisition boundary."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/hanig-swarm/scripts"
sys.path.insert(0, str(SCRIPTS))
HALT = "another coordinator has written state under us"


class TestSnapshotEpoch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "state"
        spec = importlib.util.spec_from_file_location(
            "snapshot_coordinator", SCRIPTS / "swarm.py")
        self.swarm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.swarm)
        self.addCleanup(self.release_all)
        self.path = self.directory / self.swarm.STATE_FILE
        self.epoch_path = self.directory / self.swarm.STATE_EPOCH_FILE

    def release_all(self):
        for key in list(self.swarm._LOCK_FDS):
            self.swarm.release_lease(key)

    def acquire(self, directory=None):
        ok, why = self.swarm.acquire_lease(directory or self.directory)
        self.assertTrue(ok, why)

    def load(self):
        return self.swarm.load_state(self.directory)

    def save(self, state):
        self.swarm.save_state(self.directory, state)

    def disk(self):
        return {path.name: path.read_bytes()
                for path in self.directory.iterdir() if path.is_file()}

    def refuse_without_writing(self, state):
        before = self.disk()
        with self.assertRaisesRegex(SystemExit, HALT):
            self.save(state)
        self.assertEqual(self.disk(), before)

    def test_000_reacquire_refuses_old_snapshot_reload_then_succeeds(self):
        self.acquire()
        stale = self.load()
        stale["value"] = "epoch one"
        self.save(stale)
        self.assertEqual(json.loads(self.epoch_path.read_text())["epoch"], 1)
        self.swarm.release_lease(self.directory)
        self.acquire()
        self.assertEqual(json.loads(self.epoch_path.read_text())["epoch"], 2)
        stale["value"] = "stale overwrite"
        self.refuse_without_writing(stale)
        current = self.load()
        self.assertEqual(current["value"], "epoch one")
        current["value"] = "fresh overwrite"
        self.save(current)
        self.assertEqual(json.loads(self.path.read_text())["value"],
                         "fresh overwrite")

    def test_unsaved_initial_snapshot_also_requires_reload(self):
        self.acquire()
        stale = self.load()
        self.swarm.release_lease(self.directory)
        self.acquire()
        self.refuse_without_writing(stale)
        self.assertFalse(self.path.exists())
        self.save(self.load())
        self.assertTrue(self.path.exists())

    def test_fresh_load_and_save_do_not_refresh_older_snapshots(self):
        self.acquire()
        old = [self.load(), self.load()]
        self.save(old[0])
        self.swarm.release_lease(self.directory)
        self.acquire()
        fresh = self.load()
        fresh["successor"] = "retained"
        self.save(fresh)
        for stale in old:
            stale["epoch"] = 2
            self.refuse_without_writing(stale)
            self.assertEqual(stale["epoch"], 2)

    def test_snapshot_copies_retain_the_original_epoch(self):
        self.acquire()
        stale = self.load()
        self.save(stale)
        copies = [stale.copy(), copy.copy(stale), copy.deepcopy(stale)]
        self.swarm.release_lease(self.directory)
        self.acquire()
        copies += [stale.copy(), copy.copy(stale), copy.deepcopy(stale)]
        for snapshot in copies:
            with self.subTest(type=type(snapshot).__name__):
                self.refuse_without_writing(snapshot)

    def test_nested_acquisition_and_same_epoch_copies_still_save(self):
        self.acquire()
        state = self.load()
        self.acquire()
        for snapshot in (state, state.copy(), copy.copy(state),
                         copy.deepcopy(state)):
            snapshot["ordinary"] = True
            self.save(snapshot)
        self.assertTrue(json.loads(self.path.read_text())["ordinary"])
        self.assertEqual(json.loads(self.epoch_path.read_text())["epoch"], 1)

    def test_equal_epoch_from_another_directory_is_not_the_same_snapshot(self):
        self.acquire()
        self.save(self.load())
        other = self.root / "other"
        self.acquire(other)
        state = self.swarm.load_state(other)
        self.refuse_without_writing(state)
        self.swarm.save_state(other, state)

    def test_load_before_first_acquisition_requires_reload(self):
        stale = self.load()
        self.assertFalse(self.directory.exists())
        self.acquire()
        self.refuse_without_writing(stale)
        self.save(self.load())

    def test_unleased_load_is_fenced_before_its_first_save(self):
        self.acquire()
        self.swarm.release_lease(self.directory)
        self.swarm._LOCK_EPOCHS.clear()  # A fresh unleased helper process.
        stale = self.load()
        self.assertFalse(self.swarm._LOCK_EPOCHS)
        self.acquire()
        self.swarm.release_lease(self.directory)
        self.swarm._LOCK_EPOCHS.clear()
        self.refuse_without_writing(stale)
        self.assertFalse(self.swarm._LOCK_EPOCHS)
        self.save(self.load())

    def test_tag_is_not_json_and_legacy_fields_survive(self):
        legacy = {"schema_version": 1, "units": {}, "halted": None,
                  "epoch": 99, "_snapshot_epoch": "ordinary user data"}
        self.directory.mkdir()
        self.path.write_text(json.dumps(legacy))
        self.acquire()
        state = self.load()
        self.assertEqual(json.loads(json.dumps(state)), legacy)
        self.save(state)
        del legacy["epoch"]
        self.assertEqual(json.loads(self.path.read_text()), legacy)
        self.assertEqual(json.loads(self.epoch_path.read_text()), {"epoch": 1})

    def test_no_epoch_load_and_save_neither_acquires_nor_creates_epoch(self):
        state = self.load()
        state["legacy"] = True
        self.save(state)
        self.assertTrue(json.loads(self.path.read_text())["legacy"])
        self.assertFalse(self.epoch_path.exists())
        self.assertFalse(self.swarm.renew_lease(self.directory))

    def test_invalid_epoch_does_not_break_read_only_load_but_save_refuses(self):
        self.directory.mkdir()
        self.path.write_text('{"units": {}, "halted": null}')
        self.epoch_path.write_text("broken")
        before = self.disk()
        state = self.load()
        self.assertEqual(state, {"units": {}, "halted": None})
        self.assertEqual(self.disk(), before)
        self.assertFalse(self.swarm._LOCK_EPOCHS)
        self.refuse_without_writing(state)

    def test_epoch_is_sampled_before_state_read_for_unleased_helper(self):
        self.directory.mkdir()
        self.path.write_text('{"units": {}, "halted": null}')
        self.epoch_path.write_text('{"epoch": 1}')
        read_json = self.swarm.U.read_json

        def successor_after_read(path):
            result = read_json(path)
            self.epoch_path.write_text('{"epoch": 2}')
            return result

        with mock.patch.object(self.swarm.U, "read_json",
                               side_effect=successor_after_read):
            stale = self.load()
        self.refuse_without_writing(stale)


if __name__ == "__main__":
    unittest.main()
