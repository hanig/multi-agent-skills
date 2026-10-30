"""D5 registration against real local repositories and directory objects."""
import copy
from contextlib import ExitStack, contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_registry as R
import d5_lease as lease
import d5_install as install
from tests.fixtures.d5.ready import ready
import d5_authority_store as store


class TestD5VersionGate(unittest.TestCase):
    """Fake version probes run even on hosts outside the real-Git envelope."""

    def test_old_or_unparseable_version_before_any_storage_access(self):
        for version in (b"git version 2.35.9\n", b"git version 1.99\n",
                        b"unexpected\n", b"git version 2.x\n", b""):
            with self.subTest(version=version), \
                 mock.patch.object(R, "_git", return_value=version), \
                 mock.patch.object(R, "d5_active", return_value=True), \
                 mock.patch.object(R.CP, "resolve_paths", side_effect=AssertionError("state")), \
                 mock.patch.object(R, "_directory", side_effect=AssertionError("R/P")), \
                 mock.patch.object(R, "_entries", side_effect=AssertionError("registry")), \
                 mock.patch.object(store, "present", side_effect=AssertionError("lstat")):
                for call in (install.stage, R.lookup):
                    with self.subTest(operation=call.__name__), \
                         self.assertRaisesRegex(R.AuthorityError, "unsupported Git version"):
                        call("/unused", "p", "/unused-state")

    def test_activation_and_plan_validation_precede_envelope(self):
        with mock.patch.object(R, "_git", side_effect=AssertionError("Git")):
            with self.assertRaisesRegex(R.AuthorityError, "D5 is not active"):
                R.register("/unused", "p", "/unused-state")
            with mock.patch.object(R, "d5_active", return_value=True):
                for call in (install.stage, R.lookup):
                    with self.assertRaisesRegex(R.AuthorityError, "invalid plan_id"):
                        call("/unused", "../p", "/unused-state")

    def test_supported_leading_versions_reach_layout(self):
        for version in (b"git version 2.36.0\n", b"git version 2.50.1 (Apple Git-155)\n",
                        b"git version 3.0\n"):
            with self.subTest(version=version), \
                 mock.patch.object(R, "_git", return_value=version), \
                 mock.patch.object(R, "_git_path", side_effect=RuntimeError("layout reached")):
                with self.assertRaisesRegex(RuntimeError, "layout reached"):
                    R.lookup("/unused", "p", "/unused-state")


