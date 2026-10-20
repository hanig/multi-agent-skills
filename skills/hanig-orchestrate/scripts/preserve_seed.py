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

from merge_unit import S,CE,CP,Refusal,read_object,oid

PREFIX=b"RECOVERY, NOT READY:"
LOCATION_ENV="GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_NAMESPACE GIT_COMMON_DIR GIT_PREFIX GIT_CEILING_DIRECTORIES".split()


def run(cmd,timeout=60,env=None):
 child={k: v for k,v in CE.child_env().items() if k not in LOCATION_ENV}
 child.update(GIT_OPTIONAL_LOCKS="0",GIT_TERMINAL_PROMPT="0",GIT_NO_REPLACE_OBJECTS="1",GIT_NO_LAZY_FETCH="1"); child.update(env or {})
 with subprocess.Popen(cmd,env=child,stdin=subprocess.DEVNULL,
   stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True) as p:
  try:
   out,err=p.communicate(timeout=timeout)
  except subprocess.TimeoutExpired:
   try:
    os.killpg(p.pid,signal.SIGKILL)
   except ProcessLookupError:
    pass
   p.communicate()
   raise Refusal(f"retryable: command exceeded {timeout}s: {cmd}")
 return p.returncode,out,err


def identity_run(cmd,timeout=60):
 rc,out,err=run(cmd,timeout)
 return rc,os.fsdecode(out),os.fsdecode(err)


def git(w,*a,env=None,allowed=(0,),timeout=60):
 rc,out,err=run(["git","-C",str(w),"-c","core.splitIndex=false",
      *a],env=env,timeout=timeout)
 if rc not in allowed:
  raise Refusal(f"git {a[0]} exited {rc}: {os.fsdecode(err or out)}")
 return out


def source(a,plan):
 state=read_object(Path(a.state_dir) / S.STATE_FILE); S.validate_plan(plan)
 if state.get("plan_digest") != S.plan_digest(plan):
  raise Refusal("plan differs from coordinator digest")
 unit=next((u for u in plan["units"] if u["id"] == a.unit),None)
 if not unit or unit["kind"] != "code":
  raise Refusal("not a recorded code unit")
 us=state.get("units",{}).get(a.unit,{}); aid=a.attempt or Path(us.get("attempt_dir") or "").name
 if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*",v or "")
   for v in (a.unit,aid)):
  raise Refusal("invalid unit/attempt")
 stop=(us.get("attempt_provider_stops") or {}).get(aid) or {}
 if stop.get("phase") != "retained" or stop.get("retention_problem"):
  raise Refusal("no valid retained stop")
 launch=(us.get("attempt_launch_intents") or {}).get(aid); why=S._code_launch_intent_problem(launch,unit,aid)
 f=S.SS.trusted_launch_facts(state,a.unit,aid)
 if why or not f:
  raise Refusal(why or "invalid trusted launch facts")
 for key in "unit_id attempt_id repo repository_remote base_commit base_tree branch judgment_ref launch_host".split():
  if f.get(key) != launch.get(key):
   raise Refusal("launch binding mismatch: "+key)
 w=f["execution_workspace"]; meta=(us.get("attempt_workspaces") or {}).get(aid) or {}
 destination=f"refs/heads/recovery/{a.unit}-{aid}"
 if (stop.get("worktree_path") != w or meta.get("path") != w
   or stop.get("branch") != f["branch"]
   or stop.get("recovery_ref") != destination
   or not isinstance(stop.get("reason"),str) or not stop["reason"]):
  raise Refusal("retained worktree binding mismatch")
 root=state.get("root") or a.root
 if a.root and state.get("root") and Path(a.root).resolve() != Path(root).resolve():
  raise Refusal("--root conflicts with state")
 sd,_,_=CP.resolve_paths(a.state_dir,root,plan=plan,
          extra_repos=[launch["repo"],w],need_root=True)
 if any(CP._inside(sd,other["attempt_dir"])
   for other in state.get("units",{}).values() if other.get("attempt_dir")):
  raise Refusal("state directory inside worker attempt")
 return sd,us,aid,stop,f


