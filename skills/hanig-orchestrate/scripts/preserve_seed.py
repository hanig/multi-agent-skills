#!/usr/bin/env python3
"""Local recovery plumbing."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from merge_unit import S, CE, CP, Refusal, read_object, oid

PREFIX = b"RECOVERY, NOT READY:"
LOCATION_ENV = "GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_NAMESPACE GIT_COMMON_DIR GIT_PREFIX GIT_CEILING_DIRECTORIES".split()


def run(cmd, timeout=60, env=None):
  child = {k: v for k, v in CE.child_env().items() if k not in LOCATION_ENV}
  child.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
  child.update(env or {})
  with subprocess.Popen(cmd, env=child, stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as p:
    try:
      out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
      try:
        os.killpg(p.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      p.communicate()
      raise Refusal(f"retryable: command exceeded {timeout}s: {cmd}")
  return p.returncode, out, err


def identity_run(cmd, timeout=60):
  rc, out, err = run(cmd, timeout)
  return rc, os.fsdecode(out), os.fsdecode(err)


def git(work, *args, env=None, allowed=(0,), timeout=60):
  rc, out, err = run(["git", "-C", str(work), "-c", "core.splitIndex=false",
            *args], env=env, timeout=timeout)
  if rc not in allowed:
    raise Refusal(f"git {args[0]} exited {rc}: {os.fsdecode(err or out)}")
  return out


def source(args, plan):
  state = read_object(Path(args.state_dir) / S.STATE_FILE)
  S.validate_plan(plan)
  if state.get("plan_digest") != S.plan_digest(plan):
    raise Refusal("plan differs from coordinator digest")
  unit = next((u for u in plan["units"] if u["id"] == args.unit), None)
  if not unit or unit["kind"] != "code":
    raise Refusal("not a recorded code unit")
  us = state.get("units", {}).get(args.unit, {})
  attempt = args.attempt or Path(us.get("attempt_dir") or "").name
  if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v or "")
     for v in (args.unit, attempt)):
    raise Refusal("invalid unit/attempt")
  stop = (us.get("attempt_provider_stops") or {}).get(attempt) or {}
  if stop.get("phase") != "retained" or stop.get("retention_problem"):
    raise Refusal("no valid retained stop")
  launch = (us.get("attempt_launch_intents") or {}).get(attempt)
  problem = S._code_launch_intent_problem(launch, unit, attempt)
  facts = S.SS.trusted_launch_facts(state, args.unit, attempt)
  if problem or not facts:
    raise Refusal(problem or "invalid trusted launch facts")
  for key in "unit_id attempt_id repo repository_remote base_commit base_tree branch judgment_ref launch_host".split():
    if facts.get(key) != launch.get(key):
      raise Refusal("launch binding mismatch: " + key)
  work = facts["execution_workspace"]
  meta = (us.get("attempt_workspaces") or {}).get(attempt) or {}
  destination = f"refs/heads/recovery/{args.unit}-{attempt}"
  if (stop.get("worktree_path") != work or meta.get("path") != work
      or stop.get("branch") != facts["branch"]
      or stop.get("recovery_ref") != destination
      or not isinstance(stop.get("reason"), str) or not stop["reason"]):
    raise Refusal("retained worktree binding mismatch")
  root = state.get("root") or args.root
  if args.root and state.get("root") and Path(args.root).resolve() != Path(root).resolve():
    raise Refusal("--root conflicts with state")
  state_dir, _, _ = CP.resolve_paths(args.state_dir, root, plan=plan,
                   extra_repos=[launch["repo"], work], need_root=True)
  if any(CP._inside(state_dir, other["attempt_dir"])
     for other in state.get("units", {}).values() if other.get("attempt_dir")):
    raise Refusal("state directory inside worker attempt")
  return state_dir, us, attempt, stop, facts


def stopped(us, attempt, work):
  rc, out, err = run(["paseo", "ls", "--json"])
  if rc:
    raise Refusal("cannot list agents: " + os.fsdecode(err or out))
  agents = json.loads(out)
  if not isinstance(agents, list) or any(not isinstance(a, dict) for a in agents):
    raise Refusal("unreadable agent list")
  ids = set()
  if Path(us.get("attempt_dir") or "").name == attempt and us.get("job_id"):
    ids.add(str(us["job_id"]))
  for agent in agents:
    cwd = agent.get("cwd") or agent.get("Cwd")
    if cwd and str(Path(cwd).resolve()) == work:
      aid = agent.get("id") or agent.get("agentId")
      if not aid:
        raise Refusal("agent identity missing")
      ids.add(aid)
  if not ids:
    raise Refusal("cannot identify stopped agent")
  for aid in sorted(ids):
    rc, out, err = run(["paseo", "inspect", "--json", aid])
    agent = S.SW._paseo_json(os.fsdecode(out)) if not rc else None
    if not agent:
      raise Refusal("cannot inspect retained agent: " + os.fsdecode(err or out))
    if (agent.get("Cwd") or agent.get("cwd")) != work:
      raise Refusal("agent worktree mismatch")
    status = agent.get("Status") or agent.get("status")
    if (status not in S.U.PASEO_LIFECYCLE or S.U.PASEO_LIFECYCLE[status] == "RUNNING"
        or agent.get("PendingPermissions") or agent.get("pendingPermissions")):
      raise Refusal("agent running/unknown/permission-pending")


def tree_and_ignored(work, head, d, timeout=1800):
  env = {"GIT_INDEX_FILE": str(d / "index")}
  live = Path(work) / os.fsdecode(git(work, "rev-parse", "--git-path", "index"))[:-1]
  if live.exists():
    shutil.copyfile(live, env["GIT_INDEX_FILE"])
  else:
    git(work, "read-tree", head, env=env)
  git(work, "update-index", "--really-refresh", env=env, allowed=(0, 1), timeout=timeout)
  git(work, "add", "-A", env=env, timeout=timeout)
  tree = oid(git(work, "write-tree", env=env, timeout=timeout).decode("ascii").strip())
  raw = git(work, "diff-tree", "-r", "-z", "--no-renames",
       "--ignore-submodules=none", head + "^{tree}", tree, env=env)
  for header in raw.split(b"\0")[::2]:
    if b"160000" in header.lstrip(b":").split(b" ")[:2]:
      raise Refusal("changed 160000 gitlink")
  entries = iter(git(work, "status", "--porcelain=v1", "--untracked-files=normal",
           "--ignored=traditional", "-z", env=env).split(b"\0"))
  ignored = []
  for entry in entries:
    if entry.startswith(b"!! "):
      ignored.append(os.fsdecode(entry[3:]))
    elif b"R" in entry[:2] or b"C" in entry[:2]:
      next(entries, None)
  return tree, ignored


def message(unit, attempt, stop):
  return (f"RECOVERY, NOT READY: retained provider stop\n\nUnit: {unit}\n"
      f"Attempt: {attempt}\nStop reason: {stop['reason']}\n"
      f"Retained head: {stop['head_at_retention']}\nNo review ran. Must not be merged.\n").encode("utf-8")


def validate_commit(work, commit, tree, parent, body):
  if git(work, "cat-file", "-t", oid(commit)) != b"commit\n":
    raise Refusal("recovery object must be a commit")
  raw = git(work, "cat-file", "commit", oid(commit))
  headers, separator, actual = raw.partition(b"\n\n")
  rows = headers.split(b"\n")
  if (not separator or not actual.startswith(PREFIX) or actual != body
      or [r[5:] for r in rows if r.startswith(b"tree ")] != [tree.encode("ascii")]
      or [r[7:] for r in rows if r.startswith(b"parent ")] != [parent.encode("ascii")]):
    raise Refusal("recovery tree, parent or message differs")


def read_ref(work, ref):
  # for-each-ref omits dangling symrefs.
  target = os.fsdecode(git(work, "symbolic-ref", "-q", ref, allowed=(0, 1)))[:-1]
  if target:
    raise Refusal(f"symbolic private ref {ref} targets {target}")
  try:
    row = git(work, "for-each-ref", "--format=%(objectname) %(objecttype) %(symref)", ref)
    if not row:
      return ""
    commit, kind, symref = row.decode("ascii")[:-1].split(" ", 2)
    if kind != "commit" or symref:
      raise Refusal(f"target {symref or commit} is not a direct commit")
    git(work, "cat-file", "commit", oid(commit))
    return commit
  except Refusal as exc:
    raise Refusal(f"private ref {ref}: {exc}")


def set_private(work, ref, commit, old):
  git(work, "update-ref", "--no-deref", ref, commit, old)
  if read_ref(work, ref) != commit:
    raise Refusal(f"private ref {ref} does not target {commit}")


def prepare(args, plan):
  state_dir, us, attempt, stop, facts = source(args, plan)
  work, head = facts["execution_workspace"], oid(stop.get("head_at_retention"))
  problem = S.W.workspace_identity_problem(identity_run, facts)
  if problem:
    raise Refusal(problem)
  if git(work, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip() != head:
    raise Refusal("HEAD differs from head_at_retention")
  git(work, "merge-base", "--is-ancestor", facts["base_commit"], head)
  stopped(us, attempt, work)
  private = f"refs/hanig-recovery/{args.unit}/{attempt}"
  git(work, "check-ref-format", private)
  existing = read_ref(work, private)
  if existing:
    raise Refusal(f"private ref {private} already targets {existing}; retry not implemented")
  with tempfile.TemporaryDirectory(prefix=".recovery-index-", dir=str(state_dir)) as tmp:
    d = Path(tmp)
    tree, ignored = tree_and_ignored(work, head, d, args.git_timeout)
    result = dict(tree=tree, parent=head, private_ref=private, ignored=ignored)
    if not args.write:
      return dict(result, preview=True)
    body = message(args.unit, attempt, stop)
    msg = d / "message"
    msg.write_bytes(body)
    commit = oid(git(work, "-c", "i18n.commitEncoding=UTF-8", "commit-tree",
            tree, "-p", head, "-F", str(msg)).decode("ascii").strip())
    validate_commit(work, commit, tree, head, body)
  set_private(work, private, commit, "0" * 40)
  return dict(result, commit=commit)


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("command", choices=["prepare"])
  parser.add_argument("plan")
  parser.add_argument("--state-dir", required=True)
  parser.add_argument("--unit", required=True)
  parser.add_argument("--attempt")
  parser.add_argument("--root")
  parser.add_argument("--write", action="store_true")
  parser.add_argument("--git-timeout", type=int, default=1800,
            help="refresh/add/write-tree seconds (default: 1800)")
  args = parser.parse_args(argv)
  try:
    if args.git_timeout <= 0:
      raise Refusal("--git-timeout must be positive")
    plan = read_object(args.plan)
    source(args, plan)
    ok, holder = S.acquire_lease(args.state_dir, bump_epoch=False)
    if not ok:
      raise Refusal("coordinator lease unavailable: " + str(holder))
    try:
      result = prepare(args, plan)
    finally:
      S.release_lease(args.state_dir)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0
  except (OSError, ValueError, TypeError, KeyError, AttributeError,
      subprocess.SubprocessError, S.PlanError, CP.PathPolicyError) as exc:
    print("REFUSED: " + str(exc), file=sys.stderr)
    return 2


if __name__ == "__main__":
  sys.exit(main())
