#!/usr/bin/env python3
"""Read-only comparison of merged PRs, merge journals and tracker obligations.

An observation, not a merge gate: same-credential bypass remains possible.
Only coordinator-held launch anchors bind sources; merge journals are audit
records, never authority. Unbound or unreadable state cannot certify clean.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

# Reading state must not create cache files, even in an installed copy there.
sys.dont_write_bytecode = True
import merge_unit as M

S = M.S

QUERY = """query($owner:String!, $name:String!, $branch:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(first:100, after:$cursor, states:MERGED, baseRefName:$branch,
                 orderBy:{field:UPDATED_AT, direction:DESC}) {
      nodes { number url state baseRefName headRefOid mergedAt updatedAt
              mergeCommit { oid } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("expected ISO-8601 timestamp with timezone")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def repository(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        value = "https://github.com/" + value
    return M.forge_route(value)


def command(argv):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=60)
    if result.returncode:
        raise ValueError("{} exited {}: {}".format(argv[0], result.returncode, result.stderr.strip()))
    return result.stdout


@contextmanager
def regular_input(path, optional=False):
    """Refuse special files before reading, including a FIFO with no writer.

    The strict coordinator readers own journal parsing; their preflight here
    only validates file type. Atomic trusted-writer replacements stay regular.
    This is not a boundary against concurrent hostile same-UID replacement.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        if not optional or path.is_symlink():
            raise
        yield None
        return
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("input is not a regular file: " + str(path))
        yield handle


def object_at(path):
    with regular_input(path) as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("expected object at " + str(path))
    return value


def source_repositories(state):
    """Read coordinator anchors, including retained attempts, never worker files.

    No anchors means unknown, including a newly allocated/legacy empty state.
    A multi-repository state is deliberately not a clean source for one repo.
    """
    units = state.get("units")
    if not isinstance(units, dict):
        raise ValueError("coordinator state has no units mapping")
    found = set()
    for unit in units.values():
        if not isinstance(unit, dict):
            raise ValueError("invalid coordinator unit")
        for field in ("attempt_launch_facts", "attempt_launch_intents"):
            attempts = unit.get(field, {})
            if not isinstance(attempts, dict):
                raise ValueError("invalid " + field)
            for anchor in attempts.values():
                if not isinstance(anchor, dict):
                    raise ValueError("invalid launch anchor")
                found.add(repository(anchor.get("repository_remote")))
    if not found:
        raise ValueError("coordinator state has no repository launch anchor")
    return found


def merge_records(directory):
    # iterdir raises on unreadable directories; glob can suppress that failure.
    records = []
    for path in sorted(directory.iterdir()):
        if not (path.name.startswith("merge-unit-") and path.name.endswith(".json")):
            continue
        record = object_at(path)
        binding = record.get("binding")
        if not isinstance(binding, dict):
            raise ValueError("merge record has no binding: " + str(path))
        route = repository(binding.get("repo"))
        if (type(binding.get("pr")) is not int or binding["pr"] <= 0
                or not isinstance(binding.get("target"), str) or not binding["target"]):
            raise ValueError("invalid PR/target binding: " + str(path))
        M.oid(binding.get("head"))
        phase = record.get("phase")
        if phase not in ("merge_requested", "merged", "receipt_recorded",
                         "resolved_by_abandonment", "cancelled_before_request"):
            raise ValueError("unknown merge record phase: " + str(path))
        if phase in ("merged", "receipt_recorded"):
            M.oid(record.get("merged_as"))
        records.append((route, record))
    return records


def covers(records, route, branch, pr):
    # PullRequest.headRefOid retains the PR head after merge; headRef.target
    # follows the live branch. Never replace the historical field with that
    # moving ref when comparing a recorded merge.
    for record_route, record in records:
        binding = record["binding"]
        if (record_route == route and binding["target"] == branch
                and binding["pr"] == pr["number"] and binding["head"] == pr["headRefOid"]):
            # A pending intent may have merged with its response lost. Cancelled
            # and abandoned requests do not account for a later direct merge.
            if record["phase"] == "merge_requested":
                return True
            if (record["phase"] in ("merged", "receipt_recorded")
                    and record["merged_as"] == pr["mergeCommit"]["oid"]):
                return True
    return False


def merged_prs(route, branch, since, limit):
    """Select by merge time, not creation time or later comment activity.

    GitHub orders this connection by updatedAt. An unseen PR cannot have
    merged after its update time, so the last update bounds unseen merges.
    Pagination is observational, not an atomic forge snapshot.
    """
    host, path = route
    owner, name = path.split("/")
    cursor, seen_cursors, seen_numbers, result = None, set(), set(), []
    previous_update = None
    while True:
        argv = ["gh", "api", "graphql", "--hostname", host, "-f", "query=" + QUERY,
                "-f", "owner=" + owner, "-f", "name=" + name, "-f", "branch=" + branch]
        if cursor is not None:
            argv += ["-f", "cursor=" + cursor]
        payload = json.loads(command(argv))
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ValueError("forge returned GraphQL errors or invalid response")
        connection = payload["data"]["repository"]["pullRequests"]
        nodes, page = connection["nodes"], connection["pageInfo"]
        if not isinstance(nodes, list) or not isinstance(page, dict) or type(page.get("hasNextPage")) is not bool:
            raise ValueError("invalid forge page")
        for pr in nodes:
            if (not isinstance(pr, dict) or type(pr.get("number")) is not int
                    or pr["number"] <= 0 or pr.get("state") != "MERGED"
                    or pr.get("baseRefName") != branch or pr["number"] in seen_numbers):
                raise ValueError("invalid, duplicate or retargeted merged PR")
            M.oid(pr.get("headRefOid"))
            M.oid(pr["mergeCommit"]["oid"])
            M.observed_pr_url(pr, host, path, pr["number"])
            merged, updated = timestamp(pr.get("mergedAt")), timestamp(pr.get("updatedAt"))
            if updated < merged or (previous_update is not None and updated > previous_update):
                raise ValueError("forge ordering changed during pagination; rerun")
            previous_update = updated
            seen_numbers.add(pr["number"])
            if since is None or merged >= since:
                result.append(pr)
        result.sort(key=lambda pr: (timestamp(pr["mergedAt"]), pr["number"]), reverse=True)
        if limit is not None:
            result = result[:limit]
        if not page["hasNextPage"]:
            return result
        if not nodes:
            raise ValueError("empty nonterminal forge page")
        if since is not None and previous_update < since:
            return result
        if limit is not None and len(result) == limit and previous_update < timestamp(result[-1]["mergedAt"]):
            return result
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("invalid or repeated forge cursor")
        seen_cursors.add(cursor)


def reconcile(args):
    report = {"observed_at": datetime.now(timezone.utc).isoformat(),
              "repository": None, "branch": args.branch, "findings": [], "errors": [],
              "sources": [], "merged_prs_checked": 0}
    try:
        route = repository(args.repo or command(["git", "remote", "get-url", "origin"]).strip())
        report["repository"] = "/".join(route)
        since = timestamp(args.since) if args.since else None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report["errors"].append({"source": "input", "detail": str(exc)})
        return report
    records = []
    for directory in args.state_dir:
        path = Path(directory)
        source = {"state_dir": str(path), "status": "unreadable"}
        report["sources"].append(source)
        try:
            anchors = source_repositories(object_at(path / S.STATE_FILE))
            if anchors != {route}:
                source["status"] = "mismatched"
                report["findings"].append({"kind": "MISMATCHED SOURCE", "state_dir": str(path),
                                           "repositories": sorted("/".join(a) for a in anchors)})
            else:
                source["status"] = "read"
            local_records = merge_records(path)
            for name in (S.OUTBOX, S.RECEIPTS):
                with regular_input(path / name, optional=True):
                    pass
            intents = S.load_outbox_contract(path)
            statuses, problems = S.acknowledgment_status(path)
            if problems:
                raise ValueError("receipt journal was not completely read: " + str(problems))
            if source["status"] == "read":
                records.extend(local_records)
            for intent in intents:
                key = intent["envelope"]["idempotency_key"]
                status = statuses.get(key, (S.UNACKNOWLEDGED, []))[0]
                if status == S.CONFLICT:
                    raise ValueError("conflicting tracker receipts for " + key)
                if status == S.UNACKNOWLEDGED and source["status"] == "read":
                    report["findings"].append({"kind": "UNACKNOWLEDGED OBLIGATION", "state_dir": str(path),
                                               "key": key, "operation": intent["envelope"]["requested_operation"]})
        except (OSError, ValueError, TypeError, KeyError, RecursionError, S.OutboxError) as exc:
            source["status"] = "unreadable"
            report["errors"].append({"source": str(path), "detail": str(exc)})
    try:
        prs = merged_prs(route, args.branch, since, args.limit)
        report["merged_prs_checked"] = len(prs)
        for pr in prs:
            if not covers(records, route, args.branch, pr):
                report["findings"].append({"kind": "UNMEDIATED MERGE", "pr": pr["number"],
                                           "url": pr["url"], "merged_as": pr["mergeCommit"]["oid"]})
    except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError) as exc:
        report["errors"].append({"source": "forge", "detail": str(exc)})
    return report


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="OWNER/REPO or forge remote; default: checkout origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--state-dir", action="append", required=True)
    parser.add_argument("--since", help="inclusive ISO-8601 timestamp with timezone")
    parser.add_argument("--limit", type=positive, help="most recently merged N PRs (after --since if supplied)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.since is None and args.limit is None:
        parser.error("supply --since and/or --limit")
    report = reconcile(args)
    code = 2 if report["errors"] else 1 if report["findings"] else 0
    report["status"] = "UNREADABLE" if code == 2 else "FLAGGED" if code == 1 else "CLEAN"
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("{} at {}: {} ({})".format(report["status"], report["observed_at"], report["repository"], report["branch"]))
        for finding in report["findings"]:
            print(finding["kind"] + ": " + json.dumps(finding, sort_keys=True))
        for error in report["errors"]:
            print("UNREADABLE SOURCE: " + error["source"] + ": " + error["detail"])
    return code


if __name__ == "__main__":
    sys.exit(main())
