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
from urllib.parse import quote, urlsplit

import skill_paths

_ORCHESTRATE_DIR = os.environ.get("HANIG_ORCHESTRATE_DIR") or Path(__file__).parents[1]
_SWARM_DIR = skill_paths.sibling_skill_root(
    _ORCHESTRATE_DIR, "hanig-orchestrate", "hanig-swarm")
sys.path.insert(0, str(_SWARM_DIR / "scripts"))
import child_environment as CE
import coordinator_paths as CP
import swarm as S
import verify as V


SWARM = str(_SWARM_DIR / "scripts" / "swarm.py")
PR_FIELDS = "number,url,state,headRefOid,baseRefName,mergeCommit"


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
    epoch, error = S._read_state_epoch(args.state_dir)
    if error:
        raise Refusal(error)
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
    # Both anchors are coordinator-held. A dictionary key alone does not
    # establish that its contents belong to the current attempt or remote.
    facts = (us.get("attempt_launch_facts") or {}).get(attempt)
    if not isinstance(facts, dict):
        raise Refusal("no current attempt launch facts in coordinator state")
    for key in ("unit_id", "attempt_id", "repository_remote", "repo",
                "base_commit", "base_tree", "branch"):
        if not facts.get(key) or facts[key] != launch.get(key):
            raise Refusal("launch facts disagree with current launch intent: " + key)
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
    # repo_path is the forge's owner/name, not a filesystem checkout.
    # Local Git reads use launch["repo"], captured by the coordinator.
    host, repo_path = forge_route(remote)
    binding = {"unit": args.unit, "attempt": attempt, "head": head,
               "repo": remote, "target": launch["target_branch"], "pr": args.pr}
    # Keep the legacy journal key stable: the epoch is an observation fence,
    # not a new operation, and adding it to binding would hide old intents.
    scope_binding = {"unit": args.unit, "attempt": attempt, "head": head,
                     "base": launch["base_commit"], "repository": remote,
                     "target": launch["target_branch"], "state_epoch": epoch}
    return state_dir, str(root), binding, host, repo_path, scope_binding, launch["repo"]


def commands(args, root, binding, host, repo_path):
    route = host + "/" + repo_path
    pr = str(args.pr)
    return {
        "view": ["gh", "pr", "view", pr, "--repo", route, "--json", PR_FIELDS],
        "target": ["gh", "api", "--hostname", host,
                   "repos/{}/git/ref/heads/{}".format(repo_path, quote(binding["target"], safe="/"))],
        "scope": [sys.executable, SWARM, "scope-check", args.plan,
                  "--state-dir", args.state_dir, "--unit", args.unit, "--json"],
        "checks": ["gh", "pr", "checks", pr, "--repo", route,
                   "--json", "name,state"],
        "merge": ["gh", "pr", "merge", pr, "--repo", route,
                  "--squash", "--match-head-commit", binding["head"]],
        "advance": [sys.executable, SWARM, "advance", args.plan,
                    "--state-dir", args.state_dir, "--root", root],
    }


def observed_target(command, target):
    """Read the anchored branch ref; PR base metadata can remain stale."""
    ref = json.loads(run(command).stdout)
    if not isinstance(ref, dict) or ref.get("ref") != "refs/heads/" + target:
        raise Refusal("forge returned a different or invalid target branch ref")
    obj = ref.get("object")
    if not isinstance(obj, dict) or obj.get("type") != "commit":
        raise Refusal("target branch ref does not identify a commit")
    # oid validates without trimming, case-folding or shortening the value.
    return oid(obj.get("sha"))


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


def receipt_command(args, binding, url, merged, target, integration_status):
    return [sys.executable, SWARM, "merge", "--state-dir", args.state_dir,
            "--unit", args.unit, "--pr", url, "--head", binding["head"],
            "--target", binding["target"], "--target-commit", target,
            "--merged-as", merged, "--method", "squash", "--repo", binding["repo"],
            "--integration-status", integration_status]


