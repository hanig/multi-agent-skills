"""Coordinator leases, durable snapshots and state transition clocks.

Python 3.8+, standard library only. No coordinator facade or network imports.
"""

import errno
import fcntl
import json
import os
import sys
import time
from pathlib import Path

import unit as U

STATE_FILE = "swarm-state.json"
STATE_EPOCH_FILE = "state-epoch.json"
LEASE = "lease.json"


# Locks this process holds, KEYED BY PROJECT. A single global fd made
# acquire_lease return True for any state directory once one had been
# acquired, so a second project in the same process was never actually
# locked. Caught by the new tests, which is what they are for.
_LOCK_FDS = {}
# Pinned on actual acquisition, not refreshed from a later state read. Keep
# the pin after release so a stale process cannot save over a successor.
# Legacy unleased helpers pin on their first save instead (see save_state).
_LOCK_EPOCHS = {}
# The pid that owns the entries above. flock is held per OPEN FILE
# DESCRIPTION, which a fork shares, so a forked child would inherit this dict
# and be told it already holds a lock it never took. Reviewers found that; the
# guard is cheaper than reasoning about who forks.
_LOCK_OWNER_PID = None
LOCK = "lease.lock"


def acquire_lease(state_dir):
    """One controller at a time, arbitrated by the OS. Returns (ok, holder).

    THE SIXTH VERSION, and the first that does not invent its own mutual
    exclusion. The five before it were hand-rolled from atomic file
    primitives, and two independent reviews found a CRITICAL in each of the
    last two: a deposed controller could overwrite its successor's lease, and
    the breaker that was meant to prevent that was itself reclaimable by mtime
    alone, so a holder merely PAUSED past the TTL had it stolen and then
    clobbered the successor on resume. One reviewer also showed the
    reclamation was unreachable from acquire_lease, so a killed holder wedged
    the project permanently.

    Every one of those defects lived in machinery that existed for a single
    reason: a plain file cannot tell you its owner died. An advisory lock can,
    because the kernel drops it when the process exits, however it exits. That
    removes the stale-lease TTL, the breaker directory, the ownership token,
    the mtime heuristic and the renewal loop, and with them the entire class
    of bug that five rewrites could not close.

    MEASURED, not assumed: 10 concurrent processes, three trials, on lambda
    (nfs), chimera (nfs4) and andromeda (weka) -- exactly one winner every
    time, then 8 concurrent real advances against one live DAG, three trials
    per cluster, one dispatcher every time.

    WHAT THAT MEASUREMENT DOES NOT SHOW, corrected after a reviewer pointed
    it out: every contender ran on ONE host. A mount whose locking has
    degraded to local-only still excludes same-host processes perfectly, so
    neither the manual runs nor the test suite can tell real cross-client
    locking from the local-only case. An earlier version of this docstring
    claimed the suite re-checks that property. It does not, and cannot from a
    single host.

    This is adequate rather than airtight, and it is adequate for a stated
    reason: a project runs entirely on ONE server, so same-host exclusion is
    the property that has to hold. Two controllers on two different nodes
    sharing the filesystem is outside the topology, and would need a
    cross-node test to certify.

    KNOWN LIMIT, not fixed: NFS lock recovery. If the server reboots, or
    evicts this client's lock state after a partition, the lock can be dropped
    while this process is still alive and another controller can then acquire
    it. The kernel does not tell us; the epoch fence can detect a successor's
    published acquisition on the next save, but is not an atomic compare-and-
    swap and cannot certify cross-node exclusion."""
    global _LOCK_OWNER_PID
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    key = str(Path(state_dir).resolve())
    if _LOCK_OWNER_PID is not None and _LOCK_OWNER_PID != os.getpid():
        # Inherited across a fork. Forgetting the fds is not enough: the child
        # holds real descriptors on the same open file description, so a
        # long-lived child would keep the project locked after the parent
        # exited and block honest work. Closing them here is safe -- the lock
        # lives on the OFD, and the parent's own descriptor still references
        # it -- and it is what actually releases the child's grip.
        for fd in list(_LOCK_FDS.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        _LOCK_FDS.clear()
        _LOCK_OWNER_PID = None
    if key in _LOCK_FDS and _holds_the_path(_LOCK_FDS[key], Path(state_dir)):
        return True, None                     # already ours, in this process
    path = Path(state_dir) / LOCK
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        return False, f"cannot open the lock at {path}: {e}"
    # CONTENTION IS ONE ERRNO. A bare `except OSError` here reported every
    # failure as "another controller holds it", including ENOLCK, which means
    # this filesystem cannot lock AT ALL. On such a mount every advance would
    # refuse forever, blaming a controller that does not exist -- a verifier
    # crying wolf, which is the failure this repo weights equally with a false
    # pass. EINTR is a signal, not a verdict, so it is retried.
    for _ in range(3):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            os.close(fd)
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                held, rerr = U.read_json(Path(state_dir) / LEASE)
                if not rerr and isinstance(held, dict):
                    age = int(time.time()
                              - float(held.get("acquired_at") or 0))
                    return False, (f"{held.get('owner')}@{held.get('host')} "
                                   f"pid {held.get('pid')}, {age}s ago")
                return False, "another controller holds it"
            return False, (
                f"this filesystem cannot lock {path} ({e.strerror}, errno "
                f"{e.errno}), so nothing can guarantee that only one "
                f"controller runs. This is NOT contention. Put the state "
                f"directory on a filesystem that supports advisory locking, "
                f"or run the coordinator somewhere that can reach one.")
    else:
        os.close(fd)
        return False, f"repeatedly interrupted while locking {path}"
    # The lock is on an INODE, not on a name. If the path was replaced
    # between our open and now, we are holding a lock on a file nobody else
    # will ever contend for, and a second controller can lock the new one.
    # Reviewers found this; it is cheap to detect and refuse.
    if not _holds_the_path(fd, Path(state_dir)):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        return False, (f"{path} was replaced while it was being locked, so "
                       f"this lock guards a file no other controller will "
                       f"contend for. Nothing dispatched. Re-run; if it "
                       f"repeats, something else is deleting the state "
                       f"directory underneath the coordinator.")
    _LOCK_FDS[key] = fd
    _LOCK_OWNER_PID = os.getpid()
    # Detection only: no heartbeat, TTL, expiry, mtime aging or timer. Nothing
    # reads this epoch to grant, steal or release custody; flock still decides
    # acquisition. The separate file keeps a no-op acquisition from rewriting
    # swarm-state.json. Concurrent reads/replaces and stale filesystem reads
    # remain outside this guarantee; this is not a distributed lock or a CAS.
    try:
        epoch, err = _read_state_epoch(state_dir)
        if err:
            sys.exit(f"HALTED: {err}")
        err = U.write_json(Path(state_dir) / STATE_EPOCH_FILE,
                           {"epoch": epoch + 1})
        if err:
            sys.exit(f"HALTED: cannot persist coordinator state epoch: {err}")
        _LOCK_EPOCHS[key] = (os.getpid(), epoch + 1)
    except BaseException:
        release_lease(state_dir)
        raise
    # Descriptive only. Nothing decides anything from this file; it exists so a
    # human blocked by the lock can see who has it.
    U.write_json(Path(state_dir) / LEASE,
                 {"owner": os.environ.get("USER", "?"),
                  "host": os.uname().nodename, "pid": os.getpid(),
                  "acquired_at": time.time()})
    return True, None


def _holds_the_path(fd, state_dir):
    """Is the fd we locked still the file at the lock's path?

    A flock is on an inode. If lease.lock is deleted or replaced, another
    process creates a NEW inode at that name and locks it successfully, and
    both controllers then believe they are alone. Comparing the fd's
    (device, inode) with the path's is the only way to notice.

    SCOPE, after a reviewer called the claim too strong: this is a check, and
    a check cannot be atomic with the work that follows it. Replacement in the
    instant after it returns is undetectable until the next call. What it
    genuinely buys is that a lock file deleted or replaced BETWEEN advances,
    which is the way this actually happens, is caught at the next renewal
    rather than never. It narrows the window; it does not close it. Nothing
    short of not deleting the lock file closes it."""
    try:
        a = os.fstat(fd)
        b = os.stat(str(Path(state_dir) / LOCK))
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def renew_lease(state_dir):
    """Returns True while this process still holds the lock.

    There is nothing to renew. The old lease expired on a clock because a file
    cannot notice its owner died, and that expiry is exactly what let a paused
    controller be deposed and then clobber its successor. An advisory lock is
    held until the process releases it or exits, so a slow advance keeps it by
    construction rather than by refreshing a timestamp."""
    key = str(Path(state_dir).resolve())
    if _LOCK_OWNER_PID != os.getpid():
        return False
    fd = _LOCK_FDS.get(key)
    if fd is None:
        return False
    # Not merely "we have an fd". Re-checking that the fd is still the file at
    # the path catches the replaced-inode case mid-advance, which is the one
    # way a live holder can silently stop being the only holder ON THIS HOST.
    return _holds_the_path(fd, Path(state_dir))


def release_lease(state_dir):
    """Release ours. The kernel does this anyway if we die, which is the whole
    point; doing it explicitly just frees the project sooner."""
    key = str(Path(state_dir).resolve())
    fd = _LOCK_FDS.pop(key, None)
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# --- durable state --------------------------------------------------------
class _StateSnapshot(dict):
    """Loaded state with an in-memory directory/process/epoch binding.

    Attributes are not JSON fields. Keep ordinary copies bound to the same
    load; neither a save nor a later acquisition may refresh this binding.
    This is a trusted-caller guard, not protection against deliberately
    rebuilding a snapshot as an untagged dict or editing private attributes.
    """

    def copy(self):
        snapshot = type(self)(self)
        snapshot._snapshot_epoch = self._snapshot_epoch
        return snapshot


def _read_state_epoch(state_dir):
    """Read only the fence file; absent legacy directories start at zero."""
    try:
        record = json.loads((Path(state_dir) / STATE_EPOCH_FILE).read_text())
    except FileNotFoundError:
        return 0, None
    except (OSError, ValueError) as exc:
        return None, f"unreadable coordinator state epoch: {exc}"
    if (not isinstance(record, dict) or type(record.get("epoch")) is not int
            or record["epoch"] < 0):
        return None, "invalid coordinator state epoch"
    return record["epoch"], None


def load_state(state_dir):
    """Load the durable side of the coordinator's lifecycle invariant.

    PERSIST BEFORE YOU ACT applies at both ends: record creation authority
    before starting an agent, and record conclusions plus cleanup charges
    before destroying their worktree. A crash may repeat observation, never
    creation or destruction whose governing state existed only in memory.

    Capture the binding BEFORE reading state. Unleased readers observe the
    fence without pinning the process or writing anything; an unreadable
    fence still allows status but cannot authorize a later save.
    """
    key = str(Path(state_dir).resolve())
    pin = _LOCK_EPOCHS.get(key)
    if pin is None:
        epoch, _error = _read_state_epoch(state_dir)
        pin = (os.getpid(), epoch)
    obj, err = U.read_json(Path(state_dir) / STATE_FILE)
    if err == "missing":
        obj = {"schema_version": 1, "units": {}, "halted": None}
    elif err:
        sys.exit(f"error: state at {Path(state_dir) / STATE_FILE} is unreadable "
                 f"({err}). Fix or remove it; removing it will re-dispatch "
                 f"units whose attempts are not recorded elsewhere.")
    if isinstance(obj, dict):
        obj = _StateSnapshot(obj)
        obj._snapshot_epoch = (key, *pin)
    return obj
