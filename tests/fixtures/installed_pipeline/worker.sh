#!/bin/sh
# Closed foreground worker: no daemon, scheduler, model, or external commands.
set -eu
test "$PWD" = "$SWARM_UNIT_DIR"
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
