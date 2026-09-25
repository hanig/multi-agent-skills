"""Process boundary for the delegated review tests (ARC-1035).

Contract: repository unittest discovery delegates test_review exactly once to a
sys.executable worker with fake HOME and XDG_STATE_HOME BEFORE interpreter
startup/import/collection. Its load_tests hook prevents in-process execution.
The parent owns the temporary root, seeds dedicated journal namespaces, waits
for worker termination, rejects surviving ordinary descendants, kills their
process group and establishes quiescence before comparing recursive names,
types and bytes. It reaps its child; the OS reaps orphan descendants. Zombies
cannot write and are not live survivors. No teardown fixture supplies the audit.
Timeout, signal death, unreadable state, protocol/comparison errors, missing
completion, import/setup failure and worker test failure/error all fail closed.
The completion file contains every collected id and outcome, including skips;
a wholly skipped module fails. Collection guards consume those ids, never an
exemption. Existing per-test fixtures remain, and isolated writes elsewhere do
not affect the protected journal comparison.

This is trusted test plumbing, not a security boundary against arbitrary
same-UID code replacing the supervisor or forging its protocol. Descendants
that deliberately detach with setsid, and ad hoc execution of test_review.py
outside the repository unittest command, are outside this contract. There is
no writer registry, stack inspection or runtime attribution.
"""
import collections
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKER_MARKER = "HANIG_REVIEW_SANDBOX_WORKER"
# The old three-writer bound was 600s. The complete module needs minutes;
# allow 30 minutes for loaded CI hosts, while every timeout remains a failure.
REVIEW_TIMEOUT = 1800
SEED = b"operator journal history\x00\xff\n"
_REVIEW_RUN = None


class SandboxFailure(AssertionError):
    pass


def manifest(root):
    """Read a dedicated tree after quiescence; never follow symlinks/FIFOs."""
    entries = {}

    def visit(path):
        info = path.lstat()
        name = str(path.relative_to(root))
        if stat.S_ISDIR(info.st_mode):
            if info.st_mode & 0o500 != 0o500:
                raise OSError("unreadable directory: %s" % path)
            entries[name] = ("directory", None)
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            if not info.st_mode & 0o400:
                raise OSError("unreadable file: %s" % path)
            entries[name] = ("file", path.read_bytes())
        else:
            raise OSError("unexpected entry type (including symlink): %s" % path)

    visit(Path(root))
    return entries


def group_members(pgid):
    """Read ordinary group liveness, not which process wrote an artifact."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True,
        check=True, timeout=10)
    members = []
    for line in result.stdout.splitlines():
        pid, group, state = line.split()
        if int(group) == pgid and not state.startswith("Z"):
            members.append(int(pid))
    return members


def stop_group(process):
    """Kill the owned session, reap the worker, and await no live members."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can return EPERM for an exited group containing only zombies.
        if group_members(process.pid):
            raise
    process.wait(timeout=10)
    deadline = time.monotonic() + 10
    while group_members(process.pid):
        if time.monotonic() >= deadline:
            raise SandboxFailure("could not establish process-group quiescence")
        time.sleep(0.05)