class TestD5Registry(unittest.TestCase):
    def setUp(self):
        version = self.git("--version").decode("ascii", "replace").strip()
        parsed = re.match(r"git version ([0-9]+)\.([0-9]+)", version)
        if not parsed or tuple(map(int, parsed.groups())) < (2, 36):
            self.skipTest("requires Git >= 2.36; host reports " + version)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.d = Path(temporary.name).resolve()
        self.repo = self.d / "repo"
        self.git("init", str(self.repo))
        self.disable_maintenance(self.repo)
        self.git("-C", str(self.repo), "-c", "user.name=Test", "-c",
                 "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "initial")
        self.state = self.d / "state"
        self.state.mkdir()
        self.base = self.d / "external"
        self.root = self.base / "hanig-authority/v1"
        self.patch = mock.patch.object(R.CP, "_external_base", return_value=self.base)
        self.external_base = self.patch.start()
        self.addCleanup(self.patch.stop)

    def git(self, *args):
        return subprocess.run(["git"] + list(args), check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30).stdout

    def disable_maintenance(self, repo):
        self.git("-C", str(repo), "config", "gc.auto", "0")
        self.git("-C", str(repo), "config", "maintenance.auto", "false")

    def register(self, repo=None, plan="p", state=None):
        with mock.patch.object(R, "d5_active", return_value=True):
            if self._testMethodName in (
                    "test_idempotent_retry_syncs_file_and_all_directory_links",
                    "test_lookup_and_lease_status_create_nothing_and_inherit_refusal"):
                if not hasattr(self, "ready_entry"):
                    self.ready_entry = ready(self, self.repo, "p", self.state)
                return R.register(repo or self.repo, plan, state or self.state)
            return install.stage(repo or self.repo, plan, state or self.state)

    def tree(self, root=None):
        result = {}
        for path in sorted((root or self.d).rglob("*")):
            st = path.lstat()
            result[str(path)] = (st.st_ino, st.st_mode, st.st_mtime_ns,
                                 path.read_bytes() if path.is_file() else None)
        return result

    @contextmanager
    def record_fsyncs(self, failed=None):
        """Record real fd identities; fault injection also works as root."""
        calls, real_sync = [], os.fsync
        def sync(fd):
            info = os.fstat(fd)
            key = (info.st_dev, info.st_ino)
            calls.append(key)
            if failed is not None and failed.exists():
                target = failed.stat()
                if key == (target.st_dev, target.st_ino):
                    raise OSError(errno.EIO, "synthetic fsync failure")
            real_sync(fd)
        with mock.patch.object(os, "fsync", side_effect=sync):
            yield calls

    def assert_syncs(self, calls, paths):
        self.assertCountEqual(calls, [(p.stat().st_dev, p.stat().st_ino) for p in paths])

    def chain(self):
        return [self.base, self.root.parent, self.root, self.root / "plans",
                self.root / "registry.json"]

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
        self.disable_maintenance(clone)
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
        self.state.rename(self.d / "retired-state")
        self.state.mkdir()
        self.assertNotEqual(first["state_key"], [self.state.stat().st_dev, self.state.stat().st_ino])
        before = self.tree(self.root)
        with mock.patch.object(R, "d5_active", return_value=True):
            for op in (lambda: R.lookup(self.repo, "p", self.state),
                       self.register):
                with self.subTest(op=op), self.assertRaisesRegex(R.AuthorityError, "state_key mismatch"):
                    op()
        self.assertEqual(self.tree(self.root), before)

    def test_state_must_be_accepted_by_coordinator_paths(self):
        inside = self.repo / "state"
        inside.mkdir()
        before = self.tree()
        with self.assertRaisesRegex(R.AuthorityError, "state-dir.*inside"):
            self.register(state=inside)
        self.assertEqual(self.tree(), before)

    def test_state_must_already_exist_and_be_directory(self):
        for path in (self.d / "missing", self.d / "file"):
            if path.name == "file":
                path.write_text("not a directory")
            with self.assertRaises(R.AuthorityError):
                self.register(state=path)
        with mock.patch.object(R, "d5_active", return_value=True):
            with self.assertRaisesRegex(R.AuthorityError, "state_dir is required"):
                install.stage(self.repo, "p", None)
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

    def test_owned_symlinks_refuse_before_open_or_sync(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        for path in (self.root, self.root / "plans", directory):
            target = self.d / "moved"
            path.rename(target)
            path.symlink_to(target, target_is_directory=True)
            try:
                present = store.present
                def checked(candidate, field):
                    if Path(candidate) == directory:
                        reads.assert_called_once_with(self.root / "registry.json")
                    return present(candidate, field)
                def opened(candidate, flags, **kwargs):
                    self.assertEqual(path, directory)  # R/P still refuse all opens.
                    self.assertFalse(flags & os.O_CREAT)
                    if candidate == entry["registration_id"]:
                        reads.assert_called_once_with(self.root / "registry.json")
                        self.assertEqual(flags, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        self.assertIn("dir_fd", kwargs)
                    else:
                        self.assertNotEqual(Path(candidate), directory)
                        self.assertNotIn(directory, Path(candidate).parents)
                    return os.open(str(candidate), flags, **kwargs)
                with mock.patch.object(R, "os", SimpleNamespace(**vars(os))) as ops, \
                     mock.patch.object(store, "present", side_effect=checked), \
                     mock.patch.object(store, "read_bytes", wraps=store.read_bytes) as reads, \
                     mock.patch.object(os, "fsync", side_effect=AssertionError("fsync")):
                    ops.open = opened
                    for call in (self.register, lambda: R.lookup(self.repo, "p", self.state)):
                        reads.reset_mock()
                        message = str(path) if path != directory else "filesystem/git operation failed"
                        with self.assertRaisesRegex(R.AuthorityError, message):
                            call()
            finally:
                path.unlink()
                target.rename(path)

    def test_idempotent_retry_syncs_file_and_all_directory_links(self):
        entry = self.register()
        before = self.tree(self.root)
        with self.record_fsyncs() as calls:
            self.assertEqual(self.register(), entry)
        self.assert_syncs(calls, self.chain())
        for failed in self.chain():
            with self.subTest(path=failed), self.record_fsyncs(failed):
                with self.assertRaisesRegex(R.AuthorityError, "durability is unconfirmed"):
                    self.register()
            with self.record_fsyncs() as calls:
                self.assertEqual(self.register(), entry)
            self.assert_syncs(calls, self.chain())
        self.assertEqual(self.tree(self.root), before)

    def test_initial_fsyncs_exact_chain_and_created_ancestor_parents(self):
        for case in ("existing", "missing", "nested", "intermediate"):
            with self.subTest(case=case):
                self.base = self.d / case / "base" if case == "nested" else self.d / case
                self.root = self.base / "hanig-authority/v1"
                self.external_base.return_value = self.base
                if case == "existing":
                    self.base.mkdir()
                elif case == "intermediate":
                    self.root.parent.mkdir(parents=True)
                parents = [] if case in ("existing", "intermediate") else [self.base.parent]
                if case == "nested":
                    parents.append(self.base.parent.parent)
                with self.record_fsyncs() as calls:
                    self.assertEqual(self.register()["plan_id"], "p")
                self.assert_syncs(calls, self.chain() + parents)

    def test_retry_after_each_initial_directory_fsync_failure(self):
        for name in ("ancestor", "base", "intermediate", "root", "plans"):
            with self.subTest(failed=name):
                self.base = self.d / name
                self.root = self.base / "hanig-authority/v1"
                self.external_base.return_value = self.base
                failed = {"ancestor": self.base.parent, "base": self.base,
                          "intermediate": self.root.parent, "root": self.root,
                          "plans": self.root / "plans"}[name]
                with self.record_fsyncs(failed) as calls:
                    with self.assertRaisesRegex(R.AuthorityError, "durability is unconfirmed"):
                        self.register()
                self.assertIn((failed.stat().st_dev, failed.stat().st_ino), calls)
                with self.record_fsyncs() as calls:
                    entry = self.register()
                # OD4 explicitly does not reconfirm an earlier call's B link.
                self.assert_syncs(calls, self.chain())
                self.assertEqual(self.register(), entry)

    def test_symlinked_external_base_and_parent_are_allowed(self):
        for name in ("base", "parent", "intermediate"):
            with self.subTest(name=name):
                self.base = self.d / name / "external"
                self.root = self.base / "hanig-authority/v1"
                self.external_base.return_value = self.base
                path = {"base": self.base, "parent": self.base.parent,
                        "intermediate": self.root.parent}[name]
                path.parent.mkdir(parents=True, exist_ok=True)
                target = self.d / (name + "-target")
                target.mkdir()
                path.symlink_to(target, target_is_directory=True)
                with self.record_fsyncs() as calls:
                    entry = self.register()
                extra = [self.base.parent] if name == "parent" else []
                self.assert_syncs(calls, self.chain() + extra)
                self.assertEqual(self.register(), entry)

    def test_registry_strict_schema_and_duplicate_keys(self):
        self.register()
        path = self.root / "registry.json"
        original = path.read_bytes()
        valid = json.loads(original)
        invalid = [b'{"schema_version":2,"schema_version":2,"entries":[]}', b"NaN", b"\xff"]
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
        target.write_text('{"schema_version":2,"entries":[]}')
        for kind in ("link", "fifo", "directory"):
            if kind == "link":
                path.symlink_to(target)
            elif kind == "fifo":
                os.mkfifo(path)
            else:
                path.mkdir()
            with self.subTest(kind=kind), self.assertRaisesRegex(R.AuthorityError, "registry:"):
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
                entries = list(pool.map(lambda _: install.stage(self.repo, "p", self.state), range(4)))
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

    def test_traverse_only_preexisting_ancestor(self):
        self.base.mkdir()
        original = self.d.stat().st_mode
        self.d.chmod(0o111)
        try:
            self.register()
        finally:
            self.d.chmod(original)


    # Preserved counterexamples; repaired under OD3.
    def test_final_missing_base_cannot_return_unconfirmed_registration(self):
        self.assertFalse(self.base.exists())
        parent = self.base.parent.stat()
        parent_key = (parent.st_dev, parent.st_ino)
        synced, real_sync = set(), os.fsync
        def sync(fd):
            info = os.fstat(fd)
            synced.add((info.st_dev, info.st_ino))
            real_sync(fd)
        with mock.patch.object(os, "fsync", side_effect=sync):
            try:
                self.register()
            except R.AuthorityError:
                return  # Refusal is safe; success needs a durable base link.
        self.assertIn(parent_key, synced,
                      "success leaves the newly created external-base link unconfirmed")

    def test_final_symlinked_plans_cannot_sync_outside_base(self):
        self.root.mkdir(parents=True)
        records = self.d / "records"
        records.mkdir()
        (self.root / "plans").symlink_to(records, target_is_directory=True)
        self.assertNotIn(self.base, records.parents)
        target = records.stat()
        target_key = (target.st_dev, target.st_ino)
        synced, real_sync = set(), os.fsync
        def sync(fd):
            info = os.fstat(fd)
            synced.add((info.st_dev, info.st_ino))
            real_sync(fd)
        with mock.patch.object(os, "fsync", side_effect=sync):
            try:
                self.register()
            except R.AuthorityError:
                pass  # Even a refusal must retain the fsync boundary.
        self.assertNotIn(target_key, synced,
                         "plans symlink redirected fsync outside the external base")

    def test_final_preexisting_intermediate_bounds_fsync(self):
        self.root.parent.mkdir(parents=True)
        for phase in ("initial", "idempotent"):
            with self.subTest(phase=phase), self.record_fsyncs() as calls:
                self.register()
            self.assert_syncs(calls, self.chain())

    def test_final_unneeded_ancestor_sync_must_not_abort(self):
        self.root.parent.mkdir(parents=True)
        with self.record_fsyncs(self.base.parent) as calls:
            self.assertEqual(self.register()["plan_id"], "p")
        self.assert_syncs(calls, self.chain())


    # Final OD4 counterexamples, preserved without a post-review repair.
    def test_final_unrelated_symlink_with_existing_registration(self):
        entry = self.register()
        notes = self.d / "notes"
        notes.mkdir()
        (self.root / "plans" / "notes").symlink_to(notes, target_is_directory=True)
        before = self.tree()
        for call in (lambda: R.lookup(self.repo, "p", self.state), self.register):
            with self.subTest(operation=call):
                self.assertEqual(call(), entry)
        self.assertEqual(self.tree(), before)

    def test_final_unrelated_symlink_with_absent_registry(self):
        (self.root / "plans").mkdir(parents=True)
        notes = self.d / "notes.txt"
        notes.write_text("coordinator notes")
        (self.root / "plans" / "notes").symlink_to(notes)
        before = self.tree()
        with self.subTest(operation="lookup"):
            self.assertIsNone(R.lookup(self.repo, "p", self.state))
            self.assertEqual(self.tree(), before)
        with self.subTest(operation="register"):
            self.assertEqual(self.register()["plan_id"], "p")


    def test_separate_git_directory_layout_and_named_git_non_claim(self):
        # OD1: Git cannot identify the original checkout through the linked WT.
        for name in ("common", ".git"):
            home = self.d / ("separate-" + name)
            home.mkdir()
            checkout, common, linked = home / "main", home / name, home / "linked"
            self.git("init", "--separate-git-dir=" + str(common), str(checkout))
            self.disable_maintenance(checkout)
            self.git("-C", str(checkout), "-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "initial")
            self.git("-C", str(checkout), "worktree", "add", "--detach", str(linked))
            before = self.tree()
            for op in (lambda: self.register(repo=checkout),
                       lambda: R.lookup(checkout, "p", self.state)):
                with self.subTest(common=name, original_checkout=True), \
                     self.assertRaisesRegex(R.AuthorityError, "unsupported repository layout"):
                    op()
            self.assertEqual(self.tree(), before)
            if name == ".git":
                entry = self.register(repo=linked)
                self.assertEqual(R.lookup(linked, "p", self.state), entry)
                continue  # Separate .git directory: explicit OD1 non-claim.
            for op in (lambda: self.register(repo=linked),
                       lambda: R.lookup(linked, "p", self.state)):
                with self.subTest(common=name, operation=op), \
                     self.assertRaisesRegex(R.AuthorityError, "unsupported repository layout"):
                    op()
            self.assertEqual(self.tree(), before)

    def test_bare_repository_is_unsupported(self):
        for name in ("common", ".git"):
            bare = self.d / ("bare-" + name) / name
            self.git("clone", "--bare", str(self.repo), str(bare))
            self.disable_maintenance(bare)
            linked = bare.parent / "linked"
            self.git("-C", str(bare), "worktree", "add", "--detach", str(linked))
            before = self.tree()
            for checkout in (bare, linked):
                for op in (lambda: self.register(repo=checkout),
                           lambda: R.lookup(checkout, "p", self.state)):
                    with self.subTest(name=name, checkout=checkout, operation=op), \
                         self.assertRaisesRegex(R.AuthorityError, "unsupported repository layout"):
                        op()
            self.assertEqual(self.tree(), before)

    def test_unlisted_git_file_checkout_refuses_before_storage_access(self):
        alias = self.d / "unlisted"
        alias.mkdir()
        (alias / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n")
        with mock.patch.object(R, "_directory", side_effect=AssertionError("R/P")), \
             mock.patch.object(R, "_entries", side_effect=AssertionError("registry")), \
             mock.patch.object(R.CP, "resolve_paths", side_effect=AssertionError("state")):
            for op in (lambda: self.register(repo=alias),
                       lambda: R.lookup(alias, "p", self.state)):
                with self.assertRaisesRegex(R.AuthorityError, "unsupported repository layout"):
                    op()

    def test_listed_checkout_matches_inode_and_allows_missing_other_worktree(self):
        missing, linked = self.d / "a-missing", self.d / "linked"
        for path in (missing, linked):
            self.git("-C", str(self.repo), "worktree", "add", "--detach", str(path))
        missing.rename(self.d / "moved")  # Prunable list entry before our checkout.
        listed = R._worktrees(self.repo)
        self.assertLess(listed.index(missing), listed.index(linked))
        alias = self.d / "alias"
        alias.symlink_to(self.d, target_is_directory=True)
        real_git = R._git
        def git(checkout, *args):
            raw = real_git(checkout, *args)
            if args == ("worktree", "list", "--porcelain", "-z"):
                raw = raw.replace(os.fsencode(linked), os.fsencode(alias / "linked"))
            return raw
        with mock.patch.object(R, "_git", side_effect=git):
            entry = self.register(repo=linked)
            self.assertEqual(R.lookup(linked, "p", self.state), entry)

    def test_stale_dangling_worktree_is_ignored(self):
        stale, live = self.d / "a-stale", self.d / "z-live"
        for path in (stale, live):
            self.git("-C", str(self.repo), "worktree", "add", "--detach", str(path))
        stale.rename(self.d / "moved-stale")
        stale.symlink_to(self.d / "never-created", target_is_directory=True)
        for checkout in (live, self.repo):
            with self.subTest(checkout=checkout):
                entry = self.register(repo=checkout)
                self.assertEqual(R.lookup(checkout, "p", self.state), entry)
        # An existing symlink target must still contribute its identity.
        stale.unlink()
        stale.symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(R.AuthorityError, "authority path is inside operated Git storage"):
            self.register(repo=live)

    def test_unknown_worktree_target_is_not_treated_as_absent(self):
        stale, live = self.d / "a-stale", self.d / "z-live"
        for path in (stale, live):
            self.git("-C", str(self.repo), "worktree", "add", "--detach", str(path))
        real_stat = Path.stat
        def denied(path, *args, **kwargs):
            if path == stale:
                raise PermissionError(errno.EACCES, "synthetic unknown worktree")
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(Path, "stat", denied):
            for op in (lambda: self.register(repo=live),
                       lambda: R.lookup(live, "p", self.state)):
                with self.assertRaisesRegex(R.AuthorityError, "unsupported repository layout"):
                    op()

    def test_stale_target_with_non_directory_ancestor_is_ignored(self):
        blocked = self.d / "a-blocked"
        stale, live = blocked / "stale", self.d / "z-live"
        for path in (stale, live):
            self.git("-C", str(self.repo), "worktree", "add", "--detach", str(path))
        blocked.rename(self.d / "moved-blocked")
        blocked.write_text("regular file\n")
        with self.subTest(operation="lookup"):
            self.assertIsNone(R.lookup(live, "p", self.state))
        for checkout in (live, self.repo):
            with self.subTest(operation="register", checkout=checkout):
                entry = self.register(repo=checkout)
                self.assertEqual(R.lookup(checkout, "p", self.state), entry)

    def test_common_directory_uses_tristate_lstat(self):
        parent = self.d / "layout"
        parent.mkdir()
        common = parent / ".git"
        for kind in ("missing", "file", "symlink", "unknown"):
            if kind == "file":
                common.write_text("not a directory")
            elif kind == "symlink":
                common.unlink()
                common.symlink_to(self.repo / ".git", target_is_directory=True)
            present = store.present
            def probe(path, field):
                if path == common and kind == "unknown":
                    raise R.AuthorityError(field + ": lookup failed")
                return present(path, field)
            with self.subTest(kind=kind), \
                 mock.patch.object(R, "_git_path", return_value=common), \
                 mock.patch.object(store, "present", side_effect=probe):
                for op in (self.register, lambda: R.lookup(self.repo, "p", self.state)):
                    with self.assertRaisesRegex(R.AuthorityError, "unsupported repository layout"):
                        op()

    def test_envelope_precedes_all_authority_and_state_access(self):
        self.register()
        real_git = R._git
        for failure in ("version", "layout"):
            events = []
            def git(checkout, *args):
                events.append(args)
                if args == ("--version",) and failure == "version":
                    return b"git version 2.35.0\n"
                if args == ("worktree", "list", "--porcelain", "-z"):
                    return b"worktree /wrong\0\0"
                return real_git(checkout, *args)
            present = store.present
            def check(path, field):
                self.assertEqual(Path(path), self.repo / ".git")
                return present(path, field)
            with mock.patch.object(R, "_git", side_effect=git), \
                 mock.patch.object(store, "present", side_effect=check), \
                 mock.patch.object(R.CP, "resolve_paths", side_effect=AssertionError("state")), \
                 mock.patch.object(store, "read_bytes", side_effect=AssertionError("registry")), \
                 mock.patch.object(R, "os", SimpleNamespace(**vars(os))) as ops:
                ops.open = mock.Mock(side_effect=AssertionError("open"))
                for op in (self.register, lambda: R.lookup(self.repo, "p", self.state)):
                    events.clear()
                    with self.subTest(failure=failure, operation=op), \
                         self.assertRaisesRegex(R.AuthorityError, "unsupported"):
                        op()
                    self.assertEqual(events[0], ("--version",))

    def test_plans_worktree_refuses_before_first_mkdir(self):
        plans = self.root / "plans"
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(plans))
        before = self.tree()
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")):
            with self.assertRaisesRegex(R.AuthorityError, "authority path is inside operated Git storage"):
                self.register()
        self.assertEqual(self.tree(), before)

    def test_root_and_state_containment_in_main_common_and_linked(self):
        linked = self.d / "linked"
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(linked))
        for protected in (self.repo, self.repo / ".git", linked):
            with self.subTest(protected=protected):
                for base in (protected, protected / "missing" / "external"):
                    self.external_base.return_value = base
                    with mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")):
                        with self.assertRaisesRegex(R.AuthorityError, "authority path is inside operated Git storage"):
                            self.register()
                self.external_base.return_value = self.base
                with self.assertRaisesRegex(R.AuthorityError, "authority path is inside operated Git storage"):
                    self.register(state=protected)
        self.assertFalse(self.base.exists())

    def test_protected_worktree_spelled_through_symlinked_ancestor(self):
        alias = self.d / "alias"
        alias.symlink_to(self.d, target_is_directory=True)
        linked = self.root / "plans"
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(linked))
        real_git = R._git
        def git(checkout, *args):
            raw = real_git(checkout, *args)
            if args == ("worktree", "list", "--porcelain", "-z"):
                raw = raw.replace(os.fsencode(linked), os.fsencode(alias / linked.relative_to(self.d)))
            return raw
        with mock.patch.object(R, "_git", side_effect=git):
            with self.assertRaisesRegex(R.AuthorityError, "authority path is inside operated Git storage"):
                self.register()

    def test_newline_worktree_containment(self):
        linked = self.d / "linked\nworktree \n"
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(linked))
        # CP's legacy line parser cannot supply this containment check.
        self.external_base.return_value = linked / "authority-base"
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")):
            with self.assertRaisesRegex(R.AuthorityError, "authority path is inside operated Git storage"):
                self.register()
        self.external_base.return_value = self.base
        first = self.register(repo=linked)
        self.assertEqual(R.lookup(linked, "p", self.state), first)

    def test_record_worktree_refuses_before_first_mkdir_and_on_lookup(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(directory))
        self.assertEqual(entry["record_key"], list(R._key(directory.stat())))
        before = self.tree()
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")):
            self.assert_tamper_refused("authority storage became a Git worktree")
        self.assertEqual(self.tree(), before)

    def test_candidate_record_worktree_refuses_before_first_mkdir(self):
        directory = self.root / "plans" / ("a" * 32)
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(directory))
        with mock.patch.object(R.uuid, "uuid4", return_value=SimpleNamespace(hex=directory.name)), \
             mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")):
            with self.assertRaisesRegex(R.AuthorityError, "authority path is inside operated Git storage"):
                self.register()

    def test_containment_follows_symlinked_infrastructure_ancestor(self):
        self.base.mkdir()
        (self.base / "hanig-authority").symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(R.AuthorityError, "authority path is inside operated Git storage"):
            self.register()
        self.assertFalse((self.repo / "v1").exists())

    def test_unrelated_repository_above_authority_is_allowed(self):
        self.git("init", str(self.d))
        self.disable_maintenance(self.d)
        entry = self.register()
        self.assertEqual(R.lookup(self.repo, "p", self.state), entry)


    def assert_tamper_refused(self, message):
        for call in (lambda: R.lookup(self.repo, "p", self.state), self.register):
            with self.subTest(operation=call), \
                 mock.patch.object(os, "fsync", side_effect=AssertionError("fsync before check")):
                before = len(os.listdir("/dev/fd"))
                with self.assertRaisesRegex(R.AuthorityError, message):
                    call()
                self.assertEqual(len(os.listdir("/dev/fd")), before, "fd leak on refusal")

    def test_git_entry_at_each_owned_level(self):
        entry = self.register()
        for directory in (self.root, self.root / "plans", Path(entry["record_dir"])):
            for kind in ("file", "directory", "dangling symlink"):
                marker = directory / ".git"
                if kind == "file":
                    marker.write_text("gitdir: unused\n")
                elif kind == "directory":
                    marker.mkdir()
                else:
                    marker.symlink_to(self.d / "missing")
                try:
                    with self.subTest(level=directory, kind=kind):
                        self.assert_tamper_refused("authority storage became a Git worktree")
                finally:
                    marker.rmdir() if kind == "directory" else marker.unlink()

    def test_record_recreated_refuses(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)  # Pin the old inode; reuse is a non-claim.
        directory.rmdir()
        directory.mkdir()
        self.assertNotEqual(entry["record_key"], list(R._key(directory.stat())))
        self.assert_tamper_refused("record_key mismatch: record directory replaced")

    def test_record_symlink_to_same_inode_refuses(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        moved = self.d / "moved"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        self.assertEqual(entry["record_key"], list(R._key(directory.stat())))
        self.assertFalse((moved / ".git").exists())
        self.assert_tamper_refused("filesystem/git operation failed")

    def test_walk_itself_refuses_symlinks_at_every_level(self):
        entry = self.register()
        for path in (self.root, self.root / "plans", Path(entry["record_dir"])):
            moved = self.d / "moved"
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=True)
            try:
                before = len(os.listdir("/dev/fd"))
                with self.subTest(level=path), self.assertRaises(OSError):
                    R._record_key(self.root, entry["registration_id"])
                self.assertEqual(len(os.listdir("/dev/fd")), before)
            finally:
                path.unlink()
                moved.rename(path)

    def test_walk_opens_nothing_and_leaks_no_fds_on_each_lstat_failure(self):
        entry = self.register()
        real_lstat = os.lstat
        for failed_call in range(6):  # Directory and .git at every level.
            calls = []
            def probe(path):
                calls.append(path)
                if len(calls) == failed_call + 1:
                    raise PermissionError(errno.EACCES, "synthetic walk failure")
                return real_lstat(path)
            before = len(os.listdir("/dev/fd"))
            with self.subTest(failed_call=failed_call), \
                 mock.patch.object(R, "os", SimpleNamespace(**vars(os))) as ops:
                ops.open = mock.Mock(side_effect=AssertionError("walk opened a directory"))
                ops.lstat = probe
                with self.assertRaises(PermissionError):
                    R._record_key(self.root, entry["registration_id"])
                self.assertEqual(len(calls), failed_call + 1)
                ops.open.assert_not_called()
            self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_walk_rejects_non_directory_at_every_level(self):
        entry = self.register()
        for path in (self.root, self.root / "plans", Path(entry["record_dir"])):
            moved = self.d / "moved"
            path.rename(moved)
            path.write_bytes(b"not a directory")
            try:
                with self.subTest(level=path), self.assertRaises(NotADirectoryError):
                    R._record_key(self.root, entry["registration_id"])
            finally:
                path.unlink()
                moved.rename(path)

    @contextmanager
    def walk_io(self, probe):
        """Intercept filesystem entry points while the real walk executes."""
        with ExitStack() as stack:
            for name in ("open", "scandir", "listdir", "stat"):
                stack.enter_context(mock.patch.object(
                    os, name, side_effect=AssertionError("walk called " + name)))
            stack.enter_context(mock.patch("builtins.open", side_effect=AssertionError("open")))
            stack.enter_context(mock.patch.object(Path, "open", side_effect=AssertionError("Path.open")))
            stack.enter_context(mock.patch.object(os, "lstat", side_effect=probe))
            yield

    def test_adversarial_walk_trace_and_refusals(self):
        entry = self.register()
        levels = (self.root, self.root / "plans", Path(entry["record_dir"]))
        paths = [p for level in levels for p in (level, level / ".git")]
        real_lstat = os.lstat
        # Each case supplies one altered lstat result, or one failed probe.
        cases = [("healthy", None, None, None, 6)]
        for i, level in enumerate(levels):
            cases.append(("symlink", level, SimpleNamespace(st_mode=0o120777),
                          NotADirectoryError, 2 * i + 1))
            for mode in (0o100600, 0o040700, 0o120777):
                cases.append(("git", level / ".git", SimpleNamespace(st_mode=mode),
                              R.AuthorityError, 2 * i + 2))
        for field in ("st_dev", "st_ino"):
            info = real_lstat(levels[-1])
            changed = SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino)
            setattr(changed, field, getattr(changed, field) + 1)
            cases.append((field, levels[-1], changed, R.AuthorityError, 6))
        for i, path in enumerate(paths):
            codes = (errno.EACCES, errno.EIO, errno.ENOTDIR)
            if path in levels:
                codes += (errno.ENOENT,)
            for code in codes:
                cases.append(("error", path, OSError(code, "synthetic lstat failure"),
                              OSError, i + 1))
        for label, altered, result, error, count in cases:
            calls = []
            def probe(path):
                calls.append(path)
                if path == altered:
                    if isinstance(result, OSError):
                        raise result
                    return result
                return real_lstat(path)
            with self.subTest(case=label, path=altered, result=result), self.walk_io(probe):
                if error is None:
                    R._check_record(self.root, entry)
                else:
                    with self.assertRaises(error) as caught:
                        R._check_record(self.root, entry)
                    if label == "git":
                        self.assertEqual(str(caught.exception), "authority storage became a Git worktree")
                    if label in ("st_dev", "st_ino"):
                        self.assertIn("record_key mismatch", str(caught.exception))
            self.assertEqual(calls, paths[:count])

    def test_record_key_schema_is_strict_and_new_registry_is_version_two(self):
        entry = self.register()
        path = self.root / "registry.json"
        original = path.read_bytes()
        valid = json.loads(original)
        self.assertEqual(valid["schema_version"], 2)
        self.assertEqual(entry["record_key"], list(R._key(Path(entry["record_dir"]).stat())))
        for key in (None, [], [1], [1, 2, 3], [True, 1], [-1, 1], [1, "2"], {"dev": 1}):
            value = copy.deepcopy(valid)
            value["entries"][0]["record_key"] = key
            path.write_text(json.dumps(value))
            with self.subTest(key=key):
                self.assert_tamper_refused("invalid record_key")
        value = copy.deepcopy(valid)
        del value["entries"][0]["record_key"]
        path.write_text(json.dumps(value))
        self.assert_tamper_refused("invalid entry fields")
        path.write_bytes(original)
        self.assertEqual(R.lookup(self.repo, "p", self.state), entry)

    def test_walk_after_read_before_use_and_initial_key_from_lstat(self):
        events, probes, keys = [], [], []
        real_open, real_lstat, real_fsync = os.open, os.lstat, os.fsync
        real_entries, real_walk = R._entries, R._record_key
        real_sync, real_mkdir = store.sync_directory, Path.mkdir
        real_write, real_stat = store.write_bytes, Path.stat
        walking, phase = False, None
        def entries(root):
            result = real_entries(root)
            events.append("read")
            if phase == "published" and events == ["read"]:
                return []  # Another registration publishes before the locked read.
            return result
        def walk(root, rid):
            nonlocal walking
            self.assertEqual(events[-1], "mkdir" if phase == "new" else "read")
            walking = True
            try:
                result = real_walk(root, rid)
            finally:
                walking = False
            events.append("walk")
            return result
        def opened(path, flags, *args, **kwargs):
            self.assertFalse(walking, "tamper walk opened a directory")
            self.assertNotEqual(Path(path).parent, self.root / "plans", "D opened")
            return real_open(path, flags, *args, **kwargs)
        def fsync(fd):
            if phase in ("lookup", "idempotent", "published"):
                self.assertEqual(events[-1], "walk", "fsync preceded tamper walk")
            return real_fsync(fd)
        def lstat(path):
            if walking:
                probes.append(path)
            info = real_lstat(path)
            if walking and path.parent == self.root / "plans":
                keys.append(list(R._key(info)))
            return info
        def path_stat(path, *args, **kwargs):
            if path.parent == self.root / "plans":
                self.assertNotEqual(phase, "lookup", "lookup used D outside walk")
                if phase in ("idempotent", "published"):
                    self.assertEqual(events[-1], "walk", "D use preceded tamper walk")
            return real_stat(path, *args, **kwargs)
        def mkdir(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            if path.parent == self.root / "plans":
                events.append("mkdir")
        def sync(path):
            if path in self.chain() or path.parent == self.root / "plans":
                self.assertEqual(events[-1], "walk", "fsync preceded tamper walk")
                self.assertNotEqual(path.parent, self.root / "plans", "D synced")
            elif path == self.base.parent:
                self.assertEqual(phase, "new")
                self.assertNotIn("walk", events)
                events.append("base-sync")
            real_sync(path)
        def write(path, raw):
            self.assertEqual(events[-1], "walk")
            self.assertEqual(json.loads(raw)["entries"][0]["record_key"], keys[-1])
            real_write(path, raw)
        with mock.patch.object(R, "_entries", side_effect=entries), \
             mock.patch.object(R, "_record_key", side_effect=walk), \
             mock.patch.object(R, "os", SimpleNamespace(**vars(os))) as ops, \
             mock.patch.object(Path, "mkdir", mkdir), \
             mock.patch.object(Path, "stat", path_stat), \
             mock.patch.object(store, "write_bytes", side_effect=write), \
             mock.patch.object(store, "sync_directory", side_effect=sync):
            ops.open, ops.lstat, ops.fsync = opened, lstat, fsync
            for phase, call in (("new", self.register),
                                ("lookup", lambda: R.lookup(self.repo, "p", self.state)),
                                ("idempotent", self.register),
                                ("published", self.register)):
                events.clear()
                probes.clear()
                keys.clear()
                before = len(os.listdir("/dev/fd"))
                entry = call()
                expected = {"new": ["read", "base-sync", "read", "mkdir", "walk"],
                            "lookup": ["read", "walk"],
                            "idempotent": ["read", "walk", "read", "walk"],
                            "published": ["read", "read", "walk"]}
                self.assertEqual(events, expected[phase])
                count = 2 if phase == "idempotent" else 1
                self.assertEqual(keys, [entry["record_key"]] * count)
                paths = (self.root, self.root / "plans", Path(entry["record_dir"]))
                self.assertEqual(probes, [p for d in paths for p in (d, d / ".git")] * count)
                self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_lookup_and_lease_status_create_nothing_and_inherit_refusal(self):
        entry = self.register()
        before = self.tree()
        real_open = os.open
        def opened(path, flags, *args, **kwargs):
            self.assertFalse(flags & os.O_CREAT)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=opened):
            self.assertEqual(R.lookup(self.repo, "p", self.state), entry)
            self.assertEqual(lease.status(self.repo, "p", self.state, now=150,
                                          mandate_bytes=b"mandate")["registration"], entry)
        self.assertEqual(self.tree(), before)
        (Path(entry["record_dir"]) / ".git").write_text("gitdir: unused\n")
        before = self.tree()
        calls = [lambda: lease.status(self.repo, "p", self.state, now=150, mandate_bytes=b"mandate"),
                 lambda: lease.revoke(self.repo, "p", self.state, 150, "stop"),
                 lambda: lease.grant(self.repo, "p", self.state, issued_at=100,
                                     confirmed_at=101, expires_at=200, mandate_bytes=b"mandate",
                                     scope={"plans": ["p"], "repositories": ["repo"],
                                            "actions": ["review"], "trackers": []},
                                     owner_response="confirmed", displayed_bounds="bounded")]
        with mock.patch.object(R, "d5_active", return_value=True), \
             mock.patch.object(os, "open", side_effect=opened):
            for call in calls:
                with self.assertRaisesRegex(R.AuthorityError, "authority storage became a Git worktree"):
                    call()
        self.assertEqual(self.tree(), before)


    @unittest.skipIf(os.geteuid() == 0, "requires non-root permission enforcement")
    def test_review_round2_searchable_nonlistable_record_lookup(self):
        # Preserved permission counterexample, repaired by the OD1 no-open walk.
        entry = self.register()
        directory = Path(entry["record_dir"])
        original_mode = directory.stat().st_mode & 0o777
        directory.chmod(0o300)
        try:
            self.assertEqual(list(R._key(directory.stat())), entry["record_key"])
            self.assertFalse((directory / ".git").exists())
            self.assertEqual(R.lookup(self.repo, "p", self.state), entry)
        finally:
            directory.chmod(original_mode)

    @unittest.skipIf(os.geteuid() == 0, "requires non-root permission enforcement")
    def test_search_only_levels_keep_lookup_and_required_durability(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        sentinel = directory / "work"
        sentinel.write_bytes(b"preserved work")
        registry = (self.root / "registry.json").read_bytes()
        real_sync, real_open = store.sync_directory, os.open
        for level in (directory, self.root / "plans", self.root):
            original_mode = level.stat().st_mode & 0o777
            before = self.tree()
            opened = []
            def open_fd(path, *args, **kwargs):
                opened.append(Path(path))
                return real_open(path, *args, **kwargs)
            level.chmod(0o300)
            try:
                with self.subTest(level=level), mock.patch.object(os, "open", side_effect=open_fd):
                    # Real permissions, real fsyncs: no permission or durability mocks.
                    self.assertEqual(R.lookup(self.repo, "p", self.state), entry)
                    self.assertEqual(lease.status(self.repo, "p", self.state, now=150,
                                                  mandate_bytes=b"mandate")["registration"], entry)
                    self.assertNotIn(level, opened, "lookup/status opened restricted level")
                    with mock.patch.object(store, "sync_directory", wraps=real_sync) as sync:
                        if level == directory:
                            self.assertEqual(self.register(), entry)
                            self.assertEqual([c.args[0] for c in sync.call_args_list],
                                             [self.base, self.root.parent, self.root / "plans", self.root])
                        else:
                            with self.assertRaisesRegex(R.AuthorityError, "durability is unconfirmed") as cm:
                                self.register()
                            self.assertIsInstance(cm.exception.__cause__, PermissionError)
                            self.assertEqual(cm.exception.__cause__.errno, errno.EACCES)
                            self.assertEqual(sync.call_args.args[0], level)
                    self.assertNotIn(directory, opened, "registration opened D")
                    self.assertEqual(list(R._key(directory.lstat())), entry["record_key"])
                    self.assertEqual((self.root / "registry.json").read_bytes(), registry)
                    self.assertEqual(sentinel.read_bytes(), b"preserved work")
            finally:
                level.chmod(original_mode)
            self.assertEqual(self.tree(), before)
            self.assertEqual(self.register(), entry)  # Operator recovery retains D.

    def test_review_round1_lookup_walk_after_envelope_stat_is_read_only(self):
        # Exercise a converted D from a later listed checkout without
        # constraining the separate envelope gate's filesystem probes.
        entry = self.register()
        directory = Path(entry["record_dir"])
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(directory))
        later = self.d / "zz-later"
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(later))
        listed = R._worktrees(later)
        self.assertLess(listed.index(directory), listed.index(later))
        events = []
        real_entries, real_walk, real_lstat = R._entries, R._record_key, os.lstat
        def entries(root):
            result = real_entries(root)
            events.append("registry-read")
            return result
        def walk(root, rid):
            events.append("tamper-walk")
            with self.walk_io(real_lstat):
                return real_walk(root, rid)
        before = self.tree()
        fds = len(os.listdir("/dev/fd"))
        with mock.patch.object(R, "_entries", side_effect=entries), \
             mock.patch.object(R, "_record_key", side_effect=walk):
            with self.assertRaisesRegex(R.AuthorityError, "authority storage became a Git worktree"):
                R.lookup(later, "p", self.state)
        self.assertEqual(self.tree(), before)
        self.assertEqual(len(os.listdir("/dev/fd")), fds)
        self.assertLess(events.index("registry-read"), events.index("tamper-walk"))

    def test_final_converted_record_refuses_without_walk_open(self):
        # Unit-owned behavior, independent of the envelope's traversal.
        directory = Path(self.register()["record_dir"])
        self.git("-C", str(self.repo), "worktree", "add", "--detach", str(directory))
        real_walk, real_lstat = R._record_key, os.lstat
        def walk(root, rid):
            with self.walk_io(real_lstat):
                return real_walk(root, rid)
        before = self.tree()
        with mock.patch.object(R, "_record_key", side_effect=walk) as checked:
            with self.assertRaisesRegex(R.AuthorityError, "authority storage became a Git worktree"):
                R.lookup(self.repo, "p", self.state)
        self.assertEqual(self.tree(), before)
        checked.assert_called_once()

    def test_c6_unregistered_lookup_returns_none_without_walk(self):
        # These absent/unmatched entries have no state ownership conflict.
        other = self.d / "unregistered-state"
        other.mkdir()
        for registered in (False, True):
            if registered:
                self.register()
            before = self.tree()
            with self.subTest(registry_exists=registered), \
                 mock.patch.object(R, "_entries", wraps=R._entries) as read, \
                 mock.patch.object(R, "_record_key", wraps=R._record_key) as walk:
                self.assertIsNone(R.lookup(self.repo, "unregistered", other))
                read.assert_called_once()
                walk.assert_not_called()
            self.assertEqual(self.tree(), before)

    def test_c6_state_conflicts_refuse_without_walk(self):
        self.register()
        other = self.d / "other-state"
        other.mkdir()
        for plan, state, message in (
                ("p", other, "registry: state_key mismatch: state directory replaced or conflicting"),
                ("unregistered", self.state, "registry: state_key already registered to another plan")):
            for operation in (R.lookup, install.stage):
                before = self.tree()
                with self.subTest(plan=plan, operation=operation.__name__), \
                     mock.patch.object(R, "d5_active", return_value=True), \
                     mock.patch.object(R, "_entries", wraps=R._entries) as read, \
                     mock.patch.object(R, "_record_key", wraps=R._record_key) as walk:
                    with self.assertRaises(R.AuthorityError) as caught:
                        operation(self.repo, plan, state)
                    self.assertEqual(str(caught.exception), message)
                    read.assert_called_once()
                    walk.assert_not_called()
                self.assertEqual(self.tree(), before)

    def test_schema_one_refuses_without_migration_or_mutation(self):
        entry = self.register()
        (Path(entry["record_dir"]) / "retained").write_bytes(b"existing work")
        path = self.root / "registry.json"
        original = json.loads(path.read_bytes())
        message = ("registry: unsupported registry schema 1, written before tamper checks; "
                   "D5 was never active, so it holds no grants: remove registry.json "
                   "and register again")
        for fields in ("with-key", "without-key", "empty"):
            value = copy.deepcopy(original)
            value["schema_version"] = 1
            if fields == "without-key":
                del value["entries"][0]["record_key"]
            elif fields == "empty":
                value["entries"] = []
            path.write_text(json.dumps(value))
            for call in (lambda: R.lookup(self.repo, "p", self.state), self.register):
                before = self.tree(self.root)
                with self.subTest(fields=fields, operation=call), \
                     mock.patch.object(R, "_record_key", wraps=R._record_key) as walk:
                    try:
                        with self.assertRaises(R.AuthorityError) as caught:
                            call()
                        self.assertEqual(str(caught.exception), message)
                        walk.assert_not_called()
                    finally:
                        self.assertEqual(self.tree(self.root), before)

    def test_other_schema_versions_keep_existing_refusal(self):
        self.register()
        path = self.root / "registry.json"
        value = json.loads(path.read_bytes())
        for version in (None, True, False, 0, -1, 3, 1.0, 2.0, "1", "2", [], {}):
            value["schema_version"] = version
            path.write_text(json.dumps(value))
            for call in (lambda: R.lookup(self.repo, "p", self.state), self.register):
                before = self.tree(self.root)
                with self.subTest(version=version, operation=call):
                    with self.assertRaises(R.AuthorityError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), "registry: invalid schema_version")
                    self.assertEqual(self.tree(self.root), before)


if __name__ == "__main__":
    unittest.main()
