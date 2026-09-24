"""Test-owned process cleanup; never evidence about a supervisor's behavior.

Register before launch and retain PID files until unittest's cleanups finish.
Only direct children are waitable here. Descendants are killed and observed
non-running; their actual parent (or the OS reaper) collects their exit status.
"""
import contextlib
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
from unittest import mock


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

    def popen(self, *args, **kwargs):
        proc = subprocess.Popen(*args, **kwargs)
        self.children.append((proc, proc.pid if kwargs.get('start_new_session') else None))
        return proc

    @contextlib.contextmanager
    def capture_popen(self):
        """Keep handles for in-process product calls without changing launch."""
        original = subprocess.Popen

        def launch(*args, **kwargs):
            proc = original(*args, **kwargs)
            self.children.append((proc, proc.pid if kwargs.get('start_new_session') else None))
            return proc

        with mock.patch.object(subprocess, 'Popen', launch):
            yield

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

    def cleanup(self):
        self._kill(*self._targets(process_table()))
        self._reap()
        deadline = time.monotonic() + 5
        while True:
            # Re-establish ownership, rather than treating reused numeric PIDs
            # from the pre-KILL snapshot as surviving fixtures.
            observed_after = time.monotonic()
            targets, _groups = self._targets(process_table())
            alive = {pid: row for pid, row in targets.items()
                     if not row[2].startswith('Z')}
            if not alive:
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
