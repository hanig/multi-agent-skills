"""External coordinator paths and launch-time Git preflight."""

import hashlib
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


def fake_paseo_result(argv, root, agent="agent-123"):
    """Create the worktree that a mocked successful Paseo run promises."""
    source = argv[argv.index("--cwd") + 1]
    slug = argv[argv.index("--worktree-slug") + 1]
    branch = argv[argv.index("--new-branch") + 1]
    base = argv[argv.index("--base") + 1]
    workspace = Path(root) / slug
    git(source, "worktree", "add", "-q", "-b", branch, str(workspace), base)
    return 0, json.dumps({"agentId": agent, "cwd": str(workspace)}), ""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


def _state_with_seal(uid, attempt, seal, facts=None):
    """Coordinator state as it stands after a first dispatch."""
    us = {"attempt_record_seals": {Path(attempt).name: seal}}
    if facts:
        us["attempt_launch_facts"] = {Path(attempt).name: facts}
    return {"units": {uid: us}}

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
        # A dry run records the immutable intent but asks Paseo for no
        # workspace, so there is no invented execution path or audit record.
        us = saved["units"]["code"]
        self.assertIn(attempt.name, us["attempt_launch_intents"])
        self.assertNotIn("attempt_launch_facts", us)
        self.assertFalse((attempt.parent /
                          ("launch-%s.json" % attempt.name)).exists())
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

    def test_dirty_shared_checkout_does_not_block_code_attempt(self):
        repo = repo_at(self.tmp / "repo")
        (repo / "dirty.txt").write_text("x\n")
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        real = S.U.run
        launched = []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                launched.append(argv)
                return fake_paseo_result(argv, self.tmp / "managed")
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            job, err = S._submit(code_unit(repo), str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertEqual(job, "agent-123")
        self.assertIsNone(err)
        self.assertEqual(len(launched), 1)
        self.assertFalse((self.tmp / "managed" / "attempt" / "dirty.txt").exists())

    def test_a_later_shared_checkout_edit_does_not_move_the_trusted_base(self):
        repo = repo_at(self.tmp / "repo")
        unit = code_unit(repo)
        attempt = self.tmp / "runs" / "code" / "attempt"
        attempt.mkdir(parents=True)
        base = git(repo, "rev-parse", "HEAD").stdout.strip()
        real = S.U.run
        launched = []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                (repo / "appeared-during-launch.txt").write_text("dirty\n")
                launched.append(argv)
                return fake_paseo_result(argv, self.tmp / "managed")
            return real(argv, **kwargs)

        S.U.run = spy
        try:
            job, refusal = S._submit(unit, str(attempt), False, {})
        finally:
            S.U.run = real
        self.assertEqual(job, "agent-123")
        self.assertIsNone(refusal)
        self.assertEqual(launched[0][launched[0].index("--base") + 1], base)

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
                durable = state["units"]["code"]["attempt_launch_intents"]
                self.assertEqual(durable["attempt"]["base_commit"],
                                 argv[argv.index("--base") + 1])
                self.assertEqual(argv[argv.index("--cwd") + 1],
                                 str(repo.resolve()))
                launched.append(argv)
                return fake_paseo_result(argv, self.tmp / "managed")
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
        facts = state["units"]["code"]["attempt_launch_facts"]["attempt"]
        self.assertEqual(facts["execution_workspace"],
                         str((self.tmp / "managed" / "attempt").resolve()))
        self.assertTrue((attempt.parent / "launch-attempt.json").is_file())

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
    """Explicit preflight refusals remain distinct for guarded non-code jobs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

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
        def stub(u, unit_dir, dry_run, state=None, state_dir=None):
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
        def stub(u, unit_dir, dry_run, state=None, state_dir=None):
            anchor = Path(unit_dir).parent / f"launch-{Path(unit_dir).name}.json"
            anchor.write_text(json.dumps(
                {"preflight": {"status": "refused"},
                 "execution_workspace": "/checkout"}))
            return None, "the scheduler rejected the job"

        us, _report, dispatched = self._advance_with_submit(stub)
        self.assertEqual(dispatched, 0)
        self.assertEqual(us["state"], "FAILED")
        self.assertEqual(us.get("preflight_refusals"), None)


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
