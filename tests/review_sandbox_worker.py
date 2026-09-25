"""Process boundary for the delegated review tests (ARC-1035).

Contract: unittest discovery and module-level invocation delegate test_review to a
sys.executable worker with fake HOME and XDG_STATE_HOME BEFORE interpreter
startup/import/collection. Its load_tests hook requires a parent token and
worker-local PID activation; inherited markers never authorize in-process
execution in another interpreter.
The parent owns the temporary root, seeds dedicated journal namespaces, waits
for worker termination without reaping, rejects surviving ordinary descendants,
signals live session members with TERM then KILL and establishes quiescence before
comparing recursive names, types and bytes. It reaps its child; the OS reaps
orphan descendants. Zombies cannot write and are not live survivors. No
teardown fixture supplies the audit. The unreaped leader reserves the session
ID until the final scan; there are no session scans or signals after reaping.
Timeout, signal death, unreadable state, protocol/comparison errors, missing
completion, import/setup failure and worker test failure/error all fail closed.
The completion file contains every collected id and outcome, including skips;
a wholly skipped module fails. Collection guards consume those ids, never an
exemption. Existing per-test fixtures remain, and isolated writes elsewhere do
not affect the protected journal comparison.

This is trusted test plumbing, not a security boundary against arbitrary
same-UID code replacing the supervisor or forging its protocol. Descendants
that deliberately detach with setsid, and fully qualified class/method
selections that bypass load_tests, are outside this contract. There is
no writer registry, stack inspection or runtime attribution.
"""
import collections
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import secrets
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
WORKER_ROOT = "HANIG_REVIEW_SANDBOX_ROOT"
_WORKER_PID = None
# The old three-writer bound was 600s. The complete module needs minutes;
# allow 30 minutes for loaded CI hosts, while every timeout remains a failure.
REVIEW_TIMEOUT = 1800
SEED = b"operator journal history\x00\xff\n"
_REVIEW_RUN = None


class SandboxFailure(AssertionError):
    pass


def nonreaping_waiter():
    """Return a waitid(WNOWAIT) exit observer, or refuse before launch.

    CPython does not expose os.waitid on macOS. Darwin's native waitid and
    siginfo_t (sys/wait.h and sys/signal.h) provide the same nonreaping API;
    use ctypes only on that ABI. Never substitute Popen.poll/wait here.
    """
    required = ("P_PID", "WEXITED", "WNOWAIT", "WNOHANG")
    if not all(hasattr(os, name) for name in required):
        raise SandboxFailure("sandbox requires waitid with WNOWAIT")
    options = os.WEXITED | os.WNOWAIT | os.WNOHANG
    if callable(getattr(os, "waitid", None)):
        def observe(pid):
            info = os.waitid(os.P_PID, pid, options)
            return info is not None and info.si_pid == pid
        return observe
    if sys.platform != "darwin":
        raise SandboxFailure("sandbox requires waitid with WNOWAIT")

    class Siginfo(ctypes.Structure):
        _fields_ = [
            ("si_signo", ctypes.c_int), ("si_errno", ctypes.c_int),
            ("si_code", ctypes.c_int), ("si_pid", ctypes.c_int),
            ("si_uid", ctypes.c_uint), ("si_status", ctypes.c_int),
            ("si_addr", ctypes.c_void_p), ("si_value", ctypes.c_void_p),
            ("si_band", ctypes.c_long), ("padding", ctypes.c_ulong * 7)]

    try:
        waitid = ctypes.CDLL(None, use_errno=True).waitid
    except (OSError, AttributeError) as exc:
        raise SandboxFailure("sandbox requires waitid with WNOWAIT: %s" % exc)
    waitid.argtypes = [ctypes.c_int, ctypes.c_uint,
                      ctypes.POINTER(Siginfo), ctypes.c_int]
    waitid.restype = ctypes.c_int

    def observe(pid):
        info = Siginfo()
        if waitid(os.P_PID, pid, ctypes.byref(info), options) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return info.si_pid == pid
    return observe


def worker_exited(process, observe):
    """Check ownership before any session scan; never release the leader PID."""
    if process.returncode is not None:
        raise SandboxFailure("worker was reaped before session quiescence")
    # ECHILD is a lost reservation, not evidence of a quiescent owned session.
    return observe(process.pid)


def await_worker_exit(process, observe, timeout):
    deadline = time.monotonic() + timeout
    while not worker_exited(process, observe):
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(0.01)


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


def session_members(sid):
    """List live (pid, pgid) pairs in the owned session, without env/cwd reads.

    macOS ps has no numeric sid column (sess is not a session id), so use
    getsid for each listed live pid. A process exiting during enumeration is
    ordinary host churn, not an audit failure. Other inspection errors fail
    closed. Zombies cannot write; their reaping belongs to their OS parent.
    """
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True,
        check=True, timeout=10)
    members = []
    for line in result.stdout.splitlines():
        pid, group, state = line.split()
        if state.startswith("Z"):
            continue
        pid, group = int(pid), int(group)
        try:
            session = os.getsid(pid)
        except ProcessLookupError:
            continue
        if session == sid:
            members.append((pid, group))
    return members


