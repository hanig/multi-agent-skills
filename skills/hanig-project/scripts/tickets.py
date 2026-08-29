#!/usr/bin/env python3
"""tickets.py: turn a swarm plan into a tracker DRAFT, and keep the two honest.

This script NEVER talks to a tracker. It runs where the coordinator runs, which
is a shared cluster login node, and a tracker token must not live there. It
emits a draft; a Claude Code session holding the MCP connector reads that draft,
shows it, and creates everything after ONE approval. The same separation the
swarm outbox uses, for the same reason.

Two properties it exists to enforce, from the plan's step 6:

  c. Every issue maps to one or more unit predicates, and every unit maps back
     to one issue, so neither can drift silently from the other.
  g. Re-running against an existing project updates rather than duplicating,
     keyed on the project and unit ids.

Standard library only, no network.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

DRAFT = "tickets.json"
SCHEMA = 1


def read_json(path):
    try:
        return json.loads(Path(path).read_text()), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as e:
        return None, str(e)


# The fields whose change makes an existing issue body WRONG. Comparing ids
# alone said "no drift" when a unit's declared outputs had changed underneath
# the issue that describes them, which a reviewer caught.
BODY_FIELDS = ("kind", "command", "prompt", "outputs", "needs", "gpu_hours",
               "promote_to", "description")


def unit_digest(u):
    payload = json.dumps({k: u.get(k) for k in BODY_FIELDS},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def issue_for_unit(u, plan_name):
    """One issue per unit. The BODY states the predicate, because an issue
    whose done-condition is prose is an issue two people will disagree about."""
    outputs = u.get("outputs") or []
    needs = u.get("needs") or []
    kind = u.get("kind", "slurm")
    if kind == "slurm":
        basis = ("a terminal, OK, OWNED sacct row for this attempt's job, AND "
                 "every declared output present in the attempt's exclusive "
                 "write root")
    elif kind == "pipeline":
        basis = ("the engine's recorded exit status is 0, AND every declared "
                 "output is present. The status is written by the launcher "
                 "wrapper, NOT attested by a scheduler")
    else:
        basis = ("the agent is idle in paseo AND every declared output is "
                 "present. `idle` is a lifecycle state, not a claim of "
                 "success, and the agent's own report is not an input")

    body = [
        f"**TL;DR** {u.get('description') or u['id']}",
        "",
        "### Done means",
        f"`swarm.py` marks `{u['id']}` DONE when {basis}.",
        "",
        f"- kind: `{kind}`",
        f"- declared outputs: {', '.join(f'`{o}`' for o in outputs) or 'NONE DECLARED'}",
    ]
    if needs:
        body.append(f"- blocked by: {', '.join(f'`{n}`' for n in needs)}")
    if u.get("gpu_hours"):
        body.append(f"- budget: {u['gpu_hours']} GPU-hours per attempt")
    if u.get("promote_to"):
        body.append(f"- promotes to: `{u['promote_to']}` (requires explicit "
                    f"human approval; never automatic)")
    body += [
        "",
        "### Command",
        "```sh",
        str(u.get("command") or u.get("prompt") or "").strip() or "(none)",
        "```",
        "",
        "---",
        f"Filed from the swarm plan `{plan_name}`. Issue state follows unit "
        f"state, never the reverse. **This issue is not closed on a "
        f"self-report**: closure requires the unit's predicate verdict.",
    ]
    return {
        "unit": u["id"],
        "unit_digest": unit_digest(u),
        "title": f"{u['id']}: {u.get('description') or u.get('title') or u['id']}",
        "body": "\n".join(body),
        "blocked_by": needs,
        "linear_id": None,        # filled in by the session after creation
        "identifier": None,
    }


def draft(plan, brief=None, existing=None):
    units = plan.get("units") or []
    out = {
        "schema_version": SCHEMA,
        "project": {
            "name": plan.get("name") or "unnamed-swarm-project",
            "summary": (brief or {}).get("summary")
                       or f"Swarm project with {len(units)} units.",
            "description": (brief or {}).get("description") or "",
            "team": (brief or {}).get("team"),
            "linear_id": None,
            "url": None,
        },
        "issues": [issue_for_unit(u, plan.get("name") or "?") for u in units],
    }
    if existing:
        # 6(g): re-running must UPDATE, not duplicate. Carry forward every id
        # we already know, keyed on the unit.
        by_unit = {i.get("unit"): i for i in (existing.get("issues") or [])}
        for issue in out["issues"]:
            prior = by_unit.get(issue["unit"])
            if prior:
                issue["linear_id"] = prior.get("linear_id")
                issue["identifier"] = prior.get("identifier")
        out["project"]["linear_id"] = (existing.get("project") or {}).get("linear_id")
        out["project"]["url"] = (existing.get("project") or {}).get("url")
    return out


def check(plan, tickets):
    """Both directions. A unit with no issue is work nobody can see; an issue
    with no unit is a promise nothing will ever verify."""
    problems = []
    unit_ids = {u["id"] for u in (plan.get("units") or [])}
    issue_units = [i.get("unit") for i in (tickets.get("issues") or [])]

    for uid in sorted(unit_ids - set(issue_units)):
        problems.append(f"unit {uid!r} has no issue: work nobody can see")
    for iu in sorted(set(issue_units) - unit_ids):
        problems.append(f"issue for {iu!r} has no unit: a promise nothing "
                        f"will ever verify")
    dupes = {u for u in issue_units if issue_units.count(u) > 1}
    for d in sorted(dupes):
        problems.append(f"unit {d!r} has more than one issue")

    by_unit = {i.get("unit"): i for i in (tickets.get("issues") or [])}
    for u in (plan.get("units") or []):
        if not (u.get("outputs") or []):
            problems.append(f"unit {u['id']!r} declares no outputs, so its "
                            f"issue can never be closed by a predicate")
        issue = by_unit.get(u["id"])
        if issue is None:
            continue
        recorded = issue.get("unit_digest")
        if recorded and recorded != unit_digest(u):
            problems.append(
                f"unit {u['id']!r} has changed since its issue was written "
                f"({recorded} -> {unit_digest(u)}), so the issue now "
                f"describes work that is no longer the work. Re-run `draft` "
                f"to refresh the body; ids are carried forward.")
    return problems


def cmd_draft(args):
    plan, err = read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    brief, _ = read_json(args.brief) if args.brief else (None, None)
    out_path = Path(args.out or (Path(args.plan).parent / DRAFT))
    existing, eerr = read_json(out_path)
    if eerr and eerr != "missing":
        # FAIL CLOSED. Discarding this error treated a present-but-corrupt
        # draft as absent, dropped every tracker id, and the next one-click
        # approval created a SECOND project and a duplicate of every issue in
        # a live shared workspace. Two reviewers found it; the blast radius is
        # someone else's tracker, so it refuses rather than guesses.
        print(f"REFUSING: {out_path} exists but cannot be read ({eerr}).")
        print("  It holds the tracker ids that make a re-run an UPDATE rather "
              "than a duplicate.")
        print("  Fix or remove it deliberately. Removing it means the next "
              "approval files everything again.")
        return 2
    d = draft(plan, brief, existing)

    problems = check(plan, d)
    if problems:
        print("REFUSING to write a draft that does not map cleanly:")
        for p in problems:
            print(f"  - {p}")
        return 2

    out_path.write_text(json.dumps(d, indent=2))
    known = sum(1 for i in d["issues"] if i["linear_id"])
    print(f"  drafted {len(d['issues'])} issue(s) for project "
          f"{d['project']['name']!r} -> {out_path}")
    if known:
        print(f"  {known} already exist in the tracker and will be UPDATED, "
              f"not recreated")
    print("\n  Nothing has been sent anywhere. A session holding the tracker "
          "connector\n  reads this file, shows it in full, and creates it "
          "after ONE approval.")
    return 0


def cmd_check(args):
    plan, err = read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    tickets, err = read_json(args.tickets)
    if err:
        sys.exit(f"error: no readable draft at {args.tickets}: {err}. "
                 f"Run `tickets.py draft` first.")
    problems = check(plan, tickets)
    if not problems:
        print(f"  plan and tickets agree: {len(tickets.get('issues') or [])} "
              f"issue(s), one per unit")
        return 0
    print("  plan and tickets have DRIFTED:")
    for p in problems:
        print(f"    - {p}")
    return 2


def main():
    ap = argparse.ArgumentParser(prog="tickets.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft", help="write a tracker draft from a plan")
    d.add_argument("plan")
    d.add_argument("--brief", default=None,
                   help="JSON with summary/description/team for the project")
    d.add_argument("--out", default=None)
    d.set_defaults(fn=cmd_draft)
    c = sub.add_parser("check", help="verify the plan and the draft still agree")
    c.add_argument("plan")
    c.add_argument("tickets")
    c.set_defaults(fn=cmd_check)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
