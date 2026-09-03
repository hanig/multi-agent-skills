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

And one property the tracker itself cannot enforce (plan item B8, second
half). The plan's DAG is carried into the tracker as `blockedBy` relations,
and that API is APPEND-ONLY through this interface: Linear exposes
`removeBlockedBy` as a SEPARATE operation. So when a unit's `needs` list
shrinks, the edge that no longer exists stays behind, the tracker says an
issue is blocked by something the plan no longer says blocks it, and nothing
notices. The draft therefore carries BOTH directions -- `add_blocked_by` and
`remove_blocked_by` -- and it computes them by diffing the plan against what
the tracker actually holds, which the applying session must read back and
hand in. Not against the last draft: the last draft records what was ASKED
FOR, and this is meant to verify the write rather than trust it.

When no read-back is supplied, `remove_blocked_by` is `null` and never `[]`.
Unknown is not empty. `[]` would read as "the tracker holds no stale edges",
which is a claim nothing on this machine can make.

And the read-back itself is ATTESTED, not verified, for the same reason the
outbox receipts are: there is no network here, so what arrives is a session
saying what it saw. That is a much better basis than diffing against the last
draft -- it is at least a claim about the tracker rather than about our own
intentions -- but it is not proof, so every rendering says attested.

Standard library only, no network.
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

DRAFT = "tickets.json"
# The Linear team to file under when a brief does not name one.
#
# The workspace is "Arc - projects" and its teams are Arc, peeks and SRAgent.
# Cluster and lab work belongs to Arc, alongside Entwine, CSA-RNA-FM and
# MultiDep. Without this the drafter asked every time, and once guessed
# "goodarzilab" -- which is the SLURM ACCOUNT from the plan's charge_to, not a
# Linear team at all. Two different namespaces that happen to look alike.
DEFAULT_TEAM = "Arc"

# Creating a tracker project and filing issues is OUTWARD-FACING: other people
# see it, and undoing it is manual. So it waits for a human by default, and
# the draft says so in a field a session cannot overlook.
#
# One phrase turns the whole run automatic. It is deliberately not a word
# anybody types by accident, and not "yes" or "go", which appear in ordinary
# conversation and would make the gate meaningless.
AUTOPILOT_PHRASE = "swarm autopilot"
# 2: the draft grew `blocked_by_sync` and the per-issue `add_blocked_by` /
# `remove_blocked_by` / `blocked_by_in_sync` fields. A consumer that only
# knows schema 1 applies `blocked_by` and silently never removes anything,
# so it must be able to tell.
SCHEMA = 2

# Where an applying session leaves what the tracker ACTUALLY holds. It may
# arrive as `--tracker-edges FILE` or be written into the draft under this
# key, which is how the tracker ids already come back (see 6(g) below).
# Read this word carefully; the same care the outbox receipts needed. There
# is no network here, so a read-back is a SESSION SAYING what the tracker
# holds, and anyone who can write the file can write anything. It is a far
# better basis than diffing against the previous draft -- that is a claim
# about our own intentions, this is at least a claim about the tracker -- but
# it is not proof, and the label has to carry the weakness rather than the
# reader having to remember it.
ATTESTED = ("attested by the session that read the tracker, not verified: "
            "this script has no network and cannot check it")
READBACK = "tracker_readback"
READBACK_SCHEMA = 1
READBACK_SHAPE = ('{"schema_version": 1, "read_at": "<ISO 8601>", '
                  '"source": "<what produced it>", '
                  '"edges": {"<issue>": ["<blocker>", ...]}}')
HOW_TO_SUPPLY = (
    "the session holding the tracker connector must list every issue in this "
    "project with its blockedBy relations and pass them as "
    "`tickets.py draft <plan> --tracker-edges FILE`, or write the same object "
    f"into the draft under {READBACK!r} (it is carried forward across "
    f"re-drafts). Shape: {READBACK_SHAPE}. Each key is the BLOCKED issue and "
    "each value the issues blocking it; any of them may be named by unit id, "
    "by tracker identifier (e.g. ARC-236) or by tracker uuid. An issue with "
    "no blockedBy relations must appear with an empty list -- omitting it is "
    "indistinguishable from not having looked.")


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


