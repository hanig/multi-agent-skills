"""Build continuation units from coordinator anchors and live Git only."""

import copy
import fcntl
import io
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

import coordinator_paths as CP
import swarm_code_launch as SCL
import swarm_evidence as SE
import swarm_plan_rules as SPR
import swarm_preflight as SP
import swarm_state as SS
import unit as U
import worktree as W
from swarm_types import PlanError


def _git(repo, *args):
    rc, out, err = U.run(["git", "-C", str(repo), *args],
                         timeout=120, preserve_newlines=True)
    if rc:
        raise PlanError(f"Git {args[0]} failed: {err}")
    return out.strip()


def _config_text(source):
    def quote(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace(
            "\n", "\\n").replace("\t", "\\t").replace("\b", "\\b") + '"'

    lines = []
    for entry in _git(source, "config", "--null", "--list", "--includes").split("\0"):
        if not entry:
            continue
        key, separator, value = entry.partition("\n")
        if key == "include.path" or key.startswith("includeif.") or key == "core.worktree":
            continue
        section, name = key.rsplit(".", 1)
        section, dot, subsection = section.partition(".")
        lines.append("[" + section + (" " + quote(subsection) if dot else "")
                     + "]\n" + name + (" = " + quote(value) if separator else ""))
    return "\n".join(lines) + "\n[core]\nbare = true\n"


class _ScratchGit:
    """Freeze effective Git configuration; keep cwd, redirect all Git storage."""

    def __init__(self, root):
        self.root = Path(root)
        self.repos = {}

    @contextmanager
    def repo(self, source):
        if source not in self.repos:
            dest = self.root / str(len(self.repos))
            _git(source, "-c", "protocol.file.allow=always", "clone", "--quiet",
                 "--bare", "--shared", "--", source, str(dest))
            (dest / "config").write_text(_config_text(source), encoding="utf-8")
            self.repos[source] = str(dest)
        # This CLI is synchronous. Restore environment before any source read.
        keys = set(("GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE GIT_INDEX_FILE "
                    "GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES "
                    "GIT_NAMESPACE GIT_SHALLOW_FILE GIT_GRAFT_FILE "
                    "GIT_REPLACE_REF_BASE GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM "
                    "GIT_CONFIG GIT_CONFIG_SYSTEM GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT").split())
        saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
        os.environ.update(GIT_DIR=self.repos[source], GIT_CONFIG_GLOBAL=os.devnull,
                          GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="0")
        try:
            yield source
        finally:
            for key in keys:
                os.environ.pop(key, None)
            os.environ.update(saved)

    def check(self, seed, repo, raw, remote):
        with self.repo(repo) as scratch:
            return SP._seed_reachability_problem(seed, scratch, raw, remote)


def _remote_head(repo, raw, remote, ref):
    SPR.declared_seed({"id": "continue", "kind": "code", "seed": {
        "ref": ref, "base": "0" * 40, "head": "0" * 40}})
    rc, listing, err = SP._git_push_destination(
        repo, raw, remote, 2, "ls-remote", "--exit-code", raw, ref)
    head = SP._exact_remote_ref_head(listing, ref)
    if rc or head is None:
        raise PlanError(f"ref {ref!r} does not resolve: {err}")
    return head


def _continuation(plan, state, args, scratch):
    units = plan.get("units")
    if not isinstance(units, list):
        raise PlanError("plan units must be a JSON list")
    if any(u.get("id") == args.new_id for u in units):
        raise PlanError(f"unit {args.new_id!r} already exists")
    matches = [u for u in units if u.get("id") == args.unit]
    if len(matches) != 1:
        raise PlanError(f"OLD unit {args.unit!r} must occur once in the plan")
    old = matches[0]
    if old.get("kind") != "code":
        raise PlanError("continue requires a code unit")
    us = (state.get("units") or {}).get(args.unit) or {}
    attempt_dir = us.get("attempt_dir")
    if not isinstance(attempt_dir, str) or not attempt_dir:
        raise PlanError(f"unit {args.unit!r} has no recorded attempt")
    attempt = Path(attempt_dir).name
    intent = (us.get("attempt_launch_intents") or {}).get(attempt)
    problem = SCL._code_launch_intent_problem(intent, old, attempt)
    if problem:
        raise PlanError(problem)
    repo, recorded_base = intent["repo"], intent["base_commit"]
    CP.resolve_paths(args.state_dir, plan=plan, extra_repos=[repo])
    declared_repo, problem = SP._plan_workspace(old)
    if problem or str(Path(declared_repo).resolve()) != repo:
        raise PlanError("plan repository differs from the anchored repository")
    raw, problem = W._anchored_remote_transport(U.run, intent)
    if problem:
        raise PlanError(problem)
    remote = intent["repository_remote"]
    ref = (args.head_ref if args.head_ref is not None
           else "refs/heads/" + intent["branch"])
    tip = _remote_head(repo, raw, remote, ref)
    judged = SE.trusted_produced_head(state, args.unit, attempt_dir)
    head = tip if args.head_ref is not None or judged is None else judged
    seed = {"ref": ref, "head": head, "base": recorded_base,
            "evidence": str(Path(attempt_dir) / "evidence.md")}
    SPR.declared_seed({"id": args.new_id, "kind": "code", "seed": seed})
    problem = scratch.check(seed, repo, raw, remote)
    if problem:
        raise PlanError("recorded base/head ancestry check failed: " + problem)
    target_ref = "refs/heads/" + intent["target_branch"]
    target_tip = _remote_head(repo, raw, remote, target_ref)
    problem = scratch.check({"ref": target_ref, "base": target_tip,
                             "head": target_tip}, repo, raw, remote)
    if problem:
        raise PlanError(problem)
    with scratch.repo(repo) as git_repo:
        base = _git(git_repo, "merge-base", target_tip, head)
        _git(git_repo, "merge-base", "--is-ancestor", base, target_tip)
    seed["base"] = base
    new = copy.deepcopy(old)
    new.update(id=args.new_id, seed=seed, description="Continuation of " + args.unit,
               # Decode bytes directly: text-mode universal newlines would
               # silently rewrite CRLF. JSON prompts are UTF-8 strings.
               prompt=Path(args.prompt_file).read_bytes().decode("utf-8"))
    return new


@contextmanager
def _plan_file(path, write):
    while True:
        stream = path.open("rb")
        try:
            if write:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            current, opened = path.stat(), os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                stream.close()
                continue
            yield stream
            return
        finally:
            stream.close()


def _append(path, before, plan, mode):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False) as out:
            temp = out.name
            os.fchmod(out.fileno(), stat.S_IMODE(mode))
            out.write((json.dumps(plan, indent=2, ensure_ascii=True) + "\n").encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        if path.read_bytes() != before:
            raise PlanError("plan changed during continuation validation; retry")
        os.replace(temp, str(path))
        temp = None
    finally:
        if temp is not None:
            os.unlink(temp)


def continue_plan(args, validate):
    try:
        path = Path(args.plan).resolve()
        with _plan_file(path, args.write) as stream:
            before = stream.read()
            plan = json.loads(before)
            state_dir, _, _ = CP.resolve_paths(args.state_dir, plan=plan)
            state, error = U.read_json(state_dir / SS.STATE_FILE)
            if error or not isinstance(state, dict):
                raise PlanError(f"no readable coordinator state: {error}")
            with tempfile.TemporaryDirectory(prefix="swarm-continue-") as tmp:
                scratch = _ScratchGit(tmp)
                new = _continuation(plan, state, args, scratch)
                plan["units"].append(new)
                with redirect_stdout(io.StringIO()):
                    validate(plan, scratch.check)
            if args.write:
                _append(path, before, plan, os.fstat(stream.fileno()).st_mode)
        print(json.dumps(new, indent=2, ensure_ascii=True))
        return 0
    except (PlanError, CP.PathPolicyError, OSError, ValueError,
            TypeError, KeyError, AttributeError) as exc:
        print(f"error: cannot continue: {exc}", file=sys.stderr)
        return 1
