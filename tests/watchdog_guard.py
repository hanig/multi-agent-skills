"""Check each in-process CLI exit before an alarm can kill the test runner."""

import signal


def guarded_main(entrypoint):
    """Keep the real watchdog; fail immediately on a leak, even on BaseException.

    Cancel a leaked alarm before raising so a failing regression remains safe
    to run in discovery. Restore the caller's handler for fixture isolation.
    """
    if not hasattr(signal, "SIGALRM"):
        return entrypoint()
    handler = signal.getsignal(signal.SIGALRM)
    try:
        return entrypoint()
    finally:
        pending = signal.alarm(0)
        signal.signal(signal.SIGALRM, handler)
        if pending:
            raise AssertionError("main() leaked a pending SIGALRM (%ss)" % pending)
