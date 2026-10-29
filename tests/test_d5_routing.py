"""R9 delegation through real wrappers, registry and store; no activation."""
from contextlib import contextmanager, ExitStack
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_authority as authority
import d5_authority_store as store
import d5_control as control
import d5_custody as custody
import d5_host as host
import d5_install as install
import d5_lease as lease
import d5_locks as locks
import d5_registry as registry
from tests import test_d5_custody as fixtures


class TestD5Routing(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestCustodyAPI()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.promote = self._testMethodName not in (
            "test_revoke_delegates_to_control", "test_relinquish_delegates_to_control")
        self.entry = self.fixture.register(record=False)
        self.directory = Path(self.entry["record_dir"])
        self.path = self.directory / store.RECORD_NAME
        self.mandate = self.fixture.repo / "docs/orchestrator-mandate.md"
        self.args = self.fixture.repo, "p", self.fixture.state

    def grant(self, **changes):
        record = fixtures.grant()
        args = {key: record[key] for key in ("issued_at", "confirmed_at", "expires_at", "scope")}
        args.update(record["confirmation"])
        args.update(mandate_bytes=fixtures.MANDATE)
        args.update(changes)
        return lease.grant(*self.args, **args)

    def read(self):
        return authority.decode(self.path.read_bytes())

    def claim(self, **changes):
        args = dict(now=101, mandate_bytes=fixtures.MANDATE)
        args.update(changes)
        return custody.claim(*self.args, **args)

    @contextmanager
    def store_boundary(self, name, ranks):
        """Inspect held ranks and independent flock probes at the real consumer."""
        original = getattr(store, name)

        def checked(*args, **kwargs):
            self.assertEqual([(rank, shared) for _, rank, shared, _ in locks._held()], ranks)
            for rank, _ in ranks:
                path = self.directory / ("authority.drain.lock" if rank == 0 else "authority.start.lock")
                fd = os.open(path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            return original(*args, **kwargs)

        with mock.patch.object(store, name, side_effect=checked) as observed:
            yield observed
            observed.assert_called_once()

    def test_initial_delegates_to_install_initial(self):
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(install, "install_initial", wraps=install.install_initial) as route, \
                self.store_boundary("create", [(0, False), (1, False)]):
            result = self.grant()
        route.assert_called_once_with(self.directory, result)
        self.assertEqual(self.read(), result)
        self.assertIsNone(result["custody"])

    def test_replacement_delegates_to_replace_authority(self):
        first = fixtures.grant()
        store.create(self.directory, first)
        raw = self.path.read_bytes()
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(install, "replace_authority", wraps=install.replace_authority) as route, \
                self.store_boundary("replace", [(0, False), (1, False)]):
            result = self.grant()
        route.assert_called_once_with(self.directory, result)
        self.assertNotEqual(first["grant_id"], result["grant_id"])
        self.assertEqual(self.read(), result)
        self.assertEqual((self.directory / "superseded" / (first["grant_id"] + ".json")).read_bytes(), raw)

    def test_revoke_delegates_to_control(self):
        # R9-DIRECT-STORE-REVOKE: result alone cannot establish START ownership.
        store.create(self.directory, fixtures.grant())
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(control, "revoke", wraps=control.revoke) as route, \
                self.store_boundary("revoke", [(1, False)]):
            result = lease.revoke(*self.args, 120, "owner stopped")
        route.assert_called_once_with(*self.args, 120, "owner stopped")
        self.assertEqual(self.read(), result)
        self.assertEqual(result["invalidation"], {"at": 120, "kind": "revoked", "reason": "owner stopped"})
        self.assertFalse((self.directory / "authority.drain.lock").exists())

    def test_claim_delegates_to_install_with_fresh_registered_source(self):
        # R9-DIRECT-STORE-CLAIM; caller bytes/time must not substitute for R3's.
        store.create(self.directory, fixtures.grant())
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(registry, "lookup", wraps=registry.lookup) as lookup, \
                mock.patch.object(install, "claim_custody", wraps=install.claim_custody) as route, \
                self.store_boundary("claim", [(0, False), (1, False)]):
            result = self.claim(now=999999, mandate_bytes=b"stale caller bytes")
        lookup.assert_called_once_with(*self.args)
        route.assert_called_once_with(self.entry["record_dir"], mandate_path=self.mandate)
        self.assertEqual(self.read(), result)
        self.assertEqual(result["custody"]["claimed_at"], 101)
        self.assertEqual(result["custody"]["session_id"], "native")
        self.fixture.anchor.assert_called_once_with()
        # A second call re-resolves registration and reads the new source bytes.
        self.mandate.write_bytes(b"current registered bytes")
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(registry, "lookup", wraps=registry.lookup) as lookup:
            with self.assertRaisesRegex(authority.AuthorityError, "mandate_changed"):
                self.claim()
        lookup.assert_called_once_with(*self.args)
        self.assertEqual(self.read()["invalidation"]["kind"], "mandate_changed")

    def test_claim_from_linked_checkout_uses_registered_checkout(self):
        store.create(self.directory, fixtures.grant())
        def git(*args):
            subprocess.run(["git", "-C", str(self.fixture.repo), *args], check=True,
                           capture_output=True, timeout=30)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "--allow-empty", "-m", "fixture")
        linked = self.fixture.directory / "linked"
        git("worktree", "add", "-b", "linked", str(linked), "HEAD")
        (linked / "docs").mkdir()
        (linked / "docs/orchestrator-mandate.md").write_bytes(b"unregistered source")
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(install, "claim_custody", wraps=install.claim_custody) as route:
            result = custody.claim(linked, "p", self.fixture.state, now=999999, mandate_bytes=b"cached")
        route.assert_called_once_with(self.entry["record_dir"], mandate_path=self.mandate)
        self.assertEqual(self.read(), result)
        self.assertEqual(result["custody"]["session_id"], "native")

    def test_symlinked_registered_mandate_refuses_without_custody(self):
        store.create(self.directory, fixtures.grant())
        before = self.path.read_bytes(), self.path.stat().st_ino, self.path.stat().st_mode
        target = self.fixture.directory / "regular.md"
        target.write_bytes(fixtures.MANDATE)
        self.mandate.unlink()
        self.mandate.symlink_to(target)
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(store, "claim", wraps=store.claim) as mutation:
            with self.assertRaises(OSError):
                self.claim()
        mutation.assert_not_called()
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino, self.path.stat().st_mode), before)
        self.assertIsNone(self.read()["custody"])
        self.fixture.live.assert_not_called()

    def test_relinquish_delegates_to_control(self):
        store.create(self.directory, fixtures.grant())
        store.claim(self.directory, 101, fixtures.MANDATE, "native", fixtures.ANCHOR, host.liveness)
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(control, "relinquish", wraps=control.relinquish) as route, \
                self.store_boundary("relinquish", [(1, False)]):
            result = custody.relinquish(*self.args, now=120)
        route.assert_called_once_with(*self.args, now=120)
        self.assertEqual(self.read(), result)
        self.assertEqual(result["custody"]["relinquished_at"], 120)
        self.fixture.anchor.assert_called_once_with()
        self.assertFalse((self.directory / "authority.drain.lock").exists())

    @mock.patch.object(registry, "d5_active", new=lambda: False)
    def test_inactive_delegates_are_not_called(self):
        # R9-BYPASS-INACTIVE: unrelated environment/CLI input cannot activate.
        before = self.fixture.tree()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {
                "D5_ACTIVE": "1", "HANIG_D5_ACTIVE": "1", "SWARM_AUTOPILOT": "1"}))
            stack.enter_context(mock.patch.object(sys, "argv", ["fixture", "--d5-active", "swarm autopilot"]))
            self.assertFalse(registry.d5_active())
            for target, names in ((install, ("install_initial", "replace_authority", "claim_custody")),
                    (control, ("revoke", "relinquish", "record_mandate_invalidation")),
                    (registry, ("lookup",)), (host, ("session_anchor",))):
                for name in names:
                    stack.enter_context(mock.patch.object(target, name, side_effect=AssertionError(name)))
            for operation in (self.grant, self.claim,
                    lambda: lease.revoke(*self.args, 120, "stop"),
                    lambda: custody.relinquish(*self.args, now=120)):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(authority.AuthorityError, "^D5 is not active$"):
                        operation()
        self.assertEqual(self.fixture.tree(), before)

    def test_status_paths_remain_read_only(self):
        # R9-UNSERIALIZED-STATUS-WRITE: no status persistence route exists.
        store.create(self.directory, fixtures.grant())
        before = self.fixture.tree()
        with ExitStack() as stack:
            for target, names in ((control, ("record_mandate_invalidation", "revoke", "relinquish")),
                    (install, ("install_initial", "replace_authority", "claim_custody")),
                    (store, ("read", "check", "revoke", "claim", "relinquish", "create", "replace"))):
                for name in names:
                    stack.enter_context(mock.patch.object(target, name, side_effect=AssertionError(name)))
            for active in (False, True):
                with self.subTest(active=active), mock.patch.object(registry, "d5_active", return_value=active):
                    self.assertEqual(lease.status(*self.args, now=120,
                        mandate_bytes=b"changed")["record_state"], "mandate_changed")
                    self.assertIsNone(custody.status(*self.args, now=120)["custody"])
        self.assertEqual(self.fixture.tree(), before)
        self.assertIsNone(self.read()["invalidation"])


if __name__ == "__main__":
    unittest.main()
