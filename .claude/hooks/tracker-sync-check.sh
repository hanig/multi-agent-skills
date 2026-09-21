#!/bin/bash
# PostToolUse hook: after an outward action that changes a PR or an issue,
# report unsynced tracker state, so an orchestrator cannot silently skip it.
#
# Written because prose did not work. The obligation was documented in
# hanig-project SKILL.md step 6 and then skipped twice within the hour by the
# session that wrote it. The harness runs this; the model cannot forget it.
#
# Never blocks and never fails the tool call. It reports numbers, because a
# reminder without numbers gets ignored.
#
# Host and state directory are configurable and MUST NOT be hardcoded per this
# repo's portability rule: usernames and home paths differ across chimera,
# lambda and andromeda. Set HANIG_TRACKER_HOST to empty for a local coordinator.
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

HOST="${HANIG_TRACKER_HOST-chimera-login}"
STATE="${HANIG_TRACKER_STATE_DIR-\$HOME/.local/state/hanig-swarm/projects/multi-agent-skills-56cd9d5ff760/state}"
REPO="${HANIG_TRACKER_REPO-\$HOME/multi-agent-skills}"

READ='python3 skills/hanig-swarm/scripts/swarm.py outbox --state-dir '"$STATE"' --json 2>/dev/null'
if [ -n "$HOST" ]; then
  RAW=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "cd $REPO 2>/dev/null && $READ" 2>/dev/null)
else
  RAW=$(cd "$(eval echo "$REPO")" 2>/dev/null && eval "$READ")
fi

COUNTS=$(printf '%s' "$RAW" | python3 -c "
import json,sys,collections
try:
    d=json.load(sys.stdin); ints=d if isinstance(d,list) else d.get('intents',[])
except Exception:
    raise SystemExit
un=[i for i in ints if i.get('ack_status')=='unacknowledged']
c=collections.Counter(i['envelope']['requested_operation'] for i in un)
print('%d|%d' % (c.get('close',0), len(un)))
" 2>/dev/null)

if [ -z "$COUNTS" ]; then
  printf 'TRACKER SYNC CHECK: could not read the outbox (host %s).\n' "${HOST:-local}"
  printf '  Unknown is not zero. Check it before assuming Linear is current.\n'
  exit 0
fi

TERM_PEND=${COUNTS%%|*}; ALL_PEND=${COUNTS##*|}
printf 'TRACKER SYNC CHECK (outward action detected)\n'
printf '  pending TERMINAL intents: %s   <- the alarm; should be 0\n' "$TERM_PEND"
printf '  total unacknowledged:     %s   (lifecycle intents are never applied, by policy)\n' "$ALL_PEND"
printf '  Reflect this action in Linear now. The issue comment is part of the\n'
printf '  action, not a follow-up. Draining is reconciliation, never replay.\n'
exit 0
