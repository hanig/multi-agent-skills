#!/bin/sh
# Closed foreground worker: no daemon, scheduler, model, or network.
set -eu
test "$PWD" = "$SWARM_UNIT_DIR"
# The startup-race regression releases this bounded wait after its first
# coordinator/report observation. It never grades from this handshake.
if test -n "${PIPELINE_RELEASE:-}"; then
  remaining=100
  while ! test -f "$PIPELINE_RELEASE"; do
    test "$remaining" -gt 0 || exit 65
    remaining=$((remaining - 1))
    sleep 0.1
  done
fi
case "$*" in
  honest)
    printf 'result\n' > result.txt
    printf 'details\n' > details.txt
    ;;
  hollow) ;;
  *) exit 64 ;;
esac
# Both workers claim success. The grader must ignore this narrative.
printf 'DONE: every output is complete\n'
exit 0
