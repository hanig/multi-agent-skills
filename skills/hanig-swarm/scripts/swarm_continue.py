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
from swarm_types import PlanError


def _remote_head(repo, raw, remote, ref):
    SPR.declared_seed({"id": "continue", "kind": "code", "seed": {
        "ref": ref, "base": "0" * 40, "head": "0" * 40}})
    rc, listing, err = SP._git_push_destination(
        repo, raw, remote, 2, "ls-remote", "--exit-code", raw, ref)
    head = SP._exact_remote_ref_head(listing, ref)
    if rc or head is None:
        raise PlanError(f"ref {ref!r} does not resolve: {err}")
    return head


def _continuation(plan, state, args):
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
    target, problem = SP._resolve_dispatch_target(old)
    if problem:
        raise PlanError(problem)
    if target["repo"] != repo:
        raise PlanError("plan repository differs from the anchored repository")
    remote, raw = intent["repository_remote"], target["repository_remote_raw"]
    if (target["repository_remote"] != remote
            or (intent.get("repository_remote_raw") is not None
                and intent["repository_remote_raw"] != raw)):
        raise PlanError("origin push destination differs from the anchored repository")
    ref = (args.head_ref if args.head_ref is not None
           else "refs/heads/" + intent["branch"])
    judged = SE.trusted_produced_head(state, args.unit, attempt_dir)
    head = (_remote_head(repo, raw, remote, ref)
            if args.head_ref is not None or judged is None else judged)
    seed = {"ref": ref, "head": head, "base": recorded_base,
            "evidence": str(Path(attempt_dir) / "evidence.md")}
    SPR.declared_seed({"id": args.new_id, "kind": "code", "seed": seed})
    problem = SP._seed_reachability_problem(seed, repo, raw, remote)
    if problem:
        raise PlanError("recorded base/head ancestry check failed: " + problem)
    target_tip = target["target_commit"]
    problem = SP._seed_reachability_problem(
        {"ref": target["target_ref"], "base": target_tip, "head": target_tip},
        repo, raw, remote)
    if problem:
        raise PlanError(problem)
    rc, base, err = SP._git(repo, "merge-base", target_tip, head)
    if rc:
        raise PlanError("target/head ancestry check failed: " + err)
    seed["base"] = base
    new = copy.deepcopy(old)
    new.update(id=args.new_id, seed=seed, description="Continuation of " + args.unit,
               # Byte decoding preserves CRLF in UTF-8 prompts.
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
            new = _continuation(plan, state, args)
            plan["units"].append(new)
            with redirect_stdout(io.StringIO()):
                validate(plan)
            if args.write:
                _append(path, before, plan, os.fstat(stream.fileno()).st_mode)
        print(json.dumps(new, indent=2, ensure_ascii=True))
        return 0
    except (PlanError, CP.PathPolicyError, OSError, ValueError,
            TypeError, KeyError, AttributeError) as exc:
        print(f"error: cannot continue: {exc}", file=sys.stderr)
        return 1
