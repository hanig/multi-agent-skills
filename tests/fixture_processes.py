"""Test-owned process cleanup; never evidence about a supervisor's behavior.

Register before launch and retain PID files until unittest's cleanups finish.
Only direct children are waitable here. Descendants are killed and observed
non-running; their actual parent (or the OS reaper) collects their exit status.
"""
import json
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import time
from unittest import mock


class _SupervisedFixture:
    """Child-facing handle; only FixtureProcesses may reap the supervisor."""
    def __init__(self, supervisor, args, report_fd, control_fd):
        self.supervisor = supervisor
        self.args = args
        self.report_fd = report_fd
        self.control_fd = control_fd
        self.buffer = b''
        self.returncode = None
        started = self._read(60)
        if 'error' in started:
            raise OSError(*started['error'])
        self.pid = started['pid']

    def __getattr__(self, name):
        if name in ('stdin', 'stdout', 'stderr'):
            return getattr(self.supervisor, name)
        raise AttributeError(name)

    def _read(self, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        while b'\n' not in self.buffer:
            left = None if deadline is None else max(0, deadline - time.monotonic())
            ready, _, _ = select.select([self.report_fd], [], [], left)
            if not ready:
                raise subprocess.TimeoutExpired(self.args, timeout)
            chunk = os.read(self.report_fd, 4096)
            if not chunk:
                raise AssertionError('fixture supervisor lost child status')
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._read(timeout)['returncode']
        return self.returncode

    def poll(self):
        try:
            return self.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return None

    def communicate(self, input=None, timeout=None):
        # Reuse Popen's pipe buffering/timeout behavior, but wait for the
        # reported child status rather than reaping our still-live anchor.
        with mock.patch.object(self.supervisor, 'wait', self.wait), mock.patch.object(
                self.supervisor, 'poll', self.poll):
            return self.supervisor.communicate(input=input, timeout=timeout)

    def send_signal(self, signum):
        if self.poll() is None:
            # The sole reaper sends the signal using the native child handle.
            # Sending to a child PID here would race its supervisor's wait.
            os.write(self.control_fd, ('%d\n' % signum).encode('ascii'))

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, value, traceback):
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                stream.close()
        self.wait()


def process_table():
    """Read untruncated commands on both macOS and Linux."""
    output = subprocess.check_output(
        ['ps', '-axww', '-o', 'pid=,ppid=,pgid=,stat=,command='],
        text=True, timeout=60)
    rows = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) == 5:
            pid, ppid, pgid = map(int, parts[:3])
            rows[pid] = (ppid, pgid, parts[3], parts[4])
    return rows


