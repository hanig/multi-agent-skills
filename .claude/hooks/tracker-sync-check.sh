#!/bin/bash
# PostToolUse hook: after an outward action that changes a PR or an issue,
# report unsynced tracker state, so an orchestrator cannot silently skip it.
#
# Written because prose did not work. The obligation was documented in
# hanig-project SKILL.md step 6 and then skipped twice within the hour by the
# session that wrote it. The harness runs this; the model cannot forget it.
#
# THIS FILE HAS BEEN WRONG THREE TIMES, EACH CAUGHT BY REVIEW, EACH WRITTEN BY
# THE ORCHESTRATOR. Read before editing:
#
#   1. `eval` on $HANIG_TRACKER_REPO was a command injection.
#      HANIG_TRACKER_REPO='$(rm -rf "$HOME")' would have run.
#   2. The same value interpolated into an ssh remote command string inside
#      single quotes escaped the quoting and executed on the remote host.
#      Fixing (1) without sweeping for (2) is the sibling-sweep failure
#      CLAUDE.md warns about. The remote branch is now gone entirely rather
#      than quoted more carefully; the coordinator runs on this machine.
#   3. The report went to stdout with exit 0. Verified against the harness:
#      "Exit code 0 - stdout shown in transcript mode (ctrl+o)". So the
#      reminder reached a transcript and NEVER the model. A hook built
#      because prose gets skipped was itself a no-op carrying an
#      authoritative-sounding header. It now emits
#      hookSpecificOutput.additionalContext, which the harness documents as
#      "Text injected into model context".
#
# It must never block, never hang, and never execute configuration as code.

set -u

emit() {  # $1 = message injected into the model's context
  python3 - "$1" <<'PYEOF'
import json, sys
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": sys.argv[1],
}}))
PYEOF
}

INPUT=$(cat 2>/dev/null)

# Detection is deliberately SENSITIVE, not precise, because the costs are not
# symmetric. A spurious reminder costs one line of context. A missed one costs
# the tracker sync this hook exists to guarantee. So: substring detection, and
# wording that never asserts the command actually ran.
#
# Two earlier matchers were refuted. A bare `case` substring test fired on
# prose quoting a command. Replacing it with shlex plus flag-stripping then
# produced a FALSE NEGATIVE — `gh -R owner/repo pr merge` shifted the
# subcommand position and silently did not fire — while still false-firing on
# a trigger inside a quoted argument after a separator. Parsing shell out of a
# string is the mistake; the fix is to stop claiming precision we cannot have.
# A third refutation: without re.S the patterns did not cross a backslash-newline
# continuation, so a wrapped `gh --repo X \\<newline> pr merge` silently missed.
CMD=$(printf '%s' "$INPUT" | python3 -c "
import json, re, sys
try:
    cmd = (json.load(sys.stdin).get('tool_input') or {}).get('command', '')
except Exception:
    print(''); raise SystemExit
for label, pat in (
    ('gh pr merge',  r'\bgh\b.*\bpr\b.*\bmerge\b'),
    ('gh pr close',  r'\bgh\b.*\bpr\b.*\bclose\b'),
    ('gh pr create', r'\bgh\b.*\bpr\b.*\bcreate\b'),
    ('gh issue',     r'\bgh\b.*\bissue\b'),
    ('git push',     r'\bgit\b.*\bpush\b'),
):
    if re.search(pat, cmd, re.S):   # re.S: a backslash-newline continuation still reads as one command
        print(label); raise SystemExit
print('')
" 2>/dev/null)

[ -n "$CMD" ] || exit 0

# Configuration is data, never code. No eval, no shell-string assembly.
REPO="${HANIG_TRACKER_REPO-$HOME/multi-agent-skills}"
STATE="${HANIG_TRACKER_STATE_DIR-}"

if [ -z "$STATE" ]; then
  STATE=$(python3 - "$REPO" 2>/dev/null <<'PYEOF'
import hashlib, os, re, sys
repo = os.path.realpath(sys.argv[1])
digest = hashlib.sha256(repo.encode()).hexdigest()[:12]
slug = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(repo)).strip("-.") or "project"
base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
print(os.path.join(base, "hanig-swarm", "projects", f"{slug}-{digest}", "state"))
PYEOF
)
fi
[ -n "$STATE" ] || exit 0
[ -d "$REPO" ] || exit 0

# Bounded: a hung filesystem must not stall the session. macOS has no
# coreutils `timeout`, so reap explicitly. Paths are passed as argv.
TMP=$(mktemp 2>/dev/null) || exit 0
( cd "$REPO" && python3 skills/hanig-swarm/scripts/swarm.py outbox \
    --state-dir "$STATE" --json ) > "$TMP" 2>/dev/null &
PROBE_PID=$!
WAITED=0
while kill -0 "$PROBE_PID" 2>/dev/null; do
  if [ "$WAITED" -ge 20 ]; then kill -9 "$PROBE_PID" 2>/dev/null; break; fi
  sleep 1
  WAITED=$((WAITED + 1))
done
wait "$PROBE_PID" 2>/dev/null

REPORT=$(python3 - "$TMP" 2>/dev/null <<'PYEOF'
import json, sys, collections
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
ints = d if isinstance(d, list) else d.get("intents", [])
un = [i for i in ints if i.get("ack_status") == "unacknowledged"]
c = collections.Counter((i.get("envelope") or {}).get("requested_operation") for i in un)
# close and block are both terminal CANDIDATES. Which blocks are terminal is
# the coordinator state machine's call, not this hook's, so print the verbs
# rather than collapsing them into one number this hook cannot derive.
print("unacknowledged by verb: "
      + (", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "none")
      + f" | close={c.get('close',0)} block={c.get('block',0)} are terminal candidates"
      + f" | total {len(un)}")
PYEOF
)
rm -f "$TMP" 2>/dev/null

if [ -z "$REPORT" ]; then
  emit "TRACKER SYNC CHECK: this tool call looks like \`$CMD\` (matched loosely; it may not have run). Could not read the outbox at $STATE. Unknown is not zero -- check before assuming Linear is current."
else
  emit "TRACKER SYNC CHECK: this tool call looks like \`$CMD\` (matched loosely; it may not have run). $REPORT. If it did run, reflect it in Linear now; the issue comment is part of the action, not a follow-up. Draining is reconciliation, never replay -- an intent contradicted by current tracker state is escalated, not applied."
fi
exit 0