def readback_edges(rb):
    """Validate a tracker read-back. Returns (edges, meta, reason).

    `edges` is None whenever the read-back cannot be believed, and `reason`
    then says why in words a human can act on. NOTHING here degrades a
    malformed read-back into an empty one: an empty read-back means "the
    tracker holds no edges", which is the opposite of "I could not tell".
    """
    if rb is None:
        return None, None, "absent: no tracker read-back was supplied"
    if not isinstance(rb, dict):
        return None, None, (f"unreadable: the read-back is a "
                            f"{type(rb).__name__}, not an object")
    ver = rb.get("schema_version", READBACK_SCHEMA)
    if not isinstance(ver, int) or ver > READBACK_SCHEMA:
        return None, None, (f"unreadable: read-back schema_version {ver!r} is "
                            f"newer than this script understands "
                            f"({READBACK_SCHEMA}), so its edges may not mean "
                            f"what they appear to mean")
    edges = rb.get("edges")
    if not isinstance(edges, dict):
        return None, None, ("unreadable: the read-back carries no `edges` "
                            "object")
    read_at = rb.get("read_at")
    if not isinstance(read_at, str) or not read_at.strip():
        # A read-back with no timestamp cannot be scoped, and an unscoped
        # claim of sync is the thing this whole field exists to prevent.
        return None, None, ("unreadable: the read-back carries no `read_at`, "
                            "so a claim built on it could not say WHEN the "
                            "tracker looked like this")
    clean = {}
    for holder, blockers in edges.items():
        if not isinstance(holder, str) or not holder.strip():
            return None, None, (f"unreadable: {holder!r} is not usable as an "
                                f"issue handle")
        if isinstance(blockers, str) or not isinstance(blockers, (list, tuple)):
            return None, None, (f"unreadable: the blockers of {holder!r} are "
                                f"not a list")
        out = []
        for b in blockers:
            if not isinstance(b, str) or not b.strip():
                return None, None, (f"unreadable: {holder!r} lists {b!r} as a "
                                    f"blocker, which is not a handle")
            out.append(b.strip())
        clean[holder.strip()] = list(dict.fromkeys(out))
    meta = {"read_at": read_at.strip(), "source": rb.get("source")}
    return clean, meta, None


def _handles(issue):
    for h in (issue.get("unit"), issue.get("linear_id"),
              issue.get("identifier")):
        if isinstance(h, str) and h.strip():
            yield h.strip()


def _alias_map(issues, prior_issues=()):
    """Every handle the tracker might use, mapped back to a unit id.

    PRIOR issues are included on purpose. A stale edge usually points at a
    unit the plan DELETED, so the current draft cannot name it; the draft
    that filed it can. Without this, the one edge B8 exists to remove is the
    one edge that resolves to nothing.
    """
    aliases = {}
    # Current issues first: ids are carried forward keyed on unit, so a handle
    # cannot legitimately change units, and if one ever appears to, the live
    # draft is the one to believe.
    for issue in list(issues) + list(prior_issues):
        unit = issue.get("unit")
        if not isinstance(unit, str) or not unit:
            continue
        for h in _handles(issue):
            aliases.setdefault(h, unit)
            aliases.setdefault(h.upper(), unit)
    return aliases


