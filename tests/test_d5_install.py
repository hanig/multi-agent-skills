"""R3 consumers: real stores/flocks; events and failed NB probes order waits."""
from contextlib import nullcontext
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_install as install
import d5_authority_store as store
import d5_host as host
import d5_locks as locks
import d5_protocol_locks as protocol
from tests.test_d5_authority import grant, MANDATE
from tests.fixtures.d5.ready import ready, ready_installation, InstallationWitnesses
from tests.test_d5_locks import isolate_pool

ANCHOR = host.ProcessIdentity(42, 9000001)
OTHER = host.ProcessIdentity(43, 9000002)
KINDS = ("initial", "replace", "claim")


class TestInstall(unittest.TestCase):
    def setUp(self):
        isolate_pool(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mandate = self.root / "mandate.md"
        self.mandate.write_bytes(MANDATE)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.anchor = self.stack.enter_context(mock.patch.object(
            host, "session_anchor", return_value=("native", ANCHOR)))
        self.live = self.stack.enter_context(mock.patch.object(
            host, "liveness", return_value="unknown"))
        self.clock = self.stack.enter_context(mock.patch.object(
            install.time, "time", return_value=102))

    def directory(self, name, kind="initial"):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        if kind == "replace":
            repo, state = directory / "repo", directory / "state"
            state.mkdir()
            subprocess.run(["git", "init", str(repo)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            with mock.patch.object(install.registry.CP, "_external_base",
                                   return_value=directory / "external"):
                directory = Path(ready(self, repo, "p", state)["record_dir"])
        if kind != "replace":
            ready_installation(self.root, state_dir=self.root / (name + "-state"), record_dir=directory)
        if kind != "initial":
            store.create(directory, grant())
        return directory

    def call(self, kind, directory, record=None):
        if kind == "initial":
            return install.install_initial(directory, record or grant())
        if kind == "replace":
            return install.replace_authority(directory, record or grant("b" * 32))
        return install.claim_custody(directory, mandate_path=self.mandate)

    @contextmanager
    def waiter(self, operation, directory, hold=True):
        """Observe the installer's real failed nonblocking DRAIN acquisition.

        The hook then performs its original blocking acquisition. No timeout
        is evidence of exclusion. The main thread owns/relinquishes the shared
        holder, or the caller supplies a child holder when hold=False.
        """
        ready = threading.Event()
        observations, results, errors = [], [], []
        real_flock = locks.fcntl.flock
        worker = None

        def observed(fd, mode):
            if threading.current_thread() is worker:
                info = os.fstat(fd)
                target = (directory / "authority.drain.lock").stat()
                if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                    held = [(rank, shared) for _, rank, shared, _ in locks._held()]
                    try:
                        real_flock(fd, mode | locks.fcntl.LOCK_NB)
                    except BlockingIOError:
                        observations.append((mode, held, "blocked"))
                    else:
                        observations.append((mode, held, "acquired"))
                        ready.set()
                        return
                    ready.set()
            return real_flock(fd, mode)

        def run():
            try:
                results.append(operation())
            except BaseException as exc:
                errors.append(exc)
            finally:
                ready.set()

        with mock.patch.object(locks.fcntl, "flock", side_effect=observed):
            try:
                with ExitStack() as holding:
                    if hold:
                        record = holding.enter_context(protocol.pin(directory))
                        holding.enter_context(record.drain())
                    worker = threading.Thread(target=run, daemon=True)
                    worker.start()
                    self.assertTrue(ready.wait(10), "installer watchdog")
                    self.assertEqual(observations, [(locks.fcntl.LOCK_EX, [], "blocked")],
                                     (observations, errors, results))
                    self.assertEqual(results, [])
                    self.assertEqual(errors, [])
                    yield results, errors
            finally:
                if worker is not None:
                    worker.join(10)
                    self.assertFalse(worker.is_alive(), "installer watchdog")

    def test_installation_waits_for_shared_holders(self):
        # R3-OMIT-EXCLUSIVE: the consumer must see EX DRAIN, START, then STATE.
        for kind in KINDS:
            with self.subTest(kind=kind):
                directory = self.directory(kind, kind)
                writes = []
                original = store._write

                def writing(path, raw):
                    held = [(rank, shared) for _, rank, shared, _ in locks._held()]
                    self.assertEqual(held, [(0, False), (1, False)])
                    # Independent shared probe fails specifically for EX DRAIN.
                    fd = os.open(directory / "authority.drain.lock", os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError):
                            locks.fcntl.flock(fd, locks.fcntl.LOCK_SH | locks.fcntl.LOCK_NB)
                    finally:
                        os.close(fd)
                    writes.append(path)
                    return original(path, raw)

                with mock.patch.object(store, "_write", side_effect=writing):
                    with self.waiter(lambda: self.call(kind, directory), directory) as (results, errors):
                        self.assertEqual(writes, [])
                    self.assertEqual(errors, [])
                    self.assertEqual(len(results), 1)
                self.assertTrue(writes)
                self.assertEqual(store.read(directory), results[0])

    def test_revoke_progresses_while_install_waits(self):
        # R3-START-BEFORE-DRAIN. This is the specified R2 control lock sequence;
        # the R2 wrapper is a separate unit and is not imported or implemented.
        for kind in ("replace", "claim"):
            with self.subTest(kind=kind):
                directory = self.directory(kind, kind)
                with self.waiter(lambda: self.call(kind, directory), directory) as (results, errors):
                    with protocol.pin(directory) as record, record.start():
                        revoked = store.revoke(directory, 103, "owner stopped")
                    self.assertEqual(revoked["invalidation"]["kind"], "revoked")
                self.assertEqual(results, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], store.AuthorityError)
                self.assertRegex(str(errors[0]), "invalidated")
                self.assertEqual(store.read(directory), revoked)

    def test_installation_rechecks_after_wait(self):
        # R3-CACHED-PREFLIGHT: no host observation may authorize from before wait.
        for kind in KINDS:
            with self.subTest(kind=kind):
                directory = self.directory(kind, kind)
                self.anchor.reset_mock()
                self.anchor.return_value = ("native", ANCHOR)
                with self.waiter(lambda: self.call(kind, directory), directory) as (results, errors):
                    self.anchor.assert_not_called()
                    self.anchor.return_value = host.Unknown("lost ancestry")
                self.assertEqual(results, [])
                self.assertEqual(len(errors), 1)
                self.assertEqual(str(errors[0]), "D5 anchor is not verifiable")
                self.anchor.assert_called_once_with()
                path = directory / store.RECORD_NAME
                self.assertEqual(store.read(directory), grant()) if path.exists() else self.assertEqual(kind, "initial")

    def test_claim_reloads_mandate_time_and_native_identity(self):
        directory = self.directory("claim", "claim")
        with self.waiter(lambda: self.call("claim", directory), directory) as (results, errors):
            self.anchor.return_value = ("successor", OTHER)
            self.clock.return_value = 120
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["custody"]["session_id"], "successor")
        self.assertEqual(results[0]["custody"]["anchor_pid"], OTHER.pid)
        self.assertEqual(results[0]["custody"]["claimed_at"], 120)
        for condition in ("mandate", "expiry"):
            with self.subTest(condition=condition):
                directory = self.directory(condition, "claim")
                self.mandate.write_bytes(MANDATE)
                self.clock.return_value = 102
                with self.waiter(lambda: self.call("claim", directory), directory) as (results, errors):
                    if condition == "mandate":
                        self.mandate.write_bytes(b"updated policy")
                    else:
                        self.clock.return_value = grant()["expires_at"]
                self.assertEqual(results, [])
                self.assertEqual(len(errors), 1)
                self.assertRegex(str(errors[0]), "mandate_changed" if condition == "mandate" else "expired")
                record = store.read(directory)
                self.assertIsNone(record["custody"])
                if condition == "mandate":
                    self.assertEqual(record["invalidation"]["kind"], "mandate_changed")

    def test_activation_checked_before_work_and_after_wait(self):
        # R3-BYPASS-INACTIVE and R3-CACHED-ACTIVATION.
        for kind in KINDS:
            with self.subTest(kind=kind):
                directory = self.directory(kind, kind)
                witnesses = InstallationWitnesses(install.readiness.installation(directory))
                self.addCleanup(witnesses.close)
                witnesses.present = False
                with mock.patch.object(protocol, "pin", side_effect=AssertionError("inactive pin")):
                    with self.assertRaisesRegex(store.AuthorityError, "^D5 is inactive$"):
                        self.call(kind, directory)
                witnesses.present = True
                with self.waiter(lambda: self.call(kind, directory), directory) as (results, errors):
                    witnesses.present = False
                self.assertEqual(results, [])
                self.assertEqual([str(e) for e in errors], ["D5 is inactive"])

    def test_unknown_owner_does_not_become_dead(self):
        for released in (False, True):
            directory = self.directory(str(released), "claim")
            before = store.claim(directory, 101, MANDATE, "old", OTHER, host.liveness)
            if released:
                before = store.relinquish(directory, 102, "old", OTHER)
            self.clock.return_value = grant()["expires_at"] - 1
            for value in ("alive", "unknown", None, True, "DEAD", OSError("inspection")):
                with self.subTest(released=released, value=value):
                    self.live.side_effect = value if isinstance(value, Exception) else None
                    self.live.return_value = value
                    with self.assertRaisesRegex(store.AuthorityError, "successor requires dead holder"):
                        self.call("claim", directory)
                    self.assertEqual(store.read(directory), before)
            self.live.side_effect = None
            self.live.return_value = "dead"
            updated = self.call("claim", directory)
            self.assertEqual(updated["custody"]["generation"], 2)
            self.assertEqual(updated["confirmation"], before["confirmation"])
            self.live.assert_called_with(OTHER)

    def test_store_replacement_rules_retained(self):
        directory = self.directory("replace", "replace")
        held = store.claim(directory, 101, MANDATE, "old", OTHER, host.liveness)
        for change in ("expiry", "scope", "custody", "grant_id"):
            new = grant("b" * 32)
            if change == "expiry":
                new["issued_at"] += 1
                new["expires_at"] += 1
            elif change == "scope":
                new["scope"]["actions"].append("expand")
            elif change == "custody":
                new["custody"] = dict(held["custody"], session_id="forged")
            else:
                new["grant_id"] = held["grant_id"]
            with self.subTest(change=change), self.assertRaises(store.AuthorityError):
                self.call("replace", directory, new)
            self.assertEqual(store.read(directory), held)
        new = grant("b" * 32)
        new["confirmation"]["owner_response"] = "fresh consent"
        new["scope"]["actions"].append("expand")
        result = self.call("replace", directory, new)
        self.assertEqual(result["custody"], held["custody"])
        self.assertEqual(result["scope"], new["scope"])
        self.assertIsNone(new["custody"])

    def test_installation_upgrade_refused(self):
        for kind in KINDS:
            directory = self.directory(kind, kind)
            with self.subTest(kind=kind), protocol.pin(directory) as record, record.drain():
                with mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("upgrade attempted")):
                    with self.assertRaisesRegex(protocol.LockError, "^D5 drain upgrade is unsupported$"):
                        self.call(kind, directory)

    def test_replacement_preserves_lock_identity(self):
        directory = self.directory("replace", "replace")
        with protocol.pin(directory) as record, record.drain(shared=False), record.start():
            pass
        # Keep old descriptors open so unlink/recreate cannot reuse these inodes.
        with ExitStack() as opened:
            identities = {}
            for name in locks._NAMES.values():
                fd = os.open(directory / name, os.O_RDWR)
                opened.callback(os.close, fd)
                info = os.fstat(fd)
                identities[name] = (info.st_dev, info.st_ino)
            root_info = directory.stat()
            self.call("replace", directory)
            self.assertEqual((directory.stat().st_dev, directory.stat().st_ino),
                             (root_info.st_dev, root_info.st_ino))
            for name, identity in identities.items():
                info = (directory / name).stat()
                self.assertEqual((info.st_dev, info.st_ino), identity)
        archived = directory / "superseded" / ("a" * 32 + ".json")
        self.assertEqual(store.decode(archived.read_bytes()), grant())

    def test_M5_child_holds_drain_after_ancestor_exit_through_accounting(self):
        directory = self.directory("M5", "replace")
        peer = ROOT / "tests/fixtures/d5/install_peer.py"
        ancestor = subprocess.Popen([sys.executable, str(peer), "ancestor", str(directory)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, close_fds=True)

        def cleanup():
            if ancestor.poll() is None:
                ancestor.kill()
            ancestor.communicate(timeout=10)  # EOF also releases the child's read.

        self.addCleanup(cleanup)

        def line():
            ready, _, _ = select.select([ancestor.stdout], [], [], 10)
            self.assertTrue(ready, "effect child watchdog")
            return ancestor.stdout.readline()

        self.assertEqual(line(), b"submitted\n")
        ancestor.kill()
        ancestor.wait(timeout=10)
        self.assertLess(ancestor.returncode, 0)
        original = store.replace

        def replacing(*args):
            rows = list((directory / "authority.effects").glob("*.json"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0].read_bytes())["state"], "completed")
            return original(*args)

        with mock.patch.object(store, "replace", side_effect=replacing):
            # Failed real EX probe proves survival beyond ancestor termination.
            try:
                with self.waiter(lambda: self.call("replace", directory), directory, hold=False) as (results, errors):
                    ancestor.stdin.write(b"release\n")
                    ancestor.stdin.flush()
                    self.assertEqual(line(), b"completed and released\n")
            finally:
                ancestor.stdin.close()
                ancestor.stdin = None
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(store.read(directory)["grant_id"], "b" * 32)

    def test_initial_uses_provisioned_directory_and_refuses_duplicate(self):
        # Consumer seam: the real registration sequence provisions D first.
        repo, state = self.root / "repo", self.root / "state"
        state.mkdir(mode=0o700)
        subprocess.run(["git", "init", str(repo)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        with mock.patch.object(install.registry.CP, "_external_base", return_value=self.root / "external"):
            entry = install.stage(repo, "p", state)
        directory = Path(entry["record_dir"])
        self.assertTrue(directory.is_dir())
        self.assertFalse((directory / store.RECORD_NAME).exists())
        self.assertEqual(self.call("initial", directory), grant())
        with self.assertRaisesRegex(store.AuthorityError, "already exists"):
            self.call("initial", directory)
        self.assertEqual(store.read(directory), grant())


    def test_relative_directory_remains_bound_after_wait(self):
        # R3-REBOUND-DIRECTORY: changing cwd cannot redirect a pinned install.
        for kind in KINDS:
            with self.subTest(kind=kind):
                first, second = self.root / (kind + "-a"), self.root / (kind + "-b")
                first.mkdir()
                second.mkdir()
                directory = self.directory(first.name + "/record", kind)
                other = self.directory(second.name + "/record", kind)
                prior = Path.cwd()
                try:
                    os.chdir(directory.parent)
                    relative = Path(directory.name)
                    # The peer fixture uses the same relative spelling.
                    if other.name != directory.name:
                        (other.parent / directory.name).symlink_to(other, target_is_directory=True)
                    with self.waiter(lambda: self.call(kind, relative), directory) as (results, errors):
                        os.chdir(other.parent)
                finally:
                    os.chdir(prior)
                self.assertEqual(errors, [])
                self.assertEqual(len(results), 1)
                self.assertTrue((directory / store.RECORD_NAME).exists())
                self.assertEqual(store.read(directory), results[0])
                if kind == "initial":
                    self.assertFalse((other / store.RECORD_NAME).exists())
                else:
                    self.assertEqual(store.read(other), grant())

    def test_relative_mandate_remains_bound_after_wait(self):
        # R3-REBOUND-MANDATE: reread the intended file, not a new cwd's file.
        directory = self.directory("claim-relative", "claim")
        first, second = self.root / "mandate-a", self.root / "mandate-b"
        first.mkdir()
        second.mkdir()
        (first / "mandate.md").write_bytes(MANDATE)
        (second / "mandate.md").write_bytes(b"unrelated policy")
        prior = Path.cwd()
        try:
            os.chdir(first)
            with self.waiter(lambda: install.claim_custody(directory, mandate_path=b"mandate.md"), directory) as (results, errors):
                os.chdir(second)
        finally:
            os.chdir(prior)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["custody"]["session_id"], "native")
        self.assertIsNone(store.read(directory)["invalidation"])

    def test_dotdot_locators_refused_before_work(self):
        # R3-ALLOW-DOTDOT: neither possible resolution may acquire locks.
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "child").mkdir()
        intended = self.directory("nested/record")
        other = self.directory("record")
        (self.root / "link").symlink_to(nested / "child", target_is_directory=True)
        prior = Path.cwd()
        try:
            os.chdir(self.root)
            for kind in KINDS:
                for convert in (str, os.fsencode, Path):
                    for prefix in ("", str(self.root) + "/"):
                        with self.subTest(kind=kind, convert=convert, prefix=prefix):
                            directory = convert(prefix + "link/../record")
                            with ExitStack() as guards:
                                for owner, name in [(protocol, "pin"), (os, "open"), (os, "getcwdb")] + [
                                        (store, n) for n in ("create", "replace", "claim", "read_bytes")]:
                                    guards.enter_context(mock.patch.object(
                                        owner, name, side_effect=AssertionError("work before refusal: " + name)))
                                with self.assertRaisesRegex(store.AuthorityError, r"^D5 locator contains \.\.$"):
                                    self.call(kind, directory)
                            for candidate in (intended, other):
                                self.assertEqual(list(candidate.iterdir()), [])
        finally:
            os.chdir(prior)

    def test_mandate_dotdot_is_read_without_normalization_under_locks(self):
        # R3-REFUSE-MANDATE-DOTDOT: trusted read locators remain supported.
        directory = self.directory("claim", "claim")
        nested = self.root / "nested"
        (nested / "child").mkdir(parents=True)
        (nested / "mandate.md").write_bytes(MANDATE)
        self.mandate.write_bytes(b"wrong lexical target")
        (self.root / "link").symlink_to(nested / "child", target_is_directory=True)
        read = store.read_bytes

        def reading(path):
            self.assertEqual([(rank, shared) for _, rank, shared, _ in locks._held()],
                             [(0, False), (1, False)])
            return read(path)

        prior = Path.cwd()
        try:
            os.chdir(self.root)
            with mock.patch.object(store, "read_bytes", side_effect=reading) as observed:
                result = install.claim_custody(directory, mandate_path="link/../mandate.md")
            self.assertEqual(observed.call_args_list[0][0][0],
                             str(Path.cwd()) + "/link/../mandate.md")
        finally:
            os.chdir(prior)
        self.assertEqual(result["custody"]["session_id"], "native")
        self.assertEqual(store.read(directory), result)

    def test_symlink_component_without_dotdot(self):
        nested = self.root / "nested..safe"
        nested.mkdir()
        intended = self.directory("nested..safe/record")
        (nested / "mandate.md").write_bytes(MANDATE)
        (self.root / "link").symlink_to(nested, target_is_directory=True)
        locator = str(self.root) + "/link//./record"
        with mock.patch.object(store, "create", wraps=store.create) as create:
            self.call("initial", locator)
        self.assertEqual(create.call_args[0][0], locator)
        self.assertEqual(store.read(intended), grant())
        self.mandate = os.fsencode(str(self.root) + "/link//./mandate.md")
        with mock.patch.object(store, "read_bytes", wraps=store.read_bytes) as read:
            self.call("claim", locator)
        self.assertEqual(read.call_args_list[0][0][0], os.fsdecode(self.mandate))
        self.assertEqual(store.read(intended)["custody"]["session_id"], "native")


if __name__ == "__main__":
    unittest.main()
