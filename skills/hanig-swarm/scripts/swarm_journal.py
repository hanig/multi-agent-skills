"""Durable JSONL journal primitives shared by coordinator consumers.

Python 3.8+, standard library only. No coordinator facade or network imports.
"""

import fcntl
import json
import os
from pathlib import Path

from swarm_types import OutboxError


def _heal_jsonl_tail(fh):
    """Under an exclusive lock, preserve complete JSON and drop only a tear."""
    fh.seek(0, os.SEEK_END)
    if not fh.tell():
        return
    fh.seek(-1, os.SEEK_END)
    if fh.read(1) == b"\n":
        return
    fh.seek(0)
    data = fh.read()
    cut = data.rfind(b"\n")
    tail = data[cut + 1:]
    try:
        json.loads(tail.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        fh.truncate(0 if cut < 0 else cut + 1)
    else:
        fh.seek(0, os.SEEK_END)
        fh.write(b"\n")


def _fsync_append(path, record):
    """Append one JSON line durably, serialised against other writers.

    Everything happens under ONE lock on ONE handle. Healing the tail used to
    run before the lock was taken, so two writers could interleave: A reads a
    partial tail, B truncates it and appends its receipt, then A truncates
    using its stale view and deletes B's record before appending its own. The
    repair for one crash silently ate a good receipt.

    The lock is advisory and process-scoped: the kernel drops it when this
    process exits, including when it is killed, so a crash cannot wedge the
    journal.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode()

    # r+b keeps existing content and allows truncate; a+b cannot truncate
    # portably. Create it first if absent.
    if not path.exists():
        path.touch()
    with open(path, "r+b") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise OutboxError(
                f"cannot take an exclusive lock on {path}: {exc}. Receipt "
                f"journal writes must be serialised, and this filesystem "
                f"will not "
                f"serialise them, so two drainers could interleave and "
                f"corrupt the journal. Record state on a filesystem "
                f"that supports flock.")

        # HEAL, now that nobody else can be writing. A crash can leave the
        # journal ending mid-record with no newline; appending onto that fuses
        # the new receipt to the broken one, turning a recoverable interrupted
        # write into corruption that takes the next record with it. An
        # incomplete record has no meaning, so dropping it loses nothing.
        _heal_jsonl_tail(fh)

        fh.seek(0, os.SEEK_END)
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


class _RawJournal:
    """What the raw reader returns: records you cannot reach by accident.

    Six review findings chased name-level bypasses of a static test (nested
    functions, lambdas, aliases, same-named methods). That arms race is not
    winnable: no static check over a shared module namespace can stop
    deliberate indirection, and a test claiming otherwise is the overclaim
    this repo keeps catching.

    So the barrier moved from the NAME to the DATA. The raw reader hands back
    this box. The records sit behind a private attribute that only
    `load_acknowledgments` opens, and it opens it only after refusing a bad
    journal. A caller who wants them without the check must reach into
    `_records` deliberately, in a line that says exactly what it is doing.

    The threat model is an honest maintainer in a hurry, not an adversary.
    Against a slip this is airtight; against smuggling, nothing here would be.
    """

    __slots__ = ("_records", "problems")

    def __init__(self, records, problems):
        self._records = records
        self.problems = problems


def fatal_problems(problems):
    """Everything except an interrupted tail. Structured, never grepped."""
    return [p for p in problems if p.get("kind") != "truncated_tail"]


def _fsync_directory(path):
    """Persist directory-entry changes before recording their conclusion."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