def sync_blocked_by(out, readback, prior_issues=()):
    """Fill in both directions of every issue's blockedBy edges, in place.

    The plan's declared edges stay in `blocked_by`, unchanged, because that
    is what the issue body states. What the applying session must DO is split
    out: `add_blocked_by` (declared, not held) and `remove_blocked_by` (held,
    no longer declared). The second one is the point: the relation API is
    append-only through this interface, so not-adding an edge never removes
    it, and Linear's `removeBlockedBy` has to be called deliberately.
    """
    issues = out.get("issues") or []
    edges, meta, reason = readback_edges(readback)
    if edges is None:
        for i in issues:
            i["add_blocked_by"] = list(i.get("blocked_by") or [])
            # UNKNOWN IS NOT EMPTY. `[]` here would render an absent read-back
            # as "the tracker holds no stale edges", which is precisely the
            # class of bug this repo keeps fighting: unknown reading as fine.
            i["remove_blocked_by"] = None
            i["blocked_by_in_sync"] = None
        out["blocked_by_sync"] = {
            "state": "absent" if reason.startswith("absent") else "unreadable",
            "read_at": None,
            "source": None,
            "reason": reason,
            "basis": ATTESTED,
            # Named separately from `reason` so a reader is never left
            # inferring what the two lists below being empty means.
            "in_sync": None,
            "not_read_back": sorted(i["unit"] for i in issues),
            "unresolved": [],
            "how_to_supply": HOW_TO_SUPPLY,
        }
        return out

    aliases = _alias_map(issues, prior_issues)
    held, unresolved, seen = {}, [], set()
    for holder_raw, blockers in edges.items():
        holder = aliases.get(holder_raw) or aliases.get(holder_raw.upper())
        if holder is None:
            # Only when it actually HOLDS edges. A hand-created issue sitting
            # in the same tracker project with no blockers cannot be holding
            # a stale one, and flagging it would be the cries-wolf failure
            # that gets a guard deleted.
            if blockers:
                unresolved.append({
                    "holder": holder_raw, "blocker": None,
                    "why": ("the tracker reports blockedBy edges on an issue "
                            "that no unit in this plan and no issue this "
                            "draft has ever filed maps to, so there is "
                            "nothing here to compare them against")})
            continue
        seen.add(holder)
        for b_raw in blockers:
            held.setdefault(holder, {})[b_raw] = (
                aliases.get(b_raw) or aliases.get(b_raw.upper()))

    for issue in issues:
        unit = issue["unit"]
        declared = list(dict.fromkeys(issue.get("blocked_by") or []))
        if unit not in seen and issue.get("linear_id"):
            # The issue EXISTS in the tracker and the read-back never
            # mentioned it. That is not "it has no edges"; it is "nobody
            # looked". Omitting an edgeless issue is indistinguishable from
            # skipping it, so the shape requires an empty list and this
            # refuses to guess which one happened.
            issue["add_blocked_by"] = declared
            issue["remove_blocked_by"] = None
            issue["blocked_by_in_sync"] = None
            continue
        tracker = held.get(unit, {})
        have = {u for u in tracker.values() if u is not None}
        issue["add_blocked_by"] = [d for d in declared if d not in have]
        # REMOVAL IS NAMED IN THE TRACKER'S OWN VOCABULARY. A stale edge
        # points at something the plan no longer declares -- sometimes a unit
        # that was deleted outright -- so a current unit id cannot always
        # identify it. The handle the read-back used can, and the session that
        # supplied it is the session that will call removeBlockedBy with it.
        issue["remove_blocked_by"] = [raw for raw, u in tracker.items()
                                      if u is None or u not in declared]
        for raw, u in tracker.items():
            if u is None:
                unresolved.append({
                    "holder": unit, "blocker": raw,
                    "why": ("the plan declares no such dependency and this "
                            "draft cannot say what the handle refers to. It "
                            "is listed for removal because the plan's DAG is "
                            "authoritative over this issue's blockers; if it "
                            "is a deliberate link to work outside this "
                            "project, say so and it must move out of "
                            "blockedBy")})
        issue["blocked_by_in_sync"] = not (issue["add_blocked_by"]
                                           or issue["remove_blocked_by"])

    states = [i["blocked_by_in_sync"] for i in issues]
    out["blocked_by_sync"] = {
        "state": "read",
        "read_at": meta["read_at"],
        "source": meta.get("source"),
        "reason": None,
        "basis": ATTESTED,
        # Scoped to the read, never to now: this says the tracker agreed with
        # the plan AT `read_at`, which is the only thing a read can establish.
        "in_sync": None if None in states else all(states),
        "not_read_back": sorted(i["unit"] for i in issues
                                if i["blocked_by_in_sync"] is None),
        "unresolved": unresolved,
        "how_to_supply": HOW_TO_SUPPLY,
    }
    return out


