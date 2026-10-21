"""D5 registration against real local repositories and directory objects."""
import copy
import errno
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_registry as R
import d5_authority_store as store


class TestD5Registry(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.d = Path(temporary.name).resolve()
        self.repo = self.d / "repo"
        self.git("init", str(self.repo))
        self.git("-C", str(self.repo), "-c", "user.name=Test", "-c",
                 "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "initial")
        self.state = self.d / "state"
        self.state.mkdir()
        self.base = self.d / "external"
        self.root = self.base / "hanig-authority/v1"
        self.patch = mock.patch.object(R.CP, "_external_base", return_value=self.base)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def git(self, *args):
        return subprocess.run(["git"] + list(args), check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30).stdout

    def register(self, repo=None, plan="p", state=None):
        with mock.patch.object(R, "d5_active", return_value=True):
            return R.register(repo or self.repo, plan, state or self.state)

    def tree(self, root=None):
        result = {}
        for path in sorted((root or self.d).rglob("*")):
            st = path.lstat()
            result[str(path)] = (st.st_ino, st.st_mode, st.st_mtime_ns,
                                 path.read_bytes() if path.is_file() else None)
        return result

    def test_alias_and_spelling_share_registration(self):
        first = self.register()
        alias = self.d / "alias"
        alias.symlink_to(self.state, target_is_directory=True)
        spelling = str(self.state) + "/../state/."
        before = self.tree(self.root)
        for state in (alias, spelling):
            self.assertEqual(self.register(state=state), first)
            self.assertEqual(R.lookup(self.repo, "p", state), first)
        self.assertEqual(self.tree(self.root), before)
        self.assertEqual(first["state_key"], [self.state.stat().st_dev, self.state.stat().st_ino])

    def test_linked_worktrees_share_repository_object(self):
        linked = self.d / "linked"
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(linked))
        before = self.tree(self.repo / ".git")
        first = self.register()
        self.assertEqual(self.register(repo=linked), first)
        self.assertEqual(R.lookup(linked, "p", self.state), first)
        common = self.repo / ".git"
        self.assertEqual(first["repo_key"], [common.stat().st_dev, common.stat().st_ino])
        self.assertEqual(self.tree(common), before)  # No authority or pointer in Git.

    def test_conflicts_and_two_plans_in_one_repository(self):
        first = self.register()
        other = self.d / "other-state"
        other.mkdir()
        before = self.tree(self.root)
        with self.assertRaisesRegex(R.AuthorityError, "state_key mismatch"):
            self.register(state=other)
        with self.assertRaisesRegex(R.AuthorityError, "already registered"):
            self.register(plan="q")
        self.assertEqual(self.tree(self.root), before)
        second = self.register(plan="q", state=other)
        self.assertEqual(first["repo_key"], second["repo_key"])
        self.assertNotEqual(first["registration_id"], second["registration_id"])
        self.assertEqual(R.lookup(self.repo, "q", other), second)

    def test_clone_is_not_repository_identity(self):
        first = self.register()
        clone = self.d / "clone"
        self.git("clone", str(self.repo), str(clone))
        with self.assertRaisesRegex(R.AuthorityError, "already registered"):
            self.register(repo=clone)
        other = self.d / "clone-state"
        other.mkdir()
        second = self.register(repo=clone, state=other)
        self.assertNotEqual(first["repo_key"], second["repo_key"])

    def test_idempotent_overwrites_nothing(self):
        first = self.register()
        marker = Path(first["record_dir"]) / "retained"
        marker.write_bytes(b"existing work")
        before = self.tree(self.root)
        with mock.patch.object(store, "write_bytes", side_effect=AssertionError("write")):
            self.assertEqual(self.register(), first)
        self.assertEqual(self.tree(self.root), before)

    def test_recreated_state_refuses_lookup_and_registration(self):
        first = self.register()
        fd = os.open(str(self.state), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)  # Hold old inode to prevent immediate reuse.
        self.state.rmdir()
        self.state.mkdir()
        self.assertNotEqual(first["state_key"], [self.state.stat().st_dev, self.state.stat().st_ino])
        before = self.tree(self.root)
        with mock.patch.object(R, "d5_active", return_value=True):
            for op in (lambda: R.lookup(self.repo, "p", self.state),
                       lambda: self.register()):
                with self.subTest(op=op), self.assertRaisesRegex(R.AuthorityError, "state_key mismatch"):
                    op()
        self.assertEqual(self.tree(self.root), before)

    def test_root_and_state_must_be_external(self):
        linked = self.d / "linked"
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(linked))
        for worktree in (self.repo, linked):
            with mock.patch.object(R.CP, "_external_base", return_value=worktree):
                before = self.tree()
                with self.assertRaisesRegex(R.AuthorityError, "authority root.*inside"):
                    self.register()
                self.assertEqual(self.tree(), before)
            inside = worktree / "state"
            inside.mkdir()
            with self.assertRaisesRegex(R.AuthorityError, "state-dir.*inside"):
                self.register(state=inside)
        self.assertFalse(self.root.exists())

    def test_symlinked_root_cannot_enter_worktree(self):
        self.base.mkdir()
        (self.base / "hanig-authority").symlink_to(self.repo, target_is_directory=True)
        before = self.tree()
        with self.assertRaisesRegex(R.AuthorityError, "authority root.*inside"):
            self.register()
        self.assertEqual(self.tree(), before)

    def test_state_must_already_exist_and_be_directory(self):
        for path in (self.d / "missing", self.d / "file"):
            if path.name == "file":
                path.write_text("not a directory")
            with self.assertRaises(R.AuthorityError):
                self.register(state=path)
        with mock.patch.object(R, "d5_active", return_value=True):
            with self.assertRaisesRegex(R.AuthorityError, "state_dir is required"):
                R.register(self.repo, "p", None)
        self.assertFalse(self.base.exists())

    def test_missing_lookup_creates_nothing(self):
        before = self.tree()
        self.assertIsNone(R.lookup(self.repo, "p", self.state))
        self.assertEqual(self.tree(), before)

    def test_register_refuses_before_activation(self):
        before = self.tree()
        self.assertFalse(R.d5_active())
        with self.assertRaisesRegex(R.AuthorityError, "D5 is not active"):
            R.register(self.repo, "p", self.state)
        self.assertEqual(self.tree(), before)

    def test_invalid_plan_ids(self):
        for plan in ("..", "/", "", "a" * 65, "é", "p\n", "p/q", None, True):
            with self.subTest(plan=plan), self.assertRaisesRegex(R.AuthorityError, "plan_id"):
                self.register(plan=plan)
        for plan in ("A", "a" * 64, "A._-9"):
            state = self.d / ("state-" + plan)
            state.mkdir()
            self.assertEqual(self.register(plan=plan, state=state)["plan_id"], plan)

    def test_plan_id_is_exact(self):
        first = self.register(plan="P")
        other = self.d / "other"
        other.mkdir()
        second = self.register(plan="p", state=other)
        self.assertNotEqual(first["registration_id"], second["registration_id"])

    def test_whitespace_checkout_cannot_hold_authority(self):
        checkout = self.d / "repo "
        self.repo.rename(checkout)
        state = checkout / "state"
        state.mkdir()
        self.patch.stop()
        before = self.tree()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}):
            with self.assertRaisesRegex(R.AuthorityError, "operated"):
                self.register(repo=checkout, state=state)
        self.assertEqual(self.tree(), before)

    def test_retry_syncs_root_link_before_publication(self):
        sync, write = store.sync_directory, store.write_bytes
        def interrupted(path):
            if path == self.root.parent:
                raise OSError(errno.EIO, "synthetic")
            sync(path)
        with mock.patch.object(store, "sync_directory", side_effect=interrupted):
            with self.assertRaisesRegex(R.AuthorityError, "durability is unconfirmed"):
                self.register()
        with mock.patch.object(store, "sync_directory", wraps=sync) as synced:
            def checked(path, raw):
                self.assertIn(mock.call(self.root.parent), synced.call_args_list)
                write(path, raw)
            with mock.patch.object(store, "write_bytes", side_effect=checked):
                self.register()

    def test_plans_cannot_be_git_common_directory(self):
        self.root.mkdir(parents=True)
        common = self.root / "plans"
        checkout = self.d / "separate"
        self.git("init", "--separate-git-dir=" + str(common), str(checkout))
        before = self.tree()
        with self.assertRaisesRegex(R.AuthorityError, "operated|Git common"):
            self.register(repo=checkout)
        self.assertEqual(self.tree(), before)

    def test_retry_after_registry_sync_failure(self):
        with mock.patch.object(store, "_sync_directory",
                               side_effect=OSError(errno.EIO, "synthetic post-replace")):
            with self.assertRaisesRegex(R.AuthorityError, "durability is unconfirmed"):
                self.register()
        self.assertTrue((self.root / "registry.json").is_file())
        with mock.patch.object(store, "sync_directory", wraps=store.sync_directory) as direct:
            with mock.patch.object(store, "_sync_directory", wraps=store._sync_directory) as writer:
                result = self.register()
        self.assertEqual(result["plan_id"], "p")
        self.assertIn(mock.call(self.root), direct.call_args_list + writer.call_args_list,
                      "successful retry must confirm registry-link durability")

    def test_unrelated_repository_above_storage_is_allowed(self):
        self.git("init", str(self.d))
        entry = self.register()
        self.assertEqual(entry["record_dir"], str(self.root / "plans" / entry["registration_id"]))
        self.assertEqual(R.lookup(self.repo, "p", self.state), entry)

    def test_retry_syncs_registry_with_symlinked_plans(self):
        self.root.mkdir(parents=True)
        records = self.d / "records"
        records.mkdir()
        (self.root / "plans").symlink_to(records, target_is_directory=True)
        self.test_retry_after_registry_sync_failure()

    def test_idempotent_retry_syncs_file_and_all_directory_links(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        paths = [self.root / "registry.json", directory, *directory.parents]
        keys = {(p.stat().st_dev, p.stat().st_ino) for p in paths}
        synced, real_sync = set(), os.fsync
        def sync(fd):
            info = os.fstat(fd)
            synced.add((info.st_dev, info.st_ino))
            real_sync(fd)
        before = self.tree(self.root)
        with mock.patch.object(os, "fsync", side_effect=sync):
            self.assertEqual(self.register(), entry)
        self.assertTrue(keys <= synced, "retry must sync the file and every directory link")
        for failed in keys:
            def fail(fd):
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) == failed:
                    raise OSError(errno.EIO, "synthetic retry sync failure")
                real_sync(fd)
            with self.subTest(key=failed), mock.patch.object(os, "fsync", side_effect=fail):
                with self.assertRaisesRegex(R.AuthorityError, "durability is unconfirmed"):
                    self.register()
        self.assertEqual(self.tree(self.root), before)

    def test_registry_strict_schema_and_duplicate_keys(self):
        self.register()
        path = self.root / "registry.json"
        original = path.read_bytes()
        valid = json.loads(original)
        invalid = [b'{"schema_version":1,"schema_version":1,"entries":[]}', b"NaN", b"\xff"]
        for field, value in (("schema_version", True), ("entries", {}), ("extra", 1)):
            record = copy.deepcopy(valid)
            record[field] = value
            invalid.append(json.dumps(record).encode())
        for field, value in (("state_key", [True, 1]), ("repo_key", [-1, 1]),
                             ("registration_id", "../x"), ("record_dir", str(self.repo)),
                             ("state_dir", "relative"), ("plan_id", ".."), ("extra", 1)):
            record = copy.deepcopy(valid)
            record["entries"][0][field] = value
            invalid.append(json.dumps(record).encode())
        duplicated = copy.deepcopy(valid)
        duplicated["entries"] *= 2
        invalid.append(json.dumps(duplicated).encode())
        for raw in invalid:
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(R.AuthorityError):
                R.lookup(self.repo, "p", self.state)
            self.assertEqual(path.read_bytes(), raw)
        path.write_bytes(original)
        self.assertEqual(R.lookup(self.repo, "p", self.state), valid["entries"][0])

    def test_registry_rejects_nonregular_files(self):
        self.register()
        path = self.root / "registry.json"
        path.unlink()
        target = self.d / "target"
        target.write_text('{"schema_version":1,"entries":[]}')
        for kind in ("link", "fifo", "directory"):
            if kind == "link":
                path.symlink_to(target)
            elif kind == "fifo":
                os.mkfifo(path)
            else:
                path.mkdir()
            with self.subTest(kind=kind), self.assertRaises(R.AuthorityError):
                R.lookup(self.repo, "p", self.state)
            path.rmdir() if kind == "directory" else path.unlink()

    def test_durable_write_holds_registry_lock(self):
        write = store.write_bytes
        def checked(path, raw):
            with (self.root / "registry.lock").open("rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            write(path, raw)
        with mock.patch.object(store, "write_bytes", side_effect=checked) as writes:
            self.register()
        writes.assert_called_once()
        self.assertEqual(writes.call_args.args[0], self.root / "registry.json")

    def test_concurrent_registration_is_one_entry(self):
        # flock on independently opened descriptors serializes even same-process writers.
        with mock.patch.object(R, "d5_active", return_value=True):
            with ThreadPoolExecutor(max_workers=4) as pool:
                entries = list(pool.map(lambda _: R.register(self.repo, "p", self.state), range(4)))
        self.assertTrue(all(entry == entries[0] for entry in entries))
        self.assertEqual(len(json.loads((self.root / "registry.json").read_bytes())["entries"]), 1)

    def test_failed_publication_retains_previous_registry(self):
        first = self.register()
        raw = (self.root / "registry.json").read_bytes()
        other = self.d / "other"
        other.mkdir()
        with mock.patch.object(store.os, "replace", side_effect=OSError(errno.EIO, "synthetic")):
            with self.assertRaisesRegex(R.AuthorityError, "durability is unconfirmed"):
                self.register(plan="q", state=other)
        self.assertEqual((self.root / "registry.json").read_bytes(), raw)
        self.assertEqual(R.lookup(self.repo, "p", self.state), first)
        self.assertEqual(self.register(plan="q", state=other)["plan_id"], "q")


if __name__ == "__main__":
    unittest.main()
