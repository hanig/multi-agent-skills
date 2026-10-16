"""Measure E5's state-stability premise with real, isolated advance processes.

Run: python3 -m tests.e5_verification_premise OUTPUT_DIRECTORY
Exit 1 means the premise failed; this is a diagnostic, not an implementation.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tests import test_merge_unit as fixtures


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def changed_fields(before, after, prefix=""):
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            path = prefix + "." + key if prefix else key
            if key not in before or key not in after:
                result.append(path)
            else:
                result.extend(changed_fields(before[key], after[key], path))
        return result
    return [] if canonical(before) == canonical(after) else [prefix]


def measure(output):
    output.mkdir(parents=True, exist_ok=True)
    f = fixtures.TestMergeUnit()
    try:
        f.setUp()
        # Include the normal retained watch of a completed code attempt.
        # A real setup advance below establishes clocks and refusal metadata,
        # so the measured pass does not just discover missing legacy fields.
        f.us["code_terminal_watches"] = {"a1": {
            "agent_id": f.us["job_id"], "host": os.uname().nodename,
            "started_at": "2026-10-01T00:00:00+0000",
            "log": str(f.attempt / fixtures.S.CODE_TERMINAL_WATCH_LOG),
            "status": "checked", "checked_at": "2026-10-01T00:00:01+0000"}}
        other = {"id": "other", "kind": "pipeline", "command": "true", "runtime": "none",
                 "outputs": ["result.txt"]}
        f.plan["units"].append(other)
        allocate = subprocess.run(
            [sys.executable, str(fixtures.SCRIPTS / "unit.py"), "allocate",
             "--root", str(f.root), "--task", "other", "--kind", "pipeline",
             "--output", "result.txt"], env=f.env, cwd=f.repo,
            capture_output=True, text=True, timeout=60)
        f.assertEqual(allocate.returncode, 0, allocate.stdout + allocate.stderr)
        attempt = Path(allocate.stdout.strip())
        # The diagnostic process is alive throughout both advances. No real
        # scheduler or worker is contacted; the second pass observes engine.rc.
        (attempt / "engine.json").write_text(json.dumps({
            "pid": os.getpid(), "host": os.uname().nodename}))
        f.state["units"]["other"] = {
            "state": "RUNNING", "attempt_dir": str(attempt),
            "attempts": [str(attempt)], "gpu_hours": 0,
            "job_id": str(os.getpid()),
            "attempt_launch_facts": {attempt.name: {
                "launch_host": os.uname().nodename}}}
        f.state["plan_digest"] = fixtures.S.plan_digest(f.plan)
        f.state["halted"] = None
        f.save()
        command = [sys.executable, str(fixtures.SCRIPTS / "swarm.py"), "advance",
                   str(f.plan_path), "--state-dir", str(f.state_dir),
                   "--root", str(f.root)]

        def advance(label):
            result = subprocess.run(command, cwd=f.repo, env=f.env,
                                    capture_output=True, text=True, timeout=60)
            (output / (label + ".txt")).write_text(
                "command: " + repr(command) + "\nexit: " + str(result.returncode)
                + "\nstdout:\n" + result.stdout + "stderr:\n" + result.stderr)
            f.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        def state():
            return json.loads((f.state_dir / fixtures.S.STATE_FILE).read_text())

        advance("setup")
        baseline = state()
        f.assertEqual(baseline["units"]["u"]["state"], "READY_FOR_PR")
        f.assertEqual(baseline["units"]["other"]["state"], "RUNNING")
        results = []
        for label, stopped in (("no-op", False), ("other-stopped", True)):
            before = state()
            if stopped:
                (attempt / fixtures.S.U.ENGINE_RC).write_text("1\n")
            # Watch timestamps have second resolution. Cross that boundary
            # before each measurement, rather than accidentally hiding writes.
            time.sleep(1.05)
            advance(label)
            after = state()
            f.assertEqual(after["units"]["u"]["state"], "READY_FOR_PR")
            f.assertEqual(after["units"]["other"]["state"],
                          "FAILED" if stopped else "RUNNING")
            result = {"case": label, "other_state_before": before["units"]["other"]["state"],
                      "other_state_after": after["units"]["other"]["state"]}
            for name, old, new in (
                    ("unit_record", before["units"]["u"], after["units"]["u"]),
                    ("top_level", {k: v for k, v in before.items() if k != "units"},
                     {k: v for k, v in after.items() if k != "units"})):
                result[name] = {"identical": canonical(old) == canonical(new),
                                "changed_fields": changed_fields(old, new),
                                "before_sha256": hashlib.sha256(canonical(old)).hexdigest(),
                                "after_sha256": hashlib.sha256(canonical(new)).hexdigest()}
            for suffix, value in (("before", before), ("after", after)):
                (output / (label + "-" + suffix + ".json")).write_bytes(canonical(value))
            results.append(result)
        report = {"premise_holds": all(r[k]["identical"] for r in results
                                      for k in ("unit_record", "top_level")),
                  "measurements": results}
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        (output / "measurements.json").write_text(rendered)
        print(rendered, end="")
        return 0 if report["premise_holds"] else 1
    finally:
        f.doCleanups()


if __name__ == "__main__":
    raise SystemExit(measure(Path(sys.argv[1])))