def draft(plan, brief=None, existing=None, autopilot=False, readback=None):
    units = plan.get("units") or []
    out = {
        "schema_version": SCHEMA,
        "project": {
            # A HUMAN title, separate from the identifier. `plan.name` is an
            # identifier-shaped slug used for state paths and issue bodies, and
            # using it as the Linear project title forced renaming the plan to
            # get a readable title, which changes the identifier as a side
            # effect. The brief carries the title; the slug stays the slug.
            "name": ((brief or {}).get("name")
                     or (brief or {}).get("title")
                     or plan.get("name") or "unnamed-swarm-project"),
            "slug": plan.get("name") or "unnamed-swarm-project",
            "summary": (brief or {}).get("summary")
                       or f"Swarm project with {len(units)} units.",
            "description": (brief or {}).get("description") or "",
            "team": (brief or {}).get("team") or DEFAULT_TEAM,
            "linear_id": None,
            "url": None,
        },
        # The gate. A session must not create anything while this says
        # "required": the human has not seen the project overview or the
        # issue titles yet.
        "approval": {
            "state": "required",
            "granted_by": None,
            "at": None,
            "how_to_skip_next_time": (f"say {AUTOPILOT_PHRASE!r} in the "
                                      f"request to run end to end without "
                                      f"stopping here"),
        },
        "issues": [issue_for_unit(u, plan.get("name") or "?") for u in units],
    }
    if autopilot:
        out["approval"] = {"state": "autopilot", "granted_by": "autopilot "
                           "phrase in the request", "at": None,
                           "how_to_skip_next_time": None}
    if existing:
        # An approval already granted is NOT re-requested. Re-drafting after
        # a plan edit must not force the human through the gate again for
        # work they have already seen and accepted.
        # An approval covers the work the human SAW. If any unit's digest
        # changed, or units were added or removed, this is different work and
        # the gate re-arms. Carrying the approval forward unconditionally --
        # which is what I first built -- let a changed plan be filed on a yes
        # given for something else, including a one-run autopilot.
        prior = (existing.get("approval") or {}).get("state")
        was = {i.get("unit"): i.get("unit_digest")
               for i in (existing.get("issues") or [])}
        now = {i["unit"]: i.get("unit_digest") for i in out["issues"]}
        if prior in ("granted", "autopilot") and was == now:
            out["approval"] = existing["approval"]
        elif prior in ("granted", "autopilot"):
            changed = sorted(set(was) ^ set(now)) or sorted(
                u for u in now if was.get(u) != now[u])
            out["approval"] = {
                "state": "required", "granted_by": None, "at": None,
                "how_to_skip_next_time": (
                    f"the plan changed since approval ({', '.join(changed)}), "
                    f"so this is different work and needs a new yes")}
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

    # A read-back handed in on this run wins; otherwise carry forward whatever
    # the applying session last wrote into the draft, so a re-draft after a
    # plan edit does not throw away the only knowledge of the tracker's actual
    # edges that this machine has.
    prior_issues = (existing or {}).get("issues") or []
    if readback is None:
        readback = (existing or {}).get(READBACK)
    out[READBACK] = readback
    sync_blocked_by(out, readback, prior_issues)
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
        if not recorded:
            # A missing digest is NOT a pass. An issue written before digests
            # existed, or hand-edited, would otherwise be accepted however
            # stale it had become: the check would say "no drift" precisely
            # when it could not tell. A reviewer found this; unknown must fail
            # the check that exists to detect unknown.
            problems.append(
                f"the issue for {u['id']!r} carries no unit digest, so "
                f"whether it still describes this unit cannot be told. "
                f"Re-run `draft` to refresh it; tracker ids are kept.")
        elif recorded != unit_digest(u):
            problems.append(
                f"unit {u['id']!r} has changed since its issue was written "
                f"({recorded} -> {unit_digest(u)}), so the issue now "
                f"describes work that is no longer the work. Re-run `draft` "
                f"to refresh the body; ids are carried forward.")
    return problems


