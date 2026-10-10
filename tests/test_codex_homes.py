"""Offline acceptance evidence for docs/plan-codex-agent-homes.md (except 6)."""
import contextlib
import copy
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import test_attempt_worktrees as F
from tests.scheduler_fixture import closed_bin, isolated_module_path
from tests.scheduler_fixture import cleanup_module_path as tearDownModule

S = F.S
CH = S.CH
KEY = "fake-codex-home-secret-never-persist"


def setUpModule():
    isolated_module_path()


class TestCodexHomes(unittest.TestCase):
    attempt = F.TestPerAttemptWorktrees.attempt
    submit = F.TestPerAttemptWorktrees.submit

    def setUp(self):
        F.TestPerAttemptWorktrees.setUp(self)
        self.source = CH.source_home()
        self.operator = CH.operator_home()
        self.auth = self.source / "auth.json"
        self.write_auth({"auth_mode": "apikey", "OPENAI_API_KEY": KEY})
        (self.operator / "config.toml").write_text("# operator config\n")
        (self.operator / "skills").mkdir()
        (self.operator / "skills" / "keep.txt").write_text("operator skill\n")
        self.state_dir = self.tmp / "state"
        ok, reason = S.acquire_lease(str(self.state_dir))
        self.assertTrue(ok, reason)
        self.addCleanup(S.release_lease, str(self.state_dir))

    def write_auth(self, value, mode=0o400):
        if self.auth.exists():
            self.auth.chmod(0o600)
        self.auth.write_text(json.dumps(value))
        self.auth.chmod(mode)

    def launch(self, uid="code", attempt_id="attempt"):
        attempt = self.attempt(uid, attempt_id)
        unit = F.code_unit(self.repo, uid)
        unit["provider"] = "codex/fixture-model"
        state = {"units": {}}
        job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        self.assertTrue(job)
        return unit, attempt, state

    def home(self, state, uid="code", attempt="attempt"):
        return Path(state["units"][uid]["attempt_codex_homes"][attempt]["path"])

    def home_record(self, home):
        info = home.lstat()
        return {"path": str(home), "identity": {"device": info.st_dev,
                                               "inode": info.st_ino}}

    def assert_sources(self):
        self.assertEqual(json.loads(self.auth.read_text())["OPENAI_API_KEY"], KEY)
        self.assertEqual(stat.S_IMODE(self.auth.stat().st_mode), 0o400)
        self.assertEqual((self.operator / "config.toml").read_text(), "# operator config\n")
        self.assertEqual((self.operator / "skills" / "keep.txt").read_text(), "operator skill\n")

    def test_two_codex_units_one_advance_and_other_provider(self):
        units = [F.code_unit(self.repo, uid) for uid in ("a", "b", "c")]
        for unit in units:
            unit["provider"] = "codex/fixture-model"
            unit["outputs"] = ["out"]
        units[-1]["provider"] = "claude/fixture-model"
        state = {"units": {}}
        report, dispatched, halted = S.advance(
            {"name": "homes", "units": units}, state, str(self.state_dir),
            str(self.tmp / "runs"), False)
        self.assertEqual(dispatched, 3, report)
        self.assertIsNone(halted)
        homes = []
        for argv in self.fake.launches:
            values = [argv[i + 1] for i, token in enumerate(argv) if token == "--env"]
            selected = [v.split("=", 1)[1] for v in values if v.startswith("CODEX_HOME=")]
            uid = next(v.split("=", 1)[1] for v in values if v.startswith("SWARM_UNIT_ID="))
            if uid == "c":
                self.assertEqual(selected, [])
                self.assertNotIn("attempt_codex_homes", state["units"][uid])
                continue
            self.assertEqual(len(selected), 1)
            home = Path(selected[0])
            homes.append(home)
            attempt = Path(state["units"][uid]["attempt_dir"]).name
            self.assertEqual(home, self.home(state, uid, attempt))
            self.assertEqual(home.parent, self.state_dir.resolve() / "codex-homes")
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
            self.assertTrue((home / "auth.json").is_symlink())
            self.assertEqual((home / "auth.json").resolve(), self.auth.resolve())
            self.assertEqual((home / "config.toml").resolve(), (self.operator / "config.toml").resolve())
            self.assertEqual((home / "skills").resolve(), (self.operator / "skills").resolve())
            self.assertFalse(S.CP._inside(home, state["units"][uid]["attempt_dir"]))
            self.assertFalse(S.CP._inside(home, self.repo))
        self.assertEqual(len(set(homes)), 2)

    def test_invalid_sources_refuse_before_paseo_with_safe_fix(self):
        cases = [(None, None), ({"auth_mode": "chatgpt", "key": KEY}, 0o400),
                 ({"auth_mode": KEY}, 0o400), ([], 0o400)]
        cases += [({"auth_mode": "apikey", "key": KEY}, mode)
                  for mode in (0o600, 0o200, 0o440, 0o404, 0o444, 0o500, 0o4000)]
        for i, (value, mode) in enumerate(cases):
            with self.subTest(mode=mode, i=i):
                if value is None:
                    self.auth.unlink()
                else:
                    self.write_auth(value, mode)
                state = {"units": {}}
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    job, error = self.submit(F.code_unit(self.repo), self.attempt("code", f"bad-{i}"), False, state)
                self.assertIsNone(job)
                self.assertIn(str(self.auth), error)
                self.assertIn("chmod 600", error)
                self.assertIn("printenv OPENAI_API_KEY |", error)
                self.assertIn("codex login --with-api-key && chmod 400", error)
                self.assertEqual(len(error.splitlines()), 1)
                self.assertNotIn(KEY, error + captured.getvalue() + json.dumps(state))
                self.assertEqual(self.fake.launches, [])

    def test_malformed_json_does_not_echo_key(self):
        self.auth.chmod(0o600)
        self.auth.write_text(KEY + "{\n")
        self.auth.chmod(0o400)
        with self.assertRaises(CH.HomeError) as caught:
            CH.validate_source()
        self.assertNotIn(KEY, str(caught.exception))

    def test_mode_refusal_explains_filesystem_requirement(self):
        self.auth.chmod(0o777)
        with self.assertRaises(CH.HomeError) as caught:
            CH.validate_source()
        reason = str(caught.exception)
        self.assertIn("POSIX filesystem that stores file modes", reason)
        self.assertIn("ExFAT", reason)
        self.assertIn("FAT", reason)
        self.assertEqual(len(reason.splitlines()), 1)
        self.assertNotIn(KEY, reason)

    def test_root_refuses_before_home_creation_and_paseo(self):
        attempt = self.attempt("code", "root")
        unit = F.code_unit(self.repo)
        unit["provider"] = "codex/fixture-model"
        home = CH.home_path(self.state_dir, attempt.name)
        with mock.patch.object(CH.os, "geteuid", return_value=0):
            state = {"units": {}}
            job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(job)
        self.assertIn("effective uid 0", error)
        self.assertIn("0400", error)
        self.assertEqual(len(error.splitlines()), 1)
        self.assertEqual(self.fake.launches, [])
        self.assertFalse(home.parent.exists())
        self.assertNotIn("attempt_codex_homes", state["units"]["code"])
        self.assert_sources()

    def test_root_with_invalid_source_refuses_without_reading_source(self):
        self.write_auth({"auth_mode": "chatgpt", "key": KEY})
        attempt = self.attempt("code", "root-invalid-source")
        unit = F.code_unit(self.repo)
        unit["provider"] = "codex/fixture-model"
        with mock.patch.object(CH.os, "geteuid", return_value=0), \
                mock.patch.object(CH, "validate_source", wraps=CH.validate_source) as validate, \
                mock.patch.object(os, "stat", wraps=os.stat) as stat_call, \
                mock.patch.object(os, "lstat", wraps=os.lstat) as lstat_call:
            job, error = self.submit(unit, attempt, False, {"units": {}})
        self.assertIsNone(job)
        self.assertIn("root", error)
        self.assertIn("effective uid 0", error)
        for repair in ("fix:", "chmod", "printenv", "codex login"):
            self.assertNotIn(repair, error)
        validate.assert_not_called()
        for call in stat_call.call_args_list + lstat_call.call_args_list:
            path = call.args[0]
            if not isinstance(path, int):
                self.assertNotIn(os.fspath(path), (str(self.source), str(self.auth)))
        self.assertEqual(self.fake.launches, [])
        self.assertFalse(CH.home_path(self.state_dir, attempt.name).exists())

    def test_source_must_be_regular_owned_and_unchanged_during_open(self):
        self.auth.unlink()
        self.auth.symlink_to(self.operator / "config.toml")
        with self.assertRaisesRegex(CH.HomeError, "regular file"):
            CH.validate_source()
        self.auth.unlink()
        self.auth.mkdir()
        with self.assertRaisesRegex(CH.HomeError, "regular file"):
            CH.validate_source()
        self.auth.rmdir()
        self.write_auth({"auth_mode": "apikey", "key": KEY})
        with mock.patch.object(CH.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(CH.HomeError, "owned"):
                CH.validate_source()
        original = self.auth.stat()
        changed = list(original)
        changed[1] += 1
        with mock.patch.object(CH.os, "fstat", return_value=os.stat_result(changed)):
            with self.assertRaisesRegex(CH.HomeError, "changed"):
                CH.validate_source()

    def test_source_revalidated_for_each_launch(self):
        self.launch()
        self.auth.chmod(0o600)
        job, error = self.submit(F.code_unit(self.repo, "second"), self.attempt("second", "second"), False, {"units": {}})
        self.assertIsNone(job)
        self.assertIn("0400", error)
        self.assertEqual(len(self.fake.launches), 1)

    def test_write_through_auth_symlink_fails_and_preserves_bytes(self):
        _unit, _attempt, state = self.launch()
        before = self.auth.read_bytes()
        # On root CI runners drop privilege only for this real write attempt.
        kwargs = {}
        if os.getuid() == 0:
            for path in (self.tmp, self.source.parent, self.source, self.state_dir,
                         self.home(state).parent, self.home(state)):
                path.chmod(0o755)
            kwargs["preexec_fn"] = lambda: os.setuid(65534)
        result = subprocess.run([sys.executable, "-c",
                                 "import sys; open(sys.argv[1], 'w').write('clobber')",
                                 str(self.home(state) / "auth.json")],
                                capture_output=True, text=True, **kwargs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PermissionError", result.stderr)
        self.assertEqual(self.auth.read_bytes(), before)

    def test_exclusive_creation_and_no_symlinked_parent(self):
        attempt = self.attempt("code", "exists")
        home = CH.home_path(self.state_dir, attempt.name)
        home.mkdir(parents=True)
        (home / "keep").write_text("existing")
        with self.assertRaises(CH.HomeError):
            CH.allocate(self.state_dir, attempt.name, attempt, F.code_unit(self.repo))
        self.assertEqual((home / "keep").read_text(), "existing")
        shutil.rmtree(home.parent)
        home.parent.symlink_to(self.operator)
        with self.assertRaises(CH.HomeError):
            CH.allocate(self.state_dir, attempt.name, attempt, F.code_unit(self.repo))
        self.assertFalse((self.operator / attempt.name).exists())

    def test_chmod_failure_removes_unrecorded_allocation(self):
        attempt = self.attempt("code", "chmod-failure")
        home = CH.home_path(self.state_dir, attempt.name)
        home.parent.mkdir(mode=0o700)
        unit = F.code_unit(self.repo)
        chmod = Path.chmod

        def fail_home_chmod(path, mode):
            if path == home:
                raise PermissionError
            return chmod(path, mode)

        # Exercise the chmod failure after umask removes owner permissions.
        previous_umask = os.umask(0o100)
        try:
            with mock.patch.object(Path, "chmod", new=fail_home_chmod):
                with self.assertRaises(CH.HomeError):
                    CH.allocate(self.state_dir, attempt.name, attempt, unit)
        finally:
            os.umask(previous_umask)
        self.assertFalse(home.exists())
        # The failure must leave this attempt available for a real allocation.
        allocated, identity = CH.allocate(self.state_dir, attempt.name, attempt, unit)
        self.assertEqual(allocated, home)
        self.assertEqual(identity, self.home_record(home)["identity"])
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assert_sources()

    def test_lstat_failure_removes_unrecorded_allocation(self):
        attempt = self.attempt("code", "lstat-failure")
        home = CH.home_path(self.state_dir, attempt.name)
        sibling = home.with_name("keep")
        sibling.mkdir(parents=True)
        (sibling / "keep").write_text("unrelated home")
        unit = F.code_unit(self.repo)
        lstat = Path.lstat

        def fail_home_lstat(path):
            if path == home:
                self.assertTrue(path.is_dir())
                raise PermissionError
            return lstat(path)

        with mock.patch.object(Path, "lstat", new=fail_home_lstat):
            with self.assertRaises(CH.HomeError):
                CH.allocate(self.state_dir, attempt.name, attempt, unit)
        self.assertFalse(home.exists())
        self.assertEqual((sibling / "keep").read_text(), "unrelated home")
        allocated, identity = CH.allocate(self.state_dir, attempt.name, attempt, unit)
        self.assertEqual(allocated, home)
        self.assertEqual(identity, self.home_record(home)["identity"])
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assert_sources()

    def test_lstat_failure_cleanup_checks_directory_identity(self):
        for field in ("mode", "device", "inode"):
            with self.subTest(field=field):
                attempt = self.attempt("code", "lstat-" + field)
                home = CH.home_path(self.state_dir, attempt.name)
                lstat, raw_stat = Path.lstat, os.stat
                reads = []

                def fail_home_lstat(path):
                    if path == home:
                        raise PermissionError
                    return lstat(path)

                def changed_identity(path, *args, **kwargs):
                    info = raw_stat(path, *args, **kwargs)
                    if Path(path) == home and kwargs.get("follow_symlinks") is False:
                        reads.append(info)
                        if len(reads) == 2:
                            changed = list(info)
                            if field == "mode":
                                changed[0] = stat.S_IFLNK | 0o700
                            else:
                                changed[2 if field == "device" else 1] += 1
                            return os.stat_result(changed)
                    return info

                with mock.patch.object(Path, "lstat", new=fail_home_lstat):
                    with mock.patch.object(CH.os, "stat", side_effect=changed_identity):
                        with self.assertRaisesRegex(CH.HomeError, "identity changed"):
                            CH.allocate(self.state_dir, attempt.name, attempt,
                                        F.code_unit(self.repo))
                self.assertEqual(len(reads), 2)
                self.assertTrue(home.is_dir())
        self.assert_sources()

    def test_owner_write_removing_umask_allows_new_and_existing_parent(self):
        parent = self.state_dir / "codex-homes"
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    parent.chmod(0o500)
                attempt = self.attempt("code", "umask-" + str(existing))
                previous_umask = os.umask(0o200)
                try:
                    home, _identity = CH.allocate(self.state_dir, attempt.name, attempt,
                                                 F.code_unit(self.repo))
                    self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
                    self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
                    CH.populate(home, self.auth)
                    self.assertEqual((home / "auth.json").resolve(), self.auth.resolve())
                finally:
                    os.umask(previous_umask)
                    if parent.exists():
                        parent.chmod(0o700)
        self.assert_sources()

    def test_dry_run_then_real_launch_does_not_reserve_home_or_save(self):
        attempt = self.attempt("code", "dry-then-real")
        unit = F.code_unit(self.repo)
        unit["provider"] = "codex/fixture-model"
        state = {"units": {}}
        S.save_state(str(self.state_dir), state)
        before = {p.relative_to(self.state_dir): p.read_bytes()
                  for p in self.state_dir.rglob("*") if p.is_file()}
        home = CH.home_path(self.state_dir, attempt.name)
        with mock.patch.object(S, "save_state", wraps=S.save_state) as save:
            job, error = self.submit(unit, attempt, True, state)
        self.assertIsNone(error)
        save.assert_not_called()
        self.assertFalse(home.parent.exists())
        self.assertEqual(list(attempt.iterdir()), [])
        self.assertEqual(self.fake.launches, [])
        self.assertTrue(job.startswith("dry-"))
        self.assertEqual(before, {p.relative_to(self.state_dir): p.read_bytes()
                                 for p in self.state_dir.rglob("*") if p.is_file()})
        job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(error)
        self.assertTrue(job)
        self.assertEqual(len(self.fake.launches), 1)
        self.assertEqual(self.home(state, attempt=attempt.name), home)
        self.assertTrue((home / "auth.json").is_symlink())
        self.assert_sources()

    def test_allocation_sets_private_modes_explicitly(self):
        attempt = self.attempt("code", "final-mode")
        chmod = Path.chmod
        previous_umask = os.umask(0o077)
        try:
            with mock.patch.object(Path, "chmod", autospec=True,
                                   side_effect=chmod) as patched:
                home, _identity = CH.allocate(self.state_dir, attempt.name, attempt,
                                             F.code_unit(self.repo))
                self.assertEqual(patched.call_args_list,
                                 [mock.call(home.parent, 0o700),
                                  mock.call(home, 0o700)])
        finally:
            os.umask(previous_umask)
        self.assertEqual(stat.S_IMODE(home.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)

    def test_allocation_does_not_chmod_foreign_existing_parent(self):
        attempt = self.attempt("code", "foreign-parent")
        home = CH.home_path(self.state_dir, attempt.name)
        home.parent.mkdir(mode=0o755)
        home.parent.chmod(0o755)
        # Simulate a different owner without requiring chown privileges.
        with mock.patch.object(CH.os, "getuid", return_value=os.getuid() + 1):
            allocated, _identity = CH.allocate(self.state_dir, attempt.name, attempt,
                                              F.code_unit(self.repo))
            self.assertEqual(allocated, home)
        self.assertEqual(stat.S_IMODE(home.parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)

    def test_failed_allocation_cleanup_refuses_replacement(self):
        parent = self.state_dir / "codex-homes"
        parent.mkdir(mode=0o700)
        for replacement in ("directory", "symlink"):
            with self.subTest(replacement=replacement):
                attempt = self.attempt("code", "replace-" + replacement)
                home = CH.home_path(self.state_dir, attempt.name)
                original = home.with_name(home.name + "-original")
                chmod = Path.chmod

                def replace_and_fail(path, mode):
                    if path != home:
                        return chmod(path, mode)
                    home.rename(original)
                    if replacement == "directory":
                        home.mkdir(mode=0o700)
                    else:
                        home.symlink_to(original, target_is_directory=True)
                    raise PermissionError

                previous_umask = os.umask(0o100)
                try:
                    with mock.patch.object(Path, "chmod", new=replace_and_fail):
                        with self.assertRaises(CH.HomeError) as caught:
                            CH.allocate(self.state_dir, attempt.name, attempt,
                                        F.code_unit(self.repo))
                finally:
                    os.umask(previous_umask)
                self.assertTrue(original.is_dir())
                self.assertTrue(home.is_dir())
                if replacement == "symlink":
                    self.assertTrue(home.is_symlink())
                    self.assertEqual(os.readlink(home), str(original))
                self.assertIn("identity changed", str(caught.exception))
        self.assert_sources()

    def test_home_cannot_be_in_worktree_or_attempt_root(self):
        attempt = self.attempt("code", "boundary")
        for state_dir in (self.repo / "state", attempt / "state"):
            with self.subTest(state_dir=state_dir):
                with self.assertRaises((CH.HomeError, S.CP.PathPolicyError)):
                    CH.allocate(state_dir, attempt.name, attempt, F.code_unit(self.repo))
                self.assertFalse(state_dir.exists())
        # A different Git checkout at the home parent is also forbidden.
        parent = self.state_dir / "codex-homes"
        F.repo_at(parent)
        with self.assertRaises(CH.HomeError):
            CH.allocate(self.state_dir, attempt.name, attempt, F.code_unit(self.repo))
        self.assertFalse((parent / attempt.name).exists())

    def test_invalid_attempt_ids_are_refused(self):
        for attempt in ("", ".", "..", "../escape", "/absolute", "nested/child", None):
            with self.subTest(attempt=attempt), self.assertRaises(CH.HomeError):
                CH.home_path(self.state_dir, attempt)

    def test_absent_optional_links_are_skipped_and_default_paths_ignore_codex_home(self):
        (self.operator / "config.toml").unlink()
        shutil.rmtree(self.operator / "skills")
        _unit, _attempt, state = self.launch()
        self.assertEqual(sorted(p.name for p in self.home(state).iterdir()), ["auth.json"])
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp), "CODEX_HOME": "/unused"}):
            with mock.patch.dict(os.environ):
                os.environ.pop("HANIG_SWARM_CODEX_AUTH_HOME")
                os.environ.pop("HANIG_SWARM_CODEX_OPERATOR_HOME")
                self.assertEqual(CH.source_home(), self.tmp / ".codex-api")
                self.assertEqual(CH.operator_home(), self.tmp / ".codex")

    def test_unit_cannot_override_managed_home(self):
        unit = F.code_unit(self.repo)
        unit["env"] = ["CODEX_HOME=/ignored"]
        job, error = self.submit(unit, self.attempt("code", "override"), False, {"units": {}})
        self.assertIsNone(job)
        self.assertIn("coordinator-owned", error)
        self.assertEqual(self.fake.launches, [])

    def test_path_and_identity_are_saved_before_links_and_launch(self):
        populate = CH.populate
        observed = []

        def inspect(home, auth):
            durable = S.load_state(str(self.state_dir))
            self.assertEqual(self.home(durable), home)
            self.assertEqual(durable["units"]["code"]["attempt_codex_homes"]["attempt"]["identity"],
                             self.home_record(home)["identity"])
            self.assertEqual(list(home.iterdir()), [])
            observed.append(True)
            populate(home, auth)

        with mock.patch.object(CH, "populate", side_effect=inspect):
            self.launch()
        self.assertEqual(observed, [True])

    def test_submit_does_not_restat_home_before_cleanup_record_is_saved(self):
        allocate, lstat, save_state = CH.allocate, Path.lstat, S.save_state
        pending = []
        observed = []

        def allocated(*args, **kwargs):
            result = allocate(*args, **kwargs)
            pending.append(CH.home_path(args[0], args[1]))
            return result

        def fail_unrecorded_stat(path):
            if path in pending:
                raise OSError("fixture post-allocation stat failure")
            return lstat(path)

        def save(path, state):
            if pending:
                home = pending[0]
                meta = state["units"]["code"]["attempt_codex_homes"][home.name]
                info = lstat(home)
                self.assertEqual(meta["identity"],
                                 {"device": info.st_dev, "inode": info.st_ino})
                observed.append(home)
                pending.clear()
            return save_state(path, state)

        with mock.patch.object(CH, "allocate", side_effect=allocated), \
                mock.patch.object(Path, "lstat", new=fail_unrecorded_stat), \
                mock.patch.object(S, "save_state", side_effect=save):
            self.launch()
        self.assertEqual(len(observed), 1)

    def test_submit_stat_failure_removes_home_without_saving_cleanup_state(self):
        attempt = self.attempt("code", "submit-stat-failure")
        home = CH.home_path(self.state_dir, attempt.name)
        allocate, lstat = CH.allocate, Path.lstat
        before = []
        state_path = self.state_dir / S.STATE_FILE
        state = {"units": {}}

        def allocated(*args, **kwargs):
            before.append(state_path.read_bytes())
            return allocate(*args, **kwargs)

        def fail_home_stat(path):
            if path == home:
                self.assertTrue(home.is_dir())
                raise OSError("fixture allocation stat failure")
            return lstat(path)

        with mock.patch.object(CH, "allocate", side_effect=allocated), \
                mock.patch.object(Path, "lstat", new=fail_home_stat):
            job, error = self.submit(F.code_unit(self.repo), attempt, False, state)
        self.assertIsNone(job)
        self.assertIn("cannot exclusively create Codex home", error)
        self.assertFalse(home.exists())
        self.assertEqual(before, [state_path.read_bytes()])
        self.assertNotIn("attempt_codex_homes", state["units"]["code"])
        self.assertEqual(self.fake.launches, [])

    def test_failed_home_metadata_construction_rolls_back_allocation(self):
        for failure in (RuntimeError, KeyboardInterrupt):
            with self.subTest(failure=failure.__name__):
                attempt = self.attempt("code", "metadata-" + failure.__name__)
                home = CH.home_path(self.state_dir, attempt.name)
                sibling = home.with_name(home.name + "-keep")
                sibling.mkdir(parents=True)
                (sibling / "keep").write_text("unrelated home")
                state = {"units": {}}
                before, allocated_home, injected = [], [], []
                allocate, path_str = CH.allocate, Path.__str__
                state_path = self.state_dir / S.STATE_FILE

                def allocated(*args, **kwargs):
                    before.append((copy.deepcopy(state), state_path.read_bytes()))
                    result = allocate(*args, **kwargs)
                    allocated_home.append(result[0])
                    return result

                def fail_metadata(path):
                    if allocated_home and path is allocated_home[0] and not injected:
                        injected.append(True)
                        raise failure("fixture home metadata failure")
                    return path_str(path)

                with mock.patch.object(CH, "allocate", side_effect=allocated), \
                        mock.patch.object(Path, "__str__", new=fail_metadata), \
                        mock.patch.object(CH, "populate") as populate:
                    with self.assertRaisesRegex(failure, "fixture home metadata failure"):
                        self.submit(F.code_unit(self.repo), attempt, False, state)
                self.assertEqual(injected, [True])
                self.assertFalse(home.exists())
                self.assertEqual(state, before[0][0])
                self.assertEqual(state_path.read_bytes(), before[0][1])
                self.assertEqual((sibling / "keep").read_text(), "unrelated home")
                self.assertEqual(self.fake.launches, [])
                populate.assert_not_called()
        self.assert_sources()

    def test_failed_state_save_removes_only_the_unrecorded_home(self):
        for failure in ("write-error", "os-error"):
            with self.subTest(failure=failure):
                attempt = self.attempt("code", failure)
                home = CH.home_path(self.state_dir, attempt.name)
                sibling = home.with_name(failure + "-keep")
                sibling.mkdir(parents=True)
                (sibling / "keep").write_text("unrelated home")
                state = {"units": {}}
                write_json = S.U.write_json
                observed = []
                allocate = CH.allocate
                before = []
                previous_homes = {"prior": {"path": str(sibling)}}
                if failure == "os-error":
                    state["units"]["code"] = {"attempt_codex_homes": previous_homes}

                def allocated(*args, **kwargs):
                    before.append(copy.deepcopy(state))
                    return allocate(*args, **kwargs)

                def fail_record(path, value, **kwargs):
                    meta = value.get("units", {}).get("code", {}).get(
                        "attempt_codex_homes", {}).get(attempt.name)
                    if Path(path).name == S.STATE_FILE and meta:
                        self.assertEqual(meta["identity"], self.home_record(home)["identity"])
                        self.assertEqual(list(home.iterdir()), [])
                        observed.append(meta)
                        if failure == "os-error":
                            raise OSError("fixture state write failure")
                        return "fixture state write failure"
                    return write_json(path, value, **kwargs)

                with mock.patch.object(S.U, "write_json", side_effect=fail_record), \
                        mock.patch.object(CH, "allocate", side_effect=allocated):
                    with self.assertRaisesRegex((SystemExit, OSError), "fixture state write failure"):
                        self.submit(F.code_unit(self.repo), attempt, False, state)
                self.assertEqual(len(observed), 1)
                self.assertFalse(home.exists())
                self.assertEqual(state, before[0])
                if failure == "os-error":
                    self.assertIs(state["units"]["code"]["attempt_codex_homes"], previous_homes)
                self.assertEqual((sibling / "keep").read_text(), "unrelated home")
                self.assertEqual(self.fake.launches, [])
                durable = S.load_state(str(self.state_dir))
                self.assertNotIn(attempt.name, durable["units"]["code"].get(
                    "attempt_codex_homes", {}))
        self.assert_sources()

    def test_failed_state_save_refuses_replaced_home(self):
        for replacement in ("directory", "symlink"):
            with self.subTest(replacement=replacement):
                attempt = self.attempt("code", "save-" + replacement)
                home = CH.home_path(self.state_dir, attempt.name)
                original = home.with_name(home.name + "-original")
                target = self.tmp / (replacement + "-target")
                target.mkdir()
                (target / "keep").write_text("untouched target")
                state = {"units": {}}
                write_json = S.U.write_json

                def replace_and_fail(path, value, **kwargs):
                    meta = value.get("units", {}).get("code", {}).get(
                        "attempt_codex_homes", {}).get(attempt.name)
                    if Path(path).name == S.STATE_FILE and meta:
                        home.rename(original)
                        if replacement == "directory":
                            home.mkdir()
                            (home / "keep").write_text("replacement home")
                        else:
                            home.symlink_to(target, target_is_directory=True)
                        return "fixture state write failure"
                    return write_json(path, value, **kwargs)

                with mock.patch.object(S.U, "write_json", side_effect=replace_and_fail):
                    job, error = self.submit(F.code_unit(self.repo), attempt, False, state)
                self.assertIsNone(job)
                self.assertIn("refusing Codex home replacement", error)
                self.assertTrue(original.is_dir())
                self.assertEqual(list(original.iterdir()), [])
                if replacement == "directory":
                    self.assertEqual((home / "keep").read_text(), "replacement home")
                else:
                    self.assertTrue(home.is_symlink())
                    self.assertEqual(os.readlink(home), str(target))
                self.assertEqual((target / "keep").read_text(), "untouched target")
                self.assertNotIn("attempt_codex_homes", state["units"]["code"])
                self.assertEqual(self.fake.launches, [])
        self.assert_sources()

    def test_failed_population_is_recorded_unlaunched_and_cleanable(self):
        attempt = self.attempt("code", "link-failure")
        unit = F.code_unit(self.repo)
        state = {"units": {}}
        with mock.patch.object(Path, "symlink_to", side_effect=PermissionError):
            job, error = self.submit(unit, attempt, False, state)
        self.assertIsNone(job)
        self.assertIn("cannot populate Codex home", error)
        self.assertEqual(self.fake.launches, [])
        durable = S.load_state(str(self.state_dir))
        home = self.home(durable, attempt=attempt.name)
        self.assertTrue(home.is_dir())
        S._archive_code_worktree(durable, unit, str(attempt), [], str(self.state_dir))
        self.assertFalse(home.exists())
        self.assert_sources()

    def test_failed_code_attempt_without_workspace_record_cleans_home(self):
        unit = F.code_unit(self.repo)
        attempt = self.attempt("code", "before-workspace")
        home, _identity = CH.allocate(self.state_dir, attempt.name, attempt, unit)
        CH.populate(home, self.auth)
        state = {"units": {"code": {
            "state": "FAILED", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "gpu_hours": 0,
            "attempt_codex_homes": {attempt.name: self.home_record(home)},
        }}}
        S.save_state(str(self.state_dir), state)
        durable = S.load_state(str(self.state_dir))
        self.assertNotIn("attempt_workspaces", durable["units"]["code"])
        with mock.patch.object(S, "reconcile_orphan", return_value=(None, None)):
            report, count, _halted = S.advance(
                {"units": [unit]}, durable, str(self.state_dir),
                str(self.tmp / "runs"), False, max_new=0)
        self.assertEqual(count, 0, report)
        self.assertFalse(home.exists(), report)
        saved = S.load_state(str(self.state_dir))
        self.assertTrue(saved["units"]["code"]["attempt_codex_homes"][attempt.name]["removed"])
        self.assert_sources()

    def test_preservation_precedes_cleanup_and_sources_survive(self):
        unit, attempt, state = self.launch()
        home = self.home(state)
        (home / "state_5.sqlite").write_bytes(b"runtime")
        (home / "nested").mkdir()
        (home / "nested" / "source").symlink_to(self.source, target_is_directory=True)
        workspace = Path(state["units"]["code"]["attempt_workspaces"][attempt.name]["path"])
        (workspace / "work.txt").write_bytes(b"preserved work")
        with mock.patch.object(S.R, "preserve_worktree", return_value=(None, "fixture failure")):
            S._archive_code_worktree(state, unit, str(attempt), [], str(self.state_dir))
        # The direct home retry entry point must enforce preservation too.
        S._cleanup_codex_home(state, unit, attempt.name, [], str(self.state_dir))
        self.assertTrue(home.is_dir())
        self.assertTrue(workspace.is_dir())
        S._archive_code_worktree(state, unit, str(attempt), [], str(self.state_dir))
        self.assertFalse(home.exists())
        self.assertFalse(workspace.exists())
        snapshot = state["units"]["code"]["attempt_recovery_snapshots"][attempt.name]
        restored = self.tmp / "restored"
        self.assertIsNone(S.R.restore_audit_copy(snapshot, restored))
        self.assertEqual((restored / "work.txt").read_bytes(), b"preserved work")
        self.assert_sources()

    def test_legacy_archive_also_cleans_home_after_preservation(self):
        unit, attempt, state = self.launch()
        meta = state["units"]["code"]["attempt_workspaces"][attempt.name]
        meta.update(workspace_owner="paseo", workspace_id="fixture-workspace")
        archives = []

        def run(argv, **kwargs):
            if argv[:3] == ["paseo", "workspace", "archive"]:
                self.assertIn(attempt.name, state["units"]["code"]["attempt_recovery_snapshots"])
                archives.append(argv)
                return 0, "{}", ""
            return self.fake(argv, **kwargs)

        with mock.patch.object(S.U, "run", side_effect=run):
            S._archive_code_worktree(state, unit, str(attempt), [], str(self.state_dir))
        self.assertEqual(len(archives), 1)
        self.assertFalse(self.home(state).exists())
        self.assert_sources()

    def test_cleanup_refuses_missing_or_redirected_state_path(self):
        _unit, attempt, state = self.launch()
        home = self.home(state)
        for recorded in (None, str(self.operator), str(home) + "/../" + attempt.name,
                         str(home.parent / "another-attempt")):
            with self.subTest(recorded=recorded), self.assertRaises(CH.HomeError):
                CH.remove(self.state_dir, attempt.name, recorded,
                          self.home_record(home)["identity"])
        # An agent's own records cannot enroll an unrecorded home.
        (attempt / "launch.json").write_text(json.dumps({"codex_home": str(home)}))
        state["units"]["code"].pop("attempt_codex_homes")
        S._cleanup_codex_home(state, {"id": "code"}, attempt.name, [], str(self.state_dir))
        self.assertTrue(home.exists())
        self.assert_sources()

    def test_cleanup_refuses_replacement_directory(self):
        unit, attempt, state = self.launch()
        home = self.home(state)
        original = home.with_name("original-home")
        home.rename(original)  # Keep the inode alive so it cannot be reused.
        home.mkdir()
        (home / "keep").write_text("replacement bytes")
        report = []
        S._archive_code_worktree(state, unit, str(attempt), report, str(self.state_dir))
        self.assertEqual((home / "keep").read_text(), "replacement bytes")
        self.assertTrue(original.is_dir())
        meta = S.load_state(str(self.state_dir))["units"]["code"]["attempt_codex_homes"][attempt.name]
        self.assertTrue(meta["cleanup_pending"])
        self.assertFalse(meta["removed"])
        self.assertIn("device/inode", meta["cleanup_problem"])
        self.assertIn(meta["cleanup_problem"], "\n".join(report))
        self.assert_sources()

    def test_cleanup_refuses_replacement_symlink(self):
        unit, attempt, state = self.launch()
        home = self.home(state)
        original = home.with_name("original-home")
        home.rename(original)
        home.symlink_to(original, target_is_directory=True)
        report = []
        S._archive_code_worktree(state, unit, str(attempt), report, str(self.state_dir))
        self.assertTrue(home.is_symlink())
        self.assertEqual(os.readlink(home), str(original))
        self.assertTrue((original / "auth.json").is_symlink())
        meta = S.load_state(str(self.state_dir))["units"]["code"]["attempt_codex_homes"][attempt.name]
        self.assertTrue(meta["cleanup_pending"])
        self.assertFalse(meta["removed"])
        self.assertIn("not a real directory", meta["cleanup_problem"])
        self.assertIn(meta["cleanup_problem"], "\n".join(report))
        self.assert_sources()

    def test_cleanup_refuses_missing_or_mismatched_identity(self):
        _unit, attempt, state = self.launch()
        home = self.home(state)
        identity = self.home_record(home)["identity"]
        for recorded in (None, {}, dict(identity, device=identity["device"] + 1),
                         dict(identity, inode=identity["inode"] + 1)):
            with self.subTest(recorded=recorded), self.assertRaisesRegex(CH.HomeError, "device/inode"):
                CH.remove(self.state_dir, attempt.name, str(home), recorded)
            self.assertTrue(home.is_dir())
        self.assert_sources()

    def test_cleanup_refuses_redirected_parent(self):
        _unit, attempt, state = self.launch()
        home = self.home(state)
        identity = self.home_record(home)["identity"]
        shutil.rmtree(home)
        home.parent.rmdir()
        home.parent.symlink_to(self.operator)
        with self.assertRaises(CH.HomeError):
            CH.remove(self.state_dir, attempt.name, str(home), identity)
        self.assert_sources()

    def test_live_and_dry_attempts_keep_their_homes(self):
        unit, attempt, state = self.launch()
        home = self.home(state)
        us = state["units"]["code"]
        us.update(state="SUBMITTED", attempt_dir=str(attempt), attempts=[str(attempt)],
                  job_id="fixture-agent", gpu_hours=0)
        with mock.patch.object(S, "_check", return_value=(S.RUNNING, "", "")):
            S.advance({"units": [unit]}, state, str(self.state_dir), str(self.tmp / "runs"), False, max_new=0)
        self.assertTrue(home.exists())
        self.assertNotIn("cleanup_attempts", us["attempt_codex_homes"][attempt.name])
        us["state"] = "FAILED"
        us["attempt_codex_homes"][attempt.name]["cleanup_pending"] = True
        S.save_state(str(self.state_dir), state)
        S.advance({"units": [unit]}, state, str(self.state_dir), str(self.tmp / "runs"), True, max_new=0)
        self.assertTrue(home.exists())
        self.assertNotIn("cleanup_attempts", us["attempt_codex_homes"][attempt.name])
        dry = self.attempt("dry", "dry-home")
        _job, error = self.submit(F.code_unit(self.repo, "dry"), dry, True, {"units": {}})
        self.assertIsNone(error)
        self.assertFalse(CH.home_path(self.state_dir, dry.name).exists())

    def test_cleanup_requires_state_directory_and_missing_home_retry_succeeds(self):
        unit, attempt, state = self.launch()
        home = self.home(state)
        S._preserve_and_archive_code_worktree(state, unit, str(attempt), [], str(self.state_dir))
        S._cleanup_codex_home(state, unit, attempt.name, [], None)
        self.assertTrue(home.exists())
        self.assertIn("state_dir", state["units"]["code"]["attempt_codex_homes"][attempt.name]["cleanup_problem"])
        CH.remove(self.state_dir, attempt.name, str(home), self.home_record(home)["identity"])
        S._cleanup_codex_home(state, unit, attempt.name, [], str(self.state_dir))
        self.assertTrue(state["units"]["code"]["attempt_codex_homes"][attempt.name]["removed"])

    def test_failed_cleanup_retries_after_restart_without_blocking_dispatch(self):
        unit, attempt, state = self.launch()
        us = state["units"]["code"]
        us.update(state="FAILED", attempt_dir=str(attempt), attempts=[str(attempt)],
                  job_id="agent-fixture", gpu_hours=0, launch_recovery_problem="fixture terminal")
        home = self.home(state)
        rmtree = CH.shutil.rmtree

        def fail_home(path, *args, **kwargs):
            if Path(path) == home:
                raise PermissionError
            return rmtree(path, *args, **kwargs)

        with mock.patch.object(CH.shutil, "rmtree", side_effect=fail_home):
            S._archive_code_worktree(state, unit, str(attempt), [], str(self.state_dir))
        self.assertTrue(home.exists())
        # Release the old attempt so advance exercises the home retry loop,
        # rather than the current-attempt worktree cleanup path.
        us["attempt_dir"] = None
        S.save_state(str(self.state_dir), state)
        durable = S.load_state(str(self.state_dir))
        meta = durable["units"]["code"]["attempt_codex_homes"][attempt.name]
        self.assertTrue(meta["cleanup_pending"])
        self.assertIn("retry", meta["cleanup_problem"])
        other = F.code_unit(self.repo, "other")
        other["outputs"] = ["out"]
        # Persisted refusal must not prevent another unit from launching.
        with mock.patch.object(S, "_preserve_and_archive_code_worktree",
                               side_effect=AssertionError("home retry must not re-enter worktree cleanup")):
            with mock.patch.object(CH, "remove", side_effect=RuntimeError("fixture removal failure")) as remove:
                report, count, _halted = S.advance({"units": [unit, other]}, durable,
                    str(self.state_dir), str(self.tmp / "runs"), False)
                remove.assert_called_once_with(str(self.state_dir), attempt.name,
                                               str(home), meta["identity"])
        self.assertEqual(count, 1, report)
        self.assertEqual(len(self.fake.launches), 2)
        self.assertEqual(meta["cleanup_attempts"], 2)
        self.assertIn("RuntimeError", meta["cleanup_problem"])
        self.assertIn(meta["cleanup_problem"], "\n".join(report))
        saved = S.load_state(str(self.state_dir))
        self.assertEqual(saved["units"]["code"]["attempt_codex_homes"][attempt.name], meta)
        self.assertTrue(home.exists())
        report, _count, _halted = S.advance({"units": [unit, other]}, durable,
            str(self.state_dir), str(self.tmp / "runs"), False, max_new=0)
        self.assertFalse(home.exists(), report)
        self.assertTrue(meta["removed"])
        self.assert_sources()

    def test_non_code_terminal_cleanup_preserves_outputs_and_retries_old_attempt(self):
        for kind, result in (("slurm", S.DONE), ("pipeline", S.PREEMPTED)):
            with self.subTest(kind=kind):
                attempt = self.attempt(kind, kind)
                (attempt / "out").write_text("keep output")
                home, _identity = CH.allocate(self.state_dir, attempt.name, attempt,
                                             {"kind": kind})
                CH.populate(home, self.auth)
                unit = {"id": kind, "kind": kind, "command": "true", "outputs": ["out"],
                        "max_attempts": 2}
                us = {"state": "SUBMITTED", "attempt_dir": str(attempt),
                      "attempts": [str(attempt)], "job_id": "fixture-job", "gpu_hours": 0,
                      "attempt_codex_homes": {attempt.name: self.home_record(home)}}
                state = {"units": {kind: us}}
                with mock.patch.object(S, "_check", return_value=(result, "", "")):
                    with mock.patch.object(CH, "remove", side_effect=CH.HomeError("fixture failure")):
                        S.advance({"units": [unit]}, state, str(self.state_dir), str(self.tmp / "runs"), False, max_new=0)
                    self.assertTrue(home.exists())
                    S.advance({"units": [unit]}, state, str(self.state_dir), str(self.tmp / "runs"), False, max_new=0)
                self.assertFalse(home.exists())
                self.assertEqual((attempt / "out").read_text(), "keep output")
        self.assert_sources()

    def test_key_never_reaches_child_or_coordinator_files(self):
        tools = self.tmp / "tools"
        path = closed_bin(tools)
        capture = self.tmp / "child.json"
        paseo = tools / "paseo"
        paseo.write_text("#!" + sys.executable + "\n" +
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "if args[0] == 'run':\n"
            f"    with open({str(capture)!r}, 'w') as f: json.dump({{'argv': args, 'key': os.environ.get('OPENAI_API_KEY')}}, f)\n"
            "    print(json.dumps({'agentId': 'fixture-agent', 'cwd': args[args.index('--cwd') + 1]}))\n"
            "else: print('[]')\n")
        paseo.chmod(0o755)
        captured = io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": path, "OPENAI_API_KEY": KEY}):
            with mock.patch.object(S.U, "run", side_effect=self.real_run):
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    _unit, _attempt, state = self.launch()
        self.assertIsNone(json.loads(capture.read_text())["key"])
        self.assertNotIn(KEY, captured.getvalue() + json.dumps(state))
        for directory in (self.state_dir, self.tmp / "runs"):
            for base, dirs, files in os.walk(directory, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
                for name in files:
                    file = Path(base) / name
                    if not file.is_symlink():
                        self.assertNotIn(KEY.encode(), file.read_bytes(), str(file))
        self.assertNotIn(KEY, capture.read_text())

    def test_doctor_reports_source_readiness_without_key(self):
        tools = self.tmp / "doctor-tools"
        path = closed_bin(tools, tools=("sh", "perl"))
        # The real doctor runs its bounded credential helper. An empty skill
        # prefix and stub Python socket probe keep this test strictly offline.
        python = tools / "python3"
        python.unlink()
        python.write_text("#!/bin/sh\ncase \"$*\" in *codex_home.py*) exec " +
                          str(sys.executable) + " \"$@\";; *) exit 0;; esac\n")
        python.chmod(0o755)
        env = dict(os.environ, HOME=str(self.tmp), PATH=path)
        for mode, expected in ((0o400, "READY --"), (0o600, "NOT READY --")):
            self.auth.chmod(mode)
            result = subprocess.run([str(F.ROOT / "bin" / "doctor"), "--prefix", str(self.tmp / "empty")],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Codex API source: " + expected, result.stdout)
            self.assertIn(str(self.auth), result.stdout)
            self.assertNotIn(KEY, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
