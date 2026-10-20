"""Recovery operator with real Git and a bare remote."""
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
  has = unittest.TestCase.assertIn
  def setUp(t):
    super().setUp()
    t.w = t.work
    t.root = t.tmp.resolve()
    t.sd = t.sd.resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
      GIT_OPTIONAL_LOCKS="0", OPENAI_API_KEY="synthetic-secret")
    patch = mock.patch.dict(os.environ, env, clear=True)
    patch.start()
    t.addCleanup(patch.stop)
    t.g("config", "user.name", "Fixture")
    t.g("config", "user.email", "fixture@example.invalid")
    (t.w / "tracked.txt").unlink()
    for name in ("edit", "delete", "mode", "kept.txt"):
      t.put(name, "base\n")
    t.g("add", "-A")
    t.g("commit", "-qm", "retained")
    t.base = t.g("rev-parse", "HEAD")
    t.state = P.S.load_state(t.sd)
    t.us = t.state["units"]["code"]
    P.S.SPS.record_stop(t.sd, t.state, "code", t.us, "content_filter", "fixture", [])
    t.stop = t.us["attempt_provider_stops"][t.attempt.name]
    t.path = t.sd / "recovery-seeds/code" / t.attempt.name / "prepared.json"
    t.ref = "refs/hanig-recovery/code/" + t.attempt.name
    t.agent = t.root / "agent.json"
    t.agent.write_text(json.dumps({"id": t.us["job_id"], "cwd": str(t.w),
      "status": "error", "pendingPermissions": []}))
    os.environ["SEED_AGENT"] = str(t.agent)
    Path(shutil.which("paseo")).write_text(
      "#!" + sys.executable + "\nimport json, os, sys\n"
      "assert 'OPENAI_API_KEY' not in os.environ\n"
      "a=json.load(open(os.environ['SEED_AGENT']))\n"
      "print(json.dumps([a] if sys.argv[1]=='ls' else a))\n")

  def put(t, name, text):
    (t.w / name).write_text(text)

  def g(t, *args, at=None, raw=False):
    p = subprocess.run(["git", "-C", str(at or t.w), *args],
      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    t.eq(p.returncode, 0, p.stderr)
    return p.stdout if raw else os.fsdecode(p.stdout).strip()

  def save(t):
    (t.sd / P.S.STATE_FILE).write_text(json.dumps(t.state))

  def retain(t):
    t.stop["head_at_retention"] = t.g("rev-parse", "HEAD")
    t.save()

  def live(t):
    return (t.g("rev-parse", "HEAD"), t.g("symbolic-ref", "HEAD"),
      t.g("status", "--porcelain=v1", raw=True),
      Path(t.g("rev-parse", "--git-path", "index")).read_bytes(),
      (t.sd / P.S.STATE_FILE).read_bytes(),
      t.g("show-ref", at=t.remote))

  def call(t, write=True, rc=0, extra=()):
    before = t.live()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
      code = P.main(["prepare", str(t.planfile), "--state-dir", str(t.sd),
      "--unit", "code", *(["--write"] if write else []), *extra])
    t.eq(code, rc, err.getvalue())
    t.eq(before, t.live())
    t.eq(list(t.sd.glob(".recovery-index-*")), [])
    t.absent("Traceback", err.getvalue())
    t.assertFalse(t.path.exists())
    return json.loads(out.getvalue()) if code == 0 else err.getvalue()

  def test_content(t):
    for name in ("HEAD", "objects/pack/fixture", "refs/heads/main"):
      path = t.w / "fixture.git" / name
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text("ordinary content\n")
    t.g("add", "fixture.git")
    t.put("committed", "unpushed\n")
    t.g("add", "committed")
    t.g("commit", "-qm", "unpushed")
    t.retain()
    t.put("edit", "staged\n")
    t.g("add", "edit")
    t.put("edit", "working\n")
    (t.w / "delete").unlink()
    (t.w / "mode").chmod(0o755)
    t.put("new", "new\n")
    t.eq(t.live()[2][-1:], b"\n")
    preview = t.call(write=False)
    t.eq(t.g("for-each-ref", t.ref), "")
    seed = t.call()
    t.eq(seed["tree"], preview["tree"])
    for name, content in (("committed", "unpushed"), ("edit", "working"), ("new", "new"), ("fixture.git/HEAD", "ordinary content")):
      t.eq(t.g("show", seed["commit"] + ":" + name), content)
    listing = t.g("ls-tree", "-r", seed["tree"])
    t.absent("\tdelete", listing)
    t.has("100755 blob", listing)
    t.eq(t.g("rev-parse", seed["commit"] + "^"), t.stop["head_at_retention"])

  def test_omissions(t):
    t.put(".gitignore", "*.txt\nign/\npartial/\nblocked/\nignored-pipe\n")
    (t.w / "partial").mkdir()
    t.put("partial/tracked", "tracked\n")
    t.g("add", "-f", "partial/tracked")
    t.g("commit", "-qm", "tracked ignored directory")
    t.retain()
    t.put("kept.txt", "still tracked\n")
    t.put("ignored.txt", "ignored\n")
    t.put("staged.txt", "tracked\n")
    t.g("add", "-f", "staged.txt")
    (t.w / "ign").mkdir()
    t.g("init", "-q", "ign/nested")
    os.mkfifo(t.w / "ign/pipe")
    os.mkfifo(t.w / "pipe")
    os.mkfifo(t.w / "ignored-pipe")
    blocked = t.w / "blocked/child"
    blocked.mkdir(parents=True)
    (blocked / "hidden").write_text("unreadable\n")
    blocked.chmod(0)
    try:
      seed = t.call()
    finally:
      blocked.chmod(0o700)
    t.eq(set(seed["ignored"]), {"ignored.txt", "ign/"})
    listing = t.g("ls-tree", "-r", "--name-only", seed["tree"]).splitlines()
    t.eq(set(listing), {".gitignore", "edit", "delete", "mode", "kept.txt", "partial/tracked", "staged.txt"})
    t.eq(t.g("show", seed["commit"] + ":kept.txt"), "still tracked")
    t.eq(t.g("show", seed["commit"] + ":staged.txt"), "tracked")

  def test_tracked_fifo(t):
    for name in ("edit", "new"):
      with t.subTest(path=name):
        t.put(name, "tracked\n")
        t.g("add", name)
        (t.w / name).unlink()
        os.mkfifo(t.w / name)
        t.has("can only add regular files, symbolic links or git-directories",
          t.call(rc=2))
        t.eq(t.g("for-each-ref", t.ref), "")
        (t.w / name).unlink()
        t.put(name, "tracked\n")

  def test_index_flags(t):
    t.g("update-index", "--skip-worktree", "kept.txt")
    (t.w / "kept.txt").unlink()
    t.eq(t.g("show", t.call(False)["tree"] + ":kept.txt"), "base")
    t.g("config", "core.sparseCheckout", "true")
    patterns = Path(t.g("rev-parse", "--git-path", "info/sparse-checkout"))
    patterns.parent.mkdir(parents=True, exist_ok=True)
    patterns.write_text("edit\n")
    t.g("read-tree", "-mu", "HEAD")
    t.assertFalse((t.w / "kept.txt").exists())
    t.has("S kept.txt", t.g("ls-files", "-t"))
    t.g("update-index", "--assume-unchanged", "edit")
    t.put("edit", "modified\n")
    seed = t.call()
    t.eq(t.g("show", seed["commit"] + ":kept.txt"), "base")
    t.eq(t.g("show", seed["commit"] + ":edit"), "modified")

  def test_symrefs(t):
    for target in ("refs/replace/" + t.base, "refs/heads/" + t.stop["branch"]):
      with t.subTest(target=target):
        t.g("symbolic-ref", t.ref, target)
        error = t.call(rc=2)
        t.has(t.ref, error)
        t.has(target, error)
        t.eq(t.g("symbolic-ref", t.ref), target)
        t.g("symbolic-ref", "--delete", t.ref)

  def test_env(t):
    t.g("config", "--unset", "user.name")
    t.g("config", "--unset", "user.email")
    t.g("config", "user.useConfigOnly", "true")
    home = t.root / "synthetic-home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\nname=Global\nemail=global@example.invalid\n")
    with mock.patch.dict(os.environ, HOME=str(home), GIT_CONFIG_GLOBAL=os.devnull):
      t.has("git -c exited", t.call(rc=2))
      t.eq(t.g("for-each-ref", t.ref), "")
      with mock.patch.dict(os.environ, GIT_AUTHOR_NAME="Operator", GIT_AUTHOR_EMAIL="op@example.invalid",
        GIT_COMMITTER_NAME="Operator", GIT_COMMITTER_EMAIL="op@example.invalid"):
        seed = t.call()
    t.has("Operator <op@example.invalid>", t.g("cat-file", "commit", seed["commit"]))

  def test_replace(t):
    replacement = t.g("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "replacement")
    t.g("replace", t.base, replacement)
    seed = t.call()
    t.eq(seed["parent"], t.base)
    t.eq(seed["tree"], t.g("rev-parse", "HEAD^{tree}"))

  def test_timeout(t):
    real = subprocess.Popen
    def spawn(command, **kw):
      p = real(command, **kw)
      if "add" in command:
        communicate = p.communicate
        def expire(timeout=None):
          if timeout is not None:
            t.eq(timeout, 7)
            raise subprocess.TimeoutExpired(command, timeout)
          return communicate()
        p.communicate = expire
      return p
    with mock.patch.object(P.subprocess, "Popen", side_effect=spawn), mock.patch.object(
      P.os, "killpg", wraps=os.killpg) as kill:
      t.has("retryable", t.call(rc=2, extra=("--git-timeout", "7")))
    t.eq(kill.call_count, 1)

  def test_gitlinks(t):
    t.g("init", "-q", "nested")
    t.has("does not have a commit checked out", t.call(rc=2))
    nested = t.w / "nested"
    t.g("config", "user.name", "Fixture", at=nested)
    t.g("config", "user.email", "fixture@example.invalid", at=nested)
    (nested / "file").write_text("one\n")
    t.g("add", "file", at=nested)
    t.g("commit", "-qm", "one", at=nested)
    with t.subTest(gitlink=t.g("rev-parse", "HEAD")):
      t.has("160000", t.call(rc=2))
    t.g("add", "nested")
    t.g("commit", "-qm", "inherited")
    t.retain()
    t.call(write=False)
    t.g("config", "diff.ignoreSubmodules", "all")
    (nested / "file").write_text("two\n")
    t.g("commit", "-qam", "two", at=nested)
    with t.subTest(gitlink=t.g("rev-parse", "HEAD")):
      t.has("160000", t.call(rc=2))
    shutil.rmtree(nested)
    with t.subTest(gitlink=t.g("rev-parse", "HEAD")):
      t.has("160000", t.call(rc=2))

  def test_label(t):
    for key, value in (("commit.cleanup", "strip"), ("core.commentChar", "R"),
                       ("i18n.logOutputEncoding", "UTF-16")):
      t.g("config", key, value)
    seed = t.call()
    raw = t.g("cat-file", "commit", seed["commit"])
    t.has("\n\nRECOVERY, NOT READY:", raw)
    t.has("No review ran.", raw)
    t.assertNotEqual(seed["commit"], t.base)
    t.eq(seed["tree"], t.g("rev-parse", "HEAD^{tree}"))

  def test_branch(t):
    excludes = t.root / "excludes"
    excludes.write_text("draft\n")
    config = t.root / "branch-config"
    config.write_text("[core]\nexcludesFile = " + str(excludes) + "\n")
    t.g("config", "includeIf.onbranch:" + t.stop["branch"] + ".path", str(config))
    t.put("draft", "omit on retained branch\n")
    t.eq(t.call()["ignored"], ["draft"])

  def test_objects(t):
    t.g("tag", "-a", "tag", "-m", "tag")
    for target in (t.g("rev-parse", "tag"), t.g("rev-parse", "HEAD^{tree}")):
      with t.subTest(target=target):
        t.g("update-ref", t.ref, target)
        error = t.call(rc=2)
        for text in (t.ref, target, "not a direct commit"):
          t.has(text, error)
    t.g("update-ref", "-d", t.ref)
    path = Path(t.g("rev-parse", "--git-path", t.ref))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1" * 40 + "\n")
    error = t.call(rc=2)
    for text in (t.ref, "1" * 40):
      t.has(text, error)

  def test_git_failure(t):
    original = P.git
    def fail(work, *args, **kwargs):
      if args[0] == "write-tree":
        raise P.Refusal("simulated write-tree failure")
      return original(work, *args, **kwargs)
    with mock.patch.object(P, "git", side_effect=fail):
      t.has("write-tree failure", t.call(rc=2))

  def test_bindings(t):
    with (t.sd / "lease.lock").open("w") as lock:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
      t.has("lease", t.call(rc=2))
    initial = json.loads(t.agent.read_text())
    for changes in ({"status": "running"}, {"pendingPermissions": ["request"]}, {"status": "unknown"}):
      t.agent.write_text(json.dumps(dict(initial, **changes)))
      t.has("agent", t.call(rc=2))
    t.agent.write_text(json.dumps(initial))
    t.call(rc=2, extra=("--attempt", "wrong"))
    for key, value in (("worktree_path", str(t.repo)), ("branch", "wrong"),
      ("head_at_retention", "0" * 40), ("phase", "released")):
      original = t.stop[key]
      t.stop[key] = value
      t.save()
      t.call(rc=2)
      t.stop[key] = original
    facts = t.us["attempt_launch_facts"][t.attempt.name]
    facts["attempt_id"] = "wrong"
    t.save()
    t.call(rc=2)



if __name__ == "__main__":
  unittest.main()
