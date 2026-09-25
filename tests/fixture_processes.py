"""Fixture-specific process API; never evidence about the product's behavior.

The helper accepts only repository-defined FixtureSpec values and returns a
FixtureProcess whose join, terminate and cleanup semantics are defined
independently of subprocess.Popen; arbitrary Popen keywords, descriptor
passing, preexec hooks, identity changes, and caller-controlled session or
process-group behavior are REFUSED before launch. For every accepted launch,
cleanup may return CLEAN only after owned processes are reaped and it has
positively established, using the private-session/group ledger and an
inherited launch marker, that no descendant remains; failed scans, kills,
reaps, or ambiguous identities return ERROR or INDETERMINATE. Child status
is returned only when reported by the supervisor, so an externally killed
supervisor or group yields STATUS_UNAVAILABLE rather than a fabricated -9,
while cleanup independently proves absence or fails closed.

Only trusted repository fixtures on macOS/Linux are supported. Fixtures must
inherit their launch environment, retain the marker across exec, keep any
long-lived descendant cwd inside their exclusive fixture root, stay at the
same uid, and not delegate work to pre-existing services. This is a convention
for authored tests, not a sandbox for hostile commands. A fixture that cannot
obey that convention is unsupported. The scanner uses kernel birth identities and inherited environment/cwd
markers, never argv substring matching as ownership authority. macOS can hide
system-binary environments; the inherited cwd supplies a second marker. Each
scope accepts one launch and caller-selected directories outside it are refused.
Only our supervisor is waitable by this process; it reaps its child. Orphaned
descendants must disappear through their parent/OS reaper before CLEAN.

The private pipes, selectors waits, retained private-session supervisor and
session/group ledger are ported from attempt 3 (99d9853b). No Popen interface
or synthetic return-code fallback is retained. Stdio is fixed: empty stdin,
separate UTF-8-replacement-decoded output files. join waits for a supervisor
report, not pipe EOF; output is a snapshot at that report. Cleanup is a separate
operation. Terminal results are cached, including failures (no silent retry).
"""
import ctypes
import ctypes.util
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
import struct
import subprocess
import sys
import time
from typing import Optional, Tuple
import uuid


class JoinState(Enum):
    EXITED = 'EXITED'
    TIMED_OUT = 'TIMED_OUT'
    STATUS_UNAVAILABLE = 'STATUS_UNAVAILABLE'
    ERROR = 'ERROR'


class CleanupState(Enum):
    CLEAN = 'CLEAN'
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
class SignalResult:
    state: SignalState
    detail: str = ''


_MARKER = 'HANIG_FIXTURE_LAUNCH_MARKER'


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
                if not key or '=' in key or key == _MARKER or key in keys:
                    raise ValueError('invalid, duplicate or reserved environment key')
                keys.add(key)


class FixtureRefused(ValueError):
    pass


class _Indeterminate(RuntimeError):
    pass


@dataclass(frozen=True)
class _Identity:
    birth: tuple
    uid: int
    pgid: int
    sid: int
    zombie: bool


_DARWIN_LIB = None


def _identity(pid):
    """Kernel birth identity, without reaping; missing is distinct from error."""
    global _DARWIN_LIB
    try:
        if sys.platform == 'darwin':
            if _DARWIN_LIB is None:
                _DARWIN_LIB = ctypes.CDLL(ctypes.util.find_library('proc'), use_errno=True)
            # Darwin proc_bsdinfo, PROC_PIDTBSDINFO=3 (bsd/sys/proc_info.h).
            data = ctypes.create_string_buffer(136)
            ctypes.set_errno(0)
            count = _DARWIN_LIB.proc_pidinfo(pid, 3, 0, data, len(data))
            if count != len(data):
                number = ctypes.get_errno()
                if number == errno.ESRCH:
                    return None
                # Darwin may refuse proc info during exit. Confirm disappearance
                # independently; a still-present unreadable process is an error.
                os.kill(pid, 0)
                raise OSError(number, 'cannot read process birth identity for PID %s' % pid)
            fields = struct.unpack('=12I48s6I2Q', data.raw)
            if fields[3] != pid:
                raise _Indeterminate('process identity changed during inspection')
            return _Identity(fields[-2:], fields[5], fields[14], os.getsid(pid), fields[1] == 5)
        if sys.platform.startswith('linux'):
            path = Path('/proc') / str(pid)
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            uid = (path / 'status').read_text().split('Uid:', 1)[1].split()[1]
            return _Identity((int(fields[19]),), int(uid), int(fields[2]),
                             int(fields[3]), fields[0] == 'Z')
        raise FixtureRefused('fixture absence inspection requires macOS or Linux')
    except (ProcessLookupError, FileNotFoundError):
        return None