def is_filed(tickets):
    """Has anything actually been created in the tracker yet? Before that
    there are no edges to be stale, so a missing read-back is not a fault."""
    if (tickets.get("project") or {}).get("linear_id"):
        return True
    return any(i.get("linear_id") for i in (tickets.get("issues") or []))


def edge_problems(plan, tickets):
    """The stale-edge half of B8, checked rather than asserted.

    This repo has a scar from a docstring that claimed a test enforced an
    invariant when no such test existed, and every violation then had to be
    found by hand. So: the invariant is that the tracker's blockedBy edges
    equal the plan's, and it is decided from a READ of the tracker. With no
    read, the answer is "cannot tell", which is a problem and never a pass.
    """
    problems = []
    sync = tickets.get("blocked_by_sync")
    if not isinstance(sync, dict):
        if is_filed(tickets):
            problems.append(
                f"this draft carries no `blocked_by_sync` at all (schema "
                f"{tickets.get('schema_version')!r}), so whether the tracker "
                f"still holds blockedBy edges the plan no longer declares "
                f"cannot be told. Re-run `draft` to refresh it; tracker ids "
                f"are kept.")
        return problems

    if sync.get("state") != "read":
        if is_filed(tickets):
            # FAIL CLOSED. Issues exist in the tracker, the relation API is
            # append-only through this interface, and nothing here has looked
            # at what it holds. Reporting "in sync" from that position is the
            # exact bug: unknown rendered as fine.
            problems.append(
                f"the tracker's blockedBy edges were never read back "
                f"({sync.get('reason') or sync.get('state')}), so whether it "
                f"holds edges the plan no longer declares CANNOT BE TOLD. "
                f"This is not a claim that there are none. To resolve it, "
                f"{HOW_TO_SUPPLY}")
        return problems

    at = sync.get("read_at")
    for issue in (tickets.get("issues") or []):
        stale = issue.get("remove_blocked_by")
        if stale is None:
            problems.append(
                f"the tracker's blockedBy edges for {issue.get('unit')!r} "
                f"were not in the read-back taken at {at}, and that issue "
                f"already exists in the tracker, so its edges are unknown. An "
                f"issue with no blockers must appear with an empty list; "
                f"omitting it is indistinguishable from not having looked.")
        elif stale:
            problems.append(
                f"the tracker says {issue.get('unit')!r} is blocked by "
                f"{', '.join(repr(x) for x in stale)}, which the plan no "
                f"longer declares. `blockedBy` is append-only through this "
                f"interface, so these will NOT go away by not being re-added: "
                f"the applying session must call `removeBlockedBy` for each, "
                f"then re-read and re-draft.")
    for u in sync.get("unresolved") or []:
        problems.append(
            f"the tracker read-back names "
            f"{u.get('blocker') or u.get('holder')!r} which this draft "
            f"cannot resolve: {u.get('why')}")
    return problems


