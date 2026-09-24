#!/usr/bin/env python3
"""Connected operator for one squash merge and coordinator reconciliation.

Never imported by the coordinator. Local authority readers/locking are shared
with swarm; scope checking, receipt recording and advancement use its CLI.
The operator's gh calls inherit its environment for authentication; swarm.py
children use hanig-swarm's child_environment.child_env containment instead.
Forge observations remain attestations. Same-node, trusted-writer convention;
GitHub's head compare protects the merge, not a concurrent CI rerun/retarget.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import skill_paths

_ORCHESTRATE_DIR = os.environ.get("HANIG_ORCHESTRATE_DIR") or Path(__file__).parents[1]
_SWARM_DIR = skill_paths.sibling_skill_root(
    _ORCHESTRATE_DIR, "hanig-orchestrate", "hanig-swarm")
sys.path.insert(0, str(_SWARM_DIR / "scripts"))
import child_environment as CE
import coordinator_paths as CP
import swarm as S


SWARM = str(_SWARM_DIR / "scripts" / "swarm.py")
PR_FIELDS = "number,url,state,headRefOid,baseRefName,baseRefOid,mergeCommit"


class Refusal(ValueError):
    pass


def nonempty(value):
    if not value.strip() or "\n" in value or "\r" in value:
        raise argparse.ArgumentTypeError("must be a non-empty single line")
    return value


def oid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise Refusal("missing or invalid exact Git object id")
    return value


def read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Refusal("expected a JSON object at {}".format(path))
    return value


def durable_write(path, value):
    """Atomically publish a journal record and fsync it and its directory."""
    fd, temporary = tempfile.mkstemp(prefix=".merge-unit-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(command, allowed=(0,)):
    print("+ " + shlex.join(command), flush=True)
    result = subprocess.run(command, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=300,
                            env=CE.child_env() if command[:2] == [sys.executable, SWARM] else None)
    if result.returncode not in allowed:
        raise Refusal("command exited {}: {}{}".format(
            result.returncode, result.stdout, result.stderr))
    return result


def forge_route(remote):
    """Derive gh addressing, retaining the exact anchor for receipt equality."""
    if not isinstance(remote, str):
        raise Refusal("no anchored repository remote")
    if remote.startswith("git@") and ":" in remote:
        host, path = remote[4:].split(":", 1)
    else:
        parsed = urlsplit(remote)
        if (parsed.scheme not in ("https", "http", "ssh") or not parsed.hostname
                or parsed.query or parsed.fragment or parsed.password):
            raise Refusal("anchored remote is not a supported forge URL")
        if parsed.port is not None:
            raise Refusal("explicit-port forge URLs are unsupported")
        host, path = parsed.hostname, parsed.path.lstrip("/")
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if (not re.fullmatch(r"[A-Za-z0-9.-]+", host)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path)):
        raise Refusal("anchored remote has no unambiguous forge repository")
    return host, path


def authority(args, plan):
    state = read_object(Path(args.state_dir) / S.STATE_FILE)
    S.validate_plan(plan)
    if state.get("plan_digest") != S.plan_digest(plan):
        raise Refusal("plan differs from coordinator's recorded digest")
    unit = next((u for u in plan["units"] if u["id"] == args.unit), None)
    if not unit or unit["kind"] != "code":
        raise Refusal("unit must name one code unit in the recorded plan")
    us = state.get("units", {}).get(args.unit, {})
    attempt_dir = us.get("attempt_dir")
    if not isinstance(attempt_dir, str) or not attempt_dir:
        raise Refusal("no current attempt in coordinator state")
    attempt = Path(attempt_dir).name
    launch = (us.get("attempt_launch_intents") or {}).get(attempt)
    problem = S._code_launch_intent_problem(launch, unit, attempt)
    if problem:
        raise Refusal(problem)
    head = oid(S.trusted_produced_head(state, args.unit, attempt_dir))
    root = state.get("root")
    if root is not None and (not isinstance(root, str) or not root):
        raise Refusal("invalid recorded root")
    if root and args.root and Path(root).resolve() != Path(args.root).resolve():
        raise Refusal("--root conflicts with coordinator's recorded root")
    root = root or args.root
    if not root:
        raise Refusal("no recorded root; supply --root explicitly")
    state_dir, root, _ = CP.resolve_paths(
        args.state_dir, root, plan=plan, extra_repos=[launch["repo"]], need_root=True)
    for other in state.get("units", {}).values():
        directory = other.get("attempt_dir")
        if directory and CP._inside(state_dir, directory):
            raise Refusal("state directory is inside a worker attempt")
    if us.get("state") not in ("READY_FOR_PR", "DONE"):
        raise Refusal("current attempt is not READY_FOR_PR or DONE")
    remote = launch.get("repository_remote")
    host, repo_path = forge_route(remote)
    binding = {"unit": args.unit, "attempt": attempt, "head": head,
               "repo": remote, "target": launch["target_branch"], "pr": args.pr}
    return state_dir, str(root), binding, host, repo_path


def commands(args, root, binding, host, repo_path):
    route = host + "/" + repo_path
    pr = str(args.pr)
    return {
        "view": ["gh", "pr", "view", pr, "--repo", route, "--json", PR_FIELDS],
        "scope": [sys.executable, SWARM, "scope-check", args.plan,
                  "--state-dir", args.state_dir, "--unit", args.unit, "--json"],
        "checks": ["gh", "pr", "checks", pr, "--repo", route,
                   "--json", "name,state"],
        "merge": ["gh", "pr", "merge", pr, "--repo", route,
                  "--squash", "--match-head-commit", binding["head"]],
        "advance": [sys.executable, SWARM, "advance", args.plan,
                    "--state-dir", args.state_dir, "--root", root],
    }


def check_pr(pr, binding):
    if pr.get("headRefOid") != binding["head"]:
        raise Refusal("PR head does not equal coordinator-judged head (state: {})".format(
            pr.get("state")))
    if pr.get("baseRefName") != binding["target"]:
        raise Refusal("PR target does not equal coordinator-anchored target")
    if pr.get("number") != binding["pr"]:
        raise Refusal("forge returned a different PR number")
    if pr.get("state") not in ("OPEN", "MERGED"):
        raise Refusal("PR is neither OPEN nor MERGED")


def receipt_command(args, binding, url, merged, target):
    return [sys.executable, SWARM, "merge", "--state-dir", args.state_dir,
            "--unit", args.unit, "--pr", url, "--head", binding["head"],
            "--target", binding["target"], "--target-commit", target,
            "--merged-as", merged, "--method", "squash", "--repo", binding["repo"]]


def parent_command(host, repo_path, merged):
    return ["gh", "api", "--hostname", host,
            "repos/{}/git/commits/{}".format(repo_path, merged)]


def observed_pr_url(pr, host, repo_path, number):
    """Keep the forge's web scheme; bind the URL to the selected repository."""
    url = pr.get("url")
    if not isinstance(url, str):
        raise Refusal("forge did not return a PR URL")
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or parsed.netloc != host
            or parsed.path != "/{}/pull/{}".format(repo_path, number)
            or parsed.query or parsed.fragment):
        raise Refusal("PR URL does not match the anchored forge route")
    return url


