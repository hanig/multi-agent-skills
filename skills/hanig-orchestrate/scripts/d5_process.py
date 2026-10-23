"""Inert U3-R8 subprocess roles; no production callers or activation change.

Supported envelope and accepted limits
-------------------------------------
S: managed subprocesses, trusted local execution and stable record/lock inodes.
P/H as applicable; R1 may repair lock owner permissions in place to 0600.
Worker role means no inheritance of orchestrator protection; it does not mean
the worker is cancelled or harmless. No handling of raw fork or descendants
launched outside these role-specific helpers.

R8 does not define a designated bootstrap's argument protocol or adapter
construction; each designated bootstrap, supplied by U4–U7, owns them. The
production allowlist is empty until then. No Boundary, Attempt, Receipt or
completion capability is transferred by this module.
"""
import os
import subprocess
import sys

from d5_authority import AuthorityError
import d5_locks as locks
import d5_protocol_locks as protocol


DESIGNATED_BOOTSTRAPS = frozenset()


def _communication(record, pass_fds, streams):
    """Reject protection files masquerading as communication descriptors.

    Compare identities, including dup aliases; never open or acquire STATE.
    Stable inodes and caller-owned descriptors through spawn are prerequisites.
    """
    protected = set()
    for name in locks._NAMES.values():
        try:
            info = os.stat(name, dir_fd=record.fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        protected.add((info.st_dev, info.st_ino))
    descriptors = list(pass_fds)
    for standard, stream in enumerate(streams):
        fd = standard if stream is None else stream
        fd = fd if isinstance(fd, int) else fd.fileno()
        if fd >= 0:
            descriptors.append(fd)
    for fd in descriptors:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in protected:
            raise AuthorityError("D5 communication descriptor is a protocol lock")


def protected_spawn(directory, inherited_fd, bootstrap, args=(), *, pass_fds=(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE):
    """Launch a designated entry with a caller-owned, held shared DRAIN fd.

    Call outside STATE. Refuse this thread's ambient START via R6's ownership
    check. R1 imports a temporary duplicate and closes it on success/failure;
    the original number is passed unchanged, and remains owned by the caller.
    Closing either parent reference must never unlock the shared description.
    The designated child must adopt through R7 before effect admission.
    DRAIN must use a descriptor above 2: stdio setup owns slots 0, 1 and 2.
    For a source in those slots, duplicate it above 2 before constructing args.

    args are opaque argv data. The caller names necessary communication fds
    separately; they cannot name protocol locks. The returned Popen and its
    default binary pipes are caller-owned: drain outputs and reap the child.
    """
    if bootstrap not in DESIGNATED_BOOTSTRAPS:
        raise AuthorityError("D5 protected bootstrap is not designated")
    command = [sys.executable]
    command.extend([bootstrap] if os.path.isabs(bootstrap) else ["-m", bootstrap])
    command.extend(args)
    pass_fds = tuple(pass_fds)
    with protocol.pin(directory) as pinned:
        if pinned._record._prior(1) is not None:
            raise AuthorityError("D5 protected launch inside START is forbidden")
        if inherited_fd in (0, 1, 2):
            raise AuthorityError("D5 protected DRAIN uses a standard descriptor")
        _communication(pinned._record, pass_fds, (stdin, stdout, stderr))
        with pinned.drain(inherited_fd=inherited_fd):
            return subprocess.Popen(command, close_fds=True,
                pass_fds=(inherited_fd,) + pass_fds, stdin=stdin,
                stdout=stdout, stderr=stderr, bufsize=0)


def worker_spawn(argv):
    """Launch a worker with no inherited protection or completion capability.

    Fresh binary pipes replace standard streams, and the descriptor allowlist
    is empty. The caller owns the returned Popen, drains its output and reaps it.
    This helper supplies neither admission nor cancellation.
    """
    return subprocess.Popen(argv, close_fds=True, pass_fds=(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0)