def report_edges(d):
    """Say what the applying session must do to the tracker's relations, in
    both directions, and say plainly when the second direction is unknown."""
    sync = d.get("blocked_by_sync") or {}
    adds = {i["unit"]: i.get("add_blocked_by") or [] for i in d["issues"]}
    adds = {k: v for k, v in adds.items() if v}
    if sync.get("state") != "read":
        if adds:
            print(f"  blockedBy to ADD: "
                  + "; ".join(f"{k} <- {', '.join(v)}"
                              for k, v in sorted(adds.items())))
        print(f"\n  BLOCKED-BY REMOVALS UNKNOWN ({sync.get('reason')}).")
        print("  `blockedBy` is append-only through this interface, so an "
              "edge the plan has\n  stopped declaring stays in the tracker "
              "until `removeBlockedBy` is called.")
        print("  This draft does NOT claim the tracker is in sync, and "
              "`remove_blocked_by` is\n  null rather than empty so nothing "
              "downstream can read the gap as agreement.")
        if not is_filed(d):
            # Nothing has been filed from this draft, so there is very likely
            # nothing to remove -- but "likely" comes from a LOCAL record of
            # what was filed, not from a read, so the field stays null.
            print("  Nothing in this draft carries a tracker id yet, so there "
                  "is probably nothing\n  there to remove. That is inferred "
                  "from this machine's own record, not read from\n  the "
                  "tracker, which is why it is not written down as a fact.")
        else:
            print(f"  To resolve: {HOW_TO_SUPPLY}")
        return
    removes = {i["unit"]: i.get("remove_blocked_by") or []
               for i in d["issues"]}
    removes = {k: v for k, v in removes.items() if v}
    print(f"  tracker blockedBy edges ATTESTED at {sync['read_at']}"
          + (f" ({sync['source']})" if sync.get("source") else "")
          + " -- a session's report of what it saw, not verified here")
    if adds:
        print("  ADD:    " + "; ".join(f"{k} <- {', '.join(v)}"
                                       for k, v in sorted(adds.items())))
    if removes:
        print("  REMOVE: " + "; ".join(f"{k} <- {', '.join(v)}"
                                       for k, v in sorted(removes.items())))
        print("  The removals are the point: not re-adding an edge does not "
              "delete it. Call\n  `removeBlockedBy` for each, then re-read "
              "and re-draft to verify the write.")
    if sync.get("not_read_back"):
        print("  UNKNOWN for: " + ", ".join(sync["not_read_back"])
              + " (filed, but absent from the read-back)")
    if not adds and not removes and not sync.get("not_read_back"):
        print("  blockedBy: the plan and the tracker agreed at that read, "
              "on that attestation")


def cmd_draft(args):
    plan, err = read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    brief, _ = read_json(args.brief) if args.brief else (None, None)
    if args.name:
        brief = dict(brief or {})
        brief["name"] = args.name
    if args.team:
        brief = dict(brief or {})
        brief["team"] = args.team
    readback = None
    if getattr(args, "tracker_edges", None):
        readback, rerr = read_json(args.tracker_edges)
        if rerr:
            # A read-back named on the command line and then not read must not
            # degrade into "absent". Absent is at least honest by accident;
            # this would be a human who believed they had supplied it.
            print(f"REFUSING: --tracker-edges {args.tracker_edges} could not "
                  f"be read ({rerr}).")
            print("  Without it there is no basis for removing a blockedBy "
                  "edge the plan no longer declares.")
            return 2
        _e, _m, why = readback_edges(readback)
        if _e is None:
            # Same reasoning one level in. A read-back that parses as JSON but
            # is not a read-back would land in the `unreadable` state, which
            # is honest but quiet; a human who passed the flag deliberately is
            # told to their face instead.
            print(f"REFUSING: --tracker-edges {args.tracker_edges} is not a "
                  f"usable tracker read-back ({why}).")
            print(f"  Expected {READBACK_SHAPE}")
            return 2
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
    d = draft(plan, brief, existing, autopilot=args.autopilot,
              readback=readback)

    problems = check(plan, d)
    if problems:
        print("REFUSING to write a draft that does not map cleanly:")
        for p in problems:
            print(f"  - {p}")
        return 2

    out_path.write_text(json.dumps(d, indent=2))
    known = sum(1 for i in d["issues"] if i["linear_id"])
    print(f"  drafted {len(d['issues'])} issue(s) for project "
          f"{d['project']['name']!r} under team {d['project']['team']!r} "
          f"-> {out_path}")
    if known:
        print(f"  {known} already exist in the tracker and will be UPDATED, "
              f"not recreated")
    report_edges(d)
    state = d["approval"]["state"]
    if state == "autopilot":
        print(f"\n  AUTOPILOT: the request said {AUTOPILOT_PHRASE!r}, so this "
              f"is cleared to file and dispatch without stopping.")
    else:
        print(f"\n  APPROVAL REQUIRED. Nothing has been sent anywhere and "
              f"nothing may be\n  created until a human has seen the project "
              f"overview and every issue\n  title. Show them in full, then "
              f"ask ONCE.")
        print(f"\n  To run end to end without this gate next time, say "
              f"{AUTOPILOT_PHRASE!r} in the request.")
    return 0


