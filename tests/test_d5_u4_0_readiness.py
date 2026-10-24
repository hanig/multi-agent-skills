"""Installation infrastructure, with operational enforcement deferred to U4.0b."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import unittest
from unittest import mock

from tests import test_d5_registry as fixtures
from tests.fixtures.d5.ready import ready
import d5_install as install
import d5_readiness as R
import swarm_state as SS


class TestReadiness(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestD5Registry()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.args = self.fixture.repo, "p", self.fixture.state
        self.state = self.fixture.state
        self.marker = self.state / R.MARKER

    def stage(self):
        self.entry = install.stage(*self.args)
        self.record = Path(self.entry["record_dir"])
        return self.entry

    def lease(self):
        self.assertEqual(SS.acquire_lease(self.state, bump_epoch=False), (True, None))
        self.addCleanup(SS.release_lease, self.state)

    def promoted(self):
        self.entry = ready(self, *self.args)
        self.record = Path(self.entry["record_dir"])
        return R.require(self.state)

    def test_staging_create_only_without_record_locks_or_authority(self):
        with mock.patch.object(R.protocol, "pin", side_effect=AssertionError("record lock")), \
             mock.patch.object(R.registry, "d5_active", return_value=False):
            entry = self.stage()
            before = self.fixture.tree(self.fixture.root)
            self.assertEqual(self.stage(), entry)
            self.assertEqual(self.fixture.tree(self.fixture.root), before)
            other = self.fixture.d / "other-state"
            other.mkdir()
            with self.assertRaisesRegex(R.registry.AuthorityError, "state_key mismatch"):
                install.stage(self.fixture.repo, "p", other)
            self.assertEqual(self.fixture.tree(self.fixture.root), before)
        self.assertEqual(list(self.record.iterdir()), [])
        self.assertFalse(self.marker.exists())
        self.assertFalse(SS.holds_lease(self.state))

    def test_promotion_lease_drain_start_commit_publish_order_and_no_authority(self):
        self.stage()
        trace = []
        original_open = R.protocol.locks._Record._open
        original_write = R.store.write_bytes
        original_sync = R.store.sync_directory

        def opening(record, rank):
            self.assertTrue(SS.holds_lease(self.state))
            trace.append(("lock", rank))
            return original_open(record, rank)

        def writing(path, raw):
            self.assertTrue(SS.holds_lease(self.state))
            self.assertFalse(self.marker.exists())
            if path.name == R.MARKER:
                self.assertEqual((self.record / R.INSTALLATION).read_bytes(), raw)
            trace.append(("write", path.name))
            original_write(path, raw)

        def syncing(path):
            if path == self.state:
                self.assertFalse(self.marker.exists())
                trace.append(("withdraw",))
            original_sync(path)

        with mock.patch.object(R.protocol.locks._Record, "_open", opening), \
             mock.patch.object(R.store, "write_bytes", writing), \
             mock.patch.object(R.store, "sync_directory", syncing), \
             mock.patch.object(R.store, "create", side_effect=AssertionError("grant")), \
             mock.patch.object(R.store, "replace", side_effect=AssertionError("grant")):
            value = install.promote(*self.args)
        self.assertEqual(trace, [("lock", 0), ("lock", 1), ("withdraw",),
                                 ("write", R.INSTALLATION), ("write", R.MARKER)])
        self.assertFalse((self.record / "authority.json").exists())
        self.assertFalse(SS.holds_lease(self.state))
        authority = self.record / "authority.json"
        authority.write_bytes(b"opaque existing authority")
        prior = authority.stat()
        self.lease()
        install.promote(*self.args)
        self.assertEqual(authority.read_bytes(), b"opaque existing authority")
        self.assertEqual(authority.stat(), prior)
        self.assertNotEqual(R.require(self.state)["generation"], value["generation"])

    def test_crash_at_each_publication_io_leaves_absent_or_matching_readiness(self):
        self.promoted()
        original_fsync, original_replace, original_unlink = os.fsync, os.replace, os.unlink
        # Withdrawal unlink/sync, installation file sync/rename/directory sync,
        # readiness file sync/rename/directory sync, including both rename sides.
        for point in range(8):
            for after in (False, True):
                install.promote(*self.args)
                events = []

                def intercept(operation, *args, **kwargs):
                    index = len(events)
                    events.append(operation.__name__)
                    if index == point and not after:
                        raise OSError("crash")
                    result = operation(*args, **kwargs)
                    if index == point and after:
                        raise OSError("crash")
                    return result

                with self.subTest(point=point, after=after), ExitStack() as stack:
                    for name, original in (("fsync", original_fsync),
                                           ("replace", original_replace),
                                           ("unlink", original_unlink)):
                        stack.enter_context(mock.patch.object(os, name,
                            side_effect=lambda *a, _op=original, **k: intercept(_op, *a, **k)))
                    with self.assertRaisesRegex(OSError, "crash"):
                        install.promote(*self.args)
                self.assertGreaterEqual(len(events), point + 1)
                if self.marker.exists():
                    self.assertEqual(R.require(self.state),
                                     json.loads((self.record / R.INSTALLATION).read_bytes()))

    def test_reader_refuses_missing_malformed_and_mismatched_without_processes(self):
        good = self.promoted()
        original = self.marker.read_bytes()
        bad = [b"", b"null", b"{}", b'{"schema_version":1,"schema_version":1}']
        for field, value in (("generation", "0" * 32), ("profile", "other"),
                             ("registration_id", "0" * 32), ("plan_id", "other"),
                             ("state_key", [0, 0]), ("record_key", [0, 0]),
                             ("repo_key", [0, 0]), ("state_dir", str(self.fixture.d))):
            bad.append(json.dumps(dict(good, **{field: value})).encode())
        with ExitStack() as stack:
            for name in ("Popen",):
                stack.enter_context(mock.patch.object(subprocess, name,
                    side_effect=AssertionError("subprocess")))
            for name in ("system", "fork", "posix_spawn", "posix_spawnp"):
                if hasattr(os, name):
                    stack.enter_context(mock.patch.object(os, name,
                        side_effect=AssertionError("process")))
            self.marker.unlink()
            with self.assertRaises(R.registry.AuthorityError):
                R.require(self.state)
            for raw in bad:
                with self.subTest(raw=raw):
                    self.marker.write_bytes(raw)
                    with self.assertRaises(R.registry.AuthorityError):
                        R.require(self.state)
            self.marker.write_bytes(original)
            self.assertEqual(R.require(self.state), good)
            # Equal stale marker/installation bytes still need live object identity.
            moved = self.record.with_name("retired")
            self.record.rename(moved)
            self.record.mkdir()
            (self.record / R.INSTALLATION).write_bytes(original)
            with self.assertRaisesRegex(R.registry.AuthorityError, "object mismatch"):
                R.require(self.state)

    def test_record_directory_alias_uses_identity_and_binding_is_exact(self):
        alias = self.fixture.d / "alias"
        alias.symlink_to(self.fixture.d, target_is_directory=True)
        self.fixture.external_base.return_value = alias / "external"
        value = self.promoted()
        self.assertNotEqual(value["record_dir"], str(self.record.resolve()))
        self.assertEqual(R.require(self.state), value)
        self.assertEqual(R.bound(value, self.entry), self.entry)
        for field in R.BINDING:
            changed = dict(self.entry, **{field: None})
            with self.subTest(field=field), self.assertRaisesRegex(
                    R.registry.AuthorityError, "binding mismatch"):
                R.bound(value, changed)

    def test_promotion_refuses_contended_lease_before_lookup_and_under_record_lock(self):
        self.stage()
        # Independent process holds the real coordinator lease; no inherited Python state.
        code = ("import fcntl,sys; f=open(sys.argv[1],'r'); "
                "fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); input()")
        with subprocess.Popen([__import__("sys").executable, "-c", code,
                               str(self.state / SS.LOCK)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True) as peer:
            try:
                self.assertEqual(peer.stdout.readline(), "held\n")
                with mock.patch.object(R.registry, "lookup", side_effect=AssertionError("lookup")):
                    with self.assertRaisesRegex(R.registry.AuthorityError, "lease unavailable"):
                        install.promote(*self.args)
            finally:
                peer.communicate("\n", timeout=10)
        with R.protocol.pin(self.record) as record, record.drain():
            with self.assertRaisesRegex(R.registry.AuthorityError, "under DRAIN or START"):
                install.promote(*self.args)
        self.assertFalse(self.marker.exists())

    def test_M6_inactive_operational_effects_equal_recorded_base(self):
        # Unit-local capture: load the recorded base implementations, with the
        # same real fixture and inactive predicate. No production path is patched.
        base = "17b40983a6993681d95fd9f7029eb21dbc82c5de"
        import d5_custody
        import d5_lease
        import d5_submission
        cases = [(R.registry, "register", self.args, {}),
                 (d5_lease, "grant", self.args, dict(issued_at=1, confirmed_at=1,
                  expires_at=2, mandate_bytes=b"m", scope={}, owner_response="yes",
                  displayed_bounds={})),
                 (d5_custody, "claim", self.args, dict(now=1, mandate_bytes=b"m")),
                 (d5_submission, "submit", self.args, dict(grant_id="g", session_id=1,
                  anchor=None, generation="g", operation=None, boundary=None)),
                 (install, "install_initial", (self.state, {}), {}),
                 (install, "replace_authority", (self.state, {}), {}),
                 (install, "claim_custody", (self.state,), dict(mandate_path=self.state / "m"))]
        originals = {}
        for module, _, _, _ in cases:
            if module.__name__ in originals:
                continue
            path = Path(module.__file__).relative_to(fixtures.ROOT)
            source = subprocess.check_output(["git", "show", base + ":" + str(path)],
                                             cwd=str(fixtures.ROOT))
            previous = ModuleType("_m6_" + module.__name__)
            previous.__file__ = module.__file__
            with mock.patch.dict(sys.modules, {previous.__name__: previous}):
                exec(compile(source, str(path), "exec"), previous.__dict__)
            originals[module.__name__] = previous

        def capture(call, args, kwargs):
            commands = []
            before = self.fixture.tree()

            def launched(*args, **kwargs):
                commands.append((args, kwargs))
                raise AssertionError("unexpected process")

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(subprocess, "Popen", launched))
                for name in dir(os):
                    if name.startswith(("exec", "spawn", "posix_spawn")) or name in (
                            "fork", "forkpty", "system", "popen", "startfile"):
                        if callable(getattr(os, name)):
                            stack.enter_context(mock.patch.object(os, name, launched))
                try:
                    outcome = ("return", call(*args, **kwargs))
                except Exception as exc:
                    outcome = (type(exc).__name__, str(exc))
            after = self.fixture.tree()
            rows = [p.read_bytes() for p in self.fixture.d.rglob("authority.effects/*.json")]
            return before == after, rows, commands, outcome

        for module, name, args, kwargs in cases:
            with self.subTest(entry=module.__name__ + "." + name):
                expected = capture(getattr(originals[module.__name__], name), args, kwargs)
                actual = capture(getattr(module, name), args, kwargs)
                self.assertEqual(actual, expected)
                self.assertEqual(actual[:3], (True, [], []))
                self.assertEqual(actual[3][0], "AuthorityError")


if __name__ == "__main__":
    unittest.main()
