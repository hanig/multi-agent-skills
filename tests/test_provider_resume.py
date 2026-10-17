"""Real swarm/unit CLIs with injected Paseo, clock and SIGKILL boundaries."""
import ast
import contextlib
import copy
import json
import signal
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

from tests import test_attempt_worktrees as F
from tests.scheduler_fixture import isolated_module_path as setUpModule
from tests.scheduler_fixture import cleanup_module_path as tearDownModule

S = F.S
P = S.SPRS

FAKE = r"""
import json, os, signal, sys
from pathlib import Path
root = Path(ROOT)
cfg = json.loads((root / "fake.json").read_text())
args = sys.argv[1:]
if args[0] == "inspect":
    status = cfg.get("status", "error")
    if args[1] != "--json":
        calls = root / "observations"
        count = int(calls.read_text()) + 1 if calls.exists() else 1
        calls.write_text(str(count))
        if count >= cfg.get("change_at", 999):
            status = cfg.get("fresh_status", status)
    if status == "unreadable":
        sys.exit("unreadable")
    print(json.dumps({"Status": status, "PendingPermissions": cfg.get("permissions", [])}))
elif args[0] == "logs":
    count = int((root / "observations").read_text())
    message = cfg.get("message", "Selected model is at capacity")
    if count >= cfg.get("change_at", 999):
        message = cfg.get("fresh_message", message)
    print(cfg.get("prefix", "[System Error] ") + json.dumps({"error": {"message": message}}))
elif args[0] == "send":
    assert args[2] == "--no-wait"
    state = json.loads((root / "state/swarm-state.json").read_text())
    us = state["units"]["code"]
    record = us["provider_resumes"][Path(us["attempt_dir"]).name]
    with (root / "sends.jsonl").open("a") as log:
        log.write(json.dumps({"record": record, "argv": args}) + "\n")
    if cfg.get("kill") == "after_send":
        os.kill(os.getppid(), signal.SIGKILL)
    if cfg.get("send_rc"):
        sys.exit("injected send failure")
    if not cfg.get("persistent"):
        cfg["status"] = "running"
        (root / "fake.json").write_text(json.dumps(cfg))
        (root / "continued").write_text("continued")
elif args[0] not in ("archive", "delete", "worktree"):
    sys.exit("unexpected command: " + repr(args))
"""

DRIVER = r"""
import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, SCRIPTS)
import swarm as S
root = Path(ROOT)
cfg = json.loads((root / "fake.json").read_text())
S.time.time = lambda: cfg["now"]
save = S.SS.save_state
def interrupted_save(directory, state):
    us = state["units"].get("code", {})
    entry = us.get("provider_resumes", {}).get(Path(us.get("attempt_dir") or ".").name, {})
    charged = entry.get("resumes_attempted", 0) > 0
    if charged and cfg.get("kill") == "before_charge":
        os.kill(os.getpid(), signal.SIGKILL)
    save(directory, state)
    if charged and cfg.get("kill") == "after_charge":
        os.kill(os.getpid(), signal.SIGKILL)
S.SS.save_state = interrupted_save
if cfg.get("snapshot_failure"):
    S.SPRS.R.preserve_worktree = lambda *a, **k: (None, "injected disk full")
sys.exit(S.main())
"""


class Checks(unittest.TestCase):
    eq = unittest.TestCase.assertEqual
    absent = unittest.TestCase.assertNotIn
    contains = unittest.TestCase.assertIn
    is_none = unittest.TestCase.assertIsNone