def cmd_approve(args):
    """Record that a human saw the draft and accepted it."""
    d, err = read_json(args.tickets)
    if err:
        sys.exit(f"error: no readable draft at {args.tickets}: {err}")
    d.setdefault("approval", {})
    d["approval"].update({"state": "granted", "granted_by": args.approver,
                          "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    Path(args.tickets).write_text(json.dumps(d, indent=2))
    print(f"  approved by {args.approver}: {len(d.get('issues') or [])} "
          f"issue(s) may now be created.")
    return 0


def cmd_check(args):
    plan, err = read_json(args.plan)
    if err:
        sys.exit(f"error: no readable plan at {args.plan}: {err}")
    tickets, err = read_json(args.tickets)
    if err:
        sys.exit(f"error: no readable draft at {args.tickets}: {err}. "
                 f"Run `tickets.py draft` first.")
    problems = check(plan, tickets) + edge_problems(plan, tickets)
    if not problems:
        sync = tickets.get("blocked_by_sync") or {}
        # SCOPED TO THE READ, NOT TO NOW. A read establishes what the tracker
        # held when it was taken; saying more than that is how "verified"
        # comes to mean "was verified once, about something else".
        when = (f", and its blockedBy edges as ATTESTED at "
                f"{sync['read_at']}" if sync.get("state") == "read" else "")
        print(f"  plan and tickets agree: {len(tickets.get('issues') or [])} "
              f"issue(s), one per unit{when}")
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
    d.add_argument("--autopilot", action="store_true",
                   help=f"the human said {AUTOPILOT_PHRASE!r}: file and "
                        f"dispatch without stopping for approval")
    d.add_argument("--team", default=None,
                   help=f"tracker team to file under (default: "
                        f"{DEFAULT_TEAM})")
    d.add_argument("--brief", default=None,
                   help="JSON with name/summary/description/team for the "
                        "project. `name` is the HUMAN title; the plan's own "
                        "name stays the identifier.")
    d.add_argument("--name", default=None,
                   help="the project's human title, e.g. 'scBaseCount shard "
                        "QC'. Without it the plan's identifier-shaped name is "
                        "used, which reads badly in a tracker.")
    d.add_argument("--out", default=None)
    d.add_argument("--tracker-edges", default=None, metavar="FILE",
                   help="JSON read-back of the blockedBy edges the tracker "
                        "ACTUALLY holds, from the session with the connector. "
                        f"Shape: {READBACK_SHAPE}. Without it the draft "
                        "cannot say which edges to REMOVE, and says so "
                        "rather than implying there are none.")
    d.set_defaults(fn=cmd_draft)
    a = sub.add_parser("approve", help="record a human's approval of a draft")
    a.add_argument("tickets")
    a.add_argument("--approver", required=True)
    a.set_defaults(fn=cmd_approve)

    c = sub.add_parser("check", help="verify the plan and the draft still agree")
    c.add_argument("plan")
    c.add_argument("tickets")
    c.set_defaults(fn=cmd_check)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
