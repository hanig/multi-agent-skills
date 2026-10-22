"""Managed descriptor consumer and independent nonblocking lock probe."""
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] /
                       "skills/hanig-orchestrate/scripts"))
import d5_locks as locks
import d5_protocol_locks as protocol


def main():
    action, directory = sys.argv[1:3]
    if action == "probe":
        with locks.pin(directory) as record:
            fd = record._open(int(sys.argv[3]))
            try:
                try:
                    locks.fcntl.flock(fd, locks.fcntl.LOCK_EX | locks.fcntl.LOCK_NB)
                except BlockingIOError:
                    print("blocked", flush=True)
                else:
                    print("available", flush=True)
            finally:
                os.close(fd)
    elif action == "inherited":
        inherited = int(sys.argv[3])
        with protocol.pin(directory) as record:
            with mock.patch.object(locks.fcntl, "flock", side_effect=AssertionError("conversion")):
                with record.drain(inherited_fd=inherited) as owned:
                    assert owned != inherited
                    assert not os.get_inheritable(owned)
                    os.close(inherited)
                    print("held", flush=True)
                    assert sys.stdin.readline() == "release\n"
        print("released", flush=True)
        # Stay alive after final close: process liveness is not protection.
        assert sys.stdin.readline() == "exit\n"
    else:
        raise AssertionError(action)


if __name__ == "__main__":
    main()
