"""Typed test fixtures contained within a fresh private POSIX session.

Boundary: any descendant that invokes setsid or otherwise moves into another
session, including a daemonizing double fork, is OUTSIDE this contract. Such
processes may survive cleanup and are never advertised as contained. Tests
creating an escape must arrange independent, out-of-band teardown. This helper
is for trusted repository fixtures on unprivileged macOS/Linux, not a sandbox.

All launch fields and the directory are validated before spawning, opening
fixture pipes, creating the private session or mutating the ledger. Root and
cwd resolve strictly to existing directories; component-aware containment
rejects symlinks resolving outside the root. Only the canonical cwd is launched.
The supervisor enters its private session before any fixture code runs.

Containment enumerates ONLY pid, pgid and sid, scoped to the fixture session
or ledger groups. It never reads process environments, cwd, executable or birth
metadata, and unrelated row churn never enters the stability condition. Ledger
groups are keyed by (sid, pgid); current session membership is checked before
every group signal. A stale group containing only another sid is absent, never
a signal target. The directly owned supervisor reserves the sid until final
containment where practicable. These are POSIX observations, not atomic process
handles; fixtures must not change uid or delegate to pre-existing services.

ESRCH after an observation triggers a scoped rescan. Already-reaped or absent
processes can yield STATUS_UNAVAILABLE; no signal exit status is invented.
EPERM, failed/malformed scoped scans, unresolved in-session identity ambiguity
and genuine signal/wait failures remain ERROR or INDETERMINATE. Every failed
cleanup runs emergency containment in a finally path: retry scoped enumeration,
signal ledger/last-observed fixture groups, escalate to KILL and reap the direct
supervisor. Failure caches never suppress that work or a later cleanup retry.
Only established scoped absence and required reaping permit CLEAN and a no-op.
Orphan descendants rely on their parent or the OS reaper to disappear.

The API accepts FixtureSpec only, with fixed empty stdin and separate captured
UTF-8-replacement-decoded output files. Unsupported Popen fields are refused.
All readiness and reap waits use selectors. join waits for a supervisor report,
not pipe EOF; output is a snapshot at that report. Cleanup is independent of the
availability of child status. Product assertions remain the consumers' concern;
process_table is a diagnostic helper and never containment authority.
Consumers needing descendant effects can wait_quiescent without signalling:
it observes absence of every session member except the retained supervisor,
then reads fresh output. It neither cleans up nor changes the join result.
"""
from dataclasses import dataclass
from enum import Enum
import errno
import fcntl
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import time
from typing import Optional, Tuple


class JoinState(Enum):
    EXITED = 'EXITED'
    TIMED_OUT = 'TIMED_OUT'
    STATUS_UNAVAILABLE = 'STATUS_UNAVAILABLE'
    ERROR = 'ERROR'


class CleanupState(Enum):
    CLEAN = 'CLEAN'
    ERROR = 'ERROR'
    INDETERMINATE = 'INDETERMINATE'


class QuiescenceState(Enum):
    QUIESCENT = 'QUIESCENT'
    TIMED_OUT = 'TIMED_OUT'
    ERROR = 'ERROR'
    INDETERMINATE = 'INDETERMINATE'


class SignalState(Enum):
    SENT = 'SENT'
    ALREADY_EXITED = 'ALREADY_EXITED'
    STATUS_UNAVAILABLE = 'STATUS_UNAVAILABLE'
    ERROR = 'ERROR'


class FixtureSignal(Enum):
    TERM = (signal.SIGTERM, False)
    KILL = (signal.SIGKILL, False)
    HUP = (signal.SIGHUP, False)
    INT = (signal.SIGINT, False)
    TERM_GROUP = (signal.SIGTERM, True)


@dataclass(frozen=True)
class JoinResult:
    state: JoinState
    returncode: Optional[int] = None
    stdout: str = ''
    stderr: str = ''
    detail: str = ''


@dataclass(frozen=True)
class CleanupResult:
    state: CleanupState
    detail: str = ''


@dataclass(frozen=True)
class QuiescenceResult:
    state: QuiescenceState
    stdout: str = ''
    stderr: str = ''
    detail: str = ''