def validate_completion(report, module_name):
    """Transport exit zero alone says nothing about successful test execution."""
    if not isinstance(report, dict) or report.get("version") != 1:
        raise SandboxFailure("invalid completion handshake schema")
    if report.get("module") != module_name:
        raise SandboxFailure("completion handshake names a different module")
    ids, outcomes = report.get("collected"), report.get("outcomes")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise SandboxFailure("invalid collected ids in completion handshake")
    if len(set(ids)) != len(ids):
        raise SandboxFailure("duplicate collected test ids")
    if not isinstance(outcomes, list) or not all(
            isinstance(o, dict) and isinstance(o.get("id"), str)
            and isinstance(o.get("status"), str)
            and isinstance(o.get("traceback"), str) for o in outcomes):
        raise SandboxFailure("invalid outcomes in completion handshake")
    if collections.Counter(o.get("id") for o in outcomes) != collections.Counter(ids):
        raise SandboxFailure("completion outcomes do not match collected ids")
    if (not isinstance(report.get("problems"), list)
            or not all(isinstance(p, str) for p in report["problems"])
            or not isinstance(report.get("threads"), list)
            or not all(isinstance(t, str) for t in report["threads"])):
        raise SandboxFailure("invalid diagnostics in completion handshake")
    skipped = report.get("skipped")
    if not isinstance(skipped, list) or not all(
            isinstance(o, dict) and isinstance(o.get("id"), str)
            and isinstance(o.get("reason"), str) for o in skipped):
        raise SandboxFailure("invalid skips in completion handshake")
    problems = list(report["problems"])
    for outcome in outcomes:
        if outcome.get("status") not in ("success", "skip", "expected_failure"):
            problems.append("%s: %s\n%s" % (
                outcome.get("id"), outcome.get("status"), outcome.get("traceback", "")))
    if not ids:
        problems.append("module collected no tests")
    elif all(o.get("status") == "skip" for o in outcomes):
        problems.append("whole module was skipped")
    if report.get("threads"):
        problems.append("surviving worker threads: %r" % report["threads"])
    if problems:
        raise SandboxFailure("\n".join(str(p) for p in problems))
    return report


def run_sandbox(module_path, module_name, timeout=REVIEW_TIMEOUT):
    """Parent-owned launch and final audit. A failed cleanup retains the root."""
    root = Path(tempfile.mkdtemp(prefix="review-sandbox-")).resolve()
    home, state, tmp = root / "home", root / "state", root / "tmp"
    for path in (home, state, tmp):
        path.mkdir()
    protected = [home / ".local/state/hanig-review-gate",
                 state / "hanig-review-gate",
                 tmp / "hanig-review-gate-state/hanig-review-gate"]
    for namespace in protected:
        seed = namespace / "seed/record.jsonl"
        seed.parent.mkdir(parents=True)
        seed.write_bytes(SEED)
    before = {str(path): manifest(path) for path in protected}
    env = dict(os.environ, HOME=str(home), XDG_STATE_HOME=str(state),
               TMPDIR=str(tmp), **{WORKER_MARKER: "1"})
    # Test bodies supply their own mock keys. Never give the delegated module
    # operator credentials or an inherited per-test journal marker.
    for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
                "HANIG_REVIEW_GATE_TESTING"):
        env.pop(key, None)
    completion = root / "completion.json"
    process = None
    quiet = True
    problems = []
    report = None
    started = time.monotonic()
    try:
        with (root / "worker.log").open("w+", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()),
                 str(Path(module_path).resolve()), module_name, str(completion)],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
            quiet = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                problems.append("worker timeout after %ss" % timeout)
            else:
                if process.returncode != 0:
                    problems.append("worker exit %s (signal death if negative)" % process.returncode)
                survivors = group_members(process.pid)
                if survivors:
                    problems.append("surviving worker descendants: %r" % survivors)
            finally:
                # Includes timeout, process-inspection errors and parent interruption.
                stop_group(process)
                quiet = True
            try:
                after = {str(path): manifest(path) for path in protected}
                if after != before:
                    problems.append("protected journal manifest changed (names, types or bytes)")
            except Exception as exc:
                problems.append("protected journal comparison error: %s" % exc)
            try:
                info = completion.lstat()
                if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o400:
                    raise OSError("completion is not a readable regular file")
                report = json.loads(completion.read_text())
                validate_completion(report, module_name)
            except Exception as exc:
                problems.append("completion handshake failed: %s" % exc)
            if problems:
                log.seek(0)
                raise SandboxFailure("\n".join(problems) + "\nWorker output:\n" + log.read())
        report["elapsed_seconds"] = time.monotonic() - started
        return report
    finally:
        if quiet:
            # Tests deliberately chmod paths to exercise unreadability.
            for path in root.rglob("*"):
                if not path.is_symlink():
                    path.chmod(0o700 if path.is_dir() else 0o600)
            shutil.rmtree(root)
        else:
            print("sandbox cleanup could not establish quiescence; retained %s" % root,
                  file=sys.stderr)