class Harness(Checks):
    def setUp(self):
        F.TestPerAttemptWorktrees.setUp(self)
        context = F.paseo_resolvable()
        fakebin = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.unit = dict(F.code_unit(self.repo), outputs=["evidence.md"],
                         provider_resume={"max": 2})
        self.plan = {"name": "resume-test", "units": [self.unit]}
        self.sd = self.tmp / "state"
        state = {"schema_version": 1, "units": {}, "halted": None,
                 "plan_digest": S.plan_digest(self.plan)}
        directory, error = S._allocate(self.plan, self.unit, self.tmp / "runs")
        self.is_none(error)
        self.attempt = Path(directory)
        S._capture_artifact_basis(state, "code", directory, self.unit)
        def launch(argv, **kw):
            rc, out, err = self.fake(argv, **kw)
            return rc, out.replace("agent-" + self.attempt.name, "12345678-1234-1234-1234-123456789abc"), err
        with mock.patch.object(S.U, "run", side_effect=launch):
            job, error = S._submit(self.unit, directory, False, state, str(self.sd))
        self.is_none(error)
        self.is_none(S._bind(directory, job))
        us = state["units"]["code"]
        us.update(state="RUNNING", attempt_dir=directory, attempts=[directory],
                  job_id=job, gpu_hours=0, state_changed_at=1000,
                  code_terminal_watches={self.attempt.name: {
                      "status": "start_failed", "agent_id": job}})
        self.workspace = Path(S.SS.trusted_launch_facts(
            state, "code", self.attempt.name)["execution_workspace"])
        S.save_state(self.sd, state)
        self.original = copy.deepcopy(us)
        self.cfg = {"now": 1000}
        self.planfile = self.tmp / "plan.json"
        self.planfile.write_text(json.dumps(self.plan))
        self.driver = self.tmp / "driver.py"
        self.driver.write_text(DRIVER.replace("SCRIPTS", repr(str(F.SCRIPTS)))
                               .replace("ROOT", repr(str(self.tmp))))
        fake = Path(fakebin) / "paseo"
        fake.write_text("#!" + sys.executable + "\n" + FAKE.replace("ROOT", repr(str(self.tmp))))
        fake.chmod(0o755)

    def cli(self, command="advance", expected=0, **cfg):
        self.cfg.update(cfg)
        self.planfile.write_text(json.dumps(self.plan))
        (self.tmp / "fake.json").write_text(json.dumps(self.cfg))
        (self.tmp / "observations").write_text("0")
        argv = [sys.executable, str(self.driver), command, str(self.planfile)]
        if command != "validate":
            argv += ["--state-dir", str(self.sd)]
        if command == "advance":
            argv += ["--root", str(self.tmp / "runs"), "--max-new-dispatches", "0"]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        self.eq(proc.returncode, expected, proc.stdout + proc.stderr)
        self.cfg = json.loads((self.tmp / "fake.json").read_text())
        return proc.stdout + proc.stderr

    def us(self):
        return json.loads((self.sd / S.STATE_FILE).read_text())["units"]["code"]

    def record(self):
        return self.us()["provider_resumes"][self.attempt.name]

    def sends(self):
        path = self.tmp / "sends.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def ready(self):
        self.contains("waiting for backoff", self.cli())
        self.cfg["now"] = 1300

    def assert_snapshot(self, record):
        self.eq(set(record), {"fingerprint", "resumes_attempted",
                                      "next_not_before", "snapshot_paths"})
        self.eq(len(record["snapshot_paths"]), record["resumes_attempted"])
        for path in record["snapshot_paths"]:
            snapshot = json.loads((Path(path) / "manifest.json").read_text())
            self.is_none(S.R.validate_snapshot(snapshot))

    def unchanged(self):
        for key in ("attempt_dir", "attempts", "job_id", "attempt_launch_facts"):
            self.eq(self.us()[key], self.original[key])

    def expect(self, **fields):
        for key, value in fields.items():
            self.eq(self.us()[key], value)

    def sent(self, count):
        self.eq(len(self.sends()), count)

    def failed(self):
        self.eq(self.us()["state"], "FAILED")
        self.eq(self.sends(), [])

    def change_plan(self):
        self.planfile.write_text(json.dumps(self.plan))
        state = S.load_state(self.sd)
        state["plan_digest"] = S.plan_digest(self.plan)
        S.save_state(self.sd, state)


@contextlib.contextmanager
def scenario(**cfg):
    h = Harness()
    try:
        h.setUp()
        h.cfg.update(cfg)
        yield h
    finally:
        h.doCleanups()