def stopped(us,aid,w):
 rc,out,err=run(["paseo","ls","--json"])
 if rc:
  raise Refusal("cannot list agents: "+os.fsdecode(err or out))
 agents=json.loads(out)
 if not isinstance(agents,list) or any(not isinstance(a,dict) for a in agents):
  raise Refusal("unreadable agent list")
 ids=set()
 if Path(us.get("attempt_dir") or "").name == aid and us.get("job_id"):
  ids.add(str(us["job_id"]))
 for agent in agents:
  cwd=agent.get("cwd") or agent.get("Cwd")
  if cwd and str(Path(cwd).resolve()) == w:
   aid=agent.get("id") or agent.get("agentId")
   if not aid:
    raise Refusal("agent identity missing")
   ids.add(aid)
 if not ids:
  raise Refusal("cannot identify stopped agent")
 for aid in sorted(ids):
  rc,out,err=run(["paseo","inspect","--json",aid]); agent=S.SW._paseo_json(os.fsdecode(out)) if not rc else None
  if not agent:
   raise Refusal("cannot inspect retained agent: "+os.fsdecode(err or out))
  if (agent.get("Cwd") or agent.get("cwd")) != w:
   raise Refusal("agent worktree mismatch")
  status=agent.get("Status") or agent.get("status")
  if (status not in S.U.PASEO_LIFECYCLE or S.U.PASEO_LIFECYCLE[status] == "RUNNING"
    or agent.get("PendingPermissions") or agent.get("pendingPermissions")):
   raise Refusal("agent running/unknown/permission-pending")


def envelope(w):
 partial=git(w,"config","--get","extensions.partialClone",allowed=(0,1))
 remotes=git(w,"config","--bool","--get-regexp",r"^remote\..*\.promisor$",allowed=(0,1))
 if partial or any(row.endswith(b" true") for row in remotes.splitlines()):
  raise Refusal("partial clone")
 for entry in git(w,"ls-files","-v","-z").split(b"\0"):
  if entry[:1].islower() or entry[:1] == b"S":
   raise Refusal("unsupported index flags: assume-unchanged/skip-worktree")


def tree_and_ignored(w,head,d,timeout=1800):
 env={"GIT_INDEX_FILE": str(d / "index")}; live=Path(w) / os.fsdecode(git(w,"rev-parse","--git-path","index"))[:-1]
 if live.exists():
  # Preserve the timestamp Git uses to detect racily dirty entries.
  shutil.copy2(live,env["GIT_INDEX_FILE"])
 else:
  git(w,"read-tree",head,env=env)
 git(w,"add","-A",env=env,timeout=timeout); tree=oid(git(w,"write-tree",env=env,timeout=timeout).decode("ascii").strip())
 raw=git(w,"diff-tree","-r","-z","--no-renames",
    "--ignore-submodules=none",head+"^{tree}",tree,env=env)
 for header in raw.split(b"\0")[::2]:
  if b"160000" in header.lstrip(b":").split(b" ")[:2]:
   raise Refusal("changed 160000 gitlink")
 entries=iter(git(w,"status","--porcelain=v1","--untracked-files=normal",
      "--ignored=traditional","-z",env=env).split(b"\0"))
 ignored=[]
 for entry in entries:
  if entry.startswith(b"!! "):
   ignored.append(os.fsdecode(entry[3:]))
  elif b"R" in entry[:2] or b"C" in entry[:2]:
   next(entries,None)
 return tree,ignored


def message(unit,aid,stop):
 return (f"RECOVERY, NOT READY: retained provider stop\n\nUnit: {unit}\n"
   f"Attempt: {aid}\nStop reason: {stop['reason']}\n"
   f"Retained head: {stop['head_at_retention']}\nNo review ran. Must not be merged.\n").encode("utf-8")


def validate_commit(w,commit,tree,parent,body):
 if git(w,"cat-file","-t",oid(commit)) != b"commit\n":
  raise Refusal("recovery object must be a commit")
 raw=git(w,"cat-file","commit",oid(commit)); headers,separator,actual=raw.partition(b"\n\n")
 rows=headers.split(b"\n")
 if (not separator or not actual.startswith(PREFIX) or actual != body
   or [r[5:] for r in rows if r.startswith(b"tree ")] != [tree.encode("ascii")]
   or [r[7:] for r in rows if r.startswith(b"parent ")] != [parent.encode("ascii")]):
  raise Refusal("recovery tree, parent or message differs")


