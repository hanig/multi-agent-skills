"""Recovery CLI with real Git and a bare remote."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import unittest

from tests import test_attempt_worktrees as F

S, SS = F.S, F.S.SS
OPERATOR = F.ROOT / "skills/hanig-orchestrate/scripts/preserve_seed.py"


class TestPreserveSeed(unittest.TestCase):
    eq = unittest.TestCase.assertEqual

    def setUp(self):
        F.TestPerAttemptWorktrees.setUp(self)
        context = F.paseo_resolvable()
        self.bin = Path(context.__enter__())
        self.addCleanup(context.__exit__, None, None, None)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "HANIG_", "SWARM_"))}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        for name in ("edit", "deleted", "mode", "tracked.ignored"):
            (self.repo / name).write_text("base\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-qm", "base")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        self.sd = self.tmp / "state"
        self.root = self.tmp / "runs"
        self.attempt = self.root / "u" / "a1"
        self.attempt.mkdir(parents=True)
        unit = dict(F.code_unit(self.repo, "u"), outputs=["evidence.md"])
        self.plan = {"name": "seed-test", "units": [unit]}
        self.state = {"units": {}, "root": str(self.root), "plan_digest": S.plan_digest(self.plan)}
        S._capture_artifact_basis(self.state, "u", str(self.attempt), unit)
        job, error = S._submit(unit, str(self.attempt), False, self.state, str(self.sd))
        self.assertIsNone(error)
        self.us = self.state["units"]["u"]
        self.work = Path(self.us["attempt_launch_facts"]["a1"]["execution_workspace"])
        self.git(self.work, "push", "-q", "origin", "HEAD:refs/heads/swarm-a1")
        (self.work / "committed").write_text("unpushed\n")
        self.git(self.work, "add", "committed")
        self.git(self.work, "commit", "-qm", "unpushed")
        self.head = self.git(self.work, "rev-parse", "HEAD")
        self.record = {"phase": "retained", "branch": "swarm-a1", "reason": "content_filter",
            "worktree_path": str(self.work), "head_at_retention": self.head,
            "recovery_ref": "refs/heads/recovery/u-a1"}
        self.us.update(state="FAILED", attempt_dir=str(self.attempt), job_id=job,
                       attempt_provider_stops={"a1": self.record})
        (self.sd / SS.LOCK).touch()
        self.plan_path = self.tmp / "plan.json"
        self.save()
        self.receipt = self.sd / "recovery-seeds/u/a1/prepared.json"
        self.agent = self.tmp / "agent.json"
        self.agent.write_text(json.dumps({"status": "error", "pendingPermissions": []}))
        paseo = self.bin / "paseo"
        paseo.write_text("#!" + sys.executable + "\nimport pathlib, sys\n"
                         "assert sys.argv[1:] == ['inspect', '--json', 'agent-a1']\n"
                         "print(pathlib.Path(" + repr(str(self.agent)) + ").read_text())\n")
        paseo.chmod(0o755)

    def git(self, repo, *args):
        return subprocess.run([str(self.bin / "git"), "-C", str(repo)] + list(args),
                              env=self.env, capture_output=True, text=True,
                              check=True, timeout=30).stdout.strip()

    def save(self):
        self.plan_path.write_text(json.dumps(self.plan))
        (self.sd / SS.STATE_FILE).write_text(json.dumps(self.state))

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(OPERATOR), "prepare", str(self.plan_path),
            "--state-dir", str(self.sd), "--unit", "u"] + list(args), env=self.env,
            capture_output=True, text=True, timeout=60)

    def ok(self, *args):
        result = self.invoke(*args)
        self.eq(result.returncode, 0, result.stdout + result.stderr)
        return result

    def refused(self, reason, *args):
        result = self.invoke("--write", *args)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(reason, result.stderr)
        self.assertFalse(self.receipt.exists())
        self.eq(self.git(self.work, "rev-parse", "HEAD"), self.head)

    def edits(self):
        (self.work / "edit").write_text("edited\n")
        (self.work / "deleted").unlink()
        (self.work / "mode").chmod(0o755)
        (self.work / "new").write_text("new\n")
        (self.work / "link").symlink_to("edit")
        (self.work / ".gitignore").write_text("*.ignored\ncache/\n")
        nested = self.work / "cache" / "nested"
        nested.mkdir(parents=True)
        self.git(nested, "init", "-q")
        (self.work / "tracked.ignored").write_text("tracked change\n")
        (self.work / "secret.ignored").write_text("fixture only\n")

    def snapshot(self):
        result = {}
        for path in self.tmp.rglob("*"):
            if path.is_symlink():
                value = ("link", os.readlink(path))
            elif path.is_file():
                value = (path.stat().st_mode, path.read_bytes())
            else:
                value = ("directory", path.stat().st_mode)
            result[str(path.relative_to(self.tmp))] = value
        return result

    def test_material_branch_label_ignored_and_no_push(self):
        self.edits()
        remote = self.git(self.remote, "show-ref")
        state = (self.sd / SS.STATE_FILE).read_bytes()
        result = self.ok("--write")
        self.assertIn("'secret.ignored': omitted (ignored; kept only in the pre-removal snapshot)", result.stdout)
        for name, value in (("committed", "unpushed"), ("edit", "edited"),
                            ("new", "new"), ("tracked.ignored", "tracked change")):
            self.eq(self.git(self.work, "show", "HEAD:" + name), value)
        files = self.git(self.work, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        self.assertNotIn("deleted", files)
        self.assertNotIn("secret.ignored", files)
        self.assertNotIn("cache/nested", files)
        self.assertTrue(self.git(self.work, "ls-tree", "HEAD", "mode").startswith("100755"))
        self.assertTrue(self.git(self.work, "ls-tree", "HEAD", "link").startswith("120000"))
        self.eq(self.git(self.work, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.eq(self.git(self.repo, "rev-parse", "swarm-a1"), self.head)
        self.eq(self.git(self.remote, "show-ref"), remote)
        self.eq((self.sd / SS.STATE_FILE).read_bytes(), state)
        message = self.git(self.work, "log", "-1", "--format=%B")
        self.assertTrue(message.startswith("RECOVERY, NOT READY:"))
        for value in ("Unit: u", "Attempt: a1", "content_filter", self.head, "No review ran."):
            self.assertIn(value, message)

    def test_preview_identical(self):
        self.edits()
        before = self.snapshot()
        result = self.ok()
        self.assertIn("omitted (ignored; kept only in the pre-removal snapshot)", result.stdout)
        self.eq(self.snapshot(), before)

    def test_clean_label_receipt_and_retry(self):
        self.ok("--write")
        prepared = self.git(self.work, "rev-parse", "HEAD")
        self.assertNotEqual(prepared, self.head)
        self.eq(self.git(self.work, "rev-parse", "HEAD^"), self.head)
        self.assertTrue(self.git(self.work, "log", "-1", "--format=%B").startswith("RECOVERY, NOT READY:"))
        receipt = json.loads(self.receipt.read_text())
        digest = receipt.pop("publication_digest")
        self.eq(digest, hashlib.sha256(json.dumps(
            receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.eq(receipt["prepared_head"], prepared)
        self.eq(receipt["destination_ref"], "refs/heads/recovery/u-a1")
        before = self.snapshot()
        self.ok("--write")
        self.eq(self.snapshot(), before)
        (self.work / "new").write_text("late\n")
        result = self.invoke("--write")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("receipt disagrees", result.stderr)

    def test_authority_refusals(self):
        unrelated = self.git(self.repo, "commit-tree", "HEAD^{tree}", "-m", "unrelated")
        faults = [(["attempt_provider_stops"], {}),
                  (["attempt_launch_intents", "a1", "attempt_id"], "a2"),
                  (["attempt_provider_stops", "a1", "worktree_path"], str(self.repo)),
                  (["attempt_launch_facts", "a1", "attempt_id"], "a2"),
                  (["attempt_provider_stops", "a1", "head_at_retention"], self.base)]
        original = json.dumps(self.state)
        for keys, value in faults + [(None, unrelated)]:
            with self.subTest(keys=keys):
                if keys:
                    obj = self.us
                    for key in keys[:-1]:
                        obj = obj[key]
                    obj[keys[-1]] = value
                else:
                    for key in ("attempt_launch_intents", "attempt_launch_facts"):
                        self.us[key]["a1"]["base_commit"] = value
                self.save()
                self.refused("REFUSED:")
                self.state = json.loads(original)
                self.us = self.state["units"]["u"]
        self.save()
        self.refused("no retained", "--attempt", "a2")

    def test_running_and_permissions_refuse(self):
        for status, permissions in (("running", []), ("working", []), ("busy", []),
                                    ("error", ["permission"]), ("unknown", [])):
            self.agent.write_text(json.dumps({"status": status, "pendingPermissions": permissions}))
            self.refused("agent")

    def test_unsupported_content(self):
        nested = self.work / "nested"
        nested.mkdir()
        self.git(nested, "init", "-q")
        self.refused("nested")
        shutil.rmtree(nested)
        self.git(self.work, "update-index", "--add", "--cacheinfo", "160000," + self.head + ",module")
        self.refused("module")
        self.git(self.work, "reset", "-q", "HEAD", "--", "module")
        fifo = self.work / "pipe"
        os.mkfifo(fifo)
        self.refused("pipe")
        fifo.unlink()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.work / "socket"))
        self.refused("socket")

    def test_lease_and_child_containment(self):
        self.assertTrue(SS.acquire_lease(self.sd, bump_epoch=False)[0])
        try:
            self.refused("lock unavailable")
        finally:
            SS.release_lease(self.sd)
        script = self.bin / "paseo"
        body = script.read_text()
        script.write_text(body + "\nimport fcntl, os\n"
            "assert 'OPENAI_API_KEY' not in os.environ\n"
            "f = open(" + repr(str(self.sd / SS.LOCK)) + ", 'r')\n"
            "try:\n fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except BlockingIOError:\n pass\nelse:\n raise AssertionError('lease not held')\n")
        self.env["OPENAI_API_KEY"] = "synthetic-test-only"
        self.ok("--write")


if __name__ == "__main__":
    unittest.main()
