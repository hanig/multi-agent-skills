"""Custody transitions through the locked store and inactive public API."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import ExitStack
import copy
import fcntl
from pathlib import Path
import stat
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
import d5_custody as custody
import d5_host as host
import d5_install as install
import d5_lease as lease
import d5_registry as registry
from tests.test_d5_authority import MANDATE, grant
from tests.fixtures.d5.ready import ready
from tests.test_d5_locks import isolate_pool

ANCHOR = host.ProcessIdentity(42, 9000001)
OTHER = host.ProcessIdentity(43, 9000002)


class TestCustodyStore(unittest.TestCase):
    def setUp(self):
        isolate_pool(self)
        anchor = mock.patch.object(host, "session_anchor", return_value=("native", ANCHOR))
        anchor.start()
        self.addCleanup(anchor.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / store.RECORD_NAME
        store.create(self.directory, grant())
        patch = mock.patch.object(host, "liveness", return_value="alive")
        self.live = patch.start()
        self.addCleanup(patch.stop)

    def claim(self, session="native", anchor=ANCHOR, now=101, mandate=MANDATE):
        return store.claim(self.directory, now, mandate, session, anchor, host.liveness)

    def read(self):
        return authority.decode(self.path.read_bytes())

    def test_compaction_and_each_identity_change(self):
        first = self.claim()
        self.live.assert_not_called()
        self.assertEqual(first["custody"], {
            "session_id": "native", "anchor_pid": 42, "anchor_start_us": 9000001,
            "generation": 1, "claimed_at": 101, "relinquished_at": None})
        with mock.patch.object(store, "_write", side_effect=AssertionError("rewrote")):
            self.assertEqual(self.claim(now=102), first)
        self.live.assert_not_called()
        for session, anchor in (("other", ANCHOR), ("native", OTHER),
                                ("native", host.ProcessIdentity(42, 9000002))):
            with self.subTest(session=session, anchor=anchor):
                with self.assertRaisesRegex(authority.AuthorityError, "dead holder"):
                    self.claim(session, anchor)
                self.live.assert_called_with(ANCHOR)
                self.assertEqual(self.read(), first)

    def test_relinquishment_keeps_holder_and_blocks_live_successor(self):
        first = self.claim()
        updated = store.relinquish(self.directory, 102, "native", ANCHOR)
        expected = copy.deepcopy(first)
        expected["custody"]["relinquished_at"] = 102
        self.assertEqual(updated, expected)
        self.assertEqual(self.read(), expected)
        with mock.patch.object(store, "_write", side_effect=AssertionError("rewrote")):
            self.assertEqual(store.relinquish(self.directory, 103, "native", ANCHOR), expected)
        for session, anchor in (("other", ANCHOR), ("native", OTHER)):
            with self.assertRaisesRegex(authority.AuthorityError, "exact holder"):
                store.relinquish(self.directory, 103, session, anchor)
        with self.assertRaisesRegex(authority.AuthorityError, "dead holder"):
            self.claim("next", OTHER, now=104)
        with self.assertRaisesRegex(authority.AuthorityError, "relinquished"):
            self.claim(now=104)
        self.assertEqual(self.read(), expected)

    def test_old_claim_does_not_expire_custody(self):
        first = self.claim()
        with self.assertRaisesRegex(authority.AuthorityError, "dead holder"):
            self.claim("next", OTHER, now=86499)
        self.assertEqual(self.read(), first)

    def test_dead_holder_succeeds_without_new_grant(self):
        first = self.claim()
        self.live.return_value = "dead"
        second = self.claim("next", OTHER, now=102)
        expected = dict(first, custody={
            "session_id": "next", "anchor_pid": 43, "anchor_start_us": 9000002,
            "generation": 2, "claimed_at": 102, "relinquished_at": None})
        self.assertEqual(second, expected)
        self.assertEqual(self.read(), expected)
        self.live.assert_called_once_with(ANCHOR)
        self.assertFalse((self.directory / "superseded").exists())

    def test_liveness_errors_and_nonvalues_are_unknown(self):
        first = self.claim()
        for value in ("unknown", "ALIVE", "", None, False, 0, {}, [], host.Unknown("probe")):
            with self.subTest(value=value):
                self.live.return_value = value
                with self.assertRaisesRegex(authority.AuthorityError, "dead holder"):
                    self.claim("next", OTHER)
        for error in (PermissionError("EPERM"), RuntimeError("inspection failed")):
            self.live.side_effect = error
            with self.assertRaisesRegex(authority.AuthorityError, "dead holder"):
                self.claim("next", OTHER)
        self.assertEqual(self.read(), first)

    def test_mandate_change_persists_before_refusal(self):
        first = self.claim()
        expected = dict(first, invalidation=authority.evaluate(first, 102, b"changed")[1])
        with self.assertRaisesRegex(authority.AuthorityError, "mandate_changed"):
            self.claim(now=102, mandate=b"changed")
        self.assertEqual(self.read(), expected)
        with self.assertRaisesRegex(authority.AuthorityError, "invalidated"):
            self.claim(now=103)
        self.assertEqual(self.read(), expected)
        self.live.assert_not_called()

    def test_expired_and_revoked_refuse_before_liveness(self):
        first = self.claim()
        with self.assertRaisesRegex(authority.AuthorityError, "expired"):
            self.claim("next", OTHER, now=first["expires_at"])
        self.assertEqual(self.read(), first)
        terminal = store.revoke(self.directory, 102, "stop")
        with self.assertRaisesRegex(authority.AuthorityError, "invalidated"):
            self.claim("next", OTHER, now=103)
        self.assertEqual(self.read(), terminal)
        self.live.assert_not_called()

    def test_claims_serialize_load_decide_write(self):
        entered, release, attempting = threading.Event(), threading.Event(), threading.Event()
        load = store._load

        def held_load(path):
            result = load(path)
            if not entered.is_set():
                entered.set()  # First has read the empty custody, inside its lock.
                if not release.wait(10):
                    raise AssertionError("first claimant release deadline")
            return result

        def second_claim():
            attempting.set()
            try:
                return self.claim("second", OTHER)
            except authority.AuthorityError as exc:
                return exc

        with mock.patch.object(store, "_load", side_effect=held_load), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.claim)
            try:
                self.assertTrue(entered.wait(5), "first load deadline")
                second = pool.submit(second_claim)
                self.assertTrue(attempting.wait(5), "second start deadline")
                # Wait briefly for an unlocked second claim to finish; the
                # decisive assertion below requires exactly one success.
                try:
                    second.result(timeout=0.2)
                except TimeoutError:
                    pass
            finally:
                release.set()
            first_result, second_result = first.result(timeout=5), second.result(timeout=5)
        self.assertIsInstance(second_result, authority.AuthorityError)
        self.assertIn("dead holder", str(second_result))
        self.assertEqual(self.read(), first_result)
        self.live.assert_called_once_with(ANCHOR)

    def test_custody_mutations_use_locked_atomic_write(self):
        replace = store.os.replace
        operations = []

        def checked_replace(src, dst):
            with (self.directory / store.LOCK_NAME).open("rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(Path(src).parent, self.directory)
            authority.decode(Path(src).read_bytes())
            operations.append(Path(dst))
            return replace(src, dst)

        with mock.patch.object(store.os, "replace", side_effect=checked_replace):
            self.claim()
            store.relinquish(self.directory, 102, "native", ANCHOR)
        self.assertEqual(operations, [self.path, self.path])

    def test_supplied_replacement_custody_must_match(self):
        first = self.claim()
        new = copy.deepcopy(first)
        new["grant_id"] = "b" * 32
        new["custody"]["session_id"] = "changed"
        with self.assertRaisesRegex(authority.AuthorityError, "carry over unchanged"):
            store.replace(self.directory, new)
        self.assertEqual(self.read(), first)
        new["custody"] = copy.deepcopy(first["custody"])
        self.assertEqual(store.replace(self.directory, new), new)

    def test_lease_replacement_carries_custody_and_blocks_successor(self):
        repo, state = self.directory / "repo", self.directory / "state"
        state.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(registry, "d5_active", return_value=True))
        stack.enter_context(mock.patch.object(registry.CP, "_external_base",
                                              return_value=self.directory / "external"))
        self.directory = Path(ready(self, repo, "p", state)["record_dir"])
        self.path = self.directory / store.RECORD_NAME
        store.create(self.directory, grant())
        first = self.claim()
        raw = self.path.read_bytes()
        args = {key: first[key] for key in ("issued_at", "confirmed_at", "expires_at", "scope")}
        args.update(first["confirmation"])
        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                if fresh:
                    args["owner_response"] += " fresh"
                    args["expires_at"] += 1
                    args["issued_at"] += 1
                second = lease.grant(repo, "p", state, mandate_bytes=MANDATE, **args)
                self.assertNotEqual(second["grant_id"], first["grant_id"])
                self.assertEqual(second["custody"], first["custody"])
                self.assertEqual(self.read(), second)
                self.assertEqual((self.directory / "superseded" / (first["grant_id"] + ".json")).read_bytes(), raw)
                with self.assertRaisesRegex(authority.AuthorityError, "dead holder"):
                    self.claim("next", OTHER)
                first, raw = second, self.path.read_bytes()


class TestCustodyAPI(unittest.TestCase):
    def setUp(self):
        isolate_pool(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.repo, self.state = self.directory / "repo", self.directory / "state"
        subprocess.run(["git", "init", str(self.repo)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.state.mkdir()
        (self.repo / "docs").mkdir()
        (self.repo / "docs/orchestrator-mandate.md").write_bytes(MANDATE)
        clock = mock.patch.object(install.time, "time", return_value=101)
        clock.start()
        self.addCleanup(clock.stop)
        self.base = self.directory / "external"
        for name, target, value in (("base_patch", registry.CP, ("_external_base", self.base)),
                                    ("anchor", host, ("session_anchor", ("native", ANCHOR))),
                                    ("live", host, ("liveness", "alive"))):
            patch = mock.patch.object(target, value[0], return_value=value[1])
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

        # Keep readiness valid while the operational registry is absent.
        if self._testMethodName == "test_requires_registration_before_anchor":
            with mock.patch.object(registry.CP, "_external_base",
                                   return_value=self.directory / "installed-registry"):
                ready(self, self.repo, "p", self.state)

    def register(self, record=True):
        with mock.patch.object(registry, "d5_active", return_value=True):
            entry = ready(self, self.repo, "p", self.state,
                          promote=getattr(self, "promote", True))
        if record:
            store.create(entry["record_dir"], grant())
        return entry

    def claim(self):
        return custody.claim(self.repo, "p", self.state, now=101, mandate_bytes=MANDATE)

    def relinquish(self):
        return custody.relinquish(self.repo, "p", self.state, now=102)

    def status(self, now=103):
        return custody.status(self.repo, "p", self.state, now=now)

    def tree(self):
        return {str(p.relative_to(self.directory)):
                (p.lstat().st_ino, p.lstat().st_mode, p.lstat().st_mtime_ns,
                 p.read_bytes() if p.is_file() else None)
                for p in self.directory.rglob("*")}

    @mock.patch.object(registry, "d5_active", new=lambda: False)
    def test_inactive_refuses_without_changes(self):
        self.register()
        before = self.tree()
        self.assertFalse(registry.d5_active())
        with mock.patch.object(registry, "d5_active", return_value=False), \
                mock.patch.object(registry, "lookup", side_effect=AssertionError("lookup")):
            for operation in (self.claim, self.relinquish):
                with self.assertRaisesRegex(authority.AuthorityError, "D5 is not active"):
                    operation()
        self.anchor.assert_not_called()
        self.assertEqual(self.tree(), before)

    def test_requires_registration_before_anchor(self):
        before = self.tree()
        with mock.patch.object(registry, "d5_active", return_value=True):
            for operation in (self.claim, self.relinquish):
                with self.assertRaisesRegex(authority.AuthorityError, "not registered"):
                    operation()
        self.anchor.assert_not_called()
        self.assertEqual(self.tree(), before)

    def test_unknown_anchor_refuses_without_fallback(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        before = self.tree()
        self.anchor.return_value = host.Unknown("no verified native anchor")
        with mock.patch.object(registry, "d5_active", return_value=True), ExitStack() as stack:
            # R9-UNKNOWN-FALLBACK: no authority-store operation may be entered.
            # Registry's strict byte reader remains available to resolve identity.
            for name in ("claim", "relinquish", "create", "replace", "revoke", "check", "read"):
                stack.enter_context(mock.patch.object(store, name, side_effect=AssertionError(name)))
            for operation, reason in ((self.claim, "D5 anchor is not verifiable"),
                                      (self.relinquish, "no verified native anchor")):
                with self.assertRaisesRegex(authority.AuthorityError, "^" + reason + "$"):
                    operation()
        after = self.tree()
        allowed = {str((directory / name).relative_to(self.directory))
                   for name in ("authority.drain.lock", "authority.start.lock")}
        self.assertLessEqual(set(after) - set(before), allowed)
        for relative in set(after) - set(before):
            info = (self.directory / relative).lstat()
            self.assertTrue(stat.S_ISREG(info.st_mode), relative)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600, relative)
            del after[relative]
        # Creating the allowed entries changes their containing directory's mtime.
        # Keep its inode/mode and all other paths' complete snapshots exact.
        parent = str(directory.relative_to(self.directory))
        self.assertEqual(after[parent][:2], before[parent][:2])
        after[parent] = (after[parent][0], after[parent][1], before[parent][2], after[parent][3])
        self.assertEqual(after, before)
        record = str((directory / store.RECORD_NAME).relative_to(self.directory))
        self.assertEqual(self.tree()[record], before[record])
        self.live.assert_not_called()

    def test_api_claim_and_relinquish_reinspect_identity(self):
        entry = self.register()
        with mock.patch.object(registry, "d5_active", return_value=True):
            result = self.claim()
            self.assertEqual(result["custody"]["session_id"], "native")
            self.anchor.return_value = ("changed", ANCHOR)
            with self.assertRaisesRegex(authority.AuthorityError, "exact holder"):
                self.relinquish()
            self.anchor.return_value = ("native", ANCHOR)
            result = self.relinquish()
        self.assertEqual(result["custody"]["relinquished_at"], 102)
        self.assertEqual(authority.decode(store.read_bytes(Path(entry["record_dir"]) / store.RECORD_NAME)), result)
        self.assertEqual(self.anchor.call_count, 3)

    @mock.patch.object(registry, "d5_active", new=lambda: False)
    def test_status_absent_and_ungranted_create_nothing(self):
        for registered in (False, True):
            entry = self.register(record=False) if registered else None
            if entry:
                (self.base / "hanig-authority/v1/registry.lock").unlink()
            before = self.tree()
            self.assertEqual(self.status(), {"state": "inactive", "registration": entry,
                                            "custody": None, "liveness": "unknown", "is_holder": False})
            self.assertEqual(self.tree(), before)
        self.live.assert_not_called()

    def test_status_uses_bytes_never_persists_or_creates_locks(self):
        entry = self.register()
        directory = Path(entry["record_dir"])
        first = store.claim(directory, 101, MANDATE, "native", ANCHOR, host.liveness)
        (directory / store.LOCK_NAME).unlink()
        (self.base / "hanig-authority/v1/registry.lock").unlink()
        before = self.tree()
        for active in (False, True):
            for now in (102, 86501):
                with self.subTest(active=active, now=now), \
                        mock.patch.object(registry, "d5_active", return_value=active), \
                        mock.patch.object(store, "read", side_effect=AssertionError("locking read")), \
                        mock.patch.object(store, "check", side_effect=AssertionError("persistent check")), \
                        mock.patch.object(authority, "evaluate", side_effect=AssertionError("evaluation")):
                    result = self.status(now)
                self.assertEqual(result, {"state": "active" if active else "inactive",
                    "registration": entry, "custody": first["custody"],
                    "liveness": "alive", "is_holder": True})
                self.assertEqual(self.tree(), before)
        self.live.assert_called_with(ANCHOR)

    def test_status_identity_liveness_and_terminal_custody_are_diagnostic(self):
        entry = self.register()
        store.claim(entry["record_dir"], 101, MANDATE, "native", ANCHOR, host.liveness)
        store.relinquish(entry["record_dir"], 102, "native", ANCHOR)
        store.check(entry["record_dir"], 103, b"changed")
        before = self.tree()
        for identity, expected in ((("native", ANCHOR), True), (("other", ANCHOR), False),
                                   (("native", OTHER), False), (host.Unknown("missing"), False)):
            for live in ("alive", "dead", "unknown", None):
                self.anchor.return_value, self.live.return_value = identity, live
                result = self.status()
                self.assertEqual(result["is_holder"], expected)
                self.assertEqual(result["liveness"], live if live is not None else "unknown")
                self.assertEqual(result["custody"]["relinquished_at"], 102)
                self.assertEqual(self.tree(), before)
        self.live.side_effect = PermissionError("probe")
        self.assertEqual(self.status()["liveness"], "unknown")

    def test_status_malformed_record_refuses_without_writes(self):
        entry = self.register()
        (Path(entry["record_dir"]) / store.RECORD_NAME).write_bytes(b"{broken")
        before = self.tree()
        with self.assertRaisesRegex(authority.AuthorityError, "invalid JSON"):
            self.status()
        self.assertEqual(self.tree(), before)


if __name__ == "__main__":
    unittest.main()