def read_ref(w,ref):
 # for-each-ref omits dangling symrefs.
 target=os.fsdecode(git(w,"symbolic-ref","-q",ref,allowed=(0,1)))[:-1]
 if target:
  raise Refusal(f"symbolic private ref {ref} targets {target}")
 try:
  row=git(w,"for-each-ref","--format=%(objectname) %(objecttype) %(symref)",ref)
  if not row:
   return ""
  commit,kind,symref=row.decode("ascii")[:-1].split(" ",2)
  if kind != "commit" or symref:
   raise Refusal(f"target {symref or commit} is not a direct commit")
  git(w,"cat-file","commit",oid(commit))
  return commit
 except Refusal as exc:
  raise Refusal(f"private ref {ref}: {exc}")


def set_private(w,ref,commit,old):
 git(w,"update-ref","--no-deref",ref,commit,old)
 if read_ref(w,ref) != commit:
  raise Refusal(f"private ref {ref} does not target {commit}")


def prepare(a,plan):
 sd,us,aid,stop,f=source(a,plan); w,head=f["execution_workspace"],oid(stop.get("head_at_retention"))
 why=S.W.workspace_identity_problem(identity_run,f)
 if why:
  raise Refusal(why)
 if git(w,"rev-parse","--verify","HEAD^{commit}").decode("ascii").strip() != head:
  raise Refusal("HEAD differs from head_at_retention")
 git(w,"merge-base","--is-ancestor",f["base_commit"],head); stopped(us,aid,w)
 ref=f"refs/hanig-recovery/{a.unit}/{aid}"; git(w,"check-ref-format",ref)
 old=read_ref(w,ref)
 if old:
  raise Refusal(f"private ref {ref} already targets {old}; retry not implemented")
 with tempfile.TemporaryDirectory(prefix=".recovery-index-",dir=str(sd)) as tmp:
  d=Path(tmp); tree,ignored=tree_and_ignored(w,head,d,a.git_timeout)
  result=dict(tree=tree,parent=head,private_ref=ref,ignored=ignored)
  if not a.write:
   return dict(result,preview=True)
  body=message(a.unit,aid,stop); msg=d / "message"
  msg.write_bytes(body)
  commit=oid(git(w,"-c","i18n.commitEncoding=UTF-8","commit-tree",
      tree,"-p",head,"-F",str(msg)).decode("ascii").strip())
  validate_commit(w,commit,tree,head,body)
 set_private(w,ref,commit,"0" * 40)
 return dict(result,commit=commit)


def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__); p.add_argument("command",choices=["prepare"])
 p.add_argument("plan"); p.add_argument("--state-dir",required=True)
 p.add_argument("--unit",required=True); p.add_argument("--attempt")
 p.add_argument("--root"); p.add_argument("--write",action="store_true")
 p.add_argument("--git-timeout",type=int,default=1800,
      help="add/write-tree seconds (default: 1800)")
 a=p.parse_args(argv); prior=os.environ.get("GIT_NO_LAZY_FETCH")
 os.environ["GIT_NO_LAZY_FETCH"]="1"  # Also covers shared path readers.
 try:
  if a.git_timeout <= 0:
   raise Refusal("--git-timeout must be positive")
  plan=read_object(a.plan); _,_,_,_,f=source(a,plan)
  envelope(f["execution_workspace"]); ok,holder=S.acquire_lease(a.state_dir,bump_epoch=False)
  if not ok:
   raise Refusal("coordinator lease unavailable: "+str(holder))
  try:
   result=prepare(a,plan)
  finally:
   S.release_lease(a.state_dir)
  print(json.dumps(result,sort_keys=True,indent=2))
  return 0
 except (OSError,ValueError,TypeError,KeyError,AttributeError,
   subprocess.SubprocessError,S.PlanError,CP.PathPolicyError) as exc:
  print("REFUSED: "+str(exc),file=sys.stderr)
  return 2
 finally:
  if prior is None:
   os.environ.pop("GIT_NO_LAZY_FETCH",None)
  else:
   os.environ["GIT_NO_LAZY_FETCH"]=prior


if __name__ == "__main__":
 sys.exit(main())