def current_intent(state_dir, binding, root, repair=True):
    """Follow durable abandonments from the legacy binding-derived operation.

    The sidecar is the abandonment commit point. If a crash leaves the original
    intent unmarked, finish that local update before permitting a successor.
    No worker file or filesystem timestamp selects the current operation.
    """
    operation_id = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    while True:
        path = state_dir / ("merge-unit-" + operation_id + ".json")
        abandonment_path = state_dir / ("merge-abandonment-" + operation_id + ".json")
        intent = read_object(path) if path.exists() else None
        if intent is not None and (
                intent.get("operation_id") != operation_id
                or intent.get("binding") != binding or intent.get("root") != root):
            raise Refusal("durable merge intent conflicts with current authority/root")
        if not abandonment_path.exists():
            if intent is not None and intent.get("phase") not in (
                    "merge_requested", "merged", "receipt_recorded"):
                raise Refusal("merge intent has no valid resolution record")
            return operation_id, path, intent
        record = read_object(abandonment_path)
        original = record.get("intent")
        if (not isinstance(original, dict) or original.get("phase") != "merge_requested"
                or record.get("operation_id") != operation_id
                or record.get("schema_version") != 1):
            raise Refusal("invalid abandonment record")
        resolved = dict(original, phase="resolved_by_abandonment",
                        abandonment=abandonment_path.name)
        if intent not in (original, resolved):
            raise Refusal("abandonment record does not match its retained intent")
        for key in ("approver", "reason", "observed_at"):
            nonempty(record.get(key, ""))
        datetime.fromisoformat(record["observed_at"])
        observed = record.get("observed_pr", {})
        check_pr(observed, binding)
        if observed.get("state") != "OPEN":
            raise Refusal("abandonment record did not observe an OPEN PR")
        if repair and intent != resolved:
            durable_write(path, resolved)
        operation_id = hashlib.sha256(
            (operation_id + ":after-abandonment").encode()).hexdigest()


