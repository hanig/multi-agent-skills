"""Cross-process publication for the terminal-watcher test's fake Paseo."""

import os
from pathlib import Path
import runpy
import sys
import time


# Preserve the baseline proc.wait allowance; do not invent a tighter budget.
COMPLETION_SECONDS = 10


def now():
    # Unlike time.monotonic on macOS 3.9, this clock has a shared epoch.
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def publish(path, text):
    """Each fixture has one writer per path; expose only the closed file."""
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(text)
    pending.replace(path)


def wait(root, agent):
    assert sys.argv[1:] == ["wait", agent, "--json"]
    publish(root / "wait-pid", str(os.getpid()))
    while True:
        try:
            raw = (root / "release").read_text()
        except FileNotFoundError:
            raw = None
        # Optional read acknowledgment lets regressions hold an exact
        # interleaving without guessing how long the child needs to run.
        if (root / "observe-release").exists():
            publish(root / "read-observed", repr(raw))
        # A digit prefix (e.g. '1' of '17') is not a complete release.
        if raw is not None and raw.endswith("\n") and raw[:-1].isdigit():
            rc = int(raw)
            break
        time.sleep(0.02)
    print("idle" if rc == 0 else "fixture wait failed", flush=True)
    # The interval includes child exit and the watcher's response, but not
    # waiting for the fixture to have a complete result in the first place.
    publish(root / "terminal-ready-at", str(now()))
    sys.exit(rc)


def run_watcher(completion, argv):
    """Stamp handler completion in its process, independently of the observer."""
    script = argv[0]
    sys.argv = argv
    sys.path.insert(0, str(Path(script).parent))
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        publish(Path(completion), str(now()))


if __name__ == "__main__":
    run_watcher(sys.argv[1], sys.argv[2:])