@dataclass(frozen=True)
class SignalResult:
    state: SignalState
    detail: str = ''


@dataclass(frozen=True)
class FixtureSpec:
    """An authored command with fixed I/O and supervision, no Popen options.

    command is a nonempty tuple of strings; environment is a tuple of string
    pairs (None inherits the caller); directory is an absolute string or None.
    Environment values are copied into the private launch, never caller fds.
    The caller is responsible for the documented trusted-fixture convention.
    """
    command: Tuple[str, ...]
    environment: Optional[Tuple[Tuple[str, str], ...]] = None
    directory: Optional[str] = None

    def __post_init__(self):
        self.validate()

    def validate(self):
        if type(self) is not FixtureSpec or set(vars(self)) != {
                'command', 'environment', 'directory'}:
            raise TypeError('only repository-defined FixtureSpec fields are supported')
        if (type(self.command) is not tuple or not self.command or
                any(type(x) is not str or '\0' in x for x in self.command) or
                not self.command[0]):
            raise ValueError('command must be a nonempty string tuple')
        if self.directory is not None and (
                type(self.directory) is not str or '\0' in self.directory or
                not os.path.isabs(self.directory)):
            raise ValueError('directory must be an absolute string')
        if self.environment is not None:
            if type(self.environment) is not tuple:
                raise ValueError('environment must be a tuple of string pairs')
            keys = set()
            for pair in self.environment:
                if (type(pair) is not tuple or len(pair) != 2 or
                        any(type(x) is not str or '\0' in x for x in pair)):
                    raise ValueError('environment must be a tuple of string pairs')
                key = pair[0]
                if not key or '=' in key or key in keys:
                    raise ValueError('invalid, duplicate or reserved environment key')
                keys.add(key)


class FixtureRefused(ValueError):
    pass


class _Indeterminate(RuntimeError):
    pass


@dataclass(frozen=True)
class _Identity:
    pgid: int
    sid: int


def _identity(pid):
    """Read membership only; an exit race is absence, other errors propagate."""
    try:
        sid = os.getsid(pid)
        pgid = os.getpgid(pid)
        if os.getsid(pid) != sid:
            raise _Indeterminate('session changed during membership inspection')
        return _Identity(pgid, sid)
    except ProcessLookupError:
        return None


def _session_rows(sid, groups, timeout=5):
    """Census membership, retaining only the session or its ledger groups.

    macOS ps has no numeric sid column, so getsid supplies that field. ESRCH
    on any departing row is absence; unrelated rows need no second observation.
    No command, environment, directory, uid or birth field is requested.
    """
    output = subprocess.check_output(
        ['/bin/ps', '-U', str(os.geteuid()), '-o', 'pid=,pgid='],
        text=True, timeout=timeout)
    rows, seen = {}, set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2:
            raise _Indeterminate('malformed membership enumeration')
        try:
            pid, pgid = map(int, parts)
        except ValueError as error:
            raise _Indeterminate('malformed membership enumeration') from error
        if pid <= 0 or pgid <= 0 or pid in seen:
            raise _Indeterminate('invalid membership enumeration')
        seen.add(pid)
        try:
            observed_sid = os.getsid(pid)
        except ProcessLookupError:
            continue
        if observed_sid == sid or (sid, pgid) in groups:
            # A ledger hit alone has no authority: retain its foreign sid so
            # the consumer can explicitly discard a reused group.
            rows[pid] = _Identity(pgid, observed_sid)
    # The scanner itself cannot vanish during this call. This single-row
    # completeness sentinel requires no stability from unrelated processes.
    if os.getpid() not in seen:
        raise _Indeterminate('membership census omitted its live caller')
    return rows


def _pause(seconds):
    with selectors.DefaultSelector() as readiness:
        readiness.select(seconds)


def _private_pipe():
    """Keep internal channels out of a caller's absent standard descriptors.

    Filling an absent stdin/stdout/stderr would change the child's inherited
    I/O and let its redirections overwrite a supervisor channel. Move both
    ends above stdio before launching either kind of supervisor.
    """
    descriptors = list(os.pipe())
    try:
        for index, fd in enumerate(descriptors):
            if fd < 3:
                descriptors[index] = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
                os.close(fd)
        return tuple(descriptors)
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise

