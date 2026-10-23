"""Inert U3-R8 subprocess roles; no production callers or activation change.

Supported envelope and accepted limits
-------------------------------------
S: managed subprocesses, trusted local execution and stable record/lock inodes.
P/H as applicable; R1 may repair lock owner permissions in place to 0600.
Worker role means no inheritance of orchestrator protection; it does not mean
the worker is cancelled or harmless. No handling of raw fork or descendants
launched outside these role-specific helpers.

Control descriptors, including DRAIN, must be numbered above 2; duplicate a
standard-slot source above 2 (for example with F_DUPFD_CLOEXEC, minimum 3) before
constructing the child's descriptor-bearing argv.
START refusal means the launching thread's own ownership. Another thread may
hold START during launch; the child's independent START acquisition blocks its
admission until that holder releases START. No protected work precedes admission.

R8 does not define a designated bootstrap's argument protocol or adapter
construction; each designated bootstrap, supplied by U4–U7, owns them. The
production allowlist is empty until then. No Boundary, Attempt, Receipt or
completion capability is transferred by this module.
"""
import errno
import os
import subprocess
import sys

from d5_authority import AuthorityError
import d5_locks as locks
import d5_protocol_locks as protocol


DESIGNATED_BOOTSTRAPS = frozenset()


def _communication(record, pass_fds, streams):
    """Check exports without opening STATE."""
    protected = set()
    for name in locks._NAMES.values():
        try:
            info = os.stat(name, dir_fd=record.fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        protected.add((info.st_dev, info.st_ino))
    descriptors = [(fd, False) for fd in pass_fds]
    for standard, stream in enumerate(streams):
        fd = standard if stream is None else stream
        if standard == 2 and stream == subprocess.STDOUT:
            fd = 1 if streams[1] is None else streams[1]
        fd = fd if isinstance(fd, int) else fd.fileno()
        if fd >= 0:
            descriptors.append((fd, stream is None))
    for fd, inherited_standard in descriptors:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            if inherited_standard and exc.errno == errno.EBADF:
                continue
            raise
        if inherited_standard and not os.get_inheritable(fd):
            continue
        if (info.st_dev, info.st_ino) in protected:
            raise AuthorityError("D5 communication descriptor is a protocol lock")


def protected_spawn(directory, inherited_fd, bootstrap, args=(), *, pass_fds=(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE):
    """Call outside STATE; caller owns shared DRAIN and controls above fd 2.

    R1 closes its duplicate on return/failure; R7 adopts the child copy. Caller
    drains/reaps Popen and closes its original without unlocking."""
    if bootstrap not in DESIGNATED_BOOTSTRAPS:
        raise AuthorityError("D5 protected bootstrap is not designated")
    command = [sys.executable]
    command.extend([bootstrap] if os.path.isabs(bootstrap) else ["-m", bootstrap])
    command.extend(args)
    pass_fds = tuple(pass_fds)
    with protocol.pin(directory) as pinned:
        if pinned._record._prior(1) is not None:
            raise AuthorityError("D5 protected launch inside START is forbidden")
        if any(fd <= 2 for fd in (inherited_fd,) + pass_fds):
            raise AuthorityError("D5 control descriptors must be above 2; duplicate before constructing child argv")
        _communication(pinned._record, pass_fds, (stdin, stdout, stderr))
        with pinned.drain(inherited_fd=inherited_fd):
            return subprocess.Popen(command, close_fds=True,
                pass_fds=(inherited_fd,) + pass_fds, stdin=stdin,
                stdout=stdout, stderr=stderr, bufsize=0)


def worker_spawn(argv):
    """Fresh pipes, no protection/cancellation. Caller drains and reaps."""
    return subprocess.Popen(argv, close_fds=True, pass_fds=(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0)


def worker_launch(argv, *, options=None):
    """Additive R8a async transport; legacy worker_spawn keeps its pipe contract."""
    from d5_process_launcher import launch
    return launch(argv, options=options)