def _environment(pid):
    """Read kernel launch environment as NUL-delimited fields; never log it."""
    if sys.platform.startswith('linux'):
        return (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0')
    if sys.platform != 'darwin':
        raise FixtureRefused('unsupported marker inspection platform')
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    # KERN_PROCARGS2 includes argc, executable path, padding, argv, then env.
    mib = (ctypes.c_int * 3)(1, 49, pid)
    size = ctypes.c_size_t(os.sysconf('SC_ARG_MAX'))
    data = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0) != 0:
        number = ctypes.get_errno()
        raise OSError(number, 'cannot inspect fixture launch marker')
    raw = data.raw[:size.value]
    if len(raw) < 4:
        raise _Indeterminate('incomplete marker inspection')
    argc = struct.unpack('=i', raw[:4])[0]
    if argc < 1:
        raise _Indeterminate('missing launch argument boundary')
    end = raw.find(b'\0', 4)
    if end < 0:
        raise _Indeterminate('missing executable boundary')
    pos = end + 1
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    for _ in range(argc):
        end = raw.find(b'\0', pos)
        if end < 0:
            raise _Indeterminate('incomplete launch arguments')
        pos = end + 1
    return raw[pos:].split(b'\0')


def _cwd_marker(pid, identity, root_identity):
    """Inspect the inherited directory marker, including restricted macOS execs.

    Compare filesystem identities up the observed cwd, not normalized strings.
    A removed/unreadable cwd or a PID change is indeterminate, never absence.
    """
    try:
        if sys.platform == 'darwin':
            # PROC_PIDVNODEPATHINFO=9: two 1176-byte vnode_info_path records;
            # each has 152 bytes of vnode_info followed by MAXPATHLEN bytes.
            data = ctypes.create_string_buffer(2352)
            ctypes.set_errno(0)
            count = _DARWIN_LIB.proc_pidinfo(pid, 9, 0, data, len(data))
            if count != len(data):
                raise OSError(ctypes.get_errno(), 'cannot inspect inherited cwd marker')
            raw = data.raw[152:1176]
            if b'\0' not in raw or not raw.split(b'\0', 1)[0]:
                raise _Indeterminate('incomplete cwd marker inspection')
            path = Path(os.fsdecode(raw.split(b'\0', 1)[0]))
        else:
            path = Path(os.readlink('/proc/%d/cwd' % pid))
        after = _identity(pid)
        if after is None:
            return False
        if after.birth != identity.birth:
            raise _Indeterminate('PID reused during cwd marker inspection')
        for candidate in (path,) + tuple(path.parents):
            observed = candidate.stat()
            if (observed.st_dev, observed.st_ino) == root_identity:
                return True
        return False
    except OSError:
        after = _identity(pid)
        if after is None or (after.birth == identity.birth and after.zombie):
            return False
        raise


def _has_marker(pid, identity, marker):
    try:
        fields = _environment(pid)
    except OSError:
        after = _identity(pid)
        if after is None or (after.birth == identity.birth and after.zombie):
            return False
        raise
    after = _identity(pid)
    if after is None:
        return False
    if after.birth != identity.birth:
        raise _Indeterminate('PID reused during marker inspection')
    return marker in fields


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
    output = subprocess.check_output(
        ['/bin/ps', '-ww', '-U', str(os.getuid()), '-o',
         'pid=,ppid=,pgid=,uid=,stat=,command='],
        text=True, timeout=60)
    rows = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) != 6:
            raise _Indeterminate('malformed process enumeration')
        pid, ppid, pgid, uid = map(int, parts[:4])
        if uid == os.geteuid():
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
    time.sleep(600)
