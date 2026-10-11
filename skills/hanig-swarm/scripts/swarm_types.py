"""Shared coordinator exceptions and result/exit codes (Python 3.8+)."""

# unit.py's exit codes are the ONLY judgement this coordinator consumes.
DONE, RUNNING, FAILED, PREEMPTED, INCOMPLETE, NEEDS_HUMAN = 0, 1, 2, 3, 4, 5
NAME = {0: "DONE", 1: "RUNNING", 2: "FAILED", 3: "PREEMPTED",
        4: "INCOMPLETE", 5: "NEEDS_HUMAN"}

EXIT_OK = 0
EXIT_HALTED = 1          # budget or runaway stopped new dispatch
EXIT_FAILED_UNIT = 2     # at least one unit is terminally failed
EXIT_USAGE = 64
# Two receipts claim different tracker refs for one intent, so something was
# filed twice, in two places. Not a usage error and not a clean run, so it
# gets its own code and a script can branch on it.
EXIT_CONFLICT = 3

# Advisory scope-check outcomes; neither nonzero value is merge permission.
EXIT_SCOPE_OUTSIDE = 1
EXIT_SCOPE_UNCHECKED = 2


class OutboxError(Exception):
    """The receipt journal cannot be written or read safely."""


class PlanError(Exception):
    pass
