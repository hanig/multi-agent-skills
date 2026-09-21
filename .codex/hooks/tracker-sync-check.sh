#!/bin/bash
# PostToolUse hook: after an outward action that changes a PR or an issue,
# report unsynced tracker state, so an orchestrator cannot silently skip it.
#
# Written because prose did not work. The obligation was documented in
# hanig-project SKILL.md step 6 and then skipped twice within the hour by the
# session that wrote it. The harness runs this; the model cannot forget it.
#
# It must never block, never hang, and never execute configuration as code.
# The first draft of this file did two of those three wrong and was caught by
# the review gate:
#   - `eval` on $HANIG_TRACKER_REPO was a command injection
#     (HANIG_TRACKER_REPO='$(rm -rf "$HOME")' would have run).
#   - counting only `close` reported "pending TERMINAL intents: 0" while a
#     terminal `block` sat unapplied, which is precisely the silent miss the
#     hook exists to catch.
# Both are fixed below. No eval, and the verb breakdown is printed rather than
# collapsed into one number the hook might compute wrongly.

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

# Configuration is data, never code. No eval, no command substitution on it.
# Host and paths are not hardcoded: usernames and home paths differ across
# chimera, lambda and andromeda, and the coordinator may be local.
HOST="${HANIG_TRACKER_HOST-}"
REPO="${HANIG_TRACKER_REPO-$HOME/multi-agent-skills}"
STATE="${HANIG_TRACKER_STATE_DIR-}"

if [ -z "$STATE" ]; then
  # Derive the default the same way coordinator_paths.py does.
  STATE=$(python3 - "$REPO" <<'PY' 2>/dev/null
import hashlib, os, re, sys
repo = os.path.realpath(sys.argv[1])
digest = hashlib.sha256(repo.encode()).hexdigest()[:12]
slug = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(repo)).strip("-.") or "project"
base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
print(os.path.join(base, "hanig-swarm", "projects", f"{slug}-{digest}", "state"))
PY
)
fi
[ -z "$STATE" ] && exit 0

# Bounded: a hung filesystem or ssh must not stall the session. Run the probe
# in the background and reap it; macOS has no coreutils `timeout`.
TMP=$(mktemp 2>/dev/null) || exit 0
probe() {
  if [ -n "$HOST" ]; then
    ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=4 \
        -o ServerAliveCountMax=2 "$HOST" \
        "cd '$REPO' && python3 skills/hanig-swarm/scripts/swarm.py outbox --state-dir '$STATE' --json" 2>/dev/null
  else
    ( cd "$REPO" 2>/dev/null && python3 skills/hanig-swarm/scripts/swarm.py outbox --state-dir "$STATE" --json 2>/dev/null )
  fi
}
probe > "$TMP" 2>/dev/null &
PROBE_PID=$!
WAITED=0
while kill -0 "$PROBE_PID" 2>/dev/null; do
  [ "$WAITED" -ge 20 ] && { kill -9 "$PROBE_PID" 2>/dev/null; break; }
  sleep 1; WAITED=$((WAITED + 1))
done
wait "$PROBE_PID" 2>/dev/null

REPORT=$(python3 - "$TMP" <<'PY' 2>/dev/null
import json, sys, collections
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
ints = d if isinstance(d, list) else d.get("intents", [])
un = [i for i in ints if i.get("ack_status") == "unacknowledged"]
c = collections.Counter((i.get("envelope") or {}).get("requested_operation") for i in un)
# close and block are BOTH candidates for terminal; which blocks are terminal is
# the coordinator's state machine's call, not this hook's, so report the verbs
# and let the reader judge rather than collapse them into one wrong number.
print("  unacknowledged by verb: " + (", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "none"))
print(f"  close={c.get('close',0)}  block={c.get('block',0)}   <- terminal candidates; a terminal block counts")
print(f"  total unacknowledged: {len(un)}")
PY
)
rm -f "$TMP" 2>/dev/null

printf 'TRACKER SYNC CHECK (outward action detected)\n'
if [ -z "$REPORT" ]; then
  printf '  could not read the outbox (host=%s). Unknown is not zero.\n' "${HOST:-local}"
else
  printf '%s\n' "$REPORT"
fi
printf '  Reflect this action in Linear now. The issue comment is part of the\n'
printf '  action, not a follow-up. Draining is reconciliation, never replay.\n'
exit 0
