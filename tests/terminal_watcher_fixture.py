"""Cross-process publication for the terminal-watcher test's fake Paseo."""

import os
import sys
import time


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
    sys.exit(rc)
