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
import worktree as W

STATE_FILE = "swarm-state.json"
STATE_EPOCH_FILE = "state-epoch.json"
LEASE = "lease.json"

# States in which a unit is occupying a slot on the cluster right now.
LIVE_STATES = ("ALLOCATED", "SUBMITTED", "RUNNING")
OPERATOR_TERMINAL_STATES = ("ABANDONED", "SUPERSEDED")


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


def acquire_lease(state_dir, bump_epoch=True):
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
        _LOCK_EPOCHS[key] = (os.getpid(), epoch)
        if bump_epoch:
            bump_lease_epoch(state_dir)
    except BaseException:
        release_lease(state_dir)
        raise
    # Descriptive only. Nothing decides anything from this file; it exists so a
    # human blocked by the lock can see who has it.
    if not bump_epoch:
        return True, None  # operator no-op/refusal leaves durable bytes intact
    U.write_json(Path(state_dir) / LEASE,
                 {"owner": os.environ.get("USER", "?"),
                  "host": os.uname().nodename, "pid": os.getpid(),
                  "acquired_at": time.time()})
    return True, None


def bump_lease_epoch(state_dir):
    """Fence a new mutation after an operator has checked under the lock.

    Reload state after this call: a snapshot loaded before it remains stale.
    The default acquisition still bumps immediately for all existing callers.
    """
    key = str(Path(state_dir).resolve())
    epoch, err = _read_state_epoch(state_dir)
    if (err or renew_lease(state_dir) is not True
            or _LOCK_EPOCHS.get(key) != (os.getpid(), epoch)):
        sys.exit("HALTED: cannot bump coordinator epoch without the current lease")
    err = U.write_json(Path(state_dir) / STATE_EPOCH_FILE, {"epoch": epoch + 1})
    if err:
        sys.exit(f"HALTED: cannot persist coordinator state epoch: {err}")
    _LOCK_EPOCHS[key] = (os.getpid(), epoch + 1)


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


def holds_lease(state_dir):
    """Read-only live process/descriptor/path capability; never acquire a lock."""
    key = str(Path(state_dir).resolve())
    return (_LOCK_OWNER_PID == os.getpid() and key in _LOCK_FDS
            and _holds_the_path(_LOCK_FDS[key], Path(state_dir)))


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


def save_state(state_dir, state):
    key = str(Path(state_dir).resolve())
    epoch, err = _read_state_epoch(state_dir)
    # A lease holder always uses its process pin, even after reloading state.
    # Legacy direct helpers (including promotion) do not acquire a lease:
    # their first save pins the observed epoch without bumping it or granting
    # custody. Loaded snapshots also carry their own earlier observation.
    owner, expected = _LOCK_EPOCHS.get(key, (os.getpid(), epoch))
    if err or owner != os.getpid() or epoch != expected:
        # Do not persist the halt into the successor's state.
        reason = err or "state epoch changed or process pin was inherited"
        sys.exit("HALTED: another coordinator has written state under us "
                 f"({reason}); refusing to save")
    # Re-acquisition moves the process pin, never the snapshot's load epoch.
    # Check before changing the snapshot, pinning a helper or touching disk.
    # Plain, caller-built dictionaries retain the legacy direct-helper API;
    # every dictionary returned by load_state is a tagged snapshot.
    if (isinstance(state, _StateSnapshot)
            and getattr(state, "_snapshot_epoch", None)
            != (key, owner, expected)):
        sys.exit("HALTED: another coordinator has written state under us "
                 "(snapshot belongs to a different lease epoch or state "
                 "directory); reload state before saving")
    _LOCK_EPOCHS.setdefault(key, (owner, expected))
    # Retire the old inline field on a normal save, never on acquisition.
    state.pop("epoch", None)
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    err = U.write_json(Path(state_dir) / STATE_FILE, state)
    if err:
        sys.exit(f"error: cannot persist coordinator state: {err}. Refusing to "
                 f"continue: acting on state that is not on disk is how a "
                 f"crash becomes a double submission.")


def _unit_state(state, uid):
    return state["units"].setdefault(
        uid, {"state": None, "attempt_dir": None, "attempts": [],
              "gpu_hours": 0.0})


def _set_unit_state(us, value, changed_at=None):
    """Set a unit state and durably date a real transition.

    The timestamp is coordinator state, not worker evidence. Historical state
    has no trustworthy transition time, so advance marks it from first
    observation rather than inventing an earlier event.
    """
    previous = us.get("state")
    if previous != value:
        us["state_changed_at"] = (time.time() if changed_at is None
                                  else float(changed_at))
        us.pop("state_changed_at_basis", None)
        if previous == "NEEDS_HUMAN" and value != "NEEDS_HUMAN":
            us.pop("reason", None)
            us.pop("deadline_previous_reason", None)
    us["state"] = value


def _normalise_state_clock(us, observed_at):
    """Give historical state an honest clock from its first observation."""
    if us.get("state") is not None and us.get("state_changed_at") is None:
        us["state_changed_at"] = float(observed_at)
        us["state_changed_at_basis"] = "first_observed"


def _deadline_host_evaluation(state, uid, us):
    """Whether this coordinator may evaluate the attempt's local clock."""
    anchor = trusted_launch_host_anchor(state, uid, us.get("attempt_dir"))
    belongs = W.attempt_belongs_to_host(anchor)
    if belongs is True:
        return True, None
    if belongs is False:
        return False, W.launch_host_problem(anchor)
    return None, (
        "UNJUDGEABLE HERE: coordinator state records no launch_host for this "
        "attempt, so its allocated_at deadline is not evaluable on this "
        "host. No deadline escalation is emitted")


