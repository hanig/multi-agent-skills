#!/usr/bin/env python3
"""Prepare a local recovery commit; publication and release are separate work."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from merge_unit import S, CE, CP, Refusal, read_object, oid, durable_write as write_leaf

PREFIX = b"RECOVERY, NOT READY:"


def run(command, timeout=60, env=None):
  child = {k: v for k, v in CE.child_env().items() if not k.startswith("GIT_")}
  child.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
  child.update(env or {})
  p = subprocess.run(command, env=child, stdin=subprocess.DEVNULL,
           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
  return p.returncode, p.stdout, p.stderr


def identity_runner(command, timeout=60):
  rc, out, err = run(command, timeout)
  return rc, os.fsdecode(out), os.fsdecode(err)


def git(work, *args, env=None, allowed=(0,)):
  rc, out, err = run(["git", "-C", str(work), "-c", "core.splitIndex=false",
            *args], env=env)
  if rc not in allowed:
    raise Refusal("git {} exited {}: {}".format(args[0], rc, os.fsdecode(err or out)))
  return out


def source(args, plan):
  state = read_object(Path(args.state_dir) / S.STATE_FILE)
  S.validate_plan(plan)
  if state.get("plan_digest") != S.plan_digest(plan):
    raise Refusal("plan differs from coordinator digest")
  unit = next((u for u in plan["units"] if u["id"] == args.unit), None)
  if not unit or unit["kind"] != "code":
    raise Refusal("unit must name a recorded code unit")
  us = state.get("units", {}).get(args.unit, {})
  attempt = args.attempt or Path(us.get("attempt_dir") or "").name
  if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v or "")
     for v in (args.unit, attempt)):
    raise Refusal("invalid unit or attempt name")
  stop = (us.get("attempt_provider_stops") or {}).get(attempt) or {}
  if stop.get("phase") != "retained" or stop.get("retention_problem"):
    raise Refusal("no valid retained provider stop for this attempt")
  launch = (us.get("attempt_launch_intents") or {}).get(attempt)
  problem = S._code_launch_intent_problem(launch, unit, attempt)
  facts = S.SS.trusted_launch_facts(state, args.unit, attempt)
  if problem or not facts:
    raise Refusal(problem or "missing or inconsistent trusted launch facts")
  for key in ("unit_id", "attempt_id", "repo", "repository_remote", "base_commit",
        "base_tree", "branch", "judgment_ref", "launch_host"):
    if facts.get(key) != launch.get(key):
      raise Refusal("launch facts disagree with launch intent: " + key)
  work = facts["execution_workspace"]
  meta = (us.get("attempt_workspaces") or {}).get(attempt) or {}
  destination = "refs/heads/recovery/{}-{}".format(args.unit, attempt)
  if (stop.get("worktree_path") != work or meta.get("path") != work
      or stop.get("branch") != facts["branch"]
      or stop.get("recovery_ref") != destination
      or not isinstance(stop.get("reason"), str) or not stop["reason"]):
    raise Refusal("retained record disagrees with attempt worktree binding")
  root = state.get("root") or args.root
  if args.root and state.get("root") and Path(args.root).resolve() != Path(root).resolve():
    raise Refusal("--root conflicts with coordinator root")
  state_dir, _, _ = CP.resolve_paths(args.state_dir, root, plan=plan,
                   extra_repos=[launch["repo"], work], need_root=True)
  if any(CP._inside(state_dir, other["attempt_dir"])
     for other in state.get("units", {}).values() if other.get("attempt_dir")):
    raise Refusal("state directory is inside a worker attempt")
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
        raise Refusal("workspace agent has no identity")
      ids.add(aid)
  if not ids:
    raise Refusal("cannot identify retained attempt's stopped agent")
  for aid in sorted(ids):
    rc, out, err = run(["paseo", "inspect", "--json", aid])
    agent = S.SW._paseo_json(os.fsdecode(out)) if not rc else None
    if not agent:
      raise Refusal("cannot inspect retained agent: " + os.fsdecode(err or out))
    if (agent.get("Cwd") or agent.get("cwd")) != work:
      raise Refusal("agent worktree disagrees with retained attempt")
    status = agent.get("Status") or agent.get("status")
    if (status not in S.U.PASEO_LIFECYCLE or S.U.PASEO_LIFECYCLE[status] == "RUNNING"
        or agent.get("PendingPermissions") or agent.get("pendingPermissions")):
      raise Refusal("agent is running, unknown, or has pending permissions")


def tree_and_ignored(work, head, directory):
  env = {"GIT_INDEX_FILE": str(directory / "index")}
  git(work, "read-tree", head, env=env)
  git(work, "add", "-A", env=env)
  tree = oid(git(work, "write-tree", env=env).decode("ascii").strip())
  raw = git(work, "diff-tree", "-r", "-z", "--no-renames",
       "--ignore-submodules=none", head + "^{tree}", tree, env=env)
  for header in raw.split(b"\0")[::2]:
    if b"160000" in header.lstrip(b":").split(b" ")[:2]:
      raise Refusal("changed 160000 gitlink cannot be preserved as parent-tree content")
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
  return ("RECOVERY, NOT READY: retained provider stop\n\n"
      "Unit: {}\nAttempt: {}\nStop reason: {}\nRetained head: {}\n"
      "No review ran. Must not be merged.\n".format(
        unit, attempt, stop["reason"], stop["head_at_retention"])).encode("utf-8")


def validate_commit(work, commit, tree, parent, body):
  raw = git(work, "cat-file", "commit", oid(commit))
  headers, separator, actual = raw.partition(b"\n\n")
  rows = headers.split(b"\n")
  if (not separator or not actual.startswith(PREFIX) or actual != body
      or [r[5:] for r in rows if r.startswith(b"tree ")] != [tree.encode("ascii")]
      or [r[7:] for r in rows if r.startswith(b"parent ")] != [parent.encode("ascii")]):
    raise Refusal("recovery commit has different tree, parent or recovery message")


def digest(value):
  return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"))
             .encode("utf-8")).hexdigest()


def durable_write(path, value, state_dir):
  path.parent.mkdir(parents=True, exist_ok=True)
  write_leaf(path, value)
  current = path.parent.parent
  while True:
    descriptor = os.open(str(current), os.O_RDONLY)
    try:
      os.fsync(descriptor)
    finally:
      os.close(descriptor)
    if current == state_dir:
      break
    current = current.parent


def prepare(args, plan):
  state_dir, us, attempt, stop, facts = source(args, plan)
  work, head = facts["execution_workspace"], oid(stop.get("head_at_retention"))
  problem = S.W.workspace_identity_problem(identity_runner, facts)
  if problem:
    raise Refusal(problem)
  if git(work, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip() != head:
    raise Refusal("HEAD differs from head_at_retention")
  git(work, "merge-base", "--is-ancestor", facts["base_commit"], head)
  stopped(us, attempt, work)
  private = "refs/hanig-recovery/{}/{}".format(args.unit, attempt)
  git(work, "check-ref-format", private)
  path = state_dir / "recovery-seeds" / args.unit / attempt / "prepared.json"
  with tempfile.TemporaryDirectory(prefix=".recovery-index-", dir=str(state_dir)) as tmp:
    directory = Path(tmp)
    tree, ignored = tree_and_ignored(work, head, directory)
    result = {"schema_version": 1, "unit": args.unit, "attempt": attempt,
         "retained_head": head, "tree": tree, "parent": head,
         "private_ref": private, "destination": stop["recovery_ref"],
         "ignored": ignored, "stop_reason": stop["reason"]}
    if not args.write:
      return dict(result, preview=True)
    body = message(args.unit, attempt, stop)
    existing = git(work, "rev-parse", "--verify", "--quiet", private,
           allowed=(0, 1)).decode("ascii").strip()
    if path.exists():
      receipt = read_object(path)
      supplied = receipt.pop("publication_digest", None)
      if supplied != digest(receipt):
        raise Refusal("prepared receipt publication_digest mismatch")
      for key, value in result.items():
        if key != "ignored" and receipt.get(key) != value:
          raise Refusal("prepared receipt differs: " + key)
      if not isinstance(receipt.get("ignored"), list) or not receipt.get("time"):
        raise Refusal("incomplete prepared receipt")
      commit = oid(receipt.get("commit"))
      validate_commit(work, commit, tree, head, body)
      if existing != commit:
        raise Refusal("private ref differs from prepared receipt")
      receipt["publication_digest"] = supplied
      durable_write(path, receipt, state_dir)
      return receipt
    if existing:
      commit = oid(existing)
    else:
      msg = directory / "message"
      msg.write_bytes(body)
      commit = oid(git(work, "-c", "i18n.commitEncoding=UTF-8", "commit-tree",
              tree, "-p", head, "-F", str(msg)).decode("ascii").strip())
    validate_commit(work, commit, tree, head, body)
    if not existing:
      git(work, "update-ref", private, commit, "0" * len(commit))
    result.update(commit=commit, time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    result["publication_digest"] = digest(result)
    durable_write(path, result, state_dir)
    return result


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("command", choices=["prepare"])
  parser.add_argument("plan")
  parser.add_argument("--state-dir", required=True)
  parser.add_argument("--unit", required=True)
  parser.add_argument("--attempt")
  parser.add_argument("--root")
  parser.add_argument("--write", action="store_true")
  args = parser.parse_args(argv)
  try:
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