def wait_readable(fd, timeout):
    """Wait on a pipe without select(2)'s descriptor-number ceiling."""
    with selectors.DefaultSelector() as readiness:
        readiness.register(fd, selectors.EVENT_READ)
        return bool(readiness.select(timeout))

def process_table():
    """Enumerate this unprivileged uid with untruncated diagnostic commands."""
    with subprocess.Popen(
            ['/bin/ps', '-ww', '-U', str(os.getuid()), '-o',
             'pid=,ppid=,pgid=,uid=,stat=,command='],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as reader:
        try:
            output, error = reader.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            reader.kill()
            reader.wait(timeout=5)
            raise
        if reader.returncode != 0:
            raise OSError('process enumeration failed: ' + error)
    rows = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) != 6:
            raise _Indeterminate('malformed process enumeration')
        pid, ppid, pgid, uid = map(int, parts[:4])
        if uid == os.geteuid() and pid != reader.pid:
            rows[pid] = (ppid, pgid, parts[4], parts[5])
    if os.getpid() not in rows:
        raise _Indeterminate('process enumeration omitted its caller')
    return rows

# The supervisor is a separate interpreter, not a caller-side Popen facade.
# Its two inherited descriptors are implementation channels, never fixture input.
_SUPERVISOR = r'''
import json, os, selectors, signal, subprocess, sys, time
report, control, config_path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
os.set_inheritable(report, False)
os.set_inheritable(control, False)
signal.signal(signal.SIGCHLD, signal.SIG_DFL)
def send(value):
    os.write(report, (json.dumps(value) + "\n").encode())
with open(config_path) as stream:
    config = json.load(stream)
send({'ready': True})
with selectors.DefaultSelector() as readiness:
    readiness.register(control, selectors.EVENT_READ)
    if not readiness.select(60):
        sys.exit(2)
if os.read(control, 1) != b'G':
    sys.exit(2)
try:
    with open(config['stdout'], 'wb') as out, open(config['stderr'], 'wb') as err:
        child = subprocess.Popen(config['command'], cwd=config['directory'],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            preexec_fn=os.setpgrp, restore_signals=True, close_fds=True)
except BaseException as error:
    send({'launch_error': repr(error)})
else:
    send({'pid': child.pid})
    pending = b''
    with selectors.DefaultSelector() as readiness:
        readiness.register(control, selectors.EVENT_READ)
        while child.poll() is None:
            if readiness.select(0.01):
                chunk = os.read(control, 4096)
                if not chunk:
                    raise RuntimeError('fixture control lost')
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    signum, group = json.loads(line)
                    if child.poll() is None:
                        try:
                            if group:
                                os.killpg(child.pid, signum)
                            else:
                                child.send_signal(signum)
                        except ProcessLookupError:
                            pass
    send({'returncode': child.returncode})
while True:
    with selectors.DefaultSelector() as readiness:
        readiness.select(600)
'''