def _deadline_exceeded(u, us, state, uid, observed_at=None):
    """Whether this host-owned attempt crossed its allocation deadline."""
    deadline = u.get("deadline_s")
    allocated = us.get("allocated_at")
    if deadline is None or allocated is None or not us.get("attempt_dir"):
        return False
    belongs, _problem = _deadline_host_evaluation(state, uid, us)
    if belongs is not True:
        return False
    terminal = {"DONE", "FAILED", "FAILED_EVIDENCE", "READY_FOR_PR",
                "PREFLIGHT_REFUSED", "HELD"}
    if us.get("state") in terminal or us.get("state") in OPERATOR_TERMINAL_STATES:
        return False
    now = time.time() if observed_at is None else float(observed_at)
    return now - float(allocated) >= float(deadline)


def _mark_deadline_exceeded(u, us, observed_at=None):
    """Persist the conservative escalation; return True on first breach."""
    now = time.time() if observed_at is None else float(observed_at)
    active = (us.get("deadline_breach_active") is True
              or us.get("reason") == "deadline_exceeded")
    first_breach = not active
    sequence = us.get("deadline_breach_seq")
    if type(sequence) is not int or sequence < 1:
        sequence = 0
    if first_breach:
        sequence += 1
    elif sequence == 0:
        # State written by the first deadline implementation has no sequence.
        # Adopt its active breach as event one and retain that implementation's
        # key until the breach ends rather than emitting a duplicate.
        sequence = 1
        us["deadline_breach_legacy_key"] = True
    prior_reason = us.get("reason")
    if first_breach and prior_reason:
        us["deadline_previous_reason"] = prior_reason
    us["deadline_breach_active"] = True
    us["deadline_breach_seq"] = sequence
    us["reason"] = "deadline_exceeded"
    us["deadline_s"] = float(u["deadline_s"])
    us["deadline_at"] = float(us["allocated_at"]) + float(u["deadline_s"])
    # Marking the breach includes the operator-visible state transition. Every
    # caller, including the post-check path, persists NEEDS_HUMAN through here.
    _set_unit_state(us, "NEEDS_HUMAN", changed_at=now)
    return first_breach


def _clear_deadline_breach(us):
    """Clear one ended breach while retaining its monotonic event counter."""
    was_active = (us.pop("deadline_breach_active", None) is True
                  or us.get("reason") == "deadline_exceeded")
    if not was_active:
        return False
    if us.get("reason") == "deadline_exceeded":
        us.pop("reason", None)
        prior_reason = us.pop("deadline_previous_reason", None)
        if prior_reason:
            us["reason"] = prior_reason
    else:
        us.pop("deadline_previous_reason", None)
    us.pop("deadline_s", None)
    us.pop("deadline_at", None)
    us.pop("deadline_breach_legacy_key", None)
    return True


def _deadline_attempt_is_unresolved(u, us):
    """Whether a declared deadline still protects this attempt's resources."""
    terminal = {"DONE", "FAILED", "FAILED_EVIDENCE", "READY_FOR_PR",
                "PREFLIGHT_REFUSED", "HELD"}
    return (u.get("deadline_s") is not None
            and bool(us.get("attempt_dir"))
            and us.get("state") not in terminal
            and us.get("state") not in OPERATOR_TERMINAL_STATES)


def _occupies_live_resources(u, us):
    """Whether an attempt may still execute and must retain its claim/slot.

    Deadline protection depends on the declared attempt, never on whether a
    later phase has already rewritten the operator-facing state/reason.
    """
    if us.get("state") in OPERATOR_TERMINAL_STATES:
        # A tracker disposition does not establish process termination.
        release = us.get("terminal_resource_release") or {}
        released = (bool(release)
                    and release.get("attempt_dir") == us.get("attempt_dir")
                    and release.get("job_id") == us.get("job_id"))
        return (bool((us.get("terminal_decision") or {}).get("retains_live_resources"))
                and not released)
    return (us.get("state") in LIVE_STATES
            or _deadline_attempt_is_unresolved(u, us))


def trusted_launch_facts(state, unit, attempt_dir):
    """Complete per-attempt launch authority, from coordinator state only."""
    if not attempt_dir:
        return None
    attempt = Path(attempt_dir).name
    us = (state.get("units") or {}).get(unit) or {}
    facts = (us.get("attempt_launch_facts") or {}).get(attempt)
    if W.launch_facts_problem(facts, attempt_dir,
                              {"id": unit, "task_id": unit}):
        return None
    return facts


def trusted_launch_host_anchor(state, unit, attempt_dir):
    """Coordinator-state host anchor, including an interrupted code launch.

    Completed launch facts are the normal authority. A code launch can crash
    after its pre-agent intent is durable but before Paseo's cwd completes the
    facts; that intent already contains the coordinator-observed launch host.
    Returning neither is unknown, never permission to act on host-local state.
    """
    facts = trusted_launch_facts(state, unit, attempt_dir)
    if facts is not None:
        return facts
    if not attempt_dir:
        return None
    attempt = Path(attempt_dir).name
    us = (state.get("units") or {}).get(unit) or {}
    intent = (us.get("attempt_launch_intents") or {}).get(attempt)
    return intent if isinstance(intent, dict) else None