class FixtureProcesses:
    """One temporary root, explicit process records, and a post-cleanup guard.

    PID files identify candidate groups, never ownership by themselves. A
    reaped child or detached descendant needs a fresh root-bearing command
    to anchor its group. Unreaped direct children reserve their PIDs even
    after exec removes the root. Fixtures must retain a root-bearing argv
    when detaching or outliving a reaped parent. This uses POSIX snapshots,
    not atomic kernel process handles or arbitrary-process containment.
    """
    def __init__(self, case, root, kill_group):
        self.root = Path(root)
        self.kill_group = kill_group
        self.children = []
        self._channels = []
        self.pidfiles = [self.root / 'fixture-groups.pid']
        self._sleep_wrapper = None
        # LIFO: kill/reap first, inspect second. The caller registers directory
        # deletion before constructing us, so PID records still exist here.
        case.addCleanup(self.assert_no_survivors)
        case.addCleanup(self.cleanup)

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

    def popen(self, args, **kwargs):
        """Capture every fixture tree in a recorded private session.

        A non-session child inherits a retained supervisor's private session.
        Waiting for that child never releases the enclosing group anchor.
        Explicit session leaders retain the native Popen interface and need
        root-bearing descendants if callers reap them before cleanup.
        """
        if not kwargs.get('start_new_session'):
            return self._supervised_popen(args, kwargs)
        return self._start_session(args, kwargs)

    def _start_session(self, args, kwargs):
        kwargs['start_new_session'] = True
        proc = subprocess.Popen(args, **kwargs)
        self.children.append((proc, proc.pid))
        with self.pidfiles[0].open('a') as record:
            record.write('%s\n' % proc.pid)
        return proc

    def _supervised_popen(self, args, kwargs):
        if kwargs.get('preexec_fn') is not None or kwargs.get('close_fds') is False:
            raise ValueError('use run_python for preexec_fn or close_fds=False fixtures')
        child_options = {key: kwargs.pop(key) for key in
                         ('shell', 'executable', 'restore_signals') if key in kwargs}
        if child_options.get('executable') is not None:
            child_options['executable'] = os.fsdecode(child_options['executable'])
        child_options['args'] = (os.fsdecode(args) if isinstance(args, (str, bytes, os.PathLike))
                                 else [os.fsdecode(arg) for arg in args])
        child_options['pass_fds'] = list(kwargs.get('pass_fds', ()))
        report_read, report_write = os.pipe()
        control_read, control_write = os.pipe()
        self._channels.extend((report_read, control_write))
        try:
            script = self.root / ('fixture-launch-%d.py' % len(self.children))
            script.write_text(
                'import json, os, select, subprocess, time\n'
                'report, control = %d, %d\n'
                'os.set_inheritable(report, False)\n'
                'os.set_inheritable(control, False)\n'
                'def send(value):\n'
                '    os.write(report, (json.dumps(value) + "\\n").encode())\n'
                'try:\n'
                '    child = subprocess.Popen(**json.loads(%r))\n'
                'except OSError as error:\n'
                '    send({"error": [error.errno, error.strerror, error.filename]})\n'
                'else:\n'
                '    for fd in (0, 1, 2): os.close(fd)\n'
                '    send({"pid": child.pid})\n'
                '    pending = b""\n'
                '    while child.poll() is None:\n'
                '        ready, _, _ = select.select([control], [], [], 0.01)\n'
                '        if ready:\n'
                '            chunk = os.read(control, 4096)\n'
                '            if not chunk: raise RuntimeError("fixture control lost")\n'
                '            pending += chunk\n'
                '            while b"\\n" in pending:\n'
                '                line, pending = pending.split(b"\\n", 1)\n'
                '                child.send_signal(int(line))\n'
                '    send({"returncode": child.returncode})\n'
                'while True: time.sleep(600)\n' % (
                    report_write, control_read, json.dumps(child_options)))
            kwargs['pass_fds'] = tuple(child_options['pass_fds']) + (report_write, control_read)
            proc = self._start_session([sys.executable, '-I', str(script)], kwargs)
        finally:
            os.close(report_write)
            os.close(control_read)
        return _SupervisedFixture(proc, args, report_read, control_write)

    def run_python(self, source, timeout=60):
        """Return JSON-valued `result` while retaining the session supervisor.

        The test code runs in a fresh interpreter. Its Popen calls keep their
        real session semantics, and may reap their children without releasing
        our group anchor. The supervisor stays alive until fixture cleanup.
        """
        stem = 'fixture-supervisor-%d' % len(self.children)
        script = self.root / (stem + '.py')
        result_file = self.root / (stem + '.json')
        read_fd, write_fd = os.pipe()
        try:
            script.write_text(
                'import json, os, time, traceback\n'
                'try:\n'
                '    namespace = {}\n'
                '    exec(compile(%r, %r, "exec"), namespace)\n'
                '    answer = {"result": namespace["result"]}\n'
                'except BaseException:\n'
                '    answer = {"error": traceback.format_exc()}\n'
                'with open(%r, "w") as stream: json.dump(answer, stream)\n'
                'os.write(%d, b"R")\n'
                'os.close(%d)\n'
                'while True: time.sleep(600)\n' % (
                    source, str(script), str(result_file), write_fd, write_fd))
            self.popen([sys.executable, str(script)],
                       stdin=subprocess.DEVNULL, pass_fds=(write_fd,),
                       start_new_session=True)
            os.close(write_fd)
            write_fd = None
            ready, _, _ = select.select([read_fd], [], [], timeout)
            if not ready:
                raise subprocess.TimeoutExpired(str(script), timeout)
            if os.read(read_fd, 1) != b'R':
                raise AssertionError('fixture supervisor exited before reporting')
            answer = json.loads(result_file.read_text())
            if 'error' in answer:
                raise AssertionError('fixture supervisor failed:\n' + answer['error'])
            return answer['result']
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def _root_rows(self, table):
        roots = {str(self.root), os.path.realpath(str(self.root))}
        return {pid: row for pid, row in table.items()
                if pid != os.getpid() and not row[2].startswith('Z')
                and any(root + '/' in row[3] or row[3].endswith(root)
                        for root in roots)}

    def _targets(self, table):
        recorded = set()
        for path in self.pidfiles:
            if path.exists():
                recorded.update(int(pid) for pid in path.read_text().split())
        targets = self._root_rows(table)
        groups = {row[1] for pid, row in targets.items()
                  if pid in recorded or row[1] in recorded}
        for proc, pgid in self.children:
            # Do not poll: an unreaped direct child reserves its PID. A saved
            # PGID after wait() is only a hint, requiring a fresh root anchor.
            if proc.returncode is None and proc.pid in table:
                targets[proc.pid] = table[proc.pid]
                if pgid is not None:
                    groups.add(pgid)
            elif pgid is not None and any(row[1] == pgid for row in targets.values()):
                groups.add(pgid)
        groups.discard(os.getpgrp())
        targets.update((pid, row) for pid, row in table.items() if row[1] in groups)
        # Include ordinary descendants while an owned ancestor is observable.
        while True:
            added = {pid: row for pid, row in table.items()
                     if row[0] in targets and pid not in targets}
            if not added:
                return targets, groups
            targets.update(added)

    def _kill(self, targets, groups):
        for pgid in groups:
            self.kill_group(pgid)
        # Do not signal members twice: group KILL may already have freed their
        # PIDs. Positive signals cover children outside the anchored groups.
        for pid, row in targets.items():
            if pid != os.getpid() and row[1] not in groups and not row[2].startswith('Z'):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def _reap(self):
        failure = None
        for proc, _pgid in self.children:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                failure = error
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
        if failure is not None:
            raise failure
        while self._channels:
            os.close(self._channels.pop())

    def cleanup(self):
        self._kill(*self._targets(process_table()))
        deadline = time.monotonic() + 5
        while True:
            # Re-establish ownership, rather than treating reused numeric PIDs
            # from the pre-KILL snapshot as surviving fixtures.
            observed_after = time.monotonic()
            targets, _groups = self._targets(process_table())
            alive = {pid: row for pid, row in targets.items()
                     if not row[2].startswith('Z')}
            if not alive:
                # Reserve every unreaped leader's PID through the termination
                # check. Reaping first can erase the last root-free group's
                # ownership anchor and turn a failed KILL into a clean guard.
                self._reap()
                return
            # A slow read may finish after the deadline with older rows.
            # Require a snapshot started after the grace period before failing.
            if observed_after >= deadline:
                raise AssertionError('fixture PIDs survived KILL: %r' % alive)
            time.sleep(0.05)

    def assert_no_survivors(self):
        targets, _groups = self._targets(process_table())
        survivors = {pid: row for pid, row in targets.items()
                     if not row[2].startswith('Z')}
        if survivors:
            # Preserve the observation before emergency cleanup, then fail.
            try:
                self.cleanup()
            finally:
                raise AssertionError('fixture processes survived cleanup: %r' % survivors)
