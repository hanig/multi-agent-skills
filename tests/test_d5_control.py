"""R2 controls through real registration/store; lock evidence uses hooks/probes."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_authority as authority
import d5_authority_store as store
import d5_control as control
import d5_effect_log as ledger
import d5_host as host
import d5_install as install
import d5_locks as locks
import d5_protocol_locks as protocol
import d5_registry as registry
from tests import test_d5_locks as lock_tests
from tests.test_d5_authority import grant
from tests.fixtures.d5.ready import ready

PEER = ROOT / "tests/fixtures/d5/protocol_peer.py"
MANDATE = b"fixture mandate\r\n\xff"
ANCHOR = host.ProcessIdentity(42, 9000001)


class TestD5Control(unittest.TestCase):
    def setUp(self):
        lock_tests.isolate_pool(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo, self.state = self.root / "repo", self.root / "state"
        self.git("init", str(self.repo))
        self.state.mkdir(mode=0o700)
        (self.repo / "docs").mkdir()
        self.mandate = self.repo / "docs/orchestrator-mandate.md"
        self.mandate.write_bytes(MANDATE)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(registry.CP, "_external_base",
                                                   return_value=self.root / "external"))
        self.active = self.stack.enter_context(mock.patch.object(registry, "d5_active", return_value=True))
        self.anchor = self.stack.enter_context(mock.patch.object(host, "session_anchor",
                                                                return_value=("native", ANCHOR)))
        self.args = self.repo, "p", self.state
        self.entry = ready(self, *self.args, promote=False)
        self.directory = Path(self.entry["record_dir"])
        record = grant()
        record.update(expires_at=200, mandate_sha256=authority.mandate_digest(MANDATE),
                      scope=dict(plans=["p"], repositories=[], actions=[], trackers=[]),
                      confirmation=dict(owner_response="fixture", displayed_bounds="fixture"))
        install.install_initial(self.directory, record)
        self.assertEqual(self.anchor.call_count, 1)
        self.anchor.reset_mock()
        self.original = store.claim(self.directory, 101, MANDATE, "native", ANCHOR,
                                    lambda _: "alive")
        self.path = self.directory / store.RECORD_NAME

    def git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True, timeout=30)

    def calls(self):
        return (("revoke", lambda: control.revoke(*self.args, 150, "stop")),
                ("check", lambda: control.record_mandate_invalidation(*self.args, now=150)),
                ("relinquish", lambda: control.relinquish(*self.args, now=150)))

    def read(self):
        return authority.decode(self.path.read_bytes())

    def reset(self):
        self.path.write_bytes(authority.encode(self.original))

    def probe(self, rank=1):
        result = subprocess.run([sys.executable, str(PEER), "probe", str(self.directory), str(rank)],
                                capture_output=True, timeout=10, check=True)
        return result.stdout.decode().strip()

    def launch(self, function):
        results = []

        def run():
            try:
                results.append(function())
            except BaseException as exc:
                results.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, results

    def joined(self, thread, results):
        thread.join(15)
        self.assertFalse(thread.is_alive(), "thread watchdog")
        self.assertEqual(len(results), 1)
        if isinstance(results[0], BaseException):
            raise results[0]
        return results[0]

    def test_control_orders_with_start(self):
        # R2-OMIT-START: probe an independent open description at the real
        # store entry AND its write. No mock supplies a store decision.
        self.mandate.write_bytes(b"changed")
        for name, call in self.calls():
            self.reset()
            original = getattr(store, name)
            writes = []
            write = store._write

            def checked(*args):
                self.assertEqual(self.probe(), "blocked")
                return original(*args)

            def writing(path, raw):
                self.assertEqual(self.probe(), "blocked")
                write(path, raw)
                writes.append(authority.decode(raw))

            with self.subTest(control=name), mock.patch.object(store, name, side_effect=checked), \
                    mock.patch.object(store, "_write", side_effect=writing), \
                    mock.patch.object(protocol._Protocol, "drain", side_effect=AssertionError("DRAIN")):
                result = call()
            self.assertEqual(writes, [self.read()])
            if name == "check":
                self.assertEqual(result, "mandate_changed")
                self.assertEqual(self.read()["invalidation"]["kind"], "mandate_changed")
            else:
                expected = (authority.revoked(self.original, 150, "stop") if name == "revoke" else
                            authority.relinquished(self.original, "native", ANCHOR.pid, ANCHOR.start_us, 150))
                self.assertEqual(result, expected)
                self.assertEqual(self.read(), expected)
            self.assertEqual(self.probe(), "available")

    def test_control_waits_for_peer_start(self):
        for name, call in self.calls():
            self.reset()
            attempted = threading.Event()
            trace = []
            original = getattr(store, name)

            def entered(*args):
                trace.append("store")
                attempted.set()
                return original(*args)

            with protocol.pin(self.directory) as pinned:
                guard = pinned._record.entry.guards[1]

                class ObservedGuard:
                    def __enter__(inner):
                        if threading.current_thread() is not threading.main_thread():
                            got = guard.acquire(blocking=False)
                            trace.append("acquired" if got else "blocked")
                            attempted.set()
                            if got:
                                return
                        guard.acquire()

                    def __exit__(inner, *args):
                        guard.release()

                self.addCleanup(pinned._record.entry.guards.__setitem__, 1, guard)
                with mock.patch.dict(pinned._record.entry.guards, {1: ObservedGuard()}), \
                        mock.patch.object(store, name, side_effect=entered):
                    thread, results = None, None
                    try:
                        with pinned.start():
                            thread, results = self.launch(call)
                            self.assertTrue(attempted.wait(10), "acquisition watchdog")
                            self.assertEqual(trace, ["blocked"])
                    finally:
                        if thread is not None:
                            self.joined(thread, results)
            self.assertEqual(trace, ["blocked", "store"])

    def test_revoke_records_during_protected_response_wait(self):
        # U3-M4 / R2-WAIT-DRAIN. A durable submitted fixture waits on an event
        # under shared DRAIN; revocation persists before the response is released.
        submitted, release, persisted = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def effect():
            with protocol.pin(self.directory) as pinned:
                with pinned.drain():
                    with pinned.start():
                        attempt = ledger.prepare(*self.args, record_id=self.original["grant_id"],
                            custody={k: self.original["custody"][k] for k in
                                     ("generation", "session_id", "anchor_pid", "anchor_start_us")},
                            kind="fixture", scope=self.original["scope"], correlation={"request_id": "A"})
                        receipt = ledger._classify(attempt, "submitted")
                    submitted.set()
                    if not release.wait(15):
                        raise AssertionError("response watchdog")
                    return ledger.complete(receipt, {"status": "succeeded", "detail": "A"})

        thread, results = self.launch(effect)
        try:
            self.assertTrue(submitted.wait(10), "submission watchdog")
            self.assertEqual(self.probe(0), "blocked")
            write = store._write

            def recorded(path, raw):
                write(path, raw)
                self.assertFalse(release.is_set())
                self.assertEqual(self.read()["invalidation"]["kind"], "revoked")
                persisted.set()

            with mock.patch.object(store, "_write", side_effect=recorded), \
                    mock.patch.object(protocol._Protocol, "drain", side_effect=AssertionError("DRAIN on control")):
                control.revoke(*self.args, 150, "stop")
            self.assertTrue(persisted.is_set())
            self.assertEqual(self.probe(0), "blocked")
        finally:
            release.set()
            self.joined(thread, results)
        self.assertEqual(ledger.unresolved(*self.args), [])
        self.assertEqual(self.probe(0), "available")

    def test_invalidation_reads_current_mandate(self):
        # R2-CACHED-MANDATE (between calls AND before START).
        self.assertEqual(control.record_mandate_invalidation(*self.args, now=150), "valid")
        self.assertIsNone(self.read()["invalidation"])
        (self.root / "unregistered.md").write_bytes(MANDATE)
        start = protocol._Protocol.start
        current = b"fixture mandate\n\xff"  # CRLF is significant, arbitrary bytes survive.

        @contextmanager
        def changed_after_start(pinned):
            with start(pinned):
                self.mandate.write_bytes(current)
                yield

        check = store.check

        def checked(directory, now, raw):
            self.assertEqual(raw, current)
            self.assertEqual(self.probe(), "blocked")
            return check(directory, now, raw)

        with mock.patch.object(protocol._Protocol, "start", changed_after_start), \
                mock.patch.object(store, "check", side_effect=checked):
            self.assertEqual(control.record_mandate_invalidation(*self.args, now=151), "mandate_changed")
        self.assertEqual(self.read()["invalidation"],
                         {"at": 151, "kind": "mandate_changed", "reason": "mandate byte digest changed"})
        self.mandate.write_bytes(MANDATE)
        self.assertEqual(control.record_mandate_invalidation(*self.args, now=152), "invalidated")
        self.assertEqual(self.read()["invalidation"]["at"], 151)

    def test_registered_checkout_is_source(self):
        # R2-WRONG-SOURCE: a legitimate linked caller is not the registered source.
        self.git("-C", str(self.repo), "-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture")
        linked = self.root / "linked"
        self.git("-C", str(self.repo), "worktree", "add", "-b", "linked", str(linked), "HEAD")
        (linked / "docs").mkdir()
        (linked / "docs/orchestrator-mandate.md").write_bytes(MANDATE)
        self.mandate.write_bytes(b"current registered bytes")
        self.assertEqual(registry.lookup(linked, "p", self.state), self.entry)
        self.assertEqual(control.record_mandate_invalidation(linked, "p", self.state, now=150),
                         "mandate_changed")
        self.assertEqual(self.read()["invalidation"]["kind"], "mandate_changed")

    def test_mandate_source_refusals_do_not_call_store(self):
        # R2-FOLLOW-SYMLINK / R2-NONREGULAR. The FIFO also pins NONBLOCK.
        other = self.root / "old.md"
        other.write_bytes(MANDATE)
        opening = os.open

        def checked_open(path, flags, *args, **kwargs):
            if Path(path) == self.mandate:
                # R2-BLOCK-FIFO: fail before a mutated blocking open can hang.
                self.assertTrue(flags & os.O_NONBLOCK)
            return opening(path, flags, *args, **kwargs)

        for kind in ("missing", "symlink", "directory", "fifo"):
            self.mandate.unlink()
            if kind == "symlink":
                self.mandate.symlink_to(other)
            elif kind == "directory":
                self.mandate.mkdir()
            elif kind == "fifo":
                os.mkfifo(self.mandate)
            before = self.path.read_bytes()
            try:
                with self.subTest(kind=kind), mock.patch.object(store, "check") as check, \
                        mock.patch.object(os, "open", side_effect=checked_open):
                    with self.assertRaises((OSError, authority.AuthorityError)):
                        control.record_mandate_invalidation(*self.args, now=150)
                    check.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.probe(), "available")
            finally:
                if kind == "directory":
                    self.mandate.rmdir()
                elif kind != "missing":
                    self.mandate.unlink()
                self.mandate.write_bytes(MANDATE)

    def test_unreadable_mandate_does_not_call_store(self):
        # R2-IGNORE-READ-ERROR: failures opening or reading cannot supply bytes.
        opening, fdopen = os.open, os.fdopen

        def denied(path, *args, **kwargs):
            if Path(path) == self.mandate:
                raise PermissionError("fixture open")
            return opening(path, *args, **kwargs)

        def broken(fd, *args, **kwargs):
            handle = fdopen(fd, *args, **kwargs)
            if os.fstat(fd).st_ino == self.mandate.stat().st_ino:
                handle.read = mock.Mock(side_effect=OSError("fixture read"))
            return handle

        for name, fault in (("open", denied), ("fdopen", broken)):
            before = self.path.read_bytes()
            with self.subTest(fault=name), mock.patch.object(os, name, side_effect=fault), \
                    mock.patch.object(store, "check") as check:
                with self.assertRaises(OSError):
                    control.record_mandate_invalidation(*self.args, now=150)
                check.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.probe(), "available")

    def test_standalone_read_error_regression(self):
        # R2-STANDALONE-IMPORT: the reported file command must reach its test,
        # including outside the repository and without an ambient PYTHONPATH.
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
            "TestD5Control.test_unreadable_mandate_does_not_call_store"],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_inactive_control_has_no_protocol_io(self):
        # R2-BYPASS-INACTIVE: forbid work at lookup/pin AND filesystem boundaries.
        self.active.return_value = False
        with ExitStack() as stack:
            for target, names in ((registry, ("lookup",)), (protocol, ("pin",)),
                    (os, ("open", "lstat", "stat", "mkdir")), (host, ("session_anchor",)),
                    (store, ("revoke", "check", "relinquish"))):
                for name in names:
                    stack.enter_context(mock.patch.object(target, name, side_effect=AssertionError("inactive IO")))
            for name, call in self.calls():
                with self.subTest(control=name), self.assertRaisesRegex(authority.AuthorityError, "^D5 is inactive$"):
                    call()

    def test_registration_refusals_precede_protocol_io(self):
        # R2-BYPASS-REGISTRATION: propagate absent registration and tamper gates.
        for tamper in (False, True):
            if tamper:
                (self.directory / ".git").mkdir()
            args = self.args if tamper else (self.repo, "unregistered", self.state)
            if not tamper:
                # A separate empty state avoids the existing state-identity conflict.
                state = self.root / "unregistered-state"
                state.mkdir()
                args = self.repo, "unregistered", state
            with mock.patch.object(protocol, "pin", side_effect=AssertionError("protocol IO")):
                for call in (lambda: control.revoke(*args, 150, "stop"),
                             lambda: control.record_mandate_invalidation(*args, now=150),
                             lambda: control.relinquish(*args, now=150)):
                    with self.assertRaisesRegex(authority.AuthorityError,
                                                "Git worktree" if tamper else "^not registered$"):
                        call()
            self.assertEqual(self.read(), self.original)

    def test_relinquish_reinspects_anchor_and_keeps_store_refusals(self):
        # R2-UNKNOWN-ANCHOR / R2-CACHED-ANCHOR: native inspection on each call.
        self.anchor.return_value = host.Unknown("fixture unknown identity")
        with mock.patch.object(store, "relinquish") as mutation:
            with self.assertRaisesRegex(authority.AuthorityError, "^fixture unknown identity$"):
                control.relinquish(*self.args, now=150)
            mutation.assert_not_called()
        self.anchor.return_value = ("wrong holder", ANCHOR)
        with self.assertRaisesRegex(authority.AuthorityError, "exact holder"):
            control.relinquish(*self.args, now=150)
        self.assertEqual(self.read(), self.original)
        self.anchor.return_value = ("native", ANCHOR)
        result = control.relinquish(*self.args, now=150)
        self.assertEqual(result["custody"]["relinquished_at"], 150)
        self.assertEqual(control.relinquish(*self.args, now=151), result)
        self.assertEqual(self.anchor.call_count, 4)


if __name__ == "__main__":
    unittest.main()
