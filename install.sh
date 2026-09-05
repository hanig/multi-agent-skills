#!/bin/sh
# install.sh — portable entrypoint for the multi-agent skill installer.
# Python is required only to run the installer; it is checked before any
# discovery or filesystem operation so an unavailable interpreter is actionable.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required to plan or install skills; install Python 3 and retry" >&2
  exit 127
fi

# -B is intentional: dry-run validates Python sources without leaving
# __pycache__ files in this checkout, and the wrapper never changes the cwd.
exec python3 -B "$REPO/lib/skill_installer.py" "$@"