def stop_session(process, observe):
    """Contain session members while its leader reserves the ID, then reap.

    Signal individual live members after rechecking their session, not stale
    ps process-group IDs: a descendant can leave or retire a process group
    between enumeration and containment. No killpg can reach a foreign group.
    This is not an atomic identity-and-signal primitive for descendant PIDs.
    """
    for sig, grace in ((signal.SIGTERM, 1), (signal.SIGKILL, 10)):
        deadline = time.monotonic() + grace
        signalled = set()
        while True:
            exited = worker_exited(process, observe)
            members = session_members(process.pid)
            if exited and not members:
                process.wait(timeout=10)
                return
            for pid, _group in members:
                if sig == signal.SIGTERM and pid in signalled:
                    continue
                try:
                    if os.getsid(pid) != process.pid:
                        continue
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    if any(p == pid for p, _g in session_members(process.pid)):
                        raise
                signalled.add(pid)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise SandboxFailure("could not establish worker-session quiescence")


def launch_environment_matches():
    """Check the parent's random token and the fake launch homes, fail closed."""
    token = os.environ.get(WORKER_MARKER, "")
    root_value = os.environ.get(WORKER_ROOT, "")
    if (len(token) != 64 or any(c not in "0123456789abcdef" for c in token)
            or not root_value or not Path(root_value).is_absolute()):
        return False
    try:
        root = Path(root_value).resolve(strict=True)
        for key in ("HOME", "XDG_STATE_HOME", "TMPDIR"):
            value = os.environ.get(key, "")
            if not value or not Path(value).is_absolute():
                return False
            path = Path(value).resolve(strict=True)
            if root not in path.parents or not path.is_dir():
                return False
        fd = os.open(root / "worker-token", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            return (stat.S_ISREG(info.st_mode) and info.st_size == 64
                    and os.read(fd, 65) == token.encode("ascii"))
        finally:
            os.close(fd)
    except (OSError, RuntimeError, ValueError):
        return False


def is_sandbox_worker():
    """Environment inheritance cannot grant this interpreter worker status.

    Only worker_main activates the current PID after checking the parent's
    launch token. Exec starts with no activation; fork inherits the old PID.
    The file token and contained homes remain required at collection time.
    This is accidental-inheritance protection, not a same-UID security gate.
    """
    return _WORKER_PID == os.getpid() and launch_environment_matches()


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
    observe = nonreaping_waiter()
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
    token = secrets.token_hex(32)
    (root / "worker-token").write_text(token)
    env = dict(os.environ, HOME=str(home), XDG_STATE_HOME=str(state),
               TMPDIR=str(tmp), **{WORKER_MARKER: token, WORKER_ROOT: str(root)})
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
                await_worker_exit(process, observe, timeout)
            except subprocess.TimeoutExpired:
                problems.append("worker timeout after %ss" % timeout)
            else:
                survivors = session_members(process.pid)
                if survivors:
                    problems.append("surviving worker descendants: %r" % survivors)
            finally:
                # Includes timeout, process-inspection errors and parent interruption.
                stop_session(process, observe)
                quiet = True
            if process.returncode != 0:
                problems.append("worker exit %s (signal death if negative)" % process.returncode)
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


class DelegatedReviewTests(unittest.TestCase):
    """One parent test, shared by module loading and full discovery.

    Keep the delegate outside discoverable test modules so discovery cannot
    collect a second copy. Only review_report's audited result may pass it.
    """
    def __init__(self, methodName="runTest", patterns=None):
        super().__init__(methodName)
        self.patterns = patterns

    def runTest(self):
        if self.patterns:
            print("test_review is delegated: -k selection runs the whole module "
                  "in the supervised worker.", file=sys.stderr)
        report = review_report()
        self.assertIs(report, review_report())
        self.assertTrue(report["collected"])
        skips = report["skipped"]
        print("\nDelegated test_review: %d tests, %.3fs, %d skips" % (
            len(report["collected"]), report["elapsed_seconds"], len(skips)))
        for outcome in skips:
            print("  SKIP %s: %s" % (outcome["id"], outcome["reason"]))
        # Optional validation artifact, written by the parent only after audit.
        destination = os.environ.get("HANIG_REVIEW_SANDBOX_REPORT")
        if destination:
            Path(destination).write_text(json.dumps(report, indent=2))


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
    global _WORKER_PID
    if not launch_environment_matches():
        raise RuntimeError("worker must be launched by the sandbox parent")
    _WORKER_PID = os.getpid()
    # The script is __main__; the collection hook imports this package name.
    # Keep one activation object. A fresh interpreter importing the helper
    # starts with _WORKER_PID=None even if all launch variables were inherited.
    sys.modules["tests.review_sandbox_worker"] = sys.modules[__name__]
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