def validate_scope_report(report, code, binding):
    """Validate every exit's complete report before considering an exception."""
    if not isinstance(report, dict):
        raise Refusal("scope-check returned an invalid scope report")
    for key, expected in binding.items():
        if report.get(key) != expected:
            raise Refusal("scope-check binding differs from coordinator state: " + key)
    if type(report.get("state_epoch")) is not int:
        raise Refusal("scope-check returned an invalid state epoch")
    oid(report.get("base"))
    statuses = {0: "in_scope", 1: "out_of_scope", 2: "unchecked"}
    if report.get("status") != statuses.get(code):
        raise Refusal("scope-check returned an invalid scope report status/exit")
    for key in ("scope", "out_of_scope", "deletions_out_of_scope"):
        if key not in report:
            raise Refusal("scope-check returned an invalid scope report: missing " + key)
        value = report[key]
        # No declared scope is the producer's explicit null, only unchecked.
        if key == "scope" and value is None and code == 2:
            continue
        if not isinstance(value, list) or any(not isinstance(p, str) or not p for p in value):
            raise Refusal("scope-check returned an invalid scope report: " + key)
    outside, deletions = report["out_of_scope"], report["deletions_out_of_scope"]
    if (code in (0, 2) and (outside or deletions)
            or code == 1 and not outside
            or not set(deletions).issubset(outside)):
        raise Refusal("scope-check returned an invalid scope report path lists")


def integration_evidence(state_dir, binding, repo, target):
    """Admit coordinator evidence under the exact observed target's policy."""
    policy, policy_digest, _digest, error = V.merge_precondition_policy(
        S.U.run, repo, target)
    if error:
        return None, error
    admitted, error = S.admit_verification(
        state_dir, binding["unit"], V.INTEGRATION_CLAIM, binding["head"],
        policy_digest, policy, repo=repo, base_commit=target, target_commit=target)
    if error:
        return None, error
    # A retained pass must never mask a later red run of the same candidate.
    # A repaired head or a different target is a new binding, not a waiver.
    for receipt in S.load_verifications(state_dir)[0]:
        if (receipt.get("result") == "fail"
                and all(receipt.get(k) == admitted.get(k) for k in (
                    "unit", "claim", "verifier", "verifier_sha256", "policy_sha256",
                    "subject_head", "produced_head", "target_commit", "merge_base", "candidate_tree"))):
            return None, "the candidate merge verifier returned FAIL for this exact binding"
    stability, error = V.admit_stability(
        S.U.run, repo, target, admitted, binding["unit"],
        S.load_verifications(state_dir)[0])
    if error:
        return None, error
    if stability is not None:
        admitted = dict(admitted, **{V.STABILITY_CLAIM: stability})
    return admitted, None


def retained_integration_problem(preconditions, evidence, binding, repo, target):
    """Validate the retained claim set without needing cleaned-up Git objects.

    Older intents lack the captured requirement list. When their target policy
    is still available, honor its stability declaration too; an old integration
    receipt alone cannot label that merge candidate-verified.
    """
    required = preconditions.get("required_merge_claims")
    if required is None:
        policy, _pd, error = V.read_policy(
            S.U.run, repo, target, source="target commit")
        if error:
            # Preserve already-admitted legacy reconciliation after Git cleanup.
            # Every new intent captures its required claims before any merge.
            return None
        required = [V.INTEGRATION_CLAIM]
        if V.stability_declarations(policy):
            required.append(V.STABILITY_CLAIM)
    if required not in ([V.INTEGRATION_CLAIM], [V.INTEGRATION_CLAIM, V.STABILITY_CLAIM]):
        return "invalid retained merge claim requirements"
    if V.STABILITY_CLAIM in required:
        shared = evidence.get(V.STABILITY_CLAIM)
        if not isinstance(shared, dict):
            return "retained integration evidence lacks changed-tests-stable"
        if (shared.get("result") != "pass" or shared.get("exit_code") != 0
                or shared.get("claim") != V.STABILITY_CLAIM
                or shared.get("verifier") != V.STABILITY_CLAIM
                or shared.get("authorization_commit") != target
                or shared.get("unit") != binding["unit"]
                or any(shared.get(k) != evidence.get(k) for k in (
                    "subject_head", "policy_sha256") + V.MERGE_BASIS_FIELDS)):
            return "retained changed-tests-stable does not match the integration candidate"
    return None


