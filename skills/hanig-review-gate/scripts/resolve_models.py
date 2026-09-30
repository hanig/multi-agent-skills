#!/usr/bin/env python3
"""Resolve each routing seat to its newest probed point release for a project.

Run at project start (hanig-project step 1), or alone:

    python3 "$HANIG_REVIEW_GATE_DIR/scripts/resolve_models.py" --project .

For every enabled seat in reviewers.json that declares a `family`, and for the
code-agent default in hanig-swarm/agents.json, it lists the provider catalog,
picks the greatest same-major point release above the shipped pin
(model_family.select), probes that exact id, and records it only when the
probe passes. It prints one line per seat that declares a family. The result is one snapshot per project under the state home;
the installed skill files are never modified. A newer generation is reported
as NEW_GENERATION and never chosen: raising a family's `major` is a reviewed
change. Exit 0 whenever every seat has a usable model, which includes keeping
its pin; exit 4 on a configuration or location error, or when the snapshot
cannot be written (this project's previous snapshot, if any, then stays).

This program is network-capable, like review.py. swarm.py must never import
it; it reads the snapshot through model_family only.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import model_family as MF  # noqa: E402
import review as R  # noqa: E402

# The probe goes through the gate's own call path, so the model answers under
# review.SYSTEM; the reply must parse as a review verdict, not just be text.
PROBE_PROMPT = ("Change under review: README.md gains the line 'Run the suite "
                "before merging.'\nClaim 1: The change adds one line of prose "
                "and no code.\nReturn your verdict.")
CANARY_PROMPT = "Reply with exactly OK and nothing else. Do not use any tools."
LISTING_URLS = {"openai": "https://api.openai.com/v1/models",
                "openrouter": "https://openrouter.ai/api/v1/models"}
LISTING_KEYS = {"openai": "OPENAI_API_KEY"}


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _get_json(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class PaseoError(RuntimeError):
    """A failed paseo command, carrying whatever JSON it printed anyway."""

    def __init__(self, message, payload=None):
        RuntimeError.__init__(self, message)
        self.payload = payload


def _json_after_banner(text):
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0],
                default=-1)
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except ValueError:
        return None


def _paseo(argv, timeout, cwd=None):
    """Run a paseo command; return parsed JSON from its stdout, or raise.

    A non-zero exit still carries any JSON it printed, so a run that created
    an agent and then failed can have that agent archived.
    """
    try:
        done = subprocess.run(["paseo"] + argv, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        raise PaseoError("paseo %s timed out after %ss" % (argv[0], timeout),
                         _json_after_banner(partial))
    payload = _json_after_banner(done.stdout or "")
    if done.returncode != 0:
        raise PaseoError("paseo %s exited %d: %s" % (
            argv[0], done.returncode, (done.stderr or done.stdout)[-300:].strip()),
            payload)
    if payload is None:
        raise PaseoError("paseo %s printed no JSON" % argv[0])
    return payload


def _shaped(value, kind, what):
    """Every external JSON value passes through here before it is read.

    A value of the wrong shape raises RuntimeError, which the callers turn
    into LISTING_FAILED or PROBE_FAILED, so the seat keeps its pin.
    """
    if not isinstance(value, kind):
        raise RuntimeError("%s returned %s, not %s" % (
            what, type(value).__name__, kind.__name__))
    return value


def _paseo_object(argv, timeout, cwd=None):
    return _shaped(_paseo(argv, timeout, cwd), dict, "paseo " + argv[0])


def _ids(rows, what):
    """Ids from a listing; any row without a string id fails the listing."""
    ids = set()
    for row in _shaped(rows, list, what):
        if not (isinstance(row, dict) and isinstance(row.get("id"), str)):
            raise RuntimeError("%s has a row without a string id: %r"
                               % (what, row)[:200])
        ids.add(row["id"])
    return sorted(ids)


def list_ids(listing, timeout):
    """Model ids a listing serves, or raise with the reason."""
    if listing.startswith(MF.PASEO_PREFIX):
        return _ids(_paseo(["provider", "models", listing[len(MF.PASEO_PREFIX):],
                            "--json"], timeout), "paseo provider models")
    headers = {}
    key_var = LISTING_KEYS.get(listing)
    if key_var:
        key = os.environ.get(key_var)
        if not key:
            raise RuntimeError("%s not set" % key_var)
        headers["Authorization"] = "Bearer " + key
    data = _shaped(_get_json(LISTING_URLS[listing], headers, timeout), dict,
                   listing + " model listing")
    return _ids(data.get("data"), listing + " model listing data")


def probe_reviewer(seat, candidate, timeout):
    """(record, None) when the exact candidate answered; (None, reason) otherwise."""
    call = R.PROVIDERS.get(seat["provider"])
    if call is None:
        return None, "unknown provider %r" % seat["provider"]
    response, error = call(dict(seat, model=candidate), PROBE_PROMPT, timeout)
    if error:
        return None, R.redact(str(error))
    tokens = response.get("out_tokens")
    if response.get("status") != "completed":
        return None, "status %r" % response.get("status")
    if not (response.get("text") or "").strip():
        return None, "empty text"
    if not (isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0):
        return None, "no output tokens"
    verdict, verdict_error = R.parse_verdict(response.get("text"))
    if verdict is None:
        return None, "reply is not a review verdict: %s" % verdict_error
    if response.get("served_model") != candidate:
        return None, "served %r, not %r" % (response.get("served_model"), candidate)
    record = {"date": utc_now()[:10],
              "max_output_tokens": seat.get("max_output_tokens"),
              "provider": seat["provider"], "model": candidate,
              "outcome": "completed",
              "input_tokens": response.get("in_tokens"),
              "output_tokens": tokens,
              "reasoning_tokens": response.get("reasoning_tokens"),
              "response_id": response.get("response_id"),
              "observed_in": "resolve_models.py probe at %s: a one-claim "
                             "review under review.SYSTEM; the reply parsed as a "
                             "verdict. Measures budget acceptance and a usable "
                             "verdict, not capacity for every review." % utc_now()}
    if seat.get("effort") is not None:
        record["effort"] = seat["effort"]
    return record, None


def _archive(agent):
    """Archive a canary; return None, or the reason it was not archived."""
    try:
        reply = _paseo_object(["archive", agent, "--json"], 60)
        if reply.get("status") != "archived":
            raise RuntimeError("paseo archive reported status %r"
                               % (reply.get("status"),))
        return None
    except Exception as exc:  # reported, never swallowed
        reason = str(exc)[:200] or type(exc).__name__
        print("resolve_models: canary %s was not archived: %s" % (agent, reason),
              file=sys.stderr)
        return reason


def probe_agent(route, model, thinking, timeout):
    """(inspect record, None) when paseo launched exactly this; (None, reason).

    One cleanup rule, whatever happens: when the attempt ends, every agent
    this canary may have created is archived, found by the id paseo printed
    and by the canary's unique title. A cleanup failure is printed and
    carried into the result.
    """
    canary = MF.snapshot_dir() / "canary"
    canary.mkdir(parents=True, exist_ok=True, mode=0o700)
    title = "resolve-canary-" + uuid.uuid4().hex[:12]
    printed = []
    outcome = (None, "canary did not run")
    try:
        outcome = _launch_and_inspect(route, model, thinking, timeout, title,
                                      canary, printed)
    except Exception as exc:
        outcome = (None, str(exc)[:300] or type(exc).__name__)
    finally:
        archive_error = _archive_orphans(printed[0] if printed else None,
                                         title, canary)
    record, why = outcome
    if archive_error:
        if record is None:
            why = "%s; canary left unarchived: %s" % (why, archive_error)
        else:
            record = dict(record, archive_error=archive_error)
    return record, why


def _launch_and_inspect(route, model, thinking, timeout, title, canary, printed):
    try:
        launched = _paseo_object(
            ["run", "--provider", route, "--model", model, "--thinking",
             thinking, "--title", title, "--wait-timeout", "%ds" % timeout,
             "--json", CANARY_PROMPT], timeout + 60, cwd=str(canary))
    except PaseoError as exc:
        if isinstance(exc.payload, dict) and isinstance(exc.payload.get("agentId"), str):
            printed.append(exc.payload["agentId"])
        raise
    agent = launched.get("agentId")
    if not (isinstance(agent, str) and agent):
        return None, "paseo run returned no agentId"
    printed.append(agent)
    seen = _paseo_object(["inspect", agent, "--json"], 60)
    got = {key: seen.get(key) for key in ("Provider", "Model", "Thinking")}
    want = {"Provider": route, "Model": model, "Thinking": thinking}
    if got != want:
        return None, "inspected %s, requested %s" % (got, want)
    return dict(got, agent_id=agent, date=utc_now()), None


def _archive_orphans(agent, title, cwd):
    """Archive by the printed id and by the unique title; None when all archived."""
    ids = {agent} if isinstance(agent, str) and agent else set()
    errors = []
    try:
        rows = _shaped(_paseo(["ls", "--json"], 60, cwd=str(cwd)), list, "paseo ls")
        for row in rows:
            if not (isinstance(row, dict) and row.get("name") == title):
                continue
            if isinstance(row.get("id"), str) and row["id"]:
                ids.add(row["id"])
            else:
                reason = "canary %s is listed with an unusable id %r" % (
                    title, row.get("id"))
                print("resolve_models: " + reason, file=sys.stderr)
                errors.append(reason)
    except Exception as exc:
        reason = "could not look up canary %s: %s" % (title, str(exc)[:150])
        print("resolve_models: " + reason, file=sys.stderr)
        errors.append(reason)
    errors += [error for error in (_archive(i) for i in sorted(ids)) if error]
    return "; ".join(errors) or None


def _pin_in_family(pin, family, what):
    version = MF.version_of(pin, MF.check_family(family))
    if version is None or version[0] != family["major"]:
        raise MF.FamilyError("%s: pinned %r is not in its family at major %d"
                             % (what, pin, family["major"]))


def validate_config(reviewers, agents):
    """Every configuration check, for every seat, before any seat starts.

    Raises MF.FamilyError naming the first defect; main() turns it into
    exit 4. Nothing has been listed, probed or launched when this runs.
    """
    for seat in reviewers:
        if seat.get("enabled", True) and seat.get("family") is not None:
            _pin_in_family(seat.get("model"), seat["family"], seat.get("name"))
            # A reviewer is probed through its own provider, so its catalog
            # must be that provider's; a reviewer never lists from paseo.
            if seat["family"].get("listing") != seat.get("provider"):
                raise MF.FamilyError("%s: family.listing %r must equal its "
                                     "provider %r" % (seat.get("name"),
                                     seat["family"].get("listing"),
                                     seat.get("provider")))
    if agents is None:
        return
    # The same rules swarm.py's load_agent_routing applies to this file;
    # tests/test_model_resolution.py runs both over one table of cases.
    if not isinstance(agents, dict):
        raise MF.FamilyError("agents.json must be a JSON object")
    default = agents.get("default")
    table = agents.get("thinking_by_model")
    if not isinstance(default, dict):
        raise MF.FamilyError("agents.json default must be an object")
    if not _routing_model(default.get("provider")):
        raise MF.FamilyError("agents.json default.provider must be a "
                             "PROVIDER/MODEL string without whitespace")
    if not _routing_token(default.get("thinking")):
        raise MF.FamilyError("agents.json default.thinking must be a "
                             "non-empty thinking id without whitespace")
    if not (isinstance(table, dict) and table and all(
            _routing_model(k) and _routing_token(v) for k, v in table.items())):
        raise MF.FamilyError("agents.json thinking_by_model must be a non-empty "
                             "map of PROVIDER/MODEL strings to thinking ids")
    if default.get("family") is not None:
        _pin_in_family(default["provider"].partition("/")[2], default["family"],
                       "agent-default")


def _routing_token(value):
    return (isinstance(value, str) and bool(value)
            and not any(c.isspace() for c in value))


def _routing_model(value):
    return (_routing_token(value) and len(value.split("/")) >= 2
            and all(value.split("/")))


def resolve(args):
    MF.check_state_location(args.project)  # before any canary directory exists
    reviewers = R.load_reviewers()
    agents = None  # only when the file is absent; a present file must be an object
    if R.AGENTS_CONFIG.exists():
        try:
            agents = json.loads(R.AGENTS_CONFIG.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise MF.FamilyError("%s is not valid JSON: %s" % (R.AGENTS_CONFIG, exc))
        if not isinstance(agents, dict):
            raise MF.FamilyError("%s must be a JSON object" % R.AGENTS_CONFIG)
    validate_config(reviewers, agents)
    listings, lines = {}, []
    snapshot = {"schema": MF.SCHEMA,
                "project": os.path.realpath(args.project),
                "project_key": MF.project_key(args.project),
                "config_sha256": MF.config_digests(R.CONFIG, R.AGENTS_CONFIG),
                "resolved_at": utc_now(), "reviewers": {}, "agent_default": None}

    def listed(listing):
        if listing not in listings:
            try:
                listings[listing] = (list_ids(listing, args.timeout), None)
            except Exception as exc:  # any listing failure keeps the pins
                listings[listing] = (None, R.redact(
                    "%s: %s" % (type(exc).__name__, exc))[:200])
        return listings[listing]

    def report(status, seat, pin, detail):
        lines.append({"status": status, "seat": seat, "pinned": pin,
                      "detail": detail})

    seats = [r for r in reviewers if r.get("enabled", True) and r.get("family")]
    if args.only:
        seats = [r for r in seats if r["name"] in args.only]
    for seat in seats:
        _isolated(report, seat["name"], seat["model"], _resolve_reviewer,
                  seat, listed, report, snapshot, args)

    default = (agents or {}).get("default") or {}
    if default.get("family") and (not args.only or "agent-default" in args.only):
        _isolated(report, "agent-default", default["provider"],
                  _resolve_agent_default, default, agents, listed, report,
                  snapshot, args)
    return _finish(args, snapshot, lines)


def _isolated(report, name, pinned, step, *step_args):
    """Run one seat's resolution; any failure there keeps that seat's pin.

    Configuration errors were raised before this point. Everything else a
    seat can meet (a provider or Paseo reply of an unexpected shape, an
    exception inside a provider call, an unparseable id) is that seat's
    SEAT_FAILED and never aborts the other seats.
    """
    try:
        step(*step_args)
    except Exception as exc:
        report("SEAT_FAILED", name, pinned, R.redact(
            "%s: %s" % (type(exc).__name__, exc))[:300])


def _resolve_reviewer(seat, listed, report, snapshot, args):
    family = seat["family"]
    ids, error = listed(family["listing"])
    if error:
        report("LISTING_FAILED", seat["name"], seat["model"], error)
        return
    chosen, newer = MF.select(ids, family, seat["model"])
    if newer:
        report("NEW_GENERATION", seat["name"], seat["model"],
               "listed above major %d, not adopted: %s" % (
                   family["major"], ", ".join(newer)))
    if chosen is None:
        report("PINNED", seat["name"], seat["model"], "no newer point release")
        return
    if args.dry_run:
        report("WOULD_PROBE", seat["name"], seat["model"], chosen)
        return
    record, why = probe_reviewer(seat, chosen, args.timeout)
    if record is None:
        report("PROBE_FAILED", seat["name"], seat["model"],
               "%s: %s" % (chosen, why))
        return
    snapshot["reviewers"][seat["name"]] = {"model": chosen, "record": record}
    report("RESOLVED", seat["name"], seat["model"], chosen)


def _resolve_agent_default(default, agents, listed, report, snapshot, args):
    family = default["family"]
    route, _slash, pin_model = default["provider"].partition("/")
    table = (agents or {}).get("thinking_by_model") or {}
    ids, error = listed(family["listing"])
    if error:
        report("LISTING_FAILED", "agent-default", default["provider"], error)
    else:
        chosen, newer = MF.select(ids, family, pin_model)
        if newer:
            report("NEW_GENERATION", "agent-default", default["provider"],
                   "listed above major %d, not adopted: %s" % (
                       family["major"], ", ".join(newer)))
        if chosen is None:
            report("PINNED", "agent-default", default["provider"],
                   "no newer point release")
        elif args.dry_run:
            report("WOULD_PROBE", "agent-default", default["provider"],
                   route + "/" + chosen)
        else:
            thinking = MF.agent_thinking(default, table, route + "/" + chosen)
            probe, why = probe_agent(route, chosen, thinking, args.timeout)
            if probe is None:
                report("PROBE_FAILED", "agent-default", default["provider"],
                       "%s/%s: %s" % (route, chosen, why))
            else:
                snapshot["agent_default"] = {"provider": route + "/" + chosen,
                                             "probe": probe}
                report("RESOLVED", "agent-default", default["provider"],
                       route + "/" + chosen)


def _finish(args, snapshot, lines):
    path = None
    if not args.dry_run:
        if args.only:
            # A partial run keeps the other seats' entries from a snapshot
            # that still matches this project and config; readers re-check
            # every entry anyway.
            previous = _matching_snapshot(args.project, snapshot)
            for name, entry in (previous.get("reviewers") or {}).items():
                if name not in args.only:
                    snapshot["reviewers"].setdefault(name, entry)
            if "agent-default" not in args.only and previous.get("agent_default"):
                snapshot["agent_default"] = previous["agent_default"]
        try:
            path = MF.write_snapshot(args.project, snapshot)
        except OSError:
            # write_snapshot replaces atomically, so a failed write leaves
            # this project's previous snapshot untouched. It stays in force:
            # it is a probed, config-matching record for THIS project, and
            # removing it could expose an ancestor project's snapshot.
            if MF.snapshot_path(args.project).exists():
                print("resolve_models: snapshot not written; this project's "
                      "previous snapshot %s stays in force"
                      % MF.snapshot_path(args.project), file=sys.stderr)
            raise
    return lines, path


def _matching_snapshot(project, fresh):
    try:
        data = json.loads(MF.snapshot_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not (isinstance(data, dict) and data.get("schema") == fresh["schema"]
            and data.get("project_key") == fresh["project_key"]
            and data.get("config_sha256") == fresh["config_sha256"]):
        return {}
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=".",
                    help="project directory the snapshot is keyed to")
    ap.add_argument("--only", action="append", metavar="SEAT",
                    help="resolve just this seat (repeatable; agent-default "
                         "names the code-agent default)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list and select only: no probe, no snapshot")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not Path(args.project).is_dir():
        print("error: --project %s is not a directory" % args.project,
              file=sys.stderr)
        sys.exit(4)
    try:
        lines, path = resolve(args)
    except (MF.FamilyError, OSError) as exc:  # configuration or state location
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(4)
    if args.json:
        print(json.dumps({"seats": lines,
                          "snapshot": str(path) if path else None}, indent=1))
    else:
        for line in lines:
            print("%-15s %-16s %s  %s" % (line["status"], line["seat"],
                                          line["pinned"], line["detail"]))
        print("snapshot: %s" % (path if path else "not written (dry run)"))
    sys.exit(0)


if __name__ == "__main__":
    main()