class ProviderResumeTests(Checks):
    def test_classes_backoff_attempt_ledger(self):
        for message in P.RETRYABLE_PROVIDER_MESSAGES:
            with self.subTest(message=message), scenario(message=message.swapcase()) as h:
                ledger = h.tmp / "state-home/hanig-review-gate/rounds/fixture"
                ledger.mkdir(parents=True)
                paths = [ledger / "cycle-1-round-2.json", h.attempt / "dispositions.json"]
                for path in paths:
                    path.write_text('{"round":2,"disposition":"reproduced"}')
                before = {p: p.read_bytes() for p in paths}
                h.ready()
                self.contains("waiting for backoff", h.cli(now=1299))
                h.sent(0)
                self.absent(h.attempt.name, h.us().get("attempt_receipt_seals", {}))
                self.contains("resumed 1/2", h.cli(now=1300))
                h.expect(state="RUNNING")
                h.unchanged()
                self.eq({p: p.read_bytes() for p in before}, before)
                self.assertTrue((h.tmp / "continued").exists())
                h.sent(1)
                self.contains("current review round number", h.sends()[0]["argv"][-1])
                h.assert_snapshot(h.sends()[0]["record"])
                self.contains("delivery unconfirmed", h.cli("status"))

    def test_persistent_exhaustion(self):
        with scenario(persistent=True) as h:
            h.ready()
            h.cli()
            h.cli(now=2199)
            h.sent(1)
            h.cli(now=2200)
            h.assert_snapshot(h.record())
            h.sent(2)
            self.contains("spent 2/2", h.cli())
            h.expect(state="FAILED")
            self.assertFalse(h.workspace.exists())
            h.cli()
            h.sent(2)

    def test_refused_observations(self):
        filters = ["flagged for possible cybersecurity risk (503)",
                   "FLAGGED FOR POSSIBLE CYBERSECURITY RISK (503)",
                   "FlAgGeD FoR PoSsIbLe CyBeRsEcUrItY RiSk (503)",
                   "invalid model configuration"]
        cases = [(dict(message=m), False, "FAILED") for m in filters]
        cases += [({"prefix": "[User] "}, False, "FAILED"),
                  ({"permissions": ["answer required"]}, False, "NEEDS_HUMAN")]
        cases += [(dict(status=s), True, r) for s, r in (
            ("running", "RUNNING"), ("idle", "INCOMPLETE"),
            ("done", "INCOMPLETE"), ("unreadable", "INCOMPLETE"))]
        for cfg, ready, state in cases:
            with self.subTest(cfg=cfg), scenario() as h:
                if ready:
                    h.ready()
                h.cli(**cfg)
                h.expect(state=state)
                h.sent(0)
                if not ready:
                    self.absent("provider_resumes", h.us())
                if state == "FAILED":
                    self.assertFalse(h.workspace.exists())

    def test_changed_observation(self):
        for status in ("idle", "running", "done", "complete", "completed", "unreadable", "error"):
            with self.subTest(status=status), scenario() as h:
                h.ready()
                h.cli(change_at=2, fresh_status=status, fresh_message="invalid model")
                h.failed()
                self.assertFalse(h.workspace.exists())

    def test_sigkill_boundaries(self):
        for boundary, lost, first in (("before_charge", 0, 0),
                                      ("after_charge", 1, 0), ("after_send", 1, 1)):
            with self.subTest(boundary=boundary), scenario(persistent=True) as h:
                h.ready()
                h.cli(kill=boundary, expected=-signal.SIGKILL)
                self.eq(h.record()["resumes_attempted"], lost)
                h.sent(first)
                h.cfg.pop("kill")
                h.cli()
                h.sent(first if lost else 1)
                self.eq(h.record()["resumes_attempted"], 1)
                h.cli(now=2200)
                self.eq(h.record()["resumes_attempted"], 2)
                h.sent(1 if boundary == "after_charge" else 2)
                h.cli()
                h.expect(state="FAILED")
                self.assertLessEqual(len(h.sends()), 2)

    def test_send_failure_stays_charged(self):
        with scenario() as h:
            h.ready()
            self.contains("slot remains charged", h.cli(send_rc=1))
            self.eq(h.record()["resumes_attempted"], 1)
            h.expect(state="FAILED")
            h.cli()
            h.sent(1)

    def test_snapshot_success_and_failure(self):
        for failed in (False, True):
            with self.subTest(failed=failed), scenario() as h:
                (h.workspace / "untracked.txt").write_text("keep")
                (h.workspace / "tracked.txt").write_text("changed")
                h.ready()
                h.cli(snapshot_failure=failed)
                if failed:
                    h.expect(state="FAILED")
                    self.assertTrue(h.workspace.exists())
                    h.sent(0)
                else:
                    h.assert_snapshot(h.record())
                    content = Path(h.record()["snapshot_paths"][0]) / "content"
                    self.eq((content / "untracked.txt").read_text(), "keep")
                    self.eq((content / "tracked.txt").read_text(), "changed")

    def test_disabled_legacy_state(self):
        with scenario() as h:
            initial, results = dict(S.load_state(h.sd)), []
            self.assertTrue(S.acquire_lease(h.sd)[0])
            h.addCleanup(S.release_lease, h.sd)
            result = S.CHECK_RESULT_PREFIX + ' {"produced_head":null,"receipt_sha256":"' + "f"*64 + '"}'
            with mock.patch.object(P, "maybe_resume", side_effect=AssertionError("off")), \
                    mock.patch.object(S.SE, "_check", return_value=(S.FAILED, "", "", result)), \
                    mock.patch.object(S.SW, "_archive_code_worktree"), \
                    mock.patch.object(S.time, "time", return_value=1000):
                for cfg in (None, {"max": 0}):
                    state = copy.deepcopy(initial)
                    h.unit.pop("provider_resume", None)
                    if cfg is not None:
                        h.unit["provider_resume"] = cfg
                    state["plan_digest"] = S.plan_digest(h.plan)
                    S.advance(h.plan, state, str(h.sd), str(h.tmp / "runs"), False, max_new=0)
                    self.eq(state["units"]["code"]["state"], "FAILED")
                    results.append(json.dumps(state["units"]["code"], sort_keys=True))
            self.eq(*results)
        for cfg in (None, {"max": 0}):
            with self.subTest(cfg=cfg), scenario() as h:
                h.unit.pop("provider_resume")
                if cfg is not None:
                    h.unit["provider_resume"] = cfg
                h.change_plan()
                h.cli()
                h.expect(state="FAILED")
                self.absent("provider_resumes", h.us())
                h.sent(0)

    def test_no_delivery_inference(self):
        tree = ast.parse(Path(P.__file__).read_text())
        strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                   and isinstance(n.value, str)}
        self.assertFalse({"sending_at", "sent", "sent_at", "pending", "updated_at"} & strings)
        with scenario() as h:
            h.ready()
            h.cli()
            self.eq((h.tmp / "observations").read_text(), "2")


class ProviderResumeValidationTests(Checks):
    def test_validation(self):
        with scenario() as h:
            for cfg in (None, [], 2, {}, {"max": True}, {"max": -1},
                           {"max": 4}, {"max": "2"}, {"max": 1.5}):
                h.unit["provider_resume"] = cfg
                self.contains("provider_resume", h.cli("validate", expected=1))
            for value in range(4):
                h.unit["provider_resume"] = {"max": value}
                h.cli("validate")
            for kind in ("slurm", "pipeline"):
                h.plan["units"] = [{"id": "noncode", "kind": kind,
                                    "command": "true", "outputs": ["o"], "runtime": "none"}]
                h.cli("validate")
                h.plan["units"][0]["provider_resume"] = {"max": 0}
                output = h.cli("validate", expected=1)
                self.contains("noncode", output)
                self.contains("provider_resume", output)


if __name__ == "__main__":
    unittest.main()
