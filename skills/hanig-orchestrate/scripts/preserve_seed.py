#!/usr/bin/env python3
"""Local recovery preparation (Python 3.8+)."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from merge_unit import CE, CP, S, Refusal, durable_write, oid, read_object
import paseo_io as PIO
import swarm_state as SS
import worktree as W

PREFIX = "RECOVERY, NOT READY:"


def run(command, timeout=60, cwd=None, data=None):
  env = CE.child_env()
  for key in list(env):
    if key.startswith("GIT_") and key not in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"):
      del env[key]
  env["GIT_OPTIONAL_LOCKS"] = "0"
  result = subprocess.run(command, cwd=cwd, env=env, input=data,
              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
              encoding="utf-8", errors="surrogateescape", timeout=timeout)
  return result.returncode, result.stdout, result.stderr


def git(work, *args):
  rc, out, err = run(["git", "-C", str(work), "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgsign=false"] + list(args))
  if rc:
    raise Refusal("git {}: {}".format(args[0], err or out))
  return out


def dirty(work):
  return git(work, "status", "--porcelain", "--untracked-files=all")


def token(value):
  if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) or value.endswith("."):
    raise Refusal("invalid unit/attempt component: " + repr(value))
  return value


def authority(args, plan):
  state = read_object(Path(args.state_dir) / SS.STATE_FILE)
  if state.get("plan_digest") != S.plan_digest(plan):
    raise Refusal("plan differs from coordinator's recorded digest")
  unit = next((u for u in plan["units"] if u["id"] == args.unit), None)
  if not unit or unit["kind"] != "code":
    raise Refusal("unit must name a recorded code unit")
  us = state.get("units", {}).get(args.unit, {})
  current = Path(us.get("attempt_dir") or "").name
  attempt = token(args.attempt or current)
  record = (us.get("attempt_provider_stops") or {}).get(attempt) or {}
  if record.get("phase") != "retained":
    raise Refusal("attempt has no retained provider-stop record")
  launch = (us.get("attempt_launch_intents") or {}).get(attempt)
  problem = S._code_launch_intent_problem(launch, unit, attempt)
  if problem:
    raise Refusal(problem)
  facts = SS.trusted_launch_facts(state, args.unit, attempt)
  if not facts:
    raise Refusal("invalid attempt launch facts: " + attempt)
  for key in ("unit_id", "attempt_id", "repo", "base_commit", "base_tree",
        "branch", "repository_remote", "launch_host"):
    if not facts.get(key) or facts[key] != launch.get(key):
      raise Refusal("launch intent and facts disagree: " + key)
  work = facts["execution_workspace"]
  meta = (us.get("attempt_workspaces") or {}).get(attempt) or {}
  if (record.get("branch") != facts["branch"]
      or record.get("worktree_path") != work or meta.get("path") != work):
    raise Refusal("retained branch/worktree binding mismatch")
  root = state.get("root") or args.root
  if not root or (args.root and Path(root).resolve() != Path(args.root).resolve()):
    raise Refusal("missing/conflicting coordinator root")
  CP.resolve_paths(args.state_dir, root, plan=plan,
          extra_repos=[launch["repo"], work], need_root=True)
  for other in state.get("units", {}).values():
    if other.get("attempt_dir") and CP._inside(args.state_dir, other["attempt_dir"]):
      raise Refusal("state directory inside worker attempt")
  agent = (us.get("job_id") if attempt == current else
      (us.get("code_terminal_watches", {}).get(attempt) or {}).get("agent_id"))
  if not agent:
    raise Refusal("missing coordinator agent binding: " + attempt)
  return attempt, record, facts, work, agent


def stopped(agent):
  rc, out, err = run(["paseo", "inspect", "--json", str(agent)])
  rec = PIO.first_json_object(out) if rc == 0 else None
  if not rec:
    raise Refusal("cannot inspect retained agent: " + err)
  status = str(rec.get("Status") or rec.get("status") or "").lower()
  if rec.get("PendingPermissions") or rec.get("pendingPermissions"):
    raise Refusal("agent has pending permissions")
  if status in ("running", "working", "busy"):
    raise Refusal("agent is running: " + status)
  if status not in S.U.PASEO_LIFECYCLE:
    raise Refusal("agent status unknown: " + status)


def content(work):
  """Detect entries Git can silently omit."""
  index = git(work, "ls-files", "--stage", "-z").split("\0")
  for row in index + git(work, "ls-tree", "-rz", "HEAD").split("\0"):
    if row.startswith("160000 "):
      raise Refusal("unsupported submodule gitlink: " + repr(row.split("\t", 1)[1]))
  tracked = {row.split("\t", 1)[1] for row in index if row}
  entries = []
  pending = [Path(work)]
  while pending:
    directory = pending.pop()
    for path in sorted(directory.iterdir()):
      if directory == Path(work) and path.name == ".git":
        continue
      relative = path.relative_to(work).as_posix()
      mode = path.lstat().st_mode
      nested = stat.S_ISDIR(mode) and ((path / ".git").exists() or (
        (path / "HEAD").is_file() and run([
          "git", "--git-dir", str(path), "rev-parse", "--is-bare-repository"])[0] == 0))
      entries.append((relative, mode, nested))
      if stat.S_ISDIR(mode):
        pending.append(path)
  untracked = [p for p, _, _ in entries if p not in tracked]
  ignored = set()
  if untracked:
    rc, out, err = run(["git", "-C", str(work), "check-ignore", "-z", "--stdin"],
             data="\0".join(untracked) + "\0")
    if rc not in (0, 1):
      raise Refusal("cannot classify ignored paths: " + err)
    ignored = set(out.rstrip("\0").split("\0")) if out else set()
  for path, mode, nested in entries:
    if path in ignored:
      print(repr(path) + ": omitted (ignored; kept only in the pre-removal snapshot)")
    elif nested:
      raise Refusal("unsupported nested Git repository: " + repr(path))
    elif not (stat.S_ISREG(mode) or stat.S_ISLNK(mode) or stat.S_ISDIR(mode)):
      raise Refusal("unsupported special file: " + repr(path))


def digest(receipt):
  return hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":"))
             .encode("utf-8")).hexdigest()


def prepare(args, plan):
  attempt, record, facts, work, agent = authority(args, plan)
  original = oid(record.get("head_at_retention"))
  destination = "refs/heads/recovery/{}-{}".format(args.unit, attempt)
  if record.get("recovery_ref") != destination or not record.get("reason"):
    raise Refusal("invalid retained destination or stop reason")
  git(work, "check-ref-format", destination)
  receipt_path = Path(args.state_dir) / "recovery-seeds" / args.unit / attempt / "prepared.json"
  receipt = read_object(receipt_path) if receipt_path.exists() else None
  binding = {"unit": args.unit, "attempt": attempt, "original_head": original,
       "destination_ref": destination, "stop_reason": record["reason"]}
  if receipt is not None:
    payload = {k: v for k, v in receipt.items() if k != "publication_digest"}
    if (any(receipt.get(k) != v for k, v in binding.items())
        or receipt.get("publication_digest") != digest(payload)
        or not receipt.get("prepared_at")):
      raise Refusal("receipt disagrees with binding/digest")
    expected = oid(receipt.get("prepared_head"))
    identity_facts = dict(facts, branch="HEAD")
  else:
    expected, identity_facts = original, facts
    if (git(work, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
        and not dirty(work)):
      identity_facts = dict(facts, branch="HEAD")
  problem = W.workspace_identity_problem(run, identity_facts)
  if problem:
    raise Refusal(problem)
  if git(work, "rev-parse", "--verify", "HEAD^{commit}").strip() != expected:
    raise Refusal("HEAD disagrees with retained head or receipt")
  git(work, "merge-base", "--is-ancestor", oid(facts["base_commit"]), original)
  stopped(agent)
  content(work)
  if receipt is not None:
    if (dirty(work)
        or git(work, "rev-parse", "HEAD^").strip() != original
        or not git(work, "log", "-1", "--format=%B").startswith(PREFIX)):
      raise Refusal("receipt disagrees with worktree content or commit")
    print(json.dumps(receipt, sort_keys=True))
    return
  print(git(work, "status", "--short", "--untracked-files=all"), end="")
  if not args.write:
    print("Preview: detach at " + original + "; git add -A; create recovery label commit")
    return
  git(work, "checkout", "--detach", original)
  content(work)  # includeIf.onbranch can change ignore rules after detachment.
  git(work, "add", "-A")
  message = (PREFIX + " retained provider stop\n\nUnit: {}\nAttempt: {}\n"
       "Stop reason: {}\nOriginal head: {}\nNo review ran.\n").format(
         args.unit, attempt, record["reason"], original)
  git(work, "commit", "--allow-empty", "--cleanup=verbatim", "-m", message)
  receipt = dict(binding, prepared_head=git(work, "rev-parse", "HEAD").strip(),
         prepared_at=datetime.now(timezone.utc).isoformat())
  if dirty(work):
    raise Refusal("dirty worktree after commit; no receipt written")
  receipt["publication_digest"] = digest(receipt)
  receipt_path.parent.mkdir(parents=True, exist_ok=True)
  durable_write(receipt_path, receipt)
  print(json.dumps(receipt, sort_keys=True))


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
    token(args.unit)
    plan = read_object(args.plan)
    authority(args, plan)  # path policy before opening the lease
    # Preview never creates a missing lease file.
    if not (Path(args.state_dir) / SS.LOCK).is_file():
      raise Refusal("coordinator lease lock is missing")
    ok, holder = SS.acquire_lease(args.state_dir, bump_epoch=False)
    if not ok:
      raise Refusal("coordinator lock unavailable: " + str(holder))
    try:
      prepare(args, plan)
    finally:
      SS.release_lease(args.state_dir)
    return 0
  except (OSError, ValueError, TypeError, KeyError, AttributeError,
      subprocess.SubprocessError, CP.PathPolicyError, S.PlanError) as exc:
    print("REFUSED: " + str(exc), file=sys.stderr)
    return 1


if __name__ == "__main__":
  sys.exit(main())
