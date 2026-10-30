"""Installation infrastructure, with operational enforcement deferred to U4.0b."""
from contextlib import nullcontext
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType
import unittest
from unittest import mock

from tests import test_d5_registry as fixtures
from tests.fixtures.d5.ready import ready, ready_installation, missing_installation
import d5_install as install
import d5_readiness as R
import swarm_state as SS
import d5_effect_log as ledger


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

    def test_missing_installation_targets_keep_consumer_error_types(self):
        for missing in ('state', 'record'):
            with self.subTest(missing=missing):
                entry = ready_installation(self.fixture.d / ('missing-' + missing))
                state, record = Path(entry['state_dir']), Path(entry['record_dir'])
                original = state if missing == 'state' else record
                moved = original.with_name(original.name + '-moved')
                original.rename(moved)
                selected = record if missing == 'state' else moved
                try:
                    with mock.patch.object(install, '_anchor', side_effect=AssertionError('anchor')):
                        with self.assertRaises(R.registry.AuthorityError):
                            R.installation(selected)
                        with self.assertRaises(R.registry.AuthorityError):
                            install.install_initial(selected, None)
                        with self.assertRaisesRegex(ledger.EffectError, ledger.INVALID):
                            ledger._installation_state(selected)
                finally:
                    moved.rename(original)

    def test_invalid_installation_path_keeps_authority_error(self):
        entry = ready_installation(self.fixture.d / 'invalid-binding')
        value = R.installation(entry['record_dir'])
        first, second = self.fixture.d / 'cycle-a', self.fixture.d / 'cycle-b'
        first.symlink_to(second)
        second.symlink_to(first)
        value['state_dir'] = str(first)
        with self.assertRaisesRegex(R.registry.AuthorityError, 'd5_install'):
            R._validated(value)

    def test_staging_create_only_without_record_locks_or_authority(self):
        with mock.patch.object(R.protocol, "pin", side_effect=AssertionError("record lock")), \
             nullcontext():
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
                                 ("write", R.SENTINEL), ("write", R.INSTALLATION), ("write", R.MARKER)])
        self.assertFalse((self.record / "authority.json").exists())
        self.assertFalse(SS.holds_lease(self.state))
        authority = self.record / "authority.json"
        authority.write_bytes(b"opaque existing authority")
        os.utime(authority, (1, 1))
        prior = authority.stat()
        self.lease()
        install.promote(*self.args)
        read = Path.read_bytes

        def read_with_atime(path):
            raw = read(path)
            # Make the test's own read visible even on mounts deferring atime.
            os.utime(path, ns=(prior.st_atime_ns + 1000000000, prior.st_mtime_ns))
            return raw

        with mock.patch.object(Path, "read_bytes", read_with_atime):
            self.assertEqual(authority.stat(), prior)
            self.assertEqual(authority.read_bytes(), b"opaque existing authority")
        self.assertNotEqual(R.require(self.state)["generation"], value["generation"])

    def test_crash_at_each_publication_io_leaves_absent_or_matching_readiness(self):
        self.promoted()
        original_fsync, original_replace, original_unlink = os.fsync, os.replace, Path.unlink
        # Withdrawal unlink/sync, installation file sync/rename/directory sync,
        # sentinel and readiness sync/rename/directory sync, including rename sides.
        for point in range(11):
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
                    # Python 3.9 pathlib caches unlink; intercept its public
                    # method so withdrawal is exercised on both interpreters.
                    for owner, name, original in ((os, "fsync", original_fsync),
                                                  (os, "replace", original_replace),
                                                  (Path, "unlink", original_unlink)):
                        stack.enter_context(mock.patch.object(owner, name, autospec=True,
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
        base = "cdcbf6377a72cdac456f355d8cf86d169ab5edf8"
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
        outcomes = json.loads((fixtures.ROOT / 'tests/fixtures/d5/historical-inactive-outcomes.json').read_text())[base]
        # The historical modules are measured in a separate interpreter, with
        # their own dependencies. Current entries use a real installed binding
        # whose two local witnesses are absent for this inactive comparison.
        self.promoted()
        absent = missing_installation(self.entry)
        absent.__enter__()
        self.addCleanup(absent.__exit__, None, None, None)
        cases = [(module, name, (self.record,) + args[1:] if module is install else args, kwargs)
                 for module, name, args, kwargs in cases]

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
                expected = (True, [], [], tuple(outcomes[module.__name__ + "." + name]))
                actual = capture(getattr(module, name), args, kwargs)
                self.assertEqual(actual, expected)
                self.assertEqual(actual[:3], (True, [], []))
                self.assertEqual(actual[3][0], "AuthorityError")


    def contender(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import swarm_state as s; "
                "ok,why=s.acquire_lease(sys.argv[2],bump_epoch=False); print(ok); "
                "s.release_lease(sys.argv[2])")
        result = subprocess.run([sys.executable, "-c", code,
            str(fixtures.ROOT / "skills/hanig-swarm/scripts"), str(self.state)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip() == "True"

    def overlap(self, race):
        first, second, exited, finish, queued, leave_first, queried, enter_second = [
            threading.Event() for _ in range(8)]
        holds, acquire = SS.holds_lease, SS.acquire_lease
        body = threading.RLock()
        errors = []

        class ObservedBody:
            def __enter__(inner):
                if threading.current_thread().name == "second":
                    queued.set()  # Counted, about to wait for the real body lock.
                body.acquire()

            def __exit__(inner, *exc):
                body.release()

        def query(path):
            observed = holds(path)
            self.assertFalse(observed)
            queried.set()
            self.assertTrue(enter_second.wait(30))
            return observed

        def worker(is_first):
            try:
                if not is_first:
                    if not race:
                        self.assertTrue(holds(self.state))  # Already-held sibling.
                    enter_second.set()
                with R.installer_lease(self.state):
                    (first if is_first else second).set()
                    if not is_first:
                        queued.set()  # Also wake the observer if serialization is lost.
                    self.assertTrue((leave_first if is_first else finish).wait(30))
            except BaseException as exc:
                errors.append(exc)
            finally:
                if is_first:
                    exited.set()

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(R.threading, "RLock",
                                                  return_value=ObservedBody()))
            acquired = stack.enter_context(mock.patch.object(SS, "acquire_lease", wraps=acquire))
            if race:
                stack.enter_context(mock.patch.object(SS, "holds_lease", side_effect=query))
            threads = [threading.Thread(target=worker, args=(value,), name=name)
                       for value, name in ((True, "first"), (False, "second"))]
            threads[0].start()
            try:
                self.assertTrue((queried if race else first).wait(30))
                threads[1].start()
                self.assertTrue(first.wait(30))
                self.assertTrue(queued.wait(30))
                self.assertFalse(second.is_set(), "installer bodies overlapped")
                self.assertFalse(self.contender())
                leave_first.set()
                self.assertTrue(exited.wait(30))
                self.assertTrue(second.wait(30))
                self.assertTrue(second.is_set())
                self.assertEqual(errors, [])
                self.assertTrue(holds(self.state), "second installer lost its lease")
                self.assertFalse(self.contender())
                self.assertEqual(acquired.call_count, 1)
            finally:
                enter_second.set()
                leave_first.set()
                finish.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertFalse(holds(self.state))
        self.assertTrue(self.contender())

    def test_overlapping_installers_retain_live_lease_until_both_exit(self):
        self.overlap(race=True)

    def test_second_installer_observes_already_held_lease(self):
        self.overlap(race=False)

    def test_outer_borrowed_lease_is_never_released(self):
        self.lease()
        with mock.patch.object(SS, "release_lease", wraps=SS.release_lease) as release:
            with self.assertRaisesRegex(ValueError, "body failed"):
                with R.installer_lease(self.state):
                    with R.installer_lease(self.state):
                        self.assertTrue(SS.holds_lease(self.state))
                        raise ValueError("body failed")
            release.assert_not_called()
        self.assertTrue(SS.holds_lease(self.state))
        self.assertFalse(self.contender())

    def test_nested_same_thread_is_reentrant_and_releases_only_last(self):
        # A subprocess deadline turns a non-reentrant-lock mutation into a
        # bounded test failure, never a wedged test worker.
        code = """
import sys
from pathlib import Path
for folder in ("skills/hanig-swarm/scripts", "skills/hanig-orchestrate/scripts"):
    sys.path.insert(0, str(Path(sys.argv[1]) / folder))
import d5_readiness as r
import swarm_state as s
with r.installer_lease(sys.argv[2]):
    with r.installer_lease(sys.argv[2]):
        assert s.holds_lease(sys.argv[2])
    assert s.holds_lease(sys.argv[2])
assert not s.holds_lease(sys.argv[2])
"""
        result = subprocess.run([sys.executable, "-c", code, str(fixtures.ROOT),
                                 str(self.state)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.contender())

    def test_fork_resets_count_and_guard_without_releasing_parent(self):
        import signal
        # Fork while a different thread holds the accounting guard. The child
        # must refuse the parent's lease, rather than borrowing its count or
        # deadlocking on the vanished thread's lock.
        held, finish = threading.Event(), threading.Event()

        def hold_guard():
            with R._INSTALLER_GUARD:
                held.set()
                finish.wait(30)

        context = R.installer_lease(self.state)
        context.__enter__()
        try:
            thread = threading.Thread(target=hold_guard)
            thread.start()
            self.assertTrue(held.wait(30))
            child = os.fork()
            if child == 0:
                signal.alarm(10)
                try:
                    assert not SS.holds_lease(self.state)
                    # Unwind the inherited context before a new acquisition:
                    # release_lease has no pid check and must not be called.
                    context.__exit__(None, None, None)
                    try:
                        with R.installer_lease(self.state):
                            os._exit(2)
                    except R.registry.AuthorityError as exc:
                        assert "lease unavailable" in str(exc)
                    os._exit(0)
                except BaseException:
                    os._exit(3)
            finish.set()
            thread.join(30)
            self.assertFalse(thread.is_alive())
            self.assertEqual(os.waitpid(child, 0)[1], 0)
            self.assertTrue(SS.holds_lease(self.state))
            self.assertFalse(self.contender())
        finally:
            finish.set()
            thread.join(30)
            context.__exit__(None, None, None)
        self.assertTrue(self.contender())

    def test_aliases_share_accounting_and_distinct_states_do_not_serialize(self):
        alias = self.fixture.d / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        other = self.fixture.d / "independent-state"
        other.mkdir()
        entered, finish = threading.Event(), threading.Event()
        errors = []

        def independent():
            try:
                with R.installer_lease(other):
                    entered.set()
                    self.assertTrue(finish.wait(30))
            except BaseException as exc:
                errors.append(exc)

        with R.installer_lease(self.state):
            with R.installer_lease(alias):
                self.assertTrue(SS.holds_lease(self.state))
            self.assertTrue(SS.holds_lease(self.state))
            thread = threading.Thread(target=independent)
            thread.start()
            try:
                self.assertTrue(entered.wait(30))
            finally:
                finish.set()
                thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(SS.holds_lease(other))
        self.assertFalse(SS.holds_lease(self.state))


if __name__ == "__main__":
    unittest.main()