def abandon(args, intent_path, intent, pr):
    record_path = intent_path.with_name(
        "merge-abandonment-" + intent["operation_id"] + ".json")
    record = {"schema_version": 1, "operation_id": intent["operation_id"],
              "intent": intent, "approver": args.approver, "reason": args.reason,
              "observed_at": datetime.now(timezone.utc).isoformat(), "observed_pr": pr}
    durable_write(record_path, record)
    resolved = dict(intent, phase="resolved_by_abandonment", abandonment=record_path.name)
    durable_write(intent_path, resolved)
    print("Abandoned merge intent {}; record: {}. No merge or advance ran.".format(
        intent["operation_id"], record_path))


def reconcile(args, plan):
    state_dir, root, binding, host, repo_path = authority(args, plan)
    cmd = commands(args, root, binding, host, repo_path)
    operation_id, intent_path, intent = current_intent(
        state_dir, binding, root, repair=not args.dry_run)
    if args.abandon_intent and (
            args.abandon_intent != operation_id or intent is None
            or intent.get("phase") not in ("merge_requested", "merged")):
        raise Refusal("named operation is not the unit's current unresolved intent")
    if args.dry_run:
        print("DRY RUN: no forge calls, writes, receipt or advancement; exit 2.")
        print("+ " + shlex.join(cmd["view"]))
        if args.abandon_intent:
            print("if OPEN at judged head: persist abandonment record and resolve {}; "
                  "no merge or advance".format(intent_path))
        else:
            print("if OPEN with no prior merge request:")
            for key in ("scope", "checks"):
                print("+ " + shlex.join(cmd[key]))
            print("persist intent {} before the conditional merge".format(intent_path))
            print("+ " + shlex.join(cmd["merge"]))
            print("+ " + shlex.join(cmd["view"]))
        print("if MERGED at judged head (including reconciliation):")
        print("+ " + shlex.join(parent_command(host, repo_path, "<merged-sha>")))
        print("+ " + shlex.join(receipt_command(
            args, binding, "<observed-pr-url>", "<merged-sha>", "<merge-parent-sha>")))
        print("+ " + shlex.join(cmd["advance"]))
        return None

    # Refuse an unreadable receipt journal before any merge request.
    receipts, _ = S.load_merge_receipts(state_dir)
    pr = json.loads(run(cmd["view"]).stdout)
    check_pr(pr, binding)
    url = observed_pr_url(pr, host, repo_path, args.pr)
    observation = {"approver": args.approver, "already_merged": pr["state"] == "MERGED"}
    if args.abandon_intent and pr["state"] == "OPEN":
        if intent["phase"] != "merge_requested":
            raise Refusal("cannot abandon an intent that already observed a merge")
        abandon(args, intent_path, intent, pr)
        return None
    if pr["state"] == "OPEN":
        if intent:
            raise Refusal("earlier merge request has an unresolved outcome; refusing a second merge call")
        scope = run(cmd["scope"], allowed=(0, 1, 2))
        if scope.returncode and not args.allow_unchecked_scope:
            raise Refusal("scope-check exited {}: {}".format(scope.returncode, scope.stdout))
        checks = json.loads(run(cmd["checks"]).stdout)
        if not isinstance(checks, list) or not checks or any(
                not isinstance(c, dict) or c.get("state") != "SUCCESS" for c in checks):
            raise Refusal("at least one CI check is required and every check must be SUCCESS")
        try:
            scope_report = json.loads(scope.stdout)
        except ValueError:
            if scope.returncode == 0:
                raise Refusal("successful scope-check returned malformed JSON")
            # The named exception authorizes the nonzero exit, not a guessed
            # report. Preserve the exact failed observation for the operator.
            scope_report = {"unparsed_stdout": scope.stdout, "stderr": scope.stderr}
        if scope.returncode == 0:
            if (not isinstance(scope_report, dict)
                    or scope_report.get("status") != "in_scope"
                    or scope_report.get("unit") != binding["unit"]
                    or scope_report.get("attempt") != binding["attempt"]
                    or scope_report.get("head") != binding["head"]
                    or not isinstance(scope_report.get("scope"), list)
                    or any(not isinstance(p, str) for p in scope_report["scope"])
                    or scope_report.get("out_of_scope") != []
                    or scope_report.get("deletions_out_of_scope") != []):
                raise Refusal("successful scope-check returned an invalid scope report")
            oid(scope_report.get("base"))
        observation.update({"scope_exit": scope.returncode, "scope": scope_report,
                            "scope_stdout": scope.stdout, "scope_stderr": scope.stderr,
                            "allow_unchecked_scope": args.allow_unchecked_scope, "checks": checks})
        intent = {"schema_version": 1, "operation_id": operation_id,
                  "binding": binding, "root": root, "phase": "merge_requested",
                  "preconditions": observation, "target_before_request": oid(pr.get("baseRefOid"))}
        durable_write(intent_path, intent)
        run(cmd["merge"])
        pr = json.loads(run(cmd["view"]).stdout)
        check_pr(pr, binding)
        url = observed_pr_url(pr, host, repo_path, args.pr)
        if pr["state"] != "MERGED":
            raise Refusal("merge not observed (possibly queued); rerun to reconcile, never re-merge")
    merged = oid((pr.get("mergeCommit") or {}).get("oid"))
    commit = json.loads(run(parent_command(host, repo_path, merged)).stdout)
    parents = commit.get("parents")
    if commit.get("sha") != merged or not isinstance(parents, list) or len(parents) != 1:
        raise Refusal("squash reconciliation requires the observed single-parent merge commit")
    target = oid(parents[0].get("sha"))
    # The actual merge parent survives crashes and target movement. baseRefOid
    # after merging is NOT the pre-merge target and must never be recorded as it.
    if intent is None:
        intent = {"schema_version": 1, "operation_id": operation_id,
                  "binding": binding, "root": root}
    intent.update({"phase": "merged", "merged_as": merged, "target_commit": target,
                   "reconciliation": observation})
    durable_write(intent_path, intent)
    expected = {"unit": args.unit, "repo": binding["repo"], "pr": url,
                "target": binding["target"], "head": binding["head"],
                "merged_as": merged, "method": "squash", "target_commit": target,
                "merged": True, "attested": True}
    if not any(all(r.get(k) == v for k, v in expected.items()) for r in receipts):
        run(receipt_command(args, binding, url, merged, target))
    receipts, _ = S.load_merge_receipts(state_dir)
    if not any(all(r.get(k) == v for k, v in expected.items()) for r in receipts):
        raise Refusal("swarm merge did not persist the expected receipt")
    intent["phase"] = "receipt_recorded"
    durable_write(intent_path, intent)
    return cmd["advance"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--approver", required=True, type=nonempty)
    parser.add_argument("--root")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-unchecked-scope", type=nonempty, metavar="REASON")
    parser.add_argument("--abandon-intent", type=nonempty, metavar="OPERATION_ID")
    parser.add_argument("--reason", type=nonempty)
    args = parser.parse_args(argv)
    if args.pr <= 0:
        parser.error("--pr must be a positive PR number")
    if args.abandon_intent and not args.reason:
        parser.error("--abandon-intent requires --reason")
    if args.reason and not args.abandon_intent:
        parser.error("--reason requires --abandon-intent")
    try:
        plan = read_object(args.plan)
        # Validate external paths BEFORE even opening a coordinator lock.
        authority(args, plan)
        if args.dry_run:
            reconcile(args, plan)
            return 2
        ok, holder = S.acquire_lease(args.state_dir)
        if not ok:
            raise Refusal("coordinator lock unavailable: {}".format(holder))
        try:
            advance = reconcile(args, plan)
        finally:
            S.release_lease(args.state_dir)
        if advance is None:
            return 0
        # advance owns the same lease itself; never hold it across this child.
        result = run(advance)
        print(result.stdout, end="")
        print("Merge receipt recorded and advance ran.")
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, argparse.ArgumentTypeError,
            subprocess.SubprocessError, S.PlanError, S.OutboxError,
            CP.PathPolicyError) as exc:
        print("REFUSED: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
