#!/bin/bash
# PostToolUse hook: after an outward action that changes a PR or an issue,
# report unsynced tracker state, so an orchestrator cannot silently skip it.
#
# Written because prose did not work. The obligation was documented in
# hanig-project SKILL.md step 6 and then skipped twice within the hour by the
# session that wrote it. The harness runs this; the model cannot forget it.
#
# It must never block, never hang, and never execute configuration as code.
# This file has now cost two command injections, both found by review, both
# written by the orchestrator:
#
#   1. `eval` on $HANIG_TRACKER_REPO in the local branch.
#      HANIG_TRACKER_REPO='$(rm -rf "$HOME")' would have run.
#   2. The same value interpolated into an ssh remote command string inside
#      single quotes, so HANIG_TRACKER_REPO="'; touch /tmp/canary; #" escaped
#      the quoting and executed on the remote host. Fixing (1) and not
#      sweeping for (2) is precisely the sibling-sweep failure CLAUDE.md warns
#      about.
#
# The remote branch is therefore GONE rather than quoted more carefully. The
# coordinator runs on this machine. Removing it also removes a second defect:
# the state directory was derived from the LOCAL home and sent to a host where
# that path does not exist, so the probe silently reported "could not read"
# instead of surfacing real unacknowledged intents. If a remote coordinator is
# ever wanted, design it deliberately with argv-safe transport — never by
# assembling paths into a shell string.
#
# It also reports the verb breakdown rather than one collapsed number: which
# `block` intents are terminal is the coordinator state machine's call, not
# this hook's, and an earlier version printed "pending TERMINAL intents: 0"
# while five terminal candidates sat unacknowledged.

set -u

INPUT=$(cat 2>/dev/null)
CMD=$(printf '%s' "$INPUT" | python3 -c "
import json,sys
try: print((json.load(sys.stdin).get('tool_input') or {}).get('command',''))
except Exception: print('')
" 2>/dev/null)

case "$CMD" in
  *"gh pr merge"*|*"gh pr close"*|*"gh pr create"*|*"gh issue"*|*"git push"*) ;;
  *) exit 0 ;;
esac

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
print("  unacknowledged by verb: " + (", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "none"))
print(f"  close={c.get('close',0)}  block={c.get('block',0)}   <- terminal candidates; a terminal block counts")
print(f"  total unacknowledged: {len(un)}")
PYEOF
)
rm -f "$TMP" 2>/dev/null

printf 'TRACKER SYNC CHECK (outward action detected)\n'
if [ -z "$REPORT" ]; then
  printf '  could not read the outbox at %s. Unknown is not zero.\n' "$STATE"
else
  printf '%s\n' "$REPORT"
fi
printf '  Reflect this action in Linear now. The issue comment is part of the\n'
printf '  action, not a follow-up. Draining is reconciliation, never replay.\n'
exit 0
