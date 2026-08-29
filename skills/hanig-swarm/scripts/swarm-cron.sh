#!/bin/sh
# Deterministic scheduler entry for one project. Per sol: a fresh LLM agent
# every few minutes to invoke a deterministic command adds cost and another
# failure mode, so this is cron/launchd, not a Paseo schedule.
#
# Safe to fire while a previous run is still going: swarm.py takes a lease and
# the second invocation exits without acting.
set -eu
PROJ="$1"
cd "$(dirname "$0")/.."
exec python3 scripts/swarm.py advance "$PROJ/plan.json" \
  --state-dir "$PROJ/.state" --root "$PROJ/.runs" >> "$PROJ/advance.log" 2>&1