def verification_hint(args):
    return "Run the target-authorized verifier (no merge): " + shlex.join([
        sys.executable, str(Path(__file__).resolve()), args.plan,
        "--state-dir", args.state_dir, "--unit", args.unit, "--pr", str(args.pr),
        "--approver", args.approver, "--verify-integration"] +
        (["--root", args.root] if args.root else []))


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
    """Follow durable resolutions from the legacy binding-derived operation.

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
                intent.get("binding") != binding or intent.get("root") != root):
            raise Refusal("durable merge intent conflicts with current authority/root")
        # A cancellation write can fail after rename but before directory fsync.
        # Only the separate commit marker witnesses successful cancellation
        # fsync. A visible cancelled phase alone cannot resolve a pending write.
        rollback_path = path.with_suffix(".cancellation-pending")
        if rollback_path.exists():
            original = read_object(rollback_path)
            if (original.get("binding") != binding or original.get("root") != root
                    or original.get("operation_id") != operation_id
                    or original.get("phase") != "merge_requested"):
                raise Refusal("invalid pending cancellation record")
            commit_path = path.with_suffix(".cancellation-committed")
            if commit_path.exists():
                committed = read_object(commit_path)
                if (committed != intent or committed != dict(
                        original, phase="cancelled_before_request",
                        cancellation=committed.get("cancellation"))):
                    raise Refusal("cancellation commit does not match its retained intent")
                if repair:
                    # The marker may have been visible before its own directory
                    # fsync failed. Its publication still proves the cancellation
                    # fsync succeeded; persist the marker before cleaning up.
                    durable_write(commit_path, committed)
                    rollback_path.unlink()
            else:
                if repair:
                    durable_write(path, original)
                    rollback_path.unlink()
                intent = original
        if intent is not None and intent.get("phase") == "cancelled_before_request":
            if (intent.get("operation_id") != operation_id
                    or intent.get("schema_version") != 1
                    or not isinstance(intent.get("cancellation"), dict)):
                raise Refusal("invalid pre-request cancellation record")
            cancellation = intent["cancellation"]
            nonempty(cancellation.get("reason", ""))
            datetime.fromisoformat(cancellation["observed_at"])
            if cancellation.get("observed_target") is not None:
                oid(cancellation["observed_target"])
            operation_id = hashlib.sha256(
                (operation_id + ":after-cancellation").encode()).hexdigest()
            continue
        if not abandonment_path.exists():
            # Legacy metadata never gated ordinary MERGED reconciliation.
            # Only our new resolution marker requires a companion record.
            if intent is not None and intent.get("phase") == "resolved_by_abandonment":
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


def abandon(args, intent_path, intent, pr, operation_id):
    record_path = intent_path.with_name(
        "merge-abandonment-" + operation_id + ".json")
    record = {"schema_version": 1, "operation_id": operation_id,
              "intent": intent, "approver": args.approver, "reason": args.reason,
              "observed_at": datetime.now(timezone.utc).isoformat(), "observed_pr": pr}
    durable_write(record_path, record)
    resolved = dict(intent, phase="resolved_by_abandonment", abandonment=record_path.name)
    durable_write(intent_path, resolved)
    print("Abandoned merge intent {}; record: {}. No merge or advance ran.".format(
        operation_id, record_path))


def cancel_before_request(intent_path, intent, observed, reason):
    """Resolve only a request this invocation has not transmitted.

    The pending record keeps even a post-rename fsync failure unresolved on
    rerun. Publish a separate commit marker only AFTER cancellation's directory
    fsync succeeds, so repair can distinguish committed cleanup from rollback.
    Keep the marker: pending removal may itself be lost in a later crash.
    """
    rollback_path = intent_path.with_suffix(".cancellation-pending")
    durable_write(rollback_path, intent)
    resolved = dict(intent, phase="cancelled_before_request", cancellation={
        "observed_target": observed, "reason": reason,
        "observed_at": datetime.now(timezone.utc).isoformat()})
    try:
        durable_write(intent_path, resolved)
    except OSError:
        # Best effort restores the on-disk phase now; the pending record also
        # fences reruns if storage refuses this restoration.
        durable_write(intent_path, intent)
        raise
    # Do not roll back after starting marker publication. Even if its directory
    # fsync fails, a visible marker witnesses the already durable cancellation.
    durable_write(intent_path.with_suffix(".cancellation-committed"), resolved)
    rollback_path.unlink()


def reconcile(args, plan):
    state_dir, root, binding, host, repo_path, scope_binding, repo = authority(args, plan)
    cmd = commands(args, root, binding, host, repo_path)
    operation_id, intent_path, intent = current_intent(
        state_dir, binding, root, repair=not args.dry_run)
    if args.abandon_intent and (
            args.abandon_intent != operation_id or intent is None
            or intent.get("phase") not in ("merge_requested", "merged")):
        raise Refusal("named operation is not the unit's current unresolved intent")
    if args.dry_run:
        print("DRY RUN: no forge calls, writes, receipt or advancement; exit 2.")
        print("+ " + shlex.join(cmd["scope"]))
        print("require exact coordinator binding and state epoch before forge access")
        print("+ " + shlex.join(cmd["view"]))
        print("+ " + shlex.join(cmd["target"]))
        print("require target-authorized integration-tests at exact head, target, "
              "merge base and candidate tree, plus changed-tests-stable when declared "
              "by target policy; no override")
        if args.verify_integration:
            print("run the target's pinned verifier in a disposable candidate merge; "
                  "record all target-required claims as coordinator evidence only")
            return None
        if args.abandon_intent:
            print("if OPEN at judged head: persist abandonment record and resolve {}; "
                  "no merge or advance".format(intent_path))
        else:
            print("if OPEN with no prior merge request:")
            print("+ " + shlex.join(cmd["checks"]))
            print("re-observe the exact target branch ref immediately before persisting the intent")
            print("+ " + shlex.join(cmd["target"]))
            print("persist intent {} before the conditional merge".format(intent_path))
            print("re-read the target ref; on movement/read failure durably cancel before request")
            print("+ " + shlex.join(cmd["target"]))
            print("+ " + shlex.join(cmd["merge"]))
            print("+ " + shlex.join(cmd["view"]))
        print("if MERGED at judged head (including reconciliation):")
        print("+ " + shlex.join(parent_command(host, repo_path, "<merged-sha>")))
        print("+ " + shlex.join(receipt_command(
            args, binding, "<observed-pr-url>", "<merged-sha>", "<merge-parent-sha>",
            "<integration-status>")))
        print("+ " + shlex.join(cmd["advance"]))
        return None

    # Identity is mandatory even for a named scope-policy exception or
    # already-merged reconciliation. Check it before the first forge read.
    scope = run(cmd["scope"], allowed=(0, 1, 2))
    try:
        scope_report = json.loads(scope.stdout)
    except ValueError:
        raise Refusal("scope-check returned malformed JSON; binding unavailable")
    validate_scope_report(scope_report, scope.returncode, scope_binding)

    # Refuse an unreadable receipt journal before any merge request.
    receipts, _ = S.load_merge_receipts(state_dir)
    pr = json.loads(run(cmd["view"]).stdout)
    check_pr(pr, binding)
    url = observed_pr_url(pr, host, repo_path, args.pr)
    if args.verify_integration:
        if pr["state"] != "OPEN" or intent:
            raise Refusal("verification requires an OPEN PR with no unresolved merge intent")
        S.load_verifications(state_dir)
        target = observed_target(cmd["target"], binding["target"])
        # Return only coordinator/forge inputs captured under the lease. The
        # caller releases it before running the disposable candidate verifier.
        return repo, {"binding": binding, "scope_binding": scope_binding,
                      "state_dir": state_dir, "root": root,
                      "operation_id": operation_id, "target": target,
                      "plan_digest": S.plan_digest(plan)}
    observation = {"approver": args.approver, "already_merged": pr["state"] == "MERGED",
                   "scope_exit": scope.returncode, "scope": scope_report,
                   "scope_stdout": scope.stdout, "scope_stderr": scope.stderr}
    if args.abandon_intent and pr["state"] == "OPEN":
        if intent["phase"] != "merge_requested":
            raise Refusal("cannot abandon an intent that already observed a merge")
        abandon(args, intent_path, intent, pr, operation_id)
        return None
    if pr["state"] == "OPEN":
        if intent:
            raise Refusal("earlier merge request has an unresolved outcome; refusing a second merge call")
        if scope.returncode != 0 and not (
                scope.returncode == 2 and scope_report["status"] == "unchecked"
                and args.allow_unchecked_scope):
            raise Refusal("scope-check exited {}: {}".format(scope.returncode, scope.stdout))
        checks = json.loads(run(cmd["checks"]).stdout)
        if not isinstance(checks, list) or not checks or any(
                not isinstance(c, dict) or c.get("state") != "SUCCESS" for c in checks):
            raise Refusal("at least one CI check is required and every check must be SUCCESS")
        target = observed_target(cmd["target"], binding["target"])
        evidence, error = integration_evidence(state_dir, binding, repo, target)
        if error:
            raise Refusal("integration-tests precondition: " + error + ". " + verification_hint(args))
        latest = json.loads(run(cmd["view"]).stdout)
        check_pr(latest, binding)
        if latest["state"] != "OPEN":
            raise Refusal("PR or target moved during preflight; rerun against the current target. "
                          + verification_hint(args))
        observation.update({"allow_unchecked_scope": args.allow_unchecked_scope,
                            "checks": checks, "integration": evidence,
                            "required_merge_claims": [V.INTEGRATION_CLAIM] +
                            ([V.STABILITY_CLAIM] if V.STABILITY_CLAIM in evidence else [])})
        intent = {"schema_version": 1, "operation_id": operation_id,
                  "binding": binding, "root": root, "phase": "merge_requested",
                  "preconditions": observation, "target_before_request": target}
        # Avoid publishing an intent for a move already visible in preflight.
        if observed_target(cmd["target"], binding["target"]) != target:
            raise Refusal("target moved during preflight; rerun against the current target. "
                          + verification_hint(args))
        durable_write(intent_path, intent)
        observed = None
        try:
            # This is the final external observation before transmission. In
            # particular, no intent fsync belongs between this read and merge.
            observed = observed_target(cmd["target"], binding["target"])
            if observed != target:
                raise Refusal("target moved after intent publication")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # Command diagnostics may span lines; only this display reason is
            # flattened. The observed ref comparison above always uses raw IDs.
            reason = " ".join(str(exc).splitlines()) or type(exc).__name__
            cancel_before_request(intent_path, intent, observed, reason)
            raise Refusal("cancelled before merge request: " + reason + ". "
                          + verification_hint(args))
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
    # An already-admitted precondition survives crashes and local Git cleanup.
    # Its target must be the actual parent, not PR metadata or today's ref tip.
    evidence = (intent or {}).get("preconditions", {}).get("integration")
    integration_problem = None
    if evidence is not None:
        if (evidence.get("target_commit") != target
                or (intent or {}).get("target_before_request") != target):
            integration_problem = "target moved between integration check and merge"
        elif evidence.get("subject_head") != binding["head"] or evidence.get("result") != "pass":
            integration_problem = "retained integration evidence does not match the judged head"
        else:
            integration_problem = retained_integration_problem(
                intent["preconditions"], evidence, binding, repo, target)
    else:
        evidence, integration_problem = integration_evidence(state_dir, binding, repo, target)
    integration_status = ("integration-unverified" if integration_problem else "candidate-verified")
    # The actual merge parent survives crashes and target movement. Neither PR
    # metadata nor the current branch ref can reconstruct that historical parent.
    if intent is None:
        intent = {"schema_version": 1, "operation_id": operation_id,
                  "binding": binding, "root": root}
    intent.update({"operation_id": operation_id,
                   "phase": "merged", "merged_as": merged, "target_commit": target,
                   "reconciliation": observation,
                   "integration_status": integration_status,
                   "integration_problem": integration_problem})
    durable_write(intent_path, intent)
    expected = {"unit": args.unit, "repo": binding["repo"], "pr": url,
                "target": binding["target"], "head": binding["head"],
                "merged_as": merged, "method": "squash", "target_commit": target,
                "merged": True, "attested": True, "integration_status": integration_status}
    if not any(all(r.get(k) == v for k, v in expected.items()) for r in receipts):
        run(receipt_command(args, binding, url, merged, target, integration_status))
    receipts, _ = S.load_merge_receipts(state_dir)
    if not any(all(r.get(k) == v for k, v in expected.items()) for r in receipts):
        raise Refusal("swarm merge did not persist the expected receipt")
    # Correct an already-persisted audit label too. Do not alter the unit's
    # judgment or erase old journal entries; append-only history is retained.
    state = read_object(state_dir / S.STATE_FILE)
    us = state["units"][args.unit]
    prior = us.get("merge_receipt")
    if isinstance(prior, dict) and all(prior.get(k) == expected[k] for k in (
            "unit", "repo", "pr", "target", "head", "merged_as")):
        corrected = dict(prior, target_commit=target, integration_status=integration_status)
        if corrected != prior:
            us["merge_receipt"] = corrected
            S.save_state(state_dir, state)
    intent["phase"] = "receipt_recorded"
    durable_write(intent_path, intent)
    if integration_problem:
        print("WARNING: INTEGRATION-UNVERIFIED: " + integration_problem, file=sys.stderr)
        if intent.get("preconditions", {}).get("integration") is not None:
            raise Refusal("merge occurred but its integration precondition is invalid; "
                          "receipt retained, advancement withheld")
    return cmd["advance"]


def verify_integration(args, repo, snapshot):
    """Run without the lease using the repo extracted by the authority reader.

    Like integration_evidence, this helper receives the local repository as an
    explicit argument; it introduces no repository-key reader of its own.
    """
    binding, target = snapshot["binding"], snapshot["target"]
    evidences, error = V.run_merge_preconditions(
        S.U.run, repo, binding["head"], target,
        timeout=args.verification_timeout)
    if error and not evidences:
        raise Refusal(error + ". " + verification_hint(args))

    ok, holder = S.acquire_lease(args.state_dir)
    if not ok:
        raise Refusal("verification evidence not recorded: coordinator lock unavailable: {}. "
                      "Rerun verification. {}".format(holder, verification_hint(args)))
    try:
        try:
            # Read the plan again as well: an accepted plan edit must not be
            # hidden by this process's pre-verification in-memory copy.
            plan = read_object(args.plan)
            if S.plan_digest(plan) != snapshot["plan_digest"]:
                raise Refusal("plan changed during verification")
            current = authority(args, plan)
            state_dir, root, fresh, host, repo_path, scope_binding, fresh_repo = current
            for key, value in binding.items():
                if fresh.get(key) != value:
                    raise Refusal("coordinator binding changed during verification: " + key)
            # acquire_lease increments the epoch once for our own new lease.
            # Any other increment (even a no-op coordinator pass) invalidates
            # the observation; never refresh the saved epoch to accept it.
            expected_scope = dict(snapshot["scope_binding"])
            expected_scope["state_epoch"] += 1
            for key, value in expected_scope.items():
                if scope_binding.get(key) != value:
                    raise Refusal("coordinator binding changed during verification: " + key)
            if (state_dir != snapshot["state_dir"] or root != snapshot["root"]
                    or fresh_repo != repo):
                raise Refusal("coordinator paths changed during verification")
            operation_id, _, intent = current_intent(state_dir, fresh, root)
            if intent or operation_id != snapshot["operation_id"]:
                raise Refusal("merge intent changed during verification")
            S.load_merge_receipts(state_dir)
            S.load_verifications(state_dir)
            cmd = commands(args, root, fresh, host, repo_path)
            pr = json.loads(run(cmd["view"]).stdout)
            check_pr(pr, fresh)
            observed_pr_url(pr, host, repo_path, args.pr)
            if pr["state"] != "OPEN":
                raise Refusal("PR is no longer OPEN after verification")
            if observed_target(cmd["target"], fresh["target"]) != target:
                raise Refusal("target moved during verification")
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                subprocess.SubprocessError, S.PlanError, S.OutboxError,
                CP.PathPolicyError) as exc:
            raise Refusal("verification evidence not recorded: {}. Rerun verification. {}".format(
                exc, verification_hint(args))) from exc
        for evidence in evidences:
            evidence.update({"unit": binding["unit"], "by": args.approver,
                             "at": datetime.now(timezone.utc).isoformat()})
            problem = S._verify_shape_problem(evidence)
            if problem:
                raise Refusal(problem)
        for evidence in evidences:
            S._fsync_append(state_dir / S.VERIFY_RECEIPTS, evidence)
    finally:
        S.release_lease(args.state_dir)
    for evidence in evidences:
        print("{}: {} for head {} into target {} (no merge requested)".format(
            evidence["claim"], evidence["result"].upper(),
            evidence["subject_head"], evidence["target_commit"]))
    if error:
        raise Refusal(error + ". " + verification_hint(args))
    if any(evidence["result"] != "pass" for evidence in evidences):
        raise Refusal("candidate merge verifier failed; repair the candidate")


def print_pending_close(args, plan):
    """Display the durable obligation; neither apply it nor acknowledge it."""
    try:
        state = read_object(Path(args.state_dir) / S.STATE_FILE)
        attempt = state["units"][args.unit].get("attempt_dir")
        intents = S.load_outbox_contract(args.state_dir)
        status, _ = S.acknowledgment_status(args.state_dir)
    except (OSError, ValueError, KeyError, S.OutboxError) as exc:
        print("WARNING: pending tracker close could not be read: {}".format(exc))
        return
    pending = [i for i in intents
               if i["project"] == (plan.get("name") or "swarm")
               and i["unit"] == args.unit and i["verb"] == "close"
               and i.get("attempt_dir") == attempt
               and status.get(i["key"], (S.UNACKNOWLEDGED, []))[0] == S.UNACKNOWLEDGED]
    if not pending:
        print("No unacknowledged close intent for unit {} in its current attempt; "
              "inspect outbox if synchronization is unresolved.".format(args.unit))
    for intent in pending:
        tracker = ("tracker={!r}".format(intent["tracker"]) if "tracker" in intent
                   else "no tracker declared")
        print("Pending tracker close: key={} {}; unit={}".format(
            intent["key"], tracker, args.unit))
        print("After the session applies this intent, replace ID with the tracker "
              "reference and record its attested acknowledgment:")
        print(shlex.join([sys.executable, SWARM, "outbox", "--state-dir", args.state_dir,
                          "--record-receipt", intent["key"], "--ref", "ID"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--approver", required=True, type=nonempty)
    parser.add_argument("--root")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-integration", action="store_true",
                        help="run all target-authorized merge verifiers and record evidence; never merge")
    parser.add_argument("--verification-timeout", type=int, default=900)
    parser.add_argument("--allow-unchecked-scope", type=nonempty, metavar="REASON")
    parser.add_argument("--abandon-intent", type=nonempty, metavar="OPERATION_ID")
    parser.add_argument("--reason", type=nonempty)
    args = parser.parse_args(argv)
    if args.verify_integration and args.abandon_intent:
        parser.error("--verify-integration cannot abandon an intent")
    if args.verification_timeout <= 0:
        parser.error("--verification-timeout must be positive")
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
        if args.verify_integration:
            verify_integration(args, *advance)
            return 0
        if advance is None:
            return 0
        # advance owns the same lease itself; never hold it across this child.
        result = run(advance)
        print(result.stdout, end="")
        print("Merge receipt recorded and advance ran.")
        print_pending_close(args, plan)
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, argparse.ArgumentTypeError,
            subprocess.SubprocessError, S.PlanError, S.OutboxError,
            CP.PathPolicyError) as exc:
        print("REFUSED: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