class FixtureProcess:
    """Typed status and independently checked cleanup for one fixture launch.

    join(timeout) returns TIMED_OUT without changing terminal status. terminate
    asks the sole reaper to signal the direct child (TERM_GROUP is the fixed
    child group). It never signals a saved child PID from the caller. cleanup
    contains remaining same-session groups, then kills and reaps its retained
    supervisor. Failed cleanup is retried and always attempts emergency work.
    """
    def __init__(self, scope, spec, supervisor, report, control, stem):
        self.scope, self.spec = scope, spec
        self._supervisor = supervisor
        self.supervisor_pid = supervisor.pid
        self._report, self._control = report, control
        self.stdout_path = stem.with_suffix('.stdout')
        self.stderr_path = stem.with_suffix('.stderr')
        self._buffer = b''
        self._joined = None
        self._cleaned = None
        self._signals = {}
        self._last_observed = set()
        self._reaped = False
        self._groups = set()
        self._anchor = _Identity(supervisor.pid, supervisor.pid)
        self.child_pid = None

    def _read(self, timeout):
        deadline = time.monotonic() + timeout
        while b'\n' not in self._buffer:
            if not wait_readable(self._report, max(0, deadline - time.monotonic())):
                return None
            chunk = os.read(self._report, 4096)
            if not chunk:
                raise EOFError('supervisor report channel closed')
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b'\n', 1)
        return json.loads(line)

    def _output(self, state, code=None, detail=''):
        return JoinResult(state, code,
                          self.stdout_path.read_text(encoding='utf-8', errors='replace'),
                          self.stderr_path.read_text(encoding='utf-8', errors='replace'), detail)

    def join(self, timeout=60):
        if self._joined is not None:
            return self._joined
        if type(timeout) not in (int, float) or not 0 <= timeout < float('inf'):
            raise ValueError('join timeout must be finite and nonnegative')
        try:
            report = self._read(timeout)
            if report is None:
                return self._output(JoinState.TIMED_OUT)
            if set(report) != {'returncode'} or type(report['returncode']) is not int:
                raise ValueError('invalid supervisor status report')
            self._joined = self._output(JoinState.EXITED, report['returncode'])
        except EOFError as error:
            self._joined = self._output(JoinState.STATUS_UNAVAILABLE, detail=str(error))
        except (OSError, ValueError, TypeError) as error:
            self._joined = JoinResult(JoinState.ERROR, detail=str(error))
        return self._joined

    def wait_quiescent(self, timeout=60):
        """Observe session quiescence without signalling or reaping anything.

        The retained supervisor is an idle identity anchor, not fixture work.
        Two scoped observations must contain no other member before the one
        monotonic deadline. Each census and selector wait uses the remaining
        budget; late observations and scan timeouts cannot report QUIESCENT.
        An inspection failure is typed and never invokes emergency cleanup.
        Callers can retry; join's cached child status/output remain unchanged.
        Escaped sessions and hostile same-uid writers remain outside scope.
        """
        if type(timeout) not in (int, float) or not 0 <= timeout < float('inf'):
            raise ValueError('quiescence timeout must be finite and nonnegative')
        deadline = time.monotonic() + timeout
        empty_before = False
        try:
            while time.monotonic() < deadline:
                targets = self._snapshot(deadline=deadline)
                empty = not any(pid != self.supervisor_pid for pid in targets)
                if time.monotonic() >= deadline:
                    break
                if empty and empty_before:
                    stdout = self.stdout_path.read_text(encoding='utf-8', errors='replace')
                    stderr = self.stderr_path.read_text(encoding='utf-8', errors='replace')
                    if time.monotonic() >= deadline:
                        break
                    return QuiescenceResult(QuiescenceState.QUIESCENT, stdout, stderr)
                empty_before = empty
                _pause(min(0.02, max(0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired as error:
            return QuiescenceResult(QuiescenceState.TIMED_OUT, detail=str(error))
        except _Indeterminate as error:
            return QuiescenceResult(QuiescenceState.INDETERMINATE, detail=str(error))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return QuiescenceResult(QuiescenceState.ERROR, detail=str(error))
        return QuiescenceResult(QuiescenceState.TIMED_OUT,
                                detail='session quiescence deadline expired')

    def terminate(self, requested=FixtureSignal.TERM):
        if type(requested) is not FixtureSignal:
            raise ValueError('terminate requires FixtureSignal')
        status = self.join(0)
        if status.state is JoinState.EXITED:
            return SignalResult(SignalState.ALREADY_EXITED)
        if status.state is JoinState.STATUS_UNAVAILABLE:
            return SignalResult(SignalState.STATUS_UNAVAILABLE)
        if status.state is JoinState.ERROR:
            return SignalResult(SignalState.ERROR, status.detail)
        if requested not in self._signals:
            try:
                os.write(self._control, (json.dumps(requested.value) + '\n').encode())
                self._signals[requested] = SignalResult(SignalState.SENT)
            except OSError as error:
                self._signals[requested] = SignalResult(SignalState.ERROR, str(error))
        return self._signals[requested]

    def _record(self, pid, identity):
        if identity.sid != self.supervisor_pid:
            return
        group = (identity.sid, identity.pgid)
        self._last_observed.add(group)
        if group not in self._groups:
            # Persist both identity components; PID files remain diagnostics.
            with self.scope.ledger_path.open('a') as record:
                record.write(json.dumps({'sid': group[0], 'pgid': group[1]}) + '\n')
            self._groups.add(group)

    def _snapshot(self, deadline=None):
        if deadline is None:
            rows = _session_rows(self.supervisor_pid, self._groups)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired('fixture session census', 0)
            rows = _session_rows(self.supervisor_pid, self._groups,
                                 timeout=min(5, remaining))
        targets = {pid: row for pid, row in rows.items()
                   if row.sid == self.supervisor_pid}
        if not self._reaped:
            anchor = _identity(self.supervisor_pid)
            if anchor is not None:
                if anchor != self._anchor:
                    raise _Indeterminate('supervisor session identity changed')
                if targets.get(self.supervisor_pid) != anchor:
                    raise _Indeterminate('membership census omitted the supervisor')
        for pid, row in targets.items():
            self._record(pid, row)
        return targets

    def _survivor_scan(self):
        first = self._snapshot()
        second = self._snapshot()
        return second, first == second

    def _signal_group(self, group, signum):
        sid, pgid = group
        if sid != self.supervisor_pid:
            return
        rows = _session_rows(sid, {group})
        members = {pid: row for pid, row in rows.items() if row.pgid == pgid}
        members = {pid: row for pid, row in members.items() if row.sid == sid}
        if pgid == self.supervisor_pid and set(members) <= {self.supervisor_pid}:
            return  # Keep the directly owned leader until final containment.
        if not members:
            return  # A pgid reused in another session is absent for us.
        for pid in members:
            current = _identity(pid)
            if current is None or current != members[pid]:
                # Includes ESRCH and setpgid/setsid races. Re-enumerate rather
                # than signalling stale membership; the caller retries groups.
                self._snapshot()
                return
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            self._snapshot()

    def _signal_owned(self, pid, identity, signum):
        """Only the directly owned, unreaped leader may receive a PID signal."""
        if pid != self.supervisor_pid:
            raise _Indeterminate('PID signal requires the owned session leader')
        if self._reaped:
            return
        current = _identity(pid)
        if current is None:
            self._snapshot()
            return
        if current != identity:
            raise _Indeterminate('PID changed before fixture signal')
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            self._snapshot()

    def _reap(self):
        if self._reaped:
            return
        deadline = time.monotonic() + 5
        while True:
            try:
                got, status = os.waitpid(self.supervisor_pid, os.WNOHANG)
            except ChildProcessError:
                # Another caller already reaped it; check absence before
                # accepting loss of status. Never manufacture child status.
                if _identity(self.supervisor_pid) is not None:
                    raise _Indeterminate('leader is no longer waitable but still present')
                self._reaped = True
                # Popen bookkeeping only, not an advertised fixture status.
                self._supervisor.returncode = 0
                return
            except InterruptedError:
                got = 0
            if got:
                self._supervisor.returncode = os.waitstatus_to_exitcode(status)
                self._reaped = True
                return
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('fixture supervisor reap', 5)
            _pause(0.01)

    def _contain(self, grace=0.2, budget=8):
        started = time.monotonic()
        deadline = started + budget
        term_deadline = None
        while True:
            observed_after = time.monotonic()
            targets, stable = self._survivor_scan()
            descendants = {pid: row for pid, row in targets.items()
                           if pid != self.supervisor_pid}
            if not descendants and stable:
                break
            signum = (signal.SIGTERM if term_deadline is None or
                      observed_after < term_deadline else signal.SIGKILL)
            for group in {(row.sid, row.pgid) for row in descendants.values()}:
                self._signal_group(group, signum)
            if term_deadline is None:
                # Arm grace once, after the first TERM round. Scheduling and
                # census latency before any signal cannot skip TERM or spend
                # the response interval; the overall cleanup budget stays fixed.
                term_deadline = time.monotonic() + grace
            if observed_after >= deadline:
                raise _Indeterminate('fixture session did not reach scoped absence')
            _pause(0.02)
        self.join(0)
        self._signal_owned(self.supervisor_pid, self._anchor, signal.SIGKILL)
        self._reap()
        targets, stable = self._survivor_scan()
        if targets or not stable:
            raise _Indeterminate('scoped absence not established after leader reap')
        self.join(0)
        if self._joined is None:
            self._joined = self._output(JoinState.STATUS_UNAVAILABLE,
                                       detail='cleanup closed supervisor transport')
        for name in ('_report', '_control'):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def _emergency_containment(self):
        """Best effort on every unsuccessful cleanup, preserving its verdict.

        A failed scan does not bypass the ledger or direct-child reap. Signals
        still require fresh session membership; repeated inspection failure
        cannot justify signalling an unverified numeric group.
        """
        errors = []
        for signum in (signal.SIGTERM, signal.SIGKILL, signal.SIGKILL):
            try:
                self._snapshot()
            except (OSError, ValueError, _Indeterminate, subprocess.SubprocessError) as error:
                errors.append(str(error))
            for group in self._groups | self._last_observed:
                try:
                    self._signal_group(group, signum)
                except (OSError, ValueError, _Indeterminate, subprocess.SubprocessError) as error:
                    errors.append(str(error))
            _pause(0.02)
        try:
            self._signal_owned(self.supervisor_pid, self._anchor, signal.SIGKILL)
        except (OSError, ValueError, _Indeterminate, subprocess.SubprocessError) as error:
            errors.append(str(error))
        try:
            self._reap()
        except (OSError, ValueError, _Indeterminate, subprocess.SubprocessError) as error:
            errors.append(str(error))
        return errors

    def _perform_cleanup(self):
        result = None
        try:
            self._contain()
            result = CleanupResult(CleanupState.CLEAN)
        except _Indeterminate as error:
            result = CleanupResult(CleanupState.INDETERMINATE, str(error))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            result = CleanupResult(CleanupState.ERROR, str(error))
        finally:
            if result is None or result.state is not CleanupState.CLEAN:
                self._emergency_containment()
        return result

    def cleanup(self):
        if self._cleaned is None or self._cleaned.state is not CleanupState.CLEAN:
            self._cleaned = self._perform_cleanup()
        return self._cleaned


class FixtureProcesses:
    """Temporary fixture scope; unittest consumes typed cleanup failures.

    Directory deletion is registered by the caller before this scope. The
    independent guard runs after cleanup and before deletion, and reports
    omitted cleanup through unittest even if it can subsequently contain it.
    """
    def __init__(self, case, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise FixtureRefused('fixture root must be a directory')
        self.ledger_path = self.root / 'fixture-session-groups.jsonl'
        self.children = []
        self.pidfiles = [self.root / 'fixture-groups.pid']
        self._sleep_wrapper = None
        case.addCleanup(self.assert_no_survivors)
        case.addCleanup(self.cleanup)

    def launch(self, spec):
        if type(spec) is not FixtureSpec:
            raise TypeError('launch requires a FixtureSpec')
        spec.validate()
        if self.children:
            raise FixtureRefused('one launch per exclusive fixture scope')
        directory = (self.root if spec.directory is None else
                     Path(spec.directory)).resolve(strict=True)
        root = self.root.resolve(strict=True)
        if (not root.is_dir() or not directory.is_dir() or
                os.path.commonpath((str(root), str(directory))) != str(root)):
            raise FixtureRefused('fixture cwd must resolve to a directory beneath its root')
        if sys.platform not in ('darwin', 'linux') or os.geteuid() == 0:
            raise FixtureRefused('requires unprivileged macOS/Linux fixture inspection')
        # Check membership capability without spawning or touching the ledger.
        if _identity(os.getpid()) is None:
            raise FixtureRefused('cannot inspect current session membership')
        stem = self.root / ('fixture-launch-%d' % len(self.children))
        script, config_path = stem.with_suffix('.py'), stem.with_suffix('.json')
        out_path, err_path = stem.with_suffix('.stdout'), stem.with_suffix('.stderr')
        script.write_text(_SUPERVISOR)
        out_path.write_bytes(b'')
        err_path.write_bytes(b'')
        config_path.write_text(json.dumps({
            'command': spec.command, 'directory': str(directory),
            'stdout': str(out_path), 'stderr': str(err_path)}))
        env = dict(os.environ if spec.environment is None else spec.environment)
        report_read, report_write = _private_pipe()
        try:
            control_read, control_write = _private_pipe()
        except BaseException:
            os.close(report_read)
            os.close(report_write)
            raise
        proc = None
        try:
            supervisor = subprocess.Popen(
                [sys.executable, '-I', str(script), str(report_write),
                 str(control_read), str(config_path)],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
                pass_fds=(report_write, control_read))
            proc = FixtureProcess(self, spec, supervisor,
                                  report_read, control_write, stem)
            self.children.append(proc)
        except BaseException:
            os.close(report_read)
            os.close(control_write)
            raise
        finally:
            os.close(report_write)
            os.close(control_read)
        try:
            if proc._read(60) != {'ready': True}:
                raise FixtureRefused('supervisor did not reach launch barrier')
            if _identity(supervisor.pid) != proc._anchor:
                raise FixtureRefused('private session was not established')
            proc._record(supervisor.pid, proc._anchor)
            os.write(proc._control, b'G')
            started = proc._read(60)
            if not started or set(started) != {'pid'} or type(started['pid']) is not int:
                raise FixtureRefused('fixture launch failed: %r' % started)
            proc.child_pid = started['pid']
            # The supervisor fixes the child's group before exec. The child
            # may exit before this observation; the retained SID still anchors
            # any same-session descendants; setsid escapes are outside this contract.
            child = _identity(proc.child_pid)
            if child is not None:
                proc._record(proc.child_pid, child)
            return proc
        except BaseException:
            proc.cleanup()
            raise

    def _root_rows(self, table):
        """Independent diagnostic only; root strings never authorize a kill."""
        roots = {str(self.root), os.path.realpath(str(self.root))}
        return {pid: row for pid, row in table.items()
                if pid != os.getpid() and not row[2].startswith('Z')
                and any(root + '/' in row[3] or row[3].endswith(root)
                        for root in roots)}

    def cleanup(self):
        failures = [result for result in (p.cleanup() for p in self.children)
                    if result.state is not CleanupState.CLEAN]
        if failures:
            raise AssertionError('fixture cleanup failed: %r' % failures)

    def assert_no_survivors(self):
        failures = []
        for proc in self.children:
            try:
                targets, stable = proc._survivor_scan()
                if targets or not stable or proc._cleaned is None or (
                        proc._cleaned.state is not CleanupState.CLEAN):
                    failures.append((sorted(targets), stable, proc._cleaned))
            except (OSError, ValueError, _Indeterminate, subprocess.SubprocessError) as error:
                failures.append(str(error))
        if failures:
            # Preserve the failed observation before emergency containment.
            for proc in self.children:
                proc.cleanup()
            raise AssertionError('fixture processes survived cleanup or absence is unknown: %r' % failures)

    def shell_script(self, source):
        """Record the shell and keep its sleep descendants identifiable."""
        header = '#!/bin/sh\n'
        if not source.startswith(header):
            raise ValueError('fixture must have a /bin/sh shebang')
        return (header + "printf '%s\\n' \"$$\" >> " +
                shlex.quote(str(self.pidfiles[0])) + ' || exit 1\n' +
                'sleep() { ' + shlex.quote(str(self.sleep_command())) +
                ' "$@"; }\n' + source[len(header):])

    def sleep_command(self):
        """Record a sleep PID and retain its root marker through exec.

        PATH lookup does not preserve a symlink's path in argv. Invoke this
        wrapper absolutely; it execs an absolute alias without changing the
        PID, process group, signal dispositions, or inherited descriptors.
        """
        if self._sleep_wrapper is None:
            alias = self.root / 'fixture-sleep-executable'
            alias.symlink_to('/bin/sleep')
            wrapper = self.root / 'fixture-sleep'
            wrapper.write_text(
                '#!/bin/sh\n' + "printf '%s\\n' \"$$\" >> " +
                shlex.quote(str(self.pidfiles[0])) + ' || exit 1\n' +
                'exec ' + shlex.quote(str(alias)) + ' "$@"\n')
            wrapper.chmod(0o700)
            self._sleep_wrapper = wrapper
        return self._sleep_wrapper

    def record_pidfile(self, path):
        self.pidfiles.append(Path(path))
