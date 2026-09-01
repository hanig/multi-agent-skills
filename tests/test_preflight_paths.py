"""External coordinator paths and launch-time Git preflight."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
SWARM = SCRIPTS / "swarm.py"
sys.path.insert(0, str(SCRIPTS))
import coordinator_paths as CP  # noqa: E402
import swarm as S  # noqa: E402
import unit as U  # noqa: E402
import worktree as W  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo)] + list(args), check=check,
                          env=ENV, capture_output=True, text=True)


def repo_at(path):
    path = Path(path)
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "tracked.txt").write_text("base\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path


def code_unit(repo):
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return {"id": "code", "kind": "code", "repo": str(repo),
            "branch": branch, "mode": "bypass", "prompt": "work",
            "outputs": ["out.txt"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class TestExternalPathPolicy(Base):

    def test_defaults_are_external_and_stable_for_all_commands(self):
        repo = repo_at(self.tmp / "repo")
        plan = {"units": [code_unit(repo)]}
        state1, root1, worktrees = CP.resolve_paths(
            plan=plan, cwd=repo, need_root=True)
        state2, root2, _ = CP.resolve_paths(cwd=repo, need_root=True)
        self.assertEqual((state1, root1), (state2, root2))
        for path in (state1, root1):
            for worktree in worktrees:
                with self.assertRaises(ValueError):
                    path.relative_to(worktree)

    def test_symlink_into_an_operated_repo_is_rejected_before_a_write(self):
        repo = repo_at(self.tmp / "repo")
        target = repo / "coordinator-state"
        link = self.tmp / "looks-external"
        link.symlink_to(target)
        with self.assertRaises(CP.PathPolicyError):
            CP.resolve_paths(state_dir=link, plan={"units": [code_unit(repo)]},
                             cwd=self.tmp)
        self.assertFalse(target.exists())

    def test_cli_rejects_explicit_internal_state_and_root_before_writing(self):
        repo = repo_at(self.tmp / "repo")
        plan_path = repo / "plan.json"
        plan_path.write_text(json.dumps({"name": "p", "units": [{
            "id": "job", "kind": "slurm", "runtime": "none",
            "command": "true", "outputs": ["out"]}]}))
        git(repo, "add", "plan.json")
        git(repo, "commit", "-qm", "plan")
        outside = self.tmp / "outside-runs"
        inside_state = repo / "state"
        result = subprocess.run(
            [sys.executable, str(SWARM), "run", str(plan_path), "--dry-run",
             "--state-dir", str(inside_state), "--root", str(outside)],
            cwd=repo, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resolves inside operated Git worktree", result.stderr)
        self.assertFalse(inside_state.exists())
        self.assertFalse(outside.exists())

        outside_state = self.tmp / "outside-state"
        inside_root = repo / "runs"
        result = subprocess.run(
            [sys.executable, str(SWARM), "run", str(plan_path), "--dry-run",
             "--state-dir", str(outside_state), "--root", str(inside_root)],
            cwd=repo, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(outside_state.exists())
        self.assertFalse(inside_root.exists())

    def test_an_attached_worktree_is_also_protected(self):
        repo = repo_at(self.tmp / "repo")
        worktree = self.tmp / "worktree"
        git(repo, "worktree", "add", "-q", "-b", "other", str(worktree))
        with self.assertRaises(CP.PathPolicyError) as caught:
            CP.resolve_paths(root=worktree / "runs",
                             plan={"units": [code_unit(repo)]},
                             cwd=self.tmp, need_root=True)
        self.assertIn(str(worktree.resolve()), str(caught.exception))

    def test_every_declared_workspace_source_is_protected(self):
        repo = repo_at(self.tmp / "repo")
        units = [
            {"id": "execution", "kind": "pipeline",
             "execution_workspace": str(repo)},
            {"id": "policy", "kind": "slurm", "workspace_policy": {
                "requires_clean_git": True, "path": str(repo)}},
        ]
        for unit in units:
            with self.subTest(unit=unit["id"]):
                with self.assertRaises(CP.PathPolicyError):
                    CP.resolve_paths(state_dir=repo / (unit["id"] + "-state"),
                                     plan={"units": [unit]}, cwd=self.tmp)

    def test_policy_only_workspace_is_rejected_by_cli_before_lock_write(self):
        repo = repo_at(self.tmp / "repo")
        plan_path = self.tmp / "plan.json"
        plan_path.write_text(json.dumps({"name": "p", "units": [{
            "id": "job", "kind": "slurm", "runtime": "none",
            "command": "true", "outputs": ["out"],
            "workspace_policy": {"requires_clean_git": True,
                                 "path": str(repo)}}]}))
        state = repo / "state"
        result = subprocess.run(
            [sys.executable, str(SWARM), "run", str(plan_path), "--dry-run",
             "--state-dir", str(state), "--root", str(self.tmp / "runs")],
            cwd=self.tmp, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resolves inside operated Git worktree", result.stderr)
        self.assertFalse(state.exists())

    def test_legacy_state_is_copied_without_rewriting_or_deleting_facts(self):
        repo = repo_at(self.tmp / "repo")
        old_state = repo / ".swarm" / "state"
        old_runs = repo / ".swarm" / "runs"
        attempt = old_runs / "code" / "attempt-1"
        attempt.mkdir(parents=True)
        old_state.mkdir(parents=True)
        anchor = attempt.parent / "launch-attempt-1.json"
        anchor.write_text(json.dumps({"base_commit": "abc", "attempt":
                                      "attempt-1"}))
        state_record = {"schema_version": 1, "units": {"code": {
            "attempt_dir": str(attempt), "attempts": [str(attempt)],
            "job_id": "agent-123", "attempt_bases": {"attempt-1": "abc"}}}}
        (old_state / S.STATE_FILE).write_text(json.dumps(state_record))
        (old_state / S.OUTBOX).write_text('{"key":"k"}\n')
        (old_state / S.RECEIPTS).write_text(
            '{"key":"k","ref":"ARC-1"}\n')

        state, runs, _ = CP.resolve_paths(
            plan={"units": [code_unit(repo)]}, cwd=repo, need_root=True)
        migrated = CP.migrate_legacy_defaults(state, runs, cwd=repo)
        self.assertTrue(migrated["source_files_retained"])
        copied = json.loads((state / S.STATE_FILE).read_text())
        unit = copied["units"]["code"]
        self.assertEqual(unit["attempt_dir"], str(attempt))
        self.assertEqual(unit["job_id"], "agent-123")
        self.assertEqual(unit["attempt_bases"]["attempt-1"], "abc")
        self.assertTrue((runs / "code" / "launch-attempt-1.json").is_file())
        self.assertTrue(anchor.is_file())
        self.assertTrue((old_state / S.OUTBOX).is_file())
        self.assertTrue((old_state / S.RECEIPTS).is_file())

    def test_default_dry_lifecycle_leaves_git_status_byte_identical(self):
        repo = repo_at(self.tmp / "repo")
        plan_path = repo / "plan.json"
        plan_path.write_text(json.dumps({"name": "clean", "units": [
            code_unit(repo)]}))
        git(repo, "add", "plan.json")
        git(repo, "commit", "-qm", "plan")
        before = subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain", "-z"])
        env = dict(ENV, XDG_STATE_HOME=str(self.tmp / "state-home"))
        result = subprocess.run(
            [sys.executable, str(SWARM), "run", str(plan_path), "--dry-run"],
            cwd=repo, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for command in (["status", str(plan_path)], ["outbox", "--json"]):
            viewed = subprocess.run([sys.executable, str(SWARM)] + command,
                                    cwd=repo, env=env, capture_output=True,
                                    text=True)
            self.assertEqual(viewed.returncode, 0, viewed.stderr)
        after = subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain", "-z"])
        self.assertEqual(after, before)
        # The subprocess used a controlled XDG_STATE_HOME. Recompute under it.
        old = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = env["XDG_STATE_HOME"]
        try:
            state, runs, _ = CP.resolve_paths(
                plan={"units": [code_unit(repo)]}, cwd=repo, need_root=True)
        finally:
            if old is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old
        saved = json.loads((state / S.STATE_FILE).read_text())
        attempt = Path(saved["units"]["code"]["attempt_dir"])
        anchor = json.loads((attempt.parent /
                             ("launch-%s.json" % attempt.name)).read_text())
        self.assertEqual(anchor["preflight"]["status"], "passed")
        self.assertEqual(anchor["execution_workspace"], str(repo.resolve()))
        self.assertEqual(anchor["workspace_identity"]["path"],
                         str(repo.resolve()))
        state.relative_to(Path(env["XDG_STATE_HOME"]).resolve())
        runs.relative_to(Path(env["XDG_STATE_HOME"]).resolve())


class TestCanonicalDirtyPredicate(Base):

    def _dirty_repo(self, make_dirty):
        repo = repo_at(self.tmp / ("repo-%d" % len(list(self.tmp.iterdir()))))
        make_dirty(repo)
        rc, entries = W.repo_status(U.run, repo)
        self.assertEqual(rc, 0)
        self.assertTrue(entries)
        attempt = self.tmp / "runs" / repo.name / "attempt"
        attempt.mkdir(parents=True)
        err, base = S._write_launch_record(
            attempt, {"id": repo.name, "kind": "code", "repo": str(repo)})
        self.assertIn("preflight refused", err or "")
        self.assertIsNone(base)
        receipt = json.loads((attempt.parent /
                              "launch-attempt.json").read_text())
        self.assertEqual(receipt["preflight"]["status"], "refused")
        return entries, err

    def test_staged(self):
        def dirty(repo):
            (repo / "tracked.txt").write_text("staged\n")
            git(repo, "add", "tracked.txt")
        self._dirty_repo(dirty)

    def test_unstaged(self):
        self._dirty_repo(lambda repo:
                         (repo / "tracked.txt").write_text("unstaged\n"))

    def test_deleted(self):
        self._dirty_repo(lambda repo: (repo / "tracked.txt").unlink())

    def test_renamed(self):
        self._dirty_repo(lambda repo: git(repo, "mv", "tracked.txt",
                                          "renamed.txt"))

    def test_conflicted(self):
        def dirty(repo):
            base = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            git(repo, "checkout", "-qb", "side")
            (repo / "tracked.txt").write_text("side\n")
            git(repo, "commit", "-qam", "side")
            git(repo, "checkout", "-q", base)
            (repo / "tracked.txt").write_text("main\n")
            git(repo, "commit", "-qam", "main")
            git(repo, "merge", "side", check=False)
        entries, _ = self._dirty_repo(dirty)
        self.assertTrue(any(e["status"] == "UU" for e in entries))

    def test_dirty_submodule(self):
        def dirty(repo):
            child = repo.parent / (repo.name + "-child")
            repo_at(child)
            subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "-C", str(repo),
                 "submodule", "add", "-q", str(child), "sub"], check=True,
                env=ENV, capture_output=True, text=True)
            git(repo, "commit", "-qam", "submodule")
            (repo / "sub" / "tracked.txt").write_text("dirty submodule\n")
        self._dirty_repo(dirty)

    def test_untracked_newline_path_is_one_safely_escaped_entry(self):
        entries, err = self._dirty_repo(
            lambda repo: (repo / "line\nbreak.txt").write_text("x\n"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], "line\nbreak.txt")
        self.assertIn('"line\\nbreak.txt"', err)


class TestPreflightIsTheLaunchChokepoint(Base):

    def test_dirty_code_never_calls_paseo(self):
        repo = repo_at(self.tmp / "repo")
        (repo / "dirty.txt").write_text("x\n")
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        real = S.U.run
        launched = []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                launched.append(argv)
                return 0, '{"agentId":"should-not-start"}', ""
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            job, err = S._submit(code_unit(repo), str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertIsNone(job)
        self.assertIn("dirty.txt", err)
        self.assertEqual(launched, [])

    def test_recovery_reruns_preflight_without_moving_the_anchor(self):
        repo = repo_at(self.tmp / "repo")
        unit = code_unit(repo)
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        err, base = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(err)
        anchor_path = attempt.parent / "launch-attempt.json"
        anchored = anchor_path.read_bytes()
        (repo / "appeared-during-recovery.txt").write_text("dirty\n")
        real = S.U.run
        launched = []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                launched.append(argv)
                return 0, '{"agentId":"must-not-start"}', ""
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            job, refusal = S._submit(unit, str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertIsNone(job)
        self.assertIn("appeared-during-recovery.txt", refusal)
        self.assertEqual(launched, [])
        self.assertEqual(anchor_path.read_bytes(), anchored)
        self.assertTrue(base)

    def test_direct_code_submit_without_workspace_fails_closed(self):
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        unit = {"id": "code", "kind": "code", "prompt": "work"}
        real = S.U.run
        launched = []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                launched.append(argv)
                return 0, '{"agentId":"must-not-start"}', ""
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            job, refusal = S._submit(unit, str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertIsNone(job)
        self.assertIn("requires a declared Git execution workspace", refusal)
        self.assertEqual(launched, [])
        self.assertFalse((attempt.parent / "launch-attempt.json").exists())

    def test_clean_code_dispatches_and_anchors_before_paseo(self):
        repo = repo_at(self.tmp / "repo")
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        real = S.U.run
        state, launched = {"units": {}}, []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                receipt = attempt.parent / "launch-attempt.json"
                self.assertTrue(receipt.is_file())
                self.assertEqual(argv[argv.index("--cwd") + 1],
                                 str(repo.resolve()))
                launched.append(argv)
                return 0, '{"agentId":"agent-123"}', ""
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            job, err = S._submit(code_unit(repo), str(attempt), False, state)
        finally:
            S.U.run = real
        self.assertIsNone(err)
        self.assertEqual(job, "agent-123")
        self.assertEqual(len(launched), 1)
        self.assertTrue(state["units"]["code"]["attempt_bases"]["attempt"])

    def test_dirty_refusal_is_durable_but_not_a_started_attempt(self):
        repo = repo_at(self.tmp / "repo")
        (repo / "dirty.txt").write_text("x\n")
        unit = code_unit(repo)
        plan = {"name": "p", "units": [unit]}
        state = {"schema_version": 1, "units": {}, "halted": None}
        state_dir, runs = self.tmp / "state", self.tmp / "runs"
        real = S.U.run
        launched = []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                launched.append(argv)
                return 0, '{"agentId":"must-not-start"}', ""
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            locked, why = S.acquire_lease(str(state_dir))
            self.assertTrue(locked, why)
            report, dispatched, _halted = S.advance(
                plan, state, str(state_dir), str(runs), False)
        finally:
            S.release_lease(str(state_dir))
            S.U.run = real
        us = state["units"]["code"]
        self.assertEqual(dispatched, 0)
        self.assertEqual(launched, [])
        self.assertEqual(us["state"], "PREFLIGHT_REFUSED")
        self.assertIsNone(us["attempt_dir"])
        self.assertEqual(us["attempts"], [])
        self.assertIsNone(us.get("job_id"))
        self.assertEqual(us["gpu_hours"], 0.0)
        receipt = Path(us["preflight_refusals"][0]["receipt"])
        self.assertTrue(receipt.is_file())
        self.assertIn(str(receipt), "\n".join(report))
        persisted = json.loads((state_dir / S.STATE_FILE).read_text())
        self.assertEqual(persisted["units"]["code"]["attempts"], [])

    def test_non_code_is_checked_only_when_policy_requires_it(self):
        repo = repo_at(self.tmp / "repo")
        (repo / "dirty.txt").write_text("x\n")
        clean_policy = {"requires_clean_git": True, "path": str(repo)}
        unit = {"id": "job", "kind": "slurm", "command": "true",
                "workspace_policy": clean_policy}
        attempt = self.tmp / "runs" / "job" / "attempt"
        attempt.mkdir(parents=True)
        real = S.U.run
        sbatch = []

        def spy(argv, **kwargs):
            if argv and argv[0] == "sbatch":
                sbatch.append(argv)
                return 0, "123", ""
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            _job, err = S._submit(unit, str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertIn("preflight refused", err or "")
        self.assertEqual(sbatch, [])

        unguarded = dict(unit)
        unguarded.pop("workspace_policy")
        attempt2 = self.tmp / "runs" / "job" / "attempt2"
        attempt2.mkdir()
        S.U.run = spy
        try:
            job, err = S._submit(unguarded, str(attempt2), False, {})
        finally:
            S.U.run = real
        self.assertIsNone(err)
        self.assertEqual(job, "123")
        self.assertEqual(len(sbatch), 1)


class TestRefusalIsCarriedNotRederived(unittest.TestCase):
    """The re-dispatch trap: the record says "passed", the workspace is dirty.

    `advance` classified a failed submit by reading `preflight.status` back
    off disk. On a re-dispatch that field records the earlier PASS, so the
    refusal read as a generic failure. Only one caller allocates a fresh
    random attempt id today, so the misclassification is not reachable
    through `advance` yet; these pin the classification itself so the first
    recovery path that re-submits an attempt does not make it live.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_dirty_workspace_refusal_is_typed(self):
        repo = repo_at(self.tmp / "repo")
        (repo / "dirty.txt").write_text("x\n")
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        job, err = S._submit(code_unit(repo), str(attempt), False, {})
        self.assertIsNone(job)
        self.assertIsInstance(err, S.PreflightRefusal)
        self.assertEqual(err.workspace, str(Path(repo).resolve()))
        self.assertEqual(err.dirty_count, 1)

    def test_redispatch_refusal_is_typed_though_the_record_says_passed(self):
        repo = repo_at(self.tmp / "repo")
        unit = code_unit(repo)
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        anchor_err, _base = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(anchor_err)
        anchor = attempt.parent / "launch-attempt.json"
        self.assertEqual(
            json.loads(anchor.read_text())["preflight"]["status"], "passed")

        (repo / "appeared-later.txt").write_text("dirty\n")
        real = S.U.run
        launched = []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                launched.append(argv)
                return 0, '{"agentId":"must-not-start"}', ""
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            job, err = S._submit(unit, str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertIsNone(job)
        self.assertEqual(launched, [])
        # The disk still says the opposite; the type carries the truth.
        self.assertEqual(
            json.loads(anchor.read_text())["preflight"]["status"], "passed")
        self.assertIsInstance(err, S.PreflightRefusal)
        self.assertIn("appeared-later.txt", err)

    def test_a_shape_error_is_not_a_preflight_refusal(self):
        # Restoring the retry budget is for an attempt that never started
        # because the workspace was dirty, not for every failed submit.
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        unit = {"id": "code", "kind": "code", "prompt": "work"}
        job, err = S._submit(unit, str(attempt), False, {})
        self.assertIsNone(job)
        self.assertTrue(err)
        self.assertNotIsInstance(err, S.PreflightRefusal)

    def _advance_with_submit(self, stub):
        repo = repo_at(self.tmp / "repo")
        unit = code_unit(repo)
        plan = {"name": "p", "units": [unit]}
        state = {"schema_version": 1, "units": {}, "halted": None}
        state_dir, runs = self.tmp / "state", self.tmp / "runs"
        real_submit = S._submit
        S._submit = stub
        try:
            locked, why = S.acquire_lease(str(state_dir))
            self.assertTrue(locked, why)
            report, dispatched, _halted = S.advance(
                plan, state, str(state_dir), str(runs), False)
        finally:
            S.release_lease(str(state_dir))
            S._submit = real_submit
        return state["units"]["code"], report, dispatched

    def test_advance_believes_the_observation_over_the_record(self):
        # No launch record on disk at all: the old classifier read nothing,
        # found no "refused", and charged the unit as a plain failure.
        def stub(u, unit_dir, dry_run, state=None):
            refusal = S.PreflightRefusal("dirty at dispatch")
            refusal.workspace = "/checkout"
            refusal.dirty_count = 3
            return None, refusal

        us, report, dispatched = self._advance_with_submit(stub)
        self.assertEqual(dispatched, 0)
        self.assertEqual(us["state"], "PREFLIGHT_REFUSED")
        self.assertIsNone(us["attempt_dir"])
        self.assertEqual(us["attempts"], [])
        self.assertEqual(us["gpu_hours"], 0.0)
        entry = us["preflight_refusals"][0]
        self.assertEqual(entry["workspace"], "/checkout")
        self.assertEqual(entry["dirty_path_count"], 3)
        # Nothing on disk says "refused", so nothing is cited as the receipt.
        self.assertIsNone(entry["receipt"])
        self.assertNotIn("receipt:", "\n".join(report))

    def test_a_record_saying_refused_does_not_make_a_failure_a_refusal(self):
        # The mutation in the other direction: authority is the observation,
        # so a launch record alone must not restore a retry budget.
        def stub(u, unit_dir, dry_run, state=None):
            anchor = Path(unit_dir).parent / f"launch-{Path(unit_dir).name}.json"
            anchor.write_text(json.dumps(
                {"preflight": {"status": "refused"},
                 "execution_workspace": "/checkout"}))
            return None, "the scheduler rejected the job"

        us, _report, dispatched = self._advance_with_submit(stub)
        self.assertEqual(dispatched, 0)
        self.assertEqual(us["state"], "FAILED")
        self.assertEqual(us.get("preflight_refusals"), None)


class TestTheRecordIsNotTrustedForDispatch(unittest.TestCase):
    """The launch record is agent-writable; the plan is not.

    `SWARM_UNIT_DIR` is handed to a code agent running with write
    permissions, and the record sits one level above it. Reading
    `execution_workspace` back out of it and passing it to `paseo --cwd` let
    the agent pick its own working tree while `preflight.status` still said
    "passed" for the tree the coordinator actually checked.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _spy(self):
        real, launched = S.U.run, []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                launched.append(argv)
                return 0, '{"agentId":"11111111-2222-3333-4444-555555555555"}', ""
            return real(argv, **kwargs)
        return real, spy, launched

    def test_an_edited_workspace_is_refused_not_obeyed(self):
        good = repo_at(self.tmp / "good")
        evil = repo_at(self.tmp / "evil")
        unit = code_unit(good)
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        err, _base = S._write_launch_record(str(attempt), unit)
        self.assertIsNone(err)

        anchor = W.launch_record_path(str(attempt))
        rec = json.loads(anchor.read_text())
        rec["execution_workspace"] = str(evil)      # the agent's edit
        anchor.write_text(json.dumps(rec))

        real, spy, launched = self._spy()
        S.U.run = spy
        try:
            job, refusal = S._submit(unit, str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertIsNone(job)
        self.assertEqual(launched, [])
        self.assertIn(str(evil), refusal)
        self.assertIn("writable by the agent", refusal)

    def test_a_clean_untouched_attempt_still_dispatches(self):
        # The other direction: the check must not refuse an honest launch.
        good = repo_at(self.tmp / "good")
        unit = code_unit(good)
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        real, spy, launched = self._spy()
        S.U.run = spy
        try:
            job, err = S._submit(unit, str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertIsNone(err)
        self.assertEqual(len(launched), 1)
        cwd = launched[0][launched[0].index("--cwd") + 1]
        self.assertEqual(cwd, str(Path(good).resolve()))

    def test_a_cleaned_workspace_is_no_longer_refused_by_an_old_record(self):
        # _existing_preflight_refusal refused on a stale recorded refusal even
        # after the workspace was cleaned. The current predicate decides.
        repo = repo_at(self.tmp / "repo")
        (repo / "dirty.txt").write_text("x\n")
        unit = code_unit(repo)
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        job, refusal = S._submit(unit, str(attempt), False, {})
        self.assertIsNone(job)
        self.assertIsInstance(refusal, S.PreflightRefusal)

        (repo / "dirty.txt").unlink()               # cleaned
        real, spy, launched = self._spy()
        S.U.run = spy
        try:
            job, err = S._submit(unit, str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertIsNone(err, "a cleaned workspace must dispatch")
        self.assertEqual(len(launched), 1)


class TestARefusedAttemptCannotBeBound(unittest.TestCase):
    """Round 2 refuted this on 'no recovery scan exists'. That was wrong.

    There is no scan, but `unit.py bind <dir> --job-id` is a documented
    command that takes a directory path, and the refused directory is still
    on disk with its spec intact.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _attempt(self, refused):
        att = self.tmp / "runs" / "u" / "abc123"
        att.mkdir(parents=True)
        (att / "unit.json").write_text(json.dumps(
            {"schema_version": 1, "task_id": "u", "attempt_id": "abc123",
             "kind": "code", "declared_outputs": ["x"], "job_id": None}))
        status = "refused" if refused else "passed"
        W.launch_record_path(str(att)).write_text(json.dumps(
            {"preflight": {"status": status, "workspace": "/checkout"}}))
        return att

    def _bind(self, att, job_id):
        return S.U.run([sys.executable, str(SCRIPTS / "unit.py"), "bind",
                        str(att), "--job-id", job_id], timeout=60)

    def test_bind_refuses_a_refused_attempt(self):
        att = self._attempt(refused=True)
        rc, out, err = self._bind(att, "11111111-2222-3333-4444-555555555555")
        self.assertNotEqual(rc, 0)
        self.assertIn("REFUSED at launch preflight", (err or out))
        spec = json.loads((att / "unit.json").read_text())
        self.assertIsNone(spec["job_id"])

    def test_bind_still_works_for_a_permitted_attempt(self):
        att = self._attempt(refused=False)
        rc, out, err = self._bind(att, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(rc, 0, (err or out))
        spec = json.loads((att / "unit.json").read_text())
        self.assertEqual(spec["job_id"],
                         "11111111-2222-3333-4444-555555555555")


class TestContainmentCoversAWorkspaceThatDoesNotExistYet(unittest.TestCase):
    """A declared workspace need not exist when the command runs.

    git cannot be asked about an absent path, so the path contributed nothing
    to the containment set and state was allowed inside the very repository
    the workspace would be created in. The later preflight does reject the
    missing workspace, but only after the state directory exists.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_state_inside_the_repo_is_refused_for_an_absent_workspace(self):
        repo = repo_at(self.tmp / "repo")
        plan = {"units": [{"workspace_policy":
                           {"requires_clean_git": True,
                            "path": str(repo / "not-yet-created")}}]}
        outside = self.tmp / "outside"
        outside.mkdir()
        with self.assertRaises(CP.PathPolicyError) as cm:
            CP.resolve_paths(state_dir=str(repo / "state"), plan=plan,
                             cwd=str(outside))
        self.assertIn("operated Git worktree", str(cm.exception))
        self.assertFalse((repo / "state").exists())

    def test_an_absent_workspace_outside_any_repo_is_still_allowed(self):
        # The other direction: nothing legitimate is refused.
        plain = self.tmp / "plain"
        plain.mkdir()
        plan = {"units": [{"workspace_policy":
                           {"requires_clean_git": True,
                            "path": str(plain / "later")}}]}
        state, _runs, _wt = CP.resolve_paths(
            state_dir=str(self.tmp / "state"), plan=plan, cwd=str(plain))
        self.assertEqual(state, Path(self.tmp / "state").resolve())


if __name__ == "__main__":
    unittest.main()