'''


class FixtureProcess:
    """Typed status and independently checked cleanup for one fixture launch.

    join(timeout) returns TIMED_OUT without changing terminal status. terminate
    asks the sole reaper to signal the direct child (TERM_GROUP is the fixed
    child group). It never signals a saved child PID from the caller. cleanup
    kills remaining marked/session descendants, waits for their disappearance,
    then kills and reaps its retained supervisor. Failed cleanup is sticky.
    """
    def __init__(self, scope, spec, marker, supervisor, report, control, stem):
        self.scope, self.spec = scope, spec
        self.marker = (_MARKER + '=' + marker).encode()
        self._supervisor = supervisor
        self.supervisor_pid = supervisor.pid
        self._report, self._control = report, control
        self.stdout_path = stem.with_suffix('.stdout')
        self.stderr_path = stem.with_suffix('.stderr')
        self._buffer = b''
        self._joined = None
        self._cleaned = None
        self._signals = {}
        self._known = {}
        self._groups = set()
        self._anchor = None
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
                          self.stdout_path.read_text(errors='replace'),
                          self.stderr_path.read_text(errors='replace'), detail)

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
        prior = self._known.get(pid)
        if prior is not None and prior.birth != identity.birth:
            raise _Indeterminate('recorded fixture PID was reused')
        self._known[pid] = identity
        if identity.pgid not in self._groups:
            self._groups.add(identity.pgid)
            # Persist the actual newly discovered group as well as memory.
            with self.scope.pidfiles[0].open('a') as record:
                record.write('%s\n' % identity.pgid)

    def _snapshot(self):
        """Independent whole-host enumeration plus exact inherited marker.

        The retained leader reserves the SID until the final reap. Numeric
        ledger entries alone never authorize a signal after their anchor dies.
        Processes predating this launch cannot descend from it. Identity/marker
        errors for any eligible same-uid process are unknown, never absence.
        """
        targets, inspected = {}, set()
        table = process_table()
        anchor = _identity(self.supervisor_pid)
        if anchor is not None:
            if anchor.birth != self._anchor.birth:
                raise _Indeterminate('supervisor identity changed')
            if self.supervisor_pid not in table:
                raise _Indeterminate('process enumeration omitted the supervisor')
        for pid in table:
            identity = _identity(pid)
            if identity is None:
                continue
            if identity.uid != self._anchor.uid or identity.birth < self._anchor.birth:
                continue
            inspected.add((pid, identity.birth))
            known = self._known.get(pid)
            if known is not None and known.birth != identity.birth:
                raise _Indeterminate('known descendant identity changed')
            in_session = anchor is not None and identity.sid == self.supervisor_pid
            marked = False
            if not identity.zombie:
                marked = _has_marker(pid, identity, self.marker)
                if not marked:
                    marked = _cwd_marker(pid, identity, self.scope._root_identity)
            if marked or in_session or known is not None:
                self._record(pid, identity)
                targets[pid] = identity
        return targets, inspected

    def _survivor_scan(self):
        deadline = time.monotonic() + 5
        _first, before = self._snapshot()
        while True:
            started = time.monotonic()
            second, after = self._snapshot()
            if second or not (after - before):
                return second, not (after - before)
            if started >= deadline:
                return second, False
            before = after


    def _signal_owned(self, pid, identity, signum):
        current = _identity(pid)
        if current is None:
            return
        if current.birth != identity.birth:
            raise _Indeterminate('PID changed before fixture signal')
        if current.zombie:
            return
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            # ESRCH positively reports that this target no longer exists.
            return

    def _reap(self):
        self._supervisor.wait(timeout=5)

    def _perform_cleanup(self):
        try:
            deadline = time.monotonic() + 8
            while True:
                observed_after = time.monotonic()
                targets, stable = self._survivor_scan()
                descendants = {p: row for p, row in targets.items()
                               if p != self.supervisor_pid}
                if not descendants and stable:
                    break
                for pid, identity in descendants.items():
                    self._signal_owned(pid, identity, signal.SIGKILL)
                if observed_after >= deadline:
                    return CleanupResult(CleanupState.INDETERMINATE,
                                         'descendants or a moving process population remain')
                time.sleep(0.05)
            # Keep child status if it was already delivered. Never invent one
            # when an external kill or cleanup ends reporting first.
            self.join(0)
            self._signal_owned(self.supervisor_pid, self._anchor, signal.SIGKILL)
            self._reap()
            # Recheck independently after reap; cached ledger hints are not
            # proof, and externally killed supervisors have no live SID anchor.
            targets, stable = self._survivor_scan()
            if targets or not stable:
                return CleanupResult(CleanupState.INDETERMINATE,
                                     'absence not established after supervisor reap')
            self.join(0)
            if self._joined is None:
                self._joined = self._output(JoinState.STATUS_UNAVAILABLE,
                                           detail='cleanup closed supervisor transport')
            for name in ('_report', '_control'):
                fd = getattr(self, name)
                if fd is not None:
                    os.close(fd)
                    setattr(self, name, None)
            return CleanupResult(CleanupState.CLEAN)
        except _Indeterminate as error:
            return CleanupResult(CleanupState.INDETERMINATE, str(error))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return CleanupResult(CleanupState.ERROR, str(error))

    def cleanup(self):
        if self._cleaned is None:
            self._cleaned = self._perform_cleanup()
        return self._cleaned


class FixtureProcesses:
    """Temporary fixture scope; unittest consumes typed cleanup failures.

    Directory deletion is registered by the caller before this scope. The
    independent guard runs after cleanup and before deletion, and reports
    omitted cleanup through unittest even if it can subsequently contain it.
    """
    def __init__(self, case, root):
        self.root = Path(root).absolute()
        root_stat = self.root.stat()
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
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
        directory = self.root if spec.directory is None else Path(spec.directory)
        if not any((p.stat().st_dev, p.stat().st_ino) == self._root_identity
                   for p in (directory,) + tuple(directory.parents)):
            raise FixtureRefused('fixture directory must retain the scope cwd marker')
        if sys.platform not in ('darwin', 'linux') or os.geteuid() == 0:
            raise FixtureRefused('requires unprivileged macOS/Linux fixture inspection')
        # Refuse missing inspection capability before creating the supervisor.
        if _identity(os.getpid()) is None:
            raise FixtureRefused('cannot inspect current process identity')
        _environment(os.getpid())
        marker = uuid.uuid4().hex
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
        env[_MARKER] = marker
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
            proc = FixtureProcess(self, spec, marker, supervisor,
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
            proc._anchor = _identity(supervisor.pid)
            if (proc._anchor is None or proc._anchor.sid != supervisor.pid or
                    not _has_marker(supervisor.pid, proc._anchor, proc.marker)):
                raise FixtureRefused('private-session launch marker not inspectable')
            proc._record(supervisor.pid, proc._anchor)
            os.write(proc._control, b'G')
            started = proc._read(60)
            if not started or set(started) != {'pid'} or type(started['pid']) is not int:
                raise FixtureRefused('fixture launch failed: %r' % started)
            proc.child_pid = started['pid']
            # The supervisor fixes the child's group before exec. The child
            # may exit before this observation; the retained SID still anchors
            # any same-session descendants and the marker covers setsid escapes.
            child = _identity(proc.child_pid)
            if child is not None:
                proc._record(proc.child_pid, child)
            return proc
        except BaseException:
            if proc._anchor is not None:
                proc.cleanup()
            else:
                supervisor.kill()
                supervisor.wait(timeout=5)
                for fd in (report_read, control_write):
                    os.close(fd)
                self.children.remove(proc)
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
                if proc._cleaned is None:
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
