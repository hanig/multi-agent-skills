"""Recovery preparation through the operator, with real Git and a bare remote."""
import contextlib
import io
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
from tests.test_provider_resume import Harness

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
           "skills/hanig-orchestrate/scripts"))
import preserve_seed as P


class TestPreserveSeed(Harness):
  def setUp(self):
    super().setUp()
    self.root = self.tmp.resolve()
    self.sd = self.sd.resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
         GIT_OPTIONAL_LOCKS="0", OPENAI_API_KEY="synthetic-secret")
    patch = mock.patch.dict(os.environ, env, clear=True)
    patch.start()
    self.addCleanup(patch.stop)
    self.git("config", "user.name", "Fixture")
    self.git("config", "user.email", "fixture@example.invalid")
    (self.work / "tracked.txt").unlink()
    for name in ("edit", "delete", "mode", "kept.txt"):
      self.put(name, "base\n")
    self.git("add", "-A")
    self.git("commit", "-qm", "retained")
    self.base = self.git("rev-parse", "HEAD")
    self.state = P.S.load_state(self.sd)
    self.us = self.state["units"]["code"]
    P.S.SPS.record_stop(self.sd, self.state, "code", self.us, "content_filter", "fixture", [])
    self.stop = self.us["attempt_provider_stops"][self.attempt.name]
    self.receipt = self.sd / "recovery-seeds/code" / self.attempt.name / "prepared.json"
    self.ref = "refs/hanig-recovery/code/" + self.attempt.name
    self.agent = self.root / "agent.json"
    self.agent.write_text(json.dumps({"id": self.us["job_id"], "cwd": str(self.work),
                    "status": "error", "pendingPermissions": []}))
    os.environ["SEED_AGENT"] = str(self.agent)
    Path(shutil.which("paseo")).write_text(
      "#!" + sys.executable + "\nimport json, os, sys\n"
      "assert 'OPENAI_API_KEY' not in os.environ\n"
      "a=json.load(open(os.environ['SEED_AGENT']))\n"
      "print(json.dumps([a] if sys.argv[1]=='ls' else a))\n")

  def put(self, name, text):
    (self.work / name).write_text(text)

  def git(self, *args, at=None):
    p = subprocess.run(["git", "-C", str(at or self.work), *args],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    self.eq(p.returncode, 0, p.stderr)
    return os.fsdecode(p.stdout).strip()

  def save(self):
    (self.sd / P.S.STATE_FILE).write_text(json.dumps(self.state))

  def retain(self):
    self.stop["head_at_retention"] = self.git("rev-parse", "HEAD")
    self.save()

  def snapshot(self):
    return (self.git("rev-parse", "HEAD"), self.git("symbolic-ref", "HEAD"),
        self.git("status", "--porcelain=v1"),
        Path(self.git("rev-parse", "--git-path", "index")).read_bytes(),
        (self.sd / P.S.STATE_FILE).read_bytes(),
        self.git("show-ref", at=self.remote))

  def call(self, write=True, expected=0, extra=()):
    before = self.snapshot()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
      code = P.main(["prepare", str(self.planfile), "--state-dir", str(self.sd),
             "--unit", "code", *(["--write"] if write else []), *extra])
    self.eq(code, expected, err.getvalue())
    self.eq(before, self.snapshot())
    self.eq(list(self.sd.glob(".recovery-index-*")), [])
    self.absent("Traceback", err.getvalue())
    return json.loads(out.getvalue()) if code == 0 else err.getvalue()

  def test_content_and_preview(self):
    self.put("committed", "unpushed\n")
    self.git("add", "committed")
    self.git("commit", "-qm", "unpushed")
    self.retain()
    self.put("edit", "staged\n")
    self.git("add", "edit")
    self.put("edit", "working\n")
    (self.work / "delete").unlink()
    (self.work / "mode").chmod(0o755)
    self.put("new", "new\n")
    preview = self.call(write=False)
    self.assertFalse(self.receipt.exists())
    self.eq(self.git("for-each-ref", self.ref), "")
    result = self.call()
    self.eq(result["tree"], preview["tree"])
    for name, content in (("committed", "unpushed"), ("edit", "working"), ("new", "new")):
      self.eq(self.git("show", result["commit"] + ":" + name), content)
    listing = self.git("ls-tree", "-r", result["tree"])
    self.absent("\tdelete", listing)
    self.assertIn("100755 blob", listing)
    self.eq(self.git("rev-parse", result["commit"] + "^"), self.stop["head_at_retention"])

  def test_git_omissions_and_special_files(self):
    self.put(".gitignore", "*.txt\nign/\npartial/\nblocked/\nignored-pipe\n")
    (self.work / "partial").mkdir()
    self.put("partial/tracked", "tracked\n")
    self.git("add", "-f", "partial/tracked")
    self.git("commit", "-qm", "tracked ignored directory")
    self.retain()
    self.put("kept.txt", "still tracked\n")
    self.put("ignored.txt", "ignored\n")
    (self.work / "ign").mkdir()
    self.git("init", "-q", "ign/nested")
    os.mkfifo(self.work / "ign/pipe")
    os.mkfifo(self.work / "pipe")
    os.mkfifo(self.work / "ignored-pipe")
    blocked = self.work / "blocked/child"
    blocked.mkdir(parents=True)
    (blocked / "hidden").write_text("unreadable\n")
    blocked.chmod(0)
    try:
      result = self.call()
    finally:
      blocked.chmod(0o700)
    self.eq(set(result["ignored"]), {"ignored.txt", "ign/"})
    listing = self.git("ls-tree", "-r", "--name-only", result["tree"]).splitlines()
    self.eq(set(listing), {".gitignore", "edit", "delete", "mode", "kept.txt", "partial/tracked"})
    self.eq(self.git("show", result["commit"] + ":kept.txt"), "still tracked")

  def test_gitlinks(self):
    self.git("init", "-q", "nested")
    self.assertIn("does not have a commit checked out", self.call(expected=2))
    nested = self.work / "nested"
    self.git("config", "user.name", "Fixture", at=nested)
    self.git("config", "user.email", "fixture@example.invalid", at=nested)
    (nested / "file").write_text("one\n")
    self.git("add", "file", at=nested)
    self.git("commit", "-qm", "one", at=nested)
    with self.subTest(gitlink=self.git("rev-parse", "HEAD")):
      self.assertIn("160000", self.call(expected=2))
    self.git("add", "nested")
    self.git("commit", "-qm", "inherited")
    self.retain()
    self.call(write=False)
    self.git("config", "diff.ignoreSubmodules", "all")
    (nested / "file").write_text("two\n")
    self.git("commit", "-qam", "two", at=nested)
    with self.subTest(gitlink=self.git("rev-parse", "HEAD")):
      self.assertIn("160000", self.call(expected=2))
    shutil.rmtree(nested)
    with self.subTest(gitlink=self.git("rev-parse", "HEAD")):
      self.assertIn("160000", self.call(expected=2))

  def test_bare_fixture(self):
    for name in ("HEAD", "objects/pack/fixture", "refs/heads/main"):
      path = self.work / "fixture.git" / name
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text("ordinary content\n")
    self.git("add", "fixture.git")
    self.git("commit", "-qm", "bare fixture")
    self.retain()
    result = self.call()
    self.eq(self.git("show", result["commit"] + ":fixture.git/HEAD"), "ordinary content")

  def test_label_and_retry(self):
    self.git("config", "commit.cleanup", "strip")
    self.git("config", "core.commentChar", "R")
    first = self.call()
    raw = self.git("cat-file", "commit", first["commit"])
    self.assertIn("\n\nRECOVERY, NOT READY:", raw)
    self.assertIn("No review ran.", raw)
    self.assertNotEqual(first["commit"], self.base)
    self.eq(first["tree"], self.git("rev-parse", "HEAD^{tree}"))
    self.git("config", "i18n.logOutputEncoding", "UTF-16")
    with mock.patch.object(P.time, "gmtime", return_value=__import__("time").gmtime(1)):
      self.eq(self.call(), first)
    (self.repo / ".git/info/exclude").write_text("ignored\n")
    self.put("ignored", "new ignored bytes\n")
    self.eq(self.call(), first)
    self.put("edit", "drift\n")
    self.assertIn("differs", self.call(expected=2))

  def test_branch_include_rules(self):
    excludes = self.root / "excludes"
    excludes.write_text("draft\n")
    config = self.root / "branch-config"
    config.write_text("[core]\nexcludesFile = " + str(excludes) + "\n")
    self.git("config", "includeIf.onbranch:" + self.stop["branch"] + ".path", str(config))
    self.put("draft", "omit on retained branch\n")
    self.eq(self.call()["ignored"], ["draft"])

  def test_crash_recovery(self):
    with mock.patch.object(P, "durable_write", side_effect=OSError("simulated receipt failure")):
      self.assertIn("simulated receipt failure", self.call(expected=2))
    commit = self.git("rev-parse", self.ref)
    self.assertFalse(self.receipt.exists())
    self.eq(self.call()["commit"], commit)
    saved = json.loads(self.receipt.read_text())
    saved["tree"] = self.base
    self.receipt.write_text(json.dumps(saved))
    self.assertIn("digest", self.call(expected=2))

  def test_unset_identity_and_git_failure(self):
    self.git("config", "--unset", "user.name")
    self.git("config", "--unset", "user.email")
    self.git("config", "user.useConfigOnly", "true")
    self.assertIn("git -c exited", self.call(expected=2))
    self.eq(self.git("for-each-ref", self.ref), "")
    original = P.git
    def fail(work, *args, **kwargs):
      if args[0] == "write-tree":
        raise P.Refusal("simulated write-tree failure")
      return original(work, *args, **kwargs)
    with mock.patch.object(P, "git", side_effect=fail):
      self.assertIn("write-tree failure", self.call(expected=2))

  def test_bindings(self):
    with (self.sd / "lease.lock").open("w") as lock:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
      self.assertIn("lease", self.call(expected=2))
    initial = json.loads(self.agent.read_text())
    for changes in ({"status": "running"}, {"pendingPermissions": ["request"]}, {"status": "unknown"}):
      self.agent.write_text(json.dumps(dict(initial, **changes)))
      self.assertIn("agent", self.call(expected=2))
    self.agent.write_text(json.dumps(initial))
    self.call(expected=2, extra=("--attempt", "wrong"))
    for key, value in (("worktree_path", str(self.repo)), ("branch", "wrong"),
             ("head_at_retention", "0" * 40), ("phase", "released")):
      original = self.stop[key]
      self.stop[key] = value
      self.save()
      self.call(expected=2)
      self.stop[key] = original
    facts = self.us["attempt_launch_facts"][self.attempt.name]
    facts["attempt_id"] = "wrong"
    self.save()
    self.call(expected=2)

  def test_retry_finishes_sync(self):
    real, calls = P.os.fsync, []
    def sync(fd):
      calls.append(fd)
      if len(calls) == 3:
        raise OSError("interrupted directory sync")
      return real(fd)
    with mock.patch.object(P.os, "fsync", side_effect=sync):
      self.call(expected=2)
    self.assertTrue(self.receipt.exists())
    with mock.patch.object(P.os, "fsync", wraps=real) as retry:
      self.call()
    self.assertGreaterEqual(retry.call_count, 5)

  def test_durable_ancestors(self):
    seen = set()
    original = P.os.open
    def opened(path, flags, *args, **kwargs):
      seen.add(str(path))
      return original(path, flags, *args, **kwargs)
    with mock.patch.object(P.os, "open", side_effect=opened), mock.patch.object(
        P.os, "fsync", wraps=P.os.fsync) as sync:
      self.call()
    self.assertGreaterEqual(sync.call_count, 5)
    self.assertTrue({str(p) for p in (self.sd, self.sd / "recovery-seeds",
                    self.receipt.parent.parent, self.receipt.parent)} <= seen)


if __name__ == "__main__":
  unittest.main()
