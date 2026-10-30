"""D5 lease API through real registration and the record store."""
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_authority as authority
import d5_authority_store as store
import d5_host as host
import d5_lease as lease
import d5_registry as registry
from tests.fixtures.d5.ready import ready, classification_only, missing_installation
from tests.test_d5_locks import isolate_pool


class TestD5Lease(unittest.TestCase):
    def setUp(self):
        isolate_pool(self)
        anchor = mock.patch.object(host, "session_anchor", return_value=(
            "native", host.ProcessIdentity(42, 9000001)))
        anchor.start()
        self.addCleanup(anchor.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.repo = self.directory / "repo"
        subprocess.run(["git", "init", str(self.repo)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.state = self.directory / "state"
        self.state.mkdir()
        self.base = self.directory / "external"
        patch = mock.patch.object(registry.CP, "_external_base", return_value=self.base)
        patch.start()
        self.addCleanup(patch.stop)
        self.args = dict(issued_at=100, confirmed_at=101, expires_at=200,
                         mandate_bytes=b"mandate\r\n", scope={
                             "plans": ["p"], "repositories": ["repo"],
                             "actions": ["review"], "trackers": []},
                         owner_response="Oui, confirmé.\n",
                         displayed_bounds="  Durée: 100 s; café\n")

        # Keep readiness valid while the operational registry is absent.
        if self._testMethodName == "test_management_requires_registration":
            with mock.patch.object(registry.CP, "_external_base",
                                   return_value=self.directory / "installed-registry"):
                ready(self, self.repo, "p", self.state)

    def register(self, promote=True):
        with nullcontext():
            return ready(self, self.repo, "p", self.state, promote=promote)

    def grant(self, **changes):
        args = dict(self.args, **changes)
        return lease.grant(self.repo, "p", self.state, **args)

    def status(self, **changes):
        args = dict(now=150, mandate_bytes=self.args["mandate_bytes"])
        args.update(changes)
        return lease.status(self.repo, "p", self.state, **args)

    def tree(self):
        return {str(p.relative_to(self.directory)):
                (p.lstat().st_ino, p.lstat().st_mode, p.lstat().st_mtime_ns,
                 p.read_bytes() if p.is_file() else None)
                for p in self.directory.rglob("*")}

    def test_inactive_refuses_before_filesystem_access(self):
        self.base.mkdir()
        before = self.tree()
        with classification_only(self.state), mock.patch.object(registry, "lookup", side_effect=AssertionError("lookup")), \
                mock.patch.object(store, "present", side_effect=AssertionError("stat")):
            self.assertFalse(registry.d5_active(self.state))
            for operation in (self.grant,
                              lambda: lease.revoke(self.repo, "p", self.state, 150, "stop")):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(registry.AuthorityError, "D5 is not active"):
                        operation()
        self.assertEqual(self.tree(), before)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_management_requires_registration(self):
        before = self.tree()
        with nullcontext(), \
                mock.patch.object(store, "create") as create, \
                mock.patch.object(store, "replace") as replace, \
                mock.patch.object(store, "revoke") as revoke:
            for operation in (self.grant,
                              lambda: lease.revoke(self.repo, "p", self.state, 150, "stop")):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(registry.AuthorityError, "not registered"):
                        operation()
            create.assert_not_called()
            replace.assert_not_called()
            revoke.assert_not_called()
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.base.exists())

    def test_grant_stores_exact_record_and_confirmation(self):
        entry = self.register()
        with nullcontext():
            result = self.grant()
        record = authority.decode((Path(entry["record_dir"]) / store.RECORD_NAME).read_bytes())
        self.assertEqual(record, result)
        expected = {"schema_version": 1, "grant_id": result["grant_id"],
                    "issued_at": 100, "confirmed_at": 101, "expires_at": 200,
                    "session_cap": 1, "mandate_sha256": hashlib.sha256(b"mandate\r\n").hexdigest(),
                    "scope": self.args["scope"], "custody": None, "invalidation": None,
                    "confirmation": {key: self.args[key] for key in
                                     ("owner_response", "displayed_bounds")}}
        self.assertEqual(record, expected)
        self.assertRegex(record["grant_id"], r"\A[0-9a-f]{32}\Z")

    def test_expiry_beyond_24_hours_validates_before_any_write(self):
        self.register()
        before = self.tree()
        with nullcontext(), \
                mock.patch.object(registry, "lookup", side_effect=AssertionError("lookup")), \
                mock.patch.object(authority, "validate", wraps=authority.validate) as validate:
            with self.assertRaisesRegex(registry.AuthorityError, "expires_at"):
                self.grant(expires_at=100 + 86401)
            validate.assert_called_once()
        self.assertEqual(self.tree(), before)

    def test_second_grant_replaces_and_archives_first(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        with nullcontext():
            first = self.grant()
            raw = (directory / store.RECORD_NAME).read_bytes()
            second = self.grant(owner_response="Nouvelle confirmation\n", confirmed_at=102, expires_at=300)
        self.assertNotEqual(first["grant_id"], second["grant_id"])
        self.assertEqual((directory / "superseded" / (first["grant_id"] + ".json")).read_bytes(), raw)
        self.assertEqual(authority.decode((directory / store.RECORD_NAME).read_bytes()), second)

    def test_replacement_keeps_store_consent_rules(self):
        self.register()
        with nullcontext():
            self.grant()
            before = self.tree()
            with self.assertRaisesRegex(registry.AuthorityError, "fresh confirmation"):
                self.grant(expires_at=201)
        self.assertEqual(self.tree(), before)

    def test_concurrent_initial_creates_refuse_one_caller(self):
        entry = self.register()
        path = Path(entry["record_dir"]) / store.RECORD_NAME
        present = store.present
        barrier = threading.Barrier(2)

        def synchronized_present(candidate, field):
            result = present(candidate, field)
            # Only the lease's pre-lock observation; store.create uses its
            # own locked check. Both callers must observe absence here.
            if candidate == path:
                barrier.wait(timeout=10)
            return result

        def attempt():
            try:
                return self.grant()
            except registry.AuthorityError as exc:
                return exc

        with nullcontext(), \
                mock.patch.object(store, "present", side_effect=synchronized_present), \
                ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        records = [r for r in results if isinstance(r, dict)]
        failures = [r for r in results if isinstance(r, registry.AuthorityError)]
        self.assertEqual(len(records), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("already exists", str(failures[0]))
        self.assertEqual(authority.decode(path.read_bytes()), records[0])
        self.assertFalse((path.parent / "superseded").exists())

    def test_revoke_persists_store_transition(self):
        entry = self.register()
        with nullcontext():
            first = self.grant()
            result = lease.revoke(self.repo, "p", self.state, 150, "owner stopped\n")
        self.assertEqual(result, authority.revoked(first, 150, "owner stopped\n"))
        self.assertEqual(authority.decode((Path(entry["record_dir"]) / store.RECORD_NAME).read_bytes()), result)
        self.assertEqual(self.status()["record_state"], "invalidated")

    def test_overlapping_grant_after_publication_refuses(self):
        entry = self.register()
        path = Path(entry["record_dir"]) / store.RECORD_NAME
        published, release = threading.Event(), threading.Event()
        write = store._write

        def paused_write(candidate, raw):
            write(candidate, raw)
            if candidate == path and not published.is_set():
                published.set()
                if not release.wait(20):
                    raise AssertionError("publication release deadline")

        with nullcontext(), \
                mock.patch.object(store, "_write", side_effect=paused_write), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.grant)
            try:
                self.assertTrue(published.wait(10), "publication deadline")
                second = pool.submit(self.grant)
                with self.assertRaisesRegex(registry.AuthorityError, "grant.*already exists"):
                    second.result(timeout=10)
            finally:
                release.set()
            record = first.result(timeout=10)
            self.assertEqual(authority.decode(path.read_bytes()), record)
            self.assertFalse((path.parent / "superseded").exists())
            # The permanent lock must be released: sequential replacement
            # remains allowed by the store's consent rules.
            replacement = self.grant(confirmed_at=102)
            self.assertNotEqual(replacement["grant_id"], record["grant_id"])

    def test_revoke_without_record_returns_store_domain_error(self):
        entry = self.register()
        with nullcontext():
            with self.assertRaisesRegex(registry.AuthorityError, "filesystem operation failed") as caught:
                lease.revoke(self.repo, "p", self.state, 150, "cleanup")
        self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
        self.assertFalse((Path(entry["record_dir"]) / store.RECORD_NAME).exists())

    def test_status_absent_creates_nothing(self):
        for base_exists in (False, True):
            with self.subTest(base_exists=base_exists):
                if base_exists:
                    self.base.mkdir()
                before = self.tree()
                self.assertEqual(self.status(), {"state": "inactive", "registration": None,
                                                "record": None, "record_state": "absent"})
                self.assertEqual(self.tree(), before)
                self.assertEqual(self.base.exists(), base_exists)

    def test_status_registered_without_record_creates_nothing(self):
        entry = self.register(promote=False)
        (self.base / "hanig-authority/v1/registry.lock").unlink()
        before = self.tree()
        self.assertEqual(self.status(), {"state": "inactive", "registration": entry,
                                        "record": None, "record_state": "absent"})
        self.assertEqual(self.tree(), before)

    def test_status_reads_without_lock_or_persistent_invalidation(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        with nullcontext():
            record = self.grant()
        # Remove fixture locks to detect O_CREAT even for store.read.
        (directory / "authority.lock").unlink()
        (self.base / "hanig-authority/v1/registry.lock").unlink()
        before = self.tree()
        raw = (directory / store.RECORD_NAME).read_bytes()
        for active in (False, True):
            for changes, expected in (({}, "valid"), ({"now": 200}, "expired"),
                                      ({"mandate_bytes": b"changed"}, "mandate_changed")):
                with self.subTest(active=active, expected=expected), \
                        (nullcontext() if active else missing_installation(entry)):
                    before = self.tree()
                    self.assertEqual(self.status(**changes), {
                        "state": "active" if active else "inactive", "registration": entry,
                        "record": record, "record_state": expected})
                    self.assertEqual((directory / store.RECORD_NAME).read_bytes(), raw)
                    self.assertFalse((directory / "authority.lock").exists())
                    self.assertEqual(self.tree(), before)

    def test_status_malformed_record_refuses_without_writing(self):
        entry = self.register()
        (Path(entry["record_dir"]) / store.RECORD_NAME).write_bytes(b"{bad json")
        before = self.tree()
        with self.assertRaisesRegex(registry.AuthorityError, "invalid JSON"):
            self.status()
        self.assertEqual(self.tree(), before)


if __name__ == "__main__":
    unittest.main()
