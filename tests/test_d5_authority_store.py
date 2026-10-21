"""Real temporary grant stores: schema, terminal history and durable transitions."""
import copy
import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_authority as records
import d5_authority_store as A

from tests.test_d5_authority import MANDATE, grant


class TestD5AuthorityStore(unittest.TestCase):
    eq = unittest.TestCase.assertEqual

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.d = Path(self.temp.name)
        self.path = self.d / A.RECORD_NAME

    def refuses(self, field, op, *args):
        with self.assertRaisesRegex(A.AuthorityError, field):
            op(*args)

    def create(self):
        A.create(self.d, grant())

    def stored(self, expected):
        self.eq(A.read(self.d), expected)

    def successor(self, old=None, fresh=False):
        new = copy.deepcopy(old if old is not None else grant())
        new["grant_id"] = "b" * 32
        if fresh:
            new["confirmation"]["owner_response"] += " fresh"
            new["invalidation"] = None
        return new

    @contextmanager
    def filesystem_refusal(self, field="record"):
        with self.assertRaisesRegex(A.AuthorityError, field) as caught:
            yield
        raise caught.exception.__cause__

    def test_exact_roundtrip(self):
        r = grant()
        self.eq(A.create(self.d, r), r)
        loaded = A.read(self.d)
        self.eq(loaded, r)
        for key in r["confirmation"]:
            self.eq(loaded["confirmation"][key].encode("utf-8"),
                            r["confirmation"][key].encode("utf-8"))
        loaded["scope"]["plans"].append("local")
        self.stored(r)
        self.refuses("record", A.create, self.d, grant())

    def test_digest_and_terminal_change(self):
        for content in (MANDATE.replace(b"grant", b"Grant"),
                        MANDATE.replace(b"\n", b"\r\n"), MANDATE + b" ", b"\xff"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as d:
                self.eq(records.mandate_digest(content), hashlib.sha256(content).hexdigest())
                self.assertNotEqual(records.mandate_digest(content), records.mandate_digest(MANDATE))
                A.create(d, grant())
                self.eq(A.check(d, 102, content), "mandate_changed")
                old = A.read(d)
                self.eq(old["invalidation"]["kind"], "mandate_changed")
                self.eq(old["invalidation"]["at"], 102)
                self.eq(A.check(d, 103, MANDATE), "invalidated")
                self.eq(A.check(d, 104, content), "invalidated")
                self.eq(A.revoke(d, 105, "later"), old)
                self.eq(A.read(d), old)

    def test_revoke_and_expired_mismatch(self):
        self.create()
        old = A.revoke(self.d, 102, "  owner said stop\n")
        self.eq(old["invalidation"],
                        {"at": 102, "kind": "revoked", "reason": "  owner said stop\n"})
        self.eq(A.revoke(self.d, 103, "again"), old)
        self.eq(A.check(self.d, 86501, b"changed"), "invalidated")
        with tempfile.TemporaryDirectory() as d:
            A.create(d, grant())
            self.eq(A.check(d, 86501, b"changed"), "mandate_changed")

    def test_argument_refusals(self):
        self.create()
        for now in (True, 1.0, "101", None, 0, -1, 2**63, 1 << 16384):
            self.refuses("now", A.check, self.d, now, MANDATE)
            self.refuses("now", A.revoke, self.d, now, "stop")
        for content in ("text", None, bytearray(MANDATE)):
            self.refuses("mandate_bytes", A.check, self.d, 101, content)
        for reason in ("", None, 1):
            self.refuses("reason", A.revoke, self.d, 101, reason)
        self.stored(grant())

    def test_expansion_needs_consent(self):
        self.create()
        new = self.successor()
        new["issued_at"] += 1
        new["expires_at"] += 1
        self.refuses("expires_at", A.replace, self.d, new)
        for key in grant()["scope"]:
            new = self.successor()
            new["scope"][key].append("additional")
            self.refuses("scope." + key, A.replace, self.d, new)
        self.stored(grant())
        new = self.successor(fresh=True)
        new["issued_at"] += 1
        new["expires_at"] += 1
        for values in new["scope"].values():
            values.append("additional")
        self.eq(A.replace(self.d, new), new)

    def test_same_id_refused(self):
        self.create()
        self.refuses("grant_id", A.replace, self.d, grant())
        A.revoke(self.d, 101, "stop")
        new = grant()
        new["confirmation"]["owner_response"] += " new"
        self.refuses("grant_id", A.replace, self.d, new)
        self.assertIsNotNone(A.read(self.d)["invalidation"])

    def test_terminal_archive(self):
        self.create()
        A.check(self.d, 101, b"changed")
        old = A.read(self.d)

        raw = json.dumps(old, indent=4).encode() + b" \n"
        self.path.write_bytes(raw)
        self.refuses("confirmation", A.replace, self.d, self.successor(old))
        new = self.successor(old, fresh=True)
        self.eq(A.replace(self.d, new), new)
        archive = self.d / "superseded" / (old["grant_id"] + ".json")
        self.eq(archive.read_bytes(), raw)
        self.eq(json.loads(archive.read_bytes())["invalidation"], old["invalidation"])
        self.eq(A.check(self.d, 102, MANDATE), "valid")
        recycled = grant()
        recycled["confirmation"]["owner_response"] += " new again"
        self.refuses("grant_id", A.replace, self.d, recycled)
        self.path.unlink()
        self.refuses("grant_id", A.create, self.d, recycled)
        self.eq(archive.read_bytes(), raw)

    def test_fresh_consent_clear(self):
        self.create()
        new = self.successor(fresh=True)
        new["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
        self.refuses("invalidation", A.replace, self.d, new)

    def test_archive_conflict(self):
        self.create()
        archive = self.d / "superseded"
        archive.mkdir()
        prior = archive / ("a" * 32 + ".json")
        prior.write_bytes(b"different")
        self.refuses("superseded", A.replace, self.d, self.successor())
        self.stored(grant())
        self.eq(prior.read_bytes(), b"different")

    def test_interrupted_write(self):
        self.create()
        old_raw = self.path.read_bytes()
        for op in (lambda: A.revoke(self.d, 101, "stop"),
                        lambda: A.check(self.d, 101, b"changed")):
            with mock.patch.object(A.os, "replace", side_effect=OSError("injected before replace")):
                with self.assertRaisesRegex(OSError, "injected"), self.filesystem_refusal():
                    op()
            self.eq(self.path.read_bytes(), old_raw)
            self.stored(grant())
            self.eq(list(self.d.glob(".authority-*")), [])

    def test_replacement_retry(self):
        self.create()
        old_raw = self.path.read_bytes()
        replace = os.replace
        def fail_head(src, dst):
            if Path(dst) == self.path:
                raise OSError("head interrupted")
            return replace(src, dst)
        with mock.patch.object(A.os, "replace", side_effect=OSError("archive interrupted")):
            with self.assertRaisesRegex(OSError, "archive interrupted"), self.filesystem_refusal():
                A.replace(self.d, self.successor())
        self.stored(grant())
        with mock.patch.object(A.os, "replace", side_effect=fail_head):
            with self.assertRaisesRegex(OSError, "head interrupted"), self.filesystem_refusal():
                A.replace(self.d, self.successor())
        self.stored(grant())
        archive = self.d / "superseded" / ("a" * 32 + ".json")
        self.eq(archive.read_bytes(), old_raw)
        self.eq(A.replace(self.d, self.successor()), self.successor())
        self.eq(archive.read_bytes(), old_raw)

    def test_archive_fsync_retry(self):
        self.create()
        sync = A._sync_directory
        def fail_archive(path):
            if path.name == "superseded":
                raise OSError("archive fsync failed")
            return sync(path)
        with mock.patch.object(A, "_sync_directory", side_effect=fail_archive):
            with self.assertRaisesRegex(OSError, "archive fsync failed"), self.filesystem_refusal():
                A.replace(self.d, self.successor())
        self.stored(grant())
        with mock.patch.object(A.os, "replace", wraps=A.os.replace) as install:
            A.replace(self.d, self.successor())
        self.eq([Path(call.args[1]).parent for call in install.call_args_list],
                        [self.d / "superseded", self.d])

    def test_corrupt_reads_refuse(self):
        r = grant()
        r["session_cap"] = True
        duplicate = json.dumps(grant()).replace('"session_cap": 1',
                                                '"session_cap": 2, "session_cap": 1')
        for raw in (b'{"grant_id":', b"", b"not-json", b"\xff", b"[]",
                    json.dumps(r).encode(), duplicate.encode()):
            self.path.write_bytes(raw)
            for op in (lambda: A.read(self.d),
                            lambda: A.check(self.d, 101, MANDATE),
                            lambda: A.revoke(self.d, 101, "stop"),
                            lambda: A.replace(self.d, self.successor())):
                with self.assertRaises(A.AuthorityError):
                    op()
            self.eq(self.path.read_bytes(), raw)

    def test_target_directory_is_input(self):
        missing = self.d / "not-created"
        for op in (lambda: A.create(missing, grant()), lambda: A.read(missing),
                        lambda: A.check(missing, 101, MANDATE),
                        lambda: A.revoke(missing, 101, "stop"),
                        lambda: A.replace(missing, self.successor())):
            with self.assertRaises(FileNotFoundError), self.filesystem_refusal():
                op()
            self.assertFalse(missing.exists())

    def test_durable_write_protocol(self):
        events = []
        fsync, replace = os.fsync, os.replace
        def sync(fd):
            events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return fsync(fd)
        def install(src, dst):
            src, dst = Path(src), Path(dst)
            self.eq(src.parent, dst.parent)
            self.assertNotEqual(src, dst)
            with (self.d / A.LOCK_NAME).open("rb") as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.eq(events[-1], "file")
            events.append("replace")
            return replace(str(src), str(dst))
        with mock.patch.object(A.os, "fsync", side_effect=sync), \
                mock.patch.object(A.os, "replace", side_effect=install):
            self.create()
            A.check(self.d, 101, b"changed")
            A.replace(self.d, self.successor(fresh=True))
            A.revoke(self.d, 102, "stop")
        self.eq(events.count("replace"), 5)
        for index, event in enumerate(events):
            if event == "replace":
                self.eq(events[index - 1:index + 2], ["file", "replace", "directory"])

    def test_reused_consent_invalidated(self):
        self.create()
        new = self.successor()
        new["invalidation"] = {"at": 101, "kind": "revoked", "reason": "stop"}
        self.eq(A.replace(self.d, new), new)
        self.eq(A.check(self.d, 102, MANDATE), "invalidated")
        self.stored(new)

    def test_retired_id_lookup_error(self):
        self.create()
        A.revoke(self.d, 101, "stop")
        A.replace(self.d, self.successor(fresh=True))
        prior = self.d / "superseded" / ("a" * 32 + ".json")
        raw = prior.read_bytes()
        self.path.unlink()
        lstat = os.lstat
        def denied(path, *args, **kwargs):
            if Path(path) == prior:
                raise PermissionError("archive lookup denied")
            return lstat(path, *args, **kwargs)
        with mock.patch.object(A.os, "lstat", side_effect=denied):
            with self.assertRaises(PermissionError), self.filesystem_refusal("grant_id"):
                A.create(self.d, grant())
        self.assertFalse(self.path.exists())
        self.eq(prior.read_bytes(), raw)


    def test_present_classification(self):
        for number in (errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EIO, errno.ELOOP):
            with self.subTest(errno=number):
                fault = OSError(number, "lookup")
                with mock.patch.object(A.os, "lstat", side_effect=fault):
                    if number == errno.ENOENT:
                        self.assertFalse(A._present(self.path, "probe"))
                    else:
                        with self.assertRaisesRegex(A.AuthorityError, "probe") as caught:
                            A._present(self.path, "probe")
                        self.assertIs(caught.exception.__cause__, fault)
        self.path.symlink_to(self.d / "absent")
        self.assertTrue(A._present(self.path, "probe"))
        self.assertTrue(A._present(self.d, "probe"))

    def test_lookup_routing(self):
        sites = ("record", "grant_id", "archive_directory", "superseded", "temporary")
        for site in sites:
            for number in (errno.ENOENT, errno.EACCES):
                with self.subTest(site=site, errno=number), tempfile.TemporaryDirectory() as d:
                    directory = Path(d)
                    if site in ("archive_directory", "superseded"):
                        A.create(d, grant())
                    target = directory / "superseded" / ("a" * 32 + ".json")
                    if site == "record":
                        target = directory / A.RECORD_NAME
                    elif site == "archive_directory":
                        target = directory / "superseded"
                    replacing = site in ("archive_directory", "superseded")
                    operation = A.replace if replacing else A.create
                    expected = self.successor() if replacing else grant()
                    real = os.lstat
                    fault = OSError(number, "lookup")
                    def lookup(path, *args, **kwargs):
                        match = (Path(path).name.startswith(".authority-")
                                 if site == "temporary" else Path(path) == target)
                        if match:
                            raise fault
                        return real(path, *args, **kwargs)
                    field = "superseded" if site == "archive_directory" else site
                    with mock.patch.object(A.os, "lstat", side_effect=lookup):
                        if number == errno.EACCES:
                            with self.assertRaisesRegex(A.AuthorityError, field) as caught:
                                operation(d, expected)
                            self.assertIs(caught.exception.__cause__, fault)
                            self.assertIn("durability is unconfirmed", str(caught.exception))
                        elif site == "archive_directory":
                            self.refuses(field, operation, d, expected)
                        else:
                            self.eq(operation(d, expected), expected)

    def test_durability_faults(self):
        cases = [("open", 1), ("flock", 1), ("mkdir", 1)]
        for step in ("mkstemp", "write", "flush", "replace", "replace_after"):
            cases.extend((step, occurrence) for occurrence in (1, 2))
        # Parent directory, archive file/directory, current file/directory.
        cases.extend(("fsync", occurrence) for occurrence in range(1, 6))
        for step, occurrence in cases:
            with self.subTest(step=step, occurrence=occurrence), tempfile.TemporaryDirectory() as d:
                A.create(d, grant())
                (Path(d) / "superseded").mkdir()
                current = Path(d) / A.RECORD_NAME
                old_raw = current.read_bytes()
                fault = OSError(errno.EIO, "durability")
                owner = A.fcntl if step == "flock" else A.os
                if step == "mkstemp":
                    owner = A.tempfile
                name = "replace" if step == "replace_after" else step
                calls = []
                def injected(original):
                    def call(*args, **kwargs):
                        calls.append(args)
                        if len(calls) == occurrence:
                            if step == "replace_after":
                                original(*args, **kwargs)
                            raise fault
                        return original(*args, **kwargs)
                    return call
                if step in ("write", "flush"):
                    name = "fdopen"
                    fdopen = os.fdopen
                    @contextmanager
                    def effect(*args, **kwargs):
                        with fdopen(*args, **kwargs) as handle:
                            proxy = mock.Mock(wraps=handle)
                            getattr(proxy, step).side_effect = injected(getattr(handle, step))
                            yield proxy
                else:
                    effect = injected(getattr(owner, name))
                with mock.patch.object(owner, name, side_effect=effect):
                    with self.assertRaisesRegex(A.AuthorityError, "record") as caught:
                        A.replace(d, self.successor())
                self.assertIs(caught.exception.__cause__, fault)
                self.eq(len(calls), occurrence)
                uncertain = (step in ("replace", "replace_after") and occurrence == 2
                             or step == "fsync" and occurrence == 5)
                if uncertain:
                    self.assertIn("durability is unconfirmed", str(caught.exception))
                    self.assertIn(A.read(d), [grant(), self.successor()])
                else:
                    self.eq(current.read_bytes(), old_raw)
                    self.eq(A.read(d), grant())

    def test_missing_and_unreadable_are_distinct(self):
        with self.assertRaises(FileNotFoundError), self.filesystem_refusal():
            A.read(self.d)
        self.create()
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError), self.filesystem_refusal():
                A.read(self.d)
        self.stored(grant())

    @unittest.skipIf(os.geteuid() == 0, "root bypasses search permissions")
    def test_retired_id_unsearchable_archive(self):
        self.create()
        old = A.revoke(self.d, 101, "stop")
        A.replace(self.d, self.successor(fresh=True))
        self.path.unlink()
        archive = self.d / "superseded"
        archive.chmod(0o200)
        try:
            with self.assertRaises(PermissionError), self.filesystem_refusal("grant_id"):
                A.create(self.d, grant())
            self.assertFalse(self.path.exists())
        finally:
            archive.chmod(0o700)
        self.eq(json.loads((archive / ("a" * 32 + ".json")).read_bytes()), old)


if __name__ == "__main__":
    unittest.main()
