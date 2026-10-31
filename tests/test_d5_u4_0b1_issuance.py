"""Issuance barriers through public entries; D5 remains inactive in production."""
from contextlib import nullcontext
from contextlib import contextmanager, ExitStack
import builtins
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import unittest
from unittest import mock

from tests import test_d5_registry as fixtures
from tests.fixtures.d5.ready import ready, ready_installation
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
        with nullcontext():
            for staged in (False, True):
                if staged:
                    ready(self, *self.args)
                else:
                    self.assertEqual(state.acquire_lease(self.state, bump_epoch=False), (True, None))
                    self.addCleanup(state.release_lease, self.state)
                    ready_installation(self.fixture.d, state_dir=self.state)
                self.marker.unlink()
                for module, name, kwargs in self.operations():
                    with self.subTest(staged=staged, entry=name), mock.patch.object(
                            registry, "lookup", side_effect=AssertionError("lookup")) as lookup:
                        self.refused(module, name, kwargs, "missing or malformed installation state")
                        lookup.assert_not_called()
                self.assertFalse(self.marker.exists())

    def test_grant_without_lease_and_lease_before_readiness(self):
        ready(self, *self.args)
        state.release_lease(self.state)
        with nullcontext(), \
                mock.patch.object(readiness, "_read", side_effect=AssertionError("readiness read")) as read:
            for module, name, kwargs in self.operations():
                with self.subTest(entry=name):
                    self.refused(module, name, kwargs, "caller-held coordinator lease required")
            read.assert_not_called()

    def test_register_checks_lease_before_invalid_plan(self):
        ready(self, *self.args)
        state.release_lease(self.state)
        self.args = self.fixture.repo, "../bad-plan", self.state
        with nullcontext(), \
                mock.patch.object(readiness, "_read") as read, \
                mock.patch.object(registry, "lookup") as lookup:
            self.refused(registry, "register", {}, "caller-held coordinator lease required")
            read.assert_not_called()
            lookup.assert_not_called()

    def test_claim_without_readiness_and_invalid_bindings(self):
        entry = ready(self, *self.args)
        raw = self.marker.read_bytes()
        installation = Path(entry["record_dir"]) / readiness.INSTALLATION
        for bad in (None, b"{}", b"null", b'{"schema_version":1,"schema_version":1}'):
            if bad is None:
                self.marker.unlink()
            else:
                self.marker.write_bytes(bad)
            with self.subTest(marker=bad), nullcontext():
                self.refused(custody, "claim", dict(now=101, mandate_bytes=MANDATE), "D5 readiness:")
        self.marker.write_bytes(raw)
        installation.write_bytes(b"{}")
        with nullcontext():
            for module, name, kwargs in self.operations():
                with self.subTest(entry=name):
                    self.refused(module, name, kwargs, "invalid installation fields")

    def test_register_cannot_provision_readiness(self):
        ready(self, *self.args)
        self.marker.unlink()
        with nullcontext():
            self.refused(registry, "register", {}, "missing or malformed installation state")
        self.assertFalse(self.marker.exists())

    def test_invalid_state_locators_refuse_with_authority_error_before_effects(self):
        for locator in (None, "~missing-d5-user/state"):
            with self.subTest(locator=locator), ExitStack() as patches:
                patches.enter_context(nullcontext())
                if locator is not None:
                    patches.enter_context(mock.patch.object(Path, "expanduser",
                        side_effect=RuntimeError("Could not determine home directory.")))
                lookup = patches.enter_context(mock.patch.object(registry, "lookup"))
                held = patches.enter_context(mock.patch.object(state, "holds_lease"))
                self.args = self.fixture.repo, "p", locator
                for module, name, kwargs in self.operations():
                    with self.subTest(entry=name):
                        self.refused(module, name, kwargs, "state_dir")
                with no_effects() as effects:
                    with self.assertRaisesRegex(registry.AuthorityError, "state_dir"):
                        with readiness.installer_lease(locator):
                            self.fail("invalid state locator admitted")
                    self.assertEqual(effects, ([], []))
                lookup.assert_not_called()
                held.assert_not_called()

    def test_cold_register_refusal_has_no_import_cache_writes(self):
        ready(self, *self.args)
        self.marker.unlink()  # real publication followed by broken-state corruption
        state.release_lease(self.state)
        # A fresh interpreter and cache root exercise the actual first call.
        # Audit hooks see importlib's native opens, which os.open mocks miss.
        code = """
import json, os, sys
sys.dont_write_bytecode = False
sys.pycache_prefix = sys.argv[3]
sys.path.insert(0, sys.argv[1])
import d5_registry as registry
import swarm_state as state
held = sys.argv[4] == 'held'
if held:
    assert state.acquire_lease(sys.argv[2], bump_epoch=False) == (True, None)
writes, launches = [], []
observing = False
def audit(event, args):
    if not observing:
        return
    if event == 'open' and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        writes.append(str(args[0]))
    if event in ('os.mkdir', 'os.rename', 'os.remove', 'os.rmdir'):
        writes.append(event)
    if event.startswith(('subprocess.', 'os.exec', 'os.spawn', 'os.posix_spawn', 'os.fork')) or event == 'os.system':
        launches.append(event)
sys.addaudithook(audit)
try:
    observing = True
    try:
        registry.register('/unused', 'p', sys.argv[2])
        outcome = 'returned'
    except registry.AuthorityError as exc:
        outcome = str(exc)
finally:
    observing = False
    state.release_lease(sys.argv[2])
print(json.dumps(dict(outcome=outcome, writes=writes, launches=launches)))
"""
        for held in (False, True):
            with self.subTest(held=held):
                result = subprocess.run([sys.executable, "-c", code,
                    str(fixtures.ROOT / "skills/hanig-orchestrate/scripts"),
                    str(self.state), str(self.fixture.d / ("cache-" + str(held))),
                    "held" if held else "unheld"], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = json.loads(result.stdout)
                reason = ("missing or malformed installation state" if held
                          else "caller-held coordinator lease required")
                self.assertEqual(observed["outcome"], "D5 readiness: " + reason + "; repair with d5_install promote")
                self.assertEqual(observed["writes"], [])
                self.assertEqual(observed["launches"], [])

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
        self.check_ready_issuance(entry)

    def test_expanded_state_locator_uses_registry_canonical_lease(self):
        entry = ready(self, *self.args)
        self.args = self.fixture.repo, "p", "~/state"
        with mock.patch.dict(os.environ, {"HOME": str(self.fixture.d)}):
            with readiness.installer_lease("~/state") as directory:
                self.assertEqual(directory, self.state)
            self.check_ready_issuance(entry)

    def check_ready_issuance(self, entry):
        mandate = self.fixture.repo / "docs/orchestrator-mandate.md"
        mandate.parent.mkdir()
        mandate.write_bytes(MANDATE)
        anchor = host.ProcessIdentity(42, 9000001)
        with nullcontext(), \
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

    def test_revoke_without_lease_preserves_published_readiness(self):
        entry = ready(self, *self.args)
        state.release_lease(self.state)
        store.create(entry["record_dir"], grant())
        paths = [self.marker, Path(entry["record_dir"]) / "authority.drain.lock"]
        def preserved(path):
            info = path.stat()
            # Classifier reads may update atime. Preserve all observable
            # write metadata, including the filesystem's ctime precision.
            metadata = (info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                        info.st_uid, info.st_gid, info.st_size,
                        info.st_mtime_ns, info.st_ctime_ns)
            return metadata, path.read_bytes()
        before = [preserved(p) for p in paths]
        with mock.patch.object(os, "utime", wraps=os.utime) as timestamps:
            result = control.revoke(*self.args, 120, "stop")
        timestamps.assert_not_called()
        self.assertEqual(result["invalidation"], dict(at=120, kind="revoked", reason="stop"))
        self.assertEqual([preserved(p) for p in paths], before)
        self.marker.unlink()
        with self.assertRaisesRegex(registry.AuthorityError, "d5_install"):
            control.revoke(*self.args, 121, "must refuse broken installation")

    def test_readiness_preservation_allows_only_read_access_time(self):
        stat, read = Path.stat, Path.read_bytes
        access = [1]
        marker = self.marker

        class AccessStat(tuple):
            def __new__(cls, original):
                fields = list(original)
                fields[7] = access[0]
                value = tuple.__new__(cls, fields)
                value.original = original
                return value

            def __getattr__(self, name):
                if name == 'st_atime':
                    return self[7]
                if name == 'st_atime_ns':
                    return self[7] * 1000000000
                return getattr(self.original, name)

        def observed(path, *args, **kwargs):
            value = stat(path, *args, **kwargs)
            return AccessStat(value) if path == marker else value

        def accessed(path, *args, **kwargs):
            value = read(path, *args, **kwargs)
            if path == marker:
                access[0] += 1
            return value

        # Model strictatime only; all write-sensitive metadata and bytes remain
        # real. This is deterministic on hosts whose mount defers access times.
        with mock.patch.object(Path, 'stat', observed), \
                mock.patch.object(Path, 'read_bytes', accessed):
            self.test_revoke_without_lease_preserves_published_readiness()

    def test_readiness_preservation_still_detects_metadata_writes(self):
        revoke, stat = control.revoke, Path.stat
        change = [0]
        marker = self.marker

        class WriteStat:
            def __init__(self, value):
                self.value = value
            def __getattr__(self, name):
                if name == 'st_ctime_ns':
                    return self.value.st_ctime_ns + change[0]
                return getattr(self.value, name)

        def observed(path, *args, **kwargs):
            value = stat(path, *args, **kwargs)
            return WriteStat(value) if path == marker else value

        def changed(*args, **kwargs):
            result = revoke(*args, **kwargs)
            prior = self.marker.stat()
            os.utime(self.marker, ns=(prior.st_atime_ns + 1000000000, prior.st_mtime_ns))
            # Inject an observable clock transition, independent of mount clock
            # precision. The real atime write alone may share publication's tick.
            change[0] += 1000000000
            return result

        with mock.patch.object(control, 'revoke', changed), \
                mock.patch.object(Path, 'stat', observed):
            with self.assertRaises(AssertionError):
                self.test_revoke_without_lease_preserves_published_readiness()

    def test_readiness_preservation_detects_atime_write_with_coarse_ctime(self):
        original_stat, original_revoke = Path.stat, control.revoke
        marker = self.marker
        writes = []

        class CoarseStat:
            def __init__(self, value):
                self.value = value
            def __getattr__(self, name):
                if name == 'st_ctime_ns':
                    return 1000000000
                if name == 'st_ctime':
                    return 1
                return getattr(self.value, name)

        def coarse(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            return CoarseStat(value) if path == marker else value

        def changed(*args, **kwargs):
            value = original_revoke(*args, **kwargs)
            prior = original_stat(marker)
            os.utime(marker, ns=(prior.st_atime_ns + 1000000000, prior.st_mtime_ns))
            after = original_stat(marker)
            writes.append(after.st_atime_ns != prior.st_atime_ns)
            self.assertEqual(after.st_mtime_ns, prior.st_mtime_ns)
            return value

        # Actual atime mutation, with only ctime precision modeled. Do not
        # fabricate a ctime transition that the filesystem may not expose.
        with mock.patch.object(Path, 'stat', coarse), \
                mock.patch.object(control, 'revoke', changed):
            with self.assertRaises(AssertionError):
                self.test_revoke_without_lease_preserves_published_readiness()
        self.assertEqual(writes, [True])

    def test_metadata_write_probe_handles_coarse_timestamps(self):
        original = Path.stat
        class CoarseStat:
            def __init__(self, value):
                self.value = value
            def __getattr__(self, name):
                if name == 'st_ctime_ns':
                    return 1000000000
                if name == 'st_ctime':
                    return 1
                return getattr(self.value, name)
        def coarse(path, *args, **kwargs):
            return CoarseStat(original(path, *args, **kwargs))
        # Safe equivalent of publication and mutation in one coarse clock tick.
        # No mount, host configuration or production timestamp is changed.
        with mock.patch.object(Path, 'stat', coarse):
            self.test_readiness_preservation_still_detects_metadata_writes()

    def test_M6_inactive_capture_without_git_history(self):
        # A depth-one checkout has the sources, but no historical Git objects.
        with mock.patch.object(subprocess, "check_output", side_effect=
                subprocess.CalledProcessError(128, ["git", "show"])) as history:
            self.test_M6_inactive_issuance_matches_unit_base_capture()
            history.assert_not_called()

    def test_M6_inactive_issuance_matches_unit_base_capture(self):
        baseline = json.loads((fixtures.ROOT / "tests/fixtures/d5/"
                               "u4_0b1_inactive_base.json").read_text())

        def capture(call, kwargs):
            before = self.fixture.tree()
            with no_effects() as (launches, writes):
                try:
                    outcome = ("return", call(*self.args, **kwargs))
                except Exception as exc:
                    outcome = (type(exc).__name__, str(exc))
            return outcome, launches, writes, before == self.fixture.tree()

        self.assertFalse(registry.d5_active(self.state))
        for module, name, kwargs in self.operations():
            with self.subTest(entry=name):
                outcomes = json.loads((fixtures.ROOT / "tests/fixtures/d5/historical-inactive-outcomes.json").read_text())
                expected = (tuple(outcomes[baseline["base"]][module.__name__ + "." + name]), [], [], True)
                actual = capture(getattr(module, name), kwargs)
                self.assertEqual(actual, expected)
                self.assertEqual(actual, (("AuthorityError", "D5 is not active" if name != "register"
                                          else "registry: D5 is not active"), [], [], True))


if __name__ == "__main__":
    unittest.main()
