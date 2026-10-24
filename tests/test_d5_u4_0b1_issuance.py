"""Issuance barriers through public entries; D5 remains inactive in production."""
from contextlib import contextmanager, ExitStack
import builtins
import io
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import unittest
from unittest import mock

from tests import test_d5_registry as fixtures
from tests.fixtures.d5.ready import ready
from tests.test_d5_authority import grant, MANDATE
import d5_authority_store as store
import d5_control as control
import d5_custody as custody
import d5_host as host
import d5_install as install
import d5_lease as lease
import d5_readiness as readiness
import d5_registry as registry
import swarm_state as state


@contextmanager
def no_effects():
    """Count attempted launches and writes, including caught/transient attempts."""
    launches, writes = [], []

    def refused(events, name):
        def call(*args, **kwargs):
            events.append(name)
            raise AssertionError("unexpected " + name)
        return call

    def opened(original, name):
        def call(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                return refused(writes, name)(file, mode)
            return original(file, mode, *args, **kwargs)
        return call

    original_open = os.open

    def fd_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            return refused(writes, "os.open")(path, flags)
        return original_open(path, flags, *args, **kwargs)

    with ExitStack() as stack:
        for name in ("Popen", "run", "call", "check_call", "check_output",
                     "getoutput", "getstatusoutput"):
            stack.enter_context(mock.patch.object(subprocess, name,
                               refused(launches, "subprocess." + name)))
        for name in dir(os):
            if (name.startswith(("exec", "spawn", "posix_spawn")) or name in
                    ("fork", "forkpty", "system", "popen", "startfile")):
                if callable(getattr(os, name)):
                    stack.enter_context(mock.patch.object(os, name, refused(launches, "os." + name)))
        for owner in (builtins, io):
            stack.enter_context(mock.patch.object(owner, "open", opened(owner.open, "open")))
        stack.enter_context(mock.patch.object(os, "open", fd_open))
        # Path methods are separate interception points on Python 3.9, whose
        # pathlib accessor caches os functions before this context is entered.
        for owner, names in ((os, ("write", "writev", "pwrite", "truncate", "ftruncate",
                "mkdir", "makedirs", "unlink", "remove", "rmdir", "rename", "replace",
                "link", "symlink", "chmod", "fchmod", "chown", "utime")),
                (Path, ("mkdir", "unlink", "rmdir", "rename", "replace", "touch",
                        "write_bytes", "write_text", "symlink_to", "chmod"))):
            for name in names:
                if hasattr(owner, name):
                    stack.enter_context(mock.patch.object(owner, name, refused(writes, name)))
        yield launches, writes


class TestIssuance(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestD5Registry()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.args = self.fixture.repo, "p", self.fixture.state
        self.state = self.fixture.state
        self.marker = self.state / readiness.MARKER
        self.kwargs = {key: grant()[key] for key in
                       ("issued_at", "confirmed_at", "expires_at", "scope")}
        self.kwargs.update(grant()["confirmation"], mandate_bytes=MANDATE)

    def operations(self):
        return [(registry, "register", {}), (lease, "grant", self.kwargs),
                (custody, "claim", dict(now=101, mandate_bytes=MANDATE))]

    def refused(self, module, name, kwargs, message):
        before = self.fixture.tree()
        with no_effects() as (launches, writes):
            with self.assertRaisesRegex(registry.AuthorityError, message):
                getattr(module, name)(*self.args, **kwargs)
            self.assertEqual(launches, [])
            self.assertEqual(writes, [])
        self.assertEqual(self.fixture.tree(), before)

    def test_readiness_before_registry_lookup_and_zero_effects(self):
        # Detects readiness-after-registry-lookup, even if lookup catches a
        # launch exception. Exercise both a cold and staged installation.
        with mock.patch.object(registry, "d5_active", return_value=True):
            for staged in (False, True):
                if staged:
                    ready(self, *self.args, promote=False)
                else:
                    self.assertEqual(state.acquire_lease(self.state, bump_epoch=False), (True, None))
                    self.addCleanup(state.release_lease, self.state)
                for module, name, kwargs in self.operations():
                    with self.subTest(staged=staged, entry=name), mock.patch.object(
                            registry, "lookup", side_effect=AssertionError("lookup")) as lookup:
                        self.refused(module, name, kwargs, "missing or malformed installation state")
                        lookup.assert_not_called()
                self.assertFalse(self.marker.exists())

    def test_grant_without_lease_and_lease_before_readiness(self):
        ready(self, *self.args)
        state.release_lease(self.state)
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(readiness, "_read", side_effect=AssertionError("readiness read")) as read:
            for module, name, kwargs in self.operations():
                with self.subTest(entry=name):
                    self.refused(module, name, kwargs, "caller-held coordinator lease required")
            read.assert_not_called()

    def test_claim_without_readiness_and_invalid_bindings(self):
        entry = ready(self, *self.args)
        raw = self.marker.read_bytes()
        installation = Path(entry["record_dir"]) / readiness.INSTALLATION
        for bad in (None, b"{}", b"null", b'{"schema_version":1,"schema_version":1}'):
            if bad is None:
                self.marker.unlink()
            else:
                self.marker.write_bytes(bad)
            with self.subTest(marker=bad), mock.patch.object(registry, "d5_active", return_value=True):
                self.refused(custody, "claim", dict(now=101, mandate_bytes=MANDATE), "D5 readiness:")
        self.marker.write_bytes(raw)
        installation.write_bytes(b"{}")
        with mock.patch.object(registry, "d5_active", return_value=True):
            for module, name, kwargs in self.operations():
                with self.subTest(entry=name):
                    self.refused(module, name, kwargs, "invalid installation fields")

    def test_register_cannot_provision_readiness(self):
        ready(self, *self.args, promote=False)
        with mock.patch.object(registry, "d5_active", return_value=True):
            self.refused(registry, "register", {}, "missing or malformed installation state")
        self.assertFalse(self.marker.exists())

    def test_predicate_never_publishes_readiness(self):
        ready(self, *self.args, promote=False)
        before = self.fixture.tree()
        with no_effects() as (launches, writes):
            with self.assertRaisesRegex(registry.AuthorityError, "missing or malformed"):
                readiness.require(self.state)
            self.assertEqual((launches, writes), ([], []))
        self.assertEqual(self.fixture.tree(), before)
        entry = ready(self, *self.args)
        with no_effects() as (launches, writes):
            value = readiness.require(self.state)
            self.assertEqual((launches, writes), ([], []))
        self.assertEqual(value["registration_id"], entry["registration_id"])

    def test_ready_alias_issuance_uses_live_canonical_lease_and_exact_binding(self):
        entry = ready(self, *self.args)
        alias = self.fixture.d / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        self.args = self.fixture.repo, "p", alias
        mandate = self.fixture.repo / "docs/orchestrator-mandate.md"
        mandate.parent.mkdir()
        mandate.write_bytes(MANDATE)
        anchor = host.ProcessIdentity(42, 9000001)
        with mock.patch.object(registry, "d5_active", return_value=True), \
                mock.patch.object(host, "session_anchor", return_value=("native", anchor)), \
                mock.patch.object(install.time, "time", return_value=101), \
                mock.patch.object(state, "holds_lease", wraps=state.holds_lease) as held:
            self.assertEqual(registry.register(*self.args), entry)
            issued = lease.grant(*self.args, **self.kwargs)
            claimed = custody.claim(*self.args, now=101, mandate_bytes=MANDATE)
            self.assertEqual(claimed["grant_id"], issued["grant_id"])
            self.assertEqual(claimed["custody"]["session_id"], "native")
            self.assertTrue(held.call_args_list)
            for call in held.call_args_list:
                self.assertEqual(call.args, (self.state,))
            for module, name, kwargs in self.operations():
                with self.subTest(entry=name), mock.patch.object(registry, "lookup",
                        return_value=dict(entry, registration_id="0" * 32)):
                    self.refused(module, name, kwargs, "registration binding mismatch")

    def test_revoke_without_readiness_or_lease(self):
        entry = ready(self, *self.args, promote=False)
        state.release_lease(self.state)
        store.create(entry["record_dir"], grant())
        with mock.patch.object(registry, "d5_active", return_value=True):
            result = control.revoke(*self.args, 120, "stop")
        self.assertEqual(result["invalidation"], dict(at=120, kind="revoked", reason="stop"))
        self.assertFalse(self.marker.exists())
        self.assertFalse((Path(entry["record_dir"]) / "authority.drain.lock").exists())

    def test_M6_inactive_issuance_matches_unit_base_capture(self):
        base = "a1847ee2b1ae7a7d32936e4c95cf66eb55c3219d"

        def capture(call, kwargs):
            before = self.fixture.tree()
            with no_effects() as (launches, writes):
                try:
                    outcome = ("return", call(*self.args, **kwargs))
                except Exception as exc:
                    outcome = (type(exc).__name__, str(exc))
            return outcome, launches, writes, before == self.fixture.tree()

        self.assertFalse(registry.d5_active())
        for module, name, kwargs in self.operations():
            path = Path(module.__file__).relative_to(fixtures.ROOT)
            source = subprocess.check_output(["git", "show", base + ":" + str(path)],
                                             cwd=str(fixtures.ROOT))
            previous = ModuleType("_u4_0b1_base_" + module.__name__)
            previous.__file__ = module.__file__
            exec(compile(source, str(path), "exec"), previous.__dict__)
            with self.subTest(entry=name):
                expected = capture(getattr(previous, name), kwargs)
                actual = capture(getattr(module, name), kwargs)
                self.assertEqual(actual, expected)
                self.assertEqual(actual, (("AuthorityError", "D5 is not active" if name != "register"
                                          else "registry: D5 is not active"), [], [], True))


if __name__ == "__main__":
    unittest.main()
