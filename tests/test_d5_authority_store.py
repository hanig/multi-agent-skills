"""Real temporary grant stores: schema, terminal history and durable transitions."""
import errno
import fcntl
import hashlib
import io
import json
import os
import stat
import subprocess
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
                self.eq(A.read(d), old)


    def test_argument_refusals(self):
        self.create()
        for now in (True, 1.0, "101", None, 0, -1, 2**63, 1 << 16384):
            self.refuses("now", A.check, self.d, now, MANDATE)
        for content in ("text", None, bytearray(MANDATE)):
            self.refuses("mandate_bytes", A.check, self.d, 101, content)
        self.stored(grant())

    def test_expired_mismatch_is_persisted(self):
        self.create()
        expected = grant()
        expiry = expected["expires_at"]
        self.eq(A.check(self.d, expiry, MANDATE), "expired")
        self.eq(A.check(self.d, expiry + 1, b"changed"), "mandate_changed")
        self.eq(A.check(self.d, expiry + 2, MANDATE), "invalidated")
        expected["invalidation"] = records.evaluate(expected, expiry + 1, b"changed")[1]
        self.stored(expected)


    def test_interrupted_write(self):
        self.create()
        old_raw = self.path.read_bytes()
        for op in (lambda: A.check(self.d, 101, b"changed"),):
            with mock.patch.object(A.os, "replace", side_effect=OSError("injected before replace")):
                with self.assertRaisesRegex(OSError, "injected"), self.filesystem_refusal():
                    op()
            self.eq(self.path.read_bytes(), old_raw)
            self.stored(grant())
            self.eq(list(self.d.glob(".authority-*")), [])


    def test_corrupt_reads_refuse(self):
        r = grant()
        r["session_cap"] = True
        duplicate = json.dumps(grant()).replace('"session_cap": 1',
                                                '"session_cap": 2, "session_cap": 1')
        for raw in (b'{"grant_id":', b"", b"not-json", b"\xff", b"[]",
                    json.dumps(r).encode(), duplicate.encode()):
            self.path.write_bytes(raw)
            for op in (lambda: A.read(self.d),
                            lambda: A.check(self.d, 101, MANDATE)):
                with self.assertRaises(A.AuthorityError):
                    op()
            self.eq(self.path.read_bytes(), raw)

    def test_target_directory_is_input(self):
        missing = self.d / "not-created"
        for op in (lambda: A.create(missing, grant()), lambda: A.read(missing),
                        lambda: A.check(missing, 101, MANDATE)):
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
        self.eq(events.count("replace"), 2)
        for index, event in enumerate(events):
            if event == "replace":
                self.eq(events[index - 1:index + 2], ["file", "replace", "directory"])


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


    def test_missing_and_unreadable_are_distinct(self):
        with self.assertRaises(FileNotFoundError), self.filesystem_refusal():
            A.read(self.d)
        self.create()
        real_open = os.open
        def opening(path, *args, **kwargs):
            if Path(path) == self.path:
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(A.os, "open", side_effect=opening):
            with self.assertRaises(PermissionError), self.filesystem_refusal():
                A.read(self.d)
        self.stored(grant())


    def test_retired_id_lookup_error(self):
        archive = self.d / "superseded"
        archive.mkdir()
        prior = archive / ("a" * 32 + ".json")
        raw = records.encode(records.revoked(grant(), 101, "stop"))
        prior.write_bytes(raw)
        real = os.lstat
        def denied(path, *args, **kwargs):
            if Path(path).parent == archive:
                raise PermissionError(errno.EACCES, "archive lookup denied")
            return real(path, *args, **kwargs)
        for identifier in ("a" * 32, "b" * 32):
            with self.subTest(identifier=identifier):
                record = grant()
                record["grant_id"] = identifier
                with mock.patch.object(A.os, "lstat", side_effect=denied):
                    with self.assertRaises(PermissionError), self.filesystem_refusal("grant_id"):
                        A.create(self.d, record)
                self.assertFalse(self.path.exists())
                self.eq(prior.read_bytes(), raw)
        self.refuses("grant_id", A.create, self.d, grant())

    def test_lookup_routing(self):
        for site in ("record", "grant_id", "temporary"):
            for number in (errno.ENOENT, errno.EACCES):
                with self.subTest(site=site, errno=number), tempfile.TemporaryDirectory() as d:
                    directory = Path(d)
                    target = directory / "superseded" / ("a" * 32 + ".json")
                    if site == "record":
                        target = directory / A.RECORD_NAME
                    real = os.lstat
                    fault = OSError(number, "lookup")
                    def lookup(path, *args, **kwargs):
                        match = (Path(path).name.startswith(".authority-")
                                 if site == "temporary" else Path(path) == target)
                        if match:
                            raise fault
                        return real(path, *args, **kwargs)
                    with mock.patch.object(A.os, "lstat", side_effect=lookup):
                        if number == errno.EACCES:
                            with self.assertRaisesRegex(A.AuthorityError, site) as caught:
                                A.create(d, grant())
                            self.assertIs(caught.exception.__cause__, fault)
                            self.assertIn("durability is unconfirmed", str(caught.exception))
                        else:
                            self.eq(A.create(d, grant()), grant())

    def test_durability_faults(self):
        steps = ("lock_open", "flock", "mkstemp", "fdopen", "write", "flush",
                 "file_fsync", "file_close", "replace", "replace_after",
                 "directory_open", "directory_fsync", "directory_close", "lock_close")
        for operation in ("create", "check"):
            for step in steps:
                with self.subTest(operation=operation, step=step), tempfile.TemporaryDirectory() as d:
                    current = Path(d) / A.RECORD_NAME
                    old = None
                    new = grant()
                    if operation == "check":
                        A.create(d, grant())
                        old = current.read_bytes()
                        new["invalidation"] = records.evaluate(new, 101, b"changed")[1]
                    fault = OSError(errno.EIO, "durability")
                    calls = []
                    real_open, real_close = os.open, os.close
                    descriptors = {}
                    def opening(path, flags, *args, **kwargs):
                        kind = "lock" if Path(path).name == A.LOCK_NAME else "directory"
                        if step == kind + "_open" and (kind == "lock" or Path(path) == Path(d)):
                            calls.append(step)
                            raise fault
                        fd = real_open(path, flags, *args, **kwargs)
                        if kind == "lock" or Path(path) == Path(d):
                            descriptors[fd] = kind
                        return fd
                    def closing(fd):
                        kind = descriptors.pop(fd, None)
                        real_close(fd)
                        if kind is not None and step == kind + "_close":
                            calls.append(step)
                            raise fault
                    def injected(original):
                        def call(*args, **kwargs):
                            calls.append(step)
                            if step == "replace_after":
                                original(*args, **kwargs)
                            raise fault
                        return call
                    owner, name = A.os, "replace"
                    effect = None
                    if step == "flock":
                        owner, name = A.fcntl, "flock"
                    elif step == "mkstemp":
                        owner, name = A.tempfile, "mkstemp"
                    elif step in ("fdopen", "write", "flush", "file_close"):
                        name = "fdopen"
                        fdopen = os.fdopen
                        def file_open(fd, mode, **kwargs):
                            handle = fdopen(fd, mode, **kwargs)
                            if mode != "wb":
                                return handle
                            if step == "fdopen":
                                handle.close()
                                calls.append(step)
                                raise fault
                            proxy = mock.Mock(wraps=handle)
                            if step == "file_close":
                                def close():
                                    handle.close()
                                    calls.append(step)
                                    raise fault
                                proxy.close.side_effect = close
                            else:
                                getattr(proxy, step).side_effect = injected(getattr(handle, step))
                            return proxy
                        effect = file_open
                    elif step in ("file_fsync", "directory_fsync"):
                        name = "fsync"
                        fsync = os.fsync
                        def sync(fd):
                            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                            if directory == (step == "directory_fsync"):
                                calls.append(step)
                                raise fault
                            return fsync(fd)
                        effect = sync
                    elif step.endswith("_open") or step in ("directory_close", "lock_close"):
                        name = "fstat"  # open/close hooks below inject these faults.
                        effect = os.fstat
                    effect = effect or injected(getattr(owner, name))
                    with mock.patch.object(A.os, "open", side_effect=opening), \
                            mock.patch.object(A.os, "close", side_effect=closing), \
                            mock.patch.object(owner, name, side_effect=effect):
                        with self.assertRaisesRegex(A.AuthorityError, "record") as caught:
                            if operation == "create":
                                A.create(d, grant())
                            else:
                                A.check(d, 101, b"changed")
                    self.assertIs(caught.exception.__cause__, fault)
                    self.eq(calls, [step])
                    after = step in ("replace", "replace_after", "directory_open",
                                     "directory_fsync", "directory_close", "lock_close")
                    if after:
                        self.assertIn("durability is unconfirmed", str(caught.exception))
                        visible = A.read(d) if current.exists() else None
                        self.assertIn(visible, [grant() if old else None, new])
                    elif old is not None:
                        self.eq(current.read_bytes(), old)
                        self.eq(A.read(d), grant())
                    else:
                        self.assertFalse(current.exists())

    def test_nonregular_reads_refuse_promptly(self):
        script = (
            "import sys; from tests.test_d5_authority_store import A, MANDATE\n"
            "try:\n"
            " (A.read(sys.argv[1]) if sys.argv[2] == 'read' "
            "else A.check(sys.argv[1], 101, MANDATE))\n"
            "except A.AuthorityError as exc:\n"
            " assert 'record' in str(exc), str(exc)\n"
            "else:\n"
            " raise AssertionError('accepted non-regular record')\n"
        )
        for kind in ("fifo", "directory", "symlink", "dangling"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                path = Path(d) / A.RECORD_NAME
                if kind == "fifo":
                    os.mkfifo(path)
                elif kind == "directory":
                    path.mkdir()
                else:
                    target = Path(d) / "target"
                    if kind == "symlink":
                        target.write_bytes(records.encode(grant()))
                    path.symlink_to(target)
                for operation in ("read", "check"):
                    with self.subTest(operation=operation):
                        result = subprocess.run([sys.executable, "-c", script, d, operation],
                                                cwd=str(ROOT), capture_output=True,
                                                text=True, timeout=2)
                        self.eq(result.returncode, 0, result.stdout + result.stderr)

    def test_cleanup_preserves_primary_failure(self):
        for cleanup in ("unlink", "lookup", "directory_close", "file_close", "read_close", "lock_close"):
            with self.subTest(cleanup=cleanup), tempfile.TemporaryDirectory() as d:
                A.create(d, grant())
                primary = OSError(errno.EIO, "primary failure")
                secondary = OSError(errno.EACCES, "cleanup failure")
                real_close, real_fsync, real_lstat, fdopen = os.close, os.fsync, os.lstat, os.fdopen
                path_open = io.open
                lock_info = (Path(d) / A.LOCK_NAME).stat()
                def closing(fd):
                    info = os.fstat(fd)
                    directory = stat.S_ISDIR(info.st_mode)
                    lock = (info.st_dev, info.st_ino) == (lock_info.st_dev, lock_info.st_ino)
                    real_close(fd)
                    if (directory and cleanup == "directory_close") or (lock and cleanup == "lock_close"):
                        raise secondary
                def sync(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode):
                        raise primary
                    return real_fsync(fd)
                def lookup(path, *args, **kwargs):
                    if Path(path).name.startswith(".authority-"):
                        raise secondary
                    return real_lstat(path, *args, **kwargs)
                def wrapped(handle, mode):
                    if mode != ("rb" if cleanup == "read_close" else "wb"):
                        return handle
                    proxy = mock.MagicMock(wraps=handle)
                    proxy.__enter__.return_value = proxy
                    getattr(proxy, "read" if mode == "rb" else "write").side_effect = primary
                    def close():
                        handle.close()
                        raise secondary
                    proxy.close.side_effect = close
                    proxy.__exit__.side_effect = lambda *args: close()
                    return proxy
                def file_open(fd, mode, **kwargs):
                    return wrapped(fdopen(fd, mode, **kwargs), mode)
                def legacy_open(path, mode="r", *args, **kwargs):
                    return wrapped(path_open(path, mode, *args, **kwargs), mode)
                with mock.patch.object(A.os, "close", side_effect=closing) if cleanup in (
                        "directory_close", "lock_close") else mock.patch.object(A.os, "close", wraps=real_close):
                    with mock.patch.object(A.os, "replace", side_effect=primary) if cleanup in (
                            "unlink", "lookup") else mock.patch.object(A.os, "replace", wraps=os.replace):
                        with mock.patch.object(A.os, "unlink", side_effect=secondary) if cleanup == "unlink" else \
                                mock.patch.object(A.os, "unlink", wraps=os.unlink):
                            with mock.patch.object(A.os, "lstat", side_effect=lookup) if cleanup == "lookup" else \
                                    mock.patch.object(A.os, "lstat", wraps=real_lstat):
                                with mock.patch.object(A.os, "fsync", side_effect=sync) if cleanup == "directory_close" else \
                                        mock.patch.object(A.os, "fsync", wraps=real_fsync):
                                    with mock.patch.object(io, "open", side_effect=legacy_open) if cleanup == "read_close" else \
                                            mock.patch.object(io, "open", wraps=path_open), \
                                            mock.patch.object(A.os, "fdopen", side_effect=file_open) if cleanup in (
                                            "file_close", "read_close") else mock.patch.object(A.os, "fdopen", wraps=fdopen):
                                        with self.assertRaises(A.AuthorityError) as caught:
                                            if cleanup == "lock_close":
                                                A.check(d, True, MANDATE)
                                            else:
                                                A.check(d, 101, b"changed")
                error = caught.exception
                if cleanup == "lock_close":
                    self.assertIn("now", str(error))
                else:
                    self.assertIs(error.__cause__, primary)
                    error = primary
                attached = error.cleanup_errors[0]
                self.assertIs(attached.__cause__ if cleanup == "lookup" else attached, secondary)


if __name__ == "__main__":
    unittest.main()