def review_report():
    """Share one completed run (including failure) between parent and guard."""
    global _REVIEW_RUN
    if _REVIEW_RUN is None:
        try:
            report = run_sandbox(ROOT / "tests/test_review.py", "test_review")
            _REVIEW_RUN = (report, None)
        except Exception as exc:
            _REVIEW_RUN = (None, str(exc))
    report, error = _REVIEW_RUN
    if error is not None:
        raise SandboxFailure(error)
    return report


def walk_suite(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from walk_suite(test)
        else:
            yield test


class RecordingResult(unittest.TextTestResult):
    """Record terminal outcomes, including failed subtests and fixture errors."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = []
        self.problems = []
        self.events = {}

    def startTest(self, test):
        self.events[test.id()] = []
        super().startTest(test)

    def record(self, test, status, detail=""):
        entry = (status, detail)
        if test.id() in self.events:
            self.events[test.id()].append(entry)
        else:
            self.problems.append("%s: %s\n%s" % (test.id(), status, detail))

    def stopTest(self, test):
        events = self.events.pop(test.id())
        failed = [e for e in events if e[0] in ("failure", "error", "unexpected_success")]
        terminal = [e for e in events if e[0] in ("success", "skip", "expected_failure")]
        if failed:
            status = failed[0][0]
        elif terminal:
            status = terminal[-1][0]
        elif events:
            # A skipped subtest suppresses unittest's final addSuccess even
            # when other subtests or ordinary assertions succeeded. The case
            # ran; individual subtest skips remain explicit in skipped.
            status = "success"
        else:
            status = "missing_outcome"
        self.outcomes.append({"id": test.id(), "status": status,
                              "traceback": "\n".join(e[1] for e in events if e[1])})
        super().stopTest(test)

    def addSuccess(self, test):
        self.record(test, "success")
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.record(test, "failure", self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addError(self, test, err):
        self.record(test, "error", self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addSkip(self, test, reason):
        # unittest also reports class/module fixture and subtest skips here,
        # without startTest for that placeholder. Retain them in skipped;
        # they are not errors. Unstarted collected cases are accounted below.
        if test.id() in self.events:
            self.record(test, "skip", reason)
        elif getattr(test, "test_case", None) is not None:
            self.record(test.test_case, "subtest_skip", test.id() + ": " + reason)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self.record(test, "expected_failure", self._exc_info_to_string(err, test))
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.record(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            status = "failure" if issubclass(err[0], test.failureException) else "error"
            self.record(test, status, subtest.id() + "\n" + self._exc_info_to_string(err, test))
        else:
            self.record(test, "subtest_success")
        super().addSubTest(test, subtest, err)


def worker_main(module_path, module_name, completion):
    if os.environ.get(WORKER_MARKER) != "1":
        raise RuntimeError("worker must be launched by the sandbox parent")
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(module_path).parent))
    report = {"version": 1, "module": module_name, "collected": [],
              "outcomes": [], "problems": [], "threads": [], "skipped": []}
    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(module)
        cases = list(walk_suite(suite))
        report["collected"] = [test.id() for test in cases]
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
        report["outcomes"] = result.outcomes
        report["problems"] = result.problems + loader.errors
        report["skipped"] = [{"id": case.id(), "reason": reason}
                             for case, reason in result.skipped]
        skipped = {case.id(): reason for case, reason in result.skipped}
        finished = {o["id"] for o in result.outcomes}
        for case in cases:
            if case.id() in finished:
                continue
            cls = type(case)
            # These are unittest's fixture error-holder ids, matched to the
            # actual collected case class, not inferred from test.id() text.
            fixture_ids = ("setUpClass (%s.%s)" % (cls.__module__, cls.__qualname__),
                           "setUpModule (%s)" % cls.__module__)
            reason = next((skipped[i] for i in fixture_ids if i in skipped), None)
            report["outcomes"].append({
                "id": case.id(), "status": "skip" if reason is not None else "not_run",
                "traceback": reason or ""})
    except BaseException:
        report["problems"].append(traceback.format_exc())
    report["threads"] = [t.name for t in threading.enumerate()
                         if t is not threading.current_thread() and t.is_alive()]
    Path(completion).write_text(json.dumps(report))
    # Exit 0 means the transport finished. Only the parent interprets outcomes.


if __name__ == "__main__":
    worker_main(*sys.argv[1:])
