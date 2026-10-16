"""A settled READY_FOR_PR record survives real, isolated advance processes."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

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


def read_state(f):
    return json.loads((f.state_dir / fixtures.S.STATE_FILE).read_text())


def advance(f, output=None, label="advance"):
    command = [sys.executable, str(fixtures.SCRIPTS / "swarm.py"), "advance",
               str(f.plan_path), "--state-dir", str(f.state_dir), "--root", str(f.root)]
    result = subprocess.run(command, cwd=f.repo, env=f.env,
                            capture_output=True, text=True, timeout=60)
    if output is not None:
        (output / (label + ".txt")).write_text(
            "command: " + repr(command) + "\nexit: " + str(result.returncode)
            + "\nstdout:\n" + result.stdout + "stderr:\n" + result.stderr)
    f.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return read_state(f)


def prepare_advance(f, output=None):
    # A setup advance establishes clocks and refusal/receipt metadata before
    # measuring; missing legacy fields are not an unrelated poll.
    f.us["code_terminal_watches"] = {"a1": {
        "agent_id": f.us["job_id"], "host": os.uname().nodename,
        "started_at": "2026-10-01T00:00:00+0000",
        "log": str(f.attempt / fixtures.S.CODE_TERMINAL_WATCH_LOG),
        "status": "checked", "checked_at": "2026-10-01T00:00:01+0000"}}
    f.plan["units"].append({"id": "other", "kind": "pipeline", "command": "true",
                            "runtime": "none", "outputs": ["result.txt"]})
    allocate = subprocess.run(
        [sys.executable, str(fixtures.SCRIPTS / "unit.py"), "allocate",
         "--root", str(f.root), "--task", "other", "--kind", "pipeline",
         "--output", "result.txt"], env=f.env, cwd=f.repo,
        capture_output=True, text=True, timeout=60)
    f.assertEqual(allocate.returncode, 0, allocate.stdout + allocate.stderr)
    attempt = Path(allocate.stdout.strip())
    # This process stays alive; only engine.rc ends the second unit. The closed
    # PATH fixture contacts neither a real scheduler nor an actual worker.
    (attempt / "engine.json").write_text(json.dumps({
        "pid": os.getpid(), "host": os.uname().nodename}))
    f.state["units"]["other"] = {
        "state": "RUNNING", "attempt_dir": str(attempt),
        "attempts": [str(attempt)], "gpu_hours": 0, "job_id": str(os.getpid()),
        "attempt_launch_facts": {attempt.name: {"launch_host": os.uname().nodename}}}
    f.state["plan_digest"] = fixtures.S.plan_digest(f.plan)
    f.state["halted"] = None
    f.save()
    f.state = advance(f, output, "setup")
    f.us = f.state["units"]["u"]
    f.assertEqual(f.us["state"], "READY_FOR_PR")
    f.assertEqual(f.state["units"]["other"]["state"], "RUNNING")
    return attempt


class TestAdvanceIdempotentRecord(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TestMergeUnit()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_noop_and_other_unit_advance_preserve_record(self):
        f = self.f
        output = Path(os.environ.get("E5_MEASUREMENT_DIR", str(f.directory / "measurement")))
        output.mkdir(parents=True, exist_ok=True)
        attempt = prepare_advance(f, output)
        results = []
        for label, stopped in (("no-op-1", False), ("no-op-2", False),
                               ("other-stopped", True)):
            before = read_state(f)
            if stopped:
                (attempt / fixtures.S.U.ENGINE_RC).write_text("1\n")
            # Cross the timestamp's second boundary so a rewrite cannot hide.
            time.sleep(1.05)
            after = advance(f, output, label)
            self.assertEqual(after["units"]["u"]["state"], "READY_FOR_PR")
            self.assertEqual(after["units"]["other"]["state"],
                             "FAILED" if stopped else "RUNNING")
            result = {"case": label}
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
        (output / "measurements.json").write_text(json.dumps(results, indent=2) + "\n")
        for result in results:
            for surface in ("unit_record", "top_level"):
                with self.subTest(case=result["case"], surface=surface):
                    self.assertTrue(result[surface]["identical"], result[surface])

    def test_first_check_timestamp_and_status_remain_historical(self):
        f = self.f
        prepare_advance(f)
        for prior_status in ("idle_observed", "checked"):
            with self.subTest(prior_status=prior_status):
                state = read_state(f)
                watch = state["units"]["u"]["code_terminal_watches"]["a1"]
                watch["status"] = prior_status
                watch.pop("checked_at", None)
                (f.state_dir / fixtures.S.STATE_FILE).write_text(json.dumps(state))
                checked = advance(f)["units"]["u"]["code_terminal_watches"]["a1"]
                self.assertEqual(checked["status"], "checked")
                self.assertTrue(checked["checked_at"])
                time.sleep(1.05)
                self.assertEqual(advance(f)["units"]["u"]["code_terminal_watches"]["a1"],
                                 checked)
                command = [sys.executable, str(fixtures.SCRIPTS / "swarm.py"), "status",
                           str(f.plan_path), "--state-dir", str(f.state_dir)]
                for flags in ([], ["--json"]):
                    result = subprocess.run(command + flags, env=f.env, cwd=f.repo,
                                            capture_output=True, text=True, timeout=60)
                    # READY_FOR_PR needs operator action, so status exits 2.
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    if flags:
                        row = next(row for row in json.loads(result.stdout)["units"]
                                   if row["id"] == "u")
                        self.assertEqual(row["terminal_watch"]["checked_at"], checked["checked_at"])
                    else:
                        self.assertIn("terminal watcher: checked; checked " + checked["checked_at"],
                                      result.stdout)


if __name__ == "__main__":
    unittest.main()
