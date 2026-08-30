#!/usr/bin/env python3
"""Build the end-of-run report for a swarm project.

Every run ends with one of these. It is assembled from evidence already on
disk -- plan.json, the coordinator state, and each attempt's receipt -- and
never from a narrative account of what happened.

The distinction this file exists to preserve: a receipt says an artifact is
present and carries its digest, and its `basis` block says how strong that
claim is. A report that prints DONE and stops has thrown away the second half,
which is the half a reader needs in six weeks. So `basis` is rendered, and the
"not proven" line is rendered from the receipt's own admissions rather than
from an assurance written here.

Nothing in this file interprets results. It cannot: it does not know what a
column means. Where a project wants findings in the report it emits
findings.json, and those are rendered in a section labelled as the project's
claims, not the coordinator's evidence.

stdlib only, like the rest of the repo.
"""
import argparse
import datetime as _dt
import glob
import html
import json
import os
import sys

SCHEMA = 1
DEFAULT_STATE = os.path.join(".swarm", "state")
DEFAULT_RUNS = os.path.join(".swarm", "runs")

TERMINAL_OK = ("DONE",)
TERMINAL_BAD = ("FAILED", "INCOMPLETE", "NEEDS_HUMAN")


# --------------------------------------------------------------------------
# reading


def _load(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _load_lines(path):
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def collect(project):
    """Gather every evidence source. Missing ones are absences, not errors:
    a report on a half-finished run is exactly when this is most useful."""
    p = lambda *a: os.path.join(project, *a)
    state = _load(p(DEFAULT_STATE, "swarm-state.json"), {}) or {}
    plan = _load(p("plan.json"), {}) or {}

    receipts = {}
    for path in sorted(glob.glob(p(DEFAULT_RUNS, "*", "*", "receipt.json"))):
        r = _load(path)
        if not isinstance(r, dict):
            continue
        uid = r.get("task_id") or os.path.basename(
            os.path.dirname(os.path.dirname(path)))
        # Keep the newest receipt per unit: attempts are ordered on disk, and
        # an earlier failed attempt must not shadow the one that closed.
        prev = receipts.get(uid)
        if prev is None or str(r.get("checked_at", "")) >= str(
                prev.get("checked_at", "")):
            receipts[uid] = r

    return {
        "project": project,
        "plan": plan,
        "state": state,
        "receipts": receipts,
        "outbox": _load_lines(p(DEFAULT_STATE, "outbox.jsonl")),
        "tickets": _load(p("tickets.json"), {}) or {},
        "survey": _load(p(".swarm", "survey.json"), {}) or {},
        "findings": _load(p("findings.json")),
        "brief": _load(p("brief.json"), {}) or {},
    }


# --------------------------------------------------------------------------
# deriving


def unit_rows(data):
    plan_units = {u.get("id"): u for u in (data["plan"].get("units") or [])
                  if isinstance(u, dict)}
    state_units = data["state"].get("units") or {}
    rows = []
    for uid in list(plan_units) + [u for u in state_units
                                   if u not in plan_units]:
        pu = plan_units.get(uid, {})
        su = state_units.get(uid, {}) or {}
        rc = data["receipts"].get(uid, {}) or {}
        rows.append({
            "id": uid,
            "kind": pu.get("kind") or rc.get("kind") or "?",
            "state": _state_of(su, rc),
            "state_conflict": _state_conflict(su, rc),
            "job_id": su.get("job_id") or rc.get("job_id"),
            "attempts": len(su.get("attempts") or []),
            "max_attempts": pu.get("max_attempts"),
            "needs": pu.get("needs") or [],
            "declared": pu.get("outputs") or [],
            "outputs": rc.get("outputs") or {},
            "exit_code": rc.get("exit_code"),
            "basis": rc.get("basis") or {},
            "notes": rc.get("notes") or [],
            "checked_at": rc.get("checked_at"),
            "attempt_dir": su.get("attempt_dir"),
            "runtime": _runtime_of(data["plan"], pu),
        })
    return rows


def _state_of(su, rc):
    """The receipt wins.

    This used to be `su.get("state") or rc.get("state")`, so the coordinator's
    stored state outranked the receipt that judged the attempt. A unit whose
    receipt said FAILED but whose state said DONE was reported COMPLETE: a
    self-report beating evidence, inside the one document whose whole job is
    to stop that. Reviewer found it; it is the worst defect in this change.
    """
    if rc.get("state"):
        return rc["state"]
    return su.get("state") or "NOT STARTED"


def _state_conflict(su, rc):
    """Surface a disagreement rather than silently picking a side."""
    a, b = su.get("state"), rc.get("state")
    if a and b and a != b:
        return ("coordinator state says %s; the attempt's receipt says %s. "
                "The receipt is shown, because it judged the artifacts."
                % (a, b))
    return None


def _runtime_of(plan, unit):
    """What the unit declared it would execute in, and what checked it.

    A declaration nobody reads is worth as much as no declaration, so this is
    rendered next to the unit rather than left in the plan file. The
    verification label is copied verbatim: 'declared' and 'probe passed' are
    different claims and must not be flattened into one word."""
    rt = unit.get("runtime")
    if rt in (None, ""):
        return None
    if rt == "none":
        return {"entrypoint": "base image only", "verified_by": "declared none",
                "resolution": "none"}
    if isinstance(rt, str):
        rt = (plan.get("runtimes") or {}).get(rt) or {"id": rt}
    if not isinstance(rt, dict):
        return None
    return {"entrypoint": rt.get("entrypoint") or rt.get("id") or "?",
            "resolution": rt.get("resolution") or "?",
            "verified_by": rt.get("verified_by") or "UNDECLARED"}


def evidence_gaps(rows):
    """What the receipts decline to claim. Read off `basis`, never asserted
    here: if a future receipt proves more, this section shrinks on its own."""
    gaps = []
    for r in rows:
        b = r["basis"] or {}
        if not b:
            continue
        if b.get("os_enforced_isolation") is False:
            gaps.append((r["id"], "isolation is by convention, not enforced "
                                  "by the OS"))
        if b.get("attribution_by_observation") is False:
            gaps.append((r["id"], "no observation attributes these bytes to "
                                  "this process"))
        note = b.get("note")
        if note:
            gaps.append((r["id"], str(note)))
    return gaps


# Kept byte-identical in spirit with swarm.py's reader: skills install
# individually, so a skill cannot import from a sibling.
RECEIPTS = "outbox-receipts.jsonl"


def _read_attestations(project):
    """(refs_by_key, fatal) from the receipt journal, fail-closed.

    swarm.py stopped writing the `applied` field, because it was always false
    and nothing ever set it true. This file went on reading it, so every run
    reported its tracker updates as pending forever no matter what had landed.
    That is what happens when a field is removed and its consumers are not
    traced.
    """
    path = os.path.join(project, ".swarm", "state", RECEIPTS)
    if not os.path.exists(path):
        return {}, []
    try:
        raw = open(path).read()
    except OSError as exc:
        return {}, ["cannot read the receipt journal: %s" % exc]
    lines = raw.splitlines()
    complete_tail = raw.endswith("\n")
    refs, fatal = {}, []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        last = idx == len(lines) - 1
        try:
            rec = json.loads(line)
        except ValueError:
            if not (last and not complete_tail):
                fatal.append("receipt line %d was written in full and does "
                             "not parse" % (idx + 1))
            continue
        if not isinstance(rec, dict) or rec.get("attested") is not True:
            fatal.append("receipt line %d does not assert success"
                         % (idx + 1))
            continue
        key = rec.get("key")
        if key:
            refs.setdefault(key, set()).add(str(rec.get("ref")))
    return refs, fatal


def outbox_summary(data):
    """(unacknowledged, attested, conflicts, fatal).

    ATTESTED is the drainer's word, never verified: nothing here can ask the
    tracker. Absence means no confirmation either way, NOT that nothing was
    filed."""
    refs, fatal = _read_attestations(data["project"])
    unack, attested, conflicts = [], [], []
    for i in data["outbox"]:
        got = refs.get(i.get("key"))
        if not got:
            unack.append(i)
        elif len(got) > 1:
            conflicts.append(i)
        else:
            i["_ref"] = sorted(got)[0]
            attested.append(i)
    return unack, attested, conflicts, fatal


def verdict(rows):
    if not rows:
        return "NO UNITS", "A plan with no units cannot have produced anything."
    bad = [r for r in rows if r["state"] in TERMINAL_BAD]
    live = [r for r in rows if r["state"] not in TERMINAL_BAD
            and r["state"] not in TERMINAL_OK]
    done = [r for r in rows if r["state"] in TERMINAL_OK]
    if bad:
        return "FAILED", ("%d of %d unit(s) did not produce their declared "
                          "outputs." % (len(bad), len(rows)))
    if live:
        return "IN FLIGHT", ("%d of %d unit(s) closed; the rest have not "
                             "reached a terminal state." % (len(done),
                                                            len(rows)))
    return "COMPLETE", ("All %d unit(s) closed on evidence." % len(rows))


def _fmt_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "?"


# --------------------------------------------------------------------------
# rendering

CSS = """
:root{
  --bg:#f6f7f9; --surface:#ffffff; --surface-2:#eef1f5;
  --ink:#181d24; --muted:#5b6676; --line:#dce1e9;
  --accent:#0f6f6c; --accent-soft:#d7ecea;
  --ok:#1f7a4d; --warn:#8a5d06; --bad:#a3352b;
  --shadow:0 1px 2px rgba(20,28,38,.06),0 4px 14px rgba(20,28,38,.05);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0e1217; --surface:#151b22; --surface-2:#1b232c;
    --ink:#e7ebf1; --muted:#95a1b1; --line:#242e39;
    --accent:#4fd1c5; --accent-soft:#12312f;
    --ok:#4ade80; --warn:#fbbf24; --bad:#f87171;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 20px rgba(0,0,0,.35);
  }
}
:root[data-theme="dark"]{
  --bg:#0e1217; --surface:#151b22; --surface-2:#1b232c;
  --ink:#e7ebf1; --muted:#95a1b1; --line:#242e39;
  --accent:#4fd1c5; --accent-soft:#12312f;
  --ok:#4ade80; --warn:#fbbf24; --bad:#f87171;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 20px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:"IBM Plex Sans",ui-sans-serif,system-ui,-apple-system,
    "Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:16px; line-height:1.6;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1040px;margin:0 auto;padding:48px 24px 96px}
code,kbd,.mono,td.mono{
  font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,
    monospace;
  font-variant-numeric:tabular-nums;
}
h1,h2,h3{text-wrap:balance;margin:0;line-height:1.2}
h1{font-size:2.1rem;font-weight:600;letter-spacing:-.02em}
h2{font-size:1.15rem;font-weight:600;letter-spacing:-.01em;margin-bottom:4px}
.eyebrow{
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:.72rem;text-transform:uppercase;letter-spacing:.13em;
  color:var(--muted);
}
header.head{
  display:flex;flex-direction:column;gap:10px;
  padding-bottom:22px;border-bottom:1px solid var(--line);margin-bottom:32px;
}
.sub{color:var(--muted);max-width:68ch;margin:0}
section{margin-top:40px}
.sec-head{display:flex;flex-direction:column;gap:2px;margin-bottom:14px}
.sec-head p{margin:0;color:var(--muted);font-size:.92rem;max-width:70ch}
.verdict{
  display:flex;flex-wrap:wrap;align-items:baseline;gap:14px;
  padding:18px 20px;border-radius:10px;background:var(--surface);
  border:1px solid var(--line);border-left:3px solid var(--accent);
  box-shadow:var(--shadow);
}
.verdict .big{font-size:1.5rem;font-weight:600;letter-spacing:-.01em}
.verdict p{margin:0;color:var(--muted)}
.v-COMPLETE{border-left-color:var(--ok)} .v-COMPLETE .big{color:var(--ok)}
.v-FAILED{border-left-color:var(--bad)} .v-FAILED .big{color:var(--bad)}
.v-INFLIGHT{border-left-color:var(--warn)} .v-INFLIGHT .big{color:var(--warn)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}
.stat{
  background:var(--surface);border:1px solid var(--line);border-radius:9px;
  padding:14px 16px;display:flex;flex-direction:column;gap:3px;
}
.stat .n{font-size:1.45rem;font-weight:600;letter-spacing:-.01em}
.stat .l{font-size:.78rem;color:var(--muted);letter-spacing:.02em}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:9px;
  background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{padding:9px 13px;text-align:left;border-bottom:1px solid var(--line);
  vertical-align:top;white-space:nowrap}
th{
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:.7rem;text-transform:uppercase;letter-spacing:.09em;
  color:var(--muted);background:var(--surface-2);font-weight:500;
}
tbody tr:last-child td{border-bottom:none}
td.wrap-cell{white-space:normal;min-width:230px}
.pill{
  display:inline-block;padding:1px 9px;border-radius:999px;
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:.7rem;letter-spacing:.05em;border:1px solid transparent;
}
.p-DONE{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent);
  border-color:color-mix(in srgb,var(--ok) 32%,transparent)}
.p-BAD{color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,transparent);
  border-color:color-mix(in srgb,var(--bad) 32%,transparent)}
.p-OTHER{color:var(--warn);background:color-mix(in srgb,var(--warn) 12%,transparent);
  border-color:color-mix(in srgb,var(--warn) 32%,transparent)}
.digest{font-size:.78rem;color:var(--muted)}
.note{
  background:var(--surface);border:1px solid var(--line);
  border-left:3px solid var(--warn);border-radius:9px;padding:14px 18px;
}
.note ul{margin:8px 0 0;padding-left:20px}
.note li{margin:5px 0}
.claim{border-left-color:var(--accent);background:var(--surface)}
dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;margin:0}
dl.kv dt{color:var(--muted);font-size:.85rem}
dl.kv dd{margin:0;font-size:.9rem}
footer{
  margin-top:56px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--muted);font-size:.82rem;
}
a{color:var(--accent)}
a:focus-visible,tr:focus-visible{outline:2px solid var(--accent);
  outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;
  animation:none!important}}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=IBM+Plex+Mono:wght@400;500&'
         'family=IBM+Plex+Sans:wght@400;500;600&display=swap">')


def e(x):
    return html.escape("" if x is None else str(x), quote=True)


def _pill(state):
    cls = ("p-DONE" if state in TERMINAL_OK
           else "p-BAD" if state in TERMINAL_BAD else "p-OTHER")
    return '<span class="pill %s">%s</span>' % (cls, e(state))


def render(data, title=None):
    rows = unit_rows(data)
    v, vwhy = verdict(rows)
    plan = data["plan"]
    name = (title or plan.get("project") or plan.get("name")
            or os.path.basename(os.path.abspath(data["project"])))
    survey = data["survey"] or {}
    unack, attested, ack_conflicts, ack_fatal = outbox_summary(data)
    gaps = evidence_gaps(rows)

    n_out = sum(len(r["outputs"]) for r in rows)
    tot_bytes = sum(o.get("size") or 0 for r in rows
                    for o in r["outputs"].values())
    done = sum(1 for r in rows if r["state"] in TERMINAL_OK)

    P = []
    a = P.append
    a('<div class="wrap">')

    a('<header class="head">')
    a('<div class="eyebrow">swarm run report</div>')
    a("<h1>%s</h1>" % e(name))
    if plan.get("goal") or data["brief"].get("goal"):
        a('<p class="sub">%s</p>'
          % e(plan.get("goal") or data["brief"].get("goal")))
    bits = []
    if survey.get("hostname"):
        bits.append("host <code>%s</code>" % e(survey["hostname"]))
    if data["state"].get("plan_digest"):
        bits.append("plan <code>%s</code>"
                    % e(str(data["state"]["plan_digest"])[:12]))
    bits.append("generated %s" % e(
        _dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
    a('<p class="sub">%s</p>' % " &middot; ".join(bits))
    a("</header>")

    # verdict
    a('<div class="verdict v-%s">' % ("INFLIGHT" if v == "IN FLIGHT" else v))
    a('<span class="big">%s</span><p>%s</p>' % (e(v), e(vwhy)))
    a("</div>")

    # counters
    a('<section><div class="grid">')
    for n, l in ((("%d / %d" % (done, len(rows))), "units closed"),
                 (str(n_out), "declared outputs delivered"),
                 (_fmt_bytes(tot_bytes), "output bytes, digested"),
                 (str(len(attested)), "tracker updates attested")):
        a('<div class="stat"><span class="n mono">%s</span>'
          '<span class="l">%s</span></div>' % (e(n), e(l)))
    a("</div></section>")

    # units
    a("<section>")
    a('<div class="sec-head"><h2>Units</h2>'
      "<p>What was dispatched, and what closed it. A unit is the retry "
      "boundary: a retry starts in a fresh empty directory, so attempts "
      "count against redoing the whole unit. <strong>Verified by</strong> is "
      "copied from the plan verbatim: a runtime that a compute-node probe "
      "closed is a different claim from one merely declared, and the two "
      "must not read alike.</p></div>")
    a('<div class="tablewrap"><table><thead><tr>')
    for h in ("unit", "kind", "state", "job", "attempts", "runtime",
              "verified by", "declared outputs"):
        a("<th>%s</th>" % h)
    a("</tr></thead><tbody>")
    for r in rows:
        att = str(r["attempts"])
        if r["max_attempts"]:
            att += " / %s" % r["max_attempts"]
        a("<tr>")
        a('<td class="mono">%s</td>' % e(r["id"]))
        a("<td>%s</td>" % e(r["kind"]))
        a("<td>%s%s</td>" % (
            _pill(r["state"]),
            ('<br><span class="digest">%s</span>' % e(r["state_conflict"]))
            if r["state_conflict"] else ""))
        a('<td class="mono">%s</td>' % e(r["job_id"] or "&mdash;"))
        a('<td class="mono">%s</td>' % e(att))
        rt = r["runtime"] or {}
        a('<td class="mono">%s</td>' % e(rt.get("entrypoint") or "&mdash;"))
        vb = rt.get("verified_by") or "&mdash;"
        a('<td class="mono">%s</td>' % e(vb))
        a('<td class="wrap-cell mono">%s</td>'
          % e(", ".join(r["declared"]) or "-"))
        a("</tr>")
    a("</tbody></table></div></section>")

    # evidence
    a("<section>")
    a('<div class="sec-head"><h2>Evidence</h2>'
      "<p>Each delivered artifact with the digest recorded at the moment it "
      "was judged. This is what makes the claim checkable later: re-hash the "
      "file and compare.</p></div>")
    if n_out:
        a('<div class="tablewrap"><table><thead><tr>')
        for h in ("unit", "artifact", "size", "sha256", "method"):
            a("<th>%s</th>" % h)
        a("</tr></thead><tbody>")
        for r in rows:
            for fname, o in sorted(r["outputs"].items()):
                a("<tr>")
                a('<td class="mono">%s</td>' % e(r["id"]))
                a('<td class="mono">%s</td>' % e(fname))
                a('<td class="mono">%s</td>' % e(_fmt_bytes(o.get("size"))))
                a('<td class="mono digest">%s</td>'
                  % e(str(o.get("sha256") or "")[:16] or "&mdash;"))
                a("<td>%s</td>" % e(o.get("method") or "?"))
                a("</tr>")
        a("</tbody></table></div>")
    else:
        a("<p>No outputs have been digested yet.</p>")
    a("</section>")

    # what the evidence does NOT establish
    if gaps:
        a("<section>")
        a('<div class="sec-head"><h2>What this evidence does not establish'
          "</h2><p>Read from each receipt's own <code>basis</code> block, "
          "not asserted here. A receipt that proves more will shrink this "
          "list on its own.</p></div>")
        a('<div class="note"><ul>')
        seen = set()
        for uid, text in gaps:
            if text in seen:
                continue
            seen.add(text)
            a("<li><code>%s</code> &mdash; %s</li>" % (e(uid), e(text)))
        a("</ul></div></section>")

    # project findings, clearly labelled as claims
    f = data["findings"]
    if isinstance(f, dict) and f.get("findings"):
        a("<section>")
        a('<div class="sec-head"><h2>Findings reported by this project</h2>'
          "<p>Supplied by the run in <code>findings.json</code>. These are "
          "the project's claims about what the results mean. The coordinator "
          "does not verify them: it has no idea what a column means.</p></div>")
        a('<div class="note claim"><ul>')
        for item in f["findings"]:
            if isinstance(item, dict):
                t = item.get("title") or ""
                d = item.get("detail") or ""
                a("<li><strong>%s</strong>%s</li>"
                  % (e(t), (" &mdash; " + e(d)) if d else ""))
            else:
                a("<li>%s</li>" % e(item))
        a("</ul></div></section>")

    # tracker
    if data["tickets"] or data["outbox"]:
        a("<section>")
        a('<div class="sec-head"><h2>Tracker</h2>'
          "<p>Issues filed for this plan, and whether the coordinator has "
          "confirmed each update was applied.</p></div>")
        issues = (data["tickets"].get("issues")
                  or data["tickets"].get("tickets") or [])
        if issues:
            a('<div class="tablewrap"><table><thead><tr>'
              "<th>id</th><th>unit</th><th>title</th></tr></thead><tbody>")
            for it in issues:
                if not isinstance(it, dict):
                    continue
                ident = it.get("identifier") or it.get("id") or "&mdash;"
                url = it.get("url")
                cell = ('<a href="%s">%s</a>' % (e(url), e(ident))
                        if url else e(ident))
                a("<tr>")
                a('<td class="mono">%s</td>' % cell)
                a('<td class="mono">%s</td>' % e(it.get("unit") or "-"))
                a('<td class="wrap-cell">%s</td>' % e(it.get("title") or ""))
                a("</tr>")
            a("</tbody></table></div>")
        if ack_fatal:
            a('<div class="note" style="margin-top:12px">'
              "<strong>The receipt journal cannot be read in full.</strong>"
              "<p style='margin:6px 0 0'>No acknowledgment status derived "
              "from it can be trusted, including the reassuring parts, so "
              "none is shown.</p><ul>")
            for f in ack_fatal:
                a("<li>%s</li>" % e(f))
            a("</ul></div>")
        if ack_conflicts:
            a('<div class="note" style="margin-top:12px">'
              "<strong>%d intent(s) carry conflicting tracker refs.</strong>"
              "<p style='margin:6px 0 0'>Two attestations name different "
              "references for one intent, so something was filed twice, in "
              "two places.</p></div>" % len(ack_conflicts))
        if unack:
            a('<div class="note" style="margin-top:12px">'
              "<strong>%d tracker intent(s) are unacknowledged.</strong>"
              "<p style='margin:6px 0 0'>That does NOT mean they were never "
              "filed: it means nothing here has confirmation either way. "
              "Re-draining is safe, because intents are keyed. And an "
              "<em>attested</em> intent is the drainer's word, not proof: "
              "nothing in this project can ask the tracker.</p></div>"
              % len(unack))
        a("</section>")

    # environment
    if survey or os.path.exists(os.path.join(data["project"], "env.lock")):
        a("<section>")
        a('<div class="sec-head"><h2>Environment</h2>'
          "<p>Where this ran. Recorded so the run can be repeated, or its "
          "difference from a later run explained.</p></div>")
        a('<dl class="kv">')
        for k, label in (("hostname", "host"), ("user", "user"),
                         ("python", "python"), ("scheduler", "scheduler")):
            val = survey.get(k)
            if isinstance(val, dict):
                val = val.get("version") or val.get("path") or json.dumps(val)
            if val:
                a("<dt>%s</dt><dd class='mono'>%s</dd>" % (e(label), e(val)))
        accts = survey.get("accounts")
        if accts:
            a("<dt>accounts</dt><dd class='mono'>%s</dd>"
              % e(", ".join(map(str, accts))))
        a("</dl></section>")

    a('<footer>Generated by <code>report.py</code> from plan.json, the '
      "coordinator state, and each attempt's receipt. Nothing here is a "
      "self-report: every state and digest was read off disk.</footer>")
    a("</div>")
    return "\n".join(P)


def wrap_standalone(body, title):
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,'
            'initial-scale=1"><title>%s</title>%s<style>%s</style></head>'
            "<body>%s</body></html>" % (e(title), FONTS, CSS, body))


def wrap_fragment(body, title):
    """For publishing as an artifact: the host supplies doctype/head/body."""
    return ("<title>%s</title>\n%s\n<style>%s</style>\n%s"
            % (e(title), FONTS, CSS, body))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the end-of-run report for a swarm project.")
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--out", default=None,
                    help="write here (default: <project>/report.html)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--fragment", action="store_true",
                    help="emit a body fragment for publishing as an artifact "
                         "rather than a standalone document")
    ap.add_argument("--json", action="store_true",
                    help="emit the collected evidence as JSON instead")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.project):
        sys.stderr.write("error: %s is not a directory\n" % args.project)
        return 2
    data = collect(args.project)

    if args.json:
        rows = unit_rows(data)
        v, why = verdict(rows)
        json.dump({"schema_version": SCHEMA, "verdict": v, "why": why,
                   "units": rows, "gaps": evidence_gaps(rows)},
                  sys.stdout, indent=1, default=str)
        sys.stdout.write("\n")
        return 0

    plan = data["plan"]
    title = (args.title or plan.get("project") or plan.get("name")
             or os.path.basename(os.path.abspath(args.project)))
    body = render(data, title=title)
    doc = (wrap_fragment(body, title) if args.fragment
           else wrap_standalone(body, title))
    out = args.out or os.path.join(args.project, "report.html")
    with open(out, "w") as fh:
        fh.write(doc)
    rows = unit_rows(data)
    v, _ = verdict(rows)
    sys.stderr.write("wrote %s (%s, %d unit(s))\n" % (out, v, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
